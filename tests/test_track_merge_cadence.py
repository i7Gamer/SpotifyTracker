"""HOW OFTEN the ISRC matcher is allowed to run.

The matcher used to run on every metadata-backfill cycle - every few minutes,
per user, so three times over - because it is idempotent and a run that merges
nothing invalidates nothing. What that reasoning missed is the run that DOES
merge something: it drops the cached Wrapped years its groups touch, and the
backfiller keeps completing pairs for as long as new ISRCs arrive. The live
instance merged 137 tracks in 23 batches across two days and paid for it with
147 Wrapped rebuilds a day, against ~20-40 before the toggle went on.

Narrowing the invalidation (test_wrapped_invalidation_scope) took the cost of
one batch down but not the number of batches: a 15-18 track batch still reached
most of the cache. This is the other half - the matcher gets one slot a day, so
the merges accumulate into it instead of trickling.

What that trades away, on purpose: a duplicate completed by a newly-arrived
ISRC now waits up to a day to fold. Nothing is wrong in the meantime - both
sides still count, they just count separately, which is the same thing the
instance showed for the years before the toggle existed.

The slot is claimed in app_settings rather than held in memory, for two
reasons: the three per-user backfiller threads share one instance-wide matcher
(so an in-process flag would let each thread run its own daily pass), and a
stamp on the class would not survive a restart - the trap
_catalogBackoffUntil already walked into, where a 1374-minute stand-down was
re-armed from scratch by the restart that walked back into it.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.queries._base import (TRACK_MERGE_LAST_RUN_KEY,
                                    TRACK_MERGE_MIN_INTERVAL_SECONDS)

NOW = 1_800_000_000.0   #< a fixed clock: nothing here should depend on the real one
WORKERS = 3             #< one metadata backfiller per live user
ROUNDS = 25             #< see test_only_one_of_three_concurrent_claims_wins
SPOTIFY_WORKER_NAME = "spotify_api"
MATCHER_FAILURE = "database is locked"
RELEASE_FAILURE = "still locked"
EMPTY_MERGE_SUMMARY = {"groups": 0, "merged": 0}


class CadenceTestCase(DatabaseTestCase):
    def _db(self):
        return self._makeDb({}, [])

    def _waitDurations(self, db):
        return [call.args[0] if call.args else None
                for call in db.backfiller_stop_event.wait.call_args_list]

    def assertReachedIdleWait(self, db):
        self.assertIn(db.BACKFILLER_IDLE_WAIT_SECONDS, self._waitDurations(db))

    def assertFailedSpotifyTelemetry(self, db, errorText):
        telemetry = db._getWorkerTelemetry(SPOTIFY_WORKER_NAME)
        self.assertEqual(telemetry["consecutive_failures"], 1)
        self.assertIn(errorText, telemetry["last_error"])


class TestTheDailySlot(CadenceTestCase):
    def test_the_first_claim_is_granted(self):
        db = self._db()

        self.assertTrue(db.repo.claimTrackMergeRun(now=NOW))

    def test_a_second_claim_the_same_day_is_refused(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertFalse(db.repo.claimTrackMergeRun(now=NOW + 60))

    def test_a_claim_a_second_short_of_the_interval_is_refused(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertFalse(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS - 1))

    def test_a_claim_once_the_interval_has_passed_is_granted(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertTrue(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS))

    def test_a_granted_claim_restarts_the_clock(self):
        """The stamp is the moment of the claim, not of the first one ever -
        otherwise the slot would drift to a fixed time of day and a run that
        started late would shorten the next gap."""
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)
        db.repo.claimTrackMergeRun(now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS)

        self.assertFalse(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS + 60))


class TestTheSlotIsSharedAndDurable(CadenceTestCase):
    def test_only_one_of_three_concurrent_claims_wins(self):
        """The three per-user backfillers reach the same instance-wide matcher.
        The claim is one conditional UPDATE, so the database settles it - a
        read-then-write lets more than one through the same open slot, and the
        consequence is three concurrent full-catalog merges.

        Repeated over ROUNDS days because one round is a coin flip: measured
        against a deliberately non-atomic implementation, a single round caught
        it 3 times in 6. Each round is its own day, so every one of them starts
        from a free slot. The assertion can never fail spuriously - exactly one
        winner is correct whatever order the threads happen to take - so the
        repetition only buys detection, never flakiness."""
        db = self._db()
        guard = threading.Lock()

        for round_ in range(ROUNDS):
            #< a fresh day each time, so the slot under contention is open
            day = NOW + round_ * TRACK_MERGE_MIN_INTERVAL_SECONDS
            results = []
            barrier = threading.Barrier(WORKERS)

            def claim(day=day, results=results):
                try:
                    barrier.wait()
                    granted = db.repo.claimTrackMergeRun(now=day)
                    with guard:
                        results.append(granted)
                finally:
                    #< ConnectionManager keeps one connection per THREAD, so
                    #  each worker has to hand its own back - an open handle
                    #  here keeps the temp database file locked and fails the
                    #  teardown, not the assertion
                    db.repo.connectionManager.close()

            threads = [threading.Thread(target=claim) for _ in range(WORKERS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(results.count(True), 1, f"round {round_}: {results}")
            self.assertEqual(len(results), WORKERS)

    def test_the_stamp_lives_in_the_database_not_the_process(self):
        """A restart must not hand back a slot that was already spent."""
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertEqual(float(db.repo.getAppSetting(TRACK_MERGE_LAST_RUN_KEY)), NOW)

    def test_an_unreadable_stamp_does_not_wedge_the_matcher(self):
        """Failing open: a value that is not a number reads as "never ran", so
        the worst case is one extra pass rather than a feature that is silently
        off forever."""
        db = self._db()
        db.repo.setAppSetting(TRACK_MERGE_LAST_RUN_KEY, "not-a-timestamp")

        self.assertTrue(db.repo.claimTrackMergeRun(now=NOW))


class TestTheAdminRunOwnsTheSlotItUses(CadenceTestCase):
    """Turning the checkbox on runs the matcher then and there (routes/admin.py)
    - that is what makes the toggle feel like a switch. It takes the day's slot
    with it, so the backfiller does not repeat a pass that just happened."""

    def test_a_stamped_run_refuses_the_next_claim(self):
        db = self._db()
        db.repo.stampTrackMergeRun(now=NOW)

        self.assertFalse(db.repo.claimTrackMergeRun(now=NOW + 60))

    def test_a_stamp_overrides_an_older_one(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)
        db.repo.stampTrackMergeRun(now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS)

        self.assertFalse(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS + 60))


class TestTheLoopIsWiredToIt(CadenceTestCase):
    def test_the_loop_skips_the_matcher_when_the_claim_is_refused(self):
        """The cadence is worth nothing if a refused claim still runs."""
        from unittest.mock import MagicMock
        from test_metadata_backfiller import runsOneCycle

        db = self._db()
        db.repo.isTrackMergeEnabled = MagicMock(return_value=True)
        db.repo.claimTrackMergeRun = MagicMock(return_value=False)
        db.repo.mergeTracksByIsrc = MagicMock(return_value=EMPTY_MERGE_SUMMARY)
        db.getUserSpotifyCredentials = MagicMock(return_value=None)
        db._backfillTrackIsrcs = MagicMock()
        db.backfiller_stop_event = MagicMock()
        runsOneCycle(db, db.backfiller_stop_event)

        db._metadataBackfillLoop()

        db.repo.isTrackMergeEnabled.assert_called_once()
        db.repo.claimTrackMergeRun.assert_called_once()
        db.repo.mergeTracksByIsrc.assert_not_called()

    def test_the_admin_enable_path_stamps_the_run(self):
        import inspect
        import routes.admin
        source = inspect.getsource(routes.admin)

        self.assertIn("mergeTracksByIsrc(enableSetting=True)", source)


class TestAFailedPassDoesNotSpendTheDay(CadenceTestCase):
    """The claim is taken BEFORE the matcher runs - it has to be, or three
    workers would all pass a check-then-act - so a pass that then raises has
    stamped a run that never happened. Left alone that is the worst failure the
    slot can produce: not one extra pass, but no pass at all for 24 hours,
    which is the "silently off" outcome claimTrackMergeRun's own docstring says
    it would rather fail open than reach.

    The trigger is ordinary rather than exotic: mergeTracksByIsrc opens a write
    transaction, and this database has a concurrent writer whose atomic
    overwrite import holds the write lock long enough to exhaust busy_timeout.
    The cycle's catch-all logs it and moves on; the stamp stays.

    Driven through the real loop, because the claim, the call and the release
    are three separate statements and only their arrangement is the fix.

    These assert against time.time() rather than this file's fixed NOW, and
    that is load-bearing: the loop stamps with the real clock, NOW is in 2027,
    and every stamp therefore looks a day old to it - the first draft of the
    two tests below passed with the fix absent for exactly that reason. No
    sleeps and no tolerances follow from it; every comparison is an offset from
    one captured instant."""

    def _dbReadyForACycle(self):
        from unittest.mock import MagicMock
        from Database.database import Database
        from test_metadata_backfiller import runsOneCycle

        db = self._db()
        db.repo.setTrackMergeEnabled(True)
        db.getUserSpotifyCredentials = MagicMock(return_value=None)
        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        runsOneCycle(db, db.backfiller_stop_event)
        return db

    def _runCycle(self, db):
        db._metadataBackfillLoop()

    def test_a_raising_matcher_leaves_the_slot_open(self):
        import time
        from unittest.mock import MagicMock

        db = self._dbReadyForACycle()
        db.repo.mergeTracksByIsrc = MagicMock(side_effect=Exception(MATCHER_FAILURE))
        db.backfiller_stop_event.wait.reset_mock()
        started = time.time()

        self._runCycle(db)

        db.repo.mergeTracksByIsrc.assert_called_once()
        self.assertTrue(
            db.repo.claimTrackMergeRun(now=started),
            "a pass that raised spent the day's slot without merging anything")
        self.assertFailedSpotifyTelemetry(db, MATCHER_FAILURE)
        self.assertReachedIdleWait(db)

    def test_a_raising_matcher_restores_the_PREVIOUS_run_not_never_ran(self):
        """Re-opening the slot must not also forget the pass that really did
        run - clearing the key would hand the matcher a full day of per-cycle
        re-claims, which is the cost the slot exists to stop.

        The previous run is seeded OLDER than the interval, and that is what
        makes this test real: seeded fresh, the loop's claim is REFUSED, the
        raising matcher never executes, and every assertion holds against a
        stamp nothing touched - the first version of this test did exactly
        that and passed with the release logic deleted. assert_called_once is
        the proof the claimed-then-raised path actually ran; the stamp
        equality is the release's documented contract (byte-for-byte back),
        and it tells restore apart from both clearing (None) and leaving the
        claim's own fresh stamp behind."""
        import time
        from unittest.mock import MagicMock

        db = self._dbReadyForACycle()
        #< 25h ago: old enough that the loop's claim GOES THROUGH
        previousRunAt = time.time() - TRACK_MERGE_MIN_INTERVAL_SECONDS - 3600
        db.repo.stampTrackMergeRun(now=previousRunAt)
        previousStamp = db.repo.getTrackMergeLastRun()
        db.repo.mergeTracksByIsrc = MagicMock(side_effect=Exception("boom"))
        db.backfiller_stop_event.wait.reset_mock()

        self._runCycle(db)

        db.repo.mergeTracksByIsrc.assert_called_once()
        self.assertEqual(db.repo.getTrackMergeLastRun(), previousStamp,
                         "the release must put back the run that really "
                         "happened - neither clear the key nor leave the "
                         "failed claim's own stamp standing")
        self.assertFailedSpotifyTelemetry(db, "boom")
        self.assertReachedIdleWait(db)

    def test_a_release_that_itself_fails_keeps_the_matchers_own_error(self):
        """Writes failing is the likeliest reason the merge failed, so the
        give-back can fail for the same reason. It must not become the error
        the cycle reports - the matcher's traceback is the one worth reading -
        and it must not take the loop down with it."""
        from unittest.mock import MagicMock

        db = self._dbReadyForACycle()
        db.repo.mergeTracksByIsrc = MagicMock(side_effect=Exception(MATCHER_FAILURE))
        db.repo.releaseTrackMergeRun = MagicMock(side_effect=Exception(RELEASE_FAILURE))
        db.backfiller_stop_event.wait.reset_mock()

        with self.assertLogs(level="WARNING") as captured:
            self._runCycle(db)   #< the cycle's catch-all keeps the loop alive

        db.repo.releaseTrackMergeRun.assert_called_once()
        self.assertTrue(
            any("database is locked" in line for line in captured.output),
            "the matcher's own failure must still be what the cycle reports")
        self.assertTrue(
            any("Could not release the ISRC merge slot" in line for line in captured.output),
            "the release failure should still be named for diagnosis")
        self.assertFailedSpotifyTelemetry(db, MATCHER_FAILURE)
        self.assertReachedIdleWait(db)

    def test_a_succeeding_matcher_still_spends_the_slot(self):
        """The control: without it both tests above pass against a loop that
        simply never claims."""
        import time
        from unittest.mock import MagicMock

        db = self._dbReadyForACycle()
        db.repo.mergeTracksByIsrc = MagicMock(return_value=EMPTY_MERGE_SUMMARY)
        started = time.time()

        self._runCycle(db)

        db.repo.mergeTracksByIsrc.assert_called_once()
        self.assertFalse(db.repo.claimTrackMergeRun(now=started))


if __name__ == "__main__":
    unittest.main()
