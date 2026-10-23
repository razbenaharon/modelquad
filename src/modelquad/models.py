# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Shared, serializable models for provider status and routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


#: The one name for "ModelQuad's network policy said no", shared by the two
#: places that have to agree about it: the routing gate that makes an
#: API-executed provider ineligible (``ProviderScore.ineligibility_reasons``)
#: and the ``task_status_reason`` an executor sets when the same
#: gate refuses the call it was about to make. Two spellings of
#: one vocabulary term is how a surface starts recognising only one of them.
NETWORK_POLICY_REFUSED = "network_policy_refused"


class Freshness(str, Enum):
    """Confidence level for an observed provider usage value."""

    FRESH = "fresh"
    STALE = "stale"
    VERY_STALE = "very_stale"
    UNKNOWN = "unknown"


class TaskType(str, Enum):
    ARCHITECTURE = "architecture"
    IMPLEMENTATION = "implementation"
    EXPLORATION = "exploration"
    DEBUGGING = "debugging"
    TESTING = "testing"
    REVIEW = "review"
    TRIVIAL = "trivial"


@dataclass(frozen=True)
class FreshnessThresholds:
    """Age boundaries used while evaluating usage observations."""

    fresh_seconds: int = 300
    stale_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.fresh_seconds < 0 or self.stale_seconds < self.fresh_seconds:
            raise ValueError("freshness thresholds must satisfy 0 <= fresh <= stale")


class ModelIdentitySource(str, Enum):
    """Confidence level for a model identity observation.

    An unverified model identity must never be silently treated as confirmed;
    CONFIRMED requires objective runtime evidence.
    """

    CONFIRMED = "confirmed"
    REQUESTED = "requested"
    REPORTED = "reported"
    CONFIGURED = "configured"
    UNKNOWN = "unknown"


class RuntimeRole(str, Enum):
    """What a runtime was doing in a decision.

    Runtime identity does NOT determine role. Claude Code is only ever the
    host *today* because no Claude executor is registered -- an
    implementation limitation, not a product law. Codex is
    only ever a worker today for the same reason: no host adapter reports
    them as the context owner. Encoding either as permanent would make the
    future cross-host matrix (Codex host -> Claude worker, and so on)
    unrepresentable, so role is always recorded explicitly.
    """

    CURRENT_HOST = "current_host"
    DELEGATED_WORKER = "delegated_worker"
    UNKNOWN = "unknown"


# Model-family prefixes, longest first so "gpt-5-codex" resolves to "gpt"
# rather than accidentally matching something shorter. Deriving a family
# from an exact id is a PARSE of an existing observation, never a new
# observation: it can never raise provenance (see RuntimeModelIdentity).
_MODEL_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude-opus", "opus"),
    ("claude-sonnet", "sonnet"),
    ("claude-haiku", "haiku"),
    ("claude-fable", "fable"),
    ("gemini", "gemini"),
    ("gpt", "gpt"),
    ("o1", "o-series"),
    ("o3", "o-series"),
)


def model_family_from_exact_id(exact_id: str | None) -> str | None:
    """Best-effort family for an exact model id, or None when unrecognized.

    Deliberately conservative: an id this does not recognize yields None
    rather than a guessed family. Recognizing a *family* never implies the
    exact version is known -- "opus" does not distinguish Opus 4.8 from
    Opus 5, which is precisely the drift signal the dataset must preserve.
    """

    if not exact_id:
        return None
    lowered = exact_id.strip().lower()
    for prefix, family in _MODEL_FAMILY_PREFIXES:
        if lowered.startswith(prefix):
            return family
    return None


