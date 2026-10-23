# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Every state the recent-activity surface shows must come from evidence.

BETA_MASTER_PLAN 1, property 3: for any decision the product made, a normal
user can see what ran where, on which host, and why -- without a developer
console. The evidence chain that answers it already exists (DecisionTrace
v11, delegation_attempts v12, the frozen host snapshot); this file pins down
the reading of it, including the readings that are uncomfortable:

- a delegation attempt that started and never came back;
- a worker that exited cleanly without ever reporting a result;
- a host that could not be identified at all.

Each of those must render as itself, never rounded up and never filled in.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.mcp.presentation import (
    ACTIVITY_STATES,
    activity_state,
    activity_state_from_attempt,
    activity_state_from_execution,
    build_activity_entry,
    build_activity_feed,
)
from orchestrator.service import db as service_db
from orchestrator.service import history as service_history

NOW = "2026-08-27T12:00:00+00:00"


def execution(**overrides):
    row = {
        "kind": "execution",
        "id": 1,
        "created_at": NOW,
        "task_type": "implementation",
        "provider": "codex",
        "agent_runtime": None,
        "host": "claude_code",
        "success": True,
        "exit_code": 0,
        "result": "success",
        "task_status": "completed",
        "fallback": False,
        "fallback_from": None,
        "duration_seconds": None,
        "decision_id": None,
        "outcome_verified": None,
        "metadata": {},
    }
    row.update(overrides)
    return row


def routing(**overrides):
    row = {
        "kind": "routing",
        "id": 2,
        "created_at": NOW,
        "task_type": "implementation",
        "recommendation": "codex",
        "selected_provider": "codex",
        "overridden": False,
        "override_reason": None,
        "host": "claude_code",
        "decision_source": "deterministic",
        "decision_id": None,
    }
    row.update(overrides)
    return row


def attempt(**overrides):
    row = {
        "kind": "attempt",
        "id": 3,
        "created_at": NOW,
        "task_type": "implementation",
        "requested_provider": "codex",
        "routed_provider": None,
        "status": "started",
        "resolved_at": None,
        "liveness": "in_flight",
        "failure_kind": None,
        "host": "claude_code",
        "duration_seconds": None,
        "decision_id": None,
    }
    row.update(overrides)
    return row


# -- the state vocabulary ---------------------------------------------------


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (execution(task_status="completed"), "completed"),
        (execution(task_status="partial"), "partial"),
        (execution(task_status="blocked"), "blocked"),
        (execution(task_status="failed"), "failed"),
        (execution(task_status="completed", result="timeout"), "timeout"),
        (execution(task_status=None), "no_result"),
        (execution(task_status="unknown"), "no_result"),
        (routing(), "selected"),
        (routing(recommendation=None, selected_provider=None), "not_routed"),
        # A recommendation beside nothing selected is advice for a delegation
        # that reached no executor, not a choice.
        (routing(recommendation="claude", selected_provider=None), "not_routed"),
        # No worker pid recorded, so nothing PROVES a worker is executing.
        (attempt(liveness="in_flight"), "delegating"),
        (attempt(liveness="abandoned"), "no_response"),
        (attempt(liveness="unknown"), "unknown"),
        (attempt(status="refused", resolved_at=NOW, liveness="resolved"), "refused"),
        # The caller left, the worker did not.
        (attempt(liveness="in_flight", caller_abandoned=True), "unattended"),
        # ...and was then stopped, with the ending read from the process table.
        (
            attempt(
                status="failed",
                resolved_at=NOW,
                liveness="resolved",
                caller_abandoned=True,
                failure_kind="terminated",
                outcome="failed",
            ),
            "stopped",
        ),
    ],
)
def test_every_state_is_derived_from_a_recorded_row(row, expected):
    assert activity_state(row) == expected
    assert expected in ACTIVITY_STATES


def test_the_feed_can_only_ever_speak_the_declared_vocabulary():
    rows = [execution(task_status=status) for status in (None, "completed", "partial", "blocked", "failed")]
    rows += [routing(), routing(recommendation=None, selected_provider=None)]
    rows += [attempt(liveness=live) for live in ("in_flight", "abandoned", "unknown")]
    rows += [attempt(liveness="in_flight", caller_abandoned=True)]
    rows += [
        attempt(
            status="failed",
            resolved_at=NOW,
            liveness="resolved",
            failure_kind=kind,
            outcome="failed",
        )
        for kind in ("terminated", "timeout")
    ]
    feed = build_activity_feed(rows)
    assert {entry["state"] for entry in feed["entries"]} <= ACTIVITY_STATES


