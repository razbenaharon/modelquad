# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""In-memory job/output seam; no runner, storage, or automatic application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal, Protocol

from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
from modelquad.execution.lifecycle import TerminalResolution

RETENTION_DAYS = 7
"""Default days to retain unacknowledged output."""
EXPIRY_WARNING = timedelta(days=1)
"""Warn one day ahead, within the 'a day or two' rule."""
RETENTION = timedelta(days=RETENTION_DAYS)
"""Default retention counted from store acceptance."""
MAX_RETENTION = timedelta(days=30)
"""Maximum TOTAL retention from store acceptance, including the default week."""


@dataclass(frozen=True)
class JobRef:
    """Correlate one public job identity with existing internal evidence."""

    #: Opaque stable id, e.g. a random UUID minted by the producer at start.
    job_id: str
    #: Internal delegation_attempts.id; never reused as the public job id.
    attempt_id: int
    #: Routing decision identity, absent when not observed.
    decision_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("job_id must be a nonempty string")
        if not isinstance(self.attempt_id, int) or isinstance(self.attempt_id, bool):
            raise ValueError("attempt_id must be an int, not a bool")
        if self.decision_id is not None and (
            not isinstance(self.decision_id, str) or not self.decision_id.strip()
        ):
            raise ValueError("decision_id must be None or a nonempty string")


class JobStatus(str, Enum):
    """Closed final job vocabulary, separate from claims."""

    #: The attempt returned normally, irrespective of the worker's claim.
    FINISHED = "finished"
    #: Launch, policy, or execution failed.
    FAILED = "failed"
    #: A user requested stopping; tree death is a separate fact.
    STOPPED = "stopped"
    #: Reconciliation found an attempt without its own terminal report.
    INTERRUPTED = "interrupted"
    #: The executor's process timeout fired; tree death is a separate fact.
    TIMED_OUT = "timed_out"


def cause_flags_from_result(result: ExecutionResult) -> tuple[bool, bool]:
    """Derive (stop_requested, deadline_passed), including stops before launch.

    The producer must use this helper with attempt_resolution(result): a stop
    is truthy metadata stop_requested OR reason == "stop_requested"; the
    deadline flag is truthy metadata timed_out, matching the execution
    classifier. Neither asserts tree death.
    """
    return (
        bool(result.metadata.get("stop_requested"))
        or result.task_status_reason == "stop_requested",
        bool(result.metadata.get("timed_out")),
    )


def job_status_from_resolution(
    resolution: TerminalResolution, *, stop_requested: bool, deadline_passed: bool,
) -> JobStatus:
    """Map final evidence; completed/refused outrank cause flags.

    The producer must derive flags with cause_flags_from_result(result).
    Orphaned attempts have no ExecutionResult and always map to interrupted.
    For other failed attempts, user stop takes precedence over a deadline.
    """
    pair = (resolution.status, resolution.failure_kind)
    ordinary = {
        ("completed", None): JobStatus.FINISHED,
        ("refused", "policy_refused"): JobStatus.FAILED,
        ("refused", "duplicate_in_flight"): JobStatus.FAILED,
        ("failed", "orphaned"): JobStatus.INTERRUPTED,
        **{("failed", kind): JobStatus.FAILED for kind in (
            "exception", "no_viable_worker", "provider_unavailable",
            "failed_to_start", "invalid_request", "internal_error",
        )},
    }
    if pair not in ordinary and pair not in {
        ("failed", "terminated"), ("failed", "timeout"),
    }:
        raise ValueError(f"not a recognized terminal attempt resolution: {pair!r}")
    if resolution.status in {"completed", "refused"} or pair == ("failed", "orphaned"):
        return ordinary[pair]
    if stop_requested:
        return JobStatus.STOPPED
    if deadline_passed:
        return JobStatus.TIMED_OUT
    if pair not in ordinary:
        raise ValueError("terminated/timeout requires a stop or deadline cause")
    return ordinary[pair]


def _utc(value: datetime, field: str) -> None:
    """Reject timestamps that do not carry an explicit UTC offset."""
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be a timezone-aware UTC datetime")


