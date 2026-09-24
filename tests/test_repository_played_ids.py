"""getPlayedTrackIds/getPlayedArtistIds/getPlayedAlbumIds - the batched
"does this user have any data for these ids" lookups the Compare page uses
to decide whether a counterpart's song/artist/album links to the viewer's
own detail page or out to Spotify (see app.py's _markLinkExternally). These
must match a real play-history check, not top-list membership: a track can
be genuinely played without ranking in anyone's top-N.
"""
import sys
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository
from Database.backfill_matching import LISTENER_MAX_PLAY_SPAN_SECONDS, BackfillPage


def _track(trackId, artistIds, albumId):
    """artistIds in credited order (position 0 = primary)."""
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": aid, "name": f"Artist {aid}", "url": f"http://example.com/artist/{aid}",
             "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ],
        "album": {
            "id": albumId, "name": f"Album {albumId}", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0,
        },
        "imageUrl": "", "imageId": albumId, "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class TestPlayedIds(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

        # t1: a1 primary, album al1. t2: a2 primary, SAME album al1 (never
        # played - proves album credit comes from ANY track on it, not just
        # the specific track originally linked to that album id).
        self.repo.upsertTrack(_track("t1", ["a1"], "al1"))
        self.repo.upsertTrack(_track("t2", ["a2"], "al1"))
        # t3: a3 primary, a1 SECONDARY, album al2 - proves artist credit
        # isn't limited to the primary billing position.
        self.repo.upsertTrack(_track("t3", ["a3", "a1"], "al2"))
        # t4/al3/a4: never played anywhere - the "zero data" control.
        self.repo.upsertTrack(_track("t4", ["a4"], "al3"))
        # t5: played, but deliberately excluded from every query below - the
        # IN(...) filter must not leak plays for ids nobody asked about.
        self.repo.upsertTrack(_track("t5", ["a5"], "al4"))
        self.repo.commit()

        for trackId, playedAt in (("t1", 100), ("t3", 200), ("t5", 300)):
            self.repo.insertPlay("alice", trackId, playedAt, 60000)
        self.repo.insertPlay("bob", "t2", 400, 60000)   #< bob's plays must not count for alice
        self.repo.commit()


class TestGetPlayedTrackIds(TestPlayedIds):
    def test_returns_exactly_the_played_subset_of_the_queried_ids(self):
        result = self.repo.getPlayedTrackIds("alice", ["t1", "t2", "t3", "t4"])
        self.assertEqual(result, {"t1", "t3"})

    def test_ids_outside_the_query_list_are_never_returned(self):
        """t5 was played but isn't in the queried list - must not appear."""
        result = self.repo.getPlayedTrackIds("alice", ["t1", "t4"])
        self.assertEqual(result, {"t1"})

    def test_another_users_plays_do_not_count(self):
        result = self.repo.getPlayedTrackIds("alice", ["t2"])
        self.assertEqual(result, set())

    def test_empty_id_list_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedTrackIds("alice", []), set())

    def test_unknown_user_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedTrackIds("ghost", ["t1"]), set())


class TestGetPlayedArtistIds(TestPlayedIds):
    def test_credits_the_primary_artist_of_a_played_track(self):
        result = self.repo.getPlayedArtistIds("alice", ["a1", "a2", "a3", "a4"])
        self.assertIn("a1", result)
        self.assertIn("a3", result)

    def test_credits_a_secondary_billed_artist_too(self):
        """a1 is credited on t3 at position 1 (secondary), not just t1's
        primary billing - any credited position counts."""
        result = self.repo.getPlayedArtistIds("alice", ["a1"])
        self.assertEqual(result, {"a1"})

    def test_artist_with_no_played_track_is_excluded(self):
        result = self.repo.getPlayedArtistIds("alice", ["a2", "a4"])
        self.assertEqual(result, set())

    def test_empty_id_list_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedArtistIds("alice", []), set())


