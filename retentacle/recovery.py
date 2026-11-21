"""User-friendly classification of stored backups for manual restores.

Shelly backups mix two kinds of data: the configuration that can be pushed
to a replacement device, and the live state recorded at backup time. This
module separates them, builds a readable summary of the restorable
configuration (with credential values masked), and provides a ready-to-run
curl command for the manual restore.
"""

from collections import Counter
from dataclasses import dataclass, field
import json
import re

_SECRET_PATTERN = re.compile(r"(password|secret|token|psk|key|cert)", re.IGNORECASE)
_SECTION_PATTERN = re.compile(r"([a-z][a-z_]*)(?::(\d+))?")

_SECTION_LABELS = {
    "sys": "System",
    "switch": "Switch",
    "light": "Light",
    "input": "Input",
    "cover": "Cover",
    "fan": "Fan",
    "out": "Output",
    "pm": "Power meter",
    "th": "Temperature & humidity",
    "sensor": "Sensor",
    "binary_sensor": "Binary sensor",
    "enum": "Enum",
    "script": "Script",
    "schedule": "Schedule",
    "timers": "Timers",
    "buttons": "Buttons",
    "serial": "Serial",
    "modbus": "Modbus",
    "wifi": "Wi-Fi",
    "wifi_ap": "Wi-Fi access point",
    "wifi_sta": "Wi-Fi client",
    "eth": "Ethernet",
    "mqtt": "MQTT",
    "cloud": "Cloud",
    "ble": "Bluetooth LE",
    "bthome": "Bthome",
    "knx": "KNX",
    "matter": "Matter",
    "ws": "WebSocket",
    "device": "Device",
    "led": "LED",
    "sntp": "Time sync",
    "discover": "Discovery",
    "coap": "CoAP",
    "auth": "Authentication",
    "switches": "Switches",
    "sensors": "Sensors",
    "system": "System",
    "timezone": "Time zone",
    "onboarding": "Onboarding",
    "pins": "Pins",
    "digits": "Digits",
    "thing": "Thing",
}

# Section names that describe live device state and must never be restored.
_STATE_ONLY_SECTIONS = ("status", "device_info", "actions")


@dataclass(frozen=True)
class ConfigItem:
    """A single leaf value of the restorable configuration."""

    path: str
    label: str
    value: str
    masked: bool = False


@dataclass(frozen=True)
class ConfigGroup:
    """The configuration leaves of one device section."""

    label: str
    items: list[ConfigItem] = field(default_factory=list)


@dataclass(frozen=True)
class RecoveryData:
    """Everything the restore view needs to know about one backup record."""

    kind: str
    kind_label: str
    config: dict
    state: dict
    state_json: str
    groups: list[ConfigGroup]
    command: str
    restore_filename: str

    @property
    def restorable(self) -> bool:
        """Whether a recognizable configuration payload was found."""
        return bool(self.config)


def _section_label(key: object) -> str:
    """Return a human-readable label for a configuration section."""
    name = str(key)
    if name in _SECTION_LABELS:
        return _SECTION_LABELS[name]
    match = _SECTION_PATTERN.fullmatch(name)
    if match is None:
        return name.replace("_", " ").title()
    base, index = match.group(1), match.group(2)
    label = _SECTION_LABELS.get(base, base.replace("_", " ").title())
    return f"{label} {index}" if index else label


def _is_sensitive(path: str, value: object) -> bool:
    """Whether a leaf value looks like a credential worth masking.

    Only non-empty strings under sensitive names are masked — booleans or
    numbers such as ``mqtt.use_client_cert`` are settings, not secrets.
    """
    return (
        isinstance(value, str)
        and bool(value)
        and bool(_SECRET_PATTERN.search(path.rsplit(".", 1)[-1]))
    )


def _display_value(value: object) -> str | None:
    """Return a printable value, or None when the entry can be skipped."""
    if value is None or value in ("", [], {}):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _flatten(prefix: str, node: object) -> list[tuple[str, object]]:
    """Flatten nested dicts into (path, scalar-or-list) leaves."""
    if isinstance(node, dict):
        leaves: list[tuple[str, object]] = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            leaves.extend(_flatten(path, value))
        return leaves
    if not prefix:
        return []
    return [(prefix, node)]


def _groups(config: dict) -> list[ConfigGroup]:
    """Build labelled, masked summary groups from the configuration."""
    groups: list[ConfigGroup] = []
    for key, value in config.items():
        if isinstance(value, dict) and not value:
            continue
        items: list[ConfigItem] = []
        for path, leaf in _flatten(str(key), value):
            display = _display_value(leaf)
            if display is None:
                continue
            masked = _is_sensitive(path, leaf)
            items.append(
                ConfigItem(
                    path=path,
                    label=_section_label(path),
                    value="••••••••" if masked else display,
                    masked=masked,
                )
            )
        if items:
            groups.append(ConfigGroup(label=_section_label(key), items=items))
    return groups


def _state_section(settings: dict) -> dict:
    """Collect the runtime state portions that must not be restored."""
    state: dict = {}
    for key in _STATE_ONLY_SECTIONS:
        value = settings.get(key)
        if value and isinstance(value, (dict, list)):
            state[key] = value
    return state


