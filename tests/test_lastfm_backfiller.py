"""The per-user Last.fm genre backfill worker: lifecycle, the artists->albums->
tracks cycle with genre inheritance, own-queue -> global-queue fallback,
definitive-vs-transient marking and cross-user in-flight dedup. The Last.fm
client is always mocked (conftest blocks real sockets anyway)."""
import sys
import os
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database, _LastfmInvalidKeyError
from Database.lastfm import FetchOutcome, OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY

OK_EMPTY = FetchOutcome(OUTCOME_OK, [])
ROCK_TAGS = FetchOutcome(OUTCOME_OK, [{"name": "rock", "count": 100},
                                      {"name": "seen live", "count": 90},
                                      {"name": "indie rock", "count": 80}])
OLD_API_KEY = "key-old"
ROTATED_API_KEY = "key-new"
STARTUP_DELAY_SECONDS = 17

LASTFM_LOOP_CONTRACTS = (
    {
        "name": "genre",
        "loop": "_lastfmGenreBackfillLoop",
        "event": "lastfm_stop_event",
        "enabled": "isLastfmGenreBackfillEnabled",
        "work": "_runLastfmCycle",
        "idle": "LASTFM_IDLE_WAIT_SECONDS",
        "telemetry": "lastfm_genre",
    },
    {
        "name": "artist_bio",
        "loop": "_lastfmBiographyBackfillLoop",
        "event": "lastfm_biography_stop_event",
        "enabled": "isArtistBioEnabled",
        "work": "_processLastfmBiographyBatch",
        "idle": "LASTFM_BIOGRAPHY_IDLE_WAIT_SECONDS",
        "telemetry": "lastfm_artist_bio",
    },
    {
        "name": "album_bio",
        "loop": "_lastfmAlbumBiographyBackfillLoop",
        "event": "lastfm_album_biography_stop_event",
        "enabled": "isAlbumBioEnabled",
        "work": "_processLastfmAlbumBiographyBatch",
        "idle": "LASTFM_ALBUM_BIOGRAPHY_IDLE_WAIT_SECONDS",
        "telemetry": "lastfm_album_bio",
    },
)


def _album(albumId, name=None):
    return {"id": albumId, "name": name or albumId, "url": "http://example.com/album",
            "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}


def _oneShotStopEvent():
    """Stand-in stop event for driving the loop exactly once: is_set() stays
    False, the first wait() (the startup delay) passes, any later wait (an
    idle/backoff wait) stops the loop - robust against how often the loop
    checks is_set() internally."""
    event = MagicMock()
    event.is_set.return_value = False
    calls = {"count": 0}

    def wait(timeout=None):
        calls["count"] += 1
        return calls["count"] > 1

    event.wait.side_effect = wait
    return event


class LastfmWorkerBase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        Database._lastfm_active.clear()
        self.addCleanup(Database._lastfm_active.clear)

    def _makeDbWithPlays(self, username="user1"):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}], "album": _album("alP", "Album P")},
            "tB": {"id": "tB", "name": "Song B",
                   "artists": [{"id": "aY", "name": "Artist Y"}], "album": _album("alQ", "Album Q")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 2000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 3000, "timePlayed": 5000},
        ]
        return self._makeDb(tracks, entries, username=username)


