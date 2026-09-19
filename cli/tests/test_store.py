from __future__ import annotations

import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from bgmeter import GlucoseRecord, MeasurementTime  # noqa: E402
from bgmeter_cli.store import (  # noqa: E402
    MeasurementStore,
    StoreError,
    StoreSummary,
    default_database_path,
)


STORED_AT = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)


@pytest.fixture
def record() -> GlucoseRecord:
    measured_at = MeasurementTime(
        meter_datetime=datetime(2026, 9, 16, 19, 10, 16),
        measured_at_local=datetime.fromisoformat("2026-09-16T19:10:16+02:00"),
        measured_at_utc=datetime(2026, 9, 16, 17, 10, 16, tzinfo=UTC),
        timezone="Europe/Stockholm",
        utc_offset_seconds=7200,
    )
    return GlucoseRecord(
        record_id="fake:meter-1:7",
        native_sequence=7,
        mmol_l=Decimal("7.44"),
        native_value=Decimal("134"),
        native_unit="mg/dL",
        measured_at=measured_at,
        flags={"before_meal": True, "control": False},
        source_device_id="fake:meter-1",
        source_driver_id="fake",
        driver_data={"ignored": "by store"},
    )


def test_default_database_path_uses_platform_user_data(monkeypatch):
    target_directory = Path("C:/data/bgmeter")
    monkeypatch.setattr(
        "bgmeter_cli.store.user_data_path",
        lambda app, *, appauthor=None: target_directory
        if appauthor is False
        else target_directory / app,
    )

    assert default_database_path() == target_directory / "measurements.sqlite3"


def test_default_store_moves_legacy_double_folder_database(tmp_path, record, monkeypatch):
    target_directory = tmp_path / "appdata" / "bgmeter"
    legacy_database = target_directory / "bgmeter" / "measurements.sqlite3"
    MeasurementStore(legacy_database).store((record,))
    monkeypatch.setattr(
        "bgmeter_cli.store.user_data_path",
        lambda app, *, appauthor=None: target_directory
        if appauthor is False
        else target_directory / app,
    )

    summary = MeasurementStore().store(())

    target_database = target_directory / "measurements.sqlite3"
    assert summary == StoreSummary(inserted_count=0, duplicate_count=0)
    assert target_database.exists()
    assert not legacy_database.exists()
    with sqlite3.connect(target_database) as connection:
        assert connection.execute("SELECT record_id FROM measurements").fetchall() == [
            (record.record_id,)
        ]


def test_default_store_never_overwrites_new_path_database(tmp_path, record, monkeypatch):
    target_directory = tmp_path / "appdata" / "bgmeter"
    target_database = target_directory / "measurements.sqlite3"
    legacy_database = target_directory / "bgmeter" / "measurements.sqlite3"
    legacy_record = replace(record, record_id="fake:meter-1:legacy")
    new_record = replace(record, record_id="fake:meter-1:new")
    incoming_record = replace(record, record_id="fake:meter-1:incoming")
    MeasurementStore(legacy_database).store((legacy_record,))
    MeasurementStore(target_database).store((new_record,))
    monkeypatch.setattr(
        "bgmeter_cli.store.user_data_path",
        lambda app, *, appauthor=None: target_directory
        if appauthor is False
        else target_directory / app,
    )

    MeasurementStore().store((incoming_record,))

    with sqlite3.connect(target_database) as connection:
        assert connection.execute(
            "SELECT record_id FROM measurements ORDER BY record_id"
        ).fetchall() == [("fake:meter-1:incoming",), ("fake:meter-1:new",)]
    with sqlite3.connect(legacy_database) as connection:
        assert connection.execute("SELECT record_id FROM measurements").fetchall() == [
            ("fake:meter-1:legacy",)
        ]


