import unittest
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import (
    Repository, IMAGE_KIND_TRACK, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED,
    SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON,
    COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX,
)
from config import TOP_LIST_DEFAULT_WINDOW
from Database.backfill_matching import BackfillPage


def makeTrack(trackId="t1", name="Song One", albumId="alb1", artistId="art1"):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": artistId, "name": "Artist One", "url": f"http://example.com/artist/{artistId}",
             "imageUrl": "", "imageId": artistId},
        ],
        "album": {
            "id": albumId, "name": "Album One", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "http://img.example.com/a.jpg",
            "totalTracks": 10, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example.com/a.jpg",
        "imageId": albumId,
        "duration": 200000,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 3,
        "releaseDate": 12345.0,
    }


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()


class TestTrackCatalog(RepositoryTestCase):
    def test_roundtrip_matches_input_shape(self):
        track = makeTrack()
        self.repo.upsertTrack(track)

        fetched = self.repo.getTrack("t1")

        self.assertEqual(fetched["id"], "t1")
        self.assertEqual(fetched["name"], "Song One")
        self.assertEqual(fetched["duration"], 200000)
        self.assertEqual(fetched["explicit"], False)
        self.assertEqual(fetched["isrc"], "US1234567890")
        self.assertEqual(fetched["discNumber"], 1)
        self.assertEqual(fetched["trackNumber"], 3)
        self.assertEqual(fetched["album"]["id"], "alb1")
        self.assertEqual(fetched["album"]["totalTracks"], 10)
        self.assertEqual(len(fetched["artists"]), 1)
        self.assertEqual(fetched["artists"][0]["id"], "art1")
        self.assertEqual(fetched["artists"][0]["name"], "Artist One")

    def test_upsert_does_not_blank_existing_album_metadata(self):
        # A later partial payload (missing release_date/total_tracks/name) must
        # not overwrite good album metadata a prior full play already recorded -
        # matching updateAlbumMetadata's "blank fields aren't data" guard.
        self.repo.upsertTrack(makeTrack(trackId="t1", albumId="alb1"))
        self.repo.commit()

        blank = makeTrack(trackId="t1", albumId="alb1")
        blank["album"]["releaseDate"] = 0
        blank["album"]["totalTracks"] = 0
        blank["album"]["name"] = ""
        self.repo.upsertTrack(blank)
        self.repo.commit()

        row = self.repo.connection().execute(
            "SELECT name, total_tracks, release_date FROM albums WHERE id='alb1'"
        ).fetchone()
        self.assertEqual(row["name"], "Album One")
        self.assertEqual(row["total_tracks"], 10)
        self.assertEqual(row["release_date"], 12345.0)

    def test_upsert_does_not_blank_existing_track_duration(self):
        # A zero duration (Client.formatTrack's `duration_ms or 0` for a payload
        # without one) must not wipe a real duration - a 0 corrupts skip and
        # completion classification.
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.commit()

        blank = makeTrack(trackId="t1")
        blank["duration"] = 0
        self.repo.upsertTrack(blank)
        self.repo.commit()

        self.assertEqual(self.repo.getTrack("t1")["duration"], 200000)

    def test_delete_zero_duration_plays_without_is_skip_column(self):
        # deleteZeroDurationPlays' only callers (migrate1_7_0 / migrate1_9_0) run
        # before migrate1_32_0 adds plays.is_skip, so on a legacy table the
        # column is absent - the method must not reference it there.
        conn = self.repo.connection()
        conn.execute("DROP TABLE plays")
        conn.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY, username TEXT, "
                     "track_id TEXT, played_at REAL, time_played INTEGER)")
        conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('alice','t1',100,0)")
        conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('alice','t2',200,5000)")
        self.repo.commit()

        removed = self.repo.deleteZeroDurationPlays()

        self.assertEqual(removed, 1)
        remaining = [r["track_id"] for r in conn.execute("SELECT track_id FROM plays").fetchall()]
        self.assertEqual(remaining, ["t2"])

    def test_unknown_track_returns_none(self):
        self.assertIsNone(self.repo.getTrack("missing"))

    def test_track_exists(self):
        self.assertFalse(self.repo.trackExists("t1"))
        self.repo.upsertTrack(makeTrack())
        self.assertTrue(self.repo.trackExists("t1"))

    def test_multi_artist_order_preserved(self):
        track = makeTrack()
        track["artists"] = [
            {"id": "a1", "name": "First", "url": "u", "imageUrl": "", "imageId": "a1"},
            {"id": "a2", "name": "Second", "url": "u", "imageUrl": "", "imageId": "a2"},
            {"id": "a3", "name": "Third", "url": "u", "imageUrl": "", "imageId": "a3"},
        ]
        self.repo.upsertTrack(track)

        fetched = self.repo.getTrack("t1")

        self.assertEqual([a["id"] for a in fetched["artists"]], ["a1", "a2", "a3"])

    def test_second_upsert_overwrites_and_replaces_artists(self):
        """Last write wins, matching the old tracks[id] = track dict-assignment
        semantics - including dropping artists no longer present."""
        track = makeTrack()
        self.repo.upsertTrack(track)

        updated = makeTrack()
        updated["name"] = "Song One (Remastered)"
        updated["artists"] = [
            {"id": "art2", "name": "Artist Two", "url": "u", "imageUrl": "", "imageId": "art2"},
        ]
        self.repo.upsertTrack(updated)

        fetched = self.repo.getTrack("t1")
        self.assertEqual(fetched["name"], "Song One (Remastered)")
        self.assertEqual([a["id"] for a in fetched["artists"]], ["art2"])

    def test_shared_album_and_artist_are_not_duplicated_across_tracks(self):
        trackA = makeTrack(trackId="t1", albumId="alb1", artistId="art1")
        trackB = makeTrack(trackId="t2", albumId="alb1", artistId="art1")
        self.repo.upsertTrack(trackA)
        self.repo.upsertTrack(trackB)

        conn = self.repo._conn()
        albumCount = conn.execute("SELECT COUNT(*) AS c FROM albums WHERE id='alb1'").fetchone()["c"]
        artistCount = conn.execute("SELECT COUNT(*) AS c FROM artists WHERE id='art1'").fetchone()["c"]
        self.assertEqual(albumCount, 1)
        self.assertEqual(artistCount, 1)


class TestGetTracksByIds(RepositoryTestCase):
    """Batch equivalent of getTrack(), used by Database._paginateEntries() to
    avoid a 3-query-per-play N+1 when hydrating a page of history."""

    def test_returns_a_dict_keyed_by_track_id(self):
        self.repo.upsertTrack(makeTrack(trackId="t1", name="Song One"))
        self.repo.upsertTrack(makeTrack(trackId="t2", name="Song Two", albumId="alb2", artistId="art2"))

        result = self.repo.getTracksByIds(["t1", "t2"])

        self.assertEqual(set(result.keys()), {"t1", "t2"})
        self.assertEqual(result["t1"]["name"], "Song One")
        self.assertEqual(result["t2"]["name"], "Song Two")

    def test_result_matches_getTrack_for_the_same_id(self):
        self.repo.upsertTrack(makeTrack())

        viaBatch = self.repo.getTracksByIds(["t1"])["t1"]
        viaSingle = self.repo.getTrack("t1")

        self.assertEqual(viaBatch, viaSingle)

    def test_unknown_ids_are_simply_absent_from_the_result(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))

        result = self.repo.getTracksByIds(["t1", "missing"])

        self.assertEqual(set(result.keys()), {"t1"})

    def test_empty_id_list_returns_empty_dict_without_querying(self):
        self.assertEqual(self.repo.getTracksByIds([]), {})

    def test_tracks_on_different_albums_each_get_their_own_album(self):
        self.repo.upsertTrack(makeTrack(trackId="t1", albumId="alb1", artistId="art1"))
        self.repo.upsertTrack(makeTrack(trackId="t2", albumId="alb2", artistId="art2"))

        result = self.repo.getTracksByIds(["t1", "t2"])

        self.assertEqual(result["t1"]["album"]["id"], "alb1")
        self.assertEqual(result["t2"]["album"]["id"], "alb2")

    def test_multi_artist_order_preserved_per_track(self):
        track = makeTrack(trackId="t1")
        track["artists"] = [
            {"id": "a1", "name": "First", "url": "u", "imageUrl": "", "imageId": "a1"},
            {"id": "a2", "name": "Second", "url": "u", "imageUrl": "", "imageId": "a2"},
        ]
        self.repo.upsertTrack(track)

        result = self.repo.getTracksByIds(["t1"])

        self.assertEqual([a["id"] for a in result["t1"]["artists"]], ["a1", "a2"])


class TestPlaylistCatalog(RepositoryTestCase):
    def test_roundtrip(self):
        self.assertFalse(self.repo.playlistKnown("p1", "playlist"))
        self.repo.upsertPlaylistName("p1", "playlist", "My Playlist")
        self.assertTrue(self.repo.playlistKnown("p1", "playlist"))
        self.assertEqual(self.repo.getPlaylistName("p1", "playlist"), "My Playlist")

    def test_album_and_playlist_ids_are_independent_namespaces(self):
        self.repo.upsertPlaylistName("x1", "album", "Album Name")
        self.assertIsNone(self.repo.getPlaylistName("x1", "playlist"))
        self.assertEqual(self.repo.getPlaylistName("x1", "album"), "Album Name")

    def test_private_playlist_name_is_stored_as_none(self):
        self.repo.upsertPlaylistName("p2", "playlist", None)
        self.assertTrue(self.repo.playlistKnown("p2", "playlist"))
        self.assertIsNone(self.repo.getPlaylistName("p2", "playlist"))


class TestImageClaiming(RepositoryTestCase):
    def test_first_claim_succeeds_second_is_blocked(self):
        self.assertTrue(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))
        self.assertFalse(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))

    def test_claim_blocked_once_marked_ok(self):
        self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK)
        self.repo.markImageStatus("img1", IMAGE_KIND_TRACK, IMAGE_STATUS_OK)
        self.assertFalse(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))
        self.assertEqual(self.repo.imageStatus("img1", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)

    def test_failed_download_can_be_reclaimed(self):
        self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK)
        self.repo.markImageStatus("img1", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)
        self.assertTrue(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))

    def test_track_and_artist_kinds_are_independent(self):
        self.assertTrue(self.repo.tryClaimImageDownload("shared-id", "track"))
        self.assertTrue(self.repo.tryClaimImageDownload("shared-id", "artist"))


class TestUpsertTrackRobustness(RepositoryTestCase):
    def test_upsert_track_handles_missing_album_and_artists(self):
        """upsertTrack should construct a fallback album and default to no artists if they are None/missing, avoiding ProgrammingError."""
        track = {
            "id": "t_no_album",
            "name": "Song No Album",
            "url": "https://open.spotify.com/track/t_no_album",
            "imageId": "album_t_no_album",
            "imageUrl": "",
            "duration": 180000,
            "explicit": False,
            "isrc": "",
            "discNumber": 1,
            "trackNumber": 1,
        }
        
        self.repo.upsertTrack(track)
        self.repo.commit()
        
        db_track = self.repo.getTrack("t_no_album")
        self.assertIsNotNone(db_track)
        self.assertEqual(db_track["name"], "Song No Album")
        self.assertEqual(db_track["album"]["name"], "Song No Album")


class TestUpsertTrackGuards(RepositoryTestCase):
    """upsertTrack is last-write-wins for real metadata, but degraded records
    must never clobber good catalog data."""

    def _syntheticTrack(self, trackId="t1"):
        return {
            "id": trackId,
            "name": "Fabricated Name",
            "url": "",
            "artists": [{"id": "artist_md5", "name": "X", "url": "", "imageUrl": "", "imageId": "artist_md5"}],
            "album": {
                "id": f"album_{trackId}", "name": "Fabricated Name", "url": "",
                "imageId": f"album_{trackId}", "imageUrl": "", "totalTracks": 1, "releaseDate": 0.0,
            },
            "imageUrl": "", "imageId": f"album_{trackId}",
            "duration": 1000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
            "created_reason": SYNTHETIC_FALLBACK_REASON,
        }

    def test_fallback_record_never_degrades_real_metadata(self):
        self.repo.upsertTrack(makeTrack())  #< real row with real album/artists

        self.repo.upsertTrack(self._syntheticTrack())

        fetched = self.repo.getTrack("t1")
        self.assertEqual(fetched["name"], "Song One")
        self.assertEqual(fetched["url"], "http://example.com/track/t1")
        self.assertEqual(fetched["album"]["id"], "alb1")
        self.assertEqual([a["id"] for a in fetched["artists"]], ["art1"])
        self.assertIsNone(fetched["created_reason"])

    def test_fallback_record_still_overwrites_blanked_row(self):
        """A row stored from a blanked (region-restricted) lookup has no name -
        fallback data carrying the export's own names must keep healing it."""
        blanked = makeTrack()
        blanked["name"] = ""
        self.repo.upsertTrack(blanked)

        self.repo.upsertTrack(self._syntheticTrack())

        self.assertEqual(self.repo.getTrack("t1")["name"], "Fabricated Name")

    def test_fallback_record_still_updates_existing_fallback_row(self):
        self.repo.upsertTrack(self._syntheticTrack())

        longer = self._syntheticTrack()
        longer["duration"] = 240000
        self.repo.upsertTrack(longer)

        self.assertEqual(self.repo.getTrack("t1")["duration"], 240000)

    def test_real_record_still_replaces_fallback_row(self):
        self.repo.upsertTrack(self._syntheticTrack())

        self.repo.upsertTrack(makeTrack())

        fetched = self.repo.getTrack("t1")
        self.assertEqual(fetched["name"], "Song One")
        self.assertIsNone(fetched["created_reason"])

    def test_empty_artists_list_preserves_existing_artist_links(self):
        self.repo.upsertTrack(makeTrack())

        noArtists = makeTrack()
        noArtists["artists"] = []
        self.repo.upsertTrack(noArtists)

        fetched = self.repo.getTrack("t1")
        self.assertEqual([a["id"] for a in fetched["artists"]], ["art1"])


class TestAlbumMetadataGuards(RepositoryTestCase):
    """A partial backfill response must never regress album fields another
    source already filled."""

    def test_blank_values_do_not_regress_existing_metadata(self):
        self.repo.upsertTrack(makeTrack())  #< alb1: releaseDate 12345.0, totalTracks 10, "Album One"

        self.repo.updateAlbumMetadata("alb1", 0.0, 0, name=None)

        album = self.repo.getTrack("t1")["album"]
        self.assertEqual(album["releaseDate"], 12345.0)
        self.assertEqual(album["totalTracks"], 10)
        self.assertEqual(album["name"], "Album One")

    def test_real_values_update_metadata(self):
        self.repo.upsertTrack(makeTrack())

        self.repo.updateAlbumMetadata("alb1", 1600000000.0, 12, name="New Name")

        album = self.repo.getTrack("t1")["album"]
        self.assertEqual(album["releaseDate"], 1600000000.0)
        self.assertEqual(album["totalTracks"], 12)
        self.assertEqual(album["name"], "New Name")

    def test_partial_response_updates_only_provided_fields(self):
        self.repo.upsertTrack(makeTrack())

        self.repo.updateAlbumMetadata("alb1", 1600000000.0, 0, name=None)

        album = self.repo.getTrack("t1")["album"]
        self.assertEqual(album["releaseDate"], 1600000000.0)
        self.assertEqual(album["totalTracks"], 10)
        self.assertEqual(album["name"], "Album One")


