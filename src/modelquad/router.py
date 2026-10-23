# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Pure, provider-agnostic task routing."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .capability import CapabilityGap, describe_gaps
from .config import Config, PROVIDERS, expand_capabilities as _expand_capabilities
from .quota_policy import QuotaTier, quota_evidence_for
from .quota_windows import quota_view
from .routing_freshness import routing_view
from .models import (
    NETWORK_POLICY_REFUSED,
    ExpectedConsumption,
    Freshness,
    ProviderScore,
    ProviderStatus,
    RouteTarget,
    RoutingResult,
    TaskType,
)


# Spend-preference margin: a capable, available SPEND provider is preferred over
# a higher-scoring PRESERVE provider unless the preserve provider is *materially*
# better at this task -- i.e. its task-fit exceeds the best spend provider's by
# more than this margin. This is what turns "spend X / preserve Y" from a small
# score nudge into behaviour that actually moves substantive work onto X, while
# still letting a genuinely much-better-suited preserve provider win (e.g. a
# 0.25 fit gap on architecture). Provider-name-agnostic: works for any strategy.
_SPEND_FIT_MARGIN = 0.20

# Bumped whenever the routing algorithm itself (scoring, preference stages,
# margins) changes behaviour. Recorded with every decision so a replayed
# decision can attribute a different outcome to an engine change rather than
# to different evidence.
#
# 4 makes quota display-only: no usage score, quota penalty, consumption
# preference or quota preference, and no model or effort chosen from quota.
ROUTER_ENGINE_VERSION = "4"


