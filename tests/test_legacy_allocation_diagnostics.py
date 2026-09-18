# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Allocation diagnoses every legacy name skipped without reassociating it."""
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from Database.repository import Repository
from test_user_registry import _BareHost

NEW_EMAIL = "alice@new.example"
EXISTING_EMAIL = "owner@example.test"
LOGGER_NAME = "dashboard.user_registry"


@pytest.fixture
def registry(tmp_path):
    repo = Repository(tmp_path / "registry.db")
    host = _BareHost(repo)
    host._ensureAdminExists = MagicMock()
    yield host
    repo.connectionManager.close()


@pytest.mark.parametrize("rows, expected_name, warned_names", [
    ([("alice", None), ("alice_1", None)], "alice_2", ["alice", "alice_1"]),
    ([("alice", EXISTING_EMAIL), ("alice_1", None)], "alice_2", ["alice_1"]),
    ([("Alice", None), ("ALICE_1", None)], "alice_2", ["Alice", "ALICE_1"]),
    ([("alice", EXISTING_EMAIL), ("alice_1", "second@example.test")], "alice_2", []),
    ([], "alice", []),
])
def test_reports_each_bypassed_legacy_row(registry, caplog, rows, expected_name, warned_names):
    for username, email in rows:
        registry.repo.upsertUser(username, email)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert registry.get_or_create_user(NEW_EMAIL) == expected_name
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER_NAME]
    assert messages == [
        f"Legacy account {name} has no associated email; allocated new account {expected_name}. "
        "See docs/recover-a-legacy-account.md before reassociating either account."
        for name in warned_names
    ]
    assert all(NEW_EMAIL not in message for message in messages)
    for username, email in rows:
        assert registry.repo.getEmailForUsername(username) == email
    registry._ensureAdminExists.assert_called_once()


@pytest.mark.parametrize("email", [None, EXISTING_EMAIL])
def test_cached_readonly_candidate_is_diagnosed(registry, caplog, email):
    registry.repo.upsertUser("alice", email)
    with patch("dashboard.user_registry.Database", return_value=MagicMock()):
        registry._getReadOnlyUserDb("alice")
    assert "alice" not in registry._activatedUsers
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert registry.get_or_create_user(NEW_EMAIL) == "alice_1"
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER_NAME]
    assert bool(messages) is (email is None)
    if messages:
        assert "Legacy account alice " in messages[0]
    assert registry.repo.getEmailForUsername("alice") == email


def test_diagnostics_run_outside_session_lock_and_continue_after_failure(registry, caplog):
    registry.repo.upsertUser("alice", None)
    registry.repo.upsertUser("alice_1", None)
    registry._session_lock = threading.Lock()
    original = registry.repo.getNullEmailUsernameNoCase
    checked = []

    def lookup(candidate):
        assert not registry._session_lock.locked()
        checked.append(candidate)
        if candidate == "alice":
            raise RuntimeError("synthetic diagnostic failure")
        return original(candidate)

    with patch.object(registry.repo, "getNullEmailUsernameNoCase", side_effect=lookup), \
            caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert registry.get_or_create_user(NEW_EMAIL) == "alice_2"
    assert checked == ["alice", "alice_1"]
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER_NAME]
    assert any("Could not check" in message for message in messages)
    assert any("Legacy account alice_1 " in message for message in messages)
    registry._ensureAdminExists.assert_called_once()


@pytest.mark.parametrize("existing", [False, True])
def test_no_diagnostic_queries_without_rejected_names(registry, existing):
    if existing:
        registry.repo.upsertUser("alice", NEW_EMAIL)
    with patch.object(registry.repo, "getNullEmailUsernameNoCase") as lookup:
        assert registry.get_or_create_user(NEW_EMAIL) == "alice"
    lookup.assert_not_called()
