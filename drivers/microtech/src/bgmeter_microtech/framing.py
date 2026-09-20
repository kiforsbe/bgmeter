"""MicroTech LibFrame checksums, escaping, and fragment assembly."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


DELIMITER = b"\x2d\x2d"
ESCAPE = 0x2F


@dataclass(frozen=True, slots=True)
class TransportFragment:
    """One validated fragment from the MicroTech notification channel."""

    offset: int
    final: bool
    sequence: int
    payload: bytes


def crc8_dallas(data: bytes, initial: int = 0) -> int:
    """Return the Dallas/Maxim CRC-8 used by the transport header."""
    crc = initial
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8C if crc & 1 else 0)
    return crc & 0xFF


def crc16_modbus(data: bytes, initial: int = 0xFFFF) -> int:
    """Return the Modbus CRC-16 used by the inner request packet."""
    crc = initial
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc & 0xFFFF


def _escape_transport_body(data: bytes) -> bytes:
    escaped = bytearray()
    for value in data:
        if value in (0x2D, ESCAPE):
            escaped.append(ESCAPE)
        escaped.append(value)
    return bytes(escaped)


def _unescape_transport_body(data: bytes) -> bytes:
    result = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        if value == ESCAPE:
            index += 1
            if index >= len(data) or data[index] not in (0x2D, ESCAPE):
                raise ValueError("invalid MicroTech escape sequence")
            value = data[index]
        result.append(value)
        index += 1
    return bytes(result)


def decode_transport_fragment(frame: bytes) -> TransportFragment:
    """Validate and decode one delimited MicroTech transport fragment."""
    if (
        len(frame) < 10
        or not frame.startswith(DELIMITER)
        or not frame.endswith(DELIMITER)
    ):
        raise ValueError("not a delimited MicroTech transport fragment")

    raw = _unescape_transport_body(frame[2:-2])
    if len(raw) < 6:
        raise ValueError("transport fragment header is truncated")
    if raw[2] != len(raw):
        raise ValueError(
            f"transport length mismatch: header={raw[2]}, actual={len(raw)}"
        )
    if crc8_dallas(raw[:5]) != raw[5]:
        raise ValueError("transport header CRC-8 mismatch")

    flags = raw[4]
    return TransportFragment(
        offset=raw[3],
        final=bool(flags & 0x80),
        sequence=flags & 0x3F,
        payload=raw[6:],
    )


def reassemble_transport_fragments(
    frames: Iterable[bytes | TransportFragment],
) -> bytes | None:
    """Reassemble complete fragments by byte offset, independent of arrival order."""
    fragments = tuple(
        item if isinstance(item, TransportFragment) else decode_transport_fragment(item)
        for item in frames
    )
    if not fragments:
        return None

    chunks: dict[int, bytes] = {}
    final_length: int | None = None
    for fragment in fragments:
        previous = chunks.get(fragment.offset)
        if previous is not None and previous != fragment.payload:
            raise ValueError(
                f"conflicting transport fragments at offset {fragment.offset}"
            )
        chunks[fragment.offset] = fragment.payload
        if fragment.final:
            length = fragment.offset + len(fragment.payload)
            if final_length is not None and final_length != length:
                raise ValueError("conflicting final transport lengths")
            final_length = length

    if final_length is None:
        return None

    result = bytearray(final_length)
    covered = bytearray(final_length)
    for offset, payload in chunks.items():
        end = offset + len(payload)
        if end > final_length:
            raise ValueError("transport fragment extends beyond final length")
        for position, value in enumerate(payload, start=offset):
            if covered[position] and result[position] != value:
                raise ValueError("overlapping transport fragments conflict")
            result[position] = value
            covered[position] = 1

    if not all(covered):
        return None
    return bytes(result)


_DELIMITER_BYTE = DELIMITER[0]
_MAX_BUFFERED_BYTES = 1024


class TransportFrameAssembler:
    """Rebuild delimited transport frames from a stream of BLE notifications.

    A notification holds at most 20 bytes, but escaping can push a frame past that
    (each literal ``2d`` or ``2f`` body byte costs an extra byte), so the meter
    then delivers one frame in several notifications. Feed notifications in
    arrival order; complete frames come back as they finish.

    A notification that opens with a delimiter starts a new frame, except when the
    frame in progress already has its declared length and only lacks its closing
    delimiter. That keeps a frame whose tail was lost from swallowing the next
    one. Bytes that do not form a delimited frame come back as they are, so the
    caller can record them as invalid.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[bytes, ...]:
        """Add one notification and return every frame it completes."""
        data = bytes(data)
        frames: list[bytes] = []
        if (
            self._buffer
            and data.startswith(DELIMITER)
            and not self._awaiting_closing_delimiter()
        ):
            frames.append(bytes(self._buffer))
            self._buffer.clear()
        self._buffer += data
        frames.extend(self._drain())
        return tuple(frames)

    def flush(self) -> tuple[bytes, ...]:
        """Return and forget any incomplete leftover bytes."""
        if not self._buffer:
            return ()
        leftover = bytes(self._buffer)
        self._buffer.clear()
        return (leftover,)

    @property
    def pending_byte_count(self) -> int:
        return len(self._buffer)

    def _awaiting_closing_delimiter(self) -> bool:
        buffer = self._buffer
        if not buffer.startswith(DELIMITER):
            return False
        body = bytearray()
        index = 2
        while index < len(buffer):
            value = buffer[index]
            if value == ESCAPE:
                if index + 1 >= len(buffer):
                    return False
                body.append(buffer[index + 1])
                index += 2
            elif value == _DELIMITER_BYTE:
                # Half the closing delimiter is already here; the other half is a
                # single byte, so a notification opening with a delimiter is new.
                return False
            else:
                body.append(value)
                index += 1
        return len(body) >= 3 and len(body) == body[2]

    @staticmethod
    def _frame_end(buffer: bytearray) -> int | None:
        index = 2
        while index < len(buffer):
            value = buffer[index]
            if value == ESCAPE:
                index += 2
                continue
            if value == _DELIMITER_BYTE:
                if index + 1 >= len(buffer):
                    return None
                if buffer[index + 1] == _DELIMITER_BYTE:
                    return index + 2
            index += 1
        return None

    def _drain(self) -> list[bytes]:
        frames: list[bytes] = []
        buffer = self._buffer
        while buffer:
            if not buffer.startswith(DELIMITER):
                if len(buffer) == 1 and buffer[0] == _DELIMITER_BYTE:
                    break
                cut = buffer.find(DELIMITER, 1)
                if cut < 0:
                    cut = len(buffer)
                frames.append(bytes(buffer[:cut]))
                del buffer[:cut]
                continue
            end = self._frame_end(buffer)
            if end is None:
                if len(buffer) > _MAX_BUFFERED_BYTES:
                    frames.append(bytes(buffer))
                    buffer.clear()
                break
            frames.append(bytes(buffer[:end]))
            del buffer[:end]
        return frames


