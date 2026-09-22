# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded repair invalidation preserves unrelated cached history atomically."""

import sqlite3
from unittest.mock import patch

from Database.metadata_repair import TrackRepairImpact
import Database.queries.wrapped as wrappedQueries
from test_wrapped_invalidation_scope import ScopeTestCase, _ts


REPAIR_YEAR = 2024
UNRELATED_YEAR = 2018
TRACK_LIMIT = 128
PLAY_LIMIT = 4096
PLAY_DURATION_MS = 180_000


class RepairPolicyCase(ScopeTestCase):
    def _seedTrack(self, db, trackId, albumId=None, artistId=None, canonicalId=None):
        albumId = albumId or f"album-{trackId}"
        artistId = artistId or f"artist-{trackId}"
        conn = db.repo.connection()
        conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES (?, ?, '')",
                     (albumId, albumId))
        conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms, canonical_id) "
                     "VALUES (?, ?, '', ?, ?, ?)",
                     (trackId, trackId, albumId, PLAY_DURATION_MS, canonicalId))
        conn.execute("INSERT OR IGNORE INTO artists (id, name, url) VALUES (?, ?, '')",
                     (artistId, artistId))
        conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, ?, 0)",
                     (trackId, artistId))

    @staticmethod
    def _impact(trackId="focus", oldAlbum="old-album", newAlbum="new-album",
                oldArtist="old-artist", newArtist="new-artist"):
        return TrackRepairImpact(trackId, oldAlbum, newAlbum,
                                 frozenset({oldArtist}), frozenset({newArtist}))

    @staticmethod
    def _apply(db, impacts, historyScopes=()):
        conn = db.repo.connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return db.repo._invalidateWrappedForRepairs(conn, impacts, historyScopes)


