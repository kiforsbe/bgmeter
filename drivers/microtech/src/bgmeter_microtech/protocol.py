"""Indexed MicroTech history retrieval over a connected GATT session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from bgmeter import GattSession

from .collector import HistoryRecordCollector
from .framing import build_history_request


async def read_history(
    session: GattSession,
    characteristic: str,
    *,
    request_timeout: float = 5.0,
    retries: int = 3,
) -> HistoryRecordCollector:
    """Read indexed history, stopping notification reception in all outcomes."""
    if request_timeout <= 0:
        raise ValueError("request_timeout must be positive")
    if retries <= 0:
        raise ValueError("retries must be positive")

    collector = HistoryRecordCollector(strict_live_mode=True)
    updated = asyncio.Event()

    def handle_notification(_sender: object, data: bytes) -> None:
        collector.add_notification(bytes(data))
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
            collector.register_request(event_index, request)
            await session.write_gatt_char(
                characteristic,
                request,
                response=False,
            )
            if await wait_until(predicate):
                return True
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
            collector.record_cleanup_failure("stop_notify", cleanup)
        finally:
            collector.finalize_pending()

    return collector
