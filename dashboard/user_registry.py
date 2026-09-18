# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""The per-user Database registry and the login-status cache.

This app keeps one long-lived Database per user in a single process, each
owning background threads (listener, auto-importer, backfillers). Handing one
out is therefore not a lookup - it is a construct-and-activate that can make a
live, slow Spotify call, from a request thread, for a user who may already be
being constructed by another request.

Extracted from app.py, where this sat in the middle of Flask setup, worker
loops and route registration. What makes it one concern is the locking
discipline it exists to enforce, which is easy to get subtly wrong when its
pieces are scattered:

  * `_db_lock` is a GLOBAL lock and may only be held across brief dict reads
    and writes on `user_databases` / `_activatedUsers` - never across
    startListener(), listener.stop(), or anything else that touches the
    network. Holding it across per-user work is the stall pattern this app has
    been bitten by before.
  * `_userLock(username)` serializes one user's construct/activate against
    itself without blocking any other user.
  * Lock order is ALWAYS per-user lock -> _db_lock, never the reverse.
  * Being in `user_databases` does not mean activated - see `_activatedUsers`.

`Database` is imported here, so a test that needs to stop get_user_db from
building a real one patches `dashboard.user_registry.Database`.
"""
import logging
import threading
import time

from config import FRIENDS_NOW_PLAYING_LIMIT, LOGIN_CACHE_TTL_SECONDS
from Database.database import Database

logger = logging.getLogger(__name__)


class UserRegistryMixin:
    """Per-user Database instances, their activation state, and login caching.

    Expects the host to provide `self.repo` and `self._stop_event`, and to have
    called `self._initUserRegistry()` first."""

    def _initUserRegistry(self):
        """Set up the registry's state. Called from the host's __init__ before
        any route can reach get_user_db."""
        self.user_databases = {}
        # Usernames whose Database in user_databases has had its background
        # listener/auto-importer actually started - see _getReadOnlyUserDb()
        # and get_user_db()'s activation-guard. A username can be cached in
        # user_databases without being here yet (a public share-link view
        # constructed a read-only instance before its owner ever logged in
        # this process).
        self._activatedUsers: set = set()
        self._db_lock = threading.RLock()
        self._session_lock = threading.RLock()
        # Per-username locks serialize the (possibly network-bound) construct +
        # activate of one user's Database WITHOUT holding the global _db_lock,
        # so a slow Spotify login for one user can't stall every other request
        # in the process. _db_lock now only guards the brief user_databases /
        # _activatedUsers dict ops. Lock order is always per-user lock -> _db_lock,
        # never the reverse.
        self._userLocks: dict = {}
        self._userLocksGuard = threading.Lock()
        self._login_cache: dict = {}  #< {email: (result: bool, expires_at: float)}
        # Bumped by every invalidation, so a login check already in flight can
        # tell its answer is stale before writing it (see _invalidateLoginCache).
        self._login_cache_generation: dict = {}  #< {email: int}

    # ---- account lookup ------------------------------------------------------

    def get_username_for_email(self, email):
        return self.repo.getUsernameForEmail(email)

    def get_or_create_user(self, email):
        # The whole check-then-create sequence needs the lock, not just the final
        # write: two concurrent first-time logins for different emails that
        # happen to sanitize to the same username prefix (e.g. "alice@a.com" and
        # "alice@b.com") could otherwise both pass the uniqueness check before
        # either has actually created their row.
        created = False
        rejectedNames = []
        with self._session_lock:
            username = self.repo.getUsernameForEmail(email)
            if not username:
                created = True
                # Create a new username from email prefix
                prefix = email.split("@")[0]
                sanitized = "".join(c for c in prefix if c.isalnum() or c in ("-", "_")).strip()
                if not sanitized:
                    sanitized = f"user_{int(time.time())}"

                username = sanitized
                counter = 1
                while True:
                    # A cached Database may be read-only and still need the
                    # legacy-account diagnostic below when its name is skipped.
                    if (username not in self.user_databases
                            and self.repo.createUserIfNameAvailable(username, email)):
                        # An email prefix proves no ownership of an existing
                        # account, including a legacy row with no email. The
                        # insert also reserves display names under its write lock.
                        break

                    rejectedNames.append(username)
                    username = f"{sanitized}_{counter}"
                    counter += 1

        if created:
            # Best-effort diagnostics after reservation, outside the session
            # lock. A failed lookup must not fail login or hide later orphans.
            for candidate in rejectedNames:
                try:
                    legacyUsername = self.repo.getNullEmailUsernameNoCase(candidate)
                except Exception:
                    logger.exception(
                        "Could not check whether allocated account %s was suffixed "
                        "past legacy candidate %s", username, candidate)
                    continue
                if legacyUsername is not None:
                    logger.warning(
                        "Legacy account %s has no associated email; allocated new account %s. "
                        "See docs/recover-a-legacy-account.md before reassociating either account.",
                        legacyUsername,
                        username,
                    )
            # The startup promotion ran over an empty users table on a fresh
            # install and nothing re-ran it, so the first account had no admin
            # until the next restart. Same rule as boot (ADMIN_EMAIL
            # authoritative, else the earliest user once), re-asked now that
            # there is a user to ask it about. Outside the session lock: it is
            # repo-only, and holds nothing the registry cares about.
            self._ensureAdminExists()
        return username

    # ---- the registry itself -------------------------------------------------

    def _userLock(self, username):
        """The per-username RLock guarding that user's construct/activate, so a
        slow Spotify login for one user never blocks another. Created on demand."""
        with self._userLocksGuard:
            lock = self._userLocks.get(username)
            if lock is None:
                lock = threading.RLock()
                self._userLocks[username] = lock
            return lock

    def get_user_db(self, username, email):
        # Fast path: already constructed AND activated - hand it back under a
        # brief dict read. Deliberately does NOT block on another user's
        # (possibly slow, network-bound) activation.
        with self._db_lock:
            db = self.user_databases.get(username)
            if db is not None and username in self._activatedUsers:
                return db

        # Slow path: construct/activate under THIS user's lock only. The global
        # _db_lock is taken solely for the brief dict reads/writes here, never
        # across startListener() (a live Spotify login) below - that was the old
        # behavior that let one user's slow login stall every request.
        with self._userLock(username):
            with self._db_lock:
                db = self.user_databases.get(username)
                if db is not None and username in self._activatedUsers:
                    return db
            if db is None:
                # Share the app-wide stop event so the listener reconnect
                # paths can refuse to fire once shutdown has begun.
                db = Database(user=username, email=email, shutdown_event=self._stop_event)
            else:
                # A cached-but-unactivated instance can only have come from
                # _getReadOnlyUserDb, which constructs with startWorkers=False -
                # promote it, or activation would silently hand back a Database
                # whose periodic workers never run. No-op for workers already
                # running, so a racing double-login can't stack threads.
                db.startBackgroundWorkers()

            try:
                db.startAutoImporter()
                db.resetProgress()
                db.startListener(email=email)
            except Exception:
                # This instance's background threads are running by here
                # (Database.__init__ for a fresh construction, the promotion
                # above for a read-only one); startAutoImporter
                # added its watchdog. If a later step fails (startListener is a
                # live Spotify call) the instance must not stay reachable
                # half-activated, so it's stopped and both caches rolled back -
                # every retry would otherwise stack another full set of threads
                # per user, or silently keep serving the dead instance.
                try:
                    db.stop()
                except Exception as stopError:
                    logger.error("Failed to stop partially-started Database for user %s: %s",
                                 username, stopError)
                with self._db_lock:
                    self.user_databases.pop(username, None)
                    self._activatedUsers.discard(username)
                raise
            with self._db_lock:
                self.user_databases[username] = db
                self._activatedUsers.add(username)
            return db

    def _getReadOnlyUserDb(self, username):
        """A Database for `username` suitable for a public, unauthenticated
        share-link view - never starts the listener/auto-importer (no live
        Spotify session should ever be triggered by an anonymous GET), and
        never the five periodic workers either (startWorkers=False below):
        the metadata backfiller polls Spotify on the owner's stored
        credentials, which is the same live-session rule in different
        clothes. If `username` already has an active Database (the common
        case: the owner has logged in to this process before), that instance
        is reused as-is. Otherwise a new instance is cached without
        activating it; get_user_db() activates it in place on the owner's
        next real login instead of skipping activation forever, since by
        then the username is already in user_databases. Callers must already
        know `username` exists (e.g. it came from a share_links row, which a
        foreign key guarantees points at a real user)."""
        with self._db_lock:
            db = self.user_databases.get(username)
            if db is not None:
                return db

        # Construct under the per-user lock (not the global _db_lock) so this
        # never blocks other users, and can't double-construct against a
        # concurrent get_user_db() for the same username.
        with self._userLock(username):
            with self._db_lock:
                db = self.user_databases.get(username)
                if db is not None:
                    return db
            email = self.repo.getEmailForUsername(username)
            db = Database(user=username, email=email, shutdown_event=self._stop_event,
                          startWorkers=False)
            with self._db_lock:
                self.user_databases[username] = db
            return db

    def getFriendsNowPlaying(self, username):
        """What the people `username` has an accepted share with are playing
        right now: {"friends": [...], "moreCount": int}.

        Costs no network calls - getNowPlaying reads each listener's cached
        connect state, and every cookie-holding user's listener is already
        running in this process (see _ensureAllUsersLogin). A counterpart with
        no live Database is simply skipped: constructing one here would start
        ANOTHER user's listener from this request thread, which is exactly what
        _getReadOnlyUserDb exists to avoid.

        The payload is deliberately narrower than the viewer's own now-playing:
        no position/duration (a progress bar is noise at chip size) and none of
        the friend's own played/trackPlayed flags, which describe their history
        and are nobody else's business - the chip's links are decided by the
        VIEWER's, see _markViewerPlayed."""
        if not (self.repo.isDataSharingEnabled() and self.repo.isFriendsNowPlayingEnabled()):
            return {"friends": [], "moreCount": 0}

        #< alphabetical (getAcceptedShareUsernames orders by counterpart), so
        #  chips don't reshuffle between polls
        counterparts = [
            name for name in self.repo.getAcceptedShareUsernames(username)
            if not self.repo.getUserSettings(name).get("hide_now_playing")
        ]
        # Snapshot under the lock, then read each listener outside it - holding
        # _db_lock across per-user work is the stall pattern this app has been
        # bitten by before.
        with self._db_lock:
            liveDbs = [(name, self.user_databases.get(name)) for name in counterparts]

        #< one query for the whole strip rather than one per chip; the chips are
        #  rendered client-side, so this can't go through the Jinja filter
        displayNames = self.repo.getDisplayNames([name for name, _ in liveDbs])

        playing = []
        for name, db in liveDbs:
            if db is None:
                continue   #< no active session in this process
            try:
                #< without the friend's own played flags: they are dropped below
                #  and re-answered against the viewer (_markViewerPlayed), so
                #  computing them here is two queries per friend, every 15s
                nowPlaying = db.getNowPlaying(includePlayedFlags=False)
            except Exception as e:
                logger.warning("Now-playing lookup failed for %s: %s", name, e)
                continue
            # Paused counts as not listening here: a paused track would sit in
            # the strip indefinitely, and chips appearing/vanishing on every
            # pause is exactly the churn a glanceable row can't afford.
            if not nowPlaying or nowPlaying.get("isPaused"):
                continue
            playing.append({
                #< username stays the identity; displayName is what the chip shows
                "username": name,
                "displayName": displayNames.get(name, name),
                "trackId": nowPlaying.get("trackId"),
                "name": nowPlaying.get("name"),
                "artistsText": nowPlaying.get("artistsText"),
                "imageId": nowPlaying.get("imageId"),
                # Ids and names only - artistsText already says the names out
                # loud, and the ids are catalog facts. The friend's own
                # `played` flag is dropped here rather than passed through.
                # artistsText stays the fallback: a first listen isn't in the
                # catalog yet, so getNowPlaying has no artist ids to give.
                "artists": [{"id": artist.get("id"), "name": artist.get("name", "")}
                            for artist in (nowPlaying.get("artists") or [])
                            if artist.get("id")],
            })

        #< the cap first, then the lookups: the overflow is never rendered, so
        #  querying for it is work nobody sees
        strip = playing[:FRIENDS_NOW_PLAYING_LIMIT]
        self._markViewerPlayed(username, strip)
        return {
            "friends": strip,
            "moreCount": max(0, len(playing) - FRIENDS_NOW_PLAYING_LIMIT),
        }

    def _markViewerPlayed(self, viewer, friends):
        """In place: whether the VIEWER has their own plays of each chip's
        track and artists, which is what decides whether the chip links to our
        detail pages or out to Spotify.

        The viewer's history and not the friend's, for the same reason the
        Compare page's counterpart lists use it (see
        services/taste_match._markLinkExternally): /song/<id> and /artist/<id>
        render the VIEWER's data, so for something they have never played the
        detail page has nothing to show. The friend is playing it right now, so
        their own flags would be near-always true - and reading them here would
        put their listening history in a payload built for someone else.

        Two queries for the whole strip rather than two per chip: this runs on
        a 15-second poll."""
        #< dict.fromkeys, not a set: two friends playing the same track is
        #  exactly what a shared strip surfaces, and the order stays stable
        trackIds = list(dict.fromkeys(
            friend["trackId"] for friend in friends if friend["trackId"]))
        artistIds = list(dict.fromkeys(
            artist["id"] for friend in friends for artist in friend["artists"]))
        playedTracks = self.repo.getPlayedTrackIds(viewer, trackIds)
        playedArtists = self.repo.getPlayedArtistIds(viewer, artistIds)
        for friend in friends:
            friend["trackPlayedByViewer"] = friend["trackId"] in playedTracks
            for artist in friend["artists"]:
                artist["playedByViewer"] = artist["id"] in playedArtists

    # ---- login-status cache --------------------------------------------------

    def _refresh_user_session(self, username, email):
        """Restart this user's listener against the cookies just saved to the
        database, and drop any cached login-status result. Without this, a
        re-login after expired/invalid cookies (get_user_db is a no-op for a
        username that already has a live Database) would leave the old, dead
        listener running and the stale cached is_user_logged_in() result in
        place until the process restarts."""
        with self._db_lock:
            db = self.user_databases.get(username)
        if db is not None:
            # Under the per-user lock, NOT the global _db_lock: listener.stop()
            # can join for up to 5s and startListener() is a live Spotify call -
            # holding _db_lock across either would stall every other request.
            with self._userLock(username):
                if db.listener is not None:
                    db.listener.stop()
                db.startListener(email=email)
        self._invalidateLoginCache(email)

    def _invalidateLoginCache(self, email):
        """Drop this user's cached login result and fence any check already in
        flight. is_user_logged_in's miss path evaluates isListenerLoggedIn()
        outside any lock - a live Spotify round-trip - so a check that started
        against the OLD, dead listener could otherwise finish after a
        successful re-login and write its stale False on top, marking a
        just-authenticated user logged out for the whole TTL."""
        self._login_cache.pop(email, None)
        self._login_cache_generation[email] = self._login_cache_generation.get(email, 0) + 1

    def is_user_logged_in(self, email):
        if not email:
            return False

        username = self.repo.getUsernameForEmail(email)
        if not username or self.repo.getUserCookies(username) is None:
            return False

        # isListenerLoggedIn() can make a live network call to Spotify - the result
        # is cached per user for LOGIN_CACHE_TTL_SECONDS to avoid a round-trip on
        # every request (the main cause of Waitress queue saturation).
        now_ts = time.monotonic()
        cached = self._login_cache.get(email)
        if cached is not None and cached[1] > now_ts:
            return cached[0]

        # get_user_db is a no-op (returns the existing instance) if this user
        # already has a live Database - it's only actually constructing one
        # here for a user _ensureAllUsersLogin hasn't (yet, or ever, if
        # construction kept failing) loaded. Either way this must never just
        # assume True for an unloaded user: that's exactly the check the
        # password-login branch relies on to confirm a stored session is
        # still live, not merely that cookies exist.
        generation = self._login_cache_generation.get(email, 0)
        try:
            result = self.get_user_db(username, email).isListenerLoggedIn()
        except Exception as e:
            logger.error("Error checking login status for %s: %s", email, e)
            result = False

        # Only cache if nothing invalidated this user's status while the check
        # above was running (a re-login, most importantly) - see
        # _invalidateLoginCache. The result still stands for THIS request.
        if self._login_cache_generation.get(email, 0) == generation:
            self._login_cache[email] = (result, now_ts + LOGIN_CACHE_TTL_SECONDS)
        return result
