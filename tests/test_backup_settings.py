# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One effective backup-settings policy for every consumer."""

import logging
import unittest

from Database.backup_settings import (
    BACKUP_INTERVAL_ENV_VAR,
    BACKUP_RETENTION_ENV_VAR,
    DEFAULT_BACKUP_INTERVAL_HOURS,
    DEFAULT_BACKUP_RETENTION_COUNT,
    BACKUP_INTERVAL_HOURS_MAX,
    BACKUP_RETENTION_COUNT_MAX,
    resolveBackupSettings,
)
from Database.repository import (
    BACKUP_INTERVAL_HOURS_KEY as REEXPORTED_INTERVAL_KEY,
    BACKUP_INTERVAL_HOURS_MAX as REEXPORTED_INTERVAL_MAX,
    BACKUP_RETENTION_COUNT_KEY as REEXPORTED_RETENTION_KEY,
)


class TestBackupSettingsResolver(unittest.TestCase):
    def test_repository_keeps_the_leaf_constants_as_compatibility_reexports(self):
        self.assertEqual(REEXPORTED_INTERVAL_KEY, "backup_interval_hours")
        self.assertEqual(REEXPORTED_RETENTION_KEY, "backup_retention_count")
        self.assertEqual(REEXPORTED_INTERVAL_MAX, BACKUP_INTERVAL_HOURS_MAX)
        self.assertEqual(BACKUP_RETENTION_COUNT_MAX, 365)

    def test_missing_environment_uses_code_defaults(self):
        settings = resolveBackupSettings(environ={})

        self.assertEqual(settings.intervalHours, DEFAULT_BACKUP_INTERVAL_HOURS)
        self.assertEqual(settings.retentionCount, DEFAULT_BACKUP_RETENTION_COUNT)

    def test_environment_values_are_unbounded_but_saved_values_are_clamped(self):
        environment = {
            BACKUP_INTERVAL_ENV_VAR: "999",
            BACKUP_RETENTION_ENV_VAR: "999",
        }

        environmentOnly = resolveBackupSettings(environ=environment)
        saved = resolveBackupSettings("999", "999", environ=environment)

        self.assertEqual(environmentOnly.intervalHours, 999)
        self.assertEqual(environmentOnly.retentionCount, 999)
        self.assertEqual(saved.intervalHours, BACKUP_INTERVAL_HOURS_MAX)
        self.assertEqual(saved.retentionCount, BACKUP_RETENTION_COUNT_MAX)

    def test_saved_values_win_and_zero_disables_each_field_independently(self):
        settings = resolveBackupSettings(
            "0", "12", environ={
                BACKUP_INTERVAL_ENV_VAR: "24",
                BACKUP_RETENTION_ENV_VAR: "30",
            })

        self.assertEqual(settings.intervalHours, 0)
        self.assertEqual(settings.retentionCount, 12)

    def test_negative_environment_values_have_the_historical_zero_floor(self):
        settings = resolveBackupSettings(environ={
            BACKUP_INTERVAL_ENV_VAR: "-1",
            BACKUP_RETENTION_ENV_VAR: "-30",
        })

        self.assertEqual(settings.intervalHours, 0)
        self.assertEqual(settings.retentionCount, 0)

    def test_saved_bounds_clamp_at_zero_and_the_named_upper_limits(self):
        settings = resolveBackupSettings(
            "-1", "999", environ={
                BACKUP_INTERVAL_ENV_VAR: "24",
                BACKUP_RETENTION_ENV_VAR: "7",
            })
        upper = resolveBackupSettings(
            str(BACKUP_INTERVAL_HOURS_MAX + 1),
            str(BACKUP_RETENTION_COUNT_MAX + 1),
            environ={},
        )

        self.assertEqual(settings.intervalHours, 0)
        self.assertEqual(settings.retentionCount, BACKUP_RETENTION_COUNT_MAX)
        self.assertEqual(upper.intervalHours, BACKUP_INTERVAL_HOURS_MAX)
        self.assertEqual(upper.retentionCount, BACKUP_RETENTION_COUNT_MAX)

    def test_one_valid_saved_field_does_not_hide_the_other_field_environment_fallback(self):
        settings = resolveBackupSettings(
            "12", "bad", environ={
                BACKUP_INTERVAL_ENV_VAR: "24",
                BACKUP_RETENTION_ENV_VAR: "0",
            })

        self.assertEqual(settings.intervalHours, 12)
        self.assertEqual(settings.retentionCount, 0)

    def test_valid_saved_values_do_not_parse_or_warn_about_unused_environment_values(self):
        with self.assertNoLogs("Database.backup", level="WARNING"):
            settings = resolveBackupSettings(
                "12", "30", environ={
                    BACKUP_INTERVAL_ENV_VAR: "invalid",
                    BACKUP_RETENTION_ENV_VAR: "also-invalid",
                })

        self.assertEqual(settings.intervalHours, 12)
        self.assertEqual(settings.retentionCount, 30)

    def test_missing_blank_and_invalid_saved_values_fall_back_to_environment(self):
        environment = {
            BACKUP_INTERVAL_ENV_VAR: "11",
            BACKUP_RETENTION_ENV_VAR: "13",
        }

        for interval, retention in ((None, None), ("", " "), ("bad", "2.5")):
            with self.subTest(interval=interval, retention=retention):
                settings = resolveBackupSettings(interval, retention, environ=environment)
                self.assertEqual(settings.intervalHours, 11)
                self.assertEqual(settings.retentionCount, 13)

    def test_invalid_environment_keeps_existing_warning_and_default_behavior(self):
        with self.assertLogs(logging.getLogger("Database.backup"), level="WARNING") as logs:
            settings = resolveBackupSettings(
                environ={
                    BACKUP_INTERVAL_ENV_VAR: "not-an-int",
                    BACKUP_RETENTION_ENV_VAR: "",
                })

        self.assertEqual(settings.intervalHours, DEFAULT_BACKUP_INTERVAL_HOURS)
        self.assertEqual(settings.retentionCount, DEFAULT_BACKUP_RETENTION_COUNT)
        self.assertTrue(any(BACKUP_INTERVAL_ENV_VAR in message for message in logs.output))


if __name__ == "__main__":
    unittest.main()
