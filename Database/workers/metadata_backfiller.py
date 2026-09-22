# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Imported for the type hints below. Everything used at RUNTIME still goes
# through _dbmod, so the suite's patch("Database.database.X") targets are
# unaffected - these names were simply never defined here, leaving the hints
# unresolvable for tooling and for typing.get_type_hints(). All leaf modules
# (stdlib, or Database/lastfm.py, which imports nothing of ours), so a real
# import costs nothing and cannot cycle.
import threading

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod
from Database.db import SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
#< a direct import, unlike _dbmod above: Database.utils imports nothing but the
#  standard library, so it cannot take part in the cycle _dbmod exists to break
from Database.rate_limit import retryAfterSeconds
from Database.utils import flaskDebugEnabled


# How many albums a cycle works through, on the Web API and on the cookie-client
# fallback alike, so both paths pace identically.
#
# These two were SPOTIFY_BULK_ALBUM_LIMIT and SPOTIFY_BULK_TRACK_LIMIT, named
# for the `?ids=` endpoints' hard caps on ids per request. Those endpoints are
# gone (see the note above the pacing constants below), so the numbers no longer
# describe an API limit at all - they are purely our own per-cycle pacing, and
# the old names would have had a reader looking for a bulk request that is not
# there. The values are unchanged: the drain rate was always ids-per-cycle.
ALBUM_BATCH_SIZE = 20

# The same, for tracks. Higher than the album batch because it always was - a
# different endpoint, not because ISRCs are cheaper.
TRACK_BATCH_SIZE = 50

# How much of a failed response's body reaches the log. A Spotify error object
# is ~90 characters; this is room for several, and a bound for everything else.
ERROR_BODY_LOG_LIMIT = 500

# Spotify withdrew the bulk `?ids=` catalog endpoints on 2026-07-31: they answer
# 403 Forbidden for a single id as readily as for fifty, while /v1/<kind>/<id>
# answers 200 (measured against this instance's own credentials, with plain
# requests, so it is Spotify's behaviour and not something here). A batch is
# therefore N requests rather than one.
#
# It costs no wall-clock: pacing was never request-bound. A cycle has always
# been TRACK_BATCH_SIZE ids per worker, so the whole catalog still
# drains in the same ~14 hours across three workers - it is the same 50 tracks,
# asked for 50 times instead of once.
SPOTIFY_REQUEST_PACE_SECONDS = 0.2   #< between per-id requests; ~10s of a 300s cycle

# Per catalog request. Long enough that a slow-but-alive API still answers,
# short enough that a black-holed connection cannot hold a batch of fifty past
# the cycle it belongs to.
SPOTIFY_REQUEST_TIMEOUT_SECONDS = 10

# Give up on a batch after this many failures IN A ROW. With one request per
# batch a systemic failure cost one request per cycle; with fifty it costs
# fifty, from each of three workers, every five minutes, forever - which is
# exactly the shape of the 403 this rewrite was done under. Consecutive rather
# than cumulative: an intermittent 503 among successes is the API being busy,
# not the API being gone.
CONSECUTIVE_FAILURE_ABORT = 5

# Spotify's "you are asking too often". Distinguished from the other failures
# because it is the one that says stopping now helps.
SPOTIFY_RATE_LIMIT_STATUS = 429

# How many EXTRA pages of one album's track list the repair pass will follow.
#
# GET /v1/albums/<id> embeds only the first page (50 items), so an album's
# later tracks were never seen - and an artistless one among them made
# getAlbumsWithArtistlessTracks re-select that album every retry window
# forever, repairing the same first page each time. These pages spend the same
# per-app catalog quota as the album lookups themselves (the window the ISRC
# step shares), so the walk is bounded rather than trusting the server's own
# `next` to end: 4 extra pages reaches 250 tracks, past any real album -
# a 100-disc box set is one album per disc on Spotify, not one of 2000 tracks.
ALBUM_TRACK_PAGE_MAX = 4

# How long to stand the Web-API catalog lookups down when Spotify answers 429
# without saying for how long. One stand-down for BOTH catalog steps - the ISRC
# batch and the album batch spend the same per-app quota, so a refusal on
# either is a refusal for both (see _standDownCatalogLookups).
#
# Measured, not chosen: the live instance drew QUOTA_EXCEEDED at 16:24 on
# 2026-08-08 and was still drawing it 15.5 hours and 557 requests later, because
# breaking the BATCH left the next cycle free to walk back into the wall five
# minutes on. A quota window is not a burst limit that clears in seconds, and
# retrying inside one is part of what keeps you in it.
#
# An hour is the compromise: short enough that a window which has quietly
# reopened is noticed the same day, long enough that a closed one costs 24
# requests rather than 288.
CATALOG_QUOTA_BACKOFF_SECONDS = 3600
# A malformed or absurd Retry-After must not park the queue for a week.
CATALOG_QUOTA_BACKOFF_MAX_SECONDS = 24 * 3600


