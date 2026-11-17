# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A timed-out worker is terminated *and recorded*.

The tree kill existed (`procutil.run_with_tree_kill`); what did not was any
record of whether it worked. These tests pin the chain: the runner reports
how the tree kill went and whether the direct child is confirmed dead, the
executor carries that on the result, and the reply's note says it in
words -- "terminated (exit confirmed)" or "NOT confirmed dead -- unaccounted
for" -- never rounding an attempted kill up to a finished one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from modelquad import procutil, review
from modelquad.executable_resolution import Resolution
from modelquad.execution.base import (
    ExecutionRequest,
    ExecutionResult,
    WorkerTaskStatus,
    attempt_resolution,
    describe_worker_termination,
    termination_facts,
    worker_confirmed_dead,
)
from modelquad.execution.codex import CodexExecutor
from modelquad.models import TaskType
from modelquad.procutil import run_with_tree_kill

from conftest import no_window_kwargs


def _pid_alive(pid: int) -> bool:
    """Is this pid a live process? Asked of the OS directly, on either platform.

    Deliberately NOT `procutil.pid_state`: that function is part of what these
    tests check, and a probe that shares the bug it is meant to catch proves
    nothing.
    """

    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is simply not ours to signal
    return True


# -- the runner ------------------------------------------------------------------


# A parent with one descendant, bounded at both ends. The descendant outlives
# the six-second timeout -- so on Windows the tree walk has something real to
# reach, and on POSIX something real to NOT reach -- but it cannot outlive the
# test: its sleep is short and `finally` kills it by pid regardless. Its stdio
# is detached from the pipes this test captures, which is the other half of the
# bound: on POSIX only the direct child is killed, and a survivor holding those
# pipes made the post-kill drain wait out its whole lifetime.
_BOUNDED_DESCENDANT_SECONDS = 20
_BOUNDED_PARENT_SCRIPT = """
import subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(%d)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
with open(sys.argv[1], "w") as f:
    f.write(str(child.pid))
time.sleep(120)
""" % _BOUNDED_DESCENDANT_SECONDS


def _reap(pid):
    """Leave no process behind, whatever the assertions did."""

    if pid is None or not _pid_alive(pid):
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10,
                           **no_window_kwargs())
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001 -- cleanup is best effort
        pass


def _wait_until_gone(pid, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.25)
    return not _pid_alive(pid)


@pytest.mark.serial_timing
def test_the_timeout_carries_what_became_of_the_worker(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(_BOUNDED_PARENT_SCRIPT, encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"
    descendant = None
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            run_with_tree_kill([sys.executable, str(script), str(pidfile)],
                               capture_output=True, text=True, timeout=6)
        elapsed = time.monotonic() - started
        descendant = int(pidfile.read_text(encoding="utf-8"))
        facts = getattr(caught.value, "worker_termination", None)
        assert isinstance(facts, dict), "the exception is the only thing this path returns, so the facts ride on it"
        assert isinstance(facts["pid"], int) and facts["pid"] > 0
        assert facts["confirmed_dead"] is True, "read from the process table after the kill, not assumed"
        assert not _pid_alive(facts["pid"])
        # Bounded: the call may not wait out a surviving descendant's lifetime.
        assert elapsed < 30, f"the timeout path took {elapsed:.1f}s; the post-kill drain is unbounded"
        metadata = {"worker_termination": facts}
        if os.name == "nt":
            assert facts["tree_kill"] == "ok"
            assert worker_confirmed_dead(metadata) is True, "child dead AND the tree walk succeeded"
            # The tree walk is the thing that reaches the descendant.
            assert _wait_until_gone(descendant), f"descendant {descendant} survived the tree kill"
        else:
            # There is no tree walk here, so the direct child's exit says nothing
            # about what it spawned. That is not a confirmed death, and the record
            # must not claim one -- the descendant's own lifecycle, asserted here
            # independently, is exactly what the record declines to speak for.
            assert facts["tree_kill"] == "direct_child_only"
            assert worker_confirmed_dead(metadata) is False
            assert _pid_alive(descendant), (
                f"descendant {descendant} should still be running -- that it is, is "
                "precisely why `direct_child_only` may not read as a confirmed death"
            )
    finally:
        _reap(descendant)


# -- the executors -----------------------------------------------------------------


def _request(tmp_path, timeout=60.0):
    # The test doubles carry their own short timeouts; this budget only bounds
    # the checks before launch, which a loaded machine can stretch past 1 s.
    return ExecutionRequest(provider="codex", task_type=TaskType.REVIEW, prompt="do",
                            working_directory=tmp_path, timeout=timeout)


@pytest.fixture(autouse=True)
def qualified_codex(monkeypatch):
    """Codex runs here own launch outcomes, not feature qualification."""
    from modelquad.execution import codex as codex_module, noshell

    monkeypatch.setattr(codex_module, "_probe", lambda executable: "0.160.0")
    monkeypatch.setattr(noshell, "qualify", lambda executable, version, **kwargs:
                        noshell.Qualification(True, noshell.file_digest(executable), version, True,
                                              disabled_features=noshell.DISABLED_FEATURES))


def _timeout_raiser(facts):
    def run(argv, **_kwargs):
        error = subprocess.TimeoutExpired(cmd=argv, timeout=1.0, output="", stderr="")
        if facts is not None:
            error.worker_termination = facts
        raise error
    return run


def test_the_codex_executor_carries_the_termination_facts_when_the_runner_has_them(tmp_path, *, synthetic_worker_binary):
    facts = {"pid": 4242, "tree_kill": "ok", "confirmed_dead": True}
    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=_timeout_raiser(facts)).execute(_request(tmp_path))
    assert result.metadata["timed_out"] is True
    assert result.metadata["worker_termination"] == facts
    assert result.task_status is WorkerTaskStatus.FAILED


def test_a_runner_that_says_nothing_about_the_worker_leaves_the_record_silent(tmp_path, *, synthetic_worker_binary):
    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=_timeout_raiser(None)).execute(_request(tmp_path))
    assert result.metadata["timed_out"] is True
    assert "worker_termination" not in result.metadata
    assert termination_facts(subprocess.TimeoutExpired(cmd="x", timeout=1)) == {}


