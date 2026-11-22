"""Tests for the web server module."""

import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import tempfile
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient
import pytest

from retentacle.configuration import Configuration
from retentacle.discovery import DiscoveredDevice
from retentacle.oidc import OIDCError
from retentacle.server import (
    _effective_ip_addresses,
    _format_bytes,
    _format_datetime_local,
    _format_duration,
    _format_interval,
    _format_interval_value,
    _format_timestamp,
    _interval_form_values,
    _parse_datetime_local,
    _parse_ip_addresses,
    _recent_backup_cutoff,
    _save_last_automatic_run,
    _saved_ip_addresses,
    _saved_last_automatic_duration,
    create_app,
    run_server,
)
from retentacle.storage import BackupStore
from retentacle.templating import templates
from retentacle.users import UserRole, UserStore

_ADMIN_EMAIL = "admin@example.com"
_ADMIN_PASSWORD = "unit-test-login-pass-1"


def _record(
    ip_address="192.168.1.10",
    timestamp="2026-11-15T10:00:00+00:00",
    generation=2,
    device_info=None,
    settings=None,
    *,
    automatic=None,
):
    """Create a backup record for server tests."""
    record = {
        "ip_address": ip_address,
        "timestamp": timestamp,
        "generation": generation,
        "device_info": device_info
        if device_info is not None
        else {"gen": generation, "name": "Kitchen"},
        "settings": settings or {"device": {"name": "Kitchen"}},
    }
    if automatic is not None:
        record["automatic"] = automatic
    return record


def _create_setup_app(tmpdir):
    """Create a test app without any user accounts, as on first startup."""
    config = Configuration(None, tmpdir, None, interval=0)
    store = BackupStore(config.database_path())
    users = UserStore(config.users_path(), pbkdf2_iterations=1)
    return create_app(config, store=store, users=users), users


def _create_app(tmpdir, records=None):
    """Create a test app with an optional set of seeded records."""
    config = Configuration(None, tmpdir, None, interval=0)
    store = BackupStore(config.database_path())
    for record in records or []:
        store.add(record)
    users = UserStore(config.users_path(), pbkdf2_iterations=1)
    users.create_user(_ADMIN_EMAIL, _ADMIN_PASSWORD, UserRole.ADMIN)
    return create_app(config, store=store, users=users), store


@contextmanager
def _client(app):
    """Yield a test client that is signed in as the seeded admin user."""
    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client


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


class TestDeviceIndicators:
    """Tests for automatic backup and recency indicators."""

    def test_recent_backup_cutoff_uses_interval(self):
        """Test that the recent window follows the automatic backup interval."""
        scheduler = Mock()
        scheduler.enabled = True
        scheduler.interval = 3600
        now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

        assert _recent_backup_cutoff(scheduler, now) == now - timedelta(
            hours=1, minutes=5
        )

    def test_recent_backup_cutoff_without_automatic_backups(self):
        """Test the fallback recent window when automatic backups are off."""
        scheduler = Mock()
        scheduler.enabled = False
        scheduler.interval = 3600
        now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

        assert _recent_backup_cutoff(scheduler, now) == now - timedelta(hours=24)


