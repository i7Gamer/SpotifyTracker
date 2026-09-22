# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import datetime
import logging
import os
import tempfile
import threading
import time
import json
from pathlib import Path
from io import BytesIO

import requests
from PIL import Image
import concurrent.futures

try:
    from Database.Formatters.spotifyClient import Client
    from Database.Importers.StreamingHistoryImporter import Importer
    from Database.Importers.AutoImporter import AutoImporter
    from Database.Listeners.spotifyListener import (
        Listener, CONNECT_STATE_MISSED_TRACK_LOOKBACK_SECONDS, classifyListenerError,
    )
    from Database.repository import (
        Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED,
        SKIP_RATE_PRIOR_WEIGHT,
    )
    from Database.db import BEHAVIORAL_COLUMNS, SKIP_THRESHOLD_MS, WEB_API_BACKFILL_SOURCE
    from Database.utils import flaskDebugEnabled, parseError, convertToDatetime, timeToInt, getTimezone, listeningBuckets, SECONDS_PER_DAY
    from Database.lastfm import LastfmClient, filterTagsToGenres, cleanLookupName, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from Importers.StreamingHistoryImporter import Importer
    from Importers.AutoImporter import AutoImporter
    from Listeners.spotifyListener import (
        Listener, CONNECT_STATE_MISSED_TRACK_LOOKBACK_SECONDS, classifyListenerError,
    )
    from repository import (
        Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED,
        SKIP_RATE_PRIOR_WEIGHT,
    )
    from db import BEHAVIORAL_COLUMNS, SKIP_THRESHOLD_MS, WEB_API_BACKFILL_SOURCE
    from utils import parseError, convertToDatetime, timeToInt, getTimezone, listeningBuckets, SECONDS_PER_DAY
    from lastfm import LastfmClient, filterTagsToGenres, cleanLookupName, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY

#< after the shim above on purpose: on a direct `python Database/database.py`
#  run, utils' own sys.path fix (see Database/utils.py) is what makes the
#  repo-root config module importable at all
from config import WEEKDAY_NAMES, IMPORT_KEYWORD_ENV_VAR

logger = logging.getLogger(__name__)

#< TRUTHY_DEBUG_VALUES used to live here, and two other modules carried copies
#  of it labelled "mirrors Database.database". It is Database.utils'
#  TRUTHY_ENV_VALUES now, reached through flaskDebugEnabled() - which the
#  importer and the metadata backfiller previously read off this module via
#  _dbmod purely because they could not import it any other way.

# The genre-coverage categories (also the SQL alias prefixes in
# getGenreCoverage). The overall percentage is the mean across these, so the
# count must track this tuple - never a bare literal.
GENRE_COVERAGE_CATEGORIES = ("song", "album", "artist")

# How far back getCurrentStreak scans for the ongoing daily streak. A live
# streak is always far shorter; this only bounds the bucket query so it never
# walks the whole history to compute a number that can't exceed this anyway.
CURRENT_STREAK_LOOKBACK_DAYS = 400

# MAX_TIME_SERIES_BUCKETS/TIME_SERIES_MIN_BUCKET_DAYS moved to
# services.time_buckets (CORE-6, beside the gap-fill clamp that uses them)
# and are imported below, re-exported here so
# `from Database.database import MAX_TIME_SERIES_BUCKETS` keeps working.

IMAGE_DOWNLOAD_WORKERS = 5   #< bounds total concurrent image downloads for the whole process, not per user

ARTIST_BIO_FETCH_WORKERS = 2   #< bounds concurrent artist-bio fetches for the whole process; each is one
                                #  small artist.getinfo call (no image-style download/resize work), so a
                                #  much smaller pool than IMAGE_DOWNLOAD_WORKERS is enough

ALBUM_BIO_FETCH_WORKERS = 2    #< separate pool from ARTIST_BIO_FETCH_WORKERS so album lazy-fetches never
                                #  queue behind artist lazy-fetches (or vice versa)

# getCompletionStats' complete-vs-partial boundary is now an admin setting
# (COMPLETION_COMPLETE_PERCENT_KEY, default 80%): among real plays (is_skip=0), a
# listen at/over that percent of the track's duration counts as complete, else
# partial. Skips are the is_skip=1 rows (the admin-tunable skip threshold that
# replaced both the old 30s line and the play_skips table). See getCompletionStats.

# Images are shared across every user (album art / artist photos are the same
# bytes for everyone), so they live in one directory tree instead of under each
# user's own folder. Inside Data/ (see Database/db.py's DEFAULT_DB_PATH) so the
# Docker volume mount that persists the database also covers it.
MEDIA_DIR = Path(__file__).resolve().parent / "Data" / "Media"

_SPOTIFY_IMAGE_CDN = "https://i.scdn.co/image/"
_SPOTIFY_IMAGE_URI_PREFIX = "spotify:image:"
_ABSOLUTE_URL_SCHEMES = ("https://", "http://")


def _imageIdFromConnectMeta(meta) -> str | None:
    """Extract the album imageId (album ID string) from a connect-state
    metadata dict or Metadata dataclass.  Returns None if unavailable.

    The connect-state metadata carries the album URI in the form
    'spotify:album:<id>'; the album ID is what the rest of the system
    uses as imageId (matches the on-disk filename <albumId>.jpeg)."""
    album_uri = (meta.get("album_uri") if isinstance(meta, dict)
                 else getattr(meta, "album_uri", None))
    if not album_uri:
        return None
    parts = album_uri.rsplit(":", 1)
    return parts[-1] if len(parts) == 2 and parts[-1] else None


def _imageUrlFromConnectMeta(meta) -> str | None:
    """Build the Spotify CDN URL for the track's cover art from the
    connect-state metadata dict or Metadata dataclass.  Returns None if
    unavailable.

    The connect-state usually carries the image as 'spotify:image:<hash>',
    whose CDN URL is https://i.scdn.co/image/<hash> - but it sometimes carries
    the absolute CDN URL directly, which must be passed through untouched.
    Splitting that on its last ':' splits the SCHEME, so it used to come back
    as https://i.scdn.co/image///i.scdn.co/image/<hash> and 404 forever (7 such
    downloads failed on 2026-07-23). Worse than a transient miss: every retry
    rebuilt the SAME broken URL, so the cover stayed absent however many times
    it was re-attempted, and migrate1_40_0 had to clear the rows by hand.

    (This used to say the retry never happened - that _saveImg's claim gate
    refuses a 'failed' row. It does not: tryClaimImageDownload re-claims one
    deliberately, and getNowPlaying re-drives saveTrackImg on every poll. Only
    lazyFetchArtistImage treats 'failed' as final, and it is not in this path.
    The sentence is corrected rather than deleted because believing it is what
    makes a reader think a blip costs a cover permanently - see
    tests/test_database_images.py's
    test_a_failed_track_cover_is_reclaimable_but_a_failed_artist_is_not.)

    Anything that is neither shape returns None rather than being run through
    the prefix on the off chance it works - guessing is what produced the bad
    URL above."""
    imageRef = (meta.get("image_xlarge_url") or meta.get("image_url")
                if isinstance(meta, dict)
                else getattr(meta, "image_xlarge_url", None)
                     or getattr(meta, "image_url", None))
    if not imageRef:
        return None
    if imageRef.startswith(_ABSOLUTE_URL_SCHEMES):
        return imageRef
    if not imageRef.startswith(_SPOTIFY_IMAGE_URI_PREFIX):
        return None
    imageHash = imageRef[len(_SPOTIFY_IMAGE_URI_PREFIX):]
    return _SPOTIFY_IMAGE_CDN + imageHash if imageHash else None


class _LastfmInvalidKeyError(Exception):

    """The stored Last.fm key is invalid/suspended (error 10/26): raised out
    of a worker batch so the loop idles instead of burning 4 failing requests
    per second. A fixed key is picked up on the next cycle's fresh read."""


class _ImportRunState:
    """Play rows written by the current import run (or multi-file batch).

    The import's duplicate reconciliation matches each incoming entry against
    nearby existing plays and "corrects" a single differing match instead of
    inserting. That is only valid against rows from *other* sources (live
    listener / Web API backfill, where played_at semantics can differ) - but
    inserts happen inside the same transaction the matching reads from, so
    without this state an entry would also match the play a previous entry of
    the same run just wrote, collapsing two genuine plays (e.g. a short skip
    immediately followed by a replay of the same track) into one row.

    Invariant: an existing row can be claimed by at most one import entry per
    run - one physical play corresponds to exactly one export entry."""

    def __init__(self):
        self.claimedRowIds: set[int] = set()      #< existing rows updated or confirmed identical by this run
        self.insertedPlayKeys: set[tuple] = set() #< (track_id, played_at) of rows inserted by this run
        self.correctedYears: set[int] = set()     #< years to drop from Wrapped cache once a deferred-commit
                                                   #  batch (atomic overwrite) actually commits - see
                                                   #  _importHistoryLocked's deferCommit
        self.pendingImageTracks: dict[str, dict] = {}  #< tracks awaiting saveImagesFromTrack() once a
                                                   #  deferred-commit batch actually commits (same reason
                                                   #  as correctedYears - image-claiming self-commits too)
        self.unreadableDroppedTotal: int = 0      #< sum of UNREADABLE_DROP_STAT_KEYS, same accumulation
                                                   #  and same reason as retryableDroppedTotal just below
        self.retryableDroppedTotal: int = 0       #< sum of RETRYABLE_DROP_STAT_KEYS across every file this
                                                   #  run has actually committed, so a multi-file append
                                                   #  batch can name the count on its own final progress
                                                   #  line - the per-file line _applyImportData writes is
                                                   #  overwritten in import_progress by the next file's, and
                                                   #  by the batch's own summary after the last one

    def isOwnWrite(self, trackId: str, play: dict) -> bool:
        return play["id"] in self.claimedRowIds or (trackId, play["played_at"]) in self.insertedPlayKeys


from Database.media_fetch import MediaFetchMixin
from Database.import_service import ImportMixin
from Database.workers import WorkerLifecycleMixin
from services.listening_calendar import buildListeningCalendar, CALENDAR_WEEKS
from services.time_buckets import (
    MAX_TIME_SERIES_BUCKETS, TIME_SERIES_MIN_BUCKET_DAYS,
    bucketKey as _timeBucketKey, bucketRowsByKey, buildTimeSeries, fillHeatmapGrid,
)
from services.dashboard_trends import (
    obsessionSubtitle, rediscoverySubtitle, freshFindSubtitle, forgottenSubtitle,
)


