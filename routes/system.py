# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""System / utility routes: health check, history import + export, import
progress, version status, and the small listener/now-playing JSON APIs.

Extracted verbatim from app.py. Streaming export uses services/export; the
app-level MAX_UPLOAD_MB / EXPORT_FORMATS constants are aliased from the app
module. The 413 upload-too-large error handler stays app-global in app.py.
"""
import logging
import threading

from flask import (
    render_template, redirect, request, url_for, jsonify, Response, stream_with_context,
)

from config import (
    MAX_UPLOAD_MB, MAX_UNCOMPRESSED_IMPORT_MB, MAX_IMPORT_ARCHIVE_ENTRIES,
    BYTES_PER_MB, EXPORT_FORMATS,
)
from Database.Spotify.formatting import openSpotifyUrl
from Database.utils import versionTuple, now
from routes._auth import makeRequiresUser
from services.export import generateJsonExport, generateCsvExport, attachmentDisposition
from services.import_upload import expandUploads

logger = logging.getLogger(__name__)

# Resolved once here rather than per request; the MB figure stays the one a
# human edits (config.py) and the template quotes back.
MAX_UNCOMPRESSED_IMPORT_BYTES = MAX_UNCOMPRESSED_IMPORT_MB * BYTES_PER_MB


def _friendChip(friend):
    """One friends-strip chip, with the three links it renders as.

    The chip names three things and each one goes somewhere different: the
    friend to /compare, the track to its page, every artist to theirs. The
    track and the artists point at OUR detail pages only where the viewer has
    their own plays (getFriendsNowPlaying._markViewerPlayed decides that) and
    out to Spotify otherwise - /song/<id> for a track the viewer has never
    played bounces off _missingEntityResponse.

    Built here rather than in dashboard-page.js because the chips are rendered
    client-side: a path spelled out there would be unroutable by url_for. The
    per-friend `played` flags are consumed here and do not survive into the
    JSON. `with` is a Python keyword, hence the kwargs dict."""
    trackId = friend["trackId"]
    return {
        "username": friend["username"],
        "displayName": friend["displayName"],
        "name": friend["name"],
        "artistsText": friend["artistsText"],
        "imageId": friend["imageId"],
        "compareUrl": url_for("comparePage", **{"with": friend["username"]}),
        #< openSpotifyUrl answers "" for a missing id, which renders as plain
        #  text - an empty href would be a link back to the current page
        "trackUrl": (url_for("songDetailPage", track_id=trackId)
                     if trackId and friend["trackPlayedByViewer"]
                     else openSpotifyUrl("track", trackId)),
        "artists": [
            {"name": artist["name"],
             "url": (url_for("artistDetailPage", artist_id=artist["id"])
                     if artist["playedByViewer"]
                     else openSpotifyUrl("artist", artist["id"]))}
            for artist in friend["artists"]
        ],
    }


# Progress statuses that mean "this import is over". Anything else - notably
# the 'running' the route claims before starting the thread - means the slot is
# still held and has to be released before the thread exits (see
# _runImportBatch). Kept as a set here rather than as a check for == "running"
# so a future intermediate status can't silently be treated as terminal.
_TERMINAL_IMPORT_STATUSES = frozenset({"complete", "failed"})

_IMPORT_CRASH_MESSAGE = (
    "Import stopped unexpectedly. Nothing further was written - check the server "
    "log for the error, then try the import again."
)


def register(app, dashboard):

    def _is_version_newer(remote: str, local: str) -> bool:
        try:
            return versionTuple(remote) > versionTuple(local)
        except Exception:
            return False

    def health():
        """Cheap, unauthenticated liveness/readiness check for container
        orchestration and uptime monitoring - does a trivial query rather
        than just returning 200 unconditionally, so it can tell "process
        alive" apart from "process alive but the database is unreachable"
        (the single point of failure for this app)."""
        try:
            dashboard.repo.connection().execute("SELECT 1").fetchone()
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            # Detail stays in the server log only - this endpoint is unauthenticated,
            # and a raw SQLite error can leak filesystem paths / permission details.
            logger.error("Health check failed: %s", e)
            return jsonify({"status": "error"}), 503
    app.add_url_rule("/health", "health", health, methods=["GET"])

    def importHistory():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        uploads = [f for f in request.files.getlist("history_file") if f and f.filename]
        if not uploads:
            return redirect(url_for("importPage"))

        # Decoding (and unpacking a ZIP export) lives in services/import_upload
        # so the uncompressed-size guard is testable without a request: see
        # that module for why MAX_CONTENT_LENGTH stops being enough once the
        # server is the one unpacking. unreadableCount is COUNTED rather than
        # merely logged because the drop happens above every guard the batch
        # has for unreadable input - in overwrite mode it would delete the
        # covered range of the survivors, which can bracket the dropped file's
        # plays, with nothing left to re-insert them (see importHistoryBatch's
        # unreadableFileCount).
        expansion = expandUploads(uploads, MAX_UNCOMPRESSED_IMPORT_BYTES,
                                  maxArchiveEntries=MAX_IMPORT_ARCHIVE_ENTRIES)
        contents = expansion.contents
        unreadableCount = expansion.unreadableCount
        if expansion.tooManyEntries:
            #< the byte budget is blind to this one: empty entries are free to
            #  store and costly to open, so the count needs its own ceiling
            logger.warning("Refusing import for user %s: an archive holds more than %d entries",
                           username, MAX_IMPORT_ARCHIVE_ENTRIES)
            return redirect(url_for("importPage", error="too_many_entries"))
        if expansion.exceededCap:
            #< NOT upload_too_large: the request itself passed
            #  MAX_CONTENT_LENGTH, so "try uploading fewer files at once" is
            #  wrong advice for a small archive that unpacks to a large one
            logger.warning("Refusing import for user %s: the upload unpacks past the %d MB cap",
                           username, MAX_UNCOMPRESSED_IMPORT_MB)
            return redirect(url_for("importPage", error="expanded_too_large"))
        if not contents:
            #< the arms the batch never hears about at all, so the message has
            #  to come from here: an unannounced bounce back to /import looks
            #  exactly like a click that did nothing
            if expansion.emptyArchive:
                return redirect(url_for("importPage", error="empty_archive"))
            return redirect(url_for("importPage", error="unreadable_upload"))

        # Captured before the thread starts - no request context inside it.
        overwriteRange = request.form.get("overwrite_range") is not None

        # Atomically claim the "running" slot. This both marks progress running
        # synchronously (so the state is correct the instant the redirect
        # returns - no post-thread-start time.sleep needed) AND rejects a
        # double-submit: the old readProgress()==running check and the later
        # writeProgress("running") were separate steps, so two rapid submissions
        # could both pass the check and launch two import threads concurrently
        # rewriting the same user's history.
        if not db.tryClaimImportRunning():
            return redirect(url_for("importPage"))

        def _releaseImportSlot():
            """Write a terminal progress row if the import didn't write one.

            The claim above is a lock, not just a status: tryClaimImportRunning
            only claims when the stored status is NOT already 'running', so a
            thread that dies before writing a terminal row locks this user out
            of importing anything ever again, with nothing in the UI to clear
            it.

            Database.importHistoryBatch already writes a terminal row for every
            failure it can see - both batch paths end in one - so this only
            fires for what escapes them: _computeCoveredRange runs before the
            overwrite path's first try block, and the terminal writeProgress
            calls are themselves unguarded. Conditional on purpose: the batch's
            own message ("Imported 3/4 files (1 skipped)") says far more than
            this one, and must win whenever it exists."""
            progress = db.readProgress()
            if progress and progress.get("status") in _TERMINAL_IMPORT_STATUSES:
                return
            db.writeProgress("failed", 0, len(contents), _IMPORT_CRASH_MESSAGE, error=True)

        def _runImportBatch():
            try:
                db.importHistoryBatch(contents, overwriteRange=overwriteRange,
                                      unreadableFileCount=unreadableCount)
            except Exception:
                logger.exception("Import thread failed for user %s", username)
            finally:
                try:
                    _releaseImportSlot()
                except Exception:
                    # This runs *because* the database misbehaved, so it can
                    # misbehave too. Letting it escape a daemon thread turns a
                    # stuck import into an unraisable-exception warning and
                    # nothing more useful.
                    logger.exception("Could not release the import slot for user %s", username)

        thread = threading.Thread(target=_runImportBatch, daemon=True)
        thread.start()
        if expansion.emptyArchive:
            #< worth saying even though the import is under way: the batch only
            #  reports on what it RECEIVED, so an archive holding no history at
            #  all (the account-data export, uploaded alongside the right one)
            #  would otherwise be dropped with no trace anywhere
            return redirect(url_for("importPage", error="empty_archive"))
        return redirect(url_for("importPage"))
    app.add_url_rule("/import-history", "importHistory", importHistory, methods=["POST"])

    def importPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))
        return render_template(
            "import.html",
            importProgress=db.readProgress(),
            maxUploadMb=MAX_UPLOAD_MB,
            maxUncompressedImportMb=MAX_UNCOMPRESSED_IMPORT_MB,
            uploadTooLarge=request.args.get("error") == "upload_too_large",
            unreadableUpload=request.args.get("error") == "unreadable_upload",
            expandedTooLarge=request.args.get("error") == "expanded_too_large",
            emptyArchive=request.args.get("error") == "empty_archive",
            tooManyEntries=request.args.get("error") == "too_many_entries",
            maxImportArchiveEntries=MAX_IMPORT_ARCHIVE_ENTRIES,
            section="import",
        )
    app.add_url_rule("/import", "importPage", importPage, methods=["GET"])

    def exportHistory():
        """Stream the current user's full play history as a download.
        JSON is shaped like Spotify's own extended export (re-importable
        through /import-history); CSV is for spreadsheets."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        exportFormat = request.args.get("format", "json")
        if exportFormat not in EXPORT_FORMATS:
            exportFormat = "json"

        dateText = now(tz=db.tz).strftime("%Y-%m-%d")
        filename = f"spotify_stats_export_{username}_{dateText}.{exportFormat}"
        if exportFormat == "csv":
            #< bare mimetype - Response(mimetype=...) appends "; charset=utf-8"
            #  itself for text/*, so passing it here doubled the parameter
            #  (services/export.py:resolvePlaylistFormat has the same fix)
            generator, mimetype = generateCsvExport(db), "text/csv"
        else:
            generator, mimetype = generateJsonExport(db), "application/json"

        response = Response(stream_with_context(generator), mimetype=mimetype)
        #< Not an f-string: a username outside latin-1 (str.isalnum() lets
        #  them through at creation) made the header unsendable under waitress.
        response.headers["Content-Disposition"] = attachmentDisposition(filename)
        return response
    app.add_url_rule("/export-history", "exportHistory", exportHistory, methods=["GET"])

    #< the import page polls this while a file uploads, so it is JSON-only and
    #  never carries an ?ajax= marker: the api flavour, not the page one
    @makeRequiresUser(dashboard)(api=True)
    def importProgress(username, db):
        return jsonify(db.readProgress())
    app.add_url_rule("/import-progress", "importProgress", importProgress, methods=["GET"])

    def version_status():
        # Return the current and latest versions (latest is null if not newer)
        with dashboard._version_lock:
            latest = dashboard.latestVersion
        if latest and _is_version_newer(latest, dashboard.currentVersion):
            return jsonify({"current": dashboard.currentVersion, "latest": latest})
        else:
            return jsonify({"current": dashboard.currentVersion, "latest": None})
    app.add_url_rule("/version_status", "version_status", version_status, methods=["GET"])

    @makeRequiresUser(dashboard)(api=True)
    def listenerStatus(username, db):
        health = db.getListenerHealth()
        return jsonify(health)
    app.add_url_rule("/api/listener-status", "listenerStatus", listenerStatus, methods=["GET"])

    @makeRequiresUser(dashboard)(api=True)
    def nowPlayingStatus(username, db):
        """What the user - and the people they share with - are playing right
        now, from each listener's cached connect state (no Spotify calls).

        Polled by the dashboard every 15s. The friends list rides along on this
        same request rather than getting an endpoint of its own, so the strip
        costs no extra round trips; see SpotifyDashboardApp.getFriendsNowPlaying
        for the toggles and the per-friend opt-out it honours."""
        friends = dashboard.getFriendsNowPlaying(username)
        return jsonify({
            "nowPlaying": db.getNowPlaying(),
            #< every URL a chip renders is built here, not in the browser
            #  script - see _friendChip
            "friends": [_friendChip(friend) for friend in friends["friends"]],
            "friendsMoreCount": friends["moreCount"],
        })
    app.add_url_rule("/api/now-playing", "nowPlayingStatus", nowPlayingStatus, methods=["GET"])
