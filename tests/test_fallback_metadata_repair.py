"""Repair degraded catalog rows without re-recording confirmed listening."""
import datetime
import json
import sqlite3
import time
from unittest.mock import MagicMock, patch

from conftest import DatabaseTestCase, RecordingConnection
from Database.Formatters.spotifyClient import Client
from Database.Listeners.spotifyListener import Listener
from Database.Spotify.client import fallbackTrackRecord
from Database.db import RESTRICTED_FALLBACK_REASON
from Database.database import Database
from Database.repository import TRACK_ISRC_RETRY_SECONDS
from Database.utils import timeToInt
from test_api_backfill import _MONOTONIC_NOW
from test_playback_recovery import PLAYED_AT, TRACK_DURATION_MS
from test_spotify_client_contract import buildClient
from test_track_isrc_backfill import REAL_ID, REAL_ID_2, FABRICATED_ID

LISTENED_MS = 10000
CONTEXT_URI = "spotify:playlist:context"
QUEUE_LIMIT = 50
BACKFILL_COPY_OFFSET_SECONDS = 2
WRAPPED_YEAR = 2025
WRAPPED_PLAY_MONTH = 6
WRAPPED_SNAPSHOT_TABLES = (
    "tracks", "albums", "artists", "track_artists", "plays", "app_settings", "user_wrapped",
)


def catalogTrack(trackId=REAL_ID):
    return {
        "id": trackId, "name": "Recovered song", "duration_ms": TRACK_DURATION_MS,
        "external_urls": {"spotify": f"https://open.spotify.com/track/{trackId}"},
        "external_ids": {"isrc": "TESTISRC"},
        "album": {"id": "realAlbum", "name": "Recovered album", "release_date": "2020-01-01"},
        "artists": [{"id": "realArtist", "name": "Recovered artist"}],
    }


