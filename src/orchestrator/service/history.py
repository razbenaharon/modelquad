# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Best-effort SQLite persistence for provider usage, routing, and execution.

Observation writes are best-effort and report whether a row was committed.
New workers require readable launch controls and a recorded active attempt;
CLI/MCP callers must refuse launch when those prerequisites fail. ``get_settings`` and
``put_settings`` back the window's routing profiles and are allowed to
raise -- callers (the HTTP API) turn a ``ValueError`` into a 400 response.

Nothing here ever stores a prompt, secret, token, or credential.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import privacy as _privacy
from ..config import Config
from ..execution.base import ExecutionResult, WorkerTaskStatus, worker_confirmed_dead
from ..models import FreshnessThresholds, ProviderStatus, RoutingResult, TaskType
from . import db as _db
from .doctor_bridge import provider_capabilities

logger = logging.getLogger(__name__)

PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "fit_weight": 0.75,
        "usage_weight": 0.25,
        "quota_penalty": {
            "critical_remaining_percentage": 5.0,
            "critical_penalty": 0.20,
            "low_remaining_percentage": 15.0,
            "low_penalty": 0.08,
        },
    },
    "best_quality": {
        "fit_weight": 0.90,
        "usage_weight": 0.10,
        "quota_penalty": {
            "critical_remaining_percentage": 5.0,
            "critical_penalty": 0.15,
            "low_remaining_percentage": 15.0,
            "low_penalty": 0.05,
        },
    },
    "preserve_quota": {
        "fit_weight": 0.55,
        "usage_weight": 0.45,
        "quota_penalty": {
            "critical_remaining_percentage": 10.0,
            "critical_penalty": 0.35,
            "low_remaining_percentage": 25.0,
            "low_penalty": 0.15,
        },
    },
    "fastest": {
        "fit_weight": 0.55,
        "usage_weight": 0.45,
        "quota_penalty": {
            "critical_remaining_percentage": 5.0,
            "critical_penalty": 0.05,
            "low_remaining_percentage": 15.0,
            "low_penalty": 0.02,
        },
    },
}
PRESET_NAMES: tuple[str, ...] = tuple(PRESETS)
_DEFAULT_FRESHNESS: dict[str, Any] = {"fresh_seconds": 300, "stale_seconds": 3600}
_SETTINGS_KEY = "routing_settings"
_PROVIDER_OVERRIDES_KEY = "provider_overrides"
_RESOURCE_STRATEGY_KEY = "session_resource_strategy"
_VALID_RESOURCE_STRATEGIES = frozenset({"spend", "preserve"})
_ROUTING_MODE_KEY = "routing_mode"
VALID_ROUTING_MODES: tuple[str, ...] = ("automatic",)
# Read compatibility only: retired saved modes behave as automatic.
_LEGACY_ROUTING_MODES = frozenset({"ask", "manual"})
DEFAULT_ROUTING_MODE = "automatic"
_ROUTING_PAUSED_KEY = _db.ROUTING_PAUSED_KEY
DEFAULT_ROUTING_PAUSED = _db.DEFAULT_ROUTING_PAUSED
_PLAN_ADVICE_ENABLED_KEY = "plan_advice_enabled"
_DECISION_MODE_KEY = "decision_mode"
# Legacy values remain valid for historical rows and saved settings. Active
# execution treats all of them as deterministic; planning is advice only,
# gated separately by the plan-advice switch.
VALID_DECISION_MODES: tuple[str, ...] = (
    "deterministic",
    "reasoning_shadow",
    "reasoning_primary",
)
DEFAULT_DECISION_MODE = "deterministic"


def _default_settings() -> dict[str, Any]:
    preset = PRESETS["balanced"]
    return {
        "preset": "balanced",
        "fit_weight": preset["fit_weight"],
        "usage_weight": preset["usage_weight"],
        "quota_penalty": dict(preset["quota_penalty"]),
        "freshness": dict(_DEFAULT_FRESHNESS),
    }


def _task_type_value(task_type: TaskType | str) -> str:
    return task_type.value if isinstance(task_type, TaskType) else str(task_type)


def _usage_source_resolver(fallback: str) -> Callable[[str], str]:
    """Return a provider -> usage-source-kind lookup backed by the registry.

    Recording one hardcoded kind for every provider misfiled anything that
    does not read local logs. The registry is the single place that knows this.

    ``default_registry()`` rebuilds every plugin on each call, so it is built
    once here and closed over, rather than per recorded row.
    """

    try:
        from ..registry import default_registry

        registry = default_registry()
    except Exception:
        return lambda _provider: fallback

    def resolve(provider_name: str) -> str:
        plugin = registry.get(provider_name)
        return plugin.usage_source_kind.value if plugin is not None else fallback

    return resolve


# The collector ticks every 60s whether or not anything changed. Writing an
# identical snapshot each tick grew the DB ~10 KB/minute at pure idle
# (measured 2026-08-25), so an unchanged row is skipped -- but never for
# longer than this heartbeat, which keeps the time series provably alive and
# bounds how stale the newest row can be.
_UNCHANGED_ROW_HEARTBEAT_SECONDS = 1800.0


def _heartbeat_due(last_recorded_at: Any, now: datetime) -> bool:
    try:
        recorded = datetime.fromisoformat(str(last_recorded_at))
    except (TypeError, ValueError):
        return True
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return (now - recorded).total_seconds() >= _UNCHANGED_ROW_HEARTBEAT_SECONDS


def _usage_row_is_news(
    conn: sqlite3.Connection,
    *,
    provider: str,
    resource_pool: str,
    observed_at: str | None,
    remaining_percentage: Any,
    freshness: str,
    available: int,
    now: datetime,
) -> bool:
    """True when this snapshot differs from the newest stored row (or the
    heartbeat is due). Metadata is deliberately NOT compared: probe-time
    timestamps inside it churn every tick and would defeat deduplication;
    every value the charts and the router read is in the compared columns.
    A freshness transition (fresh -> stale) counts as news even with no new
    provider observation."""

    row = conn.execute(
        "SELECT observed_at, recorded_at, remaining_percentage, freshness, available "
        "FROM provider_usage_history WHERE provider = ? AND resource_pool = ? "
        "ORDER BY id DESC LIMIT 1",
        (provider, resource_pool),
    ).fetchone()
    if row is None:
        return True
    unchanged = (
        row["observed_at"] == observed_at
        and row["remaining_percentage"] == remaining_percentage
        and row["freshness"] == freshness
        and row["available"] == available
    )
    return not unchanged or _heartbeat_due(row["recorded_at"], now)


def record_provider_usage(
    db_path: str | Path,
    statuses: Iterable[ProviderStatus],
    *,
    usage_source: str = "local_logs",
) -> None:
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        now = datetime.now(timezone.utc)
        recorded_at = now.isoformat()
        resolve_usage_source = _usage_source_resolver(usage_source)
        for status in statuses:
            # The headline row: one per integration per tick WITH news to
            # report, tagged with the subscription pool id (== provider id
            # for every current integration). Usage belongs to a resource
            # pool (V6); the provider column stays for compatibility.
            observed_iso = status.observed_at.isoformat() if status.observed_at else None
            available_int = 1 if status.available else 0
            if _usage_row_is_news(
                conn,
                provider=status.name,
                resource_pool=status.name,
                observed_at=observed_iso,
                remaining_percentage=status.remaining_percentage,
                freshness=status.freshness.value,
                available=available_int,
                now=now,
            ):
                conn.execute(
                    "INSERT INTO provider_usage_history "
                    "(provider, observed_at, recorded_at, remaining_percentage, "
                    "freshness, available, usage_source, metadata_json, resource_pool) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        status.name,
                        observed_iso,
                        recorded_at,
                        status.remaining_percentage,
                        status.freshness.value,
                        available_int,
                        resolve_usage_source(status.name),
                        _privacy.sanitize_persisted_metadata_json(status.metadata),
                        status.name,
                    ),
                )
            # Sub-pool rows: only when the integration observed a genuinely
            # finer pool structure. Readers that
            # chart per-provider history exclude these by default -- see
            # fetch_usage_history.
            if len(status.pools) > 1:
                for pool in status.pools:
                    pool_observed_iso = pool.observed_at.isoformat() if pool.observed_at else None
                    pool_available_int = 1 if pool.available else 0
                    if not _usage_row_is_news(
                        conn,
                        provider=status.name,
                        resource_pool=pool.id,
                        observed_at=pool_observed_iso,
                        remaining_percentage=pool.remaining_percentage,
                        freshness=pool.freshness.value,
                        available=pool_available_int,
                        now=now,
                    ):
                        continue
                    conn.execute(
                        "INSERT INTO provider_usage_history "
                        "(provider, observed_at, recorded_at, remaining_percentage, "
                        "freshness, available, usage_source, metadata_json, resource_pool) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            status.name,
                            pool_observed_iso,
                            recorded_at,
                            pool.remaining_percentage,
                            pool.freshness.value,
                            pool_available_int,
                            resolve_usage_source(status.name),
                            _privacy.sanitize_persisted_metadata_json(pool.raw),
                            pool.id,
                        ),
                    )
        conn.commit()
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()


class NothingSelected:
    """The type of `NOTHING_SELECTED`; there is exactly one instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NOTHING_SELECTED"


NOTHING_SELECTED = NothingSelected()
"""What a caller of `record_routing_decision` passes to record that nothing
was selected. `None` keeps meaning "the caller did not say", which records
the recommendation as the selection; this records NULL instead, so a
delegation that reached no executor does not read as one that chose the
recommended runtime."""


def _sanitized_evidence_json(evidence_json: str | None) -> str | None:
    """The decision evidence with every absolute path removed."""
    if evidence_json is None:
        return None
    try:
        return _privacy.sanitize_persisted_metadata_json(json.loads(evidence_json))
    except (TypeError, ValueError):
        return _privacy.sanitize_persisted_text(evidence_json)


def record_routing_decision(
    db_path: str | Path,
    task_type: TaskType | str,
    result: RoutingResult,
    *,
    task_id: str | None = None,
    source: str = "cli",
    overridden: bool = False,
    override_reason: str | None = None,
    selected_provider: str | NothingSelected | None = None,
    invocation_mode: str | None = None,
    invocation_mode_source: str | None = None,
    host: str | None = None,
    decision_source: str | None = None,
    decision_id: str | None = None,
    evidence_json: str | None = None,
    plan_decision_id: int | None = None,
) -> bool:
    # `result.target` (see RouteTarget in models.py) is optional and only
    # ever set when a configured model_policy prior exists for the winning
    # provider -- absent it, these columns stay NULL exactly like every
    # other optional field here.
    model_recommended = result.target.model if result.target is not None else None
    reasoning_effort_recommended = result.target.reasoning_effort if result.target is not None else None
    resource_pool_recommended = result.target.resource_pool if result.target is not None else None
    agent_runtime_recommended = result.target.agent_runtime if result.target is not None else None
    if selected_provider is NOTHING_SELECTED:
        recorded_selection = None
    elif selected_provider is not None:
        recorded_selection = selected_provider
    else:
        recorded_selection = result.recommendation
    effective_decision_source = decision_source or getattr(result, "decision_source", "deterministic")
    expected_consumption_bucket = (
        result.expected_consumption.bucket.value
        if result.expected_consumption is not None
        else None
    )
    expected_consumption_source = (
        result.expected_consumption.source.value
        if result.expected_consumption is not None
        else None
    )
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return False
    try:
        conn.execute(
            "INSERT INTO routing_decisions "
            "(created_at, task_id, task_type, recommendation, selected_provider, "
            "overridden, override_reason, providers_json, source, "
            "invocation_mode, invocation_mode_source, host, "
            "model_recommended, reasoning_effort_recommended, decision_source, "
            "expected_consumption_bucket, expected_consumption_source, "
            "resource_pool_recommended, agent_runtime_recommended, "
            "decision_id, reason, evidence_json, plan_decision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                task_id,
                _task_type_value(task_type),
                result.recommendation,
                recorded_selection,
                1 if overridden else 0,
                _privacy.sanitize_persisted_text(override_reason),
                # Provider statuses carry collector metadata (a usage file's
                # location, "Usage file not found: <path>"): no absolute
                # path is stored.
                _privacy.sanitize_persisted_metadata_json([p.to_dict() for p in result.providers]),
                source,
                invocation_mode,
                invocation_mode_source,
                host,
                model_recommended,
                reasoning_effort_recommended,
                effective_decision_source,
                expected_consumption_bucket,
                expected_consumption_source,
                resource_pool_recommended,
                agent_runtime_recommended,
                decision_id,
                # The router's own top-level rationale -- previously computed
                # and dropped, which left history unable to say WHY a
                # recommendation won (only per-provider score reasons survived
                # inside providers_json).
                _privacy.sanitize_persisted_text(result.reason),
                _sanitized_evidence_json(evidence_json),
                plan_decision_id,
            ),
        )
        conn.commit()
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        conn.close()


def record_execution(
    db_path: str | Path,
    task_type: TaskType | str,
    execution_result: ExecutionResult,
    *,
    task_id: str | None = None,
    duration_seconds: float | None = None,
    source: str = "cli",
    result: str | None = None,
    fallback: bool = False,
    fallback_from: str | None = None,
    project: str | None = None,
    task_status: str | None = None,
    task_status_reason: str | None = None,
    invocation_mode: str | None = None,
    invocation_mode_source: str | None = None,
    host: str | None = None,
    model_requested: str | None = None,
    model_confirmed: str | None = None,
    reasoning_effort_requested: str | None = None,
    evidence: Any | None = None,
    decision_id: str | None = None,
) -> None:
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        metadata = execution_result.metadata
        if task_status is None:
            task_status = execution_result.task_status.value
        if task_status_reason is None:
            task_status_reason = execution_result.task_status_reason
        if result is None:
            # `result` is the coarse legacy status bucket; `task_status` (above)
            # is the trustworthy structured signal. `task_completed` (derived
            # from task_status, not the process-level `success` field) is
            # authoritative for what counts as a real success here.
            if execution_result.task_completed:
                result = "success"
            elif task_status == WorkerTaskStatus.BLOCKED.value:
                result = "blocked"
            elif task_status == WorkerTaskStatus.PARTIAL.value:
                result = "partial"
            elif metadata.get("timed_out"):
                result = "timeout"
            elif task_status == WorkerTaskStatus.FAILED.value:
                result = "failure"
            else:
                result = "error"
        effective_fallback = bool(fallback or metadata.get("fallback", False))
        effective_fallback_from = fallback_from or metadata.get("fallback_from")
        # Executors record what was actually requested/confirmed in their
        # own metadata (see execution/codex.py);
        # an explicit caller-supplied value here always wins, matching the
        # fallback/fallback_from precedence pattern above.
        effective_model_requested = model_requested or metadata.get("requested_model")
        effective_model_confirmed = model_confirmed or metadata.get("confirmed_model")
        effective_reasoning_effort_requested = reasoning_effort_requested or metadata.get(
            "requested_reasoning_effort"
        )
        # Execution identity (V6): the manager annotates which agent runtime
        # ran and which resource pool it drew from. NULL means "the
        # provider's default" (or a pre-V6 executor path) -- never guessed.
        effective_agent_runtime = metadata.get("agent_runtime")
        effective_resource_pool = metadata.get("resource_pool")
        effective_evidence = evidence or execution_result.evidence
        effective_outcome_evidence_json = None
        if effective_evidence is not None:
            evidence_dict = effective_evidence.to_dict()
            # blocked_reason is the same free text as task_status_reason (a
            # worker's envelope "reason" or the Host's "detail"), so it gets
            # the same path redaction and cap as that column. The dict
            # is a copy: the caller's evidence, and what MCP tools return,
            # stay as they are.
            evidence_dict["blocked_reason"] = _privacy.sanitize_persisted_text(
                evidence_dict.get("blocked_reason")
            )
            effective_outcome_evidence_json = json.dumps(evidence_dict)
        # Project labels are intentionally only a basename.  History is shown
        # in a shared control plane and must never retain a user's full local
        # working-directory path.
        project_name = Path(project).name if project else None
        conn.execute(
            "INSERT INTO execution_history "
            "(created_at, task_id, task_type, project, provider, session_id, success, "
            "exit_code, duration_seconds, error, source, result, fallback, fallback_from, "
            "metadata_json, task_status, task_status_reason, "
            "invocation_mode, invocation_mode_source, host, "
            "model_requested, model_confirmed, reasoning_effort_requested, outcome_evidence_json, "
            "resource_pool, agent_runtime, decision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                task_id,
                _task_type_value(task_type),
                project_name,
                execution_result.provider,
                execution_result.session_id,
                None if execution_result.success is None else (1 if execution_result.success else 0),
                execution_result.exit_code,
                duration_seconds,
                # Executors build `error` by interpolating a live
                # exception on the launch/transport path (codex.py),
                # and such an exception names the
                # binary or endpoint that failed. The categorized structured
                # signal is already path-free; this makes the column path-free
                # in every mode, including a future executor that forgets.
                _privacy.sanitize_persisted_text(execution_result.error),
                source,
                result,
                1 if effective_fallback else 0,
                effective_fallback_from,
                # Metadata is normalized execution context, never prompts or
                # credentials -- and its serialized form is path-redacted at
                # this boundary rather than trusted to stay that way.
                _privacy.sanitize_persisted_metadata_json(metadata),
                task_status,
                _privacy.sanitize_persisted_text(task_status_reason),
                invocation_mode,
                invocation_mode_source,
                host,
                effective_model_requested,
                effective_model_confirmed,
                effective_reasoning_effort_requested,
                effective_outcome_evidence_json,
                effective_resource_pool,
                effective_agent_runtime,
                decision_id,
            ),
        )
        conn.commit()
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()


# Closed vocabularies so diagnostics and tests can rely on them.
DELEGATION_ATTEMPT_STATUSES: tuple[str, ...] = ("started", "completed", "failed", "refused")
DELEGATION_FAILURE_KINDS: tuple[str, ...] = (
    "policy_refused",
    "exception",
    "timeout",
    # ModelQuad stopped the worker and its death was CONFIRMED from the
    # process table -- the third worker fate beside completed and
    # failed. An attempted-but-unconfirmed kill stays `timeout`.
    "terminated",
    "no_viable_worker",
    "provider_unavailable",
    "failed_to_start",
    "invalid_request",
    "internal_error",
    # The recorded worker process is gone (or the row is past any plausible
    # run) and nothing ever resolved it -- closed by reconciliation, not by
    # the delegation itself.
    "orphaned",
    # A retry of the same logical request while its earlier worker is still
    # alive: refused so two workers never mutate one workspace.
    "duplicate_in_flight",
)

#: Why the CALLER stopped waiting -- a fact about the caller's transport, and
#: never a verdict about the worker, which is why this is its own closed
#: vocabulary beside `DELEGATION_FAILURE_KINDS` rather than a member of it.
#: `transport_cancelled`: the request was cancelled
#: under us, which is what a client read timeout looks like from the server.
#: `transport_closed`: the connection itself went away.
CALLER_ABANDONMENT_REASONS: tuple[str, ...] = ("transport_cancelled", "transport_closed")

_MAX_ATTEMPT_DETAIL_CHARS = 500


def _safe_detail(detail: object) -> str | None:
    """Cap and path-redact one free-text attempt detail before storing it.

    ``detail`` is written from four sites that interpolate a live exception
    (``f"{type(exc).__name__}: {exc}"``), and an exception raised by the
    filesystem or by a subprocess launch names the path that caused it. That
    is how an absolute path reaches a column AGENTS.md says paths never
    reach, through code that reads as if it only stored a failure kind.
    Enforcing it here rather than at each call site is deliberate: the next
    site to be added inherits the guarantee instead of having to remember it.
    """

    if not detail:
        return None
    return _privacy.sanitize_persisted_text(str(detail))[:_MAX_ATTEMPT_DETAIL_CHARS]


def _resolved_attempt_detail(existing: str | None, detail: str | None) -> str | None:
    """Keep an auxiliary attempt's identity when replacing terminal evidence."""
    for role in ("planning_worker", "plan_step_worker"):
        if existing == role or (existing or "").startswith(role + ":"):
            if not detail:
                detail = role
            elif detail != role and not detail.startswith(role + ":"):
                detail = f"{role}: {detail}"
            break
    return _safe_detail(detail)


class DuplicateActiveAttempt(RuntimeError):
    """The database already holds an active attempt for this logical request.

    Raised by :func:`record_delegation_attempt` when the schema's partial
    unique index rejects a second ``started`` row for one ``request_key``.
    This is the race that the pre-spawn
    :func:`find_in_flight_duplicate` check cannot close on its own: two
    callers can both look before either inserts. Unlike an ordinary
    persistence failure -- which must never break a delegation, and so
    returns None -- this one MUST propagate: the caller has to refuse the
    delegation rather than start a second worker that no row accounts for.
    """

    def __init__(self, request_key: str) -> None:
        super().__init__(f"an active delegation attempt already exists for request {request_key}")
        self.request_key = request_key


