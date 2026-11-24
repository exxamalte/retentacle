# Configuration

> Part of the [documentation](../README.md#documentation)

Devices and the backup interval are managed in the web UI while the server runs.
Everything else is configured with command line flags, an optional YAML file,
and environment variables.

## Command line options

| Option | Description |
| --- | --- |
| `--discover` | Auto-discover Shelly devices via mDNS. |
| `--target-folder` | Folder where the TinyDB databases are stored. Defaults to the current folder. |
| `--configuration` | YAML configuration file. |
| `--host` | Host the web server binds to. Defaults to `127.0.0.1`. |
| `--port` | Port the web server listens on. Defaults to `8080`. |
| `--once` | Run a single backup and exit instead of starting the server. |

## Configuration file

Create a YAML configuration file to specify multiple devices, the backup
interval, and server settings. See
[`configuration.example.yaml`](../configuration.example.yaml) for a complete
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

### Precedence

Configuration file values take precedence over command line options. Settings
saved in the web UI take precedence over both — they live in
[`retentacle_settings.json`](data.md), so they survive a restart and are what
the [schedule](scheduling.md) and the device list actually use.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `RETENTACLE_HOST` | `0.0.0.0` (container) | Bind address the container entrypoint starts the server on |
| `RETENTACLE_PORT` | `8080` | HTTP port inside the container; the health check follows it, so adjust the port mapping too (e.g. `-e RETENTACLE_PORT=9000 -p 9000:9000`) |
| `RETENTACLE_DATA_DIR` | `/data` | Where the container stores its databases |
| `PUID` / `PGID` | `10001` | User and group the server drops to; see [Docker deployment](docker.md#the-data-folder) |
| `RETENTACLE_DISCOVERY_TIMEOUT` | `30` | Seconds the on-demand device scan waits for mDNS answers |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted; see [Docker deployment](docker.md#reverse-proxy) |
| `RETENTACLE_UPDATE_CHECK` | on | Set to `0` to disable the update check entirely |
| `RETENTACLE_UPDATE_REPO` | `exxamalte/retentacle` | `owner/repo` to check for new releases |
| `RETENTACLE_UPDATE_TOKEN` | — | Token with read access to the repository, needed for private repos |

Inside the container these are the supported way to configure startup; extra
command line flags still work and are appended after the generated ones, e.g.
`docker run ... retentacle --discover`.

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

This mode is meant for cron jobs and scripts, so a missing device configuration
is reported as a warning rather than being silently ignored.

## Auto-discovery

Shelly devices announce themselves over mDNS, so they can be found without
knowing their addresses:

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

The scan takes up to 30 seconds — mDNS answers arrive with a random delay, and
bridges or sleeping devices often only reply to a later repetition — and the
button counts the remaining seconds down while it runs, so the wait is
predictable instead of an open-ended "Searching…". Set
`RETENTACLE_DISCOVERY_TIMEOUT` (in seconds) for networks that need a longer
window; the page text and the countdown follow that setting.

Discovery needs direct access to the network segment where the devices live: it
does not cross routers unless an mDNS reflector (e.g. `avahi-daemon` with
`enable-reflector`, or an `macvlan`/host-network container) is in place. In
containers, see [Docker deployment](docker.md#device-discovery).

The `--discover` option and the `discover: true` configuration value remain
available as a fallback when no devices are configured in Settings.
