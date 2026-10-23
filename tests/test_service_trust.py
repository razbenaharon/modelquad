# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The loopback service serves its own machine and its own pages, and nothing else.

Binding to 127.0.0.1 decides who can reach the socket, not who a browser is
acting for. These pin the three boundaries `service/trust.py` draws: a Host
allowlist on every request (DNS rebinding), an Origin policy on
state-changing methods (cross-site requests), and the installation secret on
every state-changing request and on the raw-output activity detail (any other
local process, including another Windows account's). Every legitimate client
-- a same-origin page or a non-browser client, which carry the secret, and
the doctor probes, which only read -- keeps working.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, __version__ as fastapi_version
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from modelquad.service import trust
from modelquad.service.app import create_app
from modelquad.usage.codex_appserver import collect_codex_appserver_usage
from modelquad.service.trust import (
    LoopbackTrustMiddleware,
    host_authority,
    hostname_of,
    origin_authority,
    origin_host,
)

UNSAFE_ROUTES = [
    ("PUT", "/api/settings", {"job_runner_enabled": False}),
    ("POST", "/api/jobs/job-1/release-fate", {}),
]

SECRET = "test-installation-secret-0123456789abcdef"
TOKEN = {"X-ModelQuad-Token": SECRET}
FASTAPI_VERSION = tuple(int(part) for part in fastapi_version.split(".")[:2])


def _app(tmp_path, **kwargs):
    usage = tmp_path / "usage"
    usage.mkdir(exist_ok=True)
    kwargs.setdefault("write_secret", SECRET)
    return create_app(db_path=tmp_path / "modelquad.db", usage_directory=usage, poll_providers=False, **kwargs)


def _client(tmp_path, **kwargs) -> TestClient:
    # The way the service is reached for real: Host carries the port.
    return TestClient(_app(tmp_path, **kwargs), base_url="http://127.0.0.1:8765")


# --------------------------------------------------------------------------
# Host: the rebinding boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "127.0.0.1", "localhost:8765", "LOCALHOST", "[::1]:8765", "[::1]"])
def test_a_loopback_host_is_served(tmp_path, host):
    client = _client(tmp_path)
    with client:
        response = client.get("/api/health", headers={"Host": host, **TOKEN})
    assert response.status_code == 200
    assert "instance_kind" in response.json()


@pytest.mark.parametrize("host", ["attacker.example", "attacker.example:8765", "127.0.0.1.attacker.example", "localhost.attacker.example:8765", "10.0.0.5:8765", "[::2]:8765"])
def test_a_foreign_host_is_refused_before_any_route_runs(tmp_path, host):
    """DNS rebinding: the page's own name resolved to loopback still arrives
    with that name in Host, and must be refused for every route."""

    client = _client(tmp_path)
    with client:
        for path in ("/api/health", "/api/providers", "/desktop", "/", "/docs"):
            response = client.get(path, headers={"Host": host})
            assert response.status_code == 421, (path, host, response.status_code)
            assert "loopback" in response.json()["detail"]


def test_a_missing_host_header_is_refused():
    middleware = LoopbackTrustMiddleware(None)
    assert middleware.refusal(Headers({}), "GET")[0] == 421
    assert middleware.refusal(Headers({"host": ""}), "GET")[0] == 421
    assert middleware.refusal(Headers({"host": "["}), "GET")[0] == 421


def test_the_default_client_name_of_a_test_client_is_not_trusted(tmp_path):
    """A TestClient without base_url says Host: testserver -- which is exactly
    a name this service must not answer to. The suites pass base_url."""

    with TestClient(_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 421


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:8765", "127.0.0.1"),
        ("[::1]:8765", "::1"),
        ("[::1]", "::1"),
        ("LocalHost", "localhost"),
        ("", None),
        (None, None),
        ("[", None),
        (":8765", None),
        ("[::1]junk", None),
        ("[::1]:junk", None),
        ("127.0.0.1:x", None),
    ],
)
def test_hostname_of(value, expected):
    assert hostname_of(value) == expected


