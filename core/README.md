# bgmeter-core

`bgmeter-core` is the protocol-neutral async library for discovering blood
glucose meters and reading normalized records through independently packaged
drivers. It provides immutable public models, typed errors, transport
capabilities, an isolated driver registry, a Bleak adapter, and lifecycle-safe
meter orchestration.

The package requires Python 3.13 or newer. Install the published package with:

```console
python -m pip install bgmeter-core
```

For development from this repository, install the test extra instead:

```console
python -m pip install -e "core[test]"
```

## Async discovery and one-shot reads

The default manager loads compatible installed `bgmeter.drivers` entry points
and uses the BLE transport. At least one independently installed driver package
must support a discovered endpoint.

```python
import asyncio

from bgmeter import ReadOptions, discover_meters, read_meter


async def main() -> None:
    devices = await discover_meters(timeout=5.0)
    for device in devices:
        print(device.selector, device.identity.manufacturer, device.identity.model)

    result = await read_meter(
        devices[0],
        ReadOptions(timezone="Europe/Stockholm", request_timeout=5.0),
    )
    for record in result.records:
        print(record.measured_at.measured_at_local, record.mmol_l)


asyncio.run(main())
```

For several operations on one connection, use the manager context directly.
It attempts to close the transport session on success, failure, and cancellation.
If a body or probe raises, that same exception (including `CancelledError`)
remains primary; a cleanup failure is attached as an exception note. With no
pending failure, disconnect errors become `MeterConnectionError` with the
original error as their cause. Cancellation during cleanup propagates.

One-shot `MeterManager.read()` / `read_meter()` retains valid records if
disconnect fails: it returns `PARTIAL`, appends a warning, and sets
`termination_reason="disconnect_failed"`. The `bgmeter.cleanup` diagnostics
entry records the cleanup error type/message and the prior completion,
termination reason, and any previous diagnostic at that key. Other diagnostics,
records, and counts are preserved; `ended_at` includes cleanup. An empty result
followed by disconnect failure raises a typed connection error. The connection
context itself cannot replace results held by its caller and still raises on a
cleanup-only failure.

```python
from bgmeter import MeterManager

manager = MeterManager.default()
devices = await manager.discover()
async with manager.open(devices[0]) as meter:
    result = await meter.read_records()
```

## Explicit isolated composition

Constructing a manager explicitly never loads installed entry points. This is
useful for applications that select drivers themselves and for deterministic
tests.

```python
from bgmeter import BleTransport, DriverRegistry, MeterManager
from example_meter import create_driver

registry = DriverRegistry()
registry.register(create_driver, source="application")
manager = MeterManager(registry=registry, transports=(BleTransport(),))
devices = await manager.discover()
```

## Complete external driver example

Drivers need only the top-level `bgmeter` API. Candidate matching is
side-effect-free; connected probing and reading receive a session owned by the
manager.

```python
from datetime import UTC, datetime

from bgmeter import (
    DRIVER_API_VERSION,
    CompletionStatus,
    DriverMatch,
    GattSession,
    MeterDevice,
    MeterIdentity,
    ReadOptions,
    ReadResult,
    TransportEndpoint,
    TransportSession,
    UnsupportedDeviceError,
)


class ExampleDriver:
    driver_id = "example"
    display_name = "Example meter"
    api_version = DRIVER_API_VERSION
    supported_transports = frozenset({"ble"})
    known_meter_identities = ("Example meter",)

    def match_candidate(self, endpoint: TransportEndpoint) -> DriverMatch | None:
        if endpoint.transport == "ble" and endpoint.name == "Example meter":
            return DriverMatch(confidence=80, evidence=("advertised name",))
        return None

    async def probe(self, session: TransportSession) -> MeterIdentity:
        if not isinstance(session, GattSession):
            raise UnsupportedDeviceError("the example driver requires GATT")
        if not any(
            service.uuid == "example-service"
            and any(char.uuid == "model-characteristic" for char in service.characteristics)
            for service in session.services
        ):
            raise UnsupportedDeviceError("required connected service relationship absent")
        model = (await session.read_gatt_char("model-characteristic")).decode()
        return MeterIdentity(manufacturer="Example Corp", model=model)

    async def read_records(
        self,
        session: TransportSession,
        device: MeterDevice,
        options: ReadOptions,
    ) -> ReadResult:
        if not isinstance(session, GattSession):
            raise UnsupportedDeviceError("the example driver requires GATT")
        started = datetime.now(UTC)
        payload = await session.read_gatt_char("records-characteristic")
        # A real driver validates and converts payload into GlucoseRecord values.
        return ReadResult(
            device=device,
            records=(),
            completion=CompletionStatus.COMPLETE,
            started_at=started,
            ended_at=datetime.now(UTC),
            received_count=0,
            diagnostics={"payload_size": len(payload)},
        )


def create_driver() -> ExampleDriver:
    return ExampleDriver()
```

