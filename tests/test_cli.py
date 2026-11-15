"""Tests for CLI module."""

import runpy
from unittest.mock import Mock, patch

from shelly_backup import cli


class TestCLI:
    """Test CLI module."""

    @patch("shelly_backup.cli.run_server")
    @patch("shelly_backup.cli.Backup")
    @patch("shelly_backup.cli.Configuration")
    def test_main_starts_server_by_default(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test that the server starts when no mode is selected."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main([])

        mock_configuration.assert_called_once_with(
            None, None, None, False, interval=None, host=None, port=None
        )
        mock_run_server.assert_called_once_with(configuration)
        mock_backup.assert_not_called()

    @patch("shelly_backup.cli.run_server")
    @patch("shelly_backup.cli.Backup")
    @patch("shelly_backup.cli.Configuration")
    def test_main_once_runs_single_backup(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test that --once runs a single backup and exits."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main(["--once", "--ip-address", "192.168.1.1"])

        mock_configuration.assert_called_once_with(
            "192.168.1.1", None, None, False, interval=None, host=None, port=None
        )
        mock_backup.assert_called_once_with(configuration)
        mock_backup.return_value.execute.assert_called_once_with()
        mock_run_server.assert_not_called()

    @patch("shelly_backup.cli.run_server")
    @patch("shelly_backup.cli.Backup")
    @patch("shelly_backup.cli.Configuration")
    def test_main_passes_server_options(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test that interval, host, and port are passed to the configuration."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main(["--interval", "30m", "--host", "0.0.0.0", "--port", "9000"])

        mock_configuration.assert_called_once_with(
            None,
            None,
            None,
            False,
            interval="30m",
            host="0.0.0.0",
            port=9000,
        )
        mock_run_server.assert_called_once_with(configuration)
        mock_backup.assert_not_called()

    @patch("shelly_backup.cli.run_server")
    @patch("shelly_backup.cli.Backup")
    @patch("shelly_backup.cli.Configuration")
    def test_main_with_configuration_file(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function with configuration file."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main(["--once", "--configuration", "config.yaml"])

        mock_configuration.assert_called_once_with(
            None, None, "config.yaml", False, interval=None, host=None, port=None
        )
        mock_backup.return_value.execute.assert_called_once_with()
        mock_run_server.assert_not_called()

    @patch("shelly_backup.cli.run_server")
    @patch("shelly_backup.cli.Backup")
    @patch("shelly_backup.cli.Configuration")
    def test_main_os_error_handling(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function handles OSError."""
        mock_configuration.side_effect = OSError("Permission denied")

        # Should not raise exception
        cli.main(["--once"])

        mock_backup.assert_not_called()
        mock_run_server.assert_not_called()

    @patch("shelly_backup.cli.run_server")
    @patch("shelly_backup.cli.Backup")
    @patch("shelly_backup.cli.Configuration")
    def test_main_value_error_handling(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function handles invalid configuration values."""
        mock_configuration.side_effect = ValueError("Invalid interval")

        # Should not raise exception
        cli.main(["--once", "--interval", "nonsense"])

        mock_backup.assert_not_called()
        mock_run_server.assert_not_called()

    @patch("shelly_backup.cli.run_server")
    @patch("shelly_backup.cli.Backup")
    @patch("shelly_backup.cli.Configuration")
    def test_main_handles_server_error(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function handles a server startup error."""
        mock_run_server.side_effect = OSError("Address already in use")

        # Should not raise exception
        cli.main([])

        mock_backup.assert_not_called()

    @patch("shelly_backup.cli.main")
    def test_python_m_entry_point(self, mock_main):
        """Test that python -m shelly_backup calls the CLI."""
        runpy.run_module("shelly_backup", run_name="__main__")

        mock_main.assert_called_once_with()
