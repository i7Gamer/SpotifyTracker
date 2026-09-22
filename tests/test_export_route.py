"""GET /export-history - users can get their full play history back out.

The JSON format is shaped like Spotify's own extended streaming history
export, with `ts` as the play's END time (Spotify's convention - the
importer subtracts ms_played back off), so an export from one instance
re-imports cleanly into another through the existing import pipeline.
"""
import csv
import io
import itertools
import json
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase, makeDatabaseWithData
from app import SpotifyDashboardApp
from _app_factory import AppTestCase
from Database.Importers.StreamingHistoryImporter import Importer
from Database.utils import timeToInt
from services.export import isoUtc
import services.export as exportModule

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

_TRACKS = {
    "t1": {"id": "t1", "name": "First Song", "artists": [{"id": "a1", "name": "Artist One"}]},
    "t2": {"id": "t2", "name": "Second Song", "artists": [{"id": "a2", "name": "Artist Two"}]},
}
_ENTRIES = [
    {"id": "t1", "playedAt": 1700000000, "timePlayed": 200000, "playedFrom": "playlist:pl1"},
    {"id": "t2", "playedAt": 1700005000, "timePlayed": 180000},
]


class _AppTestBase(AppTestCase):
    def _get(self, dash, db, path):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)


class TestPlayedFromRoundTrips(unittest.TestCase):
    """The JSON export's whole purpose is that it re-imports (its docstring says
    so), but played_from - the playlist/album a play came from - was emitted and
    then dropped: _extendedEntryTuple never carried it, so the column came back
    NULL. 646 live rows carry a context, and it is the only source for the
    listening-source breakdown.

    Spotify's own exports have no played_from field at all, so importing one is
    unaffected."""

    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        return importer

    def _entry(self, playedFrom):
        entry = {
            "ts": "2023-05-01T10:00:00Z", "ms_played": 150000,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": "spotify:track:track123",
        }
        if playedFrom is not None:
            entry["played_from"] = playedFrom
        return entry

    def test_the_context_survives_the_round_trip(self):
        metas = list(self._importer().importExtendedHistory(
            [self._entry("playlist:37i9dQZF1DXcBWIGoYBM5M")], known=[], progressCallback=None))

        self.assertEqual(metas[0]["playedFrom"], "playlist:37i9dQZF1DXcBWIGoYBM5M")

    def test_an_entry_without_a_context_carries_none(self):
        """A genuine Spotify export - no played_from key anywhere."""
        metas = list(self._importer().importExtendedHistory(
            [self._entry(None)], known=[], progressCallback=None))

        self.assertIsNone(metas[0]["playedFrom"])

    def test_a_non_string_context_is_ignored_rather_than_stored(self):
        """The field is our own extension, so an edited file can carry anything;
        played_from is later split on ':' to resolve a playlist name."""
        metas = list(self._importer().importExtendedHistory(
            [self._entry({"not": "a string"})], known=[], progressCallback=None))

        self.assertIsNone(metas[0]["playedFrom"])


class TestSubSecondTimestampsSurvive(unittest.TestCase):
    """plays.played_at is REAL, so a fractional timestamp is storable, and
    isoUtc floored it away - a play at ...565.7 would export as ...565 and
    re-import 0.7s from the original, which UNIQUE (username, track_id,
    played_at) does not catch, so the round trip would ADD a row.

    Not reachable today: every write path floors through
    Client.embedPlayInfo -> timeToInt, so no fractional played_at exists (0 rows
    on the live database). This closes the export half only - the import half
    would mean changing timeToInt's int contract across every caller to serve a
    value nothing produces, which is not a trade worth making. If a fractional
    played_at ever becomes writable, the importer is where to look next."""

    def test_a_whole_second_timestamp_is_formatted_exactly_as_before(self):
        """The case that actually occurs. Spotify's own format has no fractional
        part, so this must stay byte-identical or third-party consumers of the
        file see a format change for nothing."""
        self.assertEqual(isoUtc(1700000000), "2023-11-14T22:13:20Z")

    def test_a_whole_second_timestamp_round_trips_exactly(self):
        self.assertEqual(timeToInt(isoUtc(1700000000)), 1700000000)

    def test_a_fractional_timestamp_is_no_longer_thrown_away_on_the_way_out(self):
        self.assertEqual(isoUtc(1700000000.75), "2023-11-14T22:13:20.750000Z")


