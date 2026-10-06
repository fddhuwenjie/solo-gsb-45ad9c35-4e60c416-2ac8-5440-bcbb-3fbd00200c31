"""Unit tests for six-bit payload decoding (app.ais)."""
from __future__ import annotations

import pytest

from app.ais import (
    BitLengthError,
    UnsupportedMessageType,
    char_value,
    decode_payload,
    payload_to_bits,
)
from app.models import PositionReport, StaticAndVoyageData
from tests.conftest import (
    MULTI_PAYLOAD,
    SINGLE_PAYLOAD,
    VDO_SENTINELS_PAYLOAD,
)


def test_char_value_mapping():
    assert char_value("0") == 0
    assert char_value("9") == 9
    assert char_value("@") == 16  # 0x40 -> 64 - 48 = 16
    assert char_value("W") == 39
    assert char_value("`") == 40
    assert char_value("w") == 63
    with pytest.raises(ValueError):
        char_value("X")  # 0x58 is outside the armoured alphabet


def test_payload_to_bits_strips_fill_bits():
    bits = payload_to_bits("01", 0)
    assert len(bits) == 12
    assert payload_to_bits("01", 2) == bits[:-2]


def test_decode_message_1():
    msg = decode_payload(SINGLE_PAYLOAD, 0)
    assert isinstance(msg, PositionReport)
    assert msg.message_type == 1
    assert msg.mmsi == 366999999
    assert msg.longitude == pytest.approx(-73.9835)
    assert msg.latitude == pytest.approx(40.7484)
    assert msg.speed_over_ground == pytest.approx(8.4)
    assert msg.course_over_ground == pytest.approx(121.5)
    assert msg.true_heading == 119
    assert msg.timestamp == 34
    assert msg.navigation_status == 0


def test_decode_message_1_not_available_sentinels_become_null():
    msg = decode_payload(VDO_SENTINELS_PAYLOAD, 0)
    assert isinstance(msg, PositionReport)
    assert msg.speed_over_ground is None   # raw 1023
    assert msg.longitude is None           # raw 181 degrees
    assert msg.course_over_ground is None  # raw 3600
    assert msg.true_heading is None        # raw 511
    assert msg.timestamp is None           # raw 60
    assert msg.latitude == pytest.approx(40.7484)


def test_decode_message_5():
    msg = decode_payload(MULTI_PAYLOAD, 2)
    assert isinstance(msg, StaticAndVoyageData)
    assert msg.message_type == 5
    assert msg.mmsi == 366999999
    assert msg.imo_number == 9074729
    assert msg.call_sign == "WDC1234"
    assert msg.name == "MV CLAUDE"
    assert msg.ship_type == 70
    assert msg.dimension_to_bow == 120
    assert msg.dimension_to_stern == 30
    assert msg.dimension_to_port == 10
    assert msg.dimension_to_starboard == 12
    assert msg.eta_month == 9
    assert msg.eta_day == 25
    assert msg.eta_hour == 14
    assert msg.eta_minute == 30
    assert msg.draught == pytest.approx(8.5)
    assert msg.destination == "NEW YORK"


def test_message_1_rejects_wrong_bit_length():
    # one character short -> 162 bits instead of 168
    with pytest.raises(BitLengthError) as excinfo:
        decode_payload(SINGLE_PAYLOAD[:-1], 0)
    assert excinfo.value.expected == 168
    assert excinfo.value.actual == 162


def test_message_5_rejects_wrong_bit_length():
    with pytest.raises(BitLengthError) as excinfo:
        decode_payload(MULTI_PAYLOAD[:-1], 0)
    assert excinfo.value.expected == 424


def test_payload_too_short_for_message_type():
    with pytest.raises(BitLengthError):
        decode_payload("0", 5)  # a single bit left


def test_unsupported_message_type():
    # type 18 payload: 18 = 0b010010 -> armoured char for value 18 is '2'... build bits directly
    bits = format(18, "06b") + "0" * 162  # 168 bits of a type-18 message
    fill = (-len(bits)) % 6
    bits += "0" * fill
    payload = ""
    for i in range(0, len(bits), 6):
        v = int(bits[i:i + 6], 2)
        payload += chr(v + 48) if v < 40 else chr(v + 56)
    with pytest.raises(UnsupportedMessageType) as excinfo:
        decode_payload(payload, 0)
    assert excinfo.value.message_type == 18
