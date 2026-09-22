# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import atexit
import copy
import json
import logging
import os
import re
import signal
import threading
import time
import weakref
import websockets.sync.client
import websockets.exceptions
import spotapi.exceptions
import spotapi.status
import spotapi.websocket
from spotapi.types.alias import _Undefined

from Database.rate_limit import (
    SPOTIFY_LIMITER, SPOTIFY_ACQUIRE_TIMEOUT_SECONDS,
    SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
    SpotifyLocallyRateLimitedError,
)
# The dead-transport detector moved to the owned client package with the loop
# machinery (Phase 1.5); the websocket receive path here still needs it.
from Database.Spotify.recentlyPlayed import _isSessionClosedError
from Database.utils import TRUTHY_ENV_VALUES, flaskDebugEnabled
# Rotation recovery reads the current secret from Spotify's own web player -
# see the pinned-secret block below, and Database/Spotify/totpSecret.py.
from Database.Spotify.totpSecret import fetchWebPlayerSecrets

logger = logging.getLogger(__name__)

# Endpoint labels for the shared limiter's backoff reason - short enough for
# the /admin card, specific enough to say WHICH Spotify surface pushed back.
# (The connect-state and track-metadata labels live with their callers in
# Database/Spotify/ since the Phase 1.5 cutover.)
ENDPOINT_ACCOUNT_PROFILE = "account-settings/profile"
ENDPOINT_ACCOUNT_PLAN = "account/plan"

# HTTP statuses that are Spotify explicitly throttling rather than failing.
# 503 is included because Spotify's edge answers sustained pressure with it
# before it ever reaches a 429.
SPOTIFY_THROTTLE_STATUS_CODES = frozenset({429, 503})


def _looksThrottled(statusCode, response) -> bool:
    """Whether a reply is Spotify pushing back, as opposed to an ordinary
    failure. Two signals, both observed live:

    - an explicit throttle status, or
    - ANY HTML body. These endpoints only ever answer JSON, so a web page
      means the request was diverted to Spotify's bot-check/error fallback -
      which arrives as HTTP 200 with 46 KB of markup titled "Oh nein!", i.e.
      invisible to a status-code check alone.
    """
    if statusCode in SPOTIFY_THROTTLE_STATUS_CODES:
        return True
    if response is None:
        return False
    try:
        return _looksLikeHtml(str(response))
    except Exception:
        # Never let the throttle heuristic raise: every caller is already on
        # an error path, where this failing would replace a logged warning
        # (and a raised UserError) with an unrelated exception.
        return False


def _backoffIfThrottled(statusCode, response, endpoint: str) -> bool:
    """Pause EVERY Spotify request in this process if `response` shows Spotify
    pushing back. Returns whether a backoff was applied.

    Process-wide is the whole point: the traffic that trips Spotify's limits
    is the sum of every user's listener, and until this existed the only
    reaction to a rate limit was one listener's poll thread sleeping - which
    paused about 0.3 requests a minute while the other users' connect-state
    polls carried on at ~10 a minute each."""
    if not _looksThrottled(statusCode, response):
        return False
    SPOTIFY_LIMITER.applyBackoff(SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS, reason=endpoint)
    logger.warning(
        "Spotify pushed back on %s (status=%s) - holding every Spotify request "
        "in this process for %ds", endpoint, statusCode, SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS)
    return True

# 1. Monkey patch websockets.sync.client.connect to disable the built-in keepalive ping
# that causes ConnectionClosedError during CPU blockages / imports.
original_connect = websockets.sync.client.connect

def patched_connect(*args, **kwargs):
    # Disable built-in keepalive ping by default
    kwargs.setdefault("ping_interval", None)
    kwargs.setdefault("ping_timeout", None)
    return original_connect(*args, **kwargs)

websockets.sync.client.connect = patched_connect

# Also patch it in spotapi.websocket in case it was already imported
if hasattr(spotapi.websocket, "connect"):
    spotapi.websocket.connect = patched_connect


# 2. Add a robust reconnect method to spotapi.status.PlayerStatus.
# This prevents AttributeError: 'PlayerStatus' object has no attribute 'reconnect'
# when the websocket drops and LastPlayedManger attempts to reconnect.

# The dealer handshake authenticates ONLY through the access_token embedded in
# its URI: none of the HTTP-side machinery applies (_auth_rule's per-request
# refresh, _handle_auth_failure's retry-on-401), so an expired token is a
# guaranteed 401 with no second chance. And spotapi's _get_auth_vars refuses
# to fetch while ANY token is still set, however expired - which made the
# renewal below a no-op for the one credential the handshake needs, and every
# reconnect resent the token Spotify had just rejected (2026-08-01: 14 hours
# of storming "server rejected WebSocket connection: HTTP 401"). A stale token
# must therefore be cleared here, using the same expiry skew _auth_rule uses.
WS_ACCESS_TOKEN_REFRESH_SKEW_MS = 30_000  #< mirrors BaseClient._REFRESH_SKEW_MS


def _accessTokenNeedsRefresh(base) -> bool:
    """Whether base.access_token cannot be trusted for a NEW dealer handshake."""
    token = getattr(base, "access_token", None)
    if token is None or token is _Undefined:
        return True
    expiresAtMs = getattr(base, "access_token_expires_at_ms", 0) or 0
    if not expiresAtMs:
        return True   #< unknown expiry: assume stale - a wrong guess costs one
                      #  token fetch, guessing the other way costs a 401 loop
    return time.time() * 1000 + WS_ACCESS_TOKEN_REFRESH_SKEW_MS >= expiresAtMs


# How long the dealer may take to send its init packet (the frame carrying
# Spotify-Connection-Id). It arrives immediately after the handshake in
# practice; without a bound, a dealer that accepts the socket but never speaks
# would park the reconnecting thread in recv() forever.
WS_INIT_PACKET_TIMEOUT_SECONDS = 10

# keep_alive and the push loop's receive path watch the same socket, so one
# drop can send both into reconnect at once. The lock serializes them; the
# generation counter lets the one that waited notice the winner already
# reconnected and stand down instead of stacking a second dealer connection
# (and a second keep-alive thread) on top of a healthy one.
_reconnectLockGuard = threading.Lock()     #< guards lazy creation of per-streamer locks
_reconnectFallbackLock = threading.Lock()  #< for __slots__-only instances that can't store one


def _reconnectLockFor(self):
    """The lock serializing reconnects of one streamer, created on first use."""
    with _reconnectLockGuard:
        lock = getattr(self, "_reconnectSerializeLock", None)
        if lock is None:
            lock = threading.Lock()
            try:
                self._reconnectSerializeLock = lock
            except AttributeError:
                # Same reasoning as _setRecvReconnectFailures: every object
                # this app builds is a PlayerStatus with a __dict__; a bare
                # slotted instance gets one process-wide lock - coarser, but
                # still serialized.
                return _reconnectFallbackLock
        return lock


def player_status_reconnect(self):
    if getattr(self, "_deliberate_close", False):
        # stop() closed this socket on purpose. Resurrecting it would register
        # a ghost device on a session that is being torn down - the callers'
        # own flag checks end their loops right after this returns.
        logger.info("Skipping PlayerStatus reconnect: the websocket was closed deliberately.")
        return

    generationBefore = getattr(self, "_reconnectGeneration", 0)
    with _reconnectLockFor(self):
        if getattr(self, "_reconnectGeneration", 0) != generationBefore:
            # Another thread reconnected while we waited for the lock; the
            # drop this call was reacting to is already handled.
            logger.info("Skipping PlayerStatus reconnect: another thread just reconnected this websocket.")
            return
        if getattr(self, "_deliberate_close", False):
            logger.info("Skipping PlayerStatus reconnect: the websocket was closed deliberately.")
            return
        _reconnectPlayerStatusUnderLock(self)
        try:
            self._reconnectGeneration = generationBefore + 1
        except AttributeError:  # noqa: S110 - slotted instance: the dedup above degrades
            pass                #  to "always reconnect", which is the pre-lock behavior


