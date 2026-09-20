# Verbosity, Progress Reporting, and Logging

Status: approved design, not yet implemented.

## Problem

`bgmeter read` fails with `error: MicroTech meter returned no trustworthy
history records` and gives the user nothing to act on. The repository has no
logging at all, and the CLI cannot say what it is doing while a slow Bluetooth
operation runs. The cause of the empty result is hidden inside the driver: the
MicroTech collector records an accept/reject verdict for every reply, but
`read_records` raises before any of that reaches a `ReadResult`, so the
evidence is discarded.

## Goals

- Let a non-technical user see what the CLI is doing and, on failure, what to
  try next, using plain language (`-v`, `-vv`).
- Give engineers full technical detail (raw bytes, per-reply verdicts) from
  every layer, independent of the verbosity flags and controllable from the CLI.
- Keep stdout clean for machine-readable output; everything new goes to stderr
  or a file.
- Keep the public driver interface backward compatible.

## Non-goals

- A `--quiet` flag, progress bars, localization, log rotation, environment
  variable configuration, or structured/JSON logs.
- Diagnosing or fixing the specific device failure that motivated this work.
  That is a separate step performed with the new tooling once it exists.
- Version bumps and release. See "Release note" below.

## Two independent mechanisms

| | Progress reporting | Logging |
|---|---|---|
| Audience | Normal users | Engineers, bug reports |
| Content | Plain-language sentences | Technical records, hex, verdicts |
| Controlled by | `-v`, `-vv` | `--log-level`, `--log-file` |
| Destination | stderr | stderr by default; a file exclusively when `--log-file` is given |
| Default | Warnings and errors only | Level `error` |
| Mechanism | New `ProgressEvent` callback | Stdlib `logging` |

**The two mechanisms are fully independent.** Neither reads the other's
settings or output, and neither is filtered, suppressed, or shortened because the
other exists or already covers the same condition. `-v`/`-vv` decide only which
progress lines appear; `--log-level`/`--log-file` decide only which log records
are written. Every emission site decides what to emit on that mechanism's own
merits: a progress event exists because a user needs to understand or act on
something, a log record exists because a developer needs evidence. Any
condition that matters to both audiences is therefore emitted on both.

What keeps this from being noise is that the two say different things:

- **Different content.** Progress messages are plain-language sentences (rules
  below). Log records are technical: exception type, identifiers, timings,
  counts, raw evidence, the command that failed. A log record never repeats a
  progress sentence or gives advice, and a progress message never carries
  technical detail.
- **Different words.** Progress messages avoid protocol and Bluetooth terms; log
  messages use them freely.

Within the user-facing output alone (progress events plus the CLI's `error:`,
`hint:`, and outcome lines), a condition is reported by one line. That rule is
about not repeating a sentence to the user; it does not involve logging.
A failure that ends the command is reported by the CLI's `error:`/`hint:` lines,
so the manager and drivers emit no progress event about a failure they are about
to raise. A non-complete read is reported by the CLI's single outcome line (see
"Outcome line"), so no progress event restates it. Progress events describe
steps and recoverable hiccups, not outcomes.

## 1. Progress reporting

### Core API (`bgmeter/progress.py`, exported from `bgmeter`)

```python
class ProgressLevel(StrEnum):
    INFO = "info"        # shown with -v
    DETAIL = "detail"    # shown with -vv
    WARNING = "warning"  # always shown

@dataclass(frozen=True, slots=True)
class ProgressEvent:
    level: ProgressLevel
    message: str                 # one plain-language sentence
    current: int | None = None   # optional counter, e.g. records received
    total: int | None = None

ProgressCallback = Callable[[ProgressEvent], None]

def emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None: ...
```

`emit_progress` is the only way library code invokes a callback. It ignores a
`None` callback and catches any exception the callback raises (logging it at
WARNING), so a broken reporter can never fail a meter operation. Callbacks are
synchronous and must not block.

Message rules: full sentences in plain language; no protocol or Bluetooth
jargon (no GATT, notification, characteristic, FFE0, hex, transmission); the
words "meter", "reply", and "record" are preferred. A device's Bluetooth
address may appear only in "found" messages. Text is plain ASCII (three dots,
never an ellipsis character) so legacy Windows console code pages can print it.

