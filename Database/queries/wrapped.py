# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers


class WrappedQueries:
    """WrappedQueries: wrapped data-access methods, mixed into Repository."""

    def getCachedWrappedMaxPlayedAt(self, username: str, year: int) -> float | None:
        row = self._conn().execute(
            "SELECT max_played_at FROM user_wrapped WHERE username = ? AND year = ?",
            (username, year)
        ).fetchone()
        return row[0] if row else None

    def getCachedWrappedCalculatedAt(self, username: str, year: int) -> float | None:
        """When the cached year was computed (wall-clock, as saved) - the
        worker's past-year refresh reads it, see WRAPPED_PAST_YEAR_REFRESH_SECONDS."""
        row = self._conn().execute(
            "SELECT calculated_at FROM user_wrapped WHERE username = ? AND year = ?",
            (username, year)
        ).fetchone()
        return row[0] if row else None

    def getCachedWrappedTotalPlays(self, username: str, year: int) -> int | None:
        row = self._conn().execute(
            "SELECT total_plays FROM user_wrapped WHERE username = ? AND year = ?",
            (username, year)
        ).fetchone()
        return row[0] if row else None

    def deleteUserWrapped(self, username: str, year: int) -> None:
        """Drop one cached year. The wrapped worker's garbage collector, for a
        year whose plays are gone.

        Bumps the invalidation generation ONLY when a row actually went, which
        is the one place that gate is right (deleteUserWrappedFromYear and
        deleteAllUserWrapped bump unconditionally, and their docstrings say
        why). Two halves to it:

        - it must bump when it deletes, because the drop is an invalidation
          like any other and a recalculation in flight would otherwise put the
          row straight back;
        - it must NOT bump when it deletes nothing, because the worker calls
          this on every cycle for every play-less year whether or not anything
          is cached - a 15-minute unconditional bump, per empty year, per user,
          each one discarding some other user's worker's in-flight save.

        What makes the gate safe here and nowhere else: this call reacts to no
        data change of its own. The plays vanished earlier, through the path
        that already invalidated for them. A no-op delete is therefore genuinely
        nothing happening, whereas a no-op deleteUserWrappedFromYear still marks
        an import that moved the first listens a not-yet-cached year is built
        from."""
        conn = self._conn()
        with conn:
            dropped = conn.execute(
                "DELETE FROM user_wrapped WHERE username = ? AND year = ?",
                (username, year)
            ).rowcount
            if dropped:
                self._bumpWrappedGeneration(conn)

    def deleteUserWrappedFromYear(self, username: str, year: int) -> int:
        """Drop every cached year at or after `year`. Returns how many went.

        A Wrapped year is NOT a pure function of that year's own plays. The
        discovery fields - discovered_songs, discovered_artists and the three
        discovered_*_list columns - are anchored on each item's ALL-TIME first
        listen (getDiscoveredSongsCount groups over unfiltered history and
        keeps MIN(played_at) in range; wrapped_builder._discoveriesInYear
        filters an unbounded stats call on firstListenedAt). So a play written
        into an earlier year moves items into and out of LATER years'
        discovery lists while leaving those years' own play_count and
        max_played_at untouched - and those two are all
        _wrappedCacheNeedsRecalc compares, so nothing would ever notice.

        Only forwards: a first listen can move within or after the year the
        new play lands in, never before it, so earlier years are safe and
        dropping them would just buy a full-year recomputation each.

        A no-op for years with no cached row, so the cost is bounded by what
        was actually cached rather than by the length of the user's history.

        Bumps the invalidation generation, for the reason deleteAllWrapped
        gives and with nothing about it that is per-user: the guard's job is to
        stop a recalculation that STARTED before this delete from saving its
        pre-delete snapshot afterwards, and that recalculation can be of any
        year - including one this delete did not drop, which is the damaging
        case. An import writing into 2019 moves the all-time first listens that
        2026's discovery lists are built from, while leaving 2026's own
        total_plays and max_played_at exactly as they were - and those two are
        all _wrappedCacheNeedsRecalc compares, so a restored 2026 is wrong
        forever. The stamp is instance-wide, so the cost of bumping it for one
        user is at most one other discarded recalculation."""
        conn = self._conn()
        with conn:
            self._bumpWrappedGeneration(conn)
            return conn.execute(
                "DELETE FROM user_wrapped WHERE username = ? AND year >= ?",
                (username, year)
            ).rowcount

    def deleteAllUserWrapped(self, username: str) -> int:
        """Drop every cached year for one user, for a change that invalidates
        all of them at once rather than year by year - a timezone change, which
        moves every bucket boundary, weekday name and streak day. Returns how
        many rows went, so the caller can say whether anything was cached.

        Bumps the generation for the reason deleteUserWrappedFromYear does, and
        here it is the stated purpose of the whole call: a year recomputed
        under the OLD zone and saved after this delete is precisely the "old
        zone's figures served indefinitely, share links included" outcome the
        caller (queries/users.py) says the delete exists to prevent."""
        conn = self._conn()
        with conn:
            self._bumpWrappedGeneration(conn)
            return conn.execute(
                "DELETE FROM user_wrapped WHERE username = ?", (username,)
            ).rowcount

    def deleteAllWrapped(self) -> int:
        """Drop every cached Wrapped row, for every user at once - the FULL
        REVERT's invalidation (unmergeAllIsrcMerges), and only that one.

        A merge is global (sameness is a property of the recording), so its
        effect on the frozen top_songs / unique_songs / discovered_songs_list
        JSON reaches every account's every year simultaneously - and none of
        those years would ever notice on their own, because past years'
        max_played_at and play counts are exactly what a merge does NOT change,
        and they are all _wrappedCacheNeedsRecalc compares. Same reasoning as
        deleteUserWrappedFromYear's, taken to the instance.

        An INDIVIDUAL merge or split does not come here, and must not: the
        matcher re-ran on every backfill batch that recorded an ISRC, so
        dropping the instance each time cost a live instance 147 full-history
        rebuilds a day. It now runs at most once a day (see
        TRACK_MERGE_MIN_INTERVAL_SECONDS), which is the other half of the same
        fix - and a manual merge or split from the review queue has no cadence
        at all, so the scope still has to be right on its own.
        Those go to deleteCachedWrappedForTracks, which reaches
        the same conclusion per year instead of per instance. What keeps this
        one honest is that a full revert genuinely does touch every merged
        group at once, so the narrowed form would name the whole merged catalog
        and arrive back here the long way round.

        Cheap to invalidate, lazy to rebuild: the page recalculates a missing
        year synchronously on view (dashboard/wrapped_builder.py) and the
        15-minute worker refills the rest. Returns rows dropped."""
        conn = self._conn()
        with conn:
            return self._deleteAllWrapped(conn)

    def _deleteAllWrapped(self, conn) -> int:
        """Invalidate within a catalog or classification Save transaction.

        Never commits. Classification Save deliberately calls this even for
        unchanged settings/empty caches, so it can repair existing drift."""
        # The generation and delete must commit with the underlying change: a
        # recalculation already in flight must not resurrect its old snapshot.
        # Its save re-checks this stamp inside its own transaction.
        self._bumpWrappedGeneration(conn)
        return conn.execute("DELETE FROM user_wrapped").rowcount

    def deleteCachedWrappedForTracks(self, trackIds) -> int:
        """deleteAllWrapped narrowed to the years a merge of these tracks can
        actually move. Returns rows dropped.

        Same reasoning as deleteAllWrapped about WHY a merge has to invalidate
        at all - past years' max_played_at and play count are exactly what a
        merge does not change, so nothing would ever notice on its own - but
        applied per year instead of to everything. What makes the narrowing
        sound is that a merge only reaches a year through the group's own
        tracks: the frozen top_songs / unique_songs / discovered_*_list JSON of
        a year in which no member was ever played says the same thing before
        and after.

        The cost this replaces was the whole problem. The ISRC backfiller
        re-ran the matcher every few minutes for as long as new music arrived,
        and each batch that merged anything dropped every account's entire
        history: a live instance logged 147 full-history rebuilds a day against
        ~20-40 before the toggle went on, and in 529 recalculations not one
        cached year before the current one ever survived to be reused. The
        matcher's cadence has since been cut to one pass a day
        (TRACK_MERGE_MIN_INTERVAL_SECONDS) as well; the two fixes are
        independent, and this one is what still bounds a manual merge or split
        from the review queue, which answers to no cadence.

        `trackIds` must be the whole merge GROUP, not just the tracks whose
        canonical_id the caller moved - see _mergeGroupTrackIds for the year
        that goes missing otherwise.

        Two deliberate widenings:
        - the year bucket, by WRAPPED_YEAR_TZ_SLACK_SECONDS, because this runs
          in the shared repo and a year boundary is per user's timezone;
        - the generation stamp, which still moves for everyone. It guards
          in-flight recalculations of ANY year against caching a snapshot whose
          reads straddle this merge, so it cannot be narrowed with the delete -
          and it is bumped in the same transaction, for the same serialization
          deleteAllWrapped needs."""
        conn = self._conn()
        with conn:
            return self._deleteCachedWrappedForTracks(conn, trackIds)

    def _deleteCachedWrappedForTracks(self, conn, trackIds) -> int:
        """Invalidate within the caller's transaction, without committing it."""
        ids = list(dict.fromkeys(trackIds))
        self._bumpWrappedGeneration(conn)
        if not ids:
            return 0
        return conn.execute(
            """
            DELETE FROM user_wrapped WHERE EXISTS (
                SELECT 1 FROM plays p
                WHERE p.username = user_wrapped.username
                  AND p.track_id IN (SELECT value FROM json_each(?))
                  AND user_wrapped.year BETWEEN
                          CAST(strftime('%Y', p.played_at - ?, 'unixepoch') AS INTEGER)
                      AND CAST(strftime('%Y', p.played_at + ?, 'unixepoch') AS INTEGER)
            )
            """,
            (json.dumps(ids), WRAPPED_YEAR_TZ_SLACK_SECONDS,
             WRAPPED_YEAR_TZ_SLACK_SECONDS)
        ).rowcount

    def getWrappedInvalidationGeneration(self) -> int:
        row = self._conn().execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (WRAPPED_INVALIDATION_GENERATION_KEY,)).fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _bumpWrappedGeneration(conn) -> None:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            """,
            (WRAPPED_INVALIDATION_GENERATION_KEY,))

    def getCachedWrapped(self, username: str, year: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM user_wrapped WHERE username = ? AND year = ?",
            (username, year)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def saveCachedWrapped(self, username: str, year: int, data: dict,
                          expectedGeneration: int | None = None) -> bool:
        """Store a computed year. Returns whether it was actually stored.

        `expectedGeneration` is the invalidation stamp the computation STARTED
        under. Checked while this write's own transaction already holds the
        write lock, so it serializes against every invalidation's
        bump-and-delete: whichever commits second sees the other, and a
        snapshot whose reads straddle an invalidation is discarded here rather
        than cached as permanently-unstale truth.

        The explicit BEGIN IMMEDIATE is what makes that true, and it is not
        decoration. Python's sqlite3 under legacy transaction control opens a
        transaction for DML only - a SELECT inside `with conn:` runs in
        autocommit and takes no lock - so without it the check read the
        pre-bump value of an invalidation already in flight (uncommitted, and
        therefore invisible), passed, and the INSERT below then waited on that
        invalidation's lock and landed the instant it committed. The row it
        restored is one a merge or an import has made wrong in exactly the
        fields total_plays and max_played_at cannot betray, so
        _wrappedCacheNeedsRecalc would never ask for it again. Same remedy, and
        the same reason, as the rebuild in queries/schema.py.

        Skipped when there is nothing to check: with no expectedGeneration the
        INSERT is the only statement and is its own transaction. Guarded on
        in_transaction so a caller that has already opened one (BEGIN inside
        BEGIN is an error) still gets its write."""
        conn = self._conn()
        with conn:
            if expectedGeneration is not None:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    (WRAPPED_INVALIDATION_GENERATION_KEY,)).fetchone()
                if (int(row["value"]) if row else 0) != expectedGeneration:
                    return False
            conn.execute(
                """
                INSERT INTO user_wrapped (
                    username, year, calculated_at, max_played_at,
                    total_plays, total_ms, longest_streak, peak_day, peak_plays,
                    unique_songs, unique_artists, discovered_songs, discovered_artists,
                    time_series_day, time_series_week, time_series_month,
                    top_songs, top_artists, top_albums,
                    discovered_songs_list, discovered_artists_list, discovered_albums_list
                ) VALUES (
                    :username, :year, :calculated_at, :max_played_at,
                    :total_plays, :total_ms, :longest_streak, :peak_day, :peak_plays,
                    :unique_songs, :unique_artists, :discovered_songs, :discovered_artists,
                    :time_series_day, :time_series_week, :time_series_month,
                    :top_songs, :top_artists, :top_albums,
                    :discovered_songs_list, :discovered_artists_list, :discovered_albums_list
                )
                ON CONFLICT(username, year) DO UPDATE SET
                    calculated_at=excluded.calculated_at,
                    max_played_at=excluded.max_played_at,
                    total_plays=excluded.total_plays,
                    total_ms=excluded.total_ms,
                    longest_streak=excluded.longest_streak,
                    peak_day=excluded.peak_day,
                    peak_plays=excluded.peak_plays,
                    unique_songs=excluded.unique_songs,
                    unique_artists=excluded.unique_artists,
                    discovered_songs=excluded.discovered_songs,
                    discovered_artists=excluded.discovered_artists,
                    time_series_day=excluded.time_series_day,
                    time_series_week=excluded.time_series_week,
                    time_series_month=excluded.time_series_month,
                    top_songs=excluded.top_songs,
                    top_artists=excluded.top_artists,
                    top_albums=excluded.top_albums,
                    discovered_songs_list=excluded.discovered_songs_list,
                    discovered_artists_list=excluded.discovered_artists_list,
                    discovered_albums_list=excluded.discovered_albums_list
                """,
                {**data, "username": username, "year": year}
            )
        return True
