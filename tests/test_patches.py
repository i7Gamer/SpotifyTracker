import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch
import json
import signal
import threading
import websockets.sync.client
import spotapi.exceptions
import spotapi.status
import spotapi.websocket

import Database.rate_limit as rateLimitModule


def setUpModule():
    # Database.patches applies its spotapi patches once, at whatever moment
    # Database (the package) first gets imported. If that happened while some
    # other test module's mock was still in sys.modules, the real spotapi
    # would never get patched for the rest of the process. Re-applying here
    # makes this module correct regardless of import order.
    from Database.patches import patch_spotapi_user, patch_totp_secret, patch_spotapi_cache
    patch_spotapi_user()
    patch_totp_secret()
    patch_spotapi_cache()


class TestPatches(unittest.TestCase):
    """Verify that monkey-patches are correctly applied to websockets and spotapi."""

    def test_websockets_connect_default_arguments(self):
        """websockets.sync.client.connect should default ping_interval/ping_timeout to None."""
        mock_connect = MagicMock()
        # Temporarily swap the original connect with our mock
        from Database.patches import original_connect
        try:
            with patch("Database.patches.original_connect", mock_connect):
                # When calling websockets.sync.client.connect with some arguments
                websockets.sync.client.connect("wss://example.com", user_agent_header="test-ua")
                
                # Check that original_connect was called with defaults overridden to None
                mock_connect.assert_called_once_with(
                    "wss://example.com",
                    user_agent_header="test-ua",
                    ping_interval=None,
                    ping_timeout=None
                )
        finally:
            pass

    def test_websocket_streamer_init_restores_previous_sigint_handler(self):
        """WebsocketStreamer.__init__ must not leave spotapi's own SIGINT handler
        installed. Even if the underlying init hijacks SIGINT (as spotapi's real
        implementation does, to call ws.close(); exit(0)), whatever handler was
        registered beforehand (e.g. Python/Werkzeug's default) must win, so Ctrl+C
        doesn't get hijacked mid-request by a background listener thread."""
        def fakeOriginalInit(self, *args, **kwargs):
            signal.signal(signal.SIGINT, lambda signum, frame: None)

        sentinelHandler = lambda signum, frame: None
        originalHandler = signal.signal(signal.SIGINT, sentinelHandler)
        try:
            instance = spotapi.websocket.WebsocketStreamer.__new__(spotapi.websocket.WebsocketStreamer)
            with patch("Database.patches.original_websocket_streamer_init", fakeOriginalInit):
                spotapi.websocket.WebsocketStreamer.__init__(instance, MagicMock())
            self.assertIs(signal.getsignal(signal.SIGINT), sentinelHandler)
        finally:
            signal.signal(signal.SIGINT, originalHandler)

    def test_player_status_has_reconnect_method(self):
        """PlayerStatus class must have reconnect method injected."""
        self.assertTrue(hasattr(spotapi.status.PlayerStatus, "reconnect"))
        self.assertTrue(callable(spotapi.status.PlayerStatus.reconnect))

    @patch("websockets.sync.client.connect")
    def test_player_status_reconnect_flow(self, mock_ws_connect):
        """reconnect() must call close on old socket, renew sessions, connect, get init packet, and register."""
        # Create a mock PlayerStatus instance
        self.assertTrue(hasattr(spotapi.status.PlayerStatus, "reconnect"))

        # We will mock the required methods/attributes on PlayerStatus
        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws

        instance = MagicMock(spec=spotapi.status.PlayerStatus)
        instance._deliberate_close = False  #< a bare MagicMock's auto-attribute is truthy = deliberate close
        instance.ws = mock_ws
        instance.base = MagicMock()
        # Concrete token state (a bare MagicMock can't be compared against the
        # expiry clock): unknown expiry means the token is refreshed, and
        # get_session is what produces the new one.
        instance.base.access_token = "expired-token"
        instance.base.access_token_expires_at_ms = 0
        def renewToken():
            instance.base.access_token = "fresh-token"
        instance.base.get_session.side_effect = renewToken

        # The dealer's first frame carries the new connection ID
        mock_ws.recv.return_value = '{"headers": {"Spotify-Connection-Id": "new-conn-id"}}'

        # Thread status mock
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = False
        instance.keep_alive_thread = mock_thread

        # Call the reconnect function bound to the instance
        spotapi.status.PlayerStatus.reconnect(instance)

        # Verify old websocket is closed
        mock_ws.close.assert_called_once()

        # Verify sessions and tokens are renewed
        instance.base.get_session.assert_called_once()
        instance.base.get_client_token.assert_called_once()

        # Verify we connect to the new websocket URI
        mock_ws_connect.assert_called_once()

        # Verify connection_id was updated
        self.assertEqual(instance.connection_id, "new-conn-id")

        # Verify device registration and connection
        instance.register_device.assert_called_once()
        instance.connect_device.assert_called_once()

        # Verify keep alive thread was restarted
        mock_thread.is_alive.assert_called_once()

    def test_patched_user_get_user_info_behavior(self):
        import spotapi.user
        from spotapi.exceptions import UserError

        # Create a mock login and mock client
        mock_login = MagicMock()
        mock_login.logged_in = True

        # Create user instance
        user_inst = spotapi.user.User(mock_login)

        # Define mock responses
        mock_resp_success_json = MagicMock()
        mock_resp_success_json.status_code = 200
        mock_resp_success_json.fail = False
        mock_resp_success_json.response = {"id": "test_user", "email": "test@example.com"}
        mock_resp_success_json.raw.headers = {"X-Csrf-Token": "test_csrf"}

        mock_login.client.get.return_value = mock_resp_success_json

        # Verify success case
        res = user_inst.get_user_info()
        self.assertEqual(res["id"], "test_user")
        self.assertEqual(user_inst.csrf_token, "test_csrf")

        # Verify non-JSON/non-Mapping success response logs warning and raises UserError
        mock_resp_non_json = MagicMock()
        mock_resp_non_json.status_code = 200
        mock_resp_non_json.fail = False
        mock_resp_non_json.response = "Invalid HTML / Cloudflare screen"
        mock_resp_non_json.raw.headers = {}
        mock_login.client.get.return_value = mock_resp_non_json

        with self.assertLogs("Database.patches", level="WARNING") as log_capture:
            with self.assertRaises(UserError) as err_ctx:
                user_inst.get_user_info()

            # Check log and exception message
            self.assertIn("non-Mapping response", log_capture.output[0])
            self.assertIn("Invalid JSON", str(err_ctx.exception))
            self.assertIn("Status: 200", str(err_ctx.exception))
            self.assertIn("Type: str", str(err_ctx.exception))
            self.assertIn("Response: Invalid HTML", str(err_ctx.exception))

        # Verify failed request logs warning and raises UserError
        mock_resp_fail = MagicMock()
        mock_resp_fail.status_code = 429
        mock_resp_fail.fail = True
        mock_resp_fail.error.string = "Too Many Requests"
        mock_resp_fail.response = "Rate limit hit"
        mock_resp_fail.raw.headers = {"Retry-After": "60"}
        mock_login.client.get.return_value = mock_resp_fail

        with self.assertLogs("Database.patches", level="WARNING") as log_capture:
            with self.assertRaises(UserError) as err_ctx:
                user_inst.get_user_info()

            self.assertIn("HTTP request failed", log_capture.output[0])
            self.assertEqual(err_ctx.exception.error, "Too Many Requests")

    def test_patched_user_get_plan_info_behavior(self):
        import spotapi.user
        from spotapi.exceptions import UserError

        mock_login = MagicMock()
        mock_login.logged_in = True
        user_inst = spotapi.user.User(mock_login)

        # Verify success case
        mock_resp_success = MagicMock()
        mock_resp_success.status_code = 200
        mock_resp_success.fail = False
        mock_resp_success.response = {"plan": "premium"}
        mock_login.client.get.return_value = mock_resp_success

        res = user_inst.get_plan_info()
        self.assertEqual(res["plan"], "premium")

        # Verify non-JSON/non-Mapping success response
        mock_resp_non_json = MagicMock()
        mock_resp_non_json.status_code = 200
        mock_resp_non_json.fail = False
        mock_resp_non_json.response = "Plan text error"
        mock_resp_non_json.raw.headers = {}
        mock_login.client.get.return_value = mock_resp_non_json

        with self.assertLogs("Database.patches", level="WARNING") as log_capture:
            with self.assertRaises(UserError) as err_ctx:
                user_inst.get_plan_info()

            self.assertIn("non-Mapping response", log_capture.output[0])
            self.assertIn("Invalid JSON", str(err_ctx.exception))

    def test_player_status_renew_state_logs_error_detail_attribute(self):
        """spotapi's ParentException keeps the HTTP detail in .error, not in
        str(e) - the renew_state warning must surface it."""
        from spotapi.exceptions import WebSocketError
        from spotapi.status import PlayerStatus

        exc = WebSocketError("Could not connect device", error="429: rate limited")

        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device", side_effect=exc):
            lps = PlayerStatus(MagicMock())
            with self.assertLogs("Database.patches", level="WARNING") as cm:
                lps.renew_state()

        self.assertIsNone(lps._state)
        self.assertTrue(any("429: rate limited" in message for message in cm.output))

    def test_player_status_renew_state_handles_missing_keys_gracefully(self):
        """PlayerStatus.renew_state should not raise KeyError if connect_device returns a dict without devices or player_state."""
        from spotapi.status import PlayerStatus
        
        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device") as mock_connect:
            
            # Case 1: returns dict without player_state/devices
            mock_connect.return_value = {"something": "else"}
            lps = PlayerStatus(MagicMock())
            lps.renew_state()
            self.assertIsNone(lps._state)
            self.assertIsNone(lps._devices)
            
            # Case 2: returns None
            mock_connect.return_value = None
            lps = PlayerStatus(MagicMock())
            lps.renew_state()
            self.assertIsNone(lps._state)
            self.assertIsNone(lps._devices)
    def test_player_status_renew_state_stamps_success_even_without_player_state(self):
        """A successful connect_device PUT whose cluster carries no
        player_state is an account with no live Connect session, not a
        failure - the stamp is how the poll loop and the stale check tell
        those apart (both raise/read the same 'Could not get player state'
        ValueError otherwise)."""
        from spotapi.status import PlayerStatus

        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device") as mock_connect:
            mock_connect.return_value = {"devices": []}
            lps = PlayerStatus(MagicMock())
            self.assertIsNone(getattr(lps, "stateRenewalSucceededAt", None))
            with patch("Database.patches.time.monotonic", return_value=1234.5):
                lps.renew_state()

        self.assertEqual(1234.5, lps.stateRenewalSucceededAt)
        self.assertIsNone(lps._state)

    def test_player_status_renew_state_does_not_stamp_failures(self):
        """A renewal that raised, or answered with something other than a
        cluster dict, proved nothing - an ageing stamp is exactly how a
        genuinely failing tick becomes visible again."""
        from spotapi.exceptions import WebSocketError
        from spotapi.status import PlayerStatus

        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device") as mock_connect:
            lps = PlayerStatus(MagicMock())
            mock_connect.side_effect = WebSocketError("boom")
            with self.assertLogs("Database.patches", level="WARNING"):
                lps.renew_state()
            self.assertIsNone(getattr(lps, "stateRenewalSucceededAt", None))

            mock_connect.side_effect = None
            mock_connect.return_value = None  #< non-dict reply is not a success either
            lps.renew_state()
            self.assertIsNone(getattr(lps, "stateRenewalSucceededAt", None))

    def test_player_status_renew_state_deep_copies_player_state_to_prevent_mutation(self):
        """Regression: spotapi's Track.from_dict() mutates its input dict in-place,
        replacing metadata dict with a Metadata dataclass:
            data["metadata"] = Metadata.from_dict(metadata)
        The patched state property passes a deep copy of _state to
        PlayerState.from_dict, so _state itself is never mutated. Without this
        patch, _state["track"]["metadata"] becomes a Metadata object after the
        property is accessed, and a subsequent getConnectPlayerState() read then
        calls .get("title") on a Metadata object -> AttributeError."""
        from spotapi.status import PlayerStatus

        raw_track = {"uri": "spotify:track:abc", "uid": "u1",
                     "metadata": {"title": "Test", "artist_name": "Artist"}}
        raw_state = {"is_playing": True, "track": raw_track,
                     "timestamp": "0", "position_as_of_timestamp": "0", "duration": "0",
                     "is_paused": False}
        device_dump = {"player_state": raw_state, "devices": []}

        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device", return_value=device_dump):
            lps = PlayerStatus(MagicMock())
            lps.renew_state()

        # Access the patched state property - this calls PlayerState.from_dict
        # internally, which would mutate _state without our fix.
        with patch.object(lps, "renew_state"):  # skip renew inside the property
            lps.state  # exercises the patched property

        # _state["track"]["metadata"] must still be a plain dict.
        stored_meta = lps._state["track"]["metadata"]
        self.assertIsInstance(stored_meta, dict,
            f"_state was mutated by state property: metadata is "
            f"{type(stored_meta).__name__}, expected dict")
        self.assertEqual(stored_meta.get("title"), "Test")
        self.assertEqual(stored_meta.get("artist_name"), "Artist")


ONE_HOUR_MS = 60 * 60 * 1000


class _FakeReconnectBase:
    """Stands in for spotapi's BaseClient, reproducing the one behavior under
    test: _get_auth_vars only fetches a token when none is set (spotapi's real
    guard), so a reconnect that doesn't clear a stale token gets the stale
    token straight back."""

    def __init__(self, token="stale-token", expiresInMs=None):
        import time
        self.access_token = token
        self.access_token_expires_at_ms = (
            0 if expiresInMs is None else time.time() * 1000 + expiresInMs)
        self.tokenWhenSessionRenewed = "get_session-never-called"
        self.renewalError = None

    def get_session(self):
        from spotapi.types.alias import _Undefined
        self.tokenWhenSessionRenewed = self.access_token
        if self.renewalError is not None:
            raise self.renewalError
        if self.access_token is _Undefined:
            self.access_token = "fresh-token"

    def get_client_token(self):
        pass


