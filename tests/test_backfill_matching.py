# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest

from Database.backfill_matching import (
    BackfillPage,
    cache_backfill_evidence,
    missing_backfill_items,
)


def _item(track_id, played_at, duration_ms=180_000):
    return {
        "track": {
            "id": track_id,
            "name": "Song",
            "duration_ms": duration_ms,
            "artists": [{"id": "artist"}],
        },
        "played_at": played_at,
        "context": {"uri": "playlist:test"},
    }


class TestBackfillPage(unittest.TestCase):
    def test_api_identity_matches_alias_and_skip_rows_only_at_exact_timestamp(self):
        page = BackfillPage([_item("api-id", 100)])
        row = {
            "rowId": 7,
            "trackId": "listener-id",
            "aliases": {"listener-id", "api-id"},
            "playedAt": 100,
            "listenerCreatedAt": None,
            "createdReason": "web_api_backfill_play (user: alice)",
            "isSkip": True,
        }

        self.assertIs(page.match("api-id", 101, [row]), None)
        self.assertIs(page.match("api-id", 100, [row]), row)

    def test_claim_prevents_a_second_timestamp_from_reusing_one_row(self):
        page = BackfillPage([_item("track", 100), _item("track", 101)])
        row = {
            "rowId": 7,
            "trackId": "track",
            "aliases": {"track"},
            "playedAt": 100,
            "listenerCreatedAt": None,
            "createdReason": "listener_play (user: alice)",
            "isSkip": False,
        }

        self.assertIs(page.match("track", 100, [row]), row)
        self.assertTrue(page.claim(row, 100))
        self.assertTrue(page.claim(row, 100))
        self.assertFalse(page.claim(row, 101))
        self.assertIsNone(page.match("track", 101, [row]))

    def test_listener_end_only_match_is_reoffered(self):
        page = BackfillPage([_item("track", 200)])
        row = {
            "rowId": 7,
            "trackId": "track",
            "aliases": {"track"},
            "playedAt": 100,
            "listenerCreatedAt": 200,
            "createdReason": "listener_play (user: alice)",
            "isSkip": False,
        }

        self.assertIsNone(page.match("track", 200, [row]))

    def test_start_tolerance_can_be_explicitly_widened_for_reconciliation(self):
        page = BackfillPage([_item("track", 104)])
        row = {
            "rowId": 7,
            "trackId": "track",
            "aliases": {"track"},
            "playedAt": 100,
            "listenerCreatedAt": None,
            "createdReason": "listener_play (user: alice)",
            "isSkip": False,
        }

        self.assertIsNone(page.match("track", 104, [row]))
        self.assertIs(page.match("track", 104, [row], startToleranceSeconds=5), row)

    def test_non_listener_and_skip_matching_remain_explicit(self):
        page = BackfillPage([_item("track", 250), _item("track", 110)])
        imported = {
            "rowId": 8,
            "trackId": "track",
            "aliases": {"track"},
            "playedAt": 100,
            "listenerCreatedAt": None,
            "createdReason": "history_import (user: alice)",
            "isSkip": False,
        }
        skip = {
            "rowId": 9,
            "trackId": "track",
            "aliases": {"track"},
            "playedAt": 100,
            "listenerCreatedAt": 200,
            "createdReason": "listener_play (user: alice)",
            "isSkip": True,
        }

        self.assertIs(page.match("track", 250, [imported], toleranceSeconds=150), imported)
        self.assertIs(page.match("track", 110, [skip], skipToleranceSeconds=20), skip)


class TestBackfillLeafHelpers(unittest.TestCase):
    def test_missing_items_preserves_input_order_and_cache_evidence_is_logical(self):
        items = [_item("seen", 100), _item("missing", 200)]
        page = BackfillPage(items)
        evidence = cache_backfill_evidence(
            [{"track": {"track_id": "seen"}, "played_at": 100}], []
        )

        missing = missing_backfill_items(items, evidence, page=page)

        self.assertEqual([item["track"]["id"] for item in missing], ["missing"])
        self.assertEqual(evidence[0]["trackId"], "seen")
        self.assertEqual(evidence[0]["rowId"], ("listener_cache", "seen", 100))