class TestFallbackMetadataRepair(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])
        self.conn = self.db.repo._conn()

    def _recordFailedLookup(self, trackId=REAL_ID):
        client = buildClient()
        with patch.object(client, "track", side_effect=TimeoutError("lookup unavailable")):
            client._addToRecentlyPlayed(f"spotify:track:{trackId}", PLAYED_AT, CONTEXT_URI, LISTENED_MS)
        with patch.object(self.db, "saveImagesFromTrack"), patch.object(self.db, "updatePlaylists"):
            self.db._addToDatabaseFromListener(client.current_user_recently_played())
        assert self.conn.execute("SELECT created_reason FROM tracks WHERE id=?", (trackId,)).fetchone()[0] == RESTRICTED_FALLBACK_REASON

    def _plays(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM plays ORDER BY track_id, played_at")]

    def _catalogPoll(self, payload):
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        stop = MagicMock()
        stop.is_set.return_value = False
        with patch("requests.get", return_value=response) as get:
            self.db._backfillTrackIsrcs(lambda: "token", stop)
        return get

    def test_confirmed_play_repairs_through_full_history_snapshot(self):
        self._recordFailedLookup()
        before = self._plays()
        with patch("Database.Listeners.spotifyListener.Spotify") as spotify:
            spotify.return_value.current_user_recently_played.return_value = []
            listener = Listener("dummy", email="alice@example.test",
                                get_credentials=lambda: {"client_id": "cid", "client_secret": "cs", "refresh_token": "rt"},
                                process_backfill_page=self.db.process_backfill_page)
        callback = MagicMock(wraps=self.db._addToDatabaseFromListener)
        items = [{"track": catalogTrack(), "played_at": PLAYED_AT}]
        with patch("Database.Listeners.spotifyListener._get_current_user_from_web_api", return_value={"id": "alice", "email": "alice@example.test"}), \
             patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=items), \
             patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token"), \
             patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback, onWebApiSnapshot=self.db._reconcileWithWebApiHistory)
        callback.assert_not_called()
        row = self.conn.execute("SELECT * FROM tracks WHERE id=?", (REAL_ID,)).fetchone()
        assert (row["name"], row["duration_ms"], row["album_id"], row["created_reason"]) == (
            "Recovered song", TRACK_DURATION_MS, "realAlbum", None)
        assert self.conn.execute("SELECT artist_id FROM track_artists WHERE track_id=?", (REAL_ID,)).fetchone()[0] == "realArtist"
        assert self._plays() == before

    def test_catalog_repairs_fallback_even_when_isrc_already_recorded(self):
        self._recordFailedLookup()
        self.db.repo.updateTrackIsrcs({REAL_ID: "OLDISRC"})
        before = self._plays()
        get = self._catalogPoll(catalogTrack())
        get.assert_called_once()
        assert self.db.repo.getTrack(REAL_ID)["name"] == "Recovered song"
        assert self._plays() == before
        assert self.db.repo.getTracksMissingIsrc(QUEUE_LIMIT) == []

    def test_incomplete_catalog_response_keeps_placeholder_eligible_after_retry_window(self):
        self._recordFailedLookup()
        self._catalogPoll({"external_ids": {"isrc": "TESTISRC"}})
        assert self.db.repo.getTrack(REAL_ID)["name"] != "Recovered song"
        assert self.db.repo.getTracksMissingIsrc(QUEUE_LIMIT) == []
        with patch("Database.queries.tracks.time.time", return_value=time.time() + TRACK_ISRC_RETRY_SECONDS):
            assert self.db.repo.getTracksMissingIsrc(QUEUE_LIMIT) == [REAL_ID]

    def test_repair_does_not_insert_unknown_tracks_or_overwrite_real_metadata(self):
        self.db.repo.upsertTrack(Client.formatTrack(catalogTrack(), embedPlaybackInfo=False))
        self.db.repo.commit()
        before = self.conn.total_changes
        changed = catalogTrack()
        changed["name"] = "Stale response"
        assert self.db._repairFallbackTrackMetadata([changed, catalogTrack(REAL_ID_2)]) == 0
        assert self.db.repo.getTrack(REAL_ID)["name"] == "Recovered song"
        assert self.db.repo.getTrack(REAL_ID_2) is None
        assert self.conn.total_changes == before

    def test_incomplete_or_fallback_payload_cannot_clear_marker(self):
        self._recordFailedLookup()
        invalid = [None, {}, fallbackTrackRecord(REAL_ID)]
        for key, value in (("name", ""), ("duration_ms", 0), ("album", None), ("external_urls", None)):
            item = catalogTrack()
            item[key] = value
            invalid.append(item)
        assert self.db._repairFallbackTrackMetadata(invalid) == 0
        assert self.conn.execute("SELECT created_reason FROM tracks WHERE id=?", (REAL_ID,)).fetchone()[0] == RESTRICTED_FALLBACK_REASON

    def test_malformed_item_does_not_cost_valid_metadata_its_repair(self):
        self._recordFailedLookup()
        malformed = catalogTrack()
        malformed["duration_ms"] = "invalid duration"
        assert self.db._repairFallbackTrackMetadata(["invalid track", malformed, catalogTrack()]) == 1
        assert self.db.repo.getTrack(REAL_ID)["name"] == "Recovered song"

    def test_incomplete_album_or_credits_keep_catalog_repair_eligible(self):
        for key, value in (("album", {"id": "realAlbum"}), ("artists", []),
                           ("artists", [{"name": "Missing identity"}]),
                           ("artists", [{"id": "realArtist", "name": ""}])):
            with self.subTest(key=key, value=value):
                self.db = self._makeDb({}, [])
                self.conn = self.db.repo._conn()
                self._recordFailedLookup()
                partial = catalogTrack()
                partial[key] = value
                self._catalogPoll(partial)
                assert self.conn.execute("SELECT created_reason FROM tracks WHERE id=?", (REAL_ID,)).fetchone()[0] == RESTRICTED_FALLBACK_REASON
                assert self.db.repo.getTracksMissingIsrc(QUEUE_LIMIT) == []
                with patch("Database.queries.tracks.time.time", return_value=time.time() + TRACK_ISRC_RETRY_SECONDS):
                    assert self.db.repo.getTracksMissingIsrc(QUEUE_LIMIT) == [REAL_ID]
                with self.conn:
                    self.conn.execute("UPDATE tracks SET isrc_attempted_at=NULL WHERE id=?", (REAL_ID,))
        self._catalogPoll(catalogTrack())
        assert self.db.repo.getTrack(REAL_ID)["name"] == "Recovered song"

    def test_album_credits_can_repair_track_without_its_own_credits(self):
        self._recordFailedLookup()
        payload = catalogTrack()
        payload["album"]["artists"] = payload.pop("artists")
        assert self.db._repairFallbackTrackMetadata([payload]) == 1
        assert self.conn.execute("SELECT artist_id FROM track_artists WHERE track_id=?", (REAL_ID,)).fetchone()[0] == "realArtist"

    def test_empty_and_fallback_only_repairs_do_not_write(self):
        self._recordFailedLookup()
        before = self.conn.total_changes
        assert self.db._repairFallbackTrackMetadata([]) == 0
        assert self.db.repo.repairFallbackTracks([
            Client.formatTrack(fallbackTrackRecord(REAL_ID), embedPlaybackInfo=False)]) is None
        assert self.conn.total_changes == before

    def test_repeated_metadata_repairs_the_track_once(self):
        self._recordFailedLookup()
        assert self.db._repairFallbackTrackMetadata([catalogTrack(), catalogTrack()]) == 1
        assert self.db._repairFallbackTrackMetadata([catalogTrack()]) == 0

    def test_failed_repair_does_not_prevent_proven_duplicate_cleanup(self):
        self._recordFailedLookup()
        original = self._plays()
        self.db.repo.insertPlay(self.db.user, REAL_ID, timeToInt(PLAYED_AT) + BACKFILL_COPY_OFFSET_SECONDS,
                               TRACK_DURATION_MS, created_reason=self.db.WEB_API_BACKFILL_SOURCE)
        self.db.repo.commit()
        assert len(self._plays()) == len(original) + 1
        with patch.object(self.db.repo, "repairFallbackTracks", side_effect=RuntimeError("write interrupted")):
            self.db._reconcileWithWebApiHistory([{"track": catalogTrack(), "played_at": PLAYED_AT}])
        assert self._plays() == original

    def test_repair_rechecks_fallback_under_write_lock_and_rolls_back_whole_batch(self):
        for trackId in (REAL_ID, REAL_ID_2):
            self._recordFailedLookup(trackId)
        before = [dict(row) for row in self.conn.execute("SELECT * FROM tracks ORDER BY id")]
        statements = []
        recording = RecordingConnection(self.conn, statements)
        privateWriter = self.db.repo._upsertTrackWithExisting

        def failSecond(conn, track, createdReason, existing):
            impact = privateWriter(conn, track, createdReason, existing)
            if track["id"] == REAL_ID_2:
                raise RuntimeError("write interrupted")
            return impact

        with patch.object(self.db.repo, "_conn", return_value=recording), \
             patch.object(self.db.repo, "_upsertTrackWithExisting", side_effect=failSecond):
            with self.assertRaisesRegex(RuntimeError, "write interrupted"):
                self.db._repairFallbackTrackMetadata([catalogTrack(), catalogTrack(REAL_ID_2)])
        reads = [(sql, locked) for sql, locked in statements if sql.startswith("SELECT") and "FROM tracks" in sql]
        assert reads and all(locked for _, locked in reads)
        assert [dict(row) for row in self.conn.execute("SELECT * FROM tracks ORDER BY id")] == before
        assert not self.conn.in_transaction

    def test_fabricated_fallback_ids_stay_out_of_catalog_queue(self):
        self.db.repo.upsertTrack(Client.formatTrack(fallbackTrackRecord(FABRICATED_ID), embedPlaybackInfo=False))
        self.db.repo.commit()
        assert self.db.repo.getTracksMissingIsrc(QUEUE_LIMIT) == []

    def test_mismatched_catalog_id_does_not_repair_requested_track(self):
        self._recordFailedLookup()
        self._catalogPoll(catalogTrack(REAL_ID_2))
        assert self.conn.execute("SELECT created_reason FROM tracks WHERE id=?", (REAL_ID,)).fetchone()[0] == RESTRICTED_FALLBACK_REASON
        assert self.db.repo.getTrack(REAL_ID_2) is None


