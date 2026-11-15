"""Persistent storage for device backups."""

import ipaddress
from pathlib import Path
import threading
from typing import Any

from tinydb import TinyDB

BackupRecord = dict[str, Any]

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


def device_display_name(record: BackupRecord) -> str:
    """Return a human-readable device name for a backup record."""
    device_info = record.get("device_info") or {}
    for key in ("name", "model", "type", "app"):
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
    """Persistent storage for Shelly device backups."""

    def __init__(self, path: Path | str):
        """Initialise the store."""
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """Get the path to the TinyDB database."""
        return self._path

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
