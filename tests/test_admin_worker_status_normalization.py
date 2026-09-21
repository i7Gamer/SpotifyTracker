"""Worker status display preserves fallbacks without starting user workers."""
import unittest
from unittest.mock import patch

from test_admin_route import AdminRouteTestBase

WORKER_FIELDS = ("spotify_api_worker", "genre_worker", "artist_bio_worker", "album_bio_worker", "wrapped_worker")
FAILURES = 3
FAILURE_RATE = 0.75


class TestPeriodicWorkerStatus(unittest.TestCase):
    def test_missing_and_malformed_status_use_the_configuration_fallback(self):
        from routes.admin import _periodicWorkerStatus
        for configured in (False, True):
            for status in (None, [], "unavailable"):
                with self.subTest(configured=configured, status=status):
                    normalized = _periodicWorkerStatus(status, configured)
                    self.assertEqual(normalized, {
                        "configured": configured, "running": False,
                        "consecutive_failures": 0, "failure_rate": 0.0, "last_error": None,
                    })

    def test_partial_dict_uses_its_own_configuration_and_retains_telemetry(self):
        from routes.admin import _periodicWorkerStatus
        source = {"running": "yes", "consecutive_failures": FAILURES,
                  "failure_rate": FAILURE_RATE, "last_error": "unavailable"}
        before = source.copy()
        normalized = _periodicWorkerStatus(source, True)
        self.assertFalse(normalized["configured"])
        self.assertTrue(normalized["running"])
        self.assertEqual(normalized["consecutive_failures"], FAILURES)
        self.assertEqual(normalized["failure_rate"], FAILURE_RATE)
        self.assertEqual(normalized["last_error"], "unavailable")
        self.assertEqual(source, before)
        self.assertIsNot(source, normalized)


class TestWorkerStatusCollection(AdminRouteTestBase):
    def _rows(self, dash, patches):
        with patch("routes.admin.render_template", return_value="ok") as render:
            self.assertEqual(self._getAdmin(dash, patches=patches).status_code, 200)
        return render.call_args.kwargs["users_list"]

    def test_failing_accessor_does_not_hide_sibling_statuses(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getSpotifyApiWorkerStatus.side_effect = RuntimeError("unavailable")
        db.getLastfmWorkerStatus.return_value = {
            "configured": True, "running": True, "consecutive_failures": FAILURES,
            "failure_rate": FAILURE_RATE, "last_error": "last failure",
        }
        db.getWrappedWorkerStatus.return_value = {"configured": True, "running": True}
        patches = self._patches(dash, True, users=self._MOCK_USERS[:1], userDb=db)
        with self.assertLogs("routes.admin", level="WARNING") as logs:
            row = self._rows(dash, patches)[0]
        self.assertIn("Spotify API worker status lookup failed for alice", logs.output[0])
        self.assertFalse(row["spotify_api_worker"]["running"])
        self.assertTrue(row["spotify_api_worker"]["configured"])
        self.assertEqual(row["genre_worker"]["failure_rate"], FAILURE_RATE)
        self.assertTrue(row["wrapped_worker"]["running"])
        self.assertEqual(set(row["auto_importer_worker"]), {"configured", "running"})

    def test_inactive_accounts_do_not_get_activated(self):
        dash = self._makeApp()
        patches = self._patches(dash, True)
        dash.user_databases.clear()
        with patch.object(dash, "get_user_db", return_value=self._makeDb()) as authLookup:
            # Replace the helper's auth lookup patch, retaining one observable call.
            patches = [p for p in patches if p.attribute != "get_user_db"]
            rows = self._rows(dash, patches)
        authLookup.assert_called_once()
        self.assertEqual(dash.user_databases, {})
        for row in rows:
            self.assertTrue(all(not row[field]["running"] for field in WORKER_FIELDS))

    def test_missing_accessors_and_disabled_lastfm_use_fallbacks(self):
        dash = self._makeApp()
        db = self._makeDb()
        del db.getSpotifyApiWorkerStatus
        db.getWrappedWorkerStatus.return_value = None
        user = dict(self._MOCK_USERS[0], lastfm_api_key=None)
        row = self._rows(dash, self._patches(dash, True, users=[user], userDb=db))[0]
        self.assertTrue(row["spotify_api_worker"]["configured"])
        self.assertTrue(row["wrapped_worker"]["configured"])
        for accessor in (db.getLastfmWorkerStatus, db.getLastfmBiographyWorkerStatus,
                         db.getLastfmAlbumBiographyWorkerStatus):
            accessor.assert_not_called()
        self.assertFalse(row["genre_worker"]["configured"])
