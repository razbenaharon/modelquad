# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A caller that stopped waiting can find out what actually happened.

ModelQuad already records the truth internally -- the caller's departure,
the worker running on past it, and the terminal outcome it writes when it ends
(`tests/test_rel1_caller_abandonment.py` proves all three). What was missing was
the way back IN: the calling host's patience is the host's own setting, ModelQuad
never reads it, and when it runs out the tool call fails for the caller carrying
no attempt id, no state and no pointer to the record that already exists. From
where the caller stood, "I stopped waiting" and "the worker failed" were the same
event, and the only way to find out was to look at the workspace -- which is
inferring success from files, exactly what the Issue forbids.

So this file pins the READ. `modelquad_delegation_status` answers with the
lifecycle `mcp/livelifecycle.py` already derives, keyed by an identity that
survives the caller's departure: the request fingerprint over the same
task_type / working_directory / prompt the caller delegated with, which it still
holds precisely because its own arguments never went anywhere.

Nothing here waits for the host's real ceiling. The caller's patience in the
end-to-end proof is seconds, the worker is released by the test, and every other
assertion is made against rows written directly.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from conftest import tool_payload

from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
from modelquad.mcp import livelifecycle
from modelquad.mcp.livelifecycle import (
    LIVE_STATES,
    LiveFacts,
    derive_recovery_outlook,
)
from modelquad.service import db as service_db
from modelquad.service import history as service_history

# The fixture, the holding worker and the polling helper are part 2's, reused
# deliberately: this is the same lifecycle observed from the caller's side, and
# a second copy of the harness would be free to drift away from the behaviour
# it is supposed to be describing.
from test_rel1_caller_abandonment import (  # noqa: F401 -- `delegation` is a fixture
    CALLER_PATIENCE_SECONDS,
    _poll,
    _rows,
    delegation,
)

mcp_server = importlib.import_module("modelquad.mcp.server")


def _tool(name):
    """The undecorated tool body, for calls that are meant to be refused."""

    tool = getattr(mcp_server, name)
    return getattr(tool, "fn", tool)


# ------------------------------------------------- the outlook is pure


def test_every_lifecycle_state_can_be_explained_to_a_caller():
    """No state may reach a caller without words for what to do about it.

    The fallback in `derive_recovery_outlook` is the unknown text, and a state
    that quietly fell through to it would tell a caller its completed work was
    unestablished. Pinning coverage of the vocabulary keeps the next state
    added to `LIVE_STATES` from doing that silently.
    """

    assert set(livelifecycle._GUIDANCE) == set(LIVE_STATES)


def test_a_caller_who_left_a_running_worker_is_not_told_the_worker_failed():
    """Requirement A, at the point where the words are chosen.

    The caller has gone, the worker is observed in flight, and the row carries
    no verdict at all. `unattended` is the only honest answer; `failed` here
    would be attributing the caller's own departure to the worker.
    """

    outlook = derive_recovery_outlook(
        LiveFacts(
            decision_exists=True,
            attempt_exists=True,
            attempt_status="started",
            attempt_resolved=False,
            worker_pid=os.getpid(),
            worker_alive=True,
            liveness="in_flight",
            caller_abandoned=True,
        )
    )
    assert outlook.state == "unattended"
    assert outlook.reason == "caller_abandoned_worker_in_flight"
    assert outlook.terminal is False
    assert outlook.worker_may_be_running is True
    assert outlook.safe_to_retry is False
    assert "still running" in outlook.guidance
    assert "fail" not in outlook.guidance.lower()


def test_an_unestablished_fate_is_never_rounded_into_permission_to_retry():
    """Unknown stays unknown, and unknown is not a yes.

    `safe_to_retry` is tri-state for exactly this row: a worker whose fate the
    process table would not report is the one case where starting a second
    worker beside a live one is possible, so it may never read True.
    """

    outlook = derive_recovery_outlook(
        LiveFacts(
            attempt_exists=True,
            attempt_status="started",
            attempt_resolved=False,
            worker_pid=4242,
            worker_alive=None,
            liveness="unknown",
            caller_abandoned=True,
        )
    )
    assert outlook.state == "unknown"
    assert outlook.safe_to_retry is None
    assert outlook.safe_to_retry is not True


@pytest.mark.parametrize(
    ("task_status", "failure_kind", "expected"),
    [
        ("completed", None, "completed"),
        ("failed", None, "failed"),
        (None, "terminated", "stopped"),
    ],
)
def test_a_finished_attempt_reports_its_own_ending_and_releases_the_retry(
    task_status, failure_kind, expected
):
    """A worker that ended is terminal, and nothing of it can still be running.

    The three endings are the worker's own (`failed`), a successful one
    (`completed`), and one ModelQuad performed and confirmed (`stopped`). All
    three release the duplicate guard, and none of them is the caller's
    departure -- which the row still carries beside them.
    """

    outlook = derive_recovery_outlook(
        LiveFacts(
            attempt_exists=True,
            attempt_status="completed",
            attempt_resolved=True,
            caller_abandoned=True,
            failure_kind=failure_kind,
            task_status=task_status,
        )
    )
    assert outlook.state == expected
    assert outlook.terminal is True
    assert outlook.worker_may_be_running is False
    assert outlook.safe_to_retry is True


