# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Instance-wide display and behavior constants.

Extracted from app.py so app.py, the routes/ modules, and the dashboard/ helper
mixins can all pull the same values from one place. app.py re-exports these
(`from config import *`), so `from app import <CONST>` and routes' `appmod.<CONST>`
keep working unchanged. This module imports nothing from the app, so the mixins
can `from config import ...` (including in default arguments) without a cycle.
"""

PAGE_SIZE = 50                  #< list items shown per page
LOGIN_CACHE_TTL_SECONDS = 180  #< seconds to cache isListenerLoggedIn result per user
MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS = 300  #< seconds to cache the shared media cache folder's on-disk size (getGlobalDatabaseStats) - recomputing it walks/subprocess-scans the whole directory
CHART_ARTIST_TREND_TOP_N = 5   #< how many top artists are plotted on the trend line chart
CHART_TOP_GENRES_LIMIT = 10    #< bars on the Charts page's Top Genres chart
CHART_MOST_SKIPPED_LIMIT = 10  #< rows in the Charts page's most-skipped songs/artists lists
WRAPPED_TOP_GENRES_LIMIT = 5   #< genres listed on the Wrapped genre card
COMPARE_TOP_GENRES_LIMIT = 10  #< per-side genres (and shared genres) shown on Compare
COMPARE_GENRE_POOL_SIZE = 50   #< per-side genre pool the shared-genre intersection is computed over
TRACK_CARD_GENRE_LIMIT = 3     #< genre pills shown per track/artist/album card, position-ordered
ON_THIS_DAY_YEARS_LIMIT = 5    #< max prior years surfaced in the dashboard "On this day" card
# Chips shown in the dashboard's friends-listening block before it collapses
# into a "+N more" count. Its length is driven by other people's listening
# rather than by anything the viewer controls, so it needs a cap of some kind.
# Lowered from 6 when the block moved inside the Now Playing card: chips that
# used to spread across a full-width row now stack down one card, and six of
# them made that card several times taller than the two beside it.
FRIENDS_NOW_PLAYING_LIMIT = 4
LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS = 10   #< the listen-time totals (dashboard "Total listen time", /genres stat strip) drop the seconds component once the total reaches this many hours
RECOMMENDATION_ARTIST_LIMIT = 5    #< artists shown in the dashboard "Discover" recommendations card
RECOMMENDATION_GENRE_POOL = 15     #< how many of the user's top genres candidate artists are matched against
RECOMMENDATION_EXCLUDE_TOP_N = 25  #< user's most-played artists excluded from recommendations (already well-known to them)
GENRE_PAGE_LIST_LIMIT = 12         #< genres shown in the Genres page distribution bars / share donut / chip list
GENRE_MIX_TREND_TOP_N = 6          #< genres plotted on the Genres page "mix over time" multi-line chart (kept small so it stays legible)
GENRE_PAGE_TOP_ARTISTS_LIMIT = 10  #< top artists listed for the selected genre
GENRE_PAGE_TOP_TRACKS_LIMIT = 10   #< top tracks listed for the selected genre
WRAPPED_LIST_SIZE = 10          #< default/fallback for ?limit= - how many items per category the Wrapped page shows
WRAPPED_LIMIT_OPTIONS = (10, 25, 50, 100)   #< selectable values for Wrapped's items-per-category dropdown
# Public Wrapped share-link expiry choices: form value -> seconds until
# expiry, or None for "never". Mirrors ALBUM_BACKFILL_RETRY_SECONDS/
# GENRE_BACKFILL_RETRY_SECONDS's N * 24 * 3600 convention in repository.py.
SHARE_LINK_EXPIRY_CHOICES = {
    "never": None,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}
# Cap on concurrent (non-expired) share links per "bucket" - a bucket is
# either one specific year or the all-years link type. Prevents runaway
# link accumulation (each one is a standing, unauthenticated access grant)
# while still letting someone hand out a few links to different people
# without having to revoke-then-recreate each time.
SHARE_LINK_MAX_PER_BUCKET = 5
COMPARE_TOP_LIST_SIZE = 10                #< items per top-songs/artists/albums list shown on the Compare page
COMPARE_OVERLAP_POOL_SIZE = 100           #< how deep each side's top songs/artists/albums lists are searched for taste-match overlap
# Top Common Songs/Artists/Albums search a SEPARATE, deeper pool than
# COMPARE_OVERLAP_POOL_SIZE - decoupled on purpose so widening the shared-
# item search can never move the taste-match score (see _tasteMatchPercent,
# which only ever reads the shallower topXPool fields). First knob to
# revisit if the Top Common lists feel too sparse (raise it) or too full of
# irrelevant long-tail matches (lower it) - 300 was tried and felt too deep.
COMPARE_SHARED_POOL_SIZE = 200
COMPARE_TREND_WEEK_SPAN_DAYS = 120        #< comparison trends spanning more days than this auto-bucket by week...
COMPARE_TREND_MONTH_SPAN_DAYS = 730       #< ...and more than this by month (day buckets over years are sub-pixel)
# Sanity window for the hand-editable ?startDate=/&endDate= custom range.
# Dates outside it are treated exactly like unparseable ones (the route's
# default window takes over - see DateRangeMixin._parseCustomRangeDate): no
# play can predate the Unix epoch (broken import timestamps clamp to 0 =
# 1970), and past-2100 dates fed datetime-limit arithmetic that raised
# OverflowError (the custom end's own +1 day, getOverallStats' previous-period
# mirror subtracting a centuries-long duration) or gap-filled one chart bucket
# per day across those centuries (~740k buckets, ~9s of CPU and a >100MB
# payload per request for ?startDate=0001-01-01).
CUSTOM_RANGE_MIN_YEAR = 1970
CUSTOM_RANGE_MAX_YEAR = 2100
# Ceiling on the bucket count an EXPLICIT Trend-buckets choice may imply
# before Auto's span-derived size takes over instead (see _resolveGroupBy):
# ~27 years of day buckets - beyond any real listening history, so every
# legitimate explicit choice is untouched - while "day buckets across
# centuries" from a hand-edited URL can no longer reach the gap-fill.
# Database.MAX_TIME_SERIES_BUCKETS backstops the same limit at the query layer.
MAX_TREND_BUCKETS = 10_000
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_INLINE_ARTISTS = 5   #< artist lists longer than this collapse behind a "+N more" toggle (_artist_links.html)...
MIN_HIDDEN_ARTISTS = 2   #< ...but only when at least this many names would be hidden - "+1 more" saves no space
# Shown in place of a track/artist/album cover that 404s or fails to load
# (not yet downloaded, or the fetch failed) instead of a broken-image icon -
# a plain music-note glyph on the app's surface color. Exposed to templates
# via dashboard/context_processors.py so layout.html/layout_public.html's
# window.PLACEHOLDER_IMG and _track_card.html's inline fallback src share one
# literal instead of three hand-kept copies.
PLACEHOLDER_IMG_DATA_URI = (
    "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<rect width='100' height='100' rx='16' fill='%231d1d1d'/>"
    "<path d='M62 24v34.8a12 12 0 1 0 6 10.4V36l10-3v-9l-16 4z' fill='%23555'/></svg>"
)
MAX_UPLOAD_MB = 500              #< cap on a single import-history request's total upload size
# A ZIP upload is bounded by MAX_UPLOAD_MB like any other request body, but
# what it UNPACKS to is not - that is what this caps (services/import_upload.py).
# The same number on purpose: the ceiling means "how much history one request
# may hand the importer", and unpacking server-side should not quietly raise
# it. Without this, a 25 MB archive of 10 GB of zeroes passes every size check
# the request layer has. Raise this one, not MAX_UPLOAD_MB, if a genuinely
# larger export ever has to fit.
MAX_UNCOMPRESSED_IMPORT_MB = MAX_UPLOAD_MB
# A second ceiling the byte cap above cannot enforce: empty archive entries
# cost nothing to store and plenty to parse, so 50,000 zero-byte members spend
# 0% of the budget and still take seconds of a waitress worker thread
# (measured). This is request-wide across every ZIP in the upload and counts
# ignored members/directories too, because the cost appears before suffix
# filtering. Spotify's own export holds a few dozen files, and Flask's
# MAX_FORM_PARTS default is this same 1000, so this is generous for anything
# real. It bounds opening entries and per-member read-ahead; the
# central-directory parse before that stays bounded only by MAX_UPLOAD_MB.
MAX_IMPORT_ARCHIVE_ENTRIES = 1000
# Unit conversions, named so the ladders that format a byte count or split
# an hour total into days read as units rather than as bare powers of two.
# Binary (1024), not decimal: what they format is an on-disk size, which is
# what every filesystem tool reports this way. Deliberately NOT imported by
# Database/ - that package does not depend on config.py (see the module
# docstring), so its own byte caps keep spelling the multiplication out.
BYTES_PER_KB = 1024
BYTES_PER_MB = BYTES_PER_KB * 1024
BYTES_PER_GB = BYTES_PER_MB * 1024
HOURS_PER_DAY = 24
# The port app.run() binds. The container publishes it and the compose file
# maps it, so changing this alone doesn't move a Docker deployment's port.
DEFAULT_PORT = 5444
DEFAULT_SORT_BY = "totalTimeListened"
# The only sortBy values Repository.SONG_SORT_COLUMNS/ALBUM_SORT_COLUMNS/
# ARTIST_SORT_COLUMNS know how to handle - an unrecognized ?sortBy= would
# otherwise reach a ValueError deep in the DB layer and 500 instead of just
# falling back to the default.
VALID_SORT_BY = {"totalTimeListened", "plays", "name"}
# The Top Songs/Albums/Artists pages additionally offer "skips", which routes
# them to a skip-ranked query instead of the normal aggregates (see
# Database.SKIP_SORT_BY). Deliberately not in the shared set: /compare and
# /wrapped read the same ?sortBy= but have no skip-ranked path, so accepting it
# there would hand them differently-shaped rows (Compare) or an in-Python
# re-sort on a key those rows don't carry (Wrapped).
TOP_LIST_SORT_BY = VALID_SORT_BY | {"skips"}
# The sorts a Top list will compare against the previous period (see
# services/rank_movement.py). Deliberately a subset of the four it offers:
#   "name"  - alphabetical position shifts by one whenever anything is inserted
#             above it, so every row would carry an arrow about nothing.
#   "skips" - skip rank runs through a Bayesian prior computed over the window,
#             and the prior itself differs between the two windows, so an entry
#             could "climb" without a single play of its own changing.
# Taken off what the pages OFFER, naming both exclusions, rather than off the
# shared set - where "skips" happens not to be, so subtracting only "name"
# reads as the same thing and is one edit away from not being: promote "skips"
# into VALID_SORT_BY and this would silently start badging it, against the
# reason written directly above. See test_skip_sort.py's guard on the value.
MOVEMENT_SORT_BY = TOP_LIST_SORT_BY - {"name", "skips"}
# The highest ?page= the movement endpoint will turn into a SQL offset. It has
# no row count to clamp against (the list route does, via _calculatePagination),
# and an unclamped page multiplies into an OFFSET that overflows SQLite's int64
# and raises. Far past any real library - 50M entries at PAGE_SIZE - so every
# page it rejects was already going to answer empty.
MOVEMENT_MAX_PAGE = 1_000_000
# What users.default_top_list_window falls back to, and the column's own DEFAULT.
# The Top pages were hardcoded to all-time before that setting existed, so this
# value is what keeps an upgraded account seeing exactly what it saw. Deliberately
# the "all time" spelling rather than "" - see _topListFilters for why All Time
# must be expressible in a query string now that the default is per-user.
TOP_LIST_DEFAULT_WINDOW = "all time"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
# The silent opt-outs - mirror of TRUTHY_ENV_VALUES above, for the flags where
# "0" is joined by a spelled-out false/no/off rather than being the only one.
FALSY_ENV_VALUES = {"0", "false", "no", "off"}
# The literal FLASK_SECRET_KEY shipped as a placeholder in docker-compose.yml.
# Booting with it signs session cookies - and, when DATA_ENCRYPTION_KEY is unset,
# encrypts every stored Spotify session/secret at rest - under a publicly-known
# value, i.e. a trivial full-auth-bypass. _get_or_create_secret_key refuses to
# start on this exact value (see app.py).
PLACEHOLDER_FLASK_SECRET_KEY = "changeme-generate-your-own-random-value"
# Where the Flask signing key is persisted when FLASK_SECRET_KEY is unset, so
# sessions survive a restart. Same directory as the data encryption key
# (Database/secret_store.py's DEFAULT_KEY_PATH) and written by the same helper,
# so both get the same owner-only permissions and atomic write.
SECRETS_DIR_NAME = "secrets"
FLASK_SECRET_KEY_FILENAME = "flask_secret_key.txt"
# Admin-triggered graceful restart (POST /admin/restart). The button gracefully
# stops workers then exits so a *supervising* launch script relaunches the
# process - it must NOT be relied on for a bare, unsupervised process (the app
# would just stop). Off unless this env var is truthy, so it can only be enabled
# from a launch script the operator has made relaunch-on-exit.
ALLOW_INSTANCE_RESTART_ENV_VAR = "ALLOW_INSTANCE_RESTART"
# Delay before the graceful shutdown+exit runs, so the HTTP response for the
# restart request reaches the admin's browser before the process dies.
INSTANCE_RESTART_DELAY_SECONDS = 0.5
# Opt-in to honoring X-Forwarded-* headers from a reverse proxy (see
# _trustedProxyCount). Without it, every visitor behind a proxy shares the
# proxy's IP, so the per-IP auth rate limiter would let any one client lock
# the entire instance out of /login for the whole window.
TRUST_PROXY_HEADERS_ENV_VAR = "TRUST_PROXY_HEADERS"
# When set, the user with this email is made the instance's ONLY admin at
# startup (see _ensureAdminExists) - the explicit-configuration path, and the
# recovery path if the automatic earliest-user promotion picked the wrong
# account.
ADMIN_EMAIL_ENV_VAR = "ADMIN_EMAIL"
# The public URL Spotify redirects to after /spotify-authorize. Setting it is
# what enables Web-API backfilling: every Spotify Developer API route, link
# and badge is gated on it, and with it unset those routes 404.
SPOTIFY_CALLBACK_URL_ENV_VAR = "SPOTIFY_CALLBACK_URL"
# Truthy = skip the "do these cookies belong to this email" check at login
# (see SpotifyDashboardApp.skipEmailVerification). That check is what stops
# one user from claiming another's account, so this is off by default.
SKIP_EMAIL_VERIFICATION_ENV_VAR = "SKIP_EMAIL_VERIFICATION"
# Truthy: send notification mail over TLS WITHOUT verifying the relay's
# certificate or hostname. Off by default - the verified context is what keeps
# the SMTP credentials and every mail from going to whoever answers on that
# port. For a self-hosted relay on a self-signed certificate only; the send
# path logs a warning on every use so it cannot be forgotten.
SMTP_SKIP_TLS_VERIFY_ENV_VAR = "SMTP_SKIP_TLS_VERIFY"
# When set, the auto-importer only picks up dropped files whose name contains
# this keyword; unset = import every dropped file.
IMPORT_KEYWORD_ENV_VAR = "IMPORT_KEYWORD"
PASSWORD_MIN_LENGTH = 8   #< also enforced client-side via the minlength attribute
# The editable label an account is shown as (users.display_name). The username
# itself can never change - it's the primary key eight tables reference by
# foreign key - so this is the only name a user gets to pick. The charset is the
# auto-generated username's (see get_or_create_user) plus spaces: it lands in
# page titles, a share picker, a downloaded PNG's filename and other users'
# screens, so anything needing escaping or path handling stays out. The minimum
# is 2 rather than 3 so a short real name ("Jo") isn't rejected.
DISPLAY_NAME_MIN_LENGTH = 2
DISPLAY_NAME_MAX_LENGTH = 32   #< also enforced client-side via the maxlength attribute
DISPLAY_NAME_ALLOWED_PATTERN = r"^[A-Za-z0-9 _-]+$"
# The Spotify OAuth CSRF `state` round-trip (RFC 6749 §10.12): /spotify-authorize
# stores a one-shot random value under this session key and sends it along to
# Spotify; /spotify-callback refuses to exchange a code unless the request
# echoes that exact value back. Without it, anyone sharing this instance's
# Spotify app credentials could complete the consent themselves and trick a
# logged-in victim into loading the callback URL - storing the ATTACKER's
# refresh token (and, via backfill, their listening history) on the victim's
# account.
SPOTIFY_OAUTH_STATE_SESSION_KEY = "spotify_oauth_state"

# How long a "remember me" session cookie stays valid (Flask's
# permanent_session_lifetime). It is also the window a stolen cookie is good
# for, which is what the session_version below exists to cut short.
PERMANENT_SESSION_LIFETIME_DAYS = 30

# The session cookie's copy of users.session_version. Sessions here are signed
# COOKIES with no server-side store, so there is nothing to delete when someone
# wants their other devices signed out - instead every cookie carries this
# number and stops matching the moment the account's copy moves (a password
# reset, or "Sign out everywhere").
#
# A MISSING key reads as 0, which is what makes the upgrade free: every cookie
# minted before the column existed carries no version at all, and the column
# starts at 0 for everyone, so nobody is logged out by the upgrade itself. The
# first bump ends those cookies too - by which point their owner has asked for
# exactly that.
SESSION_VERSION_KEY = "sv"
SPOTIFY_OAUTH_STATE_NUM_BYTES = 32   #< entropy fed to secrets.token_urlsafe
# Ceiling on the /spotify-callback code-for-token exchange. It runs on the
# request thread while the user waits on a redirect, so requests' default (no
# timeout at all) would pin a Waitress thread on an unresponsive Spotify -
# the same reason VERSION_CHECK_TIMEOUT_SECONDS exists.
SPOTIFY_AUTH_TIMEOUT_SECONDS = 10
RATE_LIMIT_MAX_ATTEMPTS = 10     #< max POSTs allowed per window, per source IP, per route
RATE_LIMIT_WINDOW_SECONDS = 300  #< 5 minutes
RATE_LIMIT_ERROR_MESSAGE = "Too many attempts. Please wait a few minutes and try again."
EXPORT_FORMATS = ("json", "csv")
PLAYLIST_EXPORT_FORMATS = ("csv", "m3u", "xspf")
# How deep "export this Wrapped year as a playlist" reaches. Independent of
# WRAPPED_LIST_SIZE/WRAPPED_LIMIT_OPTIONS, which size what the PAGE shows: a
# playlist is a file the user keeps, so it takes the whole top-100 rather
# than however many rows happened to be on screen.
WRAPPED_TOP_SONGS_EXPORT_LIMIT = 100

# Dashboard Trend Insights thresholds
TREND_OBSESSION_DAYS = 7
# Play floors. Each card used to demand a higher count first (5 here, 15 for
# Forgotten Favorite) and rerun the same statement at the floor when nothing
# cleared it - but both passes order by play count and take the top row, and
# every track above the higher bar is above the floor, so the second pass could
# only ever return the row the first would have. The two-pass shape bought a
# second lifetime scan for every lighter listener; the floor alone is the same
# card (tests/test_trends.py::TestForgottenFavoriteFloor counts the statements).
TREND_OBSESSION_MIN_PLAYS = 2
TREND_REDISCOVERY_GAP_DAYS = 180
TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS = 3
# The "recent" side of the rediscovery split (plays newer than this count as
# the rediscovery; older ones are the historical listens it was rediscovered
# from). Kept equal to TREND_OBSESSION_DAYS but named separately so tuning the
# obsession window can't silently move rediscovery's boundary.
TREND_REDISCOVERY_RECENT_DAYS = 7
TREND_FORGOTTEN_GAP_DAYS = 180
TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS = 2   #< see TREND_OBSESSION_MIN_PLAYS
# Rediscovery degrades on the gap instead, since its play floor is already low:
# shorter comebacks tried in order only when no track clears the gap above.
# Longest-first, so the strongest gap that matches is the one shown - a genuine
# 180-day comeback is never displaced by a busier 30-day one.
TREND_REDISCOVERY_FALLBACK_GAP_DAYS = (60, 30)

# Fresh Find - what the Rediscovery slot shows when not even the shortest gap
# above matches: the track whose all-time first listen is the most recent
# arrival worth mentioning, ranked by how often it has been played since.
# The window is wider than TREND_OBSESSION_DAYS on purpose - a track first
# heard two days ago has had no time to accumulate plays, so a 7-day window
# would mostly find one-play tracks.
TREND_FRESH_FIND_DAYS = 14
# Two plays, not one: a single play is a track that went past, a repeat is a
# find. Also what keeps the card off a brand-new listener's dashboard until
# they have actually returned to something.
TREND_FRESH_FIND_MIN_PLAYS = 2
# Random startup-offset bounds for this module's periodic workers, so a
# restart doesn't fire every worker at the same instant (the metadata
# backfiller and wrapped worker in Database/database.py already stagger
# themselves the same way). The Spotify listener is deliberately NOT
# staggered - delaying it would lose plays.
VERSION_CHECK_MIN_START_DELAY_SECONDS = 30
VERSION_CHECK_MAX_START_DELAY_SECONDS = 180
LOGIN_CHECK_MIN_START_DELAY_SECONDS = 60
LOGIN_CHECK_MAX_START_DELAY_SECONDS = 300
# ...and how long each sleeps BETWEEN passes - the delays above only stagger
# the first one. The login re-check is the cadence a "session expired" gap in
# the log is measured against, so it belongs next to the offsets rather than
# inline in the loop.
VERSION_CHECK_INTERVAL_SECONDS = 60 * 60
LOGIN_CHECK_INTERVAL_SECONDS = 60 * 5
# Short on purpose: an unreachable GitHub is the expected case for an offline
# instance, and the result only drives an "update available" banner - waiting
# out requests' default (no timeout at all) would pin the thread indefinitely.
VERSION_CHECK_TIMEOUT_SECONDS = 6

# How long the WHOLE of shutdown's phase 2 may spend joining stopper threads
# (see _stopDatabasesConcurrently) - a deadline shared by every user, not a
# per-user allowance. Every join inside one Database.stop() is itself bounded -
# two on the listener, one on the auto-import watchdog, five on the periodic
# workers - and this covers their sum with slack, so it only ever expires for a
# user whose threads are genuinely wedged. Users are stopped concurrently, and
# the deadline is what makes that concurrency show up in the budget: joining
# them one after another at this timeout EACH costs it times the user count,
# because join(timeout=) waits out the whole timeout on a thread that is still
# running. Shared, phase 2 stays one user's worth however many there are, which
# is what keeps shutdown inside the compose file's stop_grace_period
# (tests/test_compose_shutdown_budget.py pins the two against each other).
USER_STOP_JOIN_TIMEOUT_SECONDS = 30

# Stopper threads are named "<prefix><user>". Same reason the listener and
# watchdog threads carry theirs: an anonymous "Thread-17" in a dump taken
# during a slow shutdown says nothing about whose stop is still running.
SHUTDOWN_THREAD_NAME_PREFIX = "shutdown-"

# Query parameter carrying a static asset's mtime, appended to every
# url_for('static', ...) by registerRoutes' url_defaults hook in app.py so a
# changed file is served under a URL the browser has never cached.
STATIC_VERSION_PARAM = "v"

# Baseline defense-in-depth headers applied to every response (see
# registerRoutes' after_request hook in app.py).
#
# script-src/style-src keep 'unsafe-inline': every template in this app relies
# on inline <script> blocks and inline event-handler attributes (onclick=,
# onerror=, style=...), none of which are nonce/hash-tagged - disallowing
# unsafe-inline here would break the app outright, not just tighten it.
# Google Fonts is the only external resource any template actually loads.
# The two Spotify hosts exist solely for the detail pages' "Play now" embed
# (lazily loaded, only after a click): open.spotify.com serves the iFrame API
# loader script and the player iframe, and embed-cdn.spotifycdn.com serves the
# loader's actual payload script - hence both a script-src and a frame-src
# allowance. frame-ancestors 'none'/X-Frame-Options restrict others framing
# *this* app and are unaffected by us framing Spotify.
# Strict-Transport-Security is NOT in this baseline dict: it's opt-in via the
# ENABLE_HSTS env var (see _hstsEnabled/_setSecurityHeaders in app.py). Kept
# off by default because this app is normally self-hosted over plain HTTP on a
# local network/Docker host (see README), where HSTS would force HTTPS for the
# origin going forward - actively breaking that expected setup.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://open.spotify.com https://embed-cdn.spotifycdn.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src https://open.spotify.com; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}

# What the app's own responses say about caching. Every one of them renders one
# account's data, so none may be STORED - a logout that ends the session but
# leaves rendered pages in the browser's back/forward cache means the next
# person to press Back gets the previous account's history, no request made.
#
# max-age=0 rides along for intermediaries that predate no-store.
NO_STORE_CACHE_CONTROL = "no-store, max-age=0"

# Flask's built-in endpoint for /static/<path>. The one exemption from the
# above: assets carry no account data, and this app ships htmx and a chart
# bundle that would otherwise be re-fetched on every navigation.
STATIC_ENDPOINT = "static"

# The other exemption: album art and artist pictures (routes/media.py and the
# /shared/<token> pair in routes/wrapped.py). send_from_directory already sets
# its own "no-cache" there, so the no-store above never applied to them - but
# no-cache still means a conditional request per image per page load, and these
# are authenticated routes: each of those 304s costs a session read plus
# is_user_logged_in's queries, ~30 times over on a top-list page.
#
# A week rather than a day, because the files are write-once:
# tryClaimImageDownload refuses to re-claim an image already marked OK, and the
# artist route's lazy fetch only writes when the file is missing. A given
# <imageId>.jpeg never changes content - a different image arrives under a
# different id - so there is nothing for a short window to catch, and the one
# case that DOES change (an image not downloaded yet) answers 404, which stays
# uncacheable under the rule above.
IMAGE_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
# private, not public: the routes sit behind a session check (or a share
# token), so a shared proxy must not serve one viewer's response to the next.
# Flask's own max_age= argument emits "public", so this replaces its header
# rather than adding to it.
IMAGE_CACHE_CONTROL = f"private, max-age={IMAGE_CACHE_MAX_AGE_SECONDS}"

# The detail pages (/song, /artist, /album) run Spotify's iFrame API bundle in
# our page context; that bundle is a webpack build with the `eval` devtool, so
# it needs 'unsafe-eval'. That directive can't be scoped to a host in CSP, so it
# is confined to just those three routes (see DETAIL_CSP_ENDPOINTS/
# _setSecurityHeaders in app.py) instead of relaxing the whole app. Derived from
# the baseline above so the two stay in lockstep.
DETAIL_PAGE_CSP = SECURITY_HEADERS["Content-Security-Policy"].replace(
    "script-src 'self' 'unsafe-inline'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
)

# Opt-in HTTP Strict-Transport-Security, sent only when ENABLE_HSTS is truthy
# (see _hstsEnabled/_setSecurityHeaders in app.py). Enable it only when a
# TLS-terminating reverse proxy fronts the app - on a plain-HTTP deployment it
# would pin browsers to HTTPS for the origin and break access.
ENABLE_HSTS_ENV_VAR = "ENABLE_HSTS"
HSTS_MAX_AGE_SECONDS = 31536000   #< 1 year - the minimum max-age the HSTS preload list accepts
HSTS_HEADER_VALUE = f"max-age={HSTS_MAX_AGE_SECONDS}; includeSubDomains"