### Delivery to drivers without changing the driver protocol

`ReadOptions` gains `progress: ProgressCallback | None = None`
(`compare=False, repr=False`). `MeterDriver` method signatures do not change and
`DRIVER_API_VERSION` stays 1. A driver that ignores the field keeps working;
a driver that honors it reports through `emit_progress(options.progress, ...)`.

`MeterManager.__init__` and `MeterManager.default()` accept `progress:
ProgressCallback | None = None`. `ConnectedMeter.read_records` and
`MeterManager.read` fill `options.progress` from the manager's callback only
when the caller's options do not already set one (an explicit value wins).

### Events emitted by the manager

| Level | When | Example wording |
|---|---|---|
| INFO | discovery starts | "Looking for meters nearby (5 s)..." |
| DETAIL | scan finished | "Found 7 Bluetooth devices nearby." |
| DETAIL | endpoint not matched by any driver | "Skipped 'JBL Speaker': not a supported meter." |
| DETAIL | matched device fails driver probe | "'Foo' did not respond like a supported meter." |
| INFO | device identified | "Found GoChek Connect (AA:BB:CC:DD:EE:FF)." |
| INFO | connecting for a read | "Connecting to GoChek Connect..." |
| DETAIL | disconnecting | "Disconnecting from the meter." |
| INFO | read finished, result is complete | "Read 57 records. All records were received." A partial or unknown result emits nothing here; the CLI's outcome line reports it once. |

### Events emitted by the MicroTech driver

| Level | When | Example wording |
|---|---|---|
| DETAIL | first request | "Asking the meter how many records it holds." |
| INFO (`current`/`total`) | each newly accepted record | "Reading records: 12 of 57" |
| INFO | a request is retried | "The meter did not send a usable reply; trying again (2 of 3)." |
| DETAIL | retries exhausted for a record | "Gave up on record 12 after 3 tries and moved on." |
| DETAIL | a reply is rejected | "Ignored a reply that did not match our request." |

The built-in driver emits no WARNING events. Its failures are either raised (and
reported once by the CLI) or end in a non-complete result (reported once by the
outcome line); its `-vv` detail lines explain why. The WARNING level stays in
the API for other drivers to report a non-fatal problem that nothing else
reports.

Reasons for rejected replies map from the collector's attribution strings:

| Collector attribution | Plain-language reason |
|---|---|
| `rejected_unmatched_token`, `rejected_token_mismatch`, `rejected_pre_write` | "a reply that did not match our request" |
| `rejected_wrong_event` | "a reply for a different record than the one we asked for" |
| `rejected_empty_response` | "a reply that contained no record" |
| `rejected_invalid_notification`, `rejected_conflicting_fragment`, `rejected_malformed_transmission` | "a damaged reply" |
| `rejected_incomplete_generation`, `rejected_superseded_*` | "an incomplete reply" |
| `duplicate_conflict` | "a reply that disagreed with a record we already had" |

To support this, `HistoryRecordCollector` gains public read-only members
`transmission_count`, `record_count`, and `attributions_since(start)` (the
attributions of transmissions appended from position `start` on). One
notification can append more than one transmission, so the protocol's
notification handler records `transmission_count` before `add_notification` and
emits one DETAIL event per resulting attribution that has a plain-language
reason. Attributions such as `accepted` and `duplicate_identical` have none and
emit nothing. No collector behavior changes.

### CLI verbosity

Options: `-v` / `--verbose`, repeatable. Accepted before or after the
subcommand; occurrences in both positions are summed (top-level and subcommand
counts are stored under separate destinations and added). The effective level is
capped at 2; `-vvv` behaves as `-vv`.

| Effective level | Shown on stderr |
|---|---|
| 0 | WARNING events, plus errors |
| 1 (`-v`) | WARNING and INFO events |
| 2 (`-vv`) | WARNING, INFO, and DETAIL events, each INFO/DETAIL line prefixed with elapsed time, e.g. `[ 1.2s] Asking the meter...` |

Format: warnings are `warning: <message>`; other lines are the message as-is.
The reporter (`bgmeter_cli/reporter.py`) throttles counter events so a long
history prints about eleven lines: it prints the first and last update and any
update where `current` has advanced at least 10% of `total` since the last
printed line.