def _reconnectPlayerStatusUnderLock(self):
    """The reconnect body. Callers hold this streamer's reconnect lock."""
    logger.info("Reconnecting PlayerStatus websocket...")

    # Close old connection if possible
    try:
        if hasattr(self, "ws") and self.ws:
            self.ws.close()
    except Exception:  # noqa: S110 - the old socket is being replaced; a failure to close
        pass           #  one that is already dead must not block the reconnect

    # Renew session and client token, clearing a stale access token first so
    # _get_auth_vars actually fetches a replacement (see the block above).
    if _accessTokenNeedsRefresh(self.base):
        self.base.access_token = _Undefined
    try:
        self.base.get_session()
        self.base.get_client_token()
    except Exception as e:
        if self.base.access_token is _Undefined:
            # No usable token to fall back on: connecting anyway would send a
            # literal "_Undefined" bearer token - a guaranteed 401 that the
            # callers' bounded retry paths handle better as the real error.
            raise
        logger.warning("Failed to renew session: %s", e)

    # Establish the new websocket LOCALLY and read its init packet before
    # publishing: self.ws is what the push loop's get_packet re-reads under
    # rlock, so publishing first let that thread consume the init frame -
    # leaving this one blocked forever in spotapi's untimed get_init_packet
    # recv() (a hung keep-alive thread, a device never re-registered, and a
    # listener silently degraded to polling).
    uri = f"wss://dealer.spotify.com/?access_token={self.base.access_token}"
    newWs = websockets.sync.client.connect(
        uri,
        user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    try:
        initPacket = json.loads(newWs.recv(timeout=WS_INIT_PACKET_TIMEOUT_SECONDS))
        headers = initPacket.get("headers") if isinstance(initPacket, dict) else None
        connectionId = headers.get("Spotify-Connection-Id") if isinstance(headers, dict) else None
        if connectionId is None:
            raise ValueError("Invalid init packet")  #< same contract as spotapi's get_init_packet
    except BaseException:
        try:
            newWs.close()
        except Exception:  # noqa: S110 - already tearing down a failed handshake
            pass
        raise
    self.ws_dump = initPacket
    self.connection_id = connectionId
    # The flag re-read HERE, not only at the two entry guards above: those run
    # before this body spends 1-3s on the network (get_session,
    # get_client_token, the handshake, the init-packet read), and stop() runs
    # on another thread - it closes whatever self.ws is at THAT instant, the
    # OLD socket. Publishing afterwards stranded the new one, because nothing
    # else ever closes a streamer's socket: the fork's disconnect() has no
    # caller, Spotify.close() only closes the curl TLS client, keep_alive
    # exits on the flag without closing, and signalStop has already dropped
    # the atexit hook. What was left behind is precisely what the entry
    # guard's comment says it exists to prevent - an open dealer socket, its
    # recv_events thread, and a ghost hobs_<device_id> registration on a
    # session being torn down.
    with self.rlock:
        stoppedMidHandshake = getattr(self, "_deliberate_close", False)
        if not stoppedMidHandshake:
            self.ws = newWs
    if stoppedMidHandshake:
        #< outside the lock (close() can block on a socket) and guarded for the
        #  same reason the entry close above is: this one is being abandoned
        try:
            newWs.close()
        except Exception:  # noqa: S110 - abandoning a socket nobody adopted
            pass
        logger.info("Discarding a PlayerStatus reconnect: the websocket was closed "
                    "deliberately while this one was still connecting.")
        return

    # Register and connect device
    self.register_device()
    self.connect_device()

    # Restart the keep_alive thread if it is dead (the reconnect lock keeps
    # two watchers from both starting one)
    if hasattr(self, "keep_alive_thread") and not self.keep_alive_thread.is_alive():
        self.keep_alive_thread = threading.Thread(target=self.keep_alive, daemon=True)
        self.keep_alive_thread.start()

    logger.info("PlayerStatus websocket reconnected successfully.")

# Inject the reconnect method into PlayerStatus class
spotapi.status.PlayerStatus.reconnect = player_status_reconnect


# Patch renew_state to:
# 1. Avoid KeyError: 'devices' or KeyError: 'player_state' on API failures.
# 2. Note: we do NOT deepcopy here - see the state/saved_state property patches
#    below for where the mutation is actually prevented.
def player_status_renew_state(self):
    try:
        self._device_dump = self.connect_device()
        if isinstance(self._device_dump, dict):
            self._state = self._device_dump.get("player_state")
            self._devices = self._device_dump.get("devices")
            # A cluster came back, so the PUT itself worked - stamped even when
            # it carries no player_state, because that is an account with no
            # live Connect session, not a failure. Both cases raise the same
            # ValueError out of the state property below, and this stamp is the
            # only thing that tells them apart: without it the poll loop read a
            # user who wasn't casting anything as a throttling streak (30s of
            # PROCESS-WIDE backoff plus a websocket reconnect, about once a
            # minute) and the stale check read it as a dead session.
            self.stateRenewalSucceededAt = time.monotonic()
        else:
            self._state = None
            self._devices = None
    except Exception as e:
        # spotapi's ParentException keeps the HTTP detail (e.g. the status code)
        # in .error, not in str(e) - surface it or the log can't distinguish
        # throttling from a real outage.
        errorDetail = getattr(e, "error", None)
        if errorDetail:
            logger.warning("Error renewing player state: %s (%s)", e, errorDetail)
        else:
            logger.warning("Error renewing player state: %s", e)
        self._state = None
        self._devices = None

spotapi.status.PlayerStatus.renew_state = player_status_renew_state


# Patch the `state` and `saved_state` properties to pass a shallow copy of
# _state to PlayerState.from_dict, preventing Track.from_dict from corrupting
# the cached _state dict.
#
# Root cause: spotapi's Track.from_dict() mutates its input dict in-place:
#   data["metadata"] = Metadata.from_dict(metadata)
# PlayerState.from_dict(_state) passes _state["track"] directly to
# Track.from_dict, so without a copy, _state["track"]["metadata"] becomes a
# Metadata dataclass. The next call to getConnectPlayerState() then reads
# _state["track"]["metadata"] and tries to call .get("title") on a Metadata
# object -> AttributeError.
#
# copy.copy(_state) is a shallow copy of the outer dict; that's enough because
# Track.from_dict only replaces the "metadata" key on the track dict (a
# nested value), which means we also need to copy the nested "track" dict.
# We use copy.deepcopy for correctness, but only on the _state snapshot -
# this is called once per 3-second poll, so the overhead is negligible.
def _player_status_state_property(self):
    """Gets the current state of the player (patched to prevent _state mutation)."""
    self.renew_state()
    if self._state is None:
        raise ValueError("Could not get player state")
    return spotapi.status.PlayerState.from_dict(copy.deepcopy(self._state))


def _player_status_saved_state_property(self):
    """Gets the last saved state of the player (patched to prevent _state mutation)."""
    if self._state is None:
        self.renew_state()
    if self._state is None:
        raise ValueError("Could not get player state")
    return spotapi.status.PlayerState.from_dict(copy.deepcopy(self._state))


spotapi.status.PlayerStatus.state = property(_player_status_state_property)
spotapi.status.PlayerStatus.saved_state = property(_player_status_saved_state_property)


# 3. Prevent WebsocketStreamer.__init__ from hijacking the process's SIGINT
# handler, and make its atexit registration removable.
#
# SIGINT: __init__ unconditionally does `signal.signal(signal.SIGINT,
# self.handle_interrupt)`, whose handler just does `self.ws.close(); exit(0)`.
# That overwrites Flask/Werkzeug's normal Ctrl+C handling, and since it can
# fire while a background listener thread (see LastPlayed.py's updateLoop) is
# mid-request, it leads to noisy/broken shutdowns instead of a clean
# KeyboardInterrupt. Restore whatever SIGINT handler was registered before
# spotapi's own __init__ ran.
#
# atexit: __init__ also registers an anonymous cleanup closure (a print plus
# disconnect()) whose only reference lives inside atexit's registry, so
# nothing upstream can ever unregister it. One accumulates per streamer
# constructed - with the 6-hourly session recycle that means one per user per
# 6 hours, each pinning its dead session's object graph until process exit and
# printing "Websockets closing due to program ending" there (34 lines at the
# 2026-08-04 shutdown, all but 3 of them for sessions long since replaced).
# The recorder below captures what __init__ registers, and the captured hook
# is immediately swapped for an owned, logger-based one - so even the exit
# line a crash-path streamer produces lands in the log instead of stdout. The
# _deliberate_close property further down drops the hook the moment the app
# declares this streamer's close deliberate. A streamer never deliberately
# closed keeps its (owned) hook: at exit it is the only close that socket
# will get.
original_websocket_streamer_init = spotapi.websocket.WebsocketStreamer.__init__

_streamerAtexitCapture = threading.local()  #< .captured is a list only while a patched __init__ runs in this thread


class _SpotapiAtexitRecorder:
    """Stands in for the atexit module inside spotapi.websocket.

    register() forwards to the real atexit and also records the callback in
    the calling thread's open capture (set by patched_websocket_streamer_init);
    every other attribute proxies through, so the swap stays invisible to any
    other atexit use the fork might grow."""

    def register(self, func, *args, **kwargs):
        result = atexit.register(func, *args, **kwargs)
        capture = getattr(_streamerAtexitCapture, "captured", None)
        if capture is not None:
            capture.append(func)
        return result

    def __getattr__(self, name):
        return getattr(atexit, name)


spotapi.websocket.atexit = _SpotapiAtexitRecorder()


def _makeStreamerExitCleanup(streamer):
    """A logger-based replacement for the fork's print-based atexit closure.

    Closes a websocket left open at process exit. Deliberately takes no lock:
    this runs during interpreter shutdown, where a daemon thread frozen while
    holding rlock would turn a best-effort close into a hung process - the
    2026-07-17 class of failure. The worst a lockless close can do is make a
    concurrent recv()/send() raise ConnectionClosed, which every caller
    already handles."""
    def closeStreamerAtExit():
        ws = getattr(streamer, "ws", None)
        if ws is None:
            return  #< the normal case: every deliberately closed session; stay quiet
        logger.info("Closing a %s websocket left open at process exit", type(streamer).__name__)
        try:
            streamer.ws = None
            ws.close()
        except Exception as e:  # noqa: BLE001 - an exit-time close is best-effort by definition,
            logger.warning(     #  and an exception here would land on stderr mid-teardown
                "Failed to close the leftover websocket: %s", e)
    return closeStreamerAtExit


def _closeSocketOfFailedConstruction(streamer) -> None:
    """Release the dealer socket a construction that raised left behind.

    connect() runs _create_websocket() - which assigns self.ws and then reads
    the init packet - and only afterwards register_device(). So every failure
    from the init-packet read onwards strands an open ClientConnection, and its
    recv_events thread, on a half-built streamer nothing can reach: no Listener
    was assigned, so Listener.stop()'s manager.ws.close() never runs, and
    spotapi registers its own atexit hook only after connect() RETURNS (which
    is also why the unregistration beside this call has nothing to unregister
    on this path). patched_connect forces ping_interval=None process-wide, so
    there is no keepalive to time a silent peer out either - the socket lives
    until the dealer hangs up, and retrying is exactly what _checkLoginLoop's
    restart pass and onStaleWithBackoff do.

    Here rather than inside register_device/connect_device on purpose:
    player_status_reconnect publishes self.ws BEFORE calling them, and
    connect_device is also renew_state's body on the poll tick, so both run
    against a socket with a live owner. This is the one place that knows the
    construction as a whole failed.

    patched_get_init_packet closes its own read's socket at the point of
    failure; this is the backstop that covers the rest of the construction."""
    ws = getattr(streamer, "ws", None)
    if ws is None:
        return   #< failed before _create_websocket got that far
    try:
        ws.close()
    except Exception:  # noqa: BLE001 - the construction's own exception is what the
        logger.debug(  #  caller classifies on; a teardown error must not replace it
            "Could not close the websocket of a failed streamer construction", exc_info=True)


def patched_websocket_streamer_init(self, *args, **kwargs):
    previousSigintHandler = signal.getsignal(signal.SIGINT)
    _streamerAtexitCapture.captured = []
    initSucceeded = False
    try:
        original_websocket_streamer_init(self, *args, **kwargs)
        initSucceeded = True
    finally:
        captured = _streamerAtexitCapture.captured
        _streamerAtexitCapture.captured = None
        try:
            # `is not None` first: getsignal() answers None when the current
            # handler was NOT installed from Python, and signal.signal refuses
            # None with a TypeError - which this only caught ValueError. In a
            # `finally`, where a raised exception REPLACES the one being
            # handled, that would have reported a genuine listener-build
            # failure as an unrelated complaint about a signal handler. There
            # is also nothing to put back in that case: a handler Python did
            # not install is not one Python can reinstall.
            if previousSigintHandler is not None:
                signal.signal(signal.SIGINT, previousSigintHandler)
        except ValueError:
            pass  # signal.signal only works in main thread; silently skip if in worker thread
        if not initSucceeded:
            # A failed construction has no owner to ever set _deliberate_close,
            # so anything it managed to register would be pinned for good - and
            # the socket it opened would be pinned by its own recv_events
            # thread, which is the more expensive half of the same leak.
            for cleanup in captured:
                atexit.unregister(cleanup)
            _closeSocketOfFailedConstruction(self)
    if captured:
        # Swap the fork's print-based closures for one owned cleanup: the exit
        # line lands in the log with everything else, and a failed close at
        # teardown becomes a warning instead of stdout/stderr noise.
        for forkCleanup in captured:
            atexit.unregister(forkCleanup)
        ownedCleanup = _makeStreamerExitCleanup(self)
        atexit.register(ownedCleanup)
        try:
            self._atexitCleanups = [ownedCleanup]
        except AttributeError:  # noqa: S110 - a slotted instance can't record its hook; it just
            pass                #  stays registered for the process's life (logging, not printing)

spotapi.websocket.WebsocketStreamer.__init__ = patched_websocket_streamer_init


def _dropStreamerAtexitCleanups(self) -> None:
    """Unregister every atexit hook recorded for this streamer, once."""
    for cleanup in getattr(self, "_atexitCleanups", ()):
        atexit.unregister(cleanup)
    try:
        self._atexitCleanups = []
    except AttributeError:  # noqa: S110 - slotted instance: nothing was recorded to drop
        pass


def _getDeliberateClose(self):
    return getattr(self, "_deliberateCloseValue", False)


def _setDeliberateClose(self, value) -> None:
    self._deliberateCloseValue = value  #< AttributeError on a slotted instance, which
                                        #  signalStop's own try/except already expects
    if value:
        _dropStreamerAtexitCleanups(self)


# Every reader already does getattr(self, "_deliberate_close", False), so a
# property whose getter defaults to False changes nothing for them. The setter
# is the one place that learns "this close is deliberate" first - making it
# the right moment to drop the atexit hook. That covers Listener.stop()'s
# direct ws.close() path, which never calls disconnect(), as well as every
# session rebuild. No @enforce frozen copy can shadow this (patch 7's
# concern): the name did not exist when the subclasses were decorated, and
# @enforce skips properties regardless.
spotapi.websocket.WebsocketStreamer._deliberate_close = property(_getDeliberateClose, _setDeliberateClose)


original_player_status_init = spotapi.status.PlayerStatus.__init__


def patched_player_status_init(self, *args, **kwargs):
    """PlayerStatus's construction, released as a whole when it fails.

    The base guard above is not enough, because the construction does not end
    when the base __init__ returns. PlayerStatus.__init__ calls
    register_device() a SECOND time (spotapi/status.py, right after
    super().__init__(login) has already run connect() -> register_device()).
    By then patched_websocket_streamer_init has set initSucceeded True and
    swapped in the owned atexit hook, so its `if not initSucceeded` branch is
    skipped - and a raise from that second call left behind exactly what that
    branch exists to release.

    It is worth its own patch because PlayerStatus(login) is the ONLY streamer
    this app ever constructs (Database/Spotify/recentlyPlayed.py), so this
    frame, not the base one, is what "the construction failed" means here.
    Nothing upstream can clean up after it either: the raise unwinds through
    RecentlyPlayedManager.__init__ before Spotify.lastPlayedManager is
    assigned, and through Listener.__init__ before workers/listener.py assigns
    self.listener - so neither Listener.stop() nor signalStop() is ever
    reachable for this object.

    _deliberate_close first, then the socket. The flag is the signal
    patched_keep_alive polls, so setting it is what stops the thread the base
    __init__ started - it would otherwise ping a healthy socket forever, since
    nothing else can ever set it - and its setter is also what unregisters the
    atexit hook that would otherwise pin the whole object until process exit.

    @enforce is not a concern here: it skips names starting with "__", so
    PlayerStatus.__init__ was never frozen into a subclass copy."""
    try:
        original_player_status_init(self, *args, **kwargs)
    except BaseException:
        try:
            self._deliberate_close = True
        except AttributeError:  # noqa: S110 - slotted instance: the setter's own
            pass                #  documented case; the close below still runs
        _closeSocketOfFailedConstruction(self)
        raise


spotapi.status.PlayerStatus.__init__ = patched_player_status_init


# 4. Replace WebsocketStreamer.keep_alive outright, and disable the fork's
# _supervise thread.
#
# Both spotapi builds ship a keep_alive that mishandles a dead connection: the
# PyPI build let ConnectionClosed crash the ping thread (pings silently
# stopped until the 30-minute stale-feed detector rebuilt the session), and
# the fork catches EVERYTHING internally and retries reconnect() forever -
# which made the previous wrapper here (catch ConnectionClosed around the
# original) dead code, because no exception ever escaped for it to see.
# Neither build knows _deliberate_close, so a listener stop/session rebuild
# left its daemon threads reconnecting the deliberately-closed socket for the
# rest of the process's life - succeeding at first (ghost device
# registrations), then 401ing forever once their token expired (the
# 2026-08-01 storm, together with the token staleness fixed in patch 2).
#
# So keep_alive is now a full replacement rather than a wrapper: ping every
# WS_KEEP_ALIVE_PING_INTERVAL_SECONDS, and treat ANY dead socket one way -
# exit if the close was deliberate, otherwise reconnect with a bounded number
# of attempts. A clean close (ConnectionClosedOK) that was NOT deliberate is
# Spotify hanging up (observed about hourly on the dealer) and reconnects
# too: updateLoop's push->poll fallback is one-way, so quietly stopping pings
# would degrade the listener to polling until its next rebuild.
WS_KEEP_ALIVE_PING_INTERVAL_SECONDS = 60   #< spotapi's own cadence, kept
WS_KEEP_ALIVE_STOP_POLL_SECONDS = 1.0      #< how quickly a sleeping thread notices _deliberate_close
WS_KEEP_ALIVE_PING_PAYLOAD = '{"type":"ping"}'
WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES = 3
WS_KEEP_ALIVE_RECONNECT_BACKOFF_SECONDS = 10


def _sleepInterruptibly(self, seconds) -> bool:
    """Sleep up to `seconds` in WS_KEEP_ALIVE_STOP_POLL_SECONDS steps. False as
    soon as _deliberate_close is set (the thread should exit), True once the
    full wait elapsed."""
    deadline = time.monotonic() + seconds
    while True:
        if getattr(self, "_deliberate_close", False):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(WS_KEEP_ALIVE_STOP_POLL_SECONDS, remaining))


