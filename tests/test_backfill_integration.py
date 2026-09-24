# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQLite contracts for Listener -> Database backfill and reconciliation.

Listener tests never inspect database rows; physical row/source assertions
belong to the database integration helpers below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from Database.Formatters.spotifyClient import Client
from Database.Listeners.spotifyListener import Listener
from Database.database import Database
from conftest import DatabaseTestCase


BASE_TS = 1_700_000_000
TRACK_DURATION_MS = 180_000
ISRC = "US-C5B-00001"


def _played_at(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _api_track(track_id: str, *, isrc: str = ISRC) -> dict:
    """Return the real Spotify/Spotipy track shape consumed by Client.formatTrack.

    Merge identity is established separately in the DB seed helper; the API
    payload carries only the real Spotify fields.
    """
    return {
        "id": track_id,
        "name": "C5b song",
        "duration_ms": TRACK_DURATION_MS,
        "explicit": False,
        "disc_number": 1,
        "track_number": 1,
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
        "external_ids": {"isrc": isrc},
        "artists": [{
            "id": "artist-c5b",
            "name": "C5b artist",
            "external_urls": {"spotify": "https://open.spotify.com/artist/artist-c5b"},
        }],
        "album": {
            "id": "album-c5b",
            "name": "C5b album",
            "total_tracks": 1,
            "release_date": "2023-01-01",
            "images": [],
            "external_urls": {"spotify": "https://open.spotify.com/album/album-c5b"},
        },
    }


def _api_item(track_id: str, timestamp: int, *, isrc: str = ISRC) -> dict:
    return {"track": _api_track(track_id, isrc=isrc), "played_at": _played_at(timestamp)}


def _rows(db: Database) -> list[dict]:
    """Expose physical/source rows only from DB integration assertions."""
    result = db.repo.connection().execute(
        """
        SELECT id, track_id, played_at, time_played, created_at, created_reason, is_skip
        FROM plays
        WHERE username=?
        ORDER BY played_at, id
        """,
        (db.user,),
    ).fetchall()
    return [dict(row) for row in result]


def _track_ids(db: Database) -> set[str]:
    return {
        row["id"]
        for row in db.repo.connection().execute("SELECT id FROM tracks").fetchall()
    }


def _seed_catalog_identity(db: Database, track_id: str, *, canonical_id: str | None = None,
                           isrc: str = ISRC) -> None:
    track = Client.formatTrack(_api_track(track_id, isrc=isrc), embedPlaybackInfo=False)
    db.repo.upsertTrack(track)
    if canonical_id is not None:
        db.repo.connection().execute(
            "UPDATE tracks SET canonical_id=? WHERE id=?", (canonical_id, track_id)
        )
    db.repo.commit()


def _seed_listener_play(db: Database, track_id: str, timestamp: int, created_at: int,
                        *, canonical_id: str | None = None, isrc: str = ISRC) -> None:
    _seed_catalog_identity(db, track_id, canonical_id=canonical_id, isrc=isrc)
    db.repo.insertPlay(
        db.user,
        track_id,
        timestamp,
        TRACK_DURATION_MS,
        created_reason=f"listener_play (user: {db.user})",
    )
    row = db.repo.connection().execute(
        "SELECT id FROM plays WHERE username=? AND track_id=? AND played_at=?",
        (db.user, track_id, timestamp),
    ).fetchone()
    db.repo.connection().execute("UPDATE plays SET created_at=? WHERE id=?", (created_at, row["id"]))
    db.repo.commit()


def _run_listener_page(db: Database, items: list[dict]) -> None:
    """Run the actual Web API poll into the public DB page callback."""
    get_credentials = MagicMock(return_value={
        "client_id": "c5b-client",
        "client_secret": "c5b-secret",
        "refresh_token": "c5b-refresh",
    })
    with patch("Database.Listeners.spotifyListener.Spotify") as spotify_cls:
        spotify_cls.return_value.current_user_recently_played.return_value = []
        spotify_cls.return_value.current_user.return_value = {
            "id": "alice", "display_name": "Alice", "email": "alice@example.com",
        }
        listener = Listener(
            "c5b-cookie",
            email="alice@example.com",
            get_credentials=get_credentials,
            process_backfill_page=db.process_backfill_page,
        )
    listener._lastWebApiPollTime = 0
    try:
        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="c5b-token"), \
                patch("Database.Listeners.spotifyListener._get_current_user_from_web_api", return_value={
                    "id": "alice", "display_name": "Alice", "email": "alice@example.com",
                }), \
                patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=items), \
                patch("Database.Listeners.spotifyListener.time.monotonic", return_value=99_000), \
                patch("Database.Listeners.spotifyListener.time.sleep", return_value=None):
            listener._checkWebApiBackfill(
                db._addToDatabaseFromListener,
                onWebApiSnapshot=db._reconcileWithWebApiHistory,
            )
    finally:
        with patch("Database.Listeners.spotifyListener.time.sleep", return_value=None):
            listener.stop()


