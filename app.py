# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import os
import random
import tempfile
import threading
import requests
from pathlib import Path
import time
from contextlib import suppress
from datetime import timedelta, datetime, timezone

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory, session, g, abort, Response, stream_with_context, make_response
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

from Database.database import Database
from Database.backup import (
    BackupWorker,
)
from Database.backup_settings import (
    resolveBackupSettings, BACKUP_INTERVAL_HOURS_KEY, BACKUP_RETENTION_COUNT_KEY,
)
from routes._htmx import isHtmxSwap
from routes._xhr import declaresItselfXhr as _declaresItselfXhr
from services.deploy_state import deployMismatch, sourceFingerprint
from services.email_worker import EMAIL_WORKER, queue_email_notification
from Database.queries.email_queries import EVENT_MILESTONE_REACHED
from Database.repository import Repository
from Database.Migrators.migrate import migrateIfNeeded
from Database.secret_store import readOrCreateKeyFile, FLASK_SECRET_KEY_ENV_VAR, keyFingerprint
from Database.Listeners.spotifyListener import _suppress_signal_in_thread
from Database.Spotify.recentlyPlayed import setPushListenerEnabledHook as patch_push_listener_hook
from Database.logging_config import configureLogging
from Database.utils import msToString, convertToDatetime, formatDuration, dateToString, versionTuple, now, startOfDay, parseDateString, flaskDebugEnabled
# Genre-gate / coverage helpers live in services/genre_gate.py; re-exported here
# so route code (and the test suite, which imports several by name) still reach
# them through `app`.
from services.genre_gate import (
    GENRE_GATE_OVERALL_MIN_PERCENT, GENRE_GATE_CATEGORY_MIN_PERCENT,
    emptyGenreCoverage, sanitizeGenreCoverage, resolveGenreCoverage,
    resolveGenreDistribution, genreGatePasses, emptyBiographyCoverage,
    sanitizeBiographyCoverage, resolveBiographyCoverage,
    resolveGenresForTrack, resolveGenresForAlbum, resolveGenresForArtist,
    resolveGenresForTracks, resolveGenresForAlbums, resolveGenresForArtists,
    resolveGenreTrends, resolveGenreStats, resolveTopArtistsForGenre,
    resolveTopTracksForGenre, resolveGenreHeatmap, emptyHeatmapGrid,
    resolveGenreArtistCounts, userHasLastfmKey,
)
# Taste-match scoring lives in services/taste_match.py; the compare route calls
# _tasteMatchPercent/_markLinkExternally and _buildSharedItems calls
# _rankById/_sharedRankScore.
from services.taste_match import (
    _tasteMatchPercent, _markLinkExternally, _rankById, _sharedRankScore,
)
from services.milestones import (
    detectMilestonesDetailed, recalculateMilestoneDates, formatMilestone, EMAILED_MILESTONE_KINDS,
)
from routes.media import register as registerMediaRoutes
from routes.admin import register as registerAdminRoutes
from routes.charts import register as registerChartsRoutes
from routes.details import register as registerDetailsRoutes
from routes.genres import register as registerGenresRoutes
from routes.compare import register as registerCompareRoutes
from routes.wrapped import register as registerWrappedRoutes
from routes.auth import register as registerAuthRoutes
from routes.system import register as registerSystemRoutes
from routes.tags import register as registerTagsRoutes
from dashboard.context_processors import register as registerContextProcessors
from Database.Spotify import Spotify
from Database.Spotify.cookies import saveSession, parseCookieString

logger = logging.getLogger(__name__)

SLOW_REQUEST_LOG_THRESHOLD_SECONDS = 1.0  #< warn only when a request takes over one second
SLOW_REQUEST_UNMATCHED_ROUTE = "<unmatched>"

# Instance-wide display/behavior constants live in config.py; imported * here so
# app.py and the test suite (`from app import <CONST>`) reach them through
# `app`. Route modules import them from config directly.
from config import *  # noqa: E402,F401,F403


def _trustedProxyCount() -> int:
    """How many reverse-proxy hops to trust X-Forwarded-* headers from, per the
    TRUST_PROXY_HEADERS env var: a hop count ("2"), or a plain truthy value
    ("true") meaning one proxy. 0/unset/junk disables it - trusting forwarded
    headers while NOT behind a proxy would let clients forge their source IP
    straight past the auth rate limiter, so this must stay opt-in."""
    raw = os.environ.get(TRUST_PROXY_HEADERS_ENV_VAR, "").strip().lower()
    if not raw:
        return 0
    try:
        count = int(raw)
    except ValueError:
        count = None
    if count is None and raw in TRUTHY_ENV_VALUES:
        return 1
    if count is None and raw in FALSY_ENV_VALUES:
        return 0
    if count is None or count < 0:
        # A typo ("2.0", "ture", "-1") lands here and disables the feature -
        # the per-IP auth limiter then shares one bucket across everyone
        # behind the proxy, which is what the variable exists to prevent. Say
        # so, the way Database.backup._envInt and wsgi._waitressThreads do;
        # FALSY_ENV_VALUES ("0", "false", "no", "off") are explicit opt-outs
        # and stay silent.
        logger.warning("Ignoring unrecognised %s=%r - X-Forwarded-* headers will NOT be trusted",
                       TRUST_PROXY_HEADERS_ENV_VAR, raw)
        return 0
    return count


def _hstsEnabled() -> bool:
    """Whether to send a Strict-Transport-Security header, per the ENABLE_HSTS
    env var. Off by default: the app is normally self-hosted over plain HTTP
    (see SECURITY_HEADERS in config.py), where pinning HTTPS for the origin
    would break access. Enable it only when a TLS-terminating reverse proxy is
    in front. Read live per response so a flip doesn't need a restart."""
    return os.environ.get(ENABLE_HSTS_ENV_VAR, "").strip().lower() in TRUTHY_ENV_VALUES


