# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resource-budget tests for ZIP import expansion.

These tests measure actual stdlib decompressor output/read work instead of
asserting the requested ``read(n)`` argument.
"""

import binascii
import io
import os
import random
import struct
import sys
import unittest
import zipfile
import zlib
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.import_upload import expandUploads


ZIP_READ_AHEAD_MARGIN_BYTES = 4096
CONTENT_CAP_BYTES = 64 * 1024
DOUBLE_CAP_PAYLOAD_BYTES = CONTENT_CAP_BYTES * 2
OVERSHOOT_BYTES = 1
HONEST_SMALL_PAYLOAD_BYTES = 900
TINY_DECLARED_SIZE_BYTES = 1
ZERO_DECLARED_SIZE_BYTES = 0
TWO_DECLARED_SIZE_BYTES = 2
REPEATED_FORGED_ENTRY_COUNT = 3
SMALL_LOOSE_CAP_BYTES = 10
EXACT_CAP_PAYLOAD = "x" * SMALL_LOOSE_CAP_BYTES
OVER_CAP_PAYLOAD = "x" * (SMALL_LOOSE_CAP_BYTES + OVERSHOOT_BYTES)
FIRST_PARTIAL_PAYLOAD = "a" * 6
SECOND_PARTIAL_PAYLOAD = "b" * 5
INCOMPRESSIBLE_PREFIX_BYTES = 3072
INCOMPRESSIBLE_PREFIX_SEED = 20260922
INCOMPRESSIBLE_PREFIX_ASCII_LEAD = b"{"
MIN_COMPRESSED_PREFIX_RATIO = 0.90
INCOMPRESSIBLE_PREFIX = (
    INCOMPRESSIBLE_PREFIX_ASCII_LEAD
    + random.Random(INCOMPRESSIBLE_PREFIX_SEED).randbytes(INCOMPRESSIBLE_PREFIX_BYTES - 1)
)
COMPRESSIBLE_TAIL = b"z" * CONTENT_CAP_BYTES
INVALID_UTF8_PAYLOAD = b"\xff" * len(FIRST_PARTIAL_PAYLOAD)
ZIP_LOCAL_FILE_HEADER_SIGNATURE = b"PK\x03\x04"
ZIP_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
ZIP_LOCAL_CRC_OFFSET = 14
ZIP_LOCAL_SIZE_OFFSET = 22
ZIP_LOCAL_NAME_LENGTH_OFFSET = 26
ZIP_LOCAL_EXTRA_LENGTH_OFFSET = 28
ZIP_LOCAL_NAME_START_OFFSET = 30
ZIP_CENTRAL_CRC_OFFSET = 16
ZIP_CENTRAL_SIZE_OFFSET = 24
ZIP_CENTRAL_NAME_LENGTH_OFFSET = 28
ZIP_CENTRAL_EXTRA_LENGTH_OFFSET = 30
ZIP_CENTRAL_COMMENT_LENGTH_OFFSET = 32
ZIP_CENTRAL_NAME_START_OFFSET = 46


def _asBytes(data):
    return data if isinstance(data, bytes) else data.encode("utf-8")


class _FakeUpload:
    """The two FileStorage attributes used by services.import_upload."""

    def __init__(self, filename, data):
        self.filename = filename
        self.stream = io.BytesIO(data)


def _upload(filename, data):
    return _FakeUpload(filename, _asBytes(data))


class _CountingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.readCount = 0

    def read(self, *args, **kwargs):
        self.readCount += 1
        return super().read(*args, **kwargs)


class _ReadCountingUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self.stream = _CountingStream(_asBytes(data))


def _zipBytes(entries, compression=zipfile.ZIP_DEFLATED, compressionLevel=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression,
                         compresslevel=compressionLevel) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF


def _patchZipInfoSize(rawArchive, filename, declaredSize, declaredCrc):
    """Rewrite one member's declared uncompressed size and CRC in both headers."""
    raw = bytearray(rawArchive)
    filenameBytes = filename.encode("utf-8")
    localAt = raw.find(ZIP_LOCAL_FILE_HEADER_SIGNATURE)
    while localAt != -1:
        nameLength = struct.unpack_from("<H", raw, localAt + ZIP_LOCAL_NAME_LENGTH_OFFSET)[0]
        extraLength = struct.unpack_from("<H", raw, localAt + ZIP_LOCAL_EXTRA_LENGTH_OFFSET)[0]
        nameAt = localAt + ZIP_LOCAL_NAME_START_OFFSET
        if raw[nameAt:nameAt + nameLength] == filenameBytes:
            struct.pack_into("<L", raw, localAt + ZIP_LOCAL_CRC_OFFSET, declaredCrc)
            struct.pack_into("<L", raw, localAt + ZIP_LOCAL_SIZE_OFFSET, declaredSize)
            break
        localAt = raw.find(
            ZIP_LOCAL_FILE_HEADER_SIGNATURE,
            nameAt + nameLength + extraLength,
        )
    else:
        raise AssertionError(f"local header for {filename!r} not found")

    centralAt = raw.find(ZIP_CENTRAL_DIRECTORY_SIGNATURE)
    while centralAt != -1:
        nameLength = struct.unpack_from("<H", raw, centralAt + ZIP_CENTRAL_NAME_LENGTH_OFFSET)[0]
        extraLength = struct.unpack_from("<H", raw, centralAt + ZIP_CENTRAL_EXTRA_LENGTH_OFFSET)[0]
        commentLength = struct.unpack_from(
            "<H",
            raw,
            centralAt + ZIP_CENTRAL_COMMENT_LENGTH_OFFSET,
        )[0]
        nameAt = centralAt + ZIP_CENTRAL_NAME_START_OFFSET
        if raw[nameAt:nameAt + nameLength] == filenameBytes:
            struct.pack_into("<L", raw, centralAt + ZIP_CENTRAL_CRC_OFFSET, declaredCrc)
            struct.pack_into("<L", raw, centralAt + ZIP_CENTRAL_SIZE_OFFSET, declaredSize)
            return bytes(raw)
        centralAt = raw.find(
            ZIP_CENTRAL_DIRECTORY_SIGNATURE,
            nameAt + nameLength + extraLength + commentLength,
        )
    raise AssertionError(f"central directory header for {filename!r} not found")


