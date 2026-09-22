# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""Shared module-level imports and constants for the Repository query mixins.

Split out of Database/repository.py so every Database/queries/*.py mixin and
the composed Repository can pull the same catalog/plays/settings constants and
db-layer helpers from one place (`from Database.queries._base import *`).

WHICH CONSTANTS LIVE HERE, AND WHICH IN config.py

Both are fine to import from; the direction is what matters. config.py imports
nothing from the app or from Database (see its own docstring), so anything may
import it and no cycle is possible - Database/queries/settings.py and trends.py
do exactly that. There is no rule against it, and reading one into existence
costs a round trip: a constant put in config.py but used from Database/
database.py, which happens not to import config, fails with a NameError that
looks architectural and isn't.

The split that IS real is about audience. A value the SQL itself needs -
ranking weights, retry windows, queue bounds, app_settings keys - belongs here,
next to the queries that read it. A value the pages need - page size, chart
limits, sort whitelists - belongs in config.py, where the app layer already
looks. When a constant is genuinely for both, config.py is the better home: the
data layer can reach it, and app.py re-exports it for free.
"""
import datetime
import json
import secrets
import threading
import time
from pathlib import Path

try:
    import Database.db as db
    from Database.db import (ConnectionManager, SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON,
                             BEHAVIORAL_COLUMNS, SPOTIFY_TRACK_ID_LENGTH, UNKNOWN_ALBUM_NAME)
    from Database.secret_store import (encryptSecret, decryptSecret, isEncrypted, isForeignKeyed,
                                       keyFingerprint)
    #< the one home of the day constant; star-exported from here to every
    #  query mixin like the names above (utils takes part in no import cycle)
    from Database.utils import SECONDS_PER_DAY
except ModuleNotFoundError:
    import db
    from db import (ConnectionManager, SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON,
                    BEHAVIORAL_COLUMNS, SPOTIFY_TRACK_ID_LENGTH, UNKNOWN_ALBUM_NAME)
    from secret_store import (encryptSecret, decryptSecret, isEncrypted, isForeignKeyed,
                              keyFingerprint)
    from utils import SECONDS_PER_DAY

try:
    from Database.backup_settings import (
        BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX,
        BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX,
    )
except ModuleNotFoundError:
    from backup_settings import (
        BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX,
        BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX,
    )

IMAGE_KIND_TRACK = "track"
IMAGE_KIND_ARTIST = "artist"
IMAGE_STATUS_PENDING = "pending"
IMAGE_STATUS_OK = "ok"
IMAGE_STATUS_FAILED = "failed"

# Everything skip-ranked is ordered by skip RATE, but a raw rate is wild when
# you've barely heard something - skipped once out of two encounters is not a
# 50% habit. So each row is ranked as if it started with this many encounters
# at the library's own average skip rate, exactly like judging a restaurant
# with one 5-star review against one with 500: the barely-heard drift toward
# normal, the heavily-played keep their real number. Replaces an earlier
# minimum-encounters cutoff, which hid rows entirely instead of tempering them.
# Higher = more sceptical of small samples.
SKIP_RATE_PRIOR_WEIGHT = 10

# How long the metadata backfiller waits before re-attempting an album it already
# processed - covers restricted/blanked albums whose metadata Spotify may fill in
# (or unblock) later, without hammering the API for permanently dateless albums.
ALBUM_BACKFILL_RETRY_SECONDS = 7 * 24 * 3600

# How long an album stays OUT of the artistless-track queue once its complete
# track list has been walked and every artist credit in it applied.
#
# Much longer than the window above because it answers a different question.
# That one chases metadata Spotify may fill in later; this one records that we
# have already seen everything there is to see - a track still without credits
# after it is one Spotify does not credit, and asking weekly only spends the
# per-app catalog quota the ISRC step shares. Not permanent, because an album
# CAN gain a new artistless track later (an import writes one), and a
# permanent exclusion is the shape this project has twice had to ship a
# migrator to undo.
ALBUM_ARTIST_REPAIR_RETRY_SECONDS = 90 * 24 * 3600

# How long the ISRC backfiller waits before re-asking about a track Spotify
# returned no ISRC for. Longer than the album window (which chases metadata that
# genuinely gets filled in later) because an ISRC is assigned once, when the
# recording is registered - a track that has none today is very unlikely to
# grow one next week, and the catalog is ~25k tracks against a 50-per-request
# endpoint. Tracks that DO get an ISRC leave the queue permanently.
TRACK_ISRC_RETRY_SECONDS = 30 * 24 * 3600

# How long the Last.fm genre backfiller waits before re-attempting an entity
# whose lookup came back empty/not-found. Entities that got real (non-inherited)
# genres never re-enter the queue - community tags are stable enough that a
# one-time fetch is the whole point of marking them attempted.
GENRE_BACKFILL_RETRY_SECONDS = 30 * 24 * 3600

# How many leading track_artists.position slots the genre backfiller queues
# for an artist lookup - 0 is the primary credit; widened past 0 so
# feature/collab-only artists (never anyone's primary) aren't permanently
# excluded from the backfill. Bounded rather than unlimited: position <= 4
# covers 99%+ of all real credit rows in practice, so it captures nearly
# every feature-only artist without unboundedly widening the queue.
GENRE_BACKFILL_MAX_ARTIST_POSITION = 4

# How long the background biography backfiller waits before re-attempting an
# artist whose fetch came back with no usable bio. An artist with real bio
# text never re-enters the queue (see getArtistsMissingBiographies) - only a
# definitive-empty result is retried, in case Last.fm gains a bio later.
BIOGRAPHY_BACKFILL_RETRY_SECONDS = 30 * 24 * 3600

# app_settings key for the admin's instance-wide toggle: do inherited (artist-
# derived) genre rows count in genre stats and coverage? Absent row = enabled.
INHERITED_GENRES_SETTING_KEY = "genres_include_inherited"
APP_SETTING_TRUE = "1"
APP_SETTING_FALSE = "0"

# app_settings keys for the admin's instance-wide feature kill switches (see
# the overview settings panel) - each defaults to enabled (absent row), same
# contract as INHERITED_GENRES_SETTING_KEY above.
SPOTIFY_BACKFILL_SETTING_KEY = "spotify_api_backfill_enabled"
LASTFM_BACKFILL_SETTING_KEY = "lastfm_genre_backfill_enabled"
DATA_SHARING_SETTING_KEY = "data_sharing_enabled"
REGISTRATION_SETTING_KEY = "registration_enabled"
SHARE_LINKS_SETTING_KEY = "share_links_enabled"
ARTIST_BIO_SETTING_KEY = "artist_bio_enabled"
ALBUM_BIO_SETTING_KEY = "album_bio_enabled"
MILESTONES_SETTING_KEY = "milestones_enabled"
MILESTONE_RECALC_SETTING_KEY = "milestone_recalc_enabled"
TAGS_SETTING_KEY = "tags_enabled"
FRIENDS_NOW_PLAYING_SETTING_KEY = "friends_now_playing_enabled"

# The one switch that defaults to DISABLED (absent row = off), unlike every key
# above. It changes how plays are DETECTED - listening for Spotify's pushed
# connect-state instead of polling for it - and the measurement behind it (see
# eventDrivenConnectStatePlan.md) covered 9 minutes of listening: enough to
# clear every structural unknown, not enough to make it the default. Turning it
# on is deliberate; the poll loop stays underneath as the fallback.
PUSH_LISTENER_SETTING_KEY = "push_listener_enabled"
TRACK_MERGE_SETTING_KEY = "track_merge_enabled"
# Bumped by every wrapped-cache invalidation; an in-flight recalculation that
# started under an older value discards its save. See deleteAllWrapped.
WRAPPED_INVALIDATION_GENERATION_KEY = "wrapped_invalidation_generation"
# How far either side of a UTC year boundary a play still counts as possibly
# belonging to the neighbouring year, for deleteCachedWrappedForTracks. A
# Wrapped year is bucketed in the USER's timezone (the worker builds its bounds
# from datetime.now(tz=self.tz)); that delete runs in the shared repo, which has
# no user's tz to hand, so it widens instead of guessing. The real ceiling is
# UTC+14/UTC-12, i.e. 14h; 26h clears it with room for any DST arithmetic.
# Erring wide costs one extra rebuild, erring narrow strands a year that is
# permanently wrong and that nothing downstream would ever notice.
WRAPPED_YEAR_TZ_SLACK_SECONDS = 26 * 3600

# Instance-wide skip threshold (app_settings). This is the single, admin-tunable
# boundary between a "skip" and a real listen - it replaced both the old fixed
# play_skips split and getCompletionStats' 30s line. Two modes:
#   "seconds": a play is a skip when time_played < value * 1000 (value in 5..60s)
#   "percent": a skip when it played less than value% of the track's duration
#              (value in 5..25%); tracks with unknown duration (<=0) fall back to
#              the fixed db.SKIP_THRESHOLD_MS floor.
# Materialized per row into plays.is_skip at write time and by recomputeSkipFlags()
# whenever the threshold changes. Default seconds/5 matches the historical
# SKIP_THRESHOLD_MS the merge migration seeds.
MS_PER_SECOND = 1000
PERCENT_DIVISOR = 100.0        #< int percent setting -> ratio of a duration
SKIP_THRESHOLD_MODE_KEY = "skip_threshold_mode"
SKIP_THRESHOLD_VALUE_KEY = "skip_threshold_value"
SKIP_MODE_SECONDS = "seconds"
SKIP_MODE_PERCENT = "percent"
SKIP_SECONDS_MIN = 5
SKIP_SECONDS_MAX = 60
SKIP_PERCENT_MIN = 5
SKIP_PERCENT_MAX = 25
SKIP_THRESHOLD_DEFAULT_MODE = SKIP_MODE_SECONDS
SKIP_THRESHOLD_DEFAULT_VALUE = 5

# app_settings keys for numeric tunables migrated out of code constants. Each
# falls back to its code default (config.py / Database.database) when the row is
# absent, so behavior is unchanged until an admin sets one. DISCOVER_ARTIST_LIMIT
# is read per request (live). The *_WORKERS values size ThreadPoolExecutors built
# once at process start, so a change only applies after a restart.
DISCOVER_ARTIST_LIMIT_KEY = "discover_artist_limit"
DISCOVER_ARTIST_LIMIT_MIN = 1
DISCOVER_ARTIST_LIMIT_MAX = 25
IMAGE_DOWNLOAD_WORKERS_KEY = "image_download_workers"
ARTIST_BIO_FETCH_WORKERS_KEY = "artist_bio_fetch_workers"
ALBUM_BIO_FETCH_WORKERS_KEY = "album_bio_fetch_workers"
WORKER_COUNT_MIN = 1
WORKER_COUNT_MAX = 32

# getCompletionStats' complete-vs-partial boundary, stored as an int percent of
# the track's duration (a listen at/over this counts as complete, else partial).
# Companion to the skip threshold - together they define the completion pie.
COMPLETION_COMPLETE_PERCENT_KEY = "completion_complete_percent"
COMPLETION_COMPLETE_PERCENT_MIN = 50
COMPLETION_COMPLETE_PERCENT_MAX = 100
COMPLETION_COMPLETE_PERCENT_DEFAULT = 80

# Whether login enforces the "do these cookies belong to this email" check
# (was env-only: SKIP_EMAIL_VERIFICATION disabled it). Absent row = enabled;
# the env var still force-disables regardless of the toggle.
EMAIL_VERIFICATION_SETTING_KEY = "email_verification_enabled"

# How long the Last.fm genre / biography backfillers wait before re-attempting an
# entity whose lookup came back empty, stored in DAYS (was the fixed
# *_BACKFILL_RETRY_SECONDS constants above). Bounds keep it sane.
GENRE_BACKFILL_RETRY_DAYS_KEY = "genre_backfill_retry_days"
BIO_BACKFILL_RETRY_DAYS_KEY = "bio_backfill_retry_days"
BACKFILL_RETRY_DAYS_MIN = 1
BACKFILL_RETRY_DAYS_MAX = 365

# The ISRC matcher's daily slot. It used to run on every metadata-backfill
# cycle - a few minutes apart, and once per user - which was fine while a run
# that merged nothing cost nothing, and expensive the moment one merged
# something: each such run drops the cached Wrapped years its groups touch, and
# new ISRCs keep completing pairs. 137 tracks merged in 23 batches over two
# days cost the live instance 147 Wrapped rebuilds a day against ~20-40 before.
# Narrowing the invalidation (deleteCachedWrappedForTracks) cut what one batch
# costs; this cuts how many there are, by letting the merges pile into one pass.
# The trade: a pair completed by a new ISRC waits up to a day to fold, during
# which both sides simply count separately. The stamp lives in app_settings, not
# on the class, so it is shared by the per-user workers and survives a restart.
TRACK_MERGE_LAST_RUN_KEY = "track_merge_last_run"
TRACK_MERGE_MIN_INTERVAL_SECONDS = SECONDS_PER_DAY

# getBucketedPlayTotals' fixed UTC bucket width. 15 minutes is the smallest
# granularity any real-world UTC offset uses (e.g. Asia/Kathmandu +5:45), so
# every play in one bucket maps to the same local day/hour/weekday no matter
# which IANA timezone Python later applies - which is what lets the heavy
# per-play aggregation move into SQL without losing timezone correctness.
PLAY_BUCKET_SECONDS = 15 * 60

# Whitelist mapping the public sortBy values to the SQL output-column aliases
# they're allowed to sort by. sortBy is interpolated directly into ORDER BY
# (column names can't be bound as query parameters), and it's user-controlled
# (app.py's sortBy query param) - this whitelist is what makes that safe.
# "name" sorts COLLATE NOCASE so e.g. "abba" and "ABBA" interleave by letter
# instead of every uppercase name sorting before every lowercase one (SQLite's
# default BINARY collation).
SONG_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
    "recent": "last_played_at",
}

ALBUM_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}

ARTIST_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}

# The "this play actually finished" test, as a bare SQL fragment. Lives here as
# a string because two kinds of caller need it: the clause builders below
# (which splice it into a dynamically-assembled WHERE) and the handful of
# static queries that interpolate it into an otherwise-fixed statement. Aliases
# are formatted in so both spellings of the plays table (`p` and the unaliased
# `plays`) can share one definition.
FULL_PLAY_PREDICATE = "{track}.duration_ms <= 0 OR {plays}.time_played >= {track}.duration_ms * ?"


class SqlFragments:
    """WHERE/JOIN fragments shared by the query mixins.

    Every builder appends its own bound values to `params` and returns the SQL
    to splice in, so a caller must build its clauses in the order the
    placeholders end up in the statement. That contract is the whole reason
    these are worth sharing: each one used to be copy-pasted per query, and a
    change to the filter meant finding every copy - the full-plays filter alone
    had eleven, and adding it to the skip queries missed several.
    """

    @staticmethod
    def _idSetClause(params: list, column: str, ids: list[str] | None) -> str:
        """`AND <column> IN (...)` for an explicit set of ids - a tag filter, a
        playlist export, a shared-with-me list.

        None means "no filter". An EMPTY list is an explicit empty set and must
        match nothing: `IN ()` isn't valid SQLite, so it becomes a bare `AND 0`.
        Getting that backwards shows a whole library under a tag nobody has
        used, which is why every caller routes through here."""
        if ids is None:
            return ""
        if not ids:
            return " AND 0"
        params += ids
        placeholders = ",".join("?" for _ in ids)
        return f" AND {column} IN ({placeholders})"

    @staticmethod
    def _keysetAfterClause(params: list, afterTs: float | None, afterId: int | None,
                            tsColumn: str = "played_at", idColumn: str = "id") -> str:
        """`AND ...` for the export's oldest-first keyset pager (X4): `afterTs`
        alone pages by `tsColumn >= ?`, which is what every caller used before
        `afterId` existed and stays the behaviour when only afterTs is passed.
        `afterTs` WITH `afterId` pages by the composite `(tsColumn, idColumn)`
        key the callers order by - `tsColumn > ? OR (tsColumn = ? AND idColumn
        > ?)` - so a cluster of rows sharing one timestamp (two different
        tracks logged at the exact same instant - the Musicolet importer's
        shape) is paged through by id instead of getting stuck on `>=`
        forever re-fetching the same window.

        None for both means "no cursor, start from the beginning"."""
        if afterTs is None:
            return ""
        if afterId is None:
            params.append(afterTs)
            return f" AND {tsColumn} >= ?"
        params += [afterTs, afterTs, afterId]
        return f" AND ({tsColumn} > ? OR ({tsColumn} = ? AND {idColumn} > ?))"

    @staticmethod
    def _jsonIdSetClause(params: list, column: str, ids: list[str] | None) -> str:
        """_idSetClause for a set that can be LARGE: the ids travel as ONE bound
        parameter, a JSON array unpacked by json_each.

        _idSetClause binds one parameter per id, which caps it at SQLite's
        SQLITE_MAX_VARIABLE_NUMBER - 32766 here, and that is a COMPILE-TIME
        maximum: sqlite3_limit() clamps to it and cannot raise it, so Python's
        bundled sqlite3 offers no way around it. That is fine for a tag filter or
        a playlist, and not fine for a set derived from a search: a single-letter
        query matches almost the whole catalog (measured: "e" -> 23,534 track ids
        on a 24.5k-track library), which is under the ceiling only by accident of
        this library's size.

        Costs nothing measurable - 188ms vs 178ms for an 18,459-id set - and
        removes the ceiling entirely rather than raising it, so there is no
        threshold to pick and no fallback path to keep tested.

        Same contract as _idSetClause: None means "no filter", an explicit empty
        list means "match nothing" and becomes `AND 0`."""
        if ids is None:
            return ""
        if not ids:
            return " AND 0"
        params.append(json.dumps(list(ids)))
        return f" AND {column} IN (SELECT value FROM json_each(?))"

    @staticmethod
    def _likePattern(query: str) -> str:
        """Wraps `query` for a LIKE '%...%' match, escaping LIKE's own wildcard
        characters so a literal "%" or "_" typed by the user is matched as text
        rather than treated as a wildcard - matches the substring-only
        semantics of the Python `in` check this replaces."""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def searchWords(query: str) -> list[str]:
        """`query` split into the words a match has to satisfy.

        Whitespace-separated, empties dropped, so trailing/repeated spaces don't
        produce a word that matches everything."""
        return [word for word in (query or "").split() if word]

    def _perWordConditions(self, params: list, query: str, condition: str,
                            patternsPerWord: int) -> list[str]:
        """One parenthesised copy of `condition` per word in `query`, with that
        word's LIKE patterns appended to `params`. The caller joins them, because
        the sites differ in whether they need a leading WHERE or AND.

        The whole query used to be ONE substring matched against each field
        separately, so the most natural way to search - "artist song" - could
        never match: no single field contains both. Searching this library for
        "Luis Despacito" returned nothing while "Despacito" returned nine rows.

        AND across words, OR across fields (that's the caller's template): every
        word must appear SOMEWHERE - title, album, playlist or any credited
        artist - but not all in the same one. So it narrows as more words are
        typed, and for a multi-word query it returns a superset of the old
        behaviour, never less.

        Costs nothing measurable: each ANDed word shrinks the candidate set
        rather than adding a pass, and the artist EXISTS was already evaluated
        per candidate row. Measured on a real 131k-play library at 207ms for one
        word vs 201ms for two.

        An empty query yields an empty list, so a caller that joins gets no
        clause at all - which is how every caller already guards it."""
        words = self.searchWords(query)
        for word in words:
            params += [self._likePattern(word)] * patternsPerWord
        return [f"({condition})"] * len(words)

    def _perWordAndClause(self, params: list, query: str, condition: str,
                           patternsPerWord: int) -> str:
        """_perWordConditions ready to append to an existing WHERE - the shape
        most call sites want."""
        return "".join(
            f" AND {clause}"
            for clause in self._perWordConditions(params, query, condition, patternsPerWord))

    # /history's search used to run here as three joins plus a four-field
    # predicate: JOIN tracks, LEFT JOIN albums, and a LEFT JOIN playlists matched
    # on substr(played_from, instr(...)) that no index could serve. All of it is
    # gone - _playSearchNarrowClause resolves the same match to two id sets, and
    # searchPlays' SELECT only ever read p.* columns, so the joins existed purely
    # to serve the predicate. Same for getSongsPage's own inline copy.

    #< Top Artists searches the name only - see getArtistAggregates. One pattern.
    _ARTIST_NAME_CONDITION = "ar.name LIKE ? ESCAPE '\\'"

    # The album and skipped-track conditions that used to live here are gone:
    # both correlated an artist EXISTS against the row being aggregated, so both
    # ran once per play rather than once against the catalog. They are id sets
    # now - see _albumSearchNarrowClause and _searchNarrowClause.

    def _searchNarrowClause(self, params: list, searchQuery: str | None, column: str,
                             canonicalColumn: str | None = None) -> str:
        """A song search, as a track-id set instead of an inline predicate.

        The predicate used to sit inside the per-play aggregate, so its artist
        EXISTS was re-evaluated for each of ~17.5k grouped tracks instead of once
        against the 24.5k-row catalog. That made the cost structural rather than
        proportional: on a real library the search page took ~850ms, and took the
        same 847ms for a term matching NOTHING.

        Resolving the ids first turns it into a catalog query plus an indexed
        membership test - the same shape as _trackSetClause, which took the artist
        song list from 985ms to 19ms. Measured, on the same library:
            "love"       847ms -> 217ms   (-74%)
            "radiohead"  846ms -> 203ms   (-76%, 0 results either way)
            "the"        797ms -> 356ms   (-55%)
        Results are identical, row for row and in the same order - the resolver
        applies the very same condition getSongsPage applied inline.

        Via json_each, so the id set costs ONE bound parameter however large it
        gets: a single-letter query matches almost the whole catalog, and SQLite's
        parameter ceiling cannot be raised (see _jsonIdSetClause).

        `canonicalColumn` is the grouped key of a query that MERGES (the `c.id`
        of _canonicalTrackJoin, or the COALESCE spelled out). Given one, the
        narrowing moves onto it and the matched ids are mapped to their
        canonicals in SQL, so a term matching one release of a merge group
        selects the whole group: a search decides WHICH songs are listed, and
        must not also decide which of a song's plays are counted. Without it
        the group's row carried the matching release's subtotal under the
        canonical's title, so only the number moved - the same boundary
        getTaggedTrackIds expands across, and the rediscovery/fresh-find
        narrowings spell out (see Database/queries/trends.py).

        The mapping is a subquery rather than a Python expansion on purpose:
        _expandToMergeGroups binds one parameter per id, and this set is
        exactly the one that cannot (see _jsonIdSetClause). Free when nothing
        is merged - COALESCE(canonical_id, id) is then the id, and the join
        makes the two columns equal - which is what keeps
        tests/test_search_two_phase's row-for-row equality true.

        MEASURED at the worst case it has (8,000 tracks all matching, 4,000
        merge groups, 120k plays): 165.3ms on t.id -> 153.1ms here, best of
        five. It does not cost anything because it does not change the driver -
        plays still leads on idx_plays_user_time, the id set is still one
        materialised LIST SUBQUERY, and `m` is a primary-key seek per entry.

        An empty query narrows nothing. A query matching no track yields `AND 0`
        from _jsonIdSetClause, which is correct and skips the aggregate entirely."""
        if not self.searchWords(searchQuery):
            return ""
        clause = self._jsonIdSetClause(params, column, self.getMatchingTrackIds(searchQuery))
        if not canonicalColumn or not clause or clause == " AND 0":
            return clause
        #< the matched ids are still the ones bound; only the test moves
        return (f" AND {canonicalColumn} IN (SELECT COALESCE(m.canonical_id, m.id) FROM tracks m"
                f" WHERE m.id IN (SELECT value FROM json_each(?)))")

    def _albumSearchNarrowClause(self, params: list, searchQuery: str | None,
                                  column: str = "al.id") -> str:
        """_searchNarrowClause for an album search - the same two-phase move,
        against the album resolver (see getMatchingAlbumIds for why the album
        condition needs its own, and for the measurements).

        Composes with an explicit album filter rather than replacing it: both
        are clauses on the same column, so a tag-filtered page that is also
        searched intersects the two sets."""
        if not self.searchWords(searchQuery):
            return ""
        return self._jsonIdSetClause(params, column, self.getMatchingAlbumIds(searchQuery))

    def _playSearchNarrowClause(self, params: list, searchQuery: str | None) -> str:
        """/history's search, as two id sets instead of three joins and a predicate.

        It matches what was played (track name, album, any credited artist) OR where
        it was played from (the playlist/album name), so it needs both halves - but
        neither depends on the play, only on which track and which played_from value
        the play carries. So the whole thing resolves to "track_id in this set, or
        played_from in that one", and the tracks/albums/playlists joins disappear
        entirely: searchPlays' SELECT reads only p.* columns, so those joins existed
        purely to serve the predicate.

        The playlists join was the odd one - it matched on
        substr(played_from, instr(...)), which no index can serve, evaluated per
        play row. It turns out to have been cheap anyway (123ms -> 128ms of the
        854ms), because only 62 of 73k plays carry a played_from at all.

        Measured on the real library, best of 3:
            count "love"       831ms -> ~90ms
            count "xylophone"  839ms -> ~80ms
        and it fixes the page too, which was NOT fast - it merely looked fast for
        common terms, where LIMIT 50 stops early. A term with no matches had to scan
        everything: "radiohead" took 886ms to return zero rows.

        Resolved PER WORD, and this is the subtle part: the old predicate ORed all
        four fields for each word independently, so "Beta Roadtrip" could match a
        play whose TRACK supplied "Beta" and whose PLAYLIST supplied "Roadtrip".
        Resolving one combined set per side instead would demand that every word
        match the same side, and that play would be lost - a real regression, caught
        by tests/test_search_two_phase's per-word-spans-both case. So each word gets
        its own pair of sets, ORed across the two sides and ANDed across words.

        A word that matches on NEITHER side can never be satisfied, so the whole
        clause short-circuits to `AND 0` rather than scanning the history."""
        words = self.searchWords(searchQuery)
        if not words:
            return ""
        #< where this call found `params`, so the short-circuit below can put it
        #  back exactly as it was
        paramsBefore = len(params)
        clauses = []
        for word in words:
            trackIds = self.getMatchingTrackIds(word)
            playedFrom = self.getMatchingPlayedFrom(word)
            if not trackIds and not playedFrom:
                # Unwind this call's own binds first. Each word appends its id
                # blob as it is resolved, so by the time a LATER word turns out
                # to match nothing, the earlier ones are already in `params` -
                # and " AND 0" carries no placeholder to consume them. The
                # statement's bind count then disagreed with its ? count and
                # SQLite refused it, so /history 500'd on the ordinary way
                # people narrow a search: adding a word to a query that is
                # already returning rows. Word order decided it - a
                # non-matching word FIRST returns before any append, which is
                # why this survived: it is the spelling every test used.
                del params[paramsBefore:]
                return " AND 0"
            sides = []
            if trackIds:
                params.append(json.dumps(list(trackIds)))
                sides.append("p.track_id IN (SELECT value FROM json_each(?))")
            if playedFrom:
                params.append(json.dumps(list(playedFrom)))
                sides.append("p.played_from IN (SELECT value FROM json_each(?))")
            clauses.append("(" + " OR ".join(sides) + ")")
        return "".join(f" AND {clause}" for clause in clauses)

    # The track set behind an artist / album filter. Formatted with the bound
    # placeholders by _trackSetClause below.
    ARTIST_TRACKS_SUBQUERY = "SELECT ta_ts.track_id FROM track_artists ta_ts WHERE ta_ts.artist_id IN ({placeholders})"
    ALBUM_TRACKS_SUBQUERY = "SELECT t_ts.id FROM tracks t_ts WHERE t_ts.album_id IN ({placeholders})"

    @staticmethod
    def _trackSetClause(params: list, subquery: str, ids: list[str] | None,
                        playsColumn: str = "p.track_id") -> str:
        """The seekable companion to an artist/album filter on a plays query.

        A filter written against the JOINED table - `al.id = ?`, or an EXISTS
        over track_artists - gives SQLite nothing to seek on `plays`, so it
        drives off idx_plays_user_time, reads EVERY play the user has, and
        discards all but the entity's. That was ~1.2s for a well-played artist
        on a real library, on queries a single detail page runs several of.

        Naming the same tracks as `AND <playsColumn> IN (<subquery>)` lets it
        resolve the set once and seek idx_plays_user_track per track. It is
        redundant by construction: the caller's own filter still decides
        membership, so this can only ever change the PLAN, not the rows. It is
        also a membership test rather than a join, so a track credited to
        several artists still yields one row per play, not one per credit.

        Mirrors _idSetClause's contract: None means "no filter", and an empty
        list adds nothing because _idSetClause has already emitted `AND 0`.
        Pass a single id as a one-element list.

        tests/test_narrowed_query_equivalence.py pins that narrowed results
        match the same figures derived from the unnarrowed query."""
        if not ids:
            return ""
        params += ids
        placeholders = ",".join("?" for _ in ids)
        return f" AND {playsColumn} IN ({subquery.format(placeholders=placeholders)})"

    def _anyTrackMerges(self) -> bool:
        """Whether any track is merged at all - the gate that keeps the merge
        feature free while it is off.

        The per-play canonical hop in the genre and artist aggregates measured
        +87% and +193% on a real library, which this repo's own standards call
        unshippable as a constant tax. But the hop buys nothing while
        canonical_id is NULL everywhere - which is every instance until the
        admin toggle is flipped - so those queries ask this first and keep
        their original fast shape until a merge actually exists. With merges
        present the probe short-circuits on the first hit; without them it is
        one ~25k-row column scan, ~1ms, against the ~230ms it saves."""
        row = self._conn().execute(
            "SELECT 1 FROM tracks WHERE canonical_id IS NOT NULL LIMIT 1").fetchone()
        return row is not None

    def _mergeGroupClause(self, params: list, trackId: str, column: str = "track_id") -> str:
        """Membership filter for the WHOLE merge group of `trackId`.

        `AND track_id = ?` on a song-page query answers about one release; the
        page is about the recording. This expands either end of a merge - hand
        it a merged member and it resolves the canonical first - to `IN (the
        group's ids)`, which stays seekable on idx_plays_user_track per id.
        For an unmerged track the subquery yields exactly the one id, so the
        clause degrades to what it replaced."""
        canonical = self.resolveCanonicalTrackId(trackId)
        params += [canonical, canonical]
        return f" AND {column} IN (SELECT id FROM tracks WHERE id = ? OR canonical_id = ?)"

    def _expandToMergeGroups(self, ids: list[str]) -> tuple[list[str], dict, dict]:
        """(memberIds, canonicalOfRequested, canonicalOfMember) for a set of
        track ids - the Python half of group-aware membership tests.

        Queries that ask "did the user play THESE?" (played-flags, the rank
        movement badge) receive arbitrary release ids while the user's plays
        may sit on siblings. Expanding in Python keeps the plays query itself
        the same seekable IN it always was; the two catalog lookups are PK
        probes over a handful of ids. Unknown ids map to themselves, so a
        caller about to 404 still gets its miss."""
        if not ids:
            return [], {}, {}
        conn = self._conn()
        marks = ",".join("?" for _ in ids)
        canonicalOfRequested = {
            row["id"]: row["canon"]
            for row in conn.execute(
                f"SELECT id, COALESCE(canonical_id, id) AS canon FROM tracks WHERE id IN ({marks})",
                list(ids))
        }
        canonicals = sorted({canonicalOfRequested.get(i, i) for i in ids})
        cMarks = ",".join("?" for _ in canonicals)
        canonicalOfMember = {
            row["id"]: row["canon"]
            for row in conn.execute(
                f"SELECT id, COALESCE(canonical_id, id) AS canon FROM tracks "
                f"WHERE id IN ({cMarks}) OR canonical_id IN ({cMarks})",
                canonicals + canonicals)
        }
        for i in ids:   #< unknown ids stay queryable and map to themselves
            canonicalOfMember.setdefault(i, canonicalOfRequested.get(i, i))
        return list(canonicalOfMember.keys()), canonicalOfRequested, canonicalOfMember

    @staticmethod
    def _mergesCanonically(trackId=None, artistId=None, albumId=None) -> bool:
        """Whether this query counts a merged song once - everything but an
        album scope does.

        A merge says two catalog rows are the same recording, which is true
        everywhere - but it is not the right ANSWER everywhere. An album page
        asks "what is on this album", and the canonical belongs to exactly one
        release, so merging there would hand an album a row whose title, cover
        and link belong to a different one. That is the ONLY scope that keeps
        per-release rows.

        An artist scope merges (2026-09-05; it kept per-release rows before):
        every release of the song is that artist's own, so the canonical's
        title, cover and link still belong on the page, and the artist's total
        still equals the sum of its rows - membership is decided by the PLAYED
        track (the EXISTS / _trackSetClause on t / p.track_id), so a release
        credited to someone else stays out of this artist's count. Per-release
        rows put one song on the artist page once per release, which is the
        duplicate the merge exists to remove. _uniqueSongCountSql collapses
        the artist's "unique songs" the same way, so the count and the list
        beside it agree (tests/test_track_merge_audit.py).

        A trackId lookup DOES merge: it is the song detail page's own query,
        and that page is the canonical's page - the row every merged global
        list links to. Answering per-release there is the central
        contradiction the audit found: the hero saying 9 plays under a caption
        promising "plays across all of them are counted together" while Top
        Songs says 12. (`trackId` and `artistId` are accepted and ignored so
        call sites read uniformly; only the album narrowing decides.)"""
        del trackId, artistId   #< deliberate - see above
        return albumId is None

    @staticmethod
    def _canonicalTrackJoin(trackAlias: str = "t", canonicalAlias: str = "c") -> str:
        """Join from the played track to the one it has been merged into.

        A second join rather than grouping by COALESCE(canonical_id, id) on the
        played row: that spelling groups correctly but leaves every non-aggregated
        column free to come from whichever member SQLite happened to read, so the
        title and cover could belong to the version nobody chose. It is also
        slower - 305ms against 119ms on the live database's Top Songs query,
        because the plan loses the tracks index it was grouping on.

        tracks.id is the primary key, so this is an indexed lookup per row."""
        return (f" JOIN tracks {canonicalAlias} "
                f"ON {canonicalAlias}.id = COALESCE({trackAlias}.canonical_id, {trackAlias}.id)")

    @staticmethod
    def _tracksJoin(playsAlias: str = "p", trackAlias: str = "t") -> str:
        """The plays->tracks join the duration-based filters need. Emitted only
        when one of them is active, so queries that scan plays alone stay that
        way."""
        return f" JOIN tracks {trackAlias} ON {trackAlias}.id = {playsAlias}.track_id"

    def _fullPlaysClause(self, params: list, playsAlias: str = "p", trackAlias: str = "t",
                          keepSkips: bool = False) -> str:
        """The Top pages' "Full plays only" filter: keep only plays that reached
        the admin's completion-complete percent of the track's duration.

        A track whose duration is unknown (<= 0) is kept rather than dropped -
        the filter can't judge it, and dropping it would silently hide every
        play of a track whose metadata never arrived.

        `keepSkips` is for the skip-ranked queries, where the filter applied
        verbatim would drop every skip (they are the shortest plays there are)
        and empty a page whose checkbox defaults to on. There, skips stay
        encounters and only the partial listens go.

        Requires `trackAlias` to be joined - see _tracksJoin."""
        params.append(self.getCompletionCompletePercent() / PERCENT_DIVISOR)
        predicate = FULL_PLAY_PREDICATE.format(plays=playsAlias, track=trackAlias)
        skipEscape = f"{playsAlias}.is_skip = 1 OR " if keepSkips else ""
        return f" AND ({skipEscape}{predicate})"
