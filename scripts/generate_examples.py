"""Generate the AIS sentences used in README.md and the test-suite.

The script builds message 1 and message 5 payloads bit by bit, applies the
six-bit armour, splits multi-fragment messages and computes NMEA checksums,
so every example sentence is guaranteed to be self-consistent.  Run:

    python scripts/generate_examples.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ais import SIXBIT_TEXT  # noqa: E402


# ---------------------------------------------------------------------------
# bit helpers
# ---------------------------------------------------------------------------

def ubits(value: int, length: int) -> str:
    assert 0 <= value < (1 << length), (value, length)
    return format(value, f"0{length}b")


def sbits(value: int, length: int) -> str:
    assert -(1 << (length - 1)) <= value < (1 << (length - 1)), (value, length)
    if value < 0:
        value += 1 << length
    return format(value, f"0{length}b")


def text_bits(text: str, length: int) -> str:
    text = text.upper()
    out = ""
    for ch in text:
        index = SIXBIT_TEXT.find(ch)
        assert index != -1, f"character {ch!r} not encodable"
        out += format(index, "06b")
    assert len(out) <= length
    return out + "0" * (length - len(out))  # pad with '@' (value 0)


def armor(bits: str) -> tuple[str, int]:
    """Six-bit armour a bit string; returns (payload, fill_bits)."""
    fill = (-len(bits)) % 6
    bits = bits + "0" * fill
    chars = []
    for i in range(0, len(bits), 6):
        value = int(bits[i:i + 6], 2)
        chars.append(chr(value + 48) if value < 40 else chr(value + 56))
    return "".join(chars), fill


def checksum(body: str) -> str:
    value = 0
    for ch in body:
        value ^= ord(ch)
    return f"{value:02X}"


def sentence(total: int, number: int, seq: str, channel: str,
             payload: str, fill: int, vdo: bool = False) -> str:
    body = f"AI{'VDO' if vdo else 'VDM'},{total},{number},{seq},{channel},{payload},{fill}"
    return f"!{body}*{checksum(body)}"


# ---------------------------------------------------------------------------
# message builders
# ---------------------------------------------------------------------------

def msg1_bits(mmsi: int, lon: float, lat: float, sog: float, cog: float,
              heading: int, nav_status: int = 0, rot: int = 0,
              timestamp: int = 60) -> str:
    bits = (
        ubits(1, 6) + ubits(0, 2) + ubits(mmsi, 30) + ubits(nav_status, 4)
        + sbits(rot, 8) + ubits(round(sog * 10), 10) + ubits(1, 1)
        + sbits(round(lon * 600000), 28) + sbits(round(lat * 600000), 27)
        + ubits(round(cog * 10), 12) + ubits(heading, 9) + ubits(timestamp, 6)
        + ubits(0, 2) + ubits(0, 3) + ubits(0, 1) + ubits(0, 19)
    )
    assert len(bits) == 168
    return bits


def msg5_bits(mmsi: int, imo: int, callsign: str, name: str, ship_type: int,
              bow: int, stern: int, port: int, starboard: int,
              month: int, day: int, hour: int, minute: int,
              draught: float, destination: str) -> str:
    bits = (
        ubits(5, 6) + ubits(0, 2) + ubits(mmsi, 30) + ubits(0, 2)
        + ubits(imo, 30) + text_bits(callsign, 42) + text_bits(name, 120)
        + ubits(ship_type, 8) + ubits(bow, 9) + ubits(stern, 9)
        + ubits(port, 6) + ubits(starboard, 6) + ubits(1, 4)
        + ubits(month, 4) + ubits(day, 5) + ubits(hour, 5) + ubits(minute, 6)
        + ubits(round(draught * 10), 8) + text_bits(destination, 120)
        + ubits(0, 1) + ubits(0, 1)
    )
    assert len(bits) == 424
    return bits


def split_payload(payload: str, fill: int, first_len: int = 48) -> list[tuple[str, int]]:
    """Split an armoured payload into two fragments (fill on the last one)."""
    return [(payload[:first_len], 0), (payload[first_len:], fill)]


# ---------------------------------------------------------------------------
# examples
# ---------------------------------------------------------------------------

def main() -> None:
    out: dict[str, object] = {}

    # 1. single-fragment message 1 ------------------------------------------
    payload, fill = armor(msg1_bits(
        mmsi=366999999, lon=-73.9835, lat=40.7484,
        sog=8.4, cog=121.5, heading=119, nav_status=0, timestamp=34,
    ))
    out["single_fragment"] = {
        "sentences": [{"source_id": "ais-receiver-1",
                       "sentence": sentence(1, 1, "", "A", payload, fill)}],
    }

    # 1b. single-fragment !AIVDO with "not available" sentinels --------------
    payload, fill = armor(msg1_bits(
        mmsi=366999999, lon=181.0, lat=40.7484,
        sog=102.3, cog=360.0, heading=511, timestamp=60,
    ))
    # patch raw sentinel values: sog 1023, cog 3600, lon 181deg, heading 511
    bits = msg1_bits(mmsi=366999999, lon=181.0, lat=40.7484,
                     sog=102.3, cog=360.0, heading=511, timestamp=60)
    payload, fill = armor(bits)
    out["single_fragment_vdo_sentinels"] = {
        "sentences": [{"source_id": "ais-receiver-1",
                       "sentence": sentence(1, 1, "", "B", payload, fill, vdo=True)}],
    }

    # 2. two-fragment message 5, delivered out of order ----------------------
    payload, fill = armor(msg5_bits(
        mmsi=366999999, imo=9074729, callsign="WDC1234", name="MV CLAUDE",
        ship_type=70, bow=120, stern=30, port=10, starboard=12,
        month=9, day=25, hour=14, minute=30, draught=8.5,
        destination="NEW YORK",
    ))
    (p1, f1), (p2, f2) = split_payload(payload, fill)
    frag1 = sentence(2, 1, "4", "A", p1, f1)
    frag2 = sentence(2, 2, "4", "A", p2, f2)
    out["multi_fragment"] = {
        "sentences": [
            {"source_id": "ais-receiver-1", "sentence": frag2},  # out of order
            {"source_id": "ais-receiver-1", "sentence": frag1},
        ],
    }

    # 3. ambiguous pool: two fragment-1s, one shared fragment-2, no seq id ---
    payload_a, fill_a = armor(msg5_bits(
        mmsi=366000001, imo=9074701, callsign="AAAA111", name="NORTHERN STAR",
        ship_type=70, bow=100, stern=20, port=8, starboard=8,
        month=10, day=1, hour=8, minute=0, draught=7.2, destination="BOSTON",
    ))
    payload_b, fill_b = armor(msg5_bits(
        mmsi=366000002, imo=9074702, callsign="BBBB222", name="SOUTHERN CROSS",
        ship_type=80, bow=90, stern=25, port=7, starboard=9,
        month=11, day=2, hour=9, minute=15, draught=6.4, destination="MIAMI",
    ))
    (a1, af1), (a2, af2) = split_payload(payload_a, fill_a)
    (b1, bf1), (_b2, _bf2) = split_payload(payload_b, fill_b)
    out["ambiguous"] = {
        "sentences": [
            {"source_id": "ais-receiver-2", "sentence": sentence(2, 1, "", "B", a1, af1)},
            {"source_id": "ais-receiver-2", "sentence": sentence(2, 1, "", "B", b1, bf1)},
            {"source_id": "ais-receiver-2", "sentence": sentence(2, 2, "", "B", a2, af2)},
        ],
    }

    # 4. incomplete group: fragment 2 of sequence id 9 never arrived ---------
    out["incomplete"] = {
        "sentences": [
            {"source_id": "ais-receiver-3", "sentence": sentence(2, 1, "9", "A", p1, f1)},
        ],
    }

    json.dump(out, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
