# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator
except ModuleNotFoundError:
    from base import BaseMigrator



class Migrator(BaseMigrator):
    """Advance the minor-release marker without changing schema or user data.

    The migration chain imports one step per minor version even when a release
    uses only existing tables. Both runtime VERSION and schema_version must
    advance so 1.54.x installations can start under 1.55.0.
    """

    def migrate(self):
        self.checkPreconditions()
        self.updateAppVersion("1.55.0")


if __name__ == "__main__":
    Migrator("1.54.0", "1.55.0").migrate()
