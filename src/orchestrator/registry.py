# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Static registry describing the provider plugins known to the orchestrator."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import hostruntime
from .capability import (
    CapabilityGrant,
    CapabilityProfile,
    granted,
    profile_from_declaration,
    restricted,
)

if TYPE_CHECKING:
    from .catalog import ModelDescriptor
    from .config import Config, ProviderConfig
    from .execution.base import Executor
    from .providers.base import Provider


# --- shared worker policy ----------------------------------------------------
# Every restriction below is ModelQuad's decision about what a DELEGATED WORKER
# may do, not a claim about what the runtime can do. Claude and Codex can
# both push a branch; none of them is permitted to. Stating that as a
# restriction rather than an absent capability is the entire point of the
# capability model -- and it is what keeps "broad development capability inside
# a narrow assigned workspace" from ever being read as machine-wide authority.

_OUTSIDE_WORKSPACE = restricted(
    "worker_workspace_boundary",
    "a delegated worker is confined to its assigned workspace",
)
_NO_EXTERNAL_NETWORK = restricted(
    "worker_no_external_network",
    "workers may not reach the network from inside a delegated task",
)
_NO_PUSH = restricted(
    "worker_no_push",
    "publishing a branch is a human-approved act, never a delegated one",
)
_NO_RELEASE = restricted(
    "worker_no_release",
    "tagging, releasing and publishing are never delegated",
)

#: The ordinary development grant: broad capability, narrow workspace.
WORKSPACE_DEVELOPMENT_GRANT: dict[str, CapabilityGrant] = {
    "workspace_read": granted(),
    "workspace_write": granted(),
    "run_tests": granted(),
    "run_builds": granted(),
    "git_read": granted(),
    "external_network": _NO_EXTERNAL_NETWORK,
    "external_write": _OUTSIDE_WORKSPACE,
    "push": _NO_PUSH,
    "release": _NO_RELEASE,
}



#: The declared worker capability of every provider that has one, in one place.
#: `config.py` derives the router's effective gate from this rather than keeping
#: a second table -- the two had already drifted, and the copy the router used
#: carried a claim this repository elsewhere disproved.
DEFAULT_WORKER_CAPABILITY_DECLARATIONS: dict[str, dict[str, CapabilityGrant]] = {
    "claude": dict(WORKSPACE_DEVELOPMENT_GRANT),
    # ModelQuad deliberately starts Codex without its shell tool.  These are
    # restrictions, not absent runtime abilities: callers can explain the
    # boundary accurately and the router will never select Codex for them.
    "codex": {
        **WORKSPACE_DEVELOPMENT_GRANT,
        "workspace_write": restricted("read_only_v1", "v1 runs Codex read-only with its shell tool off"),
        "run_tests": restricted("read_only_v1", "v1 runs Codex read-only with its shell tool off"),
        "run_builds": restricted("read_only_v1", "v1 runs Codex read-only with its shell tool off"),
    },
}

def _profile_to_dict(
    profile: dict[str, CapabilityGrant] | None,
) -> dict[str, Any] | None:
    """Serialise a fine-grained declaration, preserving why anything is denied."""

    if profile is None:
        return None
    return {key: grant.to_dict() for key, grant in profile.items()}


