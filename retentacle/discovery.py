"""mDNS discovery for Shelly devices."""

from collections.abc import Iterable, Iterator
import contextlib
from dataclasses import dataclass
import ipaddress
import logging
import socket
import sys
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from .consts import SHELLY_HTTP_MDNS_SERVICE_TYPE, SHELLY_MDNS_SERVICE_TYPE

_LOGGER = logging.getLogger(__name__)

MDNS_CHECK_SECONDS = 3.0

# mDNS state labels reported by the self-check.
MDNS_WORKING = "working"
MDNS_NO_SHELLY = "no-shelly"
MDNS_SILENT = "silent"
MDNS_UNAVAILABLE = "unavailable"


def local_source_for(target: str) -> str | None:
    """Return the local IPv4 address the OS would use to reach ``target``.

    Connecting a UDP socket never sends a packet, so this only consults the
    routing table — a cheap way to learn which interface (and therefore which
    network segment) a device would be reached through.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.5)
        probe.connect((target, 53))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def segment_report(device_ips: Iterable[str]) -> dict[str, list[str]]:
    """Classify device addresses against the networks reachable from here.

    Used for the passive warning that needs no listening: devices with no route
    at all are definitely unreachable, and devices whose address is not inside
    the /24 of the source address would be reached through a router — mDNS does
    not cross routers, so discovery cannot find them even when unicast works.
    """
    routed: list[str] = []
    off_segment: list[str] = []
    sources: set[str] = set()
    for raw in device_ips:
        try:
            address = ipaddress.ip_address(str(raw).strip())
        except ValueError:
            continue
        if address.version != 4:
            continue
        source = local_source_for(str(address))
        if source is None:
            routed.append(str(address))
            continue
        sources.add(source)
        network = ipaddress.ip_network(f"{source}/24", strict=False)
        if address not in network:
            off_segment.append(str(address))
    return {
        "sources": sorted(sources),
        "unroutable": routed,
        "off_segment": off_segment,
    }


@dataclass(frozen=True)
class MDNSCheck:
    """The result of the built-in mDNS reachability check."""

    state: str
    message: str
    packets: int = 0
    shelly_answers: int = 0
    sources: tuple[str, ...] = ()
    senders: tuple[str, ...] = ()
    error: str | None = None

    @property
    def usable(self) -> bool:
        """Whether discovery is worth trying at all."""
        return self.state in (MDNS_WORKING, MDNS_NO_SHELLY)

    def as_dict(self) -> dict[str, object]:
        """Return the check as a JSON friendly mapping for the web UI."""
        return {
            "state": self.state,
            "message": self.message,
            "usable": self.usable,
            "packets": self.packets,
            "shelly_answers": self.shelly_answers,
            "sources": list(self.sources),
            "senders": list(self.senders),
            "error": self.error,
        }


def _local_sources(device_ips: Iterable[str]) -> list[str]:
    """Return the local addresses this host would send mDNS queries from."""
    sources: list[str] = []
    for candidate in ("8.8.8.8", *device_ips):
        source = local_source_for(str(candidate))
        if source and source not in sources:
            sources.append(source)
    return sources


def _cached_record_count(zc: Zeroconf) -> int:
    """Return how many mDNS records the cache holds right now."""
    return sum(len(records) for records in zc.cache.cache.values())


def _mdns_verdict(
    records: int,
    shelly_answers: int,
    sources: list[str],
    device_ips: Iterable[str],
) -> MDNSCheck:
    """Describe what the observed traffic means for auto-discovery."""
    macos_hint = (
        " On macOS, allow Local Network access for the terminal or service"
        " running Retentacle (System Settings → Privacy & Security)."
        if sys.platform == "darwin"
        else ""
    )
    if shelly_answers:
        return MDNSCheck(
            state=MDNS_WORKING,
            message=(
                f"Auto-discovery works here: {shelly_answers} Shelly service(s) "
                f"answered within the last seconds."
            ),
            packets=records,
            shelly_answers=shelly_answers,
            sources=tuple(sources),
        )
    if records:
        return MDNSCheck(
            state=MDNS_NO_SHELLY,
            message=(
                f"mDNS is audible ({records} record(s) seen), but no Shelly "
                "device answered. They may be switched off, on another VLAN, or "
                "sleeping; devices added by IP address still work normally."
            ),
            packets=records,
            sources=tuple(sources),
        )
    off_segment = segment_report(device_ips)["off_segment"]
    detail = (
        f" Devices such as {off_segment[0]} would be reached through a router."
        if off_segment
        else ""
    )
    return MDNSCheck(
        state=MDNS_SILENT,
        message=(
            f"No mDNS traffic at all.{detail} Auto-discovery will find nothing "
            "from this network, although devices added by IP address keep working"
            f" — run the container with `network_mode: host` or on a macvlan in "
            f"the devices' segment, since mDNS does not cross routers.{macos_hint}"
        ),
        packets=records,
        sources=tuple(sources),
    )


def check_mdns(
    device_ips: Iterable[str] = (), timeout: float = MDNS_CHECK_SECONDS
) -> MDNSCheck:
    """Report whether mDNS auto-discovery can work from this network.

    Uses zeroconf rather than a hand-rolled listener, so the sockets, interfaces
    and multicast membership are exactly the ones a real discovery run would
    use. Listening for a moment distinguishes the cases a user cannot tell
    apart: no mDNS audible at all, mDNS audible but no Shelly answering, and a
    working segment.
    """
    sources = _local_sources(device_ips)
    try:
        zc = Zeroconf()
    except OSError as exc:
        return MDNSCheck(
            state=MDNS_UNAVAILABLE,
            message=(
                f"mDNS sockets could not be opened ({exc}), so auto-discovery "
                "cannot run here."
            ),
            error=str(exc),
            sources=tuple(sources),
        )

    listener = ShellyServiceListener(zc)
    browsers: list[ServiceBrowser] = []
    try:
        browsers = [
            ServiceBrowser(zc, service_type, listener)
            for service_type in (
                SHELLY_MDNS_SERVICE_TYPE,
                SHELLY_HTTP_MDNS_SERVICE_TYPE,
            )
        ]
        before = _cached_record_count(zc)
        time.sleep(timeout)
        records = _cached_record_count(zc) - before
        shelly_answers = sum(1 for device in listener.devices if device.ip_address)
    finally:
        for browser in browsers:
            with contextlib.suppress(OSError, RuntimeError):
                browser.cancel()
        with contextlib.suppress(OSError, RuntimeError):
            zc.close()

    return _mdns_verdict(records, shelly_answers, sources, device_ips)


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

    @property
    def sort_key_text(self) -> str:
        """Return a zero-padded key that sorts correctly as a string."""
        version, value = self.sort_key
        return f"{version}:{value:039d}"


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


def iter_shelly_device_details(
    timeout: float = 60.0,
) -> Iterator[DiscoveredDevice]:
    """Yield Shelly devices as they are discovered on the network via mDNS."""
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
                    yield device
            for browser in browsers:
                known = set(devices)
                _collect_from_browser(zeroconf, browser, devices)
                for ip_address, device in devices.items():
                    if ip_address not in known:
                        yield device
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            zeroconf.close()


def discover_shelly_device_details(timeout: float = 60.0) -> list[DiscoveredDevice]:
    """Discover Shelly devices on the network via mDNS."""
    return sorted(
        iter_shelly_device_details(timeout), key=lambda device: device.sort_key
    )


def discover_shelly_devices(timeout: float = 60.0) -> list[str]:
    """Discover Shelly device IP addresses on the network via mDNS."""
    return [device.ip_address for device in discover_shelly_device_details(timeout)]
