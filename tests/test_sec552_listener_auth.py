# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline v1 proof/ownership/connection tests. Native behavior remains unproven."""
import asyncio
import ctypes
import json
import socket
import struct
import sys
import time
from types import SimpleNamespace

import pytest

from modelquad.service import control_plane as shell
from modelquad.service import listener, listener_proof as proof, socket_identity as identity
from modelquad.service.socket_identity import ListenerError

SECRET = "synthetic-installation-secret-0123456789abcdef"
KEY = proof.derive_key(SECRET)
NONCE = "1" * 64
ENDPOINT = ["127.0.0.1", 49152, "127.0.0.1", 8765]
HEALTH = {"product": "modelquad", "service_role": "control-plane"}


@pytest.mark.parametrize("host", ["python.exe", "pythonw.exe"], ids=["console-host", "windowless-host"])
def test_installation_interpreter_images_admit_both_directions(tmp_path, monkeypatch, host):
    from modelquad import cli, executable_resolution

    images = [tmp_path / name for name in ("python.exe", "pythonw.exe")]
    for image in images:
        image.write_bytes(b"synthetic interpreter; never executed")
        image.chmod(0o700)
    foreign = tmp_path / "other-installation" / "python.exe"
    foreign.parent.mkdir()
    foreign.write_bytes(b"foreign interpreter; never executed")
    foreign.chmod(0o700)
    monkeypatch.setattr(identity.sys, "platform", "win32")
    monkeypatch.setattr(identity.sys, "executable", str(tmp_path / host))
    monkeypatch.setattr(identity.sys, "_base_executable", str(tmp_path / host), raising=False)
    resolver = executable_resolution.admit_executable
    inspected = []

    def admit(path, *, allow_batch):
        inspected.append((str(path), allow_batch))
        return resolver(path, allow_batch=allow_batch)

    monkeypatch.setattr(executable_resolution, "admit_executable", admit)
    admitted = identity.admitted_service_images(8765)
    assert admitted == frozenset(identity._normal_image(str(image)) for image in images)
    assert identity._normal_image(str(foreign)) not in admitted
    assert all(not batch for _, batch in inspected)
    assert {str(image) for image in images} <= {path for path, _ in inspected}


@pytest.mark.parametrize("host", ["python.exe", "pythonw.exe"], ids=["console-host", "windowless-host"])
@pytest.mark.parametrize("sibling", ["absent", "refused"], ids=["absent", "refused"])
def test_installation_interpreter_sibling_must_pass_admission(tmp_path, monkeypatch, host, sibling):
    from modelquad import cli

    image = tmp_path / host
    image.write_bytes(b"synthetic interpreter; never executed")
    image.chmod(0o700)
    other = image.with_name("pythonw.exe" if host == "python.exe" else "python.exe")
    if sibling == "refused":
        other.mkdir()  # An existing directory is not an admitted executable.
    monkeypatch.setattr(identity.sys, "platform", "win32")
    monkeypatch.setattr(identity.sys, "executable", str(image))
    monkeypatch.setattr(identity.sys, "_base_executable", str(image), raising=False)
    assert identity.admitted_service_images(8765) == frozenset({identity._normal_image(str(image))})


def response(**changes):
    return proof.make_proof(KEY, NONCE, ENDPOINT, HEALTH, boot="a" * 32) | changes


def test_valid_proof_has_exact_independent_hmac_vector():
    import hashlib
    import hmac
    value = response()
    mac = value.pop("mac")
    key = hmac.new(SECRET.encode("ascii"), b"ModelQuad/listener-proof/v1/key", hashlib.sha256).digest()
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    assert mac == hmac.new(key, b"ModelQuad/listener-proof/v1\0" + raw, hashlib.sha256).hexdigest()
    assert proof.validate_proof(proof.canonical(response()), KEY, NONCE, ENDPOINT) == (HEALTH, "a" * 32)


