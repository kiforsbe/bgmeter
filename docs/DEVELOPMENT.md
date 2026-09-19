# Development and packaging

This repository contains three independent Python 3.13 source bases. They are
co-located for development but have no relative-path runtime imports and can be
moved to separate repositories:

| Directory | Distribution | Import package | Responsibility |
| --- | --- | --- | --- |
| `core/` | `bgmeter-core` | `bgmeter` | Public models, driver interface, registry, transports, and orchestration |
| `drivers/microtech/` | `bgmeter-microtech` | `bgmeter_microtech` | Community-maintained MicroTech FFE0/FFE1 compatibility driver |
| `cli/` | `bgmeter-cli` | `bgmeter_cli` | Driver-neutral commands, persistent registration, selection, and exporters |

Each source base owns its packaging metadata, README, tests, and required
fixtures. The CLI depends on the published core and MicroTech distribution
versions; its source imports only the public top-level `bgmeter` API. The root
`gocheck.py` is a deprecated compatibility launcher and contains no meter
protocol code.

## Interpreter and development installation

Use the active executable as the source of truth:

```powershell
python --version
```

Development and verification use Python 3.13; the verified main interpreter
for this repository is Python 3.13.13. Install editable packages in dependency
order:

```powershell
python -m pip install -e "core[test]"
python -m pip install -e "drivers/microtech[test]"
python -m pip install -e "cli[test]"
```

Build independent wheels in the same order:

```powershell
python -m pip wheel --no-deps --wheel-dir dist/core .\core
python -m pip wheel --no-deps --wheel-dir dist/microtech .\drivers\microtech
python -m pip wheel --no-deps --wheel-dir dist/cli .\cli
```

The `dist/` directories are ignored build artifacts.

## External driver contract

Another meter implementation depends only on `bgmeter-core` and imports all
implementation-facing types from `bgmeter`, never private modules. A factory
returns an object satisfying `MeterDriver`:

```python
from bgmeter import (
    DRIVER_API_VERSION,
    DriverMatch,
    MeterDevice,
    MeterIdentity,
    ReadOptions,
    ReadResult,
    TransportEndpoint,
    TransportSession,
)


class ExampleDriver:
    driver_id = "example-meter"
    display_name = "Example blood glucose meter"
    api_version = DRIVER_API_VERSION
    supported_transports = frozenset({"ble"})
    known_meter_identities = ("Example Meter",)

    def match_candidate(self, endpoint: TransportEndpoint) -> DriverMatch | None:
        # Inspect discovery metadata only; do not perform I/O here.
        ...

    async def probe(self, session: TransportSession) -> MeterIdentity:
        # Authoritatively identify a connected candidate.
        ...

    async def read_records(
        self,
        session: TransportSession,
        device: MeterDevice,
        options: ReadOptions,
    ) -> ReadResult:
        # Parse this meter's protocol into normalized public models.
        ...


def driver_factory() -> ExampleDriver:
    return ExampleDriver()
```

Register the zero-argument factory in the driver's `pyproject.toml`:

```toml
[project.entry-points."bgmeter.drivers"]
example = "example_meter:driver_factory"
```

`DriverRegistry` validates the API version, stable ID, metadata, and async
methods. Drivers match candidates without side effects and receive manager-owned
transport sessions; they do not scan, connect, or disconnect themselves. A
driver may use a protocol entirely unrelated to MicroTech as long as it returns
the normalized public models. See `core/README.md` for a complete implementation
example.

After installing a driver distribution, enable its entry-point name for the CLI:

```powershell
bgmeter drivers list --available
bgmeter drivers register example
bgmeter drivers info example
```

Registration enables already installed code; it never downloads or installs a
package. `unregister` disables an entry point without uninstalling it.

## CLI commands and outputs

```text
bgmeter devices [--driver DRIVER]
bgmeter info --device DEVICE [--driver DRIVER]
bgmeter read [--device DEVICE] [--driver DRIVER] [--timezone ZONE]
             [--output OUTPUT]... [--show-raw] [--force]
bgmeter drivers list [--available|--registered]
bgmeter drivers info DRIVER
bgmeter drivers register DRIVER
bgmeter drivers unregister DRIVER
```

