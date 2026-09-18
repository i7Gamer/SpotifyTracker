"""How this project reads on/off environment flags, pinned in one place.

Three conventions live here; the first two were broken by the same refactor.

The first is that "which strings mean on" has ONE answer. It briefly had two:
`config.TRUTHY_ENV_VALUES` accepted {1, true, yes, on} while
`Database.utils.TRUTHY_ENV_VALUES` accepted only {1, true}. Same name, different
members, both live and both imported by name - so ENABLE_HSTS=yes was on while
TOTP_AUTO_RECOVER=yes was off, and nothing in the code said so.

The second is that FLASK_DEBUG is read through `flaskDebugEnabled()` and nowhere
else. The commit that introduced that helper converted six of the seven sites;
the one it missed spelled the exact bug the helper exists to remove - a bare
truthiness test on the raw string, which reads FLASK_DEBUG=0 as ON.

The third is that an environment variable's NAME is spelled once, as a
`*_ENV_VAR` constant, and every read goes through it. Eleven names followed
that convention while three (SPOTIFY_CALLBACK_URL at five sites, and
SKIP_EMAIL_VERIFICATION and IMPORT_KEYWORD) were bare literals - a typo at
one of the five would have disabled backfill on that one path only.

All three gates are structural, because all three failures are invisible to a
behavioural test: a second copy of the set behaves identically until someone
edits one of them, a missed call site only misbehaves under an env var no test
sets, and a misspelled literal reads as "unset".
"""
import os
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from Database import utils as databaseUtils

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where the app's own Python lives. tests/ is excluded deliberately - a test may
# legitimately spell an env var it is exercising.
SOURCE_DIRS = ("Database", "routes", "dashboard", "services")
SOURCE_FILES = ("app.py", "config.py", "wsgi.py")

#< the only module allowed to read FLASK_DEBUG out of the environment
FLASK_DEBUG_OWNER = Path("Database") / "utils.py"

_LINE_COMMENT = re.compile(r"#[^\n]*")
_DOCSTRING = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')

#< any read of the variable, however it is spelled to get at the environment
_FLASK_DEBUG_READ = re.compile(r"""["']FLASK_DEBUG["']""")

#< `SOMETHING_ENV_VAR = "NAME"` at module scope - the one place a name is spelled
_ENV_VAR_DEFINITION = re.compile(r'^([A-Z][A-Z0-9_]*_ENV_VAR)\s*=\s*["\']([A-Z][A-Z0-9_]*)["\']', re.MULTILINE)

# Every name that has a constant. The scan below is driven off the definitions
# it finds, so without this floor a deleted constant would make its name
# vanish from the scan - and its literal sites pass by never being looked for.
ENV_VAR_NAMES_WITH_CONSTANTS = frozenset({
    "ALLOW_INSTANCE_RESTART", "TRUST_PROXY_HEADERS", "ADMIN_EMAIL", "ENABLE_HSTS",
    "BACKUP_INTERVAL_HOURS", "BACKUP_RETENTION_COUNT", "BACKUP_DIR",
    "SPOTIFY_TOTP_SECRET", "SPOTIFY_TOTP_AUTO_RECOVER",
    "DATA_ENCRYPTION_KEY", "FLASK_SECRET_KEY",
    "SPOTIFY_CALLBACK_URL", "SKIP_EMAIL_VERIFICATION", "IMPORT_KEYWORD",
})
# Read by the app without a *_ENV_VAR constant: FLASK_DEBUG through its one
# reader (the gate above), WAITRESS_THREADS by wsgi.py at the last moment
# before serve(). Listed so the documentation gate still knows about them.
ENV_VAR_NAMES_WITHOUT_CONSTANTS = frozenset({"FLASK_DEBUG", "WAITRESS_THREADS"})


def _productionSources():
    """Every .py file the running app is built from, as (relativePath, text)."""
    paths = []
    for directory in SOURCE_DIRS:
        paths.extend(sorted((REPO_ROOT / directory).rglob("*.py")))
    for name in SOURCE_FILES:
        paths.append(REPO_ROOT / name)
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        yield path.relative_to(REPO_ROOT), path.read_text(encoding="utf-8")


