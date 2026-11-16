"""Tests for backup retention policies."""

from datetime import UTC, datetime

from shelly_backup.retention import (
    DEFAULT_RETENTION,
    RetentionPolicy,
    retention_policy_from_settings,
    select_expired_doc_ids,
)


def _record(doc_id, timestamp):
    """Create a minimal backup record for retention tests."""
    return {"doc_id": doc_id, "timestamp": timestamp}


def _policy(**overrides):
    """Create a policy that only enables the requested retention periods."""
    values = {"monthly": 0, "weekly": 0, "daily": 0, "hourly": 0}
    values.update(overrides)
    return RetentionPolicy(**values)


class TestRetentionPolicy:
    """Tests for retention policy defaults and settings parsing."""

    def test_defaults_keep_a_year_of_history(self):
        """Test that the default policy keeps sensible amounts of history."""
        assert (
            RetentionPolicy(monthly=12, weekly=4, daily=7, hourly=24)
            == DEFAULT_RETENTION
        )
        assert DEFAULT_RETENTION.enabled is True

    def test_disabled_policy(self):
        """Test that a policy with all periods at zero is disabled."""
        policy = _policy()

        assert policy.enabled is False

    def test_as_dict(self):
        """Test serialising a policy for persistence."""
        assert DEFAULT_RETENTION.as_dict() == {
            "monthly": 12,
            "weekly": 4,
            "daily": 7,
            "hourly": 24,
        }

    def test_policy_from_missing_settings(self):
        """Test that missing settings use the default policy."""
        assert retention_policy_from_settings(None) == DEFAULT_RETENTION
        assert retention_policy_from_settings({}) == DEFAULT_RETENTION
        assert (
            retention_policy_from_settings({"retention": "nonsense"})
            == DEFAULT_RETENTION
        )

    def test_policy_from_saved_settings(self):
        """Test that a saved retention policy is used."""
        policy = retention_policy_from_settings(
            {
                "retention": {
                    "monthly": 6,
                    "weekly": 3,
                    "daily": 5,
                    "hourly": 2,
                }
            }
        )

        assert policy == RetentionPolicy(monthly=6, weekly=3, daily=5, hourly=2)

    def test_policy_from_saved_settings_uses_defaults_for_invalid_values(self):
        """Test that invalid saved values fall back to the defaults."""
        policy = retention_policy_from_settings(
            {
                "retention": {
                    "monthly": "6",
                    "weekly": -1,
                    "daily": True,
                    "hourly": "soon",
                }
            }
        )

        assert policy == RetentionPolicy(monthly=6, weekly=4, daily=7, hourly=24)


class TestSelectExpiredDocIds:
    """Tests for selecting backups outside a retention policy."""

    def test_disabled_policy_keeps_all_backups(self):
        """Test that a disabled policy does not expire anything."""
        records = [
            _record(1, "2026-11-15T10:00:00+00:00"),
            _record(2, "2026-01-01T10:00:00+00:00"),
        ]

        assert select_expired_doc_ids(records, _policy()) == []

    def test_keeps_newest_backup_per_hour(self):
        """Test hourly retention keeps only the newest backup per hour."""
        records = [
            _record(3, "2026-11-15T09:00:00+00:00"),
            _record(1, "2026-11-15T10:55:00+00:00"),
            _record(2, "2026-11-15T10:05:00+00:00"),
        ]

        assert select_expired_doc_ids(records, _policy(hourly=1)) == [2, 3]

    def test_keeps_newest_backup_per_day(self):
        """Test daily retention keeps only the newest backup per day."""
        records = [
            _record(4, "2026-11-13T12:00:00+00:00"),
            _record(2, "2026-11-15T01:00:00+00:00"),
            _record(1, "2026-11-15T23:00:00+00:00"),
            _record(3, "2026-11-14T12:00:00+00:00"),
        ]

        assert select_expired_doc_ids(records, _policy(daily=2)) == [2, 4]

    def test_keeps_newest_backup_per_week(self):
        """Test weekly retention keeps only the newest backup per week."""
        records = [
            _record(1, "2026-11-15T12:00:00+00:00"),
            _record(2, "2026-11-08T12:00:00+00:00"),
            _record(3, "2026-11-01T12:00:00+00:00"),
        ]

        assert select_expired_doc_ids(records, _policy(weekly=2)) == [3]

    def test_keeps_newest_backup_per_month(self):
        """Test monthly retention keeps only the newest backup per month."""
        records = [
            _record(1, "2026-11-15T12:00:00+00:00"),
            _record(2, "2026-10-20T12:00:00+00:00"),
            _record(3, "2026-09-01T12:00:00+00:00"),
        ]

        assert select_expired_doc_ids(records, _policy(monthly=2)) == [3]

    def test_backup_can_count_towards_several_periods(self):
        """Test that one backup can satisfy multiple retention periods."""
        records = [
            _record(1, "2026-11-15T12:00:00+00:00"),
            _record(2, "2026-11-08T12:00:00+00:00"),
            _record(3, "2026-11-07T12:00:00+00:00"),
        ]

        expired = select_expired_doc_ids(
            records, _policy(monthly=1, weekly=1, daily=1, hourly=1)
        )

        assert expired == [2, 3]

    def test_invalid_timestamps_are_kept(self):
        """Test that records with invalid timestamps are not deleted."""
        records = [
            _record(1, "2026-11-15T12:00:00+00:00"),
            _record(2, "not-a-timestamp"),
        ]

        assert select_expired_doc_ids(records, _policy(hourly=1)) == []

    def test_records_without_document_id_are_not_expired(self):
        """Test that records that cannot be deleted are left alone."""
        records = [
            {"timestamp": "2026-11-15T12:00:00+00:00"},
            _record(2, "2026-11-15T11:00:00+00:00"),
        ]

        assert select_expired_doc_ids(records, _policy(hourly=1)) == [2]

    def test_naive_timestamps_are_treated_as_utc(self):
        """Test that timestamps without a timezone are interpreted as UTC."""
        records = [
            _record(1, datetime(2026, 11, 15, 12, 30)),
            _record(2, datetime(2026, 11, 15, 12, 0, tzinfo=UTC)),
        ]

        assert select_expired_doc_ids(records, _policy(hourly=1)) == [2]
