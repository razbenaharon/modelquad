# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The caller's departure is observed, and only that.

An earlier reproduction showed the shape:
an MCP host stops waiting for a tool result, nothing in this tree notices, the
worker runs on, and the `delegation_attempts` row sits at `started` telling
nobody anything. This file pins the fix, and just as importantly pins what the
fix is NOT allowed to do.

Two lifecycles run through one row and they are not the same lifecycle. The
CALLER waits and then leaves; the WORKER runs and then ends. The fix records
only the first: `caller_abandoned_at` / `caller_abandonment_reason` are
written the instant the transport cancels the request, and `status`,
`resolved_at` and `failure_kind` are left entirely to the delegation's own
resolution path. That is what keeps duplicate-execution protection intact --
the unique active-request index and `find_in_flight_duplicate` both key on a
row still being `started` and unresolved, and a row whose caller has gone
remains exactly that while its worker may be alive.

The acceptance test drives a REAL MCP client over the SDK's in-memory
transport and lets it genuinely time out, because a timer inside the server
would only prove that a timer fires. Honest label: validated against a real
MCP client over the in-memory transport, not against a stdio subprocess host.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import threading
import time

import anyio
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT

from conftest import tool_payload

from modelquad import procutil
from modelquad.execution import ExecutionManager
from modelquad.execution import base as execution_base
from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
from modelquad.service import db as service_db
from modelquad.service import history as service_history

def write_usage_fixtures(path):
    (path / "usage-claude.json").write_text('{"five_hour_used_percentage": 10}', encoding="utf-8")
    (path / "usage-codex.json").write_text('{"available": true, "five_hour_remaining_percentage": 80}', encoding="utf-8")


@pytest.fixture(autouse=True)
def _tool_usage_directory(tmp_path, monkeypatch):
    """The MCP tools read quota snapshots from `server.USAGE_DIRECTORY`, never
    from a tool argument. This module's snapshots live in the
    test's tmp_path, which every call here used to pass as `usage_directory`."""
    from modelquad.mcp import server as mcp_server_module

    monkeypatch.setattr(mcp_server_module, "USAGE_DIRECTORY", str(tmp_path))


mcp_server = importlib.import_module("modelquad.mcp.server")

#: The client's patience in the acceptance test. Short on purpose: the whole
#: point is a real deadline elapsing, and one that takes minutes to elapse
#: would make this file too slow to run on every change.
#:
#: It cannot be as short as it was. The clock starts when `call_tool` is
#: issued, but the worker only starts at the far end of the delegate handler
#: -- config load, provider construction, three usage files, the refusal
#: overlay, routing, the decision and history rows -- and at 1.0 s a loaded
#: hosted runner spent the whole budget before the worker was reached. The
#: proof then failed on its own step 2 with "the ordering under test was never
#: observed", which is an inconclusive run reported as a defect (observed on
#: Windows CI, run 34106231550, 2026-09-07).
#:
#: Ten seconds gives setup slack on the shared, self-hosted Windows runner:
#: every assertion is unchanged, and each still requires the deadline to fire while the
#: worker is demonstrably running. `_HoldingWorker` blocks until the test
#: releases it (up to 60 s) and the delegation's own timeout is 60 s, so
#: nothing here races the worker's end -- only its start, which is what the
#: extra slack buys.
CALLER_PATIENCE_SECONDS = 10.0


