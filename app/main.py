"""FastAPI application exposing the AIS decoding and vessel snapshot endpoints."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from . import __version__
from .assembler import assemble
from .models import (
    Candidate,
    DecodeRequest,
    DecodeResponse,
    DecodeResult,
    IngestRequest,
    IngestResponse,
    MessageData,
    ReviewBatch,
    ReviewBatchSummary,
    ReviewCreateRequest,
    ReviewResolveRequest,
    ReviewStatus,
    SentenceIn,
    Summary,
    VesselSnapshot,
    VesselTrack,
)
from .nmea import Fragment, SentenceError, parse_sentence
from .storage import (
    KIND_POSITION,
    KIND_STATIC,
    SnapshotStore,
    _parse_observed_at,
    normalize_observed_at,
)

# Default location of the snapshot database; docker-compose points
# AIS_DB_PATH at a named volume so snapshots survive restarts.
DEFAULT_DB_PATH = "ais_snapshots.db"

app = FastAPI(
    title="AIS NMEA 0183 Decoder",
    version=__version__,
    description=(
        "Decodes batches of raw !AIVDM/!AIVDO sentences. Validates XOR "
        "checksums, fragment numbering and fill bits, reassembles "
        "multi-fragment messages per source/channel/sequence id, enumerates "
        "complete combinations when no sequence id is present (HTTP 409 on "
        "ambiguity) and decodes AIS message types 1/2/3 and 5. POST /ingest "
        "additionally maintains persistent per-(source_id, MMSI) snapshots "
        "of the latest position and static/voyage messages and an append-only "
        "position track (every stored type 1/2/3 report, including late "
        "observations), queryable via GET /vessels and GET /tracks. "
        "Ambiguous batches can be stored via POST /reviews "
        "for manual review; a human picks one listed candidate per ambiguous "
        "pool and POST /reviews/{batch_id}/resolve applies the selection "
        "to the snapshots atomically."
    ),
)

_store: Optional[SnapshotStore] = None
_store_lock = threading.Lock()


def get_store() -> SnapshotStore:
    """Lazily open the snapshot database (path from AIS_DB_PATH)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SnapshotStore(os.environ.get("AIS_DB_PATH", DEFAULT_DB_PATH))
    return _store


def _error_result(item: SentenceIn, index: int, exc: SentenceError) -> DecodeResult:
    return DecodeResult(
        source_id=item.source_id,
        sentence_indexes=[index],
        status="error",
        error=exc.code,
        detail=exc.detail,
    )


def process_batch(items: list[SentenceIn]) -> list[DecodeResult]:
    """Validate, group and decode one batch of sentences."""
    results: list[DecodeResult] = []
    fragments: list[Fragment] = []
    seen: dict[tuple[str, str], Fragment] = {}

    for index, item in enumerate(items):
        key = (item.source_id, item.sentence.strip())
        if key in seen:  # identical retransmission inside the same batch
            seen[key].indexes.append(index)
            continue
        try:
            fragment = parse_sentence(item.sentence, item.source_id, index)
        except SentenceError as exc:
            results.append(_error_result(item, index, exc))
            continue
        seen[key] = fragment
        fragments.append(fragment)

    results.extend(assemble(fragments))
    results.sort(key=lambda r: r.sentence_indexes[0])
    return results


def summarize(results: list[DecodeResult], total_sentences: int) -> Summary:
    """Aggregate per-result statuses into the batch summary."""
    return Summary(
        total_sentences=total_sentences,
        invalid_sentences=sum(1 for r in results if r.status == "error" and r.error != "decode_error"),
        decoded=sum(1 for r in results if r.status == "decoded"),
        incomplete=sum(1 for r in results if r.status == "incomplete"),
        ambiguous=sum(1 for r in results if r.status == "ambiguous"),
        errors=sum(1 for r in results if r.status == "error"),
    )


