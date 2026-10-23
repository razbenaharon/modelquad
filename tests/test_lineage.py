# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Route → execution lineage (V8 slice 3, P0).

The decision id a `modelquad_route` consultation returns must survive into
the delegated execution that carries it out, so the candidate set and quota
evidence that CAUSED an execution stay joined to its outcome.
"""

from __future__ import annotations

from orchestrator.config import Config
from orchestrator.lineage import (
    REFUSAL_TASK_MISMATCH,
    REFUSAL_UNKNOWN_DECISION,
    resolve_delegation_lineage,
)
from orchestrator.models import Freshness, ProviderStatus
from orchestrator.provenance import evidence_snapshot_json, new_decision_id
from orchestrator.router import Router
from orchestrator.service import history as service_history


def _statuses():
    return [
        ProviderStatus(name="claude", available=True, remaining_percentage=60.0, freshness=Freshness.FRESH),
        ProviderStatus(name="codex", available=True, remaining_percentage=90.0, freshness=Freshness.FRESH),
    ]


def _record_route(db, task_type="implementation", decision_id=None, **kwargs):
    config = Config()
    statuses = _statuses()
    decision_id = decision_id or new_decision_id()
    result = Router(config).route(task_type, statuses)
    service_history.record_routing_decision(
        db,
        task_type,
        result,
        source="mcp",
        decision_id=decision_id,
        evidence_json=evidence_snapshot_json(statuses, config),
        **kwargs,
    )
    return decision_id, result


def test_prior_route_decision_id_is_reused_not_reminted(tmp_path):
    db = tmp_path / "h.db"
    decision_id, result = _record_route(db)

    lineage = resolve_delegation_lineage(
        db, decision_id=decision_id, task_type="implementation"
    )
    assert lineage.valid is True
    assert lineage.decision_id == decision_id  # the SAME identity, not a new one
    assert lineage.decision_id_source == "caller_supplied"
    assert lineage.prior_route is True
    assert lineage.recommended_provider == result.recommendation


def test_direct_delegation_without_prior_route_is_honest(tmp_path):
    db = tmp_path / "h.db"
    lineage = resolve_delegation_lineage(
        db, decision_id=None, task_type="implementation"
    )
    assert lineage.valid is True
    assert lineage.decision_id_source == "minted"
    # It must NOT pretend an advisory decision existed.
    assert lineage.prior_route is False
    assert lineage.recommended_provider is None
    assert "no prior routing decision" in lineage.notes[0]


def test_unknown_decision_id_is_refused_not_absorbed(tmp_path):
    db = tmp_path / "h.db"
    lineage = resolve_delegation_lineage(
        db, decision_id="does-not-exist", task_type="implementation"
    )
    assert lineage.valid is False
    assert lineage.refusal_reason == REFUSAL_UNKNOWN_DECISION
    # The claimed id is echoed, never silently replaced with a fresh one.
    assert lineage.decision_id == "does-not-exist"


def test_task_type_mismatch_is_refused(tmp_path):
    db = tmp_path / "h.db"
    decision_id, _ = _record_route(db, task_type="implementation")
    lineage = resolve_delegation_lineage(
        db, decision_id=decision_id, task_type="testing"
    )
    assert lineage.valid is False
    assert lineage.refusal_reason == REFUSAL_TASK_MISMATCH
    assert "implementation" in lineage.notes[0]


def test_user_override_preserves_both_recommended_and_actual(tmp_path):
    db = tmp_path / "h.db"
    decision_id, result = _record_route(db)
    recommended = result.recommendation
    other = "claude" if recommended != "claude" else "codex"

    lineage = resolve_delegation_lineage(
        db, decision_id=decision_id, task_type="implementation", requested_provider=other
    )
    assert lineage.is_override is True
    assert lineage.recommended_provider == recommended
    assert lineage.requested_provider == other
    resolved = lineage.resolved(actual_provider=other, actual_runtime=other)
    # BOTH facts survive: what was advised and what actually ran.
    assert resolved["recommended_provider"] == recommended
    assert resolved["actual_provider"] == other
    assert resolved["target_match"] == "override"
    assert any("explicitly requested" in note for note in resolved["notes"])


def test_target_drift_is_recorded_not_reconciled(tmp_path):
    db = tmp_path / "h.db"
    decision_id, result = _record_route(db)
    recommended = result.recommendation
    drifted = "claude" if recommended != "claude" else "codex"

    lineage = resolve_delegation_lineage(
        db, decision_id=decision_id, task_type="implementation"
    )
    # No explicit override, but a fallback executed somewhere else.
    resolved = lineage.resolved(actual_provider=drifted, actual_runtime=drifted)
    assert resolved["target_match"] == "drift"
    assert resolved["recommended_provider"] == recommended
    assert resolved["actual_provider"] == drifted


def test_matching_target_is_reported_as_match(tmp_path):
    db = tmp_path / "h.db"
    decision_id, result = _record_route(db)
    lineage = resolve_delegation_lineage(
        db, decision_id=decision_id, task_type="implementation"
    )
    resolved = lineage.resolved(
        actual_provider=result.recommendation, actual_runtime=result.recommendation
    )
    assert resolved["target_match"] == "match"


def test_one_decision_supports_many_execution_attempts(tmp_path):
    from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus

    db = tmp_path / "h.db"
    decision_id, _ = _record_route(db)

    first = resolve_delegation_lineage(
        db, decision_id=decision_id, task_type="implementation"
    )
    assert first.attempt_index == 0

    service_history.record_execution(
        db,
        "implementation",
        ExecutionResult(
            provider="codex",
            success=False,
            output="",
            exit_code=None,
            task_status=WorkerTaskStatus.BLOCKED,
            task_status_reason="permission_required",
        ),
        decision_id=decision_id,
    )

    second = resolve_delegation_lineage(
        db, decision_id=decision_id, task_type="implementation"
    )
    # The retry appends; it does not overwrite or invalidate the first.
    assert second.valid is True
    assert second.attempt_index == 1
    assert "this is attempt 2" in second.notes[0]

    service_history.record_execution(
        db,
        "implementation",
        ExecutionResult(
            provider="claude",
            success=True,
            output="",
            exit_code=0,
            task_status=WorkerTaskStatus.COMPLETED,
        ),
        decision_id=decision_id,
    )
    assert service_history.count_executions_for_decision(db, decision_id) == 2


def test_lineage_lookup_survives_a_missing_database(tmp_path):
    lineage = resolve_delegation_lineage(
        tmp_path / "nope" / "missing.db",
        decision_id="abc",
        task_type="implementation",
    )
    # An unreadable history refuses rather than fabricating a prior route.
    assert lineage.valid is False
    assert lineage.refusal_reason == REFUSAL_UNKNOWN_DECISION