class TestServer:
    """Tests for the web server routes."""

    def test_index_without_backups(self):
        """Test the overview page without any backups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert "Kitchen" in response.text
        assert "SHSW-1" in response.text
        assert "192.168.1.10" in response.text
        assert "192.168.1.11" in response.text
        assert "View backups" in response.text

    def test_page_footer_shows_trademark_disclaimer(self):
        """Test every page ends with the trademark disclaimer, not the DB path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                response = client.get("/")

        text = " ".join(response.text.split())
        assert "not associated with or endorsed by Shelly Europe Ltd" in text
        assert "retentacle_backups.json" not in text

    def test_login_page_shows_disclaimer(self):
        """Test the disclaimer is visible before signing in."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/login")

        text = " ".join(response.text.split())
        assert "Shelly is a trademark of its respective owner" in text

    def test_index_shows_device_backup_indicators(self):
        """Test the overview shows automatic and recent backup indicators."""
        now = datetime.now(UTC)
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    _record(
                        ip_address="192.168.1.10",
                        timestamp=(now - timedelta(minutes=5)).isoformat(),
                    ),
                    _record(
                        ip_address="192.168.1.11",
                        timestamp=(now - timedelta(days=2)).isoformat(),
                    ),
                    _record(
                        ip_address="192.168.1.12",
                        timestamp="not-a-timestamp",
                    ),
                ],
            )
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert 'title="Included in automatic backups">Automatic</span>' in response.text
        assert (
            'title="Not included in automatic backups">Manual</span>' in response.text
        )
        assert 'title="A backup was taken recently">Recent</span>' in response.text
        assert 'title="No recent backup">Stale</span>' in response.text
        assert 'title="The latest backup timestamp is invalid">Unknown</span>' in (
            response.text
        )

    def test_index_links_to_device_web_ui(self):
        """Test the overview opens each device web UI in a new tab."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert 'href="http://192.168.1.10"' in response.text
        assert 'target="_blank"' in response.text
        assert 'rel="noopener noreferrer"' in response.text
        assert "Open web UI" in response.text

    def test_index_links_to_ipv6_device_web_ui(self):
        """Test IPv6 device web UI links use brackets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[_record(ip_address="2001:db8::1")],
            )
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert 'href="http://[2001:db8::1]"' in response.text

    def test_index_shows_status_and_backup_started_notice(self):
        """Test the overview page shows scheduler status and the notice."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.get("/?backup_started=1")

        assert response.status_code == 200
        assert "Backup started" in response.text
        assert "Run backup now" in response.text
        assert "Backup interval" in response.text
        assert "Last automatic backup" in response.text
        assert "Next run" in response.text
        assert "Automatic backups off" in response.text
        assert "No backups yet" in response.text

    def test_index_shows_last_error(self):
        """Test the overview page shows the last scheduler error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.scheduler.last_error = "Device unreachable"
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert _format_timestamp("2026-11-15T10:00:00+00:00") in response.text
        assert (
            _format_timestamp(datetime(2026, 11, 16, 12, 0, tzinfo=UTC))
            not in response.text
        )

    def test_index_last_backup_ignores_manual_records(self):
        """Test the last backup card only counts automatic runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(
                tmpdir,
                records=[
                    _record(timestamp="2026-11-15T10:00:00+00:00", automatic=True),
                    _record(
                        ip_address="192.168.1.11",
                        timestamp="2026-11-16T10:00:00+00:00",
                        automatic=False,
                    ),
                ],
            )
            with _client(app) as client:
                text = client.get("/").text

        card = text.split("Last automatic backup</p>")[1].split("stat-label")[0]
        assert _format_timestamp(datetime(2026, 11, 15, 10, 0, tzinfo=UTC)) in card
        assert _format_timestamp(datetime(2026, 11, 16, 10, 0, tzinfo=UTC)) not in card

    def test_index_shows_never_without_backups(self):
        """Test that the overview shows no last backup without records."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.scheduler.last_run = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert "Last automatic backup" in response.text
        assert "Never" in response.text
        assert (
            _format_timestamp(datetime(2026, 11, 16, 12, 0, tzinfo=UTC))
            not in response.text
        )

    def test_index_has_progress_bar_and_live_updates(self):
        """Test the page includes the progress bar and status polling."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.get("/devices/192.168.1.10")

        assert response.status_code == 200
        assert "SNSW-001P16EU" in response.text

    def test_device_page_has_manual_backup_button(self):
        """Test the device page offers a manual backup action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                response = client.get("/devices/192.168.1.10")

        assert response.status_code == 200
        assert 'action="/devices/192.168.1.10/backup"' in response.text
        assert "Back up this device" in response.text
        assert "data-backup-progress" in response.text
        # the global "Run backup now" button moved to the dashboard header
        assert 'action="/backup?redirect=/devices' not in response.text

    def test_device_page_links_to_web_ui(self):
        """Test the device page opens its web UI in a new tab."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                response = client.get("/devices/192.168.1.10")

        assert response.status_code == 200
        assert 'href="http://192.168.1.10"' in response.text
        assert "Open web UI" in response.text
        assert 'target="_blank"' in response.text
        assert 'rel="noopener noreferrer"' in response.text

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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
                done = client.get("/devices/192.168.1.10?backup_done=1")
                failed = client.get("/devices/192.168.1.10?backup_failed=1")

        assert "Backup completed for Kitchen" in done.text
        assert "Backup failed" in failed.text

    def test_overview_has_manual_backup_button(self):
        """Test the overview offers a backup action for each device."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert 'action="/devices/192.168.1.10/backup?redirect=/"' in response.text
        assert 'aria-label="Back up Kitchen now"' in response.text
        assert '<use href="#icon-backup"/>' in response.text

    def test_manual_device_backup_returns_to_overview(self):
        """Test a per-device backup can return to the overview."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_device = AsyncMock(return_value=True)
                response = client.post(
                    "/devices/192.168.1.10/backup?redirect=/",
                    follow_redirects=False,
                )

                scheduler.run_device.assert_awaited_once_with("192.168.1.10")

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_done=1"

    def test_manual_device_backup_failure_returns_to_overview(self):
        """Test a failed overview backup redirects with an error flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_device = AsyncMock(return_value=False)
                response = client.post(
                    "/devices/192.168.1.10/backup?redirect=/",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_failed=1"

    def test_manual_device_backup_rejects_unsafe_redirect(self):
        """Test that the device backup redirect cannot leave the application."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_device = AsyncMock(return_value=True)
                response = client.post(
                    "/devices/192.168.1.10/backup?redirect=https://example.com",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/devices/192.168.1.10?backup_done=1"

    def test_manual_device_backup_rejects_unknown_page(self):
        """Test that a device backup only redirects to known pages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_device = AsyncMock(return_value=True)
                response = client.post(
                    "/devices/192.168.1.10/backup?redirect=/admin",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/devices/192.168.1.10?backup_done=1"

    def test_overview_shows_manual_backup_result(self):
        """Test the overview shows manual backup results."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                done = client.get("/?backup_done=1")
                failed = client.get("/?backup_failed=1")

        assert "Backup completed" in done.text
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
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.get("/devices/192.168.1.99")

        assert response.status_code == 404

    def test_backup_page(self):
        """Test the single backup page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                response = client.get(f"/backups/{doc_id}")

        assert response.status_code == 200
        assert "Kitchen" in response.text
        assert "settings" in response.text
        assert "Download full snapshot" in response.text
        assert "Raw backup data" in response.text
        assert "Backup ID" in response.text
        assert f"#{doc_id}" in response.text

    def test_backup_page_unknown_backup(self):
        """Test the backup page for an unknown backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.get("/backups/999")

        assert response.status_code == 404

    def test_download_backup(self):
        """Test downloading a backup as JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            record = store.all()[0]
            with _client(app) as client:
                response = client.get(f"/backups/{record['doc_id']}/download")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "retentacle-192.168.1.10-" in response.headers["content-disposition"]
        assert response.json() == record

    def test_download_unknown_backup(self):
        """Test downloading an unknown backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.get("/backups/999/download")

        assert response.status_code == 404

    def test_trigger_backup(self):
        """Test triggering a backup run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                client.post("/backup", follow_redirects=False)

                assert scheduler.queued is True
                assert scheduler.active is True

    def test_trigger_backup_returns_to_current_page(self):
        """Test that triggering a backup keeps the user on the current page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    "/backup?redirect=/devices/192.168.1.10",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/devices/192.168.1.10?backup_started=1"

    def test_trigger_backup_returns_to_settings(self):
        """Test that triggering a backup can return to the settings page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    "/backup?redirect=/settings",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/settings?backup_started=1"

    def test_trigger_backup_rejects_unknown_page(self):
        """Test that triggering a backup only redirects to known pages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post(
                    "/backup?redirect=/admin",
                    follow_redirects=False,
                )

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_started=1"

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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["database"].endswith("retentacle_backups.json")
        assert data["devices"] == 1
        assert data["last_run"] is None

    def test_index_links_to_settings(self):
        """Test that the overview page links to the settings page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.get("/")

        assert response.status_code == 200
        assert 'href="/settings"' in response.text

    def test_settings_page(self):
        """Test the settings page shows the current schedule controls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.get("/settings?saved=1")

        assert response.status_code == 200
        assert "Settings saved" in response.text

    def test_settings_page_shows_retention_section(self):
        """Test that the settings page shows the default retention policy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert 'action="/settings/devices"' in response.text
        assert 'name="ip_addresses"' in response.text
        assert "Add devices" in response.text
        assert "No devices added yet" in response.text

    def test_settings_page_can_clear_discovery_selection(self):
        """Test the streamed discovery results offer a select none action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert 'action="/settings/devices"' in response.text
        assert (
            '<button class="button" type="button" data-select-none>Select none</button>'
            in response.text
        )
        assert "querySelectorAll('[data-select-none]')" in response.text

    def test_settings_page_lists_saved_devices(self):
        """Test that saved devices are listed with a remove action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10", "2001:db8::1"]})
            with _client(app) as client:
                response = client.get("/settings")

        assert response.status_code == 200
        assert "192.168.1.10" in response.text
        assert "2001:db8::1" in response.text
        assert 'action="/settings/devices/remove"' in response.text

    def test_add_devices(self):
        """Test adding device IP addresses via the settings page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
                    "retentacle.server.discover_shelly_device_details",
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
                _client(app) as client,
            ):
                response = client.post("/settings/discover")

        assert response.status_code == 200
        assert "New devices found" in response.text
        assert response.text.count('class="checkbox-cell"') == 2
        assert 'value="192.168.1.11"' in response.text
        assert 'value="2001:db8::1"' in response.text
        assert "ShellyPlus1PM-ABC" in response.text
        assert "Plus1PM · gen 2 · 1.4.4" in response.text
        assert (
            response.text.count(
                '<button class="button" type="button" data-select-none>'
            )
            == 2
        )
        assert "data-discovery-empty hidden>No new devices found." not in response.text

    def test_discover_devices_reports_when_nothing_is_new(self):
        """Test that discovery reports when all found devices are configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with (
                patch(
                    "retentacle.server.discover_shelly_device_details",
                    return_value=[DiscoveredDevice("192.168.1.10")],
                ),
                _client(app) as client,
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
                    "retentacle.server.discover_shelly_device_details",
                    side_effect=RuntimeError("discovery failed"),
                ),
                _client(app) as client,
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
                    "retentacle.server.discover_shelly_device_details",
                    return_value=[
                        DiscoveredDevice("192.168.1.11"),
                        DiscoveredDevice("192.168.1.11"),
                        DiscoveredDevice("2001:0db8::0001"),
                        DiscoveredDevice("not-an-ip"),
                    ],
                ),
                _client(app) as client,
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
                    "retentacle.server.discover_shelly_device_details",
                    return_value=[
                        DiscoveredDevice("192.168.1.20"),
                        DiscoveredDevice("2001:db8::1"),
                        DiscoveredDevice("192.168.1.3"),
                    ],
                ),
                _client(app) as client,
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
                    "retentacle.server.iter_shelly_device_details",
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
                _client(app) as client,
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
                    "retentacle.server.iter_shelly_device_details",
                    side_effect=RuntimeError("discovery failed"),
                ),
                _client(app) as client,
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
            with _client(app) as client:
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
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post("/backup", follow_redirects=False)

                scheduler.run_once.assert_awaited_once_with(["10.0.0.1"])

        assert response.status_code == 303

    def test_save_retention(self):
        """Test saving a backup retention policy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
                response = client.post("/settings", data={}, follow_redirects=False)

                assert response.status_code == 400
                assert store.get_settings() == {}

    def test_save_settings_requires_next_run_when_enabled(self):
        """Test that enabling automatic backups requires a next run time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
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
            with _client(app) as client:
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
            with _client(app) as client:
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

    @patch("retentacle.server.uvicorn.run")
    def test_run_server(self, mock_run):
        """Test running the server with uvicorn."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, interval=0)
            run_server(config)

        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8080


