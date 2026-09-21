# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import time


def normalizeTag(tag: str | None) -> str:
    """Strip whitespace, convert to lowercase, and remove leading '#' characters."""
    if not tag:
        return ""
    cleaned = tag.strip().lower()
    while cleaned.startswith("#"):
        cleaned = cleaned[1:].strip()
    return cleaned


class TagQueries:
    """TagQueries: user tags data-access methods, mixed into Repository."""

    def addTag(self, username: str, tag: str, entity_type: str, entity_id: str) -> str | None:
        if entity_type not in ("track", "artist", "album"):
            raise ValueError(f"Invalid entity_type: {entity_type}")
        norm = normalizeTag(tag)
        if not norm:
            return None
        conn = self._conn()
        # A tag on a track is a tag on the SONG: writes land on the canonical,
        # so a tag added from any release's surface is one row, visible (and
        # removable) wherever the song shows. Reads still union across the
        # group for rows written before a merge existed.
        with conn:
            if entity_type == "track":
                #< BEGIN IMMEDIATE before the resolve, for the reason its
                #  sibling removeTag gives: the INSERT below is built from the
                #  resolve's answer, and a re-head landing between the two
                #  writes the row against a pointer that has already moved.
                #  Milder here than there - the union read still shows the row
                #  and the group delete still removes it - but the guard costs
                #  one PK probe's worth of lock and stops the two verbs
                #  disagreeing about a shape they share (2026-09-03 review, C-11).
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                entity_id = self.resolveCanonicalTrackId(entity_id)
            conn.execute(
                """
                INSERT OR IGNORE INTO user_tags (username, tag, entity_type, entity_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, norm, entity_type, entity_id, time.time()),
            )
        return norm

    def removeTag(self, username: str, tag: str, entity_type: str, entity_id: str) -> bool:
        norm = normalizeTag(tag)
        if not norm:
            return False
        conn = self._conn()
        with conn:
            if entity_type == "track":
                #< BEGIN IMMEDIATE before the resolve: the group DELETE below
                #  is built from its answer, and a re-head landing between the
                #  two makes the subselect miss members - the tag survives its
                #  own removal until another click resolves the new head. Same
                #  remedy as dismissMergeCandidate and saveCachedWrapped.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                #< the whole merge group: the union-read shows a member's legacy
                #  row on the canonical's page, so a targeted delete would leave
                #  an unremovable tag
                canonical = self.resolveCanonicalTrackId(entity_id)
                cur = conn.execute(
                    """
                    DELETE FROM user_tags WHERE username=? AND tag=? AND entity_type='track'
                      AND entity_id IN (SELECT id FROM tracks WHERE id=? OR canonical_id=?)
                    """,
                    (username, norm, canonical, canonical),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM user_tags WHERE username=? AND tag=? AND entity_type=? AND entity_id=?",
                    (username, norm, entity_type, entity_id),
                )
            return cur.rowcount > 0

    def getTagsForEntity(self, username: str, entity_type: str, entity_id: str) -> list[str]:
        conn = self._conn()
        if entity_type == "track":
            #< the union across the merge group: new writes land on the
            #  canonical, but a tag added to a release before its merge still
            #  belongs to the song
            canonical = self.resolveCanonicalTrackId(entity_id)
            rows = conn.execute(
                """
                SELECT DISTINCT tag FROM user_tags WHERE username=? AND entity_type='track'
                  AND entity_id IN (SELECT id FROM tracks WHERE id=? OR canonical_id=?)
                ORDER BY tag ASC
                """,
                (username, canonical, canonical),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tag FROM user_tags WHERE username=? AND entity_type=? AND entity_id=? ORDER BY tag ASC",
                (username, entity_type, entity_id),
            ).fetchall()
        return [r["tag"] for r in rows]

    def getUserTags(self, username: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT tag, COUNT(DISTINCT entity_type || ':' || CASE
                       WHEN entity_type = 'track' THEN COALESCE(
                           (SELECT canonical_id FROM tracks WHERE id = entity_id), entity_id)
                       ELSE entity_id END) as cnt
            FROM user_tags
            WHERE username=?
            GROUP BY tag
            ORDER BY tag ASC
            """,
            (username,),
        ).fetchall()
        return [{"tag": r["tag"], "count": r["cnt"]} for r in rows]

    def getTaggedTrackIds(self, username: str, tags: list[str], match_mode: str = "any") -> list[str]:
        # dict.fromkeys dedups while preserving order, so a caller passing the
        # same tag twice fires one query, not two - and "all" mode's tag count
        # stays honest.
        norm_tags = list(dict.fromkeys(normalizeTag(t) for t in tags if normalizeTag(t)))
        if not norm_tags:
            return []

        conn = self._conn()

        # Expand each tag outward from the (small, indexed) set of tagged
        # entities rather than scanning the whole tracks catalog and testing
        # three OR conditions per row: a track matches if it is tagged directly,
        # its album is tagged, or any of its artists is tagged. UNION dedups, so
        # this returns the same set as the old DISTINCT-with-ORs. Each branch
        # rides an index (user_tags PK prefix, then tracks PK / idx_tracks_album
        # / idx_track_artists_artist). {tags} expands to one placeholder per tag.
        #
        # The artist branch joins tracks back in for the same reason
        # getMatchingTrackIds' identical branch does, with the measurements to
        # match: this database has 195 track_artists rows whose track_id is in
        # no tracks row (a known integrity wart - see checkIntegrity), and
        # without the join those ids reach the caller. A phantom id then
        # narrows a plays aggregate to a track that does not exist, so the Top
        # Songs "Total Plays" card could exceed the sum of its own rows.
        query = """
            SELECT t.id FROM tracks t
                JOIN user_tags ut ON ut.entity_id = t.id
                WHERE ut.username = ? AND ut.entity_type = 'track' AND ut.tag IN ({tags})
            UNION
            SELECT t.id FROM tracks t
                JOIN user_tags ut ON ut.entity_id = t.album_id
                WHERE ut.username = ? AND ut.entity_type = 'album' AND ut.tag IN ({tags})
            UNION
            SELECT ta.track_id FROM track_artists ta
                JOIN tracks t2 ON t2.id = ta.track_id
                JOIN user_tags ut ON ut.entity_id = ta.artist_id
                WHERE ut.username = ? AND ut.entity_type = 'artist' AND ut.tag IN ({tags})
        """

        if match_mode == "all":
            # A track must match every tag, so each tag is resolved separately
            # (one query per tag, single placeholder) and the sets intersected -
            # at SONG level: a group tagged 'a' on its single and 'b' on its
            # album cut is one song carrying both tags.
            single = query.format(tags="?")
            track_sets = []
            for tag in norm_tags:
                ids = [r["id"] for r in conn.execute(
                    single, (username, tag, username, tag, username, tag)).fetchall()]
                _, canonicalOfRequested, _ = self._expandToMergeGroups(ids)
                track_sets.append({canonicalOfRequested.get(i, i) for i in ids})
            canonicals = set.intersection(*track_sets)
            memberIds, _, _ = self._expandToMergeGroups(sorted(canonicals))
            return memberIds

        # "any": a track matching any tag qualifies, which is exactly the union
        # over all tags - so one query with `tag IN (...)` per branch does it.
        placeholders = ",".join("?" for _ in norm_tags)
        rows = conn.execute(
            query.format(tags=placeholders),
            (username, *norm_tags, username, *norm_tags, username, *norm_tags),
        ).fetchall()
        #< expanded to whole merge groups: the plays filters these ids feed
        #  (getSongsPage(trackIds=...), the exports) must cover every release's
        #  plays, or a tagged song's row shows a fraction of its count
        memberIds, _, _ = self._expandToMergeGroups([r["id"] for r in rows])
        return memberIds

    def getTaggedArtistIds(self, username: str, tags: list[str]) -> list[str]:
        """Artist ids directly tagged with any of `tags` - unlike
        getTaggedTrackIds, this does not expand outward to e.g. artists of a
        tagged track's other artists, since "tagged" on an artist-list page
        means the artist itself was tagged, not merely associated with one."""
        norm_tags = list(dict.fromkeys(normalizeTag(t) for t in tags if normalizeTag(t)))
        if not norm_tags:
            return []
        conn = self._conn()
        placeholders = ",".join("?" for _ in norm_tags)
        rows = conn.execute(
            f"""
            SELECT entity_id AS id FROM user_tags
            WHERE username = ? AND entity_type = 'artist' AND tag IN ({placeholders})
            """,
            (username, *norm_tags),
        ).fetchall()
        return [r["id"] for r in rows]

    def getTaggedAlbumIds(self, username: str, tags: list[str]) -> list[str]:
        """Album ids directly tagged with any of `tags` - see getTaggedArtistIds
        for why this doesn't expand to e.g. albums of a tagged track."""
        norm_tags = list(dict.fromkeys(normalizeTag(t) for t in tags if normalizeTag(t)))
        if not norm_tags:
            return []
        conn = self._conn()
        placeholders = ",".join("?" for _ in norm_tags)
        rows = conn.execute(
            f"""
            SELECT entity_id AS id FROM user_tags
            WHERE username = ? AND entity_type = 'album' AND tag IN ({placeholders})
            """,
            (username, *norm_tags),
        ).fetchall()
        return [r["id"] for r in rows]

    def renameTag(self, username: str, old_tag: str, new_tag: str) -> int:
        old_norm = normalizeTag(old_tag)
        new_norm = normalizeTag(new_tag)
        if not old_norm or not new_norm or old_norm == new_norm:
            return 0
        conn = self._conn()
        with conn:
            # Update non-conflicting rows first
            cur1 = conn.execute(
                """
                UPDATE OR IGNORE user_tags
                SET tag = ?
                WHERE username = ? AND tag = ?
                """,
                (new_norm, username, old_norm),
            )
            # Delete remaining old_norm rows (which were duplicates of existing new_norm rows)
            cur2 = conn.execute(
                "DELETE FROM user_tags WHERE username = ? AND tag = ?",
                (username, old_norm),
            )
            return cur1.rowcount + cur2.rowcount

    def deleteTag(self, username: str, tag: str) -> int:
        norm = normalizeTag(tag)
        if not norm:
            return 0
        conn = self._conn()
        with conn:
            cur = conn.execute("DELETE FROM user_tags WHERE username = ? AND tag = ?", (username, norm))
            return cur.rowcount