# --------------------------------------------------------------------------
# Origin: the cross-site boundary, state-changing methods only
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), UNSAFE_ROUTES)
def test_a_cross_site_origin_may_not_change_state(tmp_path, method, path, body):
    client = _client(tmp_path)
    with client:
        response = client.request(method, path, json=body, headers={"Origin": "http://attacker.example"})
        assert response.status_code == 403, (path, response.status_code)
        assert "Origin" in response.json()["detail"]
        # Belt and braces: the browser's own fetch-metadata says cross-site.
        response = client.request(method, path, json=body, headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403, (path, response.status_code)
        # A rebound origin fails the Host check first, and is refused either way.
        response = client.request(method, path, json=body, headers={"Origin": "http://attacker.example", "Host": "attacker.example"})
        assert response.status_code == 421


@pytest.mark.parametrize(("method", "path", "body"), UNSAFE_ROUTES)
def test_the_services_own_pages_with_the_token_may_change_state(tmp_path, method, path, body):
    """A same-origin page (Origin = loopback) carrying the
    installation secret, and a non-browser client carrying it, reach the
    route. What the route then answers is the route's business; the trust
    boundary said yes."""

    client = _client(tmp_path)
    with client:
        for headers in (
            {"Origin": "http://127.0.0.1:8765"},
            {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
            {"Origin": "http://localhost:8765", "Host": "localhost:8765"},
            {"Origin": "http://[::1]:8765", "Host": "[::1]:8765"},
            {},
        ):
            response = client.request(method, path, json=body, headers={**headers, **TOKEN})
            assert response.status_code not in (403, 421), (path, headers, response.status_code)
            assert "code" not in response.json() or not str(response.json()["code"]).startswith("service_token"), path


@pytest.mark.parametrize(("method", "path", "body"), UNSAFE_ROUTES)
def test_no_token_or_a_wrong_token_is_refused_on_every_unsafe_route(tmp_path, method, path, body):
    """Loopback is reachable by every local process, other
    Windows accounts included. Without this installation's secret a request
    changes nothing -- whether it looks like a browser page of ours or not."""

    client = _client(tmp_path)
    with client:
        for headers in ({}, {"Origin": "http://127.0.0.1:8765"}, {"Sec-Fetch-Site": "same-origin"}):
            missing = client.request(method, path, json=body, headers=headers)
            assert missing.status_code == 403, (path, headers, missing.status_code)
            assert missing.json()["code"] == trust.TOKEN_MISSING
            assert SECRET not in missing.text

            for wrong in ("wrong", SECRET[:-1], SECRET + "x", SECRET.upper(), " "):
                refused = client.request(method, path, json=body, headers={**headers, "X-ModelQuad-Token": wrong})
                assert refused.status_code == 403, (path, wrong, refused.status_code)
                assert refused.json()["code"] in (trust.TOKEN_INVALID, trust.TOKEN_MISSING), wrong
                assert SECRET not in refused.text

            wrong = client.request(method, path, json=body, headers={**headers, "X-ModelQuad-Token": "wrong"})
            assert wrong.json()["code"] == trust.TOKEN_INVALID


@pytest.mark.parametrize(("method", "path", "body"), UNSAFE_ROUTES)
def test_empty_headers_no_longer_reach_any_unsafe_route(tmp_path, method, path, body):
    """The inverse of what this suite pinned before: 'no Origin' used to
    mean 'a non-browser client, let it through'. It now means nothing."""

    client = _client(tmp_path)
    with client:
        response = client.request(method, path, json=body)
        assert response.status_code == 403
        assert response.json() == {
            "detail": response.json()["detail"],
            "code": trust.TOKEN_MISSING,
        }


@pytest.mark.parametrize(("method", "path", "body"), UNSAFE_ROUTES)
def test_the_token_never_admits_a_cross_site_page(tmp_path, method, path, body):
    client = _client(tmp_path)
    with client:
        for headers in (
            {"Origin": "http://attacker.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "http://127.0.0.1:5174"},
        ):
            response = client.request(method, path, json=body, headers={**headers, **TOKEN})
            assert response.status_code == 403, (path, headers)
            assert "code" not in response.json(), "the browser boundary refuses before the token is read"


def test_an_app_with_no_secret_refuses_every_change(tmp_path):
    """There is no 'no authentication' mode: create_app's default refuses."""

    client = _client(tmp_path, write_secret=None)
    with client:
        for method, path, body in UNSAFE_ROUTES:
            for headers in ({}, TOKEN, {"X-ModelQuad-Token": ""}):
                response = client.request(method, path, json=body, headers=headers)
                assert response.status_code == 403, (path, headers)
                assert response.json()["code"] == trust.TOKEN_UNAVAILABLE
        # Reads are refused too: default deny.
        read = client.get("/api/health", headers=TOKEN)
        assert read.status_code == 403
        assert read.json()["code"] == trust.TOKEN_UNAVAILABLE


def test_an_unverifiable_secret_refuses_every_change_and_says_why(tmp_path):
    from modelquad.service.auth import ServiceSecret

    problem = "The service secret file grants access to another account."
    client = _client(tmp_path, write_secret=ServiceSecret(problem=problem))
    with client:
        for method, path, body in UNSAFE_ROUTES:
            response = client.request(method, path, json=body, headers=TOKEN)
            assert response.status_code == 403
            # Fate routes expose only the closed code, never the diagnostic.
            detail = trust.TOKEN_UNAVAILABLE if trust.is_fate_route(path) else problem
            assert response.json() == {"detail": detail, "code": trust.TOKEN_UNAVAILABLE}


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_every_read_needs_the_token_too(tmp_path, method):
    """Default deny: reads carry the installation secret like changes do."""

    client = _client(tmp_path)
    with client:
        for path in ("/api/health", "/api/pending-output", "/api/jobs/job-1/fate",
                     "/api/anything-else", "/", "/docs", "/openapi.json"):
            response = client.request(method, path)
            assert response.status_code == 403, (method, path)
            if method != "HEAD":
                assert response.json()["code"] == trust.TOKEN_MISSING
            wrong = client.request(method, path, headers={"X-ModelQuad-Token": "wrong"})
            assert wrong.status_code == 403
        assert client.request(method, "/api/health", headers=TOKEN).status_code in (200, 405)


def test_requires_token_vocabulary():
    assert not trust.requires_token("POST", "/api/listener-proof")
    assert trust.requires_token("POST", "/api/listener-proof/")
    assert trust.requires_token("PUT", "/api/listener-proof")
    assert trust.requires_token("GET", "/api/listener-proof")
    assert trust.requires_token("delete", "/anything")
    for method in ("GET", "HEAD", "OPTIONS"):
        for path in ("/api/health", "/api/pending-output", "/api/jobs/x/fate", "/"):
            assert trust.requires_token(method, path), (method, path)
    assert trust.TOKEN_CODES == frozenset({"service_token_missing", "service_token_invalid", "service_token_unavailable"})


@pytest.mark.parametrize("headers,status", [
    ({"Host": "attacker.example"}, 421),
    ({"Origin": "http://127.0.0.1:9999"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
], ids=["proof-foreign-host", "proof-foreign-origin", "proof-cross-site"])
def test_sec552_proof_exemption_keeps_browser_boundary(tmp_path, headers, status):
    with _client(tmp_path) as client:
        response = client.post("/api/listener-proof", json={"v": 1, "nonce": "a" * 64}, headers=headers)
        assert response.status_code == status


def test_another_local_process_on_another_port_is_another_origin(tmp_path):
    """A loopback hostname is not enough: same-origin is host AND port."""

    client = _client(tmp_path)
    with client:
        # (The scheme is not part of the authority: Host carries no scheme, so
        # https://127.0.0.1:8765 against Host 127.0.0.1:8765 is the same authority.)
        for origin in ("http://127.0.0.1:5174", "http://localhost:8765", "http://127.0.0.1"):
            response = client.put("/api/settings", json={"job_runner_enabled": False},
                                  headers={"Origin": origin, **TOKEN})
            assert response.status_code == 403, origin
            assert "code" not in response.json(), origin


@pytest.mark.parametrize(
    ("origin", "expected"),
    [("http://127.0.0.1:8765", ("127.0.0.1", 8765)), ("http://127.0.0.1", ("127.0.0.1", 80)), ("https://[::1]", ("::1", 443)), ("http://x:notaport", None), ("null", None)],
)
def test_origin_authority(origin, expected):
    assert origin_authority(origin) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1:8765", ("127.0.0.1", 8765)), ("localhost", ("localhost", 80)), ("[::1]:8765", ("::1", 8765)), ("[::1]", ("::1", 80)), ("127.0.0.1:x", None), ("", None)],
)
def test_host_authority(host, expected):
    assert host_authority(host) == expected


def test_the_browser_boundary_applies_to_reads_too(tmp_path):
    client = _client(tmp_path)
    with client:
        for headers in ({"Origin": "http://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
            for method in ("GET", "OPTIONS"):
                response = client.request(method, "/api/health", headers={**headers, **TOKEN})
                assert response.status_code == 403, (method, headers)
                assert "code" not in response.json(), "the browser boundary refuses before the token is read"


def test_an_opaque_or_malformed_origin_may_not_change_state():
    middleware = LoopbackTrustMiddleware(None)
    for origin in ("null", "", "attacker.example", "ftp://127.0.0.1", "http://"):
        assert middleware.refusal(Headers({"host": "127.0.0.1:8765", "origin": origin}), "PUT")[0] == 403, origin
    # Same-origin passes the browser boundary; the bare middleware has no
    # secret, so it is then refused for that, and admitted only with one.
    assert middleware.refusal(Headers({"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}), "PUT")[2] == trust.TOKEN_UNAVAILABLE
    keyed = LoopbackTrustMiddleware(None, write_secret=SECRET)
    assert keyed.refusal(Headers({"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765", **TOKEN}), "PUT") is None
    assert origin_host("http://127.0.0.1:8765") == "127.0.0.1"
    assert origin_host("https://[::1]:8765") == "::1"
    assert origin_host("null") is None


@pytest.mark.parametrize("dev_flag", [None, "1"])
@pytest.mark.parametrize("origin", [
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:5174", "http://attacker.example",
])
def test_no_cors_origin_is_honoured_with_or_without_the_dev_flag(tmp_path, monkeypatch, dev_flag, origin):
    if dev_flag is None:
        monkeypatch.delenv("MODELQUAD_DEV", raising=False)
    else:
        monkeypatch.setenv("MODELQUAD_DEV", dev_flag)
    client = _client(tmp_path)
    with client:
        for metadata in ({}, {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}):
            response = client.put(
                "/api/settings",
                json={"job_runner_enabled": False},
                headers={"Origin": origin, **metadata, **TOKEN},
            )
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
        preflight = client.options(
            "/api/settings",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type, x-modelquad-token",
            },
        )
        assert preflight.status_code == 403
        assert not any(header.startswith("access-control-") for header in preflight.headers)
        read = client.get("/api/health", headers={"Origin": origin, **TOKEN})
        assert read.status_code == 403
        assert not any(header.startswith("access-control-") for header in read.headers)


def test_the_trust_boundary_is_outermost(tmp_path, monkeypatch):
    """Refused HTTP never reaches parent dispatch or a framework-added layer."""
    seen = []
    parent_call = FastAPI.__call__
    build_stack = FastAPI.build_middleware_stack

    async def recording_call(self, scope, receive, send):
        if scope["type"] == "http":
            seen.append("FastAPI.__call__")
        await parent_call(self, scope, receive, send)

    def recording_build(self):
        stack = build_stack(self)

        async def recording_stack(scope, receive, send):
            if scope["type"] == "http":
                seen.append("framework stack")
            await stack(scope, receive, send)

        return recording_stack

    monkeypatch.setattr(FastAPI, "__call__", recording_call)
    monkeypatch.setattr(FastAPI, "build_middleware_stack", recording_build)
    app = _app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/health", headers={"Host": "attacker.example:8765"})
        assert response.status_code == 421
        assert seen == []
        # Both recording wrappers are live: an admitted request reaches them.
        assert client.get("/api/health", headers=TOKEN).status_code == 200
        assert seen == ["FastAPI.__call__", "framework stack"]


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "attacker.example:8765"])
def test_http_trust_is_checked_exactly_once(tmp_path, monkeypatch, host):
    checks = []
    refusal = LoopbackTrustMiddleware.refusal

    def recording_refusal(self, headers, method, path="/"):
        checks.append((method, path))
        return refusal(self, headers, method, path)

    monkeypatch.setattr(LoopbackTrustMiddleware, "refusal", recording_refusal)
    with _client(tmp_path) as client:
        response = client.get("/api/health", headers={"Host": host, **TOKEN})
    assert response.status_code == (200 if host.startswith("127.") else 421)
    assert checks == [("GET", "/api/health")]


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
def test_app_non_http_scopes_pass_through_unchanged(tmp_path, monkeypatch, scope_type):
    import asyncio

    seen = []

    async def parent_call(self, scope, receive, send):
        seen.append((scope, receive, send))

    def forbidden_refusal(*args, **kwargs):
        pytest.fail("non-HTTP scope entered the HTTP trust check")

    monkeypatch.setattr(FastAPI, "__call__", parent_call)
    monkeypatch.setattr(LoopbackTrustMiddleware, "refusal", forbidden_refusal)
    app = _app(tmp_path)
    scope = {"type": scope_type}
    receive, send = object(), object()
    asyncio.run(app(scope, receive, send))
    assert len(seen) == 1
    assert seen[0][0] is scope
    assert seen[0][1] is receive
    assert seen[0][2] is send


def test_the_app_publishes_no_api_documentation(tmp_path):
    app = _app(tmp_path)
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path, headers=TOKEN).status_code == 404, path


def test_non_http_scopes_pass_through():
    import asyncio

    seen = {}

    async def inner(scope, receive, send):
        seen["scope"] = scope

    middleware = LoopbackTrustMiddleware(inner)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert seen["scope"]["type"] == "lifespan"


def test_vocabulary_is_closed():
    assert trust.UNSAFE_METHODS == frozenset({"POST", "PUT", "PATCH", "DELETE"})
    assert trust.LOOPBACK_HOSTS == frozenset({"127.0.0.1", "localhost", "::1"})


def test_default_usage_refresh_never_starts_a_subprocess(tmp_path, monkeypatch):
    import subprocess

    from modelquad.usage import refresh_all

    # Make the real collector see an installed CLI on every machine. Its
    # lookup default is bound before the autouse isolation runs.
    collector = collect_codex_appserver_usage
    monkeypatch.setattr(
        collector, "__kwdefaults__",
        {**collector.__kwdefaults__, "which": lambda _: "fake-provider.exe"},
    )

    attempts = []

    def forbidden_spawn(self, *args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("default usage refresh attempted to start a process")

    # The initializer covers Popen references bound before this test. Record
    # attempts because refresh_provider catches exceptions from collectors.
    monkeypatch.setattr(subprocess.Popen, "__init__", forbidden_spawn)
    results = refresh_all(tmp_path / "usage", force=True)

    assert attempts == []
    assert set(results) == {"codex", "claude"}
    for result in results.values():
        assert result.observed_at is None
        assert result.remaining_percentage is None
        assert result.freshness == "unknown"
