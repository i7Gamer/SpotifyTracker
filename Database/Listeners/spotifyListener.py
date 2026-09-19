# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import collections
import os
import logging
import random
import re
import signal
import threading
import time
from contextlib import contextmanager
from Database.db import WEB_API_BACKFILL_SOURCE
from Database.Spotify import Spotify
from Database.rate_limit import (
    SPOTIFY_LIMITER, SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS, SpotifyLocallyRateLimitedError,
    retryAfterSeconds,
)
from Database.utils import parseError, timeToInt, flaskDebugEnabled, truncateForLog
#< the connect-state string-number coercion, shared with the poll/push tracking
#  and Database.getNowPlaying rather than copied a third time; recentlyPlayed
#  imports nothing back from here, so no cycle (same reasoning as workers/listener.py)
from Database.Spotify.recentlyPlayed import _connectStateInt, _isSessionClosedError
from spotapi.exceptions.errors import RequestError as SpotapiRequestError

# A background thread's websocket ping (e.g. spotapi's keep_alive) can raise
# websockets.exceptions.ConnectionClosed/ConnectionAbortedError for many reasons -
# a graceful shutdown close, or the connection simply dropping. Either way it's
# already handled: patched_keep_alive (Database/patches.py) reconnects on a drop,
# and the stale-feed watchdog below rebuilds the whole listener if pings stay
# dead. So log one clean line instead of letting threading's default excepthook
# dump a raw traceback. Anything else (a real bug) still gets the default
# handler so it stays loud and visible.
import websockets.exceptions

logger = logging.getLogger(__name__)

# Whatever hook was installed when this module loaded - threading's own default
# unless the host (an embedding app, pytest's thread-exception plugin) put its
# own there first. Everything this hook doesn't recognize goes back to it
# untouched: installing ours process-wide must not cost an unrelated thread its
# reporting. Forwarding to sys.__excepthook__ instead, as this did, dropped the
# "Exception in thread <name>:" header that threading's handler writes - so a
# crash in any worker read like a main-thread crash - and printed a traceback
# for a thread's SystemExit, which threading deliberately ignores.
_PREVIOUS_EXCEPTHOOK = threading.excepthook


def _shutdown_exception_hook(args):
    """Log expected websocket-close exceptions from background threads instead of
    letting them print a raw traceback; forward anything else to the hook this
    one replaced."""
    exc = args.exc_value

    if isinstance(exc, (websockets.exceptions.ConnectionClosed, ConnectionAbortedError)):
        threadName = args.thread.name if args.thread is not None else "unknown"
        # This hook is process-global, so it can fire for any thread - state the
        # observed fact only, without promising a recovery the thread may not have
        # (the listener's own reconnect/stale-feed paths are what actually recover).
        logger.warning(
            "Background thread '%s' exited after a connection close (%s).",
            threadName, exc,
        )
        return

    # Otherwise, hand it back whole - the thread it came from included
    _PREVIOUS_EXCEPTHOOK(args)

threading.excepthook = _shutdown_exception_hook

LISTENER_STOP_JOIN_TIMEOUT_SECONDS = 5  #< bound how long shutdown waits for spotapi's background LastPlayed thread to exit

# Poll threads are named "<prefix><logUser>". An anonymous "Thread-17" in a
# thread dump says nothing about whose listener is still running after a
# shutdown that should have stopped it, and the test suite's leaked-thread
# guard recognizes per-user threads by name.
LISTENER_THREAD_NAME_PREFIX = "spotify-listener-"

# current_user_recently_played() doesn't actually poll - it just returns spotapi's
# websocket-fed local cache (see Database/Spotify/client.py's current_user_recently_played).
# That websocket can silently die (its own reconnect() call targets a method that
# doesn't exist on PlayerStatus - a bug in spotapi, not this code), after which the
# cache is frozen forever: no exception, no new items, nothing recorded, ever again,
# with the polling loop below none the wiser. If nothing has changed for this long,
# assume the feed is dead and ask the caller to rebuild the session rather than
# staying wedged silently until the process is restarted.
LISTENER_STALE_TIMEOUT_SECONDS = 30 * 60
# Ceiling on how long a quiet feed may be explained away as "this account is
# simply idle" (see _staleFeedBrokenReason). The idle check reads the connect
# state, so the one failure it cannot see is a connect state that keeps
# answering while its own tick is wedged; past this, a quiet feed is recycled
# regardless of what the state claims - UNLESS the push loop has re-proven
# its subscription within LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS, which is
# direct evidence that exact failure is absent (see _checkOnce). Before the
# deferral, every idle push-mode user cost a full re-login per 6 hours (live
# app.log 2026-08-01 to 08-04, rebuilds on the :44 dot).
LISTENER_STALE_HARD_TIMEOUT_SECONDS = 6 * 60 * 60

# A track change only counts as a WITNESSED transition when the previous
# sighting is at most this old. Sightings happen every quiet tick (~1s, on a
# state the poll tick refreshes every ~3s), so a wider gap means playback
# stopped or the state vanished in between - the next sighting is then a fresh
# baseline, not a change. Without this bound, _lastPlayingUri survived idle
# gaps: pressing play on a DIFFERENT track after a 30-minute break read as "a
# play that never arrived" and rebuilt the session at the exact moment
# listening resumed (one spurious full re-login per resume; the pre-gap
# track's play, if one ended there, was the feed's to record back then).
LISTENER_PLAYBACK_CONTINUITY_SECONDS = 30

# A witnessed change waits this long for the feed to catch up before it counts
# as broken evidence. The feed entry for a finished track only lands after the
# play callback resolves its metadata, which under the shared limiter can
# legitimately take ~3 acquire windows (TRACK_FETCH_MAX_RETRIES x
# SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS = 105s; local constant for the same
# import-avoidance reason as LISTENER_PUSH_CHANNEL_ALIVE_SECONDS below).
# Without the grace, the first change after a 30-minute-quiet feed - any track
# longer than the stale timeout - was judged unrecorded while its play was
# still in flight, and the rebuild threw that play away. Anchored on the FIRST
# change since the feed last moved (_firstUnarrivedChangeTime), so constant
# churn on a genuinely dead feed cannot keep re-arming it; a real failure is
# detected this much later, against a 30-minute staleness gate.
LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS = 120

# How fresh RecentlyPlayedManager.pushChannelAliveAt has to be to still count as
# "this listener is on a working push channel" (see _staleFeedBrokenReason).
#
# The push loop stamps it on every websocket frame and gives up on itself after
# 5 minutes with no frame of any kind, so a running loop can never be more than
# that behind; double it so a loop mid-fallback is not misread as dead. Kept as
# a local constant rather than imported from Database.Spotify.recentlyPlayed -
# that module is reached through getattr chains on purpose, and this file has no
# other reason to depend on it.
LISTENER_PUSH_CHANNEL_ALIVE_SECONDS = 10 * 60

# How fresh RecentlyPlayedManager.subscriptionRenewedAt - the push loop's last
# SUCCESSFUL connect-state re-subscribe - must be for the stale check's hard
# ceiling to defer (see _checkOnce). A successful re-PUT re-registers the
# subscription AND runs the returned cluster through track detection, so a
# stamp this fresh rules out the silently-wedged subscription the ceiling
# exists to bound.
#
# What bounds it is the push loop giving up on itself, NOT this number: the loop
# re-subscribes every 5 minutes and hands back to polling after 3 consecutive
# failures, and its finally clears subscriptionRenewedAt when it does - so the
# deferral is forfeited at ~15 minutes by the stamp going away, and this window
# only has to outlast that. It does, with room.
#
# (It was originally derived as "two 15-minute cadences plus slack, so one
# failed attempt doesn't forfeit but two consecutive ones do". That arithmetic
# died when the cadence became 5 minutes and the give-up path started clearing
# the stamp; the derivation is stated as a property in
# tests/test_recently_played_loop.py now, where the two modules' constants can
# be compared - here it is a local constant on purpose, for the same
# import-avoidance reason as LISTENER_PUSH_CHANNEL_ALIVE_SECONDS.)
LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS = 35 * 60

# How fresh the POLL tick's last successful connect-state renewal
# (PlayerStatus.stateRenewalSucceededAt, stamped in Database/patches.py) has to
# be to vouch for a connect state that is legitimately absent.
#
# A successful PUT whose cluster carries no player_state is an account with no
# live Connect session - poll mode's version of the idle case push mode already
# handles, and reading it as death rebuilt the session every 30 minutes for a
# user who simply wasn't casting anything. The tick renews every refreshInterval
# (single-digit seconds), so this only ages out when the tick has genuinely
# stopped; sized like LISTENER_PUSH_CHANNEL_ALIVE_SECONDS, with room for a
# limiter backoff and a reconnect ladder in between.
LISTENER_POLL_RENEWAL_FRESH_SECONDS = 10 * 60

# Why a rebuild was requested - short, stable strings passed to onStale and
# carried through _makeOnStaleCallback into the listener-session ledger on
# /admin's Worker Health card, so an operator can tell scheduled recycling
# (the hard ceiling) from a session actually decaying without correlating
# app.log timestamps by hand.
STALE_REASON_VALIDATION_FAILED = "session validation failed"
STALE_REASON_UNRECORDED_PLAYBACK = "unrecorded playback on a quiet feed"
STALE_REASON_NO_CONNECT_STATE = "no connect state"
STALE_REASON_HARD_CEILING = "quiet feed hard ceiling"
STALE_REASON_AUTH_ERROR = "auth error"

RATE_LIMIT_ERROR_BACKOFF_SECONDS = 60  #< how long THIS listener's poll loop pauses after a rate limit.
                                        #  The process-wide pause that actually matters is applied
                                        #  separately - see the backoff branch in startListener.

RATE_LIMIT_REASON_LISTENER_POLL = "listener poll"  #< label for the shared limiter's snapshot
#< the other Spotify path that can be pushed back on: the official Web API's
#  recently-played read (see _fetch_recently_played_from_web_api). Labelled
#  apart from the poll so /admin's card says WHICH endpoint was refused.
RATE_LIMIT_REASON_WEB_API_BACKFILL = "web API backfill"
#< and the third: /v1/me, read to confirm a refreshed token belongs to the
#  account it was stored under (see _get_current_user_from_web_api). Same
#  host and same budget as the backfill read above, labelled apart for the
#  same reason.
RATE_LIMIT_REASON_WEB_API_USER = "web API user info"

# Ceiling on every Web API request made from this module. requests' own
# default is NO timeout, and all three of these run on a worker thread (the
# poll loop, the backfill check), so one unresponsive Spotify would park that
# thread for the life of the process rather than for one cycle.
SPOTIFY_WEB_API_TIMEOUT_SECONDS = 10

# Ceiling on a Retry-After from the recently-played endpoint. Far above
# anything Spotify actually sends (seconds to a couple of minutes), because its
# job is only to stop a malformed or absurd header from parking every Spotify
# call in the process for hours - the backoff is shared, so one bad header on
# this endpoint would take play tracking offline with it.
WEB_API_MAX_BACKOFF_SECONDS = 15 * 60

# How long the poll loop stands down when it trips over the shared limiter's
# OWN open window (as opposed to Spotify pushing back). Short: the window is
# already doing the waiting, and this only exists so the loop doesn't spin
# against it.
LOCAL_PAUSE_RETRY_WAIT_SECONDS = 5

# Bounds memory for the missed-track dedup set in _checkConnectStateForMissedTracks -
# prev_tracks itself is always much shorter than this (it's a rolling local queue
# history), so this is just a defensive cap, not a tuning knob.
CONNECT_STATE_MISSED_TRACK_CACHE_SIZE = 50

# How far back the missed-track cross-check looks for an existing play before
# deciding one is really missing.
#
# This was 6h on the assumption that "anything in prev_tracks was played within
# the current listening session". Live app.log refutes that: prev_tracks is the
# Spotify CLIENT's rolling queue history and lives as long as that client does,
# not as long as one listening session. Over 2026-09-12..19, of 422 flagged
# warning/track pairs, 178 (42%) had a real recorded play between 6h and 7d old
# - in the database the whole time, just older than the window could see - and
# another 54 were older still. Only the small warnings (<=3 tracks, playback
# having just moved past a track) were mostly honest at 91%; the bulk
# prev_tracks dumps a client hands over on connect were the noise.
#
# 7 days is where that distribution flattens out. The window deliberately keeps
# a ceiling: "played at some point ever" is no evidence that the play now
# sitting in the queue history was captured, and a wide enough window would
# make the cross-check permanently silent instead of merely quiet.
CONNECT_STATE_MISSED_TRACK_LOOKBACK_SECONDS = 7 * 24 * 60 * 60

