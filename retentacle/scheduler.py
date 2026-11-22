"""Periodic backup scheduler."""

import asyncio
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime, timedelta
import logging
import threading

from .backup import Backup

_LOGGER = logging.getLogger(__name__)

# Upper bound for a single timer sleep while waiting for the next run. The
# asyncio clock pauses while the machine sleeps, so due slots are re-checked
# against the wall clock periodically instead of trusting one long sleep.
_WALL_CLOCK_POLL_SECONDS = 60.0


class BackupScheduler:
    """Run backups periodically in the background."""

    def __init__(
        self,
        backup: Backup,
        interval: float,
        *,
        enabled: bool | None = None,
        next_run_at: datetime | None = None,
        last_automatic_run_at: datetime | None = None,
        ip_addresses_provider: Callable[[], list[str] | None] | None = None,
        on_schedule_changed: Callable[[datetime | None], None] | None = None,
        on_run_finished: Callable[[datetime, timedelta], None] | None = None,
    ):
        """Initialise the scheduler."""
        self._backup = backup
        self._interval = float(interval)
        self._enabled = self._interval > 0 if enabled is None else bool(enabled)
        self._ip_addresses_provider = ip_addresses_provider
        self._on_schedule_changed = on_schedule_changed
        self._on_run_finished = on_run_finished
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._progress_lock = threading.Lock()
        self._reschedule = asyncio.Event()
        self._next_run_at = next_run_at if self.enabled else None
        # Grid origin that keeps runs at the chosen wall-clock time even when
        # one runs late or after downtime; None until the first run fixes it.
        # The pending slot describes it when known; otherwise the last
        # completed automatic run does, so a restart with an unreadable or
        # missing pending slot does not move the schedule to the start time.
        self._anchor_at: datetime | None = self._next_run_at or last_automatic_run_at
        self._pending_runs = 0
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.last_backed_up: list[str] = []
        self.run_count = 0
        self.started_at: datetime | None = None
        self.progress_total = 0
        self.progress_completed = 0
        self.progress_current: str | None = None
        self.progress_results: list[dict[str, object]] = []

    @property
    def interval(self) -> float:
        """Get the interval between backups in seconds."""
        return self._interval

    @property
    def enabled(self) -> bool:
        """Whether automatic backups are enabled."""
        return self._enabled and self._interval > 0

    @property
    def started(self) -> bool:
        """Whether the background scheduler has been started."""
        return self._task is not None

    @property
    def running(self) -> bool:
        """Whether a backup is currently running."""
        return self._lock.locked()

    @property
    def queued(self) -> bool:
        """Whether a backup has been requested but has not started yet."""
        return self._pending_runs > 0

    @property
    def active(self) -> bool:
        """Whether a backup is running or waiting to start."""
        return self.running or self.queued

    @property
    def progress(self) -> dict[str, object]:
        """Get the progress of the current or last backup run."""
        with self._progress_lock:
            total = self.progress_total
            completed = self.progress_completed
            percent = min(100, round((completed / total) * 100)) if total else 0
            return {
                "total": total,
                "completed": completed,
                "current": self.progress_current,
                "percent": percent,
                "results": list(self.progress_results),
            }

    @property
    def next_run(self) -> datetime | None:
        """Get the time of the next scheduled backup."""
        if not self.enabled:
            return None
        return self._next_run_at

    async def set_interval(self, interval: float) -> None:
        """Update the interval and schedule the next backup from now."""
        interval = float(interval)
        enabled = interval > 0
        await self.set_schedule(
            interval=interval,
            enabled=enabled,
            next_run_at=(
                datetime.now(UTC) + timedelta(seconds=interval) if enabled else None
            ),
        )

    async def set_schedule(
        self,
        *,
        interval: float,
        enabled: bool,
        next_run_at: datetime | None,
    ) -> None:
        """Update the automatic backup schedule."""
        self._interval = float(interval)
        self._enabled = bool(enabled)
        self._next_run_at = next_run_at if self.enabled else None
        if self.enabled:
            # A user-selected date and time defines the new grid origin.
            self._anchor_at = next_run_at
        if not self.enabled:
            # The run loop, if active, waits for automatic backups to be
            # enabled again. Do not cancel a backup that may currently be
            # running.
            self._reschedule.set()
            return
        if self._task is None:
            await self.start()
        else:
            self._reschedule.set()

    async def start(self) -> None:
        """Start the background scheduler."""
        if not self.enabled or self._task is not None:
            return
        now = datetime.now(UTC)
        if self._next_run_at is None:
            # The pending slot is unknown: a first run, or the schedule could
            # not be saved (e.g. an unwritable data folder). Recover it from
            # the schedule grid instead of letting this restart silently move
            # the whole schedule to the current time.
            self._next_run_at = self._startup_slot(now)
        elif self._next_run_at <= now:
            # The scheduled time passed while the server was not running
            # (crash, shutdown, sleep). Never show a next run in the past:
            # run the missed backup now and reschedule from its completion.
            _LOGGER.info(
                "Backup was due at %s; catching up immediately",
                self._next_run_at.isoformat(),
            )
            self._next_run_at = now
        _LOGGER.info(
            "Automatic backups active every %.0f s, next run at %s",
            self._interval,
            self._next_run_at.isoformat(timespec="seconds"),
        )
        self._task = asyncio.create_task(self._run_loop(), name="retentacle-scheduler")

    def _startup_slot(self, now: datetime) -> datetime:
        """Return the first run time to use when no pending slot is known."""
        if self._anchor_at is None:
            # Nothing is known about the schedule yet: run now and let the
            # completion time become the grid origin.
            return now
        interval = self._interval if self._interval > 0 else 3600
        due = self._anchor_at + timedelta(seconds=interval)
        if due <= now:
            _LOGGER.info(
                "Backup was due at %s; catching up immediately",
                due.isoformat(),
            )
            return now
        return due

    async def stop(self) -> None:
        """Stop the background scheduler."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def queue(self) -> None:
        """Mark a backup as requested so clients can show it immediately."""
        self._pending_runs += 1

    def _on_start(self, total: int) -> None:
        """Reset progress when a backup run starts."""
        with self._progress_lock:
            self.progress_total = total
            self.progress_completed = 0
            self.progress_current = None
            self.progress_results = []

    def _on_progress(self, ip_address: str, success: bool) -> None:
        """Record progress after a single device has been backed up."""
        with self._progress_lock:
            self.progress_completed += 1
            self.progress_current = ip_address
            self.progress_results.append({"ip_address": ip_address, "success": success})

    async def run_once(
        self, ip_addresses: list[str] | None = None, *, automatic: bool = False
    ) -> None:
        """Run a backup once for the given or configured devices.

        ``automatic`` marks the stored records as scheduled-run backups; it
        is set by the background loop only, never by manual UI triggers.
        """
        async with self._lock:
            if self._pending_runs > 0:
                self._pending_runs -= 1
            self.last_backed_up = []
            self.last_error = None
            self.started_at = datetime.now(UTC)
            self._on_start(0)
            try:
                if ip_addresses is None and self._ip_addresses_provider is not None:
                    ip_addresses = self._ip_addresses_provider()
                self.last_backed_up = await asyncio.to_thread(
                    self._backup.execute,
                    ip_addresses,
                    self._on_start,
                    self._on_progress,
                    automatic=automatic,
                )
                _LOGGER.info(
                    "Backup completed for %d device(s)", len(self.last_backed_up)
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc) or exc.__class__.__name__
                _LOGGER.exception("Backup run failed")
            finally:
                completed = datetime.now(UTC)
                self.last_run = completed
                self.run_count += 1
                if automatic and self.started_at is not None:
                    self._notify_run_finished(completed, completed - self.started_at)
                with self._progress_lock:
                    self.progress_current = None

    async def run_device(self, ip_address: str) -> bool:
        """Back up a single device without changing the scheduler status."""
        async with self._lock:
            self._on_start(1)
            try:
                success = await asyncio.to_thread(
                    self._backup.backup_device, ip_address
                )
                self._on_progress(ip_address, success)
                return success
            finally:
                with self._progress_lock:
                    self.progress_current = None

    def _update_next_run(self, next_run_at: datetime | None) -> None:
        """Update the next run time and persist it when configured to do so."""
        self._next_run_at = next_run_at
        if self._on_schedule_changed is None:
            return
        try:
            self._on_schedule_changed(next_run_at)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unable to save the next backup time")

    def _notify_run_finished(self, completed_at: datetime, duration: timedelta) -> None:
        """Report a finished automatic run when configured to do so."""
        if self._on_run_finished is None:
            return
        try:
            self._on_run_finished(completed_at, duration)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unable to save the last automatic backup duration")

    def _next_after_run(self, scheduled: datetime | None) -> datetime:
        """Return the next run on the fixed schedule grid.

        The grid origin is the user-selected date and time (or the slot of
        the first run when none was ever chosen). Late completions and
        catch-up runs move to the next grid slot instead of shifting the
        whole schedule to the completion time, so a backup configured for
        3am keeps running at 3am.
        """
        now = datetime.now(UTC)
        interval = self._interval if self._interval > 0 else 3600
        anchor = self._anchor_at or scheduled or now
        elapsed = (now - anchor).total_seconds()
        steps = max(1, int(elapsed // interval) + 1)
        candidate = anchor + timedelta(seconds=steps * interval)
        if candidate <= now:
            candidate += timedelta(seconds=interval)
        # Fix the grid origin so subsequent runs stay on the same slots.
        self._anchor_at = anchor
        return candidate

    async def _wait_for_next_run(self) -> None:
        """Wait until the next backup is due or the schedule changes."""
        while True:
            if not self.enabled:
                await self._reschedule.wait()
                self._reschedule.clear()
                return
            now = datetime.now(UTC)
            next_run_at = self._next_run_at
            if next_run_at is None:
                next_run_at = now
                self._next_run_at = now
            delay = (next_run_at - now).total_seconds()
            if delay <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._reschedule.wait(),
                    timeout=min(delay, _WALL_CLOCK_POLL_SECONDS),
                )
            except TimeoutError:
                continue
            self._reschedule.clear()

    async def _run_loop(self) -> None:
        """Run backups until the scheduler is stopped."""
        while True:
            await self._wait_for_next_run()
            if not self.enabled:
                continue
            scheduled_next_run = self._next_run_at
            await self.run_once(automatic=True)
            if not self.enabled or self._next_run_at != scheduled_next_run:
                # The schedule changed while the backup was running. Keep the
                # user's new next-run selection.
                continue
            self._update_next_run(self._next_after_run(scheduled_next_run))
