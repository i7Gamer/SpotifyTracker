"""Tests for the connect-state cross-check that replaces the old REST-API
verification poll.

/v1/me/player/recently-played is a deprecated-for-third-parties public REST
endpoint - calling it with spotapi's web-player-scoped token 429s permanently,
no backoff recovers it (see git history for the removed _pollRestApiHistory*
code). spotapi's LastPlayedManger already re-fetches Spotify Connect state
(PlayerStatus.state, via connect_device()) every refreshInterval tick for its
own current-track detection, and that same state carries `prev_tracks` - the
local queue's play history - at no extra network cost. This cross-checks that
history against what we've already recorded, purely as a diagnostic signal for
catching plays the (known-fragile) websocket cache silently missed.
"""
import collections
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.Listeners.spotifyListener import (
    Listener,
    CONNECT_STATE_MISSED_TRACK_CACHE_SIZE,
    CONNECT_STATE_MISSED_TRACK_GRACE_SECONDS,
    _settledMissingUrisForUser,
)


def _bareListener(recentlyPlayed=None, get_recorded_track_ids=None,
                  user="alice", email="alice@example.com"):
    listener = Listener.__new__(Listener)
    listener.run = True
    #< logUser is a property over these two - a real Listener always has them,
    #  and without them every self.logUser read raises AttributeError into the
    #  cross-check's own except-clause, silently cancelling the warning
    listener.user = user
    listener.email = email
    listener.sp = MagicMock()
    listener.recentlyPlayed_Z1 = recentlyPlayed if recentlyPlayed is not None else []
    listener._settledMissingTrackUris = collections.OrderedDict()
    listener._pendingMissingTrackUris = {}
    listener.get_recorded_track_ids = get_recorded_track_ids
    return listener


def _ripen(listener):
    """Backdate every pending first-seen stamp past the grace window, so the
    next check treats the candidates as ripe. Deterministic stand-in for
    'CONNECT_STATE_MISSED_TRACK_GRACE_SECONDS elapsed' - time.monotonic() never
    decreases, so age >= grace holds without any real waiting."""
    for uri in listener._pendingMissingTrackUris:
        listener._pendingMissingTrackUris[uri] -= CONNECT_STATE_MISSED_TRACK_GRACE_SECONDS


def _checkUntilRipe(listener):
    """First sighting (stamps the candidates as pending), then a ripe re-check -
    the shortest path to a warning under the grace window."""
    listener._checkConnectStateForMissedTracks()
    _ripen(listener)
    listener._checkConnectStateForMissedTracks()


def _withConnectState(listener, prevTracks):
    """Wire up listener.sp.lastPlayedManager.manager._state the way
    spotapi's PlayerStatus actually stores it (a raw dict, not the PlayerState
    dataclass - see spotapi/status.py's `_state`)."""
    manager = MagicMock()
    manager._state = {"prev_tracks": prevTracks}
    listener.sp.lastPlayedManager = MagicMock()
    listener.sp.lastPlayedManager.manager = manager


class TestGetRecentTrackUrisFromConnectState(unittest.TestCase):
    def test_no_last_played_manager_returns_none(self):
        listener = _bareListener()
        listener.sp.lastPlayedManager = None

        self.assertIsNone(listener._getRecentTrackUrisFromConnectState())

    def test_no_manager_on_last_played_manager_returns_none(self):
        listener = _bareListener()
        listener.sp.lastPlayedManager = MagicMock()
        listener.sp.lastPlayedManager.manager = None

        self.assertIsNone(listener._getRecentTrackUrisFromConnectState())

    def test_no_state_captured_yet_returns_none(self):
        listener = _bareListener()
        manager = MagicMock()
        manager._state = None
        listener.sp.lastPlayedManager = MagicMock()
        listener.sp.lastPlayedManager.manager = manager

        self.assertIsNone(listener._getRecentTrackUrisFromConnectState())

    def test_extracts_uris_from_prev_tracks(self):
        listener = _bareListener()
        _withConnectState(listener, [
            {"uri": "spotify:track:aaa"},
            {"uri": "spotify:track:bbb"},
        ])

        self.assertEqual(
            listener._getRecentTrackUrisFromConnectState(),
            ["spotify:track:aaa", "spotify:track:bbb"],
        )

    def test_tracks_without_uri_are_skipped(self):
        listener = _bareListener()
        _withConnectState(listener, [
            {"uri": "spotify:track:aaa"},
            {"uri": None},
            {},
        ])

        self.assertEqual(listener._getRecentTrackUrisFromConnectState(), ["spotify:track:aaa"])

    def test_non_track_uris_are_filtered_out(self):
        """prev_tracks also carries queue markers (spotify:delimiter,
        spotify:meta:*) - they are not tracks and must not be returned."""
        listener = _bareListener()
        _withConnectState(listener, [
            {"uri": "spotify:track:aaa"},
            {"uri": "spotify:delimiter"},
            {"uri": "spotify:meta:node_rules_placeholder"},
        ])

        self.assertEqual(listener._getRecentTrackUrisFromConnectState(), ["spotify:track:aaa"])


