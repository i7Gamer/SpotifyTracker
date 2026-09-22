"""Background-worker startup is a lifecycle step, not a construction side-effect.

Constructing SpotifyDashboardApp used to start four background workers (backup,
email, version-check, login-check) plus - via checkLogin_thread's synchronous
first pass - one Spotify listener per user. That made `SpotifyDashboardApp()`
unusable without a patch stack, and rebound the process-global EMAIL_WORKER
singleton to whichever app was built last: under the parallel test runner a job
queued by one test could be processed against another test's temp database.

startWorkers() now owns that, so construction is inert and the workers start
exactly once, when an entry point asks for them.
"""
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = "app.SpotifyDashboardApp._get_or_create_secret_key"


@contextmanager
def _appWithStubbedWorkers():
    """A real SpotifyDashboardApp with every worker-start seam mocked but NOT
    called for it - unlike _app_factory.makeApp, which patches the thread
    starters out entirely and so can't observe whether __init__ invoked them.

    Yields (dashboard, seams) where seams maps a readable name to the mock.
    The patches stay active for the whole block so startWorkers() is observable
    too."""
    with patch(_SECRET_KEY_PATCH, return_value="test-secret-key"), \
         patch("app.migrateIfNeeded"), \
         patch("app.Path.exists", return_value=False), \
         patch("app.BackupWorker.start") as backupStart, \
         patch("app.EMAIL_WORKER") as emailWorker, \
         patch("app.SpotifyDashboardApp.startVersionCheck_thread") as versionStart, \
         patch("app.SpotifyDashboardApp.checkLogin_thread") as loginStart:
        dashboard = SpotifyDashboardApp()
        yield dashboard, {
            "backup": backupStart,
            "emailBind": emailWorker.bind_repo,
            "emailStart": emailWorker.start,
            "version": versionStart,
            "login": loginStart,
        }


class TestConstructionStartsNoWorkers(unittest.TestCase):
    def test_backup_worker_uses_environment_fallbacks_on_a_fresh_install(self):
        with patch.dict(os.environ, {
            "BACKUP_INTERVAL_HOURS": "12",
            "BACKUP_RETENTION_COUNT": "30",
        }), _appWithStubbedWorkers() as (dashboard, _seams):
            self.assertEqual(dashboard.backupWorker.intervalHours, 12)
            self.assertEqual(dashboard.backupWorker.retentionCount, 30)

    def test_construction_starts_no_worker(self):
        with _appWithStubbedWorkers() as (_dashboard, seams):
            for name, seam in seams.items():
                self.assertEqual(seam.call_count, 0,
                                 f"{name} was started during __init__")

    def test_construction_still_builds_the_backup_worker(self):
        """Only .start() is deferred - the worker itself is still constructed
        eagerly, because it reads its interval/retention from admin settings
        and /admin's Worker Health panel reads dashboard.backupWorker."""
        with _appWithStubbedWorkers() as (dashboard, _seams):
            self.assertIsNotNone(dashboard.backupWorker)

    def test_construction_does_not_rebind_the_email_worker_singleton(self):
        """EMAIL_WORKER is process-global; binding it at construction time made
        every app ever built in a test session fight over its repo."""
        with _appWithStubbedWorkers() as (_dashboard, seams):
            seams["emailBind"].assert_not_called()

    def test_routes_are_still_registered_by_construction(self):
        """Route registration is NOT a worker - a constructed app must be
        request-ready without startWorkers()."""
        with _appWithStubbedWorkers() as (dashboard, _seams):
            self.assertIn("/login", {r.rule for r in dashboard.app.url_map.iter_rules()})


class TestStartWorkers(unittest.TestCase):
    def test_start_workers_starts_every_worker(self):
        with _appWithStubbedWorkers() as (dashboard, seams):
            dashboard.startWorkers()

            for name, seam in seams.items():
                self.assertEqual(seam.call_count, 1, f"{name} was not started")

    def test_start_workers_binds_the_repo_before_starting_the_email_worker(self):
        """A started EmailWorker polls immediately, so an unbound repo would
        make its first jobs open throwaway connections."""
        with _appWithStubbedWorkers() as (dashboard, seams):
            dashboard.startWorkers()

            seams["emailBind"].assert_called_once_with(dashboard.repo)

    def test_start_workers_is_idempotent(self):
        """wsgi.py and run() both call it; a double call must not spawn a
        second login-check loop (neither checkLogin_thread nor
        startVersionCheck_thread guards against that on its own)."""
        with _appWithStubbedWorkers() as (dashboard, seams):
            dashboard.startWorkers()
            dashboard.startWorkers()

            for name, seam in seams.items():
                self.assertEqual(seam.call_count, 1, f"{name} was started twice")


