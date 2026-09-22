# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The owned Spotify cookie-session client (dependencyRewritePlan.md Phase 1).

Replaces spotipyFree's wrapper with the ten methods this application actually
calls, against spotapi directly. Most of the hard-won behavior here used to
live in Database/patches.py as monkey-patches over spotipyFree - the per-user
TLSClient login (the cross-user contamination fix), the Public.song_info track
path (the shared-default-client race fix), and the retry/fallback ladder around
track metadata. Moving them into a class we own means they can no longer be
silently un-applied by an import-order accident, and their tests pin a real
API instead of a patched attribute.

Deliberately NOT carried over from the wrapper:
  - the browser cookie-extraction fallback on login failure (this app manages
    cookies itself; a server recursing into browser profiles was never right),
  - playback control, saved tracks, audio_features - nothing here calls them.

The output shapes are pinned by tests/test_spotify_client_contract.py, whose
expectations come from the CONSUMERS (Client.formatTrack, the importers, the
listener) - see Database/Spotify/formatting.py for the mapping itself.
"""

import atexit
import json
import logging
import re
import time
from collections.abc import Mapping
from collections import deque
from contextlib import contextmanager

import spotapi
import spotapi.user
from spotapi.public import client_pool

from Database.rate_limit import (
    SPOTIFY_LIMITER, SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS,
    SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS, SpotifyLocallyRateLimitedError,
)
from Database.Spotify.formatting import (
    formatTrackUnion, formatSearchTrackData, formatAlbumUnion, formatArtistUnion,
    formatPlaylistV2, formatProfile, formatContext, openSpotifyUrl,
)
from Database.Spotify.recentlyPlayed import RecentlyPlayedManager, _isSessionClosedError

try:
    from Database.db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME
except ModuleNotFoundError:
    from db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME

logger = logging.getLogger(__name__)

# Endpoint label for the shared limiter's backoff reason - short enough for
# the /admin card, specific enough to say WHICH Spotify surface pushed back.
# (Same value as Database.patches.ENDPOINT_TRACK_INFO; the patches module's
# copy leaves with the spotipyFree patches in Phase 1.5.)
ENDPOINT_TRACK_INFO = "track metadata"

# tracks.availability_reason value for a track Spotify wouldn't describe, sitting
# alongside its own COUNTRY_RESTRICTED/PAYWALL_CONTENT reasons - so the UI's
# "May be unavailable" badge covers this case with no template change.
TRACK_INFO_UNAVAILABLE_REASON = "TRACK_INFO_UNAVAILABLE"

# How many extra attempts an incomplete song_info response gets before the
# caller degrades to a fallback record. Kept low and on a SHORT FIXED delay
# rather than the transient ladder's 1/2s exponential backoff: this runs
# inside the poll loop's callback, so during a burst every affected track pays
# the wait. One extra attempt is enough to ride out the sub-second gap that
# produced the observed cluster without making a genuinely-gone track slow.
INCOMPLETE_TRACK_INFO_RETRIES = 1
INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS = 2

TRACK_FETCH_MAX_RETRIES = 3            #< transient-failure ladder: 1s, 2s
RECENTLY_PLAYED_BUFFER_SIZE = 50       #< matches the Web API's own recently-played page size
SEARCH_DEFAULT_LIMIT = 10              #< spotapi query_songs' own default, and spotipy search's

# The curl_cffi TLS fingerprint each per-user client impersonates.
TLS_CLIENT_PROFILE = "chrome120"
TLS_CLIENT_AUTO_RETRIES = 3


class IncompleteTrackInfoError(Exception):
    """spotapi answered, but not with a usable track.

    Three degraded shapes were seen in Database/Data/app.log on 2026-07-16, all
    of which used to escape as raw TypeErrors/KeyErrors:

      {"data": None}                     -> TypeError on ["trackUnion"]
      {"data": {"trackUnion": None}}     -> TypeError downstream
      a trackUnion dict with no "uri"    -> KeyError: 'uri', raised deep inside
                                            the formatter, where the track id
                                            is no longer in scope

    Distinguishing this from a transport failure matters because the two need
    different budgets, not because one is unrecoverable: an incomplete response
    is usually a symptom of a degraded session rather than a fact about the
    track. All 11 of these in 11 days of app.log fall inside one 4m47s window
    (2026-07-16 12:59:44-13:04:31), alongside 14 session/websocket failures - so
    they get a short bounded retry, and only then does the caller degrade rather
    than lose the play."""


def extractTrackUnion(payload, trackId: str) -> dict:
    """The trackUnion out of a song_info payload, or IncompleteTrackInfoError.

    Validates the shape rather than just the nullness of each hop: a trackUnion
    can be a perfectly good dict that happens to lack "uri", which no null check
    catches and which the formatter would otherwise emit an id-less track for."""
    data = payload.get("data") if isinstance(payload, dict) else None
    trackUnion = data.get("trackUnion") if isinstance(data, dict) else None
    if not isinstance(trackUnion, dict):
        raise IncompleteTrackInfoError(
            f"song_info returned no track data for {trackId} "
            f"(data={type(data).__name__}, trackUnion={type(trackUnion).__name__})")
    if not trackUnion.get("uri"):
        raise IncompleteTrackInfoError(
            f"song_info returned a track without a uri for {trackId}")
    return trackUnion


def fallbackTrackRecord(trackId: str) -> dict:
    """A minimal stand-in for a track Spotify wouldn't describe, in the same
    spotipy shape formatTrackUnion produces.

    Invents no *facts*: the duration stays 0 and no artists are claimed, because
    a made-up number would read as real metadata downstream. The title is the
    shared UNKNOWN_TRACK_NAME placeholder rather than "" - a blank name rendered
    as an empty row in every list the track appeared in, and it costs nothing,
    since upsertTrack replaces a fallback row's name unconditionally once real
    metadata arrives.

    The id IS real, so the Spotify link is real too - only fabricated ids carry
    an empty url in this codebase.

    The album's ID is per track (album_<trackId>, the same convention the
    importer's fallbacks use), because a single fabricated album id would collect
    every undescribable track from every user into one page of unrelated songs.
    Its NAME is the shared UNKNOWN_ALBUM_NAME placeholder for the same reason the
    title is: it used to pass "", and _formatAlbum's own "Unknown album" default
    never applied because the key was present, so albums.name was stored empty and
    rendered blank on the detail page and in every album link."""
    return {
        "name": UNKNOWN_TRACK_NAME,
        "track_id": trackId,
        "id": trackId,
        "disc_number": 0,
        "track_number": 0,
        "duration_ms": 0,
        "artists": [],
        "album": {"id": f"album_{trackId}", "name": UNKNOWN_ALBUM_NAME, "images": [],
                  "external_urls": {"spotify": ""}, "total_tracks": 0},
        "explicit": False,
        "external_urls": {"spotify": openSpotifyUrl("track", trackId)},
        "popularity": 0,
        "type": "track",
        "external_ids": {"isrc": ""},
        "playability": {"playable": False, "reason": TRACK_INFO_UNAVAILABLE_REASON},
        "created_reason": RESTRICTED_FALLBACK_REASON,
    }


class PersistedQueryError(spotapi.exceptions.SongError):
    """A successful HTTP response rejected the persisted GraphQL operation."""


def _isPersistedQueryError(payload):
    """Recognize the GraphQL marker, including spotapi's status/body exception text."""
    if isinstance(payload, PersistedQueryError):
        return True
    if isinstance(payload, spotapi.exceptions.SongError):
        # ParentException.__str__ only contains the generic message. Exclude
        # 5xx even if an intermediary echoed a GraphQL error in its body.
        detail = payload.error
        if not isinstance(detail, str) or not re.match(r"Status Code: 4\d\d, Response:", detail):
            return False
        return bool(re.search(r"persisted[_\s]?query[_\s]?not[_\s]?found", detail, re.IGNORECASE))
    if not isinstance(payload, Mapping):
        return False
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, Mapping) else None
        for marker in (error.get("message"), code):
            if isinstance(marker, str) and re.fullmatch(
                    r"persisted[_\s]?query[_\s]?not[_\s]?found", marker, re.IGNORECASE):
                return True
    return False


