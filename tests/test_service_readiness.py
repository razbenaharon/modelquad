# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline managed readiness regressions. Every OS/transport seam is synthetic."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from modelquad.service import lifecycle, listener, listener_proof as proof, socket_identity as identity
from modelquad.service.socket_identity import ListenerError

PID = 2468
CREATED = 1791586800.0
SECRET = "synthetic-readiness-secret-0123456789abcdef"
ENDPOINT = ["127.0.0.1", 49152, "127.0.0.1", 8765]


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 <= seconds <= 0.1
        self.sleeps.append(seconds)
        self.now += seconds


class Child:
    pid = PID

    def __init__(self, *, unknown=False):
        self.exited, self.unknown = False, unknown
        self.calls = []

    def poll(self):
        return 1 if self.exited else None

    def kill(self):
        self.calls.append("kill")
        if not self.unknown:
            self.exited = True

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.unknown:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.exited = True
        return 1


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock, child = Clock(), Child()
    image = tmp_path / "python.exe"
    image.write_bytes(b"fixture only")
    image.chmod(0o700)
    command = [str(image), "serve", "--managed-child", "--port", "8765",
               "--db", str(tmp_path / "modelquad.db")]
    inside, launches, authentications, closed, stops = [], [], [], [], []
    contained = [None]
    background_launch = lifecycle._launch_background_child
    escape_requirements = []
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")

    @contextmanager
    def guard(_path):
        assert not inside
        inside.append(True)
        try:
            yield
        finally:
            inside.pop()

    def launch(popen, argv, kwargs, *, require_escape=False):
        assert inside
        escape_requirements.append(require_escape)
        launches.append((argv, kwargs))
        return child, contained[0]

    def authenticate(port, **kwargs):
        assert inside
        assert kwargs["require_runner"] is False
        authentications.append((port, kwargs, clock()))
        if "expected_pid" not in kwargs:
            raise ListenerError("service_absent")
        assert kwargs["expected_pid"] == PID
        assert kwargs["expected_created"] == CREATED
        assert kwargs["expected_image"] == str(image)
        return SimpleNamespace(health={"detached_runner": {"ok": False}}, close=lambda: closed.append(True))

    def terminate(pid, **kwargs):
        assert inside and pid == PID
        stops.append((pid, kwargs))
        child.exited = True
        return "terminated"

    monkeypatch.setattr(lifecycle, "_service_start_guard", guard)
    monkeypatch.setattr(lifecycle, "_launch_background_child", launch)
    manager = lifecycle.ServiceProcessManager(
        state_dir=tmp_path, popen=lambda *a, **kw: child, pid_exists=lambda pid: not child.exited,
        process_image_path=lambda pid: str(image), process_created_at=lambda pid: CREATED,
        terminate_verified=terminate, authenticate=authenticate, clock=clock, sleep=clock.sleep,
        working_directory=lambda: str(tmp_path),
    )
    return SimpleNamespace(**locals())


def record(h, *, pid=PID, created=CREATED):
    value = {"pid": pid, "argv": h.command,
             "started_at": datetime.fromtimestamp(created, timezone.utc).isoformat()}
    h.manager.pid_path.write_text(json.dumps(value), encoding="utf-8")
    return value


@pytest.mark.parametrize("case", ["delayed_ready", "readiness_timeout", "child_exited", "cleanup_confirmed",
                                  "cleanup_unconfirmed", "record_replaced"],
                         ids=["delayed_ready", "readiness_timeout", "child_exited", "cleanup_confirmed",
                              "cleanup_unconfirmed", "record_replaced"])