def test_every_activity_state_has_a_label_a_tone_and_a_note():
    """A state that reaches a surface without one of the three is a KeyError
    in front of a user, so the three tables are pinned to the vocabulary."""

    from orchestrator.mcp import presentation as pres

    for state in ACTIVITY_STATES:
        assert state in pres._STATE_LABELS, state
        assert state in pres._STATE_TONES, state
        assert state in pres._ACTIVITY_NOTES, state
        assert pres._STATE_TONES[state] in {"good", "warning", "critical", "muted"}


# -- the caller leaving is not the worker stopping ---------------------------


def test_a_caller_who_stopped_waiting_does_not_end_the_worker():
    """The second state: the request timed out, the work did not."""

    entry = build_activity_entry(attempt(liveness="in_flight", caller_abandoned=True))

    assert entry["state"] == "unattended"
    # Neither of the two lies this state exists to prevent: "Running" implies
    # someone is waiting for it, "Completed" implies it finished.
    assert entry["state"] not in {"running", "completed", "no_response"}
    assert entry["label"] == "Still running"
    assert entry["tone"] == "warning"
    assert entry["headline"] == "Codex is still running this"
    assert "still running" in entry["note"]
    assert {
        "key": "waiting",
        "label": "Waiting",
        "value": "Nothing is waiting for the result",
    } in entry["facts"]


def test_a_live_worker_pid_does_not_override_the_caller_having_left(monkeypatch):
    """A probe that proves the worker is alive is the very evidence
    `unattended` rests on -- it must not be rounded back up to `running`."""

    monkeypatch.setattr("orchestrator.procutil.pid_alive", lambda pid: True)
    entry = build_activity_entry(
        attempt(liveness="in_flight", caller_abandoned=True, worker_pid=4242)
    )

    assert entry["state"] == "unattended"
    assert entry["state"] != "running"


def test_a_confirmed_termination_reads_stopped_and_never_failed():
    """The worker reported nothing; ModelQuad ended it. Attributing that
    ending to the worker as `failed` would be the product blaming a worker
    for its own decision."""

    entry = build_activity_entry(
        attempt(
            status="failed",
            resolved_at=NOW,
            liveness="resolved",
            caller_abandoned=True,
            failure_kind="terminated",
            outcome="failed",
        )
    )

    assert entry["state"] == "stopped"
    assert entry["label"] == "Stopped"
    assert entry["tone"] == "critical"
    assert entry["headline"] == "Codex was stopped"
    assert "confirmed the process ended" in entry["note"]


def test_an_unconfirmed_termination_stays_unknown_and_says_so():
    """Worker termination leaves an unconfirmed kill as `timeout`. No evidence of
    death is not evidence of death, so the state stays UNKNOWN and the
    uncertainty is stated rather than hidden."""

    entry = build_activity_entry(
        attempt(
            status="failed",
            resolved_at=NOW,
            liveness="resolved",
            caller_abandoned=True,
            failure_kind="timeout",
            outcome="failed",
            detail=(
                "timed_out; worker pid 4242: kill attempted (tree kill timed_out) "
                "but NOT confirmed dead -- unaccounted for"
            ),
        )
    )

    assert entry["state"] == "unknown"
    assert entry["state"] != "stopped"
    assert {
        "key": "worker_fate",
        "label": "Worker",
        "value": "ModelQuad could not confirm the worker stopped",
    } in entry["facts"]


def test_an_underivable_liveness_stays_unknown_even_when_the_caller_left():
    entry = build_activity_entry(attempt(liveness="unknown", caller_abandoned=True))

    assert entry["state"] == "unknown"
    assert entry["state"] not in {"unattended", "stopped"}


