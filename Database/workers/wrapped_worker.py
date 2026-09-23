# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Imported for the type hints below. Everything used at RUNTIME still goes
# through _dbmod, so the suite's patch("Database.database.X") targets are
# unaffected - these names were simply never defined here, leaving the hints
# unresolvable for tooling and for typing.get_type_hints(). All leaf modules
# (stdlib, or Database/lastfm.py, which imports nothing of ours), so a real
# import costs nothing and cannot cycle.
import datetime
import threading

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod

# The Wrapped page's "top 100" lists, and the discovery lists rendered beside
# them: one cap spelled once rather than six literals free to drift apart.
WRAPPED_LIST_LIMIT = 100
# The two rankings a cached pool is captured under. The page re-sorts the pool
# by whichever metric the user picks (see wrapped_builder._resortByMetric) and
# can only ever show what the capture included - so both are captured and
# merged, or the year's #1 by listening time is absent whenever it sits
# outside the top WRAPPED_LIST_LIMIT by plays (a long track played rarely).
WRAPPED_POOL_METRICS = ("plays", "totalTimeListened")
# A PAST year's discovery lists carry lifetime play counts (see the comment at
# the discoveries step), which later listening keeps changing while the
# year's own play count - the freshness signal - stands still. A past year is
# therefore also rebuilt once plays have been recorded since it was computed,
# but no more often than this: rebuilding every past year on every cycle of
# active listening is the cost that signal was chosen to avoid.
WRAPPED_PAST_YEAR_REFRESH_SECONDS = 24 * 60 * 60


