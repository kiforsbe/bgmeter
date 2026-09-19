"""Driver/transport composition and connection lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

from .drivers import DriverRegistry, MeterDriver
from .errors import (
    AmbiguousDeviceError,
    DeviceNotFoundError,
    MeterConnectionError,
    UnsupportedDeviceError,
)
from .models import CompletionStatus, MeterDevice, ReadOptions, ReadResult, TransportEndpoint
from .transports import BleTransport, MeterTransport, TransportSession


class _SessionCloseError(MeterConnectionError):
    """Distinguish cleanup-only failures from errors raised by the body."""


@asynccontextmanager
async def _session_scope(session: TransportSession) -> AsyncIterator[None]:
    try:
        yield
    except BaseException as primary:
        try:
            await session.close()
        except BaseException as cleanup:
            primary.add_note(f"Session cleanup also failed: {type(cleanup).__name__}: {cleanup}")
        raise
    else:
        try:
            await session.close()
        except Exception as error:
            raise _SessionCloseError(f"Session disconnect failed: {error}") from error


class ConnectedMeter:
    """A selected meter bound to one live transport session."""

    def __init__(
        self,
        device: MeterDevice,
        driver: MeterDriver,
        session: TransportSession,
    ) -> None:
        self.device = device
        self._driver = driver
        self._session = session

    async def read_records(self, options: ReadOptions | None = None) -> ReadResult:
        return await self._driver.read_records(
            self._session,
            self.device,
            options or ReadOptions(),
        )


class MeterManager:
    """Compose an isolated driver registry with explicit transports."""

    def __init__(
        self,
        *,
        registry: DriverRegistry,
        transports: Iterable[MeterTransport],
    ) -> None:
        self.registry = registry
        self.transports = tuple(transports)
        self._transports_by_name: dict[str, MeterTransport] = {}
        for transport in self.transports:
            if transport.name in self._transports_by_name:
                raise ValueError(f"transport {transport.name!r} is already configured")
            self._transports_by_name[transport.name] = transport

    @classmethod
    def default(cls) -> MeterManager:
        registry = DriverRegistry()
        registry.register_available_entry_points()
        return cls(registry=registry, transports=(BleTransport(),))

    async def discover(self, *, timeout: float = 5.0) -> tuple[MeterDevice, ...]:
        devices: list[MeterDevice] = []
        endpoint_found = False
        descriptors = self.registry.descriptors()

        for transport in self.transports:
            drivers = tuple(
                self.registry.get(descriptor.driver_id)
                for descriptor in descriptors
                if transport.name in descriptor.supported_transports
            )
            endpoints = await transport.discover(timeout=timeout)
            endpoint_found = endpoint_found or bool(endpoints)
            for endpoint in endpoints:
                device = await self._identify_endpoint(transport, endpoint, drivers)
                if device is not None:
                    devices.append(device)

        if devices:
            return tuple(devices)
        if endpoint_found:
            raise UnsupportedDeviceError(
                "discovered endpoints are not supported by registered drivers"
            )
        raise DeviceNotFoundError("no meter endpoints were discovered")

    async def _identify_endpoint(
        self,
        transport: MeterTransport,
        endpoint: TransportEndpoint,
        drivers: tuple[MeterDriver, ...],
    ) -> MeterDevice | None:
        matches = tuple(
            (driver, match)
            for driver in drivers
            if (match := driver.match_candidate(endpoint)) is not None
        )
        if not matches:
            return None

        selector = f"{endpoint.transport}:{endpoint.identifier}"
        confidences = sorted(
            {match.confidence for _, match in matches},
            reverse=True,
        )
        for confidence in confidences:
            tier = tuple(
                (driver, match)
                for driver, match in matches
                if match.confidence == confidence
            )
            if len(tier) != 1:
                driver_ids = ", ".join(
                    sorted(driver.driver_id for driver, _ in tier)
                )
                raise AmbiguousDeviceError(
                    f"{selector} has equal-confidence matches from {driver_ids}"
                )

            driver, match = tier[0]
            session = await self._connect(transport, endpoint)
            async with _session_scope(session):
                try:
                    identity = await driver.probe(session)
                except UnsupportedDeviceError:
                    continue
            return MeterDevice(
                selector=selector,
                driver_id=driver.driver_id,
                endpoint=endpoint,
                identity=identity,
                match=match,
            )
        return None

    @asynccontextmanager
    async def open(self, device: MeterDevice) -> AsyncIterator[ConnectedMeter]:
        transport = self._transports_by_name.get(device.endpoint.transport)
        if transport is None:
            raise UnsupportedDeviceError(
                f"transport {device.endpoint.transport!r} is not configured"
            )
        try:
            driver = self.registry.get(device.driver_id)
        except KeyError as error:
            raise UnsupportedDeviceError(
                f"driver {device.driver_id!r} is not registered"
            ) from error
        if device.endpoint.transport not in driver.supported_transports:
            raise UnsupportedDeviceError(
                f"driver {device.driver_id!r} does not support transport "
                f"{device.endpoint.transport!r}"
            )

        session = await self._connect(transport, device.endpoint)
        async with _session_scope(session):
            yield ConnectedMeter(device, driver, session)

    async def read(
        self,
        device: MeterDevice,
        options: ReadOptions | None = None,
    ) -> ReadResult:
        result = None
        try:
            async with self.open(device) as meter:
                result = await meter.read_records(options)
        except _SessionCloseError as error:
            if result is None or not result.records:
                raise
            cause = error.__cause__
            return replace(
                result,
                completion=CompletionStatus.PARTIAL,
                ended_at=datetime.now(UTC),
                termination_reason="disconnect_failed",
                warnings=(*result.warnings, str(error)),
                diagnostics={
                    **result.diagnostics,
                    "bgmeter.cleanup": {
                        "error_type": type(cause).__name__,
                        "message": str(cause),
                        "previous_completion": result.completion.value,
                        "previous_termination_reason": result.termination_reason,
                        "previous_diagnostic": result.diagnostics.get("bgmeter.cleanup"),
                    },
                },
            )
        return result

    @staticmethod
    async def _connect(
        transport: MeterTransport,
        endpoint: TransportEndpoint,
    ) -> TransportSession:
        try:
            return await transport.connect(endpoint)
        except asyncio.CancelledError:
            raise
        except (MeterConnectionError, UnsupportedDeviceError):
            raise
        except Exception as error:
            raise MeterConnectionError(
                f"{transport.name} connection failed: {error}"
            ) from error


async def discover_meters(
    *,
    manager: MeterManager | None = None,
    timeout: float = 5.0,
) -> tuple[MeterDevice, ...]:
    """Discover supported meters with a supplied or default manager."""

    return await (manager or MeterManager.default()).discover(timeout=timeout)


async def read_meter(
    device: MeterDevice,
    options: ReadOptions | None = None,
    *,
    manager: MeterManager | None = None,
) -> ReadResult:
    """Open, read, and close one meter with a supplied or default manager."""

    return await (manager or MeterManager.default()).read(device, options)


__all__ = ["ConnectedMeter", "MeterManager", "discover_meters", "read_meter"]
