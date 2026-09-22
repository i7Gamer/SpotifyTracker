# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import resolveRuntimeDir, BaseMigrator
    from Database.Migrators import dbversion
except ModuleNotFoundError:
    from base import resolveRuntimeDir, BaseMigrator
    import dbversion

try:
    from Database.backup_settings import BACKUP_INTERVAL_HOURS_KEY, BACKUP_RETENTION_COUNT_KEY
except ModuleNotFoundError:
    from backup_settings import BACKUP_INTERVAL_HOURS_KEY, BACKUP_RETENTION_COUNT_KEY

import logging
import sqlite3
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

# configureLogging() (Database/logging_config.py) always runs before
# migrateIfNeeded() - it's the first line of SpotifyDashboardApp.__init__ - so
# this reaches a configured handler in the one place that matters. dev.py's
# standalone migrator run and this module's own print() calls predate that
# guarantee and are left as-is; only the new skip-notice below needs an
# actual log level (a test asserts on it), so nothing else in this file was
# converted.
logger = logging.getLogger(__name__)

# The oldest database version with a surviving migrator. Everything below it
# is the JSON-file era (history.json/entries.json/tracks.json): those six
# migrators were removed in Phase 2 of the dependency rewrite, and the release
# named here is the last one that still carried them. A database that old gets
# a clear refusal pointing through that release instead of a
# FileNotFoundError from a missing migrator module.
MIGRATION_FLOOR_VERSION = "1.6.0"
# 1.45.0, NOT the release the migrators were removed in: this tree ships as
# 1.46.x, so naming a 1.46 release would tell the user to run (a sibling of)
# the very release that is refusing them - releases either side of the removal
# share that minor and only the one cut before it can migrate. 1.45.0 uniquely
# predates the removal and migrates a JSON-era database all the way past the
# floor.
# (tests/test_migrators.py pins this constant strictly below Database/VERSION.)
LAST_JSON_ERA_CAPABLE_RELEASE = "1.45.0"


def _readBackupSettings(dbPath: Path) -> tuple[str | None, str | None]:
    """Read only persisted backup rows without opening the application schema."""
    conn = dbversion.openMigrationConnection(dbPath, readOnly=True)
    try:
        tableExists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'"
        ).fetchone() is not None
        if not tableExists:
            return None, None
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN (?, ?)",
            (BACKUP_INTERVAL_HOURS_KEY, BACKUP_RETENTION_COUNT_KEY),
        ).fetchall()
        values = {row[0]: row[1] for row in rows}
        return values.get(BACKUP_INTERVAL_HOURS_KEY), values.get(BACKUP_RETENTION_COUNT_KEY)
    finally:
        conn.close()


def _loadMigratorModule(moduleName: str, modulePath: Path):
    """One migrator file, imported by path - not via the package system, so
    dev.py can run migrations standalone from inside Database/."""
    spec = spec_from_file_location(moduleName, modulePath)
    if spec is None or spec.loader is None:
        raise ImportError(f"No loadable migrator module at {modulePath}")
    migratorModule = module_from_spec(spec)
    spec.loader.exec_module(migratorModule)
    return migratorModule


def migrate(major, minor, baseDir):
    """Run the single migration step that lifts a {major}.{minor}.0 database
    to the next minor version."""
    fromVersion = f"{major}.{minor}.0"
    print(f"Migrating from version {fromVersion}")

    # Migrator module names encode the major version too (not hardcoded to
    # "1") so a future major-version bump's migrators (e.g. migrate2_0_0.py)
    # get picked up correctly instead of always looking for a "migrate1_*"
    # file regardless of which major version the database is actually on.
    moduleName = f"migrate{major}_{minor}_0"
    migratorModule = _loadMigratorModule(moduleName, baseDir / f"{moduleName}.py")
    migratorModule.Migrator(fromVersion, f"{major}.{minor + 1}.0").migrate()

