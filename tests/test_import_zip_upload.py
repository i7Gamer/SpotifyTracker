# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Spotify hands you a ZIP; /import-history now takes it as-is.

Two concerns, two classes: services.import_upload.expandUploads decides what
bytes come out of an upload (including the uncompressed-size guard that stops
a ZIP from expanding to more than a plain upload would have been allowed to
be), and the route turns its result into the redirect the user sees.

The guard is the reason this file exists at all: MAX_CONTENT_LENGTH bounds the
REQUEST, and a 25 MB archive of 10 GB of zeroes sails straight through it.
"""
import io
import threading
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from services.import_upload import expandUploads

# Matches test_import_history_route's deadline: a failure deadline, not a pace.
_IMPORT_THREAD_DEADLINE_SECONDS = 5

_PLAY_JSON = '{"ms_played": 1}'
# Spotify nests the history files one folder deep inside the archive.
_EXPORT_DIR = "Spotify Extended Streaming History/"
# Big enough that no test trips the guard by accident, small enough to be free.
_GENEROUS_CAP = 10 * 1024 * 1024


def _zipBytes(entries, compressionLevel=None):
    """An in-memory ZIP of {name: text-or-bytes}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=compressionLevel) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class _FakeUpload:
    """The two attributes expandUploads uses off a werkzeug FileStorage."""

    def __init__(self, filename, data):
        self.filename = filename
        self.stream = io.BytesIO(data)


def _upload(filename, data):
    return _FakeUpload(filename, data if isinstance(data, bytes) else data.encode("utf-8"))


