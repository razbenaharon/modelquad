# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""How an execution result resolves into an attempt verdict, without launching anything."""

from dataclasses import FrozenInstanceError, asdict, replace
from threading import Barrier, Thread

import pytest

from modelquad.execution.base import (
    CallerWaitHandle,
    ExecutionResult,
    WorkerTaskStatus,
    attempt_resolution,
    classify_worker_result,
)


def result(**kwargs):
    return ExecutionResult(provider="codex", success=False, output="", exit_code=None, **kwargs)


def test_identity_is_write_once_and_repeated_publication_is_idempotent(tmp_path):
    db = tmp_path / "history.db"
    handle = CallerWaitHandle()
    before = handle.snapshot
    handle.publish(db, 7)
    published = handle.snapshot
    handle.publish(db, 7)
    assert handle.snapshot is published
    for identity in ((db, 8), (tmp_path / "other.db", 7)):
        with pytest.raises(ValueError, match="already bound"):
            handle.publish(*identity)
    assert before.identity is None
    assert handle.identity == (db, 7)
    with pytest.raises(AttributeError):
        handle.identity = None
    with pytest.raises(FrozenInstanceError):
        published.identity = (db, 9)


@pytest.mark.parametrize("identity", [(None, 7), ("db", None), ("db", True), ("db", 0)])
def test_partial_or_invalid_identity_never_becomes_publishable(identity):
    handle = CallerWaitHandle()
    with pytest.raises(ValueError):
        handle.publish(*identity)
    assert handle.identity is None


@pytest.mark.parametrize("departure_first", [True, False])
def test_first_departure_survives_publication_and_later_observations(departure_first):
    handle = CallerWaitHandle()
    if departure_first:
        handle.abandoned_reason = "transport_cancelled"
    handle.publish("db", 7)
    if not departure_first:
        handle.abandoned_reason = "transport_cancelled"
    departed = handle.snapshot
    handle.abandoned_reason = "transport_closed"
    handle.abandoned_reason = None
    assert handle.snapshot is departed
    assert departed.identity == ("db", 7)
    assert departed.abandoned_reason == "transport_cancelled"
    with pytest.raises(FrozenInstanceError):
        departed.abandoned_reason = None


def test_constructor_keeps_legacy_identity_and_departure_arguments():
    handle = CallerWaitHandle(("db", 7), "transport_closed")
    assert (handle.db_path, handle.attempt_id) == ("db", 7)
    assert handle.abandoned_reason == "transport_closed"


def test_publication_departure_race_leaves_at_least_one_side_with_both_facts():
    handle = CallerWaitHandle()
    rendezvous = Barrier(2)
    observations = []
    errors = []

    def publishing():
        try:
            rendezvous.wait(timeout=5)
            handle.publish("db", 7)
            observations.append((handle.identity, handle.abandoned_reason))
        except BaseException as exc:
            errors.append(exc)

    def departing():
        try:
            rendezvous.wait(timeout=5)
            handle.abandoned_reason = "transport_cancelled"
            observations.append((handle.identity, handle.abandoned_reason))
        except BaseException as exc:
            errors.append(exc)

    threads = [Thread(target=publishing), Thread(target=departing)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert (("db", 7), "transport_cancelled") in observations
    assert all(identity in (None, ("db", 7)) for identity, _ in observations)


@pytest.mark.parametrize("reason,kind", [
    ("invalid_request", "invalid_request"),
    ("runtime_not_registered", "invalid_request"),
    ("provider_not_registered", "provider_unavailable"),
    ("provider_unavailable", "provider_unavailable"),
    ("runtime_provider_mismatch", "invalid_request"),
    ("no_viable_worker", "no_viable_worker"),
    ("cli_not_found", "failed_to_start"),
    ("launch_failed", "failed_to_start"),
    ("unsafe_shim_binary", "failed_to_start"),
    ("invalid_sandbox_mode", "invalid_request"),
    ("invalid_mode", "invalid_request"),
    ("invalid_reasoning_effort", "invalid_request"),
])
def test_prelaunch_failure_classification_remains_distinct(reason, kind):
    assert attempt_resolution(result(task_status_reason=reason)) == ("failed", kind)


@pytest.mark.parametrize("reason", [
    "routing_refused", "network_policy_refused", "read_only_resume_unpinned", "action_authority_unmapped",
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
    handle = CallerWaitHandle(("db", 7))
    handle.abandoned_reason = "transport_cancelled"
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
        "task_status", "task_status_reason", "safe_to_retry", "evidence", "limitations",
    }
    assert set(asdict(normalized)) == legacy_fields
    assert set(normalized.to_dict()) == {
        "provider", "success", "process_success", "output", "exit_code", "session_id", "error",
        "metadata", "task_status", "reason", "safe_to_retry", "task_completed", "limitations", "evidence",
    }


def test_clean_exit_without_envelope_remains_an_unknown_objective():
    status, reason, retry = classify_worker_result(returncode=0, combined_output="done", primary_output="done")
    normalized = result(task_status=status, task_status_reason=reason, safe_to_retry=retry)
    assert attempt_resolution(normalized) == ("completed", None)
    assert normalized.task_status is WorkerTaskStatus.UNKNOWN
    assert normalized.task_completed is False
    assert normalized.safe_to_retry is False