class TestWrappedRepairPolicy(RepairPolicyCase):
    def test_old_new_final_memberships_and_merge_siblings_all_invalidate(self):
        db = self._db()
        self._seedTrack(db, "focus", "final-album", "final-artist")
        dependencies = (
            ("old-album-member", "old-album", None),
            ("new-album-member", "new-album", None),
            ("old-artist-member", None, "old-artist"),
            ("new-artist-member", None, "new-artist"),
            ("final-album-member", "final-album", None),
            ("final-artist-member", None, "final-artist"),
        )
        for trackId, album, artist in dependencies:
            self._seedTrack(db, trackId, album, artist)
        self._seedTrack(db, "merge-sibling", canonicalId="old-album-member")
        self._seedTrack(db, "unrelated")
        db.repo.commit()
        affected = ["focus", *(item[0] for item in dependencies), "merge-sibling"]
        for index, trackId in enumerate(affected):
            username = f"user-{index}"
            self._plays(db, username, trackId, _ts(REPAIR_YEAR))
            self._cacheYears(db, username, UNRELATED_YEAR, REPAIR_YEAR)
        self._plays(db, "unrelated", "unrelated", _ts(REPAIR_YEAR))
        self._cacheYears(db, "unrelated", REPAIR_YEAR)
        generation = db.repo.getWrappedInvalidationGeneration()

        result = self._apply(db, [self._impact(), self._impact()])

        self.assertEqual(result.repaired, 1)
        self.assertEqual((result.mode, result.reason), ("targeted", None))
        self.assertEqual((result.repairDeleted, result.historyDeleted), (len(affected), 0))
        self.assertEqual(self._survivingYears(db),
                         {(f"user-{index}", UNRELATED_YEAR) for index in range(len(affected))}
                         | {("unrelated", REPAIR_YEAR)})
        self.assertEqual(db.repo.getCachedWrappedCalculatedAt("unrelated", REPAIR_YEAR), 1)
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), generation + 1)

    def test_track_guard_boundaries_choose_exactly_one_invalidator(self):
        self.assertEqual(wrappedQueries.WRAPPED_REPAIR_MAX_EXPANDED_TRACKS, TRACK_LIMIT)
        for size in (TRACK_LIMIT - 1, TRACK_LIMIT, TRACK_LIMIT + 1):
            with self.subTest(size=size):
                db = self._db()
                for index in range(size):
                    self._seedTrack(db, "focus" if index == 0 else f"member-{index}", "shared")
                db.repo.commit()
                self._plays(db, "alice", "focus", _ts(REPAIR_YEAR))
                self._cacheYears(db, "alice", UNRELATED_YEAR, REPAIR_YEAR)
                with patch.object(db.repo, "_deleteAllWrapped", wraps=db.repo._deleteAllWrapped) as broad, \
                     patch.object(db.repo, "_deleteCachedWrappedForTracks",
                                  wraps=db.repo._deleteCachedWrappedForTracks) as targeted:
                    result = self._apply(db, [self._impact(oldAlbum="shared", newAlbum="shared")])
                self.assertEqual(broad.call_count, int(size > TRACK_LIMIT))
                self.assertEqual(targeted.call_count, int(size <= TRACK_LIMIT))
                self.assertEqual((result.mode, result.reason),
                                 ("broad", "expanded_tracks") if size > TRACK_LIMIT else ("targeted", None))
                self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)
                self.assertEqual(self._survivingYears(db),
                                 set() if size > TRACK_LIMIT else {("alice", UNRELATED_YEAR)})

    def test_play_guard_boundaries_are_bounded_indexed_and_ignore_uncached_users(self):
        self.assertEqual(wrappedQueries.WRAPPED_REPAIR_MAX_MATCHED_PLAYS, PLAY_LIMIT)
        for count in (PLAY_LIMIT - 1, PLAY_LIMIT, PLAY_LIMIT + 1):
            with self.subTest(count=count):
                db = self._db()
                self._seedTrack(db, "focus")
                db.repo.commit()
                self._cacheYears(db, "alice", UNRELATED_YEAR, REPAIR_YEAR)
                self._user(db, "uncached")
                conn = db.repo.connection()
                with conn:
                    conn.executemany("INSERT INTO plays (username,track_id,played_at,time_played) "
                                     "VALUES ('alice','focus',?,?)",
                                     [(_ts(REPAIR_YEAR) + index, PLAY_DURATION_MS) for index in range(count)])
                    conn.executemany("INSERT INTO plays (username,track_id,played_at,time_played) "
                                     "VALUES ('uncached','focus',?,?)",
                                     [(_ts(REPAIR_YEAR) + index, PLAY_DURATION_MS)
                                      for index in range(PLAY_LIMIT + 1)])
                statements = []
                conn.set_trace_callback(statements.append)
                try:
                    result = self._apply(db, [self._impact()])
                finally:
                    conn.set_trace_callback(None)
                countQueries = [sql for sql in statements if "COUNT(*)" in sql.upper()
                                and "FROM PLAYS" in sql.upper()]
                self.assertEqual(len(countQueries), 1)
                self.assertIn(f"LIMIT {PLAY_LIMIT + 1}", countQueries[0].upper())
                queryPlan = " ".join(str(tuple(row)) for row in
                                     conn.execute("EXPLAIN QUERY PLAN " + countQueries[0]))
                self.assertIn("idx_plays_user_track", queryPlan)
                self.assertEqual((result.mode, result.reason),
                                 ("broad", "matched_plays") if count > PLAY_LIMIT else ("targeted", None))
                self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)

    def test_each_expansion_stops_at_guard_and_skips_later_work(self):
        for dimension in ("album", "artist", "merge"):
            with self.subTest(dimension=dimension):
                db = self._db()
                self._seedTrack(db, "focus", "shared-album", "shared-artist")
                for index in range(TRACK_LIMIT + 1):
                    self._seedTrack(db, f"member-{index}",
                                    "shared-album" if dimension == "album" else None,
                                    "shared-artist" if dimension == "artist" else None,
                                    "focus" if dimension == "merge" else None)
                db.repo.commit()
                self._cacheYears(db, "unrelated", UNRELATED_YEAR)
                statements = []
                db.repo.connection().set_trace_callback(statements.append)
                try:
                    result = self._apply(db, [self._impact(newAlbum="shared-album", newArtist="shared-artist")])
                finally:
                    db.repo.connection().set_trace_callback(None)
                self.assertEqual(result.reason, "expanded_tracks")
                self.assertFalse(any("COUNT(*)" in sql.upper() and "FROM PLAYS" in sql.upper()
                                     for sql in statements))
                expansions = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
                              and ("WHERE ALBUM_ID" in sql.upper() or "WHERE ARTIST_ID" in sql.upper()
                                   or "WHERE CANONICAL_ID" in sql.upper())]
                self.assertTrue(expansions)
                self.assertTrue(all(f"LIMIT {TRACK_LIMIT + 1}" in sql.upper() for sql in expansions))

    def test_history_scopes_compose_without_second_generation_bump(self):
        for broad in (False, True):
            with self.subTest(broad=broad):
                db = self._db()
                self._seedTrack(db, "focus")
                db.repo.commit()
                self._plays(db, "alice", "focus", _ts(REPAIR_YEAR))
                self._cacheYears(db, "alice", UNRELATED_YEAR, REPAIR_YEAR)
                self._cacheYears(db, "bob", UNRELATED_YEAR, REPAIR_YEAR)
                limit = 0 if broad else TRACK_LIMIT
                with patch.object(wrappedQueries, "WRAPPED_REPAIR_MAX_EXPANDED_TRACKS", limit):
                    result = self._apply(db, [self._impact()], (("bob", REPAIR_YEAR), ("bob", REPAIR_YEAR)))
                self.assertEqual(result.historyDeleted, 0 if broad else 1)
                self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)
                self.assertEqual(self._survivingYears(db), set() if broad else
                                 {("alice", UNRELATED_YEAR), ("bob", UNRELATED_YEAR)})

    def test_empty_cache_still_advances_generation_and_rejects_stale_save(self):
        db = self._db()
        self._seedTrack(db, "focus")
        db.repo.commit()
        result = self._apply(db, [self._impact()])
        self.assertEqual((result.repairDeleted, result.historyDeleted), (0, 0))
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 1)
        self.assertFalse(db.repo.saveCachedWrapped(db.user, REPAIR_YEAR, {}, expectedGeneration=0))

    def test_failure_in_history_delete_rolls_back_repair_delete_and_generation(self):
        db = self._db()
        self._seedTrack(db, "focus")
        db.repo.commit()
        self._plays(db, "alice", "focus", _ts(REPAIR_YEAR))
        self._cacheYears(db, "alice", REPAIR_YEAR)
        self._cacheYears(db, "bob", REPAIR_YEAR)
        with patch.object(db.repo, "_deleteUserWrappedFromYear", side_effect=sqlite3.OperationalError("history failed")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "history failed"):
                self._apply(db, [self._impact()], (("bob", REPAIR_YEAR),))
        self.assertEqual(self._survivingYears(db), {("alice", REPAIR_YEAR), ("bob", REPAIR_YEAR)})
        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), 0)

    def test_bounded_merge_expansion_preserves_existing_unbounded_contract(self):
        db = self._db()
        self._seedTrack(db, "root")
        for index in range(TRACK_LIMIT + 1):
            self._seedTrack(db, f"member-{index}", canonicalId="root")
        db.repo.commit()
        conn = db.repo.connection()
        self.assertEqual(len(db.repo._mergeGroupTrackIds(conn, ["member-0"])), TRACK_LIMIT + 2)
        self.assertEqual(len(db.repo._mergeGroupTrackIds(conn, ["member-0"], limit=TRACK_LIMIT + 1)),
                         TRACK_LIMIT + 1)