`ManagerFactory` changes from `(registry)` to `(registry, progress)` so the CLI
can hand the reporter to the manager. The existing CLI test fakes are updated
mechanically for the new signature.

### Errors and hints

Exception types and exit statuses do not change. The CLI keeps
`error: <message>` and adds `hint:` lines chosen by exception type, printed at
every verbosity level:

| Exception | Hint |
|---|---|
| `DeviceNotFoundError` | Make sure the meter is on and close by, and that Bluetooth is enabled on this computer. |
| `UnsupportedDeviceError` | Bluetooth devices were found, but none is a supported meter. Run `bgmeter drivers list` to see what is supported. |
| `MeterTimeoutError` | The meter did not answer in time. Wake the meter, make sure it is not still connected to the phone app, and try again. Run with `-vv` to see what happened. |
| `DiscoveryError`, `MeterConnectionError` | Check that Bluetooth is on and no other app is connected to the meter. |
| `ProtocolError` | The meter sent data this program could not understand. Try again; if it keeps happening, run with `-vv --log-level debug` to see technical details. |
| selection, config, ambiguity, export errors | none (their messages are already actionable) |

The MicroTech "no records" failure message changes to "The meter connected but
did not send any usable records." It remains a `MeterTimeoutError`, so it keeps
status 5. This `error:` line is the only user-facing report of that failure; at
`-vv` the per-reply detail lines above it explain why.

### Outcome line

When a read returns records but the result is not complete, the CLI writes one
line to stderr at every verbosity level and returns status 5, as it does today.
The existing `retrieval is <state>` prefix is kept (existing tests assert it) and
gains a plain-language explanation:

- partial: `warning: retrieval is partial: some records may be missing.`
- unknown: `warning: retrieval is unknown: the meter cannot confirm that this is
  the full history.`

This is the only user-facing report of a non-complete result; no progress event
restates it.

## 2. Logging

### Emission (always on, all layers)

Stdlib `logging` with `logging.getLogger(__name__)` in each module, so logger
names follow packages: `bgmeter.manager`, `bgmeter.drivers`,
`bgmeter.transports.bleak`, `bgmeter_microtech.driver`,
`bgmeter_microtech.protocol`, `bgmeter_microtech.collector`,
`bgmeter_cli.app`, `bgmeter_cli.registry`, `bgmeter_cli.store`. The `bgmeter`
and `bgmeter_microtech` package `__init__` modules attach a `NullHandler`, so
the libraries never print on their own. No new dependency.

| Level | Content |
|---|---|
| ERROR | A failure the emitting layer absorbs instead of raising: a session cleanup that fails, a driver entry point that fails to load. Also the one-line record of the command's final failure (see "Final failure" below). A layer does not log an exception it re-raises; the CLI logs the final failure once. |
| WARNING | Recoverable problems: retries exhausted, rejected transmissions (summary), driver incompatibility, a matched candidate whose probe says it is not a supported meter (expected for look-alike devices, so not an ERROR). |
| INFO | Milestones: scan start/end with endpoint count, candidate match (driver, confidence), connect/disconnect, probe outcome, read summary (counts, completion, termination reason). |
| DEBUG | Every GATT read/write, every request (event index, attempt), every notification as hex, fragment reassembly, and the verdict for each transmission (`accepted`, `duplicate_*`, `rejected_*`). Also the CLI's resolved config path and enabled drivers. |

Privacy: INFO never contains glucose values or serial numbers. DEBUG contains
raw bytes, which include glucose values, so the docs tell users to review a
log before attaching it to an issue. Hex formatting in the notification hot path
is guarded with `isEnabledFor(DEBUG)`.

### CLI controls (independent of `-v`)

- `--log-level {debug,info,warning,error}`: minimum level of records written by
  the active sink. Default `error`.
- `--log-file PATH`: switch the sink from the terminal to a file. Records are
  appended to `PATH` and, while it is set, **nothing** from logging is written
  to the terminal. Opt-in; no file is written without it. The same
  `--log-level` (default `error`) applies to the file, so a useful capture needs
  e.g. `--log-file bgmeter.log --log-level debug`.
- Both are accepted before or after the subcommand (same rule as `-v`). If both
  positions give `--log-level` the subcommand position wins. Either option alone
  is valid.
