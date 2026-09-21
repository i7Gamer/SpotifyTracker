# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations



class MilestoneQueries:
    """MilestoneQueries: per-user achievement-milestone data access, mixed into
    Repository. The detection/seeding that writes these rows lives in
    services/milestones.py; the user_milestones table + users.milestones_baseline_at
    are defined in Database/db.py's SCHEMA."""

    def getMilestoneBaselineAt(self, username: str) -> float | None:
        """Timestamp of `username`'s first milestone-detection pass, or None if
        they've never been baselined yet (the feature hasn't reached them). A
        set baseline is what tells detection to notify (seen=0) on new crossings
        instead of silently seeding already-achieved milestones."""
        row = self._conn().execute(
            "SELECT milestones_baseline_at FROM users WHERE username=?", (username,)
        ).fetchone()
        return row["milestones_baseline_at"] if row else None

    def setMilestoneBaselineAt(self, username: str, ts: float) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET milestones_baseline_at=? WHERE username=?", (ts, username)
            )

    def hasThresholdMilestone(self, username: str, kind: str, threshold: int) -> bool:
        """Whether a threshold milestone (plays/listen_time/streak) at this
        exact level was already recorded - the dedup guard so each level only
        notifies once."""
        row = self._conn().execute(
            "SELECT 1 FROM user_milestones WHERE username=? AND kind=? AND threshold=? LIMIT 1",
            (username, kind, threshold),
        ).fetchone()
        return row is not None

    def getLatestMilestone(self, username: str, kind: str) -> dict | None:
        """The most recently achieved milestone of `kind`, or None - lets
        top_artist detection compare the current #1 against the last one
        recorded."""
        row = self._conn().execute(
            "SELECT id, kind, threshold, detail, achieved_at, seen FROM user_milestones "
            "WHERE username=? AND kind=? ORDER BY achieved_at DESC, id DESC LIMIT 1",
            (username, kind),
        ).fetchone()
        return dict(row) if row else None

    def recordMilestone(self, username: str, kind: str, threshold: int, detail: str | None,
                        achievedAt: float, seen: bool) -> int:
        """Insert one milestone row, returning its id. Threshold-kind dedup is
        the caller's responsibility (hasThresholdMilestone) - this is a plain
        insert so top_artist can record a fresh row every time the #1 changes."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "INSERT INTO user_milestones (username, kind, threshold, detail, achieved_at, seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, kind, threshold, detail, achievedAt, 1 if seen else 0),
            )
        return cursor.lastrowid

    def getNthPlayTimestamp(self, username: str, n: int) -> float | None:
        """played_at of `username`'s n-th non-skip play in chronological order
        (1-based), or None with fewer than n plays - the data-derived date a
        lifetime play-count milestone was actually reached (see
        services/milestones.py recalculateMilestoneDates)."""
        if n < 1:
            return None
        row = self._conn().execute(
            "SELECT played_at FROM plays WHERE username=? AND is_skip=0 "
            "ORDER BY played_at, id LIMIT 1 OFFSET ?",
            (username, n - 1),
        ).fetchone()
        return row["played_at"] if row else None

    def getListenTimeCrossingTimestamp(self, username: str, targetMs: int) -> float | None:
        """played_at of the non-skip play at which `username`'s cumulative
        time_played first reaches `targetMs`, or None if their lifetime total
        never does - the data-derived date a listen-time milestone was reached
        (see services/milestones.py recalculateMilestoneDates)."""
        row = self._conn().execute(
            "SELECT played_at FROM ("
            " SELECT played_at, id, SUM(time_played) OVER (ORDER BY played_at, id) AS cum_ms"
            " FROM plays WHERE username=? AND is_skip=0"
            ") WHERE cum_ms >= ? ORDER BY played_at, id LIMIT 1",
            (username, targetMs),
        ).fetchone()
        return row["played_at"] if row else None

    def updateMilestoneAchievedAt(self, milestoneId: int, achievedAt: float) -> None:
        """Rewrites one milestone row's achieved date, leaving seen untouched -
        date recalculation must never re-badge an acknowledged milestone."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE user_milestones SET achieved_at=? WHERE id=?", (achievedAt, milestoneId)
            )

    def deleteMilestone(self, milestoneId: int) -> None:
        """Removes one milestone row - the post-import prune for rows whose
        milestone the rewritten play history no longer supports (see
        services/milestones.py recalculateMilestoneDates' removeUnsupported)."""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM user_milestones WHERE id=?", (milestoneId,))

    def getMilestoneUsernames(self) -> list[str]:
        """Every user with at least one milestone row - who migrate1_35_0's
        achieved-at recalculation needs to visit."""
        rows = self._conn().execute(
            "SELECT DISTINCT username FROM user_milestones ORDER BY username"
        ).fetchall()
        return [r["username"] for r in rows]

    def getUnseenMilestoneCount(self, username: str) -> int:
        """How many milestones this user hasn't acknowledged (by opening the
        dashboard, where the Milestones card shows them) - the topbar badge
        count. Runs on every template render (see _injectMilestoneStatus in
        app.py), hence the (username, seen) index."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM user_milestones WHERE username=? AND seen=0", (username,)
        ).fetchone()
        return row["c"]

    def getMilestonesForUser(self, username: str, limit: int | None = None) -> list[dict]:
        """This user's milestones, newest first, for the dashboard's Milestones
        card."""
        sql = ("SELECT id, kind, threshold, detail, achieved_at, seen FROM user_milestones "
               "WHERE username=? ORDER BY achieved_at DESC, id DESC")
        params: list = [username]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def markMilestonesSeen(self, username: str) -> None:
        """Clears the "new milestone" badge - called when `username` opens the
        dashboard (routes/charts.py's dashboardIndex), where the Milestones card
        actually shows them. That view primes the badge count first (see
        primeMilestoneBadge), so the badge still renders on the page that
        acknowledges it."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE user_milestones SET seen=1 WHERE username=? AND seen=0", (username,)
            )