def _freeze(value: Any) -> Any:
    """Snapshot bounded JSON-shaped facts without mutable nested containers."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("worker facts must contain only JSON-shaped values")


@dataclass(frozen=True)
class JobOutput:
    """Immutable producer handoff containing output and separately observed facts.

    Interrupted jobs must also be put: ended_at is reconciliation time,
    worker_claim is UNKNOWN, claim_reason is "interrupted", output_text is
    captured partial output or "", and partial is True iff output or changed
    files were captured. changed_files stays None unless observed.
    """

    #: Public and internal identities for this output.
    job: JobRef
    #: Job execution fate, never inferred from the objective claim.
    job_status: JobStatus
    #: Worker's own account of whether its objective was achieved.
    worker_claim: WorkerTaskStatus
    #: Worker's explanation, absent when none was supplied.
    claim_reason: str | None
    #: Returned answer or available partial answer, never automatically applied.
    output_text: str
    #: Whether the handed-over output is partial.
    partial: bool
    #: Observed project-relative changes; None means not observed, () means none.
    changed_files: tuple[str, ...] | None
    #: Already bounded worker_termination facts, or None when unobserved.
    termination: Mapping[str, Any] | None
    #: Already bounded worker_descendants facts, or None when unobserved.
    descendants: Mapping[str, Any] | None
    #: Producer-observed start time in timezone-aware UTC.
    started_at: datetime
    #: Producer-observed end time in timezone-aware UTC.
    ended_at: datetime

    #: Whole-tree death observation; None means unobserved, never inferred from status.
    tree_confirmed_dead: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job_status, JobStatus):
            raise ValueError("job_status must be a JobStatus")
        if not isinstance(self.partial, bool):
            raise ValueError("partial must be a bool")
        if not isinstance(self.output_text, str):
            raise ValueError("output_text must be a str")
        if not isinstance(self.worker_claim, WorkerTaskStatus):
            raise ValueError("worker_claim must be a WorkerTaskStatus")
        if self.claim_reason is not None and not isinstance(self.claim_reason, str):
            raise ValueError("claim_reason must be None or a str")
        if self.tree_confirmed_dead is not None and not isinstance(self.tree_confirmed_dead, bool):
            raise ValueError("tree_confirmed_dead must be bool or None")
        _utc(self.started_at, "started_at")
        _utc(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        if self.changed_files is not None:
            if isinstance(self.changed_files, str):
                raise ValueError("changed_files must be a sequence of relative paths")
            paths = tuple(self.changed_files)
            for path in paths:
                if not isinstance(path, str) or not path or "\x00" in path:
                    raise ValueError("changed_files must contain nonempty relative paths")
                windows = PureWindowsPath(path)
                posix = PurePosixPath(path)
                if (
                    windows.drive or windows.root or posix.is_absolute()
                    or ":" in path or ".." in windows.parts or ".." in posix.parts
                    or PurePosixPath(path.replace("\\", "/")).as_posix() == "."
                ):
                    raise ValueError("changed_files must be project-relative without traversal")
            object.__setattr__(self, "changed_files", tuple(sorted({
                PurePosixPath(path.replace("\\", "/")).as_posix() for path in paths
            })))
        for name in ("termination", "descendants"):
            facts = getattr(self, name)
            if facts is not None:
                if not isinstance(facts, Mapping):
                    raise ValueError(f"{name} must be a mapping or None")
                object.__setattr__(self, name, _freeze(facts))


@dataclass(frozen=True)
class CollectResult:
    """Repeatable collection result whose receipt authorizes separate acknowledgment."""

    #: Retained immutable output for the requested job.
    output: JobOutput
    #: Nonempty random receipt, independent of content, hashes and job_id; stable.
    receipt: str
    #: Moment the store accepted this output, in timezone-aware UTC.
    retained_at: datetime
    #: Effective expiry time in timezone-aware UTC.
    expires_at: datetime
    #: Consumer-observed expiry warning within EXPIRY_WARNING.
    expiring_soon: bool
    #: Explicit user extension's UTC end, or None without an extension.
    extended_until: datetime | None

    #: Loss facts from the same store snapshot as the answer and receipt.
    output_truncated: bool = False
    original_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, str) or not self.receipt.strip():
            raise ValueError("receipt must be a nonempty str")
        if not isinstance(self.expiring_soon, bool):
            raise ValueError("expiring_soon must be a bool")
        if not isinstance(self.output_truncated, bool):
            raise ValueError("output_truncated must be a bool")
        if self.original_bytes is not None and (
            not isinstance(self.original_bytes, int) or isinstance(self.original_bytes, bool)
            or self.original_bytes < 0
        ):
            raise ValueError("original_bytes must be a nonnegative int or None")
        _utc(self.retained_at, "retained_at")
        _utc(self.expires_at, "expires_at")
        if self.retained_at < self.output.ended_at:
            raise ValueError("retained_at must not precede output.ended_at")
        if self.extended_until is not None:
            _utc(self.extended_until, "extended_until")
            if self.extended_until > self.retained_at + MAX_RETENTION:
                raise ValueError("extension exceeds total MAX_RETENTION")
            if self.extended_until < self.retained_at + RETENTION:
                raise ValueError("extension must not shorten default retention")
        if self.expires_at != (self.extended_until or self.retained_at + RETENTION):
            raise ValueError("expires_at must match extension or default retention")


@dataclass(frozen=True)
class PendingEntry:
    """Content-free pending-output summary for a returning Host."""

    #: Public/internal job correlation, without output content.
    job: JobRef
    #: Final execution fate, separate from the worker claim.
    job_status: JobStatus
    #: Effective expiry in timezone-aware UTC.
    expires_at: datetime
    #: True when expiry is within EXPIRY_WARNING of query time.
    expiring_soon: bool

    def __post_init__(self) -> None:
        if not isinstance(self.expiring_soon, bool):
            raise ValueError("expiring_soon must be a bool")
        _utc(self.expires_at, "expires_at")


@dataclass(frozen=True)
class JobOutcome:
    """Content-free record of a job leaving pending state."""

    #: Public/internal correlation without content or a content fingerprint.
    job: JobRef
    #: Final retention state, separate from execution fate.
    state: Literal["acknowledged", "expired", "deleted"]
    #: UTC recorded event time: max(operation now, latest now already served).
    ended_at: datetime

    def __post_init__(self) -> None:
        if self.state not in {"acknowledged", "expired", "deleted"}:
            raise ValueError("outcome state must be acknowledged, expired or deleted")
        _utc(self.ended_at, "ended_at")


class OutputSink(Protocol):
    """Retain state history without applying output; validate every UTC now.

    Expiry and warnings use the supplied now and durable served-time watermark,
    never a sink-owned clock.
    Removed jobs retain no content fingerprint; subsequent puts are ignored.
    Receipts must be random, never derived from content, a hash or job_id.
    pending, collect, state and outcomes reflect every expiry at/before now.
    All operations are serialized, including expiry materialization and reads.
    Each event time is max(operation now, latest now already served by any
    operation), never earlier than a durably recorded served time. File stores
    may return collect/state/pending on ENOSPC without advancing that watermark;
    outcomes, put and extend must fail rather than use that fallback.
    """

    def put(self, output: JobOutput, now: datetime) -> bool:
        """Store at now; now < output.ended_at raises ValueError.

        Pending conflict raises; identical/removed retry returns False.
        """
        ...

    def collect(self, job_id: str, now: datetime) -> CollectResult | None:
        """Reflect every expiry at/before now; repeat output; None unless pending."""
        ...

    def acknowledge(self, job_id: str, receipt: str, now: datetime) -> bool:
        """Matching receipt removes content; correct repeat is True, wrong is False.

        After delete or expiry, even the former matching receipt returns False.
        """
        ...

    def extend(self, job_id: str, until: datetime, now: datetime) -> CollectResult | None:
        """Extend pending retention within its 30-day total; invalid bounds raise ValueError.

        Missing/nonpending returns None before validating until. Exactly the
        default expiry is a no-op with extended_until None when unextended;
        an existing extension cannot be shortened.
        """
        ...

    def delete(self, job_id: str, now: datetime) -> bool:
        """Remove pending content manually, retaining deleted state; False if not pending."""
        ...

    def pending(self, now: datetime) -> tuple[PendingEntry, ...]:
        """Reflect every expiry at/before now; order by expires_at then job_id; warn."""
        ...

    def purge_expired(self, now: datetime) -> tuple[JobRef, ...]:
        """Expire at/before now; return refs by expires_at then job_id; keep outcomes."""
        ...

    def state(self, job_id: str, now: datetime) -> Literal["pending", "acknowledged", "expired", "deleted"] | None:
        """Reflect every expiry at/before now; None means never stored; no resurrection."""
        ...

    def outcomes(self, now: datetime, *, since: datetime) -> tuple[JobOutcome, ...]:
        """Reflect every expiry at/before now, then list transitions in [since, now].

        Content-free records are ordered by ended_at then job_id.
        The Host uses the previous query's now as since; both endpoints are
        inclusive, so it deduplicates by (job_id, state). Serialized recording
        clamps event time to max(operation now, latest now already served).
        """
        ...
