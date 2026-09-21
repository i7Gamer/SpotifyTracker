"""The admin checkbox that turns track merging on and off.

The toggle gates the DATA, not the queries: switching on runs the ISRC matcher,
switching off unmerges everything the matcher did. Read paths honour
canonical_id unconditionally - safe precisely because it is only ever set while
the feature is on - and that is what makes the off position a genuine, instant,
lossless undo rather than a display filter.

New tracks are the reason it is a checkbox and not a button: the ISRC
backfiller keeps stamping new arrivals, so merge candidates appear for as long
as anyone listens to new music, and the backfiller re-runs the matcher - once a
day, see test_track_merge_cadence - while the toggle is on.
"""
import os
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.database import Database

ISRC = "USRC12345678"
#< _dbWithPair times every play at 1e9-ish epoch seconds, which is Sept 2001
PAIR_PLAY_YEAR = 2001
UNTOUCHED_YEAR = 2015


class ToggleTestCase(DatabaseTestCase):
    def _dbWithPair(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'A', '')")
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            for trackId, plays in (("A" * 22, 5), ("B" * 22, 2)):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc) "
                             "VALUES (?, 'Song', '', 'alb1', ?)", (trackId, ISRC))
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES ('alice', ?, ?, 200000)", (trackId, 1e9 + i))
        return db

    def _canonical(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()[0]


class TestTheSettingItself(ToggleTestCase):
    def test_it_defaults_off(self):
        """A merge moves every account's numbers at once. It has to be a
        decision someone made, never a default they inherited - the only other
        toggle with this default is the push listener, for the same reason."""
        db = self._makeDb({}, [])

        self.assertFalse(db.repo.isTrackMergeEnabled())

    def test_it_round_trips(self):
        db = self._makeDb({}, [])
        db.repo.setTrackMergeEnabled(True)
        self.assertTrue(db.repo.isTrackMergeEnabled())
        db.repo.setTrackMergeEnabled(False)
        self.assertFalse(db.repo.isTrackMergeEnabled())

    def test_enable_setting_failure_rolls_back_the_merge(self):
        db = self._dbWithPair()
        conn = db.repo._conn()
        with conn:
            conn.execute(
                "CREATE TRIGGER reject_track_merge_enable BEFORE INSERT ON app_settings "
                "WHEN NEW.key = 'track_merge_enabled' BEGIN "
                "SELECT RAISE(ABORT, 'setting failed'); END"
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "setting failed"):
            db.repo.mergeTracksByIsrc(enableSetting=True)

        self.assertFalse(db.repo.isTrackMergeEnabled())
        self.assertIsNone(self._canonical(db, "A" * 22))
        self.assertIsNone(self._canonical(db, "B" * 22))
        self.assertIsNone(db.repo.getTrackMergeLastRun())


class TestTheBackfillerKeepsMergesCurrent(ToggleTestCase):
    """The "new tracks" half of the question: while the toggle is on, a pair
    completed by a newly-arrived ISRC folds without anyone pressing anything.

    WHEN it folds is test_track_merge_cadence's question - the loop now takes
    one matcher slot a day rather than one per cycle, so the wait is up to a
    day. This covers the mechanism: given a run, the new arrival merges."""

    def _runOneIsrcBatch(self, db):
        """Drive the real backfill step with a mocked per-id endpoint."""
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {"external_ids": {"isrc": ISRC}}
        stop = MagicMock(is_set=MagicMock(return_value=False))
        with patch("requests.get", return_value=response):
            db._backfillTrackIsrcs(lambda: "mock_token", stop)

    def _dbReadyForLoop(self):
        from test_metadata_backfiller import runsOneCycle

        db = self._makeDb({}, [])
        db.getUserSpotifyCredentials = MagicMock(return_value=None)
        db._backfillTrackIsrcs = MagicMock()
        db.backfiller_stop_event = MagicMock()
        runsOneCycle(db, db.backfiller_stop_event)
        return db

    def test_a_batch_that_completes_a_pair_merges_it(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'A', '')")
            #< one track already stamped, its pair still missing the ISRC -
            #  exactly the state a new arrival is in until the backfiller runs
            conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc) "
                         "VALUES (?, 'Song', '', 'alb1', ?)", ("A" * 22, ISRC))
            conn.execute("INSERT INTO tracks (id, name, url, album_id) "
                         "VALUES (?, 'Song', '', 'alb1')", ("B" * 22,))
        db.repo.setTrackMergeEnabled(True)

        self._runOneIsrcBatch(db)
        #< what the loop does after a batch that recorded ISRCs
        if db.repo.isTrackMergeEnabled():
            db.repo.mergeTracksByIsrc()

        self.assertIsNotNone(self._canonical(db, "B" * 22) or self._canonical(db, "A" * 22))

    def test_the_loop_skips_the_claim_and_matcher_while_off(self):
        """A disabled feature costs nothing: no claim, no matcher run."""
        db = self._dbReadyForLoop()
        db.repo.isTrackMergeEnabled = MagicMock(return_value=False)
        db.repo.claimTrackMergeRun = MagicMock(return_value=True)
        db.repo.mergeTracksByIsrc = MagicMock(return_value={"groups": 0, "merged": 0})

        db._metadataBackfillLoop()

        db.repo.isTrackMergeEnabled.assert_called_once()
        db.repo.claimTrackMergeRun.assert_not_called()
        db.repo.mergeTracksByIsrc.assert_not_called()