#< the dealer's first frame after a successful handshake
_INIT_PACKET_JSON = '{"headers": {"Spotify-Connection-Id": "conn-id"}}'


def _reconnectableInstance(base):
    """A stand-in PlayerStatus carrying only what player_status_reconnect touches."""
    import types
    instance = types.SimpleNamespace()
    instance.ws = MagicMock()
    instance.base = base
    instance.rlock = threading.Lock()
    instance.register_device = MagicMock()
    instance.connect_device = MagicMock()
    aliveThread = MagicMock()
    aliveThread.is_alive.return_value = True
    instance.keep_alive_thread = aliveThread
    return instance


class TestReconnectRefreshesTheAccessToken(unittest.TestCase):
    """The dealer handshake authenticates ONLY via the access_token in its URI -
    no _auth_rule header injection, no 401-retry hook, no second chance.
    spotapi's _get_auth_vars refuses to fetch while ANY token is still set,
    however expired, so a reconnect that does not clear a stale token resends
    the token Spotify just rejected - every attempt, forever. That is the
    2026-08-01 storm: 14 hours of "server rejected WebSocket connection:
    HTTP 401" at several attempts a minute."""

    def _reconnect(self, base):
        from Database.patches import player_status_reconnect
        instance = _reconnectableInstance(base)
        with patch("websockets.sync.client.connect") as wsConnect:
            wsConnect.return_value.recv.return_value = _INIT_PACKET_JSON
            player_status_reconnect(instance)
        wsConnect.assert_called_once()
        return wsConnect.call_args.args[0]

    def test_an_expired_token_is_replaced_before_connecting(self):
        from spotapi.types.alias import _Undefined

        base = _FakeReconnectBase(expiresInMs=-1000)
        uri = self._reconnect(base)

        #< cleared BEFORE the renewal ran, or _get_auth_vars would have no-opped
        self.assertIs(base.tokenWhenSessionRenewed, _Undefined)
        self.assertIn("access_token=fresh-token", uri)

    def test_a_token_of_unknown_expiry_is_treated_as_stale(self):
        """expires_at can be 0 when Spotify's token reply omitted the timestamp -
        a wrong guess here costs one token fetch, guessing the other way costs
        an unrecoverable 401 loop."""
        base = _FakeReconnectBase(expiresInMs=None)

        self.assertIn("access_token=fresh-token", self._reconnect(base))

    def test_a_token_about_to_expire_is_refreshed(self):
        """Same skew _auth_rule applies to HTTP requests: a token with seconds
        left would pass the handshake and die on the first re-handshake."""
        from Database.patches import WS_ACCESS_TOKEN_REFRESH_SKEW_MS

        base = _FakeReconnectBase(expiresInMs=WS_ACCESS_TOKEN_REFRESH_SKEW_MS / 2)

        self.assertIn("access_token=fresh-token", self._reconnect(base))

    def test_a_still_valid_token_is_reused_not_refetched(self):
        """No needless token traffic: a mid-hour network blip reconnects with
        the token it already has."""
        base = _FakeReconnectBase(expiresInMs=ONE_HOUR_MS)

        uri = self._reconnect(base)

        self.assertEqual(base.tokenWhenSessionRenewed, "stale-token")
        self.assertIn("access_token=stale-token", uri)

    def test_a_failed_renewal_with_no_token_raises_instead_of_connecting(self):
        """With the stale token cleared and no replacement, connecting would
        send a literal "_Undefined" bearer token - a guaranteed 401 that the
        callers' bounded retry paths handle better as the real error."""
        from Database.patches import player_status_reconnect

        base = _FakeReconnectBase(expiresInMs=-1000)
        base.renewalError = RuntimeError("open.spotify.com unreachable")
        instance = _reconnectableInstance(base)

        with patch("websockets.sync.client.connect") as wsConnect:
            with self.assertRaises(RuntimeError):
                player_status_reconnect(instance)

        wsConnect.assert_not_called()

    def test_a_failed_renewal_with_a_valid_token_still_connects(self):
        """Pre-existing behavior, kept: a transient renewal failure while the
        token is still good must not block a reconnect that can succeed."""
        from Database.patches import player_status_reconnect

        base = _FakeReconnectBase(expiresInMs=ONE_HOUR_MS)
        base.renewalError = RuntimeError("blip")
        instance = _reconnectableInstance(base)

        with patch("websockets.sync.client.connect") as wsConnect:
            wsConnect.return_value.recv.return_value = _INIT_PACKET_JSON
            with self.assertLogs("Database.patches", level="WARNING"):
                player_status_reconnect(instance)

        wsConnect.assert_called_once()
        self.assertIn("access_token=stale-token", wsConnect.call_args.args[0])

    def test_a_deliberately_closed_streamer_is_not_reconnected(self):
        """stop() closed this socket on purpose; resurrecting it re-registers a
        ghost device on a session that is being torn down."""
        from Database.patches import player_status_reconnect

        base = _FakeReconnectBase(expiresInMs=ONE_HOUR_MS)
        instance = _reconnectableInstance(base)
        instance._deliberate_close = True

        with patch("websockets.sync.client.connect") as wsConnect:
            player_status_reconnect(instance)

        wsConnect.assert_not_called()
        instance.ws.close.assert_not_called()
        self.assertEqual(base.tokenWhenSessionRenewed, "get_session-never-called")


class TestReconnectInitPacketHandling(unittest.TestCase):
    """The init packet must be read from a socket the rest of the process
    cannot see yet: self.ws is what get_packet re-reads under rlock, so
    publishing before the read let the push loop consume the init frame -
    leaving the reconnecting thread blocked forever in an untimed recv(),
    the device never re-registered, and the listener silently degraded to
    polling. The timeout also bounds a dealer that accepts the socket but
    never speaks."""

    def _instance(self):
        return _reconnectableInstance(_FakeReconnectBase(expiresInMs=ONE_HOUR_MS))

    def test_the_init_packet_read_is_bounded(self):
        from Database.patches import player_status_reconnect, WS_INIT_PACKET_TIMEOUT_SECONDS

        instance = self._instance()

        with patch("websockets.sync.client.connect") as wsConnect:
            wsConnect.return_value.recv.return_value = _INIT_PACKET_JSON
            player_status_reconnect(instance)

        wsConnect.return_value.recv.assert_called_once_with(
            timeout=WS_INIT_PACKET_TIMEOUT_SECONDS)

    def test_the_socket_is_published_only_after_the_init_packet_is_read(self):
        from Database.patches import player_status_reconnect

        instance = self._instance()
        oldWs = instance.ws
        publishedAtRecvTime = []

        with patch("websockets.sync.client.connect") as wsConnect:
            newWs = wsConnect.return_value

            def recordingRecv(timeout=None):
                publishedAtRecvTime.append(instance.ws)
                return _INIT_PACKET_JSON
            newWs.recv.side_effect = recordingRecv

            player_status_reconnect(instance)

        self.assertEqual(publishedAtRecvTime, [oldWs])  #< still the old socket at read time
        self.assertIs(instance.ws, newWs)
        self.assertEqual(instance.connection_id, "conn-id")

    def test_a_bad_init_packet_closes_the_new_socket_and_raises(self):
        from Database.patches import player_status_reconnect

        instance = self._instance()
        oldWs = instance.ws

        with patch("websockets.sync.client.connect") as wsConnect:
            newWs = wsConnect.return_value
            newWs.recv.return_value = '{"no": "headers"}'
            with self.assertRaises(ValueError):
                player_status_reconnect(instance)

        newWs.close.assert_called_once()
        self.assertIs(instance.ws, oldWs)  #< the dead-but-known socket, never the broken one
        instance.register_device.assert_not_called()


class TestReconnectYieldsToAStopThatLandedMidHandshake(unittest.TestCase):
    """Both _deliberate_close checks sit BEFORE the network - the entry guard
    and the one under the reconnect lock - and the body then spends 1-3s in
    get_session, get_client_token, the TLS/WS handshake and the init-packet
    read before publishing.

    stop() runs on another thread and closes whatever manager.ws is at THAT
    instant, which is the OLD socket. So a reconnect finishing afterwards
    stranded the new one: nothing else ever closes a streamer's socket - the
    fork's disconnect() has no caller, Spotify.close() only closes the curl
    TLS client, keep_alive exits on the flag without closing, and signalStop
    has already dropped the atexit hook - leaving an open dealer socket, its
    recv_events thread, and a live hobs_<device_id> registration outliving the
    listener that owned them. That is exactly what the entry guard's comment
    says it exists to prevent ("Resurrecting it would register a ghost device
    on a session that is being torn down")."""

    def _reconnect(self, stopDuringInitPacket):
        from Database.patches import player_status_reconnect

        instance = _reconnectableInstance(_FakeReconnectBase(expiresInMs=ONE_HOUR_MS))
        oldWs = instance.ws
        with patch("websockets.sync.client.connect") as wsConnect:
            newWs = wsConnect.return_value

            def recv(timeout=None):
                if stopDuringInitPacket:
                    #< the widest realistic window, and the one the rebuild
                    #  path actually races: onStale calls listener.stop()
                    #  while the manager thread is reconnecting
                    instance._deliberate_close = True
                return _INIT_PACKET_JSON
            newWs.recv.side_effect = recv

            player_status_reconnect(instance)
        return instance, oldWs, newWs

    def test_a_stop_mid_handshake_closes_the_new_socket_instead_of_publishing(self):
        instance, oldWs, newWs = self._reconnect(stopDuringInitPacket=True)

        newWs.close.assert_called_once_with()
        self.assertIs(instance.ws, oldWs,
                      "publishing here strands a socket nothing will ever close")
        #< the ghost device the entry guard names
        instance.register_device.assert_not_called()
        instance.connect_device.assert_not_called()

    def test_a_reconnect_nobody_stopped_still_publishes_and_registers(self):
        """Negative control: the guard must not swallow the ordinary path."""
        instance, _oldWs, newWs = self._reconnect(stopDuringInitPacket=False)

        self.assertIs(instance.ws, newWs)
        newWs.close.assert_not_called()
        instance.register_device.assert_called_once_with()
        instance.connect_device.assert_called_once_with()


class _LockStub:
    """Stands in for the per-streamer reconnect lock: entering it runs a
    scripted side effect - the deterministic version of "another thread held
    the lock and changed the world while we waited"."""

    def __init__(self, onEnter):
        self.onEnter = onEnter

    def __enter__(self):
        self.onEnter()

    def __exit__(self, *args):
        return False


class TestReconnectSerialization(unittest.TestCase):
    """keep_alive and the push loop's receive path watch the same socket, so
    one drop can send both into reconnect. The loser must notice the winner's
    work and stand down instead of stacking a second dealer connection (and a
    second keep-alive thread) on top of it."""

    def test_a_reconnect_that_lost_the_race_is_skipped(self):
        from Database.patches import player_status_reconnect

        base = _FakeReconnectBase(expiresInMs=ONE_HOUR_MS)
        instance = _reconnectableInstance(base)
        instance._reconnectSerializeLock = _LockStub(
            lambda: setattr(instance, "_reconnectGeneration",
                            getattr(instance, "_reconnectGeneration", 0) + 1))

        with patch("websockets.sync.client.connect") as wsConnect:
            with self.assertLogs("Database.patches", level="INFO"):
                player_status_reconnect(instance)

        wsConnect.assert_not_called()
        self.assertEqual(base.tokenWhenSessionRenewed, "get_session-never-called")

    def test_a_stop_that_landed_while_waiting_for_the_lock_wins(self):
        from Database.patches import player_status_reconnect

        base = _FakeReconnectBase(expiresInMs=ONE_HOUR_MS)
        instance = _reconnectableInstance(base)
        instance._reconnectSerializeLock = _LockStub(
            lambda: setattr(instance, "_deliberate_close", True))

        with patch("websockets.sync.client.connect") as wsConnect:
            player_status_reconnect(instance)

        wsConnect.assert_not_called()

    def test_a_successful_reconnect_bumps_the_generation(self):
        from Database.patches import player_status_reconnect

        instance = _reconnectableInstance(_FakeReconnectBase(expiresInMs=ONE_HOUR_MS))

        with patch("websockets.sync.client.connect") as wsConnect:
            wsConnect.return_value.recv.return_value = _INIT_PACKET_JSON
            player_status_reconnect(instance)

        self.assertEqual(instance._reconnectGeneration, 1)

    def test_a_failed_reconnect_leaves_the_generation_for_the_next_try(self):
        """Only a COMPLETED reconnect may make a waiting thread stand down -
        after a failure the waiter must go ahead and try itself."""
        from Database.patches import player_status_reconnect

        base = _FakeReconnectBase(expiresInMs=-1000)
        base.renewalError = RuntimeError("unreachable")
        instance = _reconnectableInstance(base)

        with patch("websockets.sync.client.connect"):
            with self.assertRaises(RuntimeError):
                player_status_reconnect(instance)

        self.assertEqual(getattr(instance, "_reconnectGeneration", 0), 0)


def _keepAliveInstance(sendEffect=None, hasWs=True):
    """A stand-in streamer carrying only what patched_keep_alive touches."""
    import types
    instance = types.SimpleNamespace()
    instance.rlock = threading.Lock()
    instance._deliberate_close = False
    instance.reconnect = MagicMock()
    if hasWs:
        instance.ws = MagicMock()
        if sendEffect is not None:
            instance.ws.send.side_effect = sendEffect
    else:
        instance.ws = None
    return instance


