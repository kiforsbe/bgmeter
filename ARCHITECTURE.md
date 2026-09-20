# Implemented Architecture

Status: implemented and independently packaged in this repository. The root
`gocheck.py` is now only a deprecated compatibility launcher for the modular
CLI. The approved implementation specification is
[`docs/superpowers/specs/2026-09-17-bgmeter-modular-architecture-design.md`](docs/superpowers/specs/2026-09-17-bgmeter-modular-architecture-design.md).

## Goals

- Provide a transport-neutral, asynchronous Python library for discovering
  compatible blood glucose meters and reading their stored measurements.
- Keep each meter protocol behind a common driver interface.
- Publish that driver interface as part of the stable core API so independent
  packages can add meter support without modifying the core or CLI.
- Discover meters without assuming that the connected device is a particular
  GoChek or Wellion unit, and allow callers to select a specific discovered
  device.
- Preserve raw protocol evidence alongside normalized, human-readable records
  so captures can be reinterpreted as protocol knowledge improves.
- Keep the command-line interface and output formatting separate from device
  communication.

## Protocol boundary

Bluetooth Low Energy, GATT, and the Device Information Service are generic
standards. The FFE0/FFE1 service, framing, command `0x05`, indexed history
requests, checksums, fragmentation, and 18-byte history records documented for
the current meter are a proprietary MicroTech protocol. They may be shared by
other MicroTech OEM/rebranded products, but they are not a general blood glucose
meter protocol.

The device-specific implementation is
`MicroTechBgmDriver`. GoChek and Wellion names identify known compatible product
variants; they do not define the generic library interface. A future meter that
implements the Bluetooth SIG Glucose Service would use a separate driver, such
as `StandardBleGlucoseDriver`.

## Components

```text
bgmeter-cli source base
    +-- CLI commands and device selection
    +-- terminal / CSV / JSON exporters
    +-- MeasurementStore: normalized SQLite persistence
    |
    | public package dependency only
    v
bgmeter-core source base
    +-- MeterManager async API
    |     +-- discover()
    |     +-- open(device) -> ConnectedMeter.read_records()
    |     +-- read(device)
    +-- One-shot discover_meters() and read_meter() helpers
    +-- Public driver interface and registry
    +-- Transports
    |     +-- BleTransport
    +-- Normalized models
          +-- MeterDevice
          +-- GlucoseRecord
          +-- ReadResult
          +-- RawCapture
    ^
    | public driver interface and registration contract
    |
bgmeter-microtech source base
    +-- MicroTechBgmDriver
    +-- FFE0/FFE1 protocol, framing, parsing, and collection

future independent driver package
    +-- OtherMeterDriver
```

### High-level library API

The public library API coordinates discovery, selection, connection, and record
retrieval. The API is async-only. Applications that need a
synchronous interface can provide their own event-loop boundary; the CLI hides
async operation from terminal users.

The accepted API shape is centered on `MeterManager`. A caller discovers
`MeterDevice` objects through the manager and opens a selected device as an
async context manager. The resulting connection-scoped meter object provides
device information and record retrieval while guaranteeing transport cleanup.
Top-level `discover_meters()` and `read_meter()` convenience functions provide
the same behavior for simple one-shot use.

### Meter drivers

`MeterDriver` is the transport-neutral interface implemented by every supported
meter family. A driver identifies compatible discovery candidates, reads device
metadata, and retrieves records through an injected transport session.

`MeterDriver` and every type required to implement it are part of the stable,
documented core API. Implementations must not import private core modules. Each
driver declares a driver API version, stable driver ID, and supported transport
capabilities. The registry validates compatibility before using a driver.

Driver identification has two stages. A synchronous, side-effect-free
`match_candidate()` examines discovery metadata and returns match evidence. An
async `probe()` may then use an opened transport session to confirm identity
from services or device information. A driver also exposes a stable driver ID
and the transport capabilities it supports. Its record-read operation accepts
read options and returns a normalized result.

