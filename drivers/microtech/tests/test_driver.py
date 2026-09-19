from __future__ import annotations

import ast
from datetime import datetime
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC
from decimal import Decimal
from pathlib import Path

import pytest
import bgmeter_microtech

from bgmeter import (
    CompletionStatus,
    GattCharacteristic,
    GattService,
    MeterDevice,
    MeterIdentity,
    ReadOptions,
    TransportEndpoint,
    UnsupportedDeviceError,
)
from bgmeter_microtech import MicroTechBgmDriver, driver_factory
from bgmeter_microtech.records import CapturedHistoryRecord, NativeHistoryRecord


FIXTURE = Path(__file__).parent / "fixtures" / "2026-09-17-1700.txt"
NOTIFICATION_PATTERN = re.compile(r"^  Hex  : ([0-9a-f ]+)$")
FFE0 = "0000ffe0-0000-1000-8000-00805f9b34fb"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"
DEVICE_INFORMATION = "0000180a-0000-1000-8000-00805f9b34fb"
DEVICE_VALUES = {
    "00002a29-0000-1000-8000-00805f9b34fb": b"MicroTech Medical\x00",
    "00002a24-0000-1000-8000-00805f9b34fb": b"GoChek Connect",
    "00002a25-0000-1000-8000-00805f9b34fb": b"Z00MCF-123",
    "00002a26-0000-1000-8000-00805f9b34fb": b"1.2.3",
    "00002a27-0000-1000-8000-00805f9b34fb": b"A1",
    "00002a28-0000-1000-8000-00805f9b34fb": b"4.5.6",
    "00002a23-0000-1000-8000-00805f9b34fb": bytes.fromhex(
        "01 02 03 04 05 06 07 08"
    ),
}


def _encode_fragment(offset: int, final: bool, sequence: int, payload: bytes) -> bytes:
    from bgmeter_microtech.framing import crc8_dallas

    header = bytes(
        (0, 0, 6 + len(payload), offset, (0x80 if final else 0) | sequence)
    )
    body = header + bytes((crc8_dallas(header),)) + payload
    escaped = bytearray()
    for value in body:
        if value in (0x2D, 0x2F):
            escaped.append(0x2F)
        escaped.append(value)
    return b"\x2d\x2d" + bytes(escaped) + b"\x2d\x2d"


def _attribute_to_request(
    transmission: tuple[bytes, ...], request: bytes
) -> tuple[bytes, ...]:
    from bgmeter_microtech.framing import decode_transport_fragment

    request_token = decode_transport_fragment(request).payload[4:6]
    attributed = []
    for raw in transmission:
        fragment = decode_transport_fragment(raw)
        payload = fragment.payload
        if fragment.offset == 0:
            payload = payload[:2] + request_token + payload[4:]
        attributed.append(
            _encode_fragment(
                fragment.offset,
                fragment.final,
                fragment.sequence,
                payload,
            )
        )
    return tuple(attributed)


def _capture_notifications() -> tuple[bytes, ...]:
    return tuple(
        bytes.fromhex(match.group(1))
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if (match := NOTIFICATION_PATTERN.match(line))
    )


def _services(*, include_ffe0: bool = True, include_ffe1: bool = True):
    services = []
    if include_ffe0:
        characteristics = (
            GattCharacteristic(FFE1, 2, {"notify", "write-without-response"}),
        ) if include_ffe1 else ()
        services.append(GattService(FFE0, 1, characteristics))
    services.append(
        GattService(
            DEVICE_INFORMATION,
            3,
            tuple(
                GattCharacteristic(uuid, handle, {"read"})
                for handle, uuid in enumerate(DEVICE_VALUES, start=4)
            ),
        )
    )
    return tuple(services)


