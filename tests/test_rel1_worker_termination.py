# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A timed-out worker is terminated *and recorded*.

The tree kill existed (`procutil.run_with_tree_kill`); what did not was any
record of whether it worked. These tests pin the chain: the runner reports
how the tree kill went and whether the direct child is confirmed dead, the
executors carry that on the result, and the attempt row's detail says it in
words -- "terminated (exit confirmed)" or "NOT confirmed dead -- unaccounted
for" -- never rounding an attempted kill up to a finished one.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time

import pytest

from orchestrator import procutil
from orchestrator.executable_resolution import Resolution
from orchestrator.execution import ExecutionManager
from orchestrator.execution.base import (
    ExecutionRequest,
    ExecutionResult,
    WorkerTaskStatus,
    attempt_resolution,
    describe_worker_termination,
    termination_facts,
    worker_confirmed_dead,
)
from orchestrator.execution.codex import CodexExecutor
from orchestrator.models import TaskType
from orchestrator.procutil import run_with_tree_kill
from orchestrator.service import db as service_db
from orchestrator.service import history as service_history

from conftest import tool_payload


def _tool_payload(name, arguments):
    import asyncio

    from orchestrator.mcp.server import server

    return tool_payload(asyncio.run(server.call_tool(name, arguments)))


def write_usage_fixtures(path):
    (path / "usage-claude.json").write_text('{"five_hour_used_percentage": 10}', encoding="utf-8")
    (path / "usage-codex.json").write_text('{"available": true, "five_hour_remaining_percentage": 80}', encoding="utf-8")

from conftest import no_window_kwargs


@pytest.fixture(autouse=True)
def _tool_usage_directory(tmp_path, monkeypatch):
    """The MCP tools read quota snapshots from `server.USAGE_DIRECTORY`, never
    from a tool argument. This module's snapshots live in the
    test's tmp_path, which every call here used to pass as `usage_directory`."""
    from orchestrator.mcp import server as mcp_server_module

    monkeypatch.setattr(mcp_server_module, "USAGE_DIRECTORY", str(tmp_path))


mcp_server = importlib.import_module("orchestrator.mcp.server")


def _pid_alive(pid: int) -> bool:
    """Is this pid a live process? Asked of the OS directly, on either platform.

    Deliberately NOT `procutil.pid_alive`: that function is part of what these
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


def _request(tmp_path, timeout=1.0):
    return ExecutionRequest(provider="codex", task_type=TaskType.REVIEW, prompt="do",
                            working_directory=tmp_path, timeout=timeout)


@pytest.fixture(autouse=True)
def qualified_codex(monkeypatch):
    """Codex runs here own launch outcomes, not feature qualification."""
    from orchestrator.execution import codex as codex_module, noshell

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


def test_the_row_never_rounds_an_attempted_kill_up_to_a_finished_one():
    assert describe_worker_termination({}) == ""
    assert describe_worker_termination({"worker_termination": {"pid": 7, "tree_kill": "ok", "confirmed_dead": True}}) == (
        "worker pid 7 terminated (tree kill ok; exit confirmed)")
    unaccounted = describe_worker_termination({"worker_termination": {"pid": 7, "tree_kill": "timed_out", "confirmed_dead": False}})
    assert "NOT confirmed dead" in unaccounted and "unaccounted" in unaccounted and "pid 7" in unaccounted
    assert "NOT confirmed" in describe_worker_termination({"worker_termination": {"pid": 7}})


# -- the closed-vocabulary outcome (what part 4 reads instead of the words) ------------


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

    assert "terminated" in service_history.DELEGATION_FAILURE_KINDS
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
    row must say `timeout`, not `terminated`."""

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


# -- the row, end to end ------------------------------------------------------------------


class _WorkerThatIsKilled:
    """A double for the executor's timeout path: it returns what `codex.py`
    returns when `run_with_tree_kill` raised with the termination facts on."""

    def __init__(self, facts):
        self.facts = facts

    def is_available(self):
        return True

    def execute(self, request):
        metadata = {"timed_out": True, "command": ["codex"]}
        if self.facts is not None:
            metadata["worker_termination"] = dict(self.facts)
        return ExecutionResult(provider=request.provider, success=False, output="", exit_code=None,
                               error="codex execution timed out", metadata=metadata,
                               task_status=WorkerTaskStatus.FAILED, task_status_reason="timed_out")