def _retryAfterSeconds(resp) -> float:
    """How long Spotify asked us to wait, or the default stand-down.

    This module's bounds on the shared parser (Database/rate_limit.py), which
    is where the two RFC 9110 forms are read. The copy that used to live here
    understood only delta-seconds, so an HTTP-date fell through to the hour -
    safe, and therefore invisible."""
    return retryAfterSeconds(resp,
                             default=CATALOG_QUOTA_BACKOFF_SECONDS,
                             maximum=CATALOG_QUOTA_BACKOFF_MAX_SECONDS)


def _responseDetail(resp) -> str:
    """`status <code>: <body>` for a log line - on one line, and bounded.

    The status alone is not a diagnosis, and 403 is the case that proves it.
    Measured on the live instance 2026-08-08: the withdrawn bulk endpoints
    answered `{"error": {"status": 403, "message": "Forbidden"}}` - a bare
    refusal, with a valid token whose refresh succeeded and whose scopes were
    intact. The listener's recently-played endpoint answers the SAME status with
    "Insufficient client scope", and that one really does mean re-authorize.
    Same number, opposite remedies, and only the body tells them apart.

    (Do not read the scope message as expected here: these endpoints take no
    scope at all. It is named only so the contrast is on the record.)

    Collapsed onto one line because Spotify pretty-prints its error objects over
    five, and a log this size is read through grep, where a five-line entry
    means the message is invisible unless you already knew to ask for context.
    Bounded because a proxy or a captive portal in front of the API answers with
    a page rather than an object, and a log line is not the place to find that
    out. str() rather than a bare read: a response is not always a real one."""
    body = " ".join(str(getattr(resp, "text", "") or "").split())
    if len(body) > ERROR_BODY_LOG_LIMIT:
        body = body[:ERROR_BODY_LOG_LIMIT] + "..."
    return f"status {resp.status_code}: {body}" if body else f"status {resp.status_code}"


class _CycleAccessToken:
    """The one Web-API access token a backfill cycle uses, minted on first ask.

    Both Web-API steps in a cycle want a token, and each used to mint its own -
    so a cycle spent two round-trips on Spotify's token endpoint to be told the
    same thing twice. Minting one eagerly at the top of the cycle fixes that but
    buys a worse problem: a round-trip on every IDLE cycle, and idle is the
    steady state here. The album queue drains for good, and the ISRC queue only
    refills every TRACK_ISRC_RETRY_SECONDS, so a settled instance would spend a
    request every five minutes forever to produce a token neither step reads.

    Lazy keeps both properties at once: at most one refresh per cycle, and none
    at all when there is nothing to spend it on. Callers ask by CALLING it, so a
    plain `lambda: token` substitutes for it anywhere a real one is awkward.

    None and REFRESH_TOKEN_REVOKED are cached unchanged too: a failed refresh
    must not be retried within a cycle, and the health observer needs the
    explicit revocation verdict even though it is not a usable bearer token."""

    __slots__ = ("_mint", "_token", "_minted", "noted")

    def __init__(self, mint):
        self._mint = mint
        self._token = None
        self._minted = False
        #< set by _noteTokenHealth, so a cycle is counted once whichever of the
        #  two steps happened to ask for the token first
        self.noted = False

    def __call__(self) -> str | object | None:
        if not self._minted:
            self._minted = True
            self._token = self._mint()
        return self._token

    @property
    def attempted(self) -> bool:
        """Whether anything in this cycle asked for a token at all. A cycle with
        no work asks for none, and that is not evidence either way."""
        return self._minted


class _CatalogBatch:
    """What one pass over a batch of catalog ids came back with.

    `attempted` are the ids that got a DEFINITIVE answer (a 200, or a 404 -
    Spotify has no such entity and never will) and so may leave the queue.
    Anything else stays queued: a 429 or a 5xx stamped as attempted would park
    the id for the whole retry window over a transient.

    `failures` is COUNTED rather than derived from len(ids) - len(attempted),
    because the batch can stop early: the ids after a break were never ASKED
    about, and calling them failures reports "50 of 50 failed" over five real
    ones, in the line that exists to be believed. `firstFailure` is the first
    one's detail, for the same line."""

    __slots__ = ("attempted", "failures", "firstFailure")

    def __init__(self, attempted, failures, firstFailure):
        self.attempted = attempted
        self.failures = failures
        self.firstFailure = firstFailure


