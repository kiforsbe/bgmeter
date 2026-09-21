# Newest-Events Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--newest N` and `--new-only` to `bgmeter read`, so a read can stop after the N most recent records or as soon as it reaches a record already in the local database.

**Architecture:** Two additive hint fields on the public `ReadOptions` (`newest_count`, `known_record_ids`) let a caller bound a read; the MicroTech driver honors them by reversing its index walk to descend from the newest event and stopping early. A new `CompletionStatus.TRUNCATED` distinguishes "delivered everything asked for, which was deliberately less than the full history" from a degraded `PARTIAL` read. The CLI supplies `known_record_ids` from a new read-only `MeasurementStore.device_state()` query.

**Tech Stack:** Python 3.13, stdlib `sqlite3`, `argparse`, `asyncio`; pytest + pytest-asyncio. Three separately installed packages: `core` (`bgmeter`), `drivers/microtech` (`bgmeter_microtech`), `cli` (`bgmeter_cli`).

**Spec:** `docs/superpowers/specs/2026-09-21-newest-events-read-design.md`

## Global Constraints

- The core package must stay protocol-neutral: nothing in `core/src/bgmeter/` may assume meter history is a monotonic integer sequence. Meter-specific numbering lives in the driver.
- Both new `ReadOptions` fields are **hints**. A driver that ignores them must remain correct, so both get defaults and no driver is required to read them.
- `ReadOptions` and every model in `core/src/bgmeter/models.py` are frozen, slotted dataclasses. New fields must be immutable value types (`int | None`, `frozenset[str]`).
- `expected_count` on `ReadResult` keeps its existing meaning — the meter's true total record count — even when the read is truncated.
- `CompletionStatus` is a `StrEnum`; the new member's value is exactly `"truncated"`.
- The MicroTech driver reaches the core only through the public `bgmeter` package, never a private module. `drivers/microtech/tests/test_driver.py::test_driver_imports_core_only_through_its_public_package` enforces this.
- `protocol.py` must not import from `driver.py` (that would be circular). Anything the walk needs about `record_id` formatting is passed in as a callable.
- Tests are run per package: `python -m pytest core/tests -q`, `python -m pytest drivers/microtech/tests -q`, `python -m pytest cli/tests -q`. Do not run the hardware suite (`drivers/microtech/tests/hardware/`).
- Commit after each task. Do not push.

---

### Task 1: Core contract — `TRUNCATED` status and the two `ReadOptions` hints

**Files:**
- Modify: `core/src/bgmeter/models.py:59-62` (`CompletionStatus`), `core/src/bgmeter/models.py:152-157` (`ReadOptions`)
- Test: `core/tests/test_public_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CompletionStatus.TRUNCATED` with value `"truncated"`.
  - `ReadOptions.newest_count: int | None = None`
  - `ReadOptions.known_record_ids: frozenset[str] = frozenset()`

  Every later task depends on these exact names.

- [ ] **Step 1: Write the failing tests**

Add to `core/tests/test_public_contract.py`, directly after `test_read_options_are_frozen`:

```python
def test_completion_status_includes_truncated():
    assert CompletionStatus.TRUNCATED.value == "truncated"
    assert [status.value for status in CompletionStatus] == [
        "complete",
        "partial",
        "unknown",
        "truncated",
    ]


def test_read_options_carry_optional_read_limits():
    default = ReadOptions()

    assert default.newest_count is None
    assert default.known_record_ids == frozenset()

    bounded = ReadOptions(newest_count=10, known_record_ids={"fake:meter-1:7"})

    assert bounded.newest_count == 10
    assert bounded.known_record_ids == frozenset({"fake:meter-1:7"})
    with pytest.raises(FrozenInstanceError):
        bounded.newest_count = 5
```

Note the second test passes a plain `set` and expects a `frozenset` back — `ReadOptions.__post_init__` must coerce it, the same way `TransportEndpoint` coerces `service_uuids`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest core/tests/test_public_contract.py -q`
Expected: FAIL — `AttributeError: TRUNCATED` and `TypeError: ReadOptions.__init__() got an unexpected keyword argument 'newest_count'`.

- [ ] **Step 3: Add the enum member**

In `core/src/bgmeter/models.py`, extend `CompletionStatus`. Append `TRUNCATED` last so existing member ordering is untouched:

```python
class CompletionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    TRUNCATED = "truncated"
```

- [ ] **Step 4: Add the `ReadOptions` fields**

Replace the `ReadOptions` dataclass in `core/src/bgmeter/models.py` with:

```python
@dataclass(frozen=True, slots=True)
class ReadOptions:
    timezone: str | None = None
    request_timeout: float = 5.0
    retries: int = 3
    newest_count: int | None = None
    known_record_ids: frozenset[str] = frozenset()
    progress: ProgressCallback | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "known_record_ids", frozenset(self.known_record_ids)
        )
```

`progress` stays last so existing positional construction of the first three fields is unaffected.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest core/tests -q`
Expected: PASS. Note `core/tests/test_final_review.py:389` parametrizes over `list(CompletionStatus)` — it must still pass with the fourth member; if it fails, stop and report rather than editing that test.

