# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""API insert guards run atomically; live listener writes bypass them."""

import os
from unittest.mock import ANY, MagicMock, patch

from conftest import DatabaseTestCase, rawSpotifyTrackForTest
from Database.database import Database


PLAYED_AT = 1_700_000_000
DURATION_MS = 180_000
MILLISECONDS_PER_SECOND = 1_000


class TestAppendTrackDataDedupGuard(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [], username="alice")
        self.db.saveImagesFromTrack = MagicMock()
        self.db.updatePlaylists = MagicMock()
        self.track = rawSpotifyTrackForTest("t1", name="Song One")
        self.track["duration_ms"] = DURATION_MS

    def _append(self, *, source="web_api_backfill", timestamp=PLAYED_AT):
        return self.db.appendTrackData(timestamp, self.track,
                                       self.track.get("duration_ms") or 0, source=source)

    def test_backfill_with_confirmed_play_is_skipped(self):
        self.assertTrue(self._append(source="listener"))
        self.assertFalse(self._append())
        self.assertEqual(self.db.repo.connection().execute("SELECT COUNT(*) FROM plays").fetchone()[0], 1)

    def test_backfill_with_no_confirmed_play_is_inserted(self):
        self.assertTrue(self._append())
        row = self.db.repo.connection().execute("SELECT played_at, created_reason FROM plays").fetchone()
        self.assertEqual(tuple(row), (PLAYED_AT, "web_api_backfill_play (user: alice)"))

    def test_live_listener_repeats_bypass_the_api_guard(self):
        self.assertTrue(self._append(source="listener"))
        with patch.object(self.db.repo, "findMatchingBackfillPlay") as guard:
            self.assertTrue(self._append(source="listener", timestamp=PLAYED_AT + 1))
        guard.assert_not_called()
        self.assertEqual(self.db.repo.connection().execute("SELECT COUNT(*) FROM plays").fetchone()[0], 2)

    def test_backfill_guard_retains_duration_plus_margin_for_legacy_rows(self):
        with patch.object(self.db.repo, "findMatchingBackfillPlay", return_value=None) as guard:
            self.assertTrue(self._append())
        guard.assert_called_once_with(
            "alice", "t1", PLAYED_AT,
            DURATION_MS // MILLISECONDS_PER_SECOND + Database.BACKFILL_INSERT_GUARD_EXTRA_SECONDS,
            skipToleranceSeconds=Database.BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS,
            page=ANY)

    def test_backfill_guard_handles_missing_duration(self):
        self.track.pop("duration_ms")
        with patch.object(self.db.repo, "findMatchingBackfillPlay", return_value=None) as guard:
            self.assertTrue(self._append())
        guard.assert_called_once_with(
            "alice", "t1", PLAYED_AT, Database.BACKFILL_INSERT_GUARD_EXTRA_SECONDS,
            skipToleranceSeconds=Database.BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS,
            page=ANY)

    def test_backfill_skipped_logs_when_debug_enabled(self):
        self.assertTrue(self._append(source="listener"))
        for value in ("1", "true"):
            with self.subTest(debug=value), patch.dict(os.environ, {"FLASK_DEBUG": value}), \
                    patch("Database.database.logger") as logger:
                self.assertFalse(self._append())
                messages = [call.args[0] for call in logger.info.call_args_list]
                self.assertTrue(any("Skipping backfilled play" in message for message in messages))
                self.assertFalse(any("Recording play" in message for message in messages))

    def test_backfill_skipped_does_not_log_when_debug_disabled(self):
        self.assertTrue(self._append(source="listener"))
        for value in ("0", None):
            with self.subTest(debug=value), patch.dict(os.environ), patch("Database.database.logger") as logger:
                if value is None:
                    os.environ.pop("FLASK_DEBUG", None)
                else:
                    os.environ["FLASK_DEBUG"] = value
                self.assertFalse(self._append())
                messages = [call.args[0] for call in logger.info.call_args_list]
                self.assertFalse(any("Skipping backfilled play" in message for message in messages))
