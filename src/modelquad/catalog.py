# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Provider model catalog: discovered facts, configured priors, and unknowns.

Three deliberately distinct notions of "we know about this model" live here,
per ``AvailabilityState``. Nothing in this module invents data: a model only
becomes ``KNOWN`` when a real, live command reported it just now (no supported
provider has such a command today); a curated list read from packaged config or
a local per-user config file is ``CONFIGURED`` -- a prior, not a verified
fact; a discovery attempt that failed or isn't supported is ``UNVERIFIED``,
never silently treated as "no models exist". Discovery failure here must
never be conflated with a provider being unavailable -- see doctor.py, which
reports model-catalog freshness as an independent signal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .registry import ProviderPluginContext


class AvailabilityState:
    """String constants for a model descriptor's provenance/trust tier."""

    KNOWN = "known"
    CONFIGURED = "configured"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"

    _ALL = frozenset({KNOWN, CONFIGURED, UNVERIFIED, UNKNOWN})


@dataclass(frozen=True)
class ModelDescriptor:
    """One model entry, honest about how confidently it is known.

    ``relative_strength``/``relative_cost`` are explicitly human-authored
    priors when present -- never fabricated empirical measurements. Leave
    them ``None`` rather than guess a number with no backing data.

    ``provider`` is the integration namespace the model was discovered
    through; ``agent_runtime`` names the specific runtime it is reachable via
    when that is narrower than the provider's default (V6: a runtime may run
    models from several vendors, and the same model family may be reachable
    through several runtimes -- each such reachability is its own
    descriptor). ``None`` means the provider's default runtime.
    """

    provider: str
    model_id: str
    display_name: str
    availability: str = AvailabilityState.UNKNOWN
    capabilities: tuple[str, ...] = ()
    relative_strength: float | None = None
    relative_cost: float | None = None
    speed_class: str | None = None
    supported_reasoning_levels: tuple[str, ...] = ()
    source: str = "unknown"
    freshness: datetime | None = None
    agent_runtime: str | None = None

    def __post_init__(self) -> None:
        if self.availability not in AvailabilityState._ALL:
            raise ValueError(f"Unknown availability state: {self.availability!r}")
        if self.relative_strength is not None and not 0 <= self.relative_strength <= 1:
            raise ValueError("relative_strength must be between 0 and 1")
        if self.relative_cost is not None and self.relative_cost < 0:
            raise ValueError("relative_cost cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "availability": self.availability,
            "capabilities": list(self.capabilities),
            "relative_strength": self.relative_strength,
            "relative_cost": self.relative_cost,
            "speed_class": self.speed_class,
            "supported_reasoning_levels": list(self.supported_reasoning_levels),
            "source": self.source,
            "freshness": self.freshness.isoformat() if self.freshness else None,
            "agent_runtime": self.agent_runtime,
        }


@dataclass(frozen=True)
class ModelCatalog:
    """A read-only collection of model descriptors, optionally multi-provider."""

    descriptors: tuple[ModelDescriptor, ...] = field(default_factory=tuple)

    def for_provider(self, provider: str) -> tuple[ModelDescriptor, ...]:
        return tuple(d for d in self.descriptors if d.provider == provider)

    def for_runtime(self, runtime_id: str, provider: str | None = None) -> tuple[ModelDescriptor, ...]:
        """Models reachable through one agent runtime.

        A descriptor without an explicit ``agent_runtime`` belongs to its
        provider's default runtime, so it matches when ``provider`` (the
        runtime's owner) is supplied and equal.
        """

        return tuple(
            d
            for d in self.descriptors
            if d.agent_runtime == runtime_id
            or (d.agent_runtime is None and provider is not None and d.provider == provider)
        )

    def to_dict(self) -> dict[str, Any]:
        return {"models": [d.to_dict() for d in self.descriptors]}


def _read_codex_configured_model(config_path: Path) -> str | None:
    # Use the scalar-only parser shared by host observation and review entry.
    from .codex_config import read_config
    return read_config(config_path)[0]


def configured_codex_models(codex_home: Path | None = None) -> tuple[ModelDescriptor, ...]:
    """The user's own local Codex default model, if their config declares one.

    A plain read of ``CODEX_HOME/config.toml`` or ``~/.codex/config.toml`` -- no process is spawned, and
    nothing is invented when the file is absent or names no model.
    """

    from .codex_config import config_path
    path = config_path(codex_home)
    configured_model = _read_codex_configured_model(path)
    if not configured_model:
        return ()
    return (
        ModelDescriptor(
            provider="codex",
            model_id=configured_model,
            display_name=configured_model,
            availability=AvailabilityState.CONFIGURED,
            supported_reasoning_levels=("minimal", "low", "medium", "high", "xhigh"),
            source=str(path),
        ),
    )


