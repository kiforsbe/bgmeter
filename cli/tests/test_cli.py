from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from bgmeter import (  # noqa: E402
    DRIVER_API_VERSION,
    AmbiguousDeviceError,
    CompletionStatus,
    DeviceNotFoundError,
    DiscoveryError,
    DriverMatch,
    GlucoseRecord,
    MeasurementTime,
    MeterConnectionError,
    MeterDevice,
    MeterIdentity,
    MeterTimeoutError,
    ProtocolError,
    RawCapture,
    ReadResult,
    TransportEndpoint,
    UnsupportedDeviceError,
)
from bgmeter_cli.app import _parser, run  # noqa: E402
from bgmeter_cli.config import (  # noqa: E402
    CONFIG_SCHEMA,
    CONFIG_VERSION,
    DriverConfig,
    load_config,
    save_config,
)
from bgmeter_cli.store import MeasurementStore, StoreError  # noqa: E402
from bgmeter_cli.exporters import (  # noqa: E402
    CSV_COLUMNS,
    render_csv,
    render_json,
    render_terminal,
)


class FakeDistribution:
    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        self.metadata = {"Name": name}


class FakeEntryPoint:
    group = "bgmeter.drivers"

    def __init__(self, name, factory, *, version="2.4.1"):
        self.name = name
        self.value = f"{name.replace('-', '_')}:driver_factory"
        self.dist = FakeDistribution(f"bgmeter-{name}", version)
        self._factory = factory

    def load(self):
        return self._factory


class FakeDriver:
    display_name = "Example glucose meter"
    api_version = DRIVER_API_VERSION
    supported_transports = frozenset({"fake", "usb"})
    known_meter_identities = ("Example One", "Example Two")

    def __init__(self, driver_id="fake"):
        self.driver_id = driver_id

    def match_candidate(self, endpoint):
        return DriverMatch(90, ("test fixture",))

    async def probe(self, session):
        return MeterIdentity(manufacturer="Example", model="Meter")

    async def read_records(self, session, device, options):
        raise AssertionError("the CLI fake manager owns reads")


def driver_factory(driver_id="fake"):
    return lambda: FakeDriver(driver_id)


def make_device(number=1, *, driver_id="fake"):
    return MeterDevice(
        selector=f"fake:meter-{number}",
        driver_id=driver_id,
        endpoint=TransportEndpoint(
            transport="fake",
            identifier=f"meter-{number}",
            name=f"Example {number}",
            service_uuids={"1808"},
            metadata={"rssi": -40 - number, "advertisement": b"\x01\xa0"},
        ),
        identity=MeterIdentity(
            manufacturer="Example Medical",
            model=f"Meter {number}",
            serial_number=f"SERIAL-{number}",
            firmware_revision="1.2.3",
            metadata={"system_id": b"\x10\x20"},
        ),
        match=DriverMatch(90, ("test fixture",)),
    )


@pytest.fixture
def complete_result():
    device = make_device()
    measured_at = MeasurementTime(
        meter_datetime=datetime(2026, 9, 16, 19, 10, 16),
        measured_at_local=datetime.fromisoformat("2026-09-16T19:10:16+02:00"),
        measured_at_utc=datetime(2026, 9, 16, 17, 10, 16, tzinfo=UTC),
        timezone="Europe/Stockholm",
        utc_offset_seconds=7200,
    )
    records = (
        GlucoseRecord(
            record_id="fake:meter-1:7",
            native_sequence=7,
            mmol_l=Decimal("7.44"),
            native_value=Decimal("134"),
            native_unit="mg/dL",
            measured_at=measured_at,
            flags={"before_meal": True, "control": False},
            source_device_id="fake:meter-1",
            source_driver_id="fake",
            driver_data={
                "fake": {
                    "exact": Decimal("0.0100"),
                    "packet": b"\x00\xff",
                    "nested": (b"\x10", {"again": bytearray(b"\x20")}),
                }
            },
            raw=RawCapture(
                request=b"\x01\x02",
                fragments=(b"\x03", b"\x04\x05"),
                response=b"\x06\x07",
                record=b"\x00\x86",
            ),
        ),
        GlucoseRecord(
            record_id="fake:meter-1:8",
            native_sequence=8,
            mmol_l=Decimal("5.00"),
            native_value=Decimal("90"),
            native_unit="mg/dL",
            measured_at=replace(
                measured_at,
                meter_datetime=datetime(2026, 9, 16, 20, 15),
                measured_at_local=datetime.fromisoformat("2026-09-16T20:15:00+02:00"),
                measured_at_utc=datetime(2026, 9, 16, 18, 15, tzinfo=UTC),
            ),
            flags={},
            source_device_id="fake:meter-1",
            source_driver_id="fake",
        ),
    )
    return ReadResult(
        device=device,
        records=records,
        completion=CompletionStatus.COMPLETE,
        started_at=datetime(2026, 9, 16, 17, 9, tzinfo=UTC),
        ended_at=datetime(2026, 9, 16, 17, 11, tzinfo=UTC),
        expected_count=2,
        received_count=2,
        duplicate_count=1,
        rejected_count=2,
        retry_count=3,
        termination_reason="history_complete",
        warnings=("clock assumed correct",),
        diagnostics={
            "fake.wire": {
                "messages": (b"\xaa\xbb", {"payload": memoryview(b"\xcc")}),
                "ratio": Decimal("1.250"),
            }
        },
    )


