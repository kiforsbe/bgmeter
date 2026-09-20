"""Argument parsing and command execution for bgmeter."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import os
import sys
import tempfile
from typing import Callable, Sequence, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bgmeter import (
    AmbiguousDeviceError,
    BleTransport,
    CompletionStatus,
    DeviceNotFoundError,
    DiscoveryError,
    MeterConnectionError,
    MeterManager,
    MeterTimeoutError,
    ProgressCallback,
    ProtocolError,
    ReadOptions,
    UnsupportedDeviceError,
)

from .config import ConfigError, DriverConfig, load_config, save_config
from .exporters import render_csv, render_json, render_terminal
from .logging_setup import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVELS,
    LogFileError,
    configured_logging,
)
from .registry import (
    DriverPlugin,
    DriverSelectionError,
    RegistryLoad,
    filter_registry,
    installed_plugins,
    load_registered_registry,
    resolve_plugin,
)
from .reporter import ConsoleReporter
from .selection import SelectionError, select_device
from .store import MeasurementStore, StoreError

_log = logging.getLogger(__name__)


class ExportError(RuntimeError):
    """An output request is invalid or cannot be written safely."""


@dataclass(frozen=True, slots=True)
class OutputSpec:
    format: str
    path: Path | None


ManagerFactory = Callable[[object, ProgressCallback | None], object]


def _timezone_name(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise argparse.ArgumentTypeError(f"invalid timezone {value!r}") from error
    return value


def _add_reporting_options(parser: argparse.ArgumentParser, suffix: str) -> None:
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        dest=f"verbose_{suffix}",
        help="explain progress in plain language (-v) with more detail (-vv)",
    )
    parser.add_argument(
        "--log-level",
        type=str.lower,
        choices=LOG_LEVELS,
        default=None,
        dest=f"log_level_{suffix}",
        help=f"minimum level of technical log records (default: {DEFAULT_LOG_LEVEL})",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        metavar="PATH",
        dest=f"log_file_{suffix}",
        help="write technical logs to this file instead of the terminal",
    )


def _verbosity(arguments) -> int:
    return min(2, arguments.verbose_top + arguments.verbose_sub)


def _log_settings(arguments) -> tuple[str, Path | None]:
    level = arguments.log_level_sub or arguments.log_level_top or DEFAULT_LOG_LEVEL
    log_file = arguments.log_file_sub or arguments.log_file_top
    return level, log_file


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bgmeter",
        description="Read blood glucose meters through installed drivers.",
    )
    _add_reporting_options(parser, "top")
    commands = parser.add_subparsers(dest="command", required=True)

    devices = commands.add_parser("devices", help="discover supported meters")
    _add_reporting_options(devices, "sub")
    devices.add_argument("--driver", help="restrict discovery to one registered driver")

    info = commands.add_parser("info", help="show discovered meter information")
    _add_reporting_options(info, "sub")
    info.add_argument("--device", required=True, help="device selector")
    info.add_argument("--driver", help="restrict discovery to one registered driver")

    read = commands.add_parser("read", help="read records from a meter")
    _add_reporting_options(read, "sub")
    read.add_argument("--device", help="device selector (required non-interactively)")
    read.add_argument("--driver", help="restrict discovery to one registered driver")
    read.add_argument(
        "--timezone",
        type=_timezone_name,
        help="IANA timezone for the meter wall clock",
    )
    read.add_argument(
        "--output",
        action="append",
        help="repeat terminal, csv[=PATH], or json[=PATH]",
    )
    read.add_argument("--show-raw", action="store_true", help="show raw bytes in terminal output")
    read.add_argument("--store", action="store_true", help="store records in the local SQLite database")
    read.add_argument("--force", action="store_true", help="replace existing output files")

    drivers = commands.add_parser("drivers", help="inspect persistent driver registration")
    _add_reporting_options(drivers, "sub")
    driver_commands = drivers.add_subparsers(dest="driver_command", required=True)
    driver_list = driver_commands.add_parser("list", help="list installed drivers")
    scope = driver_list.add_mutually_exclusive_group()
    scope.add_argument("--available", action="store_true", help="show all installed entry points")
    scope.add_argument("--registered", action="store_true", help="show registered entry points")
    driver_info = driver_commands.add_parser("info", help="show driver metadata")
    driver_info.add_argument("driver")
    driver_register = driver_commands.add_parser("register", help="enable an installed driver")
    driver_register.add_argument("driver")
    driver_unregister = driver_commands.add_parser("unregister", help="disable a driver")
    driver_unregister.add_argument("driver")
    return parser


def _default_manager_factory(registry, progress=None):
    return MeterManager(
        registry=registry, transports=(BleTransport(),), progress=progress
    )


def _report_plugin_errors(
    plugins: Sequence[DriverPlugin], stderr: TextIO, *, seen: set[tuple[str, str]]
) -> None:
    for plugin in plugins:
        if plugin.error is None:
            continue
        identity = (plugin.entry_point, plugin.error)
        if identity in seen:
            continue
        seen.add(identity)
        stderr.write(f"warning: driver {plugin.entry_point}: {plugin.error}\n")


def _save_driver_config(config: DriverConfig, config_path) -> None:
    try:
        save_config(config, config_path)
    except OSError as error:
        raise ExportError(f"cannot save driver configuration: {error}") from error


def _format_devices(devices) -> str:
    lines = []
    for device in devices:
        model = device.identity.model or device.endpoint.name or "unknown model"
        lines.append(f"{device.selector}\t{device.driver_id}\t{model}")
    return "\n".join(lines) + ("\n" if lines else "")


def _format_device_info(device) -> str:
    identity = device.identity
    fields = (
        ("Selector", device.selector),
        ("Driver", device.driver_id),
        ("Transport", device.endpoint.transport),
        ("Endpoint", device.endpoint.identifier),
        ("Name", device.endpoint.name),
        ("Manufacturer", identity.manufacturer),
        ("Model", identity.model),
        ("Serial number", identity.serial_number),
        ("Firmware revision", identity.firmware_revision),
        ("Hardware revision", identity.hardware_revision),
        ("Software revision", identity.software_revision),
    )
    return "".join(f"{label}: {value or '-'}\n" for label, value in fields)


def _format_plugin_line(plugin: DriverPlugin) -> str:
    package = " ".join(
        item for item in (plugin.package_name, plugin.package_version) if item
    ) or "package unavailable"
    registration = "registered" if plugin.registered else "not registered"
    if plugin.error is not None:
        return f"{plugin.entry_point}\t{package}\t{registration}\tincompatible"
    return (
        f"{plugin.entry_point}\t{plugin.driver_id}\t{package}\t"
        f"{registration}\tcompatible"
    )


def _format_plugin_info(plugin: DriverPlugin) -> str:
    descriptor = plugin.descriptor
    package = " ".join(
        item for item in (plugin.package_name, plugin.package_version) if item
    ) or "unavailable"
    lines = [
        f"Entry point: {plugin.entry_point}",
        f"Entry-point value: {plugin.entry_point_value or '-'}",
        f"Package: {package}",
        f"Registered: {'yes' if plugin.registered else 'no'}",
    ]
    if descriptor is None:
        lines.extend(("Compatibility: incompatible", f"Error: {plugin.error}"))
    else:
        lines.extend(
            (
                f"Driver ID: {descriptor.driver_id}",
                f"Display name: {descriptor.display_name}",
                f"Driver API: {descriptor.api_version}",
                f"Transports: {', '.join(sorted(descriptor.supported_transports)) or '-'}",
                f"Known identities: {', '.join(descriptor.known_meter_identities) or '-'}",
                "Compatibility: compatible",
            )
        )
    return "\n".join(lines) + "\n"


def _parse_outputs(values: Sequence[str] | None) -> tuple[OutputSpec, ...]:
    values = values or ("terminal",)
    outputs: list[OutputSpec] = []
    stdout_count = 0
    file_paths: set[Path] = set()
    for value in values:
        if "=" in value:
            output_format, raw_path = value.split("=", 1)
            if output_format not in {"csv", "json"} or not raw_path:
                raise ExportError(f"unknown output {value!r}")
            path = Path(raw_path)
            resolved = path.absolute()
            if resolved in file_paths:
                raise ExportError(f"output path {path} is repeated")
            file_paths.add(resolved)
            outputs.append(OutputSpec(output_format, path))
        elif value in {"terminal", "csv", "json"}:
            stdout_count += 1
            outputs.append(OutputSpec(value, None))
        else:
            raise ExportError(f"unknown output {value!r}")
    if stdout_count > 1:
        raise ExportError("only one output may target stdout")
    return tuple(outputs)


def _check_destinations(outputs: Sequence[OutputSpec], *, force: bool) -> None:
    for output in outputs:
        if output.path is not None and output.path.exists() and not force:
            raise ExportError(f"output file {output.path} already exists (use --force)")


def _render_outputs(result, outputs, *, show_raw):
    rendered = []
    for output in outputs:
        try:
            if output.format == "terminal":
                payload = render_terminal(result, show_raw=show_raw)
            elif output.format == "csv":
                payload = render_csv(result)
            else:
                payload = render_json(result)
        except Exception as error:
            raise ExportError(f"cannot render {output.format}: {error}") from error
        rendered.append((output, payload))
    return tuple(rendered)


def _atomic_write(path: Path, payload: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise ExportError(f"output file {path} already exists (use --force)")
    if not path.parent.exists():
        raise ExportError(f"output directory {path.parent} does not exist")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() and not force:
            raise ExportError(f"output file {path} already exists (use --force)")
        os.replace(temporary, path)
    except BaseException as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(error, ExportError):
            raise
        raise ExportError(f"cannot write output file {path}: {error}") from error


def _publish_outputs(rendered, *, force: bool, stdout: TextIO) -> None:
    for output, payload in rendered:
        if output.path is not None:
            _atomic_write(output.path, payload, force=force)
    for output, payload in rendered:
        if output.path is None:
            stdout.write(payload)


_OUTCOME_LINES = {
    CompletionStatus.PARTIAL: (
        "warning: retrieval is partial: some records may be missing.\n"
    ),
    CompletionStatus.UNKNOWN: (
        "warning: retrieval is unknown: the meter cannot confirm that this is "
        "the full history.\n"
    ),
}


async def _run_meter_command(
    arguments,
    *,
    registry,
    manager_factory,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    database_path: str | Path | None,
    progress: ProgressCallback | None,
) -> int:
    manager = manager_factory(registry, progress)
    devices = await manager.discover()

    if arguments.command == "devices":
        stdout.write(_format_devices(devices))
        return 0

    device = select_device(
        devices,
        arguments.device,
        stdin=stdin,
        stderr=stderr,
    )
    if arguments.command == "info":
        stdout.write(_format_device_info(device))
        return 0

    outputs = _parse_outputs(arguments.output)
    _check_destinations(outputs, force=arguments.force)
    _log.info("reading %s", device.selector)
    result = await manager.read(device, ReadOptions(timezone=arguments.timezone))
    _log.info(
        "read complete: device=%s completion=%s records=%d",
        device.selector,
        result.completion.value,
        len(result.records),
    )
    if arguments.store:
        _log.info("storing %d record(s)", len(result.records))
        try:
            MeasurementStore(database_path).store(result.records)
        except StoreError as error:
            raise ExportError(f"cannot store measurements: {error}") from error
    rendered = _render_outputs(result, outputs, show_raw=arguments.show_raw)
    _log.debug(
        "publishing outputs: %s",
        [(o.format, str(o.path) if o.path else "stdout") for o in outputs],
    )
    _publish_outputs(rendered, force=arguments.force, stdout=stdout)
    if result.completion is not CompletionStatus.COMPLETE:
        stderr.write(_OUTCOME_LINES[result.completion])
        return 5
    return 0


def _dispatch(
    arguments,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    config_path,
    database_path,
    manager_factory,
    progress,
) -> int:
    config = load_config(config_path)
    _log.debug("registered drivers: %s", list(config.registered))
    loaded = load_registered_registry(config)
    reported: set[tuple[str, str]] = set()
    _report_plugin_errors(loaded.errors, stderr, seen=reported)

    if arguments.command == "drivers":
        catalog = installed_plugins(config)
        if arguments.driver_command == "list":
            _report_plugin_errors(catalog, stderr, seen=reported)
            plugins = catalog
            if arguments.available:
                plugins = tuple(plugin for plugin in plugins if plugin.installed)
            elif arguments.registered:
                plugins = tuple(plugin for plugin in plugins if plugin.registered)
            for plugin in plugins:
                stdout.write(_format_plugin_line(plugin) + "\n")
            return 0

        plugin = resolve_plugin(catalog, arguments.driver)
        if arguments.driver_command == "info":
            if plugin.error is not None:
                _report_plugin_errors((plugin,), stderr, seen=reported)
            stdout.write(_format_plugin_info(plugin))
            return 0 if plugin.error is None else 2

        if arguments.driver_command == "register":
            if plugin.error is not None:
                raise DriverSelectionError(
                    f"driver {arguments.driver!r} is incompatible: {plugin.error}"
                )
            already_registered = plugin.entry_point in config.registered
            proposed = (
                config
                if already_registered
                else DriverConfig((*config.registered, plugin.entry_point))
            )
            proposed_load = load_registered_registry(proposed)
            proposed_plugin = next(
                item
                for item in proposed_load.plugins
                if item.entry_point == plugin.entry_point
            )
            proposed_error = proposed_plugin.error
            if proposed_error is None:
                catalog_by_entry_point = {
                    item.entry_point: item for item in catalog
                }
                related_error = next(
                    (
                        item.error
                        for item in proposed_load.errors
                        if catalog_by_entry_point.get(item.entry_point) is not None
                        and catalog_by_entry_point[item.entry_point].driver_id
                        == plugin.driver_id
                    ),
                    None,
                )
                proposed_error = related_error
            if proposed_error is not None:
                raise DriverSelectionError(
                    f"driver {arguments.driver!r} cannot be registered with "
                    f"the current drivers: {proposed_error}"
                )
            if not already_registered:
                config = proposed
                _save_driver_config(config, config_path)
            stdout.write(f"registered {plugin.entry_point}\n")
            return 0

        registered_matches = tuple(
            item
            for item in catalog
            if item.registered
            and (
                item.entry_point == arguments.driver
                or item.driver_id == arguments.driver
            )
        )
        if not registered_matches:
            raise DriverSelectionError(
                f"driver {arguments.driver!r} is not registered"
            )
        if len(registered_matches) != 1:
            raise DriverSelectionError(f"driver {arguments.driver!r} is ambiguous")
        entry_point = registered_matches[0].entry_point
        _save_driver_config(
            DriverConfig(tuple(item for item in config.registered if item != entry_point)),
            config_path,
        )
        stdout.write(f"unregistered {entry_point}\n")
        return 0

    selected_load = filter_registry(loaded, getattr(arguments, "driver", None))
    return asyncio.run(
        _run_meter_command(
            arguments,
            registry=selected_load.registry,
            manager_factory=manager_factory,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            database_path=database_path,
            progress=progress,
        )
    )


_HINT_NOT_FOUND = (
    "Make sure the meter is on and close by, and that Bluetooth is enabled on "
    "this computer."
)
_HINT_UNSUPPORTED = (
    "None of the devices found is a supported meter. Run 'bgmeter drivers list' "
    "to see what is supported."
)
_HINT_TIMEOUT = (
    "The meter did not answer in time. Wake the meter, make sure it is not still "
    "connected to the phone app, and try again. Run with -vv to see what happened."
)
_HINT_CONNECTION = (
    "Check that Bluetooth is turned on and that no other app is connected to the "
    "meter."
)
_HINT_PROTOCOL = (
    "The meter sent data this program could not understand. Try again; if it keeps "
    "happening, run with -vv --log-level debug to see technical details."
)


def _classify(error: Exception) -> tuple[int, str | None] | None:
    """Map a handled failure to its exit status and optional hint."""
    if isinstance(
        error, (SelectionError, DriverSelectionError, ConfigError, AmbiguousDeviceError)
    ):
        return 2, None
    if isinstance(error, DeviceNotFoundError):
        return 3, _HINT_NOT_FOUND
    if isinstance(error, UnsupportedDeviceError):
        return 3, _HINT_UNSUPPORTED
    if isinstance(error, MeterTimeoutError):
        return 5, _HINT_TIMEOUT
    if isinstance(error, (DiscoveryError, MeterConnectionError)):
        return 4, _HINT_CONNECTION
    if isinstance(error, ProtocolError):
        return 5, _HINT_PROTOCOL
    if isinstance(error, ExportError):
        return 6, None
    return None


def _log_final_failure(command: str, error: Exception, status: int) -> None:
    _log.error(
        "command %s failed: %s: %s (exit status %d)",
        command,
        type(error).__name__,
        error,
        status,
    )
    _log.debug("command %s failure traceback", command, exc_info=error)


def _execute(
    arguments,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    config_path,
    database_path,
    manager_factory,
    progress,
) -> int:
    try:
        return _dispatch(
            arguments,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            config_path=config_path,
            database_path=database_path,
            manager_factory=manager_factory,
            progress=progress,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        stderr.write("interrupted\n")
        return 130
    except Exception as error:
        classified = _classify(error)
        if classified is None:
            raise
        status, hint = classified
        stderr.write(f"error: {error}\n")
        if hint is not None:
            stderr.write(f"hint: {hint}\n")
        _log_final_failure(arguments.command, error, status)
        return status


def run(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    config_path: str | Path | None = None,
    database_path: str | Path | None = None,
    manager_factory: ManagerFactory | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = _parser()
    try:
        from contextlib import redirect_stderr, redirect_stdout

        with redirect_stdout(stdout), redirect_stderr(stderr):
            arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    reporter = ConsoleReporter(stderr, _verbosity(arguments))
    level, log_file = _log_settings(arguments)
    try:
        with configured_logging(
            level, log_file=log_file, stream=stderr, command=arguments.command
        ):
            return _execute(
                arguments,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                config_path=config_path,
                database_path=database_path,
                manager_factory=manager_factory or _default_manager_factory,
                progress=reporter,
            )
    except LogFileError as error:
        stderr.write(f"error: {error}\n")
        return 2


__all__ = ["ExportError", "run"]