class TestShutdownWithoutStart(unittest.TestCase):
    def test_shutdown_is_safe_when_workers_never_started(self):
        """Every test that builds an app and never starts it still ends up
        calling shutdown() from a tearDown."""
        with _appWithStubbedWorkers() as (dashboard, _seams):
            dashboard.user_databases = {}

            dashboard.shutdown()  #< must not raise

            self.assertTrue(dashboard._stop_event.is_set())

    def test_shutdown_stops_started_workers(self):
        with _appWithStubbedWorkers() as (dashboard, _seams):
            dashboard.startWorkers()
            dashboard.backupWorker = MagicMock()
            dashboard.user_databases = {}

            dashboard.shutdown()

            dashboard.backupWorker.stop.assert_called_once()


class TestShutdownStopsTheSharedThreadPools(unittest.TestCase):
    """The three process-wide ThreadPoolExecutors (image download, artist bio,
    album bio) were started by Database and shut down by nobody.

    Nothing cancels them, so CPython's own concurrent.futures atexit hook is
    what eventually stops them - and it puts its sentinel BEHIND the queued
    work and then joins every worker with NO timeout. The whole backlog
    therefore runs after shutdown() has returned and reported the app stopped,
    outside the grace period tests/test_compose_shutdown_budget.py sizes, still
    issuing Last.fm and CDN requests. Under Docker that window ends in SIGKILL
    mid-teardown."""

    def _shutdownWithMockedPools(self):
        """The mocks go in AFTER construction: SpotifyDashboardApp.__init__
        calls Database.configureWorkerPools, which rebuilds all three from the
        admin settings and would discard anything installed earlier."""
        from Database.database import Database
        mocks = (MagicMock(), MagicMock(), MagicMock())
        for mock in mocks:
            # A real int: shutdownWorkerPools sizes each replacement from the
            # pool it retires, and ThreadPoolExecutor(max_workers=<MagicMock>)
            # raises ValueError - which app.py's guard would swallow, leaving
            # the remaining pools untouched and this test quietly half-blind.
            mock._max_workers = 4
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor)
            (Database._imageDownloadExecutor,
             Database._artistBioFetchExecutor,
             Database._albumBioFetchExecutor) = mocks
            try:
                dashboard.user_databases = {}
                dashboard.shutdown()
            finally:
                (Database._imageDownloadExecutor,
                 Database._artistBioFetchExecutor,
                 Database._albumBioFetchExecutor) = originals
        return mocks

    def test_every_shared_pool_is_shut_down(self):
        for pool in self._shutdownWithMockedPools():
            with self.subTest(pool=pool):
                pool.shutdown.assert_called_once()

    def test_the_pools_are_not_waited_on_and_the_backlog_is_dropped(self):
        """wait=False because shutdown() is already inside a bounded budget,
        and cancel_futures=True because the queued work is best-effort media
        the next page view re-triggers - waiting on it is what the atexit hook
        already does badly."""
        for pool in self._shutdownWithMockedPools():
            with self.subTest(pool=pool):
                self.assertEqual(pool.shutdown.call_args.kwargs,
                                 {"wait": False, "cancel_futures": True})

    def test_the_pools_are_usable_again_afterwards(self):
        """One process outlives many app instances - AppTestCase registers
        shutdown() as a cleanup for every route test - and a stopped
        ThreadPoolExecutor raises on every later submit(). Leaving them shut
        would fail whichever unrelated test next rendered a page that lazily
        fetches an image or a bio."""
        from Database.database import Database
        with _appWithStubbedWorkers() as (dashboard, _seams):
            dashboard.user_databases = {}
            dashboard.shutdown()

            for pool in (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor):
                with self.subTest(pool=pool):
                    self.assertEqual(pool.submit(lambda: "alive").result(timeout=5), "alive")

    def test_the_replacement_pools_keep_the_configured_size(self):
        """configureWorkerPools sizes them from admin settings at startup; a
        replacement that silently reverted to the code default would quietly
        undo that for the rest of the process.

        The sizes MUST differ from the code defaults (5/2/2), or the assertion
        cannot tell the two implementations apart: a fresh app against an empty
        settings table is already sized at the defaults, so a replacement built
        from the constants would compare equal to one built from the retired
        pool. The first version of this test had exactly that hole."""
        import concurrent.futures
        from Database.database import Database
        distinctive = (7, 9, 11)   #< none of them 5/2/2
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor)
            (Database._imageDownloadExecutor,
             Database._artistBioFetchExecutor,
             Database._albumBioFetchExecutor) = (
                concurrent.futures.ThreadPoolExecutor(max_workers=size) for size in distinctive)
            try:
                dashboard.user_databases = {}
                dashboard.shutdown()

                sizesAfter = tuple(p._max_workers for p in (Database._imageDownloadExecutor,
                                                            Database._artistBioFetchExecutor,
                                                            Database._albumBioFetchExecutor))
            finally:
                (Database._imageDownloadExecutor,
                 Database._artistBioFetchExecutor,
                 Database._albumBioFetchExecutor) = originals

        self.assertEqual(sizesAfter, distinctive)

    def test_the_pools_are_retired_even_if_stopping_a_user_raises(self):
        """The move to the end of shutdown() put the call behind
        _stopDatabasesConcurrently, which is the one statement here with no
        guard of its own - it starts a thread per user and joins them. If that
        raises (a second Ctrl+C landing in the join, a process that cannot
        start another thread), the pools are never retired and the whole
        backlog goes back to running at interpreter exit."""
        from Database.database import Database
        mocks = (MagicMock(), MagicMock(), MagicMock())
        for mock in mocks:
            mock._max_workers = 4
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor)
            (Database._imageDownloadExecutor,
             Database._artistBioFetchExecutor,
             Database._albumBioFetchExecutor) = mocks
            try:
                dashboard.user_databases = {"timo": MagicMock()}
                with patch.object(dashboard, '_stopDatabasesConcurrently',
                                  side_effect=RuntimeError("can't start new thread")):
                    with self.assertRaises(RuntimeError):
                        dashboard.shutdown()

                for pool in mocks:
                    with self.subTest(pool=pool):
                        pool.shutdown.assert_called_once()
            finally:
                (Database._imageDownloadExecutor,
                 Database._artistBioFetchExecutor,
                 Database._albumBioFetchExecutor) = originals

    def test_the_pools_are_retired_after_the_threads_that_feed_them_are_stopped(self):
        """Order is the whole point. Every per-user thread that submits media
        work is alive until _stopDatabasesConcurrently returns (bounded by
        USER_STOP_JOIN_TIMEOUT_SECONDS = 30s), and shutdownWorkerPools installs
        a live REPLACEMENT pool. Retiring the pools first therefore hands that
        whole 30s window a fresh pool nothing will ever stop again - the
        listener's appendTrackData -> saveImagesFromTrack -> submit path queues
        a CDN download onto it, and the only thing left to stop that is the
        interpreter's atexit hook, i.e. exactly what this fix removes."""
        from Database.database import Database
        order = []
        mocks = (MagicMock(), MagicMock(), MagicMock())
        for index, mock in enumerate(mocks):
            mock._max_workers = 4
            mock.shutdown.side_effect = lambda *a, i=index, **k: order.append(f"pool{i}")
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor)
            (Database._imageDownloadExecutor,
             Database._artistBioFetchExecutor,
             Database._albumBioFetchExecutor) = mocks
            try:
                db = MagicMock()
                db.stop.side_effect = lambda *a, **k: order.append("userStop")
                dashboard.user_databases = {"timo": db}

                dashboard.shutdown()
            finally:
                (Database._imageDownloadExecutor,
                 Database._artistBioFetchExecutor,
                 Database._albumBioFetchExecutor) = originals

        self.assertIn("userStop", order)
        self.assertLess(order.index("userStop"), order.index("pool0"),
                        "the pools must be retired only once nothing can still submit to them")

    def test_a_failing_pool_does_not_abort_the_rest_of_shutdown(self):
        """Same rule the backup/email workers already follow: one member
        raising must not leave the per-user databases unsignalled."""
        from Database.database import Database
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor, Database._artistBioFetchExecutor)
            failing = MagicMock()
            # A real int, or ThreadPoolExecutor(max_workers=<MagicMock>) raises
            # TypeError BEFORE pool.shutdown() is ever reached and the arm under
            # test is "building the replacement raised" rather than the one the
            # side_effect below configures. That is what this test did until the
            # replacement was moved ahead of the retirement.
            failing._max_workers = 4
            failing.shutdown.side_effect = RuntimeError("boom")
            survivor = MagicMock()
            survivor._max_workers = 4
            Database._imageDownloadExecutor = failing
            Database._artistBioFetchExecutor = survivor
            try:
                dashboard.user_databases = {}

                dashboard.shutdown()  #< must not raise

                failing.shutdown.assert_called_once()    #< the configured failure really fired
                survivor.shutdown.assert_called_once()   #< and the REST of the pools still went
                self.assertIsNot(Database._imageDownloadExecutor, failing,
                                 "even a pool that failed to stop must not be left installed")
            finally:
                (Database._imageDownloadExecutor, Database._artistBioFetchExecutor) = originals


