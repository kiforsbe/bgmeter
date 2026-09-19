"""Opt-in smoke test for a physically available supported meter."""

from __future__ import annotations

import os

import pytest

from bgmeter import MeterManager, ReadOptions


pytestmark = pytest.mark.skipif(
    os.environ.get("BGMETER_HARDWARE") != "1",
    reason="set BGMETER_HARDWARE=1 to run the live-meter smoke test",
)


@pytest.mark.asyncio
async def test_live_meter_can_probe_and_read_history() -> None:
    manager = MeterManager.default()
    devices = await manager.discover(timeout=15.0)
    device = next(item for item in devices if item.driver_id == "microtech-bgm")

    result = await manager.read(
        device,
        ReadOptions(timezone="Europe/Stockholm", request_timeout=5.0, retries=3),
    )

    assert result.records
    assert all(record.source_driver_id == "microtech-bgm" for record in result.records)
