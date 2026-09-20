# bgmeter-cli

`bgmeter-cli` is the driver-neutral command line client for `bgmeter-core`.
The default installation includes the separately packaged MicroTech driver,
which is discovered through the public `bgmeter.drivers` entry-point group.

```console
bgmeter devices
bgmeter info --device ble:AA-BB-CC-DD-EE-FF
bgmeter read --device ble:AA-BB-CC-DD-EE-FF --timezone Europe/Stockholm
bgmeter read --device ble:AA-BB-CC-DD-EE-FF \
  --output terminal --output csv=records.csv --output json=records.json
bgmeter drivers list --registered
```

Driver registration only enables or disables an already installed entry point;
it never downloads or uninstalls packages. Configuration is stored as versioned
JSON in the platform user configuration directory for `bgmeter`.

The JSON read result is the canonical lossless format. CSV has a stable row per
record, while terminal output is intended for people and is not a stable data
contract. Data is written to stdout or requested files; errors and selection
prompts are written to stderr.

Exit statuses are `0` for a complete result, `2` for usage or ambiguous
selection, `3` for no supported meter, `4` for connection or transport failure,
`5` for protocol failure or an incomplete retrieval, `6` for export failure,
and `130` for interruption.

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
