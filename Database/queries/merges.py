# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import re

from Database.queries._base import (
    APP_SETTING_FALSE,
    APP_SETTING_TRUE,
    TRACK_MERGE_LAST_RUN_KEY,
    TRACK_MERGE_SETTING_KEY,
    time,
)

# The manual review tier's duration gate, measured on a live copy
# (2026-08-07): most same-recording pairs agree within 1s, and the pairs
# differing by >10s are genuinely different cuts - 3s keeps different
# recordings out while letting scan-time drift through. A duration of 0 means
# "unknown" (a fabricated import row), which cannot disagree with anything.
MERGE_REVIEW_DURATION_TOLERANCE_MS = 3000
#< display cap only - the returned totals always count the full queue
MERGE_REVIEW_PAGE_LIMIT = 50
#< same, for the list of dismissals underneath it. Its own constant because it
#  is a decision LOG rather than a work queue: nothing in it needs answering,
#  so it can be trimmed harder if it ever grows past reading.
MERGE_DISMISSED_PAGE_LIMIT = 50
# Stay below SQLite's portable parameter limit when guarding a large plan.
MERGE_GUARD_QUERY_BATCH_SIZE = 900

# A trailing " - X" or "(X)" title segment is a version marker - packaging,
# not identity - when it contains one of these. "live" is deliberately NOT
# here: a live cut is a different performance, and blurring that line is the
# one mistake the review queue must never invite. Bare "mix" and "edit" fell
# to the same rule: "Remix" contains "mix", so they proposed every remix and
# club edit against its original - a different recording, the live case
# again. The explicit same-recording forms keep their own entries ("radio
# edit", the "stereo"/"mono" mixes); the cost is that a bare "- Edit" or
# "- 2019 Mix" suffix is no longer proposed, recall traded for not inviting
# the one wrong merge.
TITLE_VERSION_MARKERS = (
    "remaster", "mono", "stereo", "deluxe", "anniversary", "re-record",
    "rerecord", "single version", "radio edit", "album version",
    "bonus track", "extended", "version", "feat.", "ft.",
)

_PARENTHETICAL_SUFFIX_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


def normalizeTrackTitle(name: str) -> str:
    """The identity half of a title: lowercased, version-marker suffixes gone.

    A remaster differs from its original by NAME ("Psycho Killer" vs "Psycho
    Killer - 2005 Remaster"), so exact matching finds only ~2/3 of the real
    duplicate groups (427 vs 648, measured). Suffixes strip repeatedly -
    "Song - Mono - 2009 Remaster" sheds both - and a title that is NOTHING
    but a marker keeps its name rather than collapsing to the empty key."""
    title = (name or "").strip().lower()
    while True:
        match = _PARENTHETICAL_SUFFIX_RE.search(title)
        if match and any(marker in match.group(1) for marker in TITLE_VERSION_MARKERS):
            title = title[:match.start()].strip()
            continue
        head, sep, tail = title.rpartition(" - ")
        if sep and any(marker in tail for marker in TITLE_VERSION_MARKERS):
            title = head.strip()
            continue
        break
    return title or (name or "").strip().lower()


def isPlainTitle(name: str) -> bool:
    """Whether this title IS the song's name, with no packaging behind it.

    The same marker list normalizeTrackTitle groups by, asked the other way
    round: a title that survives normalization untouched carries no deluxe,
    feat., remaster or mono suffix, so it is the release a person means when
    they name the song. Deriving it from the same function is the point -
    "these are the same song" and "this one is the normal version of it" must
    never be able to disagree about what a marker is.

    What is NOT a marker stays not one here either: a live cut or a remix is a
    different performance, so those titles read as plain - which is correct,
    since that IS the song's name on that release."""
    return normalizeTrackTitle(name) == (name or "").strip().lower()


def _titleRank(row) -> tuple:
    """How well a release's TITLE reads as the song itself, biggest is best.

    The stable half of _electCanonical, split out because being stable is what
    it is used for: a name either carries a version marker or it does not, and
    the answer is the same on every pass. Play counts drift, which is why an
    existing canonical is sticky against them - but a group whose head is
    beaten HERE can step down once and settle, so the ISRC tier compares this
    prefix alone before it touches a head it already elected.

    A blank name loses on the first key rather than sweeping the other two:
    "" survives normalization untouched and is the shortest string there is,
    so a blanked or fallback row would otherwise read as the plainest title in
    the group and take the song's page."""
    name = (row["name"] or "").strip()
    return (bool(name), bool(name) and isPlainTitle(name), -len(name))


