# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline service-client handoff and SEC-552 entry-point regressions.

The copied-health and replacement tests also exercise the base's public-health
path. Their expectations fail behaviorally on that base; no base run is claimed.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from modelquad import paths
from modelquad.jobs import handle, recovery, service_client as client
from modelquad.service import auth, db, history
from modelquad.service.api import state_identity_tag
from modelquad.service.app import create_app
from modelquad.statecontext import resolve_state_context

SECRET = "synthetic-installation-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("MODELQUAD_STATE_MODE", "dev")


def health(database):
    state = resolve_state_context()
    return {"product": "modelquad", "service_role": "control-plane",
            "instance_kind": state.mode.value, "state_root_id": state_identity_tag(state.state_root),
            "db_schema_version": db.SCHEMA_VERSION, "db_identity": state_identity_tag(database.resolve()),
            "detached_runner": {"ok": True}, "version": "0.2.0", "listener_auth_protocol": 1}


@contextmanager
def listener(payload, *, close_after_proof=False, launch_status=200, launch_payload=None):
    records = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def answer(self, value, close=False, status=200):
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            if 300 <= status < 400:
                self.send_header("Location", "/api/redirect-target")
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            records.append((self.path, dict(self.headers), self.client_address))
            self.answer(payload, close=close_after_proof)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            records.append((self.path, dict(self.headers), self.client_address))
            if self.path == "/api/listener-proof":
                value = {"v": 1, "nonce": body["nonce"], "boot": "a" * 32,
                         "endpoint": ["127.0.0.1", self.client_address[1], "127.0.0.1", self.server.server_port],
                         "health": payload}
                key = hmac.digest(SECRET.encode(), b"ModelQuad/listener-proof/v1/key", "sha256")
                raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
                value["mac"] = hmac.new(key, b"ModelQuad/listener-proof/v1\0" + raw, hashlib.sha256).hexdigest()
                self.answer(value, close=close_after_proof)
            else:
                value = launch_payload if launch_payload is not None else {
                    "launched": True, "job_id": self.path.split("/")[3]}
                self.answer(value, close=True, status=launch_status)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def point_at(monkeypatch, port):
    directory = paths.state_root()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "service.pid").write_text(json.dumps({"pid": 123, "argv": ["serve", "--port", str(port)]}), encoding="utf-8")
    reads = []
    monkeypatch.setattr(auth, "read_service_secret", lambda root: reads.append(root) or SECRET)
    return reads


def ownership_seam(monkeypatch, *, refuse=False):
    # On the evidence base this module does not exist and the original client
    # proceeds with copied health. Keep the entry-point regression behavioral.
    try:
        from modelquad.service import listener as transport
        from modelquad.service.socket_identity import ListenerError
    except ImportError:
        return
    monkeypatch.setattr(transport, "admitted_service_images", lambda port: frozenset({"admitted"}))
    def verify(*args, **kwargs):
        if refuse:
            raise ListenerError("service_owner_mismatch")
        return SimpleNamespace(recheck=lambda *a, **k: None, close=lambda: None)
    monkeypatch.setattr(transport, "verify_socket_owner", verify)


def test_sec552_copied_health_connect_releases_nothing(tmp_path, monkeypatch):
    """Base accepts copied fields and reads its secret; authenticated connect refuses."""
    database = tmp_path / "history.db"
    ownership_seam(monkeypatch, refuse=True)
    with listener(health(database)) as (port, records):
        reads = point_at(monkeypatch, port)
        connected = client.connect(database)
        assert isinstance(connected, client.Unavailable)
        assert reads == []
        assert not any("X-ModelQuad-Token" in headers for _, headers, _ in records)
        assert not any("/jobs/" in path for path, _, _ in records)


