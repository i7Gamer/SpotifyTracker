"""Database/secret_store.py - encryption-at-rest for stored secrets.

Spotify session cookies, API client secrets and refresh tokens live in the
shared SQLite file, which the README tells users to back up and copy around;
stored as plaintext, one leaked backup handed out every user's live Spotify
session. Values are Fernet-encrypted with a key that never lives inside the
database file itself (env var, or a file under secrets/).
"""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.secret_store as secretStore
from Database.secret_store import encryptSecret, decryptSecret, isEncrypted, ENCRYPTED_PREFIX
from config import PLACEHOLDER_FLASK_SECRET_KEY


class TestRoundTrip(unittest.TestCase):
    def test_encrypt_decrypt_round_trip(self):
        self.assertEqual(decryptSecret(encryptSecret("hello secret")), "hello secret")

    def test_encrypted_form_is_marked_and_unreadable(self):
        stored = encryptSecret('{"sp_dc": "super-secret-cookie"}')
        #< v2 now, which carries the writing key's fingerprint; isEncrypted
        #  spans both versions, which is what every caller actually asks
        self.assertTrue(stored.startswith(secretStore.ENCRYPTED_PREFIX_V2))
        self.assertNotIn("super-secret-cookie", stored)
        self.assertTrue(isEncrypted(stored))

    def test_unicode_survives_the_round_trip(self):
        self.assertEqual(decryptSecret(encryptSecret("pässwörd-日本語")), "pässwörd-日本語")


class TestLegacyAndEdgeCases(unittest.TestCase):
    def test_plaintext_passes_through_unchanged(self):
        """Values written before encryption existed have no prefix and must
        keep reading back as-is."""
        legacy = '{"sp_dc": "legacy-cookie"}'
        self.assertEqual(decryptSecret(legacy), legacy)
        self.assertFalse(isEncrypted(legacy))

    def test_none_reads_as_none(self):
        self.assertIsNone(decryptSecret(None))

    def test_undecryptable_value_reads_as_missing(self):
        """A prefixed value that can't be decrypted (garbage, or a rotated/
        lost key) must read as missing - routing the user through re-login -
        rather than raising or leaking the raw token."""
        self.assertIsNone(decryptSecret(ENCRYPTED_PREFIX + "not-a-real-token"))

    def test_value_encrypted_under_a_different_key_reads_as_missing(self):
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            stored = encryptSecret("secret")
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-two"}):
            self.assertIsNone(decryptSecret(stored))


