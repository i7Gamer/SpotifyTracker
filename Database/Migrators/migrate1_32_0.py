# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import os

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import (
        Repository, SKIP_THRESHOLD_MODE_KEY,
        SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE,
        COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_DEFAULT,
        GENRE_BACKFILL_RETRY_DAYS_KEY, BIO_BACKFILL_RETRY_DAYS_KEY,
        GENRE_BACKFILL_RETRY_SECONDS, BIOGRAPHY_BACKFILL_RETRY_SECONDS, SECONDS_PER_DAY,
        EMAIL_VERIFICATION_SETTING_KEY,
    )
    from Database.backup_settings import (
        BACKUP_INTERVAL_HOURS_KEY, BACKUP_RETENTION_COUNT_KEY,
        envInt as _envInt, BACKUP_INTERVAL_ENV_VAR, BACKUP_RETENTION_ENV_VAR,
        DEFAULT_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_RETENTION_COUNT,
    )
    from config import TRUTHY_ENV_VALUES, SKIP_EMAIL_VERIFICATION_ENV_VAR
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import (
        Repository, SKIP_THRESHOLD_MODE_KEY,
        SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE,
        COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_DEFAULT,
        GENRE_BACKFILL_RETRY_DAYS_KEY, BIO_BACKFILL_RETRY_DAYS_KEY,
        GENRE_BACKFILL_RETRY_SECONDS, BIOGRAPHY_BACKFILL_RETRY_SECONDS, SECONDS_PER_DAY,
        EMAIL_VERIFICATION_SETTING_KEY,
    )
    from backup_settings import (
        BACKUP_INTERVAL_HOURS_KEY, BACKUP_RETENTION_COUNT_KEY,
        envInt as _envInt, BACKUP_INTERVAL_ENV_VAR, BACKUP_RETENTION_ENV_VAR,
        DEFAULT_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_RETENTION_COUNT,
    )
    from config import TRUTHY_ENV_VALUES, SKIP_EMAIL_VERIFICATION_ENV_VAR


class Migrator(BaseMigrator):
    """1.32.0 -> 1.33.0: merge play_skips into plays + seed instance settings.

    Rebuilds the plays table to add is_skip and relax the time_played CHECK to
    >= 0, folds every play_skips row in as is_skip=1 (then drops play_skips), and
    seeds the instance-wide settings this release moved into app_settings: the
    skip threshold, the completion complete-percent, the Last.fm genre/biography
    backfill retry intervals, the backup interval/retention (from the existing
    env vars), and the cookie<->email verification toggle (disabled iff the
    SKIP_EMAIL_VERIFICATION env var is set). Each seed is idempotent - only
    written when the row is absent, so a re-run or an admin's prior change is
    never clobbered. See Repository.mergePlaySkipsIntoPlays."""

    def _seedSettings(self, repo) -> None:
        emailVerification = "0" if os.environ.get(SKIP_EMAIL_VERIFICATION_ENV_VAR, "").strip().lower() in TRUTHY_ENV_VALUES else "1"
        seeds = {
            SKIP_THRESHOLD_MODE_KEY: SKIP_THRESHOLD_DEFAULT_MODE,   #< value seeded via setSkipThreshold below
            COMPLETION_COMPLETE_PERCENT_KEY: str(COMPLETION_COMPLETE_PERCENT_DEFAULT),
            GENRE_BACKFILL_RETRY_DAYS_KEY: str(GENRE_BACKFILL_RETRY_SECONDS // SECONDS_PER_DAY),
            BIO_BACKFILL_RETRY_DAYS_KEY: str(BIOGRAPHY_BACKFILL_RETRY_SECONDS // SECONDS_PER_DAY),
            BACKUP_INTERVAL_HOURS_KEY: str(_envInt(BACKUP_INTERVAL_ENV_VAR, DEFAULT_BACKUP_INTERVAL_HOURS)),
            BACKUP_RETENTION_COUNT_KEY: str(_envInt(BACKUP_RETENTION_ENV_VAR, DEFAULT_BACKUP_RETENTION_COUNT)),
            EMAIL_VERIFICATION_SETTING_KEY: emailVerification,
        }
        if repo.getAppSetting(SKIP_THRESHOLD_MODE_KEY) is None:
            repo.setSkipThreshold(SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE)
        for key, value in seeds.items():
            if key == SKIP_THRESHOLD_MODE_KEY:
                continue   #< handled by setSkipThreshold (also seeds the value key)
            if repo.getAppSetting(key) is None:
                repo.setAppSetting(key, value)

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            result = repo.mergePlaySkipsIntoPlays()
            self._seedSettings(repo)
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Merged play_skips into plays ({result}); seeded instance settings.")
        self.updateAppVersion("1.33.0")


if __name__ == "__main__":
    Migrator("1.32.0", "1.33.0").migrate()
