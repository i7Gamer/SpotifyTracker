# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Backfill confirmation and insertion share the catalog write transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock, patch

from conftest import DatabaseTestCase, rawSpotifyTrackForTest
from Database.Formatters.spotifyClient import Client
from Database.backfill_matching import BackfillPage


PLAYED_AT = 1_700_000_000
PLAY_DURATION_MS = 180_000
WORKER_TIMEOUT_SECONDS = 10
CONCURRENT_WRITERS = 2
SHORT_DURATION_MS = 1_000


def _track(trackId):
    track = rawSpotifyTrackForTest(trackId, name="One recording")
    track["duration_ms"] = PLAY_DURATION_MS
    track["external_ids"]["isrc"] = "ATOMIC-RECORDING"
    return track


class TestBackfillAtomicWrite(DatabaseTestCase):
    def _db(self):
        db = self._makeDb({}, [], username="alice")
        db.saveImagesFromTrack = MagicMock()
        db.updatePlaylists = MagicMock()
        return db

    @staticmethod
    def _append(db, track, timestamp=PLAYED_AT):
        return db.appendTrackData(timestamp, track, track.get("duration_ms") or 0,
                                  source=db.WEB_API_BACKFILL_SOURCE)

    @staticmethod
    def _plays(db):
        return [dict(row) for row in db.repo.connection().execute(
            "SELECT * FROM plays WHERE username=? ORDER BY played_at, id", (db.user,))]

    def test_concurrent_alias_copies_recheck_after_obtaining_the_write_lock(self):
        db = self._db()
        tracks = [_track("release-a"), _track("release-b")]
        for track in tracks:
            db.repo.upsertTrack(Client.formatTrack(track, embedPlaybackInfo=False))
        db.repo.commit()
        ready = Barrier(CONCURRENT_WRITERS)

        def prepareMedia(_meta):
            self.assertFalse(db.repo.connection().in_transaction)
            ready.wait(timeout=WORKER_TIMEOUT_SECONDS)

        def append(track):
            try:
                return self._append(db, track)
            finally:
                db.repo.connectionManager.close()

        with patch.object(db, "saveImagesFromTrack", side_effect=prepareMedia):
            with ThreadPoolExecutor(max_workers=CONCURRENT_WRITERS) as executor:
                futures = [executor.submit(append, track) for track in tracks]
                inserted = [future.result(timeout=WORKER_TIMEOUT_SECONDS) for future in futures]

        self.assertEqual(sorted(inserted), [False, True])
        self.assertEqual(len(self._plays(db)), 1)

    def test_api_identity_never_uses_duration_or_skip_windows(self):
        for duration in (None, SHORT_DURATION_MS, PLAY_DURATION_MS):
            with self.subTest(duration=duration):
                db = self._db()
                track = _track("repeat")
                track["duration_ms"] = duration
                self.assertTrue(self._append(db, track))
                self.assertTrue(self._append(db, track, PLAYED_AT + 1))
                self.assertFalse(self._append(db, track))
                self.assertEqual(len(self._plays(db)), 2)

    def test_matcher_runs_inside_transaction_and_media_precedes_it(self):
        db = self._db()
        events = []
        originalMatch = db.repo.findMatchingBackfillPlay

        def match(*args, **kwargs):
            events.append(("match", db.repo.connection().in_transaction))
            return originalMatch(*args, **kwargs)

        with patch.object(db, "saveImagesFromTrack", side_effect=lambda _meta:
                          events.append(("media", db.repo.connection().in_transaction))), \
                patch.object(db.repo, "findMatchingBackfillPlay", side_effect=match):
            self.assertTrue(self._append(db, _track("new-track")))

        self.assertEqual(events, [("media", False), ("match", True)])
        self.assertFalse(db.repo.connection().in_transaction)

    def test_guard_failure_rolls_back_catalog_and_retry_is_not_claimed(self):
        db = self._db()
        with patch.object(db.repo, "findMatchingBackfillPlay",
                          side_effect=RuntimeError("synthetic guard failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic guard failure"):
                self._append(db, _track("retry"))
        self.assertIsNone(db.repo.connection().execute(
            "SELECT id FROM tracks WHERE id='retry'").fetchone())
        self.assertEqual(self._plays(db), [])
        self.assertFalse(db.repo.connection().in_transaction)
        self.assertTrue(self._append(db, _track("retry")))

    def test_commit_failure_rolls_back_catalog_play_and_later_retry_succeeds(self):
        db = self._db()
        with patch.object(db.repo, "commit", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic commit failure"):
                self._append(db, _track("retry"))
        self.assertIsNone(db.repo.connection().execute(
            "SELECT id FROM tracks WHERE id='retry'").fetchone())
        self.assertEqual(self._plays(db), [])
        self.assertFalse(db.repo.connection().in_transaction)
        self.assertTrue(self._append(db, _track("retry")))
        self.assertFalse(self._append(db, _track("retry")))


    def test_failed_confirmation_commit_does_not_consume_primary_for_next_event(self):
        db = self._db()
        track = _track("primary")
        self.assertTrue(db.appendTrackData(PLAYED_AT, track, PLAY_DURATION_MS, source="listener"))
        page = BackfillPage([
            {"track": track, "played_at": PLAYED_AT + 1},
            {"track": track, "played_at": PLAYED_AT + 2},
        ])
        with patch.object(db.repo, "commit", side_effect=RuntimeError("synthetic commit failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic commit failure"):
                db.appendTrackData(PLAYED_AT + 1, track, PLAY_DURATION_MS,
                                   source=db.WEB_API_BACKFILL_SOURCE, backfillPage=page)
        self.assertFalse(db.appendTrackData(PLAYED_AT + 2, track, PLAY_DURATION_MS,
                                            source=db.WEB_API_BACKFILL_SOURCE, backfillPage=page))
        self.assertEqual(len(self._plays(db)), 1)

    def test_cleanup_reads_and_deletes_under_one_transaction_and_releases_it(self):
        db = self._db()
        track = _track("duplicate")
        self.assertTrue(self._append(db, track))
        self.assertTrue(db.appendTrackData(PLAYED_AT + 3, track, PLAY_DURATION_MS, source="listener"))
        page = [{"track": track, "played_at": PLAYED_AT}]
        originalRead = db.repo.getPlaysWithSourceInRange
        originalDelete = db.repo.deletePlay
        states = []

        def read(*args, **kwargs):
            states.append(("read", db.repo.connection().in_transaction))
            return originalRead(*args, **kwargs)

        def delete(*args, **kwargs):
            states.append(("delete", db.repo.connection().in_transaction))
            return originalDelete(*args, **kwargs)

        with patch.object(db.repo, "getPlaysWithSourceInRange", side_effect=read), \
                patch.object(db.repo, "deletePlay", side_effect=delete):
            db._reconcileWithWebApiHistory(page)
        self.assertEqual(states, [("read", True), ("delete", True)])
        self.assertEqual([row["played_at"] for row in self._plays(db)], [PLAYED_AT + 3])
        self.assertFalse(db.repo.connection().in_transaction)
        db._reconcileWithWebApiHistory(page)
        self.assertFalse(db.repo.connection().in_transaction)

    def test_cleanup_delete_and_commit_failures_roll_back_without_dropping_primary(self):
        for failingMethod in ("deletePlay", "commit"):
            with self.subTest(failingMethod=failingMethod):
                db = self._db()
                track = _track("duplicate")
                self.assertTrue(self._append(db, track))
                self.assertTrue(db.appendTrackData(PLAYED_AT + 3, track, PLAY_DURATION_MS, source="listener"))
                page = [{"track": track, "played_at": PLAYED_AT}]
                with patch.object(db.repo, failingMethod, side_effect=RuntimeError("synthetic cleanup failure")):
                    db._reconcileWithWebApiHistory(page)
                self.assertEqual(len(self._plays(db)), 2)
                self.assertFalse(db.repo.connection().in_transaction)
                db._reconcileWithWebApiHistory(page)
                self.assertEqual([row["played_at"] for row in self._plays(db)], [PLAYED_AT + 3])