# -- the words -------------------------------------------------------------------------


def test_the_description_never_rounds_an_attempted_kill_up_to_a_finished_one():
    assert describe_worker_termination({}) == ""
    assert describe_worker_termination({"worker_termination": {"pid": 7, "tree_kill": "ok", "confirmed_dead": True}}) == (
        "worker pid 7 terminated (tree kill ok; exit confirmed)")
    unaccounted = describe_worker_termination({"worker_termination": {"pid": 7, "tree_kill": "timed_out", "confirmed_dead": False}})
    assert "NOT confirmed dead" in unaccounted and "unaccounted" in unaccounted and "pid 7" in unaccounted
    assert "NOT confirmed" in describe_worker_termination({"worker_termination": {"pid": 7}})


# -- the closed-vocabulary outcome ------------------------------------------------------


def _timed_out_result(facts):
    metadata = {"timed_out": True}
    if facts is not None:
        metadata["worker_termination"] = dict(facts)
    return ExecutionResult(provider="codex", success=False, output="", exit_code=None,
                           error="timed out", metadata=metadata,
                           task_status=WorkerTaskStatus.FAILED, task_status_reason="timed_out")


def test_only_a_confirmed_death_earns_the_terminated_failure_kind():
    """`terminated` is the third worker fate, and it is spelled only
    from the process table -- never from a kill having merely been asked for."""

    assert attempt_resolution(_timed_out_result({"pid": 7, "tree_kill": "ok", "confirmed_dead": True})) == (
        "failed", "terminated")
    assert attempt_resolution(_timed_out_result({"pid": 7, "tree_kill": "timed_out", "confirmed_dead": False})) == (
        "failed", "timeout")
    assert attempt_resolution(_timed_out_result({"pid": 7})) == ("failed", "timeout")
    assert attempt_resolution(_timed_out_result(None)) == ("failed", "timeout")


def test_a_dead_direct_child_is_not_a_dead_tree():
    """`confirmed_dead` is read for the DIRECT CHILD only. Where the tree walk
    did not succeed -- POSIX has none, and on Windows taskkill can fail or hang
    -- the descendants are exactly the workers this work exists to stop, so the
    result must say `timeout`, not `terminated`."""

    confirmed = {"pid": 7, "tree_kill": "ok", "confirmed_dead": True}
    assert worker_confirmed_dead({"worker_termination": confirmed}) is True
    assert attempt_resolution(_timed_out_result(confirmed)) == ("failed", "terminated")

    for tree in ("direct_child_only", "exit 128", "timed_out"):
        facts = {"pid": 7, "tree_kill": tree, "confirmed_dead": True}
        assert worker_confirmed_dead({"worker_termination": facts}) is False, tree
        assert attempt_resolution(_timed_out_result(facts)) == ("failed", "timeout"), tree
        sentence = describe_worker_termination({"worker_termination": facts})
        assert "NOT confirmed dead" in sentence and "unaccounted for" in sentence
        assert tree in sentence, "the sentence says WHY it is unaccounted for"
        assert "terminated (" not in sentence


