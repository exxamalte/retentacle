"""Persistent storage for device backups."""

from datetime import UTC, datetime
import ipaddress
from pathlib import Path
import threading
from typing import Any

from tinydb import TinyDB

from .consts import SETTINGS_FILENAME
from .retention import RetentionPolicy, select_expired_doc_ids

BackupRecord = dict[str, Any]

_SETTINGS_TABLE = "settings"
_SETTINGS_DOC_ID = 1

# A single lock protects all stores. TinyDB does not support concurrent access
# to the same file from multiple instances, and the server reads while the
# scheduler writes.
_LOCK = threading.RLock()


def _document_to_record(document: Any) -> BackupRecord:
    """Convert a TinyDB document into a record including its document ID."""
    record = dict(document)
    doc_id = getattr(document, "doc_id", None)
    if doc_id is not None:
        record["doc_id"] = doc_id
    return record


def _timestamp(record: BackupRecord) -> str:
    """Return a sortable timestamp for a backup record."""
    return str(record.get("timestamp", ""))


def _configured_name(record: BackupRecord) -> str | None:
    """Return the configured device name from a backup record, if present."""
    settings = record.get("settings") or {}
    if not isinstance(settings, dict):
        return None
    # Generation 1 stores the configured name at settings.settings.name.
    generation_1_settings = settings.get("settings")
    if isinstance(generation_1_settings, dict):
        name = generation_1_settings.get("name")
        if name:
            return str(name)
    # Generation 2 and 3 store it at settings.configuration.sys.device.name.
    generation_2_settings = settings.get("configuration")
    if isinstance(generation_2_settings, dict):
        system_settings = generation_2_settings.get("sys")
        if isinstance(system_settings, dict):
            device_settings = system_settings.get("device")
            if isinstance(device_settings, dict):
                name = device_settings.get("name")
                if name:
                    return str(name)
    return None


def device_display_name(record: BackupRecord) -> str:
    """Return a human-readable device name for a backup record."""
    device_info = record.get("device_info") or {}
    if isinstance(device_info, dict):
        name = device_info.get("name")
        if name:
            return str(name)
    name = _configured_name(record)
    if name:
        return name
    if isinstance(device_info, dict):
        for key in ("model", "type", "app"):
            value = device_info.get(key)
            if value:
                return str(value)
    return str(record.get("ip_address") or "Unknown device")


def _ip_sort_key(value: str) -> tuple[int, int, bytes]:
    """Return a sort key that orders IP addresses naturally."""
    value = str(value)
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return (1, 0, value.encode())
    return (0, address.version, address.packed)


class BackupStore:
    """Persistent storage for Shelly device backups and application settings.

    Backups live in the TinyDB database at ``path``; settings live in a
    separate file (``settings_path``, by default ``retentacle_settings.json``
    next to the database) so they can be reset without touching backups.
    """

    def __init__(self, path: Path | str, settings_path: Path | str | None = None):
        """Initialise the store."""
        self._path = Path(path)
        self._settings_path = (
            Path(settings_path)
            if settings_path is not None
            else self._path.with_name(SETTINGS_FILENAME)
        )

    @property
    def path(self) -> Path:
        """Get the path to the backup database."""
        return self._path

    @property
    def settings_path(self) -> Path:
        """Get the path to the settings database."""
        return self._settings_path

    def backup_size(self) -> int:
        """Return the on-disk size of the backup database in bytes."""
        with _LOCK:
            if not self._path.exists():
                return 0
            return self._path.stat().st_size

    def get_settings(self) -> dict[str, Any]:
        """Return persisted application settings."""
        with _LOCK:
            if not self._settings_path.exists():
                return {}
            db = TinyDB(self._settings_path)
            try:
                document = db.table(_SETTINGS_TABLE).get(doc_id=_SETTINGS_DOC_ID)
            finally:
                db.close()
        return dict(document) if document is not None else {}

    def save_settings(self, settings: dict[str, Any]) -> None:
        """Persist application settings, replacing previously saved values."""
        with _LOCK:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            db = TinyDB(self._settings_path)
            try:
                table = db.table(_SETTINGS_TABLE)
                table.truncate()
                table.insert(dict(settings))
            finally:
                db.close()

    def add(self, record: BackupRecord) -> int:
        """Store a backup record and return its document ID."""
        with _LOCK:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            db = TinyDB(self._path)
            try:
                return db.insert(record)
            finally:
                db.close()

    def all(self) -> list[BackupRecord]:
        """Return all backup records, newest first."""
        with _LOCK:
            if not self._path.exists():
                return []
            db = TinyDB(self._path)
            try:
                records = [_document_to_record(document) for document in db.all()]
            finally:
                db.close()
        return sorted(records, key=_timestamp, reverse=True)

    def get(self, doc_id: int) -> BackupRecord | None:
        """Return a single backup record by document ID."""
        with _LOCK:
            if not self._path.exists():
                return None
            db = TinyDB(self._path)
            try:
                document = db.get(doc_id=doc_id)
            finally:
                db.close()
        return _document_to_record(document) if document is not None else None

    def by_device(self, ip_address: str) -> list[BackupRecord]:
        """Return all backups for a device, newest first."""
        return [
            record for record in self.all() if record.get("ip_address") == ip_address
        ]

    def prune(self, ip_address: str, policy: RetentionPolicy) -> int:
        """Remove backups for a device that fall outside the retention policy."""
        if not policy.enabled:
            return 0
        with _LOCK:
            if not self._path.exists():
                return 0
            db = TinyDB(self._path)
            try:
                records = [
                    _document_to_record(document)
                    for document in db.all()
                    if document.get("ip_address") == ip_address
                ]
                expired = select_expired_doc_ids(records, policy)
                if expired:
                    db.remove(doc_ids=expired)
                return len(expired)
            finally:
                db.close()

    def latest_backup_time(self, *, automatic_only: bool = False) -> datetime | None:
        """Return the timestamp of the most recent backup, if any.

        With ``automatic_only`` records explicitly marked as manual backups
        are skipped; older records without a trigger marker are treated as
        automatic.
        """
        for record in self.all():
            if automatic_only and record.get("automatic") is False:
                continue
            value = record.get("timestamp")
            if isinstance(value, datetime):
                timestamp = value
            elif isinstance(value, str):
                try:
                    timestamp = datetime.fromisoformat(value)
                except ValueError:
                    continue
            else:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return timestamp.astimezone(UTC)
        return None

    def devices(self) -> list[dict[str, Any]]:
        """Return a summary of all devices that have backups."""
        summaries: dict[str, dict[str, Any]] = {}
        for record in self.all():
            ip_address = record.get("ip_address")
            if not ip_address:
                continue
            summary = summaries.get(ip_address)
            if summary is None:
                summaries[ip_address] = {
                    "ip_address": ip_address,
                    "display_name": device_display_name(record),
                    "backup_count": 1,
                    "latest": record,
                }
                continue
            summary["backup_count"] += 1
            if _timestamp(record) > _timestamp(summary["latest"]):
                summary["latest"] = record
                summary["display_name"] = device_display_name(record)
        return sorted(
            summaries.values(), key=lambda item: _ip_sort_key(item["ip_address"])
        )
