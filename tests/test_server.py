"""Tests for the web server module."""

import base64
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient
import pytest

from retentacle import updates
from retentacle.configuration import Configuration
from retentacle.consts import SETTINGS_FILENAME
from retentacle.discovery import DiscoveredDevice
from retentacle.oidc import OIDCError
from retentacle.server import (
    _describe_mount,
    _devices_without_backups,
    _discovery_timeout,
    _effective_ip_addresses,
    _effective_schedule,
    _FolderState,
    _format_bytes,
    _format_datetime_local,
    _format_duration,
    _format_interval,
    _format_interval_value,
    _format_timestamp,
    _interval_form_values,
    _is_read_only,
    _parse_datetime_local,
    _parse_ip_addresses,
    _recent_backup_cutoff,
    _require_writable_target_folder,
    _save_last_automatic_run,
    _saved_ip_addresses,
    _saved_last_automatic_duration,
    _saved_last_automatic_start,
    create_app,
    run_server,
)
from retentacle.storage import BackupStore
from retentacle.templating import templates
from retentacle.updates import UpdateInfo, cached_update
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
        assert 'aria-label="Open the web UI for Kitchen in a new tab"' in (
            response.text
        )

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
        assert 'aria-label="Open the device web UI in a new tab"' in response.text
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
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post("/backup", follow_redirects=False)

                scheduler.run_once.assert_awaited_once_with(["192.168.1.10"])

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_started=1"

    def test_trigger_backup_marks_run_as_queued(self):
        """Test that triggering a backup makes it visible as active immediately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
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
            app, _ = _create_app(tmpdir, records=[_record()])
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
            app, _ = _create_app(tmpdir, records=[_record()])
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
            app, _ = _create_app(tmpdir, records=[_record()])
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
            app, _ = _create_app(tmpdir, records=[_record()])
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
                # The saved redirect reports the resolved slot for the toast.
                assert response.headers["location"].startswith(
                    "/settings?saved=1&next_run="
                )
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

    def test_admin_may_not_self_reset_without_current_password(self):
        """Test the users page cannot bypass the current-password rule."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            admin = app.state.users.list_users()[0]
            with _client(app) as client:
                response = client.post(
                    f"/settings/users/{admin.user_id}/password",
                    data={"password": "another-admin-pass-1"},
                    follow_redirects=False,
                )

                assert response.status_code == 400
                assert "current password" in response.text
                assert (
                    app.state.users.verify_credentials(
                        _ADMIN_EMAIL, "another-admin-pass-1"
                    )
                    is None
                )

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


