# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Song, artist, and album detail routes."""

from flask import render_template, redirect, request, url_for, jsonify, Response

from config import PAGE_SIZE
from routes._auth import makeRequiresUser
from routes._htmx import isHtmxSwap
from routes.pagination import positivePageArg as _positivePageArg


# The detail pages' htmx swap targets: the id of the element the second (third,
# fourth) request is filling, which is what htmx puts in its HX-Target header.
#
# These routes are the only ones in the app with MORE THAN ONE htmx mode, so the
# HX-Request header alone cannot say which fragment is wanted. HX-Target can, and
# it costs nothing extra: htmx already sends it, and unlike the ?ajax= marker it
# replaces, it never becomes part of the URL. That matters here because the play
# log's swaps carry hx-replace-url, which writes the requested URL back to the
# address bar - a marker in the query string would end up in every link a visitor
# copies. The one mode that is NOT htmx keeps ?ajax=true: it answers the
# Trend-buckets select with chart DATA for a canvas (see detail-chart.js), and
# htmx swaps markup, not data.
DETAIL_BODY_TARGET = "detailBody"            #< the whole deferred body
DETAIL_HISTORY_TARGET = "detailHistoryResults"   #< the play log, re-sorted/paged
DETAIL_MORE_TARGET = "timelineActions"       #< "Show more": one appended batch

#< How many pages' worth of rows one ?limit= may ask for. The detail history's
#  "Show more" grows its batch, so limit legitimately exceeds PAGE_SIZE - but it
#  had no ceiling at all, so ?limit=500000 fetched and rendered half a million
#  rows. Own data only, so this is a footgun rather than a hole, and every other
#  pager is already clamped. (?offset= and ?page= beside it had none either, and
#  overflowed sqlite3's bind into a 500 - see _detailHistoryContext.)
MAX_DETAIL_HISTORY_PAGES = 10

#< Entity kinds whose History tab swaps to the song page's timeline when the
#  entity has only ever been heard via one track - see _entityDetailPage's
#  singleTrackTimeline. Started album-only (a fixed, small track set where
#  "only ever played track 3" is an ordinary state); artists joined
#  2026-09-07 for the same reason - consecutive rows would otherwise repeat
#  one title, artist and cover down the page regardless of which entity kind
#  is being viewed.
SINGLE_TRACK_TIMELINE_KINDS = ("album", "artist")


def _detailSwapTarget():
    """Which region of a detail page an htmx request is asking to fill, or ""
    for a plain page load.

    Falls back to the whole body when htmx sends no HX-Target (it only omits the
    header for a target with no id, which none of ours is): answering an
    HX-Request with the SHELL would swap a whole document into a region, which
    is the one outcome worth ruling out by construction.
    """
    if not isHtmxSwap():
        return ""
    return request.headers.get("HX-Target") or DETAIL_BODY_TARGET


