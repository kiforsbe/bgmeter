import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bgmeter import (
    DRIVER_API_VERSION,
    AmbiguousDeviceError,
    BleTransport,
    CompletionStatus,
    DiscoveryError,
    DriverMatch,
    DriverRegistry,
    GattSession,
    MeterConnectionError,
    MeterDevice,
    MeterIdentity,
    MeterManager,
    MeterTransport,
    ProgressLevel,
    ReadOptions,
    ReadResult,
    TransportEndpoint,
    TransportSession,
    UnsupportedDeviceError,
    discover_meters,
    read_meter,
)


class FakeSession:
    transport = "fake"

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.closed = False

    async def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, endpoints=()):
        self.name = "fake"
        self.endpoints = tuple(endpoints)
        self.discover_timeouts = []
        self.sessions = []

    async def discover(self, *, timeout=5.0):
        self.discover_timeouts.append(timeout)
        return self.endpoints

    async def connect(self, endpoint):
        session = FakeSession(endpoint)
        self.sessions.append(session)
        return session


class FakeDriver:
    display_name = "Fake meter"
    api_version = DRIVER_API_VERSION
    known_meter_identities = ("Test meter",)

    def __init__(
        self,
        driver_id="fake",
        *,
        confidence=90,
        supported_transports=frozenset({"fake"}),
        read_gate=None,
    ):
        self.driver_id = driver_id
        self.confidence = confidence
        self.supported_transports = supported_transports
        self.match_calls = []
        self.probe_sessions = []
        self.read_sessions = []
        self.read_options = []
        self.read_gate = read_gate

    def match_candidate(self, endpoint):
        self.match_calls.append(endpoint)
        return DriverMatch(self.confidence, (f"matched by {self.driver_id}",))

    async def probe(self, session):
        self.probe_sessions.append(session)
        return MeterIdentity(manufacturer="Example", model="Meter")

    async def read_records(self, session, device, options):
        self.read_sessions.append(session)
        self.read_options.append(options)
        if self.read_gate is not None:
            await self.read_gate.wait()
        instant = datetime(2026, 9, 18, tzinfo=UTC)
        return ReadResult(
            device=device,
            records=(),
            completion=CompletionStatus.COMPLETE,
            started_at=instant,
            ended_at=instant,
        )


def make_endpoint(*, transport="fake", identifier="device-1"):
    return TransportEndpoint(
        transport=transport,
        identifier=identifier,
        name="Test meter",
        service_uuids={"1808"},
        metadata={"rssi": -44},
    )


def make_device(*, transport="fake"):
    endpoint = make_endpoint(transport=transport)
    return MeterDevice(
        selector=f"{transport}:device-1",
        driver_id="fake",
        endpoint=endpoint,
        identity=MeterIdentity(manufacturer="Example", model="Meter"),
        match=DriverMatch(90, ("test",)),
    )


def make_manager(driver=None, transport=None, progress=None):
    driver = driver or FakeDriver()
    transport = transport or FakeTransport((make_endpoint(),))
    registry = DriverRegistry()
    registry.register(lambda: driver, source="test")
    manager = MeterManager(
        registry=registry, transports=(transport,), progress=progress
    )
    return manager, driver, transport


class FakeScanner:
    def __init__(self, advertisements):
        self.discovered_devices_and_advertisement_data = advertisements
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeBleakClient:
    def __init__(self, target, *, connect_error=None):
        self.target = target
        self.connect_error = connect_error
        self.connected = False
        self.disconnected = False
        self.writes = []
        self.stopped_notifications = []
        self.services = []

    async def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def read_gatt_char(self, characteristic):
        return bytearray(b"reply")

    async def write_gatt_char(self, characteristic, data, *, response=None):
        self.writes.append((characteristic, bytes(data), response))

    async def start_notify(self, characteristic, callback):
        result = callback("sender", bytearray(b"notice"))
        if result is not None:
            await result

    async def stop_notify(self, characteristic):
        self.stopped_notifications.append(characteristic)


