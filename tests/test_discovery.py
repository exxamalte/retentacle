"""Tests for mDNS discovery."""

from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from retentacle import consts
from retentacle.consts import SHELLY_HTTP_MDNS_SERVICE_TYPE, SHELLY_MDNS_SERVICE_TYPE
from retentacle.discovery import (
    MDNS_NO_SHELLY,
    MDNS_SILENT,
    MDNS_UNAVAILABLE,
    MDNS_WORKING,
    DiscoveredDevice,
    ShellyServiceListener,
    _collect_from_browser,
    check_mdns,
    discover_shelly_device_details,
    discover_shelly_devices,
    iter_shelly_device_details,
    segment_report,
)


def _service_info(
    *addresses,
    name="ShellyPlus1PM-ABC._shelly._tcp.local.",
    properties=None,
):
    """Create a mocked Zeroconf service info object."""
    info = Mock()
    info.name = name
    info.properties = properties or {}
    info.parsed_addresses.return_value = list(addresses)
    return info


class TestShellyServiceListener:
    """Tests for ShellyServiceListener."""

    def test_init(self):
        """Test listener initialisation."""
        zeroconf = Mock()
        listener = ShellyServiceListener(zeroconf)
        assert listener.zeroconf is zeroconf
        assert listener.devices == []

    @pytest.mark.parametrize(
        ("service_type", "name", "expected"),
        [
            (
                consts.SHELLY_MDNS_SERVICE_TYPE,
                "shellyplus1-abc._shelly._tcp.local.",
                True,
            ),
            (
                consts.SHELLY_HTTP_MDNS_SERVICE_TYPE,
                "shelly1-abc._http._tcp.local.",
                True,
            ),
            (
                consts.SHELLY_HTTP_MDNS_SERVICE_TYPE,
                "SHELLY1-ABC._http._tcp.local.",
                True,
            ),
            (
                consts.SHELLY_HTTP_MDNS_SERVICE_TYPE,
                "printer._http._tcp.local.",
                False,
            ),
            ("_other._tcp.local.", "shelly1-abc._other._tcp.local.", False),
        ],
    )
    def test_is_shelly_service(self, service_type, name, expected):
        """Test identifying Shelly services across service types."""
        listener = ShellyServiceListener(Mock())
        assert listener._is_shelly_service(service_type, name) is expected  # noqa: SLF001

    @pytest.mark.parametrize("method_name", ["add_service", "update_service"])
    def test_add_and_update_service_for_shelly(self, method_name):
        """Test that Shelly services trigger an address lookup."""
        zeroconf = Mock()
        listener = ShellyServiceListener(zeroconf)
        with patch.object(listener, "_get_addresses") as mock_get_addresses:
            getattr(listener, method_name)(
                zeroconf, consts.SHELLY_MDNS_SERVICE_TYPE, "name"
            )
            mock_get_addresses.assert_called_once_with(
                zeroconf, consts.SHELLY_MDNS_SERVICE_TYPE, "name"
            )

    @pytest.mark.parametrize("method_name", ["add_service", "update_service"])
    def test_add_and_update_service_for_non_shelly(self, method_name):
        """Test that unrelated HTTP services are ignored."""
        zeroconf = Mock()
        listener = ShellyServiceListener(zeroconf)
        with patch.object(listener, "_get_addresses") as mock_get_addresses:
            getattr(listener, method_name)(
                zeroconf,
                consts.SHELLY_HTTP_MDNS_SERVICE_TYPE,
                "printer._http._tcp.local.",
            )
            mock_get_addresses.assert_not_called()

    def test_remove_service(self):
        """Test that removing a service leaves discovered devices untouched."""
        listener = ShellyServiceListener(Mock())
        listener.devices = [DiscoveredDevice("192.168.1.10")]
        listener.remove_service(Mock(), consts.SHELLY_MDNS_SERVICE_TYPE, "name")
        assert listener.devices == [DiscoveredDevice("192.168.1.10")]

    def test_get_addresses(self):
        """Test resolving and storing service addresses."""
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = _service_info(
            "192.168.1.10", "192.168.1.11"
        )
        listener = ShellyServiceListener(zeroconf)
        listener._get_addresses(  # noqa: SLF001
            zeroconf, consts.SHELLY_MDNS_SERVICE_TYPE, "name"
        )
        assert [device.ip_address for device in listener.devices] == [
            "192.168.1.10",
            "192.168.1.11",
        ]
        zeroconf.get_service_info.assert_called_once_with(
            consts.SHELLY_MDNS_SERVICE_TYPE, "name", timeout=5000
        )

    def test_get_addresses_collects_device_details(self):
        """Test that device metadata from mDNS is stored."""
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = _service_info(
            "192.168.1.10",
            properties={
                b"gen": b"2",
                b"app": b"Plus1PM",
                b"ver": b"1.4.4",
            },
        )
        listener = ShellyServiceListener(zeroconf)
        listener._get_addresses(  # noqa: SLF001
            zeroconf, consts.SHELLY_MDNS_SERVICE_TYPE, "name"
        )

        assert listener.devices == [
            DiscoveredDevice(
                ip_address="192.168.1.10",
                name="ShellyPlus1PM-ABC",
                model="Plus1PM",
                generation=2,
                version="1.4.4",
            )
        ]

    def test_get_addresses_deduplicates_and_skips_empty(self):
        """Test that duplicate and empty addresses are ignored."""
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = _service_info(
            "", None, "192.168.1.10", "192.168.1.10", "192.168.1.11"
        )
        listener = ShellyServiceListener(zeroconf)
        listener.devices = [DiscoveredDevice("192.168.1.10")]
        listener._get_addresses(  # noqa: SLF001
            zeroconf, consts.SHELLY_MDNS_SERVICE_TYPE, "name"
        )
        assert [device.ip_address for device in listener.devices] == [
            "192.168.1.10",
            "192.168.1.11",
        ]

    def test_get_addresses_without_info(self):
        """Test that unresolved services add no devices."""
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = None
        listener = ShellyServiceListener(zeroconf)
        listener._get_addresses(  # noqa: SLF001
            zeroconf, consts.SHELLY_MDNS_SERVICE_TYPE, "name"
        )
        assert listener.devices == []