def _build_response(items: list[SentenceIn]) -> DecodeResponse:
    results = process_batch(items)
    return DecodeResponse(summary=summarize(results, len(items)), results=results)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/decode",
    response_model=DecodeResponse,
    responses={
        200: {"description": "Batch processed; no ambiguous group was found."},
        409: {
            "model": DecodeResponse,
            "description": (
                "At least one fragment pool without sequential message id "
                "allows several complete combinations; candidates are listed "
                "in the corresponding results."
            ),
        },
    },
    tags=["decode"],
)
def decode(request: DecodeRequest):
    response = _build_response(request.sentences)
    if any(r.status == "ambiguous" for r in response.results):
        return JSONResponse(status_code=409, content=jsonable_encoder(response))
    return response


def _kind_of(message: MessageData) -> str:
    """Snapshot timeline (``position``/``static``) for a decoded message."""
    return KIND_POSITION if message.message_type in (1, 2, 3) else KIND_STATIC


def _collect_writes(
    results: list[DecodeResult], observed_at: str
) -> list[tuple[str, int, str, str, dict, int]]:
    """Build ordered store writes from decoded results.

    Each write carries the first original sentence index of the message.
    *results* is sorted by first sentence index, so when one batch carries
    several messages of the same class for a vessel, the first one wins
    (later ones share the batch ``observed_at`` and lose the strictly-newer
    comparison, and insert no extra track point). Errors, incomplete
    groups and ambiguous candidates never reach the store.
    """
    writes: list[tuple[str, int, str, str, dict, int]] = []
    for result in results:
        if result.status != "decoded" or result.decoded is None:
            continue
        message = result.decoded
        writes.append(
            (
                result.source_id,
                message.mmsi,
                _kind_of(message),
                observed_at,
                message.model_dump(),
                result.sentence_indexes[0],
            )
        )
    return writes


def _apply_to_store(
    results: list[DecodeResult], observed_at: str, store: SnapshotStore
) -> list[VesselSnapshot]:
    """Persist decoded messages and return the involved vessels' snapshots.

    Snapshot upserts and position track inserts commit together in one
    transaction; a failure rolls back every write of the batch.
    """
    writes = _collect_writes(results, observed_at)
    involved = store.apply_ingest(writes)
    snapshots: list[VesselSnapshot] = []
    for source_id, mmsi in involved:
        snapshot = store.get_snapshot(source_id, mmsi)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


@app.post(
    "/ingest",
    response_model=IngestResponse,
    responses={
        200: {"description": "Batch processed and snapshots updated; no ambiguous group."},
        409: {
            "model": IngestResponse,
            "description": (
                "At least one ambiguous fragment pool; unambiguous decoded "
                "messages still updated the snapshots."
            ),
        },
    },
    tags=["snapshots"],
)
def ingest(request: IngestRequest):
    decoded = _build_response(request.sentences)
    observed_at = normalize_observed_at(request.observed_at)
    snapshots = _apply_to_store(decoded.results, observed_at, get_store())
    response = IngestResponse(
        observed_at=request.observed_at,
        summary=decoded.summary,
        results=decoded.results,
        snapshots=snapshots,
    )
    if any(r.status == "ambiguous" for r in response.results):
        return JSONResponse(status_code=409, content=jsonable_encoder(response))
    return response


@app.get(
    "/vessels",
    response_model=list[VesselSnapshot],
    tags=["snapshots"],
    description=(
        "List stored vessel snapshots, optionally filtered by source_id "
        "and/or MMSI. Each snapshot merges the latest position report "
        "(message 1/2/3) and the latest static/voyage message (type 5) "
        "kept for that (source_id, MMSI) pair."
    ),
)
def list_vessels(source_id: Optional[str] = None, mmsi: Optional[int] = None):
    return get_store().list_snapshots(source_id=source_id, mmsi=mmsi)


