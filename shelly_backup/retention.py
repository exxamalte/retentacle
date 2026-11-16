"""Backup retention policy helpers."""

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

RETENTION_FIELDS = ("monthly", "weekly", "daily", "hourly")


@dataclass(frozen=True)
class RetentionPolicy:
    """Number of backups to keep for each retention period."""

    monthly: int = 12
    weekly: int = 4
    daily: int = 7
    hourly: int = 24

    @property
    def enabled(self) -> bool:
        """Whether the policy keeps any backups."""
        return any(getattr(self, field) > 0 for field in RETENTION_FIELDS)

    def as_dict(self) -> dict[str, int]:
        """Return the policy as a dictionary for persistence and display."""
        return {field: getattr(self, field) for field in RETENTION_FIELDS}


DEFAULT_RETENTION = RetentionPolicy()


def retention_policy_from_settings(settings: object) -> RetentionPolicy:
    """Return the retention policy stored in application settings."""
    if not isinstance(settings, dict):
        return DEFAULT_RETENTION
    raw_policy = settings.get("retention")
    if not isinstance(raw_policy, dict):
        return DEFAULT_RETENTION

    values: dict[str, int] = {}
    for field in RETENTION_FIELDS:
        default = getattr(DEFAULT_RETENTION, field)
        value = raw_policy.get(field)
        if isinstance(value, bool):
            _LOGGER.warning("Ignoring invalid retention value for %s: %r", field, value)
            values[field] = default
            continue
        try:
            parsed = int(value)
        except TypeError, ValueError:
            _LOGGER.warning("Ignoring invalid retention value for %s: %r", field, value)
            values[field] = default
            continue
        if parsed < 0:
            _LOGGER.warning(
                "Ignoring negative retention value for %s: %r", field, value
            )
            values[field] = default
            continue
        values[field] = parsed
    return RetentionPolicy(**values)


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    """Return a backup record's timestamp in UTC, if valid."""
    value = record.get("timestamp")
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _bucket(timestamp: datetime, period: str) -> str:
    """Return the retention bucket key for a timestamp and period."""
    if period == "monthly":
        return timestamp.strftime("%Y-%m")
    if period == "weekly":
        return timestamp.strftime("%G-W%V")
    if period == "daily":
        return timestamp.strftime("%Y-%m-%d")
    return timestamp.strftime("%Y-%m-%dT%H")


def _keep_record(
    timestamp: datetime,
    policy: RetentionPolicy,
    kept_buckets: dict[str, set[str]],
) -> bool:
    """Mark a record's periods as kept and return whether it should be kept."""
    keep = False
    for field in RETENTION_FIELDS:
        limit = getattr(policy, field)
        if limit <= 0:
            continue
        buckets = kept_buckets[field]
        bucket = _bucket(timestamp, field)
        if bucket in buckets or len(buckets) >= limit:
            continue
        buckets.add(bucket)
        keep = True
    return keep


def select_expired_doc_ids(
    records: list[dict[str, Any]], policy: RetentionPolicy
) -> list[int]:
    """Return document IDs that fall outside the retention policy.

    The newest backup in each period is kept. A backup can count towards
    several periods, for example as both the newest daily and weekly backup.
    Records with invalid timestamps are kept to avoid accidental data loss.
    """
    if not policy.enabled:
        return []

    timestamped: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        timestamp = _record_timestamp(record)
        if timestamp is not None:
            timestamped.append((timestamp, record))
    timestamped.sort(key=lambda item: item[0], reverse=True)

    kept_buckets: dict[str, set[str]] = {field: set() for field in RETENTION_FIELDS}
    kept_doc_ids: set[int] = set()
    for timestamp, record in timestamped:
        if _keep_record(timestamp, policy, kept_buckets):
            doc_id = record.get("doc_id")
            if isinstance(doc_id, int):
                kept_doc_ids.add(doc_id)

    expired: list[int] = []
    for _, record in timestamped:
        doc_id = record.get("doc_id")
        if isinstance(doc_id, int) and doc_id not in kept_doc_ids:
            expired.append(doc_id)
    return expired