class TestWebApiBackfillSQLiteContract(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [], username="alice")
        # The repository path remains real; image and playlist side effects are
        # intentionally disabled because this contract is about play/catalog
        # persistence and reconciliation.
        self.db.saveImagesFromTrack = MagicMock()
        self.db.updatePlaylists = MagicMock()

    def test_three_distinct_api_repeats_survive_page_replay_reverse_and_restart(self):
        items = [_api_item("repeat", BASE_TS + offset) for offset in (0, 180, 360)]
        items.append(_api_item("repeat", BASE_TS + 360))

        _run_listener_page(self.db, items)
        first = _rows(self.db)
        self.assertEqual(len(first), 3)
        self.assertEqual(sum(row["time_played"] for row in first), 3 * TRACK_DURATION_MS)

        _run_listener_page(self.db, list(reversed(items)))
        _run_listener_page(self.db, items)
        self.assertEqual([(row["track_id"], row["played_at"]) for row in _rows(self.db)],
                         [("repeat", ts) for ts in (BASE_TS, BASE_TS + 180, BASE_TS + 360)])

    def test_listener_and_api_same_recording_leave_two_total_plays(self):
        _seed_catalog_identity(self.db, "canonical-c5b", isrc=ISRC)
        _seed_listener_play(self.db, "listener-release", BASE_TS + 180, BASE_TS + 360,
                            canonical_id="canonical-c5b")
        items = [
            _api_item("api-release", BASE_TS + 180),
            _api_item("api-release", BASE_TS + 360),
        ]
        _seed_catalog_identity(self.db, "api-release", canonical_id="canonical-c5b")

        _run_listener_page(self.db, items)
        rows = _rows(self.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["time_played"] for row in rows), 2 * TRACK_DURATION_MS)
        self.assertEqual([row["created_reason"].split("_play", 1)[0] for row in rows],
                         ["listener", "web_api_backfill"])

        _run_listener_page(self.db, list(reversed(items)))
        self.assertEqual(len(_rows(self.db)), 2)

    def test_exact_reservation_beats_ambiguous_match_in_either_page_order(self):
        def run_case(items):
            db = self._makeDb({}, [], username="alice")
            db.saveImagesFromTrack = MagicMock()
            db.updatePlaylists = MagicMock()
            _seed_listener_play(db, "known-id", BASE_TS + 2, BASE_TS + 100, isrc=ISRC)
            _run_listener_page(db, items)
            return db

        page = [
            _api_item("known-id", BASE_TS, isrc=ISRC),
            _api_item("not-yet-catalogued", BASE_TS + 2, isrc=ISRC),
        ]
        for ordered_page in (page, list(reversed(page))):
            db = run_case(ordered_page)
            rows = _rows(db)
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["played_at"] for row in rows}, {BASE_TS, BASE_TS + 2})

    def test_insert_failure_after_catalog_write_rolls_back_and_retries(self):
        original_insert = self.db.repo.insertPlay
        failed = False

        def fail_after_catalog_write(username, track_id, *args, **kwargs):
            nonlocal failed
            if track_id == "retry-me" and not failed:
                failed = True
                raise RuntimeError("synthetic insert failure")
            return original_insert(username, track_id, *args, **kwargs)

        items = [_api_item("retry-me", BASE_TS), _api_item("already-ok", BASE_TS + 180)]
        with patch.object(self.db.repo, "insertPlay", side_effect=fail_after_catalog_write):
            _run_listener_page(self.db, items)
        self.assertNotIn("retry-me", _track_ids(self.db))
        self.assertEqual([row["track_id"] for row in _rows(self.db)], ["already-ok"])

        _run_listener_page(self.db, items)
        self.assertEqual({row["track_id"] for row in _rows(self.db)}, {"retry-me", "already-ok"})

    def test_consumed_alias_match_does_not_swallow_later_genuine_repeat(self):
        _seed_listener_play(self.db, "physical-row", BASE_TS, BASE_TS + 1, isrc=ISRC)
        items = [
            _api_item("alias-first", BASE_TS, isrc=ISRC),
            _api_item("alias-later", BASE_TS + 10, isrc=ISRC),
        ]

        _run_listener_page(self.db, items)
        rows = _rows(self.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["track_id"] for row in rows], ["physical-row", "alias-later"])

    def test_same_api_timestamp_under_an_isrc_alias_deduplicates(self):
        _run_listener_page(self.db, [
            _api_item("alias-a", BASE_TS, isrc=ISRC),
            _api_item("alias-b", BASE_TS, isrc=ISRC),
        ])
        self.assertEqual(len(_rows(self.db)), 1)

    def test_distinct_api_timestamps_survive_reconciliation_even_with_shared_isrc(self):
        _run_listener_page(self.db, [
            _api_item("api-a", BASE_TS, isrc=ISRC),
            _api_item("api-b", BASE_TS + 1, isrc=ISRC),
        ])
        self.assertEqual(len(_rows(self.db)), 2)

    def test_failed_item_is_reoffered_on_next_page_without_duplicate_success(self):
        original_append = self.db.appendTrackData
        failed = False

        def fail_once(timestamp, track, time_played, **kwargs):
            nonlocal failed
            if track["id"] == "retry-me" and not failed:
                failed = True
                raise RuntimeError("synthetic per-item write failure")
            return original_append(timestamp, track, time_played, **kwargs)

        items = [_api_item("retry-me", BASE_TS), _api_item("already-ok", BASE_TS + 180)]
        with patch.object(self.db, "appendTrackData", side_effect=fail_once):
            _run_listener_page(self.db, items)
        self.assertEqual([row["track_id"] for row in _rows(self.db)], ["already-ok"])

        _run_listener_page(self.db, items)
        self.assertEqual([row["track_id"] for row in _rows(self.db)], ["retry-me", "already-ok"])

    def test_listener_end_time_reading_is_one_play_and_replay_is_idempotent(self):
        """#38 kept this as a possible repeat (2 rows). Live 2026-09-23: 84 such
        copies in 11h, and the listener had seen the same track start at the
        API time in none of them - it is the same listen, by end time."""
        _seed_listener_play(self.db, "listener-boundary", BASE_TS + 180, BASE_TS + 360)
        item = _api_item("api-boundary", BASE_TS + 360)

        _run_listener_page(self.db, [item])
        self.assertEqual(len(_rows(self.db)), 1)
        _run_listener_page(self.db, [item])
        self.assertEqual(len(_rows(self.db)), 1)

    def test_back_to_back_repeat_by_end_time_survives_page_replay_and_reconciliation(self):
        """F1 end to end: the listener saw the first of two consecutive plays.
        The API stamps sit at that row's start and at its end - each alone
        matches the row, so only the claim keeps the second play."""
        skewSeconds = 1  #< off by a clock second: an exact stamp is reserved by its own rule
        _seed_listener_play(self.db, "track", BASE_TS, BASE_TS + 180)
        items = [_api_item("track", BASE_TS + 180 + skewSeconds), _api_item("track", BASE_TS + skewSeconds)]

        for _ in range(2):
            _run_listener_page(self.db, items)
            self.assertEqual([row["played_at"] for row in _rows(self.db)], [BASE_TS, BASE_TS + 180 + skewSeconds])
        _run_listener_page(self.db, list(reversed(items)))
        self.assertEqual(len(_rows(self.db)), 2)

    def test_listener_start_a_few_seconds_off_is_neither_inserted_nor_churned(self):
        """Live 2026-09-23: a listener start 3.39s from the API stamp was
        inserted by the 2s prefilter and deleted by 5s reconciliation on
        every poll, wiping the user's Wrapped cache each time."""
        offsetSeconds = 3
        _seed_listener_play(self.db, "track", BASE_TS, BASE_TS + 180)
        item = _api_item("track", BASE_TS + offsetSeconds)

        with patch.object(self.db.repo, "_deleteUserWrappedFromYear") as wrappedDrop:
            for _ in range(2):
                _run_listener_page(self.db, [item])
                self.assertEqual([row["played_at"] for row in _rows(self.db)], [BASE_TS])
        wrappedDrop.assert_not_called()

    def test_failed_initial_query_reoffers_through_atomic_guard_without_losing_repeat(self):
        _seed_listener_play(self.db, "physical", BASE_TS + 2, BASE_TS + 100)
        items = [_api_item("physical", BASE_TS), _api_item("uncatalogued-alias", BASE_TS + 2)]
        with patch.object(self.db.repo, "getTrackPlayTimesInRange",
                          side_effect=RuntimeError("synthetic initial query failure")):
            _run_listener_page(self.db, items)
        self.assertEqual([row["played_at"] for row in _rows(self.db)], [BASE_TS, BASE_TS + 2])
        _run_listener_page(self.db, items)
        self.assertEqual(len(_rows(self.db)), 2)

    def test_page_claims_and_sql_evidence_do_not_cross_users(self):
        self.db.repo.upsertUser("bob", "bob@example.com")
        self.db.repo.commit()
        items = [_api_item("shared-track", BASE_TS), _api_item("shared-track", BASE_TS + 1)]
        self.db.process_backfill_page(items)
        with patch.object(self.db, "user", "bob"):
            self.db.process_backfill_page(items)
            self.assertEqual(len(_rows(self.db)), 2)
        self.db.process_backfill_page(list(reversed(items)))
        self.assertEqual(len(_rows(self.db)), 2)
        self.assertEqual(self.db.repo.connection().execute("SELECT COUNT(*) FROM plays").fetchone()[0], 4)

    def test_failed_item_degrades_health_once_and_successful_retry_recovers(self):
        items = [_api_item("failed", BASE_TS), _api_item("success", BASE_TS + 180)]
        original = self.db.appendTrackData

        def fail_first(timestamp, track, *args, **kwargs):
            if track["id"] == "failed":
                raise RuntimeError("synthetic failure")
            return original(timestamp, track, *args, **kwargs)

        with patch.object(self.db, "appendTrackData", side_effect=fail_first):
            self.db.process_backfill_page(items)
        self.assertEqual(self.db.listener_error_count, 1)
        self.assertIsNotNone(self.db.listener_last_error)
        self.assertEqual([row["track_id"] for row in _rows(self.db)], ["success"])
        self.db.process_backfill_page(items)
        self.assertEqual(self.db.listener_error_count, 0)
        self.assertIsNone(self.db.listener_last_error)
        self.assertEqual(self.db.listener_health, "HEALTHY")
        self.assertEqual(len(_rows(self.db)), 2)

    def test_uncatalogued_exact_alias_reserves_primary_during_cleanup(self):
        _seed_listener_play(self.db, "primary", BASE_TS, BASE_TS + 100)
        _seed_catalog_identity(self.db, "persisted-api")
        self.db.repo.insertPlay(self.db.user, "persisted-api", BASE_TS + 1, TRACK_DURATION_MS,
                                created_reason="web_api_backfill_play (user: alice)")
        self.db.repo.commit()
        # This API alias is confirmed by the page prefilter, so no catalog
        # upsert occurs. Cleanup must still reserve its exact primary event.
        _run_listener_page(self.db, [_api_item("uncatalogued", BASE_TS)])
        self.assertEqual([row["played_at"] for row in _rows(self.db)], [BASE_TS, BASE_TS + 1])
        self.assertNotIn("uncatalogued", _track_ids(self.db))

    def test_two_ambiguous_starts_cannot_reuse_one_primary_through_the_guard(self):
        _seed_listener_play(self.db, "physical", BASE_TS, BASE_TS + 180)
        items = [_api_item("alias-first", BASE_TS + 1), _api_item("alias-later", BASE_TS + 2)]
        for ordered in (list(reversed(items)), items):
            _run_listener_page(self.db, ordered)
            self.assertEqual([row["played_at"] for row in _rows(self.db)], [BASE_TS, BASE_TS + 2])

    def test_cleanup_does_not_delete_an_api_event_absent_from_the_current_page(self):
        _seed_listener_play(self.db, "physical", BASE_TS, BASE_TS + 180)
        self.db.repo.insertPlay(self.db.user, "physical", BASE_TS + 1, TRACK_DURATION_MS,
                                created_reason="web_api_backfill_play (user: alice)")
        self.db.repo.commit()
        self.db._reconcileWithWebApiHistory([_api_item("unrelated", BASE_TS, isrc="DIFFERENT")])
        self.assertEqual([row["played_at"] for row in _rows(self.db)], [BASE_TS, BASE_TS + 1])