@app.get(
    "/tracks",
    response_model=VesselTrack,
    tags=["snapshots"],
    responses={
        200: {"description": "Stored position reports, oldest first."},
        404: {"description": "No data for the given (source_id, MMSI)."},
        422: {"description": "A required query parameter is missing."},
    },
    description=(
        "Replay the position track of one (source_id, MMSI) pair. Every "
        "successfully stored message 1/2/3 is returned, ordered by "
        "observed_at ascending; late (older) observations are included "
        "even though they never rolled back the GET /vessels snapshot, "
        "and a repeated submission at an already known observed_at does "
        "not add another point. Points keep null coordinates as decoded."
    ),
)
def get_track(source_id: str, mmsi: int):
    store = get_store()
    if not store.vessel_exists(source_id, mmsi):
        raise HTTPException(
            status_code=404,
            detail=f"no data stored for source '{source_id}', MMSI {mmsi}",
        )
    return VesselTrack(
        source_id=source_id, mmsi=mmsi, points=store.get_track(source_id, mmsi)
    )


# ---------------------------------------------------------------------------
# Manual review of ambiguous batches
# ---------------------------------------------------------------------------

def _review_from_row(row: sqlite3.Row) -> ReviewBatch:
    """Rebuild the API view of a stored review batch."""
    sentences = [SentenceIn(**item) for item in json.loads(row["request"])]
    results = [DecodeResult(**item) for item in json.loads(row["results"])]
    resolution = json.loads(row["resolution"]) if row["resolution"] else None
    return ReviewBatch(
        batch_id=row["batch_id"],
        status=row["status"],
        observed_at=_parse_observed_at(row["observed_at"]),
        created_at=_parse_observed_at(row["created_at"]),
        resolved_at=(
            _parse_observed_at(row["resolved_at"]) if row["resolved_at"] else None
        ),
        total_sentences=len(sentences),
        ambiguous_pools=sum(1 for r in results if r.status == "ambiguous"),
        sentences=sentences,
        summary=summarize(results, len(sentences)),
        results=results,
        selections=resolution["selections"] if resolution else None,
        snapshots=(
            [VesselSnapshot(**item) for item in resolution["snapshots"]]
            if resolution
            else None
        ),
    )


@app.post(
    "/reviews",
    status_code=201,
    response_model=ReviewBatch,
    responses={
        201: {"description": "Ambiguous batch stored as a pending review batch."},
        422: {
            "description": (
                "Request body invalid, or the batch contains no ambiguous "
                "fragment pool (nothing to review)."
            ),
        },
    },
    tags=["review"],
)
def create_review(request: ReviewCreateRequest):
    """Store an ambiguous batch for later manual resolution.

    The sentences are validated, assembled and decoded exactly like
    ``POST /ingest``; the original batch and every candidate of every
    ambiguous pool are persisted, but **no vessel snapshot is touched**
    until the batch is resolved.
    """
    results = process_batch(request.sentences)
    if not any(r.status == "ambiguous" for r in results):
        raise HTTPException(
            status_code=422,
            detail=(
                "batch contains no ambiguous fragment pool; only ambiguous "
                "batches are accepted for review"
            ),
        )
    store = get_store()
    batch_id = uuid.uuid4().hex
    store.create_review(
        batch_id=batch_id,
        observed_at=normalize_observed_at(request.observed_at),
        created_at=normalize_observed_at(datetime.now(timezone.utc)),
        request_json=json.dumps(jsonable_encoder(request.sentences)),
        results_json=json.dumps(jsonable_encoder(results)),
    )
    row = store.get_review(batch_id)
    assert row is not None  # just inserted
    return _review_from_row(row)


@app.get(
    "/reviews",
    response_model=list[ReviewBatchSummary],
    tags=["review"],
    description=(
        "List review batches, optionally filtered by status "
        "('pending' or 'resolved')."
    ),
)
def list_reviews(status: Optional[ReviewStatus] = None):
    return [_review_from_row(row) for row in get_store().list_reviews(status)]