def _stripProse(text):
    """Comments and docstrings removed. Both gates below are about what the code
    DOES, and every one of these constants is discussed in prose somewhere."""
    return _LINE_COMMENT.sub("", _DOCSTRING.sub("", text))


class TestOneTruthyEnvValueSet(unittest.TestCase):
    def test_the_two_modules_share_one_object(self):
        """Not merely equal - the same object, so they cannot drift apart."""
        self.assertIs(databaseUtils.TRUTHY_ENV_VALUES, config.TRUTHY_ENV_VALUES)

    def test_yes_and_on_are_accepted(self):
        """The members the narrower copy was missing. A flag set to `yes` that
        silently means off is worse than one that rejects the spelling."""
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertIn(value, config.TRUTHY_ENV_VALUES)

    def test_off_spellings_stay_out(self):
        """The whole reason this is a set rather than a truthiness test."""
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                self.assertNotIn(value, config.TRUTHY_ENV_VALUES)

    def test_only_one_module_defines_it(self):
        definers = [
            str(path) for path, text in _productionSources()
            if re.search(r"^TRUTHY_ENV_VALUES\s*=", _stripProse(text), re.MULTILINE)
        ]

        self.assertEqual(
            definers, ["config.py"],
            "TRUTHY_ENV_VALUES must be defined once, in config.py (the module that "
            f"imports nothing); found {len(definers)} definitions: {definers}")


class TestFlaskDebugHasOneReader(unittest.TestCase):
    """FLASK_DEBUG is asked about through flaskDebugEnabled(), nowhere else.

    The helper was introduced to collapse six spellings into one, and the site
    it missed - Database/workers/listener.py's `if os.environ.get("FLASK_DEBUG")`
    - was the one that mattered most: a bare truthiness test on the raw string,
    on the hottest path in the app. "0" is a non-empty string, so an operator
    setting FLASK_DEBUG=0 to silence diagnostics turned that one ON, logging per
    ingest batch, per user, per cycle.

    Structural because the bug only shows under an env var no test sets, and
    because the next site to be written is the one this catches.
    """

    def test_only_the_owner_reads_it_from_the_environment(self):
        readers = sorted(
            str(path) for path, text in _productionSources()
            if _FLASK_DEBUG_READ.search(_stripProse(text))
        )

        self.assertEqual(
            readers, [str(FLASK_DEBUG_OWNER)],
            "FLASK_DEBUG must be read only by Database/utils.py's flaskDebugEnabled(); "
            f"these modules spell it themselves: {readers}")

    def test_the_helper_is_what_the_owner_exposes(self):
        """Guards the gate above: if the helper were renamed away, the scan
        would pass by finding nothing anywhere, which reads like success."""
        self.assertTrue(callable(databaseUtils.flaskDebugEnabled))

    def test_zero_is_off(self):
        """The bug the missed call site had, stated as behaviour."""
        with unittest.mock.patch.dict(os.environ, {"FLASK_DEBUG": "0"}):
            self.assertFalse(databaseUtils.flaskDebugEnabled())

    def test_surrounding_whitespace_does_not_hide_a_truthy_value(self):
        """Docker's --env-file and `-e KEY=VALUE ` both pass trailing/leading
        whitespace through untouched, so a value like "1 " reaches this reader
        exactly as an operator typed it. Five of the app's other env-flag
        readers (app.py:84/114/264, migrate1_32_0.py:54, patches.py:1352) do
        `.strip().lower()`; flaskDebugEnabled() did only `.lower()`, so the
        same FLASK_DEBUG=1 that turns debug logging on everywhere else read as
        off here."""
        for padded in ("1 ", " 1", "1\t"):
            with self.subTest(value=padded):
                with unittest.mock.patch.dict(os.environ, {"FLASK_DEBUG": padded}):
                    self.assertTrue(databaseUtils.flaskDebugEnabled())


