# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest

from Database.backfill_matching import (
    LISTENER_END_MATCH_TOLERANCE_SECONDS,
    LISTENER_START_MATCH_TOLERANCE_SECONDS,
    WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
    BackfillPage,
    cache_backfill_evidence,
    missing_backfill_items,
)

TRACK_SECONDS = 180
PAUSE_SECONDS = 186  #< a mid-track pause: start-to-end exceeds the track's duration
CLOCK_SKEW_SECONDS = 1  #< listener vs API clock: inside every tolerance, never an exact match


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

    @staticmethod
    def _listenerRow(start, observedEnd=None, source="listener_play (user: alice)"):
        return {"rowId": 7, "trackId": "track", "aliases": {"track"}, "playedAt": start,
                "listenerCreatedAt": observedEnd, "createdReason": source, "isSkip": False}

    def test_listener_end_only_match_suppresses_the_end_time_reading(self):
        """A paused play: its API end-time stamp matches only the listener's
        observed end (created_at). #38 reoffered this as a possible repeat;
        live data refuted that (2026-09-23: 84 of 95 backfill rows after the
        deploy were such copies, and in 0 of them had the listener seen the
        same track start at the API time)."""
        start = 100
        end = start + TRACK_SECONDS + PAUSE_SECONDS
        page = BackfillPage([_item("track", end)])
        row = self._listenerRow(start, observedEnd=end - 1)

        self.assertIs(page.match("track", end, [row]), row)

    def test_listener_end_arm_is_a_point_match(self):
        start = 100
        end = start + TRACK_SECONDS + PAUSE_SECONDS
        page = BackfillPage([_item("track", end)])
        inside = self._listenerRow(start, observedEnd=end - LISTENER_END_MATCH_TOLERANCE_SECONDS)
        outside = self._listenerRow(start, observedEnd=end - LISTENER_END_MATCH_TOLERANCE_SECONDS - 1)

        self.assertIs(page.match("track", end, [inside]), inside)
        self.assertIsNone(page.match("track", end, [outside]))

    def test_listener_duration_derived_start_suppresses_the_end_time_reading(self):
        start = 100
        apiTime = start + TRACK_SECONDS
        page = BackfillPage([_item("track", apiTime)])
        inside = self._listenerRow(start + WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS)
        outside = self._listenerRow(start + WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS + 1)

        self.assertIs(page.match("track", apiTime, [inside]), inside)
        self.assertIsNone(page.match("track", apiTime, [outside]))

    def test_listener_start_tolerance_is_shared_with_reconciliation_by_default(self):
        """Live 2026-09-23: a listener start 3.39s from the API stamp was
        'missing' to the 2s prefilter yet a 'duplicate' to 5s reconciliation,
        so it was inserted and deleted every poll."""
        page = BackfillPage([_item("track", 103.39)])
        row = self._listenerRow(100)
        tooFar = self._listenerRow(103.39 - LISTENER_START_MATCH_TOLERANCE_SECONDS - 0.01)

        self.assertIs(page.match("track", 103.39, [row]), row)
        self.assertIsNone(page.match("track", 103.39, [tooFar]))

    def test_reconciliation_can_opt_out_of_listener_end_arms(self):
        start = 100
        end = start + TRACK_SECONDS + PAUSE_SECONDS
        page = BackfillPage([_item("track", end), _item("track", start + TRACK_SECONDS)])
        paused = self._listenerRow(start, observedEnd=end)

        self.assertIsNone(page.match("track", end, [paused], listenerEndArms=False))
        self.assertIsNone(page.match("track", start + TRACK_SECONDS, [paused], listenerEndArms=False))

    def test_reconciliation_never_deletes_what_the_prefilter_would_insert(self):
        """The loop invariant: if reconciliation would call a backfill copy a
        duplicate of a listener row, the prefilter must already have
        suppressed it - otherwise every poll inserts and deletes it again."""
        reconcileTolerance = LISTENER_START_MATCH_TOLERANCE_SECONDS
        for offset in (0, 1.9, 2.1, 3.39, 4.9, 5.0, 5.1, 10):
            with self.subTest(offset=offset):
                page = BackfillPage([_item("track", 100 + offset)])
                row = self._listenerRow(100, observedEnd=100 + TRACK_SECONDS)
                reconciled = page.match("track", 100 + offset, [row], toleranceSeconds=reconcileTolerance,
                                        startToleranceSeconds=reconcileTolerance, listenerEndArms=False)
                prefiltered = page.match("track", 100 + offset, [row],
                                         derivedStartToleranceSeconds=WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS)
                if reconciled is not None:
                    self.assertIsNotNone(prefiltered)

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

    def test_live_cache_duration_derived_match_suppresses(self):
        page = [_item("track", 100 + TRACK_SECONDS)]
        evidence = cache_backfill_evidence([_item("track", 100)], [])
        self.assertEqual(missing_backfill_items(page, evidence), [])

    def test_one_listener_row_absorbs_at_most_one_end_time_reading(self):
        """F1 (#38's reason for dropping the end arms), kept fixed by the
        claim: back-to-back repeats reported by end time. The listener saw
        only the first; the second is a real play and must come through."""
        start = 100
        end = start + TRACK_SECONDS
        row = {"rowId": 7, "trackId": "track", "aliases": {"track"}, "playedAt": start,
               "listenerCreatedAt": end, "createdReason": "listener_play", "isSkip": False}
        # Two API plays: one stamped at the row's start, one at its end. Each
        # alone would match the row; two stamps are two plays, so only the
        # claim decides which one the row absorbs.
        # Skewed off the row by a clock second: an exact stamp is reserved by
        # its own rule, so only an inexact pair exercises the claim.
        items = [_item("track", end + CLOCK_SKEW_SECONDS), _item("track", start + CLOCK_SKEW_SECONDS)]
        for ordered in (items, list(reversed(items))):
            with self.subTest(order=[item["played_at"] for item in ordered]):
                missing = missing_backfill_items(ordered, [row])
                self.assertEqual([item["played_at"] for item in missing], [end + CLOCK_SKEW_SECONDS])

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
