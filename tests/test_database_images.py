import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Other test modules replace Database.database with a MagicMock at import time and
# only restore it in their tearDownModule (which runs after their tests execute, not
# during collection). Since unittest discover imports every test file before running
# any tests, we might see that mock here first - force a real import regardless.
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase
from Database.database import Database
from Database.repository import IMAGE_KIND_ARTIST, IMAGE_KIND_TRACK, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED


def _bareDatabase():
    """A Database instance with only the state lazyFetchArtistImage needs, skipping
    the heavy __init__ (autoimporter/listener setup) that isn't relevant here.
    user/email are set (rather than left unset like other _bareDatabase helpers in
    this test suite) because the cookie-client fallback path materializes a per-user
    cookies file, which reads them."""
    from Database.repository import Repository
    db = Database.__new__(Database)
    db._imageIdsLock = threading.RLock()
    temp_dir = tempfile.mkdtemp()
    db.repo = Repository(Path(temp_dir) / "test.db")
    db.user = "testuser"
    db.email = "testuser@example.com"
    return db


def _pngBytes():
    """A tiny real image, since _downloadImageTask feeds CDN response bytes through
    PIL - garbage bytes would fail to decode and the download would be marked failed
    regardless of what the test is trying to exercise."""
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (2, 2), 0).save(buffer, format="PNG")
    return buffer.getvalue()


#< how much of the tail to drop for a body that still OPENS: PNG carries its
#  dimensions in the leading IHDR chunk, so the header survives and only the
#  pixel data is short. Measured against the 64x64 image below - enough to lose
#  IEND and part of the IDAT stream, not so much that open() itself refuses.
_TRUNCATION_BYTES = 40


def _pngBytes40BytesShort():
    """A PNG whose header parses and whose pixel data does not - the shape that
    defers its failure to load() (i.e. to img.save()) instead of to open()."""
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    #< 64x64 rather than the 2x2 above: a tiny image's whole IDAT is smaller
    #  than the truncation, which would take the header with it
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()[:-_TRUNCATION_BYTES]


def _imageResponse(imageBytes):
    """A stand-in requests response for the streaming image download: the task
    reads the body through iter_content (capped), not .content.

    __enter__ returns the response itself, exactly like requests.Response does -
    a plain MagicMock would hand the `with` block a DIFFERENT auto-created mock
    whose headers/iter_content are unconfigured (see
    TestImageDownloadReleasesTheConnection)."""
    response = MagicMock()
    response.content = imageBytes
    response.headers = {"Content-Length": str(len(imageBytes))}
    response.iter_content = lambda chunk_size=None: iter([imageBytes])
    response.__enter__.return_value = response
    return response

