"""The /profile display-name form, and the rule the rest of the app has to
keep: a username is shown as its display name but ADDRESSED by its real key.

The second half is the one worth guarding. `username` reaches templates in two
completely different roles - as a label, and as the `/img/<username>/` segment
that serveTrackImage authorizes against (see routes/media.py) - and they sit
next to each other in the same files. Swapping the wrong one renders a page
that looks right and silently 404s every cover image, which no assertion about
the visible name would catch. TestNamesAreShownNotAddressedBy pins both halves
together on purpose.
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from _compare_fragment import fetchComparison


def _song(trackId, name, **extra):
    #< duration/artists are required by _embedSongTextElements' direct key access
    return {"id": trackId, "name": name, "artists": [], "duration": 60000, **extra}


def _zeroHeatmapGrid():
    return [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]


class ProfileDisplayNameTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()

    def _loginAs(self, username, email=None):
        email = email or f"{username}@example.com"
        self.dash.repo.upsertUser(username, email)
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        for patcher in (
            patch.object(self.dash, 'is_user_logged_in', return_value=True),
            patch.object(self.dash, 'get_username_for_email', return_value=username),
            patch.object(self.dash, 'get_user_db', return_value=db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client

    def _save(self, client, value):
        #< the action redirects (see test_profile_prg.py); follow it so these
        #  tests keep asserting against the rendered page. A rate-limited save
        #  returns 429 rather than a redirect and arrives here unchanged.
        return client.post("/profile", data={"action": "save_display_name",
                                             "display_name": value},
                           follow_redirects=True)


class TestSavingADisplayName(ProfileDisplayNameTestCase):
    def test_a_valid_name_is_stored_and_confirmed(self):
        client = self._loginAs("alice")

        resp = self._save(client, "Alice Wonder")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.dash.repo.getDisplayName("alice"), "Alice Wonder")
        self.assertIn(b"Display name set to Alice Wonder", resp.data)

    def test_surrounding_whitespace_is_trimmed(self):
        client = self._loginAs("alice")

        self._save(client, "   Alice Wonder   ")

        self.assertEqual(self.dash.repo.getDisplayName("alice"), "Alice Wonder")

    def test_an_empty_value_clears_back_to_the_username(self):
        client = self._loginAs("alice")
        self._save(client, "Alice Wonder")

        resp = self._save(client, "")

        self.assertEqual(self.dash.repo.getDisplayName("alice"), "alice")
        self.assertIn(b"you&#39;ll show as alice", resp.data)

    def test_a_whitespace_only_value_is_a_clear_not_an_error(self):
        """It strips to empty, so it means the same thing as an empty field -
        rejecting it as "too short" would be a confusing way to say that."""
        client = self._loginAs("alice")
        self._save(client, "Alice Wonder")

        self._save(client, "     ")

        self.assertEqual(self.dash.repo.getDisplayName("alice"), "alice")

    def test_clearing_stores_null_rather_than_a_copy_of_the_username(self):
        """A stored copy would silently stop following the key and would also
        occupy the name against other accounts' collision checks."""
        client = self._loginAs("alice")
        self._save(client, "Alice Wonder")

        self._save(client, "")

        row = self.dash.repo._conn().execute(
            "SELECT display_name FROM users WHERE username='alice'").fetchone()
        self.assertIsNone(row["display_name"])


class TestRejectingABadDisplayName(ProfileDisplayNameTestCase):
    def _assertRejected(self, value, expectedFragment):
        client = self._loginAs("alice")

        resp = self._save(client, value)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(expectedFragment, resp.data)
        self.assertEqual(self.dash.repo.getDisplayName("alice"), "alice")

    def test_too_short(self):
        self._assertRejected("A", b"at least")

    def test_too_long(self):
        self._assertRejected("A" * 33, b"at most")

    def test_disallowed_characters(self):
        """The charset is deliberately narrow: this name lands in page titles,
        a share picker, other users' screens and a downloaded file's name."""
        self._assertRejected("<script>x</script>", b"letters, digits, spaces")

    def test_a_name_another_account_already_displays_as(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.setDisplayName("bob", "Taken Name")

        self._assertRejected("Taken Name", b"already taken")

    def test_a_name_that_is_another_accounts_username(self):
        """Impersonation inside the share flow: /admin, /compare?with= and the
        share picker all identify people by the real username."""
        self.dash.repo.upsertUser("bob", "bob@example.com")

        self._assertRejected("bob", b"already taken")

    def test_your_own_username_is_yours_to_take(self):
        client = self._loginAs("alice")

        resp = self._save(client, "Alice")

        self.assertEqual(self.dash.repo.getDisplayName("alice"), "Alice")
        self.assertIn(b"Display name set to Alice", resp.data)

    def test_saving_is_rate_limited(self):
        """A rejected save reports whether a name exists, so an unthrottled
        form is a cheap enumeration oracle - same reasoning as request_share."""
        client = self._loginAs("alice")

        with patch.object(self.dash, '_rateLimited', return_value=True):
            resp = self._save(client, "Alice Wonder")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self.dash.repo.getDisplayName("alice"), "alice")