def test_store_creates_v1_normalized_schema_and_rows(tmp_path, record):
    database = tmp_path / "measurements.sqlite3"

    summary = MeasurementStore(database).store((record,), stored_at=STORED_AT)

    assert summary == StoreSummary(inserted_count=1, duplicate_count=0)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT record_id, source_driver_id, source_device_id, native_sequence, "
            "meter_datetime, measured_at_local, measured_at_utc, timezone, "
            "utc_offset_seconds, mmol_l, native_value, native_unit, stored_at_utc "
            "FROM measurements"
        ).fetchone() == (
            record.record_id,
            "fake",
            "fake:meter-1",
            7,
            "2026-09-16T19:10:16",
            "2026-09-16T19:10:16+02:00",
            "2026-09-16T17:10:16+00:00",
            "Europe/Stockholm",
            7200,
            7.44,
            134.0,
            "mg/dL",
            "2026-09-18T12:30:00+00:00",
        )
        assert connection.execute(
            "SELECT name, value FROM measurement_flags ORDER BY name"
        ).fetchall() == [("before_meal", 1), ("control", 0)]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(measurements)")
        }
        assert {"driver_data_json", "raw_json", "diagnostics_json"}.isdisjoint(columns)


def test_store_deduplicates_by_record_id_and_keeps_first_flags(tmp_path, record):
    database = tmp_path / "db.sqlite3"

    first = MeasurementStore(database).store((record,))
    changed = replace(record, mmol_l=Decimal("9.99"), flags={"before_meal": False})
    second = MeasurementStore(database).store((changed,))

    assert first == StoreSummary(1, 0)
    assert second == StoreSummary(0, 1)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT mmol_l FROM measurements").fetchone() == (7.44,)
        assert connection.execute(
            "SELECT value FROM measurement_flags WHERE name = 'before_meal'"
        ).fetchone() == (1,)


def test_concurrent_first_stores_create_schema_once(tmp_path, record, monkeypatch):
    database = tmp_path / "concurrent.sqlite3"
    real_connect = sqlite3.connect
    version_checks = 0
    version_lock = threading.Lock()
    second_version_check = threading.Event()

    class CoordinatedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal version_checks
            cursor = super().execute(sql, parameters)
            if sql == "PRAGMA user_version":
                with version_lock:
                    version_checks += 1
                    if version_checks == 2:
                        second_version_check.set()
                second_version_check.wait(timeout=0.2)
            return cursor

    monkeypatch.setattr(
        "bgmeter_cli.store.sqlite3.connect",
        lambda path: real_connect(path, factory=CoordinatedConnection),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(MeasurementStore(database).store, (record,))
            for _ in range(2)
        )
        summaries = tuple(future.result() for future in futures)

    assert sorted(summaries, key=lambda summary: summary.inserted_count) == [
        StoreSummary(inserted_count=0, duplicate_count=1),
        StoreSummary(inserted_count=1, duplicate_count=0),
    ]


def test_store_rejects_unknown_schema_version_without_writing(tmp_path, record):
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(StoreError, match="unsupported measurement database version 99"):
        MeasurementStore(database).store((record,))

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (99,)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall() == []


def test_store_rejects_invalid_flag_without_parent_insert(tmp_path, record):
    database = tmp_path / "invalid.sqlite3"
    invalid = replace(record, flags={"": True})

    with pytest.raises(StoreError, match="flag names must be non-empty strings"):
        MeasurementStore(database).store((invalid,))

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone() == (0,)


def test_store_rejects_non_boolean_flag_without_parent_insert(tmp_path, record):
    database = tmp_path / "invalid-value.sqlite3"
    invalid = replace(record, flags={"before_meal": 1})

    with pytest.raises(StoreError, match="flag values must be bool"):
        MeasurementStore(database).store((invalid,))

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone() == (0,)


def test_store_rolls_back_parent_when_flag_insert_fails(tmp_path, record):
    database = tmp_path / "flag-failure.sqlite3"
    MeasurementStore(database).store(())
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_measurement_flags
            BEFORE INSERT ON measurement_flags
            BEGIN
                SELECT RAISE(ABORT, 'simulated flag write failure');
            END
            """
        )

    with pytest.raises(StoreError, match="simulated flag write failure"):
        MeasurementStore(database).store((record,))

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM measurement_flags").fetchone() == (0,)