class Database(MediaFetchMixin, ImportMixin, WorkerLifecycleMixin):
    PROGRESS_UPDATE_INTERVAL = 10   #< Write import progress to disk every N entries instead of every entry
    RECONNECT_MAX_RETRIES = 10  #< max reconnection attempts before giving up (~30 min window with backoff)
    RECONNECT_INITIAL_DELAY = 1  #< initial backoff in seconds
    RECONNECT_MAX_DELAY = 300  #< cap backoff at 5 minutes
    LISTENER_STOP_LOCK_TIMEOUT_SECONDS = 2  #< bound how long stop() waits for an in-flight
                                             #  startListener (a live Spotify login, ~15s) to release
                                             #  the listener lock - on timeout stop() proceeds and the
                                             #  in-flight call sees _stopping afterwards and tears its
                                             #  own freshly-built listener down
    BACKFILL_INSERT_GUARD_EXTRA_SECONDS = 60  #< margin added on top of a track's own duration for the
                                               #  wide, backfill-only insert-time dedup guard (see
                                               #  appendTrackData) - accounts for Spotify's played_at
                                               #  field being documented as inconsistent about whether
                                               #  it reports a track's start or end time (spotify/web-api#1083)
    IMPORT_MATCH_START_WINDOW_SECONDS = 15     #< an existing play starting within this window of an imported
                                               #  play is treated as the same physical play recorded with a
                                               #  slightly different timestamp (e.g. listener vs export)
    IMPORT_MATCH_END_WINDOW_SECONDS = 60       #< same idea for sources whose played_at recorded the track's
                                               #  end instead of its start (see the start/end ambiguity note
                                               #  on BACKFILL_INSERT_GUARD_EXTRA_SECONDS): imported start +
                                               #  track duration must land within this window of the DB row
    DUPLICATE_RECORDING_TOLERANCE_SECONDS = 5  #< max gap between two same-track local plays for them to count as
                                                #  the same real listen recorded twice (once by the live listener,
                                                #  once by Web API backfill) rather than a genuine replay. Proximity
                                                #  alone is NOT proof - real exports contain skip-then-restart pairs
                                                #  seconds apart - so reconciliation additionally requires the
                                                #  cluster to span different sources (see _reconcileWithWebApiHistory)
    BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS = 10  #< max gap between a backfill row's played_at (Spotify's
                                                     #  end-time reading) and a same-track listener row's
                                                     #  created_at for the two to count as the same listen. A
                                                     #  listener row is inserted at the track-change moment, so
                                                     #  its created_at IS the observed end, pauses included -
                                                     #  duration-based windows can't be, since a mid-track pause
                                                     #  stretches start-to-end by an unbounded amount (the
                                                     #  2026-08-04 double-recording: a ~3min pause put the copy
                                                     #  474s after a 287s track's start). Kept a tight point
                                                     #  match: live insert lag is ~1s, poll-mode detection a few
                                                     #  seconds - anything minutes away is a different listen.
    BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS = 20  #< max gap between a backfill row's played_at and a same-track
                                                 #  SKIP row's played_at for the two to count as the same listen.
                                                 #  The Web API reports no ms_played, so a backfill row stamps the
                                                 #  track's whole duration and is is_skip=0 by construction - which
                                                 #  means the is_skip=0 arms above can never see a listen the
                                                 #  listener classified as a skip, and the backfill re-added it as
                                                 #  a full play (2026-08-14: a 3.6s skip came back as 220s, 15s
                                                 #  away). Deliberately much tighter than those arms' duration+60s:
                                                 #  measured over live data the provable duplicates sat 3-15s from
                                                 #  their skip while the ambiguous ones sat 95s and 291s away,
                                                 #  where "skip, then a genuine replay the listener missed" is the
                                                 #  likelier reading - and suppression there is unrecoverable.
    #< the module-level import above, re-exposed on the class because the mixins
    #  reach it as self.WEB_API_BACKFILL_SOURCE. It is DEFINED in Database/db.py,
    #  the leaf module Listeners/spotifyListener.py can import too - which is the
    #  whole point: the listener tags plays with it, and the insert guard and the
    #  reconciler both match on it, so it has to be one string, not three.
    WEB_API_BACKFILL_SOURCE = WEB_API_BACKFILL_SOURCE

    NOW_PLAYING_STALE_GRACE_MS = 60_000        #< a "playing" track whose duration (plus this) has fully elapsed
                                                #  since the last connect-state update is a frozen/stale feed,
                                                #  not a real playback - report nothing instead

    LISTENER_DURATION_CORRUPTION_FACTOR = 10   #< a listener-reported play duration more than this many times
                                                #  the track's own length is feed corruption (e.g.
                                                #  7062895ms for a 171s track) - the play is recorded with the
                                                #  track's actual length instead of being dropped

    LISTENER_FUTURE_PLAY_GRACE_SECONDS = 86400  #< how far ahead of now a listener-reported play may be stamped
                                                #  before it reads as another account's data arriving on this
                                                #  listener (the contamination check) rather than as clock or
                                                #  timezone skew. A day, because skew is hours at worst and a
                                                #  wrongly-dropped play cannot be recovered

    LISTENER_DEGRADED_ERROR_THRESHOLD = 5      #< consecutive listener batches with a failed insert before the
                                                #  listener reports DEGRADED (exceeded, not reached); one clean
                                                #  batch resets the count - see _addToDatabaseFromListener

    BACKFILLER_MIN_START_DELAY = 30            #< random startup-offset bounds for the metadata backfiller,
    BACKFILLER_MAX_START_DELAY = 90            #  in seconds - staggers per-user threads after a restart

    WRAPPED_WORKER_MIN_START_DELAY = 60        #< minimum initial random startup delay in seconds
    WRAPPED_WORKER_MAX_START_DELAY = 300       #< maximum initial random startup delay in seconds
    WRAPPED_WORKER_LOOP_INTERVAL = 900         #< interval between consecutive checks in seconds (15 minutes)
    WRAPPED_YEAR_DELAY_SECONDS = 5             #< breathing room delay in seconds between recalculating years

    BACKFILLER_ALBUM_QUEUE_SIZE = 80           #< number of albums queued from DB for backfilling
    BACKFILLER_TRACK_QUEUE_SIZE = 200          #< same, for the ISRC queue: four times TRACK_BATCH_SIZE,
                                                #  the ratio the album queue uses. Read wider than one batch on
                                                #  purpose - the catalog is shared, so every user's backfiller
                                                #  drains this one queue, and a pool the size of a batch would
                                                #  leave the second worker to claim nothing and sit the cycle out
    BACKFILLER_IDLE_WAIT_SECONDS = 300         #< wait between metadata-backfill cycles when there's nothing
                                                #  to do (kill switch off, queue drained) or after a cycle

    LASTFM_BACKFILLER_MIN_START_DELAY = 30     #< random startup-offset bounds for the Last.fm genre
    LASTFM_BACKFILLER_MAX_START_DELAY = 90     #  backfiller, in seconds - staggers per-user threads
    LASTFM_QUEUE_BATCH_SIZE = 30               #< entities claimed per kind (artists/albums/tracks) per cycle
    LASTFM_QUEUE_POOL_SIZE = 600                #< candidates read per Last.fm queue refill; retained in memory across cycles
    LASTFM_QUEUE_POOL_TTL_SECONDS = 1800       #< non-empty candidate pools are refreshed after 30 minutes
    LASTFM_IDLE_WAIT_SECONDS = 300             #< wait between cycles once both queues are drained (or after errors)

    # The biography backfiller runs as its own thread alongside the genre one
    # (not sequentially after it) - a later startup window just gives genres a
    # head start on the shared rate limiter, not a hard ordering guarantee.
    LASTFM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY = 120
    LASTFM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY = 180
    LASTFM_BIOGRAPHY_QUEUE_BATCH_SIZE = 20     #< smaller than LASTFM_QUEUE_BATCH_SIZE: one artist.getinfo
                                                #  call per entity, sharing the same rate limiter as genres
    LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS = 300   #< wait between cycles once the queue is drained (or after errors)

    # The album biography backfiller runs as its own thread alongside the
    # artist one (not sequentially after it) - same independent-thread shape
    # as the genre-vs-biography split above.
    LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MIN_START_DELAY = 120
    LASTFM_ALBUM_BIOGRAPHY_BACKFILLER_MAX_START_DELAY = 180
    LASTFM_ALBUM_BIOGRAPHY_QUEUE_BATCH_SIZE = 20     #< one album.getinfo call per entity, same rate limiter
    LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS = 300   #< wait between cycles once the queue is drained (or after errors)

    WORKER_HEALTH_FAILING_THRESHOLD = 3        #< consecutive failed cycles before a periodic worker counts
                                                #  as FAILING on /admin - matches the "3 consecutive" precedent
                                                #  already used for the Spotify re-auth flake filter

    # Shared across every Database instance (every user) in this process. Image
    # download de-duplication is enforced by the `images` table (atomic across
    # threads *and* users), so a single bounded pool for the whole process is
    # enough - there's no need for one per user, and no need for the old
    # per-user in-memory id sets / metadata.json files this replaces.
    imgDir_tracks = MEDIA_DIR / "tracks"
    imgDir_artists = MEDIA_DIR / "artists"
    _imageDownloadExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_WORKERS)
    # Same shape as _imageDownloadExecutor, but for the artist-bio feature's
    # lazy fetch: a much smaller pool since each task is one lightweight
    # artist.getinfo call, not a download+resize.
    _artistBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=ARTIST_BIO_FETCH_WORKERS)
    # Same idea, for the album-bio feature's lazy fetch - its own pool (see
    # ALBUM_BIO_FETCH_WORKERS) rather than sharing _artistBioFetchExecutor.
    _albumBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=ALBUM_BIO_FETCH_WORKERS)
    # Every shared media pool, by attribute name - the list shutdownWorkerPools
    # walks. One registry rather than an inline tuple so a pool added beside
    # the three above cannot be configured at startup yet never retired (its
    # backlog would run behind CPython's untimed atexit join, the exact thing
    # shutdownWorkerPools exists to remove). Pinned complete against the class
    # attributes by test_app_worker_lifecycle - the WORKER_STOP_EVENT_NAMES
    # pattern, which exists because signalStop once missed the fifth stop
    # event the same way.
    MEDIA_POOL_ATTRIBUTE_NAMES = ("_imageDownloadExecutor", "_artistBioFetchExecutor",
                                  "_albumBioFetchExecutor")
    # The one HTTP session behind _imageDownloadExecutor's work - the CDN
    # image downloads and the Web API artist lookups (MediaFetchMixin._mediaGet
    # is the only reader). Module-level requests.get builds and discards a
    # Session, pool included, on EVERY call, so a large import's hundreds of
    # covers from one CDN host each paid a fresh TCP+TLS handshake. A Session
    # opens no socket until used, keeps one per host afterwards, and is the
    # usual idiom for concurrent plain GETs across a pool's threads. Retired
    # and replaced by shutdownWorkerPools beside the executors it serves.
    # Last.fm, the listener's backfill and the metadata backfiller keep their
    # per-call requests.get deliberately (paced, or per poll).
    _mediaHttpSession = requests.Session()
    _active_backfills = set()
    # The same idea for the ISRC queue, which drains the shared tracks table and
    # so has exactly the problem albums have. A separate set because these are
    # track ids, not album ids - two different namespaces that only happen to
    # look alike - but the same lock: both are momentary set edits, never held
    # across a request, and never nested one inside the other.
    _active_isrc_backfills = set()
    _backfill_lock = threading.Lock()
    # Same idea for the Last.fm genre backfillers: catalog entities are shared,
    # so two users' workers must not fetch the same (kind, id) concurrently.
    _lastfm_active = set()
    _lastfm_active_lock = threading.Lock()

    @classmethod
    def shutdownWorkerPools(cls) -> None:
        """Stop the three process-wide media pools, and the image pool's HTTP
        session with them. Called from SpotifyDashboardApp.shutdown().

        Without this the only thing that ever stops them is CPython's own
        concurrent.futures atexit hook, which appends its sentinel BEHIND the
        queued work and then joins every worker with no timeout - so the entire
        backlog runs after shutdown() has returned and reported the app
        stopped, outside the grace period the compose file allows, still
        issuing Last.fm and CDN requests until Docker's SIGKILL lands.

        cancel_futures drops what has not started: every one of these tasks is
        best-effort media that the next page view re-triggers, and an image
        claim abandoned mid-flight is already recovered at boot by
        deleteStalePendingImages.

        wait=False keeps THIS call short; it does not make an already-running
        task free. ThreadPoolExecutor workers have not been daemons since
        bpo-39812 (3.9) - concurrent.futures joins every one of them, untimed,
        at interpreter exit - so a download in flight when SIGTERM lands is
        still on the process's way out, bounded by its own HTTP timeout rather
        than by anything here. Dropping the QUEUE is what this buys, and the
        queue is the unbounded part.

        Each pool is REPLACED by a fresh one rather than left shut: a stopped
        ThreadPoolExecutor raises on every later submit(), and one process can
        outlive an app instance - the test suite builds and shuts down many
        apps in a row, and a poisoned class attribute would fail whichever
        unrelated test next rendered a page. ThreadPoolExecutor spawns no
        thread until something is submitted, so the replacement costs nothing,
        and a stray post-shutdown submit is no worse than before this method
        existed (it used to reach a live pool too). What is NOT preserved is
        the old pool's backlog, which is the whole point."""
        for name in cls.MEDIA_POOL_ATTRIBUTE_NAMES:
            pool = getattr(cls, name)
            try:
                # Replacement FIRST, then retire the object we already hold: a
                # thread that reads the class attribute between the two must
                # never get a shut-down pool back, because submit() on one
                # raises RuntimeError - which on the listener's path would take
                # an unwritten play down with it.
                setattr(cls, name, concurrent.futures.ThreadPoolExecutor(max_workers=pool._max_workers))
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                # Per pool, for the reason SpotifyDashboardApp.shutdown() guards
                # its two workers one at a time: one failure must not leave the
                # other two running.
                logger.error("Error stopping the %s thread pool: %s", name, e)
        # The image pool's HTTP session goes the same way, and in the same
        # order: a download still in flight keeps the object it already holds,
        # a later one finds a live session rather than a closed one.
        session = cls._mediaHttpSession
        try:
            cls._mediaHttpSession = requests.Session()
            session.close()
        except Exception as e:
            logger.error("Error closing the media HTTP session: %s", e)

    @classmethod
    def configureWorkerPools(cls, repo) -> None:
        """Resize the shared background thread pools from admin settings, read
        once at startup (SpotifyDashboardApp.__init__, after migrations). These
        pools are process-wide and sized before any task is submitted, so a
        changed setting only takes effect after a restart - the admin panel
        labels them accordingly. Each falls back to its code default. Recreating
        the executors here is safe: ThreadPoolExecutor spawns no threads until a
        task is submitted, and none has been at startup, so the import-time
        placeholders are discarded without leaking threads."""
        cls._imageDownloadExecutor = concurrent.futures.ThreadPoolExecutor(
            max_workers=repo.getImageDownloadWorkers(IMAGE_DOWNLOAD_WORKERS))
        cls._artistBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(
            max_workers=repo.getArtistBioFetchWorkers(ARTIST_BIO_FETCH_WORKERS))
        cls._albumBioFetchExecutor = concurrent.futures.ThreadPoolExecutor(
            max_workers=repo.getAlbumBioFetchWorkers(ALBUM_BIO_FETCH_WORKERS))

    def __init__(self, user: str, cookiesFile: str | None = None, email: str | None = None, dbPath=None,
                 shutdown_event: threading.Event | None = None, startWorkers: bool = True):
        """`startWorkers=False` builds a fully usable instance without spawning
        the five always-on background threads (metadata backfiller, wrapped
        calculations, and the three Last.fm backfillers).

        Every attribute those workers use is still initialized, so the start*
        methods work normally when a caller opts in later (startBackgroundWorkers()
        is the bundled opt-in) - this only skips the
        automatic start. The test suite constructs ~117 Databases; each one
        spawned five threads that immediately parked on a randomized startup
        delay, then had to be signalled and joined at teardown, for work no
        assertion depended on. Production always starts them."""
        if not user:
            raise ValueError("Database user must be specified and cannot be empty.")
        self.user = user   #< internal account key; identifies this db, its folders and its logs
        self.cookiesFile = cookiesFile
        self.email = email
        self.listener = None   #< built by startListener()
        self.baseDir: Path = Path(__file__).resolve().parent

        # Shutdown coordination. shutdown_event is the app-wide "we are
        # exiting" signal (SpotifyDashboardApp shares its _stop_event here);
        # _stopping is this instance's own end-of-life flag, set by
        # signalStop()/stop() and never cleared. Both gate startListener and
        # the onStale reconnect so a stale-feed check firing during shutdown
        # can no longer resurrect a listener nothing can reach (the 2026-07-17
        # hang). _listener_lock serializes startListener against stop() and
        # against concurrent reconnects (health check vs onStale), whose
        # interleaved stop/swap used to orphan a running listener.
        self.shutdown_event = shutdown_event if shutdown_event is not None else threading.Event()
        self._stopping = False
        # The waitable form of _stopping, set by signalStop() alongside it.
        # _stopping alone can only be POLLED, so anything sleeping had to wait on
        # shutdown_event and could therefore only be interrupted by a whole-app
        # exit - a per-instance stop (a logout, a listener rebuild, a direct
        # stop()) left the reconnect backoff asleep for up to
        # RECONNECT_MAX_DELAY. App shutdown signals both: it sets shutdown_event
        # and then calls signalStop() on every user (see app.py's shutdown).
        self._stopEvent = threading.Event()
        # Bumped every time a listener is actually installed (startListener's
        # swap). A reconnect backoff captures it on entry and abandons when it
        # no longer matches: a re-login with fresh cookies replaces the listener
        # WITHOUT stopping the instance - it cannot use signalStop, which sets
        # _stopping and is never cleared - so this is what tells a parked
        # backoff that the session it was retrying toward already exists.
        self._listenerGeneration = 0
        self._listener_lock = threading.Lock()

        # Health monitoring: track listener state for graceful degradation
        self._health_lock = threading.RLock()
        self.listener_health = "INITIALIZING"  # INITIALIZING, HEALTHY, DEGRADED, DEAD
        self.listener_last_poll_time = None  # timestamp of last successful poll
        self.listener_error_count = 0  # consecutive errors
        self.listener_last_error = None  # last error message
        # The listener-session ledger for /admin's Worker Health card. One
        # websocket streamer exists per build (see the atexit notes in
        # Database/patches.py), so builds since process start is also the
        # streamer count; a runaway value is the rebuild-churn signal.
        self.listener_session_builds = 0
        self.listener_last_rebuild_time = None    # epoch seconds; None until a REbuild happens
        self.listener_last_rebuild_reason = None  # spotifyListener's STALE_REASON_*, or None

        # All Database instances (one per user) share the same underlying SQLite
        # file - catalog data (tracks/artists/albums/images) is global, so it's
        # stored once regardless of how many users have played a given track.
        self.repo = Repository(dbPath) if dbPath is not None else Repository()
        self.repo.upsertUser(user, email)

        # Raised by importHistoryBatch once a batch actually changed play
        # history; the periodic milestone pass consumes it to re-derive
        # milestone achieved_at dates (see consumeMilestoneRecalcFlag).
        self._milestone_flag_lock = threading.Lock()
        self.milestonesRecalcPending = False

        self.refreshSettings()

        self.autoImportFolderPath = self.baseDir / ".." / "autoImport" / self.user

        # Serializes this user's imports across entry points - the web upload
        # route runs importHistoryBatch on its own thread while AutoImporter
        # runs it on the watchdog thread, with nothing else coordinating them.
        # Concurrent runs interleave their staged transactions and defeat the
        # batch-scoped duplicate reconciliation (_ImportRunState); serialized,
        # a double-submit resolves cleanly instead (the second run sees the
        # first's recorded file hash and skips). RLock: importHistoryBatch
        # calls importHistory, which takes the same lock.
        self._importLock = threading.RLock()

        filterKeyword = os.environ.get(IMPORT_KEYWORD_ENV_VAR)   #< None = import every dropped file
        logger.info("auto import filtering by %s", filterKeyword)
        # importHistoryBatch (not importHistory): files dropped together share
        # one import run state, so a skip/replay pair straddling a file
        # boundary isn't collapsed - and a bad file doesn't abort the rest.
        self.autoImporter = AutoImporter(
            folderPath=self.autoImportFolderPath,
            importCallback=self.importHistoryBatch,
            pollInterval=5,
            keyword=filterKeyword)

        self._initWorkerTelemetry()

        # Serializes every periodic worker's start/stop pair. Their "already
        # running?" check and the thread/event assignment that follows it are
        # not atomic on their own, and the profile page's key save starts
        # workers from a request thread: two concurrent saves (a double-clicked
        # form) could both pass the check, and the second's assignments would
        # orphan the first's thread on an event nothing holds a reference to
        # any more - unstoppable until process exit. See PeriodicWorkerMixin.
        self._worker_lock = threading.Lock()

        self.backfiller_thread = None
        self.backfiller_stop_event = threading.Event()

        self.wrapped_thread = None
        self.wrapped_stop_event = threading.Event()
        # Guards the lazily-created per-year locks below (not the recalculation
        # itself) so the periodic worker and an on-demand /wrapped recalculation
        # never both run _calculateAndSaveWrapped for the same year at once.
        self._wrapped_recalc_locks_guard = threading.Lock()
        self._wrapped_recalc_locks: dict[int, threading.Lock] = {}

        self.lastfm_thread = None
        self.lastfm_stop_event = threading.Event()

        self.lastfm_biography_thread = None
        self.lastfm_biography_stop_event = threading.Event()

        self.lastfm_album_biography_thread = None
        self.lastfm_album_biography_stop_event = threading.Event()

        if startWorkers:
            self.startBackgroundWorkers()

    def startBackgroundWorkers(self) -> None:
        """The five always-on periodic workers a default construction spawns.

        Split out of __init__ so an instance built with startWorkers=False can
        be promoted to a fully active one later: _getReadOnlyUserDb constructs
        read-only Databases for public share-link views (an anonymous GET must
        not put the owner's stored credentials on a polling loop), and
        get_user_db() activates that same cached instance in place on the
        owner's next real login. One list here, so a future sixth worker can't
        end up started on construction but skipped on promotion.

        Safe to call on an already-active instance - each start no-ops when
        its worker is running (and refuses outright once a stop has been
        requested; see PeriodicWorkerMixin). The Last.fm three additionally
        no-op without a stored API key; the profile page's key save re-invokes
        them once a key lands."""
        self.startMetadataBackfiller()
        self.startWrappedCalculationsWorker()
        self.startLastfmGenreBackfiller()
        self.startLastfmBiographyBackfiller()
        self.startLastfmAlbumBiographyBackfiller()

    def consumeMilestoneRecalcFlag(self) -> bool:
        """One-shot read of the "an import just changed play history" marker
        raised by importHistoryBatch: the periodic milestone pass
        (_detectMilestonesSafely in app.py) consumes it - on a pass with no
        import still in flight - to re-derive this user's milestone
        achieved_at dates and prune rows the rewritten history no longer
        supports, right after detection has recorded any import-crossed rows.
        Deliberately in-memory only - a restart drops it, which self-heals for
        dates because the next pass that records a crossing also triggers a
        re-derivation (pruning waits for the next import's flag).

        The read and the clear are one step under a lock. They used to be two
        statements, so an import raising the flag between them had its raise
        erased - and that import's pruning of milestones its rewritten history
        no longer supports never ran (the loss the old "worst case is one extra
        recalculation" note missed: the risk is a DROPPED flag, not a duplicate
        one)."""
        with self._milestone_flag_lock:
            pending = self.milestonesRecalcPending
            self.milestonesRecalcPending = False
        return pending

    def raiseMilestoneRecalcFlag(self) -> None:
        """Mark that an import changed play history - see consumeMilestoneRecalcFlag."""
        with self._milestone_flag_lock:
            self.milestonesRecalcPending = True

    def refreshSettings(self) -> None:
        from zoneinfo import ZoneInfo
        import Database.utils as utils
        try:
            self.settings = self.repo.getUserSettings(self.user)
            tz_name = self.settings.get("timezone")
            self.tz = ZoneInfo(tz_name) if tz_name else utils.getTimezone()
        except Exception:
            self.tz = utils.getTimezone()

    def _materializeCookiesFile(self) -> Path:
        """spotapi only knows how to read a Spotify session from a file
        path (spotapi.saver.JSONSaver), not from a dict - write this user's
        cookies (the database is the source of truth) to a short-lived temp file
        in the same [{"identifier", "cookies"}, ...] shape Database.Spotify.cookies.saveSession
        produces. The caller is responsible for deleting it once the client
        holding it has been constructed - it's only read at construction time."""
        cookies = self.repo.getUserCookies(self.user) or {}
        email = self.repo.getEmailForUsername(self.user) or self.email
        tmpFd, tmpPath = tempfile.mkstemp(prefix=f"cookies_{self.user}_", suffix=".json")
        os.close(tmpFd)
        tmpPath = Path(tmpPath)
        payload = [{"identifier": email, "cookies": cookies}]
        tmpPath.write_text(json.dumps(payload), encoding="utf-8")
        #< flaskDebugEnabled(), not a bare os.environ.get: this was the one site
        #  that tested the string for truthiness rather than for a truthy VALUE,
        #  so FLASK_DEBUG=0 - which turns every other diagnostic off - switched
        #  this one on
        if flaskDebugEnabled():
            logger.debug(
                "Materialized cookies file for user=%s: path=%s, identifier=%s, has_cookies=%s",
                self.user, tmpPath, email, bool(cookies)
            )
        return tmpPath

    def _withCookiesFile(self, factory):
        """Call `factory(cookiesFilePath)` using either an explicitly-provided
        self.cookiesFile (manual/dev usage, e.g. this module's __main__ block) or
        a temp file materialized from this user's cookies in the database (the
        normal app path, where Database is constructed without a cookiesFile)."""
        if self.cookiesFile:
            return factory(self.cookiesFile)
        tmpPath = self._materializeCookiesFile()
        try:
            return factory(str(tmpPath))
        finally:
            # Swallowed on purpose: an exception raised in a `finally` replaces
            # whatever the block was doing - a RETURN included. This file is
            # written on every import and every Spotify client build, and on
            # Windows the moment a JSON has just been written is exactly when a
            # scanner may still hold it - so a blocked delete would turn a
            # successful login into a PermissionError, and a failed one into a
            # misdiagnosed PermissionError.
            #
            # Giving up is acceptable because of WHAT is left behind, not
            # because something else collects it: this file holds the user's
            # Spotify cookies in plaintext, and mkstemp created it readable and
            # writable by this user alone. A leftover is one owner-only file in
            # the temp dir, which is the same protection secrets/ gets - so it
            # is logged rather than retried.
            try:
                tmpPath.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Could not remove the temporary cookies file %s: %s", tmpPath, parseError(e))

    @staticmethod
    def _splitEntryAndTrack(metadata: dict) -> tuple[dict, dict]:
        entry = {
            "id": metadata["id"],
            "playedAt": metadata["playedAt"],   #< the play's own fields...
            "timePlayed": metadata["timePlayed"],
            "playedFrom": metadata.get("playedFrom"),
            # ...plus importer-decided routing/enrichment info (absent on listener metas)
            "isSkip": metadata.get("isSkip", False),
            "importExtras": metadata.get("importExtras"),
        }
        track = {k: v for k, v in metadata.items()
                 if k not in ("playedAt", "timePlayed", "playedFrom", "isSkip", "importExtras")}
        return entry, track

    @staticmethod
    def _mergeEntryWithTrack(entry: dict, track: dict) -> dict:
        meta = track.copy()   #< the catalog row stays untouched
        meta["playedAt"] = entry["playedAt"]
        meta["timePlayed"] = entry["timePlayed"]   #< the play's own fields win
        meta["playedFrom"] = entry.get("playedFrom")
        meta["extras"] = entry.get("extras")   #< behavioral columns, when the read carried them
        # The play's own classification, not the track's: the song detail
        # timeline labels each entry Full/Partial/Skipped from this (see
        # DashboardViewModels._enrichSongTimelineEntries). Dropping it here made
        # every skip on the timeline read as "Partial - N%", because the
        # fallback for a missing flag is "not a skip".
        meta["isSkip"] = entry.get("isSkip", False)
        return meta

    def _paginateEntries(self, entries: list) -> list:
        """Merge each play entry with its track's catalog metadata. Track
        metadata for every distinct id in `entries` is fetched in one batched
        round-trip (Repository.getTracksByIds) rather than once per entry -
        hydrating a page of history used to cost 3 queries per play.

        A play whose track is missing is dropped and reported. plays.track_id
        has an enforced foreign key into tracks.id, so a play can never be
        written without its track: a missing one means the track row was
        deleted afterwards - the dangling-row corruption the startup probe
        counts and migrate1_43_0 repairs.

        Rendering is not the place to fix that. This used to call the listener
        live, which meant a Spotify round-trip with up to five seconds of
        time.sleep in its retry ladders, plus an upsertTrack and a commit, on
        the request thread as a side effect of a GET - and it repeated on every
        render, since nothing caches the failure. It also usually failed, and
        the entry was dropped anyway."""
        trackIds = list({entry["id"] for entry in entries})
        tracksById = self.repo.getTracksByIds(trackIds)

        result = []
        missingTrackIds = []
        for entry in entries:
            track = tracksById.get(entry["id"])
            if track is None:
                missingTrackIds.append(entry["id"])
                continue
            result.append(self._mergeEntryWithTrack(entry, track))
        if missingTrackIds:
            # Once per page rather than per row: a corrupt track id usually has
            # several plays, and the count is the interesting part.
            logger.warning(
                "Dropped %d play(s) from this page: no catalog row for track id(s) %s. "
                "Run the migration/integrity probe - plays.track_id is a foreign key, so these "
                "are dangling rows, not a missing fetch.",
                len(missingTrackIds), ", ".join(sorted(set(missingTrackIds))),
            )
        return result

    def hydrateEntries(self, entries: list) -> list:
        """Merge raw play rows with their tracks' catalog metadata - the public
        name for _paginateEntries, for callers that must fetch raw rows first
        and hydrate separately. The streaming export needs the split so its
        keyset pager can measure chunks off the raw rows: a dangling play this
        drops must lose only itself, never mark a chunk as the last one."""
        return self._paginateEntries(entries)

    @staticmethod
    def _splitContextUri(contextUri: str) -> tuple[str, str] | None:
        """('type', 'id') from a playedFrom value like "playlist:xyz"/"album:xyz",
        or None if malformed. playedFrom is only ever written in that shape (see
        spotifyClient.formatTrack), so a colon-less value means corrupt data -
        degrade to "no known context" instead of a ValueError that would 500 the
        history page."""
        parts = contextUri.split(":", 1)
        if len(parts) != 2:
            logger.warning("Malformed playedFrom context %r - expected 'type:id'", contextUri)
            return None
        return parts[0], parts[1]

    def playlistName(self, playlistUri: str | None) -> str | None:
        """Return the playlist name for a Spotify playlist URI or id.

        Memoized for the current request only (the docstring used to claim
        caching that wasn't there): the history and detail play lists call this
        once per row, and a page of plays from one listening session mostly
        shares a handful of contexts. Request-scoped rather than instance-wide
        so a renamed playlist still shows up on the next page load, and so
        background workers - which have no app context - keep reading through."""
        if not playlistUri:
            return None
        parsed = self._splitContextUri(playlistUri)
        if parsed is None:
            return None
        contextType, playlistId = parsed

        from flask import has_app_context, g
        cache = None
        if has_app_context():
            cache = g.setdefault("_playlistNameCache", {})
            if playlistId in cache:
                return cache[playlistId]

        name = self.repo.getPlaylistName(playlistId, contextType)
        if cache is not None:
            cache[playlistId] = name
        return name

    def updatePlaylists(self, playlist: str | None) -> None:
        if playlist is None:
            return   #< a play with no known source has no playlist to record
        parsed = self._splitContextUri(playlist)
        if parsed is None:
            return
        contextType, playlistId = parsed
        if self.repo.playlistKnown(playlistId, contextType):
            return
        try:
            if contextType == "album":
                name = self.listener.albumName(playlistId)
            else:
                name = self.listener.playlistName(playlistId)
        except Exception as e:
            logger.warning(
                "Error occurred while fetching playlist name for %s (probably due to playlist being private): %s",
                playlistId, e,
            )
            #< a row written here is FINAL: playlistKnown tests only that one
            #  exists, and nothing else ever writes this table. So a failure
            #  only earns a row if it is an answer. A private or deleted
            #  playlist raises every time, and re-asking would spend a Spotify
            #  call on every play from it - that one is cached, exactly as
            #  before. A transient failure is not: the shared limiter's backoff
            #  window is opened by ANY user's 429, so it can land on the first
            #  play from a playlist through no fault of its own, and the
            #  playlist would then have no name in "played from" forever and be
            #  unfindable by /history's search. Left unwritten, the next play
            #  from it asks again.
            #  Only what classifyListenerError already calls transient (429 /
            #  rate limit / malformed JSON) is retried - sharpening that
            #  heuristic is its own deferred phase, and widening it here would
            #  fork the definition.
            if classifyListenerError(e)[1]:
                return
            name = None
        self.repo.upsertPlaylistName(playlistId, contextType, name)

    # ---- history / entries ----------------------------------------------------------

    def getEntriesCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         trackId: str | None = None, artistId: str | None = None,
                         albumId: str | None = None, includeSkips: bool = False,
                         trackIds: list[str] | None = None, fullPlaysOnly: bool = False) -> int:
        """Return total number of entries in the database, optionally scoped
        to [startDate, endDate) - see getEntriesFromNew's identical params."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getPlaysCount(self.user, startTs=startTs, endTs=endTs,
                                        trackId=trackId, artistId=artistId, albumId=albumId,
                                        includeSkips=includeSkips, trackIds=trackIds,
                                        fullPlaysOnly=fullPlaysOnly)

    def getEntriesFromNew(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True,
                           startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                           trackId: str | None = None, artistId: str | None = None,
                           albumId: str | None = None, includeSkips: bool = False,
                           trackIds: list[str] | None = None, fullPlaysOnly: bool = False) -> list:
        """ Return the latest `count` entries from history, sorted from newest to oldest. If count is None, return all entries.
        startDate/endDate optionally scope this to a half-open [startDate, endDate) range - used by the Dashboard's
        chart click-through (see app.py's dashboard()), not by its default unscoped view.
        `trackId`/`artistId`/`albumId` narrow this to one item's plays - the
        detail pages' play-history lists, same filters as getListeningTimeSeries.
        `trackIds` narrows to an explicit set of track ids - the /history page's
        tag filter (see routes/charts.py's historyPage), mirrors getSongsPage's
        identical param.
        `fullPlaysOnly` keeps only plays that reached the admin's completion
        percent - the /history page's "Full plays only" checkbox. It is a
        COMPLETION test, not a skip test: see Repository.getPlaysCount."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        entries = self.repo.getPlaysNewestFirst(self.user, count=count, startIndex=startIndex, startTs=startTs, endTs=endTs,
                                                 trackId=trackId, artistId=artistId, albumId=albumId,
                                                 includeSkips=includeSkips, trackIds=trackIds,
                                                 fullPlaysOnly=fullPlaysOnly)
        return self._paginateEntries(entries) if fullPagination else entries

    def getEntriesFromOld(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True,
                           startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                           trackId: str | None = None, artistId: str | None = None,
                           albumId: str | None = None, includeSkips: bool = False,
                           afterTs: float | None = None, afterId: int | None = None,
                           trackIds: list[str] | None = None,
                           fullPlaysOnly: bool = False) -> list:
        """ Return the oldest `count` entries from history, sorted from oldest to newest. If count is None, return all entries.
        startDate/endDate and trackId/artistId/albumId: see getEntriesFromNew's identical params.
        afterTs/afterId: see Repository.getPlaysOldestFirst. trackIds/fullPlaysOnly: see
        getEntriesFromNew's identical params."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        entries = self.repo.getPlaysOldestFirst(self.user, count=count, startIndex=startIndex, startTs=startTs, endTs=endTs,
                                                 trackId=trackId, artistId=artistId, albumId=albumId,
                                                 includeSkips=includeSkips, afterTs=afterTs, afterId=afterId,
                                                 trackIds=trackIds, fullPlaysOnly=fullPlaysOnly)
        return self._paginateEntries(entries) if fullPagination else entries

    def getSkipEntriesFromOld(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True,
                               afterTs: float | None = None, afterId: int | None = None) -> list:
        """Skip events (plays.is_skip=1) oldest first, hydrated like plays - the
        JSON export's trailing section, so skips round-trip between
        instances (they re-import as sub-threshold entries). afterTs/afterId:
        see Repository.getPlaysOldestFirst."""
        entries = self.repo.getSkipsOldestFirst(self.user, count=count, startIndex=startIndex,
                                                 afterTs=afterTs, afterId=afterId)
        return self._paginateEntries(entries) if fullPagination else entries

    def searchEntries(self, query: str, count: int | None = None, startIndex: int = 0,
                       startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                       oldestFirst: bool = False, trackIds: list[str] | None = None,
                       includeSkips: bool = False, fullPlaysOnly: bool = False) -> list:
        """Entries (newest first, or oldest first with `oldestFirst`) whose
        track/artist/album/playlist matches `query`, paginated in SQL
        (Repository.searchPlays) rather than filtering the whole history in
        Python. startDate/endDate: see getEntriesFromNew's identical param.
        trackIds/includeSkips/fullPlaysOnly: see getEntriesFromNew's identical
        params - the search box has to filter like the list beside it."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        entries = self.repo.searchPlays(self.user, query, limit=count, offset=startIndex, startTs=startTs, endTs=endTs,
                                         oldestFirst=oldestFirst, trackIds=trackIds,
                                         includeSkips=includeSkips, fullPlaysOnly=fullPlaysOnly)
        return self._paginateEntries(entries)

    def searchEntriesCount(self, query: str, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                            trackIds: list[str] | None = None,
                            includeSkips: bool = False, fullPlaysOnly: bool = False) -> int:
        """The paging counterpart to searchEntries() - total matching entries,
        for computing total page count without fetching every match. trackIds/
        includeSkips/fullPlaysOnly: see getEntriesFromNew's identical params,
        and they must match what searchEntries() was given or the pager
        disagrees with the list."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.searchPlaysCount(self.user, query, startTs=startTs, endTs=endTs, trackIds=trackIds,
                                           includeSkips=includeSkips, fullPlaysOnly=fullPlaysOnly)

    def writeProgress(self, status: str, current: int = 0, total: int = 0, message: str = "", error: bool = False) -> None:
        self.repo.writeProgress(self.user, status, current, total, message, error)

    def tryClaimImportRunning(self) -> bool:
        return self.repo.tryClaimImportRunning(self.user)

    def readProgress(self) -> dict:
        """The stored import progress, or an idle placeholder for a user who
        never imported."""
        progress = self.repo.readProgress(self.user)
        if progress is None:
            return {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return progress

    def resetProgress(self) -> None:
        self.writeProgress("idle", 0, 0, "", False)

    # A play exactly at a year boundary's midnight belongs to the NEXT year -
    # covered-year delete segments stop this far short of the boundary so it
    # only goes when its own year is covered.
    YEAR_SEGMENT_BOUNDARY_EPSILON_SECONDS = 0.001

    # ---- stats -------------------------------------------------------------------------

    @staticmethod
    def _dateRangeToTimestamps(startDate: datetime.datetime | None, endDate: datetime.datetime | None):
        startTs = startDate.timestamp() if startDate else None
        endTs = endDate.timestamp() if endDate else None
        return startTs, endTs

    def getEntitiesPlayedInRange(self, kind: str, ids: list[str],
                                  startDate: datetime.datetime = None,
                                  endDate: datetime.datetime = None,
                                  fullPlaysOnly: bool = False) -> list[str]:
        """Which of `ids` (tracks, artists or albums - see `kind`) this user
        played at all in the range. Existence only; the Top lists' rank-movement
        badge uses it to tell "not played in the previous period" apart from
        "played below the depth we ranked". See
        Repository.getEntitiesPlayedInRange for why it is not the ranking query
        with an id filter."""
        return self.repo.getEntitiesPlayedInRange(
            self.user, kind, ids, *self._dateRangeToTimestamps(startDate, endDate),
            fullPlaysOnly=fullPlaysOnly)

    def getExplicitRatio(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        """{explicit, clean} play counts for the Charts explicit ratio."""
        return self.repo.getExplicitCounts(self.user, *self._dateRangeToTimestamps(startDate, endDate))

    def getReleaseDecadeDistribution(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        """{"1990s": plays, ...} for the Charts release-era chart. The label
        shape is this layer's business; the counting is the repository's."""
        rows = self.repo.getReleaseDecadeCounts(self.user, *self._dateRangeToTimestamps(startDate, endDate))
        return {f"{row['decade']}s": row["count"] for row in rows}

    def _resolveIncludeInherited(self, includeInherited: bool | None) -> int:
        """None means "whatever the admin's instance-wide toggle says" - the
        default for every genre stat, so flipping the toggle changes charts/
        wrapped/compare/coverage everywhere without touching callers."""
        if includeInherited is None:
            includeInherited = self.repo.isInheritedGenresEnabled()
        return 1 if includeInherited else 0

    def getGenreCoverage(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         includeInherited: bool | None = None) -> dict:
        """Play-weighted Last.fm genre coverage over a date range: for each
        category, the share of this user's plays whose song/album/primary
        artist carries at least one genre row. All three categories share the
        same denominator (every play has exactly one track, album and primary
        artist - a play whose track lacks a position-0 artist row just never
        counts as artist-covered). "overall" is the mean of the three - the
        unlock gate for genre features compares against it. Each category
        also reports "ownPercent": the share covered by own (non-inherited)
        tags regardless of the toggle, so the coverage panel can show how
        much of the number rests on inheritance (equal to percent for
        artists, which have no inherited concept)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        row = self.repo.getGenreCoverageCounts(self.user, inherited, startTs, endTs)
        total = row["total"]

        def category(covered: int, ownCovered: int) -> dict:
            def percentOf(value: int) -> float:
                return round(value / total * 100, 1) if total else 0.0
            return {"covered": covered, "total": total,
                    "percent": percentOf(covered), "ownPercent": percentOf(ownCovered)}

        ownByCategory = {"song": row["song_own"], "album": row["album_own"],
                         "artist": row["artist_covered"]}
        coverage = {name: category(row[f"{name}_covered"], ownByCategory[name])
                    for name in GENRE_COVERAGE_CATEGORIES}
        coveredSum = sum(row[f"{name}_covered"] for name in GENRE_COVERAGE_CATEGORIES)
        overallPercent = (round(coveredSum / (len(GENRE_COVERAGE_CATEGORIES) * total) * 100, 1)
                          if total else 0.0)
        coverage["overall"] = {"percent": overallPercent}
        return coverage

    def getGenreDistribution(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                             limit: int | None = None, includeInherited: bool | None = None) -> dict:
        """{genre: plays} over the range, most-played first (name breaks ties -
        Last.fm counts tie constantly). A play with N genres counts once per
        genre, the standard reading for tag distributions. Track-level genres
        only: they're the finest granularity, and inherited rows already carry
        artist genres down to tag-less tracks when the toggle allows."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        rows = self.repo.getGenrePlayCounts(self.user, inherited, startTs, endTs, limit=limit)
        return {row["genre"]: row["plays"] for row in rows}

    def getGenreTrends(self, genres: list[str], startDate: datetime.datetime = None,
                       endDate: datetime.datetime = None, includeInherited: bool | None = None,
                       groupBy: str = "month") -> dict:
        """Plays per local-time bucket per genre, in the {"buckets", "series"}
        shape the multi-line trend chart consumes (same shape as getArtistTrend).
        `groupBy` sizes the buckets - day/week/month, or hour for single-day
        views - with the same keys _bucketKey gives every other trend chart
        (all lexically sortable, so the sorted union below stays
        chronological). Default month preserves the pre-Trend-buckets
        behavior. `buckets` is the sorted union of buckets in which any of the
        requested genres has a play; each series' `data` aligns to it (0 where
        that genre had no play in that bucket). Requested-genre order is
        preserved; genres with no plays at all are dropped. Empty input or no
        plays -> {"buckets": [], "series": []}."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        rows = self.repo.getBucketedGenrePlayCounts(self.user, genres, inherited, startTs, endTs)

        counts: dict = {}  # genre -> {bucket: count}
        bucketKeys: set = set()
        # bucketRowsByKey memoizes the local-timezone conversion + bucket-key
        # mapping per distinct bucketStartTs rather than recomputing it per
        # row - many rows share one (one per genre active in it) - the same
        # reason getArtistTrend uses it.
        for bucket, row in bucketRowsByKey(rows, self.tz, groupBy):
            bucketKeys.add(bucket)
            genreBuckets = counts.setdefault(row["genre"], {})
            genreBuckets[bucket] = genreBuckets.get(bucket, 0) + row["plays"]

        if not bucketKeys:
            return {"buckets": [], "series": []}

        buckets = sorted(bucketKeys)
        series = [
            {"name": genre, "data": [counts[genre].get(bucket, 0) for bucket in buckets]}
            for genre in genres if genre in counts
        ]
        return {"buckets": buckets, "series": series}

    def getGenreStats(self, genre: str, startDate: datetime.datetime = None,
                      endDate: datetime.datetime = None, includeInherited: bool | None = None) -> dict:
        """{plays, listenMs, firstPlayedTs, sharePercent} for one genre.
        sharePercent is this genre's plays as a share of all genre-tagged
        plays in range (0.0 when the user has none)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        stats = self.repo.getGenrePlayStats(self.user, genre, inherited, startTs, endTs)
        total = self.repo.getTotalGenreTaggedPlays(self.user, inherited, startTs, endTs)
        stats["sharePercent"] = round(stats["plays"] / total * 100, 1) if total else 0.0
        return stats

    def getTopArtistsForGenre(self, genre: str, limit: int, startDate: datetime.datetime = None,
                              endDate: datetime.datetime = None, includeInherited: bool | None = None) -> list[dict]:
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        return self.repo.getTopArtistsForGenre(self.user, genre, inherited, limit, startTs, endTs)

    def getTopTracksForGenre(self, genre: str, limit: int, startDate: datetime.datetime = None,
                             endDate: datetime.datetime = None, includeInherited: bool | None = None) -> list[dict]:
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        return self.repo.getTopTracksForGenre(self.user, genre, inherited, limit, startTs, endTs)

    def getGenreArtistCounts(self, genres: list[str], includeInherited: bool | None = None) -> dict:
        """{genre: distinct artist count} for the given genres - the breadth
        view that pairs with the play-weighted share donut on the Genres page."""
        inherited = self._resolveIncludeInherited(includeInherited)
        return self.repo.getArtistCountsByGenres(self.user, genres, inherited)

    def getGenreHourOfDayHeatmap(self, genre: str, startDate: datetime.datetime = None,
                                 endDate: datetime.datetime = None, includeInherited: bool | None = None) -> list:
        """7x24 grid (Monday=0..Sunday=6 x hour 0-23) of listening time/plays
        for one genre - the per-genre "listening clock". Genre-scoped analogue
        of getHourOfDayHeatmap; same local weekday/hour mapping in Python."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        inherited = self._resolveIncludeInherited(includeInherited)
        rows = self.repo.getGenreBucketedPlayTotals(self.user, genre, inherited, startTs, endTs)
        return fillHeatmapGrid(rows, self.tz)

    def getRecommendedArtists(self, limit: int, genrePool: int, excludeTopN: int) -> list[dict]:
        """"Discover" recommendations: under-played artists already in the
        user's library whose genres overlap the user's top `genrePool` genres,
        excluding the user's `excludeTopN` most-played artists. Returns [] when
        the user has no genre data at all. Callers gate this behind the genre
        coverage unlock (see genre_gate) so it only surfaces once the tag data
        is dense enough to be meaningful."""
        topGenres = list(self.getGenreDistribution(limit=genrePool).keys())
        if not topGenres:
            return []
        topArtistIds = [artist["id"] for artist in self.getTopArtists(by="plays", limit=excludeTopN)]
        return self.repo.getArtistsByGenres(self.user, topGenres, topArtistIds, limit)

    def _genreNames(self, rows, includeInherited) -> list[str]:
        """Genre names from repo genre rows, honouring the inherited-genre toggle
        (None = read the admin's instance-wide setting).

        Shared by the four track/album accessors below because the RULE is what
        repeats, not just the shape: "drop inherited rows unless the setting says
        otherwise" decides what a page displays, and four copies is four places
        to get it backwards. Artists have no inherited concept (nothing to
        inherit FROM), so they don't come through here."""
        inherited = self._resolveIncludeInherited(includeInherited)
        return [row["genre"] for row in rows if inherited or not row["inherited"]]

    def getGenresForTrack(self, trackId: str, includeInherited: bool | None = None) -> list[str]:
        """This track's own genre names, position-ordered - the track-card
        badge's data source."""
        return self._genreNames(self.repo.getTrackGenres(trackId), includeInherited)

    def getGenresForAlbum(self, albumId: str, includeInherited: bool | None = None) -> list[str]:
        """This album's own genre names, position-ordered - see
        getGenresForTrack."""
        return self._genreNames(self.repo.getAlbumGenres(albumId), includeInherited)

    def getGenresForArtist(self, artistId: str) -> list[str]:
        """This artist's own genre names, position-ordered. Artists have no
        inherited concept (nothing to inherit FROM), so no toggle here."""
        return self.repo.getArtistGenres(artistId)

    def getGenresForTracks(self, trackIds: list[str],
                            includeInherited: bool | None = None) -> dict[str, list[str]]:
        """Batched getGenresForTrack for a page of cards: {trackId: [genre, ...]}
        in two queries total (the inherited-genre setting once, the genre rows
        once) instead of two per card. Ids with no genres map to []."""
        return self._genreNamesByIds(self.repo.getTrackGenresForIds(trackIds), includeInherited)

    def getGenresForAlbums(self, albumIds: list[str],
                            includeInherited: bool | None = None) -> dict[str, list[str]]:
        """Batched getGenresForAlbum - see getGenresForTracks."""
        return self._genreNamesByIds(self.repo.getAlbumGenresForIds(albumIds), includeInherited)

    def _genreNamesByIds(self, rowsById: dict, includeInherited) -> dict[str, list[str]]:
        """_genreNames over a batched {id: rows} mapping. Resolves the
        inherited-genre setting ONCE for the whole page, which is the point of the
        batched queries above."""
        inherited = self._resolveIncludeInherited(includeInherited)
        return {entityId: [row["genre"] for row in rows if inherited or not row["inherited"]]
                for entityId, rows in rowsById.items()}

    def getGenresForArtists(self, artistIds: list[str]) -> dict[str, list[str]]:
        """Batched getGenresForArtist - one query, no inherited toggle."""
        return self.repo.getArtistGenresForIds(artistIds)

    def getSkipStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                      trackId: str | None = None, artistId: str | None = None,
                      albumId: str | None = None) -> dict:
        """{plays, skips, skipPercent} for one entity (or everything) in range.

        skipPercent is the share of ENCOUNTERS that were skips, not of plays:
        "you skipped this 3 times out of 10 times it came up" is the question a
        detail page is answering. 0 encounters -> 0.0 rather than a division
        error."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        stats = self.repo.getSkipStats(self.user, startTs, endTs,
                                        trackId=trackId, artistId=artistId, albumId=albumId)
        encounters = stats["plays"] + stats["skips"]
        return {
            **stats,
            "skipPercent": round(stats["skips"] / encounters * 100, 1) if encounters else 0.0,
        }

    #< The Top pages' sortBy value that routes to the skip-ordered path below
    #  instead of the normal aggregates.
    SKIP_SORT_BY = "skips"

    def _skipSortedPage(self, fetch, startDate, endDate, limit, offset, **filters):
        """Shared plumbing for a skip-ranked Top page.

        Same ranking as the Charts lists - skip rate, shrunk toward the
        library average - so "most skipped" means the same thing everywhere.
        Ranking by raw count would just list whatever is played most, and for
        albums that is literally longest-first, since a longer record offers
        more chances to skip.

        `filters` are the page's search/tag/entity/full-plays narrowing, passed
        straight through: this is a different sort of the same page, not a
        different page, so every filter beside the sort control still applies."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return fetch(self.user, startTs, endTs,
                     limit=(limit if limit is not None else -1), offset=offset,
                     priorWeight=SKIP_RATE_PRIOR_WEIGHT, **filters)

    @staticmethod
    def _withSkipRate(row: dict) -> dict:
        encounters = row.get("encounters") or 0
        return {**row, "skipPercent": round(row["skips"] / encounters * 100, 1) if encounters else 0.0}

    @staticmethod
    def _byDisplayedSkipRate(rows: list[dict]) -> list[dict]:
        """Order a Charts skip list by the number it actually plots.

        The rows arrive ranked by the shrunk rate, which is what should decide
        WHICH of them make the top-N cut - but the bar drawn for each is the
        true percentage, and a chart whose bars ran 100%, 75%, 90% reads as
        unsorted however defensible the underlying ranking is. Reordering the
        selected rows costs nothing (a top-N list, already in memory) and does
        not touch the selection.

        Only the fixed top-N Charts lists do this. The paginated Top pages keep
        the shrunk order end to end: their sort has to agree with the offsets
        it is paged by, and re-sorting one page of it would reorder rows within
        a page while the page boundaries stayed where the other ranking put
        them.

        sorted() is stable, so rows showing the same percentage keep the
        shrunk-rate order that selected them - the tiebreak stays with the
        better-evidenced row rather than becoming arbitrary."""
        return sorted(rows, key=lambda row: row["skipPercent"], reverse=True)

    def getMostSkippedSongs(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                             limit: int = 10, priorWeight: int = SKIP_RATE_PRIOR_WEIGHT) -> list[dict]:
        """Highest skip-rate songs, hydrated with track metadata - the Charts
        page's "Most skipped" list. Selected by shrunk rate (see
        Repository.getMostSkippedTracks), then returned in descending order of
        the true percentage each row reports - see _byDisplayedSkipRate."""
        rows = self.repo.getMostSkippedTracks(self.user, *self._dateRangeToTimestamps(startDate, endDate),
                                               limit=limit, priorWeight=priorWeight)
        if not rows:
            return []
        tracksById = self.repo.getTracksByIds([row["track_id"] for row in rows])
        skipped = []
        for row in rows:
            track = tracksById.get(row["track_id"])
            if track is None:
                # Shouldn't happen: plays.track_id is NOT NULL REFERENCES
                # tracks(id) and foreign_keys=ON, so a play whose track is gone
                # can't be written. Kept only for rows predating that
                # enforcement - dropping one shortens this list, which is
                # harmless for a fixed top-N but silently shortens a PAGE on
                # the paginated caller below, so it must stay unreachable.
                continue
            skipped.append({
                **track,
                "skips": row["skips"],
                "plays": row["plays"],
                "encounters": row["encounters"],
                "skipPercent": round(row["skips"] / row["encounters"] * 100, 1),
            })
        return self._byDisplayedSkipRate(skipped)

    def getMostSkippedArtists(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                               limit: int = 10, priorWeight: int = SKIP_RATE_PRIOR_WEIGHT) -> list[dict]:
        """Highest skip-rate artists - see getMostSkippedSongs. Names come from
        the aggregate itself, so no second hydration query is needed."""
        rows = self.repo.getMostSkippedArtists(self.user, *self._dateRangeToTimestamps(startDate, endDate),
                                                limit=limit, priorWeight=priorWeight)
        return self._byDisplayedSkipRate([{
            "id": row["id"],
            "name": row["name"],
            "skips": row["skips"],
            "plays": row["plays"],
            "encounters": row["encounters"],
            "skipPercent": round(row["skips"] / row["encounters"] * 100, 1),
        } for row in rows])

    def getCompletionStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        """Skip/complete/partial breakdown for the Charts pie chart and the
        Compare page's Skip Rate. A skip is any play with is_skip=1 - the single
        admin-tunable skip threshold, materialized per row (it replaced both the
        old 30s line and the separate play_skips table). Among real plays
        (is_skip=0), a listen at/over the admin-set complete percent of the
        track's duration counts as complete (unknown <=0 durations count as
        complete since partial can't be told apart), else partial."""
        return self.repo.getCompletionCounts(self.user, *self._dateRangeToTimestamps(startDate, endDate))

    def getListeningBehavior(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        """Raw material for the Charts page's Listening Behavior card - see
        Repository.getBehavioralCounts for the shape and
        services.listening_behavior.buildListeningBehavior for what turns it
        into the card's labelled/bucketed dict. Un-hydrated on purpose, like
        getCompletionStats above: this facade method stays a plain pass-
        through so the maths lives in one pure, DB-free place."""
        return self.repo.getBehavioralCounts(self.user, *self._dateRangeToTimestamps(startDate, endDate))

    def getSongsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                       sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                       trackId: str | None = None, artistId: str | None = None,
                       albumId: str | None = None, searchQuery: str | None = None,
                       trackIds: list[str] | None = None, fullPlaysOnly: bool = False,
                       firstListenedStart: datetime.datetime = None,
                       firstListenedEnd: datetime.datetime = None) -> list:
        """Return songs sorted by `sortBy` with full song metadata and listen
        totals - sorted/paged in SQL via a single batched query (see
        Repository.getSongsPage) rather than hydrating every song ever played
        just to discard all but the requested page. `trackId`/`artistId`/
        `albumId` narrow this to a single song's stats, an artist's songs, or an
        album's songs (see Repository.getSongsPage). `trackIds` narrows to an
        explicit set of track ids (the tag-filtered Top Songs page). `searchQuery`
        narrows to songs whose name, artist(s), or album match. `fullPlaysOnly`
        excludes plays that never reached the completion-complete percent of
        the track's duration - defaults False so only the Top Songs page opts in.
        `firstListenedStart`/`firstListenedEnd` keep only songs whose first
        listen falls in that half-open window while counts stay lifetime - the
        Wrapped discovery lists (see Repository._firstListenClause). "First"
        and "lifetime" mean over the plays this query SEES, so they mean
        all-time only when no startDate/endDate narrows it too; the Wrapped
        caller passes the window alone. Not supported by the skip-sort branch,
        whose query has no discovery caller."""
        if sortBy == self.SKIP_SORT_BY:
            # Its own query rather than a skips column on getSongsPage: that
            # query's is_skip=0 predicate is load-bearing for its plan (an
            # is_skip partial index measured a 2x regression there), and
            # rewriting its aggregates as conditional sums to carry skips would
            # put that at risk for a number most of its callers never ask for.
            rows = self._skipSortedPage(
                self.repo.getMostSkippedTracks, startDate, endDate, limit, offset,
                trackId=trackId, artistId=artistId, albumId=albumId, searchQuery=searchQuery,
                trackIds=trackIds, fullPlaysOnly=fullPlaysOnly)
            tracksById = self.repo.getTracksByIds([row["track_id"] for row in rows])
            # Spelled out rather than spreading the aggregate row: the card
            # reads camelCase, and a bare spread would also leak the query's
            # snake_case aliases into the template context. The `in tracksById`
            # guard mirrors getMostSkippedSongs' - see the note there on why it
            # is unreachable, and why it had better stay that way here: this
            # list is a page, and getSkippedTracksCount already counted the row.
            return [
                self._withSkipRate({
                    **tracksById[row["track_id"]],
                    "plays": row["plays"], "skips": row["skips"], "encounters": row["encounters"],
                    "totalTimeListened": row["total_time_listened"],
                    "firstListenedAt": row["first_listened_at"],
                    "lastPlayedAt": row["last_played_at"],
                })
                for row in rows if row["track_id"] in tracksById
            ]
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        firstListenStartTs, firstListenEndTs = self._dateRangeToTimestamps(firstListenedStart, firstListenedEnd)
        return self.repo.getSongsPage(self.user, startTs, endTs, sortBy=sortBy, limit=limit, offset=offset,
                                       trackId=trackId, artistId=artistId, albumId=albumId, searchQuery=searchQuery,
                                       trackIds=trackIds, fullPlaysOnly=fullPlaysOnly,
                                       firstListenStartTs=firstListenStartTs,
                                       firstListenEndTs=firstListenEndTs)

    def getTaggedTracks(self, tags: list[str], match_mode: str = "any", sortBy: str = "plays") -> list[dict]:
        """Return hydrated tracks matching tag filter for the current user.

        The tag-matched ids are pushed down into getSongsPage as a `trackIds`
        filter so the aggregation only touches those rows, rather than paying to
        aggregate the whole play history and discarding the non-matching rows in
        Python."""
        track_ids = self.repo.getTaggedTrackIds(self.user, tags, match_mode=match_mode)
        if not track_ids:
            return []
        return self.repo.getSongsPage(self.user, sortBy=sortBy, limit=None, trackIds=track_ids)

    def getSong(self, trackId: str) -> dict | None:
        """A single song's full metadata plus all-time listen totals - the
        song-detail page's lookup.

        getSongsPage filters is_skip=0, so a track whose plays are ALL skips has
        no row there and this used to return None - which the detail route reads
        as "no such song" and redirects away, even though the most-skipped list
        links straight to it. The skip-sorted query already aggregates
        skip-inclusive (it exists to rank by skips), so it serves as the
        fallback and reports plays=0 with the real skips/encounters.

        Deliberately a second query rather than relaxing getSongsPage: that
        query's is_skip=0 predicate is load-bearing for its plan (see
        getSongsStats), and this path only runs for a track with no real plays
        at all."""
        results = self.getSongsStats(sortBy="plays", limit=1, trackId=trackId)
        if results:
            return results[0]
        skipOnly = self.getSongsStats(sortBy=self.SKIP_SORT_BY, limit=1, trackId=trackId)
        return skipOnly[0] if skipOnly else None

    def getPlayedTrackIds(self, trackIds: list[str]) -> set[str]:
        """The subset of `trackIds` this user has at least one play of - see
        Repository.getPlayedTrackIds."""
        return self.repo.getPlayedTrackIds(self.user, trackIds)

    def getSongsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                       searchQuery: str | None = None, trackIds: list[str] | None = None,
                       fullPlaysOnly: bool = False, sortBy: str | None = None) -> int:
        """Number of distinct songs played in range - the paging counterpart to
        getSongsStats(), for computing total page count without fetching every
        song's metadata. `trackIds` mirrors the same param on getSongsStats().
        `fullPlaysOnly` mirrors the same param on getSongsStats()."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        if sortBy == self.SKIP_SORT_BY:
            return self.repo.getSkippedTracksCount(self.user, startTs, endTs, searchQuery=searchQuery,
                                                    trackIds=trackIds, fullPlaysOnly=fullPlaysOnly)
        return self.repo.getSongsCount(self.user, startTs, endTs, searchQuery=searchQuery, trackIds=trackIds,
                                        fullPlaysOnly=fullPlaysOnly)

    def getPlayTotals(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                       fullPlaysOnly: bool = False, trackIds: list[str] | None = None,
                       albumIds: list[str] | None = None) -> tuple[int, int]:
        """(play count, total time listened) across the whole range - cheap
        aggregate that doesn't require fetching per-song metadata.
        `fullPlaysOnly` mirrors getSongsStats()'s param of the same name -
        defaults False for every existing caller (milestones, Wrapped, Compare,
        dashboard); only the Top Songs/Albums page header opts in. So do
        `trackIds`/`albumIds` (that header's tag filter - see
        Repository.getPlayTotals)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getPlayTotals(self.user, startTs, endTs, fullPlaysOnly=fullPlaysOnly,
                                        trackIds=trackIds, albumIds=albumIds)

    def _getPlayDateSet(self, startTs: float | None, endTs: float | None) -> set[str]:
        """Distinct local ("%Y-%m-%d") dates on which this user actually
        listened in [startTs, endTs). Works off SQL-side buckets
        (getBucketedPlayTotals) - a bucket's start shares its local date with
        every play inside it, so the set is identical to a per-play scan's.
        Shared by the longest-streak and current-streak calculations.

        listeningBuckets, not the raw rows: those rows include skip-only
        buckets (plays=0), which are not listening - see its docstring."""
        rows = listeningBuckets(self.repo.getBucketedPlayTotals(self.user, startTs, endTs))
        return {
            convertToDatetime(r["bucketStartTs"], tz=self.tz).strftime("%Y-%m-%d")
            for r in rows
        }

    def getCurrentStreak(self, now: datetime.datetime = None) -> dict:
        """The user's ongoing consecutive-days listening streak as of `now`
        (defaults to the current local time). Returns {"days", "activeToday"}:
        - activeToday is True when there's already a play logged today.
        - The streak stays "alive" (days > 0) if the most recent play was
          today OR yesterday - a day with no play yet doesn't break it until it
          ends. Two or more empty days since the last play -> days = 0.
        Scans the last CURRENT_STREAK_LOOKBACK_DAYS, then extends the window if
        the streak actually reaches back that far: the bound keeps the common
        case cheap, but it must never cap the answer, or the longest streak
        milestone becomes unreachable no matter how long the user listens."""
        nowLocal = now.astimezone(self.tz) if now is not None else datetime.datetime.now(tz=self.tz)
        today = nowLocal.date()
        lookbackDays = CURRENT_STREAK_LOOKBACK_DAYS
        startTs = self._lookbackWindowStartTs(today, lookbackDays)
        play_dates = self._getPlayDateSet(startTs, None)
        while self._streakFillsWindow(today, play_dates, lookbackDays):
            lookbackDays *= 2
            startTs = self._lookbackWindowStartTs(today, lookbackDays)
            play_dates = self._getPlayDateSet(startTs, None)

        yesterday = today - datetime.timedelta(days=1)
        todayStr = today.strftime("%Y-%m-%d")
        if todayStr in play_dates:
            anchor, activeToday = today, True
        elif yesterday.strftime("%Y-%m-%d") in play_dates:
            anchor, activeToday = yesterday, False
        else:
            return {"days": 0, "activeToday": False}

        days = 0
        cursor = anchor
        while cursor.strftime("%Y-%m-%d") in play_dates:
            days += 1
            cursor -= datetime.timedelta(days=1)
        return {"days": days, "activeToday": activeToday}

    def _lookbackWindowStartTs(self, today: datetime.date, lookbackDays: int) -> float:
        """Local midnight of the day `lookbackDays` before `today`, as a
        timestamp. Deliberately NOT `nowLocal - timedelta(days=lookbackDays)`:
        that anchors the window to `now`'s clock time, so a play earlier in
        the boundary day than `now`'s clock time (e.g. an 08:00 play when
        asked at 18:00) fell OUTSIDE the fetched window. _streakFillsWindow
        then read the run as ending inside the window instead of at its edge,
        the loop never widened, and the streak silently capped at
        CURRENT_STREAK_LOOKBACK_DAYS regardless of how far it actually went
        back."""
        boundaryDay = today - datetime.timedelta(days=lookbackDays)
        return datetime.datetime.combine(boundaryDay, datetime.time.min, tzinfo=self.tz).timestamp()

    @staticmethod
    def _streakFillsWindow(today: datetime.date, playDates: set, lookbackDays: int) -> bool:
        """True when an unbroken run of play days reaches the oldest day the
        current window covers - i.e. the real streak may be longer than what
        was fetched, so the caller must widen and re-ask."""
        oldest = today - datetime.timedelta(days=lookbackDays)
        cursor = today if today.strftime("%Y-%m-%d") in playDates else today - datetime.timedelta(days=1)
        while cursor.strftime("%Y-%m-%d") in playDates:
            if cursor <= oldest:
                return True
            cursor -= datetime.timedelta(days=1)
        return False

    def getListeningCalendar(self, now: datetime.datetime = None,
                             weeks: int = CALENDAR_WEEKS) -> dict:
        """Per-day listening grid for the dashboard's streak calendar: the last
        `weeks` week-columns of daily play counts, each day shaded by volume
        relative to the window's busiest day - reinforcing the getCurrentStreak
        card shown beside it. `now` (defaults to the current local time) fixes
        'today', mirroring getCurrentStreak. Reuses getBucketedPlayTotals and
        the same bucket->local-date mapping as _getPlayDateSet, folding the
        15-minute buckets up to per-day counts, then hands off to
        buildListeningCalendar for the pure grid layout."""
        nowLocal = now.astimezone(self.tz) if now is not None else datetime.datetime.now(tz=self.tz)
        today = nowLocal.date()
        lastMonday = today - datetime.timedelta(days=today.weekday())
        firstMonday = lastMonday - datetime.timedelta(weeks=weeks - 1)
        startTs = datetime.datetime(firstMonday.year, firstMonday.month, firstMonday.day,
                                    tzinfo=self.tz).timestamp()

        dayCounts: dict = {}
        #< the same rows carry the listened time, so the tooltip's per-day
        #  minutes ride along on this scan rather than asking for a second one
        dayMillis: dict = {}
        #< listeningBuckets, like every other consumer: getBucketedPlayTotals
        #  stopped filtering is_skip=0, so a skip-only bucket comes back with
        #  plays=0. buildListeningCalendar happens to test count > 0, so the
        #  totals are right either way - but dayCounts otherwise carries
        #  "YYYY-MM-DD": 0 entries for skip-only days, and a future consumer
        #  reading key presence as "listened" reproduces the exact bug that
        #  helper's docstring documents.
        for row in listeningBuckets(self.repo.getBucketedPlayTotals(self.user, startTs, None)):
            dateStr = convertToDatetime(row["bucketStartTs"], tz=self.tz).strftime("%Y-%m-%d")
            dayCounts[dateStr] = dayCounts.get(dateStr, 0) + row["plays"]
            dayMillis[dateStr] = dayMillis.get(dateStr, 0) + row["totalTimeListened"]
        return buildListeningCalendar(dayCounts, today, weeks=weeks, dayMillis=dayMillis)

    def _onThisDayWindows(self, today: datetime.date) -> list[tuple[float, float]]:
        """One [start, end) timestamp window per PRIOR year the user has plays
        in, spanning today's date ±1 day in that year (local time). The padding
        absorbs the UTC-vs-local date shift; getOnThisDay still does the exact
        local month/day match. Feb 29 falls back to Feb 28 in non-leap years -
        the window is only a pre-filter, so a slightly different anchor costs
        nothing and the exact match still rejects everything."""
        span = self.repo.getPlayTimeRange(self.user)
        if span is None:
            return []
        firstYear = convertToDatetime(span[0], tz=self.tz).year
        windows = []
        for year in range(firstYear, today.year):
            try:
                anchor = datetime.date(year, today.month, today.day)
            except ValueError:
                anchor = datetime.date(year, today.month, today.day - 1)   #< Feb 29 -> Feb 28
            start = datetime.datetime(anchor.year, anchor.month, anchor.day,
                                      tzinfo=self.tz) - datetime.timedelta(days=1)
            windows.append((start.timestamp(), (start + datetime.timedelta(days=3)).timestamp()))
        return windows

    def getOnThisDay(self, now: datetime.datetime = None, limit: int | None = None) -> list[dict]:
        """"On this day" resurfacing: for each PRIOR year that has plays on
        today's local month/day, the track played most that day. Returns
        [{year, yearsAgo, trackId, trackName, artistName, playCount}], newest
        year first, capped at `limit` (None = uncapped). The repo over-selects
        a ±1-day window around today's date in each prior year; the exact local
        month/day match is applied here so it's correct across timezone offsets
        and DST."""
        nowLocal = now.astimezone(self.tz) if now is not None else datetime.datetime.now(tz=self.tz)
        today = nowLocal.date()
        rows = self.repo.getPlaysInTimeWindows(self.user, self._onThisDayWindows(today))

        perYearTrack: dict = {}
        for r in rows:
            localDt = convertToDatetime(r["played_at"], tz=self.tz)
            if (localDt.month, localDt.day) != (today.month, today.day):
                continue
            if localDt.year == today.year:
                continue
            key = (localDt.year, r["track_id"])
            agg = perYearTrack.get(key)
            if agg is None:
                perYearTrack[key] = {"count": 1, "trackName": r["track_name"],
                                     "artistName": r["artist_name"]}
            else:
                agg["count"] += 1

        def sortKey(entry: dict) -> tuple:
            # Highest play count wins; track name (then id) breaks ties so the
            # pick is deterministic.
            return (-entry["playCount"], entry["trackName"] or "", entry["trackId"])

        bestByYear: dict = {}
        for (year, trackId), agg in perYearTrack.items():
            entry = {"year": year, "trackId": trackId, "trackName": agg["trackName"],
                     "artistName": agg["artistName"], "playCount": agg["count"]}
            current = bestByYear.get(year)
            if current is None or sortKey(entry) < sortKey(current):
                bestByYear[year] = entry

        result = sorted(bestByYear.values(), key=lambda e: e["year"], reverse=True)
        if limit is not None:
            result = result[:limit]
        for entry in result:
            entry["yearsAgo"] = today.year - entry["year"]
        return result

    def getDashboardTrends(self, now_ts: float | None = None) -> dict[str, dict | None]:
        """Fetch dashboard trend insights (Obsession, Rediscovery or its Fresh
        Find fallback, Forgotten Favorite) with song metadata."""
        raw = self.repo.getDashboardTrendsRaw(self.user, now_ts=now_ts)
        now_ts = now_ts or time.time()

        result = {"obsession": None, "rediscovery": None, "freshFind": None, "forgotten": None}
        # getTrack is 3 queries; hydrating the three cards one at a time cost 9
        # on an endpoint that returns at most 3 tracks (often the same one
        # twice). getTracksByIds answers the whole set in 3, deduped. Still at
        # most 3 tracks with Fresh Find in the list - it is only ever set when
        # rediscovery is not.
        #
        # Deduped is exactly why each card below stamps its subtitle onto a COPY.
        # One dict comes back per id, and a song last heard in the spring that is
        # then played six times this week wins the obsession AND the rediscovery
        # query - neither excludes the other. Sharing the object meant the second
        # stamp overwrote the first, so the Obsession card rendered "6 plays this
        # week - unplayed for 174 days". Only the top level is written to; the
        # nested album/artist dicts are never mutated, so a shallow copy is enough.
        songsById = self.repo.getTracksByIds([
            item["track_id"] for item in
            (raw.get("obsession"), raw.get("rediscovery"), raw.get("freshFind"),
             raw.get("forgotten")) if item
        ])

        if raw.get("obsession"):
            item = raw["obsession"]
            song = songsById.get(item["track_id"])
            if song:
                song = dict(song)   #< a copy per card - see getTracksByIds above
                song["trend_subtitle"] = obsessionSubtitle(item)
                result["obsession"] = song

        if raw.get("rediscovery"):
            item = raw["rediscovery"]
            song = songsById.get(item["track_id"])
            if song:
                song = dict(song)   #< a copy per card - see getTracksByIds above
                song["trend_subtitle"] = rediscoverySubtitle(item, now_ts)
                result["rediscovery"] = song

        if raw.get("freshFind"):
            item = raw["freshFind"]
            song = songsById.get(item["track_id"])
            if song:
                song = dict(song)   #< a copy per card - see getTracksByIds above
                song["trend_subtitle"] = freshFindSubtitle(item, now_ts)
                result["freshFind"] = song

        if raw.get("forgotten"):
            item = raw["forgotten"]
            song = songsById.get(item["track_id"])
            if song:
                song = dict(song)   #< a copy per card - see getTracksByIds above
                song["trend_subtitle"] = forgottenSubtitle(item, now_ts)
                result["forgotten"] = song

        return result

    def getLongestStreak(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Longest consecutive days of plays in range. See _getPlayDateSet for
        why SQL-side buckets give the same distinct-dates set as a per-play
        scan."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        play_dates = sorted(self._getPlayDateSet(startTs, endTs))
        if not play_dates:
            return 0

        max_streak = 1
        current_streak = 1
        prev_date = None

        for current_date in play_dates:
            if prev_date:
                # Check if dates are consecutive (1 day apart)
                prev_obj = datetime.datetime.strptime(prev_date, "%Y-%m-%d")
                curr_obj = datetime.datetime.strptime(current_date, "%Y-%m-%d")
                if (curr_obj - prev_obj).days == 1:
                    current_streak += 1
                else:
                    max_streak = max(max_streak, current_streak)
                    current_streak = 1
            prev_date = current_date

        max_streak = max(max_streak, current_streak)
        return max_streak

    def getPeakListeningTime(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> tuple[str, int] | None:
        """(day_of_week_name, play_count) for the day with most plays, or None.
        Counting runs in SQL (getBucketedPlayTotals); Python maps each bucket
        to its local weekday.

        listeningBuckets first: with skip-only buckets in the rows, `if not
        rows` was no longer the same test as "no plays in range", so a range
        whose only activity was skips returned an arbitrary weekday with a count
        of 0 instead of nothing."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        rows = listeningBuckets(self.repo.getBucketedPlayTotals(self.user, startTs, endTs))
        if not rows:
            return None

        counts = {}
        for row in rows:
            dt = convertToDatetime(row["bucketStartTs"], tz=self.tz)
            #< Python's locale-independent weekday index, mapped through the
            #  canonical English names (config.WEEKDAY_NAMES is Monday-first,
            #  exactly weekday()'s order)
            day_name = WEEKDAY_NAMES[dt.weekday()]
            counts[day_name] = counts.get(day_name, 0) + row["plays"]

        peak_day = max(counts, key=counts.get)
        return peak_day, counts[peak_day]

    def getDiscoveredSongsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Count of distinct songs first played within the date range."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getDiscoveredSongsCount(self.user, startTs, endTs)

    def getDiscoveredArtistsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> int:
        """Count of distinct artists first played within the date range."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getDiscoveredArtistsCount(self.user, startTs, endTs)

    def getAlbumsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                        sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                        albumId: str | None = None, searchQuery: str | None = None,
                        albumIds: list[str] | None = None, fullPlaysOnly: bool = False,
                        firstListenedStart: datetime.datetime = None,
                        firstListenedEnd: datetime.datetime = None) -> list:
        """Return albums sorted by `sortBy` with aggregated listen totals - sorted/
        paged in SQL via a single batched query (see Repository.getAlbumsPage),
        mirroring getSongsStats(). `albumId` narrows this to a single album's
        stats. `albumIds` narrows to an explicit set of album ids (the
        tag-filtered Top Albums page). `searchQuery` narrows to albums whose
        name or artist(s) match. `fullPlaysOnly` and
        `firstListenedStart`/`firstListenedEnd` mirror getSongsStats()'s
        params of the same names."""
        if sortBy == self.SKIP_SORT_BY:
            rows = self._skipSortedPage(
                self.repo.getMostSkippedAlbums, startDate, endDate, limit, offset,
                albumId=albumId, searchQuery=searchQuery, albumIds=albumIds, fullPlaysOnly=fullPlaysOnly)
            return [self._withSkipRate(row) for row in rows]
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        firstListenStartTs, firstListenEndTs = self._dateRangeToTimestamps(firstListenedStart, firstListenedEnd)
        return self.repo.getAlbumsPage(self.user, startTs, endTs, sortBy=sortBy, limit=limit, offset=offset,
                                        albumId=albumId, searchQuery=searchQuery, albumIds=albumIds,
                                        fullPlaysOnly=fullPlaysOnly,
                                        firstListenStartTs=firstListenStartTs,
                                        firstListenEndTs=firstListenEndTs)

    def getAlbum(self, albumId: str) -> dict | None:
        """A single album's aggregate stats - the album-detail page's lookup.

        Falls back to the skip-ranked query for the same reason getArtist and
        getSong do - getAlbumsPage filters is_skip=0, so a skip-only album's own
        Most Skipped entry linked to a page that redirected away."""
        results = self.getAlbumsStats(sortBy="plays", limit=1, albumId=albumId)
        if results:
            return results[0]
        skipOnly = self.getAlbumsStats(sortBy=self.SKIP_SORT_BY, limit=1, albumId=albumId)
        return skipOnly[0] if skipOnly else None

    def getAlbumBio(self, albumId: str) -> str | None:
        """This album's stored biography (see lazyFetchAlbumBio), or None if
        it's never been fetched or Last.fm has nothing usable - mirrors
        getArtistBio."""
        return self.repo.getAlbumBioState(albumId)["bio"]

    def getPlayedAlbumIds(self, albumIds: list[str]) -> set[str]:
        """The subset of `albumIds` this user has at least one play from - see
        Repository.getPlayedAlbumIds."""
        return self.repo.getPlayedAlbumIds(self.user, albumIds)

    def getAlbumsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                        searchQuery: str | None = None, albumIds: list[str] | None = None,
                        fullPlaysOnly: bool = False, sortBy: str | None = None) -> int:
        """Number of distinct albums played in range - the paging counterpart to
        getAlbumsStats(), for computing total page count without fetching every
        album's metadata. `albumIds` mirrors the same param on getAlbumsStats().
        `fullPlaysOnly` mirrors the same param on getAlbumsStats()."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        if sortBy == self.SKIP_SORT_BY:
            return self.repo.getSkippedAlbumsCount(self.user, startTs, endTs, searchQuery=searchQuery,
                                                    albumIds=albumIds, fullPlaysOnly=fullPlaysOnly)
        return self.repo.getAlbumsCount(self.user, startTs, endTs, searchQuery=searchQuery, albumIds=albumIds,
                                         fullPlaysOnly=fullPlaysOnly)

    def getTopAlbums(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                      limit: int | None = None, offset: int = 0, searchQuery: str | None = None,
                      albumIds: list[str] | None = None, fullPlaysOnly: bool = False) -> list:
        # Albums are sorted/paged in SQL (see getAlbumsStats -> Repository.getAlbumsPage)
        # rather than re-sorted here in Python, for the same reason getTopSongs is.
        return self.getAlbumsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset, searchQuery=searchQuery,
                                    albumIds=albumIds, fullPlaysOnly=fullPlaysOnly)

    def getArtistsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         artistId: str | None = None, sortBy: str = "plays", limit: int | None = None,
                         offset: int = 0, searchQuery: str | None = None,
                         artistIds: list[str] | None = None, fullPlaysOnly: bool = False,
                         firstListenedStart: datetime.datetime = None,
                         firstListenedEnd: datetime.datetime = None) -> list:
        """Return artists sorted by `sortBy` with aggregated data and listen
        totals - sorted/paged in SQL via a single batched query (see
        Repository.getArtistAggregates) rather than fetching every artist and
        sorting/paging in Python, mirroring getSongsStats()/getAlbumsStats().
        `artistId` narrows this to a single artist's stats; `artistIds` narrows
        to an explicit set of artist ids (the tag-filtered Top Artists page);
        `searchQuery` narrows to artists whose name matches. `fullPlaysOnly`
        and `firstListenedStart`/`firstListenedEnd` mirror getSongsStats()'s
        params of the same names."""
        if sortBy == self.SKIP_SORT_BY:
            rows = self._skipSortedPage(
                self.repo.getMostSkippedArtists, startDate, endDate, limit, offset,
                artistId=artistId, searchQuery=searchQuery, artistIds=artistIds, fullPlaysOnly=fullPlaysOnly)
            return [self._withSkipRate(row) for row in rows]
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        firstListenStartTs, firstListenEndTs = self._dateRangeToTimestamps(firstListenedStart, firstListenedEnd)
        return self.repo.getArtistAggregates(self.user, startTs, endTs, artistId=artistId, sortBy=sortBy,
                                              limit=limit, offset=offset, searchQuery=searchQuery,
                                              artistIds=artistIds, fullPlaysOnly=fullPlaysOnly,
                                              firstListenStartTs=firstListenStartTs,
                                              firstListenEndTs=firstListenEndTs)

    def getArtist(self, artistId: str, startDate: datetime.datetime = None,
                  endDate: datetime.datetime = None) -> dict | None:
        """A single artist's aggregate stats - the artist-detail page's lookup.

        Falls back to the skip-ranked query for the same reason getSong does:
        getArtistAggregates filters is_skip=0, so an artist whose every
        encounter was a skip has no row there, and the detail route reads that
        as "no such artist" and redirects away - while the Most Skipped list
        (HAVING skips > 0) links straight to it, losing the sort, tag, range and
        page the user came from."""
        results = self.getArtistsStats(startDate, endDate, artistId=artistId, limit=1)
        if results:
            return results[0]
        skipOnly = self.getArtistsStats(startDate, endDate, artistId=artistId,
                                        sortBy=self.SKIP_SORT_BY, limit=1)
        return skipOnly[0] if skipOnly else None

    def getArtistBio(self, artistId: str) -> str | None:
        """This artist's stored biography (see lazyFetchArtistBio), or None
        if it's never been fetched or Last.fm has nothing usable. Kept
        separate from getArtist()/getArtistsStats() rather than added to
        that aggregate query - bio text has no place on list pages (Top
        Artists), only the detail page needs it."""
        return self.repo.getArtistBioState(artistId)["bio"]

    def getPlayedArtistIds(self, artistIds: list[str]) -> set[str]:
        """The subset of `artistIds` this user has at least one play of a
        track crediting - see Repository.getPlayedArtistIds."""
        return self.repo.getPlayedArtistIds(self.user, artistIds)

    def getArtistsCount(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                         searchQuery: str | None = None, artistIds: list[str] | None = None,
                         fullPlaysOnly: bool = False, sortBy: str | None = None) -> int:
        """Number of distinct artists played in range - the paging counterpart
        to getArtistsStats(), for computing total page count without fetching
        every artist's metadata. `artistIds` mirrors the same param on
        getArtistsStats(). `fullPlaysOnly` mirrors the same param on
        getArtistsStats()."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        if sortBy == self.SKIP_SORT_BY:
            return self.repo.getSkippedArtistsCount(self.user, startTs, endTs, searchQuery=searchQuery,
                                                     artistIds=artistIds, fullPlaysOnly=fullPlaysOnly)
        return self.repo.getArtistsCount(self.user, startTs, endTs, searchQuery=searchQuery, artistIds=artistIds,
                                          fullPlaysOnly=fullPlaysOnly)

    def getArtistTotals(self, startDate: datetime.datetime = None,
                         endDate: datetime.datetime = None, fullPlaysOnly: bool = False,
                         artistIds: list[str] | None = None) -> tuple[int, int, int]:
        """(total plays, total unique songs, total time listened) summed across
        every artist in range - the Top Artists page's "(top list)" totals,
        computed directly in SQL instead of fetching every artist and summing
        in Python. `fullPlaysOnly` mirrors getArtistsStats()'s param of the
        same name, keeping this header total consistent with the filtered list;
        so does `artistIds` (the page's tag filter)."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getArtistTotals(self.user, startTs, endTs, fullPlaysOnly=fullPlaysOnly,
                                          artistIds=artistIds)

    def getOverallStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> dict:
        """The dashboard's headline numbers: top song/artist plus play and
        duration totals, with the same-length PRECEDING window's totals for
        the trend arrows."""
        previousSongsPlayed, previousDurationMs = 0, 0
        if startDate and endDate:
            windowLength = endDate - startDate
            previousStart, previousEnd = startDate - windowLength, startDate
            prevStartTs, prevEndTs = self._dateRangeToTimestamps(previousStart, previousEnd)
            previousSongsPlayed, previousDurationMs = self.repo.getPlayTotals(self.user, prevStartTs, prevEndTs)

        # totalSongsPlayed/totalDurationMs are computed via a dedicated COUNT/SUM
        # query rather than by summing every song's stats: each play belongs to
        # exactly one song, so sum(plays-per-song) == total play count over the
        # same range - identical math, without hydrating every song just to add
        # its numbers up. currentTopSongs only needs the single top row.
        totalSongsPlayed, totalDurationMs = self.getPlayTotals(startDate, endDate)
        currentTopSongs = self.getTopSongs(startDate=startDate, endDate=endDate, by="plays", limit=1)
        currentTopArtists = self.getTopArtists(startDate=startDate, endDate=endDate, by="totalTimeListened", limit=1)

        return {
            "currentTopSongs": currentTopSongs,
            "currentTopArtists": currentTopArtists,
            "totalSongsPlayed": totalSongsPlayed,
            "totalDurationMs": totalDurationMs,
            "previousSongsPlayed": previousSongsPlayed,
            "previousDurationMs": previousDurationMs,
        }

    def getTopSongs(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                     limit: int | None = None, offset: int = 0, searchQuery: str | None = None,
                     trackIds: list[str] | None = None, fullPlaysOnly: bool = False) -> list:
        # Songs are sorted/paged in SQL (see getSongsStats -> Repository.getSongsPage)
        # rather than re-sorted here in Python: once pagination is pushed down to
        # the database, re-sorting an already-LIMIT-ed page can't reconstruct
        # global rank, so SQL ordering must be the single source of truth.
        return self.getSongsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset, searchQuery=searchQuery,
                                   trackIds=trackIds, fullPlaysOnly=fullPlaysOnly)

    def getTopArtists(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays",
                       limit: int | None = None, offset: int = 0, searchQuery: str | None = None,
                       artistIds: list[str] | None = None, fullPlaysOnly: bool = False) -> list:
        # Artists are sorted/paged in SQL (see getArtistsStats -> Repository.getArtistAggregates)
        # rather than re-sorted here in Python, for the same reason getTopSongs is.
        return self.getArtistsStats(startDate, endDate, sortBy=by, limit=limit, offset=offset, searchQuery=searchQuery,
                                     artistIds=artistIds, fullPlaysOnly=fullPlaysOnly)

    def _bucketKey(self, date: datetime.datetime, groupBy: str) -> str:
        """The chart-bucket label for one already-local `date` - see
        services.time_buckets.bucketKey (CORE-6) for the groupBy shapes and
        why `tz` has to be threaded through explicitly rather than defaulted."""
        return _timeBucketKey(date, groupBy, self.tz)

    def getPlayBuckets(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                        trackId: str | None = None, artistId: str | None = None,
                        albumId: str | None = None) -> list:
        """The raw pre-aggregated bucket rows both getListeningTimeSeries and
        getHourOfDayHeatmap map into local time. Exposed so a caller rendering
        both from the SAME range (the /charts page) can run the aggregate once
        and hand the rows to each, instead of paying for two byte-identical
        queries per request."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        return self.repo.getBucketedPlayTotals(self.user, startTs, endTs, trackId=trackId,
                                                artistId=artistId, albumId=albumId)

    def getListeningTimeSeries(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                                groupBy: str = "day", trackId: str | None = None, artistId: str | None = None,
                                albumId: str | None = None, bucketRows: list | None = None) -> list:
        """Total listening time and play count per day or week, gap-filled with
        zero-value buckets so a bar chart shows a continuous timeline.
        `trackId`/`artistId`/`albumId` narrow this to one item's plays only -
        reused as-is by the song/artist/album detail pages' play-history chart
        (same output shape, so the frontend's existing renderTimeSeriesChart
        needs no changes).

        The per-play summing happens in SQL (see getBucketedPlayTotals);
        Python only re-buckets the pre-aggregated 15-minute rows into the
        app's configurable IANA timezone, which SQLite can't express.
        `bucketRows` lets a caller supply rows it already fetched for the same
        range (see getPlayBuckets)."""
        rows = bucketRows if bucketRows is not None else self.getPlayBuckets(
            startDate, endDate, trackId=trackId, artistId=artistId, albumId=albumId)
        return buildTimeSeries(rows, self.tz, groupBy, startDate=startDate, endDate=endDate)

    def getHourOfDayHeatmap(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None,
                             trackId: str | None = None, artistId: str | None = None,
                             albumId: str | None = None, bucketRows: list | None = None) -> list:
        """7x24 grid (rows Monday=0..Sunday=6, columns hour-of-day 0-23) of total
        listening time and play count, for a 'when do I listen' heatmap.
        `trackId`/`artistId`/`albumId` narrow this to one item's plays only -
        reused by the song detail page's 'when you listen to this song' heatmap,
        same as getListeningTimeSeries's item filters. Summing runs in SQL
        (getBucketedPlayTotals); Python maps each 15-minute bucket to its
        local weekday/hour cell. `bucketRows` lets a caller supply rows it
        already fetched for the same range (see getPlayBuckets)."""
        rows = bucketRows if bucketRows is not None else self.getPlayBuckets(
            startDate, endDate, trackId=trackId, artistId=artistId, albumId=albumId)
        return fillHeatmapGrid(rows, self.tz)

    def getArtistTrend(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, topN: int = 5, groupBy: str = "week") -> dict:
        """Per-bucket play counts for the topN most-played artists in the range, for
        an 'artist trend over time' line chart. Buckets are only the ones that have
        any activity - unlike getListeningTimeSeries, a trend line doesn't need a
        gap-filled timeline the way a bar chart's x-axis does."""
        startTs, endTs = self._dateRangeToTimestamps(startDate, endDate)
        # Per-(bucket, artist) counts pre-summed in SQL; Python only re-maps
        # the 15-minute buckets into the chart's local-timezone buckets.
        rows = self.repo.getBucketedArtistPlayCounts(self.user, startTs, endTs)

        totalPlaysByArtist = {}
        idPlaysByArtist = {}   #< {name: {artistId: totalPlays}} - picks a click-through target below
        bucketedCounts = []
        # bucketRowsByKey memoizes the local-timezone conversion + bucket-key
        # mapping per distinct bucketStartTs rather than recomputing it per
        # row - ~77k rows collapse to ~21k conversions on a large library.
        for key, row in bucketRowsByKey(rows, self.tz, groupBy):
            name = row["artistName"]
            bucketedCounts.append((key, name, row["plays"]))
            totalPlaysByArtist[name] = totalPlaysByArtist.get(name, 0) + row["plays"]
            idCounts = idPlaysByArtist.setdefault(name, {})
            idCounts[row["artistId"]] = idCounts.get(row["artistId"], 0) + row["plays"]

        if not totalPlaysByArtist:
            return {"buckets": [], "series": []}

        topNames = [name for name, _ in sorted(totalPlaysByArtist.items(), key=lambda kv: kv[1], reverse=True)[:topN]]

        bucketKeys = sorted({key for key, _, _ in bucketedCounts})
        seriesData = {name: {key: 0 for key in bucketKeys} for name in topNames}
        for key, name, plays in bucketedCounts:
            if name in seriesData:
                seriesData[name][key] += plays

        # Two different artist ids sharing a display name still merge into
        # one line (by design - see getBucketedArtistPlayCounts): the id
        # that contributed the most plays under that name represents the
        # whole line for click-through, ties broken by id so the pick is
        # deterministic rather than depending on incidental row order.
        series = []
        for name in topNames:
            representativeId = min(idPlaysByArtist[name].items(), key=lambda kv: (-kv[1], kv[0]))[0]
            series.append({
                "name": name,
                "id": representativeId,
                "data": [seriesData[name][key] for key in bucketKeys],
            })
        return {"buckets": bucketKeys, "series": series}

    def getUserSpotifyCredentials(self) -> dict | None:
        return self.repo.getUserSpotifyCredentials(self.user)

    def updateUserSpotifyCredentials(self, clientId: str | None, clientSecret: str | None, refreshToken: str | None) -> None:
        self.repo.updateUserSpotifyCredentials(self.user, clientId, clientSecret, refreshToken)

    def setSpotifyNeedsReauth(self, needsReauth: bool) -> None:
        changed = self.repo.setSpotifyNeedsReauth(self.user, needsReauth)
        if needsReauth and changed:
            from services.email_worker import queue_email_notification
            from Database.queries.email_queries import EVENT_API_KEY_FAILED
            queue_email_notification(self.user, EVENT_API_KEY_FAILED)

    def getRecentlyRecordedTrackIds(self, trackIds: list[str]) -> set[str]:
        """Which of these tracks this user already has a recent play for.
        Bound to this user and handed to the Listener as a callback, so the
        listener's missed-play cross-check can consult the database without
        knowing anything about it (see _dropUrisAlreadyInDatabase)."""
        return self.repo.getRecentlyRecordedTrackIds(
            self.user, trackIds, CONNECT_STATE_MISSED_TRACK_LOOKBACK_SECONDS)

    def getRecordedPlayTimes(self, startTs: float, endTs: float) -> list[tuple[str, float, float | None]]:
        """The (track_id, played_at, listener_created_at) triples this user
        already has in a time window. Bound to this user and handed to the
        Listener as a callback (like getRecentlyRecordedTrackIds above), so the
        Web API backfill can tell a genuine gap from an empty in-memory cache
        without the listener knowing anything about the database.

        Triples, not bare times - see getTrackPlayTimesInRange for why the
        dedup cannot be sound without the track id, and for what the third
        element (a listener row's observed play end) is for."""
        return self.repo.getTrackPlayTimesInRange(self.user, startTs, endTs)

    def getUserLastfmApiKey(self) -> str | None:
        return self.repo.getUserLastfmApiKey(self.user)

    def updateUserLastfmApiKey(self, apiKey: str | None) -> None:
        self.repo.updateUserLastfmApiKey(self.user, apiKey)
