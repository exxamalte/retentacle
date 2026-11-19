"""Tests for constants."""

from retentacle import consts


def test_application_name():
    """Test application name constant."""
    assert consts.APPLICATION_NAME == "Retentacle"


def test_application_tagline():
    """Test application tagline constant."""
    assert consts.APPLICATION_TAGLINE == "Tentacles for your Shelly backups."


def test_mdns_service_types():
    """Test mDNS service type constants."""
    assert consts.SHELLY_MDNS_SERVICE_TYPE == "_shelly._tcp.local."
    assert consts.SHELLY_HTTP_MDNS_SERVICE_TYPE == "_http._tcp.local."


def test_server_defaults():
    """Test server and storage default constants."""
    assert consts.DEFAULT_INTERVAL == 3600
    assert consts.DEFAULT_HOST == "127.0.0.1"
    assert consts.DEFAULT_PORT == 8080
    assert consts.DATABASE_FILENAME == "retentacle_backups.json"
    assert consts.SETTINGS_FILENAME == "retentacle_settings.json"
