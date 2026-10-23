# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Stop requests stay attempt-scoped and preserve observed process/output evidence."""

import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from modelquad import procutil
from modelquad.config import Config
from modelquad.execution import CodexExecutor, ExecutionManager
from modelquad.execution.base import ExecutionRequest, ExecutionResult, WorkerTaskStatus, attempt_resolution, workspace_stability_note
from modelquad.models import TaskType
from modelquad.service import db, history


def opened(path):
    return history.record_delegation_attempt(path, task_type="implementation")


def stored(path):
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='attempt_stop_requests'").fetchone()
    return json.loads(row[0]) if row else []


def test_only_open_started_attempts_accept_stop_and_prune_resolved(tmp_path):
    path = tmp_path / "state.db"
    first, second = opened(path), opened(path)
    assert history.request_attempt_stop(path, first) == "accepted"
    assert history.attempt_stop_requested(path, first)
    history.resolve_delegation_attempt(path, first, status="completed")
    assert history.request_attempt_stop(path, first) == "not_running"
    assert stored(path) == [first]  # refusal writes nothing
    assert history.request_attempt_stop(path, 99999) == "not_found"
    assert history.request_attempt_stop(path, second) == "accepted"
    assert stored(path) == [second]
    refused = history.record_delegation_attempt(path, task_type="review", status="refused")
    assert history.request_attempt_stop(path, refused) == "not_running"


@pytest.mark.parametrize("bad", ["{broken", "{}", "null", '[true]', '["1"]'])
def test_corruption_is_unrequested_and_writer_replaces_it(tmp_path, bad):
    path = tmp_path / "state.db"
    attempt = opened(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('attempt_stop_requests',?)", (bad,))
    assert not history.attempt_stop_requested(path, attempt)
    assert history.request_attempt_stop(path, attempt) == "accepted"
    assert stored(path) == [attempt]
    assert history.get_launch_state(path)["routing_paused"] is False


def test_failed_read_is_read_only_and_never_raises(tmp_path, monkeypatch):
    missing = tmp_path / "missing.db"
    assert not history.attempt_stop_requested(missing, 1)
    assert history.request_attempt_stop(missing, 1) == "unavailable"
    assert not missing.exists()
    monkeypatch.setattr(history.sqlite3, "connect", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError()))
    assert not history.attempt_stop_requested(missing, 1)


def test_concurrent_requests_and_resolution_keep_consistent_list(tmp_path):
    path = tmp_path / "state.db"
    first, second, third = opened(path), opened(path), opened(path)
    barrier = threading.Barrier(2)

    def request(attempt):
        barrier.wait(timeout=5)
        return history.request_attempt_stop(path, attempt)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(request, item) for item in (first, second)]
        assert all(f.result(timeout=10) == "accepted" for f in futures)
    assert stored(path) == [first, second]
    barrier = threading.Barrier(2)

    def resolve():
        barrier.wait(timeout=5)
        history.resolve_delegation_attempt(path, third, status="completed", only_if_open=True)

    with ThreadPoolExecutor(2) as pool:
        stop, resolution = pool.submit(request, third), pool.submit(resolve)
        accepted = stop.result(timeout=10) == "accepted"
        resolution.result(timeout=10)
    assert stored(path) == [first, second] + ([third] if accepted else [])
    assert history.request_attempt_stop(path, third) == "not_running"
    assert history.request_attempt_stop(path, second) == "accepted"
    assert stored(path) == [first, second]


class PollProcess:
    pid = 42
    stdin = None
    returncode = None
    text_mode = True
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, clock, *, drain_timeout=False):
        self.clock = clock
        self.calls = []
        self.killed = False
        self.drain_timeout = drain_timeout

    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        if self.returncode == 0:
            return "completed", ""
        if self.killed:
            if self.drain_timeout:
                raise subprocess.TimeoutExpired("worker", timeout, output=b"partial plus drain", stderr=b"err")
            return "partial", ""
        self.clock[0] += timeout
        raise subprocess.TimeoutExpired("worker", timeout, output=b"partial", stderr=b"err")

    def poll(self):
        return self.returncode


