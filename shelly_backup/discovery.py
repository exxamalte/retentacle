"""mDNS discovery for Shelly devices."""

import contextlib
from dataclasses import dataclass
import ipaddress
import logging
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from .consts import SHELLY_HTTP_MDNS_SERVICE_TYPE, SHELLY_MDNS_SERVICE_TYPE

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredDevice:
    """Details about a Shelly device found via mDNS."""

    ip_address: str
    name: str = ""
    model: str = ""
    generation: int | None = None
    version: str = ""

    @property
    def details(self) -> str:
        """Return a compact summary of the device metadata."""
        parts = [self.model]
        if self.generation is not None:
            parts.append(f"gen {self.generation}")
        if self.version:
            parts.append(self.version)
        return " · ".join(part for part in parts if part)

    @property
    def sort_key(self) -> tuple[int, int]:
        """Return a key that sorts devices numerically by IP address."""
        try:
            address = ipaddress.ip_address(self.ip_address)
        except ValueError:
            return (0, 0)
        return (address.version, int(address))


def _decode(value: object) -> str:
    """Decode an mDNS text property value."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _service_properties(info) -> dict[str, str]:
    """Return the decoded TXT properties of a service."""
    properties = getattr(info, "properties", None) or {}
    return {_decode(key).lower(): _decode(value) for key, value in properties.items()}


def _first_property(properties: dict[str, str], *keys: str) -> str:
    """Return the first non-empty value for the given property keys."""
    for key in keys:
        value = properties.get(key)
        if value:
            return value
    return ""


def _parse_generation(value: str) -> int | None:
    """Parse a device generation from a TXT property."""
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _service_display_name(name: str, service_type: str) -> str:
    """Return a service instance name without its mDNS service suffix."""
    suffix = f".{service_type}"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return name.split(".", 1)[0]


def _devices_from_service_info(
    info, service_type: str, service_name: str
) -> list[DiscoveredDevice]:
    """Build discovered device details from a resolved mDNS service."""
    properties = _service_properties(info)
    name = _service_display_name(
        getattr(info, "name", "") or service_name, service_type
    )
    model = _first_property(properties, "model", "app", "product", "type")
    if not model and name:
        model = name.split("-", 1)[0]
    version = _first_property(properties, "ver", "version", "fw_ver", "fw")
    generation = _parse_generation(properties.get("gen", ""))
    return [
        DiscoveredDevice(
            ip_address=address,
            name=name,
            model=model,
            generation=generation,
            version=version,
        )
        for address in info.parsed_addresses()
        if address
    ]


class ShellyServiceListener(ServiceListener):
    """Listener for Shelly mDNS services."""

    def __init__(self, zc: Zeroconf):
        """Initialise listener."""
        self.zeroconf = zc
        self.devices: list[DiscoveredDevice] = []

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Handle service addition."""
        if self._is_shelly_service(type_, name):
            self._get_addresses(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Handle service update."""
        if self._is_shelly_service(type_, name):
            self._get_addresses(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Handle service removal."""

    def _is_shelly_service(self, type_: str, name: str) -> bool:
        """Check if the service belongs to a Shelly device."""
        if type_ == SHELLY_MDNS_SERVICE_TYPE:
            return True
        if type_ == SHELLY_HTTP_MDNS_SERVICE_TYPE:
            return "shelly" in name.lower()
        return False

    def _get_addresses(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Get device details for a service."""
        info = zc.get_service_info(type_, name, timeout=5000)
        if info:
            known = {device.ip_address for device in self.devices}
            for device in _devices_from_service_info(info, type_, name):
                if device.ip_address not in known:
                    self.devices.append(device)
                    known.add(device.ip_address)
                    _LOGGER.info("Discovered Shelly device at %s", device.ip_address)


def _collect_from_browser(
    zeroconf, browser, devices: dict[str, DiscoveredDevice]
) -> None:
    try:
        services = getattr(browser, "services", None)
        if not services:
            return
        service_type = getattr(browser, "type", SHELLY_MDNS_SERVICE_TYPE)
        for service_name in list(services.keys()):
            if (
                service_type == SHELLY_HTTP_MDNS_SERVICE_TYPE
                and "shelly" not in service_name.lower()
            ):
                continue
            with contextlib.suppress(OSError, RuntimeError):
                info = zeroconf.get_service_info(
                    service_type, service_name, timeout=3000
                )
                if info:
                    for device in _devices_from_service_info(
                        info, service_type, service_name
                    ):
                        if device.ip_address not in devices:
                            devices[device.ip_address] = device
                            _LOGGER.info(
                                "Discovered Shelly device at %s", device.ip_address
                            )
    except OSError, RuntimeError:
        pass


def discover_shelly_device_details(timeout: float = 60.0) -> list[DiscoveredDevice]:
    """Discover Shelly devices on the network via mDNS."""
    devices: dict[str, DiscoveredDevice] = {}

    zeroconf = Zeroconf()
    listener = ShellyServiceListener(zeroconf)
    browsers = []
    try:
        browsers.append(ServiceBrowser(zeroconf, SHELLY_MDNS_SERVICE_TYPE, listener))
        browsers.append(
            ServiceBrowser(zeroconf, SHELLY_HTTP_MDNS_SERVICE_TYPE, listener)
        )
        for _ in range(int(timeout)):
            time.sleep(1)
            for device in listener.devices:
                if device.ip_address and device.ip_address not in devices:
                    devices[device.ip_address] = device
            for browser in browsers:
                _collect_from_browser(zeroconf, browser, devices)
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            zeroconf.close()

    return sorted(devices.values(), key=lambda device: device.sort_key)


def discover_shelly_devices(timeout: float = 60.0) -> list[str]:
    """Discover Shelly device IP addresses on the network via mDNS."""
    return [device.ip_address for device in discover_shelly_device_details(timeout)]
