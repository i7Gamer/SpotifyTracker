<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="static/images/brand/wordmark-on-dark.png">
    <img src="static/images/brand/wordmark-on-light.png" alt="SpotifyTracker" width="420">
  </picture>
</p>

<p align="center">
  Self-hosted Spotify listening history and statistics — <b>no Spotify Premium required</b>.
</p>

<p align="center">
  <a href="https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/tests.yml"><img src="https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/lint.yml"><img src="https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/lint.yml/badge.svg" alt="Lint"></a>
  <a href="https://github.com/i7Gamer/SpotifyStatsTracker/security/code-scanning"><img src="https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/github-code-scanning/codeql/badge.svg" alt="CodeQL"></a>
</p>

If you find it useful, consider giving the [repo](https://github.com/i7Gamer/SpotifyStatsTracker) a ⭐.

<img width="1438" height="2337" alt="SpotifyTrackerOverviewV3" src="https://github.com/user-attachments/assets/44009d72-edbe-4681-8d7d-3b6f4c2e3419" />
<img width="1438" height="2265" alt="SpotifyTrackerCompareV2" src="https://github.com/user-attachments/assets/c3495dfb-268b-48b2-8f83-4388fbda3ab8" />
<img width="1437" height="2373" alt="SpotifyTrackerGenres" src="https://github.com/user-attachments/assets/e34579e5-a397-43aa-97a0-102f4cb3974d" />
<img width="1438" height="1744" alt="SpotifyTrackerWrappedV5" src="https://github.com/user-attachments/assets/be8526a3-efdf-4a48-8912-384c53439ee2" />

## Features

- **Top Lists**: View your top songs, artists, and albums with detailed statistics, with tag and "full plays only" filters, and a rank-movement badge showing how far each entry has climbed or fallen — or that it is new — since the equal-length period before the one you're looking at (loaded after the list, so it costs you no waiting)
- **Listening History**: Browse your play history on a dedicated `/history` page with instant AJAX filtering, and track daily listening activity with a contribution-style streak calendar on the Dashboard - hover any day for its play count and how long you listened
- **Trend Insights**: Dashboard cards surface your current Obsession (a track in heavy recent rotation), Rediscovery (an old favorite you've come back to — or, when you have no comeback to show, a Fresh Find: the newest arrival you've already gone back to), and Forgotten Favorite (a former favorite you haven't played in a while)
- **Personal Tagging & Playlist Export**: Tag any song, artist, or album with your own free-text labels, then filter and download a tagged playlist (or a Wrapped year's Top 100) as CSV, M3U, or XSPF on the `/playlists` page - ready for Spotify or converters like Soundiiz and TuneMyMusic
- **Achievement Milestones**: Automatically celebrate lifetime play-count and listening-time thresholds, listening streaks, and each new all-time #1 artist - surfaced as a topbar badge and a pair of dashboard cards: Milestones (what you've earned) beside Next Milestones (progress toward the next ones), plus an opt-in email notification under Profile > Notifications for each new milestone (off by default)
- **Charts & Analytics**: Visualize your listening patterns and statistics with interactive charts, customizable trend bucket granularity (hour, day, week, month, year), a Top Genres breakdown once enough genre data has been backfilled (see Genre Insights below), and a Listening Behavior card (shuffle/offline/Incognito Mode shares, how plays ended, and platforms) built from extended streaming history - it stays hidden until you've imported a range that carries that data
- **Yearly Wrapped & Share Links**: Get a personalized recap of your yearly listening with category filters (Top Songs, Artists, Albums, Discovered Songs, Artists, Albums) plus top genres, and generate shareable links with custom expiration
- **Data Sharing & Comparison**: Request to share your listening stats with another user - once they accept, compare top songs/artists/albums, a taste-match score, and shared genres side by side on the Compare page. Your dashboard also shows what those people are playing right now, each chip linking straight to a comparison with them (instance-wide admin switch, plus a per-account opt-out under Profile > Account)
- **Genre Insights & Biographies**: Add a free Last.fm API key under Profile > Connections to backfill genre tags and rich artist/album biographies in the background (see [Genre Data](#genre-data-optional) below)
- **Detail Pages & Interactive Timeline**: Drill down into individual songs, artists, and albums with an interactive play history timeline (with date headers, time gaps, and skip filters), embedded Spotify player, detailed stats, and biographies, plus a "Refresh Last.fm Data" button - an artist or album page you've only ever heard one song from gets that same timeline instead of repeating the same card down the page
- **Admin Console**: Instance admins can monitor real-time worker health (auto-importer, Last.fm backfiller, backup worker, metadata backfiller), manage user sync states, inspect catalog backfill coverage, and configure instance-wide settings at `/admin` - which also warns when the files on disk have changed since the running process started, i.e. a deploy that was copied but never restarted (see [Upgrading](#upgrading-from-an-older-version)), warns when push listening is on for someone without a working Spotify API backfill to catch what push misses, and shows each listener's 30-day live-miss ratio (the share of promptly-available plays the live path missed and the API backfill had to recover)
- **Multi-File Import**: Import multiple Spotify data export files at once with progress tracking
- **Overview Page**: See total database statistics, your listening breakdown, API backfill configuration, and genre-backfill progress
- **Auto-Import**: Automatically import files from the 'auto-import' folder with optional keyword filtering
- **Cross-Linking**: Click on artist names to explore artist pages from any song, and album links to see album details
- **Sign Out Everywhere**: Staying signed in is per browser, so logging out only ends the session in front of you. "Sign Out Everywhere Else" under Profile > Account ends every other one at once - a phone you no longer have, a shared computer, a browser you can't get back to - and you stay signed in where you pressed it. A password reset does the same automatically

## Installation

Clone the repository and install the pinned dependencies:

```bash
git clone https://github.com/i7Gamer/SpotifyStatsTracker
cd SpotifyStatsTracker
pip install -r requirements.txt
```

## Run the Application

### Using Docker

The image is published for **linux/amd64 and linux/arm64**, so `docker pull`
picks the right one on an x86 server, a Raspberry Pi or other ARM SBC, an ARM
VPS, or an Apple Silicon Mac. No emulation, and no separate tag to remember.

A ready-to-adapt compose file:

```docker
services:
  spotify-tracker:
    image: i7gamer/spotify-tracker
    ports:
      - "5000:5000"
    volumes:
      - ./Database/Data:/app/Database/Data
      - ./autoImport:/app/autoImport  #< exports dropped here are picked up and imported on their own
    environment:
      - FLASK_APP=wsgi.py
      - PYTHONUNBUFFERED=1
      - TZ=America/Los_Angeles        #< set YOUR IANA zone, or every play lands at the wrong local time
      - FLASK_SECRET_KEY=changeme-generate-your-own-random-value  #< YOU MUST CHANGE THIS, and DO NOT comment it out - the app refuses to start on this exact placeholder (it's public, so it makes sessions forgeable). Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. A fixed value = sessions survive a restart. Also used to encrypt stored Spotify sessions unless DATA_ENCRYPTION_KEY is set - changing it means everyone must log in again, and leaving it unset in Docker loses those sessions for good (see the warning below).
      # - DATA_ENCRYPTION_KEY=changeme-another-random-value  #< Optional dedicated key for encrypting stored Spotify sessions/API secrets at rest (falls back to FLASK_SECRET_KEY). If you uncomment it, CHANGE THE VALUE - the app refuses to start on this exact placeholder, because it's public and would leave stored sessions readable by anyone holding your database. Keep it safe alongside your backups: without the key that encrypted them, stored sessions can't be read and every user must re-login with fresh cookies.
      # - TRUST_PROXY_HEADERS=1       #< Set when running behind a reverse proxy (nginx/traefik/caddy) so rate limiting sees real client IPs instead of the proxy's; use the number of proxy hops (usually 1). Only set this if a proxy is actually in front - otherwise clients could forge their IP.
      # - ENABLE_HSTS=1               #< Send a Strict-Transport-Security header so browsers pin this origin to HTTPS. Only enable behind a TLS-terminating reverse proxy - on plain-HTTP access it will lock browsers out of the site.
      # - ADMIN_EMAIL=you@example.com #< Makes this account the instance's only admin (grants access to the Admin Console at /admin to view all user sync states, worker health, and system settings). Without it, the earliest-registered user is promoted automatically.
      # - SPOTIFY_CALLBACK_URL=http://localhost:5000/spotify-callback  #< Uncomment and set to your public callback URL to enable Spotify Web API backfilling
      # - IMPORT_KEYWORD=Weekly       #< Uncomment to auto-import only files whose name contains this keyword
      # - FLASK_DEBUG=1               #< Verbose Flask logging - enable when reporting an issue
      # - WAITRESS_THREADS=16         #< Request-handling threads; default 16 (each dashboard page fans out several requests per user), floor 1. Only worth raising if pages queue up with many people browsing at once
      # - SKIP_EMAIL_VERIFICATION=1   #< Uncomment to disable the "do these cookies belong to this email" check at login (only do this if you trust everyone who can reach this instance - it's what stops one user from claiming another's account, AND what stops the /reset-password flow from letting anyone set a new password on any account)
      # - SMTP_SKIP_TLS_VERIFY=1   #< Uncomment ONLY for a self-hosted mail relay with a self-signed certificate: notification mail then skips certificate and hostname verification, which otherwise protects the SMTP password and every mail from a spoofed relay
      # - SPOTIFY_TOTP_SECRET=61:44,55,...  #< Emergency override for the pinned Spotify TOTP secret. Only needed if Spotify rotates it and logins start failing instance-wide before a fixed release is out - the log line at that point tells you so. Format is "<version>:<comma-separated bytes>"; a malformed value is ignored (with an error logged) rather than taking login offline.
      # - SPOTIFY_TOTP_AUTO_RECOVER=0 #< On by default: once 3 session-token requests in a row fail (the sign Spotify rotated that secret) the app reads the current one from Spotify's own web player and adopts it, retrying at most every 15 minutes. Set to 0 if you would rather nothing touched authentication on its own - then only SPOTIFY_TOTP_SECRET or a fixed release gets logins back.
    restart: always
    stop_grace_period: 45s          #< shutdown stops each user's listener/watchdog/workers in turn; Docker's 10s default would SIGKILL it partway. Free when shutdown is quick - it's a ceiling, not a wait
```

> **Set `FLASK_SECRET_KEY` (or `DATA_ENCRYPTION_KEY`) - don't leave both unset.** The compose file above mounts `Database/Data` and `autoImport`, but not `secrets/`. With neither variable set, the app generates `secrets/data_encryption_key.txt` *inside the container* and encrypts every stored Spotify session and API secret with it. The database survives in the mounted volume; that key does not survive `docker compose pull`, or any other recreate of the container. Listening history is unaffected, but every user has to log in with fresh cookies again and anyone using Web API backfilling has to re-enter their client secret - on every update, silently. The startup log names it when it happens ("stored secret(s) were encrypted with a DIFFERENT key"); pinning the variable is what prevents it.

Then you can run `docker compose up -d` and the app should start on `http://127.0.0.1:5000` or `http://yourIp:5000`

To update the container if an update is available, run `docker compose pull`

> **Note on scaling:** the app runs as a single process (Waitress/Flask). In-memory state - the per-IP auth rate limiter, the login-status cache, and the background worker pools - lives in that one process and is not shared, so run one instance rather than scaling it horizontally behind a load balancer.

### Upgrading from an older version

**Restart the app after updating the files.** Copying a new version over a
running instance is not a deploy: `static/` is read from disk on every request,
while the templates and the Python are held in memory from startup - so the
browser gets the new scripts and the server keeps serving the old markup. The
result is pages that half-work in ways that look like anything but an upgrade
problem. `docker compose pull` followed by `up -d` handles this; a manual file
copy does not. `/admin` shows a banner whenever the running process and the
files on disk disagree.

Listening history, tracks, images, and login sessions live in a single SQLite
database under `Database/Data/`. If you were relying on `secrets/` being mounted (e.g. so
`secrets/flask_secret_key.txt` persisted across restarts), set `FLASK_SECRET_KEY`
as shown above instead; otherwise everyone's login session resets on each container
restart - and, because that variable is also the fallback encryption key, the stored
Spotify sessions in the database stop being readable too (see the warning above).

### Local Development

Start the app directly and open it in a browser:

```bash
python app.py
```

The dev server listens on port 5444: `http://localhost:5444` (or the
machine's own IP, e.g. `http://127.0.0.1:5444`).

**Note:** The Docker container persists data in the `Database/Data/` directory on your host machine.

### Running the tests

The test dependencies are separate from the app's, and `pytest-xdist` is required
rather than optional — `pyproject.toml` sets `addopts = "-n auto"`, so without it
pytest refuses to start instead of merely running serially.

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Run serially when a failure is easier to read that way, or to keep `print`/`-s`
output in order (note that `-p no:xdist` does *not* work — it makes `-n` an
unrecognized argument):

```bash
pytest -n 0
```

Linting matches CI (`ruff check .` for Python, `npm run lint` for the browser
scripts in `static/js`, after `npm install`).

### Restarting the app (admin restart button)

The admin console has an optional **"Restart app to apply"** button — used after changing worker-pool sizes on the Advanced Tuning panel, since those only take effect on restart. It works by gracefully stopping background workers and exiting, so **something must relaunch the process**. It stays hidden unless you set `ALLOW_INSTANCE_RESTART=1`.

- **Docker** already relaunches on exit (`restart: always` in `docker-compose.yml`), so it is safe to enable there.
- **Running `python wsgi.py` directly**, wrap it in a supervisor that restarts on exit (a Windows service via NSSM, a Task Scheduler task, or a simple loop script such as `while ($true) { python wsgi.py }`) **before** enabling this — otherwise the button just stops the app with nothing to bring it back.

### Backups

Listening history, tracks, images, and login sessions all live in one SQLite file at `Database/Data/spotify_stats.db`.

The cover and artist image files themselves live beside it in `Database/Data/Media/` and travel with the database: keep that folder in the same backup. A database restored without it still works, but every image is re-downloaded on demand, one at a time as it is next viewed (or, for covers, next played).

**Automatic backups are on by default**: the app snapshots the database every 24 hours into `Database/Data/Backups/` (covered by the standard volume mount) and keeps the newest 7 snapshots. Tune or disable via environment variables:

```yaml
      # - BACKUP_INTERVAL_HOURS=24   #< how often to snapshot; 0 disables automatic backups
      # - BACKUP_RETENTION_COUNT=7   #< how many snapshots to keep; 0 disables automatic backups
      # - BACKUP_DIR=/backups        #< where snapshots go; default is Backups/ beside the database
```

`BACKUP_RETENTION_COUNT=0` also skips the one-off safety snapshot normally taken right before a version upgrade migrates the database - so if you rely on that instead of an external backup, take a manual snapshot from `/admin` before upgrading.

By default these snapshots live on the same disk as the database, so they protect against corruption and accidental deletion but not against losing the disk - one failure takes the database and every snapshot of it together.

`BACKUP_DIR` is how you fix that without any scripting of your own: point it at a path on another disk, or at a mount of somewhere off the machine entirely (a NAS, an SMB/NFS share, a cloud-storage mount). Mount that location into the container and name it here:

```yaml
    volumes:
      - ./Database/Data:/app/Database/Data
      - /mnt/nas/spotify-tracker-backups:/backups   #< or a second local disk, or a cloud mount
    environment:
      - BACKUP_DIR=/backups
```

The path is created if it does not exist, and rotation applies there just as it does to the default location. Verify that the mounted storage actually survives loss of the host. The application writes to this one destination; it does not configure a separate cloud mirror or independently retained remote copies. An existing host backup service can protect completed snapshots and retain them independently of the app's local rotation. Transfer completed `.db` snapshots only, never `.partial` files.

**Keep the matching encryption key with your backups, in private storage off the host.** Save the key from the first source below that your deployment uses:

- `DATA_ENCRYPTION_KEY`, if set to a nonblank value.
- Otherwise, the nonblank `FLASK_SECRET_KEY` environment variable.
- If neither is set, copy `secrets/data_encryption_key.txt`.

These sources are checked in that order. A copy of the fallback file cannot recover credentials encrypted with an environment key. Save the deployment configuration that selects the key, and keep older keys for as long as retained snapshots need them. Keep this recovery copy accessible without the original host. Do not print keys in diagnostic output, commit them, or encrypt their only recovery copy with the application key being protected.

Changing the configured key does not re-encrypt existing values. Even a current snapshot can contain credentials written under several keys, including Spotify cookies/client secrets/refresh tokens, Last.fm keys and the SMTP password. The startup foreign-key count covers fingerprinted ciphertext only; a zero count cannot certify that older ciphertext decrypts. Without a matching key, listening history remains readable but affected credentials must be re-entered. An empty fallback key file is rejected; a missing one causes fresh-key creation, which cannot recover old credentials. Confirm the required key source exists before starting a restored application.

You can also export your own play history from the Import & Export page (JSON in Spotify's extended-export format - re-importable through the form on that same page - or CSV).

To take a manual snapshot: the app runs the database in [WAL mode](https://www.sqlite.org/wal.html), so **don't just copy the `.db` file** while the container is running - recent writes can still be sitting in a separate `-wal` file that a raw copy would miss, producing a backup that's silently missing data or corrupt. Use SQLite's own online backup API instead, which is safe to run against a live, in-use database:

```bash
docker compose exec spotify-tracker python -c "import sqlite3; sqlite3.connect('/app/Database/Data/spotify_stats.db').backup(sqlite3.connect('/app/Database/Data/spotify_stats_backup.db'))"
```

This writes `spotify_stats_backup.db` into the same `Database/Data/` folder on your host machine (via the volume mount). Copy that file somewhere else - a different disk, cloud storage, etc. - for it to actually protect you against data loss, and rename or timestamp it before backing up again if you want to keep more than one snapshot.

Anyone holding both a database backup and its matching key can read the stored credentials, so keep both access-controlled even when transferring them separately.

Verify recovery on an isolated copy before relying on it:

1. Download a completed `.db` snapshot, its matching key and deployment configuration from your off-host backup. Copy the snapshot into a new test directory as `Database/Data/spotify_stats.db`; leave the backup itself untouched. Restore `Database/Data/Media/` too if you backed it up. Keep the test application stopped and block its outbound network access so it cannot contact Spotify or send email.
2. From the test directory, run `python -c "import sqlite3; db = sqlite3.connect('file:Database/Data/spotify_stats.db?mode=ro', uri=True); print(db.execute('PRAGMA integrity_check').fetchone()[0])"`. This opens only an existing file, read-only, and must print `ok`. Compare user, track and play counts with the counts recorded when the snapshot was taken; investigate any mismatch before proceeding.
3. Restore the matching key to the environment variable or file listed above before opening the copy with application code. Check that a higher-priority environment variable will not override a restored file. Do not generate a replacement key: it cannot decrypt the old credentials.
4. With application workers and email disabled, test decryption of the stored fields in `users` (`cookies_json`, `spotify_client_secret`, `spotify_refresh_token`, `lastfm_api_key`) and the `smtp_password` app setting. Record only readable, unreadable and unset counts, never plaintext or ciphertext. Check older ciphertext by actually decrypting it; a zero startup foreign-key count is insufficient. If several keys are needed, check each separately: the running app uses one key at a time. Re-enter or separately re-encrypt unresolved credentials before returning the restored instance to service.
5. Record which snapshot you restored, its source timestamp, when it reached remote storage, how long recovery took, and any integrity, count or credential failures. A successful local snapshot or synthetic test is not a substitute for retrieving and checking your off-host copy.

For routine backup monitoring, set a maximum snapshot age, retention period and recovery-time target. Measure age from the original snapshot timestamp, not its filename or upload time: rotation can rename files, and uploading an old snapshot again does not make it fresh. Preserve the source timestamp through transfers or record it in a verified manifest. Alert when that age exceeds your chosen limit.

Key files under `secrets/` are created (and, on an existing install, narrowed on the next start) to mode `0600`, inside a `0700` directory - owner only. That is not protection against the host itself being compromised, since the app reads them unattended at boot; it keeps them out of reach of other local accounts and out of an over-broad share. Windows hosts are the exception: Python's `chmod` there only sets the read-only attribute and never narrows the ACL, so restrict the folder yourself if the machine has other user accounts on it.

### Recover a legacy account

Older databases can contain a user whose `email` is `NULL`. A later login whose
sanitized email prefix matches that username is deliberately assigned a suffixed
username instead of claiming history on the strength of a name. The app logs a
warning naming both usernames when this happens.

Before associating an email with a legacy row, verify that the requester controls
the Spotify account and target email and can identify the legacy history (for
example, its recent tracks and date range). A matching email prefix is not proof
of ownership. Stop the app, then take and retain a consistent SQLite backup with
your maintenance tool, preserving the matching encryption key. The running-container
backup command above cannot run after stopping the container. Keep the app stopped
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

### Spotify Web API Backfilling (Optional)

To enable automatic backfilling of missed plays via the Spotify Developer API, you must configure the `SPOTIFY_CALLBACK_URL` environment variable:

1. Register an application in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Set the **Redirect URI** in your Spotify app configuration to match your public callback URL (e.g. `http://localhost:5000/spotify-callback`).
3. Set the `SPOTIFY_CALLBACK_URL` environment variable in your `docker-compose.yml` to this exact callback URL.
4. Once set, the Spotify Developer settings section will become visible under Profile > Connections, allowing you to link your account.

Fallback `Unknown track` metadata can be repaired when a later successful history
response or catalog backfill supplies the real track. These automatic paths need
working Spotify Developer API credentials and do not promise immediate repair.
History repair reuses the response already fetched, so it adds no HTTP request;
catalog repair stays within the existing cadence, cooldown, and batch budget. The
repair preserves historical play and skip fields. If old skips need to be
reclassified after durations are repaired, saving the admin skip settings runs
the existing bulk reclassification remedy.

### Genre Data (Optional)

Each user can add their own [Last.fm](https://www.last.fm) API key to have a background worker fetch genre tags for the artists, albums, and songs in their listening history:

1. Create a free key on the [Last.fm API account page](https://www.last.fm/api/account/create) (no Last.fm scrobbling account required).
2. Paste it into the Last.fm API Settings section under Profile > Connections.
3. A background worker starts fetching genre tags for your most-played artists, albums, and songs first, respecting Last.fm's request-rate limits. Once your own library is covered, it keeps helping backfill genres for everyone else's, since the artist/album/song catalog is shared across all users.
4. Track progress on the Overview page. Once enough of your history has genre data, genre breakdowns unlock on the Charts, Wrapped, and Compare pages.

Songs or albums Last.fm has no tags for inherit their artist's genres; the instance admin can toggle whether those inherited genres count towards backfill progress and genre stats from the Admin Console (`/admin`).

## License

This project is licensed under the **GNU Affero General Public License v3.0 or later** - see the [COPYING](COPYING) file for the full text.

In short: you may use, study, modify and redistribute it, but derivative works must stay under the AGPL, and **if you run a modified version as a network service, you must offer its source to that service's users** (AGPL-3.0 section 13).

Two things worth knowing:

- This project began as a fork of [TzurSoffer/SpotifyStatsTracker](https://github.com/TzurSoffer/SpotifyStatsTracker) and was itself MIT-licensed through version 1.45.0. Anything obtained at or before that point stays MIT - that grant cannot be withdrawn. The current tree no longer contains MIT-licensed portions: the last upstream-attributed code was rewritten in the Phase 2 dependency rewrite (see NOTICE for the history), so the former LICENSE.MIT file is gone.
- The switch away from MIT was not purely a preference. `spotapi`, a required runtime dependency that `Database/Spotify/` builds on and `Database/patches.py` patches, is licensed under the GPL-3.0, and the Docker image bundles it. A copyleft license for the project as a whole is what that dependency requires.

See [NOTICE](NOTICE) for the relicensing history and third-party component details.

## Support

Questions and bug reports are welcome as GitHub issues on this repository.

Additional Screenshots:

<img width="1437" height="2297" alt="SpotifyTrackerAdmin" src="https://github.com/user-attachments/assets/8aebd4be-041b-4991-9a0f-a85e88800a30" />
<img width="1438" height="867" alt="SpotifyTrackerTopSongsV2" src="https://github.com/user-attachments/assets/a7bb6a78-fd65-47ea-a0f6-bfecca2498a9" />
<img width="1438" height="2308" alt="SpotifyTrackerSongSubpageV2" src="https://github.com/user-attachments/assets/328ab683-b803-4671-8749-bc13e579d53c" />
