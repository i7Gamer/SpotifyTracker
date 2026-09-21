# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Imported for the type hints below. Everything used at RUNTIME still goes
# through _dbmod, so the suite's patch("Database.database.X") targets are
# unaffected - these names were simply never defined here, leaving the hints
# unresolvable for tooling and for typing.get_type_hints(). All leaf modules
# (stdlib, or Database/lastfm.py, which imports nothing of ours), so a real
# import costs nothing and cannot cycle.
import threading
from Database.lastfm import LastfmClient

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod
from Database.workers.periodic import WORKER_STOP_JOIN_TIMEOUT_SECONDS


# Kept as an alias: the bound now lives with the shared lifecycle, but tests
# and callers still import it from here.
LASTFM_WORKER_STOP_JOIN_TIMEOUT_SECONDS = WORKER_STOP_JOIN_TIMEOUT_SECONDS


class LastfmBackfillMixin:
    """The three Last.fm backfillers - genre tags, artist biographies, album biographies - and their shared claim/lookup/inheritance helpers."""

    def _startLastfmWorker(self, threadAttr: str, eventAttr: str, loop, threadName: str,
                            logPrefix: str) -> None:
        """Shared start for the three Last.fm workers.

        Adds one thing to the standard lifecycle (PeriodicWorkerMixin, which
        owns the locking and the fresh-event-per-run rules): a keyless user
        gets no idle thread, so the API-key lookup vetoes the start."""
        def hasApiKey() -> bool:
            import sqlite3
            try:
                #< keyless users get no idle thread
                return bool(self.repo.getUserLastfmApiKey(self.user))
            except sqlite3.OperationalError as e:
                # A Database constructed against a pre-1.19 file outside the
                # app's migration path (standalone script/REPL) has no
                # users.lastfm_api_key column yet - skip the worker instead of
                # crashing __init__; it starts once the migration has run.
                _dbmod.logger.warning("[%s-%s] Not starting - schema not migrated yet: %s",
                                       logPrefix, self.user, e)
                return False

        self._startPeriodicWorker(threadAttr, eventAttr, loop, threadName,
                                   precondition=hasApiKey, logPrefix=logPrefix)

    def _stopLastfmWorker(self, threadAttr: str, eventAttr: str) -> None:
        """Shared stop for the three Last.fm workers - see PeriodicWorkerMixin."""
        self._stopPeriodicWorker(threadAttr, eventAttr)

    def _lastfmWorkerStatus(self, threadAttr: str, telemetryKey: str) -> dict:
        """All three read the same key, so "configured" means the same thing."""
        return self._workerStatus(threadAttr, telemetryKey,
                                   configured=bool(self.repo.getUserLastfmApiKey(self.user)))

    def getLastfmWorkerStatus(self) -> dict:
        return self._lastfmWorkerStatus("lastfm_thread", "lastfm_genre")

    def getLastfmBiographyWorkerStatus(self) -> dict:
        """The artist biography backfiller - used by the Overview "Biography
        Backfill Progress" widget's Artist row."""
        return self._lastfmWorkerStatus("lastfm_biography_thread", "lastfm_artist_bio")

    def getLastfmAlbumBiographyWorkerStatus(self) -> dict:
        """The album biography backfiller - the Overview widget's Album row."""
        return self._lastfmWorkerStatus("lastfm_album_biography_thread", "lastfm_album_bio")

    def startLastfmGenreBackfiller(self) -> None:
        """Start the background thread that backfills genres from Last.fm.
        No-op without a stored API key - keyless users get no idle thread,
        and the profile page's key save calls this again once a key exists."""
        self._startLastfmWorker("lastfm_thread", "lastfm_stop_event",
                                 self._lastfmGenreBackfillLoop,
                                 f"lastfm-genres-{self.user}", "LastfmWorker")

    def stopLastfmGenreBackfiller(self) -> None:
        """Signal and wait for the background genre backfiller to stop."""
        self._stopLastfmWorker("lastfm_thread", "lastfm_stop_event")

    def _runLastfmLoop(self, *, stop_event: threading.Event | None, eventAttr: str,
                       minStartDelay: int, maxStartDelay: int, idleWaitSeconds: int,
                       enabled, runWork, logPrefix: str, errorLabel: str,
                       telemetryKey: str) -> None:
        """Shared loop policy for the three Last.fm workers.

        The workers keep their own entrypoints, timings, telemetry names and
        entity processors; only the repeated loop shell lives here.
        """
        import random
        if stop_event is None:
            stop_event = getattr(self, eventAttr)
        try:
            startup_delay = random.randint(minStartDelay, maxStartDelay)
            _dbmod.logger.info("[%s-%s] Starting with initial delay of %d seconds",
                               logPrefix, self.user, startup_delay)
            if stop_event.wait(startup_delay):
                _dbmod.logger.info("[%s-%s] Stopped during startup delay", logPrefix, self.user)
                return

            while not stop_event.is_set():
                try:
                    if not enabled():
                        if stop_event.wait(idleWaitSeconds):
                            break
                        continue

                    apiKey = self.repo.getUserLastfmApiKey(self.user)
                    if not apiKey:
                        _dbmod.logger.info("[%s-%s] No API key stored anymore - exiting",
                                           logPrefix, self.user)
                        return
                    client = _dbmod.LastfmClient(apiKey)

                    processedAny = runWork(client, self.user, stop_event=stop_event)
                    if not processedAny and not stop_event.is_set():
                        processedAny = runWork(client, None, stop_event=stop_event)
                    if not processedAny:
                        if stop_event.wait(idleWaitSeconds):
                            break
                except _dbmod._LastfmInvalidKeyError:
                    self._recordWorkerCycle(
                        telemetryKey, success=False, error="Invalid Last.fm API key")
                    _dbmod.logger.warning(
                        "[%s-%s] Last.fm rejected the API key (invalid/suspended) - "
                        "idling; fix the key on the profile page", logPrefix, self.user)
                    if stop_event.wait(idleWaitSeconds):
                        break
                except Exception as e:
                    error = _dbmod.parseError(e)
                    self._recordWorkerCycle(telemetryKey, success=False, error=error)
                    _dbmod.logger.error("[%s-%s] Error in %s backfill loop: %s",
                                        logPrefix, self.user, errorLabel, error)
                    if stop_event.wait(idleWaitSeconds):
                        break
                else:
                    self._recordWorkerCycle(telemetryKey, success=True)
        finally:
            _dbmod.logger.info("[%s-%s] Exited gracefully", logPrefix, self.user)

    def _lastfmGenreBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Fetches Last.fm genre tags for this user's played artists, albums
        and tracks (most-played first), then - once the own queue is drained -
        for everyone else's (the catalog is shared, so one keyed user's worker
        converges the whole instance). Pacing comes from the process-wide rate
        limiter inside LastfmClient, not from this loop.

        `stop_event` is THIS run's private event (see the fresh-event note in
        startLastfmGenreBackfiller); the loop's own lifecycle checks use it
        exclusively so a later restart can never revive this thread."""
        self._runLastfmLoop(
            stop_event=stop_event, eventAttr="lastfm_stop_event",
            minStartDelay=self.LASTFM_BACKFILLER_MIN_START_DELAY,
            maxStartDelay=self.LASTFM_BACKFILLER_MAX_START_DELAY,
            idleWaitSeconds=self.LASTFM_IDLE_WAIT_SECONDS,
            enabled=self.repo.isLastfmGenreBackfillEnabled,
            runWork=self._runLastfmCycle, logPrefix="LastfmWorker",
            errorLabel="genre", telemetryKey="lastfm_genre")

    def _runLastfmCycle(self, client: LastfmClient, scopeUsername: str | None,
                        stop_event: threading.Event | None = None) -> bool:
        """One batch each of artists -> albums -> tracks (that order is what
        makes same-cycle inheritance work: by the time a tag-less track is
        processed, its primary artist usually has a definitive result).
        Returns whether anything got a definitive result - False means the
        scope's queue is drained (or everything failed transiently) and the
        caller should fall through to the global queue / idle.

        `stop_event` is the calling RUN's private event, threaded down into
        every batch. Reading self.lastfm_stop_event here instead broke the
        fresh-event-per-run invariant (periodic.py): stop joins for only 3s,
        then start assigns a fresh unset event - so a zombie batch from the
        stopped run (the profile page's key save restarts this worker) never
        broke early and ran a whole batch of HTTP lookups alongside the new
        worker. The None fallback mirrors the loop entrypoints', for direct
        single-run callers (tests)."""
        if stop_event is None:
            stop_event = self.lastfm_stop_event
        processedAny = self._processLastfmArtistBatch(client, scopeUsername, stop_event=stop_event)
        if stop_event.is_set():
            return processedAny
        processedAny = self._processLastfmAlbumBatch(client, scopeUsername, stop_event=stop_event) or processedAny
        if stop_event.is_set():
            return processedAny
        return self._processLastfmTrackBatch(client, scopeUsername, stop_event=stop_event) or processedAny

    def startLastfmBiographyBackfiller(self) -> None:
        """Start the background thread that backfills artist biographies from
        Last.fm. Runs independently of the genre backfiller (its own thread,
        stop event and idle cycle - see the LASTFM_BIOGRAPHY_* constants),
        not sequentially after it. No-op without a stored API key - keyless
        users get no idle thread, and the profile page's key save calls this
        again once a key exists."""
        self._startLastfmWorker("lastfm_biography_thread", "lastfm_biography_stop_event",
                                 self._lastfmBiographyBackfillLoop,
                                 f"lastfm-bios-{self.user}", "LastfmBioWorker")

    def stopLastfmBiographyBackfiller(self) -> None:
        """Signal and wait for the background biography backfiller to stop.

        Deliberately nothing after the shared helper: it nulls the thread
        attribute INSIDE the lock and only then joins, outside it. A trailing
        `self.lastfm_biography_thread = None` here (left over from the
        hand-rolled body this replaced) ran unlocked once the join returned, so
        it discarded the reference of any start that arrived during that join -
        leaving a live worker no stop path could reach."""
        self._stopLastfmWorker("lastfm_biography_thread", "lastfm_biography_stop_event")

    def _lastfmBiographyBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Fetches Last.fm biographies for this user's played artists (most-
        played first), then - once the own queue is drained - for everyone
        else's, same own-then-global shape as _lastfmGenreBackfillLoop.
        Unlike lazyFetchArtistBio's on-demand one-shot fetch, an artist whose
        lookup came back with no bio is revisited after
        BIOGRAPHY_BACKFILL_RETRY_SECONDS (see getArtistsMissingBiographies).

        `stop_event` is THIS run's private event (see the fresh-event note in
        startLastfmGenreBackfiller); the loop's own lifecycle checks use it
        exclusively so a later restart can never revive this thread."""
        self._runLastfmLoop(
            stop_event=stop_event, eventAttr="lastfm_biography_stop_event",
            minStartDelay=self.LASTFM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY,
            maxStartDelay=self.LASTFM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY,
            idleWaitSeconds=self.LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS,
            enabled=self.repo.isArtistBioEnabled,
            runWork=self._processLastfmBiographyBatch, logPrefix="LastfmBioWorker",
            errorLabel="biography", telemetryKey="lastfm_artist_bio")

    def _processLastfmBiographyBatch(self, client: LastfmClient, scopeUsername: str | None,
                                     stop_event: threading.Event | None = None) -> bool:
        """One batch of artist.getinfo lookups. Claims/releases under the
        same "bio" kind as lazyFetchArtistBio's on-demand fetch, so the two
        paths can never double-fetch the same artist concurrently. Returns
        whether anything got a definitive result - False means the scope's
        queue is drained (or everything failed transiently) and the caller
        should fall through to the global queue / idle."""
        #< stop_event: the calling run's private event - see _runLastfmCycle
        if stop_event is None:
            stop_event = self.lastfm_biography_stop_event
        rows = self.repo.getArtistsMissingBiographies(self.LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("bio", rows)
        processedAny = False
        try:
            for row in claimed:
                if stop_event.is_set():
                    break
                outcome = client.getArtistInfo(row["name"], stop_event=stop_event)
                if outcome is None:   #< rate-limit slot aborted: we're stopping
                    break
                if outcome.status == _dbmod.OUTCOME_INVALID_KEY:
                    raise _dbmod._LastfmInvalidKeyError()
                if outcome.status == _dbmod.OUTCOME_TRANSIENT:
                    continue   #< stays unattempted, retried next cycle
                bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
                self.repo.setArtistBio(row["id"], bio)
                processedAny = True
        finally:
            self._releaseLastfmEntities("bio", claimed)
        return processedAny

    def startLastfmAlbumBiographyBackfiller(self) -> None:
        """Start the background thread that backfills album biographies from
        Last.fm. Runs independently of the artist biography backfiller (its
        own thread, stop event and idle cycle - see the
        LASTFM_ALBUM_BIOGRAPHY_* constants), not sequentially after it.
        No-op without a stored API key - keyless users get no idle thread,
        and the profile page's key save calls this again once a key
        exists."""
        self._startLastfmWorker("lastfm_album_biography_thread", "lastfm_album_biography_stop_event",
                                 self._lastfmAlbumBiographyBackfillLoop,
                                 f"lastfm-album-bios-{self.user}", "LastfmAlbumBioWorker")

    def stopLastfmAlbumBiographyBackfiller(self) -> None:
        """Signal and wait for the background album biography backfiller to stop."""
        self._stopLastfmWorker("lastfm_album_biography_thread", "lastfm_album_biography_stop_event")

    def _lastfmAlbumBiographyBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Fetches Last.fm biographies for this user's played albums (most-
        played first), then - once the own queue is drained - for everyone
        else's, same own-then-global shape as _lastfmBiographyBackfillLoop.
        Unlike lazyFetchAlbumBio's on-demand one-shot fetch, an album whose
        lookup came back with no bio is revisited after
        BIOGRAPHY_BACKFILL_RETRY_SECONDS (see getAlbumsMissingBiographies).

        `stop_event` is THIS run's private event (see the fresh-event note in
        startLastfmGenreBackfiller); the loop's own lifecycle checks use it
        exclusively so a later restart can never revive this thread."""
        self._runLastfmLoop(
            stop_event=stop_event, eventAttr="lastfm_album_biography_stop_event",
            minStartDelay=self.LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY,
            maxStartDelay=self.LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY,
            idleWaitSeconds=self.LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS,
            enabled=self.repo.isAlbumBioEnabled,
            runWork=self._processLastfmAlbumBiographyBatch,
            logPrefix="LastfmAlbumBioWorker", errorLabel="album biography",
            telemetryKey="lastfm_album_bio")

    def _processLastfmAlbumBiographyBatch(self, client: LastfmClient, scopeUsername: str | None,
                                          stop_event: threading.Event | None = None) -> bool:
        """One batch of album.getinfo lookups. Claims/releases under the
        "album_bio" kind - distinct from "bio" (artist bios) and "album"
        (genre lookups), so none of the three ever collide over the same
        row. Albums with no resolvable primary artist (album.getinfo needs
        one, like the genre album batch's own lookup) are marked attempted
        with no bio rather than skipped forever. Returns whether anything
        got a definitive result - False means the scope's queue is drained
        (or everything failed transiently) and the caller should fall
        through to the global queue / idle."""
        #< stop_event: the calling run's private event - see _runLastfmCycle
        if stop_event is None:
            stop_event = self.lastfm_album_biography_stop_event
        rows = self.repo.getAlbumsMissingBiographies(self.LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("album_bio", rows)
        processedAny = False
        try:
            primaries = self.repo.getAlbumPrimaryArtists([row["id"] for row in claimed])
            for row in claimed:
                if stop_event.is_set():
                    break
                primary = primaries.get(row["id"])
                if primary is None:
                    self.repo.setAlbumBio(row["id"], None)
                    processedAny = True
                    continue
                outcome = self._lastfmLookupBioOutcome(
                    lambda name: client.getAlbumInfo(primary["artist_name"], name,
                                                     stop_event=stop_event),
                    row["name"], stop_event=stop_event)
                if outcome is None:   #< rate-limit slot aborted: we're stopping
                    break
                if outcome.status == _dbmod.OUTCOME_INVALID_KEY:
                    raise _dbmod._LastfmInvalidKeyError()
                if outcome.status == _dbmod.OUTCOME_TRANSIENT:
                    continue   #< stays unattempted, retried next cycle

                bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
                self.repo.setAlbumBio(row["id"], bio)
                processedAny = True

        finally:
            self._releaseLastfmEntities("album_bio", claimed)
        return processedAny

    def _claimLastfmEntities(self, kind: str, rows: list[dict]) -> list[dict]:
        """Process-wide in-flight dedup across users' workers (the catalog is
        shared): only rows not already claimed elsewhere are returned, and the
        caller must release them via _releaseLastfmEntities."""
        claimed = []
        with _dbmod.Database._lastfm_active_lock:
            for row in rows:
                key = (kind, row["id"])
                if key not in _dbmod.Database._lastfm_active:
                    _dbmod.Database._lastfm_active.add(key)
                    claimed.append(row)
        return claimed

    def _releaseLastfmEntities(self, kind: str, rows: list[dict]) -> None:
        with _dbmod.Database._lastfm_active_lock:
            for row in rows:
                _dbmod.Database._lastfm_active.discard((kind, row["id"]))

    @staticmethod
    def _lastfmOutcomeGenres(outcome) -> tuple[bool, list[str]]:
        """(isDefinitive, filteredGenres) for a fetch outcome. Transient
        outcomes aren't definitive - the entity stays unmarked and retries
        next cycle. Invalid-key outcomes escalate to pause the whole loop."""
        if outcome.status == _dbmod.OUTCOME_INVALID_KEY:
            raise _dbmod._LastfmInvalidKeyError()
        if outcome.status == _dbmod.OUTCOME_TRANSIENT:
            return False, []
        # OK (tags, possibly empty) and NOT_FOUND are both definitive.
        genres = _dbmod.filterTagsToGenres(outcome.tags) if outcome.status == _dbmod.OUTCOME_OK else []
        return True, genres

    def _lastfmLookupOwnGenres(self, lookup, entityName: str) -> tuple[bool, list[str], bool]:
        """(definitive, genres, aborted) for an own-tags lookup with one
        cleaned-name retry: a definitive-empty result for a decorated Spotify
        name ("Song - Radio Edit", "Song (feat. X)") re-asks with the cleaned
        form, since Last.fm frequently only knows the undecorated title.
        `lookup(name)` -> FetchOutcome | None (None = rate-limit slot aborted,
        reported via `aborted`). A non-definitive retry reports not-definitive
        so the entity stays unmarked and the pair re-runs next cycle."""
        outcome = lookup(entityName)
        if outcome is None:
            return False, [], True
        definitive, genres = self._lastfmOutcomeGenres(outcome)
        if not definitive or genres:
            return definitive, genres, False
        cleanedName = _dbmod.cleanLookupName(entityName)
        if cleanedName == entityName:
            return True, [], False
        outcome = lookup(cleanedName)
        if outcome is None:
            return False, [], True
        definitive, genres = self._lastfmOutcomeGenres(outcome)
        return definitive, genres, False

    def _lastfmLookupBioOutcome(self, lookup, entityName: str,
                                stop_event: threading.Event | None = None):
        """Final ArtistInfoOutcome|AlbumInfoOutcome (or None = rate-limit slot
        aborted, or `stop_event` fired between the two lookups) for a
        *.getinfo bio lookup with one cleaned-name retry - the bio
        counterpart of _lastfmLookupOwnGenres, sharing the exact same
        decoration-stripping fallback (cleanLookupName) so the background
        backfillers and the admin "Refresh Last.fm Data" button behave
        identically. When the verbatim name yields a definitive result
        carrying no bio, it re-asks with the cleaned form, since Last.fm often
        only knows the undecorated title ("Album (Deluxe Edition)" -> "Album").
        A non-definitive (transient/invalid-key) outcome is returned unchanged
        for the caller to classify; the retry fires only on a definitive-but-
        bioless result and its own outcome replaces the first, whatever the
        status. `lookup(name)` -> outcome | None.

        `stop_event` is optional (the on-demand lazy-fetch/refresh callers
        pass none, same as before) - a background-worker caller passes its
        own stop event so a shutdown signaled right after the verbatim
        lookup skips the retry's own full name-fallback HTTP chain instead
        of running it to completion before the per-row loop notices."""
        outcome = lookup(entityName)
        if outcome is None:
            return None
        definitive = outcome.status in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND)
        if not definitive or outcome.bio is not None:
            return outcome
        if stop_event is not None and stop_event.is_set():
            return None
        cleanedName = _dbmod.cleanLookupName(entityName)
        if cleanedName == entityName:
            return outcome
        return lookup(cleanedName)

    def _processLastfmArtistBatch(self, client: LastfmClient, scopeUsername: str | None,
                                  stop_event: threading.Event | None = None) -> bool:
        #< stop_event: the calling run's private event - see _runLastfmCycle
        if stop_event is None:
            stop_event = self.lastfm_stop_event
        rows = self.repo.getArtistsMissingGenres(self.LASTFM_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("artist", rows)
        processedAny = False
        try:
            for row in claimed:
                if stop_event.is_set():
                    break
                outcome = client.getArtistTopTags(row["name"], stop_event=stop_event)
                if outcome is None:   #< rate-limit slot aborted: we're stopping
                    break
                definitive, genres = self._lastfmOutcomeGenres(outcome)
                if not definitive:
                    continue
                if genres:
                    self.repo.replaceArtistGenres(row["id"], genres)
                self.repo.markArtistsLastfmAttempted([row["id"]])
                processedAny = True
        finally:
            self._releaseLastfmEntities("artist", claimed)
        return processedAny

    def _processLastfmAlbumBatch(self, client: LastfmClient, scopeUsername: str | None,
                                 stop_event: threading.Event | None = None) -> bool:
        #< stop_event: the calling run's private event - see _runLastfmCycle
        if stop_event is None:
            stop_event = self.lastfm_stop_event
        rows = self.repo.getAlbumsMissingGenres(self.LASTFM_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("album", rows)
        processedAny = False
        try:
            primaries = self.repo.getAlbumPrimaryArtists([row["id"] for row in claimed])
            for row in claimed:
                if stop_event.is_set():
                    break
                primary = primaries.get(row["id"])
                if primary is None:
                    # No derivable artist: album.getTopTags needs artist+album,
                    # and there's nothing to inherit from either.
                    self.repo.markAlbumsLastfmAttempted([row["id"]])
                    processedAny = True
                    continue

                candidates = self.repo.getAlbumCandidateArtists(row["id"])
                if not candidates:
                    candidates = [primary]

                definitive = False
                genres = []
                aborted = False
                selected_primary = primary
                for cand in candidates:
                    definitive, genres, aborted = self._lastfmLookupOwnGenres(
                        lambda name, c_name=cand["artist_name"]: client.getAlbumTopTags(c_name, name,
                                                                                        stop_event=stop_event),
                        row["name"])
                    if aborted:
                        break
                    if definitive and genres:
                        selected_primary = cand
                        break

                if aborted:
                    break
                if not definitive:
                    continue
                if self._storeLastfmGenresWithInheritance(
                        client, "album", row["id"], genres,
                        selected_primary["artist_id"], selected_primary["artist_name"],
                        stop_event=stop_event):
                    processedAny = True
        finally:
            self._releaseLastfmEntities("album", claimed)
        return processedAny

    def _processLastfmTrackBatch(self, client: LastfmClient, scopeUsername: str | None,
                                 stop_event: threading.Event | None = None) -> bool:
        #< stop_event: the calling run's private event - see _runLastfmCycle
        if stop_event is None:
            stop_event = self.lastfm_stop_event
        rows = self.repo.getTracksMissingGenres(self.LASTFM_QUEUE_BATCH_SIZE, scopeUsername)
        claimed = self._claimLastfmEntities("track", rows)
        processedAny = False
        try:
            for row in claimed:
                if stop_event.is_set():
                    break
                definitive, genres, aborted = self._lastfmLookupOwnGenres(
                    lambda name: client.getTrackTopTags(row["artist_name"], name,
                                                        stop_event=stop_event),
                    row["name"])
                if aborted:
                    break
                if not definitive:
                    continue
                if self._storeLastfmGenresWithInheritance(
                        client, "track", row["id"], genres,
                        row["artist_id"], row["artist_name"], albumId=row["album_id"],
                        stop_event=stop_event):
                    processedAny = True
        finally:
            self._releaseLastfmEntities("track", claimed)
        return processedAny

    def _storeLastfmGenresWithInheritance(self, client: LastfmClient, kind: str,
                                          entityId: str, ownGenres: list[str],
                                          artistId: str, artistName: str,
                                          albumId: str | None = None,
                                          stop_event: threading.Event | None = None,
                                          timeout: float | None = None) -> bool:
        """Store a definitive lookup result for a track/album. Own tags win;
        with none, a track first materializes its album's OWN genres as
        inherited rows (the closer granularity - the album batch runs earlier
        in the same cycle), then both kinds fall back to the primary artist's
        genres. An artist without a definitive result yet is resolved INLINE
        with one extra request: it may never enter the artist queue at all
        (an album's derived primary artist needs no played track), and
        leaving the entity unmarked would re-fetch it every cycle forever.
        Returns False only when the artist lookup couldn't complete
        (stopping/transient) - the entity stays unmarked and retries."""
        #< stop_event: the calling run's private event - see _runLastfmCycle
        if stop_event is None:
            stop_event = self.lastfm_stop_event
        replaceGenres = (self.repo.replaceTrackGenres if kind == "track"
                         else self.repo.replaceAlbumGenres)
        markAttempted = (self.repo.markTracksLastfmAttempted if kind == "track"
                         else self.repo.markAlbumsLastfmAttempted)
        if ownGenres:
            replaceGenres(entityId, ownGenres, inherited=False)
            markAttempted([entityId])
            return True
        if kind == "track" and albumId is not None:
            # Only the album's own tags cascade - its inherited rows are
            # already artist genres, which the fallback below covers from
            # the (possibly fresher) artist state directly.
            albumOwnGenres = [g["genre"] for g in self.repo.getAlbumGenres(albumId)
                              if not g["inherited"]]
            if albumOwnGenres:
                replaceGenres(entityId, albumOwnGenres, inherited=True)
                markAttempted([entityId])
                return True
        artistState = self.repo.getArtistLastfmState(artistId)   #< fresh: this cycle's artist batch is visible
        if artistState["attempted_at"] is None and not artistState["genres"]:
            # Rarely duplicates another worker's in-flight artist fetch (the
            # claim set only guards batch rows) - harmless, the write is
            # idempotent.
            outcome = client.getArtistTopTags(artistName, stop_event=stop_event, timeout=timeout)
            if outcome is None:
                return False
            definitive, artistGenres = self._lastfmOutcomeGenres(outcome)
            if not definitive:
                return False
            if artistGenres:
                self.repo.replaceArtistGenres(artistId, artistGenres)
            self.repo.markArtistsLastfmAttempted([artistId])
            artistState = {"attempted_at": _dbmod.time.time(), "genres": artistGenres}
        if artistState["genres"]:
            replaceGenres(entityId, artistState["genres"], inherited=True)
            markAttempted([entityId])
            return True

        # Secondary artist fallback for track / album inheritance when primary artist has 0 genres:
        secondaryArtists = []
        if kind == "track":
            secondaryArtists = self.repo.getTrackSecondaryArtists(entityId)
        elif kind == "album":
            secondaryArtists = [cand for cand in self.repo.getAlbumCandidateArtists(entityId)
                                if cand["artist_id"] != artistId]

        for sec in secondaryArtists:
            secId, secName = sec["artist_id"], sec["artist_name"]
            secState = self.repo.getArtistLastfmState(secId)
            if secState["attempted_at"] is None and not secState["genres"]:
                outcome = client.getArtistTopTags(secName, stop_event=stop_event, timeout=timeout)
                if outcome is None:
                    return False
                def_sec, secGenres = self._lastfmOutcomeGenres(outcome)
                if not def_sec:
                    return False
                if secGenres:
                    self.repo.replaceArtistGenres(secId, secGenres)
                self.repo.markArtistsLastfmAttempted([secId])
                secState = {"attempted_at": _dbmod.time.time(), "genres": secGenres}
            if secState["genres"]:
                replaceGenres(entityId, secState["genres"], inherited=True)
                markAttempted([entityId])
                return True

        markAttempted([entityId])   #< definitively tag-less everywhere
        return True

