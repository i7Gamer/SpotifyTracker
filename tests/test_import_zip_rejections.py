# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Route/UI coverage for terminal ZIP rejection outcomes.

The upload service owns detecting corrupt archives and unsupported codecs. These
route tests pin what /import-history and /import must do once expansion reports a
terminal outcome, using explicit expansion objects so failures point at route and
template behavior instead of not-yet-added service fields.
"""
import io
import lzma
import os
import struct
import sys
import unittest
import zipfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from _app_factory import AppTestCase


_PLAY_JSON = '{"ms_played": 1}'
_SECOND_PLAY_JSON = '{"ms_played": 2}'
_ROUTE_CAP_BYTES = 10
_ENTRY_LIMIT_FOR_ROUTE_TEST = 1
_ZIP_UTF8_FLAG = 0x800
_BAD_FILENAME_BYTE = 0xFF
_LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"
_CENTRAL_HEADER_SIGNATURE = b"PK\x01\x02"
_LOCAL_FLAG_OFFSET = 6
_LOCAL_CRC_OFFSET = 14
_LOCAL_NAME_LENGTH_OFFSET = 26
_LOCAL_EXTRA_LENGTH_OFFSET = 28
_LOCAL_NAME_OFFSET = 30
_LOCAL_DATA_OFFSET = 30
_CENTRAL_FLAG_OFFSET = 8
_CENTRAL_CRC_OFFSET = 16
_CENTRAL_NAME_OFFSET = 46
_BAD_CRC = 0
_LZMA_HEADER_BYTES_TO_KEEP = 9
_CORRUPT_LZMA_BYTE = 0xFF


def _expansion(**overrides):
    values = {
        "contents": [],
        "unreadableCount": 0,
        "exceededCap": False,
        "emptyArchive": False,
        "tooManyEntries": False,
        "unsupportedCompression": False,
        "unreadableArchive": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _zipBytes(entries, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _badCrcZip():
    raw = bytearray(_zipBytes({"Streaming_History.json": _PLAY_JSON}))
    local = raw.find(_LOCAL_HEADER_SIGNATURE)
    central = raw.find(_CENTRAL_HEADER_SIGNATURE)
    struct.pack_into("<L", raw, local + _LOCAL_CRC_OFFSET, _BAD_CRC)
    struct.pack_into("<L", raw, central + _CENTRAL_CRC_OFFSET, _BAD_CRC)
    return bytes(raw)


def _lzmaZip():
    return _zipBytes({"Streaming_History.json": _PLAY_JSON}, compression=zipfile.ZIP_LZMA)


def _corruptLzmaZip():
    raw = bytearray(_lzmaZip())
    local = raw.find(_LOCAL_HEADER_SIGNATURE)
    central = raw.find(_CENTRAL_HEADER_SIGNATURE)
    nameLength = struct.unpack_from("<H", raw, local + _LOCAL_NAME_LENGTH_OFFSET)[0]
    extraLength = struct.unpack_from("<H", raw, local + _LOCAL_EXTRA_LENGTH_OFFSET)[0]
    payloadStart = local + _LOCAL_DATA_OFFSET + nameLength + extraLength
    payloadEnd = central
    corruptionStart = payloadStart + _LZMA_HEADER_BYTES_TO_KEEP
    raw[corruptionStart:payloadEnd] = bytes([_CORRUPT_LZMA_BYTE]) * (payloadEnd - corruptionStart)
    return bytes(raw)


def _withBadCrc(raw):
    mutated = bytearray(raw)
    local = mutated.find(_LOCAL_HEADER_SIGNATURE)
    central = mutated.find(_CENTRAL_HEADER_SIGNATURE)
    struct.pack_into("<L", mutated, local + _LOCAL_CRC_OFFSET, _BAD_CRC)
    struct.pack_into("<L", mutated, central + _CENTRAL_CRC_OFFSET, _BAD_CRC)
    return bytes(mutated)


def _centralFilenameUnicodeErrorZip():
    raw = bytearray(_zipBytes({"Streaming_History.json": _PLAY_JSON}))
    central = raw.find(_CENTRAL_HEADER_SIGNATURE)
    flags = struct.unpack_from("<H", raw, central + _CENTRAL_FLAG_OFFSET)[0]
    struct.pack_into("<H", raw, central + _CENTRAL_FLAG_OFFSET, flags | _ZIP_UTF8_FLAG)
    raw[central + _CENTRAL_NAME_OFFSET] = _BAD_FILENAME_BYTE
    return bytes(raw)


def _localFilenameUnicodeErrorZip():
    raw = bytearray(_zipBytes({"Streaming_History.json": _PLAY_JSON}))
    local = raw.find(_LOCAL_HEADER_SIGNATURE)
    central = raw.find(_CENTRAL_HEADER_SIGNATURE)
    localFlags = struct.unpack_from("<H", raw, local + _LOCAL_FLAG_OFFSET)[0]
    centralFlags = struct.unpack_from("<H", raw, central + _CENTRAL_FLAG_OFFSET)[0]
    struct.pack_into("<H", raw, local + _LOCAL_FLAG_OFFSET, localFlags | _ZIP_UTF8_FLAG)
    struct.pack_into("<H", raw, central + _CENTRAL_FLAG_OFFSET, centralFlags | _ZIP_UTF8_FLAG)
    raw[local + _LOCAL_NAME_OFFSET] = _BAD_FILENAME_BYTE
    return bytes(raw)


class TestZipImportTerminalRouteOutcomes(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.tryClaimImportRunning.return_value = True
        db.readProgress.return_value = {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percentage": 0,
            "message": "",
            "error": False,
        }
        return db

    def _client(self, dash, db):
        patchers = (
            patch.object(dash, "is_user_logged_in", return_value=True),
            patch.object(dash, "get_username_for_email", return_value="alice"),
            patch.object(dash, "get_user_db", return_value=db),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess["email"] = "alice@example.com"
        return client

    def _postImport(self, client, overwrite=False):
        data = {"history_file": (io.BytesIO(b"placeholder"), "export.zip")}
        if overwrite:
            data["overwrite_range"] = "on"
        return client.post("/import-history", data=data, content_type="multipart/form-data")

    def _postFiles(self, client, files, overwrite=False):
        data = {"history_file": [(io.BytesIO(raw), name) for name, raw in files]}
        if overwrite:
            data["overwrite_range"] = "on"
        return client.post("/import-history", data=data, content_type="multipart/form-data")

    def _assertFatalRouteOutcome(self, expansion, marker):
        for overwrite in (False, True):
            with self.subTest(marker=marker, overwrite=overwrite):
                dash = self._makeApp()
                db = self._makeDb()
                client = self._client(dash, db)
                with patch("routes.system.expandUploads", return_value=expansion), \
                     patch("routes.system.threading.Thread") as threadCtor:
                    resp = self._postImport(client, overwrite=overwrite)

                self.assertEqual(resp.status_code, 302)
                self.assertIn(f"error={marker}", resp.headers["Location"])
                db.tryClaimImportRunning.assert_not_called()
                db.importHistoryBatch.assert_not_called()
                threadCtor.assert_not_called()
                threadCtor.return_value.start.assert_not_called()

    def _assertRealFatalUploads(self, files, marker, patchFactories=()):
        for overwrite in (False, True):
            with self.subTest(marker=marker, overwrite=overwrite):
                dash = self._makeApp()
                db = self._makeDb()
                client = self._client(dash, db)
                with ExitStack() as stack:
                    threadCtor = stack.enter_context(patch("routes.system.threading.Thread"))
                    for patchFactory in patchFactories:
                        stack.enter_context(patchFactory())
                    resp = self._postFiles(client, files, overwrite=overwrite)

                self.assertEqual(resp.status_code, 302)
                self.assertIn(f"error={marker}", resp.headers["Location"])
                db.tryClaimImportRunning.assert_not_called()
                db.importHistoryBatch.assert_not_called()
                threadCtor.assert_not_called()
                threadCtor.return_value.start.assert_not_called()

    def test_unsupported_compression_redirects_before_claim_thread_or_batch(self):
        self._assertFatalRouteOutcome(
            _expansion(unsupportedCompression=True),
            "unsupported_compression",
        )

    def test_unreadable_archive_redirects_before_claim_thread_or_batch(self):
        self._assertFatalRouteOutcome(
            _expansion(unreadableArchive=True),
            "unreadable_archive",
        )

    def test_terminal_outcomes_win_over_an_earlier_empty_archive(self):
        cases = (
            (_expansion(emptyArchive=True, unsupportedCompression=True), "unsupported_compression"),
            (_expansion(emptyArchive=True, unreadableArchive=True), "unreadable_archive"),
        )
        for expansion, marker in cases:
            with self.subTest(marker=marker):
                dash = self._makeApp()
                db = self._makeDb()
                client = self._client(dash, db)
                with patch("routes.system.expandUploads", return_value=expansion), \
                     patch("routes.system.threading.Thread") as threadCtor:
                    resp = self._postImport(client)

                self.assertIn(f"error={marker}", resp.headers["Location"])
                db.tryClaimImportRunning.assert_not_called()
                db.importHistoryBatch.assert_not_called()
                threadCtor.assert_not_called()

    def test_real_lzma_archives_redirect_as_unsupported_before_claim_or_thread(self):
        cases = (
            ("valid-lzma.zip", _lzmaZip()),
            ("corrupt-lzma.zip", _corruptLzmaZip()),
        )
        for name, raw in cases:
            with self.subTest(name=name):
                self._assertRealFatalUploads([(name, raw)], "unsupported_compression")

    def test_lzma_fixtures_exercise_valid_and_decoder_error_controls(self):
        with zipfile.ZipFile(io.BytesIO(_lzmaZip())) as archive:
            self.assertEqual(archive.read("Streaming_History.json").decode("utf-8"), _PLAY_JSON)

        with zipfile.ZipFile(io.BytesIO(_corruptLzmaZip())) as archive:
            with self.assertRaises(lzma.LZMAError):
                archive.read("Streaming_History.json")

    def test_real_unreadable_allowed_archives_redirect_before_claim_or_thread(self):
        cases = (
            ("bad-crc.zip", _badCrcZip()),
            ("bad-central-name.zip", _centralFilenameUnicodeErrorZip()),
            ("bad-local-name.zip", _localFilenameUnicodeErrorZip()),
        )
        for name, raw in cases:
            with self.subTest(name=name):
                self._assertRealFatalUploads([(name, raw)], "unreadable_archive")

    def test_real_terminal_archive_discards_good_before_it_and_later_uploads(self):
        cases = (
            ("unsupported_compression", "bad.zip", _lzmaZip()),
            ("unreadable_archive", "bad.zip", _badCrcZip()),
        )
        for marker, badName, badRaw in cases:
            with self.subTest(marker=marker):
                self._assertRealFatalUploads([
                    ("good.json", _PLAY_JSON.encode("utf-8")),
                    (badName, badRaw),
                    ("later.json", _SECOND_PLAY_JSON.encode("utf-8")),
                ], marker)

    def test_real_empty_history_zip_before_terminal_archive_keeps_terminal_marker(self):
        emptyHistory = _zipBytes({"ReadMeFirst.pdf": b"%PDF-1.4"})
        cases = (
            ("unsupported_compression", "bad.zip", _lzmaZip()),
            ("unreadable_archive", "bad.zip", _badCrcZip()),
        )
        for marker, badName, badRaw in cases:
            with self.subTest(marker=marker):
                self._assertRealFatalUploads([
                    ("account.zip", emptyHistory),
                    (badName, badRaw),
                ], marker)

    def test_real_cumulative_entry_count_redirects_before_claim_or_thread(self):
        self._assertRealFatalUploads([
            ("one.zip", _zipBytes({"first.json": _PLAY_JSON})),
            ("two.zip", _zipBytes({"second.json": _SECOND_PLAY_JSON})),
        ], "too_many_entries", patchFactories=[
            lambda: patch("routes.system.MAX_IMPORT_ARCHIVE_ENTRIES", _ENTRY_LIMIT_FOR_ROUTE_TEST),
        ])

    def test_real_cap_rejection_redirects_before_claim_or_thread(self):
        self._assertRealFatalUploads([
            ("too-large.zip", _zipBytes({"Streaming_History.json": _PLAY_JSON})),
        ], "expanded_too_large", patchFactories=[
            lambda: patch("routes.system.MAX_UNCOMPRESSED_IMPORT_BYTES", _ROUTE_CAP_BYTES),
        ])

    def test_invalid_utf8_skip_count_still_reaches_the_batch(self):
        dash = self._makeApp()
        db = self._makeDb()
        client = self._client(dash, db)
        createdThreads = []

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                self.start = MagicMock(side_effect=target)
                createdThreads.append(self)

        with patch("routes.system.expandUploads", return_value=_expansion(
            contents=[_PLAY_JSON],
            unreadableCount=1,
        )), patch("routes.system.threading.Thread", side_effect=InlineThread) as threadCtor:
            resp = self._postImport(client, overwrite=True)

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("error=", resp.headers["Location"])
        db.tryClaimImportRunning.assert_called_once()
        threadCtor.assert_called_once()
        self.assertEqual(len(createdThreads), 1)
        createdThreads[0].start.assert_called_once()
        db.importHistoryBatch.assert_called_once_with(
            [_PLAY_JSON],
            overwriteRange=True,
            unreadableFileCount=1,
        )

    def test_successful_import_claims_once_and_starts_one_batch(self):
        dash = self._makeApp()
        db = self._makeDb()
        client = self._client(dash, db)
        createdThreads = []

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                self.start = MagicMock(side_effect=target)
                createdThreads.append(self)

        with patch("routes.system.expandUploads", return_value=_expansion(contents=[_PLAY_JSON])), \
             patch("routes.system.threading.Thread", side_effect=InlineThread) as threadCtor:
            resp = self._postImport(client)

        self.assertEqual(resp.status_code, 302)
        db.tryClaimImportRunning.assert_called_once()
        threadCtor.assert_called_once()
        self.assertEqual(len(createdThreads), 1)
        createdThreads[0].start.assert_called_once()
        db.importHistoryBatch.assert_called_once_with(
            [_PLAY_JSON],
            overwriteRange=False,
            unreadableFileCount=0,
        )

    def test_real_invalid_utf8_zip_entry_keeps_good_sibling_and_reports_drop_count(self):
        dash = self._makeApp()
        db = self._makeDb()
        client = self._client(dash, db)
        createdThreads = []

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                self.start = MagicMock(side_effect=target)
                createdThreads.append(self)

        archive = _zipBytes({
            "a_Streaming_History.json": _PLAY_JSON,
            "b_Streaming_History.json": b"\xff\xfe not utf-8",
        })
        with patch("routes.system.threading.Thread", side_effect=InlineThread) as threadCtor:
            resp = self._postFiles(client, [("history.zip", archive)], overwrite=True)

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("error=", resp.headers["Location"])
        db.tryClaimImportRunning.assert_called_once()
        threadCtor.assert_called_once()
        self.assertEqual(len(createdThreads), 1)
        createdThreads[0].start.assert_called_once()
        db.importHistoryBatch.assert_called_once_with(
            [_PLAY_JSON],
            overwriteRange=True,
            unreadableFileCount=1,
        )

    def test_real_standard_zip_success_claims_once_and_starts_one_batch(self):
        dash = self._makeApp()
        db = self._makeDb()
        client = self._client(dash, db)
        createdThreads = []

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                self.start = MagicMock(side_effect=target)
                createdThreads.append(self)

        archive = _zipBytes({"Streaming_History.json": _PLAY_JSON})
        with patch("routes.system.threading.Thread", side_effect=InlineThread) as threadCtor:
            resp = self._postFiles(client, [("history.zip", archive)])

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("error=", resp.headers["Location"])
        db.tryClaimImportRunning.assert_called_once()
        threadCtor.assert_called_once()
        self.assertEqual(len(createdThreads), 1)
        createdThreads[0].start.assert_called_once()
        db.importHistoryBatch.assert_called_once_with(
            [_PLAY_JSON],
            overwriteRange=False,
            unreadableFileCount=0,
        )


class TestZipImportRejectionMessages(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.readProgress.return_value = {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percentage": 0,
            "message": "",
            "error": False,
        }
        return db

    def _page(self, query=""):
        dash = self._makeApp()
        db = self._makeDb()
        with patch.object(dash, "is_user_logged_in", return_value=True), \
             patch.object(dash, "get_username_for_email", return_value="alice"), \
             patch.object(dash, "get_user_db", return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess["email"] = "alice@example.com"
            return client.get("/import" + query).get_data(as_text=True)

    def test_new_error_markers_have_clear_recovery_messages(self):
        unsupported = self._page("?error=unsupported_compression")
        unreadable = self._page("?error=unreadable_archive")

        self.assertIn("unsupported compression", unsupported)
        self.assertIn("Stored or Deflate", unsupported)
        self.assertIn("could not be read safely", unreadable)
        self.assertIn("password-protected", unreadable)
        self.assertIn("JSON/CSV files", unreadable)

    def test_default_page_is_quiet_about_new_error_markers(self):
        page = self._page("")

        self.assertNotIn("unsupported compression", page)
        self.assertNotIn("could not be read safely", page)
        self.assertNotIn("password-protected", page)

    def test_import_hint_names_the_accepted_zip_methods(self):
        page = self._page("")

        self.assertIn("Stored or Deflate", page)

    def test_too_many_entries_message_describes_the_request_wide_count(self):
        page = self._page("?error=too_many_entries")

        self.assertIn("total entries across ZIPs in one request", page)


if __name__ == "__main__":
    unittest.main()