class WorkerLifecycleTestCase(LastfmWorkerBase):
    def test_without_a_key_start_is_a_noop(self):
        db = self._makeDbWithPlays()
        db.startLastfmGenreBackfiller()
        self.assertIsNone(db.lastfm_thread)

    def test_with_a_key_the_thread_starts_and_stop_joins_it(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        self.assertIsNotNone(db.lastfm_thread)
        self.assertTrue(db.lastfm_thread.is_alive())   #< sits in its random startup delay
        db.stopLastfmGenreBackfiller()
        self.assertIsNone(db.lastfm_thread)

    def test_duplicate_start_keeps_the_running_thread(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        firstThread = db.lastfm_thread
        db.startLastfmGenreBackfiller()
        self.assertIs(db.lastfm_thread, firstThread)
        db.stopLastfmGenreBackfiller()

    def test_concurrent_starts_never_orphan_a_running_thread(self):
        """The running-check and the thread/event assignment must happen as one
        step. Two concurrent starts (the profile page's key save, double-
        submitted) could both see no live thread; the second's assignments then
        overwrote the first's, orphaning a running thread on an Event no
        reference survived for - so no stop path could ever reach it, and it
        duplicated Last.fm traffic until the process exited."""
        import threading as threadingModule

        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        started = []

        def stubLoop(stop_event):
            # Stands in for the real loop: parks on the event this run was
            # handed, so "did stop reach every started thread?" is the only
            # thing under test (no Last.fm calls, no DB work).
            started.append(stop_event)
            stop_event.wait(10)

        barrier = threadingModule.Barrier(2)

        def start():
            barrier.wait()   #< maximize the overlap on the check-then-assign
            try:
                db.startLastfmGenreBackfiller()
            finally:
                # Connections are thread-local; the api-key read above opened
                # one on this racer thread, and only its own thread can close
                # it (an open handle keeps the temp db file locked on Windows).
                db.repo.connectionManager.close()

        with patch.object(db, "_lastfmGenreBackfillLoop", stubLoop):
            racers = [threadingModule.Thread(target=start) for _ in range(2)]
            for racer in racers:
                racer.start()
            for racer in racers:
                racer.join(timeout=5)

            self.assertEqual(len(started), 1, "a second worker thread was started for the same user")
            liveThread = db.lastfm_thread

            db.stopLastfmGenreBackfiller()

            for stopEvent in started:
                self.assertTrue(stopEvent.is_set(), "a started worker was orphaned - stop never reached it")
            liveThread.join(timeout=5)
            self.assertFalse(liveThread.is_alive())

    def test_restart_uses_a_fresh_stop_event_so_a_lingering_thread_cannot_revive(self):
        """stop() joins with a timeout - a worker blocked in a slow HTTP call
        can outlive it. A restart must NOT clear the event that zombie still
        watches (that would revive it, doubling the request volume forever);
        each run gets its own event instead."""
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        firstEvent = db.lastfm_stop_event
        db.stopLastfmGenreBackfiller()

        db.startLastfmGenreBackfiller()
        self.assertIsNot(db.lastfm_stop_event, firstEvent)
        self.assertTrue(firstEvent.is_set())            #< the old thread's signal stays set
        self.assertFalse(db.lastfm_stop_event.is_set())
        db.stopLastfmGenreBackfiller()

    def test_a_start_during_a_stops_join_keeps_its_thread_reference(self):
        """stopLastfmBiographyBackfiller kept a `self.lastfm_biography_thread =
        None` left over from its hand-rolled body, AFTER the shared helper had
        already nulled the attribute under the lock and then joined outside it.
        That trailing assignment ran unlocked, once the join returned - so it
        clobbered whatever a start arriving during the join (the profile page's
        remove-key then save-key) had assigned.

        The result was strictly worse than a duplicate thread: status reported
        `running: False`, _stopPeriodicWorker's `thread is None` branch then
        returned WITHOUT setting the stop event, and the next start overwrote the
        event too - a live worker doing Last.fm batches that no stop path could
        ever reach again.

        The interleaving is forced at the join rather than raced for: a test that
        waits on a clock to catch this catches it sometimes.
        """
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        def stubLoop(stop_event):
            stop_event.wait(10)

        with patch.object(db, "_lastfmBiographyBackfillLoop", stubLoop):
            db.startLastfmBiographyBackfiller()
            firstThread = db.lastfm_biography_thread
            self.assertIsNotNone(firstThread)

            realJoin = firstThread.join

            def joinWithAStartInFlight(timeout=None):
                # Stands in for a second request arriving while this stop is
                # blocked in its (bounded) join.
                db.startLastfmBiographyBackfiller()
                return realJoin(timeout=timeout)

            with patch.object(firstThread, "join", joinWithAStartInFlight):
                db.stopLastfmBiographyBackfiller()

            secondThread = db.lastfm_biography_thread
            self.assertIsNotNone(secondThread, "the concurrent start's thread reference was discarded")
            self.assertIsNot(secondThread, firstThread)
            self.assertTrue(db.getLastfmBiographyWorkerStatus()["running"])

            # And it must still be stoppable - the whole point of keeping the
            # reference.
            db.stopLastfmBiographyBackfiller()
            secondThread.join(timeout=5)
            self.assertFalse(secondThread.is_alive())
            self.assertIsNone(db.lastfm_biography_thread)

    def test_autostart_survives_a_pre_migration_schema(self):
        """Database() constructed against a pre-1.19 file outside the app's
        migration path (standalone script/REPL) must not crash in __init__
        just because users.lastfm_api_key doesn't exist yet."""
        import sqlite3 as sqlite3Module
        db = self._makeDbWithPlays()
        with patch.object(db.repo, "getUserLastfmApiKey",
                          side_effect=sqlite3Module.OperationalError("no such column: lastfm_api_key")):
            db.startLastfmGenreBackfiller()   #< must not raise
        self.assertIsNone(db.lastfm_thread)

    def test_database_stop_stops_the_worker(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.startLastfmGenreBackfiller()
        runningThread = db.lastfm_thread
        db.stop()
        self.assertFalse(runningThread.is_alive())
        self.assertIsNone(db.lastfm_thread)

    def test_init_autostarts_only_with_a_stored_key(self):
        withoutKey = self._makeDbWithPlays()
        self.assertIsNone(withoutKey.lastfm_thread)

        # A second instance over the same shared DB file sees the stored key.
        withoutKey.repo.updateUserLastfmApiKey("user1", "key123")
        dbPath = withoutKey.repo.connectionManager.dbPath
        withKey = Database("user1", dbPath=dbPath)
        self.addCleanup(withKey.repo.connectionManager.close)
        self.addCleanup(withKey.stop)
        self.assertIsNotNone(withKey.lastfm_thread)
        self.assertTrue(withKey.lastfm_thread.is_alive())

    def test_worker_status_reflects_key_and_thread(self):
        def withoutTelemetry(status):
            return {k: v for k, v in status.items()
                    if k not in ("consecutive_failures", "failure_rate", "last_error")}

        db = self._makeDbWithPlays()
        self.assertEqual(withoutTelemetry(db.getLastfmWorkerStatus()), {"configured": False, "running": False})
        db.repo.updateUserLastfmApiKey("user1", "key123")
        self.assertEqual(withoutTelemetry(db.getLastfmWorkerStatus()), {"configured": True, "running": False})
        db.startLastfmGenreBackfiller()
        self.assertEqual(withoutTelemetry(db.getLastfmWorkerStatus()), {"configured": True, "running": True})
        db.stopLastfmGenreBackfiller()
        self.assertEqual(withoutTelemetry(db.getLastfmWorkerStatus()), {"configured": True, "running": False})


class WorkerLoopTestCase(LastfmWorkerBase):
    @patch("Database.database.LastfmClient")
    def test_loop_without_a_key_makes_no_client(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()
        mockClientClass.assert_not_called()

    @patch("Database.database.LastfmClient")
    def test_one_cycle_processes_artists_albums_and_tracks(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistTopTags.return_value = ROCK_TAGS
        client.getAlbumTopTags.return_value = ROCK_TAGS
        client.getTrackTopTags.return_value = ROCK_TAGS
        mockClientClass.return_value = client

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        mockClientClass.assert_called_with("key123")
        self.assertEqual(db.repo.getArtistGenres("aX"), ["rock", "indie rock"])
        self.assertEqual(db.repo.getArtistGenres("aY"), ["rock", "indie rock"])
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alP")], ["rock", "indie rock"])
        self.assertEqual([g["genre"] for g in db.repo.getTrackGenres("tA")], ["rock", "indie rock"])
        self.assertFalse(any(g["inherited"] for g in db.repo.getTrackGenres("tA")))

        conn = db.repo._conn()
        for table, entityId in (("artists", "aX"), ("albums", "alP"), ("tracks", "tA")):
            stamp = conn.execute(f"SELECT lastfm_attempted_at FROM {table} WHERE id=?",
                                 (entityId,)).fetchone()["lastfm_attempted_at"]
            self.assertIsNotNone(stamp)

        # Priority order: most-played artist looked up first.
        firstArtistCall = client.getArtistTopTags.call_args_list[0]
        self.assertEqual(firstArtistCall.args[0], "Artist X")

    @patch("Database.database.LastfmClient")
    def test_loop_failure_then_success_updates_telemetry(self, mockClientClass):
        """A cycle that raises records a failure via _recordWorkerCycle; a
        later clean cycle resets consecutive_failures back to 0 (the else
        clause of the loop's try/except - see Database/workers/telemetry.py)."""
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        failingClient = MagicMock()
        failingClient.getArtistTopTags.side_effect = RuntimeError("Last.fm unreachable")
        mockClientClass.return_value = failingClient

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        telemetry = db._getWorkerTelemetry("lastfm_genre")
        self.assertEqual(telemetry["consecutive_failures"], 1)
        self.assertIn("Last.fm unreachable", telemetry["last_error"])

        succeedingClient = MagicMock()
        succeedingClient.getArtistTopTags.return_value = ROCK_TAGS
        succeedingClient.getAlbumTopTags.return_value = ROCK_TAGS
        succeedingClient.getTrackTopTags.return_value = ROCK_TAGS
        mockClientClass.return_value = succeedingClient

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        telemetry = db._getWorkerTelemetry("lastfm_genre")
        self.assertEqual(telemetry["consecutive_failures"], 0)

    @patch("Database.database.LastfmClient")
    def test_own_queue_is_drained_before_the_global_queue(self, mockClientClass):
        db = self._makeDbWithPlays(username="user1")
        db.repo.updateUserLastfmApiKey("user1", "key123")
        # Another user's played entities exist and are missing genres too.
        db.repo.upsertUser("user2", "user2@example.com")
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C", "artists": [{"id": "aZ", "name": "Artist Z"}],
             "album": _album("alR", "Album R")}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()
        # user1's own entities are all definitively attempted already.
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        db.repo.markAlbumsLastfmAttempted(["alP", "alQ"])
        db.repo.markTracksLastfmAttempted(["tA", "tB"])

        client = MagicMock()
        client.getArtistTopTags.return_value = ROCK_TAGS
        client.getAlbumTopTags.return_value = ROCK_TAGS
        client.getTrackTopTags.return_value = ROCK_TAGS
        mockClientClass.return_value = client

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        # The global fallback fetched user2's artist even though user1 is done.
        self.assertEqual(db.repo.getArtistGenres("aZ"), ["rock", "indie rock"])

    @patch("Database.database.LastfmClient")
    def test_disabled_kill_switch_idles_the_loop_without_reading_the_key(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.setLastfmGenreBackfillEnabled(False)

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()

        mockClientClass.assert_not_called()
        self.assertIsNone(db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id='aX'").fetchone()[0])

    @patch("Database.database.LastfmClient")
    def test_invalid_key_idles_the_loop_instead_of_hammering(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")

        client = MagicMock()
        client.getArtistTopTags.return_value = FetchOutcome(OUTCOME_INVALID_KEY, [])
        mockClientClass.return_value = client

        db.lastfm_stop_event = _oneShotStopEvent()
        db._lastfmGenreBackfillLoop()   #< must terminate cleanly via the idle wait

        client.getArtistTopTags.assert_called_once()   #< first invalid response stops the batch
        self.assertEqual(db.repo.getArtistGenres("aX"), [])
        stamp = db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id='aX'").fetchone()[0]
        self.assertIsNone(stamp)   #< nothing marked - a fixed key retries everything


class LastfmLoopPolicyContractTestCase(LastfmWorkerBase):
    """Loop policy shared by the three Last.fm workers.

    Entity processors stay separate; this pins only the orchestration the
    runner is allowed to share.
    """

    def _dbWithKey(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", OLD_API_KEY)
        return db

    def _event(self, *, waits, isSet=False):
        event = MagicMock()
        event.wait.side_effect = list(waits)
        event.is_set.return_value = isSet
        return event

    def _loop(self, db, contract, event):
        getattr(db, contract["loop"])(event)

    def _patchWork(self, db, contract, side_effect):
        return patch.object(db, contract["work"], side_effect=side_effect)

    def _waitDurations(self, event):
        return [call.args[0] if call.args else None for call in event.wait.call_args_list]

    @patch("Database.database.LastfmClient")
    def test_every_loop_stops_during_startup_before_reading_the_key(self, mockClientClass):
        for contract in LASTFM_LOOP_CONTRACTS:
            with self.subTest(worker=contract["name"]):
                db = self._dbWithKey()
                db.repo.getUserLastfmApiKey = MagicMock(return_value=OLD_API_KEY)
                event = self._event(waits=[True])

                with patch("random.randint", return_value=STARTUP_DELAY_SECONDS), \
                        self._patchWork(db, contract, side_effect=AssertionError("work must not run")):
                    self._loop(db, contract, event)

                db.repo.getUserLastfmApiKey.assert_not_called()
                mockClientClass.assert_not_called()
                self.assertEqual(self._waitDurations(event), [STARTUP_DELAY_SECONDS])
                mockClientClass.reset_mock()

    @patch("Database.database.LastfmClient")
    def test_every_loop_idles_when_disabled_without_reading_the_key(self, mockClientClass):
        for contract in LASTFM_LOOP_CONTRACTS:
            with self.subTest(worker=contract["name"]):
                db = self._dbWithKey()
                getattr(db.repo, contract["enabled"])
                setattr(db.repo, contract["enabled"], MagicMock(return_value=False))
                db.repo.getUserLastfmApiKey = MagicMock(return_value=OLD_API_KEY)
                event = self._event(waits=[False, True])

                with patch("random.randint", return_value=STARTUP_DELAY_SECONDS), \
                        self._patchWork(db, contract, side_effect=AssertionError("work must not run")):
                    self._loop(db, contract, event)

                db.repo.getUserLastfmApiKey.assert_not_called()
                mockClientClass.assert_not_called()
                self.assertIn(getattr(db, contract["idle"]), self._waitDurations(event))
                mockClientClass.reset_mock()

    @patch("Database.database.LastfmClient")
    def test_every_loop_exits_when_the_key_has_been_removed(self, mockClientClass):
        for contract in LASTFM_LOOP_CONTRACTS:
            with self.subTest(worker=contract["name"]):
                db = self._dbWithKey()
                db.repo.getUserLastfmApiKey = MagicMock(return_value=None)
                event = self._event(waits=[False])

                with patch("random.randint", return_value=STARTUP_DELAY_SECONDS), \
                        self._patchWork(db, contract, side_effect=AssertionError("work must not run")):
                    self._loop(db, contract, event)

                db.repo.getUserLastfmApiKey.assert_called_once_with(db.user)
                mockClientClass.assert_not_called()
                self.assertEqual(self._waitDurations(event), [STARTUP_DELAY_SECONDS])
                mockClientClass.reset_mock()

    @patch("Database.database.LastfmClient")
    def test_every_loop_rereads_a_rotated_key_each_cycle(self, mockClientClass):
        for contract in LASTFM_LOOP_CONTRACTS:
            with self.subTest(worker=contract["name"]):
                db = self._dbWithKey()
                db.repo.getUserLastfmApiKey = MagicMock(side_effect=[OLD_API_KEY, ROTATED_API_KEY])
                event = self._event(waits=[False, False, True])

                with patch("random.randint", return_value=STARTUP_DELAY_SECONDS), \
                        self._patchWork(db, contract, side_effect=[False, False, False, False]):
                    self._loop(db, contract, event)

                self.assertEqual([call.args[0] for call in mockClientClass.call_args_list],
                                 [OLD_API_KEY, ROTATED_API_KEY])
                self.assertEqual(db.repo.getUserLastfmApiKey.call_count, 2)
                mockClientClass.reset_mock()

    @patch("Database.database.LastfmClient")
    def test_every_loop_tries_own_scope_before_global_scope(self, mockClientClass):
        for contract in LASTFM_LOOP_CONTRACTS:
            with self.subTest(worker=contract["name"]):
                db = self._dbWithKey()
                event = self._event(waits=[False, False])  # startup, then productive pause
                event.is_set.side_effect = [False, False, True]
                scopes = []

                def work(client, scopeUsername, stop_event=None):
                    scopes.append(scopeUsername)
                    return scopeUsername is None

                with patch("random.randint", return_value=STARTUP_DELAY_SECONDS), \
                        self._patchWork(db, contract, side_effect=work):
                    self._loop(db, contract, event)

                self.assertEqual(scopes, [db.user, None])
                mockClientClass.reset_mock()

    @patch("Database.database.LastfmClient")
    def test_every_loop_records_invalid_key_on_its_own_telemetry_and_waits(self, mockClientClass):
        for contract in LASTFM_LOOP_CONTRACTS:
            with self.subTest(worker=contract["name"]):
                db = self._dbWithKey()
                event = self._event(waits=[False, True])

                with patch("random.randint", return_value=STARTUP_DELAY_SECONDS), \
                        self._patchWork(db, contract, side_effect=_LastfmInvalidKeyError):
                    self._loop(db, contract, event)

                telemetry = db._getWorkerTelemetry(contract["telemetry"])
                self.assertEqual(telemetry["consecutive_failures"], 1)
                self.assertEqual(telemetry["last_error"], "Invalid Last.fm API key")
                self.assertIn(getattr(db, contract["idle"]), self._waitDurations(event))
                mockClientClass.reset_mock()

    @patch("Database.database.LastfmClient")
    def test_every_loop_records_empty_cycles_as_success_and_waits(self, mockClientClass):
        for contract in LASTFM_LOOP_CONTRACTS:
            with self.subTest(worker=contract["name"]):
                db = self._dbWithKey()
                event = self._event(waits=[False, True])

                with patch("random.randint", return_value=STARTUP_DELAY_SECONDS), \
                        self._patchWork(db, contract, side_effect=[False, False]):
                    self._loop(db, contract, event)

                telemetry = db._getWorkerTelemetry(contract["telemetry"])
                self.assertEqual(telemetry["consecutive_failures"], 0)
                self.assertIsNone(telemetry["last_error"])
                self.assertIn(getattr(db, contract["idle"]), self._waitDurations(event))
                mockClientClass.reset_mock()


class WorkerBatchTestCase(LastfmWorkerBase):
    """_processLastfm*Batch details, driven directly with a real (unset)
    stop event and a crafted client."""

    def _clientReturning(self, **methodOutcomes):
        client = MagicMock()
        for method, outcome in methodOutcomes.items():
            getattr(client, method).return_value = outcome
        return client

    def test_track_with_own_tags_stores_them_uninherited(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getTrackTopTags=ROCK_TAGS)
        db._processLastfmTrackBatch(client, "user1")
        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "rock", "inherited": False},
                          {"genre": "indie rock", "inherited": False}])
        client.getTrackTopTags.assert_any_call("Artist X", "Song A", stop_event=db.lastfm_stop_event)

    def test_tagless_track_inherits_from_a_finished_artist(self):
        db = self._makeDbWithPlays()
        db.repo.replaceArtistGenres("aX", ["shoegaze", "dream pop"])
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        client = self._clientReturning(getTrackTopTags=FetchOutcome(OUTCOME_NOT_FOUND, []))

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "shoegaze", "inherited": True},
                          {"genre": "dream pop", "inherited": True}])
        # tB's artist aY was attempted but has no genres -> marked bare.
        self.assertEqual(db.repo.getTrackGenres("tB"), [])
        conn = db.repo._conn()
        self.assertIsNotNone(conn.execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tB'").fetchone()[0])

    def test_tagless_track_resolves_its_pending_artist_inline(self):
        """A tag-less entity whose artist has no definitive result yet must
        resolve the artist with one inline request instead of staying
        unmarked - the artist may never appear in any queue (see the album
        test below), and an unmarked entity is re-fetched every cycle."""
        db = self._makeDbWithPlays()   #< artists never attempted
        client = self._clientReturning(getTrackTopTags=OK_EMPTY, getArtistTopTags=ROCK_TAGS)

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getArtistGenres("aX"), ["rock", "indie rock"])
        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "rock", "inherited": True},
                          {"genre": "indie rock", "inherited": True}])
        conn = db.repo._conn()
        for table, entityId in (("tracks", "tA"), ("artists", "aX")):
            self.assertIsNotNone(conn.execute(
                f"SELECT lastfm_attempted_at FROM {table} WHERE id=?", (entityId,)).fetchone()[0])

    def test_tagless_track_stays_unmarked_when_the_inline_artist_lookup_fails(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getTrackTopTags=OK_EMPTY,
                                       getArtistTopTags=FetchOutcome(OUTCOME_TRANSIENT, []))

        db._processLastfmTrackBatch(client, "user1")

        conn = db.repo._conn()
        self.assertIsNone(conn.execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tA'").fetchone()[0])
        self.assertEqual(db.repo.getTrackGenres("tA"), [])   #< requeues next cycle

    def test_album_with_an_unplayed_primary_artist_is_resolved_in_one_pass(self):
        """Starvation regression: an album's derived primary artist can come
        from never-played sibling tracks, so it never enters the artist queue.
        Waiting for it would leave the album unmarked (re-fetched every cycle,
        permanently occupying a batch slot) - the inline resolution must
        finish it in a single pass."""
        db = self._makeDbWithPlays()
        for trackId in ("tA2", "tA3"):   #< aM outvotes aX as alP's primary artist, but was never played
            db.repo.upsertTrack(normalizeTrackForTest(
                {"id": trackId, "name": trackId,
                 "artists": [{"id": "aM", "name": "Artist M"}], "album": _album("alP", "Album P")}))
        db.repo.commit()
        client = self._clientReturning(getAlbumTopTags=OK_EMPTY, getArtistTopTags=ROCK_TAGS)

        db._processLastfmAlbumBatch(client, "user1")

        client.getArtistTopTags.assert_any_call("Artist M", stop_event=db.lastfm_stop_event,
                                                timeout=None)   #< workers stay unbounded; only request threads pass one
        self.assertEqual(db.repo.getArtistGenres("aM"), ["rock", "indie rock"])
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alP")], ["rock", "indie rock"])
        self.assertTrue(all(g["inherited"] for g in db.repo.getAlbumGenres("alP")))
        conn = db.repo._conn()
        self.assertIsNotNone(conn.execute(
            "SELECT lastfm_attempted_at FROM albums WHERE id='alP'").fetchone()[0])

    def test_transient_outcomes_leave_entities_unmarked_and_report_no_progress(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getArtistTopTags=FetchOutcome(OUTCOME_TRANSIENT, []))

        processed = db._processLastfmArtistBatch(client, "user1")

        self.assertFalse(processed)   #< transient-only batches idle the loop instead of spinning
        conn = db.repo._conn()
        self.assertIsNone(conn.execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id='aX'").fetchone()[0])

    def test_album_lookup_uses_the_derived_primary_artist(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getAlbumTopTags=ROCK_TAGS)
        db._processLastfmAlbumBatch(client, "user1")
        client.getAlbumTopTags.assert_any_call("Artist X", "Album P", stop_event=db.lastfm_stop_event)
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alP")], ["rock", "indie rock"])

    def test_album_without_a_derivable_artist_is_marked_without_a_lookup(self):
        tracks = {"tOrphan": {"id": "tOrphan", "name": "No Artist", "artists": [],
                              "album": _album("alOrphan", "Orphan Album")}}
        entries = [{"id": "tOrphan", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries, username="user1")
        client = self._clientReturning(getAlbumTopTags=ROCK_TAGS)

        db._processLastfmAlbumBatch(client, "user1")

        client.getAlbumTopTags.assert_not_called()
        stamp = db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM albums WHERE id='alOrphan'").fetchone()[0]
        self.assertIsNotNone(stamp)

    def test_entities_claimed_by_another_worker_are_skipped_and_kept(self):
        db = self._makeDbWithPlays()
        Database._lastfm_active.add(("artist", "aX"))
        client = self._clientReturning(getArtistTopTags=ROCK_TAGS)

        db._processLastfmArtistBatch(client, "user1")

        self.assertEqual(db.repo.getArtistGenres("aX"), [])          #< skipped
        self.assertEqual(db.repo.getArtistGenres("aY"), ["rock", "indie rock"])
        self.assertIn(("artist", "aX"), Database._lastfm_active)     #< other worker's claim intact
        self.assertNotIn(("artist", "aY"), Database._lastfm_active)  #< own claim released

    def test_claims_are_released_even_when_processing_raises(self):
        db = self._makeDbWithPlays()
        client = MagicMock()
        client.getArtistTopTags.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            db._processLastfmArtistBatch(client, "user1")

        self.assertNotIn(("artist", "aX"), Database._lastfm_active)
        self.assertNotIn(("artist", "aY"), Database._lastfm_active)

    def test_aborted_rate_limit_slot_ends_the_batch(self):
        db = self._makeDbWithPlays()
        client = self._clientReturning(getArtistTopTags=None)   #< acquire() aborted
        processed = db._processLastfmArtistBatch(client, "user1")
        self.assertFalse(processed)


class CleanedNameRetryTestCase(LastfmWorkerBase):
    """A definitive-empty lookup for a decorated Spotify name ("Song - Radio
    Edit", "Song (feat. X)") re-asks Last.fm once with the cleaned form."""

    DECORATED_NAME = "Blood (with Foy Vance) [Drezo Remix]"

    def _makeDbWithDecoratedTrack(self):
        tracks = {
            "tD": {"id": "tD", "name": self.DECORATED_NAME,
                   "artists": [{"id": "aX", "name": "Artist X"}], "album": _album("alP", "Album P")},
        }
        entries = [{"id": "tD", "playedAt": 1000, "timePlayed": 5000}]
        return self._makeDb(tracks, entries, username="user1")

    def test_empty_result_for_a_decorated_track_retries_with_the_cleaned_name(self):
        db = self._makeDbWithDecoratedTrack()
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, ROCK_TAGS]

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual([call.args for call in client.getTrackTopTags.call_args_list],
                         [("Artist X", self.DECORATED_NAME), ("Artist X", "Blood")])
        self.assertEqual(db.repo.getTrackGenres("tD"),
                         [{"genre": "rock", "inherited": False},
                          {"genre": "indie rock", "inherited": False}])

    def test_undecorated_names_get_no_retry(self):
        db = self._makeDbWithPlays()
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])   #< bare artists: no inline lookups
        client = MagicMock()
        client.getTrackTopTags.return_value = OK_EMPTY

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(client.getTrackTopTags.call_count, 2)   #< one per track, no retries

    def test_transient_retry_leaves_the_track_unmarked(self):
        db = self._makeDbWithDecoratedTrack()
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, FetchOutcome(OUTCOME_TRANSIENT, [])]

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tD"), [])
        self.assertIsNone(db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tD'").fetchone()[0])

    def test_aborted_retry_slot_ends_the_batch_with_the_track_unmarked(self):
        db = self._makeDbWithDecoratedTrack()
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, None]   #< acquire() aborted on the retry

        processed = db._processLastfmTrackBatch(client, "user1")

        self.assertFalse(processed)
        self.assertIsNone(db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id='tD'").fetchone()[0])

    def test_empty_retry_still_falls_through_to_inheritance(self):
        db = self._makeDbWithDecoratedTrack()
        db.repo.replaceArtistGenres("aX", ["shoegaze"])
        db.repo.markArtistsLastfmAttempted(["aX"])
        client = MagicMock()
        client.getTrackTopTags.side_effect = [OK_EMPTY, OK_EMPTY]

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tD"),
                         [{"genre": "shoegaze", "inherited": True}])

    def test_decorated_album_names_retry_too(self):
        tracks = {
            "tE": {"id": "tE", "name": "Song E",
                   "artists": [{"id": "aX", "name": "Artist X"}],
                   "album": _album("alD", "Album D (Deluxe Edition)")},
        }
        entries = [{"id": "tE", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries, username="user1")
        client = MagicMock()
        client.getAlbumTopTags.side_effect = [OK_EMPTY, ROCK_TAGS]

        db._processLastfmAlbumBatch(client, "user1")

        self.assertEqual([call.args for call in client.getAlbumTopTags.call_args_list],
                         [("Artist X", "Album D (Deluxe Edition)"), ("Artist X", "Album D")])
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alD")],
                         ["rock", "indie rock"])
        self.assertFalse(any(g["inherited"] for g in db.repo.getAlbumGenres("alD")))