class CaptureGattSession:
    transport = "ble"

    def __init__(
        self,
        endpoint: TransportEndpoint,
        *,
        notifications: tuple[bytes, ...] = (),
        services: tuple[GattService, ...] | None = None,
        values: dict[str, bytes] | None = None,
        drop_event_index: int | None = None,
        notifications_on_start: tuple[bytes, ...] = (),
    ) -> None:
        self.endpoint = endpoint
        self.services = services if services is not None else _services()
        self.values = DEVICE_VALUES if values is None else values
        self.notifications = notifications
        self.drop_event_index = drop_event_index
        self.notifications_on_start = notifications_on_start
        self.callback: Callable[[object, bytes], object] | None = None
        self.writes: list[tuple[str, bytes, bool | None]] = []
        self.stopped = False

    async def read_gatt_char(self, characteristic: str) -> bytes:
        return self.values[characteristic.lower()]

    async def write_gatt_char(
        self,
        characteristic: str,
        data: bytes,
        *,
        response: bool | None = None,
    ) -> None:
        self.writes.append((characteristic, bytes(data), response))
        requested_index = int.from_bytes(data[-4:-2], "big")
        if self.callback is None:
            return
        target_index = 4 if requested_index == 0 else requested_index
        for position in range(0, len(self.notifications), 3):
            transmission = self.notifications[position : position + 3]
            if len(transmission) != 3:
                continue
            record_index = int.from_bytes(
                _record_bytes(transmission)[12:14], "big"
            ) & 0x7FFF
            if record_index != target_index:
                continue
            if record_index == self.drop_event_index:
                continue
            attributed = _attribute_to_request(transmission, data)
            for frame in (attributed[2], attributed[0], attributed[1]):
                self.callback(characteristic, frame)
            return

    async def start_notify(self, characteristic: str, callback) -> None:
        self.callback = callback
        for notification in self.notifications_on_start:
            callback(characteristic, notification)

    async def stop_notify(self, characteristic: str) -> None:
        self.stopped = True
        self.callback = None

    async def close(self) -> None:
        pass


def _record_bytes(transmission: tuple[bytes, ...]) -> bytes:
    from bgmeter_microtech.framing import reassemble_transport_fragments

    response = reassemble_transport_fragments(transmission)
    assert response is not None
    return response[-18:]


def _endpoint(
    *,
    identifier: str = "AA:BB:CC:DD:EE:FF",
    name: str | None = "GoChek - Z00MCF",
    services: frozenset[str] = frozenset({FFE0}),
) -> TransportEndpoint:
    return TransportEndpoint(
        transport="ble",
        identifier=identifier,
        name=name,
        service_uuids=services,
    )


def _device(endpoint: TransportEndpoint, driver: MicroTechBgmDriver) -> MeterDevice:
    match = driver.match_candidate(endpoint)
    assert match is not None
    return MeterDevice(
        selector=f"ble:{endpoint.identifier}",
        driver_id=driver.driver_id,
        endpoint=endpoint,
        identity=MeterIdentity(model=endpoint.name),
        match=match,
    )


@pytest.mark.parametrize(
    ("endpoint", "evidence"),
    [
        (_endpoint(name=None), "ffe0"),
        (_endpoint(name="GoChek - Z00MCF", services=frozenset()), "gochek"),
        (_endpoint(name="Wellion NEWTON", services=frozenset()), "wellion"),
    ],
)
def test_matches_advertised_service_or_known_names(endpoint, evidence) -> None:
    match = driver_factory().match_candidate(endpoint)

    assert match is not None
    assert any(evidence in item.casefold() for item in match.evidence)


@pytest.mark.parametrize(
    "endpoint",
    [
        _endpoint(name="Other Meter", services=frozenset()),
        _endpoint(
            identifier="00:15:93:00:6B:73",
            name="Other Meter",
            services=frozenset(),
        ),
        TransportEndpoint("usb", "GoChek - Z00MCF", name="GoChek - Z00MCF"),
    ],
)
def test_does_not_match_unrelated_devices_fixed_addresses_or_other_transports(
    endpoint,
) -> None:
    assert driver_factory().match_candidate(endpoint) is None


@pytest.mark.asyncio
async def test_probe_requires_ffe0_service_with_ffe1_characteristic() -> None:
    driver = driver_factory()
    endpoint = _endpoint()

    for services in (
        _services(include_ffe0=False),
        _services(include_ffe1=False),
    ):
        with pytest.raises(UnsupportedDeviceError, match="FFE0.*FFE1"):
            await driver.probe(CaptureGattSession(endpoint, services=services))