class TestPatchedKeepAlive(unittest.TestCase):
    """keep_alive is a full replacement, not a wrapper: the fork's original
    catches EVERY exception internally and retries reconnect() forever, so a
    wrapper around it never saw a single failure - and neither spotapi build
    checks _deliberate_close, which left orphaned ping threads reconnecting
    deliberately-closed sockets for the rest of the process's life."""

    def setUp(self):
        # Deterministic on purpose: every exit below is driven by scripted
        # flags and side effects, never by the clock.
        for constant in ("WS_KEEP_ALIVE_PING_INTERVAL_SECONDS",
                         "WS_KEEP_ALIVE_RECONNECT_BACKOFF_SECONDS"):
            patcher = patch(f"Database.patches.{constant}", 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, instance):
        from Database.patches import patched_keep_alive
        patched_keep_alive(instance)

    def test_exits_on_deliberate_close_without_touching_the_socket(self):
        instance = _keepAliveInstance()
        instance._deliberate_close = True

        self._run(instance)

        instance.ws.send.assert_not_called()
        instance.reconnect.assert_not_called()

    def test_a_dropped_socket_reconnects(self):
        import websockets.exceptions

        instance = _keepAliveInstance(
            sendEffect=websockets.exceptions.ConnectionClosedError(None, None))
        instance.reconnect.side_effect = (
            lambda: setattr(instance, "_deliberate_close", True))

        with self.assertLogs("Database.patches", level="WARNING"):
            self._run(instance)

        instance.reconnect.assert_called_once_with()

    def test_a_clean_close_that_was_not_deliberate_reconnects_too(self):
        """Spotify hangs up the dealer cleanly about once an hour. The push->
        poll fallback is one-way per listener, so quietly stopping pings here
        would degrade every listener to polling until its next rebuild."""
        import websockets.exceptions

        instance = _keepAliveInstance(
            sendEffect=websockets.exceptions.ConnectionClosedOK(None, None))
        instance.reconnect.side_effect = (
            lambda: setattr(instance, "_deliberate_close", True))

        with self.assertLogs("Database.patches", level="WARNING"):
            self._run(instance)

        instance.reconnect.assert_called_once_with()

    def test_a_deliberate_close_discovered_at_the_failed_ping_stops_the_loop(self):
        """stop() can flip the flag while this thread is mid-ping - the failed
        send must lead to an exit, not a resurrection."""
        import websockets.exceptions

        instance = _keepAliveInstance()

        def sendOnClosingSocket(payload):
            instance._deliberate_close = True
            raise websockets.exceptions.ConnectionClosedError(None, None)
        instance.ws.send.side_effect = sendOnClosingSocket

        self._run(instance)

        instance.reconnect.assert_not_called()

    def test_a_missing_socket_is_treated_as_a_drop(self):
        instance = _keepAliveInstance(hasWs=False)
        instance.reconnect.side_effect = (
            lambda: setattr(instance, "_deliberate_close", True))

        with self.assertLogs("Database.patches", level="WARNING"):
            self._run(instance)

        instance.reconnect.assert_called_once_with()

    def test_reconnect_failures_are_bounded(self):
        import websockets.exceptions
        from Database.patches import WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES

        instance = _keepAliveInstance(
            sendEffect=websockets.exceptions.ConnectionClosedError(None, None))
        instance.reconnect.side_effect = RuntimeError("Spotify unreachable")

        with self.assertLogs("Database.patches", level="ERROR"):
            self._run(instance)

        self.assertEqual(instance.reconnect.call_count,
                         WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES)

    def test_a_successful_reconnect_resets_the_failure_budget(self):
        """Two separate outages, each one failure short of the ceiling, must
        not add up to a give-up - only CONSECUTIVE failures count."""
        import websockets.exceptions
        from Database.patches import WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES

        dropped = websockets.exceptions.ConnectionClosedError(None, None)
        almostMax = WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES - 1

        instance = _keepAliveInstance()
        sends = iter([dropped, dropped, "stop"])

        def scriptedSend(payload):
            action = next(sends)
            if action == "stop":
                instance._deliberate_close = True
                return
            raise action
        instance.ws.send.side_effect = scriptedSend

        reconnects = iter(
            [RuntimeError("outage 1")] * almostMax + [None]
            + [RuntimeError("outage 2")] * almostMax + [None])

        def scriptedReconnect():
            error = next(reconnects)
            if error is not None:
                raise error
        instance.reconnect.side_effect = scriptedReconnect

        with self.assertLogs("Database.patches", level="WARNING"):
            self._run(instance)

        #< both outages recovered; without the reset the second would have
        #  crossed the ceiling on its first failure and given up
        self.assertEqual(instance.reconnect.call_count, 2 * (almostMax + 1))

    def test_without_a_reconnect_method_the_loop_exits(self):
        """A plain WebsocketStreamer (no injected reconnect) must exit
        gracefully instead of raising AttributeError."""
        import types

        instance = types.SimpleNamespace()
        instance.rlock = threading.Lock()
        instance._deliberate_close = False
        instance.ws = None

        with self.assertLogs("Database.patches", level="WARNING"):
            self._run(instance)   #< must return, not raise

    def test_the_wait_notices_a_deliberate_close_without_sleeping(self):
        import types
        from Database.patches import _sleepInterruptibly

        instance = types.SimpleNamespace(_deliberate_close=True)

        with patch("Database.patches.time.sleep") as mockSleep:
            self.assertFalse(_sleepInterruptibly(instance, 3600))

        mockSleep.assert_not_called()

    def test_an_elapsed_wait_reports_completion(self):
        import types
        from Database.patches import _sleepInterruptibly

        instance = types.SimpleNamespace(_deliberate_close=False)

        self.assertTrue(_sleepInterruptibly(instance, 0))


class TestSupervisorDisabled(unittest.TestCase):
    """The fork's _supervise thread reconnects whenever self.ws is None or
    closed - exactly the state a deliberate disconnect() leaves behind - with
    no stop flag, no attempt ceiling and print() diagnostics. Every reconnect
    this app wants is already owned by a bounded, flag-aware loop (keep_alive,
    the push loop's _reconnectAfterDroppedPacket, the poll loop's escalation),
    so the supervisor thread must do nothing at all."""

    def setUp(self):
        if not hasattr(spotapi.websocket.WebsocketStreamer, "_supervise"):
            self.skipTest("this spotapi build has no _supervise thread")

    def test_the_streamer_supervisor_is_the_no_op(self):
        from Database.patches import patched_supervise
        self.assertIs(spotapi.websocket.WebsocketStreamer._supervise, patched_supervise)

    def test_the_decorated_subclasses_resolve_to_the_no_op_too(self):
        """@enforce froze a wrapped copy of the ORIGINAL _supervise onto each
        decorated subclass at import - the shadow removal must cover it, or the
        supervisor threads real instances start still run the unbounded
        original underneath the patch."""
        from Database.patches import patched_supervise
        self.assertIs(spotapi.status.PlayerStatus._supervise, patched_supervise)
        self.assertIs(spotapi.status.EventManager._supervise, patched_supervise)

    def test_the_no_op_touches_nothing(self):
        from Database.patches import patched_supervise

        instance = MagicMock()

        self.assertIsNone(patched_supervise(instance))
        self.assertEqual(instance.mock_calls, [])


class TestSafeResponseHeaders(unittest.TestCase):
    """The spotapi.User diagnostics log response headers to identify rate
    limiting and Cloudflare blocks. Spotify's responses to those same calls
    carry live session credentials (__Host-sp_csrf_sid, x-csrf-token), so only
    an explicit allowlist may reach the log."""

    #< a realistic failing-response header set: the two credential headers seen
    #  verbatim in Database/Data/app.log, plus the diagnostics worth keeping
    SAMPLE_HEADERS = {
        "Set-Cookie": "__Host-sp_csrf_sid=be483bc22ea1d2dc9b5d0ece1ed4cd0f; Path=/; HttpOnly; Secure",
        "X-Csrf-Token": "013acda7194cb9484d5810d31fbe0b5d826f20a0683137",
        "Content-Type": "text/html; charset=utf-8",
        "Retry-After": "60",
        "X-RateLimit-Remaining": "0",
        "cf-ray": "9a1b2c3d4e5f6789-FRA",
        "Authorization": "Bearer supersecret",
    }

    def test_drops_credential_bearing_headers(self):
        from Database.patches import _safeResponseHeaders

        safe = _safeResponseHeaders(self.SAMPLE_HEADERS)

        self.assertNotIn("Set-Cookie", safe)
        self.assertNotIn("X-Csrf-Token", safe)
        self.assertNotIn("Authorization", safe)
        self.assertNotIn("be483bc22ea1d2dc9b5d0ece1ed4cd0f", str(safe))
        self.assertNotIn("013acda7194cb9484d5810d31fbe0b5d826f20a0683137", str(safe))

    def test_keeps_diagnostic_headers(self):
        """Whatever is dropped, the headers these log sites exist for must
        survive - otherwise the diagnostics are gone along with the leak."""
        from Database.patches import _safeResponseHeaders

        safe = _safeResponseHeaders(self.SAMPLE_HEADERS)

        self.assertEqual(safe["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(safe["Retry-After"], "60")
        self.assertEqual(safe["X-RateLimit-Remaining"], "0")
        self.assertEqual(safe["cf-ray"], "9a1b2c3d4e5f6789-FRA")

    def test_unknown_header_is_dropped_not_kept(self):
        """Allowlist, not denylist: a header nobody anticipated must default to
        dropped, so a future credential-bearing header can't leak before anyone
        notices it exists."""
        from Database.patches import _safeResponseHeaders

        self.assertEqual(_safeResponseHeaders({"X-Some-Future-Token": "secret"}), {})

    def test_tolerates_missing_or_unusable_headers(self):
        """These call sites run on a failing response - header access must never
        be the thing that raises inside an error path."""
        from Database.patches import _safeResponseHeaders

        self.assertEqual(_safeResponseHeaders(None), {})
        self.assertEqual(_safeResponseHeaders(object()), {})

    def test_get_user_info_failure_log_has_no_credentials(self):
        """End-to-end over the real patched method: the credential values must
        not appear anywhere in the emitted log record."""
        import spotapi.user
        from spotapi.exceptions import UserError

        mock_login = MagicMock()
        mock_login.logged_in = True
        user_inst = spotapi.user.User(mock_login)

        resp = MagicMock()
        resp.status_code = 429
        resp.fail = True
        resp.error.string = "Too Many Requests"
        resp.response = "Rate limit hit"
        resp.raw.headers = dict(self.SAMPLE_HEADERS)
        mock_login.client.get.return_value = resp

        with self.assertLogs("Database.patches", level="WARNING") as logCapture:
            with self.assertRaises(UserError):
                user_inst.get_user_info()

        emitted = "\n".join(logCapture.output)
        self.assertNotIn("__Host-sp_csrf_sid", emitted)
        self.assertNotIn("013acda7194cb9484d5810d31fbe0b5d826f20a0683137", emitted)
        self.assertIn("Retry-After", emitted)  #< the diagnostic still lands


class TestResponseBodyLogging(unittest.TestCase):
    """Spotify answers a rate-limited/bot-checked request to these endpoints
    with its full HTML fallback page, so the verbatim body used to put a
    kilobyte of markup and CSS into the log - three times per flake, once here
    and twice more when the listener re-logged the exception carrying it.
    Routine operation gets a description of the page; FLASK_DEBUG brings the
    raw snippet back."""

    _LOGGER = "Database.patches"

    #< the real fallback page from Database/Data/app.log, shortened but same shape
    FALLBACK_PAGE = (
        '<!DOCTYPE html><html><head><meta charSet="utf-8" data-next-head=""/>'
        '<title data-next-head="">Oh nein!</title>'
        '<style data-next-head="">body { background-color: #ff4834; }</style>'
        + ("<p>x</p>" * 300) + "</head></html>"
    )

    def _describe(self, response, maxLen=None):
        from Database.patches import _describeResponseBody, RESPONSE_SNIPPET_MAX_LEN
        return _describeResponseBody(response, RESPONSE_SNIPPET_MAX_LEN if maxLen is None else maxLen)

    def _userInstance(self, response, status=200, fail=False):
        import spotapi.user
        mockLogin = MagicMock()
        mockLogin.logged_in = True
        resp = MagicMock()
        resp.status_code = status
        resp.fail = fail
        resp.response = response
        resp.raw.headers = {}
        mockLogin.client.get.return_value = resp
        return spotapi.user.User(mockLogin)

    def test_missing_body_stays_missing(self):
        self.assertIsNone(self._describe(None))

    def test_html_page_collapses_to_type_size_and_title(self):
        """The <title> is what distinguishes Spotify's own fallback page from a
        Cloudflare challenge - the reason these log sites exist - so it is the
        one part of the markup worth keeping."""
        described = self._describe(self.FALLBACK_PAGE)

        self.assertNotIn("<!DOCTYPE", described)
        self.assertNotIn("background-color", described)
        self.assertIn("html page", described)
        self.assertIn(str(len(self.FALLBACK_PAGE)), described)
        self.assertIn("Oh nein!", described)

    def test_short_plain_body_is_kept_verbatim(self):
        """The useful case: a short API error message is the diagnostic itself."""
        self.assertEqual(self._describe("Rate limit hit"), "Rate limit hit")

    def test_long_plain_body_is_truncated_with_its_length(self):
        from Database.patches import RESPONSE_SUMMARY_MAX_LEN

        body = "e" * (RESPONSE_SUMMARY_MAX_LEN + 500)
        described = self._describe(body)

        self.assertLess(len(described), len(body))
        self.assertTrue(described.startswith("e" * RESPONSE_SUMMARY_MAX_LEN))
        self.assertIn(str(len(body)), described)

    def test_flask_debug_returns_the_raw_snippet(self):
        with patch("Database.patches._flaskDebugEnabled", return_value=True):
            described = self._describe(self.FALLBACK_PAGE)

        self.assertTrue(described.startswith("<!DOCTYPE"))

    def test_flask_debug_snippet_still_respects_the_length_cap(self):
        with patch("Database.patches._flaskDebugEnabled", return_value=True):
            described = self._describe(self.FALLBACK_PAGE, maxLen=50)

        self.assertEqual(described, self.FALLBACK_PAGE[:50])

    def test_get_user_info_keeps_markup_out_of_log_and_error(self):
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError) as errCtx:
                    userInst.get_user_info()

        emitted = "\n".join(logCapture.output)
        self.assertNotIn("<!DOCTYPE", emitted)
        self.assertNotIn("background-color", emitted)
        self.assertIn("non-Mapping response", emitted)
        self.assertIn("html page", emitted)          #< the flake is still reported...
        self.assertIn("status=200", emitted)         #< ...with the diagnostics that identify it

        message = str(errCtx.exception)
        self.assertNotIn("<!DOCTYPE", message)
        self.assertIn("Invalid JSON", message)
        self.assertIn("Status: 200", message)
        self.assertIn("Type: str", message)

    def test_get_user_info_error_still_classifies_as_transient(self):
        """The listener buckets this exception by substring ("json"), and that
        bucket is what triggers the rate-limit backoff instead of a reconnect -
        shortening the message must not change the classification."""
        from spotapi.exceptions import UserError
        from Database.Listeners.spotifyListener import _is_rate_limit_error, _is_auth_error

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING"):
                with self.assertRaises(UserError) as errCtx:
                    userInst.get_user_info()

        self.assertTrue(_is_rate_limit_error(errCtx.exception))
        self.assertFalse(_is_auth_error(errCtx.exception))

    def test_get_user_info_with_flask_debug_logs_the_body(self):
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=True):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        self.assertIn("<!DOCTYPE", "\n".join(logCapture.output))

    def test_failed_request_log_omits_markup_too(self):
        """The resp.fail path dumps the same body from the same endpoint."""
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE, status=429, fail=True)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        emitted = "\n".join(logCapture.output)
        self.assertNotIn("<!DOCTYPE", emitted)
        self.assertIn("HTTP request failed", emitted)

    def test_get_plan_info_keeps_markup_out_of_log_and_error(self):
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError) as errCtx:
                    userInst.get_plan_info()

        self.assertNotIn("<!DOCTYPE", "\n".join(logCapture.output))
        self.assertNotIn("<!DOCTYPE", str(errCtx.exception))
        self.assertIn("Invalid JSON", str(errCtx.exception))


