"""The /admin page: every admin-only setting/view relocated off /overview -
the full users table (with per-account admin promote/demote), the 8 feature/
backfill toggles split into 3 forms (user, Last.fm, Spotify), and the
read-only Instance Insights section."""
import contextlib
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase
from Database.db import SPOTIFY_TRACK_ID_LENGTH
from Database.utils import timeToInt

_INSIGHTS_PATCHES = {
    "getCatalogGenreCoverage": {
        "song": {"covered": 0, "total": 0, "percent": 0.0},
        "album": {"covered": 0, "total": 0, "percent": 0.0},
        "artist": {"covered": 0, "total": 0, "percent": 0.0},
        "overall": {"percent": 0.0},
    },
    "getCatalogBiographyCoverage": {
        "artist": {"covered": 0, "total": 0}, "album": {"covered": 0, "total": 0},
    },
    "getRecentRegistrationCounts": {"last_7_days": 0, "last_30_days": 0},
    "getInstanceShareCounts": {"pending": 0, "accepted": 0},
    "getActiveShareLinksCount": 0,
}


# Bound for waits that should return immediately in a correct run - they exist
# to turn a hang into a failure, not to give slow machines "enough" time. No
# assertion here depends on how long anything takes: a blocked worker is held
# on an unbounded Event that the test releases in a finally, so nothing can
# expire early under load (the suite runs under xdist by default).
HANG_TIMEOUT_SECONDS = 10


class AdminRouteTestBase(AppTestCase):
    _MOCK_STATS = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100,
                   "total_time_ms": 36000000, "db_size_bytes": 1048576}

    _MOCK_USERS = [
        {
            "username": "alice", "email": "alice@example.com",
            "cookies_json": '{"sp_dc": "123"}',
            "spotify_client_id": "client_id", "spotify_refresh_token": "refresh_token",
            "lastfm_api_key": "enc:v1:something",
            "created_at": 1718000000.0, "is_admin": True,
        },
        {
            "username": "bob", "email": "bob@example.com",
            "cookies_json": '{"sp_dc": "456"}',
            "spotify_client_id": None, "spotify_refresh_token": None,
            "lastfm_api_key": None,
            "created_at": 1718000001.0, "is_admin": False,
        },
    ]

    def _makeDb(self):
        db = MagicMock()
        db.getListenerHealth.return_value = {"status": "HEALTHY", "error_count": 0,
                                             "last_error": None, "seconds_since_last_poll": 5}
        db.getLastfmWorkerStatus.return_value = {"configured": True, "running": True}
        return db

    def _patches(self, dash, isAdmin, users=None, loggedIn=True, extraInsights=None, userDb=None):
        insights = dict(_INSIGHTS_PATCHES)
        if extraInsights:
            insights.update(extraInsights)
        effectiveUsers = self._MOCK_USERS if users is None else users
        # adminPage()'s per-user row now reads dashboard.user_databases (an
        # already-active session) instead of calling get_user_db() - populate
        # it here to simulate every configured user already having a live
        # session, matching this fixture's previous (pre-fix) behavior where
        # get_user_db() was called unconditionally for them.
        dash.user_databases = {
            u["username"]: userDb or self._makeDb()
            for u in effectiveUsers
            if u.get("cookies_json") or u.get("lastfm_api_key")
        }
        patches = [
            patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=self._MOCK_STATS),
            patch.object(dash.repo, 'getAllUsersDetails', return_value=effectiveUsers),
            patch.object(dash.repo, 'isAdmin', return_value=isAdmin),
            patch.object(dash.repo, 'getPlayAndSkipCountsByUser',
                         return_value={u["username"]: {"plays": 123, "skips": 7} for u in effectiveUsers}),
            # Empty by default so every existing test stays explicit about not
            # exercising the live-miss ratio - an unpatched call would run real
            # SQL against the empty test db and silently yield nothing, which
            # is indistinguishable from "patched to empty" until a test cares.
            patch.object(dash.repo, 'getPlaySourceCountsByUser', return_value={}),
            patch.object(dash.repo, 'getAdminUsernames', return_value=['alice']),
            patch.object(dash, 'is_user_logged_in', return_value=loggedIn),
            patch.object(dash, 'get_username_for_email', return_value='alice'),
            # Still needed: get_current_user_or_redirect() calls this once for
            # the acting admin's own session (e.g. to resolve db.tz) - unrelated
            # to the per-row lookup above.
            patch.object(dash, 'get_user_db', return_value=userDb or self._makeDb()),
        ]
        for name, value in insights.items():
            patches.append(patch.object(dash.repo, name, return_value=value))
        return patches

    def _getAdmin(self, dash, isAdmin=True, users=None, loggedIn=True, extraInsights=None, patches=None, path="/admin"):
        patches = patches if patches is not None else self._patches(
            dash, isAdmin, users=users, loggedIn=loggedIn, extraInsights=extraInsights)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
            return client.get(path)

    def _post(self, dash, path, isAdmin, data, loggedIn=True):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post(path, data=data)