@pytest.mark.asyncio
async def test_ble_transport_maps_advertisements_to_generic_endpoints():
    device = SimpleNamespace(address="AA:BB", name="Backend name")
    advertisement = SimpleNamespace(
        local_name="Advertised meter",
        service_uuids=["1808", "180A"],
        manufacturer_data={42: b"maker"},
        service_data={"1808": b"service"},
        tx_power=-8,
        rssi=-51,
    )
    scanner = FakeScanner({device.address: (device, advertisement)})
    transport = BleTransport(scanner_factory=lambda: scanner)

    endpoints = await transport.discover(timeout=0)

    assert scanner.started is True
    assert scanner.stopped is True
    assert endpoints == (
        TransportEndpoint(
            transport="ble",
            identifier="AA:BB",
            name="Advertised meter",
            service_uuids={"1808", "180A"},
            metadata={
                "manufacturer_data": {42: b"maker"},
                "service_data": {"1808": b"service"},
                "tx_power": -8,
                "rssi": -51,
            },
            handle=device,
        ),
    )


@pytest.mark.parametrize(
    ("metadata_key", "entry_key", "replacement"),
    (
        ("manufacturer_data", 42, b"changed"),
        ("service_data", "1808", b"changed"),
    ),
)
@pytest.mark.asyncio
async def test_nested_advertisement_metadata_is_immutable(
    metadata_key,
    entry_key,
    replacement,
):
    device = SimpleNamespace(address="AA:BB", name="Backend name")
    advertisement = SimpleNamespace(
        local_name="Advertised meter",
        service_uuids=["1808"],
        manufacturer_data={42: b"maker"},
        service_data={"1808": b"service"},
        tx_power=-8,
        rssi=-51,
    )
    scanner = FakeScanner({device.address: (device, advertisement)})
    endpoint = (
        await BleTransport(scanner_factory=lambda: scanner).discover(timeout=0)
    )[0]

    with pytest.raises(TypeError):
        endpoint.metadata[metadata_key][entry_key] = replacement


@pytest.mark.asyncio
async def test_ble_transport_translates_scanner_constructor_failure():
    def broken_scanner_factory():
        raise RuntimeError("scanner construction failed")

    transport = BleTransport(scanner_factory=broken_scanner_factory)

    with pytest.raises(DiscoveryError, match="BLE discovery failed"):
        await transport.discover(timeout=0)