def getTrackInfoWithRetry(trackId: str, max_retries: int = TRACK_FETCH_MAX_RETRIES):
    """Fetch track info from spotapi with retry logic for transient failures.

    Returns the validated trackUnion dict from
    spotapi.Public.song_info()["data"]["trackUnion"].

    Raises IncompleteTrackInfoError if Spotify kept answering without a usable
    track, or the last transport error if all retries fail."""
    # Two failure modes, two separate budgets. An incomplete response early in a
    # fetch must not eat the transient ladder's attempts, or a Spotify blip would
    # silently shorten the recovery window for an unrelated rate limit.
    # Local import: Database.patches itself imports the Spotify package.
    from Database.patches import beginSpotapiHashAttempt, invalidateUsedSpotapiHash
    hashRefreshUsed = False
    incompleteAttempts = 0
    attempt = 0
    while attempt < max_retries:
        beginSpotapiHashAttempt()
        try:
            # Waits out a whole penalty window rather than the short polling
            # timeout the loops use - see SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS
            # for why giving up here is the expensive option.
            if not SPOTIFY_LIMITER.acquire(timeout=SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS):
                raise SpotifyLocallyRateLimitedError(
                    f"Spotify rate limit backoff in progress - skipped {ENDPOINT_TRACK_INFO} for {trackId}")
            payload = spotapi.Public.song_info(trackId)
            if _isPersistedQueryError(payload):
                # Use the same typed branch as a 4xx reported by spotapi.
                raise PersistedQueryError("Persisted query rejected")
            return extractTrackUnion(payload, trackId)
        except IncompleteTrackInfoError as e:
            if incompleteAttempts >= INCOMPLETE_TRACK_INFO_RETRIES:
                raise
            incompleteAttempts += 1
            logger.debug(
                "Incomplete track info for %s (attempt %d/%d), retrying in %ds: %s",
                trackId, incompleteAttempts, INCOMPLETE_TRACK_INFO_RETRIES + 1,
                INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS, e,
            )
            time.sleep(INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS)
            continue  #< deliberately does not advance `attempt`
        except Exception as e:
            if _isPersistedQueryError(e):
                if hashRefreshUsed:
                    raise
                hashRefreshUsed = True
                invalidateUsedSpotapiHash()
                # A dedicated retry, including on the last ordinary attempt.
                continue
            error_str = str(e).lower()
            # Our own limiter refusing a slot: nothing was sent, so this is the
            # most transient failure there is - matched by type rather than by
            # message, like SongError below. Tested FIRST, and excluded from
            # is_rate_limit below, because its message says "rate limit" too:
            # counting it as Spotify's would re-arm the very window that
            # refused this call (see SpotifyLocallyRateLimitedError).
            is_locally_paused = isinstance(e, SpotifyLocallyRateLimitedError)
            is_rate_limit = not is_locally_paused and (
                "429" in error_str or ("rate" in error_str and "limit" in error_str))
            # A permanently dead transport is not transient: curl_cffi's
            # "Session is closed, cannot send request." also says "session",
            # so the broad match below bought that error three sleeps before
            # raising anyway - while the reconnect paths (recentlyPlayed's
            # _isSessionClosedError) already treat it as unrecoverable. Same
            # helper, same answer, on the first attempt.
            if _isSessionClosedError(e):
                raise
            is_session_error = "session" in error_str   #< subsumes the old "could not get session" first clause
            # spotapi raises SongError from exactly one place in
            # Song.get_track_info: `if resp.fail`, i.e. the HTTP request itself
            # failed. That is a transport blip - the class this ladder exists
            # for - but it says neither "rate limit" nor "session", so the
            # substring tests above missed it and it was re-raised on the FIRST
            # attempt. That propagates through the poll loop's catch-all and
            # drops the whole iteration, losing a play that really happened (5
            # of the 11 such losses in 11 days of app.log). Matched by type,
            # not by message: the message is spotapi's to change.
            is_failed_request = isinstance(e, spotapi.exceptions.SongError)

            # Only retry on transient errors (rate limit, session issues, a
            # failed request), not on real 404s
            if not (is_rate_limit or is_session_error or is_failed_request or is_locally_paused):
                raise

            if is_rate_limit:
                # Spotify said so explicitly: hold the whole process, not just
                # this call's private 1/2s ladder.
                SPOTIFY_LIMITER.applyBackoff(SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS,
                                             reason=ENDPOINT_TRACK_INFO)

            if attempt < max_retries - 1:
                backoff_secs = 2 ** attempt  # 1, 2 seconds
                logger.warning("Track fetch failed (attempt %d/%d), backing off %ds: %s", attempt + 1, max_retries, backoff_secs, e)
                time.sleep(backoff_secs)
                attempt += 1
            else:
                logger.warning("Track fetch failed after %d attempts: %s", max_retries, e)
                raise