class TestBackfillClaimEdges(unittest.TestCase):
    @staticmethod
    def _row(row_id=7, timestamp=100, source="listener_play", *, skip=False):
        return {"rowId": row_id, "trackId": "track", "aliases": {"track", "alias"},
                "playedAt": timestamp, "createdReason": source, "isSkip": skip}

    def test_claimed_ambiguous_row_cannot_suppress_later_timestamp_under_an_alias(self):
        page = BackfillPage([_item("track", 101), _item("alias", 102)])
        row = self._row()
        self.assertIs(page.match("track", 101, [row]), row)
        self.assertTrue(page.claim(row, 101))
        self.assertIsNone(page.match("alias", 102, [row]))
        self.assertIs(page.match("track", 101, [row]), row)

    def test_exact_reservations_follow_fresh_evidence_after_a_row_disappears(self):
        page = BackfillPage([_item("track", 100)])
        old = self._row()
        replacement = self._row(row_id=8, timestamp=99)
        self.assertIs(page.match("track", 100, [old]), old)
        self.assertIs(page.match("track", 100, [replacement]), replacement)

    def test_cleanup_never_uses_legacy_duration_derived_start(self):
        page = BackfillPage([_item("track", 280)])
        row = self._row(source="history_import")
        self.assertIsNone(page.match("track", 280, [row], toleranceSeconds=5,
                                     startToleranceSeconds=5))

    def test_legacy_skip_never_uses_wide_real_play_or_duration_windows(self):
        page = BackfillPage([_item("track", 190)])
        for source in (None, "history_import", "unknown"):
            with self.subTest(source=source):
                row = self._row(source=source, skip=True)
                self.assertIsNone(page.match("track", 190, [row], toleranceSeconds=150,
                                             skipToleranceSeconds=20))

    def test_live_cache_end_only_match_is_reoffered(self):
        page = [_item("track", 280)]
        evidence = cache_backfill_evidence([_item("track", 100)], [])
        self.assertEqual(len(missing_backfill_items(page, evidence)), 1)

    def test_duplicate_live_cache_observations_share_one_logical_claim(self):
        item = _item("track", 100)
        evidence = cache_backfill_evidence([item, item], [])
        self.assertEqual(evidence[0]["rowId"], evidence[1]["rowId"])
        page = [_item("track", 101), _item("track", 102)]
        self.assertEqual([item["played_at"] for item in missing_backfill_items(page, evidence)], [102])

    def test_api_millisecond_timestamps_normalize_without_near_time_collapse(self):
        timestamp = "2023-11-14T22:13:20.123Z"
        normalized = 1_700_000_000.123
        row = self._row(timestamp=normalized, source="web_api_backfill_play")
        page = BackfillPage([_item("track", timestamp), _item("alias", normalized + 0.001)])
        self.assertIs(page.match("track", normalized, [row]), row)
        self.assertIsNone(page.match("alias", normalized + 0.001, [row]))

    def test_exact_api_copy_wins_over_nearby_primary(self):
        page = BackfillPage([_item("track", 100)])
        exact = self._row(row_id=9, source="web_api_backfill_play")
        primary = self._row(row_id=1, timestamp=101)
        self.assertIs(page.match("track", 100, [primary, exact]), exact)

    def test_ambiguous_assignment_is_oldest_first_in_either_page_order(self):
        items = [_item("track", 102), _item("alias", 101)]
        for ordered in (items, list(reversed(items))):
            with self.subTest(order=[item["played_at"] for item in ordered]):
                missing = missing_backfill_items(ordered, [self._row()])
                self.assertEqual([item["played_at"] for item in missing], [102])


if __name__ == "__main__":
    unittest.main()