- [ ] **Step 6: Commit**

```bash
git add core/src/bgmeter/models.py core/tests/test_public_contract.py
git commit -m "feat(core): add TRUNCATED status and read-limit options"
```

---

### Task 2: Core progress — announce a truncated read

**Files:**
- Modify: `core/src/bgmeter/manager.py:87-106` (`ConnectedMeter.read_records`)
- Test: `core/tests/test_manager.py`

**Interfaces:**
- Consumes: `CompletionStatus.TRUNCATED` (Task 1).
- Produces: no new API. A `TRUNCATED` result emits exactly one `ProgressLevel.INFO` event.

`ConnectedMeter.read_records` currently emits a closing progress line only when `result.completion is CompletionStatus.COMPLETE`, so a truncated read would finish in silence.

- [ ] **Step 1: Write the failing test**

In `core/tests/test_manager.py`, the module-level `FakeDriver.read_records` always returns `COMPLETE`. Add a constructor-driven override so a test can ask for another status. Change `FakeDriver.__init__` to accept `completion=CompletionStatus.COMPLETE` and store it as `self.completion`, then change the `ReadResult(...)` it returns to use `completion=self.completion` and `records=self.records`, with `records=()` as a new constructor argument defaulting to `()`.

Then add, next to `test_read_reports_connect_completion_and_disconnect`:

```python
@pytest.mark.asyncio
async def test_read_reports_a_truncated_read_that_returned_records():
    events = []
    driver = FakeDriver(completion=CompletionStatus.TRUNCATED, records=("a", "b"))
    manager, _, _ = make_manager(driver=driver, progress=events.append)

    await manager.read(make_device())

    assert _summary(events) == [
        (ProgressLevel.INFO, "Connecting to Meter..."),
        (ProgressLevel.INFO, "Read 2 records. Older records were not requested."),
        (ProgressLevel.DETAIL, "Disconnecting from the meter."),
    ]


@pytest.mark.asyncio
async def test_read_reports_a_truncated_read_that_returned_nothing():
    events = []
    driver = FakeDriver(completion=CompletionStatus.TRUNCATED)
    manager, _, _ = make_manager(driver=driver, progress=events.append)

    await manager.read(make_device())

    assert _summary(events) == [
        (ProgressLevel.INFO, "Connecting to Meter..."),
        (ProgressLevel.INFO, "No new records."),
        (ProgressLevel.DETAIL, "Disconnecting from the meter."),
    ]
```

`records=("a", "b")` are placeholder objects; `ReadResult.records` is only counted here, never inspected.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest core/tests/test_manager.py -q -k truncated`
Expected: FAIL — the two expected `INFO` lines are missing from `_summary(events)`.

- [ ] **Step 3: Emit the truncated lines**

In `core/src/bgmeter/manager.py`, in `ConnectedMeter.read_records`, replace the trailing `if result.completion is CompletionStatus.COMPLETE:` block with:

```python
        if result.completion is CompletionStatus.COMPLETE:
            emit_progress(
                options.progress,
                ProgressEvent(
                    ProgressLevel.INFO,
                    f"Read {_plural(len(result.records), 'record')}. "
                    "All records were received.",
                ),
            )
        elif result.completion is CompletionStatus.TRUNCATED:
            emit_progress(
                options.progress,
                ProgressEvent(
                    ProgressLevel.INFO,
                    "No new records."
                    if not result.records
                    else f"Read {_plural(len(result.records), 'record')}. "
                    "Older records were not requested.",
                ),
            )
        return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest core/tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/src/bgmeter/manager.py core/tests/test_manager.py
git commit -m "feat(core): announce a truncated read in progress output"
```

---

### Task 3: Collector — target indexes, truncated status, missing indexes

**Files:**
- Modify: `drivers/microtech/src/bgmeter_microtech/collector.py` (`HistoryRecordCollector.__init__`, `is_complete`, `status`)
- Test: `drivers/microtech/tests/test_protocol.py`

**Interfaces:**
- Consumes: `CompletionStatus.TRUNCATED` (Task 1).
- Produces, on `HistoryRecordCollector`:
  - `set_target_indexes(self, indexes: Iterable[int]) -> None` — declares which event indexes this read intends to fetch. Called at most once, after the newest index is known.
  - `truncation_reason: str | None` — a plain attribute the caller assigns; `"limit_reached"` or `"already_stored"`.
  - `missing_indexes` property → `tuple[int, ...]`, sorted, the targeted indexes not received.
  - `is_complete` now means "every **targeted** index was received".

  Without `set_target_indexes`, every property behaves exactly as today (targets default to `1..expected_count`).

- [ ] **Step 1: Write the failing tests**

Add to `drivers/microtech/tests/test_protocol.py`, after `test_collector_exposes_transmission_and_record_counters`:

```python
def _collector_with_indexes(*indexes: int) -> HistoryRecordCollector:
    collector = HistoryRecordCollector()
    for index in sorted(indexes):
        for frame in _frames(0 if index == 4 else index):
            collector.add_notification(frame)
    return collector