# How long a missed-track candidate must stay unaccounted for before it is
# worth a warning. prev_tracks shows a track the moment playback moves on,
# while its recording lands a recently-played/backfill poll later - every
# "never recorded" warning sampled in app.log 2026-08-04 was followed by its
# own web_api_backfill recording 3-22 seconds later. The check runs every poll
# tick (~1s), so this is a deferral before judging, not a schedule.
CONNECT_STATE_MISSED_TRACK_GRACE_SECONDS = 60

# One settled-set per user for the lifetime of the PROCESS, not per Listener:
# a listener is rebuilt on every reconnect and every 6h hard-ceiling recycle
# (LISTENER_STALE_HARD_TIMEOUT_SECONDS), and an idle account's prev_tracks
# never changes - so a per-listener set re-warned the same ~10 tracks every 6
# hours for days (app.log 2026-08-01..04). Keyed by the listener's log key
# (user key, else email - see Listener.logUser).
_settledMissingUrisByUser: dict[str, collections.OrderedDict] = {}
_settledMissingUrisByUserLock = threading.Lock()


def _settledMissingUrisForUser(userKey: str) -> collections.OrderedDict:
    """The shared settled-missing-URI store for `userKey`, created on first
    use. Successive Listener generations for the same user mutate the same
    OrderedDict, so a warning issued before a rebuild still suppresses after
    it. Only the get-or-create is locked; the dict itself is only ever touched
    from the (single) live listener thread of that user - a brief overlap
    during a reconnect handoff can at worst nudge the FIFO bound by one entry,
    which a diagnostic cache can afford."""
    with _settledMissingUrisByUserLock:
        return _settledMissingUrisByUser.setdefault(userKey, collections.OrderedDict())

SPOTIFY_TRACK_URI_PREFIX = "spotify:track:"  #< connect-state prev_tracks mixes real tracks with queue
                                              #  markers (spotify:delimiter, spotify:meta:*) - only URIs
                                              #  with this prefix are playable tracks

WEB_API_POLL_INTERVAL_SECONDS = 15 * 60  #< Query Web API recently-played backfill every 15 minutes

WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS = 2  #< max gap between a Web API item's played_at (interpreted as
                                               #  either a start or end time - see the comment at its use site)
                                               #  and an already-recorded play's timestamp for them to count as
                                               #  the same play rather than a genuinely missing one

WEB_API_BACKFILL_END_TIME_DEDUP_TOLERANCE_SECONDS = 10  #< max gap between a Web API item's played_at and a
                                                         #  recorded listener row's created_at (its observed play
                                                         #  end - the listener inserts at the track-change moment)
                                                         #  for them to count as the same play. Wider than the 2s
                                                         #  tolerance above because insert lag sits between the
                                                         #  two stamps (~1s live, a few seconds in poll mode), but
                                                         #  deliberately still a point match - a recorded end
                                                         #  minutes away is evidence of a DIFFERENT listen, and
                                                         #  suppressing on it would lose that play for good

# Sentinel returned by _fetch_recently_played_from_web_api to distinguish a
# scope-related rejection (the stored refresh token was never granted
# user-read-recently-played - re-authorization required) from a transient
# failure (None) that's worth silently retrying on the next poll.
_SCOPE_ERROR = object()

# Sentinel returned by _refresh_spotify_access_token when accounts.spotify.com
# answers the refresh with invalid_grant: the stored refresh token has been
# revoked by the user, or has reached the six-month lifetime Spotify gives
# refresh tokens (documented at developer.spotify.com, "Refreshing tokens").
# Distinct from None (a network blip, a rate limit, a bad client secret),
# because this one is definitive: nothing but re-authorizing brings the
# backfill back, so the account is flagged on the first sighting rather
# than logging the same error every poll until someone reads the log.
REFRESH_TOKEN_REVOKED = object()

# Accounts-token cooldowns are separate from catalog/playback pacing. Share a
# deadline among users of one configured client without pausing unrelated apps.
_accountsTokenBackoffLock = threading.Lock()
_accountsTokenBackoffUntil: dict[str, float] = {}

# Spotify's recently-played endpoint has been observed to answer a handful of
# polls with 403 "Insufficient client scope" even though the stored refresh
# token does carry the scope (confirmed live: the very next poll, using the
# same unchanged token, succeeds) - a Spotify-side flake, not a real
# revocation. Only flip the account's needs_reauth flag (and prompt the user
# to re-authorize) once this many consecutive polls all report the scope
# error, so one blip doesn't send someone through an unnecessary - and
# confusingly instant, since nothing was actually wrong - re-auth flow.
SCOPE_ERROR_CONFIRM_THRESHOLD = 3

# How long an identity check stands before the session is re-verified.
#
# MUST stay longer than WEB_API_POLL_INTERVAL_SECONDS. The check runs against
# www.spotify.com/api/account-settings/v1/profile, which is Spotify's CONSUMER
# WEB front-end and therefore bot-protected: every "Oh nein!" HTML challenge
# page in app.log came from that one endpoint, at 0.2 requests a minute per
# user, while the connect-state API backend absorbed 50x the volume without
# complaint. Volume was never what made this call fail - being pointed at a
# browser endpoint was.
#
# So for any user with Spotify API credentials the backfill's own /v1/me check
# (official API, OAuth, no bot check - see _recordExternalIdentityCheck) now
# refreshes this window before it can expire, and the browser endpoint is
# never reached on a schedule at all. Keeping this above the backfill interval
# is what makes that true; drop it below and the browser call comes back.
#
# For users WITHOUT credentials this is simply the old 5 minutes stretched:
# the contamination bug it guards against had its root cause fixed in
# patched_spotify_login (every login now gets its own TLSClient rather than
# sharing one process-wide cookie jar), __init__ still checks on every listener
# build, and a mid-session recheck is defence in depth - not worth paying for
# on a bot-protected endpoint every five minutes.
USER_VALIDATION_CACHE_SECONDS = 20 * 60

# How long the profile endpoint is left alone after IT was what failed - a
# bot-check page or a rate limit, rather than an answer.
#
# The transient branch below used to raise without recording anything, so the
# freshness cache above stayed empty and the very next poll after the loop's
# 60s backoff called current_user() again. That turned a 5-minute cadence into
# a 60-second one exactly while Spotify was pushing back - the one moment it
# should have been the other way round.
#
# Shorter than the success cache because recovery is worth noticing promptly,
# and because an auth failure arriving mid-cooldown goes unnoticed until it
# expires. That was always true of the 5-minute success cache too, so 2
# minutes is strictly the smaller blind spot, not a new one.
USER_VALIDATION_ERROR_COOLDOWN_SECONDS = 2 * 60

# Cadence of spotapi's connect-state poll (LastPlayedManger.updateLoop reads
# manager.state, which PUTs to the connect-state endpoint). This is by far the
# largest source of Spotify traffic this app generates - one request per
# interval PER USER, against roughly one every five minutes from everything
# else the listener does - so it is the first number to change if Spotify's
# pushback ever becomes sustained rather than sporadic.
LISTENER_POLL_INTERVAL_SECONDS = 6

# Random per-listener addition to that cadence. Without it every user's poll
# runs at exactly the same rate, so once two listeners start near each other
# they stay near each other forever, arriving together on every tick. The
# shared limiter (Database/rate_limit.py) would then de-align them by BLOCKING
# one thread until the other's slot passed; spreading them out here means it
# rarely has to.
LISTENER_POLL_INTERVAL_JITTER_SECONDS = 1.0

# A "playing" track whose duration has fully elapsed since the connect state
# was last updated is a frozen feed, not real playback. Same rule and same
# grace as Database.getNowPlaying's NOW_PLAYING_STALE_GRACE_MS, which already
# refuses to report these, and kept as its own constant so the two can be read
# side by side.
#
# MILLISECONDS, unlike every other timing value in this file - the _MS suffix
# is the whole reason it carries one. The connect state's `duration`,
# `position_as_of_timestamp` and `timestamp` are all Spotify's own epoch
# milliseconds, and _connectStateIsFrozen compares against time.time() * 1000,
# so this has to be in their unit rather than the file's. The two constants are
# pinned equal, and this unit pinned through that comparison, by
# tests/test_listener_reconnect.py.
CONNECT_STATE_FROZEN_GRACE_MS = 60_000


def _connectStateIsFrozen(state: dict) -> bool:
    """Whether `state` claims playback that the clock says finished long ago.

    The connect state only updates on play/pause/seek/track change, so the live
    position is the snapshot position plus the time since the snapshot. Past the
    track's own duration (plus a grace), nothing is really playing - the state
    is simply one nobody has touched."""
    durationMs = _connectStateInt(state.get("duration"))
    timestampMs = _connectStateInt(state.get("timestamp"))
    if not durationMs or not timestampMs:
        return False   #< nothing to judge against; treat it as live, as before
    positionMs = _connectStateInt(state.get("position_as_of_timestamp"))
    elapsedMs = max(0, int(time.time() * 1000) - timestampMs)
    return positionMs + elapsedMs > durationMs + CONNECT_STATE_FROZEN_GRACE_MS


def _itemTrackId(item: dict) -> str | None:
    """The track id of a recently-played entry from EITHER cache.

    The live listener's fallback rows spell it track.track_id (fallbackTrackRecord);
    the Web API's spell it track.id. `or {}` (not get's default) because an entry
    can carry "track": None, where None.get would raise.
    """
    track = item.get("track") or {}
    return track.get("id") or track.get("track_id")


#< aliased to the private name this module has always used, so the ~5 tests that
#  patch "Database.Listeners.spotifyListener._flaskDebugEnabled" keep working -
#  the call sites below resolve it from module globals, which is what patch
#  replaces. Import it plainly and every one of them silently stops taking effect.
_flaskDebugEnabled = flaskDebugEnabled


def _pollIntervalWithJitter() -> float:
    """The connect-state poll cadence for one listener: the shared base plus a
    small random offset, so no two users' polls stay in lockstep. Drawn once
    per Listener, which means a rebuilt listener re-draws - fine, and in fact
    the point: two accounts that happened to land close together get another
    chance to separate."""
    return LISTENER_POLL_INTERVAL_SECONDS + random.uniform(0, LISTENER_POLL_INTERVAL_JITTER_SECONDS)


def _describeException(exc: Exception) -> str:
    """Fully-qualified type + repr, for the classification diagnostic below -
    richer than parseError's bare type name, so a misclassification report
    (gathered with FLASK_DEBUG on) shows whether an error was, say, a spotapi
    RequestError vs a JSONDecodeError vs a KeyError. That's the signal a later,
    type-aware classification pass needs before it can safely narrow the
    string heuristics here."""
    excType = type(exc)
    return f"{excType.__module__}.{excType.__qualname__}: {exc!r}"


def _matchesAuthError(excStr: str, excTypeName: str) -> bool:
    """The auth-error string/type-name heuristic, factored out of
    classifyListenerError so it can be read and tested on its own. Inputs are
    already lower-cased."""
    return bool(
        "loginerror" in excTypeName
        or "loginerror" in excStr
        or "401" in excStr
        or "403" in excStr
        or "unauthorized" in excStr
        or re.search(r"invalid\s+.*token", excStr)
        or re.search(r"session\s+.*expired", excStr)
    )


def _matchesTransientError(excStr: str) -> bool:
    """The transient-error (429 rate limit / malformed-JSON) string heuristic.
    Input is already lower-cased."""
    return "429" in excStr or ("rate" in excStr and "limit" in excStr) or "json" in excStr