class TestReversibilityAtTheToggle(ToggleTestCase):
    """The property the whole design was chosen for: OFF undoes everything the
    matcher did, instantly and losslessly, whenever it is pressed."""

    def test_on_then_off_is_a_round_trip(self):
        db = self._dbWithPair()

        db.repo.setTrackMergeEnabled(True)
        db.repo.mergeTracksByIsrc()
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

        db.repo.setTrackMergeEnabled(False)
        undone = db.repo.unmergeAllIsrcMerges()

        self.assertEqual(undone, 1)
        self.assertIsNone(self._canonical(db, "A" * 22))
        self.assertIsNone(self._canonical(db, "B" * 22))

    def test_off_keeps_manual_verdicts_for_the_next_on(self):
        """The one thing a full undo must NOT undo: a person's "not the same
        recording". Toggle off, toggle on - the matcher still honours it."""
        db = self._dbWithPair()
        db.repo.setTrackMergeEnabled(True)
        db.repo.mergeTracksByIsrc()
        db.repo.unmergeTrack("B" * 22, decidedBy="timorzipa")

        db.repo.setTrackMergeEnabled(False)
        db.repo.unmergeAllIsrcMerges()
        db.repo.setTrackMergeEnabled(True)
        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._canonical(db, "B" * 22))


class TestWrappedInvalidation(ToggleTestCase):
    """Phase 7. A Wrapped year is a materialised snapshot - top_songs,
    unique_songs and the discovery lists frozen as JSON - and a merge changes
    what all of them would say without touching the two freshness signals the
    worker compares (max_played_at, play count). Past years would stay wrong
    forever unless the merge itself invalidates them.

    Inside the repo methods rather than at any call site, so the admin toggle,
    the backfiller's incremental pass and any future per-track split all get it
    for free. Rebuild is lazy: the page recalculates a missing year on view and
    the worker refills the rest."""

    def _cacheWrappedRow(self, db, username="alice", year=2025):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES (?, 0)", (username,))
            conn.execute(
                "INSERT INTO user_wrapped (username, year, calculated_at, max_played_at, total_plays,"
                " total_ms, longest_streak, unique_songs, unique_artists, discovered_songs,"
                " discovered_artists, time_series_day, time_series_week, time_series_month,"
                " top_songs, top_artists, top_albums, discovered_songs_list,"
                " discovered_artists_list, discovered_albums_list)"
                " VALUES (?, ?, 1, 1, 1, 1, 1, 1, 1, 1, 1, '[]', '[]', '[]', '[]', '[]', '[]',"
                " '[]', '[]', '[]')",
                (username, year))

    def _cachedYears(self, db):
        return db.repo._conn().execute("SELECT COUNT(*) FROM user_wrapped").fetchone()[0]

    def _survivingYears(self, db):
        return {(row["username"], row["year"]) for row in
                db.repo._conn().execute("SELECT username, year FROM user_wrapped")}

    def test_a_merge_drops_every_users_cached_years(self):
        """Every user's, not the actor's: the merge is global, so bob's frozen
        top_songs is as stale as alice's the moment it runs.

        Which YEARS go is test_wrapped_invalidation_scope's question - here only
        that both listeners lose the one the pair was played in, and that a year
        neither of them played it in is not collateral."""
        db = self._dbWithPair()
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('bob', 0)")
            conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                         "VALUES ('bob', ?, ?, 200000)", ("B" * 22, 1e9 + 99))
        self._cacheWrappedRow(db, "alice", PAIR_PLAY_YEAR)
        self._cacheWrappedRow(db, "alice", UNTOUCHED_YEAR)
        self._cacheWrappedRow(db, "bob", PAIR_PLAY_YEAR)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._survivingYears(db), {("alice", UNTOUCHED_YEAR)})

    def test_a_run_that_merges_nothing_leaves_the_caches_alone(self):
        """Idle backfiller cycles re-run the matcher every five minutes while
        the toggle is on; costing a full Wrapped rebuild each time would be a
        self-inflicted stampede."""
        db = self._makeDb({}, [])
        self._cacheWrappedRow(db)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._cachedYears(db), 1)

    def test_the_full_undo_drops_them_too(self):
        db = self._dbWithPair()
        db.repo.mergeTracksByIsrc()
        self._cacheWrappedRow(db)

        db.repo.unmergeAllIsrcMerges()

        self.assertEqual(self._cachedYears(db), 0)

    def test_an_undo_with_nothing_to_undo_leaves_them_alone(self):
        db = self._makeDb({}, [])
        self._cacheWrappedRow(db)

        db.repo.unmergeAllIsrcMerges()

        self.assertEqual(self._cachedYears(db), 1)

    def test_a_single_track_undo_drops_them(self):
        db = self._dbWithPair()
        db.repo.mergeTracksByIsrc()
        self._cacheWrappedRow(db, "alice", PAIR_PLAY_YEAR)

        db.repo.unmergeTrack("B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._cachedYears(db), 0)
if __name__ == "__main__":
    unittest.main()
