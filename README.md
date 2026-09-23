# bgmeter

`bgmeter` reads stored measurements from Bluetooth blood-glucose meters through
separately packaged drivers. This repository currently includes:

- `bgmeter-core`: the driver-neutral public library, discovery, and BLE transport;
- `bgmeter-microtech`: the verified proprietary FFE0/FFE1 driver used by
  MicroTech GoChek/Wellion-compatible meters; and
- `bgmeter-cli`: the `bgmeter` command-line program.

The original `gocheck.py` remains only as a deprecated compatibility launcher.

## Safety, privacy, and compatibility

This is community-maintained software. It is not medical advice, is not a
certified medical device, and must not be used to make treatment decisions.
Verify readings with an appropriate approved device and seek qualified medical
advice when needed.

The project is not affiliated with or endorsed by GoChek, Wellion, MicroTech
Medical, or their owners. Those names are used only to identify observed
compatibility; all trademarks belong to their respective owners.

Meter readings, device identifiers, databases, exports, and raw protocol output
can contain sensitive health or device information. Keep them out of public
issues and repositories. Local SQLite storage is unencrypted; you are
responsible for securing, backing up, and deleting your own data.

## Install from this repository

Use Python 3.13 or newer. The repository is verified with Python 3.13.13.

```powershell
python -m pip install -e .\core
python -m pip install -e .\drivers\microtech
python -m pip install -e .\cli
```

For development and tests, install the test extras instead:

```powershell
python -m pip install -e "core[test]"
python -m pip install -e "drivers/microtech[test]"
python -m pip install -e "cli[test]"
```

Confirm that the bundled driver is available:

```powershell
bgmeter drivers list --registered
```

It should show `microtech` as `compatible`. A missing configuration enables
that bundled entry point by default. If it was explicitly unregistered or the
saved configuration is empty, enable it again with:

```powershell
bgmeter drivers register microtech
```

## Run from a checkout without installing project packages

From the repository root, expose the three source directories for the current
PowerShell session and run the CLI module directly:

```powershell
$env:PYTHONPATH = "$PWD\core\src;$PWD\drivers\microtech\src;$PWD\cli\src"
python -m bgmeter_cli --help
python -m bgmeter_cli devices
```

The setting applies only to that PowerShell session. It avoids installing the
three local `bgmeter` packages, but Python still needs their third-party
dependencies (such as `bleak` and `platformdirs`) available in the active
environment. This direct mode does **not** create Python package entry-point
metadata, so `bgmeter_microtech` cannot be discovered or registered from a
pure `PYTHONPATH` checkout. Install the local core, MicroTech driver, and CLI
packages using the commands above before reading a meter or running
`bgmeter drivers register microtech`.

## Read a meter

First list compatible devices, then copy a selector into later commands:

```powershell
bgmeter devices
bgmeter info --device ble:AA-BB-CC-DD-EE-FF
```

Read the meter and display records in the terminal:

```powershell
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm
```

Use repeated `--output` arguments to save CSV and lossless JSON alongside the
terminal display:

```powershell
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm `
  --output terminal `
  --output csv=readings.csv `
  --output json=readings.json
```

Add a message to the latest reading returned by a read with `-m` (or
`--message`):

```powershell
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm `
  -m "After lunch" --store
```

The message appears on that latest reading in terminal, CSV, and JSON output.
When an existing local database has messages for older matching readings, those
messages appear in the exports too. Without usable stored message data, only the
newly supplied message is included, on the latest reading.

`terminal` is intended for people. CSV has stable one-record-per-row columns.
JSON is the canonical archival output: it preserves normalized records,
completion state, diagnostics, driver metadata, and raw request/response bytes.
Use `--show-raw` to include raw evidence in terminal output. Existing output
files require `--force` to overwrite them. Treat JSON and raw output as
sensitive data.

The `read` options also have short forms: `-d` device, `-D` driver, `-z`
timezone, `-o` output, `-r` show raw, `-s` store, `-n` newest, `-N` new-only,
`-f` force, and `-m` message. Shared logging options are `-v`, `-l` for log
level, and `-L` for log file.