@contextmanager
def _pooledPublicClient():
    """A TLSClient on loan from spotapi's locked pool, for one public
    (unauthenticated) lookup - the same mechanism spotapi.Public already uses
    for the track path, so album/artist lookups behave like song_info does.

    Why not the constructor defaults: spotapi.PublicAlbum/Artist default
    `client` to a single import-time TLSClient, and BaseClient.__init__
    re-points that shared client's authenticate/on_auth_failure callbacks at
    itself - so two concurrent lookups authenticate through whichever
    BaseClient was constructed last. These callers really are concurrent: the
    metadata backfiller loops per user on its own thread while media_fetch
    resolves artist images in a thread pool. The pool hands concurrent
    borrowers DISTINCT clients, which is all the isolation that race needs.

    Why not a fresh TLSClient per call (what this replaced): both
    TLSClient.__init__ and BaseClient.__init__ atexit.register a close that
    nothing unregisters, so every construction pinned one live curl session
    for the life of the process - measured at 30 lookups -> 30 sessions still
    alive after gc. Borrowing bounds live clients at peak concurrency.
    (BaseClient still appends one atexit entry per construction, but against
    a pooled client they are cheap bound methods over the same few objects -
    the pre-existing cost the track path has always paid, not a leak.)"""
    client = client_pool.get()
    try:
        yield client
    finally:
        client_pool.put(client)


