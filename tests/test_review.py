"""Integration tests for the manual review of ambiguous batches (/reviews)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from tests.conftest import (
    AMBIG_F1A,
    AMBIG_F1B,
    AMBIG_F2,
    INCOMPLETE_F1,
    SINGLE_FRAGMENT,
    make_sentence,
)

T1 = "2026-10-05T11:00:00Z"
T2 = "2026-10-05T12:00:00Z"

MMSI = 366999999
NORTHERN = 366000001  # AMBIG_F1A + AMBIG_F2 -> "NORTHERN STAR"
SOUTHERN = 366000002  # AMBIG_F1B + AMBIG_F2 -> "SOUTHERN CROSS"

# Same MMSI as SINGLE_FRAGMENT but a different longitude.
OTHER_POSITION = make_sentence("AIVDM,1,1,,A,15Mwqgh01DreF9HGD@44gkg40000,0")

# A fragment-1 whose payload is one character short: combined with AMBIG_F2
# the assembled payload holds 418 bits, so this candidate fails to decode.
SHORT_F1B = make_sentence(f"AIVDM,2,1,,B,{AMBIG_F1B.split(',')[5][:-1]},0")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a fresh snapshot database per test."""
    monkeypatch.setenv("AIS_DB_PATH", str(tmp_path / "snapshots.db"))
    monkeypatch.setattr(main, "_store", None)
    yield TestClient(main.app)
    monkeypatch.setattr(main, "_store", None)


def one(source_id: str, sentence: str) -> dict:
    return {"source_id": source_id, "sentence": sentence}


def ambig_batch(source_id: str = "rx-2") -> list[dict]:
    return [one(source_id, AMBIG_F1A), one(source_id, AMBIG_F1B), one(source_id, AMBIG_F2)]


def create(client: TestClient, sentences: list[dict], observed_at: str = T2):
    return client.post(
        "/reviews", json={"observed_at": observed_at, "sentences": sentences}
    )


def resolve(client: TestClient, batch_id: str, selections: list[list[int]]):
    return client.post(f"/reviews/{batch_id}/resolve", json={"selections": selections})


# -- creation -----------------------------------------------------------------


def test_create_review_batch_returns_id_and_stores_candidates(client):
    response = create(client, [one("rx-2", SINGLE_FRAGMENT)] + ambig_batch())
    assert response.status_code == 201
    body = response.json()
    assert body["batch_id"]
    assert body["status"] == "pending"
    assert body["observed_at"].startswith("2026-10-05T12:00:00")
    assert body["resolved_at"] is None
    assert body["selections"] is None
    assert body["snapshots"] is None
    assert body["total_sentences"] == 4
    assert body["ambiguous_pools"] == 1
    assert body["summary"]["ambiguous"] == 1
    ambiguous = next(r for r in body["results"] if r["status"] == "ambiguous")
    assert len(ambiguous["candidates"]) == 2
    # creation must not touch the vessel snapshots
    assert client.get("/vessels").json() == []


def test_create_review_rejects_batch_without_ambiguity(client):
    response = create(client, [one("rx-1", SINGLE_FRAGMENT)])
    assert response.status_code == 422
    assert client.get("/reviews").json() == []


def test_create_review_validates_request_schema(client):
    response = client.post("/reviews", json={"sentences": ambig_batch()})
    assert response.status_code == 422  # missing observed_at
    assert client.get("/reviews").json() == []


# -- querying -----------------------------------------------------------------


def test_list_and_get_review_batches(client):
    batch_id = create(client, ambig_batch()).json()["batch_id"]

    listed = client.get("/reviews").json()
    assert [b["batch_id"] for b in listed] == [batch_id]
    assert listed[0]["status"] == "pending"
    assert listed[0]["total_sentences"] == 3
    assert listed[0]["ambiguous_pools"] == 1
    assert "results" not in listed[0]  # list view carries no heavy fields

    pending = client.get("/reviews", params={"status": "pending"}).json()
    assert [b["batch_id"] for b in pending] == [batch_id]
    assert client.get("/reviews", params={"status": "resolved"}).json() == []

    detail = client.get(f"/reviews/{batch_id}").json()
    assert detail["sentences"][0]["sentence"] == AMBIG_F1A
    candidates = detail["results"][0]["candidates"]
    names = {c["decoded"]["name"] for c in candidates}
    assert names == {"NORTHERN STAR", "SOUTHERN CROSS"}

    assert client.get("/reviews/does-not-exist").status_code == 404


