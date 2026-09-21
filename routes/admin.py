# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Admin-only routes: the /admin console and its settings/action endpoints.

Extracted verbatim from app.py. Every handler is fully gated on
Repository.isAdmin. register(app, dashboard) wires them via app.add_url_rule
under their original endpoint names.
"""
import logging
import os
import sqlite3
import threading
import time

from flask import render_template, redirect, request, url_for, abort, jsonify

from routes._auth import makeRequiresUser
from routes._xhr import declaresItselfXhr

from config import (
    RECOMMENDATION_ARTIST_LIMIT, TRUTHY_ENV_VALUES,
    ALLOW_INSTANCE_RESTART_ENV_VAR, INSTANCE_RESTART_DELAY_SECONDS,
)
from Database.database import (
    Database, IMAGE_DOWNLOAD_WORKERS, ARTIST_BIO_FETCH_WORKERS, ALBUM_BIO_FETCH_WORKERS,
)
from Database.repository import (
    SKIP_MODE_SECONDS, SKIP_MODE_PERCENT,
    SKIP_SECONDS_MIN, SKIP_SECONDS_MAX, SKIP_PERCENT_MIN, SKIP_PERCENT_MAX,
    DISCOVER_ARTIST_LIMIT_KEY, DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX,
    IMAGE_DOWNLOAD_WORKERS_KEY, ARTIST_BIO_FETCH_WORKERS_KEY, ALBUM_BIO_FETCH_WORKERS_KEY,
    WORKER_COUNT_MIN, WORKER_COUNT_MAX,
    COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX,
    BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX,
    BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX,
    GENRE_BACKFILL_RETRY_DAYS_KEY, BIO_BACKFILL_RETRY_DAYS_KEY,
    BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX,
)
from Database.backup import DEFAULT_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_RETENTION_COUNT
from Database.rate_limit import SPOTIFY_LIMITER
from Database.patches import totpAuthSnapshot
from Database.utils import convertToDatetime, SECONDS_PER_DAY
from services.email_service import (
    get_smtp_config, save_smtp_config, send_test_email,
    get_instance_public_url, save_instance_public_url,
    DEFAULT_SMTP_PORT, DEFAULT_SMTP_ENCRYPTION, DEFAULT_SMTP_FROM_NAME,
    _isValidFromEmail, _isValidPublicUrl,
)
from services.email_worker import EMAIL_WORKER

logger = logging.getLogger(__name__)

# How long a manual "Create Backup Now" request waits for the snapshot before
# returning and letting it finish in the background. The common case (a small
# database) completes well within this and reports the filename synchronously;
# only a large database exceeds it, and there we return rather than hold the
# request thread open (and risk an HTTP timeout).
MANUAL_BACKUP_SYNC_WAIT_SECONDS = 20

# The live-miss ratio's rolling window: played_at (indexed) rather than
# created_at (not indexed) - see implementationPlan-2026-09-07.md section 5.
LIVE_MISS_WINDOW_DAYS = 30

# A backfill row recorded within this many seconds of created_at - played_at
# counts as a genuinely MISSED live catch; slower ones are late-arriving
# offline listening recovered separately, not a live-path failure. Matches the
# ~16-minute recently-played sweep interval validated in
# eventDrivenConnectStatePlan.md.
PROMPT_SWEEP_SECONDS = 16 * 60

# Display rounding for the live-miss ratio badge - "%.1f%%" needs an explicit
# digit count too, so the fraction isn't a bare literal in the format string.
LIVE_MISS_RATIO_DISPLAY_DECIMALS = 1


def _pushWithoutBackfill(pushEnabled: bool, backfillEnabled: bool, users: list[dict]) -> dict | None:
    """The push-listener warning section's payload - see
    implementationPlan-2026-09-07.md section 3.

    Returns None when there's nothing to warn about, {"backfillDisabled":
    True} when every push listener is unprotected because the global Web API
    backfill toggle is off, or {"usernames": [...]} naming which push
    listeners lack a working API backfill of their own.

    `users` carries the already-computed per-user values from adminPage's own
    loop: each entry is {"username", "cookies_json", "hasApi", "needsReauth"}.
    A user counts as "exposed" only if they actually have a listener
    (cookies_json - push only runs for stored-cookie users, NOT
    dashboard.user_databases, which also covers accounts merely viewed
    through a share/compare link) and either lack API credentials entirely or
    have credentials Spotify has stopped honouring (needsReauth) -
    stored-but-dead credentials protect nobody."""
    if not pushEnabled:
        return None
    if not backfillEnabled:
        return {"backfillDisabled": True}
    exposed = [
        {"username": u["username"], "hasCookies": True,
         "hasApi": u["hasApi"], "needsReauth": u["needsReauth"]}
        for u in users
        if u.get("cookies_json") and (not u["hasApi"] or u["needsReauth"])
    ]
    return {"usernames": exposed} if exposed else None


def _liveMissRatio(counts: dict) -> dict | None:
    """The admin ledger's per-user Live miss ratio (30d) - see
    implementationPlan-2026-09-07.md section 5.

    `counts` is one user's row from Repository.getPlaySourceCountsByUser:
    {"live", "prompt_backfill", "late_backfill"}. Returns
    {"pct", "live", "missed", "late"}, or None when live + missed == 0 (no
    prompt-window signal at all - the user is omitted from the row rather than
    shown a meaningless 0%). `pct` is missed / (live + missed) * 100 as a raw
    float; a tiny nonzero ratio can still round to "0.0%" once the template
    formats it - that's a display fact, not a reason to treat it as None."""
    live = counts.get("live", 0) or 0
    missed = counts.get("prompt_backfill", 0) or 0
    late = counts.get("late_backfill", 0) or 0
    total = live + missed
    if total == 0:
        return None
    return {"pct": missed / total * 100, "live": live, "missed": missed, "late": late}