- The file's directory must exist. If the file cannot be opened the CLI exits
  with `error: cannot open log file ...` and status 2 before doing any meter work.
- The handler is attached to the root logger, so third-party loggers (`bleak`,
  `asyncio`) are captured at the chosen level. It is removed, and the root level
  restored, in a `finally`, because `run()` is called repeatedly in tests.
- The terminal sink writes to the CLI's stderr stream (never stdout).
- Format for both sinks: `<ISO-8601 local timestamp> <LEVEL> <logger>:
  <message>`. When the sink is a file, its first line records `bgmeter`
  component versions, Python version, platform, and the subcommand (not the full
  argument list).

### Final failure

When a command fails, the CLI prints its own `error:` and `hint:` lines for the
user and separately logs the failure for developers as two records: an ERROR
record, one technical line (exception class, the exception's own message, the
command that failed, and the exit status) that does not repeat the hint or give
advice, and a DEBUG record holding the traceback
(`exc_info`). Both go to the active log sink under the ordinary `--log-level`
rule and nothing else. With the defaults, a failed command therefore shows the
user's `error:`/`hint:` lines and one technical ERROR line on the terminal; a
user who does not want the technical line can send logging to a file with
`--log-file`. The log file always contains how the command ended (at level
`error` or lower).

## Testing

- **core:** `emit_progress` ignores `None` and swallows callback exceptions;
  `ReadOptions.progress` precedence (explicit beats manager); manager event
  order for discover and read against fake transport/driver; `caplog` checks
  for manager logging; the public API exports.
- **microtech:** progress events for retry, exhausted retries (DETAIL), and
  rejected reply (each attribution class); no WARNING event is emitted, including
  when no usable record arrives; `caplog` DEBUG shows request, notification hex,
  and per-transmission verdicts; INFO shows no glucose value or serial number; the
  technical no-records log record carries counts and attributions and does not
  reuse the user-facing sentence; the reworded error message; the collector's
  `transmission_count`, `record_count`, and `attributions_since`.
- **cli:** stderr content at levels 0, 1, and 2 via a fake manager that emits
  events; elapsed-time prefix at `-vv`; counter throttling; hint lines per
  exception type; `-v` before, after, and on both sides of the subcommand;
  `-vvv` equals `-vv`; stdout unchanged; default terminal log level is `error`
  (an absorbed ERROR appears, a WARNING does not); `--log-level debug` shows
  DEBUG records on stderr; `--log-file` writes the header and records and
  produces no logging output on the terminal; `--log-level` applies to the file;
  the final-failure ERROR record and its DEBUG traceback follow the ordinary
  level rule on whichever sink is active (terminal by default, file with
  `--log-file`); unwritable log path exits 2 before any meter work; handler
  removed after `run()`.
- **independence (cli, end to end with fakes):** over the matrix of `-v`
  (0, 1, 2) and `--log-level` (debug, error) and with and without `--log-file`,
  the set of progress lines depends only on `-v` and the set of log records
  depends only on `--log-level` and the sink; changing one never changes the
  other's output. Progress events and errors always go to the terminal even when
  `--log-file` is set.
- **no repeated user sentences (cli, end to end with fakes):** a failing read
  prints exactly one `error:` line and no progress event restating it; a partial
  read prints exactly one `retrieval is partial` line and no "Read N records"
  line; a complete read prints one "Read N records" line; where a condition
  produces both a progress line and a log record, the log record's text names the
  exception type or identifiers and does not contain the progress sentence.
- Existing suites keep passing after the `ManagerFactory` signature update.

## Documentation

CLI README (verbosity and log options with sample output), `docs/DEVELOPMENT.md`
(running with `-vv` and `--log-file ... --log-level debug` on hardware, log
privacy note),
`ARCHITECTURE.md` (a "Progress reporting and logging" section and the
`ReadOptions.progress` contract), core README (`ProgressEvent` API), MicroTech
README (what it reports), and a CHANGELOG `Unreleased` entry.

## Release note

The CLI will require the new core API, but its dependency is
`bgmeter-core>=0.1,<0.2`. Monorepo development installs all three in order, so
this works unchanged. Choosing version numbers and tightening the dependency
range is left to the release step.