def _closeTlsClient(client) -> None:
    """Close a TLSClient now and drop its atexit registrations. Never raises.

    Both TLSClient.__init__ and every BaseClient.__init__ atexit.register a
    close over the client that nothing unregisters, so an unclosed client
    stays pinned - object graph AND live curl session - until process exit
    (measured at 30 lookups -> 30 live sessions, see _pooledPublicClient).
    The registrations are equal bound methods, so one unregister drops them
    all; closing is idempotent, so an atexit sweep that still finds one is
    harmless."""
    try:
        atexit.unregister(client.close)
    except Exception:  # noqa: S110 - unregistering is an optimization; closing is the point
        pass
    try:
        client.close()
    except Exception as e:
        logger.debug("Closing a Spotify TLS session failed: %s", e)


def normalizeSpotifyId(value) -> str:
    """A bare entity id out of any form the app passes: spotify:<kind>:<id>
    URIs (the importer, straight from export files), open.spotify.com URLs, or
    an already-bare id.

    The wrapper this replaces mangled the URI case - its isUrl() matched
    "spotify:" but its urlToId() only split on "/", so the untouched URI went
    to pathfinder as spotify:track:spotify:track:<id> and every URI lookup
    silently fell back to a name/artist search."""
    value = str(value or "")
    if value.startswith("spotify:"):
        return value.rsplit(":", 1)[-1]
    if "open.spotify.com/" in value:
        return value.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return value