class TestCheckConnectStateForMissedTracks(unittest.TestCase):
    def _recordedItem(self, trackId):
        """The owned client's REAL shape: formatTrackUnion emits `id` and no
        `track_id`. The old fixture supplied both spellings, which is exactly
        how a cross-check reading only the dead one stayed green while every
        production set collapsed to {None}."""
        return {"track": {"id": trackId}, "played_at": "t"}

    def test_logs_warning_for_track_missing_from_recorded_history(self):
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa")])
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}, {"uri": "spotify:track:bbb"}])

        listener._checkConnectStateForMissedTracks()  #< first sighting - pending, not warned
        _ripen(listener)
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_the_warning_names_the_user_it_belongs_to(self):
        """Every listener runs its own cross-check against its own account's
        queue history, so an unattributed warning cannot be acted on: a live
        scorecard of these warnings had to say "some user has this play"
        instead of "this user does", purely because the line did not say whose
        prev_tracks it came from."""
        listener = _bareListener(recentlyPlayed=[], user="carol")
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        listener._checkConnectStateForMissedTracks()
        _ripen(listener)
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("carol" in message for message in cm.output))

    def test_the_warning_falls_back_to_the_email_when_there_is_no_user_key(self):
        """Same fallback as logUser everywhere else - an anonymous listener
        still has to be distinguishable from its neighbours."""
        listener = _bareListener(recentlyPlayed=[], user=None, email="dave@example.com")
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        listener._checkConnectStateForMissedTracks()
        _ripen(listener)
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("dave@example.com" in message for message in cm.output))

    def test_does_not_warn_when_all_tracks_already_recorded(self):
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa"), self._recordedItem("bbb")])
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}, {"uri": "spotify:track:bbb"}])

        # assertNoLogs isn't available on all supported Python versions - assert
        # directly on the dedup cache instead, which only grows on a warning.
        listener._checkConnectStateForMissedTracks()
        self.assertEqual(len(listener._settledMissingTrackUris), 0)

    def test_fallback_shaped_records_are_recognized_too(self):
        """fallbackTrackRecord still spells it track_id (with id beside it),
        and older in-flight buffers may carry only the old key - the check
        must read both spellings, same as _itemTrackId."""
        listener = _bareListener(recentlyPlayed=[{"track": {"track_id": "aaa"}, "played_at": "t"}])
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}])

        listener._checkConnectStateForMissedTracks()
        self.assertEqual(len(listener._settledMissingTrackUris), 0)

    def test_does_not_warn_twice_for_the_same_missing_track(self):
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        listener._checkConnectStateForMissedTracks()  #< first sighting - pending, not warned
        _ripen(listener)
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()
        firstCallWarnings = len(cm.output)

        # Later calls: same missing track, already warned about - must not log
        # again, even after another full grace window.
        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            listener._checkConnectStateForMissedTracks()
            _ripen(listener)
            listener._checkConnectStateForMissedTracks()

        self.assertEqual(firstCallWarnings, 1)

    def test_non_track_uris_never_warn(self):
        """Queue markers (spotify:delimiter, spotify:meta:*) can never match a
        recorded play - warning about them (as happened before the filter)
        just repeats forever across reconnects."""
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [
            {"uri": "spotify:delimiter"},
            {"uri": "spotify:meta:node_rules_placeholder"},
            {"uri": "spotify:track:bbb"},
        ])

        listener._checkConnectStateForMissedTracks()  #< first sighting - pending, not warned
        _ripen(listener)
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertEqual(len(cm.output), 1)
        self.assertIn("spotify:track:bbb", cm.output[0])
        self.assertNotIn("delimiter", cm.output[0])
        self.assertNotIn("spotify:meta", cm.output[0])

    def test_only_non_track_uris_produces_no_warning(self):
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [
            {"uri": "spotify:delimiter"},
            {"uri": "spotify:meta:node_rules_placeholder"},
        ])

        listener._checkConnectStateForMissedTracks()  # must not raise

        self.assertEqual(len(listener._settledMissingTrackUris), 0)

    def test_no_connect_state_available_does_not_raise(self):
        listener = _bareListener(recentlyPlayed=[])
        listener.sp.lastPlayedManager = None

        listener._checkConnectStateForMissedTracks()  # must not raise

    def test_internal_exception_is_swallowed(self):
        """This is a diagnostic side-channel - a bug here must never take down
        the primary polling loop."""
        class _RaisingLastPlayedManager:
            @property
            def manager(self):
                raise RuntimeError("boom")

        listener = _bareListener(recentlyPlayed=[])
        listener.sp.lastPlayedManager = _RaisingLastPlayedManager()

        listener._checkConnectStateForMissedTracks()  # must not raise

    def test_dedup_cache_is_bounded(self):
        listener = _bareListener(recentlyPlayed=[])
        listener._settledMissingTrackUris = collections.OrderedDict.fromkeys(
            f"spotify:track:{i}" for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)
        )
        _withConnectState(listener, [{"uri": "spotify:track:new"}])

        _checkUntilRipe(listener)

        self.assertLessEqual(len(listener._settledMissingTrackUris), CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)

    def test_db_is_asked_about_a_given_track_only_once(self):
        """The poll loop calls this every second (_stop_event.wait(1)), and a
        track the DB vouches for is a permanent miss against the in-memory
        cache - so without recording the answer it would be re-queried ~1,800
        times per listener per idle cycle, per user. The database answer has to
        settle the URI just as a warning does. The grace window shields the
        database further: a candidate that is merely pending costs no query at
        all - only a ripe one is looked up, exactly once."""
        recorded = MagicMock(return_value={"bbb"})
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        for _ in range(5):
            listener._checkConnectStateForMissedTracks()
        recorded.assert_not_called()  #< still inside the grace window

        _ripen(listener)
        for _ in range(5):
            listener._checkConnectStateForMissedTracks()

        recorded.assert_called_once_with(["bbb"])

    def test_settled_cache_stays_bounded_for_db_confirmed_tracks(self):
        """Same FIFO bound as the warned entries - a long queue of
        already-recorded tracks must not grow the cache without limit."""
        recorded = MagicMock(side_effect=lambda ids: set(ids))
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [
            {"uri": f"spotify:track:{i}"} for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE + 10)
        ])

        _checkUntilRipe(listener)

        self.assertLessEqual(len(listener._settledMissingTrackUris),
                             CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)

    def test_failed_db_lookup_does_not_settle_the_uri(self):
        """A lookup that errored answered nothing - suppressing on it would
        turn a transient database problem into permanent silence about a
        possibly-missed play."""
        recorded = MagicMock(side_effect=RuntimeError("db gone"))
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _checkUntilRipe(listener)
        # the warning itself settles it, but the lookup must be retried for a
        # track that is still unaccounted for after the cache is cleared
        listener._settledMissingTrackUris.clear()
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _checkUntilRipe(listener)

        self.assertEqual(recorded.call_count, 2)

    def test_db_confirms_the_track_really_is_missing_before_warning(self):
        """The in-memory recentlyPlayed_Z1 cache only covers the CURRENT listener
        object's lifetime, and the listener is rebuilt on every reconnect (1,568
        times over 11 days in app.log). A play recorded before the last
        reconnect is therefore absent from the cache while sitting safely in the
        database - which is why this warning fired 1,627 times without any of
        them necessarily being real. The database is the source of truth."""
        recorded = MagicMock(return_value={"bbb"})
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _checkUntilRipe(listener)

        recorded.assert_called_once_with(["bbb"])
        #< settled, not forgotten - see test_db_is_asked_about_a_given_track_only_once
        self.assertIn("spotify:track:bbb", listener._settledMissingTrackUris)

    def test_db_lookup_still_warns_for_a_genuinely_absent_play(self):
        recorded = MagicMock(return_value=set())
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            _checkUntilRipe(listener)

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_db_lookup_only_asked_about_cache_misses(self):
        """The cheap in-memory check runs first - only what it can't vouch for
        reaches the database, so the common all-recorded case costs no query."""
        recorded = MagicMock(return_value=set())
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa")],
                                 get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}, {"uri": "spotify:track:bbb"}])

        _checkUntilRipe(listener)

        recorded.assert_called_once_with(["bbb"])

    def test_db_lookup_not_called_when_cache_vouches_for_everything(self):
        recorded = MagicMock(return_value=set())
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa")],
                                 get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}])

        _checkUntilRipe(listener)

        recorded.assert_not_called()

    def test_db_lookup_failure_falls_back_to_warning(self):
        """A broken lookup must not silence the diagnostic - degrade to the old
        cache-only behavior rather than swallowing a possible missed play."""
        recorded = MagicMock(side_effect=RuntimeError("db gone"))
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            _checkUntilRipe(listener)

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_without_the_callback_behaviour_is_unchanged(self):
        """Callers that don't supply the lookup (tests, anything constructed
        before it existed) keep the cache-only behavior."""
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=None)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            _checkUntilRipe(listener)

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_dedup_cache_evicts_oldest_entry_first(self):
        """set.pop() would evict an arbitrary element; the OrderedDict-based
        cache must specifically evict the OLDEST entry (FIFO), so recently
        warned-about tracks are never forgotten ahead of older ones."""
        listener = _bareListener(recentlyPlayed=[])
        listener._settledMissingTrackUris = collections.OrderedDict.fromkeys(
            f"spotify:track:{i}" for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)
        )
        _withConnectState(listener, [{"uri": "spotify:track:new"}])

        _checkUntilRipe(listener)

        self.assertNotIn("spotify:track:0", listener._settledMissingTrackUris)
        for i in range(1, CONNECT_STATE_MISSED_TRACK_CACHE_SIZE):
            self.assertIn(f"spotify:track:{i}", listener._settledMissingTrackUris)
        self.assertIn("spotify:track:new", listener._settledMissingTrackUris)


