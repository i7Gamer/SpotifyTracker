# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import TYPE_CHECKING

# _ImportRunState is used in the hints below and constructed at runtime via
# _dbmod. Unlike this package's other annotation-only names, it cannot become a
# real import: it lives in Database.database, which imports the mixin defined
# here, so a top-level import would cycle. That means static tooling resolves
# the hint but typing.get_type_hints() still cannot - the accepted cost of
# TYPE_CHECKING, and the reason the leaf names elsewhere are imported normally.
if TYPE_CHECKING:   # pragma: no cover
    from Database.database import _ImportRunState

import hashlib
import sqlite3

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod
#< a direct import, unlike _dbmod above: Database.utils imports nothing but the
#  standard library, so it cannot take part in the cycle _dbmod exists to break
from Database.utils import flaskDebugEnabled
from Database.metadata_repair import WrappedRepairResult

# Drop counters (see StreamingHistoryImporter._processPlay) whose plays WOULD
# import on a later attempt: the lookup failed, the data didn't. An overwrite
# import must not delete a range it can't fully rebuild, so any of these aborts
# the batch; an append import still lands what it can, but must not mark the
# file's hash as imported, or the re-import that would retry them is refused
# (see _applyImportData). "droppedNoTrack" is deliberately absent - podcast/audiobook rows
# carry no track name and can never resolve, so treating them as retryable
# would make overwrite import impossible for most real exports.
RETRYABLE_DROP_STAT_KEYS = ("droppedTransient", "droppedUnexpected")


def _exportContentHash(content) -> str:
    """The already-imported gate's identity for one export file's content -
    what markFileImported stores and isFileImported checks. One
    implementation on purpose: it used to be copy-pasted between the apply
    path (writer) and the batch loop (reader), where a future change to the
    hashing rule in one would silently desync the gate. Non-str content (a
    mocked payload) hashes its str() form, as both inline copies always
    did."""
    contentBytes = content.encode("utf-8") if isinstance(content, str) else str(content).encode("utf-8")
    return hashlib.sha256(contentBytes).hexdigest()


def _minTimestamp(current, *candidates):
    """The smallest of `current` and `candidates`, where `current` may be None
    ("nothing seen yet") and the candidates come straight off import entries or
    database rows, so they can be str or int as well as float.

    Its own function because the apply loop runs once per play and a six-figure
    export makes the alternative - bucketing each row's year as it goes - a
    six-figure pile of timezone conversions for one number."""
    smallest = current
    for candidate in candidates:
        if candidate is None:
            continue
        value = float(candidate)
        if smallest is None or value < smallest:
            smallest = value
    return smallest

# Entries that could not be READ (see StreamingHistoryImporter._parseHistory).
# They abort an overwrite for the same reason as the keys above - the covered
# range is computed from the same parse, so the OTHER entries still mark the
# year covered and the delete would take out a play nothing re-inserts - but the
# advice differs: re-running changes nothing, the file itself is unreadable.
# "droppedNegativeTime" is deliberately absent, matching "droppedNoTrack": it is
# a long-standing sanity filter, and aborting on it would block exports that
# have always been importable.
UNREADABLE_DROP_STAT_KEYS = ("droppedMalformed",)


class ImportFailureCategory(ValueError):
    """Base for expected import-file failures with fixed public categories."""


class UnsupportedImportFormatError(ImportFailureCategory):
    """The upload is not a supported Spotify JSON or Musicolet CSV export."""


class UnreadableImportEntriesError(ImportFailureCategory):
    """The upload matched a format but none of its entries could be parsed."""


class AmbiguousImportMatchError(ImportFailureCategory):
    """An overwrite entry matched several existing rows and must abort."""


# Raised when an overwrite batch reaches an entry whose near-time lookup finds
# several candidate rows (see _applyImportData). Keep this diagnostic static
# because the direct import progress path can display it. Batch summaries use
# the exception type, never this wording or interpolated upload contents.
AMBIGUOUS_MATCH_ABORT_MESSAGE = (
    "an uploaded entry matches several existing plays of the same track, so there is no single "
    "row it can safely correct and the play it describes cannot be restored")


def _classifyImportFailureReason(e: Exception) -> str:
    """One of a small, FIXED set of user-facing phrases describing why a file
    in an import batch failed - never str(e)/parseError(e) itself, which can
    carry a filename, a track id, or other uploaded content (see
    AMBIGUOUS_MATCH_ABORT_MESSAGE's comment: the same reason that message is
    itself a constant). Consumed by the batch loop's per-file "continuing"
    progress line and its final summary (UT-17) - the server log at :684
    keeps the full parseError(e) diagnostic unchanged, so nothing is lost for
    debugging, only kept out of the browser.

    Keyed on local exception types raised by the service boundary, never on
    diagnostic wording. Only expected file-level failures reachable from
    _stageImportData/_applyImportData via the batch loop's except are named
    here; anything else - including unrelated ValueError, MusicoletExpansionTooLargeError,
    and any other Exception subtype - falls to the generic default."""
    if isinstance(e, AmbiguousImportMatchError):
        return "an ambiguous match aborted this file"
    if isinstance(e, UnreadableImportEntriesError):
        return "the file could not be read"
    if isinstance(e, UnsupportedImportFormatError):
        return "unrecognised export format"
    return "an unexpected error"


def _summarizeFailureReasons(reasons: list[str]) -> str:
    """'reason (Nx), reason (Nx)' for the batch summary line - counts each
    classified reason, in first-seen order (stable across runs, unlike
    Counter.most_common's tie-break)."""
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return ", ".join(f"{reason} ({count}x)" for reason, count in counts.items())


# How far apart two skip rows for the same track may be and still be treated as
# one physical event. Sized for the recording sources disagreeing about what
# played_at means (start vs end of a sub-5s play) plus clock drift - NOT for the
# real-play path's much wider "duration + 60s" window, which would swallow a
# genuine second skip of the same track later in a session.
SKIP_NEAR_TIME_TOLERANCE_SECONDS = 10