class TestCollectFromBrowser:
    """Tests for _collect_from_browser."""

    def test_without_services(self):
        """Test that a browser without services is ignored."""
        zeroconf = Mock()
        devices: dict[str, DiscoveredDevice] = {}
        _collect_from_browser(zeroconf, Mock(services=None), devices)
        assert devices == {}
        zeroconf.get_service_info.assert_not_called()

    def test_with_empty_services(self):
        """Test that a browser with an empty service list is ignored."""
        zeroconf = Mock()
        devices: dict[str, DiscoveredDevice] = {}
        _collect_from_browser(zeroconf, Mock(services={}), devices)
        assert devices == {}
        zeroconf.get_service_info.assert_not_called()

    def test_collects_shelly_services(self):
        """Test collecting addresses from a Shelly service browser."""
        zeroconf = Mock()
        zeroconf.get_service_info.side_effect = [
            _service_info("192.168.1.10"),
            _service_info("192.168.1.11", "192.168.1.10"),
        ]
        browser = SimpleNamespace(
            type=consts.SHELLY_MDNS_SERVICE_TYPE,
            services={"one": object(), "two": object()},
        )
        devices: dict[str, DiscoveredDevice] = {}

        _collect_from_browser(zeroconf, browser, devices)

        assert list(devices) == ["192.168.1.10", "192.168.1.11"]
        assert zeroconf.get_service_info.call_args_list == [
            call(consts.SHELLY_MDNS_SERVICE_TYPE, "one", timeout=3000),
            call(consts.SHELLY_MDNS_SERVICE_TYPE, "two", timeout=3000),
        ]

    def test_skips_existing_addresses(self):
        """Test that already collected addresses are not added again."""
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = _service_info(
            "192.168.1.10", "192.168.1.11"
        )
        browser = SimpleNamespace(
            type=consts.SHELLY_MDNS_SERVICE_TYPE,
            services={"one": object()},
        )
        devices = {"192.168.1.10": DiscoveredDevice("192.168.1.10")}

        _collect_from_browser(zeroconf, browser, devices)

        assert list(devices) == ["192.168.1.10", "192.168.1.11"]

    def test_skips_non_shelly_http_services(self):
        """Test that only HTTP services with a Shelly name are resolved."""
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = _service_info("192.168.1.10")
        browser = SimpleNamespace(
            type=consts.SHELLY_HTTP_MDNS_SERVICE_TYPE,
            services={
                "shelly1-abc._http._tcp.local.": object(),
                "printer._http._tcp.local.": object(),
            },
        )
        devices: dict[str, DiscoveredDevice] = {}

        _collect_from_browser(zeroconf, browser, devices)

        assert list(devices) == ["192.168.1.10"]
        zeroconf.get_service_info.assert_called_once_with(
            consts.SHELLY_HTTP_MDNS_SERVICE_TYPE,
            "shelly1-abc._http._tcp.local.",
            timeout=3000,
        )

    def test_defaults_to_shelly_service_type(self):
        """Test that a browser without a type defaults to the Shelly service type."""
        zeroconf = Mock()
        zeroconf.get_service_info.return_value = _service_info("192.168.1.10")
        browser = SimpleNamespace(services={"anything": object()})
        devices: dict[str, DiscoveredDevice] = {}

        _collect_from_browser(zeroconf, browser, devices)

        assert list(devices) == ["192.168.1.10"]
        zeroconf.get_service_info.assert_called_once_with(
            consts.SHELLY_MDNS_SERVICE_TYPE, "anything", timeout=3000
        )

    def test_ignores_service_errors(self):
        """Test that errors resolving a single service are suppressed."""
        zeroconf = Mock()
        zeroconf.get_service_info.side_effect = OSError("boom")
        browser = SimpleNamespace(
            type=consts.SHELLY_MDNS_SERVICE_TYPE,
            services={"one": object()},
        )
        devices: dict[str, DiscoveredDevice] = {}

        _collect_from_browser(zeroconf, browser, devices)

        assert devices == {}

    def test_ignores_browser_errors(self):
        """Test that errors reading the browser's services are suppressed."""

        class BrokenBrowser:
            """Browser that fails when reading its services."""

            @property
            def services(self):
                """Raise an error when services are accessed."""
                raise RuntimeError("boom")

        devices: dict[str, DiscoveredDevice] = {}

        _collect_from_browser(Mock(), BrokenBrowser(), devices)

        assert devices == {}


