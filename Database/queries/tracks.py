# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from Database.queries._base import (
    ALBUM_ARTIST_REPAIR_RETRY_SECONDS,
    ALBUM_BACKFILL_RETRY_SECONDS,
    IMAGE_KIND_ARTIST,
    IMAGE_KIND_TRACK,
    IMAGE_STATUS_FAILED,
    IMAGE_STATUS_OK,
    IMAGE_STATUS_PENDING,
    RESTRICTED_FALLBACK_REASON,
    SPOTIFY_TRACK_ID_LENGTH,
    SYNTHETIC_FALLBACK_REASON,
    TRACK_ISRC_RETRY_SECONDS,
    UNKNOWN_ALBUM_NAME,
    time,
)

class TrackQueries:
    """TrackQueries: tracks data-access methods, mixed into Repository."""

    # ---- Catalog: tracks / artists / albums ----------------------------------

    def upsertTrack(self, track: dict, created_reason: str | None = None) -> None:
        """Upsert a track and its nested album/artists (as produced by
        Client.formatTrack). Last write wins, matching the previous
        tracks[id] = track dict-assignment semantics - with one exception: a
        fallback record (SYNTHETIC_FALLBACK_REASON / RESTRICTED_FALLBACK_REASON)
        never overwrites a row that already has real metadata; it only heals
        blanked rows or refreshes other fallback rows. If created_reason is
        provided, it's only set on INSERT (never updated on conflict) - except
        that real metadata replacing a fallback row also replaces the fallback
        marker.

        Does NOT commit - callers compose this with insertPlay() into a single
        transaction (one play = one commit; a bulk import = one commit for the
        whole batch), then call commit()/rollback() themselves."""
        conn = self._conn()

        # Defense in depth: the importer normally prevents a fallback record
        # from ever targeting a track with real metadata (its known-track index
        # resolves those first), but no caller may rely on that - degraded data
        # must never clobber good catalog data at this level either.
        if track.get("created_reason") in (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON):
            existing = conn.execute(
                "SELECT name, created_reason FROM tracks WHERE id=?", (track["id"],)
            ).fetchone()
            if existing and existing["name"] and existing["created_reason"] not in (
                SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON,
            ):
                return

        album = track.get("album")
        if not album:
            album_id = track.get("albumId") or f"album_{track['id']}"
            album = {
                "id": album_id,
                "name": track.get("name", UNKNOWN_ALBUM_NAME),
                "url": "",
                "totalTracks": 1,
                "releaseDate": 0.0,
                "imageUrl": "",
            }
        artists = track.get("artists") or []
        conn.execute(
            """
            INSERT INTO albums (id, name, url, total_tracks, release_date, image_id, image_url)
            VALUES (:id, :name, :url, :totalTracks, :releaseDate, :id, :imageUrl)
            ON CONFLICT(id) DO UPDATE SET
                name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE albums.name END,
                url = CASE WHEN excluded.url <> '' THEN excluded.url ELSE albums.url END,
                total_tracks = CASE WHEN excluded.total_tracks > 0 THEN excluded.total_tracks ELSE albums.total_tracks END,
                release_date = CASE WHEN excluded.release_date > 0 THEN excluded.release_date ELSE albums.release_date END,
                image_url = CASE WHEN excluded.image_url <> '' THEN excluded.image_url ELSE albums.image_url END
            """,
            album,
        )

        for artist in artists:
            conn.execute(
                """
                INSERT INTO artists (id, name, url, image_id)
                VALUES (:id, :name, :url, :imageId)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, url=excluded.url, image_id=excluded.image_id
                """,
                artist,
            )

        trackData = {
            **track,
            "albumId": album["id"],
            "explicit": bool(track.get("explicit", False)),
            "created_at": track.get("created_at"),
            "created_reason": track.get("created_reason"),
            "availability_reason": track.get("availability_reason"),
            "syntheticReason": SYNTHETIC_FALLBACK_REASON,
            "restrictedReason": RESTRICTED_FALLBACK_REASON,
        }
        if created_reason and not trackData["created_reason"]:
            trackData["created_reason"] = created_reason
        if trackData["created_reason"] and trackData["created_at"] is None:
            trackData["created_at"] = time.time()

        # created_at/created_reason are never updated on conflict, with one exception:
        # a fallback row (synthetic or restricted, see db.py) being overwritten by real
        # metadata drops the fallback marker, so the UI stops badging a track that
        # turned out to be fully available on Spotify after all.
        #
        # isrc follows the same blank-isn't-data rule as duration_ms, and for a
        # sharper reason: NO live ingest path supplies one. The pathfinder client
        # cannot expose ISRCs (Database/Spotify/formatting.py) and the export
        # importer has no such field, so both send "". Assigning excluded.isrc
        # unconditionally meant the Web-API backfiller's value survived only until
        # the track's next play - which, for anything in rotation, is hours.
        conn.execute(
            """
            INSERT INTO tracks (id, name, url, album_id, image_id, duration_ms, explicit, isrc, disc_number, track_number, created_at, created_reason, availability_reason)
            VALUES (:id, :name, :url, :albumId, :imageId, :duration, :explicit, :isrc, :discNumber, :trackNumber, :created_at, :created_reason, :availability_reason)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, album_id=excluded.album_id, image_id=excluded.image_id,
                duration_ms = CASE WHEN excluded.duration_ms > 0 THEN excluded.duration_ms ELSE tracks.duration_ms END,
                explicit=excluded.explicit,
                isrc = CASE WHEN excluded.isrc <> '' THEN excluded.isrc ELSE tracks.isrc END,
                disc_number=excluded.disc_number, track_number=excluded.track_number,
                availability_reason=excluded.availability_reason,
                created_at=CASE
                    WHEN tracks.created_reason IN (:syntheticReason, :restrictedReason)
                         AND (excluded.created_reason IS NULL
                              OR excluded.created_reason NOT IN (:syntheticReason, :restrictedReason))
                    THEN excluded.created_at ELSE tracks.created_at END,
                created_reason=CASE
                    WHEN tracks.created_reason IN (:syntheticReason, :restrictedReason)
                         AND (excluded.created_reason IS NULL
                              OR excluded.created_reason NOT IN (:syntheticReason, :restrictedReason))
                    THEN excluded.created_reason ELSE tracks.created_reason END
            """,
            trackData,
        )

        # An empty artists list means the caller had no artist data - not that
        # the track lost its artists. Keep whatever links are already recorded
        # instead of wiping them.
        if artists:
            conn.execute("DELETE FROM track_artists WHERE track_id=?", (track["id"],))
            for position, artist in enumerate(artists):
                conn.execute(
                    "INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, ?, ?)",
                    (track["id"], artist["id"], position),
                )

    def repairFallbackTracks(self, tracks: list[dict]) -> int:
        """Replace existing fallback catalog rows with formatted real metadata.

        Owns a short write transaction, like the catalog backfiller's other
        repairs. Recheck the marker under its lock: a concurrent live play or
        import may already have supplied better data. Never inserts a new track
        or writes plays, including their historical skip classification.
        """
        if not tracks:
            return 0
        conn = self._conn()
        repaired = 0
        with conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            for track in tracks:
                if track.get("created_reason") in (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON):
                    continue
                row = conn.execute("SELECT created_reason FROM tracks WHERE id=?", (track["id"],)).fetchone()
                if row is None or row["created_reason"] not in (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON):
                    continue
                self.upsertTrack(track)
                repaired += 1
            if repaired:
                # Shared album/artist metadata and discovery years can change
                # beyond these tracks' play years. Invalidate once per actual
                # repair batch, atomically with the catalog and fallback markers.
                self._deleteAllWrapped(conn)
        return repaired

    def getTrack(self, trackId: str) -> dict | None:
        conn = self._conn()
        trackRow = conn.execute("SELECT * FROM tracks WHERE id=?", (trackId,)).fetchone()
        if trackRow is None:
            return None
        albumRow = conn.execute("SELECT * FROM albums WHERE id=?", (trackRow["album_id"],)).fetchone()
        artistRows = conn.execute(
            """
            SELECT a.id, a.name, a.url, a.image_id FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.track_id=? ORDER BY ta.position
            """,
            (trackId,),
        ).fetchall()
        return self._trackRowToDict(trackRow, albumRow, artistRows)

    def getAllTracks(self) -> list[dict]:
        """Every track in the shared catalog, fully reconstructed - used to seed
        the importer's "don't re-fetch metadata we already have" cache."""
        conn = self._conn()
        trackRows = conn.execute("SELECT * FROM tracks").fetchall()
        albumsById = {row["id"]: row for row in conn.execute("SELECT * FROM albums").fetchall()}
        artistsByTrack: dict[str, list] = {}
        for row in conn.execute(
            """
            SELECT ta.track_id, a.id, a.name, a.url, a.image_id FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            ORDER BY ta.track_id, ta.position
            """
        ).fetchall():
            artistsByTrack.setdefault(row["track_id"], []).append(row)

        return [
            self._trackRowToDict(trackRow, albumsById.get(trackRow["album_id"]),
                                  artistsByTrack.get(trackRow["id"], []))
            for trackRow in trackRows
        ]

    def getMatchingTrackIds(self, searchQuery: str) -> list[str]:
        """Track ids whose own name, album name, or any credited artist matches
        every word of `searchQuery` - resolved against the CATALOG only, with no
        reference to plays.

        This exists so a search can be answered in two phases. The search
        predicate used to sit inside the per-play aggregate, so its artist EXISTS
        was re-evaluated for every grouped track (~17.5k of them) rather than
        once. Measured on a real 131k-play library: the whole search page cost
        ~850ms, and cost the same 847ms for a term matching NOTHING - the price
        was structural, not proportional to results. Resolving the ids first and
        narrowing the aggregate to them is the same trick that took the artist
        song list from 985ms to 19ms.

        The condition is deliberately the same one getSongsPage applies inline, so
        the two phases select exactly the same tracks - tests/test_search_two_phase
        pins that they agree row for row and in the same order.

        /history's search ALSO matches the playlist a play came from, which is an
        attribute of the play rather than the track - see getMatchingPlayedFrom for
        the other half of that set.

        Shape: one UNION per word, intersected here. A single OR'd query with a
        correlated artist EXISTS forced `SCAN tracks` and re-evaluated the subquery
        per row; the UNION lets each branch take its own path, measured at 197ms ->
        63ms for "love" and 201ms -> 61ms for a term matching nothing.

        The artist branch joins tracks BACK IN, which is not redundant: it reads
        track_artists, and this database has 195 rows there whose track_id is in no
        tracks row (a known integrity wart - see checkIntegrity). Without the join
        the union returned 7 extra ids for "the" and 84 for "a", so it would have
        counted plays of tracks that do not exist. With it the two shapes are
        set-equal, which tests/test_search_two_phase pins."""
        words = self.searchWords(searchQuery)
        if not words:
            return []
        conn = self._conn()
        matched: set | None = None
        for word in words:
            pattern = self._likePattern(word)
            rows = conn.execute(
                """
                SELECT t.id AS id FROM tracks t WHERE t.name LIKE ? ESCAPE '\\'
                UNION
                SELECT t.id AS id FROM tracks t JOIN albums al ON al.id = t.album_id
                 WHERE al.name LIKE ? ESCAPE '\\'
                UNION
                SELECT ta.track_id AS id FROM track_artists ta
                 JOIN tracks t2 ON t2.id = ta.track_id
                 JOIN artists ar ON ar.id = ta.artist_id
                 WHERE ar.name LIKE ? ESCAPE '\\'
                """,
                (pattern, pattern, pattern),
            ).fetchall()
            found = {row["id"] for row in rows}
            matched = found if matched is None else (matched & found)
            if not matched:
                return []   #< every word must match, so an empty intersection is final
        return list(matched)

    def getMatchingAlbumIds(self, searchQuery: str) -> list[str]:
        """Album ids whose own name, or any artist credited on any of their
        tracks, matches every word of `searchQuery` - the album twin of
        getMatchingTrackIds, resolved against the CATALOG only.

        It cannot reuse that one. An album's rows in a plays aggregate span
        several DIFFERENT tracks, so its artist check has to consider every
        track on the album rather than the current row's own, or the album's
        totals would silently lose its non-matching tracks (see getAlbumsPage).
        That is why the condition stayed inline as a correlated EXISTS long
        after the song search stopped doing it, and it made the album search the
        most expensive in the app - measured on the real 131k-play library, best
        of 3, page and count together:
            "love"          1526ms -> 182ms
            "the"           2435ms -> 429ms
            "radiohead"     1535ms ->  46ms   (0 results either way)
            "xylophonezzz"  2962ms ->  84ms   (0 results either way)
        against 317ms for the same page with no search at all: the old cost was
        structural, not proportional to what was found.

        Deliberately NOT paired with the seekable track-set companion that
        _trackSetClause adds for a tag filter. Measured both ways: naming the
        matched albums' tracks on plays as well helps a small set a lot ("love",
        479 albums: 134ms -> 11ms) and hurts a large one badly ("e", 18,712
        albums: 330ms -> 853ms, worse than the unnarrowed 905ms it replaced),
        because a near-catalog-wide set turns one scan into tens of thousands of
        seeks. Picking between them needs a size threshold, and a threshold is a
        second path to keep tested; the id set alone is a large win at every size
        measured.

        Same shape as getMatchingTrackIds: one UNION per word, intersected here,
        so every word must match somewhere but not all in the same field. The
        artist branch joins albums back in so the set can only ever contain
        albums that exist - tracks.album_id is not enforced against a missing
        album row, and an id that matches nothing downstream would be a silent
        extra."""
        words = self.searchWords(searchQuery)
        if not words:
            return []
        conn = self._conn()
        matched: set | None = None
        for word in words:
            pattern = self._likePattern(word)
            rows = conn.execute(
                """
                SELECT al.id AS id FROM albums al WHERE al.name LIKE ? ESCAPE '\\'
                UNION
                SELECT al2.id AS id FROM tracks t
                 JOIN albums al2 ON al2.id = t.album_id
                 JOIN track_artists ta ON ta.track_id = t.id
                 JOIN artists ar ON ar.id = ta.artist_id
                 WHERE ar.name LIKE ? ESCAPE '\\'
                """,
                (pattern, pattern),
            ).fetchall()
            found = {row["id"] for row in rows}
            matched = found if matched is None else (matched & found)
            if not matched:
                return []   #< every word must match, so an empty intersection is final
        return list(matched)

    def getMatchingPlayedFrom(self, searchQuery: str) -> list[str]:
        """The plays.played_from values whose playlist/album NAME matches every
        word - the other half of /history's search, which matches where a play came
        from as well as what was played.

        played_from is stored as "type:id" (see Client.formatTrack), so the values
        are reassembled here rather than joined through substr/instr per play row.
        Cheap by nature: this instance has 11 playlists rows against 73k plays, of
        which 62 carry a played_from at all."""
        words = self.searchWords(searchQuery)
        if not words:
            return []
        conn = self._conn()
        matched: set | None = None
        for word in words:
            rows = conn.execute(
                "SELECT type || ':' || id AS playedFrom FROM playlists "
                "WHERE name LIKE ? ESCAPE '\\'",
                (self._likePattern(word),),
            ).fetchall()
            found = {row["playedFrom"] for row in rows}
            matched = found if matched is None else (matched & found)
            if not matched:
                return []
        return list(matched)

    def getTracksByIds(self, trackIds: list[str]) -> dict[str, dict]:
        """Batch equivalent of getTrack() for a specific set of track ids, in a
        fixed 3 queries total regardless of how many ids are requested (tracks,
        albums, artists) - the caller-facing counterpart to getAllTracks(),
        scoped instead of unbounded. Mirrors getAllTracks()'s own raw-row
        artist query (not _artistsForTracks(), which returns already-converted
        dicts shaped for _songRowToDict rather than the raw rows
        _trackRowToDict() expects). Reused by Database._paginateEntries() so
        hydrating a page of play history doesn't pay 3 queries per play
        (getTrack()'s old N+1)."""
        if not trackIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        trackRows = conn.execute(f"SELECT * FROM tracks WHERE id IN ({placeholders})", trackIds).fetchall()

        albumIds = {row["album_id"] for row in trackRows}
        albumsById = {}
        if albumIds:
            albumIdList = list(albumIds)
            albumPlaceholders = ",".join("?" for _ in albumIdList)
            albumsById = {
                row["id"]: row
                for row in conn.execute(f"SELECT * FROM albums WHERE id IN ({albumPlaceholders})", albumIdList).fetchall()
            }

        resolvedIds = [row["id"] for row in trackRows]
        artistsByTrack: dict[str, list] = {}
        if resolvedIds:
            idPlaceholders = ",".join("?" for _ in resolvedIds)
            for row in conn.execute(
                f"""
                SELECT ta.track_id, a.id, a.name, a.url, a.image_id FROM track_artists ta
                JOIN artists a ON a.id = ta.artist_id
                WHERE ta.track_id IN ({idPlaceholders})
                ORDER BY ta.track_id, ta.position
                """,
                resolvedIds,
            ).fetchall():
                artistsByTrack.setdefault(row["track_id"], []).append(row)

        return {
            row["id"]: self._trackRowToDict(row, albumsById.get(row["album_id"]), artistsByTrack.get(row["id"], []))
            for row in trackRows
        }

    @classmethod
    def _trackRowToDict(cls, trackRow, albumRow, artistRows) -> dict:
        return {
            "id": trackRow["id"],
            #< the same key _songRowToDict carries, so a song reaches the
            #  template with the same shape whichever query built it - the
            #  skip-sorted page hydrates through here rather than through that
            #  one, and tests/test_skip_sort.py pins the two agreeing
            "canonicalId": (trackRow["canonical_id"]
                            if "canonical_id" in trackRow.keys() else None),
            "name": trackRow["name"],
            "url": trackRow["url"],
            "imageUrl": albumRow["image_url"] if albumRow else "",
            "imageId": trackRow["image_id"],
            "duration": trackRow["duration_ms"],
            "explicit": bool(trackRow["explicit"]),
            "isrc": trackRow["isrc"] or "",
            "discNumber": trackRow["disc_number"],
            "trackNumber": trackRow["track_number"],
            "releaseDate": albumRow["release_date"] if albumRow else None,
            "album": cls._albumRowToDict(albumRow) if albumRow else None,
            "artists": [
                {"id": r["id"], "name": r["name"], "url": r["url"], "imageUrl": "", "imageId": r["image_id"]}
                for r in artistRows
            ],
            "created_reason": trackRow["created_reason"],
            "availability_reason": trackRow["availability_reason"],
        }

    def trackExists(self, trackId: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM tracks WHERE id=?", (trackId,)).fetchone()
        return row is not None

    def artistExists(self, artistId: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM artists WHERE id=?", (artistId,)).fetchone()
        return row is not None

    @staticmethod
    def _albumRowToDict(albumRow) -> dict:
        return {
            "id": albumRow["id"],
            "name": albumRow["name"],
            "url": albumRow["url"],
            "imageId": albumRow["image_id"],
            "imageUrl": albumRow["image_url"],
            "totalTracks": albumRow["total_tracks"],
            "releaseDate": albumRow["release_date"],
        }

    # ---- Catalog: playlists ----------------------------------------------------

    def upsertPlaylistName(self, playlistId: str, playlistType: str, name: str | None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO playlists (id, type, name) VALUES (?, ?, ?)
                ON CONFLICT(id, type) DO UPDATE SET name=excluded.name
                """,
                (playlistId, playlistType, name),
            )

    def getPlaylistName(self, playlistId: str, playlistType: str) -> str | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT name FROM playlists WHERE id=? AND type=?", (playlistId, playlistType)
        ).fetchone()
        return row["name"] if row else None

    def playlistKnown(self, playlistId: str, playlistType: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM playlists WHERE id=? AND type=?", (playlistId, playlistType)
        ).fetchone()
        return row is not None

    # ---- Catalog: images (shared download-dedup tracking) ----------------------

    def tryClaimImageDownload(self, imageId: str, kind: str) -> bool:
        """Atomically claim the right to download this image: returns True if the
        caller should proceed (nothing else has claimed or finished it), False if
        it's already downloaded or another thread already claimed it. A
        previously-failed claim can be reclaimed."""
        conn = self._conn()
        with conn:
            # Single guarded write, not SELECT-then-INSERT: the old two-step form
            # had a read-then-write gap where two threads could both observe "no
            # claim" and both proceed. Here the WHERE only lets the claim land
            # when the row is absent or previously failed, and rowcount tells us
            # whether THIS caller won it (1) or someone already holds it (0).
            cur = conn.execute(
                """
                INSERT INTO images (id, kind, status) VALUES (?, ?, ?)
                ON CONFLICT(id, kind) DO UPDATE SET status=excluded.status
                    WHERE images.status NOT IN (?, ?)
                """,
                (imageId, kind, IMAGE_STATUS_PENDING, IMAGE_STATUS_OK, IMAGE_STATUS_PENDING),
            )
            return cur.rowcount > 0

    def releaseImageClaim(self, imageId: str, kind: str) -> None:
        """Give a claim back unused: this attempt never learned anything.

        The counterpart to marking 'failed', which means "asked, and there is
        no image" - permanent by design, and refused for good by
        lazyFetchArtistImage. An attempt that raised did not establish that. A
        missing row is the never-attempted state (the same state
        deleteFailedArtistImages restores rows to), so the next render tries
        again instead of inheriting a verdict nobody reached.

        Only a row still 'pending' is dropped, and only ours can be: the claim
        is what set it, and anything that finished it wrote 'ok' or 'failed'
        over the top."""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM images WHERE id=? AND kind=? AND status=?",
                         (imageId, kind, IMAGE_STATUS_PENDING))

    def forgetImageStatus(self, imageId: str, kind: str) -> None:
        """Forget an 'ok' verdict whose file is no longer on disk.

        'ok' means "downloaded, and the JPEG is at its final path" - and both
        fetch gates trust it without looking at the disk (lazyFetchArtistImage
        returns before any reclaim; tryClaimImageDownload refuses to re-claim
        it). A database restored without Database/Data/Media therefore lost
        every image it had ever fetched, permanently. The image routes call
        this where the missing file is already detected, returning the row to
        never-attempted so the next claim goes through.

        Only an 'ok' row, and in one guarded statement rather than a
        SELECT-then-DELETE: `with conn:` does not BEGIN, so a read-then-write
        here would hold no lock, and a claim landing in the gap would be
        deleted with the stale verdict. A 'pending' row is left alone - it may
        be mid-write, and deleteStalePendingImages owns those at boot - and so
        is 'failed', which is a verdict about the image, not about the disk."""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM images WHERE id=? AND kind=? AND status=?",
                         (imageId, kind, IMAGE_STATUS_OK))

    def markImageStatus(self, imageId: str, kind: str, status: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO images (id, kind, status) VALUES (?, ?, ?)
                ON CONFLICT(id, kind) DO UPDATE SET status=excluded.status
                """,
                (imageId, kind, status),
            )

    def imageStatus(self, imageId: str, kind: str) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT status FROM images WHERE id=? AND kind=?", (imageId, kind)).fetchone()
        return row["status"] if row else None

    def deleteStalePendingImages(self) -> int:
        """Forget every 'pending' download claim. Only safe at process startup,
        before any download can be in flight: a pending row surviving from a
        previous run means the claimer died (crash, or its status write failed
        against a locked database) - tryClaimImageDownload would refuse to
        reclaim it forever, leaving that artwork permanently missing. Deleted
        rather than marked failed: lazyFetchArtistImage treats 'failed' as
        permanent, while a missing row means never-attempted, so both the
        track and artist paths retry naturally. Returns the number of claims
        cleared."""
        conn = self._conn()
        with conn:
            cur = conn.execute("DELETE FROM images WHERE status=?", (IMAGE_STATUS_PENDING,))
            return cur.rowcount

    def deleteFailedArtistImages(self) -> int:
        """One-time remediation for migrate1_20_0: every artist image previously
        marked 'failed' was marked that way by scraping open.spotify.com's public
        artist page for an og:image meta tag, a method that stopped working for
        every artist (not just ones that genuinely lack a picture) once Spotify
        moved artist pages to a client-rendered SPA shell. None of those rows are
        trustworthy "no image" signals, so they're deleted rather than left
        'failed' - lazyFetchArtistImage treats 'failed' as permanent, and a missing
        row means never-attempted, letting the fixed Web-API/cookie-client fetch
        retry them. Returns the number of rows cleared."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM images WHERE kind=? AND status=?", (IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED))
            return cur.rowcount

    def deleteFailedTrackImages(self) -> int:
        """One-time remediation for migrate1_40_0: _imageUrlFromConnectMeta built
        a malformed CDN URL (https://i.scdn.co/image///i.scdn.co/image/<hash>)
        whenever the connect-state carried an absolute URL instead of a
        spotify:image: URI, so those covers 404'd and were marked 'failed'.
        A 'failed' row is permanent - _saveImg's tryClaimImageDownload gate
        refuses to re-attempt it - so the covers would stay missing forever even
        with the URL bug fixed.

        Which rows were poisoned isn't recorded anywhere (the attempted URL
        isn't stored), so every failed track image is cleared, exactly as
        migrate1_20_0 did for artists. The cost of over-reaching is one retry
        for a cover that genuinely 404s; the cost of under-reaching is a
        permanently blank cover. Returns the number of rows cleared."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM images WHERE kind=? AND status=?", (IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED))
            return cur.rowcount

    def getAlbumsMissingMetadata(self, limit: int) -> list[str]:
        """Albums with incomplete metadata (missing release date or track count),
        excluding fabricated fallback albums (album_<id> never existed on Spotify).
        backfill_attempted_at rate-limits retries: each processed album waits
        ALBUM_BACKFILL_RETRY_SECONDS before being re-queued, so restricted/blanked
        albums get another chance weekly while permanently dateless albums don't
        hammer the API every cycle."""
        conn = self._conn()
        retryCutoff = time.time() - ALBUM_BACKFILL_RETRY_SECONDS
        rows = conn.execute(
            r"""
            SELECT id FROM albums
            WHERE (release_date = 0 OR release_date IS NULL OR total_tracks = 0)
              AND id NOT LIKE 'album\_%' ESCAPE '\'
              AND (backfill_attempted_at IS NULL OR backfill_attempted_at < ?)
            LIMIT ?
            """,
            (retryCutoff, limit)
        ).fetchall()
        return [row["id"] for row in rows]

    def getAlbumsWithArtistlessTracks(self, limit: int) -> list[str]:
        """Albums holding at least one track with NO track_artists rows -
        tracks saved from degraded payloads (or legacy imports) that carried
        no artist data. The metadata backfiller piggybacks these onto its
        album fetches: the album payload's per-track artists repair the
        links. Shares the backfill_attempted_at rate limit (and the
        fabricated-id exclusion) with getAlbumsMissingMetadata."""
        conn = self._conn()
        now = time.time()
        retryCutoff = now - ALBUM_BACKFILL_RETRY_SECONDS
        #< the second exit, and the one that makes this queue terminate at all:
        #  an album whose track Spotify credits nobody on can never satisfy the
        #  NOT EXISTS above, so without this it came back every retry window
        #  forever - see markAlbumsArtistRepairDone
        repairCutoff = now - ALBUM_ARTIST_REPAIR_RETRY_SECONDS
        rows = conn.execute(
            r"""
            SELECT DISTINCT al.id FROM albums al
            JOIN tracks t ON t.album_id = al.id
            WHERE NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.track_id = t.id)
              AND al.id NOT LIKE 'album\_%' ESCAPE '\'
              AND (al.backfill_attempted_at IS NULL OR al.backfill_attempted_at < ?)
              AND (al.artist_repair_done_at IS NULL OR al.artist_repair_done_at < ?)
            LIMIT ?
            """,
            (retryCutoff, repairCutoff, limit)
        ).fetchall()
        return [row["id"] for row in rows]

    def markAlbumsArtistRepairDone(self, albumIds) -> None:
        """Record that these albums' COMPLETE track lists were walked and every
        artist credit in the payload applied.

        The artistless-track queue's terminating condition. Its own predicate -
        "this album still holds a track with no artists" - is one an album can
        never stop satisfying when Spotify credits nobody on that track, or
        when the track sits beyond the reach of what was fetched. Each pass
        then repaired nothing, stamped backfill_attempted_at, and the album
        returned ALBUM_BACKFILL_RETRY_SECONDS later - and since this queue is
        the only album source once the metadata queue drains, those albums
        crowd out real work in every batch, indefinitely.

        Only the caller knows whether the list was complete, which is why this
        is a separate call and not a side effect of the repair itself: a
        truncated fetch must NOT stamp, or a genuinely repairable album is
        silenced for ALBUM_ARTIST_REPAIR_RETRY_SECONDS. Self-committing, like
        markAlbumsBackfillAttempted beside it. Empty is a no-op."""
        ids = list(dict.fromkeys(albumIds))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        conn = self._conn()
        with conn:
            conn.execute(f"UPDATE albums SET artist_repair_done_at=? WHERE id IN ({placeholders})",
                         (time.time(), *ids))

    def addMissingTrackArtists(self, trackId: str, artists: list[dict]) -> bool:
        """Write artist links for a known track that has NONE - the metadata
        backfiller's repair path. Existing links are never touched (the album
        payload is a repair source, not an authority over what richer
        play-time payloads recorded), and existing artist rows keep their
        data. Returns whether links were written."""
        if not artists:
            return False
        conn = self._conn()
        with conn:
            #< BEGIN IMMEDIATE: the no-links check IS the decision, and under
            #  legacy transaction control it would run in autocommit holding no
            #  lock. The backfiller and an import can both reach the same
            #  blanked track; both passing an unlocked check ends in doubled
            #  links or an IntegrityError aborting whichever lands second. Same
            #  remedy as dismissMergeCandidate and saveCachedWrapped.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM track_artists WHERE track_id=? LIMIT 1",
                            (trackId,)).fetchone() is not None:
                return False
            if conn.execute("SELECT 1 FROM tracks WHERE id=?", (trackId,)).fetchone() is None:
                return False
            for position, artist in enumerate(artists):
                conn.execute(
                    """
                    INSERT INTO artists (id, name, url, image_id)
                    VALUES (:id, :name, :url, :imageId)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    artist,
                )
                conn.execute(
                    "INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, ?, ?)",
                    (trackId, artist["id"], position),
                )
        return True

    def markAlbumsBackfillAttempted(self, albumIds: list[str]) -> None:
        """Stamp albums as processed by the backfiller so they leave the queue for
        ALBUM_BACKFILL_RETRY_SECONDS - including albums Spotify returned no data
        for, which would otherwise be re-fetched every cycle forever."""
        if not albumIds:
            return
        conn = self._conn()
        placeholders = ",".join("?" for _ in albumIds)
        with conn:
            conn.execute(
                f"UPDATE albums SET backfill_attempted_at = ? WHERE id IN ({placeholders})",
                [time.time(), *albumIds],
            )

    def getTracksMissingIsrc(self, limit: int) -> list[str]:
        """Real Spotify tracks missing ISRC or still carrying fallback metadata.

        A fallback remains eligible even if an earlier ISRC-only lookup
        succeeded. The worker uses that same response to repair its catalog
        metadata, within the existing retry window and request budget.

        Fabricated ids are excluded by length: the export importer's surrogate
        is a bare 32-char md5 of "name::artist" with no prefix to test for, so
        shape is the only discriminator (SPOTIFY_TRACK_ID_LENGTH / the shared
        looksLikeSpotifyTrackId, whose rule this mirrors in SQL). Asking Spotify
        about a surrogate 404s forever.

        isrc_attempted_at rate-limits retries the same way albums use
        backfill_attempted_at - see TRACK_ISRC_RETRY_SECONDS for why this window
        is the longer one.

        Merge candidates first. This queue is drained against a Spotify app
        quota, not a clock: the live instance got through 1,791 of 25,134 tracks
        in an hour and then drew QUOTA_EXCEEDED for the rest of the day, so the
        ORDER decides when the catalog becomes USEFUL rather than when it
        finishes. An ISRC only changes an answer where two tracks are already
        candidates to be merged (tracks.canonical_id, 1.49.0), and there are
        ~900 of those - everywhere else it is a value nothing reads yet.

        A priority, not a filter: the rest still drains behind them.

        The key is the title and the FIRST artist, case-insensitively. Each part
        was measured on a copy of the live database (25,134 tracks, 840
        candidates still needing an ISRC), because each was wrong at least once:

          title only                        1,707 queued, 60% of candidates
          + first artist                      540 queued, 62%
          + LOWER()                           872 queued, 100%

        The artist is what stops a cover flooding the queue - a shared title
        alone pulled in 6,099 tracks. LOWER() is what makes it complete: SQLite
        compares text case-sensitively, so without it a third of the candidates
        never sorted to the front at all. Duration is deliberately NOT here even
        though the merge rule uses it: with the artist in the key it excluded
        nothing except pairs straddling a bucket edge, and those it excluded
        wrongly.

        872 is the number that matters. It fits inside a single Spotify quota
        window (~1,791 lookups were served before QUOTA_EXCEEDED on 2026-08-08),
        so the merge question becomes answerable in one window instead of the
        fourteen the whole catalog would need.

        MEASURED: 0.3ms -> 436ms, and worth it - this runs three times per
        five-minute cycle, so it is 0.15% of one, and 98% of what it queues now
        buys a merge decision instead of 3.7%. A correlated EXISTS over the same
        condition did not finish in two minutes (`name` is unindexed, so it
        degrades to O(n**2)); the IN subquery here is materialised once. Do not
        "simplify" it back."""
        conn = self._conn()
        retryCutoff = time.time() - TRACK_ISRC_RETRY_SECONDS
        rows = conn.execute(
            """
            SELECT id FROM tracks
            WHERE (isrc IS NULL OR isrc = '' OR created_reason IN (?, ?))
              AND LENGTH(id) = ?
              AND (isrc_attempted_at IS NULL OR isrc_attempted_at < ?)
            ORDER BY ((LOWER(tracks.name),
                       (SELECT artist_id FROM track_artists ta
                        WHERE ta.track_id = tracks.id ORDER BY ta.position LIMIT 1)) IN (
                SELECT LOWER(t.name),
                       (SELECT artist_id FROM track_artists ta2
                        WHERE ta2.track_id = t.id ORDER BY ta2.position LIMIT 1)
                FROM tracks t
                GROUP BY LOWER(t.name),
                         (SELECT artist_id FROM track_artists ta3
                          WHERE ta3.track_id = t.id ORDER BY ta3.position LIMIT 1)
                HAVING COUNT(*) > 1
            )) DESC
            LIMIT ?
            """,
            (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON,
             SPOTIFY_TRACK_ID_LENGTH, retryCutoff, limit),
        ).fetchall()
        return [row["id"] for row in rows]

    def updateTrackIsrcs(self, isrcByTrackId: dict[str, str]) -> None:
        """Store the ISRCs a backfill response carried. Blank values are skipped
        rather than written: an absent external_ids.isrc is "Spotify didn't say",
        not "this recording has no ISRC", and writing '' would be
        indistinguishable from the never-fetched state the queue reads."""
        pairs = [(isrc, trackId) for trackId, isrc in isrcByTrackId.items() if isrc]
        if not pairs:
            return
        conn = self._conn()
        with conn:
            conn.executemany("UPDATE tracks SET isrc = ? WHERE id = ?", pairs)

    def markTracksIsrcAttempted(self, trackIds: list[str]) -> None:
        """Stamp tracks as asked-about so they leave the queue for
        TRACK_ISRC_RETRY_SECONDS - including the ones Spotify returned no ISRC
        for, which would otherwise be re-requested every cycle forever."""
        if not trackIds:
            return
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        with conn:
            conn.execute(
                f"UPDATE tracks SET isrc_attempted_at = ? WHERE id IN ({placeholders})",
                [time.time(), *trackIds],
            )

    def updateAlbumMetadata(self, album_id: str, release_date: float, total_tracks: int, name: str | None = None) -> None:
        """Blank fields aren't data: a zero release_date/total_tracks or an
        empty name never overwrites an existing value, so a partial backfill
        response (e.g. an album Spotify returns without a usable release date)
        can't regress metadata another source already filled."""
        conn = self._conn()
        with conn:
            conn.execute(
                """
                UPDATE albums SET
                    release_date = CASE WHEN :release_date > 0 THEN :release_date ELSE release_date END,
                    total_tracks = CASE WHEN :total_tracks > 0 THEN :total_tracks ELSE total_tracks END,
                    name = CASE WHEN :name IS NOT NULL AND :name != '' THEN :name ELSE name END
                WHERE id = :id
                """,
                {"id": album_id, "release_date": release_date, "total_tracks": total_tracks, "name": name},
            )

    def updateTrackName(self, track_id: str, name: str, duration_ms: int | None = None) -> None:
        """duration_ms also updates the stored duration when provided (>0) - the
        album backfill response is the only source of durations for tracks whose
        own lookup came back blanked (region-restricted)."""
        conn = self._conn()
        with conn:
            if duration_ms:
                conn.execute(
                    "UPDATE tracks SET name = ?, duration_ms = ? WHERE id = ?",
                    (name, duration_ms, track_id)
                )
            else:
                conn.execute(
                    "UPDATE tracks SET name = ? WHERE id = ?",
                    (name, track_id)
                )

    def getAlbumPrimaryArtists(self, albumIds: list[str]) -> dict[str, dict]:
        """albumId -> {artist_id, artist_name} via each album's tracks'
        position-0 artists (albums carry no artist column of their own). The
        most frequent primary artist wins; ties break by artist id so repeated
        runs derive the same lookup name. Albums with no resolvable artist are
        simply absent - the worker marks those attempted without a lookup."""
        if not albumIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in albumIds)
        rows = conn.execute(
            f"""
            SELECT t.album_id AS album_id, ar.id AS artist_id, ar.name AS artist_name,
                   COUNT(*) AS cnt
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE t.album_id IN ({placeholders})
            GROUP BY t.album_id, ar.id
            ORDER BY t.album_id, cnt DESC, ar.id ASC
            """,
            albumIds,
        ).fetchall()
        primaries: dict[str, dict] = {}
        for row in rows:
            if row["album_id"] not in primaries:   #< rows are sorted best-first per album
                primaries[row["album_id"]] = {"artist_id": row["artist_id"],
                                              "artist_name": row["artist_name"]}
        return primaries

    def getAlbumCandidateArtists(self, albumId: str) -> list[dict]:
        """Ordered candidate artists for an album across all tracks (combining
        primary and secondary credited artists up to position <= 4), ranked
        by count DESC and capped at GENRE_BACKFILL_MAX_ARTIST_POSITION
        candidates - same bound as getTrackSecondaryArtists's per-track
        position cutoff, applied here to the distinct-artist count instead.
        Without it, a "Various Artists" compilation album (many tracks, many
        different credited artists) would make the genre backfiller try
        every single one - each up to two rate-limited Last.fm requests -
        against the process-wide, cross-user rate limit before giving up."""
        conn = self._conn()
        from Database.queries._base import GENRE_BACKFILL_MAX_ARTIST_POSITION
        rows = conn.execute(
            """
            SELECT ar.id AS artist_id, ar.name AS artist_name, COUNT(*) AS cnt
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.position <= ?
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE t.album_id = ?
            GROUP BY ar.id
            ORDER BY cnt DESC, ar.id ASC
            LIMIT ?
            """,
            (GENRE_BACKFILL_MAX_ARTIST_POSITION, albumId, GENRE_BACKFILL_MAX_ARTIST_POSITION),
        ).fetchall()
        return [dict(r) for r in rows]

