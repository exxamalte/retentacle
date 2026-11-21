"""Tests for the backup scheduler."""

import asyncio
from datetime import UTC, datetime, timedelta
import threading
import time
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest

from retentacle.scheduler import BackupScheduler


class SlowBackup:
    """Backup fake that records concurrent executions."""

    def __init__(self):
        """Initialise the fake."""
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def execute(
        self, ip_addresses=None, on_start=None, on_progress=None, *, automatic=False
    ):
        """Run a slow fake backup."""
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        return []

    def backup_device(self, ip_address):
        """Run a slow fake single-device backup."""
        return bool(self.execute())


class ProgressBackup:
    """Backup fake that reports progress."""

    def execute(
        self, ip_addresses=None, on_start=None, on_progress=None, *, automatic=False
    ):
        """Report progress for two fake devices."""
        on_start(2)
        on_progress("192.168.1.10", True)
        on_progress("192.168.1.11", False)
        return ["192.168.1.10"]


class BlockingProgressBackup:
    """Backup fake that pauses midway so progress can be inspected."""

    def __init__(self):
        """Initialise the fake."""
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(
        self, ip_addresses=None, on_start=None, on_progress=None, *, automatic=False
    ):
        """Pause after the first device until released."""
        on_start(2)
        on_progress("192.168.1.10", True)
        self.started.set()
        self.release.wait(timeout=1)
        on_progress("192.168.1.11", True)
        return ["192.168.1.10", "192.168.1.11"]


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

        backup.execute.assert_called_once_with(None, ANY, ANY, automatic=False)
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
    @patch("retentacle.scheduler.asyncio.to_thread", new_callable=AsyncMock)
    async def test_run_once_uses_to_thread(self, mock_to_thread):
        """Test that the backup runs outside the event loop."""
        mock_to_thread.return_value = ["192.168.1.10"]
        backup = Mock()
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once()

        mock_to_thread.assert_awaited_once_with(
            backup.execute, None, ANY, ANY, automatic=False
        )

    @pytest.mark.asyncio
    async def test_run_once_with_ip_addresses(self):
        """Test that run_once backs up the provided devices."""
        backup = Mock()
        backup.execute.return_value = ["192.168.1.10"]
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once(["192.168.1.10"])

        backup.execute.assert_called_once_with(
            ["192.168.1.10"], ANY, ANY, automatic=False
        )
        assert scheduler.last_backed_up == ["192.168.1.10"]

    @pytest.mark.asyncio
    async def test_run_once_uses_ip_addresses_provider(self):
        """Test that scheduled runs use devices from the provider."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(
            backup,
            60,
            ip_addresses_provider=lambda: ["192.168.1.10", "192.168.1.11"],
        )

        await scheduler.run_once()

        backup.execute.assert_called_once_with(
            ["192.168.1.10", "192.168.1.11"], ANY, ANY, automatic=False
        )

    @pytest.mark.asyncio
    async def test_explicit_ip_addresses_override_provider(self):
        """Test that explicitly provided devices take precedence."""
        backup = Mock()
        backup.execute.return_value = []
        provider = Mock(return_value=["192.168.1.10"])
        scheduler = BackupScheduler(backup, 60, ip_addresses_provider=provider)

        await scheduler.run_once(["192.168.1.20"])

        backup.execute.assert_called_once_with(
            ["192.168.1.20"], ANY, ANY, automatic=False
        )
        provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_once_reports_provider_errors(self):
        """Test that a failing device provider is reported as a run error."""
        backup = Mock()
        provider = Mock(side_effect=RuntimeError("provider failed"))
        scheduler = BackupScheduler(backup, 60, ip_addresses_provider=provider)

        await scheduler.run_once()

        assert scheduler.last_error == "provider failed"
        backup.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_once_tracks_progress(self):
        """Test that a backup run reports per-device progress."""
        scheduler = BackupScheduler(ProgressBackup(), 60)

        await scheduler.run_once()

        progress = scheduler.progress
        assert progress["total"] == 2
        assert progress["completed"] == 2
        assert progress["percent"] == 100
        assert progress["current"] is None
        assert progress["results"] == [
            {"ip_address": "192.168.1.10", "success": True},
            {"ip_address": "192.168.1.11", "success": False},
        ]

    @pytest.mark.asyncio
    async def test_progress_is_available_while_running(self):
        """Test that progress can be read while a backup is still running."""
        backup = BlockingProgressBackup()
        scheduler = BackupScheduler(backup, 60)

        task = asyncio.create_task(scheduler.run_once())
        try:
            assert await asyncio.to_thread(backup.started.wait, 1) is True
            assert scheduler.running is True
            assert scheduler.progress["total"] == 2
            assert scheduler.progress["completed"] == 1
            assert scheduler.progress["percent"] == 50
        finally:
            backup.release.set()
            await task

        assert scheduler.progress["completed"] == 2
        assert scheduler.progress["percent"] == 100

    @pytest.mark.asyncio
    async def test_run_once_resets_previous_progress(self):
        """Test that a new run clears progress from the previous run."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(backup, 60)
        scheduler.progress_total = 5
        scheduler.progress_completed = 3
        scheduler.progress_results = [{"ip_address": "192.168.1.10"}]

        await scheduler.run_once()

        assert scheduler.progress["total"] == 0
        assert scheduler.progress["completed"] == 0
        assert scheduler.progress["results"] == []

    def test_queue_marks_backup_as_active(self):
        """Test that a queued backup is reported as active immediately."""
        scheduler = BackupScheduler(Mock(), 60)

        assert scheduler.active is False

        scheduler.queue()

        assert scheduler.queued is True
        assert scheduler.active is True

    @pytest.mark.asyncio
    async def test_run_once_clears_queue(self):
        """Test that starting a queued backup clears the queue flag."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(backup, 60)
        scheduler.queue()

        await scheduler.run_once()

        assert scheduler.queued is False
        assert scheduler.active is False

    @pytest.mark.asyncio
    async def test_run_once_serialises_concurrent_calls(self):
        """Test that concurrent runs do not execute in parallel."""
        backup = SlowBackup()
        scheduler = BackupScheduler(backup, 60)

        await asyncio.gather(scheduler.run_once(), scheduler.run_once())

        assert backup.calls == 2
        assert backup.max_active == 1

    @pytest.mark.asyncio
    @patch("retentacle.scheduler.asyncio.to_thread", new_callable=AsyncMock)
    async def test_run_device_uses_to_thread(self, mock_to_thread):
        """Test that a single-device backup runs outside the event loop."""
        mock_to_thread.return_value = True
        backup = Mock()
        scheduler = BackupScheduler(backup, 60)

        assert await scheduler.run_device("192.168.1.10") is True

        mock_to_thread.assert_awaited_once_with(backup.backup_device, "192.168.1.10")

    @pytest.mark.asyncio
    async def test_run_device_does_not_change_scheduler_status(self):
        """Test that a manual device backup does not affect scheduling."""
        backup = Mock()
        backup.backup_device.return_value = True
        scheduler = BackupScheduler(backup, 60)

        assert await scheduler.run_device("192.168.1.10") is True

        assert scheduler.run_count == 0
        assert scheduler.last_run is None
        assert scheduler.last_backed_up == []

    @pytest.mark.asyncio
    async def test_run_device_reports_progress(self):
        """Test that a manual device backup reports progress."""
        backup = Mock()
        backup.backup_device.return_value = True
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_device("192.168.1.10")

        progress = scheduler.progress
        assert progress["total"] == 1
        assert progress["completed"] == 1
        assert progress["percent"] == 100
        assert progress["current"] is None
        assert progress["results"] == [{"ip_address": "192.168.1.10", "success": True}]

    @pytest.mark.asyncio
    async def test_run_device_reports_failure_progress(self):
        """Test that a failed manual device backup is reported."""
        backup = Mock()
        backup.backup_device.return_value = False
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_device("192.168.1.10")

        assert scheduler.progress["results"] == [
            {"ip_address": "192.168.1.10", "success": False}
        ]

    @pytest.mark.asyncio
    async def test_run_device_serialises_with_run_once(self):
        """Test that manual and scheduled backups do not run in parallel."""
        backup = SlowBackup()
        scheduler = BackupScheduler(backup, 60)

        await asyncio.gather(scheduler.run_once(), scheduler.run_device("192.168.1.10"))

        assert backup.calls == 2
        assert backup.max_active == 1

    @pytest.mark.asyncio
    async def test_manual_run_does_not_change_next_run(self):
        """Test that a manual backup keeps the scheduled next run."""
        backup = Mock()
        backup.execute.return_value = []
        next_run = datetime.now(UTC) + timedelta(seconds=600)
        scheduler = BackupScheduler(backup, 60, next_run_at=next_run)

        await scheduler.run_once()

        assert scheduler.next_run == next_run

    @pytest.mark.asyncio
    async def test_start_waits_for_explicit_next_run(self):
        """Test that an explicit future next run is not executed immediately."""
        backup = Mock()
        backup.execute.return_value = []
        next_run = datetime.now(UTC) + timedelta(seconds=600)
        scheduler = BackupScheduler(backup, 60, next_run_at=next_run)

        await scheduler.start()
        try:
            await asyncio.sleep(0.05)
            assert scheduler.run_count == 0
            assert scheduler.next_run == next_run
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_scheduled_run_updates_and_saves_next_run(self):
        """Test that a scheduled run schedules and persists the following run."""
        backup = Mock()
        backup.execute.return_value = []
        saved = []
        scheduler = BackupScheduler(backup, 3600, on_schedule_changed=saved.append)

        await scheduler.start()
        try:
            for _ in range(100):
                if scheduler.run_count:
                    break
                await asyncio.sleep(0.01)

            assert scheduler.run_count == 1
            assert scheduler.next_run == scheduler.last_run + timedelta(seconds=3600)
            assert saved[-1] == scheduler.next_run
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_set_schedule_disables_automatic_backups(self):
        """Test that disabling the schedule clears the next run."""
        scheduler = BackupScheduler(
            Mock(), 60, next_run_at=datetime.now(UTC) + timedelta(seconds=60)
        )

        await scheduler.set_schedule(interval=60, enabled=False, next_run_at=None)

        assert scheduler.enabled is False
        assert scheduler.next_run is None

    @pytest.mark.asyncio
    async def test_set_schedule_waits_for_selected_next_run(self):
        """Test that enabling the schedule waits for the selected next run."""
        backup = Mock()
        backup.execute.return_value = []
        next_run = datetime.now(UTC) + timedelta(seconds=600)
        scheduler = BackupScheduler(backup, 0)

        await scheduler.set_schedule(interval=60, enabled=True, next_run_at=next_run)
        try:
            await asyncio.sleep(0.05)
            assert scheduler.started is True
            assert scheduler.run_count == 0
            assert scheduler.next_run == next_run
        finally:
            await scheduler.stop()

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
    async def test_start_uses_ip_addresses_provider(self):
        """Test that the background loop uses devices from the provider."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(
            backup,
            3600,
            ip_addresses_provider=lambda: ["192.168.1.10"],
        )

        await scheduler.start()
        try:
            for _ in range(100):
                if scheduler.run_count:
                    break
                await asyncio.sleep(0.01)

            backup.execute.assert_called_once_with(
                ["192.168.1.10"], ANY, ANY, automatic=True
            )
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        """Test that stopping a scheduler that was never started is safe."""
        scheduler = BackupScheduler(Mock(), 60)

        await scheduler.stop()

        assert scheduler.started is False

    @pytest.mark.asyncio
    async def test_set_interval_disables_scheduler(self):
        """Test that setting a zero interval disables automatic backups."""
        scheduler = BackupScheduler(Mock(), 60)

        await scheduler.set_interval(0)

        assert scheduler.interval == 0
        assert scheduler.enabled is False
        assert scheduler.next_run is None
        assert scheduler.started is False

    @pytest.mark.asyncio
    async def test_set_interval_updates_next_run(self):
        """Test that changing the interval updates the next run time."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(backup, 3600)

        await scheduler.start()
        try:
            await scheduler.set_interval(120)

            assert scheduler.interval == 120
            assert scheduler.next_run is not None
            assert (
                datetime.now(UTC) - timedelta(seconds=5)
                <= scheduler.next_run
                <= datetime.now(UTC) + timedelta(seconds=120)
            )
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_set_interval_starts_disabled_scheduler(self):
        """Test that enabling a disabled scheduler starts it."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(backup, 0)

        await scheduler.set_interval(0.01)
        try:
            for _ in range(200):
                if scheduler.run_count:
                    break
                await asyncio.sleep(0.01)

            assert scheduler.started is True
            assert scheduler.run_count == 1
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_set_interval_reschedules_running_scheduler(self):
        """Test that a changed interval takes effect without a restart."""
        backup = Mock()
        backup.execute.return_value = []
        scheduler = BackupScheduler(backup, 3600)

        await scheduler.start()
        try:
            for _ in range(200):
                if scheduler.run_count:
                    break
                await asyncio.sleep(0.01)
            assert scheduler.run_count == 1

            await scheduler.set_interval(0.01)

            for _ in range(200):
                if scheduler.run_count >= 2:
                    break
                await asyncio.sleep(0.01)
            assert scheduler.run_count >= 2
        finally:
            await scheduler.stop()


