# Recover a legacy account

Reached from the warning `dashboard/user_registry.py` logs when a login is
given a suffixed username instead of a legacy row's history.

> **Nothing tests the SQL below.** It used to be executed against disposable
> accounts by `tests/test_user_registry.py`; that test was removed. Run it on
> a copy of the backup first, and check `rows_updated` before committing -
> the guards in the `UPDATE` are the only thing standing between a typo and
> the wrong account.

Older databases can contain a user whose `email` is `NULL`. A later login whose
sanitized email prefix matches that username is deliberately assigned a suffixed
username instead of claiming history on the strength of a name. The app logs a
warning naming both usernames when this happens.

Before associating an email with a legacy row, verify that the requester controls
the Spotify account and target email and can identify the legacy history (for
example, its recent tracks and date range). A matching email prefix is not proof
of ownership. Stop the app, then take and retain a consistent SQLite backup with
your maintenance tool, preserving the matching encryption key. The running-container
backup command in [Backup and recovery](backup-and-recovery.md#taking-a-snapshot-by-hand)
cannot run after stopping the container. Keep the app stopped
throughout recovery; do not edit a live database.

Before an upgrade, identify accounts needing verified email association with
`SELECT username FROM users WHERE email IS NULL;`. Resolve verified mappings before
their next login; do not infer ownership for every returned row.

Inspect the exact legacy username and check the target email case-insensitively;
SQLite email lookups in the app use `NOCASE` too:

```sql
SELECT username, email
FROM users
WHERE username COLLATE NOCASE = 'ExactLegacyName'
   OR email COLLATE NOCASE = 'owner@example.com';
```

If exactly one matching legacy row has a `NULL` email and no row has the target
email, use a transaction whose guards enforce both facts:

```sql
BEGIN IMMEDIATE;
UPDATE users
SET email = 'owner@example.com'
WHERE username = 'ExactLegacyName'
  AND email IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM users
      WHERE email COLLATE NOCASE = 'owner@example.com'
  );
SELECT changes() AS rows_updated;
SELECT username, email FROM users WHERE username = 'ExactLegacyName';
```

Commit only when `rows_updated` is exactly `1` and the verification row is the
intended account; otherwise issue `ROLLBACK`. Restart the app after `COMMIT` and
verify that the login opens the expected history.

If the target email already belongs to the newly allocated suffixed account, stop
here: the guarded update correctly changes zero rows. Do not delete either user or
blindly merge/reassign them. Both usernames may already contain distinct history,
settings, credentials, shares, and imports. Recovery then requires an explicit
maintenance migration: keep exports and the stopped-database backup for both
users, inventory every username-owned table, decide how each conflicting setting
and credential should survive, migrate history with its table-specific duplicate
rules inside one transaction, run `PRAGMA foreign_key_check`, and compare both
histories before committing. Test that migration on a copy of the backup first or
ask the project maintainer to prepare it for the specific two accounts.
