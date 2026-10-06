"""Integration tests for POST /tracks/invalidations (false report flagging)."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.storage import SnapshotStore
from tests.conftest import (
    AMBIG_F1A,
    AMBIG_F1B,
    AMBIG_F2,
    SINGLE_FRAGMENT,
    make_sentence,
)

T1 = "2026-10-05T11:00:00Z"
T2 = "2026-10-05T12:00:00Z"
T3 = "2026-10-05T13:00:00Z"

# Same MMSI as SINGLE_FRAGMENT (366999999) but a different longitude.
OTHER_POSITION = make_sentence("AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0")

MMSI = 366999999
NORTHERN = 366000001


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


def invalidate(client: TestClient, observed_at: str, reason: str,
               source_id: str = "rx-1", mmsi: int = MMSI):
    response = client.post(
        "/tracks/invalidations",
        json={
            "source_id": source_id,
            "mmsi": mmsi,
            "observed_at": observed_at,
            "reason": reason,
        },
    )
    return response.status_code, response.json()


def point_times(client: TestClient, source_id: str = "rx-1", mmsi: int = MMSI):
    response = client.get(
        "/tracks", params={"source_id": source_id, "mmsi": mmsi}
    )
    if response.status_code == 404:
        return []
    return [p["observed_at"] for p in response.json()["points"]]


def snapshot(client: TestClient, source_id: str = "rx-1", mmsi: int = MMSI):
    snapshots = client.get(
        "/vessels", params={"source_id": source_id, "mmsi": mmsi}
    ).json()
    return snapshots[0] if snapshots else None


# -- basic marking -------------------------------------------------------------


def test_invalidation_marks_point_and_returns_original(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)

    status, body = invalidate(client, T2, "false report confirmed by station")
    assert status == 200
    assert body["source_id"] == "rx-1"
    assert body["mmsi"] == MMSI
    assert body["observed_at"].startswith("2026-10-05T12:00:00")
    assert body["reason"] == "false report confirmed by station"
    assert body["invalidated_at"]
    # the original point comes back intact
    point = body["point"]
    assert point["observed_at"].startswith("2026-10-05T12:00:00")
    assert point["data"]["mmsi"] == MMSI
    assert point["data"]["longitude"] == pytest.approx(-73.9835)


def test_invalidated_point_disappears_from_tracks(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)

    invalidate(client, T1, "false report")

    response = client.get(
        "/tracks", params={"source_id": "rx-1", "mmsi": MMSI}
    )
    assert response.status_code == 200
    times = [p["observed_at"] for p in response.json()["points"]]
    assert len(times) == 1
    assert times[0].startswith("2026-10-05T12:00:00")


def test_invalidate_unknown_point_is_404(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)

    # unknown observed_at
    status, body = invalidate(client, T3, "no such point")
    assert status == 404
    assert "2026-10-05T13:00:00" in body["detail"]

    # unknown MMSI
    status, body = invalidate(client, T1, "no such vessel", mmsi=999999999)
    assert status == 404

    # unknown source
    status, body = invalidate(client, T1, "no such source", source_id="rx-9")
    assert status == 404


def test_invalidation_requires_reason(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    response = client.post(
        "/tracks/invalidations",
        json={"source_id": "rx-1", "mmsi": MMSI, "observed_at": T1},
    )
    assert response.status_code == 422
    response = client.post(
        "/tracks/invalidations",
        json={
            "source_id": "rx-1", "mmsi": MMSI, "observed_at": T1, "reason": "  ",
        },
    )
    assert response.status_code == 422


def test_repeated_invalidation_returns_first_record_and_keeps_reason(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)

    status, first = invalidate(client, T2, "first reason")
    assert status == 200
    first_time = first["invalidated_at"]

    status, again = invalidate(client, T2, "a different later reason")
    assert status == 200
    assert again["reason"] == "first reason"
    assert again["invalidated_at"] == first_time
    assert again["point"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_invalidation_matches_normalized_observed_at(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    # equivalent instant with an offset is the same point
    status, body = invalidate(
        client, "2026-10-05T14:00:00+02:00", "offset form"
    )
    assert status == 200
    assert body["observed_at"].startswith("2026-10-05T12:00:00")


# -- snapshot reconciliation ---------------------------------------------------


def test_invalidating_live_snapshot_rewinds_to_latest_valid_point(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)   # lon -73.9835
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)    # lon -73.980087
    assert snapshot(client)["position"]["observed_at"].startswith(T2)

    invalidate(client, T2, "false report")

    snap = snapshot(client)
    assert snap["position"]["observed_at"].startswith(T1)
    assert snap["position"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_invalidating_older_point_does_not_roll_back_newer_snapshot(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)

    invalidate(client, T1, "false report")

    snap = snapshot(client)
    assert snap["position"]["observed_at"].startswith(T2)
    assert snap["position"]["data"]["longitude"] == pytest.approx(-73.980087)


def test_invalidating_only_position_removes_position_side_keeps_static(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(
        client,
        [
            one(
                "rx-1",
                "!AIVDM,2,1,4,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*5E",
            ),
            one("rx-1", "!AIVDM,2,2,4,A,NECQEp6ClRh000000000000,2*1F"),
        ],
        T2,
    )

    invalidate(client, T1, "false report")

    snap = snapshot(client)
    assert snap is not None
    assert snap["position"] is None
    assert snap["static"]["data"]["name"] == "MV CLAUDE"

    # no valid points left: the track itself is empty (the vessel still
    # exists via its static data, so /tracks stays 200 with [])
    response = client.get(
        "/tracks", params={"source_id": "rx-1", "mmsi": MMSI}
    )
    assert response.status_code == 200
    assert response.json()["points"] == []


def test_rewinding_snapshot_can_chain(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T3)

    invalidate(client, T3, "false report")
    assert snapshot(client)["position"]["observed_at"].startswith(T2)

    invalidate(client, T2, "also false")
    assert snapshot(client)["position"]["observed_at"].startswith(T1)
    times = point_times(client)
    assert len(times) == 1
    assert times[0].startswith(T1)


def test_invalidation_isolated_per_source(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    ingest(client, [one("rx-2", OTHER_POSITION)], T2)

    invalidate(client, T2, "false report", source_id="rx-1")

    assert point_times(client, "rx-1") == []
    assert len(point_times(client, "rx-2")) == 1
    assert snapshot(client, "rx-2")["position"]["data"]["longitude"] == pytest.approx(
        -73.980087
    )


# -- later ingest cannot reactivate the point ----------------------------------


def test_later_ingest_at_same_time_does_not_reactivate_or_rewrite_snapshot(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)   # live snapshot
    invalidate(client, T2, "false report")             # rewinds to T1

    # another report arriving for the same source/mmsi/time T2
    status, body = ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    assert status == 200
    assert body["results"][0]["status"] == "decoded"  # response contract unchanged

    # the point stays hidden ...
    times = point_times(client)
    assert len(times) == 1
    assert times[0].startswith(T1)

    # ... and the snapshot stays at T1 (not restored to T2)
    snap = snapshot(client)
    assert snap["position"]["observed_at"].startswith(T1)
    assert snap["position"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_other_observations_still_write_after_invalidation(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    invalidate(client, T1, "false report")

    # a newer valid observation still writes snapshot + track normally
    ingest(client, [one("rx-1", OTHER_POSITION)], T3)
    snap = snapshot(client)
    assert snap["position"]["observed_at"].startswith(T3)
    times = point_times(client)
    assert len(times) == 1
    assert times[0].startswith(T3)


def test_resolved_review_at_invalidated_time_cannot_revive_point(client):
    # position at T1 stored through ingest, then flagged
    ingest(client, [one("rx-2", SINGLE_FRAGMENT)], T1)
    invalidate(client, T1, "false report", source_id="rx-2")

    # ambiguous batch created at T1 as well, resolved later; its
    # unambiguous position shares the invalidated (source, mmsi, time)
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
    response = client.post(
        f"/reviews/{batch_id}/resolve", json={"selections": [[1, 3]]}
    )
    assert response.status_code == 200

    # no position snapshot (invalidated point was the only one) ...
    assert snapshot(client, "rx-2") is None
    # ... and the track stays 404 for the position MMSI (point still hidden)
    response = client.get(
        "/tracks", params={"source_id": "rx-2", "mmsi": MMSI}
    )
    assert response.status_code == 404
    # the static candidate wrote its independent timeline, unaffected
    assert snapshot(client, "rx-2", NORTHERN)["static"]["data"]["name"] == (
        "NORTHERN STAR"
    )
    # the review batch itself remains resolved, with its recorded selection
    detail = client.get(f"/reviews/{batch_id}").json()
    assert detail["status"] == "resolved"
    assert detail["selections"] == [[1, 3]]


# -- durability ----------------------------------------------------------------


def test_invalidation_persists_across_restart(client, monkeypatch):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    invalidate(client, T2, "false report")

    monkeypatch.setattr(main, "_store", None)  # simulate a container restart

    times = point_times(client)
    assert len(times) == 1
    assert times[0].startswith(T1)
    snap = snapshot(client)
    assert snap["position"]["observed_at"].startswith(T1)

    # repeat invalidation after restart still returns the first record
    status, body = invalidate(client, T2, "changed reason")
    assert status == 200
    assert body["reason"] == "false report"


# -- migration from a database without invalidation columns --------------------


def test_old_database_track_table_gains_invalidation_columns(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
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
    conn.execute(
        "CREATE TABLE vessel_state ("
        "source_id TEXT NOT NULL, mmsi INTEGER NOT NULL, kind TEXT NOT NULL, "
        "observed_at TEXT NOT NULL, data TEXT NOT NULL, "
        "PRIMARY KEY (source_id, mmsi, kind))"
    )
    import json

    from app.ais import decode_payload
    from tests.conftest import SINGLE_PAYLOAD

    message = decode_payload(SINGLE_PAYLOAD, 0).model_dump()
    payload = json.dumps(message, separators=(",", ":"), sort_keys=True)
    conn.execute(
        "INSERT INTO vessel_track VALUES (?, ?, ?, ?, ?)",
        ("rx-1", MMSI, "2026-10-05T12:00:00.000000Z", 0, payload),
    )
    conn.commit()
    conn.close()

    store = SnapshotStore(path)
    invalidation = store.invalidate_track_point(
        "rx-1", MMSI, "2026-10-05T12:00:00.000000Z", "false after upgrade",
        "2026-10-05T12:05:00.000000Z",
    )
    assert invalidation is not None
    assert invalidation.reason == "false after upgrade"
    assert store.get_track("rx-1", MMSI) == []
    assert store.get_snapshot("rx-1", MMSI) is None
    store.close()
