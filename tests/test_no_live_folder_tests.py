# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""ModelQuad never runs tests in the live working folder.

Assessor 2 re-ran the caller-named tests itself, with ``cwd`` set to the
folder the Host handed over -- the user's LIVE folder -- and no isolation: no
copy, no command allowlist, no network control. Running a test suite executes
the project's own code (``conftest.py``, fixtures, test modules) with the
user's privileges and network access. ModelQuad may run tests only
inside a verified isolated environment, and when none is available it returns
"not verified" and the Host checks. None is available yet.

Every case below replaces the process launchers with recorders, so a run that
would have happened is caught as a recorded call rather than inferred from a
verdict. Each case failed on ``main`` at 79f1c51, where the requested test was
run and graded objective.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import modelquad.procutil as procutil
from modelquad.config import Config
from modelquad.execution import ExecutionManager
from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
from modelquad.outcome import (
    ASSESSOR_VERSION,
    VerificationSpec,
    assess_host_outcome,
    attach_outcome_evidence,
    build_baseline_evidence,
    build_host_outcome_result,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "modelquad"

NOT_VERIFIED = "requested_tests_not_verified"


@pytest.fixture()
def launches(monkeypatch):
    """Record every process ModelQuad tries to start, and start none.

    The recorders answer like a green pytest run, so code that still launched
    a verifier would grade the claim objective and fail these tests twice:
    once for the recorded call and once for the verdict.
    """
    calls: list[tuple[str, tuple, dict]] = []

    def _completed(name):
        def _record(*args, **kwargs):
            calls.append((name, args, kwargs))
            argv = args[0] if args else kwargs.get("args")
            return subprocess.CompletedProcess(argv, 0, stdout="1 passed\n", stderr="")

        return _record

    def _popen(*args, **kwargs):
        calls.append(("subprocess.Popen", args, kwargs))
        raise OSError("process launch recorded and refused by the test")

    monkeypatch.setattr(procutil, "run_with_tree_kill", _completed("run_with_tree_kill"))
    monkeypatch.setattr(subprocess, "run", _completed("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _popen)
    return calls


def _passing_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "live-folder"
    workspace.mkdir()
    (workspace / "test_target.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    # If anything ran the suite, this is how the test would know: conftest is
    # the project's own code, executed on collection.
    (workspace / "conftest.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('RAN').write_text('project code ran')\n",
        encoding="utf-8",
    )
    return workspace


def _completed_result(**kwargs) -> ExecutionResult:
    defaults = dict(
        provider="codex",
        success=True,
        output="done",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        task_status_reason="worker envelope: task done",
    )
    defaults.update(kwargs)
    return ExecutionResult(**defaults)


def _not_verified_items(evidence):
    return [i for i in evidence.evidence_items if i.kind == NOT_VERIFIED]


def _assert_not_verified(evidence, *, targets: int, closed: bool = False) -> None:
    items = _not_verified_items(evidence)
    assert len(items) == 1
    item = items[0]
    assert item.source == "inferred"
    # A read-only delegation's result is a closed projection: the label and
    # source survive, the prose detail does not.
    assert item.detail == (None if closed else (
        "not verified: no verified isolated environment is available, so "
        f"ModelQuad did not run the {targets} requested test target(s); "
        "the Host checks"
    ))
    assert evidence.tests_executed is False
    assert evidence.tests_passed is None
    assert evidence.verified is False
    # Nothing an objective pass would add is there.
    kinds = {i.kind for i in evidence.evidence_items}
    assert not kinds & {
        "requested_tests_passed",
        "requested_tests_failed",
        "requested_tests_unrunnable",
        "verification_context",
    }


# --------------------------------------------------------------------------
# The regression: a delegated worker's completion claim
# --------------------------------------------------------------------------


def test_a_completed_claim_with_requested_tests_starts_no_process(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)
    result = _completed_result()
    spec = VerificationSpec(relevant_tests=("test_target.py",), timeout=120.0)

    attached = attach_outcome_evidence(result, working_directory=workspace, spec=spec)

    assert launches == []
    assert not (workspace / "RAN").exists()
    evidence = attached.evidence
    _assert_not_verified(evidence, targets=1)
    assert attached.verified_completed is False
    # Level and confidence stay at the baseline: a self-reported claim.
    baseline = build_baseline_evidence(result)
    assert evidence.verification_level == baseline.verification_level == "semantic"
    assert evidence.verification_confidence == baseline.verification_confidence == "low"
    assert evidence.test_runtime_seconds is None
    assert evidence.assessor_version == ASSESSOR_VERSION == "3"


def test_a_partial_claim_is_recorded_as_not_verified_too(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)
    spec = VerificationSpec(relevant_tests=("test_target.py", "test_other.py::test_x"))

    evidence = attach_outcome_evidence(
        _completed_result(task_status=WorkerTaskStatus.PARTIAL),
        working_directory=workspace,
        spec=spec,
    ).evidence

    assert launches == []
    _assert_not_verified(evidence, targets=2)


def test_the_detail_carries_a_count_and_never_a_path_or_target(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)
    spec = VerificationSpec(relevant_tests=("tests/secret_module_name.py::test_one",))

    evidence = attach_outcome_evidence(
        _completed_result(), working_directory=workspace, spec=spec
    ).evidence

    detail = _not_verified_items(evidence)[0].detail
    assert "secret_module_name" not in detail
    assert str(workspace) not in detail
    assert str(tmp_path) not in detail


def test_a_folder_that_does_not_exist_is_not_verified_the_same_way(tmp_path, launches):
    """The "working directory does not exist" branch went away with the run:
    there is no folder check, because nothing is run in any folder."""
    spec = VerificationSpec(relevant_tests=("test_target.py",))

    evidence = attach_outcome_evidence(
        _completed_result(), working_directory=tmp_path / "gone", spec=spec
    ).evidence

    assert launches == []
    _assert_not_verified(evidence, targets=1)
    assert "does not exist" not in " ".join(i.detail or "" for i in evidence.evidence_items)


def test_the_verification_callback_is_never_invoked(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)
    seen: list[str] = []

    attach_outcome_evidence(
        _completed_result(),
        working_directory=workspace,
        spec=VerificationSpec(relevant_tests=("test_target.py",)),
        on_verification=seen.append,
    )

    assert seen == []
    assert launches == []


# --------------------------------------------------------------------------
# Unchanged: no spec, or no completion claim, is exactly today's baseline
# --------------------------------------------------------------------------


def test_without_a_spec_the_evidence_is_exactly_the_baseline(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)
    result = _completed_result()

    for spec in (None, VerificationSpec(relevant_tests=())):
        evidence = attach_outcome_evidence(
            result, working_directory=workspace, spec=spec
        ).evidence
        assert evidence == build_baseline_evidence(result)
        assert _not_verified_items(evidence) == []
        assert evidence.tests_executed is None

    assert launches == []


@pytest.mark.parametrize(
    "status",
    [WorkerTaskStatus.FAILED, WorkerTaskStatus.BLOCKED, WorkerTaskStatus.UNKNOWN],
)
def test_without_a_completion_claim_the_evidence_is_exactly_the_baseline(
    tmp_path, launches, status
):
    workspace = _passing_workspace(tmp_path)
    result = _completed_result(task_status=status, task_status_reason="worker said so")

    evidence = attach_outcome_evidence(
        result,
        working_directory=workspace,
        spec=VerificationSpec(relevant_tests=("test_target.py",)),
    ).evidence

    assert evidence == build_baseline_evidence(result)
    assert _not_verified_items(evidence) == []
    assert launches == []


# --------------------------------------------------------------------------
# Host outcomes: the Host's own live folder is never run either
# --------------------------------------------------------------------------


def test_a_host_outcome_with_relevant_tests_starts_no_process(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)

    result = assess_host_outcome(
        task_status="completed",
        working_directory=workspace,
        relevant_tests=("test_target.py",),
        reported_model="claude-opus-5",
        model_identity_source="reported",
        agent_version="2.1.246",
        agent_version_source="host_observed",
    )

    assert launches == []
    assert not (workspace / "RAN").exists()
    _assert_not_verified(result.evidence, targets=1)
    assert result.verified_completed is False

    # The rest of the record is what it was: the host's claim, unobserved
    # process facts, and the metadata the host supplied.
    unverified = assess_host_outcome(
        task_status="completed",
        reported_model="claude-opus-5",
        model_identity_source="reported",
        agent_version="2.1.246",
        agent_version_source="host_observed",
    )
    evidence = result.evidence
    assert evidence.verification_level == unverified.evidence.verification_level == "semantic"
    assert evidence.verification_confidence == unverified.evidence.verification_confidence
    assert evidence.process_success is None
    assert [i for i in evidence.evidence_items if i.kind != NOT_VERIFIED] == list(
        unverified.evidence.evidence_items
    )
    assert result.exit_code is None
    assert result.metadata == unverified.metadata
    assert result.metadata["host_outcome"] is True
    assert result.metadata["agent_version"] == "2.1.246"
    expected = build_host_outcome_result(
        task_status="completed",
        reported_model="claude-opus-5",
        model_identity_source="reported",
        agent_version="2.1.246",
        agent_version_source="host_observed",
    )
    assert result.task_status is expected.task_status
    assert result.task_status_reason == expected.task_status_reason


def test_a_user_reported_host_outcome_keeps_its_claim_tag(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)

    result = assess_host_outcome(
        task_status="completed",
        working_directory=workspace,
        relevant_tests=("test_target.py",),
        reported_by="user",
    )

    assert launches == []
    sources = {i.kind: i.source for i in result.evidence.evidence_items}
    assert sources["completion_claim"] == "user_reported"
    assert sources[NOT_VERIFIED] == "inferred"


# --------------------------------------------------------------------------
# Through the ExecutionManager, the path the MCP delegate tool takes
# --------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.requests = []

    def is_available(self):
        return True

    def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(
            provider=request.provider,
            success=True,
            output="did the thing",
            exit_code=0,
            task_status=WorkerTaskStatus.COMPLETED,
            task_status_reason="worker envelope: task done",
            # Supplied, so finalization has no `--version` probe to make and
            # every recorded launch below could only be a verifier.
            metadata={"agent_version": "0.0-test", "agent_version_source": "test"},
        )


def test_a_delegation_starts_no_verification_process(tmp_path, launches):
    workspace = _passing_workspace(tmp_path)
    fake = _FakeProvider(workspace)
    manager = ExecutionManager(Config(), executors={"codex": fake})
    seen: list[str] = []

    result = manager.delegate(
        "review",
        "fix it",
        str(workspace),
        provider="codex",
        verification=VerificationSpec(relevant_tests=("test_target.py",)),
        on_verification=seen.append,
    )

    assert len(fake.requests) == 1  # the worker did run
    assert launches == []
    assert seen == []
    assert not (workspace / "RAN").exists()
    _assert_not_verified(result.evidence, targets=1, closed=True)
    assert result.verified_completed is False
    payload = result.to_dict()["evidence"]
    assert payload["verified"] is False
    assert payload["tests_executed"] is False
    assert payload["tests_passed"] is None


# --------------------------------------------------------------------------
# The runner is gone, not merely unreachable
# --------------------------------------------------------------------------


def test_outcome_module_names_no_process_launcher():
    """Code, not prose: the module docstring may say "subprocesses"."""
    tree = ast.parse((SRC / "outcome.py").read_text(encoding="utf-8"))
    forbidden = {"subprocess", "run_with_tree_kill", "Popen"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names} & forbidden
        elif isinstance(node, ast.ImportFrom):
            found |= {alias.name for alias in node.names} & forbidden
            if node.module and node.module.split(".")[-1] in {"subprocess", "procutil"}:
                found.add(node.module)
        elif isinstance(node, ast.Name) and node.id in forbidden:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.add(node.attr)
    assert found == set()

    import modelquad.outcome as outcome

    assert not hasattr(outcome, "_run_requested_tests")


def test_the_modules_that_described_the_runner_no_longer_do():
    for relpath in ("execution/manager.py", "mcp/server.py"):
        text = (SRC / relpath).read_text(encoding="utf-8")
        assert "_run_requested_tests" not in text, relpath
        assert "re-running the caller-named tests itself" not in text, relpath
        assert "re-run caller-named tests" not in text, relpath
        assert "re-runs them itself" not in text, relpath
        assert "re-runs those tests itself" not in text, relpath


def test_the_privacy_inventory_names_the_detail_assessor_3_writes():
    """PRIVACY.md's history-database section is the privacy reader's list of
    what ``EvidenceItem.detail`` holds; it must name the not-verified detail
    assessor 3 writes."""
    inventory = (SRC.parents[1] / "PRIVACY.md").read_text(encoding="utf-8")
    assert NOT_VERIFIED in inventory
