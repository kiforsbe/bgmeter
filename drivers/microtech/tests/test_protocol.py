from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from pathlib import Path

import pytest

from bgmeter import CompletionStatus
from bgmeter_microtech.collector import HistoryRecordCollector
from bgmeter_microtech.framing import (
    TransportFrameAssembler,
    build_device_info_request,
    build_history_request,
    build_official_transport_frame,
    crc8_dallas,
    crc16_modbus,
    decode_transport_fragment,
    reassemble_transport_fragments,
)
from bgmeter_microtech.protocol import read_history
from bgmeter_microtech.records import decode_history_record


FIXTURE = Path(__file__).parent / "fixtures" / "2026-09-17-1700.txt"
NOTIFICATION_PATTERN = re.compile(r"^  Hex  : ([0-9a-f ]+)$")

RESPONSES = {
    0: (
        "2d 2d 00 00 10 00 00 4a 05 01 84 bf 63 7f 05 01 09 09 2d 2d",
        "2d 2d 00 00 10 0a 00 ad 11 11 1b 25 17 80 00 67 00 00 2d 2d",
        "2d 2d 00 00 0c 14 80 e0 00 04 03 00 00 00 2d 2d",
    ),
    1: (
        "2d 2d 00 00 10 00 01 14 05 01 45 7f 43 c2 05 01 1a 09 2d 2d",
        "2d 2d 00 00 10 0a 01 f3 10 0d 2b 28 18 80 00 62 00 00 2d 2d",
        "2d 2d 00 00 0c 14 81 be 00 01 03 00 00 00 2d 2d",
    ),
    2: (
        "2d 2d 00 00 10 00 03 a8 05 01 85 7c 12 bd 05 01 09 09 2d 2d",
        "2d 2d 00 00 10 0a 03 4f 10 13 0a 10 1b 80 00 86 00 00 2d 2d",
        "2d 2d 00 00 0c 14 83 02 00 02 03 00 00 00 2d 2d",
    ),
    3: (
        "2d 2d 00 00 10 00 02 f6 05 01 c4 be b6 a5 05 01 09 09 2d 2d",
        "2d 2d 00 00 10 0a 02 11 11 06 19 32 18 80 00 68 00 00 2d 2d",
        "2d 2d 00 00 0c 14 82 5c 00 03 03 00 00 00 2d 2d",
    ),
}

INDEX_TWO_RESPONSE = (
    "2d 2d 00 00 10 00 03 a8 05 01 05 7e b9 37 05 01 09 09 2d 2d",
    RESPONSES[2][1],
    RESPONSES[2][2],
)

WRONG_EVENT_THREE_FOR_REQUEST_TWO = (
    "2d 2d 00 00 10 00 02 f6 05 01 05 7e b9 37 05 01 09 09 2d 2d",
    RESPONSES[3][1],
    RESPONSES[3][2],
)


def _capture_notifications() -> list[bytes]:
    return [
        bytes.fromhex(match.group(1))
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if (match := NOTIFICATION_PATTERN.match(line))
    ]


def _frames(index: int) -> tuple[bytes, ...]:
    return tuple(bytes.fromhex(value) for value in RESPONSES[index])


def _indexed_frames(index: int) -> tuple[bytes, ...]:
    values = INDEX_TWO_RESPONSE if index == 2 else RESPONSES[index]
    return tuple(bytes.fromhex(value) for value in values)