def test_no_pid_and_no_raw_termination_prose_survives_into_any_feed_string():
    """`detail` carries pids and the kill command. It is stored for
    diagnostics and must never reach a user-facing string."""

    rows = [
        attempt(
            status="failed",
            resolved_at=NOW,
            liveness="resolved",
            worker_pid=4242,
            caller_abandoned=True,
            failure_kind=kind,
            outcome="failed",
            detail=(
                "timed_out; worker pid 4242 terminated (tree kill ok; exit confirmed)"
            ),
        )
        for kind in ("terminated", "timeout")
    ]
    rows.append(attempt(liveness="in_flight", caller_abandoned=True, worker_pid=4242))
    rendered = json.dumps(build_activity_feed(rows))

    for leak in ("4242", "tree kill", "taskkill", "pid", "timed_out", "unaccounted"):
        assert leak not in rendered, leak


# -- a clean exit is not success (AGENTS.md; implementation requirement c) ---


def test_a_worker_that_exited_zero_without_a_result_is_not_reported_as_done():
    row = execution(task_status=None, success=True, exit_code=0, result="success")
    entry = build_activity_entry(row)

    assert entry["state"] == "no_result"
    assert entry["label"] == "No result reported"
    assert entry["tone"] == "muted"
    assert "cannot say whether the task was done" in entry["note"]
    # Not merely "not labelled completed" -- the word must not appear at all.
    assert "success" not in json.dumps(entry).lower()
    assert "finished this task" not in entry["headline"]


def test_the_execution_state_never_reads_the_process_channel():
    """A failed process and a successful one map identically on task evidence."""

    assert activity_state_from_execution(
        execution(success=True, exit_code=0, task_status="failed")
    ) == "failed"
    assert activity_state_from_execution(
        execution(success=False, exit_code=1, task_status="completed")
    ) == "completed"


def test_a_completion_is_the_workers_own_claim_unless_modelquad_checked_it():
    reported = build_activity_entry(execution(task_status="completed", outcome_verified=False))
    unrecorded = build_activity_entry(execution(task_status="completed", outcome_verified=None))
    checked = build_activity_entry(execution(task_status="completed", outcome_verified=True))

    def result_fact(entry):
        return next(fact["value"] for fact in entry["facts"] if fact["key"] == "result")

    assert result_fact(reported) == "Reported by the worker"
    assert result_fact(unrecorded) == "Reported by the worker"
    assert result_fact(checked) == "Checked by ModelQuad"


# -- an attempt that started and never came back ----------------------------


def test_an_attempt_that_started_and_never_resolved_is_shown_as_exactly_that():
    entry = build_activity_entry(attempt(liveness="abandoned", status="started", resolved_at=None))

    assert entry["state"] == "no_response"
    assert entry["label"] == "No response"
    assert entry["headline"] == "Codex never reported back"
    assert "never reported back" in entry["note"]
    # Neither rounded up to a result, nor left running forever.
    assert entry["state"] not in {"completed", "running"}


def test_an_attempt_with_no_proven_worker_reads_as_delegating_not_a_result():
    """In flight, but nothing proves a worker is executing yet.

    `liveness` reports in_flight for a pid-less row purely because the row is
    young, which is not evidence that a worker is running. That reads as
    Delegating; Running requires an actual live pid (see the test below).
    Either way it is never rounded up into a result.
    """
    entry = build_activity_entry(attempt(liveness="in_flight"))
    assert entry["state"] == "delegating"
    assert entry["label"] == "Delegating"
    assert entry["state"] not in {"completed", "running"}
    assert entry["facts"] == []


def test_an_attempt_with_a_live_worker_pid_reads_as_running(monkeypatch):
    monkeypatch.setattr("orchestrator.procutil.pid_alive", lambda pid: True)
    entry = build_activity_entry(attempt(liveness="in_flight", worker_pid=4242))
    assert entry["state"] == "running"
    assert entry["label"] == "Running"


def test_a_refused_attempt_says_it_never_started_and_why_in_words():
    entry = build_activity_entry(
        attempt(status="refused", resolved_at=NOW, liveness="resolved", failure_kind="duplicate_in_flight")
    )
    assert entry["state"] == "refused"
    assert entry["headline"] == "This handoff was not started"
    assert {"key": "refusal", "label": "Why not", "value": "The same task was already running."} in entry["facts"]


