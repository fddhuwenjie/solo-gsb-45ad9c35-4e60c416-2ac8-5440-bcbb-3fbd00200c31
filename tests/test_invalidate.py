"""Integration tests for POST /tracks/invalidate (misreport handling)."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.ais import decode_payload
from app.storage import KIND_POSITION, SnapshotStore
from tests.conftest import (
    AMBIG_F1A,
    AMBIG_F1B,
    AMBIG_F2,
    MULTI_FRAG1,
    MULTI_FRAG2,
    SINGLE_FRAGMENT,
    SINGLE_PAYLOAD,
    make_sentence,
)

T1 = "2026-10-05T11:00:00Z"
T2 = "2026-10-05T12:00:00Z"
T3 = "2026-10-05T13:00:00Z"

# Same MMSI as SINGLE_FRAGMENT (366999999) but different longitudes.
OTHER_POSITION = make_sentence("AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0")
THIRD_POSITION = make_sentence("AIVDM,1,1,,A,15Mwqgh01DreG9HGD@44gkg40000,0")

MMSI = 366999999
NORTHERN = 366000001  # AMBIG_F1A + AMBIG_F2 -> "NORTHERN STAR"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a fresh snapshot database per test."""
    monkeypatch.setenv("AIS_DB_PATH", str(tmp_path / "snapshots.db"))
    monkeypatch.setattr(main, "_store", None)
    yield TestClient(main.app)
    monkeypatch.setattr(main, "_store", None)


def one(source_id: str, sentence: str) -> dict:
    return {"source_id": source_id, "sentence": sentence}


def ingest(client: TestClient, sentences: list[dict], observed_at: str):
    response = client.post(
        "/ingest", json={"observed_at": observed_at, "sentences": sentences}
    )
    return response.status_code, response.json()


def invalidate(
    client: TestClient,
    observed_at: str,
    reason: str = "misreport",
    source_id: str = "rx-1",
    mmsi: int = MMSI,
):
    return client.post(
        "/tracks/invalidate",
        json={
            "source_id": source_id,
            "mmsi": mmsi,
            "observed_at": observed_at,
            "reason": reason,
        },
    )


def track(client: TestClient, source_id: str = "rx-1", mmsi: int = MMSI):
    response = client.get("/tracks", params={"source_id": source_id, "mmsi": mmsi})
    return response.status_code, response.json()


def vessels(client: TestClient, source_id: str = "rx-1", mmsi: int = MMSI):
    return client.get("/vessels", params={"source_id": source_id, "mmsi": mmsi}).json()


def times(body: dict) -> list[str]:
    return [p["observed_at"] for p in body["points"]]


# -- marking and track visibility ----------------------------------------------