def _login(client, email, password):
    """Sign a test client in and return the redirect response."""
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


class TestAuthentication:
    """Tests for the first-run setup, login, and session handling."""

    def test_first_start_redirects_to_setup(self):
        """Test that all pages redirect to setup while no user exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/", follow_redirects=False)

                assert response.status_code == 303
                assert response.headers["location"] == "/setup"
                assert client.get("/settings").status_code == 200
                assert "Create admin account" in client.get("/settings").text

    def test_health_is_public_during_setup(self):
        """Test that the health endpoint never requires authentication."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/health")

                assert response.status_code == 200
                assert response.json()["status"] == "ok"

    def test_setup_page_renders(self):
        """Test that the setup page offers the admin account form."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/setup")

                assert response.status_code == 200
                assert "Create admin account" in response.text
                assert 'name="confirm_password"' in response.text

    def test_setup_creates_admin_and_signs_in(self):
        """Test that completing setup creates the admin and starts a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, users = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/setup",
                    data={
                        "email": "admin@example.com",
                        "password": "unit-test-login-pass-1",
                        "confirm_password": "unit-test-login-pass-1",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/"
                assert "retentacle_session=" in response.headers["set-cookie"]
                created = users.list_users()[0]
                assert created.email == "admin@example.com"
                assert created.role is UserRole.ADMIN
                assert client.get("/").status_code == 200
                assert "Sign out" in client.get("/").text

    def test_setup_rejects_mismatched_passwords(self):
        """Test that setup asks for matching passwords."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, users = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/setup",
                    data={
                        "email": "admin@example.com",
                        "password": "unit-test-login-pass-1",
                        "confirm_password": "different-pass",
                    },
                )

                assert response.status_code == 400
                assert "do not match" in response.text
                assert users.has_users() is False

    def test_setup_rejects_invalid_account_data(self):
        """Test that setup validates email and password."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, users = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/setup",
                    data={
                        "email": "admin@example.com",
                        "password": "short",
                        "confirm_password": "short",
                    },
                )

                assert response.status_code == 400
                assert "at least 12 characters" in response.text
                assert users.has_users() is False

    def test_setup_redirects_once_configured(self):
        """Test that the setup page is closed once a user exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/setup", follow_redirects=False)

                assert response.status_code == 303
                assert response.headers["location"] == "/login"

    def test_logo_is_public_and_linked(self):
        """Test the static logo loads without signing in and is referenced."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                asset = client.get("/static/retentacle-logo.svg")

                assert asset.status_code == 200
                assert asset.headers["content-type"].startswith("image/")
                text = client.get("/login").text
                assert 'href="/static/retentacle-logo.svg"' in text
                assert 'src="/static/retentacle-logo.svg"' in text

    def test_logo_public_during_setup(self):
        """Test the logo loads while the first-run wizard is active."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/static/retentacle-logo.svg")

        assert response.status_code == 200

    def test_setup_post_is_closed_once_a_user_exists(self):
        """Test that account setup cannot be used after onboarding."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/setup",
                    data={
                        "email": "second@example.com",
                        "password": "another-password-1",
                        "confirm_password": "another-password-1",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/login"

    def test_unauthenticated_requests_redirect_to_login(self):
        """Test that protected pages redirect signed-out users to login."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with TestClient(app) as client:
                response = client.get("/", follow_redirects=False)

                assert response.status_code == 303
                assert response.headers["location"] == "/login"
                assert "Sign in" in client.get("/").text

    def test_login_requires_valid_credentials(self):
        """Test that wrong credentials are rejected with a message."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/login",
                    data={"email": _ADMIN_EMAIL, "password": "wrong-password"},
                )

                assert response.status_code == 401
                assert "Invalid email or password" in response.text
                response = _login(client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
                assert response.status_code == 303

    def test_login_page_redirects_signed_in_users(self):
        """Test that the login page is skipped for signed-in users."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.get("/login", follow_redirects=False)

                assert response.status_code == 303
                assert response.headers["location"] == "/"

    def test_logout_ends_the_session(self):
        """Test that signing out clears the session and cookie."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            with _client(app) as client:
                response = client.post("/logout", follow_redirects=False)

                assert response.status_code == 303
                assert response.headers["location"] == "/login"
                assert client.get("/", follow_redirects=False).status_code == 303
                assert len(users.list_users()) == 1

    def test_invalid_and_expired_session_cookies_are_rejected(self):
        """Test that unknown session tokens do not grant access."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                client.cookies.set("retentacle_session", "made-up-token")
                response = client.get("/", follow_redirects=False)

                assert response.status_code == 303
                assert response.headers["location"] == "/login"

    def test_viewer_can_browse_and_download_but_not_act(self):
        """Test the permissions of a read-only account."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            doc_id = store.all()[0]["doc_id"]
            client = TestClient(app)
            assert (
                _login(client, "guest@example.com", "viewer-password-1").status_code
                == 303
            )

            assert client.get("/").status_code == 200
            assert client.get("/devices/192.168.1.10").status_code == 200
            assert client.get(f"/backups/{doc_id}").status_code == 200
            assert client.get(f"/backups/{doc_id}/download").status_code == 200
            assert client.get("/api/status").status_code == 200

            assert client.get("/settings").status_code == 403
            assert client.get("/users").status_code == 403
            assert client.post("/backup", data={}).status_code == 403
            assert (
                client.post("/devices/192.168.1.10/backup", data={}).status_code == 403
            )
            assert client.post("/settings", data={}).status_code == 403
            assert client.post("/settings/retention", data={}).status_code == 403
            assert client.post("/settings/devices", data={}).status_code == 403
            assert client.post("/settings/devices/remove", data={}).status_code == 403
            assert client.post("/settings/discover", data={}).status_code == 403

    def test_signed_in_user_page_hides_admin_actions_for_viewers(self):
        """Test that read-only users do not see settings or backup buttons."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            client = TestClient(app)
            _login(client, "guest@example.com", "viewer-password-1")

            text = client.get("/").text

            assert "Run backup now" not in text
            assert "/settings" not in text
            assert "/users" not in text
            assert "Back up Kitchen now" not in text
            assert "Sign out" in text
            assert "guest" in text

    def test_admin_page_shows_navigation_for_admins(self):
        """Test that admins see the backup button and account menu entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                text = client.get("/").text

                assert "Run backup now" in text
                assert 'class="avatar-menu-item" href="/settings"' in text
                assert ">Settings</a>" not in text
                # the run button now lives on the dashboard, not the top bar
                assert text.count("Run backup now") == 1
                assert 'href="/settings#devices"' in text
                assert "Add devices" in text
                redirect = client.get("/users", follow_redirects=False)

                assert redirect.status_code == 303
                assert redirect.headers["location"] == "/settings"

    def test_settings_devices_section_has_anchor(self):
        """Test the settings page can be deep-linked at the devices card."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                text = client.get("/settings").text

        assert 'id="devices"' in text


class TestUserManagement:
    """Tests for the user management section on the settings page."""

    def _viewer_client(self, tmpdir):
        """Create an app and a client signed in with a read-only account."""
        app, _ = _create_app(tmpdir)
        app.state.users.create_user(
            "guest@example.com", "viewer-password-1", UserRole.VIEWER
        )
        client = TestClient(app)
        _login(client, "guest@example.com", "viewer-password-1")
        return app, client

    def test_users_page_lists_accounts(self):
        """Test that the settings page shows all accounts and roles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            with _client(app) as client:
                response = client.get("/users")

                assert response.status_code == 200
                assert "admin" in response.text
                assert "guest" in response.text
                assert "Administrator" in response.text
                assert "Read-only" in response.text
                assert ">You</span>" in response.text
                assert "Add user" in response.text

    def test_create_user(self):
        """Test that admins can create accounts with both roles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            with _client(app) as client:
                response = client.post(
                    "/settings/users",
                    data={
                        "email": "operator@example.com",
                        "password": "operator-password-1",
                        "role": "admin",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/settings?saved=1#users"
                created = users.verify_credentials(
                    "operator@example.com", "operator-password-1"
                )
                assert created is not None
                assert created.role is UserRole.ADMIN

    def test_create_user_rejects_duplicates(self):
        """Test that duplicate emails are reported on the settings page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/users",
                    data={
                        "email": "Admin@Example.Com",
                        "password": "another-password-1",
                        "role": "viewer",
                    },
                )

                assert response.status_code == 400
                assert "already in use" in response.text

    def test_create_user_rejects_invalid_data(self):
        """Test that invalid new accounts are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            with _client(app) as client:
                response = client.post(
                    "/settings/users",
                    data={
                        "email": "guest@example.com",
                        "password": "short",
                        "role": "viewer",
                    },
                )

                assert response.status_code == 400
                assert "at least 12 characters" in response.text
                response = client.post(
                    "/settings/users",
                    data={
                        "email": "guest@example.com",
                        "password": "all-letters-and-symbols",
                        "role": "viewer",
                    },
                )

                assert response.status_code == 400
                assert "at least one digit" in response.text
                response = client.post(
                    "/settings/users",
                    data={
                        "email": "guest@example.com",
                        "password": "lettersanddigits1234",
                        "role": "viewer",
                    },
                )

                assert response.status_code == 400
                assert "special character" in response.text
                response = client.post(
                    "/settings/users",
                    data={
                        "email": "guest@example.com",
                        "password": "viewer-password-1",
                        "role": "wizard",
                    },
                )

                assert response.status_code == 400
                assert "Invalid role" in response.text
                assert [user.email for user in users.list_users()] == [
                    "admin@example.com"
                ]

    def test_change_user_role(self):
        """Test that admins can change the role of a user."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            guest = users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            with _client(app) as client:
                response = client.post(
                    f"/settings/users/{guest.user_id}/role",
                    data={"role": "admin"},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert users.get_user(guest.user_id).role is UserRole.ADMIN

    def test_change_role_rejects_demoting_last_admin(self):
        """Test that the last admin cannot be demoted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            admin = users.list_users()[0]
            with _client(app) as client:
                response = client.post(
                    f"/settings/users/{admin.user_id}/role",
                    data={"role": "viewer"},
                )

                assert response.status_code == 400
                assert "last admin" in response.text
                assert users.get_user(admin.user_id).role is UserRole.ADMIN

    def test_reset_password_of_other_user(self):
        """Test that admins can set a new password for other users."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            guest = users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            token = users.create_session(guest.user_id)
            with _client(app) as client:
                response = client.post(
                    f"/settings/users/{guest.user_id}/password",
                    data={"password": "fresh-password-1"},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert (
                    users.verify_credentials("guest@example.com", "fresh-password-1")
                    is not None
                )
                assert users.resolve_session(token) is None

    def test_reset_own_password_keeps_session_alive(self):
        """Test that changing your own password keeps you signed in."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            admin = users.list_users()[0]
            with _client(app) as client:
                old_token = client.cookies.get("retentacle_session")
                response = client.post(
                    f"/settings/users/{admin.user_id}/password",
                    data={"password": "unit-test-login-pass-1"},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert client.get("/users").status_code == 200
                assert users.resolve_session(old_token) is None

    def test_reset_password_rejects_invalid(self):
        """Test that password rules also apply on the users page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            guest = users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            with _client(app) as client:
                response = client.post(
                    f"/settings/users/{guest.user_id}/password",
                    data={"password": "short"},
                )

                assert response.status_code == 400
                assert "at least 12 characters" in response.text

    def test_delete_user(self):
        """Test that admins can delete other accounts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            guest = users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            with _client(app) as client:
                response = client.post(
                    f"/settings/users/{guest.user_id}/delete",
                    data={},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/settings?saved=1#users"
                assert users.get_user(guest.user_id) is None

    def test_delete_rejects_own_and_last_admin_account(self):
        """Test that the acting admin account cannot be deleted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            admin = users.list_users()[0]
            with _client(app) as client:
                response = client.post(
                    f"/settings/users/{admin.user_id}/delete", data={}
                )

                assert response.status_code == 400
                assert "own account" in response.text
                assert users.get_user(admin.user_id) is not None

    def test_delete_rejects_unknown_user(self):
        """Test that deleting an unknown account fails gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post("/settings/users/999/delete", data={})

                assert response.status_code == 400
                assert "not found" in response.text

    def test_user_routes_reject_non_admins(self):
        """Test that read-only users cannot manage accounts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, client = self._viewer_client(tmpdir)
            guest = app.state.users.verify_credentials(
                "guest@example.com", "viewer-password-1"
            )
            assert guest is not None

            assert client.get("/users").status_code == 403
            assert (
                client.post(
                    "/settings/users",
                    data={
                        "email": "x@example.com",
                        "password": "password-1234",
                        "role": "viewer",
                    },
                ).status_code
                == 403
            )
            assert (
                client.post(
                    f"/settings/users/{guest.user_id}/role", data={}
                ).status_code
                == 403
            )
            assert (
                client.post(
                    f"/settings/users/{guest.user_id}/delete", data={}
                ).status_code
                == 403
            )
            assert (
                client.post(
                    f"/settings/users/{guest.user_id}/password", data={}
                ).status_code
                == 403
            )


_OIDC_ENDPOINTS = {
    "authorization_endpoint": "https://id.example.com/auth",
    "token_endpoint": "https://id.example.com/token",
    "userinfo_endpoint": "https://id.example.com/userinfo",
}

_OIDC_SECRET = "test-client-secret"


def _seed_oidc(store, **overrides):
    """Store OIDC settings for tests and return the effective values."""
    oidc = {
        "enabled": True,
        "issuer": "https://id.example.com",
        "client_id": "retentacle",
        "client_secret": _OIDC_SECRET,
        "callback_url": "",
        "scopes": "openid email profile",
        "provider_name": "",
        "auto_create_users": False,
        "disable_password_login": False,
    }
    oidc.update(overrides)
    settings = store.get_settings()
    settings["oidc"] = dict(oidc)
    store.save_settings(settings)
    return oidc


def _jwt(claims):
    """Build an unsigned three-part JWT carrying the given payload claims."""
    segment = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return f"header.{segment.rstrip('=')}.signature"


def _oidc_login_state(client):
    """Run the OIDC login start and return the state from the provider URL."""
    response = client.get("/oidc/login", follow_redirects=False)

    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return response.headers["location"], query["state"][0]


class TestOIDCSettings:
    """Tests for the OIDC configuration on the settings page."""

    def test_settings_page_shows_oidc_section(self):
        """Test that the settings page offers the single sign-on form."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.get("/settings")

                assert response.status_code == 200
                assert 'name="oidc_issuer"' in response.text
                assert "Enable OpenID Connect sign-in" in response.text
                assert 'id="oidc_auto_create_users"' in response.text
                assert 'id="oidc_disable_password_login"' in response.text

    def test_save_oidc_settings(self):
        """Test that valid OIDC settings are persisted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "https://id.example.com/realms/main/",
                        "oidc_client_id": "retentacle",
                        "oidc_client_secret": _OIDC_SECRET,
                        "oidc_auto_create_users": "on",
                        "oidc_provider_name": "Acme ID",
                        "oidc_callback_url": "https://backup.example.com/oidc/callback",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/settings?saved=1#oidc"
                oidc = store.get_settings()["oidc"]
                assert oidc["enabled"] is True
                assert oidc["issuer"] == "https://id.example.com/realms/main"
                assert oidc["auto_create_users"] is True
                assert oidc["disable_password_login"] is False
                assert oidc["scopes"] == "openid email profile"
                assert oidc["provider_name"] == "Acme ID"
                assert (
                    oidc["callback_url"] == "https://backup.example.com/oidc/callback"
                )

    def test_save_oidc_keeps_stored_secret(self):
        """Test that a blank secret field keeps the stored one."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with _client(app) as client:
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "https://id.example.com",
                        "oidc_client_id": "retentacle",
                        "oidc_client_secret": "",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert store.get_settings()["oidc"]["client_secret"] == _OIDC_SECRET

    def test_save_oidc_rejects_invalid_issuer(self):
        """Test that the issuer must be an absolute URL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "not-a-url",
                        "oidc_client_id": "retentacle",
                        "oidc_client_secret": _OIDC_SECRET,
                    },
                )

                assert response.status_code == 400
                assert "issuer URL" in response.text
                assert "oidc" not in store.get_settings()

    def test_save_oidc_requires_client_id(self):
        """Test that a client ID is needed when enabling OIDC."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "https://id.example.com",
                        "oidc_client_id": "",
                        "oidc_client_secret": _OIDC_SECRET,
                    },
                )

                assert response.status_code == 400
                assert "client ID" in response.text

    def test_save_oidc_rejects_invalid_callback_url(self):
        """Test that the optional callback URL must be an absolute URL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "https://id.example.com",
                        "oidc_client_id": "retentacle",
                        "oidc_client_secret": _OIDC_SECRET,
                        "oidc_callback_url": "id.example.com/oidc/callback",
                    },
                )

                assert response.status_code == 400
                assert "callback URL" in response.text

    def test_disable_password_requires_secret(self):
        """Test that disabling password sign-in needs a client secret."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "https://id.example.com",
                        "oidc_client_id": "retentacle",
                        "oidc_client_secret": "",
                        "oidc_disable_password_login": "on",
                    },
                )

                assert response.status_code == 400
                assert "client secret" in response.text
                assert "oidc" not in store.get_settings()

    def test_disable_password_verifies_provider(self):
        """Test that an unreachable provider blocks disabling passwords."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.server.discover_endpoints",
                    side_effect=OIDCError("could not connect"),
                ),
                _client(app) as client,
            ):
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "https://id.example.com",
                        "oidc_client_id": "retentacle",
                        "oidc_client_secret": "",
                        "oidc_disable_password_login": "on",
                    },
                )

                assert response.status_code == 400
                assert "could not be verified" in response.text
                assert store.get_settings()["oidc"]["disable_password_login"] is False

    def test_disable_password_saves_when_provider_reachable(self):
        """Test that a working provider allows disabling password sign-in."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.server.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                _client(app) as client,
            ):
                response = client.post(
                    "/settings/oidc",
                    data={
                        "oidc_enabled": "on",
                        "oidc_issuer": "https://id.example.com",
                        "oidc_client_id": "retentacle",
                        "oidc_disable_password_login": "on",
                    },
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert store.get_settings()["oidc"]["disable_password_login"] is True

    def test_disable_password_ignored_when_oidc_off(self):
        """Test that the password flag cannot be set while OIDC is off."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/oidc",
                    data={"oidc_disable_password_login": "on"},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                oidc = store.get_settings()["oidc"]
                assert oidc["enabled"] is False
                assert oidc["disable_password_login"] is False