def register(app, dashboard):
    requiresUser = makeRequiresUser(dashboard)

    def _songHistoryContext(db, endpoint, linkArgs, groupByParam, trackId, trackDurationMs):
        """_detailHistoryContext's song-detail arm: offset/limit "Show more"
        batching, capped at MAX_DETAIL_HISTORY_PAGES rather than paged like
        the artist/album arm below, plus the sort and skips toggles (CORE-10
        part 5, 2026-09-02 review)."""
        sortOrder = dashboard._getHistorySortParam()
        oldestFirst = sortOrder == "oldest"
        skipsParam = request.args.get("skips", "true").lower()
        showSkips = skipsParam != "false"

        #< _positivePageArg rather than a bare int(): its digit cap is what
        #  keeps (page - 1) * PAGE_SIZE - and the OFFSET it turns into -
        #  inside what sqlite3 will bind. Junk and absurd pages start at the
        #  first page, as they always did.
        pageParam = _positivePageArg()
        defaultOffset = (int(pageParam) - 1) * PAGE_SIZE if pageParam else 0

        try:
            offset = max(0, int(request.args.get("offset", defaultOffset)))
        except (ValueError, TypeError):
            offset = defaultOffset

        try:
            limit = min(PAGE_SIZE * MAX_DETAIL_HISTORY_PAGES,
                        max(1, int(request.args.get("limit", PAGE_SIZE))))
        except (ValueError, TypeError):
            limit = PAGE_SIZE

        totalCount = db.getEntriesCount(trackId=trackId, includeSkips=showSkips)
        #< An in-range offset (< totalCount) passes through untouched - it
        #  already names a real row. Only an OUT-OF-RANGE one - past the
        #  end, or above 2**63-1 (an OverflowError out of sqlite3's bind
        #  and a 500, the same footgun MAX_DETAIL_HISTORY_PAGES closed for
        #  ?limit=) - gets clamped, and it snaps to the LAST BATCH
        #  boundary rather than to totalCount itself: totalCount is a
        #  count, not a valid offset into the rows, so the previous fix
        #  landed one row past the end and rendered the empty "nothing
        #  more" batch for a song that has plays - ?page=999999 or
        #  ?page=3 of 2 did this every time (UT-2, 2026-09-02 review).
        #  The largest multiple of PAGE_SIZE below totalCount is where
        #  the real last batch starts; 0 when there are no rows at all.
        if offset >= totalCount:
            offset = 0 if totalCount == 0 else ((totalCount - 1) // PAGE_SIZE) * PAGE_SIZE
        fetchEntries = db.getEntriesFromOld if oldestFirst else db.getEntriesFromNew
        plays = fetchEntries(count=limit, startIndex=offset,
                             trackId=trackId, includeSkips=showSkips)
        # "Show more" APPENDS its batch below the rows already on screen, so a
        # later batch's first row has a predecessor the enrichment cannot see -
        # it repeated the month header that row already sat under and dropped
        # its gap badge. One row, taken in DISPLAY order (which flips with
        # ?sort=oldest, and fetchEntries already reflects that), is that
        # predecessor. Only for a batch that HAS one: offset 0 opens the list.
        previousPlay = None
        if offset > 0:
            seedRows = fetchEntries(count=1, startIndex=offset - 1,
                                    trackId=trackId, includeSkips=showSkips)
            previousPlay = seedRows[0] if seedRows else None
        plays = dashboard._embedSongsTextElements(plays)
        plays = dashboard._enrichSongTimelineEntries(plays, trackDurationMs=trackDurationMs,
                                                     previousPlay=previousPlay)

        hasMore = (offset + len(plays)) < totalCount
        nextOffset = offset + len(plays)
        remainingCount = max(0, totalCount - nextOffset)
        nextBatchSize = min(PAGE_SIZE, remainingCount)

        sharedArgs = dict(linkArgs, groupBy=groupByParam,
                          sort=sortOrder if oldestFirst else None,
                          skips="false" if not showSkips else None)
        sortToggleArgs = dict(sharedArgs, sort=None if oldestFirst else "oldest", offset=0)
        skipsToggleArgs = dict(sharedArgs, skips="false" if showSkips else "true", offset=0)
        # "Show more" asks for the batch AFTER the rows already on screen and
        # appends it, so the URL carries an offset rather than a larger limit
        # - a grown limit would hit the MAX_DETAIL_HISTORY_PAGES ceiling above
        # and silently stop advancing. The batch it fetches renders this same
        # context, so the control it brings back builds its own next URL the
        # same way; there is no client-side offset bookkeeping left.
        showMoreArgs = dict(sharedArgs, offset=nextOffset)

        return {
            "plays": plays,
            "historyPartial": "_play_log.html",
            "totalCount": totalCount,
            "offset": offset,
            "hasMore": hasMore,
            "nextOffset": nextOffset,
            "nextBatchSize": nextBatchSize,
            "remainingCount": remainingCount,
            "sortOldest": oldestFirst,
            "showSkips": showSkips,
            "isSongDetail": True,
            "sortToggleUrl": dashboard._buildPageUrl(endpoint, 1, **sortToggleArgs),
            "skipsToggleUrl": dashboard._buildPageUrl(endpoint, 1, **skipsToggleArgs),
            "showMoreUrl": dashboard._buildPageUrl(endpoint, 1, **showMoreArgs),
        }

    def _entityHistoryContext(db, endpoint, linkArgs, groupByParam, trackId, artistId, albumId,
                              singleTrackTimeline):
        """_detailHistoryContext's artist/album arm: ordinary paged history,
        optionally rendered as the song page's timeline when the entity has
        only ever been heard via one track (CORE-10 part 5, 2026-09-02
        review)."""
        sortOrder = dashboard._getHistorySortParam()
        oldestFirst = sortOrder == "oldest"
        totalCount = db.getEntriesCount(trackId=trackId, artistId=artistId, albumId=albumId)
        page, totalPages, startIndex = dashboard._calculatePagination(totalCount)
        fetchEntries = db.getEntriesFromOld if oldestFirst else db.getEntriesFromNew
        plays = fetchEntries(count=PAGE_SIZE, startIndex=startIndex,
                             trackId=trackId, artistId=artistId, albumId=albumId)
        plays = dashboard._embedSongsTextElements(plays)
        if singleTrackTimeline:
            # The timeline needs more than a play-type label: its rows are
            # grouped by month and separated by the gap since the previous
            # play, and _enrichSongTimelineEntries is what attaches all three.
            # No trackDurationMs to pass - every row here carries its own
            # `duration` from the catalog, which that function prefers anyway.
            plays = dashboard._enrichSongTimelineEntries(plays)
        else:
            #< the artist/album History tab renders the same card /history does
            #  (_detail_history_results.html -> _track_card.html,
            #  section='history'), and shows partial plays for the same reason:
            #  this list is not filtered by completion. Without this the two
            #  surfaces would label the same row differently.
            plays = dashboard._attachPlayTypes(plays)
        sharedArgs = dict(linkArgs, groupBy=groupByParam, sort=sortOrder if oldestFirst else None)
        return {
            "plays": plays,
            "historyPartial": ("_detail_history_timeline.html" if singleTrackTimeline
                               else "_detail_history_results.html"),
            "startIndex": startIndex,
            "sortOldest": oldestFirst,
            "isSongDetail": False,
            "sortToggleUrl": dashboard._buildPageUrl(endpoint, 1, **dict(sharedArgs, sort=None if oldestFirst else "oldest")),
            **dashboard._buildPaginationContext(endpoint, page, totalPages, totalCount, **sharedArgs),
        }

    def _detailHistoryContext(db, endpoint, linkArgs, groupByParam="",
                               trackId=None, artistId=None, albumId=None,
                               trackDurationMs=None, singleTrackTimeline=False):
        """The detail pages' play-history list context: one sorted+paginated
        page of the item's individual plays, the Date-sort toggle URL, and
        _pagination.html's context. `linkArgs` are the endpoint kwargs every
        list URL must carry (the item id, plus view=history for the artist/
        album tabs); groupBy rides along in every URL so list navigation
        never resets the Trend-buckets chart selection.

        Also returns `historyPartial`, the template that renders the list.
        Which one it is depends on the data, not on the page - an album played
        from a single track wants the timeline the song page uses - so it is
        decided here, where the shape of the context is decided, rather than
        being hardcoded per body template as it used to be.

        `singleTrackTimeline` asks for that swap: every play in this list is
        the same track, so cards would repeat one title, artist and cover down
        the page. It only affects the artist/album branch; the song page is a
        single track's log by construction and already renders the timeline.

        A thin dispatcher onto _songHistoryContext / _entityHistoryContext
        (CORE-10 part 5, 2026-09-02 review): the two arms share nothing but
        `endpoint`/`linkArgs`/`groupByParam`, and had grown different enough
        (a "Show more" batch vs. a real page, an offset vs. a page number)
        that keeping them as one function was hiding the split, not avoiding
        one."""
        if trackId is not None and artistId is None and albumId is None:
            return _songHistoryContext(db, endpoint, linkArgs, groupByParam, trackId, trackDurationMs)
        return _entityHistoryContext(db, endpoint, linkArgs, groupByParam, trackId, artistId, albumId,
                                     singleTrackTimeline)

    def _missingEntityResponse(endpoint):
        """The answer for a detail URL whose entity no longer resolves.

        A plain GET redirects, as it always has. A second request must NOT, and
        for the same reason in both flavours: the client follows a 302
        transparently, lands on the top-list page's 200 HTML, and inlines it -
        htmx would swap a whole page into #detailBody, and the old fetch() passed
        resp.ok and then threw in resp.json(), so the visitor got "couldn't load"
        plus a Retry that behaved identically instead of being taken to the list.
        Reachable via a shared or bookmarked URL for an entity an overwrite
        import removed between the shell request and the body request.

        Each client gets the escape it understands natively: HX-Redirect + 204
        for htmx (204 rather than 404 because htmx swaps the body of any 2xx and
        reports a 4xx as an error - No Content leaves it nothing to inject and
        nothing to complain about), and the 404 + redirectUrl body for the one
        mode still on fetch(). Both shaped like unauthenticatedResponse's, so
        there is one convention for "go here instead"."""
        target = url_for(endpoint)
        if isHtmxSwap():
            return Response(status=204, headers={"HX-Redirect": target})
        if request.args.get("ajax"):
            return jsonify(redirectUrl=target), 404
        return redirect(target)

    def _detailBodyUrl(endpoint, urlArgs, groupByParam, songDetail):
        """The URL the shell's placeholder loads the deferred body from.

        Built from the VALIDATED page state rather than echoed back from
        request.full_path - the same rule the pagination links follow, and the
        one historyPage and _topListShell already carry: junk is coerced for the
        query itself, so reflecting it into the markup would assert it again in
        the one place a reader would trust. ?groupBy=bogus is the visible case,
        because the Trend-buckets select printed beside this already renders as
        Auto for it.

        Only state that survives a reload rides along. offset/limit are "Show
        more" bookkeeping the address bar has never carried, so a reload has
        always started from the first batch and still does."""
        args = dict(urlArgs,
                    groupBy=dashboard._getValidGroupBy(groupByParam, default=""),
                    #< a junk or out-of-range page is clamped on the body request
                    #  (_calculatePagination) / turned into an offset for the song
                    #  page; this only keeps a shared ?page=3 working
                    page=_positivePageArg())
        sortOrder = dashboard._getHistorySortParam()
        if sortOrder == "oldest":
            args["sort"] = sortOrder
        if songDetail:
            #< the only opt-out worth carrying: the play log shows skips by default
            if request.args.get("skips", "").lower() == "false":
                args["skips"] = "false"
        elif dashboard._getDetailViewParam() == "history":
            args["view"] = "history"
        return url_for(endpoint, **{k: v for k, v in args.items() if v})

    @requiresUser
    def songDetailPage(username, db, track_id):

        # A merged track's page IS its canonical's page. Links to the version
        # that lost the election keep working - a bookmark, a share, a row in
        # someone's own history - and land on the numbers the global lists are
        # already showing, rather than on a page whose count no longer matches
        # anything else on the site.
        #
        # A redirect rather than a silent render, so the URL in the bar names
        # the song the page is actually about; the "also released on" list below
        # is where the version they asked for is still reachable. Only on the
        # plain GET: the htmx and ajax=true requests below carry the id the
        # shell already resolved, and a redirect mid-swap would replace a region
        # with a whole page (see _missingEntityResponse for that same trap).
        song = db.getSong(track_id)
        if song is None:
            return _missingEntityResponse("topSongsPage")

        #< getSong answers about the whole merge group now, keyed on the
        #  canonical - so "the row that came back is not the row asked for" IS
        #  the merged-id signal, off data already loaded. (The canonical row's
        #  own canonicalId is None, which is why the old key stopped working the
        #  moment the lookup became group-wide.)
        answeredId = song.get("id")
        if (answeredId and answeredId != track_id
                and not request.headers.get("HX-Request")
                and request.args.get("ajax") != "true"):
            #< the query string is the caller's, so it cannot be splatted in
            #  raw: a param named track_id reaches url_for twice (TypeError),
            #  and url_for's keyword-only _method/_scheme/_external/_anchor are
            #  not query params at all - _method=POST sends the builder looking
            #  for a rule this GET-only endpoint has not got (BuildError).
            #  Either escapes the view as a 500, on a URL anyone can build off
            #  an "Also released on" link. The page's own id always wins.
            #
            #  `endpoint` is named rather than covered by the underscore test
            #  because it is the one collision that does not wear one: it is
            #  url_for's first POSITIONAL parameter, already given above as
            #  "songDetailPage", so ?endpoint= is the same TypeError as
            #  ?track_id= by a different door.
            carried = {key: value for key, value in request.args.items()
                       if key not in ("track_id", "endpoint")
                       and not key.startswith("_")}
            return redirect(url_for("songDetailPage", track_id=answeredId, **carried))

        groupByParam = request.args.get("groupBy", "")   #< raw: the select keeps showing Auto
        # Four modes share this route, and the marker differs by kind. The one
        # that answers with DATA rather than markup keeps its query parameter;
        # the three htmx ones are told apart by which region they are filling
        # (see _detailSwapTarget and the DETAIL_*_TARGET constants).
        #
        # The bucket select re-fetches just the play-history series (see
        # static/js/detail-chart.js) - everything else on the page is
        # bucket-independent, so the full render below is skipped.
        if request.args.get("ajax") == "true":
            groupBy = dashboard._resolveGroupBy(
                groupByParam, *dashboard._playRangeSpanDates(username, db.tz, trackId=track_id))
            timeSeries = dashboard._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy)
            )
            return jsonify(timeSeries=timeSeries, groupBy=groupBy)

        # Lightweight shell, the same two-phase load /charts, /genres, /history
        # and the three Top pages use: this GET renders the hero, the toolbar
        # and the tag panel - all off the one getSong above - and htmx fetches
        # everything below them right after first paint. Every query past this
        # point (the play log, the bucketed chart aggregates, the skip summary)
        # is work the first paint no longer waits on.
        swapTarget = _detailSwapTarget()
        if not swapTarget:
            return render_template(
                "song_detail.html",
                song=song,
                username=username,
                groupBy=groupByParam,
                #< the other releases carrying this same recording. Empty for
                #  every unmerged song, so the block is simply absent until a
                #  merge exists
                mergedReleases=db.repo.getMergedReleases(track_id),
                entity_tags=db.repo.getTagsForEntity(username, "track", track_id),
                success=request.args.get("success"),
                error=request.args.get("error"),
                bodyUrl=_detailBodyUrl("songDetailPage", {"track_id": track_id},
                                       groupByParam, songDetail=True),
            )

        listCtx = _detailHistoryContext(db, "songDetailPage", {"track_id": track_id},
                                        groupByParam=groupByParam, trackId=track_id,
                                        trackDurationMs=song.get("duration"))
        # Both play-log swaps skip the chart/heatmap work above: "Show more"
        # replaces only the control at the end of the timeline (with the next
        # batch of rows plus the next control - see _play_log_batch.html), and
        # the sort/skips toggles and pagination links re-render the whole log.
        # Fragments, not a JSON envelope: htmx swaps the response body straight
        # in, so a {"resultsHtml": ...} wrapper would land as literal JSON text.
        if swapTarget == DETAIL_MORE_TARGET:
            return render_template("_play_log_batch.html", username=username, **listCtx)
        if swapTarget == DETAIL_HISTORY_TARGET:
            return render_template("_play_log.html", username=username, **listCtx)

        groupBy = dashboard._resolveGroupBy(
            groupByParam, *dashboard._playRangeSpanDates(username, db.tz, trackId=track_id))
        # One aggregate, two local-time views - same as chartsPage below.
        bucketRows = db.getPlayBuckets(trackId=track_id)
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy, bucketRows=bucketRows)
        )

        song = dashboard._embedSongTextElements(song)
        song = dashboard._embedTopSongTextElements(song)
        song = dashboard._attachGenres(db, [song], "track")[0]

        heatmap = dashboard._embedHeatmapTextElements(
            db.getHourOfDayHeatmap(trackId=track_id, bucketRows=bucketRows))

        skipStats = db.getSkipStats(trackId=track_id)

        # The body as markup, with the two chart series riding inside it as a
        # JSON data island (see _detail_chart_data.html). They used to travel
        # beside the HTML in one JSON envelope; htmx swaps a response body, so
        # the HTML has to BE the response and the data has to come with it.
        return render_template(
            "_song_detail_body.html",
            song=song,
            username=username,
            skipStats=skipStats,
            chartData={"timeSeries": timeSeries, "heatmap": heatmap},
            **listCtx,
        )
    app.add_url_rule("/song/<track_id>", "songDetailPage", songDetailPage, methods=["GET"])

    def _entityDetailPage(username, db, entityId, *, kind, getter, missingEndpoint,
                          idKwarg, urlIdKwarg, shellTemplate, bodyTemplate,
                          embedEntity, fetchBio):
        """The artist/album detail route body - they were ~85-line twins whose
        six inline comments all read "see songDetailPage's identical branch"
        (the same reason the _topList* helpers above exist: a fix landing in
        one twin and missing the other). songDetailPage stays its own function:
        no songs list, a heatmap, the play-log template and a track duration.

        The shared skeleton, in order: bucket-only chart refetch (?ajax=true),
        the deferred-body shell (a plain GET), the play-log-only swap, then the
        full deferred body. `kind` is the tag/genre entity kind ("artist"/
        "album") and also names the endpoint (f"{kind}DetailPage" - the route
        registration convention below). `embedEntity`/`fetchBio` carry the two
        genuinely different steps; fetchBio(entity) returns the bio to display
        (or None), owning its own lazy-fetch + kill-switch wiring."""
        entity = getter(entityId)
        if entity is None:
            return _missingEntityResponse(missingEndpoint)

        spanKwargs = {idKwarg: entityId}
        groupByParam = request.args.get("groupBy", "")   #< raw: the select keeps showing Auto
        # The bucket select re-fetches just the play-history series (see
        # static/js/detail-chart.js) - everything else is bucket-independent.
        # The one mode that stays JSON: it answers with chart data, not markup.
        if request.args.get("ajax") == "true":
            groupBy = dashboard._resolveGroupBy(
                groupByParam, *dashboard._playRangeSpanDates(username, db.tz, **spanKwargs))
            timeSeries = dashboard._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(groupBy=groupBy, **spanKwargs)
            )
            return jsonify(timeSeries=timeSeries, groupBy=groupBy)

        # Deferred-body shell, the same two-phase load songDetailPage documents.
        # The artist page has the most to gain: the whole songs-by-this-artist
        # aggregate and the Last.fm biography fetch below happen only after the
        # page is already on screen.
        swapTarget = _detailSwapTarget()
        if not swapTarget:
            return render_template(
                shellTemplate,
                username=username,
                groupBy=groupByParam,
                entity_tags=db.repo.getTagsForEntity(username, kind, entityId),
                success=request.args.get("success"),
                error=request.args.get("error"),
                bodyUrl=_detailBodyUrl(f"{kind}DetailPage", {urlIdKwarg: entityId},
                                       groupByParam, songDetail=False),
                **{kind: entity},
            )

        # An album you have only ever played ONE track from gets the song page's
        # timeline instead of full history cards: the cards exist to say WHICH
        # song each row was, and with one song they say it over and over while
        # burying what differs (when, and how much of it played).
        #
        # uniqueSongCount is the entity's own aggregate, already fetched above,
        # and it counts distinct non-skip tracks - exactly what this list holds,
        # since it is fetched without skips - so the two cannot disagree about
        # how many songs are in it.
        #
        # kind in SINGLE_TRACK_TIMELINE_KINDS: started album-only (an album is a
        # fixed, small track set where "only ever played track 3" is an
        # ordinary state), and now covers artists too - an artist with one
        # canonical song has exactly the same repeating-card problem.
        singleTrackTimeline = kind in SINGLE_TRACK_TIMELINE_KINDS and entity.get("uniqueSongCount") == 1

        listCtx = _detailHistoryContext(db, f"{kind}DetailPage", {urlIdKwarg: entityId, "view": "history"},
                                        groupByParam=groupByParam,
                                        singleTrackTimeline=singleTrackTimeline, **spanKwargs)
        listCtx["plays"] = dashboard._attachGenres(db, listCtx["plays"], "track")
        # The sort toggle / pagination links re-swap just the play log. These
        # pages page their history rather than growing it, so there is no
        # DETAIL_MORE_TARGET branch here - only songDetailPage has a "Show more".
        #
        # Renders whichever partial the context chose, so a sort or page change
        # cannot turn a timeline back into cards.
        if swapTarget == DETAIL_HISTORY_TARGET:
            return render_template(
                listCtx["historyPartial"], username=username, kind=kind,
                itemName=entity.get("name", ""), **listCtx)

        groupBy = dashboard._resolveGroupBy(
            groupByParam, *dashboard._playRangeSpanDates(username, db.tz, **spanKwargs))
        timeSeries = dashboard._embedTimeSeriesTextElements(
            db.getListeningTimeSeries(groupBy=groupBy, **spanKwargs)
        )

        songs = db.getSongsStats(sortBy="plays", **spanKwargs)
        firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
        firstSongName = firstSong.get("name") if firstSong else None

        songs = dashboard._embedSongsTextElements(songs)
        songs = dashboard._embedTopSongsTextElements(
            songs, sortBy="plays", totalPlays=entity.get("plays", 0), totalMs=entity.get("totalTimeListened", 0)
        )
        songs = dashboard._attachGenres(db, songs, "track")
        entity = embedEntity(entity)
        entity = dashboard._attachGenres(db, [entity], kind)[0]

        entity["bio"] = fetchBio(entity)

        skipStats = db.getSkipStats(**spanKwargs)

        #< no heatmap key: these pages have no "When You Listen" canvas, and
        #  renderAllCharts skips a canvas that isn't there - see
        #  _detail_chart_data.html
        return render_template(
            bodyTemplate,
            songs=songs,
            firstSongName=firstSongName,
            username=username,
            skipStats=skipStats,
            view=dashboard._getDetailViewParam(),
            itemName=entity.get("name", ""),
            chartData={"timeSeries": timeSeries},
            kind=kind,
            **{kind: entity},
            **listCtx,
        )

    @requiresUser
    def artistDetailPage(username, db, artist_id):
        def fetchBio(artist):
            # lazyFetchArtistBio no-ops (and skips fetching) when the admin's
            # instance-wide toggle is off, same contract as the Last.fm genre
            # backfill kill switch - but the displayed bio is suppressed here
            # too, so disabling the feature also hides an artist's
            # already-fetched bio, not just new ones.
            db.lazyFetchArtistBio(artist_id, artist.get("name", ""))
            return db.getArtistBio(artist_id) if dashboard.repo.isArtistBioEnabled() else None

        return _entityDetailPage(
            username, db, artist_id,
            kind="artist", getter=db.getArtist, missingEndpoint="topArtistsPage",
            idKwarg="artistId", urlIdKwarg="artist_id",
            shellTemplate="artist_detail.html", bodyTemplate="_artist_detail_body.html",
            embedEntity=dashboard._embedArtistTextElement, fetchBio=fetchBio)
    app.add_url_rule("/artist/<artist_id>", "artistDetailPage", artistDetailPage, methods=["GET"])

    @requiresUser
    def albumDetailPage(username, db, album_id):
        def fetchBio(album):
            # Mirrors artistDetailPage's bio wiring: lazyFetchAlbumBio no-ops
            # (and skips fetching) when the admin's instance-wide toggle is
            # off, and the displayed bio is suppressed here too, so disabling
            # the feature also hides an album's already-fetched bio. The
            # primary artist (album.getinfo needs one) comes from the
            # already-loaded artists list.
            primaryArtists = album.get("artists") or []
            primaryArtistName = primaryArtists[0].get("name", "") if primaryArtists else ""
            if primaryArtistName:
                db.lazyFetchAlbumBio(album_id, album.get("name", ""), primaryArtistName)
            return db.getAlbumBio(album_id) if dashboard.repo.isAlbumBioEnabled() else None

        return _entityDetailPage(
            username, db, album_id,
            kind="album", getter=db.getAlbum, missingEndpoint="topAlbumsPage",
            idKwarg="albumId", urlIdKwarg="album_id",
            shellTemplate="album_detail.html", bodyTemplate="_album_detail_body.html",
            embedEntity=dashboard._embedAlbumTextElements, fetchBio=fetchBio)
    app.add_url_rule("/album/<album_id>", "albumDetailPage", albumDetailPage, methods=["GET"])
