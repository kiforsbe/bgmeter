"""Public API for the protocol-neutral bgmeter core."""

import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())

from .drivers import (
    DRIVER_API_VERSION,
    DriverDescriptor,
    DriverFactory,
    DriverRegistry,
    MeterDriver,
)
from .errors import (
    AmbiguousDeviceError,
    DeviceNotFoundError,
    DiscoveryError,
    IntegrityError,
    MalformedResponseError,
    MeterConnectionError,
    MeterError,
    MeterTimeoutError,
    ProtocolError,
    UnsupportedDeviceError,
)
from .manager import ConnectedMeter, MeterManager, discover_meters, read_meter
from .models import (
    CompletionStatus,
    DriverMatch,
    GlucoseRecord,
    MeasurementTime,
    MetadataValue,
    MeterDevice,
    MeterIdentity,
    RawCapture,
    ReadOptions,
    ReadResult,
    TransportEndpoint,
)
from .progress import ProgressCallback, ProgressEvent, ProgressLevel, emit_progress
from .time import interpret_meter_datetime
from .transports import (
    BleTransport, GattCharacteristic, GattService, GattSession, MeterTransport,
    NotificationCallback, TransportSession,
)

__version__ = "0.3.0"

__all__ = [
    "DRIVER_API_VERSION",
    "AmbiguousDeviceError",
    "BleTransport",
    "CompletionStatus",
    "ConnectedMeter",
    "DeviceNotFoundError",
    "DiscoveryError",
    "DriverDescriptor",
    "DriverFactory",
    "DriverMatch",
    "DriverRegistry",
    "GlucoseRecord",
    "GattSession",
    "GattCharacteristic",
    "GattService",
    "IntegrityError",
    "MalformedResponseError",
    "MeasurementTime",
    "MetadataValue",
    "MeterConnectionError",
    "MeterDevice",
    "MeterDriver",
    "MeterError",
    "MeterIdentity",
    "MeterManager",
    "MeterTransport",
    "MeterTimeoutError",
    "NotificationCallback",
    "ProgressCallback",
    "ProgressEvent",
    "ProgressLevel",
    "ProtocolError",
    "RawCapture",
    "ReadOptions",
    "ReadResult",
    "TransportEndpoint",
    "TransportSession",
    "UnsupportedDeviceError",
    "discover_meters",
    "emit_progress",
    "interpret_meter_datetime",
    "read_meter",
]
