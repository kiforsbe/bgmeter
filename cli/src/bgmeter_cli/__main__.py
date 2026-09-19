"""Module and console-script entry point."""

from __future__ import annotations

from collections.abc import Sequence

from .app import run


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