class _HoldingWorker:
    """A worker that is genuinely, checkably alive until the test lets it go.

    It reports THIS process's pid through `on_spawn`, so `procutil.pid_alive`
    on the recorded pid is a real observation rather than a fixture's promise:
    the test can assert "that worker is still running" and mean it. It then
    blocks until `release` is set and returns a COMPLETED result, so the
    delegation's own resolution path writes `completed` -- the outcome most
    likely to be corrupted by anything that treats caller departure as a
    worker verdict.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self.request = None

    def is_available(self) -> bool:
        return True

    def execute(self, request) -> ExecutionResult:
        self.calls += 1
        self.request = request
        if request.on_spawn is not None:
            request.on_spawn(os.getpid())
        self.started.set()
        self.release.wait(timeout=60)
        self.finished.set()
        return ExecutionResult(
            provider=request.provider,
            success=True,
            output="done",
            exit_code=0,
            session_id=request.session_id,
            task_status=WorkerTaskStatus.COMPLETED,
            task_status_reason="finished after the caller had gone",
        )


@pytest.fixture
def delegation(tmp_path, monkeypatch):
    """The MCP server on an isolated database, with the holding worker as `codex`."""

    write_usage_fixtures(tmp_path)
    db_path = tmp_path / "modelquad.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = service_db.DEFAULT_DB_PATH
    service_db.DEFAULT_DB_PATH = db_path

    worker = _HoldingWorker()
    real_init = ExecutionManager.__init__

    def _patched_init(self, config, **kwargs):
        kwargs.pop("executors", None)
        real_init(self, config, executors={"codex": worker}, **kwargs)

    monkeypatch.setattr(ExecutionManager, "__init__", _patched_init)

    arguments = {
        "task_type": "review",
        "prompt": "do the thing",
        "working_directory": str(workspace),
        "provider": "codex",
        "timeout": 60.0,
    }

    # One complete delegation first, against a THROWAWAY database, so the
    # acceptance test's deadline is a test of the mechanism and
    # not of Python's import machinery. The delegate path imports the router,
    # the config layer and the providers lazily on its first call; cold, that
    # can outlast a deadline chosen to be short, and the attempt row would not
    # exist yet when the caller left -- which is a different (and correct)
    # outcome, but not the one under test. Warming it here states that
    # dependency instead of hiding it behind a longer timeout.
    service_db.DEFAULT_DB_PATH = tmp_path / "warmup.db"
    worker.release.set()
    asyncio.run(mcp_server.server.call_tool("modelquad_delegate", dict(arguments, prompt="warm the path")))
    assert worker.finished.is_set(), "the warm-up delegation did not reach the worker"
    worker.release.clear()
    worker.started.clear()
    worker.finished.clear()
    worker.calls = 0
    service_db.DEFAULT_DB_PATH = db_path

    # And the REAL database gets its schema before the clock starts. Warming
    # the import path on a throwaway file leaves the file under test cold:
    # `connect_and_init` would then create every table and index inside the
    # caller's patience, a cost that has nothing to do with the mechanism and
    # everything to do with how many milliseconds SQLite needs today.
    service_db.connect_and_init(db_path).close()

    try:
        yield {
            "db_path": db_path,
            "workspace": workspace,
            "usage": str(tmp_path),
            "worker": worker,
            "arguments": arguments,
        }
    finally:
        worker.release.set()
        service_db.DEFAULT_DB_PATH = original


def _rows(delegation):
    return service_history.fetch_delegation_attempts(delegation["db_path"])


def _row(delegation, attempt_id):
    """One attempt by id. Refusing a duplicate writes a row of its own, so
    "the only row" stops being a safe way to name the attempt under test."""

    [row] = [entry for entry in _rows(delegation) if entry["id"] == attempt_id]
    return row


async def _poll(predicate, *, seconds=5.0, interval=0.02):
    """Wait for something the server side is doing; return whether it happened.

    Deliberately ``await asyncio.sleep`` rather than ``time.sleep``. The
    server handler runs as a task on THIS event loop, and the cancellation it
    has to observe is delivered there: a blocking sleep here would hold the
    loop and starve exactly the code under test, so the abandonment would
    never be recorded and the test would fail while the product was right.
    """

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ------------------------------------------------- the acceptance proof


async def _caller_leaves_and_the_worker_does_not(delegation, mode):
    """The required proof, in order, driven by a real MCP client.

    `mode` selects how the client reaches the server: `"legacy"` is the
    in-memory JSON-RPC wire path (the client's timeout sends a real
    `notifications/cancelled` and the server cancels the request scope);
    the default mode reaches the same handler through the SDK's direct
    dispatcher, where the timeout is structural cancellation of the handler
    scope. Both are ways a caller can leave, and the server must record the
    same fact either way.
    """

    worker = delegation["worker"]

    async with Client(mcp_server.server, mode=mode) as client:
        # 1. The caller begins the delegation and starts waiting on it. The
        #    request runs as a task rather than a bare `await` so that the
        #    steps below are observed WHILE the caller is still waiting: a
        #    proof that ends at the timeout cannot tell a worker that was
        #    running before the deadline from one that started after it.
        call = asyncio.ensure_future(
            client.call_tool(
                "modelquad_delegate",
                delegation["arguments"],
                read_timeout_seconds=CALLER_PATIENCE_SECONDS,
            )
        )

        try:
            # 2. The worker is genuinely running, and the caller has not yet
            #    given up on it. `call.done()` ends the wait early so the
            #    failure reads as "the deadline beat the worker" rather than
            #    as a timeout on this poll.
            assert await _poll(
                lambda: worker.started.is_set() or call.done(), seconds=10.0
            ), "the worker was never launched"
            assert worker.started.is_set(), (
                "the caller's deadline fired before the worker was even running; "
                "the ordering under test was never observed"
            )
            assert not call.done(), (
                "the caller had already stopped waiting when the worker started"
            )

            # 3. The row is open, with a live worker on it, while the caller
            #    is still there.
            rows = _rows(delegation)
            assert len(rows) == 1, f"expected exactly one attempt row, found {len(rows)}: {rows}"
            [row] = rows
            attempt_id = row["id"]
            assert row["status"] == "started"
            assert row["resolved_at"] is None
            assert row["worker_pid"] == os.getpid()
            assert procutil.pid_alive(int(row["worker_pid"]))
            assert not worker.finished.is_set()
        except BaseException:
            call.cancel()
            raise

        # 4. Only now does the caller's own deadline elapse, and the SDK says so.
        with pytest.raises(MCPError) as raised:
            await call
        assert raised.value.code == REQUEST_TIMEOUT, raised.value

        # 5. The caller's departure is durable, and named truthfully.
        assert await _poll(lambda: _rows(delegation)[0]["caller_abandoned_at"] is not None), _rows(delegation)
        [row] = _rows(delegation)
        assert row["caller_abandonment_reason"] == "transport_cancelled"
        assert row["caller_abandonment_reason"] in service_history.CALLER_ABANDONMENT_REASONS
        abandoned_at = row["caller_abandoned_at"]

        # 6. Nothing was done TO the worker, and nothing was claimed about it.
        assert not worker.finished.is_set(), "the caller leaving must not end the worker"
        assert procutil.pid_alive(os.getpid())
        assert row["status"] == "started" and row["resolved_at"] is None
        assert row["failure_kind"] is None, "no verdict about the worker is written from caller state"
        liveness = service_history.annotate_attempt_liveness(dict(row))
        assert liveness["liveness"] == "in_flight"
        assert liveness["caller_abandoned"] is True
        assert liveness["caller_waiting"] is False
        assert [entry["id"] for entry in
                service_history.fetch_caller_abandoned_open_attempts(delegation["db_path"])] == [attempt_id]

        # 7. Duplicate protection survived the caller's departure -- the whole
        #    reason the row is left open rather than resolved.
        request_key = service_history.request_fingerprint(
            "review", str(delegation["workspace"]), "do the thing"
        )
        duplicate = service_history.find_in_flight_duplicate(delegation["db_path"], request_key)
        assert duplicate is not None and duplicate["id"] == attempt_id
        refusal = tool_payload(await client.call_tool("modelquad_delegate", delegation["arguments"]))
        assert refusal["refused"] is True
        assert refusal["duplicate_of_attempt"] == attempt_id
        assert worker.calls == 1, "a second worker was started beside a live one"
        with pytest.raises(service_history.DuplicateActiveAttempt):
            service_history.record_delegation_attempt(
                delegation["db_path"],
                task_type="review",
                decision_id="whatever",
                requested_provider="codex",
                request_key=request_key,
            )

        # 8. Only now is the worker allowed to finish. Its own outcome is
        #    written by the delegation, beside the abandonment mark.
        worker.release.set()
        assert await _poll(
            lambda: _row(delegation, attempt_id)["resolved_at"] is not None, seconds=15
        ), _rows(delegation)
        row = _row(delegation, attempt_id)
        assert row["status"] == "completed", row
        assert row["caller_abandoned_at"] == abandoned_at, "the caller's departure is not rewritten"
        assert row["caller_abandonment_reason"] == "transport_cancelled"


def test_the_caller_leaves_over_the_json_rpc_transport_and_the_worker_does_not(delegation, monkeypatch):
    """The in-memory JSON-RPC wire path: a real `notifications/cancelled`."""

    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    asyncio.run(_caller_leaves_and_the_worker_does_not(delegation, "legacy"))


@pytest.mark.serial_timing
def test_the_caller_leaves_over_the_direct_dispatcher_and_the_worker_does_not(delegation, monkeypatch):
    """The same claim through the SDK's default in-process mode.

    Same handler, a different way for the caller to vanish: the request scope
    is cancelled structurally rather than in response to a wire notification.
    The recorded fact must not depend on which.
    """

    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    asyncio.run(_caller_leaves_and_the_worker_does_not(delegation, "auto"))


def test_a_caller_who_leaves_before_the_row_exists_is_still_recorded(delegation):
    """The pre-row window: the departure is known, the attempt id is not yet.

    A cancellation can arrive while `record_delegation_attempt` is still
    running. The coroutine then has nothing to mark, and before this fix the
    departure was simply lost -- the worker thread went on to create a row
    that nobody was waiting on, with `caller_abandoned_at` NULL forever.

    Driven without a transport on purpose: the race is a matter of
    microseconds and cannot be provoked reliably from outside, so the state
    it leaves behind -- a handle carrying the reason and no id -- is
    constructed directly and the delegation body is run against it. That is
    the whole of what the coroutine hands over.
    """

    worker = delegation["worker"]
    handle = execution_base.CallerWaitHandle(abandoned_reason="transport_cancelled")
    payload: dict = {}

    def run_the_delegation():
        payload["result"] = mcp_server._delegate_payload(
            **delegation["arguments"], caller_wait=handle
        )

    thread = threading.Thread(target=run_the_delegation, daemon=True)
    thread.start()
    try:
        assert worker.started.wait(timeout=30), "the delegation never reached the worker"

        # The row exists, it is marked, and it is still open -- the caller's
        # departure is not a verdict about the worker holding the thread.
        rows = _rows(delegation)
        assert len(rows) == 1, f"expected exactly one attempt row, found {len(rows)}: {rows}"
        [row] = rows
        assert row["caller_abandoned_at"] is not None, row
        assert row["caller_abandonment_reason"] == "transport_cancelled"
        assert row["status"] == "started" and row["resolved_at"] is None
        assert handle.attempt_id == row["id"], "the delegation published the id it wrote"
    finally:
        worker.release.set()
        thread.join(timeout=60)

    assert not thread.is_alive(), "the delegation did not finish"
    assert payload["result"]["success"] is True, payload["result"]
    row = _row(delegation, handle.attempt_id)
    assert row["status"] == "completed", row
    assert row["caller_abandonment_reason"] == "transport_cancelled"


# ------------------------------------------------- UNKNOWN stays UNKNOWN


async def _a_delegation_nobody_abandons(delegation):
    worker = delegation["worker"]
    worker.release.set()
    async with Client(mcp_server.server, mode="legacy") as client:
        payload = tool_payload(await client.call_tool("modelquad_delegate", delegation["arguments"]))
    assert payload["success"] is True, payload
    [row] = _rows(delegation)
    assert row["status"] == "completed"
    assert row["failure_kind"] is None, "no failure kind is fabricated from a caller budget"
    assert row["caller_abandoned_at"] is None
    assert row["caller_abandonment_reason"] is None


@pytest.mark.parametrize("budget", [None, "500"])
def test_the_callers_budget_is_never_guessed_from_the_environment(delegation, monkeypatch, budget):
    """`MCP_TOOL_TIMEOUT` is a number in the SERVER's environment, absent or not.

    It is the host's own setting for the host's own patience, and this process
    is not the host. Part 2 reads it nowhere: a caller who does not leave is
    not recorded as having left, whether that variable is unset or set to half
    a second -- which under the withdrawn timer design would have fired.
    """

    if budget is None:
        monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("MCP_TOOL_TIMEOUT", budget)
    asyncio.run(_a_delegation_nobody_abandons(delegation))


def test_no_caller_budget_is_read_anywhere_on_the_delegate_path():
    """The guard on the design, not on one run of it.

    The withdrawn candidate derived the caller's deadline from the server's
    environment and fired a timer at it. Deleting the timer is not enough --
    the next reader of this file must not put it back -- so the absence of
    both the helper and the variable is asserted directly.
    """

    assert not hasattr(execution_base, "caller_budget_seconds")
    assert not hasattr(execution_base, "CALLER_DEADLINE_SLACK_SECONDS")
    assert not hasattr(execution_base, "HOST_TOOL_CALL_BUDGET_ENV")
    source = "".join(
        inspect.getsource(function)
        for function in (
            mcp_server._delegate_observing_the_caller,
            mcp_server._delegate_payload,
            mcp_server._delegate_after_policy,
            getattr(mcp_server.modelquad_delegate, "fn", mcp_server.modelquad_delegate),
        )
    )
    assert "MCP_TOOL_TIMEOUT" not in source
    # And "caller_timeout" is not a failure kind: the caller's departure is
    # not a worker outcome and was withdrawn from that vocabulary.
    assert "caller_timeout" not in service_history.DELEGATION_FAILURE_KINDS
    assert "abandoned" not in service_history.DELEGATION_ATTEMPT_STATUSES


def test_delegate_tool_is_awaitable_so_the_caller_can_be_observed_at_all():
    """A sync tool body cannot see its caller leave; this is why they are async.

    The SDK dispatches a sync body with `anyio.to_thread.run_sync` at its
    `abandon_on_cancel=False` default, which defers the cancellation until the
    body returns. The delegation tool must therefore stay a coroutine function.
    """

    for name in ("modelquad_delegate",):
        tool = getattr(mcp_server, name)
        assert inspect.iscoroutinefunction(getattr(tool, "fn", tool)), name


# ------------------------------------------------- the state, unit by unit


def _open_attempt(db, **kwargs):
    return service_history.record_delegation_attempt(
        db, task_type="implementation", decision_id="d", requested_provider="codex", **kwargs
    )


def test_an_abandonment_reason_outside_the_vocabulary_is_refused(tmp_path):
    db = tmp_path / "h.db"
    attempt = _open_attempt(db)
    with pytest.raises(ValueError):
        service_history.record_caller_abandonment(db, attempt, reason="bored")
    [row] = service_history.fetch_delegation_attempts(db)
    assert row["caller_abandoned_at"] is None
    assert service_history.record_caller_abandonment(db, None, reason="transport_closed") is False


def test_the_first_departure_is_the_one_that_happened(tmp_path):
    """Write-once and idempotent: a repeated observation moves nothing."""

    db = tmp_path / "h.db"
    attempt = _open_attempt(db)
    assert service_history.record_caller_abandonment(db, attempt, reason="transport_cancelled") is True
    [first] = service_history.fetch_delegation_attempts(db)
    assert first["caller_abandoned_at"] is not None

    assert service_history.record_caller_abandonment(db, attempt, reason="transport_closed") is False
    [second] = service_history.fetch_delegation_attempts(db)
    assert second["caller_abandoned_at"] == first["caller_abandoned_at"]
    assert second["caller_abandonment_reason"] == "transport_cancelled"


def test_recording_the_caller_leaving_touches_nothing_else_on_the_row(tmp_path):
    """The row stays open, so the duplicate guard keeps refusing a second worker."""

    db = tmp_path / "h.db"
    key = service_history.request_fingerprint("implementation", str(tmp_path), "p")
    attempt = _open_attempt(db, detail="opened", request_key=key)
    service_history.record_worker_pid(db, attempt, os.getpid())
    service_history.record_caller_abandonment(db, attempt, reason="transport_cancelled")

    [row] = service_history.fetch_delegation_attempts(db)
    assert (row["status"], row["resolved_at"], row["failure_kind"]) == ("started", None, None)
    assert row["detail"] == "opened"
    assert service_history.find_in_flight_duplicate(db, key)["id"] == attempt
    with pytest.raises(service_history.DuplicateActiveAttempt):
        _open_attempt(db, request_key=key)


def test_the_worker_still_resolves_its_own_row_afterwards(tmp_path):
    """Part 3 will terminate workers; part 2 leaves that path entirely alone."""

    db = tmp_path / "h.db"
    attempt = _open_attempt(db)
    service_history.record_worker_pid(db, attempt, os.getpid())
    service_history.record_caller_abandonment(db, attempt, reason="transport_cancelled")
    service_history.resolve_delegation_attempt(
        db, attempt, status="completed", routed_provider="codex", outcome="completed"
    )
    [row] = service_history.fetch_delegation_attempts(db)
    assert row["status"] == "completed" and row["resolved_at"] is not None
    assert row["caller_abandoned_at"] is not None
    assert row["caller_abandonment_reason"] == "transport_cancelled"
    liveness = service_history.annotate_attempt_liveness(dict(row))
    assert liveness["caller_abandoned"] is True and liveness["caller_waiting"] is False
    # Resolved rows leave the part-3 queue.
    assert service_history.fetch_caller_abandoned_open_attempts(db) == []


def test_a_waiting_caller_is_told_apart_from_a_departed_one(tmp_path):
    db = tmp_path / "h.db"
    attempt = _open_attempt(db)
    [row] = service_history.fetch_delegation_attempts(db)
    liveness = service_history.annotate_attempt_liveness(dict(row))
    assert liveness["caller_waiting"] is True and liveness["caller_abandoned"] is False
    assert service_history.fetch_caller_abandoned_open_attempts(db) == []
    service_history.record_caller_abandonment(db, attempt, reason="transport_closed")
    [entry] = service_history.fetch_caller_abandoned_open_attempts(db)
    assert entry["id"] == attempt and entry["caller_waiting"] is False


# ------------------------------------------------- the schema step


# ------------------------------------------------- the boundary itself


def test_the_boundary_shields_its_write_and_re_raises_the_cancellation():
    """Both are load-bearing, and neither is visible in a passing happy path.

    Unshielded, the SQLite write would be cancelled at its first checkpoint
    inside the already-cancelled scope and record nothing. Swallowed, the
    dispatcher would be handed a result for a request it has already settled.
    """

    written: list[tuple] = []

    async def scenario():
        async def call_that_outlives_its_caller():
            handle = execution_base.CallerWaitHandle()
            handle.publish("db", 7)

            def blocking():
                time.sleep(1.0)
                return "too late"

            return await mcp_server._delegate_observing_the_caller(blocking, handle)

        with anyio.move_on_after(0.2) as scope:
            await call_that_outlives_its_caller()
        assert scope.cancelled_caught, "the cancellation must propagate out of the helper"

    original = service_history.record_caller_abandonment
    try:
        service_history.record_caller_abandonment = lambda db, attempt, *, reason: (
            written.append((db, attempt, reason)) or True
        )
        asyncio.run(scenario())
    finally:
        service_history.record_caller_abandonment = original

    assert written == [("db", 7, "transport_cancelled")]


# ------------------------------------------------- the identity is one value


def test_publishing_the_identity_is_a_single_indivisible_write():
    """There is no state in which one half of the identity is set.

    The publishing thread and the reading coroutine are different threads,
    and the reader arrives at an arbitrary instant. Published as two
    attribute writes, `attempt_id` then `db_path`, a reader landing between
    them saw an id with no database and handed `None` to the recorder, which
    raised `TypeError` inside the shielded scope and destroyed the
    cancellation. One tuple assignment is atomic under the GIL, so the only
    two observable states are "nothing" and "both".
    """

    handle = execution_base.CallerWaitHandle()
    assert handle.identity is None
    assert (handle.db_path, handle.attempt_id) == (None, None)

    handle.publish("db", 7)
    assert handle.identity == ("db", 7)
    assert (handle.db_path, handle.attempt_id) == ("db", 7)

    # The halves are derived views, not fields: half an identity cannot be
    # written even by a future caller who tries.
    with pytest.raises(AttributeError):
        handle.identity = ("another-db", 8)
    for half in ("db_path", "attempt_id"):
        with pytest.raises(AttributeError):
            setattr(handle, half, None)

    # And the delegation publishes through that one call, nowhere else.
    source = inspect.getsource(mcp_server._delegate_payload)
    assert "caller_wait.publish(db_path, attempt_id)" in source
    assert "caller_wait.attempt_id =" not in source
    assert "caller_wait.db_path =" not in source


def _what_the_boundary_raises(handle, *, hold=0.6, patience=0.2):
    """Cancel a caller waiting on the boundary; return what the helper raised.

    Returns ``(exception, cancelled_exception_class)``. The class is read
    inside the loop because `anyio.get_cancelled_exc_class` needs a running
    backend to answer, and the assertions happen after `asyncio.run` returns.

    No transport: the interleaving under test is a matter of microseconds and
    cannot be provoked from outside, so the state it leaves behind is built
    directly and the boundary is driven against it. The blocking call never
    returns within the caller's patience, which is the only way the
    cancellation path is reached at all.
    """

    raised: list[BaseException] = []
    cancelled_class: list[type[BaseException]] = []

    async def scenario():
        cancelled_class.append(anyio.get_cancelled_exc_class())

        async def call_that_outlives_its_caller():
            def blocking():
                time.sleep(hold)
                return "too late"

            try:
                return await mcp_server._delegate_observing_the_caller(blocking, handle)
            except BaseException as exc:  # noqa: BLE001 - the point is which one
                raised.append(exc)
                raise

        with anyio.move_on_after(patience) as scope:
            await call_that_outlives_its_caller()
        assert scope.cancelled_caught, "the cancellation must propagate out of the helper"

    asyncio.run(scenario())
    [exc] = raised
    [cancelled] = cancelled_class
    return exc, cancelled


def test_a_cancellation_before_the_row_is_published_stays_a_cancellation():
    """The pre-row window, from the coroutine's side: nothing to mark, nothing to raise.

    The handle carries no identity yet. The boundary must stamp the reason,
    leave the database alone -- there is no row to name -- and re-raise the
    cancellation unchanged. Calling the recorder with half an identity is
    what used to turn this into `TypeError`.
    """

    handle = execution_base.CallerWaitHandle()
    written: list[tuple] = []
    original = service_history.record_caller_abandonment
    try:
        service_history.record_caller_abandonment = lambda db, attempt, *, reason: (
            written.append((db, attempt, reason)) or True
        )
        exc, cancelled = _what_the_boundary_raises(handle)
    finally:
        service_history.record_caller_abandonment = original

    assert isinstance(exc, cancelled), exc
    assert not isinstance(exc, TypeError)
    assert written == [], "an unpublished handle names no row, so nothing is written"
    # The reason is still stamped, which is what lets `_delegate_payload`
    # write the mark the instant it publishes.
    assert handle.abandoned_reason == "transport_cancelled"


def test_a_cancellation_after_the_row_is_published_marks_it_and_still_cancels(tmp_path):
    """The published case, against a real row: the mark lands, the cancellation survives."""

    db = tmp_path / "h.db"
    attempt = _open_attempt(db)
    handle = execution_base.CallerWaitHandle()
    handle.publish(db, attempt)

    exc, cancelled = _what_the_boundary_raises(handle)
    assert isinstance(exc, cancelled), exc

    [row] = service_history.fetch_delegation_attempts(db)
    assert row["caller_abandoned_at"] is not None, row
    assert row["caller_abandonment_reason"] == "transport_cancelled"
    # And still no verdict about the worker on the abandoned thread.
    assert row["status"] == "started" and row["resolved_at"] is None
    assert row["failure_kind"] is None


def test_a_recording_failure_never_replaces_the_cancellation():
    """A shielded write that goes wrong is logged; the cancellation is what propagates.

    Whatever the recorder does -- a driver error, a signature that stopped
    matching -- the caller has already gone and cannot be told about it. The
    runtime must still see a cancelled request, not a failed one.
    """

    handle = execution_base.CallerWaitHandle()
    handle.publish("db", 7)

    def boom(db, attempt, *, reason):
        raise TypeError("expected str, bytes or os.PathLike object, not NoneType")

    original = service_history.record_caller_abandonment
    try:
        service_history.record_caller_abandonment = boom
        exc, cancelled = _what_the_boundary_raises(handle)
    finally:
        service_history.record_caller_abandonment = original

    assert isinstance(exc, cancelled), exc
    assert not isinstance(exc, TypeError)