class TestGetPlayedAlbumIds(TestPlayedIds):
    def test_credits_an_album_via_any_track_on_it(self):
        """al1 holds t1 (played) and t2 (never played) - the album still
        counts as played because SOME track on it was."""
        result = self.repo.getPlayedAlbumIds("alice", ["al1", "al2", "al3"])
        self.assertEqual(result, {"al1", "al2"})

    def test_album_with_no_played_track_is_excluded(self):
        result = self.repo.getPlayedAlbumIds("alice", ["al3"])
        self.assertEqual(result, set())

    def test_empty_id_list_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedAlbumIds("alice", []), set())


class TestGetRecentlyRecordedTrackIds(unittest.TestCase):
    """Backs the listener's missed-play cross-check: unlike getPlayedTrackIds
    this one is time-bounded, because "played once last year" is no evidence
    that the play currently sitting in the connect-state queue was captured."""

    HOUR = 3600

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        for trackId in ("recent", "old", "skipped", "bobs"):
            self.repo.upsertTrack(_track(trackId, ["a1"], "al1"))
        self.repo.commit()

        now = time.time()
        self.repo.insertPlay("alice", "recent", now - self.HOUR, 60000)
        self.repo.insertPlay("alice", "old", now - 48 * self.HOUR, 60000)
        #< a skip is still a captured play - the question is "did we record it",
        #  not "did they listen through"
        self.repo.insertPlay("alice", "skipped", now - self.HOUR, 2000, is_skip=1)
        self.repo.insertPlay("bob", "bobs", now - self.HOUR, 60000)
        self.repo.commit()

    def _lookup(self, trackIds, sinceSeconds=6 * 3600):
        return self.repo.getRecentlyRecordedTrackIds("alice", trackIds, sinceSeconds)

    def test_returns_tracks_played_inside_the_window(self):
        self.assertEqual(self._lookup(["recent"]), {"recent"})

    def test_excludes_tracks_last_played_before_the_window(self):
        self.assertEqual(self._lookup(["old"]), set())

    def test_a_wider_window_reaches_the_older_play(self):
        self.assertEqual(self._lookup(["old"], sinceSeconds=72 * self.HOUR), {"old"})

    def test_skips_count_as_recorded(self):
        self.assertEqual(self._lookup(["skipped"]), {"skipped"})

    def test_another_users_plays_do_not_count(self):
        self.assertEqual(self._lookup(["bobs"]), set())

    def test_unplayed_track_is_absent(self):
        self.assertEqual(self._lookup(["never-seen"]), set())

    def test_batches_a_mixed_list_in_one_call(self):
        self.assertEqual(
            self._lookup(["recent", "old", "skipped", "bobs", "never-seen"]),
            {"recent", "skipped"},
        )

    def test_empty_id_list_returns_empty_set_without_querying(self):
        self.assertEqual(self._lookup([]), set())