# -- the reply, end to end ------------------------------------------------------------------


class _WorkerThatIsKilled:
    """A double for the executor's timeout path: it returns what `codex.py`
    returns when `run_with_tree_kill` raised with the termination facts on."""

    def __init__(self, facts):
        self.facts = facts

    def execute(self, request):
        metadata = {"timed_out": True, "worker_started": True, "command": ["codex"]}
        if self.facts is not None:
            metadata["worker_termination"] = dict(self.facts)
        return ExecutionResult(provider=request.provider, success=False, output="", exit_code=None,
                               error="codex execution timed out", metadata=metadata,
                               task_status=WorkerTaskStatus.FAILED, task_status_reason="timed_out")


def _review(tmp_path, facts):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reply = review.run(review.ReviewRequest("review", "do the thing", str(workspace), timeout=5.0),
                       _WorkerThatIsKilled(facts))
    assert reply["success"] is False
    assert reply["reason"] == "timed_out"
    return reply


def test_a_confirmed_termination_is_in_the_reply(tmp_path):
    reply = _review(tmp_path, {"pid": 4242, "tree_kill": "ok", "confirmed_dead": True})
    note = reply["worker_note"]
    assert "worker pid 4242 terminated (tree kill ok; exit confirmed)" in note["worker_fate"]
    assert note["this_worker_may_still_be_running"] is False
    assert "warning" not in note


def test_an_unconfirmed_kill_reads_as_unaccounted_not_as_terminated(tmp_path):
    reply = _review(tmp_path, {"pid": 4242, "tree_kill": "timed_out", "confirmed_dead": False})
    note = reply["worker_note"]
    assert "NOT confirmed dead" in note["worker_fate"] and "unaccounted" in note["worker_fate"]
    assert "terminated (" not in note["worker_fate"]
    assert note["this_worker_may_still_be_running"] is True
    assert "may still be running" in note["warning"]


def test_a_worker_the_runner_said_nothing_about_adds_nothing_to_the_reply(tmp_path):
    reply = _review(tmp_path, None)
    assert "worker_termination" not in reply["metadata"]
    note = reply["worker_note"]
    assert "worker pid" not in note["worker_fate"]
    assert note["this_worker_may_still_be_running"] is True


# -- the tri-state probe ------------------------------------------------------------------


#: Exited children kept referenced, so Windows does not hand their pid to a
#: new process while a test still treats it as dead.
_EXITED = []


def _dead_pid():
    """A pid that belonged to a real process which has since exited."""

    proc = subprocess.Popen([sys.executable, "-c", "pass"], **no_window_kwargs())
    proc.wait()
    _EXITED.append(proc)
    return proc.pid


@pytest.mark.skipif(os.name != "nt", reason="access-denied without a handle is the Windows probe")
def test_a_real_access_denied_pid_does_not_read_as_dead():
    """Not a monkeypatched probe: pid 4 is the Windows System process, which
    `OpenProcess` refuses even PROCESS_QUERY_LIMITED_INFORMATION on. It exists,
    so the answer may not be "dead" -- that fold is the whole of the defect."""

    assert procutil.pid_state(4) != "dead"
    assert procutil.pid_state(os.getpid()) == "alive"
    assert procutil.pid_state(_dead_pid()) == "dead"


# -- a cleanup tool that will not launch ----------------------------


def _refuse_to_launch_taskkill(tmp_path, monkeypatch):
    executable = tmp_path / 'synthetic system' / 'taskkill.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'synthetic fixture - never execute\n')
    resolved = os.path.realpath(executable)

    def resolve(name):
        assert name == 'taskkill'
        return Resolution(resolved, 'system', 'binary', None)

    monkeypatch.setattr(procutil, 'system_executable', resolve)
    real_run = subprocess.run

    def _no_taskkill(argv, *args, **kwargs):
        if argv and os.path.normcase(str(argv[0])) == os.path.normcase(resolved):
            raise FileNotFoundError(2, "The system cannot find the file specified", "taskkill")
        # console-window-exempt: this IS `subprocess.run`, stood in for by
        # monkeypatch and forwarding its caller's own keyword arguments.
        # `procutil` already passes the flag at the call site under test
        # (`test_run_with_tree_kill_does_not_change_how_a_worker_is_spawned`
        # asserts it), so a `creationflags` added here would be a second one
        # and a `TypeError` on every call this shim exists to pass through.
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(procutil.subprocess, "run", _no_taskkill)