class Spotify:
    """A cookie-authenticated Spotify session for one user.

    Class and method names keep spotipy's vocabulary (current_user,
    current_user_recently_played, ...) because every call site and its tests
    already speak it - this is a drop-in for the wrapper, not a new API."""

    def __init__(self, cookiesFile=None, email=None):
        self.email = email
        # False is the not-logged-in sentinel the listener's failure handling
        # reads (isinstance bool check); a successful login replaces it with
        # the spotapi Login object.
        self.user_auth = False
        self.lastPlayedManager = None
        self.recentlyPlayed = deque(maxlen=RECENTLY_PLAYED_BUFFER_SIZE)
        if cookiesFile is not None:
            self.login(cookiesFile)

    # -- session ------------------------------------------------------------

    def login(self, cookiesFile=None) -> bool:
        """Authenticate from a saved-sessions file. Returns False - never
        raises - on any failure, leaving user_auth a bool: the listener treats
        that sentinel as "stored cookies are invalid" and degrades instead of
        crashing startup (see spotifyListener's loginFailed)."""
        if cookiesFile is None:
            return False
        identifier = None
        freshClient = None
        try:
            # spotapi.Config's `client` field defaults via `field(default=TLSClient(...))`
            # rather than `field(default_factory=...)` - dataclasses only reject known
            # mutable defaults (list/dict/set), so that TLSClient instance is built once
            # at import time and silently shared as the default for every Config() call
            # that doesn't pass client= explicitly. Since Login stores cookies directly
            # on cfg.client (a curl_cffi Session), every user's Login object was sharing
            # one process-wide cookie jar - concurrent logins/reconnects would clobber
            # each other's session cookies, causing current_user() to return whichever
            # user's cookies happened to be in the jar at request time (the cross-user
            # contamination bug). A fresh TLSClient per login isolates each user's
            # cookies.
            freshClient = spotapi.TLSClient(TLS_CLIENT_PROFILE, "", auto_retries=TLS_CLIENT_AUTO_RETRIES)
            cfg = spotapi.Config(
                logger=spotapi.Logger(),
                client=freshClient,
            )
            saver = spotapi.saver.JSONSaver(cookiesFile)
            try:
                with open(cookiesFile, "r") as f:
                    sessions = json.load(f)

                if self.email:
                    for session in sessions:
                        if session.get("identifier") == self.email:
                            identifier = session["identifier"]
                            break
                if not identifier and sessions:
                    identifier = sessions[0]["identifier"]
            except Exception as e:
                logger.error("Error loading cookies file: %s", e)
                _closeTlsClient(freshClient)
                return False

            self.user_auth = spotapi.Login.from_saver(saver, cfg, identifier)
        except Exception as e:
            logger.error("Failed to login user %s: %s", identifier or "unknown", e)
            # A failed login strands the client just built above: user_auth is
            # still False, so close() will never find it - release it here.
            if freshClient is not None:
                _closeTlsClient(freshClient)
            return False
        return True

    def close(self) -> None:
        """Release the login's TLS session. Idempotent, never raises.

        Every login builds a fresh TLSClient (the contamination fix above), so
        every retired session - a listener rebuild, a finished import - left
        one behind, atexit-pinned with its curl session open until process
        exit. Whoever retires the session (Listener.stop(), the import
        service) calls this."""
        login = self.user_auth
        if isinstance(login, bool):
            return
        client = getattr(login, "client", None)
        if client is not None:
            _closeTlsClient(client)

    def isLoggedIn(self) -> bool:
        return not isinstance(self.user_auth, bool)

    # -- catalog lookups ----------------------------------------------------

    def track(self, trackId, *args, **kwargs) -> dict:
        """Track metadata via spotapi.Public's locked client pool - NOT via
        spotapi.Song(), whose `client` argument defaults to one process-wide
        shared TLSClient (same import-time-default footgun as Config above):
        every Song() re-points that shared client's auth callbacks at itself,
        so concurrent track() calls could get authenticated with another
        thread's auth state - intermittent wrong/failed lookups under the
        importer's metadata pre-fetch."""
        trackId = normalizeSpotifyId(trackId)
        try:
            raw = getTrackInfoWithRetry(trackId)
        except IncompleteTrackInfoError as e:
            # Raising here propagates through the recently-played callback into
            # the poll loop's catch-all, which drops the whole iteration - so a
            # play that genuinely happened is lost because Spotify wouldn't
            # describe the track (11 times over 11 days in app.log). A marked
            # fallback keeps the play; the metadata is repaired the next time
            # the same id is looked up successfully, since upsertTrack lets real
            # metadata replace a fallback row and its marker.
            logger.warning("No usable track info for %s, recording a fallback record: %s", trackId, e)
            return fallbackTrackRecord(trackId)
        return formatTrackUnion(raw)

    def album(self, albumId, *args, **kwargs) -> dict:
        """Album metadata, including up to the first page of its track list
        (get_album_info's default 25 - same ceiling as the wrapper this
        replaces; the backfiller uses those tracks as a duration source)."""
        albumId = normalizeSpotifyId(albumId)
        with _pooledPublicClient() as client:
            payload = spotapi.PublicAlbum(albumId, client=client).get_album_info()
        return formatAlbumUnion(((payload or {}).get("data") or {}).get("albumUnion") or {})

    def artist(self, artistId, *args, **kwargs) -> dict:
        """Artist profile + avatar images - media_fetch's lazy artist-image
        fallback reads images[0].url."""
        artistId = normalizeSpotifyId(artistId)
        with _pooledPublicClient() as client:
            payload = spotapi.Artist(client=client).get_artist(artistId)
        return formatArtistUnion(((payload or {}).get("data") or {}).get("artistUnion") or {})

    def playlist(self, playlistId, *args, **kwargs) -> dict:
        playlistId = normalizeSpotifyId(playlistId)
        with _pooledPublicClient() as client:
            payload = spotapi.PublicPlaylist(playlistId, client=client).get_playlist_info()
        return formatPlaylistV2(((payload or {}).get("data") or {}).get("playlistV2") or {})

    def search(self, query, type="track", limit=SEARCH_DEFAULT_LIMIT, *args, **kwargs) -> dict:
        """First page of track results, spotipy-shaped. Only the importer's
        by-name fallback calls this, and it reads ["tracks"]["items"][0] with
        limit=1 - pagination would be dead code.

        query_songs directly, NOT Public.song_search: the pagination wrapper
        hardcodes a 100-result page, so the limit argument was silently
        dropped and every by-name fallback downloaded and formatted 100x what
        it used. Song's `client` default is the same shared import-time
        TLSClient as PublicAlbum's, hence the pooled borrow."""
        with _pooledPublicClient() as client:
            payload = spotapi.Song(client=client).query_songs(query, limit=limit)

        results = ((((payload or {}).get("data") or {}).get("searchV2") or {})
                   .get("tracksV2") or {}).get("items") or []
        items = []
        for result in results:
            data = ((result or {}).get("item") or {}).get("data") or {}
            if data.get("__typename") != "Track":
                continue  #< search surfaces albums/artists/playlists too
            items.append(formatSearchTrackData(data))
        return {"tracks": {"items": items}}

    # -- account ------------------------------------------------------------

    def current_user(self) -> dict:
        """{id, email} for the contamination check. The get_user_info call goes
        through spotapi.user.User, which Database.patches wraps with the shared
        limiter and HTML-response diagnostics - calling through the class keeps
        that seam."""
        return formatProfile(spotapi.user.User(self.user_auth).get_user_info())

    # -- recently played ----------------------------------------------------

    def _addToRecentlyPlayed(self, trackUri, playedAt, contextUri, timePlayed):
        """The RecentlyPlayedManager's play-finished callback: resolve metadata
        and append to the local buffer the listener drains.

        Catalog failures must not hide the next observed track or inflate this
        play's duration during retries. Keep the event with placeholder metadata;
        a later successful lookup can repair it through upsertTrack.
        """
        try:
            track = self.track(trackUri)
        except Exception as error:  # noqa: BLE001 - preserve the observed play when metadata is unavailable
            logger.warning("Could not describe finished track %s; recording fallback metadata: %s", trackUri, error)
            track = fallbackTrackRecord(normalizeSpotifyId(trackUri))
        self.recentlyPlayed.append({
            "track": track,
            "played_at": playedAt,
            "ms_played": timePlayed,
            "context": formatContext(contextUri),
        })

    def startRecentlyPlayedListener(self, refreshInterval=3, logUser=None):
        if not self.isLoggedIn():
            # The wrapper built its manager around user_auth==False here and
            # died with an AttributeError deep inside spotapi; failing at the
            # call site keeps the error where the cause is.
            raise ValueError("Cannot start the recently-played listener without a logged-in session")
        if self.lastPlayedManager is None:
            self.lastPlayedManager = RecentlyPlayedManager(self.user_auth)
        self.lastPlayedManager.start(self._addToRecentlyPlayed, refreshInterval, logUser=logUser)

    def current_user_recently_played(self, limit=RECENTLY_PLAYED_BUFFER_SIZE, after=None, before=None) -> list:
        """The LOCAL buffer of plays this session observed, oldest first - NOT
        a Spotify call. The listener's Z1 dedup and the worker's item loop are
        built on exactly that: a monotonically-appended in-process buffer that
        costs nothing to read. (The Web API's recently-played endpoint is a
        different, OAuth-only path - see _fetchRecentlyPlayedFromWebApi in the
        listener.)"""
        return list(self.recentlyPlayed)
