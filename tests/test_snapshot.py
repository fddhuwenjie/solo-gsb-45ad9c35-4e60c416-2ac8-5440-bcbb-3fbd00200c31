"""Integration tests for POST /ingest, GET /vessels and the snapshot store."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.ais import decode_payload
from app.storage import SnapshotStore, normalize_observed_at
from tests.conftest import (
    AMBIG_F1A,
    AMBIG_F1B,
    AMBIG_F2,
    INCOMPLETE_F1,
    MULTI_FRAG1,
    MULTI_FRAG2,
    MULTI_PAYLOAD,
    SINGLE_FRAGMENT,
    SINGLE_PAYLOAD,
    VDO_SENTINELS,
    make_sentence,
)

T1 = "2026-10-05T11:00:00Z"
T2 = "2026-10-05T12:00:00Z"
T3 = "2026-10-05T13:00:00Z"

# Same MMSI as SINGLE_FRAGMENT (366999999) but a different longitude.
OTHER_POSITION = make_sentence("AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0")

MMSI = 366999999


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


def test_ingest_position_returns_snapshot(client):
    status, body = ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    assert status == 200
    assert body["summary"]["decoded"] == 1
    (snapshot,) = body["snapshots"]
    assert snapshot["source_id"] == "rx-1"
    assert snapshot["mmsi"] == MMSI
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)
    assert snapshot["static"] is None


def test_ingest_static_voyage_message(client):
    status, body = ingest(client, [one("rx-1", MULTI_FRAG2), one("rx-1", MULTI_FRAG1)], T2)
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["static"]["data"]["name"] == "MV CLAUDE"
    assert snapshot["static"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert snapshot["position"] is None


def test_snapshot_merges_position_and_static_across_batches(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    status, body = ingest(
        client, [one("rx-1", MULTI_FRAG1), one("rx-1", MULTI_FRAG2)], T2
    )
    assert status == 200
    (snapshot,) = body["snapshots"]
    # position side comes from the earlier batch, static side from this one
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T11:00:00")
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)
    assert snapshot["static"]["data"]["destination"] == "NEW YORK"


def test_older_batch_does_not_roll_back_newer_state(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    status, body = ingest(client, [one("rx-1", OTHER_POSITION)], T1)  # older
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_equal_observed_at_first_saved_wins(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    status, body = ingest(client, [one("rx-1", OTHER_POSITION)], T2)  # same time
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_newer_batch_overwrites(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)
    status, body = ingest(client, [one("rx-1", OTHER_POSITION)], T2)
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.980087)


def test_same_batch_first_sentence_index_wins(client):
    status, body = ingest(
        client, [one("rx-1", SINGLE_FRAGMENT), one("rx-1", OTHER_POSITION)], T2
    )
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)

    # reversed order -> the other report is stored
    status, body = ingest(
        client, [one("rx-1", OTHER_POSITION), one("rx-1", SINGLE_FRAGMENT)], T3
    )
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.980087)


def test_null_coordinates_are_not_backfilled_from_history(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T1)  # real coordinates
    status, body = ingest(client, [one("rx-1", VDO_SENTINELS)], T2)  # null lon
    assert status == 200
    (snapshot,) = body["snapshots"]
    position = snapshot["position"]["data"]
    assert position["longitude"] is None
    assert position["speed_over_ground"] is None
    # untouched fields of the new message are kept as decoded
    assert position["latitude"] == pytest.approx(40.7484)


def test_error_and_incomplete_results_are_not_stored(client):
    status, body = ingest(client, [one("rx-3", INCOMPLETE_F1)], T2)
    assert status == 200
    assert body["results"][0]["status"] == "incomplete"
    assert body["snapshots"] == []
    assert client.get("/vessels").json() == []

    bad = SINGLE_FRAGMENT.replace("15Mwqgh", "15Mwqgi")  # checksum mismatch
    status, body = ingest(client, [one("rx-1", bad)], T2)
    assert status == 200
    assert body["results"][0]["status"] == "error"
    assert body["snapshots"] == []


def test_ambiguous_batch_returns_409_but_decoded_messages_update_snapshots(client):
    status, body = ingest(
        client,
        [
            one("rx-2", SINGLE_FRAGMENT),  # decodes unambiguously
            one("rx-2", AMBIG_F1A),
            one("rx-2", AMBIG_F1B),
            one("rx-2", AMBIG_F2),
        ],
        T2,
    )
    assert status == 409
    statuses = {r["status"] for r in body["results"]}
    assert statuses == {"decoded", "ambiguous"}
    # the unambiguous position report was stored despite the 409
    (snapshot,) = body["snapshots"]
    assert snapshot["mmsi"] == MMSI
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)
    # ambiguous candidates (MMSI 366000001 / 366000002) were not stored
    assert client.get("/vessels", params={"mmsi": 366000001}).json() == []
    assert client.get("/vessels", params={"mmsi": 366000002}).json() == []


def test_naive_and_offset_observed_at_are_normalised_to_utc(client):
    status, body = ingest(client, [one("rx-1", SINGLE_FRAGMENT)], "2026-10-05T12:00:00")
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["observed_at"].startswith("2026-10-05T12:00:00")

    # 14:00 at +02:00 == 12:00 UTC -> equal time, first saved wins
    status, body = ingest(
        client, [one("rx-1", OTHER_POSITION)], "2026-10-05T14:00:00+02:00"
    )
    assert status == 200
    (snapshot,) = body["snapshots"]
    assert snapshot["position"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_same_mmsi_from_different_sources_is_tracked_separately(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    ingest(client, [one("rx-2", OTHER_POSITION)], T2)
    snapshots = client.get("/vessels", params={"mmsi": MMSI}).json()
    assert len(snapshots) == 2
    by_source = {s["source_id"]: s for s in snapshots}
    assert by_source["rx-1"]["position"]["data"]["longitude"] == pytest.approx(-73.9835)
    assert by_source["rx-2"]["position"]["data"]["longitude"] == pytest.approx(-73.980087)


def test_get_vessels_filters_by_source_and_mmsi(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    ingest(client, [one("rx-2", AMBIG_F1A), one("rx-2", AMBIG_F2)], T2)  # 366000001

    all_snapshots = client.get("/vessels").json()
    assert {(s["source_id"], s["mmsi"]) for s in all_snapshots} == {
        ("rx-1", MMSI),
        ("rx-2", 366000001),
    }

    by_source = client.get("/vessels", params={"source_id": "rx-1"}).json()
    assert [(s["source_id"], s["mmsi"]) for s in by_source] == [("rx-1", MMSI)]

    by_both = client.get(
        "/vessels", params={"source_id": "rx-2", "mmsi": 366000001}
    ).json()
    assert len(by_both) == 1
    assert by_both[0]["static"]["data"]["name"] == "NORTHERN STAR"

    assert client.get("/vessels", params={"source_id": "rx-2", "mmsi": MMSI}).json() == []


def test_decode_endpoint_is_untouched_by_ingest(client):
    ingest(client, [one("rx-1", SINGLE_FRAGMENT)], T2)
    response = client.post("/decode", json={"sentences": [one("rx-1", SINGLE_FRAGMENT)]})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"summary", "results"}  # no snapshot fields leaked in
    assert body["results"][0]["status"] == "decoded"


def test_missing_observed_at_rejected_with_422(client):
    response = client.post("/ingest", json={"sentences": [one("rx-1", SINGLE_FRAGMENT)]})
    assert response.status_code == 422


def test_store_survives_reopen(tmp_path):
    """Simulates a container restart: a new store on the same file sees the data."""
    path = str(tmp_path / "persist.db")
    message = decode_payload(SINGLE_PAYLOAD, 0)
    store = SnapshotStore(path)
    observed = normalize_observed_at(datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))
    assert store.upsert_if_newer("rx-1", MMSI, "position", observed, message.model_dump())
    store.close()

    reopened = SnapshotStore(path)
    snapshot = reopened.get_snapshot("rx-1", MMSI)
    assert snapshot is not None
    assert snapshot.position is not None
    assert snapshot.position.data.mmsi == MMSI
    assert snapshot.position.data.longitude == pytest.approx(-73.9835)
    reopened.close()


def test_store_upsert_semantics(tmp_path):
    path = str(tmp_path / "store.db")
    store = SnapshotStore(path)
    static = decode_payload(MULTI_PAYLOAD, 2).model_dump()
    position = decode_payload(SINGLE_PAYLOAD, 0).model_dump()
    other_static = dict(static, name="OTHER NAME")

    assert store.upsert_if_newer("rx-1", 1, "static", "2026-10-05T12:00:00.000000Z", static)
    # equal timestamp -> first saved wins
    assert not store.upsert_if_newer("rx-1", 1, "static", "2026-10-05T12:00:00.000000Z", other_static)
    # older -> rejected
    assert not store.upsert_if_newer("rx-1", 1, "static", "2026-10-05T11:00:00.000000Z", other_static)
    # strictly newer -> accepted
    assert store.upsert_if_newer("rx-1", 1, "static", "2026-10-05T13:00:00.000000Z", other_static)
    # the other kind of the same vessel is an independent timeline
    assert store.upsert_if_newer("rx-1", 1, "position", "2026-10-05T10:00:00.000000Z", position)

    snapshot = store.get_snapshot("rx-1", 1)
    assert snapshot.static.data.name == "OTHER NAME"
    assert snapshot.static.observed_at.isoformat().startswith("2026-10-05T13:00:00")
    assert snapshot.position.data.mmsi == MMSI
    store.close()