def simulate(monkeypatch, callback, *, timeout=5, drain_timeout=False):
    clock = [0.0]
    proc = PollProcess(clock, drain_timeout=drain_timeout)
    kills = []
    monkeypatch.setattr(procutil.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "dead")
    monkeypatch.setattr(procutil, "_descendants_at_exit", lambda *a, **kw: {"enumerated": True})

    def kill(process, job, created):
        assert (process, job, created) == (proc, 123, 456)
        kills.append(process.pid)
        proc.killed = True
        return {"tree_kill": "ok"}

    monkeypatch.setattr(procutil, "_kill_process_tree", kill)

    def run():
        return procutil._run_to_completion(
            proc, ["worker"], input="prompt", timeout=timeout, parent_created=456,
            process_group=False, census_job=123, stop_requested=lambda: callback(proc, clock),
        )
    return run, proc, kills


def test_stop_without_deadline_keeps_platform_attached_timeout_output(monkeypatch):
    """Model a platform attaching partial output to TimeoutExpired (POSIX-shaped)."""
    run, proc, kills = simulate(monkeypatch, lambda *a: True, timeout=None, drain_timeout=True)
    with pytest.raises(procutil.WorkerStopRequested) as raised:
        run()
    assert raised.value.stdout == "partial plus drain"
    assert raised.value.stderr == "err"
    assert raised.value.worker_termination == {
        "tree_kill": "ok", "pid": 42, "confirmed_dead": True,
        "stop_requested": True, "output_drain": "timed_out",
    }
    assert kills == [42]  # only the owned job/tree path, once
    assert proc.calls == [("prompt", 1.0), (None, procutil._POST_KILL_DRAIN_SECONDS)]
    assert not isinstance(raised.value, subprocess.TimeoutExpired)


def test_stdin_is_passed_only_on_first_poll(monkeypatch):
    run, proc, _ = simulate(monkeypatch, lambda p, c: c[0] >= 2)
    with pytest.raises(procutil.WorkerStopRequested):
        run()
    assert [call[0] for call in proc.calls] == ["prompt", None, None]


def test_stop_drain_retains_platform_attached_poll_timeout_output(monkeypatch):
    """Model a platform attaching poll output to TimeoutExpired (POSIX-shaped)."""
    run, proc, kills = simulate(monkeypatch, lambda *a: True)
    communicate = proc.communicate

    def drain(input=None, timeout=None):
        if proc.killed:
            raise subprocess.TimeoutExpired("worker", timeout)
        return communicate(input=input, timeout=timeout)

    monkeypatch.setattr(proc, "communicate", drain)
    with pytest.raises(procutil.WorkerStopRequested) as raised:
        run()
    assert raised.value.stdout == "partial" and raised.value.stderr == "err"
    assert kills == [42]


def test_first_communicate_completion_never_consults_stop_or_kills(monkeypatch):
    run, proc, kills = simulate(monkeypatch, lambda *a: pytest.fail("completed worker polled for stop"))
    proc.returncode = 0
    assert run().stdout == "completed"
    assert proc.calls == [("prompt", 1.0)]
    assert kills == []


@pytest.mark.parametrize("late_in_callback", [True, False])
def test_original_deadline_beats_stop(monkeypatch, late_in_callback):
    callbacks = []

    def callback(proc, clock):
        callbacks.append(True)
        if late_in_callback:
            clock[0] = 10
        return True

    run, _, kills = simulate(monkeypatch, callback, timeout=2 if late_in_callback else 0.2)
    with pytest.raises(subprocess.TimeoutExpired):
        run()
    assert len(callbacks) == int(late_in_callback)
    assert kills == [42]


def test_natural_exit_during_callback_wins_even_after_deadline(monkeypatch):
    def callback(proc, clock):
        proc.returncode = 0
        clock[0] = 10
        return True
    run, _, kills = simulate(monkeypatch, callback)
    assert run().stdout == "completed"
    assert kills == []


@pytest.mark.parametrize("timeout", [3, None])
def test_exited_child_with_held_pipes_still_obeys_deadline_or_stop(monkeypatch, timeout):
    run, proc, kills = simulate(monkeypatch, lambda *a: timeout is None, timeout=timeout)
    proc.returncode = 0

    def held_pipe(input=None, timeout=None):
        assert timeout is not None, "output collection must always be bounded"
        proc.calls.append((input, timeout))
        proc.clock[0] += timeout
        if proc.killed:
            return "partial", ""
        raise subprocess.TimeoutExpired("worker", timeout)

    monkeypatch.setattr(proc, "communicate", held_pipe)
    with pytest.raises(procutil.WorkerStopRequested if timeout is None else subprocess.TimeoutExpired):
        run()
    assert kills == [42]
    assert proc.clock[0] == (2 if timeout is None else timeout) + procutil._POST_KILL_DRAIN_SECONDS


