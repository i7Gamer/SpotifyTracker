# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import html
import logging
import os
import smtplib
import sqlite3
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from config import SMTP_SKIP_TLS_VERIFY_ENV_VAR, TRUTHY_ENV_VALUES
from Database.repository import Repository
from Database.secret_store import encryptSecret, decryptSecret
from Database.queries.email_queries import (
    EVENT_INVALID_COOKIES,
    EVENT_API_KEY_FAILED,
    EVENT_SHARE_REQUEST,
    EVENT_MILESTONE_REACHED,
    DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
)

logger = logging.getLogger(__name__)

# App Settings Keys
SETTING_EMAIL_NOTIF_ENABLED = "email_notifications_enabled"
SETTING_SMTP_HOST = "smtp_host"
SETTING_SMTP_PORT = "smtp_port"
SETTING_SMTP_ENCRYPTION = "smtp_encryption"  # 'tls', 'ssl', 'none'
SETTING_SMTP_USER = "smtp_user"
SETTING_SMTP_PASSWORD = "smtp_password"
SETTING_SMTP_FROM_EMAIL = "smtp_from_email"
SETTING_SMTP_FROM_NAME = "smtp_from_name"
SETTING_INSTANCE_PUBLIC_URL = "instance_public_url"

DEFAULT_SMTP_PORT = 587
DEFAULT_SMTP_ENCRYPTION = "tls"
DEFAULT_SMTP_FROM_NAME = "SpotifyTracker"
# Bounds every SMTP connect/read. smtplib's own default is no timeout at all,
# so without this a black-holed host holds whichever thread is sending - an
# admin's /admin/test_email request thread, or the email worker - indefinitely.
SMTP_TIMEOUT_SECONDS = 15

# What deliver_email_notification says happened. The worker retries FAILED and
# nothing else: SKIPPED is a decision (notifications off, the user opted out,
# the cooldown, no address on file), not something a second attempt can change.
EMAIL_SENT = "sent"
EMAIL_SKIPPED = "skipped"
EMAIL_FAILED = "failed"

# Where each event's email should send the recipient, relative to
# get_instance_public_url() - lives once here instead of being threaded
# through every call site that queues a notification.
_EVENT_LINK_PATHS = {
    EVENT_INVALID_COOKIES: "/login",
    EVENT_API_KEY_FAILED: "/profile/connections",
    EVENT_SHARE_REQUEST: "/profile/sharing",
    EVENT_MILESTONE_REACHED: "/#milestones",
}


def get_smtp_config(repo: Repository, include_password: bool = True) -> dict[str, Any]:
    """Retrieve full SMTP configuration and global email notification toggle
    from app_settings.

    `include_password=False` answers "password" as "" while "has_password"
    still reports whether a USABLE one is stored (see below) - for the /admin
    template, which only ever uses the password as a presence check (the
    masked placeholder). The
    decrypted secret must not ride into a render context it is never shown
    from: it would sit one careless value="{{ ... }}" edit away from the wire,
    and a Werkzeug debug frame would display it. The worker/send paths keep
    the default and get the real value."""
    enabled_val = repo.getAppSetting(SETTING_EMAIL_NOTIF_ENABLED, "0")
    host = repo.getAppSetting(SETTING_SMTP_HOST, "") or ""
    port_str = repo.getAppSetting(SETTING_SMTP_PORT, str(DEFAULT_SMTP_PORT))
    try:
        port = int(port_str)
    except (ValueError, TypeError):
        port = DEFAULT_SMTP_PORT

    encryption = repo.getAppSetting(SETTING_SMTP_ENCRYPTION, DEFAULT_SMTP_ENCRYPTION) or DEFAULT_SMTP_ENCRYPTION
    user = repo.getAppSetting(SETTING_SMTP_USER, "") or ""
    raw_password = repo.getAppSetting(SETTING_SMTP_PASSWORD, "") or ""
    # Decrypted whichever way this is called, because BOTH answers depend on
    # the plaintext: an undecryptable blob is "missing" per
    # Database/secret_store.py (a database restored without its key, or a
    # rotated DATA_ENCRYPTION_KEY), and the admin has to see "Not set" for it
    # rather than a mask over a secret that can never authenticate - which is
    # what keying has_password off the raw blob would show. The plaintext stays
    # a local of this function on the masked path; what include_password
    # governs is whether it escapes into the caller's dict.
    decrypted = decryptSecret(raw_password) if raw_password else ""
    password = decrypted if include_password else ""

    from_email = repo.getAppSetting(SETTING_SMTP_FROM_EMAIL, "") or ""
    from_name = repo.getAppSetting(SETTING_SMTP_FROM_NAME, DEFAULT_SMTP_FROM_NAME) or DEFAULT_SMTP_FROM_NAME

    return {
        "enabled": enabled_val == "1",
        "host": host,
        "port": port,
        "encryption": encryption,
        "user": user,
        "password": password,
        "has_password": bool(decrypted),
        "from_email": from_email,
        "from_name": from_name,
    }


