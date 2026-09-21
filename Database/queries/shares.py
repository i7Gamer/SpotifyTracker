# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from Database.queries._base import secrets, threading, time


class ShareQueries:
    """ShareQueries: shares data-access methods, mixed into Repository."""

    # ---- Per-user: mutual data-sharing --------------------------------------

    # user_shares.status values (mirrors the IMAGE_STATUS_* convention above;
    # the CHECK constraint in Database/db.py's SCHEMA lists the same literals).
    SHARE_STATUS_PENDING = "pending"

    SHARE_STATUS_ACCEPTED = "accepted"

    # Serializes createShareRequest's check-then-insert: two crossing requests
    # (A->B and B->A) on different Waitress threads could otherwise both pass
    # the reverse-pending check before either INSERT lands, leaving two
    # opposite-direction pending rows the same-direction UNIQUE constraint
    # doesn't cover. Class-level so every Repository instance over the shared
    # database file serializes on the same lock (single-process deployment,
    # like the in-memory rate limiter in app.py).
    _shareWriteLock = threading.Lock()

    def createShareRequest(self, requester: str, recipient: str) -> str:
        """Outcome as a string the caller can word a message around:
        "requested" (new pending row), "already_requested" (this exact request
        was already pending - nothing changed), "accepted" (a reverse-direction
        pending request existed, so this counts as accepting it), or
        "already_accepted" (the two already share - nothing changed)."""
        with self._shareWriteLock:
            conn = self._conn()
            with conn:
                if conn.execute(
                    "SELECT 1 FROM user_shares WHERE status=? AND "
                    "((requester_username=? AND recipient_username=?) OR (requester_username=? AND recipient_username=?))",
                    (self.SHARE_STATUS_ACCEPTED, requester, recipient, recipient, requester),
                ).fetchone():
                    return "already_accepted"

                reverseRow = conn.execute(
                    "SELECT id FROM user_shares WHERE requester_username=? AND recipient_username=? AND status=?",
                    (recipient, requester, self.SHARE_STATUS_PENDING),
                ).fetchone()
                if reverseRow:
                    conn.execute(
                        "UPDATE user_shares SET status=?, responded_at=? WHERE id=?",
                        (self.SHARE_STATUS_ACCEPTED, time.time(), reverseRow["id"]),
                    )
                    return "accepted"

                cursor = conn.execute(
                    "INSERT INTO user_shares (requester_username, recipient_username, status, created_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(requester_username, recipient_username) DO NOTHING",
                    (requester, recipient, self.SHARE_STATUS_PENDING, time.time()),
                )
                return "requested" if cursor.rowcount > 0 else "already_requested"

    def getPendingIncomingShares(self, username: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, requester_username, created_at FROM user_shares "
            "WHERE recipient_username=? AND status=? ORDER BY created_at, id",
            (username, self.SHARE_STATUS_PENDING),
        ).fetchall()
        return [dict(r) for r in rows]

    def getPendingIncomingSharesCount(self, username: str) -> int:
        """Just the count, for the topbar badge - avoids fetching full rows
        (requester_username/created_at) when the caller only needs a number."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM user_shares WHERE recipient_username=? AND status=?",
            (username, self.SHARE_STATUS_PENDING),
        ).fetchone()
        return row["c"]

    def getUnseenAcceptedShareCount(self, username: str) -> int:
        """How many of `username`'s share REQUESTS (not requests they
        received) were accepted since they last visited /profile - the
        recipient doesn't get one of these, since accepting is itself their
        acknowledgment."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM user_shares WHERE requester_username=? AND status=? AND requester_seen_accepted=0",
            (username, self.SHARE_STATUS_ACCEPTED),
        ).fetchone()
        return row["c"]

    def markAcceptedSharesSeenByRequester(self, username: str) -> None:
        """Clears the "your share request was accepted" notification - called
        when `username` visits /profile, where the newly-active share is
        actually visible in their Active Shares list."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE user_shares SET requester_seen_accepted=1 WHERE requester_username=? AND status=? AND requester_seen_accepted=0",
                (username, self.SHARE_STATUS_ACCEPTED),
            )

    def getPendingOutgoingShares(self, username: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, recipient_username, created_at FROM user_shares "
            "WHERE requester_username=? AND status=? ORDER BY created_at, id",
            (username, self.SHARE_STATUS_PENDING),
        ).fetchall()
        return [dict(r) for r in rows]

    def getAcceptedShareUsernames(self, username: str) -> list[str]:
        """The other username on each of `username`'s accepted shares,
        regardless of which side originally sent the request. Used where only
        the counterpart names matter (the Compare page's authorized-user set) -
        see getAcceptedShares() for the id-bearing version a "Revoke" button
        needs."""
        return [share["counterpart"] for share in self.getAcceptedShares(username)]

    def getAcceptedShares(self, username: str) -> list[dict]:
        """[{id, counterpart}] for each of `username`'s accepted shares,
        ordered by counterpart name so pickers and lists render stably -
        SQLite's row order is otherwise unspecified, which would make e.g.
        the Compare page's default counterpart flap between requests."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, CASE WHEN requester_username=? THEN recipient_username ELSE requester_username END AS counterpart "
            "FROM user_shares WHERE status=? AND (requester_username=? OR recipient_username=?) "
            "ORDER BY counterpart",
            (username, self.SHARE_STATUS_ACCEPTED, username, username),
        ).fetchall()
        return [dict(r) for r in rows]

    def hasAnyAcceptedShare(self, username: str) -> bool:
        """True iff `username` has at least one accepted share whose
        counterpart also has stored cookies - the exact set of shares the
        Compare page can actually load (it skips cookie-less counterparts),
        so the nav link this backs never points at a page that would 404.
        LIMIT 1 existence check: this runs on every template render (see
        app.py's _injectShareStatus)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM user_shares us "
            "JOIN users u ON u.username = CASE WHEN us.requester_username=? THEN us.recipient_username ELSE us.requester_username END "
            "WHERE us.status=? AND (us.requester_username=? OR us.recipient_username=?) "
            "AND u.cookies_json IS NOT NULL LIMIT 1",
            (username, self.SHARE_STATUS_ACCEPTED, username, username),
        ).fetchone()
        return row is not None

    def respondToShareRequest(self, shareId: int, actingUsername: str, accept: bool) -> bool:
        """Only the recipient of a still-pending request may respond. Returns
        whether a row was actually affected, so the caller can tell "not
        found/not yours/already resolved" apart from "done"."""
        conn = self._conn()
        with conn:
            if accept:
                cursor = conn.execute(
                    "UPDATE user_shares SET status=?, responded_at=? "
                    "WHERE id=? AND recipient_username=? AND status=?",
                    (self.SHARE_STATUS_ACCEPTED, time.time(), shareId, actingUsername, self.SHARE_STATUS_PENDING),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM user_shares WHERE id=? AND recipient_username=? AND status=?",
                    (shareId, actingUsername, self.SHARE_STATUS_PENDING),
                )
            return cursor.rowcount > 0

    def cancelShareRequest(self, shareId: int, requesterUsername: str) -> bool:
        """Only the original requester may cancel their own still-pending
        request."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM user_shares WHERE id=? AND requester_username=? AND status=?",
                (shareId, requesterUsername, self.SHARE_STATUS_PENDING),
            )
            return cursor.rowcount > 0

    def revokeShare(self, shareId: int, actingUsername: str) -> bool:
        """Either party to an already-accepted share may end it unilaterally -
        deleting the row ends mutual access for both sides, not just the
        acting user's own view."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM user_shares WHERE id=? AND status=? AND (requester_username=? OR recipient_username=?)",
                (shareId, self.SHARE_STATUS_ACCEPTED, actingUsername, actingUsername),
            )
            return cursor.rowcount > 0

    # ---- Per-user: public Wrapped share links -------------------------------

    SHARE_LINK_KIND_WRAPPED = "wrapped"

    def createShareLink(self, username: str, kind: str, year: int | None, expiresInSeconds: float | None) -> str:
        """Creates a new link and returns its token. year=None means "all
        years" (a single link covering every year the owner has data for).
        expiresInSeconds=None means "never expires"."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        expiresAt = now + expiresInSeconds if expiresInSeconds is not None else None
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token, username, kind, year, now, expiresAt),
            )
        return token

    def createShareLinkIfUnderCap(self, username: str, kind: str, year: int | None,
                                  expiresInSeconds: float | None, maxPerBucket: int) -> str | None:
        """createShareLink, refused when this (kind, year) bucket is already
        full. The new token, or None when it is - the caller words the
        message and owns the cap value, which is policy, not schema.

        Exists because the cap was a check-then-insert split across two calls
        in the route: countActiveShareLinksForBucket read in one statement and
        createShareLink inserted in another, with no lock and no unique
        constraint spanning them. Two requests arriving at cap-1 both read
        cap-1 and both insert - and the create button is never disabled, so a
        double-click is enough, let alone two tabs or curl.

        Takes the same class-level lock createShareRequest does, for the same
        reason and with the same limit: it serializes this process, which is
        the whole deployment (see _shareWriteLock's own comment). The raw
        createShareLink stays uncapped and is still the right call for
        anything that has already made this decision."""
        with self._shareWriteLock:
            if self.countActiveShareLinksForBucket(username, kind, year) >= maxPerBucket:
                return None
            return self.createShareLink(username, kind, year, expiresInSeconds)

    def _purgeExpiredShareLinks(self, conn, extraWhere: str = "", params: tuple = ()) -> None:
        """The lazy expiry sweep every share_links reader runs first - see
        the table comment in Database/db.py for why there is no background
        sweep. `extraWhere` is an `AND ...` clause (with its `params`) that
        narrows the sweep to the rows the caller is about to read: by token
        for getShareLink, by username for getShareLinksForUser, and nothing
        for the admin card's instance-wide getActiveShareLinksCount. The ONE
        spelling of "expired" - countActiveShareLinksForBucket filters on
        the inverse, so a change to the rule (a grace period, say) is those
        two sites, not four. Self-committing (`with conn:`), like every
        share-link write."""
        with conn:
            conn.execute(
                "DELETE FROM share_links WHERE expires_at IS NOT NULL AND expires_at < ?" + extraWhere,
                (time.time(), *params),
            )

    def getShareLink(self, token: str) -> dict | None:
        """None for an unknown, revoked, or expired token - all three look
        identical to a caller, so the public route can't leak which case it
        was. An expired row is deleted here, lazily, on lookup rather than by
        a background sweep - see the share_links table comment in
        Database/db.py."""
        conn = self._conn()
        self._purgeExpiredShareLinks(conn, extraWhere=" AND token=?", params=(token,))
        row = conn.execute(
            "SELECT id, token, username, kind, year, created_at, expires_at FROM share_links WHERE token=?",
            (token,),
        ).fetchone()
        return dict(row) if row else None

    def getShareLinksForUser(self, username: str) -> list[dict]:
        """[{id, token, kind, year, created_at, expires_at}], newest year
        first (an all-years link's year is None, which SQLite sorts last in
        DESC order) - for the Profile page's link-management list. Also
        lazily deletes this user's own expired rows first (same reasoning as
        getShareLink) - otherwise an expired link would keep showing here as
        if still active, even though visiting it would already 404."""
        conn = self._conn()
        self._purgeExpiredShareLinks(conn, extraWhere=" AND username=?", params=(username,))
        rows = conn.execute(
            "SELECT id, token, kind, year, created_at, expires_at FROM share_links "
            "WHERE username=? ORDER BY year DESC, created_at DESC",
            (username,),
        ).fetchall()
        return [dict(r) for r in rows]

    def getActiveShareLinksCount(self) -> int:
        """How many public Wrapped share links are currently live (not
        expired) across every user - the admin page's instance-wide card.
        Lazily deletes expired rows first, same pattern as getShareLink/
        getShareLinksForUser but unnarrowed."""
        conn = self._conn()
        self._purgeExpiredShareLinks(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM share_links WHERE expires_at IS NULL OR expires_at >= ?", (time.time(),)
        ).fetchone()
        return row[0]

    def countActiveShareLinksForBucket(self, username: str, kind: str, year: int | None) -> int:
        """How many still-active (non-expired) links exist for one user's
        (kind, year) "bucket" - year=None is the all-years bucket. Used by
        createWrappedShareLink to enforce a per-bucket cap before inserting a
        new row (see SHARE_LINK_MAX_PER_BUCKET in app.py). Uses SQLite's
        NULL-safe `IS` rather than `=` so a single query handles both the
        all-years bucket (year IS NULL) and a specific year without a
        CASE/OR. Doesn't lazily delete expired rows first like getShareLink/
        getShareLinksForUser do - an expired row already fails the
        expires_at filter below so it can't inflate the count. That filter
        is the INVERSE of _purgeExpiredShareLinks's predicate; change the
        two together."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM share_links WHERE username=? AND kind=? AND year IS ? "
            "AND (expires_at IS NULL OR expires_at >= ?)",
            (username, kind, year, time.time()),
        ).fetchone()
        return row["n"]

    def revokeShareLink(self, linkId: int, username: str) -> bool:
        """Only the link's owner may revoke it. Returns whether a row was
        actually deleted, so the caller can tell "gone" from "not yours"."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM share_links WHERE id=? AND username=?",
                (linkId, username),
            )
            return cursor.rowcount > 0