class TestBackfillPageWindowReachesEveryMatchArm(DatabaseTestCase):
    """The prefilter can only suppress a row its range query returns. With
    the window padded by the 2s start tolerance alone, a listener row the
    matcher accepts at 3-5s (start) or up to 10s (observed end) past the
    NEWEST stamp was never read (Copilot on #40). The insert guard's wider
    lookup still caught it, but only after a reoffer and a catalog write."""

    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [], username="alice")
        self.db.saveImagesFromTrack = MagicMock()
        self.db.updatePlaylists = MagicMock()
        self.db._addToDatabaseFromListener = MagicMock()

    def test_listener_start_just_after_the_newest_stamp_is_suppressed_by_the_prefilter(self):
        startLagSeconds = 4  #< inside the 5s listener-start tolerance, outside the old 2s padding
        _seed_listener_play(self.db, "track", BASE_TS + startLagSeconds, BASE_TS + startLagSeconds + 180)

        self.db.process_backfill_page([_api_item("track", BASE_TS)])

        self.db._addToDatabaseFromListener.assert_not_called()

    def test_listener_end_just_after_the_newest_stamp_is_suppressed_by_the_prefilter(self):
        pauseSeconds = 186
        endLagSeconds = 8  #< inside the 10s observed-end tolerance, outside the old 2s padding
        _seed_listener_play(self.db, "track", BASE_TS - 180 - pauseSeconds, BASE_TS + endLagSeconds)

        self.db.process_backfill_page([_api_item("track", BASE_TS)])

        self.db._addToDatabaseFromListener.assert_not_called()