class UsageSourceKind(str, Enum):
    """How a plugin obtains the quota information it exposes."""

    SUBSCRIPTION = "subscription"
    LOCAL_LOGS = "local_logs"
    CLI_STATUS = "cli_status"
    API = "api"
    MANAGEMENT_API = "management_api"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Normalized provider abilities; complementary to the numeric task-fit matrix."""

    planning: bool = False
    architecture: bool = False
    coding: bool = False
    debugging: bool = False
    exploration: bool = False
    research: bool = False
    testing: bool = False
    review: bool = False
    session_resume: bool = False
    cli_execution: bool = False
    api_execution: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderPluginContext:
    """Everything a plugin factory needs to create one runtime instance."""

    config: "Config"
    provider_config: "ProviderConfig"
    usage_directory: "Path | None" = None


@dataclass(frozen=True)
class HostAdapterDescriptor:
    """One user-facing client integration for a provider plugin."""

    id: str
    display_name: str
    provider_id: str
    adapter_cls: type
    default_target_dir: "Callable[[], Path]"
    verified: bool = True
    detect: "Callable[[], bool] | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider_id": self.provider_id,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class ResourcePoolDescriptor:
    """Static description of the paid subscription/quota pool an integration monitors.

    V6: the pool is a first-class resource, distinct from any agent runtime or
    model. ``plan`` is deliberately absent here -- plans are observed facts
    reported by collectors (see models.ResourcePool), never static claims.
    One descriptor per subscription; a subscription whose quota is internally
    split still has one descriptor, with the observed sub-pools surfacing at
    collection time.
    """

    id: str
    provider: str
    display_name: str
    quota_unit: str = "percent"
    usage_source_kind: UsageSourceKind = UsageSourceKind.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "quota_unit": self.quota_unit,
            "usage_source_kind": self.usage_source_kind.value,
        }


@dataclass(frozen=True)
class AgentRuntimeDescriptor:
    """Static description of one agent execution environment (V6: runtime ≠ model ≠ pool).

    A runtime draws capacity from ``resource_pool`` and can run one or more
    models (``supported_models`` for static declarations, ``model_discovery``
    for live/config-derived catalogs; both empty means unknown, never "no
    models"). ``executor_factory`` is ``None`` for host-only runtimes such as
    Claude Code, which the product integrates with but cannot spawn as a
    delegated worker. Nothing here assumes a provider has exactly one runtime:
    a future Copilot plugin declares one pool and several runtime descriptors,
    and a runtime may run models from a different vendor than its own owner.
    """

    id: str
    provider: str
    display_name: str
    resource_pool: str
    cli_executable: str | None = None
    executor_factory: "Callable[[ProviderPluginContext], Executor] | None" = None
    capability_profile: dict[str, CapabilityGrant] | None = None
    supported_models: tuple[str, ...] = ()
    model_discovery: "Callable[[ProviderPluginContext], tuple[ModelDescriptor, ...]] | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "resource_pool": self.resource_pool,
            "cli_executable": self.cli_executable,
            "has_executor": self.executor_factory is not None,
            "capability_profile": _profile_to_dict(self.capability_profile),
            "supported_models": list(self.supported_models),
            "has_model_discovery": self.model_discovery is not None,
        }


@dataclass(frozen=True)
class ProviderInstallInfo:
    """How a user installs this provider's CLI, and where it lands.

    Exists because the product previously had no answer to "how do I get
    this?": a former setup picker said only "Install or sign in to one of these
    tools", which is a dead end for the audience the product targets.

    ``extra_search_dirs`` is the other half, and the more important one. A
    provider CLI is normally found by PATH discovery, but a vendor
    installer can legitimately finish *without* wiring PATH -- Claude Code's
    official Windows installer was observed doing exactly that on a clean
    machine, telling the user to edit System Properties by hand. Detection
    that trusts PATH alone then reports a working install as absent. These are
    the vendor's own documented install locations, checked as a fallback so
    the product describes the machine truthfully.

    ``command`` is a convenience, not an authority: install commands change,
    so ``docs_url`` is always present as the source of truth, and the values
    here must be re-verified against vendor documentation before a release.
    Nothing in the product ever runs these commands -- they are shown for the
    user to copy and run themselves.
    """

    docs_url: str
    command: str | None = None
    command_shell: str = "PowerShell"
    alternative_command: str | None = None
    alternative_label: str | None = None
    extra_search_dirs: "tuple[Callable[[], Path], ...]" = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "docs_url": self.docs_url,
            "command": self.command,
            "command_shell": self.command_shell,
            "alternative_command": self.alternative_command,
            "alternative_label": self.alternative_label,
        }


@dataclass(frozen=True)
class ProviderPlugin:
    """Static description and factories for one provider plugin.

    ``host_adapters`` describes the parent-client integrations belonging to the
    provider. Live doctor health and historical execution performance
    deliberately are not fields here: they are computed at request time by
    ``doctor.py`` and ``service.history`` keyed by ``id``.
    ``provider_factory`` creates the existing ``providers.base.Provider``
    usage-source abstraction; its name predates this plugin split and remains
    part of the public architecture.
    """

    id: str
    display_name: str
    capabilities: ProviderCapabilities
    usage_source_kind: UsageSourceKind
    enabled_by_default: bool = True
    provider_factory: "Callable[[ProviderPluginContext], Provider] | None" = None
    executor_factory: "Callable[[ProviderPluginContext], Executor] | None" = None
    host_adapters: "tuple[HostAdapterDescriptor, ...]" = ()
    limitation: str | None = None
    credential_env_vars: tuple[str, ...] = ()
    # The core executors use CLI names which are not always the provider id.
    # ``None`` means no CLI check.
    cli_executable: str | None = None
    # Optional: returns this provider's model catalog (see catalog.py).
    # ``None`` means no model-catalog concept exists for this provider at
    # all -- distinct from a discovery *attempt* returning an empty tuple.
    # Never required; every existing plugin construction predates this field.
    model_discovery: "Callable[[ProviderPluginContext], tuple[ModelDescriptor, ...]] | None" = None
    # Declarative execution-capability facts (workspace_read, workspace_write,
    # run_tests, run_builds, git_read, external_network, external_write, push,
    # release). Each value is a `CapabilityGrant`, which keeps *what the runtime
    # supports* separate from *what ModelQuad permits* -- a single boolean here
    # is what once made a policy decision indistinguishable from an
    # incapability. `capability_state()` derives the canonical coarse profile
    # routing consumes; the fine-grained declaration is the schema seam for
    # future role-level routing.
    capability_profile: dict[str, CapabilityGrant] | None = None
    # How to install this provider's CLI, and where it lands when PATH is not
    # wired. ``None`` means the product has nothing truthful to offer.
    install: "ProviderInstallInfo | None" = None
    # V6 canonical entities. When omitted they are derived in __post_init__
    # from the legacy single-value fields above -- the ONE documented place
    # where the current integrations' 1:1 subscription/runtime shape is
    # encoded as an integration fact rather than an architectural assumption.
    # A future multi-runtime plugin (e.g. Copilot) passes these explicitly.
    resource_pool: ResourcePoolDescriptor | None = None
    agent_runtimes: "tuple[AgentRuntimeDescriptor, ...]" = ()

    def __post_init__(self) -> None:
        if self.resource_pool is None:
            object.__setattr__(
                self,
                "resource_pool",
                ResourcePoolDescriptor(
                    id=self.id,
                    provider=self.id,
                    display_name=self.display_name,
                    usage_source_kind=self.usage_source_kind,
                ),
            )
        if not self.agent_runtimes:
            object.__setattr__(
                self,
                "agent_runtimes",
                (
                    AgentRuntimeDescriptor(
                        id=self.id,
                        provider=self.id,
                        display_name=self.display_name,
                        resource_pool=self.resource_pool.id,
                        cli_executable=self.cli_executable,
                        executor_factory=self.executor_factory,
                        capability_profile=self.capability_profile,
                        model_discovery=self.model_discovery,
                    ),
                ),
            )

    @property
    def default_runtime(self) -> AgentRuntimeDescriptor:
        """The plugin's first declared runtime -- the target when a caller names only a provider."""

        return self.agent_runtimes[0]

    def capability_state(self) -> CapabilityProfile:
        """The canonical coarse capability truth for this plugin.

        Derived from the fine-grained declaration on every call rather than
        stored, so the registry's declaration and the router's effective gate
        cannot drift apart -- which, before this milestone, they had. A plugin
        that declares nothing yields an empty profile, which answers UNKNOWN
        for every capability rather than declaring the runtime incapable.
        """

        return profile_from_declaration(self.id, self.capability_profile or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "capabilities": self.capabilities.to_dict(),
            "usage_source_kind": self.usage_source_kind.value,
            "enabled_by_default": self.enabled_by_default,
            "has_executor": self.executor_factory is not None,
            "has_usage_source": self.provider_factory is not None,
            "has_adapter": bool(self.host_adapters),
            "host_adapters": [host.to_dict() for host in self.host_adapters],
            "limitation": self.limitation,
            "credential_env_vars": list(self.credential_env_vars),
            "has_model_discovery": self.model_discovery is not None,
            "capability_profile": _profile_to_dict(self.capability_profile),
            "capability_state": self.capability_state().to_dict(),
            "install": self.install.to_dict() if self.install is not None else None,
            "resource_pool": self.resource_pool.to_dict() if self.resource_pool is not None else None,
            "agent_runtimes": [runtime.to_dict() for runtime in self.agent_runtimes],
        }


class ProviderRegistry:
    """Ordered collection of independently-implemented provider plugins."""

    def __init__(self, plugins: Sequence[ProviderPlugin] = ()) -> None:
        self._plugins: list[ProviderPlugin] = []
        self._by_id: dict[str, ProviderPlugin] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: ProviderPlugin) -> None:
        if plugin.id in self._by_id:
            raise ValueError(f"Provider plugin {plugin.id!r} is already registered")
        self._plugins.append(plugin)
        self._by_id[plugin.id] = plugin

    def get(self, provider_id: str) -> ProviderPlugin | None:
        return self._by_id.get(provider_id)

    def all(self) -> tuple[ProviderPlugin, ...]:
        return tuple(self._plugins)

    def enabled(self, config: "Config") -> tuple[ProviderPlugin, ...]:
        enabled_plugins: list[ProviderPlugin] = []
        for plugin in self._plugins:
            configured = config.providers.get(plugin.id)
            if (configured.enabled if configured is not None else plugin.enabled_by_default):
                enabled_plugins.append(plugin)
        return tuple(enabled_plugins)

    def runtimes(self) -> tuple[AgentRuntimeDescriptor, ...]:
        """Every declared agent runtime across all plugins."""

        return tuple(runtime for plugin in self._plugins for runtime in plugin.agent_runtimes)

    def find_runtime(self, runtime_id: str) -> AgentRuntimeDescriptor | None:
        """Look up an agent runtime by its own id, across plugins."""

        for plugin in self._plugins:
            for runtime in plugin.agent_runtimes:
                if runtime.id == runtime_id:
                    return runtime
        return None

    def find_pool(self, pool_id: str) -> ResourcePoolDescriptor | None:
        """Look up a resource pool by its own id, across plugins."""

        for plugin in self._plugins:
            if plugin.resource_pool is not None and plugin.resource_pool.id == pool_id:
                return plugin.resource_pool
        return None

    def default_runtime_for_provider(self, provider_id: str) -> AgentRuntimeDescriptor | None:
        plugin = self._by_id.get(provider_id)
        return plugin.default_runtime if plugin is not None else None


