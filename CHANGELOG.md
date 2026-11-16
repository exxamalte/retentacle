# Changes

## 2026.5.0
* Run as a long-running server with scheduled backups.
* Add a web UI for browsing backups per device and viewing raw backup data.
* Add a manual per-device backup action on the device detail page.
* Make the global "Run backup now" action back up all registered devices.
* Add a live backup progress bar and automatic page refresh when a run finishes.
* Show configured device names for generation 1 devices.
* Add a settings page for changing the backup interval at runtime.
* Make automatic backups toggleable and schedule the next run by date and time.
* Persist the next scheduled backup across server restarts.
* Keep manual backups from shifting the scheduled next run.
* Add device IP address management to the settings page.
* Add on-demand mDNS device discovery to the settings page.
* Show discovered device names and mDNS metadata, sorted by IP address.
* Show discovery results incrementally without reloading the settings page.
* Show the actual last backup time on the overview instead of the server start time.
* Add `--interval`, `--host`, and `--port` options.
* Add `--once` for the original single-run backup behaviour.
* Add `python -m shelly_backup` support.
* Store backups in a TinyDB database.
* Add mDNS auto-discovery support for Shelly devices.

## 2025.4.0 (xx/04/2025)
* Initial release.
