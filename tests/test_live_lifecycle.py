# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from orchestrator.mcp.livelifecycle import (
    LIVE_STATES,
    LiveFacts,
    LiveState,
    derive_live_state,
    derive_recovery_outlook,
    facts_from_attempt,
)


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (LiveFacts(attempt_exists=True, attempt_resolved=True, task_status="completed"), "completed"),
        (LiveFacts(attempt_exists=True, attempt_resolved=True, task_status="blocked"), "blocked"),
        (LiveFacts(attempt_exists=True, attempt_resolved=True, task_status="failed"), "failed"),
        (LiveFacts(attempt_exists=True, attempt_resolved=True, task_status="partial"), "partial"),
        (LiveFacts(verification_started=True), "unknown"),
        (
            LiveFacts(
                attempt_exists=True,
                attempt_status="started",
                worker_pid=1234,
                worker_alive=True,
            ),
            "running",
        ),
        (LiveFacts(attempt_exists=True, attempt_status="started"), "delegating"),
        (
            LiveFacts(
                attempt_exists=True,
                attempt_status="started",
                liveness="in_flight",
                caller_abandoned=True,
            ),
            "unattended",
        ),
        (
            LiveFacts(
                attempt_exists=True,
                attempt_resolved=True,
                task_status="failed",
                failure_kind="terminated",
            ),
            "stopped",
        ),
        (
            LiveFacts(
                attempt_exists=True,
                attempt_resolved=True,
                task_status="failed",
                failure_kind="timeout",
            ),
            "unknown",
        ),
        (LiveFacts(decision_exists=True), "selected"),
        (LiveFacts(), "unknown"),
    ],
)
def test_derives_each_live_state(facts: LiveFacts, expected: str) -> None:
    assert derive_live_state(facts).state == expected


def test_live_states_lists_every_state_the_deriver_returns() -> None:
    assert LIVE_STATES == {
        "completed",
        "blocked",
        "failed",
        "partial",
        "unknown",
        "running",
        "delegating",
        "selected",
        "unattended",
        "stopped",
    }


def test_unknown_worker_liveness_is_delegating_not_running() -> None:
    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_status="started",
            worker_pid=1234,
            worker_alive=None,
        )
    )

    assert state == derive_live_state(
        LiveFacts(attempt_exists=True, attempt_status="started", worker_pid=1234)
    )
    assert state.state == "delegating"


def test_missing_worker_pid_is_delegating_not_running() -> None:
    state = derive_live_state(
        LiveFacts(attempt_exists=True, attempt_status="started", worker_alive=True)
    )

    assert state.state == "delegating"
    assert state.reason == "no_proven_live_worker"


def test_abandoned_attempt_is_unknown_not_running_or_failed() -> None:
    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_status="started",
            worker_pid=1234,
            worker_alive=False,
            liveness="abandoned",
        )
    )

    assert state == derive_live_state(
        LiveFacts(attempt_exists=True, attempt_status="started", liveness="abandoned")
    )
    assert state.state == "unknown"
    assert state.reason == "attempt_abandoned"


def test_legacy_start_stamp_does_not_prove_a_live_worker() -> None:
    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_status="started",
            worker_pid=1234,
            worker_alive=False,
            verification_started=True,
        )
    )

    assert state == LiveState("delegating", "no_proven_live_worker")


def test_completed_claim_contradicted_by_objective_tests_is_failed() -> None:
    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_resolved=True,
            task_status="completed",
            tests_executed=True,
            tests_passed=False,
            verification_level="objective",
        )
    )

    assert state == LiveState("failed", "objective_tests_failed")


def test_completed_without_tests_is_claim_only() -> None:
    state = derive_live_state(
        LiveFacts(attempt_exists=True, attempt_resolved=True, task_status="completed")
    )

    assert state == LiveState("completed", "claim_only_no_objective_tests")


def test_blocked_is_never_converted_to_completed() -> None:
    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_resolved=True,
            task_status="blocked",
            tests_executed=True,
            tests_passed=True,
        )
    )

    assert state == LiveState("blocked", "worker_reported_blocked")


def test_absent_evidence_is_unknown() -> None:
    assert derive_live_state(LiveFacts()) == LiveState("unknown", "no_lifecycle_evidence")


def test_completed_legacy_start_stamp_preserves_terminal_recovery() -> None:
    """An unresolved historical stamp cannot keep completed work in flight."""
    facts = facts_from_attempt(
        {
            "status": "completed",
            "resolved_at": "2026-09-06T00:00:00+00:00",
            "outcome": "completed",
            "verification_started_at": "2026-09-05T23:59:00+00:00",
            "verification_resolved_at": None,
        }
    )

    assert derive_live_state(facts) == LiveState("completed", "claim_only_no_objective_tests")
    outlook = derive_recovery_outlook(facts)
    assert outlook.state == "completed"
    assert outlook.terminal is True
    assert outlook.worker_may_be_running is False
    assert outlook.safe_to_retry is True


