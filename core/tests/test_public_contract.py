from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import get_type_hints

import pytest

from bgmeter import (
    DRIVER_API_VERSION,
    CompletionStatus,
    DriverDescriptor,
    DriverMatch,
    DriverRegistry,
    GlucoseRecord,
    MeasurementTime,
    MeterDevice,
    MeterDriver,
    MeterIdentity,
    RawCapture,
    ReadOptions,
    ReadResult,
    TransportEndpoint,
    TransportSession,
    interpret_meter_datetime,
)


class FakeDriver:
    driver_id = "fake"
    display_name = "Fake meter"
    api_version = DRIVER_API_VERSION
    supported_transports = frozenset({"fake"})
    known_meter_identities = ("Test meter",)

    def match_candidate(self, endpoint):
        return DriverMatch(90, ("test",))

    async def probe(self, session):
        return MeterIdentity(manufacturer="Example", model="Meter")

    async def read_records(self, session, device, options):
        return session.result_for(device)


def fake_factory():
    return FakeDriver()


def make_device() -> MeterDevice:
    return MeterDevice(
        selector="fake:device-1",
        driver_id="fake",
        endpoint=TransportEndpoint(
            transport="fake",
            identifier="device-1",
            service_uuids={"1808"},
            metadata={"rssi": -44},
        ),
        identity=MeterIdentity(manufacturer="Example", model="Meter"),
        match=DriverMatch(90, ["test"]),
    )


def test_public_models_are_frozen_and_copy_mutable_inputs():
    fragments = [b"fragment"]
    flags = {"before_meal": True}
    driver_data = {"native_status": 3}
    diagnostics = {"attempts": 1}
    timestamp = MeasurementTime(
        meter_datetime=datetime(2026, 9, 16, 19, 10, 16),
        measured_at_local=datetime(2026, 9, 16, 19, 10, 16, tzinfo=UTC),
        measured_at_utc=datetime(2026, 9, 16, 19, 10, 16, tzinfo=UTC),
        timezone="UTC",
        utc_offset_seconds=0,
    )
    raw = RawCapture(
        request=b"request",
        fragments=fragments,
        response=b"response",
        record=bytes.fromhex("00 86"),
    )
    record = GlucoseRecord(
        record_id="fake:device-1:2",
        native_sequence=2,
        mmol_l=Decimal("7.44"),
        native_value=Decimal("134"),
        native_unit="mg/dL",
        measured_at=timestamp,
        flags=flags,
        source_device_id="device-1",
        source_driver_id="fake",
        driver_data=driver_data,
        raw=raw,
    )
    result = ReadResult(
        device=make_device(),
        records=[record],
        completion=CompletionStatus.COMPLETE,
        started_at=datetime(2026, 9, 16, 17, 10, tzinfo=UTC),
        ended_at=datetime(2026, 9, 16, 17, 11, tzinfo=UTC),
        expected_count=1,
        received_count=1,
        warnings=["clock assumed correct"],
        diagnostics=diagnostics,
    )

    fragments.append(b"late")
    flags["before_meal"] = False
    driver_data["native_status"] = 4
    diagnostics["attempts"] = 2

    assert record.mmol_l == Decimal("7.44")
    assert record.raw.record == bytes.fromhex("00 86")
    assert raw.fragments == (b"fragment",)
    assert record.flags == {"before_meal": True}
    assert isinstance(record.flags, MappingProxyType)
    assert record.driver_data == {"native_status": 3}
    assert result.records == (record,)
    assert result.warnings == ("clock assumed correct",)
    assert result.diagnostics == {"attempts": 1}
    assert result.device.endpoint.service_uuids == frozenset({"1808"})
    assert CompletionStatus.COMPLETE.value == "complete"
    assert CompletionStatus.PARTIAL.value == "partial"
    assert CompletionStatus.UNKNOWN.value == "unknown"
    with pytest.raises(FrozenInstanceError):
        record.record_id = "changed"
    with pytest.raises(TypeError):
        record.flags["before_meal"] = False


def test_read_options_are_frozen():
    options = ReadOptions(timezone="Europe/Stockholm", request_timeout=2.5, retries=4)

    assert options.timezone == "Europe/Stockholm"
    with pytest.raises(FrozenInstanceError):
        options.retries = 0


def test_completion_status_includes_truncated():
    assert CompletionStatus.TRUNCATED.value == "truncated"
    assert [status.value for status in CompletionStatus] == [
        "complete",
        "partial",
        "unknown",
        "truncated",
    ]


def test_read_options_carry_optional_read_limits():
    default = ReadOptions()

    assert default.newest_count is None
    assert default.known_record_ids == frozenset()

    bounded = ReadOptions(newest_count=10, known_record_ids={"fake:meter-1:7"})

    assert bounded.newest_count == 10
    assert bounded.known_record_ids == frozenset({"fake:meter-1:7"})
    with pytest.raises(FrozenInstanceError):
        bounded.newest_count = 5


@pytest.mark.parametrize("newest_count", [0, -1])
def test_read_options_reject_non_positive_newest_count(newest_count):
    with pytest.raises(ValueError, match="newest_count must be at least 1"):
        ReadOptions(newest_count=newest_count)