def test_start_readiness(harness, case):
    h = harness
    original = h.manager._authenticate
    attempts = []

    def authenticate(port, **kwargs):
        assert h.inside
        if "expected_pid" not in kwargs:
            return original(port, **kwargs)
        attempts.append((h.clock(), kwargs["deadline"]))
        if case == "delayed_ready":
            if len(attempts) <= 3:
                raise ListenerError("service_unreachable")
            return original(port, **kwargs)
        if case == "readiness_timeout":
            h.clock.now = kwargs["deadline"]
            raise ListenerError("service_connection_changed")
        if case == "child_exited":
            h.child.exited = True
            raise ListenerError("service_absent")
        if case == "record_replaced":
            record(h, pid=9753, created=CREATED + 5)
        raise ListenerError("service_proof_invalid")

    h.manager._authenticate = authenticate
    if case == "cleanup_unconfirmed":
        h.child.unknown = True
        h.manager._terminate_verified = lambda *a, **kw: "unconfirmed"
    result = h.manager.start(command=h.command)
    assert h.inside == [] and len(h.launches) == 1
    assert all(deadline - start <= 1.000001 for start, deadline in attempts)
    assert result.ready is (case == "delayed_ready")
    assert result.success is (case == "delayed_ready")
    if case == "delayed_ready":
        assert len(attempts) == 4 and h.closed == [True]
        assert result.running and h.manager.pid_path.exists()
        assert result.cleanup_reason is None
    else:
        expected = {"readiness_timeout": "service_ready_timeout", "child_exited": "service_child_exited"}
        assert result.reason == expected.get(case, "service_proof_invalid")
        assert result.cleanup_reason == ("service_cleanup_unconfirmed" if case == "cleanup_unconfirmed"
                                         else "service_cleanup_confirmed")
        assert result.running is (case in {"cleanup_unconfirmed", "record_replaced"})
        assert h.manager.pid_path.exists() is (case in {"cleanup_unconfirmed", "record_replaced"})
        if case == "record_replaced":
            assert json.loads(h.manager.pid_path.read_text())["pid"] == 9753
            assert h.stops == [] and h.child.calls[0] == "kill"
            assert "service_record_changed" in result.message
        elif case != "cleanup_unconfirmed":
            assert len(h.stops) <= 1
        if case == "readiness_timeout":
            assert h.clock() == 115.0


@pytest.mark.parametrize("case", ["ready_existing", "identity_unknown", "stale_pid", "reused_pid", "corrupt_record",
                                  "older_auth_unsupported", "unmanaged_listener"],
                         ids=["ready_existing", "identity_unknown", "stale_pid", "reused_pid", "corrupt_record",
                              "older_auth_unsupported", "unmanaged_listener"])
def test_existing_service(harness, case):
    h = harness
    if case != "unmanaged_listener":
        before = record(h)
    if case == "identity_unknown":
        h.manager._process_created_at = lambda pid: None
    if case == "stale_pid":
        h.manager._pid_exists = lambda pid: False
    if case == "reused_pid":
        record(h, created=CREATED - 3600)
    if case == "corrupt_record":
        h.manager.pid_path.write_text("{broken", encoding="utf-8")
    if case in {"older_auth_unsupported", "unmanaged_listener"}:
        def authenticate(*a, **kw):
            assert h.inside
            if case == "older_auth_unsupported":
                raise ListenerError("service_auth_unsupported")
            return SimpleNamespace(close=lambda: h.closed.append(True))
        h.manager._authenticate = authenticate
    result = h.manager.start(command=h.command)
    assert h.stops == []
    if case in {"ready_existing", "stale_pid", "reused_pid"}:
        assert result.success and result.ready is True
        assert len(h.launches) == (0 if case == "ready_existing" else 1)
    else:
        assert not result.success and result.running and not h.launches
        assert result.reason == {"identity_unknown": "service_process_identity_unconfirmed",
                                 "corrupt_record": "service_pid_record_unavailable",
                                 "older_auth_unsupported": "service_auth_unsupported",
                                 "unmanaged_listener": "service_unmanaged"}[case]
        if case == "older_auth_unsupported":
            assert result.message == identity.RESTART_MESSAGE
            assert json.loads(h.manager.pid_path.read_text()) == before
        if case == "unmanaged_listener":
            assert not h.manager.pid_path.exists()


@pytest.mark.parametrize("case", ["first_ready", "first_timeout_cleaned", "first_cleanup_unknown"],
                         ids=["first_ready", "first_timeout_cleaned", "first_cleanup_unknown"])
