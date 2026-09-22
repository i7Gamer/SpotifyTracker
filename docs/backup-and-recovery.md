# Backup and recovery

The [README](../README.md#backups) covers what is backed up and how to turn
automatic snapshots on or off. This is the rest: getting snapshots off the
machine, the encryption key they are useless without, taking one by hand, and
proving a restore actually works before you need it to.

`BACKUP_INTERVAL_HOURS` and `BACKUP_RETENTION_COUNT` are environment fallbacks.
Saved interval and retention values from the `/admin` backup settings take
precedence after restart, and the same effective values are used by the admin
form, scheduled worker and pre-upgrade snapshot. If either effective value is
`0`, automatic scheduled and pre-upgrade snapshots are disabled; **Create
Backup Now** remains available. `BACKUP_DIR` remains the environment-controlled
destination.

## Getting snapshots off the machine

`BACKUP_DIR` is how you fix that without any scripting of your own: point it at a path on another disk, or at a mount of somewhere off the machine entirely (a NAS, an SMB/NFS share, a cloud-storage mount). Mount that location into the container and name it here:

```yaml
    volumes:
      - ./Database/Data:/app/Database/Data
      - /mnt/nas/spotify-tracker-backups:/backups   #< or a second local disk, or a cloud mount
    environment:
      - BACKUP_DIR=/backups
```

The path is created if it does not exist, and rotation applies there just as it does to the default location. Verify that the mounted storage actually survives loss of the host. The application writes to this one destination; it does not configure a separate cloud mirror or independently retained remote copies. An existing host backup service can protect completed snapshots and retain them independently of the app's local rotation. Transfer completed `.db` snapshots only, never `.partial` files.

## The encryption key

**Keep the matching encryption key with your backups, in private storage off the host.** Save the key from the first source below that your deployment uses:

- `DATA_ENCRYPTION_KEY`, if set to a nonblank value.
- Otherwise, the nonblank `FLASK_SECRET_KEY` environment variable.
- If neither is set, copy `secrets/data_encryption_key.txt`.

These sources are checked in that order. A copy of the fallback file cannot recover credentials encrypted with an environment key. Save the deployment configuration that selects the key, and keep older keys for as long as retained snapshots need them. Keep this recovery copy accessible without the original host. Do not print keys in diagnostic output, commit them, or encrypt their only recovery copy with the application key being protected.

Changing the configured key does not re-encrypt existing values. Even a current snapshot can contain credentials written under several keys, including Spotify cookies/client secrets/refresh tokens, Last.fm keys and the SMTP password. The startup foreign-key count covers fingerprinted ciphertext only; a zero count cannot certify that older ciphertext decrypts. Without a matching key, listening history remains readable but affected credentials must be re-entered. An empty fallback key file is rejected; a missing one causes fresh-key creation, which cannot recover old credentials. Confirm the required key source exists before starting a restored application.

## Taking a snapshot by hand

You can also export your own play history from the Import & Export page (JSON in Spotify's extended-export format - re-importable through the form on that same page - or CSV).

To take a manual snapshot: the app runs the database in [WAL mode](https://www.sqlite.org/wal.html), so **don't just copy the `.db` file** while the container is running - recent writes can still be sitting in a separate `-wal` file that a raw copy would miss, producing a backup that's silently missing data or corrupt. Use SQLite's own online backup API instead, which is safe to run against a live, in-use database:

```bash
docker compose exec spotify-tracker python -c "import sqlite3; sqlite3.connect('/app/Database/Data/spotify_stats.db').backup(sqlite3.connect('/app/Database/Data/spotify_stats_backup.db'))"
```

This writes `spotify_stats_backup.db` into the same `Database/Data/` folder on your host machine (via the volume mount). Copy that file somewhere else - a different disk, cloud storage, etc. - for it to actually protect you against data loss, and rename or timestamp it before backing up again if you want to keep more than one snapshot.

Anyone holding both a database backup and its matching key can read the stored credentials, so keep both access-controlled even when transferring them separately.

## Verifying a restore

Verify recovery on an isolated copy before relying on it:

1. Download a completed `.db` snapshot, its matching key and deployment configuration from your off-host backup. Copy the snapshot into a new test directory as `Database/Data/spotify_stats.db`; leave the backup itself untouched. Restore `Database/Data/Media/` too if you backed it up. Keep the test application stopped and block its outbound network access so it cannot contact Spotify or send email.
2. From the test directory, run `python -c "import sqlite3; db = sqlite3.connect('file:Database/Data/spotify_stats.db?mode=ro', uri=True); print(db.execute('PRAGMA integrity_check').fetchone()[0])"`. This opens only an existing file, read-only, and must print `ok`. Compare user, track and play counts with the counts recorded when the snapshot was taken; investigate any mismatch before proceeding.
3. Restore the matching key to the environment variable or file listed above before opening the copy with application code. Check that a higher-priority environment variable will not override a restored file. Do not generate a replacement key: it cannot decrypt the old credentials.
4. With application workers and email disabled, test decryption of the stored fields in `users` (`cookies_json`, `spotify_client_secret`, `spotify_refresh_token`, `lastfm_api_key`) and the `smtp_password` app setting. Record only readable, unreadable and unset counts, never plaintext or ciphertext. Check older ciphertext by actually decrypting it; a zero startup foreign-key count is insufficient. If several keys are needed, check each separately: the running app uses one key at a time. Re-enter or separately re-encrypt unresolved credentials before returning the restored instance to service.
5. Record which snapshot you restored, its source timestamp, when it reached remote storage, how long recovery took, and any integrity, count or credential failures. A successful local snapshot or synthetic test is not a substitute for retrieving and checking your off-host copy.

## Monitoring

For routine backup monitoring, set a maximum snapshot age, retention period and recovery-time target. Measure age from the original snapshot timestamp, not its filename or upload time: rotation can rename files, and uploading an old snapshot again does not make it fresh. Preserve the source timestamp through transfers or record it in a verified manifest. Alert when that age exceeds your chosen limit.

## File permissions

Key files under `secrets/` are created (and, on an existing install, narrowed on the next start) to mode `0600`, inside a `0700` directory - owner only. That is not protection against the host itself being compromised, since the app reads them unattended at boot; it keeps them out of reach of other local accounts and out of an over-broad share. Windows hosts are the exception: Python's `chmod` there only sets the read-only attribute and never narrows the ACL, so restrict the folder yourself if the machine has other user accounts on it.
