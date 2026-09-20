# Verbosity, Progress Reporting, and Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add plain-language `-v`/`-vv` progress output and independent, technical stdlib logging (`--log-level`, `--log-file`) across the core, MicroTech driver, and CLI.

**Architecture:** Two independent mechanisms. Progress: a small public `ProgressEvent` callback in core, delivered to drivers through a new optional `ReadOptions.progress` field (driver protocol unchanged), rendered by a CLI `ConsoleReporter` filtered by `-v`. Logging: `logging.getLogger(__name__)` in every layer with `NullHandler`s in the libraries, and a CLI-owned sink (terminal by default, file exclusively with `--log-file`) filtered by `--log-level`. Neither reads or filters the other.

**Tech Stack:** Python 3.13, stdlib `logging`/`argparse`, `bleak`, `platformdirs`, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-20-verbosity-and-logging-design.md`

## Global Constraints

- Python `>=3.13`; no new third-party dependency.
- `DRIVER_API_VERSION` stays `1`; `MeterDriver` method signatures do not change.
- The CLI imports only documented top-level `bgmeter` APIs (never `bgmeter_microtech` or private core modules).
- All new user-facing text is plain ASCII: three dots `...`, never the ellipsis character.
- Progress messages are plain-language sentences with no protocol/Bluetooth jargon (no GATT, notification, characteristic, FFE0, hex, transmission). Log messages are technical and never repeat a progress sentence or give advice.
- Progress and logging are fully independent: neither reads the other's settings or output, and nothing is suppressed because the other covers it.
- `-v` is repeatable and accepted before or after the subcommand (counts summed); effective level capped at 2 (`-vvv` == `-vv`).
- `--log-level {debug,info,warning,error}` defaults to `error`. Logging goes to stderr by default; `--log-file PATH` sends logs to the file exclusively (nothing from logging on the terminal). Level applies to whichever sink is active.
- stdout stays clean for machine-readable output; all new output goes to stderr or the log file.
- Exception types and exit statuses do not change. Statuses: `0` ok, `2` usage/ambiguous, `3` no supported meter, `4` connection/transport, `5` protocol or non-complete retrieval, `6` export, `130` interrupted.
- INFO log records never contain glucose values or serial numbers; DEBUG may contain raw bytes.
- **Git (user rule, overrides the usual "commit" step):** never run `git commit` or `git push`. Every task ends with the changes left uncommitted.
- **Testing (user rule):** run only the test files a task touches; do not run the whole repository suite after each task. Run the full suite once, in Task 8.
- **Shell:** Windows PowerShell. Use `python -m pytest ...` from the repository root `C:\Users\kim_f\source\repos\gochek`. The three packages are already installed in editable mode.

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `core/src/bgmeter/progress.py` | Create | `ProgressLevel`, `ProgressEvent`, `ProgressCallback`, `emit_progress` |
| `core/src/bgmeter/models.py` | Modify | `ReadOptions.progress` |
| `core/src/bgmeter/__init__.py` | Modify | Export progress API; `NullHandler` on `bgmeter` |
| `core/src/bgmeter/manager.py` | Modify | Progress events + logging in manager/connected meter |
| `core/src/bgmeter/drivers.py` | Modify | Log absorbed entry-point load failures |
| `core/src/bgmeter/transports/bleak.py` | Modify | BLE transport logging |
| `core/tests/test_progress.py` | Create | Progress API tests |
| `core/tests/test_manager.py` | Modify | Manager progress + logging tests |
| `drivers/microtech/src/bgmeter_microtech/collector.py` | Modify | `transmission_count`, `record_count`, `attributions_since`; verdict logging |
| `drivers/microtech/src/bgmeter_microtech/protocol.py` | Modify | Progress events + logging in `read_history` |
| `drivers/microtech/src/bgmeter_microtech/driver.py` | Modify | Pass `options.progress`; reworded error; logging |
| `drivers/microtech/src/bgmeter_microtech/__init__.py` | Modify | `NullHandler` on `bgmeter_microtech` |
| `drivers/microtech/tests/test_protocol.py`, `test_driver.py` | Modify | Progress + logging tests |
| `cli/src/bgmeter_cli/reporter.py` | Create | `ConsoleReporter` |
| `cli/src/bgmeter_cli/logging_setup.py` | Create | `configured_logging` sink context manager, `LogFileError` |
| `cli/src/bgmeter_cli/app.py` | Modify | Flags, reporter wiring, hints, outcome line, log sink, final-failure records |
| `cli/src/bgmeter_cli/registry.py` | Modify | Log absorbed driver load failures |
| `cli/tests/test_cli.py` | Modify | Mechanical `ManagerFactory` signature update |
| `cli/tests/test_reporting.py` | Create | Reporter, verbosity, hints, outcome, logging, independence tests |
| docs (`cli/README.md`, `core/README.md`, `drivers/microtech/README.md`, `docs/DEVELOPMENT.md`, `ARCHITECTURE.md`, `README.md`, `CHANGELOG.md`) | Modify | Document the feature |

---

### Task 1: Core progress API

**Files:**
- Create: `core/src/bgmeter/progress.py`
- Modify: `core/src/bgmeter/models.py` (imports and `ReadOptions`, currently lines 5-11 and 152-156)
- Modify: `core/src/bgmeter/__init__.py`
- Test: `core/tests/test_progress.py`

**Interfaces:**
- Produces: `ProgressLevel` (`StrEnum`: `INFO="info"`, `DETAIL="detail"`, `WARNING="warning"`); `ProgressEvent(level: ProgressLevel, message: str, current: int | None = None, total: int | None = None)` (frozen, slots); `ProgressCallback = Callable[[ProgressEvent], None]`; `emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None`; `ReadOptions.progress: ProgressCallback | None = None` (excluded from equality and repr). All exported from `bgmeter`.

- [ ] **Step 1: Write the failing tests**

Create `core/tests/test_progress.py`:

```python
import logging

import bgmeter
from bgmeter import ProgressEvent, ProgressLevel, ReadOptions, emit_progress


def test_progress_levels_have_stable_values():
    assert [level.value for level in ProgressLevel] == ["info", "detail", "warning"]


def test_progress_event_defaults_and_is_frozen():
    event = ProgressEvent(ProgressLevel.INFO, "Hello.")

    assert event.current is None and event.total is None
    try:
        event.message = "changed"
    except AttributeError:
        pass
    else:
        raise AssertionError("ProgressEvent must be frozen")


def test_emit_progress_ignores_a_missing_callback():
    emit_progress(None, ProgressEvent(ProgressLevel.INFO, "Hello."))


def test_emit_progress_delivers_the_event():
    seen = []
    event = ProgressEvent(ProgressLevel.DETAIL, "Reading.", current=1, total=2)

    emit_progress(seen.append, event)

    assert seen == [event]


def test_emit_progress_swallows_callback_errors_and_logs_them(caplog):
    def broken(event):
        raise RuntimeError("reporter exploded")

    with caplog.at_level(logging.WARNING, logger="bgmeter.progress"):
        emit_progress(broken, ProgressEvent(ProgressLevel.INFO, "Hello."))

    [record] = caplog.records
    assert record.name == "bgmeter.progress"
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.getMessage()


def test_read_options_progress_is_optional_and_not_part_of_identity():
    assert ReadOptions().progress is None
    assert ReadOptions(progress=print) == ReadOptions()
    assert "progress" not in repr(ReadOptions(progress=print))


def test_progress_api_is_exported():
    for name in ("ProgressCallback", "ProgressEvent", "ProgressLevel", "emit_progress"):
        assert name in bgmeter.__all__
        assert hasattr(bgmeter, name)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest core/tests/test_progress.py -q`
Expected: FAIL/ERROR with `ImportError: cannot import name 'ProgressEvent' from 'bgmeter'`.

- [ ] **Step 3: Create `core/src/bgmeter/progress.py`**

```python
"""Plain-language progress reporting shared by managers and drivers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


_log = logging.getLogger(__name__)


class ProgressLevel(StrEnum):
    """How much verbosity a progress event needs before a frontend shows it."""

    INFO = "info"
    DETAIL = "detail"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One plain-language sentence about what a meter operation is doing."""

    level: ProgressLevel
    message: str
    current: int | None = None
    total: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


def emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    """Deliver an event; a missing or failing callback never affects the caller."""

    if callback is None:
        return
    try:
        callback(event)
    except Exception as error:
        _log.warning(
            "progress callback %r raised %s: %s; event dropped",
            callback,
            type(error).__name__,
            error,
        )


__all__ = ["ProgressCallback", "ProgressEvent", "ProgressLevel", "emit_progress"]
```

- [ ] **Step 4: Add `ReadOptions.progress`**

In `core/src/bgmeter/models.py`, add the import after the existing stdlib imports (after `from uuid import UUID`):

```python
from .progress import ProgressCallback
```

Replace the `ReadOptions` class:

```python
@dataclass(frozen=True, slots=True)
class ReadOptions:
    timezone: str | None = None
    request_timeout: float = 5.0
    retries: int = 3
    progress: ProgressCallback | None = field(default=None, compare=False, repr=False)
```

- [ ] **Step 5: Export from `core/src/bgmeter/__init__.py`**

Add after the `from .models import (...)` block:

```python
from .progress import ProgressCallback, ProgressEvent, ProgressLevel, emit_progress
```

In `__all__`, add these four entries in alphabetical position: `"ProgressCallback"`, `"ProgressEvent"`, `"ProgressLevel"` (after `"NotificationCallback"`, before `"ProtocolError"`), and `"emit_progress"` (before `"interpret_meter_datetime"`, after `"discover_meters"`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest core/tests/test_progress.py core/tests/test_public_contract.py -q`
Expected: all PASS (the second file confirms `ReadOptions` is still frozen and the API contract still holds).

- [ ] **Step 7: Leave changes uncommitted** (user git rule). Nothing to run.

---

### Task 2: Manager progress events

**Files:**
- Modify: `core/src/bgmeter/manager.py`
- Test: `core/tests/test_manager.py` (append tests; extend `FakeDriver` and `make_manager`)

**Interfaces:**
- Consumes: `ProgressCallback`, `ProgressEvent`, `ProgressLevel`, `emit_progress`, `ReadOptions.progress` from Task 1.
- Produces: `MeterManager(*, registry, transports, progress: ProgressCallback | None = None)`; `MeterManager.default(*, progress=None)`; `ConnectedMeter(device, driver, session, progress=None)`. When a caller's `ReadOptions.progress` is `None`, the manager's callback is filled in; an explicit value wins.

- [ ] **Step 1: Extend the test fakes**

In `core/tests/test_manager.py`:

1. Add to the imports from `bgmeter`: `ProgressLevel` (keep the list alphabetical, after `MeterTransport`).
2. In `FakeDriver.__init__`, add `self.read_options = []` after `self.read_sessions = []`.
3. In `FakeDriver.read_records`, add `self.read_options.append(options)` as the first line after `self.read_sessions.append(session)`.
4. Replace `make_manager`:

