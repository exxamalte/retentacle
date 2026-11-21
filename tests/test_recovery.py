"""Tests for the recovery classification module."""

from retentacle.recovery import backup_highlights, compare_configs, recovery_for

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


class TestHighlights:
    """Tests for the per-backup summary chips."""

    def test_generation_2_chips(self):
        """Test chips for Wi-Fi, MQTT, components and firmware."""
        record = {
            "device_info": {"fw": "20260101-120000/1.4.4"},
            "settings": {
                "configuration": {
                    "wifi": {"sta": {"ssid": "HomeNet"}},
                    "mqtt": {"enable": True},
                    "switch:0": {},
                    "switch:1": {},
                    "light:0": {},
                    "th:0": {},
                },
                "status": {},
            },
        }

        assert backup_highlights(record) == [
            "Wi-Fi: HomeNet",
            "MQTT",
            "1 light",
            "2 switches",
            "1 climate sensor",
            "fw 20260101-120000",
        ]

    def test_generation_1_chips(self):
        """Test Gen 1 SSID and list-based component counts."""
        record = {
            "device_info": {"ver": "1.14.0"},
            "settings": {
                "settings": {
                    "wifi_sta": {"ssid": "HomeNet"},
                    "switches": [{"name": "A"}, {"name": "B"}],
                },
                "status": {},
            },
        }

        assert backup_highlights(record) == [
            "Wi-Fi: HomeNet",
            "2 switches",
            "fw 1.14.0",
        ]

    def test_firmware_prefers_fw_over_ver(self):
        """Test that generation 2 firmware strings drop the build suffix."""
        record = {
            "device_info": {"fw": "20260101-120000/1.4.4", "ver": "ignored"},
            "settings": {"configuration": {"sys": {}}},
        }

        assert backup_highlights(record) == ["fw 20260101-120000"]

    def test_unknown_or_empty_config(self):
        """Test that records without a configuration yield no chips."""
        assert backup_highlights({"settings": {"unexpected": {}}}) == []
        assert backup_highlights({}) == []

    def test_broken_config_values_do_not_crash(self):
        """Test that malformed section values are ignored gracefully."""
        record = {
            "settings": {
                "configuration": {
                    "wifi": "not-a-dict",
                    "mqtt": "not-a-dict",
                    "switch:0": {},
                }
            }
        }

        assert backup_highlights(record) == ["1 switch"]


class TestCompareConfigs:
    """Tests for the configuration diff between two backups."""

    def test_added_removed_changed(self):
        """Test the three diff zones are detected and sorted."""
        left = {
            "mqtt": {"enable": True, "server": "mqtt://a"},
            "wifi": {"sta": {"ssid": "OldNet"}},
        }
        right = {
            "mqtt": {"enable": True},
            "wifi": {"sta": {"ssid": "NewNet"}},
            "switch:0": {"name": "A"},
        }

        diff = compare_configs(left, right)

        assert diff["identical"] is False
        assert [item.path for item in diff["added"]] == ["switch:0.name"]
        assert [item.path for item in diff["removed"]] == ["mqtt.server"]
        assert [(c.path, c.old, c.new) for c in diff["changed"]] == [
            ("wifi.sta.ssid", "OldNet", "NewNet")
        ]

    def test_identical_configs(self):
        """Test that equal configurations report no differences."""
        config = {"sys": {"device": {"name": "X"}}}

        diff = compare_configs(config, dict(config))

        assert diff["identical"] is True
        assert (diff["changed"], diff["added"], diff["removed"]) == ([], [], [])

    def test_empty_configs_are_identical(self):
        """Test that comparing nothing against nothing is identical."""
        assert compare_configs({}, {})["identical"] is True

    def test_secret_changes_are_detected_but_masked(self):
        """Test credential diffs show as changed without leaking values."""
        diff = compare_configs(
            {"wifi": {"sta": {"psk": "old-password"}}},
            {"wifi": {"sta": {"psk": "new-password"}}},
        )

        assert len(diff["changed"]) == 1
        change = diff["changed"][0]
        assert change.old == "••••••••"
        assert change.new == "••••••••"
        assert "password" not in change.old + change.new

    def test_unchanged_secret_is_not_reported(self):
        """Test identical credentials do not appear as a change."""
        diff = compare_configs(
            {"mqtt": {"password": "same"}},
            {"mqtt": {"password": "same"}},
        )

        assert diff["identical"] is True

    def test_unset_values_render_placeholder(self):
        """Test removed empty leaves render as a dash."""
        diff = compare_configs({"sys": {"debug": None}}, {})

        assert [item.value for item in diff["removed"]] == ["—"]
