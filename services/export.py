# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Streaming CSV/JSON export of a user's play history.

Extracted verbatim from app.py (behavior-preserving). Each play is re-emitted in
Spotify's own extended-streaming-history shape so an export re-imports cleanly
through the existing pipeline. app.py's /export-history route consumes
generateJsonExport / generateCsvExport, which stream in bounded chunks so an
export never holds the whole history in memory.
"""
import csv
import io
import json
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

from werkzeug.http import dump_options_header

EXPORT_CHUNK_SIZE = 5000         #< plays hydrated per round-trip while streaming an export
EXPORT_CSV_COLUMNS = ("played_at_utc", "track_name", "artists", "album", "ms_played", "spotify_track_uri", "played_from")

# Behavioral columns emitted as-is vs. as booleans - under Spotify's own
# key names (incognito is stored under the column name but exported as
# incognito_mode), so the export re-imports through _extractExtras.
EXPORT_TEXT_EXTRAS = ("platform", "conn_country", "reason_start", "reason_end")
EXPORT_BOOL_EXTRAS = (("shuffle", "shuffle"), ("skipped", "skipped"),
                      ("offline", "offline"), ("incognito", "incognito_mode"))


def _iterKeysetChunks(fetchRaw, hydrate):
    """Stream every row `fetchRaw(afterTs, afterId)` can return, oldest first,
    paging by position in the (played_at, playId) order instead of by OFFSET.

    OFFSET pagination assumed the rows behind the cursor never move. Inserts
    can't move them (a new play has the newest played_at), but DELETES can: an
    overwrite import's covered-range wipe, or the listener's Web-API
    reconciliation removing a play, shifts every later row left by one - and
    the next OFFSET then steps straight over that many entries, silently
    dropping them from the file the user downloads.

    played_at is not unique on its own (two different tracks can carry the
    same timestamp - the Musicolet importer can log a whole offline session
    in one burst), so the cursor is the composite (played_at, playId) key the
    underlying query orders by: `played_at > ? OR (played_at = ? AND id > ?)`
    is strictly increasing over that key and never revisits a row, so a
    cluster of rows sharing one timestamp is paged through by id instead of
    getting stuck - even when the cluster is bigger than EXPORT_CHUNK_SIZE.
    (An earlier, played_at-only cursor could not step past such a cluster at
    all: every fetch returned the same window, which either truncated the
    export there or - briefly, before aa7909c - alternated forever.)

    `fetchRaw` returns RAW play rows (no track hydration, but carrying
    `playId`); `hydrate` maps a chunk of them to exportable entries and may
    DROP rows it cannot hydrate (a dangling play whose catalog row is gone -
    Database._paginateEntries' contract). The cursor advances off the raw
    rows, not the hydrated ones, so a dropped row only ever loses itself:
    advancing off the hydrated output made every dropped row shorten its
    chunk below EXPORT_CHUNK_SIZE, which read as "last chunk" and silently
    truncated the export there."""
    afterTs = None
    afterId = None
    while True:
        rawEntries = fetchRaw(afterTs, afterId)
        if not rawEntries:
            return
        yield from hydrate(rawEntries)
        last = rawEntries[-1]
        afterTs = last.get("playedAt")
        afterId = last.get("playId")
        if len(rawEntries) < EXPORT_CHUNK_SIZE:
            return


def iterExportEntries(db, includeSkips=False):
    """Every play (oldest first) with hydrated track metadata, fetched in
    EXPORT_CHUNK_SIZE batches so an export never holds the whole history
    in memory. Fetched raw and hydrated per chunk - see _iterKeysetChunks
    for why the pager must never page on the hydrated entries.

    includeSkips: skip events (plays.is_skip=1) follow after every play (their
    sub-threshold ms_played re-imports as is_skip=1). JSON only - the CSV stays
    real-plays-only for spreadsheet use."""
    yield from _iterKeysetChunks(
        lambda afterTs, afterId: db.getEntriesFromOld(count=EXPORT_CHUNK_SIZE, afterTs=afterTs,
                                                       afterId=afterId, fullPagination=False),
        db.hydrateEntries)
    if not includeSkips:
        return
    yield from _iterKeysetChunks(
        lambda afterTs, afterId: db.getSkipEntriesFromOld(count=EXPORT_CHUNK_SIZE, afterTs=afterTs,
                                                           afterId=afterId, fullPagination=False),
        db.hydrateEntries)


# Spreadsheet apps (Excel, Sheets, LibreOffice) treat a cell whose text starts
# with one of these as a formula. Track/artist/album names come from Spotify's
# catalog rather than this app, but a name crafted to start with one of these
# could execute when the exported CSV is opened ("CSV formula injection"), so
# such a cell is prefixed with an apostrophe to force literal-text rendering.
# Only the human-readable text columns are guarded; the machine-readable
# URI/URL/ISRC columns importers match on are left byte-for-byte identical.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csvSafeCell(value) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def isoUtc(timestamp: float) -> str:
    """Spotify's `ts` shape, keeping any sub-second part.

    Flooring to whole seconds meant a play stored at ...565.7 exported as
    ...565 and re-imported 0.7s away from the original - which
    UNIQUE (username, track_id, played_at) does not catch, so the round trip
    ADDED a row instead of being a no-op.

    The fractional part is emitted only when there is one, so the ordinary
    whole-second case stays byte-identical to what Spotify itself writes and
    third-party consumers of this file see no change."""
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def exportEntryToDict(entry) -> dict:
    """One play in Spotify's own extended-streaming-history shape, so the
    export re-imports through the existing pipeline. `ts` is the play's
    END time - Spotify's convention, which importExtendedHistory converts
    back to a start time by subtracting ms_played. Behavioral fields are
    emitted only when stored; offline plays also carry offline_timestamp
    (their corrected start), which the importer prefers over ts."""
    artists = entry.get("artists") or []
    album = entry.get("album") or {}
    item = {
        "ts": isoUtc(entry["playedAt"] + entry["timePlayed"] // 1000),
        "ms_played": entry["timePlayed"],
        "master_metadata_track_name": entry.get("name"),
        "master_metadata_album_artist_name": artists[0].get("name") if artists else None,
        "master_metadata_album_album_name": album.get("name") if album else None,
        "spotify_track_uri": f"spotify:track:{entry['id']}",
        "played_from": entry.get("playedFrom"),   #< extra field; the importer ignores it
    }
    extras = entry.get("extras") or {}
    for column in EXPORT_TEXT_EXTRAS:
        if extras.get(column) is not None:
            item[column] = extras[column]
    for column, exportKey in EXPORT_BOOL_EXTRAS:
        if extras.get(column) is not None:
            item[exportKey] = bool(extras[column])
    if extras.get("offline"):
        item["offline_timestamp"] = int(entry["playedAt"])
    return item


def generateJsonExport(db):
    yield "[\n"
    first = True
    for entry in iterExportEntries(db, includeSkips=True):
        prefix = "" if first else ",\n"
        first = False
        yield prefix + json.dumps(exportEntryToDict(entry), ensure_ascii=False)
    yield "\n]\n"


def generateCsvExport(db):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_CSV_COLUMNS)
    for entry in iterExportEntries(db):
        artists = entry.get("artists") or []
        album = entry.get("album") or {}
        writer.writerow([
            isoUtc(entry["playedAt"]),   #< the START time - more intuitive for spreadsheet use
            _csvSafeCell(entry.get("name") or ""),
            _csvSafeCell(", ".join(a.get("name", "") for a in artists)),
            _csvSafeCell(album.get("name") or "" if album else ""),
            entry["timePlayed"],
            f"spotify:track:{entry['id']}",
            _csvSafeCell(entry.get("playedFrom") or ""),
        ])
        if buffer.tell() >= 64 * 1024:   #< flush in ~64KB chunks instead of per row or all at once
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    yield buffer.getvalue()


PLAYLIST_CSV_COLUMNS = ("Spotify URI", "Track Name", "Artist Name", "Album Name", "ISRC", "Spotify URL")


def generatePlaylistCsv(tracks: list[dict]):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(PLAYLIST_CSV_COLUMNS)
    for track in tracks:
        artists = track.get("artists") or []
        album = track.get("album") or {}
        artist_names = ", ".join(a.get("name", "") for a in artists)
        album_name = album.get("name", "") if isinstance(album, dict) else ""
        spotify_uri = f"spotify:track:{track['id']}"
        spotify_url = track.get("url") or f"https://open.spotify.com/track/{track['id']}"
        isrc = track.get("isrc") or ""
        writer.writerow([
            spotify_uri,
            _csvSafeCell(track.get("name", "")),
            _csvSafeCell(artist_names),
            _csvSafeCell(album_name),
            isrc,
            spotify_url,
        ])
        if buffer.tell() >= 64 * 1024:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    yield buffer.getvalue()


def _m3uSafeText(value: str) -> str:
    """M3U is line-oriented: a CR/LF inside a track or artist name would end the
    #EXTINF line early and let the rest be read as further playlist entries
    (an arbitrary extra URI). The CSV and XSPF exports escape their formats'
    metacharacters; this is the M3U equivalent."""
    return (value or "").replace("\r", " ").replace("\n", " ")


def generatePlaylistM3u(tracks: list[dict]):
    yield "#EXTM3U\n"
    for track in tracks:
        artists = track.get("artists") or []
        artist_names = _m3uSafeText(", ".join(a.get("name", "") for a in artists))
        title = _m3uSafeText(track.get("name", ""))
        spotify_uri = f"spotify:track:{track['id']}"
        yield f"#EXTINF:-1,{artist_names} - {title}\n{spotify_uri}\n"


def resolvePlaylistFormat(tracks: list[dict], fmt: str, title: str):
    """The (generator, mimetype) pair for one playlist download format.

    One resolver for every route that streams a playlist file (the Playlists
    page's tag export, Wrapped's Top 100, the Compare blend), so a new format
    or a mimetype fix lands everywhere at once. An unknown fmt degrades to
    CSV, matching the routes' own whitelist fallback.

    Bare mimetypes, not "...; charset=utf-8": Werkzeug's Response(mimetype=...)
    already appends "; charset=utf-8" for text/* and */*+xml types, so passing
    it here doubled the parameter (text/csv shipped as
    "text/csv; charset=utf-8; charset=utf-8"). XSPF's "+xml" suffix means it
    qualifies too, even though appending the parameter ourselves happened not
    to double it (the check is `endswith('+xml')`, which our own trailing
    parameter defeated) - made bare anyway for consistency. audio/x-mpegurl
    never gets this treatment either way."""
    if fmt == "m3u":
        return generatePlaylistM3u(tracks), "audio/x-mpegurl"
    if fmt == "xspf":
        return generatePlaylistXspf(tracks, title=title), "application/xspf+xml"
    return generatePlaylistCsv(tracks), "text/csv"


# The C0 control characters XML 1.0 forbids outright: everything below 0x20
# except tab (0x09), line feed (0x0A) and carriage return (0x0D). Unlike & and
# <, there is no escape for these - not even a numeric reference is legal - so
# one of them does not corrupt its own element, it makes the WHOLE document
# unparseable and the player rejects the file.
_XSPF_ILLEGAL_CHARS = str.maketrans(
    {code: None for code in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20))})


def _xspfSafeText(value: str) -> str:
    """XML 1.0 has no representation for most control characters, so they are
    dropped rather than escaped - the sibling of _m3uSafeText above, for the
    format whose metacharacters xml_escape already handles.

    Reachable from both sides of the app: track/artist/album names come from
    uploaded export files (the importer stores what the file says), and the
    playlist title is built out of ?tags= in routes/tags.py::playlistExport."""
    return (value or "").translate(_XSPF_ILLEGAL_CHARS)


def generatePlaylistXspf(tracks: list[dict], title: str = "SpotifyTracker Playlist"):
    import xml.sax.saxutils as xml_escape

    def xmlText(value):
        return xml_escape.escape(_xspfSafeText(value))

    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield '<playlist version="1" xmlns="http://xspf.org/ns/0/">\n'
    yield f'  <title>{xmlText(title)}</title>\n'
    yield '  <trackList>\n'
    for track in tracks:
        artists = track.get("artists") or []
        artist_names = ", ".join(a.get("name", "") for a in artists)
        track_title = track.get("name", "")
        album = track.get("album") or {}
        album_name = album.get("name", "") if isinstance(album, dict) else ""
        spotify_uri = f"spotify:track:{track['id']}"
        yield '    <track>\n'
        yield f'      <location>{xmlText(spotify_uri)}</location>\n'
        yield f'      <title>{xmlText(track_title)}</title>\n'
        if artist_names:
            yield f'      <creator>{xmlText(artist_names)}</creator>\n'
        if album_name:
            yield f'      <album>{xmlText(album_name)}</album>\n'
        yield '    </track>\n'
    yield '  </trackList>\n'
    yield '</playlist>\n'


#< What RFC 5987 lets stand unescaped inside a filename* value - the same set
#  werkzeug.utils.send_file uses.
RFC5987_SAFE_CHARS = "!#$&+-.^_`|~"


def attachmentDisposition(filename: str) -> str:
    """The Content-Disposition value for a download called `filename`.

    Usernames are minted from the email's local part with str.isalnum(), which
    is Unicode-aware, so a name built from one can carry ł, š, ğ, Cyrillic or
    CJK - and waitress serializes every header line as latin-1, so a raw
    `filename="..."` holding any of those could not be sent at all: the one
    in-app export was unusable for that account (German umlauts happen to be
    latin-1 and never showed it). This is RFC 6266 / 5987's shape, the one
    werkzeug.utils.send_file emits: an ASCII `filename=` for clients that read
    only that, plus `filename*=UTF-8\'\'...` carrying the real name. ASCII
    names stay exactly as they were."""
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        simple = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
        names = {"filename": simple,
                 "filename*": f"UTF-8''{quote(filename, safe=RFC5987_SAFE_CHARS)}"}
    else:
        names = {"filename": filename}
    return dump_options_header("attachment", names)
