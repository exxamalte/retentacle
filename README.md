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

```commandline
pip install .
```

For development, install the test dependencies as well:

```commandline
pip install -e ".[tests]"
```

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
subsequent backups run at the configured interval.

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

### Web UI

* `/` - devices overview with scheduler status, backup counts, last backup times, and per-device indicators for automatic backups and recent backups.
* `/devices/<ip-address>` - all backups for one device, newest first, plus a link to the device web UI.
* `/backups/<id>` - metadata and raw JSON for a single backup.
* `/backups/<id>/download` - download a backup as JSON.
* `/settings` - configure the backup schedule, device IP addresses, and single sign-on, and manage user accounts, roles, and passwords (admin only).
* `/setup` - first-run wizard to create the initial admin account.
* `/login`, `/logout` - sign in and out.
* `/oidc/login`, `/oidc/callback` - OpenID Connect single sign-on flow.
* `/api/status` - scheduler status and live backup progress as JSON.
* `/health` - server and scheduler status as JSON (the only page that does not require a login).
* `/users` - legacy alias that redirects to `/settings`.

A **Run backup now** button is available on every page. It backs up the devices
configured on the Settings page. If no devices are configured there, it backs up
all devices currently listed in the overview, falling back to the configured or
discovered devices when the overview is empty. Each device row in the overview
offers its actions — view backups, open the device web UI, and back up this
device — as compact icon buttons with tooltips and screen-reader labels, so the
table stays readable without horizontal scrolling. The device detail page keeps
these as labelled buttons, including **Back up this device**, to back up just
that device without waiting for the next scheduled run.

Each device in the overview shows whether it is included in automatic backups
and whether its latest backup is recent. A backup is considered recent when it
is within the configured automatic backup interval (plus a short grace period),
or within the last 24 hours when automatic backups are disabled.

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
to the GitHub Container Registry whenever `main` is updated or a version tag
is pushed:

```commandline
docker pull ghcr.io/exxamalte/retentacle:latest
docker run -d --name retentacle -p 8080:8080 -v retentacle-data:/data \
  ghcr.io/exxamalte/retentacle:latest
```

Tags work as follows: proper releases (a `vX.Y.Z` git tag) publish the plain
version (e.g. `2026.11.0`) and update `latest`. Development builds on the
default branch publish the current application version from `pyproject.toml`
(e.g. `2026.11.0-dev`), plus `main` and `sha-<commit>` identifiers — they
never touch `latest`. Prereleases (version tags with a `b` suffix, e.g.
`v2026.11.0b1`) publish only their tag name and `sha-<commit>`.

The repository also includes a `Dockerfile` and a `docker-compose.yml` for
building and running Retentacle from source.

Build and start the container with Docker Compose:

```commandline
docker compose up -d --build
```

Or with plain Docker:

```commandline
docker build -t retentacle .
docker run -d --name retentacle -p 8080:8080 -v retentacle-data:/data retentacle
```

Then open `http://localhost:8080` and complete the first-run setup wizard to
create the admin account. The container runs as a non-root user, listens on
all interfaces, and reports health via the public `/health` endpoint.

* **Data** — everything Retentacle stores (the `retentacle_backups.json`,
  `retentacle_settings.json` and `retentacle_users.json` databases) lives in
  `/data` inside the container. Keep it on a named volume, as in the compose
  example, or bind-mount a folder; with a bind mount make sure the folder is
  writable by UID 10001, the user the container runs as.
* **Options** — command line flags work as usual by replacing the container
  command, for example `docker run ... retentacle --port 9000`. Update the
  port mapping accordingly; the built-in health check assumes the default
  port 8080.
* **Device discovery** — mDNS discovery needs direct access to the local
  network. Run the container with `network_mode: host` (see the commented
  lines in the compose file) and add `--discover`, or configure device IP
  addresses in the web UI instead, which works with plain port mappings.
* **Configuration file** — to use a `configuration.yaml`, place it in the
  data folder and override the command:

  ```commandline
  docker run ... -v retentacle-data:/data retentacle --configuration /data/configuration.yaml
  ```

* **One-shot backups** — the same image works for cron-style single runs:

  ```commandline
  docker run --rm -v retentacle-data:/data retentacle --once --configuration /data/configuration.yaml
  ```

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
