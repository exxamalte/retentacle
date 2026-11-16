"""Shelly Backup."""

from collections.abc import Callable
from datetime import UTC, datetime
import logging

import httpx

from .configuration import Configuration
from .discovery import discover_shelly_devices
from .retention import retention_policy_from_settings
from .storage import BackupStore

_LOGGER = logging.getLogger(__name__)

StartCallback = Callable[[int], None]
ProgressCallback = Callable[[str, bool], None]


class Backup:
    """Shelly Backup."""

    _configuration: Configuration
    _backup_store: BackupStore

    def __init__(self, configuration: Configuration, store: BackupStore | None = None):
        """Initialise backup."""
        self._configuration = configuration
        self._backup_store = store or BackupStore(configuration.database_path())

    def execute(
        self,
        ip_addresses: list[str] | None = None,
        on_start: StartCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> list[str]:
        """Run device backup for the given or configured devices."""
        if ip_addresses is None:
            ip_addresses = self._configuration.ip_addresses()
            if not ip_addresses and self._configuration.discover():
                _LOGGER.info("Discovering Shelly devices via mDNS...")
                ip_addresses = discover_shelly_devices()
        if not ip_addresses:
            _LOGGER.warning(
                "No IP addresses of Shelly devices specified. Unable to continue."
            )
            if on_start is not None:
                on_start(0)
            return []
        if on_start is not None:
            on_start(len(ip_addresses))
        backed_up: list[str] = []
        for ip_address in ip_addresses:
            success = self.backup_device(ip_address)
            if success:
                backed_up.append(ip_address)
            if on_progress is not None:
                on_progress(ip_address, success)
        return backed_up

    def backup_device(self, ip_address: str) -> bool:
        """Back up a single device and return whether it succeeded."""
        try:
            return self._device_backup(ip_address)
        except Exception:  # noqa: BLE001
            # A single failing device must not stop the remaining backups.
            _LOGGER.exception("Unexpected error backing up %s", ip_address)
            return False

    def _device_backup(self, ip_address: str) -> bool:
        """Create device backup from Shelly under provided IP address."""
        try:
            # Get some basic information from the device to determine which generation
            url: str = f"http://{ip_address}/shelly"
            response = httpx.get(url)
            if response.status_code == httpx.codes.OK:
                response_json = response.json()
                _LOGGER.debug(response_json)
                # Check for 'gen' tag 2 or 3 (with 1 being the default)
                shelly_gen = response_json.get("gen", 1)
                _LOGGER.info("Device identified as generation %s", shelly_gen)
                match shelly_gen:
                    case 1:
                        return self._generation_1(ip_address, response_json)
                    case 2 | 3:
                        return self._generation_2(ip_address, response_json)
                    case _:
                        # Unknown generation.
                        _LOGGER.warning("Unknown generation: %s", shelly_gen)
                        return False
            _LOGGER.warning(
                "Unable to get basic device information from %s, response code %s",
                url,
                response.status_code,
            )
            return False
        except httpx.HTTPError as exc:
            _LOGGER.warning("HTTP Exception for %s - %s", exc.request.url, exc)
            return False
        except ValueError as exc:
            _LOGGER.warning("Invalid response from %s - %s", ip_address, exc)
            return False

    def _generation_1(self, ip_address: str, basic_device_information: dict) -> bool:
        """Backup generation 1 device."""
        urls: dict = {
            "settings": f"http://{ip_address}/settings",
            "actions": f"http://{ip_address}/settings/actions",
            "status": f"http://{ip_address}/status",
        }
        configuration: dict = self._device_configuration(urls)
        return self._store(ip_address, configuration, basic_device_information)

    def _generation_2(self, ip_address: str, basic_device_information: dict) -> bool:
        """Backup generation 2 device."""
        urls: dict = {
            "configuration": f"http://{ip_address}/rpc/Shelly.GetConfig",
            "device_info": f"http://{ip_address}/rpc/Shelly.GetDeviceInfo",
            "status": f"http://{ip_address}/rpc/Shelly.GetStatus",
        }
        configuration: dict = self._device_configuration(urls)
        return self._store(ip_address, configuration, basic_device_information)

    def _device_configuration(self, urls: dict) -> dict:
        """Fetch device configuration from the provided list of URLs."""
        result: dict = {}
        for information_type, url in urls.items():
            response = httpx.get(url)
            if response.status_code == httpx.codes.OK:
                response_json = response.json()
                result[information_type] = response_json
            else:
                _LOGGER.warning(
                    "Unable to get device settings from %s, response code %s",
                    url,
                    response.status_code,
                )
        return result

    def _store(
        self,
        ip_address: str,
        device_settings: dict,
        device_info: dict | None = None,
    ) -> bool:
        """Store device settings and return whether storing succeeded."""
        device_info = device_info or {}
        record = {
            "ip_address": ip_address,
            "timestamp": datetime.now(UTC).isoformat(),
            "generation": device_info.get("gen", 1),
            "device_info": device_info,
            "settings": device_settings,
        }
        try:
            self._backup_store.add(record)
        except OSError as exc:
            _LOGGER.warning("Unable to store backup for %s - %s", ip_address, exc)
            return False
        self._apply_retention(ip_address)
        _LOGGER.info(
            "Configuration stored for %s: %s", ip_address, self._backup_store.path
        )
        return True

    def _apply_retention(self, ip_address: str) -> None:
        """Apply the configured retention policy to a device's backups."""
        try:
            policy = retention_policy_from_settings(self._backup_store.get_settings())
            removed = self._backup_store.prune(ip_address, policy)
        except Exception:  # noqa: BLE001
            # Retention must never make a successful backup fail.
            _LOGGER.exception("Unable to apply backup retention for %s", ip_address)
            return
        if isinstance(removed, int) and removed:
            _LOGGER.info("Removed %s old backups for %s", removed, ip_address)
