"""Tests for the Listener's self-healing reconnect.

spotapi's own websocket-fed "recently played" feed can silently die (its
reconnect() call in LastPlayedManger.updateLoop targets a method that doesn't
exist on PlayerStatus, so recovery from a dropped websocket is broken upstream
- see Database/Listeners/spotifyListener.py). Once that happens,
current_user_recently_played() keeps returning the same frozen list forever:
no exception, no new items, nothing recorded, indefinitely. This tests the
staleness timeout that detects that frozen state and asks the caller to
rebuild the session instead of staying wedged forever.
"""
import logging
import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from _spotapi_exceptions import realProfileStatusError, realTransportRequestError
from Database.rate_limit import SpotifyLocallyRateLimitedError
from Database.Listeners.spotifyListener import (
    Listener,
    LISTENER_STALE_TIMEOUT_SECONDS,
    LISTENER_STALE_HARD_TIMEOUT_SECONDS,
    LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS,
    STALE_REASON_VALIDATION_FAILED,
    STALE_REASON_UNRECORDED_PLAYBACK,
    STALE_REASON_HARD_CEILING,
    STALE_REASON_AUTH_ERROR,
    LISTENER_PUSH_CHANNEL_ALIVE_SECONDS,
    RATE_LIMIT_ERROR_BACKOFF_SECONDS,
    RATE_LIMIT_REASON_LISTENER_POLL,
    LOCAL_PAUSE_RETRY_WAIT_SECONDS,
    USER_VALIDATION_CACHE_SECONDS,
    USER_VALIDATION_ERROR_COOLDOWN_SECONDS,
    WEB_API_POLL_INTERVAL_SECONDS,
    LISTENER_POLL_INTERVAL_SECONDS,
    LISTENER_POLL_INTERVAL_JITTER_SECONDS,
    LISTENER_PLAYBACK_CONTINUITY_SECONDS,
    LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS,
    LISTENER_POLL_RENEWAL_FRESH_SECONDS,
    STALE_REASON_NO_CONNECT_STATE,
    _pollIntervalWithJitter,
    _is_auth_error,
    _is_rate_limit_error,
    classifyListenerError,
)

# Comfortably past USER_VALIDATION_CACHE_SECONDS so _validateCurrentUser's
# freshness-cache branch is deterministically bypassed, regardless of how
# large time.monotonic() already is on the host running the test (e.g. a
# freshly booted CI runner has a much smaller monotonic clock than a
# long-uptime dev machine).
_MONOTONIC_NOW = USER_VALIDATION_CACHE_SECONDS * 10


def _bareListener(recentlyPlayed=None):
    listener = Listener.__new__(Listener)
    listener.run = True
    listener.sp = MagicMock()
    listener.recentlyPlayed_Z1 = recentlyPlayed if recentlyPlayed is not None else []
    listener.sp.current_user_recently_played.return_value = listener.recentlyPlayed_Z1
    # No connect state captured (what a listener whose websocket tick never ran
    # or keeps erroring looks like) - the stale check reads this to tell a dead
    # session from an idle account, and "no evidence" means dead.
    listener.sp.lastPlayedManager.manager._state = None
    # Poll mode: no push loop is feeding _state, which is what makes the
    # absence above evidence of a dead tick. sp is a MagicMock, so these have to
    # be set explicitly - an auto-created child attribute would read as a
    # (truthy, non-numeric) liveness stamp.
    listener.sp.lastPlayedManager.pushChannelAliveAt = None
    listener.sp.lastPlayedManager.subscriptionRenewedAt = None
    # Same reason: the poll tick's own renewal stamp, which vouches for a
    # connect state that is legitimately absent (nothing is being cast).
    listener.sp.lastPlayedManager.manager.stateRenewalSucceededAt = None
    listener._lastPlayingUri = None      #< matches Listener.__init__
    listener._lastPlayingChangeTime = 0.0
    listener._lastPlayingSeenAt = 0.0
    listener._firstUnarrivedChangeTime = 0.0
    listener._lastChangeTime = 0.0
    listener._authenticated_user_id = None
    listener.email = None
    listener.user = None  #< matches Listener.__init__; self.logUser reads both
    listener._last_user_validation_time = None  #< matches Listener.__init__: never validated yet
    listener._last_user_validation_result = True
    listener._last_validation_error_time = None  #< matches Listener.__init__: no refusal recorded
    return listener