class FakeGattSession:
    def __init__(
        self,
        attempts: dict[int, list[tuple[bytes, ...] | None]],
        *,
        notifications_on_start: tuple[tuple[bytes, ...], ...] = (),
        deferred_index_zero: tuple[tuple[bytes, ...], ...] = (),
    ) -> None:
        self.attempts = attempts
        self.notifications_on_start = notifications_on_start
        self.deferred_index_zero = deferred_index_zero
        self.attempt_counts: defaultdict[int, int] = defaultdict(int)
        self.handler = None
        self.events: list[str] = []
        self.writes: list[tuple[str, bytes, bool | None]] = []
        self._scheduled: list[asyncio.Handle] = []

    def _deliver(self, characteristic: str, transmission: tuple[bytes, ...]) -> None:
        if self.handler is None:
            return
        for frame in (transmission[2], transmission[0], transmission[1]):
            self.handler(characteristic, frame)

    def _deliver_deferred(self, characteristic: str, position: int) -> None:
        if self.handler is None:
            return
        self._deliver(characteristic, self.deferred_index_zero[position])
        next_position = position + 1
        if next_position < len(self.deferred_index_zero):
            self._scheduled.append(
                asyncio.get_running_loop().call_soon(
                    self._deliver_deferred,
                    characteristic,
                    next_position,
                )
            )

    async def start_notify(self, characteristic: str, callback) -> None:
        self.events.append(f"start:{characteristic}")
        self.handler = callback
        for transmission in self.notifications_on_start:
            self._deliver(characteristic, transmission)

    async def write_gatt_char(
        self,
        characteristic: str,
        data: bytes,
        *,
        response: bool | None = None,
    ) -> None:
        assert self.handler is not None, "read_history must subscribe before writing"
        index = int.from_bytes(data[-4:-2], "big")
        self.events.append(f"write:{index}")
        self.writes.append((characteristic, bytes(data), response))
        attempt = self.attempt_counts[index]
        self.attempt_counts[index] += 1
        if index == 0 and attempt == 0 and self.deferred_index_zero:
            self._scheduled.append(
                asyncio.get_running_loop().call_soon(
                    self._deliver_deferred,
                    characteristic,
                    0,
                )
            )
            return
        choices = self.attempts.get(index, [])
        transmission = choices[attempt] if attempt < len(choices) else None
        if transmission is not None:
            self._deliver(characteristic, transmission)

    async def stop_notify(self, characteristic: str) -> None:
        self.events.append(f"stop:{characteristic}")
        for handle in self._scheduled:
            handle.cancel()
        self.handler = None


def test_request_builders_preserve_official_wire_bytes() -> None:
    assert build_history_request(1).hex(" ") == (
        "2d 2d 00 00 10 00 80 c6 01 05 00 00 45 7f 02 01 00 01 2d 2d"
    )
    assert build_device_info_request().hex(" ") == (
        "2d 2d 00 00 0e 00 80 78 01 00 00 00 00 aa 02 00 2d 2d"
    )


def test_crc_and_escaping_are_protocol_exact() -> None:
    assert crc8_dallas(bytes.fromhex("00 00 10 00 80")) == 0xC6
    assert crc16_modbus(bytes.fromhex("01 05 00 00 02 01 00 01")) == 0x7F45

    frame = build_official_transport_frame(
        bytes.fromhex("2d 2f"),
        sequence=7,
        command=5,
    )

    assert b"\x2f\x2d" in frame[2:-2]
    assert b"\x2f\x2f" in frame[2:-2]
    fragment = decode_transport_fragment(frame)
    assert fragment.sequence == 7
    assert fragment.offset == 0
    assert fragment.final is True
    assert fragment.payload.endswith(bytes.fromhex("02 01 2d 2f"))


def test_transport_fragments_validate_crc_and_reassemble_in_any_order() -> None:
    frames = _frames(2)

    first = decode_transport_fragment(frames[0])
    assert first.offset == 0
    assert first.final is False
    assert first.sequence == 3
    assert first.payload == bytes.fromhex("05 01 85 7c 12 bd 05 01 09 09")
    assert reassemble_transport_fragments((frames[2], frames[0], frames[1])) == (
        bytes.fromhex(
            "05 01 85 7c 12 bd 05 01 09 09 "
            "10 13 0a 10 1b 80 00 86 00 00 "
            "00 02 03 00 00 00"
        )
    )

    corrupt = bytearray(frames[0])
    corrupt[7] ^= 0x01
    with pytest.raises(ValueError, match="CRC-8"):
        decode_transport_fragment(bytes(corrupt))


def test_native_record_uses_the_exact_big_endian_layout() -> None:
    raw = bytes.fromhex(
        "09 09 10 13 0a 10 1b 80 00 86 00 00 00 02 03 00 00 00"
    )

    record = decode_history_record(raw)

    assert record.meter_datetime.isoformat() == "2009-09-16T19:10:16"
    assert record.temperature_c == 27
    assert record.flags == 0x80
    assert record.glucose_mg_dl == 134
    assert record.reserved == 0
    assert record.event_index == 2
    assert record.event_port == 3
    assert record.event_type == 0
    assert record.event_level == 0
    assert record.event_value == 0


