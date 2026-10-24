# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Imported for the type hints below. Everything used at RUNTIME still goes
# through _dbmod, so the suite's patch("Database.database.X") targets are
# unaffected - these names were simply never defined here, leaving the hints
# unresolvable for tooling and for typing.get_type_hints(). All leaf modules
# (stdlib, or Database/lastfm.py, which imports nothing of ours), so a real
# import costs nothing and cannot cycle.
from pathlib import Path
from Database.lastfm import (LastfmClient, LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS,
                             LASTFM_LAZY_FETCH_ACQUIRE_TIMEOUT_SECONDS)

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod


# Cover art from Spotify's CDN is tens of KB; this is a wide ceiling that only
# a misbehaving or redirected endpoint would reach. Without it the whole body is
# materialized in memory before PIL ever inspects it, so one bad response could
# balloon a worker thread's footprint.
MAX_IMAGE_BYTES = 12 * 1024 * 1024

# Every stored image is normalized to JPEG under `<imgId>.jpeg` (see the save
# below), so this is the ONLY extension that can name a file on disk. Exported
# because the serving routes have to agree: routes/media.py used to derive the
# image id with splitext and keep whatever stem it found, which made
# `<id>.png` a request for a path that could never exist - and its
# missing-file heal then deleted a healthy row and re-fetched on every hit.
STORED_IMAGE_EXTENSION = ".jpeg"

# The only formats a downloaded image may be decoded as. Image.open otherwise
# probes every format Pillow registers, and its decoders - FlashPix, TIFF, PDF,
# the multi-frame loaders - are where its security advisories keep landing.
# Cover and artist art is served as JPEG (PNG/WEBP kept as cheap headroom), so
# anything else is refused as an undecodable image before its decoder runs.
ALLOWED_IMAGE_DECODE_FORMATS = ("JPEG", "PNG", "WEBP")

# Bounds both outbound requests below - the CDN image download and the Web API
# artist lookup. Each runs on a background image thread and has a fallback
# behind it (the cookie client, or a later retry through the pending claim), so
# a stalled endpoint should cost one slow attempt rather than a wedged worker.
MEDIA_FETCH_HTTP_TIMEOUT_SECONDS = 10

# How long a minted Web API access token is reused before asking
# accounts.spotify.com for another. Spotify issues them with expires_in=3600;
# 50 minutes leaves a 10-minute margin so a token handed out at the edge of
# the window still outlives the request it authorizes.
WEB_API_TOKEN_CACHE_SECONDS = 50 * 60


# The one status that is a verdict on the TOKEN rather than on the request or
# the image: the artist lookup reacts to it by evicting the token cache (see
# _fetchArtistImageUrl), which no other failure may do.
HTTP_STATUS_UNAUTHORIZED = 401

# The 4xx that ask us back rather than turning us away: Request Timeout, Too
# Early, Too Many Requests. They sit on the client side of the number and the
# server side of the meaning, so the "a 4xx is a verdict" rule below has to step
# around them by name. 429 is the one that actually happens - it is what a CDN
# returns under rate limiting, which any account's burst can open.
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


def _isTransientFetchError(exc: Exception) -> bool:
    """Whether this failure left us knowing nothing about the image.

    The line is "did we get the bytes and find them unusable, or did we never
    get them". Anything that is not a request error got far enough to be a
    verdict about the image itself - PIL could not decode it, the cap says it
    is too big (a ValueError, deliberately not a RequestException). Those stay
    'failed'.

    This answers about the FETCH only. A failure to store bytes that already
    arrived and decoded is a third thing - neither a blip nor a verdict - and
    it cannot be told apart by type here (PIL raises OSError for an
    undecodable image, and so does a full disk). _downloadImageTask knows
    which phase it was in and handles that case itself.

    A request error is transient EXCEPT a 4xx, which is the server telling us
    this URL is not going to work: a 404 cover stays 404, and re-asking is the
    load 'failed' exists to stop. 5xx, timeouts and connection resets carry no
    such answer - `response` is absent on those, which is what the status
    lookup falls through on.

    RETRYABLE_HTTP_STATUSES are the exception to the exception. They are 4xx
    that mean "later", not "no", and treating one as a verdict is the exact harm
    this function was written to prevent - an artist losing their picture for
    good over a busy minute, recoverable only by shipping a migrator."""
    if not isinstance(exc, _dbmod.requests.exceptions.RequestException):
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in RETRYABLE_HTTP_STATUSES:
        return True
    return not (status and 400 <= status < 500)