# -- resolution ----------------------------------------------------------------


def test_resolve_applies_selection_and_unambiguous_decodes(client):
    batch_id = create(client, [one("rx-2", SINGLE_FRAGMENT)] + ambig_batch()).json()[
        "batch_id"
    ]

    response = resolve(client, batch_id, [[1, 3]])  # AMBIG_F1A + AMBIG_F2
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None
    assert body["selections"] == [[1, 3]]
    by_mmsi = {s["mmsi"]: s for s in body["snapshots"]}
    assert set(by_mmsi) == {MMSI, NORTHERN}
    # the unambiguous position report was written too
    assert by_mmsi[MMSI]["position"]["data"]["longitude"] == pytest.approx(-73.9835)
    assert by_mmsi[MMSI]["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    # the selected candidate became the vessel's static timeline
    assert by_mmsi[NORTHERN]["static"]["data"]["name"] == "NORTHERN STAR"
    assert by_mmsi[NORTHERN]["static"]["observed_at"].startswith("2026-10-05T12:00:00")

    # the losing candidate was never written
    assert client.get("/vessels", params={"mmsi": SOUTHERN}).json() == []
    # the resolution stays queryable
    detail = client.get(f"/reviews/{batch_id}").json()
    assert detail["status"] == "resolved"
    assert detail["selections"] == [[1, 3]]
    assert {s["mmsi"] for s in detail["snapshots"]} == {MMSI, NORTHERN}


def test_resolve_requires_exactly_one_selection_per_pool(client):
    # two ambiguous pools: the same orphan fragments from two sources
    batch_id = create(client, ambig_batch("rx-2") + ambig_batch("rx-3")).json()[
        "batch_id"
    ]
    response = resolve(client, batch_id, [[0, 2]])  # covers only the first pool
    assert response.status_code == 422
    assert client.get("/vessels").json() == []
    assert client.get(f"/reviews/{batch_id}").json()["status"] == "pending"

    # empty selections are rejected by the schema as well
    assert resolve(client, batch_id, []).status_code == 422
    assert client.get(f"/reviews/{batch_id}").json()["status"] == "pending"


def test_resolve_rejects_forged_candidate(client):
    batch_id = create(client, ambig_batch()).json()["batch_id"]
    response = resolve(client, batch_id, [[0, 1]])  # two fragment-1s: not listed
    assert response.status_code == 422
    assert client.get("/vessels").json() == []
    assert client.get(f"/reviews/{batch_id}").json()["status"] == "pending"


def test_resolve_rejects_candidate_that_failed_decoding(client):
    batch_id = create(
        client, [one("rx-2", AMBIG_F1A), one("rx-2", SHORT_F1B), one("rx-2", AMBIG_F2)]
    ).json()["batch_id"]
    detail = client.get(f"/reviews/{batch_id}").json()
    candidates = {
        tuple(c["sentence_indexes"]): c
        for c in detail["results"][0]["candidates"]
    }
    assert candidates[(0, 2)]["decoded"]["name"] == "NORTHERN STAR"
    assert candidates[(1, 2)]["decoded"] is None
    assert candidates[(1, 2)]["error"] is not None

    response = resolve(client, batch_id, [[1, 2]])
    assert response.status_code == 422
    assert client.get("/vessels").json() == []
    assert client.get(f"/reviews/{batch_id}").json()["status"] == "pending"

    # the batch is still resolvable with the decodable candidate
    response = resolve(client, batch_id, [[0, 2]])
    assert response.status_code == 200
    assert client.get("/vessels", params={"mmsi": NORTHERN}).json() != []


def test_resolve_twice_is_rejected(client):
    batch_id = create(client, ambig_batch()).json()["batch_id"]
    assert resolve(client, batch_id, [[0, 2]]).status_code == 200
    again = resolve(client, batch_id, [[1, 2]])
    assert again.status_code == 409
    # the first resolution stands; the second one wrote nothing
    vessels = client.get("/vessels", params={"source_id": "rx-2"}).json()
    assert [v["mmsi"] for v in vessels] == [NORTHERN]


def test_resolve_unknown_batch_is_404(client):
    assert resolve(client, "does-not-exist", [[0, 2]]).status_code == 404


def test_resolve_does_not_roll_back_newer_snapshot(client):
    batch_id = create(
        client, [one("rx-2", SINGLE_FRAGMENT)] + ambig_batch(), observed_at=T1
    ).json()["batch_id"]
    # a newer observation for the same vessel arrives before the resolution
    client.post(
        "/ingest", json={"observed_at": T2, "sentences": [one("rx-2", OTHER_POSITION)]}
    )

    response = resolve(client, batch_id, [[1, 3]])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    by_mmsi = {s["mmsi"]: s for s in body["snapshots"]}
    # the newer position is kept; the batch's older position did not roll it back
    assert by_mmsi[MMSI]["position"]["observed_at"].startswith("2026-10-05T12:00:00")
    assert by_mmsi[MMSI]["position"]["data"]["longitude"] == pytest.approx(-73.980087)
    # the selected static message had no newer competitor and was stored
    assert by_mmsi[NORTHERN]["static"]["observed_at"].startswith("2026-10-05T11:00:00")


def test_resolve_same_observed_at_first_sentence_index_wins(client):
    batch_id = create(
        client,
        [
            one("rx-2", SINGLE_FRAGMENT),  # index 0: position for MMSI
            one("rx-2", OTHER_POSITION),   # index 1: same vessel, same class
            one("rx-2", AMBIG_F1A),
            one("rx-2", AMBIG_F1B),
            one("rx-2", AMBIG_F2),
        ],
    ).json()["batch_id"]
    response = resolve(client, batch_id, [[2, 4]])
    assert response.status_code == 200
    by_mmsi = {s["mmsi"]: s for s in response.json()["snapshots"]}
    assert by_mmsi[MMSI]["position"]["data"]["longitude"] == pytest.approx(-73.9835)


def test_resolve_ignores_error_and_incomplete_results(client):
    bad = SINGLE_FRAGMENT.replace("15Mwqgh", "15Mwqgi")  # checksum mismatch
    batch_id = create(
        client,
        [
            one("rx-2", bad),            # error result
            one("rx-3", INCOMPLETE_F1),  # incomplete keyed group
            one("rx-2", AMBIG_F1A),
            one("rx-2", AMBIG_F1B),
            one("rx-2", AMBIG_F2),
        ],
    ).json()["batch_id"]
    response = resolve(client, batch_id, [[2, 4]])
    assert response.status_code == 200
    vessels = client.get("/vessels").json()
    assert [(v["source_id"], v["mmsi"]) for v in vessels] == [("rx-2", NORTHERN)]


# -- persistence across restarts -----------------------------------------------


def test_review_batches_survive_restart(client, monkeypatch):
    pending_id = create(client, ambig_batch(), observed_at=T1).json()["batch_id"]
    resolved_id = create(client, ambig_batch(), observed_at=T2).json()["batch_id"]
    assert resolve(client, resolved_id, [[0, 2]]).status_code == 200

    # simulate a container restart: drop the cached store, reopen the same file
    monkeypatch.setattr(main, "_store", None)

    pending = client.get(f"/reviews/{pending_id}").json()
    assert pending["status"] == "pending"
    assert len(pending["results"][0]["candidates"]) == 2

    resolved = client.get(f"/reviews/{resolved_id}").json()
    assert resolved["status"] == "resolved"
    assert resolved["selections"] == [[0, 2]]
    assert resolved["snapshots"][0]["mmsi"] == NORTHERN

    listed = client.get("/reviews", params={"status": "pending"}).json()
    assert [b["batch_id"] for b in listed] == [pending_id]
    # the resolved batch cannot be resolved again after the restart
    assert resolve(client, resolved_id, [[1, 2]]).status_code == 409
