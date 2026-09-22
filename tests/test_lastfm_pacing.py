# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty-fetch memo and bounded productive-loop pacing, with virtual latency."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from Database.database import Database
import Database.database as dbModule
import Database.workers.lastfm_backfillers as workers

KINDS = ("artist", "album", "track", "bio", "album_bio")
THROUGHPUT_LOSS_TOLERANCE = 0.05  # Declared before tuning the pause ratio.
CYCLE_COUNT = 10
LOOKUPS_PER_CYCLE = 20
LOOKUP_LATENCIES = (0.25, 1.0)
LOOKUP_START_INTERVAL_SECONDS = 0.25
WORKER_BARRIER_TIMEOUT_SECONDS = 5


class QueueHarness(workers.LastfmBackfillMixin):
    LASTFM_QUEUE_POOL_SIZE = Database.LASTFM_QUEUE_POOL_SIZE
    LASTFM_QUEUE_POOL_TTL_SECONDS = Database.LASTFM_QUEUE_POOL_TTL_SECONDS
    LASTFM_QUEUE_DRAINED_MEMO_SECONDS = Database.LASTFM_QUEUE_DRAINED_MEMO_SECONDS
    LASTFM_QUEUE_BATCH_SIZE = Database.LASTFM_QUEUE_BATCH_SIZE
    LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE = Database.LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE
    LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE = Database.LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE

    def __init__(self):
        self.repo = None

    def _lastfmRevalidateRows(self, kind, scopeUsername, rows):
        return rows


@pytest.fixture
def queue(monkeypatch):
    workers._LASTFM_CANDIDATE_POOLS.clear()
    Database._lastfm_active.clear()
    clock = [0]
    monkeypatch.setattr(workers._dbmod.time, "monotonic", lambda: clock[0])
    yield QueueHarness(), clock
    workers._LASTFM_CANDIDATE_POOLS.clear()
    Database._lastfm_active.clear()


@pytest.mark.parametrize("kind", KINDS)
def test_empty_fetch_is_memoized_until_deadline_and_nonempty_fetch_clears_it(queue, kind):
    worker, clock = queue
    fetch = Mock(side_effect=[[], [{"id": "new", "name": "new"}]])
    assert worker._pooledCandidates(kind, "user", fetch) == []
    clock[0] = worker.LASTFM_QUEUE_DRAINED_MEMO_SECONDS - 1
    assert worker._pooledCandidates(kind, "user", fetch) == []
    assert fetch.call_count == 1
    clock[0] += 1
    claimed = worker._pooledCandidates(kind, "user", fetch)
    assert [row["id"] for row in claimed] == ["new"]
    assert fetch.call_count == 2
    assert workers._LASTFM_CANDIDATE_POOLS[(kind, "user", None)].drained_until is None
    worker._finishPooledCandidates(kind, "user", claimed, [])


def test_memo_does_not_cross_kind_or_scope(queue):
    worker, _ = queue
    empty = Mock(return_value=[])
    for kind, scope in (("artist", "user"), ("artist", None), ("album", "user")):
        worker._pooledCandidates(kind, scope, empty)
    assert empty.call_count == 3


@pytest.mark.parametrize("kind", KINDS)
def test_transient_and_held_rows_never_mark_a_pool_drained(queue, kind):
    worker, _ = queue
    fetch = Mock(return_value=[{"id": "pending", "name": "pending"}])
    first = worker._pooledCandidates(kind, "user", fetch)
    worker._finishPooledCandidates(kind, "user", first, first)
    Database._lastfm_active.add((kind, "pending"))
    assert worker._pooledCandidates(kind, "user", fetch) == []
    assert workers._LASTFM_CANDIDATE_POOLS[(kind, "user", None)].drained_until is None
    Database._lastfm_active.clear()
    second = worker._pooledCandidates(kind, "user", fetch)
    assert [row["id"] for row in second] == ["pending"]
    assert fetch.call_count == 1
    worker._finishPooledCandidates(kind, "user", second, [])


class VirtualStop:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, duration):
        self.waits.append(duration)
        if not self.stopped:
            self.clock[0] += duration
        return self.stopped


def runLoop(worker, event, work):
    worker._runLastfmLoop(
        stop_event=event, eventAttr="unused", minStartDelay=0, maxStartDelay=0,
        idleWaitSeconds=Database.LASTFM_IDLE_WAIT_SECONDS, enabled=lambda: True,
        runWork=work, logPrefix="Test", errorLabel="test", telemetryKey="test")


