import unittest
from unittest.mock import patch, MagicMock
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: like test_dashboard_pagination.py / test_top_albums_route.py, this file
# deliberately does NOT swap Database modules for MagicMocks in sys.modules -
# it only exercises the routes with a per-test mock db (via get_user_db).
from app import SpotifyDashboardApp
from _app_factory import AppTestCase
from _detail_client import (DetailPageClientMixin, HX_BODY_HEADERS, HX_LIST_HEADERS,
                            HX_MORE_HEADERS)
import routes.charts as chartsRoutes
import app as appmod


class TestDetailRouteRegistration(AppTestCase):
    def test_detail_routes_keep_their_public_rules_methods_and_legacy_constants(self):
        dash = self._makeApp()

        rules = {
            rule.endpoint: rule
            for rule in dash.app.url_map.iter_rules()
            if rule.endpoint in {"songDetailPage", "artistDetailPage", "albumDetailPage"}
        }

        self.assertEqual(set(rules), {"songDetailPage", "artistDetailPage", "albumDetailPage"})
        self.assertEqual(rules["songDetailPage"].rule, "/song/<track_id>")
        self.assertEqual(rules["artistDetailPage"].rule, "/artist/<artist_id>")
        self.assertEqual(rules["albumDetailPage"].rule, "/album/<album_id>")
        for rule in rules.values():
            self.assertEqual(rule.methods & {"GET", "POST", "PUT", "DELETE", "PATCH"}, {"GET"})
        self.assertEqual(chartsRoutes.DETAIL_BODY_TARGET, "detailBody")
        self.assertEqual(chartsRoutes.DETAIL_HISTORY_TARGET, "detailHistoryResults")
        self.assertEqual(chartsRoutes.DETAIL_MORE_TARGET, "timelineActions")
        self.assertEqual(chartsRoutes.MAX_DETAIL_HISTORY_PAGES, 10)


def _byId(entityId, genres):
    """Return value shape of the batched genre lookups (getGenresForTracks and
    friends): {id: [genre, ...]} for every requested id."""
    return {entityId: genres}


_SPOTIFY_ANCHOR = 'class="track-label track-spotify-link" href='
_PLAY_BUTTON = 'class="track-label track-spotify-link play-now-button"'


def _heroCard(body):
    """The detail page's hero card markup.

    `<section id="track-list">` wraps exactly the one card the page is about;
    the artist/album pages' song lists sit in their own (id-less) sections
    below, so slicing to the next </section> isolates the hero.
    """
    start = body.index('<section id="track-list"')
    return body[start:body.index("</section>", start)]


class _DetailRouteTestBase(DetailPageClientMixin, AppTestCase):
    """`self._getPath(...)` is the shell GET plus the deferred body htmx swaps
    into it, which is what a browser ends up showing - see _detail_client.py.
    Tests about the split itself live in TestDetailPageDeferredBody below and
    drive the two requests through `_getRaw` instead."""

    def _assertNoNavItemActive(self, body):
        """Detail subpages aren't the Top Songs/Artists/Albums list pages
        themselves, so nothing in the main nav should render as active/current
        while viewing one (see templates/layout.html's nav-links block)."""
        import re
        navMatch = re.search(r'<nav class="nav-links".*?</nav>', body, re.DOTALL)
        self.assertIsNotNone(navMatch, "could not find nav-links block")
        navHtml = navMatch.group(0)
        self.assertNotIn("active-parent", navHtml)
        self.assertNotIn('class="active"', navHtml)


