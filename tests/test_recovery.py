"""Tests for the recovery classification module."""

from retentacle.recovery import recovery_for

_GEN2_RECORD = {
    "doc_id": 7,
    "ip_address": "192.168.10.20",
    "timestamp": "2026-11-15T10:00:00+00:00",
    "generation": 2,
    "device_info": {"gen": 2, "model": "Plus1PM", "mac": "AC:12:3F:11:22:33"},
    "settings": {
        "configuration": {
            "sys": {
                "device": {"name": "Kitchen"},
                "sntp": {"server": "pool.ntp.org"},
                "ui_data": {},
            },
            "wifi": {
                "sta": {"ssid": "HomeNet", "psk": "wifi-secret-123"},
                "ap": {"enabled": False},
            },
            "mqtt": {
                "enable": True,
                "password": "mqtt-secret",
                "topic_prefix": "shellies/kitchen",
            },
            "switch:0": {"name": "Blinds", "initial_state": "off"},
            "cloud": {"enable": True, "connected": True},
        },
        "device_info": {"mac": "AC:12:3F:11:22:33", "uptime": 400},
        "status": {
            "sys": {"uptime": 400, "ram_free": 42000},
            "switch:0": {"output": True, "power": 12.4},
        },
    },
}

_GEN1_RECORD = {
    "doc_id": 3,
    "ip_address": "192.168.10.11",
    "timestamp": "2026-11-15T10:00:00+00:00",
    "generation": 1,
    "device_info": {"type": "SHSW-1", "mac": "ABC123"},
    "settings": {
        "settings": {
            "device": {"name": "Office"},
            "wifi_sta": {"ssid": "HomeNet", "password": "wifi-secret-123"},
            "led_status": None,
        },
        "actions": [{"id": 0, "entity_id": "0"}],
        "status": {"wifi_sta": {"connected": True}, "fs_free": 48000},
    },
}


class TestGen2Classification:
    """Tests for generation 2/3 records."""

    def test_config_and_state_split(self):
        """Test that configuration and status land in the right zones."""
        recovery = recovery_for(_GEN2_RECORD)

        assert recovery.restorable is True
        assert recovery.kind == "setconfig"
        assert recovery.config["wifi"]["sta"]["ssid"] == "HomeNet"
        assert sorted(recovery.state) == ["device_info", "status"]

    def test_summary_groups_and_labels(self):
        """Test readable section labels and flattened leaf paths."""
        groups = {group.label: group for group in recovery_for(_GEN2_RECORD).groups}

        assert "System" in groups
        assert "Wi-Fi" in groups
        assert "Switch 0" in groups
        paths = {item.path for item in groups["Wi-Fi"].items}
        assert "wifi.sta.ssid" in paths

    def test_secrets_are_masked(self):
        """Test that credential leaves never show their value."""
        recovery = recovery_for(_GEN2_RECORD)
        values = {
            item.path: (item.value, item.masked)
            for group in recovery.groups
            for item in group.items
        }

        assert values["wifi.sta.psk"] == ("••••••••", True)
        assert values["mqtt.password"] == ("••••••••", True)
        assert values["wifi.sta.ssid"][1] is False
        assert values["sys.device.name"] == ("Kitchen", False)

    def test_empty_and_none_values_are_skipped(self):
        """Test that empty containers do not clutter the summary."""
        paths = {
            item.path
            for group in recovery_for(_GEN2_RECORD).groups
            for item in group.items
        }

        assert "sys.ui_data" not in paths

    def test_restore_command_and_filename(self):
        """Test the SetConfig command targets the device and payload file."""
        recovery = recovery_for(_GEN2_RECORD)

        assert recovery.restore_filename == "restore-192.168.10.20-7.json"
        assert "rpc/Shelly.SetConfig" in recovery.command
        assert "192.168.10.20" in recovery.command
        assert recovery.restore_filename in recovery.command


class TestGen1Classification:
    """Tests for generation 1 records."""

    def test_config_and_state_split(self):
        """Test that Gen 1 settings are restorable, status and actions are not."""
        recovery = recovery_for(_GEN1_RECORD)

        assert recovery.kind == "settings"
        assert recovery.config["wifi_sta"]["ssid"] == "HomeNet"
        assert set(recovery.state) == {"status", "actions"}

    def test_restore_command_uses_settings_endpoint(self):
        """Test the Gen 1 POST target and payload file."""
        recovery = recovery_for(_GEN1_RECORD)

        assert recovery.command == (
            'curl -sS -X POST "http://192.168.10.11/settings"'
            " --data-binary @restore-192.168.10.11-3.json"
        )

    def test_masking_and_labels(self):
        """Test Gen 1 credential masking and flat section names."""
        values = {
            item.path: (item.value, item.masked)
            for group in recovery_for(_GEN1_RECORD).groups
            for item in group.items
        }

        assert values["wifi_sta.password"] == ("••••••••", True)
        labels = {group.label for group in recovery_for(_GEN1_RECORD).groups}
        assert "Device" in labels


class TestUnknownRecords:
    """Tests for records without a recognizable payload."""

    def test_minimal_shelly_shape(self):
        """Test that configuration-shaped records work without a generation."""
        record = {
            "doc_id": 1,
            "ip_address": "10.0.0.1",
            "settings": {"configuration": {"sys": {"device": {"name": "X"}}}},
        }
        recovery = recovery_for(record)

        assert recovery.restorable is True
        assert recovery.kind == "setconfig"
        assert recovery.state == {}

    def test_unrecognized_record(self):
        """Test that unknown payloads disable the restore view."""
        record = {
            "doc_id": 1,
            "ip_address": "10.0.0.1",
            "settings": {"unexpected": {"a": 1}},
        }
        recovery = recovery_for(record)

        assert recovery.restorable is False
        assert recovery.command == ""
        assert recovery.groups == []
        assert recovery.kind_label == "Unrecognized backup format"

    def test_record_without_settings(self):
        """Test that records without settings are handled gracefully."""
        recovery = recovery_for({"doc_id": 1, "ip_address": "10.0.0.1"})

        assert recovery.restorable is False
