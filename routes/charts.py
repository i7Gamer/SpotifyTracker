# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Main stats pages: the public /overview, the dashboard index (/), the Top
Songs/Albums/Artists lists, and the /charts analytics page.

Extracted verbatim from app.py. Genre-gate/coverage helpers come from services/;
the PAGE_SIZE / CHART_* constants come from config. Every stats/pagination/embed
helper is reached through the dashboard instance.
"""
import logging

from flask import render_template, redirect, request, url_for, session, jsonify, Response

from config import (
    PAGE_SIZE, CHART_ARTIST_TREND_TOP_N, CHART_TOP_GENRES_LIMIT,
    CHART_MOST_SKIPPED_LIMIT, TOP_LIST_SORT_BY, MOVEMENT_SORT_BY,
    MOVEMENT_MAX_PAGE, ON_THIS_DAY_YEARS_LIMIT,
    LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS, RECOMMENDATION_ARTIST_LIMIT,
    RECOMMENDATION_GENRE_POOL, RECOMMENDATION_EXCLUDE_TOP_N,
    TOP_LIST_DEFAULT_WINDOW, HOURS_PER_DAY, BYTES_PER_KB, BYTES_PER_MB, BYTES_PER_GB,
)
from routes._htmx import isHtmxSwap
from routes.details import (
    DETAIL_BODY_TARGET, DETAIL_HISTORY_TARGET, DETAIL_MORE_TARGET,
    MAX_DETAIL_HISTORY_PAGES,
)
from routes.pagination import positivePageArg as _positivePageArg
from routes._auth import makeRequiresUser
from dashboard.date_ranges import isSingleDayInterval
from Database.database import Database
from Database.utils import dateToString, msToString
from services.genre_gate import (
    emptyGenreCoverage, resolveGenreCoverage, genreGatePasses, resolveGenreDistribution,
    emptyBiographyCoverage, resolveBiographyCoverage, userHasLastfmKey,
)
from services.listening_behavior import buildListeningBehavior
from services.milestones import buildNextMilestones, formatMilestone, MS_PER_HOUR
from services.rank_movement import (
    PREVIOUS_WINDOW_SCAN_LIMIT, previousWindow, rankMovements,
)

logger = logging.getLogger(__name__)


def register(app, dashboard):
    # Read off the class, not the per-request db instance. A route test's
    # MagicMock db answers `db.SKIP_SORT_BY` with a Mock, which equals no
    # string - so every branch guarded on it silently took the other path and
    # the whole skip-count path went untested.
    SKIP_SORT_BY = Database.SKIP_SORT_BY
    requiresUser = makeRequiresUser(dashboard)

    # ---- Top Songs/Albums/Artists: the parts all three share -----------------
    # The three pages differ in which aggregate they read and what they call
    # the things they list. Everything else - parsing the filter card, the
    # two-phase AJAX split, choosing between the dedicated and the unique
    # count, building the pagination context - was written out three times,
    # which is how the skip-sort filters got fixed in one and missed in the
    # others.

    def _topListFilters(db, username):
        """The filter card's state, read identically by all three Top pages."""
        # The tag filter is gated on the admin's instance-wide tags kill
        # switch: with tags off we ignore a hand-crafted ?tag= (the dropdown is
        # already hidden template-side) and skip the getUserTags query.
        tagsOn = dashboard.repo.isTagsEnabled()
        # fullOnly defaults to on (a favorite has to have actually been heard)
        # - see templates/_page_card.html's checkbox. Explicit ?fullOnly=0 opts
        # out. Both spellings are kept: the raw one rebuilds pagination links,
        # the bool goes to the queries.
        fullOnly = request.args.get("fullOnly", "1")
        # These pages have their own default window, separate from the one the
        # Dashboard/Charts/Genres/Compare share: a career ranking and a "what
        # have I played lately" view want different answers, and this one
        # defaults to All Time, which is what the pages were hardcoded to
        # before the setting existed. See /profile's Preferences section.
        defaultWindow = db.repo.getUserSettings(username).get(
            "default_top_list_window", TOP_LIST_DEFAULT_WINDOW)
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")
        return {
            "searchQuery": request.args.get("q", ""),
            "sortBy": dashboard._getSortByParam(allowed=TOP_LIST_SORT_BY),
            # The Top pages' two defaults are NOT the same value - an absent
            # ?interval= takes the account's default_top_list_window, a
            # present-but-empty one has always meant All Time (every link
            # these pages built before that setting existed, and the old
            # <option value=""> submitted it) - and a custom range with
            # either date missing falls back like an empty one would. See
            # DateRangeMixin._resolveIntervalParam's docstring for why this
            # can never come back out as "": _buildPageUrl and
            # _topListShell's listArgs both drop empty values from every
            # link they build, so an "" surviving out of here would vanish
            # from listUrl and every page link, and the request that
            # followed would re-resolve defaultWindow - the card would say
            # All Time over a list scoped to something else.
            "interval": dashboard._resolveIntervalParam(
                defaultWindow, TOP_LIST_DEFAULT_WINDOW, customStart, customEnd),
            "customStart": customStart,
            "customEnd": customEnd,
            "tag": request.args.get("tag", "") if tagsOn else "",
            "fullOnly": fullOnly,
            "fullPlaysOnly": fullOnly != "0",
            #< only offered to users who've actually tagged something - see
            #  _page_card.html's {% if tags_enabled and user_tags %} guard
            "userTags": db.repo.getUserTags(username) if tagsOn else [],
        }

    def _topListShell(section, template, endpoint, username, filters):
        """The plain GET half of the two-phase load: the filter card plus an
        empty #topListResults placeholder. htmx then fetches the stat header +
        list + pagination on first paint and on every filter/sort/tag/page
        change - see templates/_page_card.html."""
        # The URL the placeholder loads from, built from the VALIDATED filter
        # values rather than echoed from request.full_path - same rule the
        # pagination links below follow, and the same one historyPage carries.
        # Reflecting the raw query string would assert a junk ?interval= in the
        # one place a reader would trust and disagree with every link beside it.
        #
        # fullOnly rides along unconditionally because it is a real tri-state to
        # the route ("1" default / "0" opt-out) that the filter card always
        # submits, so leaving it out here would make the first load and every
        # subsequent one disagree about a filter the user can see is on.
        # startDate/endDate ride along whenever they are set, rather than only
        # for interval == "custom" - deliberately matching what
        # _buildPaginationContext already puts in every page link, so the first
        # load and the links below it agree. (The dates only ever APPLY under
        # interval == "custom" - see dashboard/date_ranges.py's
        # _getDateRange - so carrying them here for another interval is inert
        # filter state, not a second way to select Custom; the card's
        # customActive in _page_card.html is keyed on interval alone.)
        listArgs = {
            "q": filters["searchQuery"],
            "sortBy": filters["sortBy"],
            "interval": filters["interval"],
            "startDate": filters["customStart"],
            "endDate": filters["customEnd"],
            "tag": filters["tag"],
            "fullOnly": filters["fullOnly"],
            "page": _positivePageArg(),
        }
        return render_template(
            template, section=section, username=username,
            sortBy=filters["sortBy"], interval=filters["interval"],
            customStart=filters["customStart"], customEnd=filters["customEnd"],
            tag=filters["tag"], user_tags=filters["userTags"],
            fullPlaysOnly=filters["fullPlaysOnly"],
            listUrl=url_for(endpoint, **{k: v for k, v in listArgs.items() if v}))

    def _topListTotal(filters, countFn, uniqueCount, **idKwarg):
        """How many rows the pager is sizing itself for.

        The plain unique count already on the stat card is reused when nothing
        narrows the list. A search or tag obviously changes it - and so does
        the skip sort, whose page lists only entities that were actually
        skipped, so its total is smaller than the unique count above it."""
        if filters["sortBy"] == SKIP_SORT_BY or filters["searchQuery"] or filters["tag"]:
            return countFn(searchQuery=filters["searchQuery"], fullPlaysOnly=filters["fullPlaysOnly"],
                           sortBy=filters["sortBy"], **idKwarg)
        return uniqueCount

    # The header cards' totals + the "Unique X" stat card, one small function
    # per section because the underlying aggregates genuinely differ in
    # shape (getArtistTotals returns a third number the other two don't) -
    # everything else about the three Top pages is identical enough to share
    # _topListPage below. `ids` is the tag-scoped id list (see
    # _TOP_LIST_KINDS' `idsKwarg`), already resolved by the caller.
    def _songsStats(db, startDate, endDate, fullPlaysOnly, ids):
        totalPlays, totalMs = db.getPlayTotals(startDate, endDate, fullPlaysOnly=fullPlaysOnly, trackIds=ids)
        uniqueCount = db.getSongsCount(startDate, endDate, fullPlaysOnly=fullPlaysOnly, trackIds=ids)
        return totalPlays, totalMs, uniqueCount, [
            {"label": "Total Plays", "value": totalPlays},
            {"label": "Time", "value": msToString(totalMs)},
            {"label": "Unique Songs", "value": uniqueCount},
        ]

    def _albumsStats(db, startDate, endDate, fullPlaysOnly, ids):
        totalPlays, totalMs = db.getPlayTotals(startDate, endDate, fullPlaysOnly=fullPlaysOnly, albumIds=ids)
        uniqueCount = db.getAlbumsCount(startDate, endDate, fullPlaysOnly=fullPlaysOnly, albumIds=ids)
        return totalPlays, totalMs, uniqueCount, [
            {"label": "Total Plays (top list)", "value": totalPlays},
            {"label": "Time", "value": msToString(totalMs)},
            {"label": "Unique Albums", "value": uniqueCount},
        ]

    def _artistsStats(db, startDate, endDate, fullPlaysOnly, ids):
        totalPlays, totalUnique, totalMs = db.getArtistTotals(
            startDate, endDate, fullPlaysOnly=fullPlaysOnly, artistIds=ids)
        uniqueCount = db.getArtistsCount(startDate, endDate, fullPlaysOnly=fullPlaysOnly, artistIds=ids)
        return totalPlays, totalMs, uniqueCount, [
            {"label": "Total Plays (top list)", "value": totalPlays},
            {"label": "Unique Songs (top list)", "value": totalUnique},
            {"label": "Unique Artists", "value": uniqueCount},
        ]

    # The one step besides `stats` that isn't shared verbatim: songs run an
    # extra _embedSongsTextElements pass the other two don't need.
    def _songsEmbed(items, sortBy, totalPlays, totalMs):
        items = dashboard._embedSongsTextElements(items)
        return dashboard._embedTopSongsTextElements(items, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

    def _albumsEmbed(items, sortBy, totalPlays, totalMs):
        return dashboard._embedAlbumsTextElements(items, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

    def _artistsEmbed(items, sortBy, totalPlays, totalMs):
        return dashboard._embedArtistsTextElements(items, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

    # Everything that tells the three Top pages apart, keyed by `section` so
    # _topListPage and topListMovement share one vocabulary rather than
    # mapping one set of names onto another (CORE-7, 2026-09-02 review - this
    # table used to be _MOVEMENT_KINDS, movement-endpoint-only; the four
    # fields below are all it ever needed, and are exactly what _topListPage
    # needs too).
    _TOP_LIST_KINDS = {
        "top_songs": {
            "getter": "getTopSongs", "tagIds": "getTaggedTrackIds",
            "idsKwarg": "trackIds", "entity": "track", "countFn": "getSongsCount",
            "template": "top_songs.html", "endpoint": "topSongsPage",
            "emptyMessage": "No top songs available. Import some listening history first.",
            "stats": _songsStats, "embed": _songsEmbed,
        },
        "top_artists": {
            "getter": "getTopArtists", "tagIds": "getTaggedArtistIds",
            "idsKwarg": "artistIds", "entity": "artist", "countFn": "getArtistsCount",
            "template": "top_artists.html", "endpoint": "topArtistsPage",
            "emptyMessage": "No top artists available. Import some listening history first.",
            "stats": _artistsStats, "embed": _artistsEmbed,
        },
        "top_albums": {
            "getter": "getTopAlbums", "tagIds": "getTaggedAlbumIds",
            "idsKwarg": "albumIds", "entity": "album", "countFn": "getAlbumsCount",
            "template": "top_albums.html", "endpoint": "topAlbumsPage",
            "emptyMessage": "No top albums available. Import some listening history first.",
            "stats": _albumsStats, "embed": _albumsEmbed,
        },
    }

    # The movement URL carries the page's ids positionally: position i is rank
    # startIndex + i + 1, so a slot must never appear or disappear.
    _MOVEMENT_ID_SEPARATOR = ","

    def _movementIds(items):
        """The page's ids for that URL, one slot per row, in rank order.

        Two entries forfeit their own badge and keep their slot, because
        dropping either would shift every rank below it - silently, since those
        badges still land on real entries and merely report a distance nobody
        held:

          * an entry with no id. None can occur today, as all three ranking
            queries inner-join their catalog table; this is what stops that
            from being load-bearing three layers away.
          * an id containing the separator. Spotify's own ids are
            alphanumeric, but an IMPORTED id is whatever the export file said -
            StreamingHistoryImporter reads it out of spotify_track_uri without
            validating it, and derives album ids from it - so the character set
            is not something to rank by.
        """
        slots = []
        for entry in items:
            entityId = entry.get("id") or ""
            slots.append("" if _MOVEMENT_ID_SEPARATOR in entityId else entityId)
        return _MOVEMENT_ID_SEPARATOR.join(slots)

    def _movementPage():
        """?page= as the rank this page starts at, or None when it names a page
        nobody can be on.

        Deliberately not _positivePageArg. That one answers "is this worth
        echoing back into markup", and the shells that use it are clamped for
        real afterwards, against a row count. Here the number IS the arithmetic
        - it decides what rank each entry is judged at - and there is no count
        to clamp against, so an impossible page is refused rather than guessed
        at: honouring it would render "Down 49,999,899" on an entry that never
        moved.

        isdecimal(), not isdigit(): see _positivePageArg - '²'/'①' are
        isdigit()-true and int()-rejected."""
        raw = request.args.get("page", "")
        if not raw:
            return 1
        if not raw.isdecimal() or len(raw) > len(str(MOVEMENT_MAX_PAGE)):
            return None   #< digits counted before int(), which refuses >4300 of them
        page = int(raw)
        return page if 0 < page <= MOVEMENT_MAX_PAGE else None

    def _movementUrl(section, filters, page, dateRange, items):
        """Where the results fragment asks for its rank arrows - or None when
        there is no question to ask, in which case it renders no trigger at all
        rather than paying for a request that can only answer empty.

        Carries the page it rendered AND the ids on it. The page, because the
        list clamps an out-of-range one against its total count and re-deriving
        that would mean running the count query again just to agree. The ids,
        because they are the answer to a query the list has already run: without
        them the endpoint has to repeat that ranking aggregate wholesale, purely
        to learn what is on screen. It also closes a race - a play recorded
        between the two requests could otherwise reorder the list and have the
        answer arrive about an entry the page never rendered, which htmx reports
        as an oob element with no target."""
        if filters["sortBy"] not in MOVEMENT_SORT_BY or previousWindow(*dateRange) is None:
            return None
        args = {
            "kind": section,
            "q": filters["searchQuery"],
            "sortBy": filters["sortBy"],
            "interval": filters["interval"],
            "startDate": filters["customStart"],
            "endDate": filters["customEnd"],
            "tag": filters["tag"],
            "fullOnly": filters["fullOnly"],
            "page": page,
            "ids": _movementIds(items),
        }
        return url_for("topListMovement", **{k: v for k, v in args.items() if v})

    @requiresUser(api=True)
    def topListMovement(username, db):
        """How far each entry on one Top-list page has moved since the period
        before it, as out-of-band spans for the placeholders the rows carry.

        A third phase on purpose. The previous period's ranking costs the same
        as the list's own aggregate, and the list is what the user is waiting
        for - so this runs after those rows are on screen, and an empty answer
        (see the guards below) leaves them exactly as they are.

        The page's own ids arrive in the request rather than being re-derived
        (see _movementUrl), so what this costs is one ranking of the previous
        period plus one existence check, not a second ranking of a period the
        list has already ranked.

        api=True for the same reason the dashboard's deferred cards keep their
        plain 401: this is a background request, and answering it with
        HX-Redirect would navigate the whole page away from under someone who
        is reading it."""
        spec = _TOP_LIST_KINDS.get(request.args.get("kind", ""))
        filters = _topListFilters(db, username)
        if spec is None or filters["sortBy"] not in MOVEMENT_SORT_BY:
            return ""
        #< capped at a page: the ids name what to badge, and a crafted request
        #  must not turn that into an unbounded id set. Empty slots are KEPT -
        #  they hold the rank positions of entries that carry no id.
        currentIds = request.args.get("ids", "").split(_MOVEMENT_ID_SEPARATOR)[:PAGE_SIZE]
        if not any(currentIds):
            return ""
        startDate, endDate = dashboard._getDateRange(
            filters["interval"], filters["customStart"], filters["customEnd"],
            default="all time", tz=db.tz)
        window = previousWindow(startDate, endDate)
        if window is None:
            return ""   #< All Time: there is no period before all of it

        # Every filter the page applied, applied identically to both windows -
        # a comparison against a differently-filtered period is a wrong answer
        # rather than a missing one.
        tag = filters["tag"]
        narrowedIds = getattr(db.repo, spec["tagIds"])(username, [tag]) if tag else None
        fetch = getattr(db, spec["getter"])
        common = {"by": filters["sortBy"], "searchQuery": filters["searchQuery"],
                  "fullPlaysOnly": filters["fullPlaysOnly"], spec["idsKwarg"]: narrowedIds}

        page = _movementPage()
        if page is None:
            return ""   #< a page nobody can be on; see _movementPage
        startIndex = (page - 1) * PAGE_SIZE
        previous = fetch(startDate=window[0], endDate=window[1],
                         limit=PREVIOUS_WINDOW_SCAN_LIMIT, offset=0, **common)
        # What the bounded scan above cannot answer: whether an entry it did not
        # reach was absent from the period or merely below its depth. Cheap
        # enough to ask about a page's worth of entries because it drives from
        # the entity side - see Repository.getEntitiesPlayedInRange.
        playedPreviously = set(db.getEntitiesPlayedInRange(
            spec["entity"], [entityId for entityId in currentIds if entityId],
            window[0], window[1], fullPlaysOnly=filters["fullPlaysOnly"]))

        return render_template(
            "_top_list_movement.html",
            movements=rankMovements(
                currentIds, [entry["id"] for entry in previous],
                startIndex=startIndex, playedPreviously=playedPreviously))
    app.add_url_rule("/api/top-list-movement", "topListMovement", topListMovement, methods=["GET"])

    def _narrowedEmptyMessage(searchQuery, tag, interval, importPitchMessage, default="all time"):
        """The "nothing here" text a search, a tag, or a narrowed interval
        deserves, versus a genuinely empty library's import pitch (UT-11,
        2026-09-02 review): once any of the three is active, the visitor is
        looking for something specific that simply isn't there - saying so
        beats offering to import history that may already exist. Only the
        search text is named; a bare tag or interval filter has no one
        string worth quoting. Shared by _topListResults (the three Top
        pages' `emptyMessage=...` literals) and historyPage's
        _history_results.html, whose Jinja carries the identical rule for
        the same reason."""
        if searchQuery or tag or interval != default:
            return f'No matches for "{searchQuery}".' if searchQuery else "No matches for this filter."
        return importPitchMessage

    def _topListResults(section, endpoint, username, filters, items, statCards,
                         page, totalPages, totalCount, startIndex, emptyMessage,
                         dateRange=(None, None)):
        pagination = dashboard._buildPaginationContext(
            endpoint, page, totalPages, totalCount,
            q=filters["searchQuery"], tag=filters["tag"], sortBy=filters["sortBy"],
            interval=filters["interval"], startDate=filters["customStart"],
            endDate=filters["customEnd"], fullOnly=filters["fullOnly"])
        # The fragment itself, not a JSON envelope around it: htmx swaps the
        # response body straight into #topListResults, so a {"resultsHtml": ...}
        # wrapper would land in the page as literal JSON text.
        return render_template(
            "_top_list_results.html", tracks=items, statCards=statCards, startIndex=startIndex,
            section=section, username=username,
            emptyMessage=_narrowedEmptyMessage(
                filters["searchQuery"], filters["tag"], filters["interval"], emptyMessage),
            movementUrl=_movementUrl(section, filters, page, dateRange, items),
            #< the cards' "First Listened" is a MIN over the SELECTED range (see
            #  _track_card.html); All Time is the only one where it is a lifetime
            #  first play, and it is the only interval _getDateRange leaves open
            rangeScopedFirstListen=dateRange[0] is not None,
            **pagination)

    def _topListPage(username, db, section):
        """The Top Songs/Artists/Albums route body, shared (CORE-7,
        2026-09-02 review): which aggregate to read and what to call things
        (see _TOP_LIST_KINDS) is the only thing that ever differed between
        the three - the two-phase shell split, the pagination context and
        the results render were written out three times, which is how the
        skip-sort filters got fixed in one and missed in the others before
        _topListFilters/_topListResults existed (reviewFindings.md
        2026-07-25 item 10; d441a77 stopped at those shared helpers)."""
        spec = _TOP_LIST_KINDS[section]
        filters = _topListFilters(db, username)
        if not isHtmxSwap():
            return _topListShell(section, spec["template"], spec["endpoint"], username, filters)

        tag = filters["tag"]
        ids = getattr(db.repo, spec["tagIds"])(username, [tag]) if tag else None
        startDate, endDate = dashboard._getDateRange(
            filters["interval"], filters["customStart"], filters["customEnd"],
            default="all time", tz=db.tz)
        fullPlaysOnly = filters["fullPlaysOnly"]
        idKwarg = {spec["idsKwarg"]: ids}

        # The header cards' totals are a whole-range aggregate regardless of
        # search - a cheap dedicated query (spec["stats"]) instead of
        # summing every item's metadata. The TAG filter DOES scope them
        # (unlike search): a tag narrows what the page is about, so cards
        # reading whole-library numbers above a tag-filtered list
        # contradicted the pager right below them.
        totalPlays, totalMs, uniqueCount, statCards = spec["stats"](db, startDate, endDate, fullPlaysOnly, ids)

        totalCount = _topListTotal(
            filters, lambda **kw: getattr(db, spec["countFn"])(startDate, endDate, **kw), uniqueCount,
            **idKwarg)
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
        # WHERE-clause matching instead of sorting+hydrating+filtering every
        # item ever played in Python.
        items = getattr(db, spec["getter"])(
            startDate=startDate, endDate=endDate, by=filters["sortBy"],
            limit=PAGE_SIZE, offset=startIndex, searchQuery=filters["searchQuery"],
            fullPlaysOnly=fullPlaysOnly, **idKwarg)

        items = spec["embed"](items, filters["sortBy"], totalPlays, totalMs)
        items = dashboard._attachGenres(db, items, spec["entity"])

        return _topListResults(
            section, spec["endpoint"], username, filters, items, statCards=statCards,
            page=page, totalPages=totalPages, totalCount=totalCount, startIndex=startIndex,
            dateRange=(startDate, endDate), emptyMessage=spec["emptyMessage"])

    # Reachable as dashboard._topListPage rather than only as the bare closure
    # above: the three one-line routes below call it through `dashboard` so a
    # test can patch.object(dash, "_topListPage") and assert each one passes
    # its own section - patching the closure itself has no effect on what a
    # route calls, since Python resolves a free variable lexically, not
    # through the instance.
    dashboard._topListPage = _topListPage

    def _workerStatus(fetch, label):
        """One `db.getXWorkerStatus()` probe, defended the same way three
        times over in overviewPage: an exception, or a reply of the wrong
        shape, leaves the caller's existing default in place rather than
        crashing the whole /overview page over a status probe. Returns None
        on either failure so the caller can `or` it onto its default; `label`
        names what failed in the log line (CORE-10 part 4, 2026-09-02
        review)."""
        try:
            status = fetch()
        except Exception as e:
            logger.warning("%s worker status lookup failed: %s", label, e)
            return None
        if isinstance(status, dict):
            return {"configured": bool(status.get("configured")), "running": bool(status.get("running"))}
        return None

    def _ownStatusBadges(current_username, current_db):
        """The logged-in user's own sync/backfill state, as the simple
        three-badge summary overviewPage's your_status renders - not a table
        (the full multi-user table with per-account admin controls lives on
        /admin now). None when the account has no row at all (CORE-10 part
        4, 2026-09-02 review)."""
        own = dashboard.repo.getAllUsersDetails(username=current_username)
        if not own:
            return None
        u = own[0]
        if u["cookies_json"] and current_db is not None:
            health = current_db.getListenerHealth()
            sync_status = health.get("status", "UNKNOWN")
        else:
            sync_status = "Not Configured"
        has_api = bool(u["spotify_client_id"] and u["spotify_refresh_token"])
        needs_reauth = bool(u.get("spotify_needs_reauth"))
        return {
            "sync_status": sync_status,
            "spotify_api_status": "Needs Re-Auth" if (has_api and needs_reauth) else ("Configured" if has_api else "Not Configured"),
            #< .get(): raw row presence check only - the stored key
            #  is encrypted and never needs decrypting here
            "lastfm_api_status": "Configured" if u.get("lastfm_api_key") else "Not Configured",
        }

    def overviewPage():
        # Intentionally unauthenticated: aggregate counts/DB size carry no
        # per-user listening data, so they're shown to any visitor as a
        # public "is this instance alive" summary - only the per-user
        # status widget below is gated on login. The full multi-user
        # table and every admin-only setting live on /admin now.
        global_stats = dashboard.repo.getGlobalDatabaseStats()

        total_time_ms = global_stats.get("total_time_ms", 0)
        total_hours = total_time_ms // MS_PER_HOUR
        if total_hours >= HOURS_PER_DAY:
            days = total_hours // HOURS_PER_DAY
            hours = total_hours % HOURS_PER_DAY
            global_time_text = f"{days}d {hours}h"
        else:
            global_time_text = f"{total_hours}h"

        db_size_bytes = global_stats.get("db_size_bytes", 0)
        if db_size_bytes >= BYTES_PER_GB:
            global_size_text = f"{db_size_bytes / BYTES_PER_GB:.2f} GB"
        elif db_size_bytes >= BYTES_PER_MB:
            global_size_text = f"{db_size_bytes / BYTES_PER_MB:.2f} MB"
        else:
            global_size_text = f"{db_size_bytes / BYTES_PER_KB:.1f} KB"

        email = session.get("email")
        is_logged_in = email is not None and dashboard.is_user_logged_in(email)

        # Instance-wide (not per-user), so it's resolved regardless of
        # login state - it also gates the public "Last.fm Genre Backfill"
        # info card further down the page.
        lastfm_enabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        artist_bio_enabled = dashboard.repo.isArtistBioEnabled()
        album_bio_enabled = dashboard.repo.isAlbumBioEnabled()

        # Get current user's timezone for consistent date display
        current_username = None
        genre_coverage = emptyGenreCoverage()
        genre_unlocked = False
        genre_worker = {"configured": False, "running": False}
        biography_coverage = emptyBiographyCoverage()
        biography_worker = {"artist": {"configured": False, "running": False},
                            "album": {"configured": False, "running": False}}
        if is_logged_in:
            current_username = dashboard.get_username_for_email(email) or dashboard.get_or_create_user(email)
            current_db = dashboard.get_user_db(current_username, email)
            if current_db is not None and lastfm_enabled:
                # All-time coverage: the progress card tracks the whole
                # library, unlike the range-scoped gates on charts/wrapped.
                genre_coverage = resolveGenreCoverage(current_db, None, None)
                genre_unlocked = genreGatePasses(genre_coverage)
                genre_worker = _workerStatus(current_db.getLastfmWorkerStatus, "Last.fm") or genre_worker
            if current_db is not None and (artist_bio_enabled or album_bio_enabled):
                biography_coverage = resolveBiographyCoverage(current_db, current_username)
                biography_worker["artist"] = (
                    _workerStatus(current_db.getLastfmBiographyWorkerStatus, "Last.fm artist biography")
                    or biography_worker["artist"])
                biography_worker["album"] = (
                    _workerStatus(current_db.getLastfmAlbumBiographyWorkerStatus, "Last.fm album biography")
                    or biography_worker["album"])

        # The logged-in user's own sync/backfill state, as a simple
        # three-badge summary - not a table (the full multi-user table
        # with per-account admin controls lives on /admin now).
        your_status = _ownStatusBadges(current_username, current_db) if is_logged_in else None

        # One row per entity kind for the combined "Biography Backfill
        # Progress" card (templates/_biography_progress.html) - built
        # here rather than assembled in Jinja so the template stays a
        # dumb iteration over a pre-shaped list.
        biography_rows = [
            {"label": "Artist", "enabled": artist_bio_enabled, "worker": biography_worker["artist"],
             **biography_coverage["artist"]},
            {"label": "Album", "enabled": album_bio_enabled, "worker": biography_worker["album"],
             **biography_coverage["album"]},
        ]

        return render_template(
            "overview.html",
            global_stats=global_stats,
            global_time_text=global_time_text,
            global_size_text=global_size_text,
            is_logged_in=is_logged_in,
            your_status=your_status,
            spotify_backfill_enabled=dashboard.repo.isSpotifyApiBackfillEnabled(),
            genre_coverage=genre_coverage,
            genre_unlocked=genre_unlocked,
            genre_worker=genre_worker,
            lastfm_enabled=lastfm_enabled,
            biography_rows=biography_rows,
            section="overview"
        )
    app.add_url_rule("/overview", "overviewPage", overviewPage, methods=["GET"])

    @requiresUser
    def dashboardIndex(username, db):

        settings = db.repo.getUserSettings(username)
        default_window = settings.get("default_dashboard_window", "day")

        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        #< resolved the same way /charts and /genres resolve it - see
        #  DateRangeMixin._resolveIntervalParam. Reading the raw param meant an
        #  unrecognised value (a stale or hand-edited URL, one truncated in a
        #  chat client) reached _getDateRange and _getIntervalLabel unchecked,
        #  where default="day" - not the user's configured window - decided
        #  what they got, and the heading then named it confidently. Both
        #  defaults are default_window here: this page has no second default
        #  the way the Top pages do.
        interval = dashboard._resolveIntervalParam(default_window, default_window, customStart, customEnd)

        #< no _getIntervalLabel here: unlike /charts and /genres, neither
        #  tracks.html nor _dashboard_summary.html renders one - the dashboard
        #  names its window with the <select> itself
        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd,
                                                     default=default_window, tz=db.tz)

        # The Time Period filter scopes these summary cards. The searchable play
        # history itself lives on its own /history page now (see historyPage).
        stats = db.getOverallStats(startDate, endDate)

        totalDurationText = msToString(stats["totalDurationMs"],
                                       hideSecondsAboveHours=LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS)

        currentTopSong = dashboard._embedTopSongTextElements(stats["currentTopSongs"][0], sortBy="plays", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopSongs"] else None
        currentTopArtist = dashboard._embedArtistTextElement(stats["currentTopArtists"][0], sortBy="totalTimeListened", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopArtists"] else None

        totalSongsChangeText, totalSongsChangeClass = dashboard._getChangeText(stats["totalSongsPlayed"], stats["previousSongsPlayed"])
        totalListenChangeText, totalListenChangeClass = dashboard._getChangeText(stats["totalDurationMs"], stats["previousDurationMs"])

        summaryArgs = dict(
            totalSongsPlayed=stats["totalSongsPlayed"],
            totalListenTime=totalDurationText,
            totalSongsChangeText=totalSongsChangeText,
            totalSongsChangeClass=totalSongsChangeClass,
            totalListenChangeText=totalListenChangeText,
            totalListenChangeClass=totalListenChangeClass,
            currentTopSong=currentTopSong,
            currentTopArtist=currentTopArtist,
            username=username,
        )

        # The Time Period filter only rescopes these four cards - the live
        # cards below (streak, on this day, discover, calendar) and next-
        # milestones are unfiltered, so a filter change re-renders just this one
        # partial and skips every query below entirely.
        #
        # htmx drives that swap, so the marker is its HX-Request header rather
        # than the ?ajax=true this page used to send - which also keeps the
        # marker out of the URL bar, since hx-replace-url writes the requested
        # URL back to the address bar. The fragment itself, not a JSON envelope
        # around it: htmx swaps the response body straight into
        # #dashboardSummary, so a {"summaryHtml": ...} wrapper would land in the
        # page as literal JSON text. See tests/test_dashboard_htmx.py.
        #
        # Unlike /history and the Top pages this is NOT a two-phase shell - the
        # same partial is rendered inline below, so there is no first load to
        # defer and no placeholder to trigger one from.
        if isHtmxSwap():
            return render_template("_dashboard_summary.html", **summaryArgs)

        # Unfiltered dashboard cards (independent of the interval/date-range
        # filter above): live streak and "on this day" resurfacing are cheap
        # and rendered inline. The Discover card's genre-coverage gate and
        # recommendations are full-history queries (~700ms combined on a large
        # library - see dashboardDiscover) so they're fetched by the page's
        # own JS after first paint instead of blocking this render.
        currentStreak = db.getCurrentStreak()
        onThisDay = db.getOnThisDay(limit=ON_THIS_DAY_YEARS_LIMIT)
        lastfmGenreEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()
        # Streak calendar: ~1 year of daily play counts, rendered inline below
        # the live cards. Comparable cost to getCurrentStreak above (a similar
        # bounded bucket scan), so it rides along in this render rather than
        # being deferred like the full-history Discover card.
        listeningCalendar = db.getListeningCalendar()

        # The milestones row: what's been earned, beside the progress bars
        # toward what's next. Both are gated on the admin kill switch - an
        # instance with the feature off recorded nothing, so advertising
        # progress toward milestones it will never grant was misleading, and
        # the queries behind it are pure waste there.
        #
        # getMilestonesForUser is an indexed read, measured at ~0.02ms against a
        # real 131k-play db next to the ~15ms getPlayTotals below already costs.
        # The threshold kinds cap out at 21 rows/user (9 plays + 7 listen-time +
        # 5 streak); top_artist appends one row per change of #1 artist, so the
        # table is slow-growing rather than strictly bounded - still nowhere
        # near enough to defer the way the Discover card is deferred.
        milestones = []
        nextMilestones = []
        if dashboard.repo.isMilestonesEnabled():
            milestones = [
                {**formatMilestone(row), "dateText": dateToString(row["achieved_at"], tz=db.tz)}
                for row in dashboard.repo.getMilestonesForUser(username)
            ]
            # Freeze the badge count BEFORE acknowledging, or the topbar badge
            # never renders on this page: context processors run after the view,
            # so _injectMilestoneStatus would count what markMilestonesSeen just
            # cleared. See primeMilestoneBadge.
            dashboard.primeMilestoneBadge(username)
            # Reaching this render means the cards are about to show them -
            # same acknowledgment pattern as the accepted-share notification.
            dashboard.repo.markMilestonesSeen(username)

            # "Next milestones" progress bars: lifetime totals against the same
            # thresholds detection uses. getPlayTotals is a single COUNT+SUM
            # scan; removing the play-history list from this page more than
            # pays for it.
            totalPlays, totalMs = db.getPlayTotals(None, None)
            streakDays = currentStreak.get("days", 0) if isinstance(currentStreak, dict) else 0
            nextMilestones = buildNextMilestones(totalPlays, (totalMs or 0) // MS_PER_HOUR, streakDays)

        return render_template(
            "tracks.html",
            currentStreak=currentStreak,
            onThisDay=onThisDay,
            listeningCalendar=listeningCalendar,
            milestones=milestones,
            nextMilestones=nextMilestones,
            lastfmGenreEnabled=lastfmGenreEnabled,
            friends_now_playing_enabled=dashboard.repo.isFriendsNowPlayingEnabled(),
            section="dashboard",
            interval=interval,
            customStart=customStart,
            customEnd=customEnd,
            #< no defaultWindow: it existed only as the popstate handler's
            #  fallback for a Back navigation onto a bare URL, and nothing here
            #  pushes a history entry any more - every URL update replaces
            **summaryArgs,
        )
    app.add_url_rule("/", "dashboard", dashboardIndex, methods=["GET"])

    def _historyFilters(db, username):
        """The History page's filter-card state, read once - mirrors
        _topListFilters, which the three Top pages already share (CORE-10
        part 3, 2026-09-02 review)."""
        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")

        # History defaults to All Time (the full list); the Time Period filter
        # then scopes it to any named interval or a custom range. Resolved
        # like dashboardIndex's (see DateRangeMixin._resolveIntervalParam):
        # the DATA was safe either way (_getDateRange coerces junk to the
        # default), but the raw value reached the template and
        # _buildPaginationContext, leaving the select unselected and junk in
        # every page link on a stale URL. Both defaults are "all time" here -
        # this page has no second default the way the Top pages do.
        interval = dashboard._resolveIntervalParam("all time", "all time", customStart, customEnd)

        sortOrder = dashboard._getHistorySortParam()

        # The tag filter mirrors the Top pages' (see _topListFilters): gated on
        # the admin's instance-wide tags kill switch, so a hand-crafted ?tag=
        # is ignored (the dropdown is already hidden template-side) and the
        # getUserTags query is skipped when tags are off.
        tagsOn = dashboard.repo.isTagsEnabled()
        tag = request.args.get("tag", "") if tagsOn else ""
        userTags = db.repo.getUserTags(username) if tagsOn else []

        # "Full plays only", the same control and the same ?fullOnly=1|0 the Top
        # pages carry (see _topListFilters) - a COMPLETION test, not a skip test.
        # A tri-state where ABSENT means the default, which is on, so only an
        # explicit "0" opts out.
        #
        # Coerced rather than echoed back raw: this page builds its URLs from
        # validated values, and ?fullOnly=bogus reaching listUrl and every page
        # link would assert junk in the one place a reader would trust (the same
        # rule test_the_first_load_url_holds_no_unvalidated_input pins for
        # ?interval=). _topListFilters still echoes its raw value.
        fullOnly = "0" if request.args.get("fullOnly") == "0" else "1"

        return {
            "searchQuery": request.args.get("q", ""),
            "interval": interval,
            "customStart": customStart,
            "customEnd": customEnd,
            "sort": sortOrder,
            "oldestFirst": sortOrder == "oldest",
            "tag": tag,
            "userTags": userTags,
            "fullOnly": fullOnly,
            "fullPlaysOnly": fullOnly != "0",
        }

    def _historyShell(username, filters):
        """The plain GET half of History's two-phase load: the filter card
        plus an empty #historyResults placeholder. Mirrors _topListShell
        (CORE-10 part 3, 2026-09-02 review)."""
        # The URL the shell's placeholder fetches the list from. Built from
        # the VALIDATED values rather than echoed back from
        # request.full_path, which is the same rule the pagination links
        # already follow (see _buildPaginationContext below): ?interval=bogus
        # is coerced to the default for the query itself, so reflecting the
        # raw value into the markup would assert it again in the one place a
        # reader would trust, and disagree with every link built beside it.
        # Pinned by test_an_unrecognized_interval_renders_as_the_default_not_raw.
        #
        # A custom range's dates ride along only when the range is actually
        # in effect, matching the disabled date inputs in the shell - see
        # the note on `disabled` in templates/history.html.
        listArgs = {
            "q": filters["searchQuery"],
            "interval": filters["interval"],
            "startDate": filters["customStart"] if filters["interval"] == "custom" else "",
            "endDate": filters["customEnd"] if filters["interval"] == "custom" else "",
            "sort": filters["sort"] if filters["sort"] == "oldest" else "",
            "tag": filters["tag"],
            #< unconditional, unlike the filters above: BOTH "1" and "0" are
            #  non-empty strings so the `if v` below keeps them, and that is
            #  required rather than incidental - an absent fullOnly means the
            #  default, so omitting it here would make the first load
            #  disagree with a checkbox the user can see is off
            "fullOnly": filters["fullOnly"],
            #< a junk or out-of-range page is clamped by _calculatePagination
            #  on the list request; this only keeps a shared ?page=3 working,
            #  so anything that isn't a page number is simply left out
            "page": _positivePageArg(),
        }
        return render_template(
            "history.html",
            username=username,
            section="history",
            interval=filters["interval"],
            customStart=filters["customStart"],
            customEnd=filters["customEnd"],
            sort=filters["sort"],
            tag=filters["tag"],
            user_tags=filters["userTags"],
            fullPlaysOnly=filters["fullPlaysOnly"],
            listUrl=url_for("history", **{k: v for k, v in listArgs.items() if v}),
        )

    @requiresUser
    def historyPage(username, db):
        """The searchable, paginated play-history list - split out of the
        dashboard so that page can stay a glanceable overview. Carries the same
        search + Time Period filter the dashboard used to host, and the same
        list-scoping rule: only an explicit custom range (a chart click-through)
        scopes the list; named intervals don't."""
        filters = _historyFilters(db, username)
        interval = filters["interval"]
        customStart = filters["customStart"]
        customEnd = filters["customEnd"]
        sortOrder = filters["sort"]
        oldestFirst = filters["oldestFirst"]
        tag = filters["tag"]
        fullOnly = filters["fullOnly"]
        fullPlaysOnly = filters["fullPlaysOnly"]

        # Lightweight shell, same two-phase load as /compare, /charts, /genres:
        # the initial GET renders just the filter controls + an empty results
        # placeholder, and the list (plus pagination strip) arrives in a second
        # request right after first paint, and again on every search/filter/page
        # change.
        #
        # This page drives that second request with htmx rather than its own
        # fetch() code, so the marker is htmx's own HX-Request header instead of
        # the ?ajax=true convention the other shell pages still use. Keying on
        # the header rather than a query param is also what keeps ?ajax=true out
        # of the URL bar: hx-replace-url writes back the URL that was requested,
        # so a marker living in the query string would become part of the page's
        # shareable address. See tests/test_history_htmx.py.
        if not isHtmxSwap():
            return _historyShell(username, filters)

        searchQuery = filters["searchQuery"]

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default="all time", tz=db.tz)

        # The Time Period filter scopes the list for every interval, not just
        # custom ranges: "Last Week" shows last week's plays, "All Time" (the
        # default) resolves to (None, None) i.e. the full history.
        listStartDate = startDate
        listEndDate = endDate

        # Same expand-outward semantics as the Top Songs tag filter
        # (getTaggedTrackIds also matches a track via its tagged album/artist).
        trackIds = db.repo.getTaggedTrackIds(username, [tag]) if tag else None

        # One checkbox, two filters, coupled HERE and nowhere below: ticking it
        # asks for plays that finished, and a skip is not one of those, so the
        # off state has to drop the skip filter too or /history would still be
        # hiding rows with every box unticked. They stay separate parameters in
        # the layers below because the song detail page's Show Skips toggle
        # drives includeSkips on its own - merging them there would break it.
        listFilters = {"includeSkips": not fullPlaysOnly, "fullPlaysOnly": fullPlaysOnly}

        if searchQuery:
            # Matching and pagination both happen in SQL (Repository.searchPlays)
            # instead of fetching every play ever recorded and filtering in Python.
            totalCount = db.searchEntriesCount(searchQuery, startDate=listStartDate, endDate=listEndDate,
                                               trackIds=trackIds, **listFilters)
            page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
            tracks = db.searchEntries(searchQuery, count=PAGE_SIZE, startIndex=startIndex,
                                      startDate=listStartDate, endDate=listEndDate,
                                      oldestFirst=oldestFirst, trackIds=trackIds, **listFilters)
        else:
            # Only materialize the page being shown - joining full track
            # metadata onto every entry ever recorded on every request gets
            # slow once the history grows large.
            totalCount = db.getEntriesCount(startDate=listStartDate, endDate=listEndDate, trackIds=trackIds,
                                            **listFilters)
            page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
            fetchEntries = db.getEntriesFromOld if oldestFirst else db.getEntriesFromNew
            tracks = fetchEntries(count=PAGE_SIZE, startIndex=startIndex,
                                  startDate=listStartDate, endDate=listEndDate, trackIds=trackIds,
                                  **listFilters)
        tracks = dashboard._embedSongsTextElements(tracks)
        tracks = dashboard._attachGenres(db, tracks, "track")
        #< the Partial/Skipped chips. Only meaningful once the filter above is
        #  off - with it on every row is a full play - but attached either way,
        #  since the template decides what is worth a chip, not the route
        tracks = dashboard._attachPlayTypes(tracks)

        pagination = dashboard._buildPaginationContext(
            "history",
            page,
            totalPages,
            totalCount,
            q=searchQuery,
            interval=interval,
            startDate=customStart,
            endDate=customEnd,
            sort=sortOrder if oldestFirst else None,
            tag=tag,
            #< _buildPageUrl drops only None and "", so the string "0" rides into
            #  every page link - which it must, or page 2 silently re-enables the
            #  filter the user turned off
            fullOnly=fullOnly,
        )

        creds = db.getUserSpotifyCredentials() or {}
        is_authenticated = bool(creds.get("refresh_token"))

        # The fragment itself, not a JSON envelope around it: htmx swaps the
        # response body straight into #historyResults, so a {"resultsHtml": ...}
        # wrapper would land in the page as literal JSON text.
        return render_template(
            "_history_results.html",
            tracks=tracks,
            startIndex=startIndex,
            interval=interval,
            searchQuery=searchQuery,
            tag=tag,
            is_authenticated=is_authenticated,
            username=username,
            **pagination,
        )
    app.add_url_rule("/history", "history", historyPage, methods=["GET"])

    @requiresUser(api=True)
    def dashboardDiscover(username, db):
        """The dashboard's Discover card, loaded by htmx after first paint (see
        tracks.html) rather than computed inline - the genre-coverage gate check
        and recommendation query are full-history scans that noticeably slowed
        the dashboard once added.

        Markup, not JSON: `unlocked` was a flag the client turned into one of
        three pre-rendered-and-hidden paragraphs, and the recommendations were
        rows it built element by element. Both are Jinja's job.

        Still api=True, so an expired session gets a plain 401 rather than
        unauthenticatedResponse's HX-Redirect: htmx reports the error and swaps
        nothing, which leaves this ONE card showing its placeholder. The
        alternative navigates the whole dashboard away because a background card
        load failed."""

        if not dashboard.repo.isLastfmGenreBackfillEnabled():
            return render_template("_dashboard_discover.html", unlocked=False, recommendations=[])

        unlocked = genreGatePasses(resolveGenreCoverage(db, None, None))
        recommendations = []
        if unlocked:
            recommendations = db.getRecommendedArtists(
                # Admin-tunable, read live per request; falls back to the code default.
                limit=dashboard.repo.getDiscoverArtistLimit(RECOMMENDATION_ARTIST_LIMIT),
                genrePool=RECOMMENDATION_GENRE_POOL,
                excludeTopN=RECOMMENDATION_EXCLUDE_TOP_N,
            )
        return render_template("_dashboard_discover.html", username=username,
                               unlocked=unlocked, recommendations=recommendations)
    app.add_url_rule("/api/dashboard-discover", "dashboardDiscover", dashboardDiscover, methods=["GET"])

    @requiresUser(api=True)
    def dashboardTrends(username, db):
        """The dashboard's Obsession, Rediscovery, and Forgotten Favorite trend
        cards, loaded by htmx after first paint.

        The partial was always the whole answer; it just used to travel as a
        string inside {"trendsHtml": ..., "trends": ...}, of which the client
        read one key and ignored the other. See dashboardDiscover for why this
        stays api=True."""

        return render_template("_dashboard_trends.html", username=username,
                               trends=db.getDashboardTrends())
    app.add_url_rule("/api/dashboard-trends", "dashboardTrends", dashboardTrends, methods=["GET"])

    @requiresUser
    def topSongsPage(username, db):
        return dashboard._topListPage(username, db, "top_songs")
    app.add_url_rule("/top-songs", "topSongsPage", topSongsPage, methods=["GET"])

    @requiresUser
    def topAlbumsPage(username, db):
        return dashboard._topListPage(username, db, "top_albums")
    app.add_url_rule("/top-albums", "topAlbumsPage", topAlbumsPage, methods=["GET"])

    @requiresUser
    def topArtistsPage(username, db):
        return dashboard._topListPage(username, db, "top_artists")
    app.add_url_rule("/top-artists", "topArtistsPage", topArtistsPage, methods=["GET"])

    @requiresUser
    def chartsPage(username, db):

        settings = db.repo.getUserSettings(username)
        defaultWindow = settings.get("default_dashboard_window", "day")

        customStart = request.args.get("startDate", "")
        customEnd = request.args.get("endDate", "")
        #< resolved like dashboardIndex above (see
        #  DateRangeMixin._resolveIntervalParam): ?interval= PRESENT and
        #  empty must not fall through unvalidated - the template's <select>
        #  compares against this variable and its All Time option only
        #  matches the "all time" spelling, so an empty ?interval= used to
        #  leave every option unselected and the control displayed the first
        #  one, Today, over default-window numbers. Both defaults are
        #  defaultWindow here; this page has no second default the way the
        #  Top pages do.
        interval = dashboard._resolveIntervalParam(defaultWindow, defaultWindow, customStart, customEnd)
        #< the raw param, not the resolved bucketing - the template's select
        #  must keep showing Auto rather than pinning the derived value
        groupByParam = request.args.get("groupBy", "")

        startDate, endDate = dashboard._getDateRange(interval, customStart, customEnd, default=defaultWindow, tz=db.tz)
        spanStart, spanEnd = startDate, endDate
        if spanStart is None or spanEnd is None:
            spanStart, spanEnd = dashboard._playRangeSpanDates(username, db.tz)   #< "All Time" has no explicit range
        groupBy = dashboard._resolveGroupBy(groupByParam, spanStart, spanEnd)
        #< same default as the _getDateRange call above, or the heading can
        #  name a different window than the data covers
        intervalLabel = dashboard._getIntervalLabel(interval, customStart, customEnd,
                                                    default=defaultWindow)

        isSingleDayView = isSingleDayInterval(interval)
        lastDayDate = startDate.strftime("%Y-%m-%d") if isSingleDayView and startDate else None

        # The admin's instance-wide kill switch: checked before spending any
        # genre queries, and the whole Top Genres section hides on the template
        # side when this is False. Cheap instance setting, so it's resolved for
        # both the shell and the ajax payload.
        lastfmEnabled = dashboard.repo.isLastfmGenreBackfillEnabled()

        # Lightweight shell: the filter card renders immediately and htmx
        # fetches the chart card below after first paint (and on every filter
        # change), so none of the heavy per-range queries block the initial
        # load. Same two-phase shape /history and the Top pages use, and the
        # same marker: htmx's own HX-Request header rather than ?ajax=true,
        # which kept the marker out of the URL bar (hx-replace-url writes the
        # requested URL back to the address bar). See tests/test_charts_htmx.py.
        if not isHtmxSwap():
            # Built from the VALIDATED filter values rather than echoed from
            # request.full_path - the rule every migrated shell follows: junk is
            # coerced for the query itself, so reflecting it into the markup
            # would assert it again in the one place a reader would trust. The
            # custom dates ride along only when the range is actually in effect,
            # matching the disabled date inputs in the shell.
            resultsArgs = {
                "interval": interval,
                "groupBy": dashboard._getValidGroupBy(groupByParam, default=""),
                "startDate": customStart if interval == "custom" else "",
                "endDate": customEnd if interval == "custom" else "",
            }
            return render_template(
                "charts.html",
                username=username,
                section="charts",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                groupBy=groupByParam,
                isSingleDayView=isSingleDayView,
                lastfmEnabled=lastfmEnabled,
                resultsUrl=url_for("chartsPage",
                                   **{k: v for k, v in resultsArgs.items() if v}),
            )

        timeSeriesGroupBy = "hour" if isSingleDayView else groupBy

        # The timeline and the heatmap are two different local-time views of
        # the SAME pre-aggregated rows, so the aggregate runs once here rather
        # than once inside each.
        bucketRows = db.getPlayBuckets(startDate=startDate, endDate=endDate)
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(startDate=startDate, endDate=endDate, groupBy=timeSeriesGroupBy,
                                       bucketRows=bucketRows),
            groupBy=timeSeriesGroupBy,
        )
        heatmap = dashboard._embedHeatmapTextElements(
            db.getHourOfDayHeatmap(startDate=startDate, endDate=endDate, bucketRows=bucketRows))
        artistTrend = None if isSingleDayView else db.getArtistTrend(startDate=startDate, endDate=endDate, topN=CHART_ARTIST_TREND_TOP_N, groupBy=groupBy)

        explicitRatio = db.getExplicitRatio(startDate=startDate, endDate=endDate)
        # Flask's JSON provider sorts dict keys alphabetically on
        # serialization (app.json.sort_keys, on by default) - a {label:
        # value} dict handed to |tojson loses whatever order the SQL
        # produced. A JSON array preserves element order regardless, so
        # both bar-chart datasets are shipped as [label, value] pairs
        # instead (see renderCategoryBarChart in charts.js).
        decadeDistribution = list(db.getReleaseDecadeDistribution(startDate=startDate, endDate=endDate).items())
        completionStats = db.getCompletionStats(startDate=startDate, endDate=endDate)
        # Aggregated over ALL plays in range, skips included - same scope as
        # completionStats above (this is not the explicit-ratio card, which
        # filters is_skip=0). buildListeningBehavior does the bucketing/ratio
        # maths; the route only fetches the raw counts and hands them over.
        listeningBehavior = buildListeningBehavior(
            db.getListeningBehavior(startDate=startDate, endDate=endDate))
        # "How often do I skip" is the donut above; these answer "what do I
        # skip". Ranked by shrunk rate - see Repository.getMostSkippedTracks.
        mostSkippedSongs = db.getMostSkippedSongs(
            startDate=startDate, endDate=endDate, limit=CHART_MOST_SKIPPED_LIMIT)
        mostSkippedArtists = db.getMostSkippedArtists(
            startDate=startDate, endDate=endDate, limit=CHART_MOST_SKIPPED_LIMIT)

        genreCoverage = emptyGenreCoverage()
        genreUnlocked = False
        genreDistribution = None
        # Whether THIS user has a Last.fm key, not just whether the admin's
        # instance-wide toggle is on - only needed for the locked branch,
        # which is the only one that renders _genre_progress.html (2026-09-02
        # review, UT-12 follow-up).
        lastfmConfigured = False
        if lastfmEnabled:
            genreCoverage = resolveGenreCoverage(db, startDate, endDate)
            genreUnlocked = genreGatePasses(genreCoverage)
            if genreUnlocked:
                distribution = resolveGenreDistribution(db, startDate, endDate,
                                                        CHART_TOP_GENRES_LIMIT)
                # Most-played first, like every other genre surface
                # (Wrapped/Compare): the section is called Top Genres, so the
                # top one belongs in the first row rather than at the bottom of
                # a chart that climbs toward it.
                genreDistribution = list(distribution.items())
            else:
                lastfmConfigured = userHasLastfmKey(dashboard.repo, db)

        # The chart card as markup, with every series riding inside it as one
        # JSON data island (see _charts_results.html). What used to be fifteen
        # JSON keys is now three kinds of thing, each handled where it belongs:
        # the two headings' text and the Top Genres section's range-scoped
        # locked/unlocked body are rendered here rather than assembled by the
        # client; whether the artist-trend section exists at all is a {% if %}
        # rather than a style.display the client sets afterwards; and the nine
        # datasets - which htmx cannot swap, because they are drawn onto
        # canvases - stay data.
        return render_template(
            "_charts_results.html",
            intervalLabel=intervalLabel,
            lastDayDate=lastDayDate,
            artistTrend=artistTrend,
            lastfmEnabled=lastfmEnabled,
            lastfmConfigured=lastfmConfigured,
            genreUnlocked=genreUnlocked,
            genreCoverage=genreCoverage,
            #< exactly the window.__chartData charts.js reads - the client used
            #  to rebuild this object key by key from the envelope
            chartData={
                "interval": interval,
                "groupBy": groupBy,
                "timeSeries": timeSeries,
                "heatmap": heatmap,
                "artistTrend": artistTrend,
                "explicitRatio": explicitRatio,
                "decadeDistribution": decadeDistribution,
                "completionStats": completionStats,
                "listeningBehavior": listeningBehavior,
                "mostSkippedSongs": mostSkippedSongs,
                "mostSkippedArtists": mostSkippedArtists,
                "genreDistribution": genreDistribution,
            },
        )
    app.add_url_rule("/charts", "chartsPage", chartsPage, methods=["GET"])
