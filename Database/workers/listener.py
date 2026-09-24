# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod

#< the connect-state string-number coercion, shared with the poll/push
#  tracking that reads the same payloads (was a byte-identical staticmethod
#  copy here); recentlyPlayed imports nothing back from workers, so no cycle
from Database.Spotify.recentlyPlayed import _connectStateInt
#< a direct import, unlike _dbmod above: Database.utils takes part in no cycle
#  (see the note on its TRUTHY_ENV_VALUES re-export)
from Database.utils import flaskDebugEnabled
from Database.backfill_matching import (
    backfill_page_window,
    missing_backfill_items,
    BackfillPage,
)
from Database.db import WEB_API_BACKFILL_SOURCE


class ListenerMixin:
    """Spotify listener lifecycle: connect/reconnect, live play ingestion, web-API reconcile, now-playing, and overall stop coordination."""

    def process_backfill_page(self, items: list) -> None:
        """Filter and enqueue one complete Web API recently-played page."""
        if not items:
            return

        page = BackfillPage(items)
        evidence = []
        window = backfill_page_window(items)
        if window is not None:
            try:
                evidence = self.repo.getTrackPlayTimesInRange(self.user, *window, page=page)
            except Exception as e:
                # A failed lookup answered nothing. Reoffer conservatively and
                # let the insert guard settle already-recorded rows.
                _dbmod.logger.debug(
                    "Backfill dedup database lookup failed, conservatively reoffering plays: %s",
                    _dbmod.parseError(e),
                )

        missed_items = missing_backfill_items(items, evidence, page=page)
        if not missed_items:
            return

        if flaskDebugEnabled():
            _dbmod.logger.info(
                "Backfilling %d plays from Web API recently-played history for user %s",
                len(missed_items),
                self.user,
            )
        # Mark these as backfilled so the database can record the source.
        for missed_item in missed_items:
            missed_item["_source"] = WEB_API_BACKFILL_SOURCE
        # Page order can vary on retries. Stable oldest-first delivery follows
        # the exact-match reservations established across the complete page.
        missed_items.sort(key=lambda item: (_dbmod.timeToInt(item["played_at"]), item["track"]["id"]))
        self._addToDatabaseFromListener(missed_items, backfillPage=page)

    def _addToDatabaseFromListener(self, data, *, backfillPage: BackfillPage | None = None) -> None:
        """Record plays from the listener. Includes validation to detect cross-user
        data contamination (a bug that previously caused plays from one user to be
        recorded under another user's account)."""
        if not data:
            return
        #< flaskDebugEnabled(), not a bare os.environ.get: this site tested the
        #  STRING for truthiness, so FLASK_DEBUG=0 - which silences every other
        #  diagnostic - switched this one on, per ingest batch, per user, per cycle
        if flaskDebugEnabled():
            source = data[0].get("_source", "unknown") if data else "unknown"
            _dbmod.logger.debug("_addToDatabaseFromListener called for user=%s with %d items, source=%s",
                        self.user, len(data), source)
        had_errors = False
        for item in data:
            track = item.get("track")
            timestamp = item.get("played_at")
            msPlayed = item.get("ms_played", 0)
            source = item.get("_source", "listener")



            # Reject completely unparseable or corrupt timestamps
            numeric_ts = _dbmod.timeToInt(timestamp)
            if numeric_ts <= 0:
                _dbmod.logger.warning(
                    "Skipping track %s: timestamp %s is invalid or could not be parsed.",
                    track.get("id") if track else "unknown",
                    timestamp
                )
                had_errors = True
                continue

            # Sanity check: verify the timestamp makes sense (not in far future)
            current_time = _dbmod.time.time()
            if numeric_ts > current_time + self.LISTENER_FUTURE_PLAY_GRACE_SECONDS:
                _dbmod.logger.error(
                    "CONTAMINATION CHECK FAILED: Track %s has timestamp %s (%.0f seconds in future). "
                    "This suggests cross-user data contamination. Skipping this play.",
                    track.get("id") if track else "unknown",
                    timestamp,
                    numeric_ts - current_time
                )
                had_errors = True
                continue

            # Sanity check: validate play duration is reasonable for a track
            # (the recently-played feed sometimes reported insane values like 7062895ms for a
            # 171s track). The played_at timestamp is still trustworthy, so
            # record the play with the track's own length - what the Web API
            # backfill would store - instead of dropping it: the recently-played
            # feed doesn't always contain the track later, and a skip then loses
            # the play for good (2026-07-17, timorzipa).
            # `or 0` (not get's default): track can carry "duration_ms": None
            # (present but null), where dict.get returns None rather than
            # falling back to 0, and the comparison below would crash.
            track_duration = (track.get("duration_ms", 0) or 0) if track else 0
            if track_duration > 0 and msPlayed > track_duration * self.LISTENER_DURATION_CORRUPTION_FACTOR:
                _dbmod.logger.warning(
                    "Track %s: recorded duration %dms is %dx the track's actual duration (%dms). "
                    "Likely play-duration corruption - recording with the track's actual duration instead.",
                    track.get("id"),
                    msPlayed, msPlayed // max(track_duration, 1), track_duration
                )
                msPlayed = track_duration

            if track:
                # Per-item isolation: if the callback raised, the listener would
                # retry the whole batch forever and record nothing new until the
                # bad item aged out of the recently-played feed. Sub-threshold
                # events are no longer split off to a separate table here -
                # appendTrackData records every event into plays, with is_skip
                # materialized from the current skip threshold.
                try:
                    kwargs = {"backfillPage": backfillPage} if backfillPage is not None else {}
                    self.appendTrackData(timestamp, track, msPlayed, context=item.get("context", None),
                                         source=source, **kwargs)
                except Exception as e:
                    _dbmod.logger.error("Error adding track %s from listener: %s", track.get("id"), _dbmod.parseError(e))
                    had_errors = True
        # Mark successful poll (only if no errors occurred during processing)
        with self._health_lock:
            self.listener_last_poll_time = _dbmod.time.monotonic()
            if had_errors:
                self.listener_error_count += 1
                self.listener_last_error = "One or more tracks failed to add from listener"
                if self.listener_error_count > self.LISTENER_DEGRADED_ERROR_THRESHOLD:
                    self.listener_health = "DEGRADED"
                    _dbmod.logger.warning("Listener error count exceeded threshold, marking as DEGRADED")
            else:
                self.listener_error_count = 0
                self.listener_last_error = None
                if self.listener_health != "HEALTHY":
                    self.listener_health = "HEALTHY"
                    _dbmod.logger.info("Listener recovered to HEALTHY state")

    # _fetchTrackFromListener/_ensureTrackMetadata used to live here. Their only
    # caller was _paginateEntries, hydrating a page of play history: on a play
    # whose track row was missing they fetched it live from Spotify and wrote it,
    # from the request thread, during a GET. plays.track_id is an enforced
    # foreign key, so a play cannot exist without its track - the only way to
    # reach that code was dangling-row corruption, which is now repaired by
    # migrate1_43_0 and reported by the startup probe rather than papered over
    # one render at a time. Nothing else fetched a track by id; the listener
    # writes its own catalog rows as it records plays.

    def _stopRequested(self) -> bool:
        """True once this instance is being stopped or the whole app is
        shutting down - reconnect/start paths must refuse from then on."""
        return self._stopping or self.shutdown_event.is_set()

    def noteListenerSuperseded(self) -> None:
        """Record that a newer listener now exists, and wake anything waiting.

        Called from startListener's swap. NOT a stop: signalStop sets _stopping,
        which is never cleared, so a user re-logging in with fresh cookies
        cannot use it - it would refuse them a listener for the rest of the
        process's life. This says only "whatever you were reconnecting toward
        has been built already"."""
        self._listenerGeneration += 1
        self._stopEvent.set()

    def _reconnectSuperseded(self, generation) -> bool:
        """Whether a listener has been installed since `generation` was taken.
        None means the caller is not part of a reconnect run and does not care."""
        return generation is not None and generation != self._listenerGeneration

    def _waitForStop(self, timeout: float, generation=None) -> bool:
        """Sleep up to `timeout` seconds, returning True as soon as this
        instance should stop - or, with `generation`, as soon as the reconnect
        it belongs to has been superseded by a listener someone else built.

        Waits on _stopEvent rather than shutdown_event, which is the app-wide
        exit signal SHARED by every user: an app shutdown sets that AND calls
        signalStop() on each user (app.py), so both paths still interrupt
        promptly - but a per-instance stop only ever touches this one, and
        waiting on the shared event meant it could not interrupt anything.

        The check BEFORE the wait is load-bearing, not a shortcut: waiting on
        _stopEvent alone would sleep out the full timeout when shutdown_event is
        already set and signalStop() has not reached this instance - which the
        old `shutdown_event.wait(...)` returned from instantly. Dropping it
        turns a prompt abort into a five-minute one.

        The re-check afterwards covers shutdown_event arriving DURING the wait
        without a signalStop() alongside it. In practice app shutdown always
        sends both (app.py sets the event, then signals every user), so
        _stopEvent ends the wait there too."""
        if self._stopRequested() or self._reconnectSuperseded(generation):
            return True
        # Cleared for THIS wait, then the conditions re-read. _stopEvent is only
        # the nudge that ends a wait early - _stopping and the generation are the
        # truth - so a set left behind by an earlier wait would otherwise make
        # this one return instantly, turning the backoff into a spin that burns
        # every retry against Spotify with no delay between them. Re-reading
        # after the clear is what makes it safe: a signal that arrived before it
        # is caught here rather than lost.
        self._stopEvent.clear()
        if self._stopRequested() or self._reconnectSuperseded(generation):
            return True
        self._stopEvent.wait(timeout)
        return self._stopRequested() or self._reconnectSuperseded(generation)

    def _makeOnStaleCallback(self) -> callable:
        """Create an onStale callback that retries with exponential backoff.
        Called when the listener detects a stale feed or auth error and needs
        to reconnect with fresh cookies/session. `reason` is the listener's
        diagnosis (spotifyListener's STALE_REASON_*), passed through to the
        session ledger startListener keeps for /admin."""
        def onStaleWithBackoff(reason=None):
            #< captured before the first attempt: if anyone else installs a
            #  listener while this loop is parked, the session it is retrying
            #  toward already exists and reconnecting again would replace it
            generation = self._listenerGeneration
            with self._health_lock:
                self.listener_health = "DEGRADED"
                self.listener_error_count += 1

            for attempt in range(self.RECONNECT_MAX_RETRIES):
                if attempt > 0:
                    backoff_delay = min(
                        self.RECONNECT_INITIAL_DELAY * (2 ** attempt),
                        self.RECONNECT_MAX_DELAY
                    )
                    _dbmod.logger.warning(
                        "Reconnection attempt %d/%d, waiting %ds before retry",
                        attempt, self.RECONNECT_MAX_RETRIES, backoff_delay
                    )
                    # Interruptible by EITHER stop: an app shutdown or this one
                    # user's own, instead of sleeping out up to
                    # RECONNECT_MAX_DELAY and reconnecting into a process - or a
                    # session - that is already going away. See _waitForStop for
                    # why it is not shutdown_event.
                    if self._waitForStop(backoff_delay, generation):
                        _dbmod.logger.info(
                            "Reconnection abandoned for user %s: %s", self.user,
                            "superseded by a newer listener"
                            if self._reconnectSuperseded(generation) else "stopping")
                        return

                if self._stopRequested() or self._reconnectSuperseded(generation):
                    _dbmod.logger.info("Reconnection abandoned for user %s: stop requested", self.user)
                    return

                try:
                    # DEBUG, not INFO: a reconnect describes the system working
                    # as intended, not a fault. It used to fire ~2 times an hour
                    # per user because an idle feed counted as a dead one; the
                    # stale check now needs evidence of unrecorded playback
                    # (see _staleFeedBrokenReason), but the level still fits.
                    _dbmod.logger.debug("Attempting to reconnect (attempt %d/%d)", attempt + 1, self.RECONNECT_MAX_RETRIES)
                    if self.startListener(email=self.email, rebuildReason=reason) is False:
                        _dbmod.logger.info("Reconnection abandoned for user %s: stop requested", self.user)
                        return
                    if attempt == 0:
                        _dbmod.logger.debug("Reconnection succeeded on attempt 1")
                    else:
                        # Anything that didn't work first time is worth reading
                        # at the default level - a session degrading toward
                        # failure shows up here before it reaches the ERROR.
                        _dbmod.logger.info("Reconnection succeeded on attempt %d", attempt + 1)
                    return
                except Exception as e:
                    _dbmod.logger.warning("Reconnection attempt %d failed: %s", attempt + 1, _dbmod.parseError(e))
                    with self._health_lock:
                        self.listener_last_error = _dbmod.parseError(e)
                    if attempt == self.RECONNECT_MAX_RETRIES - 1:
                        _dbmod.logger.error(
                            "Reconnection failed after %d attempts, tracking paused for this user",
                            self.RECONNECT_MAX_RETRIES
                        )
                        with self._health_lock:
                            self.listener_health = "DEAD"

        return onStaleWithBackoff

    def startListener(self, cookiesFile=None, email=None, rebuildReason=None) -> bool:
        """(Re)build and start this user's listener. Returns False when the
        start was refused or abandoned because stop/shutdown was requested;
        True otherwise. The whole body holds _listener_lock: concurrent
        reconnects (health check vs onStale) are serialized, and stop() can
        rely on the swap below never interleaving with its own teardown.

        `rebuildReason` is the listener's own diagnosis of why a REbuild was
        needed (spotifyListener's STALE_REASON_*), recorded in the session
        ledger below; callers without one (boot, a cookies update) leave it
        None and the ledger shows the rebuild as unattributed."""
        if self._stopRequested():
            _dbmod.logger.info("Not starting listener for user %s: stop requested", self.user)
            return False
        with self._listener_lock:
            if self._stopRequested():
                _dbmod.logger.info("Not starting listener for user %s: stop requested", self.user)
                return False
            if cookiesFile:
                self.cookiesFile = cookiesFile
            if email:
                if self.email and email != self.email:
                    _dbmod.logger.warning(
                        "Email mismatch in startListener for user %s: was %s, now %s. "
                        "This could indicate confused session state.",
                        self.user, self.email, email
                    )
                self.email = email
            isReconnect = self.listener is not None
            if isReconnect:
                # Part of the same reconnect cycle as the line above (1,354 of
                # these in the 11 days when an idle feed still forced a rebuild
                # every 30 minutes) - the genuine start below is the one worth
                # an INFO.
                _dbmod.logger.debug("Stopping existing listener for user %s before re-starting", self.user)
                try:
                    self.listener.stop()
                except Exception as e:
                    _dbmod.logger.error("Failed to stop existing listener for user %s: %s", self.user, _dbmod.parseError(e))
            newListener = self._withCookiesFile(lambda cf: _dbmod.Listener(
                cf, email=self.email, user=self.user,
                get_credentials=self.getUserSpotifyCredentials,
                get_backfill_enabled=self.repo.isSpotifyApiBackfillEnabled,
                on_scope_status_change=self.setSpotifyNeedsReauth,
                get_recorded_track_ids=self.getRecentlyRecordedTrackIds,
                process_backfill_page=self.process_backfill_page))
            if self._stopRequested():
                # stop() gave up waiting on this lock while the (slow,
                # uninterruptible) Listener login above was in flight - tear
                # the fresh listener down instead of leaving an orphan running
                # that nothing can reach (the 2026-07-17 shutdown hang).
                _dbmod.logger.info("Stop requested while listener for user %s was connecting - discarding it", self.user)
                try:
                    newListener.stop()
                except Exception as e:
                    _dbmod.logger.error("Failed to stop just-built listener for user %s: %s", self.user, _dbmod.parseError(e))
                return False
            self.listener = newListener
            #< a listener now exists: any reconnect backoff still parked from an
            #  earlier failure is retrying toward a session that is already here
            self.noteListenerSuperseded()
            with self._health_lock:
                # The session ledger: even a build that turns out contaminated
                # or login-failed constructed a session, so it counts.
                self.listener_session_builds += 1
                if isReconnect:
                    self.listener_last_rebuild_time = _dbmod.time.time()
                    self.listener_last_rebuild_reason = rebuildReason
            if self.listener.contaminationDetected:
                # The cookies authenticate as a different Spotify account (see
                # Listener.__init__'s contamination check). The listener itself
                # refuses to record; reflect that as DEAD so the UI shows the user
                # something actionable instead of a listener that looks healthy
                # while recording nothing.
                with self._health_lock:
                    self.listener_health = "DEAD"
                    self.listener_last_error = (
                        "Stored cookies belong to a different Spotify account - "
                        "re-login with matching cookies to resume tracking"
                    )
                return True
            if self.listener.loginFailed:
                # The stored cookies didn't authenticate at all (see
                # Listener.__init__'s isLoggedIn guard) - same DEAD-with-reason
                # treatment as contaminationDetected, instead of leaving this
                # user's Database uncached (get_user_db's except-and-rollback,
                # triggered by the AttributeError this used to raise) with
                # nothing in the UI explaining why.
                with self._health_lock:
                    self.listener_health = "DEAD"
                    self.listener_last_error = (
                        "Spotify login failed - stored cookies may be invalid or expired; "
                        "re-login to resume tracking"
                    )
                return True
            with self._health_lock:
                self.listener_health = "HEALTHY"
                self.listener_error_count = 0
            if not isReconnect:
                # The one lifecycle line the demotions above must not cost us:
                # a listener coming up for the first time this process is a real
                # event, unlike the reconnect churn that surrounds it.
                _dbmod.logger.info("Listener started for user %s", self.user)
            self.listener.startListener_thread(
                callback=self._addToDatabaseFromListener,
                onStale=self._makeOnStaleCallback(),
                onWebApiSnapshot=self._reconcileWithWebApiHistory,
            )
        return True

    def getAutoImporterWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the user's autoImport drop-folder watchdog."""
        auto_imp = getattr(self, "autoImporter", None)
        wd = getattr(auto_imp, "wd", None) if auto_imp is not None else None
        thread = getattr(wd, "thread", None) if wd is not None else None
        running = thread is not None and thread.is_alive() and getattr(wd, "run", False)
        return {
            "configured": True,
            "running": running,
        }


    @staticmethod
    def _groupPlaysByIdentity(plays: list[dict]) -> dict[str, list[dict]]:
        """The window's plays bucketed by RECORDING, not by release id.

        Grouping on track_id alone was blind to the commonest duplicate this
        pass exists to remove: Spotify names the same recording differently in
        connect state and in the Web API, so the listener writes one id and the
        backfill writes another, and two rows for one listen looked like two
        unrelated tracks. The 2026-08-17 sweep measured it at 9.6% of the
        backfill rows it deleted - and a sweep only clears what has already
        accumulated, so the live pass has to see it too or they come back.

        Two IDENTITY proofs, both transitive, so they are unioned rather than
        tested pairwise: a shared merge group (canonical_id), and a shared
        ISRC. tools/sweep_backfill_duplicates.py carries a third, name + duration +
        primary artist - deliberately NOT honoured here. That one is a
        heuristic, and this path deletes from live history unattended; the
        sweep prints a dry run for a person to read first, which is where a
        heuristic belongs.

        A row with neither column (an older caller's row shape, or a play whose
        track row has gone missing) groups under its own id, exactly as before.
        An EMPTY isrc is not a shared identity - folding every unstamped track
        in the window into one bucket would delete across different songs."""
        parent: dict[str, str] = {}

        def find(key: str) -> str:
            #< no path compression: a window is one Web API page, so these
            #  chains are a handful of links long and the loop is cheaper than
            #  the bookkeeping that would shorten it
            while parent.setdefault(key, key) != key:
                key = parent[key]
            return key

        def union(left: str, right: str) -> None:
            rootLeft, rootRight = find(left), find(right)
            if rootLeft != rootRight:
                parent[rootRight] = rootLeft

        def mergeGroupOf(play: dict) -> str:
            return play.get("canonicalId") or play["id"]

        firstGroupForIsrc: dict[str, str] = {}
        for play in plays:
            group = mergeGroupOf(play)
            find(group)   #< seed it, so a track with no sibling still gets a bucket
            isrc = (play.get("isrc") or "").strip()
            if isrc:
                union(firstGroupForIsrc.setdefault(isrc, group), group)

        grouped: dict[str, list[dict]] = {}
        for play in plays:
            grouped.setdefault(find(mergeGroupOf(play)), []).append(play)
        return grouped

    def _reconcileWithWebApiHistory(self, apiItems: list[dict]) -> None:
        """Repair metadata and remove only API copies assigned to a primary play.

        One primary row can explain one distinct API timestamp. Reserve exact
        matches over the complete page before considering clock proximity, so
        the start of one listen cannot also erase a later repeat. Only decided
        merge groups and ISRCs identify aliases here; the stricter import-time
        title/artist/duration heuristic still cannot authorize deletion.

        A listener's observed end once deleted pause-stretched API copies, but
        an incomplete page cannot distinguish that copy from a new repeat at
        the same timestamp. Keep that ambiguous row rather than erase history.
        All-API groups and primary rows remain untouched, and absence from the
        finite API page cannot authorize deletion. No page state persists
        between callbacks or enters Listener.
        """
        if not apiItems:
            return

        # Confirmed plays never reach appendTrackData's duplicate guard. This
        # full snapshot still supplies their metadata, without changing any
        # original listening facts. A repair failure must not block cleanup.
        try:
            self._repairFallbackTrackMetadata([item.get("track") for item in apiItems], source="history")
        except Exception as error:
            _dbmod.logger.warning("Web API metadata repair failed for user %s: %s",
                                  self.user, _dbmod.parseError(error))

        apiTimes = [
            _dbmod.timeToInt(item["played_at"])
            for item in apiItems
            #< (item.get("track") or {}), not .get("track", {}): Spotify sends
            #  the key present-and-NULL, where the default never applies and
            #  None.get raises. The AttributeError escaped into
            #  _checkWebApiBackfill's catch-all AFTER its inserts had landed, so
            #  the duplicate cleanup those inserts need never ran. Same guard the
            #  listener's own snapshot builder uses.
            if (item.get("track") or {}).get("id") and item.get("played_at")
        ]
        if not apiTimes:
            _dbmod.logger.debug("Reconciliation skipped: no API items with both track id and played_at")
            return

        # Padded by the widest tolerance a PAIR can span, because the window is
        # built from the API's stamps but the row that proves a copy is a copy
        # was written by a different recorder: for the oldest and newest item in
        # a page its sibling can land just outside, and a row the query never
        # returns cannot join a cluster - the pair then reads as one lonely play
        # and the duplicate survives. Widening the candidate set only; deletion
        # still needs both proofs below, so nothing unproven becomes deletable.
        windowPadding = max(self.DUPLICATE_RECORDING_TOLERANCE_SECONDS,
                            self.BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS)
        windowStart = min(apiTimes) - windowPadding
        windowEnd = max(apiTimes) + windowPadding

        try:
            # Read the evidence and delete under one reservation: a concurrent
            # writer cannot change a row's identity/source between these steps.
            conn = self._beginMetadataWrite()
            with conn:
                localPlays = self.repo.getPlaysWithSourceInRange(self.user, windowStart, windowEnd)
                if not localPlays:
                    return

                apiPage = BackfillPage(apiItems)
                aliases = self.repo._sameRecordingTrackIds(
                    apiPage.trackIds, {play["id"] for play in localPlays},
                    pendingTracks=apiPage.pendingTracks,
                    includeRecordingKey=False)
                toDelete = []
                for group in self._groupPlaysByIdentity(localPlays).values():
                    primary = [play for play in group if not
                               (play.get("createdReason") or "").startswith(self.WEB_API_BACKFILL_SOURCE)]
                    backfill = [play for play in group if
                                (play.get("createdReason") or "").startswith(self.WEB_API_BACKFILL_SOURCE)]
                    if not primary or not backfill:
                        continue
                    trackIds = {play["id"] for play in group}
                    # Include page events with no API row: the prefilter may
                    # already have confirmed them against a primary source.
                    groupItems = [item for item in apiItems if
                                  (item.get("track") or {}).get("id") in trackIds
                                  or trackIds.intersection(aliases.get((item.get("track") or {}).get("id"), ()))]
                    currentTimes = {_dbmod.timeToInt(item["played_at"])
                                    for item in groupItems if item.get("played_at")}
                    groupItems += [{"track": {"id": play["id"]}, "played_at": play["playedAt"]}
                                   for play in backfill]
                    page = BackfillPage(groupItems)
                    groupAliases = trackIds | {item["track"]["id"] for item in groupItems}
                    evidence = [
                        {"rowId": play["rowId"],
                         "trackId": play["id"], "aliases": groupAliases,
                         "playedAt": play["playedAt"], "listenerCreatedAt": play.get("createdAt"),
                         "createdReason": play.get("createdReason"), "isSkip": False}
                        for play in primary
                    ]
                    # Assign every page event, including those confirmed by
                    # the prefilter and therefore absent as API rows. Otherwise
                    # cleanup could reuse their primary row for a later repeat.
                    events = sorted({(_dbmod.timeToInt(item["played_at"]), item["track"]["id"])
                                     for item in groupItems if item.get("played_at")})
                    matchedTimes = set()
                    for timestamp, trackId in events:
                        match = page.match(
                            trackId, timestamp, evidence,
                            toleranceSeconds=self.DUPLICATE_RECORDING_TOLERANCE_SECONDS,
                            startToleranceSeconds=self.DUPLICATE_RECORDING_TOLERANCE_SECONDS,
                            listenerEndArms=False)   #< deleting stays start-only
                        if match is not None and page.claim(match, timestamp):
                            matchedTimes.add(timestamp)
                    # Stored API events reserve primary rows too, but only
                    # timestamps corroborated by this page authorize deletion.
                    toDelete.extend(play for play in backfill
                                    if play["playedAt"] in matchedTimes & currentTimes)

                deletedCount = 0
                deletedYears = set()
                for play in toDelete:
                    if self.repo.deletePlay(self.user, play["id"], play["playedAt"]):
                        deletedCount += 1
                        deletedYears.add(_dbmod.convertToDatetime(play["playedAt"], tz=self.tz).year)
                if deletedCount:
                    # Removing an early listen can move discoveries in later
                    # years. Invalidate with the deletes so an in-flight
                    # calculation cannot save its pre-cleanup snapshot. Only this
                    # user's plays changed, so only this user's stamp moves: the
                    # instance-wide one discarded every other user's in-flight
                    # Wrapped on each cleanup.
                    self.repo._bumpUserWrappedGeneration(conn, self.user)
                    self.repo._deleteUserWrappedFromYear(conn, self.user, min(deletedYears))
                    self.repo.commit()
                    _dbmod.logger.info(
                        "Web API reconciliation: removed %d duplicate play(s) for user %s",
                        deletedCount, self.user)
                # The context also closes an empty/delete-no-op transaction.
        except Exception as e:
            self.repo.rollbackQuietly()
            _dbmod.logger.warning(
                "Web API reconciliation aborted for user %s; staged deletes rolled back: %s",
                self.user, _dbmod.parseError(e))

    def getNowPlaying(self, includePlayedFlags: bool = True) -> dict | None:
        """What this user is playing right now, read from the listener's
        cached connect player_state (zero extra network calls - see
        Listener.getConnectPlayerState). None when the listener is no longer
        running, nothing is playing, the state looks stale, or the track
        can't be identified. Track metadata
        comes from the catalog; a first-ever listen isn't in the catalog yet
        (metadata is only fetched when a play completes), so the connect
        state's own metadata is the fallback.

        `includePlayedFlags=False` answers `trackPlayed` and each artist's
        `played` as False without asking, for a caller that has no use for them.
        The two lookups behind those flags are the only DATABASE work here - the
        rest is one catalog read - and the friends strip is exactly such a
        caller: it runs this for every counterpart on a 15-second poll, drops
        the flags (they describe someone else's history, see
        getFriendsNowPlaying) and answers the same question against the viewer
        instead. The keys stay in the payload either way, so nothing downstream
        has to learn about the distinction."""
        if self.listener is None:
            return None
        # A listener that is no longer recording has nothing to show, whatever
        # its cached state says. The staleness cut below only reaches a PLAYING
        # snapshot (a paused one has no duration to run out), and stop() never
        # clears manager._state - so a dead listener (reconnects exhausted) or
        # one stopped for a rebuild kept showing "Paused: <last track>" with a
        # frozen position, like a live paused session, until the login check
        # rebuilt it. `run` is the flag signalStop already flips; the cached
        # state itself is deliberately left alone (it feeds the position math).
        if self.listener_health == "DEAD" or not getattr(self.listener, "run", True):
            return None
        state = self.listener.getConnectPlayerState()
        if not state or not state.get("is_playing"):
            return None
        stateTrack = state.get("track") or {}
        trackUri = stateTrack.get("uri") or ""
        if not trackUri.startswith("spotify:track:"):
            return None   #< ads/episodes aren't tracks we can show
        trackId = trackUri.rsplit(":", 1)[-1]
        isPaused = bool(state.get("is_paused"))

        timestampMs = _connectStateInt(state.get("timestamp"))
        positionMs = _connectStateInt(state.get("position_as_of_timestamp"))
        durationMs = _connectStateInt(state.get("duration"))
        # Standard connect-state position math: the state only updates on
        # play/pause/seek/track change, so the live position is the snapshot
        # position plus time elapsed since the snapshot (unless paused).
        elapsedMs = max(0, int(_dbmod.time.time() * 1000) - timestampMs) if timestampMs else 0
        currentPositionMs = positionMs if isPaused else positionMs + elapsedMs
        if not isPaused and durationMs and timestampMs and currentPositionMs > durationMs + self.NOW_PLAYING_STALE_GRACE_MS:
            return None
        if durationMs:
            currentPositionMs = min(currentPositionMs, durationMs)

        track = self.repo.getTrack(trackId)
        if track:
            name = track.get("name")
            artistsText = ", ".join(a.get("name", "") for a in track.get("artists", []))
            imageId = track.get("imageId")
        else:
            stateMeta = stateTrack.get("metadata") or {}
            # spotapi may have already hydrated metadata into a Metadata
            # dataclass (which is truthy but has no .get()), so handle both.
            if isinstance(stateMeta, dict):
                name = stateMeta.get("title")
                artistsText = stateMeta.get("artist_name") or ""
                imageId = _dbmod._imageIdFromConnectMeta(stateMeta)
                imageUrl = _dbmod._imageUrlFromConnectMeta(stateMeta)
            else:
                _dbmod.logger.warning(
                    "getNowPlaying: unexpected metadata type %s for track %s "
                    "(stateTrack type=%s, value=%r); falling back to getattr",
                    type(stateMeta).__name__, trackId,
                    type(stateTrack).__name__, stateMeta,
                )
                name = getattr(stateMeta, "title", None)
                artistsText = getattr(stateMeta, "artist_name", None) or ""
                imageId = _dbmod._imageIdFromConnectMeta(stateMeta)
                imageUrl = _dbmod._imageUrlFromConnectMeta(stateMeta)
            # Kick off a background download so the cover is ready on the next
            # poll (or shortly after). saveTrackImg is fire-and-forget and
            # already deduped via tryClaimImageDownload.
            if imageId and imageUrl:
                self.saveTrackImg(imageUrl, imageId)

        if not name:
            return None   #< nothing presentable to show

        # Whether the current user has actually played this track / these
        # artists before decides whether Now Playing links to our own detail
        # pages or falls back to Spotify: a track playing for the first time has
        # no completed play logged yet, so /song/<id> would have nothing to show
        # (this is why it used to always link out to Spotify). artists carry ids
        # only in the catalog branch; a first-listen fallback has none, so the
        # UI keeps showing artistsText as plain text there.
        trackPlayed = (bool(self.repo.getPlayedTrackIds(self.user, [trackId]))
                       if includePlayedFlags else False)
        artists = []
        if track:
            artistList = track.get("artists") or []
            playedArtistIds = set()
            if includePlayedFlags:
                artistIds = [a.get("id") for a in artistList if a.get("id")]
                playedArtistIds = self.repo.getPlayedArtistIds(self.user, artistIds) if artistIds else set()
            artists = [
                {"id": a.get("id"), "name": a.get("name", ""), "played": a.get("id") in playedArtistIds}
                for a in artistList if a.get("id")
            ]

        return {
            "trackId": trackId,
            "name": name,
            "artistsText": artistsText,
            "artists": artists,
            "trackPlayed": trackPlayed,
            "imageId": imageId,
            "isPaused": isPaused,
            "positionMs": currentPositionMs,
            "durationMs": durationMs,
        }

    def startAutoImporter(self):
        # Gated like every other start path here (startListener,
        # _ensureAllUsersLogin): a signalled instance never starts a thread
        # again. Without it, an auto-import watchdog begun after the shutdown
        # snapshot was taken is a thread nothing will join - it outlives the
        # phase that was supposed to stop it, and the process waits out its
        # grace period for a worker that started after the exit began.
        if self._stopRequested():
            _dbmod.logger.info("Auto-importer not started for user %s: stop requested", self.user)
            return
        self.autoImporter.start()

    def isListenerLoggedIn(self):
        if self.listener is None:
            return False
        return self.listener.isLoggedIn()

    def getListenerHealth(self) -> dict:
        """Get current listener health status for displaying to user."""
        with self._health_lock:
            seconds_since_last_poll = None
            if self.listener_last_poll_time is not None:
                seconds_since_last_poll = _dbmod.time.monotonic() - self.listener_last_poll_time
            return {
                "status": self.listener_health,
                "error_count": self.listener_error_count,
                "last_error": self.listener_last_error,
                "seconds_since_last_poll": seconds_since_last_poll,
                "session_builds": self.listener_session_builds,
                "last_rebuild_time": self.listener_last_rebuild_time,
                "last_rebuild_reason": self.listener_last_rebuild_reason,
            }

    # Every per-user background worker's stop event, in one place: shutdown
    # phase 1 sets all of them. The album-biography worker was once missing
    # from the literal tuple this replaced, so it kept running full Last.fm
    # batches while the other users' threads were being joined.
    WORKER_STOP_EVENT_NAMES = (
        "backfiller_stop_event",
        "wrapped_stop_event",
        "lastfm_stop_event",
        "lastfm_biography_stop_event",
        "lastfm_album_biography_stop_event",
    )

    def signalStop(self) -> None:
        """Phase 1 of shutdown: flip every stop flag/event for this user
        WITHOUT joining or closing anything. shutdown() calls this for every
        user before any (potentially slow) join runs, closing the window where
        one user's still-running listener fires a stale-feed reconnect while
        another user's threads are being joined (the 2026-07-17 hang).
        Permanent: a signaled instance never starts a listener again."""
        self._stopping = True
        #< the waitable twin of the flag above: anything sleeping (the reconnect
        #  backoff) wakes on this rather than polling _stopping after its wait
        self._stopEvent.set()
        listener = self.listener
        if listener is not None:
            try:
                listener.signalStop()
            except Exception as e:
                _dbmod.logger.error("Error signaling listener stop for %s: %s", self.user, _dbmod.parseError(e))
        #< two-step like getAutoImporterWorkerStatus: on a partially built
        #  instance even the autoImporter attribute may be missing, and a raise
        #  here would skip setting the worker stop events below
        auto_imp = getattr(self, "autoImporter", None)
        wd = getattr(auto_imp, "wd", None) if auto_imp is not None else None
        if wd is not None:
            wd.signalStop()
        for eventName in self.WORKER_STOP_EVENT_NAMES:
            event = getattr(self, eventName, None)
            if event is not None:
                event.set()

    def stop(self):
        # Signal first even when called directly (idempotent when shutdown()
        # already ran signalStop): every thread starts winding down before the
        # joins below block.
        self.signalStop()
        acquired = self._listener_lock.acquire(timeout=self.LISTENER_STOP_LOCK_TIMEOUT_SECONDS)
        # On timeout an in-flight startListener holds the lock (a live Spotify
        # login) - proceed anyway: it re-checks _stopping after connecting and
        # discards its own listener, and stopping the current listener without
        # the lock is safe (Listener.stop() is idempotent).
        try:
            if self.listener is not None:
                self.listener.stop()
        finally:
            if acquired:
                self._listener_lock.release()
        # Same two-step guard as signalStop/getAutoImporterWorkerStatus: a
        # raise here would skip the five worker stops below, leaving their
        # threads signaled but never joined.
        auto_imp = getattr(self, "autoImporter", None)
        wd = getattr(auto_imp, "wd", None) if auto_imp is not None else None
        if wd is not None:
            wd.stop()
        self.stopMetadataBackfiller()
        self.stopWrappedCalculationsWorker()
        self.stopLastfmGenreBackfiller()
        self.stopLastfmBiographyBackfiller()
        self.stopLastfmAlbumBiographyBackfiller()