@pytest.mark.parametrize("changes", [
    {"mac": "0" * 64}, {"nonce": "2" * 64}, {"v": 2}, {"v": True},
    {"endpoint": ["127.0.0.1", 49153, "127.0.0.1", 8765]},
    {"endpoint": ["127.0.0.1", True, "127.0.0.1", 8765]},
    {"health": HEALTH | {"product": "modified"}}, {"boot": "A" * 32},
    {"mac": "A" * 64}, {"extra": "untrusted"},
], ids=["wrong-key", "wrong-nonce", "wrong-version", "bool-version", "relay-endpoint",
        "bool-port", "modified-health", "bad-boot", "uppercase-mac", "extra-field"])
def test_proof_rejects_modification(changes):
    with pytest.raises(ListenerError, match="service_proof_invalid"):
        proof.validate_proof(proof.canonical(response(**changes)), KEY, NONCE, ENDPOINT)


def test_replay_does_not_authenticate_a_new_nonce_or_socket():
    raw = proof.canonical(response())
    for nonce, endpoint in [("2" * 64, ENDPOINT), (NONCE, ENDPOINT[:1] + [49153] + ENDPOINT[2:])]:
        with pytest.raises(ListenerError):
            proof.validate_proof(raw, KEY, nonce, endpoint)


@pytest.mark.parametrize("raw", [
    b'{"v":1,"v":1}', b'{"v":NaN}', b'{"v":Infinity}', b'{"v":1e999}',
    b'[' * 17 + b'0' + b']' * 17, b'"\xff"', b'{broken', b'x' * 65537,
], ids=["duplicate-key", "nan", "infinity", "overflow", "deep", "bad-utf8", "malformed", "oversized"])
def test_bounded_strict_json(raw):
    with pytest.raises(ListenerError):
        proof.parse_json(raw, cap=proof.RESPONSE_CAP)


@pytest.mark.parametrize("value", [
    {"v": True, "nonce": NONCE}, {"v": 1, "nonce": NONCE.upper().replace("1", "A")},
    {"v": 1, "nonce": "0" * 63}, {"v": 1, "nonce": NONCE, "endpoint": ENDPOINT}, [],
], ids=["bool-version", "uppercase-nonce", "short-nonce", "body-endpoint", "non-object"])
def test_request_types_are_strict(value):
    with pytest.raises(ListenerError):
        proof.request_nonce(value)


class Sock:
    def __init__(self):
        self.local, self.remote = tuple(ENDPOINT[:2]), tuple(ENDPOINT[2:])
        self.closed = False
    def getsockname(self):
        return self.local
    def getpeername(self):
        return self.remote
    def close(self):
        self.closed = True
    def shutdown(self, *_a):
        self.closed = True
    def settimeout(self, _timeout):
        pass


@pytest.fixture
def wire(monkeypatch):
    sock = Sock()
    reads, requests, owners = [], [], []
    class Owner:
        changed = False
        closed = False
        def recheck(self, _sock, **_kw):
            if self.changed:
                raise ListenerError("service_connection_changed")
        def close(self):
            self.closed = True
    owner = Owner()
    class Connection:
        status = 200
        invalid = None
        closed_by_peer = False
        def __init__(self, *_a, **_kw):
            self.sock = None
        def request(self, method, path, body=b"", headers=None):
            requests.append((method, path, body, headers))
        def getresponse(self):
            nonce = json.loads(requests[-1][2])["nonce"]
            raw = proof.canonical(proof.make_proof(KEY, nonce, ENDPOINT, HEALTH, boot="a" * 32))
            if self.invalid is not None:
                raw = self.invalid
            if self.closed_by_peer:
                self.sock = None
            return SimpleNamespace(status=self.status, read=lambda cap: raw[:cap])
        def close(self):
            self.sock = None
    def verify(*a, **kw):
        owners.append(True)
        return owner
    monkeypatch.setattr(listener.socket, "create_connection", lambda *a, **kw: sock)
    monkeypatch.setattr(listener, "admitted_service_images", lambda port: frozenset({"image"}))
    monkeypatch.setattr(listener, "verify_socket_owner", verify)
    monkeypatch.setattr(listener.auth, "read_service_secret", lambda root: reads.append(True) or SECRET)
    monkeypatch.setattr(listener, "compatible_health", lambda *a, **kw: None)
    monkeypatch.setattr(listener, "_BoundHTTPConnection", Connection)
    return SimpleNamespace(sock=sock, owner=owner, reads=reads, requests=requests, owners=owners, connection=Connection)


