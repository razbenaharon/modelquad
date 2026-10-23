# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Outcome assessment: promote existing execution facts into OutcomeEvidence,
and record honestly that a requested verification did not happen.

Before this module existed, ``OutcomeEvidence`` was a type nothing
constructed: every real execution persisted ``outcome_evidence_json = NULL``
and ``verified_completed`` was structurally always False. The facts were
already there — exit codes, completion envelopes, blocked-pattern matches,
fallback metadata — they were simply never assembled. This module assembles
them (the *baseline* pass, always on, zero new subprocesses). A worker's
claim can raise ``verification_level`` to ``semantic`` at most.

Assessor 3 never runs tests in the working folder. Assessor 2 re-ran the
caller-named tests itself, in the user's LIVE folder, with no isolation:
running a test suite executes the project's own code (``conftest.py``,
fixtures, test modules) with the user's privileges and network access.
ModelQuad may run tests only inside a verified isolated
workspace and execution environment, and none exists yet, so when the
caller's capsule names relevant tests the result records
``requested_tests_not_verified`` instead and the Host checks. Records written
by assessor 2 are not backfilled.

Two invariants, restated because they are the point:

    process exit code 0  !=  task success
    agent says "done"    !=  verified completion

Host outcomes (STAY_WITH_HOST): ``record_host_outcome`` gives a
task the router advised keeping with the current host the same outcome
treatment a delegated worker gets — a self-reported claim stays
``semantic``/BRONZE, and tests the host names are recorded as not verified,
exactly as a worker's are. Nothing about host execution is fabricated: there
is no observed process, so ``exit_code`` is None and ``process_success`` is
unknown, explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .execution.base import (
    EvidenceItem,
    ExecutionResult,
    OutcomeEvidence,
    WorkerTaskStatus,
)

# Bumped whenever assessment/verification semantics change, so a stored
# evidence record can always be matched to the rules that produced it.
# Distinct from the product version: the assessor can change independently.
ASSESSOR_VERSION = "3"

# Bounded: verification must never hang a delegation indefinitely. Carried on
# VerificationSpec for a future verified isolated runner; nothing runs today.
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 300.0

# task_status_reason prefix meaning the status was derived from the process
# exit alone (classify_worker_result step 1), not from a worker's envelope.
_PROCESS_REASON_PREFIX = "process_exit_"


@dataclass(frozen=True)
class VerificationSpec:
    """What the caller asked to have verified after an execution.

    ``relevant_tests`` are pytest targets (paths or node ids) from the
    caller's TaskCapsule. Entries that look like flags are rejected up front
    — a future runner would feed them to an argv, never a shell. Today they
    are only counted: assessor 3 runs none of them.
    """

    relevant_tests: tuple[str, ...] = ()
    timeout: float = DEFAULT_VERIFICATION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.relevant_tests, tuple):
            object.__setattr__(self, "relevant_tests", tuple(self.relevant_tests))
        for entry in self.relevant_tests:
            if not entry or entry.startswith("-"):
                raise ValueError(f"invalid test target: {entry!r}")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    @property
    def has_objective_checks(self) -> bool:
        return bool(self.relevant_tests)


def _confidence_for(level: str, *, objective_failure_observed: bool) -> str:
    """Deterministic categorical confidence. No invented numbers."""
    if level == "objective":
        return "high"
    if objective_failure_observed:
        # A non-zero exit the modelquad saw itself is an objective
        # observation of failure, even without scope verification.
        return "medium"
    if level == "semantic":
        return "low"
    if level == "process":
        return "medium" if objective_failure_observed else "low"
    return "none"