```python
def make_manager(driver=None, transport=None, progress=None):
    driver = driver or FakeDriver()
    transport = transport or FakeTransport((make_endpoint(),))
    registry = DriverRegistry()
    registry.register(lambda: driver, source="test")
    manager = MeterManager(
        registry=registry, transports=(transport,), progress=progress
    )
    return manager, driver, transport
```

- [ ] **Step 2: Write the failing tests** (append to `core/tests/test_manager.py`)

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest core/tests/test_manager.py -q -k "progress or reports"`
Expected: FAIL with `TypeError: MeterManager.__init__() got an unexpected keyword argument 'progress'`.

- [ ] **Step 4: Implement in `core/src/bgmeter/manager.py`**

Add to the imports (after the `from .models import ...` line):

```python
from .progress import ProgressCallback, ProgressEvent, ProgressLevel, emit_progress
```

Add these module-level helpers after `_SessionCloseError` and before `_session_scope`:

```python
def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _endpoint_label(endpoint: TransportEndpoint) -> str:
    return endpoint.name or endpoint.identifier


def _device_label(device: MeterDevice) -> str:
    return device.identity.model or _endpoint_label(device.endpoint)
```

Replace `ConnectedMeter`:

```python
class ConnectedMeter:
    """A selected meter bound to one live transport session."""

    def __init__(
        self,
        device: MeterDevice,
        driver: MeterDriver,
        session: TransportSession,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.device = device
        self._driver = driver
        self._session = session
        self._progress = progress

    async def read_records(self, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        if options.progress is None and self._progress is not None:
            options = replace(options, progress=self._progress)
        result = await self._driver.read_records(self._session, self.device, options)
        if result.completion is CompletionStatus.COMPLETE:
            emit_progress(
                options.progress,
                ProgressEvent(
                    ProgressLevel.INFO,
                    f"Read {_plural(len(result.records), 'record')}. "
                    "All records were received.",
                ),
            )
        return result
```

In `MeterManager.__init__`, add the keyword parameter and store it:

```python
    def __init__(
        self,
        *,
        registry: DriverRegistry,
        transports: Iterable[MeterTransport],
        progress: ProgressCallback | None = None,
    ) -> None:
        self.registry = registry
        self.transports = tuple(transports)
        self._progress = progress
        self._transports_by_name: dict[str, MeterTransport] = {}
        # (existing duplicate-transport loop unchanged)
```

Replace `default`:

```python
    @classmethod
    def default(cls, *, progress: ProgressCallback | None = None) -> MeterManager:
        registry = DriverRegistry()
        registry.register_available_entry_points()
        return cls(registry=registry, transports=(BleTransport(),), progress=progress)

    def _say(self, level: ProgressLevel, message: str) -> None:
        emit_progress(self._progress, ProgressEvent(level, message))
```

In `discover`, inside the `for transport in self.transports:` loop replace the `endpoints = await transport.discover(timeout=timeout)` line with:

```python
            self._say(
                ProgressLevel.INFO, f"Looking for meters nearby ({timeout:g} s)..."
            )
            endpoints = await transport.discover(timeout=timeout)
            self._say(
                ProgressLevel.DETAIL,
                f"Found {_plural(len(endpoints), 'device')} nearby.",
            )
```

In `_identify_endpoint`, change the `if not matches: return None` block to:

```python
        if not matches:
            self._say(
                ProgressLevel.DETAIL,
                f"Skipped '{_endpoint_label(endpoint)}': not a supported meter.",
            )
            return None
```

and replace the probe section (from `session = await self._connect(...)` to the `return MeterDevice(...)`) with:

```python
            session = await self._connect(transport, endpoint)
            async with _session_scope(session):
                try:
                    identity = await driver.probe(session)
                except UnsupportedDeviceError:
                    self._say(
                        ProgressLevel.DETAIL,
                        f"'{_endpoint_label(endpoint)}' did not respond like a "
                        "supported meter.",
                    )
                    continue
            self._say(
                ProgressLevel.INFO,
                f"Found {identity.model or _endpoint_label(endpoint)} "
                f"({endpoint.identifier}).",
            )
            return MeterDevice(
                selector=selector,
                driver_id=driver.driver_id,
                endpoint=endpoint,
                identity=identity,
                match=match,
            )
```

In `open`, replace the last three lines (`session = ...` through `yield ConnectedMeter(...)`) with:

```python
        self._say(ProgressLevel.INFO, f"Connecting to {_device_label(device)}...")
        session = await self._connect(transport, device.endpoint)
        async with _session_scope(session):
            try:
                yield ConnectedMeter(device, driver, session, self._progress)
            finally:
                self._say(ProgressLevel.DETAIL, "Disconnecting from the meter.")
```

- [ ] **Step 5: Run the touched suites**

Run: `python -m pytest core/tests/test_manager.py core/tests/test_progress.py core/tests/test_public_contract.py core/tests/test_final_review.py -q`
Expected: all PASS.

- [ ] **Step 6: Leave changes uncommitted** (user git rule).

---

### Task 3: Core logging

**Files:**
- Modify: `core/src/bgmeter/__init__.py`, `core/src/bgmeter/manager.py`, `core/src/bgmeter/drivers.py`, `core/src/bgmeter/transports/bleak.py`
- Test: `core/tests/test_manager.py` (append; add `import logging` at the top)

**Interfaces:**
- Consumes: Task 2's `manager.py`.
- Produces: loggers `bgmeter`, `bgmeter.manager`, `bgmeter.drivers`, `bgmeter.transports.bleak`, `bgmeter.progress` (already used in Task 1). The `bgmeter` logger carries a `NullHandler`. Level rules: ERROR = a failure the layer absorbs instead of raising; WARNING = recoverable; INFO = milestones (no glucose values or serial numbers); DEBUG = byte-level detail. A layer never logs an exception it re-raises. Notification hex is logged by drivers, not by the transport (the transport logs only sizes for notifications).

- [ ] **Step 1: Write the failing tests** (append to `core/tests/test_manager.py`; add `import logging` next to the other stdlib imports)

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest core/tests/test_manager.py -q -k "logs or null_handler or absorbed or probe_rejection or registry_logs"`
Expected: FAIL (no loggers exist; `assert any(...)` failures and `[record] = []` unpack errors).

- [ ] **Step 3: `NullHandler` in `core/src/bgmeter/__init__.py`**

Add directly under the module docstring:

```python
import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())
```

- [ ] **Step 4: Logging in `core/src/bgmeter/manager.py`**

Add `import logging` to the stdlib imports and, below the imports, `_log = logging.getLogger(__name__)`.

Apply these edits:

1. `_session_scope`, inside `except BaseException as primary:` -> inner `except BaseException as cleanup:` -- add before the `primary.add_note(...)` line:

```python
            _log.error(
                "session cleanup failed after %s: %s: %s",
                type(primary).__name__,
                type(cleanup).__name__,
                cleanup,
            )
```

2. `ConnectedMeter.read_records`, immediately after `result = await self._driver.read_records(...)`:

```python
        _log.info(
            "read finished: device=%s completion=%s received=%d expected=%s "
            "retries=%d rejected=%d termination=%s",
            self.device.selector,
            result.completion.value,
            len(result.records),
            result.expected_count,
            result.retry_count,
            result.rejected_count,
            result.termination_reason,
        )
```

3. `discover`, after `descriptors = self.registry.descriptors()`:

```python
        _log.info(
            "discovery started: transports=%s drivers=%s timeout=%.1fs",
            [transport.name for transport in self.transports],
            [descriptor.driver_id for descriptor in descriptors],
            timeout,
        )
```

after `endpoint_found = endpoint_found or bool(endpoints)`:

```python
            _log.info(
                "transport %s discovered %d endpoint(s)", transport.name, len(endpoints)
            )
```

and before `if devices: return tuple(devices)`:

```python
        _log.info("discovery finished: %d supported device(s)", len(devices))
```

4. `_identify_endpoint`: in the `if not matches:` block, before the DETAIL `_say`, add:

```python
            _log.debug(
                "endpoint %s (%r) matched no driver", endpoint.identifier, endpoint.name
            )
```

after `selector = f"{endpoint.transport}:{endpoint.identifier}"` add:

```python
        _log.debug(
            "endpoint %s candidates: %s",
            selector,
            [(driver.driver_id, match.confidence, match.evidence) for driver, match in matches],
        )
```

change `except UnsupportedDeviceError:` in the probe to `except UnsupportedDeviceError as error:` and add as its first statement:

```python
                    _log.warning(
                        "driver %s probe rejected endpoint %s: %s",
                        driver.driver_id,
                        selector,
                        error,
                    )
```

and after the INFO `_say("Found ...")` call, before `return MeterDevice(`:

```python
            _log.info(
                "endpoint %s identified: driver=%s model=%s",
                selector,
                driver.driver_id,
                identity.model,
            )
```

5. `open`: before `self._say(ProgressLevel.INFO, f"Connecting to ...")` add `_log.info("opening %s with driver %s", device.selector, device.driver_id)`; in the `finally:` block add `_log.debug("closing session for %s", device.selector)` as the first line.

6. `read`, inside `except _SessionCloseError as error:` after the `if result is None or not result.records: raise` guard:

```python
            _log.error(
                "disconnect failed after a read that returned %d record(s); "
                "returning a partial result: %s",
                len(result.records),
                error,
            )
```

7. `_connect`, first line of the body:

```python
        _log.debug("connecting: transport=%s endpoint=%s", transport.name, endpoint.identifier)
```

- [ ] **Step 5: Logging in `core/src/bgmeter/drivers.py`**

Add `import logging` and `_log = logging.getLogger(__name__)` after the imports. In `register`, after `self._descriptors[driver.driver_id] = descriptor`:

```python
        _log.debug("registered driver %s (source=%s)", driver.driver_id, source)
```

In `register_available_entry_points`, at the top of `except Exception as error:` (before `key = point.name`):

```python
                _log.error(
                    "driver entry point %r failed to load: %s: %s",
                    point.name,
                    type(error).__name__,
                    error,
                )
                _log.debug(
                    "driver entry point %r load failure traceback",
                    point.name,
                    exc_info=True,
                )
```

- [ ] **Step 6: Logging in `core/src/bgmeter/transports/bleak.py`**

Add `import logging`, then after the imports:

```python
_log = logging.getLogger(__name__)


def _hex(data: bytes) -> str:
    return bytes(data).hex(" ")
```

Apply these edits:

- `_BleakGattSession.read_gatt_char`: replace the `try:` body so it reads
  ```python
        try:
            value = bytes(await self._client.read_gatt_char(characteristic))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _connection_error("read", error) from error
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug("GATT read %s -> %d bytes: %s", characteristic, len(value), _hex(value))
        return value
  ```
- `write_gatt_char`: before the `try:` add
  ```python
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug(
                "GATT write %s response=%s %d bytes: %s",
                characteristic,
                response,
                len(data),
                _hex(data),
            )
  ```
- `start_notify`: in `adapt_notification`, first line: `_log.debug("GATT notification from %s: %d bytes", getattr(sender, "uuid", sender), len(data))`; before the `try:` add `_log.debug("GATT subscribe %s", characteristic)`.
- `stop_notify`: before the `try:` add `_log.debug("GATT unsubscribe %s", characteristic)`.
- `close`: before the `try:` add `_log.debug("BLE disconnect %s", self.endpoint.identifier)`.
- `BleTransport.discover`: after `scanner = self._scanner_factory()` add `_log.info("BLE scan started: timeout=%.1fs", timeout)`; inside the inner `except BaseException as cleanup:` add
  ```python
                    _log.error(
                        "BLE scanner cleanup failed after %s: %s: %s",
                        type(primary).__name__,
                        type(cleanup).__name__,
                        cleanup,
                    )
  ```
  and after the `endpoints = [...]` list comprehension (before `return`), add
  ```python
        for endpoint in endpoints:
            _log.debug(
                "BLE endpoint %s name=%r services=%s rssi=%s",
                endpoint.identifier,
                endpoint.name,
                sorted(endpoint.service_uuids),
                endpoint.metadata.get("rssi"),
            )
        _log.info("BLE scan finished: %d endpoint(s)", len(endpoints))
  ```
- `BleTransport.connect`: after `target = ...` add `_log.debug("BLE connect: target=%s", target)`; replace the `try: await client.connect(); return _BleakGattSession(endpoint, client)` pair with
  ```python
        try:
            await client.connect()
            session = _BleakGattSession(endpoint, client)
            _log.info(
                "BLE connected: %s services=%d", endpoint.identifier, len(session.services)
            )
            for service in session.services:
                _log.debug(
                    "BLE service %s characteristics=%s",
                    service.uuid,
                    [characteristic.uuid for characteristic in service.characteristics],
                )
            return session
  ```
  and in the `except BaseException as error:` block change the inner `except BaseException:` (around `await client.disconnect()`) to `except BaseException as cleanup:` with body
  ```python
                _log.error(
                    "BLE disconnect after a failed connect also failed: %s: %s",
                    type(cleanup).__name__,
                    cleanup,
                )
  ```
  (replacing the bare `pass`).

- [ ] **Step 7: Run the touched suites**

Run: `python -m pytest core/tests -q`
Expected: all PASS (this is the whole `core` suite, small and fast; it also confirms no behavior changed).

- [ ] **Step 8: Leave changes uncommitted** (user git rule).

---

### Task 4: MicroTech progress events

**Files:**
- Modify: `drivers/microtech/src/bgmeter_microtech/collector.py`, `protocol.py`, `driver.py`
- Test: `drivers/microtech/tests/test_protocol.py`, `drivers/microtech/tests/test_driver.py`

**Interfaces:**
- Consumes: `ProgressCallback`, `ProgressEvent`, `ProgressLevel`, `emit_progress`, `ReadOptions.progress` from Task 1.
- Produces: `HistoryRecordCollector.transmission_count: int`, `.record_count: int`, `.attributions_since(start: int) -> tuple[str, ...]`; `read_history(session, characteristic, *, request_timeout=5.0, retries=3, progress: ProgressCallback | None = None)`. The built-in driver emits no `WARNING` events. Event wording is fixed by the tests below.

- [ ] **Step 1: Write the failing protocol tests** (append to `drivers/microtech/tests/test_protocol.py`)

```python
def _progress_summary(events):
    return [(event.level.value, event.message) for event in events]


@pytest.mark.asyncio
async def test_read_history_reports_each_accepted_record() -> None:
    events = []
    session = FakeGattSession({index: [_indexed_frames(index)] for index in range(4)})

    await read_history(
        session, "ffe1", request_timeout=0.001, retries=1, progress=events.append
    )

    assert _progress_summary(events) == [
        ("detail", "Asking the meter how many records it holds."),
        ("info", "Reading records: 1 of 4"),
        ("info", "Reading records: 2 of 4"),
        ("info", "Reading records: 3 of 4"),
        ("info", "Reading records: 4 of 4"),
    ]
    assert [(event.current, event.total) for event in events[1:]] == [
        (1, 4),
        (2, 4),
        (3, 4),
        (4, 4),
    ]


@pytest.mark.asyncio
async def test_read_history_reports_retries_and_giving_up() -> None:
    events = []
    session = FakeGattSession(
        {0: [_frames(0)], 1: [_frames(1)], 2: [None, None], 3: [_frames(3)]}
    )

    await read_history(
        session, "ffe1", request_timeout=0.001, retries=2, progress=events.append
    )

    assert _progress_summary(events) == [
        ("detail", "Asking the meter how many records it holds."),
        ("info", "Reading records: 1 of 4"),
        ("info", "Reading records: 2 of 4"),
        ("info", "The meter did not send a usable reply; trying again (2 of 2)."),
        ("detail", "Gave up on record 2 after 2 tries and moved on."),
        ("info", "Reading records: 3 of 4"),
    ]


@pytest.mark.asyncio
async def test_read_history_explains_a_rejected_reply_in_plain_words() -> None:
    events = []
    wrong_event = tuple(
        bytes.fromhex(value) for value in WRONG_EVENT_THREE_FOR_REQUEST_TWO
    )
    session = FakeGattSession(
        {0: [_frames(0)], 1: [_frames(1)], 2: [wrong_event], 3: [_frames(3)]}
    )

    await read_history(
        session, "ffe1", request_timeout=0.001, retries=1, progress=events.append
    )

    assert _progress_summary(events) == [
        ("detail", "Asking the meter how many records it holds."),
        ("info", "Reading records: 1 of 4"),
        ("info", "Reading records: 2 of 4"),
        (
            "detail",
            "Ignored a reply for a different record than the one we asked for.",
        ),
        ("detail", "Gave up on record 2 after 1 try and moved on."),
        ("info", "Reading records: 3 of 4"),
    ]


@pytest.mark.asyncio
async def test_read_history_reports_giving_up_on_the_latest_record() -> None:
    events = []
    session = FakeGattSession({0: [None, None, None]})

    await read_history(
        session, "ffe1", request_timeout=0.001, retries=3, progress=events.append
    )

    assert _progress_summary(events) == [
        ("detail", "Asking the meter how many records it holds."),
        ("info", "The meter did not send a usable reply; trying again (2 of 3)."),
        ("info", "The meter did not send a usable reply; trying again (3 of 3)."),
        ("detail", "Gave up asking the meter for its latest record after 3 tries."),
    ]


def test_collector_exposes_transmission_and_record_counters() -> None:
    collector = HistoryRecordCollector()

    assert collector.transmission_count == 0
    assert collector.record_count == 0
    for fragment in _frames(2):
        collector.add_notification(fragment)

    assert collector.record_count == 1
    assert collector.transmission_count == 1
    assert collector.attributions_since(0) == ("accepted",)
    assert collector.attributions_since(1) == ()


def test_progress_is_optional_for_read_history_callers() -> None:
    import inspect

    assert inspect.signature(read_history).parameters["progress"].default is None
```

- [ ] **Step 2: Write the failing driver tests** (append to `drivers/microtech/tests/test_driver.py`; add `MeterTimeoutError` and `ProgressLevel` to the `from bgmeter import (...)` list at the top)

```python
@pytest.mark.asyncio
async def test_no_usable_records_reports_one_plain_error_and_no_warning_events() -> None:
    events = []
    driver = driver_factory()
    endpoint = _endpoint()
    session = CaptureGattSession(endpoint)

    with pytest.raises(MeterTimeoutError) as caught:
        await driver.read_records(
            session,
            _device(endpoint, driver),
            ReadOptions(request_timeout=0.001, progress=events.append),
        )

    assert str(caught.value) == "The meter connected but did not send any usable records."
    assert [event.message for event in events] == [
        "Asking the meter how many records it holds.",
        "The meter did not send a usable reply; trying again (2 of 3).",
        "The meter did not send a usable reply; trying again (3 of 3).",
        "Gave up asking the meter for its latest record after 3 tries.",
    ]
    assert all(event.level is not ProgressLevel.WARNING for event in events)


@pytest.mark.asyncio
async def test_capture_read_forwards_progress_to_the_history_reader() -> None:
    events = []
    driver = driver_factory()
    endpoint = _endpoint()
    session = CaptureGattSession(endpoint, notifications=_capture_notifications())

    await driver.read_records(
        session,
        _device(endpoint, driver),
        ReadOptions(request_timeout=0.001, progress=events.append),
    )

    assert (events[-1].current, events[-1].total) == (4, 4)
    assert all(event.level is not ProgressLevel.WARNING for event in events)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest drivers/microtech/tests/test_protocol.py drivers/microtech/tests/test_driver.py -q -k "progress or reports or explains or counters or no_usable"`
Expected: FAIL (`TypeError: read_history() got an unexpected keyword argument 'progress'`, missing `transmission_count`, old error text).

- [ ] **Step 4: Collector counters** -- in `drivers/microtech/src/bgmeter_microtech/collector.py`, add after the `is_complete` property:

```python
    @property
    def transmission_count(self) -> int:
        return len(self._transmissions)

    @property
    def record_count(self) -> int:
        return len(self._records)

    def attributions_since(self, start: int) -> tuple[str, ...]:
        """Return the attribution of every transmission appended from ``start`` on."""
        return tuple(
            transmission.attribution for transmission in self._transmissions[start:]
        )
```

- [ ] **Step 5: Replace `drivers/microtech/src/bgmeter_microtech/protocol.py`** with:

```python
"""Indexed MicroTech history retrieval over a connected GATT session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from bgmeter import (
    GattSession,
    ProgressCallback,
    ProgressEvent,
    ProgressLevel,
    emit_progress,
)

from .collector import HistoryRecordCollector
from .framing import build_history_request


_MISMATCHED_REPLY = "a reply that did not match our request"
_DAMAGED_REPLY = "a damaged reply"
_REJECTION_REASONS = {
    "rejected_unmatched_token": _MISMATCHED_REPLY,
    "rejected_token_mismatch": _MISMATCHED_REPLY,
    "rejected_pre_write": _MISMATCHED_REPLY,
    "rejected_wrong_event": "a reply for a different record than the one we asked for",
    "rejected_empty_response": "a reply that contained no record",
    "rejected_invalid_notification": _DAMAGED_REPLY,
    "rejected_conflicting_fragment": _DAMAGED_REPLY,
    "rejected_malformed_transmission": _DAMAGED_REPLY,
    "duplicate_conflict": "a reply that disagreed with a record we already had",
}


def _rejection_reason(attribution: str) -> str | None:
    """Plain-language reason for a collector verdict, or None if it is not a rejection."""
    reason = _REJECTION_REASONS.get(attribution)
    if reason is None and attribution.startswith("rejected_"):
        return "an incomplete reply"
    return reason


def _tries(count: int) -> str:
    return f"{count} {'try' if count == 1 else 'tries'}"


async def read_history(
    session: GattSession,
    characteristic: str,
    *,
    request_timeout: float = 5.0,
    retries: int = 3,
    progress: ProgressCallback | None = None,
) -> HistoryRecordCollector:
    """Read indexed history, stopping notification reception in all outcomes."""
    if request_timeout <= 0:
        raise ValueError("request_timeout must be positive")
    if retries <= 0:
        raise ValueError("retries must be positive")

    collector = HistoryRecordCollector(strict_live_mode=True)
    updated = asyncio.Event()

    def say(
        level: ProgressLevel,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        emit_progress(progress, ProgressEvent(level, message, current, total))

    def handle_notification(_sender: object, data: bytes) -> None:
        start = collector.transmission_count
        added = collector.add_notification(bytes(data))
        for attribution in collector.attributions_since(start):
            reason = _rejection_reason(attribution)
            if reason is not None:
                say(ProgressLevel.DETAIL, f"Ignored {reason}.")
        if added:
            current, total = collector.record_count, collector.expected_count
            say(
                ProgressLevel.INFO,
                f"Reading records: {current} of {total}"
                if total is not None
                else f"Reading records: {current}",
                current=current,
                total=total,
            )
        updated.set()

    async def wait_until(predicate: Callable[[], bool]) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request_timeout
        while not predicate():
            updated.clear()
            if predicate():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(updated.wait(), timeout=remaining)
            except TimeoutError:
                return predicate()
        return True

    async def request_until(
        event_index: int,
        predicate: Callable[[], bool],
    ) -> bool:
        request = build_history_request(event_index)
        for attempt in range(retries):
            if attempt:
                collector.retry_count += 1
                say(
                    ProgressLevel.INFO,
                    "The meter did not send a usable reply; "
                    f"trying again ({attempt + 1} of {retries}).",
                )
            elif event_index == 0:
                say(ProgressLevel.DETAIL, "Asking the meter how many records it holds.")
            collector.register_request(event_index, request)
            await session.write_gatt_char(
                characteristic,
                request,
                response=False,
            )
            if await wait_until(predicate):
                return True
        if event_index == 0:
            say(
                ProgressLevel.DETAIL,
                f"Gave up asking the meter for its latest record after {_tries(retries)}.",
            )
        else:
            say(
                ProgressLevel.DETAIL,
                f"Gave up on record {event_index} after {_tries(retries)} and moved on.",
            )
        return False

    await session.start_notify(characteristic, handle_notification)
    try:
        found_latest = await request_until(
            0,
            lambda: collector.expected_count is not None,
        )
        if found_latest:
            latest_index = collector.expected_count
            for event_index in range(1, latest_index + 1):
                if collector.has_index(event_index):
                    continue
                await request_until(
                    event_index,
                    lambda index=event_index: collector.has_index(index),
                )
                if collector.is_complete:
                    break
    except BaseException as primary:
        try:
            await session.stop_notify(characteristic)
        except BaseException as cleanup:
            primary.add_note(
                "MicroTech notification cleanup also failed: "
                f"{type(cleanup).__name__}: {cleanup}"
            )
        finally:
            collector.finalize_pending()
        raise
    else:
        try:
            await session.stop_notify(characteristic)
        except asyncio.CancelledError:
            raise
        except Exception as cleanup:
            collector.record_cleanup_failure("stop_notify", cleanup)
        finally:
            collector.finalize_pending()

    return collector
```

- [ ] **Step 6: Driver changes** -- in `drivers/microtech/src/bgmeter_microtech/driver.py`:

Pass the callback: in `read_records`, change the `read_history(...)` call to add `progress=options.progress,` after `retries=options.retries,`.

Reword the error:

```python
        if not collector.records:
            raise MeterTimeoutError(
                "The meter connected but did not send any usable records."
            )
```

- [ ] **Step 7: Run the touched suites**

Run: `python -m pytest drivers/microtech/tests/test_protocol.py drivers/microtech/tests/test_driver.py -q`
Expected: all PASS, including the pre-existing tests (the `capsys` "nothing on stdout/stderr" assertions still hold because progress goes only to the callback).

- [ ] **Step 8: Leave changes uncommitted** (user git rule).

---

### Task 5: MicroTech logging

**Files:**
- Modify: `drivers/microtech/src/bgmeter_microtech/__init__.py`, `collector.py`, `protocol.py`, `driver.py`
- Test: `drivers/microtech/tests/test_protocol.py`, `drivers/microtech/tests/test_driver.py` (add `import logging` to both)

**Interfaces:**
- Consumes: Task 4's `protocol.py`, `collector.py`, `driver.py`.
- Produces: loggers `bgmeter_microtech`, `.protocol`, `.collector`, `.driver`. The `bgmeter_microtech` logger carries a `NullHandler`; `bgmeter_microtech.__all__` is unchanged (an existing test pins it). Message formats the tests rely on: `request event_index=<n> attempt=<i>/<total>`, `transmission attribution=<verdict> ...`, `history read finished: records=<n> ...`, `read_records finished: completion=<c> records=<n> ...`.

- [ ] **Step 1: Write the failing protocol tests** (append to `drivers/microtech/tests/test_protocol.py`; add `import logging` to the stdlib imports)

```python
@pytest.mark.asyncio
async def test_read_history_logs_requests_notifications_and_verdicts_at_debug(caplog) -> None:
    session = FakeGattSession({index: [_indexed_frames(index)] for index in range(4)})

    with caplog.at_level(logging.DEBUG, logger="bgmeter_microtech"):
        await read_history(session, "ffe1", request_timeout=0.001, retries=1)

    protocol = [
        r.getMessage() for r in caplog.records if r.name == "bgmeter_microtech.protocol"
    ]
    collector = [
        r.getMessage() for r in caplog.records if r.name == "bgmeter_microtech.collector"
    ]
    assert any(m.startswith("request event_index=0 attempt=1/1") for m in protocol)
    assert any(_indexed_frames(0)[2].hex(" ") in m for m in protocol)
    assert sum("attribution=accepted" in m for m in collector) == 4
    assert any(m.startswith("history read finished: records=4") for m in protocol)


@pytest.mark.asyncio
async def test_read_history_logs_exhausted_retries_and_rejections_at_warning(caplog) -> None:
    wrong_event = tuple(
        bytes.fromhex(value) for value in WRONG_EVENT_THREE_FOR_REQUEST_TWO
    )
    session = FakeGattSession(
        {0: [_frames(0)], 1: [_frames(1)], 2: [wrong_event], 3: [_frames(3)]}
    )

    with caplog.at_level(logging.WARNING, logger="bgmeter_microtech"):
        await read_history(session, "ffe1", request_timeout=0.001, retries=1)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(m.startswith("request event_index=2 unanswered after 1 attempt(s)") for m in warnings)
    assert any(
        m.startswith("history read rejected 1 transmission(s)")
        and "rejected_wrong_event" in m
        for m in warnings
    )


@pytest.mark.asyncio
async def test_read_history_logs_absorbed_unsubscribe_failure_at_error(caplog) -> None:
    class FailingStopSession(FakeGattSession):
        async def stop_notify(self, characteristic: str) -> None:
            await super().stop_notify(characteristic)
            raise RuntimeError("unsubscribe failed")

    session = FailingStopSession({index: [_indexed_frames(index)] for index in range(4)})

    with caplog.at_level(logging.ERROR, logger="bgmeter_microtech"):
        await read_history(session, "ffe1", request_timeout=0.01, retries=1)

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.name == "bgmeter_microtech.protocol"
    assert "RuntimeError" in record.getMessage()
    assert "unsubscribe failed" in record.getMessage()
```

- [ ] **Step 2: Write the failing driver tests** (append to `drivers/microtech/tests/test_driver.py`)

```python
def test_microtech_package_logger_has_a_null_handler() -> None:
    handlers = logging.getLogger("bgmeter_microtech").handlers

    assert any(isinstance(handler, logging.NullHandler) for handler in handlers)


@pytest.mark.asyncio
async def test_info_logging_never_contains_serials_or_glucose_data(caplog) -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    session = CaptureGattSession(endpoint, notifications=_capture_notifications())

    with caplog.at_level(logging.INFO, logger="bgmeter_microtech"):
        await driver.probe(session)
        await driver.read_records(
            session,
            _device(endpoint, driver),
            ReadOptions(request_timeout=0.001),
        )

    assert "read_records finished: completion=complete records=4" in caplog.text
    assert "Z00MCF" not in caplog.text
    assert "10 13 0a 10 1b" not in caplog.text
    assert "7.44" not in caplog.text


@pytest.mark.asyncio
async def test_no_usable_records_leaves_technical_evidence_in_the_log(caplog) -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    session = CaptureGattSession(endpoint)

    with caplog.at_level(logging.INFO, logger="bgmeter_microtech"):
        with pytest.raises(MeterTimeoutError):
            await driver.read_records(
                session,
                _device(endpoint, driver),
                ReadOptions(request_timeout=0.001),
            )

    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("history read finished: records=0") for m in messages)
    assert any("notifications=0" in m for m in messages)
    assert "The meter connected but did not send any usable records." not in caplog.text
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest drivers/microtech/tests/test_protocol.py drivers/microtech/tests/test_driver.py -q -k "logs or logging or null_handler or technical_evidence"`
Expected: FAIL (no loggers; missing `NullHandler`).

- [ ] **Step 4: `NullHandler` in `drivers/microtech/src/bgmeter_microtech/__init__.py`**

Add `import logging` above `from .driver import ...` and, after the imports, `logging.getLogger(__name__).addHandler(logging.NullHandler())`. Leave `__all__` exactly as is.

- [ ] **Step 5: Verdict logging in `collector.py`**

Add `import logging` to the stdlib imports and `_log = logging.getLogger(__name__)` after the imports. At the end of `_append_transmission` (after the `if attribution.startswith("rejected_") ...` block) add:

```python
        _log.debug(
            "transmission attribution=%s sequence=%s fragments=%d response_bytes=%s "
            "requested_index=%s record_indexes=%s",
            attribution,
            transport_sequence,
            len(fragments),
            len(response) if response is not None else None,
            matched_request.event_index if matched_request is not None else None,
            [record.event_index for record in records],
        )
```

- [ ] **Step 6: Logging in `protocol.py`**

Add `import logging` and `from collections import Counter` to the stdlib imports; add `_log = logging.getLogger(__name__)` after `_DAMAGED_REPLY`'s block (before `_rejection_reason`). Then:

1. In `read_history`, after the two `ValueError` guards (before `collector = ...`):

```python
    _log.info(
        "history read started: characteristic=%s request_timeout=%.1fs retries=%d",
        characteristic,
        request_timeout,
        retries,
    )
```

2. First lines of `handle_notification`:

```python
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug("notification %d bytes: %s", len(data), bytes(data).hex(" "))
```

3. In `request_until`, right after `collector.register_request(event_index, request)`:

```python
            _log.debug(
                "request event_index=%d attempt=%d/%d bytes: %s",
                event_index,
                attempt + 1,
                retries,
                request.hex(" "),
            )
```

4. In `request_until`, immediately before the `if event_index == 0:` block that emits the "Gave up" progress events:

```python
        _log.warning(
            "request event_index=%d unanswered after %d attempt(s) (timeout %.1fs each)",
            event_index,
            retries,
            request_timeout,
        )
```

5. In the `else:` branch's `except Exception as cleanup:` (the one calling `collector.record_cleanup_failure`), add as the first statement:

```python
            _log.error(
                "stop_notify failed: %s: %s", type(cleanup).__name__, cleanup
            )
```

6. Replace the final `return collector` with:

```python
    _log.info(
        "history read finished: records=%d expected=%s notifications=%d "
        "complete_messages=%d retries=%d rejected=%d duplicates=%d status=%s",
        collector.record_count,
        collector.expected_count,
        collector.notification_count,
        collector.complete_message_count,
        collector.retry_count,
        collector.rejected_transmission_count,
        collector.duplicate_transmission_count,
        collector.status.value,
    )
    rejected = Counter(
        attribution
        for attribution in collector.attributions_since(0)
        if attribution.startswith("rejected_") or attribution == "duplicate_conflict"
    )
    if rejected:
        _log.warning(
            "history read rejected %d transmission(s): %s",
            sum(rejected.values()),
            dict(sorted(rejected.items())),
        )
    return collector
```

- [ ] **Step 7: Logging in `driver.py`**

Add `import logging` and `_log = logging.getLogger(__name__)` after the imports.

- `probe`: change the inner `except Exception:` (around `session.read_gatt_char`) to

```python
                except Exception as error:
                    _log.warning(
                        "probe could not read characteristic %s: %s: %s",
                        short_uuid,
                        type(error).__name__,
                        error,
                    )
                    continue
```

and, just before `return MeterIdentity(metadata=metadata, **values)`:

```python
        _log.debug(
            "probe read device information fields: %s",
            sorted(name for name, value in values.items() if value is not None),
        )
```

- `read_records`: after `started_at = datetime.now(UTC)` add

```python
        _log.info(
            "read_records started: device=%s timezone=%s request_timeout=%.1fs retries=%d",
            device.selector,
            options.timezone,
            options.request_timeout,
            options.retries,
        )
```

and replace the trailing `return ReadResult(...)` with `result = ReadResult(...)` (same arguments), followed by:

```python
        _log.info(
            "read_records finished: completion=%s records=%d expected=%s termination=%s",
            result.completion.value,
            result.received_count,
            result.expected_count,
            result.termination_reason,
        )
        return result
```

- [ ] **Step 8: Run the touched suites**

Run: `python -m pytest drivers/microtech/tests -q`
Expected: all PASS (the `capsys` "no stdout/stderr" assertions still hold; logging is silent without a handler thanks to the `NullHandler`; the hardware test is skipped).

- [ ] **Step 9: Leave changes uncommitted** (user git rule).

---

### Task 6: CLI verbosity, hints, and outcome line

**Files:**
- Create: `cli/src/bgmeter_cli/reporter.py`
- Modify: `cli/src/bgmeter_cli/app.py`
- Modify: `cli/tests/test_cli.py` (mechanical factory-signature update only)
- Test: `cli/tests/test_reporting.py` (new)

**Interfaces:**
- Consumes: `ProgressEvent`, `ProgressLevel`, `ProgressCallback`, `MeterManager(..., progress=...)` from Tasks 1-2.
- Produces: `ConsoleReporter(stream: TextIO, verbosity: int, *, clock: Callable[[], float] = time.monotonic)` -- callable as `reporter(event)`; `ManagerFactory = Callable[[object, ProgressCallback | None], object]` called as `manager_factory(registry, progress)`; parser destinations `verbose_top` / `verbose_sub`; `_verbosity(arguments) -> int` (0-2); `_classify(error) -> tuple[int, str | None] | None`; `_add_reporting_options(parser, suffix)` (extended in Task 7 with the log options).

- [ ] **Step 1: Mechanical update of the existing test fakes**

The factory gains a second argument. Update every fake in `cli/tests/test_cli.py` so it accepts it (they still work with the old one-argument call, so the suite stays green before and after):

```powershell
$path = "cli\tests\test_cli.py"
(Get-Content $path -Raw) `
  -replace 'lambda registry:', 'lambda registry, progress=None:' `
  -replace 'def manager_factory\(registry\):', 'def manager_factory(registry, progress=None):' |
  Set-Content $path -NoNewline -Encoding utf8
(Select-String -Path $path -Pattern 'progress=None').Count
```

Expected: prints `14`.

Run: `python -m pytest cli/tests/test_cli.py -q`
Expected: all PASS.

- [ ] **Step 2: Write the failing tests**

Create `cli/tests/test_reporting.py`:

```python
from __future__ import annotations

import io
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from bgmeter import (  # noqa: E402
    CompletionStatus,
    DeviceNotFoundError,
    DiscoveryError,
    MeterConnectionError,
    MeterTimeoutError,
    ProgressEvent,
    ProgressLevel,
    ProtocolError,
    UnsupportedDeviceError,
)
from bgmeter_cli.config import DriverConfig, save_config  # noqa: E402
from bgmeter_cli.reporter import ConsoleReporter  # noqa: E402
from test_cli import (  # noqa: E402,F401
    FakeEntryPoint,
    FakeManager,
    complete_result,
    driver_factory,
    install_entry_points,
    invoke,
    make_device,
)


READ = ["read", "--device", "fake:meter-1"]


def event(level, message, current=None, total=None):
    return ProgressEvent(level, message, current, total)


class ScriptedManager(FakeManager):
    """A fake manager that reports scripted progress events during a read."""

    def __init__(self, registry, progress, *, script=(), **kwargs):
        super().__init__(registry, **kwargs)
        self.progress = progress
        self.script = tuple(script)

    async def read(self, device, options=None):
        for item in self.script:
            self.progress(item)
        return await super().read(device, options)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    install_entry_points(monkeypatch, FakeEntryPoint("example", driver_factory()))
    path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), path)
    return path


def read_with(config_path, result, argv=READ, *, script=(), **manager_kwargs):
    def factory(registry, progress):
        return ScriptedManager(
            registry,
            progress,
            devices=(make_device(),),
            result=result,
            script=script,
            **manager_kwargs,
        )

    return invoke(argv, config_path=config_path, manager_factory=factory)


SCRIPT = (
    event(ProgressLevel.INFO, "Connecting to Meter 1..."),
    event(ProgressLevel.DETAIL, "Asking the meter how many records it holds."),
    event(ProgressLevel.WARNING, "Something looked odd."),
)


def test_reporter_shows_only_warnings_by_default():
    stream = io.StringIO()
    reporter = ConsoleReporter(stream, 0)

    for item in SCRIPT:
        reporter(item)

    assert stream.getvalue() == "warning: Something looked odd.\n"


def test_reporter_prefixes_elapsed_time_at_double_verbosity():
    times = iter([100.0, 101.24])
    stream = io.StringIO()
    reporter = ConsoleReporter(stream, 2, clock=lambda: next(times))

    reporter(event(ProgressLevel.INFO, "Hello."))

    assert stream.getvalue() == "[ 1.2s] Hello.\n"


def test_reporter_caps_verbosity_at_two():
    stream = io.StringIO()
    reporter = ConsoleReporter(stream, 9, clock=lambda: 0.0)

    reporter(event(ProgressLevel.DETAIL, "Deep detail."))

    assert stream.getvalue() == "[ 0.0s] Deep detail.\n"


def test_default_run_shows_only_warnings(config_path, complete_result):
    status, stdout, stderr = read_with(config_path, complete_result, script=SCRIPT)

    assert status == 0
    assert "7.4 mmol/L" in stdout
    assert stderr == "warning: Something looked odd.\n"


def test_single_v_adds_info_lines(config_path, complete_result):
    _, _, stderr = read_with(config_path, complete_result, ["-v", *READ], script=SCRIPT)

    assert stderr == "Connecting to Meter 1...\nwarning: Something looked odd.\n"


def test_double_v_adds_detail_lines_with_elapsed_time(config_path, complete_result):
    _, _, stderr = read_with(config_path, complete_result, ["-vv", *READ], script=SCRIPT)

    lines = stderr.splitlines()
    assert re.fullmatch(r"\[ *\d+\.\ds\] Connecting to Meter 1\.\.\.", lines[0])
    assert re.fullmatch(
        r"\[ *\d+\.\ds\] Asking the meter how many records it holds\.", lines[1]
    )
    assert lines[2] == "warning: Something looked odd."


@pytest.mark.parametrize(
    ("argv", "level"),
    [
        (["-v", *READ], 1),
        ([*READ, "-v"], 1),
        (["read", "-v", "--device", "fake:meter-1"], 1),
        (["-v", *READ, "-v"], 2),
        (["-vvv", *READ], 2),
        (["read", "-vv", "--device", "fake:meter-1"], 2),
    ],
)
def test_verbosity_flags_are_accepted_in_either_position_and_summed(
    config_path, complete_result, argv, level
):
    _, _, stderr = read_with(config_path, complete_result, argv, script=SCRIPT)

    assert ("Connecting to Meter 1" in stderr) == (level >= 1)
    assert ("Asking the meter" in stderr) == (level >= 2)


def test_counter_events_are_throttled(config_path, complete_result):
    counter = tuple(
        event(ProgressLevel.INFO, f"Reading records: {n} of 57", n, 57)
        for n in range(1, 58)
    )

    _, _, stderr = read_with(config_path, complete_result, ["-v", *READ], script=counter)

    lines = stderr.splitlines()
    assert lines[0] == "Reading records: 1 of 57"
    assert lines[-1] == "Reading records: 57 of 57"
    assert len(lines) == 11


def test_verbosity_never_changes_stdout(config_path, complete_result):
    _, quiet, _ = read_with(config_path, complete_result, script=SCRIPT)
    _, chatty, _ = read_with(config_path, complete_result, ["-vv", *READ], script=SCRIPT)

    assert quiet == chatty


def test_manager_factory_receives_the_console_reporter(config_path, complete_result):
    received = []

    def factory(registry, progress):
        received.append(progress)
        return FakeManager(registry, devices=(make_device(),), result=complete_result)

    invoke(READ, config_path=config_path, manager_factory=factory)

    assert isinstance(received[0], ConsoleReporter)


@pytest.mark.parametrize(
    ("completion", "line"),
    [
        (
            CompletionStatus.PARTIAL,
            "warning: retrieval is partial: some records may be missing.\n",
        ),
        (
            CompletionStatus.UNKNOWN,
            "warning: retrieval is unknown: the meter cannot confirm that this is "
            "the full history.\n",
        ),
    ],
)
def test_incomplete_read_prints_one_outcome_line_and_status_five(
    config_path, complete_result, completion, line
):
    result = replace(complete_result, completion=completion)

    status, _, stderr = read_with(config_path, result, ["-vv", *READ])

    assert status == 5
    assert stderr == line


@pytest.mark.parametrize(
    ("error", "status", "hint"),
    [
        (DeviceNotFoundError("no meter"), 3, "Bluetooth is enabled"),
        (UnsupportedDeviceError("no driver"), 3, "bgmeter drivers list"),
        (MeterTimeoutError("slow"), 5, "phone app"),
        (MeterConnectionError("gone"), 4, "no other app is connected"),
        (DiscoveryError("scan failed"), 4, "no other app is connected"),
        (ProtocolError("garbled"), 5, "-vv --log-level debug"),
    ],
)
def test_failures_print_an_error_line_and_a_hint(
    config_path, complete_result, error, status, hint
):
    actual, stdout, stderr = read_with(
        config_path, complete_result, read_error=error
    )

    lines = stderr.splitlines()
    assert actual == status
    assert stdout == ""
    assert lines[0] == f"error: {error}"
    assert lines[1].startswith("hint: ")
    assert hint in lines[1]


def test_selection_errors_have_no_hint(config_path, complete_result):
    status, _, stderr = read_with(config_path, complete_result, ["read"])

    assert status == 2
    assert stderr.startswith("error: ")
    assert "hint:" not in stderr
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest cli/tests/test_reporting.py -q`
Expected: FAIL/ERROR (`ModuleNotFoundError: No module named 'bgmeter_cli.reporter'`).

- [ ] **Step 4: Create `cli/src/bgmeter_cli/reporter.py`**

```python
"""Plain-language progress output for the terminal."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TextIO

from bgmeter import ProgressEvent, ProgressLevel


_MAX_VERBOSITY = 2
_VERBOSITY_TO_SHOW = {
    ProgressLevel.WARNING: 0,
    ProgressLevel.INFO: 1,
    ProgressLevel.DETAIL: 2,
}


class ConsoleReporter:
    """Write progress events to a stream according to the ``-v`` level."""

    def __init__(
        self,
        stream: TextIO,
        verbosity: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream
        self._verbosity = max(0, min(verbosity, _MAX_VERBOSITY))
        self._clock = clock
        self._started = clock()
        self._last_counter: int | None = None

    def __call__(self, event: ProgressEvent) -> None:
        if self._verbosity < _VERBOSITY_TO_SHOW[event.level]:
            return
        if not self._counter_is_due(event):
            return
        if event.level is ProgressLevel.WARNING:
            line = f"warning: {event.message}"
        elif self._verbosity >= _MAX_VERBOSITY:
            line = f"[{self._clock() - self._started:4.1f}s] {event.message}"
        else:
            line = event.message
        self._stream.write(line + "\n")

    def _counter_is_due(self, event: ProgressEvent) -> bool:
        """Print the first and last update and each 10% step in between."""
        if event.current is None or event.total is None:
            return True
        last = self._last_counter
        due = (
            last is None
            or event.current >= event.total
            or event.current - last >= event.total / 10
        )
        if due:
            self._last_counter = event.current
        return due


__all__ = ["ConsoleReporter"]
```

- [ ] **Step 5: Edit `cli/src/bgmeter_cli/app.py`**

1. Add `ProgressCallback` to the `from bgmeter import (...)` list (alphabetical, between `MeterTimeoutError` and `ProtocolError`) and add below the `.registry` import block:

```python
from .reporter import ConsoleReporter
```

2. Replace the `ManagerFactory` definition:

```python
ManagerFactory = Callable[[object, ProgressCallback | None], object]
```

3. Add after `_timezone_name`:

```python
def _add_reporting_options(parser: argparse.ArgumentParser, suffix: str) -> None:
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        dest=f"verbose_{suffix}",
        help="explain progress in plain language (-v) with more detail (-vv)",
    )


def _verbosity(arguments) -> int:
    return min(2, arguments.verbose_top + arguments.verbose_sub)
```

4. In `_parser()`: directly after the `ArgumentParser(...)` call add `_add_reporting_options(parser, "top")`, and add `_add_reporting_options(<parser>, "sub")` right after each of the `devices`, `info`, `read`, and `drivers` subparsers is created (`devices = commands.add_parser(...)`, and so on).

5. Replace `_default_manager_factory`:

```python
def _default_manager_factory(registry, progress=None):
    return MeterManager(
        registry=registry, transports=(BleTransport(),), progress=progress
    )
```

6. `_run_meter_command`: add a `progress: ProgressCallback | None,` keyword parameter after `database_path`, and change `manager = manager_factory(registry)` to `manager = manager_factory(registry, progress)`. Replace the final non-complete block

```python
    if result.completion is not CompletionStatus.COMPLETE:
        stderr.write(f"retrieval is {result.completion.value}\n")
        return 5
    return 0
```

with

```python
    if result.completion is not CompletionStatus.COMPLETE:
        stderr.write(_OUTCOME_LINES[result.completion])
        return 5
    return 0
```

and define, above `_run_meter_command`:

```python
_OUTCOME_LINES = {
    CompletionStatus.PARTIAL: (
        "warning: retrieval is partial: some records may be missing.\n"
    ),
    CompletionStatus.UNKNOWN: (
        "warning: retrieval is unknown: the meter cannot confirm that this is "
        "the full history.\n"
    ),
}
```

7. `_dispatch`: add a keyword-only `progress,` parameter (after `manager_factory`) and pass `progress=progress,` in the `_run_meter_command(...)` call.

8. Add above `run`:

```python
_HINT_NOT_FOUND = (
    "Make sure the meter is on and close by, and that Bluetooth is enabled on "
    "this computer."
)
_HINT_UNSUPPORTED = (
    "None of the devices found is a supported meter. Run 'bgmeter drivers list' "
    "to see what is supported."
)
_HINT_TIMEOUT = (
    "The meter did not answer in time. Wake the meter, make sure it is not still "
    "connected to the phone app, and try again. Run with -vv to see what happened."
)
_HINT_CONNECTION = (
    "Check that Bluetooth is turned on and that no other app is connected to the "
    "meter."
)
_HINT_PROTOCOL = (
    "The meter sent data this program could not understand. Try again; if it keeps "
    "happening, run with -vv --log-level debug to see technical details."
)


def _classify(error: Exception) -> tuple[int, str | None] | None:
    """Map a handled failure to its exit status and optional hint."""
    if isinstance(
        error, (SelectionError, DriverSelectionError, ConfigError, AmbiguousDeviceError)
    ):
        return 2, None
    if isinstance(error, DeviceNotFoundError):
        return 3, _HINT_NOT_FOUND
    if isinstance(error, UnsupportedDeviceError):
        return 3, _HINT_UNSUPPORTED
    if isinstance(error, MeterTimeoutError):
        return 5, _HINT_TIMEOUT
    if isinstance(error, (DiscoveryError, MeterConnectionError)):
        return 4, _HINT_CONNECTION
    if isinstance(error, ProtocolError):
        return 5, _HINT_PROTOCOL
    if isinstance(error, ExportError):
        return 6, None
    return None
```

9. In `run`, replace everything from `try: return _dispatch(` to the end of the function (the whole `try/except` chain) with:

```python
    reporter = ConsoleReporter(stderr, _verbosity(arguments))
    try:
        return _dispatch(
            arguments,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            config_path=config_path,
            database_path=database_path,
            manager_factory=manager_factory or _default_manager_factory,
            progress=reporter,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        stderr.write("interrupted\n")
        return 130
    except Exception as error:
        classified = _classify(error)
        if classified is None:
            raise
        status, hint = classified
        stderr.write(f"error: {error}\n")
        if hint is not None:
            stderr.write(f"hint: {hint}\n")
        return status
```

- [ ] **Step 6: Run the touched suites**

Run: `python -m pytest cli/tests/test_reporting.py cli/tests/test_cli.py -q`
Expected: all PASS. (The two pre-existing assertions on `retrieval is partial` still hold: the new line begins `warning: retrieval is partial`.)

- [ ] **Step 7: Leave changes uncommitted** (user git rule).

---

### Task 7: CLI logging sinks and options

**Files:**
- Create: `cli/src/bgmeter_cli/logging_setup.py`
- Modify: `cli/src/bgmeter_cli/app.py`, `cli/src/bgmeter_cli/registry.py`
- Test: `cli/tests/test_reporting.py` (append; add `import logging`; replace the `read_with` helper)

**Interfaces:**
- Consumes: Task 6's `_add_reporting_options`, `_classify`, `run`, `ConsoleReporter`.
- Produces: `LOG_LEVELS = ("debug", "info", "warning", "error")`, `DEFAULT_LOG_LEVEL = "error"`, `LogFileError(OSError)`, `configured_logging(level: str, *, log_file: Path | None, stream: TextIO, command: str)` (context manager; attaches one handler to the root logger and always restores handlers and level); parser destinations `log_level_top`/`log_level_sub` and `log_file_top`/`log_file_sub`; `_log_settings(arguments) -> tuple[str, Path | None]`; `_execute(...)`; `_log_final_failure(command, error, status)`. Loggers `bgmeter_cli.app` and `bgmeter_cli.registry`. A log file that cannot be opened ends the command with `error: cannot open log file ...` and status 2 before any meter work.

- [ ] **Step 1: Update the test helper and write the failing tests**

In `cli/tests/test_reporting.py` add `import logging` to the stdlib imports, and replace `read_with` with this version (adds `manager_class`):

```python
def read_with(
    config_path,
    result,
    argv=READ,
    *,
    script=(),
    manager_class=ScriptedManager,
    **manager_kwargs,
):
    def factory(registry, progress):
        return manager_class(
            registry,
            progress,
            devices=(make_device(),),
            result=result,
            script=script,
            **manager_kwargs,
        )

    return invoke(argv, config_path=config_path, manager_factory=factory)
```

Append:

```python
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT")
ELAPSED = re.compile(r"^\[ *\d+\.\ds\] ")


class LoggingManager(ScriptedManager):
    """A fake manager whose layer logs at every level during a read."""

    async def read(self, device, options=None):
        log = logging.getLogger("bgmeter.fake")
        log.debug("wire detail 0x05")
        log.info("read started")
        log.error("absorbed cleanup failure")
        return await super().read(device, options)


def split_stderr(text):
    """Split stderr into (progress lines, log records without timestamps)."""
    progress, records = [], []
    for line in text.splitlines():
        if TIMESTAMP.match(line):
            records.append(line.split(" ", 1)[1])
        else:
            progress.append(ELAPSED.sub("", line))
    return progress, records


def test_default_terminal_log_level_is_error(config_path, complete_result):
    status, _, stderr = read_with(
        config_path, complete_result, manager_class=LoggingManager
    )

    _, records = split_stderr(stderr)
    assert status == 0
    assert records == ["ERROR bgmeter.fake: absorbed cleanup failure"]


def test_log_level_debug_writes_every_record_to_the_terminal(config_path, complete_result):
    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-level", "debug", *READ],
        manager_class=LoggingManager,
    )

    _, records = split_stderr(stderr)
    assert [record.split(":", 1)[0] for record in records if "bgmeter.fake" in record] == [
        "DEBUG bgmeter.fake",
        "INFO bgmeter.fake",
        "ERROR bgmeter.fake",
    ]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([*READ, "--log-level", "INFO"], ["INFO", "ERROR"]),
        (["--log-level", "debug", *READ, "--log-level", "error"], ["ERROR"]),
        (["--log-level", "info", *READ], ["INFO", "ERROR"]),
    ],
)
def test_log_level_is_case_insensitive_and_the_subcommand_position_wins(
    config_path, complete_result, argv, expected
):
    _, _, stderr = read_with(
        config_path, complete_result, argv, manager_class=LoggingManager
    )

    _, records = split_stderr(stderr)
    fake = [record for record in records if "bgmeter.fake" in record]
    assert [record.split(" ", 1)[0] for record in fake] == expected


def test_log_file_receives_records_exclusively_with_a_header(
    tmp_path, config_path, complete_result
):
    log_path = tmp_path / "bgmeter.log"

    status, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-file", str(log_path), "--log-level", "debug", *READ],
        manager_class=LoggingManager,
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert status == 0
    assert stderr == ""
    assert "bgmeter-cli" in lines[0] and "bgmeter-core" in lines[0]
    assert "command=read" in lines[0]
    fake_lines = [line for line in lines[1:] if "bgmeter.fake" in line]
    assert any("bgmeter_cli.app" in line for line in lines[1:])
    assert [line.split(" ", 1)[1] for line in fake_lines] == [
        "DEBUG bgmeter.fake: wire detail 0x05",
        "INFO bgmeter.fake: read started",
        "ERROR bgmeter.fake: absorbed cleanup failure",
    ]


def test_log_level_applies_to_the_log_file_and_defaults_to_error(
    tmp_path, config_path, complete_result
):
    log_path = tmp_path / "bgmeter.log"

    read_with(
        config_path,
        complete_result,
        ["--log-file", str(log_path), *READ],
        manager_class=LoggingManager,
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines[1:]] == [
        "ERROR bgmeter.fake: absorbed cleanup failure"
    ]


def test_progress_still_reaches_the_terminal_when_logging_goes_to_a_file(
    tmp_path, config_path, complete_result
):
    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["-v", "--log-file", str(tmp_path / "bgmeter.log"), *READ],
        script=SCRIPT,
        manager_class=LoggingManager,
    )

    assert stderr == "Connecting to Meter 1...\nwarning: Something looked odd.\n"


def test_final_failure_prints_user_lines_then_one_technical_log_record(
    config_path, complete_result
):
    status, _, stderr = read_with(
        config_path, complete_result, read_error=MeterTimeoutError("boom")
    )

    lines = stderr.splitlines()
    assert status == 5
    assert lines[0] == "error: boom"
    assert lines[1].startswith("hint: ")
    assert re.fullmatch(
        r"\S+ ERROR bgmeter_cli\.app: command read failed: MeterTimeoutError: boom "
        r"\(exit status 5\)",
        lines[2],
    )
    assert len(lines) == 3
    assert "Traceback" not in stderr


def test_final_failure_traceback_appears_only_at_debug(config_path, complete_result):
    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-level", "debug", *READ],
        read_error=MeterTimeoutError("boom"),
    )

    assert "Traceback (most recent call last)" in stderr


def test_final_failure_goes_to_the_log_file_not_the_terminal(
    tmp_path, config_path, complete_result
):
    log_path = tmp_path / "bgmeter.log"

    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-file", str(log_path), *READ],
        read_error=MeterTimeoutError("boom"),
    )

    assert "ERROR bgmeter_cli.app" not in stderr
    assert stderr.splitlines()[0] == "error: boom"
    assert "ERROR bgmeter_cli.app: command read failed: MeterTimeoutError: boom" in (
        log_path.read_text(encoding="utf-8")
    )


def test_unwritable_log_file_stops_before_any_meter_work(
    tmp_path, config_path, complete_result
):
    created = []

    def factory(registry, progress):
        created.append(registry)
        return FakeManager(registry, devices=(make_device(),), result=complete_result)

    status, stdout, stderr = invoke(
        ["--log-file", str(tmp_path / "missing" / "bgmeter.log"), *READ],
        config_path=config_path,
        manager_factory=factory,
    )

    assert status == 2
    assert stdout == ""
    assert stderr.startswith("error: cannot open log file ")
    assert created == []


def test_run_restores_the_root_logger(config_path, complete_result, tmp_path):
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level

    read_with(config_path, complete_result, ["--log-level", "debug", *READ])
    read_with(
        config_path,
        complete_result,
        ["--log-file", str(tmp_path / "bgmeter.log"), *READ],
    )

    assert root.handlers == handlers
    assert root.level == level


def test_progress_depends_only_on_v_and_logs_only_on_log_level(
    config_path, complete_result
):
    results = {}
    for verbosity in (0, 1, 2):
        for level in ("debug", "error"):
            argv = ["--log-level", level, *READ]
            if verbosity:
                argv = ["-" + "v" * verbosity, *argv]
            _, _, stderr = read_with(
                config_path,
                complete_result,
                argv,
                script=SCRIPT,
                manager_class=LoggingManager,
            )
            results[(verbosity, level)] = split_stderr(stderr)

    for verbosity in (0, 1, 2):
        assert results[(verbosity, "debug")][0] == results[(verbosity, "error")][0]
    for level in ("debug", "error"):
        assert results[(0, level)][1] == results[(1, level)][1] == results[(2, level)][1]
    assert results[(0, "debug")][0] != results[(2, "debug")][0]
    assert results[(2, "debug")][1] != results[(2, "error")][1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest cli/tests/test_reporting.py -q -k "log or independence or depends_only or final_failure or restores"`
Expected: FAIL (`error: unrecognized arguments: --log-level` -> statuses of 2; missing log output).

- [ ] **Step 3: Create `cli/src/bgmeter_cli/logging_setup.py`**

```python
"""Log sinks for the CLI: the terminal by default, or a file exclusively."""

from __future__ import annotations

import logging
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TextIO


LOG_LEVELS = ("debug", "info", "warning", "error")
DEFAULT_LOG_LEVEL = "error"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DISTRIBUTIONS = ("bgmeter-cli", "bgmeter-core", "bgmeter-microtech")


class LogFileError(OSError):
    """The requested log file cannot be opened."""


class _IsoFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return (
            datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds")
        )


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unknown"


def _header(command: str) -> str:
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    versions = " ".join(f"{name} {_version(name)}" for name in _DISTRIBUTIONS)
    return (
        f"{stamp} {versions} python {platform.python_version()} "
        f"platform {platform.platform()} command={command}"
    )


@contextmanager
def configured_logging(
    level: str,
    *,
    log_file: Path | None,
    stream: TextIO,
    command: str,
) -> Iterator[None]:
    """Attach one root-logger sink at ``level``; always restore the logger."""

    numeric = getattr(logging, level.upper())
    if log_file is not None:
        try:
            handler: logging.Handler = logging.FileHandler(
                log_file, mode="a", encoding="utf-8"
            )
        except OSError as error:
            raise LogFileError(f"cannot open log file {log_file}: {error}") from error
        handler.stream.write(_header(command) + "\n")
        handler.stream.flush()
    else:
        handler = logging.StreamHandler(stream)
    handler.setLevel(numeric)
    handler.setFormatter(_IsoFormatter(_FORMAT))

    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(numeric)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        handler.close()


__all__ = [
    "DEFAULT_LOG_LEVEL",
    "LOG_LEVELS",
    "LogFileError",
    "configured_logging",
]
```

- [ ] **Step 4: Edit `cli/src/bgmeter_cli/app.py`**

1. Add `import logging` to the stdlib imports, and below the `.registry` import block add:

```python
from .logging_setup import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVELS,
    LogFileError,
    configured_logging,
)
```

then, after all imports, `_log = logging.getLogger(__name__)`.

2. Extend `_add_reporting_options` (append inside the function, after the `-v` argument):

```python
    parser.add_argument(
        "--log-level",
        type=str.lower,
        choices=LOG_LEVELS,
        default=None,
        dest=f"log_level_{suffix}",
        help=f"minimum level of technical log records (default: {DEFAULT_LOG_LEVEL})",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        dest=f"log_file_{suffix}",
        help="write technical logs to this file instead of the terminal",
    )
```

3. Add after `_verbosity`:

```python
def _log_settings(arguments) -> tuple[str, Path | None]:
    level = arguments.log_level_sub or arguments.log_level_top or DEFAULT_LOG_LEVEL
    log_file = arguments.log_file_sub or arguments.log_file_top
    return level, log_file
```

4. Add CLI-layer log records:
   - In `_dispatch`, directly after `config = load_config(config_path)`: `_log.debug("registered drivers: %s", list(config.registered))`.
   - In `_run_meter_command`, after `_check_destinations(...)` and before `result = await manager.read(...)`: `_log.info("reading %s", device.selector)`; after the `result = await manager.read(...)` line:
     ```python
         _log.info(
             "read complete: device=%s completion=%s records=%d",
             device.selector,
             result.completion.value,
             len(result.records),
         )
     ```
   - Inside `if arguments.store:` before `MeasurementStore(...)`: `_log.info("storing %d record(s)", len(result.records))`.
   - Before `_publish_outputs(...)`: `_log.debug("publishing outputs: %s", [(o.format, str(o.path) if o.path else "stdout") for o in outputs])`.

5. Add above `run`:

```python
def _log_final_failure(command: str, error: Exception, status: int) -> None:
    _log.error(
        "command %s failed: %s: %s (exit status %d)",
        command,
        type(error).__name__,
        error,
        status,
    )
    _log.debug("command %s failure traceback", command, exc_info=error)


def _execute(
    arguments,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    config_path,
    database_path,
    manager_factory,
    progress,
) -> int:
    try:
        return _dispatch(
            arguments,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            config_path=config_path,
            database_path=database_path,
            manager_factory=manager_factory,
            progress=progress,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        stderr.write("interrupted\n")
        return 130
    except Exception as error:
        classified = _classify(error)
        if classified is None:
            raise
        status, hint = classified
        stderr.write(f"error: {error}\n")
        if hint is not None:
            stderr.write(f"hint: {hint}\n")
        _log_final_failure(arguments.command, error, status)
        return status
```

6. In `run`, replace the block starting at `reporter = ConsoleReporter(...)` (through the end of the function) with:

```python
    reporter = ConsoleReporter(stderr, _verbosity(arguments))
    level, log_file = _log_settings(arguments)
    try:
        with configured_logging(
            level, log_file=log_file, stream=stderr, command=arguments.command
        ):
            return _execute(
                arguments,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                config_path=config_path,
                database_path=database_path,
                manager_factory=manager_factory or _default_manager_factory,
                progress=reporter,
            )
    except LogFileError as error:
        stderr.write(f"error: {error}\n")
        return 2
```

- [ ] **Step 5: Log absorbed driver-load failures in `cli/src/bgmeter_cli/registry.py`**

Add `import logging` to the stdlib imports and `_log = logging.getLogger(__name__)` after the imports. In `load_registered_registry`:
- in the `if not matches:` branch, before `plugins.append(`: `_log.error("registered driver entry point %r is not installed", name)`
- in the `if len(matches) != 1:` branch, before `plugins.append(`: `_log.error("multiple installed entry points are named %r", name)`
- in `except Exception as error:` (around `registry.register_entry_point(name)`), before `plugins.append(`:
  ```python
            _log.error(
                "driver entry point %r failed to load: %s: %s",
                name,
                type(error).__name__,
                error,
            )
            _log.debug(
                "driver entry point %r load failure traceback", name, exc_info=True
            )
  ```

(`installed_plugins` deliberately logs nothing: it repeats the same probes for catalog output and would duplicate these records.)

- [ ] **Step 6: Run the touched suites**

Run: `python -m pytest cli/tests -q`
Expected: all PASS. This runs the whole CLI suite (test_cli, test_store, test_reporting, and the launcher test); it is small and it is the only way to confirm the `run()` restructure kept every existing behavior. The launcher test spawns `gocheck.py`; if it fails on timing (3 s limit) rerun once before investigating.

- [ ] **Step 7: Leave changes uncommitted** (user git rule).

---

### Task 8: Documentation and final verification

**Files:**
- Modify: `cli/README.md`, `core/README.md`, `drivers/microtech/README.md`, `README.md`, `docs/DEVELOPMENT.md`, `ARCHITECTURE.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: everything from Tasks 1-7 (flag names, event wording, logger names, level rules).
- Produces: user and developer documentation; a verified full suite.

Documentation only: do not run tests between the doc edits (user rule). The one full-suite run is Step 8.

- [ ] **Step 1: `cli/README.md`** -- append at the end:

````markdown

## Progress and logging

Two independent options control what the program tells you.

**Progress (`-v`, `-vv`)** explains, in plain language, what the program is
doing. It is accepted before or after the subcommand and can be repeated;
`-vvv` is the same as `-vv`.

```console
$ bgmeter -v read --device ble:AA:BB:CC:DD:EE:FF
Connecting to GoChek Connect...
Reading records: 1 of 57
Reading records: 7 of 57
...
Reading records: 57 of 57
Read 57 records. All records were received.
```

`-vv` adds the reasoning behind each step (for example which nearby devices
were skipped and which replies from the meter were ignored) and prefixes each
line with the elapsed time. Without either flag only warnings and errors are
shown, and every error ends with a `hint:` line saying what to try next.

**Logging (`--log-level`, `--log-file`)** is for technical detail when reporting
or diagnosing a problem. `--log-level {debug,info,warning,error}` (default
`error`) sets the minimum level of the log records written to the terminal
(stderr). `--log-file PATH` sends the records to that file instead, and only
there; the same `--log-level` applies. To capture everything:

```console
bgmeter --log-file bgmeter.log --log-level debug read --device ble:AA:BB:CC:DD:EE:FF
```

Progress and logging never affect each other. A failed command prints the
`error:` and `hint:` lines for you and also logs one technical line. Debug logs
include raw meter bytes, which contain glucose values: read a log file before
attaching it to an issue. INFO and above never contain glucose values or serial
numbers. All of this output goes to stderr or the log file; stdout is unchanged.
````

- [ ] **Step 2: `core/README.md`** -- append at the end:

````markdown

## Progress reporting and logging

`MeterManager` accepts an optional `progress` callback that receives plain
language `ProgressEvent` objects (`level` of `INFO`, `DETAIL` or `WARNING`, a
one-sentence `message`, and optional `current`/`total` counters):

```python
from bgmeter import MeterManager, ProgressEvent

def show(event: ProgressEvent) -> None:
    print(event.message)

manager = MeterManager.default(progress=show)
```

The manager passes the same callback to drivers through
`ReadOptions.progress` (a caller's explicit value wins). A driver reports with
`emit_progress(options.progress, ProgressEvent(...))`, which ignores a missing
callback and never lets a failing callback break a read. Drivers that ignore the
field keep working; `DRIVER_API_VERSION` is unchanged. Write messages in plain
language, without protocol or Bluetooth jargon.

Every layer also logs through the standard `logging` module (`bgmeter`,
`bgmeter.manager`, `bgmeter.drivers`, `bgmeter.transports.bleak`). The package
installs only a `NullHandler`; configure handlers in your application. ERROR
means a failure the layer absorbed instead of raising, WARNING a recoverable
problem, INFO a milestone (never glucose values or serial numbers), and DEBUG
byte-level detail. Progress events and log records are independent and carry
different information.
````

- [ ] **Step 3: `drivers/microtech/README.md`** -- insert before `## Protocol and capture boundary`:

````markdown
## Progress and logging

The driver reports plain-language progress through `ReadOptions.progress`
("Reading records: 12 of 57", retries, and, at detail level, replies it ignored
and why). It logs technical detail under `bgmeter_microtech.protocol`,
`.collector`, and `.driver`: DEBUG has every request, every notification as hex,
and the accept or reject verdict for each reply; INFO has counts only, never
glucose values or serial numbers; WARNING summarizes exhausted retries and
rejected replies. The package installs only a `NullHandler`. When a read returns
no usable record, the `bgmeter_microtech.protocol` log (`--log-level warning`
or lower on the CLI) shows what the meter sent and why each reply was rejected.

````

- [ ] **Step 4: `README.md`** -- in `## Develop and debug`, after the "Useful CLI diagnostics" code block, insert:

````markdown

When a read misbehaves, ask the program to explain itself. `-vv` narrates each
step in plain language, and a debug log captures the technical evidence:

```powershell
bgmeter -vv read --device ble:AA-BB-CC-DD-EE-FF --log-file bgmeter.log --log-level debug
```

Debug logs contain raw meter bytes (including glucose values); review them
before sharing. See [cli/README.md](cli/README.md) for the full option list.
````

- [ ] **Step 4b: `docs/DEVELOPMENT.md`**

Replace the `read` line of the command block under `## CLI commands and outputs` so it reads:

```text
bgmeter [-v|-vv] [--log-level LEVEL] [--log-file PATH] <command> ...
bgmeter read [--device DEVICE] [--driver DRIVER] [--timezone ZONE]
             [--output OUTPUT]... [--show-raw] [--store] [--force]
```

(keep the other command lines), and insert this section before `## Meter time semantics`:

````markdown
## Verbosity and logging

The CLI has two independent mechanisms, described in
[ARCHITECTURE.md](../ARCHITECTURE.md#progress-reporting-and-logging):

- `-v` / `-vv` show plain-language progress from `ProgressEvent`s (default: only
  warnings and errors; every error also prints a `hint:` line).
- `--log-level {debug,info,warning,error}` (default `error`) filters the stdlib
  log records written to stderr; `--log-file PATH` writes them to the file
  instead and to nothing else.

To diagnose a failed read on hardware:

```powershell
bgmeter -vv read --device <selector> --log-file bgmeter.log --log-level debug
```

`-vv` shows which replies from the meter were ignored and why; `bgmeter.log`
holds every request, every notification as hex, and the verdict for each reply.
Debug logs contain glucose values as raw bytes, so review them before attaching
them to an issue. Tests assert log records with `caplog`; new log calls must
follow the level rules in ARCHITECTURE.md (a layer never logs an exception it
re-raises, and INFO never carries glucose values or serial numbers).
````

- [ ] **Step 5: `ARCHITECTURE.md`** -- insert before `## Failure and completeness model`:

````markdown
## Progress reporting and logging

Two independent mechanisms; neither reads or filters the other.

**Progress** is user-facing. Core defines `ProgressEvent(level, message,
current, total)`, `ProgressLevel` (`INFO`, `DETAIL`, `WARNING`), `ProgressCallback`,
and `emit_progress`. `MeterManager` takes an optional callback and hands it to
drivers as `ReadOptions.progress` (an explicit caller value wins), so the
`MeterDriver` protocol and `DRIVER_API_VERSION` are unchanged. Messages are plain
ASCII sentences without protocol jargon. Within user-facing output a condition is
reported by one line: a command-ending failure by the CLI's `error:`/`hint:`
lines and a non-complete read by its single `retrieval is ...` line, so
managers and drivers emit no progress event restating either. The CLI's
`ConsoleReporter` shows `WARNING` always, `INFO` with `-v`, and `DETAIL` with
`-vv` (with elapsed time), throttles counters, and writes to stderr.

**Logging** is developer-facing stdlib `logging` with `getLogger(__name__)` in
every module; `bgmeter` and `bgmeter_microtech` install only a `NullHandler`.
ERROR: a failure the layer absorbs instead of raising. WARNING: a recoverable
problem. INFO: milestones, never glucose values or serial numbers. DEBUG:
byte-level detail (GATT reads/writes, requests, notifications as hex, per-reply
verdicts). A layer never logs an exception it re-raises; the CLI logs the final
failure once (an ERROR line plus a DEBUG traceback). Log text is technical and
never restates a progress sentence. The CLI attaches one sink to the root logger:
the terminal by default, or the `--log-file` exclusively, at `--log-level`
(default `error`), and restores the logger afterwards.
````

- [ ] **Step 6: `CHANGELOG.md`** -- insert above `## [0.1.0] - 2026-09-19`:

```markdown
## [Unreleased]

### Added

- Plain-language progress output with `-v` and `-vv`, and `hint:` lines on errors.
- `ProgressEvent`, `ProgressLevel`, `ProgressCallback`, `emit_progress`,
  `ReadOptions.progress`, and a `progress` argument on `MeterManager`.
- Standard-library logging in the core, MicroTech driver, and CLI, controlled by
  `--log-level` (default `error`) and `--log-file`.

### Changed

- The MicroTech "no usable records" error message is now plain language, and the
  CLI reports an incomplete read with an explanatory `retrieval is ...` line.
```

- [ ] **Step 7: Confirm the docs match the code**

Run: `python -m bgmeter_cli --help` and `python -m bgmeter_cli read --help`
Expected: both list `-v/--verbose`, `--log-level {debug,info,warning,error}` and `--log-file LOG_FILE`; the wording in the READMEs matches these flag names exactly. Fix any doc that differs.

- [ ] **Step 8: Full verification (the only full run)**

Run: `python -m pytest -q`
Expected: every test PASSES; the hardware test is skipped. **Report every failing test name from the complete output** (do not truncate or `tail` it). If anything fails, fix it in the task that owns the code, then rerun only that file.

Then a real-process smoke test of both sinks (no meter needed; this exercises the driver-management path):

```powershell
python -m bgmeter_cli drivers list --registered -vv --log-level debug
python -m bgmeter_cli drivers list --registered --log-file "$env:TEMP\bgmeter-smoke.log" --log-level debug
Get-Content "$env:TEMP\bgmeter-smoke.log" -TotalCount 5
Remove-Item "$env:TEMP\bgmeter-smoke.log"
```

Expected: the first command prints the `microtech` row on stdout and timestamped DEBUG records on stderr; the second prints only the row and creates the log file whose first line is the header (`bgmeter-cli ... command=drivers`) followed by DEBUG records.

- [ ] **Step 9: Leave everything uncommitted and report**

Do not commit. Report: all tasks done, full-suite result, and that the next step is diagnosing the original device failure with the new tooling (it is out of scope for this plan):

```powershell
bgmeter -vv read --device <selector> --log-file bgmeter.log --log-level debug
```

The `-vv` output shows, in plain words, which replies from the meter were ignored; `bgmeter.log` shows each verdict (`rejected_unmatched_token`, `rejected_wrong_event`, ...) with the raw bytes.