def _isValidFromEmail(s: str) -> bool:
    """Empty means "not configured yet" (the same shape `_isValidPublicUrl`
    treats its field as); a non-empty value must have exactly one "@" with
    non-empty content on both sides. Not full RFC validation - just enough to
    reject a plain non-address like "notanemail" from silently saving as the
    SMTP From header's address (UT-16)."""
    return not s or (s.count("@") == 1 and all(s.split("@")))


def _isValidPublicUrl(s: str) -> bool:
    """Empty means "no link configured" (see get_instance_public_url); a
    non-empty value must be an http(s) URL, rejecting a javascript: (or any
    other) scheme from being stored and later emitted as a link href
    (UT-16)."""
    return not s or s.startswith(("http://", "https://"))


def save_smtp_config(
    repo: Repository,
    enabled: bool,
    host: str,
    port: int,
    encryption: str,
    user: str,
    password: str | None,
    from_email: str,
    from_name: str,
) -> None:
    """Save SMTP configuration into app_settings."""
    repo.setAppSetting(SETTING_EMAIL_NOTIF_ENABLED, "1" if enabled else "0")
    repo.setAppSetting(SETTING_SMTP_HOST, host.strip())
    repo.setAppSetting(SETTING_SMTP_PORT, str(port))
    repo.setAppSetting(SETTING_SMTP_ENCRYPTION, encryption.lower().strip())
    repo.setAppSetting(SETTING_SMTP_USER, user.strip())

    if password is None:
        pass  # keep existing stored value
    elif password == "":
        repo.setAppSetting(SETTING_SMTP_PASSWORD, "")  # explicit clear
    else:
        repo.setAppSetting(SETTING_SMTP_PASSWORD, encryptSecret(password))

    repo.setAppSetting(SETTING_SMTP_FROM_EMAIL, from_email.strip())
    repo.setAppSetting(SETTING_SMTP_FROM_NAME, from_name.strip())


def get_instance_public_url(repo: Repository) -> str:
    """The admin-configured base URL notification emails use to link back to
    this instance (e.g. "https://tracker.example.com"). Empty when unset -
    callers must treat that as no link being available, never guess at one:
    a self-hosted instance has no fixed public domain to fall back on."""
    return (repo.getAppSetting(SETTING_INSTANCE_PUBLIC_URL, "") or "").strip().rstrip("/")


def save_instance_public_url(repo: Repository, url: str) -> None:
    """Save the instance's public base URL used to build links in notification emails."""
    repo.setAppSetting(SETTING_INSTANCE_PUBLIC_URL, url.strip().rstrip("/"))


