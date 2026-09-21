"""Public regression coverage for the final core review findings."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import bgmeter
import pytest

from bgmeter import (
    BleTransport, CompletionStatus, DriverRegistry, GlucoseRecord,
    MeterConnectionError, MeterIdentity, ProtocolError, RawCapture, ReadResult,
    TransportEndpoint, UnsupportedDeviceError, interpret_meter_datetime,
)
from test_manager import (
    FakeBleakClient, FakeDriver, FakeTransport,
    make_device, make_endpoint, make_manager,
)
from test_public_contract import FakeEntryPoint


def make_record(**kwargs):
    return GlucoseRecord(
        record_id="record-1", native_sequence=1, mmol_l=Decimal("5.6"),
        native_value=Decimal("5.6"), native_unit="mmol/L",
        measured_at=interpret_meter_datetime(datetime(2026, 9, 18), "UTC"),
        **kwargs,
    )


def make_result(**kwargs):
    return ReadResult(
        device=make_device(), records=(make_record(),),
        completion=CompletionStatus.COMPLETE,
        started_at=datetime(2026, 9, 18, tzinfo=UTC),
        ended_at=datetime(2026, 9, 18, tzinfo=UTC), received_count=1,
        **kwargs,
    )


def test_implementation_types_are_top_level_exports():
    required = {"GattService", "GattCharacteristic", "NotificationCallback", "MetadataValue"}
    assert required <= set(bgmeter.__all__)
    assert all(hasattr(bgmeter, name) for name in required)


@pytest.mark.asyncio
async def test_connected_inventory_preserves_service_relationship_and_snapshot():
    client = FakeBleakClient("device")
    characteristic = SimpleNamespace(uuid="2A18", handle=2, properties=["notify"])
    service = SimpleNamespace(uuid="1808", handle=1, characteristics=[characteristic])
    other = SimpleNamespace(uuid="180A", handle=3, characteristics=[
        SimpleNamespace(uuid="2A29", handle=4, properties=["read"]),
    ])
    client.services = [service, other]
    session = await BleTransport(client_factory=lambda target: client).connect(
        replace(make_endpoint(transport="ble"), service_uuids={"stale-advertisement"})
    )
    try:
        assert hasattr(session, "services"), "connected services must be public"
        inventory = session.services
        assert inventory == (
            bgmeter.GattService("1808", 1, (bgmeter.GattCharacteristic("2a18", 2, {"notify"}),)),
            bgmeter.GattService("180a", 3, (bgmeter.GattCharacteristic("2a29", 4, {"read"}),)),
        )
        assert not any(c.uuid == "2a29" for c in inventory[0].characteristics)
        characteristic.properties.append("write")
        service.characteristics.clear()
        client.services.clear()
        assert inventory[0].characteristics[0].properties == frozenset({"notify"})
        assert len(inventory) == 2
        with pytest.raises(FrozenInstanceError):
            inventory[0].uuid = "changed"
        with pytest.raises(FrozenInstanceError):
            inventory[0].characteristics[0].handle = 99
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_inventory_inspection_failure_is_typed_and_disconnects():
    class BrokenServicesClient(FakeBleakClient):
        @property
        def services(self):
            raise RuntimeError("service inspection failed")

        @services.setter
        def services(self, value):
            pass

    client = BrokenServicesClient("device")
    with pytest.raises(MeterConnectionError, match="service inspection failed"):
        await BleTransport(client_factory=lambda target: client).connect(
            make_endpoint(transport="ble")
        )
    assert client.disconnected


class FailingCloseTransport(FakeTransport):
    def __init__(self, error=None):
        super().__init__((make_endpoint(),))
        self.close_error = error or RuntimeError("disconnect failed")

    async def connect(self, endpoint):
        session = await super().connect(endpoint)

        async def close():
            session.closed = True
            raise self.close_error

        session.close = close
        return session


@pytest.mark.asyncio
async def test_one_shot_read_retains_records_and_evidence_after_close_failure():
    original = make_result(warnings=("original warning",), diagnostics={"attempts": [1]},
                           termination_reason="end_of_history", expected_count=1)

    class RecordDriver(FakeDriver):
        async def read_records(self, session, device, options):
            return original

    manager, _, transport = make_manager(RecordDriver(), FailingCloseTransport())
    result = await manager.read(make_device())
    assert result.records == original.records
    assert result.completion == CompletionStatus.PARTIAL
    assert result.received_count == result.expected_count == 1
    assert result.started_at == original.started_at
    assert result.ended_at >= original.ended_at
    assert result.termination_reason == "disconnect_failed"
    assert result.warnings[0] == "original warning"
    assert "disconnect failed" in result.warnings[-1]
    assert result.diagnostics["attempts"] == (1,)
    assert result.diagnostics["bgmeter.cleanup"]["previous_termination_reason"] == "end_of_history"
    assert result.diagnostics["bgmeter.cleanup"]["error_type"] == "RuntimeError"
    assert original.completion == CompletionStatus.COMPLETE
    assert transport.sessions[-1].closed


@pytest.mark.asyncio
async def test_close_failure_after_zero_record_truncated_read_returns_partial():
    original = replace(make_result(), records=(), completion=CompletionStatus.TRUNCATED,
                        termination_reason="already_stored", received_count=0)

    class UpToDateDriver(FakeDriver):
        async def read_records(self, session, device, options):
            return original

    manager, _, transport = make_manager(UpToDateDriver(), FailingCloseTransport())
    result = await manager.read(make_device())
    assert result.records == ()
    assert result.completion == CompletionStatus.PARTIAL
    assert result.termination_reason == "disconnect_failed"
    assert result.diagnostics["bgmeter.cleanup"]["previous_completion"] == "truncated"
    assert transport.sessions[-1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["open", "read", "probe"])
@pytest.mark.parametrize("primary_type", [ProtocolError, asyncio.CancelledError])
@pytest.mark.parametrize("cleanup_type", [RuntimeError, asyncio.CancelledError])
async def test_primary_exception_survives_cleanup_failure(operation, primary_type, cleanup_type):
    primary = primary_type("primary")
    cleanup = cleanup_type("cleanup")
    class BrokenDriver(FakeDriver):
        async def probe(self, session):
            raise primary

        async def read_records(self, session, device, options):
            raise primary

    manager, _, transport = make_manager(BrokenDriver(), FailingCloseTransport(cleanup))
    with pytest.raises(type(primary)) as caught:
        if operation == "probe":
            await manager.discover()
        elif operation == "read":
            await manager.read(make_device())
        else:
            async with manager.open(make_device()):
                raise primary
    assert caught.value is primary
    assert any("cleanup" in note for note in caught.value.__notes__)
    assert transport.sessions[-1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["open", "probe"])
async def test_close_only_failure_without_records_is_typed(operation):
    manager, _, _ = make_manager(transport=FailingCloseTransport())
    with pytest.raises(MeterConnectionError, match="disconnect failed") as caught:
        if operation == "probe":
            await manager.discover()
        else:
            async with manager.open(make_device()):
                pass
    assert isinstance(caught.value.__cause__, RuntimeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifiers", [("rejected", "valid"), ("valid", "rejected")])
async def test_probe_rejection_only_rejects_that_candidate(identifiers):
    class SelectiveDriver(FakeDriver):
        async def probe(self, session):
            if session.endpoint.identifier == "rejected":
                raise UnsupportedDeviceError("wrong connected services")
            return MeterIdentity(model="Valid")

    transport = FakeTransport(make_endpoint(identifier=item) for item in identifiers)
    manager, _, _ = make_manager(SelectiveDriver(), transport)
    devices = await manager.discover()
    assert [device.selector for device in devices] == ["fake:valid"]
    assert len(transport.sessions) == 2
    assert all(session.closed for session in transport.sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ProtocolError("bad packet"), MeterConnectionError("link lost")])
async def test_probe_transport_and_protocol_errors_are_not_candidate_rejections(failure):
    class BrokenDriver(FakeDriver):
        async def probe(self, session):
            raise failure

    manager, _, transport = make_manager(BrokenDriver())
    with pytest.raises(type(failure)) as caught:
        await manager.discover()
    assert caught.value is failure
    assert transport.sessions[0].closed


@pytest.mark.parametrize("model,field", [
    (lambda data: TransportEndpoint("fake", "id", metadata=data), "metadata"),
    (lambda data: MeterIdentity(metadata=data), "metadata"),
    (lambda data: make_record(driver_data=data), "driver_data"),
    (lambda data: make_result(diagnostics=data), "diagnostics"),
])
def test_public_metadata_is_recursively_snapshotted_and_frozen(model, field):
    data = {"nested": [{42: {"values": [Decimal("5.6"), bytearray(b"raw")], "tags": {"a"}}}]}
    frozen = getattr(model(data), field)
    data["nested"][0][42]["values"][1][0] = 0
    data["nested"][0][42]["values"].append("late")
    data["nested"][0][42]["tags"].add("late")
    assert frozen["nested"][0][42]["values"] == (Decimal("5.6"), b"raw")
    assert frozen["nested"][0][42]["tags"] == frozenset({"a"})
    with pytest.raises(TypeError):
        frozen["nested"][0][42]["new"] = True
    with pytest.raises(TypeError):
        frozen["nested"][0][42]["values"][0] = 0
    with pytest.raises(AttributeError):
        frozen["nested"][0][42]["tags"].add("bad")


def test_supported_metadata_scalars_are_preserved():
    scalars = (None, True, 7, 1.5, "text", b"bytes", Decimal("1.20"),
               datetime(2026, 9, 18, tzinfo=UTC), date(2026, 9, 18), time(12),
               timedelta(seconds=3), UUID(int=1))
    frozen = MeterIdentity(metadata={"scalars": scalars}).metadata["scalars"]
    assert frozen == scalars
    assert tuple(map(type, frozen)) == tuple(map(type, scalars))


@pytest.mark.parametrize("unsupported", [object(), {object(): "value"}])
def test_metadata_rejects_unsupported_objects_and_keys(unsupported):
    with pytest.raises(TypeError, match="metadata"):
        MeterIdentity(metadata={"bad": unsupported})


def test_metadata_rejects_cycles_but_allows_shared_children():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cyclic metadata"):
        MeterIdentity(metadata={"bad": cycle})
    child = [1]
    assert MeterIdentity(metadata={"a": child, "b": child}).metadata == {"a": (1,), "b": (1,)}


def test_raw_capture_snapshots_mutable_byte_buffers():
    buffer = bytearray(b"raw")
    raw = RawCapture(request=buffer, fragments=[buffer], response=memoryview(buffer), record=buffer)
    buffer[0] = 0
    assert raw.request == raw.response == raw.record == b"raw"
    assert raw.fragments == (b"raw",)


def test_duplicate_entry_point_errors_keep_stable_package_identity(monkeypatch):
    def broken_a():
        raise ValueError("package A failure")

    def broken_b():
        raise ValueError("package B failure")

    first = FakeEntryPoint("duplicate", broken_a)
    second = FakeEntryPoint("duplicate", broken_b)
    first.dist = SimpleNamespace(name="package-a", version="1.0")
    second.dist = SimpleNamespace(name="package-b", version="2.0")
    outputs = []
    for points in [(first, second), (second, first)]:
        monkeypatch.setattr("bgmeter.drivers.metadata.entry_points", lambda *, group: points)
        descriptors, errors = DriverRegistry().register_available_entry_points()
        assert descriptors == ()
        assert len(errors) == 2
        assert set(errors.values()) == {"package A failure", "package B failure"}
        assert any("package-a" in key for key in errors)
        assert any("package-b" in key for key in errors)
        outputs.append(errors)
    assert outputs[0] == outputs[1]


def test_duplicate_errors_use_distribution_metadata_name_fallback(monkeypatch):
    def broken():
        raise ValueError("cannot load")

    points = [FakeEntryPoint("duplicate", broken), FakeEntryPoint("duplicate", broken)]
    for point, name in zip(points, ("package-a", "package-b")):
        point.dist = SimpleNamespace(metadata={"Name": name}, version="1.0")
    monkeypatch.setattr("bgmeter.drivers.metadata.entry_points", lambda *, group: points)
    _, errors = DriverRegistry().register_available_entry_points()
    assert len(errors) == 2
    assert any("package-a" in key for key in errors)
    assert any("package-b" in key for key in errors)


def test_identical_entry_point_failures_are_not_overwritten(monkeypatch):
    def broken():
        raise ValueError("cannot load")

    points = [FakeEntryPoint("duplicate", broken) for _ in range(3)]
    monkeypatch.setattr("bgmeter.drivers.metadata.entry_points", lambda *, group: points)
    _, errors = DriverRegistry().register_available_entry_points()
    assert len(errors) == 3
    assert set(errors.values()) == {"cannot load"}


@pytest.mark.asyncio
async def test_task_cancellation_survives_disconnect_failure():
    entered = asyncio.Event()

    class WaitingDriver(FakeDriver):
        async def read_records(self, session, device, options):
            entered.set()
            await asyncio.Event().wait()

    manager, _, transport = make_manager(WaitingDriver(), FailingCloseTransport())
    task = asyncio.create_task(manager.read(make_device()))
    await entered.wait()
    task.cancel("user interrupted")
    with pytest.raises(asyncio.CancelledError, match="user interrupted") as caught:
        await task
    assert task.cancelled()
    assert transport.sessions[-1].closed
    assert "disconnect failed" in caught.value.__notes__[-1]


@pytest.mark.asyncio
async def test_cancellation_during_close_is_not_converted_to_a_partial_result():
    class RecordDriver(FakeDriver):
        async def read_records(self, session, device, options):
            return make_result()

    manager, _, _ = make_manager(RecordDriver(), FailingCloseTransport(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await manager.read(make_device())


@pytest.mark.asyncio
async def test_rejected_candidate_still_reports_disconnect_failure():
    class RejectingDriver(FakeDriver):
        async def probe(self, session):
            raise UnsupportedDeviceError("not this candidate")

    manager, _, transport = make_manager(RejectingDriver(), FailingCloseTransport())
    with pytest.raises(MeterConnectionError, match="disconnect failed"):
        await manager.discover()
    assert transport.sessions[-1].closed


@pytest.mark.asyncio
async def test_all_rejected_candidates_raise_unsupported_after_closing_all_sessions():
    class RejectingDriver(FakeDriver):
        async def probe(self, session):
            raise UnsupportedDeviceError("not this candidate")

    transport = FakeTransport([make_endpoint(identifier="a"), make_endpoint(identifier="b")])
    manager, _, _ = make_manager(RejectingDriver(), transport)
    with pytest.raises(UnsupportedDeviceError, match="not supported"):
        await manager.discover()
    assert len(transport.sessions) == 2
    assert all(session.closed for session in transport.sessions)


def test_inventory_models_copy_caller_containers():
    properties = {"read"}
    characteristic = bgmeter.GattCharacteristic("2A29", 2, properties)
    characteristics = [characteristic]
    service = bgmeter.GattService("180A", 1, characteristics)
    properties.add("write")
    characteristics.clear()
    assert service.characteristics == (characteristic,)
    assert service.characteristics[0].properties == frozenset({"read"})


@pytest.mark.parametrize("completion", list(CompletionStatus))
@pytest.mark.asyncio
async def test_cleanup_evidence_preserves_prior_partial_result_and_diagnostics(completion):
    original = replace(make_result(), completion=completion,
                       diagnostics={"bgmeter.cleanup": {"earlier": ["evidence"]}},
                       duplicate_count=2, rejected_count=3, retry_count=4)

    class RecordDriver(FakeDriver):
        async def read_records(self, session, device, options):
            return original

    manager, _, _ = make_manager(RecordDriver(), FailingCloseTransport())
    result = await manager.read(make_device())
    assert result.completion == CompletionStatus.PARTIAL
    assert (result.duplicate_count, result.rejected_count, result.retry_count) == (2, 3, 4)
    evidence = result.diagnostics["bgmeter.cleanup"]
    assert evidence["previous_completion"] == completion.value
    assert evidence["previous_diagnostic"] == {"earlier": ("evidence",)}
    with pytest.raises(TypeError):
        evidence["message"] = "changed"