def build_baseline_evidence(
    result: ExecutionResult,
    *,
    claim_source: str = "worker_self_reported",
) -> OutcomeEvidence:
    """Assemble evidence from facts the execution already produced.

    Runs no subprocesses. ``claim_source`` is who made the completion claim:
    ``worker_self_reported`` for a delegated worker's envelope (and for a
    host agent reporting on its own work), ``user_reported`` when a human
    stated the outcome.
    """
    if claim_source not in EvidenceItem.VALID_SOURCES:
        raise ValueError(f"invalid claim_source: {claim_source!r}")

    metadata = result.metadata or {}
    items: list[EvidenceItem] = []

    process_success: bool | None = None
    if result.exit_code is not None:
        process_success = result.exit_code == 0
        items.append(
            EvidenceItem(
                kind="process_exit",
                source="objective",
                detail=f"exit_code={result.exit_code}",
            )
        )

    status = result.task_status
    reason = result.task_status_reason or ""
    envelope_derived = (
        status is not WorkerTaskStatus.UNKNOWN
        and not reason.startswith(_PROCESS_REASON_PREFIX)
    )
    if envelope_derived:
        items.append(
            EvidenceItem(
                kind="completion_claim",
                source=claim_source,
                detail=f"task_status={status.value}",
            )
        )

    if envelope_derived:
        level = "semantic"
    elif result.exit_code is not None:
        level = "process"
    else:
        level = "none"

    objective_failure = result.exit_code is not None and result.exit_code != 0
    return OutcomeEvidence(
        process_success=process_success,
        semantic_status=status.value,
        fallback_used=bool(metadata.get("fallback", False)),
        blocked_reason=result.task_status_reason,
        verification_level=level,
        evidence_items=tuple(items),
        verification_confidence=_confidence_for(
            level, objective_failure_observed=objective_failure
        ),
        assessor_version=ASSESSOR_VERSION,
    )


# Kept for a future VERIFIED ISOLATED runner: nothing in this
# module runs pytest today, and this classification is pure.
#
# pytest's own documented exit codes (``pytest.ExitCode``). Only exit 1 means
# "the tests ran and some failed". Everything else non-zero means the CHECK
# could not run, and thresholding on ``returncode == 0`` collapsed all of it
# into a confident, objective, high-confidence failure — which is how a
# 392-passed worktree came to be graded ``requested_tests_failed``.
_PYTEST_TESTS_FAILED = 1
_PYTEST_UNRUNNABLE_EXITS = {
    2: "collection interrupted",
    3: "internal error",
    4: "usage error",
    5: "no tests collected",
}

# CPython's own message when ``-m pytest`` names a module that is not there.
# It exits 1, which is otherwise the "tests failed" code, so the exit code
# alone cannot distinguish a missing verifier from a real test failure.
_PYTEST_MISSING = "No module named pytest"


def _classify_pytest_exit(
    returncode: int,
    stderr: str,
    tests: tuple[str, ...],
) -> tuple[bool | None, EvidenceItem]:
    """Map a pytest exit code to a verdict and the evidence item for it.

    ``passed is None`` means UNKNOWN: the check could not run. It is never a
    stand-in for failure.
    """
    scope = f"{len(tests)} target(s)"
    if returncode == 0:
        return (
            True,
            EvidenceItem(
                kind="requested_tests_passed",
                source="objective",
                detail=f"pytest exit 0 (all requested tests passed) on {scope}",
            ),
        )
    if returncode == _PYTEST_TESTS_FAILED and _PYTEST_MISSING not in (stderr or ""):
        return (
            False,
            EvidenceItem(
                kind="requested_tests_failed",
                source="objective",
                detail=f"pytest exit 1 (tests failed) on {scope}",
            ),
        )
    if returncode == _PYTEST_TESTS_FAILED:
        return (
            None,
            EvidenceItem(
                kind="requested_tests_unrunnable",
                source="inferred",
                detail="pytest is not installed in the resolved verification runtime",
            ),
        )
    meaning = _PYTEST_UNRUNNABLE_EXITS.get(returncode, "unrecognised pytest exit")
    return (
        None,
        EvidenceItem(
            kind="requested_tests_unrunnable",
            source="inferred",
            detail=f"pytest exit {returncode} ({meaning}) on {scope}: the check could not run",
        ),
    )


def _not_verified_item(tests: tuple[str, ...]) -> EvidenceItem:
    """The evidence that requested tests were NOT run, and why.

    A count, never the targets themselves: the detail is persisted into
    ``outcome_evidence_json`` and flows on into research records.
    """
    return EvidenceItem(
        kind="requested_tests_not_verified",
        source="inferred",
        detail=(
            "not verified: no verified isolated environment is available, so "
            f"ModelQuad did not run the {len(tests)} requested test target(s); "
            "the Host checks"
        ),
    )


