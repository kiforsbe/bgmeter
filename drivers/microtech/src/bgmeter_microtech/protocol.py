"""Indexed MicroTech history retrieval over a connected GATT session."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable

from bgmeter import (
    GattSession,
    ProgressCallback,
    ProgressEvent,
    ProgressLevel,
    emit_progress,
)

from .collector import HistoryRecordCollector
from .framing import build_history_request


_MISMATCHED_REPLY = "a reply that did not match our request"
_DAMAGED_REPLY = "a damaged reply"
_REJECTION_REASONS = {
    "rejected_unmatched_token": _MISMATCHED_REPLY,
    "rejected_token_mismatch": _MISMATCHED_REPLY,
    "rejected_pre_write": _MISMATCHED_REPLY,
    "rejected_wrong_event": "a reply for a different record than the one we asked for",
    "rejected_empty_response": "a reply that contained no record",
    "rejected_invalid_notification": _DAMAGED_REPLY,
    "rejected_conflicting_fragment": _DAMAGED_REPLY,
    "rejected_malformed_transmission": _DAMAGED_REPLY,
    "duplicate_conflict": "a reply that disagreed with a record we already had",
}

_log = logging.getLogger(__name__)


def _rejection_reason(attribution: str) -> str | None:
    """Plain-language reason for a collector verdict, or None if it is not a rejection."""
    reason = _REJECTION_REASONS.get(attribution)
    if reason is None and attribution.startswith("rejected_"):
        return "an incomplete reply"
    return reason


def _tries(count: int) -> str:
    return f"{count} {'try' if count == 1 else 'tries'}"


async def read_history(
    session: GattSession,
    characteristic: str,
    *,
    request_timeout: float = 5.0,
    retries: int = 3,
    progress: ProgressCallback | None = None,
) -> HistoryRecordCollector:
    """Read indexed history, stopping notification reception in all outcomes."""
    if request_timeout <= 0:
        raise ValueError("request_timeout must be positive")
    if retries <= 0:
        raise ValueError("retries must be positive")

    _log.info(
        "history read started: characteristic=%s request_timeout=%.1fs retries=%d",
        characteristic,
        request_timeout,
        retries,
    )
    collector =HistoryRecordCollector(strict_live_mode=True)
    updated = asyncio.Event()

    def say(
        level: ProgressLevel,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        emit_progress(progress, ProgressEvent(level, message, current, total))

    def handle_notification(_sender: object, data: bytes) -> None:
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug("notification %d bytes: %s", len(data), bytes(data).hex(" "))
        start =collector.transmission_count
        added = collector.add_notification(bytes(data))
        for attribution in collector.attributions_since(start):
            reason = _rejection_reason(attribution)
            if reason is not None:
                say(ProgressLevel.DETAIL, f"Ignored {reason}.")
        if added:
            current, total = collector.record_count, collector.expected_count
            say(
                ProgressLevel.INFO,
                f"Reading records: {current} of {total}"
                if total is not None
                else f"Reading records: {current}",
                current=current,
                total=total,
            )
        updated.set()

    async def wait_until(predicate: Callable[[], bool]) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request_timeout
        while not predicate():
            updated.clear()
            if predicate():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(updated.wait(), timeout=remaining)
            except TimeoutError:
                return predicate()
        return True

    async def request_until(
        event_index: int,
        predicate: Callable[[], bool],
    ) -> bool:
        request = build_history_request(event_index)
        for attempt in range(retries):
            if attempt:
                collector.retry_count += 1
                say(
                    ProgressLevel.INFO,
                    "The meter did not send a usable reply; "
                    f"trying again ({attempt + 1} of {retries}).",
                )
            elif event_index == 0:
                say(ProgressLevel.DETAIL, "Asking the meter how many records it holds.")
            collector.register_request(event_index, request)
            _log.debug(
                "request event_index=%d attempt=%d/%d bytes: %s",
                event_index,
                attempt + 1,
                retries,
                request.hex(" "),
            )
            await session.write_gatt_char(
                characteristic,
                request,
                response=False,
            )
            if await wait_until(predicate):
                return True
        _log.warning(
            "request event_index=%d unanswered after %d attempt(s) (timeout %.1fs each)",
            event_index,
            retries,
            request_timeout,
        )
        if event_index == 0:
            say(
                ProgressLevel.DETAIL,
                f"Gave up asking the meter for its latest record after {_tries(retries)}.",
            )
        else:
            say(
                ProgressLevel.DETAIL,
                f"Gave up on record {event_index} after {_tries(retries)} and moved on.",
            )
        return False

    await session.start_notify(characteristic, handle_notification)
    try:
        found_latest = await request_until(
            0,
            lambda: collector.expected_count is not None,
        )
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
    except BaseException as primary:
        try:
            await session.stop_notify(characteristic)
        except BaseException as cleanup:
            primary.add_note(
                "MicroTech notification cleanup also failed: "
                f"{type(cleanup).__name__}: {cleanup}"
            )
        finally:
            collector.finalize_pending()
        raise
    else:
        try:
            await session.stop_notify(characteristic)
        except asyncio.CancelledError:
            raise
        except Exception as cleanup:
            _log.error(
                "stop_notify failed: %s: %s", type(cleanup).__name__, cleanup
            )
            collector.record_cleanup_failure("stop_notify", cleanup)
        finally:
            collector.finalize_pending()

    _log.info(
        "history read finished: records=%d expected=%s notifications=%d "
        "complete_messages=%d retries=%d rejected=%d duplicates=%d status=%s",
        collector.record_count,
        collector.expected_count,
        collector.notification_count,
        collector.complete_message_count,
        collector.retry_count,
        collector.rejected_transmission_count,
        collector.duplicate_transmission_count,
        collector.status.value,
    )
    rejected = Counter(
        attribution
        for attribution in collector.attributions_since(0)
        if attribution.startswith("rejected_") or attribution == "duplicate_conflict"
    )
    if rejected:
        _log.warning(
            "history read rejected %d transmission(s): %s",
            sum(rejected.values()),
            dict(sorted(rejected.items())),
        )
    return collector
