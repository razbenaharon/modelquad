# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Owned stop/publication regressions; synthetic except the Windows child test."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator import procutil
from orchestrator.service import lifecycle


PID = 2468
IMAGE = r"C:\Python\python.exe"
STARTED_AT = "2026-10-08T00:00:00+00:00"
CREATED = datetime.fromisoformat(STARTED_AT).timestamp()
REPLACEMENT_SUFFIX = " A newer service record was left in place."


@pytest.fixture
def windows_platform(monkeypatch):
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")
    monkeypatch.setattr("orchestrator.jobs.handle._launch_policy", lambda: (0, False, None))
    monkeypatch.setattr("orchestrator.jobs.handle._process_in_any_job", lambda handle: False)


def _write_record(path, *, pid=PID, started_at=STARTED_AT):
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"pid": pid, "started_at": started_at, "argv": [IMAGE, "-m", "service"]}
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _manager(state_dir, terminate, **kwargs):
    # Let the three behavioural regressions run against the base too: its
    # PID-only seam receives the same fake, without masking the actual defect.
    seam = (
        {"terminate_verified": terminate}
        if hasattr(procutil, "terminate_verified")
        else {"terminate_pid": lambda pid: terminate(pid) in ("terminated", "not_found")}
    )
    defaults = {
        "pid_exists": lambda pid: True,
        "process_image_path": lambda pid: IMAGE,
        "process_created_at": lambda pid: CREATED,
    }
    defaults.update(kwargs)
    return lifecycle.ServiceProcessManager(state_dir=state_dir, **seam, **defaults)


@pytest.mark.parametrize("case", ["held", "guard_error", "record_changed"], ids=["held", "guard_error", "record_changed"])
def test_stop_holds_start_guard(tmp_path, windows_platform, monkeypatch, case):
    inside = False
    events = []

    @contextmanager
    def guard(path):
        nonlocal inside
        assert path == manager.pid_path
        events.append("enter")
        if case == "guard_error":
            raise lifecycle._ServiceStartGuardError("Service start guard timed out.")
        inside = True
        if case == "record_changed":
            _write_record(manager.pid_path, pid=9753, started_at="2026-10-08T00:00:01+00:00")
        try:
            yield
        finally:
            events.append("exit")
            inside = False

    def terminate(pid, **kwargs):
        assert inside
        assert pid == (9753 if case == "record_changed" else PID)
        events.append("terminate")
        return "terminated"

    def exists(pid):
        assert inside
        events.append("exists")
        return True

    def image(pid):
        assert inside
        events.append("identity")
        return IMAGE

    manager = _manager(tmp_path, terminate, pid_exists=exists, process_image_path=image)
    original_record = _write_record(manager.pid_path)
    read_record = manager._read_pid_record

    def read():
        events.append("read_locked" if inside else "read_initial")
        return read_record()

    unlink = Path.unlink

    def remove(path, *args, **kwargs):
        if path == manager.pid_path:
            assert inside
            events.append("unlink")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(manager, "_read_pid_record", read)
    monkeypatch.setattr(lifecycle, "_service_start_guard", guard)
    monkeypatch.setattr(Path, "unlink", remove)
    result = manager.stop()
    if case == "guard_error":
        assert (result.success, result.running, result.pid, result.action) == (False, True, PID, "stop")
        assert result.message == "Service start guard timed out."
        assert json.loads(manager.pid_path.read_text(encoding="utf-8")) == original_record
        assert events == ["read_initial", "enter"]
    else:
        assert result.success and not result.running
        assert events == [
            "read_initial", "enter", "read_locked", "exists", "identity",
            "terminate", "read_locked", "unlink", "exit",
        ]
        assert not manager.pid_path.exists()
        before = list(events)
        empty_manager = _manager(tmp_path / "absent", terminate)
        empty = empty_manager.stop()
        assert (empty.success, empty.running, empty.action) == (True, False, "not_running")
        assert empty.message == "Service is not running."
        assert not empty_manager.state_dir.exists()
        manager.pid_path.write_text("{", encoding="utf-8")
        unreadable = manager.stop()
        assert not unreadable.success and unreadable.action == "stop"
        assert unreadable.message.startswith("Refused to stop a process: Unable to read the service pidfile:")
        assert events == before + ["read_initial"]