def test_collector_without_targets_keeps_whole_history_semantics() -> None:
    collector = _collector_with_indexes(1, 2, 3, 4)

    assert collector.expected_count == 4
    assert collector.is_complete is True
    assert collector.missing_indexes == ()
    assert collector.truncation_reason is None
    assert collector.status is CompletionStatus.COMPLETE


def test_collector_reports_truncated_when_targets_are_a_subset() -> None:
    collector = _collector_with_indexes(3, 4)
    collector.set_target_indexes([4, 3])

    assert collector.expected_count == 4
    assert collector.is_complete is True
    assert collector.missing_indexes == ()
    assert collector.status is CompletionStatus.TRUNCATED


def test_collector_reports_partial_when_a_targeted_index_is_missing() -> None:
    collector = _collector_with_indexes(4)
    collector.set_target_indexes([4, 3])

    assert collector.missing_indexes == (3,)
    assert collector.status is CompletionStatus.PARTIAL


def test_collector_reports_truncated_for_an_empty_target_set() -> None:
    collector = _collector_with_indexes(4)
    collector.set_target_indexes([])

    assert collector.is_complete is True
    assert collector.missing_indexes == ()
    assert collector.status is CompletionStatus.TRUNCATED


def test_collector_targets_covering_the_whole_history_stay_complete() -> None:
    collector = _collector_with_indexes(1, 2, 3, 4)
    collector.set_target_indexes([4, 3, 2, 1])

    assert collector.status is CompletionStatus.COMPLETE
```

`_frames(0)` carries event index 4 (the newest); `_frames(1..3)` carry indexes 1–3. These collectors run in offline capture mode (`strict_live_mode=False`), where `expected_count` tracks the highest observed index, so feeding indexes 1–4 yields `expected_count == 4`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest drivers/microtech/tests/test_protocol.py -q -k collector`
Expected: FAIL — `AttributeError: 'HistoryRecordCollector' object has no attribute 'set_target_indexes'`.

- [ ] **Step 3: Implement the target set**

In `drivers/microtech/src/bgmeter_microtech/collector.py`:

Add to the imports at the top of the file: `from collections.abc import Iterable, Mapping` (the module currently imports only `Mapping`).

In `HistoryRecordCollector.__init__`, beside the other counters, add:

```python
        self.truncation_reason: str | None = None
        self._target_indexes: frozenset[int] | None = None
```

Add the declaration method next to `has_index`:

```python
    def set_target_indexes(self, indexes: Iterable[int]) -> None:
        """Declare which event indexes this read intends to fetch."""
        self._target_indexes = frozenset(indexes)
```

Replace the `is_complete` property and add two neighbours:

```python
    @property
    def _full_history_indexes(self) -> frozenset[int] | None:
        if self.expected_count is None or self.expected_count <= 0:
            return None
        return frozenset(range(1, self.expected_count + 1))

    @property
    def _targets(self) -> frozenset[int] | None:
        if self._target_indexes is not None:
            return self._target_indexes
        return self._full_history_indexes

    @property
    def is_complete(self) -> bool:
        targets = self._targets
        if targets is None:
            return False
        return all(index in self._records for index in targets)

    @property
    def missing_indexes(self) -> tuple[int, ...]:
        targets = self._targets
        if targets is None:
            return ()
        return tuple(sorted(index for index in targets if index not in self._records))
```

Note `_targets` returns an empty frozenset (not `None`) when `set_target_indexes([])` was called, so `is_complete` is vacuously `True` — that is the "everything I wanted, I already had" case.

Replace `status`:

```python
    @property
    def status(self) -> CompletionStatus:
        if self._cleanup_evidence is not None:
            return CompletionStatus.PARTIAL
        if self.expected_count is None:
            return CompletionStatus.UNKNOWN
        if not self.is_complete:
            return CompletionStatus.PARTIAL
        if self._targets != self._full_history_indexes:
            return CompletionStatus.TRUNCATED
        return CompletionStatus.COMPLETE
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest drivers/microtech/tests/test_protocol.py -q`
Expected: PASS, including the pre-existing collector and `read_history` tests.

- [ ] **Step 5: Commit**

```bash
git add drivers/microtech/src/bgmeter_microtech/collector.py drivers/microtech/tests/test_protocol.py
git commit -m "feat(microtech): let the collector report a targeted subset as truncated"
```

---

### Task 4: Protocol — descend the index walk and stop early

**Files:**
- Modify: `drivers/microtech/src/bgmeter_microtech/protocol.py:53-60` (`read_history` signature) and `:218-233` (the walk)
- Test: `drivers/microtech/tests/test_protocol.py`

**Interfaces:**
- Consumes: `set_target_indexes`, `truncation_reason`, `missing_indexes` (Task 3).
- Produces the new `read_history` signature:

```python
async def read_history(
    session: GattSession,
    characteristic: str,
    *,
    request_timeout: float = 5.0,
    retries: int = 3,
    progress: ProgressCallback | None = None,
    newest_count: int | None = None,
    is_known: Callable[[int], bool] | None = None,
) -> HistoryRecordCollector:
```

  `is_known(event_index)` answers "the caller already holds this record". `protocol.py` never formats a `record_id` itself — Task 5 supplies the closure.

**Behaviour change to existing tests:** the walk now descends. `test_read_history_queries_zero_then_missing_indexes_and_stops` currently asserts `requested == [0, 1, 2, 3]`; it becomes `[0, 3, 2, 1]`. `test_read_history_retries_a_missing_index_and_returns_partial` currently asserts `requested == [0, 1, 2, 2, 3]`; it becomes `[0, 3, 2, 2, 1]`. Update both, and only those two assertions — every other assertion in those tests stays as written, because the final record set and status are order-independent.

- [ ] **Step 1: Write the failing tests**

Add to `drivers/microtech/tests/test_protocol.py`, after `test_read_history_queries_zero_then_missing_indexes_and_stops`:

```python
@pytest.mark.asyncio
async def test_read_history_stops_after_the_requested_newest_count() -> None:
    attempts = {index: [_indexed_frames(index)] for index in range(4)}
    session = FakeGattSession(attempts)

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=2,
        newest_count=2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    assert requested == [0, 3]
    assert {record.native.event_index for record in collector.records} == {3, 4}
    assert collector.expected_count == 4
    assert collector.status is CompletionStatus.TRUNCATED
    assert collector.truncation_reason == "limit_reached"


@pytest.mark.asyncio
async def test_newest_count_larger_than_the_history_reads_everything() -> None:
    attempts = {index: [_indexed_frames(index)] for index in range(4)}
    session = FakeGattSession(attempts)

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=2,
        newest_count=99,
    )

    assert {record.native.event_index for record in collector.records} == {1, 2, 3, 4}
    assert collector.status is CompletionStatus.COMPLETE
    assert collector.truncation_reason is None


@pytest.mark.asyncio
async def test_read_history_stops_at_the_first_record_the_caller_already_has() -> None:
    attempts = {index: [_indexed_frames(index)] for index in range(4)}
    session = FakeGattSession(attempts)

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=2,
        is_known=lambda event_index: event_index <= 2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    assert requested == [0, 3]
    assert {record.native.event_index for record in collector.records} == {3, 4}
    assert collector.status is CompletionStatus.TRUNCATED
    assert collector.truncation_reason == "already_stored"


@pytest.mark.asyncio
async def test_read_history_asks_for_nothing_when_the_newest_is_already_known() -> None:
    attempts = {index: [_indexed_frames(index)] for index in range(4)}
    session = FakeGattSession(attempts)

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=2,
        is_known=lambda event_index: True,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    assert requested == [0]
    assert collector.record_count == 1
    assert collector.status is CompletionStatus.TRUNCATED
    assert collector.truncation_reason == "already_stored"
    assert session.events[-1] == "stop:ffe1"
```

The last test still receives record 4 — the index-0 probe always delivers the newest record, and filtering it out of the *result* is Task 5's job, not the collector's.

Now update the two existing order assertions:
- In `test_read_history_queries_zero_then_missing_indexes_and_stops`, change `assert requested == [0, 1, 2, 3]` to `assert requested == [0, 3, 2, 1]`.
- In `test_read_history_retries_a_missing_index_and_returns_partial`, change `assert requested == [0, 1, 2, 2, 3]` to `assert requested == [0, 3, 2, 2, 1]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest drivers/microtech/tests/test_protocol.py -q -k "newest or already"`
Expected: FAIL — `TypeError: read_history() got an unexpected keyword argument 'newest_count'`.

- [ ] **Step 3: Implement the bounded descending walk**

In `drivers/microtech/src/bgmeter_microtech/protocol.py`:

The module already imports `Callable` from `collections.abc`. Extend the signature:

```python
async def read_history(
    session: GattSession,
    characteristic: str,
    *,
    request_timeout: float = 5.0,
    retries: int = 3,
    progress: ProgressCallback | None = None,
    newest_count: int | None = None,
    is_known: Callable[[int], bool] | None = None,
) -> HistoryRecordCollector:
```

Add validation beside the existing guards at the top of the body:

```python
    if newest_count is not None and newest_count < 1:
        raise ValueError("newest_count must be at least 1")
```

Extend the opening log line's arguments to include `newest_count`:

```python
    _log.info(
        "history read started: characteristic=%s request_timeout=%.1fs retries=%d "
        "newest_count=%s",
        characteristic,
        request_timeout,
        retries,
        newest_count,
    )
```

Replace the walk inside the `try:` block — currently:

```python
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
```

with:

```python
        if found_latest:
            latest_index = collector.expected_count
            lowest_index = (
                1 if newest_count is None else max(1, latest_index - newest_count + 1)
            )
            targets: list[int] = []
            reason: str | None = None
            for event_index in range(latest_index, lowest_index - 1, -1):
                if is_known is not None and is_known(event_index):
                    reason = "already_stored"
                    break
                targets.append(event_index)
            else:
                if lowest_index > 1:
                    reason = "limit_reached"
            collector.set_target_indexes(targets)
            collector.truncation_reason = reason
            _log.info(
                "history walk targets %d..%d of %d (reason=%s)",
                lowest_index,
                latest_index,
                latest_index,
                reason,
            )
            for event_index in targets:
                if collector.has_index(event_index):
                    continue
                await request_until(
                    event_index,
                    lambda index=event_index: collector.has_index(index),
                )
```

The stop point is computed before any fetching, so the collector's status is correct even if the walk raises partway through. The `for ... else` sets `"limit_reached"` only when the loop ran to completion (no known record stopped it) *and* the limit actually excluded something.

The `if collector.is_complete: break` early exit is removed: the loop is now explicitly bounded by `targets`, so it is redundant.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest drivers/microtech/tests -q --ignore=drivers/microtech/tests/hardware`
Expected: PASS. If a test other than the two order assertions updated in Step 1 fails, stop and report — it means the walk change had an effect this plan did not anticipate.

- [ ] **Step 5: Commit**

```bash
git add drivers/microtech/src/bgmeter_microtech/protocol.py drivers/microtech/tests/test_protocol.py
git commit -m "feat(microtech): descend the index walk and stop at a limit or a known record"
```

---

### Task 5: Driver — honor the read options and filter known records

**Files:**
- Modify: `drivers/microtech/src/bgmeter_microtech/driver.py:100-145` (`_normalize_record`), `:236-330` (`read_records`)
- Test: `drivers/microtech/tests/test_driver.py`

**Interfaces:**
- Consumes: `ReadOptions.newest_count`, `ReadOptions.known_record_ids` (Task 1); `read_history(..., newest_count=, is_known=)`, `collector.truncation_reason`, `collector.missing_indexes` (Tasks 3–4).
- Produces: `history_record_id(device: MeterDevice, event_index: int) -> str`, a module-level function in `driver.py` returning `f"{MicroTechBgmDriver.driver_id}:{device.selector}:{event_index}"`. It is the single source of truth for record identity — both `_normalize_record` and the `is_known` closure call it.

- [ ] **Step 1: Write the failing tests**

Add to `drivers/microtech/tests/test_driver.py`, after `test_partial_read_reports_exact_missing_indexes_and_counts`:

```python
@pytest.mark.asyncio
async def test_newest_count_reads_only_the_most_recent_records() -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    device = _device(endpoint, driver)
    session = CaptureGattSession(endpoint, notifications=_capture_notifications())

    result = await driver.read_records(
        session, device, ReadOptions(timezone="UTC", newest_count=2)
    )

    assert [record.native_sequence for record in result.records] == [3, 4]
    assert result.completion is CompletionStatus.TRUNCATED
    assert result.expected_count == 4
    assert result.received_count == 2
    assert result.termination_reason == "limit_reached"
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_known_record_ids_stop_the_read_and_leave_the_result() -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    device = _device(endpoint, driver)
    session = CaptureGattSession(endpoint, notifications=_capture_notifications())
    known = frozenset(
        f"microtech-bgm:{device.selector}:{index}" for index in (1, 2, 3)
    )

    result = await driver.read_records(
        session, device, ReadOptions(timezone="UTC", known_record_ids=known)
    )

    assert [record.native_sequence for record in result.records] == [4]
    assert result.completion is CompletionStatus.TRUNCATED
    assert result.termination_reason == "already_stored"


@pytest.mark.asyncio
async def test_an_up_to_date_meter_returns_an_empty_result_not_an_error() -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    device = _device(endpoint, driver)
    session = CaptureGattSession(endpoint, notifications=_capture_notifications())
    known = frozenset(
        f"microtech-bgm:{device.selector}:{index}" for index in (1, 2, 3, 4)
    )

    result = await driver.read_records(
        session, device, ReadOptions(timezone="UTC", known_record_ids=known)
    )

    assert result.records == ()
    assert result.received_count == 0
    assert result.expected_count == 4
    assert result.completion is CompletionStatus.TRUNCATED
    assert result.termination_reason == "already_stored"


@pytest.mark.asyncio
async def test_history_record_id_matches_the_normalized_record_id() -> None:
    driver = driver_factory()
    endpoint = _endpoint()
    device = _device(endpoint, driver)
    session = CaptureGattSession(endpoint, notifications=_capture_notifications())

    result = await driver.read_records(session, device, ReadOptions(timezone="UTC"))

    assert [record.record_id for record in result.records] == [
        history_record_id(device, index) for index in (1, 2, 3, 4)
    ]
