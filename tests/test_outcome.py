# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Outcome assessment: baseline promotion, requested tests recorded as not
verified, host outcomes.

Assessor 3 never runs tests in the working folder; the cases that
proved it with recorded process launches are in
``tests/test_no_live_folder_tests.py``.
"""

from __future__ import annotations

import pytest

from modelquad.execution.base import (
    EvidenceItem,
    ExecutionResult,
    OutcomeEvidence,
    WorkerTaskStatus,
)
from modelquad.outcome import (
    ASSESSOR_VERSION,
    VerificationSpec,
    _classify_pytest_exit,
    assess_host_outcome,
    attach_outcome_evidence,
    build_baseline_evidence,
    build_host_outcome_result,
    finalize_execution_result,
)


def _result(**kwargs) -> ExecutionResult:
    defaults = dict(
        provider="codex",
        success=True,
        output="done",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        task_status_reason="all steps finished",
    )
    defaults.update(kwargs)
    return ExecutionResult(**defaults)


def _write_passing_workspace(tmp_path):
    (tmp_path / "test_target.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    return tmp_path


def _write_failing_workspace(tmp_path):
    (tmp_path / "test_target.py").write_text(
        "def test_broken():\n    assert 1 + 1 == 3\n", encoding="utf-8"
    )
    return tmp_path


# --- baseline promotion ----------------------------------------------------


def test_baseline_envelope_claim_is_semantic_self_report_not_objective():
    evidence = build_baseline_evidence(_result())
    assert evidence.verification_level == "semantic"
    assert evidence.verification_confidence == "low"
    assert evidence.assessor_version == ASSESSOR_VERSION
    assert evidence.semantic_status == "completed"
    assert evidence.process_success is True
    # The claim item is self-reported; the exit code item is objective.
    sources = {item.kind: item.source for item in evidence.evidence_items}
    assert sources["completion_claim"] == "worker_self_reported"
    assert sources["process_exit"] == "objective"
    # Semantic self-report NEVER counts as verified.
    assert evidence.verified is False


def test_baseline_exit_code_only_is_process_level():
    res = _result(
        exit_code=3,
        success=False,
        task_status=WorkerTaskStatus.FAILED,
        task_status_reason="process_exit_3",
    )
    evidence = build_baseline_evidence(res)
    assert evidence.verification_level == "process"
    assert evidence.process_success is False
    # A non-zero exit the modelquad itself observed: objective failure fact.
    assert evidence.verification_confidence == "medium"
    kinds = [item.kind for item in evidence.evidence_items]
    assert kinds == ["process_exit"]


def test_baseline_nothing_observed_is_none_level():
    res = _result(
        exit_code=None,
        success=False,
        task_status=WorkerTaskStatus.UNKNOWN,
        task_status_reason=None,
    )
    evidence = build_baseline_evidence(res)
    assert evidence.verification_level == "none"
    assert evidence.verification_confidence == "none"
    assert evidence.process_success is None
    assert evidence.evidence_items == ()


def test_semantic_success_vs_process_success_are_independent():
    # Exit 0 but the worker honestly reported failure: process ok, task not.
    res = _result(task_status=WorkerTaskStatus.FAILED, task_status_reason="tests still red")
    evidence = build_baseline_evidence(res)
    assert evidence.process_success is True
    assert evidence.semantic_status == "failed"
    assert res.task_completed is False


# --- requested tests are recorded as not verified (assessor 3) ---------------


def test_requested_tests_on_a_completed_claim_are_recorded_as_not_verified(tmp_path):
    _write_passing_workspace(tmp_path)
    spec = VerificationSpec(relevant_tests=("test_target.py",), timeout=120.0)
    attached = attach_outcome_evidence(_result(), working_directory=tmp_path, spec=spec)
    evidence = attached.evidence
    assert evidence is not None
    # Nothing was run, so nothing is promoted: the claim stays a claim.
    assert evidence.verification_level == "semantic"
    assert evidence.tests_executed is False
    assert evidence.tests_passed is None
    assert evidence.verification_confidence == "low"
    assert evidence.test_runtime_seconds is None
    assert evidence.verified is False
    assert attached.verified_completed is False  # COMPLETED_UNVERIFIED
    kinds = {item.kind: item.source for item in evidence.evidence_items}
    assert kinds["requested_tests_not_verified"] == "inferred"
    assert "requested_tests_passed" not in kinds


def test_a_false_completion_claim_is_not_contradicted_because_nothing_ran(tmp_path):
    # The failing test is there, but ModelQuad does not run it in the live
    # folder: the claim is neither confirmed nor refuted, and the Host checks.
    _write_failing_workspace(tmp_path)
    spec = VerificationSpec(relevant_tests=("test_target.py",), timeout=120.0)
    attached = attach_outcome_evidence(_result(), working_directory=tmp_path, spec=spec)
    evidence = attached.evidence
    assert evidence.verification_level == "semantic"
    assert evidence.tests_passed is None  # UNKNOWN, never a confident False
    assert evidence.verified is False
    assert attached.verified_completed is False
    assert evidence.semantic_status == "completed"


def test_no_verification_without_a_completion_claim(tmp_path):
    _write_passing_workspace(tmp_path)
    spec = VerificationSpec(relevant_tests=("test_target.py",))
    res = _result(
        task_status=WorkerTaskStatus.FAILED,
        task_status_reason="worker reported failure",
        exit_code=0,
    )
    attached = attach_outcome_evidence(res, working_directory=tmp_path, spec=spec)
    # Verifying a failure claim proves nothing about the worker; level stays semantic.
    assert attached.evidence.verification_level == "semantic"
    assert attached.evidence.tests_executed is None


def test_a_missing_working_directory_is_not_verified_the_same_way(tmp_path):
    spec = VerificationSpec(relevant_tests=("test_target.py",))
    missing = tmp_path / "does-not-exist"
    attached = attach_outcome_evidence(_result(), working_directory=missing, spec=spec)
    evidence = attached.evidence
    assert evidence.verification_level == "semantic"  # unchanged
    assert evidence.tests_passed is None  # UNKNOWN, never a confident False
    kinds = [item.kind for item in evidence.evidence_items]
    assert "requested_tests_not_verified" in kinds
    assert "requested_tests_unrunnable" not in kinds


def test_spec_rejects_flag_like_test_targets():
    with pytest.raises(ValueError, match="invalid test target"):
        VerificationSpec(relevant_tests=("--exitfirst",))
    with pytest.raises(ValueError, match="timeout"):
        VerificationSpec(relevant_tests=("test_x.py",), timeout=0)


def test_existing_evidence_is_never_clobbered(tmp_path):
    supplied = OutcomeEvidence(verification_level="objective", tests_passed=True)
    res = _result(evidence=supplied)
    attached = attach_outcome_evidence(
        res,
        working_directory=tmp_path,
        spec=VerificationSpec(relevant_tests=("test_target.py",)),
    )
    assert attached.evidence is supplied


def test_finalize_records_no_environment_snapshot_and_never_raises(tmp_path):
    finalized = finalize_execution_result(_result(), working_directory=tmp_path)
    assert "environment" not in finalized.metadata
    assert finalized.evidence is not None


# --- pytest exit codes map to buckets, they are not thresholded -------------
#
# `_classify_pytest_exit` is pure and is kept for a future verified isolated
# runner; nothing calls pytest today.


@pytest.mark.parametrize(
    ("returncode", "expected_passed", "expected_kind"),
    [
        (0, True, "requested_tests_passed"),
        (1, False, "requested_tests_failed"),
        (2, None, "requested_tests_unrunnable"),
        (3, None, "requested_tests_unrunnable"),
        (4, None, "requested_tests_unrunnable"),
        (5, None, "requested_tests_unrunnable"),
        # Not a pytest exit code at all (a signal, a crashed interpreter): the
        # honest reading is that the check did not run, not that it failed.
        (-9, None, "requested_tests_unrunnable"),
        (137, None, "requested_tests_unrunnable"),
    ],
)
def test_every_pytest_exit_code_maps_to_its_documented_bucket(
    returncode, expected_passed, expected_kind
):
    passed, item = _classify_pytest_exit(returncode, "", ("test_x.py",))
    assert passed is expected_passed
    assert item.kind == expected_kind
    # Only a verdict the modelquad actually observed is "objective".
    assert item.source == ("objective" if returncode in (0, 1) else "inferred")


def test_missing_pytest_exits_one_but_is_unrunnable_not_failed():
    # `python -m pytest` with no pytest installed exits 1 -- the same code as
    # "tests failed". A missing verifier is a context failure, never a result.
    passed, item = _classify_pytest_exit(
        1, "python.exe: No module named pytest\n", ("test_x.py",)
    )
    assert passed is None
    assert item.kind == "requested_tests_unrunnable"


def test_finalize_survives_an_assessment_that_explodes(tmp_path, monkeypatch):
    # Acceptance: assessment is additive and can never fail a delegation.
    _write_passing_workspace(tmp_path)

    def _explode(*args, **kwargs):
        raise RuntimeError("assessor is on fire")

    monkeypatch.setattr("modelquad.outcome.attach_outcome_evidence", _explode)
    finalized = finalize_execution_result(
        _result(),
        working_directory=tmp_path,
        spec=VerificationSpec(relevant_tests=("test_target.py",)),
    )
    assert finalized.task_status is WorkerTaskStatus.COMPLETED
    assert finalized.error is None


# --- evidence item provenance ----------------------------------------------


def test_evidence_item_rejects_unknown_source():
    with pytest.raises(ValueError, match="source"):
        EvidenceItem(kind="test_passed", source="probably_fine")


def test_worker_self_report_cannot_be_objective():
    # There is no code path that maps a claim to source="objective"; the claim
    # item's source is fixed by the assessor from who made the claim.
    evidence = build_baseline_evidence(_result())
    claim_items = [i for i in evidence.evidence_items if i.kind == "completion_claim"]
    assert claim_items and all(i.source == "worker_self_reported" for i in claim_items)


# --- host outcomes (STAY_WITH_HOST) ----------------------------------------


def test_host_outcome_self_report_is_semantic_with_unknown_process():
    result = assess_host_outcome(task_status="completed", reported_model="opus")
    assert result.provider == "claude"
    assert result.exit_code is None
    evidence = result.evidence
    assert evidence.process_success is None  # no process was observed -- UNKNOWN
    assert evidence.verification_level == "semantic"
    assert evidence.verified is False
    assert result.metadata["host_outcome"] is True
    assert result.metadata["reported_model"] == "opus"
    assert result.metadata["model_identity_source"] == "unknown"


def test_host_outcome_with_tests_is_recorded_as_not_verified(tmp_path):
    _write_passing_workspace(tmp_path)
    result = assess_host_outcome(
        task_status="completed",
        working_directory=tmp_path,
        relevant_tests=("test_target.py",),
        reported_model="opus",
        model_identity_source="reported",
    )
    assert result.verified_completed is False
    assert result.evidence.verification_level == "semantic"
    assert result.evidence.tests_executed is False
    assert result.evidence.tests_passed is None
    kinds = [i.kind for i in result.evidence.evidence_items]
    assert "requested_tests_not_verified" in kinds
    assert result.metadata["model_identity_source"] == "reported"


def test_host_outcome_user_report_is_tagged_user_reported():
    result = assess_host_outcome(task_status="completed", reported_by="user")
    sources = {i.kind: i.source for i in result.evidence.evidence_items}
    assert sources["completion_claim"] == "user_reported"


def test_host_outcome_unknown_status_is_legal_and_stays_unknown():
    result = assess_host_outcome(task_status="unknown")
    assert result.task_status is WorkerTaskStatus.UNKNOWN
    assert result.evidence.semantic_status == "unknown"


def test_host_outcome_validates_inputs():
    with pytest.raises(ValueError, match="task_status"):
        build_host_outcome_result(task_status="finished")
    with pytest.raises(ValueError, match="model_identity_source"):
        build_host_outcome_result(task_status="completed", model_identity_source="vibes")
    with pytest.raises(ValueError, match="reported_by"):
        build_host_outcome_result(task_status="completed", reported_by="the_cat")
