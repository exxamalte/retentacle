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
        "--ip-address",
        dest="ip_address",
        required=False,
        help="IP address of the Shelly device",
    )
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
        "--interval",
        dest="interval",
        required=False,
        help="Interval between automatic backups, e.g. 30m, 1h, or 1d",
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
        # Set up configuration
        configuration = Configuration(
            args.ip_address,
            args.target_folder,
            args.configuration_file,
            args.discover,
            interval=args.interval,
            host=args.host,
            port=args.port,
        )
        if args.once:
            # Execute a single backup
            Backup(configuration).execute()
        else:
            # Run the web server and scheduled backups
            run_server(configuration)
    except ValueError as exc:
        logger.error("Invalid configuration: %s", exc)
    except OSError as exc:
        logger.error("Unable to run %s: %s", APPLICATION_NAME, exc)


if __name__ == "__main__":
    main()
