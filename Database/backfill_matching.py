# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Database-independent Web API backfill window and matching calculations."""

import logging

from Database.utils import flaskDebugEnabled, timeToInt


WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS = 2  #< max gap between a Web API item's played_at (interpreted as
                                               #  either a start or end time - see the comment at its use site)
                                               #  and an already-recorded play's timestamp for them to count as
                                               #  the same play rather than a genuinely missing one

WEB_API_BACKFILL_END_TIME_DEDUP_TOLERANCE_SECONDS = 10  #< max gap between a Web API item's played_at and a
                                                        #  recorded listener row's created_at (its observed play
                                                        #  end - the listener inserts at the track-change moment)
                                                        #  for them to count as the same play. Wider than the 2s
                                                        #  tolerance above because insert lag sits between the
                                                        #  two stamps (~1s live, a few seconds in poll mode), but
                                                        #  deliberately still a point match - a recorded end
                                                        #  minutes away is evidence of a DIFFERENT listen, and
                                                        #  suppressing on it would lose that play for good


_BACKFILL_LOGGER = logging.getLogger("Database.Listeners.spotifyListener")


def backfill_page_window(items: list) -> tuple[float, float] | None:
    """Return the existing database-evidence window for an API page."""
    timestamps = [ts for ts in (timeToInt(item.get("played_at")) for item in items) if ts > 0]
    if not timestamps:
        return None

    # played_at may be the END of a play (see the dedup comment below), in
    # which case the recorded row sits up to one track-length earlier - so
    # the window has to reach back that far to find it. A PAUSED play's
    # start sits even earlier (duration + pause), which no fixed reach-back
    # can cover - the query closes that gap itself by also matching
    # listener rows into the window by their created_at.
    longest_track_seconds = max(
        ((item.get("track") or {}).get("duration_ms", 0) or 0) // 1000 for item in items
    )
    return (
        min(timestamps) - longest_track_seconds - WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
        max(timestamps) + WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
    )


def recorded_play_times_by_track(rows) -> dict:
    """Group repository ``(track_id, played_at, listener_created_at)`` rows."""
    recorded: dict = {}
    for track_id, played_at, listener_created_at in rows:
        recorded.setdefault(track_id, set()).add(
            (float(played_at), float(listener_created_at) if listener_created_at is not None else None)
        )
    return recorded


def missing_backfill_items(items: list, recorded_timestamps: dict, log_user=None) -> list:
    """Return API items absent from the recorded evidence, preserving old arms."""
    # Built directly from `items` in one pass so each missed item stays tied
    # to its OWN source API item's played_at - no post-hoc re-matching by
    # track ID, which breaks when the same track appears more than once in
    # `items` (all copies would resolve to whichever occurrence next() finds
    # first).
    missed_items = []

    for item in items:
        played_at_str = item.get("played_at")
        track = item.get("track")
        track_id = track.get("id") if track else None
        if not played_at_str or not track_id:
            continue

        timestamp = timeToInt(played_at_str)
        # `or 0` (not get's default): an item's track can carry
        # "duration_ms": None (present but null), where dict.get returns None
        # rather than falling back to 0, and the division below would crash
        # and abort the whole poll.
        duration_ms = track.get("duration_ms", 0) or 0
        duration_s = duration_ms // 1000

        # Spotify's Web API documents played_at only as "the date and time the
        # track was played" - it does NOT specify start vs end, and Spotify's
        # own developer community has confirmed the same endpoint can report
        # either for different entries (see spotify/web-api#1083). So this
        # can't assume one direction: check both interpretations - timestamp
        # itself already being a start time, or timestamp being an end time
        # duration_s seconds after the true start - before deciding this play
        # is genuinely missing.
        #
        # Only THIS track's recorded times can answer for it. A flat set of
        # timestamps meant any recorded play within the tolerance did, and
        # the second arm made that systematic: under gapless playback a
        # missing track's derived start equals the recorded END of the track
        # before it - i.e. the one recorded neighbour a real gap always has
        # beside it. The suppressed play was then lost for good, since the
        # next poll's page collides identically and nothing else retries it.
        #
        # The third arm covers what the second cannot: a mid-track PAUSE
        # stretches start-to-end beyond duration_s by an unbounded amount
        # (2026-08-04: a ~3min pause put played_at 474s after a 287s track's
        # recorded start, and the same listen was recorded twice). A listener
        # row's created_at is its OBSERVED end - the row is inserted at the
        # track-change moment, pauses included - so the end-time interpretation
        # is matched against that stamp directly instead of deriving a start
        # that assumes uninterrupted playback.
        recorded_times = recorded_timestamps.get(track_id, ())
        matched_by_played_at = any(
            abs(timestamp - recorded_t) <= WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
            or abs(timestamp - duration_s - recorded_t) <= WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS
            for recorded_t, _recorded_end in recorded_times
        )
        matched_by_end = not matched_by_played_at and any(
            recorded_end is not None
            and abs(timestamp - recorded_end) <= WEB_API_BACKFILL_END_TIME_DEDUP_TOLERANCE_SECONDS
            for _recorded_t, recorded_end in recorded_times
        )
        if matched_by_end and flaskDebugEnabled():
            # Live validation for the 2026-08-04 pause-duplicate fix:
            # only the cases the two played_at arms would have MISSED
            # are interesting - remove once a few days of logs confirm
            # the arm fires on real pauses and nothing else.
            _BACKFILL_LOGGER.info(
                "Backfill item for track %s (played_at=%s) suppressed by the end-time arm alone "
                "(pause-stretched play already recorded) for user %s",
                track_id,
                played_at_str,
                log_user,
            )
        if not (matched_by_played_at or matched_by_end):
            context = item.get("context") or {}

            # Store played_at as given, untouched - see comment above
            # on why we no longer subtract duration_s here.
            missed_items.append({
                "track": track,
                "played_at": played_at_str,
                "ms_played": duration_ms,
                "context": context,
            })

    return missed_items