def loopWorker(ratio):
    worker = QueueHarness()
    worker.user = "synthetic"
    worker.repo = SimpleNamespace(getUserLastfmApiKey=lambda _: "synthetic")
    worker._recordWorkerCycle = Mock()
    worker.LASTFM_WORKING_CYCLE_PAUSE_RATIO = ratio
    worker.LASTFM_WORKING_CYCLE_PAUSE_MAX_SECONDS = Database.LASTFM_WORKING_CYCLE_PAUSE_MAX_SECONDS
    return worker


@pytest.mark.parametrize("workerCount", [1, 3])
@pytest.mark.parametrize("latency", LOOKUP_LATENCIES)
def test_productive_lookup_throughput_stays_within_declared_tolerance(monkeypatch, workerCount, latency):
    # Execute the production loop with fixed-latency mocked lookups. A virtual
    # clock avoids OS scheduling noise turning a preservation check flaky.
    local = threading.local()
    monkeypatch.setattr(workers._dbmod.time, "monotonic", lambda: local.clock[0])

    def measure(ratio):
        slots = [0]
        slotLock = threading.Lock()
        ready = {}
        finished = {}

        def schedule():
            # Reserve by logical arrival time, not OS thread scheduling order.
            for index, available in sorted(ready.items(), key=lambda item: (item[1], item[0])):
                start = max(available, slots[0])
                slots[0] = start + LOOKUP_START_INTERVAL_SECONDS
                finished[index] = start + latency

        barrier = threading.Barrier(workerCount, action=schedule)

        def lookup(name):
            # All workers issue one lookup per scheduling step. Reserve starts
            # at four per second, then let the HTTP latency elapse per worker.
            with slotLock:
                ready[local.index] = local.clock[0]
            barrier.wait(timeout=WORKER_BARRIER_TIMEOUT_SECONDS)
            local.clock[0] = finished[local.index]
            local.calls += 1

        monkeypatch.setattr(dbModule, "LastfmClient",
                            lambda _: SimpleNamespace(getArtistInfo=lookup))

        def run(index):
            local.clock = [0]
            local.calls = 0
            local.index = index
            event = VirtualStop(local.clock)
            worker = loopWorker(ratio)

            def work(client, scope, **kwargs):
                for _ in range(LOOKUPS_PER_CYCLE):
                    client.getArtistInfo("synthetic")
                if local.calls == CYCLE_COUNT * LOOKUPS_PER_CYCLE:
                    event.stopped = True
                return True

            runLoop(worker, event, work)
            return local.calls, local.clock[0]

        with ThreadPoolExecutor(max_workers=workerCount) as executor:
            results = list(executor.map(run, range(workerCount)))
        return sum(count for count, _ in results) / max(elapsed for _, elapsed in results)

    baseline = measure(0)
    paced = measure(Database.LASTFM_WORKING_CYCLE_PAUSE_RATIO)
    print(f"workers={workerCount}, latency={latency}s: {baseline:.4f} -> {paced:.4f} lookups/s; loss={1 - paced / baseline:.2%}")
    assert paced >= baseline * (1 - THROUGHPUT_LOSS_TOLERANCE)
    assert Database.LASTFM_WORKING_CYCLE_PAUSE_RATIO > 0


def test_empty_cycle_uses_global_fallback_and_idle_wait_without_productive_pause(monkeypatch):
    clock = [0]
    monkeypatch.setattr(workers._dbmod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dbModule, "LastfmClient", lambda _: None)
    event = VirtualStop(clock)
    worker = loopWorker(Database.LASTFM_WORKING_CYCLE_PAUSE_RATIO)
    calls = []

    def work(client, scope, **kwargs):
        calls.append(scope)
        if scope is None:
            event.stopped = True
        return False

    runLoop(worker, event, work)
    assert calls == ["synthetic", None]
    assert event.waits == [0, Database.LASTFM_IDLE_WAIT_SECONDS]


def test_productive_pause_is_capped_and_interruptible(monkeypatch):
    clock = [0]
    monkeypatch.setattr(workers._dbmod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dbModule, "LastfmClient", lambda _: None)
    worker = loopWorker(Database.LASTFM_WORKING_CYCLE_PAUSE_RATIO)
    event = VirtualStop(clock)
    event.wait = Mock(side_effect=[False, True])
    duration = worker.LASTFM_WORKING_CYCLE_PAUSE_MAX_SECONDS / worker.LASTFM_WORKING_CYCLE_PAUSE_RATIO * 2

    def work(client, scope, **kwargs):
        if clock[0]:
            raise RuntimeError("unpaced loop exceeded one cycle")
        clock[0] += duration
        return True

    runLoop(worker, event, work)
    assert [call.args[0] for call in event.wait.call_args_list] == [0, worker.LASTFM_WORKING_CYCLE_PAUSE_MAX_SECONDS]