def test_double_start(harness, monkeypatch, case):
    h = harness
    rendezvous, first_proof, second_waiting, release = Barrier(2), Event(), Event(), Event()
    lock, children, results, errors = Lock(), [], [], []
    child = h.child

    @contextmanager
    def guard(path):
        rendezvous.wait(timeout=5)
        acquired = lock.acquire(blocking=False)
        if not acquired:
            second_waiting.set()
            lock.acquire()
        try:
            h.inside.append(True)
            try:
                yield
            finally:
                h.inside.pop()
        finally:
            lock.release()

    def launch(popen, argv, kwargs):
        nonlocal child
        assert h.inside
        assert sum(c.poll() is None for c in children) == 0
        child = Child(unknown=case == "first_cleanup_unknown")
        children.append(child)
        return child, None

    attempts = []
    def authenticate(port, **kwargs):
        assert h.inside
        if "expected_pid" not in kwargs:
            raise ListenerError("service_absent")
        if not attempts:
            attempts.append(True)
            first_proof.set()
            assert release.wait(timeout=5)
            if case != "first_ready":
                h.clock.now = kwargs["deadline"] + 15
                raise ListenerError("service_absent")
        return SimpleNamespace(close=lambda: None)

    def terminate(*a, **kw):
        if case == "first_cleanup_unknown":
            return "unconfirmed"
        child.exited = True
        return "terminated"

    monkeypatch.setattr(lifecycle, "_service_start_guard", guard)
    monkeypatch.setattr(lifecycle, "_launch_background_child", launch)
    h.manager._authenticate, h.manager._terminate_verified = authenticate, terminate
    h.manager._pid_exists = lambda pid: child.poll() is None
    # Two controllers share the fake seams, but keep their own Popen result.
    other = lifecycle.ServiceProcessManager(
        state_dir=h.tmp_path, process_image_path=lambda pid: str(h.image),
        process_created_at=lambda pid: CREATED, pid_exists=lambda pid: child.poll() is None,
        authenticate=authenticate, terminate_verified=terminate, clock=h.clock, sleep=h.clock.sleep,
        working_directory=lambda: str(h.tmp_path),
    )
    def run(manager):
        try:
            results.append(manager.start(command=h.command))
        except Exception as exc:
            errors.append(type(exc).__name__)
    workers = [Thread(target=run, args=(m,)) for m in (h.manager, other)]
    for worker in workers:
        worker.start()
    try:
        assert first_proof.wait(timeout=5)
        assert second_waiting.wait(timeout=5)
        assert lock.locked()
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
    assert not any(worker.is_alive() for worker in workers) and errors == []
    assert sum(c.poll() is None for c in children) == 1
    assert len(children) == (2 if case == "first_timeout_cleaned" else 1)
    assert sum(r.success for r in results) == {"first_ready": 2, "first_timeout_cleaned": 1,
                                               "first_cleanup_unknown": 0}[case]
    if case == "first_cleanup_unknown":
        assert sorted(r.reason for r in results) == ["service_cleanup_unconfirmed", "service_ready_timeout"]
        assert h.manager.launch_barrier_path.exists()
    assert h.manager.pid_path.exists()


@pytest.mark.parametrize("case", ["access-denied", "query-failed", "unexpected-error"],
                         ids=["access-denied", "query-failed", "unexpected-error"])
def test_uncertain_process_inspection_retains_record_and_refuses_start_stop(harness, case):
    h = harness
    before = record(h)
    if case == "unexpected-error":
        def inspect(pid):
            raise OSError("hostile inspection body")
    else:
        inspect = lambda pid: None
    h.manager._pid_exists = inspect
    started = h.manager.start(command=h.command)
    stopped = h.manager.stop()
    observed = h.manager.status()
    for result in (started, stopped, observed):
        assert result.running and result.pid == PID
        assert result.reason == "service_process_identity_unconfirmed"
        assert result.ready is not True
        assert "hostile" not in result.message
    assert not started.success and not stopped.success
    assert json.loads(h.manager.pid_path.read_text()) == before
    assert h.launches == h.stops == h.authentications == [] and h.child.calls == []


@pytest.mark.parametrize("case,error,expected", [
    ("absent", 87, False), ("access-denied", 5, None), ("unknown-error", 0, None),
    ("query-failed", 0, None), ("exited", 0, False), ("live", 0, True),
], ids=["absent", "access-denied", "unknown-error", "query-failed", "exited", "live"])
def test_windows_process_presence_requires_proven_absence(harness, monkeypatch, case, error, expected):
    import ctypes
    from ctypes import wintypes

    calls = []
    last_error = [0]
    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *args):
            return self.call(*args)
    def open_process(*args):
        last_error[0] = error
        calls.append("open")
        return 123 if case in {"query-failed", "exited", "live"} else 0
    def exit_code(handle, output):
        assert handle == 123
        calls.append("query")
        if case == "query-failed":
            return False
        ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = 259 if case == "live" else 0
        return True
    kernel = SimpleNamespace(OpenProcess=Function(open_process), GetExitCodeProcess=Function(exit_code),
                             CloseHandle=Function(lambda handle: calls.append("close")))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error[0], raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: last_error.__setitem__(0, value), raising=False)
    assert lifecycle._process_exists(PID) is expected
    assert calls == (["open", "query", "close"] if case in {"query-failed", "exited", "live"} else ["open"])


