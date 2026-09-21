# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging

from Database.queries._base import (
    decryptSecret,
    encryptSecret,
    isEncrypted,
    isForeignKeyed,
    json,
    keyFingerprint,
    time,
)
from config import TOP_LIST_DEFAULT_WINDOW

logger = logging.getLogger(__name__)

# The terminal row failStaleRunningImports leaves behind for an import a dead
# process abandoned. Says what happened AND that retrying is safe: a killed
# import's in-flight file rolled back with its transaction, and already
# committed rows dedupe on re-import (insertPlay keys on username+track+
# played_at).
#
# NOT currently reachable by the user: activation calls Database.resetProgress()
# unconditionally (dashboard/user_registry.py, pinned by tests/
# test_read_only_user_db.py), and the boot sequence activates every
# cookie-holding user synchronously - so this row is back to 'idle' before
# waitress serves a request, and any other user hits the same reset on their
# next authenticated request. Making it visible means narrowing that reset to
# stale 'running' rows, which also makes ordinary import outcomes survive a
# restart: a product call, not a cleanup.
IMPORT_INTERRUPTED_BY_RESTART_MESSAGE = (
    "Import interrupted by an app restart - re-import the file(s); "
    "already imported plays are skipped.")


class UserQueries:
    """UserQueries: users data-access methods, mixed into Repository."""

    # ---- Per-user: users / cookies ----------------------------------------------

    def upsertUser(self, username: str, email: str, createdAt: float | None = None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO users (username, email, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO NOTHING",
                (username, email, createdAt if createdAt is not None else time.time()),
            )

    def createUserIfNameAvailable(self, username: str, email: str) -> bool:
        """Atomically reserve a login name against both displayed identities.

        The predicate runs inside the INSERT's write lock, just like
        setDisplayName's guarded UPDATE. Migration upserts remain unchanged.
        """
        conn = self._conn()
        with conn:
            cur = conn.execute(
                """
                INSERT INTO users (username, email, created_at)
                SELECT :username, :email, :created_at
                WHERE NOT EXISTS (
                    SELECT 1 FROM users
                    WHERE username = :username COLLATE NOCASE
                       OR display_name = :username COLLATE NOCASE)
                """,
                {"username": username, "email": email, "created_at": time.time()},
            )
        return cur.rowcount > 0

    def getUsernameForEmail(self, email: str) -> str | None:
        """Case-insensitive on the STORED side (`COLLATE NOCASE`), not on the
        input: emails are written to the row as the user typed them at
        register/login time, so `.lower()`-ing the input here would only
        match rows that happen to already be lowercase - every existing
        mixed-case account (e.g. an ADMIN_EMAIL set before this fix) would
        silently stop resolving. NOCASE is ASCII-only folding, which is all
        an email address needs.
        Without this, the same person registering as "Alice@x" and later
        logging in (or re-authenticating via cookies) as "alice@x" read as a
        different, unknown email - get_or_create_user then minted a second
        users row and a second live listener for the same Spotify account."""
        conn = self._conn()
        row = conn.execute(
            "SELECT username FROM users WHERE email=? COLLATE NOCASE", (email,)
        ).fetchone()
        return row["username"] if row else None

    def usernameExists(self, username: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        return row is not None

    def getEmailForUsername(self, username: str) -> str | None:
        """The stored email for an existing username, or None - either because
        the username doesn't exist, or it exists but has no email on record yet
        (e.g. a migrated account whose users_map.json didn't know it). Callers
        that need to tell those two cases apart should check usernameExists()
        first."""
        conn = self._conn()
        row = conn.execute("SELECT email FROM users WHERE username=?", (username,)).fetchone()
        return row["email"] if row else None

    def getNullEmailUsernameNoCase(self, username: str) -> str | None:
        """Return the stored spelling of a matching legacy username.

        Uses the same SQLite NOCASE comparison as username allocation. Python's
        broader Unicode case folding can report matches that SQLite did not use
        to reject the unsuffixed candidate.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT username FROM users "
            "WHERE username=? COLLATE NOCASE AND email IS NULL",
            (username,),
        ).fetchone()
        return row["username"] if row else None

    # ---- Per-user: display name ------------------------------------------------
    # The editable label standing in for the immutable username key (see the
    # users table comment in Database/db.py). NULL = "display as the username",
    # so every read goes through the COALESCE below rather than callers each
    # remembering the fallback.

    def getDisplayName(self, username: str) -> str:
        """This user's display name, falling back to the username itself -
        including for a username with no row at all. Deliberately forgiving:
        this feeds template rendering, where a stale counterpart name must
        degrade to the raw name rather than render None or raise."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COALESCE(display_name, username) AS name FROM users WHERE username=?",
            (username,),
        ).fetchone()
        return row["name"] if row else username

    def getDisplayNames(self, usernames: list[str]) -> dict[str, str]:
        """{username: displayName} for a whole list in one query - the share
        lists, compare headings and friend chips all name several users per
        render, and getDisplayName per row would be a query each.

        Unlike getDisplayName, an unknown username is simply ABSENT from the
        result rather than mapped to itself: callers memoize this dict, and
        inventing an entry for a user that doesn't exist would cache a fiction."""
        if not usernames:
            return {}
        conn = self._conn()
        placeholders = ", ".join("?" for _ in usernames)
        rows = conn.execute(
            f"SELECT username, COALESCE(display_name, username) AS name "
            f"FROM users WHERE username IN ({placeholders})",
            tuple(usernames),
        ).fetchall()
        return {row["username"]: row["name"] for row in rows}

    def setDisplayName(self, username: str, displayName: str | None) -> bool:
        """Set (or clear, with None) this user's display name unless the name is
        already taken. Returns True iff a row was actually written - False means
        the name collides, or the user doesn't exist.

        The availability check lives INSIDE the UPDATE, evaluated under SQLite's
        write lock, so two users claiming the same name at the same instant
        can't both pass a stale check - the same reasoning as demoteAdmin's
        count guard, and the reason this isn't a read-then-write in the route.

        Taken means, case-insensitively: another account's display_name, or
        another account's USERNAME. The second half is what no UNIQUE index
        could express and is the one that matters - /admin, /compare?with= and
        the share picker all identify people by the real username, so
        displaying as someone else's is an impersonation vector inside the
        share flow. Your OWN row is excluded from both, so re-casing your own
        name (the most likely edit) is always allowed.

        Clearing is never blocked: NULL isn't a value that can collide, and
        several accounts hold it at once."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                """
                UPDATE users SET display_name = :new
                WHERE username = :me
                  AND (:new IS NULL OR NOT EXISTS (
                        SELECT 1 FROM users
                        WHERE username <> :me
                          AND (display_name = :new COLLATE NOCASE
                               OR username = :new COLLATE NOCASE)))
                """,
                {"new": displayName, "me": username},
            )
        return cur.rowcount > 0

    def setUserEmail(self, username: str, email: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE users SET email=? WHERE username=?", (email, username))

    def setUserCookies(self, username: str, cookies: dict) -> None:
        # Encrypted at rest: these are a live Spotify session - see
        # Database/secret_store.py.
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET cookies_json=? WHERE username=?",
                (encryptSecret(json.dumps(cookies)), username),
            )

    def getUserCookies(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT cookies_json FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["cookies_json"] is None:
            return None
        # Legacy plaintext rows pass through decryptSecret unchanged; an
        # undecryptable row (rotated/lost key) reads as "no cookies stored",
        # which routes the user through re-login instead of crashing.
        decrypted = decryptSecret(row["cookies_json"])
        if decrypted is None:
            return None
        return json.loads(decrypted)

    def setUserPassword(self, username: str, passwordHash: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (passwordHash, username),
            )

    def getUserPasswordHash(self, username: str) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
        return row["password_hash"] if row else None

    def getUserSessionVersion(self, username: str) -> int | None:
        """The counter every one of this account's session cookies carries a
        copy of, or None when there is no such account.

        None rather than 0 for an unknown user so the caller can tell "never
        bumped" from "no such row" - what it then DOES with that is its own
        decision, and SpotifyDashboardApp.sessionIsCurrent (the only reader)
        deliberately treats the second as 0, having already resolved the
        username from this same table."""
        conn = self._conn()
        row = conn.execute("SELECT session_version FROM users WHERE username=?", (username,)).fetchone()
        return row["session_version"] if row else None

    def bumpUserSessionVersion(self, username: str) -> int | None:
        """End every session this account has anywhere. Returns the new value,
        or None when there is no such account.

        The whole feature: sessions are signed cookies with no server-side
        store, so there is nothing to delete - the cookies stop matching. The
        caller that IS the current browser has to re-stamp its own session
        afterwards, or it logs itself out along with the rest."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "UPDATE users SET session_version = session_version + 1 WHERE username=?",
                (username,),
            )
            if not cur.rowcount:
                return None
        return self.getUserSessionVersion(username)

    def getUserSpotifyCredentials(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT spotify_client_id, spotify_client_secret, spotify_refresh_token, "
            "spotify_needs_reauth FROM users WHERE username=?",
            (username,)
        ).fetchone()
        if not row:
            return None
        return {
            "client_id": row["spotify_client_id"],
            "client_secret": decryptSecret(row["spotify_client_secret"]),
            "refresh_token": decryptSecret(row["spotify_refresh_token"]),
            "needs_reauth": bool(row["spotify_needs_reauth"]),
        }

    def getSpotifyNeedsReauth(self, username: str) -> bool:
        """Cheap standalone read of the reauth flag (no secret decryption) -
        for the topbar badge context processor, which runs on every page
        render and has no other reason to touch the encrypted credential
        columns."""
        conn = self._conn()
        row = conn.execute(
            "SELECT spotify_needs_reauth FROM users WHERE username=?", (username,)
        ).fetchone()
        return bool(row["spotify_needs_reauth"]) if row else False

    def setSpotifyNeedsReauth(self, username: str, needsReauth: bool) -> bool:
        """Flags a revoked grant or missing required Spotify scope, cleared
        on definitive scoped success or completed reauthorization.
        Guarded on the current value so a routine poll
        that already matches doesn't write every time (see
        Listener.on_scope_status_change, called after every poll).
        Returns whether the flag changed, allowing notification callers to
        use the atomic UPDATE result instead of racing a separate read."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "UPDATE users SET spotify_needs_reauth = ? WHERE username = ? AND spotify_needs_reauth != ?",
                (int(needsReauth), username, int(needsReauth)),
            )
        return cursor.rowcount > 0

    def updateUserSpotifyCredentials(self, username: str, clientId: str | None,
                                     clientSecret: str | None, refreshToken: str | None) -> None:
        # The client id is public (it appears in the OAuth authorize URL);
        # the secret and refresh token are encrypted at rest - see
        # Database/secret_store.py.
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET spotify_client_id = ?, spotify_client_secret = ?, spotify_refresh_token = ? WHERE username = ?",
                (clientId,
                 encryptSecret(clientSecret) if clientSecret else clientSecret,
                 encryptSecret(refreshToken) if refreshToken else refreshToken,
                 username)
            )

    def encryptStoredSecretsIfPlaintext(self) -> int:
        """Encrypt any users-table secret still stored as plaintext (rows
        written before encryption existed) - the 1.16.0 -> 1.17.0 migration.
        Already-encrypted and empty values are left untouched, so re-running
        is safe. Returns the number of users updated. Does NOT commit - the
        caller (migrator) owns the transaction."""
        conn = self._conn()
        secretColumns = ("cookies_json", "spotify_client_secret", "spotify_refresh_token")
        updated = 0
        for row in conn.execute(f"SELECT username, {', '.join(secretColumns)} FROM users").fetchall():
            changes = {column: encryptSecret(row[column])
                       for column in secretColumns
                       if row[column] and not isEncrypted(row[column])}
            if changes:
                setClause = ", ".join(f"{column}=?" for column in changes)
                conn.execute(f"UPDATE users SET {setClause} WHERE username=?",
                             (*changes.values(), row["username"]))
                updated += 1
        return updated

    #< the users-table columns holding enc: values
    SECRET_COLUMNS = ("cookies_json", "spotify_client_secret", "spotify_refresh_token", "lastfm_api_key")

    def countSecretsUnderAnotherKey(self) -> int:
        """How many stored secrets name an encryption key this instance does not
        have.

        These read back as None, which callers treat as "nothing stored" -
        user credentials or instance SMTP settings appear unset, disrupting
        logins or email delivery. That is what a database restored WITHOUT its
        matching secrets/data_encryption_key.txt looks like, and it is
        recoverable: the values are intact, the key is simply elsewhere.

        Counts only values that say so outright (see secret_store's v2 format).
        A pre-fingerprint value can't be classified, so it is never counted -
        this number is a floor, never a false alarm."""
        conn = self._conn()
        rows = conn.execute(f"SELECT {', '.join(self.SECRET_COLUMNS)} FROM users").fetchall()
        #< resolved once, not per value: it reads the key file behind a lock
        current = keyFingerprint()
        userSecrets = sum(1 for row in rows for column in self.SECRET_COLUMNS
                          if isForeignKeyed(row[column], current))
        return userSecrets + int(isForeignKeyed(self.getAppSetting("smtp_password"), current))

    def getUserLastfmApiKey(self, username: str) -> str | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT lastfm_api_key FROM users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return None
        # Encrypted at rest like the Spotify client secret - see
        # Database/secret_store.py. Legacy plaintext passes through.
        return decryptSecret(row["lastfm_api_key"])

    def updateUserLastfmApiKey(self, username: str, apiKey: str | None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET lastfm_api_key = ? WHERE username = ?",
                (encryptSecret(apiKey) if apiKey else None, username),
            )

    # ---- Per-user: admin role -------------------------------------------------
    # Single-admin model (see docs/proposal-admin-and-share-links.md): the
    # earliest-created user is promoted when no admin exists, and app.py's
    # ADMIN_EMAIL bootstrap is the explicit/recovery path. There's
    # deliberately no in-app grant/revoke UI.

    def isAdmin(self, username: str | None) -> bool:
        if not username:
            return False
        conn = self._conn()
        row = conn.execute("SELECT is_admin FROM users WHERE username=?", (username,)).fetchone()
        return bool(row["is_admin"]) if row else False

    def setUserAdmin(self, username: str, isAdmin: bool) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE users SET is_admin=? WHERE username=?", (1 if isAdmin else 0, username))

    def demoteAdmin(self, username: str) -> bool:
        """Atomically clear a user's admin flag unless they are the only
        remaining admin. The count guard lives inside the UPDATE (evaluated
        under SQLite's write lock), so two admins demoting each other at the
        same instant can't both pass a stale count and strand the instance with
        zero admins. Returns True iff a row was actually demoted (False = the
        target wasn't an admin, or was the last one). Unlike setUserAdmin, this
        never promotes and never leaves zero admins, so it - not setUserAdmin -
        is what the admin console's demote path calls."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                """
                UPDATE users SET is_admin = 0
                WHERE username = ? AND is_admin = 1
                  AND (SELECT COUNT(*) FROM users WHERE is_admin = 1) > 1
                """,
                (username,),
            )
        return cur.rowcount > 0

    def getAdminUsernames(self) -> list[str]:
        conn = self._conn()
        rows = conn.execute("SELECT username FROM users WHERE is_admin=1 ORDER BY username").fetchall()
        return [r["username"] for r in rows]

    def promoteEarliestUserToAdminIfNoneExists(self) -> str | None:
        """Promote the earliest-created user (whoever set the instance up) to
        admin - only when no admin exists at all, so re-running (every app
        startup, plus migration 1.17.0) never creates a second admin or
        overrides a deliberate reassignment. Returns the promoted username,
        or None if nothing changed."""
        conn = self._conn()
        with conn:
            if conn.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
                return None
            row = conn.execute(
                "SELECT username FROM users ORDER BY created_at ASC, username ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE users SET is_admin=1 WHERE username=?", (row["username"],))
            return row["username"]

    def getUserTimezone(self, username: str) -> str | None:
        """users.timezone alone (a 1.11.0 column), NOT via getUserSettings:
        resolveUserTimezone runs inside the 1.35.0 milestone-date migrator,
        where the users table genuinely predates hide_tags_panel (1.38.0) and
        hide_now_playing (1.39.0). getUserSettings' SELECT names both, so on
        a real legacy database it raised 'no such column' - which the
        helper's fallback swallowed at debug level, recomputing every user's
        milestone dates on the app-default day boundaries. A helper a
        migrator calls must only touch columns that exist at that
        migrator's from-version."""
        conn = self._conn()
        row = conn.execute("SELECT timezone FROM users WHERE username=?", (username,)).fetchone()
        return row["timezone"] if row else None

    def getUserSettings(self, username: str) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT default_dashboard_window, default_top_list_window, timezone, hide_tags_panel, "
            "hide_now_playing FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if row:
            return {
                "default_dashboard_window": row["default_dashboard_window"] or "day",
                #< `or` the default, not just the column DEFAULT: a row written
                #  before the column existed reads NULL, and All Time is what
                #  those accounts were already getting (see migrate1_46_0)
                "default_top_list_window": row["default_top_list_window"] or TOP_LIST_DEFAULT_WINDOW,
                "timezone": row["timezone"],
                "hide_tags_panel": bool(row["hide_tags_panel"]),
                "hide_now_playing": bool(row["hide_now_playing"]),
            }
        return {"default_dashboard_window": "day",
                "default_top_list_window": TOP_LIST_DEFAULT_WINDOW, "timezone": None,
                "hide_tags_panel": False, "hide_now_playing": False}

    def updateUserSettings(self, username: str, default_dashboard_window: str | None, timezone: str | None,
                           hide_tags_panel: bool = False, hide_now_playing: bool = False,
                           default_top_list_window: str | None = None) -> None:
        conn = self._conn()
        previous = self.getUserSettings(username)
        previousTimezone = previous["timezone"]
        # None means "not submitted", NOT "reset to the default". This parameter
        # arrived long after the signature did, and giving it a plain default
        # would have silently put every existing caller's Top pages back on All
        # Time on the next save. Same hazard the hide_* checkboxes solve with
        # their _present markers - see routes/auth.py's save_preferences.
        if default_top_list_window is None:
            default_top_list_window = previous["default_top_list_window"]
        # The same rule for the window beside it, which save_preferences'
        # comment already claims ("None (never submitted) means leave it alone")
        # and only the top-list one implemented. The column is nullable, so a
        # form that omitted the select stored NULL and every page fell back to
        # "Yesterday" - silently, since nothing reads a NULL here as an error.
        if default_dashboard_window is None:
            default_dashboard_window = previous["default_dashboard_window"]
        with conn:
            conn.execute(
                "UPDATE users SET default_dashboard_window=?, default_top_list_window=?, timezone=?, "
                "hide_tags_panel=?, hide_now_playing=? WHERE username=?",
                (default_dashboard_window, default_top_list_window, timezone,
                 int(hide_tags_panel), int(hide_now_playing), username),
            )
        # A timezone change moves every bucket boundary, weekday name and streak
        # day - so everything timezone-derived in a cached Wrapped year (the
        # day/week/month series, peak_day, longest_streak, and the year's own
        # start/end) is wrong, while the two signals staleness is judged on
        # (max_played_at and the play count) are untouched. The page recalculates
        # only on a cache MISS, so without this the old zone's figures were
        # served indefinitely, share links included.
        #
        # Here rather than in the profile route because this is the single
        # writer of users.timezone: any future caller inherits it.
        if timezone != previousTimezone:
            dropped = self.deleteAllUserWrapped(username)
            if dropped:
                logger.info("Timezone changed for %s - dropped %d cached Wrapped year(s) to rebuild",
                            username, dropped)

    def getHideTagsPanel(self, username: str) -> bool:
        """Cheap standalone read of the per-user "hide the tag panel"
        preference (set on /profile) - for the context processor that gates
        _tag_widget.html on song/artist/album detail pages, which runs on
        every render and has no other reason to touch the rest of
        getUserSettings. Mirrors getSpotifyNeedsReauth's rationale."""
        conn = self._conn()
        row = conn.execute(
            "SELECT hide_tags_panel FROM users WHERE username=?", (username,)
        ).fetchone()
        return bool(row["hide_tags_panel"]) if row else False

    def getAllUsernamesExcept(self, username: str) -> list[str]:
        """Plain username list for a "who can I request a share with" picker -
        deliberately narrower than getAllUsersDetails(), which also selects
        cookies_json/spotify_refresh_token that this list has no reason to
        touch."""
        conn = self._conn()
        rows = conn.execute("SELECT username FROM users WHERE username != ? ORDER BY username", (username,)).fetchall()
        return [r["username"] for r in rows]

    def getAllUsersWithCookies(self) -> list[tuple[str, str]]:
        """(username, email) for every user who has logged in at least once -
        used at startup to make sure each of them has a running listener."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT username, email FROM users WHERE cookies_json IS NOT NULL"
        ).fetchall()
        return [(r["username"], r["email"]) for r in rows]

    # ---- Per-user: import progress ------------------------------------------------

    def writeProgress(self, username: str, status: str, current: int, total: int,
                       message: str, error: bool) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO import_progress (username, status, current, total, message, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    status=excluded.status, current=excluded.current, total=excluded.total,
                    message=excluded.message, error=excluded.error
                """,
                (username, status, current, total, message, int(error)),
            )

    def tryClaimImportRunning(self, username: str) -> bool:
        """Atomically mark this user's import 'running' only if it isn't already,
        so a double-submitted /import-history can't launch two concurrent import
        threads. Returns True iff this call is the one that claimed the slot.

        The conditional ON CONFLICT ... WHERE runs as one statement under
        SQLite's write lock, so the "is it running?" check and the "mark it
        running" claim are indivisible - unlike a readProgress() then
        writeProgress(), whose gap two rapid submissions could both slip
        through. total_changes is used rather than cursor.rowcount because the
        latter is unreliable for INSERT ... ON CONFLICT no-ops."""
        conn = self._conn()
        before = conn.total_changes
        with conn:
            conn.execute(
                """
                INSERT INTO import_progress (username, status, current, total, message, error)
                VALUES (?, 'running', 0, 0, 'Starting import', 0)
                ON CONFLICT(username) DO UPDATE SET
                    status='running', current=0, total=0, message='Starting import', error=0
                    WHERE import_progress.status <> 'running'
                """,
                (username,),
            )
        return conn.total_changes > before

    def failStaleRunningImports(self) -> int:
        """Mark every import_progress row still claiming 'running' as failed.

        Startup-only companion to tryClaimImportRunning: the claim refuses
        while a row says 'running', and only the claiming thread ever writes
        the terminal row. routes/system.py's _releaseImportSlot covers a
        THREAD that dies; a PROCESS that dies mid-import (a deploy, SIGKILL,
        power loss) skips every finally on its daemon threads and leaves the
        row saying 'running'. At startup no import can be in flight in this
        single-process app (waitress threads, one process - see wsgi.py), so
        any surviving 'running' row is by definition stale. Mirrors
        deleteStalePendingImages. Returns the number of rows failed.

        This is defence in depth, not the only guard: user activation already
        calls resetProgress() unconditionally, which clears a stale claim (to
        'idle', saying nothing about why) before the user can be blocked by
        it. So the lockout this converts is not reachable today - what this
        adds is that the stale row is resolved once, at boot, as a TERMINAL
        row rather than depending on an activation side effect that exists
        for other reasons. See IMPORT_INTERRUPTED_BY_RESTART_MESSAGE for why
        the message itself does not reach the page yet."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "UPDATE import_progress SET status='failed', message=?, error=1 "
                "WHERE status='running'",
                (IMPORT_INTERRUPTED_BY_RESTART_MESSAGE,),
            )
            return cur.rowcount

    def readProgress(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT status, current, total, message, error FROM import_progress WHERE username=?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "current": row["current"],
            "total": row["total"],
            "percentage": round((row["current"] / row["total"] * 100) if row["total"] else 0),
            "message": row["message"],
            "error": bool(row["error"]),
        }

    def isFileImported(self, username: str, file_hash: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM imported_files WHERE username = ? AND file_hash = ?",
            (username, file_hash)
        ).fetchone()
        return row is not None

    def markFileImported(self, username: str, file_hash: str) -> None:
        """Records `file_hash` as imported for `username`.

        Does NOT commit - see upsertTrack()'s docstring: overwrite batches
        (deferCommit=True) compose this with the batch's deletes and inserts so
        everything commits or rolls back together, and non-deferred import
        paths commit right after (import_service)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO imported_files (username, file_hash) VALUES (?, ?)",
            (username, file_hash)
        )

    def getAllUsersDetails(self, username: str | None = None) -> list[dict]:
        """The overview page's per-user rows. `username` narrows to a single
        user's own row - what a non-admin viewer is allowed to see (the full
        listing is admin-only, see app.py's overviewPage)."""
        conn = self._conn()
        query = ("SELECT username, display_name, email, cookies_json, spotify_client_id, "
                  "spotify_refresh_token, spotify_needs_reauth, lastfm_api_key, created_at, "
                  "is_admin FROM users")
        params: tuple = ()
        if username is not None:
            query += " WHERE username=?"
            params = (username,)
        rows = conn.execute(query, params).fetchall()
        return [{**dict(r), "is_admin": bool(r["is_admin"])} for r in rows]
