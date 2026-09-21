"""Shared factory for a stubbed SpotifyDashboardApp test instance.

~40 test classes each repeated the same 5-decorator ``_makeApp`` that builds a
SpotifyDashboardApp with migrations, the version/login background threads and
secret-key persistence patched out. This centralizes that setup (the background
workers no longer need patching at all - see SpotifyDashboardApp.startWorkers):

- subclass :class:`AppTestCase` to keep calling ``self._makeApp()`` unchanged, or
- call :func:`makeApp` directly (module-level tests, or when extra setup is needed).

Imported by bare module name (``from _app_factory import ...``) like the suite's
other shared helpers, since tests/ is on sys.path with no package __init__.
"""
import unittest
from unittest.mock import patch

from app import SpotifyDashboardApp

# The instance method that persists/reads the Flask session-signing key; stubbed
# so construction never touches secrets/ on disk.
_SECRET_KEY_PATCH = "app.SpotifyDashboardApp._get_or_create_secret_key"


def makeApp():
    """A ready-to-use SpotifyDashboardApp with no filesystem, network or threads.

    Migrations and secret-key persistence are patched out for the duration of
    construction (they still run in ``__init__``); the background workers need
    no patching, since ``startWorkers()`` - which this deliberately never calls
    - is the only thing that starts them. The returned instance is otherwise
    real, with an in-memory/temp Repository (see conftest's DB isolation)."""
    with patch(_SECRET_KEY_PATCH, return_value="test-secret-key"), \
         patch("app.migrateIfNeeded"), \
         patch("app.Path.exists", return_value=False):
        return SpotifyDashboardApp()


class AppTestCase(unittest.TestCase):
    """Base for tests that build a stubbed app via ``self._makeApp()``."""

    def _makeApp(self):
        """The app, with ``shutdown()`` registered as a cleanup.

        Construction starts nothing, but activating a user does:
        ``get_user_db()`` gives that user a live Database, i.e. the five
        periodic workers plus the auto-import watchdog, on real daemon
        threads. Unstopped they outlive the test - waking after their
        randomized startup delay to log into a closed capture stream and poll
        for the rest of the session (see conftest._noLeakedUserThreads).
        Registered here rather than left to each test's tearDown because it is
        needed by every test that activates a user, and harmless (a fast no-op)
        for every test that doesn't."""
        app = makeApp()
        self.addCleanup(app.shutdown)
        return app

    def _loginAs(self, username="alice"):
        """A test client holding a signed-in session for `username`.

        Requires the subclass to have set up ``self.dash`` (the app) and
        ``self.dbs`` (a {username: Database} the stubbed ``get_user_db``
        resolves against) - the shape four route-test files had each written
        out identically.

        Here rather than copied because the SESSION SHAPE is not local
        knowledge. Four files each built the cookie by hand, so every change to
        what a session must carry is four edits that nothing makes you
        remember - and 1.51.0 added exactly such a key (users.session_version,
        via SESSION_VERSION_KEY). These sessions still work without it because
        a missing version reads as 0 by design, which is the good case; the
        next change to the shape may not be so forgiving.

        Deliberately still hand-built rather than going through POST /login:
        these tests stub the Spotify-side login entirely, and a real login
        would drag its cookie verification in with it."""
        patch.object(self.dash, 'is_user_logged_in', return_value=True).start()
        patch.object(self.dash, 'get_username_for_email', return_value=username).start()
        patch.object(self.dash, 'get_user_db', side_effect=lambda u, e: self.dbs[u]).start()
        self.addCleanup(patch.stopall)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = f"{username}@example.com"
            sess['username'] = username
        return client

    def _loginAsWithDb(self, username, email):
        """Return a signed-in client backed by this test case's ``_makeDb``.

        The route suites using this shape need an explicit email and a user
        row before the request, while the regular ``_loginAs`` helper uses the
        ``self.dbs`` mapping instead. Keep those two setup contracts separate
        and stop each patcher independently so one test cannot clear unrelated
        patches installed by its caller.
        """
        self.dash.repo.upsertUser(username, email)
        patchers = (
            patch.object(self.dash, 'is_user_logged_in', return_value=True),
            patch.object(self.dash, 'get_username_for_email', return_value=username),
            patch.object(self.dash, 'get_user_db', return_value=self._makeDb()),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client
