# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Shared contracts for provider command-line execution."""

from __future__ import annotations

import json
import os
import re
import subprocess
from copy import deepcopy
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..models import NETWORK_POLICY_REFUSED, TaskType
from ..action_authority import TaskAuthority
# Re-exported for the existing MCP observer and caller publication boundary.
from .lifecycle import CallerWaitHandle, TerminalResolution


DEFAULT_TIMEOUT_SECONDS = 600.0


WORKER_ENV_VAR = "MODELQUAD_WORKER"

_SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class WorkerTaskStatus(str, Enum):
    """Semantic outcome of a delegated task, independent of process exit status.

    A subprocess exiting 0 only proves the *process* ran to completion --
    it says nothing about whether the requested task actually happened
    (the worker may have been permission-blocked, sandboxed out of every
    tool call, or simply have stopped partway through). Every executor
    must set this explicitly rather than leaving callers to infer it from
    ``ExecutionResult.success``.
    """

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


# The completion contract. The
# headline `task_status` answers ONE question -- was the assigned objective
# done -- and nothing else moves it:
#
#   completed  the objective was achieved as requested. A denied or unavailable
#              tool or capability that the objective did not need is NOT a
#              reason to say anything else; it is listed under `limitations`.
#   blocked    the objective could not be completed because a capability, tool
#              or action it REQUIRED was unavailable or denied.
#   partial    only part of the objective was done.
#   failed     the work was attempted and did not succeed.
#
# The instruction below used to say "use blocked if any tool call was denied",
# and four consecutive delegations came back `blocked` with the
# work complete and correct -- a headline no caller could use as a completion
# signal. The rule is stated here once, for every provider, rather than being
# compensated for consumer by consumer.
_COMPLETION_ENVELOPE_INSTRUCTION = (
    "\n\n---\n"
    "Before you finish, append a fenced code block, on its own at the very "
    "end of your reply, with exactly this shape (valid JSON, no comments; "
    "the example shows a completed task with one limitation):\n"
    "```json\n"
    '{"task_status": "completed", '
    '"reason": null, '
    '"limitations": ["a tool or capability that was denied or unavailable '
    'but that the task did not need, and what you did instead"], '
    '"files_needed": [], '
    '"evidence": {"summary": "one sentence, concrete, based on what you '
    'actually observed"}}\n'
    "```\n"
    "files_needed lists the workspace-relative paths of files you were not "
    "given that would have changed or strengthened your answer (an empty "
    "list if none). Name only files you have reason to believe exist.\n"
    "task_status is one of \"completed\", \"blocked\", \"partial\" or "
    "\"failed\", and it answers one question: was the assigned task "
    "completed? "
    "Use \"completed\" if you finished the task as requested, even if some "
    "tool call was denied or could not run along the way -- list every such "
    "denial in \"limitations\" (an empty list if there were none) so the "
    "caller knows what was unavailable, and do NOT downgrade completed work "
    "because of it. Use \"blocked\" only if the task could not be completed "
    "because a tool, capability or action it required was denied or "
    "unavailable, and say which in \"reason\". Use \"partial\" if you "
    "completed only part of the task. Use \"failed\" if you attempted the "
    "task and it did not succeed."
)

_ENVELOPE_PATTERN = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

_VALID_ENVELOPE_STATUSES = {status.value for status in WorkerTaskStatus} - {
    WorkerTaskStatus.UNKNOWN.value
}


def append_completion_protocol(prompt: str) -> str:
    """Append the structured completion-envelope instruction to a prompt."""

    return prompt + _COMPLETION_ENVELOPE_INSTRUCTION


def classify_worker_result(
    *,
    returncode: int,
    combined_output: str,
    primary_output: str,
    blocked_pattern: re.Pattern[str] | None = None,
    blocked_reason: str = "permission_required",
) -> tuple[WorkerTaskStatus, str | None, bool]:
    """Derive (task_status, reason, safe_to_retry) from a completed CLI run.

    Shared by every CLI-based executor. Evidence is weighed in this order:

    1. A non-zero exit is real process-channel evidence and always means
       ``FAILED`` -- nothing overrides it.
    2. A worker-supplied structured completion envelope (see
       ``parse_completion_envelope``) is trusted for its own self-reported
       ``blocked``/``partial``/``failed`` outcome and reason: the worker
       already told us the truth about what went wrong, so a generic
       ``blocked_pattern``-derived reason must never overwrite that honest,
       specific explanation.
    3. ``blocked_pattern`` -- a known, independently-verifiable failure
       signature specific to that CLI (a permission soft-denial notice, a
       sandbox spawn error) -- is applied in exactly two situations: (a) the
       envelope claims ``completed`` (this is what stops a worker's own
       prose from turning a genuinely blocked/failed tool call into a
       reported success -- the documented case is Codex's Windows sandbox
       bug, where Codex exits 0 and narrates success while every tool call
       silently failed), or (b) no valid envelope exists at all (the worker
       never got to respond, e.g. a headless permission denial that replaces
       the entire response with a terminal CLI notice).
    4. Absent all of the above, a zero exit is *not* definite success on its
       own -- ``UNKNOWN`` rather than assuming completion.

    ``blocked_pattern`` itself must be a strongly-anchored, CLI-internal
    diagnostic signature (e.g. a Rust module log prefix, a fixed engine-name
    prefix) rather than a bare phrase -- a bare phrase risks matching quoted
    source/output text a worker legitimately reproduces while reviewing this
    very codebase (which documents these failure signatures in its own
    comments).
    """

    if returncode != 0:
        return WorkerTaskStatus.FAILED, f"process_exit_{returncode}", False

    envelope = parse_completion_envelope(primary_output)
    pattern_matched = blocked_pattern is not None and bool(blocked_pattern.search(combined_output))

    if envelope is not None:
        reported = envelope.get("task_status")
        reason = envelope.get("reason") or f"worker_reported_{reported}"
        if reported == "completed":
            if pattern_matched:
                return WorkerTaskStatus.BLOCKED, blocked_reason, True
            return WorkerTaskStatus.COMPLETED, None, False
        if reported == "blocked":
            return WorkerTaskStatus.BLOCKED, reason, True
        if reported == "partial":
            return WorkerTaskStatus.PARTIAL, reason, False
        if reported == "failed":
            return WorkerTaskStatus.FAILED, reason, False

    if pattern_matched:
        return WorkerTaskStatus.BLOCKED, blocked_reason, True

    # A clean exit alone is never sufficient evidence of task completion.
    return WorkerTaskStatus.UNKNOWN, "no_completion_envelope", False