class TestImageWritesAreAtomic(DatabaseTestCase):
    """PIL wrote the JPEG straight to its final path, so a process that died
    mid-save left a truncated file behind. The claim row is still `pending` at
    that point, and startup's stale-pending sweep clears it - but
    lazyFetchArtistImage returns True on imagePath.exists() BEFORE it consults
    the status, so the truncated file is served forever and never re-fetched.

    Written to a temp file in the same directory and renamed instead: the same
    .partial + os.replace shape the database backup uses, so a failed write
    leaves no file at all rather than half of one."""

    def _responseWithAnImage(self):
        from PIL import Image
        from io import BytesIO
        buffer = BytesIO()
        Image.new("RGB", (2, 2), (255, 0, 0)).save(buffer, format="PNG")
        response = MagicMock()
        response.headers = {}
        response.iter_content.return_value = [buffer.getvalue()]
        response.__enter__.return_value = response   #< see _imageResponse
        return response

    def test_a_save_that_dies_partway_leaves_no_file_behind(self):
        """The real hazard: PIL gets some bytes down and then the process dies
        (full disk, power loss). Simulated by writing a truncated file and then
        raising, which is what that leaves on disk."""
        db = self._makeDb({}, [])

        def truncatedWrite(self_img, target, **kwargs):
            Path(target).write_bytes(b"\xff\xd8\xff\xe0truncated")
            raise OSError("no space left on device")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            with patch.object(Database, "_mediaGet", return_value=self._responseWithAnImage()), \
                 patch("PIL.Image.Image.save", truncatedWrite):
                db._downloadImageTask(path, "http://example.com/i.png", "img1", "artist")

            leftovers = [p.name for p in path.iterdir()]
            self.assertEqual(leftovers, [],
                             f"a half-written image was left where the app will serve it: {leftovers}")

    def test_a_cleanup_that_itself_fails_does_not_hide_why_the_save_failed(self):
        """The removal runs inside an `except`, where a raised exception
        REPLACES the one being handled - and on Windows the moment a file has
        just been written is exactly when a scanner may still hold it. The log
        line is the only record this download leaves, so it has to name the
        disk, not the janitor. (Same shape as Database/backup.py's
        _discardPartial, which is where this was found.)"""
        db = self._makeDb({}, [])

        def failingWrite(self_img, target, **kwargs):
            #< bytes([...]) rather than an escape: the same JPEG magic the
            #  sibling test writes, spelled so no quoting layer can mangle it
            Path(target).write_bytes(bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"truncated")
            raise OSError("no space left on device")

        realUnlink = Path.unlink

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)

            def unlinkThatLoses(target, missing_ok=False):
                # Only this test's own .partial: patch.object on Path.unlink is
                # process-wide, ".partial" is a suffix three subsystems use, and
                # the suite runs worker threads from other tests in the same
                # process. Everything else gets the real one.
                if target.suffix == ".partial" and target.parent == path:
                    raise PermissionError("[WinError 32] used by another process")
                return realUnlink(target, missing_ok=missing_ok)

            with patch.object(Database, "_mediaGet", return_value=self._responseWithAnImage()), \
                 patch("PIL.Image.Image.save", failingWrite), \
                 patch.object(Path, "unlink", unlinkThatLoses), \
                 self.assertLogs("Database.database", level="WARNING") as logs:
                db._downloadImageTask(path, "http://example.com/i.png", "img1", "artist")

        #< split by level, because the two lines say different things: the ERROR
        #  is this download's verdict and must name the disk, while the failed
        #  removal is a WARNING and is allowed to name WinError 32
        errors = [r.getMessage() for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual(1, len(errors))
        self.assertIn("no space left on device", errors[0])
        self.assertNotIn("WinError 32", errors[0])
        self.assertTrue(any("WinError 32" in r.getMessage()
                            for r in logs.records if r.levelname == "WARNING"),
                        "the removal that lost should still be on the record somewhere")

    def test_a_successful_save_leaves_only_the_final_file(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            with patch.object(Database, "_mediaGet", return_value=self._responseWithAnImage()):
                db._downloadImageTask(path, "http://example.com/i.png", "img1", "artist")

            self.assertEqual([p.name for p in path.iterdir()], ["img1.jpeg"])


class TestLazyFetchArtistImage(unittest.TestCase):
    def test_returns_true_without_network_call_if_file_already_exists(self):
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"
            imagePath.write_bytes(b"already-here")

            with patch.object(Database, "_mediaGet") as mock_get:
                result = db.lazyFetchArtistImage("artist123", imagePath)

            self.assertTrue(result)
            mock_get.assert_not_called()

    def test_returns_false_when_artist_id_missing(self):
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "0.jpeg"
            with patch.object(Database, "_mediaGet") as mock_get:
                result = db.lazyFetchArtistImage("", imagePath)

            self.assertFalse(result)
            mock_get.assert_not_called()

    def test_a_lookup_that_never_answered_is_not_remembered_as_no_image(self):
        """'failed' is permanent here - lazyFetchArtistImage returns False on
        it before ever reaching tryClaimImageDownload, which would otherwise
        allow the retry. So a network blip or an open limiter backoff window
        denied that artist their picture for good, fixable only by a migrator
        (which this project has now shipped twice for stuck 'failed' rows).

        An exception is not an answer: the row is released so a later render
        asks again. A clean lookup saying "no images" still marks failed - see
        the test below."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"
            with patch.object(db, "_fetchArtistImageUrl",
                              side_effect=Exception("429 Too Many Requests")):
                db.lazyFetchArtistImage("artist123", imagePath).result(timeout=5)

            self.assertIsNone(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST))

            #< and the retry actually happens rather than being refused
            with patch.object(db, "_fetchArtistImageUrl",
                              return_value="https://i.scdn.co/image/abc"), \
                 patch.object(Database, "_mediaGet", return_value=_imageResponse(_pngBytes())):
                db.lazyFetchArtistImage("artist123", imagePath).result(timeout=5)

            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_an_artist_who_really_has_no_picture_is_still_remembered(self):
        """The other half: a lookup that succeeded and said "no images" IS an
        answer, and re-asking on every page render would be the throttling
        problem the 'failed' check exists to prevent."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"
            with patch.object(db, "_fetchArtistImageUrl", return_value=None) as lookup:
                db.lazyFetchArtistImage("artist123", imagePath).result(timeout=5)

                self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST),
                                 IMAGE_STATUS_FAILED)
                self.assertFalse(db.lazyFetchArtistImage("artist123", imagePath))
                self.assertEqual(lookup.call_count, 1)

    def test_fetches_via_web_api_when_credentials_configured(self):
        """The actual fetch runs on the shared background executor, not
        inline - lazyFetchArtistImage() returns the submitted Future rather
        than the outcome directly, so the test waits on it explicitly."""
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            apiResponse = MagicMock()
            apiResponse.status_code = 200
            apiResponse.json.return_value = {"images": [{"url": "https://i.scdn.co/image/abc"}]}
            imageResponse = _imageResponse(_pngBytes())

            with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                       return_value="mock_token"), \
                 patch.object(Database, "_mediaGet", side_effect=[apiResponse, imageResponse]) as mock_get, \
                 patch("Database.Spotify.Spotify") as mock_spotipy_class:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            self.assertTrue(imagePath.exists())
            self.assertEqual(mock_get.call_count, 2)   #< GET /v1/artists/{id}, then the CDN image bytes
            mock_spotipy_class.assert_not_called()   #< official API succeeded, no fallback needed
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_falls_back_to_spotipy_free_when_no_credentials_configured(self):
        """Configuring a Spotify API client id/secret is optional - most installs
        won't have one (db.getUserSpotifyCredentials() is naturally None here,
        there's no users row for "testuser" in this fresh temp db), so the cookie client
        must still be able to find the image on its own."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            mock_sp = MagicMock()
            mock_sp.artist.return_value = {"images": [{"url": "https://i.scdn.co/image/xyz"}]}
            imageResponse = _imageResponse(_pngBytes())

            with patch("Database.Spotify.Spotify", return_value=mock_sp) as mock_sp_class, \
                 patch.object(Database, "_mediaGet", return_value=imageResponse) as mock_get:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            self.assertTrue(imagePath.exists())
            mock_sp.artist.assert_called_once_with("artist123")
            mock_get.assert_called_once()   #< just the CDN image bytes; no api.spotify.com call was made
            # No cookiesFile: artist() is a public lookup through spotapi's
            # pooled client, and a login here would atexit-pin one live curl
            # session per artist image (the leak _pooledPublicClient documents).
            mock_sp_class.assert_called_once_with()
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_partial_credentials_fall_back_instead_of_raising(self):
        """The gate used to check client_id + refresh_token but then read
        creds["client_secret"] unconditionally - a row with only two of the
        three stored (the listener's own gate at spotifyListener.py requires
        all three) raised KeyError, which the lazy-fetch wrapper swallowed
        into a plain False: no image, no fallback attempt, nothing logged
        pointing at the real cause."""
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "refresh_token": "test_refresh"})   #< no client_secret
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            mock_sp = MagicMock()
            mock_sp.artist.return_value = {"images": [{"url": "https://i.scdn.co/image/xyz"}]}
            imageResponse = _imageResponse(_pngBytes())

            with patch("Database.Spotify.Spotify", return_value=mock_sp), \
                 patch.object(Database, "_mediaGet", return_value=imageResponse):
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            mock_sp.artist.assert_called_once_with("artist123")   #< the cookie client covered it

    def test_falls_back_to_spotipy_free_when_web_api_request_fails(self):
        """Credentials configured but the official API call itself fails (expired
        grant, rate limit, ...) - must not give up, same fallback as no-credentials."""
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            apiResponse = MagicMock()
            apiResponse.status_code = 403
            imageResponse = _imageResponse(_pngBytes())

            mock_sp = MagicMock()
            mock_sp.artist.return_value = {"images": [{"url": "https://i.scdn.co/image/xyz"}]}

            with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                       return_value="mock_token"), \
                 patch.object(Database, "_mediaGet", side_effect=[apiResponse, imageResponse]), \
                 patch("Database.Spotify.Spotify", return_value=mock_sp) as mock_spotipy_class:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            mock_spotipy_class.assert_called_once()
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_does_not_fall_back_when_web_api_confirms_no_image(self):
        """A definitive 200 with an empty images list means Spotify itself has no
        picture for this artist - that's real signal, not a transient failure, so
        it must not spend an extra request (and materialize a cookies file) asking
        the cookie client the same question again."""
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            apiResponse = MagicMock()
            apiResponse.status_code = 200
            apiResponse.json.return_value = {"images": []}

            with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                       return_value="mock_token"), \
                 patch.object(Database, "_mediaGet", return_value=apiResponse) as mock_get, \
                 patch("Database.Spotify.Spotify") as mock_spotipy_class:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertFalse(result)
            mock_get.assert_called_once()
            mock_spotipy_class.assert_not_called()
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_FAILED)

    def test_does_not_retry_after_a_failed_attempt_for_same_artist(self):
        """Negative caching: once we've tried (and failed to find any image) for an
        artist id, subsequent lookups for that id must not re-hit Spotify."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "missingArtist.jpeg"

            mock_sp = MagicMock()
            mock_sp.artist.return_value = {"images": []}

            with patch("Database.Spotify.Spotify", return_value=mock_sp) as mock_spotipy_class:
                firstFuture = db.lazyFetchArtistImage("missingArtist", imagePath)
                firstResult = firstFuture.result(timeout=5)
                secondResult = db.lazyFetchArtistImage("missingArtist", imagePath)

            self.assertFalse(firstResult)
            self.assertFalse(secondResult)   #< dedup path returns a plain bool, no new Future/fetch
            mock_spotipy_class.assert_called_once()

    def test_network_exception_is_swallowed_and_returns_false(self):
        """DELIBERATE CHANGE: this used to assert IMAGE_STATUS_FAILED here.

        The swallowing and the False are the point of the test and still hold.
        The status is not: 'failed' is permanent (lazyFetchArtistImage refuses
        it before the reclaim tryClaimImageDownload would allow), and the case
        this test names - a NETWORK exception - is precisely the one that
        establishes nothing about whether the artist has a picture. Leaving no
        row is the never-attempted state, so a later render asks again. Do not
        restore the old assertion; see the two tests at the top of this class
        for the transient/definitive split it belongs to."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist999.jpeg"
            with patch("Database.Spotify.Spotify", side_effect=Exception("boom")):
                future = db.lazyFetchArtistImage("artist999", imagePath)
                result = future.result(timeout=5)

            self.assertFalse(result)
            self.assertIsNone(db.repo.imageStatus("artist999", IMAGE_KIND_ARTIST))

    def test_dispatch_does_not_block_the_calling_thread(self):
        """The whole point of routing this through the shared executor: an
        HTTP request thread calling this must get control back immediately
        instead of blocking on the cookie-client lookup.

        Proven with an event gate rather than a wall-clock threshold (a
        previous `elapsed < 0.1s` assertion flaked on loaded CI runners
        where thread spin-up alone costs hundreds of ms): the mocked
        fetch can't finish until the test opens the gate, so
        lazyFetchArtistImage returning at all - with the fetch still
        pending - means the calling thread never ran it inline."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artistSlow.jpeg"

            gate = threading.Event()

            def gatedArtist(*args, **kwargs):
                #< the timeout turns an inline-fetch regression into a test
                #  failure (the gate only opens after dispatch returns, so
                #  running this on the calling thread would otherwise hang)
                gate.wait(timeout=5)
                return {"images": []}

            mock_sp = MagicMock()
            mock_sp.artist.side_effect = gatedArtist

            with patch("Database.Spotify.Spotify", return_value=mock_sp):
                future = db.lazyFetchArtistImage("artistSlow", imagePath)

                self.assertFalse(future.done())   #< fetch is parked on the gate, dispatch already returned
                gate.set()
                #< no image found -> False; also ensures the background task
                #  finishes before tmpdir cleanup
                self.assertFalse(future.result(timeout=5))


