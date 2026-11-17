# Shelly Backup

[![Build Status](https://github.com/exxamalte/shelly-backup/actions/workflows/tests.yml/badge.svg)](https://github.com/exxamalte/shelly-backup/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/exxamalte/shelly-backup/graph/badge.svg?token=47OELOAUP4)](https://codecov.io/gh/exxamalte/shelly-backup)

Long-running backup server and command line tool for Shelly devices. It fetches
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

## Running the server

Start the server with one or more device IP addresses:

```commandline
shelly-backup --ip-address 192.168.10.20
```

Alternatively, run it as a Python module:

```commandline
python -m shelly_backup --ip-address 192.168.10.20
```

By default the web server listens on `http://127.0.0.1:8080` and runs a backup
every hour. Open that address in a browser for the first time to create the
initial admin account, then sign in to see the devices overview. The
first backup starts immediately after startup; subsequent backups run at the
configured interval.

You can also start the server without specifying device IP addresses and add
them later on the Settings page.

### Server options

| Option | Description |
| --- | --- |
| `--ip-address` | IP address of a Shelly device. Repeat by using a configuration file for multiple devices. |
| `--discover` | Auto-discover Shelly devices via mDNS. |
| `--target-folder` | Folder where the TinyDB database is stored. Defaults to the current folder. |
| `--configuration` | YAML configuration file. |
| `--interval` | Interval between automatic backups, for example `30m`, `1h`, or `1d`. Plain seconds are also accepted. Use `0` to disable automatic backups. |
| `--host` | Host the web server binds to. Defaults to `127.0.0.1`. |
| `--port` | Port the web server listens on. Defaults to `8080`. |
| `--once` | Run a single backup and exit instead of starting the server. |

To expose the web UI on your local network, bind to all interfaces:

```commandline
shelly-backup --host 0.0.0.0 --port 8080
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

### OpenID Connect single sign-on

The Settings page has a **Single sign-on (OpenID Connect)** section to let
users sign in through an external identity provider:

* Enable it and enter the **issuer URL**, **client ID**, and **client secret**
  of a confidential OIDC client registered for this app. All endpoints are
  discovered automatically from the issuer's well-known OpenID configuration,
  and the authorization code flow is used. Register the redirect URI
  `https://<your-host>/oidc/callback` (or the URL derived from the optional
  **Public URL** field, which you should set when running behind a reverse
  proxy).
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
(`shelly_backup.json`) as part of the web settings, so keep the target folder
protected. If you disable password sign-in and your provider becomes
unreachable, you can re-enable it by editing the settings file: remove the
`oidc` entry (or set `disable_password_login` to `false`) in the `settings`
document and restart the server.

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
configuration file and `--ip-address`. Changes take effect immediately and are
stored in the TinyDB database, so they survive a restart. Settings saved in the
web UI take precedence over both the configuration file and command line
options.

The Settings page also configures backup retention separately for each device.
It keeps the newest backup from each month, week, day, and hour, and removes
older backups after the next successful backup. The defaults keep 12 monthly,
4 weekly, 7 daily, and 24 hourly backups. A single backup can count towards
several periods. Set a period to `0` to keep none for it; at least one period
must remain above zero.

## One-shot backup

To run a single backup and exit, use `--once`:

```commandline
shelly-backup --once --ip-address 192.168.10.20
```

The database is stored in the current folder as `shelly_backup.json` unless a
different folder is specified:

```commandline
shelly-backup --once --ip-address 192.168.10.20 --target-folder backup_folder
```

## Auto-discovery

You can automatically discover Shelly devices on the network via mDNS:

```commandline
shelly-backup --discover
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
shelly-backup --configuration configuration.yaml
```

Configuration file values take precedence over command line options. Settings
saved in the web UI take precedence over both.

## Database

Backups are stored in a TinyDB database named `shelly_backup.json` inside the
target folder. Each record contains the device IP address, a timestamp, the
device generation, basic device information, and the fetched configuration.

User accounts and login sessions are stored in a separate TinyDB database
named `shelly_backup_users.json` in the same folder. Accounts are keyed by
email address, passwords are stored as salted PBKDF2 hashes and session
tokens only appear as salted hashes. The OpenID Connect client secret lives
in the settings document of `shelly_backup.json`, so protect the target
folder accordingly.
