# Protected dashboard deployment

The app runs as one Python/Waitress process with multiple threads. Caddy accepts
public traffic, redirects HTTP to HTTPS, and obtains/renews TLS certificates.
SQLite stores original uploaded workbooks, password hashes and revocable sessions.
All signed-in staff can read, upload, replace, remove and export all fundraiser data.
There is no public sign-up. Create accounts from the server's terminal.

## Local use

Requires Python 3.10+:

```sh
python -m pip install -r requirements.txt
python manage.py create-user admin
python server.py --dev-http
```

Enter a unique 12–128-character password/passphrase at the hidden prompt. Visit
http://127.0.0.1:8000 and sign in. Local HTTP is restricted to loopback clients and
cannot be combined with proxy mode. The default production mode refuses plain
HTTP. The browser never stores passwords or session tokens in localStorage.

Data is saved in `data/ball_drop.sqlite3` beside the Python files. Set
`BALL_DROP_DATA_DIR` or pass `--data-dir PATH` to both administration and server
commands to use another disk. The folder must be writable and persistent. Do not
use a shared network filesystem, multiple app replicas, or multiple server
processes against this database: each process caches its imported workbook view.

## Public HTTPS with Docker Compose

The planned domain is `kwballz.work`; the local `.env` is configured for it.
The domain has not been registered yet. Register it and provision a hosting server
before following the public deployment steps below. Local use works without a domain.

Prerequisites: a server running Docker Compose, a domain or subdomain you control,
and inbound TCP ports 80/443 (UDP 443 is optional for HTTP/3). With GoDaddy, point
the domain's A record to the server's public IPv4 address. Add an AAAA record only
if the server is reachable over that IPv6 address. Do not expose port 8000.

1. Keep the prepared `.env` with `DOMAIN=kwballz.work`. On a fresh checkout, copy
   `.env.example` to `.env` and set that value, without a scheme or path. This
   configuration has no passwords in it.
2. Build and create the first account before starting the app:

   ```sh
   docker compose build
   docker compose run --rm app python manage.py create-user admin
   docker compose up -d
   ```

3. Open `https://kwballz.work` and sign in. Caddy's automatic certificate issuance
   requires working DNS and public access on ports 80/443. Buying a domain alone
   does not deploy the server or activate a certificate.

`compose.yaml` publishes only Caddy's ports. The app runs as an unprivileged user
on an internal network. Caddy forwards requests to the app's private port and sets
the forwarding headers. `--behind-proxy` trusts exactly one proxy; do not use it
with a publicly exposed Python port or an additional CDN/proxy without adjusting
and testing the trust configuration. `BALL_DROP_PUBLIC_ORIGIN` must match the
public HTTPS hostname exactly. Other hosts and insecure requests are rejected.

The named volumes `app_data`, `caddy_data` and `caddy_config` survive container
replacement and ordinary `docker compose down`. `app_data` holds the database;
the Caddy volumes hold TLS certificates and configuration. Do not use
`docker compose down -v` unless you intend to delete these volumes.

The supplied files prepare HTTPS deployment; they do not register a domain, modify
DNS, purchase hosting, or deploy to a live server automatically. For certificate
behavior see [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).

## Accounts and sessions

```sh
python manage.py create-user alex
python manage.py list-users
python manage.py reset-password alex
python manage.py delete-user alex
```

For Docker, prefix these with `docker compose exec app`, for example:

```sh
docker compose exec app python manage.py reset-password alex
```

Passwords are stored as salted scrypt hashes. There are no default passwords.
Resetting a password or deleting an account revokes its sessions immediately.
Signing out revokes the current session. Sessions expire after eight hours even
if the browser stays open. Five failed sign-ins from an address trigger a temporary
15-minute limit; restarting the app does not clear it. Staff sharing one public IP
also share this limit.

Cookies are HttpOnly and SameSite=Lax, and are Secure in HTTPS mode. State-changing
requests require a per-session CSRF token and reject a conflicting Origin header.
The account-management CLI is the recovery mechanism; no email reset service is
configured.

## Backups and restores

Persistent storage survives restarts; backups protect against disk loss. Save
backups outside the live data folder and copy them to secure off-server storage:

```sh
python manage.py backup /path/to/backups/ball-drop-2026-11-08.sqlite3
```

The command uses SQLite's online backup API, includes accounts and uploaded
workbooks, and removes sessions from the backup so restored users must sign in
again. It refuses to overwrite an existing backup. Protect backups like buyer data;
this implementation does not encrypt the database at rest.

For a running Docker deployment:

```sh
docker compose exec app python manage.py backup /tmp/ball-drop-backup.sqlite3
docker compose cp app:/tmp/ball-drop-backup.sqlite3 ./ball-drop-backup.sqlite3
docker compose exec app rm /tmp/ball-drop-backup.sqlite3
```

To restore locally, stop the app, keep the old data folder as a separate recovery
copy, create a fresh data folder, and copy the backup into it as
`ball_drop.sqlite3`. Start both administration and the server with `--data-dir`
pointing to that fresh folder. Do not overwrite an active SQLite database or mix
old `-wal`/`-shm` files with a restored backup.

For Docker, stop the app before restoring a backup into a new/replacement data
volume with filename `/data/ball_drop.sqlite3` and ownership UID 10001. Preserve
the old volume until the restored data has been verified.

## Verification

```sh
python -m unittest discover -s tests -v
```

The tests cover unauthenticated access, CSRF, login limits, logout and password-reset
revocation, session expiry, storage across app recreation, invalid replacements,
backups, HTTPS enforcement, cookies, and spreadsheet exports. They use temporary
databases and fictional workbooks and do not alter real staff accounts.