class TestDiscoveredDevice:
    """Tests for DiscoveredDevice."""

    def test_details(self):
        """Test the compact metadata summary."""
        device = DiscoveredDevice(
            "192.168.1.10",
            name="ShellyPlus1PM-ABC",
            model="Plus1PM",
            generation=2,
            version="1.4.4",
        )
        assert device.details == "Plus1PM · gen 2 · 1.4.4"

    def test_details_skips_missing_values(self):
        """Test that missing metadata is omitted from the summary."""
        assert DiscoveredDevice("192.168.1.10").details == ""

    def test_sort_key_orders_numerically(self):
        """Test that IP addresses sort numerically and IPv4 comes first."""
        devices = [
            DiscoveredDevice("192.168.1.10"),
            DiscoveredDevice("2001:db8::1"),
            DiscoveredDevice("192.168.1.2"),
        ]

        assert [
            device.ip_address for device in sorted(devices, key=lambda d: d.sort_key)
        ] == [
            "192.168.1.2",
            "192.168.1.10",
            "2001:db8::1",
        ]

    def test_sort_key_text_sorts_as_string(self):
        """Test that the text sort key orders IPv6 addresses correctly as strings."""
        expected = [
            "192.168.1.2",
            "192.168.1.10",
            "2001:db8::1",
            "2001:db8::2",
        ]
        devices = [DiscoveredDevice(address) for address in reversed(expected)]

        assert [
            device.ip_address
            for device in sorted(devices, key=lambda device: device.sort_key_text)
        ] == expected


class TestIterShellyDeviceDetails:
    """Tests for iter_shelly_device_details."""

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_yields_devices_as_they_are_discovered(
        self, mock_zeroconf_class, mock_browser_class, mock_sleep
    ):
        """Test that devices are yielded as soon as they are found."""
        zeroconf = mock_zeroconf_class.return_value
        listeners = []

        def create_browser(_zeroconf, service_type, listener):
            listeners.append(listener)
            browser = Mock()
            browser.type = service_type
            browser.services = {}
            return browser

        mock_browser_class.side_effect = create_browser

        def sleep(_seconds):
            cycle = len(mock_sleep.call_args_list)
            if cycle == 1:
                listeners[0].devices.append(DiscoveredDevice("192.168.1.10"))
            elif cycle == 2:
                listeners[0].devices.extend(
                    [
                        DiscoveredDevice("192.168.1.10"),
                        DiscoveredDevice("192.168.1.11"),
                    ]
                )

        mock_sleep.side_effect = sleep

        generator = iter_shelly_device_details(timeout=3)

        assert next(generator).ip_address == "192.168.1.10"
        assert next(generator).ip_address == "192.168.1.11"
        with pytest.raises(StopIteration):
            next(generator)

        zeroconf.close.assert_called_once()

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_closes_zeroconf_when_consumer_stops(
        self, mock_zeroconf_class, mock_browser_class, mock_sleep
    ):
        """Test that stopping early still closes Zeroconf."""
        zeroconf = mock_zeroconf_class.return_value
        listeners = []

        def create_browser(_zeroconf, service_type, listener):
            listeners.append(listener)
            browser = Mock()
            browser.type = service_type
            browser.services = {}
            return browser

        mock_browser_class.side_effect = create_browser
        mock_sleep.side_effect = lambda _seconds: listeners[0].devices.append(
            DiscoveredDevice("192.168.1.10")
        )

        generator = iter_shelly_device_details(timeout=10)
        assert next(generator).ip_address == "192.168.1.10"
        generator.close()

        zeroconf.close.assert_called_once()