class TestGetRecentlyRecordedTrackIdsAcrossMerges(unittest.TestCase):
    """The same lookup, for tracks that belong to a duplicate-merge group.

    Spotify hands out different ids for the same recording over time (market
    relinking, re-releases), so the id sitting in the connect-state queue today
    and the id last week's play was recorded under can differ. Comparing raw
    track_id then reports a play the database demonstrably has. Measured on
    live data 2026-09-12..19: 49 of 285 flagged tracks had a played group
    sibling, in BOTH directions - 30 where the flagged id was the duplicate and
    19 where it was the canonical - so a one-hop canonical_id lookup is not
    enough; group membership is the question.
    """

    HOUR = 3600

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        for trackId in ("canon", "dupe", "sibling", "lonely",
                        "stale-canon", "stale-dupe", "bobs-canon", "bobs-dupe"):
            self.repo.upsertTrack(_track(trackId, ["a1"], "al1"))
        self.repo.commit()
        self._merge("dupe", "canon")
        self._merge("sibling", "canon")
        self._merge("stale-dupe", "stale-canon")
        self._merge("bobs-dupe", "bobs-canon")

        now = time.time()
        self.repo.insertPlay("alice", "canon", now - self.HOUR, 60000)
        self.repo.insertPlay("alice", "lonely", now - self.HOUR, 60000)
        #< the group's only play is older than any window under test
        self.repo.insertPlay("alice", "stale-canon", now - 30 * 24 * self.HOUR, 60000)
        self.repo.insertPlay("bob", "bobs-canon", now - self.HOUR, 60000)
        self.repo.commit()

    def _merge(self, duplicateId, canonicalId):
        self.repo._conn().execute(
            "UPDATE tracks SET canonical_id=? WHERE id=?", (canonicalId, duplicateId))
        self.repo.commit()

    def _lookup(self, trackIds, sinceSeconds=7 * 24 * 3600):
        return self.repo.getRecentlyRecordedTrackIds("alice", trackIds, sinceSeconds)

    def test_duplicate_is_vouched_for_by_its_canonical(self):
        """Direction A: queue reports the duplicate, the play is on the canonical."""
        self.assertEqual(self._lookup(["dupe"]), {"dupe"})

    def test_canonical_is_vouched_for_by_its_duplicate(self):
        """Direction B: queue reports the canonical, the play is on a duplicate.
        Only reachable by resolving both sides to the group, not by reading the
        asked track's own canonical_id."""
        self.repo.insertPlay("alice", "dupe", time.time() - self.HOUR, 60000)
        self.repo.commit()
        self.repo._conn().execute("DELETE FROM plays WHERE track_id='canon'")
        self.repo.commit()
        self.assertEqual(self._lookup(["canon"]), {"canon"})

    def test_siblings_vouch_for_each_other(self):
        """Direction C: neither asked nor played track is the other's canonical."""
        self.repo.insertPlay("alice", "dupe", time.time() - self.HOUR, 60000)
        self.repo.commit()
        self.repo._conn().execute("DELETE FROM plays WHERE track_id='canon'")
        self.repo.commit()
        self.assertEqual(self._lookup(["sibling"]), {"sibling"})

    def test_the_asked_id_is_returned_not_the_canonical_one(self):
        """The caller matches the result against the queue's own uris, so a
        canonical id in the result set would silence nothing."""
        self.assertNotIn("canon", self._lookup(["dupe"]))

    def test_a_group_whose_only_play_is_outside_the_window_still_warns(self):
        """Merge-awareness widens WHICH plays count, never the time window."""
        self.assertEqual(self._lookup(["stale-dupe"]), set())

    def test_another_users_group_play_does_not_vouch(self):
        self.assertEqual(self._lookup(["bobs-dupe"]), set())

    def test_an_unmerged_track_is_unaffected(self):
        self.assertEqual(self._lookup(["lonely"]), {"lonely"})

    def test_a_merged_track_with_no_plays_anywhere_is_absent(self):
        self.repo._conn().execute("DELETE FROM plays WHERE track_id='canon'")
        self.repo.commit()
        self.assertEqual(self._lookup(["dupe", "canon", "sibling"]), set())

    def test_a_mixed_batch_answers_merged_and_unmerged_together(self):
        self.assertEqual(
            self._lookup(["dupe", "lonely", "stale-dupe", "bobs-dupe", "never-seen"]),
            {"dupe", "lonely"},
        )


