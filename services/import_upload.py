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
`maxUncompressedBytes` is the budget that replaces it - spent across every
upload in one request, and measured on bytes actually read. Never on
ZipInfo.file_size, which is a number the archive supplies about itself.
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


@dataclass
class UploadExpansion:
    """What one request's uploads amount to.

    contents        decoded file text, in the order the importer should see it
    unreadableCount history files that were dropped - loose or archived - so
                    importHistoryBatch's overwrite path knows plays went
                    missing before it deletes the range the survivors span
    exceededCap     the budget ran out; nothing is safe to import
    emptyArchive    an archive held no history at all (the wrong ZIP), which
                    is a different mistake from one that failed to decode
    """
    contents: list = field(default_factory=list)
    unreadableCount: int = 0
    exceededCap: bool = False
    emptyArchive: bool = False


class _CapExceeded(Exception):
    """A read would have spent more than the request's remaining budget."""


class _Budget:
    """The uncompressed-byte allowance for one request, spent as it is read."""

    def __init__(self, total):
        self.total = total
        self.remaining = total

    def take(self, stream, label):
        """Read from `stream`, or give up rather than exceed what is left.

        Bounded at the read itself, which is the whole point: asking a
        decompressing stream for `remaining + 1` bytes means a bomb inflates
        to the cap and stops, instead of to whatever it wanted to be."""
        chunk = stream.read(self.remaining + _OVERSHOOT)
        if len(chunk) > self.remaining:
            logger.warning("Refusing %r: this upload unpacks past the %d-byte import cap",
                           label, self.total)
            raise _CapExceeded
        self.remaining -= len(chunk)
        return chunk


def expandUploads(uploads, maxUncompressedBytes):
    """Decode `uploads` (werkzeug FileStorages) into importable history text.

    A ZIP is expanded in place; anything else is read as text as before. The
    cap applies to the total of both - a mixed upload must not be able to
    route around it by putting the bulk in whichever half is unmetered."""
    result = UploadExpansion()
    budget = _Budget(maxUncompressedBytes)
    for upload in uploads:
        try:
            if _looksLikeZip(upload.stream):
                _expandArchive(upload, budget, result)
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


def _expandArchive(upload, budget, result):
    try:
        with zipfile.ZipFile(upload.stream) as archive:
            names = sorted(name for name in archive.namelist() if _isHistoryEntry(name))
            if not names:
                result.emptyArchive = True
                return
            for name in names:
                _takeEntry(archive, name, budget, result)
    except zipfile.BadZipFile as error:
        # is_zipfile only reads the central directory, so an archive truncated
        # or corrupted anywhere before it gets this far before failing.
        result.unreadableCount += 1
        logger.warning("Skipping upload %r: not a readable ZIP archive (%s)",
                       upload.filename, error)


def _takeEntry(archive, name, budget, result):
    try:
        with archive.open(name) as entry:
            raw = budget.take(entry, name)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, OSError, EOFError) as error:
        # BadZipFile/zlib.error: a corrupt deflate stream, a local header that
        # disagrees with the directory, or a CRC that does not match what was
        # inflated - which is also how a rewritten declared size surfaces.
        # RuntimeError: the entry is encrypted. One bad entry must not drop
        # its siblings, so this mirrors the unreadable-file path below.
        result.unreadableCount += 1
        logger.warning("Skipping archive entry %r: %s", name, error)
        return
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
    """Whether an archive entry is a file the importer should be handed."""
    if name.endswith("/"):          #< a directory entry, not a file
        return False
    segments = name.split("/")      #< ZIP names always use forward slashes
    if MACOS_METADATA_DIR in segments[:-1]:
        return False
    basename = segments[-1]
    if basename.startswith(APPLEDOUBLE_PREFIX):
        return False
    return basename.lower().endswith(IMPORTABLE_SUFFIXES)