class TestAnEmptyKeyFileFailsLoudly(unittest.TestCase):
    """An existing-but-empty key file used to be treated as "no key yet": the
    resolver fell through and MINTED A NEW ONE, over the top of the old path.
    Every enc:v1: value in the database then failed to decrypt, and decryptSecret
    returns None on failure, which callers read as "no cookies / no credentials
    stored" - so every user was silently bounced to re-login and their stored
    Spotify refresh tokens became unrecoverable, with nothing louder than a
    per-read warning.

    The state is reachable: the file is written with a plain truncate-then-write,
    so a crash or a full disk between the two leaves it empty.

    Refusing to start is the only safe answer - the same call the app already
    makes for the placeholder FLASK_SECRET_KEY. Restoring the file from a backup
    recovers everything; minting a key cannot be undone."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.keyPath = pathlib.Path(self._tmpdir.name) / "key.txt"
        patcher = patch.object(secretStore, "DEFAULT_KEY_PATH", self.keyPath)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._envPatcher = patch.dict(os.environ, {}, clear=False)
        self._envPatcher.start()
        self.addCleanup(self._envPatcher.stop)
        os.environ.pop(secretStore.ENCRYPTION_KEY_ENV_VAR, None)
        os.environ.pop(secretStore.FLASK_SECRET_KEY_ENV_VAR, None)

    def test_an_empty_key_file_raises_instead_of_minting_a_new_key(self):
        self.keyPath.write_text("", encoding="utf-8")

        with self.assertRaises(RuntimeError) as ctx:
            secretStore._keyMaterial()

        self.assertIn(str(self.keyPath), str(ctx.exception))
        self.assertEqual(self.keyPath.read_text(encoding="utf-8"), "",
                         "the empty file must be left alone, not overwritten with a new key")

    def test_a_whitespace_only_key_file_is_treated_the_same(self):
        self.keyPath.write_text("   \n", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            secretStore._keyMaterial()

    def test_a_missing_key_file_still_mints_one(self):
        """First run on a fresh install must keep working."""
        minted = secretStore._keyMaterial()

        self.assertTrue(minted)
        self.assertEqual(self.keyPath.read_text(encoding="utf-8").strip(), minted)

    def test_a_populated_key_file_is_returned_unchanged(self):
        self.keyPath.write_text("an-existing-key", encoding="utf-8")

        self.assertEqual(secretStore._keyMaterial(), "an-existing-key")

    def test_the_key_file_is_written_atomically(self):
        """The truncate-then-write is what creates the empty file above."""
        secretStore._keyMaterial()

        leftovers = [p.name for p in self.keyPath.parent.iterdir() if p != self.keyPath]
        self.assertEqual(leftovers, [], f"temporary artefacts left behind: {leftovers}")


class TestStoredSecretsNameTheirKey(unittest.TestCase):
    """`enc:v1:` carried no key identity, so decryptSecret could not tell "this
    was encrypted with a different key" from "this is corrupt" - both returned
    None, which every caller reads as "no credentials stored". The two states
    want opposite responses, and the first is the ordinary one: restoring a
    database backup without the matching secrets/ key file silently logs every
    user out and leaves their refresh tokens unrecoverable, with nothing saying
    why.

    Values now carry a short fingerprint of the key that wrote them. It is a
    truncated hash, not the key, and it discloses nothing an attacker holding
    the database couldn't already learn by trying to decrypt."""

    def test_new_values_carry_the_current_keys_fingerprint(self):
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            stored = encryptSecret("secret")

            self.assertTrue(stored.startswith(secretStore.ENCRYPTED_PREFIX_V2))
            self.assertIn(secretStore.keyFingerprint(), stored)

    def test_a_value_from_another_key_is_named_as_such(self):
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            stored = encryptSecret("secret")

        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-two"}):
            self.assertTrue(secretStore.isForeignKeyed(stored))
            self.assertIsNone(decryptSecret(stored))   #< still reads as missing to callers

    def test_a_value_from_the_current_key_is_not_foreign(self):
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            stored = encryptSecret("secret")

            self.assertFalse(secretStore.isForeignKeyed(stored))
            self.assertEqual(decryptSecret(stored), "secret")

    def test_a_supplied_fingerprint_matches_resolving_it_per_value(self):
        """isForeignKeyed takes an optional current fingerprint so a caller
        classifying many values resolves the key once - countSecretsUnderAnotherKey
        was reading the key file (behind _keyFileLock) for every secret column of
        every user. The shortcut must answer identically."""
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            mine = encryptSecret("secret")
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-two"}):
            theirs = encryptSecret("secret")
            current = secretStore.keyFingerprint()

            for value in (mine, theirs, "plain", None, "enc:v2:garbage"):
                with self.subTest(value=value):
                    self.assertEqual(secretStore.isForeignKeyed(value, current),
                                     secretStore.isForeignKeyed(value))

    def test_caching_the_derivation_still_honours_a_changed_key(self):
        """The SHA-256 derivations are memoized on the key MATERIAL, not on "the
        current key", so _keyMaterial is still consulted every call. If that ever
        became a cache of the resolved key, rotating it at runtime would silently
        keep using the old one - and every value written afterwards would carry a
        fingerprint that does not match the key that wrote it."""
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            firstFingerprint = secretStore.keyFingerprint()
            firstValue = encryptSecret("secret")
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-two"}):
            secondFingerprint = secretStore.keyFingerprint()

            self.assertNotEqual(firstFingerprint, secondFingerprint)
            self.assertIsNone(decryptSecret(firstValue))
        #< and switching back recovers it, which a stale cache could not do
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            self.assertEqual(secretStore.keyFingerprint(), firstFingerprint)
            self.assertEqual(decryptSecret(firstValue), "secret")

    def test_a_corrupt_value_is_not_reported_as_foreign(self):
        """Garbage under OUR fingerprint is damage, not a key mismatch - the
        distinction is the entire point."""
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            corrupt = f"{secretStore.ENCRYPTED_PREFIX_V2}{secretStore.keyFingerprint()}:not-a-token"

            self.assertFalse(secretStore.isForeignKeyed(corrupt))
            self.assertIsNone(decryptSecret(corrupt))

    def test_legacy_v1_values_still_decrypt(self):
        """Rows written before the fingerprint existed - the whole installed
        base. They can't be classified, only tried."""
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            legacy = secretStore.ENCRYPTED_PREFIX + secretStore._fernet().encrypt(b"secret").decode("ascii")

            self.assertTrue(isEncrypted(legacy))
            self.assertEqual(decryptSecret(legacy), "secret")
            self.assertFalse(secretStore.isForeignKeyed(legacy))   #< unknowable, so not claimed

    def test_plaintext_and_none_are_not_foreign_keyed(self):
        self.assertFalse(secretStore.isForeignKeyed(None))
        self.assertFalse(secretStore.isForeignKeyed('{"sp_dc": "legacy"}'))


