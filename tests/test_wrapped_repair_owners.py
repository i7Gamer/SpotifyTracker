# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Each ingestion owner commits catalog repair and cache invalidation together."""

import datetime
import json
import sqlite3
from unittest.mock import patch

from conftest import RecordingConnection, normalizeTrackForTest, rawSpotifyTrackForTest
from Database.database import Database, _ImportRunState
from Database.db import RESTRICTED_FALLBACK_REASON
import Database.queries.wrapped as wrappedQueries
from test_wrapped_repair_policy import RepairPolicyCase, REPAIR_YEAR, UNRELATED_YEAR, PLAY_DURATION_MS, _ts


SNAPSHOT_TABLES = ("tracks", "albums", "artists", "track_artists", "plays", "app_settings", "user_wrapped")
OWNERS = ("live", "append", "overwrite")


class TestWrappedRepairOwners(RepairPolicyCase):
    def _case(self):
        db = self._db()
        self._seedTrack(db, "focus", "old-album", "old-artist")
        self._seedTrack(db, "unrelated")
        db.repo.connection().execute("UPDATE tracks SET created_reason=? WHERE id='focus'",
                                     (RESTRICTED_FALLBACK_REASON,))
        db.repo.commit()
        self._plays(db, "bob", "focus", _ts(REPAIR_YEAR))
        self._plays(db, db.user, "unrelated", _ts(UNRELATED_YEAR))
        self._cacheYears(db, "bob", REPAIR_YEAR)
        self._cacheYears(db, db.user, UNRELATED_YEAR, REPAIR_YEAR)
        return db

    @staticmethod
    def _real(trackId="focus", album="new-album", artist="new-artist"):
        return normalizeTrackForTest({"id": trackId, "name": "Recovered song", "duration": PLAY_DURATION_MS,
                                      "imageId": album, "artists": [{"id": artist, "name": "Recovered artist"}]})

    @staticmethod
    def _entry(trackId="focus", year=REPAIR_YEAR):
        return {"id": trackId, "playedAt": _ts(year), "timePlayed": PLAY_DURATION_MS, "playedFrom": None}

    def _applyOwner(self, db, owner, state=None, tracks=None, entries=None):
        state = state or _ImportRunState()
        tracks = tracks if tracks is not None else {"focus": self._real()}
        entries = entries if entries is not None else [self._entry()]
        if owner == "live":
            with patch.object(db, "saveImagesFromTrack"), patch.object(db, "updatePlaylists"):
                db.appendMetadata({**tracks["focus"], **entries[0]})
        elif owner == "append":
            db._applyImportData(tracks, entries, {}, len(entries), "synthetic-content", "", True, False,
                                True, state, False, lambda *args, **kwargs: None)
        else:
            staged = [((tracks, entries, len(entries), {}), "synthetic-content", "", True)]
            success = db._applyStagedBatch(staged, state, None, None, set(), len(staged))
            self.assertTrue(success)
        return state

    @staticmethod
    def _snapshot(db):
        conn = sqlite3.connect(db.repo.connectionManager.dbPath)
        try:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                    for table in SNAPSHOT_TABLES}
        finally:
            conn.close()

    def test_all_ingestion_owners_invalidate_shared_repairs_and_preserve_unrelated_cache(self):
        for owner in OWNERS:
            with self.subTest(owner=owner):
                db = self._case()
                with self.assertLogs("Database", level="INFO") as logs:
                    state = self._applyOwner(db, owner)
                self.assertIsNone(db.repo.getCachedWrapped("bob", REPAIR_YEAR))
                self.assertIsNotNone(db.repo.getCachedWrapped(db.user, UNRELATED_YEAR))
                self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)
                repairLogs = [line for line in logs.output if "Metadata repair committed" in line]
                self.assertEqual(len(repairLogs), 1)
                self.assertIn(f"source={owner}", repairLogs[0])
                self.assertIn("repaired=1", repairLogs[0])
                self.assertIn("mode=targeted", repairLogs[0])
                if owner != "live":
                    self.assertEqual(state.pendingRepairImpacts, [])
                    self.assertEqual(state.committedRepairResult.repaired, 1)

    def test_old_state_reads_hold_the_owner_write_reservation(self):
        for owner in OWNERS:
            with self.subTest(owner=owner):
                db = self._case()
                statements = []
                recording = RecordingConnection(db.repo.connection(), statements)
                with patch.object(db.repo, "_conn", return_value=recording):
                    self._applyOwner(db, owner)
                oldReads = [(sql, locked) for sql, locked in statements
                            if sql.startswith("SELECT") and "created_reason" in sql and "FROM tracks WHERE id" in sql]
                self.assertEqual(len(oldReads), 1)
                self.assertTrue(all(locked for _, locked in oldReads))

    def test_each_owner_rebuilds_another_users_real_wrapped_metadata(self):
        for owner in OWNERS:
            with self.subTest(owner=owner):
                db = self._case()
                bob = Database("bob", dbPath=db.repo.connectionManager.dbPath, startWorkers=False)
                self.addCleanup(bob.repo.connectionManager.close)
                bob.recalculateWrappedForYear(REPAIR_YEAR)
                before = bob.repo.getCachedWrapped("bob", REPAIR_YEAR)
                playsBefore = [dict(row) for row in bob.repo.connection().execute(
                    "SELECT * FROM plays WHERE username='bob'")]
                self._applyOwner(db, owner)
                self.assertIsNone(bob.repo.getCachedWrapped("bob", REPAIR_YEAR))
                bob.recalculateWrappedForYear(REPAIR_YEAR)
                after = bob.repo.getCachedWrapped("bob", REPAIR_YEAR)
                self.assertEqual(json.loads(after["top_songs"])[0]["name"], "Recovered song")
                self.assertEqual(json.loads(after["top_artists"])[0]["name"], "Recovered artist")
                self.assertEqual(after["total_plays"], before["total_plays"])
                self.assertEqual(after["max_played_at"], before["max_played_at"])
                self.assertEqual([dict(row) for row in bob.repo.connection().execute(
                    "SELECT * FROM plays WHERE username='bob'")], playsBefore)

    def test_append_combines_unrelated_history_with_repair_before_commit(self):
        db = self._case()
        tracks = {"focus": self._real(), "new-history": self._real("new-history", "separate", "separate")}
        with patch.object(db.repo, "deleteUserWrappedFromYear", side_effect=AssertionError("post-commit delete")):
            state = self._applyOwner(db, "append", tracks=tracks,
                                     entries=[self._entry("new-history", UNRELATED_YEAR)])
        self.assertEqual(self._survivingYears(db), set())
        self.assertEqual((state.committedRepairResult.repairDeleted, state.committedRepairResult.historyDeleted), (1, 2))
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)

    def test_repair_only_append_does_not_invent_a_history_scope(self):
        db = self._case()
        state = self._applyOwner(db, "append", entries=[])
        self.assertEqual(self._survivingYears(db), {(db.user, UNRELATED_YEAR), (db.user, REPAIR_YEAR)})
        self.assertEqual(state.committedRepairResult.historyDeleted, 0)

    def test_overwrite_includes_removed_only_years_and_final_membership_across_files(self):
        db = self._case()
        self._seedTrack(db, "final-member", "final-album", "final-artist")
        db.repo.commit()
        self._plays(db, "carol", "final-member", _ts(REPAIR_YEAR))
        self._cacheYears(db, "carol", REPAIR_YEAR)
        state = _ImportRunState()
        staged = [(({"focus": self._real()}, [], 0, {}), "one", "", False),
                  (({"focus": self._real(album="final-album", artist="final-artist")}, [], 0, {}), "two", "", True)]
        with patch.object(db.repo, "deleteUserWrappedFromYear", side_effect=AssertionError("post-commit delete")):
            self.assertTrue(db._applyStagedBatch(staged, state, _ts(UNRELATED_YEAR), _ts(UNRELATED_YEAR),
                                                {UNRELATED_YEAR}, len(staged)))
        self.assertEqual(self._survivingYears(db), set())
        self.assertEqual((state.committedRepairResult.repaired, state.committedRepairResult.repairDeleted,
                          state.committedRepairResult.historyDeleted), (1, 2, 2))
        self.assertEqual(state.pendingRepairImpacts, [])
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)

    def test_empty_replacement_file_composes_timezone_history_with_another_files_repair(self):
        db = self._case()
        db.tz = datetime.timezone(datetime.timedelta(hours=-2))
        boundary = datetime.datetime(REPAIR_YEAR, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
        self._plays(db, db.user, "unrelated", boundary)
        self._cacheYears(db, db.user, REPAIR_YEAR - 1)
        staged = [(None, "empty-replacement", "", False),
                  (({"focus": self._real()}, [], 0, {}), "repair-only", "", True)]
        state = _ImportRunState()
        self.assertTrue(db._applyStagedBatch(staged, state, boundary, boundary, {REPAIR_YEAR}, len(staged)))
        self.assertIsNone(db.repo.getCachedWrapped(db.user, REPAIR_YEAR - 1))
        self.assertIsNotNone(db.repo.getCachedWrapped(db.user, UNRELATED_YEAR))
        self.assertEqual(state.committedRepairResult.historyDeleted, 2)
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)

    def test_policy_and_commit_failures_roll_back_catalog_plays_cache_generation_and_state(self):
        failures = ("_expandWrappedRepairTracks", "_countWrappedRepairPlays", "_deleteCachedWrappedForTracks",
                    "_deleteAllWrapped", "_deleteUserWrappedFromYear", "commit")
        for owner in OWNERS:
            for method in failures:
                if owner == "live" and method == "_deleteUserWrappedFromYear":
                    continue
                with self.subTest(owner=owner, method=method):
                    db = self._case()
                    state = _ImportRunState()
                    before = self._snapshot(db)
                    limit = 0 if method == "_deleteAllWrapped" else wrappedQueries.WRAPPED_REPAIR_MAX_EXPANDED_TRACKS
                    with patch.object(wrappedQueries, "WRAPPED_REPAIR_MAX_EXPANDED_TRACKS", limit), \
                         patch.object(db.repo, method, side_effect=sqlite3.OperationalError("synthetic failure")), \
                         patch("Database.database.logger.info") as log:
                        if owner == "overwrite":
                            staged = [(({"focus": self._real()}, [self._entry()], 1, {}), "one", "", True)]
                            self.assertFalse(db._applyStagedBatch(staged, state, _ts(UNRELATED_YEAR), _ts(UNRELATED_YEAR),
                                                                 {UNRELATED_YEAR}, len(staged)))
                        else:
                            with self.assertRaisesRegex(sqlite3.OperationalError, "synthetic failure"):
                                self._applyOwner(db, owner, state)
                        self.assertFalse(any("Metadata repair committed" in str(call) for call in log.call_args_list))
                    self.assertEqual(self._snapshot(db), before)
                    self.assertFalse(db.repo.connection().in_transaction)
                    self.assertEqual(state.pendingRepairImpacts, [])
                    self.assertIsNone(state.committedRepairResult)

    def test_later_append_cannot_replay_previous_repair_or_skip_history_invalidation(self):
        db = self._case()
        state = self._applyOwner(db, "append", entries=[])
        with patch("Database.database.logger.info") as log:
            self._applyOwner(db, "append", state, tracks={"next": self._real("next", "next", "next")},
                             entries=[self._entry("next", UNRELATED_YEAR)])
        self.assertIsNone(state.committedRepairResult)
        self.assertEqual(state.pendingRepairImpacts, [])
        self.assertEqual(self._survivingYears(db), set())
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 2)
        self.assertFalse(any("Metadata repair committed" in str(call) for call in log.call_args_list))

    def test_post_commit_reporting_failure_does_not_rollback_or_restore_pending_state(self):
        db = self._case()
        state = _ImportRunState()
        with patch.object(db, "_logCommittedMetadataRepair", side_effect=RuntimeError("report failed")), \
             patch.object(db.repo, "rollbackQuietly", wraps=db.repo.rollbackQuietly) as rollback:
            with self.assertRaisesRegex(RuntimeError, "report failed"):
                self._applyOwner(db, "append", state)
        rollback.assert_not_called()
        self.assertEqual(state.pendingRepairImpacts, [])
        self.assertEqual(state.committedRepairResult.repaired, 1)
        self.assertIsNone(db.repo.getCachedWrapped("bob", REPAIR_YEAR))
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)

    def test_dedicated_worker_keeps_integer_result_and_logs_source_mode_and_zero_deletions(self):
        for source in ("catalog", "history"):
            for broad in (False, True):
                with self.subTest(source=source, broad=broad):
                    db = self._case()
                    with db.repo.connection() as conn:
                        conn.execute("DELETE FROM user_wrapped")
                    limit = 0 if broad else wrappedQueries.WRAPPED_REPAIR_MAX_EXPANDED_TRACKS
                    with patch.object(wrappedQueries, "WRAPPED_REPAIR_MAX_EXPANDED_TRACKS", limit), \
                         self.assertLogs("Database", level="INFO") as logs:
                        repaired = db._repairFallbackTrackMetadata([rawSpotifyTrackForTest("focus")], source=source)
                    self.assertEqual(repaired, 1)
                    repairLogs = [line for line in logs.output if "Metadata repair committed" in line]
                    self.assertEqual(len(repairLogs), 1)
                    for field in (f"source={source}", "repaired=1", "repair_deleted=0", "history_deleted=0",
                                  "mode=broad" if broad else "mode=targeted",
                                  "reason=expanded_tracks" if broad else "reason=within_limits"):
                        self.assertIn(field, repairLogs[0])
                    with self.assertNoLogs("Database", level="INFO"):
                        self.assertEqual(db._repairFallbackTrackMetadata([rawSpotifyTrackForTest("focus")], source=source), 0)

    def test_no_repair_owners_do_not_run_policy_queries_or_emit_repair_logs(self):
        for owner in OWNERS:
            with self.subTest(owner=owner):
                db = self._case()
                with db.repo.connection() as conn:
                    conn.execute("UPDATE tracks SET created_reason=NULL WHERE id='focus'")
                with patch.object(db.repo, "_expandWrappedRepairTracks", side_effect=AssertionError("repair scope")), \
                     patch.object(db.repo, "_countWrappedRepairPlays", side_effect=AssertionError("repair count")), \
                     patch("Database.database.logger.info") as log:
                    self._applyOwner(db, owner)
                self.assertFalse(any("Metadata repair committed" in str(call) for call in log.call_args_list))

    def test_dedicated_commit_failure_rolls_back_cache_catalog_and_generation_without_success_log(self):
        db = self._case()
        conn = db.repo.connection()
        with conn:
            conn.execute("CREATE TABLE repair_commit_guard (username TEXT REFERENCES users(username) "
                         "DEFERRABLE INITIALLY DEFERRED)")
            conn.execute("CREATE TRIGGER fail_repair_commit AFTER UPDATE ON tracks WHEN NEW.id='focus' "
                         "BEGIN INSERT INTO repair_commit_guard VALUES ('missing-user'); END")
        before = self._snapshot(db)
        with self.assertNoLogs("Database", level="INFO"), self.assertRaises(sqlite3.IntegrityError):
            db._repairFallbackTrackMetadata([rawSpotifyTrackForTest("focus")])
        self.assertEqual(self._snapshot(db), before)
        self.assertFalse(conn.in_transaction)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM repair_commit_guard").fetchone()[0], 0)
