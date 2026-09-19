"""Native MicroTech history records and their wire provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class NativeHistoryRecord:
    """The verified 18-byte big-endian MicroTech history layout."""

    meter_datetime: datetime
    temperature_c: int
    flags: int
    glucose_mg_dl: int
    reserved: int
    event_index: int
    event_port: int
    event_type: int
    event_level: int
    event_value: int


@dataclass(frozen=True, slots=True)
class CapturedHistoryRecord:
    """One native record with every available request/response wire byte."""

    native: NativeHistoryRecord
    request: bytes | None
    fragments: tuple[bytes, ...]
    response: bytes
    record: bytes
    transport_sequence: int

    def __post_init__(self) -> None:
        if self.request is not None:
            object.__setattr__(self, "request", bytes(self.request))
        object.__setattr__(
            self,
            "fragments",
            tuple(bytes(fragment) for fragment in self.fragments),
        )
        object.__setattr__(self, "response", bytes(self.response))
        object.__setattr__(self, "record", bytes(self.record))


def decode_history_record(data: bytes) -> NativeHistoryRecord:
    """Decode one exact native 18-byte BGM history record."""
    if len(data) != 18:
        raise ValueError("a BGM history record is exactly 18 bytes")

    try:
        meter_datetime = datetime(
            2000 + data[0],
            data[1],
            data[2],
            data[3],
            data[4],
            data[5],
        )
    except ValueError as exc:
        raise ValueError("invalid BGM history timestamp") from exc

    glucose_mg_dl = int.from_bytes(data[8:10], "big")
    if not 1 <= glucose_mg_dl <= 1000:
        raise ValueError("implausible BGM glucose value")

    return NativeHistoryRecord(
        meter_datetime=meter_datetime,
        temperature_c=data[6],
        flags=data[7],
        glucose_mg_dl=glucose_mg_dl,
        reserved=int.from_bytes(data[10:12], "big"),
        event_index=int.from_bytes(data[12:14], "big") & 0x7FFF,
        event_port=data[14],
        event_type=data[15],
        event_level=data[16],
        event_value=data[17],
    )


def find_history_records(
    response: bytes,
) -> tuple[tuple[int, NativeHistoryRecord], ...]:
    """Find native records embedded in one reconstructed response."""
    found: list[tuple[int, NativeHistoryRecord]] = []
    for offset in range(max(0, len(response) - 17)):
        try:
            record = decode_history_record(response[offset : offset + 18])
        except ValueError:
            continue
        found.append((offset, record))
    return tuple(found)
