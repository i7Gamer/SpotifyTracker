# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import ssl

import pytest
from unittest.mock import patch, MagicMock

from config import SMTP_SKIP_TLS_VERIFY_ENV_VAR

from Database.repository import Repository
from services.email_service import (
    get_smtp_config,
    save_smtp_config,
    get_instance_public_url,
    save_instance_public_url,
    build_email_message,
    send_email_notification,
    deliver_email_notification,
    send_test_email,
    _render_event_template,
    _send_smtp_message,
    EMAIL_SENT,
    EMAIL_SKIPPED,
    EMAIL_FAILED,
    _isValidFromEmail,
    _isValidPublicUrl,
)
from Database.queries.email_queries import (
    EVENT_INVALID_COOKIES,
    EVENT_API_KEY_FAILED,
    EVENT_SHARE_REQUEST,
    EVENT_MILESTONE_REACHED,
)


def test_smtp_config_save_and_get():
    repo = Repository()

    # Initial state (defaults)
    config = get_smtp_config(repo)
    assert config["enabled"] is False
    assert config["host"] == ""
    assert config["port"] == 587
    assert config["encryption"] == "tls"

    # Save new settings
    save_smtp_config(
        repo=repo,
        enabled=True,
        host="smtp.example.com",
        port=465,
        encryption="ssl",
        user="testuser@example.com",
        password="secretpassword123",
        from_email="noreply@example.com",
        from_name="SpotifyTracker",
    )

    config_after = get_smtp_config(repo)
    assert config_after["enabled"] is True
    assert config_after["host"] == "smtp.example.com"
    assert config_after["port"] == 465
    assert config_after["encryption"] == "ssl"
    assert config_after["user"] == "testuser@example.com"
    assert config_after["from_email"] == "noreply@example.com"
    assert config_after["from_name"] == "SpotifyTracker"


def test_instance_public_url_save_and_get():
    repo = Repository()
    assert get_instance_public_url(repo) == ""

    save_instance_public_url(repo, "https://tracker.example.com")
    assert get_instance_public_url(repo) == "https://tracker.example.com"


def test_instance_public_url_strips_trailing_slash():
    repo = Repository()
    save_instance_public_url(repo, "https://tracker.example.com/ ")
    assert get_instance_public_url(repo) == "https://tracker.example.com"


class TestIsValidFromEmail:
    """Empty means "not configured yet" (the same shape as the public URL
    setting), and once non-empty must have exactly one "@" with content on
    both sides - not full RFC validation, just enough to reject the finding's
    case (UT-16: a from-address with no "@" saved unchecked)."""

    def test_empty_is_valid(self):
        assert _isValidFromEmail("") is True

    def test_plain_address_is_valid(self):
        assert _isValidFromEmail("noreply@example.com") is True

    def test_no_at_sign_is_invalid(self):
        assert _isValidFromEmail("notanemail") is False

    def test_two_at_signs_is_invalid(self):
        assert _isValidFromEmail("a@b@example.com") is False

    def test_empty_local_part_is_invalid(self):
        assert _isValidFromEmail("@example.com") is False

    def test_empty_domain_part_is_invalid(self):
        assert _isValidFromEmail("noreply@") is False


class TestIsValidPublicUrl:
    """Empty means "no link configured"; a non-empty value must be an
    http(s) URL - rejects a javascript: (or any other) scheme."""

    def test_empty_is_valid(self):
        assert _isValidPublicUrl("") is True

    def test_http_is_valid(self):
        assert _isValidPublicUrl("http://tracker.example.com") is True

    def test_https_is_valid(self):
        assert _isValidPublicUrl("https://tracker.example.com") is True

    def test_javascript_scheme_is_invalid(self):
        assert _isValidPublicUrl("javascript:alert(1)") is False

    def test_bare_domain_is_invalid(self):
        assert _isValidPublicUrl("tracker.example.com") is False


def test_build_email_message():
    msg = build_email_message(
        to_email="user@example.com",
        subject="Test Subject",
        text_body="Hello Plain Text",
        html_body="<h1>Hello HTML</h1>",
        from_email="noreply@example.com",
        from_name="SpotifyTracker",
    )

    assert msg["To"] == "user@example.com"
    assert msg["Subject"] == "Test Subject"
    assert "SpotifyTracker <noreply@example.com>" in msg["From"]


