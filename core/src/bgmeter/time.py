"""Interpret timezone-unspecified meter wall-clock values."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tzlocal import get_localzone_name

from .models import MeasurementTime


def interpret_meter_datetime(
    value: datetime,
    timezone_name: str | None = None,
) -> MeasurementTime:
    """Attach a selected IANA zone while preserving the meter's wall clock."""
    if value.tzinfo is not None:
        raise ValueError("meter datetime must not already contain a timezone")

    selected_name = timezone_name or get_localzone_name()
    local_value = value.replace(tzinfo=ZoneInfo(selected_name))
    offset = local_value.utcoffset()
    if offset is None:
        raise ValueError(f"timezone {selected_name!r} has no UTC offset")

    return MeasurementTime(
        meter_datetime=value,
        measured_at_local=local_value,
        measured_at_utc=local_value.astimezone(UTC),
        timezone=selected_name,
        utc_offset_seconds=int(offset.total_seconds()),
    )