class TestOIDCLogin:
    """Tests for the OpenID Connect login flow."""

    def test_login_page_offers_oidc_button(self):
        """Test that the login page shows both sign-in methods."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with TestClient(app) as client:
                response = client.get("/login")

                assert response.status_code == 200
                assert "Sign in with your account provider" in response.text
                assert 'action="/login"' in response.text

    def test_login_page_hides_password_form_when_disabled(self):
        """Test that the local form disappears with password login off."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store, disable_password_login=True)
            with TestClient(app) as client:
                response = client.get("/login")

                assert response.status_code == 200
                assert "Sign in with your account provider" in response.text
                assert 'action="/login"' not in response.text

    def test_local_login_rejected_when_password_disabled(self):
        """Test that the password route refuses requests when disabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store, disable_password_login=True)
            with TestClient(app) as client:
                response = client.post(
                    "/login",
                    data={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
                )

                assert response.status_code == 403
                assert "disabled" in response.text

    def test_oidc_login_requires_configuration(self):
        """Test that the OIDC entry point refuses unconfigured setups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/oidc/login", follow_redirects=False)

                assert response.status_code == 400
                assert "not configured" in response.text

    def test_setup_mode_blocks_oidc(self):
        """Test that OIDC waits until the admin account exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_setup_app(tmpdir)
            with TestClient(app) as client:
                response = client.get("/oidc/login", follow_redirects=False)

                assert response.status_code == 303
                assert response.headers["location"] == "/setup"

    def test_oidc_login_redirects_to_provider(self):
        """Test that the flow starts with a correct authorization request."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                TestClient(app) as client,
            ):
                response = client.get("/oidc/login", follow_redirects=False)

                assert response.status_code == 302
                location = response.headers["location"]
                assert location.startswith("https://id.example.com/auth?")
                query = parse_qs(urlsplit(location).query)
                assert query["response_type"] == ["code"]
                assert query["client_id"] == ["retentacle"]
                assert query["redirect_uri"] == ["http://testserver/oidc/callback"]
                assert query["scope"] == ["openid email profile"]
                assert query["state"]
                assert query["nonce"]

    def test_oidc_login_honours_callback_url(self):
        """Test that the configured callback URL becomes the redirect URI."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store, callback_url="https://backup.example.com/oidc/callback")
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                TestClient(app) as client,
            ):
                response = client.get("/oidc/login", follow_redirects=False)
                query = parse_qs(urlsplit(response.headers["location"]).query)

                assert query["redirect_uri"] == [
                    "https://backup.example.com/oidc/callback"
                ]

    def test_oidc_login_reports_discovery_errors(self):
        """Test that provider problems are shown on the login page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    side_effect=OIDCError("no discovery document"),
                ),
                TestClient(app) as client,
            ):
                response = client.get("/oidc/login", follow_redirects=False)

                assert response.status_code == 400
                assert "no discovery document" in response.text

    def test_login_button_shows_provider_name(self):
        """Test that the configured provider name labels the OIDC button."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store, provider_name="Acme ID")
            with TestClient(app) as client:
                response = client.get("/login")

                assert "Sign in with Acme ID" in response.text
                assert "your account provider" not in response.text

    def test_oidc_callback_signs_in_existing_user(self):
        """Test that a matching email starts a local session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "access-token"},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={
                        "email": "Admin@Example.com",
                        "email_verified": True,
                    },
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback",
                    params={"code": "auth-code", "state": state},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/"
                assert "retentacle_session=" in response.headers["set-cookie"]
                assert "Settings" in client.get("/").text

    def test_oidc_callback_rejects_unknown_state(self):
        """Test that a callback without a valid state is refused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with TestClient(app) as client:
                response = client.get(
                    "/oidc/callback",
                    params={"code": "auth-code", "state": "made-up"},
                )

                assert response.status_code == 400
                assert "invalid or expired" in response.text

    def test_oidc_callback_state_is_single_use(self):
        """Test that a state cannot be replayed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "access-token"},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={"email": _ADMIN_EMAIL, "email_verified": True},
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                first = client.get(
                    "/oidc/callback",
                    params={"code": "c", "state": state},
                    follow_redirects=False,
                )

                assert first.status_code == 303
                replay = client.get(
                    "/oidc/callback", params={"code": "c", "state": state}
                )

                assert replay.status_code == 400
                assert "invalid or expired" in replay.text

    def test_oidc_callback_surfaces_provider_error(self):
        """Test that provider-reported errors are shown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback",
                    params={"state": state, "error": "access_denied"},
                )

                assert response.status_code == 400
                assert "access_denied" in response.text

    def test_oidc_callback_handles_exchange_failure(self):
        """Test that token exchange failures are reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    side_effect=OIDCError("token endpoint returned HTTP 400."),
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback", params={"code": "c", "state": state}
                )

                assert response.status_code == 401
                assert "token endpoint" in response.text

    def test_oidc_callback_requires_email(self):
        """Test that profiles without an email are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "access-token"},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={"sub": "abc"},
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback", params={"code": "c", "state": state}
                )

                assert response.status_code == 401
                assert "did not include an email address" in response.text

    def test_oidc_callback_rejects_unverified_email(self):
        """Test that unverified email claims are refused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "access-token"},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={"email": _ADMIN_EMAIL, "email_verified": False},
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback", params={"code": "c", "state": state}
                )

                assert response.status_code == 401
                assert "not verified" in response.text

    def test_oidc_callback_rejects_unknown_user_without_auto_create(self):
        """Test that unknown emails are refused unless auto-create is on."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "access-token"},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={"email": "stranger@example.com"},
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback", params={"code": "c", "state": state}
                )

                assert response.status_code == 403
                assert "No local account exists for stranger@example.com" in (
                    response.text
                )
                assert len(app.state.users.list_users()) == 1

    def test_oidc_callback_creates_read_only_user(self):
        """Test that auto-create provisions new OIDC users as read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store, auto_create_users=True)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "access-token"},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={"email": "newcomer@example.com"},
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback",
                    params={"code": "c", "state": state},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert "retentacle_session=" in response.headers["set-cookie"]
                user = app.state.users.get_user_by_email("newcomer@example.com")
                assert user is not None
                assert user.role is UserRole.VIEWER
                assert (
                    app.state.users.verify_credentials(
                        "newcomer@example.com", "anything"
                    )
                    is None
                )


