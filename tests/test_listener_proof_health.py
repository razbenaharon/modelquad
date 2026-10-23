# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline listener-proof health contracts. No provider commands or installed state required."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modelquad.service import api, db, listener, listener_proof
from modelquad.service.socket_identity import ListenerError


@pytest.fixture
def summary_client(tmp_path):
    path = tmp_path / "summary.db"
    db.connect_and_init(path).close()
    app = FastAPI()
    app.state.db_path = path
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


def test_proof_cache_does_not_cache_health_or_identity(summary_client, monkeypatch):
    from modelquad.jobs import handle

    calls = []
    monkeypatch.setattr(handle, "service_runner_verdict", lambda: calls.append(True) or (True, "not_in_job"))
    request = SimpleNamespace(app=summary_client.app, scope={"path": "/api/listener-proof"})
    first = api.get_health(request)
    assert api.get_health(request)["detached_runner"] == first["detached_runner"]
    assert len(calls) == 1
    request.scope["path"] = "/api/health"
    api.get_health(request)
    api.get_health(request)
    assert len(calls) == 3
    stamp, verdict = request.app.state.proof_runner_cache
    request.app.state.proof_runner_cache = (stamp - api._PROOF_RUNNER_TTL_SECONDS - 1, verdict)
    request.scope["path"] = "/api/listener-proof"
    api.get_health(request)
    assert len(calls) == 4

    secret = "synthetic-installation-secret-0123456789abcdef"
    key = listener_proof.derive_key(secret)
    request.app.state.listener_proof_key = key
    request.scope.update(client=("127.0.0.1", 49152), server=("127.0.0.1", 8765))
    request.headers = {}

    async def chunks():
        yield listener_proof.canonical({"v": 1, "nonce": "a" * 64})

    request.stream = chunks
    reply = asyncio.run(listener_proof.proof_response(request))
    health, _ = listener_proof.validate_proof(reply.body, key, "a" * 64,
                                             ["127.0.0.1", 49152, "127.0.0.1", 8765])
    listener.compatible_health(health, request.app.state.db_path)
    with pytest.raises(ListenerError, match="service_identity_mismatch"):
        listener.compatible_health(health, request.app.state.db_path.with_name("foreign.db"))
    request.app.state.db_path = request.app.state.db_path.with_name("changed.db")
    assert api.get_health(request)["db_identity"] != health["db_identity"]
    tampered = json.loads(reply.body)
    tampered["health"]["db_identity"] = "wrong"
    with pytest.raises(ListenerError, match="service_proof_invalid"):
        listener_proof.validate_proof(listener_proof.canonical(tampered), key, "a" * 64,
                                      ["127.0.0.1", 49152, "127.0.0.1", 8765])


@pytest.mark.parametrize("verdict", [(True, "not_in_job"), (False, "job_kill_on_close")])
@pytest.mark.parametrize("expired_cache", [False, True])
def test_proof_retries_exception_but_caches_measured_verdict(summary_client, monkeypatch,
                                                          verdict, expired_cache):
    from modelquad.jobs import handle

    calls = []

    def measure():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("measurement unavailable")
        return verdict

    monkeypatch.setattr(handle, "service_runner_verdict", measure)
    request = SimpleNamespace(app=summary_client.app, scope={"path": "/api/listener-proof"})
    previous = None
    if expired_cache:
        previous = (time.monotonic() - api._PROOF_RUNNER_TTL_SECONDS - 1, (True, "not_in_job"))
        request.app.state.proof_runner_cache = previous
    assert api.get_health(request)["detached_runner"] == {"ok": False, "reason": "containment_unknown"}
    assert getattr(request.app.state, "proof_runner_cache", None) == previous
    expected = {"ok": verdict[0], "reason": verdict[1]}
    assert api.get_health(request)["detached_runner"] == expected
    assert api.get_health(request)["detached_runner"] == expected
    assert len(calls) == 2


def test_proof_never_caches_a_returned_containment_unknown(summary_client, monkeypatch):
    from modelquad.jobs import handle

    calls = []

    def measure():
        calls.append(True)
        return (False, "containment_unknown") if len(calls) == 1 else (True, "not_in_job")

    monkeypatch.setattr(handle, "service_runner_verdict", measure)
    request = SimpleNamespace(app=summary_client.app, scope={"path": "/api/listener-proof"})
    assert api.get_health(request)["detached_runner"] == {"ok": False, "reason": "containment_unknown"}
    assert getattr(request.app.state, "proof_runner_cache", None) is None
    assert api.get_health(request)["detached_runner"] == {"ok": True, "reason": "not_in_job"}
    assert len(calls) == 2