class TestFlaskDebugEnabled(unittest.TestCase):
    """Same switch (and same accepted values) as the rest of the app's verbose
    diagnostics - a body dump must not turn on for FLASK_DEBUG=0."""

    def _enabled(self, value):
        import os
        from Database.patches import _flaskDebugEnabled
        if value is None:
            env = {k: v for k, v in os.environ.items() if k != "FLASK_DEBUG"}
            with patch.dict(os.environ, env, clear=True):
                return _flaskDebugEnabled()
        with patch.dict(os.environ, {"FLASK_DEBUG": value}):
            return _flaskDebugEnabled()

    def test_truthy_values_enable(self):
        self.assertTrue(self._enabled("1"))
        self.assertTrue(self._enabled("true"))
        self.assertTrue(self._enabled("TRUE"))

    def test_falsy_and_unset_values_disable(self):
        self.assertFalse(self._enabled("0"))
        self.assertFalse(self._enabled(""))
        self.assertFalse(self._enabled(None))


class _FakeWebsocket:
    """Stands in for the sync websocket client: each recv() consumes the next
    scripted result - Exception instances are raised, anything else returned.
    Records the timeout it was called with, which is the whole point of the
    patch under test."""

    def __init__(self, results):
        self._results = list(results)
        self.recvTimeouts = []
        #< a real ClientConnection owns a socket and two daemon threads
        #  (recv_events, keepalive); `closed` is how the tests below see whether
        #  a failed handshake let go of them
        self.closed = False

    def recv(self, timeout=None, decode=None):
        self.recvTimeouts.append(timeout)
        if not self._results:
            raise TimeoutError("no more scripted results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def _fakeStreamer(results, deliberateClose=False):
    """A stand-in WebsocketStreamer carrying only what get_packet touches."""
    import types
    streamer = types.SimpleNamespace()
    streamer.ws = _FakeWebsocket(results)
    streamer.rlock = threading.Lock()
    streamer.ws_dump = None
    streamer._deliberate_close = deliberateClose
    streamer.reconnect = MagicMock()
    return streamer


def _getPacket(streamer, **kwargs):
    import spotapi.websocket
    return spotapi.websocket.WebsocketStreamer.get_packet(streamer, **kwargs)


class TestPatchedGetPacket(unittest.TestCase):
    """spotapi's own get_packet holds rlock across an UNBOUNDED ws.recv(), and
    keep_alive needs that same lock to send its 60s ping. On a quiet account
    the receive loop parks in recv() holding the lock, the ping never goes out,
    and Spotify drops the connection on its idle timeout - recovered by a full
    re-login, the most bot-detection-sensitive path there is.

    That is why LastPlayedManger polls instead of listening, and why adopting
    spotapi's EventManager as-is would be worse than the polling it replaces.
    Phase 0 measured the fix working: 601/601 keepalive pongs over 10 hours
    (see eventDrivenConnectStatePlan.md).

    Its bare `except Exception` is the second half of the problem: it treats a
    plain timeout - the NORMAL state of a push channel - as a reason to
    reconnect, and retries forever with a 1s sleep."""

    def test_a_frame_is_returned_as_a_dict_and_cached(self):
        streamer = _fakeStreamer(['{"type":"pong"}'])

        packet = _getPacket(streamer)

        self.assertEqual(packet, {"type": "pong"})
        self.assertEqual(streamer.ws_dump, {"type": "pong"})

    def test_the_recv_wait_is_bounded(self):
        """The starvation fix: the lock may only be held for a bounded recv,
        never an open-ended one. An unbounded wait is what stops the ping."""
        from Database.patches import WS_RECV_TIMEOUT_SECONDS

        streamer = _fakeStreamer(['{"type":"pong"}'])
        _getPacket(streamer)

        self.assertEqual(streamer.ws.recvTimeouts, [WS_RECV_TIMEOUT_SECONDS])

    def test_the_lock_is_released_before_returning(self):
        streamer = _fakeStreamer(['{"type":"pong"}'])
        _getPacket(streamer)

        self.assertTrue(streamer.rlock.acquire(blocking=False))
        streamer.rlock.release()

    def test_a_timeout_returns_none_without_reconnecting(self):
        """Silence is the normal state of a push channel - Phase 0 saw 3.75 h
        between state pushes. Reconnecting on it would turn every idle period
        into a re-login."""
        streamer = _fakeStreamer([TimeoutError("nothing pushed")])

        self.assertIsNone(_getPacket(streamer))
        streamer.reconnect.assert_not_called()

    def test_a_deliberate_close_returns_without_touching_the_socket(self):
        streamer = _fakeStreamer(['{"type":"pong"}'], deliberateClose=True)

        self.assertIsNone(_getPacket(streamer))
        self.assertEqual(streamer.ws.recvTimeouts, [])
        streamer.reconnect.assert_not_called()

    def test_a_missing_socket_returns_none_rather_than_raising(self):
        streamer = _fakeStreamer([])
        streamer.ws = None

        self.assertIsNone(_getPacket(streamer))
        streamer.reconnect.assert_not_called()

    def test_a_clean_close_that_was_not_deliberate_reconnects(self):
        """Spotify hangs up the dealer cleanly about once an hour. Before this
        reconnected, the push loop logged "closed cleanly" once per 1-second
        poll until keep_alive's next ping noticed (up to a minute) - and lost
        that much push time, against a one-way push->poll fallback."""
        import websockets.exceptions

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedOK(None, None)])

        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_called_once_with()

    def test_a_frame_clears_a_latched_reconnect_ceiling(self):
        """Nothing else clears _recvReconnectFailures - patched_keep_alive
        keeps its own local count and never touches this one - so a single
        ~2-minute dealer outage retired the receive path for the life of the
        streamer, while keep-alive's reconnect succeeded and frames resumed.
        A frame arriving IS the transport proving itself."""
        from Database.patches import WS_RECV_MAX_RECONNECT_FAILURES

        streamer = _fakeStreamer(['{"type":"pong"}'])
        streamer._recvReconnectFailures = WS_RECV_MAX_RECONNECT_FAILURES

        _getPacket(streamer)

        self.assertEqual(streamer._recvReconnectFailures, 0)

    def test_a_frame_after_a_latched_ceiling_restores_the_next_reconnect(self):
        """What the latch actually cost: at each later routine hangup ("Spotify
        hangs up the dealer cleanly about once an hour")
        _reconnectAfterDroppedPacket returned False silently and the loop paced
        itself 1s at a time until keep-alive's next ping noticed - up to 60s of
        blind push, in which a track that both starts and ends is never seen."""
        import websockets.exceptions
        from Database.patches import WS_RECV_MAX_RECONNECT_FAILURES

        streamer = _fakeStreamer(['{"type":"pong"}',
                                  websockets.exceptions.ConnectionClosedOK(None, None)])
        streamer._recvReconnectFailures = WS_RECV_MAX_RECONNECT_FAILURES

        _getPacket(streamer)   #< keep-alive reconnected; frames are flowing again
        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_called_once_with()

    def test_an_ordinary_frame_does_not_pay_for_the_reset(self):
        """Every pushed frame runs this path; the clear is guarded so the
        healthy case is a read, not a write."""
        streamer = _fakeStreamer(['{"type":"pong"}'])

        with patch("Database.patches._setRecvReconnectFailures") as setter:
            _getPacket(streamer)

        setter.assert_not_called()

    def test_a_dead_socket_at_the_reconnect_ceiling_still_honours_the_timeout(self):
        """get_packet(timeout=X) promises "wait up to X", and _runPushLoop
        leans on it as its ONLY pacing - that loop has no sleep of its own.

        recv() on a CLOSED socket raises immediately instead of waiting, and a
        failed reconnect leaves self.ws pointing at exactly that socket (it is
        closed on entry and only reassigned on success). Once
        _recvReconnectFailures latches at the ceiling,
        _reconnectAfterDroppedPacket returns without reconnecting and without
        sleeping - so every turn of the push loop cost nothing and it spun a
        core flat out until the 300s silence watchdog fell back to polling."""
        import websockets.exceptions
        from Database.patches import WS_RECV_MAX_RECONNECT_FAILURES, WS_RECV_TIMEOUT_SECONDS

        streamer = _fakeStreamer([websockets.exceptions.ConnectionClosedOK(None, None)])
        streamer._recvReconnectFailures = WS_RECV_MAX_RECONNECT_FAILURES

        with patch("Database.patches._sleepInterruptibly") as slept:
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_not_called()   #< the ceiling is latched
        slept.assert_called_once()
        self.assertEqual(slept.call_args.args[1], WS_RECV_TIMEOUT_SECONDS,
                         "the caller's own timeout is what has to be honoured")

    def test_a_reconnect_that_worked_does_not_add_a_wait(self):
        """The other side of it: after a successful reconnect the next recv
        has a live socket to wait on, so paying the timeout here as well would
        halve the push channel's responsiveness for nothing."""
        import websockets.exceptions

        streamer = _fakeStreamer([websockets.exceptions.ConnectionClosedOK(None, None)])

        with patch("Database.patches._sleepInterruptibly") as slept, \
             self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_called_once_with()
        slept.assert_not_called()

    def test_a_clean_close_after_a_deliberate_stop_stays_quiet(self):
        """stop() closes the socket mid-read: the resulting clean-close must
        end quietly, not resurrect the connection being torn down."""
        import websockets.exceptions

        streamer = _fakeStreamer([])

        def closingRecv(timeout=None, decode=None):
            streamer._deliberate_close = True
            raise websockets.exceptions.ConnectionClosedOK(None, None)
        streamer.ws.recv = closingRecv

        self.assertIsNone(_getPacket(streamer))
        streamer.reconnect.assert_not_called()

    def test_an_unexpected_drop_reconnects_once(self):
        import websockets.exceptions

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedError(None, None)])

        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_called_once_with()

    def test_reconnect_failures_are_bounded(self):
        """spotapi retries forever on a 1s sleep. A dead endpoint must not be
        hammered - the stale-feed detector rebuilds the session instead."""
        import websockets.exceptions
        from Database.patches import WS_RECV_MAX_RECONNECT_FAILURES

        drops = [websockets.exceptions.ConnectionClosedError(None, None)
                 for _ in range(WS_RECV_MAX_RECONNECT_FAILURES + 3)]
        streamer = _fakeStreamer(drops)
        streamer.reconnect = MagicMock(side_effect=RuntimeError("endpoint down"))

        with self.assertLogs("Database.patches", level="WARNING"):
            with patch("Database.patches.time.sleep"):
                for _ in range(WS_RECV_MAX_RECONNECT_FAILURES + 3):
                    _getPacket(streamer)

        self.assertLessEqual(streamer.reconnect.call_count, WS_RECV_MAX_RECONNECT_FAILURES)

    def test_a_closed_session_gives_up_immediately(self):
        """curl_cffi's closed session can never be revived - see
        _isSessionClosedError. One attempt, then stop."""
        import websockets.exceptions
        from spotapi.exceptions import RequestError

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedError(None, None),
            websockets.exceptions.ConnectionClosedError(None, None)])
        streamer.reconnect = MagicMock(side_effect=RequestError(
            "Failed to complete request.", error="Session is closed, cannot send request."))

        with self.assertLogs("Database.patches", level="ERROR"):
            _getPacket(streamer)
            _getPacket(streamer)

        streamer.reconnect.assert_called_once_with()

    def test_an_unparsable_frame_is_dropped_not_reconnected(self):
        """A malformed frame says nothing about the connection's health."""
        streamer = _fakeStreamer(["<html>nope</html>"])

        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_not_called()

    def test_a_non_object_frame_is_dropped(self):
        """dict(json.loads(...)) raises on a JSON array or scalar - spotapi's
        version let that reach its reconnect-on-anything handler."""
        streamer = _fakeStreamer(["[1, 2, 3]"])

        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_not_called()

    def test_the_caller_can_override_the_timeout(self):
        streamer = _fakeStreamer(['{"type":"pong"}'])

        _getPacket(streamer, timeout=0.25)

        self.assertEqual(streamer.ws.recvTimeouts, [0.25])

    def test_a_slotted_instance_says_so_instead_of_retrying_forever(self):
        """WebsocketStreamer declares __slots__ without the failure counter.
        Every instance this app builds is a PlayerStatus, which has a __dict__ -
        but if that ever changed, losing the ceiling silently would restore
        exactly the reconnect storm this patch exists to prevent."""
        import websockets.exceptions
        from Database.patches import _setRecvReconnectFailures

        class Slotted:
            __slots__ = ()

        with self.assertLogs("Database.patches", level="WARNING") as logCapture:
            _setRecvReconnectFailures(Slotted(), 1)

        self.assertIn("__slots__", "\n".join(logCapture.output))

    def test_a_successful_reconnect_clears_the_failure_count(self):
        import websockets.exceptions

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedError(None, None),
            websockets.exceptions.ConnectionClosedError(None, None)])
        streamer.reconnect = MagicMock(side_effect=[RuntimeError("blip"), None])

        with self.assertLogs("Database.patches", level="WARNING"):
            _getPacket(streamer)
            _getPacket(streamer)

        self.assertEqual(streamer._recvReconnectFailures, 0)

    def test_event_manager_still_gets_the_shape_it_expects(self):
        """EventManager._listen does `if event is None or event.get("payloads")
        is None: continue` - returning None on a quiet socket is exactly what
        that already handles, so the existing caller stays correct."""
        streamer = _fakeStreamer([TimeoutError()])
        event = _getPacket(streamer)

        self.assertIsNone(event)   #< _listen's own guard covers this


