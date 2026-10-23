# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Persistence tests: decision mode, plan decisions, capacity metrics, v9 migration."""

from __future__ import annotations

import sqlite3

import pytest

from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus
from orchestrator.service import db as service_db
from orchestrator.service import history as service_history


def _record_execution(db_path, *, provider, host=None, task_status="completed", fallback=False):
    service_history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(
            provider=provider,
            success=True,
            output="",
            exit_code=0,
            task_status=WorkerTaskStatus(task_status),
            metadata={"fallback": fallback} if fallback else {},
        ),
        source="mcp",
        fallback=fallback,
        host=host,
    )


def test_decision_mode_defaults_to_deterministic_and_roundtrips(tmp_path):
    db_path = tmp_path / "history.db"
    assert service_history.get_decision_mode(db_path) == "deterministic"
    assert service_history.put_decision_mode(db_path, "reasoning_shadow") == "reasoning_shadow"
    assert service_history.get_decision_mode(db_path) == "deterministic"
    with pytest.raises(ValueError):
        service_history.put_decision_mode(db_path, "vibes")
    assert service_history.get_decision_mode(db_path) == "deterministic"


def test_plan_decision_roundtrip_preserves_provenance(tmp_path):
    db_path = tmp_path / "history.db"
    service_history.record_plan_decision(
        db_path,
        task_type="implementation",
        decision_mode="reasoning_shadow",
        planner_provider="zeta",
        planner_model=None,
        planner_model_source="unknown",
        reasoning_source="delegated",
        strategy="multi_stage",
        confidence=0.7,
        steps=[
            {"role": "explore", "provider": "zeta"},
            {"role": "implement", "provider": "codex"},
        ],
        reason_summary="abundant capacity",
        validation_passed=True,
        validation_errors=[],
        executed=False,
        fallback_occurred=False,
        deterministic_recommendation="codex",
        agreement=None,
        packet={"providers": []},
        host="claude-code",
        invocation_mode="automatic",
    )
    rows = service_history.fetch_plan_decisions(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["planner_provider"] == "zeta"
    assert row["reasoning_source"] == "delegated"
    assert row["strategy"] == "multi_stage"
    assert row["steps"][1]["provider"] == "codex"
    assert row["validation_passed"] is True
    assert row["executed"] is False
    assert row["fallback_occurred"] is False
    assert row["agreement"] is None
    assert row["packet"] == {"providers": []}


def test_fallback_reason_is_recorded(tmp_path):
    db_path = tmp_path / "history.db"
    service_history.record_plan_decision(
        db_path,
        task_type="review",
        decision_mode="reasoning_primary",
        fallback_occurred=True,
        fallback_reason="malformed_output: planner response contained no plan",
    )
    row = service_history.fetch_plan_decisions(db_path)[0]
    assert row["fallback_occurred"] is True
    assert row["fallback_reason"].startswith("malformed_output")


def test_capacity_metrics_are_defensible_and_never_invent_token_savings(tmp_path):
    db_path = tmp_path / "history.db"
    _record_execution(db_path, provider="codex", host="claude-code")
    _record_execution(db_path, provider="claude", host="claude-code")
    _record_execution(db_path, provider="zeta", host="mystery-host")
    _record_execution(db_path, provider="codex", host=None, task_status="failed", fallback=True)

    metrics = service_history.fetch_capacity_metrics(db_path)
    assert metrics["executions_total"] == 4
    assert metrics["capacity_distribution"] == {"zeta": 1, "claude": 1, "codex": 2}
    # workload shifted: only host-classifiable rows count; claude-code host
    # executing codex counts as shifted, claude does not, mystery/None excluded
    assert metrics["workload_shifted"]["tasks"] == 1
    assert metrics["workload_shifted"]["classifiable_tasks"] == 2
    assert metrics["workload_shifted"]["estimated"] is True
    assert metrics["completion_rate"] == 0.75
    assert metrics["fallback_rate"] == 0.25
    # no token accounting exists, so no token claim may exist anywhere
    assert "tokens_saved" not in str(metrics)
    assert metrics["capacity_preserved"]["value"] is None
    assert "does not invent" in metrics["capacity_preserved"]["reason"]


def test_unknown_reset_state_cannot_claim_expiring_capacity_use(tmp_path):
    db_path = tmp_path / "history.db"
    _record_execution(db_path, provider="codex", host="claude-code")
    metrics = service_history.fetch_capacity_metrics(db_path)
    assert metrics["expiring_capacity_utilized"]["value"] is None
    assert "objectively known" in metrics["expiring_capacity_utilized"]["reason"]


def test_shadow_agreement_rate_computed_from_recorded_comparisons(tmp_path):
    db_path = tmp_path / "history.db"
    for agreement in (True, True, False):
        service_history.record_plan_decision(
            db_path,
            task_type="implementation",
            decision_mode="reasoning_shadow",
            deterministic_recommendation="codex",
            agreement=agreement,
        )
    service_history.record_plan_decision(
        db_path,
        task_type="implementation",
        decision_mode="reasoning_primary",
        executed=True,
    )
    metrics = service_history.fetch_capacity_metrics(db_path)
    assert metrics["plan_decisions_total"] == 4
    assert metrics["shadow_agreement_rate"] == pytest.approx(2 / 3)


def test_capacity_metrics_report_planner_usage_validity_and_stage_distribution(tmp_path):
    db_path = tmp_path / "history.db"
    service_history.record_plan_decision(
        db_path,
        task_type="implementation",
        decision_mode="reasoning_primary",
        planner_provider="zeta",
        validation_passed=True,
        executed=True,
        steps=[
            {
                "role": "explore",
                "provider": "zeta",
                "execution": {"task_status": "completed", "replanned": False,
                              "replanned_provider": None},
            },
            {
                "role": "implement",
                "provider": "zeta",
                # deterministically re-routed: the stage RAN on codex
                "execution": {"task_status": "completed", "replanned": True,
                              "replanned_provider": "codex"},
            },
            # planned but never run -- must NOT count as an executed stage
            {"role": "review", "provider": "claude"},
        ],
        final_status="completed",
    )
    service_history.record_plan_decision(
        db_path,
        task_type="review",
        decision_mode="reasoning_shadow",
        planner_provider="zeta",
        validation_passed=False,
        validation_errors=["quality floor"],
    )
    service_history.record_plan_decision(
        db_path,
        task_type="review",
        decision_mode="reasoning_primary",
        fallback_occurred=True,
        fallback_reason="planner_timeout: slow",
    )
    metrics = service_history.fetch_capacity_metrics(db_path)
    assert metrics["planner_usage"] == {"zeta": 2}
    assert metrics["plan_validity_rate"] == 0.5  # only decisions the validator judged
    # only stages that actually RAN count, attributed to the provider that
    # really performed them (review finding M5)
    assert metrics["plan_stage_distribution"] == {"zeta": 1, "codex": 1}


def test_persisted_planner_prose_is_length_bounded(tmp_path):
    """Planner-authored free text is bounded at the persistence choke point
    so a verbose planner can never smuggle unbounded prose into history
    (review finding M2)."""

    db_path = tmp_path / "history.db"
    service_history.record_plan_decision(
        db_path,
        task_type="implementation",
        decision_mode="reasoning_shadow",
        strategy="delegate",
        steps=[{
            "role": "work",
            "provider": "codex",
            "objective": "x" * 5000,
            "success_criteria": "y" * 5000,
        }],
        reason_summary="z" * 5000,
        fallback_reason="w" * 5000,
    )
    row = service_history.fetch_plan_decisions(db_path)[0]
    assert len(row["steps"][0]["objective"]) <= 510
    assert len(row["steps"][0]["success_criteria"]) <= 510
    assert len(row["reason_summary"]) <= 1010
    assert len(row["fallback_reason"]) <= 510
    assert row["steps"][0]["provider"] == "codex"  # structure untouched


def test_fetch_single_plan_decision_roundtrip(tmp_path):
    db_path = tmp_path / "history.db"
    service_history.record_plan_decision(
        db_path,
        task_type="implementation",
        decision_mode="reasoning_shadow",
        strategy="delegate",
        steps=[{"role": "work", "provider": "codex"}],
    )
    row_id = service_history.fetch_plan_decisions(db_path)[0]["id"]
    entry = service_history.fetch_plan_decision(db_path, row_id)
    assert entry["strategy"] == "delegate"
    assert entry["steps"][0]["provider"] == "codex"
    assert service_history.fetch_plan_decision(db_path, 424242) is None