class TestPlaysHistory(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t2"))

    def test_insert_and_count(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_exact_duplicate_play_is_rejected(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertFalse(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_same_track_replayed_at_different_time_is_allowed(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertTrue(self.repo.insertPlay("alice", "t1", 2000.0, 5000))
        self.assertEqual(self.repo.getPlaysCount("alice"), 2)

    def test_newest_first_ordering(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 3000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice")

        self.assertEqual([e["playedAt"] for e in entries], [3000.0, 2000.0, 1000.0])

    def test_oldest_first_ordering(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 3000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        entries = self.repo.getPlaysOldestFirst("alice")

        self.assertEqual([e["playedAt"] for e in entries], [1000.0, 2000.0, 3000.0])

    def test_oldest_first_carries_the_row_id_as_playid(self):
        """The export's keyset pager (X4) needs plays.id, not just played_at,
        to page past a cluster of rows sharing one timestamp."""
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 1000.0, 5000)

        entries = self.repo.getPlaysOldestFirst("alice")

        self.assertIn("playId", entries[0])
        self.assertLess(entries[0]["playId"], entries[1]["playId"])

    def test_after_ts_and_after_id_break_a_same_timestamp_tie(self):
        """Two different tracks logged at the exact same played_at (the
        Musicolet-import shape X4 targets) - afterTs alone can't step past
        either of them, afterId (breaking the tie by row id) can."""
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        firstId = self.repo.getPlaysOldestFirst("alice")[0]["playId"]
        self.repo.insertPlay("alice", "t2", 1000.0, 5000)

        stillBoth = self.repo.getPlaysOldestFirst("alice", afterTs=1000.0, afterId=None)
        self.assertEqual(len(stillBoth), 2)   #< afterTs alone is >=, so both remain

        pastFirst = self.repo.getPlaysOldestFirst("alice", afterTs=1000.0, afterId=firstId)
        self.assertEqual([e["id"] for e in pastFirst], ["t2"])

    def test_after_ts_and_after_id_together_still_respect_full_plays_only(self):
        """The composite cursor and _fullPlaysClause both append bound
        parameters, so a scrambled build order would compare numbers to the
        wrong things (mirrors test_full_plays_only_binds_every_clause_in_order,
        which covers the afterTs-only path)."""
        self.repo.insertPlay("alice", "t1", 1000.0, 200000)   #< full
        firstId = self.repo.getPlaysOldestFirst("alice")[0]["playId"]
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)     #< partial, same timestamp
        self.repo.insertPlay("alice", "t1", 2000.0, 200000)   #< full

        entries = self.repo.getPlaysOldestFirst("alice", afterTs=1000.0, afterId=firstId,
                                                 fullPlaysOnly=True)

        self.assertEqual([e["playedAt"] for e in entries], [2000.0])

    def test_pagination_count_and_start_index(self):
        for i in range(5):
            self.repo.insertPlay("alice", "t1", float(i), 5000)

        page = self.repo.getPlaysNewestFirst("alice", count=2, startIndex=1)

        self.assertEqual([e["playedAt"] for e in page], [3.0, 2.0])

    def test_plays_are_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("bob", "t1", 1000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice"), 1)
        self.assertEqual(self.repo.getPlaysCount("bob"), 1)

    def test_played_from_is_preserved(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="playlist:xyz")
        entries = self.repo.getPlaysNewestFirst("alice")
        self.assertEqual(entries[0]["playedFrom"], "playlist:xyz")

    def test_duplicate_play_enriches_missing_played_from(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000,
                             created_reason="history_import")
        before = self.repo.connection().execute(
            "SELECT id, created_at, created_reason FROM plays "
            "WHERE username='alice' AND track_id='t1' AND played_at=1000.0"
        ).fetchone()

        inserted = self.repo.insertPlay("alice", "t1", 1000.0, 5000,
                                        playedFrom="playlist:xyz")

        row = self.repo.connection().execute(
            "SELECT COUNT(*) AS count, id, played_from, created_at, created_reason FROM plays "
            "WHERE username='alice' AND track_id='t1' AND played_at=1000.0"
        ).fetchone()
        self.assertFalse(inserted)
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["id"], before["id"])
        self.assertEqual(row["played_from"], "playlist:xyz")
        self.assertEqual(row["created_at"], before["created_at"])
        self.assertEqual(row["created_reason"], before["created_reason"])

    def test_duplicate_play_replaces_existing_played_from(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="playlist:old")

        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="playlist:new")

        row = self.repo.connection().execute(
            "SELECT played_from FROM plays "
            "WHERE username='alice' AND track_id='t1' AND played_at=1000.0"
        ).fetchone()
        self.assertEqual(row["played_from"], "playlist:new")

    def test_duplicate_play_same_played_from_does_not_write(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="playlist:xyz")
        changesBefore = self.repo.connection().total_changes

        inserted = self.repo.insertPlay("alice", "t1", 1000.0, 5000,
                                        playedFrom="playlist:xyz")

        self.assertFalse(inserted)
        self.assertEqual(self.repo.connection().total_changes, changesBefore)

    def test_duplicate_play_empty_played_from_is_a_source_value(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="playlist:xyz")

        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="")

        row = self.repo.connection().execute(
            "SELECT played_from FROM plays "
            "WHERE username='alice' AND track_id='t1' AND played_at=1000.0"
        ).fetchone()
        self.assertEqual(row["played_from"], "")

    def test_duplicate_play_none_never_clears_played_from(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="playlist:xyz")

        inserted = self.repo.insertPlay("alice", "t1", 1000.0, 5000)

        row = self.repo.connection().execute(
            "SELECT played_from FROM plays "
            "WHERE username='alice' AND track_id='t1' AND played_at=1000.0"
        ).fetchone()
        self.assertFalse(inserted)
        self.assertEqual(row["played_from"], "playlist:xyz")

    def test_newest_first_respects_date_range(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 2000.0, 5000)
        self.repo.insertPlay("alice", "t1", 3000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice", startTs=1500.0, endTs=2500.0)

        self.assertEqual([e["playedAt"] for e in entries], [2000.0])

    def test_oldest_first_respects_date_range(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 2000.0, 5000)
        self.repo.insertPlay("alice", "t1", 3000.0, 5000)

        entries = self.repo.getPlaysOldestFirst("alice", startTs=1500.0, endTs=2500.0)

        self.assertEqual([e["playedAt"] for e in entries], [2000.0])

    def test_count_respects_date_range(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 2000.0, 5000)
        self.repo.insertPlay("alice", "t1", 3000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice", startTs=1500.0, endTs=2500.0), 1)

    def test_date_range_end_bound_is_exclusive(self):
        """Matches _dateRangeClause's documented half-open [start, end) -
        a play landing exactly on endTs belongs to the next range, not this
        one."""
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice", startTs=1000.0, endTs=2000.0), 0)
        self.assertEqual(self.repo.getPlaysCount("alice", startTs=1000.0, endTs=2001.0), 1)

    def test_date_range_start_bound_is_inclusive(self):
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice", startTs=2000.0, endTs=3000.0), 1)

    def test_newest_first_filtered_by_track_id(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 2000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice", trackId="t1")

        self.assertEqual([e["id"] for e in entries], ["t1"])

    def test_newest_first_filtered_by_artist_id(self):
        self.repo.upsertTrack(makeTrack(trackId="t3", albumId="alb2", artistId="art2"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t3", 2000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice", artistId="art2")

        self.assertEqual([e["id"] for e in entries], ["t3"])

    def test_newest_first_filtered_by_album_id(self):
        self.repo.upsertTrack(makeTrack(trackId="t3", albumId="alb2", artistId="art2"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t3", 2000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice", albumId="alb2")

        self.assertEqual([e["id"] for e in entries], ["t3"])

    def test_oldest_first_filtered_by_track_id(self):
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)
        self.repo.insertPlay("alice", "t2", 1000.0, 5000)
        self.repo.insertPlay("alice", "t1", 1500.0, 5000)

        entries = self.repo.getPlaysOldestFirst("alice", trackId="t1")

        self.assertEqual([(e["id"], e["playedAt"]) for e in entries],
                         [("t1", 1500.0), ("t1", 2000.0)])

    def test_count_filtered_by_track_id(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)
        self.repo.insertPlay("alice", "t2", 3000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice", trackId="t1"), 2)

    def test_get_plays_include_skips(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, is_skip=0)
        self.repo.insertPlay("alice", "t1", 2000.0, 1000, is_skip=1)

        normal_plays = self.repo.getPlaysNewestFirst("alice", trackId="t1", includeSkips=False)
        self.assertEqual(len(normal_plays), 1)
        self.assertFalse(normal_plays[0].get("isSkip", False))

        all_plays = self.repo.getPlaysNewestFirst("alice", trackId="t1", includeSkips=True)
        self.assertEqual(len(all_plays), 2)
        self.assertTrue(all_plays[0]["isSkip"])
        self.assertFalse(all_plays[1]["isSkip"])

    def test_get_plays_count_include_skips(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, is_skip=0)
        self.repo.insertPlay("alice", "t1", 2000.0, 1000, is_skip=1)

        self.assertEqual(self.repo.getPlaysCount("alice", trackId="t1", includeSkips=False), 1)
        self.assertEqual(self.repo.getPlaysCount("alice", trackId="t1", includeSkips=True), 2)

    # ---- fullPlaysOnly: the /history and Top pages' "Full plays only" filter,
    # which is a COMPLETION test (see _base.py's FULL_PLAY_PREDICATE) and
    # deliberately independent of includeSkips above. The two travel together
    # from one checkbox on /history, but only the route couples them.

    def test_full_plays_only_drops_a_partial_listen(self):
        """t1 is 200000ms; 5000ms of it is nobody's idea of a full play."""
        self.repo.insertPlay("alice", "t1", 1000.0, 200000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice", trackId="t1", fullPlaysOnly=True)

        self.assertEqual([e["playedAt"] for e in entries], [1000.0])
        self.assertEqual(self.repo.getPlaysCount("alice", trackId="t1", fullPlaysOnly=True), 1)
        self.assertEqual(self.repo.getPlaysCount("alice", trackId="t1"), 2)

    def test_full_plays_only_keeps_a_track_whose_duration_is_unknown(self):
        """The filter cannot judge a duration_ms of 0, and dropping it would
        silently hide every play of a track whose metadata never arrived."""
        blank = makeTrack(trackId="t9")
        blank["duration"] = 0
        self.repo.upsertTrack(blank)
        self.repo.insertPlay("alice", "t9", 1000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice", trackId="t9", fullPlaysOnly=True)

        self.assertEqual([e["playedAt"] for e in entries], [1000.0])

    def test_full_plays_only_and_include_skips_stay_independent(self):
        """One checkbox drives both on /history, but they are separate
        parameters here - a "simplification" that merges them would break the
        song-detail Show Skips toggle, which drives includeSkips alone."""
        self.repo.insertPlay("alice", "t1", 1000.0, 200000, is_skip=0)   #< full
        self.repo.insertPlay("alice", "t1", 2000.0, 5000, is_skip=0)     #< partial
        self.repo.insertPlay("alice", "t1", 3000.0, 1000, is_skip=1)     #< skip

        bothOff = self.repo.getPlaysCount("alice", trackId="t1", includeSkips=True)
        skipsOnly = self.repo.getPlaysCount("alice", trackId="t1", includeSkips=True,
                                            fullPlaysOnly=True)

        self.assertEqual(bothOff, 3)
        #< the skip is short, so the completion test drops it even though
        #  includeSkips let it past the is_skip filter
        self.assertEqual(skipsOnly, 1)

    def test_full_plays_only_binds_every_clause_in_order(self):
        """_fullPlaysClause APPENDS a bound parameter, unlike the skip clause it
        sits beside, so it has to be built in the position its `?` occupies.
        Every other bind-carrying clause is set here at once: with no range or
        afterTs, a scrambled order compares numbers to the wrong things and
        still returns the right rows."""
        self.repo.insertPlay("alice", "t1", 1000.0, 200000)   #< before startTs
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)     #< partial, and before afterTs
        self.repo.insertPlay("alice", "t2", 3000.0, 200000)
        self.repo.insertPlay("alice", "t1", 4000.0, 200000)
        self.repo.insertPlay("alice", "t1", 5000.0, 200000)   #< past endTs

        entries = self.repo.getPlaysOldestFirst(
            "alice", startTs=1500.0, endTs=4500.0, afterTs=2500.0,
            trackIds=["t1", "t2"], fullPlaysOnly=True)

        self.assertEqual([(e["id"], e["playedAt"]) for e in entries],
                         [("t2", 3000.0), ("t1", 4000.0)])

    def test_search_plays_can_include_skips(self):
        """searchPlays hardcoded `AND p.is_skip=0` before /history grew an
        opt-out, so this is the parameter that did not exist at all."""
        self.repo.insertPlay("alice", "t1", 1000.0, 200000, is_skip=0)
        self.repo.insertPlay("alice", "t1", 2000.0, 1000, is_skip=1)

        withoutSkips = self.repo.searchPlays("alice", "Song One")
        withSkips = self.repo.searchPlays("alice", "Song One", includeSkips=True)

        self.assertEqual([e["playedAt"] for e in withoutSkips], [1000.0])
        self.assertEqual([e["playedAt"] for e in withSkips], [2000.0, 1000.0])
        self.assertEqual(self.repo.searchPlaysCount("alice", "Song One", includeSkips=True), 2)

    def test_search_plays_reports_whether_a_row_is_a_skip(self):
        """The SELECT has to carry is_skip or _playRowToEntry defaults it to
        False - harmless while skips were always filtered out, a lie now that
        they can be listed."""
        self.repo.insertPlay("alice", "t1", 2000.0, 1000, is_skip=1)

        entries = self.repo.searchPlays("alice", "Song One", includeSkips=True)

        self.assertTrue(entries[0]["isSkip"])

    def test_search_plays_full_plays_only_drops_a_partial_listen(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 200000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        entries = self.repo.searchPlays("alice", "Song One", fullPlaysOnly=True)

        self.assertEqual([e["playedAt"] for e in entries], [1000.0])
        self.assertEqual(self.repo.searchPlaysCount("alice", "Song One", fullPlaysOnly=True), 1)

    def test_count_filtered_by_artist_id(self):
        self.repo.upsertTrack(makeTrack(trackId="t3", albumId="alb2", artistId="art2"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t3", 2000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice", artistId="art1"), 1)

    def test_count_filtered_by_album_id(self):
        self.repo.upsertTrack(makeTrack(trackId="t3", albumId="alb2", artistId="art2"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t3", 2000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice", albumId="alb1"), 1)

    def test_get_plays_with_source_in_range_returns_created_reason_and_respects_window(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, created_reason="listener_play (user: alice)")
        self.repo.insertPlay("alice", "t1", 1003.0, 5000, created_reason="web_api_backfill_play (user: alice)")
        self.repo.insertPlay("alice", "t2", 1500.0, 5000)  #< legacy row, no created_reason
        self.repo.insertPlay("alice", "t1", 3000.0, 5000, created_reason="history_import (user: alice)")
        self.repo.commit()

        plays = self.repo.getPlaysWithSourceInRange("alice", 900.0, 2000.0)

        self.assertEqual(len(plays), 3)
        byTime = {p["playedAt"]: p for p in plays}
        self.assertEqual(byTime[1000.0]["id"], "t1")
        self.assertEqual(byTime[1000.0]["createdReason"], "listener_play (user: alice)")
        self.assertEqual(byTime[1003.0]["createdReason"], "web_api_backfill_play (user: alice)")
        self.assertIsNone(byTime[1500.0]["createdReason"])
        self.assertEqual(byTime[1500.0]["timePlayed"], 5000)

    def test_get_plays_with_source_carries_created_at_for_listener_rows_only(self):
        """A listener row's created_at is the observed end of the play (the
        listener inserts at the track-change moment) - the anchor the
        reconciler's end-time pairing needs. Any other source's created_at is
        an import/poll moment and must come through as None."""
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = 1400.0
            self.repo.insertPlay("alice", "t1", 1000.0, 5000, created_reason="listener_play (user: alice)")
            self.repo.insertPlay("alice", "t1", 1100.0, 5000, created_reason="web_api_backfill_play (user: alice)")
        self.repo.commit()

        byTime = {p["playedAt"]: p for p in self.repo.getPlaysWithSourceInRange("alice", 900.0, 2000.0)}

        self.assertEqual(byTime[1000.0]["createdAt"], 1400.0)
        self.assertIsNone(byTime[1100.0]["createdAt"])

    def test_get_plays_with_source_finds_a_listener_row_by_its_end_time(self):
        """A paused play's start can sit more than one track-length before the
        window the API items span - the row must still be found via its
        created_at so the end-time pairing can see it."""
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = 1000.0
            self.repo.insertPlay("alice", "t1", 400.0, 5000, created_reason="listener_play (user: alice)")
            self.repo.insertPlay("alice", "t2", 400.0, 5000, created_reason="history_import (user: alice)")
        self.repo.commit()

        plays = self.repo.getPlaysWithSourceInRange("alice", 900.0, 2000.0)

        #< only the listener row is reachable via created_at; the import row's
        #  created_at means nothing about when its play ended
        self.assertEqual([(p["id"], p["playedAt"]) for p in plays], [("t1", 400.0)])

    def test_delete_zero_duration_plays_removes_only_zero_and_negative(self):
        conn = self.repo._conn()
        with conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 1000.0, 0)
            )
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 2000.0, -5)
            )
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 3000.0, 5000)
            )
        self.repo.commit()

        removedCount = self.repo.deleteZeroDurationPlays()
        self.repo.commit()

        self.assertEqual(removedCount, 2)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)
        self.assertEqual(self.repo.getPlaysNewestFirst("alice")[0]["timePlayed"], 5000)

    def test_delete_zero_duration_plays_spans_every_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        conn = self.repo._conn()
        with conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 1000.0, 0)
            )
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("bob", "t1", 1000.0, 0)
            )
        self.repo.commit()

        removedCount = self.repo.deleteZeroDurationPlays()

        self.assertEqual(removedCount, 2)

    def test_delete_zero_duration_plays_is_noop_when_none_exist(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        self.assertEqual(self.repo.deleteZeroDurationPlays(), 0)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_delete_play_removes_the_exact_row(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 1000.0, 5000)
        self.repo.commit()

        deleted = self.repo.deletePlay("alice", "t1", 1000.0)
        self.repo.commit()

        self.assertTrue(deleted)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)
        self.assertEqual(self.repo.getPlaysNewestFirst("alice")[0]["id"], "t2")

    def test_delete_play_returns_false_when_no_match(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        deleted = self.repo.deletePlay("alice", "t1", 9999.0)

        self.assertFalse(deleted)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_delete_play_is_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("bob", "t1", 1000.0, 5000)
        self.repo.commit()

        deleted = self.repo.deletePlay("alice", "t1", 1000.0)
        self.repo.commit()

        self.assertTrue(deleted)
        self.assertEqual(self.repo.getPlaysCount("alice"), 0)
        self.assertEqual(self.repo.getPlaysCount("bob"), 1)

    def _playCreatedColumns(self, username, trackId, playedAt):
        conn = self.repo._conn()
        row = conn.execute(
            "SELECT created_at, created_reason FROM plays WHERE username=? AND track_id=? AND played_at=?",
            (username, trackId, playedAt),
        ).fetchone()
        return row["created_at"], row["created_reason"]

    def test_insert_play_stores_created_reason_and_created_at(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, created_reason="listener_play (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._playCreatedColumns("alice", "t1", 1000.0)
        self.assertEqual(createdReason, "listener_play (user: alice)")
        self.assertIsNotNone(createdAt)

    def test_insert_play_without_created_reason_leaves_it_none(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        createdAt, createdReason = self._playCreatedColumns("alice", "t1", 1000.0)
        self.assertIsNone(createdReason)
        self.assertIsNone(createdAt)

    def test_updating_an_existing_play_does_not_change_created_reason(self):
        """Mirrors upsertTrack()'s semantics: created_reason/created_at are
        set once, on first insert, and never overwritten by a later update
        (e.g. a duplicate play arriving with a corrected time_played)."""
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, created_reason="listener_play (user: alice)")
        self.repo.commit()

        self.repo.insertPlay("alice", "t1", 1000.0, 8000, created_reason="history_import (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._playCreatedColumns("alice", "t1", 1000.0)
        self.assertEqual(createdReason, "listener_play (user: alice)")
        self.assertEqual(self.repo.getPlaysNewestFirst("alice")[0]["timePlayed"], 8000)


def makeSyntheticTrack(trackId="synth1", name="Ghost Song", artist="Ghost Artist"):
    """Mirrors Importer._createSyntheticTrack's output shape: empty urls and the
    synthetic created_reason marker, no created_at."""
    return {
        "id": trackId,
        "name": name,
        "url": "",
        "artists": [
            {"id": f"artist_{trackId}", "name": artist, "url": "", "imageUrl": "", "imageId": f"artist_{trackId}"},
        ],
        "album": {
            "id": f"album_{trackId}", "name": name, "url": "", "imageId": f"album_{trackId}",
            "imageUrl": "", "totalTracks": 1, "releaseDate": 0.0,
        },
        "imageUrl": "",
        "imageId": f"album_{trackId}",
        "duration": 10354,
        "explicit": False,
        "isrc": "",
        "discNumber": 1,
        "trackNumber": 1,
        "releaseDate": 0.0,
        "created_reason": SYNTHETIC_FALLBACK_REASON,
    }


EXTRAS_FULL = {
    "platform": "ios", "conn_country": "CH", "reason_start": "clickrow",
    "reason_end": "trackdone", "shuffle": 1, "skipped": 0, "offline": 0, "incognito": 0,
}


class TestGetPlaySourceCountsByUser(RepositoryTestCase):
    """getPlaySourceCountsByUser: the per-user live/prompt-backfill/late-backfill
    play counts behind the admin ledger's live-miss ratio (see
    implementationPlan-2026-09-07.md section 5). Seeds use raw SQL rather than
    insertPlay, which always stamps created_at from time.time() whenever a
    created_reason is given - these tests need createdAt/playedAt pinned to
    exact values, including a NULL createdAt for the legacy-row case."""

    SINCE_TS = 10_000.0
    PROMPT_SECONDS = 16 * 60

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))

    def _insertRawPlay(self, username, playedAt, createdReason, createdAt=None):
        conn = self.repo._conn()
        with conn:
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played, created_at, created_reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, "t1", playedAt, 5000, createdAt, createdReason),
            )

    def test_counts_are_split_per_bucket_per_user_not_just_totalled(self):
        """A total-only assertion would pass a live/backfill prefix swap -
        each bucket has to be checked on its own, for more than one user."""
        # alice: 2 live, 1 prompt backfill, 1 late backfill, 1 import (ignored)
        self._insertRawPlay("alice", self.SINCE_TS + 1, "listener_play (user: alice)",
                             createdAt=self.SINCE_TS + 1)
        self._insertRawPlay("alice", self.SINCE_TS + 2, "listener_play (user: alice)",
                             createdAt=self.SINCE_TS + 2)
        self._insertRawPlay("alice", self.SINCE_TS + 3, "web_api_backfill_play (user: alice)",
                             createdAt=self.SINCE_TS + 3 + 60)   #< 60s later: prompt
        self._insertRawPlay("alice", self.SINCE_TS + 4, "web_api_backfill_play (user: alice)",
                             createdAt=self.SINCE_TS + 4 + self.PROMPT_SECONDS + 1)   #< late
        self._insertRawPlay("alice", self.SINCE_TS + 5, "history_import (user: alice)",
                             createdAt=self.SINCE_TS + 5)
        # bob: 1 live, 1 prompt backfill only
        self._insertRawPlay("bob", self.SINCE_TS + 1, "listener_play (user: bob)",
                             createdAt=self.SINCE_TS + 1)
        self._insertRawPlay("bob", self.SINCE_TS + 2, "web_api_backfill_play (user: bob)",
                             createdAt=self.SINCE_TS + 2 + 30)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts["alice"], {"live": 2, "prompt_backfill": 1, "late_backfill": 1})
        self.assertEqual(counts["bob"], {"live": 1, "prompt_backfill": 1, "late_backfill": 0})

    def test_import_rows_never_count_in_any_bucket(self):
        self._insertRawPlay("alice", self.SINCE_TS + 1, "history_import (user: alice)",
                             createdAt=self.SINCE_TS + 1)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts["alice"], {"live": 0, "prompt_backfill": 0, "late_backfill": 0})

    def test_unrecognised_source_lands_in_neither_bucket(self):
        self._insertRawPlay("alice", self.SINCE_TS + 1, "unknown_play (user: alice)",
                             createdAt=self.SINCE_TS + 1)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts["alice"], {"live": 0, "prompt_backfill": 0, "late_backfill": 0})

    def test_played_at_exactly_at_since_ts_is_inclusive(self):
        self._insertRawPlay("alice", self.SINCE_TS, "listener_play (user: alice)",
                             createdAt=self.SINCE_TS)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts["alice"]["live"], 1)

    def test_backfill_exactly_at_the_prompt_boundary_counts_as_prompt(self):
        self._insertRawPlay("alice", self.SINCE_TS + 1, "web_api_backfill_play (user: alice)",
                             createdAt=self.SINCE_TS + 1 + self.PROMPT_SECONDS)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts["alice"], {"live": 0, "prompt_backfill": 1, "late_backfill": 0})

    def test_backfill_just_past_the_prompt_boundary_counts_as_late(self):
        self._insertRawPlay("alice", self.SINCE_TS + 1, "web_api_backfill_play (user: alice)",
                             createdAt=self.SINCE_TS + 1 + self.PROMPT_SECONDS + 1)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts["alice"], {"live": 0, "prompt_backfill": 0, "late_backfill": 1})

    def test_backfill_row_with_null_created_at_lands_in_neither_bucket(self):
        self._insertRawPlay("alice", self.SINCE_TS + 1, "web_api_backfill_play (user: alice)",
                             createdAt=None)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts["alice"], {"live": 0, "prompt_backfill": 0, "late_backfill": 0})

    def test_user_with_no_rows_in_the_window_is_omitted(self):
        self._insertRawPlay("alice", self.SINCE_TS - 100, "listener_play (user: alice)",
                             createdAt=self.SINCE_TS - 100)

        counts = self.repo.getPlaySourceCountsByUser(self.SINCE_TS, self.PROMPT_SECONDS)

        self.assertEqual(counts, {})