class TestEnforceShadowRemoval(unittest.TestCase):
    """spotapi's @enforce class decorator iterates dir(cls) - inherited methods
    included - and setattr's a signature-checking wrapper of each onto the
    decorated class itself. PlayerStatus and EventManager are both decorated,
    so at import time they froze their own copies of the ORIGINAL base-class
    methods, shadowing every later class-level patch. The 2026-07-31 symptom:
    the push loop's get_packet(timeout=...) hit the frozen (self)-only
    signature and died with "TypeError: got an unexpected keyword argument
    'timeout'" - taking the whole listener thread with it, since only a
    "fallback" RETURN reverts to polling, never an exception. These tests pin
    that every class the app can instantiate resolves to the patched methods."""

    def test_player_status_sees_the_patched_get_packet(self):
        from Database.patches import patched_get_packet
        self.assertIs(spotapi.status.PlayerStatus.get_packet, patched_get_packet)

    def test_player_status_sees_the_patched_keep_alive(self):
        from Database.patches import patched_keep_alive
        self.assertIs(spotapi.status.PlayerStatus.keep_alive, patched_keep_alive)

    def test_event_manager_sees_the_patched_methods_too(self):
        from Database.patches import patched_get_packet, patched_keep_alive
        self.assertIs(spotapi.status.EventManager.get_packet, patched_get_packet)
        self.assertIs(spotapi.status.EventManager.keep_alive, patched_keep_alive)

    def test_event_manager_inherits_the_playerstatus_level_patches(self):
        """reconnect/renew_state are patched onto PlayerStatus directly, which
        replaces its frozen copy - but EventManager froze its own copies of the
        originals at import and must fall through to the patched ones."""
        from Database.patches import player_status_reconnect, player_status_renew_state
        self.assertIs(spotapi.status.EventManager.reconnect, player_status_reconnect)
        self.assertIs(spotapi.status.EventManager.renew_state, player_status_renew_state)

    def test_get_packet_accepts_timeout_through_player_status(self):
        """The exact call _runPushLoop makes, resolved the way a real instance
        would resolve it - through PlayerStatus, not WebsocketStreamer."""
        streamer = _fakeStreamer(['{"type":"pong"}'])

        packet = spotapi.status.PlayerStatus.get_packet(streamer, timeout=0.25)

        self.assertEqual(packet, {"type": "pong"})
        self.assertEqual(streamer.ws.recvTimeouts, [0.25])

    def test_both_classes_see_the_patched_init_packet_read(self):
        """Construction reads the init packet through _create_websocket, and
        both instantiable classes froze their own copy of the unbounded
        original at import."""
        from Database.patches import patched_get_init_packet
        self.assertIs(spotapi.status.PlayerStatus.get_init_packet, patched_get_init_packet)
        self.assertIs(spotapi.status.EventManager.get_init_packet, patched_get_init_packet)

    def test_the_init_packet_read_is_bounded_through_player_status(self):
        """The read a real construction makes, resolved the way a real
        instance resolves it. Unbounded, a dealer that accepts the socket and
        then says nothing parked the constructing thread forever - and that
        thread holds the per-user _listener_lock, so the next startListener
        for that user blocked forever too, wedging _ensureAllUsersLogin's pass
        and with it the login-check loop for EVERY user until restart."""
        from Database.patches import WS_INIT_PACKET_TIMEOUT_SECONDS
        streamer = _fakeStreamer(
            ['{"headers": {"Spotify-Connection-Id": "conn-1"}}'])

        connectionId = spotapi.status.PlayerStatus.get_init_packet(streamer)

        self.assertEqual("conn-1", connectionId)
        self.assertEqual([WS_INIT_PACKET_TIMEOUT_SECONDS], streamer.ws.recvTimeouts)

    def test_an_invalid_init_packet_still_raises(self):
        """Same contract as spotapi's own: a packet without a connection id is
        a failed handshake, not a usable session."""
        streamer = _fakeStreamer(['{"headers": {}}'])

        with self.assertRaises(ValueError):
            spotapi.status.PlayerStatus.get_init_packet(streamer)

    def test_a_timed_out_init_packet_closes_the_socket(self):
        """The deadline stops the thread parking, but the socket it was reading
        is spotapi's `self.ws`, assigned by _create_websocket BEFORE this call.
        Raising past it strands an open ClientConnection - plus its recv_events
        and keepalive threads - on a half-built PlayerStatus nothing can reach:
        no Listener was assigned, so Listener.stop()'s manager.ws.close() never
        runs, and spotapi registers its atexit hook only after connect() returns.

        The reconnect path written in the same commit already does this
        (patched_reconnect's `except BaseException: newWs.close(); raise`); a
        dealer that pongs but never sends the init frame leaks one per retry,
        and retries are exactly what the login loop does."""
        streamer = _fakeStreamer([TimeoutError("dealer never spoke")])

        with self.assertRaises(TimeoutError):
            spotapi.status.PlayerStatus.get_init_packet(streamer)

        self.assertTrue(streamer.ws.closed,
                        "a handshake that timed out must not leave its socket open")

    def test_an_invalid_init_packet_closes_the_socket_too(self):
        """Same reasoning as the timeout: the connection is unusable either way,
        and this is the older of the two ways out of this function."""
        streamer = _fakeStreamer(['{"headers": {}}'])

        with self.assertRaises(ValueError):
            spotapi.status.PlayerStatus.get_init_packet(streamer)

        self.assertTrue(streamer.ws.closed)

    def test_a_successful_handshake_leaves_the_socket_open(self):
        """The other half of the contract - this is the socket the session is
        about to be built on."""
        streamer = _fakeStreamer(
            ['{"headers": {"Spotify-Connection-Id": "conn-1"}}'])

        spotapi.status.PlayerStatus.get_init_packet(streamer)

        self.assertFalse(streamer.ws.closed)

    def test_both_classes_see_the_patched_device_registration(self):
        from Database.patches import patched_register_device, patched_connect_device
        self.assertIs(spotapi.status.PlayerStatus.register_device, patched_register_device)
        self.assertIs(spotapi.status.PlayerStatus.connect_device, patched_connect_device)
        self.assertIs(spotapi.status.EventManager.register_device, patched_register_device)
        self.assertIs(spotapi.status.EventManager.connect_device, patched_connect_device)


def _fakeDeviceStreamer(resp):
    """A stand-in carrying only what register_device/connect_device touch."""
    import types
    streamer = types.SimpleNamespace()
    streamer.device_id = "device-1234"
    streamer.connection_id = "conn-5678"
    streamer.client = MagicMock()
    streamer.client.post.return_value = resp
    streamer.client.put.return_value = resp
    return streamer


def _deviceResponse(fail=False, response=None):
    resp = MagicMock()
    resp.fail = fail
    resp.status_code = 400 if fail else 200
    resp.response = response if response is not None else {}
    resp.error.string = "denied" if fail else None
    resp.raw.headers = {"Content-Type": "application/json"}
    return resp


class TestPatchedDeviceRegistration(unittest.TestCase):
    """spotapi's register_device/connect_device print a five-line diagnostic
    block to stdout on failure - invisible in app.log, where the callers' own
    one-line warnings land. And connect_device sits on the hot paths (the
    renew_state poll tick and the push loop's periodic resubscribe), so one
    throttled spell printed a block every few seconds to a console nobody
    reads during an incident. The replacements keep the exact request
    contract and route the diagnostics through logging."""

    def test_a_failed_registration_logs_and_raises_without_stdout(self):
        streamer = _fakeDeviceStreamer(_deviceResponse(fail=True))
        captured = io.StringIO()
        with self.assertLogs("Database.patches", level="WARNING") as logs, \
                contextlib.redirect_stdout(captured):
            with self.assertRaises(spotapi.exceptions.WebSocketError):
                spotapi.websocket.WebsocketStreamer.register_device(streamer)
        message = " ".join(logs.output)
        self.assertIn("device-1234", message)
        self.assertIn("denied", message)
        self.assertEqual(captured.getvalue(), "", "diagnostics must log, not print")

    def test_a_successful_registration_posts_the_fork_payload(self):
        streamer = _fakeDeviceStreamer(_deviceResponse())

        spotapi.websocket.WebsocketStreamer.register_device(streamer)

        args, kwargs = streamer.client.post.call_args
        self.assertIn("track-playback/v1/devices", args[0])
        self.assertTrue(kwargs["authenticate"])
        payload = kwargs["json"]
        self.assertEqual(payload["device"]["device_id"], "device-1234")
        self.assertEqual(payload["connection_id"], "conn-5678")
        self.assertEqual(payload["device"]["model"], "web_player")  #< fork payload, verbatim

    def test_a_failed_connect_logs_and_raises_without_stdout(self):
        streamer = _fakeDeviceStreamer(_deviceResponse(fail=True))
        captured = io.StringIO()
        with self.assertLogs("Database.patches", level="WARNING") as logs, \
                contextlib.redirect_stdout(captured):
            with self.assertRaises(spotapi.exceptions.WebSocketError):
                spotapi.websocket.WebsocketStreamer.connect_device(streamer)
        message = " ".join(logs.output)
        self.assertIn("conn-5678", message)
        self.assertEqual(captured.getvalue(), "", "diagnostics must log, not print")

    def test_a_successful_connect_returns_the_cluster_and_names_the_connection(self):
        cluster = {"player_state": {"timestamp": "1"}}
        streamer = _fakeDeviceStreamer(_deviceResponse(response=cluster))

        result = spotapi.websocket.WebsocketStreamer.connect_device(streamer)

        self.assertEqual(result, cluster)
        args, kwargs = streamer.client.put.call_args
        self.assertIn("hobs_device-1234", args[0])
        self.assertTrue(kwargs["authenticate"])
        self.assertEqual(kwargs["headers"]["x-spotify-connection-id"], "conn-5678")