def test_windows_shaped_drain_timeout_does_not_invent_partial_output(monkeypatch):
    run, proc, _ = simulate(monkeypatch, lambda *a: True, timeout=None)

    def no_output(input=None, timeout=None):
        proc.clock[0] += timeout
        raise subprocess.TimeoutExpired("worker", timeout)

    monkeypatch.setattr(proc, "communicate", no_output)
    with pytest.raises(procutil.WorkerStopRequested) as raised:
        run()
    assert raised.value.stdout is None and raised.value.stderr is None
    assert raised.value.worker_termination["output_drain"] == "timed_out"


@pytest.mark.parametrize("confirmed", [True, False])
def test_stopped_result_reports_workspace_stability(confirmed):
    note = workspace_stability_note({"stop_requested": True, "worker_termination": {
        "pid": 42, "confirmed_dead": confirmed,
        "tree_kill": "ok" if confirmed else "census_unknown",
    }}, "workspace")
    assert note["this_worker_may_still_be_writing"] is (not confirmed)
    assert ("warning" in note) is (not confirmed)
    if not confirmed:
        assert "was stopped" in note["warning"] and "may still be writing" in note["warning"]


def test_raising_callback_leaves_worker_running(monkeypatch):
    observations = []

    def callback(proc, clock):
        observations.append(clock[0])
        raise RuntimeError("read failed")

    run, proc, kills = simulate(monkeypatch, callback)
    communicate = proc.communicate

    def finish_after_two_polls(input=None, timeout=None):
        if proc.clock[0] >= 2:
            proc.returncode = 0
        return communicate(input=input, timeout=timeout)

    monkeypatch.setattr(proc, "communicate", finish_after_two_polls)
    assert run().stdout == "completed"
    assert observations == [1.0, 2.0]
    assert kills == []


@pytest.mark.skipif(sys.platform != "win32", reason="confirmed tree stop requires Windows job")
@pytest.mark.parametrize("timeout", [None, 30])
def test_real_tree_stop_with_blocked_stdin_preserves_partial_output(tmp_path, timeout):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "worker.py"
    script.write_text(
        "import subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        "creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "open(sys.argv[1],'w').write(str(child.pid))\n"
        "print('visible partial output',flush=True)\n"
        "time.sleep(120)\n", encoding="utf-8",
    )
    flag = threading.Event()

    def check():
        if pidfile.exists():
            flag.set()
        return flag.is_set()

    started = time.monotonic()
    try:
        with pytest.raises(procutil.WorkerStopRequested) as raised:
            procutil.run_with_tree_kill(
                [sys.executable, str(script), str(pidfile)], input="x" * 2_000_000,
                capture_output=True, text=True, timeout=timeout, stop_requested=check,
            )
        # Load headroom (8.10 local run: 27 s under a loaded PC). The worker and
        # its child sleep 120 s and the timeout case is 30 s, so the stop must
        # still win over both natural exit and the timeout.
        assert time.monotonic() - started < 60
        assert "visible partial output" in raised.value.stdout
        facts = raised.value.worker_termination
        assert facts["confirmed_dead"] and facts["tree_kill"] == "ok"
        assert procutil.pid_state(int(pidfile.read_text())) == "dead"
    finally:
        # Cleanup only, after every assertion has run: if the stop failed, do
        # not leave the 120 s child behind (default job close keeps descendants).
        try:
            if pidfile.exists() and procutil.pid_state(int(pidfile.read_text())) == "alive":
                subprocess.run(["taskkill", "/PID", pidfile.read_text(), "/T", "/F"],
                               capture_output=True, check=False, timeout=30,
                               **procutil.no_window_kwargs())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # never mask the test's own outcome