# ------------------------------------------------- the identity that survives


@pytest.fixture
def store(tmp_path):
    """An isolated history database with the schema already created."""

    db_path = tmp_path / "modelquad.db"
    service_db.connect_and_init(db_path).close()
    return db_path


def _an_abandoned_attempt(db_path, workspace, *, prompt="do the thing", decision_id="dec-115"):
    request_key = service_history.request_fingerprint("implementation", str(workspace), prompt)
    attempt_id = service_history.record_delegation_attempt(
        db_path,
        task_type="implementation",
        decision_id=decision_id,
        requested_provider="codex",
        request_key=request_key,
    )
    service_history.record_caller_abandonment(db_path, attempt_id, reason="transport_cancelled")
    return attempt_id, request_key


def test_the_arguments_the_caller_still_holds_find_the_attempt_it_lost(store, tmp_path):
    """The whole point: no id was returned, and the row is found anyway.

    The caller's own three arguments never travelled anywhere, so they survive
    a transport that dropped everything else. `request_fingerprint` is the same
    function the delegation path wrote the row under, so the identity is not a
    second one -- it is the one already there.
    """

    attempt_id, _ = _an_abandoned_attempt(store, tmp_path)

    found = service_history.lookup_delegation_attempts(
        store,
        request_key=service_history.request_fingerprint(
            "implementation", str(tmp_path), "do the thing"
        ),
    )
    assert [entry["id"] for entry in found] == [attempt_id]
    assert found[0]["caller_abandoned"] is True
    assert found[0]["caller_abandonment_reason"] == "transport_cancelled"
    assert found[0]["unresolved"] is True
    # And by the other two identities, which a caller may also hold.
    assert [
        entry["id"]
        for entry in service_history.lookup_delegation_attempts(store, attempt_id=attempt_id)
    ] == [attempt_id]
    assert [
        entry["id"]
        for entry in service_history.lookup_delegation_attempts(store, decision_id="dec-115")
    ] == [attempt_id]


def test_a_lookup_with_no_identity_finds_nothing_rather_than_everything(store, tmp_path):
    """Handing back the newest delegation to a caller who named none would
    invite it to read another request's outcome as its own."""

    _an_abandoned_attempt(store, tmp_path)
    assert service_history.lookup_delegation_attempts(store) == []


def test_one_requests_attempt_is_never_returned_for_another(store, tmp_path):
    """Different prompt, different fingerprint, different row."""

    mine, _ = _an_abandoned_attempt(store, tmp_path, prompt="do the thing", decision_id="mine")
    theirs, _ = _an_abandoned_attempt(
        store, tmp_path, prompt="do a different thing", decision_id="theirs"
    )
    assert mine != theirs
    found = service_history.lookup_delegation_attempts(
        store,
        request_key=service_history.request_fingerprint(
            "implementation", str(tmp_path), "do the thing"
        ),
    )
    assert [entry["id"] for entry in found] == [mine]


def test_the_workers_terminal_result_is_recoverable_after_the_caller_has_gone(store, tmp_path):
    """Requirement C: the report, not a reconstruction of it from the workspace.

    The worker finishes long after the caller left. Its outcome lands on the
    attempt row and its record lands in execution history under the same
    decision id, and the lookup joins the two -- so the caller reads the exit
    code, the worker's own status and its reason, and is handed the id to ask
    for the full record with.
    """

    attempt_id, request_key = _an_abandoned_attempt(store, tmp_path)
    service_history.resolve_delegation_attempt(
        store,
        attempt_id,
        status="completed",
        routed_provider="codex",
        outcome="completed",
        duration_seconds=1849.031,
    )
    service_history.record_execution(
        store,
        "implementation",
        ExecutionResult(
            provider="codex",
            success=True,
            output="the report the caller never received",
            exit_code=0,
            session_id=None,
            task_status=WorkerTaskStatus.COMPLETED,
            task_status_reason="finished after the caller had gone",
        ),
        duration_seconds=1849.031,
        source="mcp",
        decision_id="dec-115",
    )

    [entry] = service_history.lookup_delegation_attempts(store, request_key=request_key)
    assert entry["status"] == "completed"
    assert entry["caller_abandoned"] is True, "the departure is not rewritten by the outcome"
    execution = entry["execution"]
    assert execution is not None
    assert execution["exit_code"] == 0
    assert execution["task_status"] == "completed"
    assert execution["task_status_reason"] == "finished after the caller had gone"
    assert execution["duration_seconds"] == pytest.approx(1849.031)
    assert isinstance(execution["id"], int)
    # Compact by the same rule the activity feed follows: the megabyte of
    # worker output is NOT in this answer, it is behind the id above.
    assert "metadata_json" not in execution


