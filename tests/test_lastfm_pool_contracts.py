# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract tests for the bounded Last.fm candidate pools."""

import threading
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from Database.lastfm import OUTCOME_OK, OUTCOME_TRANSIENT
import Database.workers.lastfm_backfillers as backfillers
from Database.workers.lastfm_backfillers import LastfmBackfillMixin, _LastfmCandidatePool


POOL_SIZE = 600
GENRE_BATCH_SIZE = 30
BIOGRAPHY_BATCH_SIZE = 20
WORKER_COUNT = 3
TRANSIENT_CYCLES = 40
TEST_USERNAME = "user1"
TRACK_ID = "t1"
ALBUM_ID = "al1"
ARTIST_ID = "a1"

POOL_KINDS = (
    ("artist", GENRE_BATCH_SIZE),
    ("album", GENRE_BATCH_SIZE),
    ("track", GENRE_BATCH_SIZE),
    ("bio", BIOGRAPHY_BATCH_SIZE),
    ("album_bio", BIOGRAPHY_BATCH_SIZE),
)
PROCESSORS = (
    ("artist", "_processLastfmArtistBatch", "getArtistsMissingGenres", "getArtistTopTags"),
    ("album", "_processLastfmAlbumBatch", "getAlbumsMissingGenres", "getAlbumTopTags"),
    ("track", "_processLastfmTrackBatch", "getTracksMissingGenres", "getTrackTopTags"),
    ("bio", "_processLastfmBiographyBatch", "getArtistsMissingBiographies", "getArtistInfo"),
    ("album_bio", "_processLastfmAlbumBiographyBatch", "getAlbumsMissingBiographies", "getAlbumInfo"),
)


class _PoolHarness(LastfmBackfillMixin):
    LASTFM_QUEUE_POOL_SIZE = POOL_SIZE
    LASTFM_QUEUE_BATCH_SIZE = GENRE_BATCH_SIZE
    LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE = BIOGRAPHY_BATCH_SIZE
    LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE = BIOGRAPHY_BATCH_SIZE
    LASTFM_QUEUE_POOL_TTL_SECONDS = 1800
    LASTFM_QUEUE_DRAINED_MEMO_SECONDS = Database.LASTFM_QUEUE_DRAINED_MEMO_SECONDS

    def __init__(self):
        self.repo = None
        self.lastfm_stop_event = threading.Event()
        self.lastfm_biography_stop_event = threading.Event()
        self.lastfm_album_biography_stop_event = threading.Event()

    def _lastfmRevalidateRows(self, kind, scopeUsername, rows):
        return rows


