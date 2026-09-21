# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import sqlite3

from Database.queries._base import BEHAVIORAL_COLUMNS, db

logger = logging.getLogger(__name__)

# A probe that couldn't run says nothing about the database's health. These are
# the only failures read that way (see checkIntegrity) - everything else is
# treated as evidence of damage, including the DatabaseError a malformed file
# raises.
PROBE_UNAVAILABLE_ERROR_MARKERS = ("database is locked", "database table is locked", "busy")


class SchemaQueries:
    """SchemaQueries: schema data-access methods, mixed into Repository."""

    def checkIntegrity(self) -> dict:
        """Probe the database file for damage and dangling references.

        Returns {"ok": bool, "corruption": [...], "foreignKeyViolations":
        {table: count}, "probeError": str | None}. Never raises - this runs at
        startup, before anything else could report a problem, so a database too
        broken to query must come back as a finding rather than as a traceback
        that stops the app. A probe that could not run at all is `probeError`,
        NOT a corruption finding: the two want opposite reactions.

        Corruption and FK violations are reported separately because they mean
        different things and warrant different reactions: a damaged file is an
        emergency, while a dangling track_id is invisible to normal queries
        (JOINs drop the row) and can sit there for months. Conflating them would
        either cry corruption over legacy orphans or bury a broken file behind
        them.

        quick_check, not integrity_check: on a 105 MB production database
        quick_check takes ~130 ms against integrity_check's ~970 ms, and the
        extra work integrity_check does is index-vs-table cross-validation -
        worth having on demand, not on every boot."""
        corruption: list = []
        violations: dict[str, int] = {}
        probeError: str | None = None
        try:
            conn = self._conn()
            # quick_check reports damage as result ROWS (the single row "ok"
            # when healthy) and only raises when the file can't be read at all,
            # so the rows must be fetched to mean anything.
            rows = conn.execute("PRAGMA quick_check").fetchall()
            corruption = [row[0] for row in rows if row[0] != "ok"]
            for row in conn.execute("PRAGMA foreign_key_check").fetchall():
                violations[row[0]] = violations.get(row[0], 0) + 1
        except Exception as e:
            # Split, not folded together: "the probe could not run" and "the
            # file is damaged" call for opposite reactions, and a lock timeout
            # under a heavy import would otherwise tell an admin their database
            # is destroyed and to restore a backup.
            #
            # Narrow on purpose. A genuinely damaged file raises too - that is
            # how "database disk image is malformed" surfaces - and that IS
            # corruption evidence, so only a contended lock counts as "couldn't
            # run". Anything unrecognised stays corruption: erring toward the
            # loud answer is right when the alternative is trusting a bad file.
            if isinstance(e, sqlite3.OperationalError) and any(
                marker in str(e).lower() for marker in PROBE_UNAVAILABLE_ERROR_MARKERS
            ):
                probeError = str(e)
            else:
                corruption.append(f"integrity probe failed: {e}")
        return {
            "ok": not corruption and not violations and probeError is None,
            "corruption": corruption,
            "foreignKeyViolations": violations,
            "probeError": probeError,
        }

    def addUserIsAdminColumnIfMissing(self) -> None:
        """SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases -
        a users table that already existed before is_admin was added needs an
        explicit ALTER TABLE (migrate1_17_0). Guarded so re-running the
        migration against an already-migrated database doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    def addUserPasswordHashColumnIfMissing(self) -> None:
        """SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases -
        a users table that already existed before password_hash was added needs
        an explicit ALTER TABLE (migrate1_8_0). Guarded so re-running the
        migration against an already-migrated database doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "password_hash" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")

    def addUserSessionVersionColumnIfMissing(self) -> None:
        """SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases -
        a users table that already existed before session_version was added
        needs an explicit ALTER (migrate1_50_0). Guarded so re-running the
        migration against an already-migrated database doesn't fail, and so a
        retry never resets a counter someone has already bumped."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "session_version" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")

    def addSpotifyApiColumnsToUsersIfMissing(self) -> None:
        """Add Spotify API columns to users table if missing."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        with conn:
            if "spotify_client_id" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_client_id TEXT")
            if "spotify_client_secret" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_client_secret TEXT")
            if "spotify_refresh_token" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_refresh_token TEXT")

    def addTrackMetadataColumnsIfMissing(self) -> None:
        """Add created_at and created_reason columns to tracks table if missing.
        Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        with conn:
            if "created_at" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN created_at REAL")
            if "created_reason" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN created_reason TEXT")

    def addTrackIsrcAttemptedColumnIfMissing(self) -> None:
        """SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases -
        a tracks table that already existed before isrc_attempted_at was added
        needs an explicit ALTER TABLE (migrate1_47_0). Guarded so re-running the
        migration against an already-migrated database doesn't fail.

        Deliberately not indexed: the ISRC queue is a bounded LIMIT scan over a
        ~25k-row catalog that runs once per backfill cycle, and SCHEMA is
        re-stamped onto older databases BEFORE their migrations run - an index
        naming a column that doesn't exist yet would fail stamping (the same trap
        documented above the plays table for is_skip)."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if "isrc_attempted_at" not in columns:
            with conn:
                conn.execute("ALTER TABLE tracks ADD COLUMN isrc_attempted_at REAL")

    def addTrackCanonicalIdColumnIfMissing(self) -> None:
        """Add tracks.canonical_id (migrate1_48_0) if missing - the track this
        one has been merged into, NULL for "not merged".

        SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases, so
        a tracks table that predates the column needs an explicit ALTER. Guarded
        so re-running the migration doesn't fail.

        NULL is the whole migration: every existing row is its own canonical
        track until a matcher says otherwise, so there is nothing to backfill
        and no play count moves on upgrade.

        SQLite allows a REFERENCES clause on an added column only when its
        default is NULL, which is exactly what this wants anyway.

        Not indexed, like isrc_attempted_at and for the same reason - see the
        column's comment in db.py."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if "canonical_id" not in columns:
            with conn:
                conn.execute("ALTER TABLE tracks ADD COLUMN canonical_id TEXT REFERENCES tracks(id)")

    def addTrackMergeDecisionsTableIfMissing(self) -> None:
        """Create track_merge_decisions (migrate1_48_0) if missing.

        A plain CREATE TABLE IF NOT EXISTS, so this is really only here to make
        the migration explicit about what it adds: SCHEMA stamping would create
        it on the next connect regardless, since a brand-new TABLE (unlike a new
        COLUMN, and unlike an index over one) is safe to stamp onto any older
        database."""
        conn = self._conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS track_merge_decisions (
                    track_id     TEXT PRIMARY KEY REFERENCES tracks(id),
                    canonical_id TEXT REFERENCES tracks(id),
                    reason       TEXT NOT NULL,
                    evidence     TEXT,
                    decided_at   REAL NOT NULL,
                    decided_by   TEXT,
                    against_id   TEXT REFERENCES tracks(id),
                    carried_canonical_id TEXT REFERENCES tracks(id)
                )
            """)

    def addMergeDecisionAgainstColumnIfMissing(self) -> None:
        """Add track_merge_decisions.against_id (migrate1_49_0) if missing -
        the release a rejection was ruled AGAINST.

        A new COLUMN, so SCHEMA stamping cannot deliver it: CREATE TABLE IF
        NOT EXISTS leaves an existing table exactly as it is. The sibling
        CREATE above carries it too, for the database that gets the table for
        the first time - the two have to agree or a fresh install and an
        upgraded one end up with different shapes.

        Arrives NULL everywhere, which is the honest answer for every
        rejection recorded before it existed: nothing knows what those were
        ruled against, and guessing would put a name on a decision nobody
        made. Not indexed - it is read once per page render, for at most
        MERGE_DISMISSED_PAGE_LIMIT rows."""
        conn = self._conn()
        columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(track_merge_decisions)").fetchall()}
        if "against_id" not in columns:
            with conn:
                conn.execute("ALTER TABLE track_merge_decisions "
                             "ADD COLUMN against_id TEXT REFERENCES tracks(id)")

    def addMergeDecisionCarriedColumnIfMissing(self) -> None:
        """Add track_merge_decisions.carried_canonical_id (migrate1_52_0) if
        missing - where the MATCHER moved a person's verdict.

        Same reasoning as its against_id sibling above: a new COLUMN, so SCHEMA
        stamping cannot deliver it, and the CREATE beside it carries the column
        too so a fresh install and an upgraded one end up the same shape.

        Arrives NULL everywhere, which is the correct reading of every existing
        row: NULL means "still where the person put it". Rows the matcher had
        already re-homed before this column existed are unrecoverable - the
        target was overwritten in place - and inventing one would put a release
        on a decision nobody made. Not indexed: the revert reads it once per
        toggle-off, over a table with one row per judged track."""
        conn = self._conn()
        columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(track_merge_decisions)").fetchall()}
        if "carried_canonical_id" not in columns:
            with conn:
                conn.execute("ALTER TABLE track_merge_decisions "
                             "ADD COLUMN carried_canonical_id TEXT REFERENCES tracks(id)")

    def addPlayMetadataColumnsIfMissing(self) -> None:
        """Add created_at and created_reason columns to plays table if missing.
        Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        with conn:
            if "created_at" not in columns:
                conn.execute("ALTER TABLE plays ADD COLUMN created_at REAL")
            if "created_reason" not in columns:
                conn.execute("ALTER TABLE plays ADD COLUMN created_reason TEXT")

    def addPlayBehavioralColumnsIfMissing(self) -> None:
        """Add the behavioral metadata columns (BEHAVIORAL_COLUMNS) to plays if
        missing (migrate1_22_0). Only the pre-existing plays table needs ALTERs.
        (Historically this migration also relied on SCHEMA creating a separate
        play_skips table; that table was later merged back into plays and
        removed from SCHEMA - see mergePlaySkipsIntoPlays / migrate1_32_0 - so an
        old DB migrating through 1.22.0 no longer gets one, and the merge is a
        no-op for the absent table.) Guarded so re-running doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        columnTypes = {"shuffle": "INTEGER", "skipped": "INTEGER", "offline": "INTEGER", "incognito": "INTEGER"}
        with conn:
            for column in BEHAVIORAL_COLUMNS:
                if column not in columns:
                    conn.execute(f"ALTER TABLE plays ADD COLUMN {column} {columnTypes.get(column, 'TEXT')}")

    # plays table shape as of migrate1_32_0 (is_skip added, time_played CHECK
    # relaxed to >=0). Pinned here rather than derived from SCHEMA because the
    # rebuild below must reproduce this exact shape regardless of how SCHEMA
    # later evolves. Indexes are recreated separately after the RENAME.
    _PLAYS_NEW_TABLE_SQL = """
    CREATE TABLE plays_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL REFERENCES users(username),
        track_id        TEXT NOT NULL REFERENCES tracks(id),
        played_at       REAL NOT NULL,
        time_played     INTEGER NOT NULL CHECK (time_played >= 0),
        played_from     TEXT,
        created_at      REAL,
        created_reason  TEXT,
        platform        TEXT,
        conn_country    TEXT,
        reason_start    TEXT,
        reason_end      TEXT,
        shuffle         INTEGER,
        skipped         INTEGER,
        offline         INTEGER,
        incognito       INTEGER,
        is_skip         INTEGER NOT NULL DEFAULT 0,
        UNIQUE (username, track_id, played_at)
    )
    """

    def mergePlaySkipsIntoPlays(self) -> dict:
        """One-time rebuild for the play_skips -> plays merge (migrate1_32_0):
        add plays.is_skip, relax the time_played CHECK to >= 0, and fold every
        play_skips row back into plays as is_skip=1. Existing plays are seeded
        with is_skip = (time_played < SKIP_THRESHOLD_MS), matching the default
        seconds/5 threshold the migration also seeds. A full table rebuild is
        required because SQLite can't ALTER a CHECK constraint.

        No-op if plays already has is_skip (already migrated, or a fresh SCHEMA
        db). Robust to play_skips being absent (an old DB that migrated through
        1.22.0 after play_skips was removed from SCHEMA). Returns fold-in counts.
        Does NOT rely on the caller's transaction - it toggles foreign_keys and
        commits its own rebuild, mirroring SQLite's standard table-redefinition
        procedure."""
        conn = self._conn()
        playColumns = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        if "is_skip" in playColumns:
            return {"plays": 0, "skips": 0, "noop": True}

        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        hasSkips = "play_skips" in tables

        playsBefore = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        skipsBefore = conn.execute("SELECT COUNT(*) FROM play_skips").fetchone()[0] if hasSkips else 0

        conn.commit()   #< settle any pending state so the PRAGMA below is honored (ignored inside a tx)
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                # DDL never triggers sqlite3's implicit BEGIN (only DML does),
                # so under a bare `with conn:` the CREATE TABLE below ran in
                # autocommit: a crash mid-copy rolled back the copy but left
                # plays_new behind - and with plays still missing is_skip, the
                # retry re-entered and died on 'table plays_new already
                # exists', wedging the upgrade permanently. The explicit BEGIN
                # puts the whole rebuild (SQLite's DDL is transactional) in
                # the one transaction `with conn:` commits or rolls back -
                # same fix as migrate1_23_0's share_links rebuild.
                conn.execute("BEGIN IMMEDIATE")
                #< self-heal installs the pre-fix code already wedged: the name
                #  exists only for this procedure, so residue is safe to clear
                conn.execute("DROP TABLE IF EXISTS plays_new")
                conn.execute(self._PLAYS_NEW_TABLE_SQL)
                # Existing plays keep played_from and get a computed is_skip.
                conn.execute(
                    """
                    INSERT INTO plays_new
                        (username, track_id, played_at, time_played, played_from, created_at, created_reason,
                         platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito, is_skip)
                    SELECT username, track_id, played_at, time_played, played_from, created_at, created_reason,
                           platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito,
                           CASE WHEN time_played < ? THEN 1 ELSE 0 END
                    FROM plays
                    """,
                    (db.SKIP_THRESHOLD_MS,),
                )
                skipsFolded = 0
                if hasSkips:
                    # play_skips has no played_from (-> NULL); all rows are skips.
                    # INSERT OR IGNORE so a skip colliding with an existing play on
                    # (username, track_id, played_at) yields to the play.
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO plays_new
                            (username, track_id, played_at, time_played, created_at, created_reason,
                             platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito, is_skip)
                        SELECT username, track_id, played_at, time_played, created_at, created_reason,
                               platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito, 1
                        FROM play_skips
                        """
                    )
                    skipsFolded = cur.rowcount
                    conn.execute("DROP TABLE play_skips")
                conn.execute("DROP TABLE plays")
                conn.execute("ALTER TABLE plays_new RENAME TO plays")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user_time ON plays(username, played_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user_track ON plays(username, track_id)")
            # foreign_key_check reports violations as result rows (it never
            # raises), so it must be fetched to mean anything. The rebuild ran
            # with foreign_keys=OFF, so this is exactly where an orphaned
            # plays row (e.g. a play_skips row referencing a since-deleted
            # track) would slip through - surface it loudly rather than keep it
            # silently. Not raised: a dangling track_id FK is invisible in normal
            # queries (JOINs drop it), so it must not brick an otherwise-good
            # one-time upgrade.
            fkViolations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fkViolations:
                logger.error(
                    "mergePlaySkipsIntoPlays: %d foreign-key violation(s) after the plays rebuild: %s",
                    len(fkViolations), fkViolations[:10],
                )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

        return {"plays": playsBefore, "skips": skipsBefore, "folded": skipsFolded}

    def addUserSettingsColumnsIfMissing(self) -> None:
        """Add default_dashboard_window and timezone columns to users table if missing."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        with conn:
            if "default_dashboard_window" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN default_dashboard_window TEXT DEFAULT 'day'")
            if "timezone" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT")

    def addAvailabilityColumnsIfMissing(self) -> None:
        """Add tracks.availability_reason (Spotify playability restriction, e.g.
        COUNTRY_RESTRICTED) and albums.backfill_attempted_at (backfill retry
        rate-limiting) if missing. Guarded so re-running doesn't fail."""
        conn = self._conn()
        trackColumns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        albumColumns = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
        with conn:
            if "availability_reason" not in trackColumns:
                conn.execute("ALTER TABLE tracks ADD COLUMN availability_reason TEXT")
            if "backfill_attempted_at" not in albumColumns:
                conn.execute("ALTER TABLE albums ADD COLUMN backfill_attempted_at REAL")

    def addLastfmColumnsIfMissing(self) -> None:
        """Add users.lastfm_api_key and the lastfm_attempted_at queue columns on
        artists/albums/tracks (migrate1_18_0) if missing. The genre join tables
        and app_settings are plain CREATE TABLE IF NOT EXISTS in SCHEMA, so only
        these columns on pre-existing tables need an ALTER. Guarded so re-running
        the migration doesn't fail."""
        conn = self._conn()
        userColumns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        with conn:
            if "lastfm_api_key" not in userColumns:
                conn.execute("ALTER TABLE users ADD COLUMN lastfm_api_key TEXT")
            for table in ("artists", "albums", "tracks"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if "lastfm_attempted_at" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN lastfm_attempted_at REAL")

    def addArtistBioColumnsIfMissing(self) -> None:
        """Add artists.bio and artists.bio_attempted_at (migrate1_25_0) if
        missing. Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(artists)").fetchall()}
        with conn:
            if "bio" not in columns:
                conn.execute("ALTER TABLE artists ADD COLUMN bio TEXT")
            if "bio_attempted_at" not in columns:
                conn.execute("ALTER TABLE artists ADD COLUMN bio_attempted_at REAL")

    def addAlbumBioColumnsIfMissing(self) -> None:
        """Add albums.bio and albums.bio_attempted_at (migrate1_27_0) if
        missing, mirroring addArtistBioColumnsIfMissing for the album-bio
        feature. Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
        with conn:
            if "bio" not in columns:
                conn.execute("ALTER TABLE albums ADD COLUMN bio TEXT")
            if "bio_attempted_at" not in columns:
                conn.execute("ALTER TABLE albums ADD COLUMN bio_attempted_at REAL")

    def addAlbumArtistRepairColumnIfMissing(self) -> None:
        """Add albums.artist_repair_done_at (migrate1_51_0) if missing - when
        this album's COMPLETE track list was last walked and every artist
        credit in it applied. Arrives NULL, which reads as "never walked", so
        the upgrade queues every album exactly as before and the exclusion
        only starts applying once a cycle has actually finished one. Guarded
        so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
        with conn:
            if "artist_repair_done_at" not in columns:
                conn.execute("ALTER TABLE albums ADD COLUMN artist_repair_done_at REAL")

    def addRequesterSeenAcceptedColumnIfMissing(self) -> None:
        """Add user_shares.requester_seen_accepted (the "your share request
        was accepted" topbar notification's dismissal flag) if missing.
        Guarded so re-running doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_shares)").fetchall()}
        if "requester_seen_accepted" not in columns:
            with conn:
                conn.execute("ALTER TABLE user_shares ADD COLUMN requester_seen_accepted INTEGER NOT NULL DEFAULT 0")

    def addSpotifyNeedsReauthColumnIfMissing(self) -> None:
        """Add users.spotify_needs_reauth (migrate1_30_0) if missing - flags
        an account whose stored refresh token was rejected by the Web API
        recently-played backfill for lacking the user-read-recently-played
        scope, so Profile can surface "re-authorize with Spotify" instead of
        the listener silently failing every poll. Guarded so re-running the
        migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "spotify_needs_reauth" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_needs_reauth INTEGER NOT NULL DEFAULT 0")

    def addMilestonesBaselineColumnIfMissing(self) -> None:
        """Add users.milestones_baseline_at (migrate1_33_0) if missing - the
        timestamp of a user's first milestone-detection pass, which marks
        everything they'd already achieved by then as seen (no notification)
        so the feature shipping doesn't flood existing accounts. The
        user_milestones table itself is a plain CREATE TABLE IF NOT EXISTS in
        SCHEMA (auto-created on the next connect), so only this column on the
        pre-existing users table needs an ALTER. Guarded so re-running the
        migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "milestones_baseline_at" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN milestones_baseline_at REAL")

    def addHideTagsPanelColumnIfMissing(self) -> None:
        """Add users.hide_tags_panel (migrate1_38_0) if missing - a per-user
        preference (set on /profile) that hides the tag panel on song/artist/
        album detail pages, independent of the admin's instance-wide tags
        kill switch (isTagsEnabled). Guarded so re-running the migration
        doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "hide_tags_panel" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN hide_tags_panel INTEGER NOT NULL DEFAULT 0")

    def addHideNowPlayingColumnIfMissing(self) -> None:
        """Add users.hide_now_playing (migrate1_39_0) if missing - the per-user
        opt-out (set on /profile) for broadcasting what you're currently
        playing to people you share with, independent of the admin's
        instance-wide isFriendsNowPlayingEnabled switch. Guarded so re-running
        the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "hide_now_playing" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN hide_now_playing INTEGER NOT NULL DEFAULT 0")

    def addTopListWindowColumnIfMissing(self) -> None:
        """Add users.default_top_list_window (migrate1_46_0) if missing - the
        Top Songs/Artists/Albums pages' own default time window, split off from
        default_dashboard_window.

        The column DEFAULT carries the whole migration: those pages were
        hardcoded to all-time before this, so every existing row wants exactly
        the value the ALTER already gives it, and there is nothing to backfill.
        Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "default_top_list_window" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN default_top_list_window "
                             "TEXT DEFAULT 'all time'")

    def addDisplayNameColumnIfMissing(self) -> None:
        """Add users.display_name (migrate1_45_0) if missing - the editable
        label that stands in for the immutable username key wherever a person
        is named. NULL means "display as the username", so existing rows need
        no backfill: the ALTER's own default is already the right answer for
        every account that has never set one. Guarded so re-running the
        migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "display_name" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
