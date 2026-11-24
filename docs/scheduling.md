# Backups and scheduling

> Part of the [documentation](../README.md#documentation)

By default Retentacle runs a backup every hour. The **Settings** page turns
automatic backups on or off, chooses the date and time of the next run, and
selects how often backups repeat. Changes take effect immediately and are stored
in the settings database, so they survive a restart.

## What the schedule guarantees

* The first backup starts right after startup, later runs follow the interval.
* A slot missed while the server was not running is caught up: if the computer
  was off or asleep at a scheduled time, the missed backup runs as soon as the
  server starts again.
* While the server runs, due slots are re-checked against the wall clock every
  minute, so a schedule missed during laptop sleep or a clock change is noticed
  promptly without a restart.
* Your chosen slot is sticky: scheduled backups always happen on the grid of the
  date and time selected on the Settings page — e.g. daily at 3am — no matter
  when a run actually started or finished, and restarts, catch-up runs, and
  manual backups never move later runs off that schedule.
* Changing only the repeat interval keeps that clock time as well: going from
  hourly to daily stays on the grid's own hour instead of freezing the daily
  backup at whatever minute the Settings page was opened, and the confirmation
  message names the resulting date and time.
* When the pending slot itself is unknown — a first installation, or a settings
  database that could not be written — it is recovered from the last automatic
  backup instead of the current clock, so a restart never silently moves the
  schedule.
* While no device is configured, scheduled runs are skipped with a line in the
  log instead of recording an empty automatic backup, and **Run backup now**
  points out that there is nothing to back up rather than reporting a finished
  run.

## Manual versus automatic runs

Manual runs are first-class: they are stored like any other backup and marked as
manual, but they never touch the schedule.

* The **Last automatic backup** card on the dashboard follows scheduled runs
  only — manually started backups, including single-device ones, do not move it,
  and neither does the recorded duration of the last automatic run.
* Each device can be included in or excluded from automatic backups from its own
  detail page, without editing the shared device list.
* **Run backup now** (admins only) backs up the devices configured on the
  Settings page; if none are configured there, it backs up all devices currently
  listed in the overview, falling back to the configured or discovered devices
  when the overview is empty.

## Recency indicators

Each device in the overview shows whether it is included in automatic backups and
whether its latest backup is recent. A backup is considered recent when it is
within the configured automatic backup interval (plus a short grace period), or
within the last 24 hours when automatic backups are disabled.

The overview subtitle also counts the configured devices that do not have a
backup yet — for example `2 devices with backups · 3 devices without a backup` —
so a device that cannot be reached is noticeable without a separate warning.
Read-only users see the same count. Devices covered only by mDNS discovery are
left out of that number, because without a fixed device list there is nothing
reliable to compare against.

## Device order

Devices added on the Settings page are kept in IP order — compared as numbers,
not as text, so `10.10.4.9` comes before `10.10.4.10`, with IPv4 addresses ahead
of IPv6 ones. The list is sorted when it is read as well, so a device file
written by an older version lines up again without re-adding anything, and
adding, removing or toggling devices cannot push the list out of order. The
overview and the scheduled backup order follow the same sequence.

## How many backups are kept

Retention is configured per period on the Settings page and pruned after each
successful backup — see [Data and retention](data.md#retention).
