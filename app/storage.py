"""SQLite persistence for per-vessel AIS snapshots and review batches.

``vessel_state``: one row per ``(source_id, mmsi, kind)`` where ``kind`` is
``'position'`` (message types 1/2/3) or ``'static'`` (message type 5). A
stored row is only replaced by a *strictly newer* ``observed_at``:

* older batches never roll back newer state;
* an equal ``observed_at`` keeps the row that was saved first;
* inside one batch (all messages share the batch ``observed_at``) the
  caller applies decoded messages in sentence-index order, so the first
  message of a class wins.

Whole decoded messages are stored as-is — a position report whose
coordinates are "not available" (null) is stored with null coordinates;
no fields are borrowed from older messages.

``vessel_track``: append-only time series of every stored position report
(message types 1/2/3), one row per ``(source_id, mmsi, observed_at)`` —
late (older) observations are kept in the track even though they never
roll back the ``vessel_state`` snapshot, and a repeated submission at an
already-stored ``observed_at`` does not add another point (``INSERT OR
IGNORE`` keeps the first saved message). Each point records the original
sentence index for provenance. When an older database without this table
is opened, each vessel's existing position snapshot is copied in as a
single starting point (``sentence_index = -1``); no earlier track is
inferred.

A track point found to be a misreport is marked invalid in place
(``valid = 0`` plus the ``invalid_reason``/``invalidated_at`` record);
the row is never deleted. Invalid points are hidden from ``GET /tracks``,
and the marking, the visibility change and the snapshot fall-back commit
in one transaction:

* when the ``vessel_state`` position snapshot is exactly the invalidated
  point, it falls back to the vessel's latest still-valid track point,
  or the position part is removed when no valid point remains (the
  static part is never touched);
* invalidating an older point never rolls back a newer valid snapshot;
* a later ``/ingest`` or review resolution carrying a position report at
  an invalidated ``(source_id, mmsi, observed_at)`` can neither
  re-activate the point nor write it back into the snapshot — the write
  is skipped, while every other observation follows the usual rules.

Databases written before the invalidation columns existed are migrated
in place (``ALTER TABLE``) when the store is opened.

``review_batch``: one row per manual-review batch created from an ambiguous
batch of sentences. The original sentences and the full decode outcome
(including every candidate of every ambiguous pool) are stored as JSON, so
pending batches, their candidates and the resolution (chosen candidates plus
the snapshots actually stored) survive restarts. Resolution applies the
snapshot and track writes and flips the status in a single transaction.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from .models import (
    PositionReport,
    StaticAndVoyageData,
    TimedPositionReport,
    TimedStaticAndVoyageData,
    TrackPoint,
    VesselSnapshot,
)

KIND_POSITION = "position"
KIND_STATIC = "static"

# sentence_index recorded for track points backfilled from pre-existing
# vessel_state rows of an older database (no earlier track is inferred).
LEGACY_TRACK_INDEX = -1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vessel_state (
    source_id   TEXT NOT NULL,
    mmsi        INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('position', 'static')),
    observed_at TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (source_id, mmsi, kind)
);

CREATE TABLE IF NOT EXISTS vessel_track (
    source_id      TEXT NOT NULL,
    mmsi           INTEGER NOT NULL,
    observed_at    TEXT NOT NULL,
    sentence_index INTEGER NOT NULL,
    data           TEXT NOT NULL,
    valid          INTEGER NOT NULL DEFAULT 1,
    invalid_reason TEXT,
    invalidated_at TEXT,
    PRIMARY KEY (source_id, mmsi, observed_at)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS review_batch (
    batch_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    observed_at  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    request      TEXT NOT NULL,
    results      TEXT NOT NULL,
    resolution   TEXT
);
"""

# Existing position snapshots of an older database become one starting
# track point per vessel; INSERT OR IGNORE makes this idempotent on
# databases that already carry track points.
_BACKFILL_TRACK_SQL = """
INSERT OR IGNORE INTO vessel_track
    (source_id, mmsi, observed_at, sentence_index, data)
SELECT source_id, mmsi, observed_at, :legacy_index, data
FROM vessel_state
WHERE kind = 'position'
"""

# Databases written before track-point invalidation existed are migrated
# in place: the invalidation columns are added when missing (existing
# points stay valid).
_MIGRATE_TRACK_COLUMNS = (
    ("valid", "ALTER TABLE vessel_track ADD COLUMN valid INTEGER NOT NULL DEFAULT 1"),
    ("invalid_reason", "ALTER TABLE vessel_track ADD COLUMN invalid_reason TEXT"),
    ("invalidated_at", "ALTER TABLE vessel_track ADD COLUMN invalidated_at TEXT"),
)