def test_sec552_connection_replacement_launch_releases_nothing(tmp_path, monkeypatch):
    """Base closes health, reconnects and sends a token/job; proof transport refuses."""
    database = tmp_path / "history.db"
    ownership_seam(monkeypatch)
    with listener(health(database), close_after_proof=True) as (port, records):
        point_at(monkeypatch, port)
        connected = client.connect(database)
        if isinstance(connected, client.ServiceClient):
            connected.launch(str(uuid.uuid4()), "job-abcdefgh.json")
        assert not any("X-ModelQuad-Token" in headers for _, headers, _ in records)
        assert not any("/jobs/" in path for path, _, _ in records)


def test_sec552_valid_connect_keeps_proof_socket_for_launch(tmp_path, monkeypatch, caplog):
    ownership_seam(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://foreign.invalid:1")
    monkeypatch.setenv("NO_PROXY", "")
    database = tmp_path / "history.db"
    with listener(health(database)) as (port, records):
        reads = point_at(monkeypatch, port)
        connected = client.connect(database)
        assert isinstance(connected, client.ServiceClient)
        assert reads == [paths.state_root()]
        job_id = str(uuid.uuid4())
        assert connected.launch(job_id, "job-abcdefgh.json") == ("launched", "launched")
        assert [r[0] for r in records] == ["/api/listener-proof", f"/api/jobs/{job_id}/launch"]
        assert "X-ModelQuad-Token" not in records[0][1]
        assert records[1][1]["X-ModelQuad-Token"] == SECRET
        assert records[0][2] == records[1][2]
        assert SECRET not in repr(connected) + caplog.text


@pytest.mark.parametrize("change,label", [
    ({"product": "foreign"}, "foreign_listener"),
    ({"service_role": "worker"}, "wrong_role"),
    ({"state_root_id": "foreign"}, "state_root"),
    ({"db_identity": "other-db"}, "service_identity_mismatch"),
    ({"db_schema_version": True}, "schema"),
    ({"db_schema_version": db.SCHEMA_VERSION + 1}, "schema"),
    ({"detached_runner": {"ok": False}}, "service_contained"),
    ({"detached_runner": {"ok": 1}}, "service_identity_mismatch"),
], ids=["foreign-product", "foreign-role", "foreign-store", "foreign-db",
        "bool-schema", "wrong-schema", "contained", "nonboolean-runner"])
def test_signed_incompatible_identity_still_refuses(tmp_path, monkeypatch, change, label):
    ownership_seam(monkeypatch)
    database = tmp_path / "history.db"
    with listener(health(database) | change) as (port, records):
        point_at(monkeypatch, port)
        assert client.connect(database) == client.Unavailable(label)
        assert len(records) == 1
        assert "X-ModelQuad-Token" not in records[0][1]


@pytest.mark.parametrize("status,payload,result", [
    (503, {"reason": "launch_unavailable"}, ("ambiguous", "launch_unavailable")),
    (500, {"reason": "runner_launch_failed"}, ("ambiguous", "runner_launch_failed")),
    (409, {"reason": "job_not_launchable"}, ("refused", "job_not_launchable")),
    (409, {"reason": "service_contained"}, ("refused", "service_contained")),
    (409, {"reason": "job_runner_disabled"}, ("refused", "job_runner_disabled")),
    (429, {"reason": "launch_rate_limited"}, ("refused", "launch_rate_limited")),
    (403, {"reason": SECRET}, ("refused", "refused_other")),
    (200, {"launched": False}, ("ambiguous", "bad_response")),
    (200, {"launched": True, "job_id": "wrong"}, ("ambiguous", "bad_response")),
], ids=["server-failure", "runner-failed", "not-launchable", "contained", "runner-disabled",
        "rate-limit", "hostile-prose", "bad-success", "wrong-job"])
def test_fixed_launch_results(status, payload, result, caplog):
    from modelquad.service.listener import Reply
    closed, calls = [], []
    transport = SimpleNamespace(request=lambda *a, **k: calls.append(a) or Reply(status, "application/json", json.dumps(payload).encode()),
                                close=lambda: closed.append(True))
    connected = client.ServiceClient(transport)
    assert connected.launch(str(uuid.uuid4()), "job-abcdefgh.json") == result
    assert len(calls) == 1 and closed == [True]
    assert SECRET not in str(result) + repr(connected) + caplog.text


@pytest.mark.parametrize("status,payload", [
    (302, {"reason": SECRET}),
    (200, {"padding": "x" * 5000}),
], ids=["redirect", "oversized"])
def test_post_send_reply_failure_is_ambiguous_without_retry(tmp_path, monkeypatch, status, payload, caplog):
    from modelquad.jobs.launch_answer import refusal_label

    ownership_seam(monkeypatch)
    database = tmp_path / "history.db"
    with listener(health(database), launch_status=status, launch_payload=payload) as (port, records):
        point_at(monkeypatch, port)
        connected = client.connect(database)
        assert isinstance(connected, client.ServiceClient)
        job_id = str(uuid.uuid4())
        result = connected.launch(job_id, "job-abcdefgh.json")
        assert result == ("ambiguous", "bad_response")
        assert refusal_label(*result) is None  # Keep the existing ownership CAS path.
        assert [record[0] for record in records] == ["/api/listener-proof", f"/api/jobs/{job_id}/launch"]
        assert connected._listener._connection.sock is None
        assert SECRET not in str(result) + repr(connected) + caplog.text


@pytest.mark.parametrize("sent", [False, True], ids=["before-send-refused", "after-send-ambiguous"])
@pytest.mark.parametrize("code", ["service_connection_changed", "service_redirect_refused",
                                  "service_proof_invalid", "response_too_large"],
                         ids=["connection", "redirect", "proof", "oversized"])
def test_failure_is_not_retried_and_ambiguity_survives(monkeypatch, sent, code):
    from modelquad.service.socket_identity import ListenerError
    calls = []
    def request(*a, **k):
        calls.append(a)
        raise ListenerError(code, sent=sent)
    transport = SimpleNamespace(request=request, close=lambda: None)
    assert client.ServiceClient(transport).launch(str(uuid.uuid4()), "job-abcdefgh.json") == (
        "ambiguous" if sent else "refused",
        "bad_response" if sent and code != "service_connection_changed" else code)
    assert len(calls) == 1


def test_health_publishes_actual_app_database_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "schedule_recovery", lambda *a, **kw: False)
    monkeypatch.setattr(history, "reconcile_stale_attempts", lambda *a, **kw: None)
    monkeypatch.setattr(handle, "service_runner_verdict", lambda: (True, "not_in_job"))
    identities = []
    for name in ("first.db", "second.db"):
        database = tmp_path / name
        app = create_app(db_path=database, poll_providers=False, write_secret=SECRET)
        with TestClient(app, base_url="http://127.0.0.1") as service:
            response = service.get("/api/health", headers={"X-ModelQuad-Token": SECRET})
            assert response.status_code == 200
            payload = response.json()
        assert payload["db_identity"] == state_identity_tag(database.resolve())
        assert name not in response.text
        assert str(database) not in response.text
        assert database.as_posix() not in response.text
        assert SECRET not in response.text
        identities.append(payload["db_identity"])
    assert identities[0] != identities[1]


@pytest.mark.parametrize("record", [None, {}, {"argv": []}, {"argv": ["serve", "--port", "invalid"]},
                                   {"argv": ["serve", "--port", "0"]}],
                         ids=["absent", "empty", "no-args", "invalid-port", "zero-port"])
def test_port_hint_falls_back_to_cli_default(record):
    from modelquad.cli import SERVICE_DEFAULT_PORT
    directory = paths.state_root()
    directory.mkdir(parents=True, exist_ok=True)
    if record is not None:
        (directory / "service.pid").write_text(json.dumps(record), encoding="utf-8")
    assert client._service_port() == SERVICE_DEFAULT_PORT