def default_registry() -> ProviderRegistry:
    """Build the built-in registry; future providers are wired in only here."""

    from . import paths
    test_plugin = os.environ.get("MODELQUAD_TEST_EXECUTOR_PLUGIN")
    if (test_plugin
            and (paths.is_dev_checkout()
                 or os.environ.get("MODELQUAD_TEST_EXECUTOR_OK") == "1")):
        import importlib
        module, factory = test_plugin.split(":", 1)
        registry = getattr(importlib.import_module(module), factory)()
        if not isinstance(registry, ProviderRegistry):
            raise ValueError("test executor plugin must return a ProviderRegistry")
        return registry

    # Adapter classes are needed to describe host integrations immediately.
    # Runtime provider and executor implementations remain lazy so resource-only
    # adapter imports do not require optional runtime dependencies.
    # Claude Code is the only host (decision H1): Codex is a worker here, and
    # Claude Desktop is not a supported host.
    from .adapters.claude_adapter import ClaudeAdapter

    def usage_factory(provider_loader: Callable[[], type]):
        def create_usage_provider(ctx: ProviderPluginContext) -> Provider:
            provider_type = provider_loader()
            return provider_type(
                usage_directory=ctx.usage_directory,
                freshness_thresholds=ctx.config.freshness,
            )

        return create_usage_provider

    def claude_provider() -> type:
        from .providers.claude import ClaudeProvider

        return ClaudeProvider

    def codex_provider() -> type:
        from .providers.codex import CodexProvider

        return CodexProvider

    def codex_executor() -> Executor:
        from .execution.codex import CodexExecutor

        return CodexExecutor()

    def claude_model_discovery(ctx: ProviderPluginContext) -> "tuple[ModelDescriptor, ...]":
        from .catalog import discover_claude_models

        return discover_claude_models(ctx)

    def codex_model_discovery(ctx: ProviderPluginContext) -> "tuple[ModelDescriptor, ...]":
        from .catalog import discover_codex_models

        return discover_codex_models(ctx)

    return ProviderRegistry(
        (
            ProviderPlugin(
                id="claude",
                display_name="Claude",
                capabilities=ProviderCapabilities(
                    planning=True,
                    architecture=True,
                    debugging=True,
                    research=True,
                    testing=True,
                    review=True,
                ),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                provider_factory=usage_factory(claude_provider),
                host_adapters=(
                    HostAdapterDescriptor(
                        id="claude",
                        display_name="Claude Code",
                        provider_id="claude",
                        adapter_cls=ClaudeAdapter,
                        default_target_dir=lambda: Path.home() / ".claude",
                        detect=lambda: hostruntime.discovery_which("claude") is not None
                        or (Path.home() / ".claude").is_dir(),
                    ),
                ),
                cli_executable="claude",
                install=ProviderInstallInfo(
                    docs_url="https://code.claude.com/docs/en/setup",
                    command="irm https://claude.ai/install.ps1 | iex",
                    alternative_command="winget install Anthropic.ClaudeCode",
                    alternative_label="WinGet",
                    # The native installer puts claude.exe here and may not add
                    # it to PATH (observed on a clean Windows machine).
                    extra_search_dirs=(lambda: Path.home() / ".local" / "bin",),
                ),
                model_discovery=claude_model_discovery,
                capability_profile=dict(WORKSPACE_DEVELOPMENT_GRANT),
            ),
            ProviderPlugin(
                id="codex",
                display_name="Codex",
                capabilities=ProviderCapabilities(
                    # planning: a full reasoning CLI that can serve as a V5
                    # delegated planner (reason over a DecisionPacket and emit
                    # a structured TaskPlan) -- distinct from the task-fit
                    # matrix, which still ranks who best EXECUTES a stage.
                    planning=True,
                    coding=True,
                    debugging=True,
                    testing=True,
                    session_resume=True,
                    cli_execution=True,
                ),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                provider_factory=usage_factory(codex_provider),
                executor_factory=lambda ctx: codex_executor(),
                host_adapters=(),
                cli_executable="codex",
                install=ProviderInstallInfo(
                    docs_url="https://developers.openai.com/codex/cli/",
                    # The standalone installer reads
                    # RuntimeInformation::OSArchitecture, which Windows
                    # PowerShell 5.1 (.NET Framework) does not expose -- and
                    # 5.1 is the Windows default. Observed failing with
                    # "The property 'OSArchitecture' cannot be found on this
                    # object" on a clean Windows 11 machine, so the shell
                    # requirement is named rather than left to fail.
                    command="irm https://chatgpt.com/codex/install.ps1 | iex",
                    command_shell="PowerShell 7+",
                    alternative_command="npm install -g @openai/codex",
                    alternative_label="npm (needs Node.js 22+)",
                ),
                model_discovery=codex_model_discovery,
                capability_profile=dict(DEFAULT_WORKER_CAPABILITY_DECLARATIONS["codex"]),
            ),
        )
    )