@pytest.mark.asyncio
async def test_probe_reads_available_device_information_values() -> None:
    identity = await driver_factory().probe(CaptureGattSession(_endpoint()))

    assert identity.manufacturer == "MicroTech Medical"
    assert identity.model == "GoChek Connect"
    assert identity.serial_number == "Z00MCF-123"
    assert identity.firmware_revision == "1.2.3"
    assert identity.hardware_revision == "A1"
    assert identity.software_revision == "4.5.6"
    assert identity.metadata["microtech.system_id"] == bytes.fromhex(
        "01 02 03 04 05 06 07 08"
    )


@pytest.mark.asyncio
async def test_capture_read_normalizes_records_time_and_full_provenance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    session = CaptureGattSession(endpoint, notifications=_capture_notifications())

    result = await driver.read_records(
        session,
        _device(endpoint, driver),
        ReadOptions(timezone="Europe/Stockholm", request_timeout=0.001),
    )

    assert result.completion is CompletionStatus.COMPLETE
    assert result.expected_count == result.received_count == 4
    assert len({record.record_id for record in result.records}) == 4
    assert [record.native_sequence for record in result.records] == [2, 3, 4, 1]
    target = next(record for record in result.records if record.native_sequence == 2)
    assert target.mmol_l == Decimal("7.44")
    assert target.native_value == Decimal("134")
    assert target.native_unit == "mg/dL"
    assert target.record_id == "microtech-bgm:ble:AA:BB:CC:DD:EE:FF:2"
    assert target.measured_at.meter_datetime.isoformat() == "2009-09-16T19:10:16"
    assert target.measured_at.measured_at_local.isoformat() == (
        "2009-09-16T19:10:16+02:00"
    )
    assert target.measured_at.measured_at_utc == target.measured_at.measured_at_local.astimezone(UTC)
    assert target.measured_at.measured_at_utc.isoformat() == "2009-09-16T17:10:16+00:00"
    assert target.measured_at.timezone == "Europe/Stockholm"
    assert target.driver_data["microtech"] == {
        "temperature_c": 27,
        "flags": 0x80,
        "reserved": 0,
        "event_index": 2,
        "event_port": 3,
        "event_type": 0,
        "event_level": 0,
        "event_value": 0,
        "transport_sequence": 3,
    }
    assert target.flags == {
        "hypo": False,
        "hyper": False,
        "ketone": False,
        "pre_meal": False,
        "post_meal": False,
        "invalid": False,
        "control_solution": False,
    }
    assert target.raw.request == bytes.fromhex(
        "2d 2d 00 00 10 00 80 c6 01 05 00 00 05 7e 02 01 00 02 2d 2d"
    )
    assert len(target.raw.fragments) == 3
    assert target.raw.response == bytes.fromhex(
        "05 01 05 7e 12 bd 05 01 09 09 "
        "10 13 0a 10 1b 80 00 86 00 00 "
        "00 02 03 00 00 00"
    )
    assert target.raw.record == bytes.fromhex(
        "09 09 10 13 0a 10 1b 80 00 86 00 00 00 02 03 00 00 00"
    )
    assert session.stopped
    wire = result.diagnostics["microtech.wire"]
    assert tuple(request["event_index"] for request in wire["requests"]) == (
        0,
        1,
        2,
        3,
    )
    assert tuple(request["request"] for request in wire["requests"]) == tuple(
        data for _, data, _ in session.writes
    )
    assert Counter(wire["notifications"]) == Counter(
        fragment
        for transmission in wire["transmissions"]
        for fragment in transmission["fragments"]
    )
    assert len(wire["notifications"]) == 12
    assert len(wire["responses"]) == 4
    assert tuple(
        transmission["attribution"] for transmission in wire["transmissions"]
    ) == ("accepted", "accepted", "accepted", "accepted")
    with pytest.raises(TypeError):
        wire["requests"][0]["event_index"] = 99
    assert capsys.readouterr() == ("", "")