class TestThrottleDetection(unittest.TestCase):
    """_looksThrottled decides whether a reply is Spotify pushing back. The
    hard case is the one that actually happens: HTTP 200 carrying an HTML
    bot-check page, which no status-code check can see."""

    def _looksThrottled(self, status, response):
        from Database.patches import _looksThrottled
        return _looksThrottled(status, response)

    def test_explicit_throttle_statuses_count(self):
        self.assertTrue(self._looksThrottled(429, None))
        self.assertTrue(self._looksThrottled(503, None))

    def test_html_body_counts_even_at_http_200(self):
        """The "Oh nein!" fallback page - the exact shape seen in app.log."""
        page = "<!DOCTYPE html><html><head><title>Oh nein!</title></head><body>x</body></html>"
        self.assertTrue(self._looksThrottled(200, page))

    def test_a_normal_json_reply_is_not_throttling(self):
        self.assertFalse(self._looksThrottled(200, {"id": "alice"}))

    def test_an_ordinary_failure_is_not_throttling(self):
        """A 404 or a 500 with a plain body is a failure, not back-pressure -
        pausing every user's Spotify traffic for it would be wrong."""
        self.assertFalse(self._looksThrottled(404, "not found"))
        self.assertFalse(self._looksThrottled(500, "internal error"))

    def test_an_unstringable_body_never_raises(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("nope")

        self.assertFalse(self._looksThrottled(200, Hostile()))


class TestSharedLimiterWiring(unittest.TestCase):
    """Every Spotify request in the process goes through one limiter, and
    every rate-limit signal pauses all of them.

    This is the property the old code did NOT have: a rate limit paused the
    one listener poll thread that noticed it (~0.3 requests/minute) while every
    user's connect-state poll carried on at ~10 requests/minute each."""

    _LOGGER = "Database.patches"
    FALLBACK_PAGE = (
        "<!DOCTYPE html><html><head><title>Oh nein!</title>"
        "<style>body { background-color: #eee; }</style></head><body>x</body></html>"
    )

    def setUp(self):
        from Database.rate_limit import SPOTIFY_LIMITER
        self.limiter = SPOTIFY_LIMITER   #< conftest resets its state per test

    def _userInstance(self, response, status=200, fail=False):
        import spotapi.user
        mockLogin = MagicMock()
        mockLogin.logged_in = True
        resp = MagicMock()
        resp.status_code = status
        resp.fail = fail
        resp.response = response
        resp.raw.headers = {}
        mockLogin.client.get.return_value = resp
        return spotapi.user.User(mockLogin), mockLogin

    def test_profile_lookup_takes_a_slot_before_requesting(self):
        userInst, mockLogin = self._userInstance({"id": "alice"})

        with patch.object(self.limiter, "acquire", return_value=True) as acquire:
            userInst.get_user_info()

        acquire.assert_called_once()
        mockLogin.client.get.assert_called_once()

    def test_profile_lookup_is_skipped_while_the_process_is_paused(self):
        """No slot means the request is never sent - a backoff that still let
        the request through would be decorative."""
        from Database.patches import SpotifyLocallyRateLimitedError

        userInst, mockLogin = self._userInstance({"id": "alice"})

        with patch.object(self.limiter, "acquire", return_value=False):
            with self.assertRaises(SpotifyLocallyRateLimitedError):
                userInst.get_user_info()

        mockLogin.client.get.assert_not_called()

    def test_local_pause_classifies_as_transient_not_auth(self):
        """The listener buckets this exception the same way it buckets a real
        rate limit: back off and retry, never bounce the user to a re-login."""
        from Database.patches import SpotifyLocallyRateLimitedError
        from Database.Listeners.spotifyListener import _is_rate_limit_error, _is_auth_error

        error = SpotifyLocallyRateLimitedError(
            "Spotify rate limit backoff in progress - skipped account-settings/profile")
        self.assertTrue(_is_rate_limit_error(error))
        self.assertFalse(_is_auth_error(error))

    def test_bot_check_page_pauses_every_spotify_request(self):
        from spotapi.exceptions import UserError

        userInst, _ = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING"):
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        snapshot = self.limiter.snapshot()
        self.assertEqual(snapshot["backoffs"], 1)
        self.assertGreater(snapshot["backoffRemainingSeconds"], 0)

    def test_the_backoff_names_the_endpoint_that_pushed_back(self):
        """Which Spotify surface complained is the thing the logs could not
        answer before - all three of them logged an anonymous line."""
        from spotapi.exceptions import UserError
        from Database.patches import ENDPOINT_ACCOUNT_PROFILE, ENDPOINT_ACCOUNT_PLAN

        # Each half grants its own slot explicitly: the first bot-check opens a
        # real process-wide window, which would (correctly) refuse the second
        # call outright - the very behaviour the other tests here assert.
        for fetch, expected in (("get_user_info", ENDPOINT_ACCOUNT_PROFILE),
                                ("get_plan_info", ENDPOINT_ACCOUNT_PLAN)):
            userInst, _ = self._userInstance(self.FALLBACK_PAGE)
            with patch.object(self.limiter, "acquire", return_value=True):
                with patch("Database.patches._flaskDebugEnabled", return_value=False):
                    with self.assertLogs(self._LOGGER, level="WARNING"):
                        with self.assertRaises(UserError):
                            getattr(userInst, fetch)()
            self.assertEqual(self.limiter.snapshot()["lastReason"], expected)

    def test_the_endpoint_is_named_in_the_warning_too(self):
        from spotapi.exceptions import UserError
        from Database.patches import ENDPOINT_ACCOUNT_PROFILE

        userInst, _ = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        self.assertIn(ENDPOINT_ACCOUNT_PROFILE, "\n".join(logCapture.output))

    def test_a_clean_reply_pauses_nothing(self):
        userInst, _ = self._userInstance({"id": "alice"})
        userInst.get_user_info()
        self.assertEqual(self.limiter.snapshot()["backoffs"], 0)

    def test_an_ordinary_failure_pauses_nothing(self):
        """resp.fail with a plain body and a non-throttle status is a broken
        request, not back-pressure."""
        from spotapi.exceptions import UserError

        userInst, _ = self._userInstance("boom", status=500, fail=True)

        with self.assertLogs(self._LOGGER, level="WARNING"):
            with self.assertRaises(UserError):
                userInst.get_user_info()

        self.assertEqual(self.limiter.snapshot()["backoffs"], 0)

    def test_a_429_pauses_every_spotify_request(self):
        from spotapi.exceptions import UserError

        userInst, _ = self._userInstance("Too Many Requests", status=429, fail=True)

        with self.assertLogs(self._LOGGER, level="WARNING"):
            with self.assertRaises(UserError):
                userInst.get_user_info()

        self.assertEqual(self.limiter.snapshot()["backoffs"], 1)


class TestPinnedTotpSecret(unittest.TestCase):
    """Spotify's web player derives a TOTP from a rotating secret. spotapi
    fetched that secret from a third-party mirror on every cold start and
    every 15 minutes after - a host we don't control, can't pin and didn't
    choose, sitting in the login path, with a SILENT fallback when it failed.
    The secret is pinned here instead."""

    def test_the_pinned_secret_is_returned_without_touching_the_network(self):
        import spotapi.client
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION, SPOTIFY_TOTP_SECRET_BYTES

        with patch("spotapi.client.requests.get") as mirrorGet:
            version, secret = spotapi.client.get_latest_totp_secret()

        mirrorGet.assert_not_called()   #< the mirror is where the fetch used to go
        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)
        self.assertEqual(secret, bytearray(SPOTIFY_TOTP_SECRET_BYTES))

    def test_the_secret_is_a_fresh_bytearray_each_call(self):
        """generate_totp() enumerates and transforms the bytes; handing out the
        same mutable object every time would let one caller's mutation corrupt
        every later login."""
        import spotapi.client

        _, first = spotapi.client.get_latest_totp_secret()
        first[0] ^= 0xFF
        _, second = spotapi.client.get_latest_totp_secret()

        self.assertNotEqual(first, second)

    def test_generate_totp_still_produces_a_code_for_the_pinned_version(self):
        """The patch replaces only where the secret comes from - the derivation
        spotapi does with it, and the version it reports, must be unchanged."""
        import spotapi.client
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION

        totp, version = spotapi.client.generate_totp()

        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)
        self.assertRegex(totp, r"^\d{6}$")

    def test_the_pin_matches_the_dependency_s_own_fallback(self):
        """Sanity check on spotapi itself, and the evidence that pinning
        changed nothing: as of 2026-07-30 the mirror's newest entry and
        spotapi's hardcoded _FALLBACK_SECRET are byte-identical, so the fetch
        this patch removes was returning what the library already had. If a
        spotapi bump changes its fallback, this fails - which is the moment to
        check whether Spotify rotated and the pin needs refreshing."""
        import spotapi.client
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION, SPOTIFY_TOTP_SECRET_BYTES

        fallbackVersion, fallbackSecret = spotapi.client._FALLBACK_SECRET

        self.assertEqual(int(fallbackVersion), SPOTIFY_TOTP_SECRET_VERSION)
        self.assertEqual(bytes(fallbackSecret), bytes(bytearray(SPOTIFY_TOTP_SECRET_BYTES)))


class TestTotpSecretOverride(unittest.TestCase):
    """Rotation must not require a new Docker image. The published image is
    what most installs run, so an operator needs a way to apply a new secret
    without waiting for a release - but a malformed value must never be what
    takes an instance's login offline."""

    def _resolve(self, raw):
        from Database.patches import _resolveTotpSecret
        with patch.dict("os.environ", {"SPOTIFY_TOTP_SECRET": raw} if raw is not None else {},
                        clear=False):
            if raw is None:
                import os
                os.environ.pop("SPOTIFY_TOTP_SECRET", None)
            return _resolveTotpSecret()

    def test_unset_uses_the_pinned_default(self):
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION, SPOTIFY_TOTP_SECRET_BYTES

        version, secret = self._resolve(None)

        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)
        self.assertEqual(secret, bytearray(SPOTIFY_TOTP_SECRET_BYTES))

    def test_a_well_formed_override_wins(self):
        version, secret = self._resolve("62: 1, 2 ,3")

        self.assertEqual(version, 62)
        self.assertEqual(secret, bytearray([1, 2, 3]))

    def test_a_malformed_override_falls_back_to_the_pin_and_says_so(self):
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION

        for raw in ("nonsense", "61:", ":1,2,3", "61:1,two,3", "61:1,999,3", "61:-1,2"):
            with self.subTest(raw=raw):
                with self.assertLogs("Database.patches", level="ERROR"):
                    version, _ = self._resolve(raw)
                self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)

    def test_an_empty_override_is_just_unset_and_stays_quiet(self):
        """Set-but-empty is what an unfilled compose placeholder looks like.
        It means "no override", so it must not log an error on every start."""
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION

        with self.assertNoLogs("Database.patches", level="ERROR"):
            version, _ = self._resolve("   ")

        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)


class TestTotpRotationTracking(unittest.TestCase):
    """A rotated secret is invisible in the logs until someone greps for it,
    and its signature is distinctive: EVERY token request fails, for every
    user, persistently. Confirmed empirically (2026-07-30) by running the
    smoke test with a deliberately wrong secret - the public lookups, which
    need the TOTP-derived token but no user cookies, all failed with
    "Could not get session auth tokens", while a bad-cookies run left them
    passing and failed only current_user(). One failure is not that, though:
    a 429, an outage or a blip produce the same exception, so the state only
    flips after a run of them."""

    def setUp(self):
        from Database.patches import resetTotpAuthState
        resetTotpAuthState()
        self.addCleanup(resetTotpAuthState)

    def _fail(self, times):
        from Database.patches import recordTotpAuthFailure
        for _ in range(times):
            recordTotpAuthFailure()

    def test_a_healthy_instance_reports_ok(self):
        from Database.patches import totpAuthSnapshot

        snapshot = totpAuthSnapshot()

        self.assertFalse(snapshot["suspectedRotation"])
        self.assertEqual(snapshot["consecutiveFailures"], 0)
        self.assertEqual(snapshot["pinnedVersion"], 61)

    def test_a_single_failure_is_not_a_rotation(self):
        """Blips must not raise an alarm that sends someone editing secrets."""
        from Database.patches import totpAuthSnapshot

        self._fail(1)

        self.assertFalse(totpAuthSnapshot()["suspectedRotation"])

    def test_a_sustained_run_of_failures_is(self):
        from Database.patches import totpAuthSnapshot, TOTP_ROTATION_CONFIRM_THRESHOLD

        self._fail(TOTP_ROTATION_CONFIRM_THRESHOLD)

        snapshot = totpAuthSnapshot()
        self.assertTrue(snapshot["suspectedRotation"])
        self.assertEqual(snapshot["consecutiveFailures"], TOTP_ROTATION_CONFIRM_THRESHOLD)
        self.assertIsNotNone(snapshot["secondsSinceFirstFailure"])

    def test_one_success_clears_it(self):
        """Whatever it was, it is over - a stale alarm is worse than none."""
        from Database.patches import recordTotpAuthSuccess, totpAuthSnapshot, TOTP_ROTATION_CONFIRM_THRESHOLD

        self._fail(TOTP_ROTATION_CONFIRM_THRESHOLD)
        recordTotpAuthSuccess()

        snapshot = totpAuthSnapshot()
        self.assertFalse(snapshot["suspectedRotation"])
        self.assertEqual(snapshot["consecutiveFailures"], 0)

    def test_the_snapshot_names_what_an_operator_needs(self):
        """The panel has to answer "what now?" without a trip to the source."""
        from Database.patches import totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        snapshot = totpAuthSnapshot()

        self.assertEqual(snapshot["overrideEnvVar"], TOTP_SECRET_ENV_VAR)
        self.assertIn("overrideActive", snapshot)

    def test_it_reports_when_an_override_is_in_force(self):
        """A fetched/overridden secret must not look like the pinned one -
        otherwise "we pin 61" is a lie the panel tells you during an incident."""
        from Database.patches import totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "62:1,2,3"}):
            snapshot = totpAuthSnapshot()

        self.assertTrue(snapshot["overrideActive"])
        self.assertEqual(snapshot["activeVersion"], 62)

    def test_reapplying_the_patch_does_not_double_count(self):
        """patch_totp_secret runs at import and is re-applied by setUpModule
        when import order may have missed it. A non-idempotent wrapper would
        nest, so a single failed request would count twice and trip the
        rotation threshold on a third of the evidence it is supposed to need."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import patch_totp_secret, totpAuthSnapshot

        patch_totp_secret()
        patch_totp_secret()

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self.assertRaises(BaseClientError):
            spotapi.client.BaseClient._get_auth_vars(instance)

        self.assertEqual(totpAuthSnapshot()["consecutiveFailures"], 1)

    def test_the_patched_call_feeds_the_tracker(self):
        """The counter is worthless if the real code path doesn't drive it."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import totpAuthSnapshot

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self.assertRaises(BaseClientError):
            spotapi.client.BaseClient._get_auth_vars(instance)

        self.assertEqual(totpAuthSnapshot()["consecutiveFailures"], 1)


