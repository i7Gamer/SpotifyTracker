"""UserRegistryMixin stands alone, without Flask or SpotifyDashboardApp.

The per-user Database registry and login cache were extracted from app.py,
where they sat between Flask setup, worker loops and route registration. The
behaviour is covered in depth by the route/session suites that drive it through
a real app (test_read_only_user_db, test_multi_user, test_startup_relogin, ...);
what those can NOT show is that the extraction actually separated anything.

So this file composes the mixin onto a bare host with nothing but a repo and a
stop event. If a Flask import, a request context, or some other app-level
coupling creeps back into the registry, these fail while the app-level suites
keep passing.
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dashboard.user_registry import UserRegistryMixin
from Database.repository import Repository

_DATABASE_PATCH = "dashboard.user_registry.Database"


class _BareHost(UserRegistryMixin):
    """The entire contract the mixin expects of its host: a repo, a stop event,
    and _initUserRegistry() having been called. No Flask, no app."""

    def __init__(self, repo=None):
        self.repo = repo if repo is not None else MagicMock()
        self._stop_event = threading.Event()
        self._initUserRegistry()


class TestRegistryNeedsNoApp(unittest.TestCase):
    def test_init_sets_up_registry_state(self):
        host = _BareHost()

        self.assertEqual(host.user_databases, {})
        self.assertEqual(host._activatedUsers, set())
        self.assertEqual(host._login_cache, {})

    def test_get_user_db_constructs_activates_and_caches(self):
        host = _BareHost()

        with patch(_DATABASE_PATCH, side_effect=lambda *a, **k: MagicMock()) as MockDatabase:
            db = host.get_user_db("alice", "alice@example.com")
            again = host.get_user_db("alice", "alice@example.com")

        self.assertIs(db, again)
        self.assertEqual(MockDatabase.call_count, 1, "second call must reuse the cached instance")
        db.startListener.assert_called_once_with(email="alice@example.com")
        self.assertIn("alice", host._activatedUsers)

    def test_failed_activation_rolls_back_both_caches(self):
        """A half-activated instance must not stay reachable - otherwise each
        retry stacks another full set of per-user threads."""
        host = _BareHost()
        broken = MagicMock()
        broken.startListener.side_effect = RuntimeError("spotify said no")

        with patch(_DATABASE_PATCH, return_value=broken):
            with self.assertRaises(RuntimeError):
                host.get_user_db("alice", "alice@example.com")

        broken.stop.assert_called_once()
        self.assertNotIn("alice", host.user_databases)
        self.assertNotIn("alice", host._activatedUsers)

    def test_read_only_db_is_cached_without_being_activated(self):
        """The share-link path: an anonymous GET must never start a listener,
        but the instance is still cached so the owner's next real login
        activates it in place."""
        host = _BareHost()

        with patch(_DATABASE_PATCH, side_effect=lambda *a, **k: MagicMock()):
            db = host._getReadOnlyUserDb("alice")

        db.startListener.assert_not_called()
        self.assertIs(host.user_databases["alice"], db)
        self.assertNotIn("alice", host._activatedUsers)

    def test_login_result_is_cached_per_email(self):
        repo = MagicMock()
        repo.getUsernameForEmail.return_value = "alice"
        repo.getUserCookies.return_value = {"sp_dc": "x"}
        host = _BareHost(repo)

        with patch(_DATABASE_PATCH, side_effect=lambda *a, **k: MagicMock()):
            first = host.is_user_logged_in("alice@example.com")
            host.user_databases["alice"].isListenerLoggedIn.reset_mock()
            second = host.is_user_logged_in("alice@example.com")

            self.assertEqual(first, second)
            host.user_databases["alice"].isListenerLoggedIn.assert_not_called()

    def test_invalidation_fences_a_check_already_in_flight(self):
        """is_user_logged_in evaluates isListenerLoggedIn() outside any lock, so
        a check that started against the OLD listener must not write its stale
        answer over a successful re-login."""
        repo = MagicMock()
        repo.getUsernameForEmail.return_value = "alice"
        repo.getUserCookies.return_value = {"sp_dc": "x"}
        host = _BareHost(repo)
        email = "alice@example.com"

        staleDb = MagicMock()
        #< invalidate DURING the in-flight check, as a concurrent re-login would
        staleDb.isListenerLoggedIn.side_effect = lambda: host._invalidateLoginCache(email) or False

        with patch(_DATABASE_PATCH, return_value=staleDb):
            self.assertFalse(host.is_user_logged_in(email))

        self.assertNotIn(email, host._login_cache)


class TestLegacyAccountAllocationWarning(unittest.TestCase):
    def _host(self, repo):
        host = _BareHost(repo)
        host._ensureAdminExists = MagicMock()
        return host

    def test_warns_when_suffixing_past_case_insensitive_null_email_username(self):
        repo = MagicMock()
        repo.getUsernameForEmail.return_value = None
        repo.createUserIfNameAvailable.side_effect = [False, True]
        repo.getNullEmailUsernameNoCase.return_value = "Alice"
        host = self._host(repo)

        with patch("dashboard.user_registry.logger.warning") as warning:
            username = host.get_or_create_user("alice@example.com")

        self.assertEqual(username, "alice_1")
        warning.assert_called_once()
        rendered = warning.call_args.args[0] % warning.call_args.args[1:]
        self.assertIn("Alice", rendered)
        self.assertIn("alice_1", rendered)
        self.assertIn("docs/recover-a-legacy-account.md", rendered)
        self.assertNotIn("alice@example.com", rendered)

    def test_different_email_username_collision_does_not_warn(self):
        repo = MagicMock()
        repo.getUsernameForEmail.return_value = None
        repo.createUserIfNameAvailable.side_effect = [False, True]
        repo.getNullEmailUsernameNoCase.return_value = None
        host = self._host(repo)

        with patch("dashboard.user_registry.logger.warning") as warning:
            username = host.get_or_create_user("alice@example.com")

        self.assertEqual(username, "alice_1")
        warning.assert_not_called()

    def test_existing_email_login_does_not_warn(self):
        repo = MagicMock()
        repo.getUsernameForEmail.return_value = "Alice"
        host = self._host(repo)

        with patch("dashboard.user_registry.logger.warning") as warning:
            username = host.get_or_create_user("alice@example.com")

        self.assertEqual(username, "Alice")
        repo.createUserIfNameAvailable.assert_not_called()
        repo.getNullEmailUsernameNoCase.assert_not_called()
        warning.assert_not_called()

    def test_failed_diagnostic_lookup_does_not_fail_successful_allocation(self):
        repo = MagicMock()
        repo.getUsernameForEmail.return_value = None
        repo.createUserIfNameAvailable.side_effect = [False, True]
        repo.getNullEmailUsernameNoCase.side_effect = RuntimeError("database read failed")
        host = self._host(repo)

        with patch("dashboard.user_registry.logger.warning") as warning, \
             patch("dashboard.user_registry.logger.exception") as diagnosticError:
            username = host.get_or_create_user("alice@example.com")

        self.assertEqual(username, "alice_1")
        warning.assert_not_called()
        diagnosticError.assert_called_once()


class TestLegacyUsernameLookup(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()

    def test_matches_ascii_case_and_only_null_email_rows(self):
        self.repo.upsertUser("Alice", None)
        self.repo.upsertUser("Bob", "owner@example.com")

        self.assertEqual(self.repo.getNullEmailUsernameNoCase("alice"), "Alice")
        self.assertIsNone(self.repo.getNullEmailUsernameNoCase("bob"))

    def test_uses_sqlite_nocase_instead_of_python_unicode_casefold(self):
        self.repo.upsertUser("Straße", None)

        self.assertIsNone(self.repo.getNullEmailUsernameNoCase("STRASSE"))


class TestRegistryDoesNotImportFlask(unittest.TestCase):
    def test_module_has_no_flask_dependency(self):
        """The registry is request-agnostic on purpose: the Flask-coupled
        helpers (unauthenticatedResponse, get_current_user_or_redirect) stayed
        in app.py."""
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "dashboard" / "user_registry.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertNotIn("flask", imported)
        self.assertNotIn("app", imported)


if __name__ == "__main__":
    unittest.main()
