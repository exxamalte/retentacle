"""Tests for the backup scheduler."""

import asyncio
from datetime import timedelta
import threading
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from shelly_backup.scheduler import BackupScheduler


class SlowBackup:
    """Backup fake that records concurrent executions."""

    def __init__(self):
        """Initialise the fake."""
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def execute(self):
        """Run a slow fake backup."""
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        return []


class TestBackupScheduler:
    """Tests for BackupScheduler."""

    def test_interval_and_enabled(self):
        """Test interval and enabled properties."""
        scheduler = BackupScheduler(Mock(), 60)

        assert scheduler.interval == 60
        assert scheduler.enabled is True
        assert scheduler.next_run is None
        assert scheduler.started is False
        assert scheduler.running is False

    def test_disabled_interval(self):
        """Test that a zero interval disables automatic backups."""
        scheduler = BackupScheduler(Mock(), 0)

        assert scheduler.enabled is False
        assert scheduler.next_run is None

    @pytest.mark.asyncio
    async def test_run_once_success(self):
        """Test a successful backup run."""
        backup = Mock()
        backup.execute.return_value = ["192.168.1.10"]
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once()

        backup.execute.assert_called_once_with()
        assert scheduler.last_backed_up == ["192.168.1.10"]
        assert scheduler.last_error is None
        assert scheduler.run_count == 1
        assert scheduler.last_run is not None

    @pytest.mark.asyncio
    async def test_run_once_failure(self):
        """Test a failed backup run."""
        backup = Mock()
        backup.execute.side_effect = RuntimeError("boom")
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once()

        assert scheduler.last_backed_up == []
        assert scheduler.last_error == "boom"
        assert scheduler.run_count == 1
        assert scheduler.last_run is not None

    @pytest.mark.asyncio
    async def test_run_once_resets_previous_result_on_failure(self):
        """Test that a failed run clears the previous successful result."""
        backup = Mock()
        backup.execute.side_effect = [["192.168.1.10"], RuntimeError("boom")]
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once()
        assert scheduler.last_backed_up == ["192.168.1.10"]

        await scheduler.run_once()
        assert scheduler.last_backed_up == []
        assert scheduler.last_error == "boom"
        assert scheduler.run_count == 2

    @pytest.mark.asyncio
    @patch("shelly_backup.scheduler.asyncio.to_thread", new_callable=AsyncMock)
    async def test_run_once_uses_to_thread(self, mock_to_thread):
        """Test that the backup runs outside the event loop."""
        mock_to_thread.return_value = ["192.168.1.10"]
        backup = Mock()
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once()

        mock_to_thread.assert_awaited_once_with(backup.execute)

    @pytest.mark.asyncio
    async def test_run_once_serialises_concurrent_calls(self):
        """Test that concurrent runs do not execute in parallel."""
        backup = SlowBackup()
        scheduler = BackupScheduler(backup, 60)

        await asyncio.gather(scheduler.run_once(), scheduler.run_once())

        assert backup.calls == 2
        assert backup.max_active == 1

    @pytest.mark.asyncio
    async def test_next_run(self):
        """Test the next run time after a backup."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once()

        assert scheduler.next_run == scheduler.last_run + timedelta(seconds=60)

    @pytest.mark.asyncio
    async def test_next_run_disabled(self):
        """Test that there is no next run when automatic backups are disabled."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(backup, 0)

        await scheduler.run_once()

        assert scheduler.next_run is None

    @pytest.mark.asyncio
    async def test_start_disabled(self):
        """Test that starting a disabled scheduler does nothing."""
        backup = Mock()
        scheduler = BackupScheduler(backup, 0)

        await scheduler.start()
        await scheduler.stop()

        assert scheduler.started is False
        backup.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_runs_immediately_and_stops(self):
        """Test that the scheduler runs immediately and stops cleanly."""
        backup = Mock()
        backup.execute.return_value = ["192.168.1.10"]
        scheduler = BackupScheduler(backup, 3600)

        await scheduler.start()
        try:
            for _ in range(100):
                if scheduler.run_count:
                    break
                await asyncio.sleep(0.01)
            assert scheduler.run_count == 1
            assert scheduler.last_backed_up == ["192.168.1.10"]
            assert scheduler.started is True
        finally:
            await scheduler.stop()

        assert scheduler.started is False

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        """Test that stopping a scheduler that was never started is safe."""
        scheduler = BackupScheduler(Mock(), 60)

        await scheduler.stop()

        assert scheduler.started is False