Expose the zero-argument factory from the driver's own `pyproject.toml`:

```toml
[project.entry-points."bgmeter.drivers"]
example = "example_meter:create_driver"
```

The registry validates the driver API version and async contract before the
manager uses it. Meter protocol knowledge remains in the external driver;
transports discover endpoints, expose connected services, and transfer bytes.

## Connected GATT inventory

`GattSession.services` is a read-only tuple of public `GattService` values.
Each service contains its lowercase `uuid`, integer `handle`, and a tuple of
`GattCharacteristic` values with lowercase `uuid`, integer `handle`, and frozen
string `properties` such as `read`, `write`, or `notify`. Nesting preserves the
service/characteristic relationship, and handles distinguish repeated UUID
instances. UUID spelling is otherwise retained (Bleak supplies full UUIDs);
drivers should compare against the representation supplied by their transport.
The example above uses placeholder UUID names for its hypothetical protocol.

Bleak snapshots the connected client's service inventory during connection,
after discovery completes. Advertisement `service_uuids` are only candidate
hints. No backend service or characteristic objects escape into the inventory,
and later backend mutations cannot change it. Reconnect to obtain a fresh
snapshot. Inventory acquisition failure closes the client and raises a typed
connection error.

`GattService`, `GattCharacteristic`, `GattSession`, `TransportSession`,
`MeterTransport`, and `NotificationCallback` are available from top-level
`bgmeter`. A notification callback accepts `(sender: object, data: bytes)` and
may return `None` or an awaitable; the sender is opaque and transport-specific.

During discovery, an authoritative probe's `UnsupportedDeviceError` rejects
only that candidate; its session is closed and discovery continues. If all
candidates are rejected, discovery raises `UnsupportedDeviceError`. Other
probe, transport, and connection failures propagate. Once a candidate rejection
has been handled, a failure closing that rejected connection is a cleanup-only
connection error and is not silently ignored.

## Recursive metadata contract

`MetadataValue` is exported for driver annotations. Endpoint and identity
`metadata`, record `driver_data`, and result `diagnostics` accept string-keyed
mappings. Nested mappings may use string or integer keys (for example BLE
manufacturer IDs). Values may recursively contain mappings, lists, tuples,
sets, and frozensets. Construction snapshots mappings into read-only mapping
proxies, lists/tuples into tuples, and sets/frozensets into frozensets.
`bytearray` and `memoryview` values become independent `bytes` snapshots.
Set elements must remain hashable after freezing.

Supported scalar types are exactly `None`, `bool`, `int`, `float`, `str`, `bytes`,
`Decimal`, `datetime`, `date`, `time`, `timedelta`, and `UUID`. These retain their
types and values. Arbitrary objects and scalar subclasses are rejected with
`TypeError`; cyclic containers raise `ValueError`. Repeated references to an
acyclic child are allowed. Datetime/time timezone objects should themselves be
immutable (for example `ZoneInfo` or `datetime.timezone`).

The same snapshot rule protects record flags; their declared values remain
booleans. `RawCapture` snapshots every byte buffer, including each fragment.
Typed sequences elsewhere become tuples/frozensets and contain their declared
immutable element types. `TransportEndpoint.handle` is the explicit exception:
it is an opaque backend-owned connection token, excluded from comparison and
representation, and is not normalized metadata. No public freeze helper is
needed: public model constructors perform the snapshots.

## Bulk plugin diagnostics

`register_available_entry_points()` still returns `(descriptors, errors)` with
an error string keyed by entry-point name when that name occurs only once.
For duplicate installed names, error keys are JSON arrays encoded as strings:
`[name, package_name, package_version, entry_point_value]`. The identity is stable
across enumeration order for distinct plugins and preserves package provenance
for diagnostics. Indistinguishable repeated entries get occurrence suffixes
`#2`, `#3`, etc.; collisions with ordinary single-name keys are also avoided.
Named registration continues to reject ambiguous names.
