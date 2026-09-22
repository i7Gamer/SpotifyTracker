# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turn the files uploaded to /import-history into decoded history text.

Spotify hands you a ZIP, and the JSON inside it runs roughly eight times its
compressed size - so the workflow this app used to require (unzip by hand,
then upload the loose JSON files) is also the one most likely to be refused
before it ever arrives. A proxy in front of the app has its own request-body
limit, well under this app's MAX_UPLOAD_MB on Cloudflare's lower plans, and it
answers 413 from the edge where no message of ours can reach the user.
Accepting the archive as-is sends a fraction of the bytes and drops a manual
step at the same time.

That trade moves one guard, and this module owns the replacement.
MAX_CONTENT_LENGTH bounds the REQUEST, which is exactly the thing that stops
bounding the data the moment the server is the one unpacking it: a 25 MB
archive of 10 GB of zeroes passes every request-level check there is.
`maxUncompressedBytes` is the budget that replaces it. Loose uploads are
metered by bytes actually read. ZIP members first use ZipInfo.file_size only as
an admission/read-bound check, then debit the bytes the stdlib returns.
"""
import logging
import zipfile
import zlib
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# What the import pipeline can parse, mirroring the file input's accept= list.
# Anything else in a Spotify export - the read-me PDF, the account-data extras -
# is skipped rather than failed: it was never history, so it is not a loss.
IMPORTABLE_SUFFIXES = (".json", ".csv")
# AppleDouble shadows from an archive re-made on macOS. "__MACOSX/._Name.json"
# ends in .json and is binary, so it would otherwise be counted as history that
# failed to decode - and that count is what holds the overwrite-range delete
# back (see UploadExpansion.unreadableCount).
MACOS_METADATA_DIR = "__MACOSX"
APPLEDOUBLE_PREFIX = "._"
# Read one byte past what is left, so "filled the budget exactly" and "wants
# more than the budget" are distinguishable from the length alone.
_OVERSHOOT = 1
SUPPORTED_ARCHIVE_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


@dataclass
class UploadExpansion:
    """What one request's uploads amount to.

    contents        decoded file text, in the order the importer should see it
    unreadableCount history files with invalid UTF-8 payloads that were
                    dropped - loose or archived - so
                    importHistoryBatch's overwrite path knows plays went
                    missing before it deletes the range the survivors span
    exceededCap     the budget ran out; nothing is safe to import
    emptyArchive    an archive held no history at all (the wrong ZIP), which
                    is a different mistake from one that failed to decode
    tooManyEntries  the request held more archive entries than the caller
                    allows; the byte budget cannot see this, because empty
                    entries cost nothing to store and plenty to parse
    unsupportedCompression
                    a history member used a ZIP compression method this route
                    does not accept; nothing is safe to import from the request
    unreadableArchive
                    a detected ZIP could not be opened, or a history member
                    failed while being read; invalid UTF-8 payloads still use
                    unreadableCount instead
    """
    contents: list = field(default_factory=list)
    unreadableCount: int = 0
    exceededCap: bool = False
    emptyArchive: bool = False
    tooManyEntries: bool = False
    unsupportedCompression: bool = False
    unreadableArchive: bool = False


class _CapExceeded(Exception):
    """The request would exceed its remaining uncompressed-content budget."""


class _TooManyEntries(Exception):
    """The request declares more archive members than the caller allows."""


class _UnsupportedCompression(Exception):
    """A history member uses a compression method outside the accepted set."""


class _UnreadableArchive(Exception):
    """A ZIP or one of its history members failed while being read."""


class _Budget:
    """The uncompressed-byte allowance for one request, spent as it is read."""

    def __init__(self, total):
        self.total = total
        self.remaining = total

    def take(self, stream, label):
        """Read loose upload bytes, or give up rather than exceed what is left."""
        chunk = stream.read(self.remaining + _OVERSHOOT)
        if len(chunk) > self.remaining:
            logger.warning("Refusing %r: this upload unpacks past the %d-byte import cap",
                           label, self.total)
            raise _CapExceeded
        self.remaining -= len(chunk)
        return chunk

    def archiveEntryReadLimit(self, declaredSize, label):
        """Return one ZIP member's read size after declared-size preflight.

        ZipExtFile clips returned data to ZipInfo.file_size before it updates
        the CRC. That means this preflight is the archive member's cap decision;
        the later byte debit still uses the actual bytes returned. It is not an
        integrity proof for a forged undersized member."""
        if declaredSize > self.remaining:
            logger.warning("Refusing %r: declared %d bytes over the remaining %d-byte import cap",
                           label, declaredSize, self.remaining)
            raise _CapExceeded
        return min(self.remaining + _OVERSHOOT, declaredSize + _OVERSHOOT)

    def takeArchiveEntry(self, stream, readLimit, label):
        """Read one ZIP member within a limit chosen before opening it."""
        chunk = stream.read(readLimit)
        if len(chunk) > self.remaining:
            logger.warning("Refusing %r: this upload unpacks past the %d-byte import cap",
                           label, self.total)
            raise _CapExceeded
        self.remaining -= len(chunk)
        return chunk


def expandUploads(uploads, maxUncompressedBytes, maxArchiveEntries=None):
    """Decode `uploads` (werkzeug FileStorages) into importable history text.

    A ZIP is expanded in place; anything else is read as text as before. The
    byte cap applies to the total of both - a mixed upload must not be able to
    route around it by putting the bulk in whichever half is unmetered.

    `maxArchiveEntries` is a second, independent ceiling, because the byte
    budget cannot see this one: an archive of 50,000 zero-byte entries spends
    0% of it and still costs seconds of a worker thread (measured). None means
    no ceiling; the policy number lives in config.py and the route passes it,
    so there is one place to change it."""
    result = UploadExpansion()
    budget = _Budget(maxUncompressedBytes)
    archiveEntriesSeen = 0
    for upload in uploads:
        try:
            if _looksLikeZip(upload.stream):
                archiveEntriesSeen = _expandArchive(
                    upload, budget, result, maxArchiveEntries, archiveEntriesSeen)
            else:
                _decodeInto(budget.take(upload.stream, upload.filename),
                            upload.filename, result)
        except _CapExceeded:
            # All or nothing. A partial import here would be indistinguishable
            # from a complete one downstream, and in overwrite mode the
            # covered-range delete would then span data that never arrived.
            result.exceededCap = True
            result.contents = []
            return result
        except _TooManyEntries:
            result.tooManyEntries = True
            result.contents = []
            return result
        except _UnsupportedCompression:
            result.unsupportedCompression = True
            result.contents = []
            return result
        except _UnreadableArchive:
            result.unreadableArchive = True
            result.contents = []
            return result
    return result


def _looksLikeZip(stream):
    """Whether this upload is an archive, by content rather than by name.

    A .zip saved as .json is still an archive, and the old path handed its
    binary straight to .decode(). is_zipfile seeks to find the central
    directory at the end of the file, so the position is reset either way."""
    try:
        isZip = zipfile.is_zipfile(stream)
    except (OSError, ValueError):
        isZip = False
    stream.seek(0)
    return isZip


def _expandArchive(upload, budget, result, maxArchiveEntries, archiveEntriesSeen):
    try:
        with zipfile.ZipFile(upload.stream) as archive:
            # infolist(), never namelist(): a name is not an identity here.
            # archive.open(<str>) resolves through zipfile's NameToInfo dict,
            # which keeps only the LAST record for a repeated name - so two
            # entries sharing one name were read as the same entry twice, and
            # the first one's content vanished with unreadableCount still 0.
            # The ZipInfo records are distinct even when their names are not.
            entries = archive.infolist()
            archiveEntriesSeen += len(entries)
            if maxArchiveEntries is not None and archiveEntriesSeen > maxArchiveEntries:
                logger.warning("Refusing %r: request has %d archive entries, over the %d the importer will open",
                               upload.filename, archiveEntriesSeen, maxArchiveEntries)
                raise _TooManyEntries
            wanted = sorted((entry for entry in entries if _isHistoryEntry(entry.filename)),
                            key=lambda entry: entry.filename)
            if not wanted:
                result.emptyArchive = True
                return archiveEntriesSeen
            for entry in wanted:
                _takeEntry(archive, entry, budget, result)
            return archiveEntriesSeen
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError,
            OSError, EOFError) as error:
        # is_zipfile only reads the central directory, so an archive truncated
        # or corrupted anywhere before it gets this far before failing.
        logger.warning("Rejecting upload %r: not a safely readable ZIP archive (%s)",
                       upload.filename, error)
        raise _UnreadableArchive
    except UnicodeDecodeError as error:
        logger.warning("Rejecting upload %r: ZIP filename metadata is not readable (%s)",
                       upload.filename, error)
        raise _UnreadableArchive


def _takeEntry(archive, info, budget, result):
    name = info.filename
    if info.compress_type not in SUPPORTED_ARCHIVE_COMPRESSION:
        logger.warning("Rejecting archive entry %r: unsupported ZIP compression method %s",
                       name, info.compress_type)
        raise _UnsupportedCompression
    readLimit = budget.archiveEntryReadLimit(info.file_size, name)
    try:
        with archive.open(info) as entry:
            raw = budget.takeArchiveEntry(entry, readLimit, name)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError,
            OSError, EOFError, UnicodeDecodeError) as error:
        # BadZipFile/zlib.error: a corrupt deflate stream, a local header that
        # disagrees with the directory, or a CRC that does not match what was
        # inflated - which is also how a rewritten declared size surfaces.
        # RuntimeError: the entry is encrypted. NotImplementedError: a
        # compression method this Python build has no decompressor for - it
        # SUBCLASSES RuntimeError, so it was already caught, but two reviewers
        # read this tuple and disagreed about that, which is reason enough to
        # spell it out. UnicodeDecodeError here is ZIP filename metadata, not
        # the payload's UTF-8 decode below.
        logger.warning("Rejecting archive entry %r: %s", name, error)
        raise _UnreadableArchive
    _decodeInto(raw, name, result)


def _decodeInto(raw, label, result):
    try:
        result.contents.append(raw.decode("utf-8"))
    except UnicodeDecodeError:
        # Mirrors AutoImporter._handleImport's per-file resilience: one
        # unreadable file must not drop every other file in the same upload.
        # COUNTED, not merely logged - the drop happens above every guard
        # importHistoryBatch has for unreadable input, so the batch cannot see
        # it, and in overwrite mode it would delete the covered range of the
        # survivors with nothing left to re-insert this file's plays.
        result.unreadableCount += 1
        logger.warning("Skipping %r: not valid UTF-8 text", label)


def _isHistoryEntry(name):
    """Whether an archive entry is a file the importer should be handed.

    Directory entries need no case of their own, and an explicit one here was
    dead code: a ZIP spells a directory with a trailing slash, which leaves an
    empty basename that matches no suffix. The test still pins the outcome."""
    segments = name.split("/")      #< ZIP names always use forward slashes
    if MACOS_METADATA_DIR in segments[:-1]:
        return False
    basename = segments[-1]
    if basename.startswith(APPLEDOUBLE_PREFIX):
        return False
    return basename.lower().endswith(IMPORTABLE_SUFFIXES)