def attach_outcome_evidence(
    result: ExecutionResult,
    *,
    working_directory: str | Path | None = None,
    spec: VerificationSpec | None = None,
    claim_source: str = "worker_self_reported",
    on_verification: Any = None,
) -> ExecutionResult:
    """Return ``result`` with OutcomeEvidence attached.

    Baseline assessment always happens (unless evidence is already present —
    a caller-supplied evidence object is never clobbered). When a spec names
    tests and the worker CLAIMED completion or partial completion, the
    evidence records that those tests were NOT verified: ModelQuad starts no
    process and never runs tests in ``working_directory``, which is the
    user's live folder. Level and confidence stay at the
    baseline, ``tests_executed`` is False and ``tests_passed`` stays UNKNOWN.

    ``working_directory`` and ``on_verification`` are kept so callers need no
    change. The callback announced a running objective pass; nothing is
    verified any more, so it is never invoked.
    """
    if result.evidence is not None:
        return result

    evidence = build_baseline_evidence(result, claim_source=claim_source)

    requested = (
        spec is not None
        and spec.has_objective_checks
        and result.task_status in (WorkerTaskStatus.COMPLETED, WorkerTaskStatus.PARTIAL)
    )
    if requested:
        evidence = replace(
            evidence,
            tests_executed=False,
            tests_passed=None,
            evidence_items=evidence.evidence_items
            + (_not_verified_item(spec.relevant_tests),),
        )

    return replace(result, evidence=evidence)


def finalize_execution_result(
    result: ExecutionResult,
    *,
    working_directory: str | Path | None = None,
    spec: VerificationSpec | None = None,
    on_verification: Any = None,
) -> ExecutionResult:
    """The ExecutionManager's post-execution hook: agent version + evidence.

    Stamps the worker's agent version into metadata and attaches outcome
    evidence. Both are additive; neither can fail the delegation.
    """
    try:
        # Agent version is first-class research metadata: a harness changes
        # behavior independently of its model, so "codex" alone does not
        # identify the agent that ran. The probe is a `--version` call --
        # no provider quota, cached per process -- and stays None (UNKNOWN)
        # when the CLI cannot be probed. Never overwrites a value an
        # executor already proved for itself.
        if "agent_version" not in (result.metadata or {}):
            from .workerevidence import runtime_version

            version = runtime_version(
                result.metadata.get("agent_runtime") or result.provider
            )
            if version is not None:
                result = replace(
                    result,
                    metadata={
                        **result.metadata,
                        "agent_version": version,
                        "agent_version_source": "cli_reported",
                    },
                )
    except Exception:  # noqa: BLE001 -- provenance capture must never break execution
        pass
    try:
        return attach_outcome_evidence(
            result,
            working_directory=working_directory,
            spec=spec,
            on_verification=on_verification,
        )
    except Exception:  # noqa: BLE001 -- assessment must never break execution
        return result


# --------------------------------------------------------------------------
# STAY_WITH_HOST outcomes
# --------------------------------------------------------------------------

VALID_MODEL_IDENTITY_SOURCES = ("confirmed", "requested", "reported", "configured", "unknown")
VALID_REPORTED_BY = ("host_agent", "user")