@pytest.mark.skipif(os.name != "nt", reason="the taskkill sweep exists only on Windows")
def test_a_sweep_that_cannot_launch_still_kills_the_worker_and_still_records_it(tmp_path, monkeypatch):
    """A `taskkill` that will not start is a fact about the cleanup, not about
    the worker. It used to escape as a FileNotFoundError -- skipping the fallback
    kill, losing the facts, and leaving the executor to call a worker that HAD
    started `failed_to_start`.

    Now the job stops the tree and its census decides `tree_kill`;
    `taskkill` is a second sweep whose outcome is recorded on its own, so a
    sweep that cannot launch no longer makes a dead tree read as unaccounted."""

    _refuse_to_launch_taskkill(tmp_path, monkeypatch)
    child = None
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            run_with_tree_kill([sys.executable, "-c", "import time; time.sleep(120)"],
                               capture_output=True, text=True, timeout=3)
        facts = getattr(caught.value, "worker_termination", None)
        assert isinstance(facts, dict), "the facts must survive a failed sweep"
        child = facts["pid"]
        assert facts["taskkill"] == "launch_error: FileNotFoundError", "named, not swallowed"
        assert facts["confirmed_dead"] is True, "the kill ran, and its result was read"
        assert _wait_until_gone(child), f"worker {child} survived: the kill was skipped"
        # The census read the job empty, which is what `terminated` rests on.
        assert facts["tree_kill"] == "ok"
        assert worker_confirmed_dead({"worker_termination": facts}) is True
    finally:
        _reap(child)


@pytest.mark.skipif(os.name != "nt", reason="the taskkill sweep exists only on Windows")
def test_the_executor_calls_a_failed_sweep_a_timeout_never_a_failed_start(tmp_path, monkeypatch, *, synthetic_worker_binary):
    """The classification the review reproduced: the worker started, so the result
    may not say `failed_to_start`."""

    _refuse_to_launch_taskkill(tmp_path, monkeypatch)
    seen = {}

    def _run_a_real_worker(argv, **_kwargs):
        return run_with_tree_kill([sys.executable, "-c", "import time; time.sleep(120)"],
                                  capture_output=True, text=True, timeout=3,
                                  on_spawn=lambda pid: seen.setdefault("pid", pid))

    try:
        result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=_run_a_real_worker).execute(_request(tmp_path))
        assert result.task_status_reason == "timed_out", "the worker ran; this is its timeout"
        assert result.metadata["timed_out"] is True
        assert result.metadata["worker_termination"]["taskkill"] == "launch_error: FileNotFoundError"
        assert attempt_resolution(result) == ("failed", "terminated")
        assert _wait_until_gone(seen["pid"])
    finally:
        _reap(seen.get("pid"))


@pytest.mark.skipif(os.name != "nt", reason="the Job Object census exists only on Windows")
def test_an_unreadable_census_after_the_stop_reads_as_an_unaccounted_timeout(tmp_path, monkeypatch, *, synthetic_worker_binary):
    """What `terminated` now rests on is the census. When it cannot be read the
    result stays a timeout and says the worker is unaccounted for."""

    monkeypatch.setattr(procutil, "_census_job_pids", lambda handle: None)
    seen = {}

    def _run_a_real_worker(argv, **_kwargs):
        return run_with_tree_kill([sys.executable, "-c", "import time; time.sleep(120)"],
                                  capture_output=True, text=True, timeout=3,
                                  on_spawn=lambda pid: seen.setdefault("pid", pid))

    try:
        result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=_run_a_real_worker).execute(_request(tmp_path))
        assert result.task_status_reason == "timed_out"
        assert result.metadata["worker_termination"]["tree_kill"] == "census_unknown"
        assert attempt_resolution(result) == ("failed", "timeout")
        sentence = describe_worker_termination(result.metadata)
        assert "NOT confirmed dead" in sentence and "unaccounted for" in sentence
    finally:
        _reap(seen.get("pid"))


def test_a_failed_tree_walk_reaches_the_reply_as_an_unaccounted_timeout(tmp_path):
    """End to end, on either platform: the reply carries a closed census error and
    stays unaccounted for, never `terminated`."""

    reply = _review(tmp_path, {"pid": 4242, "tree_kill": "launch_error: FileNotFoundError",
                               "confirmed_dead": True})
    assert reply["metadata"]["worker_termination"]["tree_kill"] == "census_unknown"
    detail = reply["worker_note"]["worker_fate"]
    assert "NOT confirmed dead" in detail and "unaccounted for" in detail
    assert "census_unknown" in detail
    assert "launch_error: FileNotFoundError" not in detail
    assert "terminated (" not in detail
    assert reply["worker_note"]["this_worker_may_still_be_running"] is True