def request_fingerprint(task_type: TaskType | str, working_directory: str, prompt: str) -> str:
    """Stable fingerprint of one logical delegation request.

    Used to recognize a RETRY of the same work (same task type, workspace,
    and prompt) so a caller that timed out and asked again cannot silently
    start a second mutation worker beside a still-running first one.
    Independent requests differ in at least one component and keep distinct
    keys, so genuine concurrency is unaffected.
    """

    import hashlib

    normalized_dir = str(Path(working_directory).resolve()).lower()
    payload = "\x1f".join((_task_type_value(task_type), normalized_dir, prompt))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def workspace_identity(path: str | Path | None) -> tuple[str | None, str | None]:
    """A directory's identity for the workspace question: hashes, not the path.

    Returns ``(key, ancestors_json)`` -- the hash of the canonical directory,
    and a JSON array of the hashes of each directory above it, outermost last.
    Both are None for a path that cannot be resolved.

    It is hashed because `PRIVACY.md` promises the local history keeps a
    project directory's NAME and never its full path, and a safety feature does
    not get a quiet exemption from a published claim. Nothing is lost: the
    question is only ever "does this row concern the directory I am standing
    in?", and a query that hashes its own path and ancestors answers both
    directions of containment by set membership.

    The hashing is also what makes the matching correct for the ordinary cases.
    Prefix comparison on path strings has to special-case the drive root, the
    UNC share root and a trailing separator, and gets a repository directory
    matching its `-two` sibling wrong the moment it forgets a separator.
    Hashing whole path COMPONENTS has none of those edges.

    What it does NOT do is establish filesystem identity, and the limits are
    worth stating rather than discovering:

    - Case is folded with ``os.path.normcase``, which is a per-PLATFORM rule.
      Windows supports per-directory case sensitivity, so two directories that
      really are distinct can fold together. That error implicates an unrelated
      directory, which over-reports a hazard; it is left that way deliberately,
      because the opposite error is a workspace called clear while a worker
      writes to it.
    - Two spellings of one directory agree only once they resolve the same.
      The Win32 extended-length prefixes are stripped below because
      ``Path.resolve`` keeps them and they are pure spelling; a junction
      retargeted between the recording and the query is not recoverable here.
    - A path that does not exist gets a lexical identity. That is what allows a
      question to be asked about a worktree that has since been removed.

    Never raises: anything unresolvable yields ``(None, None)``, and a row with
    no identity is an absence that matches no query.
    """

    if path is None:
        return None, None
    try:
        # RuntimeError is in here for the symlink loop `Path.resolve` raises on
        # Python 3.10-3.12. A probe that cannot answer must return the absence,
        # not take the caller's query down with it.
        resolved = Path(path).resolve()
        ancestors = [_workspace_hash(parent) for parent in resolved.parents]
        return _workspace_hash(resolved), json.dumps(ancestors)
    except (OSError, ValueError, TypeError, RuntimeError):
        return None, None


#: The Win32 extended-length prefixes. `Path.resolve` keeps them, so `\\?\E:\x`
#: and `E:\x` -- the same directory, confirmed by `Path.samefile` -- hashed
#: differently and a live worker recorded under one spelling read as nothing at
#: all under the other. They carry no meaning beyond lifting MAX_PATH.
_EXTENDED_PATH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("\\\\?\\UNC\\", "\\\\"),
    ("\\\\?\\", ""),
)


def _workspace_hash(path: Path) -> str:
    """One directory's hash, with the spellings that mean the same thing folded."""

    import hashlib

    text = os.path.normpath(str(path))
    for prefix, replacement in _EXTENDED_PATH_PREFIXES:
        if text.startswith(prefix):
            text = replacement + text[len(prefix) :]
            break
    text = os.path.normcase(text)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _workspace_ancestor_set(ancestors_json: Any) -> frozenset[str] | None:
    """The ancestor hashes on a row, or None when they could not be read.

    None and the empty set are different answers and the difference decides a
    safety verdict: an empty set is a row whose workspace has no ancestors
    (a filesystem root), while None is a row whose ancestry is unreadable.
    Folding the second into the first would let corrupt data read as "not
    related to your directory", which is a clean answer produced by damage.
    """

    if ancestors_json is None or ancestors_json == "":
        return frozenset()
    try:
        loaded = json.loads(ancestors_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(loaded, list):
        return None
    if not all(isinstance(item, str) for item in loaded):
        return None
    return frozenset(loaded)


def workspace_relation(
    recorded_key: str | None,
    recorded_ancestors: Any,
    queried_key: str | None,
    queried_ancestors: frozenset[str] | None,
) -> str | None:
    """How a recorded workspace stands to a queried one, or None if unrelated.

    ``"same"``, ``"inside"`` (the recorded workspace is below the queried one,
    so its writes land in the queried tree), ``"contains"`` (the queried
    directory is below the recorded workspace, so the worker owns a tree that
    includes it), or ``"undetermined"`` -- an identity that could not be read
    on either side. All four are a worker that may mutate what the asker is
    standing in; the relation is reported so an answer can say WHICH, not so
    that any of them can be dismissed. Only ``None`` means "not related", and
    only a complete reading of both identities can produce it.
    """

    if not recorded_key or not queried_key:
        return None
    if recorded_key == queried_key:
        return "same"
    if queried_ancestors is None:
        return "undetermined"
    if recorded_key in queried_ancestors:
        return "contains"
    recorded = _workspace_ancestor_set(recorded_ancestors)
    if recorded is None:
        return "undetermined"
    if queried_key in recorded:
        return "inside"
    return None


def record_delegation_attempt(
    db_path: str | Path,
    *,
    task_type: TaskType | str,
    decision_id: str | None = None,
    requested_provider: str | None = None,
    status: str = "started",
    failure_kind: str | None = None,
    detail: str | None = None,
    source: str = "mcp",
    host: str | None = None,
    invocation_mode: str | None = None,
    request_key: str | None = None,
    working_directory: str | Path | None = None,
    repo_key: str | None = None,
    policy_version: int | None = None,
    capability: str | None = None,
) -> int | None:
    """Record that a delegation was ATTEMPTED, before it can fail.

    Written at the top of the delegation path so an attempt that is refused,
    crashes, or is abandoned still leaves objective evidence. Returns the row
    id for :func:`resolve_delegation_attempt`, or None if persistence failed
    (a missing claim must prevent a new worker launch).

    ``working_directory`` is the workspace this delegation was pointed at. The
    PATH is not stored -- :func:`workspace_identity` reduces it to the hash of
    the directory and of each directory above it, which is all
    :func:`workspace_delegation_liveness` needs to answer "may anything still
    be writing HERE?" for a caller holding a path and nothing else.
    Recording it never changes what is done with the delegation, and a
    path that cannot be canonicalised is stored as NULL rather than raising:
    the row is the evidence, and the evidence must not fail to be written.
    """

    if status not in DELEGATION_ATTEMPT_STATUSES:
        raise ValueError(f"status must be one of {DELEGATION_ATTEMPT_STATUSES}, got {status!r}")
    if repo_key is not None or policy_version is not None or capability is not None:
        import re
        if (not isinstance(repo_key, str) or re.fullmatch(r"[0-9a-f]{32}", repo_key) is None
                or (policy_version is not None and (type(policy_version) is not int or policy_version < 1))
                or capability not in ("delegate", "second_opinion")):
            raise ValueError("repo_policy_unavailable")
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        cursor = conn.execute(
            "INSERT INTO delegation_attempts "
            "(decision_id, created_at, resolved_at, task_type, requested_provider, "
            "status, failure_kind, detail, source, host, invocation_mode, request_key, "
            "workspace_key, workspace_ancestors, repo_key, policy_version, capability) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                now,
                # A terminal status recorded up front (a policy refusal) is
                # resolved the moment it is written; 'started' stays open.
                None if status == "started" else now,
                _task_type_value(task_type),
                requested_provider,
                status,
                failure_kind,
                _safe_detail(detail),
                source,
                host,
                invocation_mode,
                request_key,
                *workspace_identity(working_directory),
                repo_key,
                policy_version,
                capability,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        # Only the active-request invariant is expressed as a constraint, so
        # a violation on an opening row that carries a request key means
        # exactly one thing: a concurrent caller won the race for this
        # logical request. Anything else is an ordinary persistence failure
        # and stays silent.
        if status == "started" and request_key is not None:
            raise DuplicateActiveAttempt(request_key) from None
        return None
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()


def write_attempt_repo_policy(db_path, attempt_id, *, repo_key, policy_version, capability):
    """Required launch evidence: only content-free linkage, no new schema.

    A version bump is recorded before executor entry, never after it. A missing
    or terminal attempt refuses launch rather than losing which grant was used.
    """
    import re
    from ..routing_policy import LaunchRefused

    if (not isinstance(repo_key, str) or re.fullmatch(r"[0-9a-f]{32}", repo_key) is None
            or type(policy_version) is not int or policy_version < 1
            or capability not in ("delegate", "second_opinion")):
        raise LaunchRefused("repo_policy_unavailable")
    conn = None
    try:
        conn = _db.connect_and_init(db_path)
        cursor = conn.execute(
            "UPDATE delegation_attempts SET repo_key=?, policy_version=?, capability=? "
            "WHERE id=? AND status='started' AND resolved_at IS NULL "
            "AND (repo_key IS NULL OR repo_key=?) "
            "AND (capability IS NULL OR capability=?)",
            (repo_key, policy_version, capability, attempt_id, repo_key, capability),
        )
        if cursor.rowcount != 1:
            raise LaunchRefused("repo_policy_unavailable")
        conn.commit()
    except (sqlite3.Error, OSError):
        raise LaunchRefused("repo_policy_unavailable") from None
    finally:
        if conn is not None:
            conn.close()


def _open_attempt_owner(conn: sqlite3.Connection, attempt_id: int) -> int | None:
    """Find an unambiguous open outer attempt of this auxiliary worker only."""
    child = conn.execute(
        "SELECT decision_id, detail FROM delegation_attempts WHERE id = ?", (attempt_id,),
    ).fetchone()
    roles = ("planning_worker", "plan_step_worker")
    if child is None or not child["decision_id"] or not any(
        child["detail"] == role or (child["detail"] or "").startswith(role + ":")
        for role in roles
    ):
        return None
    candidates = conn.execute(
        "SELECT id, detail FROM delegation_attempts WHERE decision_id = ? "
        "AND status = 'started' AND resolved_at IS NULL AND id < ?",
        (child["decision_id"], attempt_id),
    ).fetchall()
    owners = [row["id"] for row in candidates if not any(
        row["detail"] == role or (row["detail"] or "").startswith(role + ":")
        for role in roles
    )]
    return owners[0] if len(owners) == 1 else None


def request_attempt_stop(db_path: str | Path, attempt_id: int, stop_owner: bool = False) -> str:
    """Atomically request a stop: accepted / not_running / not_found / unavailable."""
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
    except (sqlite3.Error, OSError):
        return "unavailable"
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, resolved_at FROM delegation_attempts WHERE id = ?", (attempt_id,),
        ).fetchone()
        if row is None:
            return "not_found"
        if row["status"] != "started" or row["resolved_at"] is not None:
            return "not_running"
        stored = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("attempt_stop_requests",),
        ).fetchone()
        try:
            ids = json.loads(stored["value"]) if stored else []
            if not isinstance(ids, list) or any(type(item) is not int for item in ids):
                ids = []
        except (ValueError, TypeError):
            ids = []
        open_ids = {item["id"] for item in conn.execute(
            "SELECT id FROM delegation_attempts WHERE status = 'started' AND resolved_at IS NULL",
        )}
        requested = {attempt_id}
        if stop_owner:
            owner = _open_attempt_owner(conn, attempt_id)
            if owner is not None:
                requested.add(owner)
        ids = sorted((set(ids) & open_ids) | requested)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("attempt_stop_requests", json.dumps(ids)),
        )
        conn.commit()
        return "accepted"
    except (sqlite3.Error, OSError):
        return "unavailable"
    finally:
        conn.close()


def attempt_stop_requested(db_path: str | Path, attempt_id: int) -> bool:
    """Read without creating/migrating a store; a failed poll leaves work running."""
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", ("attempt_stop_requests",),
            ).fetchone()
            ids = json.loads(row[0]) if row else []
            return (
                isinstance(ids, list) and all(type(item) is int for item in ids)
                and attempt_id in ids
            )
        finally:
            conn.close()
    except Exception:  # read errors must never escape into a worker runner
        return False


def resolve_delegation_attempt(
    db_path: str | Path,
    attempt_id: int | None,
    *,
    status: str,
    routed_provider: str | None = None,
    outcome: str | None = None,
    failure_kind: str | None = None,
    detail: str | None = None,
    duration_seconds: float | None = None,
    only_if_open: bool = False,
    exclude_job_owned: bool = False,
    worker_descendants: dict[str, Any] | None = None,
) -> bool | None:
    """Close out a previously recorded attempt with what actually happened.

    An existing planning_worker or plan_step_worker detail prefix is row
    identity and survives replacement of the terminal evidence, including
    reconciliation and a resolution with no detail.

    ``worker_descendants`` is what the runner saw the worker leave running
    when it exited, stored as JSON on the row so the workspace
    question can read it later. None means the runner did not look, and the
    column stays NULL.

    ``only_if_open`` makes the write conditional: the UPDATE also requires
    ``resolved_at IS NULL AND status = 'started'``, so a row somebody else
    already resolved truthfully is left exactly as they wrote it. That is what
    a reconciliation pass needs, because it observes and then writes, and the
    worker whose row it is may resolve between the two.

    Returns ``True``/``False`` -- whether a row was actually written -- only
    when ``only_if_open=True``. Without it the call is unconditional and
    returns ``None``, as every existing caller expects.
    """

    if attempt_id is None:
        return False if only_if_open else None
    if status not in DELEGATION_ATTEMPT_STATUSES:
        raise ValueError(f"status must be one of {DELEGATION_ATTEMPT_STATUSES}, got {status!r}")
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return False if only_if_open else None
    written = False
    try:
        descendants_json = (
            json.dumps(_bounded_descendants(worker_descendants), sort_keys=True)
            if isinstance(worker_descendants, dict)
            else None
        )
        # Read the existing marker in the UPDATE itself, under the same atomic
        # only_if_open guard as the terminal outcome. No stale read can replace
        # a worker's truthful resolution or erase its auxiliary identity.
        conn.create_function("resolved_attempt_detail", 2, _resolved_attempt_detail)
        cursor = conn.execute(
            "UPDATE delegation_attempts SET resolved_at = ?, status = ?, routed_provider = ?, "
            "outcome = ?, failure_kind = ?, detail = resolved_attempt_detail(detail, ?), duration_seconds = ?, "
            "worker_descendants = COALESCE(?, worker_descendants) WHERE id = ?"
            + (" AND resolved_at IS NULL AND status = 'started'" if only_if_open else "")
            + (" AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.attempt_id=delegation_attempts.id)"
               if exclude_job_owned else ""),
            (
                datetime.now(timezone.utc).isoformat(),
                status,
                routed_provider,
                outcome,
                failure_kind,
                detail,
                duration_seconds,
                descendants_json,
                attempt_id,
            ),
        )
        conn.commit()
        written = cursor.rowcount > 0
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()
    return written if only_if_open else None


def _bounded_descendants(facts: dict[str, Any]) -> dict[str, Any]:
    """The one bound, applied again at the row: ``execution.base.bound_descendant_facts``."""

    from ..execution.base import bound_descendant_facts

    return bound_descendant_facts(facts)


#: Kept as the row-side name for the bound's size (tests and readers use it).
from ..execution.base import DESCENDANTS_RECORDED as _DESCENDANTS_RECORDED  # noqa: E402


# An unresolved attempt younger than the longest a delegation may legitimately
# run is still IN FLIGHT, not abandoned. The ceiling is the default execution
# timeout plus a margin for process teardown and history writes -- past it, a
# row that never resolved cannot still be waiting on its own executor.
_ABANDONED_AFTER_SECONDS = 600.0 + 120.0


def record_caller_abandonment(db_path: str | Path, attempt_id: int | None, *, reason: str) -> bool:
    """Record that the CALLER stopped waiting for this attempt. Say whether it wrote.

    Two lifecycles run through one row and they are not the same lifecycle.
    The caller waits, then times out, disconnects
    or otherwise leaves; the worker runs, then completes, fails or is
    terminated. This writes the first and says nothing at all about the
    second: ``status`` stays ``started``, ``resolved_at`` stays NULL, so the
    unique active-request index and :func:`find_in_flight_duplicate` still
    refuse a second worker for the same request while this one may be alive.

    Write-once and idempotent: the first departure is the one that happened,
    and the ``caller_abandoned_at IS NULL`` guard is in the UPDATE itself, so
    a repeated observation returns False rather than moving the timestamp.
    """

    if attempt_id is None:
        return False
    if reason not in CALLER_ABANDONMENT_REASONS:
        raise ValueError(f"reason must be one of {CALLER_ABANDONMENT_REASONS}, got {reason!r}")
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError) as exc:
        # There is nobody left to raise at -- the caller has gone and the
        # worker's own path must not be disturbed -- but a departure that
        # silently failed to land is exactly the evidence gap this feature
        # exists to close, so say so. No retry: the row is not held open by
        # this write, and part 3 re-reads the table anyway.
        logger.warning("could not open the history database to record attempt %s leaving: %s", attempt_id, exc)
        return False
    try:
        cursor = conn.execute(
            "UPDATE delegation_attempts SET caller_abandoned_at = ?, caller_abandonment_reason = ? "
            "WHERE id = ? AND caller_abandoned_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), reason, attempt_id),
        )
        conn.commit()
        return int(cursor.rowcount or 0) > 0
    except (sqlite3.Error, OSError) as exc:
        logger.warning("could not record the caller of attempt %s leaving: %s", attempt_id, exc)
        return False
    finally:
        conn.close()


def fetch_caller_abandoned_open_attempts(
    db_path: str | Path, *, limit: int = 50, now: datetime | None = None
) -> list[dict[str, Any]]:
    """The attempts nobody is waiting for any more, whose worker may still run.

    Exactly the population the worker-accounting pass acts on: the caller has
    been recorded as gone, and the row is still open because the abandonment
    record writes no verdict about
    the worker. Each row is annotated by :func:`annotate_attempt_liveness`, so
    a reader gets the observed answer to "is that worker actually alive?"
    rather than having to infer it from the abandonment mark.
    """

    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM delegation_attempts "
            "WHERE caller_abandoned_at IS NOT NULL AND resolved_at IS NULL AND status = 'started' "
            "ORDER BY caller_abandoned_at DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return []
    finally:
        conn.close()
    return [annotate_attempt_liveness(dict(row), now=now) for row in rows]


