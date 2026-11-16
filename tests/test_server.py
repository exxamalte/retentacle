"""Tests for the web server module."""

import tempfile
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
import pytest

from shelly_backup.configuration import Configuration
from shelly_backup.server import (
    _format_interval,
    _format_interval_value,
    create_app,
    run_server,
)
from shelly_backup.storage import BackupStore


def _record(
    ip_address="192.168.1.10",
    timestamp="2026-11-15T10:00:00+00:00",
    generation=2,
    device_info=None,
    settings=None,
):
    """Create a backup record for server tests."""
    return {
        "ip_address": ip_address,
        "timestamp": timestamp,
        "generation": generation,
        "device_info": device_info
        if device_info is not None
        else {"gen": generation, "name": "Kitchen"},
        "settings": settings or {"device": {"name": "Kitchen"}},
    }


def _create_app(tmpdir, records=None):
    """Create a test app with an optional set of seeded records."""
    config = Configuration(None, tmpdir, None, interval=0)
    store = BackupStore(config.database_path())
    for record in records or []:
        store.add(record)
    return create_app(config, store=store), store


class TestFormatInterval:
    """Tests for the interval formatting helper."""

    def test_format_interval(self):
        """Test human-readable interval formatting."""
        assert _format_interval(0) == "disabled"
        assert _format_interval(1) == "1 second"
        assert _format_interval(60) == "1 minute"
        assert _format_interval(3600) == "1 hour"
        assert _format_interval(5400) == "1 hour 30 minutes"
        assert _format_interval(86400) == "1 day"

    def test_format_interval_value(self):
        """Test compact interval formatting for form fields."""
        assert _format_interval_value(0) == "0"
        assert _format_interval_value(1) == "1s"
        assert _format_interval_value(30) == "30s"
        assert _format_interval_value(60) == "1m"
        assert _format_interval_value(3600) == "1h"
        assert _format_interval_value(5400) == "90m"
        assert _format_interval_value(86400) == "1d"