@pytest.mark.asyncio
async def test_ble_discovery_preserves_cancellation_when_scanner_stop_fails(
    monkeypatch,
):
    entered_sleep = asyncio.Event()
    cancellation = []

    async def wait_for_cancellation(timeout):
        entered_sleep.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cancellation.append(error)
            raise

    class StopFailingScanner(FakeScanner):
        def __init__(self):
            super().__init__({})
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1
            raise RuntimeError("scanner stop failed")

    scanner = StopFailingScanner()
    monkeypatch.setattr("bgmeter.transports.bleak.asyncio.sleep", wait_for_cancellation)
    task = asyncio.create_task(
        BleTransport(scanner_factory=lambda: scanner).discover(timeout=60)
    )
    await entered_sleep.wait()
    task.cancel("scan cancelled")

    with pytest.raises(asyncio.CancelledError, match="scan cancelled") as caught:
        await task

    assert caught.value is cancellation[0]
    assert task.cancelled()
    assert scanner.stop_calls == 1
    assert "scanner stop failed" in caught.value.__notes__[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("start_error", [None, RuntimeError("scanner start failed")])
async def test_ble_discovery_translates_cleanup_failure_after_ordinary_outcome(
    start_error,
):
    class StopFailingScanner(FakeScanner):
        def __init__(self):
            super().__init__({})
            self.stop_calls = 0

        async def start(self):
            if start_error is not None:
                raise start_error
            await super().start()

        async def stop(self):
            self.stop_calls += 1
            raise RuntimeError("scanner stop failed")

    scanner = StopFailingScanner()

    with pytest.raises(DiscoveryError, match="BLE discovery failed") as caught:
        await BleTransport(scanner_factory=lambda: scanner).discover(timeout=0)

    assert scanner.stop_calls == 1
    if start_error is None:
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert str(caught.value.__cause__) == "scanner stop failed"
    else:
        assert caught.value.__cause__ is start_error
        assert "scanner stop failed" in start_error.__notes__[-1]


@pytest.mark.asyncio
async def test_ble_transport_translates_client_constructor_failure():
    def broken_client_factory(target):
        raise RuntimeError("client construction failed")

    transport = BleTransport(client_factory=broken_client_factory)

    with pytest.raises(MeterConnectionError, match="BLE connection failed"):
        await transport.connect(make_endpoint(transport="ble"))


@pytest.mark.asyncio
async def test_ble_session_exposes_generic_gatt_capabilities_and_closes():
    clients = []

    def client_factory(target):
        client = FakeBleakClient(target)
        clients.append(client)
        return client

    endpoint = make_endpoint(transport="ble")
    transport = BleTransport(client_factory=client_factory)
    session = await transport.connect(endpoint)
    notifications = []

    async def notified(sender, data):
        notifications.append((sender, data))

    assert isinstance(session, TransportSession)
    assert isinstance(session, GattSession)
    assert await session.read_gatt_char("read-char") == b"reply"
    await session.write_gatt_char("write-char", b"request", response=True)
    await session.start_notify("notify-char", notified)
    await session.stop_notify("notify-char")
    await session.close()

    assert clients[0].target == endpoint.identifier
    assert clients[0].writes == [("write-char", b"request", True)]
    assert notifications == [("sender", b"notice")]
    assert clients[0].stopped_notifications == ["notify-char"]
    assert clients[0].disconnected is True


@pytest.mark.asyncio
async def test_ble_transport_translates_scan_and_connection_failures():
    class BrokenScanner(FakeScanner):
        async def start(self):
            raise RuntimeError("radio unavailable")

    transport = BleTransport(scanner_factory=lambda: BrokenScanner({}))
    with pytest.raises(DiscoveryError, match="BLE discovery failed"):
        await transport.discover(timeout=0)

    client = FakeBleakClient("unused", connect_error=RuntimeError("link failed"))
    transport = BleTransport(client_factory=lambda target: client)
    with pytest.raises(MeterConnectionError, match="BLE connection failed"):
        await transport.connect(make_endpoint(transport="ble"))
    assert client.disconnected is True


@pytest.mark.asyncio
async def test_discovery_matches_supported_transport_probes_and_closes_session():
    manager, driver, transport = make_manager()

    class WrongTransportDriver(FakeDriver):
        def match_candidate(self, endpoint):
            raise AssertionError("unsupported driver must not see fake endpoints")

    wrong_driver = WrongTransportDriver(
        "serial-only", supported_transports=frozenset({"serial"})
    )
    manager.registry.register(lambda: wrong_driver, source="test")

    devices = await manager.discover(timeout=1.25)

    assert devices == (
        MeterDevice(
            selector="fake:device-1",
            driver_id="fake",
            endpoint=make_endpoint(),
            identity=MeterIdentity(manufacturer="Example", model="Meter"),
            match=DriverMatch(90, ("matched by fake",)),
        ),
    )
    assert transport.discover_timeouts == [1.25]
    assert driver.match_calls == [make_endpoint()]
    assert wrong_driver.match_calls == []
    assert driver.probe_sessions == transport.sessions
    assert transport.sessions[0].closed is True


@pytest.mark.asyncio
async def test_open_scopes_read_to_connection_and_always_closes():
    manager, driver, transport = make_manager()
    device = make_device()

    async with manager.open(device) as connected:
        assert connected.device is device
        result = await connected.read_records(ReadOptions(retries=1))
        assert result.device is device
        assert transport.sessions[-1].closed is False

    assert driver.read_sessions == [transport.sessions[-1]]
    assert transport.sessions[-1].closed is True


@pytest.mark.asyncio
async def test_equal_highest_confidence_driver_matches_are_ambiguous():
    manager, _, _ = make_manager()
    second = FakeDriver("also-fake", confidence=90)
    manager.registry.register(lambda: second, source="test")

    with pytest.raises(AmbiguousDeviceError, match="fake:device-1"):
        await manager.discover()


@pytest.mark.asyncio
async def test_probe_rejection_falls_back_to_lower_confidence_driver():
    class RejectingDriver(FakeDriver):
        async def probe(self, session):
            self.probe_sessions.append(session)
            raise UnsupportedDeviceError("connected services do not match")

    high = RejectingDriver("high", confidence=90)
    low = FakeDriver("low", confidence=70)
    manager, _, transport = make_manager(high)
    manager.registry.register(lambda: low, source="test")

    devices = await manager.discover()

    assert [device.driver_id for device in devices] == ["low"]
    assert high.probe_sessions == [transport.sessions[0]]
    assert low.probe_sessions == [transport.sessions[1]]
    assert all(session.closed for session in transport.sessions)


@pytest.mark.asyncio
async def test_lower_confidence_tier_ambiguity_is_raised_before_probing_tier():
    class RejectingDriver(FakeDriver):
        async def probe(self, session):
            self.probe_sessions.append(session)
            raise UnsupportedDeviceError("connected services do not match")

    high = RejectingDriver("high", confidence=90)
    lower_a = FakeDriver("lower-a", confidence=70)
    lower_b = FakeDriver("lower-b", confidence=70)
    manager, _, transport = make_manager(high)
    manager.registry.register(lambda: lower_a, source="test")
    manager.registry.register(lambda: lower_b, source="test")

    with pytest.raises(AmbiguousDeviceError, match="lower-a, lower-b"):
        await manager.discover()

    assert len(transport.sessions) == 1
    assert transport.sessions[0].closed is True
    assert lower_a.probe_sessions == []
    assert lower_b.probe_sessions == []


@pytest.mark.asyncio
async def test_all_confidence_tiers_reject_after_closing_each_session():
    class RejectingDriver(FakeDriver):
        async def probe(self, session):
            self.probe_sessions.append(session)
            raise UnsupportedDeviceError("connected services do not match")

    drivers = [
        RejectingDriver("high", confidence=90),
        RejectingDriver("middle", confidence=70),
        RejectingDriver("low", confidence=50),
    ]
    manager, _, transport = make_manager(drivers[0])
    for driver in drivers[1:]:
        manager.registry.register(lambda driver=driver: driver, source="test")

    with pytest.raises(UnsupportedDeviceError, match="not supported"):
        await manager.discover()

    assert len(transport.sessions) == 3
    assert all(session.closed for session in transport.sessions)


@pytest.mark.asyncio
async def test_open_rejects_device_for_unconfigured_transport():
    manager, _, _ = make_manager()

    with pytest.raises(UnsupportedDeviceError, match="serial"):
        async with manager.open(make_device(transport="serial")):
            pass


@pytest.mark.asyncio
async def test_cancellation_propagates_after_connection_cleanup():
    gate = asyncio.Event()
    manager, _, transport = make_manager(FakeDriver(read_gate=gate))

    async def read_until_cancelled():
        async with manager.open(make_device()) as connected:
            await connected.read_records()

    task = asyncio.create_task(read_until_cancelled())
    while not transport.sessions:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.sessions[-1].closed is True


def test_default_loads_entry_points_and_ble_but_explicit_registry_stays_isolated(
    monkeypatch,
):
    loaded = []
    default_transport = FakeTransport()

    def register_available(registry):
        loaded.append(registry)
        registry.register(FakeDriver, source="entry-point")
        return registry.descriptors(), {}

    monkeypatch.setattr(
        DriverRegistry, "register_available_entry_points", register_available
    )
    monkeypatch.setattr("bgmeter.manager.BleTransport", lambda: default_transport)

    default_manager = MeterManager.default()
    explicit_registry = DriverRegistry()
    explicit_manager = MeterManager(
        registry=explicit_registry,
        transports=(FakeTransport(),),
    )

    assert loaded == [default_manager.registry]
    assert default_manager.transports == (default_transport,)
    assert [item.driver_id for item in default_manager.registry.descriptors()] == [
        "fake"
    ]
    assert explicit_manager.registry is explicit_registry
    assert explicit_registry.descriptors() == ()


@pytest.mark.asyncio
async def test_one_shot_helpers_use_manager_discovery_and_open_lifecycle():
    manager, _, transport = make_manager()

    devices = await discover_meters(manager=manager, timeout=2.0)
    result = await read_meter(devices[0], manager=manager)

    assert result.device == devices[0]
    assert transport.discover_timeouts == [2.0]
    assert len(transport.sessions) == 2
    assert all(session.closed for session in transport.sessions)


def test_transport_protocols_accept_structural_implementations():
    transport = FakeTransport()
    session = FakeSession(make_endpoint())

    assert isinstance(transport, MeterTransport)
    assert isinstance(session, TransportSession)


def meter_specific_markers(source):
    source = source.casefold()
    forbidden = ("microtech", "gochek", "wellion", "ffe0", "ffe1")
    markers = [term for term in forbidden if term in source]
    if re.search(r"\b\w*(?:command|cmd|opcode)\w*\b[^\n\w]*(?:0x0*5|5)\b", source):
        markers.append("command 0x05")
    return markers


@pytest.mark.parametrize("source", [
    'SERVICE = "0000FFE0-0000-1000-8000-00805f9b34fb"',
    'HISTORY_COMMAND = 0x05',
    'command = 5',
    'send_command(0x05)',
    '# command `0x05` retrieves history',
])
def test_source_boundary_detects_protocol_markers(source):
    assert meter_specific_markers(source)


@pytest.mark.parametrize("source", ['timeout = 5', 'retries = 5', 'value = 0x05'])
def test_source_boundary_allows_unrelated_numeric_five(source):
    assert not meter_specific_markers(source)


def test_core_source_has_no_meter_specific_knowledge():
    source_root = Path(__file__).parents[1] / "src" / "bgmeter"

    for source_file in source_root.rglob("*.py"):
        source = source_file.read_text(encoding="utf-8").casefold()
        assert not meter_specific_markers(source), f"protocol knowledge in {source_file}"



def _summary(events):
    return [(event.level, event.message) for event in events]


@pytest.mark.asyncio
async def test_discover_reports_plain_language_progress():
    events = []
    manager, _, _ = make_manager(progress=events.append)

    await manager.discover(timeout=2.0)

    assert _summary(events) == [
        (ProgressLevel.INFO, "Looking for meters nearby (2 s)..."),
        (ProgressLevel.DETAIL, "Found 1 device nearby."),
        (ProgressLevel.INFO, "Found Meter (device-1)."),
    ]


@pytest.mark.asyncio
async def test_discover_reports_unmatched_endpoints_as_detail():
    class NoMatchDriver(FakeDriver):
        def match_candidate(self, endpoint):
            return None

    events = []
    manager, _, _ = make_manager(driver=NoMatchDriver(), progress=events.append)

    with pytest.raises(UnsupportedDeviceError):
        await manager.discover(timeout=2.0)

    assert _summary(events)[2:] == [
        (ProgressLevel.DETAIL, "Skipped 'Test meter': not a supported meter."),
    ]


@pytest.mark.asyncio
async def test_discover_reports_a_failed_probe_as_detail():
    class ProbeFailsDriver(FakeDriver):
        async def probe(self, session):
            raise UnsupportedDeviceError("not ours")

    events = []
    manager, _, _ = make_manager(driver=ProbeFailsDriver(), progress=events.append)

    with pytest.raises(UnsupportedDeviceError):
        await manager.discover(timeout=2.0)

    assert _summary(events)[2:] == [
        (
            ProgressLevel.DETAIL,
            "'Test meter' did not respond like a supported meter.",
        ),
    ]


@pytest.mark.asyncio
async def test_read_reports_connect_completion_and_disconnect():
    events = []
    manager, _, _ = make_manager(progress=events.append)

    await manager.read(make_device())

    assert _summary(events) == [
        (ProgressLevel.INFO, "Connecting to Meter..."),
        (ProgressLevel.INFO, "Read 0 records. All records were received."),
        (ProgressLevel.DETAIL, "Disconnecting from the meter."),
    ]


@pytest.mark.asyncio
async def test_read_options_progress_prefers_the_callers_callback():
    def manager_callback(event):
        pass

    def caller_callback(event):
        pass

    manager, driver, _ = make_manager(progress=manager_callback)

    await manager.read(make_device())
    await manager.read(make_device(), ReadOptions(progress=caller_callback))

    assert driver.read_options[0].progress is manager_callback
    assert driver.read_options[1].progress is caller_callback


@pytest.mark.asyncio
async def test_read_without_any_progress_callback_leaves_options_unset():
    manager, driver, _ = make_manager()

    await manager.read(make_device())

    assert driver.read_options[0].progress is None


@pytest.mark.asyncio
async def test_a_failing_progress_callback_does_not_break_operations():
    def broken(event):
        raise RuntimeError("reporter exploded")

    manager, _, _ = make_manager(progress=broken)

    devices = await manager.discover(timeout=0.0)
    result = await manager.read(devices[0])

    assert result.completion is CompletionStatus.COMPLETE


class BrokenPoint:
    name = "broken"
    value = "broken:factory"
    dist = None

    def load(self):
        raise ImportError("no module named broken")


class FailingCloseSession(FakeSession):
    async def close(self):
        raise OSError("close exploded")


class FailingCloseTransport(FakeTransport):
    async def connect(self, endpoint):
        session = FailingCloseSession(endpoint)
        self.sessions.append(session)
        return session


class LoggingClient:
    services = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def write_gatt_char(self, characteristic, data, response=None):
        pass

    async def read_gatt_char(self, characteristic):
        return bytearray(b"\xaa\xbb")


def test_bgmeter_package_logger_has_a_null_handler():
    handlers = logging.getLogger("bgmeter").handlers

    assert any(isinstance(handler, logging.NullHandler) for handler in handlers)


@pytest.mark.asyncio
async def test_manager_logs_discovery_and_read_milestones_at_info(caplog):
    manager, _, _ = make_manager()

    with caplog.at_level(logging.INFO, logger="bgmeter"):
        devices = await manager.discover(timeout=0.0)
        await manager.read(devices[0])

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "bgmeter.manager"
    ]
    assert any(message.startswith("discovery started") for message in messages)
    assert any("identified" in message and "driver=fake" in message for message in messages)
    assert any(message.startswith("opening fake:device-1") for message in messages)
    assert any(
        message.startswith("read finished") and "completion=complete" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_probe_rejection_is_a_warning_not_an_error(caplog):
    class ProbeFailsDriver(FakeDriver):
        async def probe(self, session):
            raise UnsupportedDeviceError("not ours")

    manager, _, _ = make_manager(driver=ProbeFailsDriver())

    with caplog.at_level(logging.DEBUG, logger="bgmeter"):
        with pytest.raises(UnsupportedDeviceError):
            await manager.discover(timeout=0.0)

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "bgmeter.manager"
    ]
    assert len(warnings) == 1
    assert "rejected endpoint" in warnings[0].getMessage()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_absorbed_session_cleanup_failure_is_logged_at_error(caplog):
    manager, _, _ = make_manager(
        transport=FailingCloseTransport((make_endpoint(),))
    )

    with caplog.at_level(logging.ERROR, logger="bgmeter"):
        with pytest.raises(RuntimeError, match="body failed"):
            async with manager.open(make_device()):
                raise RuntimeError("body failed")

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.name == "bgmeter.manager"
    assert "OSError" in record.getMessage()
    assert "close exploded" in record.getMessage()