class MediaFetchMixin:
    """Album/artist image + Last.fm biography fetching and on-demand refresh, mixed into Database."""

    @staticmethod
    def _readCappedBody(response) -> bytes:
        """The response body, refusing anything over MAX_IMAGE_BYTES.

        Content-Length is only an early exit - a server may omit or understate
        it - so the body is also accumulated with a hard stop. Streaming keeps
        an oversized response from being materialized in full before PIL ever
        looks at it."""
        declared = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
        #< isdecimal(), not isdigit(): a superscript or circled digit is
        #  isdigit-true and int()-rejected, and that ValueError is not the
        #  cap's own - it escaped before imageIsGood was set and, not being a
        #  RequestException, marked the image FAILED for good. A header the
        #  cap cannot read is a header to ignore; the body cap below still
        #  holds. The same 08e8e98 rule the ?page= params follow.
        if declared is not None and str(declared).isdecimal() and int(declared) > MAX_IMAGE_BYTES:
            raise ValueError(f"image too large: {declared} bytes (max {MAX_IMAGE_BYTES})")

        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _mediaGet(self, url: str, **kwargs):
        """One GET through the process-wide media session (see
        Database._mediaHttpSession) - the image downloads and the Web API
        artist lookups below, nothing else. Also the seam the suite patches
        in place of requests.get: a Session's get() is its own method, so a
        patch on the module function would no longer intercept these."""
        return self._mediaHttpSession.get(url, **kwargs)

    def _downloadImageTask(self, path: Path, url: str, imgId: str, kind: str):
        #< flipped the moment the image itself is proven good; from there on a
        #  failure is this host's, not the image's - see the storage note below
        imageIsGood = False
        try:
            # Held in a `with`: the body is streamed and _readCappedBody stops
            # draining it the moment it blows the cap. Closing the response
            # promptly hands its socket back to the shared session's pool - or
            # drops it when the body was left undrained, which is the right
            # outcome for a refused one - instead of leaving it to GC while the
            # decode/save work below runs. (Before the session, requests.get
            # built and discarded a pool per call, so there was nothing to
            # return the socket to; the `with` was right for the GC reason
            # alone.)
            with self._mediaGet(url, timeout=MEDIA_FETCH_HTTP_TIMEOUT_SECONDS, stream=True) as response:
                response.raise_for_status()
                content = self._readCappedBody(response)
            img = _dbmod.Image.open(_dbmod.BytesIO(content), formats=ALLOWED_IMAGE_DECODE_FORMATS)
            # Decoded HERE rather than left to img.save() below. Image.open only
            # reads the header, so a truncated or corrupt body opens fine and
            # raises in the middle of the storage step - where the rule below
            # would read it as the disk refusing a good image and retry it
            # forever. Work save() would do anyway, just moved in front of the
            # line that decides what the failure means.
            img.load()
            # Always store as JPEG: the templates hardcode `<imgId>.jpeg`, so an
            # image saved under its source format (e.g. .png) would 404 forever.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")   #< JPEG can't store alpha/palette modes
            # Everything past this point is LOCAL. The bytes arrived and decoded,
            # so a failure from here on is a fact about this host's disk, not
            # about the image, and must not record the permanent verdict:
            # lazyFetchArtistImage refuses a 'failed' artist before it ever
            # reaches the reclaim tryClaimImageDownload would allow, so one full
            # disk - or one antivirus handle held on a just-written file, which
            # the cleanup note below names as a real event on Windows - cost that
            # artist their picture until someone shipped a migrator. That is the
            # harm releaseImageClaim exists to stop, arriving through the one
            # door _isTransientFetchError cannot see.
            imageIsGood = True
            path.mkdir(parents=True, exist_ok=True)
            # Staged and renamed, not written in place: a save that dies partway
            # (full disk, power loss) would otherwise leave a truncated JPEG at
            # the final path - and lazyFetchArtistImage returns True on
            # imagePath.exists() BEFORE it consults the download status, so that
            # broken file would be served forever and never re-fetched. Same
            # .partial + os.replace shape the database backup uses.
            finalPath = path / f"{imgId}{STORED_IMAGE_EXTENSION}"
            partialPath = finalPath.with_suffix(".partial")
            try:
                img.save(partialPath, format="JPEG")
                _dbmod.os.replace(partialPath, finalPath)
            except Exception:
                # Swallowed on purpose: this runs inside an `except`, where a
                # raised exception REPLACES the one being handled. On Windows
                # the moment a file has just been written is exactly when a
                # scanner may still hold it, and the log line below is the only
                # record this download leaves - it has to name the disk that
                # refused the save, not the handle that refused the delete.
                # (Database/backup.py::_discardPartial, same shape.)
                try:
                    partialPath.unlink(missing_ok=True)
                except OSError as cleanupError:
                    _dbmod.logger.warning("Could not remove the incomplete image %s: %s",
                                          partialPath, _dbmod.parseError(cleanupError))
                raise
            self.repo.markImageStatus(imgId, kind, _dbmod.IMAGE_STATUS_OK)
        except Exception as e:
            if imageIsGood or _isTransientFetchError(e):
                #< see releaseImageClaim: 'failed' is a verdict about the IMAGE,
                #  and lazyFetchArtistImage refuses one for good. Two failures
                #  are not entitled to it - a network-layer one never saw the
                #  image, and a storage one (imageIsGood) saw a perfectly fine
                #  one and could not put it down. An artist whose lookup
                #  succeeded and whose download then hit a blip, or whose JPEG
                #  landed on a full disk, would otherwise lose their picture
                #  permanently. Track covers re-claim a 'failed' row anyway
                #  (tryClaimImageDownload allows it, and the listener re-saves on
                #  the next play), so for them this only avoids recording a
                #  verdict nobody reached.
                self.repo.releaseImageClaim(imgId, kind)
            else:
                self.repo.markImageStatus(imgId, kind, _dbmod.IMAGE_STATUS_FAILED)
            if isinstance(e, _dbmod.requests.exceptions.RequestException):
                _dbmod.logger.error("Error fetching image from %s (id=%s): %s", url, imgId, _dbmod.parseError(e))
            else:
                _dbmod.logger.error("Error saving image (id=%s): %s", imgId, _dbmod.parseError(e))

    def _saveImg(self, path: Path, url: str, imgId: str, kind: str):
        if not url:
            return  #< Spotify occasionally returns tracks with no album images; skip silently
        # Atomically claim the download: returns False if this image is already
        # downloaded or another thread/user already claimed it - shared across the
        # whole process (and would even be safe across separate processes, unlike
        # the old per-instance in-memory id sets).
        if not self.repo.tryClaimImageDownload(imgId, kind):
            return
        self._imageDownloadExecutor.submit(self._downloadImageTask, path, url, imgId, kind)

    def saveTrackImg(self, url: str, imgId: str):
        self._saveImg(self.imgDir_tracks, url, imgId, kind=_dbmod.IMAGE_KIND_TRACK)

    #< (access token, the refresh token it was minted from, monotonic expiry) -
    #  a class attribute so it has a declared home and a documented default
    #  (the metadata backfiller's _catalogBackoffUntil pattern); per-instance,
    #  i.e. per-user, from the first write
    _webApiTokenCache = None

    def _cachedWebApiAccessToken(self, creds):
        """An access token for this user's Web API credentials, minted at most
        once per WEB_API_TOKEN_CACHE_SECONDS.

        Every artist-image lookup used to POST accounts.spotify.com for a
        fresh token and discard it after one request. The image-status claims
        already dedup lookups to once per artist ever, but a big import queues
        hundreds of missing artists at once - hundreds of identical token
        POSTs for a token that lives an hour.

        Keyed on the refresh token, so rotated credentials miss the cache
        immediately. The one other invalidation is a 401 from the API itself:
        _fetchArtistImageUrl evicts the cache and retries once on a fresh
        mint, so a token dying mid-TTL costs one doomed request instead of
        pairing every lookup for the rest of the window with one. A failed
        refresh is never cached, so the next lookup retries - which also
        bounds a truly revoked grant at one refused POST per lookup. Two image
        threads may race a refresh and both POST; the loser's token overwrites
        the winner's and both are valid - and an eviction racing a mint at
        worst discards a still-valid token and forces one extra POST - so the
        whole cache is left unlocked on purpose."""
        cached = self._webApiTokenCache
        if cached is not None:
            token, mintedFrom, expiresAt = cached
            if mintedFrom == creds["refresh_token"] and _dbmod.time.monotonic() < expiresAt:
                return token
        from Database.Listeners.spotifyListener import _refresh_spotify_access_token
        token = _refresh_spotify_access_token(
            creds["client_id"], creds["client_secret"], creds["refresh_token"],
            logUser=self.user)
        if isinstance(token, str) and token:
            self._webApiTokenCache = (token, creds["refresh_token"],
                                      _dbmod.time.monotonic() + WEB_API_TOKEN_CACHE_SECONDS)
            return token
        return None

    def _fetchArtistImageUrl(self, artistId: str) -> str | None:
        """Looks up a real Spotify CDN image URL for an artist, mirroring the
        dual-path fetch _metadataBackfillLoop already uses for albums: the
        authenticated Web API first (if this user has API credentials configured),
        falling back to the cookie client otherwise. Returns None if neither source has
        an image (including a definitive "no images" response from the official
        API, which is trusted as-is rather than spending another request asking
        the cookie client the same question).

        Previously this scraped open.spotify.com's public artist page for an
        og:image meta tag - that stopped working once Spotify moved artist pages
        to a client-rendered SPA shell with no server-rendered metadata, for every
        artist, not just obscure ones."""
        creds = self.getUserSpotifyCredentials()
        #< all three, like the listener's own gate (spotifyListener.py): the
        #  refresh call reads client_secret unconditionally, so a two-of-three
        #  row raised KeyError here instead of falling back
        if creds and creds.get("client_id") and creds.get("client_secret") and creds.get("refresh_token"):
            access_token = self._cachedWebApiAccessToken(creds)
            if access_token:
                try:
                    artistUrl = f"https://api.spotify.com/v1/artists/{artistId}"
                    resp = self._mediaGet(artistUrl,
                                          headers={"Authorization": f"Bearer {access_token}"},
                                          timeout=MEDIA_FETCH_HTTP_TIMEOUT_SECONDS)
                    if resp.status_code == HTTP_STATUS_UNAUTHORIZED:
                        # The cached token died before its TTL (grant revoked,
                        # or the token invalidated server-side). Evict rather
                        # than pair every lookup for the rest of the cache
                        # window with a doomed request, and retry ONCE on a
                        # fresh mint; a failed refresh is never cached, so a
                        # truly revoked grant costs one refused POST here and
                        # falls back below like every other Web API failure.
                        _dbmod.logger.warning(
                            "Web API rejected the cached access token (401) for artist %s (user %s) - "
                            "evicting the token cache and retrying once.", artistId, self.user)
                        self._webApiTokenCache = None
                        access_token = self._cachedWebApiAccessToken(creds)
                        if access_token:
                            resp = self._mediaGet(artistUrl,
                                                  headers={"Authorization": f"Bearer {access_token}"},
                                                  timeout=MEDIA_FETCH_HTTP_TIMEOUT_SECONDS)
                    if resp.status_code == 200:
                        images = resp.json().get("images") or []
                        return images[0]["url"] if images else None
                except Exception as e:
                    _dbmod.logger.warning("Web API artist image fetch failed for %s, falling back to the cookie client: %s",
                                    artistId, _dbmod.parseError(e))
            else:
                _dbmod.logger.warning("Failed to refresh access token, falling back to the cookie client for artist image %s.", artistId)

        import Database.Spotify
        # No cookiesFile on purpose: artist() is a public lookup through
        # spotapi's pooled client and never touches the login. Constructing
        # with cookies ran a full login() whose TLSClient atexit-pinned one
        # live curl session PER ARTIST IMAGE for the life of the process -
        # the same leak _pooledPublicClient was built to close - and paid a
        # temp cookies-file write/delete for a session nothing used.
        sp = Database.Spotify.Spotify()
        images = (sp.artist(artistId) or {}).get("images") or []
        return images[0]["url"] if images else None

    def _lazyFetchArtistImageTask(self, artistId: str, imagePath: Path) -> bool:
        try:
            imageUrl = self._fetchArtistImageUrl(artistId)
        except Exception as e:
            _dbmod.logger.error("Failed to lazy load artist image for %s: %s", artistId, _dbmod.parseError(e))
            #< NOT 'failed'. That status means "asked, and this artist has no
            #  picture", and lazyFetchArtistImage refuses it for good - it
            #  returns False before reaching the reclaim tryClaimImageDownload
            #  would otherwise allow. An exception establishes nothing of the
            #  sort: a network blip or an open limiter backoff window (which
            #  any user's 429 opens) would deny that artist their picture
            #  permanently, fixable only by a migrator - which this project has
            #  now had to ship twice for stuck 'failed' rows. Release the claim
            #  and let a later render ask again.
            self.repo.releaseImageClaim(artistId, _dbmod.IMAGE_KIND_ARTIST)
            return False

        if not imageUrl:
            #< a real answer: the lookup came back and carried no images
            self.repo.markImageStatus(artistId, _dbmod.IMAGE_KIND_ARTIST, _dbmod.IMAGE_STATUS_FAILED)
            return False

        # Reuses the same download/normalize-to-JPEG/markImageStatus pipeline
        # tracks and albums already go through, run synchronously since this task
        # itself already runs on _imageDownloadExecutor (dispatched non-blocking
        # by lazyFetchArtistImage below) - submitting again would just queue a
        # redundant future.
        self._downloadImageTask(imagePath.parent, imageUrl, artistId, _dbmod.IMAGE_KIND_ARTIST)
        return imagePath.exists()

    def lazyFetchArtistImage(self, artistId: str, imagePath: Path):
        """Best-effort fetch of an artist's image via the Spotify Web API /
        the cookie client (see _fetchArtistImageUrl), used as a fallback for artists we
        never received image metadata for from the API. Deduplicated per artist id
        via the database's image status table so failed fetches persist across app
        restarts.

        The actual fetch runs on the shared image-download executor (like
        saveTrackImg()/saveArtistImg()) instead of inline, so a request for a
        still-missing image doesn't block the request thread on up to two
        sequential network calls. Returns True if the image is already on
        disk (nothing to do); otherwise returns the submitted Future for a
        freshly kicked-off fetch (the HTTP route that calls this doesn't wait
        on it - it just serves whatever's on disk right now, same as the
        other image types - callers that do need to wait, e.g. tests, can
        call .result() on it), or False if there's nothing to fetch (no
        artistId, or a fetch for this id already succeeded/failed)."""
        if imagePath.exists():
            return True
        if not artistId:
            return False

        status = self.repo.imageStatus(artistId, _dbmod.IMAGE_KIND_ARTIST)
        if status == _dbmod.IMAGE_STATUS_OK:
            return imagePath.exists()
        if status == _dbmod.IMAGE_STATUS_FAILED:
            return False

        if self.repo.tryClaimImageDownload(artistId, _dbmod.IMAGE_KIND_ARTIST):
            return self._imageDownloadExecutor.submit(self._lazyFetchArtistImageTask, artistId, imagePath)
        return False

    def _lazyFetchArtistBioTask(self, artistId: str, artistName: str) -> bool:
        """Returns whether a usable bio was actually stored - False for a
        definitive "nothing available" result, transient/invalid-key
        (unattempted, left for a later retry), or an unexpected error."""
        try:
            apiKey = self.repo.getUserLastfmApiKey(self.user)
            if not apiKey:
                return False   #< key removed between the claim and this task running; leave unattempted
            outcome = _dbmod.LastfmClient(apiKey).getArtistInfo(
                artistName, timeout=LASTFM_LAZY_FETCH_ACQUIRE_TIMEOUT_SECONDS)
            if outcome is None or outcome.status not in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
                return False   #< transient/invalid-key: stays unattempted, a later page view retries
            bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
            self.repo.setArtistBio(artistId, bio)
            return bio is not None
        except Exception as e:
            _dbmod.logger.error("Failed to lazy fetch artist bio for %s: %s", artistId, _dbmod.parseError(e))
            return False
        finally:
            self._releaseLastfmEntities("bio", [{"id": artistId}])

    def _submitOrRelease(self, executor, kind: str, entityId: str, task, *taskArgs):
        """Dispatch `task`, handing the in-flight claim back if it never runs.

        Every claimed entity is released by its TASK's `finally`, which is the
        right place - the task is what holds it. But that makes the dispatch
        itself a gap: if submit() raises, the claim has been taken and no task
        exists to give it back. _lastfm_active is class-level and process-wide,
        so the entity is then permanently unfetchable by every user's worker
        and every page view until the process restarts.

        Not hypothetical: submit() raises RuntimeError once the executor is
        shut down, and shutdown closes these pools while page renders are
        still arriving. Re-raised rather than swallowed - the caller asked for
        a Future and there isn't one, and the caller's own error handling is
        better placed to say so than a silently-False return here."""
        try:
            return executor.submit(task, *taskArgs)
        except Exception:
            self._releaseLastfmEntities(kind, [{"id": entityId}])
            raise

    def lazyFetchArtistBio(self, artistId: str, artistName: str):
        """Best-effort, on-demand fetch of an artist's biography via Last.fm's
        artist.getinfo (see Database.lastfm.getArtistInfo for the HTML-
        stripping and "+"-name incorrect-tag-entity guard), triggered by a
        page visit rather than the background biography backfiller's queue.
        Permanent-once-tried like artist images: THIS call never retries an
        artist it's already attempted, even if the result was "no bio" - the
        background backfiller (_lastfmBiographyBackfillLoop) is the one that
        revisits a definitive-empty result later, on its own 30-day cycle
        (see getArtistsMissingBiographies), so a page view alone can't spin
        Last.fm on every visit to an artist with no bio.

        The admin's instance-wide toggle (isArtistBioEnabled) gates fetching
        itself, not just display - disabled means no lookups at all, same
        contract as the Last.fm genre backfill's kill switch.

        Runs on a small dedicated executor (like lazyFetchArtistImage) so an
        artist page render never blocks on Last.fm's response time; the
        route doesn't wait on it and just renders whatever's already stored
        (None on a first-ever visit - a later visit shows it once fetched).
        Returns True if already attempted (bio may still be None -
        definitively no bio available), False if there's nothing to fetch
        (no artistId/name, the feature is disabled, no stored Last.fm key for
        this user, or another thread already claimed this artist), or the
        submitted Future on a freshly kicked-off fetch."""
        if not artistId or not artistName:
            return False
        if not self.repo.isArtistBioEnabled():
            return False

        state = self.repo.getArtistBioState(artistId)
        if state["attempted_at"] is not None:
            return True
        if not self.repo.getUserLastfmApiKey(self.user):
            return False

        if self._claimLastfmEntities("bio", [{"id": artistId}]):
            #< the claim is released by the TASK's finally, so a submit() that
            #  never produces a task has to hand it back here - otherwise the
            #  entity stays claimed by nothing, process-wide, for the life of
            #  the process. Reachable during shutdown, when the pools close
            #  while page renders are still arriving.
            return self._submitOrRelease(
                self._artistBioFetchExecutor, "bio", artistId,
                self._lazyFetchArtistBioTask, artistId, artistName)
        return False

    def _lazyFetchAlbumBioTask(self, albumId: str, albumName: str, artistName: str) -> bool:
        """Returns whether a usable bio was actually stored - mirrors
        _lazyFetchArtistBioTask, but album.getinfo also needs the album's
        primary artist name."""
        try:
            apiKey = self.repo.getUserLastfmApiKey(self.user)
            if not apiKey:
                return False   #< key removed between the claim and this task running; leave unattempted
            client = _dbmod.LastfmClient(apiKey)
            outcome = self._lastfmLookupBioOutcome(
                lambda name: client.getAlbumInfo(
                    artistName, name, timeout=LASTFM_LAZY_FETCH_ACQUIRE_TIMEOUT_SECONDS),
                albumName)
            if outcome is None or outcome.status not in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
                return False   #< transient/invalid-key: stays unattempted, a later page view retries
            bio = outcome.bio if outcome.status == _dbmod.OUTCOME_OK else None
            self.repo.setAlbumBio(albumId, bio)
            return bio is not None
        except Exception as e:
            _dbmod.logger.error("Failed to lazy fetch album bio for %s: %s", albumId, _dbmod.parseError(e))
            return False
        finally:
            self._releaseLastfmEntities("album_bio", [{"id": albumId}])

    def lazyFetchAlbumBio(self, albumId: str, albumName: str, artistName: str):
        """Best-effort, on-demand fetch of an album's biography via Last.fm's
        album.getinfo, mirroring lazyFetchArtistBio - same permanent-once-
        tried contract, same isAlbumBioEnabled() gate on fetching itself
        (not just display), dispatched on its own small executor
        (_albumBioFetchExecutor) so an album page render never blocks on
        Last.fm's response time. `artistName` is the album's primary
        artist - album.getinfo needs artist+album, unlike artist.getinfo.
        Returns True if already attempted, False if there's nothing to fetch
        (missing id/name/artist, the feature is disabled, no stored Last.fm
        key, or another thread already claimed this album), or the
        submitted Future on a freshly kicked-off fetch."""
        if not albumId or not albumName or not artistName:
            return False
        if not self.repo.isAlbumBioEnabled():
            return False

        state = self.repo.getAlbumBioState(albumId)
        if state["attempted_at"] is not None:
            return True
        if not self.repo.getUserLastfmApiKey(self.user):
            return False

        if self._claimLastfmEntities("album_bio", [{"id": albumId}]):
            #< see lazyFetchArtistBio: a dispatch that never ran still owns the claim
            return self._submitOrRelease(
                self._albumBioFetchExecutor, "album_bio", albumId,
                self._lazyFetchAlbumBioTask, albumId, albumName, artistName)
        return False

    def saveImagesFromTrack(self, track: dict):
        self.saveTrackImg(track["imageUrl"], track["imageId"])

    def refreshLastfmEntity(self, kind: str, entityId: str) -> dict:
        """Force a fresh Last.fm lookup for exactly one artist/album/track,
        bypassing every "already attempted" gate (GENRE_BACKFILL_RETRY_SECONDS,
        the bio permanent-once-tried contract) - the admin-triggered "Refresh
        Last.fm Data" button's synchronous action, not a batch. Uses the
        calling admin's own stored Last.fm key, like lazyFetchArtistBio.

        Returns {"status": ..., "name": <entity name, when known>}. status is
        one of: "no_api_key", "not_found", "no_artist" (album with no
        derivable primary artist), "invalid_key", "transient" (no definitive
        genre result this attempt - try again), "ok".

        Reuses _storeLastfmGenresWithInheritance for albums/tracks, so a
        refresh behaves exactly like a first-time backfill including its
        inheritance fallback - see that method's docstring for the one known
        edge case where stale OWN genres aren't cleared (the entity, its
        album and its artist all now come back empty after previously having
        own tags)."""
        apiKey = self.repo.getUserLastfmApiKey(self.user)
        if not apiKey:
            return {"status": "no_api_key"}
        client = _dbmod.LastfmClient(apiKey)

        try:
            if kind == "artist":
                return self._refreshArtistLastfmData(client, entityId)
            if kind == "album":
                return self._refreshAlbumLastfmData(client, entityId)
            if kind == "track":
                #< the SONG, not the release the admin happened to click from.
                #  Every genre read resolves the merge group, so a refresh
                #  written to a member is an answer no page can reach - and it
                #  would be looked up under the member's title while landing on
                #  the canonical's row. Resolved here, at the entry point, so
                #  the lookup row, the queried name, the write and the
                #  attempted-stamp are all one release (2026-09-03 review, H1).
                return self._refreshTrackLastfmData(
                    client, self.repo.resolveCanonicalTrackId(entityId))
            raise ValueError(f"Unknown Last.fm refresh kind: {kind!r}")
        except _dbmod._LastfmInvalidKeyError:
            return {"status": "invalid_key"}

    def _refreshArtistLastfmData(self, client: LastfmClient, artistId: str) -> dict:
        row = self.repo.getArtistLastfmLookupRow(artistId)
        if row is None:
            return {"status": "not_found"}

        # timeout on every lookup here (and in the album/track twins below):
        # this runs synchronously on an admin request thread, and an unbounded
        # limiter acquire sat out whole backoff windows (60s on a Last.fm 429)
        # holding a Waitress thread - the same rule validateApiKey follows.
        # A timed-out lookup (None) already maps to "transient": try again.
        outcome = client.getArtistTopTags(row["name"], stop_event=self.lastfm_stop_event,
                                          timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS)
        if outcome is None:
            return {"status": "transient"}
        definitive, genres = self._lastfmOutcomeGenres(outcome)
        if not definitive:
            return {"status": "transient"}
        self.repo.replaceArtistGenres(artistId, genres)
        self.repo.markArtistsLastfmAttempted([artistId])

        bioOutcome = client.getArtistInfo(row["name"], timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS)
        if bioOutcome is not None and bioOutcome.status in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
            self.repo.setArtistBio(artistId, bioOutcome.bio if bioOutcome.status == _dbmod.OUTCOME_OK else None)

        return {"status": "ok", "name": row["name"]}

    def _refreshAlbumLastfmData(self, client: LastfmClient, albumId: str) -> dict:
        row = self.repo.getAlbumLastfmLookupRow(albumId)
        if row is None:
            return {"status": "not_found"}
        primary = self.repo.getAlbumPrimaryArtists([albumId]).get(albumId)
        if primary is None:
            return {"status": "no_artist"}

        definitive, genres, aborted = self._lastfmLookupOwnGenres(
            lambda name: client.getAlbumTopTags(primary["artist_name"], name,
                                                stop_event=self.lastfm_stop_event,
                                                timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS),
            row["name"])
        if aborted or not definitive:
            return {"status": "transient"}
        if not self._storeLastfmGenresWithInheritance(
                client, "album", albumId, genres, primary["artist_id"], primary["artist_name"],
                timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS):
            return {"status": "transient"}

        bioOutcome = self._lastfmLookupBioOutcome(
            lambda name: client.getAlbumInfo(primary["artist_name"], name,
                                             timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS),
            row["name"])
        if bioOutcome is not None and bioOutcome.status in (_dbmod.OUTCOME_OK, _dbmod.OUTCOME_NOT_FOUND):
            self.repo.setAlbumBio(albumId, bioOutcome.bio if bioOutcome.status == _dbmod.OUTCOME_OK else None)

        return {"status": "ok", "name": row["name"]}

    def _refreshTrackLastfmData(self, client: LastfmClient, trackId: str) -> dict:
        row = self.repo.getTrackLastfmLookupRow(trackId)
        if row is None:
            return {"status": "not_found"}

        definitive, genres, aborted = self._lastfmLookupOwnGenres(
            lambda name: client.getTrackTopTags(row["artist_name"], name,
                                                stop_event=self.lastfm_stop_event,
                                                timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS),
            row["name"])
        if aborted or not definitive:
            return {"status": "transient"}
        if not self._storeLastfmGenresWithInheritance(
                client, "track", trackId, genres, row["artist_id"], row["artist_name"],
                albumId=row["album_id"], timeout=LASTFM_REFRESH_ACQUIRE_TIMEOUT_SECONDS):
            return {"status": "transient"}

        return {"status": "ok", "name": row["name"]}
