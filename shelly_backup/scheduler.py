"""Periodic backup scheduler."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import logging

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
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.last_backed_up: list[str] = []
        self.run_count = 0

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

    async def run_once(self) -> None:
        """Run a backup once."""
        async with self._lock:
            self.last_backed_up = []
            self.last_error = None
            try:
                self.last_backed_up = await asyncio.to_thread(self._backup.execute)
                _LOGGER.info(
                    "Backup completed for %d device(s)", len(self.last_backed_up)
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc) or exc.__class__.__name__
                _LOGGER.exception("Backup run failed")
            finally:
                self.last_run = datetime.now(UTC)
                self.run_count += 1

    async def _run_loop(self) -> None:
        """Run backups until the scheduler is stopped."""
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)