def test_registry_logs_an_absorbed_entry_point_failure_at_error(monkeypatch, caplog):
    registry = DriverRegistry()
    monkeypatch.setattr(
        DriverRegistry, "available_entry_points", lambda self: (BrokenPoint(),)
    )

    with caplog.at_level(logging.ERROR, logger="bgmeter"):
        _, errors = registry.register_available_entry_points()

    assert errors == {"broken": "no module named broken"}
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.name == "bgmeter.drivers"
    assert "broken" in record.getMessage()
    assert "ImportError" in record.getMessage()


@pytest.mark.asyncio
async def test_ble_transport_logs_scan_and_advertisements(caplog):
    device = SimpleNamespace(address="AA:BB", name="Backend name")
    advertisement = SimpleNamespace(
        local_name="Advertised meter",
        service_uuids=["1808"],
        manufacturer_data={},
        service_data={},
        tx_power=None,
        rssi=-51,
    )
    scanner = FakeScanner({device.address: (device, advertisement)})

    with caplog.at_level(logging.DEBUG, logger="bgmeter"):
        await BleTransport(scanner_factory=lambda: scanner).discover(timeout=0)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "bgmeter.transports.bleak"
    ]
    assert any(message.startswith("BLE scan started") for message in messages)
    assert any("AA:BB" in message and "Advertised meter" in message for message in messages)
    assert any(message.startswith("BLE scan finished: 1 endpoint") for message in messages)


@pytest.mark.asyncio
async def test_ble_session_logs_gatt_bytes_at_debug_only(caplog):
    transport = BleTransport(client_factory=lambda target: LoggingClient())
    session = await transport.connect(make_endpoint(transport="ble"))

    with caplog.at_level(logging.INFO, logger="bgmeter"):
        await session.write_gatt_char("ffe1", b"\x01\x02", response=False)
        await session.read_gatt_char("2a24")
    assert not [
        record
        for record in caplog.records
        if record.name == "bgmeter.transports.bleak"
        and ("01 02" in record.getMessage() or "aa bb" in record.getMessage())
    ]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="bgmeter"):
        await session.write_gatt_char("ffe1", b"\x01\x02", response=False)
        await session.read_gatt_char("2a24")
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "01 02" in text
    assert "aa bb" in text
