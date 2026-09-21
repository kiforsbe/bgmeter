# Reading Only the Newest Events

Status: approved design, not yet implemented.

## Problem

`bgmeter read` always retrieves a meter's entire stored history. The MicroTech
protocol costs one BLE request/response round trip per record, so a meter
holding several hundred events takes minutes to read, and every routine sync
pays that cost again to re-fetch records the user already has.

Two related needs follow from this:

- Read only the newest handful of events, when a full history is not wanted.
- Read only the events not already recorded in the local SQLite database, so a
  repeated sync costs close to nothing.

Both are wire-level savings, not output filtering. Reading all 500 records and
then discarding 490 of them would satisfy neither need.

## Goals

- Let a caller cap a read to the newest N records, and let the MicroTech driver
  honor that cap by stopping the index walk early.
- Let the CLI tell a driver which records it already holds, so the driver stops
  as soon as it reaches one.
- Keep the core protocol-neutral: nothing in `bgmeter` may assume history is a
  monotonic integer sequence.
- Keep the change additive to the public driver contract. A driver that ignores
  the new options stays correct.
- Report a deliberately shortened read distinguishably from a degraded one.

## Non-goals

- Filling gaps left by earlier partial reads. `--new-only` stops at the first
  known record; repairing a gap is what a full read is for.
- Time-based selection (`--since <date>`). Index-based selection is what the
  MicroTech protocol supports cheaply.
- Any change to how records are normalized, exported, or deduplicated on
  insert.
- Version bumps and release.

## User-facing surface

Two new flags on `bgmeter read`:

| Flag | Meaning |
|---|---|
| `--newest N` | Read at most the N most recent records. `N < 1` is a usage error (exit 2). |
| `--new-only` | Consult the measurement database and stop at the first record already stored for this meter. |

`--new-only` works with or without `--store`, so a user can preview what is new
without recording it. Given together, the two flags mean "at most N, and stop
early if I reach something I already have".

`--store` on its own is unchanged: a full read, deduplicated on insert.

## Core contract

`ReadOptions` gains two fields, both additive with defaults:

```python
newest_count: int | None = None
known_record_ids: frozenset[str] = frozenset()
```

Both are hints. A driver may honor or ignore either; `ReadResult.completion`
reports what actually happened.

`known_record_ids` carries `record_id` strings rather than sequence numbers.
This keeps the core free of any assumption about meter numbering: the driver
alone knows how to project a candidate record onto the `record_id` it would
produce, and a driver whose `record_id` is not predictable before fetching
simply ignores the field and performs a full read.

`known_record_ids` is both a stop signal and a filter. Records the caller
already holds are excluded from `ReadResult.records`, so an up-to-date
`--new-only` read returns zero records.

### Completion status

`CompletionStatus` gains a fourth member:

```python
TRUNCATED = "truncated"
```

It means: the read delivered everything that was asked for, and that was
deliberately less than the meter's full history.

`expected_count` continues to report the meter's true total. A `--newest 10`
read against a 500-record meter reports `expected_count=500`,
`received_count=10`, `completion=truncated` — the export stays honest about
what was left behind.

| Situation | Status |
|---|---|
| Every targeted record received, targets were the whole history | `COMPLETE` |
| Every targeted record received, targets were a subset | `TRUNCATED` |
| A targeted record is missing | `PARTIAL` |
| The meter never revealed its newest index | `UNKNOWN` |
| Post-read cleanup failed | `PARTIAL` (unchanged) |

## MicroTech driver

### Walk direction

The index walk in `protocol.read_history` currently asks index 0 to learn the
newest index N, then walks `1..N` ascending. It becomes descending, from N
down, unconditionally — including for unlimited reads.

Descending is the only order in which an early stop is meaningful, and making
it unconditional avoids carrying two walk directions. It also improves the
failure mode of a full read: an interrupted read now retains the newest
records rather than the oldest.

This changes the order of raw wire evidence for full reads. Final output
ordering is unaffected: `HistoryRecordCollector.records` already sorts by meter
datetime and event index.

### Stop conditions

