# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Release-blocker service guards for ZIP import expansion."""

import io
import struct
import unittest
import zipfile
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import services.import_upload as importUpload
from services.import_upload import expandUploads


_PLAY_JSON = '{"ms_played": 1}'
_OTHER_PLAY_JSON = '{"ms_played": 2}'
_GENEROUS_CAP = 10 * 1024 * 1024
_ENTRY_LIMIT = 3
_UNSUPPORTED_METHOD = 99
_BZIP2_METHOD = 12
_LZMA_METHOD = 14
_ZSTANDARD_LEGACY_METHOD = 20
_ZSTANDARD_METHOD = 93
_UTF8_FLAG = 0x800
_ENCRYPTED_FLAG = 0x1
_LOCAL_SIGNATURE = b"PK\x03\x04"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_LOCAL_FLAGS_OFFSET = 6
_LOCAL_METHOD_OFFSET = 8
_LOCAL_CRC_OFFSET = 14
_LOCAL_COMPRESSED_SIZE_OFFSET = 18
_LOCAL_UNCOMPRESSED_SIZE_OFFSET = 22
_LOCAL_FILENAME_LENGTH_OFFSET = 26
_LOCAL_EXTRA_LENGTH_OFFSET = 28
_LOCAL_FILENAME_OFFSET = 30
_CENTRAL_FLAGS_OFFSET = 8
_CENTRAL_METHOD_OFFSET = 10
_CENTRAL_CRC_OFFSET = 16
_CENTRAL_COMPRESSED_SIZE_OFFSET = 20
_CENTRAL_UNCOMPRESSED_SIZE_OFFSET = 24
_CENTRAL_FILENAME_LENGTH_OFFSET = 28
_CENTRAL_EXTRA_LENGTH_OFFSET = 30
_CENTRAL_FILENAME_OFFSET = 46
_FILENAME_BAD_UTF8_BYTE = 0xff


class _FakeUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self.stream = io.BytesIO(data)


class _ExplodingStream:
    def tell(self, *args, **kwargs):
        raise AssertionError("a later upload was inspected after expansion should have stopped")

    def seek(self, *args, **kwargs):
        raise AssertionError("a later upload was inspected after expansion should have stopped")

    def read(self, *args, **kwargs):
        raise AssertionError("a later upload was read after expansion should have stopped")


class _ExplodingUpload:
    filename = "later.json"
    stream = _ExplodingStream()


def _upload(filename, data):
    return _FakeUpload(filename, data if isinstance(data, bytes) else data.encode("utf-8"))


