"""Log sinks for the CLI: the terminal by default, or a file exclusively."""

from __future__ import annotations

import logging
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TextIO


LOG_LEVELS = ("debug", "info", "warning", "error")
DEFAULT_LOG_LEVEL = "error"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DISTRIBUTIONS = ("bgmeter-cli", "bgmeter-core", "bgmeter-microtech")


class LogFileError(OSError):
    """The requested log file cannot be opened."""


class _IsoFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return (
            datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds")
        )


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unknown"


def _header(command: str) -> str:
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    versions = " ".join(f"{name} {_version(name)}" for name in _DISTRIBUTIONS)
    return (
        f"{stamp} {versions} python {platform.python_version()} "
        f"platform {platform.platform()} command={command}"
    )


@contextmanager
def configured_logging(
    level: str,
    *,
    log_file: Path | None,
    stream: TextIO,
    command: str,
) -> Iterator[None]:
    """Attach one root-logger sink at ``level``; always restore the logger."""

    numeric = getattr(logging, level.upper())
    if log_file is not None:
        try:
            handler: logging.Handler = logging.FileHandler(
                log_file, mode="a", encoding="utf-8"
            )
        except OSError as error:
            raise LogFileError(f"cannot open log file {log_file}: {error}") from error
        handler.stream.write(_header(command) + "\n")
        handler.stream.flush()
    else:
        handler = logging.StreamHandler(stream)
    handler.setLevel(numeric)
    handler.setFormatter(_IsoFormatter(_FORMAT))

    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(numeric)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        handler.close()


__all__ = [
    "DEFAULT_LOG_LEVEL",
    "LOG_LEVELS",
    "LogFileError",
    "configured_logging",
]