class TestKeyResolution(unittest.TestCase):
    def test_env_key_takes_precedence_over_key_file(self):
        stored = encryptSecret("file-key-secret")   #< under the (test-isolated) key file
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "some-env-key"}):
            self.assertIsNone(decryptSecret(stored), "env key must win over the key file")

    def test_data_encryption_key_takes_precedence_over_flask_secret_key(self):
        env = {
            secretStore.ENCRYPTION_KEY_ENV_VAR: "dedicated-key",
            secretStore.FLASK_SECRET_KEY_ENV_VAR: "flask-key",
        }
        with patch.dict(os.environ, env):
            stored = encryptSecret("secret")
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "dedicated-key"}):
            self.assertEqual(decryptSecret(stored), "secret")
        with patch.dict(os.environ, {secretStore.FLASK_SECRET_KEY_ENV_VAR: "flask-key"}):
            self.assertIsNone(decryptSecret(stored))

    def test_flask_secret_key_is_used_when_no_dedicated_key(self):
        """Docker deployments already set FLASK_SECRET_KEY (the README example
        includes it) - reusing it means zero new configuration for them."""
        with patch.dict(os.environ, {secretStore.FLASK_SECRET_KEY_ENV_VAR: "flask-key"}):
            stored = encryptSecret("secret")
            self.assertEqual(decryptSecret(stored), "secret")


class TestPlaceholderKeyIsRefused(unittest.TestCase):
    """The README's compose example carries a commented-out
    DATA_ENCRYPTION_KEY=<placeholder> line. Uncommenting it without editing
    the value encrypts every stored Spotify session and API secret under a
    string published in this repo - which is the whole threat the encryption
    exists for, since it only ever protects a database file that has left the
    host. app.py already refuses to boot on FLASK_SECRET_KEY's placeholder for
    the same reason; this is the other half of that guard."""

    def test_the_shipped_placeholder_is_refused(self):
        with patch.dict(os.environ, {
                secretStore.ENCRYPTION_KEY_ENV_VAR: secretStore.PLACEHOLDER_DATA_ENCRYPTION_KEY}):
            with self.assertRaises(RuntimeError) as caught:
                secretStore._keyMaterial()
        self.assertIn(secretStore.ENCRYPTION_KEY_ENV_VAR, str(caught.exception))

    def test_surrounding_whitespace_does_not_smuggle_it_past(self):
        with patch.dict(os.environ, {
                secretStore.ENCRYPTION_KEY_ENV_VAR:
                    f"  {secretStore.PLACEHOLDER_DATA_ENCRYPTION_KEY}  "}):
            with self.assertRaises(RuntimeError):
                secretStore._keyMaterial()

    def test_a_real_key_is_unaffected(self):
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "a-real-random-key"}):
            self.assertEqual(secretStore._keyMaterial(), "a-real-random-key")

    def test_the_placeholder_is_only_checked_for_its_own_variable(self):
        """FLASK_SECRET_KEY has its own placeholder and its own guard in
        app.py; this one must not start rejecting values it does not own."""
        with patch.dict(os.environ, {
                secretStore.FLASK_SECRET_KEY_ENV_VAR: secretStore.PLACEHOLDER_DATA_ENCRYPTION_KEY}):
            self.assertEqual(secretStore._keyMaterial(),
                             secretStore.PLACEHOLDER_DATA_ENCRYPTION_KEY)

    def test_key_file_is_created_once_and_reused(self):
        self.assertFalse(secretStore.DEFAULT_KEY_PATH.exists())

        stored = encryptSecret("secret")

        self.assertTrue(secretStore.DEFAULT_KEY_PATH.exists())
        keyMaterial = secretStore.DEFAULT_KEY_PATH.read_text(encoding="utf-8").strip()
        self.assertTrue(keyMaterial)
        self.assertEqual(decryptSecret(stored), "secret", "a later call must reuse the same key file")


