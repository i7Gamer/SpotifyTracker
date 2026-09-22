# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reconciliation deletes and Wrapped invalidation commit as one change."""

import datetime
import sqlite3
from contextlib import closing
from unittest.mock import MagicMock, patch

from conftest import rawSpotifyTrackForTest, wrappedCachedRow
from Database.Formatters.spotifyClient import Client
from test_wrapped_invalidation_scope import ScopeTestCase


API_TIMESTAMP = datetime.datetime.fromisoformat("2024-01-01T00:00:01+00:00").timestamp()
LATER_API_TIMESTAMP = datetime.datetime.fromisoformat("2025-06-01T12:00:00+00:00").timestamp()
USER_ZONE_OFFSET_HOURS = -2
REMOVED_LOCAL_YEAR = 2023
EARLIER_YEAR = 2022
LATER_YEAR = 2025
PRIMARY_OFFSET_SECONDS = 3
PLAY_DURATION_MS = 180_000
EXPECTED_DUPLICATE_PLAY_COUNT = 2
EXPECTED_GENERATION_INCREASE = 1
SNAPSHOT_TABLES = ("plays", "user_wrapped", "app_settings")


class TestBackfillWrappedInvalidation(ScopeTestCase):
    def _db(self):
        db = self._makeDb({}, [], username="alice")
        db.tz = datetime.timezone(datetime.timedelta(hours=USER_ZONE_OFFSET_HOURS))
        db.saveImagesFromTrack = MagicMock()
        db.updatePlaylists = MagicMock()
        return db

    def _seedPair(self, db, timestamp=API_TIMESTAMP):
        track = rawSpotifyTrackForTest("duplicate")
        track["duration_ms"] = PLAY_DURATION_MS
        conn = db.repo.connection()
        with conn:
            db.repo.upsertTrack(Client.formatTrack(track, embedPlaybackInfo=False))
            for playedAt, source in ((timestamp, db.WEB_API_BACKFILL_SOURCE),
                                     (timestamp + PRIMARY_OFFSET_SECONDS, "listener_play")):
                conn.execute(
                    "INSERT INTO plays (username, track_id, played_at, time_played, created_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (db.user, track["id"], playedAt, PLAY_DURATION_MS, source))
        return {"track": track, "played_at": timestamp}

    @staticmethod
    def _snapshot(conn):
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in SNAPSHOT_TABLES}

    def test_cleanup_drops_own_local_year_and_later_years_once(self):
        db = self._db()
        page = [self._seedPair(db), self._seedPair(db, LATER_API_TIMESTAMP)]
        self._cacheYears(db, db.user, EARLIER_YEAR, REMOVED_LOCAL_YEAR, LATER_YEAR)
        self._cacheYears(db, "bob", REMOVED_LOCAL_YEAR, LATER_YEAR)
        generation = db.repo.getWrappedInvalidationGeneration()

        db._reconcileWithWebApiHistory(page)

        self.assertEqual(self._survivingYears(db),
                         {(db.user, EARLIER_YEAR), ("bob", REMOVED_LOCAL_YEAR), ("bob", LATER_YEAR)})
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(),
                         generation + EXPECTED_GENERATION_INCREASE)
        self.assertEqual([row[0] for row in db.repo.connection().execute(
            "SELECT played_at FROM plays WHERE username=? ORDER BY played_at", (db.user,))],
            [API_TIMESTAMP + PRIMARY_OFFSET_SECONDS, LATER_API_TIMESTAMP + PRIMARY_OFFSET_SECONDS])
        self.assertFalse(db.repo.connection().in_transaction)

    def test_empty_cache_cleanup_rejects_a_calculation_started_before_deletion(self):
        db = self._db()
        page = [self._seedPair(db)]
        generation = db.repo.getWrappedInvalidationGeneration()
        staleData = wrappedCachedRow(totalPlays=EXPECTED_DUPLICATE_PLAY_COUNT,
                                     totalMs=PLAY_DURATION_MS * EXPECTED_DUPLICATE_PLAY_COUNT)
        staleData.update(calculated_at=API_TIMESTAMP, max_played_at=API_TIMESTAMP + PRIMARY_OFFSET_SECONDS)

        db._reconcileWithWebApiHistory(page)

        self.assertFalse(db.repo.saveCachedWrapped(
            db.user, REMOVED_LOCAL_YEAR, staleData, expectedGeneration=generation))
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(),
                         generation + EXPECTED_GENERATION_INCREASE)
        self.assertEqual(self._survivingYears(db), set())

    def test_delete_cache_and_generation_become_visible_together(self):
        db = self._db()
        page = [self._seedPair(db)]
        self._cacheYears(db, db.user, REMOVED_LOCAL_YEAR, LATER_YEAR)
        generation = db.repo.getWrappedInvalidationGeneration()
        conn = db.repo.connection()
        dbPath = conn.execute("PRAGMA database_list").fetchone()["file"]
        originalCommit = db.repo.commit
        with closing(sqlite3.connect(dbPath)) as observer:
            before = self._snapshot(observer)

            def commit():
                self.assertTrue(conn.in_transaction)
                self.assertEqual(self._survivingYears(db), set())
                self.assertEqual(db.repo.getWrappedInvalidationGeneration(),
                                 generation + EXPECTED_GENERATION_INCREASE)
                self.assertEqual(self._snapshot(observer), before)
                originalCommit()

            with patch.object(db.repo, "commit", side_effect=commit) as committed:
                db._reconcileWithWebApiHistory(page)
            committed.assert_called_once()
            self.assertEqual(self._snapshot(observer), self._snapshot(conn))
            self.assertNotEqual(self._snapshot(observer), before)
        self.assertFalse(conn.in_transaction)

    def test_every_cleanup_failure_rolls_back_plays_cache_and_generation(self):
        for methodName in ("deletePlay", "_bumpWrappedGeneration", "_deleteUserWrappedFromYear", "commit"):
            with self.subTest(method=methodName):
                db = self._db()
                page = [self._seedPair(db)]
                self._cacheYears(db, db.user, REMOVED_LOCAL_YEAR, LATER_YEAR)
                before = self._snapshot(db.repo.connection())
                original = getattr(db.repo, methodName)

                def failAfterWrite(*args, _original=original, **kwargs):
                    if methodName != "commit":
                        _original(*args, **kwargs)
                    raise sqlite3.OperationalError("synthetic cleanup failure")

                with patch.object(db.repo, methodName, side_effect=failAfterWrite):
                    db._reconcileWithWebApiHistory(page)
                self.assertEqual(self._snapshot(db.repo.connection()), before)
                self.assertFalse(db.repo.connection().in_transaction)
                db._reconcileWithWebApiHistory(page)
                self.assertEqual(self._survivingYears(db), set())

    def test_noop_delete_preserves_cache_and_generation(self):
        db = self._db()
        page = [self._seedPair(db)]
        self._cacheYears(db, db.user, REMOVED_LOCAL_YEAR, LATER_YEAR)
        before = self._snapshot(db.repo.connection())
        with patch.object(db.repo, "deletePlay", return_value=False):
            db._reconcileWithWebApiHistory(page)
        self.assertEqual(self._snapshot(db.repo.connection()), before)
        self.assertFalse(db.repo.connection().in_transaction)