class TestUserMenu:
    """Tests for the avatar account menu in the header."""

    def test_admin_avatar_menu_contents(self):
        """Test the menu shows account, role, settings link and version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                text = client.get("/").text

        assert 'aria-label="Account menu"' in text
        assert ">A</span>" in text
        assert "admin@example.com" in text
        assert "Administrator" in text
        assert 'class="avatar-menu-item" href="/settings"' in text
        assert "Sign out" in text
        assert 'class="version-pill"' in text
        assert 'aria-label="Project on GitHub"' in text
        assert 'href="https://github.com/exxamalte/retentacle"' in text
        version = templates.env.globals["application_version"]
        assert f"Retentacle</strong> v{version}" in " ".join(text.split())

    def test_viewer_avatar_menu_hides_settings(self):
        """Test read-only accounts have no settings entry in their menu."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            client = TestClient(app)
            _login(client, "guest@example.com", "viewer-password-1")
            text = client.get("/").text

        assert ">G</span>" in text
        assert "Read-only" in text
        assert 'class="avatar-menu-item" href="/settings"' not in text

    def test_avatar_shows_provider_picture(self):
        """Test the OIDC picture claim replaces the letter fallback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            picture = "https://id.example.com/pic/alice.png"
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "access-token"},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={
                        "email": _ADMIN_EMAIL,
                        "email_verified": True,
                        "picture": picture,
                    },
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                client.get("/oidc/callback", params={"code": "c", "state": state})

                assert app.state.users.get_user_by_email(_ADMIN_EMAIL).picture == (
                    picture
                )
                text = client.get("/").text
                assert f'src="{picture}"' in text
                assert ">A</span>" not in text

    def test_avatar_falls_back_to_id_token_picture(self):
        """Test providers that only send picture in the ID token work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            id_token = _jwt(
                {
                    "email": _ADMIN_EMAIL,
                    "picture": "https://cdn.example/alice.png",
                }
            )
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "at", "id_token": id_token},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={"email": _ADMIN_EMAIL, "email_verified": True},
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                client.get("/oidc/callback", params={"code": "c", "state": state})

                assert app.state.users.get_user_by_email(_ADMIN_EMAIL).picture == (
                    "https://cdn.example/alice.png"
                )
                assert 'src="https://cdn.example/alice.png"' in client.get("/").text

    def test_avatar_ignores_id_token_for_other_accounts(self):
        """Test an ID token picture is dropped when the email differs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            id_token = _jwt(
                {
                    "email": "someone-else@example.com",
                    "picture": "https://cdn.example/other.png",
                }
            )
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                patch(
                    "retentacle.auth.exchange_code",
                    return_value={"access_token": "at", "id_token": id_token},
                ),
                patch(
                    "retentacle.auth.fetch_userinfo",
                    return_value={"email": _ADMIN_EMAIL, "email_verified": True},
                ),
                TestClient(app) as client,
            ):
                _, state = _oidc_login_state(client)
                response = client.get(
                    "/oidc/callback",
                    params={"code": "c", "state": state},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert app.state.users.get_user_by_email(_ADMIN_EMAIL).picture is None


class TestRecoveryView:
    """Tests for the user-friendly backup and restore view."""

    def _gen2_record(self):
        return {
            "ip_address": "192.168.10.20",
            "timestamp": "2026-11-15T10:00:00+00:00",
            "generation": 2,
            "device_info": {
                "gen": 2,
                "model": "Plus1PM",
                "mac": "AC:12:3F:11:22:33",
                "fw": "20260101-120000/v1",
            },
            "settings": {
                "configuration": {
                    "sys": {"device": {"name": "Kitchen"}},
                    "wifi": {"sta": {"ssid": "HomeNet", "psk": "super-wifi-pass"}},
                    "mqtt": {"enable": True, "password": "super-mqtt-pass"},
                },
                "device_info": {"mac": "AC:12:3F:11:22:33", "uptime": 400},
                "status": {"switch:0": {"output": True, "power": 12.4}},
            },
        }

    def test_backup_page_shows_restore_sections(self):
        """Test the backup page groups config, masks secrets and flags state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[self._gen2_record()])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                text = client.get(f"/backups/{doc_id}").text

        assert "Manual restore" in text
        assert "Shelly.SetConfig" in text
        assert "restore-192.168.10.20-" in text
        assert "Copy restore JSON" in text
        assert 'id="restore-payload"' in text
        assert "Wi-Fi" in text
        summary = text.split('aria-label="Configuration summary"')[1].split(
            "</section>"
        )[0]
        assert "super-wifi-pass" not in summary
        assert "super-mqtt-pass" not in summary
        assert "••••••••" in summary
        assert "never restored" in text

    def test_backup_page_shows_identity_info(self):
        """Test model, MAC and firmware are surfaced as information only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[self._gen2_record()])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                text = client.get(f"/backups/{doc_id}").text

        assert "Plus1PM" in text
        assert "AC:12:3F:11:22:33" in text
        assert "a replacement device" in text

    def test_restore_json_download(self):
        """Test the restore download contains exactly the configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[self._gen2_record()])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                response = client.get(f"/backups/{doc_id}/restore.json")

        assert response.status_code == 200
        assert response.json() == self._gen2_record()["settings"]["configuration"]
        assert (
            f'filename="restore-192.168.10.20-{doc_id}.json"'
            in (response.headers["content-disposition"])
        )

    def test_restore_json_missing_for_unrecognized_backup(self):
        """Test records without a config payload have no restore download."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                response = client.get(f"/backups/{doc_id}/restore.json")

                assert response.status_code == 404

    def test_backup_page_handles_unrecognized_backup(self):
        """Test the restore view degrades gracefully for odd records."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                text = client.get(f"/backups/{doc_id}").text

        assert "does not contain a recognizable configuration payload" in text

    def test_gen1_restore_command(self):
        """Test generation 1 backups use the settings endpoint."""
        record = {
            "ip_address": "192.168.10.11",
            "timestamp": "2026-11-15T10:00:00+00:00",
            "generation": 1,
            "device_info": {"type": "SHSW-1", "mac": "ABC123"},
            "settings": {
                "settings": {"device": {"name": "Office"}},
                "actions": [{"id": 0}],
                "status": {"input": [0]},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[record])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                page = client.get(f"/backups/{doc_id}").text
                download = client.get(f"/backups/{doc_id}/restore.json")

        assert "curl -sS -X POST &#34;http://192.168.10.11/settings&#34;" in page
        assert download.json() == {"device": {"name": "Office"}}

    def test_viewer_can_download_restore_config(self):
        """Test read-only accounts may download the restore payload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[self._gen2_record()])
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            doc_id = store.all()[0]["doc_id"]
            client = TestClient(app)
            _login(client, "guest@example.com", "viewer-password-1")

            assert client.get(f"/backups/{doc_id}/restore.json").status_code == 200

    def test_device_page_shows_chips_and_restore_latest(self):
        """Test the history page summarizes configs and links to a restore."""
        older = self._gen2_record()
        older["timestamp"] = "2026-11-14T10:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir, records=[self._gen2_record(), older, _record()]
            )
            doc_ids = [r["doc_id"] for r in store.all()]
            with _client(app) as client:
                text = client.get("/devices/192.168.10.20").text

        assert f'href="/backups/{doc_ids[0]}#manual-restore"' in text
        assert "Restore latest" in text
        assert "Wi-Fi: HomeNet" in text
        assert "MQTT" in text

    def test_device_page_collapses_unchanged_backups(self):
        """Test identical configurations collapse to one chip set."""
        first = self._gen2_record()
        second = self._gen2_record()
        second["timestamp"] = "2026-11-14T10:00:00+00:00"
        changed = self._gen2_record()
        changed["timestamp"] = "2026-11-13T10:00:00+00:00"
        changed["settings"]["configuration"]["mqtt"]["enable"] = False
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[first, second, changed])
            with _client(app) as client:
                text = client.get("/devices/192.168.10.20").text

        assert text.count("Wi-Fi: HomeNet") == 2
        assert text.count(">unchanged<") == 1

    def test_device_page_marks_unrecognizable_backups(self):
        """Test records without a config payload show a neutral dash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                text = client.get("/devices/192.168.1.10").text

        assert ">unchanged<" not in text
        assert "config-summary-cell" in text

    def test_backup_page_restore_card_anchor(self):
        """Test the restore card can be linked to directly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[self._gen2_record()])
            doc_id = store.all()[0]["doc_id"]
            with _client(app) as client:
                text = client.get(f"/backups/{doc_id}").text

        assert 'id="manual-restore"' in text


class TestConfigCompare:
    """Tests for the backup configuration comparison view."""

    def _record(self, timestamp, mqtt_enable=True):
        return {
            "ip_address": "192.168.10.20",
            "timestamp": timestamp,
            "generation": 2,
            "device_info": {"gen": 2, "name": "Kitchen"},
            "settings": {
                "configuration": {
                    "sys": {"device": {"name": "Kitchen"}},
                    "wifi": {"sta": {"ssid": "HomeNet", "psk": "hidden-pass"}},
                    "mqtt": {"enable": mqtt_enable},
                },
                "status": {"sys": {"uptime": 1}},
            },
        }

    def test_compare_defaults_to_latest_pair(self):
        """Test the default view diffs the newest two backups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    self._record("2026-11-20T10:00:00+00:00", mqtt_enable=False),
                    self._record("2026-11-21T10:00:00+00:00", mqtt_enable=True),
                ],
            )
            with _client(app) as client:
                response = client.get("/devices/192.168.10.20/compare")

        assert response.status_code == 200
        text = response.text
        assert "Compare configurations" in text
        assert "mqtt.enable" in text
        assert "1 changed" in text
        # live state must not leak into the diff
        assert "uptime" not in text
        assert "hidden-pass" not in text

    def test_compare_identical_backups(self):
        """Test equal configurations report no differences."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    self._record("2026-11-20T10:00:00+00:00"),
                    self._record("2026-11-21T10:00:00+00:00"),
                ],
            )
            with _client(app) as client:
                text = client.get("/devices/192.168.10.20/compare").text

        assert "identical configurations" in text

    def test_compare_specific_pair(self):
        """Test explicit backup identifiers select the diff inputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    self._record("2026-11-19T10:00:00+00:00", mqtt_enable=False),
                    self._record("2026-11-20T10:00:00+00:00"),
                    self._record("2026-11-21T10:00:00+00:00"),
                ],
            )
            old_id = [
                r["doc_id"]
                for r in store.all()
                if r["timestamp"].startswith("2026-11-19")
            ][0]
            new_id = [
                r["doc_id"]
                for r in store.all()
                if r["timestamp"].startswith("2026-11-21")
            ][0]
            with _client(app) as client:
                text = client.get(
                    f"/devices/192.168.10.20/compare?a={old_id}&b={new_id}"
                ).text

        assert "1 changed" in text
        assert "mqtt.enable" in text

    def test_compare_rejects_unknown_backups(self):
        """Test unknown or malformed backup identifiers are handled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir, records=[self._record("2026-11-21T10:00:00+00:00")]
            )
            with _client(app) as client:
                base = "/devices/192.168.10.20/compare"

                assert client.get(f"{base}?a=999").status_code == 404
                assert client.get(f"{base}?a=abc").status_code == 400
                assert client.get("/devices/10.9.8.7/compare").status_code == 404

    def test_compare_without_config_payloads(self):
        """Test records lacking configs degrade gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    _record(timestamp="2026-11-20T10:00:00+00:00"),
                    _record(timestamp="2026-11-21T10:00:00+00:00"),
                ],
            )
            with _client(app) as client:
                text = client.get("/devices/192.168.1.10/compare").text

        assert "nothing to compare" in text

    def test_device_page_links_to_diff(self):
        """Test only rows with a newer backup offer the Diff link."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    self._record("2026-11-20T10:00:00+00:00", mqtt_enable=False),
                    self._record("2026-11-21T10:00:00+00:00"),
                ],
            )
            newest, older = (r["doc_id"] for r in store.all())
            with _client(app) as client:
                text = client.get("/devices/192.168.10.20").text

        assert f"compare?a={older}&amp;b={newest}" in text
        # The newest row has no newer backup to compare against, so it shows
        # no Diff button, and never a bare default comparison.
        assert text.count(">Diff<") == 1
        assert 'href="/devices/192.168.10.20/compare"' not in text

    def test_viewer_can_compare(self):
        """Test read-only accounts may compare configurations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[
                    self._record("2026-11-20T10:00:00+00:00", mqtt_enable=False),
                    self._record("2026-11-21T10:00:00+00:00"),
                ],
            )
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            client = TestClient(app)
            _login(client, "guest@example.com", "viewer-password-1")

            assert client.get("/devices/192.168.10.20/compare").status_code == 200


