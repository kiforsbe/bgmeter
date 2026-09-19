from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_legacy_gocheck_launcher_delegates_help_and_warns() -> None:
    """Removing delegation must lose the CLI help and deprecation warning."""
    try:
        result = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "gocheck.py"), "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result = None

    assert result is not None, "legacy script did not delegate promptly to the CLI"
    assert result.returncode == 0, result.stderr
    assert "usage: bgmeter" in result.stdout
    assert "deprecated" in result.stderr.casefold()
    assert "bgmeter" in result.stderr.casefold()


def test_legacy_gocheck_launcher_preserves_cli_failure_status() -> None:
    """Replacing the delegated status with success must break compatibility."""
    result = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "gocheck.py"), "not-a-command"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 2
    assert "deprecated" in result.stderr.casefold()
    assert "invalid choice" in result.stderr.casefold()
