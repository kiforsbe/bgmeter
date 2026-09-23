"""Stable one-row-per-record CSV export."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping

from bgmeter import ReadResult

from .json import normalize_value


CSV_COLUMNS = (
    "record_id",
    "native_sequence",
    "mmol_l",
    "native_value",
    "native_unit",
    "meter_datetime",
    "measured_at_local",
    "measured_at_utc",
    "timezone",
    "utc_offset_seconds",
    "flags_json",
    "message",
    "source_device_id",
    "source_driver_id",
    "raw_request_hex",
    "raw_fragments_json",
    "raw_response_hex",
    "raw_record_hex",
    "driver_data_json",
)


def _compact_json(value: object) -> str:
    return json.dumps(
        normalize_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def render_csv(result: ReadResult, *, messages: Mapping[str, str] | None = None) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in result.records:
        measured_at = record.measured_at
        writer.writerow(
            {
                "record_id": record.record_id,
                "native_sequence": (
                    "" if record.native_sequence is None else record.native_sequence
                ),
                "mmol_l": str(record.mmol_l),
                "native_value": str(record.native_value),
                "native_unit": record.native_unit,
                "meter_datetime": measured_at.meter_datetime.isoformat(),
                "measured_at_local": measured_at.measured_at_local.isoformat(),
                "measured_at_utc": measured_at.measured_at_utc.isoformat(),
                "timezone": measured_at.timezone,
                "utc_offset_seconds": measured_at.utc_offset_seconds,
                "flags_json": _compact_json(record.flags),
                "message": "" if messages is None else messages.get(record.record_id, ""),
                "source_device_id": record.source_device_id,
                "source_driver_id": record.source_driver_id,
                "raw_request_hex": (
                    record.raw.request.hex() if record.raw.request is not None else ""
                ),
                "raw_fragments_json": _compact_json(
                    [item.hex() for item in record.raw.fragments]
                ),
                "raw_response_hex": (
                    record.raw.response.hex() if record.raw.response is not None else ""
                ),
                "raw_record_hex": (
                    record.raw.record.hex() if record.raw.record is not None else ""
                ),
                "driver_data_json": _compact_json(record.driver_data),
            }
        )
    return stream.getvalue()


__all__ = ["CSV_COLUMNS", "render_csv"]
