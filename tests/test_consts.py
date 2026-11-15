"""Tests for constants."""

from shelly_backup import consts


def test_application_name():
    """Test application name constant."""
    assert consts.APPLICATION_NAME == "Shelly Backup"


def test_mdns_service_types():
    """Test mDNS service type constants."""
    assert consts.SHELLY_MDNS_SERVICE_TYPE == "_shelly._tcp.local."
    assert consts.SHELLY_HTTP_MDNS_SERVICE_TYPE == "_http._tcp.local."


def test_server_defaults():
    """Test server and storage default constants."""
    assert consts.DEFAULT_INTERVAL == 3600
    assert consts.DEFAULT_HOST == "127.0.0.1"
    assert consts.DEFAULT_PORT == 8080
    assert consts.DATABASE_FILENAME == "shelly_backup.json"