Reads do not write a database by default. Pass `--store` to persist unique
normalized records locally:

```powershell
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm --store
```

Storage uses `platformdirs.user_data_path("bgmeter", appauthor=False) /
"measurements.sqlite3"` (normally `%LOCALAPPDATA%\bgmeter\measurements.sqlite3`
on Windows). The unencrypted database stores
measurement values, timestamp interpretations, source IDs, relational flags,
and optional reading messages; it deduplicates repeated reads by driver-scoped
`record_id`. It does not
store raw protocol bytes, driver metadata, or retrieval diagnostics. Request
JSON output when an archival capture of that evidence is needed.

Versions that created the former doubled Windows path
`%LOCALAPPDATA%\bgmeter\bgmeter\measurements.sqlite3` move that database to
the single-folder location on the next `--store` read, provided no database
already exists at the new path. An existing new-path database is never
overwritten.

To read only the most recent records instead of the whole history:

```powershell
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm --newest 20
```

To read only what the local database does not already hold, which makes a
routine sync cost almost nothing:

```powershell
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm `
  --new-only --store
```

`--new-only` walks backwards from the meter's newest record and stops at the
first one already stored, so it does not fill gaps left by an earlier
interrupted read; run a full read for that. It reads the database whether or
not `--store` is given, so it can preview what is new without recording it. It
never creates the database, so on a machine with no database it is simply a
full read. If the only database is still at the former doubled path, `--new-only`
sees an empty store (one full read) until a `--store` read migrates it. When a flag shortens the read, the result is reported as `truncated`
rather than `complete`, and the command exits 0.

The meter stores a manually set local clock. `--timezone` tells the program how
to interpret that local wall-clock time. Every record retains the original
timezone-unspecified meter time, local interpreted time, UTC time, timezone,
and UTC offset. Without `--timezone`, the host system timezone is used.

## Drivers and device selection

The CLI is not limited to the bundled meter driver. It discovers installed
Python entry points and lets you enable them without downloading code:

```powershell
bgmeter drivers list --available
bgmeter drivers register example
bgmeter drivers info example
bgmeter drivers unregister example
```

Missing configuration enables the installed `microtech` entry point by default.
The registration file is versioned JSON at the platform configuration path for
`bgmeter` (normally `%LOCALAPPDATA%\bgmeter\drivers.json` on Windows).

## Develop and debug

Run the full hardware-independent suite:

```powershell
python -m pytest -q
```

Run individual source-base suites while working on one layer:

```powershell
python -m pytest core/tests -q
python -m pytest drivers/microtech/tests -q
python -m pytest cli/tests -q
```

Useful CLI diagnostics:

```powershell
bgmeter --help
bgmeter drivers list --registered
bgmeter read --help
```

When a read misbehaves, ask the program to explain itself. `-vv` narrates each
step in plain language, and a debug log captures the technical evidence:

```powershell
bgmeter -vv read --device ble:AA-BB-CC-DD-EE-FF --log-file bgmeter.log --log-level debug
```

Debug logs contain raw meter bytes (including glucose values); review them
before sharing. See [cli/README.md](cli/README.md) for the full option list.

Normal tests use saved captures and fake GATT transport; they do not need a
meter. The live hardware test is deliberately opt-in and should only be run
when a supported meter is powered on and advertising:

```powershell
$env:BGMETER_HARDWARE = "1"
python -m pytest drivers/microtech/tests/hardware/test_live_meter.py -q -s
```

For package boundaries, external-driver implementation, wheel/system testing,
CLI exit statuses, and the complete development procedure, see
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). See [ARCHITECTURE.md](ARCHITECTURE.md)
for the public design and [KNOWLEDGEBASE.md](KNOWLEDGEBASE.md) for the
meter-specific BLE and protocol findings.

## License

This project is licensed under the [MIT License](LICENSE).