def build_official_transport_frame(
    payload: bytes,
    sequence: int = 0,
    *,
    command: int,
    message_type: int = 2,
    acknowledgement: int = 1,
) -> bytes:
    """Apply the verified DevComm and LibFrame request envelopes."""
    if not 0 <= sequence <= 0x3F:
        raise ValueError("sequence must be between 0 and 63")
    if not all(
        0 <= value <= 0xFF
        for value in (command, message_type, acknowledgement)
    ):
        raise ValueError("transport command fields must fit in one byte")

    inner_header = bytes((0x01, command, 0x00, 0x00))
    message = bytes((message_type, acknowledgement)) + bytes(payload)
    inner_crc = crc16_modbus(inner_header + message).to_bytes(2, "little")
    inner_packet = inner_header + inner_crc + message

    packet_length = 6 + len(inner_packet)
    if packet_length > 0xFF:
        raise ValueError("transport packet is too long")
    outer_header = bytes((0x00, 0x00, packet_length, 0x00, 0x80 | sequence))
    raw_packet = outer_header + bytes((crc8_dallas(outer_header),)) + inner_packet
    return DELIMITER + _escape_transport_body(raw_packet) + DELIMITER


def build_history_request(record_number: int, sequence: int = 0) -> bytes:
    """Build the official command-0x05 indexed history request."""
    if not 0 <= record_number <= 0xFFFF:
        raise ValueError("record_number must fit in an unsigned 16-bit value")
    return build_official_transport_frame(
        record_number.to_bytes(2, "big"),
        sequence,
        command=0x05,
    )


def build_device_info_request(sequence: int = 0) -> bytes:
    """Build the official command-zero session initialization request."""
    return build_official_transport_frame(
        b"",
        sequence,
        command=0,
        acknowledgement=0,
    )
