"""The topbar brand is a logo now, not the app's name spelled out.

Three things go wrong when a wordmark replaces text, and none of them are
visible to a route test:

* The link loses its accessible name. `.brand` is the link back to the
  Overview page; with the words gone, the only thing left to announce it is
  the `alt` on the image inside it. An empty or missing one leaves a screen
  reader reading out the href.
* The bar jumps on every page load. `.topbar` is sticky and `flex-wrap`, so an
  image with no intrinsic size reserved reflows the whole row (and the badges
  beside it) the moment it decodes.
* The name drifts. The words survive in the places an image cannot go - the
  `<title>`, the PWA manifest - and nothing connected those to the logo, so a
  rename could land in one and not the other.

File-level assertions: the wordmark is markup and a PNG, with no app behaviour
in between. The image dimensions are read from each PNG's own header, so the
declared `width`/`height` are checked against the file rather than against a
number written down twice.
"""
import json
import re
import struct
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _ROOT / "templates"
_STATIC = _ROOT / "static"
_CSS_PATH = _STATIC / "css" / "style.css"
_MANIFEST_PATH = _STATIC / "manifest.json"

#< the one layout with a topbar; layout_public.html renders the shared-Wrapped
#  page as bare content and has no brand link to check
_LAYOUT = "layout.html"

#< the app's name wherever an image cannot carry it, spelled as the logo spells
#  it. Every consumer below is asserted against this one string.
_APP_NAME = "SpotifyTracker"

#< the full wordmark, and the square mark that replaces it on a phone
_BRAND_IMAGE_CLASSES = ("brand-logo", "brand-mark")

#< a declared width/height that disagrees with the file by more than this
#  fraction is squashing or stretching the artwork rather than scaling it
_ASPECT_TOLERANCE = 0.02

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#< width and height are the first two fields of the IHDR chunk's data, which
#  starts at byte 16 of every PNG - see tests/test_web_manifest.py, which reads
#  them the same way rather than taking an image library for a header read
_PNG_DIMENSIONS_OFFSET = 16
_PNG_DIMENSIONS_FORMAT = ">II"

#< `filename='images/brand/wordmark-on-dark.png'` out of a url_for() call left
#  in the attribute by the template - bs4 hands these over as literal text
_URL_FOR_FILENAME_RE = re.compile(r"filename=['\"]([^'\"]+)['\"]")

#< a Jinja comment is a text node to an HTML parser but nothing to a browser,
#  and the brand link is full of them - so they come out before bs4 sees the
#  markup, or "is there stray text beside the logo?" answers yes forever
_JINJA_COMMENT_RE = re.compile(r"{#.*?#}", re.DOTALL)

#< every `selector { ...declarations... }` in the stylesheet. The body excludes
#  braces, so an `@media` wrapper is not matched as a rule of its own - the
#  scan simply steps past its `{` and finds the rules nested inside it.
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")

#< `.brand-mark` inside `.brand-logo, .brand-mark` but not inside
#  `.brand-mark-large`. \b cannot do this: there is no word boundary between a
#  space and the leading dot, so `\b\.brand-mark` never matches at all.
def _selectorNamesClass(selectorList, className):
    return re.search(r"(?<![\w-])\." + re.escape(className) + r"(?![\w-])",
                     selectorList) is not None


def _pngDimensions(path):
    data = path.read_bytes()
    assert data[:len(_PNG_SIGNATURE)] == _PNG_SIGNATURE, f"{path} is not a PNG"
    return struct.unpack(_PNG_DIMENSIONS_FORMAT,
                         data[_PNG_DIMENSIONS_OFFSET:_PNG_DIMENSIONS_OFFSET + 8])


def _layoutSource():
    return _JINJA_COMMENT_RE.sub("", (_TEMPLATES / _LAYOUT).read_text(encoding="utf-8"))


def _brandLink():
    soup = BeautifulSoup(_layoutSource(), "html.parser")
    link = soup.find("a", class_="brand")
    assert link is not None, "layout.html no longer has a .brand link"
    return link


def _brandImages():
    """{class: <img>} for each image inside the brand link."""
    return {className: _brandLink().find("img", class_=className)
            for className in _BRAND_IMAGE_CLASSES}


def _staticPathOf(image):
    match = _URL_FOR_FILENAME_RE.search(image.get("src", ""))
    assert match, f"brand image src is not a url_for static call: {image.get('src')!r}"
    return _STATIC / match.group(1)


def _cssBlocksFor(className):
    """Every declaration body in style.css whose selector list names
    `className`, in source order, with the offset each was found at - so the
    caller can ask which media query, if any, it fell inside."""
    source = _CSS_PATH.read_text(encoding="utf-8")
    return [(match.group(1).strip(), match.group(2), match.start())
            for match in _CSS_RULE_RE.finditer(source)
            if _selectorNamesClass(match.group(1), className)]


def _mediaQueryAround(position):
    """The `@media (...)` whose block contains `position`, or None at the top
    level. Good enough for this stylesheet, which never nests media queries."""
    source = _CSS_PATH.read_text(encoding="utf-8")
    depth = 0
    opener = None
    for match in re.finditer(r"@media([^{]*)\{|\{|\}", source[:position]):
        token = match.group(0)
        if token.startswith("@media"):
            if depth == 0:
                opener = match.group(1).strip()
            depth += 1
        elif token == "{":
            if depth:
                depth += 1
        else:
            if depth:
                depth -= 1
                if depth == 0:
                    opener = None
    return opener


