"""Tests for the web server module."""

import tempfile
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from shelly_backup.configuration import Configuration
from shelly_backup.server import _format_interval, create_app, run_server
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

                scheduler.run_once.assert_awaited_once()

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