def cached_catalog(
    config: Any,
    *,
    codex_home: Path | None = None,
) -> ModelCatalog:
    """Every descriptor already on disk or in config, WITHOUT probing anything.

    Routing needs the catalog to recommend a model and clamp a reasoning effort
    (without one the recommendation columns stayed empty on every production
    path). What routing must never do is spawn a
    provider CLI to get it -- a route consultation would then cost a process
    per call.

    So this is the read-only projection of the same sources
    ``discover_*_models`` use, and nothing else:

    * Codex: the user's own ``~/.codex/config.toml`` default model, else the
      configured static entries -- the same either/or the discovery function
      applies, so the two paths cannot disagree about which model is "the"
      Codex one;
    * every provider with configured ``model_catalog`` entries, including
      Claude, which has no discovery command at all.

    Descriptors already contributed by an earlier source win: a live-discovered
    (KNOWN) or locally-configured model is a better fact than a packaged prior,
    and the provenance on each descriptor still says which it was.
    """

    static = getattr(config, "model_catalog_static", {}) or {}
    descriptors: list[ModelDescriptor] = []
    seen: set[tuple[str, str]] = set()
    providers_done: set[str] = set()

    def add(items: "tuple[ModelDescriptor, ...]") -> None:
        for descriptor in items:
            key = (descriptor.provider, descriptor.model_id)
            if key in seen:
                continue
            seen.add(key)
            descriptors.append(descriptor)

    def source(label: str, load: "Callable[[], tuple[ModelDescriptor, ...]]") -> tuple[ModelDescriptor, ...]:
        """One catalog source, isolated. A bad source costs only itself.

        Routing calls this function unconditionally now, so any exception a
        source can raise -- a config file that is not UTF-8, a hand-edited
        entry with the wrong shape, a cache in an unexpected state -- would be
        an exception out of ROUTING. The truthful
        answer to "this source could not be read" is no descriptors from it,
        which routing already handles as a missing catalog: no model claim is
        made and the runtime default applies.
        """

        try:
            return load()
        except Exception:
            _LOGGER.warning("Ignoring unusable model catalog source %s", label, exc_info=True)
            return ()

    codex_local = source("codex local config", lambda: configured_codex_models(codex_home))
    if codex_local:
        # Mirrors discover_codex_models: a declared local default REPLACES the
        # packaged list rather than joining it, so the strongest-model choice
        # is not decided by a prior the user has already overridden.
        add(codex_local)
        providers_done.add("codex")
    try:
        provider_names = sorted(str(name) for name in static)
    except Exception:
        _LOGGER.warning("Ignoring unusable static model catalog", exc_info=True)
        provider_names = []
    for provider in provider_names:
        if provider in providers_done:
            continue
        add(source(f"{provider} static catalog", lambda p=provider: _static_from_config(config, p)))
    return ModelCatalog(descriptors=tuple(descriptors))


def discover_codex_models(
    context: "ProviderPluginContext",
    *,
    codex_home: Path | None = None,
) -> tuple[ModelDescriptor, ...]:
    """Codex has no `models` discovery subcommand.

    Reads the user's own local ``~/.codex/config.toml`` default `model` when
    present -- a real local fact, not an invented global list -- and
    otherwise falls back to ``Config.model_catalog_static["codex"]`` (empty
    by default; see default.yaml's own comment on why no static Codex
    entries are shipped). Both tiers are ``CONFIGURED``, never ``KNOWN``:
    neither is a live, verified catalog.
    """

    local = configured_codex_models(codex_home)
    if local:
        return local
    return _static_descriptors(context, "codex")


def discover_claude_models(context: "ProviderPluginContext") -> tuple[ModelDescriptor, ...]:
    """Claude has no model-listing CLI command; return the curated static list."""

    return _static_descriptors(context, "claude")


def _static_descriptors(context: "ProviderPluginContext", provider: str) -> tuple[ModelDescriptor, ...]:
    return _static_from_config(context.config, provider)


def _static_from_config(config: Any, provider: str) -> tuple[ModelDescriptor, ...]:
    entries = (getattr(config, "model_catalog_static", {}) or {}).get(provider, ())
    descriptors: list[ModelDescriptor] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        model_id = entry.get("model_id")
        if not model_id:
            continue
        # One malformed entry costs that entry, never the catalog. A configured
        # list is hand-edited YAML, so `supported_reasoning_levels: null` (a
        # `TypeError` from `tuple(None)`) or a bare string where a list belongs
        # is an ordinary authoring mistake -- and routing now reads this on
        # every call, so raising here crashed the route rather than the entry.
        try:
            descriptors.append(
                ModelDescriptor(
                    provider=provider,
                    model_id=str(model_id),
                    display_name=str(entry.get("display_name", model_id)),
                    availability=AvailabilityState.CONFIGURED,
                    capabilities=_string_tuple(entry.get("capabilities")),
                    relative_strength=entry.get("relative_strength"),
                    relative_cost=entry.get("relative_cost"),
                    speed_class=entry.get("speed_class"),
                    supported_reasoning_levels=_string_tuple(
                        entry.get("supported_reasoning_levels")
                    ),
                    source="default.yaml",
                )
            )
        except Exception:
            _LOGGER.warning(
                "Ignoring malformed %s model catalog entry %r", provider, model_id, exc_info=True
            )
    return tuple(descriptors)


def _string_tuple(value: Any) -> tuple[str, ...]:
    """A configured list of names, or an empty one -- never a mangled string.

    ``tuple("high")`` is ``('h', 'i', 'g', 'h')``: a scalar where a list
    belongs would silently become four nonsense capability/effort names rather
    than an obvious mistake, so a non-sequence yields nothing at all and the
    descriptor simply declares no vocabulary.
    """

    if value is None or isinstance(value, (str, bytes, Mapping)):
        return ()
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return ()
