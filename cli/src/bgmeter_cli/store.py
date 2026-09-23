"""Normalized, versioned SQLite persistence for CLI measurement reads."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from platformdirs import user_data_path

from bgmeter import GlucoseRecord


_SCHEMA_VERSION = 2
_READABLE_SCHEMA_VERSIONS = frozenset({1, _SCHEMA_VERSION})
_SCHEMA_STATEMENTS = (
    """
CREATE TABLE measurements (
    record_id TEXT PRIMARY KEY,
    source_driver_id TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    native_sequence INTEGER,
    meter_datetime TEXT NOT NULL,
    measured_at_local TEXT NOT NULL,
    measured_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    utc_offset_seconds INTEGER NOT NULL,
    mmol_l REAL NOT NULL,
    native_value REAL NOT NULL,
    native_unit TEXT NOT NULL,
    stored_at_utc TEXT NOT NULL,
    message TEXT
)
""",
    """
CREATE TABLE measurement_flags (
    record_id TEXT NOT NULL REFERENCES measurements(record_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value INTEGER NOT NULL CHECK (value IN (0, 1)),
    PRIMARY KEY (record_id, name)
)
""",
    "CREATE INDEX measurement_flags_name_value_idx ON measurement_flags(name, value)",
)


class StoreError(RuntimeError):
    """SQLite measurement persistence failed."""


@dataclass(frozen=True, slots=True)
class StoreSummary:
    """Counts reported after one store transaction."""

    inserted_count: int
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class StoredDeviceState:
    """What one database already holds for one meter."""

    record_ids: frozenset[str]
    highest_sequence: int | None


def default_database_path() -> Path:
    """Return the per-user SQLite database path."""

    return Path(user_data_path("bgmeter", appauthor=False)) / "measurements.sqlite3"


def _legacy_database_path() -> Path:
    """Return the former platformdirs path used before the Windows path fix."""

    return Path(user_data_path("bgmeter")) / "measurements.sqlite3"


class MeasurementStore:
    """Persist normalized glucose records in one versioned SQLite database."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._uses_default_path = path is None
        self._path = Path(path) if path is not None else default_database_path()

    def store(
        self,
        records: Iterable[GlucoseRecord],
        *,
        stored_at: datetime | None = None,
        messages: Mapping[str, str | None] | None = None,
    ) -> StoreSummary:
        """Insert unique normalized records and return insertion counts."""

        materialized_records = tuple(records)
        messages = {} if messages is None else messages
        stored_at_utc = self._stored_at_utc(stored_at)
        connection: sqlite3.Connection | None = None
        try:
            if self._uses_default_path:
                self._migrate_legacy_database()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path)
            connection.execute("PRAGMA foreign_keys = ON")
            self._ensure_schema(connection)
            self._validate_records(materialized_records, messages)
            inserted_count, duplicate_count = self._insert_records(
                connection, materialized_records, stored_at_utc, messages
            )
        except StoreError:
            raise
        except (OSError, OverflowError, sqlite3.Error, TypeError, ValueError) as error:
            raise StoreError(f"cannot store measurements: {error}") from error
        finally:
            if connection is not None:
                connection.close()
        return StoreSummary(
            inserted_count=inserted_count,
            duplicate_count=duplicate_count,
        )

    def device_state(
        self,
        *,
        driver_id: str,
        device_id: str,
    ) -> StoredDeviceState:
        """Return the records already stored for one meter, without writing."""

        empty = StoredDeviceState(record_ids=frozenset(), highest_sequence=None)
        if not self._path.exists():
            return empty
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in _READABLE_SCHEMA_VERSIONS:
                raise StoreError(
                    f"unsupported measurement database version {version}"
                )
            rows = connection.execute(
                "SELECT record_id, native_sequence FROM measurements "
                "WHERE source_driver_id = ? AND source_device_id = ?",
                (driver_id, device_id),
            ).fetchall()
        except StoreError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise StoreError(f"cannot read measurements: {error}") from error
        finally:
            if connection is not None:
                connection.close()
        sequences = [row[1] for row in rows if row[1] is not None]
        return StoredDeviceState(
            record_ids=frozenset(row[0] for row in rows),
            highest_sequence=max(sequences) if sequences else None,
        )

    def messages_for(self, record_ids: Iterable[str]) -> dict[str, str]:
        """Return saved messages for the requested record IDs without writing."""

        requested_ids = tuple(dict.fromkeys(record_ids))
        if not requested_ids or not self._path.exists():
            return {}
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return {}
            if version != _SCHEMA_VERSION:
                raise StoreError(f"unsupported measurement database version {version}")
            batch_size = connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            messages = {}
            for start in range(0, len(requested_ids), batch_size):
                batch = requested_ids[start : start + batch_size]
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(
                    "SELECT record_id, message FROM measurements "
                    f"WHERE record_id IN ({placeholders}) AND message IS NOT NULL",
                    batch,
                ).fetchall()
                messages.update(rows)
            return messages
        except StoreError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise StoreError(f"cannot read measurement messages: {error}") from error
        finally:
            if connection is not None:
                connection.close()

    def _migrate_legacy_database(self) -> None:
        legacy_path = _legacy_database_path()
        if self._path.exists() or not legacy_path.exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            legacy_path.rename(self._path)
        except OSError as error:
            if self._path.exists() and not legacy_path.exists():
                return
            raise StoreError(f"cannot migrate legacy measurement database: {error}") from error

    @staticmethod
    def _validate_records(
        records: tuple[GlucoseRecord, ...], messages: Mapping[str, str | None]
    ) -> None:
        for record in records:
            for name, value in record.flags.items():
                if not isinstance(name, str) or not name:
                    raise StoreError("flag names must be non-empty strings")
                if not isinstance(value, bool):
                    raise StoreError("flag values must be bool")
        for record_id, message in messages.items():
            if not isinstance(record_id, str):
                raise StoreError("message record IDs must be strings")
            if message is not None and not isinstance(message, str):
                raise StoreError("messages must be strings or None")

    @staticmethod
    def _stored_at_utc(stored_at: datetime | None) -> str:
        timestamp = datetime.now(UTC) if stored_at is None else stored_at
        if not isinstance(timestamp, datetime):
            raise StoreError("stored_at must be a datetime")
        if timestamp.tzinfo is None:
            raise StoreError("stored_at must include a timezone")
        return timestamp.astimezone(UTC).isoformat()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == _SCHEMA_VERSION:
                pass
            elif version == 1:
                connection.execute("ALTER TABLE measurements ADD COLUMN message TEXT")
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif version != 0:
                raise StoreError(f"unsupported measurement database version {version}")
            else:
                has_schema = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_schema "
                    "WHERE type IN ('table', 'index', 'view', 'trigger'))"
                ).fetchone()[0]
                if has_schema:
                    raise StoreError("unsupported measurement database version 0")
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _insert_records(
        connection: sqlite3.Connection,
        records: tuple[GlucoseRecord, ...],
        stored_at_utc: str,
        messages: Mapping[str, str | None],
    ) -> tuple[int, int]:
        inserted_count = 0
        duplicate_count = 0
        with connection:
            for record in records:
                inserted = connection.execute(
                    """
                    INSERT INTO measurements(
                        record_id, source_driver_id, source_device_id, native_sequence,
                        meter_datetime, measured_at_local, measured_at_utc, timezone,
                        utc_offset_seconds, mmol_l, native_value, native_unit, stored_at_utc,
                        message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(record_id) DO NOTHING
                    """,
                    (
                        record.record_id,
                        record.source_driver_id,
                        record.source_device_id,
                        record.native_sequence,
                        record.measured_at.meter_datetime.isoformat(),
                        record.measured_at.measured_at_local.isoformat(),
                        record.measured_at.measured_at_utc.isoformat(),
                        record.measured_at.timezone,
                        record.measured_at.utc_offset_seconds,
                        float(record.mmol_l),
                        float(record.native_value),
                        record.native_unit,
                        stored_at_utc,
                        messages.get(record.record_id),
                    ),
                ).rowcount
                if inserted:
                    inserted_count += 1
                    connection.executemany(
                        "INSERT INTO measurement_flags(record_id, name, value) VALUES (?, ?, ?)",
                        [
                            (record.record_id, name, int(value))
                            for name, value in record.flags.items()
                        ],
                    )
                else:
                    message = messages.get(record.record_id)
                    if message is not None:
                        connection.execute(
                            "UPDATE measurements SET message = ? WHERE record_id = ?",
                            (message, record.record_id),
                        )
                    duplicate_count += 1
        return inserted_count, duplicate_count


__all__ = [
    "MeasurementStore",
    "StoreError",
    "StoreSummary",
    "StoredDeviceState",
    "default_database_path",
]