class TestBrandMarkup(unittest.TestCase):
    def test_the_brand_link_shows_the_logo_rather_than_the_name_in_text(self):
        """The whole point of the change: the words are an image now. A stray
        text node here would render the name twice, once beside itself."""
        link = _brandLink()

        self.assertTrue(link.find("img"), "the brand link carries no image")
        visibleText = "".join(node for node in link.find_all(string=True, recursive=False)).strip()
        self.assertEqual(visibleText, "",
                         f"the brand link still spells out {visibleText!r} beside the logo")

    def test_both_the_wordmark_and_the_phone_mark_are_present(self):
        for className, image in _brandImages().items():
            with self.subTest(image=className):
                self.assertIsNotNone(image, f"no .{className} inside the brand link")

    def test_each_brand_image_names_the_app_so_the_link_keeps_a_name(self):
        """With the text gone, `alt` is the link's only accessible name - and
        it has to be on BOTH, because either one can be the displayed copy."""
        for className, image in _brandImages().items():
            with self.subTest(image=className):
                self.assertEqual(image.get("alt"), _APP_NAME)

    def test_each_brand_image_file_exists(self):
        for className, image in _brandImages().items():
            with self.subTest(image=className):
                self.assertTrue(_staticPathOf(image).is_file(),
                                f"{_staticPathOf(image)} is missing")

    def test_each_brand_image_reserves_its_space(self):
        """A sticky, flex-wrap bar reflows around a late-decoding image."""
        for className, image in _brandImages().items():
            with self.subTest(image=className):
                self.assertTrue(image.get("width") and image.get("height"),
                                "brand images need width and height attributes")

    def test_the_declared_size_matches_the_files_own_shape(self):
        """Scaled, not squashed: the attributes may be smaller than the file
        (they are - the PNGs are 2x for HiDPI) but not a different shape."""
        for className, image in _brandImages().items():
            with self.subTest(image=className):
                fileWidth, fileHeight = _pngDimensions(_staticPathOf(image))
                declared = int(image["width"]) / int(image["height"])
                actual = fileWidth / fileHeight

                self.assertAlmostEqual(declared, actual, delta=actual * _ASPECT_TOLERANCE,
                                       msg=f"declared {image['width']}x{image['height']} is not "
                                           f"the shape of the {fileWidth}x{fileHeight} file")

    def test_the_wordmark_is_served_at_twice_its_rendered_size(self):
        """The reason the attributes are allowed to disagree with the file at
        all. Below 2x a HiDPI screen upscales the logo and it goes soft."""
        image = _brandImages()["brand-logo"]
        fileWidth, _ = _pngDimensions(_staticPathOf(image))

        self.assertGreaterEqual(fileWidth, int(image["width"]) * 2)


class TestBrandResponsiveSwap(unittest.TestCase):
    """One of the two images is displayed at any width, never both and never
    neither - the bar would otherwise show the name twice, or nothing."""

    def _displayRules(self, className):
        return [(_mediaQueryAround(position), re.search(r"display:\s*([\w-]+)", body))
                for _, body, position in _cssBlocksFor(className)]

    def test_the_phone_mark_is_hidden_by_default(self):
        rules = [(media, match) for media, match in self._displayRules("brand-mark")
                 if media is None and match]

        self.assertTrue(rules, ".brand-mark has no top-level display rule")
        self.assertEqual(rules[-1][1].group(1), "none")

    def test_a_narrow_viewport_swaps_the_wordmark_for_the_mark(self):
        hidden = [media for media, match in self._displayRules("brand-logo")
                  if media and match and match.group(1) == "none"]
        shown = [media for media, match in self._displayRules("brand-mark")
                 if media and match and match.group(1) != "none"]

        self.assertTrue(hidden, ".brand-logo is never hidden on a narrow screen")
        self.assertTrue(shown, ".brand-mark is never shown on a narrow screen")
        self.assertEqual(set(hidden), set(shown),
                         "the two halves of the swap fire at different widths, so there "
                         "is a range showing both logos or neither")

    def test_the_swap_is_a_max_width_query(self):
        """A min-width query here would invert the whole thing: the mark would
        take over the wide screens and the wordmark the phones."""
        for media in {media for media, match in self._displayRules("brand-mark")
                      if media and match and match.group(1) != "none"}:
            with self.subTest(media=media):
                self.assertIn("max-width", media)


class TestAppNameIsWrittenOnce(unittest.TestCase):
    """Everywhere the name survives as text, it survives as the SAME text."""

    def test_the_manifest_names_the_app(self):
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], _APP_NAME)
        self.assertEqual(manifest["short_name"], _APP_NAME)

    def test_both_layouts_fall_back_to_it_in_the_title(self):
        for layout in ("layout.html", "layout_public.html"):
            with self.subTest(layout=layout):
                source = (_TEMPLATES / layout).read_text(encoding="utf-8")
                title = re.search(r"<title>(.*?)</title>", source, re.DOTALL).group(1)

                self.assertIn(_APP_NAME, title)

    def test_no_template_still_spells_the_old_names(self):
        """The two spellings the logo replaces. Caught here rather than by
        reading every page, because they were scattered across meta tags,
        headings and a placeholder attribute."""
        stale = re.compile(r"Spotify\s+(?:Stats\s+)?Tracker")
        for path in sorted(_TEMPLATES.glob("*.html")):
            with self.subTest(template=path.name):
                self.assertIsNone(stale.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
