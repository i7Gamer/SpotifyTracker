"""Every module here must import on its own, in any order.

Database.database imports Database.workers for its mixins, and the worker
modules read Database.database's module globals back (`_dbmod.logger`,
`_dbmod.parseError`, ...) so the suite's patch("Database.database.X") targets
reach them. That is a cycle, and it used to be survivable only in one
direction: importing Database.database FIRST left the package half-initialized
but working, while importing any worker submodule first raised

    ImportError: cannot import name 'WorkerLifecycleMixin' from partially
    initialized module 'Database.workers'

Which is not hypothetical - it bit Database/backup.py (imported standalone by
the migrators, before any of that exists) and then the test that reads the
join constants out of Database.workers.periodic.

Each import runs in its OWN interpreter: import order is process-global, and
by the time any test in this suite runs, conftest has long since imported
Database.database - so an in-process check would pass no matter what.
"""
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# One entry per import that must stand alone. Each is a module that either sits
# inside the cycle or is pulled in by something outside it.
FIRST_IMPORTS = (
    "Database.workers.periodic",
    "Database.workers.listener",
    "Database.workers.metadata_backfiller",
    "Database.workers.lastfm_backfillers",
    "Database.workers.wrapped_worker",
    "Database.workers",
    "Database.backup",          #< the migrators import this one standalone
    "Database.backup_settings", #< the migrators' dependency-light resolver
    "Database.Migrators.migrate1_32_0", #< settings seeding imports the leaf directly
    "Database.telemetry",
    "Database.metadata_repair",
    "Database.import_service",
    "Database.media_fetch",
    "Database.queries.merges",
    "Database.Listeners.spotifyListener",
    "Database.Importers.AutoImporter",
    "Database.database",        #< the direction that always worked - the control
)

IMPORT_TIMEOUT_SECONDS = 120   #< a cold import of this package tree, with slack

# The interpreters below are independent, so they run concurrently rather than
# one after another: serially this was the second-slowest test in the suite
# (~17s of the ~41s total), and xdist cannot help because parallelism there is
# ACROSS tests, not within one. Capped rather than unbounded - each child
# imports the whole package tree, and the suite is usually already running
# under `-n auto`, so this should not try to own the machine.
IMPORT_WORKERS = min(8, len(FIRST_IMPORTS))


def _importFirstInFreshInterpreter(moduleName):
    """Import `moduleName` into a brand-new interpreter; returns its result
    for the assertions to read, so failures are still reported per module."""
    return moduleName, subprocess.run(
        [sys.executable, "-c", f"import {moduleName}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        timeout=IMPORT_TIMEOUT_SECONDS,
        #< the app warns about an unset TZ on import; keep the check
        #  about importability, not about the environment
        env={**os.environ, "TZ": "UTC"},
    )


# Files inside the package that a developer is told, in the file itself, to run
# DIRECTLY. Running a file puts its own directory on sys.path - not the repo
# root - so a package module that reaches out to a top-level one breaks here
# while every import and every test still passes: pyproject sets
# `pythonpath = ["."]`, which puts the root on the path for the whole suite, so
# nothing running under pytest can see the difference.
#
# Deliberately NOT every `if __name__ == "__main__"` block in the tree: the
# migrators have one each and running those would migrate a real database.
SCRIPT_ENTRY_POINTS = (
    "Database/utils.py",   #< "`python Database/utils.py` drops into a REPL with the helpers loaded"
)


def _runAsScript(relativePath):
    """Run the file the way its own comment says to, in a fresh interpreter."""
    return relativePath, subprocess.run(
        [sys.executable, str(REPO_ROOT / relativePath)],
        cwd=REPO_ROOT, capture_output=True, text=True,
        input="",   #< utils.py ends in code.interact; EOF on stdin exits it
        timeout=IMPORT_TIMEOUT_SECONDS,
        env={**os.environ, "TZ": "UTC"},
    )


class TestDocumentedScriptEntryPointsStillRun(unittest.TestCase):
    """A module that says "run me like this" has to survive being run like that.

    Database/utils.py re-exports config.TRUTHY_ENV_VALUES so the Database
    package and the web layer cannot disagree about which env values mean "on".
    config.py sits at the repo ROOT, so the re-export only resolves when the
    root is on sys.path - which it is for every import of Database.utils, and
    for the whole test suite, but not for `python Database/utils.py`, where
    sys.path[0] is Database/ instead."""

    def test_each_documented_entry_point_starts(self):
        for relativePath in SCRIPT_ENTRY_POINTS:
            with self.subTest(script=relativePath):
                _, result = _runAsScript(relativePath)
                self.assertNotIn(
                    "ModuleNotFoundError", result.stderr,
                    f"`python {relativePath}` cannot resolve one of its imports:\n{result.stderr}")
                self.assertEqual(
                    result.returncode, 0,
                    f"`python {relativePath}` exited {result.returncode}:\n{result.stderr}")

    def test_backup_settings_supports_the_migrator_style_standalone_import(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import backup_settings; assert backup_settings.DEFAULT_BACKUP_INTERVAL_HOURS > 0"],
            cwd=REPO_ROOT / "Database", capture_output=True, text=True,
            timeout=IMPORT_TIMEOUT_SECONDS,
            env={**os.environ, "TZ": "UTC"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)


class TestModulesImportInAnyOrder(unittest.TestCase):
    def test_each_module_imports_first_in_a_fresh_interpreter(self):
        with ThreadPoolExecutor(max_workers=IMPORT_WORKERS) as pool:
            #< map re-raises a child's TimeoutExpired here, as the serial loop did
            results = list(pool.map(_importFirstInFreshInterpreter, FIRST_IMPORTS))

        for moduleName, result in results:
            with self.subTest(module=moduleName):
                self.assertEqual(
                    result.returncode, 0,
                    f"importing {moduleName} first failed:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
