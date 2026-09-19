#!/usr/bin/env python3
"""Deprecated compatibility entry point for the modular bgmeter CLI."""

from __future__ import annotations

import sys

from bgmeter_cli.__main__ import main as bgmeter_main


def main() -> int:
    print(
        "warning: gocheck.py is deprecated; use the 'bgmeter' command instead",
        file=sys.stderr,
    )
    return bgmeter_main()


if __name__ == "__main__":
    raise SystemExit(main())