class AlbumFirstInheritanceTestCase(LastfmWorkerBase):
    """A tag-less track inherits its album's OWN genres before falling back
    to the primary artist's - album tags are the closer granularity."""

    def test_tagless_track_prefers_album_own_genres_over_artist(self):
        db = self._makeDbWithPlays()
        db.repo.replaceAlbumGenres("alP", ["progressive rock"], inherited=False)
        db.repo.markAlbumsLastfmAttempted(["alP"])
        db.repo.replaceArtistGenres("aX", ["shoegaze"])
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        client = MagicMock()
        client.getTrackTopTags.return_value = OK_EMPTY

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "progressive rock", "inherited": True}])
        client.getArtistTopTags.assert_not_called()

    def test_album_inherited_genres_do_not_cascade_to_tracks(self):
        """An album whose own lookup was empty carries artist genres as
        inherited rows - those must not masquerade as album tags for its
        tracks (the artist fallback covers that case directly)."""
        db = self._makeDbWithPlays()
        db.repo.replaceAlbumGenres("alP", ["stale artist genre"], inherited=True)
        db.repo.replaceArtistGenres("aX", ["dream pop"])
        db.repo.markArtistsLastfmAttempted(["aX", "aY"])
        client = MagicMock()
        client.getTrackTopTags.return_value = OK_EMPTY

        db._processLastfmTrackBatch(client, "user1")

        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "dream pop", "inherited": True}])


