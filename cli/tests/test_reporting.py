from __future__ import annotations

import io
import logging
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from bgmeter import (  # noqa: E402
    CompletionStatus,
    DeviceNotFoundError,
    DiscoveryError,
    MeterConnectionError,
    MeterTimeoutError,
    ProgressEvent,
    ProgressLevel,
    ProtocolError,
    UnsupportedDeviceError,
)
from bgmeter_cli.config import DriverConfig, save_config  # noqa: E402
from bgmeter_cli.reporter import ConsoleReporter  # noqa: E402
from test_cli import (  # noqa: E402,F401
    FakeEntryPoint,
    FakeManager,
    complete_result,
    driver_factory,
    install_entry_points,
    invoke,
    make_device,
)


READ = ["read", "--device", "fake:meter-1"]


def event(level, message, current=None, total=None):
    return ProgressEvent(level, message, current, total)


class ScriptedManager(FakeManager):
    """A fake manager that reports scripted progress events during a read."""

    def __init__(self, registry, progress, *, script=(), **kwargs):
        super().__init__(registry, **kwargs)
        self.progress = progress
        self.script = tuple(script)

    async def read(self, device, options=None):
        for item in self.script:
            self.progress(item)
        return await super().read(device, options)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    install_entry_points(monkeypatch, FakeEntryPoint("example", driver_factory()))
    path = tmp_path / "drivers.json"
    save_config(DriverConfig(("example",)), path)
    return path


def read_with(
    config_path,
    result,
    argv=READ,
    *,
    script=(),
    manager_class=ScriptedManager,
    **manager_kwargs,
):
    def factory(registry, progress):
        return manager_class(
            registry,
            progress,
            devices=(make_device(),),
            result=result,
            script=script,
            **manager_kwargs,
        )

    return invoke(argv, config_path=config_path, manager_factory=factory)


SCRIPT = (
    event(ProgressLevel.INFO, "Connecting to Meter 1..."),
    event(ProgressLevel.DETAIL, "Asking the meter how many records it holds."),
    event(ProgressLevel.WARNING, "Something looked odd."),
)


def test_reporter_shows_only_warnings_by_default():
    stream = io.StringIO()
    reporter = ConsoleReporter(stream, 0)

    for item in SCRIPT:
        reporter(item)

    assert stream.getvalue() == "warning: Something looked odd.\n"


def test_reporter_prefixes_elapsed_time_at_double_verbosity():
    times = iter([100.0, 101.24])
    stream = io.StringIO()
    reporter = ConsoleReporter(stream, 2, clock=lambda: next(times))

    reporter(event(ProgressLevel.INFO, "Hello."))

    assert stream.getvalue() == "[ 1.2s] Hello.\n"


def test_reporter_caps_verbosity_at_two():
    stream = io.StringIO()
    reporter = ConsoleReporter(stream, 9, clock=lambda: 0.0)

    reporter(event(ProgressLevel.DETAIL, "Deep detail."))

    assert stream.getvalue() == "[ 0.0s] Deep detail.\n"


def test_default_run_shows_only_warnings(config_path, complete_result):
    status, stdout, stderr = read_with(config_path, complete_result, script=SCRIPT)

    assert status == 0
    assert "7.4 mmol/L" in stdout
    assert stderr == "warning: Something looked odd.\n"


def test_single_v_adds_info_lines(config_path, complete_result):
    _, _, stderr = read_with(config_path, complete_result, ["-v", *READ], script=SCRIPT)

    assert stderr == "Connecting to Meter 1...\nwarning: Something looked odd.\n"


def test_double_v_adds_detail_lines_with_elapsed_time(config_path, complete_result):
    _, _, stderr = read_with(config_path, complete_result, ["-vv", *READ], script=SCRIPT)

    lines = stderr.splitlines()
    assert re.fullmatch(r"\[ *\d+\.\ds\] Connecting to Meter 1\.\.\.", lines[0])
    assert re.fullmatch(
        r"\[ *\d+\.\ds\] Asking the meter how many records it holds\.", lines[1]
    )
    assert lines[2] == "warning: Something looked odd."


@pytest.mark.parametrize(
    ("argv", "level"),
    [
        (["-v", *READ], 1),
        ([*READ, "-v"], 1),
        (["read", "-v", "--device", "fake:meter-1"], 1),
        (["-v", *READ, "-v"], 2),
        (["-vvv", *READ], 2),
        (["read", "-vv", "--device", "fake:meter-1"], 2),
    ],
)
def test_verbosity_flags_are_accepted_in_either_position_and_summed(
    config_path, complete_result, argv, level
):
    _, _, stderr = read_with(config_path, complete_result, argv, script=SCRIPT)

    assert ("Connecting to Meter 1" in stderr) == (level >= 1)
    assert ("Asking the meter" in stderr) == (level >= 2)


