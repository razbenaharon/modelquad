# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Evidence-preserving lifecycle state for a live delegation.

This is deliberately a leaf module: callers provide already-observed facts,
and this adapter never fills a gap with a clock-based or process-based guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


LIVE_STATES: frozenset[str] = frozenset(
    {
        "completed",
        "blocked",
        "failed",
        "partial",
        "unknown",
        "running",
        "delegating",
        "selected",
        # The caller stopped waiting while the worker kept going. Neither
        # "running" (nothing is waiting for it) nor "unknown" (the worker IS
        # observed in flight) says that, so it is its own state.
        "unattended",
        # ModelQuad stopped the worker and READ its death from the process
        # table. Distinct from `failed`: the worker never reported anything;
        # the ending was ours.
        "stopped",
    }
)

@dataclass(frozen=True)
class LiveFacts:
    """Objective facts needed to describe a delegation without embellishing it."""

    decision_exists: bool = False
    attempt_exists: bool = False
    attempt_status: str | None = None
    attempt_resolved: bool = False
    worker_pid: int | None = None
    worker_alive: bool | None = None
    liveness: str | None = None
    #: Whether anyone is still waiting for this delegation's result. A fact
    #: about the CALLER's transport, recorded separately, and orthogonal
    #: to ``liveness``, which is about the worker.
    caller_abandoned: bool = False
    #: The row's closed-vocabulary resolution kind
    #: (``service.history.DELEGATION_FAILURE_KINDS``). Read for exactly two
    #: values -- ``terminated`` (death confirmed from the process table) and
    #: ``timeout`` (a stop that was attempted and never confirmed). The free
    #: text beside it is never read here.
    failure_kind: str | None = None
    verification_started: bool = False
    verification_resolved: bool = False
    task_status: str | None = None
    tests_executed: bool | None = None
    tests_passed: bool | None = None
    verification_level: str | None = None


@dataclass(frozen=True)
class LiveState:
    """One user-facing lifecycle state and the fact that selected it."""

    state: str
    reason: str


def derive_live_state(facts: LiveFacts, *, now: datetime | None = None) -> LiveState:
    """Map recorded facts to one conservative, user-facing lifecycle state.

    ``now`` is accepted for a stable caller interface, but is intentionally
    not used here. Age is not evidence of a stopped worker; liveness must be
    annotated upstream before it may affect this state.
    """

    terminal = facts.verification_resolved or (facts.attempt_exists and facts.attempt_resolved)
    if terminal:
        # How the delegation ENDED outranks the outcome recorded beside it,
        # because on both of these paths the worker reported nothing and the
        # outcome was synthesised by the timeout handler. Saying "Failed"
        # there would attribute our own ending to the worker.
        if facts.failure_kind == "terminated":
            # Death read from the process table, tree included.
            return LiveState("stopped", "worker_termination_confirmed")
        if facts.failure_kind == "timeout":
            # A stop that was attempted and never confirmed. The row was
            # closed; the worker's fate was not established, and an
            # unestablished fate is UNKNOWN, not a failure it reported.
            return LiveState("unknown", "worker_termination_unconfirmed")
        if facts.task_status == "failed":
            return LiveState("failed", "worker_reported_failed")
        if facts.task_status == "blocked":
            return LiveState("blocked", "worker_reported_blocked")
        if facts.task_status == "partial":
            return LiveState("partial", "worker_reported_partial")
        if facts.task_status == "completed":
            # Objective tests outweigh the worker's success claim when they
            # directly contradict it; a process ending cannot settle this.
            if facts.tests_executed is True and facts.tests_passed is False:
                return LiveState("failed", "objective_tests_failed")
            if facts.tests_executed is True and facts.tests_passed is True:
                return LiveState("completed", "objective_tests_passed")
            return LiveState("completed", "claim_only_no_objective_tests")
        # A resolved handoff without a recognised completion envelope is an
        # absence of evidence, not a failed (or successful) execution.
        return LiveState("unknown", "attempt_resolved_without_task_status")

    open_attempt = (
        facts.attempt_exists
        and facts.attempt_status == "started"
        and not facts.attempt_resolved
    )
    if open_attempt:
        if facts.liveness == "unknown":
            # Liveness derivation itself failed for this row (an unparseable
            # timestamp upstream). That is an absence of evidence, and an
            # absence must not be promoted into "a handoff is in progress".
            return LiveState("unknown", "liveness_underivable")
        if facts.caller_abandoned and facts.liveness == "in_flight":
            # Checked BEFORE `abandoned`, and before the pid probe below,
            # because the two words mean opposite things here: the CALLER
            # left, and the worker is still observed in flight. Rendering
            # this as `running` would imply someone is still waiting for it;
            # rendering it as `no_response` would imply the worker is gone.
            return LiveState("unattended", "caller_abandoned_worker_in_flight")
        if facts.liveness == "abandoned":
            # An attempt that never came back does not prove either failure
            # or continued execution, so it remains honestly unknown.
            return LiveState("unknown", "attempt_abandoned")
        if facts.worker_pid is not None and facts.worker_alive is True:
            return LiveState("running", "worker_pid_alive")
        # Missing and negative liveness readings are not interchangeable
        # with success, but both mean a handoff is still awaiting evidence.
        return LiveState("delegating", "no_proven_live_worker")

    if facts.decision_exists and not facts.attempt_exists:
        return LiveState("selected", "decision_selected_no_attempt")

    # A decision-less row, an unrecognised attempt state, or no row at all
    # cannot be rounded into a meaningful lifecycle position.
    return LiveState("unknown", "no_lifecycle_evidence")