def build_host_outcome_result(
    *,
    task_status: str,
    provider: str = "claude",
    agent_runtime: str | None = None,
    resource_pool: str | None = None,
    reported_model: str | None = None,
    model_identity_source: str = "unknown",
    reported_by: str = "host_agent",
    detail: str | None = None,
    agent_version: str | None = None,
    agent_version_source: str = "unknown",
) -> ExecutionResult:
    """A host-executed task's outcome as an ExecutionResult.

    There is no worker subprocess: ``exit_code`` is None, ``success`` (the
    process-level legacy field) is False because no process was observed, and
    ``process_success`` in the evidence stays unknown. What the host claims
    is recorded as a claim, and what the modelquad did not verify is
    recorded as not verified — exactly the delegated-worker rules.
    """
    try:
        status = WorkerTaskStatus(task_status)
    except ValueError as exc:
        raise ValueError(
            f"task_status must be one of "
            f"{sorted(s.value for s in WorkerTaskStatus)}, got {task_status!r}"
        ) from exc
    if status is WorkerTaskStatus.UNKNOWN:
        # An UNKNOWN report is legal (the host may honestly not know) but it
        # must arrive as an explicit claim of unknowing, not be synthesized.
        pass
    if model_identity_source not in VALID_MODEL_IDENTITY_SOURCES:
        raise ValueError(
            f"model_identity_source must be one of {VALID_MODEL_IDENTITY_SOURCES}, "
            f"got {model_identity_source!r}"
        )
    if reported_by not in VALID_REPORTED_BY:
        raise ValueError(f"reported_by must be one of {VALID_REPORTED_BY}, got {reported_by!r}")

    metadata: dict[str, Any] = {
        "host_outcome": True,
        "reported_by": reported_by,
        "model_identity_source": model_identity_source,
        # Both keys are ALWAYS set, even to None. `finalize_execution_result`
        # otherwise fills agent_version by probing `<cli> --version` on PATH,
        # which is right for a worker (that CLI is what will run) and wrong
        # for a host: the host runs from its own installation. Observed
        # 2026-08-26 on the development machine -- PATH said 2.1.233 while the
        # session said 2.1.246 -- so the probe is suppressed here and the
        # version comes from the host observation or stays UNKNOWN.
        "agent_version": agent_version,
        "agent_version_source": agent_version_source if agent_version else "unknown",
    }
    if reported_model is not None:
        metadata["reported_model"] = reported_model
    if agent_runtime is not None:
        metadata["agent_runtime"] = agent_runtime
    if resource_pool is not None:
        metadata["resource_pool"] = resource_pool

    return ExecutionResult(
        provider=provider,
        success=False,  # process-level field; no process was observed
        output="",
        exit_code=None,
        error=None,
        metadata=metadata,
        task_status=status,
        task_status_reason=detail or f"host_reported_{reported_by}",
    )


def assess_host_outcome(
    *,
    task_status: str,
    working_directory: str | Path | None = None,
    relevant_tests: tuple[str, ...] | list[str] = (),
    provider: str = "claude",
    agent_runtime: str | None = None,
    resource_pool: str | None = None,
    reported_model: str | None = None,
    model_identity_source: str = "unknown",
    reported_by: str = "host_agent",
    detail: str | None = None,
    agent_version: str | None = None,
    agent_version_source: str = "unknown",
    verification_timeout: float = DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
) -> ExecutionResult:
    """Build and assess a STAY_WITH_HOST outcome.

    Self-report alone yields ``semantic``-level evidence (BRONZE-grade data).
    Naming relevant tests records them as not verified, the same way a
    worker's are: ModelQuad never runs them in the host's live folder,
    so a host outcome is not objectively verified and the Host
    checks.
    """
    result = build_host_outcome_result(
        task_status=task_status,
        provider=provider,
        agent_runtime=agent_runtime,
        resource_pool=resource_pool,
        reported_model=reported_model,
        model_identity_source=model_identity_source,
        reported_by=reported_by,
        detail=detail,
        agent_version=agent_version,
        agent_version_source=agent_version_source,
    )
    spec = (
        VerificationSpec(
            relevant_tests=tuple(relevant_tests), timeout=verification_timeout
        )
        if relevant_tests
        else None
    )
    claim_source = "user_reported" if reported_by == "user" else "worker_self_reported"
    finalized = finalize_execution_result(
        result, working_directory=working_directory, spec=spec
    )
    if finalized.evidence is None or finalized.evidence.assessor_version is None:
        # finalize swallowed an assessment error; fall back to the baseline
        # so a host outcome is never recorded without evidence at all.
        finalized = replace(
            finalized, evidence=build_baseline_evidence(result, claim_source=claim_source)
        )
    elif claim_source == "user_reported":
        # Re-tag the claim item: the reporter was a human, not the agent.
        retagged = tuple(
            replace(item, source="user_reported") if item.kind == "completion_claim" else item
            for item in finalized.evidence.evidence_items
        )
        finalized = replace(
            finalized, evidence=replace(finalized.evidence, evidence_items=retagged)
        )
    return finalized