class TestExportRoute(DatabaseTestCase, _AppTestBase):
    def _makeSeededDb(self):
        return self._makeDb(_TRACKS, _ENTRIES, username="alice")

    def test_logged_out_redirects_to_login(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.get("/export-history")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_json_export_is_a_download_with_all_plays(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeSeededDb(), "/export-history?format=json")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(".json", resp.headers["Content-Disposition"])

        items = json.loads(resp.get_data(as_text=True))
        self.assertEqual(len(items), 2)
        byUri = {item["spotify_track_uri"]: item for item in items}
        first = byUri["spotify:track:t1"]
        self.assertEqual(first["master_metadata_track_name"], "First Song")
        self.assertEqual(first["master_metadata_album_artist_name"], "Artist One")
        self.assertEqual(first["ms_played"], 200000)
        # ts is the play's END time (Spotify's extended-export convention).
        self.assertEqual(timeToInt(first["ts"]), 1700000000 + 200000 // 1000)

    def test_csv_export_has_header_and_rows(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeSeededDb(), "/export-history?format=csv")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(".csv", resp.headers["Content-Disposition"])
        #< resp.mimetype strips parameters, so it can never see Werkzeug
        #  doubling "; charset=utf-8" for a text/* Response(mimetype=...) -
        #  only the exact header catches that (R5, 2026-09-06 review)
        self.assertEqual(resp.headers["Content-Type"], "text/csv; charset=utf-8")
        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        self.assertEqual(rows[0][:4], ["played_at_utc", "track_name", "artists", "album"])
        self.assertEqual(len(rows), 3)   #< header + 2 plays
        trackNames = {row[1] for row in rows[1:]}
        self.assertEqual(trackNames, {"First Song", "Second Song"})

    def test_unknown_format_falls_back_to_json(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeSeededDb(), "/export-history?format=xml")
        self.assertEqual(resp.status_code, 200)
        json.loads(resp.get_data(as_text=True))   #< parses as JSON

    def test_empty_history_exports_an_empty_json_list(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb({}, [], username="alice"), "/export-history")
        self.assertEqual(json.loads(resp.get_data(as_text=True)), [])


class TestExportBehavioralFields(DatabaseTestCase, _AppTestBase):
    """Behavioral columns round-trip under Spotify's own key names; skip
    events follow the plays as sub-threshold entries (JSON only)."""

    EXTRAS = {
        "platform": "ios", "conn_country": "CH", "reason_start": "clickrow",
        "reason_end": "trackdone", "shuffle": 1, "skipped": 0, "offline": 1, "incognito": 0,
    }

    def _exportItems(self, db):
        dash = self._makeApp()
        resp = self._get(dash, db, "/export-history?format=json")
        return json.loads(resp.get_data(as_text=True))

    def test_behavioral_fields_use_spotify_key_names(self):
        db = self._makeDb(_TRACKS, [], username="alice")
        db.repo.insertPlay("alice", "t1", 1700000000, 200000, extras=self.EXTRAS)
        db.repo.commit()

        item = self._exportItems(db)[0]

        self.assertEqual(item["platform"], "ios")
        self.assertEqual(item["conn_country"], "CH")
        self.assertEqual(item["reason_start"], "clickrow")
        self.assertEqual(item["reason_end"], "trackdone")
        self.assertIs(item["shuffle"], True)
        self.assertIs(item["skipped"], False)
        self.assertIs(item["offline"], True)
        self.assertIs(item["incognito_mode"], False)
        self.assertNotIn("incognito", item)   #< only the Spotify key name
        # Offline plays carry their corrected start so a reimport reconstructs it
        self.assertEqual(item["offline_timestamp"], 1700000000)

    def test_entries_without_behavioral_fields_omit_the_keys(self):
        db = self._makeDb(_TRACKS, _ENTRIES, username="alice")

        item = self._exportItems(db)[0]

        for key in ("platform", "conn_country", "reason_start", "reason_end",
                    "shuffle", "skipped", "offline", "incognito_mode", "offline_timestamp"):
            self.assertNotIn(key, item)

    def test_online_play_has_no_offline_timestamp(self):
        db = self._makeDb(_TRACKS, [], username="alice")
        db.repo.insertPlay("alice", "t1", 1700000000, 200000, extras={"offline": 0, "platform": "ios"})
        db.repo.commit()

        item = self._exportItems(db)[0]

        self.assertIs(item["offline"], False)
        self.assertNotIn("offline_timestamp", item)

    def test_skips_are_exported_after_plays(self):
        db = self._makeDb(_TRACKS, _ENTRIES, username="alice")
        db.repo.insertPlay("alice", "t2", 1700009000, 400, is_skip=1, extras={"reason_end": "fwdbtn"})
        db.repo.commit()

        items = self._exportItems(db)

        self.assertEqual(len(items), 3)
        skipItem = items[-1]   #< skips follow every play
        self.assertEqual(skipItem["ms_played"], 400)
        self.assertEqual(skipItem["spotify_track_uri"], "spotify:track:t2")
        self.assertEqual(skipItem["reason_end"], "fwdbtn")

    def test_csv_export_stays_plays_only(self):
        db = self._makeDb(_TRACKS, _ENTRIES, username="alice")
        db.repo.insertPlay("alice", "t2", 1700009000, 400, is_skip=1)
        db.repo.commit()

        dash = self._makeApp()
        resp = self._get(dash, db, "/export-history?format=csv")
        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))

        self.assertEqual(len(rows), 3)   #< header + the 2 plays, no skip row


