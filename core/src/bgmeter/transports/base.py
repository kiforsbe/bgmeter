"""Protocol-neutral transport and connected-session contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import TransportEndpoint


NotificationCallback = Callable[[object, bytes], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class GattCharacteristic:
    """One characteristic instance in a connected service (UUIDs are lowercase)."""

    uuid: str
    handle: int
    properties: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "uuid", self.uuid.lower())
        object.__setattr__(self, "properties", frozenset(self.properties))


@dataclass(frozen=True, slots=True)
class GattService:
    """One connected service instance, retaining its characteristic membership."""

    uuid: str
    handle: int
    characteristics: tuple[GattCharacteristic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "uuid", self.uuid.lower())
        object.__setattr__(self, "characteristics", tuple(self.characteristics))


@runtime_checkable
class TransportSession(Protocol):
    """A connected endpoint with an explicit asynchronous lifecycle."""

    transport: str
    endpoint: TransportEndpoint

    async def close(self) -> None: ...


@runtime_checkable
class GattSession(TransportSession, Protocol):
    """A connected GATT inventory and generic byte operations."""

    @property
    def services(self) -> tuple[GattService, ...]: ...

    async def read_gatt_char(self, characteristic: str) -> bytes: ...

    async def write_gatt_char(
        self,
        characteristic: str,
        data: bytes,
        *,
        response: bool | None = None,
    ) -> None: ...

    async def start_notify(
        self,
        characteristic: str,
        callback: NotificationCallback,
    ) -> None: ...

    async def stop_notify(self, characteristic: str) -> None: ...


@runtime_checkable
class MeterTransport(Protocol):
    """Discovers and connects to endpoints for one transport kind."""

    name: str

    async def discover(
        self,
        *,
        timeout: float = 5.0,
    ) -> tuple[TransportEndpoint, ...]: ...

    async def connect(self, endpoint: TransportEndpoint) -> TransportSession: ...


__all__ = [
    "GattCharacteristic",
    "GattService",
    "GattSession",
    "MeterTransport",
    "NotificationCallback",
    "TransportSession",
]
