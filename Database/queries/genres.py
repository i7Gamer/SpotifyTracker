# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from Database.queries._base import GENRE_BACKFILL_MAX_ARTIST_POSITION, PLAY_BUCKET_SECONDS, time

try:
    from Database.lastfm import foldStylizedArtistName
except ModuleNotFoundError:
    from lastfm import foldStylizedArtistName


class GenreQueries:
    """GenreQueries: genres data-access methods, mixed into Repository."""

    def getGenreCoverageCounts(self, username: str, inherited: int, startTs: float | None = None,
                                endTs: float | None = None) -> dict:
        """Play-weighted coverage counts in range, as one row: the total plays
        and, per category, how many of them had a genre. Database.getGenreCoverage
        turns these into percentages.

        Coverage as set membership: materialize each "has a (filtered) genre" id
        set once, then probe this user's distinct played tracks against them. The
        previous form fired 5 correlated EXISTS subqueries per distinct track
        (~87k index probes on a large library); this scans the three small genre
        tables once and joins, ~48% faster for identical output. tracks is
        LEFT-joined so a play whose track row is missing still counts toward the
        denominator (it just can't be album/artist covered), matching the old
        plays-only outer scan.

        The two TRACK sets are probed by the same key the genre stats read
        their rows under - the canonical once anything is merged, see
        _genreMembershipJoin. They gate those very pages, so measuring a
        different population is how the gate comes to report 80% over a chart
        that renders empty (the merged-away release carried the genre, and the
        stats look at the canonical). Albums and artists are untouched: neither
        is merged, and an album's genres stay the played release's own.

        `inherited` is the resolved 0/1 toggle, not None - see
        Database._resolveIncludeInherited, which owns that fallback."""
        params: list = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="played_at")
        #< free while nothing is merged: COALESCE(canonical_id, id) is the id
        songKey = "COALESCE(t.canonical_id, t.id)" if self._anyTrackMerges() else "p.track_id"
        params.extend([inherited, inherited])
        row = self._conn().execute(
            f"""
            WITH played AS (
                SELECT track_id, COUNT(*) AS cnt
                FROM plays
                WHERE username = ? AND is_skip = 0{rangeClause}
                GROUP BY track_id
            ),
            covered_tracks  AS (SELECT DISTINCT track_id  FROM track_genres WHERE (? OR inherited = 0)),
            own_tracks      AS (SELECT DISTINCT track_id  FROM track_genres WHERE inherited = 0),
            covered_albums  AS (SELECT DISTINCT album_id  FROM album_genres WHERE (? OR inherited = 0)),
            own_albums      AS (SELECT DISTINCT album_id  FROM album_genres WHERE inherited = 0),
            covered_artists AS (SELECT DISTINCT artist_id FROM artist_genres)
            SELECT
                COALESCE(SUM(p.cnt), 0) AS total,
                COALESCE(SUM(CASE WHEN ct.track_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS song_covered,
                COALESCE(SUM(CASE WHEN ca.album_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS album_covered,
                COALESCE(SUM(CASE WHEN car.artist_id IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS artist_covered,
                COALESCE(SUM(CASE WHEN ot.track_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS song_own,
                COALESCE(SUM(CASE WHEN oa.album_id   IS NOT NULL THEN p.cnt ELSE 0 END), 0) AS album_own
            FROM played p
            LEFT JOIN tracks t          ON t.id = p.track_id
            LEFT JOIN track_artists ta  ON ta.track_id = p.track_id AND ta.position = 0
            LEFT JOIN covered_tracks  ct  ON ct.track_id   = {songKey}
            LEFT JOIN own_tracks      ot  ON ot.track_id   = {songKey}
            LEFT JOIN covered_albums  ca  ON ca.album_id   = t.album_id
            LEFT JOIN own_albums      oa  ON oa.album_id   = t.album_id
            LEFT JOIN covered_artists car ON car.artist_id = ta.artist_id
            """,
            params,
        ).fetchone()
        return dict(row)

    def _genreMembershipJoin(self, merged: bool | None = None) -> str:
        """The plays->track_genres join, canonical-aware only when it has to be.

        A merged song's genres are the CANONICAL's rows - genre membership is a
        property of the song - but the tracks hop that implements that costs a
        PK probe per play row (+87% measured on the distribution query). While
        no merge exists the two spellings are equivalent, so the fast direct
        join runs until one does. See _anyTrackMerges.

        `merged` lets a caller that already asked pass the answer in: the probe
        is an unindexed column scan over the catalog, and one statement should
        not pay for it twice to build two halves of itself."""
        if self._anyTrackMerges() if merged is None else merged:
            return ("JOIN tracks trk ON trk.id = p.track_id\n"
                    "            JOIN track_genres g ON g.track_id = COALESCE(trk.canonical_id, trk.id)")
        return "JOIN track_genres g ON g.track_id = p.track_id"

    def _genreMembershipExists(self, merged: bool | None = None) -> str:
        """_genreMembershipJoin asked as a predicate: "this play's SONG carries
        a genre row at all", once per play rather than once per (play, genre).

        Its own spelling rather than the join because the two answer different
        questions about cardinality, and the one caller is a denominator: the
        join multiplies a play by its genre count, so counting rows through it
        would need COUNT(DISTINCT p.id) over a set several times larger, where
        EXISTS short-circuits on the first genre row. What it must NOT differ
        about is which song's rows count - a numerator built on the join and a
        denominator built on the played id disagree by exactly the merged
        members, and their ratio then exceeds 100%. Carries one bound
        parameter, the inherited toggle, exactly like the join's WHERE arm.

        Same gate and same reason as its sibling: while nothing is merged the
        canonical hop buys nothing, so the direct form runs (see
        _anyTrackMerges)."""
        if self._anyTrackMerges() if merged is None else merged:
            return ("EXISTS (SELECT 1 FROM tracks trk "
                    "JOIN track_genres g ON g.track_id = COALESCE(trk.canonical_id, trk.id) "
                    "WHERE trk.id = p.track_id AND (? OR g.inherited = 0))")
        return ("EXISTS (SELECT 1 FROM track_genres g "
                "WHERE g.track_id = p.track_id AND (? OR g.inherited = 0))")

    def getGenrePlayCounts(self, username: str, inherited: int, startTs: float | None = None,
                            endTs: float | None = None, limit: int | None = None) -> list[dict]:
        """[{genre, plays}] over the range, most-played first (name breaks ties -
        Last.fm counts tie constantly). A play with N genres counts once per
        genre, the standard reading for tag distributions. Track-level genres
        only: they're the finest granularity, and inherited rows already carry
        artist genres down to tag-less tracks when the toggle allows."""
        params: list = [inherited, username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        limitClause = ""
        if limit is not None:
            limitClause = " LIMIT ?"
            params.append(limit)
        rows = self._conn().execute(
            f"""
            SELECT g.genre AS genre, COUNT(*) AS plays
            FROM plays p
            {self._genreMembershipJoin()}
            WHERE (? OR g.inherited = 0) AND p.username = ? AND p.is_skip = 0{rangeClause}
            GROUP BY g.genre
            ORDER BY plays DESC, g.genre ASC{limitClause}
            """,
            params,
        ).fetchall()
        return [{"genre": row["genre"], "plays": row["plays"]} for row in rows]

    # ---- Catalog: Last.fm genres ---------------------------------------------
    # Genres are catalog data (shared across users) fetched from Last.fm by the
    # per-user genre backfiller (Database.startLastfmGenreBackfiller). Queue
    # semantics mirror getAlbumsMissingMetadata: lastfm_attempted_at rate-limits
    # retries, entities holding own (non-inherited) genre rows never requeue.

    _GENRE_TABLES = {
        "artist": ("artist_genres", "artist_id"),
        "album": ("album_genres", "album_id"),
        "track": ("track_genres", "track_id"),
    }

    def _replaceGenres(self, kind: str, entityId: str, genres: list[str], inherited: bool) -> None:
        table, idColumn = self._GENRE_TABLES[kind]
        conn = self._conn()
        with conn:
            conn.execute(f"DELETE FROM {table} WHERE {idColumn}=?", (entityId,))
            if kind == "artist":
                conn.executemany(
                    f"INSERT INTO {table} ({idColumn}, genre, position) VALUES (?, ?, ?)",
                    [(entityId, genre, position) for position, genre in enumerate(genres)],
                )
            else:
                conn.executemany(
                    f"INSERT INTO {table} ({idColumn}, genre, position, inherited) VALUES (?, ?, ?, ?)",
                    [(entityId, genre, position, int(inherited)) for position, genre in enumerate(genres)],
                )

    def replaceArtistGenres(self, artistId: str, genres: list[str]) -> None:
        self._replaceGenres("artist", artistId, genres, inherited=False)

    def replaceAlbumGenres(self, albumId: str, genres: list[str], inherited: bool = False) -> None:
        self._replaceGenres("album", albumId, genres, inherited)

    def replaceTrackGenres(self, trackId: str, genres: list[str], inherited: bool = False) -> None:
        self._replaceGenres("track", trackId, genres, inherited)

    def getArtistGenres(self, artistId: str) -> list[str]:
        rows = self._conn().execute(
            "SELECT genre FROM artist_genres WHERE artist_id=? ORDER BY position", (artistId,)
        ).fetchall()
        return [r["genre"] for r in rows]

    def getAlbumGenres(self, albumId: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT genre, inherited FROM album_genres WHERE album_id=? ORDER BY position", (albumId,)
        ).fetchall()
        return [{"genre": r["genre"], "inherited": bool(r["inherited"])} for r in rows]

    def getTrackGenres(self, trackId: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT genre, inherited FROM track_genres WHERE track_id=? ORDER BY position", (trackId,)
        ).fetchall()
        return [{"genre": r["genre"], "inherited": bool(r["inherited"])} for r in rows]

    # ---- Batched per-id genre lookups (track/album/artist card badges) --------
    # One query for a whole page of cards instead of one per card: a list page
    # renders up to a few hundred, and an artist detail page renders every song
    # the artist has (unbounded). Same rows, same position ordering, keyed by id.

    def _genresByIdForIds(self, table: str, idColumn: str, ids: list[str],
                          withInherited: bool) -> dict[str, list]:
        if not ids:
            return {}
        uniqueIds = list(dict.fromkeys(ids))   #< preserves order, drops repeats
        placeholders = ",".join("?" for _ in uniqueIds)
        inheritedColumn = ", inherited" if withInherited else ""
        rows = self._conn().execute(
            f"SELECT {idColumn} AS entity_id, genre{inheritedColumn} FROM {table} "
            f"WHERE {idColumn} IN ({placeholders}) ORDER BY {idColumn}, position",
            uniqueIds,
        ).fetchall()
        result: dict[str, list] = {entityId: [] for entityId in uniqueIds}
        for row in rows:
            value = ({"genre": row["genre"], "inherited": bool(row["inherited"])}
                     if withInherited else row["genre"])
            result[row["entity_id"]].append(value)
        return result

    def getTrackGenresForIds(self, trackIds: list[str]) -> dict[str, list[dict]]:
        """{trackId: [{genre, inherited}, ...]} for every requested id (ids with
        no rows map to []), position-ordered like getTrackGenres."""
        return self._genresByIdForIds("track_genres", "track_id", trackIds, withInherited=True)

    def getAlbumGenresForIds(self, albumIds: list[str]) -> dict[str, list[dict]]:
        """{albumId: [{genre, inherited}, ...]} - see getTrackGenresForIds."""
        return self._genresByIdForIds("album_genres", "album_id", albumIds, withInherited=True)

    def getArtistGenresForIds(self, artistIds: list[str]) -> dict[str, list[str]]:
        """{artistId: [genre, ...]} - artists carry no inherited flag."""
        return self._genresByIdForIds("artist_genres", "artist_id", artistIds, withInherited=False)

    def _markLastfmAttempted(self, table: str, ids: list[str]) -> None:
        """Stamp entities as processed so they leave the backfill queue -
        including empty/not-found lookups, which would otherwise be re-fetched
        every cycle forever (mirrors markAlbumsBackfillAttempted)."""
        if not ids:
            return
        conn = self._conn()
        placeholders = ",".join("?" for _ in ids)
        with conn:
            conn.execute(
                f"UPDATE {table} SET lastfm_attempted_at = ? WHERE id IN ({placeholders})",
                [time.time(), *ids],
            )

    def markArtistsLastfmAttempted(self, artistIds: list[str]) -> None:
        self._markLastfmAttempted("artists", artistIds)

    def markAlbumsLastfmAttempted(self, albumIds: list[str]) -> None:
        self._markLastfmAttempted("albums", albumIds)

    def markTracksLastfmAttempted(self, trackIds: list[str]) -> None:
        self._markLastfmAttempted("tracks", trackIds)

    def requeueLastfmEntitiesWithoutOwnGenres(self) -> int:
        """Clears lastfm_attempted_at wherever the entity holds no own
        (non-inherited) genre rows, re-entering it into the backfill queues
        immediately - the 1.19.0 -> 1.20.0 migration's lever after lookup
        improvements. Inherited rows stay in place so stats keep working
        until the re-run replaces them. Returns how many were requeued."""
        conn = self._conn()
        cleared = 0
        with conn:
            for table, genreTable, idColumn in (
                ("tracks", "track_genres", "track_id"),
                ("albums", "album_genres", "album_id"),
            ):
                cleared += conn.execute(
                    f"""
                    UPDATE {table} SET lastfm_attempted_at = NULL
                    WHERE lastfm_attempted_at IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM {genreTable} g
                                      WHERE g.{idColumn} = {table}.id AND g.inherited = 0)
                    """
                ).rowcount
            cleared += conn.execute(
                """
                UPDATE artists SET lastfm_attempted_at = NULL
                WHERE lastfm_attempted_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM artist_genres g WHERE g.artist_id = artists.id)
                """
            ).rowcount
        return cleared

    def _requeueCanonicalForGenres(self, conn, canonicalId: str) -> None:
        """The lever below, for ONE canonical and inside the caller's
        transaction - what a merge that just moved a group calls.

        A merge changes which release every genre read resolves to. If the new
        canonical was already marked attempted, nothing would look it up again
        and the song stays genre-less for good. No-ops when it already carries
        own rows, and when it was never attempted (NULL already).

        Callers must fire this only when the group actually MOVED: run
        unconditionally on the matcher's daily pass, a canonical that is
        genuinely tag-less everywhere would be re-looked-up every single day
        forever, since it can never satisfy the own-rows test."""
        conn.execute(
            """
            UPDATE tracks SET lastfm_attempted_at = NULL
            WHERE id = ? AND lastfm_attempted_at IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM track_genres g
                              WHERE g.track_id = tracks.id AND g.inherited = 0)
            """,
            (canonicalId,),
        )

    def requeueCanonicalsOfMergedGroupsWithoutOwnGenres(self) -> int:
        """Clears lastfm_attempted_at on every merge-group CANONICAL that holds
        no own genre rows - the one-time lever for libraries merged before the
        backfill queue learned to ask about the song (2026-09-03 review, H1).

        Those groups are the stuck case: the member was looked up and marked,
        the canonical every read resolves to was not, and nothing would ever
        requeue it. Scoped to canonicals of real groups on purpose - a plain
        track that came back genuinely tag-less is not this lever's business,
        and requeuing it would re-spend Last.fm quota on an unchanged answer.

        Nothing is copied or deleted: the members keep their rows and their
        stamps, so unmerging stays lossless and the canonical gets its OWN
        Last.fm answer rather than a differently-titled release's. Returns how
        many were requeued.

        `id IN (SELECT canonical_id ...)` rather than a correlated EXISTS:
        canonical_id is deliberately unindexed (see the tracks table), so the
        EXISTS spelling is a full scan PER candidate row."""
        conn = self._conn()
        with conn:
            return conn.execute(
                """
                UPDATE tracks SET lastfm_attempted_at = NULL
                WHERE lastfm_attempted_at IS NOT NULL
                  AND id IN (SELECT canonical_id FROM tracks
                             WHERE canonical_id IS NOT NULL)
                  AND NOT EXISTS (SELECT 1 FROM track_genres g
                                  WHERE g.track_id = tracks.id AND g.inherited = 0)
                """
            ).rowcount

    def requeueAlbumsLastfmWithoutOwnGenres(self) -> int:
        """Clears lastfm_attempted_at for albums holding no own (non-inherited)
        genre rows, re-entering them into the backfill queue immediately - the
        1.24.0 -> 1.25.0 migration's lever after the album.getinfo fallback fix
        (album.gettoptags was confirmed to miss real tag data for ~46% of
        tag-less albums that getinfo has). Scoped to albums only, unlike
        requeueLastfmEntitiesWithoutOwnGenres: that fix doesn't touch artist or
        track lookups, so requeuing those too would just re-run unchanged
        results against the shared rate limiter for no benefit. Existing
        inherited rows stay in place so genre stats keep working until the
        re-run replaces them. Returns how many albums were requeued."""
        conn = self._conn()
        with conn:
            cleared = conn.execute(
                """
                UPDATE albums SET lastfm_attempted_at = NULL
                WHERE lastfm_attempted_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM album_genres g
                                  WHERE g.album_id = albums.id AND g.inherited = 0)
                """
            ).rowcount
        return cleared

    def requeueArtistsWithFoldableNamesWithoutGenres(self) -> int:
        """Requeues artists whose Last.fm lookup was attempted before
        getArtistTopTags gained foldStylizedArtistName's stylized-letter/
        decorative-mark retry ("HUGO" recovers real tags where the stored
        "HUGØ" doesn't; confirmed live against the API, not a guess). Only
        artists whose name actually changes under folding are cleared - the
        fold test is a Python function, so candidates are filtered in Python
        rather than SQL, same as requeueDecoratedAlbumsWithoutBios. Returns
        how many artists were requeued."""
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT id, name FROM artists
            WHERE lastfm_attempted_at IS NOT NULL
              AND id NOT IN (SELECT DISTINCT artist_id FROM artist_genres)
            """
        ).fetchall()
        foldableIds = [row["id"] for row in rows
                       if row["name"] and foldStylizedArtistName(row["name"]) != row["name"]]
        if not foldableIds:
            return 0
        with conn:
            conn.executemany(
                "UPDATE artists SET lastfm_attempted_at = NULL WHERE id = ?",
                [(artistId,) for artistId in foldableIds],
            )
        return len(foldableIds)

    def getArtistsByGenres(self, username: str, genres: list[str],
                           excludeArtistIds: list[str], limit: int) -> list[dict]:
        """Recommendation candidates: artists credited on this user's played
        tracks whose own (artist_genres) tags overlap `genres`, excluding
        `excludeArtistIds`. Ranked most-relevant first: more shared genres
        wins, then FEWER plays (surface under-played artists that fit the
        user's taste), then id for a stable order. Each row carries
        sharedGenreCount and the matched genre names (for a "why" chip)."""
        if not genres:
            return []
        conn = self._conn()
        genrePlaceholders = ",".join("?" for _ in genres)
        # Decomposed so the play-count and genre-match aggregations never share
        # a join. Joining plays straight to artist_genres multiplied every play
        # row by the artist's matching-genre count, forcing COUNT(DISTINCT p.id)
        # over that inflated set (~1.1s on a large library). Counting plays and
        # genre matches independently, then joining the two per-artist
        # aggregates, is ~90% faster for byte-identical output.
        params: list = [username, *genres]
        excludeClause = ""
        if excludeArtistIds:
            excludeClause = f" WHERE ar.id NOT IN ({','.join('?' for _ in excludeArtistIds)})"
            params.extend(excludeArtistIds)
        params.append(limit)
        rows = conn.execute(
            f"""
            WITH artist_plays AS (
                SELECT ta.artist_id AS artist_id, COUNT(DISTINCT p.id) AS play_count
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                WHERE p.username = ? AND p.is_skip = 0
                GROUP BY ta.artist_id
            ),
            artist_match AS (
                SELECT artist_id,
                       COUNT(DISTINCT genre) AS shared_genre_count,
                       GROUP_CONCAT(DISTINCT genre) AS matched_genres
                FROM artist_genres
                WHERE genre IN ({genrePlaceholders})
                GROUP BY artist_id
            )
            SELECT ar.id AS id, ar.name AS name, ar.image_id AS image_id,
                   ap.play_count AS play_count,
                   am.shared_genre_count AS shared_genre_count,
                   am.matched_genres AS matched_genres
            FROM artist_match am
            JOIN artist_plays ap ON ap.artist_id = am.artist_id
            JOIN artists ar ON ar.id = am.artist_id{excludeClause}
            ORDER BY shared_genre_count DESC, play_count ASC, ar.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            {
                "id": r["id"], "name": r["name"], "imageId": r["image_id"],
                "playCount": r["play_count"], "sharedGenreCount": r["shared_genre_count"],
                "matchedGenres": r["matched_genres"].split(",") if r["matched_genres"] else [],
            }
            for r in rows
        ]

    # ---- Genres page: trends, per-genre stats, per-genre top lists ----------

    def getBucketedGenrePlayCounts(self, username: str, genres: list[str], includeInherited: int,
                                    startTs: float | None, endTs: float | None) -> list[dict]:
        """Play counts per (fixed PLAY_BUCKET_SECONDS UTC bucket, genre) for
        plays on tracks tagged with any of `genres`, over track_genres (the
        finest granularity, matching getGenreDistribution) - the genre-scoped
        analogue of getBucketedArtistPlayCounts.

        Pre-aggregating in SQL replaces a row-per-(play, genre) transfer: a
        play on a track carrying five tags used to ship five rows, and the
        caller then bucketed each one in Python. The 15-minute bucket is finer
        than any chart bucket (hour/day/week/month) and finer than any
        real-world UTC offset granularity, so folding these up into local-time
        buckets stays exact. Empty `genres` -> []."""
        if not genres:
            return []
        conn = self._conn()
        genrePlaceholders = ",".join("?" for _ in genres)
        params: list = [includeInherited, *genres, username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        rows = conn.execute(
            f"""
            SELECT CAST(p.played_at / {PLAY_BUCKET_SECONDS} AS INTEGER) AS bucket,
                   g.genre AS genre,
                   COUNT(*) AS plays
            FROM plays p
            {self._genreMembershipJoin()}
            WHERE (? OR g.inherited = 0) AND g.genre IN ({genrePlaceholders})
              AND p.username = ? AND p.is_skip = 0{rangeClause}
            GROUP BY bucket, g.genre
            ORDER BY bucket
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "genre": r["genre"],
                 "plays": r["plays"]} for r in rows]

    def getGenreBucketedPlayTotals(self, username: str, genre: str, includeInherited: int,
                                   startTs: float | None = None, endTs: float | None = None) -> list[dict]:
        """Plays/time summed per fixed PLAY_BUCKET_SECONDS UTC bucket for plays
        on tracks tagged `genre` - the genre-scoped analogue of
        getBucketedPlayTotals, feeding the per-genre listening-clock heatmap.
        Same timezone-agnostic bucket contract: the caller maps each bucket to
        a local weekday/hour cell."""
        conn = self._conn()
        params: list = [includeInherited, genre, username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        rows = conn.execute(
            f"""
            SELECT CAST(p.played_at / {PLAY_BUCKET_SECONDS} AS INTEGER) AS bucket,
                   COUNT(*) AS plays,
                   COALESCE(SUM(p.time_played), 0) AS total_time
            FROM plays p
            {self._genreMembershipJoin()} AND (? OR g.inherited = 0) AND g.genre = ?
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
            GROUP BY bucket
            ORDER BY bucket
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "plays": r["plays"],
                 "totalTimeListened": r["total_time"]} for r in rows]

    def getArtistCountsByGenres(self, username: str, genres: list[str],
                                includeInherited: int) -> dict:
        """{genre: distinct artist count} over the given genres - how many
        different artists the user has played within each genre (breadth),
        complementing the play-weighted distribution/share (depth). Genres with
        no plays are omitted. Empty input -> {}."""
        if not genres:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in genres)
        params: list = [includeInherited, *genres, username]
        rows = conn.execute(
            f"""
            SELECT g.genre AS genre, COUNT(DISTINCT ar.id) AS artist_count
            FROM plays p
            {self._genreMembershipJoin()} AND (? OR g.inherited = 0)
                AND g.genre IN ({placeholders})
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? AND p.is_skip = 0
            GROUP BY g.genre
            """,
            params,
        ).fetchall()
        return {r["genre"]: r["artist_count"] for r in rows}

    def getGenrePlayStats(self, username: str, genre: str, includeInherited: int,
                          startTs: float | None, endTs: float | None) -> dict:
        """{plays, listenMs, firstPlayedTs} for one genre's plays."""
        conn = self._conn()
        params: list = [includeInherited, genre, username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS plays, COALESCE(SUM(p.time_played), 0) AS listen_ms,
                   MIN(p.played_at) AS first_ts
            FROM plays p
            {self._genreMembershipJoin()}
            WHERE (? OR g.inherited = 0) AND g.genre = ? AND p.username = ? AND p.is_skip = 0{rangeClause}
            """,
            params,
        ).fetchone()
        return {"plays": row["plays"], "listenMs": row["listen_ms"], "firstPlayedTs": row["first_ts"]}

    def getTotalGenreTaggedPlays(self, username: str, includeInherited: int,
                                 startTs: float | None, endTs: float | None) -> int:
        """How many of the user's plays land on a SONG carrying any genre row
        (respecting the inherited toggle) - the denominator for a genre's
        listening share.

        Canonical-aware through _genreMembershipExists, because the numerator
        beside it (getGenrePlayStats) is: a merged member's plays are credited
        with the canonical's genres up top, so counting only releases carrying
        a row of their own down here dropped exactly those plays out of the
        denominator. Last.fm looks genres up by track NAME and a merge group's
        releases are named differently, so one member answering where the
        other does not is the steady state rather than an edge case - the
        share then read over 100% (measured 12/9 = 133.3% on a 3+9 pair)."""
        conn = self._conn()
        params: list = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        params.append(includeInherited)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM plays p
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
              AND {self._genreMembershipExists()}
            """,
            params,
        ).fetchone()
        return row["c"]

    def getTopArtistsForGenre(self, username: str, genre: str, includeInherited: int,
                              limit: int, startTs: float | None = None,
                              endTs: float | None = None) -> list[dict]:
        """Top artists (by play count) credited on tracks tagged `genre`."""
        conn = self._conn()
        params: list = [includeInherited, genre, username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT ar.id AS id, ar.name AS name, ar.image_id AS image_id,
                   COUNT(DISTINCT p.id) AS play_count
            FROM plays p
            {self._genreMembershipJoin()} AND (? OR g.inherited = 0) AND g.genre = ?
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
            GROUP BY ar.id
            ORDER BY play_count DESC, ar.name COLLATE NOCASE ASC, ar.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "imageId": r["image_id"],
                 "playCount": r["play_count"]} for r in rows]

    def getTopTracksForGenre(self, username: str, genre: str, includeInherited: int,
                             limit: int, startTs: float | None = None,
                             endTs: float | None = None) -> list[dict]:
        """Top tracks (by play count) tagged `genre`, each with its primary
        (position-0) artist name."""
        conn = self._conn()
        params: list = [includeInherited, genre, username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        params.append(limit)
        #< asked ONCE for the statement: the probe is an unindexed column scan
        #  over the whole catalog, and this used to run it twice - here and
        #  again inside _genreMembershipJoin - to answer the same question
        merged = self._anyTrackMerges()
        rows = conn.execute(
            f"""
            SELECT t.id AS id, t.name AS name, t.image_id AS image_id,
                   ar.name AS artist_name, COUNT(p.id) AS play_count
            FROM plays p
            {self._genreMembershipJoin(merged)} AND (? OR g.inherited = 0) AND g.genre = ?
            -- the CANONICAL row: this is a global list, so a song split across
            -- releases is one row with the group's whole count
            JOIN tracks t ON t.id = {"COALESCE(trk.canonical_id, trk.id)" if merged else "p.track_id"}
            LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            LEFT JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? AND p.is_skip = 0{rangeClause}
            GROUP BY t.id
            ORDER BY play_count DESC, t.name COLLATE NOCASE ASC, t.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "imageId": r["image_id"],
                 "artistName": r["artist_name"], "playCount": r["play_count"]} for r in rows]

    def getArtistLastfmState(self, artistId: str) -> dict:
        """Attempt stamp + current genres for one artist - the inheritance
        decision for a tag-less track/album re-reads this at process time (the
        same worker cycle has usually just processed the artist batch)."""
        row = self._conn().execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id=?", (artistId,)
        ).fetchone()
        return {
            "attempted_at": row["lastfm_attempted_at"] if row else None,
            "genres": self.getArtistGenres(artistId),
        }

    def getArtistsMissingGenres(self, limit: int, username: str | None = None) -> list[dict]:
        """Played artists credited within the first
        GENRE_BACKFILL_MAX_ARTIST_POSITION+1 track_artists positions (not just
        the position-0 primary - feature/collab-only artists get queued too)
        still needing a Last.fm lookup, most-played first. `username` scopes
        the queue (and the play counts) to one user's history; None is the
        global queue a worker falls back to once its owner's entities are
        done. Because a track can now match more than one credited artist,
        play_count reflects "plays on tracks where this artist is credited
        within the cutoff", not "plays where this artist is primary"."""
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - self.getGenreBackfillRetrySeconds(), limit])
        rows = conn.execute(
            f"""
            SELECT ar.id AS id, ar.name AS name, COUNT(*) AS play_count
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
                AND ta.position <= {GENRE_BACKFILL_MAX_ARTIST_POSITION}
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE {userClause}(ar.lastfm_attempted_at IS NULL
                   OR (ar.lastfm_attempted_at < ?
                       AND NOT EXISTS (SELECT 1 FROM artist_genres ag WHERE ag.artist_id = ar.id)))
            GROUP BY ar.id
            ORDER BY play_count DESC, ar.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def getAlbumsMissingGenres(self, limit: int, username: str | None = None) -> list[dict]:
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - self.getGenreBackfillRetrySeconds(), limit])
        rows = conn.execute(
            f"""
            SELECT al.id AS id, al.name AS name, COUNT(*) AS play_count
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN albums al ON al.id = t.album_id
            WHERE {userClause}(al.lastfm_attempted_at IS NULL
                   OR (al.lastfm_attempted_at < ?
                       AND NOT EXISTS (SELECT 1 FROM album_genres ag
                                       WHERE ag.album_id = al.id AND ag.inherited = 0)))
            GROUP BY al.id
            ORDER BY play_count DESC, al.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def getTracksMissingGenres(self, limit: int, username: str | None = None) -> list[dict]:
        """Rows carry the primary artist - both the track.getTopTags lookup and
        genre inheritance need it. Tracks with no position-0 artist row are
        structurally excluded: they can neither be looked up nor inherit.

        Asks about the SONG, not the played release, once anything is merged:
        every genre READ resolves the group (see _genreMembershipJoin), so a
        lookup written to a member is an answer no reader can reach - and the
        canonical, ranked by its own plays, sank behind every ordinary track
        (2026-09-03 review, H1).

        The merged spelling aggregates the plays side FIRST and then joins one
        explicit canonical row. That is what keeps name/album/artist
        deterministic: grouping on COALESCE(canonical_id, id) while still
        selecting t.name would leave bare columns under the GROUP BY, and the
        lookup is BY NAME - so it would query Last.fm with one release's title
        and store the answer under another, differently run to run.

        Same gate and same reason as _genreMembershipJoin: the canonical hop is
        a PK probe per play row, so the original statement runs verbatim until
        a merge exists. See _anyTrackMerges."""
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - self.getGenreBackfillRetrySeconds(), limit])
        if self._anyTrackMerges():
            #< userClause is a PREFIX ('p.username = ? AND '), so it needs a
            #  condition after it - the CTE has none of its own
            sql = f"""
            WITH grouped AS (
                SELECT COALESCE(t.canonical_id, t.id) AS cid, COUNT(*) AS play_count
                FROM plays p
                JOIN tracks t ON t.id = p.track_id
                WHERE {userClause}1
                GROUP BY cid
            )
            SELECT c.id AS id, c.name AS name, c.album_id AS album_id,
                   ar.id AS artist_id, ar.name AS artist_name,
                   grouped.play_count AS play_count
            FROM grouped
            JOIN tracks c ON c.id = grouped.cid
            JOIN track_artists ta ON ta.track_id = c.id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE c.lastfm_attempted_at IS NULL
               OR (c.lastfm_attempted_at < ?
                   AND NOT EXISTS (SELECT 1 FROM track_genres tg
                                   WHERE tg.track_id = c.id AND tg.inherited = 0))
            ORDER BY play_count DESC, c.id ASC
            LIMIT ?
            """
        else:
            sql = f"""
            SELECT t.id AS id, t.name AS name, t.album_id AS album_id,
                   ar.id AS artist_id, ar.name AS artist_name,
                   COUNT(*) AS play_count
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE {userClause}(t.lastfm_attempted_at IS NULL
                   OR (t.lastfm_attempted_at < ?
                       AND NOT EXISTS (SELECT 1 FROM track_genres tg
                                       WHERE tg.track_id = t.id AND tg.inherited = 0)))
            GROUP BY t.id
            ORDER BY play_count DESC, t.id ASC
            LIMIT ?
            """
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @staticmethod
    def _queueUserClause(params: list, username: str | None) -> str:
        """'p.username = ? AND ' with the bind appended, or '' for the global
        queue - the username filter must be dropped entirely (not IS-NULL-
        parameterized) so SQLite can still range the (username, ...) index
        when scoped and skip it when not."""
        if username is None:
            return ""
        params.append(username)
        return "p.username = ? AND "

    def getArtistLastfmLookupRow(self, artistId: str) -> dict | None:
        """{"id", "name"} for one artist, ignoring attempt state entirely -
        unlike getArtistsMissingGenres, this backs the admin "refresh Last.fm
        data" action, which must work even on an already-attempted artist."""
        row = self._conn().execute(
            "SELECT id, name FROM artists WHERE id = ?", (artistId,)
        ).fetchone()
        return dict(row) if row else None

    def getAlbumLastfmLookupRow(self, albumId: str) -> dict | None:
        """{"id", "name"} for one album - mirrors getArtistLastfmLookupRow."""
        row = self._conn().execute(
            "SELECT id, name FROM albums WHERE id = ?", (albumId,)
        ).fetchone()
        return dict(row) if row else None

    def getTrackLastfmLookupRow(self, trackId: str) -> dict | None:
        """{"id", "name", "album_id", "artist_id", "artist_name"} for one
        track via its position-0 artist - same row shape as
        getTracksMissingGenres, but for exactly one id and ignoring attempt
        state (see getArtistLastfmLookupRow). None if the track has no
        position-0 artist, the same structural exclusion
        getTracksMissingGenres applies."""
        row = self._conn().execute(
            f"""
            SELECT t.id AS id, t.name AS name, t.album_id AS album_id,
                   ar.id AS artist_id, ar.name AS artist_name
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE t.id = ?
            """,
            (trackId,),
        ).fetchone()
        return dict(row) if row else None

    # ---- Instance-wide admin insights -----------------------------------------
    # Catalog/instance-scoped numbers for the /admin page - unlike the per-user
    # stats above (getGenreCoverage, getBiographyCoverage), these aren't
    # filtered by any one user's plays, so they reflect backfill progress and
    # activity across the whole shared instance.

    def getCatalogGenreCoverage(self, includeInherited: bool | None = None) -> dict:
        """Catalog-wide analogue of Database.getGenreCoverage: the share of
        all tracks/albums/artists (not plays) that carry at least one
        Last.fm genre row. Mirrors GENRE_COVERAGE_CATEGORIES = ("song",
        "album", "artist") from Database/database.py - not imported here to
        avoid a circular import (database.py imports this module)."""
        if includeInherited is None:
            includeInherited = self.isInheritedGenresEnabled()
        inherited = 1 if includeInherited else 0
        conn = self._conn()

        def category(table: str, genreTable: str, idColumn: str, hasInherited: bool) -> dict:
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if hasInherited:
                covered = conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT e.id) FROM {table} e
                    JOIN {genreTable} g ON g.{idColumn} = e.id
                    WHERE (? OR g.inherited = 0)
                    """,
                    (inherited,),
                ).fetchone()[0]
                # With inherited genres OFF the query above IS this one -
                # `(0 OR g.inherited = 0)` is `g.inherited = 0` - so running it
                # again asked the same whole-table question twice.
                own_covered = covered if not inherited else conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT e.id) FROM {table} e
                    JOIN {genreTable} g ON g.{idColumn} = e.id
                    WHERE g.inherited = 0
                    """
                ).fetchone()[0]
            else:
                covered = conn.execute(
                    f"SELECT COUNT(DISTINCT e.id) FROM {table} e JOIN {genreTable} g ON g.{idColumn} = e.id"
                ).fetchone()[0]
                own_covered = covered
            percent = round(covered / total * 100, 1) if total else 0.0
            own_percent = round(own_covered / total * 100, 1) if total else 0.0
            return {
                "covered": covered,
                "own_covered": own_covered,
                "total": total,
                "percent": percent,
                "own_percent": own_percent,
                "ownPercent": own_percent,
            }

        coverage = {
            "song": category("tracks", "track_genres", "track_id", True),
            "album": category("albums", "album_genres", "album_id", True),
            "artist": category("artists", "artist_genres", "artist_id", False),
        }
        percents = [coverage["song"]["percent"], coverage["album"]["percent"], coverage["artist"]["percent"]]
        coverage["overall"] = {"percent": round(sum(percents) / len(percents), 1)}
        return coverage

    def getTrackSecondaryArtists(self, trackId: str) -> list[dict]:
        """Credited artists for a track at 0 < position <= GENRE_BACKFILL_MAX_ARTIST_POSITION
        (positions 1 through 4), ordered by position ASC."""
        rows = self._conn().execute(
            f"""
            SELECT ta.artist_id AS artist_id, ar.name AS artist_name, ta.position AS position
            FROM track_artists ta
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE ta.track_id = ? AND ta.position > 0 AND ta.position <= ?
            ORDER BY ta.position ASC
            """,
            (trackId, GENRE_BACKFILL_MAX_ARTIST_POSITION),
        ).fetchall()
        return [dict(r) for r in rows]

    def requeueArtistsWithTransformedNamesWithoutGenres(self) -> int:
        """Requeues artists whose Last.fm lookup was attempted before normalizeArtistLookupName
        gained slash/plus/credit transformations. Returns how many artists were requeued."""
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT id, name FROM artists
            WHERE lastfm_attempted_at IS NOT NULL
              AND id NOT IN (SELECT DISTINCT artist_id FROM artist_genres)
            """
        ).fetchall()
        from Database.lastfm import normalizeArtistLookupName
        transformable_ids = [row["id"] for row in rows
                             if row["name"] and len(normalizeArtistLookupName(row["name"])) > 0]
        if not transformable_ids:
            return 0
        with conn:
            conn.executemany(
                "UPDATE artists SET lastfm_attempted_at = NULL WHERE id = ?",
                [(artistId,) for artistId in transformable_ids],
            )
        return len(transformable_ids)

