# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dependency-light resolution of the instance backup settings.

This module is imported by startup, the admin page and standalone migrators, so
it deliberately knows nothing about Repository, SQLite or application wiring.
Saved values are bounded persisted configuration; environment values retain
their historical zero-floor and unbounded-upper behavior.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from collections.abc import Mapping


logger = logging.getLogger("Database.backup")

BACKUP_INTERVAL_ENV_VAR = "BACKUP_INTERVAL_HOURS"
BACKUP_RETENTION_ENV_VAR = "BACKUP_RETENTION_COUNT"
DEFAULT_BACKUP_INTERVAL_HOURS = 24
DEFAULT_BACKUP_RETENTION_COUNT = 7

BACKUP_INTERVAL_HOURS_KEY = "backup_interval_hours"
BACKUP_INTERVAL_HOURS_MIN = 0
BACKUP_INTERVAL_HOURS_MAX = 168
BACKUP_RETENTION_COUNT_KEY = "backup_retention_count"
BACKUP_RETENTION_COUNT_MIN = 0
BACKUP_RETENTION_COUNT_MAX = 365


@dataclass(frozen=True)
class BackupSettings:
    intervalHours: int
    retentionCount: int


def envInt(name: str, default: int, *, environ: Mapping[str, str] | None = None,
           warningLogger: logging.Logger | None = None) -> int:
    """Parse one environment integer with the historical backup semantics."""
    source = os.environ if environ is None else environ
    raw = str(source.get(name, "")).strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        (warningLogger or logger).warning(
            "Ignoring non-numeric %s=%r, using default %d", name, raw, default)
        return default


def _savedInt(raw: object, minimum: int, maximum: int) -> int | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, value))


def resolveBackupSettings(savedInterval: object = None, savedRetention: object = None,
                          *, environ: Mapping[str, str] | None = None,
                          warningLogger: logging.Logger | None = None) -> BackupSettings:
    """Return effective interval/retention from raw saved and environment values.

    A missing or malformed saved field falls back independently to its
    environment value. Persisted values are clamped to their admin bounds;
    environment values intentionally remain unbounded above for compatibility.
    """
    interval = _savedInt(
        savedInterval, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX)
    retention = _savedInt(
        savedRetention, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX)
    if interval is None:
        interval = envInt(
            BACKUP_INTERVAL_ENV_VAR, DEFAULT_BACKUP_INTERVAL_HOURS,
            environ=environ, warningLogger=warningLogger)
    if retention is None:
        retention = envInt(
            BACKUP_RETENTION_ENV_VAR, DEFAULT_BACKUP_RETENTION_COUNT,
            environ=environ, warningLogger=warningLogger)
    return BackupSettings(
        intervalHours=interval,
        retentionCount=retention,
    )
