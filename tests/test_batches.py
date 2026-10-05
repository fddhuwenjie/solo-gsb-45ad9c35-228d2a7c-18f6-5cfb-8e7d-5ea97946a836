"""Tests for the persistent review-batch endpoints (SQLite-backed)."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.store import BatchStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a throwaway SQLite file per test."""
    store = BatchStore(str(tmp_path / "batches.db"))
    monkeypatch.setattr(main, "store", store)
    with TestClient(app) as test_client:
        yield test_client
    store.close()


def _batch_payload(**overrides):
    payload = {
        "batch_id": "batch-1",
        "manuscripts": [
            {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
            {"id": "m2", "topics": ["vision"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"nlp": 5, "ml": 4}, "capacity": 2, "conflicts": ["m2"]},
            {"id": "r2", "scores": {"nlp": 3, "vision": 5}, "capacity": 1, "conflicts": []},
            {"id": "r3", "scores": {"ml": 4, "vision": 2}, "capacity": 2, "conflicts": []},
        ],
    }
    payload.update(overrides)
    return payload


def _create_batch(client, **overrides):
    response = client.post("/batches", json=_batch_payload(**overrides))
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_create_batch_saves_assignment_and_matches_assign_endpoint(client):
    body = _create_batch(client)
    assert body["batch_id"] == "batch-1"
    assert body["version"] == 1
    assert body["confirmed"] == []
    assert body["assignments"] == [
        {"manuscript_id": "m1", "reviewer_id": "r1", "score": 9},
        {"manuscript_id": "m1", "reviewer_id": "r3", "score": 4},
        {"manuscript_id": "m2", "reviewer_id": "r2", "score": 5},
    ]
    assert body["loads"] == {"r1": 1, "r2": 1, "r3": 1}

    # The saved assignment is exactly what POST /assign computes.
    plain = _batch_payload()
    plain.pop("batch_id")
    assign_body = client.post("/assign", json=plain).json()
    assert body["assignments"] == assign_body["assignments"]
    assert body["total_score"] == assign_body["total_score"]


def test_get_batch_returns_input_assignment_confirmations_and_version(client):
    _create_batch(client)
    body = client.get("/batches/batch-1").json()
    assert body["version"] == 1
    assert body["manuscripts"] == _batch_payload()["manuscripts"]
    assert body["reviewers"] == _batch_payload()["reviewers"]
    assert len(body["assignments"]) == 3
    assert body["confirmed"] == []


def test_get_unknown_batch_returns_404(client):
    response = client.get("/batches/ghost")
    assert response.status_code == 404


def test_duplicate_batch_id_does_not_overwrite(client):
    _create_batch(client)
    other = _batch_payload(
        reviewers=[
            {"id": "r9", "scores": {"nlp": 7, "ml": 7, "vision": 7}, "capacity": 5, "conflicts": []},
        ]
    )
    response = client.post("/batches", json=other)
    assert response.status_code == 409

    body = client.get("/batches/batch-1").json()
    assert body["version"] == 1
    assert body["reviewers"] == _batch_payload()["reviewers"]
    assert {a["reviewer_id"] for a in body["assignments"]} == {"r1", "r2", "r3"}


def test_infeasible_batch_is_not_saved(client):
    payload = _batch_payload(
        manuscripts=[
            {"id": "m1", "topics": ["nlp"], "required_reviewers": 2},
            {"id": "m2", "topics": ["nlp"], "required_reviewers": 2},
        ],
        reviewers=[
            {"id": "r1", "scores": {"nlp": 5}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"nlp": 4}, "capacity": 1, "conflicts": []},
            {"id": "r3", "scores": {"nlp": 3}, "capacity": 1, "conflicts": ["m1", "m2"]},
        ],
    )
    response = client.post("/batches", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["proof"] == {
        "manuscripts": ["m1", "m2"],
        "reviewers": ["r1", "r2"],
        "demand": 4,
        "capacity": 3,
        "gap": 1,
    }
    assert client.get("/batches/batch-1").status_code == 404


def test_create_batch_invalid_payload_returns_422(client):
    payload = _batch_payload()
    payload["manuscripts"].append(
        {"id": "m1", "topics": ["t"], "required_reviewers": 1}
    )
    assert client.post("/batches", json=payload).status_code == 422


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------

def test_confirm_relations_bumps_version_and_persists(client):
    _create_batch(client)
    response = client.post(
        "/batches/batch-1/confirmations",
        json={
            "version": 1,
            "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]

    fetched = client.get("/batches/batch-1").json()
    assert fetched["version"] == 2
    assert fetched["confirmed"] == body["confirmed"]
    # The assignment itself is untouched by confirmations.
    assert fetched["assignments"] == body["assignments"]


def test_confirm_accumulates_across_versions(client):
    _create_batch(client)
    client.post(
        "/batches/batch-1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    response = client.post(
        "/batches/batch-1/confirmations",
        json={"version": 2, "relations": [{"manuscript_id": "m2", "reviewer_id": "r2"}]},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 3
    assert response.json()["confirmed"] == [
        {"manuscript_id": "m1", "reviewer_id": "r1"},
        {"manuscript_id": "m2", "reviewer_id": "r2"},
    ]


def test_confirm_with_stale_version_returns_conflict_and_current_version(client):
    _create_batch(client)
    client.post(
        "/batches/batch-1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    response = client.post(
        "/batches/batch-1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r3"}]},
    )
    assert response.status_code == 409
    assert response.json()["current_version"] == 2
    # The losing request changed nothing.
    fetched = client.get("/batches/batch-1").json()
    assert fetched["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]


def test_confirm_relation_outside_assignment_returns_422(client):
    _create_batch(client)
    response = client.post(
        "/batches/batch-1/confirmations",
        json={
            "version": 1,
            "relations": [{"manuscript_id": "m2", "reviewer_id": "r1"}],
        },
    )
    assert response.status_code == 422
    assert client.get("/batches/batch-1").json()["version"] == 1


def test_confirm_duplicate_relation_returns_422(client):
    _create_batch(client)
    response = client.post(
        "/batches/batch-1/confirmations",
        json={
            "version": 1,
            "relations": [
                {"manuscript_id": "m1", "reviewer_id": "r1"},
                {"manuscript_id": "m1", "reviewer_id": "r1"},
            ],
        },
    )
    assert response.status_code == 422


def test_confirm_unknown_batch_returns_404(client):
    response = client.post(
        "/batches/ghost/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Withdrawal and republishing
# ---------------------------------------------------------------------------

def test_withdrawal_publishes_reassignment_as_next_version(client):
    _create_batch(client)
    client.post(
        "/batches/batch-1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    response = client.post(
        "/batches/batch-1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "feasible"
    assert body["version"] == 3
    assert body["assignments"] == [
        {"manuscript_id": "m1", "reviewer_id": "r1", "score": 9},
        {"manuscript_id": "m1", "reviewer_id": "r3", "score": 4},
        {"manuscript_id": "m2", "reviewer_id": "r3", "score": 2},
    ]
    assert body["revoked"] == [{"manuscript_id": "m2", "reviewer_id": "r2"}]
    assert body["added"] == [{"manuscript_id": "m2", "reviewer_id": "r3"}]
    assert body["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]
    assert body["loads"] == {"r1": 1, "r2": 0, "r3": 2}

    fetched = client.get("/batches/batch-1").json()
    assert fetched["version"] == 3
    assert fetched["assignments"] == body["assignments"]
    assert fetched["confirmed"] == body["confirmed"]


def test_withdrawal_revokes_withdrawers_confirmations_and_keeps_others(client):
    _create_batch(client)
    client.post(
        "/batches/batch-1/confirmations",
        json={
            "version": 1,
            "relations": [
                {"manuscript_id": "m1", "reviewer_id": "r1"},
                {"manuscript_id": "m2", "reviewer_id": "r2"},
            ],
        },
    )
    response = client.post(
        "/batches/batch-1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "feasible"
    # The withdrawn reviewer's relation and its confirmation are revoked;
    # the other confirmation survives.
    assert body["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]
    assert {tuple(r.values()) for r in body["revoked"]} == {("m2", "r2")}


def test_withdrawal_infeasible_leaves_batch_confirmations_and_version(client):
    payload = _batch_payload()
    payload["reviewers"][2]["capacity"] = 1  # r3 cannot take m2 as well
    _create_batch(client, reviewers=payload["reviewers"])
    client.post(
        "/batches/batch-1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    before = client.get("/batches/batch-1").json()

    response = client.post(
        "/batches/batch-1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "infeasible"
    assert body["reason"]
    assert body["version"] == 2  # unchanged

    after = client.get("/batches/batch-1").json()
    assert after == before


def test_withdrawal_with_stale_version_returns_conflict(client):
    _create_batch(client)
    client.post(
        "/batches/batch-1/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "r2"},
    )
    response = client.post(
        "/batches/batch-1/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "r3"},
    )
    assert response.status_code == 409
    assert response.json()["current_version"] == 2


def test_withdrawal_unknown_reviewer_returns_422(client):
    _create_batch(client)
    response = client.post(
        "/batches/batch-1/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "ghost"},
    )
    assert response.status_code == 422
    assert client.get("/batches/batch-1").json()["version"] == 1


def test_withdrawal_unknown_batch_returns_404(client):
    response = client.post(
        "/batches/ghost/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 404


def test_second_withdrawal_uses_published_version_as_original(client):
    _create_batch(client)
    first = client.post(
        "/batches/batch-1/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "r2"},
    ).json()
    assert first["version"] == 2
    # Published version: m1←{r1, r3}, m2←{r3}. Withdrawing r1 re-solves over
    # those relations: (m1, r3) and (m2, r3) are retained, m1's free slot
    # goes back to r2.
    second = client.post(
        "/batches/batch-1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r1"},
    ).json()
    assert second["status"] == "feasible"
    assert second["version"] == 3
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in second["assignments"]] == [
        ("m1", "r2"),
        ("m1", "r3"),
        ("m2", "r3"),
    ]
    assert second["revoked"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]
    assert second["added"] == [{"manuscript_id": "m1", "reviewer_id": "r2"}]
    assert second["loads"] == {"r1": 0, "r2": 1, "r3": 2}


# ---------------------------------------------------------------------------
# Concurrency and persistence
# ---------------------------------------------------------------------------

def test_concurrent_confirmations_on_same_version_single_winner(client):
    _create_batch(client)
    relations = [
        [{"manuscript_id": "m1", "reviewer_id": "r1"}],
        [{"manuscript_id": "m1", "reviewer_id": "r3"}],
        [{"manuscript_id": "m2", "reviewer_id": "r2"}],
        [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    ]
    barrier = threading.Barrier(len(relations))
    statuses = []

    def worker(relation_list):
        barrier.wait()
        response = client.post(
            "/batches/batch-1/confirmations",
            json={"version": 1, "relations": relation_list},
        )
        statuses.append(response.status_code)

    threads = [threading.Thread(target=worker, args=(rels,)) for rels in relations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == len(relations) - 1
    assert client.get("/batches/batch-1").json()["version"] == 2


def test_store_compare_and_set_is_atomic(tmp_path):
    store = BatchStore(str(tmp_path / "race.db"))
    assert store.create("b", {"n": 0})
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(payload):
        barrier.wait()
        outcomes.append(store.update_if_version("b", 1, payload))

    threads = [
        threading.Thread(target=worker, args=({"n": 1},)),
        threading.Thread(target=worker, args=({"n": 2},)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome is not None for outcome in outcomes) == [False, True]
    assert store.get("b")["version"] == 2
    store.close()


def test_batch_survives_store_reopen(client, tmp_path, monkeypatch):
    _create_batch(client)
    client.post(
        "/batches/batch-1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    # Simulate a container restart: a fresh store over the same SQLite file.
    reopened = BatchStore(str(tmp_path / "batches.db"))
    monkeypatch.setattr(main, "store", reopened)
    try:
        body = client.get("/batches/batch-1").json()
        assert body["version"] == 2
        assert body["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]
        assert body["assignments"] == [
            {"manuscript_id": "m1", "reviewer_id": "r1", "score": 9},
            {"manuscript_id": "m1", "reviewer_id": "r3", "score": 4},
            {"manuscript_id": "m2", "reviewer_id": "r2", "score": 5},
        ]
        assert body["manuscripts"] == _batch_payload()["manuscripts"]
        assert body["reviewers"] == _batch_payload()["reviewers"]
    finally:
        reopened.close()


def test_batches_endpoints_exposed_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/batches" in paths
    assert "/batches/{batch_id}" in paths
    assert "/batches/{batch_id}/confirmations" in paths
    assert "/batches/{batch_id}/reassignments" in paths
