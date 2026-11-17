# Changes

## 2026.5.0
* Add user management with admin and read-only roles.
* Onboard the initial admin account through a first-run setup wizard.
* Identify user accounts by email address instead of usernames.
* Add OpenID Connect single sign-on with automatic provisioning of read-only accounts.
* Allow disabling local password sign-in once the OIDC connection is verified.
* Require passwords with at least 12 characters, one digit and one special character.
* Use the full page width for the settings panels, including the user list.
* Require a login for all pages except the health endpoint.
* Let read-only users view and download backups without changing settings or running backups.
* Manage user accounts, roles, passwords, and deletion from the settings page.
* Store user accounts and login sessions in a separate `shelly_backup_users.json` database with hashed passwords.
* Run as a long-running server with scheduled backups.
* Add a web UI for browsing backups per device and viewing raw backup data.
* Add a manual per-device backup action on the device detail page.
* Add per-device backup buttons to the device overview.
* Show automatic backup inclusion and backup recency indicators on the overview.
* Add a link that opens each device's HTTP web UI in a new tab.
* Replace the overview's row buttons with compact icon buttons to avoid horizontal scrolling.
* Restrict backup redirects to known application pages.
* Make the global "Run backup now" action back up all registered devices.
* Add a live backup progress bar and automatic page refresh when a run finishes.
* Show configured device names for generation 1 devices.
* Add a settings page for changing the backup interval at runtime.
* Make automatic backups toggleable and schedule the next run by date and time.
* Persist the next scheduled backup across server restarts.
* Keep manual backups from shifting the scheduled next run.
* Add configurable monthly, weekly, daily, and hourly backup retention.
* Add device IP address management to the settings page.
* Add on-demand mDNS device discovery to the settings page.
* Show discovered device names and mDNS metadata, sorted by IP address.
* Show discovery results incrementally without reloading the settings page.
* Add a select none action to the discovered device list.
* Show the actual last backup time on the overview instead of the server start time.
* Add `--interval`, `--host`, and `--port` options.
* Add `--once` for the original single-run backup behaviour.
* Add `python -m shelly_backup` support.
* Store backups in a TinyDB database.
* Add mDNS auto-discovery support for Shelly devices.

## 2025.4.0 (xx/04/2025)
* Initial release.