class TestAdminPageAuthGate(AdminRouteTestBase):
    def test_anonymous_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_non_admin_gets_403(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=False)
        self.assertEqual(resp.status_code, 403)

    def test_admin_gets_200(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Registered Users & Sync Status", resp.data)

    def test_admin_message_card_sizing(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True, path="/admin?message=Test+email+successfully+sent+to+admin@example.com.")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Test email successfully sent to admin@example.com.", resp.data)
        html = resp.data.decode("utf-8")
        self.assertIn('min-height: auto;', html)
        self.assertIn('width: fit-content;', html)


class TestAdminUsersTable(AdminRouteTestBase):
    def test_shows_every_user(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        self.assertIn(b"alice", resp.data)
        self.assertIn(b"bob", resp.data)
        self.assertIn(b"HEALTHY", resp.data)
        self.assertIn(b"123", resp.data)

    def test_a_display_name_renders_under_the_username(self):
        """admin.html carries a sub-line for u.display_name, but the route
        rebuilt every row as an explicit key literal and left the column out,
        so the markup was dead on every render. Jinja's default Undefined is
        falsy, so the omission showed as nothing at all rather than as a 500 -
        which is why only a page-level assertion catches it. The username stays
        on the row beside it: that is the immutable account key admin acts on."""
        users = [dict(self._MOCK_USERS[0], display_name="Alice Anderson"),
                 dict(self._MOCK_USERS[1], display_name=None)]
        dash = self._makeApp()

        resp = self._getAdmin(dash, isAdmin=True, users=users)

        self.assertIn(b"Alice Anderson", resp.data)
        self.assertIn(b"alice", resp.data)

    def test_only_the_user_with_a_display_name_gets_the_sub_line(self):
        """NULL display_name is every account's starting state, so the sub-line
        must be absent for it - but "absent" is also what a BROKEN route
        produces for everyone, so asserting absence alone pins nothing (the
        first version of this test passed with the fix reverted). The
        discriminator is that exactly one of two rows has the sub-line."""
        users = [dict(self._MOCK_USERS[0], display_name="Alice Anderson"),
                 dict(self._MOCK_USERS[1], display_name=None)]
        dash = self._makeApp()

        resp = self._getAdmin(dash, isAdmin=True, users=users)

        import bs4
        soup = bs4.BeautifulSoup(resp.data, "html.parser")
        subLineByUsername = {}
        for row in soup.select("table.admin-status-table tbody tr"):
            cell = row.find("td")   #< the username cell is always first
            username = cell.find(string=True, recursive=False)   #< its own text node
            span = cell.find("span")
            subLineByUsername[username.strip()] = span.get_text(strip=True) if span else None

        self.assertEqual(subLineByUsername, {"alice": "Alice Anderson", "bob": None})

    def test_shows_total_skips_column(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        self.assertIn(b"Total Skips", resp.data)
        self.assertIn(b"7", resp.data)

    def test_shows_needs_reauth_badge_instead_of_configured(self):
        """A user whose stored refresh token was confirmed to lack the
        recently-played scope (users.spotify_needs_reauth) must show as
        distinctly needing re-authorization, not as a healthy 'Configured' -
        otherwise nothing on /admin distinguishes them from an account that's
        actually fine."""
        dash = self._makeApp()
        users = [dict(self._MOCK_USERS[0], spotify_needs_reauth=True, lastfm_api_key=None)]
        resp = self._getAdmin(dash, isAdmin=True, users=users)
        body = resp.data.decode()
        self.assertIn("NEEDS RE-AUTH", body)
        self.assertNotIn(">CONFIGURED<", body)   #< only the Last.fm column would say it, and it's unconfigured here

    def test_configured_user_without_reauth_flag_still_shows_configured(self):
        dash = self._makeApp()
        users = [dict(self._MOCK_USERS[0], spotify_needs_reauth=False, lastfm_api_key=None)]
        resp = self._getAdmin(dash, isAdmin=True, users=users)
        body = resp.data.decode()
        self.assertIn(">CONFIGURED<", body)
        self.assertNotIn("NEEDS RE-AUTH", body)

    def test_headers_are_relabeled(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        body = resp.data.decode()
        self.assertIn("Spotify API Backfill", body)
        self.assertIn("Last.fm API Backfill", body)

    def test_disabled_toggle_adds_qualifier_to_header(self):
        dash = self._makeApp()
        dash.repo.setSpotifyApiBackfillEnabled(False)
        resp = self._getAdmin(dash, isAdmin=True)
        body = resp.data.decode()
        self.assertIn("Spotify API Backfill", body)
        self.assertIn("(disabled)", body)

    def test_last_user_row_has_no_bottom_border(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        body = resp.data.decode()
        aliceRowStart = body.find("<tr", body.find(">alice<") - 200)
        bobRowStart = body.find("<tr", body.find(">bob<") - 200)
        aliceRow = body[aliceRowStart:body.find(">alice<")]
        bobRow = body[bobRowStart:body.find(">bob<")]
        self.assertIn("border-bottom", aliceRow)
        self.assertNotIn("border-bottom", bobRow)

    def test_never_activates_a_database_for_other_users_rows(self):
        """get_user_db() constructs a live Database (starts the listener,
        auto-importer, and background worker threads, including a live
        Spotify poll). Rendering the users table must never call it for any
        row - not bob's, not orphan's, not even alice's own row as a table
        entry - since doing so would silently activate a live session for
        every configured user on every /admin view. The single legitimate
        call is get_current_user_or_redirect()'s own resolution of the
        acting admin's session, which happens exactly once regardless of
        how many rows the table has."""
        dash = self._makeApp()
        users = [
            {"username": "alice", "email": "alice@example.com",
             "cookies_json": '{"sp_dc": "123"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": True},
            {"username": "bob", "email": "bob@example.com",
             "cookies_json": '{"sp_dc": "456"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": False},
            {"username": "orphan", "email": "orphan@example.com",
             "cookies_json": None,
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": False},
        ]
        patches = self._patches(dash, isAdmin=True, users=users)

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/admin")
            callCount = dash.get_user_db.call_count
            calledUsernames = [call.args[0] for call in dash.get_user_db.call_args_list]

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"bob", resp.data)
        self.assertIn(b"orphan", resp.data)
        self.assertEqual(callCount, 1)
        self.assertEqual(calledUsernames, ["alice"])

    def test_configured_but_inactive_user_shows_inactive_not_healthy(self):
        """A user with cookies configured but no entry in
        dashboard.user_databases (no live session currently running in this
        process) must be reported as Inactive - distinct from both a real
        HEALTHY session and a genuinely unconfigured account."""
        dash = self._makeApp()
        users = [
            {"username": "alice", "email": "alice@example.com",
             "cookies_json": '{"sp_dc": "123"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": True},
            {"username": "bob", "email": "bob@example.com",
             "cookies_json": '{"sp_dc": "456"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": False},
        ]
        patches = self._patches(dash, isAdmin=True, users=users)
        # bob has credentials configured but no active session this process.
        dash.user_databases = {}

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/admin")
            body = resp.data.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("INACTIVE", body)
        self.assertNotIn(b"HEALTHY", resp.data)


class TestAdminUserSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/user_settings", isAdmin=False,
                          data={"data_sharing": "1", "registration": "1", "share_links": "1"})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/user_settings", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_toggle_all_three(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isDataSharingEnabled())
        self.assertTrue(dash.repo.isRegistrationEnabled())
        self.assertTrue(dash.repo.isShareLinksEnabled())

        resp = self._post(dash, "/admin/user_settings", isAdmin=True, data={})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertFalse(dash.repo.isDataSharingEnabled())
        self.assertFalse(dash.repo.isRegistrationEnabled())
        self.assertFalse(dash.repo.isShareLinksEnabled())

        resp = self._post(dash, "/admin/user_settings", isAdmin=True,
                          data={"data_sharing": "1", "registration": "1", "share_links": "1"})
        self.assertTrue(dash.repo.isDataSharingEnabled())
        self.assertTrue(dash.repo.isRegistrationEnabled())
        self.assertTrue(dash.repo.isShareLinksEnabled())

    def test_does_not_touch_lastfm_or_spotify_settings(self):
        dash = self._makeApp()
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())
        self.assertTrue(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertTrue(dash.repo.isArtistBioEnabled())
        self.assertTrue(dash.repo.isAlbumBioEnabled())

    def test_toggles_email_verification(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isEmailVerificationEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})   #< nothing checked -> disabled
        self.assertFalse(dash.repo.isEmailVerificationEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={"email_verification": "1"})
        self.assertTrue(dash.repo.isEmailVerificationEnabled())

    def test_toggles_milestones(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isMilestonesEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})   #< nothing checked -> disabled
        self.assertFalse(dash.repo.isMilestonesEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={"milestones": "1"})
        self.assertTrue(dash.repo.isMilestonesEnabled())

    def test_toggles_milestone_recalc(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isMilestoneRecalcEnabled())   #< absent row = enabled
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})   #< nothing checked -> disabled
        self.assertFalse(dash.repo.isMilestoneRecalcEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={"milestone_recalc": "1"})
        self.assertTrue(dash.repo.isMilestoneRecalcEnabled())

    def test_toggles_tags(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isTagsEnabled())   #< absent row = enabled
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})   #< nothing checked -> disabled
        self.assertFalse(dash.repo.isTagsEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={"tags": "1"})
        self.assertTrue(dash.repo.isTagsEnabled())

    def test_toggle_off_reports_invalid_topology_and_preserves_merge_state(self):
        """A corrupt merge graph must be reported through the settings page.

        The repository validates the restore topology before writing, so this
        route test exercises the real atomic failure with a cycle rather than
        replacing the repository method with a mock.
        """
        from urllib.parse import unquote_plus

        dash = self._makeApp()
        track_a = "A" * SPOTIFY_TRACK_ID_LENGTH
        track_b = "B" * SPOTIFY_TRACK_ID_LENGTH
        decision_at = timeToInt("2026-01-01T00:00:00Z")
        conn = dash.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES (?, ?, ?)",
                         ("merge-album", "Merge album", ""))
            conn.executemany(
                "INSERT INTO tracks (id, name, url, album_id, canonical_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [(track_a, "Track A", "", "merge-album", None),
                 (track_b, "Track B", "", "merge-album", None)],
            )
            conn.executemany("UPDATE tracks SET canonical_id=? WHERE id=?",
                             [(track_b, track_a), (track_a, track_b)])
            conn.execute(
                "INSERT INTO track_merge_decisions "
                "(track_id, canonical_id, reason, decided_at, decided_by, "
                "carried_canonical_id) VALUES (?, ?, ?, ?, ?, ?)",
                (track_a, track_b, "manual-merge", decision_at, "alice", track_b),
            )
        dash.repo.setTrackMergeEnabled(True)

        response = self._post(dash, "/admin/user_settings", isAdmin=True,
                              data={})
        redirect_path = response.headers["Location"]
        location = unquote_plus(redirect_path)

        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=settings", location)
        self.assertIn("Track merge could not be disabled", location)
        self.assertIn("some saved merges need repair", location)
        self.assertIn("Other user settings were saved", location)
        self.assertFalse(dash.repo.isDataSharingEnabled())
        self.assertTrue(dash.repo.isTrackMergeEnabled())
        self.assertEqual(
            conn.execute("SELECT canonical_id FROM tracks WHERE id=?",
                         (track_a,)).fetchone()["canonical_id"],
            track_b,
        )
        self.assertEqual(
            conn.execute("SELECT carried_canonical_id FROM track_merge_decisions "
                         "WHERE track_id=?", (track_a,)).fetchone()["carried_canonical_id"],
            track_b,
        )
        rendered = self._getAdmin(dash, path=redirect_path)
        self.assertEqual(rendered.status_code, 200)
        body = rendered.data.decode()
        self.assertIn("Track merge could not be disabled", body)
        self.assertIn("some saved merges need repair", body)
        self.assertIn('name="track_merge" value="1" checked', body)


class TestAdminUserSettingsHints(AdminRouteTestBase):
    """User Settings used to bury each checkbox's real scope/consequences in a
    parenthetical tail on the label - e.g. milestone_recalc's tail is the only
    place explaining that saving it rewrites achieved_at dates. Deleting that
    text would lose real information, so it moved into an on-demand <details>
    hint instead of the label itself, which is what was inflating the card's
    height 3x past its shortest neighbour."""

    # label -> hint text as Jinja autoescape renders it (apostrophes become
    # &#39;), for every checkbox whose tail became a hint.
    _HINTS = {
        "Data sharing": "Compare page and share requests on Profile",
        "Public Wrapped share links": "no-login links to a user&#39;s own yearly recap",
        "Cookie–email verification at login": "stops one user claiming another&#39;s account",
        "Achievement milestones": "topbar badge, plus the Milestones and Next milestones cards on the dashboard",
        "Treat imported history as the milestone source of truth":
            "recalculate dates, don&#39;t notify import-crossed milestones, remove ones the data no longer supports",
        "Tags": "tag panel on song/artist/album pages, tag filter on Top Songs/Artists/Albums, and the Playlists page",
        "Friends' current track on the dashboard":
            "only between accepted shares; each user can also opt out on their own Profile",
        #< the repo default is toggle-off with an empty catalog, so the route
        #  passes a preview that merges nothing and the hint renders its
        #  fallback wording; the dry-run wording is covered by
        #  TestTrackMergeDryRunWording below
        "Merge duplicate tracks (same recording released more than once)":
            "counts a song released on several albums once in the global stats; "
            "toggling off undoes every automatic merge",
        "Enable email notifications instance-wide":
            "When disabled, outgoing emails are paused and the Notifications section is hidden on user profiles.",
    }

    def test_labels_no_longer_carry_the_explanation_inline(self):
        """The old markup rendered "Label (hint text)" as one string in the
        <label> - if that concatenation still exists the hint never actually
        moved anywhere, it just got a wrapper."""
        body = self._getAdmin(self._makeApp(), path="/admin?tab=settings").data.decode()

        for label, hint in self._HINTS.items():
            self.assertNotIn("{} ({}".format(label, hint), body)

    def test_every_hint_is_still_reachable_via_its_disclosure(self):
        body = self._getAdmin(self._makeApp(), path="/admin?tab=settings").data.decode()

        for hint in self._HINTS.values():
            self.assertIn(hint, body)

    def test_hint_count_matches_checkboxes_that_have_one(self):
        """New user registration is the one checkbox short enough to need no
        explanation - it must not grow an empty info icon just for symmetry
        with its siblings."""
        body = self._getAdmin(self._makeApp(), path="/admin?tab=settings").data.decode()

        self.assertEqual(body.count('class="setting-hint"'), len(self._HINTS))

    def test_registration_row_has_no_hint_disclosure(self):
        body = self._getAdmin(self._makeApp(), path="/admin?tab=settings").data.decode()
        start = body.index("New user registration")

        self.assertNotIn("setting-hint", body[start:start + 200])


