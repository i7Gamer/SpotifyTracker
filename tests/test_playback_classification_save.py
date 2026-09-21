"""A classification Save is one durable settings/flags/cache boundary."""
import datetime
import json
import sqlite3
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from conftest import DatabaseTestCase, RecordingConnection, normalizeTrackForTest
from _app_factory import makeApp
from test_admin_route import AdminRouteTestBase
from Database.repository import (
    COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_MIN,
    COMPLETION_COMPLETE_PERCENT_MAX, SKIP_MODE_SECONDS, SKIP_MODE_PERCENT,
    SKIP_SECONDS_MAX, SKIP_PERCENT_MAX, SKIP_THRESHOLD_MODE_KEY,
    SKIP_THRESHOLD_VALUE_KEY, WRAPPED_INVALIDATION_GENERATION_KEY,
)

YEAR = 2025
PAST_YEAR = YEAR - 1
STAMP = datetime.datetime(YEAR, 6, 15, tzinfo=datetime.timezone.utc).timestamp()
SHORT_MS = 10_000
LONG_MS = 100_000
SHORT_PLAY_MS = 3_000
MIDDLE_PLAY_MS = 6_000
LONG_PLAY_MS = 90_000
OLD_TOTAL_MS = MIDDLE_PLAY_MS + LONG_PLAY_MS
NEW_TOTAL_MS = SHORT_PLAY_MS + LONG_PLAY_MS
INITIAL_SECONDS = 5
REPAIR_SECONDS = 30
NEW_PERCENT = 10
INITIAL_COMPLETION = 80
NEW_COMPLETION = 70
TRACK_COUNT = 3
SNAPSHOT_TABLES = ("app_settings", "plays", "user_wrapped", "user_milestones", "users")
SAVE_ERROR = "Could not save playback classification settings. Please try again."