def worker_limitations(primary_output: str) -> tuple[str, ...]:
    """The `limitations` a worker listed beside its headline status, or nothing.

    Reported SEPARATELY from ``task_status`` and never folded into it: a
    denied non-essential tool call is information the caller wants,
    not a reason to call completed work blocked. Read from the same envelope
    ``classify_worker_result`` trusts; an envelope without the field, or one
    whose field is not a list of strings, yields an empty tuple rather than an
    error -- a malformed limitation must not turn a good result into a bad
    one. Entries are stripped, empty ones dropped, and the list is bounded so
    a runaway worker cannot fill the result with it.
    """

    envelope = parse_completion_envelope(primary_output)
    if envelope is None:
        return ()
    return _normalize_limitations(envelope.get("limitations"))


def worker_files_needed(primary_output: str) -> tuple[str, ...]:
    """The `files_needed` a worker listed in its envelope, or nothing.

    A worker's claim, bounded like ``limitations``: it names files to offer
    the user, never files to read or send. Whether a name is a real, sendable
    workspace file is the caller's check (``noshell.needed_files``).
    """

    envelope = parse_completion_envelope(primary_output)
    if envelope is None:
        return ()
    return _normalize_limitations(envelope.get("files_needed"))


_MAX_LIMITATIONS = 20
_MAX_LIMITATION_CHARS = 500


def _normalize_limitations(value: Any) -> tuple[str, ...]:
    # The schema says a list. Anything else -- a bare string, a dict, a number
    # -- is malformed and is dropped whole rather than guessed at.
    if not isinstance(value, (list, tuple)):
        return ()
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        kept.append(text[:_MAX_LIMITATION_CHARS])
        if len(kept) >= _MAX_LIMITATIONS:
            break
    return tuple(kept)


def parse_completion_envelope(text: str) -> dict[str, Any] | None:
    """Return the last well-formed completion envelope found in ``text``, or None.

    Only ever used to *downgrade* toward a self-reported problem or to fill in
    an otherwise-unproven outcome -- never trusted on its own to certify
    ``completed`` over an independently detected failure signature.
    """

    matches = _ENVELOPE_PATTERN.findall(text or "")
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        status = parsed.get("task_status")
        if status not in _VALID_ENVELOPE_STATUSES:
            continue
        return parsed
    return None


def validate_cli_token(value: str, field_name: str) -> None:
    """Reject a value that is not safe to place in a subprocess argv element.

    Two independent risks this closes: (1) on Windows, `subprocess.run` with
    `shell=False` still routes a resolved `.cmd`/`.bat` target through
    `cmd.exe`, which -- even though it respects most argv quoting -- expands
    `%VAR%` unconditionally and can be broken out of via an embedded `"`
    followed by a shell metacharacter, letting an attacker-controlled token
    run an arbitrary second command; (2) independent of the OS shell, a
    value starting with `-` can be misread by the *target CLI's own*
    argument parser as a flag (e.g. a `session_id` of
    `--dangerously-bypass-approvals-and-sandbox`) instead of the intended
    positional value. Restricting to a plain alphanumeric/`._-` charset that
    can't start with `-` closes both at once.
    """

    if not _SAFE_TOKEN_PATTERN.match(value):
        raise ValueError(
            f"{field_name} contains characters that are not safe to pass to a "
            f"subprocess argument: {value!r}"
        )
    if value.startswith("-"):
        raise ValueError(f"{field_name} must not start with '-': {value!r}")


from ..procutil import no_window_kwargs