class TestOnlyARealTokenFetchClearsTheRotationStreak(unittest.TestCase):
    """spotapi's _get_auth_vars is a NO-OP when access_token and client_id are
    both already set - it makes no request and returns None (its own guard:
    `if self.access_token is _Undefined or self.client_id is _Undefined`).

    Treating any non-raising return as proof the pinned secret still works let
    a token nobody had to mint clear the failure streak. The app reaches that
    no-op on a hot path: player_status_reconnect skips the token clear
    whenever the cached token is not near expiry, then calls get_session(),
    which ends in _get_auth_vars(). So during a REAL rotation every listener
    reconnect holding a live token reset the count while the genuine failures
    accrued from expiring tokens and fresh logins - and the confirm threshold
    might not be reached until the last cached token died, delaying both
    /admin's suspectedRotation flag and _startTotpRecoveryInBackground."""

    def setUp(self):
        from Database.patches import resetTotpAuthState
        resetTotpAuthState()
        self.addCleanup(resetTotpAuthState)

    def _instance(self, tokenAlreadySet):
        import spotapi.client
        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        if tokenAlreadySet:
            instance.access_token = "still-valid"
            instance.client_id = "client-id"
            #< any attribute touch here means a request was made after all,
            #  which is the thing this test is asserting does NOT happen
            instance.client = None
        else:
            instance.access_token = spotapi.client._Undefined
            instance.client_id = spotapi.client._Undefined
            ok = MagicMock()
            ok.fail = False
            ok.response = {"accessToken": "fresh", "clientId": "cid",
                           "accessTokenExpirationTimestampMs": 0}
            instance.client = MagicMock()
            instance.client.get.return_value = ok
        return instance

    def test_a_call_that_fetched_nothing_leaves_the_streak_alone(self):
        import spotapi.client
        from Database.patches import recordTotpAuthFailure, totpAuthSnapshot

        recordTotpAuthFailure()
        recordTotpAuthFailure()

        self.assertIsNone(spotapi.client.BaseClient._get_auth_vars(self._instance(True)))

        self.assertEqual(totpAuthSnapshot()["consecutiveFailures"], 2,
                         "a cached token is not evidence about the secret")

    def test_a_call_that_really_minted_a_token_still_clears_it(self):
        """The negative control, and the whole point of the counter: a genuine
        mint IS evidence the pinned secret works."""
        import spotapi.client
        from Database.patches import recordTotpAuthFailure, totpAuthSnapshot

        recordTotpAuthFailure()
        recordTotpAuthFailure()

        spotapi.client.BaseClient._get_auth_vars(self._instance(False))

        self.assertEqual(totpAuthSnapshot()["consecutiveFailures"], 0)