@pytest.mark.parametrize("error,label", [
    (ConnectionRefusedError("private"), "service_absent"),
    (TimeoutError("private"), "service_unreachable"),
    (OSError("private"), "service_unreachable"),
], ids=["absent", "reserved-port-timeout", "transport-error"])
def test_connect_failure_precedes_ownership_secret_and_http(wire, monkeypatch, error, label):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(listener.socket, "create_connection", fail)
    # A socket does LISTEN here but does not answer; with no listener the OS
    # table turns a timeout into "absent" (test_connect_timeout_uses_the_os_listener_table).
    monkeypatch.setattr(listener, "loopback_port_listening", lambda port: True)
    with pytest.raises(ListenerError) as caught:
        listener.authenticate(8765)
    assert caught.value.code == label
    assert "private" not in str(caught.value)
    assert wire.owners == wire.reads == wire.requests == []


@pytest.mark.parametrize("code", ["service_owner_mismatch", "service_image_mismatch", "service_owner_unavailable", "service_owner_unsupported"],
                         ids=["sid", "image", "unknown", "platform"])
def test_owner_failure_precedes_secret_and_all_http(wire, monkeypatch, code):
    def refuse(*a, **kw):
        raise ListenerError(code)
    monkeypatch.setattr(listener, "verify_socket_owner", refuse)
    with pytest.raises(ListenerError, match=code):
        listener.authenticate(8765)
    assert wire.reads == wire.requests == []
    assert wire.sock.closed


@pytest.mark.parametrize("status,label", [(302, "service_redirect_refused"), (404, "service_auth_unsupported"),
                                         (405, "service_auth_unsupported"), (503, "service_secret_unavailable")],
                         ids=["redirect", "legacy-absent", "legacy-method", "missing-secret"])
def test_protocol_http_refusals_are_fixed(wire, status, label):
    wire.connection.status = status
    with pytest.raises(ListenerError, match=label):
        listener.authenticate(8765)
    assert len(wire.requests) == 1
    assert "X-ModelQuad-Token" not in wire.requests[0][3]
    assert wire.sock.closed and wire.owner.closed


def test_timeout_is_fixed_and_closes_all_state(wire, monkeypatch):
    def timeout(*a):
        raise TimeoutError(SECRET)
    monkeypatch.setattr(wire.connection, "getresponse", timeout)
    with pytest.raises(ListenerError) as caught:
        listener.authenticate(8765)
    assert SECRET not in str(caught.value) + caught.value.detail
    assert wire.sock.closed and wire.owner.closed


def test_closed_proof_connection_never_reconnects(wire):
    wire.connection.closed_by_peer = True
    with pytest.raises(ListenerError, match="service_connection_changed"):
        listener.authenticate(8765)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("change", ["socket", "endpoint", "owner"], ids=["library-closed", "endpoint-changed", "process-replaced"])
def test_retained_connection_rechecked_before_token(wire, change):
    connected = listener.authenticate(8765)
    if change == "socket":
        connected._connection.sock = None
    elif change == "endpoint":
        wire.sock.remote = ("127.0.0.1", 8766)
    else:
        wire.owner.changed = True
    with pytest.raises(ListenerError) as caught:
        connected.request("POST", "/api/jobs/job/launch", b'{}')
    assert not caught.value.sent
    assert len(wire.requests) == 1
    assert connected.health == {} and connected._secret == ""


def test_http_library_implicit_reconnect_is_disabled():
    connection = listener._BoundHTTPConnection("127.0.0.1", 8765)
    with pytest.raises(ListenerError, match="service_connection_changed"):
        connection.request("POST", "/api/route", headers={"X-ModelQuad-Token": SECRET})


def test_public_advertisement_does_not_authenticate_the_probe(wire, monkeypatch):
    wire.connection.invalid = proof.canonical({"health": HEALTH | {"listener_auth_protocol": 1}})
    verdict = shell.probe_control_plane(8765)
    assert verdict.state == shell.PROBE_MISMATCH and verdict.reason == "service_proof_invalid"


