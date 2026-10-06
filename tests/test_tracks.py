"""Integration tests for GET /tracks and the position track semantics."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.ais import decode_payload
from app.storage import (
    KIND_POSITION,
    SnapshotStore,
    normalize_observed_at,
)
from tests.conftest import (
    AMBIG_F1A,
    AMBIG_F1B,
    AMBIG_F2,
    SINGLE_FRAGMENT,
    SINGLE_PAYLOAD,
    VDO_SENTINELS,
    make_sentence,
)

T1 = "2026-10-05T11:00:00Z"
T2 = "2026-10-05T12:00:00Z"
T3 = "2026-10-05T13:00:00Z"
T4 = "2026-10-05T14:00:00Z"

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


def track(client: TestClient, source_id: str = "rx-1", mmsi: int = MMSI):
    response = client.get(
        "/tracks", params={"source_id": source_id, "mmsi": mmsi}
    )
    return response.status_code, response.json()


def times(body: dict) -> list[str]:
    return [p["observed_at"] for p in body["points"]]


def lons(body: dict) -> list:
    return [p["data"]["longitude"] for p in body["points"]]


# -- basic recording -----------------------------------------------------------


def test_track_returns_position_reports_in_observed_at_order(client):
    # ingest out of chronological order: T2, then older T1, then newer T3
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    ingest(client, [one("rx-1", OTHER_POSITION)], T1)  # late older report
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T3)

    status, body = track(client)
    assert status == 200
    assert body["source_id"] == "rx-1"
    assert body["mmsi"] == MMSI
    assert len(body["points"]) == 3
    assert times(body) == sorted(times(body))
    assert [t.startswith(ts) for t, ts in zip(times(body), (T1, T2, T3))]
    # the late T1 point was kept even though it never rolled the snapshot
    assert lons(body) == pytest.approx(
        [-73.980087, -73.9835, -73.9835], abs=1e-6
    )


def test_late_observation_enters_track_but_not_snapshot(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    ingest(client, [one("rx-1", OTHER_POSITION)], T1)  # late older report

    vessels = client.get(
        "/vessels", params={"source_id": "rx-1", "mmsi": MMSI}
    ).json()
    # the snapshot stays at the newer T2 state
    assert vessels[0]["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert vessels[0]["position"]["data"]["longitude"] == pytest.approx(-73.9835)
    # the track keeps both, oldest first
    status, body = track(client)
    assert len(body["points"]) == 2
    assert lons(body)[0] == pytest.approx(-73.980087)


def test_repeated_submission_at_same_observed_at_adds_no_point(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    # same sentence resubmitted at the same batch time -> no new point
    status, body = ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    assert status == 200

    _, body = track(client)
    assert len(body["points"]) == 1
    assert lons(body) == pytest.approx([-73.9835], abs=1e-6)

    # a different position at the same time also loses (first saved wins)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    _, body = track(client)
    assert len(body["points"]) == 1
    assert lons(body) == pytest.approx([-73.9835], abs=1e-6)


def test_same_batch_multiple_positions_add_one_point(client):
    # two position reports share the batch observed_at; index 0 wins
    status, body = ingest(
        client, [one("rx-1", SINGLE_FRAGMENT), one("rx-1", OTHER_POSITION)], T2
    )
    assert status == 200
    _, body = track(client)
    assert len(body["points"]) == 1
    assert lons(body) == pytest.approx([-73.9835], abs=1e-6)


def test_null_coordinates_kept_in_track_point(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)  # real coordinates
    ingest(client, [one("rx-1", VDO_SENTINELS)], T2)   # null lon / sog

    _, body = track(client)
    assert len(body["points"]) == 2
    null_point = body["points"][1]["data"]
    assert null_point["longitude"] is None
    assert null_point["speed_over_ground"] is None
    assert null_point["latitude"] == pytest.approx(40.7484)  # as decoded
    # the older point is not borrowed to fill anything
    assert body["points"][0]["data"]["longitude"] == pytest.approx(-73.9835)


def test_static_only_vessel_has_empty_track(client):
    # only a message 5 is stored: the vessel exists but has no positions
    ingest(
        client,
        [
            one(
                "rx-1",
                "!AIVDM,2,1,4,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*5E",
            ),
            one("rx-1", "!AIVDM,2,2,4,A,NECQEp6ClRh000000000000,2*1F"),
        ],
        T1,
    )
    status, body = track(client)
    assert status == 200
    assert body["points"] == []


def test_invalid_and_incomplete_messages_create_no_track(client):
    bad = SINGLE_FRAGMENT.replace("15Mwqgh", "15Mwqgi")  # bad checksum
    ingest(client, [one("rx-1", bad)], T2)
    ingest(
        client,
        [one("rx-1", "!AIVDM,2,1,9,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*53")],
        T2,
    )
    status, _ = track(client)
    assert status == 404


def test_ambiguous_ingest_records_unambiguous_positions_only(client):
    status, body = ingest(
        client,
        [
            one("rx-2", SINGLE_FRAGMENT),  # unambiguous decode
            one("rx-2", AMBIG_F1A),
            one("rx-2", AMBIG_F1B),
            one("rx-2", AMBIG_F2),
        ],
        T2,
    )
    assert status == 409
    # the ambiguous candidates created no points for their MMSIs
    assert track(client, "rx-2", NORTHERN)[0] == 404
    assert track(client, "rx-2", 366000002)[0] == 404
    # the unambiguous position was recorded
    status, body = track(client, "rx-2", MMSI)
    assert status == 200
    assert len(body["points"]) == 1


def test_tracks_are_separate_per_source(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    ingest(client, [one("rx-2", OTHER_POSITION)], T2)

    _, body_a = track(client, "rx-1", MMSI)
    _, body_b = track(client, "rx-2", MMSI)
    assert lons(body_a) == pytest.approx([-73.9835], abs=1e-6)
    assert lons(body_b) == pytest.approx([-73.980087], abs=1e-6)


def test_track_unknown_vessel_is_404(client):
    status, body = track(client, "rx-1", 999999999)
    assert status == 404
    assert "999999999" in body["detail"]


def test_track_requires_query_parameters(client):
    assert client.get("/tracks", params={"source_id": "rx-1"}).status_code == 422
    assert client.get("/tracks", params={"mmsi": MMSI}).status_code == 422
    assert client.get("/tracks").status_code == 422


def test_track_persists_across_restart(client, monkeypatch):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    monkeypatch.setattr(main, "_store", None)  # simulate a container restart
    status, body = track(client)
    assert status == 200
    assert len(body["points"]) == 2
    assert times(body)[0].startswith("2026-10-05T11:00:00")


# -- review resolution ---------------------------------------------------------


def test_resolved_review_writes_track_points(client):
    # ambiguous batch created at the older T1; unambiguous position too
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

    # a newer position arrives before the late human resolution
    ingest(client, [one("rx-2", OTHER_POSITION)], T2)

    # creating the batch created no track points for its candidates
    assert track(client, "rx-2", NORTHERN)[0] == 404

    response = client.post(
        f"/reviews/{batch_id}/resolve", json={"selections": [[1, 3]]}
    )
    assert response.status_code == 200

    # the selected candidate is a message 5 (static) -> no track point
    # for NORTHERN; the unambiguous T1 position of MMSI entered the track
    status, body = track(client, "rx-2", MMSI)
    assert status == 200
    assert len(body["points"]) == 2  # late T1 + newer T2, oldest first
    assert times(body)[0].startswith("2026-10-05T11:00:00")
    assert lons(body)[0] == pytest.approx(-73.9835)
    # and the snapshot still shows the newer T2 report (no rollback)
    vessels = client.get(
        "/vessels", params={"source_id": "rx-2", "mmsi": MMSI}
    ).json()
    assert vessels[0]["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert vessels[0]["position"]["data"]["longitude"] == pytest.approx(-73.980087)


def test_rejected_resolution_writes_no_track_points(client):
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
    # forged candidate -> whole resolution rejected
    response = client.post(
        f"/reviews/{batch_id}/resolve", json={"selections": [[1, 2]]}
    )
    assert response.status_code == 422
    # neither the candidates nor the batch's unambiguous decode landed
    assert track(client, "rx-2", NORTHERN)[0] == 404
    assert track(client, "rx-2", MMSI)[0] == 404


# -- migration from an older database ------------------------------------------


def test_legacy_snapshot_is_seeded_as_single_track_start(tmp_path):
    """An old database with only vessel_state rows gets one starting point."""
    path = str(tmp_path / "legacy.db")

    # build the database the way the previous release did: vessel_state only
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
    import json

    message = decode_payload(SINGLE_PAYLOAD, 0).model_dump()
    conn.execute(
        "INSERT INTO vessel_state VALUES (?, ?, ?, ?, ?)",
        (
            "rx-1",
            MMSI,
            KIND_POSITION,
            "2026-10-05T12:00:00.000000Z",
            json.dumps(message),
        ),
    )
    conn.commit()
    conn.close()

    store = SnapshotStore(path)
    points = store.get_track("rx-1", MMSI)
    assert len(points) == 1  # exactly one starting point, no inferred history
    assert points[0].observed_at == datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert points[0].data.longitude == pytest.approx(-73.9835)
    # the seeded point does not duplicate itself on a second open
    store.close()
    reopened = SnapshotStore(path)
    assert len(reopened.get_track("rx-1", MMSI)) == 1
    reopened.close()


# -- atomicity -----------------------------------------------------------------


def test_track_and_snapshot_commit_or_roll_back_together(tmp_path, monkeypatch):
    """A failure mid-batch rolls back both track points and snapshots."""
    path = str(tmp_path / "atomic.db")
    store = SnapshotStore(path)
    observed = normalize_observed_at(
        datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    )
    message = decode_payload(SINGLE_PAYLOAD, 0).model_dump()

    good = ("rx-1", MMSI, KIND_POSITION, observed, message, 0)
    # a write whose payload cannot be serialised to JSON
    bad = ("rx-1", MMSI, KIND_POSITION, observed, {"oops": object()}, 1)

    with pytest.raises(TypeError):
        store.apply_ingest([good, bad])

    # the first write rolled back together with the failing second one
    assert store.get_track("rx-1", MMSI) == []
    assert store.get_snapshot("rx-1", MMSI) is None
    store.close()