class TestPlayBehavioralExtras(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))

    def _rawPlayRow(self, playedAt):
        return self.repo._conn().execute(
            "SELECT * FROM plays WHERE username='alice' AND played_at=?", (playedAt,)
        ).fetchone()

    def test_insert_with_extras_writes_behavioral_columns(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=EXTRAS_FULL))
        row = self._rawPlayRow(1000.0)
        self.assertEqual(row["platform"], "ios")
        self.assertEqual(row["conn_country"], "CH")
        self.assertEqual(row["reason_start"], "clickrow")
        self.assertEqual(row["reason_end"], "trackdone")
        self.assertEqual(row["shuffle"], 1)
        self.assertEqual(row["skipped"], 0)
        self.assertEqual(row["offline"], 0)
        self.assertEqual(row["incognito"], 0)

    def test_insert_without_extras_leaves_columns_null(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000)
        row = self._rawPlayRow(1000.0)
        self.assertIsNone(row["platform"])
        self.assertIsNone(row["reason_end"])

    def test_existing_play_is_enriched_with_new_extras(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000)
        self.assertFalse(self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=EXTRAS_FULL))
        row = self._rawPlayRow(1000.0)
        self.assertEqual(row["platform"], "ios")
        self.assertEqual(row["reason_end"], "trackdone")

    def test_none_extras_values_never_clobber_stored_values(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=EXTRAS_FULL)
        sparse = {"platform": None, "conn_country": "DE"}
        self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=sparse)
        row = self._rawPlayRow(1000.0)
        self.assertEqual(row["platform"], "ios")     #< None must not clobber
        self.assertEqual(row["conn_country"], "DE")  #< new value wins

    def test_no_update_issued_when_nothing_changes(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=EXTRAS_FULL)
        conn = self.repo._conn()
        changesBefore = conn.total_changes
        self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=dict(EXTRAS_FULL))
        self.assertEqual(conn.total_changes, changesBefore)

    def test_get_plays_near_time_carries_behavioral_columns(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=EXTRAS_FULL)
        matches = self.repo.getPlaysNearTime("alice", "t1", 1000.0, 10)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["platform"], "ios")
        self.assertEqual(matches[0]["reason_end"], "trackdone")
        self.assertEqual(matches[0]["shuffle"], 1)

    def test_get_plays_oldest_first_attaches_extras(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000, extras=EXTRAS_FULL)
        self.repo.insertPlay("alice", "t1", 2000.0, 60000)
        entries = self.repo.getPlaysOldestFirst("alice")
        self.assertEqual(entries[0]["extras"]["platform"], "ios")
        self.assertIsNone(entries[1]["extras"])