def patched_keep_alive(self):
    consecutiveFailures = 0
    while True:
        if not _sleepInterruptibly(self, WS_KEEP_ALIVE_PING_INTERVAL_SECONDS):
            logger.info("Websocket closed on shutdown, stopping keep-alive pings.")
            return

        pingError = None
        with self.rlock:
            ws = self.ws
            if ws is not None:
                try:
                    ws.send(WS_KEEP_ALIVE_PING_PAYLOAD)
                except Exception as e:  # noqa: BLE001 - any send failure means the socket is dead
                    pingError = e
        if ws is not None and pingError is None:
            consecutiveFailures = 0
            continue

        # Dead socket: self.ws is gone, or the ping could not be sent on it.
        while True:
            if getattr(self, "_deliberate_close", False):
                logger.info("Websocket closed on shutdown, stopping keep-alive pings.")
                return
            reconnect = getattr(self, "reconnect", None)
            if reconnect is None:
                logger.warning(
                    "Websocket connection lost (%s) and no reconnect() available, stopping keep-alive pings.",
                    pingError)
                return
            logger.warning("Websocket connection lost (%s), attempting reconnect...",
                           pingError if pingError is not None else "socket is gone")
            try:
                reconnect()
                consecutiveFailures = 0
                break
            except Exception as reconnectError:
                consecutiveFailures += 1
                logger.warning(
                    "Websocket reconnect failed (%d/%d): %s",
                    consecutiveFailures, WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES, reconnectError,
                )
                if consecutiveFailures >= WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES:
                    logger.error(
                        "Giving up websocket reconnects after %d attempts; the stale-feed detector will rebuild the session.",
                        WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES,
                    )
                    return
                if not _sleepInterruptibly(self, WS_KEEP_ALIVE_RECONNECT_BACKOFF_SECONDS):
                    logger.info("Websocket closed on shutdown, stopping keep-alive pings.")
                    return