@dataclass(frozen=True)
class ExecutionRequest:
    """A validated request to execute a task with one provider.

    ``model``/``reasoning_effort`` are additive and optional -- every
    pre-existing caller that never sets them keeps behaving identically.
    Only charset/shape safety is validated here (same guarantee as
    ``session_id``); whether a given value is a *provider-valid* model id or
    reasoning-effort level is each executor's own responsibility, since the
    valid set differs per provider.

    Placed after ``metadata`` (not between ``session_id`` and ``metadata``)
    even though nothing in this codebase constructs this dataclass
    positionally (every call site, in both source and tests, uses keyword
    arguments) -- an independent review flagged that a future positional
    caller inserted between these two fields would silently misdirect its
    metadata dict into ``model`` instead of erroring, so the two new fields
    go last where appending them can never shift an existing argument.
    """

    provider: str
    task_type: TaskType
    prompt: str
    working_directory: Path
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    reasoning_effort: str | None = None
    # Which agent runtime this request targets. ``None`` means the
    # provider's default runtime -- the manager resolves it before spawning.
    # Appended last for the same positional-safety reason documented above.
    agent_runtime: str | None = None
    # Called with the worker process pid the moment it is spawned, so the
    # caller's delegation-attempt row can record which OS process owns the
    # work (a caller timeout must never leave that unknowable). Optional and
    # appended last; executors pass it through to the runner only when set.
    on_spawn: Any = None
    # Called after the approved files have been packed, with the closed audit
    # record only. It is an in-process observation hook, never worker input.
    on_packed: Any = None
    stop_requested: Any = None
    # None is explicitly legacy/unmigrated; it is not implicit Host authority.
    action_authority: TaskAuthority | None = None

    def __post_init__(self) -> None:
        working_directory = Path(self.working_directory).resolve()
        object.__setattr__(self, "working_directory", working_directory)

        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if not working_directory.exists():
            raise ValueError(f"working directory does not exist: {working_directory}")
        if not working_directory.is_dir():
            raise ValueError(
                f"working directory is not a directory: {working_directory}"
            )
        if self.session_id is not None:
            validate_cli_token(self.session_id, "session_id")
        if self.model is not None:
            if self.provider == 'codex':
                from ..codex_config import safe_model_id
                if safe_model_id(self.model) != self.model:
                    raise ValueError('model is not safe: invalid Codex model id')
            else:
                validate_cli_token(self.model, "model")
        if self.reasoning_effort is not None:
            validate_cli_token(self.reasoning_effort, "reasoning_effort")
        if self.agent_runtime is not None:
            validate_cli_token(self.agent_runtime, "agent_runtime")
        if self.action_authority is not None:
            if not isinstance(self.action_authority, TaskAuthority):
                raise ValueError("action_authority must be a typed TaskAuthority")
            self.action_authority.validate_consultation(self.task_type in READ_ONLY_TASK_TYPES)

    @property
    def authority_status(self) -> str:
        return "legacy_unmigrated" if self.action_authority is None else "declared_unmapped"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "provider": self.provider,
            "task_type": self.task_type.value,
            "prompt": self.prompt,
            "working_directory": str(self.working_directory),
            "timeout": self.timeout,
            "session_id": self.session_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "metadata": self.metadata,
            "agent_runtime": self.agent_runtime,
        }
        if self.action_authority is not None:
            result["action_authority"] = self.action_authority.to_dict()
        return result


def foundation_authority_refusal(request: ExecutionRequest) -> ExecutionResult | None:
    """Fail closed until runtime effect mapping and first-path guards exist.

    Used by the manager AND native adapters so direct executor callers cannot
    silently ignore a declaration. No claimed grant, including isolation,
    activates a new path in this foundation.
    """
    if request.action_authority is None:
        return None
    return ExecutionResult(
        provider=request.provider, success=False, output="", exit_code=None,
        error="Task authority is declared but runtime permission mapping and first-path guards are not verified.",
        task_status=WorkerTaskStatus.BLOCKED,
        task_status_reason="action_authority_unmapped",
    )


