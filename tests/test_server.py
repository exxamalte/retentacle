"""Tests for the web server module."""

from datetime import UTC, datetime
import json
import tempfile
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

from shelly_backup.configuration import Configuration
from shelly_backup.discovery import DiscoveredDevice
from shelly_backup.server import (
    _effective_ip_addresses,
    _format_datetime_local,
    _format_interval,
    _format_interval_value,
    _format_timestamp,
    _interval_form_values,
    _parse_datetime_local,
    _parse_ip_addresses,
    _saved_ip_addresses,
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

    def test_interval_form_values(self):
        """Test mapping intervals to presets and the custom field."""
        assert _interval_form_values(3600) == ("1h", "")
        assert _interval_form_values(5400) == ("custom", "90m")
        assert _interval_form_values(0) == ("1h", "")

    def test_datetime_local_round_trip(self):
        """Test formatting and parsing HTML datetime-local values."""
        timestamp = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)

        assert _parse_datetime_local(_format_datetime_local(timestamp)) == timestamp


class TestIpAddresses:
    """Tests for the device IP address helpers."""

    def test_parse_ip_addresses(self):
        """Test parsing one or more IP addresses."""
        assert _parse_ip_addresses("192.168.1.10") == ["192.168.1.10"]
        assert _parse_ip_addresses("192.168.1.10\n192.168.1.11, 2001:0db8::0001") == [
            "192.168.1.10",
            "192.168.1.11",
            "2001:db8::1",
        ]

    def test_parse_ip_addresses_rejects_empty_input(self):
        """Test that empty input is rejected."""
        with pytest.raises(ValueError, match="at least one"):
            _parse_ip_addresses("  \n ")

    def test_parse_ip_addresses_rejects_invalid_values(self):
        """Test that invalid IP addresses are rejected."""
        with pytest.raises(ValueError, match="not-an-ip"):
            _parse_ip_addresses("192.168.1.10\nnot-an-ip")

    def test_saved_ip_addresses_ignores_invalid_values(self):
        """Test that invalid saved values are ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Configuration(None, tmpdir, None).database_path())
            store.save_settings(
                {"ip_addresses": ["192.168.1.10", "not-an-ip", "2001:0db8::1"]}
            )

            assert _saved_ip_addresses(store) == ["192.168.1.10", "2001:db8::1"]

    def test_effective_ip_addresses_prefers_saved_devices(self):
        """Test that devices saved in settings override the configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration("192.168.1.1", tmpdir, None)
            store = BackupStore(config.database_path())
            store.save_settings({"ip_addresses": ["10.0.0.1"]})

            assert _effective_ip_addresses(config, store) == ["10.0.0.1"]

    def test_effective_ip_addresses_falls_back_to_configuration(self):
        """Test that configured devices are used without saved devices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration("192.168.1.1", tmpdir, None)
            store = BackupStore(config.database_path())

            assert _effective_ip_addresses(config, store) == ["192.168.1.1"]


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
        assert "Last backup" in response.text
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

    def test_index_shows_last_backup_from_database(self):
        """Test that the overview shows the last backup, not the last server run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[_record(timestamp="2026-11-15T10:00:00+00:00")],
            )
            app.state.scheduler.last_run = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
            with TestClient(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert _format_timestamp("2026-11-15T10:00:00+00:00") in response.text
        assert (
            _format_timestamp(datetime(2026, 11, 16, 12, 0, tzinfo=UTC))
            not in response.text
        )

    def test_index_shows_never_without_backups(self):
        """Test that the overview shows no last backup without records."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.scheduler.last_run = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
            with TestClient(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert "Last backup" in response.text
        assert "Never" in response.text
        assert (
            _format_timestamp(datetime(2026, 11, 16, 12, 0, tzinfo=UTC))
            not in response.text
        )

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
        """Test the settings page shows the current schedule controls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert 'action="/settings"' in response.text
        assert 'name="automatic_backups"' in response.text
        assert 'name="next_run"' in response.text
        assert 'name="interval_preset"' in response.text
        assert 'name="interval_custom"' in response.text
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

    def test_settings_page_shows_retention_section(self):
        """Test that the settings page shows the default retention policy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert 'action="/settings/retention"' in response.text
        assert response.text.count('name="retention_') == 4
        assert 'id="retention_monthly"' in response.text
        assert 'id="retention_weekly"' in response.text
        assert 'id="retention_daily"' in response.text
        assert 'id="retention_hourly"' in response.text
        assert "Save retention" in response.text
        assert 'value="6"' in response.text
        assert 'value="4"' in response.text
        assert 'value="7"' in response.text
        assert 'value="4"' in response.text

    def test_settings_page_shows_devices_section(self):
        """Test that the settings page offers device management."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert 'action="/settings/devices"' in response.text
        assert 'name="ip_addresses"' in response.text
        assert "Add devices" in response.text
        assert "No devices added yet" in response.text

    def test_settings_page_lists_saved_devices(self):
        """Test that saved devices are listed with a remove action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10", "2001:db8::1"]})
            with TestClient(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert "192.168.1.10" in response.text
        assert "2001:db8::1" in response.text
        assert 'action="/settings/devices/remove"' in response.text

    def test_add_devices(self):
        """Test adding device IP addresses via the settings page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/devices",
                    data={"ip_addresses": "192.168.1.10\n2001:0db8::0001"},
                    follow_redirects=False,
                )

            assert response.status_code == 303
            assert response.headers["location"] == "/settings?saved=1"
            assert store.get_settings() == {
                "ip_addresses": ["192.168.1.10", "2001:db8::1"]
            }

    def test_add_devices_preserves_existing_settings(self):
        """Test that adding devices keeps the interval and future settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"interval": 3600, "future_setting": "keep me"})
            with TestClient(app) as client:
                response = client.post(
                    "/settings/devices",
                    data={"ip_addresses": "192.168.1.10"},
                    follow_redirects=False,
                )

            assert response.status_code == 303
            assert store.get_settings() == {
                "interval": 3600,
                "future_setting": "keep me",
                "ip_addresses": ["192.168.1.10"],
            }

    def test_add_devices_deduplicates(self):
        """Test that adding an existing device does not create duplicates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with TestClient(app) as client:
                response = client.post(
                    "/settings/devices",
                    data={"ip_addresses": "192.168.1.10\n192.168.1.11"},
                    follow_redirects=False,
                )

            assert response.status_code == 303
            assert store.get_settings() == {
                "ip_addresses": ["192.168.1.10", "192.168.1.11"]
            }

    def test_add_devices_rejects_invalid_addresses(self):
        """Test that invalid device addresses are rejected without saving."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/devices",
                    data={"ip_addresses": "192.168.1.10\nnot-an-ip"},
                    follow_redirects=False,
                )

            assert response.status_code == 400
            assert "Could not save settings" in response.text
            assert "valid IPv4 or IPv6" in response.text
            assert "192.168.1.10\nnot-an-ip" in response.text
            assert store.get_settings() == {}

    def test_add_devices_requires_a_selection(self):
        """Test that an empty device selection is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/devices",
                    content=b"",
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )

            assert response.status_code == 400
            assert "Select at least one device to add." in response.text
            assert store.get_settings() == {}

    def test_remove_device(self):
        """Test removing a device IP address."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings(
                {
                    "interval": 3600,
                    "ip_addresses": ["192.168.1.10", "192.168.1.11"],
                }
            )
            with TestClient(app) as client:
                response = client.post(
                    "/settings/devices/remove",
                    data={"ip_address": "192.168.1.10"},
                    follow_redirects=False,
                )

            assert response.status_code == 303
            assert response.headers["location"] == "/settings?saved=1"
            assert store.get_settings() == {
                "interval": 3600,
                "ip_addresses": ["192.168.1.11"],
            }

    def test_discover_devices_offers_new_devices(self):
        """Test that discovery offers devices that are not configured yet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with (
                patch(
                    "shelly_backup.server.discover_shelly_device_details",
                    return_value=[
                        DiscoveredDevice("192.168.1.10"),
                        DiscoveredDevice(
                            "192.168.1.11",
                            name="ShellyPlus1PM-ABC",
                            model="Plus1PM",
                            generation=2,
                            version="1.4.4",
                        ),
                        DiscoveredDevice("2001:0db8::0001"),
                    ],
                ),
                TestClient(app) as client,
            ):
                response = client.post("/settings/discover")

        assert response.status_code == 200
        assert "New devices found" in response.text
        assert response.text.count('class="checkbox-cell"') == 2
        assert 'value="192.168.1.11"' in response.text
        assert 'value="2001:db8::1"' in response.text
        assert "ShellyPlus1PM-ABC" in response.text
        assert "Plus1PM · gen 2 · 1.4.4" in response.text
        assert "data-discovery-empty hidden>No new devices found." not in response.text

    def test_discover_devices_reports_when_nothing_is_new(self):
        """Test that discovery reports when all found devices are configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with (
                patch(
                    "shelly_backup.server.discover_shelly_device_details",
                    return_value=[DiscoveredDevice("192.168.1.10")],
                ),
                TestClient(app) as client,
            ):
                response = client.post("/settings/discover")

        assert response.status_code == 200
        assert "No new devices found." in response.text
        assert 'class="checkbox-cell"' not in response.text

    def test_discover_devices_handles_errors(self):
        """Test that discovery errors are shown without breaking the page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with (
                patch(
                    "shelly_backup.server.discover_shelly_device_details",
                    side_effect=RuntimeError("discovery failed"),
                ),
                TestClient(app) as client,
            ):
                response = client.post("/settings/discover")

        assert response.status_code == 200
        assert "Discovery failed" in response.text
        assert "Unable to discover devices" in response.text

    def test_discover_devices_normalizes_and_deduplicates(self):
        """Test that discovered devices are normalized and deduplicated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with (
                patch(
                    "shelly_backup.server.discover_shelly_device_details",
                    return_value=[
                        DiscoveredDevice("192.168.1.11"),
                        DiscoveredDevice("192.168.1.11"),
                        DiscoveredDevice("2001:0db8::0001"),
                        DiscoveredDevice("not-an-ip"),
                    ],
                ),
                TestClient(app) as client,
            ):
                response = client.post("/settings/discover")

        assert response.status_code == 200
        assert response.text.count('class="checkbox-cell"') == 2
        assert 'value="2001:db8::1"' in response.text
        assert "not-an-ip" not in response.text

    def test_discover_devices_sorts_by_ip_address(self):
        """Test that discovered devices are sorted numerically by IP address."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with (
                patch(
                    "shelly_backup.server.discover_shelly_device_details",
                    return_value=[
                        DiscoveredDevice("192.168.1.20"),
                        DiscoveredDevice("2001:db8::1"),
                        DiscoveredDevice("192.168.1.3"),
                    ],
                ),
                TestClient(app) as client,
            ):
                response = client.post("/settings/discover")

        assert response.status_code == 200
        assert (
            response.text.index('value="192.168.1.3"')
            < response.text.index('value="192.168.1.20"')
            < response.text.index('value="2001:db8::1"')
        )

    def test_discover_devices_streams_new_devices(self):
        """Test that discovery streams filtered devices as NDJSON events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with (
                patch(
                    "shelly_backup.server.iter_shelly_device_details",
                    return_value=iter(
                        [
                            DiscoveredDevice("192.168.1.10"),
                            DiscoveredDevice(
                                "192.168.1.20",
                                name="ShellyPlus1PM-ABC",
                                model="Plus1PM",
                                generation=2,
                                version="1.4.4",
                            ),
                            DiscoveredDevice("192.168.1.20"),
                            DiscoveredDevice("2001:0db8::0001"),
                            DiscoveredDevice("not-an-ip"),
                        ]
                    ),
                ),
                TestClient(app) as client,
            ):
                response = client.post("/settings/discover/stream")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert response.headers["cache-control"] == "no-store"
        events = [json.loads(line) for line in response.text.splitlines()]
        assert [event["ip_address"] for event in events] == [
            "192.168.1.20",
            "2001:db8::1",
        ]
        assert events[0]["name"] == "ShellyPlus1PM-ABC"
        assert events[0]["details"] == "Plus1PM · gen 2 · 1.4.4"
        assert events[0]["sort_key"].startswith("4:")
        assert events[1]["sort_key"].startswith("6:")

    def test_discover_devices_streams_errors(self):
        """Test that discovery failures are streamed as an error event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with (
                patch(
                    "shelly_backup.server.iter_shelly_device_details",
                    side_effect=RuntimeError("discovery failed"),
                ),
                TestClient(app) as client,
            ):
                response = client.post("/settings/discover/stream")

        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events == [
            {
                "error": (
                    "Unable to discover devices. Check the server logs and try again."
                )
            }
        ]

    def test_add_devices_from_discovery_selection(self):
        """Test adding multiple devices selected from discovery results."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/devices",
                    content=urlencode(
                        [
                            ("ip_addresses", "192.168.1.10"),
                            ("ip_addresses", "2001:0db8::0001"),
                        ]
                    ),
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    follow_redirects=False,
                )

            assert response.status_code == 303
            assert store.get_settings() == {
                "ip_addresses": ["192.168.1.10", "2001:db8::1"]
            }

    def test_trigger_backup_uses_saved_devices(self):
        """Test that saved devices override registered devices for a manual run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[_record(ip_address="192.168.1.10")],
            )
            store.save_settings({"ip_addresses": ["10.0.0.1"]})
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post("/backup", follow_redirects=False)

                scheduler.run_once.assert_awaited_once_with(["10.0.0.1"])

        assert response.status_code == 303

    def test_save_retention(self):
        """Test saving a backup retention policy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/retention",
                    data={
                        "retention_monthly": "6",
                        "retention_weekly": "3",
                        "retention_daily": "5",
                        "retention_hourly": "2",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/settings?saved=1"
                assert store.get_settings() == {
                    "retention": {
                        "monthly": 6,
                        "weekly": 3,
                        "daily": 5,
                        "hourly": 2,
                    }
                }

    def test_save_retention_preserves_existing_settings(self):
        """Test that saving retention keeps the schedule and devices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings(
                {
                    "interval": 3600,
                    "ip_addresses": ["192.168.1.10"],
                    "future_setting": "keep me",
                }
            )
            with TestClient(app) as client:
                response = client.post(
                    "/settings/retention",
                    data={
                        "retention_monthly": "12",
                        "retention_weekly": "4",
                        "retention_daily": "7",
                        "retention_hourly": "24",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                settings = store.get_settings()
                assert settings["interval"] == 3600
                assert settings["ip_addresses"] == ["192.168.1.10"]
                assert settings["future_setting"] == "keep me"
                assert settings["retention"] == {
                    "monthly": 12,
                    "weekly": 4,
                    "daily": 7,
                    "hourly": 24,
                }

    def test_save_retention_rejects_negative_values(self):
        """Test that negative retention values are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/retention",
                    data={
                        "retention_monthly": "-1",
                        "retention_weekly": "4",
                        "retention_daily": "7",
                        "retention_hourly": "24",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert "Retention periods cannot be negative." in response.text
                assert 'value="-1"' in response.text
                assert store.get_settings() == {}

    def test_save_retention_rejects_non_numeric_values(self):
        """Test that non-numeric retention values are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/retention",
                    data={
                        "retention_monthly": "often",
                        "retention_weekly": "4",
                        "retention_daily": "7",
                        "retention_hourly": "24",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert "Retention periods must be whole numbers." in response.text
                assert store.get_settings() == {}

    def test_save_retention_requires_a_positive_value(self):
        """Test that a policy cannot expire every backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/retention",
                    data={
                        "retention_monthly": "0",
                        "retention_weekly": "0",
                        "retention_daily": "0",
                        "retention_hourly": "0",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert (
                    "Keep at least one backup by setting one of the periods above zero."
                    in response.text
                )
                assert store.get_settings() == {}

    def test_save_retention_rejects_excessive_values(self):
        """Test that unreasonably large retention values are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings/retention",
                    data={
                        "retention_monthly": "1001",
                        "retention_weekly": "4",
                        "retention_daily": "7",
                        "retention_hourly": "24",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert "Retention periods cannot exceed 1000." in response.text
                assert store.get_settings() == {}

    def test_save_settings(self):
        """Test saving an automatic backup schedule."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "30m",
                        "interval_custom": "",
                        "next_run": "2099-01-01T12:00",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/settings?saved=1"
                settings = store.get_settings()
                assert settings["interval"] == 1800
                assert settings["automatic_backups"] is True
                assert datetime.fromisoformat(settings["next_run_at"]) == (
                    _parse_datetime_local("2099-01-01T12:00")
                )
                assert scheduler.interval == 1800
                assert scheduler.enabled is True
                assert scheduler.next_run == datetime.fromisoformat(
                    settings["next_run_at"]
                )

    def test_save_settings_uses_custom_interval(self):
        """Test saving a custom interval that is not a preset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "custom",
                        "interval_custom": "45m",
                        "next_run": "2099-01-01T12:00",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert store.get_settings()["interval"] == 2700
                assert app.state.scheduler.interval == 2700

    def test_save_settings_preserves_future_settings(self):
        """Test that saving the schedule keeps other saved settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"interval": 60, "future_setting": "keep me"})
            with TestClient(app) as client:
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "1h",
                        "interval_custom": "",
                        "next_run": "2099-01-01T12:00",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                settings = store.get_settings()
                assert settings["interval"] == 3600
                assert settings["future_setting"] == "keep me"

    def test_save_settings_rejects_invalid_interval(self):
        """Test that invalid intervals are rejected without saving."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "custom",
                        "interval_custom": "soon",
                        "next_run": "2099-01-01T12:00",
                    },
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

    def test_save_settings_requires_next_run_when_enabled(self):
        """Test that enabling automatic backups requires a next run time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "1h",
                        "interval_custom": "",
                        "next_run": "",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert "Choose the next backup date and time." in response.text
                assert store.get_settings() == {}

    def test_save_settings_rejects_zero_interval_when_enabled(self):
        """Test that an enabled schedule needs a positive interval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "custom",
                        "interval_custom": "0",
                        "next_run": "2099-01-01T12:00",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert "Choose an interval greater than zero." in response.text
                assert store.get_settings() == {}

    def test_save_settings_can_disable_automatic_backups(self):
        """Test that disabling automatic backups keeps the interval."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with TestClient(app) as client:
                scheduler = app.state.scheduler
                response = client.post(
                    "/settings",
                    data={
                        "interval_preset": "1h",
                        "interval_custom": "",
                        "next_run": "",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert store.get_settings() == {
                    "interval": 3600,
                    "automatic_backups": False,
                    "next_run_at": None,
                }
                assert scheduler.interval == 3600
                assert scheduler.enabled is False
                assert scheduler.next_run is None

    def test_saved_settings_apply_on_startup(self):
        """Test that settings saved via the web UI override the configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, interval=0)
            store = BackupStore(config.database_path())
            next_run_at = _parse_datetime_local("2099-01-01T12:00")
            store.save_settings(
                {
                    "interval": 900,
                    "automatic_backups": True,
                    "next_run_at": next_run_at.isoformat(),
                }
            )

            app = create_app(config, store=store)

        assert app.state.scheduler.interval == 900
        assert app.state.scheduler.enabled is True
        assert app.state.scheduler.next_run == next_run_at

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