def classifyListenerError(exc: Exception) -> tuple[bool, bool]:
    """(isAuth, isTransient) for a listener-path exception - the single source
    of truth behind _is_auth_error/_is_rate_limit_error.

    The two flags are INDEPENDENT: an error can be both (e.g. a rate-limited
    login failure), and every call site applies its own precedence (auth-first
    in startListener's loop, transient-first in the validate/poll paths).
    Collapsing them into one bucket would silently change the outcome for
    errors that match both, so this returns a pair rather than a single class.

    Today this is purely the same string/type-name heuristic the two
    predicates always used, just consolidated behind one seam and made
    observable - a later type-aware pass (isinstance against spotapi's
    exception hierarchy, JSONDecodeError, etc.) can sharpen it here without
    touching the five call sites. The diagnostic is gated on FLASK_DEBUG, the
    same switch the rest of this module's verbose logging uses."""
    excStr = str(exc).lower()
    excTypeName = type(exc).__name__.lower()
    isAuth = _matchesAuthError(excStr, excTypeName)
    isTransient = _matchesTransientError(excStr)
    if _flaskDebugEnabled():
        logger.info("Listener error classified isAuth=%s isTransient=%s: %s",
                    isAuth, isTransient, _describeException(exc))
    return isAuth, isTransient


def _is_auth_error(exc: Exception) -> bool:
    """Whether an exception is an authentication-related error (expired/invalid
    credentials, 401/403) rather than a transient network issue - thin wrapper
    over classifyListenerError (see there)."""
    return classifyListenerError(exc)[0]


def _is_rate_limit_error(exc: Exception) -> bool:
    """Whether an exception is a rate-limit (429) or other transient Spotify
    API error (malformed JSON, etc.) - thin wrapper over classifyListenerError
    (see there)."""
    return classifyListenerError(exc)[1]  # Invalid JSON usually indicates Spotify API issue


NO_VERDICT_SERVER_ERROR_PATTERN = re.compile(r"status code:\s*5\d\d")  #< spotapi's TLSClient.get on a 5xx:
                                                                        #  LoginError("Could not GET <url>. Status Code: 503")


def _isNoVerdictError(exc: Exception) -> bool:
    """True when a current_user() failure says nothing about the cookies
    either way: the request never reached Spotify (spotapi's RequestError,
    raised by TLSClient.build_request for any curl-level failure - reset,
    timeout, DNS), or Spotify itself fell over (a 5xx on the profile endpoint,
    which TLSClient.get surfaces as LoginError("Could not GET <url>. Status
    Code: 503") - the status rides in the message; the .error detail is the
    constant "Request Failed."). Live, 2026-07..09: 39x503 + 1x504 on that
    endpoint, each read by isLoggedIn() as a logout, and 32 listener rebuilds
    from _validateCurrentUser reading the same thing as a refusal.

    Deliberately NOT a third flag on classifyListenerError: that pair drives
    startListener's precedence, and calling a transport failure "transient"
    there would fire the process-wide SPOTIFY_LIMITER.applyBackoff for a
    local outage. This is consulted only where the question is "did Spotify
    refuse these cookies?" - isLoggedIn() and _validateCurrentUser - and the
    answer is "it never got to say".

    The one RequestError that IS a verdict: a closed curl session (see
    _isSessionClosedError) - that transport is dead for good, so it keeps
    reading as a refusal rather than being retried against forever."""
    if _isSessionClosedError(exc):
        return False
    if isinstance(exc, SpotapiRequestError):
        return True
    return NO_VERDICT_SERVER_ERROR_PATTERN.search(str(exc).lower()) is not None


def _isComparableSpotifyUserId(value) -> bool:
    """Whether `value` is a Spotify user id the backfill's identity check can
    hold against another one: a non-empty string that is not an email address.
    The account-settings profile hands some account types their email AS the
    id (see formatProfile), and /v1/me's id is never that."""
    return isinstance(value, str) and bool(value) and "@" not in value