@dataclass(frozen=True)
class EvidenceItem:
    """One verification fact with its provenance.

    ``source`` states who observed the fact — the load-bearing distinction is
    that a worker's own claim (``worker_self_reported``) can never silently
    become ``objective``: only checks the modelquad ran itself are
    objective. ``verification_level`` on OutcomeEvidence summarizes; the
    items are the auditable trail behind that summary.
    """

    kind: str
    source: str
    detail: str | None = None

    VALID_SOURCES = frozenset(
        {"objective", "independent", "inferred", "user_reported", "worker_self_reported"}
    )

    def __post_init__(self) -> None:
        if self.source not in self.VALID_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(self.VALID_SOURCES)}, got {self.source!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceItem":
        return cls(
            kind=str(data.get("kind", "unknown")),
            source=data.get("source", "inferred"),
            detail=data.get("detail"),
        )


@dataclass(frozen=True)
class OutcomeEvidence:
    """Objective and semantic verification signals for a completed task.

    ``verification_level`` maps onto the conceptual verification classes:
    ``objective`` = OBJECTIVE (modelquad ran the checks itself),
    ``semantic`` = SELF_REPORTED (a worker/host completion claim),
    ``process`` = only the process exit was observed, ``none`` = nothing.
    The COMPLETED_VERIFIED vs COMPLETED_UNVERIFIED distinction is
    ``ExecutionResult.verified_completed`` — task_status COMPLETED plus
    ``verified`` here — not a duplicate status enum.
    """

    process_success: bool | None = None
    semantic_status: str | None = None
    files_changed: tuple[str, ...] | None = None
    tests_executed: bool | None = None
    tests_passed: bool | None = None
    build_passed: bool | None = None
    lint_passed: bool | None = None
    artifact_validated: bool | None = None
    scope_matches: bool | None = None
    fallback_used: bool | None = None
    blocked_reason: str | None = None
    duration_seconds: float | None = None
    verification_level: str = "none"
    # v2 additions: per-item provenance, acceptance-check tallies,
    # a categorical confidence (deliberately not a fabricated number), the
    # assessor's own version, and the verifier's measured runtime. All
    # optional; records written before these fields simply lack them.
    evidence_items: tuple[EvidenceItem, ...] = ()
    acceptance_checks_total: int | None = None
    acceptance_checks_passed: int | None = None
    verification_confidence: str = "none"
    assessor_version: str | None = None
    test_runtime_seconds: float | None = None

    _VALID_VERIFICATION_LEVELS = frozenset({"none", "process", "semantic", "objective"})
    # Categorical by design: "high" = the modelquad ran objective checks
    # itself; "medium" = an objective process-level observation (e.g. a
    # non-zero exit the modelquad saw) without scope verification;
    # "low" = a self-reported claim only; "none" = no evidence at all.
    _VALID_CONFIDENCE = frozenset({"high", "medium", "low", "none"})

    def __post_init__(self) -> None:
        if self.verification_level not in self._VALID_VERIFICATION_LEVELS:
            raise ValueError(
                f"verification_level must be one of {sorted(self._VALID_VERIFICATION_LEVELS)}, "
                f"got {self.verification_level!r}"
            )
        if self.verification_confidence not in self._VALID_CONFIDENCE:
            raise ValueError(
                f"verification_confidence must be one of {sorted(self._VALID_CONFIDENCE)}, "
                f"got {self.verification_confidence!r}"
            )
        if self.files_changed is not None and not isinstance(self.files_changed, tuple):
            object.__setattr__(self, "files_changed", tuple(self.files_changed))
        if not isinstance(self.evidence_items, tuple):
            object.__setattr__(self, "evidence_items", tuple(self.evidence_items))

    @property
    def verified(self) -> bool:
        """True only when verification level is objective and no test/build failures occurred."""
        return (
            self.verification_level == "objective"
            and self.tests_passed is not False
            and self.build_passed is not False
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_success": self.process_success,
            "semantic_status": self.semantic_status,
            "files_changed": list(self.files_changed) if self.files_changed is not None else None,
            "tests_executed": self.tests_executed,
            "tests_passed": self.tests_passed,
            "build_passed": self.build_passed,
            "lint_passed": self.lint_passed,
            "artifact_validated": self.artifact_validated,
            "scope_matches": self.scope_matches,
            "fallback_used": self.fallback_used,
            "blocked_reason": self.blocked_reason,
            "duration_seconds": self.duration_seconds,
            "verification_level": self.verification_level,
            "verified": self.verified,
            "evidence_items": [item.to_dict() for item in self.evidence_items],
            "acceptance_checks_total": self.acceptance_checks_total,
            "acceptance_checks_passed": self.acceptance_checks_passed,
            "verification_confidence": self.verification_confidence,
            "assessor_version": self.assessor_version,
            "test_runtime_seconds": self.test_runtime_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutcomeEvidence:
        files = data.get("files_changed")
        items_raw = data.get("evidence_items") or ()
        return cls(
            process_success=data.get("process_success"),
            semantic_status=data.get("semantic_status"),
            files_changed=tuple(files) if files is not None else None,
            tests_executed=data.get("tests_executed"),
            tests_passed=data.get("tests_passed"),
            build_passed=data.get("build_passed"),
            lint_passed=data.get("lint_passed"),
            artifact_validated=data.get("artifact_validated"),
            scope_matches=data.get("scope_matches"),
            fallback_used=data.get("fallback_used"),
            blocked_reason=data.get("blocked_reason"),
            duration_seconds=data.get("duration_seconds"),
            verification_level=data.get("verification_level", "none"),
            evidence_items=tuple(
                EvidenceItem.from_dict(item) for item in items_raw if isinstance(item, dict)
            ),
            acceptance_checks_total=data.get("acceptance_checks_total"),
            acceptance_checks_passed=data.get("acceptance_checks_passed"),
            verification_confidence=data.get("verification_confidence", "none"),
            assessor_version=data.get("assessor_version"),
            test_runtime_seconds=data.get("test_runtime_seconds"),
        )


@dataclass(frozen=True)
class ExecutionResult:
    """A normalized result from a provider CLI invocation.

    ``success`` is process-level only (subprocess launched and exited 0, or
    pre-flight validation passed) and is kept solely for backward
    compatibility. It is NOT evidence that the requested task happened.
    ``task_status``/``task_completed``/``safe_to_retry`` are the trustworthy,
    explicit signals -- every new caller (fallback, MCP, history, analytics)
    must read those instead.
    """

    provider: str
    success: bool
    output: str
    exit_code: int | None
    session_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    task_status: WorkerTaskStatus = WorkerTaskStatus.UNKNOWN
    task_status_reason: str | None = None
    safe_to_retry: bool = False
    evidence: OutcomeEvidence | None = None
    # What the worker could not use and did not need: denied or
    # unavailable tools/capabilities that did not stop the objective. Beside
    # the headline, never inside it -- a caller reads `task_status` for
    # whether the work was done and this for what to know about how.
    limitations: tuple[str, ...] = ()

    # Normalization fixes the terminal verdict before manager/MCP annotations.
    # Metadata remains the legacy dict interface; it is not the authority for
    # revising the same result's terminal verdict after normalization.
    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", deepcopy(self.metadata))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        # Deliberately not a dataclass field: generic asdict/replace and the
        # public to_dict payload retain their existing fields. A fresh result
        # (including replace) normalizes its own supplied execution facts.
        object.__setattr__(self, "_terminal_resolution", _classify_attempt_resolution(self))

    @property
    def terminal_resolution(self) -> TerminalResolution:
        """Immutable terminal classification, independent of caller departure."""
        return self._terminal_resolution

    @property
    def task_completed(self) -> bool:
        """True only when the task itself is confirmed done, not just the process."""

        return self.task_status is WorkerTaskStatus.COMPLETED

    @property
    def verified_completed(self) -> bool:
        """This is the VERIFIED_COMPLETED vs COMPLETED_UNVERIFIED distinction
        expressed on the existing status system rather than a duplicate enum."""

        return self.task_completed and self.evidence is not None and self.evidence.verified

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "success": self.success,
            "process_success": self.success,
            "output": self.output,
            "exit_code": self.exit_code,
            "session_id": self.session_id,
            "error": self.error,
            "metadata": deepcopy(self.metadata),
            "task_status": self.task_status.value,
            "reason": self.task_status_reason,
            "safe_to_retry": self.safe_to_retry,
            "task_completed": self.task_completed,
            "limitations": list(self.limitations),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


# task_status_reasons that mean the delegation never reached a running worker:
# the ExecutionManager's own pre-execution failures (execution/manager.py) and
# each executor's pre-launch rejections and launch failures. For the attempt
# record that is the difference between "the delegation happened and the task
# went badly" and "the delegation never reached a worker at all" -- which is
# the whole question delegation_attempts exists to answer.
ATTEMPT_FAILURE_REASONS: dict[str, str] = {
    "stop_requested": "timeout",  # without observed tree death, the worker is unaccounted for
    # ExecutionManager pre-execution failures: no executor was ever asked.
    "invalid_request": "invalid_request",
    "runtime_not_registered": "invalid_request",
    "provider_not_registered": "provider_unavailable",
    "provider_unavailable": "provider_unavailable",
    "runtime_provider_mismatch": "invalid_request",
    "no_viable_worker": "no_viable_worker",
    # Executor pre-launch failures: the worker process never started.
    "cli_not_found": "failed_to_start",
    "launch_failed": "failed_to_start",
    "unsafe_shim_binary": "failed_to_start",
    "invalid_sandbox_mode": "invalid_request",
    "invalid_mode": "invalid_request",
    "invalid_reasoning_effort": "invalid_request",
}


#: The reason Codex gives when it refuses to resume a session; see
#: `POLICY_REFUSAL_REASONS`.
READ_ONLY_RESUME_UNPINNED = "read_only_resume_unpinned"


#: The Codex consultation's pre-launch refusals, one closed label per cause
#: (``noshell.REFUSAL_MESSAGES`` holds the sentence for each, and a test pins
#: the two sets together). None of them starts a worker.
NOSHELL_PRELAUNCH_REFUSALS: frozenset[str] = frozenset({
    "task_text_secret", "supplied_file_secret", "supplied_file_missing",
    "supplied_files_over_limit", "supplied_file_unavailable", "relevant_files_ambiguous",
    "rendered_prompt_cap", "codex_shim_only", "codex_version_unsupported",
    "codex_feature_unverified", "codex_executable_unverified", "supplied_file_changed",
    "unsupported_platform",
    # The repository's .modelquad.toml (repo_policy.REASONS).
    "repo_policy_codex_off", "repo_policy_kind_removed", "repo_policy_account_workspace_required",
    "repo_policy_invalid", "repo_policy_unreadable", "repo_policy_file_outside_repo",
    # The working folder is not a local folder (localpath.UNUSABLE).
    "project_directory_unusable",
    # The user's own per-repository setting (repo_settings.REASONS).
    "user_repo_codex_off",
})


#: task_status_reasons that mean a ModelQuad policy gate said no -- NOT that a
#: delegation tried and could not. No worker was asked, and none should have
#: been, so the attempt resolves as `refused`/`policy_refused` rather than as a
#: failure. `routing_refused` is the router's
#: own terminal refusal; `network_policy_refused` is `netpolicy`'s, which is
#: what its gate returns to an executor that would have called a provider's
#: API from this process. `read_only_resume_unpinned` is Codex's refusal to
#: resume a session, and `task_type_not_read_only` its refusal of a task type
#: that is not read-only. Host launch refusals are recorded before a worker
#: starts by _launch_refusal as policy_refused, outside this result set.
POLICY_REFUSAL_REASONS: frozenset[str] = frozenset(
    {"routing_refused",
     NETWORK_POLICY_REFUSED, READ_ONLY_RESUME_UNPINNED,
     "action_authority_unmapped", "task_type_not_read_only",
     *NOSHELL_PRELAUNCH_REFUSALS}
)

#: The task types a delegation runs read-only: a consultation, second opinion
#: or independent review reads the user's files and never writes them.
#: Defined once, here, because every executor
#: already imports this module: Codex maps these to `--sandbox read-only`, and
#: two copies of the set is how one executor starts widening a task type
#: another still protects.
READ_ONLY_TASK_TYPES: frozenset[TaskType] = frozenset(
    {TaskType.REVIEW, TaskType.EXPLORATION, TaskType.ARCHITECTURE}
)

# `NETWORK_POLICY_REFUSED` -- the `task_status_reason` an executor sets when
# `netpolicy` denied its call -- is defined in `models.py` and imported above,
# staying importable from here where every executor already reads it. One
# definition, because the routing gate now states the same refusal as an
# `ineligibility_reason` and two spellings of one vocabulary term
# is how a surface starts recognising only one of them.


def termination_facts(exc: BaseException) -> dict[str, Any]:
    """The ``worker_termination`` a tree-killing runner attached to its timeout, as
    result metadata -- or nothing, for a runner that attached none. Absent is
    absent: a fake or a plain ``subprocess.run`` says nothing about the worker's
    fate, and the record must not say more than the runner did."""
    facts = getattr(exc, "worker_termination", None)
    return {"worker_termination": dict(facts)} if isinstance(facts, dict) else {}


def descendant_facts(completed: Any) -> dict[str, Any]:
    """The ``worker_descendants`` a tree-killing runner attached to a normal
    exit, as result metadata -- or nothing, for a runner that attached none.
    Same rule as :func:`termination_facts`: a fake or a plain
    ``subprocess.run`` observed nothing, and the record says nothing. Bounded
    here, where the facts enter a result, so every place a result's metadata
    is persisted carries the same shape."""
    facts = getattr(completed, "worker_descendants", None)
    return {"worker_descendants": bound_descendant_facts(facts)} if isinstance(facts, dict) else {}


#: How many left-behind processes one record will name. A worker that leaves
#: more than this is still recorded as having left "more than listed".
DESCENDANTS_RECORDED = 32


def bound_descendant_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Exactly the fields a record may carry, and nothing a path could hide in.

    Per process: pid, the executable's basename (never a path) and a creation
    time; per record: whether enumeration succeeded, why not, how, when, and
    whether more was left than is listed (``PRIVACY.md``).
    """

    import os

    listed = facts.get("descendants")
    entries: list[dict[str, Any]] = []
    if isinstance(listed, list):
        for item in listed[:DESCENDANTS_RECORDED]:
            if not isinstance(item, dict):
                continue
            pid = item.get("pid")
            image = item.get("image")
            created = item.get("created")
            entries.append(
                {
                    "pid": int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None,
                    "image": os.path.basename(str(image))[:64] if isinstance(image, str) and image else None,
                    "created": (
                        float(created)
                        if isinstance(created, (int, float)) and not isinstance(created, bool)
                        else None
                    ),
                }
            )
    bounded: dict[str, Any] = {
        "enumerated": bool(facts.get("enumerated")),
        "reason": str(facts.get("reason") or "")[:64] or None,
        "method": str(facts.get("method") or "")[:32] or None,
        "observed_at": (
            facts.get("observed_at") if isinstance(facts.get("observed_at"), (int, float)) else None
        ),
        "descendants": entries,
        "truncated": bool(facts.get("truncated"))
        or (isinstance(listed, list) and len(listed) > DESCENDANTS_RECORDED),
    }
    workers = facts.get("workers")
    if isinstance(workers, int) and not isinstance(workers, bool):
        bounded["workers"] = workers
    return bounded


def merge_descendant_facts(*facts: dict[str, Any] | None) -> dict[str, Any] | None:
    """One record for an attempt that ran several workers.

    A multi-step plan resolves ONE attempt from its last result, and an
    earlier worker's leftovers would otherwise be dropped on the floor. (A
    routed delegation starts at most one worker, so it carries that worker's
    own record and needs no merge.) The merged record lists every process
    any of the workers left, keyed by full identity -- pid, image AND
    creation time -- so a pid
    an earlier worker's child gave up and a later worker's child took keeps
    both sightings: the stale one reads as recycled and the live one is what
    the workspace question matches. It is ``enumerated`` only if
    every worker's enumeration succeeded; a worker that attached no facts at
    all -- a runner that did not look -- makes the whole record unobserved,
    because a union with a hole in it is not a union. All-None stays None:
    nothing looked, and the row says nothing.
    """

    present = [dict(item) for item in facts if isinstance(item, dict)]
    if not present:
        return None
    merged: dict[str, Any] = {
        "observed_at": max((item.get("observed_at") or 0.0) for item in present) or None,
        "method": "merged",
        "workers": len(facts),
        "descendants": [],
    }
    seen: set[tuple[Any, Any, Any]] = set()
    for item in present:
        for entry in item.get("descendants") or ():
            if not isinstance(entry, dict):
                continue
            identity = (entry.get("pid"), entry.get("image"), entry.get("created"))
            if identity not in seen:
                seen.add(identity)
                merged["descendants"].append(dict(entry))
    merged["truncated"] = any(bool(item.get("truncated")) for item in present)
    if len(present) < len(facts):
        merged.update({"enumerated": False, "reason": "worker_not_observed"})
        return merged
    failed = [item for item in present if not item.get("enumerated")]
    if failed:
        reasons = sorted({str(item.get("reason") or "unknown") for item in failed})
        merged.update({"enumerated": False, "reason": ",".join(reasons)})
        return merged
    merged["enumerated"] = True
    return merged


def worker_confirmed_dead(metadata: dict[str, Any] | None) -> bool:
    """True only when the runner READ the whole TREE's death from the process table.

    The single predicate behind both the row's sentence and its closed-vocabulary
    ``failure_kind``, so the two can never disagree. Absent facts, or facts whose
    ``confirmed_dead`` is False, are not evidence of death.

    ``confirmed_dead`` alone is not enough: it is read for the DIRECT CHILD only.
    On Windows every provider CLI is a ``.cmd`` shim (cmd.exe -> node.exe), so a
    dead direct child whose tree was not confirmed dead leaves the real worker
    running and unobserved -- exactly the defect this work exists to close. Death
    is therefore confirmed only when ``tree_kill`` is ``ok`` as well, which the
    runner writes only after its Job Object census read empty. Any
    other value -- ``direct_child_only`` on POSIX, ``job_not_empty``,
    ``census_unknown`` or ``uncontained``, and the ``exit N`` / ``timed_out`` /
    ``launch_error`` readings older rows carry from when the tree walk was
    ``taskkill`` -- leaves descendants unaccounted for and must not read as
    ``terminated``.
    """
    facts = (metadata or {}).get("worker_termination")
    if not isinstance(facts, dict):
        return False
    return facts.get("confirmed_dead") is True and facts.get("tree_kill") == "ok"


def describe_worker_termination(metadata: dict[str, Any] | None) -> str:
    """One sentence for the attempt row about what became of a timed-out worker.

    ``confirmed_dead`` is the fact that matters: it was read from the process
    table after the kill, not inferred from the kill having been asked for.
    Without it -- or without a tree confirmed dead, since ``confirmed_dead``
    speaks only for the direct child -- the worker is *unaccounted for*, and the
    sentence says so, and says why, rather than rounding an attempted kill up to
    a finished one.
    """
    facts = (metadata or {}).get("worker_termination")
    if not isinstance(facts, dict):
        return ""
    pid = facts.get("pid")
    who = "worker pid " + str(pid) if pid is not None else "the worker"
    tree = str(facts.get("tree_kill") or "not attempted")
    if worker_confirmed_dead(metadata):
        return who + " terminated (tree kill " + tree + "; exit confirmed)"
    if facts.get("confirmed_dead") is True:
        return (
            who + ": direct child exit confirmed but NOT confirmed dead as a tree "
            "(tree kill " + tree + "; descendants unaccounted for)"
        )
    return who + ": kill attempted (tree kill " + tree + ") but NOT confirmed dead -- unaccounted for"


def workspace_stability_note(
    metadata: dict[str, Any] | None, working_directory: Any
) -> dict[str, Any]:
    """What an interrupted delegation's RESULT says about ITS OWN worker.

    :func:`describe_worker_termination` has always put the worker's fate on the
    attempt ROW. The party that is about to run a gate over that tree, review
    its diff or commit it is the CALLER, and previously the caller was
    never told: it received a failure and a workspace that looked settled while
    a descendant of the worker could still be writing to it.

    Scoped to THIS delegation's worker, and named that way. One delegation
    ending cleanly says nothing about the workspace, because a different
    request -- a different fingerprint, so not a duplicate and not refused --
    can be running against the same directory at the same time. The only
    workspace-wide answer is
    ``modelquad_delegation_status(working_directory=...)``, and this points at
    it in BOTH branches rather than substituting a one-worker fact for it.

    Empty for anything that did not time out or stop: a delegation that ended on its
    own terms says nothing about processes, and inventing a reassurance is as
    wrong as withholding a warning. Otherwise the answer turns on
    :func:`worker_confirmed_dead`, a positive reading of the whole TREE's death
    from the process table -- a kill merely attempted, a tree walk that failed,
    or a runner that recorded nothing all leave descendants unaccounted for.

    A read-only run (``requested_sandbox`` is ``read-only``) cannot write to
    the workspace, so its note says only that the process may still be
    running and using quota.
    """

    if not ((metadata or {}).get("timed_out") or (metadata or {}).get("stop_requested")):
        return {}
    confirmed = worker_confirmed_dead(metadata)
    workspace = str(working_directory)
    interruption = "was stopped" if (metadata or {}).get("stop_requested") else "timed out"
    if (metadata or {}).get("requested_sandbox") == "read-only":
        # A read-only worker cannot write to the workspace; what is left of
        # it can only keep running and spend the user's quota.
        note = {
            "workspace": workspace,
            "worker_fate": describe_worker_termination(metadata)
            or "the runner recorded no termination facts",
            "this_worker_may_still_be_running": not confirmed,
            "this_worker_may_still_be_writing": False,
        }
        if not confirmed:
            note["warning"] = (
                "This delegation " + interruption + " and the worker's death was NOT "
                "confirmed, so a process of it may still be running and using quota."
            )
        return note
    ask = (
        "Ask modelquad_delegation_status(working_directory=...) for whether "
        "anything is still writing to " + workspace + "; this result speaks only "
        "for this delegation's own worker."
    )
    note: dict[str, Any] = {
        "workspace": workspace,
        "worker_fate": describe_worker_termination(metadata)
        or "the runner recorded no termination facts",
        "this_worker_may_still_be_writing": not confirmed,
        "workspace_answer": ask,
    }
    if not confirmed:
        note["warning"] = (
            "This delegation " + interruption + " and the worker's death was NOT confirmed, so "
            "a process of it may still be writing to " + workspace + ". Do not treat "
            "a test run, a review or a commit of that tree as belonging to one "
            "state. " + ask
        )
    return note


def attempt_resolution(execution_result: "ExecutionResult") -> tuple[str, str | None]:
    """Map an execution result onto (attempt status, failure_kind).

    A worker that ran and reported BLOCKED/FAILED/PARTIAL still completed the
    delegation -- its outcome is recorded separately. Only a delegation that
    never reached a worker, or timed out, is a failed attempt.

    A timeout resolves as ``terminated`` only when the runner's own facts say
    the worker's death was read from the process table AND the job census read
    its whole tree empty. Anything less -- a kill that was merely
    attempted, a tree that was not confirmed dead, or a runner that
    reported nothing -- stays ``timeout``, with
    :func:`describe_worker_termination` saying "unaccounted for" in the
    detail. UNKNOWN stays UNKNOWN, and no caller-side fact (the
    caller leaving, a deadline passing) can produce ``terminated``: the only
    input here is what the process table said.
    """

    terminal = execution_result.terminal_resolution
    return terminal.status, terminal.failure_kind


def _classify_attempt_resolution(execution_result: ExecutionResult) -> TerminalResolution:
    """Capture existing resolution semantics once at result normalization.

    Only executor/runner result inputs participate. Worker objective status,
    process success, caller departure and elapsed caller deadlines cannot turn
    an unaccounted timeout into confirmed termination.
    """
    if (execution_result.task_status_reason == "stop_requested"
            and execution_result.metadata.get("worker_started") is False):
        return TerminalResolution("failed", "failed_to_start")
    if execution_result.metadata.get("timed_out") or execution_result.metadata.get("stop_requested"):
        if worker_confirmed_dead(execution_result.metadata):
            return TerminalResolution("failed", "terminated")
        return TerminalResolution("failed", "timeout")
    reason = execution_result.task_status_reason
    if reason in POLICY_REFUSAL_REASONS:
        return TerminalResolution("refused", "policy_refused")
    if reason in ATTEMPT_FAILURE_REASONS:
        return TerminalResolution("failed", ATTEMPT_FAILURE_REASONS[reason])
    return TerminalResolution("completed", None)


class Executor(ABC):
    """Contract implemented by concrete provider CLI executors."""

    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """Return True iff the underlying CLI can be located, without executing it."""

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run a request and return a normalized result."""


# --------------------------------------------------------------------------
# stderr handling
# --------------------------------------------------------------------------

STDERR_CATEGORIES: tuple[str, ...] = (
    "empty",
    "rate_limit",
    "permission",
    "sandbox",
    "error",
    "narration",
)


def classify_stderr(text: str) -> str:
    """Coarsen a stderr body into a closed-vocabulary category.

    Structured mode persists this instead of the body. The category is
    derived from shape only -- no substring of the input is ever returned,
    so a credential, path, or command in the narration cannot ride out
    through the label.
    """

    if not text or not text.strip():
        return "empty"
    lowered = text.lower()
    if "usage limit" in lowered or "rate limit" in lowered or "quota" in lowered:
        return "rate_limit"
    if "permission" in lowered or "denied" in lowered or "not permitted" in lowered:
        return "permission"
    if "sandbox" in lowered:
        return "sandbox"
    if "error" in lowered or "fatal" in lowered or "panic" in lowered:
        return "error"
    return "narration"


def result_error(stderr: str | None, *, success: bool, structured: bool = True) -> str | None:
    """The error string a failed run reports (and history persists).

    The body is withheld in EVERY mode: the typed provider signal in
    ``worker_metrics`` is the precise diagnostic, and a stderr body is a
    proven carrier of credentials and paths.

    ``structured`` is retained as an accepted-and-ignored keyword. It used to
    select between withholding the body and returning it
    verbatim, which contradicted AGENTS.md's "the stderr BODY is never
    persisted in any mode" -- a review recorded the contradiction. Both call sites already passed
    ``structured=True``, so no diagnostic that exists today is lost; what is
    removed is the parameter's ability to turn the guarantee off, which is
    the only thing a future executor could have reached for by accident.
    """

    if success:
        return None
    body = stderr or ""
    if not body.strip():
        return "worker reported a non-zero exit with no stderr output"
    return (
        f"worker reported a non-zero exit ({classify_stderr(body)}; "
        f"{len(body)} bytes of stderr withheld from persistence)"
    )


def quota_refusal_evidence(result: ExecutionResult) -> str | None:
    """Name the evidence that this run was REFUSED for quota, or None.

    The executor-side half of principle 3. `service/health.py` has been
    able to RECORD a runtime refusal for a long time, but nothing
    in the product ever wrote one -- the overlay that removes a refusing
    provider from routing was fed only by hand. This is the pure predicate
    that turns a finished `ExecutionResult` into that fact, kept here (beside
    `classify_stderr`, which produced one of its two inputs) so it stays
    testable without a manager, a database or a provider.

    A COMPLETED task is never a refusal. Completion is the provider answering
    the work, which is the strongest possible evidence that it did not turn it
    away, and it outranks either signal below.

    Two signals, which are trusted differently because they are not equally
    good, and the difference is a clean process exit:

    * ``worker_metrics.rate_limit_events`` -- Codex's own TYPED turn-failure
      codes (`workerevidence._CODEX_RATE_LIMIT_ERRORS`). The provider itself
      said it throttled us, so the EXIT CODE is irrelevant: `codex exec
      --json` reports `turn.failed` with `usage_limit_reached` and still
      exits 0, and reading that as a served run would both lose the refusal
      and (through the recovery rule) clear one already on record.
    * ``stderr_category == "rate_limit"`` -- the coarse shape the executor
      already derived, and the only signal a runtime without a rate-limit
      field offers (`rate_limit_events` is None there rather than 0). It is a substring test over "usage limit" / "rate
      limit" / "quota", and a worker narrating a task ABOUT quota routing
      prints all three -- this repository's own workers do, routinely. So it
      counts only when the process ALSO exited non-zero. A clean exit whose
      narration merely mentions a quota is not evidence of anything, and
      without that guard a delegation could take its own provider out of
      routing for the next half hour on the strength of a word in a log line.

    Only the CATEGORY the executor already computed is read, never the stderr
    body, so this cannot become a path by which a credential or an absolute
    path reaches the database (AGENTS.md: the body is never persisted in any
    mode). The returned string names the SIGNAL, not any part of the output.
    """

    if result.task_status is WorkerTaskStatus.COMPLETED:
        return None
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    metrics = metadata.get("worker_metrics")
    if isinstance(metrics, dict):
        events = metrics.get("rate_limit_events")
        if isinstance(events, int) and not isinstance(events, bool) and events > 0:
            return "worker_metrics.rate_limit_events"
    if not result.success and metadata.get("stderr_category") == "rate_limit":
        return "stderr_category.rate_limit"
    return None