@pytest.mark.parametrize(
    ("row", "worker_alive", "expected"),
    [
        ({"status": "started", "worker_pid": 1234}, False, "delegating"),
        ({"status": "started", "worker_pid": 1234}, True, "running"),
        ({"status": "started", "liveness": "abandoned"}, False, "unknown"),
        ({"status": "started", "liveness": "unknown"}, None, "unknown"),
        (
            {"status": "started", "liveness": "in_flight", "caller_abandoned": True},
            True,
            "unattended",
        ),
        (
            {"status": "orphaned", "resolved_at": "2026-09-06T00:00:00+00:00"},
            False,
            "unknown",
        ),
        (
            {"status": "failed", "resolved_at": "2026-09-06T00:00:00+00:00", "outcome": "failed"},
            False,
            "failed",
        ),
        (
            {"status": "completed", "resolved_at": "2026-09-06T00:00:00+00:00", "outcome": "blocked"},
            None,
            "blocked",
        ),
        (
            {"status": "completed", "resolved_at": "2026-09-06T00:00:00+00:00", "outcome": "partial"},
            None,
            "partial",
        ),
        (
            {
                "status": "failed",
                "resolved_at": "2026-09-06T00:00:00+00:00",
                "outcome": "failed",
                "failure_kind": "terminated",
            },
            False,
            "stopped",
        ),
        (
            {
                "status": "failed",
                "resolved_at": "2026-09-06T00:00:00+00:00",
                "outcome": "failed",
                "failure_kind": "timeout",
            },
            None,
            "unknown",
        ),
    ],
)
def test_legacy_start_stamp_preserves_other_recorded_facts(row, worker_alive, expected) -> None:
    unstamped = derive_live_state(facts_from_attempt(row, worker_alive=worker_alive))
    stamped = derive_live_state(
        facts_from_attempt(
            {
                **row,
                "verification_started_at": "2026-09-05T23:59:00+00:00",
                "verification_resolved_at": None,
            },
            worker_alive=worker_alive,
        )
    )
    assert unstamped.state == expected
    assert stamped == unstamped


def test_obsolete_state_is_absent_from_live_vocabulary() -> None:
    from orchestrator.mcp.presentation import ACTIVITY_STATES, execution_lifecycle
    from orchestrator.mcp.server import modelquad_delegation_status

    assert "verifying" not in LIVE_STATES
    assert "verifying" not in ACTIVITY_STATES
    with pytest.raises(ValueError):
        execution_lifecycle("verifying")
    assert "verifying" not in (modelquad_delegation_status.__doc__ or "").lower()


def test_resolved_verification_settles_into_a_terminal_state() -> None:
    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_resolved=True,
            task_status="completed",
            verification_started=True,
            verification_resolved=True,
            tests_executed=True,
            tests_passed=True,
        )
    )

    assert state == LiveState("completed", "objective_tests_passed")


def test_underivable_liveness_is_unknown_not_delegating() -> None:
    """An absence of evidence must not be promoted to "handoff in progress"."""
    state = derive_live_state(
        LiveFacts(attempt_exists=True, attempt_status="started", liveness="unknown")
    )

    assert state == LiveState("unknown", "liveness_underivable")


def test_a_caller_who_left_does_not_stop_a_worker_that_is_still_in_flight() -> None:
    """The caller's transport and the worker's process are two facts. Only
    the worker's may end the worker's state."""

    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_status="started",
            liveness="in_flight",
            caller_abandoned=True,
            worker_pid=1234,
            worker_alive=True,
        )
    )
    assert state.state == "unattended"
    assert state.reason == "caller_abandoned_worker_in_flight"


def test_a_caller_who_left_a_worker_already_gone_is_still_not_unattended() -> None:
    """`unattended` claims the worker IS running. Without that observation
    the row stays the honest `unknown` it was before."""

    assert (
        derive_live_state(
            LiveFacts(
                attempt_exists=True,
                attempt_status="started",
                liveness="abandoned",
                caller_abandoned=True,
            )
        ).state
        == "unknown"
    )
    assert (
        derive_live_state(
            LiveFacts(
                attempt_exists=True,
                attempt_status="started",
                liveness="unknown",
                caller_abandoned=True,
            )
        ).state
        == "unknown"
    )


def test_a_confirmed_stop_outranks_the_outcome_recorded_beside_it() -> None:
    """On the termination path nothing was reported by the worker: the
    `failed` outcome is the timeout handler's, and presenting it as the
    worker's verdict would misattribute ModelQuad's own decision."""

    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_resolved=True,
            task_status="failed",
            failure_kind="terminated",
        )
    )
    assert state.state == "stopped"
    assert state.reason == "worker_termination_confirmed"


def test_an_unconfirmed_stop_is_unknown_not_stopped_and_not_failed() -> None:
    state = derive_live_state(
        LiveFacts(
            attempt_exists=True,
            attempt_resolved=True,
            task_status="failed",
            failure_kind="timeout",
        )
    )
    assert state.state == "unknown"
    assert state.reason == "worker_termination_unconfirmed"


def test_facts_from_attempt_reads_the_two_new_columns() -> None:
    from orchestrator.mcp.livelifecycle import facts_from_attempt

    facts = facts_from_attempt(
        {
            "status": "failed",
            "resolved_at": "2026-09-06T00:00:00+00:00",
            "caller_abandoned": True,
            "failure_kind": "terminated",
            "detail": "timed_out; worker pid 4242 terminated (tree kill ok)",
        }
    )
    assert facts.caller_abandoned is True
    assert facts.failure_kind == "terminated"
    # The free text beside them is not carried into the lifecycle model.
    assert "detail" not in facts.__dict__


def test_a_row_without_the_new_columns_still_derives(tmp_path=None) -> None:
    """Older rows carry neither column; absent must read as absent."""

    from orchestrator.mcp.livelifecycle import facts_from_attempt

    facts = facts_from_attempt({"status": "started", "resolved_at": None})
    assert facts.caller_abandoned is False
    assert facts.failure_kind is None