spotapi.websocket.WebsocketStreamer.keep_alive = patched_keep_alive


def patched_supervise(self):
    """No-op replacement for the fork's _supervise thread body.

    The fork's supervisor reconnects whenever self.ws is None or closed -
    which is exactly the state disconnect() leaves ON PURPOSE - with no
    _deliberate_close check, no attempt ceiling, and print() diagnostics.
    Every reconnect path this app wants already exists with bounds and stop
    flags: keep_alive above, the push loop's _reconnectAfterDroppedPacket,
    and the poll loop's failure-threshold escalation. A fourth, unbounded
    one adds storms, not resilience."""
    return


if hasattr(spotapi.websocket.WebsocketStreamer, "_supervise"):
    spotapi.websocket.WebsocketStreamer._supervise = patched_supervise


# 5. Patch WebsocketStreamer.get_packet so a push channel can actually be read.
#
# spotapi's version holds self.rlock across an UNBOUNDED self.ws.recv(), and
# keep_alive above needs that same lock to send its 60-second ping. On a quiet
# account the receive loop parks inside recv() holding the lock, the ping never
# goes out, and Spotify drops the connection on its idle timeout - recovered by
# a full re-login, the most bot-detection-sensitive path this app has. That is
# almost certainly why LastPlayedManger polls connect-state every few seconds
# instead of listening, and it means adopting spotapi's EventManager as-is
# would be worse than the polling it would replace.
#
# Its `except Exception` is the other half: a plain timeout - the NORMAL state
# of a push channel, which measured 3.75 h between updates overnight - was
# treated as a reason to reconnect, forever, on a 1-second sleep.
#
# Nothing in this application calls get_packet yet; PlayerStatus only sends
# pings and never reads. This lands on its own because it is a prerequisite for
# reading the dealer pushes (eventDrivenConnectStatePlan.md Phase 2) and is
# strictly safer than what it replaces even if that never happens.
WS_RECV_TIMEOUT_SECONDS = 1.0    #< ceiling on how long rlock may be held, so the ping thread
                                  #  waits at most this long for its turn
WS_RECV_MAX_RECONNECT_FAILURES = 3  #< then stand down and let the stale-feed detector rebuild


def patched_get_packet(self, timeout: float = WS_RECV_TIMEOUT_SECONDS):
    """The next websocket frame as a dict, or None.

    None means "nothing to act on, call again": the wait elapsed, the socket is
    gone, shutdown was requested, or a frame arrived that wasn't a JSON object.
    Callers loop, which also gives them a place to check their own stop flags -
    spotapi's EventManager._listen already skips a None event, so its contract
    is unchanged."""
    if getattr(self, "_deliberate_close", False):
        return None

    try:
        with self.rlock:
            ws = self.ws        #< re-read under the lock: a concurrent reconnect swaps it
            if ws is None:
                return None
            raw = ws.recv(timeout=timeout)
    except TimeoutError:
        return None             #< silence is the normal state of a push channel, not a fault
    except websockets.exceptions.ConnectionClosed as e:
        # A clean close (ConnectionClosedOK) lands here too, on purpose:
        # Spotify hangs up the dealer cleanly about once an hour, and until
        # this reconnected, the push loop logged "closed cleanly" once per
        # 1-second poll while waiting up to a minute for keep_alive's next
        # ping to notice - lost push time, against a one-way push->poll
        # fallback. A DELIBERATE close is told apart by the flag, which
        # _reconnectAfterDroppedPacket checks first.
        if not _reconnectAfterDroppedPacket(self, e) and timeout:
            # Nothing came back on this socket, and recv() on a CLOSED one
            # raises INSTANTLY rather than waiting out the timeout - so
            # returning here hands the caller a turn that cost nothing.
            # _runPushLoop has no sleep of its own (this timeout IS its
            # pacing), so once the reconnect ceiling latched - three failures,
            # after which _reconnectAfterDroppedPacket returns without even
            # trying again - it spun a core flat out for the five minutes
            # until the frame-silence watchdog fell back to polling. Skipped
            # after a SUCCESSFUL reconnect, where the next recv has a live
            # socket to wait on. Interruptibly, so a deliberate close still
            # exits at once.
            _sleepInterruptibly(self, timeout)
        return None
    except Exception as e:  # noqa: BLE001 - anything else is diagnosed, never silently retried
        logger.warning("Unexpected error reading websocket packet: %s: %s",
                       type(e).__name__, e)
        return None

    # A frame arrived, so reads work again: clear whatever a past outage left
    # on the receive-reconnect counter. Nothing else clears it - patched_keep_alive
    # keeps a separate local count and never touches this one - so one ~2-minute
    # dealer outage retired this path for the life of the streamer while
    # keep-alive's own reconnect succeeded and frames resumed. Every later
    # routine hangup then had _reconnectAfterDroppedPacket return False in
    # silence, leaving the loop to pace itself 1s at a time until the next ping
    # noticed. Read-guarded: every pushed frame runs this line.
    if getattr(self, "_recvReconnectFailures", 0):
        _setRecvReconnectFailures(self, 0)

    try:
        packet = json.loads(raw)
    except Exception:
        logger.warning("Dropping unparsable websocket frame (%d bytes)", len(raw))
        return None
    if not isinstance(packet, dict):
        # dict(json.loads(...)) is what spotapi did, so a JSON array or scalar
        # raised straight into its reconnect-on-anything handler.
        logger.warning("Dropping non-object websocket frame of type %s", type(packet).__name__)
        return None

    self.ws_dump = packet
    return packet


def _reconnectAfterDroppedPacket(self, closeError) -> bool:
    """Bounded recovery for a dropped receive socket. Returns whether the
    socket is actually back.

    Mirrors patched_keep_alive's shape - one concise line per attempt, a hard
    ceiling, and an immediate stop once the underlying HTTP session is closed
    for good - rather than spotapi's unbounded `while True` + sleep(1), which
    turns an unreachable endpoint into a reconnect storm against the same
    endpoint that just refused us.

    Every "no" answer here leaves self.ws pointing at a socket that is already
    closed (the reconnect body closes it on entry and only reassigns on
    success), and recv() on one of those raises instantly - so the caller has
    to do its own pacing on a False. See patched_get_packet."""
    if getattr(self, "_deliberate_close", False):
        return False
    reconnect = getattr(self, "reconnect", None)
    if reconnect is None:
        logger.warning("Websocket dropped (%s) and no reconnect() available.", closeError)
        return False

    failures = getattr(self, "_recvReconnectFailures", 0)
    if failures >= WS_RECV_MAX_RECONNECT_FAILURES:
        return False  #< already gave up and said so; stay quiet rather than log per packet

    logger.warning("Websocket dropped while reading (%s), attempting reconnect...", closeError)
    try:
        reconnect()
        _setRecvReconnectFailures(self, 0)
        return True
    except Exception as reconnectError:
        if _isSessionClosedError(reconnectError):
            logger.error("Websocket receive giving up: the HTTP session is closed and "
                         "cannot be revived: %s", reconnectError)
            _setRecvReconnectFailures(self, WS_RECV_MAX_RECONNECT_FAILURES)
            return False
        failures += 1
        _setRecvReconnectFailures(self, failures)
        logger.warning("Websocket reconnect failed (%d/%d): %s",
                       failures, WS_RECV_MAX_RECONNECT_FAILURES, reconnectError)
        if failures >= WS_RECV_MAX_RECONNECT_FAILURES:
            logger.error(
                "Giving up websocket receive reconnects after %d attempts; the "
                "stale-feed detector will rebuild the session.",
                WS_RECV_MAX_RECONNECT_FAILURES)


def _setRecvReconnectFailures(self, count: int) -> None:
    """Remember the failure count on the streamer, tolerating an instance that
    won't take new attributes.

    WebsocketStreamer declares __slots__ without this name. Every object this
    app actually builds is a PlayerStatus, which defines no __slots__ of its own
    and so still carries a __dict__ - the same assumption signalStop already
    makes for _deliberate_close, with the same guard. If a future/bare instance
    ever refuses the write, the ceiling below stops applying, so say so once
    rather than silently reverting to spotapi's unbounded retries."""
    try:
        self._recvReconnectFailures = count
    except AttributeError:
        logger.warning(
            "Cannot track websocket receive-reconnect failures on a %s "
            "(__slots__); the %d-attempt ceiling will not apply.",
            type(self).__name__, WS_RECV_MAX_RECONNECT_FAILURES)


spotapi.websocket.WebsocketStreamer.get_packet = patched_get_packet