class TestDiscoverShellyDevices:
    """Tests for discover_shelly_devices."""

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_discovery(self, mock_zeroconf_class, mock_browser_class, mock_sleep):
        """Test discovering devices from both Shelly and HTTP service types."""
        zeroconf = mock_zeroconf_class.return_value
        listeners = []

        def create_browser(_zeroconf, service_type, listener):
            listeners.append(listener)
            browser = Mock()
            browser.type = service_type
            browser.services = {}
            return browser

        mock_browser_class.side_effect = create_browser

        def sleep(_seconds):
            if len(mock_sleep.call_args_list) == 1:
                listeners[0].devices.extend(
                    [
                        DiscoveredDevice(
                            "192.168.1.10",
                            name="ShellyPlus1PM-ABC",
                            model="Plus1PM",
                            generation=2,
                            version="1.4.4",
                        ),
                        DiscoveredDevice("192.168.1.10"),
                        DiscoveredDevice("192.168.1.11"),
                        DiscoveredDevice(""),
                    ]
                )

        mock_sleep.side_effect = sleep

        result = discover_shelly_devices(timeout=2)

        assert result == ["192.168.1.10", "192.168.1.11"]
        assert mock_sleep.call_count == 2
        assert mock_browser_class.call_args_list == [
            call(zeroconf, consts.SHELLY_MDNS_SERVICE_TYPE, listeners[0]),
            call(zeroconf, consts.SHELLY_HTTP_MDNS_SERVICE_TYPE, listeners[0]),
        ]
        zeroconf.close.assert_called_once()

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_discovery_collects_from_browsers(
        self, mock_zeroconf_class, mock_browser_class, mock_sleep
    ):
        """Test that both browsers contribute discovered devices."""
        zeroconf = mock_zeroconf_class.return_value
        services = {
            consts.SHELLY_MDNS_SERVICE_TYPE: {"gen2._shelly._tcp.local.": object()},
            consts.SHELLY_HTTP_MDNS_SERVICE_TYPE: {
                "shelly1-abc._http._tcp.local.": object(),
                "printer._http._tcp.local.": object(),
            },
        }

        def create_browser(_zeroconf, service_type, _listener):
            browser = Mock()
            browser.type = service_type
            browser.services = services[service_type]
            return browser

        mock_browser_class.side_effect = create_browser

        def get_service_info(service_type, _name, timeout):  # noqa: ARG001
            if service_type == consts.SHELLY_MDNS_SERVICE_TYPE:
                return _service_info("192.168.1.20")
            return _service_info("192.168.1.3")

        zeroconf.get_service_info.side_effect = get_service_info

        result = discover_shelly_devices(timeout=1)

        assert result == ["192.168.1.3", "192.168.1.20"]
        assert zeroconf.get_service_info.call_args_list == [
            call(
                consts.SHELLY_MDNS_SERVICE_TYPE,
                "gen2._shelly._tcp.local.",
                timeout=3000,
            ),
            call(
                consts.SHELLY_HTTP_MDNS_SERVICE_TYPE,
                "shelly1-abc._http._tcp.local.",
                timeout=3000,
            ),
        ]

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_discovery_returns_device_details(
        self, mock_zeroconf_class, mock_browser_class, mock_sleep
    ):
        """Test that discovery returns device names and metadata."""
        zeroconf = mock_zeroconf_class.return_value
        zeroconf.get_service_info.return_value = _service_info(
            "192.168.1.10",
            name="ShellyPlus1PM-ABC._shelly._tcp.local.",
            properties={
                b"gen": b"2",
                b"app": b"Plus1PM",
                b"ver": b"1.4.4",
            },
        )
        browser = Mock()
        browser.type = consts.SHELLY_MDNS_SERVICE_TYPE
        browser.services = {"ShellyPlus1PM-ABC._shelly._tcp.local.": object()}
        mock_browser_class.return_value = browser

        result = discover_shelly_device_details(timeout=1)

        assert result == [
            DiscoveredDevice(
                ip_address="192.168.1.10",
                name="ShellyPlus1PM-ABC",
                model="Plus1PM",
                generation=2,
                version="1.4.4",
            )
        ]

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_discovery_without_timeout(
        self, mock_zeroconf_class, mock_browser_class, mock_sleep
    ):
        """Test that a zero timeout performs no discovery cycles."""
        zeroconf = mock_zeroconf_class.return_value

        result = discover_shelly_devices(timeout=0)

        assert result == []
        mock_sleep.assert_not_called()
        assert mock_browser_class.call_count == 2
        zeroconf.close.assert_called_once()

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_discovery_closes_zeroconf_on_error(
        self, mock_zeroconf_class, mock_browser_class, mock_sleep
    ):
        """Test that Zeroconf is closed when discovery fails."""
        zeroconf = mock_zeroconf_class.return_value
        mock_sleep.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            discover_shelly_devices(timeout=1)

        zeroconf.close.assert_called_once()

    @patch("retentacle.discovery.time.sleep")
    @patch("retentacle.discovery.ServiceBrowser")
    @patch("retentacle.discovery.Zeroconf")
    def test_discovery_suppresses_close_errors(
        self, mock_zeroconf_class, mock_browser_class, mock_sleep
    ):
        """Test that errors while closing Zeroconf are suppressed."""
        zeroconf = mock_zeroconf_class.return_value
        zeroconf.close.side_effect = OSError("boom")

        result = discover_shelly_devices(timeout=0)

        assert result == []