def _refresh_spotify_access_token(client_id: str, client_secret: str, refresh_token: str,
                                   logUser: str | None = None) -> str | object | None:
    """Return a token, REFRESH_TOKEN_REVOKED, or transient/unavailable None.

    `logUser` is the internal user key (e.g. "7kevinegger"), used only to
    identify which account's worker a logged failure belongs to. It is
    deliberately not the email: these lines are the highest-volume in the log
    and the key identifies the account just as well without writing an address
    to disk on every poll."""
    import base64
    import requests
    with _accountsTokenBackoffLock:
        deadline = _accountsTokenBackoffUntil.get(client_id)
        if deadline is not None:
            if time.monotonic() < deadline:
                return None
            del _accountsTokenBackoffUntil[client_id]
    url = "https://accounts.spotify.com/api/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    try:
        resp = requests.post(url, data=payload, headers=headers,
                             timeout=SPOTIFY_WEB_API_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            if isinstance(token, str) and token:
                return token
            logger.warning("Spotify token refresh returned no usable access token for user %s", logUser)
            return None
        elif resp.status_code == 429:
            # Do not stand SPOTIFY_LIMITER down: accounts.spotify.com needs its
            # own cooldown, leaving catalog/playback requests with valid tokens
            # free to proceed. Never hold the lock while making HTTP requests.
            standDown = retryAfterSeconds(resp,
                                          default=SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
                                          maximum=WEB_API_MAX_BACKOFF_SECONDS)
            with _accountsTokenBackoffLock:
                deadline = time.monotonic() + standDown
                _accountsTokenBackoffUntil[client_id] = max(
                    _accountsTokenBackoffUntil.get(client_id, deadline), deadline)
            logger.warning(
                "Spotify rate-limited the access-token refresh for user %s - it asked for %.0fs",
                logUser, standDown)
        elif resp.status_code == 400 and "invalid_grant" in (resp.text or ""):
            logger.error(
                "Spotify no longer accepts the stored refresh token for user %s (revoked, or past "
                "its six-month lifetime) - re-authorization required: %s",
                logUser, truncateForLog(resp.text))
            return REFRESH_TOKEN_REVOKED
        else:
            logger.error("Failed to refresh Spotify access token for user %s: %s %s",
                         logUser, resp.status_code, truncateForLog(resp.text))
    except Exception as e:
        logger.error("Error refreshing Spotify access token for user %s: %s", logUser, str(e))
    return None


def _fetch_recently_played_from_web_api(access_token: str, logUser: str | None = None):
    """Returns the list of recently-played items on success (possibly empty),
    the _SCOPE_ERROR sentinel when Spotify rejected the request because the
    refresh token lacks the user-read-recently-played scope, or None for any
    other failure (network error, rate limit, etc). `logUser` is the internal
    user key, only used to identify which account's worker a logged failure
    belongs to (see _refresh_spotify_access_token)."""
    import requests
    url = "https://api.spotify.com/v1/me/player/recently-played?limit=50"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=SPOTIFY_WEB_API_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            return resp.json().get("items", [])
        if resp.status_code == 403 and "insufficient" in resp.text.lower():
            logger.error(
                "Spotify Web API rejected recently-played request for user %s: refresh token lacks "
                "user-read-recently-played scope - re-authorization required: %s", logUser, truncateForLog(resp.text))
            return _SCOPE_ERROR
        if resp.status_code == 429:
            # This request is made with a bare requests.get and never passes
            # SPOTIFY_LIMITER, so nothing was pacing it: a 429 used to be
            # logged and forgotten, and the next poll walked straight back
            # into the window Spotify had just announced. Standing the SHARED
            # limiter down is the point - the limit is per-IP, so every other
            # Spotify caller in this process is inside the same window - and it
            # also puts the refusal on /admin's rate-limit card, which this
            # path was invisible to.
            standDown = retryAfterSeconds(resp,
                                          default=SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
                                          maximum=WEB_API_MAX_BACKOFF_SECONDS)
            SPOTIFY_LIMITER.applyBackoff(standDown, reason=RATE_LIMIT_REASON_WEB_API_BACKFILL)
            logger.warning(
                "Spotify Web API rate-limited the recently-played read for user %s - "
                "standing down for %.0fs", logUser, standDown)
            return None
        logger.error("Failed to fetch recently played tracks from Web API for user %s: %s %s", logUser, resp.status_code, truncateForLog(resp.text))
    except Exception as e:
        logger.error("Error fetching recently played tracks from Web API for user %s: %s", logUser, str(e))
    return None


def _get_current_user_from_web_api(access_token: str, logUser: str | None = None) -> dict | None:
    """Fetch current user info from Web API to validate the access token belongs to the expected user.
    `logUser` is the internal user key, only used to identify which account's worker a logged
    failure belongs to (see _refresh_spotify_access_token)."""
    import requests
    url = "https://api.spotify.com/v1/me"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=SPOTIFY_WEB_API_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 429:
            # Same host, same budget and same bare-requests problem as
            # _fetch_recently_played_from_web_api, so the same answer: stand the
            # SHARED limiter down, because the limit is per-caller and every
            # other api.spotify.com call in this process is inside the window
            # Spotify just announced. This one is called on every token refresh
            # in the backfill path, so left unpaced it walked back into that
            # window as often as the poll did.
            standDown = retryAfterSeconds(resp,
                                          default=SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
                                          maximum=WEB_API_MAX_BACKOFF_SECONDS)
            SPOTIFY_LIMITER.applyBackoff(standDown, reason=RATE_LIMIT_REASON_WEB_API_USER)
            logger.warning(
                "Spotify Web API rate-limited the user-info read for user %s - "
                "standing down for %.0fs", logUser, standDown)
        else:
            logger.error("Failed to fetch current user from Web API for user %s: %s %s", logUser, resp.status_code, truncateForLog(resp.text))
    except Exception as e:
        logger.error("Error fetching current user from Web API for user %s: %s", logUser, str(e))
    return None


# The real signal.signal, pinned at import (imports run on the main thread,
# before any suppression context can have patched it). Restoration below
# always installs THIS - never a captured value that might itself be the
# stub - so the stub can never outlive the last active context.
_REAL_SIGNAL = signal.signal

# Guards the depth counter and the install/restore of the process-global
# patch. Never held across the yield, so overlapping Listener constructions
# (a reconnect thread and a login thread) still run concurrently.
_SUPPRESS_SIGNAL_LOCK = threading.Lock()
_SUPPRESS_SIGNAL_DEPTH = 0


def _suppressedSignal(signalnum, handler):
    """Installed as signal.signal while any suppression context is active.
    The patch is process-global but the reason for it ("this thread cannot
    register handlers") is thread-local - so a genuine main-thread
    registration arriving mid-suppression is delegated, not swallowed."""
    if threading.current_thread() is threading.main_thread():
        return _REAL_SIGNAL(signalnum, handler)
    logger.debug("Suppressing signal registration for signal %s in context where signals are not allowed", signalnum)
    return signal.getsignal(signalnum)


@contextmanager
def _suppress_signal_in_thread():
    """Temporarily patch signal.signal to skip SIGINT registration when called
    from a non-main thread (e.g. Flask worker threads). The spotapi library
    unconditionally registers a SIGINT handler in its __init__, which raises
    ValueError on non-main threads.

    Depth-counted under a lock rather than capture-and-restore: two of these
    contexts can overlap (Listener.__init__ runs on reconnect threads and on
    Flask login threads), and the old swap let the second thread capture the
    first's stub as "original" - its probe called the stub, didn't raise, so
    it installed nothing - then re-install that stub after the first thread
    had already restored the real function, leaving signal.signal a no-op for
    the life of the process."""
    global _SUPPRESS_SIGNAL_DEPTH
    if threading.current_thread() is threading.main_thread():
        #< signals register fine here; nothing to patch
        yield
        return
    with _SUPPRESS_SIGNAL_LOCK:
        _SUPPRESS_SIGNAL_DEPTH += 1
        if _SUPPRESS_SIGNAL_DEPTH == 1:
            signal.signal = _suppressedSignal
    try:
        yield
    finally:
        with _SUPPRESS_SIGNAL_LOCK:
            _SUPPRESS_SIGNAL_DEPTH -= 1
            if _SUPPRESS_SIGNAL_DEPTH == 0:
                signal.signal = _REAL_SIGNAL


class Listener:  #< one user's live playback watcher: cookie session + Web API backfill
    def __init__(self, cookiesFile, refreshInterval=None, email=None, get_credentials=None,
                 get_backfill_enabled=None, on_scope_status_change=None, user=None,
                 get_recorded_track_ids=None, get_recorded_play_times=None):
        # None (every production caller) means "the shared cadence, jittered
        # for this listener" - see _pollIntervalWithJitter. An explicit value
        # is honoured verbatim so a test can pin it.
        self.refreshInterval = _pollIntervalWithJitter() if refreshInterval is None else refreshInterval
        self.run = False   #< startListener() raises it; stop()/signalStop() clear it
        self._stop_event = threading.Event()
        self.email = email  #< store expected email for validation
        # Internal user key (e.g. "7kevinegger"), used for log identification so
        # the routine per-poll lines don't write an email address to disk
        # thousands of times a day. The email stays authoritative for the
        # identity checks below - only the LOG representation changes. Falls
        # back to the email when no key was passed (older callers, tests), since
        # an unidentifiable log line is worse than a private one.
        self.user = user
        self._authenticated_user_id = None  #< cache spotify user id for validation
        self.contaminationDetected = False  #< True when the cookies authenticate as a DIFFERENT account than self.email
        self.loginFailed = False  #< True when the stored cookies didn't authenticate at all (see below)
        self.get_credentials = get_credentials
        # Optional admin kill switch, re-read each poll (like get_credentials)
        # so a restart is never needed to see a flip - None (the default for
        # any caller that doesn't pass it, e.g. existing tests) means always
        # enabled.
        self.get_backfill_enabled = get_backfill_enabled
        # Optional callback(needs_reauth: bool), invoked after every Web API
        # backfill poll that got a definitive answer (success or scope
        # rejection) - lets the caller persist "this account needs to
        # re-authorize with Spotify" for display, without the listener itself
        # knowing anything about the database. None (tests, callers that
        # don't care) means the status is simply not persisted anywhere.
        self.on_scope_status_change = on_scope_status_change
        # Optional callback(trackIds) -> set of ids this user already has a
        # recent play for; see _dropUrisAlreadyInDatabase. None means the
        # missed-track cross-check judges on its in-memory cache alone.
        self.get_recorded_track_ids = get_recorded_track_ids
        # Optional callback(startTs, endTs) -> the played_at values this user
        # already has in that window; see _recordedPlayTimesFromDatabase. None
        # means the Web API backfill judges on its in-memory caches alone.
        self.get_recorded_play_times = get_recorded_play_times
        self._consecutiveScopeErrors = 0  #< see SCOPE_ERROR_CONFIRM_THRESHOLD
        self._lastWebApiPollTime = None  #< None means "never polled yet" - forces an immediate first poll
        with _suppress_signal_in_thread():
            self.sp = Spotify(cookiesFile=cookiesFile, email=email)

        # A TLS session exists from here on, and Listener.stop() is the only
        # thing that ever closes one. An exception out of __init__ means
        # startListener never assigns self.listener, so stop() is unreachable
        # for this object for good, and spotapi's atexit registration holds its
        # curl session open until the process exits.
        #
        # That used to be nearly unreachable - the init-packet read simply
        # parked forever instead of raising. c8b688b gave it a deadline and
        # a899886 a socket close, which turned the wedge into a raise; retrying
        # is precisely what _checkLoginLoop's restart pass and
        # onStaleWithBackoff do, so it would now be one session per attempt.
        #
        # A failed LOGIN is not a failed construction: it returns a real
        # Listener (see the else branch below), which startListener assigns and
        # whose stop() owns the close as usual.
        #
        # The suppression context is split rather than widened: it is
        # depth-counted, and only the PlayerStatus construction below needs it.
        constructionSucceeded = False
        try:
            with _suppress_signal_in_thread():
                if self.sp.isLoggedIn():
                    self.sp.startRecentlyPlayedListener(refreshInterval=self.refreshInterval,
                                                        logUser=self.logUser)
                else:
                    # self.sp.user_auth stays a plain bool (the client's
                    # not-logged-in sentinel) when the stored cookies fail to
                    # authenticate. startRecentlyPlayedListener() would refuse
                    # with a ValueError in that state; skip it and let
                    # startListener() (Database/workers/listener.py) report this
                    # the same way it already does for contaminationDetected
                    # below.
                    self.loginFailed = True
                    logger.warning("Spotify login failed for user %s - stored cookies may be invalid or expired", self.logUser)

            # Validate that this Spotify client is properly authenticated for the expected user
            try:
                self._captureIdentityBaseline(self.sp.current_user())

                # The Spotify id used to be logged alongside this - but for these
                # accounts Spotify returns the email AS the id, so the line printed
                # the address twice. It stays available on self._authenticated_user_id
                # and is reported by the contamination/validation errors, which are
                # the only places it actually distinguishes anything.
                # DEBUG: a Listener is rebuilt on every stale-feed reconnect, so
                # this fired 1,568 times in 11 days for 3 users. The worker's own
                # start/stop lines cover the lifecycle events worth seeing.
                logger.debug("Listener initialized for user %s", self.logUser)
            except Exception as e:
                # No baseline then - _validateCurrentUser's first successful
                # answer adopts one (see there). Before that adoption existed,
                # a listener built through a bot-check page on the profile
                # endpoint (13x on live, last 2026-07-29) ran its whole life
                # with the ongoing id check comparing against None, i.e. passing
                # every answer whoever it named.
                logger.warning("Could not verify authenticated user during listener init: %s", parseError(e))

            if (self.loginFailed or self.contaminationDetected) and self.user:
                from services.email_worker import queue_email_notification
                from Database.queries.email_queries import EVENT_INVALID_COOKIES
                queue_email_notification(self.user, EVENT_INVALID_COOKIES)

            self.recentlyPlayed_Z1 = self.sp.current_user_recently_played()   #< Z1 = the previous poll's snapshot
            self.webApiRecentlyPlayed_Z1 = []  #< _checkWebApiBackfill's own dedup bookkeeping, kept
                                                #  separate from recentlyPlayed_Z1 (live-listener-owned,
                                                #  different dict shape) so the two polling loops never
                                                #  stomp on each other's cache
            self._lastChangeTime = time.monotonic()
            self._lastPlayingUri = None       #< last track the connect state reported playing, and when it
            self._lastPlayingChangeTime = 0.0  #  last changed - see _observePlaybackForStaleness
            self._lastPlayingSeenAt = 0.0     #< when that track was last sighted at all - the continuity bound
            self._firstUnarrivedChangeTime = 0.0  #< first change since the feed last moved - the grace anchor
            # Dedupes _checkConnectStateForMissedTracks warnings; OrderedDict (not
            # set) so eviction can target the oldest entry. Shared across this
            # user's successive Listener generations via _settledMissingUrisForUser
            # so a rebuild doesn't re-warn what was already answered; anonymous
            # listeners (no user key and no email - only ever tests) keep a
            # private store rather than all sharing one.
            userKey = self.logUser
            self._settledMissingTrackUris = (
                _settledMissingUrisForUser(userKey) if userKey else collections.OrderedDict()
            )
            self._pendingMissingTrackUris: dict[str, float] = {}  #< uri -> monotonic first-seen; see
                                                                  #  _checkConnectStateForMissedTracks
            self._last_user_validation_time = None  #< None means "never validated yet" - forces an immediate first check
            self._last_user_validation_result = True  #< cache validation result
            self._last_validation_error_time = None  #< when the profile endpoint last refused to answer;
                                                      #  see USER_VALIDATION_ERROR_COOLDOWN_SECONDS
            constructionSucceeded = True
        finally:
            if not constructionSucceeded:
                self._retireSessionOfFailedConstruction()

    def _retireSessionOfFailedConstruction(self) -> None:
        """Close the TLS session of a Listener that never came into existence.

        Spotify.close() is idempotent and documented never to raise, but
        Listener.stop() guards its own call to it and so does this: the caller
        classifies on the CONSTRUCTION error (classifyListenerError decides
        retry-vs-give-up from it), and a teardown failure surfacing in its place
        would be diagnosed as something else entirely.

        This releases login.client only. The streamer's dealer socket is a
        separate leak with a separate owner - lastPlayedManager is still None
        when the PlayerStatus constructor is what raised - and is handled by
        _closeSocketOfFailedConstruction in Database/patches.py."""
        closeSession = getattr(self.sp, "close", None)
        if not callable(closeSession):
            return
        try:
            closeSession()
        except Exception as e:  # noqa: BLE001 - see the docstring: the construction's
            logger.warning(     #  own exception is the one worth propagating
                "Failed to close the Spotify session of a listener that could not be built: %s", parseError(e))

    @property
    def logUser(self) -> str | None:
        """How this listener identifies itself in log messages: the internal
        user key when the caller supplied one, else the email as before."""
        return self.user or self.email

    def isLoggedIn(self) -> bool:
        # A contaminated session is technically logged in - as the WRONG
        # account. Reporting False routes the user back through the login
        # flow, whose cookie verification requires the matching account.
        if self.contaminationDetected:
            return False
        if not self.sp.isLoggedIn():
            return False   #< the client's own cookie-level verdict
        try:
            self.sp.current_user()   #< any answer at all proves the session works
            return True   #< the session answered for itself
        except SpotifyLocallyRateLimitedError as e:
            # Our own open window, not a verdict on these cookies - see
            # _validateCurrentUser. Same answer as the transient branch below
            # (stay logged in), without the warning: this is the system
            # working, not an incident.
            logger.debug("Skipped login check for user %s while the shared Spotify "
                         "backoff is open: %s", self.logUser, parseError(e))
            return True
        except Exception as e:
            error_str = str(e).lower()
            # Spotify sometimes answers current_user() with a non-JSON
            # fallback/bot-check page (e.g. an "Oh nein!" HTML error page)
            # instead of the profile - transient, not proof the cookies are
            # actually invalid. Treat it like _validateCurrentUser/_checkOnce
            # already do: trust the cookie check above and stay logged in,
            # rather than bouncing the user to the login flow.
            if "json" in error_str or _is_rate_limit_error(e):
                logger.warning("Transient error checking login status for user %s "
                               "(rate limit or malformed response): %s", self.logUser, parseError(e))
                return True
            if _isNoVerdictError(e):
                # The request never got Spotify's opinion of these cookies: a
                # transport failure, or the profile endpoint itself down (5xx).
                # Same answer as above - the cookie-level verdict stands. Read
                # as a refusal, this parked every page on /login and refused a
                # correct password for LOGIN_CACHE_TTL_SECONDS (user_registry
                # caches the False), 40 times on live in two months.
                logger.warning("Could not check login status for user %s (Spotify unreachable "
                               "or answered 5xx) - keeping the cookie-level verdict: %s",
                               self.logUser, parseError(e))
                return True
            return False   #< a real refusal: the cookies no longer authenticate

    def _recordExternalIdentityCheck(self, now: float) -> None:
        """Count a POSITIVE identity check made elsewhere as this listener's
        session validation, so the cookie-based one doesn't have to run.

        Today that means the Web API backfill's /v1/me lookup: same question
        ("do these credentials still belong to this account?"), asked of the
        official OAuth API instead of the bot-protected consumer web endpoint
        that _validateCurrentUser has to use. It runs every
        WEB_API_POLL_INTERVAL_SECONDS, which is shorter than the window it
        refreshes here, so a credentialed user's listener stops touching
        www.spotify.com on a schedule entirely.

        Only ever called for a check that actually PROVED identity - see the
        caller. A comparison that couldn't run (no email on either side) proves
        nothing and must not suppress the real one."""
        self._last_user_validation_time = now
        self._last_user_validation_result = True
        self._last_validation_error_time = None

    def _captureIdentityBaseline(self, current_user: dict) -> bool:
        """Adopt the session's Spotify id as the identity every later
        _validateCurrentUser answer is compared against, and check the
        session's email against the account the cookies are stored under.
        Returns False when that email proves the session is another account's.

        CRITICAL: Verify cookies actually belong to the expected user, not a
        different account. Detection alone isn't enough - the flag set here
        makes this listener refuse to record anything (see startListener/
        isLoggedIn): without it, plays from the wrong account kept being
        recorded under this user, and the id comparison was no help because
        it baselines on _authenticated_user_id, i.e. the WRONG account's own
        id, so the ongoing check always passed. Only a real, non-empty string
        email is proof of a mismatch - Spotify can return "email": null, which
        must not read as contamination.

        Runs from __init__, and again from _validateCurrentUser's first
        successful answer when the build-time call raised and left no
        baseline - the same comparison either way, so a listener whose
        constructor met a bot-check page is not unguarded for life."""
        self._authenticated_user_id = current_user.get("id")
        authenticated_email = current_user.get("email")
        if isinstance(authenticated_email, str) and authenticated_email and self.email:
            if authenticated_email.lower() != self.email.lower():
                self.contaminationDetected = True
                logger.error(
                    "CRITICAL: Cookie contamination detected! Cookies for %s are actually authenticated as %s. "
                    "Recording is disabled for this listener so plays from %s are NOT recorded under %s's account. "
                    "The stored cookies must be re-authorized.",
                    self.email, authenticated_email, authenticated_email, self.email
                )
                return False
        return True

    def _validationIsOnCooldown(self, now: float) -> bool:
        """Whether the profile endpoint should be left alone this poll.

        Two independent reasons, and they are NOT the same thing: a fresh
        answer (don't re-ask for USER_VALIDATION_CACHE_SECONDS), or a refusal
        to answer at all (don't re-ask for USER_VALIDATION_ERROR_COOLDOWN_SECONDS).
        Only the first existed before, which is why a bot-checked profile call
        was retried every 60s instead of every 5 minutes."""
        if self._last_user_validation_time is not None and \
                (now - self._last_user_validation_time) < USER_VALIDATION_CACHE_SECONDS:
            return True
        return self._last_validation_error_time is not None and \
            (now - self._last_validation_error_time) < USER_VALIDATION_ERROR_COOLDOWN_SECONDS

    def _validateCurrentUser(self) -> bool:
        """Verify that the authenticated Spotify session still belongs to the expected user.
        Returns True if valid, False if session has changed. Logs warnings if mismatches detected.

        Results are cached for USER_VALIDATION_CACHE_SECONDS to reduce bot detection triggers
        from excessive polling; a refusal to answer starts its own, shorter
        cooldown (see _validationIsOnCooldown). Auth errors are not cached at
        all - those return False immediately so the caller can reconnect. A
        transport failure or a Spotify 5xx is neither a verdict nor a rate
        limit (see _isNoVerdictError): the last known answer stands and the
        refusal cooldown starts."""
        now = time.monotonic()
        if self._validationIsOnCooldown(now):
            # The last KNOWN answer, which on a listener that has never had one
            # is the constructor's optimistic default. That is unchanged
            # behaviour: the 5-minute success cache always returned it too for
            # the window after any check, and __init__'s contamination check
            # (or, when that raised, the first successful validation's - see
            # _captureIdentityBaseline) is what actually guards against
            # recording under the wrong account.
            return self._last_user_validation_result

        try:
            current_user = self.sp.current_user()
            current_user_id = current_user.get("id")
            if not self._authenticated_user_id:
                # No baseline: __init__'s own identity check raised and the
                # guard below had nothing to compare against - before this
                # branch existed, for the listener's whole life. This first
                # answer becomes the baseline, through the same email
                # comparison __init__ makes; a mismatch is the contamination
                # that comparison exists to catch, and False here hands the
                # loop to onStale (see _checkOnce), so the rebuilt listener's
                # own __init__ check is what refuses to record.
                result = self._captureIdentityBaseline(current_user)
            elif current_user_id != self._authenticated_user_id:
                logger.error(
                    "Session user mismatch! Expected %s, got %s - this could indicate cross-user contamination",
                    self._authenticated_user_id, current_user_id
                )
                result = False
            else:
                result = True

            self._last_user_validation_time = now
            self._last_user_validation_result = result
            self._last_validation_error_time = None  #< it answered; the endpoint is healthy again
            return result
        except SpotifyLocallyRateLimitedError as e:
            # WE paused, not Spotify - the endpoint was never asked. So there
            # is no refusal to record (that cooldown exists to stop us pestering
            # an endpoint that pushed back; this one didn't), and nothing to
            # back off from, because the open window that refused this call IS
            # the backoff. Answer from cache like the cooldown branch above:
            # the pause is far shorter than the cache it sits inside.
            #
            # Raising instead is what produced the 2026-07-29 17:43 log - one
            # real rate limit on one account, then every other listener
            # tripping over the shared window and reporting it as its own
            # brand-new rate limit, each opening yet another.
            logger.debug("Skipped validating current user %s while the shared Spotify "
                         "backoff is open: %s", self.logUser, parseError(e))
            return self._last_user_validation_result
        except Exception as e:
            error_str = str(e).lower()
            # Invalid JSON errors usually indicate rate limiting or bad response from Spotify
            if "json" in error_str or _is_rate_limit_error(e):
                # Record the REFUSAL, not a result: the caller still backs off
                # on this raise, but the next poll after that backoff must not
                # walk straight back into the same endpoint.
                self._last_validation_error_time = now
                logger.warning("Transient error validating current user %s "
                               "(rate limit or malformed response): %s", self.logUser, parseError(e))
                raise  # Trigger rate limit backoff in startListener
            if _isNoVerdictError(e):
                # Spotify never answered the identity question (transport
                # failure, or the profile endpoint down with a 5xx). Not a
                # refusal of the cookies, so not the False that makes
                # _checkOnce rebuild the listener - a 503 here cost 32 full
                # rebuilds on live; and not a rate limit, so not a raise into
                # startListener's process-wide backoff. The last known answer
                # stands, and the refusal cooldown keeps the next poll from
                # walking straight back into the same endpoint.
                self._last_validation_error_time = now
                logger.warning("Could not validate current user %s (Spotify unreachable or "
                               "answered 5xx) - keeping the last known answer: %s",
                               self.logUser, parseError(e))
                return self._last_user_validation_result
            if not _is_auth_error(e):
                raise
            logger.warning("Could not validate current user: %s", parseError(e))
            return False

    def _observePlaybackForStaleness(self) -> None:
        """Remember what the connect state says is playing, so the stale-feed
        check can tell an idle account from a dead session.

        Costs nothing: getConnectPlayerState reads the dict the update loop's own
        tick already refreshes, with no network call. Only a change BETWEEN two
        real tracks is recorded - the first sighting isn't one (a listener
        rebuilt mid-track would otherwise immediately justify the next rebuild),
        and ads/episodes never reach the recently-played feed at all, so moving
        between them proves nothing about the feed's health.

        "Between" is bounded by LISTENER_PLAYBACK_CONTINUITY_SECONDS: a
        different track sighted across a wider gap is a fresh baseline, not a
        witnessed transition - the resume-after-idle case, where the pre-gap
        track's play was the feed's to record back when it ended.

        Note this runs AFTER _validateCurrentUser and the feed read, so an error
        on either skips it and the loop backs off - 60s for a rate limit, 30s
        otherwise, both longer than the continuity bound. Track changes during a
        backoff therefore land outside the window and read as fresh baselines,
        which under-detects a genuinely broken feed exactly when Spotify is
        misbehaving. That is the deliberate side of the trade: we did not
        observe the gap, so we cannot claim to have witnessed anything across
        it, and widening the window to cover backoffs would re-admit the
        spurious rebuild-on-resume this bound exists to stop. Under-detection
        costs a later rebuild; over-detection costs a re-login at the moment
        someone starts listening."""
        state = self.getConnectPlayerState()
        if not state or not state.get("is_playing") or state.get("is_paused"):
            return
        if _connectStateIsFrozen(state):
            # Not a sighting at all. A frozen state keeps saying is_playing on
            # every ~1s tick, so counting it refreshed _lastPlayingSeenAt
            # forever - and the continuity bound below measures the gap between
            # SIGHTINGS, so hours of idleness became invisible and the eventual
            # resume onto another track read as a witnessed transition. That is
            # a full re-login at the moment someone starts listening, which is
            # the exact failure the continuity bound was added to prevent.
            return
        uri = (state.get("track") or {}).get("uri") or ""
        if not uri.startswith(SPOTIFY_TRACK_URI_PREFIX):
            return
        now = time.monotonic()
        if (self._lastPlayingUri is not None and uri != self._lastPlayingUri
                and now - self._lastPlayingSeenAt <= LISTENER_PLAYBACK_CONTINUITY_SECONDS):
            if self._lastPlayingChangeTime <= self._lastChangeTime:
                # First change since the feed last moved: the catch-up grace in
                # _staleFeedBrokenReason runs from here, not from later churn.
                self._firstUnarrivedChangeTime = now
            self._lastPlayingChangeTime = now
        self._lastPlayingUri = uri
        self._lastPlayingSeenAt = now

    def _pushChannelIsAlive(self) -> bool:
        """Whether a push loop is currently feeding this listener's connect
        state, proven within LISTENER_PUSH_CHANNEL_ALIVE_SECONDS.

        A stamp rather than a flag: a push thread that dies leaves its last
        value behind, and only an ageing one stops vouching for it. Read
        through the same getattr chain as getConnectPlayerState, and type-
        checked for the same reason - whatever the other side left there is
        not something to do arithmetic on unchecked."""
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        aliveAt = getattr(lastPlayedManager, "pushChannelAliveAt", None)
        if not isinstance(aliveAt, (int, float)):
            return False
        return (time.monotonic() - aliveAt) <= LISTENER_PUSH_CHANNEL_ALIVE_SECONDS

    def _pushSubscriptionIsFresh(self) -> bool:
        """Whether the push loop re-proved its connect-state subscription
        within LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS - the evidence that
        lets the hard ceiling defer. Same stamp-not-flag, getattr-chain and
        type-check reasoning as _pushChannelIsAlive."""
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        renewedAt = getattr(lastPlayedManager, "subscriptionRenewedAt", None)
        if not isinstance(renewedAt, (int, float)):
            return False
        return (time.monotonic() - renewedAt) <= LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS

    def _pollRenewalIsFresh(self) -> bool:
        """Whether the poll tick renewed the connect state successfully within
        LISTENER_POLL_RENEWAL_FRESH_SECONDS - the evidence that an ABSENT
        connect state is an account with nothing cast rather than a dead tick.
        Same stamp-not-flag, getattr-chain and type-check reasoning as
        _pushChannelIsAlive; one level deeper, since the stamp lives on the
        PlayerStatus the manager owns."""
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        manager = getattr(lastPlayedManager, "manager", None)
        renewedAt = getattr(manager, "stateRenewalSucceededAt", None)
        if not isinstance(renewedAt, (int, float)):
            return False
        return (time.monotonic() - renewedAt) <= LISTENER_POLL_RENEWAL_FRESH_SECONDS

    def _staleFeedBrokenReason(self) -> str | None:
        """Why a feed that hasn't changed in LISTENER_STALE_TIMEOUT_SECONDS is
        evidence of a dead session rather than of nobody listening - or None
        when it isn't. The reason rides into the /admin session ledger, so the
        two failures stay distinguishable there.

        The feed only gains an entry when a track FINISHES, so silence is the
        normal state of an idle account - and rebuilding on silence alone meant
        a full spotapi re-login every 30 minutes per user, forever. Two things
        count as evidence instead: no connect state at all (the websocket tick
        that produces it isn't running either), or a track change observed
        after the feed's last update, i.e. a play that finished and never
        arrived.

        An absent connect state only counts when nothing vouches for the tick
        that fills it. Push mode has no tick at all: _state is written when
        Spotify pushes a cluster and at no other time, so an account nobody is
        listening to legitimately has none, and reading that as death brought
        the 30-minute rebuild straight back (live app.log 2026-08-01, one
        rebuild per LISTENER_STALE_TIMEOUT_SECONDS on the dot). Poll mode has
        a tick, but an account with no live Connect session answers its PUT
        successfully with no player_state - equally absent, equally idle, and
        the renewal stamp is what proves the tick still runs. The hard ceiling
        still applies above this - pongs alone never defer it, because a
        channel can pong while its subscription is wedged; only a recently
        RE-PROVEN subscription does (see _pushSubscriptionIsFresh and the
        ceiling branch in _checkOnce).

        A witnessed change is only evidence once the feed has had
        LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS to catch up - the finished
        play is normally still resolving its metadata when the change is first
        observed, and rebuilding inside that window threw the play away."""
        if not self.getConnectPlayerState():
            if self._pushChannelIsAlive() or self._pollRenewalIsFresh():
                return None
            return STALE_REASON_NO_CONNECT_STATE
        if (self._lastPlayingChangeTime > self._lastChangeTime
                and time.monotonic() - self._firstUnarrivedChangeTime
                > LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS):
            return STALE_REASON_UNRECORDED_PLAYBACK
        return None

    def _unrecordedChangeGraceIsRunning(self) -> bool:
        """Whether a witnessed track change is still inside its catch-up window.

        Both this and _staleFeedBrokenReason answer None/False right now, but
        for opposite reasons: "no unrecorded playback" versus "a play the feed
        still owes us, too recently to judge". Only the second is a reason to
        hold the hard ceiling off - see the ceiling branch in _checkOnce, which
        would otherwise recycle the session mid-grace and throw away the very
        play the grace exists to protect."""
        return (self._lastPlayingChangeTime > self._lastChangeTime
                and time.monotonic() - self._firstUnarrivedChangeTime
                <= LISTENER_UNRECORDED_CHANGE_GRACE_SECONDS)

    def getNewItems(self, new: list) -> list | None:
        """The suffix of `new` that starts at the first item the previous
        snapshot (Z1) hasn't seen, or None when nothing is new. Items are
        identified by played_at - the feed only ever appends."""
        knownTimes = {item["played_at"] for item in self.recentlyPlayed_Z1}
        for index, item in enumerate(new):
            if item["played_at"] not in knownTimes:
                return new[index:]
        return None

    def playlistName(self, playlistId) -> str | None:
        #< None, not a placeholder: the one caller (updatePlaylists) stores this
        #  as the playlist's name, permanently and with no retry, and a literal
        #  "Unknown Playlist" is indistinguishable from a title someone chose.
        #  A degraded response is an absence of an answer, so say that.
        return (self.sp.playlist(playlistId) or {}).get("name")

    def albumName(self, albumId) -> str | None:
        return (self.sp.album(albumId) or {}).get("name")

    def getConnectPlayerState(self) -> dict | None:
        """The raw connect player_state dict off the same PlayerStatus object
        the RecentlyPlayedManager already keeps refreshed every
        refreshInterval tick (see Database/Spotify/recentlyPlayed.py) - no extra
        network call needed. Feeds both the missed-track cross-check and the
        dashboard's Now Playing.

        Deliberately reads the raw cached `_state` dict rather than calling
        PlayerStatus.state/.saved_state/.last_songs_played: `.state` makes a
        fresh connect_device() HTTP request on every access (spotapi's own
        LastPlayed.py comments that this "often gets rate limited"), and
        `.saved_state`/`.last_songs_played` are functools.cached_property, so
        they'd freeze at whatever state existed on first access and never
        pick up later refreshes.

        Returns None if no connect-state has been captured yet (e.g. the
        websocket listener hasn't ticked once, or was never started)."""
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        manager = getattr(lastPlayedManager, "manager", None) if lastPlayedManager is not None else None
        state = getattr(manager, "_state", None) if manager is not None else None
        return state or None

    def _getRecentTrackUrisFromConnectState(self):
        """Previously-played track URIs from the connect state, or None if no
        state has been captured yet - see getConnectPlayerState(). prev_tracks
        also carries queue markers that are not tracks (spotify:delimiter,
        spotify:meta:*); they can never match a recorded play, so they are
        filtered out here - the missed-track check used to re-warn about them
        after every reconnect."""
        state = self.getConnectPlayerState()
        if not state:
            return None
        return [
            uri
            for uri in (track.get("uri") for track in state.get("prev_tracks", []))
            if uri and uri.startswith(SPOTIFY_TRACK_URI_PREFIX)
        ]

    def _dropUrisAlreadyInDatabase(self, missingUris: list[str]) -> list[str]:
        """`missingUris` minus the ones the database already has a recent play
        for.

        recentlyPlayed_Z1 only covers the CURRENT listener object's lifetime,
        and a listener is rebuilt on every reconnect - 1,568 times over 11 days
        in app.log, against 1,627 of these warnings. Anything recorded before
        the last reconnect is missing from that cache while sitting safely in
        the database, so the cache alone can't tell "we lost this play" from
        "we recorded it two reconnects ago". Asking the database can.

        Kept as a callback rather than a repo handle so the listener still knows
        nothing about the database (same contract as get_credentials /
        on_scope_status_change); no callback means the previous cache-only
        behavior. A failing lookup also degrades to warning: this exists to
        surface possible data loss, and silence would be the wrong failure
        mode."""
        if not missingUris or not self.get_recorded_track_ids:
            return missingUris
        trackIds = [uri.removeprefix(SPOTIFY_TRACK_URI_PREFIX) for uri in missingUris]
        try:
            recorded = self.get_recorded_track_ids(trackIds)
        except Exception as e:
            # Deliberately not settled: a lookup that errored answered nothing,
            # and suppressing on it would turn a transient database problem into
            # permanent silence about a possibly-missed play.
            logger.debug("Missed-track database cross-check failed, warning anyway: %s", parseError(e))
            return missingUris

        stillMissing = []
        for uri in missingUris:
            if uri.removeprefix(SPOTIFY_TRACK_URI_PREFIX) in recorded:
                # Settle it: the poll loop runs this every second, and a track
                # the database vouches for is a permanent miss against the
                # in-memory cache - re-asking would mean ~1,800 queries per
                # listener per idle cycle, per user, all answering the same way.
                self._settleMissingUri(uri)
            else:
                stillMissing.append(uri)
        return stillMissing

    def _settleMissingUri(self, uri: str) -> None:
        """Record that `uri` needs no further attention this listener's lifetime -
        either it was warned about, or the database confirmed it was recorded
        after all. FIFO-bounded; an OrderedDict (not a set) so eviction can
        target the oldest entry rather than an arbitrary one."""
        if len(self._settledMissingTrackUris) >= CONNECT_STATE_MISSED_TRACK_CACHE_SIZE:
            self._settledMissingTrackUris.popitem(last=False)
        self._settledMissingTrackUris[uri] = None

    def _checkConnectStateForMissedTracks(self) -> None:
        """Diagnostic cross-check: warn if Spotify's Connect-state queue
        history (prev_tracks) contains a track we never recorded via
        current_user_recently_played(). This is a side-channel, not a source
        of truth - it comes from spotapi's already-running websocket tick, so
        it costs no extra network calls, but prev_tracks is only the local
        queue's rolling history (no per-item timestamp), not an account-wide
        play log - so it can only flag a possible miss, not backfill one.

        A candidate is only judged once it has stayed unaccounted for a full
        CONNECT_STATE_MISSED_TRACK_GRACE_SECONDS: prev_tracks shows a track
        the moment playback moves on, while its recording lands a
        recently-played/backfill poll later, so judging on first sighting
        warned about plays that were recorded seconds afterwards. The deferral
        also keeps the database out of it until it matters - a pending
        candidate costs no lookup at all.

        Must never raise: a bug here is not allowed to disrupt the primary
        polling loop."""
        try:
            recentUris = self._getRecentTrackUrisFromConnectState()
            if not recentUris:
                return

            #< _itemTrackId, NOT a direct key read: the owned client's tracks
            #  spell it track.id and only fallback records still carry
            #  track.track_id - reading one spelling collapses this set to
            #  {None} and every queue URI reads as "never recorded".
            recordedTrackIds = {
                _itemTrackId(item)
                for item in self.recentlyPlayed_Z1
                if item.get("track")
            }

            candidates = []
            for uri in recentUris:
                trackId = uri.removeprefix(SPOTIFY_TRACK_URI_PREFIX)
                if trackId in recordedTrackIds or uri in self._settledMissingTrackUris:
                    continue
                candidates.append(uri)

            # A pending entry that stopped being a candidate resolved itself -
            # recorded after all, or rolled out of prev_tracks - and is
            # forgotten. This is also what bounds the pending dict: it can
            # only ever hold what prev_tracks currently holds.
            candidateSet = set(candidates)
            for uri in [u for u in self._pendingMissingTrackUris if u not in candidateSet]:
                del self._pendingMissingTrackUris[uri]

            now = time.monotonic()
            ripeUris = [
                uri for uri in candidates
                if now - self._pendingMissingTrackUris.setdefault(uri, now)
                >= CONNECT_STATE_MISSED_TRACK_GRACE_SECONDS
            ]
            if not ripeUris:
                return

            missingUris = self._dropUrisAlreadyInDatabase(ripeUris)
            for uri in ripeUris:
                # Answered either way - warned below, or settled by the
                # database lookup (a failed lookup warns too) - so nothing
                # ripe stays pending.
                self._pendingMissingTrackUris.pop(uri, None)

            if missingUris:
                # Named, because every listener cross-checks its OWN account's
                # queue history: an unattributed line cannot be acted on, and a
                # live scorecard of these warnings had to settle for "some user
                # has this play" instead of "this user does".
                logger.warning(
                    "Connect-state queue history for user %s shows %d track(s) that were never "
                    "recorded via current_user_recently_played() - the websocket cache may have "
                    "missed a play. Missing tracks: %s",
                    self.logUser,
                    len(missingUris),
                    ", ".join(missingUris),
                )
                for uri in missingUris:
                    self._settleMissingUri(uri)
        except Exception as e:
            logger.debug("Connect-state cross-check failed (non-fatal): %s", parseError(e))

    def _checkOnce(self, callback, onStale) -> bool:
        """One iteration of the poll loop. Returns False if the feed was found
        stale and handed off to `onStale` for reconnection - the caller should
        stop this listener, since a new one now owns tracking. Raises an exception
        if an auth error is detected so startListener can handle it immediately."""
        # Validate session identity to detect cross-user contamination
        if not self._validateCurrentUser():
            logger.error("Listener session validation failed - triggering reconnection")
            if onStale is not None:
                try:
                    onStale(reason=STALE_REASON_VALIDATION_FAILED)
                except Exception as e:
                    logger.error("Reconnect attempt failed: %s", parseError(e))
            return False

        try:
            recentlyPlayed = self.sp.current_user_recently_played()
        except Exception as e:
            error_str = str(e).lower()
            # Invalid JSON errors usually indicate rate limiting or bad response from Spotify
            if "json" in error_str or _is_rate_limit_error(e):
                logger.warning("Transient error fetching recently played for user %s "
                               "(rate limit or malformed response): %s", self.logUser, parseError(e))
                raise  # Trigger rate limit backoff in startListener
            raise  # Let startListener handle other errors

        # BEFORE the feed-change branch below, and that ordering is the whole
        # point. current_user_recently_played() is a local in-process buffer
        # (Database/Spotify/client.py), and the poll loop writes the connect
        # state for the INCOMING track before _applyStateToTracking appends the
        # OUTGOING one - so the tick that first sees a finished track's feed
        # entry already reads its replacement.
        #
        # This used to sit under that branch, on the reasoning that a moving
        # feed is "alive by definition". It is, but skipping the sighting left
        # _lastPlayingUri on the track that just finished, so the NEXT quiet
        # tick registered the transition with a timestamp AFTER the feed had
        # moved - which is precisely _staleFeedBrokenReason's "a play finished
        # and never arrived" arm. Every ordinary track change armed it, and the
        # first quiet 30 minutes afterwards rebuilt the session: the per-user
        # rebuild loop that check exists to prevent.
        #
        # Still after _validateCurrentUser and the feed read, which the
        # docstring depends on: an error on either skips the sighting and the
        # loop backs off, so a gap we did not observe is never claimed as
        # witnessed.
        self._observePlaybackForStaleness()

        if recentlyPlayed != self.recentlyPlayed_Z1:
            newItems = self.getNewItems(recentlyPlayed)
            if newItems:
                logger.info("Listener callback: %d new items for user %s", len(newItems), self.logUser)
            callback(newItems)
            self.recentlyPlayed_Z1 = recentlyPlayed
            # Stamped after the sighting above, never before it: the two are
            # compared with a strict > , so a change seen alongside its own
            # feed entry has to land at or before this to read as recorded.
            self._lastChangeTime = time.monotonic()
            return True

        if onStale is None:
            return True

        elapsed = time.monotonic() - self._lastChangeTime

        if elapsed <= LISTENER_STALE_TIMEOUT_SECONDS:
            return True

        brokenReason = self._staleFeedBrokenReason()
        if not brokenReason and (elapsed < LISTENER_STALE_HARD_TIMEOUT_SECONDS
                                 or self._pushSubscriptionIsFresh()
                                 or self._unrecordedChangeGraceIsRunning()):
            # Nobody is listening - the feed has nothing to report, which is
            # not a fault. This is what the 30-minute rebuild was really
            # detecting: 1,270 reconnects in 11 days, spread evenly across all
            # 24 hours while actual plays vary 100x between night and day.
            # Past the hard ceiling the benefit of the doubt normally ends -
            # unless the push loop re-proved its subscription just now, which
            # rules out the wedged-subscription failure the ceiling bounds
            # (see LISTENER_PUSH_SUBSCRIPTION_FRESH_SECONDS).
            logger.debug(
                "Recently-played feed unchanged for over %ss, but the connect state shows no "
                "unrecorded playback - treating the account as idle rather than the session as dead",
                LISTENER_STALE_TIMEOUT_SECONDS,
            )
            return True

        # DEBUG, not WARNING: the reconnect it triggers reports its own
        # outcome, and only an unsuccessful one is worth the default level.
        logger.debug(
            "Recently-played feed unchanged for over %ss (%s) - assuming the underlying "
            "session/websocket died silently - reconnecting",
            LISTENER_STALE_TIMEOUT_SECONDS, brokenReason or STALE_REASON_HARD_CEILING,
        )
        try:
            # Broken evidence is the stronger diagnosis: past the ceiling WITH
            # evidence, "hard ceiling" would misread a genuinely broken session
            # as routine recycling.
            onStale(reason=brokenReason or STALE_REASON_HARD_CEILING)
        except Exception as e:
            logger.error("Reconnect attempt failed: %s", parseError(e))
        return False

    def startListener(self, callback, onStale=None, onWebApiSnapshot=None):
        if self.contaminationDetected:
            # Never record from a session that belongs to a different account -
            # see the contamination check in __init__.
            logger.error(
                "Listener for %s not started: the stored cookies authenticate as a "
                "different Spotify account. Re-login with matching cookies to resume tracking.",
                self.logUser,
            )
            self.run = False
            return
        self.run = True   #< from here only _checkOnce/auth errors or a stop signal end the loop
        while self.run and not self._stop_event.is_set():
            try:
                if not self._checkOnce(callback, onStale):
                    self.run = False
                    return
                self._checkConnectStateForMissedTracks()
                self._checkWebApiBackfill(callback, onWebApiSnapshot=onWebApiSnapshot)
                self._stop_event.wait(1)
            except Exception as e:
                if _is_auth_error(e):
                    # Auth errors skip the reconnect ladder and trigger
                    # reconnection immediately, not after the 30-minute stale
                    # timeout - the credentials are bad now, so waiting out
                    # the usual ceiling would just keep failing quietly.
                    logger.warning("Auth error detected, triggering immediate reconnection: %s", parseError(e))
                    if onStale is not None:
                        try:
                            onStale(reason=STALE_REASON_AUTH_ERROR)
                        except Exception as reconnect_err:
                            logger.error("Reconnect attempt failed: %s", parseError(reconnect_err))
                    self.run = False
                    return
                elif isinstance(e, SpotifyLocallyRateLimitedError):
                    # Tested BEFORE the rate-limit branch, which its message
                    # would otherwise match. This is our own open window
                    # reaching the loop from some path that doesn't answer it
                    # inline; re-arming the window here is what turned one real
                    # rate limit into one per listener. Stand down briefly and
                    # let the window expire on its own schedule.
                    logger.debug("Listener poll for user %s skipped while the shared "
                                 "Spotify backoff is open: %s", self.logUser, parseError(e))
                    self._stop_event.wait(LOCAL_PAUSE_RETRY_WAIT_SECONDS)
                elif _is_rate_limit_error(e):
                    # Pause every Spotify request in the process, not just this
                    # thread. The local wait below is nearly worthless on its
                    # own: this loop's only rate-limited call is the
                    # 5-minutely _validateCurrentUser (the recently-played read
                    # is a local cache hit), while the connect-state poll that
                    # actually generates ~10 requests a minute PER USER runs on
                    # spotapi's own thread and never saw this backoff at all.
                    # It stays as the local half - there is no point retrying
                    # validation inside a window we just opened - but the
                    # shared one is what the limit is actually about.
                    SPOTIFY_LIMITER.applyBackoff(SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
                                                 reason=RATE_LIMIT_REASON_LISTENER_POLL)
                    # One line for a routine backoff: the path that raised has
                    # normally already logged the exception (see
                    # _validateCurrentUser's transient branch), so repeating it
                    # here just doubled the noise - and Spotify's rate-limit
                    # reply is a whole HTML fallback page. FLASK_DEBUG brings
                    # the detail back for the paths that raise without logging.
                    if _flaskDebugEnabled():
                        logger.warning("Rate limit error detected for user %s, backing off for %d seconds: %s",
                                       self.logUser, RATE_LIMIT_ERROR_BACKOFF_SECONDS, parseError(e))
                    else:
                        logger.warning("Rate limit error detected for user %s, backing off for %d seconds",
                                       self.logUser, RATE_LIMIT_ERROR_BACKOFF_SECONDS)
                    self._stop_event.wait(RATE_LIMIT_ERROR_BACKOFF_SECONDS)
                else:
                    logger.error("Error in listener: %s", parseError(e))
                    self._stop_event.wait(30)

    def _recordedPlayTimesFromDatabase(self, items: list) -> dict:
        """{track_id: {(played_at, listener_created_at), ...}} for the window
        `items` spans - the half of the backfill's dedup set that survives a
        listener rebuild. The second tuple element is the recorded row's
        created_at when the row came from the live listener (its observed play
        end - see the dedup comment in _checkWebApiBackfill), None otherwise.

        Keyed by track because this window is wide (the whole API page plus a
        track length: hours, typically hundreds of rows, and skip bursts sitting
        seconds apart). A flat set of timestamps let any recorded row answer for
        any candidate, and the end-time interpretation below made that
        systematic rather than accidental - see the dedup comment in
        _checkWebApiBackfill.

        Kept as a callback rather than a repo handle so the listener still knows
        nothing about the database (same contract as get_credentials /
        get_recorded_track_ids). No callback, no usable timestamps, or a failing
        lookup return no confirmations: announcing an
        already-recorded play is harmless (appendTrackData's guard drops it),
        whereas suppressing a genuinely missing one would lose it for good."""
        if not self.get_recorded_play_times or not items:
            return {}

        timestamps = [ts for ts in (timeToInt(item.get("played_at")) for item in items) if ts > 0]
        if not timestamps:
            return {}

        # played_at may be the END of a play (see the dedup comment below), in
        # which case the recorded row sits up to one track-length earlier - so
        # the window has to reach back that far to find it. A PAUSED play's
        # start sits even earlier (duration + pause), which no fixed reach-back
        # can cover - the query closes that gap itself by also matching
        # listener rows into the window by their created_at.
        longestTrackSeconds = max(
            ((item.get("track") or {}).get("duration_ms", 0) or 0) // 1000 for item in items
        )
        startTs = min(timestamps) - longestTrackSeconds - WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
        endTs = max(timestamps) + WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
        try:
            recorded: dict = {}
            for trackId, playedAt, listenerCreatedAt in self.get_recorded_play_times(startTs, endTs):
                recorded.setdefault(trackId, set()).add(
                    (float(playedAt), float(listenerCreatedAt) if listenerCreatedAt is not None else None))
            return recorded
        except Exception as e:
            logger.debug("Backfill dedup database lookup failed, conservatively reoffering plays: %s",
                         parseError(e))
            return {}

    def _checkWebApiBackfill(self, callback, onWebApiSnapshot=None) -> None:
        if not self.get_credentials:
            return
        if self.get_backfill_enabled is not None and not self.get_backfill_enabled():
            return

        now = time.monotonic()
        # Query every 15 minutes (never polled yet on first call)
        lastPollTime = getattr(self, "_lastWebApiPollTime", None)
        if lastPollTime is not None and now - lastPollTime < WEB_API_POLL_INTERVAL_SECONDS:
            return

        self._lastWebApiPollTime = now

        try:
            creds = self.get_credentials()
            if not creds or not creds.get("client_id") or not creds.get("client_secret") or not creds.get("refresh_token"):
                return

            if _flaskDebugEnabled():
                logger.info("Running Spotify Web API recently-played backfill check... (user %s)", self.logUser)
            access_token = _refresh_spotify_access_token(
                creds["client_id"], creds["client_secret"], creds["refresh_token"], logUser=self.logUser)
            if access_token is REFRESH_TOKEN_REVOKED:
                # Straight to the flag, no SCOPE_ERROR_CONFIRM_THRESHOLD: that
                # streak exists for a 403 Spotify has been seen to answer by
                # mistake, and a refused grant is not a flake. Cleared the same
                # way the scope flag is - by the next definitive fetch.
                if self.on_scope_status_change:
                    try:
                        self.on_scope_status_change(True)
                    except Exception as e:
                        logger.error("Failed to record Spotify reauth-needed status for user %s: %s", self.logUser, parseError(e))
                return
            if not access_token:
                logger.warning("Could not obtain access token for Web API backfill for user %s.", self.logUser)
                return

            # Validate that the access token belongs to the authenticated user,
            # not a different Spotify account (prevents cross-user contamination)
            web_api_user = _get_current_user_from_web_api(access_token, logUser=self.logUser)
            if not web_api_user:
                logger.warning("Could not validate Web API user, skipping backfill for user %s.", self.logUser)
                return

            web_api_user_id = web_api_user.get("id")
            web_api_user_display = web_api_user.get("display_name", web_api_user_id)
            web_api_user_email = web_api_user.get("email", "")
            if _flaskDebugEnabled():
                logger.info("Web API user: %s (ID: %s, email: %s), Listener email: %s",
                           web_api_user_display, web_api_user_id, web_api_user_email, self.email)

            # Validate that the access token belongs to the authenticated user.
            # The email is the strongest reading, but /v1/me only carries one
            # under the user-read-email scope - grants made before the
            # authorize URL asked for it (SPOTIFY_OAUTH_SCOPE) never have it,
            # and for those the comparison used to be skipped outright, so the
            # guard this comment describes never ran. Without an email, the
            # ids are compared instead: the cookie session's
            # _authenticated_user_id is the account-settings `username`, which
            # is the same Spotify user id /v1/me reports - two independent
            # readings of one fact, unlike the cookie-side id check, which
            # baselines on itself (see _captureIdentityBaseline). Lenient only
            # when there is genuinely nothing to compare: no baseline (a
            # listener built through a bot-check page), no id in the answer,
            # or a baseline that is the account's email standing in for its
            # id, which /me's id never is.
            mismatch = False
            identityConfirmed = False  #< the comparison actually RAN and passed
            cookieUserId = self._authenticated_user_id
            if self.email and web_api_user_email and self.email.lower() != web_api_user_email.lower():
                mismatch = True
                mismatch_reason = f"email mismatch: API has {web_api_user_email}, listener is {self.email}"
            elif self.email and web_api_user_email:
                identityConfirmed = True
            elif (_isComparableSpotifyUserId(cookieUserId) and _isComparableSpotifyUserId(web_api_user_id)
                  and cookieUserId.lower() != web_api_user_id.lower()):
                mismatch = True
                mismatch_reason = (f"user id mismatch: API has {web_api_user_id}, "
                                   f"cookie session is {cookieUserId}")
            elif _isComparableSpotifyUserId(cookieUserId) and _isComparableSpotifyUserId(web_api_user_id):
                identityConfirmed = True
            else:
                logger.debug("Web API response carries no email and the ids cannot be compared "
                             "(API: %r, cookie session: %r) - cannot prove a mismatch, being lenient",
                             web_api_user_id, cookieUserId)

            if mismatch:
                # Deliberately NOT fed into the validation cache as a False.
                # This proves the OAuth CREDENTIALS point at another account,
                # which rebuilding the cookie session cannot fix - the two
                # identities have separate failure modes and separate remedies.
                logger.error(
                    "CONTAMINATION CHECK FAILED: Web API user mismatch (%s). Skipping backfill to prevent cross-user data import.",
                    mismatch_reason
                )
                return

            if identityConfirmed:
                # The official API just answered the question _validateCurrentUser
                # would otherwise ask the bot-protected consumer endpoint.
                self._recordExternalIdentityCheck(now)

            items = _fetch_recently_played_from_web_api(access_token, logUser=self.logUser)

            if items is _SCOPE_ERROR:
                self._consecutiveScopeErrors += 1
                if self._consecutiveScopeErrors < SCOPE_ERROR_CONFIRM_THRESHOLD:
                    logger.warning(
                        "Spotify Web API reported insufficient scope for user %s (%d/%d consecutive) - "
                        "treating as a transient Spotify-side flake for now.",
                        self.logUser, self._consecutiveScopeErrors, SCOPE_ERROR_CONFIRM_THRESHOLD,
                    )
                    return
                if self.on_scope_status_change:
                    try:
                        self.on_scope_status_change(True)
                    except Exception as e:
                        logger.error("Failed to record Spotify reauth-needed status for user %s: %s", self.logUser, parseError(e))
                return

            # A definitive (non-None, non-scope-error) response - even an empty
            # list - proves the current token DOES carry the required scope,
            # so any previously-recorded reauth-needed status is stale.
            if items is not None:
                self._consecutiveScopeErrors = 0
                if self.on_scope_status_change:
                    try:
                        self.on_scope_status_change(False)
                    except Exception as e:
                        logger.error("Failed to clear Spotify reauth-needed status for user %s: %s", self.logUser, parseError(e))

            if _flaskDebugEnabled():
                logger.info("Web API returned %d items for backfill check (user %s)", len(items) if items else 0, self.logUser)
            if not items:
                return

            # Production supplies the database confirmation callback. Both
            # caches contain observations/offers, including failed inserts, so
            # neither can acknowledge persistence. A failed lookup reoffers
            # conservatively; the database's insert guard still deduplicates.
            # Keep cache-only behavior for embedders without a provider.
            recorded_timestamps: dict = {}
            if not self.get_recorded_play_times:
                for item in self.recentlyPlayed_Z1 + self.webApiRecentlyPlayed_Z1:
                    trackId = _itemTrackId(item)
                    if not item.get("played_at") or not trackId:
                        continue
                    recorded_timestamps.setdefault(trackId, set()).add((timeToInt(item.get("played_at")), None))
            # Both caches above live and die with this listener object, and a
            # listener is rebuilt on every stale-feed reconnect (1,568 times in
            # 11 days for 3 users) - webApiRecentlyPlayed_Z1 starts empty, and
            # so does recentlyPlayed_Z1, since the client's deque only fills
            # from track changes its websocket observes after start. So the
            # first poll after every rebuild saw the whole page as missing:
            # 74,579 plays announced over those 11 days, of which 201 were
            # genuinely new - the rest were dropped one at a time by
            # appendTrackData's own duplicate guard, long after the log had
            # claimed them. The database is what tells a real gap from a cold
            # cache.
            for trackId, playedAts in self._recordedPlayTimesFromDatabase(items).items():
                recorded_timestamps.setdefault(trackId, set()).update(playedAts)

            # Built directly from `items` in one pass so each missed item stays
            # tied to its OWN source API item's played_at - no post-hoc
            # re-matching by track ID, which breaks when the same track
            # appears more than once in `items` (all copies would resolve to
            # whichever occurrence next() finds first).
            missed_items = []

            for item in items:
                played_at_str = item.get("played_at")
                track = item.get("track")
                track_id = track.get("id") if track else None
                if not played_at_str or not track_id:
                    continue

                timestamp = timeToInt(played_at_str)
                # `or 0` (not get's default): an item's track can carry
                # "duration_ms": None (present but null), where dict.get
                # returns None rather than falling back to 0, and the
                # division below would crash and abort the whole poll.
                duration_ms = track.get("duration_ms", 0) or 0
                duration_s = duration_ms // 1000

                # Spotify's Web API documents played_at only as "the date and
                # time the track was played" - it does NOT specify start vs
                # end, and Spotify's own developer community has confirmed the
                # same endpoint can report either for different entries (see
                # spotify/web-api#1083). So this can't assume one direction:
                # check both interpretations - timestamp itself already being
                # a start time, or timestamp being an end time duration_s
                # seconds after the true start - before deciding this play is
                # genuinely missing.
                #
                # Only THIS track's recorded times can answer for it. A flat set
                # of timestamps meant any recorded play within the tolerance
                # did, and the second arm made that systematic: under gapless
                # playback a missing track's derived start equals the recorded
                # END of the track before it - i.e. the one recorded neighbour a
                # real gap always has beside it. The suppressed play was then
                # lost for good, since the next poll's page collides identically
                # and nothing else retries it.
                #
                # The third arm covers what the second cannot: a mid-track
                # PAUSE stretches start-to-end beyond duration_s by an
                # unbounded amount (2026-08-04: a ~3min pause put played_at
                # 474s after a 287s track's recorded start, and the same
                # listen was recorded twice). A listener row's created_at is
                # its OBSERVED end - the row is inserted at the track-change
                # moment, pauses included - so the end-time interpretation is
                # matched against that stamp directly instead of deriving a
                # start that assumes uninterrupted playback.
                recordedTimes = recorded_timestamps.get(track_id, ())
                matched_by_played_at = any(
                    abs(timestamp - recorded_t) <= WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
                    or abs(timestamp - duration_s - recorded_t) <= WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
                    for recorded_t, _recorded_end in recordedTimes
                )
                matched_by_end = not matched_by_played_at and any(
                    recorded_end is not None
                    and abs(timestamp - recorded_end) <= WEB_API_BACKFILL_END_TIME_DEDUP_TOLERANCE_SECONDS
                    for _recorded_t, recorded_end in recordedTimes
                )
                if matched_by_end and _flaskDebugEnabled():
                    # Live validation for the 2026-08-04 pause-duplicate fix:
                    # only the cases the two played_at arms would have MISSED
                    # are interesting - remove once a few days of logs confirm
                    # the arm fires on real pauses and nothing else.
                    logger.info(
                        "Backfill item for track %s (played_at=%s) suppressed by the end-time arm alone "
                        "(pause-stretched play already recorded) for user %s",
                        track_id, played_at_str, self.logUser,
                    )
                is_recorded = matched_by_played_at or matched_by_end
                if not is_recorded:
                    context = item.get("context") or {}

                    # Store played_at as given, untouched - see comment above
                    # on why we no longer subtract duration_s here.
                    missed_items.append({
                        "track": track,
                        "played_at": played_at_str,
                        "ms_played": duration_ms,
                        "context": context
                    })

            if missed_items:
                # Routine progress, like the two lines above: the plays this
                # announces are written to the database with their
                # web_api_backfill source, so the record survives without it.
                if _flaskDebugEnabled():
                    logger.info("Backfilling %d plays from Web API recently-played history for user %s",
                                len(missed_items), self.logUser)
                # Mark these as backfilled so the database can record the source
                for missed_item in missed_items:
                    missed_item["_source"] = WEB_API_BACKFILL_SOURCE
                # Pass them to callback (it expects a list, newest plays last)
                # Web API returns newest plays first, so reverse to maintain cron order
                missed_items.reverse()
                callback(missed_items)

            # Replace webApiRecentlyPlayed_Z1 (NOT recentlyPlayed_Z1 - that
            # cache belongs to the live listener) with this batch so it holds
            # exactly the last batch checked for cache-only callers. Production
            # checks database confirmations again next time, so failed offers
            # in this snapshot cannot suppress a retry. Invalid entries are skipped.
            self.webApiRecentlyPlayed_Z1 = [
                {
                    "track": item.get("track"),
                    "played_at": item.get("played_at"),
                    # `or {}` (not get's default): an item can carry "track": None,
                    # where dict.get returns None and None.get(...) would crash and
                    # abort the whole rebuild for this poll. The trailing `or 0`
                    # covers the sibling case - "duration_ms": None (present but
                    # null) - which get's default does not: dict.get returns
                    # None rather than falling back to 0.
                    "ms_played": (item.get("track") or {}).get("duration_ms", 0) or 0,
                    "context": item.get("context") or {}
                }
                for item in items
                if item.get("played_at") and (item.get("track") or {}).get("id")
            ]

            if onWebApiSnapshot is not None:
                onWebApiSnapshot(items)

        except Exception as e:
            logger.error("Error during Web API backfill for user %s: %s", self.logUser, parseError(e))

    def startListener_thread(self, callback, onStale=None, onWebApiSnapshot=None):
        self._stop_event.clear()
        self.thread = threading.Thread(
            target=self.startListener,
            args=(callback,),
            kwargs={"onStale": onStale, "onWebApiSnapshot": onWebApiSnapshot},
            name=f"{LISTENER_THREAD_NAME_PREFIX}{self.logUser}",
            daemon=True,
        )
        self.thread.start()

    def signalStop(self):
        """Signal-only half of stop(): flip every stop flag (the poll loop,
        spotapi's LastPlayed thread, the patched keep_alive/updateLoop) without
        joining threads or closing sockets. Shutdown's phase 1 calls this for
        every user before any join blocks, so no user's listener is still
        running unsignaled while another user's threads are being joined."""
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        self.run = False   #< the poll loop checks this every pass

        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        if lastPlayedManager is not None:
            manager = getattr(lastPlayedManager, "manager", None)
            if manager is not None:
                # Tell the patched keep_alive/updateLoop (Database.patches) the
                # coming close is intentional so they exit instead of reconnecting.
                try:
                    manager._deliberate_close = True
                except AttributeError:
                    pass  # __slots__-only instance; the patches treat a missing flag as False
            lastPlayedManager.run = False

    def stop(self):
        self.signalStop()

        # Also stop spotapi's own background LastPlayed thread (started via
        # startRecentlyPlayedListener). Left running, it can hit a rate-limited or
        # malformed response mid-request while the interpreter is shutting down,
        # producing spurious errors. Close the websocket connection so the
        # keep_alive thread doesn't try to send pings on a closed connection.
        lastPlayedManager = getattr(self.sp, "lastPlayedManager", None)
        if lastPlayedManager is not None:
            manager = getattr(lastPlayedManager, "manager", None)
            if manager is not None:
                ws = getattr(manager, "ws", None)
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: S110 - closing an already-closed socket during
                        pass           #  shutdown; there is nothing left to act on

            # The sleep gives the keep_alive thread (patches.py's
            # patched_keep_alive, started as manager.keep_alive_thread) a
            # moment to notice the now-closed socket and exit on its own -
            # it already saw signalStop() set manager._deliberate_close
            # above, which is what makes it exit instead of reconnecting.
            # The join right below is NOT of that thread: it's
            # lastPlayedManager.thread, spotapi's separate LastPlayed POLL
            # thread. keep_alive_thread is deliberately never joined here -
            # a third join would blow the per-user shutdown budget
            # (LISTENER_JOINS_PER_USER, see tests/test_compose_shutdown_budget.py)
            # - 2026-09-04 review, C12.
            time.sleep(0.1)
            thread = getattr(lastPlayedManager, "thread", None)
            if thread is not None and thread.is_alive():
                thread.join(timeout=LISTENER_STOP_JOIN_TIMEOUT_SECONDS)

        # Bounded join on the listener thread itself to wait for clean exit.
        # Skip the join if called from within the listener thread itself to avoid
        # "cannot join current thread" error (happens when onStale() callback
        # invokes reconnection from within the listener thread).
        if hasattr(self, "thread") and self.thread.is_alive() and threading.current_thread() != self.thread:
            self.thread.join(timeout=LISTENER_STOP_JOIN_TIMEOUT_SECONDS)

        # Release the spotapi TLS session - fresh per login (see
        # Database/Spotify/client.py), so every rebuild retired one, and until
        # this existed each stayed atexit-pinned with its curl session open
        # until process exit. After the joins above on purpose: the loops get
        # their chance to exit cleanly first, and a thread that outlived its
        # join timeout now fails with "Session is closed", which the reconnect
        # paths already treat as a terminal give-up.
        closeSession = getattr(self.sp, "close", None)
        if callable(closeSession):
            try:
                closeSession()
            except Exception as e:
                logger.warning("Failed to close the Spotify session on stop: %s", e)
