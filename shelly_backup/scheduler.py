"""Periodic backup scheduler."""

import asyncio
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime, timedelta
import logging
import threading

from .backup import Backup

_LOGGER = logging.getLogger(__name__)


class BackupScheduler:
    """Run backups periodically in the background."""

    def __init__(
        self,
        backup: Backup,
        interval: float,
        *,
        ip_addresses_provider: Callable[[], list[str] | None] | None = None,
    ):
        """Initialise the scheduler."""
        self._backup = backup
        self._interval = float(interval)
        self._ip_addresses_provider = ip_addresses_provider
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._progress_lock = threading.Lock()
        self._reschedule = asyncio.Event()
        self._next_run_at: datetime | None = None
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
        return self._interval > 0

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
        """Update the interval and reschedule the next backup."""
        self._interval = float(interval)
        self._next_run_at = (
            datetime.now(UTC) + timedelta(seconds=self._interval)
            if self.enabled
            else None
        )
        if not self.enabled:
            # The run loop, if active, waits for the interval to be enabled
            # again. Do not cancel a backup that may currently be running.
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
        self._task = asyncio.create_task(
            self._run_loop(), name="shelly-backup-scheduler"
        )

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

    async def run_once(self, ip_addresses: list[str] | None = None) -> None:
        """Run a backup once for the given or configured devices."""
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
                )
                _LOGGER.info(
                    "Backup completed for %d device(s)", len(self.last_backed_up)
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc) or exc.__class__.__name__
                _LOGGER.exception("Backup run failed")
            finally:
                self.last_run = datetime.now(UTC)
                self._next_run_at = (
                    self.last_run + timedelta(seconds=self._interval)
                    if self.enabled
                    else None
                )
                self.run_count += 1
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

    async def _wait_for_next_run(self) -> None:
        """Wait for the current interval to elapse or to be changed."""
        while True:
            if not self.enabled:
                await self._reschedule.wait()
                self._reschedule.clear()
                return
            try:
                await asyncio.wait_for(self._reschedule.wait(), timeout=self._interval)
            except TimeoutError:
                return
            self._reschedule.clear()

    async def _run_loop(self) -> None:
        """Run backups until the scheduler is stopped."""
        while True:
            if not self.enabled:
                await self._reschedule.wait()
                self._reschedule.clear()
                continue
            await self.run_once()
            await self._wait_for_next_run()