class LastfmPoolBoundedContractsTest(unittest.TestCase):
    def setUp(self):
        Database._lastfm_active.clear()
        backfillers._LASTFM_CANDIDATE_POOLS.clear()
        self.addCleanup(Database._lastfm_active.clear)
        self.addCleanup(backfillers._LASTFM_CANDIDATE_POOLS.clear)

    def _rows(self, kind):
        return [{"id": f"{kind}-{index}", "name": f"{kind} {index}"}
                for index in range(POOL_SIZE)]

    def test_each_kind_drains_600_candidates_in_its_existing_batch_size_with_one_fetch(self):
        for kind, batchSize in POOL_KINDS:
            with self.subTest(kind=kind):
                db = _PoolHarness()
                rows = self._rows(kind)
                fetch = Mock(return_value=rows)
                selected = []

                for _ in range(POOL_SIZE // batchSize):
                    batch = db._pooledCandidates(kind, TEST_USERNAME, fetch)
                    self.assertEqual(len(batch), batchSize)
                    selected.extend(row["id"] for row in batch)
                    db._finishPooledCandidates(kind, TEST_USERNAME, batch, ())

                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(selected, [row["id"] for row in rows])

    def test_each_kind_claims_no_more_than_its_existing_batch_size(self):
        for kind, batchSize in POOL_KINDS:
            with self.subTest(kind=kind):
                workers = [_PoolHarness() for _ in range(WORKER_COUNT)]
                rows = self._rows(kind)
                fetch = Mock(return_value=rows)
                batches = [worker._pooledCandidates(kind, TEST_USERNAME, fetch)
                           for worker in workers]

                self.assertEqual([len(batch) for batch in batches],
                                 [batchSize] * WORKER_COUNT)
                self.assertEqual(len(Database._lastfm_active), batchSize * WORKER_COUNT)

                for worker, batch in zip(workers, batches):
                    worker._finishPooledCandidates(kind, TEST_USERNAME, batch, ())

    def test_persistent_transient_prefix_does_not_refill_or_grow_for_any_kind(self):
        for kind, batchSize in POOL_KINDS:
            with self.subTest(kind=kind):
                db = _PoolHarness()
                rows = self._rows(kind)
                fetch = Mock(return_value=rows)

                for _ in range(TRANSIENT_CYCLES):
                    batch = db._pooledCandidates(kind, TEST_USERNAME, fetch)
                    self.assertEqual(len(batch), batchSize)
                    db._finishPooledCandidates(kind, TEST_USERNAME, batch, batch)

                pool = backfillers._LASTFM_CANDIDATE_POOLS[(kind, TEST_USERNAME, None)]
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(len(pool.rows), POOL_SIZE)
                self.assertEqual(len(pool.retry_rows), batchSize)
                self.assertLessEqual(len(pool.retry_rows), batchSize)

    def _processorHarness(self, kind, query, lookup):
        worker = _PoolHarness()
        rows = [{"id": f"{kind}{i}", "name": f"Name{i}", "artist_id": "artist",
                 "artist_name": "Artist", "album_id": "album"} for i in range(POOL_SIZE)]
        worker.repo = Mock()
        worker.repo.connectionManager = None
        getattr(worker.repo, query).return_value = rows
        worker.repo.getAlbumPrimaryArtists.return_value = {
            row["id"]: {"artist_id": "artist", "artist_name": "Artist"} for row in rows}
        worker.repo.getAlbumCandidateArtists.return_value = []
        worker._storeLastfmGenresWithInheritance = Mock(return_value=True)
        client = Mock()
        ok = SimpleNamespace(status=OUTCOME_OK, tags=[{"name": "rock", "count": 100}], bio="Biography")
        getattr(client, lookup).return_value = ok
        return worker, rows, client, ok

    def test_mixed_real_processors_retry_only_the_transient_row_first(self):
        for kind, processor, query, lookup in PROCESSORS:
            with self.subTest(kind=kind):
                worker, rows, client, ok = self._processorHarness(kind, query, lookup)
                batchSize = worker._lastfmPoolBatchSize(kind)
                pending = SimpleNamespace(status=OUTCOME_TRANSIENT)
                getattr(client, lookup).side_effect = [ok] * (batchSize - 1) + [pending]
                self.assertTrue(getattr(worker, processor)(client, TEST_USERNAME))
                pool = backfillers._LASTFM_CANDIDATE_POOLS[(kind, TEST_USERNAME, None)]
                self.assertEqual([row["id"] for row in pool.retry_rows], [rows[batchSize - 1]["id"]])
                getattr(client, lookup).side_effect = None
                getattr(client, lookup).reset_mock()
                self.assertTrue(getattr(worker, processor)(client, TEST_USERNAME))
                self.assertEqual(getattr(client, lookup).call_args_list[0].args[-1], rows[batchSize - 1]["name"])
                self.assertEqual(getattr(worker.repo, query).call_count, 1)
                self.assertFalse(Database._lastfm_active)

    def test_stop_and_exception_retain_unprocessed_rows_and_release_every_claim(self):
        completedBeforeStop = 3
        for mode in ("stop", "exception"):
            for kind, processor, query, lookup in PROCESSORS:
                with self.subTest(kind=kind, mode=mode):
                    backfillers._LASTFM_CANDIDATE_POOLS.clear()
                    worker, rows, client, ok = self._processorHarness(kind, query, lookup)
                    event = threading.Event()
                    calls = []

                    def outcome(*args, **kwargs):
                        calls.append(args)
                        if len(calls) == completedBeforeStop:
                            if mode == "exception":
                                raise RuntimeError("lookup failed")
                            event.set()
                        return ok

                    getattr(client, lookup).side_effect = outcome
                    if mode == "exception":
                        with self.assertRaises(RuntimeError):
                            getattr(worker, processor)(client, TEST_USERNAME, stop_event=event)
                    else:
                        getattr(worker, processor)(client, TEST_USERNAME, stop_event=event)
                    pool = backfillers._LASTFM_CANDIDATE_POOLS[(kind, TEST_USERNAME, None)]
                    completed = completedBeforeStop - (mode == "exception")
                    self.assertEqual([row["id"] for row in pool.retry_rows],
                                     [row["id"] for row in rows[completed:worker._lastfmPoolBatchSize(kind)]])
                    self.assertFalse(Database._lastfm_active)


class LastfmPoolRepositoryRevalidationTest(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        Database._lastfm_active.clear()
        backfillers._LASTFM_CANDIDATE_POOLS.clear()
        self.addCleanup(Database._lastfm_active.clear)
        self.addCleanup(backfillers._LASTFM_CANDIDATE_POOLS.clear)

    def _database(self):
        tracks = {
            TRACK_ID: normalizeTrackForTest({
                "id": TRACK_ID,
                "name": "Track One",
                "artists": [{"id": ARTIST_ID, "name": "Artist One"}],
                "album": {
                    "id": ALBUM_ID,
                    "name": "Album One",
                    "url": "http://example.com/album/al1",
                    "totalTracks": 1,
                    "releaseDate": 0,
                    "imageUrl": "",
                },
            }),
        }
        entries = [{"id": TRACK_ID, "playedAt": 1000, "timePlayed": 5000}]
        return self._makeDb(tracks, entries, username=TEST_USERNAME)

    def _rowForKind(self, kind):
        entityId = {
            "artist": ARTIST_ID,
            "album": ALBUM_ID,
            "track": TRACK_ID,
            "bio": ARTIST_ID,
            "album_bio": ALBUM_ID,
        }[kind]
        return {"id": entityId, "name": f"{kind} row"}

    def _resolveWithRealSetter(self, db, kind):
        if kind == "artist":
            db.repo.replaceArtistGenres(ARTIST_ID, ["rock"])
            db.repo.markArtistsLastfmAttempted([ARTIST_ID])
        elif kind == "album":
            db.repo.replaceAlbumGenres(ALBUM_ID, ["indie"], inherited=False)
            db.repo.markAlbumsLastfmAttempted([ALBUM_ID])
        elif kind == "track":
            db.repo.replaceTrackGenres(TRACK_ID, ["pop"], inherited=False)
            db.repo.markTracksLastfmAttempted([TRACK_ID])
        elif kind == "bio":
            db.repo.setArtistBio(ARTIST_ID, "Artist biography")
        elif kind == "album_bio":
            db.repo.setAlbumBio(ALBUM_ID, "Album biography")
        else:
            raise AssertionError(f"unexpected kind: {kind}")

    def _state(self, db, kind):
        if kind == "artist":
            return (db.repo.getArtistGenres(ARTIST_ID),
                    db.repo.getArtistLastfmState(ARTIST_ID)["attempted_at"])
        if kind == "album":
            genres = db.repo.getAlbumGenres(ALBUM_ID)
            stamp = db.repo._conn().execute(
                "SELECT lastfm_attempted_at FROM albums WHERE id=?", (ALBUM_ID,)
            ).fetchone()["lastfm_attempted_at"]
            return genres, stamp
        if kind == "track":
            genres = db.repo.getTrackGenres(TRACK_ID)
            stamp = db.repo._conn().execute(
                "SELECT lastfm_attempted_at FROM tracks WHERE id=?", (TRACK_ID,)
            ).fetchone()["lastfm_attempted_at"]
            return genres, stamp
        if kind == "bio":
            state = db.repo.getArtistBioState(ARTIST_ID)
            return state["bio"], state["attempted_at"]
        if kind == "album_bio":
            state = db.repo.getAlbumBioState(ALBUM_ID)
            return state["bio"], state["attempted_at"]
        raise AssertionError(f"unexpected kind: {kind}")

    def test_real_setters_make_all_five_pooled_rows_ineligible_without_mutation(self):
        for kind, _batchSize in POOL_KINDS:
            with self.subTest(kind=kind):
                db = self._database()
                row = self._rowForKind(kind)
                dbPath = db.repo.connectionManager.dbPath
                backfillers._LASTFM_CANDIDATE_POOLS[(kind, TEST_USERNAME, dbPath)] = _LastfmCandidatePool(
                    [row], time.monotonic()
                )

                self._resolveWithRealSetter(db, kind)
                before = self._state(db, kind)
                fetch = Mock(side_effect=AssertionError("revalidation must not refill"))

                selected = db._pooledCandidates(kind, TEST_USERNAME, fetch)

                self.assertEqual(selected, [])
                self.assertEqual(self._state(db, kind), before)
                fetch.assert_not_called()

    def test_revalidation_never_reads_play_history(self):
        db = self._database()
        conn = db.repo._conn()
        def authorize(action, table, column, database, trigger):
            return sqlite3.SQLITE_DENY if table == "plays" else sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        try:
            for kind, _ in POOL_KINDS:
                with self.subTest(kind=kind):
                    result = db._lastfmRevalidateRows(kind, TEST_USERNAME, [self._rowForKind(kind)])
                    self.assertEqual(len(result), 1)
        finally:
            conn.set_authorizer(None)

    def test_newly_merged_track_is_excluded_from_pooled_revalidation(self):
        db = self._database()
        canonical = normalizeTrackForTest({"id": "canonical", "name": "Canonical",
                                          "artists": [{"id": ARTIST_ID, "name": "Artist"}]})
        db.repo.upsertTrack(canonical)
        conn = db.repo._conn()
        with conn:
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", ("canonical", TRACK_ID))
        self.assertEqual(db._lastfmRevalidateRows("track", TEST_USERNAME, [self._rowForKind("track")]), [])


if __name__ == "__main__":
    unittest.main()
