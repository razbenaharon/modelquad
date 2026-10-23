# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Route → execution lineage (V8 slice 3, P0).

Before this existed, `modelquad_route` minted decision id A, and the
`modelquad_delegate` that executed its advice minted an unrelated id B. The
causal join — candidate set, quota evidence, routing policy, selected runtime
→ execution → outcome — was silently broken for every advisory-then-execute
flow, which is exactly the flow a host follows.

A caller now passes the `decision_id` it received from `modelquad_route`, and
that one identity survives to the outcome. Rules:

- **An unknown decision id is refused, never absorbed.** Minting a fresh
  decision because the claimed one does not exist would fabricate lineage.
- **Direct delegation stays first-class.** No `decision_id` means no prior
  route happened; a fresh id is minted and the record says so
  (`decision_id_source="minted"`, `prior_route=False`) rather than implying
  an advisory decision that never occurred.
- **Historical routing facts are never rewritten.** When a delegation
  executes a prior decision, no second routing row is written; what the
  router recommended then and what actually executed now are preserved
  separately, with drift recorded rather than reconciled away.
- **One decision may produce many executions.** A fallback chain or retry
  appends attempts under the same decision id; `attempt_index` orders them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .provenance import new_decision_id
from .service import history as service_history

# Why a claimed lineage was refused. Closed vocabulary: these become
# delegation_attempts.failure_kind detail and must stay analyzable.
REFUSAL_UNKNOWN_DECISION = "unknown_decision_id"
REFUSAL_TASK_MISMATCH = "decision_task_type_mismatch"


@dataclass(frozen=True)
class DecisionLineage:
    """How this execution relates to a routing decision."""

    decision_id: str
    decision_id_source: str  # "caller_supplied" | "minted"
    prior_route: bool
    valid: bool = True
    refusal_reason: str | None = None
    task_type: str | None = None
    recommended_provider: str | None = None
    recommended_model: str | None = None
    recommended_runtime: str | None = None
    requested_provider: str | None = None
    attempt_index: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_override(self) -> bool:
        """True when the caller named a provider other than the recommendation."""

        return (
            self.requested_provider is not None
            and self.recommended_provider is not None
            and self.requested_provider != self.recommended_provider
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision_id_source": self.decision_id_source,
            "prior_route": self.prior_route,
            "valid": self.valid,
            "refusal_reason": self.refusal_reason,
            "task_type": self.task_type,
            "recommended_provider": self.recommended_provider,
            "recommended_model": self.recommended_model,
            "recommended_runtime": self.recommended_runtime,
            "requested_provider": self.requested_provider,
            "is_override": self.is_override,
            "attempt_index": self.attempt_index,
            "notes": list(self.notes),
        }

    def resolved(self, actual_provider: str | None, actual_runtime: str | None) -> dict[str, Any]:
        """The lineage plus what actually executed, for the execution record.

        ``target_match`` distinguishes the three honest cases: the execution
        matched the recommendation, a caller override moved it deliberately,
        or it drifted (fallback / availability change) after the decision.
        """

        payload = self.to_dict()
        payload["actual_provider"] = actual_provider
        payload["actual_runtime"] = actual_runtime
        if self.recommended_provider is None or actual_provider is None:
            payload["target_match"] = "unknown"
        elif actual_provider == self.recommended_provider:
            payload["target_match"] = "match"
        elif self.is_override and actual_provider == self.requested_provider:
            payload["target_match"] = "override"
        else:
            payload["target_match"] = "drift"
        return payload


def resolve_delegation_lineage(
    db_path: str | Path,
    *,
    decision_id: str | None,
    task_type: str,
    requested_provider: str | None = None,
) -> DecisionLineage:
    """Validate a claimed decision id, or mint a fresh one for direct delegation.

    Never raises: an invalid claim comes back as ``valid=False`` with a
    reason, so the caller refuses the delegation and records why.
    """

    if decision_id is None:
        return DecisionLineage(
            decision_id=new_decision_id(),
            decision_id_source="minted",
            prior_route=False,
            task_type=task_type,
            requested_provider=requested_provider,
            notes=("no prior routing decision was claimed by the caller",),
        )

    row = service_history.fetch_routing_decision_by_decision_id(db_path, decision_id)
    if row is None:
        return DecisionLineage(
            decision_id=decision_id,
            decision_id_source="caller_supplied",
            prior_route=False,
            valid=False,
            refusal_reason=REFUSAL_UNKNOWN_DECISION,
            task_type=task_type,
            requested_provider=requested_provider,
        )

    recorded_task = row.get("task_type")
    if recorded_task is not None and recorded_task != task_type:
        return DecisionLineage(
            decision_id=decision_id,
            decision_id_source="caller_supplied",
            prior_route=True,
            valid=False,
            refusal_reason=REFUSAL_TASK_MISMATCH,
            task_type=recorded_task,
            requested_provider=requested_provider,
            notes=(
                f"decision was routed for task_type {recorded_task!r}, "
                f"delegation claims {task_type!r}",
            ),
        )

    attempt_index = service_history.count_executions_for_decision(db_path, decision_id)
    notes: list[str] = []
    if attempt_index:
        # Legitimate (fallback / retry), but worth recording: this decision
        # has already produced executions and this one appends to the chain.
        notes.append(
            f"decision already has {attempt_index} execution(s); this is attempt "
            f"{attempt_index + 1}"
        )

    recommended = row.get("selected_provider") or row.get("recommendation")
    lineage = DecisionLineage(
        decision_id=decision_id,
        decision_id_source="caller_supplied",
        prior_route=True,
        task_type=recorded_task,
        recommended_provider=recommended,
        recommended_model=row.get("model_recommended"),
        recommended_runtime=row.get("agent_runtime_recommended"),
        requested_provider=requested_provider,
        attempt_index=attempt_index,
        notes=tuple(notes),
    )
    if lineage.is_override:
        lineage = replace(
            lineage,
            notes=lineage.notes
            + (
                f"caller explicitly requested {requested_provider!r} over the "
                f"recommended {recommended!r}",
            ),
        )
    return lineage