class TestExportSurvivesDanglingPlays(DatabaseTestCase):
    """A play whose catalog row is gone (a dangling FK row - the corruption the
    startup probe counts and migrate1_43_0 repairs) is dropped from the export,
    but must not TRUNCATE it: the keyset pager used to measure chunk exhaustion
    from the HYDRATED entries, so every dropped row made a full chunk look like
    the last one and everything after it was silently lost (12 plays in, 4
    exported, no error anywhere)."""

    _CHUNK = 5     #< small enough that a 12-play history spans several chunks
    _PLAYS = 12

    def _makeDbWithDanglingTrack(self, danglingId, skipTimes=()):
        tracks = {f"t{i}": {"id": f"t{i}", "name": f"Song {i}", "artists": []}
                  for i in range(self._PLAYS)}
        entries = [{"id": f"t{i}", "playedAt": 1700000000 + i * 100, "timePlayed": 200000}
                   for i in range(self._PLAYS)]
        db = self._makeDb(tracks, entries)
        for ts in skipTimes:
            db.repo.insertPlay("testuser", danglingId, ts, 400, is_skip=1)
        db.repo.commit()
        # Delete the catalog row out from under its play - only possible with
        # the FK enforcement off, which is exactly how the real dangling rows
        # predating that enforcement came to exist.
        conn = db.repo.connection()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM tracks WHERE id=?", (danglingId,))
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        return db

    def _exportedTrackIds(self, db):
        import services.export as exportModule
        with patch.object(exportModule, "EXPORT_CHUNK_SIZE", self._CHUNK):
            items = json.loads("".join(exportModule.generateJsonExport(db)))
        return [item["spotify_track_uri"].removeprefix("spotify:track:") for item in items]

    def test_a_dangling_play_mid_chunk_does_not_truncate_the_export(self):
        db = self._makeDbWithDanglingTrack("t2")
        self.assertEqual(self._exportedTrackIds(db),
                         [f"t{i}" for i in range(self._PLAYS) if i != 2])

    def test_a_dangling_play_at_a_chunk_boundary_does_not_truncate_or_loop(self):
        #< t4 is the last row of the first chunk, so the keyset cursor itself
        #  must advance off the raw rows, not the hydrated ones
        db = self._makeDbWithDanglingTrack("t4")
        self.assertEqual(self._exportedTrackIds(db),
                         [f"t{i}" for i in range(self._PLAYS) if i != 4])

    def test_a_dangling_skip_does_not_truncate_the_skip_section(self):
        skipTimes = [1700100000 + i * 100 for i in range(7)]
        db = self._makeDbWithDanglingTrack("t3", skipTimes=skipTimes)
        exported = self._exportedTrackIds(db)
        # The plays section loses only t3's play; the skip section (all seven
        # skips are of the deleted track) empties without ending the export.
        self.assertEqual(exported, [f"t{i}" for i in range(self._PLAYS) if i != 3])