def test_counter_events_are_throttled(config_path, complete_result):
    counter = tuple(
        event(ProgressLevel.INFO, f"Reading records: {n} of 57", n, 57)
        for n in range(1, 58)
    )

    _, _, stderr = read_with(config_path, complete_result, ["-v", *READ], script=counter)

    lines = stderr.splitlines()
    assert lines[0] == "Reading records: 1 of 57"
    assert lines[-1] == "Reading records: 57 of 57"
    assert len(lines) == 11


def test_verbosity_never_changes_stdout(config_path, complete_result):
    _, quiet, _ = read_with(config_path, complete_result, script=SCRIPT)
    _, chatty, _ = read_with(config_path, complete_result, ["-vv", *READ], script=SCRIPT)

    assert quiet == chatty


def test_manager_factory_receives_the_console_reporter(config_path, complete_result):
    received = []

    def factory(registry, progress):
        received.append(progress)
        return FakeManager(registry, devices=(make_device(),), result=complete_result)

    invoke(READ, config_path=config_path, manager_factory=factory)

    assert isinstance(received[0], ConsoleReporter)


@pytest.mark.parametrize(
    ("completion", "line"),
    [
        (
            CompletionStatus.PARTIAL,
            "warning: retrieval is partial: some records may be missing.\n",
        ),
        (
            CompletionStatus.UNKNOWN,
            "warning: retrieval is unknown: the meter cannot confirm that this is "
            "the full history.\n",
        ),
    ],
)
def test_incomplete_read_prints_one_outcome_line_and_status_five(
    config_path, complete_result, completion, line
):
    result = replace(complete_result, completion=completion)

    status, _, stderr = read_with(config_path, result, ["-vv", *READ])

    assert status == 5
    assert stderr == line


@pytest.mark.parametrize(
    ("error", "status", "hint"),
    [
        (DeviceNotFoundError("no meter"), 3, "Bluetooth is enabled"),
        (UnsupportedDeviceError("no driver"), 3, "bgmeter drivers list"),
        (MeterTimeoutError("slow"), 5, "phone app"),
        (MeterConnectionError("gone"), 4, "no other app is connected"),
        (DiscoveryError("scan failed"), 4, "no other app is connected"),
        (ProtocolError("garbled"), 5, "-vv --log-level debug"),
    ],
)
def test_failures_print_an_error_line_and_a_hint(
    config_path, complete_result, error, status, hint
):
    actual, stdout, stderr = read_with(
        config_path, complete_result, read_error=error
    )

    lines = stderr.splitlines()
    assert actual == status
    assert stdout == ""
    assert lines[0] == f"error: {error}"
    assert lines[1].startswith("hint: ")
    assert hint in lines[1]


def test_selection_errors_have_no_hint(config_path, complete_result):
    status, _, stderr = read_with(config_path, complete_result, ["read"])

    assert status == 2
    assert stderr.startswith("error: ")
    assert "hint:" not in stderr


TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT")
ELAPSED = re.compile(r"^\[ *\d+\.\ds\] ")


class LoggingManager(ScriptedManager):
    """A fake manager whose layer logs at every level during a read."""

    async def read(self, device, options=None):
        log = logging.getLogger("bgmeter.fake")
        log.debug("wire detail 0x05")
        log.info("read started")
        log.error("absorbed cleanup failure")
        return await super().read(device, options)


def split_stderr(text):
    """Split stderr into (progress lines, log records without timestamps)."""
    progress, records = [], []
    for line in text.splitlines():
        if TIMESTAMP.match(line):
            records.append(line.split(" ", 1)[1])
        else:
            progress.append(ELAPSED.sub("", line))
    return progress, records


def test_default_terminal_log_level_is_error(config_path, complete_result):
    status, _, stderr = read_with(
        config_path, complete_result, manager_class=LoggingManager
    )

    _, records = split_stderr(stderr)
    assert status == 0
    assert records == ["ERROR bgmeter.fake: absorbed cleanup failure"]


def test_log_level_debug_writes_every_record_to_the_terminal(config_path, complete_result):
    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-level", "debug", *READ],
        manager_class=LoggingManager,
    )

    _, records = split_stderr(stderr)
    assert [record.split(":", 1)[0] for record in records if "bgmeter.fake" in record] == [
        "DEBUG bgmeter.fake",
        "INFO bgmeter.fake",
        "ERROR bgmeter.fake",
    ]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([*READ, "--log-level", "INFO"], ["INFO", "ERROR"]),
        (["--log-level", "debug", *READ, "--log-level", "error"], ["ERROR"]),
        (["--log-level", "info", *READ], ["INFO", "ERROR"]),
    ],
)
def test_log_level_is_case_insensitive_and_the_subcommand_position_wins(
    config_path, complete_result, argv, expected
):
    _, _, stderr = read_with(
        config_path, complete_result, argv, manager_class=LoggingManager
    )

    _, records = split_stderr(stderr)
    fake = [record for record in records if "bgmeter.fake" in record]
    assert [record.split(" ", 1)[0] for record in fake] == expected


