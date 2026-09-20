"""Plain-language progress output for the terminal."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TextIO

from bgmeter import ProgressEvent, ProgressLevel


_MAX_VERBOSITY = 2
_VERBOSITY_TO_SHOW = {
    ProgressLevel.WARNING: 0,
    ProgressLevel.INFO: 1,
    ProgressLevel.DETAIL: 2,
}


class ConsoleReporter:
    """Write progress events to a stream according to the ``-v`` level."""

    def __init__(
        self,
        stream: TextIO,
        verbosity: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream
        self._verbosity = max(0, min(verbosity, _MAX_VERBOSITY))
        self._clock = clock
        self._started = clock()
        self._last_counter: int | None = None

    def __call__(self, event: ProgressEvent) -> None:
        if self._verbosity < _VERBOSITY_TO_SHOW[event.level]:
            return
        if not self._counter_is_due(event):
            return
        if event.level is ProgressLevel.WARNING:
            line = f"warning: {event.message}"
        elif self._verbosity >= _MAX_VERBOSITY:
            line = f"[{self._clock() - self._started:4.1f}s] {event.message}"
        else:
            line = event.message
        self._stream.write(line + "\n")

    def _counter_is_due(self, event: ProgressEvent) -> bool:
        """Print the first and last update and each 10% step in between."""
        if event.current is None or event.total is None:
            return True
        last = self._last_counter
        due = (
            last is None
            or event.current >= event.total
            or event.current - last >= event.total / 10
        )
        if due:
            self._last_counter = event.current
        return due


__all__ = ["ConsoleReporter"]