# 5b. Bound the init-packet read that CONSTRUCTION performs.
#
# spotapi reads the handshake's first frame with a bare self.ws.recv() and no
# timeout, so a dealer that accepts the socket and then says nothing parks the
# caller forever - the exact condition patched_reconnect above already bounds
# on the reconnect path. Construction reaches the same read through
# _create_websocket, and construction runs under the per-user _listener_lock
# (Database/workers/listener.py's startListener, via RecentlyPlayedManager):
# a thread parked there holds that lock forever, so the user's next
# startListener blocks forever too - and since _ensureAllUsersLogin walks
# users one at a time, the process-wide login-check loop wedges with it and
# NO user gets login re-checks or milestone detection until a restart.
# Timing out instead surfaces as a failed listener build, which the login
# loop already contains per user and retries on its next pass.
def patched_get_init_packet(self) -> str:
    """spotapi's get_init_packet, with the read bounded and its contract kept
    (same ws_dump assignment, same ValueError on a packet with no connection
    id, same return value) - and the socket released when it fails.

    The close matters because `self.ws` is assigned by _create_websocket BEFORE
    this is called, so raising past it strands an open ClientConnection and its
    two daemon threads (recv_events, keepalive) on a half-built PlayerStatus
    that nothing can reach: no Listener was assigned, so Listener.stop()'s
    manager.ws.close() never runs, and spotapi registers its atexit hook only
    after connect() returns. A dealer that answers pings but never sends the
    init frame would leak one per retry - and retries are what the login loop
    does. patched_reconnect closes the same way on the same failure."""
    try:
        self.ws_dump = dict(json.loads(self.ws.recv(timeout=WS_INIT_PACKET_TIMEOUT_SECONDS)))

        if (self.ws_dump.get("headers") is None
                or dict(self.ws_dump["headers"]).get("Spotify-Connection-Id") is None):
            raise ValueError("Invalid init packet")
    except BaseException:
        try:
            self.ws.close()
        except Exception:  # noqa: S110 - already tearing down a failed handshake
            pass
        raise

    return self.ws_dump["headers"]["Spotify-Connection-Id"]


spotapi.websocket.WebsocketStreamer.get_init_packet = patched_get_init_packet


# 6. Replace register_device/connect_device's stdout diagnostics with logging.
#
# On a failed response both print a five-line block ("REGISTER DEVICE FAILED",
# device id, connection id, error, raw body) straight to stdout before
# raising - invisible in app.log, where the callers' one-line warnings land.
# And these are hot paths: connect_device carries renew_state (the poll tick)
# and the push loop's periodic resubscribe, so one throttled spell printed a
# block every few seconds to a console nobody reads while the incident was
# being diagnosed from the log. Full replacements with byte-identical request
# payloads and the same raise contract; the diagnostics go through the
# module's safe formatters, which never raise and never log a Spotify HTML
# fallback page or a credential-bearing header.
WS_REGISTER_DEVICE_URL = "https://gue1-spclient.spotify.com/track-playback/v1/devices"
WS_CONNECT_DEVICE_URL_TEMPLATE = "https://gue1-spclient.spotify.com/connect-state/v1/devices/hobs_{deviceId}"


def _logDeviceCallFailure(self, operation: str, resp) -> None:
    logger.warning(
        "%s failed: device_id=%s, connection_id=%s, error=%s, response=%s, headers=%s",
        operation,
        getattr(self, "device_id", None),
        getattr(self, "connection_id", None),
        getattr(getattr(resp, "error", None), "string", None),
        _describeResponseBody(getattr(resp, "response", None), RESPONSE_SNIPPET_MAX_LEN),
        _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None)))


def patched_register_device(self) -> None:
    """spotapi's register_device with its stdout prints routed to logging.
    The payload is the fork's, verbatim; the raise contract is unchanged."""
    payload = {
        "device": {
            "brand": "spotify",
            "capabilities": {
                "change_volume": True,
                "enable_play_token": True,
                "supports_file_media_type": True,
                "play_token_lost_behavior": "pause",
                "disable_connect": False,
                "audio_podcasts": True,
                "video_playback": True,
                "manifest_formats": [
                    "file_ids_mp3",
                    "file_urls_mp3",
                    "manifest_urls_audio_ad",
                    "manifest_ids_video",
                    "file_urls_external",
                    "file_ids_mp4",
                    "file_ids_mp4_dual",
                    "manifest_urls_audio_ad",
                ],
            },
            "device_id": self.device_id,
            "device_type": "computer",
            "metadata": {},
            "model": "web_player",
            "name": "Web Player (Chrome)",
            "platform_identifier": "web_player windows 10;chrome 120.0.0.0;desktop",
            "is_group": False,
        },
        "outro_endcontent_snooping": False,
        "connection_id": self.connection_id,
        "client_version": "harmony:4.43.2-a61ecaf5",
        "volume": 65535,
    }

    resp = self.client.post(WS_REGISTER_DEVICE_URL, json=payload, authenticate=True)

    if resp.fail:
        _logDeviceCallFailure(self, "Websocket device registration", resp)
        raise spotapi.exceptions.WebSocketError(
            "Could not register device", error=resp.error.string)


def patched_connect_device(self):
    """spotapi's connect_device with its stdout prints routed to logging.
    The payload is the fork's, verbatim; returns the cluster reply unchanged."""
    payload = {
        "member_type": "CONNECT_STATE",
        "device": {
            "device_info": {
                "capabilities": {
                    "can_be_player": False,
                    "hidden": True,
                    "needs_full_player_state": True,
                }
            }
        },
    }
    headers = {
        "x-spotify-connection-id": self.connection_id,
    }

    resp = self.client.put(
        WS_CONNECT_DEVICE_URL_TEMPLATE.format(deviceId=self.device_id),
        json=payload, authenticate=True, headers=headers)

    if resp.fail:
        _logDeviceCallFailure(self, "Websocket device connect", resp)
        raise spotapi.exceptions.WebSocketError(
            "Could not connect device", error=resp.error.string)

    return resp.response


spotapi.websocket.WebsocketStreamer.register_device = patched_register_device
spotapi.websocket.WebsocketStreamer.connect_device = patched_connect_device


# 7. Remove @enforce's frozen copies of the methods patched above.
#
# spotapi's @enforce class decorator iterates dir(cls) - inherited methods
# included - and setattr's a signature-checking wrapper of each onto the
# decorated class itself. PlayerStatus and EventManager are both decorated, so
# at import time they froze their own copies of the ORIGINAL base-class
# methods, and those copies shadow every class-level patch above on the
# classes this app actually instantiates: get_packet's frozen copy still had
# the (self)-only signature, so the push loop's get_packet(timeout=...) raised
# "got an unexpected keyword argument 'timeout'" and killed the listener
# thread (2026-07-31) - and patched_keep_alive never ran at all. Deleting the
# copies lets method lookup fall through the MRO to the patched versions.
# Dunders (__init__, patch 3) never need this: @enforce skips them - but ONLY
# dunders, so single-underscore names like the fork's _supervise are frozen
# like any other. The state/saved_state properties don't either: @enforce
# skips properties. The vars() guard covers names a given spotapi version
# never froze (the PyPI/fork builds disagree on whether reconnect or
# _supervise exist to begin with).
_ENFORCE_SHADOWED_METHODS = (
    (spotapi.status.PlayerStatus,
     ("keep_alive", "get_packet", "get_init_packet", "_supervise",
      "register_device", "connect_device")),
    (spotapi.status.EventManager,
     ("keep_alive", "get_packet", "get_init_packet", "reconnect", "renew_state",
      "_supervise", "register_device", "connect_device")),
)
for _shadowedClass, _methodNames in _ENFORCE_SHADOWED_METHODS:
    for _methodName in _methodNames:
        if _methodName in vars(_shadowedClass):
            delattr(_shadowedClass, _methodName)


RESPONSE_SNIPPET_MAX_LEN = 1000
RESPONSE_ERROR_SNIPPET_MAX_LEN = 200

# How much of a response body survives into a log line / error message while
# FLASK_DEBUG is off: enough to read a short API error verbatim, far too little
# to bury the line under a page of markup.
RESPONSE_SUMMARY_MAX_LEN = 120
HTML_SNIFF_LEN = 200      #< leading chars examined to decide "web page, not an API reply"
HTML_TITLE_MAX_LEN = 60   #< the page's <title>, kept as its identity

HTML_BODY_MARKERS = ("<!doctype", "<html")
_HTML_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

#< aliased to the private name this module has always used, so the ~10 tests that
#  patch "Database.patches._flaskDebugEnabled" keep working - the call sites
#  resolve it from module globals, which is what patch replaces. Import it
#  plainly and every one of them silently stops taking effect.
_flaskDebugEnabled = flaskDebugEnabled


def _looksLikeHtml(text: str) -> bool:
    return text.lstrip()[:HTML_SNIFF_LEN].lower().startswith(HTML_BODY_MARKERS)


def _htmlTitle(text: str) -> str | None:
    match = _HTML_TITLE_PATTERN.search(text)
    if match is None:
        return None
    return " ".join(match.group(1).split())[:HTML_TITLE_MAX_LEN] or None


