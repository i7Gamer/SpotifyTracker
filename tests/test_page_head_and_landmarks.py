"""Per-page <title>s, and the keyboard route into the page content.

Three things a route test can't see and a browser-less suite otherwise never
checks:

* Every page shipped the same fixed `<title>`, so a stack of open tabs, a
  browser-history search and a bookmark all read "SpotifyTracker" with no way
  to tell one page from another.
* There was no skip link, so reaching the main content by keyboard meant
  tabbing through the whole topbar - every badge, both dropdowns, the account
  menu - on every page load.
* A track card links the cover image and the title to the SAME destination, so
  every card cost two tab stops for one action. On a Top page of 50 cards that
  is 50 dead stops.

File-level assertions, plus one live render so the static markers are checked
against a page the app actually produces.
"""
import re
import unittest
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _ROOT / "templates"
_CSS_PATH = _ROOT / "static" / "css" / "style.css"

_LAYOUTS = ("layout.html", "layout_public.html")

# The suffix every page's title keeps, so a tab is still identifiable as this
# app once the page-specific part is truncated by a narrow tab strip.
_TITLE_SUFFIX = "SpotifyTracker"

_MAIN_ID = "main-content"
_SKIP_LINK_CLASS = "skip-link"

_EXTENDS_RE = re.compile(r"{%-?\s*extends\s")
_TITLE_BLOCK_RE = re.compile(r"{%-?\s*block\s+title\s*-?%}")


def _read(path):
    return path.read_text(encoding="utf-8")


def _pageTemplates():
    """Every template that extends another one - i.e. renders a whole page
    rather than a fragment. Derived rather than listed so a new page can't be
    added without a title."""
    return sorted(
        path for path in _TEMPLATES.glob("*.html")
        if _EXTENDS_RE.search(_read(path))
    )


class TestPageTitles(unittest.TestCase):
    def test_layouts_expose_a_title_block_with_the_app_name_kept(self):
        for name in _LAYOUTS:
            with self.subTest(layout=name):
                source = _read(_TEMPLATES / name)
                match = re.search(r"<title>(.*?)</title>", source, re.DOTALL)
                self.assertIsNotNone(match, "layout must still render a <title>")
                title = match.group(1)
                self.assertIsNotNone(_TITLE_BLOCK_RE.search(title),
                                     "the <title> must be overridable per page")
                self.assertIn(_TITLE_SUFFIX, title)

    def test_every_page_template_sets_its_own_title(self):
        missing = [
            path.name for path in _pageTemplates()
            if not _TITLE_BLOCK_RE.search(_read(path))
        ]

        self.assertEqual(missing, [], "page templates with no {% block title %}: " + ", ".join(missing))

    def test_titles_are_distinct_between_pages(self):
        """A block that every page fills with the same words is the original
        bug wearing a block tag."""
        titles = {}
        for path in _pageTemplates():
            match = re.search(r"{%-?\s*block\s+title\s*-?%}(.*?){%-?\s*endblock", _read(path), re.DOTALL)
            if match:
                titles.setdefault(match.group(1).strip(), []).append(path.name)

        # The profile tabs legitimately share a stem, so compare on the whole
        # set rather than demanding 1:1 - what must not happen is one title
        # covering most of the app.
        worst = max(titles.values(), key=len)
        self.assertLessEqual(len(worst), 2, "too many pages share a title: " + ", ".join(worst))


class TestSkipLink(unittest.TestCase):
    def test_both_layouts_open_the_body_with_a_skip_link(self):
        for name in _LAYOUTS:
            with self.subTest(layout=name):
                source = _read(_TEMPLATES / name)
                bodyAt = source.index("<body>")
                skipLink = re.search(rf"<a[^>]*{_SKIP_LINK_CLASS}[^>]*>", source[bodyAt:])
                self.assertIsNotNone(skipLink, "no skip link in the body")
                skipAt = bodyAt + skipLink.start()
                # "<main " with the space: the layouts' own comments mention a
                # bare "<main>", and matching that instead would test prose.
                mainAt = source.index("<main ", bodyAt)

                self.assertIn(f'href="#{_MAIN_ID}"', skipLink.group(0))
                self.assertLess(skipAt, mainAt, "the skip link must precede the content it skips to")
                # Nothing focusable may sit between <body> and the skip link,
                # or the very first Tab lands somewhere else.
                self.assertNotIn("<a ", source[bodyAt:skipAt])
                self.assertNotIn("<button", source[bodyAt:skipAt])

    def test_main_is_the_skip_target_and_can_hold_focus(self):
        """Without tabindex="-1" the anchor scrolls but focus stays on the
        skip link, so the next Tab continues from the topbar - the exact
        traversal the link exists to avoid."""
        for name in _LAYOUTS:
            with self.subTest(layout=name):
                source = _read(_TEMPLATES / name)
                main = re.search(r"<main\s[^>]*>", source).group(0)
                self.assertIn(f'id="{_MAIN_ID}"', main)
                self.assertIn('tabindex="-1"', main)

    def test_skip_link_is_hidden_until_focused(self):
        css = _read(_CSS_PATH)
        rule = re.search(r"\.skip-link\s*\{([^}]*)\}", css)
        focusRule = re.search(r"\.skip-link:focus\s*\{([^}]*)\}", css)

        self.assertIsNotNone(rule, "missing .skip-link rule")
        self.assertIsNotNone(focusRule, "missing .skip-link:focus rule")
        # display:none / visibility:hidden would take it out of the tab order
        # too, which defeats the point; it has to be moved, not removed.
        self.assertNotIn("display: none", rule.group(1))
        self.assertNotIn("visibility: hidden", rule.group(1))