class TestPlaySkips(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t2"))

    def test_insert_skip_and_exact_duplicate_ignored(self):
        # Skips are is_skip=1 rows in plays (no separate play_skips table).
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 400, is_skip=1))
        self.assertFalse(self.repo.insertPlay("alice", "t1", 1000.0, 400, is_skip=1))
        count = self.repo._conn().execute("SELECT COUNT(*) FROM plays WHERE is_skip=1").fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_skip_accepts_zero_duration_and_stamps_reason(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 0, is_skip=1,
                                             extras=EXTRAS_FULL,
                                             created_reason="history_import (user: alice)"))
        row = self.repo._conn().execute("SELECT * FROM plays WHERE is_skip=1").fetchone()
        self.assertEqual(row["time_played"], 0)
        self.assertEqual(row["created_reason"], "history_import (user: alice)")
        self.assertIsNotNone(row["created_at"])
        self.assertEqual(row["reason_end"], "trackdone")

    def test_insert_skip_does_not_commit(self):
        self.repo.commit()  #< persist the seeded user/tracks first
        self.repo.insertPlay("alice", "t1", 1000.0, 400, is_skip=1)
        self.repo.rollback()
        count = self.repo._conn().execute("SELECT COUNT(*) FROM plays WHERE is_skip=1").fetchone()[0]
        self.assertEqual(count, 0)

    def test_get_skips_oldest_first(self):
        self.repo.insertPlay("alice", "t2", 2000.0, 300, is_skip=1, extras=EXTRAS_FULL)
        self.repo.insertPlay("alice", "t1", 1000.0, 400, is_skip=1)
        entries = self.repo.getSkipsOldestFirst("alice")
        self.assertEqual([e["id"] for e in entries], ["t1", "t2"])
        self.assertEqual(entries[0]["playedAt"], 1000.0)
        self.assertEqual(entries[0]["timePlayed"], 400)
        self.assertIsNone(entries[0]["playedFrom"])
        self.assertIsNone(entries[0]["extras"])
        self.assertEqual(entries[1]["extras"]["platform"], "ios")

    def test_get_skips_oldest_first_after_id_breaks_a_same_timestamp_tie(self):
        """Mirrors test_after_ts_and_after_id_break_a_same_timestamp_tie for
        the skip feed - iterExportEntries pages this the same way (X4)."""
        self.repo.insertPlay("alice", "t1", 1000.0, 400, is_skip=1)
        firstId = self.repo.getSkipsOldestFirst("alice")[0]["playId"]
        self.repo.insertPlay("alice", "t2", 1000.0, 400, is_skip=1)

        pastFirst = self.repo.getSkipsOldestFirst("alice", afterTs=1000.0, afterId=firstId)

        self.assertEqual([e["id"] for e in pastFirst], ["t2"])

    def test_get_skips_oldest_first_excludes_real_plays(self):
        # A real play (is_skip=0) at the same time must not leak into the skip feed.
        self.repo.insertPlay("alice", "t1", 500.0, 60000)
        self.repo.insertPlay("alice", "t2", 1000.0, 400, is_skip=1)
        entries = self.repo.getSkipsOldestFirst("alice")
        self.assertEqual([e["id"] for e in entries], ["t2"])

    def test_get_skip_count_scoped_to_user_and_range(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("alice", "t1", 1000.0, 400, is_skip=1)
        self.repo.insertPlay("alice", "t1", 2000.0, 400, is_skip=1)
        self.repo.insertPlay("bob", "t1", 1500.0, 400, is_skip=1)
        # A real play must not be counted as a skip.
        self.repo.insertPlay("alice", "t2", 1200.0, 60000)

        self.assertEqual(self.repo.getSkipCount("alice"), 2)
        self.assertEqual(self.repo.getSkipCount("alice", startTs=1500.0), 1)
        self.assertEqual(self.repo.getSkipCount("alice", endTs=1500.0), 1)
        self.assertEqual(self.repo.getSkipCount("bob"), 1)

    def test_get_skip_count_empty_table_returns_zero(self):
        self.assertEqual(self.repo.getSkipCount("alice"), 0)


class TestRangeDeletes(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))

    def test_delete_plays_in_range_scoped_to_user_and_range(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000)
        self.repo.insertPlay("alice", "t1", 2000.0, 60000)
        self.repo.insertPlay("alice", "t1", 3000.0, 60000)
        self.repo.insertPlay("bob", "t1", 2000.0, 60000)

        deleted = self.repo.deletePlaysInRange("alice", 1500.0, 2500.0)

        self.assertEqual(deleted, 1)
        self.assertEqual(self.repo.getPlaysCount("alice"), 2)
        self.assertEqual(self.repo.getPlaysCount("bob"), 1)

    def test_delete_plays_in_range_bounds_are_inclusive(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000)
        self.repo.insertPlay("alice", "t1", 2000.0, 60000)
        deleted = self.repo.deletePlaysInRange("alice", 1000.0, 2000.0)
        self.assertEqual(deleted, 2)

    def test_delete_skips_in_range_scoped_to_user_and_range(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 400, is_skip=1)
        self.repo.insertPlay("alice", "t1", 2000.0, 400, is_skip=1)
        self.repo.insertPlay("bob", "t1", 2000.0, 400, is_skip=1)
        # A real play in range must survive deleteSkipsInRange.
        self.repo.insertPlay("alice", "t1", 2100.0, 60000)

        deleted = self.repo.deleteSkipsInRange("alice", 1500.0, 2500.0)

        self.assertEqual(deleted, 1)
        remaining = self.repo._conn().execute(
            "SELECT username, played_at FROM plays WHERE is_skip=1 ORDER BY played_at").fetchall()
        self.assertEqual([(r["username"], r["played_at"]) for r in remaining],
                         [("alice", 1000.0), ("bob", 2000.0)])

    def test_range_deletes_do_not_commit(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 60000)
        self.repo.commit()
        self.repo.deletePlaysInRange("alice", 0.0, 5000.0)
        self.repo.rollback()
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)


class TestSyntheticTrackLifecycle(RepositoryTestCase):
    def _trackCreatedColumns(self, trackId):
        row = self.repo._conn().execute(
            "SELECT created_at, created_reason FROM tracks WHERE id=?", (trackId,)
        ).fetchone()
        return row["created_at"], row["created_reason"]

    def test_synthetic_insert_stamps_created_at(self):
        """A created_reason without a created_at breaks the 'reason implies
        timestamp' invariant insertPlay() documents - the repo must stamp it."""
        self.repo.upsertTrack(makeSyntheticTrack())
        self.repo.commit()

        createdAt, createdReason = self._trackCreatedColumns("synth1")
        self.assertEqual(createdReason, SYNTHETIC_FALLBACK_REASON)
        self.assertIsNotNone(createdAt)

    def test_synthetic_reupsert_keeps_marker(self):
        """Re-importing history round-trips the synthetic dict (via getAllTracks)
        - the marker must survive, matching the created-on-INSERT-only rule."""
        self.repo.upsertTrack(makeSyntheticTrack())
        self.repo.upsertTrack(makeSyntheticTrack(), created_reason="history_import (user: alice)")
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("synth1")
        self.assertEqual(createdReason, SYNTHETIC_FALLBACK_REASON)

    def test_real_metadata_promotes_synthetic_row(self):
        """A track that turns out to exist on Spotify (e.g. the listener fetches
        the same id later) must lose the synthetic marker so the UI stops badging
        it as Deleted/Unavailable."""
        self.repo.upsertTrack(makeSyntheticTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="listener_fetch (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, "listener_fetch (user: alice)")
        self.assertIsNotNone(createdAt)
        self.assertEqual(self.repo.getTrack("t1")["url"], "http://example.com/track/t1")

    def test_real_metadata_without_reason_clears_synthetic_marker(self):
        self.repo.upsertTrack(makeSyntheticTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("t1")
        self.assertIsNone(createdReason)

    def test_real_metadata_promotes_restricted_row(self):
        """Same promotion as synthetic rows: a restricted-fallback row overwritten
        by real metadata loses its May-be-unavailable marker."""
        restricted = makeTrack(trackId="t1")
        restricted["created_reason"] = RESTRICTED_FALLBACK_REASON
        self.repo.upsertTrack(restricted)
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="listener_fetch (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, "listener_fetch (user: alice)")
        self.assertIsNotNone(createdAt)

    def test_restricted_reupsert_keeps_marker(self):
        """Re-imports round-trip the restricted marker through the catalog cache -
        it must survive, like the synthetic marker does."""
        restricted = makeTrack(trackId="t1")
        restricted["created_reason"] = RESTRICTED_FALLBACK_REASON
        self.repo.upsertTrack(restricted)
        self.repo.upsertTrack(dict(restricted), created_reason="history_import (user: alice)")
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, RESTRICTED_FALLBACK_REASON)

    def test_conflict_keeps_non_synthetic_created_reason(self):
        """The promotion exception applies only to synthetic rows - a real row's
        provenance is still never overwritten on conflict."""
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="history_import (user: alice)")
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="listener_fetch (user: alice)")
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, "history_import (user: alice)")

    def test_song_rows_include_created_reason(self):
        """getSongsPage feeds the top-songs/dashboard track cards - it must
        carry created_reason so the Deleted/Unavailable badge can render there."""
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeSyntheticTrack(trackId="synth1"))
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "synth1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)
        self.repo.commit()

        songs = {song["id"]: song for song in self.repo.getSongsPage("alice")}
        self.assertEqual(songs["synth1"]["created_reason"], SYNTHETIC_FALLBACK_REASON)
        self.assertIsNone(songs["t1"]["created_reason"])


class TestAvailabilityReason(RepositoryTestCase):
    def test_roundtrip_and_clear_on_later_upsert(self):
        """availability_reason reflects the latest lookup (current state, not
        provenance): stored on upsert, cleared when a later upsert has none."""
        track = makeTrack(trackId="t1")
        track["availability_reason"] = "COUNTRY_RESTRICTED"
        self.repo.upsertTrack(track)
        self.repo.commit()
        self.assertEqual(self.repo.getTrack("t1")["availability_reason"], "COUNTRY_RESTRICTED")

        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.commit()
        self.assertIsNone(self.repo.getTrack("t1")["availability_reason"])

    def test_song_rows_include_availability_reason(self):
        self.repo.upsertUser("alice", "alice@example.com")
        track = makeTrack(trackId="t1")
        track["availability_reason"] = "COUNTRY_RESTRICTED"
        self.repo.upsertTrack(track)
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice")
        self.assertEqual(songs[0]["availability_reason"], "COUNTRY_RESTRICTED")

    def test_add_availability_columns_if_missing_on_legacy_db(self):
        import sqlite3

        legacyPath = Path(self._tmpdir.name) / "legacy.db"
        conn = sqlite3.connect(legacyPath)
        conn.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, album_id TEXT NOT NULL)")
        conn.execute("CREATE TABLE albums (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL)")
        conn.commit()
        conn.close()

        legacyRepo = Repository(legacyPath)
        try:
            legacyRepo.addAvailabilityColumnsIfMissing()
            legacyRepo.addAvailabilityColumnsIfMissing()  #< idempotent
            trackCols = {r["name"] for r in legacyRepo._conn().execute("PRAGMA table_info(tracks)").fetchall()}
            albumCols = {r["name"] for r in legacyRepo._conn().execute("PRAGMA table_info(albums)").fetchall()}
            self.assertIn("availability_reason", trackCols)
            self.assertIn("backfill_attempted_at", albumCols)
        finally:
            legacyRepo.connectionManager.close()


class TestFindMatchingBackfillPlay(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        unrelated = makeTrack(trackId="t2", name="Unrelated Song")
        unrelated["isrc"] = "OTHER-RECORDING"
        self.repo.upsertTrack(unrelated)
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

    def _match(self, username, trackId, timestamp, tolerance, *, skipToleranceSeconds=None):
        page = BackfillPage([{"track": {"id": trackId}, "played_at": timestamp}])
        return self.repo.findMatchingBackfillPlay(username, trackId, timestamp, tolerance,
                                                  skipToleranceSeconds=skipToleranceSeconds, page=page)

    def test_true_within_tolerance(self):
        self.assertTrue(self._match("alice", "t1", 1050.0, 100))

    def test_true_at_exact_boundary(self):
        self.assertTrue(self._match("alice", "t1", 1100.0, 100))
        self.assertTrue(self._match("alice", "t1", 900.0, 100))

    def test_false_just_outside_tolerance(self):
        self.assertFalse(self._match("alice", "t1", 1101.0, 100))
        self.assertFalse(self._match("alice", "t1", 899.0, 100))

    def test_false_for_different_track_id(self):
        self.assertFalse(self._match("alice", "t2", 1050.0, 100))

    def test_false_for_different_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.assertFalse(self._match("bob", "t1", 1050.0, 100))

    def _insertPlayCreatedAt(self, trackId, playedAt, createdAt, created_reason, is_skip=0):
        """created_at is stamped with time.time() inside insertPlay - pin the
        clock so the test controls the row's insert-time stamp."""
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = createdAt
            self.repo.insertPlay("alice", trackId, playedAt, 5000, created_reason=created_reason,
                                 is_skip=is_skip)
        self.repo.commit()

    def test_listener_observed_end_without_start_proof_suppresses(self):
        """A long-paused play: its start is far outside the guard's reach, its
        observed end (created_at) is the API stamp. #38 reoffered this; live
        data showed such stamps are the same listen (2026-09-23), and the guard
        is the last check - the page lookup may have failed or run before this
        row committed."""
        self._insertPlayCreatedAt("t2", 2000.0, 5000.0, "listener_play (user: alice)")

        self.assertTrue(self._match("alice", "t2", 5000.0, 100))

    def test_non_listener_insert_time_is_not_playback_evidence(self):
        """An import or backfill row's created_at is the import/poll moment,
        not a play end - matching on it would suppress genuine plays."""
        self._insertPlayCreatedAt("t2", 2000.0, 5000.0, "history_import (user: alice)")

        self.assertFalse(self._match("alice", "t2", 5000.0, 100))

    def test_listener_end_match_is_a_point_match(self):
        self._insertPlayCreatedAt("t2", 2000.0, 5000.0, "listener_play (user: alice)")

        self.assertFalse(self._match("alice", "t2", 5011.0, 100))
        self.assertTrue(self._match("alice", "t2", 5010.0, 100))

    def test_skip_tolerance_matches_a_skip_by_its_played_at(self):
        """2026-08-14: the listener recorded a 3.6s skip at 16:29:06, the Web
        API reported the same listen at 16:28:51 - 15s away, inside a wide
        window of 280s - and the backfill re-added it as a full 220s play.
        Every arm was is_skip=0, so the row was never a candidate.

        The filter cannot work for this source: the Web API gives no ms_played,
        so a backfill row stamps the track's whole duration and is is_skip=0 by
        construction (verified: 479/479 live rows). An is_skip=0-only guard
        therefore cannot see a skipped listen AT ALL - the miss is systematic,
        not a coincidence of timing."""
        self._insertPlayCreatedAt("t2", 2000.0, 2025.0, "listener_play (user: alice)", is_skip=1)

        self.assertFalse(self._match("alice", "t2", 1985.0, 280))
        self.assertTrue(self._match("alice", "t2", 1985.0, 280,
                                                  skipToleranceSeconds=20))

    def test_skip_tolerance_is_a_tight_point_match(self):
        self._insertPlayCreatedAt("t2", 2000.0, 2025.0, "listener_play (user: alice)", is_skip=1)

        self.assertTrue(self._match("alice", "t2", 2020.0, 280, skipToleranceSeconds=20))
        self.assertFalse(self._match("alice", "t2", 2021.0, 280, skipToleranceSeconds=20))
        self.assertTrue(self._match("alice", "t2", 1980.0, 280, skipToleranceSeconds=20))
        self.assertFalse(self._match("alice", "t2", 1979.0, 280, skipToleranceSeconds=20))

    def test_a_skip_never_gets_the_wide_duration_window(self):
        """Measured over live data (2026-08-15): the provable duplicates sat
        3-15s from their skip, the two ambiguous ones 95s and 291s away - and
        at that distance "skip, then a genuine replay the listener missed" is
        the likelier reading. Handing the skip the wide duration+60s window
        would suppress those, and suppression is unrecoverable: the next poll's
        page collides identically and nothing retries it."""
        self._insertPlayCreatedAt("t2", 2000.0, 2025.0, "listener_play (user: alice)", is_skip=1)

        self.assertFalse(self._match("alice", "t2", 2095.0, 280, skipToleranceSeconds=20))

    def test_a_skips_created_at_never_anchors_an_end(self):
        """A skip's created_at is when the user skipped AWAY, not the end of a
        play the feed still owes us - the rule getPlaysWithSourceInRange already
        enforces on the announce side. Only a skip's played_at counts."""
        self._insertPlayCreatedAt("t2", 1000.0, 5000.0, "listener_play (user: alice)", is_skip=1)

        self.assertFalse(self._match("alice", "t2", 5000.0, 100, skipToleranceSeconds=20))

    def test_the_skip_arm_is_not_restricted_to_listener_rows(self):
        """Unlike the end arm - which needs created_at to MEAN a play end, true
        of listener rows only - this arm matches played_at, which every source
        records honestly. An imported skip at the same instant is the same
        physical event."""
        self._insertPlayCreatedAt("t2", 2000.0, 2025.0, "history_import (user: alice)", is_skip=1)

        self.assertTrue(self._match("alice", "t2", 2010.0, 280, skipToleranceSeconds=20))


def makeSearchableTrack(trackId, name, artistName, albumName):
    """Unlike makeTrack() (which hardcodes "Artist One"/"Album One" regardless
    of id - fine for id-uniqueness tests, wrong for text-search tests), this
    lets each fixture track carry genuinely distinct searchable text."""
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": f"{trackId}-artist", "name": artistName, "url": "http://example.com/artist",
             "imageUrl": "", "imageId": f"{trackId}-artist"},
        ],
        "album": {
            "id": f"{trackId}-album", "name": albumName, "url": "http://example.com/album",
            "imageId": f"{trackId}-album", "imageUrl": "", "totalTracks": 1, "releaseDate": 12345.0,
        },
        "imageUrl": "", "imageId": f"{trackId}-album", "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 12345.0,
    }