def _listenerSessionLedger(health: dict, tz) -> dict | None:
    """The Worker Health card's listener-session entry for one user: sessions
    built since process start, plus a "when - why" line for the last rebuild
    (shown as the badge's tooltip). None when the snapshot carries no ledger
    (a Database predating it, or a test mock), so the template skips the badge
    instead of rendering zeros."""
    builds = health.get("session_builds")
    if not builds:
        return None
    parts = []
    rebuiltAt = health.get("last_rebuild_time")
    if rebuiltAt:
        try:
            parts.append(convertToDatetime(rebuiltAt, tz=tz).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: S110 - an unformattable timestamp costs the tooltip its
            pass           #  date, not the page
    reason = health.get("last_rebuild_reason")
    if reason:
        parts.append(reason)
    return {"builds": builds, "last_rebuild": " - ".join(parts) or None}


def register(app, dashboard):
    # The pre-bound admin flavour (see routes/_auth.py): logged-in + isAdmin,
    # or 403; anonymous redirects to login with next=/admin. This replaced 13
    # hand-rolled copies of the same four-line preamble - the exact
    # forgettable-guard situation makeRequiresUser was written to close.
    # adminRefreshLastfmEntity keeps its own guard: its login redirect targets
    # the detail page the button lives on, not /admin.
    requiresAdmin = makeRequiresUser(dashboard)(admin=True)

    def _saveClampedIntSetting(field, key, lo, hi):
        """The admin forms' shared numeric-field rule - previously four
        hand-rolled copies (Last.fm retry days, completion percent, backup
        interval/retention, tuning): read the form field, store it clamped
        into [lo, hi] via setIntSetting, and leave the stored value alone on
        a blank or unparseable input. Note form values are STRINGS, so a
        literal "0" is truthy and passes the `not raw` guard - the backup
        form's 0-means-disable fields save fine (one copy spelled the guard
        `raw is None or raw == ""` out of caution; same behavior)."""
        raw = request.form.get(field)
        if not raw:
            return
        try:
            dashboard.repo.setIntSetting(key, int(raw), lo, hi)
        except (TypeError, ValueError):
            pass

    @requiresAdmin
    def adminPage(username, db):
        """Every admin-only setting/view for the instance: the full
        users table (with per-account admin promote/demote), the 8
        feature/backfill toggles regrouped into 3 logical categories, and
        read-only instance-wide insights. Fully gated (unlike
        overviewPage, which stays visible to everyone) since there's
        nothing here for a non-admin to see."""

        users_list = []
        # One grouped scan for every user's play/skip counts instead of a
        # getPlaysCount()+getSkipCount() pair per user (2*N queries).
        countsByUser = dashboard.repo.getPlayAndSkipCountsByUser()
        # Same shape of saving: one grouped scan for every user's live-miss
        # source counts, rather than a per-user query. The window is rolling
        # from now, not "since process start" like the sessions ledger above -
        # see LIVE_MISS_WINDOW_DAYS.
        sourceCountsByUser = dashboard.repo.getPlaySourceCountsByUser(
            time.time() - LIVE_MISS_WINDOW_DAYS * SECONDS_PER_DAY, PROMPT_SWEEP_SECONDS)
        # The push-warning section's input: only the per-user fields
        # _pushWithoutBackfill actually needs, collected as the loop below
        # computes them anyway (see its own has_api/needs_reauth).
        pushExposureCandidates = []
        for u in dashboard.repo.getAllUsersDetails():
            u_username = u["username"]
            u_email = u["email"]
            has_lastfm_key = bool(u.get("lastfm_api_key"))

            # dashboard.user_databases only holds a Database for a user with
            # an already-active session (started by their own login/usage) -
            # deliberately NOT dashboard.get_user_db(), which would construct
            # one on demand and start its listener/auto-importer/worker
            # threads (a live Spotify poll included) just to report status.
            # A user who isn't currently active is reported as "Inactive"
            # rather than paying that cost to find out.
            u_db = dashboard.user_databases.get(u_username)

            listener_sessions = None
            if u["cookies_json"]:
                if u_db is not None:
                    health = u_db.getListenerHealth()
                    sync_status = health.get("status", "UNKNOWN")
                    listener_sessions = _listenerSessionLedger(health, db.tz)
                else:
                    sync_status = "Inactive"
            else:
                sync_status = "Not Configured"

            has_api = bool(u["spotify_client_id"] and u["spotify_refresh_token"])
            needs_reauth = bool(u.get("spotify_needs_reauth"))
            pushExposureCandidates.append({
                "username": u_username, "cookies_json": u["cookies_json"],
                "hasApi": has_api, "needsReauth": needs_reauth,
            })

            # Per-user background worker statuses for the Worker Health panel.
            # consecutive_failures/failure_rate/last_error are only populated
            # for the 5 periodic workers with cycle telemetry (see
            # Database/workers/telemetry.py) - auto_importer's watchdog loop
            # lives outside Database/workers/ and has no equivalent counters.
            _telemetryDefaults = {"consecutive_failures": 0, "failure_rate": 0.0, "last_error": None}
            spotify_api_worker = {"configured": has_api, "running": False, **_telemetryDefaults}
            genre_worker = {"configured": has_lastfm_key, "running": False, **_telemetryDefaults}
            album_bio_worker = {"configured": has_lastfm_key, "running": False, **_telemetryDefaults}
            artist_bio_worker = {"configured": has_lastfm_key, "running": False, **_telemetryDefaults}
            auto_importer_worker = {"configured": True, "running": False}
            wrapped_worker = {"configured": True, "running": False, **_telemetryDefaults}

            if u_db is not None:
                try:
                    if hasattr(u_db, "getSpotifyApiWorkerStatus"):
                        st = u_db.getSpotifyApiWorkerStatus()
                        if isinstance(st, dict):
                            spotify_api_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                                   "consecutive_failures": st.get("consecutive_failures", 0),
                                                   "failure_rate": st.get("failure_rate", 0.0),
                                                   "last_error": st.get("last_error")}
                except Exception as e:
                    logger.warning("Spotify API worker status lookup failed for %s: %s", u_username, e)

                if has_lastfm_key:
                    try:
                        workerStatus = u_db.getLastfmWorkerStatus()
                        if isinstance(workerStatus, dict):
                            genre_worker = {"configured": bool(workerStatus.get("configured")), "running": bool(workerStatus.get("running")),
                                             "consecutive_failures": workerStatus.get("consecutive_failures", 0),
                                             "failure_rate": workerStatus.get("failure_rate", 0.0),
                                             "last_error": workerStatus.get("last_error")}
                    except Exception as e:
                        logger.warning("Last.fm worker status lookup failed for %s: %s", u_username, e)

                    try:
                        if hasattr(u_db, "getLastfmAlbumBiographyWorkerStatus"):
                            st = u_db.getLastfmAlbumBiographyWorkerStatus()
                            if isinstance(st, dict):
                                album_bio_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                                     "consecutive_failures": st.get("consecutive_failures", 0),
                                                     "failure_rate": st.get("failure_rate", 0.0),
                                                     "last_error": st.get("last_error")}
                    except Exception as e:
                        logger.warning("Last.fm album bio worker status lookup failed for %s: %s", u_username, e)

                    try:
                        if hasattr(u_db, "getLastfmBiographyWorkerStatus"):
                            st = u_db.getLastfmBiographyWorkerStatus()
                            if isinstance(st, dict):
                                artist_bio_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                                      "consecutive_failures": st.get("consecutive_failures", 0),
                                                      "failure_rate": st.get("failure_rate", 0.0),
                                                      "last_error": st.get("last_error")}
                    except Exception as e:
                        logger.warning("Last.fm artist bio worker status lookup failed for %s: %s", u_username, e)

                try:
                    if hasattr(u_db, "getAutoImporterWorkerStatus"):
                        st = u_db.getAutoImporterWorkerStatus()
                        if isinstance(st, dict):
                            auto_importer_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running"))}
                except Exception as e:
                    logger.warning("AutoImporter worker status lookup failed for %s: %s", u_username, e)

                try:
                    if hasattr(u_db, "getWrappedWorkerStatus"):
                        st = u_db.getWrappedWorkerStatus()
                        if isinstance(st, dict):
                            wrapped_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                               "consecutive_failures": st.get("consecutive_failures", 0),
                                               "failure_rate": st.get("failure_rate", 0.0),
                                               "last_error": st.get("last_error")}
                except Exception as e:
                    logger.warning("Wrapped worker status lookup failed for %s: %s", u_username, e)

            created_at_val = u.get("created_at")
            created_date_str = ""
            if created_at_val:
                try:
                    created_date_str = convertToDatetime(created_at_val, tz=db.tz).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:  # noqa: S110 - an unformattable created_at renders as a blank
                    pass           #  cell rather than 500-ing the whole admin table

            users_list.append({
                "username": u_username,
                #< the raw column, NOT the displayName filter: admin.html renders this
                #  as a sub-line UNDER the username, which stays the immutable account
                #  key every admin action addresses. .get() matches lastfm_api_key
                #  below - getAllUsersDetails returns plain dicts.
                "display_name": u.get("display_name"),
                "email": u_email,
                "is_admin": u["is_admin"],
                "sync_status": sync_status,
                "listener_sessions": listener_sessions,
                "live_miss": _liveMissRatio(sourceCountsByUser.get(u_username, {})),
                "spotify_api_status": "Needs Re-Auth" if (has_api and needs_reauth) else ("Configured" if has_api else "Not Configured"),
                #< .get(): raw row presence check only - the stored key
                #  is encrypted and never needs decrypting here
                "lastfm_api_status": "Configured" if u.get("lastfm_api_key") else "Not Configured",
                "genre_worker": genre_worker,
                "spotify_api_worker": spotify_api_worker,
                "album_bio_worker": album_bio_worker,
                "artist_bio_worker": artist_bio_worker,
                "auto_importer_worker": auto_importer_worker,
                "wrapped_worker": wrapped_worker,
                "plays_count": countsByUser.get(u_username, {}).get("plays", 0),
                "skips_count": countsByUser.get(u_username, {}).get("skips", 0),
                "created_at": created_date_str,
            })

        push_listener_enabled = dashboard.repo.isPushListenerEnabled()
        spotify_backfill_enabled = dashboard.repo.isSpotifyApiBackfillEnabled()
        push_backfill_warning = _pushWithoutBackfill(
            push_listener_enabled, spotify_backfill_enabled, pushExposureCandidates)

        listener_summary: dict[str, int] = {}
        for u in users_list:
            listener_summary[u["sync_status"]] = listener_summary.get(u["sync_status"], 0) + 1

        spotify_api_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        lastfm_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        lastfm_album_bio_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        lastfm_artist_bio_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        auto_importer_worker_summary = {"running": 0, "idle": 0}
        wrapped_worker_summary = {"running": 0, "idle": 0, "failing": 0}

        def _isFailing(w: dict) -> bool:
            return w["configured"] and w["consecutive_failures"] >= Database.WORKER_HEALTH_FAILING_THRESHOLD

        for u in users_list:
            # Spotify API Backfill
            w = u["spotify_api_worker"]
            if not w["configured"]:
                spotify_api_worker_summary["no_key"] += 1
            elif w["running"]:
                spotify_api_worker_summary["running"] += 1
            else:
                spotify_api_worker_summary["idle"] += 1
            if _isFailing(w):
                spotify_api_worker_summary["failing"] += 1

            # Last.fm Genre
            w = u["genre_worker"]
            if not w["configured"]:
                lastfm_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_worker_summary["running"] += 1
            else:
                lastfm_worker_summary["idle"] += 1
            if _isFailing(w):
                lastfm_worker_summary["failing"] += 1

            # Last.fm Album Bio
            w = u["album_bio_worker"]
            if not w["configured"]:
                lastfm_album_bio_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_album_bio_worker_summary["running"] += 1
            else:
                lastfm_album_bio_worker_summary["idle"] += 1
            if _isFailing(w):
                lastfm_album_bio_worker_summary["failing"] += 1

            # Last.fm Artist Bio
            w = u["artist_bio_worker"]
            if not w["configured"]:
                lastfm_artist_bio_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_artist_bio_worker_summary["running"] += 1
            else:
                lastfm_artist_bio_worker_summary["idle"] += 1
            if _isFailing(w):
                lastfm_artist_bio_worker_summary["failing"] += 1

            # AutoImporter
            w = u["auto_importer_worker"]
            if w["running"]:
                auto_importer_worker_summary["running"] += 1
            else:
                auto_importer_worker_summary["idle"] += 1

            # Wrapped Worker
            w = u["wrapped_worker"]
            if w["running"]:
                wrapped_worker_summary["running"] += 1
            else:
                wrapped_worker_summary["idle"] += 1
            if _isFailing(w):
                wrapped_worker_summary["failing"] += 1

        # Thread liveness alone said nothing about whether backups were being
        # TAKEN: a service failing every 15-minute cycle read RUNNING like any
        # other. The worker now reports the same cycle telemetry as the
        # per-user backfillers, judged here against the same threshold.
        backupWorker = getattr(dashboard, "backupWorker", None)
        backup_worker_summary = {"status": "INACTIVE", "consecutive_failures": 0,
                                 "failure_rate": 0.0, "last_error": None}
        if backupWorker is not None:
            backup_worker_summary = backupWorker.getSummary()
        backup_worker_summary["failing"] = (
            backup_worker_summary["consecutive_failures"] >= Database.WORKER_HEALTH_FAILING_THRESHOLD)

        # Milestone detection has no thread of its own - it rides the periodic
        # login-check loop (see _detectMilestonesSafely), so its health IS that
        # thread's liveness. DISABLED reflects the admin kill switch (the pass
        # no-ops then regardless of the thread); recalc_enabled surfaces the
        # import-hygiene toggle as a warning badge, since with it off imports
        # silently stop recalculating dates / suppressing badge floods.
        loginCheckThread = getattr(dashboard, "_checkLoginThread", None)
        if not dashboard.repo.isMilestonesEnabled():
            milestone_status = "DISABLED"
        elif loginCheckThread is not None and loginCheckThread.is_alive():
            milestone_status = "RUNNING"
        else:
            milestone_status = "INACTIVE"
        milestone_worker_summary = {
            "status": milestone_status,
            "recalc_enabled": dashboard.repo.isMilestoneRecalcEnabled(),
        }

        email_worker_summary = EMAIL_WORKER.get_summary(dashboard.repo)

        # Instance-wide, not per-user: every listener and worker shares one
        # Spotify request budget because Spotify enforces its limits per IP
        # (see Database/rate_limit.py). Before this, a rate-limit event was a
        # log line nobody counted - the only way to answer "is this getting
        # worse?" was to grep app.log.
        spotify_rate_limit = SPOTIFY_LIMITER.snapshot()

        # Spotify rotates the TOTP secret the web player authenticates with, and
        # this build pins it (see Database/patches.py). A rotation takes every
        # user's session down at once and its only other trace is a log line, so
        # it belongs on the panel someone actually opens when "nothing works".
        spotify_totp = totpAuthSnapshot()

        skip_mode, skip_value = dashboard.repo.getSkipThreshold()
        # .strip() before .lower(): Docker's --env-file/`-e KEY=VALUE ` pass
        # surrounding whitespace through untouched; other env-flag readers in
        # the app already strip before comparing (see adminRestart's gate,
        # which reads this same variable).
        restart_enabled = os.environ.get(ALLOW_INSTANCE_RESTART_ENV_VAR, "").strip().lower() in TRUTHY_ENV_VALUES

        # Run live rather than reusing the startup probe's result: the number's
        # value is in noticing when it CHANGES, and a boot-time figure would
        # still show the old count after a repair migration had cleared it -
        # which is exactly when someone is looking. ~240 ms on a 105 MB
        # database (quick_check, not integrity_check - see checkIntegrity), on
        # an admin-only page that already does heavier work per render.
        database_integrity = dashboard.repo.checkIntegrity()
        dangling_row_total = sum(database_integrity["foreignKeyViolations"].values())

        current_tab = request.args.get("tab", "overview").lower()
        if current_tab not in ("overview", "workers", "settings"):
            current_tab = "overview"

        return render_template(
            "admin.html",
            #< a deploy that copied files over a running process, which nothing
            #  else in the app can see (see services/deploy_state.py)
            deploy_mismatch=dashboard.getDeployMismatch(),
            restart_enabled=restart_enabled,
            users_list=users_list,
            admin_count=len(dashboard.repo.getAdminUsernames()),
            spotify_backfill_enabled=spotify_backfill_enabled,
            push_listener_enabled=push_listener_enabled,
            push_backfill_warning=push_backfill_warning,
            lastfm_backfill_enabled=dashboard.repo.isLastfmGenreBackfillEnabled(),
            sharing_enabled=dashboard.repo.isDataSharingEnabled(),
            inherited_genres_enabled=dashboard.repo.isInheritedGenresEnabled(),
            skip_mode=skip_mode,
            skip_value=skip_value,
            skip_mode_seconds=SKIP_MODE_SECONDS,
            skip_mode_percent=SKIP_MODE_PERCENT,
            skip_seconds_min=SKIP_SECONDS_MIN, skip_seconds_max=SKIP_SECONDS_MAX,
            skip_percent_min=SKIP_PERCENT_MIN, skip_percent_max=SKIP_PERCENT_MAX,
            discover_artist_limit=dashboard.repo.getDiscoverArtistLimit(RECOMMENDATION_ARTIST_LIMIT),
            image_download_workers=dashboard.repo.getImageDownloadWorkers(IMAGE_DOWNLOAD_WORKERS),
            artist_bio_workers=dashboard.repo.getArtistBioFetchWorkers(ARTIST_BIO_FETCH_WORKERS),
            album_bio_workers=dashboard.repo.getAlbumBioFetchWorkers(ALBUM_BIO_FETCH_WORKERS),
            discover_min=DISCOVER_ARTIST_LIMIT_MIN, discover_max=DISCOVER_ARTIST_LIMIT_MAX,
            worker_min=WORKER_COUNT_MIN, worker_max=WORKER_COUNT_MAX,
            completion_complete_percent=dashboard.repo.getCompletionCompletePercent(),
            completion_min=COMPLETION_COMPLETE_PERCENT_MIN, completion_max=COMPLETION_COMPLETE_PERCENT_MAX,
            email_verification_enabled=dashboard.repo.isEmailVerificationEnabled(),
            milestone_recalc_enabled=dashboard.repo.isMilestoneRecalcEnabled(),
            friends_now_playing_enabled=dashboard.repo.isFriendsNowPlayingEnabled(),
            track_merge_enabled=dashboard.repo.isTrackMergeEnabled(),
            track_merge_preview=(None if dashboard.repo.isTrackMergeEnabled()
                                 else dashboard.repo.previewMergeTracksByIsrc()),
            genre_backfill_retry_days=dashboard.repo.getGenreBackfillRetryDays(),
            bio_backfill_retry_days=dashboard.repo.getBioBackfillRetryDays(),
            backfill_retry_min=BACKFILL_RETRY_DAYS_MIN, backfill_retry_max=BACKFILL_RETRY_DAYS_MAX,
            backup_interval_hours=dashboard.repo.getBackupIntervalHours(DEFAULT_BACKUP_INTERVAL_HOURS),
            backup_retention_count=dashboard.repo.getBackupRetentionCount(DEFAULT_BACKUP_RETENTION_COUNT),
            backup_interval_min=BACKUP_INTERVAL_HOURS_MIN, backup_interval_max=BACKUP_INTERVAL_HOURS_MAX,
            backup_retention_min=BACKUP_RETENTION_COUNT_MIN, backup_retention_max=BACKUP_RETENTION_COUNT_MAX,
            #< masked: the template only checks whether a password EXISTS, so
            #  the decrypted secret stays out of the render context entirely
            smtp_config=get_smtp_config(dashboard.repo, include_password=False),
            instance_public_url=get_instance_public_url(dashboard.repo),
            database_integrity=database_integrity,
            dangling_row_total=dangling_row_total,
            listener_summary=listener_summary,
            spotify_rate_limit=spotify_rate_limit,
            spotify_totp=spotify_totp,
            spotify_api_worker_summary=spotify_api_worker_summary,
            lastfm_worker_summary=lastfm_worker_summary,
            lastfm_album_bio_worker_summary=lastfm_album_bio_worker_summary,
            lastfm_artist_bio_worker_summary=lastfm_artist_bio_worker_summary,
            auto_importer_worker_summary=auto_importer_worker_summary,
            wrapped_worker_summary=wrapped_worker_summary,
            backup_worker_summary=backup_worker_summary,
            milestone_worker_summary=milestone_worker_summary,
            email_worker_summary=email_worker_summary,
            catalog_genre_coverage=dashboard.repo.getCatalogGenreCoverage(),
            catalog_biography_coverage=dashboard.repo.getCatalogBiographyCoverage(),
            registration_counts=dashboard.repo.getRecentRegistrationCounts(),
            instance_share_counts=dashboard.repo.getInstanceShareCounts(),
            active_share_links_count=dashboard.repo.getActiveShareLinksCount(),
            current_tab=current_tab,
            error=request.args.get("error"),
            message=request.args.get("message"),
            section="admin",
        )
    app.add_url_rule("/admin", "adminPage", adminPage, methods=["GET"])

    @requiresAdmin
    def adminEmailSettings(username, db):
        """Admin-only: update instance-wide SMTP configuration, the global
        email notifications toggle, and the public URL notification emails
        link back to."""
        enabled = request.form.get("email_notifications_enabled") == "1"
        host = request.form.get("smtp_host", "")
        try:
            port = int(request.form.get("smtp_port", str(DEFAULT_SMTP_PORT)))
        except (ValueError, TypeError):
            port = DEFAULT_SMTP_PORT
        encryption = request.form.get("smtp_encryption", DEFAULT_SMTP_ENCRYPTION)
        user = request.form.get("smtp_user", "")
        raw_password = request.form.get("smtp_password", "")
        clear_password = request.form.get("clear_password") == "1"
        # None = keep existing encrypted value; "" = intentional clear; non-empty = update
        if clear_password:
            password = ""  # triggers explicit clear in save_smtp_config
        elif raw_password:
            password = raw_password
        else:
            password = None  # blank field submitted without clear → keep existing
        from_email = request.form.get("smtp_from_email", "")
        from_name = request.form.get("smtp_from_name", DEFAULT_SMTP_FROM_NAME)
        public_url = request.form.get("instance_public_url", "")

        # Reject before either save touches app_settings, so a bad submission
        # never overwrites a working configuration (UT-16).
        if enabled and not host.strip():
            return redirect(url_for("adminPage", tab="settings",
                                    error="Enabling notifications requires an SMTP host."))
        if enabled and not from_email.strip():
            return redirect(url_for("adminPage", tab="settings",
                                    error="Enabling notifications requires a From address."))
        if not _isValidFromEmail(from_email.strip()):
            return redirect(url_for("adminPage", tab="settings",
                                    error="From address must be a valid email address."))
        if not _isValidPublicUrl(public_url.strip()):
            return redirect(url_for("adminPage", tab="settings",
                                    error="Public URL must start with http:// or https://."))

        save_smtp_config(
            repo=dashboard.repo,
            enabled=enabled,
            host=host,
            port=port,
            encryption=encryption,
            user=user,
            password=password,
            from_email=from_email,
            from_name=from_name,
        )
        save_instance_public_url(dashboard.repo, public_url)
        return redirect(url_for("adminPage", tab="settings", message="Email notification settings saved."))
    app.add_url_rule("/admin/email_settings", "adminEmailSettings", adminEmailSettings, methods=["POST"])

    @requiresAdmin
    def adminTestEmail(username, db):
        """Admin-only: send a test email to the current admin account."""
        #< requiresAdmin deliberately doesn't pass the email through (it's
        #  guard-only everywhere else); this one view actually sends to it
        email = dashboard.repo.getEmailForUsername(username)
        success, err = send_test_email(dashboard.repo, email)
        if success:
            return redirect(url_for("adminPage", tab="settings", message=f"Test email successfully sent to {email}."))
        else:
            # `err` rides here only as far as the caller - the redirect's query
            # string is browser history and the access log, and an SMTP auth
            # failure echoes the configured username in its text. The detail
            # is already logged at ERROR by send_test_email
            # (services/email_service.py:213-215); keep it out of the URL.
            return redirect(url_for("adminPage", tab="settings",
                                    error="Test email failed - see the server log for the SMTP error."))
    app.add_url_rule("/admin/test_email", "adminTestEmail", adminTestEmail, methods=["POST"])

    @requiresAdmin
    def adminUserSettings(username, db):
        """Admin-only: instance-wide toggles for data sharing (Compare +
        share requests), new user registration, public Wrapped share links,
        achievement milestones, automatic milestone-date recalculation, and
        the personal tagging system (tag panel, tag filters, Playlists page) -
        see Database/repository.py's app_settings."""
        # Unchecked checkboxes aren't submitted: absence means disable.
        dashboard.repo.setDataSharingEnabled(request.form.get("data_sharing") == "1")
        dashboard.repo.setRegistrationEnabled(request.form.get("registration") == "1")
        dashboard.repo.setShareLinksEnabled(request.form.get("share_links") == "1")
        dashboard.repo.setEmailVerificationEnabled(request.form.get("email_verification") == "1")
        dashboard.repo.setMilestonesEnabled(request.form.get("milestones") == "1")
        dashboard.repo.setMilestoneRecalcEnabled(request.form.get("milestone_recalc") == "1")
        dashboard.repo.setTagsEnabled(request.form.get("tags") == "1")
        dashboard.repo.setFriendsNowPlayingEnabled(request.form.get("friends_now_playing") == "1")

        # The merge toggle acts on its EDGES, which is what makes it a real
        # undo switch rather than a display filter: on runs the ISRC matcher
        # now (the backfiller keeps it current afterwards - see
        # _metadataBackfillLoop), off takes back everything the matcher ever
        # did, manual verdicts excepted. Both are idempotent and cheap, and
        # no read path consults the setting - by the time this returns, the
        # data already says whatever the checkbox now says.
        mergeWanted = request.form.get("track_merge") == "1"
        message = "User settings saved."
        if mergeWanted != dashboard.repo.isTrackMergeEnabled():
            if mergeWanted:
                # The merge, enabled flag and run stamp commit atomically. A
                # failed first pass must leave the checkbox off, while this
                # successful pass owns the backfiller's slot for the day.
                summary = dashboard.repo.mergeTracksByIsrc(enableSetting=True)
                #< before-count first - "merged 433 into 407" reads as a shrink
                #  of 26, but the two counts are disjoint (duplicates that fold
                #  away vs songs that survive), so lead with their sum. NB
                #  summary["groups"] is already a count, unlike the preview's
                #  list - len() of it was a TypeError that 500'd this POST
                groupCount = summary["groups"]
                message = (f"User settings saved. Track merge enabled: "
                           f"{summary['merged'] + groupCount} release(s) collapsed into "
                           f"{groupCount} song(s); {summary['merged']} duplicate(s) removed.")
            else:
                try:
                    undone = dashboard.repo.unmergeAllIsrcMerges(disableSetting=True)
                except ValueError as error:
                    logger.warning("Track merge disable rejected: %s", error)
                    errorMessage = (
                        "Track merge could not be disabled because some saved merges "
                        "need repair. Other user settings were saved.")
                    return redirect(url_for(
                        "adminPage", tab="settings",
                        error=errorMessage))
                message = (f"User settings saved. Track merge disabled: "
                           f"{undone} track(s) unmerged; manual decisions kept.")
        return redirect(url_for("adminPage", tab="settings", message=message))
    app.add_url_rule("/admin/user_settings", "adminUserSettings", adminUserSettings, methods=["POST"])

    @requiresAdmin
    def adminLastfmSettings(username, db):
        """Admin-only: Last.fm genre backfill, artist/album biography
        backfill, and whether inherited (artist-derived) genre rows count
        in genre stats and coverage - see Database/repository.py's
        app_settings."""
        dashboard.repo.setLastfmGenreBackfillEnabled(request.form.get("lastfm_backfill") == "1")
        dashboard.repo.setArtistBioEnabled(request.form.get("artist_bio") == "1")
        dashboard.repo.setAlbumBioEnabled(request.form.get("album_bio") == "1")
        dashboard.repo.setInheritedGenresEnabled(request.form.get("include_inherited") == "1")
        # Backfill retry intervals (days) for the empty-result re-attempt gate.
        for field, key in (("genre_backfill_retry_days", GENRE_BACKFILL_RETRY_DAYS_KEY),
                           ("bio_backfill_retry_days", BIO_BACKFILL_RETRY_DAYS_KEY)):
            _saveClampedIntSetting(field, key, BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX)
        return redirect(url_for("adminPage", tab="workers", message="Last.fm settings saved."))
    app.add_url_rule("/admin/lastfm_settings", "adminLastfmSettings", adminLastfmSettings, methods=["POST"])

    def adminRefreshLastfmEntity(kind, entity_id):
        """Admin-only: force a fresh Last.fm lookup for one artist/album/
        track (the detail pages' "Refresh Last.fm Data" button) - see
        Database.refreshLastfmEntity for what "fresh" bypasses."""
        routeByKind = {"artist": "artistDetailPage", "album": "albumDetailPage",
                      "track": "songDetailPage"}
        idKwargByKind = {"artist": "artist_id", "album": "album_id", "track": "track_id"}
        if kind not in routeByKind:
            abort(404)
        detailRoute = routeByKind[kind]
        idKwarg = idKwargByKind[kind]

        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            #< its own next (the detail page the button lives on, not /admin) is
            #  why this keeps a hand-rolled guard - but it goes through
            #  unauthenticatedResponse for the XHR branch below, which the bare
            #  redirect it used to be had no way to reach
            return dashboard.unauthenticatedResponse(
                nextPath=url_for(detailRoute, **{idKwarg: entity_id}))
        if not dashboard.repo.isAdmin(username):
            abort(403)

        result = db.refreshLastfmEntity(kind, entity_id)
        STATUS_MESSAGES = {
            "no_api_key": ("error", "Add a Last.fm API key on your profile to refresh Last.fm data."),
            "invalid_key": ("error", "Your stored Last.fm API key was rejected by Last.fm."),
            "not_found": ("error", "Couldn't find this item to refresh."),
            "no_artist": ("error", "Couldn't determine this album's artist."),
            "transient": ("error", "Last.fm didn't respond - try again in a moment."),
            "ok": ("success", f"Refreshed Last.fm data for “{result.get('name', '')}”."),
        }
        messageKind, message = STATUS_MESSAGES[result["status"]]

        # The detail pages submit this form via fetch (static/js/admin-refresh.js)
        # so a refresh doesn't navigate away and reset tab/sort/page state; the
        # redirect below stays as the no-JS fallback.
        if declaresItselfXhr():
            return jsonify(kind=messageKind, message=message)

        redirectArgs = {idKwarg: entity_id, messageKind: message}
        groupBy = request.form.get("groupBy")
        if groupBy:
            redirectArgs["groupBy"] = groupBy
        return redirect(url_for(detailRoute, **redirectArgs))
    app.add_url_rule("/admin/lastfm/refresh/<kind>/<entity_id>", "adminRefreshLastfmEntity", adminRefreshLastfmEntity, methods=["POST"])

    def adminSplitTrack(track_id):
        """Admin-only: take one release back out of its merge - the "Split"
        control beside the song page's "Also released on" list.

        Records a manual "not the same recording" verdict (unmergeTrack), which
        is exactly the row the matcher refuses to overrule - so the split holds
        across every later automatic pass, and across the toggle being cycled.
        Admin-gated because a merge is instance-wide: splitting moves every
        account's numbers, the same reason the toggle itself lives on /admin.

        The wrongly-split case has an exit too: deleting the decision row lets
        the next matcher pass re-merge, which is deliberately NOT a button yet -
        a split is a person overruling the machine, and un-overruling deserves
        more ceremony than an adjacent click."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("songDetailPage", track_id=track_id)))
        if not dashboard.repo.isAdmin(username):
            abort(403)

        #< resolved BEFORE the split: afterwards the member resolves to itself,
        #  and the admin is standing on the canonical's page
        canonicalId = dashboard.repo.resolveCanonicalTrackId(track_id)
        try:
            dashboard.repo.unmergeTrack(track_id, decidedBy=username)
        except ValueError:
            abort(400)   #< an id that names no track, same answer as the review queue's verdicts
        return redirect(url_for("songDetailPage", track_id=canonicalId,
                                success="Split out of the merge - this stays a separate song from now on."))
    app.add_url_rule("/admin/split_track/<track_id>", "adminSplitTrack", adminSplitTrack, methods=["POST"])

    @requiresAdmin
    def adminMergeReview(username, db):
        """Admin-only: the manual review queue - same-title, same-artist,
        duration-agreeing groups the ISRC tier can NOT decide (a remaster is a
        new master with its own ISRC; a fabricated import row has none at all).
        The page proposes, a person rules, and both verdicts write the pinned
        decision rows every automatic pass already honours."""
        dismissed = dashboard.repo.getDismissedMergeCandidates()
        #< formatted here rather than in the template, same as the user table's
        #  created_at: the repo answers with the stored epoch, and the tz is
        #  the instance's. An unformattable stamp renders blank rather than
        #  500-ing the whole page over a decoration.
        for entry in dismissed["entries"]:
            entry["decidedOn"] = ""
            try:
                entry["decidedOn"] = convertToDatetime(
                    entry["decidedAt"], tz=db.tz).strftime("%Y-%m-%d %H:%M")
            except Exception:  # noqa: S110 - see above
                pass
        return render_template(
            "merge_review.html",
            review=dashboard.repo.getMergeReviewCandidates(),
            dismissed=dismissed,
            #< which release a person picked to keep the song's page, carried
            #  across the redirect each verdict causes. A group of three takes
            #  more than one click, and the pick used to live only in the page
            #  it was made on: the next render re-elected from the survivors
            #  and suggested a third release. Never echoed - the template
            #  falls back to its own suggestion unless this names a release in
            #  the group, the same rule merge-review.js applies client-side.
            main=request.args.get("main", ""),
            track_merge_enabled=dashboard.repo.isTrackMergeEnabled(),
            message=request.args.get("message"),
            error=request.args.get("error"),
            section="admin",
        )
    app.add_url_rule("/admin/merge-review", "adminMergeReview", adminMergeReview, methods=["GET"])

    @requiresAdmin
    def adminMergeReviewMerge(username, db):
        """Admin-only: a person's "same recording" for one proposed member.
        Admin-gated like the toggle and the split, and for the same reason: a
        merge is instance-wide, so it moves every account's numbers.

        Redirects carrying the same `canonical` back as `main`: the group may
        still have releases left to rule on, and the pick that named this
        target has to survive the reload or the next click re-elects."""
        canonical = request.form.get("canonical", "")
        try:
            merged = dashboard.repo.mergeTrackManually(
                request.form.get("member", ""), canonical, decidedBy=username)
        except ValueError:
            abort(400)
        #< 0 is the verb's idempotent no-op: the release is already in this
        #  group. Same stale-page race the reject verb answers in words - the
        #  queue open in two tabs, or the daily matcher folding the pair
        #  between render and click - and not an error, because the outcome
        #  the admin clicked for is already true. "Merged 0 release(s)" claimed
        #  a merge had happened and handed over a count nothing explains.
        message = (f"Merged {merged} release(s) into one song." if merged else
                   "That release is already part of this song - nothing to merge.")
        return redirect(url_for("adminMergeReview", main=canonical or None,
                                message=message))
    app.add_url_rule("/admin/merge_review/merge", "adminMergeReviewMerge",
                     adminMergeReviewMerge, methods=["POST"])

    @requiresAdmin
    def adminMergeReviewReject(username, db):
        """Admin-only: a person's "not the same recording". Recorded, so the
        pair leaves this queue for good (a later shared ISRC still outranks
        it - see dismissMergeCandidate).

        The form carries the same `canonical` field the merge verb does - the
        release keeping the song's page, which the picker rewrites when a
        person chooses another - so the row records what the "no" was ruled
        AGAINST and the log can say so later, and comes back as `main` so the
        pick survives the reload (see adminMergeReviewMerge)."""
        member = request.form.get("member", "")
        canonical = request.form.get("canonical", "")
        #< the friendly answer for the realistic race - the queue open in two
        #  tabs, or the matcher merging the pair between render and click. Not
        #  the 400 crafted junk gets: the admin did nothing wrong, so they
        #  land back on the queue (re-rendered without the merged member) and
        #  are told where the "no" for a merged track lives. The repo's own
        #  in-transaction check stays the backstop for anything slipping this
        #  read.
        stale = None
        if member and dashboard.repo.resolveCanonicalTrackId(member) != member:
            stale = ("That release was merged after this page loaded, so nothing was "
                     "recorded. A wrong merge can be split from the song's own page.")
        elif (canonical and canonical != member
                and dashboard.repo.resolveCanonicalTrackId(canonical) == member):
            #< the mirror shape, and the same race: the counterpart was merged
            #  INTO the release being ruled on, so the pair is already one song
            #  and the "no" is contradicted before it is written. The
            #  canonical != member arm is not redundant: resolving an UNMERGED
            #  id returns that id, so without it a release ruled against ITSELF
            #  matches here and gets a 302 claiming a merge that never
            #  happened. That one is crafted (the main row renders no verdict
            #  buttons) and stays the repo's 400.
            #  dismissMergeCandidate refuses it in-transaction, which reached
            #  abort(400) - a bare error page for the admin who did nothing
            #  wrong, where the shape above gets an explanation. The repo's
            #  ValueError stays the backstop for a post nothing rendered.
            stale = ("Those two releases were merged into one after this page loaded, so "
                     "nothing was recorded. A wrong merge can be split from the song's "
                     "own page.")
        if stale:
            return redirect(url_for("adminMergeReview", main=canonical or None, error=stale))
        try:
            dashboard.repo.dismissMergeCandidate(
                member, decidedBy=username, againstId=canonical)
        except ValueError:
            abort(400)
        return redirect(url_for(
            "adminMergeReview", main=canonical or None,
            message="Kept separate - this release will not be suggested again."))
    app.add_url_rule("/admin/merge_review/reject", "adminMergeReviewReject",
                     adminMergeReviewReject, methods=["POST"])

    @requiresAdmin
    def adminMergeReviewUndismiss(username, db):
        """Admin-only: take back a "not the same recording" so the queue may
        ask about it again. The 400 covers both a junk id and a track whose
        row is no longer a dismissal - a shared ISRC can have overruled it into
        an ordinary merge since the page rendered, and that is undone by the
        toggle or a split rather than from here."""
        try:
            dashboard.repo.undismissMergeCandidate(request.form.get("member", ""))
        except ValueError:
            abort(400)
        return redirect(url_for(
            "adminMergeReview",
            message="Back in the queue - this release can be suggested again."))
    app.add_url_rule("/admin/merge_review/undismiss", "adminMergeReviewUndismiss",
                     adminMergeReviewUndismiss, methods=["POST"])

    @requiresAdmin
    def adminSpotifySettings(username, db):
        """Admin-only: the Spotify Developer API backfill kill switch
        (missed-plays recovery and album/track metadata fetching)."""
        dashboard.repo.setSpotifyApiBackfillEnabled(request.form.get("spotify_backfill") == "1")
        # Read once per listener build, so this takes effect on the next
        # rebuild rather than mid-stream - say so instead of implying it is live.
        dashboard.repo.setPushListenerEnabled(request.form.get("push_listener") == "1")
        return redirect(url_for("adminPage", tab="workers",
                                message="Spotify settings saved. Push mode applies when each listener next restarts."))
    app.add_url_rule("/admin/spotify_settings", "adminSpotifySettings", adminSpotifySettings, methods=["POST"])

    @requiresAdmin
    def adminSkipSettings(username, db):
        """Save classification settings and every user's skip flags atomically.

        Every Save invalidates Wrapped for lazy rebuilding and repairs drift,
        including when the submitted settings are unchanged."""
        mode = request.form.get("skip_mode", SKIP_MODE_SECONDS)
        if mode not in (SKIP_MODE_SECONDS, SKIP_MODE_PERCENT):
            mode = SKIP_MODE_SECONDS
        try:
            value = int(request.form.get("skip_value", ""))
        except (TypeError, ValueError):
            return redirect(url_for("adminPage", tab="settings", error="Skip threshold must be a whole number."))
        # Keep the existing lenient completion rule: blank/bad input leaves
        # the stored setting alone. The repository clamps valid inputs.
        try:
            completionPercent = int(request.form.get("completion_complete_percent", ""))
        except (TypeError, ValueError):
            completionPercent = None
        try:
            dashboard.repo.savePlaybackClassificationSettings(mode, value, completionPercent)
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            logger.exception("Could not save playback classification settings")
            return redirect(url_for("adminPage", tab="settings",
                                    error="Could not save playback classification settings. Please try again."))
        return redirect(url_for("adminPage", tab="settings", message="Playback classification settings saved."))
    app.add_url_rule("/admin/skip_settings", "adminSkipSettings", adminSkipSettings, methods=["POST"])

    @requiresAdmin
    def adminBackupSettings(username, db):
        """Admin-only: automatic-backup interval (hours) and retention (count),
        0 to disable either. Read when the BackupWorker is constructed, so a
        change applies after the app restarts."""
        for field, key, lo, hi in (
            ("backup_interval_hours", BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX),
            ("backup_retention_count", BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX),
        ):
            #< "0" (disable) is a truthy string, so it saves - see the helper
            _saveClampedIntSetting(field, key, lo, hi)
        return redirect(url_for("adminPage", tab="settings", message="Backup settings saved."))
    app.add_url_rule("/admin/backup_settings", "adminBackupSettings", adminBackupSettings, methods=["POST"])

    @requiresAdmin
    def adminCreateBackup(username, db):
        """Admin-only: trigger an immediate on-demand database backup. Runs
        unconditionally even if scheduled automatic backups are disabled.

        The snapshot runs on a background thread so a large database can't tie
        up the request thread (and risk an HTTP timeout): the request waits up
        to MANUAL_BACKUP_SYNC_WAIT_SECONDS for the fast common case - reporting
        the snapshot filename (or the failure) synchronously - and otherwise
        returns immediately, leaving the backup to finish in the background.
        Returns JSON when requested via AJAX, or redirects to /admin."""

        is_ajax = declaresItselfXhr()

        def respond(kind, message):
            if is_ajax:
                return jsonify(kind=kind, message=message)
            key = "message" if kind == "success" else "error"
            return redirect(url_for("adminPage", tab="settings", **{key: message}))

        backup_worker = getattr(dashboard, "backupWorker", None)
        if backup_worker is None:
            return respond("error", "Backup worker not available.")

        # Fast, friendly rejection for a second click. The real mutual
        # exclusion lives in runBackup itself, which also covers the scheduled
        # worker - this check alone couldn't, since the scheduler can start
        # between the check and the thread below.
        if backup_worker.isBackupRunning():
            return respond("error", "A backup is already in progress.")

        result = {}

        def _run():
            try:
                result["path"] = backup_worker.runBackup()
            except Exception as e:
                # Logged here so a failure that outlives the synchronous wait
                # (a slow backup that then errors) still reaches the log.
                result["error"] = e
                logger.error("Manual database backup failed: %s", e)

        thread = threading.Thread(target=_run, name="manual-backup", daemon=True)
        thread.start()
        thread.join(MANUAL_BACKUP_SYNC_WAIT_SECONDS)

        if thread.is_alive():
            # Still running - return rather than hold the request open; the
            # worker's lock stays held until the snapshot finishes, so a
            # follow-up click gets "already in progress" until then.
            return respond("success", "Backup started - a large database can take a while, "
                                      "so the snapshot will appear in the backups folder shortly.")

        if "error" in result:
            return respond("error", f"Backup failed: {result['error']}")

        backup_path = result.get("path")
        filename = getattr(backup_path, "name", str(backup_path))
        return respond("success", f"Database snapshot created: {filename}")
    app.add_url_rule("/admin/create_backup", "adminCreateBackup", adminCreateBackup, methods=["POST"])

    @requiresAdmin
    def adminTuningSettings(username, db):
        """Admin-only: numeric tunables migrated out of code constants. The
        Discover artist count is read live per request; the worker pool sizes
        apply only after a restart (see Database.configureWorkerPools). Each
        value is clamped to its bounds; a blank/unparseable field is left as-is."""

        _saveClampedIntSetting("discover_artist_limit", DISCOVER_ARTIST_LIMIT_KEY, DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX)
        _saveClampedIntSetting("image_download_workers", IMAGE_DOWNLOAD_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        _saveClampedIntSetting("artist_bio_workers", ARTIST_BIO_FETCH_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        _saveClampedIntSetting("album_bio_workers", ALBUM_BIO_FETCH_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        return redirect(url_for("adminPage", tab="workers", message="Advanced tuning settings saved."))
    app.add_url_rule("/admin/tuning_settings", "adminTuningSettings", adminTuningSettings, methods=["POST"])

    @requiresAdmin
    def adminRestart(username, db):
        """Admin-only: gracefully stop every worker and exit so a SUPERVISING
        launch script relaunches the process - the only way restart-only
        settings (worker pool sizes) take effect. Gated behind
        ALLOW_INSTANCE_RESTART so it can't be triggered on a bare, unsupervised
        process, which would just stop the app. The exit is deferred by
        INSTANCE_RESTART_DELAY_SECONDS so this response reaches the browser
        first; threading.Timer is the testable seam (no real os._exit under
        test, which patches it)."""
        # .strip() before .lower(): see the same read in the settings-tab
        # snapshot above (restart_enabled) for why.
        if os.environ.get(ALLOW_INSTANCE_RESTART_ENV_VAR, "").strip().lower() not in TRUTHY_ENV_VALUES:
            return redirect(url_for("adminPage", tab="settings",
                error="Instance restart is disabled. Set ALLOW_INSTANCE_RESTART=1 in a supervised launch to enable it."))

        def _gracefulExit():
            try:
                dashboard.shutdown()
            finally:
                os._exit(0)
        threading.Timer(INSTANCE_RESTART_DELAY_SECONDS, _gracefulExit).start()
        # `message`, not `error`: the restart was accepted, so /admin should
        # show it in the informational banner rather than the red error one.
        return redirect(url_for("adminPage", tab="settings",
            message="Restarting now - the app will be back in a few seconds if the process is supervised."))
    app.add_url_rule("/admin/restart", "adminRestart", adminRestart, methods=["POST"])

    @requiresAdmin
    def adminSetUserAdmin(actingUsername, db, username):
        """Admin-only: promote/demote a user's admin status. Demotion goes
        through Repository.demoteAdmin, which atomically refuses to remove the
        last remaining admin (setUserAdmin otherwise happily allows zero admins,
        stranding the instance with nobody able to reach any admin-gated
        surface). The block is raised only when the target actually IS that last
        admin - demoting a non-admin is a harmless no-op, not an error."""
        tab = request.args.get("tab") or request.form.get("tab") or "overview"
        makeAdmin = request.form.get("make_admin") == "1"
        if makeAdmin:
            dashboard.repo.setUserAdmin(username, True)
            return redirect(url_for("adminPage", tab=tab))
        # Demotion: demoteAdmin returns False both when the target was never an
        # admin (nothing to do) and when it's the last admin (must be blocked) -
        # only the latter is an error, so re-check admin status to tell them
        # apart.
        if not dashboard.repo.demoteAdmin(username) and dashboard.repo.isAdmin(username):
            return redirect(url_for("adminPage", tab=tab, error="Cannot remove the instance's last admin."))
        return redirect(url_for("adminPage", tab=tab))
    app.add_url_rule("/admin/users/<username>/admin", "adminSetUserAdmin", adminSetUserAdmin, methods=["POST"])

