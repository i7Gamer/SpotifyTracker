# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline HTTP contracts for the real spotapi pool, auth callbacks and hashes."""

import atexit
import base64
import gc
import json
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import spotapi
import spotapi.client as upstream
from spotapi.http.request import TLSClient
from spotapi.public import Pooler

import Database.patches as patches
import Database.Spotify.client as owned

OPERATIONS = ("getTrack", "getAlbum", "queryArtistOverview", "fetchPlaylist", "searchDesktop", "extraMutation")
POOL_CONCURRENCY = 14
NOW = 1000
TOKEN_EXPIRY_MS = 2000000
PACK_URL = "https://open.spotifycdn.com/cdn/build/web-player/web-player.test.js"
TRACK = {"data": {"trackUnion": {"uri": "spotify:track:test"}}}
STALE = {"errors": [{"message": "PersistedQueryNotFound"}]}


class HttpScript:
    def __init__(self):
        self.calls = []
        self.catalog = []
        self.version = "a"
        self.token = "access-a"
        self.clientToken = "client-a"
        self.tokenStatus = 200
        self.chunkStatus = 200
        self.onCatalog = None

    def send(self, method, url, **kwargs):
        # _send reuses kwargs on auth retry; keep the headers as sent.
        self.calls.append((method, url, dict(kwargs.get("headers", {}))))
        headers = {}
        status = 200
        if url == "https://open.spotify.com":
            config = base64.b64encode(json.dumps({"clientVersion": "test", "recaptchaWebPlayerFraudSiteKey": ""}).encode()).decode()
            body = f'<script src="{PACK_URL}"></script><script id="appServerConfig" type="text/plain">{config}</script>'
        elif url.endswith("/api/token"):
            status = self.tokenStatus
            body = {"accessToken": self.token, "clientId": "cid", "accessTokenExpirationTimestampMs": TOKEN_EXPIRY_MS}
        elif "clienttoken.spotify.com" in url:
            body = {"response_type": "RESPONSE_GRANTED_TOKEN_RESPONSE", "granted_token": {"token": self.clientToken}}
        elif url == PACK_URL:
            body = "bootstrap"
        elif url.endswith("chunk.js"):
            status = self.chunkStatus
            body = "".join(f'"{op}","{"mutation" if op == "extraMutation" else "query"}","{op}-{self.version}"' for op in OPERATIONS)
        else:
            if self.onCatalog:
                self.onCatalog()
            status, body, headers = self.catalog.pop(0) if self.catalog else (200, TRACK, {})
        encoded = body if isinstance(body, str) else json.dumps(body)
        return SimpleNamespace(status_code=status, text=encoded, headers=headers, json=lambda: body, url=url)


@pytest.fixture
def script(monkeypatch):
    patches.patch_spotapi_cache()
    patches.invalidateSpotapiHashCache()
    patches._spotapiAuthByClient.clear()
    patches.resetTotpAuthState()
    value = HttpScript()
    monkeypatch.setattr(upstream, "extract_mappings", lambda _: ({}, {}))
    monkeypatch.setattr(upstream, "combine_chunks", lambda *_: ["chunk.js"])
    monkeypatch.setattr(upstream.time, "time", lambda: NOW)
    monkeypatch.setattr(upstream, "generate_totp", lambda: ("synthetic", 61))
    monkeypatch.setattr(TLSClient, "build_request", lambda self, method, url, **kw: value.send(method, url, **kw))
    monkeypatch.setattr(owned.SPOTIFY_LIMITER, "acquire", lambda **_: True)
    monkeypatch.setattr(owned.time, "sleep", Mock())
    clients = []

    def factory():
        tls = TLSClient("chrome120", "")
        tls.cookies.set("sp_t", "synthetic-device")
        clients.append(tls)
        return tls

    value.pool = Pooler(factory=factory)
    monkeypatch.setattr(spotapi.public, "client_pool", value.pool)
    yield value
    for tls in clients:
        atexit.unregister(tls.close)
        tls.close()
    patches._spotapiAuthByClient.clear()
    patches.invalidateSpotapiHashCache()
    patches.resetTotpAuthState()


def base(script):
    return upstream.BaseClient(script.pool.get())


def test_warm_real_public_song_info_is_one_catalog_post(script):
    assert spotapi.Public.song_info("test") == TRACK
    script.calls.clear()
    assert spotapi.Public.song_info("test") == TRACK
    assert [(method, url) for method, url, _ in script.calls] == [("POST", "https://api-partner.spotify.com/pathfinder/v1/query")]