class TestUpdateBadge:
    """Tests for the admin-only update badge in the header."""

    def _seed_release_cache(self, latest):
        updates.clear_cache()
        # Seeding the module cache directly keeps the test offline.
        updates._state["info"] = UpdateInfo(  # noqa: SLF001
            current=updates.current_version(),
            latest=latest,
            html_url=("https://github.com/exxamalte/retentacle/releases/tag/" + latest),
        )

    def test_admin_sees_update_badge(self):
        """Test the header shows the version badge for admins."""
        self._seed_release_cache("v9999.0.0")
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                text = client.get("/").text

        assert "Update available: v9999.0.0" in text
        # The old template prepended its own "v" to a tag that already has one,
        # producing "vv9999.0.0"; a plain substring check accepted that.
        assert "vv9999.0.0" not in text
        assert 'class="badge update-badge"' in text
        assert (
            'href="https://github.com/exxamalte/retentacle/releases/tag/v9999.0.0"'
            in text
        )

    def test_badge_label_normalises_the_version_prefix(self):
        """Test a repository that tags without a leading v still reads once."""
        self._seed_release_cache("9999.0.1")
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                text = client.get("/").text

        assert "Update available: v9999.0.1" in text
        assert "vv9999.0.1" not in text

    def test_viewer_does_not_see_update_badge(self):
        """Test read-only accounts never see the update badge."""
        self._seed_release_cache("v9999.0.0")
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            client = TestClient(app)
            _login(client, "guest@example.com", "viewer-password-1")
            text = client.get("/").text

        assert 'class="badge update-badge"' not in text
        assert "v9999.0.0" not in text

    def test_no_badge_when_up_to_date(self):
        """Test the badge stays hidden when the release is not newer."""
        self._seed_release_cache("v0.0.1")
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                text = client.get("/").text

        assert "v0.0.1 available" not in text
        assert 'class="badge update-badge"' not in text

    def test_dashboard_view_triggers_background_refresh(self, monkeypatch):
        """Test visiting the dashboard refreshes a stale cache."""
        updates.clear_cache()
        monkeypatch.setenv("RETENTACLE_UPDATE_CHECK", "1")
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "tag_name": "v9999.0.0",
            "html_url": "https://github.com/exxamalte/retentacle/releases",
        }
        with (
            patch("httpx.get", return_value=response),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                client.get("/")
                refreshed = cached_update()
                text = client.get("/").text

        assert refreshed is not None
        assert refreshed.latest == "v9999.0.0"
        assert 'class="badge update-badge"' in text


class TestProxyCookieSecurity:
    """Tests for secure session cookies behind TLS and reverse proxies."""

    def test_https_login_sets_secure_cookie(self):
        """Test sessions over https get the Secure cookie flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app, base_url="https://testserver/") as client:
                response = client.post(
                    "/login",
                    data={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                cookie = response.headers["set-cookie"].lower()
                assert "secure" in cookie
                assert "httponly" in cookie

    def test_plain_http_login_omits_secure_cookie(self):
        """Test plain http sessions do not set the Secure flag (LAN access)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/login",
                    data={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert "secure" not in response.headers["set-cookie"].lower()

    def test_setup_over_https_sets_secure_cookie(self):
        """Test onboarding over https also marks the cookie secure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_setup_app(tmpdir)
            with TestClient(app, base_url="https://testserver/") as client:
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
                assert "secure" in response.headers["set-cookie"].lower()


class TestDeviceAutomaticToggle:
    """Tests for the per-device automatic-backup membership buttons."""

    def test_page_shows_current_membership(self):
        """Test the badge and the mutually exclusive toggle button."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                text = client.get("/devices/192.168.1.10").text
                assert "Manual" in text
                assert 'id="automatic-enable"' in text
                assert 'id="automatic-disable"' not in text

                store.save_settings(
                    {**store.get_settings(), "ip_addresses": ["192.168.1.10"]}
                )
                text = client.get("/devices/192.168.1.10").text

                assert "Automatic" in text
                assert 'id="automatic-disable"' in text
                assert 'id="automatic-enable"' not in text

    def test_enable_adds_device_to_settings_list(self):
        """Test enabling a device persists it into the automatic list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                response = client.post(
                    "/devices/192.168.1.10/automatic/enable", follow_redirects=False
                )

                assert response.status_code == 303
                assert (
                    response.headers["location"] == "/devices/192.168.1.10?automatic=on"
                )
                assert store.get_settings()["ip_addresses"] == ["192.168.1.10"]

    def test_enable_materializes_configured_devices(self):
        """Test enabling keeps devices from the configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration("192.168.9.9", tmpdir, None, interval=0)
            store = BackupStore(config.database_path())
            store.add(_record())
            users = UserStore(config.users_path(), pbkdf2_iterations=1)
            users.create_user(_ADMIN_EMAIL, _ADMIN_PASSWORD, UserRole.ADMIN)
            app = create_app(config, store=store, users=users)
            with _client(app) as client:
                response = client.post(
                    "/devices/192.168.1.10/automatic/enable", follow_redirects=False
                )

            assert response.status_code == 303
            # Materialized and toggled devices share one IP-ordered list.
            assert store.get_settings()["ip_addresses"] == [
                "192.168.1.10",
                "192.168.9.9",
            ]

    def test_disable_removes_only_that_device(self):
        """Test removing one device leaves the others automatic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(
                tmpdir,
                records=[_record(), _record(ip_address="192.168.1.11")],
            )
            store.save_settings(
                {
                    **store.get_settings(),
                    "ip_addresses": ["192.168.1.10", "192.168.1.11"],
                }
            )
            with _client(app) as client:
                response = client.post(
                    "/devices/192.168.1.10/automatic/disable",
                    follow_redirects=False,
                )

                assert response.status_code == 303
                assert (
                    response.headers["location"]
                    == "/devices/192.168.1.10?automatic=off"
                )
                assert store.get_settings()["ip_addresses"] == ["192.168.1.11"]

    def test_notices_render_after_redirect(self):
        """Test the flash notices after toggling."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                on = client.get("/devices/192.168.1.10?automatic=on").text
                off = client.get("/devices/192.168.1.10?automatic=off").text

        assert "now included in automatic backups" in on
        assert "no longer receives automatic backups" in off

    def test_toggle_requires_admin(self):
        """Test read-only accounts cannot change membership."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            client = TestClient(app)
            _login(client, "guest@example.com", "viewer-password-1")

            assert client.get("/devices/192.168.1.10").status_code == 200
            assert (
                'id="automatic-enable"' not in client.get("/devices/192.168.1.10").text
            )
            assert (
                client.post("/devices/192.168.1.10/automatic/enable").status_code == 403
            )
            assert (
                client.post("/devices/192.168.1.10/automatic/disable").status_code
                == 403
            )

    def test_toggle_unknown_device_not_found(self):
        """Test toggling a device without backups fails cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/devices/192.168.1.99/automatic/enable", follow_redirects=False
                )

                assert response.status_code == 404


class TestToastNotifications:
    """Tests for flash messages rendered as overlay toasts."""

    def test_flash_messages_render_as_toasts(self):
        """Test flashes appear as toast scripts without layout blocks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[_record()])
            with _client(app) as client:
                text = client.get("/?backup_started=1").text

                assert 'id="toasts"' in text
                assert "function showToast" in text
                assert 'showToast("Backup started' in text
                assert '<div class="notice"' not in text

    def test_device_flash_escapes_names(self):
        """Test device names with quotes render safely in toast scripts."""
        record = _record()
        record["device_info"] = {"gen": 2, "name": 'Kitchen "Special"'}
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir, records=[record])
            with _client(app) as client:
                text = client.get("/devices/192.168.1.10?automatic=on").text

        assert 'showToast("Kitchen \\"Special\\" is now included' in text

    def test_settings_error_renders_as_error_toast(self):
        """Test validation failures use the toast error variant."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/settings/retention", data={"retention_monthly": "abc"}
                )

        assert response.status_code == 400
        assert 'showToast("Could not save settings' in response.text
        assert (
            '<div class="alert"'
            not in response.text.split('aria-label="Backup schedule"')[0]
        )

    def test_login_error_stays_inline(self):
        """Test the login card keeps its inline error alert."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with TestClient(app) as client:
                response = client.post(
                    "/login",
                    data={"email": _ADMIN_EMAIL, "password": "totally-wrong-pass"},
                )

        assert response.status_code == 401
        assert '<div class="alert"' in response.text


class TestDataFolderGuard:
    """Tests for the startup check on the data folder."""

    def test_writable_folder_passes_without_leftovers(self):
        """Test a usable data folder is accepted and cleaned up again."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, interval=0)

            data_dir = _require_writable_target_folder(config)

            assert data_dir == Path(tmpdir)
            assert not (data_dir / ".write-test").exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder modes")
    def test_unwritable_folder_explains_the_remedy(self):
        """Test the failure names the Docker causes and their fixes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            locked = Path(tmpdir) / "locked"
            locked.mkdir(mode=0o500)
            config = Configuration(None, str(locked), None, interval=0)

            with pytest.raises(OSError, match="cannot be used") as excinfo:
                _require_writable_target_folder(config)

        message = str(excinfo.value)
        assert "cannot be used" in message
        # The message names the offending folder, the facts about it, and the
        # current image; the cause-specific advice is covered separately.
        assert str(locked) in message
        assert "Found:" in message
        assert "is owned by" in message
        assert "the server runs as" in message
        assert "Remedy:" in message
        assert ":main" in message


class TestScheduleAnchorRecovery:
    """Tests for recovering the backup schedule after a restart."""

    def _store(self, tmpdir, settings):
        """Create a store with the given settings and return it with the config."""
        config = Configuration(None, tmpdir, None, interval=0)
        store = BackupStore(config.database_path())
        store.save_settings(settings)
        return config, store

    def test_missing_pending_slot_uses_the_last_automatic_run(self):
        """Test the saved run keeps the schedule instead of the start time."""
        started = datetime.now(UTC) - timedelta(hours=1)
        settings = {
            "automatic_backups": True,
            "interval": 86400,
            "last_automatic_run_at": (started + timedelta(seconds=40)).isoformat(),
            "last_automatic_duration": 40,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config, store = self._store(tmpdir, settings)

            enabled, interval, next_run, anchor = _effective_schedule(config, store)

        assert enabled is True
        assert interval == 86400
        assert next_run is None
        # The saved completion time is 40 s after the start, and the start of
        # the run is what the schedule grid is anchored to.
        assert anchor is not None
        assert abs((anchor - started).total_seconds()) < 2

    def test_without_any_saved_state_nothing_is_anchored(self):
        """Test a first installation has no anchor to recover from."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config, store = self._store(
                tmpdir, {"automatic_backups": True, "interval": 86400}
            )

            assert _effective_schedule(config, store) == (True, 86400, None, None)

    def test_invalid_saved_timestamp_is_ignored(self):
        """Test a corrupt timestamp does not break the schedule."""
        settings = {
            "automatic_backups": True,
            "interval": 86400,
            "next_run_at": "not-a-date",
            "last_automatic_run_at": "also-not-a-date",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config, store = self._store(tmpdir, settings)

            assert _effective_schedule(config, store) == (True, 86400, None, None)

    def test_duration_is_optional(self):
        """Test the completion time is used when no duration was saved."""
        completed = datetime.now(UTC)
        assert (
            _saved_last_automatic_start(
                {"last_automatic_run_at": completed.isoformat()}
            )
            == completed
        )
        assert _saved_last_automatic_start({"last_automatic_duration": 40}) is None

    def test_app_recovery_schedules_the_slot_not_the_restart(self):
        """Test a started app waits for the grid slot instead of backing up."""
        anchor = datetime.now(UTC) - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            config, store = self._store(
                tmpdir,
                {
                    "automatic_backups": True,
                    "interval": 86400,
                    "last_automatic_run_at": anchor.isoformat(),
                },
            )
            users = UserStore(config.users_path(), pbkdf2_iterations=1)
            users.create_user(_ADMIN_EMAIL, _ADMIN_PASSWORD, UserRole.ADMIN)
            app = create_app(config, store=store, users=users)
            scheduler = app.state.scheduler

            with TestClient(app):
                assert scheduler.run_count == 0
                expected = anchor + timedelta(seconds=86400)
                assert abs((scheduler.next_run - expected).total_seconds()) < 10


class TestDataFolderDiagnosis:
    """Tests for explaining an unusable data folder precisely."""

    # Excerpt of /proc/self/mountinfo from a Docker host with a bind mount.
    _MOUNTINFO = [
        "1 0 8:1 / / rw,relatime - ext4 /dev/sda1 rw",
        "36 1 0:35 / /data rw,relatime - tmpfs tmpfs rw,mode=0755",
        "41 36 8:2 /srv/retentacle /data/nested ro,nosuid - xfs /dev/sdb1 ro",
        "44 1 0:40 / /run/rpc_pipefs rw,relatime - rpc_pipefs sunrpc rw",
    ]

    def test_mount_details_pick_the_most_specific_entry(self):
        """Test the deepest covering mount is used for the description."""
        assert _describe_mount(self._MOUNTINFO, "/data") == ("tmpfs", "rw,relatime")
        assert _describe_mount(self._MOUNTINFO, "/data/nested/db.json") == (
            "xfs",
            "ro,nosuid",
        )
        # Everything else falls back to the root mount.
        assert _describe_mount(self._MOUNTINFO, "/etc/hostname") == (
            "ext4",
            "rw,relatime",
        )

    def test_mount_details_ignores_incomplete_lines(self):
        """Test truncated mountinfo lines are skipped."""
        assert _describe_mount(["garbage", "2 1 8:1 /"], "/data") is None
        assert _describe_mount([], "/data") is None

    def test_read_only_detection_uses_option_boundaries(self):
        """Test only a real read-only flag counts, not option substrings."""
        assert _is_read_only(("xfs", "ro,nosuid")) is True
        assert _is_read_only(("ext4", "rw,relatime")) is False
        assert _is_read_only(("ext4", "size=root")) is False
        assert _is_read_only(None) is False

    def test_stored_database_is_checked_not_only_the_folder(self):
        """Test a read-only database inside a writable folder still fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data = Path(tmpdir)
            stored = data / SETTINGS_FILENAME
            stored.write_text("{}", encoding="utf-8")
            os.chmod(stored, 0o444)
            config = Configuration(None, tmpdir, None, interval=0)

            with pytest.raises(OSError, match=SETTINGS_FILENAME) as excinfo:
                _require_writable_target_folder(config)

        assert "chmod u+w" in str(excinfo.value)

    def test_healthy_folder_reports_no_remedy(self):
        """Test the collected facts of a usable folder need no repair hints."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _FolderState.collect(Path(tmpdir))

        assert state.read_only is False
        assert not state.foreign_files
        assert any("the server runs as" in item for item in state.evidence())

    def test_foreign_owner_and_selinux_produce_their_own_advice(self):
        """Test each container cause is named only when it is present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data = Path(tmpdir)
            foreign = _FolderState(
                path=data,
                folder_owner="0:0",
                folder_mode="0755",
                file_owners={SETTINGS_FILENAME: "0:0"},
                process_owner="10001:10001",
                mount=("ext4", "rw,relatime"),
                read_only=False,
                selinux=None,
            )
            squashed = replace(foreign, file_owners={})
            labelled = replace(foreign, selinux="enforcing", folder_owner="10001:10001")
            readonly = replace(foreign, mount=("nfs", "ro,relatime"), read_only=True)

            text = "; ".join(foreign.remedies())
            assert "user:" in text
            assert "chown" in text
            assert SETTINGS_FILENAME in text
            assert SETTINGS_FILENAME not in "; ".join(squashed.remedies())
            assert ":z" in "; ".join(labelled.remedies())
            assert "read-write" in "; ".join(readonly.remedies())


class TestBackupRunWithoutDevices:
    """Tests for starting a backup while nothing is configured."""

    def test_run_is_not_started(self):
        """Test the backup button does not start an empty run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post("/backup", follow_redirects=False)

                scheduler.run_once.assert_not_awaited()
                assert scheduler.queued is False

        assert response.status_code == 303
        assert response.headers["location"] == "/?backup_empty=1"

    def test_empty_run_is_reported_as_such(self):
        """Test the dashboard explains why nothing was backed up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                text = client.get("/?backup_empty=1").text

        assert 'showToast("No devices configured yet' in text
        assert "Backup started" not in text

    def test_configured_devices_start_the_run(self):
        """Test a saved device list makes the button work as before."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["192.168.1.10"]})
            with _client(app) as client:
                scheduler = app.state.scheduler
                scheduler.run_once = AsyncMock()
                response = client.post("/backup", follow_redirects=False)

                scheduler.run_once.assert_awaited_once_with(["192.168.1.10"])

        assert response.headers["location"] == "/?backup_started=1"


class TestScheduleIntervalChange:
    """Tests for changing the repeat interval on the settings page."""

    def _hourly_app(self, tmpdir, usual_time):
        """Build an app running hourly, with a grid anchored at usual_time."""
        config = Configuration(None, tmpdir, None, interval=0)
        store = BackupStore(config.database_path())
        store.save_settings(
            {
                "interval": 3600,
                "automatic_backups": True,
                "next_run_at": usual_time.isoformat(),
            }
        )
        users = UserStore(config.users_path(), pbkdf2_iterations=1)
        users.create_user(_ADMIN_EMAIL, _ADMIN_PASSWORD, UserRole.ADMIN)
        app = create_app(config, store=store, users=users)
        scheduler = app.state.scheduler
        # The server has been running hourly for a while, so the pending slot
        # is the next hour - not the time the user originally chose.
        pending = datetime.now(UTC) + timedelta(minutes=40)
        scheduler._anchor_at = usual_time  # noqa: SLF001
        scheduler._next_run_at = pending  # noqa: SLF001
        return app, store, pending

    def test_untouched_time_keeps_the_usual_backup_time(self):
        """Test switching to daily lands on the daily slot, not the page time."""
        usual = (datetime.now(UTC) - timedelta(days=1)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store, pending = self._hourly_app(tmpdir, usual)
            with _client(app) as client:
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "1d",
                        "next_run": _format_datetime_local(pending),
                    },
                    follow_redirects=False,
                )
                next_run = app.state.scheduler.next_run
                anchor = app.state.scheduler.anchor
                saved = dict(store.get_settings())

        assert response.status_code == 303
        assert next_run is not None
        # Not the prefilled hourly slot any more, but the same clock time.
        assert next_run > pending
        assert (next_run - usual).total_seconds() % 86400 == 0
        assert (next_run.hour, next_run.minute) == (23, 0)
        assert anchor == next_run
        assert saved["interval"] == 86400
        assert saved["next_run_at"] == next_run.isoformat()

    def test_edited_time_is_used_as_given(self):
        """Test an explicit new date and time wins over the projection."""
        usual = (datetime.now(UTC) - timedelta(days=1)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        chosen = (datetime.now(UTC) + timedelta(days=2)).replace(
            hour=4, minute=30, second=0, microsecond=0
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _store, _pending = self._hourly_app(tmpdir, usual)
            with _client(app) as client:
                client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "1d",
                        "next_run": _format_datetime_local(chosen),
                    },
                    follow_redirects=False,
                )

        assert app.state.scheduler.next_run == chosen

    def test_redirect_reports_the_resolved_time(self):
        """Test the confirmation toast can name the new next backup time."""
        usual = (datetime.now(UTC) - timedelta(days=1)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _store, pending = self._hourly_app(tmpdir, usual)
            with _client(app) as client:
                response = client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "1d",
                        "next_run": _format_datetime_local(pending),
                    },
                    follow_redirects=False,
                )
                location = response.headers["location"]
                page = client.get(location).text

        assert location.startswith("/settings?saved=1&next_run=")
        assert "Next automatic backup: " in page
        assert "toLocaleString" in page
        assert "retention is applied" not in page

    def test_same_interval_never_moves_the_slot(self):
        """Test re-saving without an interval change keeps the pending slot."""
        usual = (datetime.now(UTC) - timedelta(days=1)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _store, pending = self._hourly_app(tmpdir, usual)
            with _client(app) as client:
                client.post(
                    "/settings",
                    data={
                        "automatic_backups": "on",
                        "interval_preset": "1h",
                        "next_run": _format_datetime_local(pending),
                    },
                    follow_redirects=False,
                )
                next_run = app.state.scheduler.next_run

        assert abs((next_run - pending).total_seconds()) < 60


class TestDiscoveryTimeout:
    """Tests for the mDNS discovery time budget and its feedback."""

    _ENV = "RETENTACLE_DISCOVERY_TIMEOUT"

    def test_default_budget_is_thirty_seconds(self):
        """Test the scan waits long enough for delayed mDNS answers."""
        with patch.dict(os.environ, {self._ENV: ""}):
            assert _discovery_timeout() == 30.0

    def test_environment_overrides_the_budget(self):
        """Test the timeout can be tuned without a code change."""
        with patch.dict(os.environ, {self._ENV: "8.5"}):
            assert _discovery_timeout() == 8.5

    @pytest.mark.parametrize("value", ["wide", "0", "-3"])
    def test_unusable_values_fall_back(self, value, caplog):
        """Test nonsense budgets are ignored with a warning."""
        with (
            patch.dict(os.environ, {self._ENV: value}),
            caplog.at_level("WARNING", logger="retentacle.server"),
        ):
            assert _discovery_timeout() == 30.0

        assert "RETENTACLE_DISCOVERY_TIMEOUT" in caplog.text

    def test_stream_passes_the_budget_and_advertises_it(self):
        """Test the browser learns the exact deadline it is counting down."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            scan = Mock(return_value=iter([]))
            with (
                patch.dict(os.environ, {self._ENV: "12"}),
                patch("retentacle.server.iter_shelly_device_details", scan),
                _client(app) as client,
            ):
                response = client.post("/settings/discover/stream")

        assert response.headers["x-discovery-timeout"] == "12"
        scan.assert_called_once_with(timeout=12.0)

    def test_form_fallback_uses_the_same_budget(self):
        """Test the plain form path scans for just as long."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with (
                patch.dict(os.environ, {self._ENV: "12"}),
                patch(
                    "retentacle.server.discover_shelly_device_details",
                    return_value=[],
                ) as scan,
                _client(app) as client,
            ):
                client.post("/settings/discover")

        assert scan.call_args.kwargs["timeout"] == 12.0

    def test_page_states_and_counts_down_the_scan(self):
        """Test the UI names the duration and can render the countdown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                page = client.get("/settings").text

        assert "Scan the network via mDNS for up to\n        30" in page
        assert "x-discovery-timeout" in page
        assert "startCountdown(" in page
        assert "almost done" in page


class TestDeviceOrderIsStable:
    """Tests for the order of the configured device list."""

    _LEGACY = ["10.10.4.10", "10.10.4.9", "192.168.1.5", "2001:db8::1"]

    def test_a_legacy_list_displays_in_ip_order(self):
        """Test stored append order does not decide the settings table."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": list(self._LEGACY)})
            with _client(app) as client:
                body = client.get("/settings").text
                body = body[body.find('id="devices"') :]

            listed = re.findall(r'class="device-ip">([^<]+)<', body)

        assert listed == ["10.10.4.9", "10.10.4.10", "192.168.1.5", "2001:db8::1"]

    def test_adding_devices_persists_them_in_order(self):
        """Test newly added devices do not just pile up at the end."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": list(self._LEGACY)})
            with _client(app) as client:
                client.post(
                    "/settings/devices",
                    data={"ip_addresses": "10.10.4.2"},
                    follow_redirects=False,
                )

            assert store.get_settings()["ip_addresses"] == [
                "10.10.4.2",
                "10.10.4.9",
                "10.10.4.10",
                "192.168.1.5",
                "2001:db8::1",
            ]

    def test_backup_devices_follow_the_same_order(self):
        """Test scheduled and manual runs use the sorted device list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": list(self._LEGACY), "interval": 3600})
            config = app.state.configuration

            assert _effective_ip_addresses(config, store) == [
                "10.10.4.9",
                "10.10.4.10",
                "192.168.1.5",
                "2001:db8::1",
            ]

    def test_automatic_toggle_keeps_the_order(self):
        """Test opting one device in or out does not reorder the rest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            store.save_settings({"ip_addresses": ["10.10.4.10", "10.10.4.9"]})
            store.add(
                {"ip_address": "10.10.4.2", "timestamp": "2026-11-15T10:00:00+00:00"}
            )
            with _client(app) as client:
                client.post(
                    "/devices/10.10.4.2/automatic/enable", follow_redirects=False
                )

            assert store.get_settings()["ip_addresses"] == [
                "10.10.4.2",
                "10.10.4.9",
                "10.10.4.10",
            ]


class TestDevicesWithoutBackups:
    """Tests for the dashboard count of devices lacking a backup."""

    def _app(self, tmpdir, configured, backed_up=()):
        """Build an app with the given configured and backed-up devices."""
        config = Configuration(None, tmpdir, None, interval=0)
        store = BackupStore(config.database_path())
        if configured:
            store.save_settings({"ip_addresses": list(configured)})
        for index, ip_address in enumerate(backed_up):
            store.add(
                {
                    "ip_address": ip_address,
                    "timestamp": f"2026-11-1{index}T10:00:00+00:00",
                    "device_info": {"gen": 3, "name": f"Shelly{ip_address}"},
                }
            )
        users = UserStore(config.users_path(), pbkdf2_iterations=1)
        users.create_user(_ADMIN_EMAIL, _ADMIN_PASSWORD, UserRole.ADMIN)
        users.create_user("viewer@example.com", _ADMIN_PASSWORD, UserRole.VIEWER)
        return create_app(config, store=store, users=users), store

    def test_missing_devices_are_detected(self):
        """Test only configured devices without any backup are listed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = self._app(
                tmpdir,
                ["10.10.4.10", "10.10.4.9", "10.10.4.30"],
                backed_up=["10.10.4.10", "10.10.4.9"],
            )

            assert _devices_without_backups(app.state.configuration, store) == [
                "10.10.4.30",
            ]

    def test_listed_in_ip_order(self):
        """Test the count uses the same numeric order as the tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = self._app(
                tmpdir, ["10.10.4.100", "10.10.4.9", "2001:db8::2", "10.10.4.20"]
            )

            assert _devices_without_backups(app.state.configuration, store) == [
                "10.10.4.9",
                "10.10.4.20",
                "10.10.4.100",
                "2001:db8::2",
            ]

    def test_discovery_coverage_stays_quiet(self):
        """Test devices covered only by discovery are not claimed as missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None, discover=True, interval=0)
            store = BackupStore(config.database_path())
            store.add(
                {"ip_address": "10.10.4.9", "timestamp": "2026-11-15T10:00:00+00:00"}
            )

            assert _devices_without_backups(config, store) == []

    def test_subtitle_counts_the_missing_devices(self):
        """Test the quiet subtitle line is the only place this is reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = self._app(
                tmpdir,
                ["10.10.4.9", "10.10.4.10", "10.10.4.30"],
                backed_up=["10.10.4.9"],
            )
            with _client(app) as client:
                page = client.get("/").text

        assert "1 device with backups" in page
        assert "· 2 devices without a backup" in page

    def test_read_only_users_see_the_count(self):
        """Test viewers get the same subtitle without administrative actions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = self._app(
                tmpdir, ["10.10.4.9", "10.10.4.30"], backed_up=["10.10.4.9"]
            )
            with TestClient(app) as client:
                client.post(
                    "/login",
                    data={"email": "viewer@example.com", "password": _ADMIN_PASSWORD},
                    follow_redirects=False,
                )
                page = client.get("/").text

        assert "· 1 device without a backup" in page
        assert "Run backup now" not in page

    def test_nothing_is_reported_when_all_are_backed_up(self):
        """Test a healthy installation shows no extra wording at all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = self._app(
                tmpdir,
                ["10.10.4.9", "10.10.4.10"],
                backed_up=["10.10.4.9", "10.10.4.10"],
            )
            with _client(app) as client:
                page = client.get("/").text

        assert "without a backup" not in page
        assert "no backup yet" not in page

    def test_no_warning_message_anywhere(self):
        """Test the removed warning does not come back in any state."""
        states = (
            (["10.10.4.9", "10.10.4.30"], ["10.10.4.9"]),  # some missing
            (["10.10.4.9", "10.10.4.30"], []),  # fresh install
            ([], []),  # nothing configured
        )
        for configured, backed_up in states:
            with tempfile.TemporaryDirectory() as tmpdir:
                app, _ = self._app(tmpdir, configured, backed_up)
                with _client(app) as client:
                    page = client.get("/").text

            assert "no backup yet" not in page
            assert "pending-backups" not in page


class TestLastAdminRoleLock:
    """Tests for the users table when only one admin exists."""

    def _users_section(self, client):
        html = client.get("/settings").text
        return html[html.find('id="users"') :]

    def _row(self, section, email):
        for row in re.findall(r"<tr>(.*?)</tr>", section, re.DOTALL):
            if email in row:
                return row
        raise AssertionError(f"row for {email} not found")

    def test_role_control_is_hidden_for_the_only_admin(self):
        """Test the last admin is offered no way to change their role."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            admin = app.state.users.list_users()[0]
            with _client(app) as client:
                section = self._users_section(client)
                row = self._row(section, _ADMIN_EMAIL)

        assert "Only admin" in row
        assert "<select" not in row
        assert f"/settings/users/{admin.user_id}/role" not in section

    def test_explanation_is_shown(self):
        """Test the reason for the lock is stated instead of silent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                section = self._users_section(client)

        assert "only administrator account" in section

    def test_read_only_accounts_stay_assignable(self):
        """Test promoting another account is unaffected by the lock."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            guest = app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            with _client(app) as client:
                row = self._row(self._users_section(client), "guest@example.com")

        assert "<select" in row
        assert f"/settings/users/{guest.user_id}/role" in row

    def test_control_returns_once_a_second_admin_exists(self):
        """Test the lock lifts as soon as another admin is available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            admin = users.list_users()[0]
            users.create_user(
                "second@example.com", "second-admin-pass-1", UserRole.ADMIN
            )
            with _client(app) as client:
                section = self._users_section(client)
                row = self._row(section, _ADMIN_EMAIL)

        assert "<select" in row
        assert "Only admin" not in row
        assert "only administrator account" not in section
        assert f"/settings/users/{admin.user_id}/role" in section

    def test_locked_admin_can_still_change_its_own_password(self):
        """Test the role lock does not block the self-service password form."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            with _client(app) as client:
                response = client.post(
                    "/account/password",
                    data={
                        "current_password": _ADMIN_PASSWORD,
                        "new_password": "another-admin-pass-9",
                        "confirm_password": "another-admin-pass-9",
                    },
                    follow_redirects=False,
                )

            assert response.status_code == 303
            assert users.verify_credentials(_ADMIN_EMAIL, "another-admin-pass-9")
            assert users.verify_credentials(_ADMIN_EMAIL, _ADMIN_PASSWORD) is None