def build_email_message(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    from_email: str = "",
    from_name: str = DEFAULT_SMTP_FROM_NAME,
) -> MIMEMultipart:
    """Construct a MIME email message with plain text and optional HTML content."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject

    if from_name:
        msg["From"] = f"{from_name} <{from_email}>" if from_email else from_name
    else:
        msg["From"] = from_email

    msg["To"] = to_email

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    return msg


def _smtpTlsContext() -> ssl.SSLContext:
    """The context both TLS arms below hand smtplib.

    Handed explicitly because smtplib's own default is not one: with no
    context, SMTP_SSL and starttls() build ssl._create_stdlib_context(), which
    is CERT_NONE with hostname checking off (measured on the 3.14 runtime this
    ships on), so the credentials and every message went to whoever answered
    on that port. create_default_context() verifies the chain against the
    system store and the hostname against the certificate.

    The opt-out exists for a self-hosted relay on a self-signed certificate,
    which the verified context would refuse - and it warns on every send, so
    an instance left in that state keeps saying so in its log."""
    context = ssl.create_default_context()
    if os.environ.get(SMTP_SKIP_TLS_VERIFY_ENV_VAR, "").strip().lower() in TRUTHY_ENV_VALUES:
        logger.warning("%s is set - sending mail without verifying the SMTP server's certificate",
                       SMTP_SKIP_TLS_VERIFY_ENV_VAR)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _send_smtp_message(config: dict[str, Any], msg: MIMEMultipart) -> tuple[bool, str | None]:
    """Execute SMTP connection and transmit email message."""
    host = config["host"]
    port = config["port"]
    encryption = config["encryption"]
    user = config["user"]
    password = config["password"]

    if not host:
        return False, "SMTP Host is not configured"

    try:
        if encryption == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS,
                                  context=_smtpTlsContext()) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS) as server:
                if encryption == "tls":
                    server.starttls(context=_smtpTlsContext())
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        return True, None
    except Exception as e:
        logger.error("Failed to send email via SMTP (%s:%s): %s", host, port, e)
        return False, str(e)


def send_test_email(repo: Repository, recipient_email: str) -> tuple[bool, str | None]:
    """Send a test notification email to recipient_email using saved SMTP settings."""
    config = get_smtp_config(repo)
    if not config["host"]:
        return False, "SMTP host is empty. Please enter SMTP settings first."

    subject = "SpotifyTracker — Test Email"
    text_body = "This is a test notification email from your SpotifyTracker instance."
    html_body = """
    <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #121212; color: #ffffff;">
      <h2 style="color: #1db954;">SpotifyTracker</h2>
      <p>This is a test notification email verifying that your SMTP credentials are configured correctly.</p>
      <p style="color: #888888; font-size: 0.85em;">Sent by SpotifyTracker Admin Settings</p>
    </div>
    """

    msg = build_email_message(
        to_email=recipient_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        from_email=config["from_email"],
        from_name=config["from_name"],
    )

    return _send_smtp_message(config, msg)


def _eventLink(event_type: str, base_url: str) -> str | None:
    """The absolute URL an event's email should point to, or None when no
    instance URL is configured (or this event has no known destination) -
    callers must fall back to plain-text guidance instead of ever emitting a
    dead or guessed-at href."""
    if not base_url:
        return None
    path = _EVENT_LINK_PATHS.get(event_type)
    return f"{base_url.rstrip('/')}{path}" if path else None


def _ctaButton(link: str, label: str) -> str:
    """A styled call-to-action <a> for the HTML body. `link` is admin-
    supplied (the instance public URL setting), so it's escaped like any
    other value interpolated into an HTML attribute; `label` is always one
    of this module's own literal strings, never escaped."""
    return (
        f'<a href="{html.escape(link)}" style="background: #1db954; color: #ffffff; padding: 8px 16px; '
        f'text-decoration: none; border-radius: 4px; display: inline-block;">{label}</a>'
    )


