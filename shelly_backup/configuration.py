"""Configuration."""

from pathlib import Path
import re

import yaml

from .consts import DATABASE_FILENAME, DEFAULT_HOST, DEFAULT_INTERVAL, DEFAULT_PORT

_INTERVAL_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$")
_INTERVAL_FACTORS = {
    "": 1,
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}


def parse_interval(value: float | str) -> int:
    """Parse an interval value into seconds."""
    if isinstance(value, bool):
        raise TypeError(f"Invalid interval: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        match = _INTERVAL_PATTERN.fullmatch(value.strip().lower())
        if not match:
            raise ValueError(f"Invalid interval: {value!r}")
        seconds = float(match.group(1)) * _INTERVAL_FACTORS[match.group(2)]
    else:
        raise TypeError(f"Invalid interval: {value!r}")
    if seconds < 0:
        raise ValueError(f"Invalid interval: {value!r}")
    return int(seconds)


class Configuration:
    """Configuration."""

    def __init__(
        self,
        ip_address: str | None = None,
        target_folder: str | None = None,
        configuration_file: str | None = None,
        discover: bool = False,
        *,
        interval: float | str | None = None,
        host: str | None = None,
        port: int | str | None = None,
    ):
        """Initialise backup."""
        self._configuration: dict | None = None
        if configuration_file:
            with open(configuration_file) as f:
                self._configuration = yaml.load(f, Loader=yaml.FullLoader)
        self._ip_address = ip_address
        self._target_folder = target_folder
        self._discover = discover
        self._interval = interval
        self._host = host
        self._port = port

    def ip_addresses(self) -> list[str] | None:
        """Get IP addresses from configuration."""
        if self._configuration and "ip_addresses" in self._configuration:
            return self._configuration["ip_addresses"]
        if self._ip_address:
            return [self._ip_address]
        return None

    def target_folder(self) -> Path:
        """Get the target folder from configuration."""
        target_folder: str | None = None
        if self._configuration and "target_folder" in self._configuration:
            target_folder = self._configuration["target_folder"]
        elif self._target_folder:
            target_folder = self._target_folder
        if target_folder:
            # Create folder structure if it doesn't exist.
            Path(target_folder).mkdir(parents=True, exist_ok=True)
            return Path(target_folder)
        Path.cwd().mkdir(parents=True, exist_ok=True)
        return Path.cwd()

    def database_path(self) -> Path:
        """Get the path to the backup database."""
        return self.target_folder() / DATABASE_FILENAME

    def discover(self) -> bool:
        """Whether to discover devices via mDNS."""
        if self._configuration and "discover" in self._configuration:
            return bool(self._configuration["discover"])
        return bool(self._discover)

    def interval(self) -> int:
        """Get the interval between automatic backups in seconds."""
        if self._configuration and "interval" in self._configuration:
            return parse_interval(self._configuration["interval"])
        if self._interval is not None:
            return parse_interval(self._interval)
        return DEFAULT_INTERVAL

    def host(self) -> str:
        """Get the host the web server should bind to."""
        if self._configuration and "host" in self._configuration:
            host = self._configuration["host"]
            if host:
                return str(host)
        if self._host:
            return self._host
        return DEFAULT_HOST

    def port(self) -> int:
        """Get the port the web server should listen on."""
        port: int | str | None = None
        if self._configuration and "port" in self._configuration:
            port = self._configuration["port"]
        elif self._port is not None:
            port = self._port
        if port is None:
            return DEFAULT_PORT
        try:
            port_number = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid port: {port!r}") from exc
        if not 1 <= port_number <= 65535:
            raise ValueError(f"Invalid port: {port!r}")
        return port_number