class TestAccountPasswordPage:
    """Tests for the self-service change-password flow."""

    _NEW = "brand-new-pass-9"

    def _form(self, current=_ADMIN_PASSWORD, new=None, confirm=None):
        new = new or self._NEW
        return {
            "current_password": current,
            "new_password": new,
            "confirm_password": confirm if confirm is not None else new,
        }

    def test_page_offers_the_change_form(self):
        """Test a local account sees current, new and confirm fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                page = client.get("/account").text

        assert "Change password" in page
        for field in ("current_password", "new_password", "confirm_password"):
            assert f'name="{field}"' in page
        assert "Reset" not in page

    def test_provider_only_account_has_no_form(self):
        """Test an SSO-provisioned account is told where sign-in is managed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            sso = app.state.users.create_user("sso@example.com", None, UserRole.VIEWER)
            with TestClient(app) as client:
                # A provider account cannot sign in by password, so seed a session.
                token = app.state.users.create_session(sso.user_id)
                client.cookies.set("retentacle_session", token)
                page = client.get("/account").text

        assert "no local password" in page
        assert 'name="new_password"' not in page

    def test_password_change_keeps_this_session(self):
        """Test the active device stays signed in and others are logged out."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            admin = users.list_users()[0]
            other_session = users.create_session(admin.user_id)
            with _client(app) as client:
                token = client.cookies.get("retentacle_session")
                response = client.post(
                    "/account/password", data=self._form(), follow_redirects=False
                )

                assert response.status_code == 303
                assert response.headers["location"] == "/account?password_changed=1"
                follow_up = client.get("/account")
                assert users.verify_credentials(_ADMIN_EMAIL, self._NEW) is not None
                assert users.verify_credentials(_ADMIN_EMAIL, _ADMIN_PASSWORD) is None
                assert users.resolve_session(token) is not None
                assert users.resolve_session(other_session) is None

        assert follow_up.status_code == 200
        assert "showToast" in follow_up.text

    def test_mismatched_confirmation_is_refused(self):
        """Test the two new password fields have to agree."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/account/password",
                    data=self._form(new=self._NEW, confirm="different-pass-99"),
                )

        assert response.status_code == 400
        assert "do not match" in response.text

    def test_wrong_current_password_is_refused(self):
        """Test guessing the current password does not change anything."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            users = app.state.users
            with _client(app) as client:
                response = client.post(
                    "/account/password",
                    data=self._form(current="not-my-password-1"),
                )

            assert response.status_code == 400
            assert "current password is not correct" in response.text
            assert users.verify_credentials(_ADMIN_EMAIL, self._NEW) is None

    def test_weak_new_password_is_refused(self):
        """Test the strength policy applies to the self-service form too."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                response = client.post(
                    "/account/password",
                    data=self._form(new="short1!", confirm="short1!"),
                )

        assert response.status_code == 400
        assert "Password not changed" in response.text

    def test_page_requires_a_login(self):
        """Test anonymous visitors are sent to the login page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            response = TestClient(app).get("/account", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_avatar_menu_links_to_the_account_page(self):
        """Test the menu offers the change for local accounts only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            sso = app.state.users.create_user("sso2@example.com", None, UserRole.VIEWER)
            with _client(app) as client:
                menu = client.get("/").text
            with TestClient(app) as visitor:
                visitor.cookies.set(
                    "retentacle_session", app.state.users.create_session(sso.user_id)
                )
                sso_menu = visitor.get("/").text

        assert 'href="/account"' in menu
        assert "Change password" in menu
        assert 'href="/account"' not in sso_menu
        assert "Signed in via provider" in sso_menu