def test_bundle_and_extraction_are_shared_generic_and_ttl_checked_on_hits(script, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(patches.time, "monotonic", lambda: clock[0])
    extract = Mock(wraps=patches._extractSpotapiOperationHash)
    monkeypatch.setattr(patches, "_extractSpotapiOperationHash", extract)
    first, second = base(script), base(script)
    for op in OPERATIONS:
        assert first.part_hash(op) == f"{op}-a"
        assert second.part_hash(op) == f"{op}-a"
    assert extract.call_count == len(OPERATIONS)
    assert sum(url == PACK_URL for _, url, _ in script.calls) == 1
    script.version = "b"
    clock[0] += patches.SPOTAPI_HASH_CACHE_TTL_SECONDS
    assert second.part_hash("getAlbum") == "getAlbum-b"
    assert patches._spotapiHashByName == {"getAlbum": "getAlbum-b"}
    assert first.part_hash("getTrack") == "getTrack-b"


def test_cold_stampede_fetches_once(script):
    clients = [base(script) for _ in range(POOL_CONCURRENCY)]
    barrier = threading.Barrier(POOL_CONCURRENCY)

    def getHash(client):
        barrier.wait()
        return client.part_hash("getTrack")

    with ThreadPoolExecutor(max_workers=POOL_CONCURRENCY) as executor:
        assert list(executor.map(getHash, clients)) == ["getTrack-a"] * POOL_CONCURRENCY
    assert sum(url == PACK_URL for _, url, _ in script.calls) == 1


def test_generation_invalidation_does_not_discard_new_bundle(script):
    client = base(script)
    client.part_hash("getTrack")
    old = patches._spotapiBundleGeneration
    assert patches.invalidateSpotapiHashCache(old)
    script.version = "b"
    client.part_hash("getTrack")
    assert not patches.invalidateSpotapiHashCache(old)
    assert client.part_hash("getTrack") == "getTrack-b"


def test_join_publishes_once_and_failure_leaves_no_partial_cache(script, monkeypatch):
    client = base(script)
    assignments = []
    original = upstream.BaseClient.__setattr__

    def record(self, name, value):
        if self is client and name == "raw_hashes":
            assignments.append(value)
        return original(self, name, value)

    monkeypatch.setattr(upstream.BaseClient, "__setattr__", record)
    monkeypatch.setattr(upstream, "combine_chunks", lambda *_: ["one-chunk.js", "two-chunk.js"])
    client.get_sha256_hash()
    assert len(assignments) == 1
    assert client.raw_hashes.startswith("bootstrap")
    assert client.raw_hashes.count('"getTrack"') == 2
    patches.invalidateSpotapiHashCache()
    script.chunkStatus = 503
    with pytest.raises(spotapi.exceptions.BaseClientError):
        client.get_sha256_hash()
    assert patches._spotapiBundle is None
    assert patches._spotapiHashByName == {}
    assert patches._spotapiJsPack is None


def test_installer_is_idempotent_per_method(script):
    methods = ("get_sha256_hash", "part_hash", "_auth_rule", "_handle_auth_failure")
    before = [getattr(upstream.BaseClient, name) for name in methods]
    patches.patch_spotapi_cache()
    patches.patch_totp_secret()
    patches.patch_spotapi_cache()
    assert [getattr(upstream.BaseClient, name) for name in methods] == before


def test_auth_same_transport_reused_different_transport_isolated(script):
    first = base(script)
    assert first._auth_rule({})["headers"]["Authorization"] == "Bearer access-a"
    script.token = "access-b"
    second = upstream.BaseClient(first.client)
    assert second._auth_rule({})["headers"]["Authorization"] == "Bearer access-a"
    other = base(script)
    assert other._auth_rule({})["headers"]["Authorization"] == "Bearer access-b"


def test_real_expiry_refresh_and_direct_session_state_is_not_overwritten(script, monkeypatch):
    first = base(script)
    first._auth_rule({})
    script.token = "access-b"
    monkeypatch.setattr(upstream.time, "time", lambda: TOKEN_EXPIRY_MS / 1000)
    second = upstream.BaseClient(first.client)
    assert second._auth_rule({})["headers"]["Authorization"] == "Bearer access-b"
    script.token = "direct-session"
    third = upstream.BaseClient(first.client)
    third.get_session()
    monkeypatch.setattr(upstream.time, "time", lambda: NOW)
    assert third._auth_rule({})["headers"]["Authorization"] == "Bearer direct-session"


@pytest.mark.parametrize("status,headers,field,fresh", [
    (400, {"Client-Token-Error": "INVALID_CLIENTTOKEN"}, "Client-Token", "client-b"),
    (401, {}, "Authorization", "Bearer access-b"),
])
def test_real_send_auth_retry_and_next_base_use_fresh_tokens(script, status, headers, field, fresh):
    spotapi.Public.song_info("test")
    script.catalog = [(status, {}, headers), (200, TRACK, {})]
    script.token, script.clientToken = "access-b", "client-b"
    script.calls.clear()
    spotapi.Public.song_info("test")
    spotapi.Public.song_info("test")
    sent = [h[field] for _, url, h in script.calls if "pathfinder" in url]
    assert sent[-2:] == [fresh, fresh]
    assert sent[0] != fresh


def test_failed_auth_refresh_leaves_cache_empty(script):
    spotapi.Public.song_info("test")
    tls = script.pool.queue[0]
    script.catalog = [(401, {}, {})]
    script.tokenStatus = 503
    with pytest.raises(spotapi.exceptions.BaseClientError):
        spotapi.Public.song_info("test")
    assert id(tls) not in patches._spotapiAuthByClient


def test_weak_identity_is_checked_and_does_not_retain_client(script):
    first, second = base(script), base(script)
    first._auth_rule({})
    patches._spotapiAuthByClient[id(second.client)] = patches._spotapiAuthByClient[id(first.client)]
    script.token = "separate"
    assert second._auth_rule({})["headers"]["Authorization"] == "Bearer separate"
    tls = second.client
    key = id(tls)
    # The fixture owns clients; prove the cache's entry itself is weak.
    assert patches._spotapiAuthByClient[key][0]() is tls
    detached = SimpleNamespace()
    class WeakClient:
        pass
    detached.client = WeakClient()
    for field in patches._SPOTAPI_AUTH_FIELDS:
        setattr(detached, field, "test")
    patches._publishSpotapiAuth(detached)
    key = id(detached.client)
    ref = weakref.ref(detached.client)
    del detached
    gc.collect()
    assert ref() is None
    assert key not in patches._spotapiAuthByClient


@pytest.mark.parametrize("payload,expected", [
    (None, False), ([], False), ({"errors": {}}, False),
    ({"errors": [None, {"message": "unrelated", "extensions": []}]}, False),
    ({"errors": [{"extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"}}]}, True),
    ({"errors": [{"message": "Persisted Query Not Found"}]}, True),
])
def test_persisted_query_marker_shapes(payload, expected):
    assert owned._isPersistedQueryError(payload) is expected


