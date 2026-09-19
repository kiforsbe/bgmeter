"""Public driver protocol and isolated driver registry."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from importlib import metadata
from inspect import iscoroutinefunction
import json
from typing import Callable, Protocol, runtime_checkable

from .models import (
    DriverMatch,
    MeterDevice,
    MeterIdentity,
    ReadOptions,
    ReadResult,
    TransportEndpoint,
)
from .transports import TransportSession


DRIVER_API_VERSION = 1
ENTRY_POINT_GROUP = "bgmeter.drivers"


@runtime_checkable
class MeterDriver(Protocol):
    driver_id: str
    display_name: str
    api_version: int
    supported_transports: frozenset[str]
    known_meter_identities: tuple[str, ...]

    def match_candidate(self, endpoint: TransportEndpoint) -> DriverMatch | None: ...

    async def probe(self, session: TransportSession) -> MeterIdentity: ...

    async def read_records(
        self,
        session: TransportSession,
        device: MeterDevice,
        options: ReadOptions,
    ) -> ReadResult: ...


DriverFactory = Callable[[], MeterDriver]


@dataclass(frozen=True, slots=True)
class DriverDescriptor:
    driver_id: str
    display_name: str
    api_version: int
    supported_transports: frozenset[str]
    known_meter_identities: tuple[str, ...]
    source: str
    entry_point: str | None = None
    entry_point_value: str | None = None
    package_name: str | None = None
    package_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supported_transports",
            frozenset(self.supported_transports),
        )
        object.__setattr__(
            self,
            "known_meter_identities",
            tuple(self.known_meter_identities),
        )


class DriverRegistry:
    """An explicit, process-local collection of validated driver factories."""

    def __init__(self) -> None:
        self._factories: dict[str, DriverFactory] = {}
        self._descriptors: dict[str, DriverDescriptor] = {}

    def register(
        self,
        factory: DriverFactory,
        *,
        source: str = "manual",
        entry_point: str | None = None,
        entry_point_value: str | None = None,
        package_name: str | None = None,
        package_version: str | None = None,
    ) -> DriverDescriptor:
        driver = self._create_and_validate(factory)
        if driver.driver_id in self._factories:
            raise ValueError(f"driver {driver.driver_id!r} is already registered")

        descriptor = DriverDescriptor(
            driver_id=driver.driver_id,
            display_name=driver.display_name,
            api_version=driver.api_version,
            supported_transports=frozenset(driver.supported_transports),
            known_meter_identities=tuple(driver.known_meter_identities),
            source=source,
            entry_point=entry_point,
            entry_point_value=entry_point_value,
            package_name=package_name,
            package_version=package_version,
        )
        self._factories[driver.driver_id] = factory
        self._descriptors[driver.driver_id] = descriptor
        return descriptor

    def unregister(self, driver_id: str) -> DriverDescriptor:
        descriptor = self._descriptors.pop(driver_id)
        del self._factories[driver_id]
        return descriptor

    def get(self, driver_id: str) -> MeterDriver:
        factory = self._factories[driver_id]
        return self._create_and_validate(factory)

    def descriptors(self) -> tuple[DriverDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    def available_entry_points(self) -> tuple[metadata.EntryPoint, ...]:
        points = metadata.entry_points(group=ENTRY_POINT_GROUP)
        return tuple(sorted(points, key=lambda point: point.name))

    def register_entry_point(self, name: str) -> DriverDescriptor:
        matches = tuple(
            point for point in self.available_entry_points() if point.name == name
        )
        if not matches:
            raise KeyError(f"no installed driver entry point named {name!r}")
        if len(matches) != 1:
            raise ValueError(f"multiple installed driver entry points named {name!r}")
        return self._register_loaded_entry_point(matches[0])

    def register_available_entry_points(
        self,
    ) -> tuple[tuple[DriverDescriptor, ...], dict[str, str]]:
        descriptors: list[DriverDescriptor] = []
        errors: dict[str, str] = {}
        points = self.available_entry_points()
        counts = Counter(point.name for point in points)
        reserved_names = {point.name for point in points if counts[point.name] == 1}
        for point in points:
            try:
                descriptors.append(self._register_loaded_entry_point(point))
            except Exception as error:
                key = point.name
                if counts[key] > 1:
                    package_name, package_version = self._package_identity(point)
                    key = json.dumps([
                        point.name,
                        package_name,
                        package_version,
                        getattr(point, "value", None),
                    ])
                    base_key = key
                    occurrence = 1
                    while key in errors or key in reserved_names:
                        occurrence += 1
                        key = f"{base_key}#{occurrence}"
                errors[key] = str(error)
        return tuple(descriptors), errors

    @staticmethod
    def _package_identity(point: metadata.EntryPoint) -> tuple[str | None, str | None]:
        distribution = getattr(point, "dist", None)
        package_name = getattr(distribution, "name", None)
        if package_name is None and distribution is not None:
            package_name = distribution.metadata.get("Name")
        return package_name, getattr(distribution, "version", None)

    def _register_loaded_entry_point(
        self,
        point: metadata.EntryPoint,
    ) -> DriverDescriptor:
        package_name, package_version = self._package_identity(point)
        factory = point.load()
        return self.register(
            factory,
            source="entry-point",
            entry_point=point.name,
            entry_point_value=getattr(point, "value", None),
            package_name=package_name,
            package_version=package_version,
        )

    @staticmethod
    def _create_and_validate(factory: DriverFactory) -> MeterDriver:
        if not callable(factory):
            raise TypeError("driver factory must be callable")
        driver = factory()

        driver_id = getattr(driver, "driver_id", None)
        if not isinstance(driver_id, str) or not driver_id:
            raise ValueError("driver_id must be a non-empty string")
        display_name = getattr(driver, "display_name", None)
        if not isinstance(display_name, str) or not display_name:
            raise ValueError("display_name must be a non-empty string")
        api_version = getattr(driver, "api_version", None)
        if api_version != DRIVER_API_VERSION:
            raise ValueError(
                f"driver API version {api_version!r} is incompatible with "
                f"{DRIVER_API_VERSION}"
            )
        supported = getattr(driver, "supported_transports", None)
        if not isinstance(supported, frozenset) or not all(
            isinstance(item, str) and item for item in supported
        ):
            raise ValueError("supported_transports must be a frozenset of strings")
        identities = getattr(driver, "known_meter_identities", None)
        if not isinstance(identities, tuple) or not all(
            isinstance(item, str) for item in identities
        ):
            raise ValueError("known_meter_identities must be a tuple of strings")
        match_candidate = getattr(driver, "match_candidate", None)
        if not callable(match_candidate):
            raise ValueError("driver must implement match_candidate()")
        for method_name in ("probe", "read_records"):
            method = getattr(driver, method_name, None)
            if not callable(method):
                raise ValueError(f"driver must implement {method_name}()")
            if not iscoroutinefunction(method):
                raise ValueError(f"{method_name}() must be async")
        return driver