`read` defaults to terminal output. Repeat `--output` to request any combination
of `terminal`, `csv=PATH`, and `json=PATH`; at most one output may use stdout.
Existing files require `--force`. Terminal output is human-oriented, CSV is a
stable one-row-per-record format, and JSON is the canonical lossless
`bgmeter.read-result` schema version 1 containing completion data, diagnostics,
driver metadata, Decimal text, and hexadecimal raw evidence. Data goes to
stdout/files while prompts, warnings, and diagnostics go to stderr.

The CLI stores versioned registration JSON at
`platformdirs.user_config_path("bgmeter") / "drivers.json"` (normally
`%LOCALAPPDATA%\bgmeter\drivers.json` on Windows and
`~/.config/bgmeter/drivers.json` on Linux). A missing file enables the installed
`microtech` entry point by default; an explicitly saved empty list remains
empty.

## CLI measurement database

`bgmeter read` does not persist records unless the caller passes `--store`.
With that flag, the CLI-private `MeasurementStore` stores valid records before
rendering or publishing requested output. It owns the unencrypted database at
`platformdirs.user_data_path("bgmeter", appauthor=False) / "measurements.sqlite3"`
(normally `%LOCALAPPDATA%\bgmeter\measurements.sqlite3` on Windows). It is
separate from driver registration and has `PRAGMA user_version = 1`.

On Windows, the store explicitly disables `platformdirs`' optional application
author directory so the path has only one `bgmeter` segment. When a legacy
database exists at `%LOCALAPPDATA%\bgmeter\bgmeter\measurements.sqlite3` and
the corrected path is absent, `--store` moves it to the corrected path before
opening it. It never replaces an existing corrected-path database.

The `measurements` table has one normalized row per `record_id` with source
IDs, native sequence, meter/local/UTC timestamps, timezone/offset, `mmol_l`,
native numeric value/unit, and first-store UTC timestamp. The two numeric
values use SQLite `REAL`. The relational `measurement_flags` table stores one
boolean row per `(record_id, name)` and indexes `(name, value)`.

Each call validates schema version, enables foreign keys, and writes new parent
rows plus flags in one transaction. A duplicate `record_id` keeps the original
measurement and flags. Raw protocol evidence, driver-specific metadata,
diagnostics, and JSON output documents are deliberately excluded; use JSON
export when that archival evidence is required. Storage errors during a
`--store` read prevent output publication and use CLI status `6`.

Store tests inject `run(database_path=tmp_path / "measurements.sqlite3")`; this
keyword-only parameter is internal test/embedding support, not a command-line
option. `--store` uses the platform data path.

Exit statuses are:

| Status | Meaning |
| ---: | --- |
| 0 | Complete success |
| 2 | Usage error or ambiguous selection |
| 3 | No supported meter |
| 4 | Connection or transport failure |
| 5 | Protocol failure or non-complete retrieval |
| 6 | Export or configuration-write failure |
| 130 | Interrupted operation |

## Meter time semantics

The observed MicroTech meter stores only a manually set, timezone-unspecified
local wall-clock value. The library and CLI assume that clock is correct; they
do not synchronize or silently correct it. Every normalized record retains:

- `meter_datetime`, exactly as recorded by the meter and without timezone;
- `measured_at_local`, interpreted using the selected IANA timezone;
- `measured_at_utc`, converted from that local interpretation; and
- the timezone name and UTC offset used.

Use `--timezone Europe/Stockholm` (or another IANA name) for reproducible CLI
conversion. Without it, the host timezone is used. Retaining the original meter
value permits later reinterpretation if the meter clock was wrong.

## Tests and system verification

Normal tests are hardware-independent:

```powershell
python -m pytest core/tests -q
python -m pytest drivers/microtech/tests -q
python -m pytest cli/tests -q
python -m pytest -q
```

For the installed-system boundary, build all three wheels, create a uniquely
named virtual environment inside the repository, install core then driver then
CLI, and run:

```powershell
<venv>\Scripts\bgmeter.exe drivers list --registered
<venv>\Scripts\bgmeter.exe --help
<venv>\Scripts\python.exe gocheck.py --help
```

The registered-driver output must show `microtech` as compatible. Before
removing the temporary environment, resolve its absolute path and verify it is
inside this repository; remove only that validated directory.

## Opt-in physical-meter test

The normal suite never requires Bluetooth hardware. To exercise a powered,
advertising supported meter explicitly:

```powershell
$env:BGMETER_HARDWARE = "1"
python -m pytest drivers/microtech/tests/hardware/test_live_meter.py -q -s
```
