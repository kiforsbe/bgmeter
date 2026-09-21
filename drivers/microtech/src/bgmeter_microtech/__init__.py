"""Public MicroTech driver API."""

import logging

from .driver import MicroTechBgmDriver, driver_factory

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "0.3.0"

__all__ = [
    "__version__",
    "MicroTechBgmDriver",
    "driver_factory",
]