def _render_event_template(
    event_type: str, username: str, context: dict[str, Any], base_url: str = ""
) -> tuple[str, str, str]:
    """Generate subject, text_body, and html_body for a given event type.

    base_url is the instance's configured public URL (see
    get_instance_public_url): when set, the email links straight to the
    relevant page; when blank, the copy falls back to telling the recipient
    where to go in words, since there's nothing reliable to link to."""
    link = _eventLink(event_type, base_url)
    # Escaped for the HTML bodies only (the text bodies are text/plain).
    # Usernames are sanitized at creation to str.isalnum() plus - and _
    # (Unicode-aware, so ł or Cyrillic survive but no HTML metacharacter
    # does), so none can actually reach these f-strings - but the escaping
    # decision was made nowhere (while _ctaButton right below escapes its
    # link), and a future switch to display names, which allow spaces and
    # more, would have turned the interpolation live silently.
    htmlUsername = html.escape(username)

    if event_type == EVENT_INVALID_COOKIES:
        subject = "SpotifyTracker — Action Required: Re-authenticate Session"
        text_body = (
            f"Hello {username},\n\nYour Spotify session cookies are invalid or expired. "
            "SpotifyTracker cannot log your listening activity until you log in again.\n\n"
            + (f"Log in here: {link}" if link else "Please visit your tracker instance to update your session.")
        )
        cta = _ctaButton(link, "Log In to Re-authenticate") if link else \
            "Please visit your tracker instance and log in again to resume tracking."
        html_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #121212; color: #ffffff;">
          <h2 style="color: #e05252;">Session Re-authentication Required</h2>
          <p>Hello <strong>{htmlUsername}</strong>,</p>
          <p>Your Spotify session cookies have expired or become invalid. Listening tracking has been paused.</p>
          <p>{cta}</p>
        </div>
        """
    elif event_type == EVENT_API_KEY_FAILED:
        subject = "SpotifyTracker — API Key Error"
        text_body = (
            f"Hello {username},\n\nYour Spotify or Last.fm API credentials returned an authentication error during backfill.\n\n"
            + (f"Update them here: {link}" if link else "Please check your account Connections on your profile page.")
        )
        cta = _ctaButton(link, "Update Connections") if link else \
            "Please update your credentials on your Profile Connections page."
        html_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #121212; color: #ffffff;">
          <h2 style="color: #ff9800;">API Credentials Error</h2>
          <p>Hello <strong>{htmlUsername}</strong>,</p>
          <p>Your API credentials (Spotify Developer API or Last.fm key) failed during recent backfill tasks.</p>
          <p>{cta}</p>
        </div>
        """
    elif event_type == EVENT_SHARE_REQUEST:
        requester = context.get("requester_username", "A user")
        subject = f"SpotifyTracker — New Share Request from {requester}"
        text_body = (
            f"Hello {username},\n\n{requester} requested to share listening data with you on SpotifyTracker.\n\n"
            + (f"Respond here: {link}" if link else "Log in to accept or decline this request on your profile Sharing page.")
        )
        cta = _ctaButton(link, "View Request") if link else \
            "Log in to view, accept, or decline this request on your Profile Sharing page."
        html_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #121212; color: #ffffff;">
          <h2 style="color: #1db954;">New Data Sharing Request</h2>
          <p>Hello <strong>{htmlUsername}</strong>,</p>
          <p><strong>{html.escape(requester)}</strong> sent you a request to share listening statistics.</p>
          <p>{cta}</p>
        </div>
        """
    elif event_type == EVENT_MILESTONE_REACHED:
        # context["milestones"] is [{icon, label, artistId?}, ...] - the same
        # shape formatMilestone (services/milestones.py) returns, one entry
        # per crossing this detection pass recorded. Kept possibly-empty
        # rather than assumed non-empty: a defensive caller (or a future one)
        # must get sane copy instead of a crash or a blank email.
        milestones = context.get("milestones") or []
        count = len(milestones)
        if count == 1:
            subject = f"SpotifyTracker — Milestone Reached: {milestones[0].get('label', 'Milestone reached')}"
        elif count > 1:
            subject = f"SpotifyTracker — {count} New Milestones Reached"
        else:
            subject = "SpotifyTracker — Milestone Reached"

        if milestones:
            textLines = "\n".join(f"- {m.get('icon', '')} {m.get('label', '')}".strip() for m in milestones)
            introText = f"You just reached:\n\n{textLines}\n\n"
            itemsHtml = "".join(
                f"<li>{html.escape(m.get('icon', ''))} {html.escape(m.get('label', ''))}</li>"
                for m in milestones
            )
            bodyHtml = f"<ul>{itemsHtml}</ul>"
        else:
            introText = "You reached a new listening milestone.\n\n"
            bodyHtml = "<p>You reached a new listening milestone.</p>"

        text_body = (
            f"Hello {username},\n\n{introText}"
            + (f"See it on your dashboard: {link}" if link else "Log in to see it on your dashboard.")
        )
        cta = _ctaButton(link, "View Your Milestones") if link else \
            "Log in to your SpotifyTracker dashboard to see it."
        html_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #121212; color: #ffffff;">
          <h2 style="color: #1db954;">Milestone Reached!</h2>
          <p>Hello <strong>{htmlUsername}</strong>,</p>
          {bodyHtml}
          <p>{cta}</p>
        </div>
        """
    else:
        subject = f"SpotifyTracker — Notification ({event_type})"
        text_body = f"Hello {username},\n\nYou have a new notification on SpotifyTracker."
        html_body = f"<p>Hello {htmlUsername}, you have a new notification.</p>"

    return subject, text_body, html_body