@pytest.mark.parametrize(
    "stage", ["terminate", "stale_pid", "identity_mismatch"],
    ids=["terminate", "stale_pid", "identity_mismatch"],
)
def test_stop_keeps_replacement_record(tmp_path, windows_platform, stage):
    calls = []
    replacement = None

    def replace():
        nonlocal replacement
        # Never re-enter start while stop holds the guard.
        replacement = _write_record(
            manager.pid_path, pid=9753, started_at="2026-10-08T00:01:00+00:00",
        )

    def terminate(pid, **kwargs):
        calls.append(pid)
        replace()
        return "terminated"

    def exists(pid):
        if stage == "stale_pid":
            replace()
            return False
        return True

    def image(pid):
        if stage == "identity_mismatch":
            replace()
            return r"C:\Other\other.exe"
        return IMAGE

    manager = _manager(tmp_path, terminate, pid_exists=exists, process_image_path=image)
    _write_record(manager.pid_path)
    result = manager.stop()
    assert result.success and not result.running
    assert json.loads(manager.pid_path.read_text(encoding="utf-8")) == replacement
    assert result.message.endswith(REPLACEMENT_SUFFIX)
    assert calls == ([PID] if stage == "terminate" else [])


@pytest.mark.parametrize(
    "case",
    ["replace_denied", "write_error", "invalid_pid", "cleanup_unconfirmed", "replace_denied_then_ok"],
    ids=["replace_denied", "write_error", "invalid_pid", "cleanup_unconfirmed", "replace_denied_then_ok"],
)
def test_pid_publish_failure_cleans_exact_child(tmp_path, windows_platform, monkeypatch, case):
    calls = []
    publications = []
    sleeps = []
    launches = []
    proofs = []
    closed = []

    class Child:
        pid = None if case == "invalid_pid" else PID

        def poll(self):
            return None

        def kill(self):
            calls.append("kill")

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            if case == "cleanup_unconfirmed":
                raise subprocess.TimeoutExpired("synthetic child", timeout)
            return 1

    child = Child()

    def popen(*args, **kwargs):
        launches.append(args[0])
        return child

    def authenticate(port, **kwargs):
        from types import SimpleNamespace
        from orchestrator.service.socket_identity import ListenerError
        proofs.append(kwargs)
        assert port == 8765 and kwargs["require_runner"] is False
        if "expected_pid" not in kwargs:
            raise ListenerError("service_absent")
        assert kwargs["expected_pid"] == PID and kwargs["expected_created"] == CREATED
        assert kwargs["expected_image"] == IMAGE
        return SimpleNamespace(close=lambda: closed.append(True))

    manager = lifecycle.ServiceProcessManager(
        state_dir=tmp_path, popen=popen, pid_exists=lambda pid: True,
        process_image_path=lambda pid: IMAGE, process_created_at=lambda pid: CREATED,
        authenticate=authenticate, working_directory=lambda: str(tmp_path), sleep=sleeps.append,
    )
    write_text = Path.write_text
    error = PermissionError("replace denied") if case.startswith("replace_denied") else OSError("write error")

    def publish(path, content, *, private):
        assert path == manager.pid_path and private is False
        publications.append(content)
        if case == "replace_denied_then_ok" and len(publications) == 2:
            write_text(path, content, encoding="utf-8")
            return
        raise error

    def base_publication(path, *args, **kwargs):
        if path == manager.pid_path:
            raise error
        return write_text(path, *args, **kwargs)

    # The base uses write_text: forcing that failure shows its child leak,
    # rather than failing just because atomic_write was not imported yet.
    monkeypatch.setattr(lifecycle, "atomic_write", publish, raising=False)
    monkeypatch.setattr(Path, "write_text", base_publication)
    monkeypatch.setattr(lifecycle.time, "sleep", sleeps.append)
    result = manager.start(command=[IMAGE, "-m", "service"])
    if case == "replace_denied_then_ok":
        assert (result.success, result.running, result.pid, result.action) == (True, True, PID, "start")
        assert calls == []
        assert result.ready is True and closed == [True]
        assert len(proofs) == 2
        record = json.loads(manager.pid_path.read_text(encoding="utf-8"))
        assert record == {"argv": [IMAGE, "-m", "service"], "pid": PID, "started_at": record["started_at"]}
        assert publications == [json.dumps(record, indent=2, sort_keys=True)] * 2
        assert sleeps == [lifecycle.SERVICE_PID_PUBLISH_RETRY_SECONDS]
        assert not manager.launch_barrier_path.exists()
        return
    assert calls == ["kill", ("wait", getattr(lifecycle, "SERVICE_STOP_WAIT_SECONDS", 5.0))]
    unknown = case == "cleanup_unconfirmed"
    assert (result.success, result.running, result.pid, result.action) == (False, unknown, PID if unknown else None, "start")
    assert not manager.pid_path.exists()
    reason = "invalid_path" if case == "invalid_pid" else "path_unreadable"
    cleanup = "service_cleanup_unconfirmed" if unknown else "service_cleanup_confirmed"
    assert result.reason == reason and result.ready is False and result.cleanup_reason == cleanup
    assert result.message == lifecycle.SERVICE_DETAILS[reason] + " " + lifecycle.SERVICE_DETAILS[cleanup]
    assert len(proofs) == 1 and closed == []
    assert manager.launch_barrier_path.exists() is unknown
    if unknown:
        refused = manager.start(command=[IMAGE, "-m", "service"])
        assert not refused.success and refused.running and refused.reason == "service_cleanup_unconfirmed"
        assert len(launches) == 1 and len(proofs) == 1
    attempts = getattr(lifecycle, "SERVICE_PID_PUBLISH_ATTEMPTS", 10)
    assert len(publications) == (attempts if case == "replace_denied" else 0 if case == "invalid_pid" else 1)
    assert sleeps == ([lifecycle.SERVICE_PID_PUBLISH_RETRY_SECONDS] * (attempts - 1) if case == "replace_denied" else [])