def recovery_for(record: dict) -> RecoveryData:
    """Classify a stored backup record for the restore view."""
    settings = record.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    ip_address = str(record.get("ip_address") or "device")
    doc_id = record.get("doc_id")
    filename = f"restore-{ip_address}-{doc_id}.json"
    configuration = settings.get("configuration")
    if isinstance(configuration, dict) and configuration:
        kind = "setconfig"
        kind_label = "Generation 2/3 (Shelly.SetConfig)"
        config = configuration
        state = _state_section(settings)
        command = (
            f'curl -sS -X POST "http://{ip_address}/rpc/Shelly.SetConfig"'
            ' -H "Content-Type: application/json"'
            f" --data-binary @{filename}"
        )
    elif isinstance(settings.get("settings"), dict) and settings.get("settings"):
        kind = "settings"
        kind_label = "Generation 1 (POST /settings)"
        config = settings["settings"]
        state = _state_section(settings)
        command = (
            f'curl -sS -X POST "http://{ip_address}/settings" --data-binary @{filename}'
        )
    else:
        kind = "unknown"
        kind_label = "Unrecognized backup format"
        config = {}
        state = {}
        command = ""

    return RecoveryData(
        kind=kind,
        kind_label=kind_label,
        config=config,
        state=state,
        state_json=json.dumps(state, indent=2, sort_keys=True),
        groups=_groups(config),
        command=command,
        restore_filename=filename,
    )


_COMPONENT_NAMES = {
    "switch": ("switch", "switches"),
    "light": ("light", "lights"),
    "input": ("input", "inputs"),
    "cover": ("cover", "covers"),
    "fan": ("fan", "fans"),
    "out": ("output", "outputs"),
    "pm": ("power meter", "power meters"),
    "th": ("climate sensor", "climate sensors"),
    "sensor": ("sensor", "sensors"),
    "binary_sensor": ("binary sensor", "binary sensors"),
    "enum": ("enum", "enums"),
}

# Gen 1 app profiles keep components as plain lists.
_GEN1_LIST_COMPONENTS = {
    "switches": ("switch", "switches"),
    "sensors": ("sensor", "sensors"),
}

_COMPONENT_KEY_PATTERN = re.compile(r"^([a-z][a-z_]*)[:/]\d+$")


def restorable_config(record: dict) -> dict:
    """Return the restorable configuration of a record, if any."""
    settings = record.get("settings")
    if not isinstance(settings, dict):
        return {}
    for key in ("configuration", "settings"):
        value = settings.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _component_counts(config: dict) -> list[str]:
    """Summarize configured components such as '2 switches'."""
    counts = Counter(
        match.group(1)
        for key in config
        if (match := _COMPONENT_KEY_PATTERN.match(str(key)))
    )
    chips = []
    for base, count in sorted(counts.items()):
        singular, plural = _COMPONENT_NAMES.get(base, (base, f"{base}s"))
        chips.append(f"{count} {plural if count > 1 else singular}")
    for key, (singular, plural) in _GEN1_LIST_COMPONENTS.items():
        value = config.get(key)
        if isinstance(value, list) and value:
            label = plural if len(value) > 1 else singular
            chips.append(f"{len(value)} {label}")
    return chips


def _wifi_ssid(config: dict) -> str | None:
    """Return the configured station SSID for either generation."""
    wifi = config.get("wifi")
    if isinstance(wifi, dict) and isinstance(wifi.get("sta"), dict):
        ssid = wifi["sta"].get("ssid")
        if isinstance(ssid, str) and ssid:
            return ssid
    sta = config.get("wifi_sta")
    if isinstance(sta, dict):
        ssid = sta.get("ssid")
        if isinstance(ssid, str) and ssid:
            return ssid
    return None


def backup_highlights(record: dict) -> list[str]:
    """Return short summary chips describing a backup's configuration."""
    config = restorable_config(record)
    if not config:
        return []
    chips: list[str] = []
    ssid = _wifi_ssid(config)
    if ssid:
        chips.append(f"Wi-Fi: {ssid}")
    mqtt = config.get("mqtt")
    if isinstance(mqtt, dict) and mqtt.get("enable"):
        chips.append("MQTT")
    chips.extend(_component_counts(config))
    info = record.get("device_info")
    firmware = info.get("fw") or info.get("ver") if isinstance(info, dict) else None
    if isinstance(firmware, str) and firmware:
        chips.append(f"fw {firmware.split('/')[0]}")
    return chips


@dataclass(frozen=True)
class ConfigChange:
    """A configuration value that differs between two backups."""

    path: str
    old: str
    new: str


def _leaf_value(value: object, path: str) -> str:
    """Return the display value for a diff leaf, masking secrets."""
    if _is_sensitive(path, value):
        return "••••••••"
    display = _display_value(value)
    return "—" if display is None else display


def compare_configs(left: dict, right: dict) -> dict:
    """Diff two configuration payloads by leaf, oldest on the left."""
    leaves_left = dict(_flatten("", left))
    leaves_right = dict(_flatten("", right))
    added = [
        ConfigItem(
            path,
            "",
            _leaf_value(leaves_right[path], path),
            _is_sensitive(path, leaves_right[path]),
        )
        for path in sorted(leaves_right.keys() - leaves_left.keys())
    ]
    removed = [
        ConfigItem(
            path,
            "",
            _leaf_value(leaves_left[path], path),
            _is_sensitive(path, leaves_left[path]),
        )
        for path in sorted(leaves_left.keys() - leaves_right.keys())
    ]
    changed = [
        ConfigChange(
            path,
            _leaf_value(leaves_left[path], path),
            _leaf_value(leaves_right[path], path),
        )
        for path in sorted(leaves_left.keys() & leaves_right.keys())
        if leaves_left[path] != leaves_right[path]
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "identical": not (added or removed or changed),
    }
