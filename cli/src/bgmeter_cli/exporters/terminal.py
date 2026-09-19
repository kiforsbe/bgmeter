"""Human-oriented terminal rendering."""

from __future__ import annotations

from decimal import Decimal

from bgmeter import ReadResult


def render_terminal(result: ReadResult, *, show_raw: bool = False) -> str:
    lines = [
        f"Meter: {result.device.selector} ({result.device.driver_id})",
        (
            f"Retrieval: {result.completion.value}; "
            f"received {result.received_count}"
            + (
                f" of {result.expected_count}"
                if result.expected_count is not None
                else ""
            )
        ),
    ]
    for record in result.records:
        local = record.measured_at.measured_at_local.isoformat()
        utc = record.measured_at.measured_at_utc.isoformat()
        value = record.mmol_l.quantize(Decimal("0.1"))
        lines.append(
            f"{record.record_id}: {value} mmol/L | local {local} | UTC {utc}"
        )
        if show_raw:
            request = record.raw.request.hex() if record.raw.request is not None else ""
            fragments = ",".join(item.hex() for item in record.raw.fragments)
            response = record.raw.response.hex() if record.raw.response is not None else ""
            native_record = (
                record.raw.record.hex() if record.raw.record is not None else ""
            )
            lines.append(
                "  raw: "
                f"request={request} fragments={fragments} "
                f"response={response} record={native_record}"
            )
    for warning in result.warnings:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines) + "\n"


__all__ = ["render_terminal"]