def test_an_unrecognised_refusal_kind_produces_no_sentence_rather_than_a_raw_token():
    entry = build_activity_entry(
        attempt(status="refused", resolved_at=NOW, failure_kind="some_future_kind")
    )
    assert all(fact["key"] != "refusal" for fact in entry["facts"])
    assert "some_future_kind" not in json.dumps(entry)


# -- UNKNOWN is rendered, never filled in -----------------------------------


def test_an_unidentified_host_reads_unknown_and_never_a_guessed_runtime():
    entry = build_activity_entry(execution(host=None, metadata={}))

    assert entry["where"]["origin"] == {"key": None, "label": "Unknown", "known": False}
    # "we do not know whether it moved" is not "it did not move".
    assert entry["where"]["moved"] is None


def test_an_attempt_whose_liveness_cannot_be_derived_reads_unknown():
    entry = build_activity_entry(attempt(liveness="unknown"))

    assert entry["state"] == "unknown"
    assert entry["label"] == "Unknown"
    assert entry["tone"] == "muted"
    assert "not enough recorded evidence" in entry["note"].lower()


def test_a_row_with_no_recorded_decision_source_says_nothing_about_why():
    assert build_activity_entry(routing(decision_source=None))["why"] is None
    assert build_activity_entry(execution(decision_id="d1"), decision_sources={})["why"] is None


# -- origin and executor are two records, never merged ----------------------


def test_origin_and_executor_stay_two_records_for_a_handoff():
    entry = build_activity_entry(
        execution(
            provider="codex",
            metadata={
                "host_handoff": {
                    "origin_runtime": "claude_code",
                    "target_runtime": "codex",
                    "handoff_required": True,
                }
            },
        )
    )
    where = entry["where"]
    assert where["origin"]["label"] == "Claude Code"
    assert where["ran_on"]["label"] == "Codex"
    assert where["moved"] is True


def test_a_task_kept_in_the_calling_session_is_one_runtime_at_two_moments():
    entry = build_activity_entry(
        execution(provider="claude", host="claude_code", metadata={"host_outcome": True})
    )
    where = entry["where"]
    assert where["origin"]["known"] is True
    assert where["ran_on"]["known"] is True
    # Still two keys. `moved` is what says they are the same runtime.
    assert where["moved"] is False


def test_a_routing_row_names_what_was_chosen_and_never_claims_it_ran():
    entry = build_activity_entry(routing())
    assert entry["state"] == "selected"
    assert entry["headline"] == "Chose Codex"
    assert "ran" not in entry["headline"].lower()
    assert "finished" not in entry["headline"].lower()


# -- why, in the user's vocabulary ------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("deterministic", "Advice (task fit)"),
        ("host_reasoned", "Advice (with reasoning)"),
        ("delegated_reasoned", "Advice (with reasoning)"),
        ("fallback", "Substituted worker (older record)"),
        ("user_override", "You chose the provider"),
    ],
)
def test_the_recorded_decision_source_becomes_a_sentence_not_a_token(source, expected):
    assert build_activity_entry(routing(decision_source=source))["why"] == expected


def test_an_execution_inherits_why_from_the_routing_row_it_shares_an_identity_with():
    entry = build_activity_entry(
        execution(decision_id="d1"), decision_sources={"d1": "fallback"}
    )
    assert entry["why"] == "Substituted worker (older record)"


def test_an_override_says_you_chose_it_even_without_a_decision_source():
    entry = build_activity_entry(
        routing(decision_source=None, overridden=True, override_reason="I wanted Claude")
    )
    assert entry["why"] == "You chose the provider"
    assert {"key": "override", "label": "You said", "value": "I wanted Claude"} in entry["facts"]


# -- no internals reach a normal user ---------------------------------------


_FORBIDDEN = (
    "decision_id",
    "task_id",
    "exit_code",
    "sqlite",
    "routing_decisions",
    "execution_history",
    "delegation_attempts",
    "task_status",
    "fit_weight",
    "usage_weight",
    "quota_penalty",
    "decision_source",
    "providers_json",
    "metadata_json",
)


