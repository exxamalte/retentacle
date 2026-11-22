"""Retentacle CLI tool."""

import argparse
import importlib.metadata
import logging

# Must use absolute imports in this file.
from retentacle.backup import Backup
from retentacle.configuration import Configuration
from retentacle.consts import APPLICATION_NAME
from retentacle.server import run_server


def _create_parser() -> argparse.ArgumentParser:
    """Create the command line argument parser."""
    parser = argparse.ArgumentParser(description=APPLICATION_NAME)
    parser.add_argument(
        "--target-folder",
        dest="target_folder",
        required=False,
        help="Target folder where configuration will be stored",
    )
    parser.add_argument(
        "--configuration",
        dest="configuration_file",
        required=False,
        help="Configuration file",
    )
    parser.add_argument(
        "--discover",
        dest="discover",
        action="store_true",
        required=False,
        help="Auto-discover Shelly devices via mDNS",
    )
    parser.add_argument(
        "--once",
        dest="once",
        action="store_true",
        required=False,
        help="Run a single backup and exit instead of starting the web server",
    )
    parser.add_argument(
        "--host",
        dest="host",
        required=False,
        help="Host the web server should bind to",
    )
    parser.add_argument(
        "--port",
        dest="port",
        required=False,
        type=int,
        help="Port the web server should listen on",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Retentacle CLI tool."""
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.info(
        "%s started (version %s)...",
        APPLICATION_NAME,
        importlib.metadata.version("retentacle"),
    )
    args = _create_parser().parse_args(argv)
    try:
        # Set up configuration. Devices and the backup interval are managed
        # on the web UI Settings page or in the configuration file.
        configuration = Configuration(
            target_folder=args.target_folder,
            configuration_file=args.configuration_file,
            discover=args.discover,
            host=args.host,
            port=args.port,
        )
        if args.once:
            # Execute a single backup (scheduled/cron style run).
            backed_up = Backup(configuration).execute(automatic=True)
            if not backed_up and not configuration.ip_addresses():
                # Nothing to do is a configuration problem for a scripted run.
                logger.warning(
                    "No devices configured: nothing was backed up. List them"
                    " under ip_addresses in the configuration file or run with"
                    " --discover.",
                )
        else:
            # Run the web server and scheduled backups
            run_server(configuration)
    except ValueError as exc:
        logger.error("Invalid configuration: %s", exc)
        raise SystemExit(1) from exc
    except OSError as exc:
        # Fatal: without a usable data folder or port the server cannot keep
        # any state, so leave a non-zero exit status for supervisors.
        logger.error("Unable to run %s: %s", APPLICATION_NAME, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