`MicroTechBgmDriver` owns all knowledge specific to the current protocol:

- FFE0/FFE1 service and characteristic recognition;
- MicroTech frame encoding, escaping, checksums, and fragmentation;
- command `0x05` history requests and completion detection;
- 18-byte BGM history record decoding;
- duplicate suppression and raw protocol capture.

Protocol codecs and packet collectors remain private implementation details of
the driver rather than requirements imposed on unrelated drivers.

### Transports

A transport adapter owns physical discovery and byte transfer. `BleTransport`
scans, connects, enumerates GATT services, subscribes to notifications, and
reads/writes characteristics. It does not interpret glucose measurements or
MicroTech packets. This composition allows future drivers to reuse BLE without
inheriting MicroTech behavior and permits future non-BLE transports.

### Driver registry and device selection

The registry accepts explicitly supplied driver instances and discovers
installed driver packages through a documented Python package entry-point
group. Each registered driver can probe a transport discovery candidate and
return a match result with identity and confidence information. Entry points
return a driver factory; the registry validates its driver API version and
reports incompatible packages without attempting to use them.

The public `DriverRegistry` supports registering and unregistering factories,
looking up a driver by stable ID, and enumerating driver metadata. Registration
is independent of device discovery: applications can construct an isolated
registry for tests or embed additional drivers without modifying global state.

The MicroTech implementation registers through this same public mechanism. It
is not imported or special-cased by the core. A future driver package can depend
only on the published core package, implement `MeterDriver`, register its
factory, and become available to both applications and the CLI.

The CLI maintains a persistent enabled-driver registry in its user
configuration. It stores installed package entry-point identities, not copied
code or filesystem paths. A third-party driver must first be installed through
normal Python/package-management tooling; the CLI does not download or install
arbitrary executable packages.

The accepted driver-management commands are:

```text
bgmeter drivers list [--available|--registered]
bgmeter drivers info DRIVER
bgmeter drivers register DRIVER
bgmeter drivers unregister DRIVER
```

`register` resolves an installed entry point, validates the driver API version
and stable ID, and then persists it. `unregister` only disables the driver for
this CLI and does not uninstall its package. `list` and `info` can query the
saved registration later and report package version, driver API version,
supported transports, known meter identities, and compatibility errors.
Device discovery and reads query all registered drivers unless `--driver`
restricts the operation. The bundled MicroTech entry point is registered in the
CLI's initial default configuration but remains governed by the same registry.

Discovery returns selectable `MeterDevice` descriptions rather than silently
choosing the first BLE peripheral. The CLI prompts when multiple supported
meters are found in an interactive terminal. Non-interactive use must specify a
stable device selector with `--device`.

If multiple drivers claim the same endpoint with equal confidence, the manager
reports an ambiguous match rather than choosing silently. Drivers do not open
transports themselves: `MeterManager` controls connection lifecycle and injects
an appropriate session into the selected driver.

### Normalized data and provenance

Drivers return vendor-neutral `GlucoseRecord` objects collected in a
`ReadResult`. Normalized data includes the glucose value in mmol/L, measurement
time, available status/context flags, and source-device identity. Optional or
unknown fields remain explicit instead of being guessed.

The MicroTech meter stores only a manually maintained local wall-clock value;
it does not provide a timezone or evidence of clock synchronization. For now,
the library and CLI assume that this clock was set correctly. Each normalized
record exposes:

- `meter_datetime`: the timezone-unspecified date and time decoded verbatim
  from the record;
- `measured_at_local`: that value interpreted in the selected local timezone;
- `measured_at_utc`: the corresponding UTC date and time; and
- the timezone name and UTC offset used for the interpretation.

The library accepts a local timezone for the read operation and defaults to the
host system timezone. The CLI follows the same default and provides an
explicit timezone option for reproducible conversion. The original
timezone-unspecified value is always retained so a record can be reinterpreted
if the meter clock or timezone assumption later proves incorrect.

