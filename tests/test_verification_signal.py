# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Timing-only verification storage kept for a future verified isolated runner.

Assessor 3 runs no objective pass at all: ModelQuad never runs tests in the
working folder, so the signal is never raised. The history
recorders and their privacy boundary are still pinned here; the obsolete
live state and rung are removed.
"""

from __future__ import annotations

import sqlite3

import pytest

from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus
from orchestrator.mcp.livelifecycle import derive_live_state, facts_from_attempt
from orchestrator.outcome import VerificationSpec, attach_outcome_evidence
from orchestrator.service import db as service_db
from orchestrator.service import history as service_history


def _attempt_row(db_path, attempt_id):
    conn = service_db.connect_and_init(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM delegation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def test_requested_tests_raise_no_verification_signal(tmp_path):
    """A completed claim naming tests is not "Verifying": nothing is verified.

    This used to bracket a real pytest run in the worker's folder. That run
    is gone, so the callback is never invoked and the attempt
    row never says verification started.
    """
    db_path = tmp_path / "history.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    attempt_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", requested_provider="codex"
    )
    assert attempt_id is not None

    seen: list[str] = []

    def on_verification(phase: str) -> None:
        seen.append(phase)
        if phase == "started":
            service_history.record_verification_started(db_path, attempt_id)
        else:
            service_history.record_verification_resolved(db_path, attempt_id)

    result = ExecutionResult(
        provider="codex",
        success=True,
        output="",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
    )
    finished = attach_outcome_evidence(
        result,
        working_directory=workspace,
        spec=VerificationSpec(relevant_tests=("test_sample.py",)),
        on_verification=on_verification,
    )

    assert seen == []
    row = _attempt_row(db_path, attempt_id)
    assert row["verification_started_at"] is None
    assert row["verification_resolved_at"] is None
    assert derive_live_state(facts_from_attempt(row)).state != "verifying"
    kinds = [i.kind for i in finished.evidence.evidence_items]
    assert "requested_tests_not_verified" in kinds


def test_no_objective_pass_means_no_verification_signal(tmp_path):
    """A worker that never claimed completion is never "Verifying"."""
    seen: list[str] = []
    result = ExecutionResult(
        provider="codex",
        success=False,
        output="",
        exit_code=1,
        task_status=WorkerTaskStatus.FAILED,
    )
    attach_outcome_evidence(
        result,
        working_directory=tmp_path,
        spec=VerificationSpec(relevant_tests=("test_sample.py",)),
        on_verification=lambda phase: seen.append(phase),
    )
    assert seen == []


def test_a_raising_callback_never_breaks_the_delegation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    def boom(phase: str) -> None:
        raise RuntimeError("observability exploded")

    result = ExecutionResult(
        provider="codex",
        success=True,
        output="",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
    )
    finished = attach_outcome_evidence(
        result,
        working_directory=workspace,
        spec=VerificationSpec(relevant_tests=("test_sample.py",)),
        on_verification=boom,
    )
    assert finished.evidence is not None


def test_the_signal_persists_timing_only(tmp_path):
    """Privacy boundary: no command, prompt, path or content is stored."""
    db_path = tmp_path / "history.db"
    attempt_id = service_history.record_delegation_attempt(
        db_path, task_type="implementation", requested_provider="codex"
    )
    service_history.record_verification_started(db_path, attempt_id)
    service_history.record_verification_resolved(db_path, attempt_id)

    row = _attempt_row(db_path, attempt_id)
    for column in ("verification_started_at", "verification_resolved_at"):
        value = row[column]
        # An ISO-8601 instant and nothing else.
        assert value is not None and value.startswith("20") and "T" in value
        assert "\\" not in value and "/" not in value and " " not in value