def test_normalization_exposes_documented_native_flags() -> None:
    from bgmeter_microtech.driver import _normalize_record

    driver = driver_factory()
    endpoint = _endpoint()
    flagged = CapturedHistoryRecord(
        native=NativeHistoryRecord(
            meter_datetime=datetime(2026, 9, 17, 17, 27),
            temperature_c=27,
            flags=0x7F,
            glucose_mg_dl=103,
            reserved=0,
            event_index=5,
            event_port=3,
            event_type=0,
            event_level=0,
            event_value=0,
        ),
        request=None,
        fragments=(),
        response=b"response",
        record=b"record",
        transport_sequence=5,
    )

    record = _normalize_record(
        flagged,
        device=_device(endpoint, driver),
        timezone_name="Europe/Stockholm",
    )

    assert record.flags == {
        "hypo": True,
        "hyper": True,
        "ketone": True,
        "pre_meal": True,
        "post_meal": True,
        "invalid": True,
        "control_solution": True,
    }
    assert record.driver_data["microtech"]["flags"] == 0x7F


@pytest.mark.asyncio
async def test_unsubscribe_failure_retains_records_as_partial_result() -> None:
    class FailingUnsubscribeSession(CaptureGattSession):
        async def stop_notify(self, characteristic: str) -> None:
            await super().stop_notify(characteristic)
            raise RuntimeError("unsubscribe failed")

    driver = driver_factory()
    endpoint = _endpoint()
    session = FailingUnsubscribeSession(
        endpoint,
        notifications=_capture_notifications(),
    )

    result = await driver.read_records(
        session,
        _device(endpoint, driver),
        ReadOptions(timezone="Europe/Stockholm", request_timeout=0.001),
    )

    assert len(result.records) == result.expected_count == result.received_count == 4
    assert result.completion is CompletionStatus.PARTIAL
    assert result.termination_reason == "unsubscribe_failed"
    assert result.warnings == ("MicroTech notification cleanup failed: unsubscribe failed",)
    assert result.diagnostics["microtech.cleanup"] == {
        "operation": "stop_notify",
        "error_type": "RuntimeError",
        "message": "unsubscribe failed",
        "previous_completion": "complete",
    }
    with pytest.raises(TypeError):
        result.diagnostics["microtech.cleanup"]["message"] = "changed"


@pytest.mark.asyncio
async def test_partial_read_reports_exact_missing_indexes_and_counts() -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    session = CaptureGattSession(
        endpoint,
        notifications=_capture_notifications(),
        drop_event_index=2,
    )

    result = await driver.read_records(
        session,
        _device(endpoint, driver),
        ReadOptions(timezone="Europe/Stockholm", request_timeout=0.001, retries=1),
    )

    assert result.completion is CompletionStatus.PARTIAL
    assert result.expected_count == 4
    assert result.received_count == 3
    assert result.termination_reason == "missing_records"
    assert result.diagnostics["microtech.missing_event_indexes"] == (2,)
    assert "event index 2" in result.warnings[0]


@pytest.mark.asyncio
async def test_read_result_counts_each_rejected_transmission_once() -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    session = CaptureGattSession(
        endpoint,
        notifications=_capture_notifications(),
        notifications_on_start=(b"not-a-transport-fragment",),
    )

    result = await driver.read_records(
        session,
        _device(endpoint, driver),
        ReadOptions(timezone="Europe/Stockholm", request_timeout=0.001),
    )

    assert result.completion is CompletionStatus.COMPLETE
    assert result.rejected_count == 1
    rejected = result.diagnostics["microtech.wire"]["transmissions"][0]
    assert rejected["attribution"] == "rejected_invalid_notification"
    assert rejected["fragments"] == (b"not-a-transport-fragment",)


def test_driver_imports_core_only_through_its_public_package() -> None:
    source_path = Path(__file__).parents[1] / "src" / "bgmeter_microtech" / "driver.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "bgmeter" in imported_modules
    assert not any(module.startswith("bgmeter.") for module in imported_modules)


def test_package_root_exports_only_supported_driver_api() -> None:
    assert bgmeter_microtech.__all__ == [
        "__version__",
        "MicroTechBgmDriver",
        "driver_factory",
    ]