def send_email_notification(
    repo: Repository, username: str, event_type: str, context: dict[str, Any] | None = None
) -> bool:
    """Whether an event notification email went out to username - see
    deliver_email_notification for the three-way answer underneath."""
    return deliver_email_notification(repo, username, event_type, context) == EMAIL_SENT


def deliver_email_notification(
    repo: Repository, username: str, event_type: str, context: dict[str, Any] | None = None
) -> str:
    """Send an event notification email to username, adhering to global and
    user settings and cooldown limits. Answers EMAIL_SENT, EMAIL_SKIPPED or
    EMAIL_FAILED: the worker retries a failure, and a skip is not one."""
    try:
        config = get_smtp_config(repo)
        if not config["enabled"] or not config["host"]:
            return EMAIL_SKIPPED

        # Check user preference
        if not repo.getUserNotificationPreference(username, event_type):
            logger.debug("User %s opted out of email notification for event %s", username, event_type)
            return EMAIL_SKIPPED

        # Check anti-spam cooldown
        if repo.isNotificationCooldownActive(username, event_type, cooldown_seconds=DEFAULT_NOTIFICATION_COOLDOWN_SECONDS):
            logger.debug("Cooldown active for user %s event %s, skipping email", username, event_type)
            return EMAIL_SKIPPED

        user_email = repo.getEmailForUsername(username)
        if not user_email:
            logger.warning("No email address on record for user %s", username)
            return EMAIL_SKIPPED

        ctx = context or {}
        base_url = get_instance_public_url(repo)
        subject, text_body, html_body = _render_event_template(event_type, username, ctx, base_url)

        msg = build_email_message(
            to_email=user_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_email=config["from_email"],
            from_name=config["from_name"],
        )
    except sqlite3.OperationalError as error:
        # Nothing has reached SMTP yet, so the worker can safely retry using
        # its bounded policy. Keep post-send cooldown writes outside this
        # handler: retrying their failure would duplicate a delivered message.
        logger.error("Database error preparing notification email (%s) for %s: %s", event_type, username, error)
        return EMAIL_FAILED

    success, err = _send_smtp_message(config, msg)
    if success:
        try:
            repo.recordNotificationSent(username, event_type)
        except sqlite3.Error as error:
            # SMTP has already accepted the message. Retrying now would send a
            # duplicate, so report delivery as successful while making the
            # missing anti-spam stamp visible to operators.
            logger.error("Notification email (%s) was sent to %s but could not "
                         "record its cooldown: %s", event_type, username, error)
        logger.info("Notification email (%s) sent successfully to %s", event_type, username)
        return EMAIL_SENT
    logger.error("Failed to send notification email (%s) to %s: %s", event_type, username, err)
    return EMAIL_FAILED