class TestEnvVarNamesAreSpelledOnce(unittest.TestCase):
    """An environment variable's name lives in exactly one `*_ENV_VAR` constant
    and nowhere else in code.

    A bare literal at a read site is a name that can be misspelled without
    anything noticing - os.environ.get("SPOTFIY_CALLBACK_URL") is simply
    unset, and that one path quietly behaves as if the feature were off. A
    constant makes the typo a NameError at import.
    """

    @staticmethod
    def _definitions():
        """{name: relativePath} for every `*_ENV_VAR = "NAME"` in the app."""
        found = {}
        for path, text in _productionSources():
            for _constant, name in _ENV_VAR_DEFINITION.findall(_stripProse(text)):
                found[name] = str(path)
        return found

    def test_every_known_name_has_a_constant(self):
        """The floor for the scan below - see ENV_VAR_NAMES_WITH_CONSTANTS."""
        self.assertEqual(
            sorted(ENV_VAR_NAMES_WITH_CONSTANTS - set(self._definitions())), [],
            "these env var names have lost their *_ENV_VAR constant")

    def test_no_module_spells_a_named_variable_itself(self):
        definitions = self._definitions()
        offenders = []
        for path, text in _productionSources():
            code = _stripProse(text)
            for name, owner in definitions.items():
                if str(path) == owner:
                    continue
                if re.search(rf"""["']{name}["']""", code):
                    offenders.append(f"{path}: {name!r} (constant lives in {owner})")

        self.assertEqual(
            offenders, [],
            "env var names must be read through their *_ENV_VAR constant, not "
            f"spelled as a literal:\n" + "\n".join(offenders))


#< the block scanned for KEY names below; stops at the next line whose
# indentation returns to (or above) "environment:"'s own
_COMPOSE_ENV_BLOCK_HEADER = "environment:"
#< `- KEY=...` or `# - KEY=...`, active or commented, one per compose line
_COMPOSE_ENV_VAR_LINE = re.compile(r'^\s*#?\s*-\s*([A-Za-z_][A-Za-z0-9_]*)=')


class TestComposeFileDocumentsEveryEnvVar(unittest.TestCase):
    """docker-compose.yml is the file an operator actually copies and edits,
    and it is now the ONLY place a variable is guaranteed to be written down -
    the prose that also describes them is not checked by anything. A variable
    missing here (ALLOW_INSTANCE_RESTART and the BACKUP_* trio once were, as
    were DATA_ENCRYPTION_KEY, TRUST_PROXY_HEADERS, ENABLE_HSTS and
    ADMIN_EMAIL) is invisible to whoever deploys from it. Structural for the
    same reason as the gates above: an undocumented variable misbehaves
    nowhere, so nothing but a scan catches its absence."""

    @staticmethod
    def _composeEnvironmentBlockNames():
        """KEY names of every `- KEY=...`/`# - KEY=...` line inside
        docker-compose.yml's `environment:` block (active or commented)."""
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        block = []
        inBlock = False
        headerIndent = None
        for line in compose.splitlines():
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" "))
            if not inBlock:
                if stripped == _COMPOSE_ENV_BLOCK_HEADER:
                    inBlock = True
                    headerIndent = indent
                continue
            if stripped and indent <= headerIndent:
                break   #< back out to the block's own indentation or above
            block.append(line)

        names = set()
        for line in block:
            match = _COMPOSE_ENV_VAR_LINE.match(line)
            if match:
                names.add(match.group(1))
        return block, names

    def test_the_compose_file_lists_every_variable_the_app_reads(self):
        block, composeNames = self._composeEnvironmentBlockNames()
        self.assertTrue(block, "docker-compose.yml has no `environment:` block to scan")

        appNames = set(TestEnvVarNamesAreSpelledOnce._definitions()) | ENV_VAR_NAMES_WITHOUT_CONSTANTS

        self.assertEqual(
            sorted(appNames - composeNames), [],
            "these env vars are read by the app but missing from docker-compose.yml's "
            "environment: block (active or commented)")
