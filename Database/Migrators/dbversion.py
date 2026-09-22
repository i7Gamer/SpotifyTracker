# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The database's schema version, stored inside the sqlite file itself rather
than in a sibling VERSION text file. A raw file copy (a backup, a manual
`cp`) carries this along automatically - SQLite's own backup API (used by
Database/backup.py) copies every table, schema_version included - whereas a
sibling file left in the original directory silently desyncs from a restored
copy. Deliberately independent of Repository/ConnectionManager/db.SCHEMA:
those run the app's full *current* schema on every connection, which would
stamp every current table onto an old database before its true version was
ever read.
"""
import sqlite3
import time
from pathlib import Path

# Python's sqlite3 connections wait five seconds for a lock by default.
# Allow thirty seconds here for a checkpoint, VACUUM, or concurrent writer,
# The app and backup connections also set their lock timeouts explicitly.
MIGRATION_BUSY_TIMEOUT_MS = 30_000


def openMigrationConnection(dbPath: Path, readOnly: bool = False) -> sqlite3.Connection:
    """A connection for the helpers below, with MIGRATION_BUSY_TIMEOUT_MS set.

    readOnly opens through a file: URI so a read can neither create the file
    nor write to it - readDbVersion documents itself as never writing, and
    "never writing" has to include never bringing a missing database into
    existence."""
    if readOnly:
        conn = sqlite3.connect(f"file:{Path(dbPath).resolve().as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(dbPath)
    conn.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")
    return conn


SCHEMA_VERSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     TEXT NOT NULL,
    applied_at  REAL NOT NULL
);
"""


def readDbVersion(dbPath: Path) -> str | None:
    """The most recently recorded version, or None if this database predates
    the schema_version table (or the table exists but is empty - same
    meaning, just written by a prior readDbVersion() call).

    Answers None for a database that isn't there: a version read is also how
    a caller asks "is this a new install", and raising at it would make the
    answer an exception rather than a value."""
    if not Path(dbPath).exists():
        return None
    conn = openMigrationConnection(dbPath, readOnly=True)
    try:
        # Probe rather than CREATE TABLE IF NOT EXISTS: a read must not write.
        # Creating the table here would fail on a read-only file/filesystem
        # (e.g. inspecting a backup on read-only media) and leave a stray empty
        # table + journal on any db that's only being inspected. Table creation
        # lives in writeDbVersion. A missing table means the same as an empty
        # one: no version recorded yet -> None.
        tableExists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone() is not None
        if not tableExists:
            return None
        # rowid alone: writeDbVersion only ever INSERTs (see its docstring),
        # so this table is append-only and rowid order IS insertion order.
        # applied_at is an audit column, not an ordering key - it stays for
        # "when was this applied" but must never be allowed to reorder reads.
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def writeDbVersion(dbPath: Path, version: str) -> None:
    """Appends a new current-version row rather than overwriting the last one
    - a cheap audit trail, and it means this can never lose a prior write."""
    conn = openMigrationConnection(dbPath)
    try:
        conn.execute(SCHEMA_VERSION_TABLE_SQL)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def hasAnyData(dbPath: Path) -> bool:
    """Whether any table other than schema_version itself has at least one
    row - distinguishes a genuinely fresh/empty database (safe to stamp with
    the current version, no migration needed) from a legacy database that has
    real data but was never given a version marker."""
    conn = openMigrationConnection(dbPath, readOnly=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for table in tables:
            if table == "schema_version":
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count > 0:
                return True
        return False
    finally:
        conn.close()