class TestTrackMergeDryRunWording(AdminRouteTestBase):
    """The dry-run hint while the toggle is off, rendered from a real planner.

    The 1.49.0 wording - "would merge 433 track(s) into 407 song(s)" - read as
    a before/after pair, i.e. a shrink of 26, when the two counts actually name
    disjoint things (duplicates that fold away vs songs that survive them). The
    hint now leads with the before-count so the arithmetic is on the page:
    before = duplicates + survivors, removed = duplicates."""

    def _seedGroups(self, dash):
        """Two actionable groups: a pair and a triple - 5 releases, 2 songs,
        3 duplicates. Multi-group on purpose: the before-count is a SUM the
        template computes, and a single pair (2 into 1, 1 removed) could not
        tell it apart from either of its addends."""
        conn = dash.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'A', '')")
            for i, isrc in enumerate(["USRC12345678"] * 2 + ["GBAYE0000001"] * 3):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc, created_at) "
                             "VALUES (?, 'Song', '', 'alb1', ?, ?)",
                             (chr(ord("A") + i) * 22, isrc, 1000.0 + i))

    def test_the_hint_shows_before_after_and_removed(self):
        dash = self._makeApp()
        self._seedGroups(dash)

        body = self._getAdmin(dash, path="/admin?tab=settings").data.decode()

        self.assertIn("matched by ISRC; would collapse 5 release(s) into "
                      "2 song(s) right now (3 duplicate(s) removed)", body)
        self.assertNotIn("would merge", body)   #< the misreadable shape is gone

    def test_enabling_reports_the_same_arithmetic(self):
        """The flash message after the toggle-on edge had the identical
        misreadable shape; it states the same three numbers the same way."""
        from urllib.parse import unquote_plus
        dash = self._makeApp()
        self._seedGroups(dash)

        resp = self._post(dash, "/admin/user_settings", isAdmin=True,
                          data={"track_merge": "1"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("Track merge enabled: 5 release(s) collapsed into "
                      "2 song(s); 3 duplicate(s) removed.",
                      unquote_plus(resp.headers["Location"]))
        self.assertTrue(dash.repo.isTrackMergeEnabled())
        self.assertIsNotNone(dash.repo.getTrackMergeLastRun())

    def test_failed_enable_merge_leaves_the_feature_disabled(self):
        dash = self._makeApp()

        with patch.object(dash.repo, "mergeTracksByIsrc",
                          side_effect=RuntimeError("merge failed")) as merge:
            response = self._post(dash, "/admin/user_settings", isAdmin=True,
                                  data={"track_merge": "1"})

        self.assertEqual(response.status_code, 500)
        merge.assert_called_once_with(enableSetting=True)
        self.assertFalse(dash.repo.isTrackMergeEnabled())


class TestAdminMilestoneWorkerHealth(AdminRouteTestBase):
    """The Instance Services panel's Milestone Detection entry. The milestone
    pass has no thread of its own - it rides the periodic login-check loop - so
    its health is that hosting thread's: RUNNING while alive, INACTIVE
    otherwise, DISABLED when the admin kill switch turns the whole feature off,
    plus a warning badge when the import-hygiene auto-recalc toggle is off."""

    def _milestoneSection(self, resp):
        """The Milestone Detection badge markup only - RUNNING/INACTIVE also
        appear in the Worker Health card's sections, so assertions must scope
        to this section's marker id."""
        self.assertIn(b'id="milestoneWorkerStatus"', resp.data)
        return resp.data.split(b'id="milestoneWorkerStatus"')[1][:300]

    def _makeAppWithLoopThread(self):
        dash = self._makeApp()
        dash._checkLoginThread = MagicMock()
        dash._checkLoginThread.is_alive.return_value = True
        return dash

    def test_inactive_without_the_login_check_loop(self):
        # The test app never starts checkLogin_thread, so the hosting loop
        # thread is absent - the panel must say so instead of implying health.
        dash = self._makeApp()
        resp = self._getAdmin(dash)
        self.assertIn(b"Milestone Detection", resp.data)
        self.assertIn(b"INACTIVE", self._milestoneSection(resp))

    def test_running_with_a_live_loop_thread(self):
        dash = self._makeAppWithLoopThread()
        resp = self._getAdmin(dash)
        section = self._milestoneSection(resp)
        self.assertIn(b"RUNNING", section)
        self.assertNotIn(b"AUTO-RECALC OFF", section)

    def test_disabled_by_the_kill_switch(self):
        dash = self._makeAppWithLoopThread()
        dash.repo.setMilestonesEnabled(False)
        resp = self._getAdmin(dash)
        section = self._milestoneSection(resp)
        self.assertIn(b"DISABLED", section)
        self.assertNotIn(b"RUNNING", section)
        self.assertNotIn(b"AUTO-RECALC OFF", section)   #< moot while the whole feature is off

    def test_warns_when_auto_recalc_is_off(self):
        dash = self._makeAppWithLoopThread()
        dash.repo.setMilestoneRecalcEnabled(False)
        resp = self._getAdmin(dash)
        section = self._milestoneSection(resp)
        self.assertIn(b"RUNNING", section)
        self.assertIn(b"AUTO-RECALC OFF", section)


class TestAdminListenerSessionLedger(AdminRouteTestBase):
    """The Worker Health card's Listener Sessions row: one badge per active
    user with the sessions-built-since-start count, carrying the last
    rebuild's time and reason in its tooltip. That is the at-a-glance answer
    to "is this rebuild churn?" the 2026-08-04 websocket investigation had to
    reconstruct from app.log by hand."""

    def _dbWithLedger(self, builds=4, reason="quiet feed hard ceiling"):
        userDb = self._makeDb()
        userDb.getListenerHealth.return_value = {
            "status": "HEALTHY", "error_count": 0, "last_error": None,
            "seconds_since_last_poll": 5,
            "session_builds": builds,
            "last_rebuild_time": 1718000000.0,
            "last_rebuild_reason": reason,
        }
        return userDb

    def test_the_ledger_renders_per_user_with_the_rebuild_reason(self):
        dash = self._makeApp()
        userDb = self._dbWithLedger()
        resp = self._getAdmin(dash, patches=self._patches(dash, True, userDb=userDb))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Listener Sessions", resp.data)
        self.assertIn(b"alice: 4", resp.data)
        self.assertIn(b"quiet feed hard ceiling", resp.data)

    def test_a_health_snapshot_without_the_ledger_still_renders(self):
        """_makeDb's default getListenerHealth has no session fields - the
        page must render regardless (an older Database, or a mocked one)."""
        dash = self._makeApp()
        resp = self._getAdmin(dash)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Listener Sessions", resp.data)
        self.assertNotIn(b"alice: 4", resp.data)


class TestPushWithoutBackfillHelper(unittest.TestCase):
    """_pushWithoutBackfill: the pure decision behind the push-listener warning
    section - see implementationPlan-2026-09-07.md section 3. "Exposed" means
    a user has a listener (cookies_json) AND either lacks API credentials
    entirely or has credentials Spotify no longer honours (needsReauth) -
    stored-but-dead credentials protect nobody."""

    def test_push_disabled_returns_none_regardless_of_backfill_or_users(self):
        from routes.admin import _pushWithoutBackfill
        exposedUser = [{"username": "bob", "cookies_json": '{"a":1}', "hasApi": False, "needsReauth": False}]
        self.assertIsNone(_pushWithoutBackfill(False, False, exposedUser))
        self.assertIsNone(_pushWithoutBackfill(False, True, exposedUser))

    def test_push_on_and_global_backfill_off_warns_regardless_of_users(self):
        from routes.admin import _pushWithoutBackfill
        self.assertEqual(_pushWithoutBackfill(True, False, []), {"backfillDisabled": True})

    def test_push_on_and_backfill_on_lists_only_exposed_usernames(self):
        from routes.admin import _pushWithoutBackfill
        users = [
            {"username": "alice", "cookies_json": '{"a":1}', "hasApi": True, "needsReauth": False},
            {"username": "bob", "cookies_json": '{"a":1}', "hasApi": False, "needsReauth": False},
        ]
        result = _pushWithoutBackfill(True, True, users)
        self.assertEqual([u["username"] for u in result["usernames"]], ["bob"])

    def test_push_on_and_backfill_on_but_no_exposed_users_returns_none(self):
        from routes.admin import _pushWithoutBackfill
        users = [{"username": "alice", "cookies_json": '{"a":1}', "hasApi": True, "needsReauth": False}]
        self.assertIsNone(_pushWithoutBackfill(True, True, users))

    def test_needs_reauth_user_is_exposed_even_with_stored_credentials(self):
        from routes.admin import _pushWithoutBackfill
        users = [{"username": "alice", "cookies_json": '{"a":1}', "hasApi": True, "needsReauth": True}]
        result = _pushWithoutBackfill(True, True, users)
        self.assertEqual([u["username"] for u in result["usernames"]], ["alice"])

    def test_import_only_user_without_cookies_is_never_exposed(self):
        """No cookies_json means no listener at all - push never runs for
        this account regardless of how bad its API credentials are."""
        from routes.admin import _pushWithoutBackfill
        users = [{"username": "carol", "cookies_json": None, "hasApi": False, "needsReauth": False}]
        self.assertIsNone(_pushWithoutBackfill(True, True, users))


class TestPushBackfillWarningSection(AdminRouteTestBase):
    """The /admin push-listener warning section, directly under the
    deploy-mismatch block - see implementationPlan-2026-09-07.md section 3.

    Every username already appears in the main users table regardless of
    exposure, so assertIn(username, resp.data) alone proves nothing - every
    assertion here slices to the warning section's own container first."""

    def _warningSection(self, resp):
        self.assertIn(b'id="push-backfill-warning"', resp.data)
        after = resp.data.split(b'id="push-backfill-warning"', 1)[1]
        return after.split(b'</section>', 1)[0]

    def test_no_section_when_push_is_disabled(self):
        dash = self._makeApp()
        dash.repo.setPushListenerEnabled(False)
        resp = self._getAdmin(dash, isAdmin=True)
        self.assertNotIn(b'id="push-backfill-warning"', resp.data)

    def test_backfill_disabled_wording_when_the_global_toggle_is_off(self):
        dash = self._makeApp()
        dash.repo.setPushListenerEnabled(True)
        dash.repo.setSpotifyApiBackfillEnabled(False)
        resp = self._getAdmin(dash, isAdmin=True)
        section = self._warningSection(resp)
        self.assertIn(b"disabled", section.lower())

    def test_user_without_any_credentials_is_listed(self):
        # bob, the base fixture's second user, has cookies but no Spotify API
        # credentials at all.
        dash = self._makeApp()
        dash.repo.setPushListenerEnabled(True)
        dash.repo.setSpotifyApiBackfillEnabled(True)
        resp = self._getAdmin(dash, isAdmin=True)
        section = self._warningSection(resp)
        self.assertIn(b"bob", section)
        self.assertNotIn(b"alice", section)   #< alice has valid, live credentials

    def test_user_with_credentials_but_needing_reauth_is_listed(self):
        dash = self._makeApp()
        dash.repo.setPushListenerEnabled(True)
        dash.repo.setSpotifyApiBackfillEnabled(True)
        users = [dict(self._MOCK_USERS[0], spotify_needs_reauth=True)]   #< alice only
        resp = self._getAdmin(dash, isAdmin=True, users=users)
        section = self._warningSection(resp)
        self.assertIn(b"alice", section)

    def test_import_only_user_is_never_listed(self):
        dash = self._makeApp()
        dash.repo.setPushListenerEnabled(True)
        dash.repo.setSpotifyApiBackfillEnabled(True)
        users = self._MOCK_USERS + [{
            "username": "carol", "email": "carol@example.com",
            "cookies_json": None,
            "spotify_client_id": None, "spotify_refresh_token": None,
            "lastfm_api_key": None, "created_at": 1718000002.0, "is_admin": False,
        }]
        resp = self._getAdmin(dash, isAdmin=True, users=users)
        section = self._warningSection(resp)
        self.assertIn(b"bob", section)      #< still exposed, keeps the section rendered
        self.assertNotIn(b"carol", section)

    def test_user_with_valid_credentials_is_never_listed(self):
        dash = self._makeApp()
        dash.repo.setPushListenerEnabled(True)
        dash.repo.setSpotifyApiBackfillEnabled(True)
        resp = self._getAdmin(dash, isAdmin=True)   #< base fixture: alice valid, bob exposed
        section = self._warningSection(resp)
        self.assertNotIn(b"alice", section)
        self.assertIn(b"bob", section)


class TestLiveMissRatioHelper(unittest.TestCase):
    """_liveMissRatio: the pure ratio maths behind the admin ledger's Live
    miss ratio (30d) row - see implementationPlan-2026-09-07.md section 5."""

    def test_zero_live_and_zero_missed_returns_none(self):
        from routes.admin import _liveMissRatio
        self.assertIsNone(_liveMissRatio({}))
        self.assertIsNone(_liveMissRatio({"live": 0, "prompt_backfill": 0, "late_backfill": 5}))

    def test_all_live_gives_zero_percent(self):
        from routes.admin import _liveMissRatio
        result = _liveMissRatio({"live": 10, "prompt_backfill": 0, "late_backfill": 0})
        self.assertEqual(result, {"pct": 0.0, "live": 10, "missed": 0, "late": 0})

    def test_all_backfill_gives_full_percent(self):
        from routes.admin import _liveMissRatio
        result = _liveMissRatio({"live": 0, "prompt_backfill": 10, "late_backfill": 3})
        self.assertEqual(result, {"pct": 100.0, "live": 0, "missed": 10, "late": 3})

    def test_a_tiny_ratio_rounds_to_display_zero_but_is_not_none(self):
        from routes.admin import _liveMissRatio
        result = _liveMissRatio({"live": 9999, "prompt_backfill": 1, "late_backfill": 0})
        self.assertIsNotNone(result)
        self.assertEqual("{:.1f}%".format(result["pct"]), "0.0%")
        self.assertNotEqual(result["pct"], 0)


class TestAdminLiveMissRatioSection(AdminRouteTestBase):
    """The Worker Health card's Live miss ratio (30d) row, a sibling of
    Listener Sessions - see implementationPlan-2026-09-07.md section 5."""

    def _ratioSection(self, resp):
        self.assertIn(b'id="live-miss-ratio"', resp.data)
        after = resp.data.split(b'id="live-miss-ratio"', 1)[1]
        return after.split(b'</div>', 1)[0]

    def test_no_data_fallback_when_nothing_is_in_the_window(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        section = self._ratioSection(resp)
        self.assertIn(b"NO DATA", section)

    def test_badge_and_tooltip_render_from_the_repo(self):
        dash = self._makeApp()
        patches = self._patches(dash, True)
        patches.append(patch.object(
            dash.repo, "getPlaySourceCountsByUser",
            return_value={"alice": {"live": 92, "prompt_backfill": 8, "late_backfill": 40}}))
        resp = self._getAdmin(dash, patches=patches)
        section = self._ratioSection(resp)
        self.assertIn(b"alice: 8.0%", section)
        self.assertIn(b"8 missed live of 100 prompt plays in 30d; 40 more recovered late "
                      b"(offline listening, not counted)", section)

    def test_user_without_rows_in_the_window_is_omitted(self):
        dash = self._makeApp()
        patches = self._patches(dash, True)
        patches.append(patch.object(
            dash.repo, "getPlaySourceCountsByUser",
            return_value={"alice": {"live": 10, "prompt_backfill": 0, "late_backfill": 0}}))
        resp = self._getAdmin(dash, patches=patches)
        section = self._ratioSection(resp)
        self.assertIn(b"alice", section)
        self.assertNotIn(b"bob", section)


class TestAdminSkipSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=False,
                          data={"skip_mode": "seconds", "skip_value": "30"})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=True,
                          data={"skip_mode": "seconds", "skip_value": "30"}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_set_seconds_threshold(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=True,
                          data={"skip_mode": "seconds", "skip_value": "30"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertEqual(dash.repo.getSkipThreshold(), ("seconds", 30))

    def test_admin_can_set_percent_threshold(self):
        dash = self._makeApp()
        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "percent", "skip_value": "20"})
        self.assertEqual(dash.repo.getSkipThreshold(), ("percent", 20))

    def test_value_is_clamped_to_mode_bounds(self):
        dash = self._makeApp()
        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "seconds", "skip_value": "999"})
        self.assertEqual(dash.repo.getSkipThreshold(), ("seconds", 60))

    def test_invalid_value_redirects_with_error(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=True,
                          data={"skip_mode": "seconds", "skip_value": "abc"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    def test_saving_recomputes_skip_flags(self):
        dash = self._makeApp()
        from conftest import normalizeTrackForTest
        durationMs, playedMs, thresholdSeconds = 100_000, 6_000, 30
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.upsertTrack(normalizeTrackForTest({"id": "t", "name": "Track", "duration": durationMs, "artists": []}))
        dash.repo.insertPlay("alice", "t", timeToInt("2025-06-15"), playedMs, is_skip=0)
        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "seconds", "skip_value": str(thresholdSeconds)})
        self.assertEqual(dash.repo._conn().execute("SELECT is_skip FROM plays").fetchone()[0], 1)
        self.assertFalse(dash.repo._conn().in_transaction)

    def test_saves_completion_percent(self):
        dash = self._makeApp()
        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "seconds", "skip_value": "5", "completion_complete_percent": "70"})
        self.assertEqual(dash.repo.getCompletionCompletePercent(), 70)

    def test_a_new_completion_percent_is_applied_before_the_recompute(self):
        """Both values live in one form, and since 73e1a2c the completion percent
        is an INPUT to plays.is_skip (computeIsSkip caps its threshold at the
        completion boundary) - not just a display setting. Recomputing before
        storing it left every row classified under the old percent, so the flag
        on disk disagreed with the classifier until someone saved the form a
        second time: exactly the "complete and abandoned at once" contradiction
        73e1a2c removed.

        20s track, 12s played. At the default 80% the boundary is 16s, so it is
        a skip; at the 50% being saved here it is 10s, so it is not."""
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.upsertTrack({
            "id": "t1", "name": "Short One", "url": "", "artists": [],
            "album": {"id": "al1", "name": "Album", "url": "", "imageId": "al1",
                      "imageUrl": "", "totalTracks": 1, "releaseDate": 0.0},
            "imageUrl": "", "imageId": "al1", "duration": 20_000, "explicit": False,
            "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
        })
        dash.repo.insertPlay("alice", "t1", 1_700_000_000.0, 12_000, is_skip=1)

        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "seconds", "skip_value": "30",
                         "completion_complete_percent": "50"})

        stored = dash.repo._conn().execute(
            "SELECT is_skip FROM plays WHERE track_id = 't1'").fetchone()["is_skip"]
        self.assertEqual(dash.repo.getCompletionCompletePercent(), 50)
        self.assertEqual(stored, dash.repo.computeIsSkip(12_000, 20_000),
                         "the stored flag must agree with the classifier the save left behind")
        self.assertEqual(stored, 0)