class FakeManager:
    def __init__(self, registry, *, devices=(), result=None, discover_error=None, read_error=None):
        self.registry = registry
        self.devices = tuple(devices)
        self.result = result
        self.discover_error = discover_error
        self.read_error = read_error
        self.read_calls = []

    async def discover(self, *, timeout=5.0):
        if self.discover_error is not None:
            raise self.discover_error
        enabled = {item.driver_id for item in self.registry.descriptors()}
        devices = tuple(item for item in self.devices if item.driver_id in enabled)
        if not devices:
            raise DeviceNotFoundError("no fake meters")
        return devices

    async def read(self, device, options=None):
        self.read_calls.append((device, options))
        if self.read_error is not None:
            raise self.read_error
        return replace(self.result, device=device)


class Input(io.StringIO):
    def __init__(self, value="", *, interactive=False):
        super().__init__(value)
        self.interactive = interactive

    def isatty(self):
        return self.interactive


def install_entry_points(monkeypatch, *points):
    monkeypatch.setattr(
        "bgmeter.DriverRegistry.available_entry_points",
        lambda self: tuple(points),
    )


def invoke(
    argv,
    *,
    config_path,
    database_path=None,
    manager_factory=None,
    stdin=None,
):
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = {}
    if database_path is not None:
        options["database_path"] = database_path
    status = run(
        argv,
        stdin=stdin or Input(),
        stdout=stdout,
        stderr=stderr,
        config_path=config_path,
        manager_factory=manager_factory,
        **options,
    )
    return status, stdout.getvalue(), stderr.getvalue()


def test_missing_config_defaults_to_microtech_but_saved_empty_stays_empty(tmp_path):
    path = tmp_path / "drivers.json"

    assert load_config(path).registered == ("microtech",)

    save_config(DriverConfig(registered=()), path)

    assert load_config(path).registered == ()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": CONFIG_SCHEMA,
        "schema_version": CONFIG_VERSION,
        "registered": [],
    }


def test_read_supports_short_forms_for_its_options_and_logging_options():
    arguments = _parser().parse_args(
        [
            "read",
            "-d",
            "fake:meter-1",
            "-D",
            "fake",
            "-z",
            "Europe/Stockholm",
            "-o",
            "terminal",
            "-r",
            "-s",
            "-n",
            "1",
            "-N",
            "-f",
            "-m",
            "After lunch",
            "-l",
            "debug",
            "-L",
            "bgmeter.log",
        ]
    )

    assert arguments.device == "fake:meter-1"
    assert arguments.driver == "fake"
    assert arguments.timezone == "Europe/Stockholm"
    assert arguments.output == ["terminal"]
    assert arguments.show_raw is True
    assert arguments.store is True
    assert arguments.newest == 1
    assert arguments.new_only is True
    assert arguments.force is True
    assert arguments.message == "After lunch"
    assert arguments.log_level_sub == "debug"
    assert arguments.log_file_sub == Path("bgmeter.log")


def test_config_save_is_atomic_if_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "drivers.json"
    save_config(DriverConfig(("old",)), path)
    previous = path.read_bytes()
    replacements = []

    def fail_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        assert Path(source).parent == path.parent
        assert Path(source).is_file()
        raise OSError("simulated replace failure")

    monkeypatch.setattr("bgmeter_cli.config.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_config(DriverConfig(("new",)), path)

    assert path.read_bytes() == previous
    assert replacements[0][1] == path


def test_registration_persists_only_after_core_validation_and_unregister_keeps_installed(
    tmp_path, monkeypatch
):
    class IncompatibleDriver(FakeDriver):
        driver_id = "broken"
        api_version = 999

    good = FakeEntryPoint("example", driver_factory())
    broken = FakeEntryPoint("broken", IncompatibleDriver)
    install_entry_points(monkeypatch, broken, good)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(()), config_path)

    status, stdout, stderr = invoke(
        ["drivers", "register", "broken"], config_path=config_path
    )
    assert status == 2
    assert stdout == ""
    assert "incompatible" in stderr
    assert load_config(config_path).registered == ()

    status, stdout, stderr = invoke(
        ["drivers", "register", "example"], config_path=config_path
    )
    assert (status, stderr) == (0, "")
    assert "registered example" in stdout
    assert load_config(config_path).registered == ("example",)

    status, stdout, stderr = invoke(
        ["drivers", "unregister", "example"], config_path=config_path
    )
    assert (status, stderr) == (0, "")
    assert "unregistered example" in stdout
    assert load_config(config_path).registered == ()

    status, stdout, stderr = invoke(
        ["drivers", "list", "--available"], config_path=config_path
    )
    assert status == 0
    assert "example" in stdout
    assert "not registered" in stdout
    assert "bgmeter-example 2.4.1" in stdout
    assert "broken" in stderr