@pytest.fixture
def delegation(tmp_path, monkeypatch):
    write_usage_fixtures(tmp_path)
    db_path = tmp_path / "orchestrator.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = service_db.DEFAULT_DB_PATH
    service_db.DEFAULT_DB_PATH = db_path
    holder = {"worker": None}
    real_init = ExecutionManager.__init__

    def _patched_init(self, config, **kwargs):
        kwargs.pop("executors", None)
        real_init(self, config, executors={"codex": holder["worker"]}, **kwargs)

    monkeypatch.setattr(ExecutionManager, "__init__", _patched_init)
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "900000")
    try:
        yield {"db_path": db_path, "workspace": workspace, "usage": str(tmp_path), "holder": holder}
    finally:
        service_db.DEFAULT_DB_PATH = original


def _delegate(delegation):
    return _tool_payload("modelquad_delegate", {
        "task_type": "review", "prompt": "do the thing",
        "working_directory": str(delegation["workspace"]), "provider": "codex", "timeout": 5.0,
    })


def test_a_confirmed_termination_is_on_the_attempt_row(delegation):
    delegation["holder"]["worker"] = _WorkerThatIsKilled({"pid": 4242, "tree_kill": "ok", "confirmed_dead": True})
    payload = _delegate(delegation)
    assert payload["success"] is False
    [row] = service_history.fetch_delegation_attempts(delegation["db_path"])
    assert (row["status"], row["failure_kind"]) == ("failed", "terminated")
    assert "worker pid 4242 terminated (tree kill ok; exit confirmed)" in str(row["detail"])


def test_an_unconfirmed_kill_reads_as_unaccounted_not_as_terminated(delegation):
    delegation["holder"]["worker"] = _WorkerThatIsKilled({"pid": 4242, "tree_kill": "timed_out", "confirmed_dead": False})
    _delegate(delegation)
    [row] = service_history.fetch_delegation_attempts(delegation["db_path"])
    assert row["status"] == "failed"
    # The kind stays `timeout`, so part 4 renders it UNKNOWN rather than dead.
    assert row["failure_kind"] == "timeout"
    assert "NOT confirmed dead" in str(row["detail"]) and "unaccounted" in str(row["detail"])
    assert "terminated (" not in str(row["detail"])


def test_a_worker_the_runner_said_nothing_about_adds_nothing_to_the_row(delegation):
    delegation["holder"]["worker"] = _WorkerThatIsKilled(None)
    _delegate(delegation)
    [row] = service_history.fetch_delegation_attempts(delegation["db_path"])
    assert "worker pid" not in str(row["detail"])
    assert row["failure_kind"] == "timeout"


# -- the caller already left: account for the worker, do not assume its fate ----------


def _dead_pid():
    """A pid that belonged to a real process which has since exited."""

    proc = subprocess.Popen([sys.executable, "-c", "pass"], **no_window_kwargs())
    proc.wait()
    return proc.pid


def _abandoned_attempt(db_path, decision_id, pid=None):
    attempt_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", decision_id=decision_id
    )
    if pid is not None:
        service_history.record_worker_pid(db_path, attempt_id, pid)
    assert service_history.record_caller_abandonment(db_path, attempt_id, reason="transport_cancelled")
    return attempt_id


def _row(db_path, attempt_id):
    return [r for r in service_history.fetch_delegation_attempts(db_path) if r["id"] == attempt_id][0]


def test_an_abandoned_caller_whose_worker_is_dead_is_accounted_for_not_left_open(tmp_path):
    """The dead worker of a departed caller becomes terminal -- as `orphaned`,
    the kind reconciliation already uses for a dead pid. Nothing killed it
    here, so it is not `terminated`."""

    db_path = tmp_path / "history.db"
    attempt_id = _abandoned_attempt(db_path, "gone-and-dead", pid=_dead_pid())

    [reported] = service_history.reconcile_abandoned_attempts(db_path)

    assert reported["id"] == attempt_id and reported["reconciled"] is True
    row = _row(db_path, attempt_id)
    assert (row["status"], row["failure_kind"]) == ("failed", "orphaned")
    assert "the caller had abandoned this attempt at" in str(row["detail"])
    assert "was found dead at reconcile time" in str(row["detail"])
    assert row["unresolved"] is False
    # The departure itself is never rewritten by the accounting for the worker.
    assert row["caller_abandoned_at"] is not None