@pytest.mark.parametrize("executor,provider", [(CodexExecutor, "codex")])
@pytest.mark.parametrize("confirmed", [True, False])
def test_executor_maps_stop_and_forwards_callback(tmp_path, monkeypatch, executor, provider, confirmed,
                                                  synthetic_worker_binary):
    import tempfile
    from modelquad.execution import codex as codex_module, noshell

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(codex_module, "_probe", lambda executable: "0.160.0")
    monkeypatch.setattr(noshell, "qualify", lambda executable, version, **kwargs:
                        noshell.Qualification(True, noshell.file_digest(executable), version, True,
                                              disabled_features=noshell.DISABLED_FEATURES))
    callback = lambda: False  # A stop that arrives while the worker runs.
    facts = {"pid": 42, "tree_kill": "ok" if confirmed else "census_unknown", "confirmed_dead": confirmed}

    def run(argv, **kwargs):
        assert kwargs["stop_requested"] is callback
        raise procutil.WorkerStopRequested(argv, stdout="partial edits", stderr="private error", worker_termination=facts)

    worker = synthetic_worker_binary("worker.exe")
    result = executor(which=lambda _: worker, run=run).execute(
        ExecutionRequest(provider=provider, task_type=TaskType.REVIEW, prompt="task",
                         working_directory=tmp_path, stop_requested=callback),
    )
    # A partial stream is never returned as an answer.
    assert not result.success and result.output == ""
    assert result.task_status_reason == "stop_requested"
    assert result.metadata["stop_requested"] is True
    assert result.metadata["worker_termination"] == facts
    assert "timed_out" not in result.metadata
    assert "private error" not in str(result.metadata)
    assert attempt_resolution(result) == ("failed", "terminated" if confirmed else "timeout")


@pytest.mark.parametrize("role", ["planning_worker", "plan_step_worker"])
@pytest.mark.parametrize("has_outer", [True, False])
def test_auxiliary_stop_binding_ignores_unrelated_attempt(tmp_path, monkeypatch, role, has_outer):
    path = tmp_path / "state.db"
    unrelated = opened(path)
    assert history.request_attempt_stop(path, unrelated) == "accepted"

    class Worker:
        name = "codex"

        def is_available(self):
            return True

        def execute(self, request):
            [row] = [row for row in history.fetch_delegation_attempts(path) if row["detail"] == role]
            assert not request.stop_requested()
            assert history.request_attempt_stop(path, row["id"]) == "accepted"
            assert request.stop_requested()
            return ExecutionResult(provider="codex", success=True, output="done", exit_code=0,
                                   task_status=WorkerTaskStatus.COMPLETED)

    manager = ExecutionManager(Config(), executors={"codex": Worker()}, health_db_path=path)
    manager.launch_attempt_id = opened(path) if has_outer else None
    if not has_outer:
        # Synthetic auxiliary-only manager: no production outer admission claim.
        monkeypatch.setattr(manager, "require_launch_admission", lambda: None)
    manager.main_worker_entered = role == "plan_step_worker"
    manager.executors["codex"].execute(ExecutionRequest(
        provider="codex", task_type=TaskType.ARCHITECTURE, prompt="task", working_directory=tmp_path,
        on_spawn=(lambda pid: None) if role == "plan_step_worker" else None,
    ))


@pytest.mark.parametrize("role", ["planning_worker", "plan_step_worker"])
def test_auxiliary_stop_before_start_has_no_worker_fate(tmp_path, role):
    path = tmp_path / "state.db"
    outer = opened(path)
    assert history.request_attempt_stop(path, outer) == "accepted"

    class Worker:
        name = "codex"

        def is_available(self):
            return True

        def execute(self, request):
            pytest.fail("stopped auxiliary worker must not start")

    manager = ExecutionManager(Config(), executors={"codex": Worker()}, health_db_path=path)
    manager.launch_attempt_id = outer
    manager.main_worker_entered = role == "plan_step_worker"
    manager.on_launch_entered = lambda: pytest.fail("executor entry must not be signalled")
    result = manager.executors["codex"].execute(ExecutionRequest(
        provider="codex", task_type=TaskType.ARCHITECTURE, prompt="task", working_directory=tmp_path,
        on_spawn=(lambda pid: pytest.fail("no spawn")) if role == "plan_step_worker" else None,
    ))
    assert result.task_status_reason == "stop_requested"
    assert attempt_resolution(result) == ("failed", "failed_to_start")
    assert result.metadata == {"worker_started": False}
    assert not workspace_stability_note(result.metadata, tmp_path)
    assert not manager.launch_entered
    [row] = [row for row in history.fetch_delegation_attempts(path) if row["id"] != outer]
    assert row["status"] == "failed" and row["failure_kind"] == "failed_to_start"
    assert row["detail"] == role + ": stop_requested"
    assert row["worker_pid"] is None


