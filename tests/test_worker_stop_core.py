# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A stop request ends the worker tree and preserves observed process/output evidence."""

import subprocess
import sys
import threading
import time

import pytest

from modelquad import procutil
from modelquad.execution import CodexExecutor
from modelquad.execution.base import ExecutionRequest, attempt_resolution, workspace_stability_note
from modelquad.models import TaskType


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