def delegation_attempt_worker_pid(db_path: str | Path, attempt_id: int | None) -> int | None:
    """The worker pid recorded on an attempt, or None when none is (yet)."""
    if attempt_id is None:
        return None
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute("SELECT worker_pid FROM delegation_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return None
    finally:
        conn.close()


# How long a 'started' row may sit with NO recorded worker pid before it can
# be treated as not-actually-running by the duplicate guard: spawning happens
# within seconds of the row being written, so a pid-less row older than this
# either predates pid recording or died before its worker started.
_SPAWN_GRACE_SECONDS = 120.0


def unresolved_worker_attempts(db_path: str | Path) -> list[tuple[int, str]]:
    """Read open worker candidates before any native snapshot; never migrate.

    Errors propagate to the MCP guard's explicitly best-effort layer. A short
    lock wait keeps a busy history database from delaying normal Host calls.
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.05)
    try:
        return conn.execute(
            "SELECT worker_pid, created_at FROM delegation_attempts "
            "WHERE status = 'started' AND resolved_at IS NULL AND worker_pid IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()


def record_worker_pid(db_path: str | Path, attempt_id: int | None, pid: int) -> None:
    """Attach the spawned worker's OS pid to its attempt row.

    This is what makes a caller timeout survivable: with the pid on record,
    "is that worker still running?" is a checkable fact instead of a guess.
    """

    if attempt_id is None:
        return
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        conn.execute(
            "UPDATE delegation_attempts SET worker_pid = ? WHERE id = ?",
            (int(pid), attempt_id),
        )
        conn.commit()
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()


def _stamp_verification(db_path: str | Path, attempt_id: int | None, column: str) -> None:
    """Record that objective verification started/resolved for one attempt.

    The recorders are kept, uncalled, for a future verified isolated runner.
    Only a timestamp is written; no command line, prompt, workspace path or
    other content is persisted here.

    Like every other history write on the delegation path, failing to record
    must never break the delegation itself.
    """

    if attempt_id is None:
        return
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        conn.execute(
            f"UPDATE delegation_attempts SET {column} = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), attempt_id),
        )
        conn.commit()
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()


def record_verification_started(db_path: str | Path, attempt_id: int | None) -> None:
    """Objective verification has BEGUN for this attempt."""

    _stamp_verification(db_path, attempt_id, "verification_started_at")


def record_verification_resolved(db_path: str | Path, attempt_id: int | None) -> None:
    """Objective verification has FINISHED for this attempt (any verdict)."""

    _stamp_verification(db_path, attempt_id, "verification_resolved_at")


def annotate_attempt_liveness(entry: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Split 'unresolved' into in-flight versus abandoned.

    When the row carries a ``worker_pid`` the worker itself is probed -- a
    live pid is in flight regardless of age, a dead pid is abandoned
    immediately. Rows without a pid (pre-v13, or died before spawn) fall back
    to the age-based inference, labeled as such.

    Additively, it also splits "unresolved" the OTHER way -- by the caller.
    ``caller_abandoned`` is whether anyone is still waiting for this
    delegation's result; ``caller_waiting`` is whether someone still is. They
    are orthogonal to ``liveness``, which is about the worker: an attempt can
    perfectly well be ``in_flight`` with ``caller_abandoned`` True, and that
    combination is precisely what the worker-accounting pass exists to dispose of.

    The pid probe is tri-state (:func:`procutil.pid_state`): only a pid the
    process table positively reports gone reads ``abandoned``. A probe that
    could not answer -- another user's process, a denied handle -- reads
    ``unknown``, the same as a pid-less or malformed row, and never
    ``abandoned``.
    """

    from ..procutil import pid_state

    entry["unresolved"] = entry.get("resolved_at") is None
    entry["caller_abandoned"] = entry.get("caller_abandoned_at") is not None
    entry["caller_waiting"] = bool(entry["unresolved"]) and not entry["caller_abandoned"]
    entry["age_seconds"] = None
    entry["liveness"] = "resolved"
    if not entry["unresolved"]:
        return entry
    reference = now or datetime.now(timezone.utc)
    try:
        started = datetime.fromisoformat(str(entry["created_at"]))
    except (ValueError, TypeError, KeyError):
        entry["liveness"] = "unknown"
        return entry
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age = max(0.0, (reference - started).total_seconds())
    entry["age_seconds"] = age
    pid = entry.get("worker_pid")
    if pid:
        try:
            worker_pid = int(pid)
        except (TypeError, ValueError):
            # A pid we cannot read is UNKNOWN. Annotation is a reading of a
            # row, not a validator of it, and must never raise on one.
            entry["liveness"] = "unknown"
            return entry
        entry["liveness"] = {
            "alive": "in_flight",
            "dead": "abandoned",
            "unknown": "unknown",
        }.get(pid_state(worker_pid), "unknown")
    else:
        entry["liveness"] = "in_flight" if age < _ABANDONED_AFTER_SECONDS else "abandoned"
    return entry


def find_in_flight_duplicate(
    db_path: str | Path, request_key: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Return an unresolved attempt for the same logical request that may
    still have a live worker, or None.

    'May still be live' means: its recorded worker pid is not KNOWN dead, or
    it has no pid yet and is younger than the spawn grace window. Only a pid
    the process table positively reports gone makes the retry legitimate (and
    reconciliation will close that row as orphaned); a probe that could not
    answer still guards the request, because releasing the guard on a failure
    to observe is how a second worker gets started on live work.
    """

    from ..procutil import pid_state

    if not request_key:
        return None
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        rows = conn.execute(
            "SELECT * FROM delegation_attempts "
            "WHERE request_key = ? AND resolved_at IS NULL AND status = 'started' "
            "ORDER BY id DESC",
            (request_key,),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()
    reference = now or datetime.now(timezone.utc)
    for row in rows:
        # Annotated in both branches so a caller refusing a duplicate can say
        # how long the live attempt has been running, not merely that one
        # exists -- "wait for it" is only actionable with an elapsed time.
        annotated = annotate_attempt_liveness(dict(row), now=reference)
        pid = annotated.get("worker_pid")
        if pid:
            if pid_state(pid) != "dead":
                return annotated
            continue
        age = annotated.get("age_seconds")
        if age is not None and age < _SPAWN_GRACE_SECONDS:
            return annotated
    return None


def reconcile_stale_attempts(db_path: str | Path, *, now: datetime | None = None) -> int:
    """Close unresolved attempts whose worker verifiably no longer exists.

    A row whose recorded pid is dead, or that never recorded a pid and is
    past any plausible run, is resolved as failed/orphaned with an explicit
    detail -- an ambiguous permanently-'started' row is worse evidence than
    an honest 'orphaned'. Returns how many rows were reconciled. Rows whose
    pid is alive, and young pid-less rows, are never touched.

    'Verifiably' is literal: a row with a pid is closed only where
    :func:`procutil.pid_state` answered ``"dead"``. A probe that could not
    answer -- another user's process, a denied handle -- leaves the row
    exactly as it found it, because closing a row on a failed observation is
    how a LIVE worker's attempt gets retired.
    """

    from ..procutil import pid_state

    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return 0
    try:
        rows = conn.execute(
            "SELECT id, created_at, worker_pid FROM delegation_attempts "
            "WHERE resolved_at IS NULL AND status = 'started' "
            "AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.attempt_id=delegation_attempts.id)"
        ).fetchall()
    except (sqlite3.Error, OSError):
        conn.close()
        return 0
    finally:
        conn.close()

    reference = now or datetime.now(timezone.utc)
    reconciled = 0
    for row in rows:
        entry = dict(row)
        pid = entry.get("worker_pid")
        if pid:
            if pid_state(pid) != "dead":
                continue
            detail = (
                f"reconciled: recorded worker pid {pid} is no longer alive "
                "and the attempt was never resolved"
            )
        else:
            annotated = annotate_attempt_liveness(
                {"created_at": entry.get("created_at"), "resolved_at": None}, now=reference
            )
            age = annotated.get("age_seconds")
            if age is None or age < _ABANDONED_AFTER_SECONDS:
                continue
            detail = (
                "reconciled: no worker pid was recorded and the attempt is past "
                "any plausible run duration"
            )
        # Conditional: the row was observed open, but the attempt's own thread
        # may have resolved it truthfully in between. A reconciliation guess
        # must never overwrite what actually happened.
        if resolve_delegation_attempt(
            db_path,
            int(entry["id"]),
            status="failed",
            failure_kind="orphaned",
            detail=detail,
            only_if_open=True, exclude_job_owned=True,
        ):
            reconciled += 1
    return reconciled


def reconcile_abandoned_attempts(
    db_path: str | Path, *, limit: int = 50, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Account for the workers of callers who already left. Observe first.

    The intended ordering is "the caller already abandoned -> account for the
    worker -> ... -> only then terminal", and this is the accounting step.
    It reads exactly the population the abandonment record leaves
    open -- :func:`fetch_caller_abandoned_open_attempts` -- and closes a row
    only where the process table has already answered: the recorded worker pid
    is known and NOT alive. That row resolves as failed/``orphaned``, the same
    kind and convention :func:`reconcile_stale_attempts` uses for a dead pid,
    because nothing here terminated anything; ``terminated`` is reserved for a
    kill ModelQuad performed and confirmed.

    Everything else is left OPEN and merely reported: a live pid is a running
    worker (the caller's departure is not a verdict about it), and an unknown
    pid is UNKNOWN -- including a pid the probe could not READ, which
    :func:`procutil.pid_state` reports as ``"unknown"`` rather than folding
    into "dead". That fold is what closed a live worker's row and released its
    duplicate protection; the row now stays ``started`` and
    comes back with ``liveness`` ``"unknown"``. Nothing is killed -- killing a
    bare pid with no ``Popen`` behind it is a pid-reuse hazard and is
    deliberately not done here.
    ``caller_abandoned_at`` is never modified; the departure already happened.

    Returns every row it looked at, annotated by
    :func:`annotate_attempt_liveness` and carrying ``reconciled`` -- True for
    the rows it closed -- so a call site can report the ones still open. A row
    whose recorded pid cannot be read -- malformed, or probed without an
    answer -- is skipped and reported with ``reconcile_skipped`` saying which;
    this pass runs on the delegation path and may not take a delegation down
    over one unreadable row.
    Bounded by ``limit`` and synchronous: no thread, no scheduler.
    """

    from ..procutil import pid_state

    rows = fetch_caller_abandoned_open_attempts(db_path, limit=limit, now=now)
    for entry in rows:
        if job_for_attempt(db_path, entry["id"]) is not None:
            entry["reconciled"] = False
            continue
        entry["reconciled"] = False
        pid = entry.get("worker_pid")
        if not pid:
            continue
        try:
            worker_pid = int(pid)
        except (TypeError, ValueError):
            # An unreadable pid is UNKNOWN, like an absent one. A malformed
            # row must leave the pass reporting, never raising: this runs on
            # the delegation path and may not take a delegation down with it.
            entry["reconcile_skipped"] = f"recorded worker pid {pid!r} is not an integer"
            continue
        state = pid_state(worker_pid)
        if state != "dead":
            # "alive" is a running worker; "unknown" is a probe that could not
            # answer. Only the first is a verdict, and neither is death, so
            # the row stays open and is reported (its `liveness` already says
            # which of the two it was).
            if state == "unknown":
                entry["reconcile_skipped"] = (
                    f"recorded worker pid {worker_pid} could not be probed; "
                    "the row stays open"
                )
            continue
        # Conditional: the worker's own thread may have resolved this row
        # truthfully between the probe above and this write, and a
        # reconciliation guess never overwrites a real outcome.
        entry["reconciled"] = bool(
            resolve_delegation_attempt(
                db_path,
                int(entry["id"]),
                status="failed",
                failure_kind="orphaned",
                detail=(
                    f"reconciled: the caller had abandoned this attempt at "
                    f"{entry.get('caller_abandoned_at')} and recorded worker pid "
                    f"{worker_pid} was found dead at reconcile time"
                ),
                only_if_open=True, exclude_job_owned=True,
            )
        )
    return rows


#: How many rows one workspace question will scan, per population. The two
#: populations are read separately and bounded separately on purpose: closed
#: rows accumulate forever, and a shared allowance would let old history crowd
#: the open rows -- the ones that matter -- out of the scan entirely.
#:
#: Containment is decided by hash-set membership rather than by SQL, so the
#: rows are read and filtered here. A safety question may not become a
#: full-table scan on a large history; and when a scan does fill its bound the
#: answer says so and refuses to conclude "nothing", rather than reporting a
#: workspace it did not finish looking at.
_WORKSPACE_SCAN_LIMIT = 200

#: Endings that ACCOUNT for the worker. Every other resolved row leaves it
#: unaccounted for and keeps casting doubt on its workspace.
#:
#: ``timeout`` is the load-bearing exclusion. ``attempt_resolution`` writes
#: ``terminated`` only when the whole TREE's death was read from the process
#: table, and leaves ``timeout`` for a kill that was attempted and never
#: confirmed. On Windows that is not a formality: every provider CLI is a
#: ``.cmd`` shim (cmd.exe -> node.exe), so a ``taskkill /T`` that did not
#: return success can leave the real worker running while the direct child --
#: the only pid the row records -- is gone. ``orphaned`` is excluded for the
#: same reason: reconciliation closes a row because the recorded pid was found
#: dead, which is a fact about that pid and not about its descendants.
#:
#: The doubt does not expire. A descendant that survived a failed tree walk has
#: no remaining lifetime anyone here can bound, and elapsed time is not
#: evidence -- it is the absence of it. UNKNOWN stays UNKNOWN (the
#: rule already established for the pid probe itself).
#: ``None`` -- a resolution with no failure kind -- is in the set, and it is
#: worth saying why, because it is the one that clears most workspaces. It
#: means the executor WAITED for the worker process and collected its result:
#: `run_with_tree_kill` returned rather than raising, so the process it spawned
#: exited. That is an observation of the process, not of the task's success,
#: which is exactly the right basis here -- a worker that reports FAILED or
#: BLOCKED has still stopped writing.
#:
#: What such an ending accounts for is the worker PROCESS ModelQuad spawned.
#: What that worker itself LEFT RUNNING is a separate record on the row
#: (``worker_descendants``, schema v19): on Windows the runner
#: enumerates the worker's live descendants the moment it returns and
#: :func:`_descendant_hazard` reads them; a row with no such record -- written
#: before the runner looked, or on a platform where it does not look -- is
#: accounted for by its worker process only, and the statement says so.
_ENDINGS_THAT_ACCOUNT_FOR_THE_WORKER: frozenset[str | None] = frozenset(
    {
        None,  # a clean resolution: the executor waited and the process exited
        "terminated",  # death read from the process table, tree included
        "policy_refused",
        "duplicate_in_flight",
        "no_viable_worker",
        "provider_unavailable",
        "failed_to_start",
        "invalid_request",
    }
)

#: Deliberately NOT in the set above: ``exception`` and ``internal_error``.
#: They are written when something went wrong in a way nobody characterised,
#: from a wrapper that catches everything raised anywhere in the delegation --
#: including after a worker was spawned. "We do not know what happened" is the
#: opposite of an accounting, and a workspace question may not read it as one.

#: Endings that assert NOTHING EVER RAN. They account for a worker only on a
#: row that never recorded one: a pid on the row is the executor saying it
#: spawned something, and a spawned process is not accounted for by a label
#: claiming there was none.
#:
#: This is not hypothetical. An ``OSError`` on the pipe AFTER ``Popen``
#: succeeded is currently classified ``launch_failed`` and resolves
#: ``failed_to_start`` -- with the pid already published through ``on_spawn``.
#: Trusting the label there would clear a workspace whose worker is running.
_ENDINGS_THAT_ASSERT_NOTHING_RAN: frozenset[str] = frozenset(
    {
        "policy_refused",
        "duplicate_in_flight",
        "no_viable_worker",
        "provider_unavailable",
        "failed_to_start",
        "invalid_request",
    }
)

#: The same set as SQL sees it: the ``None`` above is a clean resolution with
#: no failure kind at all, which the query expresses as ``IS NULL`` rather than
#: as a member of an ``IN`` list.
_ACCOUNTED_FAILURE_KINDS: tuple[str, ...] = tuple(
    sorted(kind for kind in _ENDINGS_THAT_ACCOUNT_FOR_THE_WORKER if kind is not None)
)

#: :data:`_ENDINGS_THAT_ASSERT_NOTHING_RAN` as SQL sees it.
_NOTHING_RAN_FAILURE_KINDS: tuple[str, ...] = tuple(sorted(_ENDINGS_THAT_ASSERT_NOTHING_RAN))

#: The three answers to "may anything still be writing to this workspace?".
#: Only the last is a "no", and nothing that merely FAILED TO OBSERVE may reach
#: it: an unreadable database, a database that does not exist, an unprobeable
#: pid, an open row with no pid at all, a row this store cannot attribute to
#: any workspace, an unfinished scan, and a termination that was never
#: confirmed all land on ``unknown``.
WORKSPACE_VERDICTS: frozenset[str] = frozenset(
    {"may_be_mutating", "unknown", "no_delegation_in_flight"}
)

_WORKSPACE_STATEMENTS: dict[str, str] = {
    "may_be_mutating": (
        "A delegation ModelQuad started is still observed running against this "
        "workspace. Files here may change under you: a test run, a review or a "
        "commit taken now cannot be attributed to any single tree state."
    ),
    "unknown": (
        "Whether a delegation is still writing to this workspace could not be "
        "established. That is not a 'no': treat the workspace as possibly "
        "changing until this says otherwise."
    ),
    "no_delegation_in_flight": (
        "Every delegation ModelQuad recorded against this workspace ended in a "
        "way that accounts for the worker ModelQuad spawned and, where the "
        "runner looked, for the processes that worker left running when it "
        "exited. Two limits, both real: it knows only the delegations it "
        "started and recorded -- not editors, not other agents, not a "
        "delegation run with history disabled -- and a delegation recorded "
        "before ModelQuad looked at what a worker left behind is accounted "
        "for by its worker process only."
    ),
}


def _workspace_row_hazard(entry: dict[str, Any]) -> tuple[str, str] | None:
    """What one matched row contributes to the verdict, and why.

    ``("may_be_mutating", reason)`` for a positively OBSERVED live worker,
    ``("unknown", reason)`` for anything unfinished or unaccounted for, and
    None for a row that is genuinely settled.

    Two rules, and every case below is one of them:

    - An OPEN row can never be clean. It is an unfinished delegation by
      definition, and the strongest thing that can be said about one is what
      the process table says about its pid -- `alive` (a hazard we can see),
      or anything else (a hazard we cannot). A dead pid on an open row is not
      reassurance, it is a CONTRADICTION: the row says in flight and the pid
      says gone, and on Windows that is exactly the shape of a `.cmd` shim
      whose node child outlived it.
    - A RESOLVED row is clean only if its ending accounts for the worker
      (:data:`_ENDINGS_THAT_ACCOUNT_FOR_THE_WORKER`).

    Age is never consulted. ``annotate_attempt_liveness`` falls back to it for
    a pid-less row and labels the fallback as such; a merely young attempt is
    not an observed worker and a merely old one is not an ended one, so this
    reads the pid itself rather than that fallback.
    """

    from ..procutil import pid_state

    attempt = entry.get("id")
    if entry.get("resolved_at") is not None:
        kind = entry.get("failure_kind")
        if kind in _ENDINGS_THAT_ACCOUNT_FOR_THE_WORKER:
            # One exception, and it is about the row rather than the label: an
            # ending that asserts nothing ever ran is only believable where
            # nothing was ever observed to run. A recorded pid contradicts it.
            if kind in _ENDINGS_THAT_ASSERT_NOTHING_RAN and entry.get("worker_pid"):
                return (
                    "unknown",
                    f"attempt {attempt} ended '{kind}' -- which means no worker "
                    f"started -- yet it recorded worker pid {entry.get('worker_pid')}; "
                    "the label and the row disagree about whether anything ran",
                )
            # The worker itself is accounted for. What it LEFT RUNNING is the
            # remaining descendant hazard: read the row's record of
            # that, if the runner made one.
            return _descendant_hazard(entry)
        return (
            "unknown",
            f"attempt {attempt} ended '{kind}' without its worker's death being "
            "confirmed for the whole process tree, so a descendant of it may "
            "still be writing here",
        )
    pid = entry.get("worker_pid")
    if not pid:
        return (
            "unknown",
            f"attempt {attempt} is still open and no worker process was ever "
            "recorded for it, so nothing about it has been observed",
        )
    try:
        worker_pid = int(pid)
    except (TypeError, ValueError):
        return ("unknown", f"attempt {attempt} records a worker pid that cannot be read")
    state = pid_state(worker_pid)
    if state == "alive":
        return ("may_be_mutating", f"attempt {attempt}'s worker pid {worker_pid} is alive")
    if state == "unknown":
        return (
            "unknown",
            f"attempt {attempt}'s worker pid {worker_pid} could not be probed",
        )
    return (
        "unknown",
        f"attempt {attempt} is still open while its recorded worker pid "
        f"{worker_pid} is gone; the row and the process table disagree, and a "
        "descendant of that worker may have outlived it",
    )


def _descendant_hazard(entry: dict[str, Any], *, native_only=False) -> tuple[str, str] | None:
    """What a resolved, accounted row's left-behind processes contribute.

    NULL -- a row written before the runner looked -- is not evidence either
    way and keeps the verdict such rows always had (None: settled by its
    worker's accounting alone). A record that says enumeration FAILED is
    doubt, never a "no". A listed process is a hazard only when the process
    table reports it alive AND it is identity-matched -- the recorded image
    and a creation time within tolerance of the recorded one, the pid
    identity check -- so a pid recycled since by something else clears it. A live pid
    whose identity cannot be checked is doubt.
    """

    from ..procutil import pid_state, process_creation_epoch, process_image_name
    if native_only:
        from .. import procutil
        snapshot = procutil._windows_process_snapshot()
        images = {} if snapshot is None else {pid: image for pid, _parent, image in snapshot}
        process_image_name = images.get
        process_creation_epoch = procutil.native_process_creation_epoch

    raw = entry.get("worker_descendants")
    if raw is None:
        return None
    attempt = entry.get("id")
    try:
        facts = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ("unknown", f"attempt {attempt}'s record of what its worker left running cannot be read")
    if not isinstance(facts, dict):
        return ("unknown", f"attempt {attempt}'s record of what its worker left running cannot be read")
    census_doubt = None
    if not facts.get("enumerated"):
        if facts.get("reason") == "unsupported_platform" and not native_only:
            # Not looked at, and said so -- the same standing as a row written
            # before the runner looked: the descendant accounting with its stated
            # limit, which the no_delegation_in_flight statement names.
            return None
        census_doubt = (
            "unknown",
            f"attempt {attempt}'s worker exited but what it left running could not be "
            "enumerated, so a process of its own may still be writing here",
        )
        if not native_only:
            return census_doubt
    if facts.get("truncated"):
        census_doubt = (
            "unknown",
            f"attempt {attempt}'s worker left more processes running than the row records",
        )
        if not native_only:
            return census_doubt
    doubts: list[str] = []
    for item in facts.get("descendants") or ():
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        state = pid_state(pid)
        if state == "dead":
            continue
        if state != "alive":
            doubts.append(f"pid {pid} left by attempt {attempt}'s worker could not be probed")
            continue
        image, created = item.get("image"), item.get("created")
        if not isinstance(image, str) or not isinstance(created, (int, float)):
            doubts.append(
                f"pid {pid} left by attempt {attempt}'s worker is alive but its identity was "
                "not recorded well enough to tell it from a recycled pid"
            )
            continue
        live_image = process_image_name(pid)
        live_created = process_creation_epoch(pid)
        if live_image is None or live_created is None:
            doubts.append(
                f"pid {pid} left by attempt {attempt}'s worker is alive but could not be "
                "identified as the recorded process"
            )
            continue
        same_image = live_image.casefold() == image.casefold() if os.name == "nt" else live_image == image
        if same_image and abs(live_created - float(created)) <= _DESCENDANT_IDENTITY_TOLERANCE_SECONDS:
            return (
                "may_be_mutating",
                f"pid {pid} ({image}), left running by attempt {attempt}'s worker when it "
                "exited, is alive",
            )
        # Alive, but not the process that was recorded: the pid was recycled.
    if doubts:
        return ("unknown", "; ".join(doubts))
    return census_doubt


#: A recorded and a live creation time are the same instant within this;
#: POSIX reads creation as one-second elapsed time (see procutil).
_DESCENDANT_IDENTITY_TOLERANCE_SECONDS = 2.0


def _read_only_uri(db_path: str | Path) -> str:
    """A SQLite URI that opens this file read-only and never creates it.

    The percent-encoding is not decoration. SQLite parses everything after a
    ``?`` as URI parameters and everything after a ``#`` as a fragment, and it
    decodes ``%`` escapes -- so a store under a directory containing ``%`` (or
    ``?`` on POSIX, where it is a legal filename character) simply failed to
    open. That failed SAFE, because an unopenable store answers ``unknown``,
    but a workspace question that cannot read a perfectly good history is a
    question nobody will keep asking.
    """

    from urllib.parse import quote

    # `:` and `/` are kept literal: a Windows drive letter is part of the path
    # here, not a URI scheme, and SQLite accepts it that way.
    return "file:" + quote(Path(db_path).as_posix(), safe="/:") + "?mode=ro"


def workspace_delegation_liveness(
    db_path: str | Path,
    working_directory: str | Path,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """May a delegation ModelQuad started still be writing to this workspace?

    The question a caller can actually ask after losing a delegation's result.
    :func:`lookup_delegation_attempts` needs an attempt id, a
    decision id, or the exact ``task_type`` + ``working_directory`` + ``prompt``
    that was delegated with -- and the consumers this protects are a validation
    run, a review and a commit, which stand in a directory holding a PATH and
    nothing else. The prompt left with the result that never arrived. So the
    path has to be enough, and this is where it is.

    The answer is one of :data:`WORKSPACE_VERDICTS`. ``may_be_mutating``
    requires a worker pid the process table positively reports ALIVE;
    ``no_delegation_in_flight`` requires that both scans completed, that every
    row this store cannot attribute to a workspace is settled, and that every
    matched row ended in a way that accounts for its worker. Everything in
    between is ``unknown``, and ``unknown`` is not a "no" -- it is the honest
    report of a question this store could not close.

    Read-only, and it fabricates nothing. Nothing here starts, stops, kills,
    reconciles or retries anything. A database it cannot open -- or one that
    does not exist yet -- answers ``unknown`` rather than being created and
    then certified empty.
    """
    return _workspace_delegation_liveness_core(db_path, working_directory, limit=limit, now=now)


def _workspace_delegation_liveness_core(
    db_path: str | Path, working_directory: str | Path, *,
    limit: int = 20, now: datetime | None = None, diagnosis=False,
) -> dict[str, Any]:
    """Shared read-only liveness scan."""

    queried_key, queried_ancestors_json = workspace_identity(working_directory)
    answer: dict[str, Any] = {
        "workspace": str(working_directory),
        "verdict": "unknown",
        "attempts": [],
        "scan_truncated": False,
    }

    def unknown(reason: str) -> dict[str, Any]:
        answer["verdict"] = "unknown"
        answer["reason"] = reason
        answer["statement"] = _WORKSPACE_STATEMENTS["unknown"]
        return answer

    if queried_key is None:
        return unknown("the workspace path could not be resolved")
    queried_ancestors = _workspace_ancestor_set(queried_ancestors_json)
    if queried_ancestors is None:
        return unknown("the workspace's own ancestry could not be read")
    # Not `connect_and_init`, which CREATES the file and MIGRATES it: a store
    # this call brought into existence one line earlier is not evidence that no
    # delegation is running, and a read has no business writing. SQLite's
    # read-only URI mode refuses to create, so the race between the check below
    # and the open cannot end in a fabricated empty answer either.
    try:
        store_exists = Path(db_path).exists()
    except OSError:
        # `exists()` raises on a path the process may not even stat -- and a
        # probe that cannot answer must return the absence, not escape as an
        # exception from a function documented never to raise.
        return unknown("whether a delegation history exists here could not be checked")
    if not store_exists:
        return unknown(
            "no delegation history exists at this state store, so it cannot say "
            "what ModelQuad has running"
        )
    try:
        conn = sqlite3.connect(_read_only_uri(db_path), uri=True)
        conn.row_factory = sqlite3.Row
    except (sqlite3.Error, OSError) as exc:
        logger.warning("could not open the history database for a workspace question: %s", type(exc).__name__)
        return unknown("the delegation history could not be read")
    # Both hazard scans are narrowed to THIS workspace in SQL. A global scan
    # meant unrelated history could fill the bound and hold every directory at
    # `unknown` for ever. The `LIKE` reaches rows recorded BELOW the queried
    # directory, whose ancestry contains its key; the key is 32 hex characters,
    # so the pattern carries no SQL wildcard and matches nothing it should not.
    # The last two disjuncts pull in any row whose ancestry is not even shaped
    # like one this code wrote. Narrowing in SQL would otherwise EXCLUDE a
    # corrupt row, and a row dropped for being unreadable is a hazard silently
    # reclassified as unrelated -- the exact substitution this whole answer
    # refuses to make. A well-formed value is `[]` or begins `["`, so anything
    # else is suspect and gets read; `_workspace_ancestor_set` then decides.
    # SQLite's LIKE has no character classes, so the patterns are literal.
    scoped = (
        " AND (workspace_key IN (%s) OR workspace_ancestors LIKE ? "
        "OR workspace_ancestors IS NULL "
        "OR (workspace_ancestors <> '[]' AND workspace_ancestors NOT LIKE '[\"%%'))"
        % ", ".join("?" for _ in range(1 + len(queried_ancestors)))
    )
    scoped_params: tuple[Any, ...] = (
        queried_key,
        *sorted(queried_ancestors),
        '%"' + queried_key + '"%',
    )
    try:
        from . import fate_history
        conn.execute('BEGIN')
        released_descendant_hazards = fate_history.register_scan(conn)
        # A release removes a diagnosis block, not factual uncertainty. Query
        # existence separately so released historical rows cannot fill caps.
        released_present = conn.execute(
            'SELECT 1 FROM delegation_attempts WHERE fate_released(id)=1 AND workspace_key IS NOT NULL'
            + scoped + ' LIMIT 1', scoped_params,
        ).fetchone() is not None
        release_filter = ' AND fate_exempts(id)=0'
        # Three populations, bounded separately so none can crowd out another.
        # Open rows are the delegations that have not finished. Rows that ended
        # without accounting for their worker keep casting doubt. And rows this
        # store cannot attribute to ANY workspace -- written before schema v18,
        # or by a path that would not resolve -- cannot be shown to be about
        # this directory, and equally cannot be shown not to be, so they are
        # read under the same two conditions and reported without attribution.
        open_rows = conn.execute(
            "SELECT * FROM delegation_attempts "
            "WHERE status = 'started' AND resolved_at IS NULL "
            "AND workspace_key IS NOT NULL" + scoped + release_filter + " ORDER BY id DESC LIMIT ?",
            (*scoped_params, _WORKSPACE_SCAN_LIMIT),
        ).fetchall()
        # The second disjunct is the contradiction case: an ending that claims
        # nothing ran, on a row that recorded a pid. It is in the accounted set
        # and so would be filtered out here, which would hide it from
        # `_workspace_row_hazard` -- the one thing that can tell the label and
        # the row apart.
        # The third disjunct covers left-behind descendants: a row that ended cleanly but
        # carries a record of what its worker LEFT RUNNING must be read too,
        # because that record is the only place the hazard can live. A clean
        # row with no such record is settled by its worker's accounting.
        unaccounted_clause = (
            "resolved_at IS NOT NULL AND ((failure_kind IS NOT NULL AND ("
            "failure_kind NOT IN (%s) OR (failure_kind IN (%s) AND worker_pid IS NOT NULL)))"
            " OR worker_descendants IS NOT NULL)"
            % (
                ", ".join("?" for _ in _ACCOUNTED_FAILURE_KINDS),
                ", ".join("?" for _ in _NOTHING_RAN_FAILURE_KINDS),
            )
        )
        unaccounted_rows = conn.execute(
            "SELECT * FROM delegation_attempts WHERE workspace_key IS NOT NULL AND "
            + unaccounted_clause
            + scoped
            + release_filter
            + " ORDER BY id DESC LIMIT ?",
            (*_ACCOUNTED_FAILURE_KINDS, *_NOTHING_RAN_FAILURE_KINDS, *scoped_params, _WORKSPACE_SCAN_LIMIT),
        ).fetchall()
        # The unscoped population is BOTH shapes. Reading only the open ones
        # let a workspace-less row have its hazard erased by being resolved as
        # an unconfirmed timeout -- the row left the population instead of
        # keeping its doubt.
        unscoped = conn.execute(
            "SELECT * FROM delegation_attempts WHERE workspace_key IS NULL AND "
            "((status = 'started' AND resolved_at IS NULL) OR ("
            + unaccounted_clause
            + ")) ORDER BY id DESC LIMIT ?",
            (*_ACCOUNTED_FAILURE_KINDS, *_NOTHING_RAN_FAILURE_KINDS, _WORKSPACE_SCAN_LIMIT),
        ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("could not read delegation attempts for a workspace question: %s", type(exc).__name__)
        return unknown("the delegation history could not be read")
    finally:
        conn.close()
    answer["scan_truncated"] = any(
        len(rows) >= _WORKSPACE_SCAN_LIMIT
        for rows in (open_rows, unaccounted_rows, unscoped)
    )

    matched: list[dict[str, Any]] = []
    hazards: list[tuple[str, str]] = []
    for row in (*open_rows, *unaccounted_rows):
        entry = dict(row)
        relation = workspace_relation(
            entry.get("workspace_key"),
            entry.get("workspace_ancestors"),
            queried_key,
            queried_ancestors,
        )
        if relation is None:
            continue
        annotated = annotate_attempt_liveness(entry, now=now)
        annotated["workspace_relation"] = relation
        # SQL admission and factual reporting retain the same positive live
        # descendant observation even if a later process probe becomes unknown.
        hazard = (released_descendant_hazards[entry['id']]
                  if entry['id'] in released_descendant_hazards
                  else _workspace_row_hazard(annotated))
        annotated["workspace_hazard"] = None if hazard is None else hazard[0]
        annotated["workspace_hazard_reason"] = None if hazard is None else hazard[1]
        if hazard is not None:
            hazards.append(hazard)
        matched.append(annotated)

    # A row with no workspace on it is an ABSENCE, not a wildcard: it is never
    # reported as being about this directory. But it is an unfinished or
    # unaccounted-for delegation that COULD be about it, and calling the
    # workspace clear while one exists would be a clean answer produced by a
    # gap in the record. It contributes doubt only -- never `may_be_mutating`,
    # which is a positive claim about this directory that no unplaceable row
    # can support.
    for row in unscoped:
        entry = annotate_attempt_liveness(dict(row), now=now)
        if _workspace_row_hazard(entry) is None:
            continue
        hazards.append(
            (
                "unknown",
                f"attempt {entry.get('id')} has no workspace recorded in this store "
                "and has not been accounted for, so it cannot be ruled out of this one",
            )
        )

    # Bounded separately from the scans: the scans must be wide to be truthful,
    # the answer must be small to be readable. The verdict is derived from every
    # match, never from the page of them that is handed back.
    answer["attempts"] = matched[: max(0, int(limit))]

    live = [reason for kind, reason in hazards if kind == "may_be_mutating"]
    if live:
        answer["verdict"] = "may_be_mutating"
        answer["statement"] = _WORKSPACE_STATEMENTS["may_be_mutating"]
        answer["reason"] = "; ".join(live)
        return answer
    doubts = [reason for kind, reason in hazards if kind == "unknown"]
    if doubts:
        return unknown("; ".join(doubts))
    if answer["scan_truncated"]:
        # Nothing matched, but a scan did not finish. "Nothing in the part I
        # looked at" is not "nothing".
        return unknown(
            f"{_WORKSPACE_SCAN_LIMIT} or more delegation attempts matched one of "
            "this question's scans, so it did not read them all"
        )
    if released_present and not diagnosis:
        return unknown('a recorded workspace block was released; worker or tree death remains unconfirmed')
    answer["verdict"] = "no_delegation_in_flight"
    answer["statement"] = _WORKSPACE_STATEMENTS["no_delegation_in_flight"]
    return answer


def diagnosis_admission(db_path, working_directory, *, now=None):
    """Permission for diagnosis only; never certifies workspace isolation/death."""
    result = _workspace_delegation_liveness_core(
        db_path, working_directory, now=now, diagnosis=True)
    return {'allowed': result['verdict'] == 'no_delegation_in_flight',
            'reason': None if result['verdict'] == 'no_delegation_in_flight' else 'workspace_unsettled'}


def fetch_delegation_attempts(
    db_path: str | Path, *, limit: int = 100, unresolved_only: bool = False
) -> list[dict[str, Any]]:
    """Recent delegation attempts, newest first.

    ``unresolved`` marks a row that began and never came back; ``liveness``
    separates one that may still be running from one that cannot be
    (see :func:`annotate_attempt_liveness`).
    """

    conn = _db.connect_and_init(db_path)
    try:
        query = "SELECT * FROM delegation_attempts"
        if unresolved_only:
            query += " WHERE resolved_at IS NULL"
        query += " ORDER BY id DESC LIMIT ?"
        rows = conn.execute(query, (max(1, int(limit)),)).fetchall()
        return [annotate_attempt_liveness(dict(row)) for row in rows]
    finally:
        conn.close()


def fetch_open_delegation_attempts(
    db_path: str | Path, *, limit: int = 50, now: datetime | None = None,
    workspace_key: str | None = None,
) -> list[dict[str, Any]]:
    """The delegation attempts that no execution row will ever account for.

    Exactly two shapes qualify, and both are things a person should be able
    to see:

    - still ``started`` with no ``resolved_at`` -- the attempt began and has
      not come back. :func:`annotate_attempt_liveness` splits that into one
      that may still be running and one that cannot be.
    - ``refused`` -- ModelQuad declined before anything ran, so there is no
      execution to show and the refusal itself is the whole record.

    A ``completed`` or ``failed`` attempt is deliberately excluded: its
    execution row is the richer record of the same event, and listing both
    would show one run twice. Each row is tagged ``kind="attempt"`` so a
    caller can tell it apart from the two history tables without inspecting
    its columns. ``workspace_key`` keeps only attempts pointed at that
    directory.
    """

    scope, params = "", [max(0, int(limit))]
    if workspace_key is not None:
        scope, params = " AND workspace_key = ?", [workspace_key, *params]
    conn = _db.connect_and_init(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM delegation_attempts "
            "WHERE ((status = 'started' AND resolved_at IS NULL) OR status = 'refused')"
            + scope + " ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = annotate_attempt_liveness(dict(row), now=now)
        entry["kind"] = "attempt"
        entries.append(entry)
    return entries


#: The execution_history columns a recovery answer may carry. Explicit rather
#: than ``SELECT *`` because this row is handed to a caller that has already
#: lost one result and wants the next answer to be small: ``metadata_json``
#: alone can embed a megabyte of worker stdout. The full record stays one
#: ``modelquad_activity_detail(kind="execution", id=...)`` away, and the id
#: to ask with is in here.
_RECOVERY_EXECUTION_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "task_type",
    "provider",
    "model_confirmed",
    "success",
    "exit_code",
    "duration_seconds",
    "result",
    "error",
    "task_status",
    "task_status_reason",
    "agent_runtime",
    "decision_id",
)


def fetch_execution_for_decision(
    db_path: str | Path, decision_id: str | None
) -> dict[str, Any] | None:
    """The newest execution recorded under one decision id, in summary form.

    The join a caller cannot make for itself. An attempt row says how the
    delegation ENDED; this row is what the worker actually produced -- its
    exit code, its own status and reason, and the bounded ``error`` beside
    them. They are joined on ``decision_id``, the same lineage key
    :func:`fetch_current_task` and the MCP layer already use, so a recovered
    result is attributed to one identity or to none.

    Newest wins because one decision may legitimately own several executions
    (fallback, retry) and the last one is the one that settled it. ``None``
    is an honest empty: no execution has been recorded under this decision,
    which is the normal state while a worker is still running.
    """

    if not decision_id:
        return None
    columns = ", ".join(_RECOVERY_EXECUTION_COLUMNS)
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute(
            f"SELECT {columns} FROM execution_history "
            "WHERE decision_id = ? ORDER BY id DESC LIMIT 1",
            (str(decision_id),),
        ).fetchone()
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()
    if row is None:
        return None
    entry = dict(row)
    if entry.get("success") is not None:
        entry["success"] = bool(entry["success"])
    entry["error"] = _truncate_error(entry.get("error"))
    entry["kind"] = "execution"
    return entry


def _truncate_error(error: Any) -> Any:
    """Bound a worker error field the way the activity feed bounds it."""

    if not isinstance(error, str) or len(error) <= _MAX_ATTEMPT_DETAIL_CHARS:
        return error
    return error[:_MAX_ATTEMPT_DETAIL_CHARS] + "... (truncated)"


def lookup_delegation_attempts(
    db_path: str | Path,
    *,
    attempt_id: int | None = None,
    decision_id: str | None = None,
    request_key: str | None = None,
    limit: int = 5,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every attempt matching one durable identity, newest first.

    This is the READ half of caller recovery. A caller whose
    transport stopped waiting holds no attempt id -- the result that would
    have carried one never arrived -- but it does still hold the three
    arguments it delegated with, and :func:`request_fingerprint` turns those
    into the ``request_key`` this row was written under. That fingerprint,
    the ``decision_id`` from a prior route, and the attempt id itself are the
    three identities that survive the caller's departure; any one of them
    finds the row.

    Each entry is annotated by :func:`annotate_attempt_liveness` -- so the
    worker is genuinely probed rather than inferred -- and carries
    ``execution`` : the summary row :func:`fetch_execution_for_decision`
    finds under the same decision, or ``None`` when nothing has been recorded
    yet. Nothing here writes, reconciles, terminates or retries: it is a
    reading, and a reading that fabricates nothing. An identity that matches
    no row returns ``[]``, which means "no such attempt in this state store",
    never "the attempt failed".

    Several attempts may share one ``request_key`` (the first was abandoned,
    a later one refused as a duplicate), so this returns a list; ``limit``
    bounds it and the newest is first.
    """

    clauses: list[str] = []
    params: list[Any] = []
    if attempt_id is not None:
        clauses.append("id = ?")
        params.append(int(attempt_id))
    if decision_id is not None:
        clauses.append("decision_id = ?")
        params.append(str(decision_id))
    if request_key is not None:
        clauses.append("request_key = ?")
        params.append(str(request_key))
    if not clauses:
        # No identity is not the same as "match everything": handing back the
        # most recent delegations to a caller who asked about a specific one
        # would invite it to read another request's outcome as its own.
        return []
    query = (
        "SELECT * FROM delegation_attempts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?"
    )
    params.append(max(0, int(limit)))
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return []
    try:
        rows = conn.execute(query, params).fetchall()
    except (sqlite3.Error, OSError):
        return []
    finally:
        conn.close()
    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = annotate_attempt_liveness(dict(row), now=now)
        entry["kind"] = "attempt"
        entry["execution"] = fetch_execution_for_decision(db_path, entry.get("decision_id"))
        entries.append(entry)
    return entries


#: How long a FINISHED attempt keeps describing "now".
#:
#: The Current Task card is a statement about the present, so a row that is
#: over may only hold it for as long as its outcome is still the answer to
#: "what just happened here". Fifteen minutes is long enough that a user who
#: looks up after a handoff completes still sees how it ended, and short
#: enough that it can never be mistaken for live work. Without a bound, the
#: newest row IS the current task by definition, which is how an eight-day-old
#: policy refusal came to be headlined CURRENT TASK.
_CURRENT_TASK_RECENT_SECONDS = 900.0

#: How far back to look for a current attempt. The newest row is not always
#: the live one -- an instantly-refused attempt can be recorded while an
#: earlier worker is still running -- so the scan walks back rather than
#: blanking the card the moment something else lands on top of live work.
_CURRENT_TASK_SCAN_LIMIT = 50


def _attempt_is_current(entry: dict[str, Any], reference: datetime) -> bool:
    """Is this attempt still the task that is happening now?

    Two limbs, in this order:

    * **Liveness.** An attempt nothing ever resolved is current for as long
      as it stays open, whatever its age -- ModelQuad has not accounted for
      that worker, and a worker it cannot account for is precisely what it
      must not stop showing. The one exception is
      ``abandoned``: there the product has already concluded, from a dead pid
      or from the row outliving any plausible run, that nothing is executing.
    * **Recency.** Anything else is over, and stays current only while its
      ending is recent.

    An unparseable timestamp reads *not* current. Being current is a claim
    the card makes out loud, so it needs evidence; a row we cannot date
    supplies none.
    """

    if entry.get("unresolved") and entry.get("liveness") != "abandoned":
        return True
    stamp = entry.get("resolved_at") or entry.get("created_at")
    try:
        finished = datetime.fromisoformat(str(stamp))
    except (ValueError, TypeError):
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return (reference - finished).total_seconds() <= _CURRENT_TASK_RECENT_SECONDS


def fetch_current_task(
    db_path: str | Path, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """The delegation attempt happening NOW, joined to the routing decision that caused it.

    This is the row the Current Task surface describes. It is deliberately
    an *attempt* rather than a routing decision: a route-only consultation
    that was never executed is a recommendation, not a task in flight, and
    showing it as one would be the UI inventing work that never started.

    "Current" is a test the row has to pass -- :func:`_attempt_is_current` --
    not a synonym for "newest". The newest row that passes selects the
    decision; an open current attempt within that decision takes precedence
    over a resolved child. Cold endings are stepped over, so a live attempt
    underneath one is still found.

    Returns the attempt with its ``liveness`` annotation plus the routing
    row's ``reason``/``decision_source`` under ``routing``. ``None`` means
    nothing is current in THIS state store -- either no attempt was ever
    recorded, or none is current any more. Both are an honest empty, and the
    surface says "nothing running" for both rather than reaching for the
    most recent thing it can find.
    """

    reference = now or datetime.now(timezone.utc)
    conn = _db.connect_and_init(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM delegation_attempts ORDER BY id DESC LIMIT ?",
            (_CURRENT_TASK_SCAN_LIMIT,),
        ).fetchall()
        entry: dict[str, Any] | None = None
        for row in rows:
            candidate = annotate_attempt_liveness(dict(row), now=reference)
            if _attempt_is_current(candidate, reference):
                entry = candidate
                break
        if entry is None:
            return None
        # Preserve newest-decision selection, but prefer its open worker over
        # a resolved planner. The first implementation reuses the older owner.
        if entry.get("resolved_at") is not None and entry.get("decision_id"):
            for row in rows:
                if (row["decision_id"] == entry["decision_id"]
                        and row["status"] == "started" and row["resolved_at"] is None):
                    candidate = annotate_attempt_liveness(dict(row), now=reference)
                    if _attempt_is_current(candidate, reference):
                        entry = candidate
                        break
            # Once both rows resolve, the owning delegation carries the stop
            # outcome; the completed/failed planner does not describe it.
            if entry.get("resolved_at") is not None:
                from orchestrator.mcp.presentation import user_stop_facts

                stopped_owners = [
                    row for row in rows
                    if row["decision_id"] == entry["decision_id"]
                    and row["id"] < entry["id"]
                    and row["resolved_at"] is not None
                    and not user_stop_facts(entry)[0]
                    and user_stop_facts(dict(row))[0]
                ]
                if len(stopped_owners) == 1:
                    candidate = annotate_attempt_liveness(dict(stopped_owners[0]), now=reference)
                    if _attempt_is_current(candidate, reference):
                        entry = candidate
        entry["kind"] = "attempt"
        decision_id = entry.get("decision_id")
        routing: dict[str, Any] | None = None
        if decision_id:
            # The routing row carries WHY this provider was chosen. It is
            # joined on decision_id -- the same lineage key MCP uses -- so
            # both surfaces attribute a task to one identity or to none.
            match = conn.execute(
                "SELECT reason, decision_source, recommendation, selected_provider, "
                "overridden, created_at FROM routing_decisions "
                "WHERE decision_id = ? ORDER BY id DESC LIMIT 1",
                (str(decision_id),),
            ).fetchone()
            if match is not None:
                routing = dict(match)
                routing["overridden"] = bool(routing.get("overridden"))
            # The planner keeps its own immutable ending. Attach only the
            # outer stop facts so the card and activity describe that ending
            # in the context of the same delegation, without changing owners.
            stop_row = conn.execute(
                "SELECT task_status_reason, metadata_json FROM execution_history "
                "WHERE decision_id = ? ORDER BY id DESC LIMIT 1",
                (str(decision_id),),
            ).fetchone()
            if stop_row is not None and stop_row["task_status_reason"] in (
                "stop_requested_after_planner_completed", "stop_requested_after_planner_ran",
            ):
                stop_metadata = _compact_execution_metadata(
                    stop_row["metadata_json"], stop_row["task_status_reason"],
                )
                entry["execution"] = {
                    "task_status_reason": stop_row["task_status_reason"],
                    "metadata": {
                        key: value for key, value in stop_metadata.items()
                        if key in ("stop_requested", "worker_confirmed_dead", "stopped_before_start")
                    },
                }
        entry["routing"] = routing
        return entry
    finally:
        conn.close()


def fetch_decision_sources(
    db_path: str | Path, decision_ids: Any
) -> dict[str, str]:
    """Map each given ``decision_id`` to the recorded ``decision_source``.

    This is the v11 lineage join, read-only: an execution or delegation
    attempt carries no ``decision_source`` of its own, but the routing row
    that caused it does, and one ``decision_id`` joins them. Ids with no
    routing row, or a routing row that predates ``decision_source``, are
    simply absent from the result -- the caller must then say nothing rather
    than assume a method.
    """

    wanted = [str(item) for item in dict.fromkeys(decision_ids or ()) if item]
    if not wanted:
        return {}
    sources: dict[str, str] = {}
    conn = _db.connect_and_init(db_path)
    try:
        # Chunked so a long activity page cannot exceed SQLite's variable
        # limit; ordering by id ascending lets a later row win, which matches
        # fetch_routing_decision_by_decision_id taking the newest row.
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT decision_id, decision_source FROM routing_decisions "
                f"WHERE decision_id IN ({placeholders}) ORDER BY id ASC",
                chunk,
            ).fetchall()
            for row in rows:
                if row["decision_source"]:
                    sources[str(row["decision_id"])] = str(row["decision_source"])
    except sqlite3.Error:
        # A lineage lookup is an enrichment: failing it must leave the
        # activity surface saying less, never break it.
        return {}
    finally:
        conn.close()
    return sources


def record_provider_health(
    db_path: str | Path,
    doctor_report: Any,
    provider_names: tuple[str, ...] | None = None,
) -> None:
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        if provider_names is None:
            from ..registry import default_registry

            provider_names = tuple(plugin.id for plugin in default_registry().all())
        checks_by_name = {check.name: check for check in doctor_report.checks}
        checked_at = datetime.now(timezone.utc).isoformat()
        now = datetime.now(timezone.utc)
        for provider in provider_names:
            cli_detected, adapter_installed = provider_capabilities(checks_by_name, provider)
            details = [
                check.detail
                for check in (checks_by_name.get(f"{provider} CLI"), checks_by_name.get(f"{provider} adapter installed"))
                if check is not None
            ]
            values = (
                1 if cli_detected else 0,
                None if cli_detected is None else int(cli_detected),
                None if adapter_installed is None else int(adapter_installed),
                _privacy.sanitize_persisted_text(" | ".join(details)) if details else None,
            )
            # Health rarely changes; an identical row every 60s tick is pure
            # churn. Skip unless the values differ or the heartbeat is due.
            last = conn.execute(
                "SELECT checked_at, available, cli_detected, adapter_installed, detail "
                "FROM provider_health WHERE provider = ? ORDER BY id DESC LIMIT 1",
                (provider,),
            ).fetchone()
            if (
                last is not None
                and (last["available"], last["cli_detected"], last["adapter_installed"], last["detail"])
                == values
                and not _heartbeat_due(last["checked_at"], now)
            ):
                continue
            conn.execute(
                "INSERT INTO provider_health "
                "(provider, checked_at, available, cli_detected, adapter_installed, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (provider, checked_at, *values),
            )
        conn.commit()
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()


def fetch_usage_history(
    db_path: str | Path,
    *,
    provider: str | None = None,
    resource_pool: str | None = None,
    since_hours: float | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Usage rows, newest first.

    By default only headline rows are returned (one per integration per tick:
    ``resource_pool`` NULL for pre-V6 rows or equal to the provider id) so
    per-provider charts keep one series. Pass ``resource_pool`` to read one
    specific pool's series, sub-pools included.
    """

    conn = _db.connect_and_init(db_path)
    try:
        query = (
            "SELECT provider, observed_at, recorded_at, remaining_percentage, "
            "freshness, available, usage_source, resource_pool FROM provider_usage_history"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if resource_pool:
            clauses.append("resource_pool = ?")
            params.append(resource_pool)
        else:
            clauses.append("(resource_pool IS NULL OR resource_pool = provider)")
        if since_hours is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
            clauses.append("recorded_at >= ?")
            params.append(cutoff)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        rows = conn.execute(query, params).fetchall()
        return [
            {**dict(row), "available": bool(row["available"])}
            for row in rows
        ]
    finally:
        conn.close()


# Execution metadata can embed a CLI worker's raw stdout/stderr verbatim --
# a single review run has been observed carrying over 1MB of it in
# `metadata_json.stderr` alone. A normal activity listing keeps only these
# small, genuinely summary-shaped keys by default; the full blob is still
# available, in full, via `fetch_activity_detail`.
_COMPACT_METADATA_KEYS = frozenset(
    {
        "command",
        "requested_model",
        "confirmed_model",
        "requested_reasoning_effort",
        "timed_out",
        "agent_runtime",
        "resource_pool",
        # Where the task came from and what ran it, as two separate records
        # (never merged -- AGENTS.md "Evidence, not self-report").
        "host_handoff",
        "host_outcome",
    }
)

# Past this length, `error` (raw CLI stderr on a failed run) is truncated in
# the default compact listing for the same reason metadata is filtered.
_MAX_COMPACT_ERROR_CHARS = 500


def _compact_execution_metadata(
    raw_metadata_json: str | None, task_status_reason: str | None = None,
) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_metadata_json) if raw_metadata_json else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    compact = {key: value for key, value in parsed.items() if key in _COMPACT_METADATA_KEYS}
    # Only bounded intent and fate cross into listings, never kill diagnostics.
    before_start = task_status_reason == "stop_requested" and parsed.get("worker_started") is False
    if type(parsed.get("stop_requested")) is bool or before_start:
        compact["stop_requested"] = parsed.get("stop_requested") is True or before_start
        compact["worker_confirmed_dead"] = worker_confirmed_dead(parsed)
        compact["stopped_before_start"] = before_start
    return compact


def _outcome_verified(raw_outcome_evidence_json: str | None) -> bool | None:
    """Did ModelQuad itself verify this outcome, or is it the worker's word?

    True only when ``OutcomeEvidence.verified`` was recorded True -- the
    orchestrator ran objective checks. False when evidence exists and does
    not reach that bar. None when no outcome evidence was recorded at all,
    which is a third, genuinely different state and must not collapse into
    False for a caller that wants to say so.
    """

    try:
        parsed = json.loads(raw_outcome_evidence_json) if raw_outcome_evidence_json else None
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    verified = parsed.get("verified")
    return bool(verified) if isinstance(verified, bool) else None


def _compact_error(error: str | None) -> str | None:
    if error is None or len(error) <= _MAX_COMPACT_ERROR_CHARS:
        return error
    return (
        error[:_MAX_COMPACT_ERROR_CHARS]
        + f"... [truncated, {len(error)} chars total -- use fetch_activity_detail for the full text]"
    )


#: A history row is in a workspace when an attempt with its decision_id was.
_IN_WORKSPACE = (
    "decision_id IN (SELECT decision_id FROM delegation_attempts "
    "WHERE decision_id IS NOT NULL AND workspace_key = ?)"
)


def fetch_activity(
    db_path: str | Path,
    *,
    limit: int = 50,
    provider: str | None = None,
    task_type: str | None = None,
    result: str | None = None,
    overridden: bool | None = None,
    fallback: bool | None = None,
    include_metadata: bool = False,
    workspace_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent routing/execution activity, compact by default.

    ``workspace_key`` (a :func:`workspace_identity` hash) keeps only the rows
    whose decision has a delegation attempt pointed at that directory. The
    history rows carry no workspace of their own; the attempt sharing their
    ``decision_id`` does. A row that cannot be attributed (a CLI ``route``
    with no delegation) is left out of a scoped listing, never guessed in.

    ``include_metadata=False`` (the default) strips each execution entry's
    raw ``metadata_json`` (which can embed 1MB+ of worker stdout/stderr)
    down to a small, genuinely summary-shaped ``metadata`` dict, and
    truncates an overlong ``error`` field -- this is what keeps a normal
    listing call boundable in size regardless of how much any individual
    run's CLI worker printed. Pass ``include_metadata=True`` for the
    pre-existing full-fidelity behavior (for internal callers that
    render full worker output), or call
    ``fetch_activity_detail`` to fetch one specific entry's full record.
    """
    conn = _db.connect_and_init(db_path)
    try:
        limit = max(0, int(limit))
        routing_rows = []
        if result is None and fallback is None:
            routing_query = "SELECT * FROM routing_decisions"
            routing_clauses: list[str] = []
            routing_params: list[Any] = []
            if provider is not None:
                routing_clauses.append("(recommendation = ? OR selected_provider = ?)")
                routing_params.extend((provider, provider))
            if task_type is not None:
                routing_clauses.append("task_type = ?")
                routing_params.append(task_type)
            if overridden is not None:
                routing_clauses.append("overridden = ?")
                routing_params.append(1 if overridden else 0)
            if workspace_key is not None:
                routing_clauses.append(_IN_WORKSPACE)
                routing_params.append(workspace_key)
            if routing_clauses:
                routing_query += " WHERE " + " AND ".join(routing_clauses)
            routing_query += " ORDER BY created_at DESC LIMIT ?"
            routing_params.append(limit)
            routing_rows = conn.execute(routing_query, routing_params).fetchall()

        execution_rows = []
        if overridden is None:
            execution_query = "SELECT * FROM execution_history"
            execution_clauses: list[str] = []
            execution_params: list[Any] = []
            if provider is not None:
                execution_clauses.append("provider = ?")
                execution_params.append(provider)
            if task_type is not None:
                execution_clauses.append("task_type = ?")
                execution_params.append(task_type)
            if result is not None:
                if result == "error":
                    execution_clauses.append("(result = ? OR result IS NULL)")
                else:
                    execution_clauses.append("result = ?")
                execution_params.append(result)
            if fallback is not None:
                execution_clauses.append("fallback = ?")
                execution_params.append(1 if fallback else 0)
            if workspace_key is not None:
                execution_clauses.append(_IN_WORKSPACE)
                execution_params.append(workspace_key)
            if execution_clauses:
                execution_query += " WHERE " + " AND ".join(execution_clauses)
            execution_query += " ORDER BY created_at DESC LIMIT ?"
            execution_params.append(limit)
            execution_rows = conn.execute(execution_query, execution_params).fetchall()
        entries: list[dict[str, Any]] = []
        for row in routing_rows:
            entry = dict(row)
            entry["kind"] = "routing"
            entry["overridden"] = bool(entry["overridden"])
            try:
                entry["providers"] = json.loads(entry.pop("providers_json"))
            except (TypeError, ValueError):
                entry.pop("providers_json", None)
                entry["providers"] = []
            entries.append(entry)
        for row in execution_rows:
            entry = dict(row)
            entry["kind"] = "execution"
            if entry["success"] is not None:
                entry["success"] = bool(entry["success"])
            entry["fallback"] = bool(entry["fallback"])
            entry["outcome_verified"] = _outcome_verified(entry.get("outcome_evidence_json"))
            if include_metadata:
                try:
                    entry["metadata"] = json.loads(entry.pop("metadata_json"))
                except (TypeError, ValueError):
                    entry.pop("metadata_json", None)
                    entry["metadata"] = {}
            else:
                entry["metadata"] = _compact_execution_metadata(
                    entry.pop("metadata_json", None), entry.get("task_status_reason"),
                )
                entry["error"] = _compact_error(entry.get("error"))
                entry.pop("outcome_evidence_json", None)
            entries.append(entry)
        entries.sort(key=lambda item: item["created_at"], reverse=True)
        return entries[:limit]
    finally:
        conn.close()


def fetch_activity_detail(
    db_path: str | Path, kind: str, entry_id: int, *, workspace_key: str | None = None
) -> dict[str, Any] | None:
    """Return the full record for exactly one routing or execution entry.

    The bounded, compact `fetch_activity` listing is deliberately lossy
    (see `include_metadata`); this is the explicit, single-record retrieval
    path for when a worker's full stdout/stderr is actually needed, e.g.
    while debugging one specific blocked or failed run. Returns None if no
    entry with that id/kind exists -- or, with ``workspace_key``, none in that
    workspace (see :func:`fetch_activity`).
    """

    if kind not in {"routing", "execution"}:
        raise ValueError(f"Unknown kind {kind!r}; must be 'routing' or 'execution'.")
    conn = _db.connect_and_init(db_path)
    try:
        table = "routing_decisions" if kind == "routing" else "execution_history"
        if workspace_key is None:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entry_id,)).fetchone()
        else:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND {_IN_WORKSPACE}",
                               (entry_id, workspace_key)).fetchone()
        if row is None:
            return None
        entry = dict(row)
        entry["kind"] = kind
        if kind == "routing":
            entry["overridden"] = bool(entry["overridden"])
            try:
                entry["providers"] = json.loads(entry.pop("providers_json"))
            except (TypeError, ValueError):
                entry.pop("providers_json", None)
                entry["providers"] = []
        else:
            if entry["success"] is not None:
                entry["success"] = bool(entry["success"])
            entry["fallback"] = bool(entry["fallback"])
            try:
                entry["metadata"] = json.loads(entry.pop("metadata_json"))
            except (TypeError, ValueError):
                entry.pop("metadata_json", None)
                entry["metadata"] = {}
            audit = _file_audit_view(conn, entry)
            if audit is not None:
                entry["file_audit"] = audit
        return entry
    finally:
        conn.close()


def _file_audit_view(conn: sqlite3.Connection, entry: dict[str, Any]) -> dict[str, Any] | None:
    """The metadata-only record of the files one delegation sent to its worker.

    Written by the Codex executor into the execution's metadata (relative
    paths, byte counts, SHA-256 prefixes, and whether the user approved through
    the delegate prompt or the handoff form); never file contents. The project
    key is the delegation attempt's hashed workspace key, not a path.
    """
    metadata = entry.get("metadata")
    audit = metadata.get("file_audit") if isinstance(metadata, dict) else None
    if not isinstance(audit, dict):
        return None
    project_key = None
    if entry.get("decision_id"):
        row = conn.execute(
            "SELECT workspace_key FROM delegation_attempts WHERE decision_id = ? "
            "AND workspace_key IS NOT NULL ORDER BY id DESC LIMIT 1",
            (entry["decision_id"],),
        ).fetchone()
        project_key = row[0] if row is not None else None
    return {
        "recorded_at": entry.get("created_at"),
        "project_key": project_key,
        "approval": audit.get("approval"),
        "sent_files": audit.get("sent") or [],
        "skipped_files": audit.get("skipped") or [],
        "total_bytes": audit.get("total_bytes"),
    }


def _window_cutoff(since_hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()


def fetch_reliability(
    db_path: str | Path,
    *,
    since_hours: float = 168,
    by_task_type: bool = False,
) -> list[dict[str, Any]]:
    conn = _db.connect_and_init(db_path)
    try:
        grouping = "task_type, provider" if by_task_type else "provider"
        selected_task_type = "task_type, " if by_task_type else ""
        rows = conn.execute(
            "SELECT "
            + selected_task_type
            + "provider, "
            + "SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) AS success, "
            + "SUM(CASE WHEN result = 'failure' THEN 1 ELSE 0 END) AS failure, "
            + "SUM(CASE WHEN result = 'timeout' THEN 1 ELSE 0 END) AS timeout, "
            + "SUM(CASE WHEN result = 'blocked' THEN 1 ELSE 0 END) AS blocked, "
            + "SUM(CASE WHEN result = 'partial' THEN 1 ELSE 0 END) AS partial, "
            + "SUM(CASE WHEN result IN "
            + "('success', 'failure', 'timeout', 'blocked', 'partial') THEN 0 ELSE 1 END) AS error, "
            + "COUNT(*) AS total FROM execution_history WHERE created_at >= ? "
            + f"GROUP BY {grouping} ORDER BY total DESC, {grouping} ASC",
            (_window_cutoff(since_hours),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_settings(db_path: str | Path) -> dict[str, Any]:
    conn = _db.connect_and_init(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_SETTINGS_KEY,)
        ).fetchone()
        if row is None:
            return _default_settings()
        return json.loads(row["value"])
    finally:
        conn.close()


def get_provider_overrides(db_path: str | Path) -> dict[str, bool]:
    """Return persisted provider enablement overrides, or an empty mapping."""

    conn = _db.connect_and_init(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_PROVIDER_OVERRIDES_KEY,)
        ).fetchone()
        if row is None:
            return {}
        decoded = json.loads(row["value"])
        if not isinstance(decoded, dict):
            return {}
        return {
            str(provider): enabled
            for provider, enabled in decoded.items()
            if isinstance(enabled, bool)
        }
    finally:
        conn.close()


def put_provider_override(db_path: str | Path, provider: str, enabled: bool) -> dict[str, bool]:
    """Persist one provider enablement override and return the full mapping."""

    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    overrides = get_provider_overrides(db_path)
    overrides[provider] = enabled
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PROVIDER_OVERRIDES_KEY, json.dumps(overrides)),
        )
        conn.commit()
    finally:
        conn.close()
    return overrides


def get_resource_strategy(db_path: str | Path) -> dict[str, str]:
    """Return persisted session resource strategy, or an empty mapping."""

    conn = _db.connect_and_init(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_RESOURCE_STRATEGY_KEY,)
        ).fetchone()
        if row is None:
            return {}
        decoded = json.loads(row["value"])
        if not isinstance(decoded, dict) or any(
            not isinstance(value, str) or value not in _VALID_RESOURCE_STRATEGIES
            for value in decoded.values()
        ):
            raise LaunchStateUnavailable("resource_strategy_invalid")
        return {
            str(provider): str(strategy)
            for provider, strategy in decoded.items()
            if strategy in _VALID_RESOURCE_STRATEGIES
        }
    finally:
        conn.close()


def put_resource_strategy(db_path: str | Path, strategy: dict[str, str]) -> dict[str, str]:
    """Persist a full replacement resource strategy (unlike put_provider_override, not a per-key merge)."""

    if not isinstance(strategy, dict):
        raise ValueError("strategy must be a dictionary")
    cleaned: dict[str, str] = {}
    for provider, intent in strategy.items():
        if intent not in _VALID_RESOURCE_STRATEGIES:
            raise ValueError(
                f"Invalid strategy {intent!r} for provider {provider!r}; "
                f"must be one of {sorted(_VALID_RESOURCE_STRATEGIES)}"
            )
        cleaned[str(provider)] = str(intent)

    conn = _db.connect_and_init(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_RESOURCE_STRATEGY_KEY, json.dumps(cleaned)),
        )
        conn.commit()
    finally:
        conn.close()
    return cleaned


_DISCONNECTED_HOSTS_KEY = "disconnected_hosts"


def _validated_disconnected_hosts(hosts: Any) -> list[str]:
    from ..routing_policy import VALID_HOST_IDS

    if not isinstance(hosts, list) or any(
        not isinstance(host, str) or host not in VALID_HOST_IDS for host in hosts
    ):
        raise ValueError("disconnected_hosts must be a list of valid Host ids")
    return sorted(set(hosts))


def put_disconnected_hosts(db_path: str | Path, hosts: list[str]) -> list[str]:
    """Persist only Host ids; never edit a Host's configuration."""
    cleaned = _validated_disconnected_hosts(hosts)
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_DISCONNECTED_HOSTS_KEY, json.dumps(cleaned)),
        )
        conn.commit()
    finally:
        conn.close()
    return cleaned


def set_host_disconnected(db_path: str | Path, host_id: str, disconnected: bool) -> list[str]:
    """Atomically change one Host without losing another concurrent choice."""
    _validated_disconnected_hosts([host_id])
    if type(disconnected) is not bool:
        raise ValueError("disconnected must be a boolean")
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        hosts = set(_launch_state_from_connection(conn)["disconnected_hosts"])
        if disconnected:
            hosts.add(host_id)
        else:
            hosts.discard(host_id)
        cleaned = sorted(hosts)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_DISCONNECTED_HOSTS_KEY, json.dumps(cleaned)),
        )
        conn.commit()
        return cleaned
    finally:
        conn.close()


class LaunchStateUnavailable(ValueError):
    """Required persisted controls could not be established; never a default."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _launch_state_from_connection(conn) -> dict[str, Any]:
    rows = dict(conn.execute(
        "SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?, ?, ?)",
        (_ROUTING_MODE_KEY, _ROUTING_PAUSED_KEY, _RESOURCE_STRATEGY_KEY, _DECISION_MODE_KEY,
         _DISCONNECTED_HOSTS_KEY, 'routing_paused_since'),
    ).fetchall())
    disconnected_hosts = []
    if _DISCONNECTED_HOSTS_KEY in rows:
        try:
            disconnected_hosts = _validated_disconnected_hosts(json.loads(rows[_DISCONNECTED_HOSTS_KEY]))
        except (ValueError, TypeError):
            raise LaunchStateUnavailable("disconnected_hosts_invalid") from None
    mode = DEFAULT_ROUTING_MODE
    paused = DEFAULT_ROUTING_PAUSED
    if _ROUTING_MODE_KEY in rows:
        raw = rows[_ROUTING_MODE_KEY]
        try:
            mode = json.loads(raw)
        except (ValueError, TypeError):
            # Older stores may carry the exact unquoted mode vocabulary.
            mode = raw
        if isinstance(mode, str) and mode in _LEGACY_ROUTING_MODES:
            mode = DEFAULT_ROUTING_MODE
        if not isinstance(mode, str) or mode not in VALID_ROUTING_MODES:
            raise LaunchStateUnavailable("launch_state_invalid")
    if _ROUTING_PAUSED_KEY in rows:
        try:
            paused = json.loads(rows[_ROUTING_PAUSED_KEY])
        except (ValueError, TypeError):
            raise LaunchStateUnavailable("launch_state_invalid") from None
        if type(paused) is not bool:
            raise LaunchStateUnavailable("launch_state_invalid")
    if _DECISION_MODE_KEY in rows:
        try:
            decision_mode = json.loads(rows[_DECISION_MODE_KEY])
        except (ValueError, TypeError):
            decision_mode = rows[_DECISION_MODE_KEY]
        if isinstance(decision_mode, str) and decision_mode in {"reasoning_primary", "reasoning_shadow"}:
            decision_mode = DEFAULT_DECISION_MODE
        if not isinstance(decision_mode, str) or decision_mode not in VALID_DECISION_MODES:
            raise LaunchStateUnavailable("launch_state_invalid")
    if _RESOURCE_STRATEGY_KEY in rows:
        try:
            strategy = json.loads(rows[_RESOURCE_STRATEGY_KEY])
        except (ValueError, TypeError):
            raise LaunchStateUnavailable("launch_state_invalid") from None
        if not isinstance(strategy, dict) or any(
            not isinstance(value, str) or value not in _VALID_RESOURCE_STRATEGIES
            for value in strategy.values()
        ):
            raise LaunchStateUnavailable("launch_state_invalid")
    return {
        "routing_mode": mode, "routing_paused": paused,
        "routing_paused_since": _setting_utc_stamp(rows.get('routing_paused_since')) if paused else None,
        "migration_required": False,
        "mode_is_set": _ROUTING_MODE_KEY in rows,
        "disconnected_hosts": disconnected_hosts,
    }


def get_launch_state(db_path: str | Path) -> dict[str, Any]:
    """Read both controls together. Only successfully absent rows use defaults.

    Compatibility display readers below must never authorize worker launch.
    Errors are fixed categories, without paths or database error bodies.
    """
    try:
        conn = _db.connect_and_init(db_path)
        try:
            return _launch_state_from_connection(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        raise LaunchStateUnavailable("launch_state_unreadable") from None


def get_job_runner_enabled(db_path: str | Path) -> bool:
    """Internal opt-in only. Missing, malformed or unreadable means OFF."""
    try:
        conn = _db.connect_and_init(db_path)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='job_runner_enabled'").fetchone()
            return row is not None and json.loads(row['value']) is True
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def _setting_utc_stamp(raw) -> str | None:
    """Optional presentation evidence must never authorize launch."""
    try:
        value = json.loads(raw)
        if type(value) is not str or len(value) > 64:
            return None
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def get_job_runner_setting(db_path: str | Path) -> dict:
    """Strict presentation reader: successfully absent is OFF, errors unknown."""
    try:
        conn = _db.connect_and_init(db_path)
        try:
            rows = dict(conn.execute("SELECT key,value FROM settings WHERE key IN (?,?)",
                                     ('job_runner_enabled', 'job_runner_changed_at')).fetchall())
            enabled = json.loads(rows['job_runner_enabled']) if 'job_runner_enabled' in rows else False
            if type(enabled) is not bool:
                raise ValueError
            return {'job_runner_enabled': enabled,
                    'job_runner_changed_at': _setting_utc_stamp(rows.get('job_runner_changed_at')),
                    'job_runner_setting_status': 'available'}
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {'job_runner_enabled': None, 'job_runner_changed_at': None,
                'job_runner_setting_status': 'unavailable'}


def record_job_runner_consent(db_path: str | Path) -> str:
    """Store when the user consented to background work at the CLI.

    Only ``modelquad settings on`` calls this, after the typed answer or
    ``--yes``. The service refuses to turn the job runner on without it.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute("INSERT INTO settings (key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ('job_runner_consent_at', json.dumps(stamp)))
        conn.commit()
    finally:
        conn.close()
    return stamp


def get_job_runner_consent_at(db_path: str | Path) -> str | None:
    """The recorded consent time, or None when none is recorded.

    Raises when the database cannot be read: an unreadable consent is not
    a recorded one, and the caller refuses.
    """
    conn = _db.connect_and_init(db_path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='job_runner_consent_at'").fetchone()
    finally:
        conn.close()
    return _setting_utc_stamp(row['value']) if row is not None else None


def put_job_runner_enabled(db_path: str | Path, enabled: bool) -> bool:
    """Atomically persist the explicit user choice and its UTC time."""
    if type(enabled) is not bool:
        raise ValueError('job_runner_enabled must be a boolean')
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.executemany("INSERT INTO settings (key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         [('job_runner_enabled', json.dumps(enabled)),
                          ('job_runner_changed_at', json.dumps(datetime.now(timezone.utc).isoformat()))])
        conn.commit()
    finally:
        conn.close()
    return enabled


def job_for_attempt(db_path: str | Path, attempt_id: int) -> dict | None:
    """The reservation is the attempt-ownership transfer marker."""
    conn = _db.connect_and_init(db_path)
    try:
        row = conn.execute('SELECT * FROM jobs WHERE attempt_id=?', (attempt_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def read_job_attempt(db_path: str | Path, attempt_id: int) -> dict | None:
    """Reread terminal evidence after a conditional write; never infer a race."""
    conn = _db.connect_and_init(db_path)
    try:
        row = conn.execute('SELECT * FROM delegation_attempts WHERE id=?', (attempt_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fail_job_attempt(db_path: str | Path, job_id: str, *, detail: str,
                     launch_limitation: str | None = None) -> bool:
    """Close a reserved pre-launch failure and its attempt in one transaction."""
    if launch_limitation not in (None, 'contained_innermost_job_without_kill_on_close'):
        raise ValueError('unrecognized job launch limitation')
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None or row['ended_at'] is not None:
            return False
        stamp = datetime.now(timezone.utc).isoformat()
        conn.create_function('resolved_attempt_detail', 2, _resolved_attempt_detail)
        conn.execute(
            "UPDATE delegation_attempts SET status='failed', failure_kind='failed_to_start', "
            "outcome='unknown', resolved_at=?, detail=resolved_attempt_detail(detail, ?) "
            "WHERE id=? AND status='started' AND resolved_at IS NULL",
            (stamp, detail, row['attempt_id']),
        )
        conn.execute("UPDATE jobs SET status='failed', ended_at=?, updated_at=?, "
                     "launch_limitation=COALESCE(launch_limitation, ?) WHERE job_id=?",
                     (stamp, stamp, launch_limitation, job_id))
        conn.commit()
        return True
    finally:
        conn.close()


def reserve_job(db_path: str | Path, job_id: str, attempt_id: int) -> dict[str, Any]:
    """Atomically reserve an existing open attempt; errors refuse all spawning."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("a nonempty job identity is required")
    if type(attempt_id) is not int or attempt_id <= 0:
        raise ValueError("an existing open attempt is required")
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute(
            "SELECT decision_id FROM delegation_attempts WHERE id=? "
            "AND status='started' AND resolved_at IS NULL", (attempt_id,),
        ).fetchone()
        if attempt is None:
            raise ValueError("an existing open attempt is required")
        from .. import procutil
        before_boot = procutil.observe_boot()
        now = datetime.now(timezone.utc).isoformat()
        after_boot = procutil.observe_boot()
        anchors = reservation_boot_anchors(before_boot, after_boot, reserved_at=now)
        conn.execute(
            "INSERT INTO jobs (job_id, attempt_id, decision_id, status, created_at, updated_at, "
            "reservation_boot_id, reservation_uptime_ms, reservation_boot_qualified) "
            "VALUES (?, ?, ?, 'starting', ?, ?, ?, ?, ?)",
            (job_id, attempt_id, attempt['decision_id'], now, now, *anchors),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def reservation_boot_anchors(before, after, *, reserved_at=None):
    """No legacy backfill. Inconsistent or hibernation observations stay NULL."""
    if (before is not None and after is not None and before.qualified is True
            and after.qualified is True and before.session == after.session
            and before.uptime_ms <= after.uptime_ms
            and before.observed_at <= after.observed_at
            and (reserved_at is None or before.observed_at <= datetime.fromisoformat(reserved_at) <= after.observed_at)):
        return before.session, before.uptime_ms, 1
    return None, None, None


def claim_job_launch(db_path: str | Path, job_id: str, *, attempt_id: int,
                     decision_id: str | None, starter_pid: int, starter_created: float) -> bool:
    """Transfer an unlaunched reservation to an exactly identified service."""
    import math
    if (type(starter_pid) is not int or starter_pid <= 0 or
            type(starter_created) not in (int, float) or
            not math.isfinite(starter_created) or starter_created <= 0):
        raise ValueError('starter identity unavailable')
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute(
            'UPDATE jobs SET runner_pid=?, runner_created=?, updated_at=? '
            "WHERE job_id=? AND attempt_id=? AND decision_id IS ? AND status='starting' "
            'AND runner_pid IS NULL AND heartbeat_at IS NULL AND ended_at IS NULL '
            'AND recovery_claim IS NULL',
            (starter_pid, starter_created, datetime.now(timezone.utc).isoformat(),
             job_id, attempt_id, decision_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def release_unlaunched_job(db_path: str | Path, job_id: str) -> bool:
    """Return ownership to the caller only when launch/recovery never claimed it."""
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute(
            "DELETE FROM jobs WHERE job_id=? AND status='starting' "
            'AND runner_pid IS NULL AND heartbeat_at IS NULL AND ended_at IS NULL '
            'AND recovery_claim IS NULL', (job_id,),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def read_job(db_path: str | Path, job_id: str) -> dict[str, Any] | None:
    """Read without creating a database or changing lifecycle on missing evidence."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def require_unreleased_job(db_path, job_id):
    """Refuse late runner entry/publication independently of attempt writers."""
    conn = _db.connect_and_init(db_path)
    try:
        if conn.execute('SELECT 1 FROM job_fate_releases WHERE job_id=?', (job_id,)).fetchone():
            raise ValueError('job fate released')
    finally:
        conn.close()


@contextmanager
def job_publication_fence(db_path, job_id):
    """Serialize late answer publication against administrative fate closure."""
    from . import fate_history

    with fate_history.publication_lock(db_path):
        require_unreleased_job(db_path, job_id)
        # The sink and competing runner may use other DB connections. Keep
        # lifecycle writes available; recovery's final CAS retains ownership.
        yield


@contextmanager
def job_spawn_fence(db_path, ref):
    """Serialize suspended runner spawn/identity recording against fate release."""
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE job_id=?', (ref.job_id,)).fetchone()
        if (row is None or row['ended_at'] is not None or row['recovery_claim'] is not None
                or row['attempt_id'] != ref.attempt_id or row['decision_id'] != ref.decision_id
                or conn.execute('SELECT 1 FROM job_fate_releases WHERE job_id=?',
                                (ref.job_id,)).fetchone()):
            raise ValueError('job not launchable')
        yield conn
        conn.commit()
    finally:
        conn.close()


def list_active_jobs(db_path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    """Read bounded, content-free open job handles without lifecycle writes."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(
            "SELECT job_id, status, created_at FROM jobs WHERE ended_at IS NULL "
            "ORDER BY created_at DESC, job_id DESC LIMIT ?", (max(0, min(limit, 50)),),
        )]
    finally:
        conn.close()


def pending_project_metadata(db_path, references, workspace_key, *, deadline,
                             clock=None):
    """Exact retained/job/attempt/decision join; no schema initialization.

    Scope is equality, never ancestor membership. Bounded indexed lookups and
    a VM instruction/time budget also bound missing-index or lock scenarios.
    Missing/malformed linkage is excluded; unavailable evidence raises.
    """
    import re
    import time

    clock = clock or time.monotonic
    if not isinstance(workspace_key, str) or not re.fullmatch('[0-9a-f]{32}', workspace_key):
        raise ValueError('pending_scope_invalid')
    references = tuple(references)
    if len(references) > 256 or clock() >= deadline:
        raise ValueError('pending_summary_unavailable')
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro',
                           uri=True, timeout=0)
    conn.row_factory = sqlite3.Row
    instructions = 0

    def interrupt():
        nonlocal instructions
        instructions += 1000
        return int(instructions > 200000 or clock() >= deadline)

    conn.set_progress_handler(interrupt, 1000)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        result = {}
        for ref in references:
            if clock() >= deadline:
                raise ValueError('pending_summary_unavailable')
            safe = lambda v: isinstance(v, str) and re.fullmatch('[A-Za-z0-9_-]{1,128}', v)
            if (not safe(ref.job_id) or type(ref.attempt_id) is not int
                    or not 0 < ref.attempt_id <= 9223372036854775807
                    or not safe(ref.decision_id)):
                continue
            rows = conn.execute(
                'SELECT j.status, a.task_type FROM jobs j '
                'JOIN delegation_attempts a ON a.id=j.attempt_id '
                'JOIN routing_decisions d ON d.decision_id=a.decision_id '
                'WHERE j.job_id=? AND j.attempt_id=? AND j.decision_id=? '
                'AND a.decision_id=j.decision_id AND a.workspace_key=? '
                'AND d.task_type=a.task_type LIMIT 2',
                (ref.job_id, ref.attempt_id, ref.decision_id, workspace_key),
            ).fetchall()
            if len(rows) != 1:
                continue
            row = rows[0]
            if row['status'] not in {'finished', 'failed', 'stopped', 'interrupted', 'timed_out', 'unknown'}:
                continue
            try:
                task = TaskType(row['task_type']).value
            except ValueError:
                continue
            result[ref.job_id] = {'job_status': row['status'], 'task_type': task}
        if clock() >= deadline:
            raise ValueError('pending_summary_unavailable')
        return result
    finally:
        conn.close()


def update_job(db_path: str | Path, job_id: str, **fields: Any) -> None:
    """Update only lifecycle facts; content and paths are never accepted."""
    allowed = {'runner_pid', 'runner_created', 'worker_pid', 'worker_created', 'status', 'heartbeat_at', 'ended_at',
               'launch_limitation'}
    if not fields or not fields.keys() <= allowed:
        raise ValueError("only job lifecycle facts may be written")
    if fields.get('launch_limitation') not in (None, 'contained_innermost_job_without_kill_on_close'):
        raise ValueError("unrecognized job launch limitation")
    for name in ('runner_pid', 'worker_pid'):
        value = fields.get(name)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("process identities must be positive integers")
    for name in ('runner_created', 'worker_created'):
        created = fields.get(name)
        if created is not None:
            import math
            if type(created) not in (int, float) or not math.isfinite(created) or created <= 0:
                raise ValueError("process creation time must be a finite epoch")
    for name in ('heartbeat_at', 'ended_at'):
        value = fields.get(name)
        if value is not None:
            if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() != timedelta(0):
                raise ValueError("job timestamps must be UTC instants")
    fields['updated_at'] = datetime.now(timezone.utc).isoformat()
    conn = _db.connect_and_init(db_path)
    try:
        cursor = conn.execute(
            "UPDATE jobs SET " + ', '.join(f'{key}=?' for key in fields) +
            " WHERE job_id=? AND ended_at IS NULL", (*fields.values(), job_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("job is missing or already terminal")
        conn.commit()
    finally:
        conn.close()



# Recovery snapshots include every runner-mutated identity/heartbeat field.
# A runner registering, spawning or completing after observation invalidates
# the recovery write, regardless of a recoverer's lease token.
_RECOVERY_SNAPSHOT = ('runner_pid', 'runner_created', 'worker_pid',
                      'worker_created', 'heartbeat_at', 'updated_at')


def recovery_candidates(db_path: str | Path) -> list[dict]:
    conn = _db.connect_and_init(db_path)
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM jobs WHERE ended_at IS NULL "
            "AND (recovery_decision IS NULL OR recovery_decision != 'unknown') "
            "AND (status IN ('starting','running','unknown') OR recovery_decision IS NOT NULL) "
            "ORDER BY created_at, job_id"
        )]
    finally:
        conn.close()


def claim_job_recovery(db_path, row, *, token, now, lease_seconds) -> dict | None:
    """One atomic claim; caller must have revalidated the runner as dead.

    Both the prior claim and heartbeat must be old before lease takeover.
    No write updates the heartbeat or pretends that recovery is a runner.
    """
    conn = _db.connect_and_init(db_path)
    try:
        cutoff = (now - timedelta(seconds=lease_seconds)).isoformat()
        where = ' AND '.join(f'{key} IS ?' for key in _RECOVERY_SNAPSHOT)
        cursor = conn.execute(
            "UPDATE jobs SET recovery_claim=?, recovery_claimed_at=? "
            "WHERE job_id=? AND ended_at IS NULL AND " + where +
            " AND (recovery_claim IS NULL OR "
            "(julianday(recovery_claimed_at)<julianday(?) AND "
            "julianday(COALESCE(heartbeat_at, created_at))<julianday(?)))",
            (token, now.isoformat(), row['job_id'],
             *(row[key] for key in _RECOVERY_SNAPSHOT), cutoff, cutoff),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return None
        return dict(conn.execute('SELECT * FROM jobs WHERE job_id=?', (row['job_id'],)).fetchone())
    finally:
        conn.close()


def write_job_recovery(db_path, row, *, token, now, unknown=False, release=False, containment_lost=False, ended=None, decision=None, status=None) -> dict | None:
    """Fenced unknown observation or write-once reconciliation timestamp.

    A persisted decision is immutable and precedes every output store write.
    Its status and timestamp survive acknowledgement and lease takeover.
    Unknown never closes the job or changes the attempt.
    A containment-loss observation keeps its claim for the exact root stop;
    unknown plus a reconciliation timestamp preserves that uncertainty on retry.
    """
    if decision is not None and decision not in {
            'restore_pending', 'map_resolved', 'interrupted', 'startup_failed', 'unknown'}:
        raise ValueError('unrecognized recovery decision')
    conn = _db.connect_and_init(db_path)
    try:
        where = ' AND '.join(f'{key} IS ?' for key in _RECOVERY_SNAPSHOT)
        fields = ("status='unknown', recovery_ended_at=COALESCE(recovery_ended_at, ?)"
                  if containment_lost else
                  "status='unknown', recovery_claim=NULL, recovery_claimed_at=NULL"
                  if unknown else "recovery_claim=NULL, recovery_claimed_at=NULL" if release else
                  "recovery_ended_at=COALESCE(recovery_ended_at, ?)")
        params = () if unknown or release else ((ended or now).isoformat(),)
        if decision is not None:
            fields = "recovery_decision=?, status=?, recovery_ended_at=COALESCE(recovery_ended_at, ?)"
            params = (decision, status, (ended or now).isoformat())
        cursor = conn.execute(
            'UPDATE jobs SET ' + fields +
            ' WHERE job_id=? AND recovery_claim=? AND ended_at IS NULL AND ' + where +
            (' AND recovery_decision IS NULL' if decision is not None else ''),
            (*params, row['job_id'], token, *(row[key] for key in _RECOVERY_SNAPSHOT)),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return None
        return dict(conn.execute('SELECT * FROM jobs WHERE job_id=?', (row['job_id'],)).fetchone())
    finally:
        conn.close()


def recovery_claim_current(db_path, row, *, token) -> bool:
    current = read_job(db_path, row['job_id'])
    return bool(current and current['ended_at'] is None and current['recovery_claim'] == token
                and all(current[key] == row[key] for key in _RECOVERY_SNAPSHOT))


def _plain_recovery_facts(value):
    """Thaw retained JSON facts before using the dict/list-only classifiers."""
    if isinstance(value, Mapping):
        return {key: _plain_recovery_facts(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_recovery_facts(item) for item in value]
    return value


def finalize_job_recovery(db_path, row, *, token, status, now, startup=False,
                          evidence=None) -> bool:
    """Finalize the job, then conditionally resolve its attempt atomically.

    The claim and open-job fence is checked inside the write transaction.
    SQLite holds that fence until commit, including after the job is ended.
    Output publication belongs to the caller and precedes this transaction.
    Existing terminal attempts are never rewritten.
    Only a retained answer from this restore_pending decision supplies evidence.
    A lease takeover supplies none: the saved job status is not proof of death.
    """
    resolutions = {
        'finished': ('completed', None, 'recovered_after_runner_loss'),
        'failed': ('failed', 'exception', 'recovered_after_runner_loss'),
        'stopped': ('failed', 'timeout', 'stop_requested; recovered_after_runner_loss'),
        'interrupted': ('failed', 'orphaned', 'recovered_after_runner_loss'),
        'timed_out': ('failed', 'timeout', 'recovered_after_runner_loss'),
    }
    attempt_status, kind, detail = resolutions[status]
    if startup and status == 'failed':
        kind, detail = 'failed_to_start', 'runner_startup_deadline_exceeded'
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        where = ' AND '.join(f'{key} IS ?' for key in _RECOVERY_SNAPSHOT)
        params = (row['job_id'], token, *(row[key] for key in _RECOVERY_SNAPSHOT))
        eligible = conn.execute(
            'SELECT * FROM jobs WHERE job_id=? AND recovery_claim=? '
            'AND ended_at IS NULL AND ' + where, params,
        ).fetchone()
        if (eligible is None or eligible['recovery_ended_at'] is None
                or eligible['recovery_decision'] in (None, 'unknown')
                or eligible['status'] != status
                or startup != (eligible['recovery_decision'] == 'startup_failed')):
            conn.rollback()
            return False
        attempt = conn.execute('SELECT * FROM delegation_attempts WHERE id=?',
                               (eligible['attempt_id'],)).fetchone()
        if attempt is None:
            conn.rollback()
            return False
        descendants_json = None
        confirmed = False
        if evidence is not None:
            if (eligible['recovery_decision'] != 'restore_pending'
                    or evidence.job.job_id != eligible['job_id']
                    or evidence.job.attempt_id != eligible['attempt_id']
                    or evidence.job_status.value != status):
                raise ValueError('recovery evidence does not match the retained job')
            termination = _plain_recovery_facts(evidence.termination)
            descendants = _plain_recovery_facts(evidence.descendants)
            confirmed = (evidence.tree_confirmed_dead is True
                         and worker_confirmed_dead({'worker_termination': termination})
                         and (descendants is None or (
                             descendants.get('enumerated') is True
                             and descendants.get('truncated') is False
                             and descendants.get('descendants') == [])))
            if descendants is not None:
                descendants_json = json.dumps(_bounded_descendants(descendants), sort_keys=True)
        if status in {'stopped', 'timed_out'}:
            kind = 'terminated' if confirmed else 'timeout'
            detail += '; worker_tree_death_confirmed' if confirmed else '; worker_tree_death_unconfirmed'
        cursor = conn.execute(
            'UPDATE jobs SET status=?, ended_at=recovery_ended_at, updated_at=? '
            'WHERE job_id=? AND recovery_claim=? AND ended_at IS NULL AND ' + where,
            (status, now.isoformat(), *params),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.create_function('resolved_attempt_detail', 2, _resolved_attempt_detail)
        conn.execute(
            'UPDATE delegation_attempts SET status=?, failure_kind=?, '
            "outcome='unknown', resolved_at=?, detail=resolved_attempt_detail(detail, ?), "
            "worker_descendants=COALESCE(?, worker_descendants) "
            "WHERE id=? AND status='started' AND resolved_at IS NULL",
            (attempt_status, kind, eligible['recovery_ended_at'], detail,
             descendants_json, eligible['attempt_id']),
        )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_job_startup(
    db_path: str | Path, job_id: str, *, startup_seconds: float,
    runner_pid: int | None = None, runner_created: float | None = None,
    launch_limitation: str | None = None,
) -> bool:
    """Atomically register the runner, or fail an overdue unreported startup.

    The first heartbeat distinguishes runner-owned identity from the starter's
    identity. Registration and deadline observation share the same write lock:
    a late runner cannot launch a worker after its startup was failed.
    Returns True on registration or expiry, False when nothing changed.
    """
    import math
    if not math.isfinite(startup_seconds) or startup_seconds <= 0:
        raise ValueError('startup deadline must be finite and positive')
    registering = runner_pid is not None
    if launch_limitation not in (None, 'contained_innermost_job_without_kill_on_close'):
        raise ValueError('unrecognized job launch limitation')
    if launch_limitation is not None and not registering:
        raise ValueError('limitation requires runner startup')
    if registering and (type(runner_pid) is not int or runner_pid <= 0 or
                        type(runner_created) not in (int, float) or
                        not math.isfinite(runner_created) or runner_created <= 0):
        raise ValueError('runner identity unavailable')
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None or row['ended_at'] is not None or row['heartbeat_at'] is not None:
            return False
        now = datetime.now(timezone.utc)
        if (now - datetime.fromisoformat(row['created_at'])).total_seconds() >= startup_seconds:
            stamp = now.isoformat()
            conn.execute("UPDATE jobs SET status='failed', ended_at=?, updated_at=? WHERE job_id=?",
                         (stamp, stamp, job_id))
            conn.create_function('resolved_attempt_detail', 2, _resolved_attempt_detail)
            conn.execute(
                "UPDATE delegation_attempts SET status='failed', failure_kind='failed_to_start', "
                "outcome='unknown', resolved_at=?, detail=resolved_attempt_detail(detail, ?) "
                "WHERE id=? AND status='started' AND resolved_at IS NULL",
                (stamp, 'runner_startup_deadline_exceeded', row['attempt_id']),
            )
            conn.commit()
            return not registering
        if not registering:
            return False
        attempt = conn.execute('SELECT decision_id, status, resolved_at FROM delegation_attempts WHERE id=?',
                               (row['attempt_id'],)).fetchone()
        if (row['recovery_claim'] is not None or attempt is None or
                attempt['decision_id'] != row['decision_id'] or attempt['status'] != 'started' or
                attempt['resolved_at'] is not None or
                conn.execute('SELECT 1 FROM job_fate_releases WHERE job_id=?', (job_id,)).fetchone()):
            return False
        conn.execute(
            'UPDATE jobs SET runner_pid=?, runner_created=?, heartbeat_at=?, updated_at=?, '
            'launch_limitation=COALESCE(launch_limitation, ?) WHERE job_id=?',
            (runner_pid, runner_created, now.isoformat(), now.isoformat(), launch_limitation, job_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def require_launch_attempt(db_path: str | Path, attempt_id: int | None) -> None:
    """Verify the lifecycle claim still exists before spending provider quota."""
    if attempt_id is None:
        raise LaunchStateUnavailable("attempt_unrecorded")
    try:
        conn = _db.connect_and_init(db_path)
        try:
            row = conn.execute(
                "SELECT status, resolved_at FROM delegation_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None or row["resolved_at"] is not None or row["status"] != "started":
                raise LaunchStateUnavailable("attempt_not_active")
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        raise LaunchStateUnavailable("attempt_unreadable") from None


def get_routing_mode(db_path: str | Path) -> str:
    """Return effective automatic routing, including retired saved values.

    Missing, invalid, or unreadable values fall back to DEFAULT_ROUTING_MODE.
    """
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return DEFAULT_ROUTING_MODE
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_ROUTING_MODE_KEY,)
        ).fetchone()
        if row is None:
            return DEFAULT_ROUTING_MODE
        try:
            decoded = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError, ValueError):
            decoded = row["value"]
        if isinstance(decoded, str) and decoded in _LEGACY_ROUTING_MODES:
            return DEFAULT_ROUTING_MODE
        if isinstance(decoded, str) and decoded in VALID_ROUTING_MODES:
            return decoded
        return DEFAULT_ROUTING_MODE
    except (sqlite3.Error, OSError):
        return DEFAULT_ROUTING_MODE
    finally:
        conn.close()


def routing_mode_if_set(db_path: str | Path) -> str | None:
    """Return None only for successfully absent state, never unreadable controls.

    Retired saved modes read as automatic without rewriting their rows.
    Corrupt or unreadable controls still refuse launch and onboarding.
    """
    state = get_launch_state(db_path)
    return state["routing_mode"] if state["mode_is_set"] else None


def put_routing_mode(db_path: str | Path, mode: str) -> str:
    """Persist automatic routing; retired modes are accepted only when reading."""
    if mode not in VALID_ROUTING_MODES:
        raise ValueError(
            "Routing mode must be automatic."
        )
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_ROUTING_MODE_KEY, json.dumps(mode)),
        )
        conn.commit()
    finally:
        conn.close()
    return mode


def get_routing_paused(db_path: str | Path) -> bool:
    """Return persisted global routing pause state.

    Pausing blocks all new provider work. It does not stop existing workers,
    disable providers, change routing_mode, or affect usage collection. Resuming
    restores the previously configured routing_mode untouched (pause is an
    orthogonal boolean, not a routing_mode value).

    Missing, invalid, or unreadable values fall back to DEFAULT_ROUTING_PAUSED (False).
    The reading lives in ``db`` so the light hook-gate entry shares it.
    """
    return _db.read_routing_paused(db_path)


def put_routing_paused(db_path: str | Path, paused: bool) -> None:
    """Persist global routing pause state as a JSON-encoded boolean.

    Pausing blocks all new provider work. It does not stop existing workers,
    disable providers, change routing_mode, or affect usage collection. Resuming
    restores the previously configured routing_mode untouched (pause is an
    orthogonal boolean, not a routing_mode value).
    """
    if not isinstance(paused, bool):
        raise ValueError(f"Invalid routing_paused value {paused!r}; must be a boolean")
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _write_pause_transition(conn, paused)
        conn.commit()
    finally:
        conn.close()


def _write_pause_transition(conn, paused: bool) -> None:
    rows = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?)',
                             (_ROUTING_PAUSED_KEY, 'routing_paused_since')).fetchall())
    try:
        previous = json.loads(rows.get(_ROUTING_PAUSED_KEY, 'false'))
    except (ValueError, TypeError):
        previous = None
    # A historical pause with no readable time stays unknown on repeated writes.
    stamp = (_setting_utc_stamp(rows.get('routing_paused_since')) if previous is True
             else datetime.now(timezone.utc).isoformat()) if paused else None
    conn.executemany('INSERT INTO settings (key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     [(_ROUTING_PAUSED_KEY, json.dumps(paused)),
                      ('routing_paused_since', json.dumps(stamp))])


def get_decision_mode(db_path: str | Path) -> str:
    """Return deterministic execution mode, including for saved legacy modes.

    Stored values remain untouched for historical compatibility. Missing,
    invalid or unreadable settings also use DEFAULT_DECISION_MODE.
    """

    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return DEFAULT_DECISION_MODE
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_DECISION_MODE_KEY,)
        ).fetchone()
        if row is None:
            return DEFAULT_DECISION_MODE
        try:
            decoded = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError, ValueError):
            decoded = row["value"]
        if isinstance(decoded, str) and decoded in VALID_DECISION_MODES:
            return DEFAULT_DECISION_MODE
        return DEFAULT_DECISION_MODE
    except (sqlite3.Error, OSError):
        return DEFAULT_DECISION_MODE
    finally:
        conn.close()


def put_decision_mode(db_path: str | Path, mode: str) -> str:
    """Persist a new decision mode; rejects anything outside VALID_DECISION_MODES."""

    if mode not in VALID_DECISION_MODES:
        raise ValueError(
            f"Invalid decision mode {mode!r}; must be one of {VALID_DECISION_MODES}"
        )
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_DECISION_MODE_KEY, json.dumps(mode)),
        )
        conn.commit()
    finally:
        conn.close()
    return mode


def record_plan_decision(
    db_path: str | Path,
    *,
    task_type: TaskType | str,
    decision_mode: str,
    planner_provider: str | None = None,
    planner_model: str | None = None,
    planner_model_source: str | None = None,
    reasoning_source: str | None = None,
    strategy: str | None = None,
    confidence: float | None = None,
    steps: list[dict[str, Any]] | None = None,
    reason_summary: str | None = None,
    validation_passed: bool | None = None,
    validation_errors: list[str] | None = None,
    executed: bool = False,
    fallback_occurred: bool = False,
    fallback_reason: str | None = None,
    deterministic_recommendation: str | None = None,
    agreement: bool | None = None,
    final_status: str | None = None,
    duration_seconds: float | None = None,
    replan_used: bool = False,
    packet: dict[str, Any] | None = None,
    source: str = "mcp",
    host: str | None = None,
    invocation_mode: str | None = None,
    decision_id: str | None = None,
    reasoning_attempted: bool | None = None,
    reasoning_status: str | None = None,
    reasoning_started_at: str | None = None,
    reasoning_completed_at: str | None = None,
    engine_versions: dict[str, Any] | None = None,
) -> int | None:
    """Best-effort persistence of one reasoning-plan decision.

    Returns the inserted row id (or None when persistence failed), so the
    caller can link the routing_decisions row for the same decision.

    ``steps`` contains step dictionaries (roles, providers, models, efforts,
    objectives) and ``packet`` the DecisionPacket
    history view -- callers are responsible for passing
    ``DecisionPacket.to_history_dict()``, which strips task text.

    Planner-authored free text (step objectives/success criteria, the reason
    summary, fallback reasons) is plan metadata the transparency UI needs,
    but it is BOUNDED here -- this is the single persistence choke point --
    so a verbose planner can never smuggle unbounded task or workspace prose
    into history. Step objectives/success criteria and the reason summary
    are also path-redacted after their existing caps, as is execution free
    text (including task_status_reason). Identity fields and
    packet evidence are unchanged; fallback reasons and validation errors
    remain outside this redaction boundary.
    """

    def _bounded(text: Any, limit: int) -> Any:
        if isinstance(text, str) and len(text) > limit:
            return text[:limit] + " ..."
        return text

    if steps is not None:
        sanitized_steps = []
        for step in steps:
            if isinstance(step, dict):
                step = {
                    **step,
                    "objective": _privacy.sanitize_persisted_text(
                        _bounded(step.get("objective"), 500)
                    ),
                    "success_criteria": _privacy.sanitize_persisted_text(
                        _bounded(step.get("success_criteria"), 500)
                    ),
                }
                execution = step.get("execution")
                if isinstance(execution, dict):
                    # The service adds status/replanned provider identities and
                    # worker prose here. Preserve identities; sanitize prose
                    # before serializing, without mutating the caller's dict.
                    identity_fields = {
                        "task_status", "replanned_provider", "provider",
                        "status", "outcome", "model", "agent_runtime",
                        "resource_pool", "role", "task_type", "reasoning_effort",
                    }
                    step["execution"] = {
                        key: value if key in identity_fields
                        else _privacy.sanitize_persisted_text(value)
                        for key, value in execution.items()
                    }
            sanitized_steps.append(step)
        steps = sanitized_steps
    reason_summary = _privacy.sanitize_persisted_text(_bounded(reason_summary, 1000))
    fallback_reason = _bounded(fallback_reason, 500)

    # A fallback without a reason would be exactly the silent fallback the
    # provenance model exists to prevent. Callers are expected to always
    # supply one; when a code path fails to, the omission itself is recorded
    # as the reason so diagnostics can surface it as a bug instead of the
    # row masquerading as a reasoned decision.
    if fallback_occurred and not fallback_reason:
        fallback_reason = "unrecorded_fallback_reason: caller recorded a fallback without a reason"

    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        cursor = conn.execute(
            "INSERT INTO plan_decisions "
            "(created_at, task_type, decision_mode, planner_provider, planner_model, "
            "planner_model_source, reasoning_source, strategy, confidence, steps_json, "
            "reason_summary, validation_passed, validation_errors_json, executed, "
            "fallback_occurred, fallback_reason, deterministic_recommendation, agreement, "
            "final_status, duration_seconds, replan_used, packet_json, source, host, "
            "invocation_mode, decision_id, reasoning_attempted, reasoning_status, "
            "reasoning_started_at, reasoning_completed_at, engine_versions_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                _task_type_value(task_type),
                decision_mode,
                planner_provider,
                planner_model,
                planner_model_source,
                reasoning_source,
                strategy,
                confidence,
                json.dumps(steps) if steps is not None else None,
                reason_summary,
                None if validation_passed is None else (1 if validation_passed else 0),
                json.dumps(validation_errors) if validation_errors is not None else None,
                1 if executed else 0,
                1 if fallback_occurred else 0,
                fallback_reason,
                deterministic_recommendation,
                None if agreement is None else (1 if agreement else 0),
                final_status,
                duration_seconds,
                1 if replan_used else 0,
                json.dumps(packet) if packet is not None else None,
                source,
                host,
                invocation_mode,
                decision_id,
                None if reasoning_attempted is None else (1 if reasoning_attempted else 0),
                reasoning_status,
                reasoning_started_at,
                reasoning_completed_at,
                json.dumps(engine_versions) if engine_versions is not None else None,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()


def _parse_plan_row(row: Any) -> dict[str, Any]:
    entry = dict(row)
    for json_field in ("steps_json", "validation_errors_json", "packet_json", "engine_versions_json"):
        raw = entry.pop(json_field, None)
        key = json_field.removesuffix("_json")
        if raw:
            try:
                entry[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                entry[key] = None
        else:
            entry[key] = None
    for bool_field in ("validation_passed", "agreement", "reasoning_attempted"):
        if entry.get(bool_field) is not None:
            entry[bool_field] = bool(entry[bool_field])
    for bool_field in ("executed", "fallback_occurred", "replan_used"):
        entry[bool_field] = bool(entry.get(bool_field))
    return entry


def fetch_plan_decisions(
    db_path: str | Path, *, limit: int = 50, decision_mode: str | None = None
) -> list[dict[str, Any]]:
    """Return recent plan decisions, newest first."""

    conn = _db.connect_and_init(db_path)
    try:
        query = "SELECT * FROM plan_decisions"
        params: list[Any] = []
        if decision_mode is not None:
            query += " WHERE decision_mode = ?"
            params.append(decision_mode)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(query, params).fetchall()
        return [_parse_plan_row(row) for row in rows]
    finally:
        conn.close()


def fetch_plan_decision(db_path: str | Path, entry_id: int) -> dict[str, Any] | None:
    """Return one plan decision by id (parsed like fetch_plan_decisions), or None."""

    conn = _db.connect_and_init(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM plan_decisions WHERE id = ?", (entry_id,)
        ).fetchone()
        return _parse_plan_row(row) if row is not None else None
    finally:
        conn.close()


# Host client names (MCP clientInfo) mapped to the provider whose capacity a
# session in that host would otherwise consume. Used only for the
# workload-shifted metric; an unrecognized host is honestly excluded rather
# than guessed.
_HOST_PROVIDER_HINTS: tuple[tuple[str, str], ...] = (
    ("claude", "claude"),
    ("codex", "codex"),
)


def provider_for_host(host: str | None) -> str | None:
    """Map an MCP host client name to the provider it consumes, or None.

    A hint used for host-context evidence and the workload-shifted metric --
    an unrecognized host maps to None (honestly unclassifiable), never to a
    guess."""

    if not host:
        return None
    lowered = host.lower()
    for needle, provider in _HOST_PROVIDER_HINTS:
        if needle in lowered:
            return provider
    return None


# Host client names mapped to the AGENT RUNTIME the session runs in (V6:
# host ≠ provider ≠ runtime). A name such as "claude desktop" (not a
# supported host) consumes Claude capacity but is not the Claude Code
# runtime, so it maps to no runtime rather than the wrong one. Runtime ids
# equal provider ids for the current integrations -- an integration fact
# encoded in the registry's derived descriptors, not here.
_HOST_RUNTIME_HINTS: tuple[tuple[str, str], ...] = (
    ("claude desktop", ""),
    ("claude-desktop", ""),
    ("claude_desktop", ""),
    ("claude", "claude"),
    ("codex", "codex"),
)


def runtime_for_host(host: str | None) -> str | None:
    """Map an MCP host client name to the agent runtime it runs in, or None.

    An unrecognized host maps to None."""

    if not host:
        return None
    lowered = host.lower()
    for needle, runtime in _HOST_RUNTIME_HINTS:
        if needle in lowered:
            return runtime or None
    return None


def fetch_capacity_metrics(db_path: str | Path, *, since_hours: float = 168) -> dict[str, Any]:
    """Objective capacity-allocation metrics for the transparency UI.

    Every number here is defensible from recorded history alone. There is
    deliberately NO "tokens saved" figure: the product has no per-task token
    accounting, so claiming one would be invented. Metrics that need evidence
    the product does not collect (per-execution capacity estimates, reset
    timing at execution time) are reported as None with an explicit reason
    instead of a fabricated value.
    """

    conn = _db.connect_and_init(db_path)
    try:
        cutoff = _window_cutoff(since_hours)
        rows = conn.execute(
            "SELECT provider, host, task_status, fallback FROM execution_history "
            "WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        total = len(rows)
        by_provider: dict[str, int] = {}
        shifted = 0
        shift_classifiable = 0
        completed = 0
        fallbacks = 0
        for row in rows:
            provider = row["provider"]
            by_provider[provider] = by_provider.get(provider, 0) + 1
            if row["task_status"] == WorkerTaskStatus.COMPLETED.value:
                completed += 1
            if row["fallback"]:
                fallbacks += 1
            host_provider = provider_for_host(row["host"])
            if host_provider is not None:
                shift_classifiable += 1
                if provider != host_provider:
                    shifted += 1

        plan_rows = conn.execute(
            "SELECT decision_mode, fallback_occurred, agreement, planner_provider, "
            "validation_passed, executed, steps_json FROM plan_decisions "
            "WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        plan_total = len(plan_rows)
        plan_fallbacks = sum(1 for row in plan_rows if row["fallback_occurred"])
        shadow_rows = [
            row for row in plan_rows
            if row["decision_mode"] == "reasoning_shadow" and row["agreement"] is not None
        ]
        shadow_agreements = sum(1 for row in shadow_rows if row["agreement"])

        # Planner usage: which provider did the THINKING (distinct from who
        # executed). Only decisions where a planner is actually recorded.
        planner_usage: dict[str, int] = {}
        for row in plan_rows:
            if row["planner_provider"]:
                planner_usage[row["planner_provider"]] = (
                    planner_usage.get(row["planner_provider"], 0) + 1
                )

        # Validity rate among decisions where the validator actually ran.
        validated = [row for row in plan_rows if row["validation_passed"] is not None]
        valid_count = sum(1 for row in validated if row["validation_passed"])

        # Execution stages by provider -- counting only stages that actually
        # RAN (each carries a per-stage execution annotation), attributed to
        # the provider that really performed them when a stage was
        # deterministically re-routed. Planned-but-never-run stages are not
        # execution and are not counted (review finding M5).
        stage_distribution: dict[str, int] = {}
        for row in plan_rows:
            if not row["executed"] or not row["steps_json"]:
                continue
            try:
                steps = json.loads(row["steps_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            for step in steps if isinstance(steps, list) else []:
                if not isinstance(step, dict):
                    continue
                execution = step.get("execution")
                if not isinstance(execution, dict):
                    continue
                provider = (
                    execution.get("replanned_provider")
                    if execution.get("replanned")
                    else step.get("provider")
                )
                if provider:
                    stage_distribution[provider] = stage_distribution.get(provider, 0) + 1

        return {
            "window_hours": since_hours,
            "executions_total": total,
            "capacity_distribution": dict(sorted(by_provider.items())),
            "workload_shifted": {
                "tasks": shifted,
                "classifiable_tasks": shift_classifiable,
                "basis": (
                    "executions whose recorded MCP host maps to a provider and "
                    "whose executing provider differs from it; executions with "
                    "an unrecognized host are excluded, not guessed"
                ),
                "estimated": True,
            },
            "completion_rate": completed / total if total else None,
            "completion_rate_basis": (
                "confirmed COMPLETED / all recorded executions; unknown "
                "outcomes count in the denominator, so this understates "
                "rather than inflates"
            ),
            "fallback_rate": fallbacks / total if total else None,
            "plan_decisions_total": plan_total,
            "plan_fallback_rate": plan_fallbacks / plan_total if plan_total else None,
            "shadow_agreement_rate": (
                shadow_agreements / len(shadow_rows) if shadow_rows else None
            ),
            "planner_usage": dict(sorted(planner_usage.items())),
            "plan_validity_rate": (
                valid_count / len(validated) if validated else None
            ),
            "plan_stage_distribution": dict(sorted(stage_distribution.items())),
            "capacity_preserved": {
                "value": None,
                "reason": (
                    "no defensible per-task capacity estimate exists; ModelQuad "
                    "does not invent token or percentage savings"
                ),
            },
            "expiring_capacity_utilized": {
                "value": None,
                "reason": (
                    "reset timing is not recorded alongside executions; this "
                    "metric activates only when reset information is "
                    "objectively known"
                ),
            },
        }
    finally:
        conn.close()


def fetch_offload_summary(
    db_path: str | Path, since_hours: float = 168.0
) -> dict[str, Any]:
    """Honest task-count offload metrics for the transparency UI and summary API.

    Every number here is computed solely from recorded execution history.
    Only semantically completed executions (task_status == 'completed') with a
    classifiable originating host count toward offload calculations.
    Unclassifiable hosts, failed, blocked, partial, and unknown outcomes are
    excluded. Fallback executions that completed count normally under the
    provider that actually finished the work.
    """
    conn = _db.connect_and_init(db_path)
    try:
        cutoff = _window_cutoff(since_hours)
        rows = conn.execute(
            "SELECT provider, host, task_status, fallback FROM execution_history "
            "WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()

        executions_total = len(rows)
        completed_total = 0
        classifiable_completed = 0
        offloaded_completed = 0
        distribution_counts: dict[str, int] = {}

        for row in rows:
            provider = row["provider"]
            status = row["task_status"]
            if status == WorkerTaskStatus.COMPLETED.value or status == "completed":
                completed_total += 1
                distribution_counts[provider] = distribution_counts.get(provider, 0) + 1
                origin = provider_for_host(row["host"])
                if origin is not None:
                    classifiable_completed += 1
                    if provider != origin:
                        offloaded_completed += 1

        offload_rate = (
            (offloaded_completed / classifiable_completed)
            if classifiable_completed > 0
            else None
        )
        sorted_distribution = {
            p: distribution_counts[p] for p in sorted(distribution_counts)
        }

        return {
            "executions_total": executions_total,
            "completed_total": completed_total,
            "classifiable_completed": classifiable_completed,
            "offloaded_completed": offloaded_completed,
            "offload_rate": offload_rate,
            "distribution": sorted_distribution,
            "estimated": True,
            "basis": (
                "offloaded_completed / classifiable_completed; only semantically "
                "completed executions with a classifiable originating host count; "
                "failed, blocked, partial and unknown outcomes are excluded"
            ),
            "window_hours": since_hours,
        }
    finally:
        conn.close()


def get_plan_advice_enabled(db_path: str | Path) -> bool:
    """Return persisted plan advice (default False).

    Missing, invalid, or unreadable values fall back to False.
    """
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return False
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_PLAN_ADVICE_ENABLED_KEY,)
        ).fetchone()
        if row is None:
            return False
        try:
            decoded = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError, ValueError):
            decoded = row["value"]
        if isinstance(decoded, bool):
            return decoded
        return False
    except (sqlite3.Error, OSError):
        return False
    finally:
        conn.close()


def put_plan_advice_enabled(db_path: str | Path, enabled: bool) -> bool:
    """Persist plan advice setting."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    conn = _db.connect_and_init(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PLAN_ADVICE_ENABLED_KEY, json.dumps(enabled)),
        )
        conn.commit()
    finally:
        conn.close()
    return enabled


def _resolve_settings_payload(payload: dict[str, Any]) -> dict[str, Any]:
    preset_name = payload.get("preset")
    if preset_name is not None:
        if preset_name not in PRESETS:
            raise ValueError(f"Unknown preset {preset_name!r}; choose one of: {', '.join(PRESET_NAMES)}")
        base = PRESETS[preset_name]
        return {
            "preset": preset_name,
            "fit_weight": base["fit_weight"],
            "usage_weight": base["usage_weight"],
            "quota_penalty": dict(base["quota_penalty"]),
            "freshness": dict(_DEFAULT_FRESHNESS),
        }

    required = ("fit_weight", "usage_weight", "quota_penalty")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"Settings payload missing required fields: {', '.join(missing)}")
    quota_penalty = payload["quota_penalty"]
    if not isinstance(quota_penalty, dict):
        raise ValueError("quota_penalty must be an object")
    freshness = payload.get("freshness") or dict(_DEFAULT_FRESHNESS)
    if not isinstance(freshness, dict):
        raise ValueError("freshness must be an object")
    return {
        "preset": "custom",
        "fit_weight": payload["fit_weight"],
        "usage_weight": payload["usage_weight"],
        "quota_penalty": dict(quota_penalty),
        "freshness": dict(freshness),
    }


def put_settings(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate, persist, and return a new routing settings document.

    Raises ``ValueError`` (never persisting anything) on invalid input, by
    reusing ``Config``'s own validation rather than re-implementing it.
    """

    resolved = _resolve_settings_payload(payload)
    Config(freshness=FreshnessThresholds(**resolved["freshness"]))

    conn = _db.connect_and_init(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_SETTINGS_KEY, json.dumps(resolved)),
        )
        conn.commit()
    finally:
        conn.close()
    return resolved


def settings_to_config(settings: dict[str, Any], base: Config | None = None) -> Config:
    """Build a full routing ``Config`` from a persisted settings document.

    Only freshness is overridden (routing weights and penalties are inert);
    the task-fit matrix keeps its packaged default (not exposed in user settings).
    """

    return Config(
        freshness=FreshnessThresholds(**settings["freshness"]),
        task_fit=base.task_fit if base is not None else None,
        sessions_path=base.sessions_path if base is not None else Config().sessions_path,
        providers=dict(base.providers) if base is not None else {},
    )


# ---------------------------------------------------------------------------
# Delayed outcome evidence -- append-only revisions
# ---------------------------------------------------------------------------

# Why the immediate outcome may later need revising. Closed vocabulary so
# analytics never meet a free-text reason; extend deliberately, not ad hoc.
OUTCOME_REVISION_REASONS: tuple[str, ...] = (
    "rerun_same_task",        # user immediately re-ran the same logical task
    "user_switched_agent",    # user manually re-routed to a different agent
    "fallback_completed",     # a fallback agent later completed the task
    "repair_by_other_agent",  # another agent repaired the first result
    "corrective_task",        # a follow-up fix task was required
    "reverted",               # the produced changes were reverted
    "later_validation_failed",  # tests/build that passed later failed
    "user_verdict",           # the user explicitly accepted/rejected the outcome
    "manual_override",        # the route was manually overridden afterwards
)

# Typed review events belong exclusively to second_opinion_history's closed,
# size-validated transactional writer. The general text writer refuses them.
REVIEW_OUTCOME_REVISION_REASONS: tuple[str, ...] = (
    "review_requested",
    "review_replaces",
    "review_catalog",
    "review_finding",
    "review_disposition",
)

# Who/what produced the delayed evidence -- same provenance vocabulary as
# EvidenceItem sources, plus unknown. A revision is not automatically more
# objective than the original; its source says what it is.
OUTCOME_REVISION_SOURCES: tuple[str, ...] = (
    "objective",
    "independent",
    "inferred",
    "user_reported",
    "worker_self_reported",
    "host_reported",
    "unknown",
)

_REVISION_DETAIL_MAX_CHARS = 500

_VALID_REVISION_STATUSES = tuple(status.value for status in WorkerTaskStatus)


def record_outcome_revision(
    db_path: str | Path,
    *,
    revision_reason: str,
    decision_id: str | None = None,
    execution_id: int | None = None,
    new_semantic_status: str | None = None,
    outcome_source: str = "unknown",
    detail: str | None = None,
    assessor_version: str | None = None,
) -> bool:
    """Append one delayed-evidence row. Never mutates the original outcome.

    Input validation raises (a malformed revision must not be silently
    coerced); storage errors return False (recording delayed evidence must
    never break whatever surfaced it). At least one of decision_id /
    execution_id must anchor the revision -- unanchored evidence is noise.
    """

    if revision_reason not in OUTCOME_REVISION_REASONS:
        raise ValueError(
            f"revision_reason must be one of {OUTCOME_REVISION_REASONS}, got {revision_reason!r}"
        )
    if outcome_source not in OUTCOME_REVISION_SOURCES:
        raise ValueError(
            f"outcome_source must be one of {OUTCOME_REVISION_SOURCES}, got {outcome_source!r}"
        )
    if new_semantic_status is not None and new_semantic_status not in _VALID_REVISION_STATUSES:
        raise ValueError(
            f"new_semantic_status must be one of {_VALID_REVISION_STATUSES}, "
            f"got {new_semantic_status!r}"
        )
    if decision_id is None and execution_id is None:
        raise ValueError("a revision must reference a decision_id or an execution_id")
    if detail is not None:
        detail = detail[:_REVISION_DETAIL_MAX_CHARS]

    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return False
    try:
        conn.execute(
            "INSERT INTO outcome_revisions "
            "(decision_id, execution_id, created_at, revision_reason, "
            " new_semantic_status, outcome_source, detail, assessor_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                execution_id,
                datetime.now(timezone.utc).isoformat(),
                revision_reason,
                new_semantic_status,
                outcome_source,
                detail,
                assessor_version,
            ),
        )
        conn.commit()
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        conn.close()


def fetch_outcome_revisions(
    db_path: str | Path,
    *,
    decision_id: str | None = None,
    execution_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read revisions for a decision and/or execution, oldest first."""

    clauses: list[str] = []
    params: list[Any] = []
    if decision_id is not None:
        clauses.append("decision_id = ?")
        params.append(decision_id)
    if execution_id is not None:
        clauses.append("execution_id = ?")
        params.append(execution_id)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return []
    try:
        rows = conn.execute(
            "SELECT id, decision_id, execution_id, created_at, revision_reason, "
            "new_semantic_status, outcome_source, detail, assessor_version "
            f"FROM outcome_revisions {where}ORDER BY id ASC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError):
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Decision lineage -- joining an execution to the route that
# caused it
# ---------------------------------------------------------------------------


def fetch_routing_decision_by_decision_id(
    db_path: str | Path, decision_id: str
) -> dict[str, Any] | None:
    """The routing decision a caller claims caused this delegation.

    Returns the newest routing row for the id (decision_id is indexed but not
    UNIQUE), or None when no such decision exists -- an unknown id must be
    refused, never silently treated as a fresh decision.
    """

    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute(
            "SELECT * FROM routing_decisions WHERE decision_id = ? ORDER BY id DESC LIMIT 1",
            (decision_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()


def count_executions_for_decision(db_path: str | Path, decision_id: str) -> int:
    """How many executions this decision has already produced.

    One decision legitimately produces several executions (a fallback chain,
    a retry). This is the attempt index for the next one -- executions are
    appended, never overwritten.
    """

    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM execution_history WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0
    except (sqlite3.Error, OSError):
        return 0
    finally:
        conn.close()