@dataclass(frozen=True)
class RuntimeModelIdentity:
    """Who ran (or would run) a task, at full resolution.

    Six independent axes the research dataset must never collapse:

    1. ``runtime``          -- the harness (claude_code / codex)
    2. ``runtime_version``  -- the harness's OWN version
    3. ``model_family``     -- opus / gpt / gemini
    4. ``model_exact_id``   -- claude-opus-5, distinct from claude-opus-4-5
    5. ``model_source``     -- provenance of 3+4
    6. ``role``             -- host vs delegated worker

    Runtime version and model version are separate experimental variables:
    "Claude Code vA + Opus 4.8" vs "Claude Code vA + Opus 5" isolates a model
    change, while "Claude Code vA + Opus 5" vs "Claude Code vB + Opus 5"
    isolates a harness change. Collapsing either axis destroys the ability to
    attribute a performance shift to the right cause.
    """

    runtime: str | None = None
    runtime_version: str | None = None
    model_provider: str | None = None
    model_family: str | None = None
    model_exact_id: str | None = None
    model_display_name: str | None = None
    model_source: ModelIdentitySource = ModelIdentitySource.UNKNOWN
    role: RuntimeRole = RuntimeRole.UNKNOWN
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.model_source, str):
            try:
                object.__setattr__(
                    self, "model_source", ModelIdentitySource(self.model_source)
                )
            except ValueError:
                object.__setattr__(self, "model_source", ModelIdentitySource.UNKNOWN)
        if isinstance(self.role, str):
            try:
                object.__setattr__(self, "role", RuntimeRole(self.role))
            except ValueError:
                object.__setattr__(self, "role", RuntimeRole.UNKNOWN)
        # A family may be filled in from an exact id, because that is
        # reading the same observation more precisely. It NEVER changes
        # model_source: parsing a string is not runtime confirmation.
        if self.model_family is None and self.model_exact_id:
            derived = model_family_from_exact_id(self.model_exact_id)
            if derived is not None:
                object.__setattr__(self, "model_family", derived)
        # An identity with no model evidence at all must not claim a source.
        if (
            self.model_exact_id is None
            and self.model_family is None
            and self.model_source is not ModelIdentitySource.UNKNOWN
        ):
            raise ValueError(
                f"model_source={self.model_source.value!r} claims provenance for a "
                "model identity that carries no model information"
            )

    @property
    def is_confirmed(self) -> bool:
        """True only for objective runtime evidence of the exact model."""

        return (
            self.model_source is ModelIdentitySource.CONFIRMED
            and self.model_exact_id is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "model_provider": self.model_provider,
            "model_family": self.model_family,
            "model_exact_id": self.model_exact_id,
            "model_display_name": self.model_display_name,
            "model_source": self.model_source.value,
            "role": self.role.value,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeModelIdentity":
        return cls(
            runtime=data.get("runtime"),
            runtime_version=data.get("runtime_version"),
            model_provider=data.get("model_provider"),
            model_family=data.get("model_family"),
            model_exact_id=data.get("model_exact_id"),
            model_display_name=data.get("model_display_name"),
            model_source=data.get("model_source", ModelIdentitySource.UNKNOWN),
            role=data.get("role", RuntimeRole.UNKNOWN),
            observed_at=data.get("observed_at"),
        )


class ConsumptionBucket(str, Enum):
    """Estimated resource consumption tier for a task."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConsumptionSource(str, Enum):
    """Origin of a consumption estimate."""

    HOST_SUPPLIED = "host_supplied"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExpectedConsumption:
    """Expected consumption is EVIDENCE about how much provider capacity a task will likely consume; it is independent of reasoning difficulty and never a routing decision by itself."""

    bucket: ConsumptionBucket
    source: ConsumptionSource = ConsumptionSource.UNKNOWN
    basis: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.bucket, str):
            try:
                object.__setattr__(self, "bucket", ConsumptionBucket(self.bucket))
            except ValueError as exc:
                valid = sorted(b.value for b in ConsumptionBucket)
                raise ValueError(f"bucket must be one of {valid}, got {self.bucket!r}") from exc
        elif not isinstance(self.bucket, ConsumptionBucket):
            valid = sorted(b.value for b in ConsumptionBucket)
            raise ValueError(f"bucket must be one of {valid}, got {self.bucket!r}")

        if isinstance(self.source, str):
            try:
                object.__setattr__(self, "source", ConsumptionSource(self.source))
            except ValueError:
                object.__setattr__(self, "source", ConsumptionSource.UNKNOWN)
        elif not isinstance(self.source, ConsumptionSource):
            object.__setattr__(self, "source", ConsumptionSource.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket.value,
            "source": self.source.value,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExpectedConsumption:
        if not isinstance(data, dict):
            raise ValueError("ExpectedConsumption data must be a dictionary")
        bucket_raw = data.get("bucket")
        source_raw = data.get("source")
        try:
            source = (
                ConsumptionSource(source_raw)
                if source_raw is not None
                else ConsumptionSource.UNKNOWN
            )
        except (ValueError, TypeError):
            source = ConsumptionSource.UNKNOWN
        return cls(
            bucket=bucket_raw,
            source=source,
            basis=data.get("basis"),
        )



@dataclass(frozen=True)
class HostContext:
    """Observed parent host client state and identity.

    ``current_agent_runtime`` names the agent runtime the host session runs in
    (V6: a host is not a subscription, and a provider is not a runtime). It is
    derived from the host client identity when that mapping is known and stays
    ``None`` otherwise -- never guessed.

    ``current_runtime_version`` is the harness's OWN version and is a
    different experimental variable from the model: "Claude Code vA + Opus 5"
    vs "vB + Opus 5" isolates a harness change, which is only attributable if
    the two axes stay in separate fields.

    ``current_model`` may hold either a family ("opus") or an exact id
    ("claude-opus-5"); ``current_model_source`` is what says which kind of
    claim it is and how much to believe it. A family is never widened into an
    exact release to fill this field.
    """

    host: str | None = None
    current_agent_runtime: str | None = None
    current_runtime_version: str | None = None
    current_provider: str | None = None
    current_model: str | None = None
    current_model_source: ModelIdentitySource = ModelIdentitySource.UNKNOWN
    current_reasoning_mode: str | None = None
    context_owner: str | None = None
    context_richness: str | None = None

    _VALID_RICHNESS = frozenset({"none", "sparse", "rich"})

    def __post_init__(self) -> None:
        if isinstance(self.current_model_source, str):
            try:
                object.__setattr__(
                    self, "current_model_source", ModelIdentitySource(self.current_model_source)
                )
            except ValueError:
                object.__setattr__(self, "current_model_source", ModelIdentitySource.UNKNOWN)
        if self.context_richness is not None and self.context_richness not in self._VALID_RICHNESS:
            raise ValueError(
                f"context_richness must be one of {sorted(self._VALID_RICHNESS)}, got {self.context_richness!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "current_agent_runtime": self.current_agent_runtime,
            "current_runtime_version": self.current_runtime_version,
            "current_provider": self.current_provider,
            "current_model": self.current_model,
            "current_model_source": self.current_model_source.value,
            "current_reasoning_mode": self.current_reasoning_mode,
            "context_owner": self.context_owner,
            "context_richness": self.context_richness,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HostContext:
        source_raw = data.get("current_model_source")
        try:
            source = ModelIdentitySource(source_raw) if source_raw is not None else ModelIdentitySource.UNKNOWN
        except (ValueError, TypeError):
            source = ModelIdentitySource.UNKNOWN
        return cls(
            host=data.get("host"),
            current_agent_runtime=data.get("current_agent_runtime"),
            current_runtime_version=data.get("current_runtime_version"),
            current_provider=data.get("current_provider"),
            current_model=data.get("current_model"),
            current_model_source=source,
            current_reasoning_mode=data.get("current_reasoning_mode"),
            context_owner=data.get("context_owner"),
            context_richness=data.get("context_richness"),
        )


@dataclass(frozen=True)
class CapacityState:
    """Observed provider quota, reset timing, and burn rate.

    Unknown stays None — the product never invents quota or reset values.
    """

    remaining_percentage: float | None = None
    used_percentage: float | None = None
    reset_at: datetime | None = None
    window: str | None = None
    freshness: Freshness = Freshness.UNKNOWN
    collected_at: datetime | None = None
    burn_rate_per_hour: float | None = None
    resource_objective: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.freshness, str):
            object.__setattr__(self, "freshness", Freshness(self.freshness))
        if self.remaining_percentage is not None and not 0 <= self.remaining_percentage <= 100:
            raise ValueError("remaining_percentage must be between 0 and 100")
        if self.used_percentage is not None and not 0 <= self.used_percentage <= 100:
            raise ValueError("used_percentage must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        return {
            "remaining_percentage": self.remaining_percentage,
            "used_percentage": self.used_percentage,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "window": self.window,
            "freshness": self.freshness.value,
            "collected_at": self.collected_at.isoformat() if self.collected_at else None,
            "burn_rate_per_hour": self.burn_rate_per_hour,
            "resource_objective": self.resource_objective,
        }


class QuotaUnit(str, Enum):
    """The native unit a resource pool's quota is expressed in.

    Percent-remaining is common but not universal (a provider may report a
    prepaid balance). A pool whose unit is not PERCENT must leave
    ``remaining_percentage`` as ``None`` rather than fake a normalization.
    """

    PERCENT = "percent"
    CREDITS = "credits"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResourcePool:
    """One observed paid quota pool (V6: the subscription is a first-class resource).

    A pool belongs to a subscription/vendor (``provider``), never to a model
    or an agent runtime. An integration that today maps one provider to one
    pool is an integration fact, not an invariant: a subscription that exposes
    independently-moving quota groups surfaces here as several pools.

    ``raw`` preserves the provider-native quota facts verbatim (windows,
    buckets, reset times, credit balances) with no normalization; ``plan`` is
    only ever a provider-reported string (``plan_source`` records that), and
    stays ``None`` when the provider does not report one -- never inferred.
    """

    id: str
    provider: str
    display_name: str | None = None
    plan: str | None = None
    plan_source: str = "unknown"
    quota_unit: QuotaUnit = QuotaUnit.PERCENT
    remaining_percentage: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None
    freshness: Freshness = Freshness.UNKNOWN
    available: bool = True
    reason: str | None = None

    _VALID_PLAN_SOURCES = frozenset({"provider_reported", "unknown"})

    def __post_init__(self) -> None:
        if isinstance(self.quota_unit, str):
            try:
                object.__setattr__(self, "quota_unit", QuotaUnit(self.quota_unit))
            except ValueError:
                object.__setattr__(self, "quota_unit", QuotaUnit.UNKNOWN)
        if isinstance(self.freshness, str):
            object.__setattr__(self, "freshness", Freshness(self.freshness))
        if self.plan_source not in self._VALID_PLAN_SOURCES:
            raise ValueError(
                f"plan_source must be one of {sorted(self._VALID_PLAN_SOURCES)}, got {self.plan_source!r}"
            )
        if self.remaining_percentage is not None and not 0 <= self.remaining_percentage <= 100:
            raise ValueError("remaining_percentage must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "plan": self.plan,
            "plan_source": self.plan_source,
            "quota_unit": self.quota_unit.value,
            "remaining_percentage": self.remaining_percentage,
            "raw": self.raw,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "freshness": self.freshness.value,
            "available": self.available,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourcePool:
        observed_raw = data.get("observed_at")
        observed_at = None
        if isinstance(observed_raw, str):
            try:
                observed_at = datetime.fromisoformat(observed_raw)
            except ValueError:
                observed_at = None
        try:
            freshness = Freshness(data.get("freshness")) if data.get("freshness") else Freshness.UNKNOWN
        except ValueError:
            freshness = Freshness.UNKNOWN
        return cls(
            id=str(data.get("id", "")),
            provider=str(data.get("provider", "")),
            display_name=data.get("display_name"),
            plan=data.get("plan"),
            plan_source=data.get("plan_source", "unknown"),
            quota_unit=data.get("quota_unit", QuotaUnit.UNKNOWN),
            remaining_percentage=data.get("remaining_percentage"),
            raw=dict(data.get("raw") or {}),
            observed_at=observed_at,
            freshness=freshness,
            available=bool(data.get("available", True)),
            reason=data.get("reason"),
        )


@dataclass(frozen=True)
class ProviderStatus:
    """Normalized, read-only usage state for one integration's resource pools.

    ``remaining_percentage`` is the derived headline scalar (tightest pool or
    window wins) kept for routing and display; the pool-level truth lives in
    ``pools``, one entry per observed quota pool. An empty ``pools`` tuple
    means the integration predates pool reporting for this observation, not
    that the subscription has no pools. ``plan`` is the provider-reported
    subscription plan when one is observed, never inferred.
    """

    name: str
    available: bool = True
    remaining_percentage: float | None = None
    observed_at: datetime | None = None
    freshness: Freshness = Freshness.UNKNOWN
    metadata: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    capacity: CapacityState | None = None
    pools: tuple[ResourcePool, ...] = ()
    plan: str | None = None

    def __post_init__(self) -> None:
        if self.remaining_percentage is not None and not 0 <= self.remaining_percentage <= 100:
            raise ValueError("remaining_percentage must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "remaining_percentage": self.remaining_percentage,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "freshness": self.freshness.value,
            "metadata": self.metadata,
            "reason": self.reason,
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "pools": [pool.to_dict() for pool in self.pools],
            "plan": self.plan,
        }


@dataclass(frozen=True)
class ProviderScore:
    """One routing candidate's contribution to a recommendation.

    A candidate is conceptually a ``(resource_pool, agent_runtime)``
    combination (V6). ``None`` for either field means "the provider's default
    pool/runtime" -- the current Beta integrations each expose exactly one of
    each, an integration fact the scorer does not depend on.
    """

    provider: str
    available: bool
    fit: float | None
    remaining_percentage: float | None
    freshness: Freshness
    usage_score: float | None
    usage_confidence: float | None
    quota_penalty: float | None
    score: float | None
    reason: str | None = None
    strategy: str | None = None
    strategy_adjustment: float | None = None
    capable: bool = True
    missing_capabilities: tuple[str, ...] = ()
    resource_pool: str | None = None
    agent_runtime: str | None = None
    # Eligibility is a HARD constraint answer ("could this runtime legally
    # perform this task at all?") and is deliberately separate from
    # preference ("was it the better choice?"). Conflating them teaches a
    # future learned router a false relationship -- that one runtime was
    # slightly preferred over another, when the truth was that the other could
    # not run the tests the task required. ``exclusion_stage`` names WHICH
    # hard gate rejected the candidate, so the exclusion is analyzable
    # rather than merely a null score.
    eligible: bool = True
    ineligibility_reasons: tuple[str, ...] = ()
    exclusion_stage: str | None = None
    required_capabilities: tuple[str, ...] = ()
    available_capabilities: tuple[str, ...] = ()
    # One entry per capability that excluded this candidate, each naming the
    # STATE that produced it (restricted / unsupported / needs_repair /
    # unverified) and, for a restriction, ModelQuad's own stated reason.
    #
    # `missing_capabilities` above answers only "which"; this answers "why",
    # and the difference is the difference between a true and a false statement
    # about a provider. Without it, a decision record cannot distinguish a
    # runtime that cannot do the work from one ModelQuad declined to let do it
    # -- so a policy choice would be replayed forever as a provider limitation,
    # and any learning built on that history would inherit the error.
    capability_gaps: tuple[dict[str, Any], ...] = ()
    # Quota evidence AT THE MOMENT OF THE DECISION (router engine 3).
    # Additive and evidence-only: none of these enters the score, and
    # each stays honestly absent when the provider did not report it. They
    # ride in `routing_decisions.providers_json`, so a later reader can ask
    # "what was this provider's quota tier, binding pool and reset when this
    # was decided?" without re-deriving it from a snapshot that has moved.
    #
    # `quota_tier` defaults to "unknown" rather than to any numeric band: an
    # absent reading is not an empty one and must never become one.
    quota_tier: str = "unknown"
    reset_in_seconds: float | None = None
    refusal_observed_at: str | None = None
    pool_id: str | None = None

    # "network_policy" is ModelQuad's own outbound-traffic gate (`netpolicy`)
    # refusing a provider whose work this process would perform by
    # calling its API directly. It is a separate stage from "availability"
    # on purpose: the provider is configured, reachable and perfectly able --
    # ModelQuad is simply not permitted to call it right now, and recording
    # that as unavailability would blame the provider for our decision.
    EXCLUSION_STAGES = frozenset(
        {"availability", "configuration", "capability", "network_policy"}
    )

    def __post_init__(self) -> None:
        if self.exclusion_stage is not None and self.exclusion_stage not in self.EXCLUSION_STAGES:
            raise ValueError(
                f"exclusion_stage must be one of {sorted(self.EXCLUSION_STAGES)} or None, "
                f"got {self.exclusion_stage!r}"
            )
        # An ineligible candidate must never carry a preference score: a
        # number here would read as a ranked alternative downstream.
        if not self.eligible and self.score is not None:
            raise ValueError(
                f"ineligible candidate {self.provider!r} must not carry a preference score"
            )

    @property
    def preference_score(self) -> float | None:
        """The score, but only where a preference is meaningful (eligible)."""

        return self.score if self.eligible else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "fit": self.fit,
            "remaining_percentage": self.remaining_percentage,
            "freshness": self.freshness.value,
            "usage_score": self.usage_score,
            "usage_confidence": self.usage_confidence,
            "quota_penalty": self.quota_penalty,
            "score": self.score,
            "reason": self.reason,
            "strategy": self.strategy,
            "strategy_adjustment": self.strategy_adjustment,
            "capable": self.capable,
            "missing_capabilities": list(self.missing_capabilities),
            "resource_pool": self.resource_pool,
            "agent_runtime": self.agent_runtime,
            "eligible": self.eligible,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "exclusion_stage": self.exclusion_stage,
            "preference_score": self.preference_score,
            "required_capabilities": list(self.required_capabilities),
            "available_capabilities": list(self.available_capabilities),
            "capability_gaps": [dict(gap) for gap in self.capability_gaps],
            "quota_tier": self.quota_tier,
            "reset_in_seconds": self.reset_in_seconds,
            "refusal_observed_at": self.refusal_observed_at,
            "pool_id": self.pool_id,
        }

    @property
    def excluded_by_policy(self) -> bool:
        """True when ModelQuad's own policy, not the runtime, excluded this.

        Outcome classification and any future learned router read this: a
        candidate refused for a policy reason carries no information about that
        provider's ability, and counting it as one would teach the product a
        falsehood it created itself.
        """

        if self.exclusion_stage == "network_policy":
            # ModelQuad's own network switch, not anything about the provider.
            return True
        return bool(self.capability_gaps) and all(
            gap.get("is_policy_restriction") for gap in self.capability_gaps
        )


@dataclass(frozen=True)
class RouteTarget:
    """A routing decision's execution identity (V6: pool ≠ runtime ≠ model).

    ``provider`` is the only field every caller can rely on; the rest are
    optional refinements. A missing ``model`` means "use the runtime's default
    behavior" -- model selection is never mandatory. ``model_source`` records
    *why* a model is set, distinguishing a human-authored config prior from an
    explicit caller override, so history never has to guess.

    ``resource_pool`` names which subscription capacity the decision spends
    and ``agent_runtime`` which execution environment runs it. ``None`` means
    "the provider's default pool/runtime" -- resolved against the registry at
    execution time, never guessed here. Selecting a provider does not by
    itself determine a model, and selecting a model does not determine a
    runtime; this type can express any combination.
    """

    provider: str
    model: str | None = None
    reasoning_effort: str | None = None
    model_source: str = "unset"
    resource_pool: str | None = None
    agent_runtime: str | None = None

    # "quota_policy" is the router's own capacity recommendation (engine
    # 3). A human-authored `model_policy` stays authoritative over every field
    # it STATES: the quota policy fills a target nobody configured for this
    # provider/task pair, and -- because the packaged policy deliberately
    # states a reasoning effort and never a model id -- the model of a target
    # whose prior leaves it unset. It never overwrites a configured value.
    _VALID_SOURCES = frozenset(
        {"unset", "configured_prior", "explicit_override", "quota_policy"}
    )

    def __post_init__(self) -> None:
        if self.model_source not in self._VALID_SOURCES:
            raise ValueError(
                f"model_source must be one of {sorted(self._VALID_SOURCES)}, got {self.model_source!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "model_source": self.model_source,
            "resource_pool": self.resource_pool,
            "agent_runtime": self.agent_runtime,
        }


DECISION_SOURCES: tuple[str, ...] = (
    "deterministic",
    "host_reasoned",
    "delegated_reasoned",
    "user_override",
    "fallback",
    "learned_future",
)


@dataclass(frozen=True)
class RoutingResult:
    """A deterministic routing decision with every evaluated provider."""

    task_type: TaskType
    providers: tuple[ProviderScore, ...]
    recommendation: str | None
    reason: str | None = None
    target: RouteTarget | None = None
    decision_source: str = "deterministic"
    expected_consumption: ExpectedConsumption | None = None

    def __post_init__(self) -> None:
        if self.decision_source not in DECISION_SOURCES:
            raise ValueError(
                f"decision_source must be one of {DECISION_SOURCES}, got {self.decision_source!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_type": self.task_type.value,
            "providers": [provider.to_dict() for provider in self.providers],
            "recommendation": self.recommendation,
            "reason": self.reason,
            "decision_source": self.decision_source,
        }
        if self.target is not None:
            payload["target"] = self.target.to_dict()
        if self.expected_consumption is not None:
            payload["expected_consumption"] = self.expected_consumption.to_dict()
        return payload