def facts_from_attempt(
    row: dict[str, Any] | None,
    *,
    decision_exists: bool = False,
    worker_alive: bool | None = None,
    task_status: str | None = None,
    tests_executed: bool | None = None,
    tests_passed: bool | None = None,
) -> LiveFacts:
    """Assemble :class:`LiveFacts` from one delegation_attempts row.

    ``worker_alive`` is passed in rather than probed here so this module
    stays pure and testable: the caller owns the process probe. It must be
    the result of an ACTUAL pid probe, never the age-based fallback
    ``annotate_attempt_liveness`` uses for pid-less rows -- an attempt that
    is merely young is not a running worker, and rule "Running requires
    proven liveness" is the whole point of keeping them separate.
    """

    if row is None:
        return LiveFacts(decision_exists=decision_exists)
    return LiveFacts(
        decision_exists=decision_exists or bool(row.get("decision_id")),
        attempt_exists=True,
        attempt_status=row.get("status"),
        attempt_resolved=row.get("resolved_at") is not None,
        worker_pid=row.get("worker_pid"),
        worker_alive=worker_alive,
        liveness=row.get("liveness"),
        caller_abandoned=bool(row.get("caller_abandoned")),
        failure_kind=row.get("failure_kind"),
        verification_started=row.get("verification_started_at") is not None,
        verification_resolved=row.get("verification_resolved_at") is not None,
        task_status=task_status if task_status is not None else row.get("outcome"),
        tests_executed=tests_executed,
        tests_passed=tests_passed,
    )


@dataclass(frozen=True)
class RecoveryOutlook:
    """What a caller who stopped waiting may conclude, and may safely do next.

    Every field is derived from one :class:`LiveState`; none of them is a new
    lifecycle. ``safe_to_retry`` is deliberately tri-state and is about
    DUPLICATE EXECUTION only -- True means no worker of this attempt can
    still be running, False means one may be, and None means that could not
    be established. None is never rounded to True: starting a second mutation
    worker beside a live one is the failure this answer exists to prevent.
    """

    state: str
    reason: str
    terminal: bool
    worker_may_be_running: bool
    safe_to_retry: bool | None
    guidance: str


#: The states in which a worker of this attempt may still be executing. Each
#: one is either an observed live worker or an unresolved handoff, and in
#: both cases a second delegation of the same request would be a duplicate.
#: ``unknown`` is in here, and the word is the reason. `worker_may_be_running`
#: asks "MAY it be running?", and the honest answer for a fate nobody
#: established is yes. Reporting False there asserted a worker was not running
#: on the strength of not knowing, and it contradicted the workspace answer:
#: for the same row -- a timeout whose tree kill was never confirmed -- that
#: one says a descendant may still be writing.
#:
#: It never reaches ``safe_to_retry``. :func:`derive_recovery_outlook` answers
#: None for ``unknown`` before it consults this set at all, so an unestablished
#: fate still refuses to call a retry safe rather than declaring it unsafe --
#: the delegation-status tri-state is unchanged in both directions.
_WORKER_MAY_RUN_STATES: frozenset[str] = frozenset(
    {"running", "unattended", "delegating", "unknown"}
)

_GUIDANCE: dict[str, str] = {
    "running": "The original delegation is still running and someone is still waiting for it. Do not start it again.",
    "unattended": "The original delegation is still running, but nobody is waiting for its result any more. Do not start it again; ask here for its result once it ends.",
    "delegating": "The original delegation has been handed off and no worker has been observed for it yet. Do not start it again until this says otherwise.",
    "completed": "The original delegation finished successfully. Read its result rather than running the same work a second time.",
    "failed": "The original delegation ran and the worker reported failure. Nothing of it is still running, so this work may be started again.",
    "blocked": "The original delegation ran and the worker reported it was blocked. Nothing of it is still running.",
    "partial": "The original delegation ran and the worker reported partial progress. Nothing of it is still running.",
    "stopped": "ModelQuad stopped the original delegation and confirmed the worker had ended. Nothing of it is still running.",
    "selected": "A provider was chosen for this work and no delegation of it was ever recorded.",
    "unknown": "What became of the original delegation could not be established. Establish it before starting the same work again.",
}


def derive_recovery_outlook(facts: LiveFacts, *, now: datetime | None = None) -> RecoveryOutlook:
    """Answer 'what happened to my delegation, and may I run it again?'.

    The caller-facing half of the delegation lifecycle, and pure like the rest of
    this module: it adds no state, probes nothing, and reads exactly what
    :func:`derive_live_state` already decided. A caller whose transport gave
    up cannot tell its own departure from the worker's failure, and this is
    where that distinction is made -- ``unattended`` is a running worker
    nobody is waiting for, and it is never reported as a failure.
    """

    live = derive_live_state(facts, now=now)
    worker_may_be_running = live.state in _WORKER_MAY_RUN_STATES
    # An unresolved row whose state could not be derived leaves the worker's
    # fate unestablished, so it is not terminal either -- and terminality is
    # read from the row rather than guessed from the state, because the same
    # `unknown` covers a row that resolved without an outcome and one that
    # never resolved at all.
    terminal = bool(facts.attempt_exists and facts.attempt_resolved)
    if live.state == "unknown":
        safe_to_retry: bool | None = None
    elif worker_may_be_running:
        safe_to_retry = False
    else:
        safe_to_retry = True
    return RecoveryOutlook(
        state=live.state,
        reason=live.reason,
        terminal=terminal,
        worker_may_be_running=worker_may_be_running,
        safe_to_retry=safe_to_retry,
        guidance=_GUIDANCE.get(live.state, _GUIDANCE["unknown"]),
    )