class TestMissedTrackGraceWindow(unittest.TestCase):
    """prev_tracks shows a track the moment playback moves on, while its
    recording lands a recently-played/backfill poll later - every 'never
    recorded' warning sampled in app.log 2026-08-04 was followed by its own
    web_api_backfill recording 3-22 seconds later. A candidate must stay
    unaccounted for a full grace window before it is worth a warning."""

    def test_first_sighting_only_marks_pending_no_warning(self):
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            listener._checkConnectStateForMissedTracks()

        self.assertIn("spotify:track:bbb", listener._pendingMissingTrackUris)
        self.assertEqual(len(listener._settledMissingTrackUris), 0)

    def test_repeat_checks_inside_the_grace_window_stay_quiet(self):
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            for _ in range(5):
                listener._checkConnectStateForMissedTracks()

        self.assertIn("spotify:track:bbb", listener._pendingMissingTrackUris)

    def test_still_missing_after_the_grace_window_warns_once(self):
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        listener._checkConnectStateForMissedTracks()
        _ripen(listener)
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("bbb" in message for message in cm.output))
        #< answered - the warning must also clear the pending stamp
        self.assertNotIn("spotify:track:bbb", listener._pendingMissingTrackUris)

    def test_track_recorded_during_the_grace_window_never_warns(self):
        """The exact live-log sequence: sighted in prev_tracks, recorded via
        backfill seconds later - must resolve silently."""
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        listener._checkConnectStateForMissedTracks()  #< sighted, pending
        listener.recentlyPlayed_Z1 = [{"track": {"id": "bbb"}, "played_at": "t"}]  #< backfill landed

        _ripen(listener)
        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            listener._checkConnectStateForMissedTracks()

        self.assertNotIn("spotify:track:bbb", listener._pendingMissingTrackUris)
        self.assertEqual(len(listener._settledMissingTrackUris), 0)

    def test_track_recorded_in_database_during_the_grace_window_settles_silently(self):
        recorded = MagicMock(return_value={"bbb"})
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _checkUntilRipe(listener)

        self.assertIn("spotify:track:bbb", listener._settledMissingTrackUris)
        self.assertNotIn("spotify:track:bbb", listener._pendingMissingTrackUris)

    def test_track_that_rolls_out_of_queue_history_is_forgotten(self):
        """Also what keeps the pending dict bounded: it can only ever hold
        what prev_tracks currently holds."""
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])
        listener._checkConnectStateForMissedTracks()

        _withConnectState(listener, [{"uri": "spotify:track:ccc"}])
        listener._checkConnectStateForMissedTracks()

        self.assertNotIn("spotify:track:bbb", listener._pendingMissingTrackUris)
        self.assertIn("spotify:track:ccc", listener._pendingMissingTrackUris)