@patch("smtplib.SMTP")
@patch("smtplib.SMTP_SSL")
def test_send_email_notification_disabled_globally(mock_ssl, mock_smtp):
    repo = Repository()
    username = "user_notif_disabled"
    repo.upsertUser(username, "user@example.com")

    # Global notifications disabled
    save_smtp_config(repo, enabled=False, host="smtp.example.com", port=587, encryption="tls", user="", password="", from_email="n@e.com", from_name="N")

    sent = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent is False
    mock_smtp.assert_not_called()
    mock_ssl.assert_not_called()


@patch("smtplib.SMTP")
def test_send_email_notification_success(mock_smtp_class):
    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__.return_value = mock_server

    repo = Repository()
    username = "user_notif_success"
    repo.upsertUser(username, "user_success@example.com")

    save_smtp_config(
        repo=repo,
        enabled=True,
        host="smtp.example.com",
        port=587,
        encryption="tls",
        user="smtp_user",
        password="smtp_password",
        from_email="noreply@example.com",
        from_name="SpotifyTracker",
    )

    sent = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent is True
    assert mock_server.send_message.called is True

    # Cooldown should prevent immediate second email
    sent_again = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent_again is False


@patch("smtplib.SMTP")
def test_send_email_notification_includes_configured_instance_link(mock_smtp_class):
    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__.return_value = mock_server

    repo = Repository()
    username = "user_notif_link"
    repo.upsertUser(username, "user_link@example.com")

    save_smtp_config(
        repo=repo, enabled=True, host="smtp.example.com", port=587, encryption="tls",
        user="smtp_user", password="smtp_password", from_email="noreply@example.com", from_name="SpotifyTracker",
    )
    save_instance_public_url(repo, "https://tracker.example.com")

    sent = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent is True

    msg = mock_server.send_message.call_args[0][0]
    htmlPart = next(part for part in msg.walk() if part.get_content_type() == "text/html")
    html = htmlPart.get_payload(decode=True).decode("utf-8")
    assert 'href="https://tracker.example.com/login"' in html


@patch("smtplib.SMTP")
def test_send_test_email(mock_smtp_class):
    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__.return_value = mock_server

    repo = Repository()
    save_smtp_config(
        repo=repo,
        enabled=True,
        host="smtp.example.com",
        port=587,
        encryption="tls",
        user="smtp_user",
        password="smtp_password",
        from_email="noreply@example.com",
        from_name="SpotifyTracker",
    )

    result, err = send_test_email(repo, "admin@example.com")
    assert result is True
    assert err is None
    assert mock_server.send_message.called is True