def test_an_identity_that_matches_nothing_is_an_empty_answer_not_a_failure(store):
    assert service_history.lookup_delegation_attempts(store, attempt_id=9999) == []
    assert service_history.lookup_delegation_attempts(store, decision_id="never-existed") == []


# ------------------------------------------------- through the MCP tool


async def _status(**kwargs):
    return tool_payload(
        await mcp_server.server.call_tool("modelquad_delegation_status", kwargs)
    )


def test_the_tool_refuses_a_half_named_request():
    """Two thirds of a fingerprint is not an identity, and guessing the third
    would answer about a request nobody asked about."""

    # The tool boundary answers with one stable public message; the reason
    # stays on the chained domain error, never in the text the model reads.
    with pytest.raises(ToolError, match="could not be accepted") as raised:
        _tool("modelquad_delegation_status")(
            task_type="implementation", working_directory="somewhere"
        )
    assert "together" in str(raised.value.__cause__)
    assert "somewhere" not in str(raised.value)


def test_the_tool_needs_some_identity_at_all():
    with pytest.raises(ToolError, match="could not be accepted") as raised:
        _tool("modelquad_delegation_status")()
    assert "name the delegation" in str(raised.value.__cause__)


async def _the_caller_leaves_and_then_asks_what_happened(delegation):
    """The end-to-end proof, in the order a real caller lives it.

    Nothing here waits for the calling host's real ceiling: the client's
    patience is `CALLER_PATIENCE_SECONDS`, and the worker is held by the test
    until it is released. What is reproduced is the ORDERING -- caller gone
    first, worker terminal second -- which is the only part of the original
    live observation that matters.
    """

    worker = delegation["worker"]
    arguments = delegation["arguments"]
    identity = {
        "task_type": arguments["task_type"],
        "working_directory": arguments["working_directory"],
        "prompt": arguments["prompt"],
    }

    async with Client(mcp_server.server, mode="legacy") as client:
        call = asyncio.ensure_future(
            client.call_tool(
                # Setup needs slack on the shared, self-hosted Windows runner;
                # the assertions below still require a running worker before timeout.
                "modelquad_delegate", arguments, read_timeout_seconds=CALLER_PATIENCE_SECONDS
            )
        )
        try:
            assert await _poll(lambda: worker.started.is_set() or call.done(), seconds=15.0)
            assert worker.started.is_set(), (
                "the caller's deadline fired before the worker was even running; "
                "the ordering under test was never observed"
            )
            assert not call.done()

            # Moment 1 -- the caller is still waiting, and is told so.
            waiting = await _status(**identity)
            assert waiting["found"] is True
            [entry] = waiting["attempts"]
            attempt_id = entry["attempt_id"]
            assert entry["state"] == "running"
            assert entry["caller_abandoned"] is False
            assert entry["caller_waiting"] is True
            assert entry["safe_to_retry"] is False
            assert entry["result"] is None, "no result is invented while the worker runs"
        except BaseException:
            call.cancel()
            raise

        # Moment 2 -- the caller's own patience runs out. This is the event the
        # Issue was filed about, and the ONLY thing it establishes is that the
        # caller left.
        with pytest.raises(MCPError):
            await call
        assert await _poll(lambda: _rows(delegation)[0]["caller_abandoned_at"] is not None)

        abandoned = await _status(**identity)
        [entry] = abandoned["attempts"]
        assert entry["attempt_id"] == attempt_id, "the same attempt, found without an id"
        assert entry["state"] == "unattended"
        assert entry["state"] != "failed", "the caller leaving is not the worker failing"
        assert entry["caller_abandoned"] is True
        assert entry["caller_abandonment_reason"] == "transport_cancelled"
        assert entry["caller_waiting"] is False
        assert entry["terminal"] is False
        assert entry["worker_may_be_running"] is True
        assert entry["safe_to_retry"] is False, "a live worker must not be duplicated"
        assert entry["result"] is None
        assert not worker.finished.is_set(), "asking must not have ended the worker"

        # The two clocks are stated in the answer itself, not only in a docstring.
        assert "worker" in abandoned["clocks"]["worker_budget"]
        assert "HOST" in abandoned["clocks"]["caller_patience"]

        # Moment 3 -- the worker reaches its own terminal state, alone.
        worker.release.set()
        assert await _poll(
            lambda: _rows(delegation)[0]["resolved_at"] is not None, seconds=15
        ), _rows(delegation)

        recovered = await _status(**identity)
        [entry] = recovered["attempts"]
        assert entry["attempt_id"] == attempt_id
        assert entry["state"] == "completed"
        assert entry["terminal"] is True
        assert entry["worker_may_be_running"] is False
        assert entry["safe_to_retry"] is True
        assert entry["caller_abandoned"] is True, "the departure survives the outcome"
        result = entry["result"]
        assert result is not None, "the report the caller never received is recoverable"
        assert result["task_status"] == "completed"
        assert result["task_status_reason"] == "worker_reported"
        # Notes-framed worker prose stays out of persisted recovery records.
        with service_db.connect(delegation["db_path"]) as conn:
            assert "finished after the caller had gone" not in "\n".join(conn.iterdump())
        assert result["exit_code"] == 0
        assert entry["result_detail"] == {"kind": "execution", "id": result["id"]}

        # And the pointer it hands back really is the full record.
        detail = tool_payload(
            await client.call_tool(
                "modelquad_activity_detail", {"kind": "execution", "id": result["id"]}
            )
        )
        assert detail["id"] == result["id"]
        assert "finished after the caller had gone" not in repr(detail)