class MetadataBackfillMixin:
    """The Spotify Web-API metadata backfiller (missing album/track dates, artistless tracks)."""

    # Immutable default until this instance first hits its catalog quota.
    # _standDownCatalogLookups assigns self._catalogBackoffUntil, shadowing
    # this default for that user only; other instances keep their own deadline.
    # Do not reset it when restarting a worker on the same Database instance:
    # the existing quota window still applies. A new process starts at zero.
    _catalogBackoffUntil = 0.0

    def _spendCatalogBatch(self, kind: str, ids: list, headers: dict,
                            stop_event: threading.Event, onOk) -> _CatalogBatch:
        """Ask Spotify about `ids`, one GET /v1/<kind>/<id> each, and apply one
        failure policy to the answers.

        Both catalog steps run this: the ISRC step over tracks and the album
        metadata step over albums. They spend the same per-app quota and want
        the same behaviour under refusal, and they used to say so twice - ~35
        lines of identical control flow either side of a different URL and a
        different thing done with a 200's body. The album copy was the one
        without tests for the abort or the batch-stop, which is what a
        duplicated policy costs: a fix lands on one of them.

        `onOk(entityId, resp)` handles a 200's body and is called INSIDE the
        per-id try, so a 200 whose JSON is malformed is a failure of that id
        and not of the batch.

        The policy, in full:
          * one id at a time, because the bulk `?ids=` endpoints were withdrawn
            (see SPOTIFY_REQUEST_PACE_SECONDS' note);
          * the stop event is checked BETWEEN ids, so shutdown lands inside a
            fifty-request batch rather than waiting it out;
          * an exception is caught per id, so one reset connection does not
            cost the rest of the batch its turn;
          * a 429 arms the shared stand-down (one per-app budget, one wall) and
            ends the batch - the quota window outlasts the cycle;
          * CONSECUTIVE_FAILURE_ABORT failures in a row end it too. Consecutive
            rather than cumulative: an intermittent 503 among successes is the
            API being busy, not a systemic refusal."""
        import requests

        attempted = []
        failures = 0
        firstFailure = None
        consecutiveFailures = 0

        for entityId in ids:
            if stop_event.is_set() or _dbmod.time.time() < self._catalogBackoffUntil:
                break

            rateLimited = False
            try:
                resp = requests.get(f"https://api.spotify.com/v1/{kind}/{entityId}",
                                    headers=headers, timeout=SPOTIFY_REQUEST_TIMEOUT_SECONDS)
                if resp.status_code == 200:
                    onOk(entityId, resp)
                    attempted.append(entityId)
                    consecutiveFailures = 0
                elif resp.status_code == 404:
                    attempted.append(entityId)
                    consecutiveFailures = 0
                else:
                    if firstFailure is None:
                        firstFailure = _responseDetail(resp)
                    failures += 1
                    consecutiveFailures += 1
                    rateLimited = resp.status_code == SPOTIFY_RATE_LIMIT_STATUS
                    if rateLimited:
                        self._standDownCatalogLookups(resp)
            except Exception as e:
                if firstFailure is None:
                    firstFailure = f"request failed: {e}"
                failures += 1
                consecutiveFailures += 1

            if rateLimited or consecutiveFailures >= CONSECUTIVE_FAILURE_ABORT:
                break
            stop_event.wait(SPOTIFY_REQUEST_PACE_SECONDS)

        return _CatalogBatch(attempted, failures, firstFailure)

    def _walkRemainingAlbumTracks(self, albumRaw, headers, stop_event) -> bool:
        """Extend `albumRaw["tracks"]["items"]` with the pages beyond the
        first, in place. Returns whether the list is now COMPLETE.

        Only the completeness answer may drive markAlbumsArtistRepairDone: a
        walk that stopped early has not established that Spotify credits
        nobody on the tracks it never saw, and stamping it would silence a
        repairable album for ALBUM_ARTIST_REPAIR_RETRY_SECONDS.

        Failures are swallowed rather than raised, deliberately. This runs
        inside _spendCatalogBatch's per-id try, where raising would discard
        the FIRST page too and mark the whole album a failure - so a blip on
        page 2 would cost the repairs page 1 had already earned. Keeping them
        and reporting "not complete" leaves the album queued for a later pass,
        which is exactly the outcome that wants. A 429 still arms the shared
        stand-down on its way out, because that is about the whole app's
        quota, not about this album."""
        import requests

        tracks = albumRaw.get("tracks")
        if not isinstance(tracks, dict) or not isinstance(tracks.get("items"), list):
            return False   #< no track list at all: nothing walked, nothing to claim
        for _ in range(ALBUM_TRACK_PAGE_MAX):
            nextUrl = tracks.get("next")
            if not nextUrl:
                return True
            if stop_event.is_set():
                return False
            stop_event.wait(SPOTIFY_REQUEST_PACE_SECONDS)   #< paced like every other catalog call
            try:
                resp = requests.get(nextUrl, headers=headers,
                                    timeout=SPOTIFY_REQUEST_TIMEOUT_SECONDS)
                if resp.status_code == SPOTIFY_RATE_LIMIT_STATUS:
                    self._standDownCatalogLookups(resp)
                    return False
                if resp.status_code != 200:
                    _dbmod.logger.warning(
                        "[Backfiller-%s] Album track page returned %s; keeping the pages already read",
                        self.user, resp.status_code)
                    return False
                page = resp.json()
            except Exception as e:
                _dbmod.logger.warning("[Backfiller-%s] Album track page failed: %s",
                                      self.user, _dbmod.parseError(e))
                return False
            tracks["items"].extend(page.get("items") or [])
            tracks["next"] = page.get("next")
        return not tracks.get("next")   #< ran out of allowance, not out of pages

    def getSpotifyApiWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the Spotify API metadata
        backfiller worker thread."""
        return self._workerStatus(
            "backfiller_thread", "spotify_api",
            configured=bool(self.repo.getUserSpotifyCredentials(self.user)))

    def startMetadataBackfiller(self) -> None:
        """Start the background thread to fill in missing album metadata."""
        self._startPeriodicWorker("backfiller_thread", "backfiller_stop_event",
                                   self._metadataBackfillLoop,
                                   f"metadata-backfiller-{self.user}", logPrefix="MetadataBackfiller")

    def stopMetadataBackfiller(self) -> None:
        """Signal and wait for the background backfiller thread to stop."""
        self._stopPeriodicWorker("backfiller_thread", "backfiller_stop_event")

    @staticmethod
    def _normalizeBackfillArtists(artistsRaw: list) -> list[dict]:
        """Repo-shaped artist dicts from an album payload's per-track artist
        list (Web API and the cookie client both expose id/name/external_urls).
        Entries without a real id or name are dropped - fabricating links
        would be worse than leaving the track for the next repair pass."""
        artists = []
        for artist in artistsRaw:
            if not isinstance(artist, dict):
                continue
            artistId = artist.get("id")
            name = artist.get("name")
            if not artistId or not name:
                continue
            url = (artist.get("external_urls") or {}).get("spotify") or \
                f"https://open.spotify.com/artist/{artistId}"
            artists.append({"id": artistId, "name": name, "url": url, "imageId": artistId})
        return artists

    def _standDownCatalogLookups(self, resp) -> None:
        """Arm this user's catalog stand-down from a 429's Retry-After.

        One wall for both Web-API steps, because Spotify meters one budget: the
        ISRC lookups and the album lookups spend the same per-app quota, so a
        refusal met on either is a refusal for both. What each does behind the
        wall differs - the ISRC step waits it out (it has no other source),
        while the album step goes straight to the cookie client, which answers
        through a different door and spends no Web-API quota at all.

        Logged unconditionally, unlike the album batch's debug-gated failure
        line: arming is a state change that can silence both steps for a day
        (Retry-After runs to ~23h on the live instance), never colour on a
        story the log already tells."""
        standDown = _retryAfterSeconds(resp)
        self._catalogBackoffUntil = _dbmod.time.time() + standDown
        _dbmod.logger.warning(
            "[Backfiller-%s] Spotify refused on quota; standing Web-API catalog "
            "lookups down for %.0f minutes", self.user, standDown / 60)

    def _noteTokenHealth(self, getAccessToken, hasWebApiCreds: bool) -> None:
        """Ask for reauthorization only on an explicit revoked-grant verdict.

        None means unavailable/transient, including rate limits and network
        failures. Repeating it does not turn it into evidence of revocation.

        Deliberately NOT wired to a 403 from the lookups themselves, however
        many times it repeats. That was tested for us on 2026-07-31, when
        Spotify withdrew the bulk `?ids=` catalog endpoints and every call began
        answering 403 Forbidden - with a refresh that succeeded, scopes intact,
        and /v1/me answering 200. Prompting on that would have told all three
        accounts their Spotify authorization had failed, over a platform change
        no login could fix; one of them re-consented anyway and it changed
        nothing. A 403 says "not allowed", not "not you". The 403 that DOES mean
        re-authorization carries "Insufficient client scope" in its body and
        arrives on a SCOPED endpoint - the listener's recently-played poll owns
        that one (see _SCOPE_ERROR), and nothing here asks for a scope at all.

        A cycle that asked for no token is not evidence either way, so it
        neither counts nor clears.

        Sets but never clears. Clearing belongs to a definitive scoped success
        (the listener's on_scope_status_change) or to the user finishing the
        re-auth flow: a token refresh working again says nothing about the scope
        the flag may have been raised for."""
        if getAccessToken.noted or not (hasWebApiCreds and getAccessToken.attempted):
            return
        getAccessToken.noted = True

        from Database.Listeners.spotifyListener import REFRESH_TOKEN_REVOKED
        token = getAccessToken()
        if token is REFRESH_TOKEN_REVOKED:
            # The shared setter queues only on an atomic false-to-true flag
            # transition, even if another worker observed the same revocation.
            self.setSpotifyNeedsReauth(True)

    def _repairFallbackTrackMetadata(self, rawTracks: list[dict], source: str = "catalog") -> int:
        """Reuse full catalog/history responses without fetching or recording plays.

        Incomplete responses must leave the marker intact so the existing
        catalog queue can retry. Artwork URLs are stored by upsertTrack; no
        image download or other network work belongs in this repair.
        """
        tracks = []
        for raw in rawTracks:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            if raw.get("created_reason") in (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON):
                continue
            try:
                album = raw.get("album") or {}
                artists = raw.get("artists") or album.get("artists") or []
                if (not (raw.get("name") or "").strip()
                        or (raw.get("duration_ms") or 0) <= 0
                        or not album.get("id") or not (album.get("name") or "").strip()
                        or not artists
                        or any(not artist.get("id") or not (artist.get("name") or "").strip()
                               for artist in artists)
                        or not (raw.get("external_urls") or {}).get("spotify")):
                    continue
                tracks.append(_dbmod.Client.formatTrack(raw, embedPlaybackInfo=False))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                _dbmod.logger.warning("Cannot repair fallback metadata for track %s: %s",
                                      raw["id"], _dbmod.parseError(error))
        result = self.repo.repairFallbackTracks(tracks)
        self._logCommittedMetadataRepair(source, result)
        return result.repaired if result is not None else 0

    def _backfillTrackIsrcs(self, getAccessToken, stop_event: threading.Event) -> None:
        """Fill ISRCs and repair fallback tracks from one catalog batch per cycle.

        Web-API only, with no cookie-client fallback - and that is a property of
        the data, not an omission. The pathfinder client cannot expose ISRCs at
        all (Database/Spotify/formatting.py), so an instance whose users have no
        Web-API credentials simply has no ISRC source; attempting the fallback
        would burn a request per track to learn nothing. Nothing is stamped in
        that case either, so the queue is intact the moment credentials arrive.
        `getAccessToken()` answers None exactly then - and also when a refresh
        failed, which wants the same treatment for the same reason.

        Asked for AFTER the queue read and the claim below, and asked for at all
        only when there is a batch to spend it on - see _CycleAccessToken for
        why a cycle with nothing to do must make no network calls.

        The ISRC is what makes "the single and the album track are the same
        recording" answerable without guessing at names: both releases carry the
        issuer's identifier for the master. (A remaster is a new master and gets
        its own ISRC, so this is a precise key, not a fuzzy one.)

        Nothing raises out of here, and that covers the repo calls as much as
        the request: a locked database is the ordinary way one of them fails on
        a live instance. This step runs FIRST in the cycle (see the loop, which
        explains why), so anything escaping is caught by the per-cycle handler
        and costs the album and artist-link repair their whole turn - a DNS blip
        or a busy database stalling unrelated work for five minutes."""
        #< the shutdown check first: a worker being torn down should stop
        #  whether or not this instance has credentials, and it is the one gate
        #  here that is about the process rather than about the data
        if stop_event.is_set():
            return

        # Before the queue read and before the token: a quota wall that lasts
        # hours must not cost a refresh round-trip every five minutes while it
        # does. See CATALOG_QUOTA_BACKOFF_SECONDS for what standing down is
        # worth. The album step honours the same wall (its Web-API branch is
        # skipped for the cookie client), and either step's 429 can have armed
        # it - one per-app budget, one stand-down.
        #
        # Per USER, and that is correct rather than incidental: Spotify meters
        # per registered app, and each account here has its own. Measured when
        # all three hit the wall together on 2026-08-08 - Spotify answered them
        # 392, 431 and 430 minutes, and a shared budget answers one window to
        # everyone. They ran out at the same moment because they consume at the
        # same rate on the same five-minute schedule, not because they share
        # anything.
        #
        # So do NOT "optimise" this into a process-wide stand-down: it would
        # idle two accounts that still have quota. The one setup where sharing
        # WOULD be right is every account pointed at a single app - if that ever
        # happens here, key this on the client_id rather than on the instance.
        if _dbmod.time.time() < self._catalogBackoffUntil:
            return

        target_ids = []
        try:
            # The catalog is shared - one tracks table for the whole instance -
            # so every user's backfiller drains THIS queue, and two in flight at
            # once used to request the same 50 ids: three API calls to make one
            # batch of progress, and three writes of the same rows. Claimed
            # process-wide for the length of the request, exactly as the album
            # queue does it (Database._active_backfills, step 4 in the loop).
            #
            #< the pool is read four times wider than the batch, the ratio the
            #  album queue uses: reading exactly one batch would have the second
            #  worker find everything claimed and sit the cycle out, which is
            #  correct but throws away the parallelism three workers are for
            pool = self.repo.getTracksMissingIsrc(self.BACKFILLER_TRACK_QUEUE_SIZE)
            with _dbmod.Database._backfill_lock:
                for track_id in pool:
                    if track_id in _dbmod.Database._active_isrc_backfills:
                        continue
                    target_ids.append(track_id)
                    _dbmod.Database._active_isrc_backfills.add(track_id)
                    if len(target_ids) >= TRACK_BATCH_SIZE:
                        break
            if not target_ids:
                return
            access_token = getAccessToken()
            if not isinstance(access_token, str) or not access_token:
                return

            headers = {"Authorization": f"Bearer {access_token}"}
            isrcByTrackId = {}
            trackMetadata = []

            def _recordIsrc(track_id, resp):
                #< a track Spotify HAS but has no ISRC for is still ANSWERED,
                #  and _spendCatalogBatch counts it attempted either way -
                #  re-asking every cycle forever is what the bulk form's null
                #  entries meant. Only the value is conditional
                payload = resp.json()
                isrc = (payload.get("external_ids") or {}).get("isrc")
                if isrc:
                    isrcByTrackId[track_id] = isrc
                # Do not attach a relinked release's metadata to the requested
                # id. Existing ISRC handling remains independent of this repair.
                if payload.get("id") == track_id:
                    trackMetadata.append(payload)

            batch = self._spendCatalogBatch("tracks", target_ids, headers,
                                            stop_event, _recordIsrc)

            self.repo.updateTrackIsrcs(isrcByTrackId)
            self._repairFallbackTrackMetadata(trackMetadata, source="catalog")
            self.repo.markTracksIsrcAttempted(batch.attempted)

            if batch.firstFailure is not None:
                # One line for the batch, not one per track: fifty requests can
                # fail fifty times and the log is read by a person.
                #
                # Unconditional, unlike the album lookup's twin below: that one
                # falls back to the cookie client and says so, so its status is
                # colour on a story the log already tells. This step has no
                # fallback, which makes the status the ENTIRE story - and behind
                # a FLASK_DEBUG gate a persistent 401 was indistinguishable from
                # "still draining", the queue sitting untouched cycle after
                # cycle with nothing in the log to say why.
                _dbmod.logger.warning(
                    "[Backfiller-%s] ISRC lookup failed for %d of %d track(s); first was %s",
                    self.user, batch.failures, len(target_ids), batch.firstFailure)

            if isrcByTrackId:
                _dbmod.logger.info("[Backfiller-%s] Recorded ISRCs for %d/%d track(s)",
                                    self.user, len(isrcByTrackId), len(target_ids))
        except Exception as e:
            #< a timeout, a reset, a 200 that isn't JSON, a 200 whose JSON is a
            #  list, a locked database. Same verdict as a 429: nothing learned,
            #  so nothing stamped, and the ids stay queued
            _dbmod.logger.warning("[Backfiller-%s] ISRC backfill failed: %s", self.user, e)
        finally:
            # In a finally because the failure paths are the ones that need it:
            # a successful batch has stamped isrc_attempted_at and left the queue
            # anyway, while a failure stamps nothing, so ids held after one would
            # be skipped as "already in flight" by every later cycle in this
            # process - a backfill that quietly stops making progress on them.
            try:
                with _dbmod.Database._backfill_lock:
                    _dbmod.Database._active_isrc_backfills.difference_update(target_ids)
            except Exception as cleanupError:
                _dbmod.logger.warning("[Backfiller-%s] Failed to release %d in-flight track ids: %s",
                                       self.user, len(target_ids), cleanupError)

    def _runTrackMergeIfDue(self) -> None:
        """Run the ISRC matcher when the feature toggle and daily claim allow it."""
        # New ISRCs can complete a pair, so the matcher keeps running while the
        # toggle is on. Once a DAY, not once a cycle: a run that merges
        # something drops the cached Wrapped years its groups touch, and at
        # cycle cadence that was 147 full rebuilds a day on the live instance.
        if not self.repo.isTrackMergeEnabled():
            return

        # Read before the claim overwrites it. The claim makes the slot safe
        # against three workers, but it also stamps a run that has not happened
        # yet, so a matcher that raises must give the previous stamp back.
        previousStamp = self.repo.getTrackMergeLastRun()
        if not self.repo.claimTrackMergeRun():
            return

        try:
            mergeSummary = self.repo.mergeTracksByIsrc()
        except Exception:
            try:
                self.repo.releaseTrackMergeRun(previousStamp)
            except Exception:
                _dbmod.logger.warning(
                    "[Backfiller-%s] Could not release the ISRC merge slot; "
                    "the matcher waits out the interval", self.user,
                    exc_info=True)
            raise

        if mergeSummary["merged"]:
            _dbmod.logger.info(
                "[Backfiller-%s] ISRC merge: %d track(s) newly merged",
                self.user, mergeSummary["merged"])

    def _runAlbumMetadataBatch(self, getAccessToken, hasWebApiCreds: bool,
                               stop_event: threading.Event) -> bool:
        """Process one album metadata batch.

        Returns False when no album work was claimed; the caller owns the idle
        wait so extraction does not change loop cadence.
        """
        target_ids = []
        try:
            missing_ids = self.repo.getAlbumsMissingMetadata(limit=self.BACKFILLER_ALBUM_QUEUE_SIZE)
            if len(missing_ids) < self.BACKFILLER_ALBUM_QUEUE_SIZE:
                known_ids = set(missing_ids)
                missing_ids.extend(
                    albumId for albumId in self.repo.getAlbumsWithArtistlessTracks(
                        self.BACKFILLER_ALBUM_QUEUE_SIZE - len(missing_ids))
                    if albumId not in known_ids)
            if not missing_ids:
                return False

            with _dbmod.Database._backfill_lock:
                for album_id in missing_ids:
                    if album_id not in _dbmod.Database._active_backfills:
                        target_ids.append(album_id)
                        _dbmod.Database._active_backfills.add(album_id)
                        if len(target_ids) >= ALBUM_BATCH_SIZE:
                            break
            if not target_ids:
                return False

            _dbmod.logger.info("[Backfiller-%s] Fetching metadata for %d albums",
                               self.user, len(target_ids))
            fetched_albums = []
            attempted_ids = []
            fullyWalkedIds = []
            use_fallback = True

            quotaWalled = _dbmod.time.time() < self._catalogBackoffUntil
            access_token = None if quotaWalled else getAccessToken()
            self._noteTokenHealth(getAccessToken, hasWebApiCreds)
            if isinstance(access_token, str) and access_token:
                headers = {"Authorization": f"Bearer {access_token}"}

                def onAlbum(album_id, resp, headers=headers):
                    albumRaw = resp.json()
                    if self._walkRemainingAlbumTracks(albumRaw, headers, stop_event):
                        fullyWalkedIds.append(album_id)
                    fetched_albums.append(albumRaw)

                batch = self._spendCatalogBatch(
                    "albums", target_ids, headers, stop_event, onAlbum)
                attempted_ids.extend(batch.attempted)
                use_fallback = not attempted_ids

                if batch.firstFailure is not None and flaskDebugEnabled():
                    _dbmod.logger.warning(
                        "[Backfiller-%s] Spotify Web API returned %s for %d of %d album(s).",
                        self.user, batch.firstFailure, batch.failures, len(target_ids)
                    )
            elif hasWebApiCreds and not quotaWalled:
                _dbmod.logger.warning(
                    "[Backfiller-%s] Failed to refresh access token. Falling back to the cookie client.",
                    self.user)

            if use_fallback:
                import Database.Spotify
                sp = Database.Spotify.Spotify()
                for album_id in target_ids:
                    if stop_event.is_set():
                        break
                    try:
                        album_raw = sp.album(album_id)
                        if album_raw:
                            fetched_albums.append(album_raw)
                        attempted_ids.append(album_id)
                    except Exception as fe:
                        _dbmod.logger.warning(
                            "[Backfiller-%s] Cookie client failed for album %s: %s",
                            self.user, album_id, fe)
                    stop_event.wait(1.0)

                if fetched_albums:
                    _dbmod.logger.info("[Backfiller-%s] Cookie client fetched %d album(s)",
                                       self.user, len(fetched_albums))
                else:
                    _dbmod.logger.warning(
                        "[Backfiller-%s] Cookie-client fallback failed to fetch any albums",
                        self.user)

            from Database.utils import convertToDatetime
            updated_count = 0
            for album_raw in fetched_albums:
                album_id = album_raw.get("id")
                release_date_str = album_raw.get("release_date")
                total_tracks = album_raw.get("total_tracks", 0)
                album_name = album_raw.get("name")

                if release_date_str == "0000-00-00" or not release_date_str:
                    release_date = 0.0
                else:
                    try:
                        dt = convertToDatetime(release_date_str)
                        release_date = dt.timestamp() if dt else 0.0
                    except Exception:
                        release_date = 0.0

                self.repo.updateAlbumMetadata(album_id, release_date, total_tracks,
                                              name=album_name if album_name else None)

                tracks_data = album_raw.get("tracks", {}).get("items") or []
                for track_raw in tracks_data:
                    track_id = track_raw.get("id") or track_raw.get("track_id")
                    if not track_id:
                        continue
                    track_name = track_raw.get("name")
                    if track_name:
                        duration_ms = track_raw.get("duration_ms") or 0
                        self.repo.updateTrackName(
                            track_id, track_name,
                            duration_ms=duration_ms if duration_ms > 0 else None)
                    repair_artists = self._normalizeBackfillArtists(track_raw.get("artists") or [])
                    if repair_artists:
                        self.repo.addMissingTrackArtists(track_id, repair_artists)

                updated_count += 1

            if attempted_ids:
                self.repo.markAlbumsBackfillAttempted(attempted_ids)
            self.repo.markAlbumsArtistRepairDone(fullyWalkedIds)

            if updated_count > 0:
                _dbmod.logger.info(
                    "[Backfiller-%s] Updated metadata for %d album(s)",
                    self.user, updated_count
                )
            return True
        finally:
            try:
                with _dbmod.Database._backfill_lock:
                    for album_id in target_ids:
                        _dbmod.Database._active_backfills.discard(album_id)
            except Exception as cleanupError:  # noqa: BLE001 - must not mask the in-flight error
                _dbmod.logger.warning("[Backfiller-%s] Failed to release %d in-flight album ids: %s",
                                      self.user, len(target_ids), cleanupError)

    def _metadataBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Periodically queries Spotify for missing album release dates and tracks.

        `stop_event` is THIS run's private event (see the fresh-event note in
        startMetadataBackfiller) - a later restart can never revive this
        thread."""
        import random
        if stop_event is None:
            stop_event = self.backfiller_stop_event
        try:
            # 1. Random startup offset to prevent multiple user threads from starting at the same moment
            startup_delay = random.randint(self.BACKFILLER_MIN_START_DELAY, self.BACKFILLER_MAX_START_DELAY)
            _dbmod.logger.info("[Backfiller-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                _dbmod.logger.info("[Backfiller-%s] Stopped during startup delay", self.user)
                return

            while not stop_event.is_set():
                try:
                    if not self.repo.isSpotifyApiBackfillEnabled():
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # 2. Get Spotify API credentials if configured, and set up
                    # the one access token this cycle's Web-API work shares.
                    # Both steps below want it and each used to refresh its own,
                    # so a cycle spent two round-trips on Spotify's token
                    # endpoint to be told the same thing twice. Minted on first
                    # ask rather than here, so a cycle with nothing to do still
                    # makes no network calls at all - see _CycleAccessToken.
                    #
                    #< all three, like the listener's own gate - see the same
                    #  comment on _fetchArtistImageUrl's copy in media_fetch.py
                    creds = self.getUserSpotifyCredentials()
                    hasWebApiCreds = bool(creds and creds.get("client_id") and creds.get("client_secret")
                                          and creds.get("refresh_token"))

                    def mintAccessToken():
                        if not hasWebApiCreds:
                            return None
                        from Database.Listeners.spotifyListener import _refresh_spotify_access_token
                        return _refresh_spotify_access_token(
                            creds["client_id"], creds["client_secret"], creds["refresh_token"],
                            logUser=self.user)

                    getAccessToken = _CycleAccessToken(mintAccessToken)

                    # 2b. One ISRC batch per cycle. Deliberately ahead of the
                    # album queue's early-out below: albums run dry long before
                    # tracks do (the album queue only holds rows with MISSING
                    # metadata, while every track in the catalog starts without
                    # an ISRC), so placing this after the `continue` would have
                    # meant the ISRC backfill silently stopped the moment album
                    # metadata was complete - which is the steady state. That
                    # ordering is also why it swallows its own failures.
                    self._backfillTrackIsrcs(getAccessToken, stop_event)

                    self._runTrackMergeIfDue()

                    #< here AND after the album branch's own use below, because
                    #  either step can be the one that asks for the token first
                    #  (this one has work in the steady state; the album queue
                    #  drains) - the token object remembers it was counted, so a
                    #  cycle is counted once either way
                    self._noteTokenHealth(getAccessToken, hasWebApiCreds)

                    if not self._runAlbumMetadataBatch(
                            getAccessToken, hasWebApiCreds, stop_event):
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                except Exception as e:
                    self._recordWorkerCycle("spotify_api", success=False, error=_dbmod.parseError(e))
                    _dbmod.logger.error("[Backfiller-%s] Error in metadata backfiller loop: %s", self.user, e)
                else:
                    self._recordWorkerCycle("spotify_api", success=True)

                if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                    break

        finally:
            _dbmod.logger.info("[Backfiller-%s] Exited gracefully", self.user)