def test_capture_reassembles_deduplicates_and_preserves_event_two_bytes() -> None:
    collector = HistoryRecordCollector()
    notifications = _capture_notifications()
    delivered = []
    assert len(notifications) == 96

    for start in range(0, len(notifications), 3):
        group = notifications[start : start + 3]
        assert len(group) == 3
        for frame in (group[2], group[0], group[1]):
            delivered.append(frame)
            collector.add_notification(frame)

    by_index = {record.native.event_index: record for record in collector.records}
    assert collector.notification_count == 96
    assert collector.complete_message_count == 32
    assert collector.duplicate_transmission_count == 8
    assert collector.identical_transmission_count == 8
    assert collector.conflicting_transmission_count == 0
    assert collector.highest_observed_index == 4
    assert collector.expected_count == 4
    assert collector.status is CompletionStatus.COMPLETE
    assert sorted(by_index) == [1, 2, 3, 4]
    assert {
        index: record.native.glucose_mg_dl for index, record in by_index.items()
    } == {1: 98, 2: 134, 3: 104, 4: 103}

    target = by_index[2]
    assert target.request is None
    assert target.fragments == _frames(2)
    assert target.response == bytes.fromhex(
        "05 01 85 7c 12 bd 05 01 09 09 "
        "10 13 0a 10 1b 80 00 86 00 00 "
        "00 02 03 00 00 00"
    )
    assert target.record == bytes.fromhex(
        "09 09 10 13 0a 10 1b 80 00 86 00 00 00 02 03 00 00 00"
    )
    evidence = collector.wire_evidence
    assert evidence["notifications"] == tuple(delivered)
    assert evidence["responses"] == tuple(
        transmission["response"] for transmission in evidence["transmissions"]
        if transmission["response"] is not None
    )
    assert len(evidence["transmissions"]) == 32


def test_collector_keeps_first_payload_for_conflicting_event_retry() -> None:
    collector = HistoryRecordCollector()
    original = _frames(2)
    conflict = tuple(
        bytes.fromhex(value)
        for value in (
            RESPONSES[2][0],
            "2d 2d 00 00 10 0a 03 4f 10 13 0a 11 1b 80 00 86 00 00 2d 2d",
            RESPONSES[2][2],
        )
    )

    for transmission in (original, original, conflict):
        for frame in transmission:
            collector.add_notification(frame)

    stored = collector.records[0]
    assert stored.native.meter_datetime.isoformat() == "2009-09-16T19:10:16"
    assert stored.fragments == original
    assert collector.duplicate_transmission_count == 2
    assert collector.identical_transmission_count == 1
    assert collector.conflicting_transmission_count == 1
    assert [
        transmission["attribution"]
        for transmission in collector.wire_evidence["transmissions"]
    ] == ["accepted", "duplicate_identical", "duplicate_conflict"]
    assert tuple(
        transmission["fragments"]
        for transmission in collector.wire_evidence["transmissions"]
    ) == (original, original, conflict)