class TestUsersTablePasswordColumn:
    """Tests for the per-row password and delete affordances."""

    def _section(self, client):
        html = client.get("/settings").text
        return html[html.find('id="users"') :]

    def _row(self, section, email):
        for row in re.findall(r"<tr>(.*?)</tr>", section, re.DOTALL):
            if email in row:
                return row
        raise AssertionError(f"no row for {email}")

    def test_own_row_links_to_the_account_page(self):
        """Test the signed-in account is not offered a password override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            with _client(app) as client:
                row = self._row(self._section(client), _ADMIN_EMAIL)

        assert 'href="/account"' in row
        assert "Needs your current password." in row
        assert 'name="password"' not in row

    def test_other_rows_use_set_password_wording(self):
        """Test an administrator action is labelled as what it is."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.users.create_user(
                "guest@example.com", "viewer-password-1", UserRole.VIEWER
            )
            with _client(app) as client:
                row = self._row(self._section(client), "guest@example.com")

        assert "Set password" in row
        assert "Reset" not in row

    def test_passwordless_account_offers_a_first_password(self):
        """Test provider accounts can be given a password by an admin."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.users.create_user("sso3@example.com", None, UserRole.VIEWER)
            with _client(app) as client:
                row = self._row(self._section(client), "sso3@example.com")

        assert "Set first password" in row
        assert "no local password" not in row

    def test_delete_reasons_are_shown(self):
        """Test blocked deletions explain themselves instead of greying out."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, _ = _create_app(tmpdir)
            app.state.users.create_user(
                "colleague@example.com", "colleague-pass-1", UserRole.ADMIN
            )
            with _client(app) as client:
                section = self._section(client)
                own = self._row(section, _ADMIN_EMAIL)
                other = self._row(section, "colleague@example.com")

        assert "Signed in" in own
        assert "cannot delete the account you are signed in with" in own
        assert "Delete" in other
        assert 'action="/settings/users/' in other