class TestGetPlayTimesInRange(unittest.TestCase):
    """Backs the Web API backfill's dedup: the listener's in-memory caches only
    cover the current listener object's lifetime, so after a reconnect the
    database is the only thing that knows which of the last 50 Web API plays
    were already recorded."""

    HOUR = 3600

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        for trackId in ("t1", "t2", "t3", "t4"):
            self.repo.upsertTrack(_track(trackId, ["a1"], "al1"))
        self.repo.commit()

        self.base = 1_700_000_000.0
        self.repo.insertPlay("alice", "t1", self.base, 60000)
        self.repo.insertPlay("alice", "t2", self.base + 600, 2000, is_skip=1)  #< a skip is still recorded
        self.repo.insertPlay("alice", "t3", self.base + 10 * self.HOUR, 60000)  #< outside the window
        self.repo.insertPlay("bob", "t4", self.base + 60, 60000)
        self.repo.commit()

    def _lookup(self, startTs, endTs):
        return sorted(playedAt for _trackId, playedAt, _createdAt in self._lookupPairs(startTs, endTs))

    def _lookupPairs(self, startTs, endTs):
        return [
            (row["trackId"], row["playedAt"], row["listenerCreatedAt"])
            for row in self.repo.getTrackPlayTimesInRange("alice", startTs, endTs)
        ]

    def _insertPlayCreatedAt(self, trackId, playedAt, createdAt, created_reason, is_skip=0):
        """A play whose insert-time stamp is controlled: created_at is stamped
        with time.time() inside insertPlay, so pin the clock for the call."""
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = createdAt
            self.repo.insertPlay("alice", trackId, playedAt, 60000,
                                 created_reason=created_reason, is_skip=is_skip)
        self.repo.commit()

    def test_each_time_carries_the_track_it_belongs_to(self):
        """The backfill's dedup compares per track: a timestamp on its own let a
        recorded play of one track suppress a genuinely missing play of another
        (their played_at values sit seconds apart under gapless playback)."""
        self.assertEqual(sorted(self._lookupPairs(self.base - 10, self.base + 1200)),
                         [("t1", self.base, None), ("t2", self.base + 600, None)])

    def test_listener_rows_carry_their_created_at_as_the_observed_end(self):
        """The listener inserts a play at the track-change moment, so a listener
        row's created_at IS the observed end of the play, pauses included - the
        anchor the backfill's end-time dedup arm compares against."""
        self._insertPlayCreatedAt("t4", self.base + 300, self.base + 700,
                                  "listener_play (user: alice)")

        self.assertIn(("t4", self.base + 300, self.base + 700),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_non_listener_rows_never_carry_a_created_at(self):
        """An import row's created_at is the moment the import ran - unrelated
        to when the play ended - and a backfill row's is the poll moment. Only
        a listener row's insert time means "this play just finished"."""
        self._insertPlayCreatedAt("t4", self.base + 300, self.base + 700,
                                  "history_import (user: alice)")

        self.assertIn(("t4", self.base + 300, None),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_a_listener_row_starting_before_the_window_is_found_via_its_end(self):
        """A paused play's start can sit MORE than one track-length before its
        end - the exact case the end-time arm exists for - so the row must be
        findable by when it ended, not only by when it started."""
        self._insertPlayCreatedAt("t4", self.base - 5000, self.base + 100,
                                  "listener_play (user: alice)")

        self.assertIn(("t4", self.base - 5000, self.base + 100),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_a_listener_skip_row_never_anchors_an_end(self):
        """A skip's created_at is when the user skipped AWAY, not the end of a
        play the feed still owes us. Letting it anchor the caller's +/-10s
        end-time arm meant a skip could suppress the backfill of a real play -
        the user skips a track, the listener then dies and misses the full
        replay, and the Web API's start-reading of that replay lands within
        10s of the skip's stamp. Suppressing here is unrecoverable - the item
        is dropped before it ever reaches the insert guard, and every later
        poll collides identically: the play is lost for good.

        This nulls the ANCHOR only. The skip's played_at still comes through
        for the caller's two played_at arms, and the insert guard behind this
         check matches it directly on a tight tolerance (the backfill guard's
        skipToleranceSeconds) - which is where a backfill row re-recording a
        skipped listen gets dropped, with per-row knowledge this layer has
        not got."""
        self._insertPlayCreatedAt("t4", self.base + 300, self.base + 700,
                                  "listener_play (user: alice)", is_skip=1)

        self.assertIn(("t4", self.base + 300, None),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_a_skip_is_not_reached_into_the_window_by_its_created_at(self):
        """The reach-back arm exists to find a play whose END landed in the
        window; a skip row has no such end, so its stamp must not pull it in
        either. Its own played_at still counts wherever that falls."""
        self._insertPlayCreatedAt("t4", self.base - 5000, self.base + 100,
                                  "listener_play (user: alice)", is_skip=1)

        self.assertEqual([], [row for row in self._lookupPairs(self.base - 10, self.base + 1200)
                              if row[0] == "t4"])

    def test_returns_played_at_values_inside_the_window(self):
        self.assertEqual(self._lookup(self.base - 10, self.base + 1200), [self.base, self.base + 600])

    def test_bounds_are_inclusive(self):
        """The caller pads the window by exactly the dedup tolerance, so an
        exclusive bound would drop the play sitting on the edge."""
        self.assertEqual(self._lookup(self.base, self.base), [self.base])

    def test_skips_count_as_recorded(self):
        self.assertEqual(self._lookup(self.base + 600, self.base + 600), [self.base + 600])

    def test_plays_outside_the_window_are_excluded(self):
        self.assertEqual(self._lookup(self.base - 10, self.base + 60), [self.base])

    def test_another_users_plays_do_not_count(self):
        """bob's play sits inside the window - scoping it to alice is what keeps
        one account's history from suppressing another's backfill."""
        self.assertNotIn(self.base + 60, self._lookup(self.base - 10, self.base + 1200))

    def test_empty_window_returns_nothing(self):
        self.assertEqual(self._lookup(self.base + 2 * self.HOUR, self.base + 3 * self.HOUR), [])


class TestPlayTimesInRangeAliases(unittest.TestCase):
    """The same recording can reach us under two different Spotify track ids -
    the connect player_state and the Web API's recently-played endpoint pick
    different releases of it. The caller's dedup is keyed by track id, so the
    listener recording a play under one id left the backfill's copy of the SAME
    listen unmatched, and it was inserted a second time (measured on the live
    instance 2026-08-17: 4 of the last 307 plays, every pair one track-length
    apart with identical duration_ms and, where known, identical ISRC).

    So a recorded play answers for every id that denotes its recording, not
    only the id it was stored under. The three ways two ids can be known to be
    the same recording, tightest first - over-matching here SUPPRESSES a
    genuinely missing play, which is unrecoverable (the item is dropped before
    it reaches the insert guard, and every later poll collides identically)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.commit()
        self.base = 1_700_000_000.0

    DURATION_MS = 267534

    def _upsert(self, trackId, *, name="Same Song", durationMs=DURATION_MS, isrc="",
                artistIds=("a1",), albumId="al1"):
        track = _track(trackId, list(artistIds), albumId)
        track["name"] = name
        track["duration"] = durationMs
        track["isrc"] = isrc
        self.repo.upsertTrack(track)
        self.repo.commit()

    def _idsFor(self, playedTrackId):
        """Which track ids the recorded play of `playedTrackId` now answers for."""
        self.repo.insertPlay("alice", playedTrackId, self.base, 60000)
        self.repo.commit()
        return {
            alias
            for row in self.repo.getTrackPlayTimesInRange("alice", self.base - 10, self.base + 10)
            if row["playedAt"] == self.base
            for alias in row["aliases"]
        }

    def test_a_merged_sibling_is_the_same_recording(self):
        """A merge is a decided fact - the strongest signal there is. This is
        the shape 3 of the 4 live duplicates had by the time they were found:
        the ISRC matcher had already merged the phantom id."""
        self._upsert("canon")
        self._upsert("variant", name="Same Song (Radio Edit)")
        self.repo.mergeTrackManually("variant", "canon", "tester")
        self.repo.commit()

        self.assertEqual(self._idsFor("canon"), {"canon", "variant"})

    def test_a_shared_isrc_is_the_same_recording(self):
        """One master, two releases - exactly what the automatic merge tier
        merges on, so the dedup may lean on it before that tier has run."""
        self._upsert("album_release", name="Album Title", isrc="DEU601606324")
        self._upsert("single_release", name="Single Title", isrc="DEU601606324")

        self.assertEqual(self._idsFor("album_release"), {"album_release", "single_release"})

    def test_the_same_title_duration_and_artist_is_the_same_recording(self):
        """The live 2026-08-17 case, and the one that matters most: the id the
        listener invented is brand new, so its ISRC has not been fetched yet
        (tracks.isrc = '') and no merge tier can have seen it. Identical title,
        identical primary artist and a duration equal to the millisecond is
        what is left, and it is what the merge review queue itself asks a
        person about."""
        self._upsert("listener_id", isrc="")
        self._upsert("web_api_id", isrc="DEU601606324")

        self.assertEqual(self._idsFor("listener_id"), {"listener_id", "web_api_id"})

    def test_an_empty_isrc_matches_nothing(self):
        """Most rows have isrc = '' (it is only filled in when the catalog
        lookup lands). Treating that as a shared ISRC would alias the entire
        library to itself and suppress every backfill."""
        self._upsert("one", name="One", durationMs=111_000, isrc="")
        self._upsert("two", name="Two", durationMs=222_000, isrc="")

        self.assertEqual(self._idsFor("one"), {"one"})

    def test_a_different_duration_is_a_different_recording(self):
        """Durations agree to the millisecond or not at all. A re-recording or
        a remaster is a genuinely different play to log, and the merge review
        queue - which tolerates a few seconds - exists to ask a person about
        those rather than have this layer guess."""
        self._upsert("original")
        self._upsert("remaster", durationMs=self.DURATION_MS + 1)

        self.assertEqual(self._idsFor("original"), {"original"})

    def test_a_different_title_is_a_different_recording(self):
        self._upsert("original")
        self._upsert("other", name="Different Song")

        self.assertEqual(self._idsFor("original"), {"original"})

    def test_a_different_primary_artist_is_a_different_recording(self):
        """A cover keeping the original's exact length is unlikely, but a play
        wrongly suppressed here is gone for good - so the credited artist has
        to agree too."""
        self._upsert("original", artistIds=("a1",))
        self._upsert("cover", artistIds=("a2",))

        self.assertEqual(self._idsFor("original"), {"original"})

    def test_an_untitled_artistless_track_matches_only_itself(self):
        """A fallback row can carry no artists at all; two of them agreeing on
        "no primary artist" is not evidence of anything."""
        self._upsert("fallback_one", artistIds=())
        self._upsert("fallback_two", artistIds=())

        self.assertEqual(self._idsFor("fallback_one"), {"fallback_one"})

    def test_an_alias_carries_the_recorded_rows_end_anchor(self):
        """The end-time arm compares against a listener row's created_at. An
        alias that dropped it would leave the pause-stretched case (the arm's
        whole reason for existing) broken for exactly the cross-id plays this
        aliasing is here to catch."""
        self._upsert("listener_id")
        self._upsert("web_api_id", isrc="DEU601606324")
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = self.base + 700
            self.repo.insertPlay("alice", "listener_id", self.base, 60000,
                                 created_reason="listener_play (user: alice)")
        self.repo.commit()

        rows = self.repo.getTrackPlayTimesInRange("alice", self.base - 10, self.base + 10)
        self.assertTrue(any(row["trackId"] == "listener_id"
                            and "web_api_id" in row["aliases"]
                            and row["listenerCreatedAt"] == self.base + 700
                            for row in rows))

    def test_a_recorded_play_is_reported_once_under_its_own_id(self):
        """Aliasing adds ids; it must not duplicate the row it started from."""
        self._upsert("listener_id")
        self._upsert("web_api_id", isrc="DEU601606324")
        self.repo.insertPlay("alice", "listener_id", self.base, 60000)
        self.repo.commit()

        rows = self.repo.getTrackPlayTimesInRange("alice", self.base - 10, self.base + 10)
        self.assertEqual(len(rows), len({row["rowId"] for row in rows}))
        self.assertEqual(len([row for row in rows if row["trackId"] == "listener_id"]), 1)

    def test_candidate_bound_alias_query_does_not_return_unrequested_tracks(self):
        self._upsert("one", isrc="SHARED-ISRC")
        self._upsert("two", isrc="SHARED-ISRC")

        aliases = self.repo._sameRecordingTrackIds({"one"}, candidateIds={"one"})

        self.assertEqual(aliases.get("one", set()), set())

    def test_pending_raw_identity_adds_alias_without_inventing_merge_group(self):
        self._upsert("catalogued", isrc="SHARED-ISRC")
        page = BackfillPage([{
            "track": {
                "id": "future",
                "name": "Track catalogued",
                "duration_ms": self.DURATION_MS,
                "external_ids": {"isrc": "SHARED-ISRC"},
                "artists": [{"id": "a1"}],
            },
            "played_at": self.base,
        }])
        self.repo.insertPlay("alice", "catalogued", self.base, 60000)
        self.repo.commit()

        rows = self.repo.getTrackPlayTimesInRange(
            "alice", self.base - 10, self.base + 10, page=page
        )

        self.assertEqual(len(rows), 1)
        self.assertIn("future", rows[0]["aliases"])

    def test_bounded_page_retains_merge_only_alias_in_both_directions(self):
        self._upsert("canonical", name="Original", isrc="CANONICAL-ISRC")
        self._upsert("variant", name="Different title", isrc="VARIANT-ISRC",
                     durationMs=self.DURATION_MS + 1)
        self.repo.mergeTrackManually("variant", "canonical", "tester")
        self.repo.insertPlay("alice", "canonical", self.base, 60000)
        self.repo.commit()
        page = BackfillPage([{"track": {"id": "variant"}, "played_at": self.base}])
        rows = self.repo.getTrackPlayTimesInRange("alice", self.base - 10, self.base + 10, page=page)
        self.assertEqual(len(rows), 1)
        self.assertIn("variant", rows[0]["aliases"])
        self.assertIsNotNone(self.repo.findMatchingBackfillPlay("alice", "variant", self.base,
                                                               100, page=page))

    def test_alias_proofs_do_not_form_a_transitive_chain(self):
        self._upsert("first", name="First", isrc="ONE")
        self._upsert("bridge", name="Bridge", isrc="ONE")
        self._upsert("last", name="Last", isrc="TWO")
        self.repo.mergeTrackManually("last", "bridge", "tester")
        aliases = self.repo._sameRecordingTrackIds({"first", "bridge", "last"})
        self.assertIn("bridge", aliases["first"])
        self.assertNotIn("last", aliases["first"])

    def test_insert_guard_work_is_bounded_by_time_window_not_full_user_history(self):
        history_count = 10_000
        progress_step = 100
        max_progress_callbacks = 20
        #< before the listener-end lookup's span bound: inside it, the guard
        #  legitimately reads rows (at most LISTENER_MAX_PLAY_SPAN_SECONDS of them)
        history_start = self.base - LISTENER_MAX_PLAY_SPAN_SECONDS - history_count * 2
        self._upsert("track")
        conn = self.repo.connection()
        conn.executemany(
            "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
            (("alice", "track", history_start + offset, 60000) for offset in range(history_count)))
        self.repo.commit()
        callbacks = 0

        def count_steps():
            nonlocal callbacks
            callbacks += 1
            return 0

        page = BackfillPage([{"track": {"id": "track"}, "played_at": self.base}])
        conn.set_progress_handler(count_steps, progress_step)
        try:
            result = self.repo.findMatchingBackfillPlay("alice", "track", self.base, 100, page=page)
        finally:
            conn.set_progress_handler(None, 0)
        self.assertIsNone(result)
        self.assertLess(callbacks, max_progress_callbacks,
                        "the per-insert guard scanned history outside its narrow window")

if __name__ == "__main__":
    unittest.main()