@pytest.mark.parametrize(
    "outcome", ["terminated", "not_found", "mismatch", "unconfirmed"],
    ids=["terminated", "not_found", "mismatch", "unconfirmed"],
)
def test_stop_outcomes(tmp_path, windows_platform, outcome):
    calls = []
    existence_calls = []

    def terminate(pid, **kwargs):
        calls.append(pid)
        assert kwargs["expected_created"] == CREATED
        assert kwargs["tolerance"] == lifecycle.SERVICE_IDENTITY_TOLERANCE_SECONDS
        assert kwargs["wait_seconds"] == lifecycle.SERVICE_STOP_WAIT_SECONDS
        assert kwargs["image_matches"](r"c:\PYTHON\.\python.exe")
        assert not kwargs["image_matches"](r"C:\Other\other.exe")
        return outcome

    manager = lifecycle.ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: existence_calls.append(pid) or len(existence_calls) == 1,
        process_image_path=lambda pid: IMAGE, process_created_at=lambda pid: CREATED,
        terminate_verified=terminate,
        terminate_pid=lambda pid: pytest.fail("the compatibility terminator must remain unused"),
    )
    record = _write_record(manager.pid_path)
    result = manager.stop()
    assert calls == existence_calls == [PID]
    assert result.supported and not result.dry_run
    if outcome == "unconfirmed":
        assert (result.success, result.running, result.pid, result.action) == (False, True, PID, "stop")
        assert result.message == f"Could not stop the control-plane service: could not confirm it stopped (PID {PID})."
        assert json.loads(manager.pid_path.read_text(encoding="utf-8")) == record
    else:
        assert (result.success, result.running, result.pid) == (True, False, None)
        assert not manager.pid_path.exists()
        if outcome == "mismatch":
            assert result.action == "not_running"
            assert result.message == (
                f"Service is not running; its pidfile named PID {PID}, which now belongs to "
                "another process (its process identity changed before termination). "
                "Removed the stale pidfile and stopped nothing."
            )
        else:
            assert result.action == "stop"
            assert result.message == "Stopped the control-plane service."