def test_driver_list_and_info_show_package_api_transport_identity_and_broken_plugins(
    tmp_path, monkeypatch
):
    good = FakeEntryPoint("example", driver_factory())
    broken = FakeEntryPoint("broken", lambda: object())
    install_entry_points(monkeypatch, broken, good)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example", "broken")), config_path)

    status, stdout, stderr = invoke(
        ["drivers", "list", "--registered"], config_path=config_path
    )
    assert status == 0
    assert "example" in stdout
    assert "fake" in stdout
    assert "compatible" in stdout
    assert "broken" in stdout
    assert "broken" in stderr

    status, stdout, stderr = invoke(
        ["drivers", "info", "example"], config_path=config_path
    )
    assert status == 0
    assert "warning: driver broken" in stderr
    assert "Entry point: example" in stdout
    assert "Driver ID: fake" in stdout
    assert "Package: bgmeter-example 2.4.1" in stdout
    assert f"Driver API: {DRIVER_API_VERSION}" in stdout
    assert "Transports: fake, usb" in stdout
    assert "Known identities: Example One, Example Two" in stdout
    assert "Registered: yes" in stdout
    assert "Compatibility: compatible" in stdout


def test_devices_and_info_use_registered_drivers_and_driver_filter(
    tmp_path, monkeypatch
):
    one = FakeEntryPoint("one", driver_factory("driver-one"))
    two = FakeEntryPoint("two", driver_factory("driver-two"))
    install_entry_points(monkeypatch, one, two)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("one", "two")), config_path)
    devices = (make_device(1, driver_id="driver-one"), make_device(2, driver_id="driver-two"))
    managers = []

    def manager_factory(registry, progress=None):
        manager = FakeManager(registry, devices=devices)
        managers.append(manager)
        return manager

    status, stdout, stderr = invoke(
        ["devices", "--driver", "driver-two"],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert (status, stderr) == (0, "")
    assert "fake:meter-2" in stdout
    assert "fake:meter-1" not in stdout
    assert [item.driver_id for item in managers[-1].registry.descriptors()] == [
        "driver-two"
    ]

    status, stdout, stderr = invoke(
        ["info", "--device", "fake:meter-2", "--driver", "two"],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert (status, stderr) == (0, "")
    assert "Selector: fake:meter-2" in stdout
    assert "Driver: driver-two" in stdout
    assert "Manufacturer: Example Medical" in stdout
    assert "Model: Meter 2" in stdout
    assert "Serial number: SERIAL-2" in stdout


def test_read_interactively_selects_from_multiple_devices(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    devices = (make_device(1), make_device(2))
    managers = []

    def manager_factory(registry, progress=None):
        manager = FakeManager(registry, devices=devices, result=complete_result)
        managers.append(manager)
        return manager

    status, stdout, stderr = invoke(
        ["read", "--timezone", "Europe/Stockholm"],
        config_path=config_path,
        manager_factory=manager_factory,
        stdin=Input("2\n", interactive=True),
    )

    assert status == 0
    assert "7.4 mmol/L" in stdout
    assert "Select a meter" in stderr
    assert "2) fake:meter-2" in stderr
    assert managers[0].read_calls[0][0].selector == "fake:meter-2"
    assert managers[0].read_calls[0][1].timezone == "Europe/Stockholm"


def test_read_noninteractive_requires_device_and_keeps_data_off_stderr(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)

    def manager_factory(registry, progress=None):
        return FakeManager(registry, devices=(make_device(),), result=complete_result)

    status, stdout, stderr = invoke(
        ["read"], config_path=config_path, manager_factory=manager_factory
    )
    assert status == 2
    assert stdout == ""
    assert "--device is required for non-interactive use" in stderr

    status, stdout, stderr = invoke(
        ["read", "--device", "fake:meter-1"],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert status == 0
    assert "7.4 mmol/L" in stdout
    assert stderr == ""


def test_read_does_not_create_database_without_store(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    database_path = tmp_path / "measurements.sqlite3"
    manager_factory = lambda registry, progress=None: FakeManager(
        registry, devices=(make_device(),), result=complete_result
    )

    status, stdout, stderr = invoke(
        ["read", "--device", "fake:meter-1"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=manager_factory,
    )

    assert status == 0
    assert "7.4 mmol/L" in stdout
    assert stderr == ""
    assert not database_path.exists()


def test_read_stores_normalized_records_when_store_is_requested(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    database_path = tmp_path / "measurements.sqlite3"
    manager_factory = lambda registry, progress=None: FakeManager(
        registry, devices=(make_device(),), result=complete_result
    )

    status, stdout, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--store"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=manager_factory,
    )

    assert status == 0
    assert "7.4 mmol/L" in stdout
    assert stderr == ""
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone() == (2,)
        assert connection.execute(
            "SELECT name, value FROM measurement_flags ORDER BY name"
        ).fetchall() == [("before_meal", 1), ("control", 0)]

    status, _, _ = invoke(
        ["read", "--device", "fake:meter-1", "--store"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=manager_factory,
    )

    assert status == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone() == (2,)


def test_read_message_annotates_latest_record_and_exports_historic_messages(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    database_path = tmp_path / "measurements.sqlite3"
    MeasurementStore(database_path).store(
        complete_result.records,
        messages={complete_result.records[0].record_id: "Before breakfast"},
    )
    json_path = tmp_path / "records.json"
    csv_path = tmp_path / "records.csv"
    manager_factory = lambda registry, progress=None: FakeManager(
        registry, devices=(make_device(),), result=complete_result
    )

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "-m",
            "After lunch",
            "--store",
            "--output",
            f"json={json_path}",
            "--output",
            f"csv={csv_path}",
            "--output",
            "terminal",
        ],
        config_path=config_path,
        database_path=database_path,
        manager_factory=manager_factory,
    )

    assert status == 0
    assert stderr == ""
    assert "fake:meter-1:7: 7.4 mmol/L" in stdout
    assert "message Before breakfast" in stdout
    assert "fake:meter-1:8: 5.0 mmol/L" in stdout
    assert "message After lunch" in stdout
    assert [item["message"] for item in json.loads(json_path.read_text())['records']] == [
        "Before breakfast",
        "After lunch",
    ]
    with csv_path.open(newline="", encoding="utf-8-sig") as stream:
        assert [row["message"] for row in csv.DictReader(stream)] == [
            "Before breakfast",
            "After lunch",
        ]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT message FROM measurements ORDER BY native_sequence"
        ).fetchall() == [("Before breakfast",), ("After lunch",)]


def test_read_message_without_a_database_only_annotates_the_latest_record(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    database_path = tmp_path / "measurements.sqlite3"
    manager_factory = lambda registry, progress=None: FakeManager(
        registry, devices=(make_device(),), result=complete_result
    )

    status, stdout, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--message", "After lunch"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=manager_factory,
    )

    assert status == 0
    assert stderr == ""
    assert "message After lunch" in stdout
    assert stdout.count("message ") == 1
    assert not database_path.exists()


def test_plain_store_does_not_overwrite_a_message_changed_after_lookup(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    database_path = tmp_path / "measurements.sqlite3"
    historical = complete_result.records[0]
    MeasurementStore(database_path).store(
        complete_result.records, messages={historical.record_id: "Original"}
    )
    original_messages_for = MeasurementStore.messages_for

    def messages_for_then_change(self, record_ids):
        messages = original_messages_for(self, record_ids)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE measurements SET message = ? WHERE record_id = ?",
                ("Changed elsewhere", historical.record_id),
            )
        return messages

    monkeypatch.setattr(
        MeasurementStore, "messages_for", messages_for_then_change
    )
    manager_factory = lambda registry, progress=None: FakeManager(
        registry, devices=(make_device(),), result=complete_result
    )

    status, _, _ = invoke(
        ["read", "--device", "fake:meter-1", "--store"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=manager_factory,
    )

    assert status == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT message FROM measurements WHERE record_id = ?", (historical.record_id,)
        ).fetchone() == ("Changed elsewhere",)


def test_partial_read_stores_valid_records_before_returning_status_five(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    database_path = tmp_path / "measurements.sqlite3"
    partial = replace(complete_result, completion=CompletionStatus.PARTIAL)
    manager_factory = lambda registry, progress=None: FakeManager(
        registry, devices=(make_device(),), result=partial
    )

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--store"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=manager_factory,
    )

    assert status == 5
    assert "retrieval is partial" in stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone() == (2,)


def _read_setup(tmp_path, monkeypatch, result):
    """Return (config_path, database_path, managers, manager_factory).

    ``managers`` is filled in by the factory, following the same pattern as
    ``test_read_prompts_for_a_device_when_several_are_discovered``.
    """
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    database_path = tmp_path / "measurements.sqlite3"
    managers = []

    def manager_factory(registry, progress=None):
        manager = FakeManager(registry, devices=(make_device(),), result=result)
        managers.append(manager)
        return manager

    return config_path, database_path, managers, manager_factory


def _consistent_result(result):
    """The fixture's native sequences run to 8, so a consistent meter reports 8 records."""
    return replace(result, expected_count=8)


def test_newest_passes_a_limit_to_the_driver(tmp_path, monkeypatch, complete_result):
    config_path, database_path, managers, factory = _read_setup(
        tmp_path, monkeypatch, complete_result
    )

    status, _, _ = invoke(
        ["read", "--device", "fake:meter-1", "--newest", "5"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    _, options = managers[0].read_calls[0]
    assert options.newest_count == 5
    assert options.known_record_ids == frozenset()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_newest_rejects_a_non_positive_count(tmp_path, monkeypatch, complete_result, value):
    config_path, database_path, _managers, factory = _read_setup(
        tmp_path, monkeypatch, complete_result
    )

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--newest", value],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 2
    assert "must be a positive whole number" in stderr


def test_new_only_passes_stored_record_ids_without_requiring_store(
    tmp_path, monkeypatch, complete_result
):
    up_to_date = _consistent_result(complete_result)
    config_path, database_path, managers, factory = _read_setup(
        tmp_path, monkeypatch, up_to_date
    )
    MeasurementStore(database_path).store(complete_result.records)

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--new-only"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "appears to have been reset" not in stderr
    _, options = managers[0].read_calls[0]
    assert options.known_record_ids == frozenset(
        {"fake:meter-1:7", "fake:meter-1:8"}
    )


def test_new_only_on_a_fresh_database_sends_no_known_ids(
    tmp_path, monkeypatch, complete_result
):
    config_path, database_path, managers, factory = _read_setup(
        tmp_path, monkeypatch, complete_result
    )

    status, _, _ = invoke(
        ["read", "--device", "fake:meter-1", "--new-only"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert managers[0].read_calls[0][1].known_record_ids == frozenset()
    assert not database_path.exists()


def test_truncated_read_succeeds_quietly(tmp_path, monkeypatch, complete_result):
    truncated = replace(
        complete_result,
        completion=CompletionStatus.TRUNCATED,
        termination_reason="limit_reached",
    )
    config_path, database_path, _managers, factory = _read_setup(
        tmp_path, monkeypatch, truncated
    )

    status, stdout, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--newest", "2"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "warning" not in stderr
    assert "truncated" in stdout


def test_an_empty_truncated_read_renders_every_output(
    tmp_path, monkeypatch, complete_result
):
    nothing_new = replace(
        complete_result,
        records=(),
        received_count=0,
        completion=CompletionStatus.TRUNCATED,
        termination_reason="already_stored",
    )
    config_path, database_path, _managers, factory = _read_setup(
        tmp_path, monkeypatch, nothing_new
    )
    csv_path = tmp_path / "out.csv"
    json_path = tmp_path / "out.json"

    status, stdout, _ = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--new-only",
            "--output",
            "terminal",
            "--output",
            f"csv={csv_path}",
            "--output",
            f"json={json_path}",
        ],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "received 0 of 2" in stdout
    assert json.loads(json_path.read_text(encoding="utf-8"))["records"] == []
    assert list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines())) == []


def test_new_only_warns_when_the_meter_holds_fewer_records_than_the_database(
    tmp_path, monkeypatch, complete_result
):
    reset_meter = replace(complete_result, expected_count=1)
    config_path, database_path, _managers, factory = _read_setup(
        tmp_path, monkeypatch, reset_meter
    )
    MeasurementStore(database_path).store(complete_result.records)

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--new-only"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "appears to have been reset" in stderr
    assert "full read" in stderr


def test_new_only_reports_an_unreadable_database_as_a_store_error(
    tmp_path, monkeypatch, complete_result
):
    config_path, database_path, managers, factory = _read_setup(
        tmp_path, monkeypatch, complete_result
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 99")
    connection.close()

    status, stdout, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--new-only"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 6
    assert stdout == ""
    assert "error: cannot read stored measurements:" in stderr
    assert managers[0].read_calls == []


@pytest.mark.parametrize("expected_count", [8, 9])
def test_new_only_does_not_warn_when_the_meter_holds_the_stored_history(
    tmp_path, monkeypatch, complete_result, expected_count
):
    meter = replace(complete_result, expected_count=expected_count)
    config_path, database_path, _managers, factory = _read_setup(
        tmp_path, monkeypatch, meter
    )
    MeasurementStore(database_path).store(complete_result.records)

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--new-only"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "appears to have been reset" not in stderr


def test_the_reset_warning_is_only_for_new_only_reads(
    tmp_path, monkeypatch, complete_result
):
    reset_meter = replace(complete_result, expected_count=1)
    config_path, database_path, _managers, factory = _read_setup(
        tmp_path, monkeypatch, reset_meter
    )
    MeasurementStore(database_path).store(complete_result.records)

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "appears to have been reset" not in stderr


def test_new_only_with_store_sends_known_ids_and_stores_the_new_records(
    tmp_path, monkeypatch, complete_result
):
    up_to_date = _consistent_result(complete_result)
    config_path, database_path, managers, factory = _read_setup(
        tmp_path, monkeypatch, up_to_date
    )
    MeasurementStore(database_path).store(complete_result.records[:1])

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--new-only", "--store"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "appears to have been reset" not in stderr
    assert managers[0].read_calls[0][1].known_record_ids == frozenset({"fake:meter-1:7"})
    with sqlite3.connect(database_path) as connection:
        stored = connection.execute(
            "SELECT record_id FROM measurements ORDER BY native_sequence"
        ).fetchall()
    connection.close()
    assert stored == [("fake:meter-1:7",), ("fake:meter-1:8",)]


def test_new_only_with_store_warns_about_a_reset_meter_and_still_stores(
    tmp_path, monkeypatch, complete_result
):
    reset_meter = replace(complete_result, expected_count=1)
    config_path, database_path, _managers, factory = _read_setup(
        tmp_path, monkeypatch, reset_meter
    )
    MeasurementStore(database_path).store(complete_result.records[:1])

    status, _, stderr = invoke(
        ["read", "--device", "fake:meter-1", "--new-only", "--store"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
    assert "appears to have been reset" in stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM measurements").fetchone() == (2,)
    connection.close()


def test_store_failure_returns_status_six_without_publishing_output(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    output = tmp_path / "records.csv"
    manager_factory = lambda registry, progress=None: FakeManager(
        registry, devices=(make_device(),), result=complete_result
    )
    monkeypatch.setattr(
        "bgmeter_cli.app.MeasurementStore",
        lambda path=None: (_ for _ in ()).throw(StoreError("disk full")),
    )

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--store",
            "--output",
            f"csv={output}",
        ],
        config_path=config_path,
        database_path=tmp_path / "measurements.sqlite3",
        manager_factory=manager_factory,
    )

    assert status == 6
    assert stdout == ""
    assert not output.exists()
    assert "cannot store measurements" in stderr


def test_discovery_and_read_share_one_asyncio_lifecycle(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    loops = []

    class LoopAwareManager(FakeManager):
        async def discover(self, *, timeout=5.0):
            loops.append(asyncio.get_running_loop())
            return await super().discover(timeout=timeout)

        async def read(self, device, options=None):
            loops.append(asyncio.get_running_loop())
            return await super().read(device, options)

    def manager_factory(registry, progress=None):
        return LoopAwareManager(
            registry, devices=(make_device(),), result=complete_result
        )

    status, _, _ = invoke(
        ["read", "--device", "fake:meter-1"],
        config_path=config_path,
        manager_factory=manager_factory,
    )

    assert status == 0
    assert len(loops) == 2
    assert loops[0] is loops[1]


def test_exporters_are_lossless_stable_and_human_readable(complete_result):
    document = json.loads(render_json(complete_result))

    assert document["schema"] == "bgmeter.read-result"
    assert document["schema_version"] == 2
    assert document["completion"] == {
        "status": "complete",
        "expected_count": 2,
        "received_count": 2,
        "duplicate_count": 1,
        "rejected_count": 2,
        "retry_count": 3,
        "termination_reason": "history_complete",
    }
    assert document["device"]["endpoint"]["metadata"]["advertisement"] == {
        "$bytes_hex": "01a0"
    }
    assert document["records"][0]["mmol_l"] == "7.44"
    assert document["records"][0]["native_value"] == "134"
    assert document["records"][0]["meter_datetime"] == "2026-09-16T19:10:16"
    assert document["records"][0]["measured_at_local"] == "2026-09-16T19:10:16+02:00"
    assert document["records"][0]["measured_at_utc"] == "2026-09-16T17:10:16+00:00"
    assert document["records"][0]["raw"] == {
        "request_hex": "0102",
        "fragments_hex": ["03", "0405"],
        "response_hex": "0607",
        "record_hex": "0086",
    }
    assert document["records"][0]["driver_data"]["fake"]["exact"] == "0.0100"
    assert document["records"][0]["driver_data"]["fake"]["packet"] == {
        "$bytes_hex": "00ff"
    }
    assert document["diagnostics"]["fake.wire"]["messages"] == [
        {"$bytes_hex": "aabb"},
        {"payload": {"$bytes_hex": "cc"}},
    ]
    assert document["diagnostics"]["fake.wire"]["ratio"] == "1.250"

    csv_text = render_csv(complete_result)
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert tuple(reader.fieldnames) == CSV_COLUMNS == (
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
    assert len(rows) == 2
    assert rows[0]["mmol_l"] == "7.44"
    assert rows[0]["measured_at_local"] == "2026-09-16T19:10:16+02:00"
    assert rows[0]["measured_at_utc"] == "2026-09-16T17:10:16+00:00"
    assert rows[0]["flags_json"] == '{"before_meal":true,"control":false}'
    assert rows[0]["raw_request_hex"] == "0102"
    assert rows[0]["raw_fragments_json"] == '["03","0405"]'
    assert '"$bytes_hex":"00ff"' in rows[0]["driver_data_json"]

    terminal = render_terminal(complete_result, show_raw=True)
    assert "7.4 mmol/L" in terminal
    assert "local 2026-09-16T19:10:16+02:00" in terminal
    assert "UTC 2026-09-16T17:10:16+00:00" in terminal
    assert "request=0102" in terminal
    assert "fragments=03,0405" in terminal
    assert "response=0607" in terminal
    assert "record=0086" in terminal


def test_read_supports_repeated_outputs_and_atomic_overwrite_protection(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    csv_path = tmp_path / "records.csv"
    json_path = tmp_path / "records.json"
    managers = []

    def manager_factory(registry, progress=None):
        manager = FakeManager(
            registry, devices=(make_device(),), result=complete_result
        )
        managers.append(manager)
        return manager

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--output",
            "terminal",
            "--output",
            f"csv={csv_path}",
            "--output",
            f"json={json_path}",
            "--show-raw",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert (status, stderr) == (0, "")
    assert "7.4 mmol/L" in stdout
    assert csv_path.read_text(encoding="utf-8-sig") == render_csv(complete_result)
    assert json.loads(json_path.read_text(encoding="utf-8"))["schema_version"] == 2
    assert not list(tmp_path.glob(".*.tmp"))

    previous = csv_path.read_bytes()
    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--output",
            f"csv={csv_path}",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert status == 6
    assert stdout == ""
    assert "already exists" in stderr
    assert csv_path.read_bytes() == previous
    assert len(managers[-1].read_calls) == 0

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--output",
            "terminal",
            "--output",
            "json",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert status == 6
    assert stdout == ""
    assert "only one output may target stdout" in stderr

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--output",
            f"csv={csv_path}",
            "--force",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert (status, stdout, stderr) == (0, "", "")
    assert csv_path.read_text(encoding="utf-8-sig") == render_csv(complete_result)


def test_read_maps_temporary_file_creation_failure_to_export_status(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)

    def manager_factory(registry, progress=None):
        return FakeManager(registry, devices=(make_device(),), result=complete_result)

    def fail_mkstemp(*args, **kwargs):
        raise PermissionError("simulated permission failure")

    monkeypatch.setattr("bgmeter_cli.app.tempfile.mkstemp", fail_mkstemp)

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--output",
            f"csv={tmp_path / 'records.csv'}",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )

    assert status == 6
    assert stdout == ""
    assert "cannot write output file" in stderr
    assert "simulated permission failure" in stderr
    assert "Traceback" not in stderr


def test_read_closes_temporary_descriptor_if_open_fails(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    descriptors = []

    def manager_factory(registry, progress=None):
        return FakeManager(registry, devices=(make_device(),), result=complete_result)

    def fail_fdopen(descriptor, *args, **kwargs):
        descriptors.append(descriptor)
        raise PermissionError("simulated open failure")

    monkeypatch.setattr("bgmeter_cli.app.os.fdopen", fail_fdopen)

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--output",
            f"csv={tmp_path / 'records.csv'}",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )

    assert status == 6
    assert stdout == ""
    assert "simulated open failure" in stderr
    assert "Traceback" not in stderr
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not list(tmp_path.glob(".records.csv.*.tmp"))


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (AmbiguousDeviceError("ambiguous"), 2),
        (DeviceNotFoundError("missing"), 3),
        (UnsupportedDeviceError("unsupported"), 3),
        (DiscoveryError("adapter failed"), 4),
        (MeterConnectionError("connection failed"), 4),
        (MeterTimeoutError("history timed out"), 5),
        (ProtocolError("bad frame"), 5),
        (KeyboardInterrupt(), 130),
    ],
)
def test_cli_maps_public_errors_to_documented_exit_codes(
    tmp_path, monkeypatch, complete_result, error, expected_status
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)

    def manager_factory(registry, progress=None):
        return FakeManager(
            registry,
            devices=(make_device(),),
            result=complete_result,
            discover_error=error,
        )

    status, stdout, stderr = invoke(
        ["devices"], config_path=config_path, manager_factory=manager_factory
    )
    assert status == expected_status
    assert stdout == ""
    assert stderr


def test_usage_export_and_incomplete_read_exit_codes(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)

    status, stdout, stderr = invoke(["info"], config_path=config_path)
    assert status == 2
    assert stdout == ""
    assert "--device" in stderr

    partial = replace(
        complete_result,
        completion=CompletionStatus.PARTIAL,
        termination_reason="missing_records",
    )

    def manager_factory(registry, progress=None):
        return FakeManager(registry, devices=(make_device(),), result=partial)

    status, stdout, stderr = invoke(
        ["read", "--device", "fake:meter-1"],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert status == 5
    assert "partial" in stdout
    assert "retrieval is partial" in stderr

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--output",
            "yaml=out.yaml",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )
    assert status == 6
    assert stdout == ""
    assert "unknown output" in stderr


def test_available_driver_list_excludes_registered_missing_entry_points(
    tmp_path, monkeypatch
):
    installed = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, installed)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("missing",)), config_path)

    status, stdout, stderr = invoke(
        ["drivers", "list", "--available"], config_path=config_path
    )

    assert status == 0
    assert "example" in stdout
    assert "missing" not in stdout
    assert "warning: driver missing" in stderr


def test_register_validates_candidate_with_current_registry_before_persisting(
    tmp_path, monkeypatch
):
    existing = FakeEntryPoint("existing", driver_factory("shared-id"))
    duplicate = FakeEntryPoint("duplicate", driver_factory("shared-id"))
    broken = FakeEntryPoint("broken", lambda: object())
    install_entry_points(monkeypatch, existing, broken, duplicate)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("existing", "broken")), config_path)

    status, stdout, stderr = invoke(
        ["drivers", "register", "duplicate"], config_path=config_path
    )

    assert status == 2
    assert stdout == ""
    assert "warning: driver broken" in stderr
    assert "already registered" in stderr
    assert load_config(config_path).registered == ("existing", "broken")


def test_register_does_not_report_success_for_a_persisted_driver_conflict(
    tmp_path, monkeypatch
):
    existing = FakeEntryPoint("existing", driver_factory("shared-id"))
    duplicate = FakeEntryPoint("duplicate", driver_factory("shared-id"))
    install_entry_points(monkeypatch, existing, duplicate)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("existing", "duplicate")), config_path)

    status, stdout, stderr = invoke(
        ["drivers", "register", "duplicate"], config_path=config_path
    )

    assert status == 2
    assert stdout == ""
    assert "already registered" in stderr
    assert load_config(config_path).registered == ("existing", "duplicate")


def test_register_rejects_the_first_member_of_a_persisted_driver_conflict(
    tmp_path, monkeypatch
):
    existing = FakeEntryPoint("existing", driver_factory("shared-id"))
    duplicate = FakeEntryPoint("duplicate", driver_factory("shared-id"))
    install_entry_points(monkeypatch, existing, duplicate)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("existing", "duplicate")), config_path)

    status, stdout, stderr = invoke(
        ["drivers", "register", "existing"], config_path=config_path
    )

    assert status == 2
    assert stdout == ""
    assert "already registered" in stderr
    assert load_config(config_path).registered == ("existing", "duplicate")