@pytest.mark.parametrize("case", ["creation-unknown", "publication-denied", "pid-unknown"],
                         ids=["creation-unknown", "publication-denied", "pid-unknown"])
def test_unpublished_unknown_child_blocks_another_controller(harness, monkeypatch, case):
    h = harness
    h.child.unknown = True
    if case == "creation-unknown":
        h.manager._process_created_at = lambda pid: None
    elif case == "pid-unknown":
        h.child.pid = None
    else:
        def deny(*a, **kw):
            raise OSError("synthetic publication failure")
        monkeypatch.setattr(lifecycle, "atomic_write", deny)
    first = h.manager.start(command=h.command)
    assert not first.success and first.running and first.cleanup_reason == "service_cleanup_unconfirmed"
    assert first.reason == {"creation-unknown": "service_process_identity_unconfirmed",
                           "publication-denied": "path_unreadable", "pid-unknown": "invalid_path"}[case]
    assert not h.manager.pid_path.exists()
    assert h.manager.launch_barrier_path.read_bytes() == b"service_cleanup_unconfirmed\n"
    assert h.child.calls == ["kill", ("wait", lifecycle.SERVICE_STOP_WAIT_SECONDS)]
    # A new manager sees durable evidence, even when it cannot inspect a PID.
    other = lifecycle.ServiceProcessManager(state_dir=h.tmp_path,
        popen=lambda *a, **kw: pytest.fail("a second live child must never be spawned"),
        authenticate=lambda *a, **kw: pytest.fail("unknown fate must refuse before a listener probe"))
    second = other.start(command=h.command)
    assert not second.success and second.running and second.pid is None
    assert second.reason == "service_cleanup_unconfirmed"
    assert len(h.launches) == len(h.authentications) == 1
    assert other.status().reason == other.stop().reason == "service_cleanup_unconfirmed"
    assert other.launch_barrier_path.exists()


def test_launch_barrier_must_be_durable_before_spawning(harness, monkeypatch):
    h = harness
    def deny_fsync(fd):
        raise OSError("synthetic marker flush failure")
    monkeypatch.setattr(lifecycle.os, "fsync", deny_fsync)
    result = h.manager.start(command=h.command)
    assert not result.success and result.reason == "path_unreadable"
    assert h.launches == []
    assert h.manager.launch_barrier_path.exists()
    assert h.manager.start(command=h.command).reason == "service_cleanup_unconfirmed"


@pytest.mark.parametrize("containment", [None, "job_shared", "containment_unknown"],
                         ids=["detached", "contained", "containment_unknown"])
def test_readiness_containment(harness, containment):
    h = harness
    h.contained[0] = containment
    result = h.manager.start(command=h.command)
    assert result.success and result.ready and result.contained == containment
    assert all(not kwargs["require_runner"] for _, kwargs, _ in h.authentications)
    # Lifecycle never uses the retention-only listener for launch admission.
    assert h.closed == [True]


@pytest.mark.parametrize("case", ["hostile_exception", "hostile_reply", "token_canary"],
                         ids=["hostile_exception", "hostile_reply", "token_canary"])
def test_closed_errors_no_secret(harness, caplog, capsys, case):
    h = harness
    def authenticate(*a, **kw):
        if "expected_pid" not in kw:
            raise ListenerError("service_absent")
        if case == "hostile_exception":
            raise OSError(SECRET)
        raise ListenerError(SECRET)
    h.manager._authenticate = authenticate
    h.child.unknown = True
    h.manager._terminate_verified = lambda *a, **kw: "unconfirmed"
    result = h.manager.start(command=h.command)
    assert not result.success and result.reason == "service_proof_invalid"
    output = json.dumps(result.to_dict()) + h.manager.pid_path.read_text() + repr(h.launches)
    output += caplog.text + str(capsys.readouterr())
    assert SECRET not in output


