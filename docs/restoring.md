# Restoring from a backup

> Part of the [documentation](../README.md#documentation)

Retentacle does not push settings to devices — it prepares everything needed to
apply them yourself, quickly and with the right payload. Every backup page is
therefore triaged into three zones:

1. **Manual restore** — the restorable payload (`settings.configuration` for
   Gen 2/3, `settings.settings` for Gen 1) with one-click copy, a dedicated
   `restore.json` download, a four-step checklist, and a ready-made `curl`
   command pre-filled with the device's IP, the correct endpoint
   (`rpc/Shelly.SetConfig` or `/settings`) and the download's filename. Adjust
   the IP and run it next to the downloaded file.
2. **Configuration** — the same payload as a human-readable table, grouped by
   device section (System, Wi-Fi, MQTT, Switch 0, …), with credential values
   masked and copy buttons on every value.
3. **Device state** — the live `status` (and Gen 1 `actions`) recorded at backup
   time, collapsed and marked *information only — never restored*.

Model, MAC and firmware are shown for reference only; they never appear in a
restore payload.

## Typical recovery flow

After losing a device configuration:

1. Open the device history and find the last backup before the unwanted change.
   The list shows each backup's Wi-Fi SSID, MQTT state, component counts and
   firmware as chips, and collapses rows whose configuration matches the newer
   one to "unchanged".
2. Use **Restore latest**, or open the backup you picked. To decide between two
   candidates, the **Diff** link on each history row compares that backup's
   configuration with the next newer one — changed, added and removed settings
   are listed separately (credentials stay masked, live state is never
   compared).
3. Download the `restore.json` payload, adjust the IP address in the shown
   command, and run it next to the downloaded file.
4. Take a fresh backup in Retentacle to verify the device now reports what you
   restored.

## Restoring to a replacement device

Because the payload is just the configuration, it can also be applied to a spare
unit of the same model: download `restore.json`, point the `curl` command at the
new device's address, and run it. Wi-Fi credentials, MQTT settings, schedules
and component names come across; the device's own identity (MAC, serial) stays
untouched.

## What restoring does not do

* It never writes to a device by itself — every restore is an explicit action
  you run.
* Live state (`status`, Gen 1 `actions`) is recorded for diagnosis only and is
  excluded from both `restore.json` and the diff view.
* Authenticated device access is not supported, so devices with device-side
  auth enabled cannot be fetched or restored over HTTP.