class TestTheProfileForm(ProfileDisplayNameTestCase):
    def test_it_shows_the_permanent_username_alongside_the_display_name(self):
        """The username still appears in some URLs, so hiding it entirely would
        leave someone unable to explain what they see in the address bar."""
        client = self._loginAs("alice")
        self._save(client, "Alice Wonder")

        body = client.get("/profile").data.decode("utf-8")

        self.assertIn("Alice Wonder", body)
        self.assertIn("<strong>Username:</strong> alice", body)

    def test_the_field_redisplays_the_saved_name(self):
        client = self._loginAs("alice")

        self._save(client, "Alice Wonder")
        body = client.get("/profile").data.decode("utf-8")

        self.assertIn('name="display_name" value="Alice Wonder"', body)

    def test_the_field_is_empty_while_the_name_is_just_the_username(self):
        """The fallback is not an edit: prefilling it with the username would
        make an untouched save silently pin a copy of the key."""
        client = self._loginAs("alice")

        body = client.get("/profile").data.decode("utf-8")

        self.assertIn('name="display_name" value=""', body)
        self.assertIn('placeholder="alice"', body)

    def test_the_saved_name_is_shown_immediately_after_saving(self):
        """The render reads the name AFTER the POST branch, so the page a user
        gets back from the save already reflects it."""
        client = self._loginAs("alice")

        body = self._save(client, "Alice Wonder").data.decode("utf-8")

        self.assertIn('name="display_name" value="Alice Wonder"', body)


class TestTheTopbar(ProfileDisplayNameTestCase):
    def test_the_account_menu_shows_the_display_name(self):
        client = self._loginAs("alice")
        self._save(client, "Alice Wonder")

        body = client.get("/profile").data.decode("utf-8")

        self.assertIn('class="dropdown-trigger"', body)
        self.assertIn("Alice Wonder <span class=\"chevron\">", body)

    def test_it_falls_back_to_the_username(self):
        client = self._loginAs("alice")

        body = client.get("/profile").data.decode("utf-8")

        self.assertIn("alice <span class=\"chevron\">", body)


class TestTheShareLists(ProfileDisplayNameTestCase):
    def setUp(self):
        super().setUp()
        #< both rows must exist before any share row can reference them
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.setDisplayName("bob", "Bob Builder")

    def test_the_request_picker_labels_by_display_name_but_submits_the_username(self):
        client = self._loginAs("alice")

        body = client.get("/profile/sharing").data.decode("utf-8")

        self.assertIn('<option value="bob">Bob Builder</option>', body)

    def test_an_incoming_request_names_the_requester_by_display_name(self):
        self.dash.repo.createShareRequest("bob", "alice")
        client = self._loginAs("alice")

        body = client.get("/profile/sharing").data.decode("utf-8")

        self.assertIn("Bob Builder", body)
        self.assertIn("wants to share data with you", body)

    def test_an_outgoing_request_names_the_recipient_by_display_name(self):
        client = self._loginAs("alice")
        self.dash.repo.createShareRequest("alice", "bob")

        body = client.get("/profile/sharing").data.decode("utf-8")

        self.assertIn("Waiting on", body)
        self.assertIn("Bob Builder", body)

    def test_an_accepted_share_names_the_counterpart_by_display_name(self):
        self.dash.repo.createShareRequest("bob", "alice")
        shareId = self.dash.repo.getPendingIncomingShares("alice")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "alice", accept=True)
        client = self._loginAs("alice")

        body = client.get("/profile/sharing").data.decode("utf-8")

        self.assertIn("Bob Builder", body)
        #< the Compare link still addresses the counterpart by the real key
        self.assertIn("/compare?with=bob", body)


