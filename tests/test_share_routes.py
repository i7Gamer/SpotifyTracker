"""Data-sharing management: the request_share action on /profile/sharing, and
the accept/decline/cancel/revoke actions on POST /profile/shares/<id>.
"""
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, RATE_LIMIT_MAX_ATTEMPTS
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class ShareRoutesTestCase(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        return db

    def _loginAs(self, username, email):
        return self._loginAsWithDb(username, email)

    def setUp(self):
        self.dash = self._makeApp()


class TestDataSharingDisabled(ShareRoutesTestCase):
    """The admin's instance-wide kill switch: request/accept/decline/cancel/
    revoke all refuse, the nav Compare link and both topbar badges hide, and
    /compare itself 404s - existing share rows are left untouched in the DB,
    just unreachable through the blocked routes until re-enabled."""

    def test_request_share_action_refuses(self):
        self.dash.repo.setDataSharingEnabled(False)
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_share_action_route_refuses(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareIdFor("alice", "bob")
        self.dash.repo.setDataSharingEnabled(False)
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "accept"})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.dash.repo.getAcceptedShareUsernames("alice"), [])

    def test_existing_accepted_share_survives_untouched_in_the_db(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareIdFor("alice", "bob")
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)

        self.dash.repo.setDataSharingEnabled(False)

        self.assertIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))

    def _pendingShareIdFor(self, requester, recipient):
        self.dash.repo.upsertUser(requester, f"{requester}@example.com")
        self.dash.repo.createShareRequest(requester, recipient)
        return self.dash.repo.getPendingIncomingShares(recipient)[0]["id"]

    def test_compare_page_404s(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareIdFor("alice", "bob")
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        self.dash.repo.setUserCookies("bob", {"sp_dc": "test"})
        self.dash.repo.setDataSharingEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 404)

    def test_nav_link_and_badges_hide(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareIdFor("alice", "bob")
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)   #< alice has an accepted share
        self.dash.repo.setUserCookies("bob", {"sp_dc": "test"})
        self.dash.repo.setDataSharingEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile/sharing")

        self.assertNotIn(b'href="/compare"', resp.data)   #< nav Compare link gone (hasAcceptedShares forced False)
        self.assertNotIn(b"share request", resp.data)      #< no badge, despite a real accepted share existing

    def test_sharing_page_is_gone(self):
        """It has its own URL now, so the switch takes the whole page rather
        than blanking a section of one."""
        self.dash.repo.setDataSharingEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile/sharing")

        self.assertEqual(resp.status_code, 404)

    def test_sub_nav_drops_the_sharing_tab(self):
        self.dash.repo.setDataSharingEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"/profile/sharing", resp.data)
        self.assertNotIn(b"request_share", resp.data)


class TestRequestShareAction(ShareRoutesTestCase):
    def test_requesting_a_share_creates_a_pending_request(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Share request sent", resp.data)
        outgoing = self.dash.repo.getPendingOutgoingShares("alice")
        self.assertEqual([r["recipient_username"] for r in outgoing], ["bob"])

    def test_reverse_pending_request_reports_as_immediately_active(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.createShareRequest("bob", "alice")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"now sharing", resp.data)
        self.assertIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))

    def test_cannot_request_a_share_with_yourself(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "alice"},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"yourself", resp.data)
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_cannot_request_a_share_with_a_nonexistent_user(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "ghost"},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"does not exist", resp.data)
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_blank_target_username_is_rejected(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": ""},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"error", resp.data.lower())
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_re_requesting_a_pending_share_says_already_pending(self):
        """createShareRequest treats a repeat as a no-op - the message must
        say so, not claim a new request was just sent."""
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")
        client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"})

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"already pending", resp.data)
        self.assertNotIn(b"Share request sent", resp.data)

    def test_re_requesting_an_accepted_share_says_already_sharing(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"already share data with bob", resp.data)
        self.assertNotIn(b"now sharing data with each other", resp.data)

    def test_request_share_is_rate_limited(self):
        """Declines delete the share row, so without a throttle a rejected
        requester could re-request (or fan out to every user) indefinitely -
        request_share shares the same per-IP limiter as /login and /register."""
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"},
                               follow_redirects=True)
            self.assertEqual(resp.status_code, 200)

        resp = client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    def test_rate_limit_does_not_affect_other_profile_actions(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        client = self._loginAs("alice", "alice@example.com")
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS + 1):
            client.post("/profile/sharing", data={"action": "request_share", "target_username": "bob"})

        resp = client.post("/profile", data={"action": "save_preferences",
                                             "default_dashboard_window": "week", "timezone": ""},
                           follow_redirects=True)

        self.assertEqual(resp.status_code, 200)