class TestSkipAndOfflineRoundTrip(DatabaseTestCase, _AppTestBase):
    def test_offline_play_and_skip_survive_a_reimport(self):
        sourceDb = self._makeDb(_TRACKS, [], username="alice")
        sourceDb.repo.insertPlay("alice", "t1", 1700000000, 200000,
                                 extras={"platform": "ios", "offline": 1, "reason_end": "trackdone"})
        sourceDb.repo.insertPlay("alice", "t2", 1700005000, 400, is_skip=1,
                                 extras={"reason_end": "fwdbtn", "skipped": 1})
        sourceDb.repo.commit()

        dash = self._makeApp()
        exportedJson = self._get(dash, sourceDb, "/export-history?format=json").get_data(as_text=True)

        bareImporter = Importer.__new__(Importer)
        bareImporter.sp = MagicMock()
        targetDb = self._makeDb(_TRACKS, [], username="bob")
        with patch("Database.database.Importer", return_value=bareImporter):
            targetDb.importHistory(exportedJson)

        playRows = targetDb.repo._conn().execute(
            "SELECT * FROM plays WHERE username='bob' AND is_skip=0").fetchall()
        self.assertEqual(len(playRows), 1)
        play = dict(playRows[0])
        self.assertEqual(play["played_at"], 1700000000)   #< corrected offline start survives
        self.assertEqual(play["platform"], "ios")
        self.assertEqual(play["offline"], 1)
        self.assertEqual(play["reason_end"], "trackdone")

        skipRows = targetDb.repo._conn().execute(
            "SELECT * FROM plays WHERE username='bob' AND is_skip=1").fetchall()
        self.assertEqual(len(skipRows), 1)
        skip = dict(skipRows[0])
        self.assertEqual(skip["track_id"], "t2")
        self.assertEqual(skip["played_at"], 1700005000)
        self.assertEqual(skip["time_played"], 400)
        self.assertEqual(skip["reason_end"], "fwdbtn")


class TestExportRoundTrip(DatabaseTestCase, _AppTestBase):
    """The whole point of the JSON format: it re-imports through the existing
    pipeline, reproducing the same plays in a fresh database."""

    def test_export_reimports_into_a_fresh_database(self):
        dash = self._makeApp()
        sourceDb = self._makeDb(_TRACKS, _ENTRIES, username="alice")
        resp = self._get(dash, sourceDb, "/export-history?format=json")
        exportedJson = resp.get_data(as_text=True)

        # The export must parse as a recognized Spotify extended export.
        bareImporter = Importer.__new__(Importer)   #< skips Spotify client construction
        bareImporter.sp = MagicMock()
        parsed, exportType = bareImporter._convertToList(exportedJson)
        self.assertEqual(exportType, "spotifyExtendedExport")
        self.assertEqual(len(parsed), 2)

        # Import it into a fresh database seeded with the same catalog (so no
        # metadata lookups are needed) - the plays must come back identical.
        targetDb = self._makeDb(_TRACKS, [], username="bob")
        with patch("Database.database.Importer", return_value=bareImporter):
            targetDb.importHistory(exportedJson)

        imported = {(e["id"], int(e["playedAt"]), e["timePlayed"])
                    for e in targetDb.getEntriesFromOld(fullPagination=False)}
        expected = {(e["id"], e["playedAt"], e["timePlayed"]) for e in _ENTRIES}
        self.assertEqual(imported, expected)