class RunEventThreadingTestCase(LastfmWorkerBase):
    """The fresh-event-per-run invariant (Database/workers/periodic.py): stop
    joins for only 3s, then start assigns a FRESH unset event - so a zombie
    batch still running from the old thread must obey its own run's event.
    The batch helpers used to read self.lastfm_stop_event instead, which
    after a restart (the profile page's key save) is the new event: the
    zombie never broke early and ran a whole batch of HTTP lookups alongside
    the new worker."""

    @patch("Database.database.LastfmClient")
    def test_the_loop_hands_its_private_event_to_every_cycle(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        mockClientClass.return_value = MagicMock()
        captured = []

        def capture(client, scope, stop_event=None):
            captured.append(stop_event)
            return False

        privateEvent = _oneShotStopEvent()
        with patch.object(db, "_runLastfmCycle", side_effect=capture):
            db._lastfmGenreBackfillLoop(privateEvent)

        self.assertEqual(len(captured), 2)   #< own queue, then the global fallback
        self.assertTrue(all(ev is privateEvent for ev in captured))

    def test_the_cycle_hands_the_event_to_every_batch(self):
        db = self._makeDbWithPlays()
        captured = []

        def capture(client, scope, stop_event=None):
            captured.append(stop_event)
            return False

        privateEvent = MagicMock()
        privateEvent.is_set.return_value = False
        with patch.object(db, "_processLastfmArtistBatch", side_effect=capture), \
                patch.object(db, "_processLastfmAlbumBatch", side_effect=capture), \
                patch.object(db, "_processLastfmTrackBatch", side_effect=capture):
            db._runLastfmCycle(MagicMock(), "user1", stop_event=privateEvent)

        self.assertEqual(len(captured), 3)
        self.assertTrue(all(ev is privateEvent for ev in captured))

    def test_a_set_run_event_stops_the_artist_batch_despite_a_fresh_attribute(self):
        db = self._makeDbWithPlays()
        self.assertTrue(db.repo.getArtistsMissingGenres(10, "user1"))   #< real rows to walk
        oldEvent = threading.Event()
        oldEvent.set()                              #< this run was stopped
        db.lastfm_stop_event = threading.Event()    #< the restart's fresh, unset event
        client = MagicMock()

        processed = db._processLastfmArtistBatch(client, "user1", stop_event=oldEvent)

        self.assertFalse(processed)
        client.getArtistTopTags.assert_not_called()


if __name__ == "__main__":
    unittest.main()