class _Kernel32:
    """Every API checks the same handle; no real Windows process is touched."""

    handle = 0x123456789

    def __init__(self, case, set_error):
        self.case = case
        self.set_error = set_error
        self.calls = []

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("open", access, inherit, pid))
        if self.case.startswith("open_"):
            self.set_error({"open_missing": 87, "open_denied": 5, "open_other": 6}[self.case])
            return None
        return self.handle

    def WaitForSingleObject(self, handle, milliseconds):
        assert handle == self.handle
        self.calls.append(("wait", milliseconds))
        if len(self.calls) == 2:
            return 0 if self.case == "already_signaled" else 0xFFFFFFFF if self.case == "initial_wait_failed" else 0x102
        if self.case in ("wait_timeout", "terminate_failed"):
            return 0x102
        if self.case == "wait_failed":
            return 0xFFFFFFFF
        return 0

    def GetProcessTimes(self, handle, creation, exit_time, kernel, user):
        assert handle == self.handle
        self.calls.append(("created",))
        if self.case == "creation_unreadable":
            return False
        ticks = int((CREATED + 11_644_473_600) * 10_000_000)
        value = ctypes.cast(creation, ctypes.POINTER(wintypes.FILETIME)).contents
        value.dwLowDateTime, value.dwHighDateTime = ticks & 0xFFFFFFFF, ticks >> 32
        return True

    def QueryFullProcessImageNameW(self, handle, flags, buffer, size):
        assert handle == self.handle and flags == 0
        self.calls.append(("image",))
        if self.case == "image_unreadable":
            return False
        buffer.value = IMAGE
        ctypes.cast(size, ctypes.POINTER(wintypes.DWORD)).contents.value = len(IMAGE)
        return True

    def TerminateProcess(self, handle, code):
        assert handle == self.handle and code == 1
        self.calls.append(("terminate",))
        if self.case == "api_exception":
            raise OSError("synthetic API failure")
        return self.case not in ("terminate_failed", "exiting_on_terminate")

    def CloseHandle(self, handle):
        assert handle == self.handle
        self.calls.append(("close",))
        return self.case != "close_failed"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("open_missing", "not_found"), ("open_denied", "unconfirmed"), ("open_other", "unconfirmed"),
        ("already_signaled", "not_found"), ("initial_wait_failed", "unconfirmed"),
        ("creation_unreadable", "unconfirmed"), ("image_unreadable", "unconfirmed"),
        ("creation_mismatch", "mismatch"), ("image_mismatch", "mismatch"),
        ("terminated", "terminated"), ("wait_timeout", "unconfirmed"), ("wait_failed", "unconfirmed"),
        ("terminate_failed", "unconfirmed"), ("exiting_on_terminate", "terminated"),
        ("api_exception", "unconfirmed"), ("close_failed", "terminated"),
    ],
    ids=[
        "open_missing", "open_denied", "open_other", "already_signaled", "initial_wait_failed",
        "creation_unreadable", "image_unreadable", "creation_mismatch", "image_mismatch",
        "terminated", "wait_timeout", "wait_failed", "terminate_failed", "exiting_on_terminate",
        "api_exception", "close_failed",
    ],
)
def test_terminate_verified_api_outcomes(monkeypatch, case, expected):
    last_error = [123]
    error_writes = []

    def set_error(value):
        error_writes.append(value)
        last_error[0] = value

    monkeypatch.setattr(ctypes, "set_last_error", set_error, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error[0], raising=False)
    api = _Kernel32(case, set_error)
    monkeypatch.setattr(procutil, "_verified_termination_api", lambda: api)
    outcome = procutil.terminate_verified(
        PID, expected_created=CREATED + (3600 if case == "creation_mismatch" else 0),
        tolerance=5.0, image_matches=lambda image: image == IMAGE and case != "image_mismatch",
        wait_seconds=5.0,
    )
    assert outcome == expected
    assert error_writes[0] == 0
    assert api.calls[0] == ("open", 0x0001 | 0x1000 | 0x00100000, False, PID)
    assert sum(call[0] == "open" for call in api.calls) == 1
    if case.startswith("open_"):
        assert len(api.calls) == 1
    else:
        assert api.calls[-1] == ("close",)
        assert sum(call[0] == "close" for call in api.calls) == 1
    if case in ("terminated", "wait_timeout", "wait_failed", "close_failed"):
        assert api.calls == [
            ("open", 0x101001, False, PID), ("wait", 0), ("created",),
            ("image",), ("terminate",), ("wait", 5000), ("close",),
        ]
    elif case in ("terminate_failed", "exiting_on_terminate"):
        assert api.calls[-3:] == [("terminate",), ("wait", 0), ("close",)]
    elif case != "api_exception":
        assert ("terminate",) not in api.calls
    if case == "already_signaled":
        assert api.calls == [("open", 0x101001, False, PID), ("wait", 0), ("close",)]


@pytest.mark.skipif(os.name != "nt", reason="real Windows process handles")
@pytest.mark.parametrize("case", ["terminated", "mismatch"], ids=["terminated", "mismatch"])
def test_terminate_verified_windows(case):
    child = subprocess.Popen(
        [sys._base_executable, "-c", "import time; time.sleep(60)"],
        **procutil.no_window_kwargs(),
    )
    try:
        created = procutil.process_creation_epoch(child.pid)
        image = procutil.windows_process_image_path(child.pid)
        assert created is not None and image is not None
        assert lifecycle._same_image_path(image, sys._base_executable)
        outcome = procutil.terminate_verified(
            child.pid, expected_created=created + (3600 if case == "mismatch" else 0),
            tolerance=lifecycle.SERVICE_IDENTITY_TOLERANCE_SECONDS,
            image_matches=lambda actual: lifecycle._same_image_path(actual, image),
            wait_seconds=lifecycle.SERVICE_STOP_WAIT_SECONDS,
        )
        assert outcome == case
        if case == "terminated":
            assert child.wait(timeout=5) is not None
        else:
            assert child.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