class TestKeysetPagerTermination(unittest.TestCase):
    """The pager advances on the composite (played_at, playId) key the
    underlying query orders by (X4) - played_at alone is not unique (two
    different tracks can log at the exact same instant, the shape a
    Musicolet import session writes in one burst), so a group of rows
    sharing one timestamp is paged through by playId instead of stalling.

    Before the composite key, the pager restarted each chunk AT the last
    timestamp seen (`played_at >= ?`) and could not step past a group bigger
    than one chunk at all: the next fetch returned the exact same window, so
    a cluster of EXPORT_CHUNK_SIZE+ rows at one timestamp silently truncated
    the export right there (an earlier version of this pager instead
    alternated the two halves of such a group forever - an export that never
    ends - fixed in aa7909c by stopping instead; the composite key removes
    the need to stop at all).

    Driven directly with a small chunk size: reproducing the cluster case
    through the real query would need EXPORT_CHUNK_SIZE (5000) plays sharing
    one exact played_at, which is what makes it latent rather than live - and
    also what makes it worth a test, since nothing else here would ever reach
    it (see TestExportPagesThroughATimestampCluster for the query-level
    version, with a real chunk size small enough to hit it directly)."""

    CHUNK = 3

    def _rows(self):
        #< the boundary falls INSIDE the T group: one row before it, three on it
        return ([{"id": "a", "playedAt": 100.0, "playId": 1}]
                + [{"id": f"r{i}", "playedAt": 200.0, "playId": i} for i in range(1, 4)])

    def _fetch(self, rows):
        def fetchRaw(afterTs, afterId):
            #< the real queries order by (played_at, id) ASC and take
            #  played_at > ? OR (played_at = ? AND id > ?), LIMIT chunk
            if afterTs is None:
                eligible = rows
            else:
                eligible = [r for r in rows if (r["playedAt"], r["playId"]) > (afterTs, afterId)]
            return sorted(eligible, key=lambda r: (r["playedAt"], r["playId"]))[:self.CHUNK]
        return fetchRaw

    def test_every_row_is_emitted_exactly_once_and_the_export_ends(self):
        rows = self._rows()
        with patch.object(exportModule, "EXPORT_CHUNK_SIZE", self.CHUNK):
            #< islice is the hang guard: before the aa7909c fix this generator is infinite
            emitted = list(itertools.islice(
                exportModule._iterKeysetChunks(self._fetch(rows), lambda chunk: chunk),
                len(rows) * 4))

        self.assertEqual([r["id"] for r in emitted], ["a", "r1", "r2", "r3"])

    def test_a_cluster_larger_than_one_chunk_pages_through_by_id(self):
        """X4: a group at one timestamp bigger than a chunk. The old
        played_at-only cursor could never step past it (the next fetch
        returned the same window again) and silently truncated the export
        here; the composite (played_at, playId) key pages through it by id."""
        rows = [{"id": f"r{i}", "playedAt": 200.0, "playId": i} for i in range(1, 6)]
        with patch.object(exportModule, "EXPORT_CHUNK_SIZE", self.CHUNK):
            emitted = list(itertools.islice(
                exportModule._iterKeysetChunks(self._fetch(rows), lambda chunk: chunk),
                len(rows) * 4))

        self.assertEqual([r["id"] for r in emitted], [f"r{i}" for i in range(1, 6)])

    def test_ordinary_paging_is_unaffected(self):
        rows = [{"id": f"r{i}", "playedAt": 100.0 + i, "playId": i} for i in range(1, 8)]
        with patch.object(exportModule, "EXPORT_CHUNK_SIZE", self.CHUNK):
            emitted = list(itertools.islice(
                exportModule._iterKeysetChunks(self._fetch(rows), lambda chunk: chunk),
                len(rows) * 4))

        self.assertEqual([r["id"] for r in emitted], [f"r{i}" for i in range(1, 8)])

    def test_a_dropped_unhydratable_row_does_not_truncate_the_export(self):
        """The pager measures chunks on RAW rows precisely so a row hydrate()
        drops only loses itself - pinned here because the fix touches the same
        bookkeeping."""
        rows = [{"id": f"r{i}", "playedAt": 100.0 + i, "playId": i} for i in range(1, 8)]
        with patch.object(exportModule, "EXPORT_CHUNK_SIZE", self.CHUNK):
            emitted = list(itertools.islice(
                exportModule._iterKeysetChunks(
                    self._fetch(rows),
                    lambda chunk: [r for r in chunk if r["id"] != "r2"]),
                len(rows) * 4))

        self.assertEqual([r["id"] for r in emitted],
                         [f"r{i}" for i in range(1, 8) if i != 2])