def test_each_replacement_socket_requires_fresh_nonce_owner_and_proof(wire, monkeypatch):
    first = listener.authenticate(8765)
    first.close()
    replacement = Sock()
    monkeypatch.setattr(listener.socket, "create_connection", lambda *args, **kwargs: replacement)
    def verify(*args, **kwargs):
        wire.owners.append(True)
        return SimpleNamespace(recheck=lambda *a, **kw: None, close=lambda: None)
    monkeypatch.setattr(listener, "verify_socket_owner", verify)
    second = listener.authenticate(8765)
    assert first._socket is not second._socket
    nonces = [json.loads(request[2])["nonce"] for request in wire.requests]
    assert len(wire.owners) == len(wire.reads) == len(nonces) == 2
    assert nonces[0] != nonces[1]
    assert all("X-ModelQuad-Token" not in request[3] for request in wire.requests)
    second.close()


@pytest.mark.parametrize("changed", ["exit", "pid-reuse", "endpoint"], ids=["process-exit", "creation-changed", "tuple-changed"])
def test_retained_process_identity_refuses_change(monkeypatch, changed):
    sock = Sock()
    facts = (12345, "image", "sid")
    owner = identity.SocketOwner(99, 42, ENDPOINT, True, facts)
    monkeypatch.setattr(identity, "_owner_pid", lambda *a, **kw: 42)
    def read(_handle):
        if changed == "exit":
            raise ListenerError("service_connection_changed")
        return (54321, "image", "sid") if changed == "pid-reuse" else facts
    monkeypatch.setattr(identity, "_facts", read)
    if changed == "endpoint":
        sock.remote = ("127.0.0.1", 8766)
    with pytest.raises(ListenerError, match="service_connection_changed"):
        owner.recheck(sock)


@pytest.mark.parametrize("rows,accepted", [(1, True), (0, False), (2, False)], ids=["unique-exact-tuple", "listening-only", "ambiguous"])
@pytest.mark.parametrize("server", [True, False], ids=["service-owner", "renderer-peer"])
def test_tcp_table_requires_exact_established_tuple(monkeypatch, rows, accepted, server):
    local_port, remote_port = (8765, 49152) if server else (49152, 8765)
    def row(state, port):
        return bytes(identity._TcpRow(state, struct.unpack("=I", socket.inet_aton("127.0.0.1"))[0],
                                     socket.htons(port), struct.unpack("=I", socket.inet_aton("127.0.0.1"))[0],
                                     socket.htons(remote_port), 42))
    entries = [row(2, local_port), row(5, local_port + 1)] + [row(5, local_port)] * rows
    raw = struct.pack("=I", len(entries)) + b"".join(entries)
    def table(buffer, size, *_args):
        size._obj.value = len(raw)
        if buffer is None:
            return 122
        ctypes.memmove(buffer, raw, len(raw))
        return 0
    monkeypatch.setattr(identity, "_windows", lambda: (None, None, SimpleNamespace(GetExtendedTcpTable=table)))
    if accepted:
        assert identity._owner_pid(ENDPOINT, server=server) == 42
    else:
        with pytest.raises(ListenerError, match="service_owner_unavailable"):
            identity._owner_pid(ENDPOINT, server=server)


@pytest.mark.parametrize("failure", ["token", "table", "image", "sid"], ids=["token-inaccessible", "table-inaccessible", "image-mismatch", "sid-mismatch"])
def test_native_inspection_refuses_and_releases_handle(monkeypatch, failure):
    closed = []
    monkeypatch.setattr(identity, "_windows", lambda: (SimpleNamespace(OpenProcess=lambda *a: 99, CloseHandle=closed.append), None, None))
    def pid(*a, **kw):
        if failure == "table":
            raise OSError("untrusted")
        return 42
    monkeypatch.setattr(identity, "_owner_pid", pid)
    def facts(handle):
        if failure == "token":
            raise OSError("untrusted")
        return (123, "other" if failure == "image" else "image", "other" if failure == "sid" else "sid")
    monkeypatch.setattr(identity, "_facts", facts)
    monkeypatch.setattr(listener.auth, "current_user_sid", lambda: "sid")
    expected = {"image": "service_image_mismatch", "sid": "service_owner_mismatch"}.get(failure, "service_owner_unavailable")
    with pytest.raises(ListenerError, match=expected):
        identity.verify_socket_owner(Sock(), frozenset({"image"}))
    assert closed == ([] if failure == "table" else [99])