class TestHistoryFilterEquivalence(RepositoryTestCase):
    START_BOUND = 1500.0
    END_BOUND = 3500.0
    TIED_TIMESTAMP = 2000.0
    FIRST_TIMESTAMP = 1000.0
    SKIP_TIMESTAMP = 3000.0
    LAST_TIMESTAMP = 4000.0
    FULL_LISTEN_MS = 180000
    PARTIAL_LISTEN_MS = 5000
    SKIP_LISTEN_MS = 1000
    ALICE_PLAY_COUNT = 5
    BOB_PLAY_COUNT = 1
    PAGE_LIMIT = 2
    PAGE_OFFSET = 1

    MERGE_METADATA_STATES = (False, True)
    INCLUDE_SKIP_STATES = (False, True)
    FULL_PLAY_STATES = (False, True)
    BOUND_CASES = (
        ("all", {}),
        ("bounded", {"startTs": START_BOUND, "endTs": END_BOUND}),
    )
    HISTORY_FILTER_CASES = (
        ("none", {}),
        ("trackId", {"trackId": "t1"}),
        ("artistId", {"artistId": "art1"}),
        ("albumId", {"albumId": "alb1"}),
        ("trackIds", {"trackIds": ["t1", "t3"]}),
        ("emptyTrackIds", {"trackIds": []}),
    )
    SEARCH_TRACK_ID_CASES = (
        ("none", None),
        ("some", ["t1", "t3"]),
        ("empty", []),
    )

    def _makeSeededRepo(self, mergeMetadata=False):
        tmpdir = tempfile.TemporaryDirectory()
        repo = Repository(Path(tmpdir.name) / "filter_equivalence.db")
        self.addCleanup(tmpdir.cleanup)
        self.addCleanup(repo.connectionManager.close)

        repo.upsertUser("alice", "alice@example.com")
        repo.upsertUser("bob", "bob@example.com")
        repo.upsertTrack(makeSearchableTrack("t1", "Needle Alpha", "Artist One", "Album One"))
        repo.upsertTrack(makeSearchableTrack("t2", "Needle Beta", "Artist One", "Album One"))
        repo.upsertTrack(makeSearchableTrack("t3", "Needle Gamma", "Artist Two", "Album Two"))
        repo.upsertTrack(makeSearchableTrack("t4", "Needle Skip", "Artist Three", "Album Three"))
        if mergeMetadata:
            with repo.connection():
                repo.connection().execute("UPDATE tracks SET canonical_id='t1' WHERE id='t2'")

        repo.insertPlay("alice", "t1", self.FIRST_TIMESTAMP, self.FULL_LISTEN_MS, is_skip=0)
        repo.insertPlay("alice", "t2", self.TIED_TIMESTAMP, self.PARTIAL_LISTEN_MS, is_skip=0)
        repo.insertPlay("alice", "t3", self.TIED_TIMESTAMP, self.FULL_LISTEN_MS, is_skip=0)
        repo.insertPlay("alice", "t4", self.SKIP_TIMESTAMP, self.SKIP_LISTEN_MS, is_skip=1)
        repo.insertPlay("alice", "t1", self.LAST_TIMESTAMP, self.FULL_LISTEN_MS, is_skip=0)
        repo.insertPlay("bob", "t1", self.TIED_TIMESTAMP, self.FULL_LISTEN_MS, is_skip=0)
        return repo

    def _entryKey(self, entry):
        return (entry["id"], entry["playedAt"], entry["timePlayed"], entry["isSkip"])

    def _entryKeys(self, entries):
        return [self._entryKey(entry) for entry in entries]

    def test_history_rows_and_count_stay_equivalent_across_filter_matrix(self):
        """96 cases: merge metadata x skip toggle x full-play toggle x six id/entity filters x bounds."""
        for mergeMetadata in self.MERGE_METADATA_STATES:
            repo = self._makeSeededRepo(mergeMetadata)
            self.assertEqual(repo.getPlaysCount("alice", includeSkips=True), self.ALICE_PLAY_COUNT)
            self.assertEqual(repo.getPlaysCount("bob", includeSkips=True), self.BOB_PLAY_COUNT)
            for includeSkips in self.INCLUDE_SKIP_STATES:
                for fullPlaysOnly in self.FULL_PLAY_STATES:
                    for _filterName, filterKwargs in self.HISTORY_FILTER_CASES:
                        for _boundName, boundKwargs in self.BOUND_CASES:
                            kwargs = {
                                **filterKwargs,
                                **boundKwargs,
                                "includeSkips": includeSkips,
                                "fullPlaysOnly": fullPlaysOnly,
                            }
                            with self.subTest(merge=mergeMetadata, kwargs=kwargs):
                                newest = repo.getPlaysNewestFirst("alice", **kwargs)
                                oldest = repo.getPlaysOldestFirst("alice", **kwargs)
                                total = repo.getPlaysCount("alice", **kwargs)

                                self.assertEqual(total, len(newest))
                                self.assertEqual(total, len(oldest))
                                self.assertEqual(self._entryKeys(newest), list(reversed(self._entryKeys(oldest))))
                                self.assertEqual(repo.getPlaysNewestFirst(
                                    "alice", count=0, **kwargs), [])
                                self.assertEqual(
                                    self._entryKeys(repo.getPlaysNewestFirst(
                                        "alice", count=self.PAGE_LIMIT, startIndex=self.PAGE_OFFSET, **kwargs)),
                                    self._entryKeys(newest)[self.PAGE_OFFSET:self.PAGE_OFFSET + self.PAGE_LIMIT],
                                )

    def test_search_rows_and_count_stay_equivalent_across_filter_matrix(self):
        """48 cases: merge metadata x skip toggle x full-play toggle x three tag-id states x bounds."""
        searchQuery = "Needle"
        for mergeMetadata in self.MERGE_METADATA_STATES:
            repo = self._makeSeededRepo(mergeMetadata)
            for includeSkips in self.INCLUDE_SKIP_STATES:
                for fullPlaysOnly in self.FULL_PLAY_STATES:
                    for _trackIdsName, trackIds in self.SEARCH_TRACK_ID_CASES:
                        for _boundName, boundKwargs in self.BOUND_CASES:
                            kwargs = {
                                **boundKwargs,
                                "trackIds": trackIds,
                                "includeSkips": includeSkips,
                                "fullPlaysOnly": fullPlaysOnly,
                            }
                            with self.subTest(merge=mergeMetadata, kwargs=kwargs):
                                newest = repo.searchPlays("alice", searchQuery, **kwargs)
                                oldest = repo.searchPlays("alice", searchQuery, oldestFirst=True, **kwargs)
                                total = repo.searchPlaysCount("alice", searchQuery, **kwargs)

                                self.assertEqual(total, len(newest))
                                self.assertEqual(total, len(oldest))
                                self.assertEqual(self._entryKeys(newest), list(reversed(self._entryKeys(oldest))))
                                self.assertEqual(repo.searchPlays(
                                    "alice", searchQuery, limit=0, **kwargs), [])
                                self.assertEqual(
                                    self._entryKeys(repo.searchPlays(
                                        "alice", searchQuery, limit=self.PAGE_LIMIT,
                                        offset=self.PAGE_OFFSET, **kwargs)),
                                    self._entryKeys(newest)[self.PAGE_OFFSET:self.PAGE_OFFSET + self.PAGE_LIMIT],
                                )

    def test_history_export_cursor_pages_through_equal_timestamps(self):
        repo = self._makeSeededRepo()
        oldest = repo.getPlaysOldestFirst("alice", includeSkips=True)
        tied = [entry for entry in oldest if entry["playedAt"] == self.TIED_TIMESTAMP]

        pageAfterFirstTie = repo.getPlaysOldestFirst(
            "alice", includeSkips=True, afterTs=tied[0]["playedAt"], afterId=tied[0]["playId"])

        self.assertEqual(len(tied), 2)
        self.assertEqual(self._entryKeys(pageAfterFirstTie), self._entryKeys(oldest[2:]))


