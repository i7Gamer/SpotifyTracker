# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import threading
import time
import unittest
from unittest.mock import patch

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from Database.lastfm import ArtistInfoOutcome, OUTCOME_OK
from Database.workers.lastfm_backfillers import (
    LastfmBackfillMixin,
    _LastfmCandidatePool,
    _LASTFM_CANDIDATE_POOLS,
)


POOL_SIZE = 600
GENRE_BATCH_SIZE = 30
BIOGRAPHY_BATCH_SIZE = 20


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


class LastfmPoolContractTests(unittest.TestCase):
    def setUp(self):
        Database._lastfm_active.clear()
        import Database.workers.lastfm_backfillers as backfillers
        backfillers._LASTFM_CANDIDATE_POOLS.clear()
        self.addCleanup(Database._lastfm_active.clear)
        self.addCleanup(backfillers._LASTFM_CANDIDATE_POOLS.clear)

    def test_genre_pool_is_fetched_once_for_twenty_batches(self):
        db = _PoolHarness()
        rows = [{"id": f"a{i}", "name": f"Artist {i}"} for i in range(POOL_SIZE)]
        fetch = lambda limit: rows[:limit]

        with patch("Database.workers.lastfm_backfillers._dbmod.time.monotonic", return_value=10):
            selected = []
            for _ in range(POOL_SIZE // GENRE_BATCH_SIZE):
                batch = db._pooledCandidates("artist", "user1", fetch)
                selected.extend(row["id"] for row in batch)
                db._finishPooledCandidates("artist", "user1", batch, ())

        self.assertEqual(selected, [f"a{i}" for i in range(POOL_SIZE)])

    def test_biography_pool_uses_twenty_row_batches(self):
        db = _PoolHarness()
        rows = [{"id": f"a{i}", "name": f"Artist {i}"} for i in range(POOL_SIZE)]
        fetch = lambda limit: rows[:limit]

        selected = []
        for _ in range(POOL_SIZE // BIOGRAPHY_BATCH_SIZE):
            batch = db._pooledCandidates("bio", "user1", fetch)
            selected.extend(row["id"] for row in batch)
            db._finishPooledCandidates("bio", "user1", batch, ())

        self.assertEqual(selected, [f"a{i}" for i in range(POOL_SIZE)])

    def test_pool_key_includes_scope(self):
        db = _PoolHarness()
        user_rows = [{"id": "user-row", "name": "User"}]
        global_rows = [{"id": "global-row", "name": "Global"}]
        fetch = {"user1": lambda limit: user_rows, None: lambda limit: global_rows}

        user_batch = db._pooledCandidates("artist", "user1", fetch["user1"])
        global_batch = db._pooledCandidates("artist", None, fetch[None])

        self.assertEqual([row["id"] for row in user_batch], ["user-row"])
        self.assertEqual([row["id"] for row in global_batch], ["global-row"])
        db._finishPooledCandidates("artist", "user1", user_batch, ())
        db._finishPooledCandidates("artist", None, global_batch, ())

    def test_expired_pool_refills_even_when_rows_remain(self):
        db = _PoolHarness()
        fetch = unittest.mock.Mock(side_effect=[
            [{"id": "a1", "name": "A1"}],
            [{"id": "a2", "name": "A2"}],
        ])

        with patch("Database.workers.lastfm_backfillers._dbmod.time.monotonic", side_effect=[0, 1801]):
            first = db._pooledCandidates("artist", "user1", fetch)
            db._finishPooledCandidates("artist", "user1", first, ())
            second = db._pooledCandidates("artist", "user1", fetch)
            db._finishPooledCandidates("artist", "user1", second, ())

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([row["id"] for row in second], ["a2"])

    def test_transient_row_is_the_only_retry_prefix_on_next_cycle(self):
        db = _PoolHarness()
        rows = [{"id": f"a{i}", "name": f"Artist {i}"} for i in range(GENRE_BATCH_SIZE)]
        fetch = lambda limit: rows

        first = db._pooledCandidates("artist", "user1", fetch)
        transient = first[-1:]
        db._finishPooledCandidates("artist", "user1", first, transient)
        second = db._pooledCandidates("artist", "user1", fetch)

        self.assertEqual([row["id"] for row in second], [rows[-1]["id"]])
        db._finishPooledCandidates("artist", "user1", second, ())

    def test_persistent_transient_retry_does_not_refill_the_database_pool(self):
        db = _PoolHarness()
        rows = [{"id": "a1", "name": "A1"}]
        fetch = unittest.mock.Mock(return_value=rows)

        first = db._pooledCandidates("artist", "user1", fetch)
        db._finishPooledCandidates("artist", "user1", first, first)
        second = db._pooledCandidates("artist", "user1", fetch)
        db._finishPooledCandidates("artist", "user1", second, second)
        third = db._pooledCandidates("artist", "user1", fetch)
        db._finishPooledCandidates("artist", "user1", third, third)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([row["id"] for row in third], ["a1"])

    def test_exhaustion_refills_once_and_preserves_held_retry_rows(self):
        db = _PoolHarness()
        rows = [{"id": f"a{i}", "name": f"A{i}"} for i in range(GENRE_BATCH_SIZE + 1)]
        fetch = unittest.mock.Mock(side_effect=[rows, []])

        first = db._pooledCandidates("artist", "user1", fetch)
        db._finishPooledCandidates("artist", "user1", first, ())
        Database._lastfm_active.add(("artist", f"a{GENRE_BATCH_SIZE}"))
        self.assertEqual(db._pooledCandidates("artist", "user1", fetch), [])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(db._pooledCandidates("artist", "user1", fetch), [])
        self.assertEqual(fetch.call_count, 2)
        pool = _LASTFM_CANDIDATE_POOLS[("artist", "user1", None)]
        self.assertEqual([row["id"] for row in pool.retry_rows], [f"a{GENRE_BATCH_SIZE}"])
        Database._lastfm_active.remove(("artist", f"a{GENRE_BATCH_SIZE}"))

        retry = db._pooledCandidates("artist", "user1", fetch)
        self.assertEqual([row["id"] for row in retry], [f"a{GENRE_BATCH_SIZE}"])
        db._finishPooledCandidates("artist", "user1", retry, ())

    def test_three_workers_skip_held_prefix_and_bound_claims(self):
        rows = [{"id": f"a{i}", "name": f"Artist {i}"} for i in range(POOL_SIZE)]
        fetch = lambda limit: rows
        workers = [_PoolHarness() for _ in range(3)]

        first = workers[0]._pooledCandidates("artist", "user1", fetch)
        second = workers[1]._pooledCandidates("artist", "user1", fetch)
        third = workers[2]._pooledCandidates("artist", "user1", fetch)

        self.assertEqual(len(first), GENRE_BATCH_SIZE)
        self.assertEqual(len(second), GENRE_BATCH_SIZE)
        self.assertEqual(len(third), GENRE_BATCH_SIZE)
        self.assertEqual(len(Database._lastfm_active), GENRE_BATCH_SIZE * 3)
        self.assertEqual(len({row["id"] for row in first + second + third}), GENRE_BATCH_SIZE * 3)
        for worker, batch in zip(workers, (first, second, third)):
            worker._finishPooledCandidates("artist", "user1", batch, ())


class LastfmPoolRepositoryTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        Database._lastfm_active.clear()
        _LASTFM_CANDIDATE_POOLS.clear()
        self.addCleanup(Database._lastfm_active.clear)
        self.addCleanup(_LASTFM_CANDIDATE_POOLS.clear)

    def test_stale_artist_bio_is_skipped_without_changing_stored_value_or_stamp(self):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}]},
        }
        db = self._makeDb(
            tracks,
            [{"id": "tA", "playedAt": 1000, "timePlayed": 5000}],
            username="user1",
        )
        queued = db.repo.getArtistsMissingBiographies(600, "user1")
        dbPath = db.repo.connectionManager.dbPath
        _LASTFM_CANDIDATE_POOLS[("bio", "user1", dbPath)] = _LastfmCandidatePool(
            queued, time.monotonic()
        )
        db.repo.setArtistBio("aX", "Existing biography")
        before = db.repo.getArtistBioState("aX")

        client = unittest.mock.MagicMock()
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "New biography")
        processed = db._processLastfmBiographyBatch(client, "user1")

        self.assertFalse(processed)
        self.assertEqual(db.repo.getArtistBioState("aX"), before)
        client.getArtistInfo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
