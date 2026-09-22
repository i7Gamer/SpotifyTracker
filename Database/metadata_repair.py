# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Small immutable records shared by catalog repair owners."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackRepairImpact:
    trackId: str
    oldAlbumId: str | None
    newAlbumId: str | None
    oldArtistIds: frozenset[str]
    newArtistIds: frozenset[str]


@dataclass(frozen=True)
class WrappedRepairResult:
    repaired: int
    repairDeleted: int
    historyDeleted: int
    mode: str
    reason: str | None