class TestSongDetailRoute(_DetailRouteTestBase):
    def _song(self):
        return {
            "id": "t1", "name": "Song One", "url": "http://example.com/t1",
            "imageId": "alb1", "duration": 200000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1",
                      "imageId": "alb1", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "", "imageId": "a1"}],
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }

    def test_known_song_renders(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Song One", resp.data)
        db.getSong.assert_called_once_with("t1")
        db.getListeningTimeSeries.assert_called_once()
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("trackId"), "t1")
        self.assertEqual(db.getHourOfDayHeatmap.call_args.kwargs.get("trackId"), "t1")

    def test_no_nav_item_is_active(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self._assertNoNavItemActive(resp.data.decode())

    def test_tag_widget_shown_by_default(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'class="tag-widget"', resp.data)

    def test_tag_widget_hidden_when_admin_disables_tags(self):
        dash = self._makeApp()
        dash.repo.setTagsEnabled(False)
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_tag_widget_hidden_when_user_hides_it(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.updateUserSettings("alice", "day", None, hide_tags_panel=True)
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_rendered_page_has_balanced_div_tags(self):
        """Guard against an unclosed container leaking every section below it
        into the toolbar (the tag-widget insert once dropped .detail-toolbar's
        closing </div>, which cascaded the whole subpage layout)."""
        import re
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        opens = len(re.findall(r"<div[\s>]", body))
        closes = body.count("</div>")
        self.assertEqual(opens, closes, f"unbalanced <div> tags: {opens} open vs {closes} close")

    def test_genre_badge_renders_when_track_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop", "shoegaze"])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'<span class="track-label genre-label">dream pop</span>', resp.data)
        self.assertIn(b'<span class="track-label genre-label">shoegaze</span>', resp.data)
        db.getGenresForTracks.assert_called_once_with(["t1"])

    def test_genre_badge_is_capped_at_track_card_genre_limit(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["one", "two", "three", "four"])

        resp = self._getPath(dash, db, "/song/t1")

        from app import TRACK_CARD_GENRE_LIMIT
        self.assertEqual(TRACK_CARD_GENRE_LIMIT, 3)
        for genre in ("one", "two", "three"):
            self.assertIn(f'<span class="track-label genre-label">{genre}</span>'.encode(), resp.data)
        self.assertNotIn(b"genre-label\">four<", resp.data)

    def test_genre_badge_hides_when_the_admin_disables_lastfm_backfill(self):
        """Per-track badges normally show regardless of the aggregate
        charts/wrapped/compare unlock threshold - but the admin's instance-
        wide kill switch still applies, same as every other genre surface."""
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop", "shoegaze"])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b"genre-label", resp.data)
        db.getGenresForTracks.assert_not_called()

    def test_genre_badge_absent_without_genre_data(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", [])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b"genre-label", resp.data)

    def test_genre_badges_render_inside_track_attributes_after_other_labels(self):
        """Genre badges are nested inside .track-attributes, after the other
        label spans, so that .genre-badges-container's display:contents lets
        them join .track-attributes' own flex row on mobile/tablet - same
        row, same size, just a different color. Desktop promotes the
        container to an absolutely positioned overlay instead - see the
        has-genres media query in style.css."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop", "shoegaze"])

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        attributesOpenIdx = body.index('class="track-attributes"')
        durationLabelIdx = body.index('Duration:')
        genreContainerIdx = body.index('class="genre-badges-container"')
        attributesCloseIdx = body.index('</div>', genreContainerIdx)
        self.assertLess(attributesOpenIdx, durationLabelIdx)
        self.assertLess(durationLabelIdx, genreContainerIdx)
        self.assertLess(genreContainerIdx, attributesCloseIdx)

    def test_play_now_button_renders_last_after_genre_badges(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop"])

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        self.assertIn('class="track-label track-spotify-link play-now-button"', body)
        genreContainerIdx = body.index('class="genre-badges-container"')
        buttonIdx = body.index('class="track-label track-spotify-link play-now-button"')
        self.assertLess(genreContainerIdx, buttonIdx)

    def test_the_hero_card_offers_the_spotify_link_before_the_play_button(self):
        """The Play now button used to REPLACE the Open in Spotify anchor here,
        so the detail pages were the one surface from which you couldn't reach
        the entity on Spotify itself. The hero carries both now, the anchor
        first: leaving is the plain option, the embedded player the extra one."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        card = _heroCard(self._getPath(dash, db, "/song/t1").data.decode())

        self.assertIn("Open in Spotify", card)
        self.assertLess(card.index(_SPOTIFY_ANCHOR), card.index(_PLAY_BUTTON))
        self.assertIn('href="http://example.com/t1"', card)
        self.assertIn('data-spotify-url="http://example.com/t1"', card)
        self.assertIn('data-embed-type="track"', card)

    def test_play_embed_container_renders_hidden_between_card_and_charts(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        self.assertIn('id="play-embed"', body)
        embedTag = body[body.index('id="play-embed"'):body.index('>', body.index('id="play-embed"')) + 1]
        self.assertIn('hidden', embedTag)
        trackListIdx = body.index('id="track-list"')
        embedIdx = body.index('id="play-embed"')
        chartCardIdx = body.index('chart-card')
        self.assertLess(trackListIdx, embedIdx)
        self.assertLess(embedIdx, chartCardIdx)

    def test_play_embed_renders_even_when_the_chart_section_is_absent(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1   #< single play => no Play History chart section
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'id="timeSeriesChart"', resp.data)
        self.assertIn(b'id="play-embed"', resp.data)

    def test_no_play_button_or_embed_for_a_fabricated_song(self):
        """A deleted/unavailable track has an empty url (a fabricated md5 id) -
        it gets neither the Spotify anchor nor a Play now button/embed, same
        guard the anchor always used."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["url"] = ""
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'play-now-button', resp.data)
        self.assertNotIn(b'track-spotify-link', resp.data)
        self.assertNotIn(b'id="play-embed"', resp.data)

    def test_play_embed_script_is_included(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'js/play-embed.js', resp.data)

    def test_csp_allows_unsafe_eval_for_the_spotify_embed(self):
        """Spotify's iFrame API bundle is a webpack eval-devtool build that runs
        in our page context, so the detail responses (and only those) must allow
        'unsafe-eval' - see DETAIL_CSP_ENDPOINTS in app.py."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn("'unsafe-eval'", resp.headers.get("Content-Security-Policy", ""))

    def test_track_card_gets_has_genres_class_only_when_genres_exist(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop"])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'class="track-card has-genres"', resp.data)

    def test_track_card_omits_has_genres_class_without_genre_data(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", [])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'class="track-card"', resp.data)
        self.assertNotIn(b'has-genres', resp.data)

    def test_play_history_panel_hidden_for_single_play_song(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b"Play History", resp.data)
        self.assertNotIn(b'id="timeSeriesChart"', resp.data)
        self.assertIn(b"When You Listen to This Song", resp.data)

    def test_play_history_panel_shown_for_multiple_plays(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 2
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play History", resp.data)
        self.assertIn(b'id="timeSeriesChart"', resp.data)

    def test_play_history_panel_shown_for_a_song_that_was_only_ever_skipped(self):
        """2e4f9e3 made a skip-only song's page render, and gave the timeline a
        skips series so it would have something to show - but the canvas that
        series lands on was gated on plays > 1, and getSong's skip-only fallback
        reports plays=0 by design. So the one page the series was built for
        rendered no chart at all. The Most Skipped lists link straight here."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 0        #< the skip-sorted fallback's shape
        song["skips"] = 3
        db.getSong.return_value = song
        #< the route always computes this alongside the song; it is where the
        #  gate reads the skip count from, since getSongsPage has no skips column
        db.getSkipStats.return_value = {"plays": 0, "skips": 3, "skipPercent": 100.0}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play History", resp.data)
        self.assertIn(b'id="timeSeriesChart"', resp.data)

    def test_play_history_panel_shown_for_a_much_skipped_song_with_one_play(self):
        """The rule is "is there a history worth plotting", and the skips series
        is on the timeline either way - so 1 play + 40 skips must chart, exactly
        as 0 plays + 2 skips does.

        The count has to come from skipStats: getSongsPage's SELECT has no skips
        column, so song['skips'] exists ONLY on getSong's skip-only fallback
        (plays=0). Reading it off the song made the gate a no-op for every track
        that has any real play at all."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1
        db.getSong.return_value = song
        db.getSkipStats.return_value = {"plays": 1, "skips": 40, "skipPercent": 97.6}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play History", resp.data)
        self.assertIn(b'id="timeSeriesChart"', resp.data)

    def test_one_play_and_no_skips_is_still_too_little_to_chart(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1
        db.getSong.return_value = song
        db.getSkipStats.return_value = {"plays": 1, "skips": 0, "skipPercent": 0.0}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'id="timeSeriesChart"', resp.data)

    def test_a_stubbed_skip_summary_cannot_500_the_page(self):
        """Most route tests hand the page a bare MagicMock db, so skipStats is a
        Mock rather than a dict - arithmetic on it would raise inside the
        template. The gate must degrade, not explode."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 4
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="timeSeriesChart"', resp.data)   #< 4 plays alone clears the bar

    def test_a_single_skip_is_still_too_little_to_chart(self):
        """Same rule as a single play: one point is not a history."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 0
        song["skips"] = 1
        db.getSong.return_value = song
        db.getSkipStats.return_value = {"plays": 0, "skips": 1, "skipPercent": 100.0}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'id="timeSeriesChart"', resp.data)

    def test_unknown_song_redirects_to_top_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = None

        resp = self._getPath(dash, db, "/song/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-songs", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_invalid_groupby_resolves_like_auto(self):
        # Junk goes through the same span-derived resolution as the Auto
        # option (see _resolveGroupBy) - with no recorded plays the span is
        # empty, which resolves to day.
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        self._getPath(dash, db, "/song/t1?groupBy=nonsense")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "day")

    def test_ajax_returns_only_the_time_series_json_and_skips_heavy_work(self):
        """The Trend-buckets select re-fetches just the play-history series via
        ?ajax=true (static/js/detail-chart.js); the heavy per-page work (heatmap,
        genres) must be deferred - see the branch in songDetailPage."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = [{"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1}]

        resp = self._getPath(dash, db, "/song/t1?ajax=true")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(sorted(payload.keys()), ["groupBy", "timeSeries"])
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("trackId"), "t1")
        db.getHourOfDayHeatmap.assert_not_called()
        db.getGenresForTracks.assert_not_called()

    def test_ajax_groupby_is_passed_through_and_echoed(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/song/t1?groupBy=month&ajax=true")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertEqual(resp.get_json().get("groupBy"), "month")

    def _playEntry(self, playedAtText="20 Jul 2026, 15:30", timePlayedText="3m 20s"):
        """A play entry as _embedSongsTextElements would emit it - tests stub
        the embedder to identity (the /history tests' convention), so the
        text fields are supplied directly."""
        return {"id": "t1", "name": "Song One", "playedAtText": playedAtText,
                "timePlayedText": timePlayedText, "contextName": None, "artists": []}

    def test_play_log_renders_play_rows(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getEntriesCount.return_value = 2
        db.getEntriesFromNew.return_value = [
            self._playEntry("20 Jul 2026, 15:30"),
            self._playEntry("19 Jul 2026, 09:12"),
        ]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play Timeline", resp.data)
        self.assertIn(b"20 Jul 2026, 15:30", resp.data)
        self.assertIn(b"19 Jul 2026, 09:12", resp.data)
        self.assertIn(b"Time Played: 3m 20s", resp.data)

    def test_play_log_labels_a_skip_skipped_not_partial(self):
        """The badge comes from the play's own is_skip flag - what the current
        skip threshold classified it as (recomputeSkipFlags rewrites every row
        when an admin changes the setting). A skip that ran far enough to look
        like a plausible partial must still read "Skipped", so the timeline
        agrees with the skip counts everywhere else on the page."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()   #< 200s track
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        skip = self._playEntry()
        skip.update(playedAt=1784560000, timePlayed=100000, isSkip=True)   #< 50%: "Partial" on the raw percentage alone
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [skip]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Skipped", resp.data)
        self.assertNotIn(b"Partial", resp.data)

    def test_play_log_scoped_to_track_id(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        self._getPath(dash, db, "/song/t1")

        self.assertEqual(db.getEntriesCount.call_args.kwargs.get("trackId"), "t1")
        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("trackId"), "t1")

    def test_play_log_page_2_offsets_by_page_size(self):
        from app import PAGE_SIZE
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getEntriesCount.return_value = PAGE_SIZE * 2 + 5

        self._getPath(dash, db, "/song/t1?page=2")

        #< the first call is the batch; the second is the one-row seed for the
        #  appended batch's month header and gap badge (see _songHistoryContext)
        batchCall = db.getEntriesFromNew.call_args_list[0]
        self.assertEqual(batchCall.kwargs.get("startIndex"), PAGE_SIZE)
        self.assertEqual(batchCall.kwargs.get("count"), PAGE_SIZE)

    def test_play_log_empty_state(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"No plays recorded yet.", resp.data)

    def test_sort_oldest_uses_entries_from_old(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1?sort=oldest")

        self.assertEqual(db.getEntriesFromOld.call_args.kwargs.get("trackId"), "t1")
        db.getEntriesFromNew.assert_not_called()
        self.assertIn("Date ↑".encode(), resp.data)   #< arrow shows the current order

    def test_sort_newest_is_the_default_and_shows_down_arrow(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1?sort=bogus")

        db.getEntriesFromNew.assert_called_once()
        db.getEntriesFromOld.assert_not_called()
        self.assertIn("Date ↓".encode(), resp.data)
        self.assertIn(b"sort=oldest", resp.data)   #< the toggle links to the flipped order

    def test_the_play_log_swap_returns_the_fragment_and_skips_chart_work(self):
        """The sort toggle / pagination links re-swap just the play log (an
        HX-Request targeting #detailHistoryResults) - chart and heatmap work
        must be skipped."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [self._playEntry()]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            resp = self._getRaw(dash, db, "/song/t1", headers=HX_LIST_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/html")
        self.assertIn("20 Jul 2026", resp.get_data(as_text=True))
        db.getHourOfDayHeatmap.assert_not_called()

    def test_song_detail_skips_toggle_passed_to_db(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = []
        db.getEntriesCount.return_value = 2
        db.getEntriesFromNew.return_value = [self._playEntry()]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            self._getPath(dash, db, "/song/t1?skips=false")

        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("includeSkips"), False)

    def test_song_detail_play_log_offers_the_next_batch(self):
        """hasMore/nextOffset/nextBatchSize used to travel beside resultsHtml in
        the JSON envelope for the client to act on. They are now spent in the
        template: the "Show more" control's own URL carries the next offset, and
        its label carries the batch size."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 120
        db.getEntriesFromNew.return_value = [self._playEntry() for _ in range(50)]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            body = self._getRaw(dash, db, "/song/t1?offset=0",
                                headers=HX_LIST_HEADERS).get_data(as_text=True)

        self.assertIn('id="timelineActions"', body)
        self.assertIn("offset=50", body)
        self.assertIn("Show More Plays (50)", body)
        self.assertIn("20 Jul 2026", body)
        db.getListeningTimeSeries.assert_not_called()
        db.getHourOfDayHeatmap.assert_not_called()

    def test_show_more_drops_itself_behind_a_list_change_in_flight(self):
        """The button used to INHERIT the container's hx-sync
        "#detailHistoryResults:replace". That is right in one direction only:
        a sort/skips/page change must abort a batch in flight (stale rows
        must not append into a fresh list). In the other direction it was
        wrong - a Show More activated while a sort change was in flight
        (keyboard: the fade's pointer-events:none only blocks the mouse)
        ABORTED the sort, and the batch, computed from the pre-sort offset,
        landed on the old ordering while the URL never followed. `drop` on the
        same queue makes the button yield: its request is ignored while any
        list change is in flight, and the container's `replace` still aborts a
        batch when a change arrives. An attribute test pins the attribute; the
        abort/drop semantics were checked in a browser."""
        from bs4 import BeautifulSoup
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 120
        db.getEntriesFromNew.return_value = [self._playEntry() for _ in range(50)]
        #< the body render also draws the charts island
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            listHtml = self._getRaw(dash, db, "/song/t1?offset=0",
                                    headers=HX_LIST_HEADERS).get_data(as_text=True)
            pageHtml = self._getRaw(dash, db, "/song/t1",
                                    headers=HX_BODY_HEADERS).get_data(as_text=True)

        button = BeautifulSoup(listHtml, "html.parser").select_one("#showMorePlaysBtn")
        container = BeautifulSoup(pageHtml, "html.parser").select_one("#detailHistoryResults")
        self.assertEqual(button["hx-sync"], "#detailHistoryResults:drop")
        self.assertEqual(container["hx-sync"], "#detailHistoryResults:replace")

    def test_the_play_log_limit_is_clamped(self):
        """?limit had no ceiling at all - the one pagination parameter in the
        codebase that didn't - so ?limit=500000 fetched and rendered half a
        million rows. Own data only, so a footgun rather than a hole, but every
        other pager is clamped and a shared URL carrying it shouldn't be able to
        wedge the page."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 10 ** 6
        db.getEntriesFromNew.return_value = []

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            self._getRaw(dash, db, "/song/t1?limit=500000", headers=HX_LIST_HEADERS)

        requestedLimit = db.getEntriesFromNew.call_args.kwargs["count"]
        self.assertEqual(requestedLimit, appmod.PAGE_SIZE * chartsRoutes.MAX_DETAIL_HISTORY_PAGES)

    def test_a_limit_within_the_ceiling_is_honoured(self):
        """"Show more" legitimately grows the batch past PAGE_SIZE, so the clamp
        must not collapse every request back to one page."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 10 ** 6
        db.getEntriesFromNew.return_value = []
        allowed = appmod.PAGE_SIZE * 2

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            self._getRaw(dash, db, f"/song/t1?limit={allowed}", headers=HX_LIST_HEADERS)

        self.assertEqual(db.getEntriesFromNew.call_args.kwargs["count"], allowed)

    def test_song_detail_next_batch_size_partial_remaining(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 62
        db.getEntriesFromNew.return_value = [self._playEntry() for _ in range(50)]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            body = self._getRaw(dash, db, "/song/t1?offset=0",
                                headers=HX_LIST_HEADERS).get_data(as_text=True)

        self.assertIn("offset=50", body)
        self.assertIn("Show More Plays (12)", body)

    def test_the_show_more_label_never_falls_back_to_a_literal_page_size(self):
        """The label was `{{ nextBatchSize or 50 }}`.

        The fallback could not be reached: inside {% if hasMore %} the route has
        already computed min(PAGE_SIZE, remainingCount), and hasMore is true
        exactly when remainingCount > 0 - so the value is always at least 1 and
        never falsy. What the 50 did do was spell config.PAGE_SIZE's value as a
        literal, in a template that has no other reason to know it, so raising
        the page size would have left a stale number in the one place a reader
        would trust it. Dead and wrong at the same time.
        """
        source = (Path(__file__).resolve().parent.parent
                  / "templates" / "_play_log_batch.html").read_text(encoding="utf-8")
        self.assertNotRegex(
            source, r"nextBatchSize\s+or\s+\d",
            "The batch size comes from the route; a numeric fallback here is "
            "unreachable and duplicates config.PAGE_SIZE.")

    def test_the_offered_batch_is_always_a_real_count_while_the_control_shows(self):
        """What makes the fallback above safe to delete, pinned at the boundary
        it would matter at: one row left is the smallest count that still
        renders the control, and it must read as 1 rather than as a page."""
        for remaining in (1, 2, appmod.PAGE_SIZE, appmod.PAGE_SIZE + 1):
            with self.subTest(remaining=remaining):
                dash = self._makeApp()
                db = MagicMock()
                db.getSong.return_value = self._song()
                db.getEntriesCount.return_value = appmod.PAGE_SIZE + remaining
                db.getEntriesFromNew.return_value = [
                    self._playEntry() for _ in range(appmod.PAGE_SIZE)]

                with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
                    body = self._getRaw(dash, db, "/song/t1?offset=0",
                                        headers=HX_LIST_HEADERS).get_data(as_text=True)

                expected = min(appmod.PAGE_SIZE, remaining)
                self.assertIn('id="timelineActions"', body)
                self.assertIn(f"Show More Plays ({expected})", body)

    def test_the_control_is_gone_rather_than_offering_an_empty_batch(self):
        """The other half of the same invariant: nothing remaining means no
        control at all, which is why a zero batch size never reaches the label."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = appmod.PAGE_SIZE
        db.getEntriesFromNew.return_value = [self._playEntry() for _ in range(appmod.PAGE_SIZE)]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            body = self._getRaw(dash, db, "/song/t1?offset=0",
                                headers=HX_LIST_HEADERS).get_data(as_text=True)

        self.assertNotIn('id="timelineActions"', body)
        self.assertNotIn("Show More Plays", body)

    def test_a_show_more_batch_is_rows_and_the_next_control_only(self):
        """It replaces the control at the end of the timeline, so the rows land
        where the append used to put them and the log's header stays put."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 120
        db.getEntriesFromNew.return_value = [self._playEntry() for _ in range(50)]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            body = self._getRaw(dash, db, "/song/t1?offset=50",
                                headers=HX_MORE_HEADERS).get_data(as_text=True)

        self.assertIn("20 Jul 2026", body)
        self.assertIn("offset=100", body)     #< the batch after this one
        self.assertNotIn("Play Timeline", body)



class TestArtistDetailRoute(_DetailRouteTestBase):
    def _artist(self):
        return {"id": "a1", "name": "Artist A", "url": "http://example.com/a1", "imageUrl": "",
                "imageId": "a1", "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2,
                "firstListenedAt": 100}

    def _song(self, trackId, name, firstListenedAt):
        return {
            "id": trackId, "name": name, "url": "u", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                        "imageUrl": "", "totalTracks": 2, "releaseDate": 0},
            "artists": [], "plays": 3, "totalTimeListened": 30000, "firstListenedAt": firstListenedAt,
        }

    def test_known_artist_renders_with_their_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Artist A", resp.data)
        db.getArtist.assert_called_once_with("a1")
        self.assertEqual(db.getSongsStats.call_args.kwargs.get("artistId"), "a1")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("artistId"), "a1")

    def test_no_nav_item_is_active(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self._assertNoNavItemActive(resp.data.decode())

    def test_tag_widget_hidden_when_admin_disables_tags(self):
        dash = self._makeApp()
        dash.repo.setTagsEnabled(False)
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_genre_badge_renders_when_artist_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getGenresForArtists.return_value = _byId("a1", ["indie rock"])

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b'<span class="track-label genre-label">indie rock</span>', resp.data)
        db.getGenresForArtists.assert_called_once_with(["a1"])

    def test_biography_renders_when_present(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = "A great band from somewhere."

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"Biography", resp.data)
        self.assertIn(b"A great band from somewhere.", resp.data)
        self.assertIn(b"Biography via Last.fm", resp.data)
        db.lazyFetchArtistBio.assert_called_once_with("a1", "Artist A")
        db.getArtistBio.assert_called_once_with("a1")

    def test_biography_section_absent_without_a_bio(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = None

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertNotIn(b"Biography", resp.data)

    def test_biography_hides_when_the_admin_disables_the_feature(self):
        """Same contract as the genre badge's kill switch: disabled hides
        the section even for an artist whose bio was already fetched and
        stored - db.getArtistBio isn't even consulted for display."""
        dash = self._makeApp()
        dash.repo.setArtistBioEnabled(False)
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = "A great band from somewhere."

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertNotIn(b"Biography", resp.data)
        db.getArtistBio.assert_not_called()

    def test_biography_text_is_html_escaped(self):
        """Last.fm bio text must never be rendered as raw HTML (defense in
        depth alongside the tag-stripping already done in
        Database.lastfm._extractArtistBio)."""
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = "<script>alert('xss')</script>"

        resp = self._getPath(dash, db, "/artist/a1")

        #< the raw payload must never appear unescaped (the page legitimately
        #  contains other <script> tags from layout.html/charts.js, so check
        #  the specific string instead of a bare "<script>" substring)
        self.assertNotIn(b"<script>alert", resp.data)
        self.assertIn(b"&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;", resp.data)

    def test_unknown_artist_redirects_to_top_artists(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = None

        resp = self._getPath(dash, db, "/artist/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-artists", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_ajax_returns_only_the_time_series_json_and_skips_heavy_work(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getListeningTimeSeries.return_value = [{"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1}]

        resp = self._getPath(dash, db, "/artist/a1?ajax=true")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(sorted(payload.keys()), ["groupBy", "timeSeries"])
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("artistId"), "a1")
        db.getSongsStats.assert_not_called()
        db.lazyFetchArtistBio.assert_not_called()
        db.getArtistBio.assert_not_called()

    def test_hero_gets_play_button_but_song_sublist_keeps_spotify_anchors(self):
        """Only the hero artist card gains a Play now button; the songs sub-list
        below (a second _track_card.html include) must keep its normal Open in
        Spotify anchors and nothing more - the playNowButton flag must not leak
        into that loop's shared page context."""
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = [
            self._song("t1", "Song One", firstListenedAt=100),
            self._song("t2", "Song Two", firstListenedAt=200),
        ]
        db.getListeningTimeSeries.return_value = []

        body = self._getPath(dash, db, "/artist/a1").data.decode()

        self.assertEqual(body.count(_PLAY_BUTTON), 1)
        self.assertIn('data-embed-type="artist"', body)
        self.assertIn("Open in Spotify", body)
        card = _heroCard(body)
        self.assertLess(card.index(_SPOTIFY_ANCHOR), card.index(_PLAY_BUTTON))

    def test_csp_allows_unsafe_eval_for_the_spotify_embed(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn("'unsafe-eval'", resp.headers.get("Content-Security-Policy", ""))

    def test_first_song_you_listened_to_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = [
            self._song("t1", "Later Song", firstListenedAt=200),
            self._song("t2", "Earliest Song", firstListenedAt=100),
        ]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"First Song You Listened To", resp.data)
        self.assertIn(b"Earliest Song", resp.data)

    def test_unique_song_count_card_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"Unique Songs Listened", resp.data)
        self.assertIn(b'<p class="summary-value">2</p>', resp.data)

    def _makeHistoryDb(self):
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        return db

    def test_history_tab_scoped_to_artist_id(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        self._getPath(dash, db, "/artist/a1")

        self.assertEqual(db.getEntriesCount.call_args.kwargs.get("artistId"), "a1")
        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("artistId"), "a1")

    def test_default_view_activates_top_songs_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b'<div data-category="top-songs" class="visible">', resp.data)
        self.assertIn(b'<div data-category="history" class="">', resp.data)

    def test_view_history_activates_history_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1?view=history")

        self.assertIn(b'<div data-category="top-songs" class="">', resp.data)
        self.assertIn(b'<div data-category="history" class="visible">', resp.data)
        self.assertIn(b"History with Artist A", resp.data)

    def test_unknown_view_falls_back_to_top_songs(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1?view=bogus")

        self.assertIn(b'<div data-category="top-songs" class="visible">', resp.data)
        self.assertIn(b'<div data-category="history" class="">', resp.data)

    def test_history_pagination_link_carries_view_and_groupby(self):
        from app import PAGE_SIZE
        dash = self._makeApp()
        db = self._makeHistoryDb()
        db.getEntriesCount.return_value = PAGE_SIZE * 2 + 5

        resp = self._getPath(dash, db, "/artist/a1?groupBy=month")

        body = resp.data.decode()
        self.assertIn("view=history", body)
        self.assertIn("groupBy=month", body)
        self.assertIn("page=2", body)

    def test_sort_oldest_uses_entries_from_old(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1?sort=oldest")

        self.assertEqual(db.getEntriesFromOld.call_args.kwargs.get("artistId"), "a1")
        db.getEntriesFromNew.assert_not_called()
        self.assertIn("Date ↑".encode(), resp.data)

    def test_the_play_log_swap_skips_heavy_work(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "History Song", "playedAtText": "20 Jul 2026, 15:30", "artists": []}]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs), \
             patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            resp = self._getRaw(dash, db, "/artist/a1", headers=HX_LIST_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/html")
        resultsHtml = resp.get_data(as_text=True)
        self.assertIn("History with Artist A", resultsHtml)
        self.assertIn("History Song", resultsHtml)
        self.assertIn("Played at 20 Jul 2026, 15:30", resultsHtml)
        db.getSongsStats.assert_not_called()
        db.lazyFetchArtistBio.assert_not_called()
        db.getListeningTimeSeries.assert_not_called()

    def test_a_partial_listen_in_the_history_tab_says_so(self):
        """This tab renders the same card /history does (section='history') and
        is not filtered by completion either, so the two have to label the same
        row the same way - see _attachPlayTypes. The song page is exempt: its
        log is the timeline, which already labels every play."""
        dash = self._makeApp()
        db = self._makeHistoryDb()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "History Song", "artists": [],
             "duration": 200000, "timePlayed": 20000, "isSkip": False}]   #< 10%

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs), \
             patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            body = self._getPath(dash, db, "/artist/a1?view=history").get_data(as_text=True)

        self.assertIn('class="track-label play-type-partial"', body)
        self.assertIn("Partial • 10%", body)


class TestAlbumDetailRoute(_DetailRouteTestBase):
    def _album(self):
        return {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1", "imageId": "alb1",
                "imageUrl": "", "totalTracks": 2, "releaseDate": 0, "artists": [],
                "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2, "firstListenedAt": 100}

    def _song(self, trackId, firstListenedAt):
        return {
            "id": trackId, "name": f"Song {trackId}", "url": "u", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                        "imageUrl": "", "totalTracks": 2, "releaseDate": 0},
            "artists": [], "plays": 3, "totalTimeListened": 30000, "firstListenedAt": firstListenedAt,
        }

    def test_known_album_renders_with_its_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = [self._song("t1", 200), self._song("t2", 100)]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Album One", resp.data)
        db.getAlbum.assert_called_once_with("alb1")
        self.assertEqual(db.getSongsStats.call_args.kwargs.get("albumId"), "alb1")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("albumId"), "alb1")

    def test_no_nav_item_is_active(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self._assertNoNavItemActive(resp.data.decode())

    def test_tag_widget_hidden_when_admin_disables_tags(self):
        dash = self._makeApp()
        dash.repo.setTagsEnabled(False)
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_genre_badge_renders_when_album_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getGenresForAlbums.return_value = _byId("alb1", ["indie rock"])

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b'<span class="track-label genre-label">indie rock</span>', resp.data)
        db.getGenresForAlbums.assert_called_once_with(["alb1"])

    def _albumWithArtist(self):
        album = self._album()
        album["artists"] = [{"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "", "imageId": "a1"}]
        return album

    def test_biography_renders_when_present(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = "A landmark album from somewhere."

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b"Biography", resp.data)
        self.assertIn(b"A landmark album from somewhere.", resp.data)
        self.assertIn(b"Biography via Last.fm", resp.data)
        db.lazyFetchAlbumBio.assert_called_once_with("alb1", "Album One", "Artist A")
        db.getAlbumBio.assert_called_once_with("alb1")

    def test_biography_section_absent_without_a_bio(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = None

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertNotIn(b"Biography", resp.data)

    def test_biography_hides_when_the_admin_disables_the_feature(self):
        """Same contract as the artist bio's kill switch: disabled hides the
        section even for an album whose bio was already fetched and stored -
        db.getAlbumBio isn't even consulted for display."""
        dash = self._makeApp()
        dash.repo.setAlbumBioEnabled(False)
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = "A landmark album from somewhere."

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertNotIn(b"Biography", resp.data)
        db.getAlbumBio.assert_not_called()

    def test_biography_text_is_html_escaped(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = "<script>alert('xss')</script>"

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertNotIn(b"<script>alert", resp.data)
        self.assertIn(b"&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;", resp.data)

    def test_no_lazy_fetch_without_a_resolvable_primary_artist(self):
        """_album() (no artists) can't be looked up via album.getinfo, which
        needs an artist name - the route must skip the fetch, not crash."""
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = None

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.status_code, 200)
        db.lazyFetchAlbumBio.assert_not_called()

    def test_unknown_album_redirects_to_top_albums(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = None

        resp = self._getPath(dash, db, "/album/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-albums", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_ajax_returns_only_the_time_series_json_and_skips_heavy_work(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getListeningTimeSeries.return_value = [{"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1}]

        resp = self._getPath(dash, db, "/album/alb1?ajax=true")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(sorted(payload.keys()), ["groupBy", "timeSeries"])
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("albumId"), "alb1")
        db.getSongsStats.assert_not_called()
        db.lazyFetchAlbumBio.assert_not_called()
        db.getAlbumBio.assert_not_called()

    def test_hero_gets_play_button_but_song_sublist_keeps_spotify_anchors(self):
        """Only the hero album card gains a Play now button; the tracklist below
        keeps its Open in Spotify anchors and nothing more (the playNowButton
        flag must not leak into that loop's shared page context)."""
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = [self._song("t1", 100), self._song("t2", 200)]
        db.getListeningTimeSeries.return_value = []

        body = self._getPath(dash, db, "/album/alb1").data.decode()

        self.assertEqual(body.count(_PLAY_BUTTON), 1)
        self.assertIn('data-embed-type="album"', body)
        self.assertIn("Open in Spotify", body)
        card = _heroCard(body)
        self.assertLess(card.index(_SPOTIFY_ANCHOR), card.index(_PLAY_BUTTON))

    def test_csp_allows_unsafe_eval_for_the_spotify_embed(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn("'unsafe-eval'", resp.headers.get("Content-Security-Policy", ""))

    def test_unique_song_count_card_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b"Unique Songs Listened", resp.data)
        self.assertIn(b'<p class="summary-value">2</p>', resp.data)

    def _makeHistoryDb(self):
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getAlbumBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        return db

    def test_history_tab_scoped_to_album_id(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        self._getPath(dash, db, "/album/alb1")

        self.assertEqual(db.getEntriesCount.call_args.kwargs.get("albumId"), "alb1")
        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("albumId"), "alb1")

    def test_view_history_activates_history_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/album/alb1?view=history")

        self.assertIn(b'<div data-category="top-songs" class="">', resp.data)
        self.assertIn(b'<div data-category="history" class="visible">', resp.data)
        self.assertIn(b"History with Album One", resp.data)

    def test_default_view_activates_top_songs_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b'<div data-category="top-songs" class="visible">', resp.data)
        self.assertIn(b'<div data-category="history" class="">', resp.data)

    def test_sort_oldest_uses_entries_from_old(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/album/alb1?sort=oldest")

        self.assertEqual(db.getEntriesFromOld.call_args.kwargs.get("albumId"), "alb1")
        db.getEntriesFromNew.assert_not_called()
        self.assertIn("Date ↑".encode(), resp.data)

    def test_the_play_log_swap_skips_heavy_work(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "History Song", "playedAtText": "20 Jul 2026, 15:30", "artists": []}]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs), \
             patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            resp = self._getRaw(dash, db, "/album/alb1", headers=HX_LIST_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/html")
        resultsHtml = resp.get_data(as_text=True)
        self.assertIn("History with Album One", resultsHtml)
        self.assertIn("History Song", resultsHtml)
        db.getSongsStats.assert_not_called()
        db.lazyFetchAlbumBio.assert_not_called()
        db.getListeningTimeSeries.assert_not_called()


class TestAlbumHistoryTimeline(_DetailRouteTestBase):
    """An album you have only ever played ONE track from gets the song page's
    timeline instead of full history cards.

    The cards exist because consecutive plays on an artist/album page are
    normally different songs, so each row has to say which - see the comment in
    _detail_history_results.html. When there is only one song that reasoning
    inverts: every card repeats the same title, artist and cover, and the thing
    that actually differs between rows (when, and how much of it played) is what
    the timeline is built to show.

    uniqueSongCount is the album's own aggregate, already on the page, and it
    counts distinct NON-SKIP tracks - which is exactly what this list holds, so
    the two cannot disagree about how many songs are in it."""

    def _album(self, uniqueSongCount):
        return {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1", "imageId": "alb1",
                "imageUrl": "", "totalTracks": 12, "releaseDate": 0, "artists": [],
                "plays": 5, "totalTimeListened": 50000,
                "uniqueSongCount": uniqueSongCount, "firstListenedAt": 100}

    def _plays(self, *specs):
        """(playedAt, timePlayed) pairs, all of the same track."""
        return [{"id": "t1", "name": "The Only Song", "artists": [], "duration": 200000,
                 "playedAt": playedAt, "timePlayed": timePlayed, "isSkip": False}
                for playedAt, timePlayed in specs]

    def _db(self, uniqueSongCount, plays):
        db = MagicMock()
        db.getAlbum.return_value = self._album(uniqueSongCount)
        db.getAlbumBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getEntriesCount.return_value = len(plays)
        db.getEntriesFromNew.return_value = plays
        return db

    def _body(self, uniqueSongCount, plays, query="?view=history"):
        """The History TAB only. The page's hero is itself a .track-card, so a
        whole-page assertion about cards would always pass."""
        dash = self._makeApp()
        db = self._db(uniqueSongCount, plays)
        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            body = self._getPath(dash, db, f"/album/alb1{query}").get_data(as_text=True)
        return self._historyTab(body)

    @staticmethod
    def _historyTab(body):
        start = body.index('data-category="history"')
        return body[start:body.index('id="detailChartData"', start)]

    def test_one_played_track_gets_the_timeline(self):
        body = self._body(1, self._plays((1784560000, 190000), (1784540000, 190000)))

        self.assertIn('class="timeline-container"', body)
        self.assertIn("timeline-item", body)
        self.assertNotIn('class="track-card', body)

    def test_two_played_tracks_keep_the_cards(self):
        """The normal album page is untouched."""
        body = self._body(2, self._plays((1784560000, 190000), (1784540000, 190000)))

        self.assertIn('class="track-card', body)
        self.assertNotIn('class="timeline-container"', body)

    def test_the_timeline_still_says_which_album_it_is(self):
        body = self._body(1, self._plays((1784560000, 190000)))

        self.assertIn("History with Album One", body)

    def test_the_timeline_keeps_the_sort_toggle(self):
        """Swapping the row rendering must not cost the tab its controls."""
        body = self._body(1, self._plays((1784560000, 190000)))

        self.assertIn("sort-toggle", body)
        self.assertIn("Date ↓", body)

    def test_the_timeline_keeps_numbered_pagination(self):
        """This page pages its history; it does NOT grow it. The song page's
        "Show more" batching belongs to that page's offset/limit contract, and
        borrowing the timeline must not drag it along."""
        from app import PAGE_SIZE
        dash = self._makeApp()
        db = self._db(1, self._plays((1784560000, 190000)))
        db.getEntriesCount.return_value = PAGE_SIZE * 2 + 5

        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            body = self._historyTab(
                self._getPath(dash, db, "/album/alb1?view=history").get_data(as_text=True))

        self.assertIn('class="pagination"', body)
        self.assertIn("page=2", body)
        self.assertNotIn("Show More Plays", body)

    def test_the_timeline_labels_each_play(self):
        """The same playType vocabulary the song page's timeline uses - it is
        the same template, and _enrichSongTimelineEntries is what fills it."""
        body = self._body(1, self._plays((1784560000, 190000), (1784540000, 20000)))

        self.assertIn("play-type-full", body)
        self.assertIn("play-type-partial", body)
        self.assertIn("Partial • 10%", body)

    def test_the_timeline_carries_its_month_headers_and_gaps(self):
        """The two things the timeline adds over a bare list, and the reason
        _enrichSongTimelineEntries is the right enricher here rather than
        _attachPlayTypes. The fixture is newest-first, so the second (older)
        card's gap badge reads "earlier" (UT-14)."""
        threeHours = 10800
        body = self._body(1, self._plays((1784560000, 190000), (1784560000 - threeHours, 190000)))

        self.assertIn("timeline-date-header", body)
        self.assertIn("timeline-gap-badge", body)
        self.assertIn("3 hours earlier", body)

    def test_the_htmx_list_swap_returns_the_timeline_too(self):
        """The sort/page controls re-swap only the list, so that response has to
        agree with the one the full body rendered - otherwise changing the sort
        turns the timeline back into cards."""
        dash = self._makeApp()
        db = self._db(1, self._plays((1784560000, 190000)))

        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            resp = self._getRaw(dash, db, "/album/alb1", headers=HX_LIST_HEADERS)

        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('class="timeline-container"', body)
        self.assertNotIn('class="track-card', body)

    def test_an_album_with_no_plays_at_all_does_not_crash(self):
        """uniqueSongCount comes from the album's aggregate, which falls back to
        a skip-ranked lookup for a skip-only album - so it can report 1 while
        this list, which excludes skips, is empty."""
        body = self._body(1, [])

        self.assertIn("No plays recorded yet", body)

    def test_the_artist_page_now_gets_the_same_treatment(self):
        """SINGLE_TRACK_TIMELINE_KINDS extended this from album-only to
        (album, artist) - see TestArtistHistoryTimeline below for the full
        mirror of this class's coverage on the artist route. This one test
        stays here as the album class's own regression guard: the mechanism
        is one shared flag, so a change to it must not silently stop covering
        artists too."""
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = {"id": "a1", "name": "Artist A", "url": "u", "imageId": "a1",
                                     "imageUrl": "", "plays": 5, "totalTimeListened": 50000,
                                     "uniqueSongCount": 1, "firstListenedAt": 100}
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = self._plays((1784560000, 190000))

        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            body = self._historyTab(
                self._getPath(dash, db, "/artist/a1?view=history").get_data(as_text=True))

        self.assertIn('class="timeline-container"', body)
        self.assertNotIn('class="track-card', body)

    def test_the_timeline_row_never_shows_an_album_name_on_the_album_page(self):
        """The album-name addition to timeline rows (_play_log_rows.html) is
        guarded to kind == "artist" - an album page already names the album in
        the tab heading, so repeating it on every row would be noise. Guards
        against the shared row partial regressing this page when the artist
        branch is added."""
        plays = self._plays((1784560000, 190000))
        plays[0]["album"] = {"id": "alb1", "name": "Deluxe Reissue"}
        body = self._body(1, plays)

        self.assertNotIn("Deluxe Reissue", body)


class TestArtistHistoryTimeline(_DetailRouteTestBase):
    """The artist-page mirror of TestAlbumHistoryTimeline above
    (SINGLE_TRACK_TIMELINE_KINDS = ("album", "artist")): an artist you have
    only ever played ONE song from gets the timeline too, for the same reason
    - consecutive rows would otherwise repeat that song's title, artist and
    cover and bury what actually differs (when, and how much of it played).

    One difference from the album page: an artist's one canonical song can
    still have been played from several releases (single, album,
    compilation) - the album page has no equivalent gap, since the tab is
    already scoped to one album. _play_log_rows.html adds "· <album name>"
    to each row for kind == "artist" when the entry carries one (see
    TestArtistTimelineShowsAlbumName below)."""

    def _artist(self, uniqueSongCount):
        return {"id": "a1", "name": "Artist One", "url": "http://example.com/a1", "imageId": "a1",
                "imageUrl": "", "plays": 5, "totalTimeListened": 50000,
                "uniqueSongCount": uniqueSongCount, "firstListenedAt": 100}

    def _plays(self, *specs):
        """(playedAt, timePlayed) pairs, all of the same track."""
        return [{"id": "t1", "name": "The Only Song", "artists": [], "duration": 200000,
                 "playedAt": playedAt, "timePlayed": timePlayed, "isSkip": False}
                for playedAt, timePlayed in specs]

    def _db(self, uniqueSongCount, plays):
        db = MagicMock()
        db.getArtist.return_value = self._artist(uniqueSongCount)
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getEntriesCount.return_value = len(plays)
        db.getEntriesFromNew.return_value = plays
        return db

    def _body(self, uniqueSongCount, plays, query="?view=history"):
        """The History TAB only - see TestAlbumHistoryTimeline._body: the
        hero is itself a .track-card, so a whole-page assertion would always
        pass."""
        dash = self._makeApp()
        db = self._db(uniqueSongCount, plays)
        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            body = self._getPath(dash, db, f"/artist/a1{query}").get_data(as_text=True)
        return self._historyTab(body)

    @staticmethod
    def _historyTab(body):
        start = body.index('data-category="history"')
        return body[start:body.index('id="detailChartData"', start)]

    def test_one_played_track_gets_the_timeline(self):
        body = self._body(1, self._plays((1784560000, 190000), (1784540000, 190000)))

        self.assertIn('class="timeline-container"', body)
        self.assertIn("timeline-item", body)
        self.assertNotIn('class="track-card', body)

    def test_two_played_tracks_keep_the_cards(self):
        """The normal artist page (several songs) is untouched."""
        body = self._body(2, self._plays((1784560000, 190000), (1784540000, 190000)))

        self.assertIn('class="track-card', body)
        self.assertNotIn('class="timeline-container"', body)

    def test_the_timeline_still_says_which_artist_it_is(self):
        body = self._body(1, self._plays((1784560000, 190000)))

        self.assertIn("History with Artist One", body)

    def test_the_timeline_keeps_the_sort_toggle(self):
        """Swapping the row rendering must not cost the tab its controls."""
        body = self._body(1, self._plays((1784560000, 190000)))

        self.assertIn("sort-toggle", body)
        self.assertIn("Date ↓", body)

    def test_the_timeline_keeps_numbered_pagination(self):
        """This page pages its history; it does NOT grow it - the song
        page's "Show more" batching belongs to that page's offset/limit
        contract, and borrowing the timeline must not drag it along."""
        from app import PAGE_SIZE
        dash = self._makeApp()
        db = self._db(1, self._plays((1784560000, 190000)))
        db.getEntriesCount.return_value = PAGE_SIZE * 2 + 5

        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            body = self._historyTab(
                self._getPath(dash, db, "/artist/a1?view=history").get_data(as_text=True))

        self.assertIn('class="pagination"', body)
        self.assertIn("page=2", body)
        self.assertNotIn("Show More Plays", body)

    def test_the_timeline_labels_each_play(self):
        """The same playType vocabulary the song page's timeline uses - it is
        the same template, and _enrichSongTimelineEntries is what fills it."""
        body = self._body(1, self._plays((1784560000, 190000), (1784540000, 20000)))

        self.assertIn("play-type-full", body)
        self.assertIn("play-type-partial", body)
        self.assertIn("Partial • 10%", body)

    def test_the_timeline_carries_its_month_headers_and_gaps(self):
        """The two things the timeline adds over a bare list. The fixture is
        newest-first, so the second (older) card's gap badge reads
        "earlier"."""
        threeHours = 10800
        body = self._body(1, self._plays((1784560000, 190000), (1784560000 - threeHours, 190000)))

        self.assertIn("timeline-date-header", body)
        self.assertIn("timeline-gap-badge", body)
        self.assertIn("3 hours earlier", body)

    def test_the_htmx_list_swap_returns_the_timeline_too(self):
        """The sort/page controls re-swap only the list, so that response has
        to agree with the one the full body rendered - otherwise changing the
        sort turns the timeline back into cards."""
        dash = self._makeApp()
        db = self._db(1, self._plays((1784560000, 190000)))

        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            resp = self._getRaw(dash, db, "/artist/a1", headers=HX_LIST_HEADERS)

        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('class="timeline-container"', body)
        self.assertNotIn('class="track-card', body)

    def test_an_artist_with_no_plays_at_all_does_not_crash(self):
        """uniqueSongCount comes from the artist's aggregate, which falls back
        to a skip-ranked lookup for a skip-only artist - so it can report 1
        while this list, which excludes skips, is empty. Mirrors
        TestAlbumHistoryTimeline's identical disagreement case."""
        body = self._body(1, [])

        self.assertIn("No plays recorded yet", body)


class TestArtistTimelineShowsAlbumName(_DetailRouteTestBase):
    """The one deliberate difference from the album page's timeline (see the
    "Known UX loss" note in implementationPlan-2026-09-07.md, feature 4): an
    artist's one canonical song can still have plays across several releases
    (single, album, compilation), which the timeline otherwise drops. Since
    getEntriesFromNew's hydrated entries already carry the track's `album`
    dict (the same one _track_card.html reads at line 218-224), this is a
    template-only addition, guarded to kind == "artist" so the album page -
    which already names its one album in the tab heading - is unchanged."""

    def _db(self, plays):
        db = MagicMock()
        db.getArtist.return_value = {"id": "a1", "name": "Artist One", "url": "u", "imageId": "a1",
                                     "imageUrl": "", "plays": 5, "totalTimeListened": 50000,
                                     "uniqueSongCount": 1, "firstListenedAt": 100}
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getEntriesCount.return_value = len(plays)
        db.getEntriesFromNew.return_value = plays
        return db

    @staticmethod
    def _historyTab(body):
        start = body.index('data-category="history"')
        return body[start:body.index('id="detailChartData"', start)]

    def test_the_album_name_appears_on_an_artist_timeline_row(self):
        plays = [{"id": "t1", "name": "The Only Song", "artists": [], "duration": 200000,
                  "playedAt": 1784560000, "timePlayed": 190000, "isSkip": False,
                  "album": {"id": "alb1", "name": "The Single Version"}}]
        dash = self._makeApp()
        db = self._db(plays)

        with patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            body = self._historyTab(
                self._getPath(dash, db, "/artist/a1?view=history").get_data(as_text=True))

        self.assertIn("The Single Version", body)


class TestDetailPageDeferredBody(_DetailRouteTestBase):
    """The two-phase split itself: the plain GET is a shell and everything
    below the toolbar arrives in a second request htmx makes on first paint
    (see DETAIL_BODY_TARGET in routes/details.py and static/js/detail-page.js).
    These drive the two requests through _getRaw rather than the composing
    _getPath the markup tests use.

    The transport is pinned in tests/test_detail_htmx.py; what is asserted here
    is which QUERIES land on which side of the split."""

    #< every query the split moved off the first paint, per page
    DEFERRED_LOOKUPS = {
        "/song/t1": ("getListeningTimeSeries", "getHourOfDayHeatmap", "getPlayBuckets",
                     "getSkipStats", "getEntriesCount", "getEntriesFromNew", "getGenresForTracks"),
        "/artist/a1": ("getListeningTimeSeries", "getSkipStats", "getSongsStats",
                       "getEntriesCount", "lazyFetchArtistBio"),
        "/album/alb1": ("getListeningTimeSeries", "getSkipStats", "getSongsStats",
                        "getEntriesCount", "lazyFetchAlbumBio"),
    }

    def _db(self):
        db = MagicMock()
        db.getSong.return_value = {
            "id": "t1", "name": "Song One", "url": "http://example.com/t1", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "artists": [{"id": "a1", "name": "Artist A", "url": "u",
                                            "imageUrl": "", "imageId": "a1"}],
            "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                       "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }
        db.getArtist.return_value = {"id": "a1", "name": "Artist A", "url": "u", "imageId": "a1",
                                      "imageUrl": "", "plays": 5, "totalTimeListened": 50000,
                                      "firstListenedAt": 100, "uniqueSongCount": 1}
        db.getAlbum.return_value = {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                     "imageUrl": "", "totalTracks": 1, "releaseDate": 0, "artists": [],
                                     "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
                                     "uniqueSongCount": 1}
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getPlayBuckets.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0}
                                                for _ in range(24)] for _ in range(7)]
        db.getSkipStats.return_value = {"plays": 5, "skips": 1, "skipPercent": 16.7}
        db.getArtistBio.return_value = None
        db.getAlbumBio.return_value = None
        return db

    def test_the_shell_names_the_entity_and_holds_a_placeholder(self):
        for path, name in (("/song/t1", "Song One"), ("/artist/a1", "Artist A"),
                           ("/album/alb1", "Album One")):
            with self.subTest(path=path):
                dash = self._makeApp()

                body = self._getRaw(dash, self._db(), path).data.decode()

                self.assertIn(name, body)                    #< the hero identifies the page at once
                self.assertIn('id="detailBody"', body)
                self.assertIn('class="detail-skeleton"', body)
                self.assertIn("js/detail-page.js", body)

    def test_the_shell_leaves_every_heavy_lookup_to_the_deferred_load(self):
        """The point of the split: none of these run before the first paint."""
        for path, lookups in self.DEFERRED_LOOKUPS.items():
            with self.subTest(path=path):
                dash = self._makeApp()
                db = self._db()

                self._getRaw(dash, db, path)

                for name in lookups:
                    getattr(db, name).assert_not_called()

    def test_the_body_opens_with_the_hero_as_detailBodys_own_child(self):
        """The hero .track-list is the fragment's first element, so once htmx
        swaps the body in it is a DIRECT child of #detailBody - which is what
        the spacing rule keyed on in TestDetailHeroSpacingCss below matches.
        Nesting it any deeper drops the gap between the hero card and whatever
        follows it."""
        for path in self.DEFERRED_LOOKUPS:
            with self.subTest(path=path):
                dash = self._makeApp()

                body = self._getRaw(dash, self._db(), path,
                                    headers=HX_BODY_HEADERS).get_data(as_text=True)

                self.assertTrue(body.strip().startswith(
                    '<section id="track-list" class="track-list">'), body[:120])

    def test_each_page_with_its_body_has_one_h1_and_no_skipped_level(self):
        """The hero card's h3 sat straight under the shell's h1 (2026-09-02
        review, UI-05); see tests/_headings.py."""
        from _headings import assertHeadingOrder
        for path in self.DEFERRED_LOOKUPS:
            with self.subTest(path=path):
                dash = self._makeApp()

                page = self._getPath(dash, self._db(), path).get_data(as_text=True)

                assertHeadingOrder(self, page, path)

    def test_the_body_tabs_carry_aria_pressed_for_the_open_view(self):
        """The Top Songs / History tabs are toggle buttons whose state was a
        class alone (2026-09-02 review, UI-03): the body renders aria-pressed
        on both, true on the tab ?view= opened, and detail-history.js keeps
        it in step on click."""
        import bs4
        for path, pressed in (("/artist/a1", "top-songs"), ("/album/alb1?view=history", "history")):
            with self.subTest(path=path):
                dash = self._makeApp()

                body = self._getRaw(dash, self._db(), path, headers=HX_BODY_HEADERS).get_data(as_text=True)
                tabs = bs4.BeautifulSoup(body, "html.parser").select(".stats-filter-button")

                self.assertEqual(len(tabs), 2)
                self.assertTrue(all(tab.has_attr("aria-pressed") for tab in tabs))
                self.assertEqual([tab["data-filter"] for tab in tabs if tab["aria-pressed"] == "true"], [pressed])

    def test_the_shell_disables_the_bucket_select_until_the_body_lands(self):
        """Its ?ajax=true refetch targets a chart that isn't on the page yet,
        and its result would be overwritten by the body payload in flight."""
        dash = self._makeApp()

        body = self._getRaw(dash, self._db(), "/song/t1").data.decode()

        selectTag = body[body.index('<select id="groupBy"'):]
        self.assertIn("disabled", selectTag[:selectTag.index(">")])

    def test_the_deferred_body_carries_the_markup_and_the_chart_data(self):
        for path, key in (("/song/t1", "Song One"), ("/artist/a1", "Songs by Artist A"),
                          ("/album/alb1", "Top Songs on Album One")):
            with self.subTest(path=path):
                dash = self._makeApp()

                resp = self._getRaw(dash, self._db(), path, headers=HX_BODY_HEADERS)

                self.assertEqual(resp.mimetype, "text/html")
                body = resp.get_data(as_text=True)
                self.assertIn("track-card", body)
                self.assertIn(key, body)
                self.assertIn("timeSeries", body)   #< the data island, see _detail_chart_data.html

    def test_only_the_song_body_carries_a_heatmap(self):
        """Its "When You Listen" canvas is the only one on the three pages."""
        dash = self._makeApp()

        songBody = self._getRaw(dash, self._db(), "/song/t1",
                                headers=HX_BODY_HEADERS).get_data(as_text=True)
        artistBody = self._getRaw(dash, self._db(), "/artist/a1",
                                  headers=HX_BODY_HEADERS).get_data(as_text=True)

        self.assertIn("heatmap", songBody)
        self.assertNotIn("heatmap", artistBody)

    def test_the_deferred_body_brings_the_play_log_with_it(self):
        dash = self._makeApp()
        db = self._db()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "Song One", "playedAtText": "20 Jul 2026, 15:30",
             "timePlayedText": "3m 20s", "contextName": None, "artists": []}]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            body = self._getRaw(dash, db, "/song/t1",
                                headers=HX_BODY_HEADERS).get_data(as_text=True)

        self.assertIn("Play Timeline", body)
        self.assertIn("20 Jul 2026, 15:30", body)

    def test_the_narrower_modes_keep_their_own_branches(self):
        """The bucket series and the play log are partial refetches OF the body
        this adds, so both must still answer with their own response rather than
        falling through to the shell or to the whole body."""
        for path in ("/song/t1", "/artist/a1", "/album/alb1"):
            with self.subTest(path=path):
                dash = self._makeApp()

                seriesPayload = self._getRaw(dash, self._db(), f"{path}?ajax=true").get_json()
                log = self._getRaw(dash, self._db(), path,
                                   headers=HX_LIST_HEADERS).get_data(as_text=True)

                self.assertEqual(sorted(seriesPayload.keys()), ["groupBy", "timeSeries"])
                self.assertIn("sort-toggle", log)
                self.assertNotIn("detailChartData", log)   #< the whole body's island

    def test_an_unrecognised_ajax_value_falls_back_to_the_shell(self):
        """?ajax= now marks exactly one mode, the JSON chart series. Anything
        else is a page load, so a stale or hand-edited value can't serve a
        fragment as a page."""
        dash = self._makeApp()
        db = self._db()

        resp = self._getRaw(dash, db, "/song/t1?ajax=nonsense")

        self.assertEqual(resp.mimetype, "text/html")
        self.assertIn('id="detailBody"', resp.data.decode())
        db.getSkipStats.assert_not_called()

    def _missingEntityApp(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = None
        db.getArtist.return_value = None
        db.getAlbum.return_value = None
        return dash, db

    MISSING_PATHS = (("/song/missing", "/top-songs"), ("/artist/missing", "/top-artists"),
                     ("/album/missing", "/top-albums"))

    def test_an_unknown_entity_is_resolved_before_any_of_this(self):
        """A plain GET still redirects, as it always has."""
        for path, endpoint in self.MISSING_PATHS:
            with self.subTest(path=path):
                dash, db = self._missingEntityApp()

                resp = self._getRaw(dash, db, path)

                self.assertEqual(resp.status_code, 302)
                self.assertIn(endpoint, resp.headers["Location"])

    def test_an_unknown_entity_tells_the_fetch_based_mode_where_to_go(self):
        """A second request must NOT get the 302: fetch follows it transparently
        to the top-list page's 200 HTML, which passes resp.ok and then throws in
        resp.json() - so the visitor saw "couldn't load" and a Retry that behaved
        identically, rather than arriving at the list.

        Reachable from a shared or bookmarked URL for an entity an overwrite
        import removed between the shell request and the body request. Shaped
        like unauthenticatedResponse's loginUrl, so the client has one convention
        for "go here instead". The htmx modes get HX-Redirect instead - see
        tests/test_detail_htmx.py's TestMissingEntitySwap."""
        for path, endpoint in self.MISSING_PATHS:
            with self.subTest(path=path):
                dash, db = self._missingEntityApp()

                resp = self._getRaw(dash, db, path + "?ajax=true")

                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.mimetype, "application/json")
                self.assertIn(endpoint, resp.get_json()["redirectUrl"])


class TestDetailHeroSpacingCss(unittest.TestCase):
    """The stylesheet half of the gap under a detail page's hero card.

    `.page > .track-list` gave that card its gap, and the two-phase split
    moved the list out of main.page and into #detailBody - a direct-child
    selector the wrapper silently broke. It went unnoticed because the Spotify
    embed sits in that gap and carries a margin of its own, so the hero only
    collapses onto the card below it while the player is closed, which is
    every page load."""

    def setUp(self):
        cssPath = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")
        with open(cssPath, encoding="utf-8") as handle:
            self.css = handle.read()

    def _selectorsGivingATrackListItsGapBelow(self):
        import re
        rules = re.findall(r"([^{}]*)\{([^{}]*)\}", self.css)
        return [selector.strip() for selector, block in rules
                if ".track-list" in selector and "margin-bottom" in block]

    def test_a_track_list_sitting_on_the_page_gets_a_gap_below_it(self):
        self.assertTrue(self._selectorsGivingATrackListItsGapBelow(),
                        "no rule gives a .track-list a bottom margin")

    def test_the_deferred_detail_body_counts_as_sitting_on_the_page(self):
        selectors = " ".join(self._selectorsGivingATrackListItsGapBelow())

        self.assertIn("#detailBody", selectors,
                      "the detail pages' hero list lives inside #detailBody and is missed")


class TestPlayHistoryChartLegend(_DetailRouteTestBase):
    """The Play History chart's key.

    Only the detail pages draw the skips series, as a second, narrower bar per
    bucket on its own count scale (skips carry no listening time, so they can't
    share the millisecond axis - see charts.js's renderTimeSeriesChart). Two
    differently coloured bars shared each slot under a single axis with nothing
    naming either one, so the skip bar read as a second measure of listening
    against a scale it isn't drawn to. charts.js fills this slot in; without it
    in the markup there is nowhere for the key to land."""

    LEGEND_SLOT = b'id="timeSeriesLegend"'

    def _assertChartIsKeyed(self, body):
        self.assertIn(b'id="timeSeriesChart"', body)
        self.assertIn(self.LEGEND_SLOT, body)

    def test_song_page_carries_the_legend_slot(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = {
            "id": "t1", "name": "Song One", "url": "u", "imageId": "alb1", "duration": 200000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": None, "artists": [],
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getSkipStats.return_value = {"plays": 5, "skips": 2, "skipPercent": 28.6}

        resp = self._getPath(dash, db, "/song/t1")

        self._assertChartIsKeyed(resp.data)

    def test_artist_page_carries_the_legend_slot(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = {"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "",
                                     "imageId": "a1", "plays": 5, "totalTimeListened": 50000,
                                     "uniqueSongCount": 2, "firstListenedAt": 100}
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self._assertChartIsKeyed(resp.data)

    def test_album_page_carries_the_legend_slot(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                    "imageUrl": "", "totalTracks": 2, "releaseDate": 0, "artists": [],
                                    "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2,
                                    "firstListenedAt": 100}
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self._assertChartIsKeyed(resp.data)

    def test_an_unfilled_legend_takes_up_no_room(self):
        """The slot ships on every detail page but is filled only where there
        are skip bars to name, and .chart-legend carries a top margin - so
        without this the majority of pages (and every legend that renders after
        its data lands) pay 14px of dead space under the chart."""
        import os
        import re
        cssPath = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")
        with open(cssPath, encoding="utf-8") as handle:
            css = handle.read()

        rules = re.findall(r"([^{}]*)\{([^{}]*)\}", css)
        hidden = [selector.strip() for selector, block in rules
                  if ".chart-legend:empty" in selector and "display: none" in block]

        self.assertTrue(hidden, "an empty .chart-legend still reserves its margin")

    def test_the_aggregate_pages_have_no_legend_slot(self):
        """/charts and /wrapped leave the skips series off, so their chart is
        one series against one axis - a key there would be noise. charts.js
        no-ops without the element, which is what keeps them unchanged.

        Named by the partial rather than the page: both moved their chart into
        the fragment htmx swaps, so the page template no longer holds a canvas
        at all - and asserting on a file that cannot contain the canvas would
        pass for the wrong reason forever."""
        import os
        for name in ("_charts_results.html", "_wrapped_results.html"):
            path = os.path.join(os.path.dirname(__file__), "..", "templates", name)
            with open(path, encoding="utf-8") as handle:
                markup = handle.read()
            self.assertIn('id="timeSeriesChart"', markup)
            self.assertNotIn('id="timeSeriesLegend"', markup, f"{name} should not be keyed")


class TestPlayHistoryChartAccessibleName(_DetailRouteTestBase):
    """The chart canvas is a role="img" named for its page (2026-09-02 range
    review, RR-4). Since ea79499 both pages render one template that reads
    `entityKind` into the name; nothing pinned the interpolation, so a copy
    hardcoding either kind would have passed every detail-page test."""

    def _chartName(self, entityKind):
        return f'aria-label="Play history for this {entityKind} over time"'.encode()

    def test_the_artist_chart_is_named_for_an_artist(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = {"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "",
                                     "imageId": "a1", "plays": 5, "totalTimeListened": 50000,
                                     "uniqueSongCount": 2, "firstListenedAt": 100}
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(self._chartName("artist"), resp.data)
        self.assertNotIn(self._chartName("album"), resp.data)

    def test_the_album_chart_is_named_for_an_album(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                    "imageUrl": "", "totalTracks": 2, "releaseDate": 0, "artists": [],
                                    "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2,
                                    "firstListenedAt": 100}
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(self._chartName("album"), resp.data)
        self.assertNotIn(self._chartName("artist"), resp.data)


if __name__ == "__main__":
    unittest.main()