def _describeResponseBody(response, maxLen: int) -> str | None:
    """What of a response body may be written to a log line or error message.

    Spotify answers a rate-limited or bot-checked request to these endpoints
    with its full HTML fallback page, so logging the body verbatim wrote a
    kilobyte of markup and CSS per occurrence - and three times per flake, once
    here and twice more when the listener re-logged the exception carrying it.
    With FLASK_DEBUG off a page collapses to its size and <title> (the part
    that tells a Spotify fallback apart from a Cloudflare challenge, which is
    what these diagnostics exist to identify), while short plain-text bodies -
    the case where the body IS the diagnostic - are kept verbatim. Turn
    FLASK_DEBUG on to get the raw snippet back for a bug report."""
    if response is None:
        return None
    text = str(response)
    if _flaskDebugEnabled():
        return text[:maxLen]
    if _looksLikeHtml(text):
        title = _htmlTitle(text)
        titlePart = f", title={title!r}" if title else ""
        return f"<html page, {len(text)} chars{titlePart}>"
    if len(text) <= RESPONSE_SUMMARY_MAX_LEN:
        return text
    return f"{text[:RESPONSE_SUMMARY_MAX_LEN]}... ({len(text)} chars total)"


# Response headers that may be written to the log. Spotify's replies to these
# endpoints carry live session credentials - __Host-sp_csrf_sid (a one-hour
# session cookie) and x-csrf-token - so the header dict must never be logged
# wholesale. This is an allowlist rather than a denylist on purpose: a header
# nobody anticipated defaults to dropped, so a credential-bearing header added
# upstream can't leak in the window before anyone notices it exists.
LOGGABLE_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "content-encoding",
    "date", "server", "retry-after",
    "cf-ray", "cf-cache-status",
})
# Prefix-matched alongside the exact names above - the rate-limit family varies
# by endpoint (x-ratelimit-remaining / -limit / -reset) and all of it is useful.
LOGGABLE_RESPONSE_HEADER_PREFIXES = ("x-ratelimit-",)


def _safeResponseHeaders(headers) -> dict:
    """The subset of `headers` that is safe to write to the log.

    Takes the raw header mapping (or anything at all) and returns a plain dict
    holding only allowlisted entries, preserving their original casing. Never
    raises: every caller is already on an error path, where a failure to
    format the diagnostic would replace a logged warning with an exception."""
    try:
        return {
            name: value
            for name, value in headers.items()
            if (lowered := str(name).lower()) in LOGGABLE_RESPONSE_HEADERS
            or lowered.startswith(LOGGABLE_RESPONSE_HEADER_PREFIXES)
        }
    except Exception:
        # Not a mapping at all (None, a bare object, a test double whose
        # .items() isn't iterable) - an unloggable header set is not a reason
        # to lose the warning it was going to be attached to.
        return {}


def patch_spotapi_user() -> bool:
    """Patch spotapi.user.User methods to log detailed response information
    on JSON deserialization failure, helping identify rate-limiting or
    Cloudflare blocks.
    """
    try:
        import spotapi.user
        from spotapi.exceptions import UserError
        from collections.abc import Mapping
        from typing import Any


        def patched_get_user_info(self) -> Mapping[str, Any]:
            url = "https://www.spotify.com/api/account-settings/v1/profile"
            if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_ACQUIRE_TIMEOUT_SECONDS):
                raise SpotifyLocallyRateLimitedError(
                    f"Spotify rate limit backoff in progress - skipped {ENDPOINT_ACCOUNT_PROFILE}")
            resp = self.login.client.get(url)

            if resp.fail:
                logger.warning(
                    "spotapi.User.get_user_info HTTP request failed: endpoint=%s, status=%s, error=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PROFILE,
                    resp.status_code,
                    resp.error.string if hasattr(resp.error, "string") else None,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PROFILE)
                raise UserError("Could not get user info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_user_info returned non-Mapping response: endpoint=%s, status=%s, type=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PROFILE,
                    resp.status_code,
                    type(resp.response).__name__,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PROFILE)
                raise UserError(
                    f"Invalid JSON (Status: {resp.status_code}, Type: {type(resp.response).__name__}, "
                    f"Response: {_describeResponseBody(resp.response, RESPONSE_ERROR_SNIPPET_MAX_LEN)})"
                )

            self.csrf_token = resp.raw.headers.get("X-Csrf-Token")
            return resp.response

        def patched_get_plan_info(self) -> Mapping[str, Any]:
            url = "https://www.spotify.com/ca-en/api/account/v2/plan/"
            if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_ACQUIRE_TIMEOUT_SECONDS):
                raise SpotifyLocallyRateLimitedError(
                    f"Spotify rate limit backoff in progress - skipped {ENDPOINT_ACCOUNT_PLAN}")
            resp = self.login.client.get(url)

            if resp.fail:
                logger.warning(
                    "spotapi.User.get_plan_info HTTP request failed: endpoint=%s, status=%s, error=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PLAN,
                    resp.status_code,
                    resp.error.string if hasattr(resp.error, "string") else None,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PLAN)
                raise UserError("Could not get user plan info", error=resp.error.string)

            if not isinstance(resp.response, Mapping):
                logger.warning(
                    "spotapi.User.get_plan_info returned non-Mapping response: endpoint=%s, status=%s, type=%s, response=%s, headers=%s",
                    ENDPOINT_ACCOUNT_PLAN,
                    resp.status_code,
                    type(resp.response).__name__,
                    _describeResponseBody(resp.response, RESPONSE_SNIPPET_MAX_LEN),
                    _safeResponseHeaders(getattr(getattr(resp, "raw", None), "headers", None))
                )
                _backoffIfThrottled(resp.status_code, resp.response, ENDPOINT_ACCOUNT_PLAN)
                raise UserError(
                    f"Invalid JSON (Status: {resp.status_code}, Type: {type(resp.response).__name__}, "
                    f"Response: {_describeResponseBody(resp.response, RESPONSE_ERROR_SNIPPET_MAX_LEN)})"
                )

            return resp.response

        spotapi.user.User.get_user_info = patched_get_user_info
        spotapi.user.User.get_plan_info = patched_get_plan_info
        return True
    except (ModuleNotFoundError, ImportError):
        return False


# --- Pinned TOTP secret ---------------------------------------------------------
#
# Spotify's web player proves itself to open.spotify.com/api/token with a TOTP
# derived from a secret Spotify rotates occasionally. spotapi does not ship that
# secret as configuration - it FETCHES it, from a third-party mirror
# (code.thetadev.de), on every cold start and every 15 minutes after, and falls
# back SILENTLY to its own hardcoded copy when the fetch fails. That put a host
# this project does not control, cannot pin and did not choose directly in the
# login path of every user.
#
# Verified 2026-07-30: the mirror's newest entry (version 61) is byte-identical
# to spotapi's own _FALLBACK_SECRET. The request being removed here was
# returning a value the library already had compiled in, so pinning changes
# nothing about what is sent to Spotify - it only deletes the round trip and the
# dependency. tests/test_patches.py asserts that equality, so a spotapi bump
# that changes its fallback fails loudly instead of drifting.
#
# WHEN SPOTIFY ROTATES: logins stop working and _get_auth_vars raises
# BaseClientError("Could not get session auth tokens"). The wrapper below turns
# that into a log line naming these constants. Two ways to recover:
#   - an operator can set SPOTIFY_TOTP_SECRET="<version>:<b1,b2,...>" and
#     restart, without waiting for a new image;
#   - the fix proper is to refresh the two constants here and cut a release.
# Rotation becomes a loud, infrequent, deliberate maintenance event instead of a
# silent runtime dependency on someone else's uptime.
SPOTIFY_TOTP_SECRET_VERSION = 61
SPOTIFY_TOTP_SECRET_BYTES = (
    44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120,
    97, 75, 76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78,
)

TOTP_SECRET_ENV_VAR = "SPOTIFY_TOTP_SECRET"
TOTP_SECRET_BYTE_MAX = 255   #< a secret byte is a byte; anything else is a typo, not a rotation

# How many token requests must fail in a row before the admin panel calls it a
# rotation. A rotation is total and instance-wide - confirmed empirically on
# 2026-07-30 by running the smoke test against a deliberately wrong secret:
# every public lookup failed with "Could not get session auth tokens", whereas
# a bad-cookies run left them all passing and failed only current_user(). But a
# 429, a Spotify outage or a network blip raise the same exception, so a single
# failure proves nothing. Same "don't believe it until it repeats" shape as
# SCOPE_ERROR_CONFIRM_THRESHOLD in the listener.
TOTP_ROTATION_CONFIRM_THRESHOLD = 3

# Once a rotation is CONFIRMED, the replacement is read from Spotify's own
# web-player bundle (Database/Spotify/totpSecret.py) rather than from any
# third-party mirror. Two unauthenticated requests, and only ever after the
# threshold above - the pinned secret remains the normal path, so if Spotify
# restructures that bundle the result is "recovery unavailable" (exactly
# today's behaviour) rather than broken logins.
#
# On by default because the source is Spotify itself and it only runs when the
# instance is already failing; the switch exists for operators who would rather
# nothing touched authentication on its own.
TOTP_AUTO_RECOVER_ENV_VAR = "SPOTIFY_TOTP_AUTO_RECOVER"

# An instance in this state retries on every request, so without a cooldown a
# rotation would turn into a request flood against Spotify's CDN.
TOTP_RECOVERY_COOLDOWN_SECONDS = 15 * 60

_totpAuthLock = threading.Lock()
_totpConsecutiveFailures = 0
_totpFirstFailureAt = None   #< monotonic; "how long has this been going on"
_totpAdoptedSecret = None    #< (version, bytearray) once recovery has found a newer one
_totpLastRecoveryAt = None   #< monotonic; gates the cooldown above
_totpRecoveryThread = None   #< daemon running attemptTotpRecovery; tests join it