def _resolveDatabaseVersion(runtimeDir: Path) -> str | None:
    """The current database version, or None if this is a genuinely fresh
    install (nothing to migrate). The version lives inside spotify_stats.db
    itself (schema_version table) once one exists - it then survives a raw
    file copy, which is how Database/backup.py snapshots the database and
    exactly the scenario a sibling VERSION file gets desynced by (a backup
    predating a later migration, restored after the sibling file has since
    moved on).

    A database that predates the schema_version table falls back to the
    sibling file, backfilling the in-db marker so the next read (and the
    next backup) carries it. A database with real data but no marker
    anywhere - an orphaned file, or a backup restored without its VERSION
    file - is refused rather than guessed at: several historical migrations
    (data-only cleanups, in-place encryption) leave no structural trace a
    version could be reliably inferred from, so a silent wrong guess risks
    silently skipping a migration the data actually needs."""
    dbPath = runtimeDir / "spotify_stats.db"
    databaseVersionFile = runtimeDir / "VERSION"

    if dbPath.exists():
        dbVersion = dbversion.readDbVersion(dbPath)
        if dbVersion is not None:
            return dbVersion
        if databaseVersionFile.exists():
            version = databaseVersionFile.read_text().strip()
            dbversion.writeDbVersion(dbPath, version)
            return version
        if dbversion.hasAnyData(dbPath):
            raise RuntimeError(
                f"{dbPath} has data but no version marker, either inside the "
                "database or in a sibling VERSION file - refusing to guess. "
                "If this is a restored backup, restore its VERSION file "
                "alongside it, or set the version explicitly via "
                "Database.Migrators.dbversion.writeDbVersion()."
            )
        return None   #< empty db, no marker anywhere - fresh install
    if databaseVersionFile.exists():
        return databaseVersionFile.read_text().strip()
    return None   #< pre-database (JSON-history) era, nothing on disk yet - fresh install


def _snapshotBeforeMigrating(runtimeDir: Path) -> None:
    """One-off safety snapshot taken right before the first migration step
    runs this startup. Migrations run automatically at every app boot,
    before the backup worker's own scheduled snapshot has a chance to run
    (its randomized startup delay exists specifically to not race this) - so
    without this, the most recent recovery point for a migration that
    corrupts data (a logic bug, or a crash mid multi-step migration) could be
    up to BACKUP_INTERVAL_HOURS (default 24h) stale.

    Best-effort: a database that doesn't exist yet (pre-1.7.0, JSON-history
    era, or a fresh install) has nothing to snapshot, and a failed snapshot
    must never block startup - migrating with no extra safety net is still
    better than refusing to start at all.

    Skipped entirely when the operator has disabled backups
    (BACKUP_RETENTION_COUNT=0 or BACKUP_INTERVAL_HOURS=0, i.e.
    BackupWorker.isEnabled() is False): before this check, retention 0 still
    left the SCHEDULED backup worker inert (see isEnabled/_rotate) while this
    one-off call kept writing a full, unrotated copy of the database into
    Backups/ on every minor upgrade - unbounded growth for exactly the
    operator who said "no automatic backups" (F-B-3, 2026-09-04 review).
    Honoring the same switch here removes that operator's last automatic
    recovery point for a migration gone wrong; the README says to take a
    manual snapshot from /admin first if that matters to them."""
    dbPath = runtimeDir / "spotify_stats.db"
    if not dbPath.exists():
        return
    try:
        from Database.backup import BackupWorker, BACKUP_INTERVAL_ENV_VAR, BACKUP_RETENTION_ENV_VAR
        from Database.backup_settings import (
            resolveBackupSettings,
        )
    except ModuleNotFoundError:
        from backup import BackupWorker, BACKUP_INTERVAL_ENV_VAR, BACKUP_RETENTION_ENV_VAR
        from backup_settings import (
            resolveBackupSettings,
        )
    try:
        savedInterval, savedRetention = _readBackupSettings(dbPath)
        settings = resolveBackupSettings(savedInterval, savedRetention)
    except (sqlite3.Error, OSError) as e:
        logger.error("Skipping pre-migration snapshot: could not read backup settings: %s", e)
        return
    # No backupDir on purpose: the operator's BACKUP_DIR governs this
    # snapshot exactly like the scheduled ones - off-disk protection matters
    # most at the riskiest write of the boot. The trade is that a BACKUP_DIR
    # mount that is down at boot costs this snapshot (caught below, startup
    # continues), which the beside-the-db default never risked.
    # test_migration_chain pins this call shape.
    worker = BackupWorker(
        dbPath=dbPath,
        intervalHours=settings.intervalHours,
        retentionCount=settings.retentionCount,
    )
    if not worker.isEnabled():
        logger.info(
            "Skipping pre-migration snapshot: backups are disabled (%s=%s, %s=%s).",
            BACKUP_INTERVAL_ENV_VAR, worker.intervalHours,
            BACKUP_RETENTION_ENV_VAR, worker.retentionCount,
        )
        return
    try:
        worker.runBackup()
    except Exception as e:
        print(f"Pre-migration snapshot failed (continuing without it): {e}")