class TestSearchPlays(RepositoryTestCase):
    """searchPlays()/searchPlaysCount() match a play's track name, artist(s),
    album, or source playlist - pushed down into SQL (with LIMIT/OFFSET)
    instead of requiring every play to be fetched and filtered in Python."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeSearchableTrack("t1", "Bohemian Rhapsody", "Queen", "A Night at the Opera"))
        self.repo.upsertTrack(makeSearchableTrack("t2", "Another One Bites the Dust", "Queen", "The Game"))
        self.repo.upsertTrack(makeSearchableTrack("t3", "Unrelated Song", "Random Artist", "Random Album"))

    def test_matches_track_name(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "bohemian")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_match_is_case_insensitive(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)

        results = self.repo.searchPlays("alice", "BOHEMIAN")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_matches_artist_name(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "Queen")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_matches_album_name(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "Night at the Opera")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_matches_playlist_name(self):
        self.repo.upsertPlaylistName("pl1", "playlist", "Road Trip Mix")
        self.repo.insertPlay("alice", "t1", 100.0, 5000, playedFrom="playlist:pl1")
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "road trip")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_no_match_returns_empty(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)

        self.assertEqual(self.repo.searchPlays("alice", "nonexistent"), [])
        self.assertEqual(self.repo.searchPlaysCount("alice", "nonexistent"), 0)

    def test_oldest_first_orders_ascending(self):
        self.repo.insertPlay("alice", "t1", 300.0, 5000)
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t1", 200.0, 5000)

        results = self.repo.searchPlays("alice", "bohemian", oldestFirst=True)

        self.assertEqual([r["playedAt"] for r in results], [100.0, 200.0, 300.0])

    def test_percent_and_underscore_are_matched_literally_not_as_wildcards(self):
        self.repo.upsertTrack(makeSearchableTrack("t4", "100% Pure Love", "Random Artist", "Random Album"))
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t4", 200.0, 5000)

        results = self.repo.searchPlays("alice", "100%")

        self.assertEqual([r["id"] for r in results], ["t4"])

    def test_results_are_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("bob", "t1", 100.0, 5000)

        self.assertEqual(self.repo.searchPlaysCount("alice", "bohemian"), 1)
        self.assertEqual(len(self.repo.searchPlays("bob", "bohemian")), 1)

    def test_search_respects_date_range(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        results = self.repo.searchPlays("alice", "bohemian", startTs=1500.0, endTs=2500.0)

        self.assertEqual([r["playedAt"] for r in results], [2000.0])

    def test_search_count_respects_date_range(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        self.assertEqual(self.repo.searchPlaysCount("alice", "bohemian", startTs=1500.0, endTs=2500.0), 1)

    def test_ordered_newest_first(self):
        """"the" matches t1 via its album ("A Night at the Opera") and t2 via
        its own name ("...Bites the Dust")."""
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t2", 300.0, 5000)
        self.repo.insertPlay("alice", "t1", 200.0, 5000)

        results = self.repo.searchPlays("alice", "the")

        self.assertEqual([r["playedAt"] for r in results], [300.0, 200.0, 100.0])

    def test_limit_and_offset_paginate_matches(self):
        for i in range(5):
            self.repo.insertPlay("alice", "t1", float(i), 5000)

        page = self.repo.searchPlays("alice", "bohemian", limit=2, offset=1)

        self.assertEqual([r["playedAt"] for r in page], [3.0, 2.0])

    def test_count_matches_full_result_length(self):
        for i in range(5):
            self.repo.insertPlay("alice", "t1", float(i), 5000)
        self.repo.insertPlay("alice", "t3", 100.0, 5000)

        self.assertEqual(self.repo.searchPlaysCount("alice", "bohemian"), 5)
        self.assertEqual(len(self.repo.searchPlays("alice", "bohemian")), 5)


class TestTransactionControl(RepositoryTestCase):
    """upsertTrack/insertPlay don't auto-commit, so a caller (e.g. a bulk import)
    can compose several of them into one all-or-nothing transaction."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def test_uncommitted_track_upsert_is_still_visible_on_same_connection(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.assertTrue(self.repo.trackExists("t1"))  #< read-your-own-writes, no commit() call yet

    def test_uncommitted_play_insert_is_still_visible_on_same_connection(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_rollback_discards_uncommitted_track_and_play(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)

        self.repo.rollback()

        self.assertFalse(self.repo.trackExists("t1"))
        self.assertEqual(self.repo.getPlaysCount("alice"), 0)

    def test_commit_then_new_connection_sees_the_data(self):
        """Simulates a second thread (a fresh connection to the same file) reading
        after commit() - the real cross-thread visibility the app depends on."""
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        otherConnRepo = Repository(self.repo.connectionManager.dbPath)
        try:
            self.assertTrue(otherConnRepo.trackExists("t1"))
            self.assertEqual(otherConnRepo.getPlaysCount("alice"), 1)
        finally:
            otherConnRepo.connectionManager.close()


class TestStatsAggregates(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, *artistIds):
        track = makeTrack(trackId=trackId, albumId=albumId)
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        return track

    def test_get_all_tracks_reconstructs_every_track(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", "a2"))

        tracks = {t["id"]: t for t in self.repo.getAllTracks()}

        self.assertEqual(set(tracks.keys()), {"t1", "t2"})
        self.assertEqual([a["id"] for a in tracks["t2"]["artists"]], ["a1", "a2"])

    def test_artist_aggregates_grouped_by_artist_id_not_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        aggregates = {a["id"]: a for a in self.repo.getArtistAggregates("alice")}

        self.assertEqual(set(aggregates.keys()), {"a1", "a2"})
        self.assertEqual(aggregates["a1"]["plays"], 1)
        self.assertEqual(aggregates["a1"]["totalTimeListened"], 1000)
        self.assertEqual(aggregates["a1"]["uniqueSongCount"], 1)
        self.assertEqual(aggregates["a1"]["firstListenedAt"], 100.0)

    def test_artist_aggregates_filtered_by_artist_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", artistId="a1")

        self.assertEqual([a["id"] for a in aggregates], ["a1"])

    def test_artist_aggregates_filtered_by_unknown_artist_id_returns_empty(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistAggregates("alice", artistId="missing"), [])

    def test_artist_aggregates_filtered_by_artist_ids_returns_only_that_set(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.upsertTrack(self._track("t3", "alb1", "a3"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", artistIds=["a1", "a3"])

        self.assertCountEqual([a["id"] for a in aggregates], ["a1", "a3"])

    def test_artist_aggregates_filtered_by_empty_artist_ids_matches_nothing(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistAggregates("alice", artistIds=[]), [])

    def test_artist_aggregates_artist_ids_none_is_unfiltered(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        self.assertEqual(len(self.repo.getArtistAggregates("alice", artistIds=None)), 2)

    def test_artist_aggregates_unique_song_count(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)
        self.repo.commit()

        aggregates = {a["id"]: a for a in self.repo.getArtistAggregates("alice")}

        self.assertEqual(aggregates["a1"]["plays"], 3)
        self.assertEqual(aggregates["a1"]["uniqueSongCount"], 2)

    def test_artist_aggregates_sorted_by_plays_descending_by_default(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice")

        self.assertEqual([a["id"] for a in aggregates], ["a2", "a1"])

    def test_artist_aggregates_sorted_by_name_ascending(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", sortBy="name")

        self.assertEqual([a["id"] for a in aggregates], ["a1", "a2"])  #< "Artist a1" < "Artist a2"

    def test_artist_aggregates_name_sort_is_case_insensitive(self):
        """SQLite's default BINARY collation sorts every uppercase letter
        before every lowercase one, so "Banana" would otherwise land before
        "apple"/"cherry" instead of interleaving alphabetically by letter."""
        def trackWithArtist(trackId, artistId, artistName):
            track = makeTrack(trackId=trackId, albumId="alb1")
            track["artists"] = [{"id": artistId, "name": artistName, "url": "u", "imageUrl": "", "imageId": artistId}]
            return track

        self.repo.upsertTrack(trackWithArtist("t1", "a1", "apple"))
        self.repo.upsertTrack(trackWithArtist("t2", "a2", "Banana"))
        self.repo.upsertTrack(trackWithArtist("t3", "a3", "cherry"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", sortBy="name")

        self.assertEqual([a["id"] for a in aggregates], ["a1", "a2", "a3"])  #< apple, Banana, cherry

    def _trackWithNamedArtist(self, trackId, artistId, artistName):
        track = makeTrack(trackId=trackId, albumId="alb1")
        track["artists"] = [{"id": artistId, "name": artistName, "url": "u", "imageUrl": "", "imageId": artistId}]
        return track

    def test_artist_aggregates_plays_ties_break_by_name_a_to_z(self):
        """Artists tied on plays AND total time listened order A->Z - the
        name tiebreak column keeps its own ASC direction instead of
        inheriting the plays ranking's DESC. Zeta gets the SMALLER id so
        the final id-ASC fallback would order it first if the name leg
        regressed."""
        self.repo.upsertTrack(self._trackWithNamedArtist("t1", "a1", "Zeta"))
        self.repo.upsertTrack(self._trackWithNamedArtist("t2", "a9", "Alpha"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", sortBy="plays")

        self.assertEqual([a["name"] for a in aggregates], ["Alpha", "Zeta"])

    def test_artist_aggregates_name_sort_ties_break_by_most_time_listened(self):
        """Two different artists sharing one display name tie on the name
        sort - the time tiebreak ranks the MORE-listened one first (its own
        DESC direction, not the name sort's ASC). The louder artist gets
        the LARGER id so the id-ASC fallback would order it last if the
        time leg regressed."""
        self.repo.upsertTrack(self._trackWithNamedArtist("t1", "a1", "Same Name"))
        self.repo.upsertTrack(self._trackWithNamedArtist("t2", "a9", "Same Name"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 5000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", sortBy="name")

        self.assertEqual([a["id"] for a in aggregates], ["a9", "a1"])

    def test_artist_aggregates_rejects_unknown_sortby(self):
        with self.assertRaises(ValueError):
            self.repo.getArtistAggregates("alice", sortBy="not_a_real_column")

    def test_artist_aggregates_limit_and_offset_paginate(self):
        for i in range(5):
            trackId, artistId = f"t{i}", f"a{i}"
            self.repo.upsertTrack(self._track(trackId, "alb1", artistId))
            self.repo.insertPlay("alice", trackId, float(i), (i + 1) * 1000)  #< distinct play counts for a stable sort
        self.repo.commit()

        page = self.repo.getArtistAggregates("alice", limit=2, offset=1)

        self.assertEqual(len(page), 2)

    def test_artist_aggregates_filtered_by_search_query(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", searchQuery="a1")

        self.assertEqual([a["id"] for a in aggregates], ["a1"])

    def test_get_artists_count(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistsCount("alice"), 2)

    def test_get_artists_count_filtered_by_search_query(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistsCount("alice", searchQuery="a1"), 1)

    def test_get_artists_count_filtered_by_artist_ids(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistsCount("alice", artistIds=["a1"]), 1)
        self.assertEqual(self.repo.getArtistsCount("alice", artistIds=[]), 0)

    def test_get_artist_totals_sums_across_every_artist(self):
        """A multi-artist track's plays are counted once per artist on it - the
        totals are a sum of each artist's own aggregate, not the track-level
        total getPlayTotals() would give."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        totalPlays, totalUnique, totalTime = self.repo.getArtistTotals("alice")

        # a1: 2 plays (t1, t2), 2 unique songs; a2: 1 play (t1), 1 unique song.
        self.assertEqual(totalPlays, 3)
        self.assertEqual(totalUnique, 3)
        self.assertEqual(totalTime, 4000)

    def test_get_artist_totals_empty_range_returns_zeros(self):
        self.assertEqual(self.repo.getArtistTotals("alice"), (0, 0, 0))

    def test_ranged_play_queries_use_the_time_index(self):
        """The old static '(? IS NULL OR played_at >= ?)' range clause is
        non-sargable - SQLite can't use played_at as an index range bound
        through the OR, so every ranged query walked the user's whole play
        history. The clause must emit only the bounds that exist, letting
        the (username, played_at) index prune the scan."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()
        conn = self.repo._conn()

        params = ["alice"]
        clause = self.repo._dateRangeClause(params, 50.0, 150.0)
        plan = "\n".join(row[3] for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM plays WHERE username = ?{clause}", params))

        self.assertIn("idx_plays_user_time", plan)
        self.assertIn("played_at", plan)   #< the index is used as a RANGE scan, not just the username prefix

    def test_date_range_clause_emits_no_conditions_for_all_time(self):
        params = ["alice"]
        clause = self.repo._dateRangeClause(params, None, None)
        self.assertEqual(clause, "")
        self.assertEqual(params, ["alice"])

    def test_bucketed_play_totals_sums_within_a_bucket(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)   #< same 15-minute bucket as 100.0
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucketStartTs"], 0)
        self.assertEqual(rows[0]["plays"], 2)
        self.assertEqual(rows[0]["totalTimeListened"], 3000)

    def test_bucketed_play_totals_filtered_by_track_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice", trackId="t1")

        self.assertEqual([(r["plays"], r["totalTimeListened"]) for r in rows], [(1, 1000)])

    def test_bucketed_play_totals_filtered_by_artist_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice", artistId="a1")

        self.assertEqual([(r["plays"], r["totalTimeListened"]) for r in rows], [(1, 1000)])

    def test_bucketed_play_totals_filtered_by_album_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice", albumId="alb1")

        self.assertEqual([(r["plays"], r["totalTimeListened"]) for r in rows], [(1, 1000)])

    def test_bucketed_artist_play_counts_yield_one_count_per_artist(self):
        """A play whose track has N artists counts once per artist, matching
        the per-(play, artist) increment the old Python loop did."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        rows = self.repo.getBucketedArtistPlayCounts("alice")

        self.assertEqual(sorted((r["artistName"], r["plays"]) for r in rows),
                         [("Artist a1", 1), ("Artist a2", 1)])

    def test_play_totals(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        count, total = self.repo.getPlayTotals("alice")

        self.assertEqual(count, 2)
        self.assertEqual(total, 3000)

    def test_play_totals_empty_range_returns_zero(self):
        count, total = self.repo.getPlayTotals("alice")
        self.assertEqual((count, total), (0, 0))

    def test_play_at_boundary_belongs_to_exactly_one_adjacent_range(self):
        """The date-range clause implements the half-open interval
        [startTs, endTs) documented by app.py's _getDateRange - a play
        landing exactly on a shared boundary between two adjacent ranges
        must be counted in the later range only, not both."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 1000)
        self.repo.commit()

        earlierRange = self.repo.getPlayTotals("alice", startTs=0, endTs=1000.0)
        laterRange = self.repo.getPlayTotals("alice", startTs=1000.0, endTs=2000.0)

        self.assertEqual(earlierRange, (0, 0))
        self.assertEqual(laterRange, (1, 1000))

    def test_play_time_range_returns_first_and_last_played_at(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 500.0, 1000)
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 900.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getPlayTimeRange("alice"), (100.0, 900.0))

    def test_play_time_range_is_scoped_to_the_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("bob", "t1", 999.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getPlayTimeRange("alice"), (100.0, 100.0))

    def test_play_time_range_with_no_plays_is_none(self):
        self.assertIsNone(self.repo.getPlayTimeRange("alice"))


class TestSongsPage(RepositoryTestCase):
    """getSongsPage()/getSongsCount() replace the old N+1 getTrack()-per-row
    loop with a single batched query - these tests pin down the merged output
    shape, SQL-level ordering/tie-breaking, and LIMIT/OFFSET pagination."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, *artistIds, name=None, albumName=None):
        track = makeTrack(trackId=trackId, name=name or f"Song {trackId}", albumId=albumId)
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        if albumName is not None:
            track["album"]["name"] = albumName
        return track

    def test_returns_merged_shape_with_plays_and_track_metadata(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice")

        self.assertEqual(len(songs), 1)
        song = songs[0]
        self.assertEqual(song["id"], "t1")
        self.assertEqual(song["name"], "Song t1")
        self.assertEqual(song["album"]["id"], "alb1")
        self.assertEqual(song["plays"], 2)
        self.assertEqual(song["totalTimeListened"], 3000)
        self.assertEqual(song["firstListenedAt"], 100.0)

    def test_multi_artist_order_preserved(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2", "a3"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        song = self.repo.getSongsPage("alice")[0]

        self.assertEqual([a["id"] for a in song["artists"]], ["a1", "a2", "a3"])

    def _seedThreeSongs(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Bravo"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Alpha"))
        self.repo.upsertTrack(self._track("t3", "alb1", "a1", name="Charlie"))
        self.repo.insertPlay("alice", "t1", 100.0, 5000)   # t1: 1 play, 5000ms
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)   # t2: 2 plays, 2000ms
        self.repo.insertPlay("alice", "t3", 400.0, 9000)   # t3: 1 play, 9000ms
        self.repo.commit()

    def test_order_by_plays_descending(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", sortBy="plays")

        # t1 and t3 tie on plays (1 each); tie-break is totalTimeListened desc,
        # and t3's 9000ms beats t1's 5000ms.
        self.assertEqual([s["id"] for s in songs], ["t2", "t3", "t1"])

    def test_order_by_total_time_listened_descending(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", sortBy="totalTimeListened")

        self.assertEqual([s["id"] for s in songs], ["t3", "t1", "t2"])

    def test_order_by_name_ascending(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", sortBy="name")

        self.assertEqual([s["name"] for s in songs], ["Alpha", "Bravo", "Charlie"])

    def test_order_by_name_is_case_insensitive(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="apple"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Banana"))
        self.repo.upsertTrack(self._track("t3", "alb1", "a1", name="cherry"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", sortBy="name")

        self.assertEqual([s["name"] for s in songs], ["apple", "Banana", "cherry"])

    def test_plays_ties_break_by_name_a_to_z(self):
        """Songs tied on plays AND total time order A->Z (name keeps its
        own ASC direction in the plays ranking). Zeta gets the smaller
        track id so the id-ASC fallback would flip this if the name leg
        regressed."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Zeta"))
        self.repo.upsertTrack(self._track("t9", "alb1", "a1", name="Alpha"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t9", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", sortBy="plays")

        self.assertEqual([s["name"] for s in songs], ["Alpha", "Zeta"])

    def test_invalid_sort_by_raises_value_error(self):
        self._seedThreeSongs()

        with self.assertRaises(ValueError):
            self.repo.getSongsPage("alice", sortBy="; DROP TABLE plays;--")

    def test_order_by_recent_descending(self):
        self._seedThreeSongs()
        # t3 (400.0) was played most recently, then t2 (300.0), then t1 (100.0).

        songs = self.repo.getSongsPage("alice", sortBy="recent")

        self.assertEqual([s["id"] for s in songs], ["t3", "t2", "t1"])

    def test_recent_uses_latest_play_not_first(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="First played, last replayed"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Only played once, in between"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t1", 300.0, 1000)   # t1's most recent play beats t2's
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", sortBy="recent")

        self.assertEqual([s["id"] for s in songs], ["t1", "t2"])
        self.assertEqual(songs[0]["lastPlayedAt"], 300.0)

    def test_limit_and_offset_paginate_default_order(self):
        self._seedThreeSongs()

        firstPage = self.repo.getSongsPage("alice", sortBy="plays", limit=2, offset=0)
        secondPage = self.repo.getSongsPage("alice", sortBy="plays", limit=2, offset=2)

        self.assertEqual([s["id"] for s in firstPage], ["t2", "t3"])
        self.assertEqual([s["id"] for s in secondPage], ["t1"])

    def test_limit_none_returns_everything(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", limit=None)

        self.assertEqual(len(songs), 3)

    def test_offset_past_end_returns_empty(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", limit=2, offset=10)

        self.assertEqual(songs, [])

    def test_no_plays_returns_empty(self):
        songs = self.repo.getSongsPage("alice")
        self.assertEqual(songs, [])

    def test_tied_rows_paginate_deterministically(self):
        """Two songs identical on plays/totalTimeListened/name have no natural
        tie-break in SQL GROUP BY output order - track_id is used as the final
        tie-break so paging never repeats or drops a row."""
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Same"))
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Same"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        firstCall = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays")]
        secondCall = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays")]
        page1 = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays", limit=1, offset=0)]
        page2 = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays", limit=1, offset=1)]

        self.assertEqual(firstCall, secondCall)
        self.assertEqual(page1 + page2, firstCall)

    def test_date_range_filtering(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 2000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", startTs=0, endTs=1000)

        self.assertEqual(songs[0]["plays"], 1)
        self.assertEqual(songs[0]["totalTimeListened"], 1000)

    def test_search_query_matches_track_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Bohemian Rhapsody"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2", name="Unrelated"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", searchQuery="bohemian")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_search_query_matches_artist_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", searchQuery="Artist a1")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_search_query_matches_album_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", albumName="A Night at the Opera"))
        self.repo.upsertTrack(self._track("t2", "alb2", "a1", albumName="Unrelated Album"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", searchQuery="night at the opera")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_search_query_paginates_with_getSongsCount(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Bohemian Rhapsody"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Bohemian Remix"))
        self.repo.upsertTrack(self._track("t3", "alb1", "a1", name="Unrelated"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getSongsCount("alice", searchQuery="bohemian"), 2)
        page = self.repo.getSongsPage("alice", searchQuery="bohemian", limit=1, offset=0)
        self.assertEqual(len(page), 1)

    def test_missing_album_row_falls_back_like_get_track(self):
        """The LEFT JOIN can in principle return no matching album row, and
        _songRowToDict must degrade gracefully like getTrack()'s equivalent
        fallback - exercised directly against a synthetic row rather than via
        the database, since tracks.album_id is a NOT NULL foreign key and this
        state can't actually be produced through the public API (see
        test_db_schema.py::test_foreign_keys_enforced)."""
        row = {
            "track_id": "t1", "name": "Song", "url": "u", "image_id": "img1",
            "duration_ms": 1000, "explicit": 0, "isrc": None,
            "disc_number": 1, "track_number": 1, "created_reason": None,
            "availability_reason": None,
            "album_id": None, "album_name": None, "album_url": None,
            "album_total_tracks": None, "album_release_date": None,
            "album_image_id": None, "album_image_url": None,
            "plays": 1, "total_time_listened": 1000, "first_listened_at": 100.0,
            "last_played_at": 100.0,
        }

        song = Repository._songRowToDict(row, [])

        self.assertIsNone(song["album"])
        self.assertEqual(song["imageUrl"], "")
        self.assertIsNone(song["releaseDate"])

    def test_songs_count_matches_distinct_track_count(self):
        self._seedThreeSongs()
        self.assertEqual(self.repo.getSongsCount("alice"), 3)

    def test_songs_count_respects_date_range(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getSongsCount("alice", startTs=0, endTs=1000), 1)

    def test_songs_count_zero_when_no_plays(self):
        self.assertEqual(self.repo.getSongsCount("alice"), 0)

    def test_filtered_by_track_id_returns_only_that_track(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", trackId="t2")

        self.assertEqual([s["id"] for s in songs], ["t2"])

    def test_filtered_by_track_id_unknown_returns_empty(self):
        self._seedThreeSongs()

        self.assertEqual(self.repo.getSongsPage("alice", trackId="missing"), [])

    def test_filtered_by_track_ids_returns_only_that_set(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", trackIds=["t1", "t3"])

        self.assertCountEqual([s["id"] for s in songs], ["t1", "t3"])

    def test_filtered_by_track_ids_preserves_sort_order(self):
        self._seedThreeSongs()   # totalTimeListened: t3=9000 > t1=5000 > t2=2000

        songs = self.repo.getSongsPage("alice", sortBy="totalTimeListened", trackIds=["t1", "t2", "t3"])

        self.assertEqual([s["id"] for s in songs], ["t3", "t1", "t2"])

    def test_filtered_by_empty_track_ids_matches_nothing(self):
        self._seedThreeSongs()

        self.assertEqual(self.repo.getSongsPage("alice", trackIds=[]), [])

    def test_track_ids_none_is_unfiltered(self):
        self._seedThreeSongs()

        self.assertEqual(len(self.repo.getSongsPage("alice", trackIds=None)), 3)

    def test_songs_count_filtered_by_track_ids(self):
        self._seedThreeSongs()

        self.assertEqual(self.repo.getSongsCount("alice", trackIds=["t1", "t3"]), 2)
        self.assertEqual(self.repo.getSongsCount("alice", trackIds=[]), 0)
        self.assertEqual(self.repo.getSongsCount("alice", trackIds=None), 3)

    def test_songs_count_filtered_by_track_ids_combines_with_search_query(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Bohemian Rhapsody"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Bohemian Remix"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        self.assertEqual(
            self.repo.getSongsCount("alice", searchQuery="bohemian", trackIds=["t1"]), 1)

    def test_filtered_by_artist_id_returns_only_that_artists_songs(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Song One"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2", name="Song Two"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", artistId="a1")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_filtered_by_artist_id_does_not_duplicate_multi_artist_tracks(self):
        """A track credited to multiple artists must still yield exactly one
        row (not one per matching artist) when filtered by one of them."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", artistId="a1")

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["plays"], 2)

    def test_filtered_by_album_id_returns_only_that_albums_songs(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Song One"))
        self.repo.upsertTrack(self._track("t2", "alb2", "a1", name="Song Two"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", albumId="alb1")

        self.assertEqual([s["id"] for s in songs], ["t1"])


class TestAlbumsPage(RepositoryTestCase):
    """getAlbumsPage()/getAlbumsCount() aggregate plays by album, mirroring
    getSongsPage()'s batched sort/page/date-range pattern."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, albumName, *artistIds):
        track = makeTrack(trackId=trackId, albumId=albumId)
        track["album"]["name"] = albumName
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        return track

    def test_returns_merged_shape_with_plays_and_album_metadata(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice")

        self.assertEqual(len(albums), 1)
        album = albums[0]
        self.assertEqual(album["id"], "alb1")
        self.assertEqual(album["name"], "Album One")
        self.assertEqual(album["plays"], 2)
        self.assertEqual(album["totalTimeListened"], 3000)
        self.assertEqual(album["firstListenedAt"], 100.0)
        self.assertEqual(album["uniqueSongCount"], 1)
        self.assertEqual([a["id"] for a in album["artists"]], ["a1"])

    def test_plays_across_multiple_tracks_on_same_album_are_combined(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        album = self.repo.getAlbumsPage("alice")[0]

        self.assertEqual(album["plays"], 2)
        self.assertEqual(album["totalTimeListened"], 3000)
        self.assertEqual(album["uniqueSongCount"], 2)

    def test_artists_across_the_album_are_deduplicated(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1", "a2"))
        self.repo.upsertTrack(self._track("t2", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        album = self.repo.getAlbumsPage("alice")[0]

        self.assertEqual(sorted(a["id"] for a in album["artists"]), ["a1", "a2"])

    def _seedThreeAlbums(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Bravo", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Alpha", "a1"))
        self.repo.upsertTrack(self._track("t3", "alb3", "Charlie", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 5000)   # alb1: 1 play, 5000ms
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)   # alb2: 2 plays, 2000ms
        self.repo.insertPlay("alice", "t3", 400.0, 9000)   # alb3: 1 play, 9000ms
        self.repo.commit()

    def test_order_by_plays_descending(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", sortBy="plays")

        # alb1 and alb3 tie on plays (1 each); tie-break is totalTimeListened desc.
        self.assertEqual([a["id"] for a in albums], ["alb2", "alb3", "alb1"])

    def test_order_by_total_time_listened_descending(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", sortBy="totalTimeListened")

        self.assertEqual([a["id"] for a in albums], ["alb3", "alb1", "alb2"])

    def test_order_by_name_ascending(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", sortBy="name")

        self.assertEqual([a["name"] for a in albums], ["Alpha", "Bravo", "Charlie"])

    def test_order_by_name_is_case_insensitive(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "apple", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Banana", "a1"))
        self.repo.upsertTrack(self._track("t3", "alb3", "cherry", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", sortBy="name")

        self.assertEqual([a["name"] for a in albums], ["apple", "Banana", "cherry"])

    def test_plays_ties_break_by_name_a_to_z(self):
        """Albums tied on plays AND total time order A->Z (name keeps its
        own ASC direction in the plays ranking). Zeta gets the smaller
        album id so the id-ASC fallback would flip this if the name leg
        regressed."""
        self.repo.upsertTrack(self._track("t1", "alb1", "Zeta", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb9", "Alpha", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", sortBy="plays")

        self.assertEqual([a["name"] for a in albums], ["Alpha", "Zeta"])

    def test_invalid_sort_by_raises_value_error(self):
        self._seedThreeAlbums()

        with self.assertRaises(ValueError):
            self.repo.getAlbumsPage("alice", sortBy="; DROP TABLE plays;--")

    def test_limit_and_offset_paginate_default_order(self):
        self._seedThreeAlbums()

        firstPage = self.repo.getAlbumsPage("alice", sortBy="plays", limit=2, offset=0)
        secondPage = self.repo.getAlbumsPage("alice", sortBy="plays", limit=2, offset=2)

        self.assertEqual([a["id"] for a in firstPage], ["alb2", "alb3"])
        self.assertEqual([a["id"] for a in secondPage], ["alb1"])

    def test_limit_none_returns_everything(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", limit=None)

        self.assertEqual(len(albums), 3)

    def test_offset_past_end_returns_empty(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", limit=2, offset=10)

        self.assertEqual(albums, [])

    def test_no_plays_returns_empty(self):
        albums = self.repo.getAlbumsPage("alice")
        self.assertEqual(albums, [])

    def test_date_range_filtering(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 2000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", startTs=0, endTs=1000)

        self.assertEqual(albums[0]["plays"], 1)
        self.assertEqual(albums[0]["totalTimeListened"], 1000)

    def test_albums_count_matches_distinct_album_count(self):
        self._seedThreeAlbums()
        self.assertEqual(self.repo.getAlbumsCount("alice"), 3)

    def test_albums_count_respects_date_range(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getAlbumsCount("alice", startTs=0, endTs=1000), 1)

    def test_albums_count_zero_when_no_plays(self):
        self.assertEqual(self.repo.getAlbumsCount("alice"), 0)

    def test_filtered_by_album_id_returns_only_that_album(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", albumId="alb2")

        self.assertEqual([a["id"] for a in albums], ["alb2"])

    def test_filtered_by_album_id_unknown_returns_empty(self):
        self._seedThreeAlbums()

        self.assertEqual(self.repo.getAlbumsPage("alice", albumId="missing"), [])

    def test_filtered_by_album_ids_returns_only_that_set(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", albumIds=["alb1", "alb3"])

        self.assertCountEqual([a["id"] for a in albums], ["alb1", "alb3"])

    def test_filtered_by_empty_album_ids_matches_nothing(self):
        self._seedThreeAlbums()

        self.assertEqual(self.repo.getAlbumsPage("alice", albumIds=[]), [])

    def test_album_ids_none_is_unfiltered(self):
        self._seedThreeAlbums()

        self.assertEqual(len(self.repo.getAlbumsPage("alice", albumIds=None)), 3)

    def test_albums_count_filtered_by_album_ids(self):
        self._seedThreeAlbums()

        self.assertEqual(self.repo.getAlbumsCount("alice", albumIds=["alb1", "alb2"]), 2)
        self.assertEqual(self.repo.getAlbumsCount("alice", albumIds=[]), 0)

    def test_search_query_matches_album_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "A Night at the Opera", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Unrelated Album", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", searchQuery="night at the opera")

        self.assertEqual([a["id"] for a in albums], ["alb1"])

    def test_search_query_matches_artist_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Album Two", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", searchQuery="Artist a1")

        self.assertEqual([a["id"] for a in albums], ["alb1"])

    def test_search_match_on_one_track_still_aggregates_the_whole_album(self):
        """A row-level filter (checking only the current play's own track)
        would silently shrink a matching album's totals down to just its
        matching track's plays - the search must be evaluated per-album so a
        match still returns the album's TRUE totals across every track on it,
        matching exactly what a non-search fetch of the same album returns."""
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))     # matches "Artist a1"
        self.repo.upsertTrack(self._track("t2", "alb1", "Album One", "a2"))     # does not match, same album
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        searched = self.repo.getAlbumsPage("alice", searchQuery="Artist a1")
        unfiltered = self.repo.getAlbumsPage("alice")

        self.assertEqual(len(searched), 1)
        self.assertEqual(searched[0]["plays"], unfiltered[0]["plays"])
        self.assertEqual(searched[0]["totalTimeListened"], unfiltered[0]["totalTimeListened"])
        self.assertEqual(searched[0]["uniqueSongCount"], unfiltered[0]["uniqueSongCount"])
        self.assertEqual(searched[0]["plays"], 2)
        self.assertEqual(searched[0]["totalTimeListened"], 3000)

    def test_search_query_paginates_with_getAlbumsCount(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Bohemian Album", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Bohemian Remix", "a1"))
        self.repo.upsertTrack(self._track("t3", "alb3", "Unrelated", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getAlbumsCount("alice", searchQuery="bohemian"), 2)
        page = self.repo.getAlbumsPage("alice", searchQuery="bohemian", limit=1, offset=0)
        self.assertEqual(len(page), 1)


class TestFullPlaysOnlyFilter(RepositoryTestCase):
    """`fullPlaysOnly` on getSongsPage/getSongsCount, getAlbumsPage/
    getAlbumsCount, getArtistAggregates/getArtistsCount/getArtistTotals, and
    getPlayTotals - a play only counts as "full" once it reaches the admin's
    completion-complete percent of the track's duration (COMPLETION_COMPLETE_
    PERCENT_KEY, default 80%), same standard as getCompletionStats() and the
    Forgotten Favorite trend. Every track here uses makeTrack()'s default
    200000ms duration, so the default 80% threshold is 160000ms."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, *artistIds, durationMs=200000):
        track = makeTrack(trackId=trackId, albumId=albumId)
        track["duration"] = durationMs
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        return track

    def test_songs_page_excludes_partial_only_track_when_enabled(self):
        self.repo.upsertTrack(self._track("full", "alb1", "a1"))
        self.repo.upsertTrack(self._track("partial", "alb1", "a1"))
        self.repo.insertPlay("alice", "full", 100.0, 200000)      # 100% - full
        self.repo.insertPlay("alice", "partial", 200.0, 50000)    # 25% - partial
        self.repo.commit()

        unfiltered = {s["id"] for s in self.repo.getSongsPage("alice")}
        filtered = {s["id"] for s in self.repo.getSongsPage("alice", fullPlaysOnly=True)}

        self.assertEqual(unfiltered, {"full", "partial"})
        self.assertEqual(filtered, {"full"})

    def test_songs_count_matches_songs_page_filtering(self):
        self.repo.upsertTrack(self._track("full", "alb1", "a1"))
        self.repo.upsertTrack(self._track("partial", "alb1", "a1"))
        self.repo.insertPlay("alice", "full", 100.0, 200000)
        self.repo.insertPlay("alice", "partial", 200.0, 50000)
        self.repo.commit()

        self.assertEqual(self.repo.getSongsCount("alice"), 2)
        self.assertEqual(self.repo.getSongsCount("alice", fullPlaysOnly=True), 1)
        # The search-query path joins tracks differently - must agree too.
        self.assertEqual(self.repo.getSongsCount("alice", searchQuery="Song", fullPlaysOnly=True), 1)

    def test_albums_page_and_count_exclude_partial_only_album(self):
        self.repo.upsertTrack(self._track("full", "alb-full", "a1"))
        self.repo.upsertTrack(self._track("partial", "alb-partial", "a1"))
        self.repo.insertPlay("alice", "full", 100.0, 200000)
        self.repo.insertPlay("alice", "partial", 200.0, 50000)
        self.repo.commit()

        self.assertEqual(self.repo.getAlbumsCount("alice"), 2)
        self.assertEqual(self.repo.getAlbumsCount("alice", fullPlaysOnly=True), 1)
        self.assertEqual(self.repo.getAlbumsCount("alice", searchQuery="alb", fullPlaysOnly=True), 1)
        filtered = {a["id"] for a in self.repo.getAlbumsPage("alice", fullPlaysOnly=True)}
        self.assertEqual(filtered, {"alb-full"})

    def test_artist_queries_exclude_artist_whose_only_plays_are_partial(self):
        self.repo.upsertTrack(self._track("full", "alb1", "a-full"))
        self.repo.upsertTrack(self._track("partial", "alb1", "a-partial"))
        self.repo.insertPlay("alice", "full", 100.0, 200000)
        self.repo.insertPlay("alice", "partial", 200.0, 50000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistsCount("alice"), 2)
        self.assertEqual(self.repo.getArtistsCount("alice", fullPlaysOnly=True), 1)

        filtered = {a["id"] for a in self.repo.getArtistAggregates("alice", fullPlaysOnly=True)}
        self.assertEqual(filtered, {"a-full"})

        totalPlays, totalUnique, totalTime = self.repo.getArtistTotals("alice", fullPlaysOnly=True)
        self.assertEqual((totalPlays, totalUnique, totalTime), (1, 1, 200000))

    def test_artist_aggregate_counts_only_the_full_plays_of_a_mixed_artist(self):
        """One artist with both a full and a partial play - the partial play
        must not inflate that artist's own plays/time when the filter is on,
        not just cause a different artist to be dropped entirely."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 200000)   # full
        self.repo.insertPlay("alice", "t2", 200.0, 50000)    # partial
        self.repo.commit()

        artist = self.repo.getArtistAggregates("alice", fullPlaysOnly=True)[0]

        self.assertEqual(artist["plays"], 1)
        self.assertEqual(artist["totalTimeListened"], 200000)

    def test_play_totals_excludes_partial_plays_when_enabled(self):
        self.repo.upsertTrack(self._track("full", "alb1", "a1"))
        self.repo.upsertTrack(self._track("partial", "alb1", "a1"))
        self.repo.insertPlay("alice", "full", 100.0, 200000)
        self.repo.insertPlay("alice", "partial", 200.0, 50000)
        self.repo.commit()

        self.assertEqual(self.repo.getPlayTotals("alice"), (2, 250000))
        self.assertEqual(self.repo.getPlayTotals("alice", fullPlaysOnly=True), (1, 200000))

    def test_unknown_duration_track_counts_as_full(self):
        """Mirrors getCompletionStats(): a track with duration_ms<=0 can't be
        told apart from a full listen, so it counts as complete rather than
        being penalized for missing metadata."""
        self.repo.upsertTrack(self._track("unknown", "alb1", "a1", durationMs=0))
        self.repo.insertPlay("alice", "unknown", 100.0, 5000)
        self.repo.commit()

        self.assertEqual(self.repo.getSongsCount("alice", fullPlaysOnly=True), 1)

    def test_respects_admin_tunable_completion_percent(self):
        """Lowering the completion-complete percent must lower the full-play
        bar for every one of these queries, not just getCompletionStats()."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 110000)   # 55% of 200000ms
        self.repo.commit()

        # Under the default 80% bar, 55% doesn't qualify as a full play...
        self.assertEqual(self.repo.getSongsCount("alice", fullPlaysOnly=True), 0)

        # ...but lowering the admin's bar to 50% (the allowed minimum) does.
        self.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_MIN,
                                 COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)
        self.assertEqual(self.repo.getSongsCount("alice", fullPlaysOnly=True), 1)


class TestUsersAndCookies(RepositoryTestCase):
    def test_upsert_and_lookup_by_email(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertEqual(self.repo.getUsernameForEmail("alice@example.com"), "alice")

    def test_lookup_by_email_is_case_insensitive(self):
        """The row is stored exactly as typed at registration - only the
        lookup folds case (COLLATE NOCASE), so a later login/cookie-refresh
        typed in a different case still resolves to the same account instead
        of minting a second users row."""
        self.repo.upsertUser("alice", "Alice@Example.com")

        self.assertEqual(self.repo.getUsernameForEmail("alice@example.com"), "alice")
        self.assertEqual(self.repo.getUsernameForEmail("ALICE@EXAMPLE.COM"), "alice")
        self.assertEqual(self.repo.getUsernameForEmail("Alice@Example.com"), "alice")

    def test_unknown_email_returns_none(self):
        self.assertIsNone(self.repo.getUsernameForEmail("nobody@example.com"))

    def test_username_exists(self):
        self.assertFalse(self.repo.usernameExists("alice"))
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertTrue(self.repo.usernameExists("alice"))

    def test_upsert_is_idempotent_on_conflict(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("alice", "alice@example.com")  #< must not raise
        self.assertTrue(self.repo.usernameExists("alice"))

    def test_get_email_for_username(self):
        self.assertIsNone(self.repo.getEmailForUsername("alice"))  #< doesn't exist yet
        self.repo.upsertUser("alice", None)
        self.assertIsNone(self.repo.getEmailForUsername("alice"))  #< exists, but no email on record
        self.repo.upsertUser("bob", "bob@example.com")
        self.assertEqual(self.repo.getEmailForUsername("bob"), "bob@example.com")

    def test_set_user_email_claims_an_orphaned_username(self):
        self.repo.upsertUser("alice", None)

        self.repo.setUserEmail("alice", "alice@example.com")

        self.assertEqual(self.repo.getEmailForUsername("alice"), "alice@example.com")
        self.assertEqual(self.repo.getUsernameForEmail("alice@example.com"), "alice")

    def test_cookies_default_to_none(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertIsNone(self.repo.getUserCookies("alice"))

    def test_cookies_roundtrip(self):
        self.repo.upsertUser("alice", "alice@example.com")
        cookies = {"sp_dc": "abc123", "sp_key": "def456"}

        self.repo.setUserCookies("alice", cookies)

        self.assertEqual(self.repo.getUserCookies("alice"), cookies)

    def test_cookies_can_be_updated(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.setUserCookies("alice", {"sp_dc": "old"})
        self.repo.setUserCookies("alice", {"sp_dc": "new"})
        self.assertEqual(self.repo.getUserCookies("alice"), {"sp_dc": "new"})

    def test_get_all_users_with_cookies_excludes_users_without_cookies(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.setUserCookies("alice", {"sp_dc": "abc"})

        result = self.repo.getAllUsersWithCookies()

        self.assertEqual(result, [("alice", "alice@example.com")])

    def test_get_all_users_with_cookies_empty_when_none_logged_in(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertEqual(self.repo.getAllUsersWithCookies(), [])

    def test_password_hash_defaults_to_none(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertIsNone(self.repo.getUserPasswordHash("alice"))

    def test_password_hash_roundtrip(self):
        self.repo.upsertUser("alice", "alice@example.com")

        self.repo.setUserPassword("alice", "hashed-value")

        self.assertEqual(self.repo.getUserPasswordHash("alice"), "hashed-value")

    def test_password_hash_can_be_updated(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.setUserPassword("alice", "old-hash")
        self.repo.setUserPassword("alice", "new-hash")
        self.assertEqual(self.repo.getUserPasswordHash("alice"), "new-hash")

    def test_add_user_password_hash_column_if_missing_is_a_noop_when_present(self):
        """The column already exists via SCHEMA on a fresh test database -
        calling this again (as migrate1_8_0 does defensively) must not raise."""
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.addUserPasswordHashColumnIfMissing()
        self.assertIsNone(self.repo.getUserPasswordHash("alice"))

    def test_session_version_starts_at_zero(self):
        """Zero, not NULL: a session cookie minted before this column existed
        carries no version at all, and the check reads a missing one as 0 - so
        the two have to agree or the upgrade logs everyone out."""
        self.repo.upsertUser("alice", "alice@example.com")

        self.assertEqual(self.repo.getUserSessionVersion("alice"), 0)

    def test_bumping_the_session_version_advances_it(self):
        self.repo.upsertUser("alice", "alice@example.com")

        self.assertEqual(self.repo.bumpUserSessionVersion("alice"), 1)
        self.assertEqual(self.repo.bumpUserSessionVersion("alice"), 2)
        self.assertEqual(self.repo.getUserSessionVersion("alice"), 2)

    def test_bumping_one_user_leaves_another_alone(self):
        """The counter ends THIS account's sessions. A shared bump would log
        the whole instance out of every device on one password reset."""
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

        self.repo.bumpUserSessionVersion("alice")

        self.assertEqual(self.repo.getUserSessionVersion("bob"), 0)

    def test_an_unknown_user_has_no_session_version(self):
        """None, not 0: "no such account" and "this account has never bumped"
        are different answers, and this layer is where they stay apart.

        What the app then does with None is its own decision, and NOT the
        obvious one: SpotifyDashboardApp.sessionIsCurrent reads it as 0 on
        purpose, because it resolved the username from this same table a line
        earlier - see its docstring before "hardening" that away. Nothing here
        promises otherwise."""
        self.assertIsNone(self.repo.getUserSessionVersion("nobody"))

    def test_bumping_an_unknown_user_is_a_no_op(self):
        self.assertIsNone(self.repo.bumpUserSessionVersion("nobody"))

    def test_add_session_version_column_if_missing_is_a_noop_when_present(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.bumpUserSessionVersion("alice")

        self.repo.addUserSessionVersionColumnIfMissing()

        #< idempotent, and it does not reset what is already there
        self.assertEqual(self.repo.getUserSessionVersion("alice"), 1)


class TestImportProgress(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def test_unknown_user_returns_none(self):
        self.assertIsNone(self.repo.readProgress("alice"))

    def test_write_then_read(self):
        self.repo.writeProgress("alice", "running", 5, 10, "Imported 5 of 10", False)

        progress = self.repo.readProgress("alice")

        self.assertEqual(progress["status"], "running")
        self.assertEqual(progress["current"], 5)
        self.assertEqual(progress["total"], 10)
        self.assertEqual(progress["percentage"], 50)
        self.assertEqual(progress["message"], "Imported 5 of 10")
        self.assertFalse(progress["error"])

    def test_percentage_zero_when_total_is_zero(self):
        self.repo.writeProgress("alice", "running", 0, 0, "Starting", False)
        self.assertEqual(self.repo.readProgress("alice")["percentage"], 0)

    def test_write_overwrites_previous_progress(self):
        self.repo.writeProgress("alice", "running", 1, 10, "step 1", False)
        self.repo.writeProgress("alice", "complete", 10, 10, "done", False)

        progress = self.repo.readProgress("alice")
        self.assertEqual(progress["status"], "complete")
        self.assertEqual(progress["current"], 10)

    def test_progress_is_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.writeProgress("alice", "running", 1, 10, "", False)

        self.assertIsNone(self.repo.readProgress("bob"))


class TestUserSettings(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def test_default_settings_returned_for_new_user(self):
        settings = self.repo.getUserSettings("alice")
        self.assertEqual(settings["default_dashboard_window"], "day")
        self.assertIsNone(settings["timezone"])
        self.assertFalse(settings["hide_tags_panel"])

    def test_update_and_get_settings(self):
        self.repo.updateUserSettings("alice", "month", "Europe/London")
        settings = self.repo.getUserSettings("alice")
        self.assertEqual(settings["default_dashboard_window"], "month")
        self.assertEqual(settings["timezone"], "Europe/London")
        self.assertFalse(settings["hide_tags_panel"])   #< defaults to False when omitted

    def test_update_and_get_settings_with_hide_tags_panel(self):
        self.repo.updateUserSettings("alice", "month", "Europe/London", hide_tags_panel=True)
        settings = self.repo.getUserSettings("alice")
        self.assertTrue(settings["hide_tags_panel"])

        self.repo.updateUserSettings("alice", "month", "Europe/London", hide_tags_panel=False)
        self.assertFalse(self.repo.getUserSettings("alice")["hide_tags_panel"])

    def test_an_omitted_dashboard_window_is_left_alone(self):
        """save_preferences documents None as "never submitted, leave it
        alone" for BOTH selects, and updateUserSettings implemented it for
        default_top_list_window only - the dashboard one went straight into
        the UPDATE against a nullable column, so a form that omitted it stored
        NULL and every page silently fell back to "Yesterday" (2026-09-03
        review, L5)."""
        self.repo.updateUserSettings("alice", "month", "Europe/London")

        self.repo.updateUserSettings("alice", None, "Europe/London")

        self.assertEqual(self.repo.getUserSettings("alice")["default_dashboard_window"], "month")

    def test_an_omitted_dashboard_window_still_saves_the_rest(self):
        """"Leave it alone" must not mean "skip the write" - the other fields
        in the same call are what the caller is actually saving."""
        self.repo.updateUserSettings("alice", "month", None)

        self.repo.updateUserSettings("alice", None, "Asia/Tokyo", hide_tags_panel=True)

        settings = self.repo.getUserSettings("alice")
        self.assertEqual(settings["default_dashboard_window"], "month")
        self.assertEqual(settings["timezone"], "Asia/Tokyo")
        self.assertTrue(settings["hide_tags_panel"])

    def test_settings_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.updateUserSettings("alice", "week", "Asia/Tokyo", hide_tags_panel=True)

        bob_settings = self.repo.getUserSettings("bob")
        self.assertEqual(bob_settings["default_dashboard_window"], "day")
        self.assertIsNone(bob_settings["timezone"])
        self.assertFalse(bob_settings["hide_tags_panel"])

    def test_get_hide_tags_panel_cheap_read(self):
        self.assertFalse(self.repo.getHideTagsPanel("alice"))
        self.repo.updateUserSettings("alice", "day", None, hide_tags_panel=True)
        self.assertTrue(self.repo.getHideTagsPanel("alice"))

    def test_get_hide_tags_panel_unknown_user_returns_false(self):
        self.assertFalse(self.repo.getHideTagsPanel("nobody"))

    def test_top_list_window_defaults_to_all_time(self):
        """The Top pages ranked all-time before this setting existed, so its
        default is what keeps every existing account seeing what it saw."""
        self.assertEqual(self.repo.getUserSettings("alice")["default_top_list_window"],
                         TOP_LIST_DEFAULT_WINDOW)

    def test_unknown_user_gets_the_top_list_window_default_too(self):
        self.assertEqual(self.repo.getUserSettings("nobody")["default_top_list_window"],
                         TOP_LIST_DEFAULT_WINDOW)

    def test_update_and_get_top_list_window(self):
        self.repo.updateUserSettings("alice", "month", None, default_top_list_window="year")
        self.assertEqual(self.repo.getUserSettings("alice")["default_top_list_window"], "year")

    def test_omitting_the_top_list_window_leaves_it_alone(self):
        """None means "not submitted", not "reset to the default". Several
        callers predate this parameter, and a plain default would have silently
        put every one of them back on All Time."""
        self.repo.updateUserSettings("alice", "month", None, default_top_list_window="week")
        self.repo.updateUserSettings("alice", "day", "Europe/London")

        settings = self.repo.getUserSettings("alice")
        self.assertEqual(settings["default_top_list_window"], "week")
        self.assertEqual(settings["default_dashboard_window"], "day")   #< the rest still saved

    def test_top_list_window_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.updateUserSettings("alice", "day", None, default_top_list_window="week")

        self.assertEqual(self.repo.getUserSettings("bob")["default_top_list_window"],
                         TOP_LIST_DEFAULT_WINDOW)


class TestImportProgressClaim(RepositoryTestCase):
    def test_claim_running_is_atomic_and_single_winner(self):
        self.repo.upsertUser("alice", "alice@example.com")

        # First claim wins and marks the import running.
        self.assertTrue(self.repo.tryClaimImportRunning("alice"))
        self.assertEqual(self.repo.readProgress("alice")["status"], "running")

        # A second claim while it's still running is rejected.
        self.assertFalse(self.repo.tryClaimImportRunning("alice"))

        # Once it's no longer running, a fresh claim succeeds again.
        self.repo.writeProgress("alice", "done", 1, 1, "Done", False)
        self.assertTrue(self.repo.tryClaimImportRunning("alice"))

    def test_claim_is_per_user(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

        self.assertTrue(self.repo.tryClaimImportRunning("alice"))
        # bob's slot is independent of alice's.
        self.assertTrue(self.repo.tryClaimImportRunning("bob"))
        self.assertFalse(self.repo.tryClaimImportRunning("alice"))


class TestRollbackQuietly(RepositoryTestCase):
    """A rollback in an `except` must never replace the failure that caused it.

    Every caller sits inside an except that goes on to re-raise or report the
    ORIGINAL exception, and Database.utils.parseError reads only the exception
    it is handed - it never walks __context__. So a rollback that raised took
    the real cause's place in the log, in the user-facing import progress line
    and in listener_last_error, precisely when the database is unhealthy, which
    is when rollback is most likely to fail in the first place.

    Swallowed but NOT silent: a failed rollback discards the connection so its
    still-open transaction cannot leak into a later operation."""

    def test_a_successful_rollback_discards_the_staged_write(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.commit()
        self.repo.upsertTrack(makeTrack(trackId="staged"))

        self.assertTrue(self.repo.rollbackQuietly())

        self.assertIsNone(self.repo.getTrack("staged"))
        self.assertFalse(self.repo._conn().in_transaction)

    def test_a_failing_rollback_does_not_raise(self):
        original = self.repo._conn()
        with patch.object(self.repo, "rollback",
                          side_effect=RuntimeError("cannot operate on a closed database")):
            self.assertFalse(self.repo.rollbackQuietly())   #< must not raise

        self.assertIsNot(self.repo._conn(), original)

    def test_a_failing_rollback_is_reported(self):
        """Discarding the connection is still reported alongside the failure."""
        with patch.object(self.repo, "rollback",
                          side_effect=RuntimeError("cannot operate on a closed database")):
            with self.assertLogs("Database.repository", level="ERROR") as logs:
                self.repo.rollbackQuietly()

        self.assertIn("cannot operate on a closed database", str(logs.output))

    def test_close_forgets_a_connection_even_when_its_close_raises(self):
        broken = MagicMock()
        broken.close.side_effect = RuntimeError("close failed")
        self.repo.connectionManager._local.conn = broken

        with self.assertRaisesRegex(RuntimeError, "close failed"):
            self.repo.connectionManager.close()

        self.assertIsNone(getattr(self.repo.connectionManager._local, "conn", None))


if __name__ == "__main__":
    unittest.main()