class TestProfilePageShareListings(ShareRoutesTestCase):
    def test_picker_excludes_users_already_in_a_share_relationship(self):
        """Re-requesting an existing counterpart is always a no-op, so the
        dropdown must only offer users with no pending/accepted relationship."""
        for u in ("bob", "carol", "dave", "erin"):
            self.dash.repo.upsertUser(u, f"{u}@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        bobShareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(bobShareId, "bob", accept=True)   #< accepted
        self.dash.repo.createShareRequest("alice", "carol")                    #< pending outgoing
        self.dash.repo.createShareRequest("dave", "alice")                     #< pending incoming

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile/sharing")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'<option value="erin">', resp.data)   #< unrelated user still offered
        self.assertNotIn(b'<option value="bob">', resp.data)
        self.assertNotIn(b'<option value="carol">', resp.data)
        self.assertNotIn(b'<option value="dave">', resp.data)

    def test_lists_pending_incoming_outgoing_and_accepted_and_candidates(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("carol", "carol@example.com")
        self.dash.repo.upsertUser("dave", "dave@example.com")
        self.dash.repo.createShareRequest("bob", "alice")       #< incoming to alice
        self.dash.repo.createShareRequest("alice", "carol")     #< outgoing from alice
        self.dash.repo.createShareRequest("alice", "dave")
        daveShareId = self.dash.repo.getPendingOutgoingShares("alice")[-1]["id"]
        self.dash.repo.respondToShareRequest(daveShareId, "dave", accept=True)   #< accepted

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile/sharing")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"bob", resp.data)     #< pending incoming
        self.assertIn(b"carol", resp.data)   #< pending outgoing
        self.assertIn(b"dave", resp.data)    #< accepted

    def test_share_tables_style_rows_via_share_table_class(self):
        """All three share tables ("Requests waiting on you", "Requests you
        sent", "Active shares") carry their row border via .share-table
        instead of inline styles, so the last row can drop its bottom border
        via :last-child (same pattern as .compare-table)."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("carol", "carol@example.com")
        self.dash.repo.upsertUser("dave", "dave@example.com")
        self.dash.repo.createShareRequest("bob", "alice")       #< pending incoming
        self.dash.repo.createShareRequest("alice", "carol")     #< pending outgoing
        self.dash.repo.createShareRequest("alice", "dave")
        daveShareId = self.dash.repo.getPendingOutgoingShares("alice")[-1]["id"]
        self.dash.repo.respondToShareRequest(daveShareId, "dave", accept=True)   #< accepted

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile/sharing")

        self.assertEqual(resp.data.count(b'class="status-table share-table"'), 3)
        self.assertNotIn(b'<tr style="border-bottom: 1px solid var(--glass-border);">', resp.data)


class TestRequestAShareHint(ShareRoutesTestCase):
    """templates/_share_manage_panel.html:12-14's "Request to share..." hint
    used to render unconditionally even though the request picker beside it
    (:16-36) is gated on shareCandidates - so with no candidate (everyone
    already shared or pending) the user was told to do something with no
    control to do it. Three states, matching how routes/auth.py:634-641
    computes shareCandidates (every other user minus accepted counterparts,
    pending-incoming requesters and pending-outgoing recipients)."""

    _ORIGINAL_HINT = b"Request to share your listening stats with another user"
    _NO_CANDIDATE_HINT = (b"Everyone else on this instance already shares with you "
                           b"or has a request open, so there is nobody new to ask.")
    _EMPTY_INSTANCE_HINT = b"There's nobody else on this instance to share with yet."

    def test_a_candidate_shows_the_original_hint_and_the_picker(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")   #< no relationship yet - a candidate

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile/sharing")

        self.assertIn(self._ORIGINAL_HINT, resp.data)
        self.assertIn(b'id="targetUsername"', resp.data)
        self.assertNotIn(self._NO_CANDIDATE_HINT, resp.data)
        self.assertNotIn(self._EMPTY_INSTANCE_HINT, resp.data)

    def test_no_candidate_but_an_existing_relationship_shows_the_alternate_hint(self):
        """bob is already accepted, so he is the only other user and there is
        no one left to request - but the panel is not empty (there IS an
        accepted share to show), so the "nobody on this instance" sentence
        would be false here."""
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        bobShareId = self.dash.repo.getPendingOutgoingShares("alice")[0]["id"]
        self.dash.repo.respondToShareRequest(bobShareId, "bob", accept=True)

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile/sharing")

        self.assertIn(self._NO_CANDIDATE_HINT, resp.data)
        self.assertNotIn(self._ORIGINAL_HINT, resp.data)
        self.assertNotIn(b'id="targetUsername"', resp.data)
        self.assertNotIn(self._EMPTY_INSTANCE_HINT, resp.data)

    def test_a_brand_new_instance_shows_neither_hint(self):
        """No other user exists at all, so shareCandidates AND every list are
        empty - the existing bottom sentence already covers this case, and
        neither of the two hints above should also render."""
        self.dash.repo.upsertUser("alice", "alice@example.com")

        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile/sharing")

        self.assertNotIn(self._ORIGINAL_HINT, resp.data)
        self.assertNotIn(self._NO_CANDIDATE_HINT, resp.data)
        self.assertNotIn(b'id="targetUsername"', resp.data)
        self.assertIn(self._EMPTY_INSTANCE_HINT, resp.data)


class TestPendingSharesTopbarBadge(ShareRoutesTestCase):
    """The badge next to the version-badge in the topbar (layout.html) - the
    only place a user is alerted to an incoming share request without
    visiting /profile themselves."""

    def test_hidden_with_no_pending_incoming_requests(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertNotIn(b"pending-shares-badge", resp.data)

    def test_shows_the_count_of_pending_incoming_requests(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("carol", "carol@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("bob", "alice")
        self.dash.repo.createShareRequest("carol", "alice")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertIn(b'class="pending-shares-badge"', resp.data)
        self.assertIn(b"2 share requests", resp.data)

    def test_singular_wording_for_exactly_one_request(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("bob", "alice")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertIn(b"1 share request", resp.data)
        self.assertNotIn(b"1 share requests", resp.data)

    def test_outgoing_requests_do_not_count(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("alice", "bob")   #< alice is the requester, not recipient
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertNotIn(b"pending-shares-badge", resp.data)

    def test_badge_links_to_profile(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("bob", "alice")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertIn(b'href="/profile/sharing"', resp.data)

    def test_disappears_once_the_request_is_resolved(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("bob", "alice")
        shareId = self.dash.repo.getPendingIncomingShares("alice")[0]["id"]
        client = self._loginAs("alice", "alice@example.com")

        client.post(f"/profile/shares/{shareId}", data={"action": "accept"})
        resp = client.get("/import")

        self.assertNotIn(b"pending-shares-badge", resp.data)


class TestAcceptedShareTopbarBadge(ShareRoutesTestCase):
    """The green "your request was accepted" badge - the requester's only
    signal that their pending request became active, since accepting itself
    is the recipient's acknowledgment and needs no such badge."""

    def test_hidden_with_no_accepted_shares(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertNotIn(b"accepted-share-badge", resp.data)

    def test_appears_once_the_recipient_accepts(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        client = self._loginAs("alice", "alice@example.com")

        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        resp = client.get("/import")

        self.assertIn(b'class="accepted-share-badge"', resp.data)
        self.assertIn(b"1 share request accepted!", resp.data)

    def test_the_recipient_does_not_see_their_own_acceptance_as_a_notification(self):
        """bob just clicked Accept himself - he doesn't need to be told."""
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        client = self._loginAs("bob", "bob@example.com")

        client.post(f"/profile/shares/{shareId}", data={"action": "accept"})
        resp = client.get("/import")

        self.assertNotIn(b"accepted-share-badge", resp.data)

    def test_visiting_profile_clears_the_badge(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        client = self._loginAs("alice", "alice@example.com")

        profileResp = client.get("/profile/sharing")
        importResp = client.get("/import")

        self.assertNotIn(b"accepted-share-badge", profileResp.data)   #< cleared on the very same load
        self.assertNotIn(b"accepted-share-badge", importResp.data)    #< and stays cleared afterward

    def test_a_later_share_still_notifies_after_an_earlier_one_was_seen(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("carol", "carol@example.com")
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        bobShareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(bobShareId, "bob", accept=True)
        client = self._loginAs("alice", "alice@example.com")
        client.get("/profile/sharing")   #< sees and clears the bob notification

        self.dash.repo.createShareRequest("alice", "carol")
        carolShareId = self.dash.repo.getPendingIncomingShares("carol")[0]["id"]
        self.dash.repo.respondToShareRequest(carolShareId, "carol", accept=True)
        resp = client.get("/import")

        self.assertIn(b"accepted-share-badge", resp.data)


class TestShareActionRoute(ShareRoutesTestCase):
    def _pendingShareId(self, requester, recipient):
        self.dash.repo.upsertUser(requester, f"{requester}@example.com")
        self.dash.repo.upsertUser(recipient, f"{recipient}@example.com")
        self.dash.repo.createShareRequest(requester, recipient)
        return self.dash.repo.getPendingIncomingShares(recipient)[0]["id"]

    def test_recipient_can_accept(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))

    def test_recipient_can_decline(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "decline"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.dash.repo.getPendingIncomingShares("bob"), [])

    def test_requester_can_cancel(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "cancel"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.dash.repo.getPendingOutgoingShares("alice"), [])

    def test_either_party_can_revoke_an_accepted_share(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "revoke"})

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))

    def test_an_unrelated_user_cannot_act_on_someone_elses_share(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.upsertUser("carol", "carol@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("carol", "carol@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("bob", self.dash.repo.getAcceptedShareUsernames("alice"))
        self.assertEqual(len(self.dash.repo.getPendingIncomingShares("bob")), 1)

    def test_unknown_action_value_is_rejected(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "hack"})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.dash.repo.getPendingIncomingShares("bob")), 1)

    def test_nonexistent_share_id_does_not_500(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile/shares/999999", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)

    def test_anonymous_request_is_redirected_to_login(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        shareId = self._pendingShareId("alice", "bob")
        client = self.dash.app.test_client()   #< no session at all

        resp = client.post(f"/profile/shares/{shareId}", data={"action": "accept"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        self.assertEqual(len(self.dash.repo.getPendingIncomingShares("bob")), 1)   #< nothing acted on


if __name__ == "__main__":
    unittest.main()