Every record also retains `RawCapture` provenance, including the native record
bytes and relevant response/fragments. Driver-specific decoded fields can be
retained as namespaced metadata. `ReadResult` carries completeness state,
warnings, device information, and protocol diagnostics so partial retrieval is
not mistaken for a complete history.

The accepted normalized record model contains:

- a stable, driver-scoped record ID and an optional native sequence/index;
- the glucose value represented precisely in mmol/L;
- the native numeric value and unit reported by the device;
- the three timestamp representations and their timezone interpretation
  described above;
- available meal, context, and status flags;
- source device and driver identity;
- namespaced driver-specific decoded metadata; and
- raw request, notification fragments, reconstructed response, and native
  record bytes when available.

The accepted `ReadResult` model contains the normalized records, device
metadata, retrieval-completeness state, warnings, collection start/end times,
and protocol diagnostics. Records are unique within a result and presented in
measurement-time order even when the device supplies them in another order.

### CLI and exporters

`bgmeter` is a frontend over the public library, not the home of protocol
logic. Its operations cover device listing, device information,
and record retrieval.

The accepted meter command surface is:

```text
bgmeter devices [--driver DRIVER]
bgmeter info --device DEVICE [--driver DRIVER]
bgmeter read [--device DEVICE] [--driver DRIVER] [--timezone ZONE] [--output OUTPUT]...
```

Driver registration and inspection use the `bgmeter drivers` command group
described under driver registry and device selection.

Terminal output is the default. Repeated outputs such as `terminal`,
`csv=readings.csv`, and `json=readings.json` allow a single retrieval to feed
multiple exporters. At most one output may target stdout. Existing files are
protected unless `--force` is supplied. Progress and diagnostics use stderr so
machine-readable stdout remains clean.

Interactive use prompts when multiple compatible meters are available.
Non-interactive use requires `--device`, using a stable opaque selector shown by
`bgmeter devices`. Interactive selectors may accept an unambiguous prefix.

Terminal records show mmol/L plus local and UTC times, with `--show-raw` for
protocol evidence. CSV uses stable columns and represents retained raw data as
hexadecimal or JSON where needed. Versioned JSON is the lossless representation
of the complete normalized result. A partial retrieval still exports the
records obtained, marks the result incomplete, and returns a nonzero status.

After a successful meter read and before any output is rendered or published,
the CLI-private `MeasurementStore` persists each normalized record to its
per-user SQLite database. The store owns its database path, schema-version
check, connection lifecycle, and write transaction; the CLI only orchestrates
it. It stores normalized measurement/provenance/time columns and relational
boolean flags, deduplicating solely by public driver-scoped `record_id`. Raw
captures, driver metadata, and read diagnostics remain in JSON export rather
than the database. Storage also occurs for valid records from a partial result;
failure prevents publication and maps to CLI status `6`.

Exporters belong to the CLI source base. They consume the library's public
normalized `ReadResult` data and do not depend on a specific driver or import
library internals. The first release provides human-readable terminal
output, CSV, and a versioned generic JSON format. Multiple outputs may be
requested for one retrieval. Healthcare-specific formats such as FHIR are
deferred, but the CLI exporter interface leaves room for them without changing
the library or drivers.

### Export contracts

The accepted canonical JSON document uses schema name `bgmeter.read-result`
and integer `schema_version` 1. It contains generation metadata, full completion
statistics, device identity, normalized records, warnings, and diagnostics.
Each record includes its IDs, native and mmol/L glucose values, all timestamp
representations, flags, source identity, driver-specific metadata, and raw
request/fragments/response/record bytes encoded as hexadecimal. Decimal values
are JSON strings to avoid floating-point alteration; the native numeric value
and raw bytes provide additional provenance.

The accepted CSV contract has one record per row and stable columns for record
ID, native sequence, glucose values/units, meter/local/UTC times, timezone and
offset, flags, device and driver IDs, each raw byte representation, and driver
metadata. Nested values use compact JSON in a CSV cell. Terminal presentation
is deliberately human-oriented and is not a stable machine-readable schema.

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