class TestSegmentReport:
    """Tests for the passive network-segment assessment."""

    def test_devices_reachable_only_through_a_router_are_flagged(self):
        """Test mDNS cannot cross the gateway, so those devices stay hidden."""
        with patch(
            "retentacle.discovery.local_source_for",
            side_effect=lambda target: "172.18.0.5",
        ):
            report = segment_report(["10.10.4.128"])

        assert report["sources"] == ["172.18.0.5"]
        assert report["off_segment"] == ["10.10.4.128"]
        assert report["unroutable"] == []

    def test_devices_on_a_local_subnet_are_not_flagged(self):
        """Test same-segment devices produce no warning."""
        with patch(
            "retentacle.discovery.local_source_for",
            side_effect=lambda target: "10.10.4.30",
        ):
            report = segment_report(["10.10.4.128", "10.10.4.9"])

        assert report["off_segment"] == []

    def test_unroutable_devices_are_reported_separately(self):
        """Test a missing route is distinguished from a routed path."""
        with patch("retentacle.discovery.local_source_for", return_value=None):
            report = segment_report(["192.168.9.9"])

        assert report["unroutable"] == [
            "10.10.4.128".replace("10.10.4.128", "192.168.9.9")
        ]
        assert report["off_segment"] == []

    def test_invalid_and_ipv6_addresses_are_skipped(self):
        """Test junk and IPv6 devices never produce a false warning."""
        with patch("retentacle.discovery.local_source_for", return_value=None):
            report = segment_report(["not-an-ip", "2001:db8::5"])

        assert report == {"sources": [], "unroutable": [], "off_segment": []}


