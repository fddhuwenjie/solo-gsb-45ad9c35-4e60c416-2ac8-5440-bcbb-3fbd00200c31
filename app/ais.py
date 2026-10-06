"""Six-bit payload decoding for AIS messages (ITU-R M.1371).

Implements the six-bit armour, bit extraction and decoders for message
types 1/2/3 (class A position report) and 5 (static and voyage data).
"Not available" sentinel values defined by the standard are mapped to
``None`` so they serialise as JSON null.
"""
from __future__ import annotations

from .models import MessageData, PositionReport, StaticAndVoyageData

# ITU-R M.1371 six-bit ASCII table (index 0..63).
SIXBIT_TEXT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
assert len(SIXBIT_TEXT) == 64

POSITION_REPORT_BITS = 168   # message types 1/2/3
STATIC_VOYAGE_BITS = 424     # message type 5

# "Not available" sentinels defined by ITU-R M.1371.
_ROT_NOT_AVAILABLE = -128          # 0x80 as signed 8 bit
_SOG_NOT_AVAILABLE = 1023
_LON_NOT_AVAILABLE = 0x6791AC0     # 181 degrees in 1/10000 minute
_LAT_NOT_AVAILABLE = 0x3412140     # 91 degrees in 1/10000 minute
_COG_NOT_AVAILABLE = 3600
_HEADING_NOT_AVAILABLE = 511
_TIMESTAMP_NOT_AVAILABLE = 60


class AisDecodeError(ValueError):
    """Base class for payload decoding failures."""


class UnsupportedMessageType(AisDecodeError):
    def __init__(self, message_type: int):
        super().__init__(
            f"message type {message_type} is not supported "
            "(supported: 1, 2, 3, 5)")
        self.message_type = message_type


class BitLengthError(AisDecodeError):
    def __init__(self, message_type: int | None, expected: int, actual: int):
        if message_type is None:
            msg = (f"payload holds {actual} bits, at least {expected} bits are "
                   "required to read the message type")
        else:
            msg = (f"message type {message_type} requires exactly {expected} "
                   f"bits, got {actual}")
        super().__init__(msg)
        self.message_type = message_type
        self.expected = expected
        self.actual = actual


def char_value(ch: str) -> int:
    """Six-bit value of one armoured payload character.

    Valid characters are 0x30-0x57 ('0'-'W') and 0x60-0x77 ('`'-'w');
    the gap 0x58-0x5F is rejected even though the naive ``c - 48``
    formula would map it into range.
    """
    code = ord(ch)
    if 0x30 <= code <= 0x57:
        return code - 48
    if 0x60 <= code <= 0x77:
        return code - 56
    raise AisDecodeError(f"invalid six-bit character {ch!r}")


def payload_to_bits(payload: str, fill_bits: int) -> str:
    """Return the payload as a '0101...' string with fill bits removed."""
    bits = "".join(format(char_value(c), "06b") for c in payload)
    if fill_bits:
        if fill_bits >= len(bits):
            raise BitLengthError(None, fill_bits + 1, len(bits))
        bits = bits[:-fill_bits]
    return bits


def _uint(bits: str, start: int, length: int) -> int:
    return int(bits[start:start + length], 2)


def _sint(bits: str, start: int, length: int) -> int:
    value = int(bits[start:start + length], 2)
    if value >= 1 << (length - 1):
        value -= 1 << length
    return value


def _text(bits: str, start: int, length: int) -> str:
    """Decode six-bit ASCII; drop trailing '@' padding and surrounding blanks."""
    chars = [SIXBIT_TEXT[_uint(bits, i, 6)] for i in range(start, start + length, 6)]
    return "".join(chars).rstrip("@").strip()


def decode_payload(payload: str, fill_bits: int) -> MessageData:
    """Decode a fully reassembled payload into a typed message model.

    Raises :class:`BitLengthError` when the bit length does not match the
    message type exactly, and :class:`UnsupportedMessageType` for message
    types other than 1/2/3/5.
    """
    bits = payload_to_bits(payload, fill_bits)
    if len(bits) < 6:
        raise BitLengthError(None, 6, len(bits))
    message_type = _uint(bits, 0, 6)
    if message_type in (1, 2, 3):
        if len(bits) != POSITION_REPORT_BITS:
            raise BitLengthError(message_type, POSITION_REPORT_BITS, len(bits))
        return _decode_position_report(bits, message_type)
    if message_type == 5:
        if len(bits) != STATIC_VOYAGE_BITS:
            raise BitLengthError(message_type, STATIC_VOYAGE_BITS, len(bits))
        return _decode_static_voyage(bits, message_type)
    raise UnsupportedMessageType(message_type)


def _decode_position_report(bits: str, message_type: int) -> PositionReport:
    rot = _sint(bits, 42, 8)
    sog = _uint(bits, 50, 10)
    lon = _sint(bits, 61, 28)
    lat = _sint(bits, 89, 27)
    cog = _uint(bits, 116, 12)
    heading = _uint(bits, 128, 9)
    timestamp = _uint(bits, 137, 6)
    return PositionReport(
        message_type=message_type,
        repeat_indicator=_uint(bits, 6, 2),
        mmsi=_uint(bits, 8, 30),
        navigation_status=_uint(bits, 38, 4),
        rate_of_turn=None if rot == _ROT_NOT_AVAILABLE else rot,
        speed_over_ground=None if sog == _SOG_NOT_AVAILABLE else round(sog / 10.0, 1),
        position_accuracy=bool(_uint(bits, 60, 1)),
        longitude=None if lon == _LON_NOT_AVAILABLE else round(lon / 600000.0, 6),
        latitude=None if lat == _LAT_NOT_AVAILABLE else round(lat / 600000.0, 6),
        course_over_ground=None if cog == _COG_NOT_AVAILABLE else round(cog / 10.0, 1),
        true_heading=None if heading == _HEADING_NOT_AVAILABLE else heading,
        timestamp=None if timestamp == _TIMESTAMP_NOT_AVAILABLE else timestamp,
        maneuver_indicator=_uint(bits, 143, 2),
        raim_flag=bool(_uint(bits, 148, 1)),
        radio_status=_uint(bits, 149, 19),
    )


def _decode_static_voyage(bits: str, message_type: int) -> StaticAndVoyageData:
    imo = _uint(bits, 40, 30)
    ship_type = _uint(bits, 232, 8)
    month = _uint(bits, 274, 4)
    day = _uint(bits, 278, 5)
    hour = _uint(bits, 283, 5)
    minute = _uint(bits, 288, 6)
    draught = _uint(bits, 294, 8)
    return StaticAndVoyageData(
        message_type=message_type,
        repeat_indicator=_uint(bits, 6, 2),
        mmsi=_uint(bits, 8, 30),
        ais_version=_uint(bits, 38, 2),
        imo_number=None if imo == 0 else imo,
        call_sign=_text(bits, 70, 42),
        name=_text(bits, 112, 120),
        ship_type=None if ship_type == 0 else ship_type,
        dimension_to_bow=_uint(bits, 240, 9),
        dimension_to_stern=_uint(bits, 249, 9),
        dimension_to_port=_uint(bits, 258, 6),
        dimension_to_starboard=_uint(bits, 264, 6),
        position_fix_type=_uint(bits, 270, 4),
        eta_month=None if month == 0 else month,
        eta_day=None if day == 0 else day,
        eta_hour=None if hour == 24 else hour,
        eta_minute=None if minute == 60 else minute,
        draught=None if draught == 0 else round(draught / 10.0, 1),
        destination=_text(bits, 302, 120),
        dte=bool(_uint(bits, 422, 1)),
    )