class _RateLimiter:
    """In-memory fixed-window rate limiter, keyed by (bucket, identifier).

    Single-process only (state isn't shared across workers and doesn't
    survive a restart) - adequate for this app's single-process Waitress
    deployment, and mirrors the existing in-memory _login_cache pattern
    rather than pulling in an external dependency for a personal, low-
    traffic self-hosted app."""

    def __init__(self, maxAttempts: int, windowSeconds: float):
        self.maxAttempts = maxAttempts
        self.windowSeconds = windowSeconds
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()
        # hit() only ever prunes the key it touches, so a key for a (bucket, IP)
        # that stops sending requests would otherwise live forever - one entry
        # per source IP per bucket, an unbounded leak in a long-lived process
        # scanned from many IPs. A full sweep, run at most once per window,
        # drops keys whose every timestamp has aged out.
        self._lastSweep = time.monotonic()

    def _sweepExpired(self, now_ts: float, cutoff: float):
        if now_ts - self._lastSweep < self.windowSeconds:
            return
        self._lastSweep = now_ts
        self._hits = {
            key: fresh
            for key, timestamps in self._hits.items()
            if (fresh := [t for t in timestamps if t >= cutoff])
        }

    def hit(self, bucket: str, identifier: str) -> bool:
        """Record one attempt for (bucket, identifier). Returns True if it's
        allowed (under the limit), False if this attempt should be rejected."""
        key = (bucket, identifier)
        now_ts = time.monotonic()
        cutoff = now_ts - self.windowSeconds
        with self._lock:
            self._sweepExpired(now_ts, cutoff)
            hits = [t for t in self._hits.get(key, []) if t >= cutoff]
            if len(hits) >= self.maxAttempts:
                self._hits[key] = hits
                return False
            hits.append(now_ts)
            self._hits[key] = hits
            return True


from dashboard.view_models import ViewModelMixin
from dashboard.pagination import PaginationMixin
from dashboard.date_ranges import DateRangeMixin
from dashboard.wrapped_builder import WrappedBuilderMixin
from dashboard.compare_stats import CompareStatsMixin
from dashboard.user_registry import UserRegistryMixin


