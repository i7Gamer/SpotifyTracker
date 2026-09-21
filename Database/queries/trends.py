# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import time

from Database.queries._base import FULL_PLAY_PREDICATE, PERCENT_DIVISOR, SECONDS_PER_DAY
from config import (
    TREND_OBSESSION_DAYS,
    TREND_OBSESSION_MIN_PLAYS,
    TREND_REDISCOVERY_RECENT_DAYS,
    TREND_REDISCOVERY_GAP_DAYS,
    TREND_REDISCOVERY_FALLBACK_GAP_DAYS,
    TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS,
    TREND_FRESH_FIND_DAYS,
    TREND_FRESH_FIND_MIN_PLAYS,
    TREND_FORGOTTEN_GAP_DAYS,
    TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS,
)


class TrendQueries:
    """TrendQueries: SQL queries for Dashboard Obsession, Rediscovery, Fresh
    Find, and Forgotten Favorites.

    All of them exclude skips (plays.is_skip=1): a track you keep skipping is
    not an obsession, a rediscovery, a find, or a forgotten favorite. Forgotten
    Favorite additionally requires each counted play to be a full listen
    (completion ratio) - see getDashboardTrendsRaw.

    Rediscovery and Fresh Find share one dashboard slot: the card shows a
    rediscovery when there is one and the newest arrival otherwise, so a user
    whose listening has no comebacks in it still gets an insight there."""

    def getDashboardTrendsRaw(self, username: str, now_ts: float | None = None) -> dict[str, dict | None]:
        if now_ts is None:
            now_ts = time.time()

        conn = self._conn()

        # 1. Obsession. One statement, run once at the play floor - see
        # TREND_OBSESSION_MIN_PLAYS for why the higher first pass went.
        obsession_cutoff = now_ts - (TREND_OBSESSION_DAYS * SECONDS_PER_DAY)
        obsessionQuery = """
            SELECT COALESCE(t.canonical_id, t.id) AS track_id, COUNT(*) as recent_count, SUM(time_played) as recent_ms
            FROM plays JOIN tracks t ON t.id = plays.track_id
            WHERE username = ? AND is_skip = 0 AND played_at >= ?
            -- the expression, not `track_id`: that name also belongs to a real
            -- column on plays, and SQLite resolves GROUP BY to the COLUMN over
            -- the output alias - which grouped per release while the SELECT
            -- reported the canonical, and the counts came out per release
            GROUP BY COALESCE(t.canonical_id, t.id)
            HAVING recent_count >= ?
            ORDER BY recent_count DESC, recent_ms DESC
            LIMIT 1
            """

        obsession_row = conn.execute(
            obsessionQuery, (username, obsession_cutoff, TREND_OBSESSION_MIN_PLAYS)).fetchone()

        # 2. Rediscovery. One statement, run once per gap tier. Unlike the play
        # floors the cards either side of it used to retry on, a shorter gap
        # CAN surface a different track (the ordering is by count, the tier is
        # a date), so the retry here is not redundant.
        rediscovery_recent_cutoff = now_ts - (TREND_REDISCOVERY_RECENT_DAYS * SECONDS_PER_DAY)
        rediscoveryQuery = """
            SELECT COALESCE(t.canonical_id, t.id) AS track_id,
                   COUNT(CASE WHEN played_at >= ? THEN 1 END) as recent_count,
                   COUNT(CASE WHEN played_at < ? THEN 1 END) as old_count,
                   MAX(CASE WHEN played_at < ? THEN played_at END) as max_old_played_at
            FROM plays JOIN tracks t ON t.id = plays.track_id
            WHERE username = ? AND is_skip = 0
              -- Only tracks with a recent real play can survive the
              -- `recent_count >= 1` below, so aggregating any others is wasted
              -- work. Restricting the candidates first turns a GROUP BY over
              -- the user's whole history into one over the handful of tracks
              -- they have actually played lately (~180ms -> ~0.5ms on a real
              -- library, on the landing page). The subquery repeats the
              -- is_skip = 0 filter deliberately: a track whose only recent
              -- plays are skips has recent_count 0 and must stay excluded, the
              -- same as before (see test_rediscovery_excludes_skips).
              -- Canonical on BOTH sides. Narrowing by the PLAYED id while
              -- grouping by the canonical splits a merge group across this
              -- boundary: the recently-played release passes, the other
              -- release's rows are dropped from the aggregate, and old_count /
              -- max_old_played_at lie about the song's history. A song
              -- rediscovered VIA a different release is still a rediscovery.
              AND COALESCE(t.canonical_id, t.id) IN (
                  SELECT COALESCE(t2.canonical_id, t2.id) FROM plays p2
                  JOIN tracks t2 ON t2.id = p2.track_id
                  WHERE p2.username = ? AND p2.is_skip = 0 AND p2.played_at >= ?
              )
            GROUP BY COALESCE(t.canonical_id, t.id)   -- see the note above
            HAVING recent_count >= 1
               AND old_count >= ?
               AND max_old_played_at IS NOT NULL
               AND max_old_played_at <= ?
            ORDER BY recent_count DESC, old_count DESC
            LIMIT 1
            """

        def rediscoveryWithGap(gapDays: int):
            return conn.execute(
                rediscoveryQuery,
                (
                    rediscovery_recent_cutoff,
                    rediscovery_recent_cutoff,
                    rediscovery_recent_cutoff,
                    username,
                    username,
                    rediscovery_recent_cutoff,
                    TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS,
                    now_ts - (gapDays * SECONDS_PER_DAY),
                ),
            ).fetchone()

        rediscovery_row = None
        # Longest gap first: a genuine 180-day comeback must not be displaced by
        # a 30-day one that happens to have been played more this week (within a
        # tier the ordering is by recent_count, so a single flat query would).
        for gapDays in (TREND_REDISCOVERY_GAP_DAYS, *TREND_REDISCOVERY_FALLBACK_GAP_DAYS):
            rediscovery_row = rediscoveryWithGap(gapDays)
            if rediscovery_row:
                break

        # 2b. Fresh Find - the same card's fallback insight, so it is only asked
        # for when no gap tier matched. The newest arrival: a track whose
        # all-time first real listen is inside the window, ranked by how often it
        # has been played since.
        fresh_find_row = None
        if not rediscovery_row:
            fresh_find_row = conn.execute(
                """
                SELECT COALESCE(t.canonical_id, t.id) AS track_id,
                       COUNT(*) as play_count,
                       MIN(played_at) as first_played_at,
                       SUM(time_played) as total_ms
                FROM plays JOIN tracks t ON t.id = plays.track_id
                WHERE username = ? AND is_skip = 0
                  -- Same candidate narrowing as the rediscovery query above,
                  -- lossless for the same kind of reason: a track whose
                  -- all-time first real play is inside the window necessarily
                  -- has a real play inside the window. Note the aggregate
                  -- itself is deliberately NOT windowed - MIN(played_at) has to
                  -- see the track's whole history, or every track played this
                  -- week would look new.
                  -- Canonical on both sides, like the rediscovery narrowing
                  -- above - and here the per-release version was not merely an
                  -- undercount but a FALSE POSITIVE: dropping the other
                  -- release's rows leaves MIN(played_at) seeing only this
                  -- week's plays, so a song first heard half a year ago reads
                  -- as discovered days ago. First heard is a property of the
                  -- SONG, which is exactly what the merge group is.
                  AND COALESCE(t.canonical_id, t.id) IN (
                      SELECT COALESCE(t2.canonical_id, t2.id) FROM plays p2
                      JOIN tracks t2 ON t2.id = p2.track_id
                      WHERE p2.username = ? AND p2.is_skip = 0 AND p2.played_at >= ?
                  )
                  -- The obsession is usually a new track played hard, which
                  -- would put the same song in two adjacent cards. Compared on
                  -- canonicals: the obsession's track_id is a canonical now, so
                  -- a played-id comparison would only shield one release of the
                  -- group. `IS NOT` and not `!=` because the parameter is NULL
                  -- when the user has no obsession, and `!= NULL` excludes
                  -- everything.
                  AND COALESCE(t.canonical_id, t.id) IS NOT ?
                GROUP BY COALESCE(t.canonical_id, t.id)   -- the expression; see the note above
                HAVING first_played_at >= ? AND play_count >= ?
                ORDER BY play_count DESC, total_ms DESC
                LIMIT 1
                """,
                (
                    username,
                    username,
                    now_ts - (TREND_FRESH_FIND_DAYS * SECONDS_PER_DAY),
                    obsession_row["track_id"] if obsession_row else None,
                    now_ts - (TREND_FRESH_FIND_DAYS * SECONDS_PER_DAY),
                    TREND_FRESH_FIND_MIN_PLAYS,
                ),
            ).fetchone()

        # 3. Forgotten Favorite - only counts full listens (is_skip=0 and at/over
        # the admin's completion-complete percent, same boundary getCompletionStats
        # uses), so a track that was merely started/skipped a lot never reads as a
        # forgotten favorite - a favorite has to have been actually heard.
        forgotten_gap_cutoff = now_ts - (TREND_FORGOTTEN_GAP_DAYS * SECONDS_PER_DAY)
        completion_ratio = self.getCompletionCompletePercent() / PERCENT_DIVISOR
        # One statement, run once at the play floor (the higher first pass it
        # used to make could only return the same row - see
        # TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS). The completion test is the
        # shared FULL_PLAY_PREDICATE rather than its own copy of the same SQL,
        # so a change to what "a full listen" means reaches here too.
        forgottenQuery = f"""
            SELECT COALESCE(t.canonical_id, t.id) as track_id, COUNT(*) as total_plays,
                   MAX(p.played_at) as last_played_at
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            WHERE p.username = ?
              AND p.is_skip = 0
              AND ({FULL_PLAY_PREDICATE.format(plays="p", track="t")})
            GROUP BY COALESCE(t.canonical_id, t.id)   -- the expression; see the note above
            HAVING last_played_at <= ? AND total_plays >= ?
            ORDER BY total_plays DESC, last_played_at DESC
            LIMIT 1
            """

        forgotten_row = conn.execute(
            forgottenQuery,
            (username, completion_ratio, forgotten_gap_cutoff, TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS),
        ).fetchone()

        return {
            "obsession": dict(obsession_row) if obsession_row else None,
            "rediscovery": dict(rediscovery_row) if rediscovery_row else None,
            "freshFind": dict(fresh_find_row) if fresh_find_row else None,
            "forgotten": dict(forgotten_row) if forgotten_row else None,
        }