After index 0 reveals N, the walk targets N down to
`max(1, N - newest_count + 1)`, or to 1 when `newest_count` is `None`. It stops
early at the first index whose projected `record_id` is in `known_record_ids`.

The index-to-`record_id` projection must be shared with `_normalize_record` in
`driver.py` rather than duplicated, so the stop test and the emitted
identifiers cannot drift apart.

### Collector

`HistoryRecordCollector` learns the target index set so `status` can
distinguish `COMPLETE` from `TRUNCATED`. `expected_count` keeps its present
meaning: the meter's newest index, that is, its total record count.

### Empty results

`read_records` currently raises `MeterTimeoutError` when the collector produced
no records. That guard must distinguish two cases:

- The collector received nothing usable at all: still `MeterTimeoutError`.
- The collector received records, all of which were already known: a valid
  `ReadResult` with no records, `completion=TRUNCATED`, and
  `termination_reason="already_stored"`.

### Termination reasons

`termination_reason` gains `"limit_reached"` (the `--newest` cap was hit) and
`"already_stored"` (the walk reached a record the caller already held).

## Store

One new read method on `MeasurementStore`, opening the database once:

```python
def device_state(self, *, driver_id: str, device_id: str) -> StoredDeviceState
```

returning a frozen
`StoredDeviceState(record_ids: frozenset[str], highest_sequence: int | None)`,
scoped by `source_driver_id` and `source_device_id` so two meters sharing one
database cannot stop each other's reads.

A database file that does not exist yet returns empty state and is **not**
created — `--new-only` on a fresh machine is simply a full read. A database at
an unsupported schema version still raises `StoreError`. Legacy-path migration
is not triggered by a read.

## CLI

`--new-only` calls `device_state` after device selection and before the read,
then passes `known_record_ids` through `ReadOptions`. `--newest` passes
`newest_count`.

### Outcome handling

`_run_meter_command` currently treats any status other than `COMPLETE` as exit
5, via a dictionary lookup that would raise `KeyError` on a new member. It
becomes a membership test: `TRUNCATED` prints no warning and exits 0;
`PARTIAL` and `UNKNOWN` keep today's warning text and exit 5.

### Reset guard

`record_id` is `driver:selector:index`. If a meter's memory is cleared and its
indexes restart, new measurements collide with stored ones. This hazard already
exists for `--store`; `--new-only` would make it silent, because every genuinely
new record would look like one already held.

The guard: `device_state` also reports the highest stored `native_sequence` for
the meter. If the read comes back with an `expected_count` below that value, the
CLI writes a warning to stderr stating that the meter's history appears to have
been reset or cleared, and that a full read is needed. It is a warning, not an
error.

### Progress

`ConnectedMeter.read_records` announces success only on `COMPLETE`, so a
truncated read would otherwise finish silently. A progress line is added for
the truncated case, distinguishing "read N newer records, stopped at records
already saved" from "no new records".

### Exporters

`render_terminal`, `render_csv`, and `render_json` must render a result with
zero records without raising.

## Testing

Test-driven, per package, running only the suites covering the changed code.

**core** — `test_public_contract.py`: the new `CompletionStatus` member's
value, and the new `ReadOptions` fields' defaults and frozenness.

**MicroTech driver** — the walk is descending; `newest_count` stops at the
correct lowest index; a known `record_id` stops the walk and is filtered out of
the result; the `COMPLETE` / `TRUNCATED` / `PARTIAL` boundaries; an all-known
read yields an empty result rather than `MeterTimeoutError`.

**CLI** — `--newest 0` rejected at parse time; `--new-only` without `--store`
is accepted and still consults the database; exit 0 with no warning on
`TRUNCATED`; the reset-guard warning fires when the
meter reports fewer records than the database holds; a zero-record result
renders through all three exporters.

**store** — `device_state` against a missing file, a populated file, and two
devices in one database.

## Files

- `core/src/bgmeter/models.py`
- `drivers/microtech/src/bgmeter_microtech/{protocol,collector,driver}.py`
- `cli/src/bgmeter_cli/{app,store}.py`
- tests in all three packages
- `README.md`, `ARCHITECTURE.md` (completeness model and CLI surface),
  `CHANGELOG.md`
