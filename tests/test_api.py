"""Integration tests for the POST /decode endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import (
    AMBIG_F1A,
    AMBIG_F1B,
    AMBIG_F2,
    INCOMPLETE_F1,
    MULTI_FRAG1,
    MULTI_FRAG2,
    SINGLE_FRAGMENT,
    VDO_SENTINELS,
)

client = TestClient(app)


def post(sentences: list[dict]) -> tuple[int, dict]:
    response = client.post("/decode", json={"sentences": sentences})
    return response.status_code, response.json()


def one(source_id: str, sentence: str) -> dict:
    return {"source_id": source_id, "sentence": sentence}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_single_fragment_decodes():
    status, body = post([one("rx-1", SINGLE_FRAGMENT)])
    assert status == 200
    (result,) = body["results"]
    assert result["status"] == "decoded"
    assert result["message_type"] == 1
    decoded = result["decoded"]
    assert decoded["mmsi"] == 366999999
    assert decoded["longitude"] == pytest.approx(-73.9835)
    assert decoded["latitude"] == pytest.approx(40.7484)
    assert decoded["speed_over_ground"] == pytest.approx(8.4)
    assert decoded["course_over_ground"] == pytest.approx(121.5)
    assert body["summary"]["decoded"] == 1


def test_vdo_sentinels_decode_to_null():
    status, body = post([one("rx-1", VDO_SENTINELS)])
    assert status == 200
    decoded = body["results"][0]["decoded"]
    assert body["results"][0]["sentence_type"] == "VDO"
    assert decoded["speed_over_ground"] is None
    assert decoded["longitude"] is None
    assert decoded["course_over_ground"] is None
    assert decoded["true_heading"] is None


def test_multi_fragment_out_of_order_reassembly():
    status, body = post([
        one("rx-1", MULTI_FRAG2),  # fragment 2 arrives first
        one("rx-1", MULTI_FRAG1),
    ])
    assert status == 200
    (result,) = body["results"]
    assert result["status"] == "decoded"
    assert result["sequence_id"] == "4"
    assert result["sentence_indexes"] == [0, 1]
    decoded = result["decoded"]
    assert decoded["message_type"] == 5
    assert decoded["name"] == "MV CLAUDE"
    assert decoded["call_sign"] == "WDC1234"
    assert decoded["imo_number"] == 9074729
    assert decoded["ship_type"] == 70
    assert decoded["destination"] == "NEW YORK"


def test_ambiguous_orphan_pool_returns_409_with_candidates():
    status, body = post([
        one("rx-2", AMBIG_F1A),
        one("rx-2", AMBIG_F1B),
        one("rx-2", AMBIG_F2),
    ])
    assert status == 409
    (result,) = body["results"]
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    names = {c["decoded"]["name"] for c in result["candidates"]}
    assert names == {"NORTHERN STAR", "SOUTHERN CROSS"}
    # every candidate uses the shared fragment 2 exactly once
    for candidate in result["candidates"]:
        assert 2 in candidate["sentence_indexes"]
    assert body["summary"]["ambiguous"] == 1


def test_missing_fragment_reports_missing_numbers():
    status, body = post([one("rx-3", INCOMPLETE_F1)])
    assert status == 200
    (result,) = body["results"]
    assert result["status"] == "incomplete"
    assert result["missing_fragments"] == [2]
    assert result["received_fragments"] == [1]


def test_invalid_checksum_is_reported_per_sentence():
    status, body = post([
        one("rx-1", SINGLE_FRAGMENT.replace("15Mwqgh", "15Mwqgi")),
        one("rx-1", SINGLE_FRAGMENT),
    ])
    assert status == 200
    error_result, ok_result = body["results"]
    assert error_result["status"] == "error"
    assert error_result["error"] == "checksum_mismatch"
    assert ok_result["status"] == "decoded"
    assert body["summary"]["invalid_sentences"] == 1


def test_identical_retransmissions_are_deduplicated():
    status, body = post([one("rx-1", SINGLE_FRAGMENT), one("rx-1", SINGLE_FRAGMENT)])
    assert status == 200
    (result,) = body["results"]
    assert result["status"] == "decoded"
    assert result["sentence_indexes"] == [0, 1]


def test_conflicting_duplicate_fragment_in_keyed_group():
    # same sequence id and fragment number but different payloads
    other = MULTI_FRAG1.replace("55Mwqgh", "55Mwqgi")
    # fix checksum for the mutated sentence
    body_fields = other[1:other.index("*")]
    from tests.conftest import make_sentence
    other = make_sentence(body_fields)
    status, body = post([one("rx-1", MULTI_FRAG1), one("rx-1", other), one("rx-1", MULTI_FRAG2)])
    assert status == 200
    (result,) = body["results"]
    assert result["status"] == "error"
    assert result["error"] == "conflicting_fragments"


def test_wrong_bit_length_is_rejected():
    # valid checksum but payload one character short -> 162 bits for type 1
    from tests.conftest import make_sentence
    short = make_sentence("AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg4000,0")
    status, body = post([one("rx-1", short)])
    assert status == 200
    (result,) = body["results"]
    assert result["status"] == "error"
    assert result["error"] == "decode_error"
    assert "168" in result["detail"]


def test_empty_batch_rejected_with_422():
    response = client.post("/decode", json={"sentences": []})
    assert response.status_code == 422


def test_missing_fields_rejected_with_422():
    response = client.post("/decode", json={"sentences": [{"sentence": SINGLE_FRAGMENT}]})
    assert response.status_code == 422