class TestExportPagesThroughATimestampCluster(DatabaseTestCase):
    """X4, at the query level: the Musicolet importer can log a whole offline
    session with several plays stamped at the exact same played_at. A cluster
    bigger than one export chunk used to silently truncate the export right
    there (TestKeysetPagerTermination covers the pager unit in isolation);
    the composite (played_at, playId) keyset cursor pages through it."""

    _CHUNK = 3
    _CLUSTER_SIZE = 5   #< bigger than a chunk, so the cluster spans two fetches

    def test_a_cluster_larger_than_one_chunk_is_not_truncated(self):
        clusterTs = 1700000000
        laterTs = clusterTs + 100
        trackCount = self._CLUSTER_SIZE + 1
        tracks = {f"t{i}": {"id": f"t{i}", "name": f"Song {i}", "artists": []}
                  for i in range(trackCount)}
        #< CLUSTER_SIZE distinct tracks at the same timestamp, then one later play
        entries = [{"id": f"t{i}", "playedAt": clusterTs, "timePlayed": 200000}
                   for i in range(self._CLUSTER_SIZE)]
        entries.append({"id": f"t{self._CLUSTER_SIZE}", "playedAt": laterTs, "timePlayed": 200000})
        db = self._makeDb(tracks, entries)

        with patch.object(exportModule, "EXPORT_CHUNK_SIZE", self._CHUNK):
            items = json.loads("".join(exportModule.generateJsonExport(db)))

        exportedIds = [item["spotify_track_uri"].removeprefix("spotify:track:") for item in items]
        self.assertEqual(exportedIds, [f"t{i}" for i in range(trackCount)])


