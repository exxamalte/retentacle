# Retentacle

[![Build Status](https://github.com/exxamalte/retentacle/actions/workflows/tests.yml/badge.svg)](https://github.com/exxamalte/retentacle/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/exxamalte/retentacle/graph/badge.svg?token=47OELOAUP4)](https://codecov.io/gh/exxamalte/retentacle)

Retentacle is a long-running backup server and command line tool for Shelly
devices. It fetches
the configuration of Shelly devices, stores snapshots in a local TinyDB
database, and provides a web UI for browsing backups per device. This might be
useful if a device loses its configuration for whatever reason. Restoring
settings to a device is not supported.

* Support for generation 1, 2 and 3 devices.
* Scheduled backups with a selectable next run time and interval.
* Configurable retention for monthly, weekly, daily, and hourly backups.
* Web UI for browsing devices, backup history, and individual backup snapshots.
* User accounts with admin and read-only roles, created on first startup.
* Optional OpenID Connect single sign-on with automatic user provisioning.
* One-shot backup mode for scripts and cron jobs.
* Auto-discovery of Shelly devices via mDNS (`--discover`).
* Fetches device configuration:
  * Gen 1: Settings, actions and status
  * Gen 2 and Gen 3: Configuration, device info and status
* No support for authenticated access to devices.
* No restore capability.

## Installation

Retentacle is distributed as a regular Python package. Installing it puts the
`retentacle` command on your PATH — this is the command used in all examples
below (equivalently, invoke it as `python -m retentacle`):

```commandline
pip install .
```

Tip: [`pipx`](https://pipx.pypa.io) installs the command into an isolated,
own virtual environment if you prefer not to pollute your system Python.

For development, install the test dependencies as well:

```commandline
pip install -e ".[tests]"
```

There are no standalone executables or binary downloads — running the app
requires a Python 3.14+ environment (or use the container image).

To run Retentacle as a container service instead, see [Docker](#docker).

## Running the server

Start the server:

```commandline
retentacle
```

Alternatively, run it as a Python module:

```commandline
python -m retentacle
```

By default the web server listens on `http://127.0.0.1:8080` and runs a backup
every hour. Open that address in a browser for the first time to create the
initial admin account, then sign in and add your device IP addresses on the
**Settings** page. Alternatively, list devices under `ip_addresses` in a
configuration file. The first backup starts immediately after startup;
subsequent backups run at the configured interval. If the computer was off or
asleep at a scheduled time, the missed backup runs right away at the next
startup. While the server runs, due slots are re-checked against the wall
clock every minute, so a schedule missed during laptop sleep or a clock
change is caught up promptly without a restart. Your chosen slot is sticky: scheduled backups always happen on the
grid of the date and time selected on the Settings page — e.g. daily at 3am —
no matter when a run actually started or finished, and restarts, catch-up
runs, and manual backups never move later runs off that schedule. Changing
only the repeat interval keeps that clock time as well: going from hourly to
daily stays on the grid's own hour instead of freezing the daily backup at
whatever minute the Settings page was opened, and the confirmation message
names the resulting date and time.

### Server options

Devices and the backup interval are configured in the web UI (or a
configuration file); the remaining startup options are command line flags:

| Option | Description |
| --- | --- |
| `--discover` | Auto-discover Shelly devices via mDNS. |
| `--target-folder` | Folder where the TinyDB databases are stored. Defaults to the current folder. |
| `--configuration` | YAML configuration file. |
| `--host` | Host the web server binds to. Defaults to `127.0.0.1`. |
| `--port` | Port the web server listens on. Defaults to `8080`. |
| `--once` | Run a single backup and exit instead of starting the server. |

To expose the web UI on your local network, bind to all interfaces:

```commandline
retentacle --host 0.0.0.0 --port 8080
```

The server requires a login for all pages except `/health`. Only expose it on
networks you trust.

### Users and roles

The web server ships with simple built-in user management:

* On the first start — while no user accounts exist — every page redirects to a
  setup wizard where you create the initial **admin** account. You are then
  signed in automatically.
* Accounts are identified by **email address**, which also links them to
  single sign-on identities (see below).
* **Administrators** can do everything: browse and download backups, run
  backups, change settings, and manage user accounts in the Users section of
  the Settings page.
* **Read-only** users can look around: they can browse devices, view the backup
  history, and download backups, but they cannot change any settings or take
  new backups. The admin-only buttons and pages are hidden from them, and the
  server rejects their requests to those routes.
* Additional accounts — with either role — are created by admins on the
  **Settings** page. Passwords must be at least 12 characters long and include at
  least one digit and one special character. They are stored as salted PBKDF2
  hashes, never in plain text. The last admin account can neither be deleted
  nor demoted, and admins cannot delete their own account.

Login sessions are stored server-side and referenced by HTTP-only cookies.
They expire after 30 days. Changing a password signs that user out of all
sessions.

Signed-in users get an avatar in the top right corner instead of their email
address and a sign-out button. Clicking it opens a menu with the account name
and role at the top, a link to the Settings page (admins only), the sign-out
action, and the app version at the bottom. The avatar shows the profile
picture from the OIDC provider when the provider sends a `picture` claim —
either in its userinfo response or in the ID token — and falls back to the
first letter of the email address otherwise. If the picture stays missing,
add the scope your provider requires for avatars to the **Scopes** field
(e.g. Keycloak only sends `picture` with the `microprofile` scope or a
dedicated client scope/mapper); the claim is picked up on the next OIDC
sign-in.

### OpenID Connect single sign-on

The Settings page has a **Single sign-on (OpenID Connect)** section to let
users sign in through an external identity provider:

* Enable it and enter the **issuer URL**, **client ID**, and **client secret**
  of a confidential OIDC client registered for this app. All endpoints are
  discovered automatically from the issuer's well-known OpenID configuration,
  and the authorization code flow is used. Register the redirect URI
  `https://<your-host>/oidc/callback` at the provider; leave the optional
  **Callback URL** field empty to derive it from the current request, or set
  it to the exact registered redirect URI when running behind a reverse proxy.
* Optionally set a **provider name**: the login button then reads
  "Sign in with <name>" instead of "Sign in with your account provider".
* Users are matched to local accounts by their **email claim**. Signing in
  with the same email address as a local account therefore links to it — the
  easiest way to bootstrap an admin: create the local admin account during
  setup with your provider email, then sign in via OIDC.
* Optionally turn on **automatically create accounts for new OIDC users**.
  New accounts are created as read-only; promote them in the Users section.
* Optionally **disable local password sign-in**. The provider connection is
  verified when you save, so an unreachable issuer cannot lock you out; the
  login page then offers only the provider button and local password requests
  are rejected.
* Identity is anchored on the provider's userinfo endpoint, which the app
  calls with the just-exchanged access token; unverified email claims are
  refused.

Note that the OIDC **client secret is stored in the settings database**
(`retentacle_settings.json`) as part of the web settings, so keep the target
folder protected. If you disable password sign-in and your provider becomes
unreachable, you can re-enable it by deleting `retentacle_settings.json` (or
removing the `oidc` entry from its `settings` document) and restarting the
server — your device backups remain untouched.

### Update notifications

When a newer version is published, admins see a warning-toned badge in the
header (e.g. **v2026.6.0 available**) linking to the release page; read-only
accounts never see it. The badge appears once a check has completed — checks
run in the background when a signed-in user views the dashboard or the
Settings page, and results are cached for 6 hours, so the app works fine
without internet access (it just never shows the badge).

Requirements and options:

* **GitHub releases are required.** The check calls the GitHub Releases API
  (`/releases/latest`) of the project repository. The **Release workflow**
  published in this repository automatically creates a GitHub Release from
  every pushed `v*` tag, so tagging a version is all it takes — no manual
  release step.
* **Pre-releases are never announced.** Only plain numeric versions such as
  `v2026.6.0` can light up the badge. Tags with any appended suffix —
  `-dev`, `-alpha`, `-beta`, `-rc`, `.dev1`, `b1` and similar — are
  classified as pre-releases: the Release workflow creates them marked as
  pre-release (and keeps them out of GitHub's "latest"), and the in-app
  check defensively ignores any release flagged `prerelease` or `draft`.
* To cut a release, bump the version in `pyproject.toml`, tag and push:
  * stable: `v2026.6.0` → full Release, becomes "latest", triggers the badge
  * preview: `v2026.6.0-beta1` (or `-alpha`, `-rc`, `b1`) → pre-release
    Release; existing installs are not prompted to update to it
* The project repository is public, so no token is needed. It talks to
  `api.github.com` anonymously (at most ~4 requests/day thanks to the cache,
  far below the 60/hour limit). For a **private** repository (e.g. your own
  fork with releases), set `RETENTACLE_UPDATE_TOKEN` to a token with read
  access to that repo's contents.
* Environment variables (also usable in Docker/compose):
  * `RETENTACLE_UPDATE_REPO` — check a different `owner/repo` (default:
    `exxamalte/retentacle`).
  * `RETENTACLE_UPDATE_TOKEN` — bearer token for private repositories.
  * `RETENTACLE_UPDATE_CHECK=0` — disable update checks entirely.

### Web UI

* `/` - devices overview with scheduler status, backup counts, last automatic backup, total backup size on disk, the duration of the last automatic backup, next run time, and per-device indicators for automatic backups and recent backups.
* `/devices/<ip-address>` - all backups for one device, newest first, with per-backup configuration chips, an "unchanged" marker for identical configs, plus links to the device web UI and to restoring the latest backup. Admins can also add or remove that single device from the automatic-backup list right from this page.
* `/devices/<ip-address>/compare?a=<id>&b=<id>` - leaf-level diff of the restorable configurations of two backups (changed / added / removed, credentials masked, live state excluded); defaults to the newest pair.
* `/backups/<id>` - metadata, a manual restore section, a configuration summary, the device state at backup time, and raw JSON for a single backup.
* `/backups/<id>/download` - download a backup as JSON.
* `/backups/<id>/restore.json` - download only the restorable configuration, ready for `Shelly.SetConfig` (Gen 2/3) or `POST /settings` (Gen 1).
* `/settings` - configure the backup schedule, device IP addresses, and single sign-on, and manage user accounts, roles, and passwords (admin only).
* `/setup` - first-run wizard to create the initial admin account.
* `/login`, `/logout` - sign in and out.
* `/oidc/login`, `/oidc/callback` - OpenID Connect single sign-on flow.
* `/api/status` - scheduler status and live backup progress as JSON.
* `/health` - server and scheduler status as JSON (the only page that does not require a login).
* `/users` - legacy alias that redirects to `/settings`.

### Restoring from a backup

Retentacle does not push settings to devices, so the backup page is built
around a fast manual restore. Every backup is triaged into three zones:

1. **Manual restore** — the restorable payload (`settings.configuration`
   for Gen 2/3, `settings.settings` for Gen 1) with one-click copy, a
   dedicated `restore.json` download, a four-step checklist, and a
   ready-made `curl` command pre-filled with the device's IP, the correct
   endpoint (`rpc/Shelly.SetConfig` or `/settings`) and the download's
   filename. Adjust the IP and run it next to the downloaded file.
2. **Configuration** — the same payload as a human-readable table,
   grouped by device section (System, Wi-Fi, MQTT, Switch 0, …), with
   credential values masked and copy buttons on every value.
3. **Device state** — the live `status` (and Gen 1 `actions`) recorded at
   backup time, collapsed and marked *information only — never restored*.

Typical flow after losing a device: open the device history and find the
last backup before the unwanted change — the list shows each backup's Wi-Fi
SSID, MQTT state, component counts and firmware as chips, and collapses rows
whose configuration matches the newer one to "unchanged" — then hit
**Restore latest** or open the chosen backup. The **Diff** link on each
history row compares that backup's configuration with the next newer one —
changed, added and removed settings are listed separately (credentials stay
masked, live state is never compared), so the right restore point is easy to
pick. Then download the restore JSON, adjust the IP in the shown command and
run it, and take a fresh backup in Retentacle to verify. Model, MAC and
firmware are shown for reference only; they never appear in a restore payload.

The dashboard header offers **Run backup now** (admins only) — it backs up
the devices configured on the Settings page; if none are configured there, it
backs up all devices currently listed in the overview, falling back to the
configured or discovered devices when the overview is empty. Next to it,
**Add devices** jumps straight to the device section of the Settings page. On
a fresh install both actions also appear in the "No backups yet" panel.
Each device row in the overview offers its actions — view backups, open the
device web UI, and back up this device — as compact icon buttons with
tooltips and screen-reader labels, so the table stays readable without
horizontal scrolling. The device detail page keeps these as labelled buttons,
including **Back up this device**, to back up just that device without
waiting for the next scheduled run.

Each device in the overview shows whether it is included in automatic backups
and whether its latest backup is recent. A backup is considered recent when it
is within the configured automatic backup interval (plus a short grace period),
or within the last 24 hours when automatic backups are disabled. The
**Last automatic backup** card follows scheduled runs only — manually started
backups, including single-device ones, do not move it.

While no device is configured, scheduled runs are skipped with a line in the
log instead of recording an empty automatic backup, and **Run backup now**
points out that there is nothing to back up rather than reporting a finished
run.

Transient messages — a started or finished backup, saved settings, a failed form — appear as auto-dismissing toast notifications in the lower-right corner, overlaying the page instead of shifting tables and buttons down. They stack, close on click, and announce themselves politely to screen readers.

Shelly devices usually serve their own configuration page over HTTP, so each
device's web UI can be opened in a new tab from the overview icon or the detail
page link. It opens `http://<ip-address>`; the backup tool does not proxy or
reach the device itself.

While a backup is running, the page shows a live progress bar and updates the
status automatically. Once the run finishes, the page reloads so newly created
backups appear without a manual refresh.

The **Settings** page lets you turn automatic backups on or off, choose the
date and time of the next run, and select how often backups repeat. It also lets
you add or remove device IP addresses while the server is running. Devices added
there are used for scheduled and manual backups and take precedence over the
configuration file. Changes take effect immediately and are stored in the
settings database, so they survive a restart. Settings saved in the web UI
take precedence over the configuration file.

The Settings page also configures backup retention separately for each device.
It keeps the newest backup from each month, week, day, and hour, and removes
older backups after the next successful backup. The defaults keep 12 monthly,
4 weekly, 7 daily, and 24 hourly backups. A single backup can count towards
several periods. Set a period to `0` to keep none for it; at least one period
must remain above zero.

## Docker

Retentacle publishes a multi-architecture (amd64 and arm64) container image
to the GitHub Container Registry. Images live under the full name
`ghcr.io/exxamalte/retentacle`:

```commandline
docker pull ghcr.io/exxamalte/retentacle:main
docker run -d --name retentacle -p 8080:8080 -v retentacle-data:/data \
  ghcr.io/exxamalte/retentacle:main
```

Tags work as follows: proper releases (a plain numeric `vX.Y.Z` git tag)
publish the version (e.g. `2026.11.0`) and update `latest`. Development
builds on the default branch publish the current application version from
`pyproject.toml` (e.g. `2026.11.0-dev`), plus `main` and `sha-<commit>`
identifiers — they never touch `latest`. Pre-release tags (any suffix such
as `-alpha`, `-beta`, `-rc`, `-dev`, `.dev1` or `b1`) publish only their tag
name and `sha-<commit>`, never `latest`.

Until the first proper release of a version, `latest` therefore points at the
previous stable build, not the newest one. Use the moving `main` tag, or pin
an exact version such as `2026.11.0-beta2`, to get current code; `main` and
tag builds are published by the *Container* workflow, which runs on every
version tag and can be started manually for `main`.

The Docker files live in the `docker/` folder: a `Dockerfile`, an entrypoint
script, a `docker-compose.yml` and the ignore rules for the build context.

With UI managers like Portainer, either create a volume in the Volumes tab
and map it to `/data`, or set the `PUID`/`PGID` environment variables to the
owner of your host folder — no terminal needed in either case.

Build and start the container with Docker Compose:

```commandline
docker compose -f docker/docker-compose.yml up -d --build
```

Or with plain Docker — the build context must be the repository root, while
the Dockerfile lives in `docker/`:

```commandline
docker build -t retentacle -f docker/Dockerfile .
docker run -d --name retentacle -p 8080:8080 -v retentacle-data:/data retentacle
```

Then open `http://localhost:8080` and complete the first-run setup wizard to
create the admin account. The container runs as a non-root user, listens on
all interfaces, and reports health via the public `/health` endpoint.

* **Data** — everything Retentacle stores (the `retentacle_backups.json`,
  `retentacle_settings.json` and `retentacle_users.json` databases) lives in
  `/data` inside the container. Keep it on a named volume, as in the compose
  example, or bind-mount a host folder. Since the entrypoint chowns the data
  folder at startup, a fresh (root-owned) bind mount needs no manual setup;
  on read-only or NFS mounts where chown cannot work, match `PUID`/`PGID` to
  the folder's owner instead.
* **Configuration via environment** — startup options are configured with
  environment variables instead of hand-written commands:

  | Variable | Default | Purpose |
  | --- | --- | --- |
  | `RETENTACLE_HOST` | `0.0.0.0` | Bind address inside the container |
  | `RETENTACLE_PORT` | `8080` | HTTP port inside the container (the health check follows it; adjust the port mapping, e.g. `-e RETENTACLE_PORT=9000 -p 9000:9000`) |
  | `RETENTACLE_DATA_DIR` | `/data` | Where the databases are stored. The image pre-creates this default with the right ownership, so an empty named volume mounted at `/data` "just works" (the Docker `VOLUME` instruction cannot follow the variable, so the mount hint stays `/data`) |
  | `PUID` / `PGID` | `10001` | User/group the server runs as. The container starts as root, **takes ownership of the data folder once at startup**, then drops to these IDs — so fresh root-owned host bind mounts (the default when Docker creates them) work without any manual `chown` on the host. Set them to your host folder's owner, e.g. `PUID=1000 PGID=1000`, to keep files directly readable/editable as your user. Note that these variables only work while the container starts as root: a `user:` setting (or the **User** field in Portainer) overrides them and disables the ownership repair, see below |
  | `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse-proxy support, see below |
  | `RETENTACLE_UPDATE_CHECK` / `RETENTACLE_UPDATE_REPO` / `RETENTACLE_UPDATE_TOKEN` | — | Update check, see below |

  Any extra CLI flags still work and are appended after the generated ones,
  e.g. `docker run ... retentacle --discover` or
  `docker run ... -v retentacle-data:/data retentacle --configuration /data/configuration.yaml`.
* **Reverse proxy (Traefik, nginx, Caddy)** — terminate TLS at the proxy and
  let Retentacle serve plain HTTP. Set `FORWARDED_ALLOW_IPS` so the server
  trusts the proxy's `X-Forwarded-*` headers: the proxy's container IP, its
  Docker network CIDR, or `*` for the simplest setups. With that, the app
  sees the original `https` scheme and host name — session cookies are
  automatically sent with the `Secure` flag over HTTPS while plain LAN http
  access keeps working without it. Two proxy caveats worth knowing:
  * OIDC: set the **Callback URL** on the Settings page to your public
    `https://<host>/oidc/callback`, so the OAuth redirect never depends on
    header forwarding.
  * Path prefixes (serving the app under `https://host/retentacle/`) are not
    supported — use a dedicated hostname/subdomain or port.

  Minimal Traefik (label discovery) compose example:

  ```yaml
  services:
    retentacle:
      image: ghcr.io/exxamalte/retentacle:main
      volumes:
        - retentacle-data:/data
      environment:
        FORWARDED_ALLOW_IPS: "*"
      labels:
        - traefik.enable=true
        - traefik.http.routers.retentacle.rule=Host(`backup.example.com`)
        - traefik.http.routers.retentacle.entrypoints=websecure
        - traefik.http.routers.retentacle.tls=true
  ```
* **Device discovery** — mDNS discovery needs direct access to the network 
  where the Shelly devices are located. Run the container with `network_mode: host`
  (see the commented lines in the compose file) or configure a `macvlan` network 
  interface that is connected to the network where the Shelly devices are located.

* **One-shot backups** — the same image works for cron-style single runs:

  ```commandline
  docker run --rm -v retentacle-data:/data retentacle --once --configuration /data/configuration.yaml
  ```

### When the data folder cannot be written

`PermissionError: '/data/retentacle_settings.json'` means the server may not
write its databases. Current builds refuse to start in that state instead of
running without persistence, and both the entrypoint and the server say what
they found:

```text
entrypoint: data folder /data 0:0 -> 10001:10001, running as 10001:10001
ERROR:retentacle.cli:Unable to run Retentacle: Data folder /data cannot be used ([Errno 13] Permission denied: '/data/.write-test'). Found: /data is owned by 0:0 mode 0755; the server runs as 10001:10001; /data is a ext4 mount (rw,relatime). Remedy: start the container as root (remove any user: setting) so the entrypoint can take ownership of the folder, or chown the host folder to PUID/PGID (10001:10001).
```

The `Found:` line is the part to look at: it reports who owns the folder, who
the server is running as, the filesystem and mount options, and whether SELinux
is enforcing — which of the causes below applies falls out of it.

Work through the causes in this order:

1. **Check that the container is allowed to start as root.** The ownership
   repair only runs when the entrypoint is root. A `user:` entry in the stack,
   or the **User** field in Portainer's container form, overrides the image and
   the `PUID`/`PGID` variables — the entrypoint then logs
   `running as 10001:10001 without root, leaving /data (owned by 0:0) as is`
   and the server refuses to start. Remove that setting (the server drops
   privileges itself), or make the host folder match the forced user once with
   `chown -R <uid>:<gid> /host/path`. Check with
   `docker inspect <name> --format 'user={{.Config.User}}'`: it must be empty,
   because the image ships no `USER` instruction.

2. **Check that the running image is current.** A tag is only a name, and a
   container manager may keep using a cached build:
   `docker image inspect <image> --format 'user={{.Config.User}} entrypoint={{.Config.Entrypoint}} created={{.Created}}'`.
   An image with `user=retentacle` predates the PUID/PGID entrypoint and can
   never repair a root-owned bind mount; `user=` (empty) plus
   `entrypoint=['/entrypoint.sh']` is what a current image reports. Re-pull
   (Portainer: *Re-pull image*) or rebuild before recreating the container.
3. **Pick the right owner for the files.** With the entrypoint running as root,
   it takes ownership of the folder at every start; set `PUID`/`PGID` to the
   owner of the host folder (e.g. `PUID=1000 PGID=1000`) when the files should
   stay directly readable and editable there.
4. **SELinux (Fedora, RHEL, openSUSE, …).** Run `getenforce` on the host. With
   `Enforcing`, ownership is correct but the security label still denies
   writes — add `:z` to the bind mount (`/host/path:/data:z`), relabel it with
   `chcon -Rt container_file_t /host/path`, or use a named volume.
5. **Read-only or remote mounts.** NFS exports with `root_squash` reject the
   startup `chown` (the entrypoint logs a warning) and read-only mounts reject
   writes entirely. Match `PUID`/`PGID` to the folder owner, fix the export
   mapping, or use a named volume.
6. **No host terminal needed at all:** a named volume. Create one in Portainer
   under *Volumes*, mount it at `/data`, and nothing has to be chowned — the
   image pre-creates `/data` with the right ownership and an empty volume
   inherits it.

## One-shot backup

To run a single backup and exit, use `--once` together with a configuration
file that lists `ip_addresses` (or with `--discover`):

```commandline
retentacle --once --configuration configuration.yaml
```

```commandline
retentacle --once --discover
```

Backups are stored in the current folder as `retentacle_backups.json` unless a
different folder is specified:

```commandline
retentacle --once --configuration configuration.yaml --target-folder backup_folder
```

## Auto-discovery

You can automatically discover Shelly devices on the network via mDNS:

```commandline
retentacle --discover
```

The **Settings** page also has a **Discover devices** button. It scans the
network on demand and lets you select which discovered devices to add. Newly
found devices are preselected, and a **Select none** button clears the selection
in one click. Results appear as soon as they are found, without reloading the
page, are sorted by IP address, and show the device name, model/type,
generation, and firmware version advertised via mDNS when available. Only
devices that are not already in the Settings device list are offered, so running
discovery again shows just the new ones.

The `--discover` option and `discover: true` configuration value remain
available as a fallback when no devices are configured in Settings.

## Configuration file

Create a YAML configuration file to specify multiple devices, the backup
interval, and server settings. See
[`configuration.example.yaml`](configuration.example.yaml) for a complete
example.

```yaml
ip_addresses:
  - 192.168.10.20
  - 192.168.10.21
  - 192.168.10.23
  - 192.168.10.24

target_folder: backup_folder
interval: 1h
host: 127.0.0.1
port: 8080
```

To use mDNS discovery instead of listing IP addresses:

```yaml
discover: true
target_folder: backup_folder
```

```commandline
retentacle --configuration configuration.yaml
```

Configuration file values take precedence over command line options. Settings
saved in the web UI take precedence over both.

## Database

Device backups are stored in a TinyDB database named `retentacle_backups.json` inside
the target folder. Each record contains the device IP address, a timestamp, the
device generation, basic device information, and the fetched configuration.

Application settings — the backup schedule, devices, retention policy, and
single sign-on configuration managed on the Settings page — live in a separate
database named `retentacle_settings.json`, so they can be reset or repaired
without touching any backups.

User accounts and login sessions are stored in a separate TinyDB database
named `retentacle_users.json` in the same folder. Accounts are keyed by
email address, passwords are stored as salted PBKDF2 hashes and session
tokens only appear as salted hashes. The OpenID Connect client secret lives
in `retentacle_settings.json`, so protect the target folder accordingly.