def test_invalidate_returns_record_with_original_point(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    response = invalidate(client, T2, reason="shore station misreport")
    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == "rx-1"
    assert body["mmsi"] == MMSI
    assert body["observed_at"].startswith("2026-10-05T12:00:00")
    assert body["reason"] == "shore station misreport"
    assert body["invalidated_at"]  # first-marking time is recorded
    # the original point is returned as stored before invalidation
    assert body["point"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert body["point"]["data"]["mmsi"] == MMSI
    assert body["point"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_tracks_only_returns_valid_points(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    ingest(client, [one("rx-1", THIRD_POSITION)], T3)

    assert invalidate(client, T2).status_code == 200

    status, body = track(client)
    assert status == 200
    assert len(body["points"]) == 2
    assert times(body)[0].startswith("2026-10-05T11:00:00")
    assert times(body)[1].startswith("2026-10-05T13:00:00")


def test_invalidation_is_keyed_per_source(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    ingest(client, [one("rx-2", SINGLE_FRAGMENT)], T2)

    assert invalidate(client, T2, source_id="rx-1").status_code == 200

    # the same (mmsi, observed_at) point of another source stays valid
    _, body = track(client, "rx-2")
    assert len(body["points"]) == 1
    _, body = track(client, "rx-1")
    assert body["points"] == []


# -- snapshot fall-back ----------------------------------------------------------


def test_snapshot_falls_back_to_latest_valid_point(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)  # snapshot is the T2 point

    assert invalidate(client, T2).status_code == 200

    (snapshot,) = vessels(client)
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T11:00:00")
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_position_part_removed_when_no_valid_point_remains(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", MULTI_FRAG1), one("rx-1", MULTI_FRAG2)], T2)

    assert invalidate(client, T1).status_code == 200

    (snapshot,) = vessels(client)
    assert snapshot["position"] is None  # position part removed
    assert snapshot["static"]["data"]["name"] == "MV CLAUDE"  # static kept


def test_vessel_without_static_disappears_from_vessels_but_track_stays(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)

    assert invalidate(client, T2).status_code == 200

    assert vessels(client) == []  # no snapshot parts remain
    status, body = track(client)  # the vessel still exists, with no valid points
    assert status == 200
    assert body["points"] == []


def test_invalidating_older_point_does_not_roll_back_newer_snapshot(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)  # newer, valid snapshot

    assert invalidate(client, T1).status_code == 200

    (snapshot,) = vessels(client)
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.980087)
    _, body = track(client)
    assert len(body["points"]) == 1  # only the valid T2 point remains visible


# -- not found and idempotency ---------------------------------------------------


def test_invalidate_unknown_point_is_404(client):
    response = invalidate(client, T2)
    assert response.status_code == 404
    assert str(MMSI) in response.json()["detail"]

    # a stored vessel but no point at this exact observed_at -> still 404
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    assert invalidate(client, T1).status_code == 404
    # the stored point is untouched
    _, body = track(client)
    assert len(body["points"]) == 1


def test_repeated_invalidate_returns_first_record(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)

    first = invalidate(client, T2, reason="first reason")
    assert first.status_code == 200
    second = invalidate(client, T2, reason="changed reason")
    assert second.status_code == 200
    # the first record is returned; the reason is not changed
    assert second.json()["reason"] == "first reason"
    assert second.json()["invalidated_at"] == first.json()["invalidated_at"]
    assert second.json()["point"]["data"]["longitude"] == pytest.approx(-73.9835)

    _, body = track(client)
    assert body["points"] == []


def test_invalidate_validates_the_request(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    base = {"source_id": "rx-1", "mmsi": MMSI, "observed_at": T2, "reason": "r"}
    for missing in ("source_id", "mmsi", "observed_at", "reason"):
        payload = {k: v for k, v in base.items() if k != missing}
        assert client.post("/tracks/invalidate", json=payload).status_code == 422
    assert (
        client.post("/tracks/invalidate", json={**base, "reason": ""}).status_code
        == 422
    )
    # nothing was invalidated by the rejected requests
    _, body = track(client)
    assert len(body["points"]) == 1


def test_naive_and_offset_observed_at_match_the_stored_point(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)  # stored as UTC
    # 14:00 at +02:00 is the same instant as 12:00 UTC
    response = client.post(
        "/tracks/invalidate",
        json={
            "source_id": "rx-1",
            "mmsi": MMSI,
            "observed_at": "2026-10-05T14:00:00+02:00",
            "reason": "offset form",
        },
    )
    assert response.status_code == 200
    _, body = track(client)
    assert body["points"] == []


# -- later observations cannot re-activate the point ------------------------------


def test_ingest_at_invalidated_time_neither_reactivates_nor_rewrites_snapshot(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    assert invalidate(client, T2).status_code == 200  # snapshot fell back to T1

    # the same misreported position arrives again for the invalidated time
    status, body = ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    assert status == 200
    assert body["summary"]["decoded"] == 1  # decode contract unchanged
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T11:00:00")

    _, body = track(client)
    assert len(body["points"]) == 1  # the invalid point was not re-activated
    assert times(body)[0].startswith("2026-10-05T11:00:00")
    (snapshot,) = vessels(client)
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)

    # other observations still follow the usual rules
    ingest(client, [one("rx-1", THIRD_POSITION)], T3)
    (snapshot,) = vessels(client)
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T13:00:00")
    _, body = track(client)
    assert len(body["points"]) == 2


def test_static_message_at_invalidated_time_still_applies(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    assert invalidate(client, T2).status_code == 200

    # a static/voyage message at the same observed_at is a different
    # timeline and is stored normally
    status, body = ingest(
        client, [one("rx-1", MULTI_FRAG1), one("rx-1", MULTI_FRAG2)], T2
    )
    assert status == 200
    (snapshot,) = vessels(client)
    assert snapshot["position"] is None
    assert snapshot["static"]["data"]["name"] == "MV CLAUDE"
    assert snapshot["static"]["observed_at"].startswith("2026-10-05T12:00:00")


def test_review_resolution_at_invalidated_time_does_not_reactivate(client):
    batch_id = client.post(
        "/reviews",
        json={
            "observed_at": T1,
            "sentences": [
                one("rx-2", SINGLE_FRAGMENT),  # unambiguous position @T1
                one("rx-2", AMBIG_F1A),
                one("rx-2", AMBIG_F1B),
                one("rx-2", AMBIG_F2),
            ],
        },
    ).json()["batch_id"]
    # the same position is ingested and then invalidated before resolution
    ingest(client, [one("rx-2", SINGLE_FRAGMENT)], T1)
    assert invalidate(client, T1, source_id="rx-2").status_code == 200

    response = client.post(
        f"/reviews/{batch_id}/resolve", json={"selections": [[1, 3]]}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"

    # the resolved position write at the invalidated time was skipped
    _, body = track(client, "rx-2")
    assert body["points"] == []
    assert vessels(client, "rx-2") == []
    # the selected static candidate of the same batch was stored normally
    (northern,) = vessels(client, "rx-2", NORTHERN)
    assert northern["static"]["data"]["name"] == "NORTHERN STAR"


def test_review_records_are_unaffected_by_invalidation(client):
    batch_id = client.post(
        "/reviews",
        json={
            "observed_at": T1,
            "sentences": [
                one("rx-2", SINGLE_FRAGMENT),
                one("rx-2", AMBIG_F1A),
                one("rx-2", AMBIG_F1B),
                one("rx-2", AMBIG_F2),
            ],
        },
    ).json()["batch_id"]
    client.post(f"/reviews/{batch_id}/resolve", json={"selections": [[1, 3]]})
    recorded = client.get(f"/reviews/{batch_id}").json()
    assert recorded["status"] == "resolved"
    assert recorded["snapshots"]  # resolution stored a position snapshot

    # invalidating the stored point must not rewrite the resolved record
    assert invalidate(client, T1, source_id="rx-2").status_code == 200
    assert client.get(f"/reviews/{batch_id}").json() == recorded

    # a pending batch is untouched as well
    pending_id = client.post(
        "/reviews",
        json={
            "observed_at": T2,
            "sentences": [one("rx-2", AMBIG_F1A), one("rx-2", AMBIG_F1B), one("rx-2", AMBIG_F2)],
        },
    ).json()["batch_id"]
    pending = client.get(f"/reviews/{pending_id}").json()
    assert pending["status"] == "pending"
    assert client.get(f"/reviews/{pending_id}").json() == pending


# -- persistence and migration ----------------------------------------------------


def test_invalidation_survives_restart(client, monkeypatch):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    first = invalidate(client, T2, reason="kept across restart").json()

    monkeypatch.setattr(main, "_store", None)  # simulate a container restart

    _, body = track(client)
    assert len(body["points"]) == 1
    assert times(body)[0].startswith("2026-10-05T11:00:00")
    (snapshot,) = vessels(client)
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T11:00:00")
    # the first invalidation record is still the one returned
    again = invalidate(client, T2, reason="another attempt").json()
    assert again["reason"] == "kept across restart"
    assert again["invalidated_at"] == first["invalidated_at"]


def test_legacy_database_is_migrated_and_invalidation_works(tmp_path):
    """A database with the pre-invalidation track schema is migrated in place."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE vessel_state (
            source_id TEXT NOT NULL, mmsi INTEGER NOT NULL, kind TEXT NOT NULL,
            observed_at TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (source_id, mmsi, kind)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE vessel_track (
            source_id TEXT NOT NULL, mmsi INTEGER NOT NULL,
            observed_at TEXT NOT NULL, sentence_index INTEGER NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (source_id, mmsi, observed_at)
        ) WITHOUT ROWID
        """
    )
    message = json.dumps(decode_payload(SINGLE_PAYLOAD, 0).model_dump())
    observed = "2026-10-05T12:00:00.000000Z"
    conn.execute(
        "INSERT INTO vessel_state VALUES (?, ?, ?, ?, ?)",
        ("rx-1", MMSI, KIND_POSITION, observed, message),
    )
    conn.execute(
        "INSERT INTO vessel_track VALUES (?, ?, ?, ?, ?)",
        ("rx-1", MMSI, observed, 0, message),
    )
    conn.commit()
    conn.close()

    store = SnapshotStore(path)
    columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(vessel_track)")
    }
    assert {"valid", "invalid_reason", "invalidated_at"} <= columns
    assert len(store.get_track("rx-1", MMSI)) == 1  # old points stay valid

    record = store.invalidate_point(
        "rx-1", MMSI, observed, "legacy misreport", "2026-10-06T00:00:00.000000Z"
    )
    assert record is not None
    point, reason, _ = record
    assert point.data.longitude == pytest.approx(-73.9835)
    assert reason == "legacy misreport"
    assert store.get_track("rx-1", MMSI) == []
    assert store.get_snapshot("rx-1", MMSI) is None  # position part removed
    store.close()

    reopened = SnapshotStore(path)  # the invalidation survives a restart
    assert reopened.get_track("rx-1", MMSI) == []
    assert reopened.invalidate_point(
        "rx-1", MMSI, observed, "changed", "2026-10-06T01:00:00.000000Z"
    )[1] == "legacy misreport"
    reopened.close()


def test_failed_invalidation_rolls_back_atomically(tmp_path, monkeypatch):
    """A failure during the snapshot fall-back keeps the point valid."""
    store = SnapshotStore(str(tmp_path / "atomic.db"))
    observed = "2026-10-05T12:00:00.000000Z"
    message = decode_payload(SINGLE_PAYLOAD, 0).model_dump()
    store.apply_ingest([("rx-1", MMSI, KIND_POSITION, observed, message, 0)])

    def boom(source_id, mmsi, observed_at):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store, "_fallback_position_snapshot", boom)
    with pytest.raises(RuntimeError):
        store.invalidate_point(
            "rx-1", MMSI, observed, "r", "2026-10-06T00:00:00.000000Z"
        )

    # the marking was rolled back together with the failed fall-back
    assert len(store.get_track("rx-1", MMSI)) == 1
    assert store.get_snapshot("rx-1", MMSI).position is not None
    store.close()
