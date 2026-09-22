# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import datetime
import unittest
from unittest.mock import MagicMock, patch

from conftest import DatabaseTestCase
from Database.backfill_matching import backfill_page_window, WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
from Database.Listeners.spotifyListener import (
    Listener,
    WEB_API_POLL_INTERVAL_SECONDS,
)
from Database.utils import timeToInt


USER = "alice"
TRACK_DURATION_MS = 180_000
TRACK_DURATION_SECONDS = TRACK_DURATION_MS // 1000
FIRST_PLAYED_AT = "2026-07-26T14:20:00Z"
SECOND_PLAYED_AT = "2026-07-26T14:22:00Z"
MONOTONIC_NOW = WEB_API_POLL_INTERVAL_SECONDS * 10
PAUSE_SECONDS = 186
INSERT_LAG_SECONDS = 1


def _item(track_id, played_at, duration_ms=TRACK_DURATION_MS):
    return {
        "track": {"id": track_id, "duration_ms": duration_ms},
        "played_at": played_at,
        "context": {"type": "playlist"},
    }


def _iso_from_timestamp(timestamp):
    return datetime.datetime.fromtimestamp(
        timestamp, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rich_evidence(rows):
    """Convert only legacy synthetic triples into explicit C5b evidence rows."""
    if all(isinstance(row, dict) for row in rows):
        return rows
    return [
        {
            "rowId": f"synthetic-row-{index}",
            "trackId": track_id,
            "aliases": {track_id},
            "playedAt": played_at,
            "listenerCreatedAt": listener_created_at,
            "createdReason": (
                "listener_play (user: alice)" if listener_created_at is not None else None
            ),
            "isSkip": 0,
        }
        for index, (track_id, played_at, listener_created_at) in enumerate(rows)
    ]


class ListenerBackfillPageContractTest(unittest.TestCase):
    def test_backfill_leaf_import_preserves_original_page_window(self):
        timestamp = timeToInt(FIRST_PLAYED_AT)

        self.assertEqual(
            backfill_page_window([_item("track", FIRST_PLAYED_AT)]),
            (
                timestamp - TRACK_DURATION_SECONDS - WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
                timestamp + WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
            ),
        )

    def _make_listener(self, process_backfill_page=None, credentials=True,
                       backfill_enabled=None):
        get_credentials = None
        if credentials:
            get_credentials = MagicMock(return_value={
                "client_id": "cid",
                "client_secret": "secret",
                "refresh_token": "refresh",
            })

        with patch("Database.Listeners.spotifyListener.Spotify") as spotify_cls:
            spotify = spotify_cls.return_value
            spotify.isLoggedIn.return_value = True
            spotify.current_user.return_value = {
                "id": USER,
                "email": "alice@example.com",
            }
            listener = Listener(
                "dummy-cookie",
                email="alice@example.com",
                user=USER,
                get_credentials=get_credentials,
                get_backfill_enabled=backfill_enabled,
                process_backfill_page=process_backfill_page,
            )
        listener._lastWebApiPollTime = 0
        return listener

    def _run_poll(self, listener, items, ordinary_callback=None,
                  snapshot_callback=None):
        ordinary_callback = ordinary_callback or MagicMock()
        with patch(
            "Database.Listeners.spotifyListener._get_current_user_from_web_api",
            return_value={"id": USER, "email": "alice@example.com"},
        ), patch(
            "Database.Listeners.spotifyListener._refresh_spotify_access_token",
            return_value="token",
        ), patch(
            "Database.Listeners.spotifyListener._fetch_recently_played_from_web_api",
            return_value=items,
        ), patch(
            "Database.Listeners.spotifyListener.time.monotonic",
            return_value=MONOTONIC_NOW,
        ):
            listener._checkWebApiBackfill(
                ordinary_callback,
                onWebApiSnapshot=snapshot_callback,
            )
        return ordinary_callback

    def test_configured_processor_receives_exact_page_and_ordinary_callback_is_not_used(self):
        items = [_item("newer", SECOND_PLAYED_AT), _item("older", FIRST_PLAYED_AT)]
        original_items = copy.deepcopy(items)
        processor = MagicMock()
        ordinary_callback = MagicMock()
        snapshot_callback = MagicMock()
        listener = self._make_listener(process_backfill_page=processor)

        self._run_poll(listener, items, ordinary_callback, snapshot_callback)

        processor.assert_called_once()
        self.assertIs(processor.call_args.args[0], items)
        self.assertEqual(items, original_items)
        ordinary_callback.assert_not_called()
        snapshot_callback.assert_called_once_with(items)
        self.assertEqual(
            [entry["track"]["id"] for entry in listener.webApiRecentlyPlayed_Z1],
            ["newer", "older"],
        )

    def test_missing_credentials_gate_page_delegation(self):
        processor = MagicMock()
        listener = self._make_listener(process_backfill_page=processor, credentials=False)
        ordinary_callback = MagicMock()

        listener._checkWebApiBackfill(ordinary_callback)

        processor.assert_not_called()
        ordinary_callback.assert_not_called()

    def test_account_mismatch_gates_page_delegation_before_fetch(self):
        processor = MagicMock()
        listener = self._make_listener(process_backfill_page=processor)
        ordinary_callback = MagicMock()
        items = [_item("track", FIRST_PLAYED_AT)]

        with patch(
            "Database.Listeners.spotifyListener._get_current_user_from_web_api",
            return_value={"id": "someoneelse", "email": "bob@example.com"},
        ), patch(
            "Database.Listeners.spotifyListener._refresh_spotify_access_token",
            return_value="token",
        ), patch(
            "Database.Listeners.spotifyListener._fetch_recently_played_from_web_api",
            return_value=items,
        ) as fetch, patch(
            "Database.Listeners.spotifyListener.time.monotonic",
            return_value=MONOTONIC_NOW,
        ):
            listener._checkWebApiBackfill(ordinary_callback)

        fetch.assert_not_called()
        processor.assert_not_called()
        ordinary_callback.assert_not_called()

    def test_disabled_backfill_kill_switch_gates_page_delegation(self):
        processor = MagicMock()
        enabled = MagicMock(return_value=False)
        listener = self._make_listener(
            process_backfill_page=processor,
            backfill_enabled=enabled,
        )
        ordinary_callback = MagicMock()
        with patch(
            "Database.Listeners.spotifyListener._fetch_recently_played_from_web_api"
        ) as fetch:
            listener._checkWebApiBackfill(ordinary_callback)

        enabled.assert_called_once_with()
        fetch.assert_not_called()
        processor.assert_not_called()
        ordinary_callback.assert_not_called()

    def test_absent_processor_preserves_cache_only_deduplication(self):
        cached = _item("cached", FIRST_PLAYED_AT)
        listener = self._make_listener(process_backfill_page=None)
        listener.recentlyPlayed_Z1 = [copy.deepcopy(cached)]
        listener.webApiRecentlyPlayed_Z1 = []
        ordinary_callback = MagicMock()
        snapshot_callback = MagicMock()

        self._run_poll(
            listener,
            [_item("cached", FIRST_PLAYED_AT)],
            ordinary_callback,
            snapshot_callback,
        )

        ordinary_callback.assert_not_called()
        snapshot_callback.assert_called_once()
        self.assertEqual(
            listener.webApiRecentlyPlayed_Z1[0]["track"]["id"], "cached"
        )

    def test_api_cache_does_not_use_duration_to_suppress_a_later_api_play(self):
        listener = self._make_listener(process_backfill_page=None)
        listener.webApiRecentlyPlayed_Z1 = [_item("cached", FIRST_PLAYED_AT)]
        callback = MagicMock()

        self._run_poll(
            listener,
            [_item("cached", _iso_from_timestamp(
                timeToInt(FIRST_PLAYED_AT) + TRACK_DURATION_SECONDS
            ))],
            callback,
        )

        callback.assert_called_once()

    def test_api_cache_same_timestamp_still_deduplicates(self):
        listener = self._make_listener(process_backfill_page=None)
        listener.webApiRecentlyPlayed_Z1 = [_item("cached", FIRST_PLAYED_AT)]
        callback = MagicMock()

        self._run_poll(listener, [_item("cached", FIRST_PLAYED_AT)], callback)

        callback.assert_not_called()

    def test_live_and_api_cache_keep_their_source_semantics_separate(self):
        live_cached = self._make_listener(process_backfill_page=None)
        live_cached.recentlyPlayed_Z1 = [_item("cached", FIRST_PLAYED_AT)]
        live_callback = MagicMock()
        self._run_poll(
            live_cached,
            [_item("cached", FIRST_PLAYED_AT)],
            live_callback,
        )
        live_callback.assert_not_called()

        api_cached = self._make_listener(process_backfill_page=None)
        api_cached.webApiRecentlyPlayed_Z1 = [_item("cached", FIRST_PLAYED_AT)]
        api_callback = MagicMock()
        self._run_poll(
            api_cached,
            [_item("cached", _iso_from_timestamp(
                timeToInt(FIRST_PLAYED_AT) + TRACK_DURATION_SECONDS
            ))],
            api_callback,
        )
        api_callback.assert_called_once()


class DatabaseBackfillPageContractTest(DatabaseTestCase):
    def _make_db_with_evidence(self, evidence):
        db = self._makeDb({}, [], username=USER)
        db.repo.getTrackPlayTimesInRange = MagicMock(return_value=_rich_evidence(evidence))
        db._addToDatabaseFromListener = MagicMock()
        return db

    def _submitted_items(self, db):
        if not db._addToDatabaseFromListener.call_args_list:
            return []
        return db._addToDatabaseFromListener.call_args.args[0]

    def test_page_lookup_uses_original_window_and_submits_oldest_first(self):
        items = [_item("newer", SECOND_PLAYED_AT), _item("older", FIRST_PLAYED_AT)]
        db = self._make_db_with_evidence([])

        db.process_backfill_page(items)

        start_ts = timeToInt(FIRST_PLAYED_AT)
        end_ts = timeToInt(SECOND_PLAYED_AT)
        expected_start = (
            start_ts
            - TRACK_DURATION_SECONDS
            - WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
        )
        expected_end = end_ts + WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
        lookup = db.repo.getTrackPlayTimesInRange
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args, (USER, expected_start, expected_end))
        self.assertIn("page", lookup.call_args.kwargs)
        self.assertEqual(
            [item["track"]["id"] for item in self._submitted_items(db)],
            ["older", "newer"],
        )
        self.assertIn("backfillPage", db._addToDatabaseFromListener.call_args.kwargs)

    def test_pause_end_anchor_is_reoffered_by_page_processing(self):
        end_ts = timeToInt(SECOND_PLAYED_AT)
        start_ts = end_ts - TRACK_DURATION_SECONDS - PAUSE_SECONDS
        evidence = [("track", start_ts, end_ts + INSERT_LAG_SECONDS)]
        db = self._make_db_with_evidence(evidence)

        db.process_backfill_page([_item("track", SECOND_PLAYED_AT)])

        self.assertEqual(
            [item["track"]["id"] for item in self._submitted_items(db)],
            ["track"],
        )

    def test_gapless_previous_track_end_does_not_filter_next_track(self):
        first_ts = timeToInt(FIRST_PLAYED_AT)
        items = [
            _item("next", _iso_from_timestamp(first_ts + TRACK_DURATION_SECONDS)),
            _item("previous", FIRST_PLAYED_AT),
        ]
        db = self._make_db_with_evidence([("previous", first_ts, None)])

        db.process_backfill_page(items)

        self.assertEqual(
            [item["track"]["id"] for item in self._submitted_items(db)],
            ["next"],
        )

    def test_lookup_failure_reoffers_the_complete_page_oldest_first(self):
        items = [_item("newer", SECOND_PLAYED_AT), _item("older", FIRST_PLAYED_AT)]
        db = self._make_db_with_evidence([])
        db.repo.getTrackPlayTimesInRange.side_effect = RuntimeError("temporary lookup failure")

        db.process_backfill_page(items)

        self.assertEqual(
            [item["track"]["id"] for item in self._submitted_items(db)],
            ["older", "newer"],
        )