class RecordingBackup:
    """Backup fake that records the trigger of each run."""

    def __init__(self):
        """Initialise the fake."""
        self.calls: list[bool] = []
        self.done = threading.Event()

    def execute(
        self, ip_addresses=None, on_start=None, on_progress=None, *, automatic=False
    ):
        """Record the automatic flag of this run."""
        self.calls.append(automatic)
        self.done.set()
        return []


class TestSchedulerRunTrigger:
    """Tests for marking automatic and manual backup runs."""

    @pytest.mark.asyncio
    async def test_run_once_defaults_to_manual(self):
        """Test that a directly requested run is not marked automatic."""
        backup = RecordingBackup()
        scheduler = BackupScheduler(backup, 60)

        await scheduler.run_once(["192.168.1.10"])

        assert backup.calls == [False]

    @pytest.mark.asyncio
    async def test_loop_run_is_marked_automatic(self):
        """Test that scheduled loop runs record the automatic trigger."""
        backup = RecordingBackup()
        scheduler = BackupScheduler(backup, 60, next_run_at=datetime.now(UTC))
        await scheduler.start()
        try:
            assert await asyncio.to_thread(backup.done.wait, 2)
        finally:
            await scheduler.stop()

        assert backup.calls == [True]

    @pytest.mark.asyncio
    async def test_start_clamps_missed_next_run(self):
        """Test that a next run in the past is caught up immediately."""
        backup = RecordingBackup()
        missed = datetime.now(UTC) - timedelta(days=1)
        scheduler = BackupScheduler(backup, 86400, next_run_at=missed)

        await scheduler.start()
        try:
            assert scheduler.next_run > missed
            assert await asyncio.to_thread(backup.done.wait, 2)
            await asyncio.sleep(0)
            assert backup.calls == [True]
            assert scheduler.next_run > datetime.now(UTC)
        finally:
            await scheduler.stop()