class TestOIDCDiagnostics:
    """Tests for making OIDC setup problems visible where they are fixed."""

    def test_settings_show_the_derived_redirect_uri(self):
        """Test the page states the URI that must be registered at the provider."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with _client(app) as client:
                page = client.get("/settings").text
            block = page[page.find("oidc-redirect-preview") :]

        assert "http://testserver/oidc/callback" in block
        assert "FORWARDED_ALLOW_IPS" in block

    def test_settings_show_the_configured_redirect_uri(self):
        """Test an explicit callback URL is echoed instead of a guess."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store, callback_url="https://backup.example.com/oidc/callback")
            with _client(app) as client:
                page = client.get("/settings").text
            block = page[page.find("oidc-redirect-preview") :]

        assert "https://backup.example.com/oidc/callback" in block
        assert "FORWARDED_ALLOW_IPS" not in block

    def test_login_start_logs_the_redirect_uri(self, caplog):
        """Test the server log records the URI the provider will compare."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store, callback_url="https://backup.example.com/oidc/callback")
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    return_value=dict(_OIDC_ENDPOINTS),
                ),
                caplog.at_level("INFO", logger="retentacle.auth"),
                TestClient(app) as client,
            ):
                client.get("/oidc/login", follow_redirects=False)

        assert "redirect_uri=https://backup.example.com/oidc/callback" in caplog.text

    def test_discovery_failure_names_the_issuer_and_the_cause(self, caplog):
        """Test an unreachable provider explains the server-side requirement."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app, store = _create_app(tmpdir)
            _seed_oidc(store)
            with (
                patch(
                    "retentacle.auth.discover_endpoints",
                    side_effect=OIDCError(
                        "Unable to reach the OIDC discovery document: connect error"
                    ),
                ),
                caplog.at_level("WARNING", logger="retentacle.auth"),
                TestClient(app) as client,
            ):
                response = client.get("/oidc/login", follow_redirects=False)

        assert response.status_code == 400
        assert "https://id.example.com" in response.text
        assert "must be able to reach" in response.text
        assert "OIDC discovery failed for https://id.example.com" in caplog.text