class TestInsertGuardAfterAFailedPageLookup(DatabaseTestCase):
    """The insert guard reads evidence by played_at only (fast, indexed), so a
    long-paused listener play - start more than duration + 60s before the
    API's end stamp - is invisible to it. That is fine while the page
    prefilter, which also reads listener ends, has run; after a failed
    initial lookup the guard is the only check (Copilot on #40)."""

    PAUSE_SECONDS = 186  #< well past the guard's 60s margin

    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [], username="alice")
        self.db.saveImagesFromTrack = MagicMock()
        self.db.updatePlaylists = MagicMock()

    def test_a_long_paused_listener_play_is_not_duplicated_when_the_page_lookup_fails(self):
        _seed_listener_play(self.db, "track", BASE_TS - 180 - self.PAUSE_SECONDS, BASE_TS + 1)

        with patch.object(self.db.repo, "getTrackPlayTimesInRange",
                          side_effect=RuntimeError("synthetic initial query failure")):
            self.db.process_backfill_page([_api_item("track", BASE_TS)])

        self.assertEqual(len(_rows(self.db)), 1)

    def test_the_guard_stays_on_the_indexed_range_when_the_page_lookup_succeeded(self):
        """The created_at arm scans the user's whole history (measured ~25ms
        per insert on a 100k-play user, under the write reservation)."""
        guardLookups = []
        original = self.db.repo._getTrackPlayEvidence

        def spy(*args, includeListenerEnds=False, **kwargs):
            guardLookups.append(includeListenerEnds)
            return original(*args, includeListenerEnds=includeListenerEnds, **kwargs)

        with patch.object(self.db.repo, "_getTrackPlayEvidence", side_effect=spy):
            self.db.process_backfill_page([_api_item("track", BASE_TS)])

        self.assertEqual(len(_rows(self.db)), 1)
        self.assertEqual(guardLookups, [True, False])   #< page prefilter, then the insert guard