def test_public_status_is_one_bounded_nonmutating_authentication(harness):
    h = harness
    before = record(h)
    calls = []
    h.manager._authenticate = lambda port, **kw: calls.append(kw) or SimpleNamespace(close=lambda: None)
    result = h.manager.status()
    assert result.running and result.ready and result.reason is None
    assert len(calls) == 1 and calls[0]["deadline"] == 103.0 and not calls[0]["require_runner"]
    assert h.launches == h.stops == h.inside == []
    assert json.loads(h.manager.pid_path.read_text()) == before
    assert result.to_dict()["ready"] is True


def test_doctor_refuses_authenticated_unrecorded_listener(harness, monkeypatch):
    from modelquad import doctor
    from modelquad.service import control_plane as shell
    h = harness
    calls, closed = [], []
    def authenticate(port, **kwargs):
        calls.append((port, kwargs))
        assert "expected_pid" not in kwargs and kwargs["require_runner"] is False
        return SimpleNamespace(close=lambda: closed.append(True))
    h.manager._authenticate = authenticate
    monkeypatch.setattr(lifecycle, "ServiceProcessManager", lambda: h.manager)
    monkeypatch.setattr(shell, "probe_control_plane", lambda *a: pytest.fail("unbound probe must not run"))
    check = doctor._control_plane_check()
    assert check.status is doctor.CheckStatus.MISSING
    assert lifecycle.SERVICE_DETAILS["service_unmanaged"] in check.detail
    assert "Running and verified" not in check.detail
    assert len(calls) == 1 and calls[0][1]["deadline"] == 103.0 and closed == [True]
    assert not h.manager.pid_path.exists() and not h.manager.launch_barrier_path.exists()
    assert h.launches == h.stops == h.inside == []


def test_dry_run_never_claims_ready_or_authenticates(harness):
    h = harness
    h.manager._authenticate = lambda *a, **kw: pytest.fail("dry run must not authenticate")
    result = h.manager.start(command=h.command, dry_run=True)
    assert result.ready is None and not h.launches and not h.manager.pid_path.exists()


@pytest.mark.parametrize("case", ["wrong_pid", "reused_pid", "wrong_image", "wrong_sid", "owner_unknown",
                                  "bad_mac", "replay", "redirect", "wrong_db", "oversized_proof"],
                         ids=["wrong_pid", "reused_pid", "wrong_image", "wrong_sid", "owner_unknown",
                              "bad_mac", "replay", "redirect", "wrong_db", "oversized_proof"])
