# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Token endpoint evidence, through the installed spotapi mint wrapper."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import spotapi.client as upstream
from spotapi.exceptions import BaseClientError, RequestError
from spotapi.http.data import Response

import Database.patches as patches

FAST_FAILURE_TIMES = (0, 1, 3)
SLOW_FAILURE_TIMES = (0, 60, 121)
SPARSE_REJECTION_TIMES = (0, 3600, 7200)
# Anonymous invalid-version and invalid-code probes on 2026-09-22 returned
# HTTP 400 with these fields. Trace data and policy text are not needed here.
REJECTIONS = (
    {"totpVerExpired": "error", "error": {"code": 400, "message": "Unauthorized request"}},
    {"error": {"code": 400, "message": "Unauthorized request"}},
)


@pytest.fixture
def mint(monkeypatch):
    patches.patch_totp_secret()
    patches.resetTotpAuthState()
    clock = [0]
    monkeypatch.setattr(patches.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(upstream, "generate_totp", lambda: ("synthetic", 61))
    recovery = Mock()
    monkeypatch.setattr(patches, "_startTotpRecoveryInBackground", recovery)
    client = upstream.BaseClient.__new__(upstream.BaseClient)
    client.client = SimpleNamespace(get=Mock())

    def run(status=200, body=None, error=None):
        client.access_token = upstream._Undefined
        client.client_id = upstream._Undefined
        client.client.get.side_effect = error
        client.client.get.return_value = Response(raw=None, status_code=status, response=body or {
            "accessToken": "synthetic", "clientId": "synthetic", "accessTokenExpirationTimestampMs": 0,
        })
        client._get_auth_vars()

    yield SimpleNamespace(run=run, clock=clock, recovery=recovery, client=client)
    patches.resetTotpAuthState()


@pytest.mark.parametrize("times", [FAST_FAILURE_TIMES, SLOW_FAILURE_TIMES])
@pytest.mark.parametrize("status", [429, 500, 503, 504])
def test_transport_outages_do_not_trigger_rotation(mint, caplog, times, status):
    for when in times:
        mint.clock[0] = when
        with pytest.raises(BaseClientError):
            mint.run(status, {"error": "unavailable"})
    snapshot = patches.totpAuthSnapshot()
    assert snapshot["transportFailures"] == len(times)
    assert snapshot["consecutiveFailures"] == 0
    assert not snapshot["suspectedRotation"]
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    mint.recovery.assert_not_called()


@pytest.mark.parametrize("times", [FAST_FAILURE_TIMES, SPARSE_REJECTION_TIMES])
@pytest.mark.parametrize("body", REJECTIONS)
def test_real_rejection_shape_triggers_count_threshold_without_time_gate(mint, caplog, times, body):
    for when in times:
        mint.clock[0] = when
        with pytest.raises(BaseClientError):
            mint.run(400, body)
    snapshot = patches.totpAuthSnapshot()
    assert snapshot["consecutiveFailures"] == len(times)
    assert snapshot["transportFailures"] == 0
    assert snapshot["suspectedRotation"]
    assert len([record for record in caplog.records if record.levelname == "ERROR"]) == 1
    mint.recovery.assert_called_once()


def test_network_exception_and_unknown_http_failures_are_not_rejections(mint):
    failures = (
        RequestError("Request kept failing", error="connection timeout"),
        BaseClientError("token failed", error=None),
        BaseClientError("token failed", error="Status Code: 400, Response: not JSON"),
        BaseClientError("token failed", error='Status Code: 400, Response: {"error":"bad input"}'),
        BaseClientError("token failed", error="Status Code: 401, Response: unauthorized"),
    )
    for failure in failures:
        with pytest.raises(type(failure)):
            mint.run(error=failure)
    assert patches.totpAuthSnapshot()["transportFailures"] == len(failures)
    assert patches.totpAuthSnapshot()["consecutiveFailures"] == 0
    mint.recovery.assert_not_called()


def test_real_mint_resets_both_failures_but_cached_call_preserves_evidence(mint):
    with pytest.raises(BaseClientError):
        mint.run(400, REJECTIONS[0])
    with pytest.raises(RequestError):
        mint.run(error=RequestError("timeout"))
    mint.client.access_token = "cached"
    mint.client.client_id = "cached"
    mint.client._get_auth_vars()
    snapshot = patches.totpAuthSnapshot()
    assert snapshot["consecutiveFailures"] == snapshot["transportFailures"] == 1
    assert snapshot["secondsSinceLastMint"] is None
    mint.clock[0] = 10
    mint.run()
    mint.clock[0] = 15
    snapshot = patches.totpAuthSnapshot()
    assert snapshot["consecutiveFailures"] == snapshot["transportFailures"] == 0
    assert snapshot["secondsSinceLastMint"] == 5


@pytest.mark.parametrize("detail,expected", [
    ('Status Code: 400, Response: {"totpVerExpired":"error"}', True),
    ('Status Code: 400, Response: {"error":{"message":"Unauthorized request"}}', True),
    ('Status Code: 503, Response: {"totpVerExpired":"error"}', False),
    ('Status Code: 400, Response: ["Unauthorized request"]', False),
    ('Status Code: 400, Response: {"error":[]}', False),
])
def test_only_explicit_token_rejection_evidence_is_classified(detail, expected):
    assert patches._isTotpRejection(BaseClientError("token", error=detail)) is expected