@pytest.mark.parametrize("status", [200, 400])
def test_persisted_query_response_refreshes_once_before_incomplete_handling(script, status):
    script.catalog = [(status, STALE, {}), (200, TRACK, {})]
    assert owned.getTrackInfoWithRetry("test") == TRACK["data"]["trackUnion"]
    assert sum(url == PACK_URL for _, url, _ in script.calls) == 2
    owned.time.sleep.assert_not_called()


def test_persisted_error_on_final_transport_attempt_gets_extra_retry(script):
    script.catalog = [(503, {}, {}), (503, {}, {}), (200, STALE, {}), (200, TRACK, {})]
    assert owned.getTrackInfoWithRetry("test") == TRACK["data"]["trackUnion"]
    assert [c.args[0] for c in owned.time.sleep.call_args_list] == [1, 2]
    assert sum(url == PACK_URL for _, url, _ in script.calls) == 2


def test_hash_retry_is_bounded_and_separate_from_other_budgets(script):
    script.catalog = [(503, {}, {}), (200, {"data": None}, {}), (200, STALE, {}), (503, {}, {}), (200, TRACK, {})]
    assert owned.getTrackInfoWithRetry("test") == TRACK["data"]["trackUnion"]
    assert [c.args[0] for c in owned.time.sleep.call_args_list] == [1, owned.INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS, 2]
    script.catalog = [(200, STALE, {})] * 10
    with pytest.raises(spotapi.exceptions.SongError):
        owned.getTrackInfoWithRetry("test")
    assert sum(url == PACK_URL for _, url, _ in script.calls) == 3


@pytest.mark.parametrize("failure", [
    spotapi.exceptions.SongError("Invalid JSON"),
    spotapi.exceptions.SongError("failed", error="Status Code: 503, Response: PersistedQueryNotFound"),
    spotapi.exceptions.BaseClientError("session", error="Status Code: 400, Response: PersistedQueryNotFound"),
    owned.SpotifyLocallyRateLimitedError("local"),
])
def test_unrelated_failures_never_invalidate(script, monkeypatch, failure):
    base(script).part_hash("getTrack")
    generation = patches._spotapiBundleGeneration
    monkeypatch.setattr(spotapi.Public, "song_info", Mock(side_effect=failure))
    with pytest.raises(type(failure)):
        owned.getTrackInfoWithRetry("test", max_retries=1)
    assert patches._spotapiBundleGeneration == generation


def test_late_hash_failure_does_not_discard_new_generation(script):
    def refreshElsewhere():
        script.onCatalog = None
        old = patches._spotapiBundleGeneration
        def refresh():
            patches.invalidateSpotapiHashCache(old)
            base(script).part_hash("getTrack")
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(refresh).result()
    script.onCatalog = refreshElsewhere
    script.catalog = [(200, STALE, {}), (200, TRACK, {})]
    assert owned.getTrackInfoWithRetry("test") == TRACK["data"]["trackUnion"]
    assert sum(url == PACK_URL for _, url, _ in script.calls) == 2