class ClassificationSaveTestCase(DatabaseTestCase):
    def _seed(self, latestMiddle=False):
        tracks = {
            "short": {"id": "short", "name": "Short", "duration": SHORT_MS, "artists": []},
            "middle": {"id": "middle", "name": "Middle", "duration": LONG_MS, "artists": []},
            "long": {"id": "long", "name": "Long", "duration": LONG_MS, "artists": []},
        }
        order = ("short", "long", "middle") if latestMiddle else ("short", "middle", "long")
        played = {"short": SHORT_PLAY_MS, "middle": MIDDLE_PLAY_MS, "long": LONG_PLAY_MS}
        db = self._makeDb(tracks, [
            {"id": track, "playedAt": STAMP + offset, "timePlayed": played[track]}
            for offset, track in enumerate(order)
        ])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, INITIAL_SECONDS)
        db.repo.setAppSetting(COMPLETION_COMPLETE_PERCENT_KEY, str(INITIAL_COMPLETION))
        db.repo.setAppSetting(WRAPPED_INVALIDATION_GENERATION_KEY, "0")
        db.repo.recomputeSkipFlags()
        db.repo.setMilestoneBaselineAt(db.user, STAMP)
        db.repo.recordMilestone(db.user, "plays", TRACK_COUNT, None, STAMP, seen=True)
        db.recalculateWrappedForYear(YEAR)
        return db

    def _observer(self, repo):
        conn = sqlite3.connect(repo.connectionManager.dbPath)
        self.addCleanup(conn.close)
        return conn

    def _snapshot(self, conn):
        return {table: list(conn.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in SNAPSHOT_TABLES}

    def test_every_write_failure_rolls_back_the_whole_save(self):
        failures = (
            ("app_settings", "UPDATE", f"WHEN NEW.key='{SKIP_THRESHOLD_MODE_KEY}'"),
            ("app_settings", "UPDATE", f"WHEN NEW.key='{SKIP_THRESHOLD_VALUE_KEY}'"),
            ("app_settings", "UPDATE", f"WHEN NEW.key='{COMPLETION_COMPLETE_PERCENT_KEY}'"),
            ("plays", "UPDATE", ""),
            ("app_settings", "UPDATE", f"WHEN NEW.key='{WRAPPED_INVALIDATION_GENERATION_KEY}'"),
            ("user_wrapped", "DELETE", ""),
        )
        for table, operation, condition in failures:
            with self.subTest(table=table, condition=condition):
                db = self._seed()
                observer = self._observer(db.repo)
                before = self._snapshot(observer)
                db.repo._conn().execute(
                    f"CREATE TEMP TRIGGER fail_save BEFORE {operation} ON {table} {condition} "
                    "BEGIN SELECT RAISE(ABORT, 'injected save failure'); END")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT, NEW_COMPLETION)
                self.assertEqual(self._snapshot(observer), before)
                self.assertFalse(db.repo._conn().in_transaction)

    def test_joined_caller_writes_commit_or_roll_back_with_save(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                db = self._seed()
                observer = self._observer(db.repo)
                before = self._snapshot(observer)
                db.repo._conn().execute("INSERT INTO app_settings VALUES ('caller_staged', 'yes')")
                if fail:
                    with patch.object(db.repo, "_deleteAllWrapped", side_effect=sqlite3.OperationalError("fail")):
                        with self.assertRaises(sqlite3.OperationalError):
                            db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT)
                    self.assertEqual(self._snapshot(observer), before)
                else:
                    db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT)
                    self.assertEqual(observer.execute(
                        "SELECT value FROM app_settings WHERE key='caller_staged'").fetchone(), ("yes",))
                    self.assertEqual(observer.execute("SELECT COUNT(*) FROM user_wrapped").fetchone(), (0,))
                self.assertFalse(db.repo._conn().in_transaction)

    def test_count_neutral_changes_rebuild_even_when_latest_timestamp_decreases(self):
        for latestMiddle in (False, True):
            with self.subTest(latestMiddle=latestMiddle):
                db = self._seed(latestMiddle)
                before = db.repo.getCachedWrapped(db.user, YEAR)
                self.assertEqual(before["total_ms"], OLD_TOTAL_MS)
                processed = db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT)
                self.assertEqual(processed, TRACK_COUNT)
                self.assertIsNone(db.repo.getCachedWrapped(db.user, YEAR))
                self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)
                db.recalculateWrappedForYear(YEAR)
                after = db.repo.getCachedWrapped(db.user, YEAR)
                self.assertEqual(after["total_plays"], before["total_plays"])
                self.assertEqual(after["total_ms"], NEW_TOTAL_MS)
                self.assertLessEqual(after["max_played_at"], before["max_played_at"])
                self.assertEqual({row["id"] for row in json.loads(after["top_songs"])}, {"short", "long"})

    def test_same_settings_repair_flags_and_stale_cache_only(self):
        for driftFlags in (False, True):
            with self.subTest(driftFlags=driftFlags):
                db = self._seed()
                db.milestonesRecalcPending = driftFlags
                with db.repo._conn() as conn:
                    conn.execute("UPDATE user_wrapped SET total_ms=0")
                    if driftFlags:
                        conn.execute("UPDATE plays SET is_skip=0 WHERE track_id='short'")
                before = self._snapshot(self._observer(db.repo))
                db.repo.savePlaybackClassificationSettings(SKIP_MODE_SECONDS, INITIAL_SECONDS)
                self.assertEqual(db.repo._conn().execute(
                    "SELECT is_skip FROM plays WHERE track_id='short'").fetchone()[0], 1)
                self.assertIsNone(db.repo.getCachedWrapped(db.user, YEAR))
                db.recalculateWrappedForYear(YEAR)
                self.assertEqual(db.repo.getCachedWrapped(db.user, YEAR)["total_ms"], OLD_TOTAL_MS)
                after = self._snapshot(self._observer(db.repo))
                for table in ("user_milestones", "users"):
                    self.assertEqual(after[table], before[table])
                self.assertIs(db.milestonesRecalcPending, driftFlags)

    def test_every_save_invalidates_other_users_and_years_and_empty_caches(self):
        db = self._seed()
        db.repo.upsertUser("other", "other@example.com")
        data = db.repo.getCachedWrapped(db.user, YEAR)
        for username, year in ((db.user, PAST_YEAR), ("other", YEAR)):
            db.repo.saveCachedWrapped(username, year, data)
        for expectedGeneration in (1, 2):
            db.repo.savePlaybackClassificationSettings(SKIP_MODE_SECONDS, INITIAL_SECONDS)
            self.assertEqual(db.repo._conn().execute("SELECT COUNT(*) FROM user_wrapped").fetchone()[0], 0)
            self.assertEqual(db.repo.getWrappedInvalidationGeneration(), expectedGeneration)

    def test_in_flight_calculation_cannot_restore_the_old_snapshot(self):
        db = self._seed()
        db.repo.deleteAllWrapped()
        original = db.getTopSongs
        changed = False

        def changeDuringCalculation(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT)
            return original(*args, **kwargs)

        with patch.object(db, "getTopSongs", side_effect=changeDuringCalculation):
            db.recalculateWrappedForYear(YEAR)
        self.assertTrue(changed)
        self.assertIsNone(db.repo.getCachedWrapped(db.user, YEAR))
        db.recalculateWrappedForYear(YEAR)
        self.assertEqual(db.repo.getCachedWrapped(db.user, YEAR)["total_ms"], NEW_TOTAL_MS)

    def test_transaction_starts_before_classification_reads(self):
        db = self._seed()
        statements = []
        proxy = RecordingConnection(db.repo._conn(), statements)
        with patch.object(db.repo, "_conn", return_value=proxy):
            db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT)
        self.assertEqual(statements[0], ("BEGIN IMMEDIATE", False))
        self.assertTrue(any(sql.startswith("SELECT") for sql, _ in statements))
        self.assertTrue(all(inTx for _, inTx in statements[1:]), statements)

    def test_bounds_and_completion_are_effective_during_recompute(self):
        cases = (
            (SKIP_MODE_SECONDS, SKIP_SECONDS_MAX + 1, COMPLETION_COMPLETE_PERCENT_MIN - 1,
             SKIP_SECONDS_MAX, COMPLETION_COMPLETE_PERCENT_MIN),
            (SKIP_MODE_PERCENT, SKIP_PERCENT_MAX + 1, COMPLETION_COMPLETE_PERCENT_MAX + 1,
             SKIP_PERCENT_MAX, COMPLETION_COMPLETE_PERCENT_MAX),
        )
        for mode, value, completion, expectedValue, expectedCompletion in cases:
            with self.subTest(mode=mode):
                db = self._seed()
                db.repo.savePlaybackClassificationSettings(mode, value, completion)
                self.assertEqual(db.repo.getSkipThreshold(), (mode, expectedValue))
                self.assertEqual(db.repo.getCompletionCompletePercent(), expectedCompletion)
                for row in db.repo._conn().execute(
                        "SELECT p.time_played,p.is_skip,t.duration_ms FROM plays p JOIN tracks t ON t.id=p.track_id"):
                    self.assertEqual(row["is_skip"], db.repo.computeIsSkip(row["time_played"], row["duration_ms"]))

    def test_public_wrappers_still_commit_without_invalidating_wrapped(self):
        db = self._seed()
        observer = self._observer(db.repo)
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_SECONDS, REPAIR_SECONDS),
                         (SKIP_MODE_SECONDS, REPAIR_SECONDS))
        self.assertEqual(observer.execute("SELECT value FROM app_settings WHERE key=?",
                                         (SKIP_THRESHOLD_VALUE_KEY,)).fetchone()[0], str(REPAIR_SECONDS))
        self.assertEqual(db.repo.recomputeSkipFlags(), TRACK_COUNT)
        self.assertEqual(observer.execute("SELECT is_skip FROM plays WHERE track_id='middle'").fetchone()[0], 1)
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 0)
        self.assertIsNotNone(db.repo.getCachedWrapped(db.user, YEAR))

    def test_public_share_and_playlist_export_rebuild_from_new_classifications(self):
        db = self._seed()
        dash = makeApp()
        self.addCleanup(dash.shutdown)
        token = db.repo.createShareLink(db.user, db.repo.SHARE_LINK_KIND_WRAPPED, YEAR, None)
        client = dash.app.test_client()
        with patch.object(dash, "repo", db.repo), \
             patch.object(dash, "_getReadOnlyUserDb", return_value=db), \
             patch.object(dash, "get_current_user_or_redirect", return_value=("user@example.com", db.user, db)):
            db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT)
            with patch("routes.wrapped.render_template", return_value="wrapped") as render:
                response = client.get(f"/shared/{token}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual({song["id"] for song in render.call_args.kwargs["topSongs"]}, {"short", "long"})
            self.assertEqual(db.repo.getCachedWrapped(db.user, YEAR)["total_ms"], NEW_TOTAL_MS)
            db.repo.savePlaybackClassificationSettings(SKIP_MODE_PERCENT, NEW_PERCENT)
            response = client.get(f"/playlist/export?year={YEAR}&format=csv")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Short", response.get_data(as_text=True))
            self.assertNotIn("Middle", response.get_data(as_text=True))
            self.assertEqual(db.repo.getCachedWrapped(db.user, YEAR)["total_ms"], NEW_TOTAL_MS)


class TestClassificationSaveRoute(AdminRouteTestBase):
    def test_recompute_failure_redirects_and_leaves_no_partial_save(self):
        dash = self._makeApp()
        repo = dash.repo
        repo.upsertUser("alice", "alice@example.com")
        repo.upsertTrack(normalizeTrackForTest(
            {"id": "middle", "name": "Middle", "duration": LONG_MS, "artists": []}))
        repo.insertPlay("alice", "middle", STAMP, MIDDLE_PLAY_MS)
        repo.setSkipThreshold(SKIP_MODE_SECONDS, INITIAL_SECONDS)
        repo._conn().execute("CREATE TEMP TRIGGER fail_flags BEFORE UPDATE ON plays "
                             "BEGIN SELECT RAISE(ABORT, 'private detail'); END")
        with self.assertLogs("routes.admin", level="ERROR"):
            response = self._post(dash, "/admin/skip_settings", True,
                                  {"skip_mode": SKIP_MODE_SECONDS, "skip_value": str(REPAIR_SECONDS)})
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response.location).query)
        self.assertEqual(query, {"tab": ["settings"], "error": [SAVE_ERROR]})
        with sqlite3.connect(repo.connectionManager.dbPath) as observer:
            self.assertEqual(observer.execute("SELECT value FROM app_settings WHERE key=?",
                                             (SKIP_THRESHOLD_VALUE_KEY,)).fetchone()[0], str(INITIAL_SECONDS))
            self.assertEqual(observer.execute("SELECT is_skip FROM plays").fetchone()[0], 0)

    def test_blank_absent_and_invalid_completion_keep_the_stored_value(self):
        for raw in (None, "", "garbage"):
            with self.subTest(raw=raw):
                dash = self._makeApp()
                dash.repo.setAppSetting(COMPLETION_COMPLETE_PERCENT_KEY, str(NEW_COMPLETION))
                form = {"skip_mode": SKIP_MODE_SECONDS, "skip_value": str(INITIAL_SECONDS)}
                if raw is not None:
                    form["completion_complete_percent"] = raw
                generation = dash.repo.getWrappedInvalidationGeneration()
                self.assertEqual(self._post(dash, "/admin/skip_settings", True, form).status_code, 302)
                self.assertEqual(dash.repo.getCompletionCompletePercent(), NEW_COMPLETION)
                self.assertEqual(dash.repo.getWrappedInvalidationGeneration(), generation + 1)

    def test_operational_errors_are_logged_and_unexpected_errors_propagate(self):
        dash = self._makeApp()
        dash.app.config["TESTING"] = True
        form = {"skip_mode": SKIP_MODE_SECONDS, "skip_value": str(INITIAL_SECONDS)}
        with patch.object(dash.repo, "savePlaybackClassificationSettings", side_effect=sqlite3.OperationalError("busy")):
            with self.assertLogs("routes.admin", level="ERROR"):
                response = self._post(dash, "/admin/skip_settings", True, form)
        self.assertEqual(parse_qs(urlsplit(response.location).query)["error"], [SAVE_ERROR])
        with patch.object(dash.repo, "savePlaybackClassificationSettings", side_effect=RuntimeError("unexpected")):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                self._post(dash, "/admin/skip_settings", True, form)