class TestTotpAutoRecovery(unittest.TestCase):
    """Once a rotation is confirmed, the new secret is read from Spotify's own
    web-player bundle - the source of truth, not a third-party mirror. This is
    the RECOVERY path only: the pinned secret stays the normal one, so a
    Spotify restructure degrades to "recovery unavailable" (today's behaviour)
    rather than to broken logins."""

    def setUp(self):
        from Database.patches import resetTotpAuthState
        resetTotpAuthState()
        self.addCleanup(resetTotpAuthState)

    def _fetches(self, secrets):
        return patch("Database.patches.fetchWebPlayerSecrets", return_value=secrets)

    def test_a_newer_secret_is_adopted(self):
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret

        with self._fetches([(62, bytearray([1, 2, 3])), (61, bytearray([9]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                adopted = attemptTotpRecovery()

        self.assertTrue(adopted)
        self.assertEqual(_resolveTotpSecret(), (62, bytearray([1, 2, 3])))

    def test_the_same_version_is_not_adopted(self):
        """Re-reading the version we already pin proves nothing changed, so
        swapping it in would only muddy which secret is in force."""
        from Database.patches import attemptTotpRecovery, SPOTIFY_TOTP_SECRET_VERSION

        with self._fetches([(SPOTIFY_TOTP_SECRET_VERSION, bytearray([1, 2, 3]))]):
            self.assertFalse(attemptTotpRecovery())

    def test_an_older_secret_is_never_adopted(self):
        """A stale or rolled-back bundle must not walk us backwards."""
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret, SPOTIFY_TOTP_SECRET_VERSION

        with self._fetches([(SPOTIFY_TOTP_SECRET_VERSION - 5, bytearray([1, 2, 3]))]):
            self.assertFalse(attemptTotpRecovery())
        self.assertEqual(_resolveTotpSecret()[0], SPOTIFY_TOTP_SECRET_VERSION)

    def test_finding_nothing_is_not_a_crash(self):
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret, SPOTIFY_TOTP_SECRET_VERSION

        with self._fetches([]):
            self.assertFalse(attemptTotpRecovery())
        self.assertEqual(_resolveTotpSecret()[0], SPOTIFY_TOTP_SECRET_VERSION)

    def test_an_explicit_override_still_wins(self):
        """A human setting the variable is a decision; a scrape is a guess."""
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret, TOTP_SECRET_ENV_VAR

        with self._fetches([(62, bytearray([1, 2, 3]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                attemptTotpRecovery()

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "70:7,7,7"}):
            self.assertEqual(_resolveTotpSecret(), (70, bytearray([7, 7, 7])))

    def test_recovery_is_rate_limited(self):
        """An instance in this state retries constantly; without a cooldown it
        would hammer Spotify's CDN for every failed login."""
        from Database.patches import attemptTotpRecovery

        with self._fetches([]) as fetch:
            attemptTotpRecovery()
            attemptTotpRecovery()
            attemptTotpRecovery()

        self.assertEqual(fetch.call_count, 1)

    def test_it_can_be_switched_off(self):
        from Database.patches import attemptTotpRecovery, TOTP_AUTO_RECOVER_ENV_VAR

        with self._fetches([(62, bytearray([1, 2, 3]))]) as fetch:
            with patch.dict("os.environ", {TOTP_AUTO_RECOVER_ENV_VAR: "0"}):
                self.assertFalse(attemptTotpRecovery())

        fetch.assert_not_called()

    def test_a_confirmed_rotation_triggers_it(self):
        """The wiring: the failure streak reaching the threshold is what calls
        recovery - it must not need anything else to notice."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import TOTP_ROTATION_CONFIRM_THRESHOLD

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self._fetches([]) as fetch:
            with self.assertLogs("Database.patches", level="ERROR"):
                for _ in range(TOTP_ROTATION_CONFIRM_THRESHOLD):
                    with self.assertRaises(BaseClientError):
                        spotapi.client.BaseClient._get_auth_vars(instance)
            self._joinRecoveryThread()

        fetch.assert_called_once()

    def test_recovery_runs_off_the_failing_thread(self):
        """The thread that hit the auth failure can be a Flask request thread
        (login's cookie verification reaches _get_auth_vars), and recovery is
        two 15s-timeout GETs plus a multi-MB bundle download - inline, a login
        POST during a rotation hung for ~30s+. The adopted secret only takes
        effect on the NEXT attempt anyway, so nothing needs to wait: the fetch
        must run on its own thread, never the caller's."""
        import threading
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import TOTP_ROTATION_CONFIRM_THRESHOLD

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        fetchThreads = []

        def recordingFetch():
            fetchThreads.append(threading.current_thread())
            return []

        with patch("Database.patches.fetchWebPlayerSecrets", side_effect=recordingFetch):
            with self.assertLogs("Database.patches", level="ERROR"):
                for _ in range(TOTP_ROTATION_CONFIRM_THRESHOLD):
                    with self.assertRaises(BaseClientError):
                        spotapi.client.BaseClient._get_auth_vars(instance)
            self._joinRecoveryThread()

        self.assertEqual(len(fetchThreads), 1)
        self.assertIsNot(fetchThreads[0], threading.current_thread(),
                         "recovery fetched on the failing caller's thread - "
                         "a login request would block on it")

    def _joinRecoveryThread(self):
        """Recovery is asynchronous by design; tests wait for it explicitly
        instead of sleeping (deterministic, no clock)."""
        import Database.patches as patches
        thread = patches._totpRecoveryThread
        self.assertIsNotNone(thread, "no recovery thread was started")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "recovery thread did not finish")

    def test_a_single_failure_does_not_trigger_it(self):
        """Recovery is for a confirmed rotation, not for every blip."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "429 Too Many Requests"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self._fetches([]) as fetch:
            with self.assertRaises(BaseClientError):
                spotapi.client.BaseClient._get_auth_vars(instance)

        fetch.assert_not_called()

    def test_a_malformed_override_does_not_report_as_active(self):
        """_resolveTotpSecret IGNORES a malformed override (a typo must not
        take login offline), so the snapshot saying OVERRIDDEN for one is a
        lie with a cost: it also suppresses the autoRecovered flag, whose
        admin-panel note is the only prompt to pin an adopted secret before a
        restart loses it. The operator trusts their (dead) override and never
        pins."""
        from Database.patches import attemptTotpRecovery, totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        with self._fetches([(62, bytearray([1, 2, 3]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                attemptTotpRecovery()

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "62:44,55,notabyte"}):
            snapshot = totpAuthSnapshot()

        self.assertFalse(snapshot["overrideActive"])
        self.assertTrue(snapshot["autoRecovered"])
        self.assertEqual(snapshot["activeVersion"], 62)   #< what is actually in force

    def test_a_wellformed_override_reports_as_active(self):
        from Database.patches import totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "70:7,7,7"}):
            snapshot = totpAuthSnapshot()

        self.assertTrue(snapshot["overrideActive"])
        self.assertFalse(snapshot["autoRecovered"])
        self.assertEqual(snapshot["activeVersion"], 70)

    def test_the_admin_snapshot_reports_an_adopted_secret(self):
        """"We pin 61" must not be what the panel says while running on 62."""
        from Database.patches import attemptTotpRecovery, totpAuthSnapshot

        with self._fetches([(62, bytearray([1, 2, 3]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                attemptTotpRecovery()

        snapshot = totpAuthSnapshot()
        self.assertTrue(snapshot["autoRecovered"])
        self.assertEqual(snapshot["activeVersion"], 62)
        self.assertEqual(snapshot["pinnedVersion"], 61)


class TestAuthFailureHint(unittest.TestCase):
    """When Spotify rotates the secret, the only symptom is
    BaseClientError("Could not get session auth tokens") from a module nobody
    here owns - a message that names neither TOTP nor the pinned constant.
    Without a hint, the pin is undiscoverable from the failure it causes."""

    def setUp(self):
        from Database.patches import resetTotpAuthState
        resetTotpAuthState()   #< the failure streak is process-global
        self.addCleanup(resetTotpAuthState)

    def _failingClient(self):
        import spotapi.client
        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "401 Unauthorized"
        instance.client = MagicMock()
        instance.client.get.return_value = failure
        return instance

    def test_a_sustained_token_failure_points_at_the_pinned_secret(self):
        """Reported once the streak confirms it, not per attempt: an instance
        in this state retries constantly, and a line per attempt would bury
        the incident it is reporting."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import TOTP_ROTATION_CONFIRM_THRESHOLD

        client = self._failingClient()
        with self.assertLogs("Database.patches", level="ERROR") as logCapture:
            for _ in range(TOTP_ROTATION_CONFIRM_THRESHOLD):
                with self.assertRaises(BaseClientError):
                    spotapi.client.BaseClient._get_auth_vars(client)

        self.assertEqual(len(logCapture.records), 1, "one line per confirmed streak, not per attempt")
        message = " ".join(logCapture.output)
        self.assertIn("SPOTIFY_TOTP_SECRET", message)   #< the env override an operator can set
        self.assertIn("61", message)                    #< the version currently pinned

    def test_a_successful_call_is_untouched(self):
        import spotapi.client

        instance = self._failingClient()
        ok = MagicMock()
        ok.fail = False
        ok.response = {"accessToken": "tok", "clientId": "cid",
                       "accessTokenExpirationTimestampMs": "1234"}
        instance.client.get.return_value = ok

        spotapi.client.BaseClient._get_auth_vars(instance)

        self.assertEqual(instance.access_token, "tok")
        self.assertEqual(instance.client_id, "cid")
        self.assertEqual(instance.access_token_expires_at_ms, 1234.0)


class TestStreamerAtexitUnregistration(unittest.TestCase):
    """spotapi's WebsocketStreamer.__init__ registers an anonymous atexit
    closure (a print plus disconnect()) whose only reference lives inside
    atexit's registry, so nothing upstream can ever unregister it. One
    accumulated per streamer constructed - one per user per 6-hourly session
    recycle - each pinning its dead session's object graph until process exit
    and printing "Websockets closing due to program ending" there (34 lines at
    the 2026-08-04 shutdown, all but 3 for sessions long since replaced).

    The patch records what __init__ registers and drops the hook the moment
    _deliberate_close is set - the one signal (spotifyListener.signalStop)
    that every stop, rebuild and shutdown path already raises."""

    def _newStreamer(self):
        # PlayerStatus, not WebsocketStreamer: the base class is slotted (no
        # __dict__), and PlayerStatus is the only thing this app instantiates.
        return spotapi.status.PlayerStatus.__new__(spotapi.status.PlayerStatus)

    def _initRegistering(self, instance, cleanup, failure=None):
        """Run the patched __init__ with a fake original that registers
        `cleanup` the way spotapi's does, optionally raising `failure` after."""
        def fakeOriginalInit(self, *args, **kwargs):
            spotapi.websocket.atexit.register(cleanup)
            if failure is not None:
                raise failure
        with patch("Database.patches.original_websocket_streamer_init", fakeOriginalInit):
            spotapi.websocket.WebsocketStreamer.__init__(instance, MagicMock())

    def test_an_unrestorable_sigint_handler_does_not_replace_the_real_error(self):
        """signal.getsignal() answers None when the current handler was not
        installed from Python, and signal.signal(SIGINT, None) raises
        TypeError - which only ValueError was catching. That restore runs in
        the `finally` of the patched __init__, where a raised exception
        REPLACES the one being handled, so a genuine construction failure
        would have surfaced as an unrelated TypeError about a signal handler,
        sending the reader nowhere near the listener build that actually
        failed. None is also not restorable by definition, so skipping it is
        the correct action rather than a workaround."""
        instance = self._newStreamer()
        boom = RuntimeError("the real construction failure")

        with patch("Database.patches.signal.getsignal", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                self._initRegistering(instance, lambda: None, failure=boom)

        self.assertIs(caught.exception, boom)

    def test_a_restorable_handler_is_still_put_back(self):
        """Negative control: SIG_DFL/SIG_IGN and real Python handlers are
        restorable, and skipping those would leave spotapi's own SIGINT hook
        installed over the app's."""
        import signal as signalModule

        instance = self._newStreamer()
        previous = signalModule.getsignal(signalModule.SIGINT)

        with patch("Database.patches.signal.signal") as setHandler:
            self._initRegistering(instance, lambda: None)

        setHandler.assert_called_once_with(signalModule.SIGINT, previous)

    def test_spotapi_websocket_atexit_is_the_recording_shim(self):
        """The fork's module-level `atexit` name must resolve to the recorder,
        and unknown attributes must fall through to the real module."""
        import atexit as realAtexit
        from Database.patches import _SpotapiAtexitRecorder
        self.assertIsInstance(spotapi.websocket.atexit, _SpotapiAtexitRecorder)
        self.assertIs(spotapi.websocket.atexit.unregister, realAtexit.unregister)

    def test_init_swaps_the_forks_atexit_hook_for_an_owned_one(self):
        """The fork's print-based closure must be unregistered right after
        capture and replaced with the logger-based cleanup - which is what the
        instance records for the deliberate-close unregistration."""
        cleanup = lambda: None
        instance = self._newStreamer()
        with patch("Database.patches.atexit") as mockAtexit:
            self._initRegistering(instance, cleanup)
        mockAtexit.unregister.assert_called_once_with(cleanup)
        self.assertEqual(len(instance._atexitCleanups), 1)
        owned = instance._atexitCleanups[0]
        self.assertIsNot(owned, cleanup)
        registered = [call.args[0] for call in mockAtexit.register.call_args_list]
        self.assertEqual(registered, [cleanup, owned])  #< the recorder forwards the fork's, then the swap

    def test_a_deliberate_close_unregisters_the_owned_cleanup(self):
        cleanup = lambda: None
        instance = self._newStreamer()
        with patch("Database.patches.atexit") as mockAtexit:
            self._initRegistering(instance, cleanup)
            owned = instance._atexitCleanups[0]
            mockAtexit.unregister.reset_mock()  #< drop the init-time swap call
            instance._deliberate_close = True
        mockAtexit.unregister.assert_called_once_with(owned)
        self.assertEqual(instance._atexitCleanups, [])
        self.assertTrue(instance._deliberate_close)

    def test_setting_the_flag_twice_unregisters_the_hook_once(self):
        cleanup = lambda: None
        instance = self._newStreamer()
        with patch("Database.patches.atexit") as mockAtexit:
            self._initRegistering(instance, cleanup)
            mockAtexit.unregister.reset_mock()  #< drop the init-time swap call
            instance._deliberate_close = True
            instance._deliberate_close = True
        self.assertEqual(mockAtexit.unregister.call_count, 1)

    def test_clearing_the_flag_leaves_the_hook_registered(self):
        cleanup = lambda: None
        instance = self._newStreamer()
        with patch("Database.patches.atexit") as mockAtexit:
            self._initRegistering(instance, cleanup)
            owned = instance._atexitCleanups[0]
            mockAtexit.unregister.reset_mock()  #< drop the init-time swap call
            instance._deliberate_close = False
        mockAtexit.unregister.assert_not_called()
        self.assertFalse(instance._deliberate_close)
        self.assertEqual(instance._atexitCleanups, [owned])

    def _ownedHook(self, instance):
        """Construct with a registering fake init and return the owned hook."""
        with patch("Database.patches.atexit"):
            self._initRegistering(instance, lambda: None)
        return instance._atexitCleanups[0]

    def test_the_owned_hook_closes_a_leftover_socket_through_logging(self):
        instance = self._newStreamer()
        hook = self._ownedHook(instance)
        ws = MagicMock()
        instance.ws = ws
        captured = io.StringIO()
        with self.assertLogs("Database.patches", level="INFO"), \
                contextlib.redirect_stdout(captured):
            hook()
        ws.close.assert_called_once()
        self.assertIsNone(instance.ws)
        self.assertEqual(captured.getvalue(), "", "exit cleanup must log, not print")

    def test_the_owned_hook_is_silent_when_the_socket_is_already_gone(self):
        """The overwhelmingly common exit case - every deliberately closed
        session - must not add a line per dead streamer to the log."""
        instance = self._newStreamer()
        hook = self._ownedHook(instance)
        instance.ws = None
        with self.assertNoLogs("Database.patches"):
            hook()

    def test_the_owned_hook_logs_a_failed_close_instead_of_raising(self):
        """An exception out of an atexit callback would land on stderr during
        interpreter teardown - exactly the noise this exists to remove."""
        instance = self._newStreamer()
        hook = self._ownedHook(instance)
        ws = MagicMock()
        ws.close.side_effect = RuntimeError("transport already torn down")
        instance.ws = ws
        with self.assertLogs("Database.patches", level="WARNING"):
            hook()

    def test_the_flag_defaults_to_false_on_a_fresh_streamer(self):
        """Every reader does getattr(self, "_deliberate_close", False); the
        property getter must preserve that default on an instance nobody has
        flagged yet - a truthy default would make every close look deliberate
        and disable reconnects entirely."""
        self.assertFalse(self._newStreamer()._deliberate_close)

    def test_a_bare_slotted_streamer_keeps_the_signalstop_contract(self):
        """signalStop wraps `manager._deliberate_close = True` in
        try/except AttributeError for __slots__-only instances; the property
        setter must keep raising there, and the getter must still read False."""
        bare = spotapi.websocket.WebsocketStreamer.__new__(spotapi.websocket.WebsocketStreamer)
        with self.assertRaises(AttributeError):
            bare._deliberate_close = True
        self.assertFalse(bare._deliberate_close)

    def test_a_failed_init_unregisters_what_it_captured(self):
        """A streamer whose construction raises has no owner to ever set
        _deliberate_close, so anything it registered would be pinned for good."""
        cleanup = lambda: None
        instance = self._newStreamer()
        failure = RuntimeError("handshake failed")
        with patch("Database.patches.atexit") as mockAtexit:
            with self.assertRaises(RuntimeError):
                self._initRegistering(instance, cleanup, failure=failure)
        mockAtexit.unregister.assert_called_once_with(cleanup)
        mockAtexit.register.assert_called_once_with(cleanup)  #< no owned hook for a dead construction

    def test_a_failed_init_closes_the_socket_it_opened(self):
        """The atexit unregistration alone leaves the socket itself behind -
        see TestFailedStreamerConstructionClosesItsSocket for why that matters.
        Asserted here too because these are one `if not initSucceeded` branch:
        a future edit that keeps one half must not silently drop the other."""
        instance = self._newStreamer()
        instance.ws = MagicMock()
        with patch("Database.patches.atexit"):
            with self.assertRaises(RuntimeError):
                self._initRegistering(instance, lambda: None, failure=RuntimeError("handshake failed"))
        instance.ws.close.assert_called_once_with()

    def test_a_registration_outside_a_streamer_init_still_reaches_atexit(self):
        """The recorder must stay invisible to atexit use it wasn't built for -
        no open capture means plain forwarding, not a crash."""
        cleanup = lambda: None
        with patch("Database.patches.atexit") as mockAtexit:
            spotapi.websocket.atexit.register(cleanup)
        mockAtexit.register.assert_called_once_with(cleanup)

    def test_an_instance_that_cannot_record_still_gets_the_owned_hook(self):
        """A slotted instance can't store _atexitCleanups; the owned hook then
        simply stays registered for the process's life - logging instead of
        printing, but never an exception out of __init__."""
        class Slotted:
            __slots__ = ()

        cleanup = lambda: None
        instance = Slotted()
        with patch("Database.patches.atexit") as mockAtexit:
            self._initRegistering(instance, cleanup)
        mockAtexit.unregister.assert_called_once_with(cleanup)
        self.assertEqual(mockAtexit.register.call_count, 2)  #< the fork's, then the owned swap
        self.assertFalse(hasattr(instance, "_atexitCleanups"))


class TestFailedStreamerConstructionClosesItsSocket(unittest.TestCase):
    """A construction that raises after the dealer socket was opened must let
    go of it.

    spotapi's connect() runs _create_websocket() - which assigns self.ws and
    then reads the init packet - and only afterwards register_device(). So
    every failure from the init-packet read onwards leaves an open
    ClientConnection, plus its recv_events thread, on a half-built streamer
    nothing can reach: no Listener was assigned, so Listener.stop()'s
    manager.ws.close() never runs, and spotapi registers its own atexit hook
    only after connect() RETURNS.

    Nor does it heal on its own. patched_connect forces ping_interval=None
    process-wide, so there is no keepalive to time a silent peer out; the
    socket lives until the dealer hangs up, and the login loop's restart pass
    and onStaleWithBackoff are precisely what retry.

    Guarded here, at the one place that knows the construction failed, rather
    than in register_device: player_status_reconnect publishes self.ws BEFORE
    calling register_device/connect_device, and connect_device is also
    renew_state's body on the poll tick - both run against a socket that has a
    live owner, and closing it there would tear down a healthy session."""

    def _newStreamer(self):
        return spotapi.status.PlayerStatus.__new__(spotapi.status.PlayerStatus)

    def _init(self, instance, failure=None, ws=None):
        """Run the patched __init__ with a fake original that opens `ws` the
        way _create_websocket does, then optionally fails the way
        register_device does."""
        def fakeOriginalInit(self, *args, **kwargs):
            if ws is not None:
                self.ws = ws
            if failure is not None:
                raise failure
        with patch("Database.patches.original_websocket_streamer_init", fakeOriginalInit):
            spotapi.websocket.WebsocketStreamer.__init__(instance, MagicMock())

    def test_a_construction_that_raises_closes_the_socket_it_opened(self):
        ws = MagicMock()
        instance = self._newStreamer()

        with patch("Database.patches.atexit"):
            with self.assertRaises(spotapi.exceptions.WebSocketError):
                self._init(instance, failure=spotapi.exceptions.WebSocketError("Could not register device"), ws=ws)

        ws.close.assert_called_once_with()

    def test_a_successful_construction_keeps_its_socket(self):
        """The whole point of the session: closing here would end every
        listener the moment it was built."""
        ws = MagicMock()
        instance = self._newStreamer()

        with patch("Database.patches.atexit"):
            self._init(instance, ws=ws)

        ws.close.assert_not_called()

    def test_a_failure_before_the_socket_exists_is_not_an_attribute_error(self):
        """get_session/get_client_token run before _create_websocket, so `ws`
        can be absent entirely - and __init__ must still raise what it raised."""
        instance = self._newStreamer()

        with patch("Database.patches.atexit"):
            with self.assertRaises(RuntimeError):
                self._init(instance, failure=RuntimeError("could not get a session"))

    def test_a_close_that_fails_does_not_replace_the_real_failure(self):
        """The caller classifies on the construction error (see
        classifyListenerError); a teardown OSError in its place would be
        diagnosed as something else entirely."""
        ws = MagicMock()
        ws.close.side_effect = OSError("socket already gone")
        instance = self._newStreamer()

        with patch("Database.patches.atexit"):
            with self.assertRaises(spotapi.exceptions.WebSocketError):
                self._init(instance, failure=spotapi.exceptions.WebSocketError("Could not register device"), ws=ws)

        ws.close.assert_called_once_with()


class TestFailedPlayerStatusConstructionReleasesTheStreamer(unittest.TestCase):
    """The construction does not end when the base __init__ returns.

    spotapi's PlayerStatus.__init__ calls register_device() a SECOND time,
    after super().__init__(login) has already run connect() ->
    register_device(). By then patched_websocket_streamer_init has set
    initSucceeded True and swapped in the owned atexit hook, so its
    `if not initSucceeded` branch - the whole of the socket release - is
    skipped, and a raise from that second call leaves behind the dealer
    socket, its recv_events thread, the patched_keep_alive thread (whose
    _deliberate_close nothing can ever set, because the raise unwinds before
    any Listener is assigned) and the atexit closure pinning the object.

    PlayerStatus(login) is the only streamer this app constructs, so this is
    the frame that decides whether a construction leaked."""

    def _construct(self, ws=None, registerFails=None):
        """Drive a real PlayerStatus construction with the base __init__ faked
        to a successful connect, and register_device wired to fail or not."""
        captured = {}

        def fakeBaseInit(self, *args, **kwargs):
            if ws is not None:
                self.ws = ws
            captured["instance"] = self

        registerDevice = MagicMock(side_effect=registerFails)
        with patch("Database.patches.original_websocket_streamer_init", fakeBaseInit), \
             patch("Database.patches.atexit"), \
             patch.object(spotapi.status.PlayerStatus, "register_device", registerDevice):
            spotapi.status.PlayerStatus(MagicMock())
        return captured.get("instance")

    def test_a_second_register_device_that_fails_closes_the_socket(self):
        ws = MagicMock()

        with self.assertRaises(spotapi.exceptions.WebSocketError):
            self._construct(ws=ws, registerFails=spotapi.exceptions.WebSocketError("Could not register device"))

        ws.close.assert_called_once_with()

    def test_it_also_stops_the_keepalive_the_base_init_started(self):
        """Closing the socket alone is not enough: patched_keep_alive polls
        _deliberate_close, and on a half-built streamer nothing else can ever
        set it. Its setter is also what drops the atexit hook."""
        captured = {}

        def fakeBaseInit(self, *args, **kwargs):
            self.ws = MagicMock()
            captured["instance"] = self

        with patch("Database.patches.original_websocket_streamer_init", fakeBaseInit), \
             patch("Database.patches.atexit"), \
             patch.object(spotapi.status.PlayerStatus, "register_device",
                          MagicMock(side_effect=spotapi.exceptions.WebSocketError("nope"))):
            with self.assertRaises(spotapi.exceptions.WebSocketError):
                spotapi.status.PlayerStatus(MagicMock())

        self.assertTrue(captured["instance"]._deliberate_close)

    def test_a_successful_construction_keeps_its_socket_and_stays_live(self):
        """The guard must not fire on the ordinary path - this socket is the
        listener."""
        ws = MagicMock()

        instance = self._construct(ws=ws)

        ws.close.assert_not_called()
        self.assertFalse(instance._deliberate_close)


if __name__ == "__main__":
    unittest.main()