def test_an_abandoned_caller_whose_worker_still_runs_stays_open_and_is_reported(tmp_path):
    """The caller leaving is not a verdict about the worker: a live pid keeps
    the row `started`, and the pass reports it instead of closing it."""

    db_path = tmp_path / "history.db"
    attempt_id = _abandoned_attempt(db_path, "gone-but-alive", pid=os.getpid())
    before = _row(db_path, attempt_id)["caller_abandoned_at"]

    [reported] = service_history.reconcile_abandoned_attempts(db_path)

    assert reported["reconciled"] is False and reported["liveness"] == "in_flight"
    assert reported["caller_abandoned"] is True
    row = _row(db_path, attempt_id)
    assert row["status"] == "started" and row["resolved_at"] is None
    assert row["failure_kind"] is None
    assert row["caller_abandoned_at"] == before
    # And the live worker still blocks a second worker for the same request.
    key = service_history.request_fingerprint("implementation", str(tmp_path), "do the thing")
    dup_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", decision_id="dup", request_key=key
    )
    service_history.record_worker_pid(db_path, dup_id, os.getpid())
    assert service_history.record_caller_abandonment(db_path, dup_id, reason="transport_closed")
    service_history.reconcile_abandoned_attempts(db_path)
    duplicate = service_history.find_in_flight_duplicate(db_path, key)
    assert duplicate is not None and duplicate["id"] == dup_id


def test_the_pass_never_touches_a_row_whose_caller_is_still_waiting(tmp_path):
    """Only rows part 2 marked as abandoned are in this pass's population --
    a caller who never left keeps its row entirely, dead pid or not."""

    db_path = tmp_path / "history.db"
    waiting = service_history.record_delegation_attempt(
        db_path, task_type="implementation", decision_id="still-waiting"
    )
    service_history.record_worker_pid(db_path, waiting, _dead_pid())

    assert service_history.reconcile_abandoned_attempts(db_path) == []

    row = _row(db_path, waiting)
    assert row["status"] == "started" and row["resolved_at"] is None
    assert row["caller_abandoned_at"] is None


def test_the_pass_is_bounded_and_reads_only_open_abandoned_rows(tmp_path):
    """Bounded by `limit`, and a row it already closed does not come back:
    the pass is per-call work, not a queue that grows."""

    db_path = tmp_path / "history.db"
    for index in range(3):
        _abandoned_attempt(db_path, f"dead-{index}", pid=_dead_pid())

    first = service_history.reconcile_abandoned_attempts(db_path, limit=2)
    assert len(first) == 2 and all(entry["reconciled"] for entry in first)
    second = service_history.reconcile_abandoned_attempts(db_path)
    assert len(second) == 1 and second[0]["reconciled"] is True
    assert service_history.reconcile_abandoned_attempts(db_path) == []


def test_a_row_resolved_between_the_probe_and_the_write_keeps_its_real_outcome(tmp_path, monkeypatch):
    """The pass observes, then writes, and the worker's own thread may resolve
    the row in between. A guess must never overwrite a fact: `orphaned` is what
    reconciliation believes, `completed` is what actually happened."""

    db_path = tmp_path / "history.db"
    attempt_id = _abandoned_attempt(db_path, "resolved-mid-pass", pid=_dead_pid())

    def _the_worker_finishes_while_we_probe(pid):
        service_history.resolve_delegation_attempt(
            db_path,
            attempt_id,
            status="completed",
            routed_provider="codex",
            outcome="success",
            detail="the worker came back while the pass was probing",
        )
        return "dead"

    monkeypatch.setattr(procutil, "pid_state", _the_worker_finishes_while_we_probe)

    [reported] = service_history.reconcile_abandoned_attempts(db_path)

    assert reported["reconciled"] is False, "nothing was written, so nothing may be claimed"
    row = _row(db_path, attempt_id)
    assert (row["status"], row["outcome"]) == ("completed", "success")
    assert row["failure_kind"] is None
    assert "orphaned" not in str(row["detail"])
    assert "the worker came back while the pass was probing" in str(row["detail"])


def test_a_worker_pid_that_is_not_an_integer_is_skipped_and_reported(tmp_path):
    """A malformed row is UNKNOWN, like a pid-less one. This pass runs on the
    delegation path, so it reports the row instead of raising on it."""

    db_path = tmp_path / "history.db"
    attempt_id = _abandoned_attempt(db_path, "unreadable-pid", pid=_dead_pid())
    conn = service_db.connect_and_init(db_path)
    try:
        conn.execute("UPDATE delegation_attempts SET worker_pid = ? WHERE id = ?", ("not-a-pid", attempt_id))
        conn.commit()
    finally:
        conn.close()

    [reported] = service_history.reconcile_abandoned_attempts(db_path)

    assert reported["reconciled"] is False
    assert "not an integer" in str(reported.get("reconcile_skipped"))
    row = _row(db_path, attempt_id)
    assert row["status"] == "started" and row["resolved_at"] is None


# -- a probe that cannot answer is not a death -----------------------


