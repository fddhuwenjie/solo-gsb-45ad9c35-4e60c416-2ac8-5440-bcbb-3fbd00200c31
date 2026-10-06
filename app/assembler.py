"""Grouping, reassembly and ambiguity resolution for validated fragments.

Rules implemented here:

* single-fragment sentences decode on their own;
* multi-fragment sentences carrying a sequential message id are grouped by
  (source_id, channel, sequence_id, fragment count) and reassembled in
  fragment order — fragments may arrive out of order;
* multi-fragment sentences with an empty sequence id are pooled by
  (source_id, channel, fragment count); every complete combination that
  uses each physical sentence at most once per candidate is enumerated:

  - exactly one combination  -> decode it;
  - several combinations     -> report the group as ambiguous and list
    every candidate (the API answers with HTTP 409);
  - some fragment number missing -> report the missing fragment numbers.
"""
from __future__ import annotations

from itertools import product
from typing import Optional

from .ais import AisDecodeError, decode_payload
from .models import Candidate, DecodeResult, MessageData
from .nmea import Fragment

# Upper bound on enumerated combinations for an ambiguous pool, so a
# pathological batch cannot explode the response.
MAX_CANDIDATES = 100


def _all_indexes(fragments: list[Fragment]) -> list[int]:
    indexes: list[int] = []
    for frag in fragments:
        indexes.extend(frag.indexes)
    return sorted(indexes)


def _decode_combination(fragments: list[Fragment]) -> tuple[Optional[MessageData], Optional[str], str]:
    """Concatenate payloads in fragment order and decode.

    Returns (decoded, error, assembled_payload).
    """
    ordered = sorted(fragments, key=lambda f: f.number)
    payload = "".join(f.payload for f in ordered)
    try:
        message = decode_payload(payload, ordered[-1].fill_bits)
        return message, None, payload
    except AisDecodeError as exc:
        return None, str(exc), payload


def _base_result(fragments: list[Fragment], status: str) -> DecodeResult:
    first = fragments[0]
    return DecodeResult(
        source_id=first.source_id,
        sentence_indexes=_all_indexes(fragments),
        status=status,  # type: ignore[arg-type]
        sentence_type=first.sentence_type,
        channel=first.channel or None,
        sequence_id=first.sequence_id or None,
        total_fragments=first.total,
        received_fragments=sorted({f.number for f in fragments}),
    )


def _decoded_result(fragments: list[Fragment]) -> DecodeResult:
    message, error, _payload = _decode_combination(fragments)
    if error is not None:
        result = _base_result(fragments, "error")
        result.error = "decode_error"
        result.detail = error
        return result
    result = _base_result(fragments, "decoded")
    result.message_type = message.message_type  # type: ignore[union-attr]
    result.decoded = message
    return result


def _incomplete_result(fragments: list[Fragment], missing: list[int]) -> DecodeResult:
    result = _base_result(fragments, "incomplete")
    result.missing_fragments = missing
    result.detail = (
        f"missing fragment(s) {', '.join(map(str, missing))} "
        f"of {fragments[0].total}"
    )
    return result


def _assemble_keyed(key: tuple, fragments: list[Fragment]) -> DecodeResult:
    """Reassemble a group sharing an explicit sequential message id."""
    by_number: dict[int, list[Fragment]] = {}
    for frag in fragments:
        by_number.setdefault(frag.number, []).append(frag)

    conflicts = sorted(n for n, group in by_number.items() if len(group) > 1)
    if conflicts:
        result = _base_result(fragments, "error")
        result.error = "conflicting_fragments"
        result.detail = (
            f"conflicting payloads for fragment number(s) "
            f"{', '.join(map(str, conflicts))} within sequence id "
            f"'{key[2]}'"
        )
        return result

    total = key[3]
    missing = [n for n in range(1, total + 1) if n not in by_number]
    if missing:
        return _incomplete_result(fragments, missing)
    return _decoded_result([by_number[n][0] for n in range(1, total + 1)])


def _assemble_orphan_pool(key: tuple, fragments: list[Fragment]) -> DecodeResult:
    """Resolve a pool of multi-fragment sentences without sequence ids.

    Every complete combination (one sentence per fragment number, each
    physical sentence used at most once per combination) is enumerated.
    """
    source_id, channel, total = key
    by_number: dict[int, list[Fragment]] = {}
    for frag in fragments:
        by_number.setdefault(frag.number, []).append(frag)

    missing = [n for n in range(1, total + 1) if n not in by_number]
    if missing:
        return _incomplete_result(fragments, missing)

    groups = [by_number[n] for n in range(1, total + 1)]
    candidates: list[Candidate] = []
    truncated = False
    for combo in product(*groups):
        if len(candidates) >= MAX_CANDIDATES:
            truncated = True
            break
        message, error, payload = _decode_combination(list(combo))
        candidates.append(
            Candidate(
                sentence_indexes=_all_indexes(list(combo)),
                payload=payload,
                message_type=message.message_type if message else None,
                decoded=message,
                error=error,
            )
        )

    if len(candidates) == 1 and not truncated:
        only = candidates[0]
        combo = list(next(product(*groups)))
        if only.error is not None:
            result = _base_result(combo, "error")
            result.error = "decode_error"
            result.detail = only.error
            return result
        result = _base_result(combo, "decoded")
        result.message_type = only.message_type
        result.decoded = only.decoded
        return result

    result = _base_result(fragments, "ambiguous")
    result.candidates = candidates
    note = f" (enumeration capped at {MAX_CANDIDATES})" if truncated else ""
    result.detail = (
        f"{len(candidates)} complete combinations are possible for fragment "
        f"pool (source_id={source_id!r}, channel={channel or '-'!r}, "
        f"fragments={total}) without sequential message ids{note}"
    )
    return result


def assemble(fragments: list[Fragment]) -> list[DecodeResult]:
    """Group validated fragments and decode every group."""
    results: list[DecodeResult] = []
    keyed: dict[tuple, list[Fragment]] = {}
    orphan_pools: dict[tuple, list[Fragment]] = {}

    for frag in fragments:
        if frag.is_single:
            results.append(_decoded_result([frag]))
        elif frag.sequence_id:
            key = (frag.source_id, frag.channel, frag.sequence_id, frag.total)
            keyed.setdefault(key, []).append(frag)
        else:
            key = (frag.source_id, frag.channel, frag.total)
            orphan_pools.setdefault(key, []).append(frag)

    for key in sorted(keyed):
        results.append(_assemble_keyed(key, keyed[key]))
    for key in sorted(orphan_pools):
        results.append(_assemble_orphan_pool(key, orphan_pools[key]))
    return results
