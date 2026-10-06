"""Pydantic request/response models for the AIS decoding API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class SentenceIn(BaseModel):
    """One raw NMEA 0183 sentence together with its receiver identifier."""

    source_id: str = Field(
        ..., min_length=1, max_length=128,
        description="Identifier of the receiver/source that produced the sentence.",
        examples=["ais-receiver-1"],
    )
    sentence: str = Field(
        ..., min_length=1, max_length=512,
        description="Raw !AIVDM or !AIVDO sentence, including the *HH checksum.",
        examples=["!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C"],
    )


class DecodeRequest(BaseModel):
    """A batch of sentences captured during the same time window."""

    sentences: list[SentenceIn] = Field(..., min_length=1, max_length=2000)


class IngestRequest(BaseModel):
    """A batch of sentences plus the UTC observation time of the whole batch.

    Same sentence list as :class:`DecodeRequest`; successfully decoded
    messages additionally update the persistent per-vessel snapshots.
    """

    observed_at: datetime = Field(
        ...,
        description=(
            "UTC observation time of the whole batch (ISO 8601). "
            "Naive values are treated as UTC; offsets are converted to UTC."
        ),
        examples=["2026-10-05T12:00:00Z"],
    )
    sentences: list[SentenceIn] = Field(..., min_length=1, max_length=2000)

    @field_validator("observed_at")
    @classmethod
    def _assume_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


# ---------------------------------------------------------------------------
# Decoded message bodies
# ---------------------------------------------------------------------------

class PositionReport(BaseModel):
    """AIS message types 1/2/3 — position report, class A."""

    message_type: int
    repeat_indicator: int
    mmsi: int
    navigation_status: int
    rate_of_turn: Optional[int] = Field(
        None, description="Raw AIS turn value, null when 0x80 (not available).")
    speed_over_ground: Optional[float] = Field(
        None, description="Knots, null when the raw value is 1023.")
    position_accuracy: bool
    longitude: Optional[float] = Field(
        None, description="Degrees, null when the raw value is 181 degrees.")
    latitude: Optional[float] = Field(
        None, description="Degrees, null when the raw value is 91 degrees.")
    course_over_ground: Optional[float] = Field(
        None, description="Degrees, null when the raw value is 3600.")
    true_heading: Optional[int] = Field(
        None, description="Degrees, null when the raw value is 511.")
    timestamp: Optional[int] = Field(
        None, description="UTC second, null when the raw value is 60.")
    maneuver_indicator: int
    raim_flag: bool
    radio_status: int


class StaticAndVoyageData(BaseModel):
    """AIS message type 5 — static and voyage related data."""

    message_type: int
    repeat_indicator: int
    mmsi: int
    ais_version: int
    imo_number: Optional[int] = Field(None, description="Null when raw value is 0.")
    call_sign: str
    name: str
    ship_type: Optional[int] = Field(None, description="Null when raw value is 0 (not available).")
    dimension_to_bow: int
    dimension_to_stern: int
    dimension_to_port: int
    dimension_to_starboard: int
    position_fix_type: int
    eta_month: Optional[int] = Field(None, description="Null when raw value is 0.")
    eta_day: Optional[int] = Field(None, description="Null when raw value is 0.")
    eta_hour: Optional[int] = Field(None, description="Null when raw value is 24.")
    eta_minute: Optional[int] = Field(None, description="Null when raw value is 60.")
    draught: Optional[float] = Field(None, description="Metres, null when raw value is 0.")
    destination: str
    dte: bool


MessageData = Union[PositionReport, StaticAndVoyageData]


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

ResultStatus = Literal["decoded", "incomplete", "ambiguous", "error"]


class Candidate(BaseModel):
    """One possible complete message assembled from an ambiguous fragment pool."""

    sentence_indexes: list[int] = Field(
        ..., description="Indexes (into the request batch) of the sentences used.")
    payload: str = Field(..., description="Concatenated six-bit payload.")
    message_type: Optional[int] = None
    decoded: Optional[MessageData] = None
    error: Optional[str] = Field(
        None, description="Decoding error for this candidate, if any.")


class DecodeResult(BaseModel):
    """Outcome for one sentence or one reassembled message group."""

    source_id: str
    sentence_indexes: list[int] = Field(
        ..., description="Indexes (into the request batch) of the sentences involved.")
    status: ResultStatus
    sentence_type: Optional[str] = Field(None, description="VDM or VDO.")
    channel: Optional[str] = None
    sequence_id: Optional[str] = None
    total_fragments: Optional[int] = None
    received_fragments: Optional[list[int]] = None
    missing_fragments: Optional[list[int]] = None
    message_type: Optional[int] = None
    decoded: Optional[MessageData] = None
    candidates: Optional[list[Candidate]] = None
    error: Optional[str] = Field(None, description="Machine readable error code.")
    detail: Optional[str] = Field(None, description="Human readable explanation.")


class Summary(BaseModel):
    total_sentences: int
    invalid_sentences: int
    decoded: int
    incomplete: int
    ambiguous: int
    errors: int


class DecodeResponse(BaseModel):
    summary: Summary
    results: list[DecodeResult]


# ---------------------------------------------------------------------------
# Persistent vessel snapshots
# ---------------------------------------------------------------------------

class TimedPositionReport(BaseModel):
    """The latest stored position report (message 1/2/3) with its batch time."""

    observed_at: datetime
    data: PositionReport


class TimedStaticAndVoyageData(BaseModel):
    """The latest stored static/voyage message (type 5) with its batch time."""

    observed_at: datetime
    data: StaticAndVoyageData


class VesselSnapshot(BaseModel):
    """Merged latest state of one (source_id, MMSI) pair.

    ``position`` and ``static`` are independent timelines: each is replaced
    only by a strictly newer ``observed_at``. Either side is null until the
    corresponding message class has been decoded for the vessel.
    """

    source_id: str
    mmsi: int
    position: Optional[TimedPositionReport] = None
    static: Optional[TimedStaticAndVoyageData] = None


class IngestResponse(BaseModel):
    """Batch outcome plus the merged snapshots of every vessel involved."""

    observed_at: datetime
    summary: Summary
    results: list[DecodeResult]
    snapshots: list[VesselSnapshot] = Field(
        ...,
        description=(
            "Merged snapshots (after applying this batch) of every vessel "
            "with a successfully decoded message in the batch."
        ),
    )


# ---------------------------------------------------------------------------
# Persistent position tracks
# ---------------------------------------------------------------------------

class TrackPoint(BaseModel):
    """One stored position report (message 1/2/3) with its batch time.

    Points are returned in ascending ``observed_at`` order. Coordinates
    stay null when the source report carried the "not available" sentinel;
    no neighbouring position is borrowed.
    """

    observed_at: datetime
    data: PositionReport


class VesselTrack(BaseModel):
    """Full position history of one (source_id, MMSI) pair.

    Every successfully stored position report is kept — including late
    (older) observations — while the latest snapshot in ``GET /vessels``
    is never rolled back. A repeated submission at an already known
    ``observed_at`` does not add another point.
    """

    source_id: str
    mmsi: int
    points: list[TrackPoint] = Field(
        ..., description="Position reports ordered by observed_at ascending."
    )


# ---------------------------------------------------------------------------
# Manual review of ambiguous batches
# ---------------------------------------------------------------------------

class ReviewCreateRequest(IngestRequest):
    """Sentences plus batch observation time, stored as a pending review batch.

    Same payload as :class:`IngestRequest`; the batch is validated, assembled
    and decoded exactly like ``POST /ingest``, but creating the review batch
    never touches the vessel snapshots.
    """


ReviewStatus = Literal["pending", "resolved"]


class ReviewResolveRequest(BaseModel):
    """One listed candidate per ambiguous pool, chosen by a human reviewer.

    Every entry is the ``sentence_indexes`` list of the chosen candidate,
    copied verbatim from the batch's ``results[].candidates``.
    """

    selections: list[list[int]] = Field(
        ...,
        min_length=1,
        description=(
            "One entry per ambiguous pool of the batch: the sentence_indexes "
            "of the chosen candidate, exactly as listed in the batch results."
        ),
        examples=[[[0, 2]]],
    )


class ReviewBatchSummary(BaseModel):
    """Headline state of one review batch (list view)."""

    batch_id: str
    status: ReviewStatus
    observed_at: datetime = Field(
        ..., description="UTC observation time of the reviewed batch.")
    created_at: datetime = Field(
        ..., description="UTC time the review batch was created.")
    resolved_at: Optional[datetime] = Field(
        None, description="UTC time of resolution; null while pending.")
    total_sentences: int
    ambiguous_pools: int


class ReviewBatch(ReviewBatchSummary):
    """Full detail of one review batch.

    ``sentences``/``results`` are the original batch and its decoded outcome
    (each ambiguous pool lists every candidate under ``candidates``). Once
    resolved, ``selections`` records the chosen candidates and ``snapshots``
    the vessel snapshots actually stored by the resolution.
    """

    sentences: list[SentenceIn]
    summary: Summary
    results: list[DecodeResult]
    selections: Optional[list[list[int]]] = None
    snapshots: Optional[list[VesselSnapshot]] = None