```

Add `history_record_id` to the `from bgmeter_microtech.driver import ...` line in the test module's imports. If the module currently imports only from `bgmeter_microtech`, add a new line:

```python
from bgmeter_microtech.driver import history_record_id
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest drivers/microtech/tests/test_driver.py -q -k "newest or known or up_to_date or history_record_id"`
Expected: FAIL — `ImportError: cannot import name 'history_record_id'`.

- [ ] **Step 3: Extract the record-id projection**

In `drivers/microtech/src/bgmeter_microtech/driver.py`, add above `_normalize_record`:

```python
def history_record_id(device: MeterDevice, event_index: int) -> str:
    """Return the public record identity for one MicroTech event index."""

    return f"{MicroTechBgmDriver.driver_id}:{device.selector}:{event_index}"
```

and in `_normalize_record`, replace the inline `record_id=` expression with:

```python
        record_id=history_record_id(device, native.event_index),
```

Add `"history_record_id"` to the module's `__all__`.

- [ ] **Step 4: Thread the options through the read**

In `read_records`, extend the opening log call's format string and arguments with `newest_count=%s` / `options.newest_count`, then replace the `read_history(...)` call with:

```python
            collector = await read_history(
                session,
                characteristic.uuid,
                request_timeout=options.request_timeout,
                retries=options.retries,
                progress=options.progress,
                newest_count=options.newest_count,
                is_known=(
                    (
                        lambda event_index: history_record_id(device, event_index)
                        in options.known_record_ids
                    )
                    if options.known_record_ids
                    else None
                ),
            )
```

- [ ] **Step 5: Filter known records and allow an empty result**

Still in `read_records`, replace everything from the `if not collector.records:` guard down to the `warnings = (...)` assignment with:

```python
        if not collector.records:
            raise MeterTimeoutError(
                "The meter connected but did not send any usable records."
            )

        captured = tuple(
            item
            for item in collector.records
            if history_record_id(device, item.native.event_index)
            not in options.known_record_ids
        )
        records = tuple(
            _normalize_record(
                item,
                device=device,
                timezone_name=options.timezone,
            )
            for item in captured
        )
        expected_count = collector.expected_count
        missing_indexes = collector.missing_indexes
        warnings = (
            (
                "Missing MicroTech history "
                + ("event index " if len(missing_indexes) == 1 else "event indexes ")
                + ", ".join(str(index) for index in missing_indexes)
            ),
        ) if missing_indexes else ()
```

The `MeterTimeoutError` guard now sits *before* the filter, so "the meter said nothing" still raises while "everything it said was already known" yields an empty result. The local `received_indexes` computation is gone — `collector.missing_indexes` (Task 3) replaces it.

- [ ] **Step 6: Report the truncation reason**

Replace the `termination_reason = (...)` expression with:

```python
        if cleanup_evidence is not None:
            termination_reason = "unsubscribe_failed"
        elif collector.status is CompletionStatus.TRUNCATED:
            termination_reason = collector.truncation_reason or "history_truncated"
        elif collector.status is CompletionStatus.COMPLETE:
            termination_reason = "history_complete"
        elif missing_indexes:
            termination_reason = "missing_records"
        else:
            termination_reason = "count_unknown"
```

Written as a statement rather than the previous nested conditional expression: mixing `or` with `if`/`else` in one expression reads ambiguously even though it parses correctly.

Also add `"microtech.truncation_reason": collector.truncation_reason` to the `diagnostics` mapping, beside `"microtech.highest_observed_index"`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest drivers/microtech/tests -q --ignore=drivers/microtech/tests/hardware`
Expected: PASS, including `test_no_usable_records_reports_one_plain_error_and_no_warning_events` (the `MeterTimeoutError` path is unchanged) and `test_package_root_exports_only_supported_driver_api`. If the export test fails, `history_record_id` belongs in `driver.py`'s `__all__` but must NOT be re-exported from `bgmeter_microtech/__init__.py`.

- [ ] **Step 8: Commit**

```bash
git add drivers/microtech/src/bgmeter_microtech/driver.py drivers/microtech/tests/test_driver.py
git commit -m "feat(microtech): honor read limits and drop records the caller already has"
```

---

### Task 6: Store — read what a device already has

**Files:**
- Modify: `cli/src/bgmeter_cli/store.py`
- Test: `cli/tests/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class StoredDeviceState:
    record_ids: frozenset[str]
    highest_sequence: int | None

class MeasurementStore:
    def device_state(self, *, driver_id: str, device_id: str) -> StoredDeviceState: ...
```

  Exported from `bgmeter_cli.store` via `__all__`.

- [ ] **Step 1: Write the failing tests**

Add to `cli/tests/test_store.py`, and extend the existing import block to pull in `StoredDeviceState`:

