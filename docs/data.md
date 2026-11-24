# Data, retention and privacy

> Part of the [documentation](../README.md#documentation)

## Databases

Device backups are stored in a TinyDB database named `retentacle_backups.json`
inside the target folder. Each record contains the device IP address, a
timestamp, the device generation, basic device information, and the fetched
configuration.

Application settings — the backup schedule, devices, retention policy, and
single sign-on configuration managed on the Settings page — live in a separate
database named `retentacle_settings.json`, so they can be reset or repaired
without touching any backups.

User accounts and login sessions are stored in a separate TinyDB database
named `retentacle_users.json` in the same folder. Accounts are keyed by email
address, passwords are stored as salted PBKDF2 hashes and session tokens only
appear as salted hashes.

| File | Contains | Safe to delete? |
| --- | --- | --- |
| `retentacle_backups.json` | The device configuration snapshots themselves. | No — this is the data you came for. |
| `retentacle_settings.json` | Schedule, device list, retention, OIDC configuration including the client secret. | Only to reset configuration; the app falls back to defaults and you re-create the admin account through the wizard if you also remove the users. |
| `retentacle_users.json` | Accounts (email, role, password hash) and login sessions. | Only to start over: the first page load then shows the setup wizard. |

Deleting the settings file does not remove any backups, and deleting a backup
file does not lock anyone out — the three are deliberately separate.

## Retention

The Settings page configures backup retention separately for each period. Retentacle
keeps the newest backup from each month, week, day, and hour, and removes older
backups after the next successful backup. The defaults keep 12 monthly,
4 weekly, 7 daily, and 24 hourly backups. A single backup can count towards
several periods. Set a period to `0` to keep none for it; at least one period
must remain above zero.

Retention only ever prunes after a *successful* backup, so an unreachable device
cannot lose its last known-good configuration.

The dashboard's **Backup size on disk** card reports the total size of the
backup database, which is the number to watch when tuning retention.

## Credentials in the data

The stored configurations contain whatever the device holds, including Wi-Fi
passphrases, MQTT credentials and KNX or API keys. Retentacle masks them where
they only need to be read: values show as `••••••••` in the configuration
summary, and the diff view compares structure without exposing the old or new
secret. The `restore.json` download, the raw JSON export and the payload in the
manual restore section keep the real values, because that is the only way a
restore can work.

Treat the target folder accordingly:

* Restrict filesystem access to the user the server runs as (in containers,
  `PUID`/`PGID` — see [Docker deployment](docker.md#the-data-folder)).
* Do not serve the folder over a share, and do not include it in a world-readable
  backup set.
* The OIDC client secret also lives in `retentacle_settings.json`. If you need to
  wipe the SSO configuration, removing the `oidc` entry from its `settings`
  document is enough — see [Authentication](authentication.md#where-the-secret-lives-and-how-to-get-back-in).

## Moving an installation

Stop the server, copy all three `retentacle_*.json` files to the new target
folder, and start it with `--target-folder` (or `RETENTACLE_DATA_DIR` in the
container) pointing there. Accounts, settings and history come with it; the only
thing tied to a host is the OIDC redirect URI registered at the provider.