def migrateIfNeeded() -> None:
    migratorsDir = Path(__file__).resolve().parent
    appVersion = (migratorsDir / ".." / "VERSION").read_text().strip()

    runtimeDir = resolveRuntimeDir(migratorsDir)
    databaseVersion = _resolveDatabaseVersion(runtimeDir)
    if databaseVersion is None:
        # First run: stamp the current version everywhere and skip migration.
        runtimeDir.mkdir(parents=True, exist_ok=True)   #< runtime data dir absent on a fresh install
        (runtimeDir / "VERSION").write_text(appVersion)
        dbPath = runtimeDir / "spotify_stats.db"
        if dbPath.exists():
            dbversion.writeDbVersion(dbPath, appVersion)
        return

    # Compare the full (major, minor) pair, not just the minor component -
    # otherwise a database and app that only differ in major version (e.g.
    # "1.7.0" vs "2.7.0") would be mistaken for already being up to date, and
    # a genuine major bump would make the loop below hunt forever for a
    # migrator file that can never satisfy a minor-only comparison.
    needsMigration = BaseMigrator.getMajorMinor(databaseVersion) != BaseMigrator.getMajorMinor(appVersion)
    if needsMigration:
        # The rollback direction, and the mirror of the floor check below.
        # The chain only ever steps FORWARD, so an app older than its database
        # sent the loop hunting for a migrator that steps further away from the
        # app - one that by definition does not exist yet. What the operator got
        # was `FileNotFoundError: .../migrate1_51_0.py`, which reads as a broken
        # build rather than "you rolled back onto a newer database", and under
        # Docker repeats as a crash loop. Nothing is damaged either way - no
        # migrator runs - but the message has to name the actual situation.
        #
        # Before _snapshotBeforeMigrating: the snapshot is for a migration that
        # is about to happen, and taking one here would copy the whole database
        # on every restart of that crash loop.
        if BaseMigrator.getMajorMinor(databaseVersion) > BaseMigrator.getMajorMinor(appVersion):
            raise RuntimeError(
                f"This database is at version {databaseVersion}, newer than this release "
                f"({appVersion}) - it was written by a later version and migrations only "
                f"run forwards. Start the newer release again, or restore a backup taken "
                f"before the upgrade."
            )

        # The floor only matters when there is something to migrate: an ancient
        # install whose app and database AGREE has nothing to run and keeps
        # working (its own release still carries whatever it needs).
        if BaseMigrator.getMajorMinor(databaseVersion) < BaseMigrator.getMajorMinor(MIGRATION_FLOOR_VERSION):
            raise RuntimeError(
                f"This database is at version {databaseVersion}, older than "
                f"{MIGRATION_FLOOR_VERSION} - the oldest version this release can "
                f"still migrate (the JSON-file-era migrators were removed). Run "
                f"release {LAST_JSON_ERA_CAPABLE_RELEASE} once to bring the data "
                f"up, then upgrade to this release."
            )

        _snapshotBeforeMigrating(runtimeDir)

        while BaseMigrator.getMajorMinor(databaseVersion) != BaseMigrator.getMajorMinor(appVersion):
            dbMajor, dbMinor = BaseMigrator.getMajorMinor(databaseVersion)
            migrate(dbMajor, dbMinor, migratorsDir)

            runtimeDir = resolveRuntimeDir(migratorsDir)   #< location may have changed (e.g. a Users/ -> Data/ rename)
            databaseVersion = _resolveDatabaseVersion(runtimeDir)

            # Termination is otherwise entirely on every migrator remembering to
            # stamp its successor. One that forgets - or copy-pastes the wrong
            # version - re-runs its own DATA migration forever at boot, against
            # real user databases, with no error and no output: the worst shape a
            # startup bug can take. All 46 shipped migrators do stamp; this is
            # what makes the 47th's mistake a startup failure that names it.
            if (databaseVersion is None
                    or BaseMigrator.getMajorMinor(databaseVersion) <= (dbMajor, dbMinor)):
                raise RuntimeError(
                    f"The migrator for {dbMajor}.{dbMinor} left this database at "
                    f"{databaseVersion} - it did not stamp a newer version, so the "
                    f"migration chain cannot move forward. Its migrate() has to call "
                    f"updateAppVersion() with the version it upgrades to."
                )

    # The PATCH component is this function's own job: the chain steps at minor
    # granularity and its last migrator stamps x.y.0, so on a patch release
    # (1.46.0 -> 1.46.1) no migrator runs and none could bring the markers to
    # the exact running version. Without this, an upgraded install reports the
    # old version forever while a fresh install of the same release stamps the
    # full one. A no-op on the common startup (markers already exact).
    if databaseVersion != appVersion:
        (runtimeDir / "VERSION").write_text(appVersion)
        dbPath = runtimeDir / "spotify_stats.db"
        if dbPath.exists():
            dbversion.writeDbVersion(dbPath, appVersion)