class SpotifyDashboardApp(ViewModelMixin, PaginationMixin, DateRangeMixin, WrappedBuilderMixin,
                          CompareStatsMixin, UserRegistryMixin):
    def __init__(self):
        configureLogging()
        migrateIfNeeded()   #< before anything opens the database
        self.app = Flask(__name__)
        # The genre-gate thresholds are quoted in templates (the locked-state
        # progress card) - exposed as globals so every include sees them
        # without each route re-passing them.
        self.app.jinja_env.globals.update(
            genreGateOverallMinPercent=GENRE_GATE_OVERALL_MIN_PERCENT,
            genreGateCategoryMinPercent=GENRE_GATE_CATEGORY_MIN_PERCENT,
        )
        proxyHops = _trustedProxyCount()
        if proxyHops:
            # Restores the real client address (and scheme/host) from the
            # X-Forwarded-* headers set by the reverse proxy in front of this
            # app - request.remote_addr is what the auth rate limiter keys on.
            self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_for=proxyHops, x_proto=proxyHops, x_host=proxyHops)
        self.baseDir = Path(__file__).resolve().parent
        self.app.secret_key = self._get_or_create_secret_key()
        # Evaluate the data-encryption key material now too, right after the
        # Flask signing key above and OUTSIDE any try/except - the only other
        # boot-time touch is _logIntegrityProbe's countSecretsUnderAnotherKey
        # probe below, which is wrapped in `except Exception: logger.debug(...)`
        # so it can't be trusted to refuse anything. Without this call, a
        # DATA_ENCRYPTION_KEY placeholder or an emptied
        # secrets/data_encryption_key.txt let the app boot clean, health-check
        # green, and 500 on every login/page instead of refusing to start
        # (F-B-1, 2026-09-04 review). Accepted side effect: with neither key
        # env var set, this now MINTS secrets/data_encryption_key.txt here
        # instead of at first encrypt/decrypt - same file, just earlier - and
        # an EMPTY key file refuses boot instead of silently being accepted
        # later (see readOrCreateKeyFile's emptyFileError).
        keyFingerprint()
        self.app.permanent_session_lifetime = timedelta(days=PERMANENT_SESSION_LIFETIME_DAYS)
        # Session cookie hardening. HttpOnly (Flask's default) keeps JS off the
        # cookie; SameSite=Lax stops it riding cross-site POSTs. Secure is gated
        # on the same TLS signal as HSTS - left off on the plain-HTTP default
        # deployment, where a Secure cookie would never be sent and would lock
        # everyone out. Read once at construction, like the proxy-hops setting.
        self.app.config["SESSION_COOKIE_HTTPONLY"] = True
        self.app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        self.app.config["SESSION_COOKIE_SECURE"] = _hstsEnabled()
        # Caps a single import-history request's total upload size (summed across
        # every file in a multi-file upload) - without this, an oversized/
        # accidental upload is read fully into memory before anything can reject
        # it.
        self.app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * BYTES_PER_MB
        self.app.config["WTF_CSRF_TIME_LIMIT"] = None
        if os.environ.get("PYTEST_CURRENT_TEST") or self.app.config.get("TESTING"):
            self.app.config["WTF_CSRF_ENABLED"] = False
        self._registerSlowRequestLogging()
        CSRFProtect(self.app)
        # Users, emails and Spotify session cookies live in the shared database
        # (see Database/repository.py) instead of secrets/users_map.json and
        # secrets/cookies.json.
        self.repo = Repository()
        # Size the shared background thread pools from admin settings, once,
        # before any Database instance (and its workers) is constructed. A
        # changed value applies only after a restart - see configureWorkerPools.
        Database.configureWorkerPools(self.repo)
        # No download is in flight this early, so any surviving 'pending'
        # image claim is stale (a previous run died mid-download) and would
        # block that image from ever being fetched.
        staleImageClaims = self.repo.deleteStalePendingImages()
        if staleImageClaims:
            logger.info("Cleared %d stale pending image download claim(s) from a previous run", staleImageClaims)
        # Same reasoning for import progress: no import thread can be running
        # this early, so a row still claiming 'running' was left by a process
        # that died mid-import, and tryClaimImportRunning refuses while it
        # stands. Belt to the braces of the unconditional resetProgress() each
        # user's activation already runs: this resolves the row ONCE, at boot,
        # as a terminal row rather than leaning on a side effect of activation.
        staleImports = self.repo.failStaleRunningImports()
        if staleImports:
            logger.info("Marked %d import(s) left 'running' by a previous run as failed", staleImports)
        self._logIntegrityProbe()
        self._ensureAdminExists()

        # user_databases, the activation set, the lock hierarchy and the
        # login-status cache - see dashboard/user_registry.py.
        self._initUserRegistry()
        # Throttles POSTs to /login, /register, /reset-password per source IP -
        # without this, a network-reachable instance is brute-forceable
        # indefinitely (check_password_hash costs some compute, but nothing
        # stops an unlimited number of attempts).
        self._authRateLimiter = _RateLimiter(RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS)
        # Lets a self-hoster turn off the "do these cookies actually belong to
        # this email" check at login, e.g. if Spotify starts blocking the
        # verification request for their account. Off by default since it's
        # what stops one user from claiming another's account/database.
        self.skipEmailVerification = os.environ.get(SKIP_EMAIL_VERIFICATION_ENV_VAR, "").strip().lower() in TRUTHY_ENV_VALUES
        
        self.currentVersion = "0.0.0"   #< placeholder in case the VERSION file is unreadable
        with suppress(Exception):
            #< read once - the app cannot update without a restart
            self.currentVersion = (self.baseDir / "Database" / "VERSION").read_text(encoding="utf-8").strip()
        # ...which is exactly why getDeployMismatch re-reads it: a file-copy
        # deploy without a restart updates the file and leaves this variable,
        # the compiled templates and this module itself on the old build. The
        # fingerprint is the same question asked of the source tree, and has to
        # be taken HERE, while what is on disk is still what got loaded.
        self._bootFingerprint = sourceFingerprint(self.baseDir)
        self.latestVersion = None   #< set by the version-check worker when a newer release exists
        self._version_lock = threading.Lock()
        self._stop_event = threading.Event()
        # Per-user {username: (totalPlays, totalMs)} from the last milestone
        # pass, so an idle background cycle skips the heavy streak/top-artist
        # queries when a user's totals haven't moved (see detectMilestones).
        # In-process is sufficient: detection only runs from the single
        # _checkLoginLoop thread, and this app is single-process by design.
        self._milestoneChangeCache: dict = {}
        # The periodic login-check loop's thread (started by
        # checkLogin_thread; None until then, e.g. under test) - /admin reads
        # its liveness as the Milestone Detection health, since the milestone
        # pass runs inside that loop rather than on a thread of its own.
        self._checkLoginThread: threading.Thread | None = None
        # Snapshots the shared database on a schedule (see Database/backup.py) -
        # a manual backup command in the README protects nobody who doesn't run it.
        # Interval/retention come from admin settings, falling back to the env
        # vars then the code defaults; read once here, so changes apply on restart.
        backupSettings = resolveBackupSettings(
            self.repo.getAppSetting(BACKUP_INTERVAL_HOURS_KEY),
            self.repo.getAppSetting(BACKUP_RETENTION_COUNT_KEY),
        )
        self.backupWorker = BackupWorker(
            intervalHours=backupSettings.intervalHours,
            retentionCount=backupSettings.retentionCount,
        )
        # The workers themselves are constructed here (BackupWorker reads its
        # schedule from admin settings, and /admin's Worker Health panel reads
        # self.backupWorker) but deliberately NOT started - see startWorkers().
        self._workersStarted = False

        self.registerRoutes()   #< all HTTP surface lives in routes/, registered here

    def _get_or_create_secret_key(self):
        """Resolve the Flask session-signing key. Prefers FLASK_SECRET_KEY, otherwise
        persists a random key under secrets/ so sessions can't be forged using the
        publicly-known default that used to ship in this repo."""
        envKey = os.environ.get(FLASK_SECRET_KEY_ENV_VAR)
        if envKey:
            if envKey.strip() == PLACEHOLDER_FLASK_SECRET_KEY:
                # Refuse to boot on the shipped placeholder: it is a public value,
                # so sessions signed with it are forgeable and (when it doubles as
                # the at-rest encryption key) stored Spotify sessions are readable.
                raise RuntimeError(
                    "FLASK_SECRET_KEY is still set to the docker-compose placeholder "
                    "value. Generate a real random key - e.g. "
                    "`python -c \"import secrets; print(secrets.token_hex(32))\"` - "
                    "and set FLASK_SECRET_KEY to it before starting."
                )
            return envKey

        # No emptyFileError, unlike the data encryption key this shares the
        # helper with: an empty or lost signing key only invalidates live
        # sessions (everyone logs in again), while a lost data encryption key
        # strands every stored Spotify session for good - so this one re-mints
        # where that one has to refuse.
        return readOrCreateKeyFile(self.baseDir / SECRETS_DIR_NAME / FLASK_SECRET_KEY_FILENAME)

    def _logIntegrityProbe(self):
        """Say plainly, once per start, whether the database is intact.

        On 2026-07-15 a corrupt database announced itself as 16 "disk image is
        malformed" errors and 22 UNIQUE-constraint failures on track_artists,
        spread across three modules inside one minute - and the constraint
        failures looked enough like a write race to be misdiagnosed as one.
        A single named line at boot is what that morning was missing.

        Findings are reported, never acted on: the orphan cleanup that would
        delete dangling rows was deliberately removed (commit ad9a804) because
        it destroyed catalog metadata, so this must not quietly reintroduce it.
        Costs ~240 ms on a 105 MB database."""
        integrity = self.repo.checkIntegrity()
        if integrity.get("probeError"):
            logger.warning(
                "Database integrity check could not run: %s. This says nothing about the "
                "database's health - it means the probe itself failed (a lock, most likely).",
                integrity["probeError"],
            )
        if integrity["corruption"]:
            logger.error(
                "DATABASE INTEGRITY CHECK FAILED - the database file is damaged. "
                "Restore from a backup before trusting anything this process records. Details: %s",
                integrity["corruption"][:5],
            )
        if integrity["foreignKeyViolations"]:
            # Not an error: a dangling reference is invisible to normal queries
            # (JOINs drop the row), so this is a "known, tolerated" signal whose
            # value is in noticing when the number CHANGES.
            logger.warning(
                "Database has %d dangling foreign-key row(s): %s. These are inert (JOINs drop them) "
                "but a rising count points at a write path bypassing foreign_keys=ON.",
                sum(integrity["foreignKeyViolations"].values()),
                integrity["foreignKeyViolations"],
            )
        if integrity["ok"]:
            logger.info("Database integrity check passed")

        # Said once, at boot, because the symptom otherwise is every user
        # appearing logged out with nothing anywhere explaining it - which is
        # exactly what a database restored without its matching key file does.
        try:
            foreignSecrets = self.repo.countSecretsUnderAnotherKey()
        except Exception as e:
            logger.debug("Could not check stored-secret key ownership: %s", e)
            foreignSecrets = 0
        if foreignSecrets:
            logger.warning(
                "%d stored secret(s) were encrypted with a DIFFERENT key than this instance uses. "
                "They are intact but unreadable here - restore secrets/data_encryption_key.txt from "
                "the same backup as the database (or set DATA_ENCRYPTION_KEY to the original value). "
                "Until then affected user credentials or SMTP settings remain unreadable; "
                "user logins or email delivery may fail.",
                foreignSecrets,
            )

    def _ensureAdminExists(self):
        """Admin bootstrap, run at every startup. ADMIN_EMAIL (when set) is
        authoritative: that user becomes the ONLY admin - demoting anyone
        else, which is what makes it the recovery path when the automatic
        promotion picked the wrong account. A typo'd ADMIN_EMAIL changes
        nothing (losing all admins to a typo would be worse than keeping a
        stale one). Without the env var, the earliest-created user is
        promoted once if no admin exists yet, so fresh installs converge on
        the instance owner (migration 1.17.0 does the same for upgrades)."""
        adminEmail = os.environ.get(ADMIN_EMAIL_ENV_VAR, "").strip()
        if adminEmail:
            # getUsernameForEmail is itself case-insensitive (COLLATE NOCASE)
            # now, so ADMIN_EMAIL differing only in case from the stored
            # email still resolves - no separate *CaseInsensitive method
            # needed here any more.
            username = self.repo.getUsernameForEmail(adminEmail)
            if not username:
                logger.warning("%s=%s does not match any user - admin assignment unchanged",
                               ADMIN_EMAIL_ENV_VAR, adminEmail)
                return
            for other in self.repo.getAdminUsernames():
                if other != username:
                    self.repo.setUserAdmin(other, False)
                    logger.info("Demoted %s from admin (%s designates %s)", other, ADMIN_EMAIL_ENV_VAR, username)
            if not self.repo.isAdmin(username):
                self.repo.setUserAdmin(username, True)
                logger.info("Promoted %s to admin (%s)", username, ADMIN_EMAIL_ENV_VAR)
            return

        promoted = self.repo.promoteEarliestUserToAdminIfNoneExists()
        if promoted:
            logger.info("Promoted earliest-created user %s to admin (no admin existed yet)", promoted)

    def _verifyCookiesMatchEmail(self, cookies: dict, email: str) -> bool:
        """Check that the submitted Spotify cookies actually belong to `email` by
        fetching the account profile with them. The cookies are written to a
        throwaway session file so an unverified login attempt can never overwrite
        another user's stored cookies. Without this check, anyone could claim any
        email at login and be handed that user's database."""
        if not cookies or not email:
            return False

        tmpFd, tmpPath = tempfile.mkstemp(prefix="verify_cookies_", suffix=".json")
        os.close(tmpFd)
        sp = None
        try:
            saveSession(cookies, email, tmpPath)
            with _suppress_signal_in_thread():
                sp = Spotify(cookiesFile=tmpPath, email=email)
            if not sp.isLoggedIn():
                return False
            profile = sp.current_user() or {}
            profileEmail = (profile.get("email") or "").strip().lower()
            return profileEmail == email.strip().lower()
        except Exception as e:
            logger.warning("Cookie verification failed for %s: %s", email, e)
            return False
        finally:
            if sp is not None:
                # This login built its own TLS client, and every one of those
                # is atexit-pinned until the process exits - so a session left
                # open here accumulated one per login/register/reset attempt.
                sp.close()
            try:
                os.unlink(tmpPath)
            except OSError:
                pass

    def unauthenticatedResponse(self, nextPath: str | None = None):
        """The "no live session" response for a route. An ajax request gets a
        401 JSON body: a redirect would be followed transparently by fetch(),
        and the page would then try to parse the login HTML as JSON and show a
        generic "couldn't load" with a Retry that fails identically forever.
        The client turns the 401 into a real navigation (see
        AjaxStatus.redirectIfUnauthorized). Everything else keeps the redirect.

        "An ajax request" is either spelling, because the fetches in this app
        use both and the choice belongs to the route, not to this guard: the
        page loaders mark the URL with ?ajax=, while the admin console's
        Create-backup and Refresh-Last.fm forms send X-Requested-With because
        their ROUTES read that header to pick JSON over a redirect. Reading
        only the query marker left those three answering a 302 that fetch
        followed, so an expired session was reported as "Backup failed - try
        again" instead of sending the admin to log in (see _declaresItselfXhr).

        An htmx request needs the same escape for the same reason - htmx follows
        a 302 as transparently as fetch() did, and would swap the login page's
        HTML into whatever region was being refreshed. It gets the equivalent it
        understands natively instead: HX-Redirect, which the client turns into a
        real navigation. 204 rather than 401 because htmx swaps the body of any
        2xx and treats 4xx as an error to report; a No Content response leaves
        it nothing to inject and nothing to complain about, so the only thing
        that happens is the redirect.

        A history restore is NOT such a request, even though htmx marks it with
        the same HX-Request header. Its response replaces document.body and its
        XHR never looks at HX-Redirect, so the 204 would blank the page rather
        than navigate. It takes the ordinary redirect below, which the XHR
        follows transparently - putting the login page where the restored page
        would have been. isHtmxSwap draws that line (see routes/_htmx.py)."""
        #< full_path, not path: the query string IS the page state here (interval,
        #  custom dates, sortBy, tag, page), so dropping it landed the user on an
        #  unfiltered first page after logging back in. rstrip("?") because
        #  full_path always appends one, even with no args. _safeNextUrl still
        #  vets it on the way back (see routes/auth.py).
        target = nextPath or request.full_path.rstrip("?")
        loginUrl = url_for("login", next=target)
        if isHtmxSwap():
            return Response(status=204, headers={"HX-Redirect": loginUrl})
        if request.args.get("ajax") or _declaresItselfXhr():
            return jsonify(error="Not logged in", loginUrl=loginUrl), 401
        return redirect(loginUrl)

    def get_current_user_or_redirect(self):
        """The (email, username, db) triple for the authenticated session, or
        (None, None, None) when no live session exists - route handlers redirect
        to /login on the None case. Also self-heals a session whose username
        drifted from its email mapping.

        The db is stashed on `g` so anything later in the SAME request can
        reach it without a second lookup - the context processors, chiefly.
        This said "for teardown" for a long time, and no teardown hook has ever
        been registered: the Database instances are long-lived and per-user
        (see user_databases), owned by the registry rather than by a request,
        so there is nothing here for a teardown to release. Naming a hook that
        does not exist reads as a guarantee that something is being cleaned
        up."""
        email = session.get("email")
        if not email or not self.is_user_logged_in(email):
            return None, None, None

        #< no session-version check here: _endSessionsTheAccountHasInvalidated
        #  has already cleared an out-of-date cookie for this request, so the
        #  email read above is absent for one and this returns None on its own
        # Ensure the username matches the correct email mapping to prevent session pollution from legacy user "Tzur"
        correct_username = self.get_username_for_email(email)
        if not correct_username:
            correct_username = self.get_or_create_user(email)

        if session.get("username") != correct_username:
            session["username"] = correct_username

        username = correct_username
        db = self.get_user_db(username, email)
        g.db = db
        return email, username, db

    def sessionIsCurrent(self, username) -> bool:
        """Whether THIS request's session cookie still belongs to `username`'s
        current generation.

        Sessions are signed cookies with no server-side store, so a "log out"
        could only ever clear the cookie in front of it - every other device
        kept a valid session for the rest of its 30 days, including after a
        password reset. users.session_version closes that: each cookie carries
        a copy, and bumping the stored value invalidates every cookie for the
        account at once (see SESSION_VERSION_KEY).

        A missing key reads as 0 on BOTH sides, which is what makes the upgrade
        free - every cookie already in the wild carries no version, and the
        column starts at 0.

        A row with no version reads as 0 too, rather than as a rejection: the
        only caller resolved this username FROM that row a line earlier, so the
        case is unreachable, and treating it as a rejection would only ever
        fire where a test has stubbed the lookup - never for a session this is
        meant to end.

        Deliberately NOT folded into is_user_logged_in: that one is also called
        from the background login loop, where there is no request and no
        session to read."""
        return session.get(SESSION_VERSION_KEY, 0) == (self.repo.getUserSessionVersion(username) or 0)

    def stampSessionVersion(self, username) -> None:
        """Mark this session as belonging to the account's CURRENT generation.

        Every door into a session has to call this - a login that skips it is
        signed out again by its own next request, and the browser that bumps
        the version has to re-stamp itself or it signs out the device the user
        is holding."""
        session[SESSION_VERSION_KEY] = self.repo.getUserSessionVersion(username) or 0

    def _detectMilestonesSafely(self, db, username):
        """Run one user's milestone-detection pass from the periodic background
        loop (_ensureAllUsersLogin), never the request path - so a page render
        never pays for the aggregate queries or the DB write, and the badge
        just reads an already-computed count. Failures are logged and
        swallowed so one user's bad pass can't stall the loop. Only users with
        stored cookies are covered (that's who the loop iterates); an
        import-only account with no live session first gets its milestones on
        its next cookie login. No-op when the admin kill switch is off (see
        isMilestonesEnabled) - the badge/section are hidden then too, so there's
        no point recording new rows.

        Also queues one milestone-reached email per pass listing every
        crossing this pass recorded that wasn't already seen (seeding and
        import-backfill passes record everything seen=True, so they stay
        silent - same contract as the topbar badge). A pass that crosses
        several thresholds at once still sends a single email; a second pass
        within 24h is absorbed by the existing per-(user, event) cooldown in
        deliver_email_notification, so this never doubles up."""
        if not self.repo.isMilestonesEnabled():
            return
        localNeedsRecalc = False
        try:
            # Import-backfill hygiene, one admin toggle for both halves:
            # crossings surfaced by imported history are recorded as already
            # seen (they're past achievements - the same no-notification
            # contract as first-pass seeding), and milestone dates are
            # re-derived from play history. `pending` is the end-of-batch
            # flag; the readProgress check additionally catches passes
            # landing mid-import (this loop runs every 5 minutes and large
            # imports span that), where the flag doesn't exist yet.
            recalcEnabled = self.repo.isMilestoneRecalcEnabled()
            # While an import is rewriting this user's history, the whole pass
            # is wasted energy: any row detection recorded would land seen=1
            # (invisible) and be re-dated by the settled pass the end-of-batch
            # flag guarantees, while detection's aggregate queries compete
            # with the import's writes. Skip everything, leaving the flag its
            # one shot. A stale "running" can't wedge this permanently -
            # get_user_db resets import progress on activation after a
            # restart. Gated on the hygiene toggle: off = pre-1.36.0 behavior
            # wholesale, including mid-import detection.
            if recalcEnabled and db.readProgress().get("status") == "running":
                return
            pending = db.consumeMilestoneRecalcFlag() if recalcEnabled else False
            localNeedsRecalc = pending
            rows = detectMilestonesDetailed(db, db.repo, username,
                                            changeCache=self._milestoneChangeCache,
                                            markSeen=pending)
            recorded = len(rows)
            # Date re-derivation strictly after detection so import-crossed
            # rows exist first. Triggered by the end-of-batch flag or by any
            # recorded crossing - the latter turns "when this pass noticed"
            # timestamps into actual crossing times and self-heals a flag lost
            # to a restart. Only the settled post-import pass may also prune
            # rows the rewritten history no longer supports (removeUnsupported)
            # - never organic passes, where a tightened skip threshold
            # shrinking totals would delete rows only to re-notify them later.
            # While the toggle is off the flag stays raised so enabling later
            # catches up. See services/milestones.py recalculateMilestoneDates
            # for details.
            if recalcEnabled and (pending or recorded > 0):
                recalculateMilestoneDates(db.repo, username, db.tz,
                                          removeUnsupported=pending)
                localNeedsRecalc = False
            unseen = [r for r in rows if not r["seen"] and r["kind"] in EMAILED_MILESTONE_KINDS]
            if unseen:
                queue_email_notification(username, EVENT_MILESTONE_REACHED,
                                         {"milestones": [formatMilestone(r) for r in unseen]})
        except Exception as e:
            if localNeedsRecalc:
                # Retry consumed import work, including partially applied
                # recalculation. Never clear a concurrent import's new flag.
                db.raiseMilestoneRecalcFlag()
            logger.warning("Milestone detection failed for %s: %s", username, e)

    def primeMilestoneBadge(self, username: str) -> None:
        """Freeze the milestone badge count for THIS render, before the caller
        acknowledges the milestones.

        Context processors run when the template renders - i.e. AFTER the view
        function. The dashboard both shows the milestones and clears the badge
        (markMilestonesSeen), so without priming, _injectMilestoneStatus (in
        dashboard/context_processors.py) counts what is left after the clear:
        zero. The badge would then never appear on
        the very page the user landed on, and `/` is where login drops them - so
        for anyone whose first page is the dashboard the notification was
        silently consumed without ever being shown.

        Priming g with the pre-clear count gives the badge its one appearance
        alongside the card, and the next page load has nothing left to show.
        Costs nothing: the context processor's own memo means this replaces its
        read rather than adding one."""
        if "unseenMilestoneCount" not in g:
            g.milestonesEnabled = self.repo.isMilestonesEnabled()
            g.unseenMilestoneCount = (
                self.repo.getUnseenMilestoneCount(username)
                if g.milestonesEnabled and username else 0
            )

    def _rateLimited(self, bucket: str) -> bool:
        """True if this request's source IP has exceeded RATE_LIMIT_MAX_ATTEMPTS
        for `bucket` within RATE_LIMIT_WINDOW_SECONDS - callers should reject
        the request with RATE_LIMIT_ERROR_MESSAGE when this returns True."""
        identifier = request.remote_addr or "unknown"
        return not self._authRateLimiter.hit(bucket, identifier)

    def checkLogin_thread(self) -> None:
        self._ensureAllUsersLogin()
        # Stored (not just started) so /admin's Worker Health panel can report
        # this loop's liveness - it hosts the per-user milestone pass, which
        # has no thread of its own to inspect.
        self._checkLoginThread = threading.Thread(target=self._checkLoginLoop, daemon=True)
        self._checkLoginThread.start()

    def _ensureAllUsersLogin(self):
        try:
            usersWithCookies = self.repo.getAllUsersWithCookies()
        except Exception as e:
            logger.error("Error initializing users: %s", e)
            return

        for username, email in usersWithCookies:
            # Shutdown can land mid-pass, and the rest of this loop is not free:
            # each remaining user costs a get_user_db (which opens a database),
            # a health check, and a milestone pass that runs queries. The
            # per-user start paths already refuse to create threads once
            # signalled (see Database/workers/listener.py), so what this
            # prevents is the WORK, not a leak - but the shutdown budget is
            # fixed while the user count is not, so the pass has to be able to
            # stop rather than always run to the end of the list.
            if self._stop_event.is_set():
                logger.info("Stop requested - ending the user login pass early")
                return
            try:
                db = self.get_user_db(username, email)
                # If listener has crashed, marked DEAD, or its thread has stopped, restart it -
                # UNLESS the last start failed on definitively bad/mismatched cookies. Those
                # never recover by retrying (every restart re-attempts the same failing Spotify
                # login, risking bot-detection/rate-limit escalation); only a fresh re-login
                # (which rebuilds the listener via _refresh_user_session) should retry them.
                listener = db.listener
                credentialFailure = listener is not None and (listener.loginFailed or listener.contaminationDetected)
                needsRestart = db.getListenerHealth()["status"] == "DEAD" or not (
                    listener and listener.thread and listener.thread.is_alive()
                )
                if needsRestart and not credentialFailure:
                    logger.warning("Listener thread for user %s is not running or is DEAD. Restarting...", username)
                    db.startListener(email=email)
                # Folded into this existing per-user pass rather than a loop of
                # its own. On a cycle where the user's play totals haven't moved
                # it costs just the one getPlayTotals scan (the change signal) -
                # the heavier streak/top-artist queries are skipped via
                # _milestoneChangeCache; see detectMilestones.
                self._detectMilestonesSafely(db, username)
            except Exception as e:
                logger.error("Error initializing user %s: %s", username, e)

    def _checkLoginLoop(self) -> None:
        # checkLogin_thread() already ran _ensureAllUsersLogin synchronously
        # before this thread started (listeners must come up immediately), so
        # the loop's own first pass can wait out a random offset - staggering
        # the periodic re-checks against the other workers after a restart.
        if self._stop_event.wait(random.randint(LOGIN_CHECK_MIN_START_DELAY_SECONDS,
                                                LOGIN_CHECK_MAX_START_DELAY_SECONDS)):
            return
        while not self._stop_event.is_set():
            self._ensureAllUsersLogin()
            self._stop_event.wait(LOGIN_CHECK_INTERVAL_SECONDS)

    def startVersionCheck_thread(self) -> None:
        threading.Thread(target=self._versionCheckLoop, daemon=True).start()

    def _versionCheckLoop(self) -> None:
        # Check the latest published GitHub Release - not just whatever
        # Database/VERSION says on main, which can be bumped ahead of what's
        # actually been released/tagged (see .github/workflows/dockerReleaseTag.yml)
        # - shortly after startup (random offset, so a restart doesn't fire
        # every worker at once) and then every hour.
        url = "https://api.github.com/repos/i7Gamer/SpotifyStatsTracker/releases/latest"
        if self._stop_event.wait(random.randint(VERSION_CHECK_MIN_START_DELAY_SECONDS,
                                                VERSION_CHECK_MAX_START_DELAY_SECONDS)):
            return
        while not self._stop_event.is_set():
            try:
                resp = requests.get(url, timeout=VERSION_CHECK_TIMEOUT_SECONDS,
                                    headers={"Accept": "application/vnd.github+json"})
                if resp.status_code == 200:   #< a real published release to compare against
                    # Releases are tagged e.g. "1.31.0" (occasionally "v1.31.0").
                    remoteVersion = resp.json().get("tag_name", "").strip().lstrip("vV")
                    # Keep it only while it is strictly newer than what runs here.
                    try:
                        with self._version_lock:
                            isNewer = remoteVersion and versionTuple(remoteVersion) > versionTuple(self.currentVersion)
                            self.latestVersion = remoteVersion if isNewer else None
                    except Exception as e:
                        logger.warning("Ignoring malformed release tag %r: %s", remoteVersion, e)
                elif resp.status_code == 404:
                    # No release has ever been published - nothing to notify about.
                    with self._version_lock:
                        self.latestVersion = None
                else:
                    # Neither a release nor evidence that none exists - a 429 off
                    # the unauthenticated hourly cap this endpoint shares with
                    # everything else on the egress IP, or a GitHub 5xx. Leave
                    # latestVersion alone (blinking the badge off would read as
                    # the update having been withdrawn) but say so: an instance
                    # stuck on this would otherwise just never show the badge,
                    # with nothing in the log to explain it. WARNING, unlike the
                    # transport failures below, because it can't spam at one
                    # line an hour and DEBUG is off in production.
                    logger.warning("Version check got unexpected HTTP %s from %s",
                                   resp.status_code, url)
            except Exception as e:
                # Transient (DNS/TLS/GitHub outage) - debug, not warning, so an
                # offline instance doesn't spam its log every hour.
                logger.debug("Version check request failed: %s", e)

            self._stop_event.wait(VERSION_CHECK_INTERVAL_SECONDS)

    # Batched per-kind genre lookups for _attachGenres - one query per rendered
    # list, not per card (see resolveGenresForTracks' degrade-to-{} contract).
    _GENRE_RESOLVERS = {
        "track": resolveGenresForTracks,
        "album": resolveGenresForAlbums,
        "artist": resolveGenresForArtists,
    }

    # The three routes whose responses embed Spotify's iFrame API and therefore
    # need the eval-allowing CSP variant (DETAIL_PAGE_CSP). Confined here so
    # 'unsafe-eval' never leaks onto the rest of the app.
    _DETAIL_CSP_ENDPOINTS = frozenset({"songDetailPage", "artistDetailPage", "albumDetailPage"})

    def _staticVersionStamp(self, filename):
        """The value appended to a /static URL as ``?v=`` - the asset's mtime,
        or None when there is no file behind the name (a typo'd asset must
        still 404 in the browser rather than break the page render).

        Deliberately not memoized: one stat() per asset per render is nothing
        next to this app's queries, and caching would leave a developer editing
        static/js/*.js serving yesterday's URL until the next restart."""
        try:
            return str(int(os.stat(os.path.join(self.app.static_folder, filename)).st_mtime))
        except OSError:
            return None

    def getDeployMismatch(self):
        """Whether this process is still running the files on disk, for the
        /admin banner - None when it is. See services/deploy_state.py for the
        failure this exists to name; the short version is that the stamping
        above is what makes it so confusing, since it cache-busts the new
        scripts into a page rendered from the old templates.

        Read live rather than at boot, and not memoized: the whole point is to
        notice a change made after this process started, and /admin is not a
        page anyone loads in a loop."""
        diskVersion = None
        with suppress(Exception):
            diskVersion = (self.baseDir / "Database" / "VERSION").read_text(encoding="utf-8").strip()
        return deployMismatch(self.currentVersion, diskVersion,
                              self._bootFingerprint, sourceFingerprint(self.baseDir))

    def _registerSlowRequestLogging(self) -> None:
        @self.app.before_request
        def _stampSlowRequest():
            """Capture a monotonic start time before any other request hook."""
            g._slowRequestStartedAt = time.monotonic()

        @self.app.teardown_request
        def _logSlowRequest(error):
            """Log slow requests without exposing paths, query strings, or tokens."""
            startedAt = getattr(g, "_slowRequestStartedAt", None)
            if startedAt is None:
                return
            del g._slowRequestStartedAt

            duration = time.monotonic() - startedAt
            if duration <= SLOW_REQUEST_LOG_THRESHOLD_SECONDS:
                return

            urlRule = getattr(request.url_rule, "rule", None) or SLOW_REQUEST_UNMATCHED_ROUTE
            logger.warning("Slow request method=%s route=%s duration=%.3fs",
                           request.method, urlRule, duration)

    def registerRoutes(self) -> None:
        @self.app.url_defaults
        def _versionStaticUrl(endpoint, values):
            """Make a changed static file a cache MISS instead of a
            revalidation the browser is free to skip.

            Flask serves /static with a bare ``no-cache``, which asks the
            browser to revalidate but doesn't force it to - so a copy could
            outlive its file. That was survivable while each file stood alone;
            it stopped being survivable once nine page loaders came to depend
            on one shared function in ajax-status.js, because a browser holding
            the older shared file broke every AJAX page at once (dashboard,
            /history, /charts, the top lists) with no way out but a hard
            reload. Stamping the URL with the file's mtime means a deploy
            changes the URL, and a URL the browser has never seen cannot be
            answered from its cache."""
            if endpoint != "static" or "filename" not in values:
                return
            stamp = self._staticVersionStamp(values["filename"])
            if stamp:
                values[STATIC_VERSION_PARAM] = stamp

        @self.app.before_request
        def _endSessionsTheAccountHasInvalidated():
            """Drop a cookie whose session_version the account has moved past,
            once, before anything else looks at it.

            A hook rather than a check per route, because "reads the session"
            is a much bigger set than "is decorated with @requiresUser": the
            public /overview page decides on its own whether to add the
            viewer's account block, the image routes authorize straight off
            session["email"], and the context processors behind the topbar read
            session["username"] on every render including anonymous pages. A
            guard repeated at each of those is one someone forgets - the same
            reasoning routes/_auth.py's decorator exists for. Clearing here
            makes every one of them see an ordinary anonymous request.

            Static assets are skipped: they carry no account data, are
            requested dozens at a time, and a signed-out browser still needs
            the stylesheet for the login page it is about to be shown.

            Registered after CSRFProtect's own before_request (constructed in
            __init__), so a POST is still validated against the session that
            signed its token - the caller gets the ordinary logged-out redirect
            rather than a CSRF error."""
            email = session.get("email")
            if request.endpoint == STATIC_ENDPOINT or not email:
                return
            username = self.get_username_for_email(email)
            #< an email with no account behind it is NOT a session somebody
            #  signed out - clearing it here would take over a case the routes
            #  have always had their own answer for (get_or_create_user), for a
            #  cookie this feature has nothing to say about
            if username and not self.sessionIsCurrent(username):
                session.clear()

        @self.app.after_request
        def _setSecurityHeaders(response):
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            if request.endpoint in self._DETAIL_CSP_ENDPOINTS:
                response.headers["Content-Security-Policy"] = DETAIL_PAGE_CSP
            if _hstsEnabled():
                response.headers.setdefault("Strict-Transport-Security", HSTS_HEADER_VALUE)
            # Everything the app itself renders is one account's data, so none of
            # it may be stored: without this a logout takes away the SESSION but
            # not the pages, and Back replays a fully rendered dashboard out of
            # the browser's back/forward cache with no request and no session
            # check - which is a shared browser handing the next person the
            # previous account's listening history. Clearing htmx's snapshot
            # cache on the login page covers only the entries htmx owns.
            #
            # Static assets are exempt: they carry no account data, and this app
            # ships htmx plus a chart bundle that would otherwise be re-fetched
            # on every navigation.
            if request.endpoint != STATIC_ENDPOINT:
                response.headers.setdefault("Cache-Control", NO_STORE_CACHE_CONTROL)
            return response

        @self.app.template_filter("displayName")
        def _displayNameFilter(username):
            """Resolve a username to the label people actually see (see
            users.display_name). Applied only where a name is DISPLAYED - the
            `/img/<username>/` segment, `?with=`, and the admin route params are
            the immutable key and must stay raw.

            Memoized per request on `g`: one render names the same user several
            times (the compare headings alone name two users six times) and a
            share list names a dozen, so the un-memoized version would be a
            query per mention rather than per user. A falsy value passes
            through untouched so layout.html's `session.get('username') or
            'Account'` fallback still works on a logged-out page."""
            if not username:
                return username
            cache = getattr(g, "_displayNames", None)
            if cache is None:
                cache = {}
                g._displayNames = cache
            if username not in cache:
                cache[username] = self.repo.getDisplayName(username)
            return cache[username]

        registerContextProcessors(self.app, self)

        registerSystemRoutes(self.app, self)

        registerMediaRoutes(self.app, self)

        @self.app.errorhandler(413)
        def _uploadTooLarge(error):
            return redirect(url_for("importPage", error="upload_too_large"))

        registerAuthRoutes(self.app, self)

        registerChartsRoutes(self.app, self)

        registerDetailsRoutes(self.app, self)

        registerGenresRoutes(self.app, self)

        registerAdminRoutes(self.app, self)

        registerWrappedRoutes(self.app, self)

        registerCompareRoutes(self.app, self)

        registerTagsRoutes(self.app, self)

    def startWorkers(self):
        """Start the background workers. Call once, from the entry point, after
        construction.

        Kept out of __init__ because constructing this object should not have
        side-effects on the world: these start four threads, and
        checkLogin_thread's synchronous first pass additionally opens every
        user's database and performs a network-bound Spotify login per user.
        A caller that only wants the WSGI app (a test, a CLI subcommand) would
        otherwise have to patch each one out individually - and EMAIL_WORKER,
        being a module-level singleton, would have its repo rebound by whichever
        app was constructed last.

        Idempotent: wsgi.py's module-level construction and run() would
        otherwise both start the login/version loops, neither of which guards
        against a second thread of itself."""
        if self._workersStarted:
            return
        self._workersStarted = True
        # Tell the listener patches how to read the push-mode kill switch. It
        # lives three layers below where the decision is made (Database ->
        # Listener -> Spotify.startRecentlyPlayedListener -> LastPlayedManger,
        # which starts its own thread), and the setting is instance-wide, so a
        # hook beats threading a flag through all of them. Set before any
        # listener starts; unset, the patches stay on polling.
        patch_push_listener_hook(self.repo.isPushListenerEnabled)
        self.backupWorker.start()
        # Bind before start: the worker polls immediately, and an unbound one
        # opens a throwaway connection per job (see EmailWorker.process_one).
        EMAIL_WORKER.bind_repo(self.repo)
        EMAIL_WORKER.start()
        self.startVersionCheck_thread()
        self.checkLogin_thread()

    def shutdown(self):
        self._stop_event.set()
        # Per worker, deliberately: these two run before the per-user loop
        # below, which is the part that already tolerates a failing member. An
        # exception up here aborted shutdown before a single user's threads
        # were even signaled - leaving exactly the outliving-threads state the
        # two-phase dance exists to prevent.
        for worker in (self.backupWorker, EMAIL_WORKER):
            try:
                worker.stop()
            except Exception as e:
                logger.error("Error stopping %s: %s", type(worker).__name__, e)
        with self._db_lock:
            databases = list(self.user_databases.values())
        # Two-phase: SIGNAL every user's stop flags first (no joins), THEN
        # join. While user A's threads were being joined, user B's still-
        # running listener used to hit its stale-feed check and resurrect
        # itself mid-shutdown (the 2026-07-17 hang); with every stop flag
        # already set, the reconnect paths refuse instead.
        for db in databases:
            try:
                db.signalStop()
            except Exception as e:
                logger.error("Error signaling stop for %s: %s", db.user, e)
        try:
            self._stopDatabasesConcurrently(databases)
        finally:
            self._shutdownMediaPools()

    def _shutdownMediaPools(self) -> None:
        """Retire the three process-wide media pools.

        LAST, not first: every per-user thread that submits media work - the
        listener's appendTrackData -> saveImagesFromTrack, a page render's
        lazyFetch* - is alive until the phase-2 join returns, and
        shutdownWorkerPools installs a live REPLACEMENT pool. Retiring the
        pools any earlier would hand that whole window a fresh pool nothing
        stops again, leaving the interpreter's atexit hook to do it: the very
        thing this exists to avoid.

        Reached from a `finally`, because phase 2 is the one part of shutdown
        with no guard of its own - it starts a thread per user and joins them,
        so a second Ctrl+C landing in a join, or a process that cannot start
        another thread, used to skip the retirement entirely. Guarded like the
        two workers above so a raise here cannot escape either."""
        try:
            Database.shutdownWorkerPools()
        except Exception as e:
            logger.error("Error stopping the shared media thread pools: %s", e)

    def _stopDatabaseQuietly(self, db) -> None:
        """One user's phase-2 stop. Failures are logged, never raised: this
        runs on its own thread, where an exception would be reported by the
        excepthook and take that user's stop with it silently."""
        try:
            db.stop()
        except Exception as e:
            logger.error("Error stopping database for %s: %s", db.user, e)

    def _stopDatabasesConcurrently(self, databases) -> None:
        """Phase 2, all users at once.

        Each user's stop() is a chain of bounded joins - two on the listener,
        one on the auto-import watchdog, five on the periodic workers - which
        adds up to roughly USER_STOP_JOIN_TIMEOUT_SECONDS in the worst case
        where every thread is wedged. Run one user after another that was the
        worst case TIMES the number of users, growing past any container's stop
        grace period as an instance gained users (see the compose file's
        stop_grace_period). Run together it stays one user's worth however many
        there are, and the common case - every thread parked on its stop event -
        is immediate either way.

        Starting them together is not enough on its own: join(timeout=) waits
        out the WHOLE timeout on a thread that is still running, so joining N
        wedged users at the full timeout each costs N times the budget however
        much they overlapped. The deadline below is therefore shared - each
        join gets what is left of it, and once it is spent the rest return
        immediately.

        Safe to overlap because phase 1 has already signaled everyone: no
        user's stop can revive another's listener, which is the property the
        two-phase split exists for."""
        stoppers = []
        for db in databases:
            thread = threading.Thread(target=self._stopDatabaseQuietly, args=(db,),
                                      name=f"{SHUTDOWN_THREAD_NAME_PREFIX}{db.user}", daemon=True)
            thread.start()
            stoppers.append((db, thread))
        deadline = time.monotonic() + USER_STOP_JOIN_TIMEOUT_SECONDS
        for db, thread in stoppers:
            # Bounded even though stop() is: a wedged user must not hold the
            # process past the grace period it is racing, and the threads are
            # daemons, so one that outlives this dies with the interpreter.
            allowance = max(0, deadline - time.monotonic())
            thread.join(timeout=allowance)
            if thread.is_alive():
                # The ALLOWANCE, not the constant. The budget is shared, so a
                # user reached after it is spent is joined for ~0s - reporting
                # the constant said it had been given the full 30 and used all
                # of it, which during a shutdown hang points the investigation
                # at the wrong user entirely. The budget rides along so a 0.0s
                # allowance reads as "this queued behind others" rather than as
                # a bug in the timeout itself.
                logger.warning("Database for %s did not stop within its %.1fs of the shared "
                               "%ss shutdown budget - continuing shutdown",
                               db.user, allowance, USER_STOP_JOIN_TIMEOUT_SECONDS)

    def run(self) -> None:
        try:
            self.startWorkers()
            #< the same helper every diagnostic gate reads, so Flask's debug mode
            #  and the app's verbose logging can never disagree about one value
            debug = flaskDebugEnabled()
            # use_debugger=False on purpose (F-B-2, 2026-09-04 review): Flask's
            # own app.run() does `options.setdefault("use_debugger", self.debug)`,
            # so debug=True alone also switched on the interactive Werkzeug
            # console on 0.0.0.0 - reachable to anyone on the LAN, guarded only
            # by a PIN whose derivation is a known target. FLASK_DEBUG is
            # documented (README, docker-compose.yml) as a logging knob, not as
            # "expose a code-execution console"; debug= still drives
            # flaskDebugEnabled()'s shared logging gate.
            self.app.run(host="0.0.0.0", debug=debug, use_debugger=False,
                         port=DEFAULT_PORT, use_reloader=False)#, threaded=False)
        finally:
            self.shutdown()


if __name__ == "__main__":
    # Handy dev-shell settings:  $env:IMPORT_KEYWORD="Weekly"   $env:TZ="America/Los_Angeles"
    SpotifyDashboardApp().run()