def test_no_storage_or_routing_internal_survives_into_the_feed():
    feed = build_activity_feed(
        [
            routing(decision_id="dead-beef", decision_source="fallback"),
            execution(decision_id="dead-beef", task_status=None, fallback=True, fallback_from="claude"),
        ],
        attempts=[attempt(decision_id="dead-beef", liveness="abandoned")],
        decision_sources={"dead-beef": "fallback"},
    )
    blob = json.dumps(feed).lower()
    for token in _FORBIDDEN:
        assert token not in blob, token
    assert "dead-beef" not in blob


def test_the_feed_carries_no_provider_score_or_raw_percentage():
    feed = build_activity_feed([routing()])
    blob = json.dumps(feed)
    assert "providers" not in blob
    assert "0." not in blob


def test_an_empty_feed_invites_action_rather_than_reporting_an_absence():
    feed = build_activity_feed([])
    assert feed["entries"] == []
    assert feed["empty"].startswith("No activity yet")


def test_rows_are_ordered_newest_first_across_all_three_kinds():
    feed = build_activity_feed(
        [
            execution(id=1, created_at="2026-08-27T09:00:00+00:00"),
            routing(id=2, created_at="2026-08-27T11:00:00+00:00"),
        ],
        attempts=[attempt(id=3, created_at="2026-08-27T10:00:00+00:00")],
    )
    assert [entry["id"] for entry in feed["entries"]] == [2, 3, 1]


# -- the module stays a leaf ------------------------------------------------


def test_presentation_imports_nothing_from_the_orchestrator_package_at_module_level():
    """It is a reader of decisions, so it must not be able to cause one.

    A module-level import of the orchestrator package would let this adapter
    reach back into routing, state, or the database -- and would make it
    importable only alongside the machinery it is meant to describe. The one
    orchestrator import it makes (`registry`, for a provider's display name)
    is function-local and guarded on purpose.
    """

    source = Path("src/orchestrator/mcp/presentation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    for node in module_level:
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"relative import at module level: {ast.dump(node)}"
            assert not (node.module or "").startswith("orchestrator"), node.module
        else:
            for alias in node.names:
                assert not alias.name.startswith("orchestrator"), alias.name


# -- the storage layer feeding it -------------------------------------------


def test_open_and_refused_attempts_are_returned_and_resolved_ones_are_not(tmp_path):
    db_path = tmp_path / "history.db"
    open_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", requested_provider="codex"
    )
    service_history.record_delegation_attempt(
        db_path,
        task_type="implementation",
        requested_provider="codex",
        status="refused",
        failure_kind="policy_refused",
    )
    done_id = service_history.record_delegation_attempt(
        db_path, task_type="review", requested_provider="codex"
    )
    service_history.resolve_delegation_attempt(db_path, done_id, status="completed")

    rows = service_history.fetch_open_delegation_attempts(db_path)
    statuses = {row["status"] for row in rows}

    assert statuses == {"started", "refused"}
    assert all(row["kind"] == "attempt" for row in rows)
    assert open_id in {row["id"] for row in rows}
    assert done_id not in {row["id"] for row in rows}


def test_an_old_unresolved_attempt_reaches_the_feed_as_no_response(tmp_path):
    db_path = tmp_path / "history.db"
    service_history.record_delegation_attempt(
        db_path, task_type="implementation", requested_provider="codex"
    )
    # Far past the longest a delegation may legitimately run, so the row
    # cannot still be waiting on its own executor.
    later = datetime.now(timezone.utc) + timedelta(days=1)

    rows = service_history.fetch_open_delegation_attempts(db_path, now=later)
    feed = build_activity_feed([], attempts=rows)

    assert [entry["state"] for entry in feed["entries"]] == ["no_response"]
    assert feed["entries"][0]["headline"] == "Codex never reported back"


def test_a_caller_abandoned_open_attempt_reaches_the_feed_as_unattended(tmp_path):
    """End to end over the real query: the row a caller walked away from is
    in the feed, saying it is still running, not hidden and not rounded up."""

    db_path = tmp_path / "history.db"
    attempt_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", requested_provider="codex"
    )
    service_history.record_caller_abandonment(
        db_path, attempt_id, reason="transport_cancelled"
    )

    rows = service_history.fetch_open_delegation_attempts(db_path)
    feed = build_activity_feed([], attempts=rows)

    assert [entry["state"] for entry in feed["entries"]] == ["unattended"]
    assert feed["entries"][0]["headline"] == "Codex is still running this"