@pytest.mark.parametrize("callback,stop_outer", [(False, True), (True, True), (True, False)])
def test_main_worker_stop_before_executor_entry(tmp_path, callback, stop_outer):
    path = tmp_path / "state.db"
    outer = opened(path)
    if stop_outer:
        assert history.request_attempt_stop(path, outer) == "accepted"

    class Worker:
        name = "codex"

        def is_available(self):
            return True

        def execute(self, request):
            pytest.fail("stopped main worker must not start")

    manager = ExecutionManager(Config(), executors={"codex": Worker()}, health_db_path=path)
    manager.launch_attempt_id = outer
    manager.on_launch_entered = lambda: pytest.fail("executor entry must not be signalled")
    result = manager.executors["codex"].execute(ExecutionRequest(
        provider="codex", task_type=TaskType.IMPLEMENTATION, prompt="task", working_directory=tmp_path,
        on_spawn=lambda pid: pytest.fail("no spawn"),
        stop_requested=(lambda: not stop_outer) if callback else None,
    ))
    assert result.task_status_reason == "stop_requested"
    assert attempt_resolution(result) == ("failed", "failed_to_start")
    assert result.metadata == {"worker_started": False}
    assert not manager.launch_entered and not manager.main_worker_entered
    row = history.fetch_delegation_attempts(path)[0]
    assert row["id"] == outer and row["status"] == "failed"
    assert row["failure_kind"] == "failed_to_start" and row["detail"] == "stop_requested"


@pytest.mark.parametrize("role", ["planning_worker", "plan_step_worker"])
@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("stop_outer", [True, False])
def test_auxiliary_attempt_stop_binding_and_terminal_detail(tmp_path, role, confirmed, stop_outer):
    path = tmp_path / "state.db"
    db.connect_and_init(path).close()

    class Worker:
        name = "codex"

        def is_available(self):
            return True

        def execute(self, request):
            [row] = [row for row in history.fetch_delegation_attempts(path) if row["detail"] == role]
            assert row["detail"] == role
            assert not request.stop_requested()
            assert history.request_attempt_stop(path, manager.launch_attempt_id if stop_outer else row["id"]) == "accepted"
            assert request.stop_requested()
            request.on_spawn(42)
            return ExecutionResult(
                provider="codex", success=False, output="partial", exit_code=None,
                task_status=WorkerTaskStatus.FAILED, task_status_reason="stop_requested",
                metadata={"stop_requested": True, "worker_termination": {
                    "pid": 42, "confirmed_dead": confirmed,
                    "tree_kill": "ok" if confirmed else "census_unknown",
                }},
            )

    manager = ExecutionManager(Config(), executors={"codex": Worker()}, health_db_path=path)
    manager.launch_attempt_id = opened(path)
    manager.main_worker_entered = role == "plan_step_worker"
    manager.executors["codex"].execute(ExecutionRequest(
        provider="codex", task_type=TaskType.ARCHITECTURE, prompt="task", working_directory=tmp_path,
        on_spawn=(lambda pid: pytest.fail("main binding reused")) if role == "plan_step_worker" else None,
    ))
    [row] = [row for row in history.fetch_delegation_attempts(path) if row["id"] != manager.launch_attempt_id]
    assert row["status"] == "failed" and row["failure_kind"] == ("terminated" if confirmed else "timeout")
    assert row["detail"].startswith(role + ": stop_requested; worker pid 42")
    assert ("exit confirmed" if confirmed else "unaccounted for") in row["detail"]


def test_database_less_manager_honours_request_stop(tmp_path):
    from modelquad.execution.manager import _AdmittedExecutor

    class Worker:
        name = "codex"

        def is_available(self):
            return True

        def execute(self, request):
            pytest.fail("stopped main worker must not start")

    manager = ExecutionManager(Config(), executors={"codex": Worker()})
    manager.launch_attempt_id = 123
    result = _AdmittedExecutor(manager.executors["codex"], manager).execute(ExecutionRequest(
        provider="codex", task_type=TaskType.IMPLEMENTATION, prompt="task", working_directory=tmp_path,
        on_spawn=lambda pid: pytest.fail("no spawn"), stop_requested=lambda: True,
    ))
    assert result.task_status_reason == "stop_requested"
    assert result.metadata == {"worker_started": False}
    assert not manager.launch_entered and not manager.main_worker_entered
