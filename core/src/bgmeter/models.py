"""Immutable, protocol-neutral public data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, TypeVar, cast
from uuid import UUID

from .progress import ProgressCallback


_Value = TypeVar("_Value")


type MetadataValue = (
    None | bool | int | float | str | bytes | Decimal | datetime | date | time
    | timedelta | UUID | Mapping[str | int, MetadataValue]
    | tuple[MetadataValue, ...] | frozenset[MetadataValue]
    | list[MetadataValue] | set[MetadataValue] | bytearray | memoryview
)


def _freeze_metadata(value: object, active: set[int]) -> MetadataValue:
    if type(value) in (
        type(None), bool, int, float, str, bytes, Decimal, datetime, date, time,
        timedelta, UUID,
    ):
        return cast(MetadataValue, value)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        raise TypeError(f"unsupported metadata value: {type(value).__name__}")
    if id(value) in active:
        raise ValueError("cyclic metadata is not supported")
    active.add(id(value))
    try:
        if isinstance(value, Mapping):
            if any(type(key) not in (str, int) for key in value):
                raise TypeError("metadata mapping keys must be strings or integers")
            return MappingProxyType({
                key: _freeze_metadata(item, active) for key, item in value.items()
            })
        items = (_freeze_metadata(item, active) for item in value)
        return frozenset(items) if isinstance(value, (set, frozenset)) else tuple(items)
    finally:
        active.remove(id(value))


def _readonly_mapping(value: Mapping[str, _Value]) -> Mapping[str, _Value]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError("top-level metadata must be a mapping with string keys")
    return cast(Mapping[str, _Value], _freeze_metadata(value, set()))


class CompletionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class TransportEndpoint:
    transport: str
    identifier: str
    name: str | None = None
    service_uuids: frozenset[str] = frozenset()
    metadata: Mapping[str, MetadataValue] = field(default_factory=dict)
    handle: object | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_uuids", frozenset(self.service_uuids))
        object.__setattr__(self, "metadata", _readonly_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class DriverMatch:
    confidence: int
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True, slots=True)
class MeterIdentity:
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_revision: str | None = None
    hardware_revision: str | None = None
    software_revision: str | None = None
    metadata: Mapping[str, MetadataValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _readonly_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class MeterDevice:
    selector: str
    driver_id: str
    endpoint: TransportEndpoint
    identity: MeterIdentity
    match: DriverMatch


@dataclass(frozen=True, slots=True)
class MeasurementTime:
    meter_datetime: datetime
    measured_at_local: datetime
    measured_at_utc: datetime
    timezone: str
    utc_offset_seconds: int


@dataclass(frozen=True, slots=True)
class RawCapture:
    request: bytes | None = None
    fragments: tuple[bytes, ...] = ()
    response: bytes | None = None
    record: bytes | None = None

    def __post_init__(self) -> None:
        for name in ("request", "response", "record"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, bytes(value))
        object.__setattr__(self, "fragments", tuple(bytes(item) for item in self.fragments))


@dataclass(frozen=True, slots=True)
class GlucoseRecord:
    record_id: str
    native_sequence: int | None
    mmol_l: Decimal
    native_value: Decimal
    native_unit: str
    measured_at: MeasurementTime
    flags: Mapping[str, bool] = field(default_factory=dict)
    source_device_id: str = ""
    source_driver_id: str = ""
    driver_data: Mapping[str, MetadataValue] = field(default_factory=dict)
    raw: RawCapture = field(default_factory=RawCapture)

    def __post_init__(self) -> None:
        object.__setattr__(self, "flags", _readonly_mapping(self.flags))
        object.__setattr__(self, "driver_data", _readonly_mapping(self.driver_data))


@dataclass(frozen=True, slots=True)
class ReadOptions:
    timezone: str | None = None
    request_timeout: float = 5.0
    retries: int = 3
    newest_count: int | None = None
    known_record_ids: frozenset[str] = frozenset()
    progress: ProgressCallback | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.newest_count is not None and self.newest_count < 1:
            raise ValueError("newest_count must be at least 1")
        object.__setattr__(
            self, "known_record_ids", frozenset(self.known_record_ids)
        )


@dataclass(frozen=True, slots=True)
class ReadResult:
    device: MeterDevice
    records: tuple[GlucoseRecord, ...]
    completion: CompletionStatus
    started_at: datetime
    ended_at: datetime
    expected_count: int | None = None
    received_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    retry_count: int = 0
    termination_reason: str | None = None
    warnings: tuple[str, ...] = ()
    diagnostics: Mapping[str, MetadataValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "diagnostics", _readonly_mapping(self.diagnostics))