class TestDeleteFailedArtistImages(unittest.TestCase):
    """Repository.deleteFailedArtistImages() is the one-time remediation
    migrate1_20_0 runs to un-stick artists caught by the old og:image-scrape
    bug - see that migrator's docstring."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        from Database.repository import Repository
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

    def test_clears_failed_artist_images_only(self):
        self.repo.markImageStatus("artBroken1", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)
        self.repo.markImageStatus("artBroken2", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)
        self.repo.markImageStatus("artOk", IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)
        self.repo.markImageStatus("trackBroken", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)

        cleared = self.repo.deleteFailedArtistImages()

        self.assertEqual(cleared, 2)
        self.assertIsNone(self.repo.imageStatus("artBroken1", IMAGE_KIND_ARTIST))
        self.assertIsNone(self.repo.imageStatus("artBroken2", IMAGE_KIND_ARTIST))
        self.assertEqual(self.repo.imageStatus("artOk", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)
        #< a failed track image is a real per-URL 404, not a broken fetch method - untouched
        self.assertEqual(self.repo.imageStatus("trackBroken", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def test_cleared_artist_is_reclaimable(self):
        self.repo.markImageStatus("artBroken", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)

        self.repo.deleteFailedArtistImages()

        self.assertTrue(self.repo.tryClaimImageDownload("artBroken", IMAGE_KIND_ARTIST))

    def test_no_failed_artist_images_is_a_noop(self):
        self.repo.markImageStatus("artOk", IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)
        self.assertEqual(self.repo.deleteFailedArtistImages(), 0)


class TestDeleteFailedTrackImages(unittest.TestCase):
    """Repository.deleteFailedTrackImages() is the one-time remediation
    migrate1_40_0 runs to un-stick covers that failed against the malformed
    https://i.scdn.co/image///i.scdn.co/image/<hash> URL - see
    _imageUrlFromConnectMeta."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        from Database.repository import Repository
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

    def test_clears_failed_track_images_only(self):
        self.repo.markImageStatus("trkBroken1", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)
        self.repo.markImageStatus("trkBroken2", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)
        self.repo.markImageStatus("trkOk", IMAGE_KIND_TRACK, IMAGE_STATUS_OK)
        self.repo.markImageStatus("artBroken", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)

        cleared = self.repo.deleteFailedTrackImages()

        self.assertEqual(cleared, 2)
        self.assertIsNone(self.repo.imageStatus("trkBroken1", IMAGE_KIND_TRACK))
        self.assertIsNone(self.repo.imageStatus("trkBroken2", IMAGE_KIND_TRACK))
        self.assertEqual(self.repo.imageStatus("trkOk", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)
        #< artist images go through a different fetch path, untouched by this bug
        self.assertEqual(self.repo.imageStatus("artBroken", IMAGE_KIND_ARTIST), IMAGE_STATUS_FAILED)

    def test_cleared_track_is_reclaimable(self):
        """The point of deleting rather than re-marking: _saveImg's claim gate
        is what blocks the retry, and only an absent row reads as
        never-attempted."""
        self.repo.markImageStatus("trkBroken", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)

        self.repo.deleteFailedTrackImages()

        self.assertTrue(self.repo.tryClaimImageDownload("trkBroken", IMAGE_KIND_TRACK))

    def test_no_failed_track_images_is_a_noop(self):
        self.repo.markImageStatus("trkOk", IMAGE_KIND_TRACK, IMAGE_STATUS_OK)
        self.assertEqual(self.repo.deleteFailedTrackImages(), 0)


class TestDownloadImageTaskExtension(DatabaseTestCase):
    """The templates hardcode `<imgId>.jpeg`, so downloaded covers must always be
    saved as .jpeg regardless of the format the CDN returns - a PNG saved as
    `<imgId>.png` would 404 forever."""

    def _makeResponse(self, imageBytes):
        response = MagicMock()
        response = _imageResponse(imageBytes)
        return response

    def _pngBytes(self, mode="RGBA"):
        from io import BytesIO
        from PIL import Image
        buffer = BytesIO()
        Image.new(mode, (2, 2), (255, 0, 0, 128) if mode == "RGBA" else 0).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_png_response_is_saved_as_jpeg(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)

            with patch.object(Database, "_mediaGet", return_value=self._makeResponse(self._pngBytes())):
                db._downloadImageTask(imgDir, "https://img.example/x", "img1", IMAGE_KIND_TRACK)

            self.assertTrue((imgDir / "img1.jpeg").exists())
            self.assertFalse((imgDir / "img1.png").exists())

            from PIL import Image
            with Image.open(imgDir / "img1.jpeg") as saved:
                self.assertEqual(saved.format, "JPEG")

    def test_download_marks_image_ok_in_the_shared_catalog(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)

            with patch.object(Database, "_mediaGet", return_value=self._makeResponse(self._pngBytes())):
                db._downloadImageTask(imgDir, "https://img.example/x", "img1", IMAGE_KIND_TRACK)

            self.assertEqual(db.repo.imageStatus("img1", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)


def _imageBytesAs(imageFormat):
    """A tiny real image encoded as `imageFormat` - every one used here is a
    format this Pillow build can both write and read, so a refusal can only
    come from the decode allowlist, never from a codec that isn't there."""
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(buffer, format=imageFormat)
    return buffer.getvalue()


class TestDownloadedImagesDecodeOnlyWebFormats(DatabaseTestCase):
    """Pillow's decoders are where its advisories live (FlashPix, TIFF, PDF,
    multi-frame loaders...), and Image.open probes EVERY registered format by
    default. Cover and artist art is only ever served as a web format, so the
    download decodes only those - anything else is refused before a decoder
    for it ever runs."""

    def _download(self, imageBytes, imgId):
        db = self._makeDb({}, [])
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        imgDir = Path(tmpdir)
        with patch.object(Database, "_mediaGet", return_value=_imageResponse(imageBytes)):
            db._downloadImageTask(imgDir, "https://img.example/x", imgId, IMAGE_KIND_TRACK)
        return db, imgDir

    def test_each_web_format_is_still_stored(self):
        for imageFormat in ("JPEG", "PNG", "WEBP"):
            with self.subTest(imageFormat=imageFormat):
                db, imgDir = self._download(_imageBytesAs(imageFormat), "img1")
                self.assertTrue((imgDir / "img1.jpeg").exists())
                self.assertEqual(db.repo.imageStatus("img1", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)

    def test_a_decodable_non_web_format_is_refused_and_failed(self):
        """TIFF and BMP decode fine in Pillow - only the allowlist stops them."""
        for imageFormat in ("TIFF", "BMP"):
            with self.subTest(imageFormat=imageFormat):
                db, imgDir = self._download(_imageBytesAs(imageFormat), "img2")
                self.assertEqual(list(imgDir.iterdir()), [])
                self.assertEqual(db.repo.imageStatus("img2", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)


class TestSaveImgEmptyUrlGuard(DatabaseTestCase):
    """_saveImg() must silently skip when url is empty/None (MissingSchema fix)."""

    def _makeDbWithFakeExecutor(self, tmpdir):
        db = self._makeDb({}, [])
        db.imgDir_tracks = Path(tmpdir) / "tracks"
        db._imageDownloadExecutor = MagicMock()
        return db

    def test_empty_url_does_not_call_executor(self):
        """An empty imageUrl must never reach the thread pool / requests.get."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "", "some-img-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_not_called()

    def test_none_url_does_not_call_executor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, None, "some-img-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_not_called()

    def test_empty_url_does_not_poison_the_claim(self):
        """imgId must NOT be claimed for an empty URL - a retry should be possible
        if the URL is later populated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "", "poison-id", kind=IMAGE_KIND_TRACK)
            self.assertIsNone(db.repo.imageStatus("poison-id", IMAGE_KIND_TRACK))

    def test_valid_url_still_reaches_executor(self):
        """Sanity check: a proper URL must still be submitted to the executor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "https://example.com/cover.jpg", "valid-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_called_once()

    def test_already_claimed_image_does_not_reach_executor(self):
        """The second saveImg for the same id (e.g. two users' plays of the same
        song) must not re-download - the claim is shared, not per user."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "https://example.com/cover.jpg", "shared-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_called_once()

            db._saveImg(db.imgDir_tracks, "https://example.com/cover.jpg", "shared-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_called_once()  #< still just the one call


class TestDownloadImageTaskErrorLog(DatabaseTestCase):
    """_downloadImageTask() must include imgId in error log lines and mark the
    image as failed in the shared catalog (not left permanently 'pending')."""

    def _pngBytes(self):
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (1, 1)).save(buf, format="PNG")
        return buf.getvalue()

    def test_request_error_log_includes_imgid(self):
        import requests as req
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)
            with patch.object(Database, "_mediaGet",
                       side_effect=req.exceptions.ConnectionError("timeout")), \
                 self.assertLogs("Database.database", level="ERROR") as logs:
                db._downloadImageTask(imgDir, "https://img.example/x", "track-abc", IMAGE_KIND_TRACK)

        self.assertIn("track-abc", " ".join(logs.output))
        #< DELIBERATE: this asserted IMAGE_STATUS_FAILED. The log line is this
        #  test's subject and still holds; the status is not, because a
        #  connection error never reached the image. This class's stated worry
        #  is "not left permanently pending", and no row at all satisfies that
        #  strictly better than 'failed' does - it is the claimable state.
        self.assertIsNone(db.repo.imageStatus("track-abc", IMAGE_KIND_TRACK))


class TestATransientDownloadFailureIsRetryable(DatabaseTestCase):
    """Where a 'failed' row is PERMANENT, a blip must not write one.

    It is permanent in exactly one place, and the asymmetry is deliberate.
    tryClaimImageDownload re-claims a 'failed' row, so a track cover retries
    by itself the next time the listener saves that track - the driver is a
    play, which is rare. lazyFetchArtistImage instead returns False on
    'failed' BEFORE it reaches that claim, because its driver is a page render
    and re-asking on every render is the load the check exists to prevent.

    So an artist whose URL lookup succeeded and whose DOWNLOAD then hit a CDN
    blip lost their picture for good. The lookup half of this was fixed with
    releaseImageClaim; this is the download half, and it is the same rule: a
    network-layer failure never saw the image, a response we read and could
    not use did."""

    def _httpError(self, status):
        import requests as req
        response = MagicMock()
        response.status_code = status
        return req.exceptions.HTTPError(f"{status} error", response=response)

    def test_a_failed_track_cover_is_reclaimable_but_a_failed_artist_is_not(self):
        """The asymmetry this whole class turns on, pinned because a docstring
        in database.py asserted the opposite for years ("_saveImg's claim gate
        then refuses to retry it") and that sentence is what makes a reader
        conclude track covers are lost forever. They are not: the claim gate
        re-claims a 'failed' row, and getNowPlaying re-drives saveTrackImg on
        every poll. Only lazyFetchArtistImage refuses, before it ever reaches
        the gate."""
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            db.repo.markImageStatus("t9", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)
            db.repo.markImageStatus("a9", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)

            self.assertTrue(db.repo.tryClaimImageDownload("t9", IMAGE_KIND_TRACK))
            self.assertFalse(db.lazyFetchArtistImage("a9", Path(tmpdir) / "a9.jpeg"))

    def _artistAfter(self, db, tmpdir, failure):
        """Claim an artist image the way _saveImg does, then fail the download."""
        db.repo.tryClaimImageDownload("art-1", IMAGE_KIND_ARTIST)
        with patch.object(Database, "_mediaGet", **failure):
            db._downloadImageTask(Path(tmpdir), "https://img/x", "art-1", IMAGE_KIND_ARTIST)

    def test_a_connection_error_leaves_the_artist_retryable(self):
        """Asserted on the stored state rather than by calling
        lazyFetchArtistImage: its retry dispatches onto the shared executor,
        and a job outliving this temp directory fails the cleanup, not the
        code. 'failed' is the ONLY status that path refuses, so its absence is
        exactly what "retryable" means here."""
        import requests as req
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            self._artistAfter(
                db, tmpdir, {"side_effect": req.exceptions.ConnectionError("reset by peer")})

            self.assertIsNone(db.repo.imageStatus("art-1", IMAGE_KIND_ARTIST))
            self.assertTrue(db.repo.tryClaimImageDownload("art-1", IMAGE_KIND_ARTIST))

    def test_a_server_error_is_transient_too(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet", side_effect=self._httpError(503)):
                db._downloadImageTask(Path(tmpdir), "https://img/x", "t1", IMAGE_KIND_TRACK)

            self.assertIsNone(db.repo.imageStatus("t1", IMAGE_KIND_TRACK))

    def test_the_4xx_that_mean_ask_again_later_are_transient(self):
        """408, 425 and 429 are 4xx by number and 5xx by meaning.

        The rule underneath is "a 4xx is the server telling us this URL will
        not work", which is true of 404 and 403 and precisely untrue of these
        three - each one says come back. 429 is the one that happens: it is
        what a CDN returns under the rate limiting any account's burst can
        open, and the artist half records 'failed' permanently, so a single
        busy moment would cost an artist their picture until someone shipped a
        migrator to clear it. That is the harm releaseImageClaim was added to
        stop; it just could not see these through the status test."""
        import requests as req
        for status in (408, 425, 429):
            with self.subTest(status=status):
                db = self._makeDb({}, [])
                with tempfile.TemporaryDirectory() as tmpdir:
                    self._artistAfter(db, tmpdir, {"side_effect": self._httpError(status)})

                    self.assertIsNone(db.repo.imageStatus("art-1", IMAGE_KIND_ARTIST))
                    #< the whole point: the next render may ask again
                    self.assertTrue(db.repo.tryClaimImageDownload("art-1", IMAGE_KIND_ARTIST))

    def test_a_dead_url_is_still_remembered_as_failed(self):
        """The definitive half: a 404 IS the answer, so the artist path goes on
        refusing it and the render stops asking."""
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            self._artistAfter(db, tmpdir, {"side_effect": self._httpError(404)})

            self.assertEqual(db.repo.imageStatus("art-1", IMAGE_KIND_ARTIST), IMAGE_STATUS_FAILED)
            #< refused synchronously, before any dispatch - the load the
            #  'failed' short-circuit exists to prevent
            self.assertFalse(db.lazyFetchArtistImage("art-1", Path(tmpdir) / "art-1.jpeg"))

    def test_bytes_that_are_not_an_image_are_still_failed(self):
        """We got the response and it was not decodable - nothing transient
        about that, and the same URL will not improve."""
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet",
                       return_value=_imageResponse(b"not-an-image")):
                db._downloadImageTask(Path(tmpdir), "https://img/x", "t3", IMAGE_KIND_TRACK)

            self.assertEqual(db.repo.imageStatus("t3", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def test_an_oversize_image_is_still_failed(self):
        """_readCappedBody raises ValueError, not a RequestException - the cap
        is a verdict about this image, not about the network."""
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            oversize = _imageResponse(b"x" * 10)
            oversize.headers = {"Content-Length": str(10 * 1024 * 1024 * 10)}
            with patch.object(Database, "_mediaGet", return_value=oversize):
                db._downloadImageTask(Path(tmpdir), "https://img/x", "t4", IMAGE_KIND_TRACK)

            self.assertEqual(db.repo.imageStatus("t4", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def _artistAfterAFailingStore(self, db, tmpdir, target, boom):
        """Download a perfectly good image, then fail at `target` while storing it.

        The patches are process-wide and the suite runs other tests' worker
        threads in this same process, so each one fires only for THIS test's
        temp directory and delegates everything else to the real function
        (same shape as test_a_cleanup_that_itself_fails's unlinkThatLoses)."""
        db.repo.tryClaimImageDownload("art-1", IMAGE_KIND_ARTIST)
        with patch.object(Database, "_mediaGet",
                   return_value=_imageResponse(_pngBytes())), \
             patch(target, boom):
            db._downloadImageTask(Path(tmpdir), "https://img/x", "art-1", IMAGE_KIND_ARTIST)

    def test_a_disk_that_refuses_the_write_leaves_the_artist_retryable(self):
        """The third category the two in this class's docstring miss: the
        bytes arrived AND decoded, and the local disk refused them.

        That is not a verdict about the image - the same URL will serve the
        same good bytes a minute later - but it used to record one, because
        the classifier only asks "was this a RequestException". For an artist
        that verdict is permanent (lazyFetchArtistImage refuses 'failed'
        before it ever reaches the reclaim), so one full disk or one
        antivirus-held handle cost that artist their picture until someone
        shipped a migrator - the exact harm releaseImageClaim exists to
        prevent, arriving through the one door it did not cover."""
        from PIL import Image as PILImage
        realSave = PILImage.Image.save
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            def saveThatRunsOutOfDisk(self_img, target, **kwargs):
                if Path(target).parent == Path(tmpdir):
                    raise OSError("no space left on device")
                return realSave(self_img, target, **kwargs)

            self._artistAfterAFailingStore(db, tmpdir, "PIL.Image.Image.save",
                                           saveThatRunsOutOfDisk)

            self.assertIsNone(db.repo.imageStatus("art-1", IMAGE_KIND_ARTIST))
            #< the whole point: the next render may ask again
            self.assertTrue(db.repo.tryClaimImageDownload("art-1", IMAGE_KIND_ARTIST))

    def test_a_rename_a_scanner_blocks_leaves_the_artist_retryable(self):
        """The same rule at the other storage step, and the one this host
        actually meets: media_fetch's own cleanup comment names it - on
        Windows the moment a file has just been written is exactly when a
        scanner may still hold it, and os.replace is what fails then."""
        realReplace = os.replace
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            def replaceTheScannerBlocks(src, dst, **kwargs):
                if Path(dst).parent == Path(tmpdir):
                    raise PermissionError("[WinError 32] used by another process")
                return realReplace(src, dst, **kwargs)

            self._artistAfterAFailingStore(db, tmpdir, "Database.database.os.replace",
                                           replaceTheScannerBlocks)

            self.assertIsNone(db.repo.imageStatus("art-1", IMAGE_KIND_ARTIST))
            self.assertTrue(db.repo.tryClaimImageDownload("art-1", IMAGE_KIND_ARTIST))

    def test_a_body_that_decodes_only_partway_is_still_failed(self):
        """The guard on the two above, and the reason the decode is forced
        before the store rather than left where it fell.

        Image.open only reads the header, so a truncated body opens fine and
        raises inside img.save() - i.e. in the middle of the storage step. Any
        "a failure while storing is transient" rule that trusts WHERE the
        exception surfaced would hand this corrupt image a retry forever."""
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            truncated = _pngBytes40BytesShort()
            with patch.object(Database, "_mediaGet",
                       return_value=_imageResponse(truncated)):
                db._downloadImageTask(Path(tmpdir), "https://img/x", "t5", IMAGE_KIND_TRACK)

            self.assertEqual(db.repo.imageStatus("t5", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def test_save_error_log_includes_imgid(self):
        """If saving the image raises (e.g. corrupt bytes), the imgId appears in the log."""
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)
            bad_response = _imageResponse(b"not-an-image")
            with patch.object(Database, "_mediaGet", return_value=bad_response), \
                 self.assertLogs("Database.database", level="ERROR") as logs:
                db._downloadImageTask(imgDir, "https://img.example/x", "track-xyz", IMAGE_KIND_TRACK)

        self.assertIn("track-xyz", " ".join(logs.output))
        self.assertEqual(db.repo.imageStatus("track-xyz", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)


class TestImageDownloadSizeCap(DatabaseTestCase):
    """The body used to be materialized in full before PIL ever inspected it, so
    a misbehaving or redirected endpoint could balloon a worker thread's
    memory. URLs only ever come from Spotify's own metadata, so this is a
    ceiling, not a filter."""

    def _oversizedResponse(self, declaredLength=None):
        from Database.media_fetch import MAX_IMAGE_BYTES

        chunk = b"x" * (1024 * 1024)
        chunkCount = (MAX_IMAGE_BYTES // len(chunk)) + 2
        response = MagicMock()
        response.headers = {} if declaredLength is None else {"Content-Length": str(declaredLength)}
        response.iter_content = lambda chunk_size=None: (chunk for _ in range(chunkCount))
        response.__enter__.return_value = response   #< see _imageResponse
        return response

    def test_an_oversized_body_is_refused_and_marked_failed(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet", return_value=self._oversizedResponse()),                  self.assertLogs("Database.database", level="ERROR"):
                db._downloadImageTask(Path(tmpdir), "https://img.example/big", "track-big", IMAGE_KIND_TRACK)

        self.assertEqual(db.repo.imageStatus("track-big", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def test_an_oversized_content_length_short_circuits_before_reading(self):
        from Database.media_fetch import MAX_IMAGE_BYTES

        db = self._makeDb({}, [])
        response = self._oversizedResponse(declaredLength=MAX_IMAGE_BYTES + 1)
        read = []
        response.iter_content = lambda chunk_size=None: read.append(1) or iter([])

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet", return_value=response),                  self.assertLogs("Database.database", level="ERROR"):
                db._downloadImageTask(Path(tmpdir), "https://img.example/big", "track-big2", IMAGE_KIND_TRACK)

        self.assertEqual(read, [])   #< never started reading the body

    def test_a_normal_image_is_unaffected(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)
            with patch.object(Database, "_mediaGet", return_value=_imageResponse(_pngBytes())):
                db._downloadImageTask(imgDir, "https://img.example/ok", "track-ok", IMAGE_KIND_TRACK)

            self.assertTrue((imgDir / "track-ok.jpeg").exists())
        self.assertEqual(db.repo.imageStatus("track-ok", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)

    def test_a_non_decimal_content_length_is_ignored_rather_than_fatal(self):
        """The early exit guards its int() with isdigit(), which admits
        characters like the superscript two that int() then refuses (the same
        08e8e98 shape as the ?page= params). That ValueError is not the cap's
        own, it escapes _readCappedBody before imageIsGood is set, and it is
        not a RequestException - so the image was marked FAILED for good, the
        permanent status the artist path refuses forever. A Content-Length the
        cap cannot read is a Content-Length to ignore; the body cap still
        holds. 2026-09-05 range review."""
        db = self._makeDb({}, [])
        response = _imageResponse(_pngBytes())
        response.headers = {"Content-Length": "\u00b2"}
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)
            with patch.object(Database, "_mediaGet", return_value=response):
                db._downloadImageTask(imgDir, "https://img.example/odd", "track-odd", IMAGE_KIND_TRACK)

            self.assertTrue((imgDir / "track-odd.jpeg").exists())
        self.assertEqual(db.repo.imageStatus("track-odd", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)


class TestMediaFetchRequestTimeouts(DatabaseTestCase):
    """Both outbound requests in this module run on a background image thread
    with a fallback behind them (the cookie client for the artist lookup, a
    retry via the pending claim for the download), so neither may hang that
    thread on a stalled endpoint. One constant because they are the same knob:
    how long background image work may block on the network."""

    def test_the_cdn_download_uses_the_named_timeout(self):
        from Database.media_fetch import MEDIA_FETCH_HTTP_TIMEOUT_SECONDS

        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet",
                       return_value=_imageResponse(_pngBytes())) as mock_get:
                db._downloadImageTask(Path(tmpdir), "https://img.example/ok", "track-t", IMAGE_KIND_TRACK)

        self.assertEqual(mock_get.call_args.kwargs["timeout"], MEDIA_FETCH_HTTP_TIMEOUT_SECONDS)

    def test_the_web_api_artist_lookup_uses_the_named_timeout(self):
        from Database.media_fetch import MEDIA_FETCH_HTTP_TIMEOUT_SECONDS

        db = self._makeDb({}, [])
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"images": [{"url": "https://cdn.example/a.jpg"}]}
        creds = {"client_id": "cid", "client_secret": "sec", "refresh_token": "rt"}

        with patch.object(db, "getUserSpotifyCredentials", return_value=creds), \
             patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="access-token"), \
             patch.object(Database, "_mediaGet", return_value=response) as mock_get:
            url = db._fetchArtistImageUrl("artist-1")

        self.assertEqual(url, "https://cdn.example/a.jpg")   #< the Web API path really was taken
        self.assertEqual(mock_get.call_args.kwargs["timeout"], MEDIA_FETCH_HTTP_TIMEOUT_SECONDS)


class TestImageDownloadReleasesTheConnection(DatabaseTestCase):
    """The download is streamed, and _readCappedBody deliberately STOPS draining
    an oversized body - a response left open never returns its connection to
    the shared media session's pool (Database._mediaHttpSession), so the next
    image download pays a fresh TCP+TLS handshake. Holding it in a `with`
    releases it on every exit path, not only the one where the body happened
    to be read to the end."""

    def _refusedResponse(self):
        """A response whose body blows the cap, so _readCappedBody raises
        partway through iterating it."""
        from Database.media_fetch import MAX_IMAGE_BYTES

        chunk = b"x" * (1024 * 1024)
        response = MagicMock()
        response.headers = {}
        response.iter_content = lambda chunk_size=None: (
            chunk for _ in range((MAX_IMAGE_BYTES // len(chunk)) + 2))
        response.__enter__.return_value = response
        return response

    def test_a_refused_oversized_body_still_releases_the_response(self):
        db = self._makeDb({}, [])
        response = self._refusedResponse()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet", return_value=response), \
                 self.assertLogs("Database.database", level="ERROR"):
                db._downloadImageTask(Path(tmpdir), "https://img.example/big", "track-big3", IMAGE_KIND_TRACK)

        self.assertTrue(response.__exit__.called,
                        "the streamed response must be released even when its body is refused")

    def test_undecodable_bytes_still_release_the_response(self):
        """PIL rejecting what was read is the other early-exit path."""
        db = self._makeDb({}, [])
        response = _imageResponse(b"not an image")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet", return_value=response), \
                 self.assertLogs("Database.database", level="ERROR"):
                db._downloadImageTask(Path(tmpdir), "https://img.example/junk", "track-junk", IMAGE_KIND_TRACK)

        self.assertTrue(response.__exit__.called)
        self.assertEqual(db.repo.imageStatus("track-junk", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def test_a_successful_download_releases_the_response(self):
        db = self._makeDb({}, [])
        response = _imageResponse(_pngBytes())
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Database, "_mediaGet", return_value=response):
                db._downloadImageTask(Path(tmpdir), "https://img.example/ok", "track-ok2", IMAGE_KIND_TRACK)

        self.assertTrue(response.__exit__.called)
        self.assertEqual(db.repo.imageStatus("track-ok2", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)


class TestWebApiTokenCache(unittest.TestCase):
    """Every artist-image lookup used to POST accounts.spotify.com for a fresh
    access token and discard it after one request. The image-status claims
    already dedup lookups to once per artist ever, but a big import queues
    hundreds of missing artists - hundreds of identical token POSTs for a
    token that lives an hour."""

    def _dbWithCreds(self, refreshToken="rtoken"):
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret",
            "refresh_token": refreshToken})
        return db

    def _artistResponse(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"images": [{"url": "https://i.scdn.co/image/abc"}]}
        return resp

    def test_a_second_lookup_reuses_the_token(self):
        db = self._dbWithCreds()
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="tok1") as refresh, \
                patch.object(Database, "_mediaGet", return_value=self._artistResponse()):
            db._fetchArtistImageUrl("a1")
            db._fetchArtistImageUrl("a2")

        self.assertEqual(refresh.call_count, 1)

    def test_an_expired_token_is_refreshed(self):
        import time
        db = self._dbWithCreds()
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   side_effect=["tok1", "tok2"]) as refresh, \
                patch.object(Database, "_mediaGet", return_value=self._artistResponse()):
            db._fetchArtistImageUrl("a1")
            #< age the cache past its TTL rather than patching the clock -
            #  time.monotonic is process-wide and other threads read it
            token, mintedFrom, _expiresAt = db._webApiTokenCache
            db._webApiTokenCache = (token, mintedFrom, time.monotonic() - 1)
            db._fetchArtistImageUrl("a2")

        self.assertEqual(refresh.call_count, 2)

    def test_rotated_credentials_miss_the_cache(self):
        db = self._dbWithCreds()
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   side_effect=["tok1", "tok2"]) as refresh, \
                patch.object(Database, "_mediaGet", return_value=self._artistResponse()):
            db._fetchArtistImageUrl("a1")
            db.getUserSpotifyCredentials = MagicMock(return_value={
                "client_id": "cid", "client_secret": "csecret",
                "refresh_token": "rotated"})
            db._fetchArtistImageUrl("a2")

        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(refresh.call_args[0][2], "rotated")

    def test_a_failed_refresh_is_not_cached(self):
        db = self._dbWithCreds()
        mock_sp = MagicMock()
        mock_sp.artist.return_value = {"images": []}
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value=None) as refresh, \
                patch("Database.Spotify.Spotify", return_value=mock_sp):
            db._fetchArtistImageUrl("a1")
            db._fetchArtistImageUrl("a2")

        #< None must not be remembered as a token - the next lookup retries
        self.assertEqual(refresh.call_count, 2)

    def _cookieFallbackClient(self):
        mock_sp = MagicMock()
        mock_sp.artist.return_value = {"images": [{"url": "https://cookie.example/fallback"}]}
        return mock_sp

    def _unauthorizedResponse(self):
        resp = MagicMock()
        resp.status_code = 401
        return resp

    def test_a_401_evicts_the_cache_and_a_retry_succeeds(self):
        """A token dying mid-TTL (revoked, or invalidated server-side) used to
        pin every lookup for the rest of the 50-minute cache window to one
        doomed request each - a 401 now evicts the cache and retries once on a
        fresh mint."""
        db = self._dbWithCreds()
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   side_effect=["tok1", "tok2"]) as refresh, \
                patch.object(Database, "_mediaGet",
                      side_effect=[self._unauthorizedResponse(), self._artistResponse()]) as get, \
                patch("Database.Spotify.Spotify", return_value=self._cookieFallbackClient()):
            url = db._fetchArtistImageUrl("a1")

        self.assertEqual(url, "https://i.scdn.co/image/abc",
                         "the retried Web API answer, not the cookie fallback")
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer tok2",
                         "the retry must carry the fresh mint, not the evicted token")
        token, _mintedFrom, _expiresAt = db._webApiTokenCache
        self.assertEqual(token, "tok2")

    def test_a_persistent_401_falls_back_after_one_retry(self):
        db = self._dbWithCreds()
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   side_effect=["tok1", "tok2"]) as refresh, \
                patch.object(Database, "_mediaGet",
                      side_effect=[self._unauthorizedResponse(), self._unauthorizedResponse()]) as get, \
                patch("Database.Spotify.Spotify", return_value=self._cookieFallbackClient()), \
                self.assertLogs("Database.database", level="WARNING"):
            #< the assertLogs pins the other half of the fix: today's 401 is
            #  SILENT (only exceptions log), so 50 degraded minutes left no trace
            url = db._fetchArtistImageUrl("a1")

        self.assertEqual(url, "https://cookie.example/fallback")
        self.assertEqual(get.call_count, 2, "one retry, never a loop")
        self.assertEqual(refresh.call_count, 2)

    def test_a_401_with_a_dead_grant_falls_back_without_a_retry(self):
        """After the evict, a refused mint (revoked grant) leaves nothing to
        retry with - one refused POST, then the cookie client."""
        db = self._dbWithCreds()
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   side_effect=["tok1", None]), \
                patch.object(Database, "_mediaGet",
                      side_effect=[self._unauthorizedResponse()]) as get, \
                patch("Database.Spotify.Spotify", return_value=self._cookieFallbackClient()):
            url = db._fetchArtistImageUrl("a1")

        self.assertEqual(url, "https://cookie.example/fallback")
        self.assertEqual(get.call_count, 1)
        self.assertIsNone(db._webApiTokenCache,
                          "a failed refresh is never cached - the next lookup retries")

    def test_a_non_401_failure_does_not_evict_the_cache(self):
        """A 5xx (or 403/429) is not a verdict on the token - only a 401 may
        pay the eviction's extra mint."""
        db = self._dbWithCreds()
        serverError = MagicMock()
        serverError.status_code = 500
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="tok1") as refresh, \
                patch.object(Database, "_mediaGet", side_effect=[serverError]), \
                patch("Database.Spotify.Spotify", return_value=self._cookieFallbackClient()):
            url = db._fetchArtistImageUrl("a1")

        self.assertEqual(url, "https://cookie.example/fallback")
        self.assertEqual(refresh.call_count, 1)
        token, _mintedFrom, _expiresAt = db._webApiTokenCache
        self.assertEqual(token, "tok1", "a 5xx must not cost the cache its still-valid token")

    def test_the_token_mint_names_the_user_it_logs_for(self):
        """_refresh_spotify_access_token's failure lines carry logUser to say
        WHOSE worker failed; a mint without it logs anonymously in a
        multi-user instance."""
        db = self._dbWithCreds()
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="tok1") as refresh, \
                patch.object(Database, "_mediaGet", return_value=self._artistResponse()):
            db._fetchArtistImageUrl("a1")

        self.assertEqual(refresh.call_args.kwargs.get("logUser"), db.user)


if __name__ == "__main__":
    unittest.main()
