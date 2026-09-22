# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Track repair impact contracts and bounded catalog reads."""

from unittest.mock import Mock, patch

from conftest import DatabaseTestCase, RecordingConnection, normalizeTrackForTest
from Database.db import RESTRICTED_FALLBACK_REASON, SYNTHETIC_FALLBACK_REASON
from Database.metadata_repair import TrackRepairImpact, WrappedRepairResult


def track(trackId="track-1", albumId="album-new", artistIds=("artist-new",),
          createdReason=None):
    value = normalizeTrackForTest({
        "id": trackId,
        "name": f"Song {trackId}",
        "imageId": albumId,
        "album": {
            "id": albumId,
            "name": f"Album {albumId}",
            "url": "",
            "imageId": albumId,
            "imageUrl": "",
            "totalTracks": 1,
            "releaseDate": 0.0,
        },
        "artists": [{"id": artistId, "name": f"Artist {artistId}"}
                    for artistId in artistIds],
    })
    if createdReason is not None:
        value["created_reason"] = createdReason
    return value


class TestTrackRepairImpacts(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])

    def _seedFallback(self, trackId="track-1", reason=SYNTHETIC_FALLBACK_REASON):
        self.db.repo.upsertTrack(track(trackId=trackId, albumId="album-old", artistIds=("artist-old",),
                                        createdReason=reason))
        self.db.repo.commit()

    def test_public_upsert_reports_only_fallback_to_real_membership_change(self):
        self._seedFallback()

        impact = self.db.repo.upsertTrack(
            track(albumId="album-new", artistIds=("artist-new", "artist-second")))

        assert impact == TrackRepairImpact(
            trackId="track-1",
            oldAlbumId="album-old",
            newAlbumId="album-new",
            oldArtistIds=frozenset({"artist-old"}),
            newArtistIds=frozenset({"artist-new", "artist-second"}),
        )

    def test_noop_and_rejected_inputs_return_none(self):
        assert self.db.repo.upsertTrack(
            track(albumId="album-new", artistIds=("artist-new",))) is None
        self.db.repo.commit()

        assert self.db.repo.upsertTrack(
            track(albumId="album-new", artistIds=("artist-new",))) is None
        self._seedFallback(trackId="fallback-only")
        assert self.db.repo.upsertTrack(
            track(trackId="fallback-only", albumId="album-other", artistIds=("artist-other",),
                  createdReason=SYNTHETIC_FALLBACK_REASON)) is None
        assert self.db.repo.getTrack("fallback-only")["created_reason"] == SYNTHETIC_FALLBACK_REASON
        assert self.db.repo.upsertTrack(
            track(albumId="album-new", artistIds=("artist-new",)),
            created_reason=RESTRICTED_FALLBACK_REASON) is None

    def test_empty_artists_preserve_old_membership_in_impact(self):
        self._seedFallback()
        real = track(albumId="album-new", artistIds=())

        impact = self.db.repo.upsertTrack(real)

        assert impact.oldArtistIds == frozenset({"artist-old"})
        assert impact.newArtistIds == frozenset({"artist-old"})

    def test_missing_album_uses_the_existing_default_and_reports_it(self):
        self._seedFallback()
        real = track(albumId="album-new", artistIds=("artist-new",))
        real.pop("album")
        real["albumId"] = "album-from-track"

        impact = self.db.repo.upsertTrack(real)

        assert impact.newAlbumId == "album-from-track"
        assert self.db.repo.getTrack("track-1")["album"]["id"] == "album-from-track"

    def test_missing_album_without_album_id_uses_synthetic_default(self):
        self._seedFallback()
        real = track(albumId="album-new", artistIds=("artist-new",))
        real.pop("album")

        impact = self.db.repo.upsertTrack(real)

        assert impact.newAlbumId == "album_track-1"
        assert self.db.repo.getTrack("track-1")["album"]["id"] == "album_track-1"

    def test_direct_real_upsert_reads_track_once_without_artist_lookup(self):
        statements = []
        conn = self.db.repo._conn()
        conn.execute("BEGIN IMMEDIATE")
        recording = RecordingConnection(conn, statements)
        real = track(albumId="album-new", artistIds=("artist-new",))

        with patch.object(self.db.repo, "_conn", return_value=recording):
            assert self.db.repo.upsertTrack(real) is None

        trackReads = [sql for sql, _locked in statements
                      if sql.lstrip().upper().startswith("SELECT")
                      and "FROM tracks" in sql]
        artistReads = [sql for sql, _locked in statements
                       if sql.lstrip().upper().startswith("SELECT")
                       and "track_artists" in sql]
        assert len(trackReads) == 1
        assert not artistReads
        assert next(locked for sql, locked in statements if sql == trackReads[0])
        conn.commit()

    def test_dedicated_repair_reuses_eligibility_row_and_invalidates_once(self):
        self._seedFallback()
        statements = []
        recording = RecordingConnection(self.db.repo._conn(), statements)
        invalidate = Mock(return_value=WrappedRepairResult(
            repaired=1, repairDeleted=1, historyDeleted=0,
            mode="targeted", reason=None))
        real = track(albumId="album-new", artistIds=("artist-new",))

        with patch.object(self.db.repo, "_conn", return_value=recording), \
             patch.object(self.db.repo, "_invalidateWrappedForRepairs", invalidate):
            result = self.db.repo.repairFallbackTracks([real])

        assert result == WrappedRepairResult(
            repaired=1, repairDeleted=1, historyDeleted=0,
            mode="targeted", reason=None)
        invalidate.assert_called_once()
        impacts = invalidate.call_args.args[1]
        assert impacts == [TrackRepairImpact(
            trackId="track-1", oldAlbumId="album-old", newAlbumId="album-new",
            oldArtistIds=frozenset({"artist-old"}),
            newArtistIds=frozenset({"artist-new"}),
        )]
        trackReads = [sql for sql, _locked in statements
                      if sql.lstrip().upper().startswith("SELECT")
                      and "FROM tracks" in sql]
        assert len(trackReads) == 1
        assert next(locked for sql, locked in statements if sql == trackReads[0])
        artistReads = [sql for sql, _locked in statements
                       if sql.lstrip().upper().startswith("SELECT")
                       and "track_artists" in sql]
        assert len(artistReads) == 1

    def test_dedicated_repair_deduplicates_and_rejects_unknown_or_real_rows(self):
        self._seedFallback()
        realId = "already-real"
        self.db.repo.upsertTrack(track(trackId=realId, albumId="real-album"))
        self.db.repo.commit()
        invalidate = Mock(return_value=WrappedRepairResult(
            repaired=1, repairDeleted=0, historyDeleted=0,
            mode="targeted", reason=None))
        candidate = track(albumId="album-new", artistIds=("artist-new",))

        with patch.object(self.db.repo, "_invalidateWrappedForRepairs", invalidate):
            result = self.db.repo.repairFallbackTracks([
                candidate, candidate, track(trackId="missing"),
                track(trackId=realId, albumId="ignored-album"),
            ])

        assert result.repaired == 1
        invalidate.assert_called_once()
        assert len(invalidate.call_args.args[1]) == 1

    def test_dedicated_repair_rolls_back_catalog_and_marker_when_invalidation_fails(self):
        self._seedFallback()
        before = [dict(row) for row in self.db.repo._conn().execute(
            "SELECT id, album_id, created_reason FROM tracks")]
        invalidate = Mock(side_effect=RuntimeError("invalidation failed"))

        with patch.object(self.db.repo, "_invalidateWrappedForRepairs", invalidate):
            try:
                self.db.repo.repairFallbackTracks([track(albumId="album-new")])
            except RuntimeError as error:
                assert str(error) == "invalidation failed"
            else:
                raise AssertionError("expected invalidation failure")

        after = [dict(row) for row in self.db.repo._conn().execute(
            "SELECT id, album_id, created_reason FROM tracks")]
        assert after == before
        assert not self.db.repo._conn().in_transaction

    def test_empty_or_ineligible_dedicated_repair_returns_none_without_invalidation(self):
        self._seedFallback()
        invalidate = Mock()
        with patch.object(self.db.repo, "_invalidateWrappedForRepairs", invalidate):
            assert self.db.repo.repairFallbackTracks([]) is None
            assert self.db.repo.repairFallbackTracks([
                track(createdReason=SYNTHETIC_FALLBACK_REASON),
                track(trackId="unknown"),
            ]) is None
        invalidate.assert_not_called()