class TestNamesAreShownNotAddressedBy(AppTestCase):
    """The label-vs-authorization-segment rule, pinned on /compare - the page
    that renders both at once and the one where getting it wrong is invisible
    until every cover image 404s."""

    def _makeStubDb(self, topSongs=()):
        db = MagicMock()
        db.tz = None
        db.getPlayTotals.return_value = (0, 0)
        db.getTopSongs.return_value = list(topSongs)
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getSongsCount.return_value = 0
        db.getArtistsCount.return_value = 0
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getHourOfDayHeatmap.return_value = _zeroHeatmapGrid()
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0,
                                        "percentage": 0, "message": "", "error": False}
        return db

    def setUp(self):
        self.dash = self._makeApp()
        for username in ("alice", "bob", "carol"):
            self.dash.repo.upsertUser(username, f"{username}@example.com")
            self.dash.repo.setUserCookies(username, {"sp_dc": "test"})
        self.dash.repo.setDisplayName("alice", "Alice Wonder")
        self.dash.repo.setDisplayName("bob", "Bob Builder")
        self.dash.repo.setDisplayName("carol", "Carol Danvers")

        #< two counterparts: one is enough to compare, but the picker nav that
        #  shows a label while linking by the key only renders above one
        for counterpart in ("bob", "carol"):
            self.dash.repo.createShareRequest("alice", counterpart)
            shareId = self.dash.repo.getPendingIncomingShares(counterpart)[0]["id"]
            self.dash.repo.respondToShareRequest(shareId, counterpart, accept=True)

        self.dbs = {"alice": self._makeStubDb([_song("t1", "Song One", imageId="img1")]),
                    "bob": self._makeStubDb(),
                    "carol": self._makeStubDb()}

    def _loginAs(self, username):
        return super()._loginAs(username)

    def _payload(self, client):
        _, data = fetchComparison(client, "/compare?with=bob")
        html = "".join(data.get(key) or "" for key in (
            "statsTableHtml", "genresHtml", "myTopSongsHtml", "theirTopSongsHtml"))
        return data, html

    def test_the_shell_headings_use_display_names(self):
        client = self._loginAs("alice")

        body = client.get("/compare?with=bob").data.decode("utf-8")

        self.assertIn("Compare with <span class=\"compare-user-theirs js-with-username\">Bob Builder", body)
        #< the apostrophe is literal template text, so it isn't entity-escaped
        self.assertIn("Alice Wonder</span>'s Top Songs", body)

    def test_the_counterpart_badge_labels_by_display_name_and_links_by_username(self):
        """The picker nav only renders with more than one counterpart, hence
        carol - and it is exactly where label and identity have to differ."""
        client = self._loginAs("alice")

        body = client.get("/compare?with=bob").data.decode("utf-8")

        self.assertIn(">Bob Builder</a>", body)
        self.assertIn(">Carol Danvers</a>", body)
        self.assertIn("with=bob", body)
        self.assertIn("with=carol", body)

    def test_the_ajax_payload_carries_both_names_separately(self):
        client = self._loginAs("alice")

        data, _ = self._payload(client)

        self.assertEqual(data["withUsername"], "bob")
        self.assertEqual(data["withDisplayName"], "Bob Builder")

    def test_the_stats_table_names_both_users_by_display_name(self):
        client = self._loginAs("alice")

        _, html = self._payload(client)

        self.assertIn("Alice Wonder", html)
        self.assertIn("Bob Builder", html)

    def test_the_trend_legend_uses_display_names(self):
        client = self._loginAs("alice")

        data, _ = self._payload(client)

        self.assertEqual([s["name"] for s in data["comparisonTrend"]["series"]],
                         ["Alice Wonder", "Bob Builder"])

    def test_cover_images_still_address_the_viewers_real_username(self):
        """The whole point of this class. /img/<username>/ is checked against
        the session's real username by routes/media.py, so a display name here
        would 404 every cover."""
        client = self._loginAs("alice")

        _, html = self._payload(client)

        self.assertIn("/img/alice/", html)
        self.assertNotIn("/img/Alice Wonder/", html)
        self.assertNotIn("/img/Alice%20Wonder/", html)


if __name__ == "__main__":
    unittest.main()
