"""Observed playback survives metadata and persistence failures without queues."""
import unittest
from unittest.mock import MagicMock, patch

from Database.Listeners.spotifyListener import Listener
from Database.Spotify.recentlyPlayed import _applyPushedState, _applyStateToTracking
from Database.rate_limit import SpotifyLocallyRateLimitedError
from Database.utils import timeToInt
from test_api_backfill import _MONOTONIC_NOW, _testPageProcessor
from test_recently_played_loop import _pushLastPlayed, makePlayingState, pushedCluster
from test_spotify_client_contract import buildClient

PLAYED_AT = "2026-07-26T14:20:00Z"
TRACK_DURATION_MS = 200000
STATE_TIMESTAMPS_MS = (1000000, 1003000, 1008000, 1013000)


class TestDatabaseConfirmedBackfill(unittest.TestCase):
    def setUp(self):
        self.lookup = MagicMock(return_value=[])
        with patch("Database.Listeners.spotifyListener.Spotify") as spotify:
            spotify.return_value.current_user_recently_played.return_value = []
            self.listener = Listener(
                "dummy", email="alice@example.test",
                get_credentials=lambda: {"client_id": "cid", "client_secret": "cs", "refresh_token": "rt"},
                process_backfill_page=None)
        self.items = [{"track": {"id": "track1", "duration_ms": TRACK_DURATION_MS}, "played_at": PLAYED_AT}]

    def _poll(self, callback):
        self.listener.process_backfill_page = _testPageProcessor(self.lookup, callback)
        self.listener._lastWebApiPollTime = 0
        with patch("Database.Listeners.spotifyListener._get_current_user_from_web_api",
                   return_value={"id": "alice", "email": "alice@example.test"}), \
             patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=self.items), \
             patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token"), \
             patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.listener._checkWebApiBackfill(callback)

    def test_failed_live_offer_in_cache_is_still_backfilled(self):
        self.listener.recentlyPlayed_Z1 = list(self.items)
        callback = MagicMock()
        self._poll(callback)
        callback.assert_called_once()
        assert callback.call_args.args[0][0]["track"]["id"] == "track1"

    def test_failed_offers_retry_until_database_confirms_the_play(self):
        callback = MagicMock()  # Worker isolates insert errors and returns normally.
        self._poll(callback)
        self._poll(callback)
        assert callback.call_count == 2
        self.lookup.return_value = [("track1", timeToInt(PLAYED_AT), None)]
        self._poll(callback)
        assert callback.call_count == 2

    def test_lookup_failure_does_not_turn_either_cache_into_an_ack(self):
        self.listener.recentlyPlayed_Z1 = list(self.items)
        self.listener.webApiRecentlyPlayed_Z1 = list(self.items)
        self.lookup.side_effect = RuntimeError("database is locked")
        callback = MagicMock()
        self._poll(callback)
        callback.assert_called_once()

    def test_processor_failure_does_not_fall_back_to_cache_acknowledgement(self):
        self.listener.webApiRecentlyPlayed_Z1 = list(self.items)
        self.listener.process_backfill_page = MagicMock(side_effect=RuntimeError("page failed"))
        callback = MagicMock()

        self.listener._lastWebApiPollTime = 0
        with patch("Database.Listeners.spotifyListener._get_current_user_from_web_api",
                   return_value={"id": "alice", "email": "alice@example.test"}), \
             patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api",
                   return_value=self.items), \
             patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="token"), \
             patch("Database.Listeners.spotifyListener.time.monotonic",
                   return_value=_MONOTONIC_NOW):
            self.listener._checkWebApiBackfill(callback)

        callback.assert_not_called()


class TestMetadataFailurePlayback(unittest.TestCase):
    def test_catalog_failures_preserve_the_original_event(self):
        for error in (TimeoutError("exhausted retries"), SpotifyLocallyRateLimitedError("cooldown"),
                      RuntimeError("session is closed"), ValueError("invalid catalog response")):
            with self.subTest(error=type(error).__name__):
                client = buildClient()
                with patch.object(client, "track", side_effect=error):
                    client._addToRecentlyPlayed("spotify:track:realid", PLAYED_AT, "spotify:playlist:context", 3000)
                item = client.current_user_recently_played()[0]
                assert item["track"]["id"] == "realid"
                assert item["track"]["duration_ms"] == 0
                assert item["played_at"] == PLAYED_AT
                assert item["context"] == {"uri": "spotify:playlist:context"}
                assert item["ms_played"] == 3000

    def test_repeated_failures_preserve_each_push_and_poll_transition(self):
        for mode in ("push", "poll"):
            with self.subTest(mode=mode):
                client = buildClient()
                manager = MagicMock()
                tracking = _pushLastPlayed(manager)
                with patch.object(client, "track", side_effect=RuntimeError("catalog unavailable")):
                    for track_id, timestamp in zip("ABCD", STATE_TIMESTAMPS_MS, strict=True):
                        with patch("Database.Spotify.recentlyPlayed.time.time", return_value=timestamp / 1000):
                            if mode == "push":
                                cluster = pushedCluster(trackUri=f"spotify:track:{track_id}", uid=track_id,
                                                        timestampMs=str(timestamp), contextUri=f"spotify:playlist:{track_id}")
                                cluster["player_state"]["position_as_of_timestamp"] = "0"
                                manager._state = cluster["player_state"]
                                _applyPushedState(tracking, manager, client._addToRecentlyPlayed)
                            else:
                                state = makePlayingState(uid=track_id, uri=f"spotify:track:{track_id}")
                                state.timestamp = timestamp
                                state.position_as_of_timestamp = 0
                                state.context_uri = f"spotify:playlist:{track_id}"
                                _applyStateToTracking(tracking, state, client._addToRecentlyPlayed)
                items = client.current_user_recently_played()
                assert [item["track"]["id"] for item in items] == ["A", "B", "C"]
                assert [item["ms_played"] for item in items] == [3000, 5000, 5000]
                assert [item["context"]["uri"] for item in items] == [f"spotify:playlist:{key}" for key in "ABC"]
                assert tracking.lastPlayedUid == "D"