class TestCheckOnceNewItems(unittest.TestCase):
    def test_new_item_invokes_callback_and_resets_change_time(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener.sp.current_user_recently_played.return_value = [{"played_at": 1}, {"played_at": 2}]
        callback = MagicMock()

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=500.0):
            stillRunning = listener._checkOnce(callback, onStale=None)

        self.assertTrue(stillRunning)
        callback.assert_called_once_with([{"played_at": 2}])
        self.assertEqual(listener.recentlyPlayed_Z1, [{"played_at": 1}, {"played_at": 2}])
        self.assertEqual(listener._lastChangeTime, 500.0)

    def test_unchanged_feed_within_timeout_does_not_trigger_onStale(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock()

        withinTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS - 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=withinTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()


class TestCheckOnceStaleness(unittest.TestCase):
    def test_frozen_feed_past_timeout_triggers_onStale_and_stops(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock()

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_frozen_feed_past_timeout_without_onStale_keeps_running(self):
        """No onStale callback wired (e.g. a bare/manual Listener) - must not
        crash, and since there's no way to recover, it just keeps polling."""
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=None)

        self.assertTrue(stillRunning)

    def test_onStale_exception_is_swallowed_and_still_stops(self):
        """A failed reconnect attempt (e.g. cookies genuinely expired) must not
        crash the polling thread silently with no trace - the exception is
        logged and the (now-dead) listener still stops."""
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock(side_effect=RuntimeError("reconnect failed"))

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()


class TestStaleReasonReporting(unittest.TestCase):
    """Every onStale call names WHY the rebuild was requested. The reason rides
    through _makeOnStaleCallback into the listener-session ledger on /admin's
    Worker Health card, so an operator can tell scheduled recycling (the
    quiet-feed hard ceiling) from a session actually decaying - without
    grepping app.log and correlating timestamps by hand (how the 2026-08-04
    websocket investigation had to do it)."""

    TRACK_A = "spotify:track:aaaaaaaaaaaaaaaaaaaaaa"
    TRACK_B = "spotify:track:bbbbbbbbbbbbbbbbbbbbbb"

    def _poll(self, listener, now, state, onStale):
        listener.sp.lastPlayedManager.manager._state = state
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=now):
            return listener._checkOnce(MagicMock(), onStale=onStale)

    def _listener(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        return listener

    def test_unrecorded_playback_names_its_reason(self):
        """A track change the feed never recorded - the genuine failure."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, 210.0, _playingState(self.TRACK_B), onStale)  #< within the continuity window
        self._poll(listener, 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1,
                   _playingState(self.TRACK_B), onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_UNRECORDED_PLAYBACK)

    def test_the_hard_ceiling_names_its_reason(self):
        """An idle account recycled purely because the quiet feed aged past the
        ceiling - scheduled maintenance, not decay, and the ledger must say so."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1,
                   {"is_playing": False}, onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_HARD_CEILING)

    def test_broken_evidence_beats_the_ceiling_label(self):
        """Past the hard ceiling WITH evidence of unrecorded playback, the
        evidence is the stronger diagnosis - "hard ceiling" would misread a
        genuinely broken session as routine recycling."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, 210.0, _playingState(self.TRACK_B), onStale)  #< within the continuity window
        self._poll(listener, 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1,
                   _playingState(self.TRACK_B), onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_UNRECORDED_PLAYBACK)

    def test_a_frozen_connect_state_is_not_a_playback_sighting(self):
        """The continuity bound is what makes a resume-after-idle a fresh
        baseline rather than a witnessed change - but it measures the gap
        between SIGHTINGS, and a frozen state is re-sighted on every ~1s tick.
        So _lastPlayingSeenAt never aged, the hours of idleness were invisible,
        and the eventual resume onto a different track read as a transition the
        feed had failed to record: a full re-login at the exact moment someone
        started listening, which is the bug the continuity bound was added for.

        getNowPlaying already refuses this same state as not-real-playback; the
        staleness observer did not."""
        listener, onStale = self._listener(), MagicMock()

        #< two ticks of a frozen state, close enough together that the second
        #  would refresh the first's sighting
        self._poll(listener, 200.0, _frozenState(self.TRACK_A), onStale)
        self._poll(listener, 220.0, _frozenState(self.TRACK_A), onStale)
        #< the resume, within the continuity window of that last frozen tick
        self._poll(listener, 240.0, _playingState(self.TRACK_B), onStale)
        self._poll(listener,
                   100.0 + LISTENER_STALE_TIMEOUT_SECONDS
                   + LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS + 1,
                   _playingState(self.TRACK_B), onStale)

        for call in onStale.call_args_list:
            self.assertNotEqual(STALE_REASON_UNRECORDED_PLAYBACK, call.kwargs.get("reason"),
                                "a frozen connect state was counted as a witnessed track change")

    def test_a_real_playback_sighting_still_counts(self):
        """Guards the test above: the frozen filter must not swallow the
        genuine evidence this whole check exists to gather."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, 210.0, _playingState(self.TRACK_B), onStale)
        self._poll(listener,
                   100.0 + LISTENER_STALE_TIMEOUT_SECONDS
                   + LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS + 1,
                   _playingState(self.TRACK_B), onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_UNRECORDED_PLAYBACK)

    def test_a_change_the_feed_recorded_does_not_arm_the_evidence(self):
        """The ORDINARY transition, which must not read as unrecorded playback.

        current_user_recently_played() is a local in-process buffer (see
        Database/Spotify/client.py), and the poll loop writes the connect state
        for the INCOMING track before _applyStateToTracking appends the
        OUTGOING one. So the tick that first sees a finished track's feed entry
        already reads its replacement - and skipping the playback observation
        on that tick (the feed-change branch returns early) left
        _lastPlayingUri on the finished track. The next quiet tick then
        registered the transition with a timestamp AFTER the feed had already
        moved, which is exactly _staleFeedBrokenReason's "a play finished and
        never arrived" arm.

        So every ordinary track change armed it, and the first 30-minute quiet
        stretch after one - someone stopping listening, or a long track -
        rebuilt the session: the per-user rebuild loop this whole check exists
        to prevent, plus the risk of killing the in-flight track() lookup for
        the play it was protecting."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        #< the transition: A's entry lands in the feed on the same tick the
        #  connect state already names B
        listener.sp.current_user_recently_played.return_value = [{"played_at": 1}, {"played_at": 2}]
        self._poll(listener, 201.0, _playingState(self.TRACK_B), onStale)
        #< the next quiet tick, well inside the continuity window
        self._poll(listener, 202.0, _playingState(self.TRACK_B), onStale)
        #< then listening simply stops, past the stale timeout and its grace
        self._poll(listener, 201.0 + LISTENER_STALE_TIMEOUT_SECONDS
                   + LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS + 1,
                   _playingState(self.TRACK_B), onStale)

        onStale.assert_not_called()

    def test_the_catch_up_grace_defers_the_hard_ceiling_too(self):
        """The grace exists because a finished play is normally still resolving
        its metadata when the track change is first seen, and rebuilding inside
        that window throws the play away. It deferred the 30-minute verdict but
        not the 6-hour ceiling, so a change witnessed seconds before the ceiling
        fell through to STALE_REASON_HARD_CEILING and rebuilt anyway -
        Listener.stop() joins for 5s and then closes sp, killing the in-flight
        track() lookup with "Session is closed".

        The exact loss the grace was added to prevent, reached by the one path
        it did not cover. Bounded: the deferral can only last as long as the
        grace itself."""
        listener, onStale = self._listener(), MagicMock()
        pastHardCeiling = 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1

        #< the change is witnessed just before the ceiling is crossed, so its
        #  grace is still running when the ceiling check runs
        self._poll(listener, pastHardCeiling - 10, _playingState(self.TRACK_A), onStale)
        self._poll(listener, pastHardCeiling - 5, _playingState(self.TRACK_B), onStale)
        stillRunning = self._poll(listener, pastHardCeiling,
                                  _playingState(self.TRACK_B), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_the_ceiling_still_fires_once_the_grace_has_run_out(self):
        """The deferral is bounded by the grace, not open-ended - a feed that
        really is wedged must still be recycled."""
        listener, onStale = self._listener(), MagicMock()
        pastHardCeiling = 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1

        self._poll(listener, pastHardCeiling - 10, _playingState(self.TRACK_A), onStale)
        self._poll(listener, pastHardCeiling - 5, _playingState(self.TRACK_B), onStale)
        self._poll(listener, pastHardCeiling + LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS + 1,
                   _playingState(self.TRACK_B), onStale)

        #< the evidence outlived its grace, so it is the diagnosis - not the ceiling
        onStale.assert_called_once_with(reason=STALE_REASON_UNRECORDED_PLAYBACK)

    def test_an_idle_account_past_the_ceiling_is_still_recycled(self):
        """No witnessed change means no grace to run, so nothing defers."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1,
                   {"is_playing": False}, onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_HARD_CEILING)

    def test_a_failed_session_validation_names_its_reason(self):
        listener, onStale = self._listener(), MagicMock()
        listener._validateCurrentUser = MagicMock(return_value=False)

        listener._checkOnce(MagicMock(), onStale=onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_VALIDATION_FAILED)

    def test_a_fresh_push_subscription_defers_the_hard_ceiling(self):
        """An idle push-mode listener whose subscription was re-proven within
        LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS is demonstrably healthy - a
        successful re-PUT re-registers the subscription and runs the returned
        cluster through track detection, so the wedged-subscription failure the
        ceiling bounds cannot be hiding. Recycling it anyway was a pointless
        full re-login per user per 6 hours (live app.log 2026-08-01 to 08-04)."""
        listener, onStale = self._listener(), MagicMock()
        pastHardCeiling = 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1
        listener.sp.lastPlayedManager.subscriptionRenewedAt = pastHardCeiling - 60

        stillRunning = self._poll(listener, pastHardCeiling, {"is_playing": False}, onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_a_stale_subscription_stamp_lets_the_ceiling_fire(self):
        """A stamp the push loop stopped renewing has stopped vouching - the
        loop either died or its re-subscribes are failing, and past the ceiling
        that quiet feed is recycled exactly as before."""
        listener, onStale = self._listener(), MagicMock()
        pastHardCeiling = 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1
        listener.sp.lastPlayedManager.subscriptionRenewedAt = (
            pastHardCeiling - LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS - 1)

        stillRunning = self._poll(listener, pastHardCeiling, {"is_playing": False}, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once_with(reason=STALE_REASON_HARD_CEILING)

    def test_broken_evidence_beats_a_fresh_subscription(self):
        """A track change the feed never recorded is direct evidence of a
        broken session - no liveness stamp may explain that away."""
        listener, onStale = self._listener(), MagicMock()
        pastHardCeiling = 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1
        listener.sp.lastPlayedManager.subscriptionRenewedAt = pastHardCeiling - 60

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, 210.0, _playingState(self.TRACK_B), onStale)  #< within the continuity window
        stillRunning = self._poll(listener, pastHardCeiling, _playingState(self.TRACK_B), onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once_with(reason=STALE_REASON_UNRECORDED_PLAYBACK)

    def test_an_auth_error_names_its_reason(self):
        listener, onStale = self._listener(), MagicMock()
        listener._stop_event = threading.Event()
        listener.contaminationDetected = False
        listener._checkOnce = MagicMock(side_effect=RuntimeError("boom"))

        with patch("Database.Listeners.spotifyListener._is_auth_error", return_value=True):
            listener.startListener(MagicMock(), onStale=onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_AUTH_ERROR)
        self.assertFalse(listener.run)


def _playingState(trackUri, isPaused=False):
    return {"is_playing": True, "is_paused": isPaused, "track": {"uri": trackUri}}


_FROZEN_TRACK_DURATION_MS = 180_000


def _frozenState(trackUri):
    """A connect state stuck at is_playing:true - the track's whole duration
    (and then some) has elapsed since anything last updated it.

    The same shape getNowPlaying already refuses via NOW_PLAYING_STALE_GRACE_MS:
    "a 'playing' track whose duration has fully elapsed since the last
    connect-state update is a frozen/stale feed, not a real playback"."""
    import time as _realTime
    longAgoMs = int(_realTime.time() * 1000) - (_FROZEN_TRACK_DURATION_MS * 20)
    return {
        "is_playing": True,
        "is_paused": False,
        "track": {"uri": trackUri},
        "duration": _FROZEN_TRACK_DURATION_MS,
        "position_as_of_timestamp": 0,
        "timestamp": longAgoMs,
    }


class TestTheFrozenGraceIsMillisecondsAndMatchesItsSibling(unittest.TestCase):
    """CONNECT_STATE_FROZEN_GRACE_MS's unit and its cross-module twin.

    Its own comment says "same rule and same grace as Database.getNowPlaying's
    NOW_PLAYING_STALE_GRACE_MS" - two constants in two modules that have to
    agree, with nothing asserting it. The comment that shipped beside it also
    said the value was "spelled here in seconds because this file works in
    them", which it is not: the name says MS, and _connectStateIsFrozen
    compares it against time.time() * 1000. Acting on that comment - editing
    60_000 to 60 - is the plausible mistake, and it would call a track frozen
    one second after it ended.

    So the guard is behavioural rather than a bare equality on the number: it
    pins the unit through the comparison that actually uses it."""

    def test_the_two_graces_are_one_value(self):
        from Database.Listeners.spotifyListener import CONNECT_STATE_FROZEN_GRACE_MS
        from Database.database import Database

        self.assertEqual(CONNECT_STATE_FROZEN_GRACE_MS, Database.NOW_PLAYING_STALE_GRACE_MS,
                         "the frozen-state grace and getNowPlaying's must stay the same rule")

    def test_a_track_just_past_its_duration_is_not_frozen_yet(self):
        """The unit, stated as behaviour. A grace of 60 rather than 60_000
        would make this True - and with it most of the ordinary gap between one
        track ending and the next one starting."""
        from Database.Listeners.spotifyListener import _connectStateIsFrozen
        import time as _realTime

        oneSecondPast = {
            "is_playing": True,
            "is_paused": False,
            "duration": _FROZEN_TRACK_DURATION_MS,
            "position_as_of_timestamp": _FROZEN_TRACK_DURATION_MS,
            "timestamp": int(_realTime.time() * 1000) - 1_000,
        }

        self.assertFalse(_connectStateIsFrozen(oneSecondPast))

    def test_a_genuinely_frozen_state_still_reads_as_frozen(self):
        """Guards the test above: a grace wide enough to pass it by never
        firing would be no grace at all."""
        from Database.Listeners.spotifyListener import _connectStateIsFrozen

        self.assertTrue(_connectStateIsFrozen(_frozenState("spotify:track:t1")))


class TestStaleFeedIdleDetection(unittest.TestCase):
    """A quiet recently-played feed used to mean "the session died", so a
    listener was rebuilt every 30 minutes whether or not anyone was listening -
    1,270 stale reconnects over 11 days, spread evenly across all 24 hours
    while actual plays vary 100x between night and day. The feed only changes
    when a track finishes, so silence is the normal state of an idle account.
    What separates the two is the connect state: it says whether anything is
    playing at all, and whether a track change happened that the feed then
    failed to record."""

    TRACK_A = "spotify:track:aaaaaaaaaaaaaaaaaaaaaa"
    TRACK_B = "spotify:track:bbbbbbbbbbbbbbbbbbbbbb"

    def _poll(self, listener, now, state, onStale):
        listener.sp.lastPlayedManager.manager._state = state
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=now):
            return listener._checkOnce(MagicMock(), onStale=onStale)

    def _listener(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        return listener

    def _pastTimeout(self, extra=1):
        return 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + extra

    def test_nothing_playing_is_idle_not_stale(self):
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), {"is_playing": False}, onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_paused_playback_is_idle_not_stale(self):
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(),
                                  _playingState(self.TRACK_A, isPaused=True), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_one_long_track_playing_throughout_is_not_stale(self):
        """The feed only gains an entry when a track ENDS, so a 40-minute mix
        legitimately produces nothing for the whole window."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_A), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_a_track_change_the_feed_never_recorded_is_stale(self):
        """The genuine failure this watchdog exists for: playback moved on, so
        the finished track should have reached the feed - and didn't."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, 210.0, _playingState(self.TRACK_B), onStale)  #< within the continuity window
        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_B), onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_first_sighting_of_a_track_is_not_a_change(self):
        """A listener rebuilt mid-track sees that track for the first time -
        that is not evidence anything was missed, or every rebuild would
        immediately justify the next one."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_A), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_non_track_playback_is_not_a_track_change(self):
        """Ads and podcast episodes never reach the recently-played feed, so
        moving between them proves nothing about the feed's health."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState("spotify:episode:aaaaaaaaaaaaaaaaaaaaaa"), onStale)
        stillRunning = self._poll(listener, self._pastTimeout(),
                                  _playingState("spotify:ad:bbbbbbbbbbbbbbbbbbbbbb"), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_resuming_after_an_idle_gap_with_a_new_track_is_not_a_change(self):
        """The deterministic misfire this guards against: the last-playing track
        survives an idle gap, so pressing play on a DIFFERENT track after a
        30-minute break read as "a play that never arrived" and rebuilt the
        session at the exact moment listening resumed. A sighting across a gap
        wider than LISTENER_PLAYBACK_CONTINUITY_SECONDS is a fresh baseline -
        whatever ended before the gap was the feed's to record back then."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, 200.0, _playingState(self.TRACK_A), onStale)
        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_B), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_a_change_just_witnessed_gets_the_feed_a_grace_window(self):
        """A 40-minute mix ends and the next track starts: the mix's play is
        still in the callback's metadata fetch when the tick lands, so the feed
        legitimately lags the observed change by seconds (worst case ~105s
        under the shared limiter). Declaring the session broken in that window
        rebuilt it mid-recording and could lose the play for good."""
        listener, onStale = self._listener(), MagicMock()
        mixEndsAt = self._pastTimeout()

        self._poll(listener, mixEndsAt - 2.0, _playingState(self.TRACK_A), onStale)
        stillRunning = self._poll(listener, mixEndsAt, _playingState(self.TRACK_B), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_a_change_the_feed_never_caught_up_with_is_stale_after_the_grace(self):
        """The grace defers the verdict; it must not disable it."""
        listener, onStale = self._listener(), MagicMock()
        changeAt = self._pastTimeout()

        self._poll(listener, changeAt - 2.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, changeAt, _playingState(self.TRACK_B), onStale)
        stillRunning = self._poll(listener, changeAt + LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS + 1,
                                  _playingState(self.TRACK_B), onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_track_churn_does_not_keep_rearming_the_grace(self):
        """A genuinely dead feed under constant listening keeps producing fresh
        changes; the grace runs from the FIRST change the feed never recorded,
        or the verdict would defer forever."""
        listener, onStale = self._listener(), MagicMock()
        firstChangeAt = self._pastTimeout()
        churnStep = LISTENER_PLAYBACK_CONTINUITY_SECONDS - 5

        self._poll(listener, firstChangeAt - 2.0, _playingState(self.TRACK_A), onStale)
        self._poll(listener, firstChangeAt, _playingState(self.TRACK_B), onStale)
        for step in (1, 2, 3, 4):  #< 4 x 25s of churn, all inside the 120s grace
            track = self.TRACK_A if step % 2 else self.TRACK_B
            self._poll(listener, firstChangeAt + step * churnStep, _playingState(track), onStale)
        stillRunning = self._poll(listener,
                                  firstChangeAt + LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS + 1,
                                  _playingState(self.TRACK_A), onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_a_change_after_a_feed_update_rearms_the_grace(self):
        """Once the feed catches up, the next unrecorded change starts a fresh
        grace window instead of inheriting a long-expired anchor."""
        listener = self._listener()
        listener._lastChangeTime = 1000.0           #< the feed moved after the old pending change...
        listener._lastPlayingChangeTime = 900.0
        listener._firstUnarrivedChangeTime = 500.0  #< ...whose grace anchor is long expired
        listener._lastPlayingUri = self.TRACK_A
        listener._lastPlayingSeenAt = 1000.0
        listener.sp.lastPlayedManager.manager._state = _playingState(self.TRACK_B)

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=1010.0):
            listener._observePlaybackForStaleness()
        graceStillRunning = 1010.0 + LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS - 1
        with patch("Database.Listeners.spotifyListener.time.monotonic",
                   return_value=graceStillRunning):
            self.assertFalse(listener._staleFeedBrokenReason())

    def test_track_change_already_reflected_in_the_feed_is_not_stale(self):
        """The change was observed BEFORE the feed's last update, i.e. the feed
        did record it - only a change the feed never caught up with counts."""
        listener, onStale = self._listener(), MagicMock()
        listener._lastPlayingUri = self.TRACK_A
        listener._lastPlayingChangeTime = 50.0  #< before _lastChangeTime (100.0)

        stillRunning = self._poll(listener, self._pastTimeout(), _playingState(self.TRACK_A), onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_missing_connect_state_still_rebuilds(self):
        """In POLL mode, no connect state AND no proof the tick still runs
        means the websocket tick that feeds it isn't running either - no
        evidence of life, so keep the old behaviour. The two stamps below are
        the exceptions."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), None, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_missing_connect_state_on_a_renewing_poll_tick_is_idle_not_stale(self):
        """An account with no live Connect session answers a SUCCESSFUL PUT
        with no player_state, so poll mode has no connect state either - and
        reading that as death rebuilt the session every 30 minutes for a user
        who simply wasn't casting anything. A renewal stamp this fresh is
        positive proof the tick is alive, which absence never was."""
        listener, onStale = self._listener(), MagicMock()
        now = self._pastTimeout()
        listener.sp.lastPlayedManager.manager.stateRenewalSucceededAt = now - 10

        stillRunning = self._poll(listener, now, None, onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_missing_connect_state_with_an_aged_poll_renewal_still_rebuilds(self):
        """Same stamp-not-flag rule as the push channel: a tick that stopped
        renewing has stopped vouching."""
        listener, onStale = self._listener(), MagicMock()
        now = self._pastTimeout()
        listener.sp.lastPlayedManager.manager.stateRenewalSucceededAt = (
            now - LISTENER_POLL_RENEWAL_FRESH_SECONDS - 1)

        stillRunning = self._poll(listener, now, None, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_a_non_numeric_poll_renewal_stamp_is_not_evidence_of_life(self):
        """Read through the same cross-module getattr chain as the push stamps,
        so it gets the same type check rather than arithmetic on trust."""
        listener, onStale = self._listener(), MagicMock()
        listener.sp.lastPlayedManager.manager.stateRenewalSucceededAt = MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), None, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_a_missing_connect_state_names_its_own_reason(self):
        """"Unrecorded playback" misstates this evidence: nothing was observed
        playing at all. The ledger on /admin is only worth reading if the
        reasons are distinguishable."""
        listener, onStale = self._listener(), MagicMock()

        self._poll(listener, self._pastTimeout(), None, onStale)

        onStale.assert_called_once_with(reason=STALE_REASON_NO_CONNECT_STATE)

    def test_missing_connect_state_on_a_live_push_channel_is_idle_not_stale(self):
        """Push mode has no tick: _state is only written when Spotify pushes a
        cluster, so an account nobody is listening to legitimately has none at
        all. Reading that as a dead session rebuilt the whole spotapi session
        every 30 minutes per user - the regression this class exists to prevent,
        arriving through a door it didn't cover (live app.log 2026-08-01)."""
        listener, onStale = self._listener(), MagicMock()
        now = self._pastTimeout()
        listener.sp.lastPlayedManager.pushChannelAliveAt = now - 60

        stillRunning = self._poll(listener, now, None, onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()

    def test_missing_connect_state_with_an_aged_push_stamp_still_rebuilds(self):
        """A stamp is only evidence while it is fresh: a push thread that died
        leaves its last one behind, and a flag would keep vouching for it."""
        listener, onStale = self._listener(), MagicMock()
        now = self._pastTimeout()
        listener.sp.lastPlayedManager.pushChannelAliveAt = now - LISTENER_PUSH_CHANNEL_ALIVE_SECONDS - 1

        stillRunning = self._poll(listener, now, None, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_a_non_numeric_push_stamp_is_not_evidence_of_life(self):
        """Read through a getattr chain across two modules, so the value is
        whatever the other side left there - it must never be arithmetic on
        trust (a MagicMock here used to be truthy, and subtracting it raises)."""
        listener, onStale = self._listener(), MagicMock()
        listener.sp.lastPlayedManager.pushChannelAliveAt = MagicMock()

        stillRunning = self._poll(listener, self._pastTimeout(), None, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_the_hard_ceiling_still_rebuilds_a_live_push_listener(self):
        """The push exemption widens the idle window, it does not remove the
        backstop: a channel delivering pongs while its subscription is wedged
        is exactly what the hard ceiling is for."""
        listener, onStale = self._listener(), MagicMock()
        now = 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1
        listener.sp.lastPlayedManager.pushChannelAliveAt = now - 60

        stillRunning = self._poll(listener, now, None, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()
        self.assertGreater(LISTENER_STALE_HARD_TIMEOUT_SECONDS, LISTENER_PUSH_CHANNEL_ALIVE_SECONDS)

    def test_hard_ceiling_rebuilds_even_when_idle(self):
        """The one failure the idle check cannot see is a connect state that
        keeps answering while its own tick is wedged, so a quiet feed still
        gets recycled eventually - just hours apart instead of every 30
        minutes."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS + 1,
                                  {"is_playing": False}, onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_idle_listener_keeps_polling_below_the_ceiling(self):
        """Sanity bound on the ceiling: an idle account is left alone for hours,
        not minutes."""
        listener, onStale = self._listener(), MagicMock()

        stillRunning = self._poll(listener, 100.0 + LISTENER_STALE_HARD_TIMEOUT_SECONDS - 60,
                                  {"is_playing": False}, onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()
        self.assertGreater(LISTENER_STALE_HARD_TIMEOUT_SECONDS, LISTENER_STALE_TIMEOUT_SECONDS)

    def test_an_active_feed_never_reaches_the_stale_check(self):
        """Regression guard: the playback observation must not disturb the
        normal path where the feed IS changing."""
        listener, onStale = self._listener(), MagicMock()
        listener.sp.current_user_recently_played.return_value = [{"played_at": 1}, {"played_at": 2}]
        callback = MagicMock()

        listener.sp.lastPlayedManager.manager._state = _playingState(self.TRACK_B)
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=self._pastTimeout()):
            stillRunning = listener._checkOnce(callback, onStale=onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()
        callback.assert_called_once()
        self.assertEqual(listener._lastChangeTime, self._pastTimeout())


class TestAuthErrorDetection(unittest.TestCase):
    def test_loginerror_is_detected_as_auth_error(self):
        exc = Exception("spotapi.exceptions.errors.LoginError: Could not GET ...")
        self.assertTrue(_is_auth_error(exc))

    def test_401_status_is_detected_as_auth_error(self):
        exc = Exception("HTTP 401 Unauthorized")
        self.assertTrue(_is_auth_error(exc))

    def test_403_status_is_detected_as_auth_error(self):
        exc = Exception("HTTP 403 Forbidden")
        self.assertTrue(_is_auth_error(exc))

    def test_expired_session_is_detected_as_auth_error(self):
        exc = Exception("Session expired")
        self.assertTrue(_is_auth_error(exc))

    def test_invalid_token_is_detected_as_auth_error(self):
        exc = Exception("Invalid access token")
        self.assertTrue(_is_auth_error(exc))

    def test_503_error_is_not_detected_as_auth_error(self):
        exc = Exception("HTTP 503 Service Unavailable")
        self.assertFalse(_is_auth_error(exc))

    def test_timeout_error_is_not_detected_as_auth_error(self):
        exc = Exception("Connection timeout")
        self.assertFalse(_is_auth_error(exc))


class TestRateLimitErrorDetection(unittest.TestCase):
    """Characterization of _is_rate_limit_error (the transient bucket), which
    had no direct tests before. Pins today's string heuristic so a later
    narrowing pass is a conscious, test-visible change."""

    def test_429_is_transient(self):
        self.assertTrue(_is_rate_limit_error(Exception("HTTP 429 Too Many Requests")))

    def test_rate_limit_phrase_is_transient(self):
        self.assertTrue(_is_rate_limit_error(Exception("Rate limit exceeded, slow down")))

    def test_malformed_json_wording_is_transient(self):
        # The "json" substring is what today catches Spotify answering with a
        # non-JSON bot-check page instead of the profile JSON.
        self.assertTrue(_is_rate_limit_error(Exception("Invalid JSON in response body")))

    def test_503_is_not_transient(self):
        self.assertFalse(_is_rate_limit_error(Exception("HTTP 503 Service Unavailable")))

    def test_timeout_is_not_transient(self):
        self.assertFalse(_is_rate_limit_error(Exception("Connection timeout")))


class TestClassifyListenerError(unittest.TestCase):
    """classifyListenerError is the single seam behind both predicates; its
    (isAuth, isTransient) pair must stay INDEPENDENT - some errors are both,
    and call-site precedence relies on that rather than one flag winning."""

    def test_pure_auth_error(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 401 Unauthorized")), (True, False))

    def test_pure_transient_error(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 429 Too Many Requests")), (False, True))

    def test_neither_bucket(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 503 Service Unavailable")), (False, False))

    def test_error_that_is_both_auth_and_transient(self):
        # A rate-limited login failure matches both; the flags must stay
        # independent so each call site's own precedence still applies.
        self.assertEqual(classifyListenerError(Exception("LoginError: 429 rate limited")), (True, True))

    def test_predicates_are_thin_wrappers_over_the_pair(self):
        exc = Exception("Invalid access token")
        isAuth, isTransient = classifyListenerError(exc)
        self.assertEqual(_is_auth_error(exc), isAuth)
        self.assertEqual(_is_rate_limit_error(exc), isTransient)


class TestClassifyRealSpotapiExceptions(unittest.TestCase):
    """The classifier must handle actual spotapi exception instances, not just
    Exception('...text...'): LoginError is classified by its type name even
    when its message carries no auth keyword."""

    def test_spotapi_loginerror_is_auth_by_type_name(self):
        from spotapi.exceptions.errors import LoginError
        # Message has NO auth keyword - the type name is what classifies it.
        self.assertEqual(classifyListenerError(LoginError("Could not GET recently played")), (True, False))

    def test_spotapi_transport_requesterror_is_neither(self):
        # RequestError("Failed to complete request.", error=<curl detail>) is
        # what TLSClient.build_request raises for a reset/timeout: no status
        # anywhere in the message, so neither bucket - and it must STAY that
        # way, or startListener would open the process-wide backoff for a
        # local outage. _isNoVerdictError handles it where it matters
        # (tests/test_listener_no_verdict_errors.py).
        self.assertEqual(classifyListenerError(realTransportRequestError()), (False, False))

    def test_spotapi_429_on_the_profile_endpoint_is_both(self):
        # TLSClient.get raises LoginError("Could not GET <url>. Status Code:
        # 429") - auth by type name, transient by the status in the message.
        self.assertEqual(classifyListenerError(realProfileStatusError(429)), (True, True))

    def test_spotapi_503_on_the_profile_endpoint_is_auth_by_type_name_only(self):
        # The classifier alone reads a Spotify 5xx as an auth failure - which
        # is exactly why isLoggedIn()/_validateCurrentUser consult
        # _isNoVerdictError before it.
        self.assertEqual(classifyListenerError(realProfileStatusError(503)), (True, False))


class TestClassificationDiagnostic(unittest.TestCase):
    """The FLASK_DEBUG-gated diagnostic that records the concrete exception type
    at each classification - the observability a real misclassification report
    needs before the heuristics can be safely narrowed. Off by default so it
    never spams production logs."""

    _LOGGER = "Database.Listeners.spotifyListener"

    def test_logs_type_and_flags_when_flask_debug_enabled(self):
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=True):
            with self.assertLogs(self._LOGGER, level="INFO") as cm:
                classifyListenerError(Exception("HTTP 401 Unauthorized"))
        joined = "\n".join(cm.output)
        self.assertIn("isAuth=True", joined)
        self.assertIn("isTransient=False", joined)
        self.assertIn("builtins.Exception", joined)   #< the fully-qualified type

    def test_silent_when_flask_debug_disabled(self):
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        moduleLogger = logging.getLogger(self._LOGGER)
        moduleLogger.addHandler(handler)
        try:
            with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=False):
                classifyListenerError(Exception("HTTP 401 Unauthorized"))
        finally:
            moduleLogger.removeHandler(handler)
        self.assertEqual([r for r in records if "classified" in r.getMessage()], [])


class TestValidateCurrentUser(unittest.TestCase):
    def test_valid_user_returns_true(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user1"}
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertTrue(listener._validateCurrentUser())

    def test_mismatched_user_returns_false(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user2"}
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertFalse(listener._validateCurrentUser())

    def test_auth_error_returns_false_and_does_not_raise(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.side_effect = Exception("HTTP 401 Unauthorized")
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertFalse(listener._validateCurrentUser())

    def test_transient_error_bubbles_up(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.side_effect = Exception("HTTP 503 Service Unavailable")
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            with self.assertRaises(Exception) as ctx:
                listener._validateCurrentUser()
        self.assertIn("503", str(ctx.exception))

    def test_first_check_runs_even_with_a_low_monotonic_clock(self):
        """Regression test: _last_user_validation_time must start as None
        ("never validated"), not 0, so the very first check always performs
        a real validation - even on a host where time.monotonic() itself is
        still small (e.g. shortly after boot), which previously made a
        freshly constructed Listener look like it had already validated
        "recently" and silently return the unvalidated cached default."""
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user1"}

        lowUptimeMonotonic = 1.0  # smaller than USER_VALIDATION_CACHE_SECONDS
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=lowUptimeMonotonic):
            listener._validateCurrentUser()

        listener.sp.current_user.assert_called_once()


class TestValidationErrorCooldown(unittest.TestCase):
    """A refused profile call now starts its own cooldown.

    Before, the transient branch raised without recording anything, so the
    freshness cache stayed empty and the very next poll after the loop's 60s
    backoff called current_user() again - a 60-second cadence in place of a
    5-minute one, at the exact moment Spotify was pushing back."""

    _TRANSIENT = "Invalid JSON (Status: 200, Type: str)"

    def _listener(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        return listener

    def _validateAt(self, listener, when):
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=when):
            return listener._validateCurrentUser()

    def _failOnceAt(self, listener, when):
        listener.sp.current_user.side_effect = Exception(self._TRANSIENT)
        with self.assertRaisesRegex(Exception, "Invalid JSON"):
            self._validateAt(listener, when)
        listener.sp.current_user.reset_mock()
        listener.sp.current_user.side_effect = None
        listener.sp.current_user.return_value = {"id": "user1"}

    def test_a_refusal_still_raises_so_the_caller_backs_off(self):
        listener = self._listener()
        listener.sp.current_user.side_effect = Exception(self._TRANSIENT)

        with self.assertRaisesRegex(Exception, "Invalid JSON"):
            self._validateAt(listener, _MONOTONIC_NOW)

    def test_the_next_poll_inside_the_cooldown_makes_no_request(self):
        """This is the whole fix: the poll that follows the 60s backoff must
        not walk straight back into the endpoint that just refused."""
        listener = self._listener()
        self._failOnceAt(listener, _MONOTONIC_NOW)

        justAfterTheBackoff = _MONOTONIC_NOW + RATE_LIMIT_ERROR_BACKOFF_SECONDS + 1
        self._validateAt(listener, justAfterTheBackoff)

        listener.sp.current_user.assert_not_called()

    def test_the_cooldown_keeps_reporting_the_last_known_answer(self):
        listener = self._listener()
        listener.sp.current_user.return_value = {"id": "user2"}   #< a real mismatch
        self.assertFalse(self._validateAt(listener, _MONOTONIC_NOW))

        laterFailure = _MONOTONIC_NOW + USER_VALIDATION_CACHE_SECONDS + 1
        self._failOnceAt(listener, laterFailure)

        self.assertFalse(self._validateAt(listener, laterFailure + 1))

    def test_validation_resumes_once_the_cooldown_expires(self):
        """Shorter than the success cache on purpose - recovery is worth
        noticing promptly."""
        listener = self._listener()
        self._failOnceAt(listener, _MONOTONIC_NOW)

        self.assertTrue(self._validateAt(
            listener, _MONOTONIC_NOW + USER_VALIDATION_ERROR_COOLDOWN_SECONDS + 1))
        listener.sp.current_user.assert_called_once()

    def test_the_cooldown_is_shorter_than_the_success_cache(self):
        self.assertLess(USER_VALIDATION_ERROR_COOLDOWN_SECONDS, USER_VALIDATION_CACHE_SECONDS)

    def test_a_successful_check_clears_the_cooldown(self):
        """Otherwise a stale refusal would keep suppressing checks long after
        the endpoint recovered."""
        listener = self._listener()
        self._failOnceAt(listener, _MONOTONIC_NOW)

        recoveredAt = _MONOTONIC_NOW + USER_VALIDATION_ERROR_COOLDOWN_SECONDS + 1
        self._validateAt(listener, recoveredAt)

        self.assertIsNone(listener._last_validation_error_time)

    def test_an_auth_error_starts_no_cooldown(self):
        """An auth failure is an answer, not a refusal - it must keep returning
        False on every poll so the reconnect fires."""
        listener = self._listener()
        listener.sp.current_user.side_effect = Exception("HTTP 401 Unauthorized")

        self.assertFalse(self._validateAt(listener, _MONOTONIC_NOW))
        self.assertIsNone(listener._last_validation_error_time)
        self.assertFalse(self._validateAt(listener, _MONOTONIC_NOW + 1))
        self.assertEqual(listener.sp.current_user.call_count, 2)


class TestLocalPauseIsNotSpotifyPushback(unittest.TestCase):
    """Regression, observed live at 2026-07-29 17:43.

    One real rate limit on laurateresaschoen opened the shared window. The next
    two listeners' profile calls were then refused by that window - and each
    reported it as its own brand-new rate limit, logging a WARNING, standing
    down 60s, and opening ANOTHER process-wide backoff. One event became three:
    the admin count inflated per user, the recorded cause was overwritten by
    whichever listener tripped last, and the window crept forward each time.

    A refusal from our own limiter is not evidence about Spotify. It is the
    backoff, so it must never re-arm it."""

    def _listener(self):
        listener = _bareListener()
        listener.user = "timorzipa"
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.side_effect = SpotifyLocallyRateLimitedError(
            "Spotify rate limit backoff in progress - skipped account-settings/profile")
        return listener

    def test_validation_answers_from_cache_instead_of_raising(self):
        listener = self._listener()

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertTrue(listener._validateCurrentUser())

    def test_validation_records_no_refusal_cooldown(self):
        """That cooldown exists to stop us pestering an endpoint that pushed
        back. This one never answered because it was never asked."""
        listener = self._listener()

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._validateCurrentUser()

        self.assertIsNone(listener._last_validation_error_time)

    def test_validation_logs_no_warning(self):
        listener = self._listener()
        moduleLogger = logging.getLogger("Database.Listeners.spotifyListener")
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        moduleLogger.addHandler(handler)
        try:
            with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
                listener._validateCurrentUser()
        finally:
            moduleLogger.removeHandler(handler)

        self.assertEqual([r for r in records if r.levelno >= logging.WARNING], [])

    def test_login_check_stays_logged_in_without_a_warning(self):
        listener = self._listener()
        listener.contaminationDetected = False
        listener.sp.isLoggedIn.return_value = True
        moduleLogger = logging.getLogger("Database.Listeners.spotifyListener")
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        moduleLogger.addHandler(handler)
        try:
            self.assertTrue(listener.isLoggedIn())
        finally:
            moduleLogger.removeHandler(handler)

        self.assertEqual([r for r in records if r.levelno >= logging.WARNING], [])

    def test_the_poll_loop_never_re_arms_the_window_it_tripped_over(self):
        from Database.rate_limit import SPOTIFY_LIMITER

        listener = _bareListener()
        listener.user = "7kevinegger"
        listener.contaminationDetected = False
        listener._stop_event = MagicMock()
        listener._stop_event.is_set.side_effect = [False, True]
        listener._checkOnce = MagicMock(side_effect=SpotifyLocallyRateLimitedError(
            "Spotify rate limit backoff in progress - skipped account-settings/profile"))

        listener.startListener(MagicMock())

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)
        listener._stop_event.wait.assert_any_call(LOCAL_PAUSE_RETRY_WAIT_SECONDS)

    def test_one_real_event_stays_one_event_across_every_listener(self):
        """The end-to-end shape of the 17:43 log: a genuine rate limit on one
        account, then two more listeners refused by the window it opened."""
        from Database.rate_limit import SPOTIFY_LIMITER

        real = _bareListener()
        real.user = "laurateresaschoen"
        real.contaminationDetected = False
        real._stop_event = MagicMock()
        real._stop_event.is_set.side_effect = [False, True]
        real._checkOnce = MagicMock(side_effect=Exception("Invalid JSON (Status: 200, Type: str)"))
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            real.startListener(MagicMock())

        for username in ("timorzipa", "7kevinegger"):
            refused = _bareListener()
            refused.user = username
            refused.contaminationDetected = False
            refused._stop_event = MagicMock()
            refused._stop_event.is_set.side_effect = [False, True]
            refused._checkOnce = MagicMock(side_effect=SpotifyLocallyRateLimitedError(
                "Spotify rate limit backoff in progress - skipped account-settings/profile"))
            refused.startListener(MagicMock())

        snapshot = SPOTIFY_LIMITER.snapshot()
        self.assertEqual(snapshot["backoffs"], 1)
        self.assertEqual(snapshot["lastReason"], RATE_LIMIT_REASON_LISTENER_POLL)

    def test_it_still_classifies_as_transient(self):
        """The substring classifier must keep bucketing it as transient - that
        is what stands the poll loop down rather than escalating to a
        reconnect. Which is exactly why the type check has to come first."""
        error = SpotifyLocallyRateLimitedError(
            "Spotify rate limit backoff in progress - skipped account-settings/profile")
        self.assertTrue(_is_rate_limit_error(error))
        self.assertFalse(_is_auth_error(error))


class TestWebApiIdentityCheckReplacesTheBrowserEndpoint(unittest.TestCase):
    """_validateCurrentUser's only tool is
    www.spotify.com/api/account-settings/v1/profile - Spotify's CONSUMER WEB
    front-end, and therefore bot-protected. Every "Oh nein!" HTML challenge in
    app.log came from that one endpoint at 0.2 requests/minute/user, while the
    connect-state API backend took 50x the volume without complaint. Volume was
    never what made it fail; being pointed at a browser endpoint was.

    The backfill's /v1/me lookup already asks the same question of the official
    OAuth API, so for a credentialed user it can carry the answer and the
    browser endpoint never needs to be called on a schedule."""

    def _listenerWithBackfill(self, apiUser, listenerEmail="alice@example.com"):
        listener = _bareListener()
        listener.email = listenerEmail
        listener.user = "alice"
        listener.get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt"})
        listener.get_backfill_enabled = None
        listener.process_backfill_page = None
        listener._lastWebApiPollTime = None
        listener._consecutiveScopeErrors = 0
        listener.on_scope_status_change = None
        listener.webApiRecentlyPlayed_Z1 = []

        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="token123"), \
             patch("Database.Listeners.spotifyListener._get_current_user_from_web_api",
                   return_value=apiUser), \
             patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api",
                   return_value=[]), \
             patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(MagicMock())
        return listener

    def _matchingApiUser(self):
        return {"id": "alice", "display_name": "Alice", "email": "alice@example.com"}

    def test_a_confirmed_api_identity_stops_the_browser_call(self):
        listener = self._listenerWithBackfill(self._matchingApiUser())

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW + 1):
            self.assertTrue(listener._validateCurrentUser())

        listener.sp.current_user.assert_not_called()

    def test_the_trust_window_outlives_the_backfill_interval(self):
        """The invariant that makes it work at all: if the window expired
        before the next /v1/me check, the browser endpoint would be reached in
        the gap and nothing would have been gained."""
        self.assertGreater(USER_VALIDATION_CACHE_SECONDS, WEB_API_POLL_INTERVAL_SECONDS)

    def test_an_api_response_without_an_email_proves_nothing(self):
        """The comparison never ran, so it must not suppress the real check -
        being lenient about a missing email is not the same as confirming."""
        listener = self._listenerWithBackfill(
            {"id": "alice", "display_name": "Alice", "email": ""})

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW + 1):
            listener._validateCurrentUser()

        listener.sp.current_user.assert_called_once()

    def test_a_listener_without_an_expected_email_proves_nothing(self):
        listener = self._listenerWithBackfill(self._matchingApiUser(), listenerEmail=None)

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW + 1):
            listener._validateCurrentUser()

        listener.sp.current_user.assert_called_once()

    def test_a_mismatch_never_marks_the_cookie_session_invalid(self):
        """A Web API mismatch means the OAuth CREDENTIALS point at another
        account. Rebuilding the cookie session cannot fix that, so it must not
        be reported as a failed session validation."""
        listener = self._listenerWithBackfill(
            {"id": "bob", "display_name": "Bob", "email": "bob@example.com"})

        self.assertIsNone(listener._last_user_validation_time)
        listener.sp.current_user.return_value = {"id": None}
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW + 1):
            self.assertTrue(listener._validateCurrentUser())

    def test_a_user_without_credentials_still_gets_validated(self):
        """No API credentials means no /v1/me to lean on - the cookie check is
        still the only identity source, just on a longer leash."""
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user1"}

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertTrue(listener._validateCurrentUser())

        listener.sp.current_user.assert_called_once()


class TestPollIntervalJitter(unittest.TestCase):
    """Every listener polling at exactly the same rate means two accounts that
    start near each other arrive together on every tick, forever. The shared
    limiter would then de-align them by blocking a thread; spreading them here
    means it rarely has to."""

    def test_the_interval_stays_within_the_declared_band(self):
        for _ in range(50):
            interval = _pollIntervalWithJitter()
            self.assertGreaterEqual(interval, LISTENER_POLL_INTERVAL_SECONDS)
            self.assertLessEqual(
                interval, LISTENER_POLL_INTERVAL_SECONDS + LISTENER_POLL_INTERVAL_JITTER_SECONDS)

    def test_separate_draws_do_not_all_land_on_the_same_value(self):
        self.assertGreater(len({_pollIntervalWithJitter() for _ in range(50)}), 1)

    def test_the_base_cadence_is_a_named_constant(self):
        """It used to be a bare `refreshInterval=6` default - the single
        biggest lever on how much Spotify traffic this app generates, with no
        name to find it by."""
        self.assertEqual(LISTENER_POLL_INTERVAL_SECONDS, 6)

    def _build(self, **kwargs):
        sp = MagicMock()
        sp.isLoggedIn.return_value = True
        sp.current_user.return_value = {"id": "u1", "email": None}
        sp.current_user_recently_played.return_value = []
        with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
            listener = Listener(cookiesFile="unused.json", **kwargs)
        return listener, sp

    def test_a_listener_polls_at_its_own_jittered_interval(self):
        listener, sp = self._build()

        sp.startRecentlyPlayedListener.assert_called_once_with(
            refreshInterval=listener.refreshInterval, logUser=listener.logUser)
        self.assertGreaterEqual(listener.refreshInterval, LISTENER_POLL_INTERVAL_SECONDS)
        self.assertLessEqual(
            listener.refreshInterval,
            LISTENER_POLL_INTERVAL_SECONDS + LISTENER_POLL_INTERVAL_JITTER_SECONDS)

    def test_an_explicit_interval_is_honoured_verbatim(self):
        """So a test can pin the cadence without fighting the jitter."""
        listener, sp = self._build(refreshInterval=2)

        self.assertEqual(listener.refreshInterval, 2)
        #< logUser rides along so the connect-state thread can be named after
        #  the user it belongs to (Database/Spotify/recentlyPlayed.start)
        sp.startRecentlyPlayedListener.assert_called_once_with(refreshInterval=2,
                                                               logUser=listener.logUser)


class TestRateLimitBackoffLogging(unittest.TestCase):
    """The routine rate-limit backoff is one line. The exception that caused it
    was already logged by the path that raised it (e.g. _validateCurrentUser's
    transient branch), and Spotify's rate-limit reply is an HTML fallback page,
    so repeating it here only doubled the noise. FLASK_DEBUG brings it back for
    the paths that raise without logging first."""

    _LOGGER = "Database.Listeners.spotifyListener"
    _ERROR_TEXT = "Invalid JSON (Status: 200, Type: str)"

    def _runOneRateLimitedIteration(self, user="7kevinegger"):
        listener = _bareListener()
        listener.user = user
        listener.contaminationDetected = False
        listener._stop_event = MagicMock()
        listener._stop_event.is_set.side_effect = [False, True]  #< one iteration, then stop
        listener._checkOnce = MagicMock(side_effect=Exception(self._ERROR_TEXT))

        with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
            listener.startListener(MagicMock())
        return listener, logCapture

    def test_routine_backoff_logs_one_line_without_the_exception(self):
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=False):
            listener, logCapture = self._runOneRateLimitedIteration()

        self.assertEqual(len(logCapture.output), 1)
        emitted = logCapture.output[0]
        self.assertIn("Rate limit error detected", emitted)
        self.assertIn(str(RATE_LIMIT_ERROR_BACKOFF_SECONDS), emitted)
        self.assertNotIn("Invalid JSON", emitted)
        # The backoff itself is unchanged - only the logging got quieter.
        listener._stop_event.wait.assert_any_call(RATE_LIMIT_ERROR_BACKOFF_SECONDS)

    def test_flask_debug_restores_the_exception_detail(self):
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=True):
            _, logCapture = self._runOneRateLimitedIteration()

        emitted = "\n".join(logCapture.output)
        self.assertIn("Rate limit error detected", emitted)
        self.assertIn(self._ERROR_TEXT, emitted)

    def test_the_backoff_line_names_the_user(self):
        """Which account hit the limit was the one thing these lines never
        said - with four listeners logging identically there was no way to
        tell one struggling account from an instance-wide problem."""
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=False):
            _, logCapture = self._runOneRateLimitedIteration(user="7kevinegger")

        self.assertIn("7kevinegger", "\n".join(logCapture.output))


class TestRateLimitPausesEveryListener(unittest.TestCase):
    """The listener's own 60s wait only ever paused this thread - whose sole
    rate-limited call is the 5-minutely _validateCurrentUser, since the
    recently-played read is a local cache hit. The traffic that actually
    provokes Spotify (~10 connect-state requests a minute PER USER) runs on
    spotapi's thread and never saw it. So the backoff that matters is the
    process-wide one."""

    _LOGGER = "Database.Listeners.spotifyListener"
    _ERROR_TEXT = "Invalid JSON (Status: 200, Type: str)"

    def _runOneRateLimitedIteration(self):
        listener = _bareListener()
        listener.user = "7kevinegger"
        listener.contaminationDetected = False
        listener._stop_event = MagicMock()
        listener._stop_event.is_set.side_effect = [False, True]  #< one iteration, then stop
        listener._checkOnce = MagicMock(side_effect=Exception(self._ERROR_TEXT))
        with self.assertLogs(self._LOGGER, level="WARNING"):
            listener.startListener(MagicMock())
        return listener

    def test_a_rate_limit_opens_a_process_wide_backoff(self):
        from Database.rate_limit import SPOTIFY_LIMITER
        from Database.Listeners.spotifyListener import RATE_LIMIT_REASON_LISTENER_POLL

        self._runOneRateLimitedIteration()

        snapshot = SPOTIFY_LIMITER.snapshot()
        self.assertEqual(snapshot["backoffs"], 1)
        self.assertEqual(snapshot["lastReason"], RATE_LIMIT_REASON_LISTENER_POLL)
        self.assertGreater(snapshot["backoffRemainingSeconds"], 0)

    def test_the_local_wait_is_kept_as_well(self):
        """Retrying validation inside a window we just opened would be pointless
        traffic, so this thread still stands down too."""
        from Database.Listeners.spotifyListener import RATE_LIMIT_ERROR_BACKOFF_SECONDS

        listener = self._runOneRateLimitedIteration()

        listener._stop_event.wait.assert_any_call(RATE_LIMIT_ERROR_BACKOFF_SECONDS)

    def test_an_ordinary_error_pauses_nothing(self):
        """Only rate limits are instance-wide. A plain listener bug must not
        stall every other user's tracking."""
        from Database.rate_limit import SPOTIFY_LIMITER

        listener = _bareListener()
        listener.contaminationDetected = False
        listener._stop_event = MagicMock()
        listener._stop_event.is_set.side_effect = [False, True]
        listener._checkOnce = MagicMock(side_effect=Exception("something else broke"))

        with self.assertLogs("Database.Listeners.spotifyListener", level="ERROR"):
            listener.startListener(MagicMock())

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)


if __name__ == "__main__":
    unittest.main()