class TestSettledCachePersistsAcrossListenerGenerations(unittest.TestCase):
    """A listener is rebuilt on every reconnect and every 6h hard-ceiling
    recycle (LISTENER_STALE_HARD_TIMEOUT_SECONDS), and an idle account's
    prev_tracks never changes - so a per-listener settled set re-warned the
    same ~10 storm-era tracks every 6 hours for days (app.log 2026-08-01..04).
    The settled store therefore lives per user for the lifetime of the
    process, not per Listener object.

    Every test uses its own unique user key: the registry is process-global
    on purpose, so a shared key would couple tests run in the same worker."""

    def test_same_user_key_returns_the_same_store(self):
        self.assertIs(_settledMissingUrisForUser("crosscheck-gen-same"),
                      _settledMissingUrisForUser("crosscheck-gen-same"))

    def test_different_user_keys_get_separate_stores(self):
        self.assertIsNot(_settledMissingUrisForUser("crosscheck-gen-left"),
                         _settledMissingUrisForUser("crosscheck-gen-right"))

    def test_rebuilt_listener_does_not_rewarn_a_settled_track(self):
        userKey = "crosscheck-gen-rebuild"
        first = _bareListener(recentlyPlayed=[])
        first._settledMissingTrackUris = _settledMissingUrisForUser(userKey)
        _withConnectState(first, [{"uri": "spotify:track:bbb"}])
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _checkUntilRipe(first)

        rebuilt = _bareListener(recentlyPlayed=[])
        rebuilt._settledMissingTrackUris = _settledMissingUrisForUser(userKey)
        _withConnectState(rebuilt, [{"uri": "spotify:track:bbb"}])
        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _checkUntilRipe(rebuilt)

    def _constructListener(self, **kwargs):
        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            return Listener("dummy_cookie", **kwargs)

    def test_constructor_wires_the_shared_store_for_a_keyed_listener(self):
        listener = self._constructListener(user="crosscheck-gen-wired",
                                           email="crosscheck-gen-wired@example.com")

        self.assertIs(listener._settledMissingTrackUris,
                      _settledMissingUrisForUser("crosscheck-gen-wired"))

    def test_constructor_falls_back_to_the_email_key(self):
        listener = self._constructListener(email="crosscheck-gen-email-only@example.com")

        self.assertIs(listener._settledMissingTrackUris,
                      _settledMissingUrisForUser("crosscheck-gen-email-only@example.com"))

    def test_anonymous_listeners_keep_private_stores(self):
        """No user key and no email (only ever tests) - sharing one store
        between unrelated anonymous listeners would couple them for no
        benefit."""
        listener = self._constructListener()
        other = self._constructListener()

        self.assertIsNot(listener._settledMissingTrackUris, other._settledMissingTrackUris)


if __name__ == "__main__":
    unittest.main()