def test_invalid_timezone_returns_usage_status_without_read_or_traceback(
    tmp_path, monkeypatch, complete_result
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), config_path)
    managers = []

    def manager_factory(registry, progress=None):
        manager = FakeManager(
            registry, devices=(make_device(),), result=complete_result
        )
        managers.append(manager)
        return manager

    status, stdout, stderr = invoke(
        [
            "read",
            "--device",
            "fake:meter-1",
            "--timezone",
            "Not/A_Real_Timezone",
        ],
        config_path=config_path,
        manager_factory=manager_factory,
    )

    assert status == 2
    assert stdout == ""
    assert "invalid timezone" in stderr
    assert "Traceback" not in stderr
    assert not managers or managers[0].read_calls == []


@pytest.mark.parametrize(
    ("command", "registered"),
    [
        (["drivers", "register", "example"], ()),
        (["drivers", "unregister", "example"], ("example",)),
    ],
)
def test_config_write_failures_return_export_status_without_traceback(
    tmp_path, monkeypatch, command, registered
):
    point = FakeEntryPoint("example", driver_factory())
    install_entry_points(monkeypatch, point)
    config_path = tmp_path / "drivers.json"
    save_config(DriverConfig(registered), config_path)

    def fail_save(config, path):
        raise OSError("disk full")

    monkeypatch.setattr("bgmeter_cli.app.save_config", fail_save)

    status, stdout, stderr = invoke(command, config_path=config_path)

    assert status == 6
    assert stdout == ""
    assert "cannot save driver configuration" in stderr
    assert "disk full" in stderr
    assert "Traceback" not in stderr