class WrappedWorkerMixin:
    """The periodic Wrapped recalculation worker and its per-year cache invalidation."""

    def getWrappedWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the asynchronous Wrapped
        stats calculator. Always "configured" - it needs no credentials."""
        return self._workerStatus("wrapped_thread", "wrapped", configured=True)

    def startWrappedCalculationsWorker(self) -> None:
        """Start the background thread to precalculate wrapped data."""
        self._startPeriodicWorker("wrapped_thread", "wrapped_stop_event",
                                   self._wrappedCalculationsLoop,
                                   f"wrapped-worker-{self.user}", logPrefix="WrappedWorker")

    def stopWrappedCalculationsWorker(self) -> None:
        """Signal and wait for the background wrapped worker thread to stop."""
        self._stopPeriodicWorker("wrapped_thread", "wrapped_stop_event")

    def _wrappedCalculationsLoop(self, stop_event: threading.Event | None = None) -> None:
        """Periodically checks if plays have changed and recalculates wrapped stats.

        `stop_event` is THIS run's private event (see the fresh-event note in
        startWrappedCalculationsWorker) - a later restart can never revive
        this thread."""
        import random
        if stop_event is None:
            stop_event = self.wrapped_stop_event
        try:
            # 1. Random startup delay to distribute CPU load if multiple users are loaded
            startup_delay = random.randint(self.WRAPPED_WORKER_MIN_START_DELAY, self.WRAPPED_WORKER_MAX_START_DELAY)
            _dbmod.logger.info("[WrappedWorker-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                return

            while not stop_event.is_set():
                try:
                    self._checkAndRecalculateWrapped(stop_event)
                except Exception as e:
                    self._recordWorkerCycle("wrapped", success=False, error=_dbmod.parseError(e))
                    _dbmod.logger.error("[WrappedWorker-%s] Error checking wrapped: %s", self.user, _dbmod.parseError(e))
                else:
                    self._recordWorkerCycle("wrapped", success=True)

                # Check loop interval
                if stop_event.wait(self.WRAPPED_WORKER_LOOP_INTERVAL):
                    break
        except Exception as e:
            _dbmod.logger.error("[WrappedWorker-%s] Worker loop crashed: %s", self.user, _dbmod.parseError(e))

    def _getWrappedRecalcLock(self, year: int) -> threading.Lock:
        """Per-(user instance, year) lock so the periodic worker and an
        on-demand /wrapped recalculation never run _calculateAndSaveWrapped
        for the same year at the same time."""
        with self._wrapped_recalc_locks_guard:
            lock = self._wrapped_recalc_locks.get(year)
            if lock is None:
                lock = _dbmod.threading.Lock()
                self._wrapped_recalc_locks[year] = lock
            return lock

    def _wrappedCacheNeedsRecalc(self, year: int, yearStart: datetime.datetime, yearEnd: datetime.datetime, max_played_at: float):
        """Compares the cached (max_played_at, play_count) snapshot for a year
        against live values. Returns (isStale, cached_max, cached_total, current_total).

        A past year is stale on one more count - see
        WRAPPED_PAST_YEAR_REFRESH_SECONDS."""
        current_total = self.repo.getPlayCountInPeriod(self.user, yearStart.timestamp(), yearEnd.timestamp())
        cached_max = self.repo.getCachedWrappedMaxPlayedAt(self.user, year)
        cached_total = self.repo.getCachedWrappedTotalPlays(self.user, year)
        isStale = cached_max is None or cached_total is None or cached_max < max_played_at or cached_total != current_total
        if not isStale:
            isStale = self._pastYearDiscoveriesAreStale(year, yearEnd)
        return isStale, cached_max, cached_total, current_total

    def _pastYearDiscoveriesAreStale(self, year: int, yearEnd: datetime.datetime) -> bool:
        """Whether a year that is over has had plays recorded since its cache
        was computed, that computation being at least
        WRAPPED_PAST_YEAR_REFRESH_SECONDS old. Both halves, or it is not
        worth a rebuild: nothing new means the lifetime counts stand, and a
        fresh computation already holds whatever landed before it."""
        nowTs = _dbmod.time.time()
        if yearEnd.timestamp() > nowTs:
            return False
        calculatedAt = self.repo.getCachedWrappedCalculatedAt(self.user, year)
        if calculatedAt is None or nowTs - calculatedAt < WRAPPED_PAST_YEAR_REFRESH_SECONDS:
            return False
        return self.repo.getPlayCountInPeriod(self.user, calculatedAt, nowTs) > 0

    def _checkAndRecalculateWrapped(self, stop_event: threading.Event | None = None) -> None:
        """Checks for each year if there is new data and triggers recalculation if needed."""
        if stop_event is None:
            stop_event = self.wrapped_stop_event
        nowLocal = _dbmod.datetime.datetime.now(tz=self.tz)
        currentYear = nowLocal.year

        oldestEntries = self.getEntriesFromOld(count=1, fullPagination=False)
        earliestYear = _dbmod.convertToDatetime(oldestEntries[0]["playedAt"], tz=self.tz).year if oldestEntries else currentYear
        availableYears = list(range(currentYear, earliestYear - 1, -1))

        for year in availableYears:
            if stop_event.is_set():
                break

            yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

            # Query max played_at for this year
            max_played_at = self.repo.getMaxPlayedAtInPeriod(self.user, yearStart.timestamp(), yearEnd.timestamp())
            if max_played_at is None:
                # No plays for this year. If there is cached data, delete it.
                self.repo.deleteUserWrapped(self.user, year)
                continue

            isStale, cached_max, cached_total, current_total = self._wrappedCacheNeedsRecalc(year, yearStart, yearEnd, max_played_at)
            if not isStale:
                continue

            lock = self._getWrappedRecalcLock(year)
            if not lock.acquire(blocking=False):
                # An on-demand /wrapped recalculation is already handling this
                # year; don't duplicate the work or block the periodic loop -
                # the next cycle will notice if anything is still stale.
                _dbmod.logger.info("[WrappedWorker-%s] Year %d recalculation already in progress elsewhere, skipping this cycle", self.user, year)
                continue
            try:
                cachedMaxDisplay = _dbmod.convertToDatetime(cached_max, tz=self.tz).isoformat() if cached_max is not None else "none"
                actualMaxDisplay = _dbmod.convertToDatetime(max_played_at, tz=self.tz).isoformat()
                _dbmod.logger.info("[WrappedWorker-%s] Recalculating wrapped for year %d (cached max: %s, actual max: %s, cached plays: %s, actual plays: %s)",
                            self.user, year, cachedMaxDisplay, actualMaxDisplay, str(cached_total), str(current_total))
                self._calculateAndSaveWrapped(year, yearStart, yearEnd, max_played_at)
            finally:
                lock.release()
            # Sleep briefly between years to distribute database load
            if stop_event.wait(self.WRAPPED_YEAR_DELAY_SECONDS):
                break

    def _calculateAndSaveWrapped(self, year: int, yearStart: datetime.datetime, yearEnd: datetime.datetime, max_played_at: float) -> None:
        """Runs all queries to precalculate the Spotify Wrapped stats and caches them in user_wrapped table."""
        #< read before the first query, compared again inside the save's own
        #  transaction: a merge/split invalidation landing mid-computation
        #  makes this snapshot a torn read that must not be cached. Per user,
        #  so it also sees invalidations of this user's plays alone
        startGeneration = self.repo.getWrappedInvalidationGeneration(self.user)
        # 1. Total plays and milliseconds
        totalPlays, totalMs = self.getPlayTotals(yearStart, yearEnd)

        # 2. Longest streak
        longestStreak = self.getLongestStreak(yearStart, yearEnd)

        # 3. Peak listening time
        peakListeningTime = self.getPeakListeningTime(yearStart, yearEnd)
        peak_day = peakListeningTime[0] if peakListeningTime else None
        peak_plays = peakListeningTime[1] if peakListeningTime else None

        # 4. Unique counts
        uniqueSongs = self.getSongsCount(yearStart, yearEnd)
        uniqueArtists = self.getArtistsCount(yearStart, yearEnd)
        discoveredSongsCount = self.getDiscoveredSongsCount(yearStart, yearEnd)
        discoveredArtistsCount = self.getDiscoveredArtistsCount(yearStart, yearEnd)

        # 5. Timeseries
        timeSeriesDay = self.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy="day")
        timeSeriesWeek = self.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy="week")
        timeSeriesMonth = self.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy="month")

        # 6. Top 100 lists - the top WRAPPED_LIST_LIMIT under EACH metric in
        #    WRAPPED_POOL_METRICS, merged (see there). Plays-ranked first, so
        #    the pool's own order is still the plays ranking the export button
        #    and the default sort read it as.
        topSongs = self._pooledByEveryMetric(
            lambda by: self.getTopSongs(startDate=yearStart, endDate=yearEnd, by=by, limit=WRAPPED_LIST_LIMIT))
        topArtists = self._pooledByEveryMetric(
            lambda by: self.getTopArtists(startDate=yearStart, endDate=yearEnd, by=by, limit=WRAPPED_LIST_LIMIT))
        topAlbums = self._pooledByEveryMetric(
            lambda by: self.getTopAlbums(startDate=yearStart, endDate=yearEnd, by=by, limit=WRAPPED_LIST_LIMIT))

        # 7. Discoveries lists. The same lifetime aggregates as always -
        #    ranking and displayed counts stay LIFETIME numbers, and an entity
        #    is credited to the year of its first-ever listen - but the
        #    first-listen filter, the plays ordering and the list cap now run
        #    in SQL (see Repository._firstListenClause). The old shape
        #    hydrated every entity ever played, three times, to keep 100 rows
        #    each - and the current year recalculates on every worker cycle
        #    that saw new plays, i.e. every 15 minutes of active listening.
        discoveredSongsList = self._pooledByEveryMetric(
            lambda by: self.getSongsStats(sortBy=by, limit=WRAPPED_LIST_LIMIT,
                                          firstListenedStart=yearStart, firstListenedEnd=yearEnd))
        discoveredArtistsList = self._pooledByEveryMetric(
            lambda by: self.getArtistsStats(sortBy=by, limit=WRAPPED_LIST_LIMIT,
                                            firstListenedStart=yearStart, firstListenedEnd=yearEnd))
        discoveredAlbumsList = self._pooledByEveryMetric(
            lambda by: self.getAlbumsStats(sortBy=by, limit=WRAPPED_LIST_LIMIT,
                                           firstListenedStart=yearStart, firstListenedEnd=yearEnd))

        data = {
            "calculated_at": _dbmod.time.time(),
            "max_played_at": max_played_at,
            "total_plays": totalPlays,
            "total_ms": totalMs,
            "longest_streak": longestStreak,
            "peak_day": peak_day,
            "peak_plays": peak_plays,
            "unique_songs": uniqueSongs,
            "unique_artists": uniqueArtists,
            "discovered_songs": discoveredSongsCount,
            "discovered_artists": discoveredArtistsCount,
            "time_series_day": _dbmod.json.dumps(timeSeriesDay),
            "time_series_week": _dbmod.json.dumps(timeSeriesWeek),
            "time_series_month": _dbmod.json.dumps(timeSeriesMonth),
            "top_songs": _dbmod.json.dumps(topSongs),
            "top_artists": _dbmod.json.dumps(topArtists),
            "top_albums": _dbmod.json.dumps(topAlbums),
            "discovered_songs_list": _dbmod.json.dumps(discoveredSongsList),
            "discovered_artists_list": _dbmod.json.dumps(discoveredArtistsList),
            "discovered_albums_list": _dbmod.json.dumps(discoveredAlbumsList),
        }
        if not self.repo.saveCachedWrapped(self.user, year, data,
                                           expectedGeneration=startGeneration):
            #< an invalidation (a merge, a split) landed while this year was
            #  being computed: some reads predate it, some follow it, and the
            #  freshness signals cannot tell the difference. Dropped; the next
            #  worker cycle or page view recomputes from a clean state.
            _dbmod.logger.info(
                "[WrappedWorker-%s] Year %d recalculated across an invalidation; discarded",
                self.user, year)

    @staticmethod
    def _pooledByEveryMetric(fetch) -> list:
        """`fetch(metric)` for each of WRAPPED_POOL_METRICS, concatenated in
        that order with an entity that ranks under more than one metric kept
        once, at its first (plays-ranked) position."""
        pool: list = []
        seen: set = set()
        for metric in WRAPPED_POOL_METRICS:
            for item in fetch(metric):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                pool.append(item)
        return pool

    def recalculateWrappedForYear(self, year: int) -> None:
        """Calculate and cache wrapped stats for a year immediately (synchronously).

        Waits on this year's recalc lock rather than racing the periodic
        worker: if the worker is already recalculating this exact year, this
        blocks until it's done instead of duplicating the (expensive) work,
        then re-checks whether the cache is still stale before doing anything -
        the worker may have already brought it up to date while we waited.

        max_played_at is read TWICE, and the second read is the one that
        counts. It is the freshness stamp written next to the computed numbers,
        so it has to describe the data actually read - and the wait above can
        be as long as a whole Wrapped calculation. A play landing in that gap
        was included in the numbers but not in the stamp, so the next freshness
        check compared the new max against the old stamp and recomputed
        immediately. The first read stays outside the lock because it is a
        different question ("has this user any plays this year at all"), and
        answering it under the lock would make every empty year - which the
        periodic worker walks in full - pay an acquisition to learn nothing.
        """
        nowLocal = _dbmod.datetime.datetime.now(tz=self.tz)
        yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        if self.repo.getMaxPlayedAtInPeriod(self.user, yearStart.timestamp(), yearEnd.timestamp()) is None:
            return

        with self._getWrappedRecalcLock(year):
            max_played_at = self.repo.getMaxPlayedAtInPeriod(
                self.user, yearStart.timestamp(), yearEnd.timestamp())
            if max_played_at is None:
                return   #< an overwrite-import wiped the year while we waited
            isStale, _, _, _ = self._wrappedCacheNeedsRecalc(year, yearStart, yearEnd, max_played_at)
            if isStale:
                self._calculateAndSaveWrapped(year, yearStart, yearEnd, max_played_at)
