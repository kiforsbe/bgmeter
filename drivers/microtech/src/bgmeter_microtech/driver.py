"""Public MicroTech GoChek/Wellion driver adapter."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from bgmeter import (
    DRIVER_API_VERSION,
    CompletionStatus,
    DriverMatch,
    GlucoseRecord,
    GattSession,
    MalformedResponseError,
    MeterConnectionError,
    MeterDevice,
    MeterError,
    MeterIdentity,
    MeterTimeoutError,
    ProtocolError,
    RawCapture,
    ReadOptions,
    ReadResult,
    TransportEndpoint,
    TransportSession,
    UnsupportedDeviceError,
    interpret_meter_datetime,
)

from .protocol import read_history
from .records import CapturedHistoryRecord

_log = logging.getLogger(__name__)

_BLUETOOTH_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"
_FFE0 = "ffe0"
_FFE1 = "ffe1"
_DEVICE_INFORMATION = "180a"
_DEVICE_INFORMATION_FIELDS = {
    "2a29": "manufacturer",
    "2a24": "model",
    "2a25": "serial_number",
    "2a26": "firmware_revision",
    "2a27": "hardware_revision",
    "2a28": "software_revision",
}
_SYSTEM_ID = "2a23"
_MMOL_QUANTUM = Decimal("0.01")
_MG_DL_PER_MMOL_L = Decimal("18")
_DOCUMENTED_FLAG_BITS = {
    "hypo": 0x01,
    "hyper": 0x02,
    "ketone": 0x04,
    "pre_meal": 0x08,
    "post_meal": 0x10,
    "invalid": 0x20,
    "control_solution": 0x40,
}


def _uuid16(value: str) -> str:
    normalized = value.casefold()
    if normalized.startswith("0000") and normalized.endswith(_BLUETOOTH_BASE_SUFFIX):
        return normalized[4:8]
    return normalized


def _find_characteristic(session: GattSession, service_uuid: str, char_uuid: str):
    for service in session.services:
        if _uuid16(service.uuid) != service_uuid:
            continue
        for characteristic in service.characteristics:
            if _uuid16(characteristic.uuid) == char_uuid:
                return characteristic
    return None


def _require_data_characteristic(session: TransportSession):
    if getattr(session, "transport", None) != "ble" or not hasattr(session, "services"):
        raise UnsupportedDeviceError(
            "MicroTech meters require a connected BLE GATT session with FFE0/FFE1"
        )
    characteristic = _find_characteristic(session, _FFE0, _FFE1)
    if characteristic is None:
        raise UnsupportedDeviceError(
            "connected meter does not expose the required FFE0 service with FFE1 characteristic"
        )
    return characteristic


def history_record_id(device: MeterDevice, event_index: int) -> str:
    """Return the public record identity for one MicroTech event index."""

    return f"{MicroTechBgmDriver.driver_id}:{device.selector}:{event_index}"


def _normalize_record(
    captured: CapturedHistoryRecord,
    *,
    device: MeterDevice,
    timezone_name: str | None,
) -> GlucoseRecord:
    native = captured.native
    return GlucoseRecord(
        record_id=history_record_id(device, native.event_index),
        native_sequence=native.event_index,
        mmol_l=(Decimal(native.glucose_mg_dl) / _MG_DL_PER_MMOL_L).quantize(
            _MMOL_QUANTUM
        ),
        native_value=Decimal(native.glucose_mg_dl),
        native_unit="mg/dL",
        measured_at=interpret_meter_datetime(native.meter_datetime, timezone_name),
        flags={
            name: bool(native.flags & bit)
            for name, bit in _DOCUMENTED_FLAG_BITS.items()
        },
        source_device_id=device.selector,
        source_driver_id=MicroTechBgmDriver.driver_id,
        driver_data={
            "microtech": {
                "temperature_c": native.temperature_c,
                "flags": native.flags,
                "reserved": native.reserved,
                "event_index": native.event_index,
                "event_port": native.event_port,
                "event_type": native.event_type,
                "event_level": native.event_level,
                "event_value": native.event_value,
                "transport_sequence": captured.transport_sequence,
            }
        },
        raw=RawCapture(
            request=captured.request,
            fragments=captured.fragments,
            response=captured.response,
            record=captured.record,
        ),
    )


class MicroTechBgmDriver:
    """Public driver for the verified MicroTech FFE0/FFE1 protocol."""

    driver_id = "microtech-bgm"
    display_name = "MicroTech GoChek/Wellion blood glucose meter"
    api_version = DRIVER_API_VERSION
    supported_transports = frozenset({"ble"})
    known_meter_identities = (
        "GoChek",
        "GoChek Connect",
        "Wellion NEWTON",
        "MicroTech Medical",
    )

    def match_candidate(self, endpoint: TransportEndpoint) -> DriverMatch | None:
        if endpoint.transport != "ble":
            return None

        evidence: list[str] = []
        confidence = 0
        if any(_uuid16(uuid) == _FFE0 for uuid in endpoint.service_uuids):
            evidence.append("advertises MicroTech FFE0 service")
            confidence = 100

        name = (endpoint.name or "").casefold()
        for marker in ("gochek", "wellion"):
            if marker in name:
                evidence.append(f"device name contains {marker}")
                confidence = max(confidence, 80)

        if not evidence:
            return None
        return DriverMatch(confidence=confidence, evidence=tuple(evidence))

    async def probe(self, session: TransportSession) -> MeterIdentity:
        _require_data_characteristic(session)
        values: dict[str, str | None] = {
            field: None for field in _DEVICE_INFORMATION_FIELDS.values()
        }
        metadata: dict[str, object] = {}

        for service in session.services:
            if _uuid16(service.uuid) != _DEVICE_INFORMATION:
                continue
            for characteristic in service.characteristics:
                short_uuid = _uuid16(characteristic.uuid)
                field = _DEVICE_INFORMATION_FIELDS.get(short_uuid)
                if field is None and short_uuid != _SYSTEM_ID:
                    continue
                try:
                    raw_value = await session.read_gatt_char(characteristic.uuid)
                except Exception as error:
                    _log.warning(
                        "probe could not read characteristic %s: %s: %s",
                        short_uuid,
                        type(error).__name__,
                        error,
                    )
                    continue
                if short_uuid == _SYSTEM_ID:
                    metadata["microtech.system_id"] = raw_value
                else:
                    decoded = raw_value.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
                    values[field] = decoded or None

        _log.debug(
            "probe read device information fields: %s",
            sorted(name for name, value in values.items() if value is not None),
        )
        return MeterIdentity(metadata=metadata, **values)

    async def read_records(
        self,
        session: TransportSession,
        device: MeterDevice,
        options: ReadOptions,
    ) -> ReadResult:
        characteristic = _require_data_characteristic(session)
        started_at = datetime.now(UTC)
        _log.info(
            "read_records started: device=%s timezone=%s request_timeout=%.1fs retries=%d newest_count=%s",
            device.selector,
            options.timezone,
            options.request_timeout,
            options.retries,
            options.newest_count,
        )
        try:
            collector = await read_history(
                session,
                characteristic.uuid,
                request_timeout=options.request_timeout,
                retries=options.retries,
                progress=options.progress,
                newest_count=options.newest_count,
                is_known=(
                    (
                        lambda event_index: history_record_id(device, event_index)
                        in options.known_record_ids
                    )
                    if options.known_record_ids
                    else None
                ),
            )
        except asyncio.CancelledError:
            raise
        except MeterError:
            raise
        except TimeoutError as error:
            raise MeterTimeoutError("MicroTech history retrieval timed out") from error
        except ValueError as error:
            raise MalformedResponseError(
                f"MicroTech history response was malformed: {error}"
            ) from error
        except OSError as error:
            raise MeterConnectionError(
                f"MicroTech meter communication failed: {error}"
            ) from error
        except Exception as error:
            raise ProtocolError(f"MicroTech history retrieval failed: {error}") from error

        if not collector.records:
            raise MeterTimeoutError(
                "The meter connected but did not send any usable records."
            )

        captured = tuple(
            item
            for item in collector.records
            if history_record_id(device, item.native.event_index)
            not in options.known_record_ids
        )
        records = tuple(
            _normalize_record(
                item,
                device=device,
                timezone_name=options.timezone,
            )
            for item in captured
        )
        expected_count = collector.expected_count
        missing_indexes = collector.missing_indexes
        warnings = (
            (
                "Missing MicroTech history "
                + ("event index " if len(missing_indexes) == 1 else "event indexes ")
                + ", ".join(str(index) for index in missing_indexes)
            ),
        ) if missing_indexes else ()
        cleanup_evidence = collector.cleanup_evidence
        if cleanup_evidence is not None:
            warnings += (
                "MicroTech notification cleanup failed: "
                + cleanup_evidence["message"],
            )
        if cleanup_evidence is not None:
            termination_reason = "unsubscribe_failed"
        elif collector.status is CompletionStatus.TRUNCATED:
            termination_reason = collector.truncation_reason or "history_truncated"
        elif collector.status is CompletionStatus.COMPLETE:
            termination_reason = "history_complete"
        elif missing_indexes:
            termination_reason = "missing_records"
        else:
            termination_reason = "count_unknown"

        result = ReadResult(
            device=device,
            records=records,
            completion=collector.status,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            expected_count=expected_count,
            received_count=len(records),
            duplicate_count=collector.duplicate_transmission_count,
            rejected_count=collector.rejected_transmission_count,
            retry_count=collector.retry_count,
            termination_reason=termination_reason,
            warnings=warnings,
            diagnostics={
                "microtech.missing_event_indexes": missing_indexes,
                "microtech.notification_count": collector.notification_count,
                "microtech.complete_message_count": collector.complete_message_count,
                "microtech.identical_transmission_count": (
                    collector.identical_transmission_count
                ),
                "microtech.conflicting_transmission_count": (
                    collector.conflicting_transmission_count
                ),
                "microtech.invalid_notification_count": (
                    collector.invalid_notification_count
                ),
                "microtech.empty_response_count": collector.empty_response_count,
                "microtech.highest_observed_index": collector.highest_observed_index,
                "microtech.truncation_reason": collector.truncation_reason,
                "microtech.wire": collector.wire_evidence,
                **(
                    {"microtech.cleanup": cleanup_evidence}
                    if cleanup_evidence is not None
                    else {}
                ),
            },
        )
        _log.info(
            "read_records finished: completion=%s records=%d expected=%s termination=%s",
            result.completion.value,
            result.received_count,
            result.expected_count,
            result.termination_reason,
        )
        return result


def driver_factory() -> MicroTechBgmDriver:
    """Create one stateless MicroTech driver instance."""

    return MicroTechBgmDriver()


__all__ = ["MicroTechBgmDriver", "driver_factory", "history_record_id"]