class TestSmtpTlsVerification:
    """_send_smtp_message used to hand smtplib no SSL context, and the one
    smtplib builds for itself (ssl._create_stdlib_context) verifies nothing -
    CERT_NONE and no hostname check, measured on the 3.14 runtime this ships
    on. Every SMTP credential and message then went to whoever answered on
    that port (2026-09-07 review, item 3)."""

    def _config(self, encryption):
        return {"host": "smtp.example.com", "port": 465, "encryption": encryption,
                "user": "smtp_user", "password": "smtp_password"}

    @patch("smtplib.SMTP_SSL")
    def test_implicit_tls_verifies_the_server(self, mock_ssl):
        ok, err = _send_smtp_message(self._config("ssl"), MagicMock())

        assert (ok, err) == (True, None)
        context = mock_ssl.call_args.kwargs["context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    @patch("smtplib.SMTP")
    def test_starttls_verifies_the_server(self, mock_smtp):
        server = mock_smtp.return_value.__enter__.return_value

        ok, _ = _send_smtp_message(self._config("tls"), MagicMock())

        assert ok is True
        context = server.starttls.call_args.kwargs["context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    @patch("smtplib.SMTP")
    def test_no_encryption_never_starts_tls(self, mock_smtp):
        server = mock_smtp.return_value.__enter__.return_value

        _send_smtp_message(self._config("none"), MagicMock())

        server.starttls.assert_not_called()

    @patch("smtplib.SMTP_SSL")
    def test_the_opt_out_keeps_a_self_signed_relay_working(self, mock_ssl, monkeypatch, caplog):
        """A self-hoster on a relay with a self-signed certificate would
        otherwise lose email on upgrade - the opt-out has to exist, and has to
        say in the log that it is on."""
        monkeypatch.setenv(SMTP_SKIP_TLS_VERIFY_ENV_VAR, "1")

        with caplog.at_level("WARNING", logger="services.email_service"):
            _send_smtp_message(self._config("ssl"), MagicMock())

        context = mock_ssl.call_args.kwargs["context"]
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False
        assert SMTP_SKIP_TLS_VERIFY_ENV_VAR in caplog.text

    @patch("smtplib.SMTP_SSL")
    def test_a_false_opt_out_still_verifies(self, mock_ssl, monkeypatch):
        monkeypatch.setenv(SMTP_SKIP_TLS_VERIFY_ENV_VAR, "0")

        _send_smtp_message(self._config("ssl"), MagicMock())

        assert mock_ssl.call_args.kwargs["context"].verify_mode == ssl.CERT_REQUIRED


class TestDeliverOutcome:
    """The worker retries a FAILED send and nothing else, so the service has to
    say which of its Falses was a failure: a disabled/opted-out/cooling-down
    send is a decision, not a fault (2026-09-07 review, item 13)."""

    def _configuredRepo(self, username, enabled=True):
        repo = Repository()
        repo.upsertUser(username, f"{username}@example.com")
        save_smtp_config(repo=repo, enabled=enabled, host="smtp.example.com", port=587,
                         encryption="tls", user="u", password="p",
                         from_email="noreply@example.com", from_name="N")
        return repo

    @patch("smtplib.SMTP")
    def test_a_delivered_mail_is_sent(self, mock_smtp):
        repo = self._configuredRepo("deliver_ok")

        assert deliver_email_notification(repo, "deliver_ok", EVENT_INVALID_COOKIES) == EMAIL_SENT

    @patch("smtplib.SMTP")
    def test_an_smtp_error_is_a_failure(self, mock_smtp):
        mock_smtp.return_value.__enter__.side_effect = OSError("connection refused")
        repo = self._configuredRepo("deliver_fail")

        assert deliver_email_notification(repo, "deliver_fail", EVENT_INVALID_COOKIES) == EMAIL_FAILED
        assert send_email_notification(repo, "deliver_fail", EVENT_INVALID_COOKIES) is False

    @patch("smtplib.SMTP")
    def test_notifications_off_is_a_skip_not_a_failure(self, mock_smtp):
        repo = self._configuredRepo("deliver_off", enabled=False)

        assert deliver_email_notification(repo, "deliver_off", EVENT_INVALID_COOKIES) == EMAIL_SKIPPED
        mock_smtp.assert_not_called()

    @patch("smtplib.SMTP")
    def test_the_cooldown_is_a_skip_not_a_failure(self, mock_smtp):
        repo = self._configuredRepo("deliver_cool")
        assert deliver_email_notification(repo, "deliver_cool", EVENT_INVALID_COOKIES) == EMAIL_SENT

        assert deliver_email_notification(repo, "deliver_cool", EVENT_INVALID_COOKIES) == EMAIL_SKIPPED


class TestRenderEventTemplate:
    """_render_event_template's html_body is shown as-is in an email client -
    every link it offers must actually go somewhere, since there is no way
    for a recipient to retry a dead button."""

    def test_invalid_cookies_has_no_dead_link(self):
        subject, text_body, html_body = _render_event_template(EVENT_INVALID_COOKIES, "alice", {})
        assert "alice" in text_body
        assert "alice" in html_body
        assert 'href="#"' not in html_body
        assert "<a " not in html_body   #< no link at all beats a dead one

    def test_api_key_failed_mentions_username(self):
        subject, text_body, html_body = _render_event_template(EVENT_API_KEY_FAILED, "alice", {})
        assert "alice" in text_body
        assert "alice" in html_body
        assert 'href="#"' not in html_body

    def test_share_request_mentions_requester(self):
        subject, text_body, html_body = _render_event_template(
            EVENT_SHARE_REQUEST, "alice", {"requester_username": "bob"})
        assert "bob" in subject
        assert "bob" in text_body
        assert "bob" in html_body
        assert 'href="#"' not in html_body

    def test_share_request_defaults_requester_when_missing_from_context(self):
        subject, text_body, html_body = _render_event_template(EVENT_SHARE_REQUEST, "alice", {})
        assert "A user" in subject

    def test_unknown_event_type_falls_back_gracefully(self):
        subject, text_body, html_body = _render_event_template("some_future_event", "alice", {})
        assert "alice" in text_body
        assert "alice" in html_body
        assert "some_future_event" in subject

    def test_names_are_escaped_in_every_html_body(self):
        """Usernames are sanitized to [A-Za-z0-9_-] at creation today, so no
        metacharacter reaches these f-strings - but the escaping decision was
        made nowhere (while _ctaButton right beside them escapes its link),
        and a future switch to display names (which allow spaces and more)
        would have turned the interpolation live. The text bodies are
        text/plain and stay raw."""
        hostile = "<img src=x onerror=alert(1)>"
        for event, context in ((EVENT_INVALID_COOKIES, {}),
                               (EVENT_API_KEY_FAILED, {}),
                               (EVENT_SHARE_REQUEST, {}),
                               ("some_future_event", {})):
            _subject, _text, html_body = _render_event_template(event, hostile, context)
            assert "<img" not in html_body, f"{event}: username reached the HTML unescaped"
            assert "&lt;img" in html_body

        _subject, _text, html_body = _render_event_template(
            EVENT_SHARE_REQUEST, "alice", {"requester_username": hostile})
        assert "<img" not in html_body, "requester reached the HTML unescaped"
        assert "&lt;img" in html_body

    def test_invalid_cookies_links_to_login_when_base_url_configured(self):
        _subject, text_body, html_body = _render_event_template(
            EVENT_INVALID_COOKIES, "alice", {}, base_url="https://tracker.example.com")
        assert 'href="https://tracker.example.com/login"' in html_body
        assert "https://tracker.example.com/login" in text_body

    def test_api_key_failed_links_to_connections_when_base_url_configured(self):
        _subject, text_body, html_body = _render_event_template(
            EVENT_API_KEY_FAILED, "alice", {}, base_url="https://tracker.example.com")
        assert 'href="https://tracker.example.com/profile/connections"' in html_body
        assert "https://tracker.example.com/profile/connections" in text_body

    def test_share_request_links_to_sharing_when_base_url_configured(self):
        _subject, text_body, html_body = _render_event_template(
            EVENT_SHARE_REQUEST, "alice", {"requester_username": "bob"}, base_url="https://tracker.example.com")
        assert 'href="https://tracker.example.com/profile/sharing"' in html_body
        assert "https://tracker.example.com/profile/sharing" in text_body

    def test_base_url_is_html_escaped_in_href(self):
        """The base URL is admin-supplied and stored, not hardcoded - it must
        not be able to break out of the href attribute it's interpolated into."""
        _subject, _text_body, html_body = _render_event_template(
            EVENT_INVALID_COOKIES, "alice", {}, base_url='https://evil.example.com"onmouseover="alert(1)')
        assert 'onmouseover="alert(1)"' not in html_body
        assert "&quot;" in html_body

    def test_trailing_slash_in_base_url_does_not_double_up(self):
        _subject, _text_body, html_body = _render_event_template(
            EVENT_INVALID_COOKIES, "alice", {}, base_url="https://tracker.example.com/")
        assert "//login" not in html_body

    def test_milestone_reached_one_milestone(self):
        subject, text_body, html_body = _render_event_template(
            EVENT_MILESTONE_REACHED, "alice",
            {"milestones": [{"icon": "🎧", "label": "1,000 lifetime plays"}]})
        assert "1,000 lifetime plays" in subject
        assert "1,000 lifetime plays" in text_body
        assert "1,000 lifetime plays" in html_body
        assert 'href="#"' not in html_body

    def test_milestone_reached_several_milestones(self):
        milestones = [
            {"icon": "🎧", "label": "1,000 lifetime plays"},
            {"icon": "🔥", "label": "7-day listening streak"},
        ]
        subject, text_body, html_body = _render_event_template(
            EVENT_MILESTONE_REACHED, "alice", {"milestones": milestones})
        assert "2" in subject
        for m in milestones:
            assert m["label"] in text_body
            assert m["label"] in html_body

    def test_milestone_reached_label_escaped_in_html_only(self):
        milestones = [{"icon": "👑", "label": "New #1 artist: <script>alert(1)</script>"}]
        _subject, text_body, html_body = _render_event_template(
            EVENT_MILESTONE_REACHED, "alice", {"milestones": milestones})
        assert "<script>alert(1)</script>" in text_body   #< text/plain: raw
        assert "<script>" not in html_body
        assert "&lt;script&gt;" in html_body

    def test_milestone_reached_link_present_with_base_url(self):
        _subject, text_body, html_body = _render_event_template(
            EVENT_MILESTONE_REACHED, "alice",
            {"milestones": [{"icon": "🎧", "label": "1,000 lifetime plays"}]},
            base_url="https://tracker.example.com")
        assert 'href="https://tracker.example.com/#milestones"' in html_body
        assert "https://tracker.example.com/#milestones" in text_body

    def test_milestone_reached_link_absent_without_base_url(self):
        _subject, _text_body, html_body = _render_event_template(
            EVENT_MILESTONE_REACHED, "alice",
            {"milestones": [{"icon": "🎧", "label": "1,000 lifetime plays"}]})
        assert 'href="#"' not in html_body
        assert "<a " not in html_body

    def test_milestone_reached_empty_list_has_sane_copy(self):
        subject, text_body, html_body = _render_event_template(
            EVENT_MILESTONE_REACHED, "alice", {"milestones": []})
        assert "alice" in text_body
        assert "alice" in html_body
        assert subject   #< no crash, non-empty subject

    def test_milestone_reached_missing_context_key_has_sane_copy(self):
        subject, text_body, html_body = _render_event_template(EVENT_MILESTONE_REACHED, "alice", {})
        assert "alice" in text_body
        assert "alice" in html_body
        assert subject


class TestSmtpTimeout:
    """A misconfigured or black-holed SMTP host must not pin the caller: an
    admin's /admin/test_email holds a request thread, and the notification path
    runs inside the email worker. smtplib's default is no timeout at all, so
    the value is the only thing bounding either - named rather than repeated
    inline across the SSL and STARTTLS branches."""

    @patch("smtplib.SMTP")
    def test_starttls_connection_passes_the_named_timeout(self, mock_smtp_class):
        from services.email_service import SMTP_TIMEOUT_SECONDS

        mock_smtp_class.return_value.__enter__.return_value = MagicMock()
        repo = Repository()
        save_smtp_config(
            repo=repo, enabled=True, host="smtp.example.com", port=587, encryption="tls",
            user="smtp_user", password="smtp_password",
            from_email="noreply@example.com", from_name="SpotifyTracker",
        )

        result, err = send_test_email(repo, "admin@example.com")

        assert (result, err) == (True, None)
        assert mock_smtp_class.call_args.kwargs["timeout"] == SMTP_TIMEOUT_SECONDS

    @patch("smtplib.SMTP_SSL")
    def test_ssl_connection_passes_the_named_timeout(self, mock_ssl_class):
        from services.email_service import SMTP_TIMEOUT_SECONDS

        mock_ssl_class.return_value.__enter__.return_value = MagicMock()
        repo = Repository()
        save_smtp_config(
            repo=repo, enabled=True, host="smtp.example.com", port=465, encryption="ssl",
            user="smtp_user", password="smtp_password",
            from_email="noreply@example.com", from_name="SpotifyTracker",
        )

        result, err = send_test_email(repo, "admin@example.com")

        assert (result, err) == (True, None)
        assert mock_ssl_class.call_args.kwargs["timeout"] == SMTP_TIMEOUT_SECONDS
