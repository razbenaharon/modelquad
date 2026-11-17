# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""How an execution result resolves into an attempt verdict, without launching anything."""

from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from modelquad.execution.base import (
    ExecutionResult,
    WorkerTaskStatus,
    attempt_resolution,
    classify_worker_result,
)


def result(**kwargs):
    return ExecutionResult(provider="codex", success=False, output="", exit_code=None, **kwargs)


@pytest.mark.parametrize("reason,kind", [
    ("invalid_request", "invalid_request"),
    ("cli_not_found", "failed_to_start"),
    ("invalid_sandbox_mode", "invalid_request"),
    ("invalid_reasoning_effort", "invalid_request"),
])
def test_prelaunch_failure_classification_remains_distinct(reason, kind):
    assert attempt_resolution(result(task_status_reason=reason)) == ("failed", kind)


@pytest.mark.parametrize("reason", [
    "network_policy_refused", "read_only_resume_unpinned", "action_authority_unmapped",
])
def test_policy_refusal_did_not_attempt_a_worker(reason):
    assert attempt_resolution(result(task_status_reason=reason)) == ("refused", "policy_refused")


@pytest.mark.parametrize("status", list(WorkerTaskStatus))
@pytest.mark.parametrize("process_success", [False, True])
def test_finished_attempt_is_separate_from_objective_claim_and_process_success(status, process_success):
    completed = ExecutionResult(
        provider="codex", success=process_success, output="", exit_code=0 if process_success else 1,
        task_status=status, task_status_reason="worker_reported_" + status.value,
    )
    assert attempt_resolution(completed) == ("completed", None)
    assert completed.task_completed is (status is WorkerTaskStatus.COMPLETED)


@pytest.mark.parametrize("facts,kind", [
    (None, "timeout"),
    ({"confirmed_dead": True}, "timeout"),
    ({"confirmed_dead": False, "tree_kill": "ok"}, "timeout"),
    ({"confirmed_dead": True, "tree_kill": "direct_child_only"}, "timeout"),
    ({"confirmed_dead": True, "tree_kill": "census_unknown"}, "timeout"),
    ({"confirmed_dead": True, "tree_kill": "job_not_empty"}, "timeout"),
    ({"confirmed_dead": True, "tree_kill": "ok"}, "terminated"),
])
def test_timeout_needs_confirmed_tree_death_even_over_a_worker_completion_claim(facts, kind):
    metadata = {"timed_out": True}
    if facts is not None:
        metadata["worker_termination"] = facts
    completed_claim = result(metadata=metadata, task_status=WorkerTaskStatus.COMPLETED)
    assert attempt_resolution(completed_claim) == ("failed", kind)


def test_caller_departure_and_deadline_metadata_cannot_assert_worker_termination():
    unaccounted = result(metadata={"timed_out": True, "caller_abandoned": True, "deadline_elapsed": True})
    assert attempt_resolution(unaccounted) == ("failed", "timeout")
    ordinary = result(metadata={"caller_abandoned": True, "deadline_elapsed": True})
    assert attempt_resolution(ordinary) == ("completed", None)
    assert ordinary.task_status is WorkerTaskStatus.UNKNOWN


@pytest.mark.parametrize("confirmed,kind", [(False, "timeout"), (True, "terminated")])
def test_result_normalization_detaches_inputs_and_fixes_terminal_verdict(confirmed, kind):
    metadata = {"timed_out": True, "worker_termination": {"confirmed_dead": confirmed, "tree_kill": "ok"}}
    normalized = result(metadata=metadata)
    terminal = normalized.terminal_resolution
    metadata["timed_out"] = False
    metadata["worker_termination"]["confirmed_dead"] = not confirmed
    assert normalized.metadata["timed_out"] is True
    assert normalized.metadata["worker_termination"]["confirmed_dead"] is confirmed
    serialized = normalized.to_dict()
    serialized["metadata"]["worker_termination"]["confirmed_dead"] = not confirmed
    assert normalized.metadata["worker_termination"]["confirmed_dead"] is confirmed
    # Preserve mutable legacy annotations without making them terminal authority.
    normalized.metadata["timed_out"] = False
    normalized.metadata["worker_termination"]["confirmed_dead"] = not confirmed
    assert attempt_resolution(normalized) == ("failed", kind)
    assert normalized.terminal_resolution is terminal
    with pytest.raises(FrozenInstanceError):
        terminal.failure_kind = "timeout" if confirmed else "terminated"


def test_existing_manager_annotation_replacement_preserves_terminal_classification():
    original = result(metadata={"timed_out": True})
    annotated = replace(original, metadata={**original.metadata, "agent_runtime": "codex"})
    assert attempt_resolution(annotated) == attempt_resolution(original) == ("failed", "timeout")
    assert annotated.metadata["agent_runtime"] == "codex"


def test_fresh_normalization_can_intentionally_supply_different_execution_facts():
    original = result(metadata={"timed_out": True})
    accounted = replace(original, metadata={
        "timed_out": True, "worker_termination": {"confirmed_dead": True, "tree_kill": "ok"},
    })
    assert attempt_resolution(original) == ("failed", "timeout")
    assert attempt_resolution(accounted) == ("failed", "terminated")


def test_internal_terminal_snapshot_does_not_expand_serialization_fields():
    normalized = result(metadata={"timed_out": True})
    legacy_fields = {
        "provider", "success", "output", "exit_code", "session_id", "error", "metadata",
        "task_status", "task_status_reason", "safe_to_retry", "limitations",
    }
    assert set(asdict(normalized)) == legacy_fields
    assert set(normalized.to_dict()) == {
        "provider", "success", "process_success", "output", "exit_code", "session_id", "error",
        "metadata", "task_status", "reason", "safe_to_retry", "task_completed", "limitations",
    }


def test_clean_exit_without_envelope_remains_an_unknown_objective():
    status, reason, retry = classify_worker_result(returncode=0, combined_output="done", primary_output="done")
    normalized = result(task_status=status, task_status_reason=reason, safe_to_retry=retry)
    assert attempt_resolution(normalized) == ("completed", None)
    assert normalized.task_status is WorkerTaskStatus.UNKNOWN
    assert normalized.task_completed is False
    assert normalized.safe_to_retry is False
