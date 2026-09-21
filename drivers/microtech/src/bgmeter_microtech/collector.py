"""Fragment collection, strict live attribution, and wire evidence."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from bgmeter import CompletionStatus

from .framing import (
    TransportFragment,
    TransportFrameAssembler,
    decode_transport_fragment,
    reassemble_transport_fragments,
)
from .records import CapturedHistoryRecord, find_history_records

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingTransmission:
    request_generation: int
    fragments: dict[int, tuple[bytes, TransportFragment]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class _RegisteredRequest:
    event_index: int
    request: bytes
    generation: int
    token: bytes


@dataclass(frozen=True, slots=True)
class _RecordEvidence:
    offset: int
    event_index: int
    record: bytes
    attribution: str


@dataclass(frozen=True, slots=True)
class _TransmissionEvidence:
    fragments: tuple[bytes, ...]
    response: bytes | None
    transport_sequence: int | None
    started_after_request_generation: int
    active_requested_event_index: int | None
    matched_request_generation: int | None
    requested_event_index: int | None
    request: bytes | None
    response_token: bytes | None
    record_event_indexes: tuple[int, ...]
    records: tuple[_RecordEvidence, ...]
    attribution: str


class HistoryRecordCollector:
    """Collect history while separating accepted records from forensic evidence.

    Offline capture mode accepts every valid decoded record. Strict live mode accepts
    only a response whose token matches the request active when its first fragment
    arrived. A nonzero request additionally accepts only its requested event index.

    The six-bit transport sequence is not a generation identifier. If offset zero
    arrives after an incomplete transmission already has offset zero, the old
    generation is rejected and the new fragment starts a fresh generation. This
    conservative boundary prevents cross-generation assembly while still allowing
    arbitrary fragment order when offset zero has not yet arrived.
    """

    def __init__(self, *, strict_live_mode: bool = False) -> None:
        self.notification_count = 0
        self.complete_message_count = 0
        self.duplicate_transmission_count = 0
        self.identical_transmission_count = 0
        self.conflicting_transmission_count = 0
        self.invalid_notification_count = 0
        self.empty_response_count = 0
        self.rejected_transmission_count = 0
        self.highest_observed_index = 0
        self.expected_count: int | None = None
        self.retry_count = 0
        self.truncation_reason: str | None = None
        self._target_indexes: frozenset[int] | None = None
        self._cleanup_evidence: dict[str, str] | None = None
        self._strict_live_mode = strict_live_mode
        self._request_generation = 0
        self._requests: list[_RegisteredRequest] = []
        self._requests_by_generation: dict[int, _RegisteredRequest] = {}
        self._requests_by_token: dict[bytes, _RegisteredRequest] = {}
        self._notifications: list[bytes] = []
        self._responses: list[bytes] = []
        self._transmissions: list[_TransmissionEvidence] = []
        self._pending: dict[int, _PendingTransmission] = {}
        self._frames = TransportFrameAssembler()
        self._records: dict[int, CapturedHistoryRecord] = {}

    @property
    def notifications(self) -> tuple[bytes, ...]:
        return tuple(self._notifications)

    @property
    def responses(self) -> tuple[bytes, ...]:
        return tuple(self._responses)

    @property
    def records(self) -> tuple[CapturedHistoryRecord, ...]:
        return tuple(
            sorted(
                self._records.values(),
                key=lambda captured: (
                    captured.native.meter_datetime,
                    captured.native.event_index,
                ),
            )
        )

    @property
    def wire_evidence(self) -> dict[str, object]:
        """Return lossless JSON-ready wire evidence for public diagnostics."""
        return {
            "requests": tuple(
                {
                    "generation": request.generation,
                    "event_index": request.event_index,
                    "token": request.token,
                    "request": request.request,
                }
                for request in self._requests
            ),
            "notifications": self.notifications,
            "responses": self.responses,
            "transmissions": tuple(
                {
                    "fragments": transmission.fragments,
                    "response": transmission.response,
                    "transport_sequence": transmission.transport_sequence,
                    "started_after_request_generation": (
                        transmission.started_after_request_generation
                    ),
                    "active_requested_event_index": (
                        transmission.active_requested_event_index
                    ),
                    "matched_request_generation": (
                        transmission.matched_request_generation
                    ),
                    "requested_event_index": transmission.requested_event_index,
                    "request": transmission.request,
                    "response_token": transmission.response_token,
                    "record_event_indexes": transmission.record_event_indexes,
                    "records": tuple(
                        {
                            "offset": record.offset,
                            "event_index": record.event_index,
                            "record": record.record,
                            "attribution": record.attribution,
                        }
                        for record in transmission.records
                    ),
                    "attribution": transmission.attribution,
                }
                for transmission in self._transmissions
            ),
        }

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

    @property
    def cleanup_evidence(self) -> Mapping[str, str] | None:
        if self._cleanup_evidence is None:
            return None
        return MappingProxyType(self._cleanup_evidence)

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

    def record_cleanup_failure(self, operation: str, error: Exception) -> None:
        """Retain serializable evidence when post-read cleanup fails."""
        previous_completion = self.status
        self._cleanup_evidence = {
            "operation": operation,
            "error_type": type(error).__name__,
            "message": str(error),
            "previous_completion": previous_completion.value,
        }

    def has_index(self, event_index: int) -> bool:
        return event_index in self._records

    def set_target_indexes(self, indexes: Iterable[int]) -> None:
        """Declare which event indexes this read intends to fetch."""
        self._target_indexes = frozenset(indexes)

    def register_request(self, event_index: int, request: bytes) -> None:
        """Register one request write by the token echoed in its response."""
        raw_request = bytes(request)
        fragment = decode_transport_fragment(raw_request)
        if fragment.offset != 0 or not fragment.final or len(fragment.payload) < 6:
            raise ValueError("history request is not a complete transport message")

        self._request_generation += 1
        token = fragment.payload[4:6]
        registered = _RegisteredRequest(
            event_index=event_index,
            request=raw_request,
            generation=self._request_generation,
            token=token,
        )
        self._requests.append(registered)
        self._requests_by_generation[registered.generation] = registered
        self._requests_by_token[token] = registered

    def _append_transmission(
        self,
        *,
        fragments: tuple[bytes, ...],
        response: bytes | None,
        transport_sequence: int | None,
        started_after_request_generation: int,
        matched_request: _RegisteredRequest | None,
        response_token: bytes | None,
        records: tuple[_RecordEvidence, ...] = (),
        attribution: str,
    ) -> None:
        active_request = self._requests_by_generation.get(
            started_after_request_generation
        )
        self._transmissions.append(
            _TransmissionEvidence(
                fragments=tuple(bytes(fragment) for fragment in fragments),
                response=bytes(response) if response is not None else None,
                transport_sequence=transport_sequence,
                started_after_request_generation=started_after_request_generation,
                active_requested_event_index=(
                    active_request.event_index if active_request is not None else None
                ),
                matched_request_generation=(
                    matched_request.generation if matched_request is not None else None
                ),
                requested_event_index=(
                    matched_request.event_index if matched_request is not None else None
                ),
                request=(
                    matched_request.request if matched_request is not None else None
                ),
                response_token=(
                    bytes(response_token) if response_token is not None else None
                ),
                record_event_indexes=tuple(record.event_index for record in records),
                records=records,
                attribution=attribution,
            )
        )
        if attribution.startswith("rejected_") or attribution == "duplicate_conflict":
            self.rejected_transmission_count += 1
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

    def _reject_pending(self, sequence: int, attribution: str) -> None:
        pending = self._pending.pop(sequence)
        ordered = tuple(
            pending.fragments[offset][0] for offset in sorted(pending.fragments)
        )
        self._append_transmission(
            fragments=ordered,
            response=None,
            transport_sequence=sequence,
            started_after_request_generation=pending.request_generation,
            matched_request=None,
            response_token=None,
            attribution=attribution,
        )

    def finalize_pending(self) -> None:
        """Reject and preserve all incomplete frames and fragment generations."""
        for leftover in self._frames.flush():
            self._add_frame(leftover)
        for sequence in tuple(sorted(self._pending)):
            self._reject_pending(sequence, "rejected_incomplete_generation")

    def add_notification(
        self,
        data: bytes,
    ) -> tuple[CapturedHistoryRecord, ...]:
        """Consume one notification and return newly accepted unique records.

        One transport frame may span several notifications, so a notification can
        complete no frame, one frame, or more than one.
        """
        raw_notification = bytes(data)
        self.notification_count += 1
        self._notifications.append(raw_notification)
        added: list[CapturedHistoryRecord] = []
        for frame in self._frames.feed(raw_notification):
            added.extend(self._add_frame(frame))
        if self._frames.pending_byte_count:
            _log.debug(
                "notification of %d bytes left %d bytes of an incomplete frame buffered",
                len(raw_notification),
                self._frames.pending_byte_count,
            )
        return tuple(added)

    def _add_frame(self, raw_notification: bytes) -> tuple[CapturedHistoryRecord, ...]:
        """Consume one complete transport frame."""
        try:
            fragment = decode_transport_fragment(raw_notification)
        except ValueError:
            self.invalid_notification_count += 1
            self._append_transmission(
                fragments=(raw_notification,),
                response=None,
                transport_sequence=None,
                started_after_request_generation=self._request_generation,
                matched_request=None,
                response_token=None,
                attribution="rejected_invalid_notification",
            )
            return ()

        pending = self._pending.get(fragment.sequence)
        if (
            pending is not None
            and pending.request_generation != self._request_generation
        ):
            self._reject_pending(
                fragment.sequence,
                "rejected_superseded_request_generation",
            )
            pending = None
        if pending is not None and fragment.offset == 0 and 0 in pending.fragments:
            self._reject_pending(
                fragment.sequence,
                "rejected_superseded_incomplete_generation",
            )
            pending = None
        if pending is None:
            pending = _PendingTransmission(
                request_generation=self._request_generation
            )
            self._pending[fragment.sequence] = pending

        previous = pending.fragments.get(fragment.offset)
        if previous is not None:
            if previous[1].payload != fragment.payload:
                self.invalid_notification_count += 1
                self._append_transmission(
                    fragments=(raw_notification,),
                    response=None,
                    transport_sequence=fragment.sequence,
                    started_after_request_generation=pending.request_generation,
                    matched_request=None,
                    response_token=None,
                    attribution="rejected_conflicting_fragment",
                )
                return ()
        else:
            pending.fragments[fragment.offset] = (raw_notification, fragment)

        ordered = tuple(pending.fragments[offset] for offset in sorted(pending.fragments))
        try:
            response = reassemble_transport_fragments(
                item_fragment for _, item_fragment in ordered
            )
        except ValueError:
            self.invalid_notification_count += len(ordered)
            self._reject_pending(fragment.sequence, "rejected_malformed_transmission")
            return ()
        if response is None:
            return ()

        self._pending.pop(fragment.sequence, None)
        self.complete_message_count += 1
        self._responses.append(response)
        decoded = find_history_records(response)
        raw_fragments = tuple(raw for raw, _ in ordered)
        response_token = response[2:4] if len(response) >= 4 else None
        request_context = (
            self._requests_by_token.get(response_token)
            if response_token is not None
            else None
        )

        rejection: str | None = None
        if self._strict_live_mode:
            if pending.request_generation == 0:
                rejection = "rejected_pre_write"
            elif request_context is None:
                rejection = "rejected_unmatched_token"
            elif request_context.generation != pending.request_generation:
                rejection = "rejected_token_mismatch"

        if not decoded:
            self.empty_response_count += 1
            self._append_transmission(
                fragments=raw_fragments,
                response=response,
                transport_sequence=fragment.sequence,
                started_after_request_generation=pending.request_generation,
                matched_request=request_context,
                response_token=response_token,
                attribution=rejection or "rejected_empty_response",
            )
            return ()

        record_evidence: list[_RecordEvidence] = []
        added: list[CapturedHistoryRecord] = []
        accepted_indexes: list[int] = []
        for offset, native in decoded:
            raw_record = response[offset : offset + 18]
            attribution = rejection
            if (
                attribution is None
                and self._strict_live_mode
                and request_context is not None
                and request_context.event_index != 0
                and native.event_index != request_context.event_index
            ):
                attribution = "rejected_wrong_event"

            if attribution is None:
                existing = self._records.get(native.event_index)
                if existing is not None:
                    self.duplicate_transmission_count += 1
                    if existing.record == raw_record:
                        self.identical_transmission_count += 1
                        attribution = "duplicate_identical"
                    else:
                        self.conflicting_transmission_count += 1
                        attribution = "duplicate_conflict"
                else:
                    attribution = "accepted"
                    accepted_indexes.append(native.event_index)
                    captured = CapturedHistoryRecord(
                        native=native,
                        request=(
                            request_context.request
                            if request_context is not None
                            and request_context.generation
                            == pending.request_generation
                            else None
                        ),
                        fragments=raw_fragments,
                        response=response,
                        record=raw_record,
                        transport_sequence=fragment.sequence,
                    )
                    self._records[native.event_index] = captured
                    added.append(captured)

            record_evidence.append(
                _RecordEvidence(
                    offset=offset,
                    event_index=native.event_index,
                    record=raw_record,
                    attribution=attribution,
                )
            )

        if accepted_indexes:
            self.highest_observed_index = max(
                self.highest_observed_index,
                *accepted_indexes,
            )
            if not self._strict_live_mode:
                self.expected_count = self.highest_observed_index
            elif request_context is not None and request_context.event_index == 0:
                self.expected_count = max(accepted_indexes)

        attributions = {record.attribution for record in record_evidence}
        overall_attribution = (
            next(iter(attributions)) if len(attributions) == 1 else "mixed"
        )
        self._append_transmission(
            fragments=raw_fragments,
            response=response,
            transport_sequence=fragment.sequence,
            started_after_request_generation=pending.request_generation,
            matched_request=request_context,
            response_token=response_token,
            records=tuple(record_evidence),
            attribution=overall_attribution,
        )
        return tuple(added)
