# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Database-independent Web API backfill windows and per-page matching."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from Database.db import WEB_API_BACKFILL_SOURCE
from Database.utils import timeToInt


WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS = 2
LISTENER_START_MATCH_TOLERANCE_SECONDS = 5  #< max gap between a listener row's start and an API stamp for
                                            #  the two to be one listen. Shared with reconciliation
                                            #  (Database.DUPLICATE_RECORDING_TOLERANCE_SECONDS): a narrower
                                            #  prefilter inserts what reconciliation then deletes, every poll
                                            #  (live 2026-09-23: a 3.39s gap looped 36 times in 8h)
LISTENER_END_MATCH_TOLERANCE_SECONDS = 10  #< max gap between an API stamp and a listener row's created_at,
                                           #  its observed end, pauses included. A point match: live insert
                                           #  lag is ~1s, anything minutes away is a different listen
                                           #  (Database.BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS)
MILLISECONDS_PER_SECOND = 1_000
_LISTENER_SOURCE_PREFIX = "listener_play"
_LIVE_CACHE_SOURCE = "listener_cache"


def _item_track_id(item: Mapping) -> str | None:
    track = item.get("track") or item.get("item") or {}
    if not isinstance(track, Mapping):
        return None
    return track.get("id") or track.get("track_id")


def _item_timestamp(item: Mapping) -> float:
    value = item.get("played_at")
    if value is None:
        value = item.get("playedAt")
    return timeToInt(value) if value is not None else 0


def _track_descriptor(item: Mapping) -> dict:
    """The raw page can supply identity before a suppressed alias is catalogued.

    Canonical/merge identity is deliberately absent: only the repository can
    supply that decided fact. This descriptor carries the two existing
    fallback proofs (ISRC and exact name/primary-artist/duration).
    """
    track = item.get("track") or item.get("item") or {}
    artists = track.get("artists") or []
    primary = artists[0] if artists and isinstance(artists[0], Mapping) else {}
    external = track.get("external_ids") or {}
    return {
        "id": _item_track_id(item),
        "name": track.get("name"),
        "durationMs": track.get("duration_ms"),
        "isrc": track.get("isrc") or external.get("isrc") or "",
        "primaryArtistId": primary.get("id"),
    }


class BackfillPage:
    """One page's logical events and confirmed physical-row assignments.

    Rows and aliases remain fresh inputs to match(), including after a catalog
    upsert. Only claims persist here. Exact page timestamps reserve their
    matching rows before any ambiguous assignment, independent of page order.
    """

    def __init__(self, items: Iterable[Mapping]):
        self._events: dict[tuple[str, float], dict] = {}
        self._times_by_track: dict[str, set[float]] = {}
        self._claims: dict[object, float] = {}
        for item in items:
            track_id = _item_track_id(item)
            timestamp = _item_timestamp(item)
            if track_id and timestamp > 0:
                self._events.setdefault((track_id, timestamp), _track_descriptor(item))
                self._times_by_track.setdefault(track_id, set()).add(timestamp)

    @property
    def trackIds(self) -> set[str]:
        return set(self._times_by_track)

    @property
    def pendingTracks(self) -> list[dict]:
        return list(self._events.values())

    def match(self, trackId: str, playedAt: float, rows: Iterable[Mapping], *,
              toleranceSeconds: float | None = None,
              skipToleranceSeconds: float | None = None,
              startToleranceSeconds: float = WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
              derivedStartToleranceSeconds: float | None = None,
              listenerEndArms: bool = True):
        """Return a confirmed candidate without consuming it.

        API rows use exact normalized timestamp identity, even when classified
        as skips. A non-skip listener row is one listen with the API stamp when
        the stamp is its start (LISTENER_START_MATCH_TOLERANCE_SECONDS), its
        observed end (created_at, pauses included) or its start plus the
        track's duration - Spotify's played_at is documented as either. The
        claim keeps that one-to-one: a row absorbs one stamp per page, so a
        back-to-back repeat the listener missed still comes through.
        #38 reoffered the end readings as possible repeats; live data refuted
        it (2026-09-23: 84 of 95 backfill rows after the deploy were copies,
        and at the API time the listener had seen the same track start in 0).
        Reconciliation deletes, so it passes listenerEndArms=False and stays
        start-only. NULL/import/unknown rows retain the legacy guard window;
        only the page prefilter opts into their duration-derived start.
        """
        timestamp = float(playedAt)
        descriptor = self._events.get((trackId, timestamp), {})
        candidates = []
        for row in rows:
            row_id = row.get("rowId")
            row_time = row.get("playedAt")
            if row_id is None or row_time is None:
                continue
            row_time = float(row_time)
            claimed_time = self._claims.get(row_id)
            if claimed_time is not None and claimed_time != timestamp:
                continue
            aliases = set(row.get("aliases") or ()) | {row["trackId"]}
            if trackId not in aliases:
                continue
            reason = row.get("createdReason") or ""
            is_api = reason.startswith(WEB_API_BACKFILL_SOURCE)
            distance = abs(row_time - timestamp)
            if distance == 0:
                candidates.append((0 if is_api else 1, distance, str(row_id), row))
                continue
            if is_api:
                continue
            # This physical row belongs to an exact event elsewhere in the
            # complete page. Alias lookup is current, never cached pre-upsert.
            if any(row_time in self._times_by_track.get(alias, ()) for alias in aliases):
                continue
            if row.get("isSkip"):
                skip_tolerance = (skipToleranceSeconds if skipToleranceSeconds is not None
                                  else startToleranceSeconds)
                if distance <= skip_tolerance:
                    candidates.append((2, distance, str(row_id), row))
                continue
            is_listener = reason.startswith(_LISTENER_SOURCE_PREFIX) or reason == _LIVE_CACHE_SOURCE
            start_tolerance = (max(startToleranceSeconds, LISTENER_START_MATCH_TOLERANCE_SECONDS)
                               if is_listener else startToleranceSeconds)
            if distance <= start_tolerance:
                candidates.append((2, distance, str(row_id), row))
                continue
            if is_listener:
                if listenerEndArms:
                    end_distance = self._listener_end_distance(row, timestamp, descriptor)
                    if end_distance is not None:
                        candidates.append((3, end_distance, str(row_id), row))
                continue
            if toleranceSeconds is not None and distance <= toleranceSeconds:
                candidates.append((3, distance, str(row_id), row))
                continue
            duration_ms = descriptor.get("durationMs") or 0
            if derivedStartToleranceSeconds is not None and duration_ms:
                derived_distance = abs(timestamp - duration_ms / MILLISECONDS_PER_SECOND - row_time)
                if derived_distance <= derivedStartToleranceSeconds:
                    candidates.append((3, derived_distance, str(row_id), row))
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: candidate[:3])[3]

    @staticmethod
    def _listener_end_distance(row: Mapping, timestamp: float, descriptor: Mapping) -> float | None:
        """How far the API stamp sits from this listener row's end, or None
        when neither end reading lands within its tolerance."""
        distances = []
        observed_end = row.get("listenerCreatedAt")
        if observed_end is not None:
            distances.append((abs(timestamp - float(observed_end)), LISTENER_END_MATCH_TOLERANCE_SECONDS))
        duration_ms = descriptor.get("durationMs") or 0
        if duration_ms:
            derived = abs(timestamp - duration_ms / MILLISECONDS_PER_SECOND - float(row["playedAt"]))
            distances.append((derived, WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS))
        within = [distance for distance, tolerance in distances if distance <= tolerance]
        return min(within) if within else None

    def claim(self, row: Mapping, playedAt: float) -> bool:
        """Record a confirmed read or committed match; failed writes never call this."""
        row_id = row.get("rowId")
        timestamp = float(playedAt)
        if row_id is None:
            return False
        existing = self._claims.get(row_id)
        if existing is not None:
            return existing == timestamp
        self._claims[row_id] = timestamp
        return True


