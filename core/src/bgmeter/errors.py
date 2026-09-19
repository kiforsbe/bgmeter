"""Typed failures exposed by the protocol-neutral core."""


class MeterError(Exception):
    """Base class for all meter-core failures."""


class DiscoveryError(MeterError):
    """A transport failed while discovering candidate meters."""


class DeviceNotFoundError(MeterError):
    """A requested meter could not be found."""


class AmbiguousDeviceError(MeterError):
    """A device or driver selection could not be resolved uniquely."""


class UnsupportedDeviceError(MeterError):
    """No compatible driver supports a discovered meter."""


class MeterConnectionError(MeterError):
    """A transport could not connect to or communicate with a meter."""


class MeterTimeoutError(MeterConnectionError):
    """A meter operation exceeded its allowed duration."""


class ProtocolError(MeterError):
    """A meter protocol operation failed."""


class IntegrityError(ProtocolError):
    """A protocol message failed an integrity check."""


class MalformedResponseError(ProtocolError):
    """A meter response could not be parsed safely."""