class TestListenerStatusPillText(unittest.TestCase):
    """UI-07 (2026-09-02 review): #listener-status-pill's state was colour (its
    class) plus a hover-only title - nothing a screen reader could read
    without hovering an element that carried no text of its own.
    static/js/layout-chrome.js's updateListenerStatus writes the SAME words
    into #listener-status-text that it writes into the title (see
    tests/test_layout_chrome.js for the runtime half); this pins the static
    markup only layout.html renders (layout_public.html has no pill - a
    logged-out visitor has no listener to report on)."""

    def test_the_pill_carries_a_visually_hidden_text_span(self):
        source = _read(_TEMPLATES / "layout.html")
        pill = re.search(r'<span id="listener-status-pill"[^>]*>(.*?)</span>\s*</a>', source, re.DOTALL)

        self.assertIsNotNone(pill, "no #listener-status-pill in layout.html")
        inner = re.search(r'<span class="visually-hidden" id="listener-status-text">([^<]*)</span>',
                           pill.group(1))
        self.assertIsNotNone(inner, "no #listener-status-text inside the pill")

    def test_its_initial_text_matches_the_pill_s_own_title(self):
        """The two must agree even before any poll has landed, or the very
        first screen-reader announcement disagrees with what a sighted user
        sees on hover."""
        source = _read(_TEMPLATES / "layout.html")
        pill = re.search(r'<span id="listener-status-pill"[^>]*title="([^"]*)"[^>]*>(.*?)</span>\s*</a>',
                          source, re.DOTALL)
        self.assertIsNotNone(pill)
        title = pill.group(1)
        inner = re.search(r'<span class="visually-hidden" id="listener-status-text">([^<]*)</span>',
                           pill.group(2))
        self.assertIsNotNone(inner)
        self.assertEqual(inner.group(1), title)

    def test_layout_public_has_no_pill_to_speak_of(self):
        """A logged-out visitor has no listener state to report - confirms the
        span belongs only where the poll that fills it in also runs."""
        source = _read(_TEMPLATES / "layout_public.html")
        self.assertNotIn("listener-status-pill", source)


class TestTrackCardTabStops(unittest.TestCase):
    def setUp(self):
        self.source = _read(_TEMPLATES / "_track_card.html")

    def test_cover_links_are_out_of_the_tab_order(self):
        """The cover link and the title link go to the same place, so only one
        of them should be reachable by keyboard - and it should be the one with
        a readable accessible name."""
        coverLinks = re.findall(r"<a[^>]*track-cover-link[^>]*>", self.source)

        self.assertTrue(coverLinks, "no cover links found - has the markup moved?")
        for link in coverLinks:
            with self.subTest(link=link):
                self.assertIn('tabindex="-1"', link)
                self.assertIn('aria-hidden="true"', link)

    def test_the_title_link_stays_reachable(self):
        """Whatever is done to the cover, the heading link must keep its tab
        stop - it is the one carrying the accessible name."""
        headerArea = self.source[self.source.index('class="track-header"'):]
        firstAnchor = re.search(r"<a[^>]*>", headerArea).group(0)

        self.assertNotIn("tabindex", firstAnchor)
        self.assertNotIn("aria-hidden", firstAnchor)


class TestRenderedPageHead(AppTestCase):
    """One live render, so the static markers above are checked against markup
    the app actually emits rather than only against template source."""

    def test_login_page_has_its_own_title_and_a_skip_link(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        html = client.get("/login").get_data(as_text=True)

        title = re.search(r"<title>(.*?)</title>", html, re.DOTALL).group(1).strip()
        self.assertIn(_TITLE_SUFFIX, title)
        self.assertNotEqual(title, _TITLE_SUFFIX, "the page-specific part is missing")
        self.assertIn(f'href="#{_MAIN_ID}"', html)
        self.assertIn(f'id="{_MAIN_ID}"', html)


if __name__ == "__main__":
    unittest.main()