def _forgedDeflateArchive(filename, payload, declaredSize):
    encoded = _asBytes(payload)
    prefix = encoded[:declaredSize]
    archive = _zipBytes({filename: encoded}, compression=zipfile.ZIP_DEFLATED, compressionLevel=9)
    return _patchZipInfoSize(archive, filename, declaredSize, _crc32(prefix))


def _forgedStoredArchive(filename, payload, declaredSize):
    encoded = _asBytes(payload)
    prefix = encoded[:declaredSize]
    archive = _zipBytes({filename: encoded}, compression=zipfile.ZIP_STORED)
    return _patchZipInfoSize(archive, filename, declaredSize, _crc32(prefix))


class _CountingDecompressor:
    """Wrap the real decompressor and count every byte it returns, including flush."""

    def __init__(self, wrapped, meter):
        self._wrapped = wrapped
        self._meter = meter

    def decompress(self, data, max_length=0):
        chunk = self._wrapped.decompress(data, max_length)
        self._meter["deflateOutputBytes"] += len(chunk)
        return chunk

    def flush(self, *args, **kwargs):
        chunk = self._wrapped.flush(*args, **kwargs)
        self._meter["deflateFlushCalls"] += 1
        self._meter["deflateFlushOutputBytes"] += len(chunk)
        self._meter["deflateOutputBytes"] += len(chunk)
        return chunk

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _expandWithDeflateOutputMeter(uploads, cap, maxArchiveEntries=None):
    meter = {
        "deflateOutputBytes": 0,
        "deflateFlushCalls": 0,
        "deflateFlushOutputBytes": 0,
    }
    realGetDecompressor = zipfile._get_decompressor

    def measuredGetDecompressor(compressType):
        decompressor = realGetDecompressor(compressType)
        if compressType == zipfile.ZIP_DEFLATED:
            return _CountingDecompressor(decompressor, meter)
        return decompressor

    with patch.object(zipfile, "_get_decompressor", measuredGetDecompressor):
        result = expandUploads(uploads, cap, maxArchiveEntries=maxArchiveEntries)
    return result, meter