class TestDashboardStats:
    """Tests for the backup size and duration cards on the overview."""

    def test_format_bytes(self):
        """Test human-readable byte sizes."""
        assert _format_bytes(0) == "0 B"
        assert _format_bytes(512) == "512 B"
        assert _format_bytes(2048) == "2.0 KB"
        assert _format_bytes(3 * 1024**2) == "3.0 MB"
        assert _format_bytes(5 * 1024**3) == "5.0 GB"

    def test_format_duration(self):
        """Test human-readable durations."""
        assert _format_duration(None) == "Unknown"
        assert _format_duration(-1) == "Unknown"
        assert _format_duration(0.345) == "345 ms"
        assert _format_duration(4.21) == "4.2 s"
        assert _format_duration(3600) == "1 hour"

    def test_save_and_read_last_automatic_run(self):
        """Test the duration round-trip through the settings database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Configuration(None, tmpdir, None).database_path())
            completed = datetime(2026, 11, 21, 3, 0, 0, tzinfo=UTC)

            assert _saved_last_automatic_duration(store) is None

            _save_last_automatic_run(store, completed, timedelta(seconds=4.21))

            assert _saved_last_automatic_duration(store) == 4.21
            settings = store.get_settings()
            assert settings["last_automatic_run_at"] == completed.isoformat()

    def test_ignores_invalid_saved_duration(self):
        """Test that a corrupted duration value reads as unknown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Configuration(None, tmpdir, None).database_path())
            store.save_settings({"last_automatic_duration": "yesterday"})

            assert _saved_last_automatic_duration(store) is None

    def test_overview_shows_size_and_duration(self):
        """Test the overview cards include size and persisted duration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            store.save_settings(
                {"last_automatic_duration": 4.21, "last_automatic_run_at": "x"}
            )
            with _client(app) as client:
                text = client.get("/").text

            expected_size = _format_bytes(store.backup_size())

        assert "Backup size on disk" in text
        assert expected_size in text
        assert "Last backup duration" in text
        assert "4.2 s" in text

    def test_overview_duration_unknown_without_history(self):
        """Test the duration card falls back to Unknown on a fresh install."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                text = client.get("/").text

        assert "Last backup duration" in text
        assert ">Unknown<" in text.split("Last backup duration</p>")[1]

    @pytest.mark.asyncio
    async def test_scheduled_run_persists_duration(self):
        """Test that the app-wired scheduler stores the run duration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)

            await app.state.scheduler.run_once(automatic=True)

            assert _saved_last_automatic_duration(store) is not None
            assert "last_automatic_run_at" in store.get_settings()