class TestExpandUploads(unittest.TestCase):
    """The service: bytes in, decoded history text out."""

    def test_a_plain_json_upload_is_passed_through_unchanged(self):
        """The pre-ZIP behaviour is the one that must not move."""
        result = expandUploads([_upload("history.json", _PLAY_JSON)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 0)
        self.assertFalse(result.exceededCap)
        self.assertFalse(result.emptyArchive)

    def test_a_non_utf8_plain_upload_is_counted_not_raised(self):
        result = expandUploads([_upload("bad.json", b"\xff\xfe not utf-8 \xfa")], _GENEROUS_CAP)

        self.assertEqual(result.contents, [])
        self.assertEqual(result.unreadableCount, 1)

    def test_a_zip_yields_every_history_file_inside_it(self):
        archive = _zipBytes({
            _EXPORT_DIR + "Streaming_History_Audio_2024_1.json": '{"ms_played": 2}',
            _EXPORT_DIR + "Streaming_History_Audio_2023_0.json": '{"ms_played": 1}',
        })

        result = expandUploads([_upload("my_export.zip", archive)], _GENEROUS_CAP)

        # Sorted by entry name, like AutoImporter._handleImport's sorted(paths):
        # batch-scoped duplicate-claim tracking reads better in file order, and
        # archive order is not something the user can see or control.
        self.assertEqual(result.contents, ['{"ms_played": 1}', '{"ms_played": 2}'])
        self.assertEqual(result.unreadableCount, 0)

    def test_a_zip_is_detected_by_content_not_by_its_name(self):
        """A .zip renamed to .json is still a ZIP, and vice versa - the old
        path would have handed its binary straight to .decode()."""
        archive = _zipBytes({"Streaming_History_Audio.json": _PLAY_JSON})

        result = expandUploads([_upload("actually_a_zip.json", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])

    def test_non_history_entries_in_the_archive_are_skipped_silently(self):
        """Spotify's export ships a read-me PDF and friends. Skipping them is
        not a failure, so they must not inflate unreadableCount - that count
        drives the overwrite-range safety net."""
        archive = _zipBytes({
            _EXPORT_DIR + "Streaming_History_Audio.json": _PLAY_JSON,
            _EXPORT_DIR + "ReadMeFirst.pdf": b"%PDF-1.4 not history",
            _EXPORT_DIR + "Userdata.txt": "not history either",
        })

        result = expandUploads([_upload("export.zip", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 0)

    def test_csv_entries_are_importable_too(self):
        """The form accepts loose .csv, so an archived one is no different."""
        archive = _zipBytes({"most_played.csv": "track,plays\na,1\n"})

        result = expandUploads([_upload("export.zip", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, ["track,plays\na,1\n"])

    def test_macos_resource_forks_are_skipped(self):
        """A ZIP re-made on macOS carries __MACOSX/._Name.json shadows: they
        end in .json, they are binary, and counting them unreadable would tell
        the overwrite path that real history went missing."""
        archive = _zipBytes({
            "Streaming_History_Audio.json": _PLAY_JSON,
            "__MACOSX/._Streaming_History_Audio.json": b"\x00\x05\x16\x07\x00\x02",
        })

        result = expandUploads([_upload("export.zip", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 0)

    def test_anything_under_the_macos_metadata_folder_is_skipped(self):
        """That folder is metadata by definition, and the rule does not lean
        on the ._ convention that usually accompanies it - without a case of
        its own this branch would be masked by the basename check below."""
        archive = _zipBytes({
            "Streaming_History_Audio.json": _PLAY_JSON,
            "__MACOSX/Streaming_History_Audio.json": '{"ms_played": 999}',
        })

        result = expandUploads([_upload("export.zip", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])

    def test_a_resource_fork_beside_its_file_is_skipped_too(self):
        """Not every AppleDouble shadow sits under __MACOSX/ - a folder zipped
        on macOS carries them next to the files they shadow, where only the
        basename check can catch them. (Their bytes are all under 0x80, so
        they decode cleanly and would land in contents as garbage history.)"""
        archive = _zipBytes({
            "Streaming_History_Audio.json": _PLAY_JSON,
            "._Streaming_History_Audio.json": "\x00\x05\x16\x07",
        })

        result = expandUploads([_upload("export.zip", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 0)

    def test_directory_entries_are_skipped(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(zipfile.ZipInfo(_EXPORT_DIR), b"")
            archive.writestr(_EXPORT_DIR + "Streaming_History_Audio.json", _PLAY_JSON)

        result = expandUploads([_upload("export.zip", buffer.getvalue())], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 0)

    def test_an_unreadable_entry_does_not_drop_its_readable_siblings(self):
        archive = _zipBytes({
            "a_Streaming_History.json": _PLAY_JSON,
            "b_broken.json": b"\xff\xfe not utf-8 \xfa",
        })

        result = expandUploads([_upload("export.zip", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 1)

    def test_an_archive_with_no_history_files_is_reported_as_such(self):
        """Uploading the wrong Spotify ZIP (account data, not extended
        history) must not read as 'none of those files were valid UTF-8'."""
        archive = _zipBytes({"ReadMeFirst.pdf": b"%PDF-1.4"})

        result = expandUploads([_upload("export.zip", archive)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [])
        self.assertTrue(result.emptyArchive)
        self.assertEqual(result.unreadableCount, 0)

    def test_a_corrupt_archive_is_counted_unreadable_not_raised(self):
        """Truncated mid-entry: is_zipfile reads the central directory at the
        END of the file, so this is detected as a ZIP and then fails on read."""
        archive = bytearray(_zipBytes({"Streaming_History.json": _PLAY_JSON * 200}))
        archive[40:60] = b"\x00" * 20   #< corrupt the deflate stream, keep the directory

        result = expandUploads([_upload("export.zip", bytes(archive))], _GENEROUS_CAP)

        self.assertEqual(result.contents, [])
        self.assertEqual(result.unreadableCount, 1)

    def test_a_file_named_zip_that_is_not_one_falls_back_to_text(self):
        result = expandUploads([_upload("not_really.zip", _PLAY_JSON)], _GENEROUS_CAP)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 0)

    def test_a_mixed_upload_takes_both_the_archive_and_the_loose_file(self):
        archive = _zipBytes({"Streaming_History_Audio.json": '{"ms_played": 1}'})

        result = expandUploads([
            _upload("export.zip", archive),
            _upload("extra.json", '{"ms_played": 2}'),
        ], _GENEROUS_CAP)

        self.assertEqual(result.contents, ['{"ms_played": 1}', '{"ms_played": 2}'])
        self.assertFalse(result.emptyArchive)

    # --- the guard ------------------------------------------------------

    def test_expanding_past_the_cap_is_refused(self):
        payload = "x" * 5000
        archive = _zipBytes({"Streaming_History.json": payload})

        result = expandUploads([_upload("export.zip", archive)], 1000)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])

    def test_a_zip_bomb_is_refused_without_materialising_it(self):
        """The whole point. 8 MB of zeroes compresses to a few KB; a cap
        enforced on ZipInfo.file_size alone would be enforced on a number the
        archive itself supplies."""
        archive = _zipBytes({"Streaming_History.json": "\0" * (8 * 1024 * 1024)},
                            compressionLevel=9)
        self.assertLess(len(archive), 100 * 1024, "the fixture stopped being a bomb")

        result = expandUploads([_upload("bomb.zip", archive)], 64 * 1024)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])

    def test_a_bomb_is_never_inflated_past_the_cap(self):
        """Refusing it is only half the guard.

        A cap enforced AFTER inflating 10 GB has already spent the memory it
        exists to save, and no assertion on the returned result can tell the
        two apart - both just say "refused". So pin the read itself: nothing
        may ask the decompressing stream for more than the budget. This is the
        assertion that fails if `read(remaining + 1)` ever becomes `read()`,
        or starts trusting the archive's own declared size."""
        cap = 64 * 1024
        archive = _zipBytes({"Streaming_History.json": "\0" * (8 * 1024 * 1024)},
                            compressionLevel=9)
        requested = []
        realRead = zipfile.ZipExtFile.read

        def recordingRead(entry, size=-1):
            requested.append(size)
            return realRead(entry, size)

        with patch.object(zipfile.ZipExtFile, "read", recordingRead):
            result = expandUploads([_upload("bomb.zip", archive)], cap)

        self.assertTrue(result.exceededCap)
        self.assertTrue(requested, "the entry was never read through ZipExtFile.read")
        self.assertTrue(
            all(size is not None and 0 <= size <= cap + 1 for size in requested),
            f"asked the decompressor for {requested} bytes on a {cap}-byte budget")

    def test_nothing_decoded_before_the_cap_tripped_survives(self):
        """All or nothing. A partial import is indistinguishable from a
        complete one downstream, and in overwrite mode the covered-range
        delete would then span data that never arrived."""
        archive = _zipBytes({
            "a_Streaming_History.json": "x" * 400,   #< fits, and is decoded
            "b_Streaming_History.json": "y" * 900,   #< then the budget runs out
        })

        result = expandUploads([_upload("export.zip", archive)], 1000)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])

    def test_a_lying_declared_size_does_not_get_a_free_pass(self):
        """ZipInfo.file_size is attacker-supplied. Rewriting it to 1 must not
        change what the guard measures - only the bytes actually read count."""
        payload = "x" * 5000
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("Streaming_History.json")
            archive.writestr(info, payload)
        raw = buffer.getvalue()
        self.assertIn(b"\x88\x13", raw, "5000 little-endian should appear as a declared size")
        raw = raw.replace(b"\x88\x13\x00\x00", b"\x01\x00\x00\x00")   #< 5000 -> 1

        result = expandUploads([_upload("liar.zip", raw)], 1000)

        # Either outcome is safe: refused by the guard, or refused as corrupt
        # (zipfile itself checks the declared size against what it inflated).
        # What must never happen is 5000 bytes sailing through a 1000 cap.
        self.assertEqual(result.contents, [])

    def test_content_exactly_at_the_cap_is_allowed(self):
        """Off-by-one: the cap is a ceiling, not an exclusive bound."""
        payload = "x" * 1000
        archive = _zipBytes({"Streaming_History.json": payload})

        result = expandUploads([_upload("export.zip", archive)], 1000)

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.contents, [payload])

    def test_the_cap_is_a_budget_across_every_upload_in_the_request(self):
        """Two archives that each fit but together do not."""
        first = _zipBytes({"Streaming_History_1.json": "x" * 700})
        second = _zipBytes({"Streaming_History_2.json": "y" * 700})

        result = expandUploads([_upload("a.zip", first), _upload("b.zip", second)], 1000)

        self.assertTrue(result.exceededCap)

    def test_the_budget_counts_loose_files_too(self):
        """Otherwise a mixed upload could route around the cap entirely."""
        archive = _zipBytes({"Streaming_History.json": "x" * 700})

        result = expandUploads([
            _upload("loose.json", "y" * 700),
            _upload("export.zip", archive),
        ], 1000)

        self.assertTrue(result.exceededCap)


class TestZipUploadRoute(AppTestCase):
    """The route: what the user gets back."""

    def _makeDb(self):
        db = MagicMock()
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0,
                                        "percentage": 0, "message": "", "error": False}
        return db

    def _importStarted(self, db):
        started = threading.Event()
        db.importHistoryBatch.side_effect = lambda *args, **kwargs: started.set()
        return started

    def _postImport(self, dash, db, files):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.post('/import-history', data=files, content_type='multipart/form-data')

    def _getImportPage(self, dash, db, query):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get('/import' + query).get_data(as_text=True)

    def test_a_zip_upload_reaches_the_importer_as_its_contents(self):
        dash = self._makeApp()
        db = self._makeDb()
        started = self._importStarted(db)
        archive = _zipBytes({_EXPORT_DIR + "Streaming_History_Audio.json": _PLAY_JSON})

        self._postImport(dash, db, {'history_file': (io.BytesIO(archive), 'export.zip')})

        self.assertTrue(started.wait(_IMPORT_THREAD_DEADLINE_SECONDS),
                        "the background import thread never called importHistoryBatch")
        self.assertEqual(db.importHistoryBatch.call_args.args[0], [_PLAY_JSON])

    def test_an_over_cap_archive_redirects_without_starting_an_import(self):
        dash = self._makeApp()
        db = self._makeDb()
        archive = _zipBytes({"Streaming_History.json": "x" * 5000})

        with patch("routes.system.MAX_UNCOMPRESSED_IMPORT_BYTES", 1000):
            resp = self._postImport(dash, db, {'history_file': (io.BytesIO(archive), 'export.zip')})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=expanded_too_large", resp.headers["Location"])
        db.importHistoryBatch.assert_not_called()

    def test_the_over_cap_message_names_the_archive_not_the_upload(self):
        """"Try uploading fewer files" is wrong advice for a 25 MB ZIP that
        the server refused to expand - the request itself was never too big."""
        dash = self._makeApp()
        db = self._makeDb()

        page = self._getImportPage(dash, db, "?error=expanded_too_large")

        self.assertIn("unpacks to more than", page)

    def test_an_archive_without_history_says_so(self):
        dash = self._makeApp()
        db = self._makeDb()
        archive = _zipBytes({"ReadMeFirst.pdf": b"%PDF-1.4"})

        resp = self._postImport(dash, db, {'history_file': (io.BytesIO(archive), 'export.zip')})

        self.assertIn("error=empty_archive", resp.headers["Location"])
        db.importHistoryBatch.assert_not_called()
        self.assertIn("no .json or .csv files", self._getImportPage(dash, db, "?error=empty_archive"))

    def test_the_import_page_is_quiet_without_those_markers(self):
        dash = self._makeApp()
        db = self._makeDb()

        page = self._getImportPage(dash, db, "")

        self.assertNotIn("unpacks to more than", page)
        self.assertNotIn("no .json or .csv files", page)

    def test_the_form_accepts_zip_files(self):
        dash = self._makeApp()
        db = self._makeDb()

        page = self._getImportPage(dash, db, "")

        self.assertIn(".zip", page)

    def test_unreadable_entries_inside_a_zip_are_reported_to_the_batch(self):
        """Same contract as a loose unreadable file: the overwrite-range
        delete must know history went missing before it spans the survivors."""
        dash = self._makeApp()
        db = self._makeDb()
        started = self._importStarted(db)
        archive = _zipBytes({
            "a_Streaming_History.json": _PLAY_JSON,
            "b_broken.json": b"\xff\xfe not utf-8 \xfa",
        })

        self._postImport(dash, db, {
            'history_file': (io.BytesIO(archive), 'export.zip'),
            'overwrite_range': 'on',
        })

        self.assertTrue(started.wait(_IMPORT_THREAD_DEADLINE_SECONDS),
                        "the background import thread never called importHistoryBatch")
        self.assertEqual(db.importHistoryBatch.call_args.kwargs.get("unreadableFileCount"), 1)


if __name__ == "__main__":
    unittest.main()
