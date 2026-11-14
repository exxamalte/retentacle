"""Tests for the backup storage module."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile

from shelly_backup.storage import BackupStore


def _record(
    ip_address="192.168.1.10",
    timestamp="2026-11-15T10:00:00+00:00",
    device_info=None,
):
    """Create a backup record for tests."""
    return {
        "ip_address": ip_address,
        "timestamp": timestamp,
        "generation": (device_info or {}).get("gen", 1),
        "device_info": device_info or {},
        "settings": {"test": True},
    }


class TestBackupStore:
    """Tests for BackupStore."""

    def test_path(self):
        """Test the database path property."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "shelly_backup.json"
            store = BackupStore(path)
            assert store.path == path

    def test_all_without_database(self):
        """Test reading from a database that does not exist yet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "missing.json")
            assert store.all() == []

    def test_get_without_database(self):
        """Test getting a record from a database that does not exist yet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "missing.json")
            assert store.get(1) is None

    def test_add_creates_database_and_returns_document_id(self):
        """Test adding a record creates the database and returns its ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "shelly_backup.json"
            store = BackupStore(path)
            record = _record()

            doc_id = store.add(record)

            assert doc_id == 1
            assert path.exists()
            assert store.all() == [{**record, "doc_id": doc_id}]

    def test_add_multiple_and_all_sorted_newest_first(self):
        """Test that all records are returned newest first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            store.add(_record(timestamp="2026-11-15T10:00:00+00:00"))
            store.add(_record(timestamp="2026-11-15T12:00:00+00:00"))
            store.add(_record(timestamp="2026-11-15T11:00:00+00:00"))

            timestamps = [record["timestamp"] for record in store.all()]

            assert timestamps == [
                "2026-11-15T12:00:00+00:00",
                "2026-11-15T11:00:00+00:00",
                "2026-11-15T10:00:00+00:00",
            ]

    def test_get(self):
        """Test getting a single record by document ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            record = _record()
            doc_id = store.add(record)

            assert store.get(doc_id) == {**record, "doc_id": doc_id}

    def test_get_missing_document(self):
        """Test getting a document ID that does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            store.add(_record())

            assert store.get(99) is None

    def test_by_device(self):
        """Test filtering backups by device, newest first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            store.add(
                _record(
                    ip_address="192.168.1.10",
                    timestamp="2026-11-15T10:00:00+00:00",
                )
            )
            store.add(
                _record(
                    ip_address="192.168.1.11",
                    timestamp="2026-11-15T11:00:00+00:00",
                )
            )
            store.add(
                _record(
                    ip_address="192.168.1.10",
                    timestamp="2026-11-15T12:00:00+00:00",
                )
            )

            records = store.by_device("192.168.1.10")

            assert len(records) == 2
            assert [record["timestamp"] for record in records] == [
                "2026-11-15T12:00:00+00:00",
                "2026-11-15T10:00:00+00:00",
            ]

    def test_by_device_without_matches(self):
        """Test filtering by a device that has no backups."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            store.add(_record())

            assert store.by_device("10.0.0.1") == []

    def test_devices_without_records(self):
        """Test device summaries without any records."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            assert store.devices() == []

    def test_devices_summary(self):
        """Test grouping backups into device summaries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            store.add(
                _record(
                    ip_address="192.168.1.10",
                    timestamp="2026-11-15T10:00:00+00:00",
                    device_info={"gen": 2, "name": "Kitchen"},
                )
            )
            store.add(
                _record(
                    ip_address="192.168.1.10",
                    timestamp="2026-11-15T12:00:00+00:00",
                    device_info={"gen": 2, "name": "Kitchen"},
                )
            )
            store.add(
                _record(
                    ip_address="192.168.1.11",
                    timestamp="2026-11-15T11:00:00+00:00",
                    device_info={"gen": 1, "type": "SHSW-1"},
                )
            )

            devices = store.devices()

            assert len(devices) == 2
            assert devices[0]["ip_address"] == "192.168.1.10"
            assert devices[0]["display_name"] == "Kitchen"
            assert devices[0]["backup_count"] == 2
            assert devices[0]["latest"]["timestamp"] == "2026-11-15T12:00:00+00:00"
            assert devices[0]["latest"]["doc_id"] == 2
            assert devices[1]["ip_address"] == "192.168.1.11"
            assert devices[1]["display_name"] == "SHSW-1"
            assert devices[1]["backup_count"] == 1

    def test_devices_sorted_naturally_by_ip(self):
        """Test that device summaries are sorted by IP address."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            for ip_address in ("192.168.1.100", "192.168.1.2", "10.0.0.1"):
                store.add(_record(ip_address=ip_address))

            devices = store.devices()

            assert [device["ip_address"] for device in devices] == [
                "10.0.0.1",
                "192.168.1.2",
                "192.168.1.100",
            ]

    def test_devices_display_name_fallbacks(self):
        """Test display name fallbacks for generation 1 and 2 devices."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            store.add(
                _record(
                    ip_address="192.168.1.10",
                    device_info={"gen": 2, "model": "SNSW-001X16EU"},
                )
            )
            store.add(
                _record(
                    ip_address="192.168.1.11",
                    device_info={"gen": 1, "type": "SHSW-1"},
                )
            )
            store.add(
                _record(
                    ip_address="192.168.1.12",
                    device_info={},
                )
            )

            names = {
                device["ip_address"]: device["display_name"]
                for device in store.devices()
            }

            assert names == {
                "192.168.1.10": "SNSW-001X16EU",
                "192.168.1.11": "SHSW-1",
                "192.168.1.12": "192.168.1.12",
            }

    def test_devices_ignores_records_without_ip_address(self):
        """Test that records without an IP address are not grouped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")
            store.add(_record(ip_address=None))
            store.add(_record(ip_address="192.168.1.10"))

            devices = store.devices()

            assert len(devices) == 1
            assert devices[0]["ip_address"] == "192.168.1.10"

    def test_concurrent_adds(self):
        """Test that concurrent writes are serialised."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BackupStore(Path(tmpdir) / "shelly_backup.json")

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(
                    executor.map(
                        lambda index: store.add(
                            _record(
                                ip_address=f"192.168.1.{index}",
                                timestamp=f"2026-11-15T10:00:{index:02d}+00:00",
                            )
                        ),
                        range(10),
                    )
                )

            assert len(store.all()) == 10