def test_proof_uses_accepted_transport_not_forwarded_headers(monkeypatch):
    from modelquad.service import api
    monkeypatch.setattr(api, "get_health", lambda request: HEALTH)
    async def chunks():
        yield proof.canonical({"v": 1, "nonce": NONCE})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(listener_proof_key=KEY)),
                              headers={"x-forwarded-for": "foreign", "x-forwarded-port": "123"},
                              scope={"client": tuple(ENDPOINT[:2]), "server": tuple(ENDPOINT[2:])}, stream=chunks)
    reply = asyncio.run(proof.proof_response(request))
    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert proof.validate_proof(reply.body, KEY, NONCE, ENDPOINT)[0] == HEALTH


@pytest.mark.parametrize("raw", [b'{}', b'x' * 1025, b'[' * 17 + b'0' + b']' * 17], ids=["invalid", "oversized", "deep"])
def test_service_proof_route_bounds_request(monkeypatch, raw):
    async def chunks():
        yield raw
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(listener_proof_key=KEY)),
                              headers={}, scope={}, stream=chunks)
    reply = asyncio.run(proof.proof_response(request))
    assert reply.status_code == 400
    assert json.loads(reply.body)["code"] == "service_proof_invalid"


def test_proof_without_verified_secret_is_503():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(listener_proof_key=None)))
    reply = asyncio.run(proof.proof_response(request))
    assert reply.status_code == 503
    assert json.loads(reply.body)["code"] == "service_secret_unavailable"


def test_legacy_refusal_has_exact_recovery_message():
    assert ListenerError("service_auth_unsupported").detail == identity.RESTART_MESSAGE
    assert "Restart it from the installation that started it" in identity.RESTART_MESSAGE


def test_optional_managed_identity_precedes_secret_and_is_rechecked_after_proof(wire):
    order = []
    def verify_expected_process(**kwargs):
        order.append(("expected", len(wire.reads), len(wire.requests), kwargs))
    wire.owner.verify_expected_process = verify_expected_process
    connected = listener.authenticate(8765, expected_pid=42, expected_created=1000.0, expected_image="image")
    assert order == [
        ("expected", 0, 0, {"pid": 42, "created": 1000.0, "image": "image"}),
        ("expected", 1, 1, {"pid": 42, "created": 1000.0, "image": "image"}),
    ]
    assert len(wire.requests) == 1
    connected.close()


# Installed train 36 (2026-10-10): Windows resets a connect to a closed loopback
# port only after ~2 s of SYN retries, so the 0.75 s connect timed out and an
# absent service read as "unreachable"; start then refused forever.
@pytest.mark.parametrize("listening, code", [
    (False, "service_absent"), (True, "service_unreachable"), (None, "service_unreachable"),
], ids=["no-listener", "listener-present", "table-unavailable"])
def test_connect_timeout_uses_the_os_listener_table(monkeypatch, listening, code):
    def timeout(*args, **kwargs):
        raise TimeoutError("timed out")
    monkeypatch.setattr(listener.socket, "create_connection", timeout)
    monkeypatch.setattr(listener, "loopback_port_listening", lambda port: listening)
    with pytest.raises(ListenerError) as raised:
        listener.authenticate(8765, deadline=time.monotonic() + 3.0, require_runner=False)
    assert raised.value.code == code


@pytest.mark.skipif(sys.platform != "win32", reason="Windows loopback reset timing")
def test_native_closed_port_is_absent_within_the_deadline():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # bound, never listened: nothing accepts on this port now
    assert identity.loopback_port_listening(port) is False
    started = time.monotonic()
    with pytest.raises(ListenerError) as raised:
        listener.authenticate(port, deadline=started + 3.0, require_runner=False)
    assert raised.value.code == "service_absent"
    assert time.monotonic() - started < 3.0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows TCP table")
def test_native_listener_table_sees_a_listening_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        assert identity.loopback_port_listening(server.getsockname()[1]) is True
    finally:
        server.close()