class ImportMixin:
    """History import / reconciliation (append*, importHistory*, overwrite range), mixed into Database."""

    # ---- writing plays ---------------------------------------------------------------

    def _beginMetadataWrite(self):
        """Reserve the writer before reading catalog state used by repair decisions."""
        conn = self.repo.connection()
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        return conn

    def _logCommittedMetadataRepair(self, source: str, result: WrappedRepairResult | None) -> None:
        if result is not None:
            _dbmod.logger.info(
                "Metadata repair committed: source=%s user=%s repaired=%d repair_deleted=%d "
                "history_deleted=%d mode=%s reason=%s",
                source, self.user, result.repaired, result.repairDeleted, result.historyDeleted,
                result.mode, result.reason or "within_limits")

    def appendMetadata(self, meta: dict, created_reason: str | None = None) -> bool:
        self.saveImagesFromTrack(meta)
        entry, track = self._splitEntryAndTrack(meta)
        # These two are ONE transaction that this method owns: neither commits
        # (see their docstrings), so a failure between them - or partway through
        # upsertTrack's five write statements, which include a DELETE of
        # track_artists followed by re-INSERTing them - leaves real rows staged
        # on this thread's long-lived connection. Staged, they hold the WAL
        # write lock, and then whatever commits next persists them. The listener
        # is what makes that reachable rather than theoretical: it catches per
        # item and moves to the next one (see _addToDatabaseFromListener), so
        # item N's half-written catalog rows were committed by item N+1.
        #
        # updatePlaylists stays OUTSIDE this: it commits its own write (via
        # upsertPlaylistName's `with conn:`) and runs only once the play is
        # durably committed, so it has no part in this transaction.
        try:
            conn = self._beginMetadataWrite()
            impact = self.repo.upsertTrack(track, created_reason=created_reason)
            # Classify against the current threshold + the track's duration (percent
            # mode needs it); a sub-threshold event now lands as is_skip=1 in plays
            # rather than in a separate table.
            is_skip = self.repo.computeIsSkip(entry["timePlayed"], track.get("duration"))
            was_inserted = self.repo.insertPlay(self.user, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom"),
                                  created_reason=created_reason, is_skip=is_skip)
            repairResult = self.repo._invalidateWrappedForRepairs(conn, [impact]) if impact is not None else None
            self.repo.commit()
        except Exception:
            self.repo.rollbackQuietly()
            raise
        self._logCommittedMetadataRepair("live", repairResult)
        self.updatePlaylists(entry.get("playedFrom"))
        return was_inserted

    def appendTrackData(self, timestamp, track, timePlayed, context=None, source="listener"):
        formatted_track = _dbmod.Client.formatTrack(track, timestamp, timePlayed, context=context)
        track_id = track.get("id", "unknown")
        track_name = track.get("name", "unknown")

        if source == self.WEB_API_BACKFILL_SOURCE:
            # Wide, defense-in-depth guard: skip if this exact track already has a
            # play within (duration + 60s) of this one. Deliberately NOT applied to
            # the live listener's own inserts (source == "listener") - the listener
            # is the primary, trusted source, and a genuine short-track replay
            # within this window is normal listening behavior that must not be
            # silently dropped. Backfill is a catch-up mechanism and should be
            # conservative about re-adding something a trusted source may already
            # have captured - this window is symmetric so it catches a duplicate
            # regardless of whether Spotify reported this entry's played_at as a
            # start or end time (see _checkWebApiBackfill for why that can't be
            # assumed one way or the other).
            #
            # The listener-end tolerance covers the case the duration window
            # cannot: a mid-track pause stretches start-to-end by an unbounded
            # amount, but a listener row's created_at is its observed end (the
            # listener inserts at the track-change moment), so an entry whose
            # played_at sits at that stamp is the same listen however long the
            # pause was (see BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS).
            #
            # Both of those only ever look at real plays, and this source can
            # never produce one that matches a SKIP: with no ms_played from the
            # Web API the row below stamps the track's whole duration, so it is
            # is_skip=0 by construction and a skipped listen was invisible to
            # the guard entirely (2026-08-14: a 3.6s skip came back as a full
            # 220s play recorded 15s away). The skip tolerance is that third
            # arm, and is tight for the reason its constant explains.
            durationSeconds = (track.get("duration_ms", 0) or 0) // 1000
            tolerance = durationSeconds + self.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
            if self.repo.hasPlayNearTime(self.user, track_id, formatted_track["playedAt"], tolerance,
                                         listenerEndToleranceSeconds=self.BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS,
                                         skipToleranceSeconds=self.BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS):
                if flaskDebugEnabled():
                    _dbmod.logger.info(
                        "Skipping backfilled play for track %s (%s): an existing play already exists "
                        "within %ds (duration+60s) of played_at=%s",
                        track_id, track_name, tolerance, formatted_track["playedAt"],
                    )
                return False

        created_reason = f"{source}_play (user: {self.user})"
        was_inserted = self.appendMetadata(formatted_track, created_reason=created_reason)
        if was_inserted:
            _dbmod.logger.info(
                "Recording play for user %s: track=%s (%s), timestamp=%s, duration=%dms, source=%s",
                self.user, track_id, track_name, timestamp, timePlayed, source
            )
        return was_inserted

    def importHistory(self, exportedHistory, progressPrefix: str = "", isFinalFile: bool = True, hasPriorError: bool = False, track_file_hash: bool = False,
                      runState: _ImportRunState | None = None):
        """Import one export file. Serialized per user via _importLock (see
        its comment in __init__); the actual work is in _importHistoryLocked."""
        with self._importLock:
            return self._importHistoryLocked(exportedHistory, progressPrefix, isFinalFile, hasPriorError,
                                             track_file_hash, runState)

    def _importHistoryLocked(self, exportedHistory, progressPrefix: str = "", isFinalFile: bool = True, hasPriorError: bool = False, track_file_hash: bool = False,
                             runState: _ImportRunState | None = None):
        """Import one export file: Phase 1 stages it (parse + Spotify metadata
        fetch) holding NO write transaction, then Phase 2 applies the staged
        rows. Splitting them keeps the network-bound fetch out of the write
        transaction - the atomic overwrite batch depends on this to avoid
        holding SQLite's single write lock across Spotify lookups.

        Always deferCommit=False on both phase calls: this path owns one
        transaction per file, so writeProgress's self-commits are safe here.
        The atomic overwrite batch never comes through this method - it
        drives _stageImportData/_applyImportData directly with
        deferCommit=True, handing the APPLY phase a no-op progress reporter
        because a self-commit there would flush a prior file's staged writes
        mid-batch (see _importHistoryBatchOverwriteLocked). A deferCommit parameter used to
        exist here for that batch and was never called with it; had it been,
        the staging-failure rollback below would have discarded a prior
        file's staged writes in a transaction this method does not own."""
        importer = self._withCookiesFile(lambda cookiesFile: _dbmod.Importer(cookiesFile=cookiesFile, email=self.email))
        try:
            if runState is None:
                runState = _dbmod._ImportRunState()

            # Snapshot for the staging-failure path; the apply path restores the
            # same run-state fields itself (see _applyImportData's except).
            claimedRowIdsBefore = set(runState.claimedRowIds)
            insertedPlayKeysBefore = set(runState.insertedPlayKeys)
            pendingRepairImpactsBefore = list(runState.pendingRepairImpacts)
            correctedYearsBefore = set(runState.correctedYears)
            try:
                staged = self._stageImportData(importer, exportedHistory, progressPrefix,
                                               hasPriorError, self.writeProgress, runState, deferCommit=False)
            except Exception as e:
                # Staging (parse / Spotify metadata fetch) failed before any DB
                # write. Report it and restore the batch-shared run state, matching
                # the apply path's failure handling.
                self.repo.rollbackQuietly()
                runState.claimedRowIds = claimedRowIdsBefore
                runState.insertedPlayKeys = insertedPlayKeysBefore
                runState.pendingRepairImpacts = pendingRepairImpactsBefore
                runState.correctedYears = correctedYearsBefore
                runState.committedRepairResult = None
                self.writeProgress("failed", 0, 0, f"{progressPrefix}Import failed: {_dbmod.parseError(e)}", error=True)
                raise
            if staged is None:
                return
            stagedTracks, stagedPlays, total, importStats = staged
            self._applyImportData(stagedTracks, stagedPlays, importStats, total, exportedHistory,
                                  progressPrefix, isFinalFile, hasPriorError, track_file_hash,
                                  runState, False, self.writeProgress)
        finally:
            # Each Importer logs in with its own fresh TLS session (see
            # Database/Spotify/client.py) - release it or every import leaks
            # one, atexit-pinned, until process exit.
            importer.sp.close()

    def _stageImportData(self, importer, exportedHistory, progressPrefix, hasPriorError,
                         reportProgress, runState, deferCommit, knownTracks=None):
        """Phase 1 of an import: parse the export and fetch any missing track
        metadata from Spotify. Does NO database writes and holds NO write
        transaction, so this network-bound work cannot block other writers.
        Returns (stagedTracks, stagedPlays, total, importStats), or None when
        the file parsed to nothing; raises ValueError on a corrupt file.

        knownTracks seeds the importer's "don't re-fetch what we already have"
        cache; the overwrite batch passes a shared, growing list so a track one
        file fetched isn't fetched again for a later file (the per-file
        transaction that used to make that happen implicitly is gone)."""
        parsedHistory, exportType = importer._convertToList(exportedHistory)
        if exportType == "None":
            # Unrecognized content (corrupt JSON, a file read mid-copy, the
            # wrong file entirely) must fail loudly: returning silently here
            # used to make AutoImporter move never-imported files to DONE/ as
            # successes and the web UI report the import as complete. The caller
            # (orchestrator / overwrite batch) reports the failure progress.
            raise UnsupportedImportFormatError(
                "Unrecognized or corrupt export file - expected a Spotify JSON export or Musicolet CSV backup")
        if not parsedHistory:
            return None

        # Plays, not rows: one Musicolet CSV row carries a play COUNT and
        # expands to that many plays, so the loop below - which counts what the
        # importer yields - reported against a denominator it could exceed
        # ("Fetched 20000 of 800"). Every other format expands 1:1 and this is
        # still len(parsedHistory) there.
        total = importer.expectedEntryCount(parsedHistory, exportType)
        reportProgress("running", 0, total, f"{progressPrefix}Starting import", error=hasPriorError)

        def progressCallback(status, current, totalSteps, message):
            reportProgress(status, current, totalSteps, f"{progressPrefix}{message}", error=hasPriorError)

        # Staged in memory; written to the DB only in Phase 2 (_applyImportData)
        # once the whole file's metadata has been fetched.
        stagedTracks: dict[str, dict] = {}
        stagedPlays: list[dict] = []
        importStats: dict = {}
        if knownTracks is None:
            knownTracks = self.repo.getAllTracks()
        for index, meta in enumerate(
            importer.importHistory(parsedHistory, knownTracks, exportType, progressCallback=progressCallback,
                                   stats=importStats),
            start=1,
        ):
            entry, track = self._splitEntryAndTrack(meta)
            stagedTracks[track["id"]] = track
            stagedPlays.append(entry)
            if deferCommit:
                # saveImagesFromTrack -> tryClaimImageDownload self-commits (the
                # INVARIANT the overwrite batch's apply phase protects; harmless
                # here, where no transaction is open - reportProgress is the
                # real writeProgress on this path for that reason) - the batch
                # claims images only after its final commit, see
                # _importHistoryBatchOverwriteLocked.
                runState.pendingImageTracks[track["id"]] = track
            else:
                # Safe here: Phase 2 hasn't run, so nothing is staged on the
                # connection for this self-committing call to flush.
                self.saveImagesFromTrack(track)

            if index % self.PROGRESS_UPDATE_INTERVAL == 0 or index == total:
                #< "Fetched", not "Imported": nothing is written until Phase 2,
                #  and in an overwrite batch that is after EVERY file's fetch
                reportProgress("running", index, total, f"{progressPrefix}Fetched {index} of {total}")

        # A file nothing could be read from fails as loudly as an unrecognized
        # one, and for the same reason. _convertToList types a file from its
        # FIRST row alone, so a JSON list whose rows carry `ts` but not the
        # master_metadata_* keys is typed as an extended export and then fails
        # on every row - which used to import as a silent success ("0 tracks
        # imported", progress complete, AutoImporter filing it under DONE/).
        #
        # Only when EVERY entry was unreadable: a file that is partly readable
        # imports what it can, and the droppedMalformed counter is what stops an
        # overwrite from deleting the rest (see UNREADABLE_DROP_STAT_KEYS).
        # Podcast-only files are unaffected - episode rows parse, and are
        # dropped a layer lower as droppedNoTrack.
        #
        # Format-neutral wording: this used to be reachable only from the two
        # Spotify JSON paths, so it named Spotify and told the user to
        # re-download from there. Musicolet CSV rows are counted now too, and a
        # shifted-column CSV that never came from Spotify would have been given
        # advice about a Spotify export it does not have.
        entriesSeen = importStats.get("entriesSeen", 0)
        if entriesSeen and importStats.get("droppedMalformed", 0) == entriesSeen:
            raise UnreadableImportEntriesError(
                f"None of the {entriesSeen} entries in this file could be read - it matches a known "
                "export format but carries none of the expected fields. Re-export it and upload the "
                "streaming-history file itself, not another one."
            )

        return stagedTracks, stagedPlays, total, importStats

    def _claimNearbySkip(self, track_id, played_at, runState) -> dict | None:
        """Claim the nearest existing skip row (is_skip=1) of this track
        within SKIP_NEAR_TIME_TOLERANCE_SECONDS of played_at, if there is
        one - the one physical event the live listener already recorded
        for this entry. plays' UNIQUE constraint alone wasn't enough: the
        two sources' played_at can differ by seconds (Spotify's
        start-vs-end ambiguity), so one skip landed twice and inflated
        skip counts.

        Returns the claimed row when one exists (the entry is already
        recorded; insert nothing), or None when there is nothing to claim.
        Shared by the two callers that store an entry as a skip: the
        sub-floor skip path (_applySkipEntry) and the real-play path once
        it has found nothing to correct - see the dispatch comment in
        _applyImportData for why they are two paths at all."""
        nearbySkips = [
            skip for skip in self.repo.getSkipsNearTime(
                self.user, track_id, played_at, SKIP_NEAR_TIME_TOLERANCE_SECONDS)
            # Rows this run wrote belong to other entries of the same
            # export - two genuinely distinct skips must not collapse
            # into one (same rule as the real-play path's near-time match).
            if not runState.isOwnWrite(track_id, skip)
        ]
        if not nearbySkips:
            return None
        # Claim it, exactly as the real-play path does. An
        # unclaimed match stayed a candidate for every LATER
        # entry too, so a second genuine skip inside the same
        # 10s window matched the same row and was dropped as a
        # duplicate - silently, and counted by nothing. Nearest
        # first, so which entry pairs with which row does not
        # depend on the order the query returned them in.
        closest = min(nearbySkips, key=lambda skip: abs(skip["played_at"] - played_at))
        runState.claimedRowIds.add(closest["id"])
        return closest

    def _applySkipEntry(self, track_id, played_at, time_played, extras, runState,
                        playedFrom=None) -> tuple[int, int]:
        """Sub-5s events (entry["isSkip"], the fixed import floor) never
        claim or correct a real play row - they match only against
        other skips (see _claimNearbySkip for why matching against
        skips is needed at all).

        The apply loop's per-entry dispatch calls this and always
        `continue`s afterward - this branch fully owns the entry once
        isSkip fires. Returns (skipsSaved, enriched) so the caller's
        skipsSavedCount only counts genuine new rows while metadata enrichment
        remains visible in the import summary."""
        extras = extras or {}
        extrasValues = [extras.get(column) for column in _dbmod.BEHAVIORAL_COLUMNS]
        claimedSkip = self._claimNearbySkip(track_id, played_at, runState)
        if claimedSkip is not None:
            enriched = self._enrichMatchedPlay(claimedSkip, playedFrom, extras, extrasValues)
            return 0, int(enriched)
        #< playedFrom passed through exactly as the real-play insert passes
        #  it: this path used to drop the playlist context of every sub-floor
        #  skip on the way in
        if self.repo.insertPlay(self.user, track_id, played_at, time_played, playedFrom,
                                created_reason=f"history_import (user: {self.user})",
                                extras=extras, is_skip=1):
            runState.insertedPlayKeys.add((track_id, played_at))
            return 1, 0
        return 0, 0

    def _nearTimeMatches(self, track_id, played_at, durationSeconds, runState):
        """Existing play rows within (duration + 60s) tolerance of one import
        entry - same logic as API backfill to handle potential overlap with
        backfilled data where Spotify's played_at can be ambiguous (start or
        end time)."""
        tolerance = durationSeconds + self.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
        raw_matches = self.repo.getPlaysNearTime(self.user, track_id, played_at, tolerance)
        matches = []
        for m in raw_matches:
            # Rows this run already wrote belong to other import entries and
            # are never candidates - otherwise a replay would "correct" the
            # skip play inserted moments earlier instead of being recorded
            # itself (see _ImportRunState).
            if runState.isOwnWrite(track_id, m):
                continue
            db_played_at = m["played_at"]
            diff_start = abs(db_played_at - played_at)
            diff_end = abs(db_played_at - (played_at + durationSeconds))
            if diff_start <= self.IMPORT_MATCH_START_WINDOW_SECONDS or diff_end <= self.IMPORT_MATCH_END_WINDOW_SECONDS:
                matches.append(m)
        return matches

    def _enrichMatchedPlay(self, existing_play, playedFrom, extras, extrasValues) -> bool:
        """Apply supplied context/behavioral values and report actual changes."""
        contextDiffers = (
            playedFrom is not None and playedFrom != existing_play.get("played_from")
        )
        extrasDiffers = any(
            extras.get(column) is not None and extras.get(column) != existing_play.get(column)
            for column in _dbmod.BEHAVIORAL_COLUMNS
        )
        if not (contextDiffers or extrasDiffers):
            return False
        self.repo.enrichPlayBehavioralColumns(existing_play["id"], playedFrom, extrasValues)
        return True

    def _reconcileSingleMatch(self, existing_play, track_id, played_at, time_played, isSkip,
                              playedFrom, extras, extrasValues):
        """The apply loop's exactly-one-match arm: safe to update the
        existing row in place rather than insert a duplicate. Returns
        (updated, enriched, correctedYears, earliestTouchedTimestamp) -
        correctedYears is a set (0 or 2 entries) and earliestTouchedTimestamp
        is None unless updated, so the caller can fold either straight into
        its running totals with set union / _minTimestamp. Never raises: a
        played_at collision the near-time matcher couldn't see (see the
        sqlite3.IntegrityError branch) is logged and reported as unchanged,
        matching the original inline branch's `continue`."""
        data_differs = (
            existing_play["time_played"] != time_played or
            existing_play["played_at"] != played_at or
            existing_play["is_skip"] != isSkip
        )
        if data_differs:
            # Update both fields with imported data (more accurate source).
            # A corrected time_played can cross the skip threshold, so
            # is_skip is recomputed alongside it.
            corrected_is_skip = isSkip
            try:
                self.repo.correctPlay(existing_play["id"], played_at, time_played,
                                       corrected_is_skip, playedFrom, extrasValues)
            except sqlite3.IntegrityError:
                # Correcting played_at would collide with an existing
                # (username, track_id, played_at) row the near-time
                # matcher can't see - it filters is_skip=0, so a merged
                # skip sitting at exactly this timestamp is invisible.
                # Leave the row uncorrected rather than fail the whole
                # file/batch on the UNIQUE violation.
                _dbmod.logger.info(
                    "Skipping played_at correction for track %s: target timestamp already recorded",
                    track_id,
                )
                return False, False, set(), None
            changes = []
            if int(existing_play["played_at"]) != int(played_at):
                changes.append(f"played_at corrected from {int(existing_play['played_at'])} to {int(played_at)}")
            if existing_play["time_played"] != time_played:
                changes.append(f"time_played corrected from {existing_play['time_played']}ms to {time_played}ms")
            if existing_play["is_skip"] != isSkip:
                changes.append(f"is_skip corrected from {existing_play['is_skip']} to {isSkip}")

            _dbmod.logger.info(
                "Updated import play for track %s: %s",
                track_id, ", ".join(changes)
            )
            # A correction can move a play without changing its
            # year's play count or max timestamp - invisible to
            # _wrappedCacheNeedsRecalc, so those years' cached
            # Wrapped is dropped after commit (see the caller's
            # touchedYears).
            correctedYears = {
                _dbmod.convertToDatetime(existing_play["played_at"], tz=self.tz).year,
                _dbmod.convertToDatetime(played_at, tz=self.tz).year,
            }
            earliestTouchedTimestamp = _minTimestamp(None, existing_play["played_at"], played_at)
            return True, False, correctedYears, earliestTouchedTimestamp
        elif self._enrichMatchedPlay(existing_play, playedFrom, extras, extrasValues):
            # Same play, but this import carries behavioral
            # metadata the row lacks - backfill it in place.
            return False, True, set(), None
        else:
            # Data matches - skip, no update needed
            if flaskDebugEnabled():
                _dbmod.logger.info(
                    "Skipping import play for track %s: duplicate found with identical data",
                    track_id,
                )
            return False, False, set(), None

    def _applyImportData(self, stagedTracks, stagedPlays, importStats, total, exportedHistory,
                         progressPrefix, isFinalFile, hasPriorError, track_file_hash, runState,
                         deferCommit, reportProgress):
        """Phase 2 of an import: write the staged tracks/plays to the database.
        Pure DB work (no network), so the write transaction it opens - and, in a
        deferCommit overwrite batch, the covered-range delete sharing that
        transaction - is held only as long as the local writes take. The caller
        must invoke this only after Phase 1 staging has completed."""
        index = 0   #< advanced per applied play below, so the failure path can say how far it got
        # Rolled-back writes must not stay claimed in a batch-shared run state
        claimedRowIdsBefore = set(runState.claimedRowIds)
        insertedPlayKeysBefore = set(runState.insertedPlayKeys)
        pendingRepairImpactsBefore = list(runState.pendingRepairImpacts)
        correctedYearsBefore = set(runState.correctedYears)
        if not deferCommit:
            runState.committedRepairResult = None
        try:
            conn = self._beginMetadataWrite()
            for track in stagedTracks.values():
                impact = self.repo.upsertTrack(track, created_reason=f"history_import (user: {self.user})")
                if impact is not None:
                    runState.pendingRepairImpacts.append(impact)

            insertedCount = 0
            updatedCount = 0
            enrichedCount = 0
            skipsSavedCount = 0
            correctedYears = set()
            # The oldest play this batch WRITES, corrected or inserted. Kept as
            # a timestamp and bucketed once at the end rather than per row:
            # this loop runs per play, and a large export is six figures of
            # them. It is what decides how far back the Wrapped invalidation
            # has to reach - see _invalidateWrappedFromEarliestOf.
            earliestTouchedTimestamp = None
            # Fetch both classifier settings once for the whole batch so each
            # row's is_skip is computed without a per-row settings read.
            skipThreshold = self.repo.getSkipThreshold()
            completionPercent = self.repo.getCompletionCompletePercent()
            for index, entry in enumerate(stagedPlays, start=1):
                track_id = entry["id"]
                played_at = entry["playedAt"]
                time_played = entry["timePlayed"]
                played_from = entry.get("playedFrom")
                extras = entry.get("importExtras") or {}
                extrasValues = [extras.get(column) for column in _dbmod.BEHAVIORAL_COLUMNS]

                track = stagedTracks.get(track_id)
                #< staged tracks carry Client.formatTrack's "duration" key (ms)
                trackDurationMs = track.get("duration") if track else None
                isSkip = self.repo.computeIsSkip(
                    time_played, trackDurationMs,
                    threshold=skipThreshold, completionPercent=completionPercent)

                # The classifier has to agree, not just the floor. Since 73e1a2c
                # computeIsSkip caps its threshold at the completion boundary, so
                # a play under 5s is a COMPLETE play whenever duration x
                # completion% < 5000ms - any track shorter than 6.25s at the
                # default 80%. A 3s interlude played to its end used to be stored
                # is_skip=1 here, which both disagreed with the classifier (the
                # next recomputeSkipFlags flipped it) and hid the listener's
                # correctly-classified is_skip=0 row for the same event from the
                # dedup, so that recompute turned one event into two real plays.
                # Those fall through to the real-play path below instead, which
                # is the matcher that can see the row they need to dedup against.
                #
                # The converse is deliberately NOT routed here: an event ABOVE
                # the floor that the classifier calls a skip (a high admin
                # threshold) still goes to the real-play path, because it may
                # legitimately be a correction of an existing longer play, and
                # this path cannot correct. That path's matcher only sees real
                # plays, though, so when the listener already recorded the
                # same event - classified is_skip=1 under the same threshold -
                # there is nothing to correct and the twin was invisible: a
                # second skip row landed seconds away, and every 5-30s abandon
                # the listener caught counted twice after the yearly export.
                # So the real-play path claims a nearby skip row too, but only
                # AFTER it has found nothing to correct (see the insert below):
                # a correction of a longer play still wins first. See
                # _applySkipEntry for why a sub-5s event never claims or
                # corrects a real play row.
                if entry.get("isSkip") and isSkip:
                    skipsSaved, skipsEnriched = self._applySkipEntry(
                        track_id, played_at, time_played, entry.get("importExtras"), runState,
                        playedFrom=played_from)
                    skipsSavedCount += skipsSaved
                    enrichedCount += skipsEnriched
                    continue

                # Check if a play for this track already exists within (duration + 60s) tolerance -
                # see _nearTimeMatches.
                durationSeconds = (trackDurationMs or 0) // 1000
                matches = self._nearTimeMatches(track_id, played_at, durationSeconds, runState)

                if matches:
                    if len(matches) == 1:
                        # Exactly one match - safe to update if data differs.
                        # See _reconcileSingleMatch for the update/enrich/no-op
                        # decision and what each outcome means for the totals below.
                        existing_play = matches[0]
                        runState.claimedRowIds.add(existing_play["id"])
                        updated, enriched, matchCorrectedYears, matchTouchedTimestamp = self._reconcileSingleMatch(
                            existing_play, track_id, played_at, time_played, isSkip,
                            played_from, extras, extrasValues)
                        if updated:
                            updatedCount += 1
                            correctedYears |= matchCorrectedYears
                            earliestTouchedTimestamp = _minTimestamp(
                                earliestTouchedTimestamp, matchTouchedTimestamp)
                        elif enriched:
                            enrichedCount += 1
                        continue
                    else:
                        # Multiple matches - ambiguous, skip to avoid wrong update
                        if deferCommit:
                            # ...except in an overwrite batch, where skipping is
                            # not the safe option it is above. This entry's own
                            # row was already deleted with the covered range, so
                            # the rows still visible are survivors from OUTSIDE
                            # the span (just past its edges, or across an
                            # uncovered-year boundary) - "already recorded" is
                            # exactly what they are not. Skipping would drop the
                            # play for good under a "complete" message. Abort
                            # instead: the delete shares this transaction, so the
                            # rollback restores the whole range. Same invariant as
                            # the staging drop guards, one phase later.
                            _dbmod.logger.error(
                                "Overwrite import for user %s: %d plays found within tolerance of an "
                                "entry for track %s - aborting rather than dropping it",
                                self.user, len(matches), track_id,
                            )
                            raise AmbiguousImportMatchError(AMBIGUOUS_MATCH_ABORT_MESSAGE)
                        if flaskDebugEnabled():
                            _dbmod.logger.info(
                                "Skipping import play for track %s: %d plays found within tolerance - ambiguous, "
                                "not updating to avoid wrong match",
                                track_id, len(matches),
                            )
                        continue

                # No real play to correct. An entry the classifier calls a skip
                # is stored as one, so before inserting it, look for the skip
                # row the listener may already hold for this same event - the
                # matcher above filters is_skip=0 and cannot see it (see the
                # dispatch comment). Same claim-the-nearest rule and tolerance
                # as the sub-floor skip path; a claimed twin is the entry
                # already recorded, and counts as nothing new. This is the
                # real-play path's own tight second look, not a widening of
                # the matcher: the duration-wide window above would swallow a
                # genuine second abandon of the same track later in a session.
                if isSkip:
                    claimedSkip = self._claimNearbySkip(track_id, played_at, runState)
                    if claimedSkip is not None:
                        if self._enrichMatchedPlay(claimedSkip, played_from, extras, extrasValues):
                            enrichedCount += 1
                        continue

                # Otherwise insert as usual, with the is_skip computed above
                # from the batch threshold + this track's duration.
                if self.repo.insertPlay(self.user, track_id, played_at, time_played, played_from,
                                        created_reason=f"history_import (user: {self.user})",
                                        extras=entry.get("importExtras"), is_skip=isSkip):
                    insertedCount += 1
                    earliestTouchedTimestamp = _minTimestamp(earliestTouchedTimestamp, played_at)
                runState.insertedPlayKeys.add((track_id, played_at))

            # The hash's job is to stop a COMPLETE import of this file from
            # repeating. A file whose plays were partly dropped for a retryable
            # reason (see RETRYABLE_DROP_STAT_KEYS) is not one: the importer's
            # own drop log line tells the user to re-import it, and the append
            # batch answers a marked hash with "skipped" before it builds an
            # Importer - so marking it here made that advice impossible to
            # follow. Left unmarked, the re-import runs in full and the plays
            # that did land dedup against themselves. (A deterministic
            # per-entry failure therefore re-imports in full on every drop of
            # the file; dedup absorbs the rows, and only the still-missing
            # tracks are looked up again.)
            retryableDropped = sum(importStats.get(key, 0) for key in RETRYABLE_DROP_STAT_KEYS)
            unreadableDropped = sum(importStats.get(key, 0) for key in UNREADABLE_DROP_STAT_KEYS)
            if track_file_hash and not retryableDropped:
                self.repo.markFileImported(self.user, _exportContentHash(exportedHistory))

            # Both corrections and new earlier listens can move discoveries
            # in later years. A metadata-only repair has no history scope.
            touchedYears = set(correctedYears)
            if earliestTouchedTimestamp is not None:
                touchedYears.add(_dbmod.convertToDatetime(earliestTouchedTimestamp, tz=self.tz).year)
            if deferCommit:
                # The outer batch owns invalidation and commit after all
                # files, including their final catalog membership, are known.
                runState.correctedYears |= correctedYears
            else:
                historyScopes = ((self.user, min(touchedYears)),) if touchedYears else ()
                repairResult = (self.repo._invalidateWrappedForRepairs(
                    conn, runState.pendingRepairImpacts, historyScopes)
                    if runState.pendingRepairImpacts else None)
                self.repo.commit()
                runState.pendingRepairImpacts.clear()
                runState.committedRepairResult = repairResult
        except Exception as e:
            self.repo.rollbackQuietly()
            runState.claimedRowIds = claimedRowIdsBefore
            runState.insertedPlayKeys = insertedPlayKeysBefore
            runState.pendingRepairImpacts = pendingRepairImpactsBefore
            runState.correctedYears = correctedYearsBefore
            runState.committedRepairResult = None
            self.writeProgress("failed", index, total, f"{progressPrefix}Import failed: {_dbmod.parseError(e)}", error=True)
            raise

        # Reporting and legacy non-repair cache cleanup happen after the
        # commit boundary. An error here must never pretend durable writes
        # were rolled back or put committed repair impacts back into the run.
        if not deferCommit:
            if runState.committedRepairResult is not None:
                self._logCommittedMetadataRepair("append", runState.committedRepairResult)
            else:
                self._invalidateWrappedFromEarliestOf(touchedYears, "Import")
            runState.retryableDroppedTotal += retryableDropped
            runState.unreadableDroppedTotal += unreadableDropped

        droppedNoTrack = importStats.get("droppedNoTrack", 0)
        summary = (f"{insertedCount} new, {updatedCount} corrected, {enrichedCount} enriched, "
                   f"{skipsSavedCount} skips saved")
        if droppedNoTrack:
            summary += f", {droppedNoTrack} without track info dropped"
        if unreadableDropped:
            summary += f", {unreadableDropped} unreadable entries skipped"
        if retryableDropped:
            summary += (f", {retryableDropped} could not be looked up "
                        "(re-import this file to retry them)")
        _dbmod.logger.info("Imported %d tracks for user %s: %s", len(stagedTracks), self.user, summary)

        status = "complete" if isFinalFile else "running"
        reportProgress(status, total, total, f"{progressPrefix}Import complete: {summary}", error=hasPriorError)

    def importHistoryBatch(self, fileContents: list[str], overwriteRange: bool = False,
                           unreadableFileCount: int = 0) -> list[str]:
        """Import multiple export files sequentially - cached up front by the
        caller (app.py reads every upload before starting this thread) and then
        processed one after another, mirroring AutoImporter's existing
        one-file-at-a-time folder-watching behavior. Serialized per user via
        _importLock (see its comment in __init__).

        overwriteRange=False: a failure in one file is logged and skipped
        rather than aborting the whole batch, so a single bad upload doesn't
        block the rest. Returns one outcome per input file, in order -
        "imported", "skipped" (already imported before, by hash), or "failed"
        - so AutoImporter can route each file to DONE/ or FAILED/ instead of
        assuming success.

        overwriteRange=True: the covered-range delete (see _deleteCoveredRange)
        and every file's import share ONE transaction - see
        _importHistoryBatchOverwriteLocked. A failure anywhere aborts the
        whole batch and rolls back everything, so either every file's data
        lands or none of it does; the returned outcomes are all "imported" or
        all "failed" accordingly. Also bypasses the already-imported hash gate
        so unchanged files re-import fresh.

        `unreadableFileCount`: how many uploaded files never made it into
        `fileContents` because the caller could not read them at all (see
        routes/system.py, which drops an upload that is not valid UTF-8). It has
        to be passed rather than inferred: by the time the batch runs there is
        no content left to count, and the batch would otherwise report "1/1
        files imported" to someone who selected two. Overwrite mode does more
        than report it - see _importHistoryBatchOverwriteLocked. Always 0 from
        AutoImporter, which quarantines an unreadable file to FAILED/ itself and
        never hands one on."""
        with self._importLock:
            if overwriteRange:
                outcomes = self._importHistoryBatchOverwriteLocked(fileContents, unreadableFileCount)
            else:
                outcomes = self._importHistoryBatchLocked(fileContents, unreadableFileCount)
        if "imported" in outcomes:
            # Milestone achieved_at dates derive from play history, which this
            # batch just changed - raise the marker the periodic milestone pass
            # consumes to re-derive them (see Database.consumeMilestoneRecalcFlag).
            # All-skipped/all-failed batches changed nothing, so nothing is due.
            self.raiseMilestoneRecalcFlag()
        return outcomes

    def _importHistoryBatchLocked(self, fileContents: list[str], unreadableFileCount: int = 0) -> list[str]:
        if not fileContents:
            return []

        total = len(fileContents)
        outcomes: list[str] = []
        failureReasons: list[str] = []   #< one _classifyImportFailureReason(e) per "failed" outcome

        # One run state for the whole batch: files commit separately, so a
        # skip/replay pair straddling a file boundary would otherwise collapse
        # (the replay in file N+1 matching the skip committed by file N).
        runState = _dbmod._ImportRunState()
        for index, content in enumerate(fileContents, start=1):
            failedSoFar = outcomes.count("failed")
            try:
                isFinalFile = (index == total)
                file_hash = _exportContentHash(content)

                if self.repo.isFileImported(self.user, file_hash):
                    _dbmod.logger.info("File %s/%s already imported (hash: %s). Skipping.", index, total, file_hash)
                    outcomes.append("skipped")
                    status = "complete" if isFinalFile else "running"
                    self.writeProgress(status, index, total, f"File {index}/{total}: Skipping already imported file", error=(failedSoFar > 0))
                    continue

                self.importHistory(
                    content,
                    progressPrefix=f"File {index}/{total}: ",
                    isFinalFile=isFinalFile,
                    hasPriorError=(failedSoFar > 0),
                    track_file_hash=True,
                    runState=runState
                )
                outcomes.append("imported")
            except Exception as e:
                outcomes.append("failed")
                reason = _classifyImportFailureReason(e)
                failureReasons.append(reason)
                _dbmod.logger.error("Import failed for file %s/%s: %s", index, total, _dbmod.parseError(e))
                if not isFinalFile:
                    #< importHistory wrote a TERMINAL 'failed' on its way out,
                    #  but that is this FILE's verdict and the batch is still
                    #  going. Left standing it says "settled" for as long as
                    #  the next file's Importer takes to log in - seconds - and
                    #  the periodic milestone pass reads exactly this status to
                    #  decide whether an import is in flight. It would then run
                    #  mid-batch with the end-of-batch flag not yet raised, and
                    #  record every threshold the imported files just crossed
                    #  as UNSEEN: years-old achievements arriving as new
                    #  notifications, which no later pass repairs
                    #  (recalculateMilestoneDates never touches seen flags).
                    #  Same shape as the skip arm above: terminal only on the
                    #  final file, and the batch summary below owns the real
                    #  verdict either way. error=True keeps the banner red.
                    self.writeProgress("running", index, total,
                                       f"File {index}/{total}: Import failed ({reason}), continuing",
                                       error=True)

        failedCount = outcomes.count("failed")
        skippedCount = outcomes.count("skipped")
        succeededCount = total - failedCount - skippedCount
        # `total` counts only the files that reached this batch, so a file the
        # caller could not read is invisible in every count above - the summary
        # said "Imported 1/1 files" to someone who selected two, and the one
        # that vanished was never mentioned anywhere the user looks. Appended
        # rather than folded into the counts, which describe what the IMPORTER
        # saw; and it makes the line an error, because a file that did not land
        # is not a clean run whatever the others did.
        unreadableNote = (f" {unreadableFileCount} file(s) could not be read (not valid UTF-8 text) "
                          "and were skipped." if unreadableFileCount else "")
        # failureReasons carries one classified phrase per "failed" outcome
        # (never raw exception text - see _classifyImportFailureReason), so
        # the summary can name WHY without repeating the per-file messages.
        failureNote = f" - {_summarizeFailureReasons(failureReasons)}" if failedCount else ""
        if failedCount == 0 and skippedCount == total:
            status, message = "complete", "All files were already imported"
        elif failedCount == 0:
            status, message = "complete", f"Imported {succeededCount}/{total} files ({skippedCount} skipped)"
        elif succeededCount == 0 and skippedCount == 0:
            status, message = "failed", f"Imported 0/{total} files (all failed{failureNote})"
        else:
            status, message = "complete", (f"Imported {succeededCount}/{total} files "
                                           f"({skippedCount} skipped, {failedCount} failed{failureNote})")
        # runState.retryableDroppedTotal is the sum _applyImportData added per
        # committed file (see its comment there). This line is the ONLY one
        # import_progress still holds once the batch has finished - every
        # per-file "Import complete: ..." line naming its own drops (66c4a3a)
        # is overwritten in turn by the next file's line and finally by this
        # one, so a drop that never reaches here never reaches the /import
        # page at all. Zero for an all-skipped/all-failed batch, since a
        # skipped file never runs _applyImportData and a failed one raised
        # before its accumulation point.
        retryableNote = (f" {runState.retryableDroppedTotal} could not be looked up "
                         "(re-import the affected file(s) to retry them)." if runState.retryableDroppedTotal else "")
        #< same accumulator shape as the retryable count, same reason: the
        #  per-file line naming these is gone by the time the page polls
        unreadableEntriesNote = (f" {runState.unreadableDroppedTotal} unreadable entries were skipped "
                                 "(see the server log)." if runState.unreadableDroppedTotal else "")
        self.writeProgress(status, total, total, message + unreadableNote + retryableNote + unreadableEntriesNote,
                           error=bool(failedCount or unreadableFileCount))
        return outcomes

    def _importHistoryBatchOverwriteLocked(self, fileContents: list[str],
                                           unreadableFileCount: int = 0) -> list[str]:
        """Atomic overwrite in two phases. Phase 1 stages every file (parse +
        Spotify metadata fetch) into memory holding NO write transaction; Phase 2
        then runs the covered-range delete and every file's apply in ONE short
        transaction with no network in between, committed once at the end. That
        keeps SQLite's single write lock from being held across Spotify lookups -
        the old flow held it for the whole run, timing other writers (the live
        listener) out and losing their plays. Any failure rolls back everything
        and aborts, leaving the original data untouched; only an all-success
        batch commits."""
        if not fileContents:
            return []

        total = len(fileContents)

        # An uploaded file the caller could not read at all is the same failure
        # as UNREADABLE_DROP_STAT_KEYS one layer up, and the more dangerous
        # half: those guards count ENTRIES, and only entries that reached this
        # batch. A whole file that never arrived leaves the covered range to be
        # computed from the survivors, which happily bracket it - upload the
        # three part-files Spotify splits a year into with the MIDDLE one
        # undecodable and its plays are deleted with nothing to put them back,
        # under a completion line naming the two that did arrive.
        #
        # Ahead of _computeCoveredRange rather than merely ahead of the delete:
        # the answer is already known, and parsing every file (and logging an
        # Importer into Spotify to do it) is work spent on a batch that cannot
        # run.
        if unreadableFileCount:
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: {unreadableFileCount} uploaded file(s) "
                               "could not be read (not valid UTF-8 text), so the plays they hold "
                               "cannot be restored - nothing was deleted, your data is unchanged. "
                               "Re-export or re-save those file(s) as UTF-8, or import without the "
                               "overwrite option to add what is readable.",
                               error=True)
            return ["failed"] * total

        # Phase 0 - covered range (parse only, no DB, no lock). Detects an
        # unrecognized/corrupt file before anything is deleted.
        coverage = self._computeCoveredRange(fileContents)
        if coverage is None:
            self.writeProgress("failed", 0, total,
                               "Overwrite import aborted: unrecognized or corrupt export file - nothing was deleted",
                               error=True)
            return ["failed"] * total
        minStart, maxEnd, coveredYears = coverage

        try:
            runState, stagedFiles = self._stageAllFiles(fileContents, total)
        except Exception as e:
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: no changes were applied, original data is intact - {_dbmod.parseError(e)}",
                               error=True)
            return ["failed"] * total

        if self._guardStagedDrops(stagedFiles, total):
            return ["failed"] * total

        if not self._applyStagedBatch(stagedFiles, runState, minStart, maxEnd, coveredYears, total):
            return ["failed"] * total

        # Post-commit cleanup, OUTSIDE the try above: the commit is the point
        # of no return, and these are repairable side effects, not part of the
        # atomic apply. When they shared the try, a failure here fell into the
        # rollback handler - which told the user "no changes were applied,
        # original data is intact" about an overwrite that had durably landed,
        # and returned all-failed, so AutoImporter moved the successfully
        # imported files to FAILED/ and importHistoryBatch never raised the
        # milestone recalc flag. Each loop is guarded on its own so a Wrapped
        # hiccup still lets the cover art queue.
        if runState.committedRepairResult is None:
            rewrittenYears = self._wrappedYearsToInvalidate(minStart, maxEnd, coveredYears,
                                                            runState.correctedYears)
            self._invalidateWrappedFromEarliestOf(rewrittenYears, "Overwrite import")
        for track in runState.pendingImageTracks.values():
            try:
                self.saveImagesFromTrack(track)
            except Exception as e:
                #< missing art self-heals too: the detail/list pages lazy-fetch
                _dbmod.logger.warning("Overwrite import committed, but queueing cover art for track %s "
                                      "failed: %s", track.get("id"), _dbmod.parseError(e))

        # Phase 2's per-file completion lines are routed to noProgress in
        # overwrite mode, so this line is the only place a permanent drop can
        # surface at all. Recomputed rather than threaded through
        # _guardStagedDrops - cheap (a dict sum over already-staged data) and
        # keeps that guard's return value a plain "must abort" bool.
        summary = f"Overwrite import complete: {total}/{total} files imported"
        permanentDropped = self._sumImportStats(stagedFiles).get("droppedNoTrack", 0)
        if permanentDropped:
            summary += f" ({permanentDropped} entries dropped: no track info, e.g. podcasts)"
        self.writeProgress("complete", total, total, summary)
        return ["imported"] * total

    def _stageAllFiles(self, fileContents: list[str], total: int):
        """Phase 1 of the overwrite batch: stage every file (parse + Spotify
        metadata fetch) into memory. Holds NO write transaction, so these
        network-bound lookups can't block other writers. writeProgress here
        is safe: nothing is staged on the connection during staging, only
        in-memory structures. So staging gets the REAL reporter, as the
        non-overwrite path's importHistory gives it: this limiter-paced
        phase dominates a big export, and with a no-op reporter it sat at
        "Fetching metadata" and 0% from the first lookup to the last, then
        jumped to complete.

        Returns (runState, stagedFiles) - the SAME runState instance Phase 2
        (_applyStagedBatch) must be given back: it accumulates claimed rows
        and pending images across both phases, not staging alone. Raises on
        any staging failure; the caller reports the abort (nothing was
        written yet, so "original data is intact" holds for every cause)."""
        runState = _dbmod._ImportRunState()
        importer = None
        try:
            importer = self._withCookiesFile(lambda cookiesFile: _dbmod.Importer(cookiesFile=cookiesFile, email=self.email))
            knownTracks = self.repo.getAllTracks()   #< shared, grown per file below
            stagedFiles = []
            for index, content in enumerate(fileContents, start=1):
                progressPrefix = f"File {index}/{total}: "
                self.writeProgress("running", index - 1, total, f"{progressPrefix}Fetching metadata")
                staged = self._stageImportData(importer, content, progressPrefix,
                                               False, self.writeProgress, runState, deferCommit=True,
                                               knownTracks=knownTracks)
                if staged is not None:
                    # Feed this file's fetched tracks forward so a later file
                    # that replays the same track doesn't re-fetch it.
                    knownTracks.extend(staged[0].values())
                stagedFiles.append((staged, content, progressPrefix, index == total))
            return runState, stagedFiles
        finally:
            # Staging is the importer's last use - the phases below only touch
            # the staged rows. Release its TLS session (fresh per login, see
            # Database/Spotify/client.py) here rather than at process exit.
            if importer is not None:
                importer.sp.close()

    def _guardStagedDrops(self, stagedFiles: list, total: int) -> bool:
        """Phase 1b of the overwrite batch: the delete range covers every
        play the files PARSED, but only the plays that survived staging get
        re-inserted. A play dropped for a retryable reason (rate limit,
        timeout, unexpected error) would therefore be deleted and never
        replaced, inside the same transaction that makes the rest atomic -
        permanent, silent loss of rows the listener or an earlier import had
        already recorded. Abort while nothing has been deleted yet and let
        the user re-run.

        Returns True when the batch must abort (the failure progress has
        already been written); False when it's safe to proceed to Phase 2."""
        droppedStats = self._sumImportStats(stagedFiles)
        retryableDropped = sum(droppedStats.get(key, 0) for key in RETRYABLE_DROP_STAT_KEYS)
        if retryableDropped:
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: {retryableDropped} play(s) could not be looked up "
                               "(Spotify rate limit or outage) - nothing was deleted, your data is unchanged. "
                               "Please try the import again.",
                               error=True)
            return True

        unreadableDropped = sum(droppedStats.get(key, 0) for key in UNREADABLE_DROP_STAT_KEYS)
        if unreadableDropped:
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: {unreadableDropped} entr(y/ies) in the uploaded file(s) "
                               "could not be read, so the plays they describe cannot be restored - nothing was "
                               "deleted, your data is unchanged. Re-export the file, or import "
                               "without the overwrite option to add what is readable.",
                               error=True)
            return True
        return False

    def _applyStagedBatch(self, stagedFiles: list, runState, minStart, maxEnd, coveredYears, total: int) -> bool:
        """Phase 2 of the overwrite batch: ONE short transaction - delete the
        covered range, then apply every file's staged rows. No network here,
        so the write lock is held only for the local DB work. writeProgress
        must NOT run between the delete and the final commit (it
        self-commits - INVARIANT), so the per-file apply calls below are
        given a no-op reporter and their completion lines never surface.

        Returns True on a successful commit; False (having already rolled
        back and written the failure progress) on any failure - the whole
        batch is all-or-nothing, so the caller reports every file as
        "failed" either way."""
        def noProgress(*args, **kwargs):
            return None

        runState.committedRepairResult = None
        runState.pendingRepairImpacts.clear()
        try:
            self.writeProgress("running", 0, total, f"Overwrite: applying {total} file(s)")
            conn = self._beginMetadataWrite()
            deletedPlays, deletedSkips, skippedYears = self._deletePlaysInCoveredRange(minStart, maxEnd, coveredYears)
            message = f"Overwrite: staged deletion of {deletedPlays} plays and {deletedSkips} skip events in the covered range"
            if skippedYears:
                yearsText = ", ".join(str(year) for year in skippedYears)
                message += f" ({yearsText} not covered by uploaded files - left untouched)"
            _dbmod.logger.info("%s for user %s", message, self.user)

            for staged, content, progressPrefix, isFinalFile in stagedFiles:
                if staged is None:
                    continue  #< a valid-but-empty file staged nothing
                stagedTracks, stagedPlays, fileTotal, importStats = staged
                self._applyImportData(stagedTracks, stagedPlays, importStats, fileTotal, content,
                                      progressPrefix, isFinalFile, False, True,
                                      runState, True, noProgress)

            repairResult = None
            if runState.pendingRepairImpacts:
                rewrittenYears = self._wrappedYearsToInvalidate(minStart, maxEnd, coveredYears,
                                                                runState.correctedYears)
                historyScopes = ((self.user, min(rewrittenYears)),) if rewrittenYears else ()
                repairResult = self.repo._invalidateWrappedForRepairs(conn, runState.pendingRepairImpacts,
                                                                     historyScopes)
            self.repo.commit()
            runState.pendingRepairImpacts.clear()
            runState.committedRepairResult = repairResult
        except Exception as e:
            # _applyImportData's except already rolled back the whole
            # transaction (the delete plus every prior file's staged writes)
            # when the failure came from an apply; call it again defensively
            # (a no-op if nothing is pending) in case it came from the delete.
            self.repo.rollbackQuietly()
            runState.pendingRepairImpacts.clear()
            runState.committedRepairResult = None
            runState.correctedYears.clear()
            runState.claimedRowIds.clear()
            runState.insertedPlayKeys.clear()
            _dbmod.logger.error("Overwrite import aborted after a failure - no changes were applied, "
                        "original data is intact: %s", _dbmod.parseError(e))
            self.writeProgress("failed", 0, total,
                               f"Overwrite import aborted: no changes were applied, original data is intact - {_dbmod.parseError(e)}",
                               error=True)
            return False

        self._logCommittedMetadataRepair("overwrite", runState.committedRepairResult)
        return True

    @staticmethod
    def _sumImportStats(stagedFiles: list) -> dict:
        """Batch-wide totals of the per-file drop counters staging produced.
        stagedFiles entries are (staged, content, progressPrefix, isFinalFile),
        where `staged` is None for a valid-but-empty file and otherwise carries
        its importStats dict as its last element."""
        totals: dict = {}
        for staged, *_ in stagedFiles:
            if staged is None:
                continue
            for key, count in staged[3].items():
                totals[key] = totals.get(key, 0) + count
        return totals

    def _computeCoveredRange(self, fileContents: list[str]) -> tuple | None:
        """Parse every file (no DB writes, no lock, no Spotify session) and
        return (minStart, maxEnd, coveredYears) for the overwrite delete: the
        batch span [earliest entry, latest entry] and the union of covered
        years (a year counts as covered only if some entry STARTS in it - see
        Importer.coverage). Returns (None, None, set()) when the files cover
        nothing (all valid-but-empty), or None when any file is unrecognized/
        corrupt - the caller must abort WITHOUT deleting."""
        # No cookiesFile: this pass calls _convertToList and coverage, and
        # neither touches Importer.sp - the client is there for the metadata
        # lookups (_searchForSong/_fetchTrackMeta), which belong to the staging
        # phase. Handing one over ran a full spotapi login anyway, since
        # Spotify.__init__ logs in whenever it is given a path: a TLSClient of
        # its own, and a cookies file written to disk and deleted around it,
        # per overwrite import, for a pass that is pure text parsing. Staging
        # still builds its own WITH cookies (_importHistoryBatchOverwriteLocked
        # Phase 1, which runs right after this) - that one needs them, and
        # dropping them there instead would silently synthesize every track
        # rather than look it up.
        #
        # Nothing to release afterwards either, which is why the close() this
        # used to end with is gone: without a path Spotify.__init__ leaves
        # user_auth False and never builds a session.
        importer = _dbmod.Importer(email=self.email)
        minStart = None
        maxEnd = None
        coveredYears: set[int] = set()
        for content in fileContents:
            parsedHistory, exportType = importer._convertToList(content)
            if exportType == "None":
                return None
            fileCoverage = importer.coverage(parsedHistory, exportType)
            if fileCoverage is None:
                continue  #< a valid-but-empty export covers nothing
            fileMin, fileMax, fileYears = fileCoverage
            minStart = fileMin if minStart is None else min(minStart, fileMin)
            maxEnd = fileMax if maxEnd is None else max(maxEnd, fileMax)
            coveredYears |= fileYears

        return minStart, maxEnd, coveredYears

    def _invalidateWrappedFromEarliestOf(self, rewrittenYears, whatCommitted: str) -> None:
        """Drop the cached Wrapped for the earliest year this import rewrote,
        and for every year after it.

        `rewrittenYears` says which years' PLAYS changed. That is not the same
        question as which years' cached Wrapped is now wrong, and it is the
        narrower one: the discovery fields (discovered_songs,
        discovered_artists and the three discovered_*_list columns) are
        anchored on each item's ALL-TIME first listen, so a play written into
        an earlier year moves items into and out of LATER years' discovery
        lists. Those years' own play_count and max_played_at do not move, and
        they are all _wrappedCacheNeedsRecalc compares - so the periodic worker
        never notices and the wrong lists survive until something else drops
        the row.

        This deliberately reaches past the gap years _wrappedYearsToInvalidate
        protects. That narrowing is still right for what it answers - which
        years the delete rewrote, and therefore which plays exist - but a gap
        year's DISCOVERIES really can change when a year before it does.

        Forwards only: a first listen can move within or after the year the new
        play lands in, never before it.

        Guarded like its neighbours in the post-commit block: the commit is the
        point of no return, and a stale cache row is repairable - the deleted
        rows recompute on demand or on the worker's next cycle, and a row left
        behind by a failure here is exactly the state this fix describes, not a
        new one."""
        if not rewrittenYears:
            return
        earliestYear = min(rewrittenYears)
        try:
            self.repo.deleteUserWrappedFromYear(self.user, earliestYear)
        except Exception as e:
            _dbmod.logger.warning("%s committed, but clearing the cached Wrapped from %d onwards "
                                  "failed (those years may show stale data until recalculated): %s",
                                  whatCommitted, earliestYear, _dbmod.parseError(e))

    def _wrappedYearsToInvalidate(self, minStart, maxEnd, coveredYears, correctedYears) -> set:
        """Which years' cached Wrapped this overwrite may have moved.

        Three year notions meet here, and two of them are counted in different
        timezones on purpose:

        - `coveredYears` and the delete segments beside it are bucketed in the
          INSTANCE zone, matching Importer.coverage - that is what makes the
          segments line up with the files' own coverage.
        - `correctedYears` is bucketed in the USER's zone, because that is the
          zone a Wrapped year is defined in (see _computeAvailableYears, which
          reads db.tz).

        A user whose timezone differs from the instance's - `refreshSettings`
        lets them - can therefore have a play that Importer.coverage calls year
        N while their Wrapped calls it N+1, so passing covered years straight to
        a user-keyed cache could drop the wrong year and leave the right one
        stale.

        So each covered year's SEGMENT is re-bucketed in the user's zone and
        unioned in rather than substituted. Widening is safe in a way that
        switching is not: an extra year here costs one recomputation of a cache
        that is rebuilt on demand, while a missed year shows stale numbers.
        Nothing about which PLAYS get deleted is affected - that decision is
        made and committed above.

        Per covered year rather than across the whole span, because a span can
        contain years no file covers - upload 2018 and 2021 and the years
        between are not touched at all. _deletePlaysInCoveredRange protects them
        by design and reports them as skippedYears, so invalidating their cache
        would contradict the delete this is following, and cost a full-year
        recomputation on the next request thread to ask for one (the wrapped
        cache miss recalculates synchronously).

        Only each segment's two ENDS are converted: year() is monotonic in time,
        so no play inside a segment can fall outside the years its endpoints
        land in.
        """
        years = set(coveredYears) | set(correctedYears)
        if minStart is None or maxEnd is None:
            return years

        # The instance zone, matching the delete's segmentation exactly - the
        # segments below have to be the same ones whose plays were removed.
        instanceTz = _dbmod.getTimezone()

        def yearStartTs(year: int) -> float:
            return _dbmod.datetime.datetime(year, 1, 1, tzinfo=instanceTz).timestamp()

        def usersYear(timestamp: float) -> int:
            return _dbmod.convertToDatetime(timestamp, tz=self.tz).year

        for year in sorted(coveredYears):
            segmentStart = max(yearStartTs(year), minStart)
            segmentEnd = self._yearSegmentEnd(year, maxEnd, yearStartTs)
            if segmentStart > segmentEnd:
                continue   #< a covered year the batch span does not actually reach
            years.update(range(usersYear(segmentStart), usersYear(segmentEnd) + 1))
        return years

    def _yearSegmentEnd(self, year: int, maxEnd: float, yearStartTs) -> float:
        """Where `year`'s slice of a batch span ends: the instant before the
        next year begins, or the span's own end if that comes first.

        Year 9999 has no year 10000 to subtract from - datetime refuses it -
        and a far-future played_at reaches this (a corrupt export;
        _plausibleStart is deliberately a floor with no ceiling). The overwrite
        then died with "year must be in 1..9999, not 10000": the transaction
        rolls back so nothing is lost, but the message names nothing actionable
        and re-running never succeeds. maxEnd is the right answer there anyway -
        nothing in the span can be later than it - and it is what the min()
        below already picks for the span's last year."""
        if year >= _dbmod.datetime.datetime.max.year:
            return maxEnd
        return min(yearStartTs(year + 1) - self.YEAR_SEGMENT_BOUNDARY_EPSILON_SECONDS, maxEnd)

    def _deletePlaysInCoveredRange(self, minStart, maxEnd, coveredYears) -> tuple[int, int, list[int]]:
        """Delete this user's plays and skips in each covered year's segment of
        [minStart, maxEnd]. Years inside the span that no file covers (missing
        files) are left untouched and returned as skippedYears. Does NOT commit
        and does NOT touch the Wrapped cache - runs inside the overwrite batch's
        single transaction, so the caller commits and invalidates Wrapped once
        the whole batch succeeds. Returns (deletedPlays, deletedSkips,
        skippedYears); an empty covered range deletes nothing."""
        if minStart is None:
            return 0, 0, []

        # Same timezone as Importer.coverage's year bucketing, so segments
        # line up exactly with the covered-years set.
        tz = _dbmod.getTimezone()

        def yearStartTs(year: int) -> float:
            return _dbmod.datetime.datetime(year, 1, 1, tzinfo=tz).timestamp()

        deletedPlays = 0
        deletedSkips = 0
        skippedYears: list[int] = []
        firstYear = _dbmod.convertToDatetime(minStart, tz).year
        lastYear = _dbmod.convertToDatetime(maxEnd, tz).year
        for year in range(firstYear, lastYear + 1):
            if year not in coveredYears:
                skippedYears.append(year)
                continue
            segmentStart = max(yearStartTs(year), minStart)
            segmentEnd = self._yearSegmentEnd(year, maxEnd, yearStartTs)
            deletedPlays += self.repo.deletePlaysInRange(self.user, segmentStart, segmentEnd)
            deletedSkips += self.repo.deleteSkipsInRange(self.user, segmentStart, segmentEnd)

        return deletedPlays, deletedSkips, skippedYears
