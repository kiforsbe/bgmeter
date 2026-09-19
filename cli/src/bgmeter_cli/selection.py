"""Device selection for interactive and non-interactive commands."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TextIO

from bgmeter import MeterDevice


class SelectionError(ValueError):
    """A meter selection is missing, invalid, or ambiguous."""


def select_device(
    devices: Sequence[MeterDevice],
    selector: str | None,
    *,
    stdin: TextIO,
    stderr: TextIO,
) -> MeterDevice:
    if selector is not None:
        matches = tuple(device for device in devices if device.selector == selector)
        if not matches:
            raise SelectionError(f"device {selector!r} was not discovered")
        if len(matches) != 1:
            raise SelectionError(f"device {selector!r} is ambiguous")
        return matches[0]

    if not stdin.isatty():
        raise SelectionError("--device is required for non-interactive use")
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise SelectionError("no meters are available for selection")

    stderr.write("Select a meter:\n")
    for index, device in enumerate(devices, start=1):
        label = device.identity.model or device.endpoint.name or "unknown model"
        stderr.write(f"  {index}) {device.selector} ({label}, {device.driver_id})\n")
    stderr.write("Selection: ")
    stderr.flush()
    answer = stdin.readline().strip()
    try:
        index = int(answer)
    except ValueError as error:
        raise SelectionError("selection must be a meter number") from error
    if not 1 <= index <= len(devices):
        raise SelectionError("selection is outside the available meter range")
    return devices[index - 1]


__all__ = ["SelectionError", "select_device"]
