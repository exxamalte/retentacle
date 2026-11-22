# Changes

## 2026.11.0
* Present backups in restore-oriented zones: restorable configuration, masked settings summary, and clearly separated live state.
* Offer a ready-to-run manual restore command and a dedicated `restore.json` download per backup.
* Summarize each backup on the device history page with configuration chips, mark unchanged configs, and add a "Restore latest" shortcut.
* Add a configuration diff view comparing any two backups of a device, with changed, added and removed settings.
* Add a context-aware button on the device detail page for admins to add or remove that device from automatic backups.
* Rework the device page header: status badges join the title line, "Back up this device" and "Restore latest" sit together, and the device web UI opens via an icon link next to the device name.
* Show the history Diff button only where a newer backup exists to compare against.
* Rename the application to Retentacle.
* Show the tagline "Tentacles for your Shelly backups." under the name in the header.
* Show the total size of all backups on disk and the duration of the last automatic backup as dashboard cards on the overview.
* Show flash messages as auto-dismissing toast overlays instead of banners that shift the page content.
* Fix host bind-mount permissions out of the box: the container now takes ownership of the data folder at startup and drops to the unprivileged PUID/PGID user.
* Refuse to start when the data folder or a stored database cannot be written, logging who owns the folder, which user the server runs as, the mount options and the SELinux mode, plus the matching remedy — and exit non-zero so supervisors report the failure instead of a container that silently keeps no state.
* Log what the container entrypoint did with the data folder and which user the server runs as.
* Keep the configured backup time across restarts: when the pending slot is unknown, the schedule is recovered from the last automatic backup instead of re-anchoring to the restart time, and slots missed while the server was down are caught up in a single run.
* Move the "Run backup now" button from the top bar to the dashboard header and add an "Add devices" shortcut to the settings device section.
* Arrange the overview stat cards in a fixed three-column grid on wide screens.
* Add a trademark disclaimer to the bottom of every page and remove the database path from the overview.
* Remove the `--ip-address` and `--interval` command line options; configure devices and the backup schedule in the web UI or the configuration file.
* Store web settings in a separate `retentacle_settings.json` database, so they can be reset without touching backups.
* Name the backups database file `retentacle_backups.json`.
* Run missed scheduled backups immediately after startup instead of showing a next run in the past.
* Keep automatic backups on the fixed wall-clock schedule chosen in the settings, even after restarts, late catch-up runs, or manual backups.
* Detect due backup slots within a minute even after system sleep, instead of waiting for a stalled timer.
* Track the trigger of each backup and base the overview's last automatic backup card on scheduled runs only.
* Add a GitHub link next to the version number in the account menu.
* Show an admin-only badge in the header when a newer stable release is available on GitHub; pre-releases are never announced.
* Add a release workflow that creates a GitHub Release from pushed version tags, marking suffixed versions (dev, alpha, beta, rc, b) as pre-releases.
* Replace the header email badge and sign-out button with an avatar account menu showing the role, settings link, and app version, using the OIDC profile picture from userinfo or the ID token when available.
* Remove the Settings button from the header; it lives in the account menu now.
* Serve the app logo from a static folder inside the package and show it in the header and as favicon.
* Add a Docker deployment option with a Dockerfile, docker-compose example and data volume.
* Configure the container's host, port and data folder via RETENTACLE_HOST, RETENTACLE_PORT and RETENTACLE_DATA_DIR environment variables.
* Derive the image's pre-created data folder from RETENTACLE_DATA_DIR instead of hard-coding it in the Dockerfile.
* Move all Docker-related files (Dockerfile, entrypoint, compose, ignore rules) into a docker/ folder.
* Support reverse proxies like Traefik via FORWARDED_ALLOW_IPS and mark session cookies Secure when served over HTTPS.
* Publish multi-architecture container images to the GitHub Container Registry from CI.
* Reserve the `latest` container tag for proper releases and tag development builds with the current application version.
* Stop building standalone executables; install the Python package instead.
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
* Store user accounts and login sessions in a separate `retentacle_users.json` database with hashed passwords.
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