```python
def test_device_state_of_a_missing_database_is_empty_and_creates_nothing(tmp_path):
    database_path = tmp_path / "measurements.sqlite3"

    state = MeasurementStore(database_path).device_state(
        driver_id="fake", device_id="fake:meter-1"
    )

    assert state == StoredDeviceState(record_ids=frozenset(), highest_sequence=None)
    assert not database_path.exists()


def test_device_state_reports_stored_ids_and_highest_sequence(tmp_path, record):
    database_path = tmp_path / "measurements.sqlite3"
    store = MeasurementStore(database_path)
    store.store(
        (record, replace(record, record_id="fake:meter-1:9", native_sequence=9)),
        stored_at=STORED_AT,
    )

    state = store.device_state(driver_id="fake", device_id="fake:meter-1")

    assert state.record_ids == frozenset({"fake:meter-1:7", "fake:meter-1:9"})
    assert state.highest_sequence == 9


def test_device_state_is_scoped_to_one_driver_and_device(tmp_path, record):
    database_path = tmp_path / "measurements.sqlite3"
    store = MeasurementStore(database_path)
    store.store(
        (
            record,
            replace(
                record,
                record_id="fake:meter-2:42",
                native_sequence=42,
                source_device_id="fake:meter-2",
            ),
            replace(
                record,
                record_id="other:meter-1:99",
                native_sequence=99,
                source_driver_id="other",
            ),
        ),
        stored_at=STORED_AT,
    )

    state = store.device_state(driver_id="fake", device_id="fake:meter-1")

    assert state.record_ids == frozenset({"fake:meter-1:7"})
    assert state.highest_sequence == 7


def test_device_state_rejects_an_unsupported_schema_version(tmp_path):
    database_path = tmp_path / "measurements.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(StoreError, match="version 99"):
        MeasurementStore(database_path).device_state(
            driver_id="fake", device_id="fake:meter-1"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest cli/tests/test_store.py -q -k device_state`
Expected: FAIL — `ImportError: cannot import name 'StoredDeviceState'`.

- [ ] **Step 3: Implement `device_state`**

In `cli/src/bgmeter_cli/store.py`, add the dataclass beside `StoreSummary`:

```python
@dataclass(frozen=True, slots=True)
class StoredDeviceState:
    """What one database already holds for one meter."""

    record_ids: frozenset[str]
    highest_sequence: int | None
```

Add the method to `MeasurementStore`, after `store`:

```python
    def device_state(
        self,
        *,
        driver_id: str,
        device_id: str,
    ) -> StoredDeviceState:
        """Return the records already stored for one meter, without writing."""

        empty = StoredDeviceState(record_ids=frozenset(), highest_sequence=None)
        if not self._path.exists():
            return empty
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != _SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported measurement database version {version}"
                )
            rows = connection.execute(
                "SELECT record_id, native_sequence FROM measurements "
                "WHERE source_driver_id = ? AND source_device_id = ?",
                (driver_id, device_id),
            ).fetchall()
        except StoreError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise StoreError(f"cannot read measurements: {error}") from error
        finally:
            if connection is not None:
                connection.close()
        sequences = [row[1] for row in rows if row[1] is not None]
        return StoredDeviceState(
            record_ids=frozenset(row[0] for row in rows),
            highest_sequence=max(sequences) if sequences else None,
        )
```

This never creates the file, never creates the schema, and never triggers the legacy-path migration — all deliberate, per the spec.

Add `"StoredDeviceState"` to the module's `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest cli/tests/test_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/src/bgmeter_cli/store.py cli/tests/test_store.py
git commit -m "feat(cli): read what a meter already has stored"
```

---

### Task 7: CLI — `--newest` and `--new-only`

**Files:**
- Modify: `cli/src/bgmeter_cli/app.py` — the `read` subparser (`:124-142`), `_OUTCOME_LINES` (`:350-358`), `_run_meter_command` (`:362-414`)
- Test: `cli/tests/test_cli.py`

**Interfaces:**
- Consumes: `ReadOptions.newest_count`, `ReadOptions.known_record_ids`, `CompletionStatus.TRUNCATED` (Task 1); `MeasurementStore.device_state`, `StoredDeviceState` (Task 6).
- Produces: no API others depend on.

- [ ] **Step 1: Write the failing tests**

Add to `cli/tests/test_cli.py`, after `test_partial_read_stores_valid_records_before_returning_status_five`. These reuse the module's existing `complete_result` fixture, `make_device`, `FakeManager`, `FakeEntryPoint`, `install_entry_points` and `invoke` helpers:

```python
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
    config_path, database_path, managers, factory = _read_setup(
        tmp_path, monkeypatch, complete_result
    )
    MeasurementStore(database_path).store(complete_result.records)

    status, _, _ = invoke(
        ["read", "--device", "fake:meter-1", "--new-only"],
        config_path=config_path,
        database_path=database_path,
        manager_factory=factory,
    )

    assert status == 0
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
```

Add `MeasurementStore` to the existing `from bgmeter_cli.store import StoreError` line. `csv` and `json` are already imported by the module.

`complete_result`'s records have `source_driver_id="fake"` and `source_device_id="fake:meter-1"`, matching `make_device()`'s selector and driver id, which is what makes `device_state` find them.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest cli/tests/test_cli.py -q -k "newest or new_only or truncated"`
Expected: FAIL — `unrecognized arguments: --newest`.

- [ ] **Step 3: Add the flags**

In `cli/src/bgmeter_cli/app.py`, add a validator beside `_timezone_name`:

```python
def _positive_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        count = 0
    if count < 1:
        raise argparse.ArgumentTypeError(
            f"{value!r} must be a positive whole number"
        )
    return count
```

