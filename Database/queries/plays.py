# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from Database.queries._base import (
    ALBUM_SORT_COLUMNS,
    ARTIST_SORT_COLUMNS,
    BEHAVIORAL_COLUMNS,
    PERCENT_DIVISOR,
    PLAY_BUCKET_SECONDS,
    SKIP_RATE_PRIOR_WEIGHT,
    SONG_SORT_COLUMNS,
    time,
)

# The created_reason prefixes a play is stored under for a live listener catch
# vs. a Web API backfill recovery (see appendTrackData and
# WEB_API_BACKFILL_SOURCE in Database/db.py). They would sit next to that
# constant, but this module doesn't import Database.db directly (only
# Database.queries._base does, on its own already-established import path),
# and getPlaySourceCountsByUser below is their only user - see its docstring.
LIVE_PLAY_REASON_PREFIX = "listener_play"
BACKFILL_PLAY_REASON_PREFIX = "web_api_backfill_play"


class PlayQueries:
    """PlayQueries: plays data-access methods, mixed into Repository."""

    # ---- Per-user: plays (play history) -----------------------------------------

    @staticmethod
    def _behavioralSetSql() -> str:
        """`col = COALESCE(?, col)` for every behavioral column: a None in the
        incoming row never clobbers a stored value, so a thinner export can
        only ever add detail."""
        return ", ".join(f"{column} = COALESCE(?, {column})" for column in BEHAVIORAL_COLUMNS)

    def correctPlay(self, playId: int, playedAt: float, timePlayed: int, isSkip: int,
                     extrasValues: tuple) -> None:
        """Rewrite one play from a more accurate import row.

        Raises sqlite3.IntegrityError when moving played_at would collide with
        an existing (username, track_id, played_at) row - the caller decides
        whether to leave the row uncorrected, since that is an import-policy
        question, not a storage one."""
        self._conn().execute(
            f"UPDATE plays SET played_at = ?, time_played = ?, is_skip = ?, {self._behavioralSetSql()}"
            " WHERE id = ?",
            (playedAt, timePlayed, isSkip, *extrasValues, playId),
        )

    def enrichPlayBehavioralColumns(self, playId: int, extrasValues: tuple) -> None:
        """Backfill one play's behavioral columns from an import that carries
        metadata the stored row lacks. The play itself is unchanged."""
        self._conn().execute(
            f"UPDATE plays SET {self._behavioralSetSql()} WHERE id = ?",
            (*extrasValues, playId),
        )

    def insertPlay(self, username: str, trackId: str, playedAt: float, timePlayed: int,
                   playedFrom: str | None = None, created_reason: str | None = None,
                   extras: dict | None = None, is_skip: int = 0) -> bool:
        """Returns True if a new row was inserted, False if this exact
        (username, trackId, playedAt) play was already recorded (updates
        time_played if different, and enriches behavioral columns from
        `extras` - a non-None extras value wins over the stored one, a None
        value never clobbers).
        If created_reason is provided, it's only set on INSERT (never updated
        on an existing play, matching upsertTrack()'s semantics).

        is_skip: 0 for a real play (the default), 1 for a skip. The write paths
        that classify (the importer, and the listener/backfill via
        appendMetadata) compute it from the current threshold and pass it here;
        direct callers default to a real play, and recomputeSkipFlags()
        reclassifies every row when the admin changes the threshold. It's also
        written on the update path so a re-recorded play tracks the supplied
        value.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        existing = conn.execute(
            f"SELECT id, time_played, played_from, is_skip, {behavioralSelect} FROM plays WHERE username=? AND track_id=? AND played_at=?",
            (username, trackId, playedAt)
        ).fetchone()

        if existing:
            extras = extras or {}
            playedFromChanged = playedFrom is not None and playedFrom != existing["played_from"]
            behavioralChanged = any(
                extras.get(column) is not None and extras.get(column) != existing[column]
                for column in BEHAVIORAL_COLUMNS
            )
            if (existing["time_played"] != timePlayed or playedFromChanged
                    or existing["is_skip"] != is_skip or behavioralChanged):
                behavioralSet = ", ".join(f"{column} = COALESCE(?, {column})" for column in BEHAVIORAL_COLUMNS)
                conn.execute(
                    f"UPDATE plays SET time_played = ?, is_skip = ?, played_from = COALESCE(?, played_from), {behavioralSet} WHERE id = ?",
                    (timePlayed, is_skip, playedFrom, *[extras.get(column) for column in BEHAVIORAL_COLUMNS], existing["id"])
                )
            return False

        createdAt = time.time() if created_reason else None
        extras = extras or {}
        behavioralInsert = ", ".join(BEHAVIORAL_COLUMNS)
        behavioralPlaceholders = ", ".join("?" for _ in BEHAVIORAL_COLUMNS)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO plays (username, track_id, played_at, time_played, played_from, created_at, created_reason, is_skip, {behavioralInsert}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {behavioralPlaceholders})",
            (username, trackId, playedAt, timePlayed, playedFrom, createdAt, created_reason, is_skip,
             *[extras.get(column) for column in BEHAVIORAL_COLUMNS]),
        )
        return cur.rowcount > 0

    def deletePlay(self, username: str, trackId: str, playedAt: float) -> bool:
        """Delete one specific play - the exact (username, trackId, playedAt)
        tuple insertPlay() already treats as unique. Returns True if a row was
        deleted.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM plays WHERE username=? AND track_id=? AND played_at=?",
            (username, trackId, playedAt),
        )
        return cur.rowcount > 0

    def hasPlayNearTime(self, username: str, trackId: str, playedAt: float, toleranceSeconds: float,
                        listenerEndToleranceSeconds: float | None = None,
                        skipToleranceSeconds: float | None = None) -> bool:
        """True if a play for this exact track already exists for this user
        within toleranceSeconds of playedAt (inclusive both directions).
        Reuses idx_plays_user_track. See Database.appendTrackData for why this
        is a wide, defense-in-depth guard applied only to Web API backfill
        inserts, not the live listener's own insert path.

        listenerEndToleranceSeconds additionally matches a LISTENER row whose
        created_at sits within that (much tighter) tolerance of playedAt: the
        listener inserts at the track-change moment, so its created_at is the
        observed end of the play, pauses included - which the duration-based
        window above cannot cover, since a mid-track pause stretches
        start-to-end by an unbounded amount. Listener rows only: any other
        source's created_at is an import/poll moment, not a play end.

        Both of those arms are is_skip=0, which near-time matching has been
        since skips lived in their own table: a backfill row must never dedup
        against, or claim/correct, a merged skip row. That filter cannot hold
        for this caller, though - the Web API reports no ms_played, so a
        backfill row stamps the track's whole duration and is is_skip=0 by
        construction, and a listen the listener classified as a SKIP was
        therefore invisible to every arm and got re-added as a full play.
        skipToleranceSeconds is the answer: a third arm matching a skip's
        played_at, kept far tighter than the duration window because at that
        distance a skip followed by a genuine replay is the likelier reading
        (see Database.BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS). played_at only -
        a skip's created_at is when the user skipped AWAY, not an end the feed
        still owes us, the same rule getPlaysWithSourceInRange enforces. Not
        restricted by source, unlike the end arm: that one needs created_at to
        MEAN a play end, while played_at is recorded honestly by all of them."""
        conn = self._conn()
        timeMatch = "played_at BETWEEN ? AND ?"
        params: list = [playedAt - toleranceSeconds, playedAt + toleranceSeconds]
        if listenerEndToleranceSeconds is not None:
            timeMatch += " OR (created_reason LIKE 'listener_play%' AND created_at BETWEEN ? AND ?)"
            params += [playedAt - listenerEndToleranceSeconds, playedAt + listenerEndToleranceSeconds]
        clauses = f"(is_skip=0 AND ({timeMatch}))"
        if skipToleranceSeconds is not None:
            clauses += " OR (is_skip=1 AND played_at BETWEEN ? AND ?)"
            params += [playedAt - skipToleranceSeconds, playedAt + skipToleranceSeconds]
        row = conn.execute(
            f"SELECT 1 FROM plays WHERE username=? AND track_id=? AND ({clauses}) LIMIT 1",
            (username, trackId, *params),
        ).fetchone()
        return row is not None

    def getRecentlyRecordedTrackIds(self, username: str, trackIds: list[str],
                                    sinceSeconds: float) -> set[str]:
        """Which of `trackIds` this user has any play for in the last
        `sinceSeconds`, counting a play on any member of the same
        duplicate-merge group.

        Backs the listener's missed-play cross-check, so it answers "did we
        record this at all", not "did we record this specific play" - is_skip
        rows count, since a skip is still a play that was captured. Batched into
        one query because the caller asks about a whole connect-state queue at
        once, on a polling loop.

        The merge group matters because Spotify hands out different ids for the
        same recording over time (market relinking, re-releases), so the id in
        today's connect-state queue and the id last week's play was recorded
        under can differ - and comparing raw track_id then reports a play the
        database demonstrably has. Live 2026-09-12..19: 49 of 285 flagged tracks
        had a played group sibling, 30 with the flagged id as the duplicate and
        19 as the canonical, so reading the asked track's own canonical_id is
        not enough in either direction. Both sides are resolved to
        COALESCE(canonical_id, id) instead, the same group key the rest of the
        read path uses.

        Driven from the play side (username + played_at, idx_plays_user_time),
        then joined out to tracks by primary key - never from tracks, which
        carries no canonical_id index and must not grow one for this (see
        findRecentDuplicateCandidates). Measured on a copy of the live database,
        20 queue ids over a 7-day window: 0.02ms -> 0.77ms per call, all index
        searches, no scan. The caller only reaches here for candidates that
        have already survived the grace window, and settles the answer after.

        The ASKED id is returned, not the group's canonical one: the caller
        matches the result against the queue's own uris."""
        if not trackIds:
            return set()
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        rows = conn.execute(
            f"SELECT DISTINCT asked.id AS track_id FROM plays p "
            f"JOIN tracks played ON played.id = p.track_id "
            f"JOIN tracks asked ON COALESCE(asked.canonical_id, asked.id) "
            f"                   = COALESCE(played.canonical_id, played.id) "
            f"WHERE p.username=? AND p.played_at >= ? AND asked.id IN ({placeholders})",
            (username, time.time() - sinceSeconds, *trackIds),
        ).fetchall()
        return {row["track_id"] for row in rows}

    def getTrackPlayTimesInRange(self, username: str, startTs: float,
                                 endTs: float) -> list[tuple[str, float, float | None]]:
        """Every (track_id, played_at, listener_created_at) this user has in
        the closed [startTs, endTs] window.

        Backs the Web API backfill's duplicate check (see _checkWebApiBackfill):
        the listener's in-memory caches only cover the current listener object's
        lifetime, so after a reconnect the database is the only thing that knows
        which of the last 50 Web API plays were already recorded. Like
        getRecentlyRecordedTrackIds it answers "did we record this at all", so
        is_skip rows count - re-announcing a recorded skip as missing is the
        same false positive. One range query per poll, rather than the
        page-sized set of point lookups the insert guard answers one by one.

        A play is reported under its own track id AND under every other id
        denoting the same recording (see _sameRecordingTrackIds), because one
        listen reaches us under two ids whenever the connect player_state and
        the Web API disagree about which release is playing - the caller keys
        its dedup by id, so without this it recorded that listen twice.

        The track id travels WITH the timestamp because the caller's dedup is
        only sound per track: this window spans the whole API page (hours,
        typically hundreds of rows, skip bursts seconds apart), and comparing
        timestamps alone let any one of them answer for a different track's
        genuine gap - which under gapless playback is not a coincidence but the
        norm, since a missing track's derived start equals its predecessor's
        recorded end.

        The third element is the row's created_at, passed through for real
        LISTENER plays only: the listener inserts a play at the track-change
        moment, so its created_at IS the observed end of the play, pauses
        included - the anchor the caller's end-time dedup arm compares
        against. Any other source's created_at is an import/poll moment,
        meaningless as a play end, and comes through as None. So is a SKIP's:
        that stamp is when the user skipped away, not the end of a play the
        feed still owes us, and letting it anchor the arm let a skip suppress
        the backfill of a real play seconds later. Note this nulls the ANCHOR
        only - a skip's played_at still comes through, and the insert guard
        behind this check matches it directly on a tight tolerance (see
        hasPlayNearTime's skipToleranceSeconds). Real listener plays are also
        matched INTO the window by that created_at, not just by played_at: a
        paused play can start more than one track-length before its end, which
        is exactly when the played_at-only window would miss the row the
        end-time arm needs."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT track_id, played_at, "
            "CASE WHEN created_reason LIKE 'listener_play%' AND is_skip=0 THEN created_at "
            "ELSE NULL END AS listener_created_at "
            "FROM plays WHERE username=? AND (played_at BETWEEN ? AND ? "
            "OR (created_reason LIKE 'listener_play%' AND is_skip=0 AND created_at BETWEEN ? AND ?))",
            (username, startTs, endTs, startTs, endTs),
        ).fetchall()
        recorded = [(row["track_id"], row["played_at"], row["listener_created_at"]) for row in rows]
        aliases = self._sameRecordingTrackIds({row["track_id"] for row in rows})
        return recorded + [(aliasId, playedAt, listenerCreatedAt)
                           for trackId, playedAt, listenerCreatedAt in recorded
                           for aliasId in aliases.get(trackId, ())]

    def _sameRecordingTrackIds(self, trackIds: set[str]) -> dict[str, set[str]]:
        """{track id -> the OTHER ids denoting the same recording}, for the
        caller above: one listen can reach us under two Spotify track ids,
        because the connect player_state and the Web API's recently-played
        endpoint pick different releases of it. Keyed by track id, the backfill
        dedup then saw the listener's row and the Web API's copy of the same
        listen as two different plays and recorded it twice (live, 2026-08-17:
        4 of the last 307 plays, each pair one track-length apart with
        identical duration_ms).

        Three signals, and only three - a play wrongly suppressed here is lost
        for good (the item never reaches the insert guard, and every later poll
        collides identically), so each has to mean "same master", not "probably
        related":
          - the same merge group: a decided fact, in either tier;
          - the same non-empty ISRC: one master, two releases - what the
            automatic merge tier itself merges on, available here before that
            (daily) tier has run;
          - the same title, primary artist and duration TO THE MILLISECOND: all
            that is left for an id minted minutes ago, whose ISRC has not been
            fetched yet and which no merge tier can have seen. Deliberately
            stricter than the merge review queue's seconds-wide tolerance,
            which exists to ask a person precisely because that width also
            catches re-recordings.

        One scan of tracks per backfill poll (every 15 minutes per user);
        tracks carries no index on name/isrc/canonical_id, and adding three to
        serve this would cost every write for it."""
        if not trackIds:
            return {}
        conn = self._conn()
        seeds = conn.execute(
            f"""
            SELECT t.id, t.name, t.duration_ms, t.isrc,
                   COALESCE(t.canonical_id, t.id) AS group_id, ta.artist_id AS primary_artist
            FROM tracks t
            LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            WHERE t.id IN ({",".join("?" for _ in trackIds)})
            """,
            list(trackIds),
        ).fetchall()
        if not seeds:
            return {}

        def recordingKey(row):
            """None whenever any part is unknown - two rows agreeing on "we
            don't know" is not evidence that they are the same recording."""
            if not row["name"] or not row["duration_ms"] or not row["primary_artist"]:
                return None
            return (row["name"], row["duration_ms"], row["primary_artist"])

        groupIds = {row["group_id"] for row in seeds}
        isrcs = {row["isrc"] for row in seeds if row["isrc"]}
        names = {row["name"] for row in seeds if recordingKey(row)}
        clauses = [f"COALESCE(t.canonical_id, t.id) IN ({','.join('?' for _ in groupIds)})"]
        params = list(groupIds)
        if isrcs:
            clauses.append(f"(t.isrc <> '' AND t.isrc IN ({','.join('?' for _ in isrcs)}))")
            params += list(isrcs)
        if names:
            clauses.append(f"t.name IN ({','.join('?' for _ in names)})")
            params += list(names)
        candidates = conn.execute(
            f"""
            SELECT t.id, t.name, t.duration_ms, t.isrc,
                   COALESCE(t.canonical_id, t.id) AS group_id, ta.artist_id AS primary_artist
            FROM tracks t
            LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            WHERE {" OR ".join(clauses)}
            """,
            params,
        ).fetchall()

        byGroup: dict = {}
        byIsrc: dict = {}
        byRecording: dict = {}
        for row in candidates:
            byGroup.setdefault(row["group_id"], set()).add(row["id"])
            if row["isrc"]:
                byIsrc.setdefault(row["isrc"], set()).add(row["id"])
            key = recordingKey(row)
            if key:
                byRecording.setdefault(key, set()).add(row["id"])

        result = {}
        for row in seeds:
            same = set(byGroup.get(row["group_id"], ()))
            if row["isrc"]:
                same |= byIsrc.get(row["isrc"], set())
            key = recordingKey(row)
            if key:
                same |= byRecording.get(key, set())
            same.discard(row["id"])
            if same:
                result[row["id"]] = same
        return result

    def getPlaysNearTime(self, username: str, trackId: str, playedAt: float, toleranceSeconds: float) -> list[dict]:
        """Return all plays for this exact track already existing for this user
        within toleranceSeconds of playedAt (inclusive both directions).
        Used during imports to detect duplicates and decide whether to update;
        carries the behavioral columns so the import can enrich NULLs in place."""
        conn = self._conn()
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        # is_skip=0: only real plays are correction/dedup candidates (see hasPlayNearTime).
        rows = conn.execute(
            f"SELECT id, played_at, time_played, is_skip, {behavioralSelect} FROM plays "
            f"WHERE username=? AND track_id=? AND played_at BETWEEN ? AND ? AND is_skip=0",
            (username, trackId, playedAt - toleranceSeconds, playedAt + toleranceSeconds),
        ).fetchall()
        return [dict(row) for row in rows]

    def getSkipsNearTime(self, username: str, trackId: str, playedAt: float,
                          toleranceSeconds: float) -> list[dict]:
        """Skip rows (is_skip=1) for this track within toleranceSeconds of
        playedAt - the skip-side counterpart to getPlaysNearTime.

        Deliberately scoped to is_skip=1 in both directions: a skip must never
        dedup against, claim or correct a real play row, and a real play must
        never dedup against a skip (see hasPlayNearTime). This exists because
        the live listener and a history import both record the same physical
        sub-threshold event, and their played_at can differ by seconds
        (Spotify's documented start-vs-end ambiguity), so plays' UNIQUE
        constraint - which needs an exact timestamp match - let one skip land
        twice and inflate skip counts."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, played_at FROM plays "
            "WHERE username=? AND track_id=? AND played_at BETWEEN ? AND ? AND is_skip=1",
            (username, trackId, playedAt - toleranceSeconds, playedAt + toleranceSeconds),
        ).fetchall()
        return [dict(row) for row in rows]

    def deletePlaysInRange(self, username: str, startTs: float, endTs: float) -> int:
        """Delete every real play (is_skip=0) of this user whose played_at falls
        inside the closed [startTs, endTs] window - the overwrite-import wipe.
        Skips in the same range are removed by deleteSkipsInRange, so the two
        counts stay separately reportable. Returns the number of rows removed.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM plays WHERE username=? AND played_at BETWEEN ? AND ? AND is_skip=0",
            (username, startTs, endTs),
        )
        return cur.rowcount

    def deleteSkipsInRange(self, username: str, startTs: float, endTs: float) -> int:
        """Skip counterpart of deletePlaysInRange(): removes the is_skip=1 rows
        in range (skips now live in plays, not a separate play_skips table).

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM plays WHERE username=? AND played_at BETWEEN ? AND ? AND is_skip=1",
            (username, startTs, endTs),
        )
        return cur.rowcount

    def deleteZeroDurationPlays(self) -> int:
        """Remove real plays with zero (or negative) recorded listening time,
        across every user - leftover skip/error events that older importer
        versions recorded as real plays before the importer started filtering
        them out at import time. Returns the number removed.

        Since migrate1_32_0 a merged skip row can legitimately be 0ms and must
        NOT be deleted, so the delete is scoped to is_skip=0 WHEN that column
        exists. But this method's only callers are migrate1_7_0 / migrate1_9_0,
        which run BEFORE migrate1_32_0 adds plays.is_skip: on a genuinely old
        (<=1.9) database the column doesn't exist yet, and CREATE TABLE IF NOT
        EXISTS on connect never adds a column to an existing table (the same
        reason the SCHEMA forbids indexing is_skip - see db.py). Referencing
        is_skip unconditionally would raise "no such column: is_skip" and abort
        the upgrade, so the guard is added only when the column is present.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        hasIsSkip = any(row["name"] == "is_skip"
                        for row in conn.execute("PRAGMA table_info(plays)").fetchall())
        skipClause = " AND is_skip = 0" if hasIsSkip else ""
        cur = conn.execute(f"DELETE FROM plays WHERE time_played <= 0{skipClause}")
        return cur.rowcount

    # `fullPlaysOnly` is the "Full plays only" filter the Top pages and /history
    # both offer: a COMPLETION test (see _base.py's FULL_PLAY_PREDICATE), which
    # is why it is a separate parameter from `includeSkips` rather than folded
    # into it. /history drives both from one checkbox; the song detail page's
    # Show Skips toggle drives includeSkips alone.
    #
    # Two things to keep right when adding it to a query here. It needs the
    # tracks join, so the alias is `ft` and NOT `t` - _itemFilterClauses' album
    # subquery already names a `t`. And unlike the skip clause beside it, it
    # BINDS a parameter, so it has to be built in the position its `?` occupies
    # (see the SqlFragments docstring); the skip clause can sit anywhere only
    # because it binds nothing.

    def _playHistoryFilterParts(self, username: str, startTs: float | None, endTs: float | None,
                                trackId: str | None, artistId: str | None, albumId: str | None,
                                includeSkips: bool, trackIds: list[str] | None, fullPlaysOnly: bool
                                ) -> tuple[str, str, list]:
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)
        extraClauses += self._idSetClause(params, "track_id", trackIds)
        joinClause = ""
        if fullPlaysOnly:
            joinClause = self._tracksJoin(playsAlias="plays", trackAlias="ft")
            extraClauses += self._fullPlaysClause(params, playsAlias="plays", trackAlias="ft")
        skipClause = "" if includeSkips else " AND is_skip=0"
        return joinClause, f"{skipClause}{rangeClause}{extraClauses}", params

    def getPlaysCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                       trackId: str | None = None, artistId: str | None = None,
                       albumId: str | None = None, includeSkips: bool = False,
                       trackIds: list[str] | None = None, fullPlaysOnly: bool = False) -> int:
        conn = self._conn()
        joinClause, filterClause, params = self._playHistoryFilterParts(
            username, startTs, endTs, trackId, artistId, albumId, includeSkips, trackIds, fullPlaysOnly)
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM plays{joinClause} WHERE username=?{filterClause}",
            params,
        ).fetchone()
        return row["c"]

    def getPlaysNewestFirst(self, username: str, count: int | None = None, startIndex: int = 0,
                             startTs: float | None = None, endTs: float | None = None,
                             trackId: str | None = None, artistId: str | None = None,
                             albumId: str | None = None, includeSkips: bool = False,
                             trackIds: list[str] | None = None, fullPlaysOnly: bool = False) -> list[dict]:
        conn = self._conn()
        limit = -1 if count is None else count
        joinClause, filterClause, params = self._playHistoryFilterParts(
            username, startTs, endTs, trackId, artistId, albumId, includeSkips, trackIds, fullPlaysOnly)
        params += [limit, startIndex]
        #< ORDER BY is qualified whether or not the join is emitted: `plays` and
        #  `tracks` share id/created_at/created_reason, so a bare `id` is an
        #  ambiguous column under the join - and one statement shape is easier
        #  to trust than two
        rows = conn.execute(
            f"SELECT track_id, played_at, time_played, played_from, is_skip FROM plays{joinClause} "
            f"WHERE username=?{filterClause} "
            f"ORDER BY plays.played_at DESC, plays.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def getPlaysOldestFirst(self, username: str, count: int | None = None, startIndex: int = 0,
                             startTs: float | None = None, endTs: float | None = None,
                             trackId: str | None = None, artistId: str | None = None,
                             albumId: str | None = None, includeSkips: bool = False,
                             afterTs: float | None = None, afterId: int | None = None,
                             trackIds: list[str] | None = None,
                             fullPlaysOnly: bool = False) -> list[dict]:
        """`afterTs`/`afterId` page by position in the (played_at, id) order
        rather than by OFFSET - see iterExportEntries, which streams the whole
        history and must not skip rows if a concurrent delete shifts every
        later row left. `afterId` breaks a same-`played_at` tie (see
        _keysetAfterClause); omitting it keeps the old played_at >= afterTs
        behaviour for callers that don't need the composite key."""
        conn = self._conn()
        limit = -1 if count is None else count
        joinClause, filterClause, params = self._playHistoryFilterParts(
            username, startTs, endTs, trackId, artistId, albumId, includeSkips, trackIds,
            fullPlaysOnly)
        filterClause += self._keysetAfterClause(
            params, afterTs, afterId, tsColumn="plays.played_at", idColumn="plays.id")
        params += [limit, startIndex]
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        #< qualified ORDER BY: see getPlaysNewestFirst
        rows = conn.execute(
            f"SELECT plays.id AS play_id, track_id, played_at, time_played, played_from, is_skip, "
            f"{behavioralSelect} FROM plays{joinClause} "
            f"WHERE username=?{filterClause} "
            f"ORDER BY plays.played_at ASC, plays.id ASC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]


    def getSkipsOldestFirst(self, username: str, count: int | None = None, startIndex: int = 0,
                             afterTs: float | None = None, afterId: int | None = None) -> list[dict]:
        """Skip events (is_skip=1) oldest-first, shaped like getPlaysOldestFirst
        entries (skips carry no played_from - it comes back None). Feeds the JSON
        export so skips round-trip between instances. `afterTs`/`afterId`: see
        getPlaysOldestFirst."""
        conn = self._conn()
        limit = -1 if count is None else count
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        params = [username]
        afterClause = self._keysetAfterClause(params, afterTs, afterId)
        params += [limit, startIndex]
        rows = conn.execute(
            f"SELECT id AS play_id, track_id, played_at, time_played, is_skip, {behavioralSelect} FROM plays "
            f"WHERE username=? AND is_skip=1{afterClause} ORDER BY played_at ASC, id ASC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def getSkipCount(self, username: str, startTs: float | None = None, endTs: float | None = None) -> int:
        """Number of skip events (plays.is_skip=1) in range - the boundary is
        the admin-tunable skip threshold, materialized per row, so this is a
        plain count with no per-row duration check."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        row = conn.execute(f"SELECT COUNT(*) AS c FROM plays WHERE username=? AND is_skip=1{rangeClause}", params).fetchone()
        return row["c"]

    def getPlayAndSkipCountsByUser(self) -> dict[str, dict]:
        """All-time play (is_skip=0) and skip (is_skip=1) counts for every user,
        as {username: {"plays": int, "skips": int}}, in ONE grouped scan - the
        admin user table's old getPlaysCount()+getSkipCount() pair per user was
        2*N queries. Users with no plays are simply absent; the caller defaults
        them to 0."""
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT username,
                   SUM(CASE WHEN is_skip = 0 THEN 1 ELSE 0 END) AS plays,
                   SUM(CASE WHEN is_skip = 1 THEN 1 ELSE 0 END) AS skips
            FROM plays
            GROUP BY username
            """
        ).fetchall()
        return {r["username"]: {"plays": r["plays"], "skips": r["skips"]} for r in rows}

    def getPlaySourceCountsByUser(self, sinceTs: float, promptSeconds: int) -> dict[str, dict]:
        """Per-user play counts for the admin ledger's live-miss ratio, split
        into three buckets over plays with played_at >= sinceTs:
        - "live": caught by the listener directly (LIVE_PLAY_REASON_PREFIX).
        - "prompt_backfill": recovered by the Web API sweep
          (BACKFILL_PLAY_REASON_PREFIX) within `promptSeconds` of the play
          ending - a genuine missed live catch.
        - "late_backfill": recovered by the same sweep, but later - offline
          listening synced after the fact, not a live-path failure.
        Any other created_reason (history_import, legacy NULL rows, an
        unrecognised value) counts in none of the three: only live vs. prompt
        backfill speaks to whether the live catch is working, and late/import
        rows would only dilute that.

        A NULL created_at (rows that predate the column) falls out of BOTH
        backfill buckets on its own: `created_at - played_at` is NULL, so the
        `<=`/`>` comparison is NULL, and `TRUE AND NULL` is NULL rather than
        0 or 1 - SQLite's SUM() then simply ignores that row, which is the
        right behaviour (unknown promptness is not evidence either way).

        Users with no rows in the window are simply absent, same as
        getPlayAndSkipCountsByUser above - the caller decides how to treat an
        absent user."""
        conn = self._conn()
        livePattern = f"{LIVE_PLAY_REASON_PREFIX}%"
        backfillPattern = f"{BACKFILL_PLAY_REASON_PREFIX}%"
        rows = conn.execute(
            """
            SELECT username,
                   SUM(created_reason LIKE ?) AS live,
                   SUM(created_reason LIKE ? AND created_at - played_at <= ?) AS prompt_backfill,
                   SUM(created_reason LIKE ? AND created_at - played_at > ?) AS late_backfill
            FROM plays
            WHERE played_at >= ?
            GROUP BY username
            """,
            (livePattern, backfillPattern, promptSeconds, backfillPattern, promptSeconds, sinceTs),
        ).fetchall()
        return {
            r["username"]: {
                "live": r["live"] or 0,
                "prompt_backfill": r["prompt_backfill"] or 0,
                "late_backfill": r["late_backfill"] or 0,
            }
            for r in rows
        }

    def getPlaysWithSourceInRange(self, username: str, startTs: float, endTs: float) -> list[dict]:
        """Plays in the closed [startTs, endTs] window including their
        created_reason. The Web API reconciliation needs the source to
        guarantee it only deletes provable double-recordings (a backfill row
        next to a row from another source) - proximity alone is not proof,
        since real exports contain genuine same-track plays seconds apart
        (skip, then restart).

        canonicalId/isrc carry the track's IDENTITY, because the release id
        alone is not one: Spotify names the same recording differently in
        connect state and in the Web API, so the listener and the backfill
        record one listen under two ids. The reconciler groups on these rather
        than on track_id (see Database._groupPlaysByIdentity).

        createdAt carries the row's created_at for LISTENER rows only - their
        insert happens at the track-change moment, so it is the observed end
        of the play, pauses included; the reconciler's end-time pairing needs
        it to recognise a pause-stretched backfill copy. Other sources'
        created_at (an import/poll moment) comes through as None. Listener
        rows are also matched into the window by that created_at: a paused
        play can start before the window the API items span while still
        ending inside it.

        is_skip=0 is DELIBERATE, and it is not the same omission the insert
        guard had. hasPlayNearTime was blind to skips and re-added a skipped
        listen as a full play, which is why it grew a third arm; this reads as
        the matching gap and is not one, because the two do different things.
        hasPlayNearTime declines to INSERT - free, reversible, and wrong only
        by leaving a play out that the next poll re-offers. This DELETES, on
        the live history, with nothing to recover from.

        The tolerances are what make reusing this path wrong rather than
        merely unnecessary. Skips pair on BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS
        (20s), which is tight on purpose: measured against live data the
        provable duplicates sat 3-15s from their skip while the ambiguous ones
        sat 95s and 291s away, where "skip, then a genuine replay the listener
        missed" is the likelier reading. The reconciler's own windows are tuned
        for pairing real plays - 5s proximity, and an end-time arm reaching 10s
        off a listener created_at that a mid-track pause can stretch by minutes
        - and its mixed-sources rule then deletes the backfill row from any
        cluster holding a sibling from another source. Letting skips into these
        clusters would delete genuine replays.

        So the skip case is OWNED by hasPlayNearTime's skip arm, which stops
        the duplicate being written at all, and by tools/sweep_backfill_duplicates.py
        for anything that landed before that arm existed (its own
        --skip-tolerance, same constant). Reaching it from here would need a
        separate arm in _isSameListen keyed to the skip tolerance, not a
        widening of this filter. The ordering the gap would require is also the
        unnatural one: the listener writes its skip at the track-change moment
        while the backfill only sees a play after it has ended."""
        conn = self._conn()
        #< the join is enrichment, not a filter: every WHERE clause still reads
        #  a plays column, so this stays the same narrow indexed range scan and
        #  does not become the all-history scan a joined-table filter costs
        #  (see _trackSetClause's callers). LEFT, so a play whose track row is
        #  somehow missing still comes through with a null identity rather than
        #  vanishing from a pass that DELETES what it does see.
        rows = conn.execute(
            "SELECT p.track_id, p.played_at, p.time_played, p.created_reason, "
            "t.canonical_id, t.isrc, "
            "CASE WHEN p.created_reason LIKE 'listener_play%' THEN p.created_at ELSE NULL END AS listener_created_at "
            "FROM plays p LEFT JOIN tracks t ON t.id = p.track_id "
            "WHERE p.username=? AND p.is_skip=0 AND (p.played_at BETWEEN ? AND ? "
            "OR (p.created_reason LIKE 'listener_play%' AND p.created_at BETWEEN ? AND ?))",
            (username, startTs, endTs, startTs, endTs),
        ).fetchall()
        return [
            {
                "id": r["track_id"],
                "playedAt": r["played_at"],
                "timePlayed": r["time_played"],
                "createdReason": r["created_reason"],
                "createdAt": r["listener_created_at"],
                #< the recording this release is a copy of, for the reconciler's
                #  identity grouping (Database._groupPlaysByIdentity)
                "canonicalId": r["canonical_id"],
                "isrc": r["isrc"],
            }
            for r in rows
        ]

    def getPlayedTrackIds(self, username: str, trackIds: list[str]) -> set[str]:
        """Which of `trackIds` the user has really played - where "played" spans
        each id's whole MERGE GROUP. The callers decide whether a link points at
        our own song page or out to Spotify, and the page they would link to is
        the canonical's, which shows the group: a viewer whose plays sit on the
        sibling release must get the internal link. Expanded in Python so the
        plays query stays the plain seekable IN it always was."""
        memberIds, canonicalOfRequested, canonicalOfMember = self._expandToMergeGroups(trackIds)
        playedMembers = self._playedTrackIdsRaw(username, memberIds)
        playedCanonicals = {canonicalOfMember[m] for m in playedMembers}
        return {i for i in trackIds if canonicalOfRequested.get(i, i) in playedCanonicals}

    def _playedTrackIdsRaw(self, username: str, trackIds: list[str]) -> set[str]:
        """The subset of `trackIds` this user has at least one play of - the
        Compare page's "does the viewer have their own data for this
        counterpart item" check (see app.py's comparePage), so a counterpart
        song only links out to Spotify when the viewer's own detail page
        would have nothing to show. Deliberately a real play-history lookup,
        not membership in the viewer's own top-N pool: a track can rank
        outside someone's top 100 while they've still genuinely played it,
        and getSongsPage's own trackId lookup (what the detail page actually
        renders from) has no pool-depth limit either - this matches that
        exactly."""
        if not trackIds:
            return set()
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        rows = conn.execute(
            f"SELECT DISTINCT track_id FROM plays WHERE username=? AND is_skip=0 AND track_id IN ({placeholders})",
            [username, *trackIds],
        ).fetchall()
        return {r["track_id"] for r in rows}

    def getPlayedArtistIds(self, username: str, artistIds: list[str]) -> set[str]:
        """The subset of `artistIds` this user has at least one play of a
        track crediting (any billing position) - the artist counterpart to
        getPlayedTrackIds(), matching getArtistAggregates' own artistId
        lookup exactly."""
        if not artistIds:
            return set()
        conn = self._conn()
        params: list = [username]
        idSet = self._idSetClause(params, "ta.artist_id", artistIds)
        trackSet = self._trackSetClause(params, self.ARTIST_TRACKS_SUBQUERY, artistIds)
        rows = conn.execute(
            f"""
            SELECT DISTINCT ta.artist_id AS artist_id
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            WHERE p.username=? AND p.is_skip=0{idSet}{trackSet}
            """,
            params,
        ).fetchall()
        return {r["artist_id"] for r in rows}

    def getPlayedAlbumIds(self, username: str, albumIds: list[str]) -> set[str]:
        """The subset of `albumIds` this user has at least one play of a
        track from - the album counterpart to getPlayedTrackIds(), matching
        getAlbumsPage's own albumId lookup exactly."""
        if not albumIds:
            return set()
        conn = self._conn()
        params: list = [username]
        idSet = self._idSetClause(params, "t.album_id", albumIds)
        trackSet = self._trackSetClause(params, self.ALBUM_TRACKS_SUBQUERY, albumIds)
        rows = conn.execute(
            f"""
            SELECT DISTINCT t.album_id AS album_id
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            WHERE p.username=? AND p.is_skip=0{idSet}{trackSet}
            """,
            params,
        ).fetchall()
        return {r["album_id"] for r in rows}

    @staticmethod
    def _playRowToEntry(row) -> dict:
        columns = row.keys()
        entry = {
            "id": row["track_id"],
            "playedAt": row["played_at"],
            "timePlayed": row["time_played"],
            "playedFrom": row["played_from"] if "played_from" in columns else None,
            "isSkip": bool(row["is_skip"]) if "is_skip" in columns else False,
        }
        # play_id (plays.id) is only SELECTed by the oldest-first readers the
        # export's keyset pager uses (X4) - narrower SELECT sites keep their shape.
        if "play_id" in columns:
            entry["playId"] = row["play_id"]
        # Behavioral columns are only attached when the SELECT carried them
        # (wider play/skip reads) - narrower SELECT sites keep their shape.
        if "platform" in columns:
            extras = {column: row[column] for column in BEHAVIORAL_COLUMNS}
            entry["extras"] = extras if any(v is not None for v in extras.values()) else None
        return entry

    def _searchPlayFilterParts(self, username: str, query: str, startTs: float | None, endTs: float | None,
                               trackIds: list[str] | None, includeSkips: bool,
                               fullPlaysOnly: bool) -> tuple[str, str, list]:
        params = [username]
        matchClause = self._playSearchNarrowClause(params, query)
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        trackIdsClause = self._idSetClause(params, "p.track_id", trackIds)
        joinClause = ""
        fullPlaysClause = ""
        if fullPlaysOnly:
            joinClause = self._tracksJoin(trackAlias="ft")
            fullPlaysClause = self._fullPlaysClause(params, trackAlias="ft")
        skipClause = "" if includeSkips else " AND p.is_skip=0"
        return joinClause, f"{skipClause} {matchClause}{rangeClause}{trackIdsClause}{fullPlaysClause}", params

    def searchPlays(self, username: str, query: str, limit: int | None = None, offset: int = 0,
                     startTs: float | None = None, endTs: float | None = None,
                     oldestFirst: bool = False, trackIds: list[str] | None = None,
                     includeSkips: bool = False, fullPlaysOnly: bool = False) -> list[dict]:
        """Plays (newest first, or oldest first with `oldestFirst`) whose track
        name, artist(s), album, or source playlist/album match `query` - the
        SQL-pushed-down, paginated replacement for fetching every play and
        filtering in Python. `trackIds` narrows to an explicit set of track ids
        (the history page's tag filter) - see getSongsPage's identical param.

        `includeSkips`/`fullPlaysOnly` mirror getPlaysNewestFirst's, so the
        /history search box filters exactly like the list beside it. The skip
        filter used to be hardcoded here - the parameter arrived with the
        history page's "Full plays only" checkbox, whose OFF state is the first
        thing that ever wanted skips in a search result."""
        conn = self._conn()
        limitValue = -1 if limit is None else limit
        joinClause, filterClause, params = self._searchPlayFilterParts(
            username, query, startTs, endTs, trackIds, includeSkips, fullPlaysOnly)
        params += [limitValue, offset]
        direction = "ASC" if oldestFirst else "DESC"
        #< p.is_skip is SELECTed so _playRowToEntry can report it: without the
        #  column it defaults every row to isSkip=False, which was harmless
        #  while skips could never be returned and a lie once they can
        rows = conn.execute(
            f"""
            SELECT p.track_id AS track_id, p.played_at AS played_at,
                   p.time_played AS time_played, p.played_from AS played_from,
                   p.is_skip AS is_skip
            FROM plays p{joinClause}
            WHERE p.username = ?{filterClause}
            ORDER BY p.played_at {direction}, p.id {direction}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def searchPlaysCount(self, username: str, query: str,
                          startTs: float | None = None, endTs: float | None = None,
                          trackIds: list[str] | None = None,
                          includeSkips: bool = False, fullPlaysOnly: bool = False) -> int:
        """The paging counterpart to searchPlays() - total matching plays,
        for computing total page count without fetching every match.
        `trackIds`, `includeSkips` and `fullPlaysOnly` mirror the same params on
        searchPlays(), and have to stay in step with it: the two statements are
        separate, so a filter reaching one and not the other leaves the rows
        right and the pager reporting a total nothing can page to."""
        conn = self._conn()
        joinClause, filterClause, params = self._searchPlayFilterParts(
            username, query, startTs, endTs, trackIds, includeSkips, fullPlaysOnly)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM plays p{joinClause}
            WHERE p.username = ?{filterClause}
            """,
            params,
        ).fetchone()
        return row["c"]

    # ---- Per-user: stats aggregates (SQL GROUP BY instead of Python loops over
    # the full history) -----------------------------------------------------------

    @staticmethod
    def _dateRangeClause(params: list, startTs: float | None, endTs: float | None,
                          column: str = "played_at") -> str:
        """Half-open [startTs, endTs) range, matching app.py's _getDateRange()
        documented half-open interval - a play landing exactly on endTs
        belongs to the next adjacent range, not this one. (The endTs bound
        used to be inclusive, which double-counted a boundary play into both
        a period and the immediately-following one, e.g. getOverallStats()'s
        current vs. previous period comparison.)

        Emits only the conditions whose bound exists, appending the bound
        values to `params` in clause order. The previous static
        '(? IS NULL OR played_at >= ?)' form was non-sargable: SQLite can't
        use played_at as an index range bound through the OR, so every
        ranged query walked the user's whole play history via the username
        index prefix instead of range-scanning (username, played_at)."""
        clause = ""
        if startTs is not None:
            clause += f" AND {column} >= ?"
            params.append(startTs)
        if endTs is not None:
            clause += f" AND {column} < ?"
            params.append(endTs)
        return clause

    # Driving table, the id it answers with, and how that row reaches a play,
    # for getEntitiesPlayedInRange. Entity-first on purpose - see its docstring.
    _PLAYED_IN_RANGE_KINDS = {
        "track": ("tracks e", "e.id", "e.id"),
        #< the internal recursion target for kind="track" after its merge-group
        #  expansion: same driver, but the ids are MEMBER releases and must be
        #  tested raw rather than re-expanded
        "_track_raw": ("tracks e", "e.id", "e.id"),
        "artist": ("track_artists e", "e.artist_id", "e.track_id"),
        "album": ("tracks e", "e.album_id", "e.id"),
    }

    def getEntitiesPlayedInRange(self, username: str, kind: str, ids: list[str],
                                  startTs: float | None, endTs: float | None,
                                  fullPlaysOnly: bool = False) -> list[str]:
        """Which of `ids` this user played at all in [startTs, endTs) - existence
        only, no counts and no ordering. `kind` is "track", "artist" or "album".

        The Top lists' rank-movement badge asks this about the <=50 entries on
        one page, to tell "not played in the previous period" (new) apart from
        "played, but below the depth we ranked" (unplaceable). The ranking scan
        cannot answer it: a year of one person's listening runs thousands of
        entries deep, so absence from a bounded scan means only that the scan
        ended first.

        Deliberately not the ranking query with an id filter bolted on. That
        filter lands on the JOINED table, which SQLite cannot push into the
        plays index, so it degrades to a full window scan - measured at 12ms
        (artists) and 90ms (albums) over five years of the reference library.
        Driving from the entity side instead lets every kind seek
        (username, track_id, played_at) directly: 3.4ms, 29ms and 3.7ms for the
        same window, and well under 3ms for the ranges people actually pick.

        `fullPlaysOnly` mirrors the pages' own filter, because it changes
        whether a play was a LISTEN: a period whose plays were all partial did
        not hear the track, and the badge has to agree with the list above it.
        Search and tag filters are absent on purpose - they decide which
        entities are listed, not whether one was played, and the caller's ids
        have already been through them."""
        if kind not in self._PLAYED_IN_RANGE_KINDS:
            raise ValueError(f"Unknown kind: {kind!r}")
        if not ids:
            return []   #< an empty page asks nothing, rather than everything
        if kind == "track":
            # The ids are CANONICAL (the global lists merged them), but the
            # previous period's plays may sit on member releases. A group played
            # only via its single last month was still played, and "new" must
            # not say otherwise. Expand, ask about the members, map back.
            memberIds, canonicalOfRequested, canonicalOfMember = self._expandToMergeGroups(ids)
            playedMembers = self.getEntitiesPlayedInRange(
                username, "_track_raw", memberIds, startTs, endTs, fullPlaysOnly)
            playedCanonicals = {canonicalOfMember[m] for m in playedMembers}
            return [i for i in ids if canonicalOfRequested.get(i, i) in playedCanonicals]
        driver, idColumn, trackColumn = self._PLAYED_IN_RANGE_KINDS[kind]

        conn = self._conn()
        params: list = []
        idsClause = self._idSetClause(params, idColumn, ids)
        params.append(username)
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        fullJoin = ""
        fullClause = ""
        if fullPlaysOnly:
            fullJoin = self._tracksJoin(trackAlias="ft")
            fullClause = self._fullPlaysClause(params, trackAlias="ft")

        rows = conn.execute(
            f"""
            SELECT DISTINCT {idColumn} AS id
            FROM {driver}
            WHERE 1{idsClause} AND EXISTS (
                SELECT 1 FROM plays p{fullJoin}
                WHERE p.username = ? AND p.track_id = {trackColumn}
                      AND p.is_skip = 0{rangeClause}{fullClause}
            )
            """,
            params,
        ).fetchall()
        return [row["id"] for row in rows]

    def _uniqueSongCountSql(self) -> tuple[str, str]:
        """(join, COUNT expression) for "unique songs" on the artist surfaces.

        Collapses a merge group: a merged recording is one song, which is what
        the global lists say AND, since 2026-09-05, what an artist's own song
        list says too (_mergesCanonically). The two are one decision - this
        number captions that list, and a count nobody can reconcile with what
        is on screen is indistinguishable from a wrong one, whether it spans
        more than the list or less. It collapsed once before over a list that
        did not merge ("Unique Songs Listened: 1" above two visible rows), was
        scoped back to per-release for exactly that reason, and returns with
        the list. tests/test_track_merge_audit.py's
        TestEntitySongCountsMatchTheListBesideThem pins the pairing.

        Artist surfaces only, which is the whole blast radius: the album
        aggregates count DISTINCT p.track_id inline and never collapse, so an
        album's count matches its per-release list, and the album History
        tab's singleTrackTimeline flag (routes/charts.py) keeps reading a
        per-release number.

        The tracks probe that collapses measured +193% per query, paid on every
        play row - free while nothing is merged (every instance with the
        toggle off), so the join only exists once a merge does. See
        _anyTrackMerges. Kept as a seam rather than inlined so the pairing
        stays one decision across all three artist queries."""
        if self._anyTrackMerges():
            return ("\n                JOIN tracks tsong ON tsong.id = p.track_id",
                    "COUNT(DISTINCT COALESCE(tsong.canonical_id, p.track_id)) AS unique_song_count")
        return "", "COUNT(DISTINCT p.track_id) AS unique_song_count"

    @staticmethod
    def _firstListenClause(params: list, firstListenStartTs: float | None,
                           firstListenEndTs: float | None) -> str:
        """HAVING filter on a group's FIRST listen - MIN(p.played_at) over
        every play the aggregate sees, i.e. the whole lifetime when no date
        range narrows it. This is the Wrapped discovery lists' question
        ("which songs/artists/albums did this year introduce?"), pushed into
        SQL so the worker keeps its 100 rows without hydrating every entity
        ever played. Half-open [start, end), matching _dateRangeClause's
        convention; both bounds or nothing - a discovery window is a year,
        never a ray."""
        if firstListenStartTs is None or firstListenEndTs is None:
            return ""
        params += [firstListenStartTs, firstListenEndTs]
        return " HAVING MIN(p.played_at) >= ? AND MIN(p.played_at) < ?"

    def getArtistAggregates(self, username: str, startTs: float | None = None,
                             endTs: float | None = None, artistId: str | None = None,
                             sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                             searchQuery: str | None = None, artistIds: list[str] | None = None,
                             fullPlaysOnly: bool = False,
                             firstListenStartTs: float | None = None,
                             firstListenEndTs: float | None = None) -> list[dict]:
        """One row per artist who appears on at least one played track, grouped by
        artist id (not name - two different artists that happen to share a display
        name are no longer merged, unlike the old name-keyed in-memory grouping).
        Sorted/paged in SQL (mirrors getSongsPage()/getAlbumsPage()) rather than
        fetching every artist and sorting/paging in Python.

        `artistId` narrows this to a single artist - reused by artist-detail
        pages to fetch that one artist's own aggregate stats. `artistIds`
        narrows to an explicit set of artist ids (the tag-filtered Top Artists
        page) so the caller aggregates only those rows; an empty list matches
        nothing. `searchQuery` narrows to artists whose name matches (the only
        field Top Artists' search ever matched, since a bare artist dict
        carries no track/album/playlist text to search). `fullPlaysOnly`
        mirrors getSongsPage()'s param of the same name - adds a `tracks` join
        (not needed otherwise here, since this aggregates via track_artists)
        only when set, so the default (unfiltered) path is unaffected."""
        if sortBy not in ARTIST_SORT_COLUMNS:
            raise ValueError(f"Unknown sortBy: {sortBy!r}")
        sortColumn = ARTIST_SORT_COLUMNS[sortBy]
        direction = "ASC" if sortBy == "name" else "DESC"
        limitValue = -1 if limit is None else limit

        conn = self._conn()
        # Aggregate play-side first, then join artists for only the surviving
        # ids. Joining artists up front made SQLite scan the entire global
        # artists catalog (tens of thousands of rows, most never played by this
        # user) and probe plays per artist; aggregating from this user's plays
        # and looking up only the artists that appear is ~70% faster for
        # byte-identical output on a large library.
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        aggFilter = ""
        if artistId is not None:
            # ta.artist_id == ar.id, so this is equivalent to the old outer
            # ar.id filter but prunes the aggregation to the one artist.
            aggFilter += " AND ta.artist_id = ?"
            params.append(artistId)
        aggFilter += self._idSetClause(params, "ta.artist_id", artistIds)
        aggFilter += self._trackSetClause(params, self.ARTIST_TRACKS_SUBQUERY,
                                          [artistId] if artistId is not None else artistIds)
        aggJoin = ""
        if fullPlaysOnly:
            aggJoin = self._tracksJoin()
            aggFilter += self._fullPlaysClause(params)
        #< before the outer search appends ITS params - the HAVING sits inside
        #  the CTE, so its placeholders bind ahead of the outer WHERE's
        firstListenClause = self._firstListenClause(params, firstListenStartTs, firstListenEndTs)
        outerFilter = ""
        if searchQuery:
            # The name filter only selects WHICH artists to return; it never
            # changes an artist's own play totals, so applying it after
            # aggregation is equivalent to the old pre-group filter.
            outerFilter += " WHERE " + " AND ".join(self._perWordConditions(
                params, searchQuery, self._ARTIST_NAME_CONDITION, 1))
        params += [limitValue, offset]
        tsongJoin, uniqueSongCount = self._uniqueSongCountSql()
        rows = conn.execute(
            f"""
            WITH agg AS (
                SELECT ta.artist_id AS artist_id,
                       COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                       MIN(p.played_at) AS first_listened_at,
                       {uniqueSongCount}
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id{tsongJoin}{aggJoin}
                WHERE p.username = ? AND p.is_skip=0{rangeClause}{aggFilter}
                GROUP BY ta.artist_id{firstListenClause}
            )
            SELECT ar.id AS id, ar.name AS name, ar.url AS url, ar.image_id AS image_id,
                   agg.plays AS plays, agg.total_time_listened AS total_time_listened,
                   agg.first_listened_at AS first_listened_at, agg.unique_song_count AS unique_song_count
            FROM agg
            JOIN artists ar ON ar.id = agg.artist_id{outerFilter}
            ORDER BY {sortColumn} {direction}, total_time_listened DESC, name COLLATE NOCASE ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [
            {
                "id": r["id"], "name": r["name"], "url": r["url"], "imageUrl": "", "imageId": r["image_id"],
                "plays": r["plays"], "totalTimeListened": r["total_time_listened"],
                "uniqueSongCount": r["unique_song_count"], "firstListenedAt": r["first_listened_at"],
            }
            for r in rows
        ]

    def getArtistsCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                         searchQuery: str | None = None, artistIds: list[str] | None = None,
                         fullPlaysOnly: bool = False) -> int:
        """Number of distinct artists played in range - the paging counterpart
        to getArtistAggregates(), used to compute total page count without
        fetching every artist's metadata. `artistIds` mirrors the same param
        on getArtistAggregates(). `fullPlaysOnly` mirrors getArtistAggregates()'s
        param of the same name."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        searchClause = ""
        if searchQuery:
            searchClause = self._perWordAndClause(params, searchQuery,
                                                  self._ARTIST_NAME_CONDITION, 1)
        searchClause += self._idSetClause(params, "ta.artist_id", artistIds)
        searchClause += self._trackSetClause(params, self.ARTIST_TRACKS_SUBQUERY, artistIds)
        joinClause = ""
        if fullPlaysOnly:
            joinClause = self._tracksJoin()
            searchClause += self._fullPlaysClause(params)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT ta.artist_id FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                JOIN artists ar ON ar.id = ta.artist_id{joinClause}
                WHERE p.username = ? AND p.is_skip=0{rangeClause}{searchClause}
                GROUP BY ta.artist_id
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def getArtistTotals(self, username: str, startTs: float | None = None,
                         endTs: float | None = None, fullPlaysOnly: bool = False,
                         artistIds: list[str] | None = None) -> tuple[int, int, int]:
        """(total plays, total unique songs, total time listened) summed across
        every artist in range - the Top Artists page's "(top list)" totals.
        Deliberately a sum of each artist's own aggregate (an artist with N
        plays contributes N; a multi-artist track's plays are counted once per
        artist on it), not the same number as getPlayTotals()'s track-level
        total - matches the totals the old fetch-everything-then-sum-in-Python
        code computed, just without hydrating every artist's name/url first.
        `fullPlaysOnly` mirrors getArtistAggregates()'s param of the same name;
        so does `artistIds` (the page's tag filter), which keeps this header
        total consistent with the tag-filtered list below it."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        artistFilter = self._idSetClause(params, "ta.artist_id", artistIds)
        artistFilter += self._trackSetClause(params, self.ARTIST_TRACKS_SUBQUERY, artistIds)
        joinClause = ""
        fullPlaysClause = ""
        if fullPlaysOnly:
            joinClause = self._tracksJoin()
            fullPlaysClause = self._fullPlaysClause(params)
        tsongJoin, uniqueSongCount = self._uniqueSongCountSql()
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(plays), 0) AS total_plays,
                   COALESCE(SUM(unique_song_count), 0) AS total_unique,
                   COALESCE(SUM(total_time_listened), 0) AS total_time_listened
            FROM (
                SELECT COUNT(*) AS plays,
                       {uniqueSongCount},
                       SUM(p.time_played) AS total_time_listened
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id{tsongJoin}{joinClause}
                WHERE p.username = ? AND p.is_skip=0{rangeClause}{artistFilter}{fullPlaysClause}
                GROUP BY ta.artist_id
            )
            """,
            params,
        ).fetchone()
        return row["total_plays"], row["total_unique"], row["total_time_listened"]

    def getSongsPage(self, username: str, startTs: float | None = None, endTs: float | None = None,
                      sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                      trackId: str | None = None, artistId: str | None = None,
                      albumId: str | None = None, searchQuery: str | None = None,
                      trackIds: list[str] | None = None, fullPlaysOnly: bool = False,
                      firstListenStartTs: float | None = None,
                      firstListenEndTs: float | None = None) -> list[dict]:
        """Sorted/paged song stats in one batched round-trip, replacing the old
        "aggregate, then getTrack() per row" N+1 pattern - a caller asking for
        page N now pays for page N, not for every song ever played.

        tracks/albums are a 1:1 relationship (tracks.album_id NOT NULL), so
        they're safe to aggregate together in one GROUP BY t.id query without
        duplicating rows. artists are 1:many per track, so they're fetched in a
        second, small query keyed by just this page's track ids (mirrors
        getAllTracks()'s two-query shape) rather than fanning out the GROUP BY.

        `trackId` narrows to ONE SONG - the whole merge group, aggregated onto
        its canonical row, because the caller is the song detail page and that
        page is the canonical's (ask by either end of a merge and the same row
        answers; the route redirects on the id mismatch). `artistId` narrows
        to an artist's own songs and merges like the global list does - every
        release is the artist's own, and membership is decided by the played
        track, so the total still adds up (see _mergesCanonically). `albumId`
        narrows to an album's own songs and stays per-release, since an album
        page describes one release. `artistId` is
        matched via EXISTS rather than an extra JOIN so a multi-artist track
        still yields exactly one row. `trackIds` narrows to an explicit set of
        track ids (the tag-filtered playlist export, already group-expanded by
        getTaggedTrackIds) so the caller aggregates
        only those rows instead of the whole library; an empty list matches
        nothing. `searchQuery` narrows to songs whose
        name, album, or artist(s) match - safe to check via the current row's
        own t.id (unlike getAlbumsPage(), every row already shares the same
        t.id within a GROUP BY t.id group, so there's no risk of the filter
        seeing a different track's data than the one being aggregated).

        `fullPlaysOnly`: when True, only counts plays that reached the admin's
        completion-complete percent of the track's duration (same standard as
        getCompletionStats() and the Forgotten Favorite trend) - a play that
        was merely started/skipped-late never counts as a "listen" of the
        song. Defaults False so every other caller (song-detail page, tagged
        exports, search) keeps its full play history unfiltered; only the Top
        Songs page opts in. tracks is already joined here regardless, so this
        costs one extra WHERE predicate, no new join.
        """
        if sortBy not in SONG_SORT_COLUMNS:
            raise ValueError(f"Unknown sortBy: {sortBy!r}")
        sortColumn = SONG_SORT_COLUMNS[sortBy]
        direction = "ASC" if sortBy == "name" else "DESC"
        limitValue = -1 if limit is None else limit

        conn = self._conn()
        # Decided before the filters because the trackId one depends on it: a
        # merged song is ONE song on a global list AND on its own page - see
        # _mergesCanonically. `e` is whichever track the row is really about.
        canonicalJoin = ""
        e = "t"
        if self._mergesCanonically(trackId, artistId, albumId):
            canonicalJoin = self._canonicalTrackJoin()
            e = "c"

        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        extraClauses = ""
        if trackId is not None:
            #< the group's canonical, so asking by EITHER end of a merge returns
            #  the one canonical row with the group's totals. The route redirects
            #  when the answer's id differs from the asked id.
            extraClauses += f" AND {e}.id = ?"
            params.append(self.resolveCanonicalTrackId(trackId) if e == "c" else trackId)
        if artistId is not None:
            # The EXISTS decides membership; _trackSetClause below adds the
            # seekable twin (see its docstring - this one measured ~985ms
            # without it, on the artist detail page's own song list).
            extraClauses += " AND EXISTS (SELECT 1 FROM track_artists ta2 WHERE ta2.track_id = t.id AND ta2.artist_id = ?)"
            params.append(artistId)
        if albumId is not None:
            extraClauses += " AND al.id = ?"
            params.append(albumId)
        extraClauses += self._trackSetClause(params, self.ARTIST_TRACKS_SUBQUERY,
                                             [artistId] if artistId is not None else None)
        extraClauses += self._trackSetClause(params, self.ALBUM_TRACKS_SUBQUERY,
                                             [albumId] if albumId is not None else None)
        extraClauses += self._idSetClause(params, "t.id", trackIds)
        #< `trackIds` above needs no canonical hop: getTaggedTrackIds already
        #  hands over whole merge groups. The search set cannot - it is
        #  resolved from the catalog by name, so it holds whichever releases
        #  matched - hence the grouped key, which is `c.id` only when merging
        extraClauses += self._searchNarrowClause(params, searchQuery, "t.id",
                                                 canonicalColumn="c.id" if e == "c" else None)
        if fullPlaysOnly:
            extraClauses += self._fullPlaysClause(params)
        firstListenClause = self._firstListenClause(params, firstListenStartTs, firstListenEndTs)
        params += [limitValue, offset]

        rows = conn.execute(
            f"""
            SELECT
                {e}.id AS track_id, {e}.name AS name, {e}.url AS url, {e}.image_id AS image_id,
                {e}.duration_ms AS duration_ms, {e}.explicit AS explicit, {e}.isrc AS isrc,
                {e}.disc_number AS disc_number, {e}.track_number AS track_number,
                {e}.created_reason AS created_reason, {e}.availability_reason AS availability_reason,
                {e}.canonical_id AS canonical_id,
                al.id AS album_id, al.name AS album_name, al.url AS album_url,
                al.total_tracks AS album_total_tracks, al.release_date AS album_release_date,
                al.image_id AS album_image_id, al.image_url AS album_image_url,
                COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                MIN(p.played_at) AS first_listened_at, MAX(p.played_at) AS last_played_at
            FROM plays p
            JOIN tracks t ON t.id = p.track_id{canonicalJoin}
            LEFT JOIN albums al ON al.id = {e}.album_id
            WHERE p.username = ? AND p.is_skip=0{rangeClause}{extraClauses}
            GROUP BY {e}.id{firstListenClause}
            ORDER BY {sortColumn} {direction}, total_time_listened DESC, name COLLATE NOCASE ASC, track_id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()

        artistsByTrack = self._artistsForTracks([row["track_id"] for row in rows])
        return [self._songRowToDict(row, artistsByTrack.get(row["track_id"], [])) for row in rows]

    def getSongsCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                       searchQuery: str | None = None, trackIds: list[str] | None = None,
                       fullPlaysOnly: bool = False) -> int:
        """Number of distinct songs played in range - the paging counterpart to
        getSongsPage(), used to compute total page count without fetching every
        song's metadata. `trackIds` mirrors the same param on getSongsPage().
        `fullPlaysOnly` mirrors getSongsPage()'s param of the same name -
        defaults False (unfiltered) for every caller except the Top Songs page."""
        conn = self._conn()
        if not searchQuery:
            # No name/artist/album lookup needed, so skip the joins entirely -
            # this stays exactly as cheap as before search support was added,
            # unless fullPlaysOnly needs tracks.duration_ms.
            params = [username]
            rangeClause = self._dateRangeClause(params, startTs, endTs)
            trackIdsClause = self._idSetClause(params, "track_id", trackIds)
            joinClause = ""
            fullPlaysClause = ""
            if fullPlaysOnly:
                joinClause = self._tracksJoin(playsAlias="plays")
                fullPlaysClause = self._fullPlaysClause(params, playsAlias="plays")
            # Counts what getSongsPage LISTS, so it has to merge the same way -
            # the two are separate queries and pagination breaks the moment they
            # disagree about how many rows exist. getSongsCount takes no
            # track/artist/album narrowing at all, so it is always the global
            # answer. The tracks join arrives here for the canonical even when
            # fullPlaysOnly did not ask for one.
            countJoin = joinClause or self._tracksJoin(playsAlias="plays")
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM (
                    SELECT COALESCE(t.canonical_id, t.id) AS k FROM plays{countJoin}
                    WHERE username = ? AND is_skip=0{rangeClause}{trackIdsClause}{fullPlaysClause}
                    GROUP BY k
                )
                """,
                params,
            ).fetchone()
            return row["c"]

        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        trackIdsClause = self._idSetClause(params, "p.track_id", trackIds)
        # The search is resolved to a track-id set on p.track_id
        # (_searchNarrowClause), so this branch scans plays alone too - the
        # tracks/albums joins were leftovers from when the match predicate ran
        # inline here. tracks joins only for fullPlaysOnly's duration, exactly
        # like the no-search branch above.
        joinClause = ""
        fullPlaysClause = ""
        if fullPlaysOnly:
            joinClause = self._tracksJoin()
            fullPlaysClause = self._fullPlaysClause(params)
        #< appended last, so it must also be the last clause in the SQL below.
        #  On the grouped key like getSongsPage's, or the count would size the
        #  pager off a different population than the list it sits under
        searchClause = self._searchNarrowClause(params, searchQuery, "p.track_id",
                                                canonicalColumn="COALESCE(t.canonical_id, t.id)")
        countJoin = joinClause or self._tracksJoin()   #< see the branch above
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT COALESCE(t.canonical_id, t.id) AS k FROM plays p{countJoin}
                WHERE p.username = ? AND p.is_skip=0{rangeClause}{trackIdsClause}{fullPlaysClause}{searchClause}
                GROUP BY k
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def getAlbumsPage(self, username: str, startTs: float | None = None, endTs: float | None = None,
                       sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                       albumId: str | None = None, searchQuery: str | None = None,
                       albumIds: list[str] | None = None, fullPlaysOnly: bool = False,
                       firstListenStartTs: float | None = None,
                       firstListenEndTs: float | None = None) -> list[dict]:
        """Sorted/paged album stats in one batched round-trip - one row per
        album, aggregated across every track on it this user played. Mirrors
        getSongsPage()'s SQL-first sort/page pattern exactly.

        `albumId` narrows this to a single album - reused by album-detail pages
        to fetch that one album's own aggregate stats. `albumIds` narrows to an
        explicit set of album ids (the tag-filtered Top Albums page) so the
        caller aggregates only those rows; an empty list matches nothing.
        `searchQuery` narrows to albums whose name or any artist on them
        matches, resolved to an id set first (getMatchingAlbumIds). The artist
        check deliberately spans every track on the album rather than just the
        current row's own track: unlike getSongsPage() (grouped by t.id, so
        every row in a group already shares one track), an album's rows span
        multiple different tracks, so judging by the current row's track alone
        would silently drop that album's non-matching tracks from the aggregate
        instead of keeping the album's true totals. As an inline correlated
        EXISTS that rule cost 1526ms here for "love" and 2962ms for a term
        matching nothing - see getMatchingAlbumIds.
        """
        if sortBy not in ALBUM_SORT_COLUMNS:
            raise ValueError(f"Unknown sortBy: {sortBy!r}")
        sortColumn = ALBUM_SORT_COLUMNS[sortBy]
        direction = "ASC" if sortBy == "name" else "DESC"
        limitValue = -1 if limit is None else limit

        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        extraClauses = ""
        if albumId is not None:
            # Redundant against al.id (= t.album_id) and present only so the
            # planner has a seekable track set - see the same rewrite in
            # getSongsPage. Narrowing to one album read the user's entire play
            # history before this (~60ms), and the album detail page pays it on
            # both the shell and the deferred body.
            extraClauses += " AND al.id = ?"
            params.append(albumId)
        extraClauses += self._idSetClause(params, "al.id", albumIds)
        extraClauses += self._trackSetClause(params, self.ALBUM_TRACKS_SUBQUERY,
                                             [albumId] if albumId is not None else albumIds)
        extraClauses += self._albumSearchNarrowClause(params, searchQuery)
        if fullPlaysOnly:
            extraClauses += self._fullPlaysClause(params)
        firstListenClause = self._firstListenClause(params, firstListenStartTs, firstListenEndTs)
        params += [limitValue, offset]

        rows = conn.execute(
            f"""
            SELECT
                al.id AS album_id, al.name AS name, al.url AS url, al.image_id AS image_id,
                al.image_url AS image_url, al.total_tracks AS total_tracks, al.release_date AS release_date,
                COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                COUNT(DISTINCT p.track_id) AS unique_song_count, MIN(p.played_at) AS first_listened_at
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN albums al ON al.id = t.album_id
            WHERE p.username = ? AND p.is_skip=0{rangeClause}{extraClauses}
            GROUP BY al.id{firstListenClause}
            ORDER BY {sortColumn} {direction}, total_time_listened DESC, name COLLATE NOCASE ASC, album_id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()

        artistsByAlbum = self._artistsForAlbums([row["album_id"] for row in rows])
        return [self._albumStatsRowToDict(row, artistsByAlbum.get(row["album_id"], [])) for row in rows]

    def getAlbumsCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                        searchQuery: str | None = None, albumIds: list[str] | None = None,
                        fullPlaysOnly: bool = False) -> int:
        """Number of distinct albums played in range - the paging counterpart to
        getAlbumsPage(), used to compute total page count without fetching every
        album's metadata. `albumIds` mirrors the same param on getAlbumsPage().
        `fullPlaysOnly` mirrors getAlbumsPage()'s param of the same name.

        One query for both cases now: the search used to need its own branch,
        because matching an album by name or by a credited artist meant joining
        albums (and correlating an EXISTS over its tracks) inside this
        aggregate. Resolved to an id set on t.album_id instead, a searched count
        is the unsearched one plus one membership test - so the branch that
        existed to keep the unsearched count cheap has nothing left to skip."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        albumIdsClause = self._idSetClause(params, "t.album_id", albumIds)
        # Redundant against t.album_id above and present only so the planner has
        # a seekable track set - see getAlbumsPage's identical twin. This was the
        # one member of the family without it, so the tag-filtered Top Albums
        # pager's COUNT read all history.
        albumIdsClause += self._trackSetClause(params, self.ALBUM_TRACKS_SUBQUERY, albumIds)
        albumIdsClause += self._albumSearchNarrowClause(params, searchQuery, "t.album_id")
        fullPlaysClause = ""
        if fullPlaysOnly:
            fullPlaysClause = self._fullPlaysClause(params)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT t.album_id FROM plays p
                JOIN tracks t ON t.id = p.track_id
                WHERE p.username = ? AND p.is_skip=0{rangeClause}{albumIdsClause}{fullPlaysClause}
                GROUP BY t.album_id
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def _artistsForAlbums(self, albumIds: list[str]) -> dict[str, list[dict]]:
        """Distinct artists across every track on each album, grouped by album id
        and ordered by their earliest track position - the album-level
        counterpart to _artistsForTracks()."""
        if not albumIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in albumIds)
        rows = conn.execute(
            f"""
            SELECT t.album_id AS album_id, a.id AS id, a.name AS name, a.url AS url, a.image_id AS image_id,
                   MIN(ta.position) AS min_position
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            JOIN tracks t ON t.id = ta.track_id
            WHERE t.album_id IN ({placeholders})
            GROUP BY t.album_id, a.id
            ORDER BY t.album_id, min_position
            """,
            albumIds,
        ).fetchall()
        artistsByAlbum: dict[str, list] = {}
        for row in rows:
            artistsByAlbum.setdefault(row["album_id"], []).append(
                {"id": row["id"], "name": row["name"], "url": row["url"], "imageUrl": "", "imageId": row["image_id"]}
            )
        return artistsByAlbum

    @staticmethod
    def _albumStatsRowToDict(row, artists: list[dict]) -> dict:
        return {
            "id": row["album_id"],
            "name": row["name"],
            "url": row["url"],
            "imageId": row["image_id"],
            "imageUrl": row["image_url"],
            "totalTracks": row["total_tracks"],
            "releaseDate": row["release_date"],
            "artists": artists,
            "plays": row["plays"],
            "totalTimeListened": row["total_time_listened"],
            "uniqueSongCount": row["unique_song_count"],
            "firstListenedAt": row["first_listened_at"],
        }

    def _artistsForTracks(self, trackIds: list[str]) -> dict[str, list[dict]]:
        """Ordered artists for a specific set of track ids, grouped by track id -
        the batched counterpart to the per-artist JOIN in getTrack()."""
        if not trackIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        rows = conn.execute(
            f"""
            SELECT ta.track_id AS track_id, a.id AS id, a.name AS name, a.url AS url, a.image_id AS image_id
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.track_id IN ({placeholders})
            ORDER BY ta.track_id, ta.position
            """,
            trackIds,
        ).fetchall()
        artistsByTrack: dict[str, list] = {}
        for row in rows:
            artistsByTrack.setdefault(row["track_id"], []).append(
                {"id": row["id"], "name": row["name"], "url": row["url"], "imageUrl": "", "imageId": row["image_id"]}
            )
        return artistsByTrack

    @staticmethod
    def _songRowToDict(row, artists: list[dict]) -> dict:
        hasAlbum = row["album_id"] is not None
        return {
            "id": row["track_id"],
            # The track this one was merged into, or None. Carried so the song
            # page can redirect to the canonical without a second lookup - it
            # has already loaded the row that knows.
            #
            #< asked for rather than indexed: this function degrades gracefully
            #  on a partial row (see the album fallback below and the test that
            #  hands it one directly), and a bare row["canonical_id"] made that
            #  a KeyError
            "canonicalId": (row["canonical_id"] if "canonical_id" in row.keys() else None),
            "name": row["name"],
            "url": row["url"],
            "imageUrl": row["album_image_url"] if hasAlbum else "",
            "imageId": row["image_id"],
            "duration": row["duration_ms"],
            "explicit": bool(row["explicit"]),
            "isrc": row["isrc"] or "",
            "discNumber": row["disc_number"],
            "trackNumber": row["track_number"],
            "releaseDate": row["album_release_date"] if hasAlbum else None,
            "album": {
                "id": row["album_id"],
                "name": row["album_name"],
                "url": row["album_url"],
                "imageId": row["album_image_id"],
                "imageUrl": row["album_image_url"],
                "totalTracks": row["album_total_tracks"],
                "releaseDate": row["album_release_date"],
            } if hasAlbum else None,
            "artists": artists,
            "plays": row["plays"],
            "totalTimeListened": row["total_time_listened"],
            "firstListenedAt": row["first_listened_at"],
            "lastPlayedAt": row["last_played_at"],
            "created_reason": row["created_reason"],
            "availability_reason": row["availability_reason"],
        }

    def getExplicitCounts(self, username: str, startTs: float | None = None,
                           endTs: float | None = None) -> dict:
        """{explicit, clean} play counts in range - the Charts explicit ratio."""
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        # Single aggregated row instead of GROUP BY t.explicit: NULL and 0
        # both mean "not explicit" and must land in the same clean count.
        row = self._conn().execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN t.explicit THEN 1 ELSE 0 END), 0) AS explicit_count,
                COALESCE(SUM(CASE WHEN t.explicit THEN 0 ELSE 1 END), 0) AS clean_count
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
            """,
            params,
        ).fetchone()
        return {"explicit": row["explicit_count"], "clean": row["clean_count"]}

    def getReleaseDecadeCounts(self, username: str, startTs: float | None = None,
                                endTs: float | None = None) -> list[dict]:
        """[{decade, count}] in range, oldest first - the Charts release-era bars.

        Decades computed fully in SQL. Release dates are stored as midnight-UTC
        timestamps of a calendar date, so the year is read back in UTC too -
        applying the app timezone here (as the Python loop this replaced did)
        shifted every Jan 1 release into the previous year whenever the offset
        was negative. HAVING drops the NULL decade a timestamp outside
        strftime's supported year range would produce, matching the old loop's
        swallow-and-skip."""
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        rows = self._conn().execute(
            f"""
            SELECT (CAST(strftime('%Y', al.release_date, 'unixepoch') AS INTEGER) / 10) * 10 AS decade,
                   COUNT(*) AS count
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            JOIN albums al ON t.album_id = al.id
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
              AND al.release_date IS NOT NULL
              AND al.release_date != 0
            GROUP BY decade
            HAVING decade IS NOT NULL
            ORDER BY decade
            """,
            params,
        ).fetchall()
        return [{"decade": row["decade"], "count": row["count"]} for row in rows]

    def getCompletionCounts(self, username: str, startTs: float | None = None,
                             endTs: float | None = None) -> dict:
        """{skips, completes, partials} in range - the Charts completion pie.

        Fully classified in SQL: one aggregate row instead of a row per distinct
        (time_played, duration) pair. A skip is any is_skip=1 play. Among real
        plays, one at/over the admin's complete percent counts as complete
        (unknown <=0 durations count complete, since partial can't be told
        apart), else partial - so the three always sum to the range's plays.

        The completion test is spelled out rather than taken from
        _fullPlaysClause: that builder emits a WHERE fragment, and this needs
        the same boundary twice, inside a CASE pair, with the partial branch
        being its inverse."""
        ratio = self.getCompletionCompletePercent() / PERCENT_DIVISOR
        params = [ratio, ratio, username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        row = self._conn().execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END), 0) AS skips,
                COALESCE(SUM(CASE WHEN p.is_skip = 0
                                   AND (t.duration_ms <= 0 OR p.time_played >= t.duration_ms * ?)
                                  THEN 1 ELSE 0 END), 0) AS completes,
                COALESCE(SUM(CASE WHEN p.is_skip = 0
                                   AND t.duration_ms > 0 AND p.time_played < t.duration_ms * ?
                                  THEN 1 ELSE 0 END), 0) AS partials
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            WHERE p.username = ?{rangeClause}
            """,
            params,
        ).fetchone()
        return {"skips": row["skips"], "completes": row["completes"], "partials": row["partials"]}

    def getBehavioralCounts(self, username: str, startTs: float | None = None,
                             endTs: float | None = None) -> dict:
        """Raw material for the Charts page's Listening Behavior card
        (services/listening_behavior.buildListeningBehavior does the labelling
        and ratio maths - this returns un-bucketed rows).

        Four reads, all on `plays` alone - no join, so idx_plays_user_time
        (username, played_at) serves every one of them:
          - scalars: the range's total play count, and for each of
            shuffle/offline/incognito how many rows have it set at all
            (`known`) and how many of those are 1 (`on`). Every listener-
            recorded play leaves all three NULL (see BEHAVIORAL_COLUMNS'
            docstring in Database/db.py), so `known` is never `total` in a
            range mixing imported and live rows - that's the normal case,
            which is why the ratio card divides by `known`, not `total`.
          - a GROUP BY reason_end (the NULL row is kept as its own group,
            not filtered - a live play's missing reason_end is itself
            information the card reports, not noise to drop);
          - a GROUP BY platform;
          - a GROUP BY conn_country.
        All three GROUP BYs come back as raw (value, count) pairs - no
        label map here; bucketReasonEnd/bucketPlatform own that."""
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        scalarRow = self._conn().execute(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(shuffle IS NOT NULL), 0) AS shuffle_known,
                COALESCE(SUM(shuffle = 1), 0) AS shuffle_on,
                COALESCE(SUM(offline IS NOT NULL), 0) AS offline_known,
                COALESCE(SUM(offline = 1), 0) AS offline_on,
                COALESCE(SUM(incognito IS NOT NULL), 0) AS incognito_known,
                COALESCE(SUM(incognito = 1), 0) AS incognito_on
            FROM plays p
            WHERE p.username = ?{rangeClause}
            """,
            params,
        ).fetchone()

        def _groupedCounts(column: str) -> list[tuple]:
            groupParams = [username]
            groupRangeClause = self._dateRangeClause(groupParams, startTs, endTs, column="p.played_at")
            rows = self._conn().execute(
                f"""
                SELECT {column} AS value, COUNT(*) AS c
                FROM plays p
                WHERE p.username = ?{groupRangeClause}
                GROUP BY {column}
                """,
                groupParams,
            ).fetchall()
            return [(row["value"], row["c"]) for row in rows]

        return {
            "total": scalarRow["total"],
            "shuffle": {"known": scalarRow["shuffle_known"], "on": scalarRow["shuffle_on"]},
            "offline": {"known": scalarRow["offline_known"], "on": scalarRow["offline_on"]},
            "incognito": {"known": scalarRow["incognito_known"], "on": scalarRow["incognito_on"]},
            "reasonEnd": _groupedCounts("reason_end"),
            "platforms": _groupedCounts("platform"),
            "countries": _groupedCounts("conn_country"),
        }

    def getSkipStats(self, username: str, startTs: float | None = None, endTs: float | None = None,
                      trackId: str | None = None, artistId: str | None = None,
                      albumId: str | None = None) -> dict:
        """{plays, skips} for one entity (or the whole library) in range.

        Counts both halves in a single pass so the two numbers always come from
        the same scan - the detail pages show them as a pair ("12 skips, 54
        plays") and a plays figure from one query with a skips figure from
        another could disagree across a concurrent write.

        Deliberately NOT folded into getSongsPage: that query has is_skip=0 in
        its WHERE, which is load-bearing for its plan (an is_skip partial index
        was already measured to regress it 2x), and rewriting its aggregates as
        conditional sums to carry skips would put that at risk for a number
        most of its callers don't want."""
        conn = self._conn()
        params: list = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="played_at")
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)
        row = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN is_skip = 0 THEN 1 ELSE 0 END), 0) AS plays,
                COALESCE(SUM(CASE WHEN is_skip = 1 THEN 1 ELSE 0 END), 0) AS skips
            FROM plays
            WHERE username = ?{rangeClause}{extraClauses}
            """,
            params,
        ).fetchone()
        return {"plays": row["plays"], "skips": row["skips"]}

    def _shrunkSkipRateSql(self) -> str:
        """Ranking expression: the row's skip rate, pulled toward the library's
        own average by SKIP_RATE_PRIOR_WEIGHT imaginary encounters.

        A raw rate is unusable for ranking at low volume - one skip out of two
        encounters is not a 50% habit, it's barely evidence. The old fix was a
        minimum-encounters cutoff, which hid those rows entirely; this tempers
        them instead, so nothing is silently missing from a paged list while
        heavily-played rows keep essentially their real number.

        Note this is the ORDER BY only. The percentage SHOWN to the user stays
        the true skips/encounters - a displayed number should be the real one."""
        return "(skips + ? * (SELECT rate FROM lib)) / (encounters + ?)"

    def _libraryRateCte(self, username: str, params: list, startTs, endTs) -> str:
        """The user's overall skip rate in range - the average low-volume rows
        are pulled toward. Appends its own bound params, so it must be built
        before the main query's.

        Never narrowed by the page's filters, only by the date range. The prior
        is "this listener's own norm", so shrinking a row toward the average of
        whatever else matched the search box would make its rank depend on its
        neighbours.

        That once excluded partial listens too, when the Full-plays checkbox
        was on, so the prior was measured the same way as the rows compared
        against it. Dropped: it needed a tracks join for one scalar and doubled
        the whole query (1.5s -> 3.1s on 400k plays) on the path that checkbox
        defaults to, and it bought only a second-order shift in ordering. One
        rule - the prior is whole-library, always - is also easier to keep.

        The 0.0 fallback is a real, not an integer: an all-integer numerator
        would make the ranking expression integer-divide and collapse every
        row to 0. (An empty library can't reach the ORDER BY today, since the
        same filter feeds both halves - this just doesn't depend on that.)"""
        params.append(username)
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        return f"""lib AS (
                SELECT COALESCE(
                    CAST(SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*), 0), 0.0
                ) AS rate
                FROM plays p WHERE p.username = ?{rangeClause}
            )"""

    def _fullPlayOrSkipClause(self, params: list) -> str:
        """The Top pages' "Full plays only" checkbox, as the skip queries mean it.

        Everywhere else that checkbox drops any play short of the admin's
        completion percent. Applied verbatim here it would drop every SKIP too
        - they're the shortest plays there are - and empty the page the
        checkbox defaults to on. So skips are always kept and only the
        partial listens go, leaving "of the times this came up, how often did
        I skip it rather than hear it through"."""
        return self._fullPlaysClause(params, keepSkips=True)

    def _skippedTrackFilters(self, params: list, trackId: str | None, artistId: str | None,
                              albumId: str | None, searchQuery: str | None,
                              trackIds: list[str] | None, fullPlaysOnly: bool) -> tuple[str, str]:
        """(joins, WHERE, tracksJoined) for the per-track skip scan - mirrors
        getSongsPage()'s filters, and is shared by the list and its count so
        the pager can never size itself for a different list than the one on
        screen.

        Joins are added only when a filter needs them, so an unfiltered page
        stays the plain plays scan it was - hence `tracksJoined`, which says
        whether `t` is already in scope. Both callers need to know, because the
        canonical join hangs off `t` and they must not join it twice; they used
        to answer it by searching the generated SQL for the substring
        `" tracks t "`, which is a guess about a string this function owns the
        truth of - and the fallback beside it (`startswith("JOIN tracks")`)
        would have SUPPRESSED a needed join the day this emitted a differently
        aliased one."""
        joins = ""
        where = ""
        if trackId is not None:
            #< the group, for the same reason as _itemFilterClauses: the only
            #  trackId caller is getSong's skip-only fallback, i.e. the page
            where += self._mergeGroupClause(params, trackId, column="p.track_id")
        where += self._idSetClause(params, "p.track_id", trackIds)
        # The artist filter is the track-set clause outright (there is no other
        # artist predicate here to decide membership); the album one still
        # filters t.album_id and only gains the seekable twin. See
        # _trackSetClause - this scan measured ~1.2s for a well-played artist.
        where += self._trackSetClause(params, self.ARTIST_TRACKS_SUBQUERY,
                                      [artistId] if artistId is not None else None)
        tracksJoined = albumId is not None or fullPlaysOnly
        if tracksJoined:
            joins += self._tracksJoin()
        if albumId is not None:
            where += " AND t.album_id = ?"
            params.append(albumId)
        where += self._trackSetClause(params, self.ALBUM_TRACKS_SUBQUERY,
                                      [albumId] if albumId is not None else None)
        # The search matched the track's name, its album's name, or a credited
        # artist - all attributes of the track, none of the play - so it resolves
        # to the same id set getSongsPage uses, and the tracks/albums joins it
        # needed go with it. Inline it cost 219ms unsearched -> 880ms for a term
        # matching nothing on the real library.
        #
        #< onto the grouped key wherever this scan merges, exactly as
        #  getSongsPage does it - and a skip RATE makes it sharper than a plain
        #  total, being a ratio whose two halves must come from one population.
        #  Recomputed from these same params by both callers (the list's
        #  skipKey, the count's unconditional c.id), so they cannot disagree
        where += self._searchNarrowClause(
            params, searchQuery, "p.track_id",
            canonicalColumn=("c.id" if self._mergesCanonically(trackId, artistId, albumId)
                             else None))
        if fullPlaysOnly:
            where += self._fullPlayOrSkipClause(params)
        return joins, where, tracksJoined

    def _skippedArtistFilters(self, params: list, artistId: str | None, searchQuery: str | None,
                               artistIds: list[str] | None, fullPlaysOnly: bool) -> tuple[str, str]:
        """(joins, WHERE) for the per-artist skip scan - see
        _skippedTrackFilters. Assumes track_artists ta and artists ar are
        already joined; only tracks is conditional."""
        joins = ""
        where = ""
        if artistId is not None:
            where += " AND ta.artist_id = ?"
            params.append(artistId)
        where += self._idSetClause(params, "ta.artist_id", artistIds)
        where += self._trackSetClause(params, self.ARTIST_TRACKS_SUBQUERY,
                                      [artistId] if artistId is not None else artistIds)
        if searchQuery:
            # Name is the only field Top Artists' search ever matched - see
            # getArtistAggregates. It selects WHICH artists appear and never
            # changes one's own totals, so it's the same before or after the
            # GROUP BY.
            where += self._perWordAndClause(params, searchQuery,
                                            self._ARTIST_NAME_CONDITION, 1)
        if fullPlaysOnly:
            joins += " JOIN tracks t ON t.id = p.track_id"
            where += self._fullPlayOrSkipClause(params)
        return joins, where

    def _skippedAlbumFilters(self, params: list, albumId: str | None, searchQuery: str | None,
                              albumIds: list[str] | None, fullPlaysOnly: bool) -> str:
        """WHERE for the per-album skip scan - see _skippedTrackFilters. Both
        tracks t and albums al are already joined here, so nothing conditional
        is needed."""
        where = ""
        if albumId is not None:
            where += " AND al.id = ?"
            params.append(albumId)
        where += self._idSetClause(params, "al.id", albumIds)
        where += self._trackSetClause(params, self.ALBUM_TRACKS_SUBQUERY,
                                      [albumId] if albumId is not None else albumIds)
        # The artist check spans every track on the album rather than the
        # current row's own - see getAlbumsPage() on why, and
        # getMatchingAlbumIds on what that cost inline (734ms -> 3574ms here).
        where += self._albumSearchNarrowClause(params, searchQuery)
        if fullPlaysOnly:
            where += self._fullPlayOrSkipClause(params)
        return where

    def getMostSkippedTracks(self, username: str, startTs: float | None = None,
                              endTs: float | None = None, limit: int = 10, offset: int = 0,
                              priorWeight: int = SKIP_RATE_PRIOR_WEIGHT,
                              trackId: str | None = None, artistId: str | None = None,
                              albumId: str | None = None, searchQuery: str | None = None,
                              trackIds: list[str] | None = None,
                              fullPlaysOnly: bool = False) -> list[dict]:
        """Tracks this user skips, ranked by shrunk skip rate (see
        _shrunkSkipRateSql), highest first.

        Rate rather than raw count: a count just resurfaces whatever is played
        most, and for albums it would literally rank by track count, since a
        longer record offers more chances to skip. A rate is per encounter, so
        length cancels out of it.

        Carries the same per-row figures the aggregate pages show (plays, time,
        first/last listen) so a skip-ranked page can render normal cards
        without touching getSongsPage - see the note there on why that query's
        is_skip=0 is left alone. A card can't tell which sort produced it, so
        anything getSongsPage returns has to be here too or the row renders
        with blanks. first_listened_at falls back to the first ENCOUNTER for a
        track that was only ever skipped.

        The filter params mirror getSongsPage()'s and mean the same things
        there - see _skippedTrackFilters.

        A single-track lookup skips the ranking machinery entirely. `trackId`
        makes the GROUP BY produce at most one row, so the shrunk rate can't
        change which row comes back - but the `lib` prior is deliberately never
        narrowed by the page's filters, so computing it meant a full read of the
        user's history to order one row against nothing. That path is not rare:
        getSong falls back to this query for every skip-only song page AND for
        every request naming a track the user has never played (just before the
        redirect), and the detail route's shell and deferred body each run it."""
        params: list = []
        rankByLibraryRate = trackId is None
        libCte = self._libraryRateCte(username, params, startTs, endTs) if rankByLibraryRate else ""
        params.append(username)
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        joins, filterClause, tracksJoined = self._skippedTrackFilters(
            params, trackId, artistId, albumId, searchQuery, trackIds, fullPlaysOnly)
        if rankByLibraryRate:
            params += [priorWeight, priorWeight]
            orderBy = f"{self._shrunkSkipRateSql()} DESC, skips DESC, track_id ASC"
        else:
            orderBy = "skips DESC, track_id ASC"   #< the same tiebreakers, minus a prior for one row
        # Merged wherever getSongsPage merges: a song skipped on both its
        # releases is one song you skip, globally and on its artist's page. An
        # album-scoped list keeps its own rows, because "what do I skip on this
        # album" is a question about that album.
        skipKey = "p.track_id"
        skipJoins = joins
        if self._mergesCanonically(trackId, artistId, albumId):
            if not tracksJoined:   #< the canonical join hangs off `t`
                skipJoins += self._tracksJoin()
            skipJoins += self._canonicalTrackJoin()
            skipKey = "c.id"
        params += [limit, offset]
        rows = self._conn().execute(
            f"""
            WITH {libCte + "," if libCte else ""}
            agg AS (
                SELECT {skipKey} AS track_id,
                       SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END) AS skips,
                       SUM(CASE WHEN p.is_skip = 0 THEN 1 ELSE 0 END) AS plays,
                       COUNT(*) AS encounters,
                       COALESCE(SUM(CASE WHEN p.is_skip = 0 THEN p.time_played ELSE 0 END), 0) AS total_time_listened,
                       COALESCE(MIN(CASE WHEN p.is_skip = 0 THEN p.played_at END), MIN(p.played_at)) AS first_listened_at,
                       COALESCE(MAX(CASE WHEN p.is_skip = 0 THEN p.played_at END), MAX(p.played_at)) AS last_played_at
                FROM plays p{skipJoins}
                WHERE p.username = ?{rangeClause}{filterClause}
                GROUP BY {skipKey}
                HAVING skips > 0
            )
            SELECT * FROM agg
            ORDER BY {orderBy}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def getSkippedTracksCount(self, username: str, startTs: float | None = None,
                               endTs: float | None = None, searchQuery: str | None = None,
                               trackIds: list[str] | None = None, fullPlaysOnly: bool = False) -> int:
        """How many tracks appear in a skip-ranked list - the paging counterpart
        to getMostSkippedTracks, filtered identically. Every track with at least
        one skip qualifies: low-volume rows are tempered by the ranking rather
        than excluded, so the count and the pages always agree.

        Deliberately takes no trackId/artistId/albumId, unlike the list it pages:
        no caller narrows a skip-ranked page to a single entity today (only the
        three Top pages offer sortBy="skips", and none of them does), and a count
        that silently ignored such a filter would size the pager off a different
        population than the list it sits under. If a detail page ever gains a
        skip sort, thread the entity through here rather than leaving it to be
        dropped in silence."""
        params: list = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        joins, filterClause, tracksJoined = self._skippedTrackFilters(
            params, None, None, None, searchQuery, trackIds, fullPlaysOnly)
        #< always global (this takes no entity narrowing), and it has to merge
        #  the same way the list does or the pager sizes a different population
        countJoins = joins
        if not tracksJoined:   #< the canonical join hangs off `t`
            countJoins += self._tracksJoin()
        countJoins += self._canonicalTrackJoin()
        row = self._conn().execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT c.id FROM plays p{countJoins}
                WHERE p.username = ?{rangeClause}{filterClause}
                GROUP BY c.id
                HAVING SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END) > 0
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def getMostSkippedArtists(self, username: str, startTs: float | None = None,
                               endTs: float | None = None, limit: int = 10, offset: int = 0,
                               priorWeight: int = SKIP_RATE_PRIOR_WEIGHT,
                               artistId: str | None = None, searchQuery: str | None = None,
                               artistIds: list[str] | None = None,
                               fullPlaysOnly: bool = False) -> list[dict]:
        """Artists this user skips, ranked like getMostSkippedTracks. A play of
        a track with several credited artists counts toward each of them,
        matching how every other artist aggregate here treats collaborations.

        Returns getArtistAggregates()' row shape plus skips/encounters: the
        Top Artists page renders these through the same card, so a key missing
        here is a field that silently blanks out under one sort.

        The filter params mirror getArtistAggregates()' - see
        _skippedArtistFilters.

        A single-artist lookup skips the ranking machinery entirely, exactly
        like getMostSkippedTracks' trackId path (see its docstring for why the
        prior is a full history read): `artistId` makes the GROUP BY produce
        at most one row, so the shrunk rate can't change which row comes back.
        getArtist falls back to this query for skip-only artist pages, on both
        the detail shell and the deferred body."""
        params: list = []
        rankByLibraryRate = artistId is None
        libCte = self._libraryRateCte(username, params, startTs, endTs) if rankByLibraryRate else ""
        params.append(username)
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        joins, filterClause = self._skippedArtistFilters(
            params, artistId, searchQuery, artistIds, fullPlaysOnly)
        if rankByLibraryRate:
            params += [priorWeight, priorWeight]
            orderBy = f"{self._shrunkSkipRateSql()} DESC, skips DESC, artist_id ASC"
        else:
            orderBy = "skips DESC, artist_id ASC"   #< the same tiebreakers, minus a prior for one row
        params += [limit, offset]
        tsongJoin, uniqueSongCount = self._uniqueSongCountSql()
        rows = self._conn().execute(
            f"""
            WITH {libCte + "," if libCte else ""}
            agg AS (
                SELECT ar.id AS artist_id, ar.name AS name, ar.url AS url, ar.image_id AS image_id,
                       SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END) AS skips,
                       SUM(CASE WHEN p.is_skip = 0 THEN 1 ELSE 0 END) AS plays,
                       COUNT(*) AS encounters,
                       COALESCE(SUM(CASE WHEN p.is_skip = 0 THEN p.time_played ELSE 0 END), 0) AS total_time_listened,
                       COALESCE(MIN(CASE WHEN p.is_skip = 0 THEN p.played_at END), MIN(p.played_at)) AS first_listened_at,
                       {uniqueSongCount}
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id{tsongJoin}
                JOIN artists ar ON ar.id = ta.artist_id{joins}
                WHERE p.username = ?{rangeClause}{filterClause}
                GROUP BY ar.id
                HAVING skips > 0
            )
            SELECT * FROM agg
            ORDER BY {orderBy}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [
            {
                "id": r["artist_id"], "name": r["name"], "url": r["url"], "imageUrl": "", "imageId": r["image_id"],
                "plays": r["plays"], "totalTimeListened": r["total_time_listened"],
                "uniqueSongCount": r["unique_song_count"], "firstListenedAt": r["first_listened_at"],
                "skips": r["skips"], "encounters": r["encounters"],
            }
            for r in rows
        ]

    def getSkippedArtistsCount(self, username: str, startTs: float | None = None,
                                endTs: float | None = None, searchQuery: str | None = None,
                                artistIds: list[str] | None = None, fullPlaysOnly: bool = False) -> int:
        """Paging counterpart to getMostSkippedArtists, filtered identically.

        Joins artists because the name search is written against ar.name, and
        grouping on ar.id keeps this structurally identical to the list. Same
        as the albums count: track_artists.artist_id is NOT NULL REFERENCES
        artists(id), so the join can't drop a credit."""
        params: list = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        joins, filterClause = self._skippedArtistFilters(
            params, None, searchQuery, artistIds, fullPlaysOnly)
        row = self._conn().execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT ar.id
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                JOIN artists ar ON ar.id = ta.artist_id{joins}
                WHERE p.username = ?{rangeClause}{filterClause}
                GROUP BY ar.id
                HAVING SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END) > 0
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def getMostSkippedAlbums(self, username: str, startTs: float | None = None,
                              endTs: float | None = None, limit: int = 10, offset: int = 0,
                              priorWeight: int = SKIP_RATE_PRIOR_WEIGHT,
                              albumId: str | None = None, searchQuery: str | None = None,
                              albumIds: list[str] | None = None,
                              fullPlaysOnly: bool = False) -> list[dict]:
        """Albums this user skips, aggregated across every track on them.

        Ranking by rate matters most here: an album's raw skip COUNT scales
        with how many tracks it has, so counting would put a 60-track
        compilation above a single that gets skipped every time it comes on. A
        rate is per encounter, so album length cancels out entirely.

        Returns getAlbumsPage()' row shape plus skips/encounters - including
        the second artists lookup, which the shared card renders whatever the
        sort is. The filter params mirror getAlbumsPage()' - see
        _skippedAlbumFilters.

        A single-album lookup skips the ranking machinery entirely - same
        shortcut and same reasoning as getMostSkippedTracks/Artists: one
        grouped row can't be reordered, and the prior costs a full history
        read. getAlbum falls back to this query for skip-only album pages."""
        params: list = []
        rankByLibraryRate = albumId is None
        libCte = self._libraryRateCte(username, params, startTs, endTs) if rankByLibraryRate else ""
        params.append(username)
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        filterClause = self._skippedAlbumFilters(params, albumId, searchQuery, albumIds, fullPlaysOnly)
        if rankByLibraryRate:
            params += [priorWeight, priorWeight]
            orderBy = f"{self._shrunkSkipRateSql()} DESC, skips DESC, album_id ASC"
        else:
            orderBy = "skips DESC, album_id ASC"   #< the same tiebreakers, minus a prior for one row
        params += [limit, offset]
        rows = self._conn().execute(
            f"""
            WITH {libCte + "," if libCte else ""}
            agg AS (
                SELECT al.id AS album_id, al.name AS name, al.url AS url, al.image_id AS image_id,
                       al.image_url AS image_url, al.total_tracks AS total_tracks,
                       al.release_date AS release_date,
                       SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END) AS skips,
                       SUM(CASE WHEN p.is_skip = 0 THEN 1 ELSE 0 END) AS plays,
                       COUNT(*) AS encounters,
                       COALESCE(SUM(CASE WHEN p.is_skip = 0 THEN p.time_played ELSE 0 END), 0) AS total_time_listened,
                       COALESCE(MIN(CASE WHEN p.is_skip = 0 THEN p.played_at END), MIN(p.played_at)) AS first_listened_at,
                       COUNT(DISTINCT p.track_id) AS unique_song_count
                FROM plays p
                JOIN tracks t ON t.id = p.track_id
                JOIN albums al ON al.id = t.album_id
                WHERE p.username = ?{rangeClause}{filterClause}
                GROUP BY al.id
                HAVING skips > 0
            )
            SELECT * FROM agg
            ORDER BY {orderBy}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        artistsByAlbum = self._artistsForAlbums([row["album_id"] for row in rows])
        return [
            {
                **self._albumStatsRowToDict(row, artistsByAlbum.get(row["album_id"], [])),
                "skips": row["skips"], "encounters": row["encounters"],
            }
            for row in rows
        ]

    def getSkippedAlbumsCount(self, username: str, startTs: float | None = None,
                               endTs: float | None = None, searchQuery: str | None = None,
                               albumIds: list[str] | None = None, fullPlaysOnly: bool = False) -> int:
        """Paging counterpart to getMostSkippedAlbums, filtered identically.

        Joins albums because the shared filter clauses are written against
        al.*, and grouping on al.id keeps this structurally identical to the
        list. It can't change the count: tracks.album_id is NOT NULL
        REFERENCES albums(id) with foreign_keys=ON, so every played track has
        exactly one album row to join to."""
        params: list = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        filterClause = self._skippedAlbumFilters(params, None, searchQuery, albumIds, fullPlaysOnly)
        row = self._conn().execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT al.id
                FROM plays p
                JOIN tracks t ON t.id = p.track_id
                JOIN albums al ON al.id = t.album_id
                WHERE p.username = ?{rangeClause}{filterClause}
                GROUP BY al.id
                HAVING SUM(CASE WHEN p.is_skip = 1 THEN 1 ELSE 0 END) > 0
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def _itemFilterClauses(self, params: list, trackId: str | None, artistId: str | None,
                            albumId: str | None) -> str:
        """The shared track/artist/album narrowing used by the play-scan
        queries; appends the bound values to `params` in clause order.

        The artist/album clauses name the entity's track set as an IN subquery
        rather than a correlated EXISTS. Both express the same set, but the
        EXISTS form is only checkable per row, so the planner drove off
        idx_plays_user_time and tested EVERY play this user has - ~1.2s for a
        well-played artist on a real library, paid several times over on one
        artist detail page (chart, heatmap, skip summary, play range, play
        log). The IN form is seekable: it resolves the track set once, then
        seeks idx_plays_user_track per track (~5ms).

        Still one row per PLAY, not per credit - the subquery is a membership
        test, so a track credited to several artists is not duplicated the way
        a join would duplicate it. tests/test_narrowed_query_equivalence.py
        pins that, and that the two forms select the same plays."""
        extraClauses = ""
        if trackId is not None:
            #< the whole merge group, not the one release: every trackId caller
            #  of this seam is the song detail page (play log, pager, chart,
            #  heatmap, skip summary, bucket span), and that page is global
            extraClauses += self._mergeGroupClause(params, trackId)
        if artistId is not None:
            extraClauses += " AND plays.track_id IN (SELECT ta.track_id FROM track_artists ta WHERE ta.artist_id = ?)"
            params.append(artistId)
        if albumId is not None:
            extraClauses += " AND plays.track_id IN (SELECT t.id FROM tracks t WHERE t.album_id = ?)"
            params.append(albumId)
        return extraClauses

    def getBucketedPlayTotals(self, username: str, startTs: float | None = None,
                               endTs: float | None = None, trackId: str | None = None,
                               artistId: str | None = None, albumId: str | None = None) -> list[dict]:
        """Play count and listened time summed per fixed PLAY_BUCKET_SECONDS
        UTC bucket, ordered by bucket start - the SQL half of the
        date-bucketed charts (time series, heatmap, streak/peak-day stats).
        The buckets are deliberately timezone-agnostic: callers map each
        bucket's start timestamp to the app's configurable IANA timezone in
        Python, which SQLite's date functions can't express correctly, while
        SQL does the per-play heavy lifting (see PLAY_BUCKET_SECONDS for why
        the mapping is lossless).

        `trackId`/`artistId`/`albumId` narrow this to one item's plays -
        reused by the song/artist/album detail pages' "play history over
        time" chart and heatmap.

        Skips are counted per bucket alongside real plays instead of being
        filtered out in the WHERE. A track whose plays are ALL skips otherwise
        produced no rows at all, so its detail page rendered a blank chart with
        nothing to say why. `plays`/`totalTimeListened` still count only real
        listens - the heatmap and streak stats read those from these same rows
        and mean listening by them - so only the added `skips` key is new."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)

        rows = conn.execute(
            f"""
            SELECT CAST(played_at / {PLAY_BUCKET_SECONDS} AS INTEGER) AS bucket,
                   SUM(CASE WHEN is_skip = 0 THEN 1 ELSE 0 END) AS plays,
                   SUM(CASE WHEN is_skip = 1 THEN 1 ELSE 0 END) AS skips,
                   COALESCE(SUM(CASE WHEN is_skip = 0 THEN time_played ELSE 0 END), 0) AS total_time
            FROM plays WHERE username = ?{rangeClause}{extraClauses}
            GROUP BY bucket
            ORDER BY bucket
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "plays": r["plays"],
                 "skips": r["skips"],
                 "totalTimeListened": r["total_time"]} for r in rows]

    def getPlaysInTimeWindows(self, username: str, windows: list[tuple[float, float]]) -> list[dict]:
        """Raw plays falling in any of the given [startTs, endTs) windows, each
        with its track name and primary (position-0) artist name - the
        CANONICAL's, so the caller's per-(year, track) aggregation counts a
        merged recording once instead of splitting it across releases and
        letting a lesser track win the year's "played most" card.

        The windows are half-open ranges on played_at, so idx_plays_user_time
        drives one index range scan per window. The previous form matched on
        strftime('%m-%d', played_at, 'unixepoch'), which no index can satisfy -
        it re-derived a calendar date for every play the user has ever made,
        on every dashboard render.

        Callers deliberately over-select (Database.getOnThisDay asks for a
        ±1-day window per year) and apply the exact local-date match in Python,
        since a play's local date can differ from its UTC date by up to a day."""
        if not windows:
            return []
        conn = self._conn()
        rangeClauses = " OR ".join("(p.played_at >= ? AND p.played_at < ?)" for _ in windows)
        params: list = [username]
        for startTs, endTs in windows:
            params.extend((startTs, endTs))
        rows = conn.execute(
            f"""
            SELECT p.played_at AS played_at, c.id AS track_id,
                   c.name AS track_name, ar.name AS artist_name
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN tracks c ON c.id = COALESCE(t.canonical_id, t.id)
            LEFT JOIN track_artists ta ON ta.track_id = c.id AND ta.position = 0
            LEFT JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? AND p.is_skip=0 AND ({rangeClauses})
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def getBucketedArtistPlayCounts(self, username: str, startTs: float | None = None,
                                     endTs: float | None = None) -> list[dict]:
        """Play counts per (fixed PLAY_BUCKET_SECONDS UTC bucket, artist id) -
        the SQL half of the artist-trend chart, replacing a row-per-
        (play, artist) transfer. A play whose track has N artists still
        counts once per artist. artist_id rides along per row so the caller
        (Database.getArtistTrend) can pick a representative id for same-
        named artists, which still merge into one series/line there exactly
        as before - this only adds data, it doesn't change that merge.
        Ordered by bucket so callers iterate in play-time order."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        rows = conn.execute(
            f"""
            SELECT CAST(p.played_at / {PLAY_BUCKET_SECONDS} AS INTEGER) AS bucket,
                   ar.id AS artist_id,
                   ar.name AS artist_name,
                   COUNT(*) AS plays
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? AND p.is_skip=0{rangeClause}
            GROUP BY bucket, ar.id, ar.name
            ORDER BY bucket, ar.name
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "artistId": r["artist_id"],
                 "artistName": r["artist_name"],
                 "plays": r["plays"]} for r in rows]

    def getPlayTotals(self, username: str, startTs: float | None = None,
                       endTs: float | None = None, fullPlaysOnly: bool = False,
                       trackIds: list[str] | None = None,
                       albumIds: list[str] | None = None) -> tuple[int, int]:
        """`fullPlaysOnly` mirrors getSongsPage()'s param of the same name -
        defaults False (unfiltered) for every existing caller (milestones,
        Wrapped, Compare, dashboard); only the Top Songs/Albums header totals
        opt in, to stay consistent with their own fullPlaysOnly-filtered list.
        `trackIds`/`albumIds` narrow the totals to an explicit id set (those
        same headers' tag filter) so the cards can't contradict the
        tag-filtered list right below them. None means no filter; an empty
        list matches nothing (see _idSetClause)."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        idClause = self._idSetClause(params, "track_id", trackIds)
        if albumIds is not None and not albumIds:
            #< explicit empty set: _trackSetClause alone emits nothing for []
            #  (its callers normally pair it with an _idSetClause on the joined
            #  table, which is what carries the `AND 0` - there's no join here)
            idClause += " AND 0"
        else:
            idClause += self._trackSetClause(params, self.ALBUM_TRACKS_SUBQUERY, albumIds,
                                             playsColumn="track_id")
        joinClause = ""
        fullPlaysClause = ""
        if fullPlaysOnly:
            joinClause = self._tracksJoin(playsAlias="plays")
            fullPlaysClause = self._fullPlaysClause(params, playsAlias="plays")
        row = conn.execute(
            f"SELECT COUNT(*) AS c, COALESCE(SUM(time_played), 0) AS total FROM plays{joinClause} "
            f"WHERE username = ? AND is_skip=0{rangeClause}{idClause}{fullPlaysClause}",
            params,
        ).fetchone()
        return row["c"], row["total"]

    def getDiscoveredSongsCount(self, username: str, startTs: float | None = None,
                                 endTs: float | None = None) -> int:
        """Count of distinct songs first played (across all time) within the year range."""
        conn = self._conn()
        # A song is "discovered in range" iff its all-time first (non-skip) play
        # falls in range - which already implies it was played in range. So group
        # once per track and keep those whose MIN(played_at) is in range, instead
        # of the old per-row correlated MIN() subquery (O(plays) vs O(plays^2)).
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                -- Grouped by the canonical: "songs discovered" counts SONGS, and
                -- one recording released twice is one discovery. MIN(played_at)
                -- then spans both releases, which is also the right answer - the
                -- song was discovered when it was first heard, on whichever of
                -- them that was.
                SELECT COALESCE(t.canonical_id, t.id) AS k
                FROM plays p JOIN tracks t ON t.id = p.track_id
                WHERE p.username = ? AND p.is_skip=0
                GROUP BY k
                HAVING MIN(p.played_at) >= ? AND MIN(p.played_at) < ?
            )
            """,
            (username, startTs, endTs),
        ).fetchone()
        return row["c"]

    def getDiscoveredArtistsCount(self, username: str, startTs: float | None = None,
                                   endTs: float | None = None) -> int:
        """Count of distinct artists first played (across all time) within the year range."""
        conn = self._conn()
        # Same shape as getDiscoveredSongsCount: an artist is "discovered in
        # range" iff their all-time first (non-skip) play - across any of their
        # tracks - is in range. Group once per artist over the plays<->artist
        # join and keep those whose MIN(played_at) is in range, instead of the
        # old correlated per-row subquery (which re-scanned every track of the
        # artist for each candidate row).
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT ta.artist_id
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                WHERE p.username = ? AND p.is_skip=0
                GROUP BY ta.artist_id
                HAVING MIN(p.played_at) >= ? AND MIN(p.played_at) < ?
            )
            """,
            (username, startTs, endTs),
        ).fetchone()
        return row["c"]

    def getMaxPlayedAtInPeriod(self, username: str, startTs: float, endTs: float) -> float | None:
        row = self._conn().execute(
            "SELECT MAX(played_at) FROM plays WHERE username = ? AND is_skip=0 AND played_at >= ? AND played_at < ?",
            (username, startTs, endTs)
        ).fetchone()
        return row[0] if row else None

    def getPlayTimeRange(self, username: str, trackId: str | None = None,
                          artistId: str | None = None, albumId: str | None = None) -> tuple[float, float] | None:
        """(earliest, latest) played_at across the user's whole history, or
        None if they have no plays - lets a caller pin an "all time" query to
        an explicit range (e.g. the Compare page aligning two users' trend
        buckets over one shared axis). `trackId`/`artistId`/`albumId` narrow
        it to one item's plays - the span the detail pages' auto trend-bucket
        resolution derives from."""
        params: list = [username]
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)
        row = self._conn().execute(
            f"SELECT MIN(played_at) AS minTs, MAX(played_at) AS maxTs FROM plays "
            f"WHERE username = ? AND is_skip=0{extraClauses}",
            params,
        ).fetchone()
        if row is None or row["minTs"] is None:
            return None
        return row["minTs"], row["maxTs"]

    def getPlayCountInPeriod(self, username: str, startTs: float, endTs: float) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM plays WHERE username = ? AND is_skip=0 AND played_at >= ? AND played_at < ?",
            (username, startTs, endTs)
        ).fetchone()
        return row[0] if row else 0
