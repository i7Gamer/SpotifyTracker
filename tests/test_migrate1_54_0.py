# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The 1.55 release must advance its minor marker without rewriting user data."""

import sqlite3
from unittest.mock import patch

from Database.Migrators import dbversion
from Database.Migrators.migrate1_54_0 import Migrator
from test_migration_chain import APP_VERSION, MigrationChainTestCase


FROM_VERSION = "1.54.0"
PATCH_VERSION = "1.54.2"
TO_VERSION = "1.55.0"
WRONG_VERSION = "1.53.0"


class TestMigrate1_54_0(MigrationChainTestCase):
    def _nonVersionDump(self):
        conn = sqlite3.connect(self.dbPath)
        try:
            return [line for line in conn.iterdump()
                    if not line.startswith('INSERT INTO "schema_version"')]
        finally:
            conn.close()

    def _versions(self):
        conn = sqlite3.connect(self.dbPath)
        try:
            return conn.execute("SELECT version FROM schema_version ORDER BY rowid").fetchall()
        finally:
            conn.close()

    def test_minor_and_patch_markers_advance_without_schema_or_data_changes(self):
        for version in (FROM_VERSION, PATCH_VERSION):
            with self.subTest(version=version):
                self._seedDatabase(version=version)
                before = self._nonVersionDump()
                with self._redirectedRuntimeDir():
                    Migrator(FROM_VERSION, TO_VERSION).migrate()

                self.assertEqual(dbversion.readDbVersion(self.dbPath), TO_VERSION)
                self.assertEqual((self.runtimeDir / "VERSION").read_text().strip(), TO_VERSION)
                self.assertEqual(self._nonVersionDump(), before)

    def test_wrong_minor_is_rejected_without_changing_data_or_markers(self):
        self._seedDatabase(version=WRONG_VERSION)
        before = self._nonVersionDump(), self._versions()
        with self._redirectedRuntimeDir(), self.assertRaisesRegex(Exception, "expected from-version"):
            Migrator(FROM_VERSION, TO_VERSION).migrate()

        self.assertEqual((self._nonVersionDump(), self._versions()), before)
        self.assertEqual((self.runtimeDir / "VERSION").read_text().strip(), WRONG_VERSION)

    def test_full_startup_chain_from_last_release_is_a_no_op_on_restart(self):
        self._seedDatabase(version=PATCH_VERSION)
        before = self._nonVersionDump()
        with patch("Database.Migrators.migrate._snapshotBeforeMigrating") as snapshot:
            self._runChain()
            versionsAfterUpgrade = self._versions()
            self._runChain()

        snapshot.assert_called_once_with(self.runtimeDir)
        self.assertEqual(dbversion.readDbVersion(self.dbPath), APP_VERSION)
        self.assertEqual(self._versions(), versionsAfterUpgrade)
        self.assertEqual(self._nonVersionDump(), before)
