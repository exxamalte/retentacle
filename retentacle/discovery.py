"""mDNS discovery for Shelly devices."""

from collections.abc import Iterable, Iterator
import contextlib
from dataclasses import dataclass
import ipaddress
import logging
import socket
import struct
import sys
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from .consts import SHELLY_HTTP_MDNS_SERVICE_TYPE, SHELLY_MDNS_SERVICE_TYPE

_LOGGER = logging.getLogger(__name__)

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
MDNS_CHECK_SECONDS = 3.0

# mDNS state labels reported by the self-check.
MDNS_WORKING = "working"
MDNS_NO_SHELLY = "no-shelly"
MDNS_SILENT = "silent"
MDNS_UNAVAILABLE = "unavailable"


def _query_packet(name: str) -> bytes:
    """Build a minimal DNS PTR query for an mDNS service name."""
    header = struct.pack(">HHHHHH", 0, 0x0100, 1, 0, 0, 0)
    labels = b"".join(
        bytes([len(part)]) + part.encode("ascii") for part in name.strip(".").split(".")
    )
    return header + labels + b"\x00" + struct.pack(">HH", 12, 1)


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


def _open_mdns_listener() -> tuple[socket.socket | None, str | None]:
    """Bind a reusable UDP listener on the mDNS port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        listener.bind(("", MDNS_PORT))
    except OSError as exc:
        listener.close()
        return None, str(exc)
    # Joining with INADDR_ANY covers every interface on Linux.
    with contextlib.suppress(OSError):
        listener.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(MDNS_GROUP) + socket.inet_aton("0.0.0.0"),
        )
    with contextlib.suppress(OSError):
        listener.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", 2)
        )
    return listener, None


def _send_queries(
    listener: socket.socket, sources: list[str]
) -> tuple[int, str | None]:
    """Send Shelly mDNS queries out of every candidate interface."""
    queries = [
        _query_packet(SHELLY_MDNS_SERVICE_TYPE),
        _query_packet(SHELLY_HTTP_MDNS_SERVICE_TYPE),
    ]
    sent = 0
    last_error: str | None = None
    for source in sources:
        with contextlib.suppress(OSError):
            listener.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source)
            )
        for query in queries:
            try:
                listener.sendto(query, (MDNS_GROUP, MDNS_PORT))
                sent += 1
            except OSError as exc:
                last_error = str(exc)
    return sent, last_error


def _collect_answers(
    listener: socket.socket, timeout: float
) -> tuple[int, int, set[str]]:
    """Count mDNS packets, Shelly answers and their senders in a time window."""
    listener.settimeout(0.3)
    packets = 0
    shelly_answers = 0
    senders: set[str] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, peer = listener.recvfrom(65535)
        except TimeoutError:
            continue
        except OSError:
            break
        packets += 1
        senders.add(str(peer[0]))
        if b"shelly" in data.lower():
            shelly_answers += 1
    return packets, shelly_answers, senders


def _mdns_verdict(
    packets: int,
    shelly_answers: int,
    senders: set[str],
    sources: list[str],
    device_ips: Iterable[str],
) -> MDNSCheck:
    """Describe what the collected answers mean for discovery."""
    if shelly_answers:
        return MDNSCheck(
            state=MDNS_WORKING,
            message=(
                f"mDNS works: {shelly_answers} Shelly answer(s) from "
                f"{len(senders)} host(s)."
            ),
            packets=packets,
            shelly_answers=shelly_answers,
            sources=tuple(sources),
            senders=tuple(sorted(senders)),
        )
    if packets:
        return MDNSCheck(
            state=MDNS_NO_SHELLY,
            message=(
                f"mDNS traffic is visible ({packets} packets from "
                f"{len(senders)} host(s)), but no Shelly device answered. They "
                "may be switched off, on another VLAN, or sleeping."
            ),
            packets=packets,
            sources=tuple(sources),
            senders=tuple(sorted(senders)),
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
            f"No mDNS traffic at all.{detail} Run the container with "
            "`network_mode: host` or on a macvlan attached to the devices' "
            "segment — discovery cannot cross a router."
        ),
        packets=packets,
        sources=tuple(sources),
    )


def check_mdns(
    device_ips: Iterable[str] = (), timeout: float = MDNS_CHECK_SECONDS
) -> MDNSCheck:
    """Send an mDNS query and report what came back.

    Listening for a couple of seconds distinguishes the three failures that
    users otherwise cannot tell apart: no network for multicast at all, a
    segment where mDNS works but no Shelly answers, and a healthy setup.
    """
    sources: list[str] = []
    for candidate in ("8.8.8.8", *device_ips):
        source = local_source_for(str(candidate))
        if source and source not in sources:
            sources.append(source)

    platform_hint = (
        " On macOS, allow Local Network access for the terminal or service"
        " running Retentacle (System Settings → Privacy & Security)."
        if sys.platform == "darwin"
        else ""
    )
    listener, bind_error = _open_mdns_listener()
    if listener is None:
        return MDNSCheck(
            state=MDNS_UNAVAILABLE,
            message=(
                f"Cannot listen on UDP port {MDNS_PORT} ({bind_error}), so "
                "auto-discovery cannot report anything from here. Another mDNS "
                "responder holds the port; the devices themselves are likely "
                "still reachable by IP address."
            ),
            error=bind_error,
            sources=tuple(sources),
        )

    try:
        sent, send_error = _send_queries(listener, sources)
        if not sent:
            return MDNSCheck(
                state=MDNS_UNAVAILABLE,
                message=(
                    f"Multicast queries could not be sent ({send_error}). This "
                    "network has no route for mDNS, so the devices' segment is "
                    f"not reachable from here.{platform_hint}"
                ),
                error=send_error,
                sources=tuple(sources),
            )
        packets, shelly_answers, senders = _collect_answers(listener, timeout)
    finally:
        listener.close()

    return _mdns_verdict(packets, shelly_answers, senders, sources, device_ips)


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