def recordTotpAuthFailure() -> int:
    """Count a failed session-token request. Returns the new streak length."""
    global _totpConsecutiveFailures, _totpFirstFailureAt
    with _totpAuthLock:
        if _totpConsecutiveFailures == 0:
            _totpFirstFailureAt = time.monotonic()
        _totpConsecutiveFailures += 1
        return _totpConsecutiveFailures


def recordTotpAuthSuccess() -> None:
    """Clear the streak. Whatever it was, it is over - and a stale alarm on the
    admin panel is worse than no alarm, because it trains people to ignore it."""
    global _totpConsecutiveFailures, _totpFirstFailureAt
    with _totpAuthLock:
        _totpConsecutiveFailures = 0
        _totpFirstFailureAt = None


def resetTotpAuthState() -> None:
    """Test seam - this state is process-global, so tests must not inherit each
    other's streaks, adopted secrets, cooldowns or in-flight recoveries."""
    global _totpAdoptedSecret, _totpLastRecoveryAt, _totpRecoveryThread
    thread = _totpRecoveryThread
    if thread is not None and thread.is_alive():
        #< joined OUTSIDE the lock: the running recovery needs _totpAuthLock to
        #  finish. In tests the fetch is always patched, so this returns fast.
        thread.join(timeout=5)
    recordTotpAuthSuccess()
    with _totpAuthLock:
        _totpAdoptedSecret = None
        _totpLastRecoveryAt = None
        _totpRecoveryThread = None


def _autoRecoverEnabled() -> bool:
    raw = os.environ.get(TOTP_AUTO_RECOVER_ENV_VAR)
    if raw is None:
        return True   #< default on
    return raw.strip().lower() in TRUTHY_ENV_VALUES


def attemptTotpRecovery() -> bool:
    """Read the current secret from Spotify's web player and adopt it if it is
    newer than the one in force. True when something was adopted.

    Strictly-newer only: re-reading the version we already have proves nothing
    changed, and an older one (a stale edge, a rollback) must never walk us
    backwards. Never raises - the caller is already handling a failure, and an
    exception here would replace a diagnosable auth error with a network one."""
    global _totpAdoptedSecret, _totpLastRecoveryAt

    if not _autoRecoverEnabled():
        return False

    now = time.monotonic()
    with _totpAuthLock:
        if _totpLastRecoveryAt is not None and now - _totpLastRecoveryAt < TOTP_RECOVERY_COOLDOWN_SECONDS:
            return False
        _totpLastRecoveryAt = now

    currentVersion, _ = _resolveTotpSecret()
    candidates = [(version, secret) for version, secret in fetchWebPlayerSecrets()
                  if version > currentVersion]
    if not candidates:
        logger.warning(
            "Read Spotify's web player for a newer TOTP secret and found none newer "
            "than version %s. If logins are still failing, the cause is something "
            'else, or set %s="<version>:<comma-separated bytes>" manually.',
            currentVersion, TOTP_SECRET_ENV_VAR)
        return False

    version, secret = max(candidates, key=lambda pair: pair[0])
    with _totpAuthLock:
        _totpAdoptedSecret = (version, bytearray(secret))
    logger.warning(
        "Adopted TOTP secret version %s, read from Spotify's own web player (was %s). "
        "Logins should recover on the next attempt. This lives in memory only - pin it "
        "in Database/patches.py (SPOTIFY_TOTP_SECRET_VERSION/_BYTES) to make it "
        "permanent, or it will be re-read after every restart.",
        version, currentVersion)
    return True


def _startTotpRecoveryInBackground() -> None:
    """attemptTotpRecovery on a daemon thread.

    The thread that trips the confirmation threshold can be a Flask request
    thread - login's cookie verification reaches _get_auth_vars - and recovery
    is two 15s-timeout GETs plus a multi-MB bundle download, so running it
    inline hung a login POST for ~30s+ during the exact incident it exists
    for. Nothing needs the result: the adopted secret takes effect on the NEXT
    attempt by design. attemptTotpRecovery's cooldown gate (stamped under the
    lock before any network) keeps overlapping spawns from stacking fetches;
    the is_alive check just avoids piling up idle thread objects."""
    global _totpRecoveryThread
    with _totpAuthLock:
        if _totpRecoveryThread is not None and _totpRecoveryThread.is_alive():
            return
        _totpRecoveryThread = threading.Thread(
            target=attemptTotpRecovery, name="totp-secret-recovery", daemon=True)
        _totpRecoveryThread.start()


def totpAuthSnapshot() -> dict:
    """What /admin shows for the pinned TOTP secret.

    Deliberately answers "what now?" as well as "what is wrong": which version
    is actually in force (which is NOT the pinned one when an override is set),
    and the name of the variable to set."""
    activeVersion, _ = _resolveTotpSecret()
    with _totpAuthLock:
        failures = _totpConsecutiveFailures
        firstAt = _totpFirstFailureAt
        adopted = _totpAdoptedSecret
    # "Parses", not "is set": _resolveTotpSecret IGNORES a malformed override,
    # so reporting one as active would claim a dead value is in force - and
    # suppress the autoRecovered note below, whose "pin it before a restart"
    # advice is exactly what that operator needs.
    envOverride = False
    raw = os.environ.get(TOTP_SECRET_ENV_VAR, "").strip()
    if raw:
        try:
            _parseTotpSecretOverride(raw)
            envOverride = True
        except ValueError:
            pass   #< _resolveTotpSecret already logged the malformed value
    return {
        "pinnedVersion": SPOTIFY_TOTP_SECRET_VERSION,
        "activeVersion": activeVersion,
        # Distinguished on purpose: "someone set a variable" and "we adopted
        # one off Spotify" call for different follow-up, and both differ from
        # the pinned default.
        "overrideActive": envOverride,
        "autoRecovered": not envOverride and adopted is not None,
        "overrideEnvVar": TOTP_SECRET_ENV_VAR,
        "consecutiveFailures": failures,
        "suspectedRotation": failures >= TOTP_ROTATION_CONFIRM_THRESHOLD,
        "secondsSinceFirstFailure": None if firstAt is None else time.monotonic() - firstAt,
    }


def _parseTotpSecretOverride(raw: str):
    """A "<version>:<b1,b2,...>" override string as (version, bytearray), or a
    ValueError when it isn't one. Strict on purpose - a half-understood value
    would fail later, at Spotify, as an unexplained login failure."""
    version, separator, byteText = raw.partition(":")
    if not separator:
        raise ValueError('expected "<version>:<comma-separated bytes>"')
    values = [chunk.strip() for chunk in byteText.split(",") if chunk.strip()]
    if not values:
        raise ValueError("no secret bytes given")
    secret = bytearray()
    for value in values:
        number = int(value)   #< ValueError for anything non-numeric
        if not 0 <= number <= TOTP_SECRET_BYTE_MAX:
            raise ValueError(f"byte {number} out of range 0-{TOTP_SECRET_BYTE_MAX}")
        secret.append(number)
    return int(version.strip()), secret


def _resolveTotpSecret():
    """The (version, secret) to authenticate with, in precedence order:

      1. the environment override, when it parses - a human setting it is a
         decision, and it must beat anything derived automatically;
      2. a secret adopted by recovery from Spotify's own web player;
      3. the pinned constants - the normal path.

    A malformed override is reported and then IGNORED rather than raised. The
    override exists to rescue an instance during a rotation; letting a typo in
    it take login offline would invert the point of having it."""
    raw = os.environ.get(TOTP_SECRET_ENV_VAR, "").strip()
    if raw:
        try:
            return _parseTotpSecretOverride(raw)
        except ValueError as e:
            logger.error(
                "Ignoring malformed %s (%s); falling back to the secret pinned in "
                "Database/patches.py (version %s).",
                TOTP_SECRET_ENV_VAR, e, SPOTIFY_TOTP_SECRET_VERSION)

    with _totpAuthLock:
        adopted = _totpAdoptedSecret
    if adopted is not None:
        return adopted[0], bytearray(adopted[1])

    # A fresh bytearray per call: generate_totp() transforms these bytes, and a
    # shared mutable would let one caller's mutation corrupt every later login.
    return SPOTIFY_TOTP_SECRET_VERSION, bytearray(SPOTIFY_TOTP_SECRET_BYTES)


