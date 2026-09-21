import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest, rawSpotifyTrackForTest
import Database.Importers.StreamingHistoryImporter as importerModule
import Database.import_service as importService


MALFORMED_JSON_EXPORT = "{not valid json"
MALFORMED_DATE = "not-a-date"
MALFORMED_MUSICOLET_CSV = "\n".join([
    "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT",
    "/music/bad.mp3,Shifted",
])


def _meta(trackId, playedAt):
    """A full importer-yielded item: entry fields + enough track metadata to
    satisfy Repository.upsertTrack (mirrors what Client.formatTrack produces)."""
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = 60000   #< a full listen (> the 5s skip floor) -> real play
    track["playedFrom"] = None
    return track


class TestImportHistoryCommit(DatabaseTestCase):
    """importHistory must commit atomically: a mid-import failure may not leave
    half-imported entries behind, and a successful import may not drop entries
    the listener recorded meanwhile."""

    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
        ])

    def _mockImporter(self, generatorFactory, parsedCount=2):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
        importer.importHistory.return_value = generatorFactory()
        return importer

    def _playedAts(self):
        return [e["playedAt"] for e in self.db.getEntriesFromOld(fullPagination=False)]

    def _stubSpotifyClient(self, *trackIds):
        """A stand-in for the importer's Spotify client that answers lookups from
        canned wire-shape data, so the real parser still runs but nothing tries
        to log in or retry over the network."""
        canned = {trackId: rawSpotifyTrackForTest(trackId) for trackId in trackIds}
        client = MagicMock()
        client.track.side_effect = lambda uri: canned.get(str(uri).rsplit(":", 1)[-1])
        return client

    def test_successful_import_merges_and_sorts(self):
        def gen():
            yield _meta("i1", 200)
            yield _meta("i2", 50)   #< out of order on purpose

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        self.assertEqual(self._playedAts(), [50, 100, 200, 300])
        self.assertIsNotNone(self.db.repo.getTrack("i1"))
        self.assertIsNotNone(self.db.repo.getTrack("i2"))
        self.assertEqual(self.db.readProgress()["status"], "complete")

    def test_import_closes_the_spotify_session(self):
        """Each import builds its own Importer, whose Spotify login holds a
        fresh TLS session (Database/Spotify/client.py) - retiring it without
        closing leaked one live curl session per import."""
        def gen():
            yield _meta("i1", 200)

        importer = self._mockImporter(gen)
        with patch("Database.database.Importer", return_value=importer):
            self.db.importHistory("raw export")

        importer.sp.close.assert_called_once_with()

    def test_a_failed_import_still_closes_the_spotify_session(self):
        def gen():
            raise RuntimeError("network died mid-import")
            yield  #< unreachable on purpose: makes this a generator

        importer = self._mockImporter(gen)
        with patch("Database.database.Importer", return_value=importer):
            with self.assertRaises(RuntimeError):
                self.db.importHistory("raw export")

        importer.sp.close.assert_called_once_with()

    def test_failed_import_leaves_database_untouched(self):
        def gen():
            yield _meta("i1", 200)
            raise RuntimeError("network died mid-import")

        entriesBefore = self._playedAts()

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            with self.assertRaises(RuntimeError):
                self.db.importHistory("raw export")

        self.assertEqual(self._playedAts(), entriesBefore)
        self.assertIsNone(self.db.repo.getTrack("i1"), "a failed import must not persist anything")
        self.assertEqual(self.db.readProgress()["status"], "failed")

    def test_a_mid_apply_failure_reports_how_far_it_got(self):
        """The apply-phase failure path's writeProgress reported current=0 no
        matter where the play loop died - `index` was initialized for exactly
        this line and then never advanced."""
        def gen():
            yield _meta("i1", 200)
            yield _meta("i2", 400)
            yield _meta("i3", 600)

        realInsert = self.db.repo.insertPlay
        insertCalls = {"n": 0}

        def failingInsert(*args, **kwargs):
            insertCalls["n"] += 1
            if insertCalls["n"] >= 2:
                raise RuntimeError("disk died mid-apply")
            return realInsert(*args, **kwargs)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen, parsedCount=3)), \
             patch.object(self.db.repo, "insertPlay", side_effect=failingInsert):
            with self.assertRaises(RuntimeError):
                self.db.importHistory("raw export")

        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["current"], 2)   #< died applying the 2nd of 3 staged plays

    def test_listener_entries_recorded_during_import_are_kept(self):
        # Seed the track the "listener" play references, same as a real concurrent
        # appendMetadata() call would (it always upserts the track before the play).
        self.db.repo.upsertTrack(normalizeTrackForTest({"id": "L1", "name": "Live Song", "artists": []}))
        self.db.repo.commit()

        def gen():
            yield _meta("i1", 200)
            # Simulate the listener recording a play while the import is running -
            # a plain committed insert on the shared connection, like a real
            # concurrent listener write.
            self.db.repo.insertPlay(self.db.user, "L1", 250, 1000,
                                    created_reason="listener_play (test)")
            self.db.repo.commit()
            yield _meta("i2", 50)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        ids = [e["id"] for e in self.db.getEntriesFromOld(fullPagination=False)]
        self.assertIn("L1", ids)
        self.assertEqual(self._playedAts(), [50, 100, 200, 250, 300])

    def test_unrecognized_export_raises_and_marks_progress_failed(self):
        """A file that parses as no known export type (corrupt JSON, a
        partially-copied file, the wrong file entirely) must FAIL loudly.
        It used to return silently, so AutoImporter moved never-imported
        files to DONE/ as successes and the web UI reported 'complete'."""
        importer = MagicMock()
        importer._convertToList.return_value = ([], "None")
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)

        with patch("Database.database.Importer", return_value=importer):
            with self.assertRaises(ValueError):
                self.db.importHistory("not an export")

        self.assertEqual(len(self._playedAts()), 2)
        importer.importHistory.assert_not_called()
        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "failed")
        self.assertTrue(progress["error"])

    def test_a_file_whose_every_entry_is_unreadable_fails_loudly(self):
        """Sibling of the case above, through the REAL importer. _convertToList
        types a file from its first row alone, so a JSON list whose rows carry
        `ts` but not the master_metadata_* keys is typed as an extended export
        and then fails on every single row. That used to import as a silent
        success - "0 tracks imported", progress complete, and AutoImporter
        moving the file to DONE/ - with nothing anywhere saying a row was lost."""
        import json
        unreadable = json.dumps([
            {"ts": "2023-05-01T10:00:00Z", "ms_played": 150000},
            {"ts": "2023-05-01T11:00:00Z", "ms_played": 120000},
        ])

        with self.assertRaises(ValueError) as ctx:
            self.db.importHistory(unreadable)

        self.assertIn("2", str(ctx.exception))
        self.assertEqual(len(self._playedAts()), 2)   #< nothing written
        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "failed")
        self.assertTrue(progress["error"])

    def test_a_file_that_is_only_partly_unreadable_still_imports_the_rest(self):
        """The loud failure is for a file nothing could be read from. One bad
        row among good ones must not throw the good ones away - they import,
        and the count is what the overwrite guard reads.

        Unlike its siblings this exercises the REAL parser (that is the point -
        _extendedEntryTuple raising KeyError on the bad row is the behaviour
        under test), so only the Spotify client is stubbed. Constructing a real
        one made this the slowest test in the suite by 15x - 7.3s, of which 2.1s
        was retry backoff on a lookup that cannot succeed offline - for a
        network round trip the assertions never look at."""
        import json
        mixed = json.dumps([
            {"ts": "2023-05-01T10:00:00Z", "ms_played": 150000,
             "master_metadata_track_name": "Song One",
             "master_metadata_album_artist_name": "Artist One",
             "spotify_track_uri": "spotify:track:track123"},
            {"ts": "2023-05-01T11:00:00Z", "ms_played": 120000},   #< unreadable
        ])

        with patch.object(importerModule, "Spotify",
                          return_value=self._stubSpotifyClient("track123")):
            self.db.importHistory(mixed)

        self.assertEqual(self.db.readProgress()["status"], "complete")
        self.assertEqual(len(self._playedAts()), 3)   #< the two seeded plus the readable one

    def test_a_malformed_date_entry_is_dropped_without_failing_the_file(self):
        """Malformed dates stay at the parser/drop boundary: they must not turn
        into an unsupported-format service failure or block readable rows."""
        import json
        mixed = json.dumps([
            {"ts": MALFORMED_DATE, "ms_played": 150000,
             "master_metadata_track_name": "Bad Date",
             "master_metadata_album_artist_name": "Artist One",
             "spotify_track_uri": "spotify:track:badDate"},
            {"ts": "2023-05-01T10:00:00Z", "ms_played": 150000,
             "master_metadata_track_name": "Song One",
             "master_metadata_album_artist_name": "Artist One",
             "spotify_track_uri": "spotify:track:track123"},
        ])

        with patch.object(importerModule, "Spotify",
                          return_value=self._stubSpotifyClient("badDate", "track123")):
            self.db.importHistory(mixed)

        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "complete")
        self.assertNotIn("unrecognised export format", progress["message"])
        self.assertEqual(len(self._playedAts()), 3)

    def test_recognized_but_empty_export_is_a_noop(self):
        """An empty-but-valid export (e.g. a JSON []) has nothing to import,
        which is not an error."""
        importer = MagicMock()
        importer._convertToList.return_value = ([], "emptyExport")
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)

        with patch("Database.database.Importer", return_value=importer):
            self.db.importHistory("[]")   #< must not raise

        self.assertEqual(len(self._playedAts()), 2)
        importer.importHistory.assert_not_called()

    def test_progress_prefix_is_included_in_messages(self):
        def gen():
            yield _meta("i1", 200)

        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export", progressPrefix="File 1/1: ")

        self.assertTrue(capturedMessages)
        self.assertTrue(all(m.startswith("File 1/1: ") for m in capturedMessages))


