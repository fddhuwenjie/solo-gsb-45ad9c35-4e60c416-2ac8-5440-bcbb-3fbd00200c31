"""Parsing and validation of raw NMEA 0183 AIS sentences (!AIVDM / !AIVDO).

Validation order for every sentence:
  1. framing ('!' prefix, single '*' checksum delimiter, two hex digits),
  2. XOR checksum over the body between '!' and '*',
  3. field count and talker/sentence type,
  4. fragment count / fragment number consistency,
  5. sequential message id and channel syntax,
  6. six-bit payload character set,
  7. fill bits range (0-5) and zero-fill on non-final fragments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEX2 = re.compile(r"^[0-9A-Fa-f]{2}$")

# Valid six-bit armoured payload characters: 0x30-0x57 and 0x60-0x77.
_PAYLOAD_CHARS = frozenset(
    chr(c) for c in list(range(0x30, 0x58)) + list(range(0x60, 0x78))
)

_SENTENCE_TYPES = ("AIVDM", "AIVDO")


class SentenceError(ValueError):
    """Raised when a raw sentence fails validation."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class Fragment:
    """A validated sentence, ready for grouping and reassembly."""

    source_id: str
    indexes: list[int] = field(default_factory=list)
    sentence_type: str = "VDM"          # VDM | VDO
    total: int = 1
    number: int = 1
    sequence_id: str = ""               # '' when the sentence carries none
    channel: str = ""                   # '' | 'A' | 'B'
    payload: str = ""
    fill_bits: int = 0
    raw: str = ""

    @property
    def is_single(self) -> bool:
        return self.total == 1


def xor_checksum(body: str) -> int:
    """XOR of every character of *body* (the part between '!' and '*')."""
    value = 0
    for ch in body:
        value ^= ord(ch)
    return value


def parse_sentence(raw: str, source_id: str, index: int) -> Fragment:
    """Validate *raw* and return a :class:`Fragment`.

    Raises :class:`SentenceError` with a machine readable ``code`` on any
    validation failure.
    """
    sentence = raw.strip()

    # -- framing -----------------------------------------------------------
    if not sentence.startswith("!"):
        raise SentenceError("bad_prefix", "sentence must start with '!'")
    star = sentence.find("*")
    if star == -1:
        raise SentenceError("missing_checksum", "no '*' checksum delimiter found")
    if star != len(sentence) - 3:
        raise SentenceError(
            "bad_checksum_field",
            "checksum must be exactly two hex digits at the end of the sentence",
        )
    checksum_text = sentence[star + 1:]
    if not _HEX2.match(checksum_text):
        raise SentenceError(
            "bad_checksum_field", f"checksum '{checksum_text}' is not two hex digits")

    # -- XOR checksum --------------------------------------------------------
    body = sentence[1:star]
    expected = int(checksum_text, 16)
    actual = xor_checksum(body)
    if expected != actual:
        raise SentenceError(
            "checksum_mismatch",
            f"XOR checksum mismatch: sentence carries 0x{expected:02X}, "
            f"computed 0x{actual:02X}",
        )

    # -- fields --------------------------------------------------------------
    parts = body.split(",")
    if len(parts) != 7:
        raise SentenceError(
            "bad_field_count", f"expected 7 comma separated fields, got {len(parts)}")
    head, total_s, number_s, seq, channel_s, payload, fill_s = parts

    if head not in _SENTENCE_TYPES:
        raise SentenceError(
            "bad_sentence_type",
            f"expected !AIVDM or !AIVDO, got '!{head}'",
        )

    # -- fragment count / number ---------------------------------------------
    if not (total_s.isdigit() and len(total_s) == 1 and 1 <= int(total_s) <= 9):
        raise SentenceError(
            "bad_fragment_count", f"fragment count must be a digit 1-9, got '{total_s}'")
    total = int(total_s)
    if not (number_s.isdigit() and len(number_s) == 1 and 1 <= int(number_s) <= 9):
        raise SentenceError(
            "bad_fragment_number",
            f"fragment number must be a digit 1-9, got '{number_s}'")
    number = int(number_s)
    if number > total:
        raise SentenceError(
            "bad_fragment_number",
            f"fragment number {number} exceeds fragment count {total}",
        )

    # -- sequential message id -------------------------------------------------
    if seq and not (len(seq) == 1 and seq.isdigit()):
        raise SentenceError(
            "bad_sequence_id",
            f"sequential message id must be empty or a digit 0-9, got '{seq}'",
        )

    # -- channel ---------------------------------------------------------------
    channel = channel_s.upper()
    if channel == "1":
        channel = "A"
    elif channel == "2":
        channel = "B"
    if channel not in ("", "A", "B"):
        raise SentenceError(
            "bad_channel", f"channel must be 'A' or 'B', got '{channel_s}'")

    # -- payload -----------------------------------------------------------------
    if not payload:
        raise SentenceError("empty_payload", "encapsulated payload is empty")
    bad = sorted({c for c in payload if c not in _PAYLOAD_CHARS})
    if bad:
        raise SentenceError(
            "bad_payload_character",
            f"payload contains invalid six-bit character(s): {''.join(bad)!r}",
        )

    # -- fill bits -----------------------------------------------------------------
    if not (fill_s.isdigit() and len(fill_s) == 1):
        raise SentenceError(
            "bad_fill_bits", f"fill bits must be a digit 0-5, got '{fill_s}'")
    fill = int(fill_s)
    if fill > 5:
        raise SentenceError(
            "bad_fill_bits", f"fill bits must be between 0 and 5, got {fill}")
    if number < total and fill != 0:
        raise SentenceError(
            "bad_fill_bits",
            "non-final fragments must carry 0 fill bits so payloads align "
            "on six-bit boundaries",
        )

    return Fragment(
        source_id=source_id,
        indexes=[index],
        sentence_type="VDO" if head == "AIVDO" else "VDM",
        total=total,
        number=number,
        sequence_id=seq,
        channel=channel,
        payload=payload,
        fill_bits=fill,
        raw=sentence,
    )