def test_readiness_impostor(tmp_path, monkeypatch, case):
    from modelquad import statecontext
    from modelquad.service import api, db

    image, other = tmp_path / "python.exe", tmp_path / "other.exe"
    for candidate in (image, other):
        candidate.write_bytes(b"fixture image")
    facts = (int(CREATED * 10000000) + 116444736000000000, identity._normal_image(str(image)), "sid")
    inspected, reads, requests, closed = [], [], [], []
    class Sock:
        def getsockname(self):
            return tuple(ENDPOINT[:2])
        def getpeername(self):
            return tuple(ENDPOINT[2:])
        def settimeout(self, value):
            assert value > 0
        def shutdown(self, *_args):
            pass
        def close(self):
            closed.append("socket")
    sock = Sock()
    kernel = SimpleNamespace(OpenProcess=lambda *a: 99, CloseHandle=lambda handle: closed.append("owner"))
    monkeypatch.setattr(identity, "_windows", lambda: (kernel, None, None))
    monkeypatch.setattr(identity, "_owner_pid", lambda *a, **kw: PID + (case == "wrong_pid"))
    monkeypatch.setattr(listener.auth, "current_user_sid", lambda: "sid")
    def inspect(handle):
        inspected.append(handle)
        if case == "owner_unknown":
            raise OSError(SECRET)
        return (facts[0] + (10000000 if case == "reused_pid" else 0),
                identity._normal_image(str(other)) if case == "wrong_image" else facts[1],
                "foreign-sid" if case == "wrong_sid" else facts[2])
    monkeypatch.setattr(identity, "_facts", inspect)
    monkeypatch.setattr(listener, "admitted_service_images", lambda port: frozenset({facts[1], identity._normal_image(str(other))}))
    monkeypatch.setattr(listener.socket, "create_connection", lambda *a, **kw: sock)
    monkeypatch.setattr(listener.auth, "read_service_secret", lambda *a, **kw: reads.append(True) or SECRET)
    context = statecontext.StateContext(statecontext.StateMode.INSTALLED, statecontext.StateSource.ENV_OVERRIDE,
                                        tmp_path, str(image))
    monkeypatch.setattr(statecontext, "resolve_state_context", lambda: context)
    health = {"product": "modelquad", "service_role": "control-plane", "instance_kind": "installed",
              "state_root_id": api.state_identity_tag(tmp_path), "version": "test-version",
              "db_schema_version": db.SCHEMA_VERSION,
              "db_identity": api.state_identity_tag(tmp_path / "modelquad.db"),
              "detached_runner": {"ok": False}}
    if case == "wrong_db":
        health["db_identity"] = "other"
    class Connection:
        def __init__(self, *_a, **_kw):
            self.sock = None
        def request(self, method, path, body, headers):
            requests.append((method, path, body, headers))
        def getresponse(self):
            nonce = json.loads(requests[-1][2])["nonce"]
            value = proof.make_proof(proof.derive_key(SECRET), nonce, ENDPOINT, health)
            if case == "bad_mac":
                value["mac"] = "0" * 64
            if case == "replay":
                value["nonce"] = "f" * 64
            raw = b"x" * 65537 if case == "oversized_proof" else proof.canonical(value)
            return SimpleNamespace(status=302 if case == "redirect" else 200, read=lambda cap: raw[:cap])
        def close(self):
            self.sock = None
            closed.append("connection")
    monkeypatch.setattr(listener, "_BoundHTTPConnection", Connection)
    with pytest.raises(ListenerError) as caught:
        listener.authenticate(8765, db_path=tmp_path / "modelquad.db", require_runner=False,
                              expected_pid=PID, expected_created=CREATED, expected_image=str(image))
    expected = {"wrong_sid": "service_owner_mismatch", "owner_unknown": "service_owner_unavailable",
                "redirect": "service_redirect_refused"}
    assert caught.value.code == expected.get(case, "service_identity_mismatch" if case in
                                            {"wrong_pid", "reused_pid", "wrong_image", "wrong_db"}
                                            else "service_proof_invalid")
    if case in {"wrong_pid", "reused_pid", "wrong_image", "wrong_sid", "owner_unknown"}:
        assert reads == requests == []
    else:
        assert reads == [True] and len(requests) == 1
        assert "X-ModelQuad-Token" not in requests[0][3]
    assert "socket" in closed and "owner" in closed
    assert SECRET not in str(caught.value) + caught.value.detail


def test_expected_process_is_rechecked_after_signed_proof(tmp_path, monkeypatch):
    image = tmp_path / "python.exe"
    image.write_bytes(b"fixture")
    initial = (int(CREATED * 10000000) + 116444736000000000, identity._normal_image(str(image)), "sid")
    owner = identity.SocketOwner(99, PID, ENDPOINT, True, initial)
    monkeypatch.setattr(identity, "_facts", lambda handle: initial)
    owner.verify_expected_process(pid=PID, created=CREATED, image=str(image))
    monkeypatch.setattr(identity, "_facts", lambda handle: (initial[0] + 1, initial[1], initial[2]))
    with pytest.raises(ListenerError, match="service_connection_changed"):
        owner.verify_expected_process(pid=PID, created=CREATED, image=str(image))


def test_cleanup_keeps_same_process_replacement_options(harness):
    h = harness
    def authenticate(port, **kwargs):
        raise ListenerError("service_proof_invalid" if "expected_pid" in kwargs else "service_absent")
    replacement = []
    def terminate(pid, **kwargs):
        assert h.inside and pid == PID
        value = json.loads(h.manager.pid_path.read_text())
        value["argv"] = value["argv"] + ["--poll-interval", "0.5"]
        h.manager.pid_path.write_text(json.dumps(value), encoding="utf-8")
        replacement.append(value)
        h.child.exited = True
        return "terminated"
    h.manager._authenticate, h.manager._terminate_verified = authenticate, terminate
    result = h.manager.start(command=h.command)
    assert not result.success and result.reason == "service_proof_invalid"
    assert result.cleanup_reason == "service_cleanup_confirmed" and result.running
    assert json.loads(h.manager.pid_path.read_text()) == replacement[0]
    assert "service_record_changed" in result.message