@app.get(
    "/reviews/{batch_id}",
    response_model=ReviewBatch,
    responses={404: {"description": "Unknown batch id."}},
    tags=["review"],
    description=(
        "Full detail of one review batch: original sentences, decode "
        "results with every listed candidate per ambiguous pool and, once "
        "resolved, the chosen candidates and the stored snapshots."
    ),
)
def get_review(batch_id: str):
    row = get_store().get_review(batch_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown review batch '{batch_id}'")
    return _review_from_row(row)


@app.post(
    "/reviews/{batch_id}/resolve",
    response_model=ReviewBatch,
    responses={
        200: {
            "description": (
                "Batch resolved: the selected candidates and the batch's "
                "unambiguous decodes were applied to the snapshots."
            ),
        },
        404: {"description": "Unknown batch id."},
        409: {"description": "Batch was already resolved."},
        422: {
            "description": (
                "A selection is missing, does not match a listed candidate, "
                "or refers to a candidate that failed to decode; the whole "
                "resolution is rejected and nothing is written."
            ),
        },
    },
    tags=["review"],
)
def resolve_review(batch_id: str, request: ReviewResolveRequest):
    """Apply a human resolution: one listed, successfully decoded candidate
    per ambiguous pool, plus the batch's unambiguous decodes.

    Every message is written under the batch ``observed_at`` in original
    sentence-index order (first index wins for one vessel and class). The
    status flip and the snapshot writes commit atomically; a newer snapshot
    already stored is never rolled back, but the batch is still marked
    resolved and the response carries the snapshots actually stored.
    """
    store = get_store()
    row = store.get_review(batch_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown review batch '{batch_id}'")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409, detail=f"review batch '{batch_id}' is already resolved"
        )
    review = _review_from_row(row)

    pools = [r for r in review.results if r.status == "ambiguous"]
    # a candidate is uniquely identified by its sentence indexes
    listed: dict[tuple[int, ...], tuple[int, DecodeResult, Candidate]] = {}
    for pool_index, pool in enumerate(pools):
        for candidate in pool.candidates or []:
            listed[tuple(candidate.sentence_indexes)] = (pool_index, pool, candidate)

    if len(request.selections) != len(pools):
        raise HTTPException(
            status_code=422,
            detail=(
                "exactly one selection per ambiguous pool is required: the "
                f"batch has {len(pools)} pool(s), got "
                f"{len(request.selections)} selection(s)"
            ),
        )

    chosen: list[tuple[int, str, MessageData]] = []
    covered: set[int] = set()
    for selection in request.selections:
        key = tuple(selection)
        if key not in listed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"selection {selection} does not match any candidate "
                    "listed for this batch"
                ),
            )
        pool_index, pool, candidate = listed[key]
        covered.add(pool_index)
        if candidate.error is not None or candidate.decoded is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"selected candidate {selection} failed to decode and "
                    "cannot be applied"
                ),
            )
        chosen.append((candidate.sentence_indexes[0], pool.source_id, candidate.decoded))
    if len(covered) != len(pools):
        raise HTTPException(
            status_code=422,
            detail="every ambiguous pool needs exactly one selected candidate",
        )

    # The batch's unambiguous decodes join the write set; everything is
    # applied in original sentence-index order under the batch observed_at.
    indexed: list[tuple[int, str, MessageData]] = [
        (r.sentence_indexes[0], r.source_id, r.decoded)
        for r in review.results
        if r.status == "decoded" and r.decoded is not None
    ]
    indexed.extend(chosen)
    indexed.sort(key=lambda item: item[0])
    observed_at = normalize_observed_at(review.observed_at)
    writes = [
        (
            source_id,
            message.mmsi,
            _kind_of(message),
            observed_at,
            message.model_dump(),
            index,
        )
        for index, source_id, message in indexed
    ]

    resolved_at = normalize_observed_at(datetime.now(timezone.utc))
    snapshots = store.resolve_review(batch_id, resolved_at, request.selections, writes)
    if snapshots is None:  # lost a race against a concurrent resolution
        raise HTTPException(
            status_code=409, detail=f"review batch '{batch_id}' is already resolved"
        )
    row = store.get_review(batch_id)
    assert row is not None  # just resolved
    return _review_from_row(row)
