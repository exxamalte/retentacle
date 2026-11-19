"""Tests for CLI module."""

import runpy
from unittest.mock import Mock, patch

import pytest

from retentacle import cli
from retentacle.cli import _create_parser


class TestCLI:
    """Test CLI module."""

    @patch("retentacle.cli.run_server")
    @patch("retentacle.cli.Backup")
    @patch("retentacle.cli.Configuration")
    def test_main_starts_server_by_default(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test that the server starts when no mode is selected."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main([])

        mock_configuration.assert_called_once_with(
            target_folder=None,
            configuration_file=None,
            discover=False,
            host=None,
            port=None,
        )
        mock_run_server.assert_called_once_with(configuration)
        mock_backup.assert_not_called()

    @patch("retentacle.cli.run_server")
    @patch("retentacle.cli.Backup")
    @patch("retentacle.cli.Configuration")
    def test_main_once_runs_single_backup(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test that --once runs a single backup and exits."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main(["--once", "--target-folder", "backups"])

        mock_configuration.assert_called_once_with(
            target_folder="backups",
            configuration_file=None,
            discover=False,
            host=None,
            port=None,
        )
        mock_backup.assert_called_once_with(configuration)
        mock_backup.return_value.execute.assert_called_once_with()
        mock_run_server.assert_not_called()

    @patch("retentacle.cli.run_server")
    @patch("retentacle.cli.Backup")
    @patch("retentacle.cli.Configuration")
    def test_main_passes_server_options(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test that host and port are passed to the configuration."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main(["--host", "0.0.0.0", "--port", "9000"])

        mock_configuration.assert_called_once_with(
            target_folder=None,
            configuration_file=None,
            discover=False,
            host="0.0.0.0",
            port=9000,
        )
        mock_run_server.assert_called_once_with(configuration)
        mock_backup.assert_not_called()

    @patch("retentacle.cli.run_server")
    @patch("retentacle.cli.Backup")
    @patch("retentacle.cli.Configuration")
    def test_main_with_configuration_file(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function with configuration file."""
        configuration = Mock()
        mock_configuration.return_value = configuration

        cli.main(["--once", "--configuration", "config.yaml"])

        mock_configuration.assert_called_once_with(
            target_folder=None,
            configuration_file="config.yaml",
            discover=False,
            host=None,
            port=None,
        )
        mock_backup.return_value.execute.assert_called_once_with()
        mock_run_server.assert_not_called()

    @patch("retentacle.cli.run_server")
    @patch("retentacle.cli.Backup")
    @patch("retentacle.cli.Configuration")
    def test_main_os_error_handling(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function handles OSError."""
        mock_configuration.side_effect = OSError("Permission denied")

        # Should not raise exception
        cli.main(["--once"])

        mock_backup.assert_not_called()
        mock_run_server.assert_not_called()

    @patch("retentacle.cli.run_server")
    @patch("retentacle.cli.Backup")
    @patch("retentacle.cli.Configuration")
    def test_main_value_error_handling(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function handles invalid configuration values."""
        mock_configuration.side_effect = ValueError("Invalid configuration")

        # Should not raise exception
        cli.main(["--once"])

        mock_backup.assert_not_called()
        mock_run_server.assert_not_called()

    @patch("retentacle.cli.run_server")
    @patch("retentacle.cli.Backup")
    @patch("retentacle.cli.Configuration")
    def test_main_handles_server_error(
        self, mock_configuration, mock_backup, mock_run_server
    ):
        """Test main function handles a server startup error."""
        mock_run_server.side_effect = OSError("Address already in use")

        # Should not raise exception
        cli.main([])

        mock_backup.assert_not_called()

    def test_web_configured_options_are_rejected(self):
        """Test that flags managed via the web UI were removed."""
        parser = _create_parser()
        for option in ("--ip-address", "--interval"):
            with pytest.raises(SystemExit):
                parser.parse_args([option, "example"])

    @patch("retentacle.cli.main")
    def test_python_m_entry_point(self, mock_main):
        """Test that python -m retentacle calls the CLI."""
        runpy.run_module("retentacle", run_name="__main__")

        mock_main.assert_called_once_with()