## Failure and completeness model

The accepted `ReadResult.completion` value has three states:

- `COMPLETE`: the driver can positively establish that it received the full
  available history;
- `PARTIAL`: retrieval ended early or the driver knows records are missing; and
- `UNKNOWN`: valid records were retrieved but the protocol cannot prove that
  the history is complete.

The result records expected, received, duplicate, rejected, and retry counts
when known, plus a structured termination reason. Duplicate events contribute
to diagnostics but do not appear more than once in normalized records.

A failure before any trustworthy result raises a typed exception. Corrupt or
malformed individual responses are retried when safe and retained as
diagnostics. If valid records exist before a later failure, the library returns
them as a partial result rather than discarding them. Cancellation propagates
after transport cleanup.

The library exception families are discovery, missing device, ambiguous device,
unsupported device, connection, timeout, protocol integrity, and malformed
response errors under `MeterError`. The CLI owns exporter failures through
`ExportError`.

The accepted CLI status codes are `0` for complete success, `2` for usage or
ambiguous selection, `3` for no supported meter, `4` for transport/connection
failure, `5` for protocol failure or non-complete retrieval, `6` for export
failure, and `130` for interruption.

## Repository and package boundaries

The generic core, MicroTech driver, and CLI are separate Python source bases
colocated in this repository for now. Each is independently buildable,
installable, versioned, and testable so any directory can later move to its own
repository without restructuring Python packages or tests.

```text
core/
    pyproject.toml
    README.md
    src/
        bgmeter/
            __init__.py
            drivers.py
            manager.py
            models.py
            errors.py
            time.py
            transports/
                base.py
                bleak.py
    tests/

drivers/
    microtech/
        pyproject.toml
        README.md
        src/
            bgmeter_microtech/
                driver.py
                framing.py
                protocol.py
                records.py
                collector.py
        tests/
            fixtures/
            hardware/

cli/
    pyproject.toml
    README.md
    src/
        bgmeter_cli/
            app.py
            config.py
            store.py
            registry.py
            selection.py
            exporters/
            __main__.py
    tests/
```

The core distribution exposes the `bgmeter` import package and the public
driver implementation interface. The MicroTech distribution exposes
`bgmeter_microtech`, depends only on the public core API, and registers
`MicroTechBgmDriver` through the standard driver entry point. This repository's
default CLI installation depends on both the core and MicroTech distributions,
thereby shipping GoChek/Wellion support while keeping the CLI itself
driver-neutral.

The CLI distribution imports only documented `bgmeter` APIs and installs the
`bgmeter` executable. Package metadata uses normal versioned dependencies rather
than relative path dependencies. Monorepo development installs core, then the
MicroTech driver, then the CLI into the main Python 3.13 environment. This
version is based on the verified active interpreter (`python --version` reports
3.13.13), not an inferred or requested installation name.

Persistent CLI registration belongs to the CLI source base and is separate from
the stateless core registry. On each invocation the CLI resolves its saved
entry-point identities, constructs a core `DriverRegistry`, and asks the core to
validate and query the resulting drivers.

Tests and fixtures live with the source base they verify. Core tests cover
models, transports, discovery, registry behavior, and the public driver
contract. MicroTech tests cover framing, parsing, protocol behavior, saved
captures, and conformance with that public contract. CLI tests cover argument
handling, selection, exit statuses, and exporters by using public core APIs or
public-interface fakes; they do not import driver internals. No shared private
test-helper package crosses a source-base boundary.

## Implementation status

The component boundary, timestamp semantics, normalized record/result model,
public manager/driver API shape, CLI contract, and failure/completeness model
above are implemented, including the versioned JSON and stable CSV contracts.
The generic core, independently registered MicroTech driver, and separate CLI
are independently buildable Python 3.13 packages. Development, test, wheel,
installed-system, and hardware-smoke procedures are documented in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).