def test_log_file_receives_records_exclusively_with_a_header(
    tmp_path, config_path, complete_result
):
    log_path = tmp_path / "bgmeter.log"

    status, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-file", str(log_path), "--log-level", "debug", *READ],
        manager_class=LoggingManager,
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert status == 0
    assert stderr == ""
    assert "bgmeter-cli" in lines[0] and "bgmeter-core" in lines[0]
    assert "command=read" in lines[0]
    fake_lines = [line for line in lines[1:] if "bgmeter.fake" in line]
    assert any("bgmeter_cli.app" in line for line in lines[1:])
    assert [line.split(" ", 1)[1] for line in fake_lines] == [
        "DEBUG bgmeter.fake: wire detail 0x05",
        "INFO bgmeter.fake: read started",
        "ERROR bgmeter.fake: absorbed cleanup failure",
    ]


def test_log_level_applies_to_the_log_file_and_defaults_to_error(
    tmp_path, config_path, complete_result
):
    log_path = tmp_path / "bgmeter.log"

    read_with(
        config_path,
        complete_result,
        ["--log-file", str(log_path), *READ],
        manager_class=LoggingManager,
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines[1:]] == [
        "ERROR bgmeter.fake: absorbed cleanup failure"
    ]


def test_progress_still_reaches_the_terminal_when_logging_goes_to_a_file(
    tmp_path, config_path, complete_result
):
    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["-v", "--log-file", str(tmp_path / "bgmeter.log"), *READ],
        script=SCRIPT,
        manager_class=LoggingManager,
    )

    assert stderr == "Connecting to Meter 1...\nwarning: Something looked odd.\n"


def test_final_failure_prints_user_lines_then_one_technical_log_record(
    config_path, complete_result
):
    status, _, stderr = read_with(
        config_path, complete_result, read_error=MeterTimeoutError("boom")
    )

    lines = stderr.splitlines()
    assert status == 5
    assert lines[0] == "error: boom"
    assert lines[1].startswith("hint: ")
    assert re.fullmatch(
        r"\S+ ERROR bgmeter_cli\.app: command read failed: MeterTimeoutError: boom "
        r"\(exit status 5\)",
        lines[2],
    )
    assert len(lines) == 3
    assert "Traceback" not in stderr


def test_final_failure_traceback_appears_only_at_debug(config_path, complete_result):
    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-level", "debug", *READ],
        read_error=MeterTimeoutError("boom"),
    )

    assert "Traceback (most recent call last)" in stderr


def test_final_failure_goes_to_the_log_file_not_the_terminal(
    tmp_path, config_path, complete_result
):
    log_path = tmp_path / "bgmeter.log"

    _, _, stderr = read_with(
        config_path,
        complete_result,
        ["--log-file", str(log_path), *READ],
        read_error=MeterTimeoutError("boom"),
    )

    assert "ERROR bgmeter_cli.app" not in stderr
    assert stderr.splitlines()[0] == "error: boom"
    assert "ERROR bgmeter_cli.app: command read failed: MeterTimeoutError: boom" in (
        log_path.read_text(encoding="utf-8")
    )


def test_unwritable_log_file_stops_before_any_meter_work(
    tmp_path, config_path, complete_result
):
    created = []

    def factory(registry, progress):
        created.append(registry)
        return FakeManager(registry, devices=(make_device(),), result=complete_result)

    status, stdout, stderr = invoke(
        ["--log-file", str(tmp_path / "missing" / "bgmeter.log"), *READ],
        config_path=config_path,
        manager_factory=factory,
    )

    assert status == 2
    assert stdout == ""
    assert stderr.startswith("error: cannot open log file ")
    assert created == []


def test_run_restores_the_root_logger(config_path, complete_result, tmp_path):
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level

    read_with(config_path, complete_result, ["--log-level", "debug", *READ])
    read_with(
        config_path,
        complete_result,
        ["--log-file", str(tmp_path / "bgmeter.log"), *READ],
    )

    assert root.handlers == handlers
    assert root.level == level


def test_progress_depends_only_on_v_and_logs_only_on_log_level(
    config_path, complete_result
):
    results = {}
    for verbosity in (0, 1, 2):
        for level in ("debug", "error"):
            argv = ["--log-level", level, *READ]
            if verbosity:
                argv = ["-" + "v" * verbosity, *argv]
            _, _, stderr = read_with(
                config_path,
                complete_result,
                argv,
                script=SCRIPT,
                manager_class=LoggingManager,
            )
            results[(verbosity, level)] = split_stderr(stderr)

    for verbosity in (0, 1, 2):
        assert results[(verbosity, "debug")][0] == results[(verbosity, "error")][0]
    for level in ("debug", "error"):
        assert results[(0, level)][1] == results[(1, level)][1] == results[(2, level)][1]
    assert results[(0, "debug")][0] != results[(2, "debug")][0]
    assert results[(2, "debug")][1] != results[(2, "error")][1]