def backfill_page_window(items: list) -> tuple[float, float] | None:
    timestamps = [ts for ts in (timeToInt(item.get("played_at")) for item in items) if ts > 0]
    if not timestamps:
        return None
    longest_track_seconds = max(
        ((item.get("track") or {}).get("duration_ms", 0) or 0) // MILLISECONDS_PER_SECOND for item in items
    )
    return (
        min(timestamps) - longest_track_seconds - WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
        max(timestamps) + WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS,
    )


def cache_backfill_evidence(liveItems: Iterable[Mapping], apiItems: Iterable[Mapping]) -> list[dict]:
    """Cache-only observations use logical identities, never database row IDs."""
    evidence = []
    for source, items in ((_LIVE_CACHE_SOURCE, liveItems), (WEB_API_BACKFILL_SOURCE, apiItems)):
        for item in items or ():
            track_id = _item_track_id(item)
            timestamp = _item_timestamp(item)
            if not track_id or timestamp <= 0:
                continue
            evidence.append({
                "rowId": (source, track_id, timestamp),
                "trackId": track_id,
                "aliases": {track_id},
                "playedAt": timestamp,
                "listenerCreatedAt": None,
                "createdReason": source,
                "isSkip": bool(item.get("is_skip", item.get("isSkip", 0))),
            })
    return evidence


def missing_backfill_items(items: list, evidenceRows, *, page: BackfillPage | None = None) -> list:
    """Claim oldest-first, then return missing items in their original order."""
    page = page if page is not None else BackfillPage(items)
    evidence = list(evidenceRows or ())
    missing = {}
    ordered = sorted(enumerate(items), key=lambda pair:
                     (_item_timestamp(pair[1]), _item_track_id(pair[1]) or "", pair[0]))
    for index, item in ordered:
        track = item.get("track") or {}
        track_id = _item_track_id(item)
        played_at = item.get("played_at")
        timestamp = _item_timestamp(item)
        if not track_id or not played_at or timestamp <= 0:
            continue
        matched = page.match(track_id, timestamp, evidence,
                             derivedStartToleranceSeconds=WEB_API_BACKFILL_DEDUP_TOLERANCE_SECONDS)
        if matched is not None and page.claim(matched, timestamp):
            continue
        missing[index] = {
            "track": track,
            "played_at": played_at,
            "ms_played": track.get("duration_ms", 0) or 0,
            "context": item.get("context") or {},
        }
    return [missing[index] for index in sorted(missing)]
