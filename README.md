# Shelly Backup

[![Build Status](https://github.com/exxamalte/shelly-backup/actions/workflows/tests.yml/badge.svg)](https://github.com/exxamalte/shelly-backup/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/exxamalte/shelly-backup/graph/badge.svg?token=47OELOAUP4)](https://codecov.io/gh/exxamalte/shelly-backup)

Long-running backup server and command line tool for Shelly devices. It fetches
the configuration of Shelly devices, stores snapshots in a local TinyDB
database, and provides a web UI for browsing backups per device. This might be
useful if a device loses its configuration for whatever reason. Restoring
settings to a device is not supported.

* Support for generation 1, 2 and 3 devices.
* Scheduled backups at a configurable interval.
* Web UI for browsing devices, backup history, and individual backup snapshots.
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
every hour. Open that address in a browser to see the devices overview. The
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

The server has no authentication. Only expose it on networks you trust.

### Web UI

* `/` - devices overview with scheduler status, backup counts, and last backup times.
* `/devices/<ip-address>` - all backups for one device, newest first.
* `/backups/<id>` - metadata and raw JSON for a single backup.
* `/backups/<id>/download` - download a backup as JSON.
* `/settings` - configure the backup interval and device IP addresses.
* `/api/status` - scheduler status and live backup progress as JSON.
* `/health` - server and scheduler status as JSON.

A **Run backup now** button is available on every page. It backs up the devices
configured on the Settings page. If no devices are configured there, it backs up
all devices currently listed in the overview, falling back to the configured or
discovered devices when the overview is empty. The device detail page also has a
**Back up this device** button to back up just that device without waiting for
the next scheduled run.

While a backup is running, the page shows a live progress bar and updates the
status automatically. Once the run finishes, the page reloads so newly created
backups appear without a manual refresh.

The **Settings** page lets you change the backup interval and add or remove
device IP addresses while the server is running. Devices added there are used
for scheduled and manual backups and take precedence over the configuration
file and `--ip-address`. Changes take effect immediately and are stored in the
TinyDB database, so they survive a restart. Settings saved in the web UI take
precedence over both the configuration file and command line options.

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
network on demand and lets you select which discovered devices to add. Only
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