class TestDatabaseIntegrityPanel(AdminRouteTestBase):
    """The startup probe has counted dangling foreign-key rows at every boot
    since it landed, into the log and nowhere else - so the live instance
    carried the same 201 for over a week with nobody able to see them without
    reading app.log. The number's whole value is in noticing when it CHANGES,
    which needs somewhere it can be looked at."""

    def test_a_healthy_database_says_so(self):
        dash = self._makeApp()

        body = self._getAdmin(dash).data.decode()

        self.assertIn("Database Integrity", body)
        self.assertIn('<span class="badge badge-success">OK</span>', body)

    def test_a_healthy_database_adds_no_filler_line(self):
        """The OK badge is the whole message. The "No problems found." sentence
        that used to sit under it said nothing the badge didn't, and cost height
        in the card the insights row wants short."""
        dash = self._makeApp()

        body = self._getAdmin(dash).data.decode()

        self.assertNotIn("No problems found", body)

    def test_dangling_rows_are_named_and_counted(self):
        dash = self._makeApp()
        patches = self._patches(dash, isAdmin=True)
        patches.append(patch.object(dash.repo, "checkIntegrity", return_value={
            "ok": False, "corruption": [],
            "foreignKeyViolations": {"track_artists": 195, "plays": 6},
        }))

        body = self._getAdmin(dash, patches=patches).data.decode()

        self.assertIn("201", body)          #< the total, which is what moves
        self.assertIn("track_artists", body)
        self.assertIn("plays", body)

    def test_a_probe_that_could_not_run_is_not_reported_as_damage(self):
        """checkIntegrity funnels ANY exception into `corruption`, which is the
        right shape for a log line and the wrong one for a verdict: a lock
        timeout under a heavy import would tell an admin their database is
        destroyed and to restore from a backup."""
        dash = self._makeApp()
        patches = self._patches(dash, isAdmin=True)
        patches.append(patch.object(dash.repo, "checkIntegrity", return_value={
            "ok": False, "corruption": [], "foreignKeyViolations": {},
            "probeError": "database is locked",
        }))

        body = self._getAdmin(dash, patches=patches).data.decode()

        self.assertNotIn("Restore from a backup", body)
        self.assertNotIn("DAMAGED", body)
        self.assertIn("could not run", body.lower())

    def test_corruption_is_reported_separately_from_dangling_rows(self):
        """They warrant different reactions - a damaged file is an emergency,
        a dangling track_id is inert - so the panel must not conflate them."""
        dash = self._makeApp()
        patches = self._patches(dash, isAdmin=True)
        patches.append(patch.object(dash.repo, "checkIntegrity", return_value={
            "ok": False, "corruption": ["row 42 missing from index idx_plays_user_time"],
            "foreignKeyViolations": {},
        }))

        body = self._getAdmin(dash, patches=patches).data.decode()

        self.assertIn("row 42 missing from index", body)
        self.assertIn("Restore from a backup", body)


class TestAdminTuningSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/tuning_settings", isAdmin=False,
                          data={"discover_artist_limit": "10"})
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_set_discover_limit(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/tuning_settings", isAdmin=True,
                          data={"discover_artist_limit": "12"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dash.repo.getDiscoverArtistLimit(5), 12)

    def test_worker_counts_are_clamped(self):
        dash = self._makeApp()
        self._post(dash, "/admin/tuning_settings", isAdmin=True,
                   data={"image_download_workers": "999"})
        self.assertEqual(dash.repo.getImageDownloadWorkers(5), 32)

    def test_blank_field_is_left_unchanged(self):
        dash = self._makeApp()
        dash.repo.setIntSetting("discover_artist_limit", 8, 1, 25)
        self._post(dash, "/admin/tuning_settings", isAdmin=True,
                   data={"discover_artist_limit": ""})
        self.assertEqual(dash.repo.getDiscoverArtistLimit(5), 8)


class TestAdminRestart(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/restart", isAdmin=False, data={})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/restart", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_disabled_by_default_does_not_schedule_exit(self):
        dash = self._makeApp()
        with patch("threading.Timer") as timer, patch.dict(os.environ, clear=False):
            os.environ.pop("ALLOW_INSTANCE_RESTART", None)
            resp = self._post(dash, "/admin/restart", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertNotIn("message=", resp.headers["Location"])
        timer.assert_not_called()

    def test_enabled_schedules_graceful_exit(self):
        # threading.Timer is mocked, so the scheduled shutdown+os._exit never
        # fires - the test only asserts the exit was scheduled.
        dash = self._makeApp()
        with patch("threading.Timer") as timer, \
             patch.dict(os.environ, {"ALLOW_INSTANCE_RESTART": "1"}):
            resp = self._post(dash, "/admin/restart", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        timer.assert_called_once()

    def test_enabled_reports_progress_as_a_message_not_an_error(self):
        # A successfully scheduled restart is informational - it must land in
        # /admin's green `message` banner, not the red `error` one.
        dash = self._makeApp()
        with patch("threading.Timer"), \
             patch.dict(os.environ, {"ALLOW_INSTANCE_RESTART": "1"}):
            resp = self._post(dash, "/admin/restart", isAdmin=True, data={})
        location = resp.headers["Location"]
        self.assertIn("message=", location)
        self.assertNotIn("error=", location)

    def test_enabled_with_trailing_whitespace_still_schedules_graceful_exit(self):
        # Docker's --env-file and `-e KEY=VALUE ` pass trailing whitespace
        # through untouched, so ALLOW_INSTANCE_RESTART="1 " reaches the gate
        # exactly as an operator's env file spelled it. routes/admin.py used
        # to read this with only `.lower()` (no `.strip()`), so a value every
        # other env-flag reader in the app treats as on left this one - and
        # POST /admin/restart - reading as off.
        dash = self._makeApp()
        with patch("threading.Timer") as timer, \
             patch.dict(os.environ, {"ALLOW_INSTANCE_RESTART": "1 "}):
            resp = self._post(dash, "/admin/restart", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        timer.assert_called_once()


class TestAdminLastfmSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=False, data={})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_toggle_all_four(self):
        dash = self._makeApp()

        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertFalse(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertFalse(dash.repo.isArtistBioEnabled())
        self.assertFalse(dash.repo.isAlbumBioEnabled())
        self.assertFalse(dash.repo.isInheritedGenresEnabled())

        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={
            "lastfm_backfill": "1", "artist_bio": "1", "album_bio": "1", "include_inherited": "1",
        })
        self.assertTrue(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertTrue(dash.repo.isArtistBioEnabled())
        self.assertTrue(dash.repo.isAlbumBioEnabled())
        self.assertTrue(dash.repo.isInheritedGenresEnabled())

    def test_does_not_touch_user_or_spotify_settings(self):
        dash = self._makeApp()
        self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={})
        self.assertTrue(dash.repo.isDataSharingEnabled())
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())

    def test_saves_backfill_retry_days(self):
        dash = self._makeApp()
        self._post(dash, "/admin/lastfm_settings", isAdmin=True,
                   data={"genre_backfill_retry_days": "14", "bio_backfill_retry_days": "60"})
        self.assertEqual(dash.repo.getGenreBackfillRetryDays(), 14)
        self.assertEqual(dash.repo.getBioBackfillRetryDays(), 60)


class TestAdminBackupSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/backup_settings", isAdmin=False, data={"backup_interval_hours": "12"})
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_set_interval_and_retention(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/backup_settings", isAdmin=True,
                          data={"backup_interval_hours": "12", "backup_retention_count": "10"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dash.repo.getBackupIntervalHours(24), 12)
        self.assertEqual(dash.repo.getBackupRetentionCount(7), 10)

    def test_zero_disables(self):
        dash = self._makeApp()
        self._post(dash, "/admin/backup_settings", isAdmin=True, data={"backup_interval_hours": "0"})
        self.assertEqual(dash.repo.getBackupIntervalHours(24), 0)


class TestAdminEmailSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=False, data={})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_save_settings(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "email_notifications_enabled": "1",
            "smtp_host": "smtp.example.com",
            "smtp_port": "465",
            "smtp_encryption": "ssl",
            "smtp_user": "bot@example.com",
            "smtp_password": "hunter2",
            "smtp_from_email": "noreply@example.com",
            "smtp_from_name": "Tracker",
            "instance_public_url": "https://tracker.example.com",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])

        from services.email_service import get_smtp_config, get_instance_public_url
        config = get_smtp_config(dash.repo)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["host"], "smtp.example.com")
        self.assertEqual(config["port"], 465)
        self.assertEqual(config["encryption"], "ssl")
        self.assertEqual(config["from_email"], "noreply@example.com")
        self.assertEqual(get_instance_public_url(dash.repo), "https://tracker.example.com")

    def test_public_url_defaults_to_empty_when_omitted(self):
        dash = self._makeApp()
        self._post(dash, "/admin/email_settings", isAdmin=True, data={})

        from services.email_service import get_instance_public_url
        self.assertEqual(get_instance_public_url(dash.repo), "")

    def test_invalid_port_falls_back_to_default(self):
        dash = self._makeApp()
        self._post(dash, "/admin/email_settings", isAdmin=True, data={"smtp_port": "not-a-number"})

        from services.email_service import get_smtp_config, DEFAULT_SMTP_PORT
        self.assertEqual(get_smtp_config(dash.repo)["port"], DEFAULT_SMTP_PORT)

    def test_malformed_from_email_is_rejected_and_nothing_saved(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "smtp_host": "smtp.example.com",
            "smtp_from_email": "notanemail",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

        from services.email_service import get_smtp_config
        config = get_smtp_config(dash.repo)
        self.assertEqual(config["host"], "")
        self.assertEqual(config["from_email"], "")

    def test_javascript_scheme_public_url_is_rejected_and_nothing_saved(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "instance_public_url": "javascript:alert(1)",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

        from services.email_service import get_instance_public_url
        self.assertEqual(get_instance_public_url(dash.repo), "")

    def test_enabling_notifications_with_empty_host_is_rejected_and_nothing_saved(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "email_notifications_enabled": "1",
            "smtp_host": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

        from services.email_service import get_smtp_config
        self.assertFalse(get_smtp_config(dash.repo)["enabled"])

    def test_enabling_notifications_with_empty_from_address_is_rejected_and_nothing_saved(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "email_notifications_enabled": "1",
            "smtp_host": "smtp.example.com",
            "smtp_from_email": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

        from services.email_service import get_smtp_config
        self.assertFalse(get_smtp_config(dash.repo)["enabled"])

    def test_disabled_notifications_with_empty_from_address_saves(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "smtp_host": "smtp.example.com",
            "smtp_from_email": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("error=", resp.headers["Location"])

        from services.email_service import get_smtp_config
        config = get_smtp_config(dash.repo)
        self.assertFalse(config["enabled"])
        self.assertEqual(config["from_email"], "")

    def test_invalid_submission_does_not_clobber_a_working_configuration(self):
        dash = self._makeApp()
        self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "email_notifications_enabled": "1",
            "smtp_host": "smtp.example.com",
            "smtp_from_email": "noreply@example.com",
            "instance_public_url": "https://tracker.example.com",
        })

        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data={
            "email_notifications_enabled": "1",
            "smtp_host": "smtp.example.com",
            "smtp_from_email": "not-an-address",
            "instance_public_url": "https://tracker.example.com",
        })
        self.assertIn("error=", resp.headers["Location"])

        from services.email_service import get_smtp_config, get_instance_public_url
        config = get_smtp_config(dash.repo)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["host"], "smtp.example.com")
        self.assertEqual(config["from_email"], "noreply@example.com")
        self.assertEqual(get_instance_public_url(dash.repo), "https://tracker.example.com")


class TestSmtpPasswordStaysOutOfTemplateContext(AdminRouteTestBase):
    """admin.html only ever uses the SMTP password as a presence check (the
    masked placeholder), so the decrypted secret has no business in the render
    context: it sits one careless value="{{ ... }}" edit away from the wire,
    and a Werkzeug debug frame would show it. The worker/send paths keep
    getting the real value - only the template call site asks for the masked
    shape."""

    def _saveSmtpPassword(self, dash, password):
        from services.email_service import save_smtp_config
        save_smtp_config(repo=dash.repo, enabled=True, host="smtp.example.com",
                         port=587, encryption="tls", user="bot@example.com",
                         password=password, from_email="noreply@example.com",
                         from_name="Tracker")

    def test_the_admin_context_carries_no_plaintext_password(self):
        from flask import template_rendered
        dash = self._makeApp()
        self._saveSmtpPassword(dash, "hunter2")

        captured = []

        def record(sender, template, context, **extra):
            captured.append((template.name, context))

        template_rendered.connect(record, dash.app)
        try:
            #< the email card renders on the settings tab
            resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=settings")
        finally:
            template_rendered.disconnect(record, dash.app)

        self.assertEqual(resp.status_code, 200)
        contexts = [ctx for name, ctx in captured if name == "admin.html"]
        self.assertEqual(len(contexts), 1)
        smtpConfig = contexts[0]["smtp_config"]
        self.assertNotIn("hunter2", str(smtpConfig))
        self.assertTrue(smtpConfig["has_password"])
        #< the placeholder mask must survive the masking of the value it marks
        self.assertIn("••••••••", resp.data.decode("utf-8"))

    def test_an_unset_password_reads_as_not_set(self):
        dash = self._makeApp()

        resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=settings")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Not set", resp.data)

    def test_an_undecryptable_password_reads_as_not_set(self):
        """decryptSecret treats a value it cannot read as MISSING (a database
        restored without its key, a rotated DATA_ENCRYPTION_KEY). The admin
        page has to agree: masking it as present would promise working email
        from a secret that can never authenticate, and offer to "clear" a
        password the page cannot see. Keying the flag off the raw blob rather
        than the plaintext is exactly that mistake."""
        from services.email_service import get_smtp_config
        dash = self._makeApp()
        self._saveSmtpPassword(dash, "hunter2")

        with patch("services.email_service.decryptSecret", return_value=None):
            config = get_smtp_config(dash.repo, include_password=False)
            self.assertFalse(config["has_password"])

            resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=settings")

        self.assertIn(b"Not set", resp.data)

    def test_the_worker_path_still_gets_the_real_password(self):
        from services.email_service import get_smtp_config
        dash = self._makeApp()
        self._saveSmtpPassword(dash, "hunter2")

        self.assertEqual(get_smtp_config(dash.repo)["password"], "hunter2")

        masked = get_smtp_config(dash.repo, include_password=False)
        self.assertEqual(masked["password"], "")
        self.assertTrue(masked["has_password"])


class TestAdminSendTestEmail(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/test_email", isAdmin=False, data={})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/test_email", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_without_smtp_configured_redirects_with_error(self):
        """No SMTP host has been saved yet - send_test_email must fail
        cleanly (no network call, blocked by conftest's _blockNetwork
        anyway) rather than the route raising."""
        dash = self._makeApp()
        resp = self._post(dash, "/admin/test_email", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    def test_admin_success_redirects_with_message(self):
        dash = self._makeApp()
        with patch("routes.admin.send_test_email", return_value=(True, None)):
            resp = self._post(dash, "/admin/test_email", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("message=", resp.headers["Location"])

    def test_admin_failure_redirects_with_error(self):
        dash = self._makeApp()
        with patch("routes.admin.send_test_email", return_value=(False, "Connection refused")):
            resp = self._post(dash, "/admin/test_email", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    def test_a_failed_send_keeps_the_smtp_error_out_of_the_redirect(self):
        """The flash rides in the redirect's query string - browser history
        and the access log - and services/email_service.py's send_test_email
        already logs the exception at ERROR (:213-215), including an SMTP
        auth failure's echoed username. Same class of bug as 9b1d44a
        (routes/auth.py's profile actions)."""
        from urllib.parse import unquote_plus

        dash = self._makeApp()
        with patch("routes.admin.send_test_email",
                   return_value=(False, "(535, b'5.7.8 Authentication failed: someone@example.com')")):
            resp = self._post(dash, "/admin/test_email", isAdmin=True, data={})

        self.assertEqual(resp.status_code, 302)
        location = unquote_plus(resp.headers["Location"])
        self.assertIn("error=", location)
        self.assertIn("Test email failed", location)
        self.assertNotIn("someone@example.com", location)
        self.assertNotIn("Authentication failed", location)


class TestAdminCreateBackup(AdminRouteTestBase):
    def _mockWorker(self, runBackup=None, error=None):
        """Stand-in BackupWorker whose isBackupRunning() tracks whether a
        snapshot is in flight, the way the real one's lock does - the route
        asks it before starting a second run."""
        import threading
        from pathlib import Path

        worker = MagicMock()
        inFlight = threading.Event()

        def run():
            inFlight.set()
            try:
                if error is not None:
                    raise error
                if runBackup is not None:
                    return runBackup()
                return Path("/fake/Backups/spotify_stats_backup_20260724_120000.db")
            finally:
                inFlight.clear()

        worker.runBackup.side_effect = run
        worker.isBackupRunning.side_effect = inFlight.is_set
        return worker

    def _postBackup(self, dash, isAdmin=True, loggedIn=True, backupWorker=None, headers=None):
        from pathlib import Path
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            dash.backupWorker = backupWorker
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post("/admin/create_backup", headers=headers or {})

    def test_unauthenticated_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._postBackup(dash, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._postBackup(dash, isAdmin=False)
        self.assertEqual(resp.status_code, 403)

    def test_ajax_success_returns_json(self):
        from pathlib import Path
        dash = self._makeApp()
        mock_worker = self._mockWorker()
        resp = self._postBackup(dash, backupWorker=mock_worker, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "success")
        self.assertIn("spotify_stats_backup_20260724_120000.db", payload["message"])
        mock_worker.runBackup.assert_called_once()

    def test_form_success_redirects_with_message(self):
        from pathlib import Path
        dash = self._makeApp()
        mock_worker = self._mockWorker()
        resp = self._postBackup(dash, backupWorker=mock_worker)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertIn("message=", resp.headers["Location"])

    def test_runs_even_when_scheduler_disabled(self):
        from pathlib import Path
        dash = self._makeApp()
        mock_worker = self._mockWorker()
        mock_worker.isEnabled.return_value = False
        resp = self._postBackup(dash, backupWorker=mock_worker, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        mock_worker.runBackup.assert_called_once()

    def test_ajax_error_returns_json_200(self):
        dash = self._makeApp()
        mock_worker = self._mockWorker(error=RuntimeError("disk full"))
        resp = self._postBackup(dash, backupWorker=mock_worker, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "error")
        self.assertIn("disk full", payload["message"])

    def test_form_error_redirects_with_error_param(self):
        dash = self._makeApp()
        mock_worker = self._mockWorker(error=RuntimeError("disk full"))
        resp = self._postBackup(dash, backupWorker=mock_worker)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertIn("error=", resp.headers["Location"])

    def test_missing_backup_worker_returns_error(self):
        dash = self._makeApp()
        resp = self._postBackup(dash, backupWorker=None, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "error")
        self.assertIn("not available", payload["message"])

    def test_slow_backup_returns_without_blocking(self):
        """A backup that outlives the synchronous wait returns promptly with a
        "still running" message rather than tying up the request thread.

        Deterministic by construction rather than by timing: the sync wait is
        patched to 0, so join() returns at once, and the backup blocks on an
        Event with no timeout - Thread.is_alive() is True from start() until
        run() returns, and run() cannot return while blocked. There is no
        duration here that a loaded machine could overshoot."""
        import threading
        from pathlib import Path
        dash = self._makeApp()
        release = threading.Event()

        def blocking_backup():
            release.wait()   #< unbounded on purpose; released in the finally below
            return Path("/fake/Backups/spotify_stats_backup_20260724_120000.db")

        mock_worker = self._mockWorker(runBackup=blocking_backup)
        try:
            with patch("routes.admin.MANUAL_BACKUP_SYNC_WAIT_SECONDS", 0):
                resp = self._postBackup(dash, backupWorker=mock_worker,
                                        headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertEqual(payload["kind"], "success")
            self.assertIn("shortly", payload["message"])
        finally:
            release.set()

    def test_a_backup_already_in_flight_is_rejected(self):
        """The route's whole job here: ask the worker, and refuse if it says a
        snapshot is running.

        No threads and no clock - the previous version of this test started a
        real background backup and raced it, which is what made it flaky under
        xdist. Whether the worker's answer is *correct* is the worker's own
        concern, covered by TestConcurrentBackups in test_backup_worker.py."""
        dash = self._makeApp()
        mock_worker = self._mockWorker()
        mock_worker.isBackupRunning.side_effect = None
        mock_worker.isBackupRunning.return_value = True

        resp = self._postBackup(dash, backupWorker=mock_worker,
                                headers={"X-Requested-With": "XMLHttpRequest"})

        payload = resp.get_json()
        self.assertEqual(payload["kind"], "error")
        self.assertIn("already in progress", payload["message"])
        mock_worker.runBackup.assert_not_called()   #< never reached a second snapshot

    def test_a_second_backup_is_allowed_once_the_first_finishes(self):
        """The rejection is state-driven, not a one-shot latch."""
        dash = self._makeApp()
        mock_worker = self._mockWorker()

        first = self._postBackup(dash, backupWorker=mock_worker,
                                 headers={"X-Requested-With": "XMLHttpRequest"})
        second = self._postBackup(dash, backupWorker=mock_worker,
                                  headers={"X-Requested-With": "XMLHttpRequest"})

        self.assertEqual(first.get_json()["kind"], "success")
        self.assertEqual(second.get_json()["kind"], "success")
        self.assertEqual(mock_worker.runBackup.call_count, 2)



class TestAdminRefreshLastfmEntity(AdminRouteTestBase):
    """/admin/lastfm/refresh/<kind>/<entity_id> - the detail pages' "Refresh
    Last.fm Data" button. Database.refreshLastfmEntity itself is covered by
    tests/test_lastfm_refresh_entity.py; this only exercises the route's
    admin gating and its status -> redirect/message mapping."""

    def _postRefresh(self, dash, kind, entity_id, isAdmin=True, loggedIn=True, db=None, data=None, headers=None):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db or self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post(f"/admin/lastfm/refresh/{kind}/{entity_id}", data=data or {},
                               headers=headers or {})

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._postRefresh(dash, "artist", "aX", isAdmin=False)
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._postRefresh(dash, "artist", "aX", loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_unknown_kind_is_404(self):
        dash = self._makeApp()
        resp = self._postRefresh(dash, "playlist", "aX")
        self.assertEqual(resp.status_code, 404)

    def test_artist_success_redirects_with_success_message_and_group_by(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "ok", "name": "Artist X"}
        resp = self._postRefresh(dash, "artist", "aX", db=db, data={"groupBy": "month"})
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/artist/aX", location)
        self.assertIn("success=", location)
        self.assertIn("groupBy=month", location)
        db.refreshLastfmEntity.assert_called_once_with("artist", "aX")

    def test_album_error_status_redirects_with_error_message(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "no_artist"}
        resp = self._postRefresh(dash, "album", "alP", db=db)
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/album/alP", location)
        self.assertIn("error=", location)

    def test_track_kind_redirects_to_the_song_page(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "ok", "name": "Song A"}
        resp = self._postRefresh(dash, "track", "tA", db=db)
        self.assertIn("/song/tA", resp.headers["Location"])

    def test_ajax_post_returns_json_instead_of_redirecting(self):
        """The detail pages submit the form via fetch (admin-refresh.js) so a
        refresh doesn't navigate away and reset tab/sort/page state - the
        route answers XHR posts with the message JSON instead of a redirect."""
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "ok", "name": "Artist X"}

        resp = self._postRefresh(dash, "artist", "aX", db=db,
                                 headers={"X-Requested-With": "XMLHttpRequest"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "success")
        self.assertIn("Artist X", payload["message"])

    def test_ajax_post_returns_error_kind_for_error_statuses(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "transient"}

        resp = self._postRefresh(dash, "album", "alP", db=db,
                                 headers={"X-Requested-With": "XMLHttpRequest"})

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "error")
        self.assertIn("didn't respond", payload["message"])


class TestAdminSpotifySettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/spotify_settings", isAdmin=False, data={"spotify_backfill": "1"})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/spotify_settings", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_toggle_it(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())

        resp = self._post(dash, "/admin/spotify_settings", isAdmin=True, data={})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertFalse(dash.repo.isSpotifyApiBackfillEnabled())

        resp = self._post(dash, "/admin/spotify_settings", isAdmin=True, data={"spotify_backfill": "1"})
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())

    def test_push_listener_is_off_until_an_admin_turns_it_on(self):
        """The only feature key that defaults to DISABLED. It changes how plays
        are DETECTED, and the measurement behind it covered 9 minutes of
        listening - enough to clear the structural unknowns, not enough to be
        the default (see eventDrivenConnectStatePlan.md)."""
        dash = self._makeApp()
        self.assertFalse(dash.repo.isPushListenerEnabled())

        self._post(dash, "/admin/spotify_settings", isAdmin=True,
                   data={"spotify_backfill": "1", "push_listener": "1"})
        self.assertTrue(dash.repo.isPushListenerEnabled())

        self._post(dash, "/admin/spotify_settings", isAdmin=True, data={"spotify_backfill": "1"})
        self.assertFalse(dash.repo.isPushListenerEnabled())

    def test_toggling_backfill_does_not_disturb_push_mode(self):
        """Both live on one form, so a save that omits one must not silently
        flip the other - the form posts both every time."""
        dash = self._makeApp()
        self._post(dash, "/admin/spotify_settings", isAdmin=True,
                   data={"spotify_backfill": "1", "push_listener": "1"})

        self._post(dash, "/admin/spotify_settings", isAdmin=True, data={"push_listener": "1"})

        self.assertFalse(dash.repo.isSpotifyApiBackfillEnabled())
        self.assertTrue(dash.repo.isPushListenerEnabled())


class TestAdminManageAdmins(AdminRouteTestBase):
    """/admin/users/<username>/admin - promote/demote, driven against a real
    repo so the last-admin invariant (Repository.demoteAdmin) is exercised end
    to end rather than mocked."""

    def _postSetAdmin(self, dash, username, makeAdmin, acting="alice", loggedIn=True):
        with patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value=acting), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = f'{acting}@example.com'
                    sess['username'] = acting
            return client.post(f"/admin/users/{username}/admin",
                               data={"make_admin": "1" if makeAdmin else "0"})

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        dash.repo.upsertUser("bob", "bob@example.com")   #< acting user, not an admin
        dash.repo.commit()
        resp = self._postSetAdmin(dash, "alice", makeAdmin=True, acting="bob")
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._postSetAdmin(dash, "bob", makeAdmin=True, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_promote_another_user(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.upsertUser("bob", "bob@example.com")
        dash.repo.setUserAdmin("alice", True)
        dash.repo.commit()
        self.assertFalse(dash.repo.isAdmin("bob"))

        resp = self._postSetAdmin(dash, "bob", makeAdmin=True)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertTrue(dash.repo.isAdmin("bob"))

    def test_admin_can_demote_another_admin_when_not_the_last_one(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.upsertUser("bob", "bob@example.com")
        dash.repo.setUserAdmin("alice", True)
        dash.repo.setUserAdmin("bob", True)
        dash.repo.commit()

        resp = self._postSetAdmin(dash, "bob", makeAdmin=False)

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("error=", resp.headers["Location"])
        self.assertFalse(dash.repo.isAdmin("bob"))
        self.assertTrue(dash.repo.isAdmin("alice"))

    def test_cannot_demote_the_last_admin(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserAdmin("alice", True)
        dash.repo.commit()

        resp = self._postSetAdmin(dash, "alice", makeAdmin=False)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertIn("error=", resp.headers["Location"])
        self.assertTrue(dash.repo.isAdmin("alice"), "the last admin must stay an admin")

    def test_demote_non_admin_is_a_noop_not_an_error(self):
        # Regression: with exactly one admin, demoting a *non-admin* user used to
        # be falsely blocked with "Cannot remove the instance's last admin" even
        # though nothing would change.
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.upsertUser("bob", "bob@example.com")
        dash.repo.setUserAdmin("alice", True)   #< sole admin; bob is not one
        dash.repo.commit()

        resp = self._postSetAdmin(dash, "bob", makeAdmin=False)

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("error=", resp.headers["Location"])
        self.assertTrue(dash.repo.isAdmin("alice"))
        self.assertFalse(dash.repo.isAdmin("bob"))


class TestAdminInsights(AdminRouteTestBase):
    def test_renders_catalog_coverage_worker_health_and_activity(self):
        dash = self._makeApp()
        extra = {
            "getCatalogGenreCoverage": {
                "song": {"covered": 5, "total": 10, "percent": 50.0},
                "album": {"covered": 5, "total": 10, "percent": 50.0},
                "artist": {"covered": 5, "total": 10, "percent": 50.0},
                "overall": {"percent": 50.0},
            },
            "getRecentRegistrationCounts": {"last_7_days": 2, "last_30_days": 9},
            "getInstanceShareCounts": {"pending": 3, "accepted": 4},
            "getActiveShareLinksCount": 6,
        }

        resp = self._getAdmin(dash, isAdmin=True, extraInsights=extra)
        body = resp.data.decode()

        self.assertIn("Catalog Backfill Coverage", body)
        self.assertIn("50.0%", body)
        self.assertIn("Worker Health", body)
        self.assertIn("RUNNING:", body)
        self.assertIn("Activity", body)
        self.assertIn("2", body)   # last_7_days
        self.assertIn("9", body)  # last_30_days


class TestAdminInsightsLayout(AdminRouteTestBase):
    """The insights row was Coverage | Worker Health | Activity, and .grid
    stretches every card in a row to the tallest one: Worker Health carried
    nine worker families plus the integrity probe, Activity carried five
    numbers, so Activity rendered at roughly three times the height its content
    needed. The instance-scoped entries are their own card now, and Activity
    left the row entirely rather than being padded out to fill it."""

    # Every entry that must stay in the Worker Health card - all per-account
    # thread pools, in render order.
    _WORKER_FAMILIES = (
        "Listener Sync",
        "Spotify API Backfill Workers",
        "Last.fm Genre Workers",
        "Last.fm Album Bio Workers",
        "Last.fm Artist Bio Workers",
        "Auto-Importer Watchdogs",
        "Wrapped Calculation Workers",
    )

    # Instance-scoped, one of each per install rather than one per account.
    # The TOTP secret is shared by every account's session, so its entry
    # leads the card rather than living with the per-account worker pools.
    _SERVICE_ENTRIES = (
        "Spotify Session Token",
        "Milestone Detection",
        "Database Backup Service",
        "Mail Worker",
        "Database Integrity",
    )

    def _assertTile(self, body, label, value):
        """A stat tile renders its value close after its label - the window
        keeps this from passing on a digit that happens to appear anywhere
        else on a page full of counts."""
        at = body.index(label)
        window = body[at:at + 300]
        self.assertIn(">{}</h2>".format(value), window,
                      "the '{}' tile must show {}".format(label, value))

    def test_instance_services_is_its_own_card(self):
        body = self._getAdmin(self._makeApp()).data.decode()

        self.assertIn("Instance Services", body)

    def test_worker_families_and_services_land_in_separate_cards(self):
        """The split is the whole point - if an entry drifts back across the
        boundary the tall card comes back with it."""
        body = self._getAdmin(self._makeApp()).data.decode()
        servicesAt = body.index("Instance Services")

        for family in self._WORKER_FAMILIES:
            self.assertLess(body.index(family), servicesAt,
                            "{} belongs in the Worker Health card".format(family))
        for entry in self._SERVICE_ENTRIES:
            self.assertGreater(body.index(entry), servicesAt,
                               "{} belongs in the Instance Services card".format(entry))

    def test_spotify_session_token_leads_the_instance_services_card(self):
        """It affects every account at once, so it renders first in the card."""
        body = self._getAdmin(self._makeApp()).data.decode()
        servicesAt = body.index("Instance Services")

        self.assertLess(body.index("Spotify Session Token", servicesAt),
                        body.index("Milestone Detection", servicesAt),
                        "Spotify Session Token must be the card's first entry")

    def test_spotify_rate_limiting_backing_off_badge(self):
        fake_snapshot = {
            "backoffRemainingSeconds": 45.2,
            "backoffs": 3,
            "lastReason": "429 Too Many Requests",
            "secondsSinceLastBackoff": 120
        }
        with patch('routes.admin.SPOTIFY_LIMITER.snapshot', return_value=fake_snapshot):
            body = self._getAdmin(self._makeApp()).data.decode()
            self.assertIn("BACKING OFF: 45s left", body)
            self.assertIn("LAST: 429 Too Many Requests", body)
            self.assertIn("2 min ago", body)

    def test_spotify_rate_limiting_handles_none_gracefully(self):
        with patch('routes.admin.SPOTIFY_LIMITER.snapshot', return_value=None):
            body = self._getAdmin(self._makeApp()).data.decode()
            self.assertIn("Spotify Rate Limiting", body)
            self.assertIn("UNAVAILABLE", body)

    _HEALTHY_TOTP = {
        "pinnedVersion": 61, "activeVersion": 61, "overrideActive": False,
        "autoRecovered": False, "overrideEnvVar": "SPOTIFY_TOTP_SECRET",
        "consecutiveFailures": 0, "suspectedRotation": False,
        "secondsSinceFirstFailure": None,
    }

    def _totpBody(self, **overrides):
        snapshot = {**self._HEALTHY_TOTP, **overrides}
        with patch("routes.admin.totpAuthSnapshot", return_value=snapshot):
            return self._getAdmin(self._makeApp()).data.decode()

    def test_spotify_session_token_reports_ok_when_healthy(self):
        body = self._totpBody()

        self.assertIn("Spotify Session Token", body)
        self.assertIn("VERSION: 61", body)
        #< no recovery instructions while nothing is wrong
        self.assertNotIn("TOTP SECRET LIKELY ROTATED", body)

    def test_a_confirmed_rotation_is_called_out_with_how_to_recover(self):
        """The panel has to answer "what now?" - an operator seeing only a red
        badge still has to go find the env var name in the source."""
        body = self._totpBody(consecutiveFailures=7, suspectedRotation=True,
                              secondsSinceFirstFailure=300)

        self.assertIn("TOTP SECRET LIKELY ROTATED", body)
        self.assertIn("7 FAILURES IN A ROW", body)
        self.assertIn("SINCE 5 MIN AGO", body)
        self.assertIn("SPOTIFY_TOTP_SECRET", body)
        self.assertIn("smoketest", body)   #< how to confirm it's the secret, not cookies

    def test_a_short_failure_streak_is_reported_without_crying_rotation(self):
        """Below the threshold it's a blip - visible, but not an alarm that
        sends someone editing secrets."""
        body = self._totpBody(consecutiveFailures=1)

        self.assertIn("TOKEN FAILURES: 1", body)
        self.assertNotIn("TOTP SECRET LIKELY ROTATED", body)

    def test_an_active_override_is_never_hidden(self):
        """"We pin 61" would be a lie during an incident where someone already
        applied a different secret."""
        body = self._totpBody(activeVersion=62, overrideActive=True)

        self.assertIn("VERSION: 62", body)
        self.assertIn("OVERRIDDEN VIA SPOTIFY_TOTP_SECRET", body)

    def test_transport_failures_and_last_real_mint_are_visible(self):
        body = self._totpBody(transportFailures=3, secondsSinceLastMint=120)
        self.assertIn("TOKEN REQUEST FAILURES: 3", body)
        self.assertIn("LAST TOKEN MINT: 2 MIN AGO", body)
        self.assertNotIn("TOTP SECRET LIKELY ROTATED", body)

    def test_an_auto_recovered_secret_is_shown_as_temporary(self):
        """Recovery keeps the instance running but holds the secret in memory,
        so the panel has to say it will vanish on restart - otherwise the
        incident looks closed and the pin never gets updated."""
        body = self._totpBody(activeVersion=62, autoRecovered=True)

        self.assertIn("AUTO-RECOVERED (PINNED: 61)", body)
        self.assertIn("VERSION: 62", body)
        self.assertIn("in memory only", body)
        self.assertIn("SPOTIFY_TOTP_SECRET_VERSION", body)   #< where to make it permanent

    def test_an_env_override_outranks_auto_recovery_in_the_display(self):
        """Both can be true at once; the env var is what's actually in force,
        so showing the auto-recovery badge would misdescribe the instance."""
        body = self._totpBody(activeVersion=70, overrideActive=True, autoRecovered=True)

        self.assertIn("OVERRIDDEN VIA SPOTIFY_TOTP_SECRET", body)
        self.assertNotIn("AUTO-RECOVERED", body)

    def test_spotify_session_token_handles_none_gracefully(self):
        with patch("routes.admin.totpAuthSnapshot", return_value=None):
            body = self._getAdmin(self._makeApp()).data.decode()

        self.assertIn("Spotify Session Token", body)
        self.assertIn("UNAVAILABLE", body)

    def test_skip_value_input_has_aria_label(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=settings")
        body = resp.data.decode()
        self.assertIn('id="skipValueInput"', body)
        self.assertIn('aria-label="Skip threshold value"', body)

    def test_insights_cards_do_not_stretch_to_the_tallest_in_the_row(self):
        """Splitting the card evens the row out but never exactly - without
        this the shorter two are still inflated to whatever the tallest one
        happens to be.  The layout is now enforced via .admin-card-grid which
        carries align-items: start in style.css rather than as an inline style."""
        body = self._getAdmin(self._makeApp()).data.decode()

        # The grid class is present (carries align-items: start in CSS)
        self.assertIn("admin-card-grid", body)

    def test_activity_renders_between_the_users_table_and_the_insights_row(self):
        body = self._getAdmin(self._makeApp()).data.decode()

        self.assertLess(body.index("Registered Users & Sync Status"), body.index("New users (30d)"))
        self.assertLess(body.index("New users (30d)"), body.index("Catalog Backfill Coverage"))

    def test_activity_tiles_render_every_metric(self):
        extra = {
            "getRecentRegistrationCounts": {"last_7_days": 2, "last_30_days": 9},
            "getInstanceShareCounts": {"pending": 3, "accepted": 4},
            "getActiveShareLinksCount": 6,
        }

        body = self._getAdmin(self._makeApp(), extraInsights=extra).data.decode()

        self._assertTile(body, "New users (30d)", 9)
        self._assertTile(body, "Pending shares", 3)
        self._assertTile(body, "Accepted shares", 4)
        self._assertTile(body, "Wrapped links", 6)

    def test_activity_heading_and_description_render_inline(self):
        """The description rides on the heading's baseline rather than on its
        own row. The flex/baseline pair used to be an inline style here and is
        now .admin-section-heading in style.css - it moved because its bottom
        margin was a page gap that no rule could reach while it sat inline
        (see TestPageRhythmAndInlineHero) - so the row is asserted where each
        half of it now lives."""
        body = self._getAdmin(self._makeApp()).data.decode()

        self.assertIn("Instance-wide signups and data-sharing activity.", body)
        descAt = body.index("Instance-wide signups and data-sharing activity.")
        self.assertIn("admin-section-heading", body[descAt - 200:descAt + 100])

        with open(os.path.join(os.path.dirname(__file__), "..",
                               "static", "css", "style.css"), encoding="utf-8") as fh:
            rule = fh.read().split(".admin-section-heading {")[1].split("}")[0]
        self.assertIn("display: flex", rule)
        self.assertIn("align-items: baseline", rule)


class TestAdminMailWorkerHealth(AdminRouteTestBase):
    def test_mail_worker_renders_in_instance_services(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        body = resp.data.decode()

        self.assertIn("Mail Worker", body)
        self.assertIn('id="emailWorkerStatus"', body)

    def test_send_test_email_button_design_and_position(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=settings")
        body = resp.data.decode()

        #< button-small since the 2026-08-10 sweep: sizes are classes now,
        #  never inline paddings
        self.assertIn('form="adminTestEmailForm" class="primary-button button-small"', body)
        self.assertIn('Send Test Email to Admin</button>', body)


class TestAdminTabNavigation(AdminRouteTestBase):
    def test_registered_users_table_always_rendered(self):
        dash = self._makeApp()
        for tab in ("overview", "workers", "settings"):
            resp = self._getAdmin(dash, isAdmin=True, path=f"/admin?tab={tab}")
            body = resp.data.decode()
            self.assertIn("Registered Users & Sync Status", body)
            self.assertIn("alice", body)

    def test_overview_tab_contents(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=overview")
        body = resp.data.decode()

        self.assertIn("Activity", body)
        self.assertIn("Catalog Backfill Coverage", body)
        self.assertIn("Worker Health", body)
        self.assertIn("Instance Services", body)

    def test_workers_tab_contents(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=workers")
        body = resp.data.decode()

        self.assertIn("Spotify API Backfilling", body)
        self.assertIn("Last.fm Backfilling Settings", body)
        self.assertIn("Advanced Tuning", body)

    def test_settings_tab_contents(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=settings")
        body = resp.data.decode()

        self.assertIn("User Settings", body)
        self.assertIn("Email &amp; Notifications", body)
        self.assertIn("Playback Classification", body)
        self.assertIn("Backups", body)

    def test_settings_tab_3_column_structure(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True, path="/admin?tab=settings")
        body = resp.data.decode()

        playback_idx = body.find("Playback Classification")
        backups_idx = body.find("Backups")
        self.assertNotEqual(playback_idx, -1)
        self.assertNotEqual(backups_idx, -1)
        self.assertLess(playback_idx, backups_idx)
        self.assertIn('class="admin-settings-col"', body)


    def test_email_settings_post_redirects_to_settings_tab(self):
        dash = self._makeApp()
        data = {"email_notifications_enabled": "1", "smtp_host": "smtp.example.com", "smtp_port": "587"}
        resp = self._post(dash, "/admin/email_settings", isAdmin=True, data=data)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("tab=settings", resp.location)

    def test_user_admin_post_redirect_preserves_tab(self):
        dash = self._makeApp()
        dash.repo.upsertUser("bob", "bob@example.com")
        resp = self._post(dash, "/admin/users/bob/admin?tab=overview", isAdmin=True, data={"make_admin": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("tab=overview", resp.location)

    def test_test_email_post_redirects_to_settings_tab(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/test_email", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("tab=settings", resp.location)


class TestAdminRestartTabRedirect(AdminRouteTestBase):
    def test_restart_disabled_redirects_to_settings_tab(self):
        """adminRestart error redirect must land on the settings tab."""
        import os
        from unittest.mock import patch
        dash = self._makeApp()
        with patch.dict(os.environ, {}, clear=False):
            # ALLOW_INSTANCE_RESTART not set → disabled path
            resp = self._post(dash, "/admin/restart", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("tab=settings", resp.location)


class TestAdminUserAdminFormHiddenTab(AdminRouteTestBase):
    def test_promote_demote_form_includes_hidden_tab_field(self):
        """The Promote/Demote form must carry a hidden tab input so the
        backend can redirect back to the tab the admin was on."""
        dash = self._makeApp()
        dash.repo.upsertUser("charlie", "charlie@example.com")
        body = self._getAdmin(dash, isAdmin=True).data.decode()
        self.assertIn('name="tab"', body)

    def test_aria_live_on_tab_body(self):
        """#admin-tab-body must declare aria-live so AJAX swaps are
        announced to screen readers."""
        dash = self._makeApp()
        body = self._getAdmin(dash, isAdmin=True).data.decode()
        self.assertIn('aria-live', body)
        # The aria-live attribute must be on or near the tab body container
        idx = body.find('id="admin-tab-body"')
        self.assertNotEqual(idx, -1)
        snippet = body[idx:idx + 80]
        self.assertIn('aria-live', snippet)


if __name__ == "__main__":
    unittest.main()




