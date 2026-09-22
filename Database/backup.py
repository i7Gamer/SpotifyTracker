# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Automatic scheduled backups of the shared SQLite database.

Snapshots are taken with SQLite's online backup API - safe against a live,
WAL-mode database (the README explains why a raw file copy is not) - into
Database/Data/Backups/, which the standard Docker volume mount already
persists. Old snapshots are rotated out. Restart-safe: whether a backup is
due is judged from the newest existing backup file's mtime, not from process
start time, so a daily-restarting container doesn't back up on every boot.

Backups protect against app/database corruption and accidental deletion on
the same disk - copy them elsewhere for real disaster protection. Stored
secrets inside a backup are encrypted (see secret_store.py); keep the
encryption key alongside the backups, and treat backup+key together as
sensitive.
"""
import datetime
import logging

from Database import utils
import os
import random
import sqlite3
import threading
import time
from pathlib import Path

try:
    from Database.backup_settings import (
        BACKUP_INTERVAL_ENV_VAR, BACKUP_RETENTION_ENV_VAR,
        DEFAULT_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_RETENTION_COUNT,
        BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX,
        BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX,
        envInt as _leafEnvInt,
    )
except ModuleNotFoundError:
    from backup_settings import (
        BACKUP_INTERVAL_ENV_VAR, BACKUP_RETENTION_ENV_VAR,
        DEFAULT_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_RETENTION_COUNT,
        BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX,
        BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX,
        envInt as _leafEnvInt,
    )

try:
    import Database.db as db
    from Database.utils import parseError
    from Database.telemetry import WorkerTelemetryMixin
except ModuleNotFoundError:
    import db
    from utils import parseError
    from telemetry import WorkerTelemetryMixin

logger = logging.getLogger(__name__)

# Where snapshots go. Unset means Backups/ beside the database, which is the
# right default (it is inside the one directory the compose file already
# persists) and also the reason this variable exists: the snapshots then share
# a disk with the file they exist to survive, so one disk failure takes the
# database and every backup of it in the same moment.
#
# Interval and retention have been tunable from the environment all along; the
# DESTINATION was the one knob that needed a code change, which meant "keep a
# copy somewhere else" was not something an operator could just do. Point it at
# another disk, or at a mount of somewhere off the machine entirely.
BACKUP_DIR_ENV_VAR = "BACKUP_DIR"
BACKUP_DIR_NAME = "Backups"                 #< created next to the database file, inside the persisted Data/ volume
BACKUP_FILENAME_PREFIX = "spotify_stats_backup_"
BACKUP_STARTUP_MIN_DELAY_SECONDS = 60       #< random startup-offset bounds: don't race app startup (migrations
BACKUP_STARTUP_MAX_DELAY_SECONDS = 300      #  just ran, listeners are spinning up), and stagger against the other
                                            #  periodic workers instead of all firing at the same instant
BACKUP_CHECK_INTERVAL_SECONDS = 15 * 60     #< how often the worker re-checks whether a backup is due
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"   #< lexicographic order == chronological order, which rotation relies on
# How far past the newest existing snapshot a new name is pushed when the wall
# clock would not already sort after it - one tick of the format's own
# resolution, i.e. the smallest step that still orders. See _nextBackupStamp.
BACKUP_STAMP_MIN_STEP_SECONDS = 1
# Matches the app's own SQLITE_BUSY_TIMEOUT_MS. Not imported from Database.db:
# this module is deliberately importable standalone (the migrators use it that
# way, see Migrators/migrate.py's dual import).
BACKUP_BUSY_TIMEOUT_MS = 5000
# This worker's key in the shared cycle telemetry (see WorkerTelemetryMixin).
# One per process, so unlike the per-user workers it needs no user in the name.
BACKUP_TELEMETRY_NAME = "backup"
# How long stop() waits for the loop thread - it can be inside a multi-GB
# snapshot, which no shutdown should sit through. Part of the shutdown budget
# the compose file's stop_grace_period has to cover (tests/test_compose_shutdown_budget.py).
BACKUP_STOP_JOIN_TIMEOUT_SECONDS = 5

# Serializes every snapshot in the PROCESS, not merely per BackupWorker. The
# scheduled loop and the admin's Create Backup Now button share one instance, but
# Migrators/migrate.py builds its own ad-hoc worker for the pre-migration
# snapshot - and a per-instance lock left those two free to overlap while the
# docstring promised otherwise. Overlapping runs sweep each other's .partial file
# mid-write (PermissionError on Windows, where an open handle can't be unlinked)
# and, when both start within the same second, write the same path from two
# connections - so os.replace can promote an interleaved copy to a
# valid-looking snapshot.
#
# All callers back the one shared database, so there is nothing to gain from a
# per-path lock, and a single one cannot be got wrong.
_BACKUP_LOCK = threading.Lock()


def _envInt(name: str, default: int) -> int:
    return _leafEnvInt(name, default, warningLogger=logger)


def _discardPartial(partialPath: Path) -> None:
    """Remove a .partial the caller is about to raise about.

    Swallowing the removal's own failure is the point: both callers run this
    from an `except` block, and an exception raised there REPLACES the one
    being handled. The failure this cleans up after is a disk-full copy, and on
    Windows the moment a full-size file is written is exactly when a scanner
    may still hold it - answering "why did my backup fail?" with the janitor's
    PermissionError instead of the disk's error would send the operator after
    the wrong problem. The leftover is logged and collected by the sweep at the
    top of the next run."""
    try:
        partialPath.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not remove the incomplete backup %s (it will be swept on the next run): %s",
                       partialPath, parseError(e))


class BackupWorker(WorkerTelemetryMixin):
    """One per process (the database is shared across every user).

    Cycle outcomes are recorded through the same telemetry mixin the per-user
    backfillers use: a scheduled backup that fails is logged and retried on the
    next check, which is right, but for a long time that was the whole story -
    /admin reported the service by thread liveness alone, so one that had been
    failing every 15 minutes for a month still read RUNNING. See getSummary."""

    def __init__(self, dbPath: Path | None = None, backupDir: Path | None = None,
                 intervalHours: int | None = None, retentionCount: int | None = None):
        # Resolved at call time (not as a default argument) so tests that
        # monkeypatch db.DEFAULT_DB_PATH are honored - same pattern as
        # Repository.__init__.
        self.dbPath = Path(dbPath if dbPath is not None else db.DEFAULT_DB_PATH)
        # Argument first, then the variable, then beside the database. No
        # PRODUCTION caller passes the argument - the scheduled worker
        # (app.py) and the pre-migration snapshot (Migrators/migrate.py) both
        # leave it unset ON PURPOSE, so an operator's BACKUP_DIR governs every
        # snapshot alike, the pre-migration one included: off-disk protection
        # matters most at the riskiest write of the boot
        # (test_migration_chain pins that call shape). The argument is the
        # test seam, and it winning over the variable is what lets a test pin
        # a location the environment can't move.
        self.backupDir = Path(backupDir) if backupDir is not None else self._configuredBackupDir()
        self.intervalHours = intervalHours if intervalHours is not None else _envInt(
            BACKUP_INTERVAL_ENV_VAR, DEFAULT_BACKUP_INTERVAL_HOURS)
        self.retentionCount = retentionCount if retentionCount is not None else _envInt(
            BACKUP_RETENTION_ENV_VAR, DEFAULT_BACKUP_RETENTION_COUNT)
        self.thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._initWorkerTelemetry()

    def _configuredBackupDir(self) -> Path:
        """BACKUP_DIR, or Backups/ beside the database.

        Stripped and tested for emptiness rather than passed to Path()
        directly: `BACKUP_DIR=` in a compose file means "I did not set this",
        and Path("") is Path("."), which for the container is /app - a layer
        that does not survive `docker compose pull`. Snapshots would be written
        and then thrown away by the next update, silently."""
        configured = os.environ.get(BACKUP_DIR_ENV_VAR, "").strip()
        if not configured:
            return self.dbPath.parent / BACKUP_DIR_NAME
        return Path(configured)

    def getSummary(self) -> dict:
        """{status, consecutive_failures, failure_rate, last_error} for
        /admin's Worker Health card - the same fields the per-user workers
        report, judged against the same WORKER_HEALTH_FAILING_THRESHOLD.

        `status` is still thread liveness; the telemetry is what distinguishes
        a service that is running from one that is working."""
        running = self.thread is not None and self.thread.is_alive()
        return {
            "status": "RUNNING" if running else "INACTIVE",
            **self._getWorkerTelemetry(BACKUP_TELEMETRY_NAME),
        }

    def isEnabled(self) -> bool:
        return self.intervalHours > 0 and self.retentionCount > 0

    def _backupFiles(self) -> list[Path]:
        """Existing snapshots, oldest first (timestamped names sort
        chronologically). Only files this worker created are considered -
        a user's own manual copies in the same folder are never touched."""
        if not self.backupDir.exists():
            return []
        return sorted(p for p in self.backupDir.iterdir()
                      if p.is_file() and p.name.startswith(BACKUP_FILENAME_PREFIX) and p.suffix == ".db")

    def _newestBackupFile(self) -> Path | None:
        files = self._backupFiles()
        return files[-1] if files else None

    def newestBackupTime(self) -> float | None:
        newestFile = self._newestBackupFile()
        return newestFile.stat().st_mtime if newestFile is not None else None

    def isDue(self) -> bool:
        if not self.isEnabled():
            return False
        newestFile = self._newestBackupFile()
        if newestFile is None:
            return True
        elapsed = time.time() - newestFile.stat().st_mtime
        if elapsed < 0:
            # A clock that stepped backward (or a snapshot with a bogus future
            # mtime) would otherwise wedge this into "never due again": the
            # delta only grows more negative, so the interval threshold below
            # is never crossed again. Treat it as due instead of silently
            # stopping scheduled backups forever.
            logger.warning(
                "Newest backup %s has a timestamp ahead of the system clock "
                "(mtime is %.0fs in the future); treating a backup as due",
                newestFile, -elapsed)
            return True
        return elapsed >= self.intervalHours * 3600

    def isBackupRunning(self) -> bool:
        """True while a snapshot is in progress anywhere in this process.
        Advisory only - callers use it for a fast "already in progress" answer;
        correctness comes from runBackup's own lock."""
        return _BACKUP_LOCK.locked()

    def runBackup(self) -> Path:
        """Snapshot the database and rotate old snapshots. Writes to a
        .partial file first and renames only on success, so a crash mid-backup
        can't leave a truncated file that looks like a valid snapshot.

        Serialized process-wide (see _BACKUP_LOCK): a caller arriving while
        another snapshot runs waits for it rather than overlapping - including
        the ad-hoc worker the migrators build for their pre-migration snapshot."""
        with _BACKUP_LOCK:
            return self._runBackupLocked()

    def _runBackupLocked(self) -> Path:
        # A missing source would be silently CREATED by sqlite3.connect below,
        # producing a valid-looking but EMPTY snapshot - refuse instead, so a
        # misconfigured path can't mask itself as a successful backup.
        if not self.dbPath.exists():
            raise FileNotFoundError(f"Database to back up does not exist: {self.dbPath}")

        self.backupDir.mkdir(parents=True, exist_ok=True)
        # Sweep any .partial left by a process that died mid-backup: rotation
        # only ever looks at *.db, so these would otherwise accumulate forever
        # (each a full multi-GB copy).
        for stalePartial in self.backupDir.glob(f"{BACKUP_FILENAME_PREFIX}*.partial"):
            stalePartial.unlink(missing_ok=True)

        # In the instance's configured zone, not the C runtime's local time -
        # the same defect the log had (logging_config.InstanceZoneFormatter):
        # Windows' UCRT parses TZ as a POSIX spec, so an IANA name silently
        # resolved to a fixed offset and every filename ran an hour off for
        # half the year. The name is what an operator picks a restore by.
        stamp = self._nextBackupStamp()
        finalPath = self.backupDir / f"{BACKUP_FILENAME_PREFIX}{stamp}.db"
        partialPath = finalPath.with_suffix(".partial")

        source = sqlite3.connect(self.dbPath)
        try:
            # Every ConnectionManager connection waits out a lock rather than
            # failing instantly (see Database/db.py); this one opened raw and
            # didn't, so a snapshot starting while a checkpoint or VACUUM held
            # the file raised "database is locked" immediately. The scheduled
            # loop swallows that into a skipped backup and the admin button
            # reports an error, for a wait the rest of the app is happy to make.
            source.execute(f"PRAGMA busy_timeout = {BACKUP_BUSY_TIMEOUT_MS}")
            destination = sqlite3.connect(partialPath)
            try:
                source.backup(destination)
            except BaseException:
                # Closed first, then removed: on Windows an open handle blocks
                # the unlink. The sweep at the top of the next run would collect
                # it eventually, but "eventually" is the next scheduled backup,
                # and a full-size copy of the database left behind is exactly
                # what must not happen when the copy failed for want of space.
                destination.close()
                _discardPartial(partialPath)
                raise
            finally:
                destination.close()
        finally:
            source.close()

        try:
            os.replace(partialPath, finalPath)
        except Exception:
            _discardPartial(partialPath)
            raise
        logger.info("Database backed up to %s", finalPath)
        self._rotate(justWritten=finalPath)
        return finalPath

    @staticmethod
    def _stampOf(path: Path) -> str:
        """The timestamp part of a snapshot's filename."""
        return path.stem[len(BACKUP_FILENAME_PREFIX):]

    def _nextBackupStamp(self) -> str:
        """The filename stamp for a new snapshot, guaranteed to sort after
        every snapshot already in the directory.

        BACKUP_TIMESTAMP_FORMAT's comment states the invariant rotation relies
        on - lexicographic order == chronological order - but nothing enforced
        it, and the wall clock is not monotonic. A backward step (NTP
        correcting a fast clock, a restored VM snapshot, a resume from suspend)
        gave the new snapshot a name that sorted BEFORE the retained ones, so
        _rotate deleted the file os.replace had just promoted, reported
        success, and - because isDue()'s own future-mtime guard kept firing -
        did it again on every check, copying the whole database each time.

        Stepping past the newest existing name makes that invariant true by
        construction, which is cheaper than teaching every reader of
        _backupFiles() to distrust the order. The stamp then lags the wall
        clock for as long as the anomaly lasts: that is the trade, and it is
        the right way round, because a name off by the size of the clock error
        still restores, while a directory that silently stops accepting
        snapshots does not. The file's mtime still records when the copy was
        really taken.

        It also closes a smaller hole on the way past: two backups landing in
        the same second used to build the same name, and os.replace would
        overwrite a snapshot that was still being retained."""
        stamp = datetime.datetime.now(tz=utils.tz).strftime(BACKUP_TIMESTAMP_FORMAT)
        newest = self._newestBackupFile()
        if newest is None:
            return stamp
        newestStamp = self._stampOf(newest)
        if stamp > newestStamp:
            return stamp
        try:
            newestMoment = datetime.datetime.strptime(newestStamp, BACKUP_TIMESTAMP_FORMAT)
        except ValueError:
            #< a hand-renamed file this cannot read; nothing better than the
            #  wall clock is available, and _rotate's justWritten guard below
            #  still keeps this run's own snapshot
            return stamp
        return (newestMoment + datetime.timedelta(seconds=BACKUP_STAMP_MIN_STEP_SECONDS)
                ).strftime(BACKUP_TIMESTAMP_FORMAT)

    def _rotate(self, justWritten: Path | None = None) -> None:
        # retentionCount 0 disables scheduled backups outright (see isEnabled),
        # so rotation must be a no-op: reading it as "keep zero snapshots"
        # would make a direct runBackup() call delete every existing snapshot -
        # including the one runBackup() itself just wrote.
        if not self.retentionCount:
            return
        files = self._backupFiles()
        for stale in files[:-self.retentionCount]:
            # Belt and braces behind _nextBackupStamp: that makes this run's
            # snapshot the newest name, so it cannot reach this slice - but
            # "never delete the copy you just took" is the property that
            # actually matters, and it should not rest on the naming alone
            # (see the unparseable-name fallback above). Skipped here rather
            # than filtered out of `files`, which would have excluded it from
            # the retention COUNT too and kept one snapshot too many.
            if justWritten is not None and stale == justWritten:
                continue
            try:
                stale.unlink()
                logger.info("Rotated out old backup %s", stale.name)
            except OSError as e:
                logger.warning("Could not delete old backup %s: %s", stale, e)

    def _loop(self, stop_event: threading.Event | None = None) -> None:
        # `stop_event` is THIS run's private event (see the fresh-event note
        # in start()) - a later restart can never revive this thread.
        if stop_event is None:
            stop_event = self._stop_event
        if stop_event.wait(random.randint(BACKUP_STARTUP_MIN_DELAY_SECONDS,
                                          BACKUP_STARTUP_MAX_DELAY_SECONDS)):
            return
        while not stop_event.is_set():
            try:
                # Recorded only when a backup was actually attempted: an idle
                # check between two daily snapshots is not a cycle, and
                # counting it would dilute the failure rate towards zero
                # exactly when it should be alarming (see _recordWorkerCycle).
                # A failing isDue() does count - it reads the backup directory,
                # so no backup can be taken through it either.
                if self.isDue():
                    self.runBackup()
                    self._recordWorkerCycle(BACKUP_TELEMETRY_NAME, success=True)
            except Exception as e:
                self._recordWorkerCycle(BACKUP_TELEMETRY_NAME, success=False, error=parseError(e))
                logger.error("Scheduled backup failed: %s", e)
            if stop_event.wait(BACKUP_CHECK_INTERVAL_SECONDS):
                return

    def start(self) -> None:
        if not self.isEnabled():
            logger.info("Scheduled backups disabled (%s=%d, %s=%d)",
                        BACKUP_INTERVAL_ENV_VAR, self.intervalHours,
                        BACKUP_RETENTION_ENV_VAR, self.retentionCount)
            return
        if self.thread is not None and self.thread.is_alive():
            return
        # A FRESH event per run: stop() joins with a timeout, so a thread
        # blocked in a long backup can outlive it - clearing a shared event
        # here would revive that zombie alongside the new thread. With its
        # own still-set event it exits on its own instead.
        stop_event = threading.Event()
        self._stop_event = stop_event
        self.thread = threading.Thread(target=self._loop, args=(stop_event,),
                                       name="backup-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=BACKUP_STOP_JOIN_TIMEOUT_SECONDS)