class TestServer:
    """Tests for the web server routes."""

    def test_index_without_backups(self):
        """Test the overview page without any backups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert "No backups yet" in response.text
        assert "disabled" in response.text

    def test_index_with_devices(self):
        """Test the overview page lists devices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[
                    _record(),
                    _record(
                        ip_address="192.168.1.11",
                        generation=1,
                        device_info={"gen": 1, "type": "SHSW-1"},
                    ),
                ],
            )
            with TestClient(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert "Kitchen" in response.text
        assert "SHSW-1" in response.text
        assert "192.168.1.10" in response.text
        assert "192.168.1.11" in response.text
        assert "View backups" in response.text

    def test_index_shows_status_and_backup_started_notice(self):
        """Test the overview page shows scheduler status and the notice."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/?backup_started=1")

        assert response.status_code == 200
        assert "Backup started" in response.text
        assert "Run backup now" in response.text
        assert "Backup interval" in response.text
        assert "Last run" in response.text
        assert "Next run" in response.text
        assert "Automatic backups off" in response.text
        assert "No backups yet" in response.text

    def test_index_shows_last_error(self):
        """Test the overview page shows the last scheduler error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.scheduler.last_error = "Device unreachable"
            with TestClient(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert "Last backup failed" in response.text
        assert "Device unreachable" in response.text

    def test_index_has_progress_bar_and_live_updates(self):
        """Test the page includes the progress bar and status polling."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert "data-backup-progress" in response.text
        assert "data-progress-fill" in response.text
        assert "data-scheduler-status" in response.text
        assert "/api/status" in response.text
        assert "window.location.replace" in response.text

    def test_api_status(self):
        """Test the status API returns scheduler and progress information."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["enabled"] is False
        assert data["active"] is False
        assert data["queued"] is False
        assert data["devices"] == 1
        assert data["progress"] == {
            "total": 0,
            "completed": 0,
            "current": None,
            "percent": 0,
            "results": [],
        }

    def test_api_status_reports_progress(self):
        """Test the status API exposes per-device backup progress."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            scheduler = app.state.scheduler
            scheduler.progress_total = 3
            scheduler.progress_completed = 1
            scheduler.progress_current = "192.168.1.11"
            scheduler.progress_results = [
                {"ip_address": "192.168.1.10", "success": True}
            ]
            with TestClient(app) as client:
                response = client.get("/api/status")

        data = response.json()
        assert data["progress"]["total"] == 3
        assert data["progress"]["completed"] == 1
        assert data["progress"]["current"] == "192.168.1.11"
        assert data["progress"]["percent"] == 33
        assert data["progress"]["results"] == [
            {"ip_address": "192.168.1.10", "success": True}
        ]

    def test_device_page(self):
        """Test the device page lists backups newest first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    _record(timestamp="2026-11-15T10:00:00+00:00"),
                    _record(timestamp="2026-11-15T12:00:00+00:00"),
                ],
            )
            with TestClient(app) as client:
                response = client.get("/devices/192.168.1.10")
                assert len(store.by_device("192.168.1.10")) == 2

        assert response.status_code == 200
        assert "Kitchen" in response.text
        assert "/backups/2" in response.text
        assert "/backups/1" in response.text
        assert "Download" in response.text
        assert "Generation 2" in response.text

    def test_device_page_shows_model(self):
        """Test the device page shows the device model."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[_record(device_info={"gen": 2, "model": "SNSW-001P16EU"})],
            )
            with TestClient(app) as client:
                response = client.get("/devices/192.168.1.10")

        assert response.status_code == 200
        assert "SNSW-001P16EU" in response.text

    def test_device_page_has_manual_backup_button(self):
        """Test the device page offers a manual backup action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                response = client.get("/devices/192.168.1.10")

        assert response.status_code == 200
        assert 'action="/devices/192.168.1.10/backup"' in response.text
        assert "Back up this device" in response.text
        assert "data-backup-progress" in response.text
        assert 'action="/backup?redirect=/devices/192.168.1.10"' in response.text

    def test_pages_use_generation_1_configured_name(self):
        """Test that generation 1 devices show their configured name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[
                    _record(
                        ip_address="10.10.4.130",
                        generation=1,
                        device_info={"gen": 1, "type": "SHSW-25"},
                        settings={"settings": {"name": "Dining Room"}},
                    )
                ],
            )
            with TestClient(app) as client:
                index_response = client.get("/")
                device_response = client.get("/devices/10.10.4.130")
                doc_id = app.state.store.all()[0]["doc_id"]
                backup_response = client.get(f"/backups/{doc_id}")

        assert "Dining Room" in index_response.text
        assert "Dining Room" in device_response.text
        assert "Dining Room" in backup_response.text

    def test_manual_device_backup(self):
        """Test backing up a single device from its detail page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_device = AsyncMock(return_value=True)
                response = client.post(
                    "/devices/192.168.1.10/backup", follow_redirects=False
                )

                scheduler.run_device.assert_awaited_once_with("192.168.1.10")

        assert response.status_code == 303
        assert response.headers["location"] == "/devices/192.168.1.10?backup_done=1"

    def test_manual_device_backup_failure(self):
        """Test a failed single-device backup redirects with an error flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_device = AsyncMock(return_value=False)
                response = client.post(
                    "/devices/192.168.1.10/backup", follow_redirects=False
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/devices/192.168.1.10?backup_failed=1"

    def test_manual_device_backup_unknown_device(self):
        """Test that an unknown device cannot be backed up manually."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_device = AsyncMock(return_value=True)
                response = client.post(
                    "/devices/192.168.1.99/backup", follow_redirects=False
                )

                scheduler.run_device.assert_not_awaited()

        assert response.status_code == 404

    def test_device_page_shows_manual_backup_result(self):
        """Test the device page shows manual backup results."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                done = client.get("/devices/192.168.1.10?backup_done=1")
                failed = client.get("/devices/192.168.1.10?backup_failed=1")

        assert "Backup completed for Kitchen" in done.text
        assert "Backup failed" in failed.text

    def test_legacy_records_render(self):
        """Test that records without generation or device info still render."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[
                    {
                        "ip_address": "192.168.1.20",
                        "timestamp": "2026-11-15T10:00:00+00:00",
                        "settings": {"settings": {}},
                    }
                ],
            )
            with TestClient(app) as client:
                index_response = client.get("/")
                device_response = client.get("/devices/192.168.1.20")
                doc_id = app.state.store.all()[0]["doc_id"]
                backup_response = client.get(f"/backups/{doc_id}")

        assert index_response.status_code == 200
        assert "192.168.1.20" in index_response.text
        assert device_response.status_code == 200
        assert "192.168.1.20" in device_response.text
        assert backup_response.status_code == 200
        assert "192.168.1.20" in backup_response.text

    def test_device_page_unknown_device(self):
        """Test the device page for an unknown device."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/devices/192.168.1.99")

        assert response.status_code == 404

    def test_backup_page(self):
        """Test the single backup page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            doc_id = store.all()[0]["doc_id"]
            with TestClient(app) as client:
                response = client.get(f"/backups/{doc_id}")

        assert response.status_code == 200
        assert "Kitchen" in response.text
        assert "settings" in response.text
        assert "Download JSON" in response.text
        assert "Raw backup data" in response.text
        assert "Backup ID" in response.text
        assert f"#{doc_id}" in response.text

    def test_backup_page_unknown_backup(self):
        """Test the backup page for an unknown backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/backups/999")

        assert response.status_code == 404

    def test_download_backup(self):
        """Test downloading a backup as JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            record = store.all()[0]
            with TestClient(app) as client:
                response = client.get(f"/backups/{record['doc_id']}/download")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "shelly-backup-192.168.1.10-" in response.headers["content-disposition"]
        assert response.json() == record

    def test_download_unknown_backup(self):
        """Test downloading an unknown backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/backups/999/download")

        assert response.status_code == 404

    def test_trigger_backup(self):
        """Test triggering a backup run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post("/backup", follow_redirects=False)

                scheduler.run_once.assert_awaited_once_with(None)

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_started=1"

    def test_trigger_backup_marks_run_as_queued(self):
        """Test that triggering a backup makes it visible as active immediately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                client.post("/backup", follow_redirects=False)

                assert scheduler.queued is True
                assert scheduler.active is True

    def test_trigger_backup_returns_to_current_page(self):
        """Test that triggering a backup keeps the user on the current page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    "/backup?redirect=/devices/192.168.1.10",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/devices/192.168.1.10?backup_started=1"

    @pytest.mark.parametrize(
        "redirect",
        [
            "https://example.com",
            "//example.com",
            "/\\example.com",
            "devices/192.168.1.10",
            "",
        ],
    )
    def test_trigger_backup_rejects_unsafe_redirect(self, redirect):
        """Test that the redirect target cannot leave the application."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    f"/backup?redirect={redirect}",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_started=1"

    def test_trigger_backup_drops_redirect_query(self):
        """Test that only the redirect path is used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    "/backup?redirect=/devices/192.168.1.10?foo=bar",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/devices/192.168.1.10?backup_started=1"

    def test_trigger_backup_uses_registered_devices(self):
        """Test that the backup button backs up all registered devices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[
                    _record(ip_address="192.168.1.10"),
                    _record(ip_address="192.168.1.11"),
                    _record(ip_address="10.0.0.1"),
                ],
            )
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post("/backup", follow_redirects=False)

                scheduler.run_once.assert_awaited_once_with(
                    ["10.0.0.1", "192.168.1.10", "192.168.1.11"]
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_started=1"

    def test_health(self):
        """Test the health endpoint."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["database"].endswith("shelly_backup.json")
        assert data["devices"] == 1
        assert data["last_run"] is None

    def test_index_links_to_settings(self):
        """Test that the overview page links to the settings page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert 'href="/settings"' in response.text

    def test_settings_page(self):
        """Test the settings page shows the current interval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert 'action="/settings"' in response.text
        assert 'name="interval"' in response.text
        assert 'value="0"' in response.text
        assert "Save settings" in response.text
        assert "Automatic backups off" in response.text

    def test_settings_page_shows_saved_notice(self):
        """Test the settings page confirms a successful save."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/settings?saved=1")

        assert response.status_code == 200
        assert "Settings saved" in response.text

    def test_save_settings(self):
        """Test saving a new backup interval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    "/settings",
                    data={"interval": "30m"},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/settings?saved=1"
                assert store.get_settings() == {"interval": 1800}
                assert scheduler.interval == 1800
                assert scheduler.enabled is True

    def test_save_settings_preserves_future_settings(self):
        """Test that saving the interval keeps other saved settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"interval": 60, "future_setting": "keep me"})
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    "/settings",
                    data={"interval": "1h"},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert store.get_settings() == {
                    "interval": 3600,
                    "future_setting": "keep me",
                }

    def test_save_settings_rejects_invalid_interval(self):
        """Test that invalid intervals are rejected without saving."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                response = client.post(
                    "/settings",
                    data={"interval": "soon"},
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert "Could not save settings" in response.text
                assert 'value="soon"' in response.text
                assert store.get_settings() == {}
                assert scheduler.interval == 0

    def test_save_settings_rejects_missing_interval(self):
        """Test that a missing interval is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post("/settings", data={}, follow_redirects=False)

                assert response.status_code == 400
                assert store.get_settings() == {}

    def test_save_settings_can_disable_automatic_backups(self):
        """Test that a zero interval disables automatic backups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                response = client.post(
                    "/settings",
                    data={"interval": "0"},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert store.get_settings() == {"interval": 0}
                assert scheduler.interval == 0
                assert scheduler.enabled is False

    def test_saved_settings_apply_on_startup(self):
        """Test that settings saved via the web UI override the configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, interval=0)
            store = BackupStore(config.database_path())
            store.save_settings({"interval": 900})

            app = create_app(config, store=store)

        assert app.state.scheduler.interval == 900
        assert app.state.scheduler.enabled is True

    def test_invalid_saved_settings_fall_back_to_configuration(self):
        """Test that invalid saved settings are ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, interval=0)
            store = BackupStore(config.database_path())
            store.save_settings({"interval": "soon"})

            app = create_app(config, store=store)

        assert app.state.scheduler.interval == 0

    def test_lifespan_starts_and_stops_scheduler(self):
        """Test that the app lifespan manages the scheduler."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, interval=0)
            store = BackupStore(config.database_path())
            scheduler = Mock()
            scheduler.start = AsyncMock()
            scheduler.stop = AsyncMock()
            app = create_app(config, store=store, scheduler=scheduler)

            with TestClient(app):
                scheduler.start.assert_awaited_once()

            scheduler.stop.assert_awaited_once()

    @patch("shelly_backup.server.uvicorn.run")
    def test_run_server(self, mock_run):
        """Test running the server with uvicorn."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, interval=0)
            run_server(config)

        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8080