class MergeQueries:
    """Merge planning, decisions and canonical reads, mixed into Repository."""

    # The per-track play tally both merge tiers rank by, as ONE aggregate pass
    # over plays joined back - never a correlated per-candidate COUNT. No play
    # index leads with track_id (both lead with username), so the correlated
    # form re-read every play for each candidate: measured 8 s at 840
    # candidates over 134k plays, paid on every admin page view while the
    # toggle is off. LEFT JOIN, because a track nobody has played yet must
    # stay in its group at zero.
    _PLAY_TALLY_JOIN = ("LEFT JOIN (SELECT track_id, COUNT(*) AS play_count "
                        "FROM plays GROUP BY track_id) pc ON pc.track_id = t.id")

    @staticmethod
    def _clearContradictedRejection(conn, canonicalId: str) -> None:
        """Drop a "not the same recording" that this merge has just disproved.

        Only the release being MERGED has its decision row rewritten - it is
        the one the verdict is about. That left the opposite direction alone:
        rejecting the plain release against a remaster and then merging the
        remaster INTO it kept the rejection, so the "Kept separate" log went
        on naming a release that had since joined its group. The title rule
        makes it the likely direction rather than the odd one, since the
        election prefers exactly the plain release a person rules the remaster
        against.

        Deleted, not rewritten, for undismissMergeCandidate's reason: there is
        no row meaning "un-rejected", and the state that lets the queue ask
        again is the one where nobody has answered. Splitting the pair back
        apart does not bring it back - the same one-way trip the member arm
        already makes when a merge overwrites its row.

        Narrow on purpose: ONLY a rejection whose counterpart is now in this
        group. "B is not the same as C" is not contradicted by A merging into
        B, and a rejection naming nothing (recorded before against_id existed)
        cannot be contradicted by anything. against_id can only be a member,
        never the canonical itself - dismissMergeCandidate refuses both a
        self-reference and a counterpart already merged into the track."""
        conn.execute(
            """
            DELETE FROM track_merge_decisions
            WHERE track_id = ? AND reason = 'manual-reject'
              AND against_id IN (SELECT id FROM tracks WHERE canonical_id = ?)
            """,
            (canonicalId, canonicalId))

    @staticmethod
    def _electCanonical(candidates: list):
        """Which release of a group the others fold into: a named release over
        a blank one, then one with plays here over one with none, then the
        plainest title, then the shortest, then most played, then first seen,
        then id.

        The title leads because the winner becomes the song's PAGE. Plays
        alone used to decide, which handed the page to whichever pressing
        happened to be seeded best - a song listed as "Psycho Killer - 2005
        Remaster" everywhere reads as a different song from the one a person
        played, even though the number underneath is right. isPlainTitle finds
        the release that is just the song's name; where a group has no such
        release ("- Mono - 2009 Remaster" against "- 2009 Remaster") the
        shortest name is the least packaged of what is on offer. Plays did not
        go away, they moved below a question they were never answering: two
        copies of the bare title are exactly the case the title rule is silent
        about, and there the most played still wins.

        One rule, two tiers. The ISRC matcher elects from a group's unpinned
        members and the review queue elects the anchor it proposes merging
        INTO, and they must not disagree about which release survives - a
        person answering the queue would otherwise place a song somewhere the
        next automatic pass would have chosen differently. Both said so in
        prose while spelling the tuple twice; this is the same reason
        _planIsrcMerges exists as a seam between the preview and the run.

        Having ANY plays here is its own tier, between the name guard and the
        title rule. The title rule reads a name to find the release a person
        means by the song, and it read one for releases nobody in the instance
        has ever played too: on the live queue (2026-08-12) three of the fifty
        visible rows proposed handing the page to an untouched pressing - "We
        Are the Warriors" on a compilation, against the feat. release carrying
        all 22 plays - taking the cover and the Spotify link with it. "Played
        less" is the question the title rule answers; "no history here at all"
        is not the same question. Groups where nothing has been played tie
        here and fall through to the title rule unchanged, which is every
        group in a fresh instance.

        Deliberately NOT folded into _titleRank, which _planIsrcMerges
        compares alone to let an already-elected head step down: that tuple is
        the drift-free half, and a key that flips the first time a sibling is
        played would move a settled song's page out from under the reader.
        The cost is that this tier only ever decides a FIRST election - a
        merged group keeps the head it was given.

        Rows need name, play_count, created_at and id. The last two are pure
        tie-breakers, there so the answer is deterministic rather than
        whatever order SQLite happened to return."""
        def rank(row):
            named, plain, brevity = _titleRank(row)
            return (named, bool(row["play_count"]), plain, brevity,
                    row["play_count"], -(row["created_at"] or 0), row["id"])
        return max(candidates, key=rank)

    def resolveCanonicalTrackId(self, trackId: str) -> str:
        """The track a merged id should be read as, or the id itself.

        Its own id for anything unmerged AND for anything unknown - a caller
        that is about to 404 on a bad id must still get that 404 rather than a
        None it has to special-case."""
        row = self._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()
        return (row["canonical_id"] if row and row["canonical_id"] else trackId)

    def getMergedReleases(self, trackId: str) -> list[dict]:
        """The OTHER releases carrying this same recording - a single where the
        page is showing the album cut, a compilation, and so on.

        What makes the merge legible. Once the global lists count a song once,
        its play count silently spans releases, and a total that spans something
        invisible is indistinguishable from a wrong one. This is the page's
        answer to "where did that number come from".

        Takes either end of a merge: hand it a merged id and it answers about
        the canonical, so a caller does not have to know which it is holding.
        The canonical itself is not in the result - the page is already showing
        it."""
        canonicalId = self.resolveCanonicalTrackId(trackId)
        rows = self._conn().execute(
            """
            SELECT t.id, t.name, t.url, t.duration_ms, t.isrc,
                   al.id AS album_id, al.name AS album_name, al.url AS album_url,
                   al.image_id AS album_image_id, al.release_date AS album_release_date
            FROM tracks t
            LEFT JOIN albums al ON al.id = t.album_id
            WHERE t.canonical_id = ?
            ORDER BY al.release_date, al.name COLLATE NOCASE, t.id
            """,
            (canonicalId,),
        ).fetchall()
        return [
            {
                "trackId": row["id"],
                "name": row["name"],
                "url": row["url"],
                "duration": row["duration_ms"],
                "isrc": row["isrc"] or "",
                "album": {
                    "id": row["album_id"],
                    "name": row["album_name"],
                    "url": row["album_url"],
                    "imageId": row["album_image_id"],
                    "releaseDate": row["album_release_date"],
                },
            }
            for row in rows
        ]

    def mergeTracksByIsrc(self, *, enableSetting: bool = False) -> dict:
        """Point every track that shares an ISRC with another at one canonical.

        An ISRC identifies a RECORDING, so two tracks carrying the same one are
        the same performance released twice - a single and an album, an album
        and a compilation. That is a fact rather than an inference, which is why
        this tier merges on its own where a title-similarity tier would have to
        ask. It also means the two things this feature was told NOT to do come
        free: a remaster is a new master with its own ISRC, and so is a mono
        mix, so neither can be merged into the original by accident.

        Global, because sameness is a property of the recording rather than of
        one listener - which is also why the election counts plays across every
        account rather than the caller's.

        Three rules keep it from overreaching, and they are the whole reason
        this is more than a GROUP BY:

        1. A manual PLACEMENT outranks it. A person who split a track out
           (manual-split) overruled this very ISRC and an automatic pass that
           re-merged it would make the disagreement invisible; a manual-merge
           placed it somewhere on purpose and is not poached. A review-queue
           dismissal (manual-reject) is the one manual row that yields: it
           judged a title-similarity guess on a pair that HAD no shared ISRC -
           the queue never proposes one that does - so a shared ISRC arriving
           later is the fact the guess stood in for, and fact wins. The
           override resets decided_by, keeping it toggle-revertible.
        2. An existing canonical is STICKY - against PLAYS, which is what it
           was written against: re-electing on them every pass would move the
           canonical, and every link to it, as counts drift. Titles do not
           drift, so a head beaten on _titleRank alone by its own group steps
           down once and settles there. Without that, every group merged
           before the title rule existed keeps a song page reading "- 2005
           Remaster", since a merged group is otherwise never re-elected.
        3. Never a chain, and all three halves of that are guarded. The
           canonical's half is an invariant rather than a step in the ordinary
           case: it is only ever a track with no pointer of its own - either
           elected from members that all had none, or the single value already
           agreed on, and if THAT track pointed anywhere its target would be a
           second value in `existing` and the group would have been skipped by
           the conflict branch. (An earlier version NULLed the canonical's own
           pointer "to be safe"; mutation testing showed the line could not be
           reached, which is how the invariant got noticed at all. Rule 2's
           title move is what finally reaches it - a promoted track was a
           member a moment ago - so the clear is back, guarded by the plan
           saying a move happened rather than by hoping.) The MEMBERS' half is
           a step: a member can itself be a manual merge's anchor - the planner
           cannot see that, since the hand-merged track carries no ISRC - so
           pointing the member away carries its dependents along, exactly as
           mergeTrackManually does. The old HEAD's half is the same step: it
           becomes an ordinary member, and everything that pointed at it rides
           along on the one write.

        Idempotent: a second run merges nothing and rewrites nothing. Returns
        {"groups", "merged"} - groups considered, tracks newly pointed.

        enableSetting is the admin toggle's activation edge. It writes the
        enabled flag and daily-run stamp in the same transaction as the merge,
        so neither state can survive without the other when any write fails."""
        plan = self._planIsrcMerges(includeGuardState=True)
        conn = self._conn()
        merged = 0
        now = time.time()
        canonicals = []

        def writeActivationSettings():
            for key, value in (
                    (TRACK_MERGE_SETTING_KEY, APP_SETTING_TRUE),
                    (TRACK_MERGE_LAST_RUN_KEY, str(now))):
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

        if not plan["groups"]:
            if enableSetting:
                with conn:
                    writeActivationSettings()
            return {"groups": 0, "merged": 0}

        with conn:
            #< BEGIN IMMEDIATE: the carry-along below re-reads each member's
            #  dependents mid-surgery and re-points what it finds; unlocked,
            #  those reads run in autocommit, and a manual merge re-pointing a
            #  dependent between read and write strands it one hop from its
            #  group. The PLAN above stays outside on purpose: the run is
            #  single-flighted (claimTrackMergeRun). Revalidate the original
            #  pointer snapshot and manual pins before applying its decisions.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            plannedIds = list({trackId for group in plan["groups"]
                               for trackId in group["expectedPointers"]})
            currentPointers = {}
            pinned = set()
            for offset in range(0, len(plannedIds), MERGE_GUARD_QUERY_BATCH_SIZE):
                batch = plannedIds[offset:offset + MERGE_GUARD_QUERY_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                for row in conn.execute(
                        "SELECT t.id, t.canonical_id, d.decided_by, d.reason FROM tracks t "
                        "LEFT JOIN track_merge_decisions d ON d.track_id=t.id "
                        f"WHERE t.id IN ({placeholders})", batch):
                    currentPointers[row["id"]] = row["canonical_id"]
                    if row["decided_by"] is not None and row["reason"] != "manual-reject":
                        pinned.add(row["id"])
            for group in plan["groups"]:
                canonicalId = group["canonical"]["trackId"]
                expected = group["expectedPointers"]
                changed = {trackId for trackId, pointer in expected.items()
                           if trackId in pinned or trackId not in currentPointers
                           or currentPointers[trackId] != pointer}
                if canonicalId in changed or group["reHeadedFrom"] in changed:
                    continue
                members = [member for member in group["members"] if member["trackId"] not in changed]
                if not members:
                    continue
                canonicals.append(canonicalId)
                if group["reHeadedFrom"]:
                    #< the one canonical that arrives pointing somewhere: it was
                    #  a member of this very group until the title rule promoted
                    #  it. BEFORE anything else, or the carry-along below reads
                    #  it as a dependent of the head it is replacing and points
                    #  it at itself. Its decision row goes with the pointer - a
                    #  canonical was not merged, it is what the others were
                    #  merged into - but only if the matcher wrote it: a
                    #  person's manual-reject is a verdict about a different
                    #  question (see rule 1) and is not this run's to discard.
                    conn.execute("UPDATE tracks SET canonical_id=NULL WHERE id=?",
                                 (canonicalId,))
                    currentPointers[canonicalId] = None
                    conn.execute("DELETE FROM track_merge_decisions "
                                 "WHERE track_id=? AND decided_by IS NULL", (canonicalId,))
                for member in members:
                    #< a member can be a manual merge's own anchor (the planner
                    #  only sees ISRC-carrying tracks, so a hand-merged remaster
                    #  pointing at this member is invisible to it). Its
                    #  dependents move too, audit rows included, or the
                    #  member's new pointer strands them one hop away - the
                    #  same carry-along mergeTrackManually does, because every
                    #  reader resolves exactly one hop. Not counted in
                    #  `merged`: already collapsed, merely re-homed - which
                    #  also keeps the preview's number equal to the run's.
                    for row in conn.execute(
                            "SELECT id FROM tracks WHERE canonical_id = ?",
                            (member["trackId"],)).fetchall():
                        conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?",
                                     (canonicalId, row["id"]))
                        currentPointers[row["id"]] = canonicalId
                        #< a MATCHER row's target is the matcher's to rewrite; a
                        #  PERSON's is not. Overwriting it in place destroyed the
                        #  release they chose (track_id is the PK - there is no
                        #  history), leaving a row that named this run's head and
                        #  still credited them for it, and left the toggle's off
                        #  edge with nothing to restore. The pointer above still
                        #  moves either way: readers resolve exactly one hop.
                        conn.execute(
                            """
                            UPDATE track_merge_decisions
                            SET canonical_id = CASE WHEN decided_by IS NULL
                                                    THEN ? ELSE canonical_id END,
                                carried_canonical_id = CASE WHEN decided_by IS NULL
                                                            THEN NULL ELSE ? END
                            WHERE track_id = ?
                            """,
                            (canonicalId, canonicalId, row["id"]))
                    conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?",
                                 (canonicalId, member["trackId"]))
                    # A later group can share this destination through an
                    # older pointer. Revalidate against our own writes too.
                    currentPointers[member["trackId"]] = canonicalId
                    #< decided_by=NULL in the UPDATE arm too: the one manual
                    #  row this can land on is a manual-reject (rule 1 keeps
                    #  every other kind out of `members`), and the override
                    #  must become a plain matcher row - left as the person's,
                    #  it would dodge unmergeAllIsrcMerges and strand the
                    #  merge past the toggle's off edge
                    conn.execute(
                        """
                        INSERT INTO track_merge_decisions
                            (track_id, canonical_id, reason, evidence, decided_at, decided_by)
                        VALUES (?, ?, 'isrc', ?, ?, NULL)
                        ON CONFLICT(track_id) DO UPDATE SET
                            canonical_id=excluded.canonical_id, reason=excluded.reason,
                            evidence=excluded.evidence, decided_at=excluded.decided_at,
                            decided_by=NULL, against_id=NULL, carried_canonical_id=NULL
                        """,
                        (member["trackId"], canonicalId, group["isrc"], now),
                    )
                    merged += 1
                #< after the members, so the group it tests against is the one
                #  this run just made
                self._clearContradictedRejection(conn, canonicalId)
                #< unconditional is safe HERE, and only here: the planner drops
                #  a group with nothing to merge (`if not toMerge: continue`),
                #  so every group that reaches this loop is one that MOVED. That
                #  is what _requeueCanonicalForGenres requires - called on a
                #  settled group it would re-look-up a canonical that is
                #  genuinely tag-less everywhere on every daily pass, forever,
                #  since such a canonical can never satisfy the own-rows test.
                self._requeueCanonicalForGenres(conn, canonicalId)
            if enableSetting:
                writeActivationSettings()
            if merged:
                # Expand the groups after the writes, while still holding the
                # transaction lock. Merge changes, settings, generation, and
                # cache deletion must all commit or roll back together.
                self._deleteCachedWrappedForTracks(
                    conn, self._mergeGroupTrackIds(conn, canonicals))
        return {"groups": len(canonicals), "merged": merged}

    def previewMergeTracksByIsrc(self) -> dict:
        """What a run WOULD do, without doing it.

        Same planner, no writes. Concurrent manual verdicts can make a later
        run skip changed candidates. A merge is global and moves every account's
        numbers at once; being able to look first is what makes turning it on a
        decision rather than a leap."""
        return self._planIsrcMerges()

    def _planIsrcMerges(self, *, includeGuardState: bool = False) -> dict:
        """The groups a run would act on, and which track each would fold into.

        Split out so the preview and the run cannot drift: one of them being
        wrong is worse than neither existing."""
        conn = self._conn()
        # Only ISRCs that more than one track carries; a group of one is nothing
        # to decide. Fabricated ids are already excluded by getTracksMissingIsrc
        # never filling them, but the length test costs nothing and keeps this
        # honest if an ISRC ever arrives by another route.
        #< t.name is here for two readers: _electCanonical's title rule, and the
        #  group descriptions below. It used to be a second query over the same
        #  WHERE clause, which is one scan of the duplicate set per planner run.
        #  The backfiller's pass is down to one a day
        #  (TRACK_MERGE_MIN_INTERVAL_SECONDS), so that saving is smaller than it
        #  was - but the planner also backs previewMergeTracksByIsrc, which the
        #  admin page runs on demand and where a second scan is felt directly.
        rows = conn.execute(
            f"""
            SELECT t.id, t.name, t.isrc, t.canonical_id, t.created_at,
                   COALESCE(pc.play_count, 0) AS play_count
            FROM tracks t
            {self._PLAY_TALLY_JOIN}
            WHERE t.isrc IS NOT NULL AND t.isrc <> ''
              AND t.isrc IN (SELECT isrc FROM tracks
                             WHERE isrc IS NOT NULL AND isrc <> ''
                             GROUP BY isrc HAVING COUNT(*) > 1)
            """
        ).fetchall()
        if not rows:
            return {"groups": [], "merged": 0}

        #< manual-reject is deliberately NOT in this set. That verdict was
        #  reached in the review queue, which by construction only proposes
        #  pairs WITHOUT a shared ISRC - so it answered a title-similarity
        #  guess, not the fact. A shared ISRC arriving later IS the fact, and
        #  fact outranks guess (the merge below also resets decided_by, so the
        #  override stays an ordinary, toggle-revertible matcher row). A
        #  manual SPLIT is the opposite statement - a person overruling the
        #  ISRC itself - and stays pinned, manual-merge placements likewise.
        pinned = {row["track_id"] for row in conn.execute(
            "SELECT track_id FROM track_merge_decisions "
            "WHERE decided_by IS NOT NULL AND reason != 'manual-reject'")}
        #< a canonical named by an EXISTING pointer can sit outside the
        #  duplicate set entirely, which is why the lookups below tolerate a
        #  miss rather than indexing
        names = {row["id"]: row["name"] for row in rows}
        originalPointers = {row["id"]: row["canonical_id"] for row in rows}

        byIsrc = {}
        for row in rows:
            byIsrc.setdefault(row["isrc"], []).append(row)

        groups = []
        for isrc, members in byIsrc.items():
            #< rule 1: a pinned track takes no part at all - neither merged
            #  nor elected, since electing it would move others onto a track
            #  a person has said is not the same recording
            members = [m for m in members if m["id"] not in pinned]
            if len(members) < 2:
                continue

            #< rule 2: whatever this group already agreed on wins. Any member
            #  already pointing somewhere names the canonical; otherwise the
            #  election is _electCanonical's, shared with the review queue so
            #  the two tiers cannot disagree about which release survives
            existing = {m["canonical_id"] for m in members if m["canonical_id"]}
            reHeadedFrom = None
            if len(existing) == 1:
                canonicalId = existing.pop()
                #< rule 2's ONE exception, and the reason it is safe: sticky
                #  is written against DRIFT, and only the play half of the
                #  election drifts. A title does not - a release either carries
                #  a version marker or it does not, on every pass - so a head
                #  beaten on _titleRank alone can step down once and settle
                #  there. Without it every group merged before the title rule
                #  existed keeps a song page reading "- 2005 Remaster" forever,
                #  since a merged group is never re-elected. Only a head inside
                #  its own group may be moved: one outside it is either pinned
                #  (rule 1 says a pinned track takes no part) or points
                #  somewhere this planner cannot see.
                head = next((m for m in members if m["id"] == canonicalId), None)
                best = self._electCanonical(members)
                if head is not None and _titleRank(best) > _titleRank(head):
                    reHeadedFrom, canonicalId = canonicalId, best["id"]
            elif existing:
                #< two canonicals for one recording: a merge from before this
                #  ran, or a hand edit. Left alone rather than guessed at
                continue
            else:
                canonicalId = self._electCanonical(members)["id"]

            #< the old head's own members are left out of the list: pointing it
            #  at the new one carries them along (dependents move with their
            #  canonical, exactly as they do for a member), so listing them
            #  here would write them twice and count each as newly collapsed
            #  when nothing about their grouping changed. A set rather than a
            #  != : with no move, reHeadedFrom is None and so is an unmerged
            #  member's pointer, which would have excluded every one of them.
            carried = {reHeadedFrom} if reHeadedFrom else set()
            toMerge = [m for m in members
                       if m["id"] != canonicalId and m["canonical_id"] != canonicalId
                       and m["canonical_id"] not in carried]
            if not toMerge:
                continue   #< everything already points where it should
            groups.append({
                "isrc": isrc,
                "canonical": {"trackId": canonicalId, "name": names.get(canonicalId, "")},
                #< the run needs this to clear the promoted track's OWN pointer:
                #  it is the one canonical that arrives here pointing somewhere
                "reHeadedFrom": reHeadedFrom,
                "members": [{"trackId": m["id"], "name": names.get(m["id"], ""),
                             "plays": m["play_count"]} for m in toMerge],
                "plays": sum(m["play_count"] for m in toMerge),
            })
            if includeGuardState:
                # Members retain the pointers from the original planner rows.
                # A promoted head may be a member OR a newly arrived root.
                # Destinations outside the snapshot must remain roots.
                expected = {m["id"]: m["canonical_id"] for m in toMerge}
                expected[canonicalId] = originalPointers.get(canonicalId)
                groups[-1]["expectedPointers"] = expected

        #< biggest first, so the admin preview leads with what matters
        groups.sort(key=lambda g: -g["plays"])
        return {"groups": groups, "merged": sum(len(g["members"]) for g in groups)}

    @staticmethod
    def _mergeGroupTrackIds(conn, trackIds) -> list[str]:
        """Expand track ids to every track that shares a merge group with them.

        The scope deleteCachedWrappedForTracks needs, and it is wider than what
        the caller wrote for one reason: a group's discovery entry is anchored
        on its ALL-TIME first listen. A joining track whose own first play
        predates the group's drags that anchor backwards, which pulls the group
        out of the discovery lists of the year the old anchor fell in - a year
        that belongs to some member this run never touched, and whose own play
        count and max_played_at did not move. Built from the ids alone, the
        invalidation would miss it, and nothing downstream ever would.

        Resolves each id to its root - itself, when it points nowhere - then
        collects everything pointing at those roots. One hop each way is
        enough: a canonical never points anywhere itself (see the "never a
        chain" rule in mergeTracksByIsrc), so root-of-root is root.

        Call it BEFORE the write when a group is dissolving (unmergeTrack) and
        AFTER when one is forming, so it sees the membership that changed."""
        ids = list(dict.fromkeys(trackIds))
        if not ids:
            return []
        roots = {row["canonical_id"] or row["id"] for row in conn.execute(
            "SELECT id, canonical_id FROM tracks WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),))}
        group = set(ids) | roots
        if roots:
            group.update(row["id"] for row in conn.execute(
                "SELECT id FROM tracks WHERE canonical_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(roots)),)))
        return sorted(group)

    def unmergeTrack(self, trackId: str, decidedBy: str) -> None:
        """Take one track back out of its merge, and KEEP it out.

        Both halves matter: clearing canonical_id alone would last exactly until
        the next matcher pass re-merged it. The decision row flips to a manual
        "not the same recording" verdict - decided_by names who - which is
        precisely the row the matcher refuses to overrule.

        Raises ValueError for an unknown track, like its siblings - without
        the check the decision INSERT hits the tracks(id) FOREIGN KEY
        instead, which the route cannot turn into a 400."""
        conn = self._conn()
        with conn:
            #< BEGIN IMMEDIATE: the existence check and the group snapshot
            #  decide the split and its invalidation scope; unlocked they can
            #  capture a membership a concurrent merge changes before the
            #  UPDATE lands, leaving the years the OLD group appeared in
            #  uninvalidated. Same remedy as mergeTrackManually.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (trackId,)).fetchone():
                raise ValueError(f"unknown track: {trackId}")
            #< BEFORE the split: the group this track is leaving is what moves, and
            #  a moment from now its membership no longer says who was in it
            group = self._mergeGroupTrackIds(conn, [trackId])
            conn.execute("UPDATE tracks SET canonical_id=NULL WHERE id=?", (trackId,))
            conn.execute(
                """
                INSERT INTO track_merge_decisions
                    (track_id, canonical_id, reason, evidence, decided_at, decided_by)
                VALUES (?, NULL, 'manual-split', NULL, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    canonical_id=NULL, reason='manual-split', evidence=NULL,
                    decided_at=excluded.decided_at, decided_by=excluded.decided_by,
                    against_id=NULL, carried_canonical_id=NULL
                """,
                (trackId, time.time(), decidedBy),
            )
            # Both sides of the split move, so invalidate the old group in the
            # same transaction as the pointer and manual verdict.
            self._deleteCachedWrappedForTracks(conn, group)

    def unmergeAllIsrcMerges(self, *, disableSetting: bool = False) -> int:
        """Undo everything the MATCHER did, leaving every human verdict alone.

        The full-revert half of reversibility. Nothing a merge writes is
        destructive - canonical_id is a pointer and the decision rows are
        additive - so undoing a run is clearing them, not restoring a backup.
        Manual rows (decided_by set) survive: a revert means "undo what the
        matcher did", not "forget what anyone decided". Returns rows cleared.

        Surviving is not the same as being restored, which is what
        carried_canonical_id fixes: a manual merge the matcher re-homed points
        at the matcher's head, and leaving it there keeps a piece of the
        matcher's work standing past the off edge. Those go back to the release
        the person picked. disableSetting atomically turns the admin toggle
        off with the revert; invalid topology leaves both unchanged."""
        conn = self._conn()
        with conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            pointers = {row["id"]: row["canonical_id"] for row in conn.execute(
                "SELECT id, canonical_id FROM tracks")}
            decisions = conn.execute(
                "SELECT track_id, canonical_id, reason, decided_by, carried_canonical_id "
                "FROM track_merge_decisions").fetchall()
            carried = {}
            for decision in decisions:
                trackId = decision["track_id"]
                if decision["decided_by"] is None and decision["reason"] == "isrc":
                    pointers[trackId] = None
                elif decision["carried_canonical_id"] is not None:
                    carried[trackId] = decision["canonical_id"]
                    pointers[trackId] = decision["canonical_id"]
            # Resolve against ALL final edges before writing any restoration.
            # A carried target may itself be restored later in row order.
            restoredPointers = {}
            for trackId, target in carried.items():
                seen = {trackId}
                while target is not None:
                    if target in seen or target not in pointers:
                        raise ValueError("Cannot restore manual merges: invalid canonical topology")
                    seen.add(target)
                    nextTarget = pointers[target]
                    if nextTarget is None:
                        break
                    target = nextTarget
                restoredPointers[trackId] = target
            cur = conn.execute(
                """
                UPDATE tracks SET canonical_id=NULL WHERE id IN (
                    SELECT track_id FROM track_merge_decisions
                    WHERE decided_by IS NULL AND reason='isrc'
                )
                """
            )
            conn.execute(
                "DELETE FROM track_merge_decisions WHERE decided_by IS NULL AND reason='isrc'")
            conn.executemany("UPDATE tracks SET canonical_id=? WHERE id=?",
                             [(target, trackId) for trackId, target in restoredPointers.items()])
            restored = len(restoredPointers)
            conn.execute("UPDATE track_merge_decisions SET carried_canonical_id=NULL "
                         "WHERE carried_canonical_id IS NOT NULL")
            if disableSetting:
                conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (TRACK_MERGE_SETTING_KEY, APP_SETTING_FALSE))
            if cur.rowcount or restored:
                # Either arm changes cached years. Invalidate before committing
                # the revert and optional setting change. Restored tracks are
                # not returned: they moved into a merge, not out of one.
                self._deleteAllWrapped(conn)
        return cur.rowcount

    def getMergeReviewCandidates(self) -> dict:
        """The title-similarity tier that ASKS: groups a person should look at.

        The ISRC matcher merges on fact and was told not to touch remasters -
        each is a new master with its own ISRC. This surfaces what that leaves:
        same normalized title (see normalizeTrackTitle), same primary artist,
        durations agreeing within MERGE_REVIEW_DURATION_TOLERANCE_MS. Nothing
        here writes; the two verdicts live in mergeTrackManually and
        dismissMergeCandidate.

        What it refuses to propose, and why:
        - a pinned track (any decided_by row): a person already answered, in
          either direction, and re-asking makes the queue a nag;
        - a track merged into some OTHER group: decided - the sticky rule
          says decisions don't drift, so it is not poached back;
        - a pair sharing a non-empty ISRC: same master, the automatic tier's
          case - its checkbox preview already lists it, and a question with a
          known answer doesn't belong in a review queue.

        The anchor each group proposes merging INTO comes from the ISRC tier's
        own election (_electCanonical), applied to the group's unmerged
        members - the same function rather than the same description, so the
        two tiers cannot disagree about which release survives. Returns
        {"groups" (capped at
        MERGE_REVIEW_PAGE_LIMIT, biggest plays first), "totalGroups",
        "totalMembers"} - totals count the whole queue, not the page."""
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT t.id, t.name, t.duration_ms, t.canonical_id, t.isrc,
                   t.created_at, al.name AS album_name, ar.name AS artist_name,
                   COALESCE(pc.play_count, 0) AS play_count
            FROM tracks t
            JOIN albums al ON al.id = t.album_id
            LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            LEFT JOIN artists ar ON ar.id = ta.artist_id
            {self._PLAY_TALLY_JOIN}
            WHERE t.name IS NOT NULL AND t.name <> ''
              AND t.id NOT IN (SELECT track_id FROM track_merge_decisions
                               WHERE decided_by IS NOT NULL)
            """
        ).fetchall()

        byKey = {}
        for row in rows:
            key = (normalizeTrackTitle(row["name"]),
                   (row["artist_name"] or "").strip().lower())
            byKey.setdefault(key, []).append(row)

        def _describe(row):
            return {"trackId": row["id"], "name": row["name"],
                    "album": row["album_name"], "plays": row["play_count"],
                    "durationMs": row["duration_ms"] or 0,
                    "isrc": row["isrc"] or ""}

        groups = []
        for members in byKey.values():
            if len(members) < 2:
                continue
            #< elected among UNMERGED members only, so the target is always a
            #  genuine endpoint - a merged member's group already has a head,
            #  and pointing at a member would be the chain no reader may walk
            unmerged = [m for m in members if not m["canonical_id"]]
            if not unmerged:
                continue
            anchor = self._electCanonical(unmerged)
            anchorDuration = anchor["duration_ms"] or 0
            toMerge = []
            for m in unmerged:
                if m["id"] == anchor["id"]:
                    continue
                duration = m["duration_ms"] or 0
                if (duration and anchorDuration
                        and abs(duration - anchorDuration) > MERGE_REVIEW_DURATION_TOLERANCE_MS):
                    continue
                if m["isrc"] and anchor["isrc"] and m["isrc"] == anchor["isrc"]:
                    continue
                toMerge.append(m)
            if not toMerge:
                continue
            groups.append({
                "canonical": _describe(anchor),
                "members": [_describe(m) for m in toMerge],
                "plays": sum(m["play_count"] for m in toMerge),
            })

        #< biggest first, same as the ISRC preview - the page leads with what
        #  moves the most numbers
        groups.sort(key=lambda g: -g["plays"])
        return {
            "groups": groups[:MERGE_REVIEW_PAGE_LIMIT],
            "totalGroups": len(groups),
            "totalMembers": sum(len(g["members"]) for g in groups),
        }

    def mergeTrackManually(self, trackId: str, canonicalId: str, decidedBy: str) -> int:
        """A person's "same recording": point trackId at canonicalId's group.

        The decision row (reason 'manual-merge', decided_by set) is the pinned
        kind the automatic tier refuses to overrule in EITHER direction: no
        later ISRC pass re-elects it away, and the toggle's off edge
        (unmergeAllIsrcMerges) leaves it standing.

        Two shapes the caller never has to think about:
        - the target resolves first, so merging into a MEMBER of a group
          lands on that group's canonical rather than creating a chain;
        - a track that is itself a canonical brings its members along, and
          their audit rows move with them - the no-chain invariant holds by
          construction, not by hoping.

        Idempotent for a track already in the target group (returns 0 and
        drops no caches). Returns tracks newly pointed, dependents included.
        Raises ValueError for an unknown track on either side."""
        conn = self._conn()
        now = time.time()
        merged = 0
        with conn:
            #< BEGIN IMMEDIATE: the resolve, the existence checks and the
            #  leaving-group snapshot are decisions about what the writes below
            #  make true; under legacy transaction control they would run in
            #  autocommit holding no lock, and a matcher pass re-heading `root`
            #  between resolve and write leaves trackId pointing at a track
            #  that itself now points away - a chain, which every reader
            #  resolves exactly one hop of. Same remedy as
            #  dismissMergeCandidate and saveCachedWrapped.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            root = self.resolveCanonicalTrackId(canonicalId)
            known = {row["id"] for row in conn.execute(
                "SELECT id FROM tracks WHERE id IN (?, ?)", (trackId, root))}
            if trackId not in known:
                raise ValueError(f"unknown track: {trackId}")
            if root not in known:
                raise ValueError(f"unknown track: {root}")
            current = conn.execute(
                "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()
            if trackId == root or current["canonical_id"] == root:
                return 0

            #< BEFORE the write, and the reason unmergeTrack takes the same
            #  snapshot: this is the one verb that can DISSOLVE a group and form
            #  one in a single call. When trackId is a member of some other group,
            #  that group's head stays behind and loses a listen - which moves its
            #  all-time discovery anchor - while the post-write expansion below can
            #  only ever see the group trackId JOINED. In the ordinary case (an
            #  unmerged track) this is trackId and its dependents, every one of
            #  which the write below moves into root's group anyway - so the union
            #  widens nothing and only the cross-group case pays for it.
            leaving = self._mergeGroupTrackIds(conn, [trackId])
            #< dependents first: re-pointing them is what keeps "a canonical
            #  never points anywhere itself" true through this merge
            dependents = [r["id"] for r in conn.execute(
                "SELECT id FROM tracks WHERE canonical_id = ?", (trackId,))]
            for depId in dependents:
                conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (root, depId))
                #< a person moving the group re-decides its dependents too, so
                #  their carry (if any) is abandoned - see the matcher's
                #  carry-along for why a stale one builds a chain on revert
                conn.execute(
                    "UPDATE track_merge_decisions SET canonical_id=?, "
                    "carried_canonical_id=NULL WHERE track_id=?",
                    (root, depId))
                merged += 1
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (root, trackId))
            conn.execute(
                """
                INSERT INTO track_merge_decisions
                    (track_id, canonical_id, reason, evidence, decided_at, decided_by)
                VALUES (?, ?, 'manual-merge', NULL, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    canonical_id=excluded.canonical_id, reason='manual-merge',
                    evidence=NULL, decided_at=excluded.decided_at,
                    decided_by=excluded.decided_by, against_id=NULL,
                    carried_canonical_id=NULL
                """,
                (trackId, root, now, decidedBy))
            merged += 1
            #< last, so it tests against the group this merge just made
            self._clearContradictedRejection(conn, root)
            #< the group moved (the early return above covers the no-op case),
            #  so the release every genre read now resolves to may never have
            #  been looked up - see _requeueCanonicalForGenres
            self._requeueCanonicalForGenres(conn, root)
            # Invalidate both the old and resulting groups before committing.
            # Expand after the writes so carried dependents are included;
            # sort for a stable JSON array bound into the DELETE.
            self._deleteCachedWrappedForTracks(
                conn, sorted(set(leaving) | set(self._mergeGroupTrackIds(conn, [root]))))
        return merged

    def dismissMergeCandidate(self, trackId: str, decidedBy: str,
                              againstId: str | None = None) -> None:
        """A person's "not the same recording", recorded so it STAYS answered.

        The row (NULL canonical_id, decided_by set) is what migrate1_48_0
        promised that shape would mean: a deliberate no, different from having
        no row (never looked at), and what stops a rejected pair being
        re-proposed by this queue forever. The ISRC matcher honours it only
        while the pair still has no shared ISRC: the no answered a
        title-similarity guess (this queue proposes nothing that shares one),
        so a shared ISRC arriving later outranks it - see mergeTracksByIsrc's
        rule 1. A split (unmergeTrack) is how a person overrules the ISRC
        itself, and THAT the matcher never touches.

        Unlike unmergeTrack this touches no pointer and moves no numbers, so
        it deliberately drops no Wrapped caches. Raises ValueError for an
        unknown track, and for a track that is currently MERGED: the queue
        only proposes unmerged members, so a dismissal arriving for a merged
        one was aimed at a state that no longer exists - the queue open in two
        tabs, or the matcher landing between render and click. Recording it
        anyway would leave the pointer standing under a row claiming "not the
        same recording": an audit that lies, one the toggle's off edge can
        never clear (decided_by is set), and one the reject-yields rule could
        flip straight back regardless. The "no" for a merged track is the
        split (unmergeTrack), which pins - the route explains exactly that.

        againstId records WHICH release was on screen when the verdict was
        reached - the one keeping the song's page. Audit only: the row still
        judges trackId, which leaves the queue whatever it was compared with,
        and no read path decides anything from it. It is what lets the "Kept
        separate" log say not the same as WHAT, months later when the pair
        that made it obvious is gone from the queue and out of memory.
        Optional, because every row written before the column existed has no
        answer and a guess would be worse than the blank. An empty string is
        the same as absent - a form field nobody filled - but an id naming no
        track is refused, since letting it through raises a FOREIGN KEY the
        route cannot turn into a 400."""
        againstId = againstId or None
        conn = self._conn()
        with conn:
            #< BEGIN IMMEDIATE, so the state it checks really is the state the
            #  row lands in. `with conn:` alone does not give that: sqlite3
            #  under legacy transaction control opens a transaction for DML
            #  only, so these SELECTs would run in autocommit holding no lock,
            #  and a matcher pass could merge trackId between the check and the
            #  INSERT - landing the merged-track row this very guard refuses,
            #  which nothing downstream can clear (decided_by is set, so
            #  unmergeAllIsrcMerges spares it, and _clearContradictedRejection
            #  only fires on the merge side). See saveCachedWrapped for the
            #  same remedy against the same mechanism.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()
            if not row:
                raise ValueError(f"unknown track: {trackId}")
            if row["canonical_id"]:
                raise ValueError(f"track is merged: {trackId}")
            if againstId == trackId:
                #< nothing on the page can post it (the row keeping the song's
                #  page carries no verdict buttons), and a log entry reading
                #  "X, not the same as X" is unre-checkable by construction
                raise ValueError(f"track cannot be rejected against itself: {trackId}")
            if againstId:
                against = conn.execute(
                    "SELECT canonical_id FROM tracks WHERE id=?", (againstId,)).fetchone()
                if not against:
                    raise ValueError(f"unknown track: {againstId}")
                if against["canonical_id"] == trackId:
                    #< the counterpart is already IN this track's group, so the
                    #  verdict would be born contradicted - the same state
                    #  _clearContradictedRejection deletes on the way in. The
                    #  queue cannot post it (it proposes only unmerged pairs);
                    #  refusing here is what makes the contradiction
                    #  unrepresentable from BOTH ends rather than just cleaned
                    #  up from one.
                    raise ValueError(
                        f"already merged into {trackId}: {againstId}")
            conn.execute(
                """
                INSERT INTO track_merge_decisions
                    (track_id, canonical_id, reason, evidence, decided_at, decided_by,
                     against_id)
                VALUES (?, NULL, 'manual-reject', NULL, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    canonical_id=NULL, reason='manual-reject', evidence=NULL,
                    decided_at=excluded.decided_at, decided_by=excluded.decided_by,
                    against_id=excluded.against_id
                """,
                (trackId, time.time(), decidedBy, againstId))

    def getDismissedMergeCandidates(self) -> dict:
        """Every "not the same recording" a person has recorded, newest first.

        The dismissal is the only verdict with nowhere to look at it. A merge
        shows on the song's page and can be split from there; a split shows as
        the song standing on its own. A "no" just makes a pair vanish out of
        the review queue - deliberately, so the queue stops nagging - which
        also means the page offered no way to find one again, and a decision
        you cannot find is one you cannot change your mind about.

        Reads the human "no" rows only (reason 'manual-reject'), and every
        admin's - a merge is instance-wide, so the log of what was kept apart
        is too; decidedBy names who on each row. A dismissal that a later
        shared ISRC overruled is no longer one: mergeTracksByIsrc rewrites it
        to an ordinary matcher merge, and that is undone by the toggle or a
        split, not from here.

        "against" is what the release was ruled against - the one keeping the
        song's page at the time - and is None where the row does not say: it
        is an audit column added after the verdict existed (migrate1_49_0), so
        every rejection recorded before it names nothing rather than a guess.
        LEFT JOIN for the same reason, plus the counterpart being a track row
        like any other. Note what the column does NOT do: the verdict still
        applies to the track, which is not re-proposed against anything with
        that title, so this narrows nothing.

        Returns {"entries" (capped at MERGE_DISMISSED_PAGE_LIMIT), "total"} -
        the total counts the whole list, not the page."""
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.duration_ms, d.decided_at, d.decided_by,
                   al.name AS album_name,
                   ag.id AS against_id, ag.name AS against_name,
                   agal.name AS against_album
            FROM track_merge_decisions d
            JOIN tracks t ON t.id = d.track_id
            LEFT JOIN albums al ON al.id = t.album_id
            LEFT JOIN tracks ag ON ag.id = d.against_id
            LEFT JOIN albums agal ON agal.id = ag.album_id
            WHERE d.reason = 'manual-reject'
            ORDER BY d.decided_at DESC, t.id
            """
        ).fetchall()
        page = rows[:MERGE_DISMISSED_PAGE_LIMIT]
        #< the tally is a SECOND query rather than _PLAY_TALLY_JOIN's subquery,
        #  because that one groups the whole plays table before the join can
        #  discard it - a full scan on every render of this page, paid even
        #  when nobody has ever dismissed anything. Here the common case costs
        #  nothing at all, and the rest bounds itself: at most
        #  MERGE_DISMISSED_PAGE_LIMIT ids, nowhere near SQLite's param ceiling.
        plays = {}
        if page:
            placeholders = ",".join("?" for _ in page)
            plays = dict(conn.execute(
                f"SELECT track_id, COUNT(*) FROM plays WHERE track_id IN "
                f"({placeholders}) GROUP BY track_id",
                [row["id"] for row in page]))
        return {
            "entries": [{"trackId": row["id"], "name": row["name"],
                         "album": row["album_name"], "plays": plays.get(row["id"], 0),
                         "durationMs": row["duration_ms"] or 0,
                         "decidedAt": row["decided_at"],
                         "decidedBy": row["decided_by"],
                         "against": ({"trackId": row["against_id"],
                                      "name": row["against_name"],
                                      "album": row["against_album"]}
                                     if row["against_id"] else None)}
                        for row in page],
            "total": len(rows),
        }

    def undismissMergeCandidate(self, trackId: str) -> None:
        """Take back a "not the same recording": the pair can be asked again.

        Deleting the row rather than writing a third verdict is the point. The
        queue skips anything a person has answered, so the state that lets it
        propose the pair again is the one where nobody has - there is no row
        meaning "un-rejected", and inventing one would be a decision the
        matcher's rules have never heard of.

        Refuses anything that is not a dismissal. The four verdicts differ by
        one column each, so an unfiltered DELETE here would silently unpin a
        merge or a split - the two rows that exist precisely to survive every
        automatic pass - or erase a matcher decision the toggle owns. Raises
        ValueError for those, and for a track nobody dismissed.

        No pointer moves and no play changes group, so this drops no Wrapped
        caches, exactly like the dismissal it undoes."""
        conn = self._conn()
        with conn:
            #< inside the write's own transaction: the row it checks is the row
            #  it deletes, so a matcher pass landing between the two cannot
            #  turn this into the deletion of a merge
            cur = conn.execute(
                "DELETE FROM track_merge_decisions "
                "WHERE track_id=? AND reason='manual-reject'", (trackId,))
            if not cur.rowcount:
                raise ValueError(f"not a dismissed track: {trackId}")
