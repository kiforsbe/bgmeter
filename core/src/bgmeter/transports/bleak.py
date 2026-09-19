"""Bleak-backed implementation of the generic BLE/GATT transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from inspect import isawaitable
from typing import Any

from bleak import BleakClient, BleakScanner

from ..errors import (
    DiscoveryError,
    MeterConnectionError,
    MeterTimeoutError,
    UnsupportedDeviceError,
)
from ..models import TransportEndpoint
from .base import GattCharacteristic, GattService, GattSession, NotificationCallback


def _connection_error(action: str, error: Exception) -> MeterConnectionError:
    if isinstance(error, MeterConnectionError):
        return error
    if isinstance(error, TimeoutError):
        return MeterTimeoutError(f"BLE {action} timed out: {error}")
    return MeterConnectionError(f"BLE {action} failed: {error}")


class _BleakGattSession:
    transport = "ble"

    def __init__(self, endpoint: TransportEndpoint, client: Any) -> None:
        self.endpoint = endpoint
        self._client = client
        self._closed = False
        self._services = tuple(
            GattService(
                service.uuid,
                service.handle,
                tuple(
                    GattCharacteristic(char.uuid, char.handle, char.properties)
                    for char in service.characteristics
                ),
            )
            for service in client.services
        )

    @property
    def services(self) -> tuple[GattService, ...]:
        return self._services

    async def read_gatt_char(self, characteristic: str) -> bytes:
        try:
            return bytes(await self._client.read_gatt_char(characteristic))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _connection_error("read", error) from error

    async def write_gatt_char(
        self,
        characteristic: str,
        data: bytes,
        *,
        response: bool | None = None,
    ) -> None:
        try:
            await self._client.write_gatt_char(
                characteristic,
                data,
                response=response,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _connection_error("write", error) from error

    async def start_notify(
        self,
        characteristic: str,
        callback: NotificationCallback,
    ) -> None:
        async def adapt_notification(sender: object, data: bytearray) -> None:
            result = callback(sender, bytes(data))
            if isawaitable(result):
                await result

        try:
            await self._client.start_notify(characteristic, adapt_notification)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _connection_error("notification subscription", error) from error

    async def stop_notify(self, characteristic: str) -> None:
        try:
            await self._client.stop_notify(characteristic)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _connection_error("notification removal", error) from error

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _connection_error("disconnect", error) from error
        self._closed = True


ScannerFactory = Callable[[], Any]
ClientFactory = Callable[[object], Any]


class BleTransport:
    """Discover BLE advertisements and expose connected generic GATT sessions."""

    name = "ble"

    def __init__(
        self,
        *,
        scanner_factory: ScannerFactory = BleakScanner,
        client_factory: ClientFactory = BleakClient,
    ) -> None:
        self._scanner_factory = scanner_factory
        self._client_factory = client_factory

    async def discover(
        self,
        *,
        timeout: float = 5.0,
    ) -> tuple[TransportEndpoint, ...]:
        try:
            scanner = self._scanner_factory()
            try:
                await scanner.start()
                await asyncio.sleep(timeout)
            except BaseException as primary:
                try:
                    await scanner.stop()
                except BaseException as cleanup:
                    primary.add_note(
                        "Scanner cleanup also failed: "
                        f"{type(cleanup).__name__}: {cleanup}"
                    )
                raise
            else:
                await scanner.stop()
            advertisements = scanner.discovered_devices_and_advertisement_data
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise DiscoveryError(f"BLE discovery failed: {error}") from error

        endpoints = [
            self._endpoint_from_advertisement(device, advertisement)
            for device, advertisement in advertisements.values()
        ]
        return tuple(sorted(endpoints, key=lambda endpoint: endpoint.identifier))

    async def connect(self, endpoint: TransportEndpoint) -> GattSession:
        if endpoint.transport != self.name:
            raise UnsupportedDeviceError(
                f"BLE transport cannot open {endpoint.transport!r} endpoint"
            )

        target = endpoint.handle if endpoint.handle is not None else endpoint.identifier
        try:
            client = self._client_factory(target)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise _connection_error("connection", error) from error
        try:
            await client.connect()
            return _BleakGattSession(endpoint, client)
        except BaseException as error:
            try:
                await client.disconnect()
            except BaseException:
                pass
            if isinstance(error, asyncio.CancelledError):
                raise
            if isinstance(error, Exception):
                raise _connection_error("connection", error) from error
            raise

    @staticmethod
    def _endpoint_from_advertisement(
        device: Any,
        advertisement: Any,
    ) -> TransportEndpoint:
        metadata = {
            "manufacturer_data": getattr(advertisement, "manufacturer_data", {}),
            "service_data": getattr(advertisement, "service_data", {}),
            "tx_power": getattr(advertisement, "tx_power", None),
            "rssi": getattr(advertisement, "rssi", None),
        }
        return TransportEndpoint(
            transport="ble",
            identifier=device.address,
            name=getattr(advertisement, "local_name", None)
            or getattr(device, "name", None),
            service_uuids=frozenset(
                getattr(advertisement, "service_uuids", ()) or ()
            ),
            metadata=metadata,
            handle=device,
        )


__all__ = ["BleTransport"]
