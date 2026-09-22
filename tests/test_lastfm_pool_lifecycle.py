# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pool retirement and concurrency contracts without live data or HTTP."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from Database.database import Database
import Database.workers.lastfm_backfillers as workers


THREAD_TIMEOUT_SECONDS = 5
FETCH_TIMEOUT_SECONDS = THREAD_TIMEOUT_SECONDS * 2
START_TIME = 100
REGISTRY_ACCESS_REPEAT_COUNT = 5


class PoolHarness(workers.LastfmBackfillMixin):
    LASTFM_QUEUE_POOL_SIZE = Database.LASTFM_QUEUE_POOL_SIZE
    LASTFM_QUEUE_POOL_TTL_SECONDS = Database.LASTFM_QUEUE_POOL_TTL_SECONDS
    LASTFM_QUEUE_DRAINED_MEMO_SECONDS = Database.LASTFM_QUEUE_DRAINED_MEMO_SECONDS
    LASTFM_QUEUE_BATCH_SIZE = Database.LASTFM_QUEUE_BATCH_SIZE
    LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE = Database.LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE
    LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE = Database.LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE

    def __init__(self, path="database-a"):
        self.repo = SimpleNamespace(connectionManager=SimpleNamespace(dbPath=path))

    def _lastfmRevalidateRows(self, kind, scopeUsername, rows):
        return rows


@pytest.fixture
def clock(monkeypatch):
    workers._LASTFM_CANDIDATE_POOLS.clear()
    Database._lastfm_active.clear()
    now = [START_TIME]
    monkeypatch.setattr(workers._dbmod.time, "monotonic", lambda: now[0])
    yield now
    workers._LASTFM_CANDIDATE_POOLS.clear()
    Database._lastfm_active.clear()


def rows(prefix="row"):
    return [{"id": f"{prefix}-{index}", "name": f"Name {index}"}
            for index in range(Database.LASTFM_QUEUE_POOL_SIZE)]


@pytest.mark.parametrize("retry", [False, True])
def test_idle_scope_is_evicted_but_inflight_batch_and_late_retry_survive(clock, retry):
    worker = PoolHarness()
    fetched = rows()
    retired = worker._pooledCandidates("artist", "retired", lambda _: fetched)
    worker._finishPooledCandidates("artist", "retired", retired, retired if retry else [])
    inflight = worker._pooledCandidates("bio", "active", lambda _: fetched)
    clock[0] += worker.LASTFM_QUEUE_POOL_TTL_SECONDS - 1
    worker._pooledCandidates("album", "new", lambda _: [])
    assert ("artist", "retired", "database-a") in workers._LASTFM_CANDIDATE_POOLS
    clock[0] += 1

    worker._pooledCandidates("album", "new", lambda _: [])

    assert ("artist", "retired", "database-a") in workers._LASTFM_CANDIDATE_POOLS
    clock[0] += worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS
    worker._pooledCandidates("album", "new", lambda _: [])
    assert ("artist", "retired", "database-a") not in workers._LASTFM_CANDIDATE_POOLS
    assert ("bio", "active", "database-a") in workers._LASTFM_CANDIDATE_POOLS
    worker._finishPooledCandidates("bio", "active", inflight, inflight)
    worker._pooledCandidates("track", "new", lambda _: [])
    replay = worker._pooledCandidates("bio", "active", lambda _: fetched)
    assert replay == inflight
    worker._finishPooledCandidates("bio", "active", replay, [])
    assert not Database._lastfm_active