def patch_totp_secret() -> bool:
    """Replace spotapi's mirror fetch with the pinned secret, and make a token
    failure name it (see the block comment above)."""
    try:
        import spotapi.client
    except (ModuleNotFoundError, ImportError):
        return False

    spotapi.client.get_latest_totp_secret = _resolveTotpSecret

    # Idempotent on purpose: this runs at import AND is re-applied deliberately
    # by tests whose import order may have missed it (see setUpModule in
    # tests/test_patches.py). Without the guard the second application wraps
    # the first, so one failed request would count twice and trip the rotation
    # threshold early. The other patches in this module get this for free by
    # capturing their originals at module scope.
    if getattr(spotapi.client.BaseClient._get_auth_vars, "_totpTracked", False):
        return True

    original_get_auth_vars = spotapi.client.BaseClient._get_auth_vars

    def patched_get_auth_vars(self, *args, **kwargs):
        # Whether the call below will actually ASK Spotify for a token, decided
        # before it runs. spotapi's _get_auth_vars is a no-op when both of these
        # are already set - no request, returns None - and counting that as
        # proof the pinned secret still works let a cached token clear the
        # rotation streak. The app hits the no-op on a hot path
        # (player_status_reconnect keeps a token that is not near expiry, then
        # get_session() ends here), so during a REAL rotation every listener
        # reconnect reset the count while the genuine failures accrued, and the
        # confirm threshold could go unreached until the last cached token
        # expired - delaying /admin's flag and the auto-recovery behind it.
        #
        # This mirrors spotapi's own guard (client.py: `if self.access_token is
        # _Undefined or self.client_id is _Undefined`). It is a deliberate
        # coupling to an internal, so it is pinned by a test that drives the
        # no-op through the real installed spotapi rather than a stand-in: a
        # version bump that moves the condition breaks that test rather than
        # the rotation detector.
        wouldFetch = (getattr(self, "access_token", _Undefined) is _Undefined
                      or getattr(self, "client_id", _Undefined) is _Undefined)
        try:
            result = original_get_auth_vars(self, *args, **kwargs)
        except spotapi.exceptions.BaseClientError:
            failures = recordTotpAuthFailure()
            # The message spotapi raises names neither TOTP nor these
            # constants, so the pin would be undiscoverable from the symptom it
            # produces. Logged at the confirmation threshold and then every
            # further multiple of it: an instance in this state retries
            # constantly, and one line per attempt would bury the incident it
            # is reporting.
            if failures % TOTP_ROTATION_CONFIRM_THRESHOLD == 0:
                logger.error(
                    "Spotify has refused the session token request %d times in a row. "
                    "That is instance-wide, so the most likely cause is a rotated TOTP "
                    "secret: this build pins version %s (SPOTIFY_TOTP_SECRET_VERSION in "
                    'Database/patches.py). Set %s="<version>:<comma-separated bytes>" and '
                    "restart to apply a new one without waiting for a release. See the "
                    "Worker Health panel on /admin.",
                    failures, SPOTIFY_TOTP_SECRET_VERSION, TOTP_SECRET_ENV_VAR)
                # Confirmed streak: go read the current secret off Spotify's own
                # web player - on a BACKGROUND thread, because this caller can
                # be a Flask request thread mid-login. Rate-limited and never
                # raising (see attemptTotpRecovery); an adopted secret takes
                # effect on the next attempt rather than retrying this one,
                # which keeps the error semantics of this call unchanged.
                _startTotpRecoveryInBackground()
            raise
        if wouldFetch:
            recordTotpAuthSuccess()
        return result

    patched_get_auth_vars._totpTracked = True
    spotapi.client.BaseClient._get_auth_vars = patched_get_auth_vars
    return True


# Public web-player assets can be shared process-wide. Cookies and tokens cannot:
# spotapi builds a new BaseClient for every lookup on a borrowed TLS transport.
SPOTAPI_HASH_CACHE_TTL_SECONDS = 6 * 3600
_spotapiBundle = None
_spotapiHashByName = {}
_spotapiJsPack = None
_spotapiBundleFetchedAt = None
_spotapiBundleGeneration = 0
_spotapiBundleLock = threading.Lock()
_spotapiHashUse = threading.local()
_spotapiAuthByClient = {}
_spotapiAuthLock = threading.Lock()
_SPOTAPI_AUTH_FIELDS = (
    "access_token", "client_id", "access_token_expires_at_ms",
    "client_token", "client_version", "device_id",
)


def _clearSpotapiBundle():
    """Clear the entire public generation while holding _spotapiBundleLock."""
    global _spotapiBundle, _spotapiJsPack, _spotapiBundleFetchedAt, _spotapiBundleGeneration
    _spotapiBundle = _spotapiJsPack = _spotapiBundleFetchedAt = None
    _spotapiHashByName.clear()
    _spotapiBundleGeneration += 1


def invalidateSpotapiHashCache(generation=None):
    """Evict only the generation rejected by Spotify, or unconditionally for reset."""
    with _spotapiBundleLock:
        if generation is not None and generation != _spotapiBundleGeneration:
            return False
        _clearSpotapiBundle()
        return True


def beginSpotapiHashAttempt():
    """Do not attribute a failure before part_hash to a previous lookup's bundle."""
    _spotapiHashUse.generation = None


def invalidateUsedSpotapiHash():
    """The thread's actual part_hash generation, not a racy pre-request snapshot."""
    generation = getattr(_spotapiHashUse, "generation", None)
    return generation is not None and invalidateSpotapiHashCache(generation)


def _hydrateSpotapiAuth(base):
    # Only a newly constructed, empty BaseClient needs hydration. A direct
    # get_session (e.g. websocket refresh) may already have supplied newer state.
    if any(getattr(base, field, _Undefined) is not _Undefined
           for field in ("access_token", "client_id", "client_token")):
        return
    with _spotapiAuthLock:
        entry = _spotapiAuthByClient.get(id(base.client))
        if entry is not None and entry[0]() is base.client:
            for field, value in entry[1].items():
                setattr(base, field, value)


def _publishSpotapiAuth(base):
    key = id(base.client)

    def discard(ref):
        with _spotapiAuthLock:
            entry = _spotapiAuthByClient.get(key)
            if entry is not None and entry[0] is ref:
                _spotapiAuthByClient.pop(key, None)

    state = {field: getattr(base, field) for field in _SPOTAPI_AUTH_FIELDS}
    with _spotapiAuthLock:
        _spotapiAuthByClient[key] = (weakref.ref(base.client, discard), state)


def _evictSpotapiAuth(base):
    with _spotapiAuthLock:
        _spotapiAuthByClient.pop(id(base.client), None)


def _loadSpotapiBundle(base):
    """Load under the bundle lock, publishing only the fully assembled string."""
    global _spotapiBundle, _spotapiJsPack, _spotapiBundleFetchedAt, _spotapiBundleGeneration
    import spotapi.client as upstream

    if (_spotapiBundle is None or _spotapiBundleFetchedAt is None
            or time.monotonic() - _spotapiBundleFetchedAt >= SPOTAPI_HASH_CACHE_TTL_SECONDS):
        _clearSpotapiBundle()
        _hydrateSpotapiAuth(base)
        # Rediscover the pack URL on each refresh; base.js_pack may itself be old.
        base.get_session()
        if not base.js_pack or base.js_pack is _Undefined:
            raise ValueError("Could not get playlist hashes")
        response = base.client.get(str(base.js_pack))
        if response.fail:
            raise spotapi.exceptions.BaseClientError("Could not get general hashes", error=response.error.string)
        chunks = [response.response]
        strMapping, hashMapping = upstream.extract_mappings(str(chunks[0]))
        for chunk in upstream.combine_chunks(hashMapping, strMapping):
            response = base.client.get(f"https://open.spotifycdn.com/cdn/build/web-player/{chunk}")
            if response.fail:
                raise spotapi.exceptions.BaseClientError("Could not get general hashes", error=response.error.string)
            chunks.append(response.response)
        _spotapiBundle = "".join(chunks)
        _spotapiJsPack = str(base.js_pack)
        _spotapiBundleFetchedAt = time.monotonic()
        _spotapiBundleGeneration += 1
    base.raw_hashes = _spotapiBundle
    base.js_pack = _spotapiJsPack


def _extractSpotapiOperationHash(bundle, name):
    try:
        return bundle.split(f'"{name}","query","', 1)[1].split('"', 1)[0]
    except IndexError:
        return bundle.split(f'"{name}","mutation","', 1)[1].split('"', 1)[0]


def patch_spotapi_cache():
    """Reuse public hashes and per-transport auth without changing spotapi's expiry rules."""
    import spotapi.client as upstream
    baseClass = upstream.BaseClient

    if not getattr(baseClass.get_sha256_hash, "_spotapiCached", False):
        def getHashes(self):
            with _spotapiBundleLock:
                _loadSpotapiBundle(self)
        getHashes._spotapiCached = True
        baseClass.get_sha256_hash = getHashes

    if not getattr(baseClass.part_hash, "_spotapiCached", False):
        def partHash(self, name):
            with _spotapiBundleLock:
                # Check TTL even for an operation already in the map.
                _loadSpotapiBundle(self)
                if name not in _spotapiHashByName:
                    _spotapiHashByName[name] = _extractSpotapiOperationHash(_spotapiBundle, name)
                _spotapiHashUse.generation = _spotapiBundleGeneration
                return _spotapiHashByName[name]
        partHash._spotapiCached = True
        baseClass.part_hash = partHash

    if not getattr(baseClass._auth_rule, "_spotapiCached", False):
        originalAuth = baseClass._auth_rule

        def authRule(self, kwargs):
            _hydrateSpotapiAuth(self)
            try:
                result = originalAuth(self, kwargs)
            except Exception:
                _evictSpotapiAuth(self)
                raise
            _publishSpotapiAuth(self)
            return result
        authRule._spotapiCached = True
        baseClass._auth_rule = authRule

    if not getattr(baseClass._handle_auth_failure, "_spotapiCached", False):
        originalFailure = baseClass._handle_auth_failure

        def authFailure(self, response):
            try:
                headers = {key.lower(): value for key, value in response.raw.headers.items()}
            except (AttributeError, TypeError):
                headers = {}
            refresh = (response.status_code == 401 or (response.status_code == 400
                       and headers.get("client-token-error") == "INVALID_CLIENTTOKEN"))
            if refresh:
                _evictSpotapiAuth(self)
            result = originalFailure(self, response)
            if refresh and result:
                # TLS._send authenticates again immediately after this callback.
                _publishSpotapiAuth(self)
            return result
        authFailure._spotapiCached = True
        baseClass._handle_auth_failure = authFailure
    return True


patch_spotapi_user()
patch_totp_secret()
patch_spotapi_cache()