def _zipBytes(entries, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _patchFirstMemberMethod(raw, method):
    for signature, offset in ((_LOCAL_SIGNATURE, _LOCAL_METHOD_OFFSET),
                              (_CENTRAL_SIGNATURE, _CENTRAL_METHOD_OFFSET)):
        at = raw.find(signature)
        struct.pack_into("<H", raw, at + offset, method)


def _patchAllMemberCrcs(raw, crc):
    position = 0
    while True:
        position = raw.find(_LOCAL_SIGNATURE, position)
        if position < 0:
            break
        struct.pack_into("<I", raw, position + _LOCAL_CRC_OFFSET, crc)
        position += len(_LOCAL_SIGNATURE)
    position = 0
    while True:
        position = raw.find(_CENTRAL_SIGNATURE, position)
        if position < 0:
            break
        struct.pack_into("<I", raw, position + _CENTRAL_CRC_OFFSET, crc)
        position += len(_CENTRAL_SIGNATURE)


def _patchFirstMemberFlags(raw, flag):
    for signature, offset in ((_LOCAL_SIGNATURE, _LOCAL_FLAGS_OFFSET),
                              (_CENTRAL_SIGNATURE, _CENTRAL_FLAGS_OFFSET)):
        at = raw.find(signature)
        flags = struct.unpack_from("<H", raw, at + offset)[0]
        struct.pack_into("<H", raw, at + offset, flags | flag)


def _patchFirstMemberSizes(raw, compressedSize=None, uncompressedSize=None):
    for signature, compressedOffset, uncompressedOffset in (
        (_LOCAL_SIGNATURE, _LOCAL_COMPRESSED_SIZE_OFFSET, _LOCAL_UNCOMPRESSED_SIZE_OFFSET),
        (_CENTRAL_SIGNATURE, _CENTRAL_COMPRESSED_SIZE_OFFSET, _CENTRAL_UNCOMPRESSED_SIZE_OFFSET),
    ):
        at = raw.find(signature)
        if compressedSize is not None:
            struct.pack_into("<I", raw, at + compressedOffset, compressedSize)
        if uncompressedSize is not None:
            struct.pack_into("<I", raw, at + uncompressedOffset, uncompressedSize)


def _historyZipWithMethod(method):
    raw = bytearray(_zipBytes({"history.json": _PLAY_JSON, "later.json": _OTHER_PLAY_JSON},
                              compression=zipfile.ZIP_STORED))
    _patchFirstMemberMethod(raw, method)
    return bytes(raw)


def _zipWithUnsupportedIgnoredSidecar():
    raw = bytearray(_zipBytes({"ReadMeFirst.pdf": b"%PDF-1.4", "history.json": _PLAY_JSON},
                              compression=zipfile.ZIP_STORED))
    _patchFirstMemberMethod(raw, _UNSUPPORTED_METHOD)
    return bytes(raw)


def _zipWithOnlyUnsupportedIgnoredSidecar():
    raw = bytearray(_zipBytes({"ReadMeFirst.pdf": b"%PDF-1.4"}, compression=zipfile.ZIP_STORED))
    _patchFirstMemberMethod(raw, _UNSUPPORTED_METHOD)
    return bytes(raw)


def _zipWithBadCrc(entries):
    raw = bytearray(_zipBytes(entries))
    _patchAllMemberCrcs(raw, 0)
    return bytes(raw)


def _zipWithCorruptLocalHeader():
    raw = bytearray(_zipBytes({"history.json": _PLAY_JSON}, compression=zipfile.ZIP_STORED))
    local = raw.find(_LOCAL_SIGNATURE)
    raw[local:local + len(_LOCAL_SIGNATURE)] = b"NOPE"
    return bytes(raw)


def _nonZipTruncatedBytes():
    raw = _zipBytes({"history.json": b"\xff\xfe not utf-8 \xfa"})
    return raw[:len(raw) // 2]


def _zipWithMalformedCentralFilename():
    raw = bytearray(_zipBytes({"history.json": _PLAY_JSON}, compression=zipfile.ZIP_STORED))
    central = raw.find(_CENTRAL_SIGNATURE)
    flags = struct.unpack_from("<H", raw, central + _CENTRAL_FLAGS_OFFSET)[0]
    struct.pack_into("<H", raw, central + _CENTRAL_FLAGS_OFFSET, flags | _UTF8_FLAG)
    # First central-directory filename byte. The local header stays readable.
    raw[central + _CENTRAL_FILENAME_OFFSET] = _FILENAME_BAD_UTF8_BYTE
    return bytes(raw)


def _zipWithMalformedLocalFilename():
    raw = bytearray(_zipBytes({"history.json": _PLAY_JSON}, compression=zipfile.ZIP_STORED))
    local = raw.find(_LOCAL_SIGNATURE)
    flags = struct.unpack_from("<H", raw, local + _LOCAL_FLAGS_OFFSET)[0]
    struct.pack_into("<H", raw, local + _LOCAL_FLAGS_OFFSET, flags | _UTF8_FLAG)
    # First local-header filename byte. The central directory stays readable, so
    # construction succeeds and the failure is at archive.open(info).
    raw[local + _LOCAL_FILENAME_OFFSET] = _FILENAME_BAD_UTF8_BYTE
    return bytes(raw)


def _zipWithEncryptedStoredMember():
    raw = bytearray(_zipBytes({"history.json": _PLAY_JSON}, compression=zipfile.ZIP_STORED))
    _patchFirstMemberFlags(raw, _ENCRYPTED_FLAG)
    return bytes(raw)


def _zipWithOversizedDeclaredMember(declaredSize):
    raw = bytearray(_zipBytes({"history.json": _PLAY_JSON}, compression=zipfile.ZIP_STORED))
    _patchFirstMemberSizes(raw, uncompressedSize=declaredSize)
    return bytes(raw)


def _lzmaZipBytes():
    return _zipBytes({"history.json": _PLAY_JSON}, compression=zipfile.ZIP_LZMA)


def _corruptLzmaZipBytes():
    raw = bytearray(_lzmaZipBytes())
    local = raw.find(_LOCAL_SIGNATURE)
    filenameLength = struct.unpack_from("<H", raw, local + _LOCAL_FILENAME_LENGTH_OFFSET)[0]
    extraLength = struct.unpack_from("<H", raw, local + _LOCAL_EXTRA_LENGTH_OFFSET)[0]
    dataOffset = local + _LOCAL_FILENAME_OFFSET + filenameLength + extraLength
    raw[dataOffset] ^= 0xff
    return bytes(raw)


def _zipWithDirectoryAndFile():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("folder/"), b"")
        archive.writestr("folder/history.json", _PLAY_JSON)
    return buffer.getvalue()


class TestImportUploadArchiveGuards(unittest.TestCase):
    def assertNoTerminalArchiveFlag(self, result):
        self.assertFalse(getattr(result, "unsupportedCompression", False))
        self.assertFalse(getattr(result, "unreadableArchive", False))

    def test_unsupported_history_compression_rejects_the_whole_request(self):
        result = expandUploads([
            _upload("good.json", _PLAY_JSON),
            _upload("unsupported.zip", _historyZipWithMethod(_UNSUPPORTED_METHOD)),
            _ExplodingUpload(),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unsupportedCompression)
        self.assertEqual(result.contents, [])
        self.assertEqual(result.unreadableCount, 0)

    def test_standard_unsupported_methods_reject_the_whole_request(self):
        for method in (_BZIP2_METHOD, _LZMA_METHOD, _ZSTANDARD_LEGACY_METHOD, _ZSTANDARD_METHOD):
            with self.subTest(method=method):
                archive = _historyZipWithMethod(method)
                realOpen = zipfile.ZipFile.open
                opened = []

                def recordingOpen(zipFile, *args, **kwargs):
                    opened.append(args[0])
                    return realOpen(zipFile, *args, **kwargs)

                with patch("zipfile.ZipFile.open", new=recordingOpen):
                    result = expandUploads([
                        _upload("unsupported.zip", archive),
                    ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

                self.assertTrue(result.unsupportedCompression)
                self.assertEqual(result.contents, [])
                self.assertEqual(opened, [])

    def test_real_lzma_member_is_rejected_before_open(self):
        for archiveFactory in (_lzmaZipBytes, _corruptLzmaZipBytes):
            with self.subTest(archive=archiveFactory.__name__):
                archive = archiveFactory()
                realOpen = zipfile.ZipFile.open
                opened = []

                def recordingOpen(zipFile, *args, **kwargs):
                    opened.append(args[0])
                    return realOpen(zipFile, *args, **kwargs)

                with patch("zipfile.ZipFile.open", new=recordingOpen):
                    result = expandUploads([
                        _upload("lzma.zip", archive),
                    ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

                self.assertTrue(result.unsupportedCompression)
                self.assertEqual(result.contents, [])
                self.assertEqual(opened, [])

    def test_unsupported_compression_on_ignored_sidecar_is_ignored(self):
        result = expandUploads([
            _upload("export.zip", _zipWithUnsupportedIgnoredSidecar()),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertNoTerminalArchiveFlag(result)

    def test_unsupported_compression_on_ignored_only_archive_preserves_empty_archive(self):
        result = expandUploads([
            _upload("export.zip", _zipWithOnlyUnsupportedIgnoredSidecar()),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertEqual(result.contents, [])
        self.assertTrue(result.emptyArchive)
        self.assertNoTerminalArchiveFlag(result)

    def test_bad_crc_rejects_the_whole_request_and_stops_later_inputs(self):
        archive = _zipWithBadCrc({
            "a_history.json": _PLAY_JSON,
            "b_history.json": _OTHER_PLAY_JSON,
        })

        result = expandUploads([
            _upload("good.json", _PLAY_JSON),
            _upload("bad.zip", archive),
            _ExplodingUpload(),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(result.contents, [])

    def test_malformed_central_directory_filename_is_a_terminal_archive_error(self):
        result = expandUploads([
            _upload("bad-name.zip", _zipWithMalformedCentralFilename()),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(result.contents, [])

    def test_malformed_local_header_filename_is_a_terminal_archive_error(self):
        result = expandUploads([
            _upload("bad-local-name.zip", _zipWithMalformedLocalFilename()),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(result.contents, [])

    def test_corrupt_local_header_is_a_terminal_archive_error(self):
        result = expandUploads([
            _upload("bad-header.zip", _zipWithCorruptLocalHeader()),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(result.contents, [])

    def test_truncation_without_eocd_uses_the_loose_upload_path(self):
        result = expandUploads([
            _upload("truncated.zip", _nonZipTruncatedBytes()),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertEqual(result.contents, [])
        self.assertEqual(result.unreadableCount, 1)
        self.assertNoTerminalArchiveFlag(result)

    def test_encrypted_allowed_member_is_a_terminal_archive_error(self):
        result = expandUploads([
            _upload("encrypted.zip", _zipWithEncryptedStoredMember()),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(result.contents, [])

    def test_missing_deflate_decoder_is_a_terminal_archive_error(self):
        archive = _zipBytes({"history.json": _PLAY_JSON}, compression=zipfile.ZIP_DEFLATED)
        realGetDecompressor = zipfile._get_decompressor

        def missingDeflate(compressType):
            if compressType == zipfile.ZIP_DEFLATED:
                raise NotImplementedError("deflate decoder missing")
            return realGetDecompressor(compressType)

        with patch("zipfile._get_decompressor", side_effect=missingDeflate):
            result = expandUploads([
                _upload("deflate.zip", archive),
            ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(result.contents, [])

    def test_oversized_declared_member_rejects_without_opening(self):
        archive = _zipWithOversizedDeclaredMember(declaredSize=1001)
        realOpen = zipfile.ZipFile.open
        opened = []

        def recordingOpen(zipFile, *args, **kwargs):
            opened.append(args[0])
            return realOpen(zipFile, *args, **kwargs)

        with patch("zipfile.ZipFile.open", new=recordingOpen):
            result = expandUploads([
                _upload("too-big.zip", archive),
            ], 1000, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])
        self.assertNoTerminalArchiveFlag(result)
        self.assertEqual(opened, [])

    def test_archive_entry_limit_is_request_wide_and_clears_prior_contents(self):
        first = _zipBytes({
            "one.json": _PLAY_JSON,
            "ignored.pdf": b"%PDF-1.4",
        })
        second = _zipBytes({
            "two.json": _OTHER_PLAY_JSON,
            "also-ignored.pdf": b"%PDF-1.4",
        })
        opened = []
        realOpen = zipfile.ZipFile.open

        def recordOpen(archive, info, *args, **kwargs):
            opened.append(info.filename)
            return realOpen(archive, info, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "open", recordOpen):
            result = expandUploads([
                _upload("first.zip", first),
                _upload("second.zip", second),
                _ExplodingUpload(),
            ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.tooManyEntries)
        self.assertEqual(result.contents, [])
        self.assertEqual(opened, ["one.json"])

    def test_cumulative_archive_entry_limit_allows_exactly_the_limit(self):
        first = _zipWithDirectoryAndFile()
        second = _zipBytes({"two.json": _OTHER_PLAY_JSON})

        result = expandUploads([
            _upload("first.zip", first),
            _upload("second.zip", second),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertFalse(result.tooManyEntries)
        self.assertEqual(result.contents, [_PLAY_JSON, _OTHER_PLAY_JSON])
        self.assertNoTerminalArchiveFlag(result)

    def test_directories_count_toward_the_request_wide_entry_limit(self):
        result = expandUploads([
            _upload("with-directory.zip", _zipWithDirectoryAndFile()),
            _upload("second.zip", _zipBytes({"two.json": _OTHER_PLAY_JSON})),
        ], _GENEROUS_CAP, maxArchiveEntries=2)

        self.assertTrue(result.tooManyEntries)
        self.assertEqual(result.contents, [])

    def test_invalid_utf8_history_member_still_skips_individually(self):
        archive = _zipBytes({
            "good.json": _PLAY_JSON,
            "bad.json": b"\xff\xfe not utf-8 \xfa",
        })

        result = expandUploads([
            _upload("mixed.zip", archive),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertEqual(result.contents, [_PLAY_JSON])
        self.assertEqual(result.unreadableCount, 1)
        self.assertNoTerminalArchiveFlag(result)

    def test_prior_unreadable_count_is_preserved_by_later_terminal_archive_error(self):
        result = expandUploads([
            _upload("bad.json", b"\xff\xfe not utf-8 \xfa"),
            _upload("bad.zip", _zipWithBadCrc({"history.json": _PLAY_JSON})),
        ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(result.contents, [])
        self.assertEqual(result.unreadableCount, 1)

    def test_no_later_member_is_opened_after_terminal_archive_error(self):
        archive = _zipWithBadCrc({
            "a_bad.json": _PLAY_JSON,
            "b_later.json": _OTHER_PLAY_JSON,
        })

        realOpen = zipfile.ZipFile.open
        opened = []

        def recordingOpen(archive, *args, **kwargs):
            opened.append(args[0])
            return realOpen(archive, *args, **kwargs)

        with patch("zipfile.ZipFile.open", new=recordingOpen):
            result = expandUploads([
                _upload("bad.zip", archive),
            ], _GENEROUS_CAP, maxArchiveEntries=_ENTRY_LIMIT)

        self.assertTrue(result.unreadableArchive)
        self.assertEqual(len(opened), 1)


if __name__ == "__main__":
    unittest.main()
