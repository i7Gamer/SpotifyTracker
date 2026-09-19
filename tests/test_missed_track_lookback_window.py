"""The lookback window the listener's missed-play cross-check judges against.

Database.getRecentlyRecordedTrackIds is where
CONNECT_STATE_MISSED_TRACK_LOOKBACK_SECONDS is actually bound - the repository
query underneath takes the window as a parameter (see
test_repository_played_ids.py), so nothing below this layer pins how far back
the cross-check really looks.

Why it has to reach past one listening session: the window's original 6h was
chosen on the assumption that "anything in prev_tracks was played within the
current listening session". Live app.log (2026-09-12..19, 422 flagged
warning/track pairs) refutes that - prev_tracks is the Spotify client's own
rolling queue history and survives for as long as the client does, so 42% of
all flagged pairs had a real, recorded play between 6h and 7d old. Those were
pure false positives: the play was in the database the whole time, just older
than the window could see.

The ceiling matters as much as the reach, which is why the far-side case is
pinned too - "played at some point ever" is no evidence that the play now
sitting in the queue history was captured, and a window wide enough to say so
would make the cross-check permanently silent.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database
from Database.repository import Repository

HOUR_SECONDS = 3600
DAY_SECONDS = 24 * HOUR_SECONDS
PLAY_DURATION_MS = 60000


def _track(trackId):
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [{"id": "a1", "name": "Artist a1", "url": "http://example.com/artist/a1",
                     "imageUrl": "", "imageId": "a1"}],
        "album": {"id": "al1", "name": "Album al1", "url": "http://example.com/album/al1",
                  "imageId": "al1", "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0},
        "imageUrl": "", "imageId": "al1", "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class TestMissedTrackLookbackWindow(unittest.TestCase):
    """Real Repository behind the real wrapper: a mocked repo would only let
    this assert the constant equals itself, which pins nothing."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        for trackId in ("just-now", "yesterday", "two-days", "six-days",
                        "last-month", "bobs"):
            self.repo.upsertTrack(_track(trackId))
        self.repo.commit()

        now = time.time()
        self.repo.insertPlay("alice", "just-now", now - HOUR_SECONDS, PLAY_DURATION_MS)
        self.repo.insertPlay("alice", "yesterday", now - DAY_SECONDS, PLAY_DURATION_MS)
        self.repo.insertPlay("alice", "two-days", now - 2 * DAY_SECONDS, PLAY_DURATION_MS)
        self.repo.insertPlay("alice", "six-days", now - 6 * DAY_SECONDS, PLAY_DURATION_MS)
        self.repo.insertPlay("alice", "last-month", now - 30 * DAY_SECONDS, PLAY_DURATION_MS)
        self.repo.insertPlay("bob", "bobs", now - DAY_SECONDS, PLAY_DURATION_MS)
        self.repo.commit()

        self.db = Database.__new__(Database)
        self.db.user = "alice"
        self.db.repo = self.repo

    def test_a_play_from_this_session_is_still_found(self):
        self.assertEqual(self.db.getRecentlyRecordedTrackIds(["just-now"]), {"just-now"})

    def test_yesterdays_play_is_found(self):
        """prev_tracks routinely still holds it - see the module docstring."""
        self.assertEqual(self.db.getRecentlyRecordedTrackIds(["yesterday"]), {"yesterday"})

    def test_a_two_day_old_play_is_found(self):
        self.assertEqual(self.db.getRecentlyRecordedTrackIds(["two-days"]), {"two-days"})

    def test_a_six_day_old_play_is_found(self):
        """The far edge of what the window is meant to reach."""
        self.assertEqual(self.db.getRecentlyRecordedTrackIds(["six-days"]), {"six-days"})

    def test_a_month_old_play_is_not_treated_as_evidence(self):
        """The ceiling: without one the cross-check could never report anything."""
        self.assertEqual(self.db.getRecentlyRecordedTrackIds(["last-month"]), set())

    def test_another_users_play_does_not_count(self):
        """The wrapper binds self.user; a shared track must not vouch across users."""
        self.assertEqual(self.db.getRecentlyRecordedTrackIds(["bobs"]), set())

    def test_a_never_played_track_is_absent(self):
        self.assertEqual(self.db.getRecentlyRecordedTrackIds(["never-seen"]), set())

    def test_a_mixed_batch_is_answered_in_one_call(self):
        self.assertEqual(
            self.db.getRecentlyRecordedTrackIds(
                ["just-now", "two-days", "last-month", "bobs", "never-seen"]),
            {"just-now", "two-days"},
        )

    def test_an_empty_batch_asks_nothing(self):
        self.assertEqual(self.db.getRecentlyRecordedTrackIds([]), set())


if __name__ == "__main__":
    unittest.main()
