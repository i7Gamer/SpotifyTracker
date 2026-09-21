"""The "Spotify re-authorization needed" topbar badge (layout.html) - the
only place a user is alerted that Web API backfill is stuck on a missing
scope without visiting /profile themselves. See
Database.Listeners.spotifyListener's on_scope_status_change and
app.py's _injectSpotifyReauthStatus context processor.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

_SPOTIFY_ENV = {"SPOTIFY_CALLBACK_URL": "http://localhost:5000/spotify-callback"}


class SpotifyReauthBadgeTestCase(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        return db

    def _loginAs(self, username, email):
        return self._loginAsWithDb(username, email)

    def setUp(self):
        self.dash = self._makeApp()


@patch.dict(os.environ, _SPOTIFY_ENV)
class TestSpotifyReauthTopbarBadge(SpotifyReauthBadgeTestCase):
    def test_hidden_when_flag_is_not_set(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertNotIn(b"spotify-reauth-badge", resp.data)

    def test_shows_once_flagged(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.setSpotifyNeedsReauth("alice", True)

        resp = client.get("/import")

        self.assertIn(b'class="spotify-reauth-badge"', resp.data)
        self.assertIn(b"Spotify re-authorization needed", resp.data)

    def test_badge_links_to_profile(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.setSpotifyNeedsReauth("alice", True)

        resp = client.get("/import")

        self.assertIn(b'href="/profile"', resp.data)

    def test_disappears_once_cleared(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.setSpotifyNeedsReauth("alice", True)
        self.dash.repo.setSpotifyNeedsReauth("alice", False)

        resp = client.get("/import")

        self.assertNotIn(b"spotify-reauth-badge", resp.data)

    def test_flagged_user_does_not_leak_the_badge_to_another_user(self):
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.setSpotifyNeedsReauth("bob", True)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/import")

        self.assertNotIn(b"spotify-reauth-badge", resp.data)


class TestSpotifyReauthBadgeHiddenWhenFeatureDisabled(SpotifyReauthBadgeTestCase):
    """With SPOTIFY_CALLBACK_URL unset, /spotify-authorize 404s - a badge
    pointing there would be a dead end, so it must stay hidden even if the
    flag is somehow set (e.g. left over from before the feature was
    disabled)."""

    @patch.dict(os.environ, {}, clear=True)
    def test_hidden_even_when_flag_is_set(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.setSpotifyNeedsReauth("alice", True)

        resp = client.get("/import")

        self.assertNotIn(b"spotify-reauth-badge", resp.data)


if __name__ == "__main__":
    unittest.main()
