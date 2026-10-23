# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Window retention controls use the real store without returning content."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from orchestrator.execution.base import WorkerTaskStatus
from orchestrator.jobs.seam import JobOutput, JobRef, JobStatus
from orchestrator.jobs.store import FileOutputStore
from orchestrator.service import api, history
from orchestrator.service.app import create_app


@pytest.fixture
def surface(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    app = create_app(db_path=path, usage_directory=tmp_path / "usage", poll_providers=False,
                     write_secret="pending-test-secret")
    store = FileOutputStore(tmp_path / "pending")
    monkeypatch.setattr(api, "default_store", lambda: store)
    with TestClient(app, base_url="http://127.0.0.1",
                    headers={"X-ModelQuad-Token": "pending-test-secret"}) as client:
        yield client, path, store


def put(store, job_id="waiting-job", attempt_id=99999, *, age=timedelta()):
    now = datetime.now(timezone.utc) - age
    output = JobOutput(JobRef(job_id, attempt_id, None), JobStatus.FINISHED,
                       WorkerTaskStatus.COMPLETED, "private-claim", "private-answer", False,
                       ("private-file.py",), None, None, now, now)
    store.put(output, now)
    return now


def test_list_has_only_summary_fields_with_optional_attempt_labels(surface):
    client, path, store = surface
    ident = history.record_delegation_attempt(path, task_type="implementation", requested_provider="codex")
    put(store, attempt_id=ident)
    response = client.get("/api/pending-output")
    assert response.status_code == 200
    item = response.json()[0]
    assert set(item) == {"job_id", "job_status", "expires_at", "expiring_soon", "extended_until",
                         "max_expires_at", "task_label", "provider"}
    assert item["task_label"] == "implementation" and item["provider"] is None
    assert item["job_status"] == "finished" and item["extended_until"] is None
    assert not item["expiring_soon"]
    for private in ("private-answer", "private-claim", "private-file.py", "receipt", "output_text"):
        assert private not in response.text
    # Pending-output GET is protected by the existing shared token guard.
    client.headers.pop("X-ModelQuad-Token")
    assert client.get("/api/pending-output").status_code == 403
    client.headers["X-ModelQuad-Token"] = "pending-test-secret"
    # Optional labels still allow an absent history row for the desktop list.
    put(store, "no-attempt", age=timedelta(days=6, hours=1))
    item = client.get("/api/pending-output").json()[0]
    assert item["job_id"] == "no-attempt" and item["expiring_soon"]
    assert item["task_label"] is None and item["provider"] is None


def test_expired_results_are_not_listed(surface):
    client, _, store = surface
    put(store, age=timedelta(days=8))
    assert client.get("/api/pending-output").json() == []


def test_history_failure_keeps_waiting_results_visible(surface, monkeypatch):
    client, _, store = surface
    put(store)
    def unavailable(*args):
        raise OSError("private history path")
    monkeypatch.setattr(history, "read_job_attempt", unavailable)
    response = client.get("/api/pending-output")
    assert response.status_code == 200
    assert response.json()[0]["task_label"] is None
    assert response.json()[0]["provider"] is None


@pytest.mark.parametrize("token", [None, "wrong-token"])
@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/api/pending-output/waiting-job", None),
    ("POST", "/api/pending-output/waiting-job/extend", {"days": 7}),
    ("DELETE", "/api/pending-output", {"confirm": "delete-all"}),
])
def test_removed_mutations_are_refused_without_the_token(surface, token, method, path, body):
    client, _, store = surface
    put(store)
    client.headers.pop("X-ModelQuad-Token")
    if token:
        client.headers["X-ModelQuad-Token"] = token
    assert client.request(method, path, json=body).status_code == 403
    assert len(store.pending(datetime.now(timezone.utc))) == 1


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/pending-output", None),
])
def test_store_failure_is_503_without_private_error(surface, monkeypatch, method, path, body):
    client, _, _ = surface
    def unavailable():
        raise OSError("private-answer /private/path")
    monkeypatch.setattr(api, "default_store", unavailable)
    response = client.request(method, path, json=body)
    assert response.status_code == 503
    assert "private-answer" not in response.text and "/private/path" not in response.text


@pytest.mark.parametrize("operation,method,path,body", [
    ("pending", "GET", "/api/pending-output", None),
    ("collect", "GET", "/api/pending-output", None),
])
def test_operation_failure_is_503(surface, monkeypatch, operation, method, path, body):
    client, _, store = surface
    put(store)
    def unavailable(*args):
        raise OSError("private-answer /private/path")
    monkeypatch.setattr(store, operation, unavailable)
    response = client.request(method, path, json=body)
    assert response.status_code == 503
    assert "private-answer" not in response.text and "/private/path" not in response.text


def test_pending_provider_uses_recorded_auto_route_not_request(surface):
    client, path, store = surface
    ident = history.record_delegation_attempt(path, task_type="review", requested_provider="claude")
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE delegation_attempts SET routed_provider='codex' WHERE id=?", (ident,))
    put(store, attempt_id=ident)
    assert client.get("/api/pending-output").json()[0]["provider"] == "codex"


@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/api/pending-output/waiting-job", None),
    ("POST", "/api/pending-output/waiting-job/extend", {"days": 7}),
    ("DELETE", "/api/pending-output", {"confirm": "delete-all"}),
])
def test_the_service_offers_no_pending_output_mutation(surface, method, path, body):
    """Only the read a release client uses exists; nothing is deleted or kept longer."""

    client, _, store = surface
    put(store)
    assert client.request(method, path, json=body).status_code in (404, 405)
    assert len(store.pending(datetime.now(timezone.utc))) == 1