class TestImportHistoryBatch(DatabaseTestCase):
    """importHistoryBatch imports multiple files sequentially (cached and
    processed one after another), mirroring AutoImporter's existing
    one-file-at-a-time folder-watching behavior."""

    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])

    def _mockImporter(self, generatorFactory, parsedCount=1):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
        importer.importHistory.return_value = generatorFactory()
        return importer

    def _ids(self):
        return [e["id"] for e in self.db.getEntriesFromOld(fullPagination=False)]

    def test_files_are_imported_sequentially_and_merged(self):
        def gen1():
            yield _meta("f1i1", 100)

        def gen2():
            yield _meta("f2i1", 200)

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(gen1), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["export one", "export two"])

        self.assertEqual(self._ids(), ["f1i1", "f2i1"])
        self.assertEqual(self.db.readProgress()["status"], "complete")

    def test_one_failing_file_does_not_block_the_rest(self):
        def failing():
            raise RuntimeError("bad file")
            yield  # unreachable - keeps this a generator function

        def gen2():
            yield _meta("f2i1", 200)

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["bad export", "good export"])

        self.assertEqual(self._ids(), ["f2i1"])
        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "complete")
        self.assertIn("1 failed", progress["message"])
        self.assertTrue(progress["error"])

    def test_error_flag_is_not_cleared_on_subsequent_import_steps(self):
        def failing():
            raise RuntimeError("bad file")
            yield

        def gen2():
            yield _meta("f2i1", 200)

        capturedErrors = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedErrors.append(error)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["bad export", "good export"])

        # Once the first file fails, all subsequent writeProgress calls must preserve error=True.
        self.assertTrue(capturedErrors[-1])  # Final status has error=True
        # Check that starting file 2 sets error=True instead of False.
        self.assertTrue(capturedErrors[2])

    def test_all_files_failing_marks_progress_failed(self):
        def failing():
            raise RuntimeError("bad file")
            yield  # unreachable - keeps this a generator function

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(failing)]):
            self.db.importHistoryBatch(["bad one", "bad two"])

        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "failed")
        self.assertTrue(progress["error"])

    def test_unrecognized_export_names_its_class_in_the_progress_line(self):
        """UT-17: the per-file progress line must name a fixed failure
        class, not the bare "Import failed, continuing" it used to carry
        with no reason at all - an unrecognized/corrupt file (invalid JSON
        included: _convertToList folds a JSON parse error into the same
        "None" export type) classifies as "unrecognised export format"."""
        badImporter = MagicMock()
        badImporter._convertToList.return_value = ([], "None")

        def gen2():
            yield _meta("f2i1", 200)

        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        with patch("Database.database.Importer",
                    side_effect=[badImporter, self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["bad export", "good export"])

        midBatchMessage = next(m for m in capturedMessages if "continuing" in m)
        self.assertIn("unrecognised export format", midBatchMessage)
        self.assertNotIn("Unrecognized or corrupt export file", midBatchMessage)

    def test_malformed_json_stays_an_unsupported_format_failure(self):
        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        def gen2():
            yield _meta("f2i1", 200)

        with patch.object(importerModule, "Spotify", return_value=MagicMock()), \
             patch("Database.database.Importer",
                   side_effect=[importerModule.Importer(), self._mockImporter(gen2)]):
            self.db.importHistoryBatch([MALFORMED_JSON_EXPORT, "good export"])

        midBatchMessage = next(m for m in capturedMessages if "continuing" in m)
        self.assertIn("unrecognised export format", midBatchMessage)
        self.assertNotIn(MALFORMED_JSON_EXPORT, midBatchMessage)
        self.assertEqual(self._ids(), ["f2i1"])

    def test_malformed_musicolet_csv_stays_an_unreadable_file_failure(self):
        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        def gen2():
            yield _meta("f2i1", 200)

        with patch.object(importerModule, "Spotify", return_value=MagicMock()), \
             patch("Database.database.Importer",
                   side_effect=[importerModule.Importer(), self._mockImporter(gen2)]):
            self.db.importHistoryBatch([MALFORMED_MUSICOLET_CSV, "good export"])

        midBatchMessage = next(m for m in capturedMessages if "continuing" in m)
        self.assertIn("the file could not be read", midBatchMessage)
        self.assertNotIn("/music/bad.mp3", midBatchMessage)
        self.assertEqual(self._ids(), ["f2i1"])

    def test_generic_failure_names_an_unexpected_error_and_hides_its_text(self):
        """A failure that isn't one of the classifier's known ValueError
        shapes must still name a FIXED class - and must never leak the raw
        exception text (which could carry a filename, a track id, or any
        other user-supplied content) into the progress line."""
        def failing():
            raise RuntimeError("some internal detail nobody should see")
            yield  # unreachable - keeps this a generator function

        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        def gen2():
            yield _meta("f2i1", 200)

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["bad export", "good export"])

        midBatchMessage = next(m for m in capturedMessages if "continuing" in m)
        self.assertIn("an unexpected error", midBatchMessage)
        self.assertNotIn("some internal detail nobody should see", midBatchMessage)

    def test_unrelated_value_error_names_an_unexpected_error_and_hides_its_text(self):
        def failing():
            raise ValueError("some internal detail nobody should see")
            yield  #< unreachable - keeps this a generator function

        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        def gen2():
            yield _meta("f2i1", 200)

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["bad export", "good export"])

        midBatchMessage = next(m for m in capturedMessages if "continuing" in m)
        self.assertIn("an unexpected error", midBatchMessage)
        self.assertNotIn("unrecognised export format", midBatchMessage)
        self.assertNotIn("some internal detail nobody should see", midBatchMessage)

    def test_import_failure_categories_do_not_depend_on_diagnostic_wording(self):
        cases = (
            (importService.UnsupportedImportFormatError("diagnostic changed"),
             "unrecognised export format"),
            (importService.UnreadableImportEntriesError("diagnostic changed"),
             "the file could not be read"),
            (importService.AmbiguousImportMatchError("diagnostic changed"),
             "an ambiguous match aborted this file"),
        )

        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(importService._classifyImportFailureReason(error), expected)

    def test_all_files_failing_summary_lists_failure_classes_with_counts(self):
        badImporter1 = MagicMock()
        badImporter1._convertToList.return_value = ([], "None")
        badImporter2 = MagicMock()
        badImporter2._convertToList.return_value = ([], "None")

        with patch("Database.database.Importer", side_effect=[badImporter1, badImporter2]):
            self.db.importHistoryBatch(["bad one", "bad two"])

        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "failed")
        self.assertIn("unrecognised export format", progress["message"])
        self.assertIn("2", progress["message"])   #< both files failed the same way

    def test_progress_prefix_identifies_current_file(self):
        def gen():
            yield _meta("i1", 100)

        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistoryBatch(["only export"])

        self.assertTrue(any("File 1/1" in m for m in capturedMessages))

    def test_empty_file_list_is_a_noop(self):
        self.db.importHistoryBatch([])
        self.assertEqual(self._ids(), [])

    def test_a_file_the_caller_could_not_read_is_named_in_the_summary(self):
        """`total` counts what reached the batch, so a file the route dropped
        (routes/system.py refuses an upload that is not valid UTF-8) is absent
        from every count in this line. Left alone it read "Imported 1/1 files"
        to someone who had selected two, and the one that vanished appeared
        nowhere the user looks."""
        def gen():
            yield _meta("f1i1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistoryBatch(["good export"], unreadableFileCount=1)

        progress = self.db.readProgress()
        self.assertIn("1 file(s) could not be read", progress["message"])
        self.assertIn("UTF-8", progress["message"])
        #< red: a file that did not land is not a clean run, whatever the
        #  others did. Still "complete" - the import itself finished.
        self.assertTrue(progress["error"])
        self.assertEqual(progress["status"], "complete")

    def test_the_summary_is_byte_identical_when_every_file_arrived(self):
        """The negative control for the note above - it must not creep into the
        ordinary line."""
        def gen():
            yield _meta("f1i1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistoryBatch(["good export"])

        progress = self.db.readProgress()
        self.assertEqual(progress["message"], "Imported 1/1 files (0 skipped)")
        self.assertFalse(progress["error"])

    def test_an_all_skipped_batch_still_reports_the_unreadable_file(self):
        """The one summary arm with no counts in it at all, so it is the arm
        where a dropped file is easiest to lose."""
        def gen():
            yield _meta("i1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistoryBatch(["same content"])
            self.db.importHistoryBatch(["same content"], unreadableFileCount=2)

        progress = self.db.readProgress()
        self.assertTrue(progress["message"].startswith("All files were already imported"))
        self.assertIn("2 file(s) could not be read", progress["message"])
        self.assertTrue(progress["error"])

    def test_batch_returns_per_file_outcomes(self):
        """AutoImporter routes each file to DONE/ or FAILED/ based on the
        outcome importHistoryBatch reports for it."""
        def gen():
            yield _meta("f1i1", 100)

        def failing():
            raise RuntimeError("bad file")
            yield  # unreachable - keeps this a generator function

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(gen), self._mockImporter(failing)]):
            outcomes = self.db.importHistoryBatch(["good export", "bad export"])

        self.assertEqual(outcomes, ["imported", "failed"])

    def test_batch_reports_already_imported_files_as_skipped(self):
        def gen():
            yield _meta("i1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            firstOutcomes = self.db.importHistoryBatch(["same content"])
            secondOutcomes = self.db.importHistoryBatch(["same content"])

        self.assertEqual(firstOutcomes, ["imported"])
        self.assertEqual(secondOutcomes, ["skipped"])

    def test_import_history_batch_skips_already_imported_files(self):
        """importHistoryBatch should skip already imported files by checking their hash and update progress accordingly."""
        def gen():
            yield _meta("i1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen, parsedCount=1)):
            self.db.importHistoryBatch(["content-a", "content-b"])
        
        self.assertIsNotNone(self.db.repo.getTrack("i1"))
        
        self.db.writeProgress("idle", 0, 0, "", False)
        
        original_import_history = self.db.importHistory
        with patch.object(self.db, "importHistory", side_effect=original_import_history) as mock_import:
            self.db.importHistoryBatch(["content-a", "content-c"])
            self.assertEqual(mock_import.call_count, 1)


class TestAPlainImportInvalidatesLaterWrappedYears(DatabaseTestCase):
    """The append path has the same cross-year problem as the overwrite one.

    A Wrapped year's discovery fields are anchored on all-time first listens,
    so inserting a play into 2018 can move an artist out of "discovered in
    2022" - while 2022's own play_count and max_played_at, the only two things
    _wrappedCacheNeedsRecalc compares, do not move at all. Nothing rebuilt it.

    This path used to invalidate correctedYears alone and lean on the count
    check for everything else, which covers the year the plays LAND in and no
    other."""

    USER = "testuser"

    def _dbWithCachedWrappedYears(self, *years):
        db = self._makeDb({}, [])
        for year in years:
            db.repo.saveCachedWrapped(self.USER, year, {
                "calculated_at": 0, "max_played_at": 0, "total_plays": 1, "total_ms": 1,
                "longest_streak": 1, "peak_day": "Monday", "peak_plays": 1,
                "unique_songs": 1, "unique_artists": 1,
                "discovered_songs": 1, "discovered_artists": 1,
                "time_series_day": "[]", "time_series_week": "[]", "time_series_month": "[]",
                "top_songs": "[]", "top_artists": "[]", "top_albums": "[]",
                "discovered_songs_list": "[]", "discovered_artists_list": "[]",
                "discovered_albums_list": "[]",
            })
        return db

    def _cachedYears(self, db):
        return {r["year"] for r in db.repo._conn().execute(
            "SELECT year FROM user_wrapped WHERE username=?", (self.USER,)).fetchall()}

    def _importAt(self, db, playedAt):
        def gen():
            yield _meta("newTrack", playedAt)

        importer = MagicMock()
        importer._convertToList.return_value = ([{}], "spotifyAcountExport")
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
        importer.importHistory.return_value = gen()
        with patch("Database.database.Importer", return_value=importer):
            db.importHistory("raw export")

    @staticmethod
    def _tsIn(year):
        import datetime
        from Database.utils import getTimezone
        return datetime.datetime(year, 6, 1, tzinfo=getTimezone()).timestamp()

    def test_inserting_an_old_play_drops_the_later_cached_years(self):
        db = self._dbWithCachedWrappedYears(2018, 2022)

        self._importAt(db, self._tsIn(2018))

        self.assertEqual(self._cachedYears(db), set())

    def test_inserting_a_recent_play_keeps_the_earlier_cached_years(self):
        """The bound. Guards against the fix degenerating into "drop
        everything on every import", which would make each one cost a
        full-year recomputation per cached year."""
        db = self._dbWithCachedWrappedYears(2018, 2022)

        self._importAt(db, self._tsIn(2022))

        self.assertEqual(self._cachedYears(db), {2018})

    def test_a_wrapped_cleanup_failure_does_not_fail_a_committed_import(self):
        """The append path gained the overwrite path's post-commit guard.

        Before c165ac0 this side had none: the invalidation ran unguarded
        AFTER self.repo.commit() but INSIDE the try whose handler rolls back
        and re-raises, so a failure clearing a cache row would have reported a
        durably-committed import as failed. Same point-of-no-return rule as
        test_a_wrapped_cleanup_failure_does_not_report_a_committed_import_as_failed
        on the overwrite side - pinned here because nothing did."""
        db = self._dbWithCachedWrappedYears(2018, 2022)

        with patch.object(db.repo, "deleteUserWrappedFromYear", side_effect=RuntimeError("boom")):
            self._importAt(db, self._tsIn(2018))

        self.assertEqual(db.readProgress()["status"], "complete")
        playedAts = [e["playedAt"] for e in db.getEntriesFromOld(fullPagination=False)]
        self.assertIn(self._tsIn(2018), playedAts)   #< the commit stands
        self.assertEqual(self._cachedYears(db), {2018, 2022})   #< and the stale rows stay, repairably

    def test_an_import_that_inserts_nothing_keeps_the_cache(self):
        """A re-uploaded file whose plays are all already recorded rewrites
        nothing, so it must not throw away work either."""
        db = self._dbWithCachedWrappedYears(2018, 2022)
        self._importAt(db, self._tsIn(2018))
        db = self._dbWithCachedWrappedYears(2018, 2022)  #< re-prime after that first import

        def gen():
            return iter([])

        importer = MagicMock()
        importer._convertToList.return_value = ([], "spotifyAcountExport")
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
        importer.importHistory.return_value = gen()
        with patch("Database.database.Importer", return_value=importer):
            db.importHistory("raw export")

        self.assertEqual(self._cachedYears(db), {2018, 2022})


if __name__ == "__main__":
    import unittest
    unittest.main()