class TestCheckMDNS:
    """Tests for the active mDNS reachability check."""

    def setup_method(self):
        """Create the zeroconf doubles shared by the cases."""
        self.zc = Mock()
        self.zc_factory = Mock(return_value=self.zc)
        self.listener = Mock()
        self.listener.devices = []
        self.browser = Mock()
        self.browser_factory = Mock(return_value=self.browser)

    def _patches(self, *, records=0, shelly=0, socket_error=None, off_segment=()):
        if socket_error is not None:
            self.zc_factory = Mock(side_effect=OSError(socket_error))
        self.listener.devices = [
            DiscoveredDevice(f"10.10.4.{120 + index}") for index in range(shelly)
        ]
        return [
            patch("retentacle.discovery.local_source_for", return_value="172.18.0.5"),
            patch("retentacle.discovery.Zeroconf", self.zc_factory),
            patch(
                "retentacle.discovery.ShellyServiceListener", return_value=self.listener
            ),
            patch("retentacle.discovery.ServiceBrowser", self.browser_factory),
            patch(
                "retentacle.discovery._cached_record_count", side_effect=[0, records]
            ),
            patch("retentacle.discovery.time.sleep"),
            patch(
                "retentacle.discovery.segment_report",
                return_value={
                    "sources": ["172.18.0.5"],
                    "unroutable": [],
                    "off_segment": list(off_segment),
                },
            ),
        ]

    def _run(self, patches, devices=()):
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            return check_mdns(devices, timeout=0)

    def test_shelly_answers_mean_discovery_works(self):
        """Test a reply from a Shelly service is reported as usable."""
        check = self._run(self._patches(records=12, shelly=2), ["10.10.4.128"])

        assert check.state == MDNS_WORKING
        assert check.usable is True
        assert "Auto-discovery works here" in check.message
        assert check.shelly_answers == 2

    def test_it_browses_both_shelly_service_types(self):
        """Test Gen 1 and Gen 2/3 announcements are both queried."""
        self._run(self._patches(shelly=1))

        types = [call.args[1] for call in self.browser_factory.call_args_list]
        assert SHELLY_MDNS_SERVICE_TYPE in types
        assert SHELLY_HTTP_MDNS_SERVICE_TYPE in types

    def test_traffic_without_shelly_answers_is_not_called_broken(self):
        """Test an audible segment without replies keeps devices usable."""
        check = self._run(self._patches(records=17))

        assert check.state == MDNS_NO_SHELLY
        assert check.usable is True
        assert "mDNS is audible" in check.message
        assert "still work normally" in check.message

    def test_silence_is_about_discovery_not_the_devices(self):
        """Test the wording keeps backups out of the message."""
        check = self._run(
            self._patches(records=0, off_segment=["10.10.4.128"]), ["10.10.4.128"]
        )
        message = check.message

        assert check.state == MDNS_SILENT
        assert check.usable is False
        assert message.startswith("No mDNS traffic at all. ")
        assert "at all No" not in message
        assert "would be reached through a router." in message
        assert "Auto-discovery will find nothing" in message
        assert "devices added by IP address keep working" in message
        assert "macvlan" in message

    def test_socket_failure_is_unavailable(self):
        """Test a network that cannot open mDNS sockets is named."""
        check = self._run(self._patches(socket_error="Permission denied"))

        assert check.state == MDNS_UNAVAILABLE
        assert "mDNS sockets could not be opened" in check.message
        assert check.error == "Permission denied"
        self.zc.close.assert_not_called()

    def test_resources_are_released_after_an_error(self):
        """Test browsers and the zeroconf instance are closed even on failure."""
        patches = self._patches()
        patches[4] = patch(
            "retentacle.discovery._cached_record_count",
            side_effect=RuntimeError("boom"),
        )
        with pytest.raises(RuntimeError):
            self._run(patches)

        assert self.browser.cancel.call_count == 2
        self.zc.close.assert_called_once()

    def test_serialises_for_the_browser(self):
        """Test the web endpoint payload carries the fields the UI reads."""
        payload = self._run(self._patches(records=9, shelly=3)).as_dict()

        assert payload["state"] == MDNS_WORKING
        assert payload["usable"] is True
        assert payload["shelly_answers"] == 3
        assert payload["sources"] == ["172.18.0.5"]


class TestMDNSPlatformHint:
    """Tests for platform specific advice in the reachability check."""

    def _silent(self, platform):
        with (
            patch("retentacle.discovery.local_source_for", return_value="10.0.0.5"),
            patch("retentacle.discovery.Zeroconf", Mock()),
            patch(
                "retentacle.discovery.ShellyServiceListener",
                return_value=Mock(devices=[]),
            ),
            patch("retentacle.discovery.ServiceBrowser", return_value=Mock()),
            patch("retentacle.discovery._cached_record_count", side_effect=[0, 0]),
            patch("retentacle.discovery.time.sleep"),
            patch(
                "retentacle.discovery.segment_report",
                return_value={"sources": [], "unroutable": [], "off_segment": []},
            ),
            patch("retentacle.discovery.sys.platform", platform),
        ):
            return check_mdns([], timeout=0)

    def test_macos_advises_local_network_permission(self):
        """Test silence on macOS mentions the privacy gate, not only the network."""
        check = self._silent("darwin")

        assert check.state == MDNS_SILENT
        assert "Local Network" in check.message

    def test_linux_omits_the_macos_hint(self):
        """Test container users are not told about a missing permission."""
        check = self._silent("linux")

        assert "Local Network" not in check.message