class Router:
    """Score normalized status snapshots without executing any external action."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def route(
        self,
        task_type: TaskType | str,
        statuses: Iterable[ProviderStatus],
        expected_consumption: ExpectedConsumption | None = None,
        required_capabilities: Iterable[str] | None = None,
        *,
        independence_excluded: Iterable[str] | None = None,
        policy_excluded: Mapping[str, str] | None = None,
        catalog: Any = None,
        now: datetime | None = None,
        effective_models: Mapping[str, str | None] | None = None,
    ) -> RoutingResult:
        """Return a transparent, advisory recommendation for one task type.

        Quota is display-only (engine 4): it never enters a score, a
        preference, a model or a reasoning effort. The ranking is task fit
        among the candidates that pass the hard gates, then the session's
        configured spend/preserve preference. The quota facts still ride on
        every candidate (``quota_tier``, reset, pool) so the reply can show
        them. ``expected_consumption`` is recorded with the result and changes
        nothing. The recommendation is advice: no worker is started from it
        (see ``ExecutionManager.delegate``).

        ``required_capabilities`` states what THIS request actually needs (from
        the caller's task capsule). It is unioned with the task type's floor,
        so it can only tighten the capability gate: a debugging task that must
        run the failing test excludes a worker that cannot run commands, while
        a read-only debugging task leaves that same worker eligible.

        ``policy_excluded`` maps a provider to the reason ModelQuad's own
        network policy refuses it -- a provider whose work this process
        performs by calling its API directly. It is a HARD
        gate like the three above it, applied before any preference stage, and
        it is passed IN rather than read here: this module stays pure, and the
        switch it reflects lives in a database only a surface has resolved
        (`netpolicy.providers_refused_by_policy` is the one call that asks).

        ``independence_excluded`` names providers this assignment may not use
        (an independent review may not go to its author); ``now`` dates the
        quota facts. ``catalog`` is accepted from callers that hold one and is
        not consulted: the router names a model only from configuration.

        ``effective_models`` supplies per-provider execution model overrides.
        A present key outranks the configured prior; a None value explicitly
        selects that runtime's default.
        """

        task = self._task_type(task_type)
        required = frozenset(required_capabilities) if required_capabilities else None
        status_by_name = {status.name: status for status in statuses}
        ordered_names = list(PROVIDERS) + sorted(set(status_by_name) - set(PROVIDERS))
        for name in ordered_names:
            if name in status_by_name:
                status_by_name[name] = routing_view(quota_view(
                    status_by_name[name], now=now,
                ), self.config.freshness, now=now)
        breakdowns = tuple(
            self._score_provider(
                task,
                name,
                status_by_name.get(name),
                required,
                now,
                policy_excluded=policy_excluded,
            )
            for name in ordered_names
        )
        # HARD CONSTRAINTS FIRST, then preference. `eligible` is the single
        # gate: availability, configuration, and capability rejections all
        # set it, and ProviderScore guarantees an ineligible candidate has no
        # preference score. Every preference stage below is handed only this
        # list, so no preference rule can resurrect an impossible candidate.
        candidates = [item for item in breakdowns if item.eligible and item.score is not None]
        if not candidates:
            ineligible = [item for item in breakdowns if not item.eligible]
            detail = "; ".join(
                f"{item.provider}: {', '.join(item.ineligibility_reasons) or 'not eligible'}"
                for item in ineligible
            )
            return RoutingResult(
                task_type=task,
                providers=breakdowns,
                recommendation=None,
                reason=(
                    f"No eligible providers for this stage ({detail})"
                    if detail
                    else "No available providers have a score."
                ),
                decision_source="deterministic",
                expected_consumption=expected_consumption,
            )
        top = max(candidates, key=lambda item: (item.score, item.fit or 0.0, item.provider))
        winner, independence_reason = self._apply_independence(
            top, candidates, independence_excluded,
        )
        if winner is None:
            # An honest refusal: every eligible candidate was excluded by the
            # caller's independence requirement. Nothing is substituted.
            return RoutingResult(
                task_type=task,
                providers=breakdowns,
                recommendation=None,
                reason=independence_reason or "No independent provider is available.",
                decision_source="deterministic",
                expected_consumption=expected_consumption,
            )
        final_reason = independence_reason
        target = self._build_target(winner.provider, task)
        if effective_models is not None and winner.provider in effective_models:
            target = RouteTarget(
                provider=winner.provider,
                model=effective_models[winner.provider],
                reasoning_effort=target.reasoning_effort if target else None,
                model_source="explicit_override",
            )
        return RoutingResult(
            task_type=task,
            providers=breakdowns,
            recommendation=winner.provider,
            reason=final_reason,
            target=target,
            decision_source="deterministic",
            expected_consumption=expected_consumption,
        )

    def _apply_independence(
        self,
        winner: ProviderScore,
        candidates: list[ProviderScore],
        independence_excluded: Iterable[str] | None,
    ) -> tuple[ProviderScore | None, str | None]:
        """Keep the pick unless this assignment may not use it.

        Independence says which provider may not do the work; it does not
        suspend the user's instruction to preserve another one. When the two
        together leave nothing, the answer is ``(None, reason)``, never a
        quiet violation of either. The replacement is the best remaining
        candidate by score (task fit), never by quota.
        """

        excluded = frozenset(independence_excluded or ())
        if winner.provider not in excluded:
            return winner, None
        allowed = [c for c in candidates if c.provider not in excluded]
        if not allowed:
            blocked = ", ".join(sorted({c.provider for c in candidates} & excluded)) or "every candidate"
            return None, (
                "No independent provider is available for this task: "
                f"{blocked} could not be used because independence is required."
            )
        chosen = max(allowed, key=lambda c: (c.score or 0.0, c.fit or 0.0, c.provider))
        return chosen, f"{winner.provider.capitalize()} could not be used because independence is required."

    def _build_target(self, provider: str, task: TaskType) -> RouteTarget | None:
        """Attach a RouteTarget only when a configured prior exists for this pair.

        Purely a lookup into human-authored `Config.model_policy` -- no
        learned/empirical optimization happens here (see config.py's own
        comment on why `model` is absent from the packaged defaults).
        """

        entry = self.config.model_policy.get(provider, {}).get(task.value)
        if not entry:
            return None
        model = entry.get("model")
        reasoning_effort = entry.get("reasoning_effort")
        if model is None and reasoning_effort is None:
            return None
        return RouteTarget(
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            model_source="configured_prior",
        )

    def _task_type(self, task_type: TaskType | str) -> TaskType:
        if isinstance(task_type, TaskType):
            return task_type
        try:
            return TaskType(task_type)
        except ValueError as exc:
            choices = ", ".join(item.value for item in TaskType)
            raise ValueError(f"Unknown task type {task_type!r}; choose one of: {choices}") from exc

    def eligibility_refusal(
        self,
        task: TaskType | str,
        provider_name: str,
        status: ProviderStatus | None,
        required_capabilities: Iterable[str] | None = None,
        now: datetime | None = None,
        *,
        policy_excluded: Mapping[str, str] | None = None,
    ) -> ProviderScore | None:
        """Return the authoritative hard-gate refusal, or None when eligible.

        Both routed and explicit choices use this gate. It neither computes a
        preference score nor chooses a provider, model, or reasoning effort.
        """
        task = self._task_type(task)
        required_capabilities = frozenset(required_capabilities) if required_capabilities else None
        fit = self.config.task_fit[task.value].get(provider_name)
        if status is None:
            return ProviderScore(
                provider=provider_name,
                available=False,
                fit=fit,
                remaining_percentage=None,
                freshness=Freshness.UNKNOWN,
                usage_score=None,
                usage_confidence=None,
                quota_penalty=None,
                score=None,
                reason="No provider status was supplied.",
                eligible=False,
                ineligibility_reasons=("no_provider_status",),
                exclusion_stage="availability",
                required_capabilities=self._required_capabilities(task, required_capabilities),
                **self._quota_fields(status, now),
            )
        if fit is None:
            return ProviderScore(
                provider=provider_name,
                available=status.available,
                fit=None,
                remaining_percentage=status.remaining_percentage,
                freshness=status.freshness,
                usage_score=None,
                usage_confidence=None,
                quota_penalty=None,
                score=None,
                reason=status.reason or "No task-fit configuration exists for this provider.",
                eligible=False,
                ineligibility_reasons=("no_task_fit_configured",),
                exclusion_stage="configuration",
                required_capabilities=self._required_capabilities(task, required_capabilities),
                available_capabilities=self._provider_capabilities(provider_name),
                **self._quota_fields(status, now),
            )
        # Network policy, BEFORE availability: a provider the network policy
        # refuses is configured, reachable and entirely able -- ModelQuad is
        # simply not permitted to call its API from this process right now.
        # Reporting that as "provider unavailable" would state a falsehood
        # about the provider to explain a decision of ours, which is the same
        # error the capability model exists to prevent. Hard gate: nothing
        # below can rank it, and no preference stage ever sees it.
        refusal = (policy_excluded or {}).get(provider_name)
        if refusal is not None:
            return ProviderScore(
                provider=provider_name,
                available=status.available,
                fit=fit,
                remaining_percentage=status.remaining_percentage,
                freshness=status.freshness,
                usage_score=None,
                usage_confidence=None,
                quota_penalty=None,
                score=None,
                reason=f"Refused by ModelQuad network policy: {refusal}",
                eligible=False,
                ineligibility_reasons=(NETWORK_POLICY_REFUSED,),
                exclusion_stage="network_policy",
                required_capabilities=self._required_capabilities(task, required_capabilities),
                available_capabilities=self._provider_capabilities(provider_name),
                **self._quota_fields(status, now),
            )

        provider_config = self.config.providers.get(provider_name)
        if provider_config is not None and not provider_config.enabled:
            return ProviderScore(
                provider=provider_name, available=status.available, fit=fit,
                remaining_percentage=status.remaining_percentage, freshness=status.freshness,
                usage_score=None, usage_confidence=None, quota_penalty=None, score=None,
                reason="Provider disabled by ModelQuad configuration.", eligible=False,
                ineligibility_reasons=("provider_disabled",), exclusion_stage="configuration",
                required_capabilities=self._required_capabilities(task, required_capabilities),
                available_capabilities=self._provider_capabilities(provider_name),
                **self._quota_fields(status, now),
            )

        if not status.available:
            return ProviderScore(
                provider=provider_name,
                available=False,
                fit=fit,
                remaining_percentage=status.remaining_percentage,
                freshness=status.freshness,
                usage_score=None,
                usage_confidence=None,
                quota_penalty=None,
                score=None,
                reason=status.reason or "Provider explicitly reported itself unavailable.",
                eligible=False,
                ineligibility_reasons=("provider_unavailable",),
                exclusion_stage="availability",
                required_capabilities=self._required_capabilities(task, required_capabilities),
                available_capabilities=self._provider_capabilities(provider_name),
                **self._quota_fields(status, now),
            )

        # Capability gate: a provider whose delegated worker will not perform the
        # actions this stage requires (e.g. a files-only worker asked for a
        # command-coupled testing stage) is not routable -- score None so it is
        # excluded from candidates rather than "winning" and then completing as
        # blocked, forcing the host to redo the work.
        #
        # WHY the refusal is not phrased as "lacks": a capability can be absent
        # because the runtime cannot do it, because ModelQuad forbids it, or
        # because it failed here. Those are different facts about different
        # actors, and saying "lacks" for all three tells the user a falsehood
        # about their agent -- a runtime may run commands while ModelQuad
        # withholds a grant it cannot bound. Each gap therefore carries its
        # own state, and the reason string is built from it.
        gaps = self._capability_gaps(task, provider_name, required_capabilities)
        if gaps:
            return ProviderScore(
                provider=provider_name,
                available=status.available,
                fit=fit,
                remaining_percentage=status.remaining_percentage,
                freshness=status.freshness,
                usage_score=None,
                usage_confidence=None,
                quota_penalty=None,
                score=None,
                reason=f"cannot perform this stage: {describe_gaps(gaps)}",
                capable=False,
                missing_capabilities=tuple(gap.capability for gap in gaps),
                eligible=False,
                ineligibility_reasons=tuple(gap.reason_code for gap in gaps),
                exclusion_stage="capability",
                required_capabilities=self._required_capabilities(task, required_capabilities),
                available_capabilities=self._provider_capabilities(provider_name),
                capability_gaps=tuple(gap.to_dict() for gap in gaps),
                **self._quota_fields(status, now),
            )

        return None

    def _score_provider(
        self,
        task: TaskType,
        provider_name: str,
        status: ProviderStatus | None,
        required_capabilities: frozenset[str] | None = None,
        now: datetime | None = None,
        *,
        policy_excluded: Mapping[str, str] | None = None,
    ) -> ProviderScore:
        if status is not None:
            status = routing_view(status, self.config.freshness, now=now)
        refusal = self.eligibility_refusal(
            task, provider_name, status, required_capabilities, now,
            policy_excluded=policy_excluded,
        )
        if refusal is not None:
            return refusal
        assert status is not None
        fit = self.config.task_fit[task.value][provider_name]
        # Quota is display-only: the preference score is task fit alone, so
        # no reading, stale or fresh, can move a recommendation.
        score = round(fit, 6)

        return ProviderScore(
            provider=provider_name,
            available=True,
            fit=fit,
            remaining_percentage=status.remaining_percentage,
            freshness=status.freshness,
            usage_score=None,
            usage_confidence=None,
            quota_penalty=None,
            score=score,
            reason=status.reason,
            strategy=None,
            strategy_adjustment=None,
            eligible=True,
            required_capabilities=self._required_capabilities(task, required_capabilities),
            available_capabilities=self._provider_capabilities(provider_name),
            **self._quota_fields(status, now),
        )

    def _quota_fields(self, status: ProviderStatus | None, now: datetime | None) -> dict[str, Any]:
        """The quota facts a decision record needs, ridden along on each score.

        Additive and evidence-only: they never enter the score. They exist so
        `routing_decisions.providers_json` can later answer "what was this
        provider's quota tier, binding pool and reset when the decision was
        made?" without re-deriving it from a snapshot that has since moved.
        An absent status yields the honest UNKNOWN defaults.
        """

        if status is None:
            return {"quota_tier": QuotaTier.UNKNOWN.value}
        evidence = quota_evidence_for(status, None, now, freshness_thresholds=self.config.freshness)
        return {
            "quota_tier": evidence.tier.value,
            "reset_in_seconds": evidence.reset_in_seconds,
            "pool_id": evidence.pool_id,
            "refusal_observed_at": (status.metadata or {}).get("quota_refusal_observed_at"),
        }

    def _required_capabilities(
        self,
        task: TaskType,
        required_capabilities: frozenset[str] | None = None,
    ) -> tuple[str, ...]:
        """What this stage needs: the task-type floor unioned with the request.

        Recorded on every candidate so a research record can answer "what was
        actually required here?" without re-deriving it from a config version
        that may since have changed.
        """

        need = set((self.config.task_capability_requirements or {}).get(task.value, frozenset()))
        if required_capabilities:
            need |= _expand_capabilities(required_capabilities)
        return tuple(sorted(need))

    def _provider_capabilities(self, provider_name: str) -> tuple[str, ...]:
        """What this provider's worker may do here, or () when unconfigured.

        These are the OPERATIONAL capabilities -- supported, permitted and not
        known-broken. Deliberately not the supported set: what a runtime could
        do in principle is a different question from what it may do on this
        machine, and a routing surface must answer the second.

        An unconfigured provider is treated as fully capable by the gate (it is
        never silently excluded); the empty tuple here honestly says "no
        capability profile was configured", not "it can do nothing".
        """

        have = (self.config.provider_capabilities or {}).get(provider_name)
        return tuple(sorted(have)) if have is not None else ()

    def _capability_gaps(
        self,
        task: TaskType,
        provider_name: str,
        required_capabilities: frozenset[str] | None = None,
    ) -> tuple[CapabilityGap, ...]:
        """Required capabilities this provider is not operational for, and why.

        The task type supplies a floor -- what any stage of this kind needs at
        minimum -- and ``required_capabilities`` carries what THIS request
        actually needs, which the task type alone cannot know: one debugging
        task is read-only reasoning over a stack trace, the next has to run the
        failing test. The two are unioned, so a request can only ever tighten
        the requirement, never quietly drop one the task type mandates.

        Each gap carries the STATE that produced it (restricted / unsupported /
        needs-repair), so no caller downstream has to guess whether the runtime
        or ModelQuad is the reason. An unknown provider yields no gaps and stays
        routable.
        """

        if (self.config.provider_capabilities or {}).get(provider_name) is None:
            return ()
        need = set((self.config.task_capability_requirements or {}).get(task.value, frozenset()))
        if required_capabilities:
            need |= _expand_capabilities(required_capabilities)
        return self.config.capability_profile(provider_name).gaps(need)