def _abandoned_with_key(db_path, decision_id, key, pid):
    attempt_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", decision_id=decision_id, request_key=key
    )
    service_history.record_worker_pid(db_path, attempt_id, pid)
    assert service_history.record_caller_abandonment(db_path, attempt_id, reason="transport_cancelled")
    return attempt_id


def test_an_unanswerable_probe_keeps_the_row_open_and_keeps_guarding_the_request(tmp_path, monkeypatch):
    """A pid probe that cannot answer -- Windows `OpenProcess` handing back no
    handle -- used to read as "dead", which closed a LIVE worker's attempt and
    released the duplicate guard with it. UNKNOWN stays UNKNOWN: the row stays
    `started` and still refuses a second worker."""

    db_path = tmp_path / "history.db"
    key = service_history.request_fingerprint("implementation", str(tmp_path), "do the thing")
    attempt_id = _abandoned_with_key(db_path, "probe-cannot-answer", key, pid=os.getpid())

    monkeypatch.setattr(procutil, "pid_state", lambda pid: "unknown")

    [reported] = service_history.reconcile_abandoned_attempts(db_path)

    assert reported["id"] == attempt_id and reported["reconciled"] is False
    assert reported["liveness"] == "unknown", "annotation may not call an unreadable probe `abandoned`"
    assert "could not be probed" in str(reported.get("reconcile_skipped"))
    row = _row(db_path, attempt_id)
    assert row["status"] == "started" and row["resolved_at"] is None
    assert row["failure_kind"] is None

    # The still-open row is still the guard: a second worker for the same
    # logical request is refused, which is what closing it used to release.
    with pytest.raises(service_history.DuplicateActiveAttempt):
        service_history.record_delegation_attempt(
            db_path, task_type="implementation", decision_id="second-worker", request_key=key
        )
    assert service_history.find_in_flight_duplicate(db_path, key) is not None

    # The age-based stale pass must not close it either.
    assert service_history.reconcile_stale_attempts(db_path) == 0
    assert _row(db_path, attempt_id)["status"] == "started"


def test_a_probe_that_answers_dead_still_closes_the_row(tmp_path, monkeypatch):
    """The other half of the tri-state: `unknown` blocking the close must not
    have cost the pass its actual job."""

    db_path = tmp_path / "history.db"
    key = service_history.request_fingerprint("implementation", str(tmp_path), "do the other thing")
    attempt_id = _abandoned_with_key(db_path, "probe-says-dead", key, pid=os.getpid())

    monkeypatch.setattr(procutil, "pid_state", lambda pid: "dead")

    [reported] = service_history.reconcile_abandoned_attempts(db_path)

    assert reported["reconciled"] is True
    row = _row(db_path, attempt_id)
    assert (row["status"], row["failure_kind"]) == ("failed", "orphaned")
    assert row["resolved_at"] is not None


@pytest.mark.skipif(os.name != "nt", reason="access-denied without a handle is the Windows probe")
def test_a_real_access_denied_pid_does_not_read_as_dead():
    """Not a monkeypatched probe: pid 4 is the Windows System process, which
    `OpenProcess` refuses even PROCESS_QUERY_LIMITED_INFORMATION on. It exists,
    so the answer may not be "dead" -- that fold is the whole of the defect."""

    assert procutil.pid_state(4) != "dead"
    assert procutil.pid_state(os.getpid()) == "alive"
    assert procutil.pid_state(_dead_pid()) == "dead"


def test_the_tri_state_probe_agrees_with_the_boolean_one_where_both_can_speak():
    assert procutil.pid_alive(os.getpid()) is True
    dead = _dead_pid()
    assert procutil.pid_state(dead) == "dead" and procutil.pid_alive(dead) is False


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
    """The classification the review reproduced: the worker started, so the row
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
    row stays a timeout and says the worker is unaccounted for."""

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


def test_a_failed_tree_walk_reaches_the_row_as_an_unaccounted_timeout(delegation):
    """End to end, on either platform: the row carries a closed census error and
    stays a timeout, never `terminated` and never a failed start."""

    delegation["holder"]["worker"] = _WorkerThatIsKilled(
        {"pid": 4242, "tree_kill": "launch_error: FileNotFoundError", "confirmed_dead": True}
    )
    _delegate(delegation)
    [row] = service_history.fetch_delegation_attempts(delegation["db_path"])
    assert (row["status"], row["failure_kind"]) == ("failed", "timeout")
    detail = str(row["detail"])
    assert "NOT confirmed dead" in detail and "unaccounted for" in detail
    assert "census_unknown" in detail
    assert "launch_error: FileNotFoundError" not in detail
    assert "terminated (" not in detail
