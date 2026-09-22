# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from Database.queries._base import GENRE_BACKFILL_MAX_ARTIST_POSITION, time

try:
    from Database.lastfm import cleanLookupName
except ModuleNotFoundError:
    from lastfm import cleanLookupName


class BioQueries:
    """BioQueries: bios data-access methods, mixed into Repository."""

    def getArtistBioState(self, artistId: str) -> dict:
        """{"bio", "attempted_at"} for one artist - lazyFetchArtistBio's
        claim check. The on-demand lazy fetch never retries an artist once
        attempted, same permanent-once-tried philosophy as artist images
        (IMAGE_STATUS_FAILED); the background biography backfiller
        (getArtistsMissingBiographies) is the one that revisits a
        definitive-empty result later, on its own 30-day cycle."""
        row = self._conn().execute(
            "SELECT bio, bio_attempted_at FROM artists WHERE id=?", (artistId,)
        ).fetchone()
        if row is None:
            return {"bio": None, "attempted_at": None}
        return {"bio": row["bio"], "attempted_at": row["bio_attempted_at"]}

    def setArtistBio(self, artistId: str, bio: str | None) -> None:
        """Stores the fetch result (bio text, or None when Last.fm has
        nothing usable) and stamps bio_attempted_at in one call - there's no
        separate "mark attempted" step like the genre tables, since a bio
        fetch has no list of rows to replace."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE artists SET bio = ?, bio_attempted_at = ? WHERE id = ?",
                (bio, time.time(), artistId),
            )

    def getAlbumBioState(self, albumId: str) -> dict:
        """{"bio", "attempted_at"} for one album - lazyFetchAlbumBio's claim
        check. Same permanent-once-tried contract as getArtistBioState: the
        on-demand lazy fetch never retries an album once attempted; the
        background album biography backfiller (getAlbumsMissingBiographies)
        is the one that revisits a definitive-empty result later, on its own
        30-day cycle."""
        row = self._conn().execute(
            "SELECT bio, bio_attempted_at FROM albums WHERE id=?", (albumId,)
        ).fetchone()
        if row is None:
            return {"bio": None, "attempted_at": None}
        return {"bio": row["bio"], "attempted_at": row["bio_attempted_at"]}

    def setAlbumBio(self, albumId: str, bio: str | None) -> None:
        """Stores the fetch result (bio text, or None when Last.fm has
        nothing usable) and stamps bio_attempted_at in one call, mirroring
        setArtistBio."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE albums SET bio = ?, bio_attempted_at = ? WHERE id = ?",
                (bio, time.time(), albumId),
            )

    def requeueCorruptedBiographies(self) -> int:
        """Clears bio and bio_attempted_at for artists whose stored bio
        doesn't end in terminal punctuation - the mid-sentence cutoff left
        behind by fetches made before the bio.content + sentence-boundary
        truncation fix (bio.summary was Last.fm's own truncated excerpt, cut
        at a fixed character budget with no regard for sentence boundaries).
        Clearing bio_attempted_at (not just bio) re-enters them at the front
        of getArtistsMissingBiographies immediately instead of after the
        30-day retry window - the same lever requeueLastfmEntitiesWithoutOwnGenres
        uses for the genre backlog. A NULL bio (never attempted, or a
        definitive no-bio result) is left alone - it isn't corrupted text.
        Returns how many artists were requeued."""
        conn = self._conn()
        with conn:
            cleared = conn.execute(
                """
                UPDATE artists SET bio = NULL, bio_attempted_at = NULL
                WHERE bio IS NOT NULL
                  AND bio NOT LIKE '%.' AND bio NOT LIKE '%!' AND bio NOT LIKE '%?'
                """
            ).rowcount
        return cleared

    def requeueDecoratedAlbumsWithoutBios(self) -> int:
        """Clears bio_attempted_at for albums that were attempted, came back
        with no bio, AND carry a Spotify decoration in their name ("(Deluxe
        Edition)", "- Remastered", ...) - these were tried before the album-bio
        lookup gained cleanLookupName's decoration-stripping retry, so their
        verbatim-name album.getinfo found nothing on Last.fm that the cleaned
        title might still hold. Clearing bio_attempted_at re-enters only those
        at the front of getAlbumsMissingBiographies immediately (instead of
        after the 30-day retry window), where the fixed lookup now retries the
        undecorated title - the album-bio analogue of requeueCorruptedBiographies.
        Undecorated no-bio albums are left alone (the fallback changes nothing
        for them), as is a NULL bio_attempted_at (never attempted). The
        decoration test is cleanLookupName, a Python regex, so candidates are
        filtered in Python rather than SQL. Returns how many albums were requeued."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, name FROM albums WHERE bio IS NULL AND bio_attempted_at IS NOT NULL"
        ).fetchall()
        decoratedIds = [row["id"] for row in rows
                        if row["name"] and cleanLookupName(row["name"]) != row["name"]]
        if not decoratedIds:
            return 0
        with conn:
            conn.executemany(
                "UPDATE albums SET bio_attempted_at = NULL WHERE id = ?",
                [(albumId,) for albumId in decoratedIds],
            )
        return len(decoratedIds)

    def getArtistsMissingBiographies(self, limit: int, username: str | None = None) -> list[dict]:
        """Played artists still needing a Last.fm artist.getinfo lookup,
        most-played first - the background biography backfiller's queue
        (Database._lastfmBiographyBackfillLoop). Same own-vs-global scoping as
        getArtistsMissingGenres, but the retry condition keys off bio directly
        (there's no join table for it): an artist with real bio text never
        requeues, one whose lookup came back empty does after
        BIOGRAPHY_BACKFILL_RETRY_SECONDS.

        Spans the first GENRE_BACKFILL_MAX_ARTIST_POSITION+1 credit positions, not
        just the position-0 primary. Restricting it to 0 meant an artist who is
        never anyone's primary credit could never be looked up at all - 3,543
        artists on the real library, 28.7% of every artist played. That is the same
        gap getArtistsMissingGenres was fixed for (where it produced a 56.7% genre
        coverage figure that read as Last.fm sparsity and was really a queue that
        never asked); this queue was missed at the time. Bounded rather than
        unlimited for the reason given on that constant."""
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - self.getBioBackfillRetrySeconds(), limit])
        rows = conn.execute(
            f"""
            SELECT ar.id AS id, ar.name AS name, COUNT(*) AS play_count
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
                AND ta.position <= {GENRE_BACKFILL_MAX_ARTIST_POSITION}
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE {userClause}(ar.bio_attempted_at IS NULL
                   OR (ar.bio_attempted_at < ? AND ar.bio IS NULL))
            GROUP BY ar.id
            ORDER BY play_count DESC, ar.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def getAlbumsMissingBiographies(self, limit: int, username: str | None = None) -> list[dict]:
        """Played albums still needing a Last.fm album.getinfo lookup,
        most-played first - the background album biography backfiller's
        queue (Database._lastfmAlbumBiographyBackfillLoop). Same
        play-count-ordered, own-vs-global scoping as
        getArtistsMissingBiographies, keyed off albums.bio directly (there's
        no join table for it, same as artist bios)."""
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - self.getBioBackfillRetrySeconds(), limit])
        rows = conn.execute(
            f"""
            SELECT al.id AS id, al.name AS name, COUNT(*) AS play_count
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN albums al ON al.id = t.album_id
            WHERE {userClause}(al.bio_attempted_at IS NULL
                   OR (al.bio_attempted_at < ? AND al.bio IS NULL))
            GROUP BY al.id
            ORDER BY play_count DESC, al.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def getLastfmBiographyRowsByIds(self, kind: str, entityIds: list[str],
                                    username: str | None = None) -> list[dict]:
        """Revalidate a bounded pooled biography candidate set.

        The history aggregate is intentionally absent here: callers already
        selected these ids from it. Recheck biography eligibility by primary
        key; scope membership is refreshed with the candidate pool's TTL.
        """
        if not entityIds:
            return []
        placeholders = ",".join("?" for _ in entityIds)
        cutoff = time.time() - self.getBioBackfillRetrySeconds()
        conn = self._conn()

        if kind == "bio":
            rows = conn.execute(
                f"""
                SELECT ar.id AS id, ar.name AS name
                FROM artists ar
                WHERE ar.id IN ({placeholders})
                  AND (ar.bio_attempted_at IS NULL
                       OR (ar.bio_attempted_at < ? AND ar.bio IS NULL))
                """,
                [*entityIds, cutoff],
            ).fetchall()
        elif kind == "album_bio":
            rows = conn.execute(
                f"""
                SELECT al.id AS id, al.name AS name
                FROM albums al
                WHERE al.id IN ({placeholders})
                  AND (al.bio_attempted_at IS NULL
                       OR (al.bio_attempted_at < ? AND al.bio IS NULL))
                """,
                [*entityIds, cutoff],
            ).fetchall()
        else:
            raise ValueError(f"unknown Last.fm biography kind: {kind}")
        return [dict(row) for row in rows]

    def getBiographyCoverage(self, username: str) -> dict:
        """Entity-count coverage for the Overview "Biography Backfill
        Progress" widget: {"artist": {"covered", "total"}, "album": {...}} -
        how many of this user's played primary artists / played albums
        already have a stored Last.fm biography, out of how many total. A
        simple boolean-per-entity count (not the genre backfill's
        play-weighted percentage - see getGenreCoverage) since a bio is
        present-or-absent per entity, not a per-play attribute.

        Joins against a DISTINCT-track_id subquery rather than the raw plays
        table: joining plays directly (as this used to) repeats the same
        track_artists/artists (or tracks/albums) join once per play instead
        of once per distinct track, which on a real listening history means
        paying the join cost dozens of times over for a track's replays."""
        conn = self._conn()
        artistRow = conn.execute(
            """
            SELECT COUNT(DISTINCT ar.id) AS total,
                   COUNT(DISTINCT CASE WHEN ar.bio IS NOT NULL THEN ar.id END) AS covered
            FROM (SELECT DISTINCT track_id FROM plays WHERE username = ?) p
            -- position = 0 here is deliberate, and deliberately DIFFERENT from
            -- getArtistsMissingBiographies above, which spans position <= 4. This
            -- is a coverage figure, and it means "of the artists you actually
            -- listen to, how many have a bio" - the primary credit is the axis a
            -- reader has in mind. The QUEUE has no reason to be that narrow, since
            -- a feature-only artist still gets a detail page. Same split
            -- getGenreCoverageCounts settled on, for the same reason; widening
            -- this to match the queue would move ~3.5k feature-only artists into
            -- the denominator and make the percentage drop for no real change.
            JOIN track_artists ta ON ta.track_id = p.track_id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            """,
            (username,),
        ).fetchone()
        albumRow = conn.execute(
            """
            SELECT COUNT(DISTINCT al.id) AS total,
                   COUNT(DISTINCT CASE WHEN al.bio IS NOT NULL THEN al.id END) AS covered
            FROM (SELECT DISTINCT track_id FROM plays WHERE username = ?) p
            JOIN tracks t ON t.id = p.track_id
            JOIN albums al ON al.id = t.album_id
            """,
            (username,),
        ).fetchone()
        return {
            "artist": {"covered": artistRow["covered"], "total": artistRow["total"]},
            "album": {"covered": albumRow["covered"], "total": albumRow["total"]},
        }

    def getCatalogBiographyCoverage(self) -> dict:
        """Catalog-wide analogue of getBiographyCoverage: how many artists/
        albums in the whole shared catalog have a stored Last.fm biography,
        regardless of whether anyone has played them recently."""
        conn = self._conn()
        artistRow = conn.execute(
            "SELECT COUNT(*) AS total, COUNT(CASE WHEN bio IS NOT NULL THEN 1 END) AS covered FROM artists"
        ).fetchone()
        albumRow = conn.execute(
            "SELECT COUNT(*) AS total, COUNT(CASE WHEN bio IS NOT NULL THEN 1 END) AS covered FROM albums"
        ).fetchone()
        return {
            "artist": {"covered": artistRow["covered"], "total": artistRow["total"]},
            "album": {"covered": albumRow["covered"], "total": albumRow["total"]},
        }
