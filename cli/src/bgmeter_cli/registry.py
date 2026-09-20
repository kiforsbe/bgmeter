"""Installed entry-point inspection and fresh registry construction."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from bgmeter import DriverDescriptor, DriverRegistry

from .config import DriverConfig

_log = logging.getLogger(__name__)


class DriverSelectionError(ValueError):
    """A requested driver cannot be resolved uniquely."""


@dataclass(frozen=True, slots=True)
class DriverPlugin:
    entry_point: str
    entry_point_value: str | None
    package_name: str | None
    package_version: str | None
    registered: bool
    descriptor: DriverDescriptor | None = None
    error: str | None = None

    @property
    def driver_id(self) -> str | None:
        return self.descriptor.driver_id if self.descriptor is not None else None

    @property
    def installed(self) -> bool:
        return self.entry_point_value is not None


@dataclass(frozen=True, slots=True)
class RegistryLoad:
    registry: DriverRegistry
    plugins: tuple[DriverPlugin, ...]

    @property
    def errors(self) -> tuple[DriverPlugin, ...]:
        return tuple(plugin for plugin in self.plugins if plugin.error is not None)


def _package_identity(point) -> tuple[str | None, str | None]:
    distribution = getattr(point, "dist", None)
    package_name = getattr(distribution, "name", None)
    if package_name is None and distribution is not None:
        package_name = distribution.metadata.get("Name")
    return package_name, getattr(distribution, "version", None)


def _plugin_from_point(
    point,
    *,
    registered: bool,
    descriptor: DriverDescriptor | None = None,
    error: str | None = None,
) -> DriverPlugin:
    package_name, package_version = _package_identity(point)
    return DriverPlugin(
        entry_point=point.name,
        entry_point_value=getattr(point, "value", None),
        package_name=package_name,
        package_version=package_version,
        registered=registered,
        descriptor=descriptor,
        error=error,
    )


def installed_plugins(config: DriverConfig) -> tuple[DriverPlugin, ...]:
    probe = DriverRegistry()
    points = probe.available_entry_points()
    counts = Counter(point.name for point in points)
    plugins: list[DriverPlugin] = []
    for point in points:
        registered = point.name in config.registered
        if counts[point.name] != 1:
            plugins.append(
                _plugin_from_point(
                    point,
                    registered=registered,
                    error=f"multiple installed entry points named {point.name!r}",
                )
            )
            continue
        registry = DriverRegistry()
        try:
            descriptor = registry.register_entry_point(point.name)
        except Exception as error:
            plugins.append(
                _plugin_from_point(
                    point, registered=registered, error=str(error)
                )
            )
        else:
            plugins.append(
                _plugin_from_point(
                    point, registered=registered, descriptor=descriptor
                )
            )
    installed_names = {point.name for point in points}
    for name in config.registered:
        if name not in installed_names:
            plugins.append(
                DriverPlugin(
                    entry_point=name,
                    entry_point_value=None,
                    package_name=None,
                    package_version=None,
                    registered=True,
                    error=f"no installed driver entry point named {name!r}",
                )
            )
    return tuple(
        sorted(
            plugins,
            key=lambda item: (
                item.entry_point.casefold(),
                item.package_name or "",
                item.entry_point_value or "",
            ),
        )
    )


def load_registered_registry(config: DriverConfig) -> RegistryLoad:
    registry = DriverRegistry()
    plugins: list[DriverPlugin] = []
    points = registry.available_entry_points()
    by_name: dict[str, list[object]] = {}
    for point in points:
        by_name.setdefault(point.name, []).append(point)

    for name in config.registered:
        matches = by_name.get(name, [])
        if not matches:
            _log.error("registered driver entry point %r is not installed", name)
            plugins.append(
                DriverPlugin(
                    entry_point=name,
                    entry_point_value=None,
                    package_name=None,
                    package_version=None,
                    registered=True,
                    error=f"no installed driver entry point named {name!r}",
                )
            )
            continue
        if len(matches) != 1:
            _log.error("multiple installed entry points are named %r", name)
            plugins.append(
                _plugin_from_point(
                    matches[0],
                    registered=True,
                    error=f"multiple installed entry points named {name!r}",
                )
            )
            continue
        point = matches[0]
        try:
            descriptor = registry.register_entry_point(name)
        except Exception as error:
            _log.error(
                "driver entry point %r failed to load: %s: %s",
                name,
                type(error).__name__,
                error,
            )
            _log.debug(
                "driver entry point %r load failure traceback", name, exc_info=True
            )
            plugins.append(
                _plugin_from_point(point, registered=True, error=str(error))
            )
        else:
            plugins.append(
                _plugin_from_point(
                    point, registered=True, descriptor=descriptor
                )
            )
    return RegistryLoad(registry=registry, plugins=tuple(plugins))


def filter_registry(load: RegistryLoad, identifier: str | None) -> RegistryLoad:
    if identifier is None:
        return load
    matches = tuple(
        plugin
        for plugin in load.plugins
        if plugin.error is None
        and (
            plugin.entry_point == identifier
            or plugin.driver_id == identifier
        )
    )
    if not matches:
        raise DriverSelectionError(
            f"driver {identifier!r} is not a compatible registered driver"
        )
    if len(matches) != 1:
        raise DriverSelectionError(f"driver {identifier!r} is ambiguous")
    selected = matches[0]
    registry = DriverRegistry()
    descriptor = registry.register_entry_point(selected.entry_point)
    plugin = DriverPlugin(
        entry_point=selected.entry_point,
        entry_point_value=selected.entry_point_value,
        package_name=selected.package_name,
        package_version=selected.package_version,
        registered=True,
        descriptor=descriptor,
    )
    return RegistryLoad(registry=registry, plugins=(plugin,))


def resolve_plugin(
    plugins: Iterable[DriverPlugin], identifier: str
) -> DriverPlugin:
    matches = tuple(
        plugin
        for plugin in plugins
        if plugin.entry_point == identifier or plugin.driver_id == identifier
    )
    if not matches:
        raise DriverSelectionError(f"no installed driver named {identifier!r}")
    if len(matches) != 1:
        raise DriverSelectionError(f"driver {identifier!r} is ambiguous")
    return matches[0]


__all__ = [
    "DriverPlugin",
    "DriverSelectionError",
    "RegistryLoad",
    "filter_registry",
    "installed_plugins",
    "load_registered_registry",
    "resolve_plugin",
]
