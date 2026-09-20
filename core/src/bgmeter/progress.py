"""Plain-language progress reporting shared by managers and drivers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


_log = logging.getLogger(__name__)


class ProgressLevel(StrEnum):
    """How much verbosity a progress event needs before a frontend shows it."""

    INFO = "info"
    DETAIL = "detail"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One plain-language sentence about what a meter operation is doing."""

    level: ProgressLevel
    message: str
    current: int | None = None
    total: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


def emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    """Deliver an event; a missing or failing callback never affects the caller."""

    if callback is None:
        return
    try:
        callback(event)
    except Exception as error:
        _log.warning(
            "progress callback %r raised %s: %s; event dropped",
            callback,
            type(error).__name__,
            error,
        )


__all__ = ["ProgressCallback", "ProgressEvent", "ProgressLevel", "emit_progress"]
