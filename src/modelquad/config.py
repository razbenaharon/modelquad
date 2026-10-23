# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Configuration loading and validation for the routing core."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Mapping

import yaml

from .models import FreshnessThresholds, TaskType


DEFAULT_TASK_FIT: dict[str, dict[str, float]] = {
    "architecture": {"claude": 1.00, "codex": 0.55},
    "implementation": {"claude": 0.70, "codex": 1.00},
    "exploration": {"claude": 0.80, "codex": 0.70},
    "debugging": {"claude": 0.90, "codex": 0.90},
    "testing": {"claude": 0.70, "codex": 1.00},
    "review": {"claude": 1.00, "codex": 0.80},
    "trivial": {"claude": 1.00, "codex": 0.50},
}
PROVIDERS = ("claude", "codex")
DEFAULT_SESSIONS_PATH = Path.home() / ".modelquad" / "sessions.json"


# --- Worker capability model -------------------------------------------------
# What a delegated worker may actually DO inside its workspace, and what each
# task stage minimally requires to complete. A provider that is not operational
# for a required capability is not routable for that stage: this is what stops
# the router from recommending a files-only worker for a stage that must run
# shell commands (tests/builds), instead of it silently "completing" as blocked
# and forcing the host to redo the work.
#
# The vocabulary itself now lives in `capability.py`, which is also where the
# four facts a capability carries (supported / allowed / verified / operational)
# are kept apart. It is re-exported here because most of this repository reads
# these names from `config`.
#
# Provider capability is derived from the registry's declarations, which state
# support and permission separately, so there is nothing here to drift.
from .capability import (  # noqa: E402  -- re-exported vocabulary, see above
    ALL_CAPABILITIES,
    CAP_COMMAND,
    CAP_READ,
    CAP_WRITE,
    CAPABILITY_PROFILE_KEYS,
    CapabilityProfile,
    coarse_capabilities_from_profile,
    expand_capabilities,
    profile_from_declaration,
)


def default_provider_capability_profiles() -> dict[str, CapabilityProfile]:
    """The canonical per-provider capability truth, derived from the registry.

    Imported lazily: the registry is the owner of provider facts, and a
    module-level import here would make the configuration layer depend on
    plugin construction just to answer a question about vocabulary.
    """

    from .registry import DEFAULT_WORKER_CAPABILITY_DECLARATIONS

    return {
        provider: profile_from_declaration(provider, declaration)
        for provider, declaration in DEFAULT_WORKER_CAPABILITY_DECLARATIONS.items()
    }


def _default_provider_capabilities() -> dict[str, set[str]]:
    """Coarse OPERATIONAL capability sets, for the legacy set-shaped callers."""

    return {
        provider: set(profile.operational_capabilities)
        for provider, profile in default_provider_capability_profiles().items()
    }


#: The effective default gate, kept as a module constant for the callers that
#: still read a flat set. It is DERIVED, not declared: editing it is impossible
#: without editing the registry declaration it comes from, which is what stops
#: the two from disagreeing again.
DEFAULT_PROVIDER_CAPABILITIES: dict[str, set[str]] = _default_provider_capabilities()

# A stage requires the "highest" capability it cannot complete without. write
# implies read; command implies write+read (enforced in _validate_capabilities).
DEFAULT_TASK_CAPABILITY_REQUIREMENTS: dict[str, set[str]] = {
    "architecture": {CAP_READ},
    "exploration": {CAP_READ},
    "review": {CAP_READ},
    "implementation": {CAP_WRITE},
    "debugging": {CAP_WRITE},
    "testing": {CAP_COMMAND},
    "trivial": set(),
}

# Historical private alias, kept so existing call sites read unchanged. The
# implementation now lives in `capability.py`; this file keeps no copy, because
# two copies of one mapping are free to disagree.
_expand_capabilities = expand_capabilities