def _expandWithStoredReadMeter(uploads, cap, maxArchiveEntries=None):
    meter = {"storedReadBytes": 0}
    realRead2 = zipfile.ZipExtFile._read2

    def measuredRead2(entry, size):
        chunk = realRead2(entry, size)
        if getattr(entry, "_compress_type", None) == zipfile.ZIP_STORED:
            meter["storedReadBytes"] += len(chunk)
        return chunk

    with patch.object(zipfile.ZipExtFile, "_read2", measuredRead2):
        result = expandUploads(uploads, cap, maxArchiveEntries=maxArchiveEntries)
    return result, meter["storedReadBytes"]


class TestRuntimeZipReadAheadContract(unittest.TestCase):
    def test_zip_ext_file_min_read_size_matches_the_measured_margin(self):
        self.assertEqual(zipfile.ZipExtFile.MIN_READ_SIZE, ZIP_READ_AHEAD_MARGIN_BYTES)


class TestZipImportDeflateBudget(unittest.TestCase):
    def test_tiny_forged_declared_sizes_do_not_get_a_content_sized_decode_allowance(self):
        """Matching-prefix CRCs can make a forged tiny member look successful."""
        for declaredSize in (ZERO_DECLARED_SIZE_BYTES, TINY_DECLARED_SIZE_BYTES, TWO_DECLARED_SIZE_BYTES):
            with self.subTest(declaredSize=declaredSize):
                archive = _forgedDeflateArchive(
                    "Streaming_History_Audio.json",
                    "a" * CONTENT_CAP_BYTES,
                    declaredSize,
                )

                result, meter = _expandWithDeflateOutputMeter(
                    [_upload("export.zip", archive)],
                    CONTENT_CAP_BYTES,
                    maxArchiveEntries=1,
                )

                self.assertFalse(result.exceededCap)
                self.assertEqual(result.unreadableCount, 0)
                self.assertEqual(result.contents, ["a" * declaredSize])
                self.assertLessEqual(
                    meter["deflateOutputBytes"],
                    ZIP_READ_AHEAD_MARGIN_BYTES,
                )

    def test_honest_small_deflate_member_decodes_without_using_the_margin(self):
        payload = "h" * HONEST_SMALL_PAYLOAD_BYTES
        archive = _zipBytes(
            {"Streaming_History_Audio.json": payload},
            compression=zipfile.ZIP_DEFLATED,
            compressionLevel=9,
        )

        result, meter = _expandWithDeflateOutputMeter(
            [_upload("export.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=1,
        )

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.unreadableCount, 0)
        self.assertEqual(result.contents, [payload])
        self.assertGreaterEqual(meter["deflateOutputBytes"], HONEST_SMALL_PAYLOAD_BYTES)
        self.assertLessEqual(
            meter["deflateOutputBytes"],
            HONEST_SMALL_PAYLOAD_BYTES + ZIP_READ_AHEAD_MARGIN_BYTES,
        )
        self.assertGreater(meter["deflateFlushCalls"], 0)

    def test_forged_declared_size_at_the_cap_uses_only_the_cap_plus_margin(self):
        payload = "c" * DOUBLE_CAP_PAYLOAD_BYTES
        archive = _forgedDeflateArchive(
            "Streaming_History_Audio.json",
            payload,
            CONTENT_CAP_BYTES,
        )

        result, meter = _expandWithDeflateOutputMeter(
            [_upload("export.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=1,
        )

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.unreadableCount, 0)
        self.assertEqual(result.contents, ["c" * CONTENT_CAP_BYTES])
        self.assertGreaterEqual(meter["deflateOutputBytes"], CONTENT_CAP_BYTES)
        self.assertLessEqual(
            meter["deflateOutputBytes"],
            CONTENT_CAP_BYTES + ZIP_READ_AHEAD_MARGIN_BYTES,
        )

    def test_repeated_tiny_forged_entries_are_bounded_by_the_request_entry_limit(self):
        entries = {}
        for index in range(REPEATED_FORGED_ENTRY_COUNT):
            name = f"Streaming_History_Audio_{index}.json"
            entries[name] = "a" * CONTENT_CAP_BYTES
        archive = _zipBytes(entries, compression=zipfile.ZIP_DEFLATED, compressionLevel=9)
        for name, payload in entries.items():
            archive = _patchZipInfoSize(
                archive,
                name,
                TINY_DECLARED_SIZE_BYTES,
                _crc32(payload[:TINY_DECLARED_SIZE_BYTES].encode("utf-8")),
            )

        result, meter = _expandWithDeflateOutputMeter(
            [_upload("export.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=REPEATED_FORGED_ENTRY_COUNT,
        )

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.unreadableCount, 0)
        self.assertEqual(result.contents, ["a"] * REPEATED_FORGED_ENTRY_COUNT)
        self.assertLessEqual(
            meter["deflateOutputBytes"],
            CONTENT_CAP_BYTES + REPEATED_FORGED_ENTRY_COUNT * ZIP_READ_AHEAD_MARGIN_BYTES,
        )

    def test_mixed_prefix_forged_member_still_has_a_small_decode_bound(self):
        payload = INCOMPRESSIBLE_PREFIX + COMPRESSIBLE_TAIL
        compressedPrefix = zlib.compress(INCOMPRESSIBLE_PREFIX)
        self.assertGreater(
            len(compressedPrefix),
            len(INCOMPRESSIBLE_PREFIX) * MIN_COMPRESSED_PREFIX_RATIO,
        )
        archive = _forgedDeflateArchive(
            "Streaming_History_Audio.json",
            payload,
            TINY_DECLARED_SIZE_BYTES,
        )

        result, meter = _expandWithDeflateOutputMeter(
            [_upload("export.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=1,
        )

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.unreadableCount, 0)
        self.assertEqual(result.contents, [INCOMPRESSIBLE_PREFIX_ASCII_LEAD.decode("utf-8")])
        self.assertLessEqual(
            meter["deflateOutputBytes"],
            ZIP_READ_AHEAD_MARGIN_BYTES,
        )

    def test_valid_deflate_member_exactly_at_the_cap_can_decode_the_whole_payload(self):
        archive = _zipBytes(
            {"Streaming_History_Audio.json": "x" * CONTENT_CAP_BYTES},
            compression=zipfile.ZIP_DEFLATED,
            compressionLevel=9,
        )

        result, meter = _expandWithDeflateOutputMeter(
            [_upload("export.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=1,
        )

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.contents, ["x" * CONTENT_CAP_BYTES])
        self.assertGreaterEqual(meter["deflateOutputBytes"], CONTENT_CAP_BYTES)
        self.assertLessEqual(
            meter["deflateOutputBytes"],
            CONTENT_CAP_BYTES + ZIP_READ_AHEAD_MARGIN_BYTES,
        )
        self.assertGreater(meter["deflateFlushCalls"], 0)


class TestZipImportStoredBudget(unittest.TestCase):
    def test_forged_tiny_stored_member_reads_only_the_declared_prefix_margin(self):
        archive = _forgedStoredArchive(
            "Streaming_History_Audio.json",
            "s" * CONTENT_CAP_BYTES,
            TINY_DECLARED_SIZE_BYTES,
        )

        result, storedReadBytes = _expandWithStoredReadMeter(
            [_upload("stored.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=1,
        )

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.unreadableCount, 0)
        self.assertEqual(result.contents, ["s"])
        self.assertLessEqual(storedReadBytes, ZIP_READ_AHEAD_MARGIN_BYTES)

    def test_stored_member_exactly_at_the_cap_reads_only_the_payload(self):
        archive = _zipBytes(
            {"Streaming_History_Audio.json": "s" * CONTENT_CAP_BYTES},
            compression=zipfile.ZIP_STORED,
        )

        result, storedReadBytes = _expandWithStoredReadMeter(
            [_upload("stored.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=1,
        )

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.contents, ["s" * CONTENT_CAP_BYTES])
        self.assertEqual(storedReadBytes, CONTENT_CAP_BYTES)

    def test_stored_member_over_the_cap_is_rejected_without_member_data_reads(self):
        archive = _zipBytes(
            {"Streaming_History_Audio.json": "s" * (CONTENT_CAP_BYTES + OVERSHOOT_BYTES)},
            compression=zipfile.ZIP_STORED,
        )

        result, storedReadBytes = _expandWithStoredReadMeter(
            [_upload("stored.zip", archive)],
            CONTENT_CAP_BYTES,
            maxArchiveEntries=1,
        )

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])
        self.assertEqual(storedReadBytes, 0)


class TestLooseUploadBudget(unittest.TestCase):
    def test_loose_upload_over_the_cap_uses_the_post_read_length_check(self):
        result = expandUploads([_upload("loose.json", OVER_CAP_PAYLOAD)], SMALL_LOOSE_CAP_BYTES)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])

    def test_loose_upload_exactly_at_the_cap_is_allowed(self):
        result = expandUploads([_upload("loose.json", EXACT_CAP_PAYLOAD)], SMALL_LOOSE_CAP_BYTES)

        self.assertFalse(result.exceededCap)
        self.assertEqual(result.contents, [EXACT_CAP_PAYLOAD])

    def test_two_loose_uploads_cumulatively_exceed_the_cap_and_clear_prior_content(self):
        result = expandUploads([
            _upload("first.json", FIRST_PARTIAL_PAYLOAD),
            _upload("second.json", SECOND_PARTIAL_PAYLOAD),
        ], SMALL_LOOSE_CAP_BYTES)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])

    def test_actual_byte_debit_is_shared_between_loose_uploads_and_zip_members(self):
        archive = _zipBytes(
            {"Streaming_History_Audio.json": SECOND_PARTIAL_PAYLOAD},
            compression=zipfile.ZIP_DEFLATED,
        )

        result = expandUploads([
            _upload("first.json", FIRST_PARTIAL_PAYLOAD),
            _upload("second.zip", archive),
        ], SMALL_LOOSE_CAP_BYTES)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])

    def test_loose_over_cap_after_good_zip_clears_contents_and_leaves_later_input_unread(self):
        archive = _zipBytes(
            {"Streaming_History_Audio.json": FIRST_PARTIAL_PAYLOAD},
            compression=zipfile.ZIP_DEFLATED,
        )
        laterUpload = _ReadCountingUpload("later.json", EXACT_CAP_PAYLOAD)

        result = expandUploads([
            _upload("first.zip", archive),
            _upload("second.json", SECOND_PARTIAL_PAYLOAD),
            laterUpload,
        ], SMALL_LOOSE_CAP_BYTES)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.contents, [])
        self.assertEqual(laterUpload.stream.readCount, 0)

    def test_invalid_utf8_still_spends_budget_before_the_next_upload(self):
        result = expandUploads([
            _upload("bad.json", INVALID_UTF8_PAYLOAD),
            _upload("second.json", SECOND_PARTIAL_PAYLOAD),
        ], SMALL_LOOSE_CAP_BYTES)

        self.assertTrue(result.exceededCap)
        self.assertEqual(result.unreadableCount, 1)
        self.assertEqual(result.contents, [])