_INSERT_TRACK_SQL = """
INSERT OR IGNORE INTO vessel_track
    (source_id, mmsi, observed_at, sentence_index, data)
VALUES (?, ?, ?, ?, ?)
"""

_UPSERT_SQL = """
INSERT INTO vessel_state (source_id, mmsi, kind, observed_at, data)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (source_id, mmsi, kind) DO UPDATE SET
    observed_at = excluded.observed_at,
    data = excluded.data
WHERE excluded.observed_at > vessel_state.observed_at
"""


def normalize_observed_at(value: datetime) -> str:
    """Fixed-width UTC ISO 8601 string; lexicographic order == time order."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_observed_at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class SnapshotStore:
    """Thread-safe SQLite-backed store of the latest message per vessel."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            # One-time migration for older databases: existing position
            # snapshots seed each vessel's track with a single starting point.
            self._conn.execute(
                _BACKFILL_TRACK_SQL, {"legacy_index": LEGACY_TRACK_INDEX}
            )
            # One-time migration for older databases: add the invalidation
            # columns to vessel_track when they are missing.
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(vessel_track)")
            }
            for column, ddl in _MIGRATE_TRACK_COLUMNS:
                if column not in columns:
                    self._conn.execute(ddl)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -------------------------------------------------------------

    def _point_invalidated(
        self, source_id: str, mmsi: int, observed_at: str
    ) -> bool:
        """True when the track point for this exact key was marked invalid.

        Caller must hold the lock.
        """
        row = self._conn.execute(
            """
            SELECT valid FROM vessel_track
            WHERE source_id = ? AND mmsi = ? AND observed_at = ?
            """,
            (source_id, mmsi, observed_at),
        ).fetchone()
        return row is not None and not row["valid"]

    def _write_message(
        self,
        source_id: str,
        mmsi: int,
        kind: str,
        observed_at: str,
        data: dict,
        sentence_index: int = 0,
    ) -> bool:
        """Upsert the snapshot row and record a position track point.

        Every position report (kind == 'position') is appended to
        ``vessel_track`` regardless of whether it also replaces the
        snapshot: a late older observation still enters the track, while
        ``INSERT OR IGNORE`` keeps the first saved message for an already
        known ``(source_id, mmsi, observed_at)``. A position report whose
        ``(source_id, mmsi, observed_at)`` was marked invalid is skipped
        entirely — an invalidated misreport is never re-activated or
        written back into the snapshot. Caller must hold the lock and the
        transaction.
        """
        if kind == KIND_POSITION and self._point_invalidated(
            source_id, mmsi, observed_at
        ):
            return False
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
        cursor = self._conn.execute(
            _UPSERT_SQL, (source_id, mmsi, kind, observed_at, payload)
        )
        if kind == KIND_POSITION:
            self._conn.execute(
                _INSERT_TRACK_SQL,
                (source_id, mmsi, observed_at, sentence_index, payload),
            )
        return cursor.rowcount > 0

    def upsert_if_newer(
        self,
        source_id: str,
        mmsi: int,
        kind: str,
        observed_at: str,
        data: dict,
    ) -> bool:
        """Store *data* unless an equal-or-newer snapshot row already exists.

        Position reports additionally append a track point (late reports
        still enter the track; a repeated submission at the same
        ``observed_at`` does not duplicate it). *observed_at* must be
        normalised with :func:`normalize_observed_at`. Returns True when
        the snapshot row was (re)written.
        """
        with self._lock, self._conn:
            return self._write_message(source_id, mmsi, kind, observed_at, data)

    def apply_ingest(
        self,
        writes: list[tuple[str, int, str, str, dict, int]],
    ) -> list[tuple[str, int]]:
        """Apply one normal ``/ingest`` batch in a single transaction.

        *writes* are
        ``(source_id, mmsi, kind, observed_at, data, sentence_index)``
        tuples, already ordered by original sentence index. Snapshot
        upserts and position track inserts commit or roll back together.
        Returns the involved vessels' ``(source_id, mmsi)`` pairs.
        """
        with self._lock, self._conn:
            for source_id, mmsi, kind, observed_at, data, sentence_index in writes:
                self._write_message(
                    source_id, mmsi, kind, observed_at, data, sentence_index
                )
            return sorted({(w[0], w[1]) for w in writes})

    # -- reads ----------------------------------------------------------------

    def _fetch_rows(
        self,
        source_id: Optional[str] = None,
        mmsi: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        """SELECT matching ``vessel_state`` rows; caller must hold the lock."""
        sql = "SELECT source_id, mmsi, kind, observed_at, data FROM vessel_state"
        clauses: list[str] = []
        params: list = []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if mmsi is not None:
            clauses.append("mmsi = ?")
            params.append(mmsi)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_id, mmsi, kind"
        return list(self._conn.execute(sql, params).fetchall())

    def _rows(
        self,
        source_id: Optional[str] = None,
        mmsi: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        with self._lock:
            return self._fetch_rows(source_id, mmsi)

    @staticmethod
    def _to_snapshot(
        source_id: str, mmsi: int, rows: list[sqlite3.Row]
    ) -> VesselSnapshot:
        snapshot = VesselSnapshot(source_id=source_id, mmsi=mmsi)
        for row in rows:
            observed_at = _parse_observed_at(row["observed_at"])
            data = json.loads(row["data"])
            if row["kind"] == KIND_POSITION:
                snapshot.position = TimedPositionReport(
                    observed_at=observed_at, data=PositionReport(**data)
                )
            else:
                snapshot.static = TimedStaticAndVoyageData(
                    observed_at=observed_at, data=StaticAndVoyageData(**data)
                )
        return snapshot

    def get_snapshot(self, source_id: str, mmsi: int) -> Optional[VesselSnapshot]:
        rows = self._rows(source_id=source_id, mmsi=mmsi)
        if not rows:
            return None
        return self._to_snapshot(source_id, mmsi, rows)

    def list_snapshots(
        self,
        source_id: Optional[str] = None,
        mmsi: Optional[int] = None,
    ) -> list[VesselSnapshot]:
        grouped: dict[tuple[str, int], list[sqlite3.Row]] = {}
        for row in self._rows(source_id=source_id, mmsi=mmsi):
            grouped.setdefault((row["source_id"], row["mmsi"]), []).append(row)
        return [
            self._to_snapshot(src, mmsi, rows)
            for (src, mmsi), rows in sorted(grouped.items())
        ]

    # -- track ----------------------------------------------------------------

    def vessel_exists(self, source_id: str, mmsi: int) -> bool:
        """True when the vessel has any stored snapshot or track point."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM vessel_state WHERE source_id = ? AND mmsi = ? LIMIT 1",
                (source_id, mmsi),
            ).fetchone()
            if row is not None:
                return True
            row = self._conn.execute(
                "SELECT 1 FROM vessel_track WHERE source_id = ? AND mmsi = ? LIMIT 1",
                (source_id, mmsi),
            ).fetchone()
            return row is not None

    def get_track(self, source_id: str, mmsi: int) -> list[TrackPoint]:
        """All valid stored position reports for one vessel, oldest first.

        Ordered by ``observed_at`` (then original sentence index); points
        are stored as decoded, including null coordinates. Points marked
        invalid (misreports) are not returned.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT observed_at, data FROM vessel_track
                WHERE source_id = ? AND mmsi = ? AND valid = 1
                ORDER BY observed_at, sentence_index
                """,
                (source_id, mmsi),
            ).fetchall()
        return [
            TrackPoint(
                observed_at=_parse_observed_at(row["observed_at"]),
                data=PositionReport(**json.loads(row["data"])),
            )
            for row in rows
        ]

    # -- track point invalidation ----------------------------------------------

    def _fallback_position_snapshot(
        self, source_id: str, mmsi: int, observed_at: str
    ) -> None:
        """Fall the position snapshot back to the latest valid track point.

        Only acts when the stored position snapshot is exactly the point
        being invalidated — invalidating an older point never rolls back a
        newer valid snapshot. When no valid point remains, the position
        part is removed (the static part is untouched). Caller must hold
        the lock and the transaction.
        """
        row = self._conn.execute(
            """
            SELECT observed_at FROM vessel_state
            WHERE source_id = ? AND mmsi = ? AND kind = ?
            """,
            (source_id, mmsi, KIND_POSITION),
        ).fetchone()
        if row is None or row["observed_at"] != observed_at:
            return
        fallback = self._conn.execute(
            """
            SELECT observed_at, data FROM vessel_track
            WHERE source_id = ? AND mmsi = ? AND valid = 1
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (source_id, mmsi),
        ).fetchone()
        if fallback is None:
            self._conn.execute(
                """
                DELETE FROM vessel_state
                WHERE source_id = ? AND mmsi = ? AND kind = ?
                """,
                (source_id, mmsi, KIND_POSITION),
            )
        else:
            self._conn.execute(
                """
                UPDATE vessel_state
                SET observed_at = ?, data = ?
                WHERE source_id = ? AND mmsi = ? AND kind = ?
                """,
                (
                    fallback["observed_at"],
                    fallback["data"],
                    source_id,
                    mmsi,
                    KIND_POSITION,
                ),
            )

    def invalidate_point(
        self,
        source_id: str,
        mmsi: int,
        observed_at: str,
        reason: str,
        invalidated_at: str,
    ) -> Optional[tuple[TrackPoint, str, str]]:
        """Mark one stored track point as a misreport, atomically.

        The point stays stored but becomes invisible to ``GET /tracks``;
        when the position snapshot is exactly this point it falls back to
        the latest valid point (or is removed). The marking, the
        visibility change and the snapshot fall-back commit in one
        transaction. Returns ``(original_point, reason, invalidated_at)``
        of the *first* invalidation record — a repeated marking changes
        nothing and returns the first record — or ``None`` when no track
        point exists for the key.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT data, valid, invalid_reason, invalidated_at
                FROM vessel_track
                WHERE source_id = ? AND mmsi = ? AND observed_at = ?
                """,
                (source_id, mmsi, observed_at),
            ).fetchone()
            if row is None:
                return None
            point = TrackPoint(
                observed_at=_parse_observed_at(observed_at),
                data=PositionReport(**json.loads(row["data"])),
            )
            if not row["valid"]:
                # already invalid: the first record wins, nothing changes
                return point, row["invalid_reason"], row["invalidated_at"]
            self._conn.execute(
                """
                UPDATE vessel_track
                SET valid = 0, invalid_reason = ?, invalidated_at = ?
                WHERE source_id = ? AND mmsi = ? AND observed_at = ?
                """,
                (reason, invalidated_at, source_id, mmsi, observed_at),
            )
            self._fallback_position_snapshot(source_id, mmsi, observed_at)
            return point, reason, invalidated_at

    # -- review batches -------------------------------------------------------

    def create_review(
        self,
        batch_id: str,
        observed_at: str,
        created_at: str,
        request_json: str,
        results_json: str,
    ) -> None:
        """Store a new pending review batch (snapshots are not touched)."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO review_batch
                    (batch_id, status, observed_at, created_at, request, results)
                VALUES (?, 'pending', ?, ?, ?, ?)
                """,
                (batch_id, observed_at, created_at, request_json, results_json),
            )

    def get_review(self, batch_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM review_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()

    def list_reviews(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM review_batch"
        params: list = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at, batch_id"
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def resolve_review(
        self,
        batch_id: str,
        resolved_at: str,
        selections: list[list[int]],
        writes: list[tuple[str, int, str, str, dict, int]],
    ) -> Optional[list[VesselSnapshot]]:
        """Apply snapshot/track *writes* and mark the batch resolved, atomically.

        *writes* are
        ``(source_id, mmsi, kind, observed_at, data, sentence_index)``
        tuples in original-sentence-index order; each is applied with the
        usual strictly-newer snapshot rule (newer snapshots are never
        rolled back), while every position report also enters the track.
        The status flip, every snapshot write and every track insert
        commit or roll back together. Returns the involved vessels'
        snapshots as actually stored, or ``None`` when the batch is
        unknown or already resolved (nothing is written then).
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM review_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            for source_id, mmsi, kind, observed_at, data, sentence_index in writes:
                self._write_message(
                    source_id, mmsi, kind, observed_at, data, sentence_index
                )
            involved = sorted({(w[0], w[1]) for w in writes})
            snapshots = [
                self._to_snapshot(source_id, mmsi, self._fetch_rows(source_id, mmsi))
                for source_id, mmsi in involved
            ]
            resolution = json.dumps(
                {
                    "selections": selections,
                    "snapshots": [s.model_dump(mode="json") for s in snapshots],
                },
                separators=(",", ":"),
            )
            self._conn.execute(
                """
                UPDATE review_batch
                SET status = 'resolved', resolved_at = ?, resolution = ?
                WHERE batch_id = ?
                """,
                (resolved_at, resolution, batch_id),
            )
            return snapshots
