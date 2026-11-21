"""User-friendly classification of stored backups for manual restores.

Shelly backups mix two kinds of data: the configuration that can be pushed
to a replacement device, and the live state recorded at backup time. This
module separates them, builds a readable summary of the restorable
configuration (with credential values masked), and provides a ready-to-run
curl command for the manual restore.
"""

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


def _is_secret(path: str) -> bool:
    """Whether the last path segment looks like a credential."""
    return bool(_SECRET_PATTERN.search(path.rsplit(".", 1)[-1]))


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
            masked = _is_secret(path)
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