def test_interpret_meter_datetime_preserves_wall_clock_and_adds_utc():
    result = interpret_meter_datetime(
        datetime(2026, 9, 16, 19, 10, 16),
        "Europe/Stockholm",
    )

    assert result.meter_datetime.isoformat() == "2026-09-16T19:10:16"
    assert result.measured_at_local.isoformat() == "2026-09-16T19:10:16+02:00"
    assert result.measured_at_utc.isoformat() == "2026-09-16T17:10:16+00:00"
    assert result.timezone == "Europe/Stockholm"
    assert result.utc_offset_seconds == 7200


def test_interpret_meter_datetime_uses_detected_host_zone(monkeypatch):
    monkeypatch.setattr("bgmeter.time.get_localzone_name", lambda: "Europe/Stockholm")

    result = interpret_meter_datetime(datetime(2026, 9, 17, 6, 25))

    assert result.timezone == "Europe/Stockholm"
    assert result.measured_at_utc.isoformat() == "2026-09-17T04:25:00+00:00"


def test_interpret_meter_datetime_rejects_aware_values():
    with pytest.raises(ValueError, match="must not already contain a timezone"):
        interpret_meter_datetime(datetime(2026, 9, 17, 6, 25, tzinfo=UTC))


def test_registries_are_isolated_and_support_lifecycle():
    first = DriverRegistry()
    second = DriverRegistry()

    descriptor = first.register(fake_factory, source="test")

    assert descriptor.driver_id == "fake"
    assert descriptor.display_name == "Fake meter"
    assert first.get("fake").driver_id == "fake"
    assert [item.driver_id for item in first.descriptors()] == ["fake"]
    assert second.descriptors() == ()
    assert first.unregister("fake") == descriptor
    with pytest.raises(KeyError):
        first.get("fake")


def test_duplicate_and_incompatible_drivers_are_rejected():
    registry = DriverRegistry()
    registry.register(fake_factory, source="test")

    with pytest.raises(ValueError, match="already registered"):
        registry.register(fake_factory, source="duplicate")

    class IncompatibleDriver(FakeDriver):
        driver_id = "incompatible"
        api_version = 999

    with pytest.raises(ValueError, match="driver API version"):
        registry.register(IncompatibleDriver, source="test")


def test_meter_driver_type_hints_resolve_public_transport_session():
    assert get_type_hints(MeterDriver.probe) == {
        "session": TransportSession,
        "return": MeterIdentity,
    }
    assert get_type_hints(MeterDriver.read_records) == {
        "session": TransportSession,
        "device": MeterDevice,
        "options": ReadOptions,
        "return": ReadResult,
    }


@pytest.mark.parametrize("method_name", ["probe", "read_records"])
def test_registry_rejects_synchronous_async_contract_methods(method_name):
    class SynchronousDriver(FakeDriver):
        driver_id = f"sync-{method_name}"

    setattr(SynchronousDriver, method_name, lambda *args: None)

    with pytest.raises(ValueError, match=rf"{method_name}\(\) must be async"):
        DriverRegistry().register(SynchronousDriver, source="test")


def test_driver_descriptor_copies_capability_sequences():
    transports = {"fake"}
    identities = ["Test meter"]
    descriptor = DriverDescriptor(
        driver_id="direct",
        display_name="Direct descriptor",
        api_version=DRIVER_API_VERSION,
        supported_transports=transports,
        known_meter_identities=identities,
        source="test",
    )

    transports.add("late")
    identities.append("Late meter")

    assert descriptor.supported_transports == frozenset({"fake"})
    assert descriptor.known_meter_identities == ("Test meter",)


class FakeDistribution:
    name = "example-driver-package"
    version = "2.4.1"
    metadata = {"Name": name}


class FakeEntryPoint:
    group = "bgmeter.drivers"
    value = "example_driver:create_driver"
    dist = FakeDistribution()

    def __init__(self, name, factory):
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


def test_entry_point_discovery_and_named_registration_include_provenance(monkeypatch):
    entry_point = FakeEntryPoint("example", fake_factory)

    def fake_entry_points(*, group):
        assert group == "bgmeter.drivers"
        return (entry_point,)

    monkeypatch.setattr("bgmeter.drivers.metadata.entry_points", fake_entry_points)
    registry = DriverRegistry()

    assert registry.available_entry_points() == (entry_point,)
    descriptor = registry.register_entry_point("example")

    assert descriptor.entry_point == "example"
    assert descriptor.package_name == "example-driver-package"
    assert descriptor.package_version == "2.4.1"
    assert registry.get("fake").display_name == "Fake meter"


def test_register_available_entry_points_keeps_compatible_plugins(monkeypatch):
    class IncompatibleDriver(FakeDriver):
        driver_id = "incompatible"
        api_version = 999

    entry_points = (
        FakeEntryPoint("working", fake_factory),
        FakeEntryPoint("broken", IncompatibleDriver),
    )
    monkeypatch.setattr(
        "bgmeter.drivers.metadata.entry_points",
        lambda *, group: entry_points,
    )
    registry = DriverRegistry()

    descriptors, errors = registry.register_available_entry_points()

    assert [item.driver_id for item in descriptors] == ["fake"]
    assert set(errors) == {"broken"}
    assert "driver API version" in errors["broken"]
    assert registry.get("fake").driver_id == "fake"