@dataclass(frozen=True)
class ProviderConfig:
    """Per-plugin enablement and free-form provider-specific configuration."""

    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    freshness: FreshnessThresholds = FreshnessThresholds()
    task_fit: dict[str, dict[str, float]] | None = None
    sessions_path: Path = field(default_factory=lambda: DEFAULT_SESSIONS_PATH)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    # Human-authored priors for the optional model/reasoning-effort
    # refinement (RouteTarget) layered on top of the provider-only
    # recommendation above -- see default.yaml's own comment for why
    # `model` is deliberately absent from the packaged defaults.
    # provider -> task_type -> {"model": str | None, "reasoning_effort": str | None}
    model_policy: dict[str, dict[str, dict[str, str | None]]] = field(default_factory=dict)
    # Curated, CONFIGURED-tier (not runtime-discovered) model descriptors,
    # consumed by catalog.py -- never scattered provider-specific constants.
    # provider -> tuple of raw descriptor mappings (model_id/display_name/...)
    model_catalog_static: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    # provider -> capabilities it can perform; task -> capabilities it requires.
    # None means "use packaged defaults" (mirrors task_fit's convention).
    provider_capabilities: dict[str, frozenset[str]] | None = None
    task_capability_requirements: dict[str, frozenset[str]] | None = None
    # Canonical capability truth, keyed by provider. Derived in __post_init__
    # from the registry's declarations plus any permission override above --
    # never supplied directly, so it cannot become a competing source.
    provider_capability_profiles: dict[str, CapabilityProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        matrix = self.task_fit if self.task_fit is not None else _copy_default_task_fit()
        _validate_task_fit(matrix)
        object.__setattr__(self, "task_fit", matrix)
        _validate_model_policy(self.model_policy)

        # Overrides MERGE over the packaged defaults (they do not replace them):
        # a partial override -- e.g. only tightening one task's requirement --
        # must never silently drop the other tasks' requirements, which would
        # let a files-only worker be routed to a stage it cannot complete.
        profiles = default_provider_capability_profiles()
        if self.provider_capabilities is not None:
            # Validate the RAW override first. Applying it iterates the known
            # vocabulary, which would quietly discard a misspelled capability
            # instead of telling the user their config does not say what they
            # think it says.
            _validate_capability_names(self.provider_capabilities)
            # A configured set is a statement about PERMISSION, never about what
            # a runtime can do. Reading an omitted capability as "unsupported"
            # would let a local config silently manufacture a false claim about
            # a provider -- the exact defect this milestone removes -- so the
            # override is applied as `allowed` and support is left untouched.
            for provider, allowed in self.provider_capabilities.items():
                profiles[provider] = _apply_permission_override(
                    profiles.get(provider) or CapabilityProfile(provider=provider),
                    expand_capabilities(allowed),
                )
        object.__setattr__(self, "provider_capability_profiles", profiles)

        caps = {p: set(profile.operational_capabilities) for p, profile in profiles.items()}
        reqs = {t: set(c) for t, c in DEFAULT_TASK_CAPABILITY_REQUIREMENTS.items()}
        if self.task_capability_requirements is not None:
            reqs.update({t: set(c) for t, c in self.task_capability_requirements.items()})
        caps, reqs = _validate_capabilities(caps, reqs)
        object.__setattr__(self, "provider_capabilities", caps)
        object.__setattr__(self, "task_capability_requirements", reqs)

    def capability_profile(self, provider: str) -> CapabilityProfile:
        """This provider's canonical capability truth.

        An unconfigured provider gets an empty profile, which answers UNKNOWN
        for every capability rather than declaring it incapable of everything.
        """

        profiles = getattr(self, "provider_capability_profiles", None) or {}
        return profiles.get(provider) or CapabilityProfile(provider=provider)

    def provider_can_perform(self, provider: str, task: str) -> bool:
        """True iff ``provider``'s capabilities satisfy ``task``'s requirements.

        A provider with no configured capability entry is treated as fully
        capable (unknown providers are not silently excluded from routing);
        an unknown task requires nothing.
        """

        assert self.provider_capabilities is not None  # set in __post_init__
        assert self.task_capability_requirements is not None
        have = self.provider_capabilities.get(provider)
        if have is None:
            return True
        need = self.task_capability_requirements.get(task, frozenset())
        return need <= have


def _validate_capability_names(provider_capabilities: Mapping[str, Any]) -> None:
    """Reject unknown capability names before anything normalises them away."""

    for provider, values in provider_capabilities.items():
        if not isinstance(values, (set, frozenset, list, tuple)):
            raise ValueError(
                f"provider_capabilities.{provider} must be a list of capability names"
            )
        unknown = set(values) - ALL_CAPABILITIES
        if unknown:
            raise ValueError(
                f"provider_capabilities.{provider} has unknown capabilities: "
                f"{', '.join(sorted(unknown))}"
            )


def _apply_permission_override(
    profile: CapabilityProfile,
    allowed: frozenset[str],
) -> CapabilityProfile:
    """Re-express a legacy flat capability set as PERMISSION over known support.

    Idempotent: applying the same set twice yields an equal profile, because
    nothing here reads the profile's previous permission state -- only its
    support facts, which the override never touches.
    """

    from .capability import (
        CapabilityFact,
        RestrictionReason,
        VerificationEvidence,
        VerificationState,
    )

    facts: dict[str, CapabilityFact] = {}
    for capability in ALL_CAPABILITIES:
        known = profile.facts.get(capability)
        permitted = capability in allowed
        if known is None and not permitted:
            # Nothing is known and nothing is permitted: say exactly that rather
            # than inventing either half.
            continue
        facts[capability] = CapabilityFact(
            capability=capability,
            supported=known.supported if known is not None else None,
            allowed=permitted,
            restriction_reason=(
                None
                if permitted
                else RestrictionReason(
                    "config_permission_override",
                    "withheld by this installation's provider_capabilities configuration",
                )
            ),
            verification=(
                known.verification if known is not None else VerificationState.UNVERIFIED
            ),
            evidence=known.evidence if known is not None else VerificationEvidence(),
            support_basis=known.support_basis if known is not None else None,
        )
    return CapabilityProfile(provider=profile.provider, facts=facts)


def _copy_default_task_fit() -> dict[str, dict[str, float]]:
    return {task: dict(fits) for task, fits in DEFAULT_TASK_FIT.items()}


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in config file {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping")
    return loaded


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _as_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _validate_task_fit(matrix: Mapping[str, Mapping[str, float]]) -> None:
    expected_tasks = {task.value for task in TaskType}
    missing_tasks = expected_tasks - set(matrix)
    if missing_tasks:
        raise ValueError(f"task_fit is missing task types: {', '.join(sorted(missing_tasks))}")
    for task in expected_tasks:
        fits = matrix[task]
        missing_providers = set(PROVIDERS) - set(fits)
        if missing_providers:
            raise ValueError(f"task_fit.{task} is missing providers: {', '.join(sorted(missing_providers))}")
        for provider in PROVIDERS:
            score = fits[provider]
            if not isinstance(score, (int, float)) or not 0 <= float(score) <= 1:
                raise ValueError(f"task_fit.{task}.{provider} must be between 0 and 1")


def _validate_model_policy(policy: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
    expected_tasks = {task.value for task in TaskType}
    for provider, by_task in policy.items():
        if not isinstance(by_task, Mapping):
            raise ValueError(f"model_policy.{provider} must be a mapping")
        for task, entry in by_task.items():
            if task not in expected_tasks:
                raise ValueError(f"model_policy.{provider}.{task} is not a known task type")
            if not isinstance(entry, Mapping):
                raise ValueError(f"model_policy.{provider}.{task} must be a mapping")
            extra = set(entry) - {"model", "reasoning_effort"}
            if extra:
                raise ValueError(
                    f"model_policy.{provider}.{task} has unknown keys: {', '.join(sorted(extra))}"
                )
            for key in ("model", "reasoning_effort"):
                value = entry.get(key)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"model_policy.{provider}.{task}.{key} must be a string or null")


def _validate_capabilities(
    provider_capabilities: Mapping[str, Any],
    task_requirements: Mapping[str, Any],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """Validate + normalize capability maps, closing each set under implication."""

    caps: dict[str, frozenset[str]] = {}
    for provider, values in provider_capabilities.items():
        if not isinstance(values, (set, frozenset, list, tuple)):
            raise ValueError(f"provider_capabilities.{provider} must be a list of capability names")
        unknown = set(values) - ALL_CAPABILITIES
        if unknown:
            raise ValueError(
                f"provider_capabilities.{provider} has unknown capabilities: {', '.join(sorted(unknown))}"
            )
        caps[str(provider)] = _expand_capabilities(set(values))

    reqs: dict[str, frozenset[str]] = {}
    expected_tasks = {task.value for task in TaskType}
    for task, values in task_requirements.items():
        if task not in expected_tasks:
            raise ValueError(f"task_capability_requirements.{task} is not a known task type")
        if not isinstance(values, (set, frozenset, list, tuple)):
            raise ValueError(f"task_capability_requirements.{task} must be a list of capability names")
        unknown = set(values) - ALL_CAPABILITIES
        if unknown:
            raise ValueError(
                f"task_capability_requirements.{task} has unknown capabilities: {', '.join(sorted(unknown))}"
            )
        reqs[str(task)] = _expand_capabilities(set(values))
    return caps, reqs


def _parse_model_policy(raw: Any) -> dict[str, dict[str, dict[str, str | None]]]:
    mapping = _mapping(raw, "model_policy")
    policy: dict[str, dict[str, dict[str, str | None]]] = {}
    for provider, by_task in mapping.items():
        by_task_mapping = _mapping(by_task, f"model_policy.{provider}")
        policy[str(provider)] = {
            str(task): {
                "model": entry.get("model"),
                "reasoning_effort": entry.get("reasoning_effort"),
            }
            for task, entry in by_task_mapping.items()
        }
    return policy


def _parse_model_catalog_static(raw: Any) -> dict[str, tuple[dict[str, Any], ...]]:
    mapping = _mapping(raw, "model_catalog")
    catalog: dict[str, tuple[dict[str, Any], ...]] = {}
    for provider, entries in mapping.items():
        if not isinstance(entries, list):
            raise ValueError(f"model_catalog.{provider} must be a list")
        catalog[str(provider)] = tuple(dict(entry) for entry in entries)
    return catalog


def load_config(user_config_path: str | Path | None = None) -> Config:
    """Load packaged defaults and optionally merge a user YAML override."""

    default_resource = resources.files("modelquad").joinpath("default.yaml")
    with resources.as_file(default_resource) as default_path:
        raw: dict[str, Any] = _read_yaml(default_path)
    if user_config_path is not None:
        user_raw = _read_yaml(Path(user_config_path).expanduser())
        raw = _deep_merge(raw, user_raw)
        # A pre-v0.5 user override has no providers section. Preserve that
        # distinction so the registry can apply each plugin's default without
        # forcing a migration merely because packaged defaults now document it.
        if "providers" not in user_raw:
            raw.pop("providers", None)

    freshness = _mapping(raw.get("freshness", {}), "freshness")
    task_fit_raw = _mapping(raw.get("task_fit", {}), "task_fit")
    task_fit = {
        str(task): {
            str(provider): _as_float(value, f"task_fit.{task}.{provider}")
            for provider, value in _mapping(fits, f"task_fit.{task}").items()
        }
        for task, fits in task_fit_raw.items()
    }
    sessions_value = raw.get("sessions_path", str(DEFAULT_SESSIONS_PATH))
    if not isinstance(sessions_value, str):
        raise ValueError("sessions_path must be a string path")
    providers_raw = raw.get("providers", {})
    providers_mapping = _mapping(providers_raw, "providers")
    providers: dict[str, ProviderConfig] = {}
    for provider_id, provider_value in providers_mapping.items():
        entry = _mapping(provider_value, f"providers.{provider_id}")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"providers.{provider_id}.enabled must be a boolean")
        providers[str(provider_id)] = ProviderConfig(
            enabled=enabled,
            options={str(key): value for key, value in entry.items() if key != "enabled"},
        )

    return Config(
        freshness=FreshnessThresholds(
            fresh_seconds=int(_as_float(freshness.get("fresh_seconds", 300), "freshness.fresh_seconds")),
            stale_seconds=int(_as_float(freshness.get("stale_seconds", 3600), "freshness.stale_seconds")),
        ),
        task_fit=task_fit,
        sessions_path=Path(sessions_value).expanduser(),
        providers=providers,
        model_policy=_parse_model_policy(raw.get("model_policy", {})),
        model_catalog_static=_parse_model_catalog_static(raw.get("model_catalog", {})),
        provider_capabilities=_parse_capability_map(raw.get("provider_capabilities")),
        task_capability_requirements=_parse_capability_map(raw.get("task_capability_requirements")),
    )


def _parse_capability_map(raw: Any) -> dict[str, frozenset[str]] | None:
    """Parse a {name: [capabilities]} YAML mapping, or None to use defaults."""

    if raw is None:
        return None
    mapping = _mapping(raw, "capabilities")
    result: dict[str, frozenset[str]] = {}
    for name, values in mapping.items():
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"capabilities.{name} must be a list of capability names")
        result[str(name)] = frozenset(str(v) for v in values)
    return result
