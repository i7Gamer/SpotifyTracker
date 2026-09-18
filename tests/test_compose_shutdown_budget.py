"""The compose file must give shutdown longer than Docker's default.

Every join in the shutdown path is deliberately bounded - they exist because
unbounded ones once left the app hanging for ~2.5 minutes on Ctrl+C - but the
bounds add up: the process-wide workers first, then every user's listener,
watchdog and five periodic workers (app.shutdown -> Database.stop). The users
run concurrently, so their part costs one user's worth however many there are,
capped by USER_STOP_JOIN_TIMEOUT_SECONDS.

Docker's own default is 10 seconds between SIGTERM and SIGKILL, and that
covers the WHOLE shutdown. Without a declared stop_grace_period, one wedged
Spotify call is enough to have the container killed partway through. Nothing
corrupts when that happens (SQLite is crash-safe, and a play is one
transaction), but the remaining threads never get their clean stop.

Costs nothing when shutdown is quick, which is the normal case: the grace
period is a ceiling, not a wait - Docker proceeds the moment the process exits.

"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.backup import BACKUP_STOP_JOIN_TIMEOUT_SECONDS
from Database.Importers.AutoImporter import WATCHDOG_STOP_JOIN_TIMEOUT_SECONDS
from Database.Listeners.spotifyListener import LISTENER_STOP_JOIN_TIMEOUT_SECONDS
from Database.workers.periodic import WORKER_STOP_JOIN_TIMEOUT_SECONDS
from config import USER_STOP_JOIN_TIMEOUT_SECONDS
from services.email_worker import EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

#< Listener.stop() joins twice: spotapi's LastPlayed thread, then the poll thread
LISTENER_JOINS_PER_USER = 2
#< Database.stop() joins the five periodic workers (see WORKER_STOP_EVENT_NAMES)
PERIODIC_WORKERS_PER_USER = 5

PROCESS_WIDE_JOIN_BUDGET_SECONDS = (
    BACKUP_STOP_JOIN_TIMEOUT_SECONDS + EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS)
PER_USER_JOIN_BUDGET_SECONDS = (
    LISTENER_JOINS_PER_USER * LISTENER_STOP_JOIN_TIMEOUT_SECONDS
    + WATCHDOG_STOP_JOIN_TIMEOUT_SECONDS
    + PERIODIC_WORKERS_PER_USER * WORKER_STOP_JOIN_TIMEOUT_SECONDS)


def _graceSeconds(text: str):
    """The declared stop_grace_period in seconds, or None if there is none.
    A trailing `#<` comment is this repo's house style, so allow one."""
    match = re.search(r"^\s*stop_grace_period:\s*(\d+)s\s*(#.*)?$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


class TestComposeShutdownBudget(unittest.TestCase):
    def test_the_compose_file_declares_one(self):
        self.assertIsNotNone(_graceSeconds(COMPOSE_PATH.read_text(encoding="utf-8")),
                             "without it Docker allows 10s for the whole shutdown")

    def test_it_covers_the_whole_shutdown(self):
        """The process-wide workers plus phase 2's deadline - and phase 2 is
        the same length for one user or twenty, since they stop concurrently."""
        needed = PROCESS_WIDE_JOIN_BUDGET_SECONDS + USER_STOP_JOIN_TIMEOUT_SECONDS

        self.assertGreaterEqual(
            _graceSeconds(COMPOSE_PATH.read_text(encoding="utf-8")), needed,
            f"shutdown can spend {needed}s before it gives up; raise "
            "stop_grace_period, or lower whichever bound grew")

    def test_phase_twos_deadline_covers_a_user_that_is_behaving(self):
        """USER_STOP_JOIN_TIMEOUT_SECONDS is a backstop for a WEDGED user, so
        it has to sit above the joins a healthy stop() may legitimately spend -
        otherwise shutdown abandons users that were about to finish."""
        self.assertGreaterEqual(USER_STOP_JOIN_TIMEOUT_SECONDS, PER_USER_JOIN_BUDGET_SECONDS)



if __name__ == "__main__":
    unittest.main()