and register both flags on the `read` subparser, next to `--store`:

```python
    read.add_argument(
        "--newest",
        type=_positive_count,
        metavar="N",
        help="read only the N most recent records",
    )
    read.add_argument(
        "--new-only",
        action="store_true",
        help="stop at the first record already in the local database",
    )
```

`argparse` turns `--new-only` into `arguments.new_only`.

- [ ] **Step 4: Wire the options into the read**

In `_run_meter_command`, replace the block from `_log.info("reading %s", ...)` through the `result = await manager.read(...)` line with:

```python
    stored_state = None
    if arguments.new_only:
        try:
            stored_state = MeasurementStore(database_path).device_state(
                driver_id=device.driver_id,
                device_id=device.selector,
            )
        except StoreError as error:
            raise ExportError(f"cannot read stored measurements: {error}") from error
        _log.info(
            "device %s already holds %d record(s)",
            device.selector,
            len(stored_state.record_ids),
        )
    _log.info("reading %s", device.selector)
    result = await manager.read(
        device,
        ReadOptions(
            timezone=arguments.timezone,
            newest_count=arguments.newest,
            known_record_ids=(
                stored_state.record_ids if stored_state is not None else frozenset()
            ),
        ),
    )
```

and immediately after the existing `_log.info("read complete: ...")` call, add the reset guard:

```python
    if (
        stored_state is not None
        and stored_state.highest_sequence is not None
        and result.expected_count is not None
        and result.expected_count < stored_state.highest_sequence
    ):
        stderr.write(
            f"warning: the meter reports {result.expected_count} record(s) but the "
            f"database already holds {stored_state.highest_sequence}; its history "
            "appears to have been reset or cleared.\n"
        )
        stderr.write(
            "hint: run a full read (without --new-only) to capture this meter's "
            "current history.\n"
        )
```

- [ ] **Step 5: Make `TRUNCATED` a success**

Replace the closing block of `_run_meter_command`:

```python
    if result.completion in _OUTCOME_LINES:
        stderr.write(_OUTCOME_LINES[result.completion])
        return 5
    return 0
```

`_OUTCOME_LINES` itself is unchanged: it holds `PARTIAL` and `UNKNOWN` only, so `COMPLETE` and `TRUNCATED` both fall through to `return 0`. This also removes the `KeyError` that a new status would otherwise cause.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest cli/tests -q`
Expected: PASS, including the pre-existing `test_partial_read_stores_valid_records_before_returning_status_five` (still exit 5) and the `--store` tests.

- [ ] **Step 7: Commit**

```bash
git add cli/src/bgmeter_cli/app.py cli/tests/test_cli.py
git commit -m "feat(cli): add --newest and --new-only to bgmeter read"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`
- Test: none — documentation only, so no test run (per the repository's verification discipline).

**Interfaces:**
- Consumes: the finished behaviour of Tasks 1–7.
- Produces: nothing code depends on.

- [ ] **Step 1: Document the flags in `README.md`**

In the reading section (around the `bgmeter read --device ... --store` example at `README.md:117`), add after the `--store` paragraph:

````markdown
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
interrupted read — run a full read for that. It reads the database whether or
not `--store` is given, so it can preview what is new without recording it.
Both flags report `truncated` rather than `complete`, and exit 0.
````

- [ ] **Step 2: Document the status in `ARCHITECTURE.md`**

In the "Failure and completeness model" section, add `truncated` to the description of `CompletionStatus`: *the read delivered every record it was asked for, and that was deliberately fewer than the meter's whole history; `expected_count` still reports the meter's total.*

In the "Command-line interface" section, add `--newest N` and `--new-only` to the description of the `read` command, and note that `--new-only` reads `measurements.sqlite3` before the read to learn which records it can skip.

- [ ] **Step 3: Record the change in `CHANGELOG.md`**

Add under an `## [Unreleased]` heading (create it above the newest released version if it does not exist):

```markdown
### Added

- `bgmeter read --newest N` reads only the N most recent records.
- `bgmeter read --new-only` stops at the first record already in the local
  measurement database, making a repeated sync nearly free.
- `CompletionStatus.TRUNCATED` distinguishes a deliberately shortened read from
  a degraded one. `ReadOptions` gains `newest_count` and `known_record_ids`,
  both optional hints a driver may honor.

### Changed

- The MicroTech driver now walks history from the newest record backwards. An
  interrupted read therefore retains the newest records rather than the oldest.
```

- [ ] **Step 4: Commit**

```bash
git add README.md ARCHITECTURE.md CHANGELOG.md
git commit -m "docs: describe --newest, --new-only, and the truncated status"
```

---

## Verification

After Task 8, run all three suites once and report every failing test name from the full output:

```bash
python -m pytest core/tests -q
python -m pytest drivers/microtech/tests -q --ignore=drivers/microtech/tests/hardware
python -m pytest cli/tests -q
```

The hardware suite (`drivers/microtech/tests/hardware/test_live_meter.py`) needs a real meter and is not part of this verification.
