"""Unit tests for NMEA 0183 sentence validation (app.nmea)."""
from __future__ import annotations

import pytest

from app.nmea import SentenceError, parse_sentence, xor_checksum
from tests.conftest import MULTI_FRAG1, SINGLE_FRAGMENT, make_sentence


def test_parse_valid_single_fragment():
    frag = parse_sentence(SINGLE_FRAGMENT, "src-1", 0)
    assert frag.source_id == "src-1"
    assert frag.indexes == [0]
    assert frag.sentence_type == "VDM"
    assert frag.total == 1
    assert frag.number == 1
    assert frag.sequence_id == ""
    assert frag.channel == "A"
    assert frag.payload == "15Mwqgh01DreE9HGD@44gkg40000"
    assert frag.fill_bits == 0
    assert frag.is_single


def test_parse_valid_multi_fragment():
    frag = parse_sentence(MULTI_FRAG1, "src-1", 3)
    assert frag.total == 2
    assert frag.number == 1
    assert frag.sequence_id == "4"
    assert not frag.is_single


def test_vdo_sentence_type():
    body = "AIVDO,1,1,,B,15Mwqgh01DreE9HGD@44gkg40000,0"
    frag = parse_sentence(make_sentence(body), "src", 0)
    assert frag.sentence_type == "VDO"
    assert frag.channel == "B"


def test_checksum_mismatch():
    bad = SINGLE_FRAGMENT.replace("15Mwqgh", "15Mwqgi")
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(bad, "src", 0)
    assert excinfo.value.code == "checksum_mismatch"


def test_missing_checksum():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence("!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0", "src", 0)
    assert excinfo.value.code == "missing_checksum"


def test_checksum_must_be_two_trailing_hex_digits():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(SINGLE_FRAGMENT + "FF", "src", 0)
    assert excinfo.value.code == "bad_checksum_field"


def test_bad_prefix():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence("$AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*00", "src", 0)
    assert excinfo.value.code == "bad_prefix"


def test_bad_sentence_type():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("BSVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0"), "src", 0)
    assert excinfo.value.code == "bad_sentence_type"


def test_bad_field_count():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,1,1,A,15Mwqgh01DreE9HGD@44gkg40000,0"), "src", 0)
    assert excinfo.value.code == "bad_field_count"


def test_bad_fragment_count_zero():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,0,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0"), "src", 0)
    assert excinfo.value.code == "bad_fragment_count"


def test_fragment_number_exceeds_count():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,2,3,4,A,15Mwqgh01DreE9HGD@44gkg40000,0"), "src", 0)
    assert excinfo.value.code == "bad_fragment_number"


def test_bad_sequence_id():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,2,1,12,A,15Mwqgh01DreE9HGD@44gkg40000,0"), "src", 0)
    assert excinfo.value.code == "bad_sequence_id"


def test_channel_digit_normalised_to_letter():
    frag = parse_sentence(make_sentence("AIVDM,1,1,,1,15Mwqgh01DreE9HGD@44gkg40000,0"), "src", 0)
    assert frag.channel == "A"


def test_bad_channel():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,1,1,,C,15Mwqgh01DreE9HGD@44gkg40000,0"), "src", 0)
    assert excinfo.value.code == "bad_channel"


def test_empty_payload():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,1,1,,A,,0"), "src", 0)
    assert excinfo.value.code == "empty_payload"


def test_invalid_payload_character():
    # 'X' (0x58) sits in the forbidden gap of the six-bit armour table.
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,1,1,,A,15MwqghX1DreE9HGD@44gkg40000,0"), "src", 0)
    assert excinfo.value.code == "bad_payload_character"


def test_fill_bits_out_of_range():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,6"), "src", 0)
    assert excinfo.value.code == "bad_fill_bits"


def test_non_final_fragment_must_have_zero_fill():
    with pytest.raises(SentenceError) as excinfo:
        parse_sentence(make_sentence("AIVDM,2,1,4,A,15Mwqgh01DreE9HGD@44gkg40000,1"), "src", 0)
    assert excinfo.value.code == "bad_fill_bits"


def test_xor_checksum_helper():
    assert xor_checksum("AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0") == 0x1C