class _KeyFileTestCase(unittest.TestCase):
    """A key path inside a not-yet-existing secrets/ directory, so the helper
    has to create the directory as well as the file."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.keyPath = pathlib.Path(self._tmpdir.name) / "secrets" / "key.txt"

    def _writeExisting(self, contents):
        self.keyPath.parent.mkdir(parents=True, exist_ok=True)
        self.keyPath.write_text(contents, encoding="utf-8")

    def _mode(self, path):
        return path.stat().st_mode & 0o777


class TestReadOrCreateKeyFile(_KeyFileTestCase):
    """The file mechanics shared by the data encryption key and app.py's Flask
    session key. The two differ only in what an EMPTY file means, which is the
    one parameter: losing the Flask key logs everyone out (recoverable, so
    re-mint), losing the data encryption key strands every stored Spotify
    session for good (so refuse and say to restore it)."""

    def test_a_missing_file_is_minted_and_persisted(self):
        minted = secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertTrue(minted)
        self.assertEqual(self.keyPath.read_text(encoding="utf-8").strip(), minted)

    def test_an_existing_key_is_returned_stripped(self):
        self._writeExisting("  an-existing-key\n")

        self.assertEqual(secretStore.readOrCreateKeyFile(self.keyPath), "an-existing-key")

    def test_an_empty_file_is_reminted_when_no_error_is_supplied(self):
        self._writeExisting("")

        minted = secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertTrue(minted)
        self.assertEqual(self.keyPath.read_text(encoding="utf-8").strip(), minted)

    def test_a_whitespace_only_file_is_reminted_too(self):
        self._writeExisting("   \n")

        minted = secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertEqual(self.keyPath.read_text(encoding="utf-8").strip(), minted)

    def test_an_empty_file_raises_the_supplied_error_and_is_left_alone(self):
        self._writeExisting("")

        with self.assertRaises(RuntimeError) as ctx:
            secretStore.readOrCreateKeyFile(self.keyPath, emptyFileError="restore it from a backup")

        self.assertIn("restore it from a backup", str(ctx.exception))
        self.assertEqual(self.keyPath.read_text(encoding="utf-8"), "",
                         "minting over it would make every encrypted value unreadable")

    def test_a_stale_partial_from_a_crash_does_not_block_minting(self):
        """os.open(O_EXCL) refuses an existing file, and a killed process can
        leave the temp behind - so a crashed first boot must not wedge every
        boot after it."""
        self._writeExisting("")
        self.keyPath.unlink()
        stale = self.keyPath.with_name(self.keyPath.name + secretStore.PARTIAL_SUFFIX)
        stale.write_text("half-written", encoding="utf-8")

        minted = secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertEqual(self.keyPath.read_text(encoding="utf-8").strip(), minted)
        self.assertFalse(stale.exists())

    def test_nothing_is_left_behind(self):
        secretStore.readOrCreateKeyFile(self.keyPath)

        leftovers = [p.name for p in self.keyPath.parent.iterdir() if p != self.keyPath]
        self.assertEqual(leftovers, [], f"temporary artefacts left behind: {leftovers}")


class TestAMintThatFailsPartWay(_KeyFileTestCase):
    """_writeKeyFile's except arm: the only path here with no coverage.

    The write is deliberately atomic - temp file, then rename - because a plain
    write truncates first, so a crash between the two leaves exactly the empty
    file readOrCreateKeyFile has to refuse. That leaves one question this suite
    never asked: what the FAILING half does.

    A full disk is the realistic trigger, and os.replace is where it surfaces:
    the temp file is written and the rename is what cannot complete. What must
    then be true is that the error reaches the caller (a boot that could not
    persist its key must not look like a boot that did) and that the temp file
    goes, since it is not a key and nothing will ever read it.
    """

    def _failingReplace(self):
        return patch.object(secretStore.os, "replace",
                            side_effect=OSError("No space left on device"))

    def _partialPath(self):
        return self.keyPath.with_name(self.keyPath.name + secretStore.PARTIAL_SUFFIX)

    def test_the_failure_reaches_the_caller(self):
        with self._failingReplace(), self.assertRaises(OSError):
            secretStore.readOrCreateKeyFile(self.keyPath)

    def test_no_key_file_is_left_claiming_to_be_one(self):
        with self._failingReplace(), self.assertRaises(OSError):
            secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertFalse(self.keyPath.exists(),
                         "a half-mint must not leave something the next boot reads as the key")

    def test_the_half_written_temp_file_is_cleaned_up(self):
        with self._failingReplace(), self.assertRaises(OSError):
            secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertFalse(self._partialPath().exists())
        leftovers = [p.name for p in self.keyPath.parent.iterdir()]
        self.assertEqual(leftovers, [], f"left behind after a failed mint: {leftovers}")

    def test_the_next_boot_can_still_mint(self):
        """The behavioural payoff. os.open(O_EXCL) refuses an existing file, so
        a temp left behind by the arm above would wedge every boot after it -
        the same wedge test_a_stale_partial_from_a_crash_does_not_block_minting
        covers for a KILLED process, now for one that survived its own error."""
        with self._failingReplace(), self.assertRaises(OSError):
            secretStore.readOrCreateKeyFile(self.keyPath)

        minted = secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertTrue(minted)
        self.assertEqual(self.keyPath.read_text(encoding="utf-8").strip(), minted)

    def test_a_cleanup_that_cannot_delete_does_not_hide_why_the_mint_failed(self):
        """The removal runs inside the `except`, where a raised exception
        REPLACES the one being handled. This is a boot-time failure whose only
        symptom is the message the operator sees, and on Windows the moment a
        file has just been written is exactly when a scanner may still hold it
        - so "No space left on device" must not become a delete permission
        problem, which points at entirely the wrong fix.

        Same shape as Database/backup.py::_discardPartial."""
        realUnlink = pathlib.Path.unlink
        realExists = pathlib.Path.exists
        ourPartial = self._partialPath()

        def unlinkThatLoses(target, missing_ok=False):
            #< only THIS test's .partial, and only once it EXISTS: patch.object
            #  here is process-wide and ".partial" is a suffix three subsystems
            #  use, while _writeKeyFile also unlinks a not-yet-existing temp
            #  before O_EXCL - a delete of nothing is not what a scanner blocks
            if target == ourPartial and realExists(target):
                raise PermissionError("[WinError 32] used by another process")
            return realUnlink(target, missing_ok=missing_ok)

        with self._failingReplace(), \
                patch.object(pathlib.Path, "unlink", unlinkThatLoses), \
                self.assertRaises(OSError) as raised:
            secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertNotIsInstance(raised.exception, PermissionError)
        self.assertIn("No space left on device", str(raised.exception))

    def test_a_write_that_fails_before_the_rename_is_cleaned_up_too(self):
        """The other half of the try block. Patching the write rather than the
        rename also proves the descriptor is closed on the way out - Windows
        refuses to unlink a file that is still open, so a leaked handle fails
        this on that platform rather than passing everywhere."""
        realFdopen = secretStore.os.fdopen

        class FailingStream:
            def __init__(self, stream):
                self._stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._stream.close()
                return False

            def write(self, _contents):
                raise OSError("No space left on device")

        with patch.object(secretStore.os, "fdopen",
                          lambda *args, **kwargs: FailingStream(realFdopen(*args, **kwargs))), \
                self.assertRaises(OSError):
            secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertFalse(self._partialPath().exists())
        self.assertFalse(self.keyPath.exists())


@unittest.skipIf(os.name == "nt",
                 "CPython's os.chmod on Windows only toggles the read-only attribute and never "
                 "narrows the ACL, and st_mode is synthetic - there is no mode to assert on")
class TestKeyFilePermissions(_KeyFileTestCase):
    """A key file readable by every account on the host is one `cat` away from
    forged sessions and decrypted Spotify credentials. Not a defence against
    the host being compromised - the app reads the key unattended at boot, so
    whatever it can read as its own user, an attacker running as that user can
    too - but against other local accounts, an over-broad share ACL, and the
    key riding along in an archive or `docker cp`."""

    def test_a_minted_key_file_is_owner_only(self):
        secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertEqual(self._mode(self.keyPath), secretStore.KEY_FILE_MODE)

    def test_the_directory_it_creates_is_owner_only(self):
        secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertEqual(self._mode(self.keyPath.parent), secretStore.SECRETS_DIR_MODE)

    def test_the_key_is_never_world_readable_even_briefly(self):
        """Creating with the default mode and chmod'ing after leaves a window
        where the key is readable by anyone; the mode has to be set at
        creation, on the temp file the rename preserves."""
        observed = []
        realReplace = os.replace

        def captureThenReplace(src, dst):
            if str(src).endswith(secretStore.PARTIAL_SUFFIX):
                observed.append(os.stat(src).st_mode & 0o777)
            return realReplace(src, dst)

        with patch.object(os, "replace", side_effect=captureThenReplace):
            secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertEqual(observed, [secretStore.KEY_FILE_MODE])

    def test_an_existing_loose_key_file_is_tightened_on_read(self):
        """Installs predating this wrote the file with the default mode and
        never re-create it, so a fresh-install-only fix would never reach the
        files that are actually exposed."""
        self._writeExisting("an-existing-key")
        os.chmod(self.keyPath, 0o644)

        self.assertEqual(secretStore.readOrCreateKeyFile(self.keyPath), "an-existing-key")
        self.assertEqual(self._mode(self.keyPath), secretStore.KEY_FILE_MODE)

    def test_an_existing_loose_directory_is_tightened_on_read(self):
        self._writeExisting("an-existing-key")
        os.chmod(self.keyPath.parent, 0o755)

        secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertEqual(self._mode(self.keyPath.parent), secretStore.SECRETS_DIR_MODE)

    def test_an_already_narrower_file_is_left_alone(self):
        """0o400 grants less than we ask for; resetting it to 0o600 would be a
        downgrade of a deliberate choice."""
        self._writeExisting("an-existing-key")
        os.chmod(self.keyPath, 0o400)

        secretStore.readOrCreateKeyFile(self.keyPath)

        self.assertEqual(self._mode(self.keyPath), 0o400)

    def test_a_failure_to_tighten_is_survivable(self):
        """A key file owned by another account still has to be readable - the
        app going down is worse than the permissions staying loose."""
        self._writeExisting("an-existing-key")
        os.chmod(self.keyPath, 0o644)

        with patch.object(os, "chmod", side_effect=PermissionError("not the owner")):
            self.assertEqual(secretStore.readOrCreateKeyFile(self.keyPath), "an-existing-key")


if __name__ == "__main__":
    unittest.main()