@pytest.mark.asyncio
async def test_read_history_queries_zero_then_missing_indexes_and_stops(
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = {index: [_indexed_frames(index)] for index in range(4)}
    session = FakeGattSession(attempts)

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    assert requested == [0, 1, 2, 3]
    assert all(characteristic == "ffe1" for characteristic, _, _ in session.writes)
    assert all(response is False for _, _, response in session.writes)
    assert session.events[0] == "start:ffe1"
    assert session.events[-1] == "stop:ffe1"
    assert collector.status is CompletionStatus.COMPLETE
    assert collector.retry_count == 0
    assert {record.native.event_index for record in collector.records} == {1, 2, 3, 4}
    assert next(
        record for record in collector.records if record.native.event_index == 4
    ).request == build_history_request(0)
    assert next(
        record for record in collector.records if record.native.event_index == 2
    ).request == build_history_request(2)
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
async def test_prewrite_record_cannot_establish_count_or_inherit_request() -> None:
    session = FakeGattSession(
        {
            1: [_indexed_frames(1)],
            2: [_indexed_frames(2)],
            3: [_indexed_frames(3)],
        },
        notifications_on_start=(_frames(1),),
        deferred_index_zero=(_frames(0),),
    )

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    by_index = {record.native.event_index: record for record in collector.records}
    assert requested == [0, 1, 2, 3]
    assert collector.highest_observed_index == 4
    assert collector.expected_count == 4
    assert collector.status is CompletionStatus.COMPLETE
    assert by_index[1].request == build_history_request(1)
    assert by_index[4].request == build_history_request(0)
    assert any(
        transmission["attribution"] == "rejected_pre_write"
        for transmission in collector.wire_evidence["transmissions"]
    )
    assert session.events[-1] == "stop:ffe1"


@pytest.mark.asyncio
async def test_reordered_complete_message_cannot_impersonate_latest_response() -> None:
    session = FakeGattSession(
        {
            1: [_indexed_frames(1)],
            2: [_indexed_frames(2)],
            3: [_indexed_frames(3)],
        },
        deferred_index_zero=(_frames(1), _frames(0)),
    )

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    by_index = {record.native.event_index: record for record in collector.records}
    assert requested == [0, 1, 2, 3]
    assert collector.highest_observed_index == 4
    assert collector.expected_count == 4
    assert collector.status is CompletionStatus.COMPLETE
    assert by_index[1].request == build_history_request(1)
    assert by_index[4].request == build_history_request(0)
    assert any(
        transmission["attribution"] == "rejected_unmatched_token"
        for transmission in collector.wire_evidence["transmissions"]
    )
    assert session.events[-1] == "stop:ffe1"


@pytest.mark.asyncio
async def test_nonzero_request_rejects_a_response_for_the_wrong_event() -> None:
    session = FakeGattSession(
        {
            0: [_indexed_frames(0)],
            1: [_indexed_frames(1)],
            2: [
                tuple(bytes.fromhex(value) for value in WRONG_EVENT_THREE_FOR_REQUEST_TWO),
                _indexed_frames(2),
            ],
            3: [_indexed_frames(3)],
        }
    )

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.001,
        retries=2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    assert requested == [0, 1, 2, 2, 3]
    assert collector.status is CompletionStatus.COMPLETE
    by_index = {record.native.event_index: record for record in collector.records}
    assert by_index[3].request == build_history_request(3)
    mismatch = next(
        transmission
        for transmission in collector.wire_evidence["transmissions"]
        if transmission["attribution"] == "rejected_wrong_event"
    )
    assert mismatch["requested_event_index"] == 2
    assert mismatch["record_event_indexes"] == (3,)
    assert tuple(
        request["event_index"] for request in collector.wire_evidence["requests"]
    ) == (0, 1, 2, 2, 3)


def test_response_token_for_prior_request_cannot_match_active_request() -> None:
    collector = HistoryRecordCollector(strict_live_mode=True)
    collector.register_request(0, build_history_request(0))
    for fragment in _frames(0):
        collector.add_notification(fragment)

    collector.register_request(1, build_history_request(1))
    for fragment in _frames(0):
        collector.add_notification(fragment)

    assert {record.native.event_index for record in collector.records} == {4}
    assert collector.duplicate_transmission_count == 0
    mismatch = collector.wire_evidence["transmissions"][-1]
    assert mismatch["attribution"] == "rejected_token_mismatch"
    assert mismatch["active_requested_event_index"] == 1
    assert mismatch["requested_event_index"] == 0


def test_new_offset_zero_fragment_discards_incomplete_same_sequence_generation() -> None:
    collector = HistoryRecordCollector()
    original = _frames(2)
    new_generation = _indexed_frames(2)

    for fragment in (
        original[0],
        original[1],
        new_generation[0],
        new_generation[2],
    ):
        collector.add_notification(fragment)

    assert collector.complete_message_count == 0
    assert collector.records == ()
    collector.finalize_pending()
    assert [
        transmission["attribution"]
        for transmission in collector.wire_evidence["transmissions"]
    ] == [
        "rejected_superseded_incomplete_generation",
        "rejected_incomplete_generation",
    ]
    assert collector.wire_evidence["transmissions"][0]["fragments"] == original[:2]


def test_new_request_never_combines_pending_same_sequence_fragments() -> None:
    collector = HistoryRecordCollector(strict_live_mode=True)
    request = build_history_request(2)
    frames = _indexed_frames(2)
    collector.register_request(2, request)
    collector.add_notification(frames[0])

    collector.register_request(2, request)
    collector.add_notification(frames[1])
    collector.add_notification(frames[2])

    assert collector.complete_message_count == 0
    collector.finalize_pending()
    assert [
        transmission["attribution"]
        for transmission in collector.wire_evidence["transmissions"]
    ] == [
        "rejected_superseded_request_generation",
        "rejected_incomplete_generation",
    ]


@pytest.mark.asyncio
async def test_read_history_retries_a_missing_index_and_returns_partial() -> None:
    attempts = {
        0: [_frames(0)],
        1: [_frames(1)],
        2: [None, None],
        3: [_frames(3)],
    }
    session = FakeGattSession(attempts)

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.001,
        retries=2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    assert requested == [0, 1, 2, 2, 3]
    assert collector.status is CompletionStatus.PARTIAL
    assert collector.retry_count == 1
    assert {record.native.event_index for record in collector.records} == {1, 3, 4}
    assert session.events[-1] == "stop:ffe1"


@pytest.mark.asyncio
async def test_read_history_stops_notifications_after_latest_request_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeGattSession({0: [None, None]})

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.001,
        retries=2,
    )

    requested = [int.from_bytes(data[-4:-2], "big") for _, data, _ in session.writes]
    assert requested == [0, 0]
    assert collector.status is CompletionStatus.UNKNOWN
    assert collector.retry_count == 1
    assert collector.records == ()
    assert session.events == ["start:ffe1", "write:0", "write:0", "stop:ffe1"]
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
async def test_unsubscribe_failure_returns_partial_collector_and_finalizes_evidence(
) -> None:
    class FailingStopSession(FakeGattSession):
        async def stop_notify(self, characteristic: str) -> None:
            assert self.handler is not None
            self.handler(characteristic, _frames(1)[0])
            await super().stop_notify(characteristic)
            raise RuntimeError("unsubscribe failed")

    session = FailingStopSession(
        {index: [_indexed_frames(index)] for index in range(4)}
    )

    collector = await read_history(
        session,
        "ffe1",
        request_timeout=0.01,
        retries=1,
    )

    assert {record.native.event_index for record in collector.records} == {1, 2, 3, 4}
    assert collector.status is CompletionStatus.PARTIAL
    assert collector.cleanup_evidence == {
        "operation": "stop_notify",
        "error_type": "RuntimeError",
        "message": "unsubscribe failed",
        "previous_completion": "complete",
    }
    assert collector.wire_evidence["transmissions"][-1]["attribution"] == (
        "rejected_incomplete_generation"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_type", [RuntimeError, asyncio.CancelledError])
async def test_primary_failure_survives_unsubscribe_failure(primary_type) -> None:
    primary = primary_type("primary failure")

    class FailingSession(FakeGattSession):
        async def write_gatt_char(
            self,
            characteristic: str,
            data: bytes,
            *,
            response: bool | None = None,
        ) -> None:
            raise primary

        async def stop_notify(self, characteristic: str) -> None:
            raise RuntimeError("unsubscribe failed")

    with pytest.raises(primary_type, match="primary failure") as caught:
        await read_history(
            FailingSession({}),
            "ffe1",
            request_timeout=0.01,
            retries=1,
        )

    assert "unsubscribe failed" in caught.value.__notes__[-1]


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


# A record whose seconds field is 0x2F. Escaping that byte grows the middle frame of
# the reply from 20 to 21 bytes, which the meter sends as two notifications.
ESCAPED_SECONDS_RECORD = bytes.fromhex("1a 09 14 07 1f 2f 16 80 00 64 00 00 00 0d 03 00 00 00")


def _encode_frame(offset: int, final: bool, sequence: int, payload: bytes) -> bytes:
    header = bytes((0, 0, 6 + len(payload), offset, (0x80 if final else 0) | sequence))
    body = header + bytes((crc8_dallas(header),)) + payload
    escaped = bytearray()
    for value in body:
        if value in (0x2D, 0x2F):
            escaped.append(0x2F)
        escaped.append(value)
    return b"\x2d\x2d" + bytes(escaped) + b"\x2d\x2d"


def _escaped_seconds_reply() -> tuple[bytes, bytes, bytes]:
    record = ESCAPED_SECONDS_RECORD
    return (
        _encode_frame(0, False, 0, bytes.fromhex("05 01 84 bf 63 7f 05 01") + record[:2]),
        _encode_frame(10, False, 0, record[2:12]),
        _encode_frame(20, True, 0, record[12:]),
    )


def _split_reply_notifications() -> tuple[bytes, ...]:
    first, middle, last = _escaped_seconds_reply()
    assert len(middle) == 21
    return first, middle[:20], middle[20:], last


class SplitReplySession(FakeGattSession):
    """Deliver each scripted piece as its own notification, in the order given."""

    def _deliver(self, characteristic: str, transmission: tuple[bytes, ...]) -> None:
        if self.handler is None:
            return
        for piece in transmission:
            self.handler(characteristic, piece)


def test_frame_assembler_passes_whole_frames_through() -> None:
    assembler = TransportFrameAssembler()

    for frame in _frames(2):
        assert assembler.feed(frame) == (frame,)
    assert assembler.pending_byte_count == 0


def test_frame_assembler_joins_a_frame_split_across_notifications() -> None:
    _, middle, _ = _escaped_seconds_reply()
    assembler = TransportFrameAssembler()

    assert assembler.feed(middle[:20]) == ()
    assert assembler.pending_byte_count == 20
    assert assembler.feed(middle[20:]) == (middle,)
    assert assembler.pending_byte_count == 0


@pytest.mark.parametrize("tail", [1, 2, 5])
def test_frame_assembler_joins_a_split_at_any_tail_length(tail: int) -> None:
    _, middle, _ = _escaped_seconds_reply()
    assembler = TransportFrameAssembler()

    assert assembler.feed(middle[:-tail]) == ()
    assert assembler.feed(middle[-tail:]) == (middle,)


def test_frame_assembler_joins_a_split_between_an_escape_and_its_byte() -> None:
    _, middle, _ = _escaped_seconds_reply()
    escape_at = middle.index(b"\x2f\x2f", 2)
    assembler = TransportFrameAssembler()

    assert assembler.feed(middle[: escape_at + 1]) == ()
    assert assembler.feed(middle[escape_at + 1 :]) == (middle,)


def test_frame_assembler_joins_a_frame_split_into_many_pieces() -> None:
    _, middle, _ = _escaped_seconds_reply()
    assembler = TransportFrameAssembler()

    results = [assembler.feed(middle[index : index + 1]) for index in range(len(middle))]

    assert results[:-1] == [()] * (len(middle) - 1)
    assert results[-1] == (middle,)


def test_frame_assembler_splits_frames_that_share_one_notification() -> None:
    first, _, last = _escaped_seconds_reply()

    assert TransportFrameAssembler().feed(first + last) == (first, last)


@pytest.mark.parametrize("kept", [15, 20])
def test_frame_assembler_does_not_let_a_truncated_frame_swallow_the_next(
    kept: int,
) -> None:
    first, middle, _ = _escaped_seconds_reply()
    assembler = TransportFrameAssembler()

    assert assembler.feed(middle[:kept]) == ()
    assert assembler.feed(first) == (middle[:kept], first)
    assert assembler.pending_byte_count == 0


def test_frame_assembler_takes_a_bare_delimiter_as_the_closing_bytes() -> None:
    _, middle, _ = _escaped_seconds_reply()
    assembler = TransportFrameAssembler()

    assert assembler.feed(middle[:-2]) == ()
    assert assembler.feed(b"\x2d\x2d") == (middle,)


def test_frame_assembler_returns_bytes_that_are_not_a_frame() -> None:
    assembler = TransportFrameAssembler()

    assert assembler.feed(b"\x99\x98") == (b"\x99\x98",)
    assert assembler.feed(b"\x2d") == ()
    assert assembler.feed(b"\x00\x01") == (b"\x2d\x00\x01",)


def test_frame_assembler_flush_returns_the_incomplete_leftover_once() -> None:
    _, middle, _ = _escaped_seconds_reply()
    assembler = TransportFrameAssembler()
    assembler.feed(middle[:20])

    assert assembler.flush() == (middle[:20],)
    assert assembler.flush() == ()


def test_collector_accepts_a_reply_whose_frame_arrives_in_two_notifications() -> None:
    collector = HistoryRecordCollector()

    added = [
        record
        for notification in _split_reply_notifications()
        for record in collector.add_notification(notification)
    ]

    assert [record.native.event_index for record in added] == [13]
    assert added[0].native.meter_datetime.second == 0x2F
    assert len(added[0].fragments) == 3 and len(added[0].fragments[1]) == 21
    assert collector.notification_count == 4
    assert collector.complete_message_count == 1
    assert collector.invalid_notification_count == 0
    assert collector.attributions_since(0) == ("accepted",)


def test_collector_records_an_unfinished_frame_as_invalid_when_finalized() -> None:
    collector = HistoryRecordCollector()
    _, middle, _ = _escaped_seconds_reply()

    collector.add_notification(middle[:20])
    assert collector.attributions_since(0) == ()
    collector.finalize_pending()

    assert collector.invalid_notification_count == 1
    assert collector.attributions_since(0) == ("rejected_invalid_notification",)


@pytest.mark.asyncio
async def test_read_history_recovers_the_latest_record_from_a_split_frame() -> None:
    events = []
    session = SplitReplySession({0: [_split_reply_notifications()]})

    collector = await read_history(
        session, "ffe1", request_timeout=0.001, retries=1, progress=events.append
    )

    assert collector.expected_count == 13
    assert collector.has_index(13)
    assert collector.invalid_notification_count == 0
    assert ("info", "Reading records: 1 of 13") in _progress_summary(events)
    assert not any("damaged" in event.message for event in events)