class TestFallbackRepairWrappedCache(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])

    def _seedFallback(self, trackId=REAL_ID):
        self.db.repo.upsertTrack(Client.formatTrack(fallbackTrackRecord(trackId), embedPlaybackInfo=False))
        self._play(self.db, trackId, WRAPPED_YEAR)

    def _play(self, db, trackId, year):
        playedAt = datetime.datetime(year, WRAPPED_PLAY_MONTH, 1, tzinfo=db.tz).timestamp()
        db.repo.insertPlay(db.user, trackId, playedAt, TRACK_DURATION_MS)
        db.repo.commit()

    def _cache(self, db=None, year=WRAPPED_YEAR):
        db = db or self.db
        db.recalculateWrappedForYear(year)
        cached = db.repo.getCachedWrapped(db.user, year)
        self.assertIsNotNone(cached)
        return cached

    def _committedState(self):
        # Verify durable state from an independent reader, including shared
        # metadata and credits, not just the repairing connection's view.
        conn = sqlite3.connect(self.db.repo.connectionManager.dbPath)
        try:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                    for table in WRAPPED_SNAPSHOT_TABLES}
        finally:
            conn.close()

    def test_repair_refreshes_real_wrapped_without_new_listening(self):
        self._seedFallback()
        before = self._cache()
        self.assertNotEqual(json.loads(before["top_songs"])[0]["name"], "Recovered song")
        plays = self._committedState()["plays"]

        self.assertEqual(self.db._repairFallbackTrackMetadata([catalogTrack()]), 1)

        self.assertIsNone(self.db.repo.getCachedWrapped(self.db.user, WRAPPED_YEAR))
        after = self._cache()
        for field, name in (("top_songs", "Recovered song"), ("top_albums", "Recovered album"),
                            ("top_artists", "Recovered artist")):
            self.assertEqual(json.loads(after[field])[0]["name"], name)
        self.assertEqual(after["total_plays"], before["total_plays"])
        self.assertEqual(after["max_played_at"], before["max_played_at"])
        self.assertEqual(self._committedState()["plays"], plays)

    def test_repair_invalidates_other_users_and_later_discovery_years(self):
        self._seedFallback()
        oldMetadata = catalogTrack(REAL_ID_2)
        oldMetadata["artists"][0]["name"] = "Old artist name"
        oldMetadata["album"]["name"] = "Old album name"
        self.db.repo.upsertTrack(Client.formatTrack(oldMetadata, embedPlaybackInfo=False))
        laterYear = WRAPPED_YEAR + 1
        self._play(self.db, REAL_ID_2, laterYear)
        bob = Database("bob", dbPath=self.db.repo.connectionManager.dbPath, startWorkers=False)
        self.addCleanup(bob.repo.connectionManager.close)
        self._play(bob, REAL_ID_2, laterYear)
        self._cache()
        self.assertEqual(self._cache(year=laterYear)["discovered_artists"], 1)
        self.assertEqual(json.loads(self._cache(bob, laterYear)["top_artists"])[0]["name"], "Old artist name")

        self.db._repairFallbackTrackMetadata([catalogTrack()])

        self.assertEqual(self.db.repo._conn().execute("SELECT COUNT(*) FROM user_wrapped").fetchone()[0], 0)
        self.assertEqual(self._cache(year=laterYear)["discovered_artists"], 0)
        bobCache = self._cache(bob, laterYear)
        self.assertEqual(json.loads(bobCache["top_artists"])[0]["name"], "Recovered artist")
        self.assertEqual(json.loads(bobCache["top_albums"])[0]["name"], "Recovered album")

    def test_multi_repair_batch_bumps_generation_once_without_cached_rows(self):
        for trackId in (REAL_ID, REAL_ID_2):
            self._seedFallback(trackId)
        generation = self.db.repo.getWrappedInvalidationGeneration()

        repaired = self.db._repairFallbackTrackMetadata([catalogTrack(), catalogTrack(REAL_ID_2), catalogTrack()])

        self.assertEqual(repaired, 2)
        self.assertEqual(self.db.repo.getWrappedInvalidationGeneration(), generation + 1)

    def test_noop_batches_preserve_cached_rows_and_generation(self):
        self._seedFallback()
        self.db._repairFallbackTrackMetadata([catalogTrack()])
        self._cache()
        before = self._committedState()
        for payload in ([], [None, {}], [fallbackTrackRecord(REAL_ID)],
                        [catalogTrack(REAL_ID_2)], [catalogTrack(), catalogTrack()]):
            with self.subTest(payload=payload):
                self.assertEqual(self.db._repairFallbackTrackMetadata(payload), 0)
                self.assertEqual(self._committedState(), before)

    def test_inflight_pre_repair_snapshot_cannot_restore_stale_metadata(self):
        self._seedFallback()
        stale = self._cache()
        generation = self.db.repo.getWrappedInvalidationGeneration()
        self.db._repairFallbackTrackMetadata([catalogTrack()])

        self.assertFalse(self.db.repo.saveCachedWrapped(
            self.db.user, WRAPPED_YEAR, stale, expectedGeneration=generation))
        self.assertIsNone(self.db.repo.getCachedWrapped(self.db.user, WRAPPED_YEAR))

    def test_invalidation_failure_rolls_back_catalog_and_cache_and_allows_retry(self):
        triggers = (
            "CREATE TRIGGER fail_invalidation BEFORE INSERT ON app_settings "
            "WHEN NEW.key='wrapped_invalidation_generation' "
            "BEGIN SELECT RAISE(ABORT, 'invalidation failed'); END",
            "CREATE TRIGGER fail_invalidation AFTER DELETE ON user_wrapped "
            "BEGIN SELECT RAISE(ABORT, 'invalidation failed'); END",
        )
        for trigger in triggers:
            with self.subTest(trigger=trigger):
                self.db = self._makeDb({}, [])
                self._seedFallback()
                self._cache()
                before = self._committedState()
                with self.db.repo._conn() as conn:
                    conn.execute(trigger)

                with self.assertRaisesRegex(sqlite3.IntegrityError, "invalidation failed"):
                    self.db._repairFallbackTrackMetadata([catalogTrack()])

                self.assertEqual(self._committedState(), before)
                self.assertFalse(conn.in_transaction)
                with conn:
                    conn.execute("DROP TRIGGER fail_invalidation")
                self.assertEqual(self.db._repairFallbackTrackMetadata([catalogTrack()]), 1)
                self.assertIsNone(self.db.repo.getCachedWrapped(self.db.user, WRAPPED_YEAR))
