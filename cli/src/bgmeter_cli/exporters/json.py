"""Canonical versioned lossless JSON export."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from typing import Any
from uuid import UUID

from bgmeter import MeterDevice, ReadResult


def normalize_value(value: object) -> Any:
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes_hex": bytes(value).hex()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"$timedelta_seconds": str(Decimal(str(value.total_seconds())))}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        if all(type(key) is str for key in value):
            return {key: normalize_value(item) for key, item in value.items()}
        return {
            "$mapping": [
                {"key": key, "value": normalize_value(item)}
                for key, item in value.items()
            ]
        }
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [normalize_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
    raise TypeError(f"cannot serialize value of type {type(value).__name__}")


def _device_document(device: MeterDevice) -> dict[str, object]:
    identity = device.identity
    endpoint = device.endpoint
    return {
        "selector": device.selector,
        "driver_id": device.driver_id,
        "endpoint": {
            "transport": endpoint.transport,
            "identifier": endpoint.identifier,
            "name": endpoint.name,
            "service_uuids": sorted(endpoint.service_uuids),
            "metadata": normalize_value(endpoint.metadata),
        },
        "identity": {
            "manufacturer": identity.manufacturer,
            "model": identity.model,
            "serial_number": identity.serial_number,
            "firmware_revision": identity.firmware_revision,
            "hardware_revision": identity.hardware_revision,
            "software_revision": identity.software_revision,
            "metadata": normalize_value(identity.metadata),
        },
        "match": {
            "confidence": device.match.confidence,
            "evidence": list(device.match.evidence),
        },
    }


def read_result_document(result: ReadResult) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for record in result.records:
        measured_at = record.measured_at
        records.append(
            {
                "record_id": record.record_id,
                "native_sequence": record.native_sequence,
                "mmol_l": str(record.mmol_l),
                "native_value": str(record.native_value),
                "native_unit": record.native_unit,
                "meter_datetime": measured_at.meter_datetime.isoformat(),
                "measured_at_local": measured_at.measured_at_local.isoformat(),
                "measured_at_utc": measured_at.measured_at_utc.isoformat(),
                "timezone": measured_at.timezone,
                "utc_offset_seconds": measured_at.utc_offset_seconds,
                "flags": normalize_value(record.flags),
                "source_device_id": record.source_device_id,
                "source_driver_id": record.source_driver_id,
                "driver_data": normalize_value(record.driver_data),
                "raw": {
                    "request_hex": (
                        record.raw.request.hex()
                        if record.raw.request is not None
                        else None
                    ),
                    "fragments_hex": [item.hex() for item in record.raw.fragments],
                    "response_hex": (
                        record.raw.response.hex()
                        if record.raw.response is not None
                        else None
                    ),
                    "record_hex": (
                        record.raw.record.hex()
                        if record.raw.record is not None
                        else None
                    ),
                },
            }
        )
    return {
        "schema": "bgmeter.read-result",
        "schema_version": 1,
        "completion": {
            "status": result.completion.value,
            "expected_count": result.expected_count,
            "received_count": result.received_count,
            "duplicate_count": result.duplicate_count,
            "rejected_count": result.rejected_count,
            "retry_count": result.retry_count,
            "termination_reason": result.termination_reason,
        },
        "device": _device_document(result.device),
        "started_at": result.started_at.isoformat(),
        "ended_at": result.ended_at.isoformat(),
        "records": records,
        "warnings": list(result.warnings),
        "diagnostics": normalize_value(result.diagnostics),
    }


def render_json(result: ReadResult) -> str:
    return (
        json.dumps(
            read_result_document(result),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


__all__ = ["normalize_value", "read_result_document", "render_json"]