class TestEverySharedPoolIsRegisteredForRetirement(unittest.TestCase):
    """shutdownWorkerPools can only retire the pools it knows by name, and the
    names used to be spelled inline in three places - the class attributes,
    configureWorkerPools and shutdownWorkerPools itself. A fourth pool added by
    copying the visible pattern would be configured at startup and silently
    never retired: its backlog handed back to the untimed atexit join this
    whole mechanism exists to remove, with no test failing. Same registry-and-
    pin shape as WORKER_STOP_EVENT_NAMES, extracted after signalStop silently
    missed the fifth stop event."""

    def test_every_pool_class_attribute_is_in_the_registry(self):
        import concurrent.futures
        from Database.database import Database

        pools = sorted(name for name, value in vars(Database).items()
                       if isinstance(value, concurrent.futures.ThreadPoolExecutor))

        self.assertEqual(pools, sorted(Database.MEDIA_POOL_ATTRIBUTE_NAMES),
                         "a pool the registry does not name is a pool shutdown never stops")


if __name__ == "__main__":
    unittest.main()


class TestShutdownJoinBudgetReporting(unittest.TestCase):
    """Phase 2's join budget is SHARED across users, not per user, so the second
    and later users can be joined with almost none of it left - that is the
    whole point of the shared deadline. The warning named the CONSTANT
    regardless, so a user actually given 0s was reported as "did not stop within
    30s". Read during a shutdown hang, that says every user got the full budget
    and none of them used it, which is the opposite of what happened."""

    def test_the_warning_names_the_wait_it_actually_gave(self):
        with _appWithStubbedWorkers() as (dashboard, _seams):
            release = threading.Event()
            slow = MagicMock()
            slow.user = "slowuser"
            slow.stop.side_effect = lambda *a, **k: release.wait(5)
            quick = MagicMock()
            quick.user = "quickuser"

            # A scripted clock, not a real one: what is under test is the SECOND
            # join landing after the shared deadline is spent, and reaching that
            # honestly would put USER_STOP_JOIN_TIMEOUT_SECONDS of real waiting
            # into the suite. Three reads - the deadline, then one per user.
            ticks = iter([0.0, 0.0, 30.0])

            def clock():
                try:
                    return next(ticks)
                except StopIteration:
                    return 30.0

            try:
                with patch("app.time.monotonic", side_effect=clock),                      patch("app.USER_STOP_JOIN_TIMEOUT_SECONDS", 30),                      self.assertLogs("app", level="WARNING") as logs:
                    dashboard._stopDatabasesConcurrently([quick, slow])
            finally:
                release.set()   #< or the stopper thread outlives the test

        warning = str(logs.output)   #< the list repr carries every record
        self.assertIn("slowuser", warning)
        self.assertNotIn("quickuser", warning,
                         "a user that stopped inside its allowance must not be reported")
        self.assertIn("0.0s", warning,
                      "the warning still claims a wait it never gave")
        self.assertIn("budget", warning,
                      "the line has to say the shared budget was spent, or 0s reads as a bug")