def test_a_confirmed_stop_leaves_the_open_feed_and_is_shown_by_current_task(tmp_path):
    """Resolving the row removes it from the open-attempt query by design --
    its execution row is the richer record of the same event. `stopped` is
    therefore a Current Task state, and this pins that boundary so a later
    change cannot start listing one delegation twice."""

    db_path = tmp_path / "history.db"
    attempt_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", requested_provider="codex"
    )
    service_history.record_caller_abandonment(
        db_path, attempt_id, reason="transport_cancelled"
    )
    service_history.resolve_delegation_attempt(
        db_path,
        attempt_id,
        status="failed",
        routed_provider="codex",
        outcome="failed",
        failure_kind="terminated",
        detail="timed_out; worker pid 4242 terminated (tree kill ok; exit confirmed)",
    )

    assert service_history.fetch_open_delegation_attempts(db_path) == []

    current = service_history.fetch_current_task(db_path)
    assert activity_state_from_attempt(current) == "stopped"


def test_verification_status_survives_the_compact_listing(tmp_path):
    from orchestrator.execution.base import ExecutionResult, OutcomeEvidence, WorkerTaskStatus

    db_path = tmp_path / "history.db"
    for verification_level, provider in (("objective", "codex"), ("semantic", "claude")):
        service_history.record_execution(
            db_path,
            "implementation",
            ExecutionResult(
                provider=provider,
                success=True,
                output="",
                exit_code=0,
                task_status=WorkerTaskStatus.COMPLETED,
                evidence=OutcomeEvidence(verification_level=verification_level),
            ),
        )

    rows = {row["provider"]: row for row in service_history.fetch_activity(db_path)}

    assert rows["codex"]["outcome_verified"] is True
    assert rows["claude"]["outcome_verified"] is False
    # The raw evidence blob still does not travel in the compact listing.
    assert "outcome_evidence_json" not in rows["codex"]

    assert (
        next(
            fact["value"]
            for fact in build_activity_entry(rows["codex"])["facts"]
            if fact["key"] == "result"
        )
        == "Checked by ModelQuad"
    )
    assert (
        next(
            fact["value"]
            for fact in build_activity_entry(rows["claude"])["facts"]
            if fact["key"] == "result"
        )
        == "Reported by the worker"
    )


def test_decision_sources_are_looked_up_never_reconstructed(tmp_path):
    from orchestrator.models import Freshness, ProviderScore, RoutingResult

    db_path = tmp_path / "history.db"
    result = RoutingResult(
        task_type="implementation",
        providers=[
            ProviderScore(
                provider="codex",
                available=True,
                fit=1.0,
                remaining_percentage=50.0,
                freshness=Freshness.FRESH,
                usage_score=0.5,
                usage_confidence=1.0,
                quota_penalty=0.0,
                score=0.8,
            )
        ],
        recommendation="codex",
        reason="because",
    )
    service_history.record_routing_decision(
        db_path, "implementation", result, decision_id="known", decision_source="fallback"
    )

    sources = service_history.fetch_decision_sources(db_path, ["known", "never-recorded", None])

    assert sources == {"known": "fallback"}
    assert service_history.fetch_decision_sources(db_path, []) == {}


def test_the_activity_tool_serves_a_plain_language_view_beside_the_raw_record(tmp_path, monkeypatch):
    import importlib

    from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus

    mcp_server = importlib.import_module("orchestrator.mcp.server")
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)

    def _call(tool, **kwargs):
        return (getattr(tool, "fn", None) or tool)(**kwargs)

    service_history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(
            provider="codex", success=True, output="", exit_code=0, task_status=WorkerTaskStatus.UNKNOWN
        ),
    )
    service_history.record_delegation_attempt(
        db_path, task_type="review", requested_provider="codex"
    )

    payload = _call(mcp_server.modelquad_activity, limit=20)

    assert "activity" in payload and "presentation" in payload
    states = {entry["state"] for entry in payload["presentation"]["entries"]}
    # The clean exit with no envelope, and the attempt that is still open.
    # The open attempt has no recorded worker pid, so it is honestly
    # "delegating" -- a handoff under way, not a proven running worker.
    assert states == {"no_result", "delegating"}
    assert "decision_id" not in json.dumps(payload["presentation"])