@pytest.mark.parametrize("other_kind,other_scope,other_path", [
    ("bio", "user", "database-a"),
    ("artist", "other-user", "database-a"),
    ("artist", "user", "database-b"),
])
def test_slow_refill_does_not_block_other_pool_fetch_or_batch_finish(
        clock, other_kind, other_scope, other_path):
    worker = PoolHarness()
    other = PoolHarness(other_path)
    inflight = other._pooledCandidates(other_kind, other_scope, lambda _: rows("other"))
    started = threading.Event()
    release = threading.Event()

    def slowFetch(_):
        started.set()
        assert release.wait(FETCH_TIMEOUT_SECONDS)
        return rows("slow")

    def finishAndClaim():
        other._finishPooledCandidates(other_kind, other_scope, inflight, inflight)
        return other._pooledCandidates(other_kind, other_scope, lambda _: rows("other"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        slow = executor.submit(worker._pooledCandidates, "artist", "user", slowFetch)
        try:
            assert started.wait(THREAD_TIMEOUT_SECONDS)
            unrelated = executor.submit(finishAndClaim)
            replay = unrelated.result(timeout=THREAD_TIMEOUT_SECONDS)
            assert replay == inflight
        finally:
            release.set()
        batch = slow.result(timeout=THREAD_TIMEOUT_SECONDS)
    worker._finishPooledCandidates("artist", "user", batch, [])
    other._finishPooledCandidates(other_kind, other_scope, replay, [])
    assert not Database._lastfm_active


def test_same_pool_refill_is_shared_and_fetching_pool_is_not_retired(clock):
    worker = PoolHarness()
    entered = threading.Event()
    release = threading.Event()
    secondStarted = threading.Event()

    def fetch(_):
        entered.set()
        assert release.wait(FETCH_TIMEOUT_SECONDS)
        return rows()

    fetchSpy = Mock(side_effect=fetch)

    def secondClaim():
        secondStarted.set()
        return worker._pooledCandidates("artist", "user", fetchSpy)

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(worker._pooledCandidates, "artist", "user", fetchSpy)
        try:
            assert entered.wait(THREAD_TIMEOUT_SECONDS)
            second = executor.submit(secondClaim)
            assert secondStarted.wait(THREAD_TIMEOUT_SECONDS)
            clock[0] += worker.LASTFM_QUEUE_POOL_TTL_SECONDS
            unrelated = executor.submit(worker._pooledCandidates, "bio", "new", lambda _: [])
            assert unrelated.result(timeout=THREAD_TIMEOUT_SECONDS) == []
            assert ("artist", "user", "database-a") in workers._LASTFM_CANDIDATE_POOLS
        finally:
            release.set()
        batches = [future.result(timeout=THREAD_TIMEOUT_SECONDS) for future in (first, second)]
    assert fetchSpy.call_count == 1
    assert len({row["id"] for batch in batches for row in batch}) == 2 * worker.LASTFM_QUEUE_BATCH_SIZE
    for batch in batches:
        worker._finishPooledCandidates("artist", "user", batch, [])


def test_failed_refill_does_not_leave_a_locked_or_pinned_pool(clock):
    worker = PoolHarness()
    fetch = Mock(side_effect=[RuntimeError("database stalled"), rows()])
    with pytest.raises(RuntimeError, match="database stalled"):
        worker._pooledCandidates("artist", "user", fetch)
    batch = worker._pooledCandidates("artist", "user", fetch)
    worker._finishPooledCandidates("artist", "user", batch, [])
    clock[0] += (worker.LASTFM_QUEUE_POOL_TTL_SECONDS
                 + worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS)
    worker._pooledCandidates("bio", "new", lambda _: [])
    assert ("artist", "user", "database-a") not in workers._LASTFM_CANDIDATE_POOLS
    assert not Database._lastfm_active


def test_revalidation_failure_releases_pin_and_preserves_next_retry(clock):
    worker = PoolHarness()
    fetched = rows()
    worker._lastfmRevalidateRows = Mock(side_effect=RuntimeError("revalidation failed"))
    with pytest.raises(RuntimeError, match="revalidation failed"):
        worker._pooledCandidates("artist", "user", lambda _: fetched)
    assert not Database._lastfm_active
    worker._lastfmRevalidateRows = lambda kind, scope, batch: batch
    replay = worker._pooledCandidates("artist", "user", lambda _: fetched)
    assert replay == fetched[:worker.LASTFM_QUEUE_BATCH_SIZE]
    worker._finishPooledCandidates("artist", "user", replay, [])
    clock[0] += (worker.LASTFM_QUEUE_POOL_TTL_SECONDS
                 + worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS)
    worker._pooledCandidates("bio", "new", lambda _: [])
    assert ("artist", "user", "database-a") not in workers._LASTFM_CANDIDATE_POOLS


def test_stale_revalidation_does_not_pin_retired_pool(clock):
    worker = PoolHarness()
    worker._lastfmRevalidateRows = Mock(return_value=[])
    assert worker._pooledCandidates("artist", "user", lambda _: rows()) == []
    assert not Database._lastfm_active
    clock[0] += (worker.LASTFM_QUEUE_POOL_TTL_SECONDS
                 + worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS)
    worker._pooledCandidates("bio", "new", lambda _: [])
    assert ("artist", "user", "database-a") not in workers._LASTFM_CANDIDATE_POOLS


def test_finish_without_pool_releases_claims_without_creating_an_entry(clock):
    worker = PoolHarness()
    claimed = worker._claimLastfmEntities("artist", rows()[:worker.LASTFM_QUEUE_BATCH_SIZE])
    worker._finishPooledCandidates("artist", "absent", claimed, claimed)
    assert not Database._lastfm_active
    assert not workers._LASTFM_CANDIDATE_POOLS


def test_pool_registry_maintenance_is_amortized_and_resumes_after_clock_rollback(
        clock, monkeypatch):
    class CountingRegistry(dict):
        scanCount = 0

        def items(self):
            self.scanCount += 1
            return super().items()

    registry = CountingRegistry()
    monkeypatch.setattr(workers, "_LASTFM_CANDIDATE_POOLS", registry)
    monkeypatch.setattr(workers, "_LASTFM_CANDIDATE_POOLS_LAST_MAINTENANCE_AT", None,
                        raising=False)
    worker = PoolHarness()

    worker._pooledCandidates("artist", "old", lambda _: [])
    firstScanCount = registry.scanCount
    for _ in range(REGISTRY_ACCESS_REPEAT_COUNT):
        worker._pooledCandidates("artist", "old", lambda _: [])
        worker._finishPooledCandidates("artist", "old", [], [])
    assert registry.scanCount == firstScanCount

    clock[0] += worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS
    worker._pooledCandidates("artist", "old", lambda _: [])
    assert registry.scanCount == firstScanCount + 1

    clock[0] -= worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS + 1
    worker._pooledCandidates("artist", "old", lambda _: [])
    assert registry.scanCount == firstScanCount + 2


def test_delayed_pool_maintenance_keeps_pinned_pool_then_retires_it(clock):
    worker = PoolHarness()
    claimed = worker._pooledCandidates("artist", "old", lambda _: rows())
    oldKey = ("artist", "old", "database-a")

    clock[0] += worker.LASTFM_QUEUE_POOL_TTL_SECONDS + worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS
    worker._pooledCandidates("bio", "new", lambda _: [])
    assert oldKey in workers._LASTFM_CANDIDATE_POOLS

    worker._finishPooledCandidates("artist", "old", claimed, [])
    clock[0] += worker.LASTFM_QUEUE_POOL_TTL_SECONDS + worker.LASTFM_POOL_MAINTENANCE_INTERVAL_SECONDS
    worker._pooledCandidates("track", "newer", lambda _: [])
    assert oldKey not in workers._LASTFM_CANDIDATE_POOLS