def api_execution_providers(registry: "ProviderRegistry | None" = None) -> tuple[str, ...]:
    """Providers whose work ModelQuad performs by calling an API from this process.

    ``api_execution`` is the registry's own statement that the outbound call
    belongs to *ModelQuad's* process, which is exactly the traffic `netpolicy`
    governs. A vendor CLI ModelQuad launches declares ``cli_execution``
    instead: that call is made by the vendor's program and never reaches the
    gate, so the network policy must not exclude it. A registry fact, never a
    hardcoded provider name -- a second API-executed plugin is governed the
    day it is registered, without changing any caller.
    """

    try:
        active = registry if registry is not None else default_registry()
        return tuple(plugin.id for plugin in active.all() if plugin.capabilities.api_execution)
    except Exception:  # noqa: BLE001 -- a registry failure must not break routing surfaces
        return ()


def is_host_only_runtime(provider: str | None, registry: "ProviderRegistry | None" = None) -> bool:
    """True when this provider's default runtime cannot be spawned as a worker.

    Being un-spawnable is exactly what makes a runtime host-only: the plugin
    declares no ``executor_factory``, so ``ExecutionManager`` never registers
    it. A recommendation for such a runtime means STAY_WITH_HOST -- the host
    keeps the task, and the only way that decision becomes research data is
    the host reporting the outcome back. This is a registry fact, never a
    hardcoded provider name, so a future host-only runtime behaves correctly
    without changing any caller.
    """

    if provider is None:
        return False
    try:
        active = registry if registry is not None else default_registry()
        runtime = active.default_runtime_for_provider(provider)
    except Exception:  # noqa: BLE001 -- a registry failure must not break routing surfaces
        return False
    return runtime is not None and runtime.executor_factory is None
