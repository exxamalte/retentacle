"""Tests for configuration module."""

from pathlib import Path
import tempfile

import pytest
import yaml

from retentacle.configuration import Configuration


class TestConfiguration:
    """Test Configuration class."""

    def test_init_without_config_file(self):
        """Test initialization without configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_path = Path(tmpdir) / "backup"
            config = Configuration("192.168.1.1", str(backup_path), None)
            assert config.ip_addresses() == ["192.168.1.1"]
            assert config.target_folder() == Path(str(backup_path))

    def test_init_with_config_file(self):
        """Test initialization with configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_data = {
                "ip_addresses": ["192.168.1.1", "192.168.1.2"],
                "target_folder": str(Path(tmpdir) / "backup"),
            }
            with open(config_file, "w") as f:
                yaml.dump(config_data, f)

            config = Configuration(None, None, str(config_file))
            assert config.ip_addresses() == ["192.168.1.1", "192.168.1.2"]

    def test_ip_addresses_from_config_file(self):
        """Test getting IP addresses from configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_data = {"ip_addresses": ["192.168.1.1", "192.168.1.2"]}
            with open(config_file, "w") as f:
                yaml.dump(config_data, f)

            config = Configuration(None, None, str(config_file))
            assert config.ip_addresses() == ["192.168.1.1", "192.168.1.2"]

    def test_ip_addresses_from_ip_address_param(self):
        """Test getting IP addresses from ip_address parameter."""
        config = Configuration("192.168.1.100", None, None)
        assert config.ip_addresses() == ["192.168.1.100"]

    def test_ip_addresses_priority_config_over_param(self):
        """Test that configuration file takes priority over ip_address parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_data = {"ip_addresses": ["192.168.1.1", "192.168.1.2"]}
            with open(config_file, "w") as f:
                yaml.dump(config_data, f)

            config = Configuration("192.168.1.100", None, str(config_file))
            assert config.ip_addresses() == ["192.168.1.1", "192.168.1.2"]

    def test_ip_addresses_none_when_not_specified(self):
        """Test ip_addresses returns None when nothing specified."""
        config = Configuration(None, None, None)
        assert config.ip_addresses() is None

    def test_target_folder_from_config_file(self):
        """Test getting target folder from configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            backup_dir = Path(tmpdir) / "backup"
            config_data = {"target_folder": str(backup_dir)}
            with open(config_file, "w") as f:
                yaml.dump(config_data, f)

            config = Configuration(None, None, str(config_file))
            result = config.target_folder()
            assert result == backup_dir

    def test_target_folder_from_param(self):
        """Test getting target folder from parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = Path(tmpdir) / "my" / "backup"
            config = Configuration(None, str(backup_dir), None)
            result = config.target_folder()
            assert result == backup_dir

    def test_target_folder_priority_config_over_param(self):
        """Test that configuration file takes priority over parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_dir = Path(tmpdir) / "config"
            param_dir = Path(tmpdir) / "param"
            config_data = {"target_folder": str(config_dir)}
            with open(config_file, "w") as f:
                yaml.dump(config_data, f)

            config = Configuration(None, str(param_dir), str(config_file))
            result = config.target_folder()
            assert result == config_dir

    def test_target_folder_default_to_cwd(self):
        """Test target folder defaults to current working directory."""
        config = Configuration(None, None, None)
        result = config.target_folder()
        assert result == Path.cwd()

    def test_database_path(self):
        """Test database path is inside the target folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None)
            assert config.database_path() == Path(tmpdir) / "retentacle.json"

    def test_users_path(self):
        """Test user database path is inside the target folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Configuration(None, tmpdir, None)
            assert config.users_path() == Path(tmpdir) / "retentacle_users.json"

    def test_interval_default(self):
        """Test interval defaults to one hour."""
        config = Configuration(None, None, None)
        assert config.interval() == 3600

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, 0),
            (30, 30),
            ("30", 30),
            ("30s", 30),
            ("5m", 300),
            ("1h", 3600),
            ("1d", 86400),
            ("1.5h", 5400),
            (" 2 H ", 7200),
        ],
    )
    def test_interval_from_param(self, value, expected):
        """Test interval values from the parameter."""
        config = Configuration(None, None, None, interval=value)
        assert config.interval() == expected

    def test_interval_from_config_file(self):
        """Test interval from the configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            with open(config_file, "w") as f:
                yaml.dump({"interval": "15m"}, f)

            config = Configuration(None, None, str(config_file))
            assert config.interval() == 900

    def test_interval_priority_config_over_param(self):
        """Test that the configuration file interval takes priority."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            with open(config_file, "w") as f:
                yaml.dump({"interval": "2h"}, f)

            config = Configuration(None, None, str(config_file), interval="30m")
            assert config.interval() == 7200

    @pytest.mark.parametrize("value", ["", "abc", "5x", "-5", -5, "1.5.5h"])
    def test_interval_invalid(self, value):
        """Test invalid interval values."""
        config = Configuration(None, None, None, interval=value)
        with pytest.raises(ValueError, match="Invalid interval"):
            config.interval()

    @pytest.mark.parametrize("value", [True, []])
    def test_interval_invalid_type(self, value):
        """Test interval values of an unsupported type."""
        config = Configuration(None, None, None, interval=value)
        with pytest.raises(TypeError, match="Invalid interval"):
            config.interval()

    def test_interval_invalid_from_config_file(self):
        """Test invalid interval in the configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            with open(config_file, "w") as f:
                yaml.dump({"interval": "soon"}, f)

            config = Configuration(None, None, str(config_file))
            with pytest.raises(ValueError, match="Invalid interval"):
                config.interval()

    def test_host_default(self):
        """Test host defaults to localhost."""
        config = Configuration(None, None, None)
        assert config.host() == "127.0.0.1"

    def test_host_from_param(self):
        """Test host from the parameter."""
        config = Configuration(None, None, None, host="0.0.0.0")
        assert config.host() == "0.0.0.0"

    def test_host_from_config_file(self):
        """Test host from the configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            with open(config_file, "w") as f:
                yaml.dump({"host": "192.168.1.5"}, f)

            config = Configuration(None, None, str(config_file))
            assert config.host() == "192.168.1.5"

    def test_host_priority_config_over_param(self):
        """Test that the configuration file host takes priority."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            with open(config_file, "w") as f:
                yaml.dump({"host": "192.168.1.5"}, f)

            config = Configuration(None, None, str(config_file), host="0.0.0.0")
            assert config.host() == "192.168.1.5"

    def test_port_default(self):
        """Test port defaults to 8080."""
        config = Configuration(None, None, None)
        assert config.port() == 8080

    @pytest.mark.parametrize("value", [9000, "9000"])
    def test_port_from_param(self, value):
        """Test port from the parameter."""
        config = Configuration(None, None, None, port=value)
        assert config.port() == 9000

    def test_port_from_config_file(self):
        """Test port from the configuration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            with open(config_file, "w") as f:
                yaml.dump({"port": 8888}, f)

            config = Configuration(None, None, str(config_file))
            assert config.port() == 8888

    def test_port_priority_config_over_param(self):
        """Test that the configuration file port takes priority."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            with open(config_file, "w") as f:
                yaml.dump({"port": 8888}, f)

            config = Configuration(None, None, str(config_file), port=9000)
            assert config.port() == 8888

    @pytest.mark.parametrize("value", [0, -1, 65536, "abc"])
    def test_port_invalid(self, value):
        """Test invalid port values."""
        config = Configuration(None, None, None, port=value)
        with pytest.raises(ValueError, match="Invalid port"):
            config.port()