class TestExportLinksLiveOnTheImportPage(_AppTestBase):
    """History in and history out are the same job in the same format, so the
    download links sit with the upload form rather than on the settings page
    they used to share with API keys and share requests."""

    def _makeDb(self):
        db = MagicMock()
        db.getImportProgress.return_value = {"status": "idle", "percentage": 0, "message": ""}
        return db

    def test_import_page_offers_both_formats(self):
        dash = self._makeApp()

        body = self._get(dash, self._makeDb(), "/import").data

        self.assertIn(b"/export-history?format=json", body)
        self.assertIn(b"/export-history?format=csv", body)

    def test_import_page_is_titled_for_both_directions(self):
        dash = self._makeApp()

        body = self._get(dash, self._makeDb(), "/import").data

        self.assertIn(b"Import &amp; Export", body)

    def test_nav_entry_names_both_directions(self):
        dash = self._makeApp()

        body = self._get(dash, self._makeDb(), "/import").data

        self.assertIn(b">Import &amp; Export</a>", body)

    def test_profile_no_longer_offers_the_downloads(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getUserSpotifyCredentials.return_value = {}
        db.getUserLastfmApiKey.return_value = None

        body = self._get(dash, db, "/profile").data

        self.assertNotIn(b"/export-history", body)
        self.assertNotIn(b"Export as JSON", body)

    def test_export_card_has_compact_export_card_class(self):
        dash = self._makeApp()

        body = self._get(dash, self._makeDb(), "/import").data

        self.assertIn(b'class="import-card export-card"', body)

    def test_export_panel_is_not_a_generic_card(self):
        """`.card` adds a 260px min-height and a hover lift meant for the
        dashboard tiles; on a two-line download panel that reads as padding."""
        dash = self._makeApp()

        body = self._get(dash, self._makeDb(), "/import").data

        self.assertNotIn(b'class="card import-card export-card"', body)

    def test_import_panel_is_headed_like_the_export_panel(self):
        """The two panels are siblings, so the upload half gets a title in the
        same shape as "Export your history" instead of a bare field label."""
        dash = self._makeApp()

        body = self._get(dash, self._makeDb(), "/import").data

        self.assertIn(b'<h2 class="import-card-heading">Import your history</h2>', body)
        self.assertIn(b"<h2>Export your history</h2>", body)
        self.assertNotIn(b'<label for="history_file">', body)

    def test_the_file_input_keeps_its_accessible_name(self):
        """The visible <label> went away with the heading; the name it gave the
        file input must not go with it."""
        dash = self._makeApp()

        body = self._get(dash, self._makeDb(), "/import").data

        self.assertIn(b'aria-label="Spotify history ZIP, JSON or CSV file(s)"', body)



if __name__ == "__main__":
    unittest.main()


class TestTheDownloadNameSurvivesANonLatin1Username(DatabaseTestCase, _AppTestBase):
    """Usernames are minted from the email's local part with str.isalnum(),
    which is Unicode-aware - so `łukasz@example.com` becomes the username
    `łukasz`, verbatim. The export put that straight into
    Content-Disposition, and waitress serializes the whole header block as
    latin-1: for any letter outside Latin-1 (ł, š, ğ, Cyrillic,
    CJK) the response could not be built at all, and the one in-app export was
    unusable for that account. German umlauts happen to be Latin-1 and never
    showed it.

    The fix is the shape Werkzeug's own send_file uses (RFC 6266 / 5987): an
    ASCII `filename` for old clients plus `filename*=UTF-8''...` carrying the
    real name, and the header line is then latin-1 by construction."""

    def _getAs(self, dash, db, path, username):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value=username), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = username + '@example.com'
            return client.get(path)

    def test_a_non_latin1_username_still_gets_a_download(self):
        dash = self._makeApp()
        db = self._makeDb(_TRACKS, _ENTRIES, username="łukasz")

        resp = self._getAs(dash, db, "/export-history?format=csv", "łukasz")

        self.assertEqual(resp.status_code, 200)
        disposition = resp.headers["Content-Disposition"]
        # What waitress does with every header line (task.py: res.encode("latin-1")).
        disposition.encode("latin-1")
        self.assertIn("attachment", disposition)
        self.assertIn("filename*=UTF-8''spotify_stats_export_%C5%82ukasz_", disposition)
        self.assertIn("filename=spotify_stats_export_ukasz_", disposition)
        self.assertTrue(disposition.endswith(".csv"))

    def test_an_ascii_username_keeps_the_plain_filename(self):
        dash = self._makeApp()
        resp = self._getAs(dash, self._makeSeededDb(), "/export-history?format=json", "alice")

        disposition = resp.headers["Content-Disposition"]
        self.assertIn("filename=spotify_stats_export_alice_", disposition)
        self.assertNotIn("filename*", disposition)
        self.assertTrue(disposition.endswith(".json"))

    def _makeSeededDb(self):
        return self._makeDb(_TRACKS, _ENTRIES, username="alice")


class TestAttachmentDisposition(unittest.TestCase):
    """The pure helper behind the header, so the encoding rule is pinned
    without a request."""

    def test_ascii_names_are_emitted_plainly(self):
        self.assertEqual(exportModule.attachmentDisposition("export_alice.csv"),
                         "attachment; filename=export_alice.csv")

    def test_non_ascii_names_get_the_rfc5987_form_and_a_latin1_safe_line(self):
        value = exportModule.attachmentDisposition("export_łukasz.csv")
        value.encode("latin-1")
        self.assertEqual(value, "attachment; filename=export_ukasz.csv; "
                                "filename*=UTF-8''export_%C5%82ukasz.csv")

    def test_latin1_but_non_ascii_names_take_the_same_form(self):
        """jürgen would encode as latin-1, but a bare non-ASCII `filename=`
        is undefined by RFC 6266 and browsers disagree on it - the * form is
        the one that is defined."""
        value = exportModule.attachmentDisposition("export_jürgen.csv")
        value.encode("latin-1")
        self.assertIn("filename=export_jurgen.csv", value)
        self.assertIn("filename*=UTF-8''export_j%C3%BCrgen.csv", value)
