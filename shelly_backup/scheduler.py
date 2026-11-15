"""Periodic backup scheduler."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import logging
import threading

from .backup import Backup

_LOGGER = logging.getLogger(__name__)


class BackupScheduler:
    """Run backups periodically in the background."""

    def __init__(self, backup: Backup, interval: float):
        """Initialise the scheduler."""
        self._backup = backup
        self._interval = float(interval)
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._progress_lock = threading.Lock()
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
        if not self.enabled or self.last_run is None:
            return None
        return self.last_run + timedelta(seconds=self._interval)

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

    async def _run_loop(self) -> None:
        """Run backups until the scheduler is stopped."""
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)