def test_a_caller_that_stopped_waiting_recovers_the_attempt_and_then_its_result(
    delegation, monkeypatch
):
    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    asyncio.run(_the_caller_leaves_and_then_asks_what_happened(delegation))


async def _an_ordinary_short_delegation(delegation):
    """Requirement: normal delegations are unchanged, and answerable too."""

    worker = delegation["worker"]
    worker.release.set()
    arguments = delegation["arguments"]
    async with Client(mcp_server.server, mode="legacy") as client:
        payload = tool_payload(await client.call_tool("modelquad_delegate", arguments))
    assert payload["success"] is True, payload

    answer = await _status(
        task_type=arguments["task_type"],
        working_directory=arguments["working_directory"],
        prompt=arguments["prompt"],
    )
    [entry] = answer["attempts"]
    assert entry["state"] == "completed"
    assert entry["caller_abandoned"] is False, "nobody left, and nothing says they did"
    assert entry["caller_abandonment_reason"] is None
    assert entry["safe_to_retry"] is True
    assert entry["result"]["task_status"] == "completed"


def test_an_ordinary_delegation_is_unchanged_and_still_answerable(delegation):
    asyncio.run(_an_ordinary_short_delegation(delegation))


# ------------------------------------------------- the contract, and this file


def test_the_delegate_contract_says_the_two_clocks_are_not_one():
    """Requirement B, asserted on the text a caller actually reads.

    A worker budget read as a promise the synchronous call could keep is what
    produced the original defect. The release delegate description is two plain
    lines, so the recovery tool carries the contract: whose clock is whose and
    where the result goes when the caller's runs out first -- and neither may
    name the host's own setting, which ModelQuad still does not read.
    """

    recovery = inspect.getdoc(_tool("modelquad_delegation_status")) or ""
    assert "worker" in recovery.lower() and "budget" in recovery.lower()
    assert "patience" in recovery.lower()
    assert "modelquad_delegate(timeout=" in recovery
    for name in ("modelquad_delegate", "modelquad_delegation_status"):
        assert "MCP_TOOL_TIMEOUT" not in (inspect.getdoc(_tool(name)) or "")


def test_nothing_here_waits_for_the_hosts_ceiling():
    """The Issue's own test requirement, pinned rather than promised.

    The original observation took half an hour to make live. Reproducing the
    ORDERING takes seconds, and a file that quietly started waiting for a real
    host ceiling would stop being run at all.
    """

    assert CALLER_PATIENCE_SECONDS <= 10.0


@pytest.mark.parametrize("pid", [None, 0])
def test_an_age_based_liveness_reading_is_never_read_as_a_live_worker(pid):
    """The trap under the pid gate in `_recovery_view`, pinned rather than argued.

    `annotate_attempt_liveness` derives `liveness` from the row's AGE when the
    row carries no usable worker pid, and a young row reads `in_flight` there.
    That reading is not a probe, so it may never reach `worker_alive` -- rule
    "Running requires proven liveness". Tightening the gate to `is not None`
    (which looks like a strictness improvement, and was proposed as one in
    review) admits a recorded pid of 0 and turns exactly that age-based guess
    into a claimed live worker.
    """

    view = mcp_server._recovery_view(
        {
            "id": 1,
            "decision_id": None,
            "task_type": "implementation",
            "status": "started",
            "resolved_at": None,
            "worker_pid": pid,
            "liveness": "in_flight",
            "caller_abandoned": False,
            "caller_waiting": True,
        }
    )
    assert view["state"] == "delegating", "an unproven worker is a handoff, not a run"
    assert view["state"] != "running"
    assert view["safe_to_retry"] is False
