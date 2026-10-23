# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from orchestrator.config import Config, ProviderConfig
from orchestrator.registry import (
    HostAdapterDescriptor,
    ProviderCapabilities,
    ProviderPlugin,
    ProviderRegistry,
    UsageSourceKind,
    default_registry,
)


def _plugin(identifier: str, *, enabled: bool = True) -> ProviderPlugin:
    return ProviderPlugin(
        id=identifier,
        display_name=identifier.title(),
        capabilities=ProviderCapabilities(coding=True),
        usage_source_kind=UsageSourceKind.API,
        enabled_by_default=enabled,
        provider_factory=lambda context: object(),
    )


def test_registry_registration_lookup_order_and_duplicate_rejection():
    first = _plugin("first")
    second = _plugin("second")
    registry = ProviderRegistry((first, second))

    assert registry.get("first") is first
    assert registry.get("missing") is None
    assert registry.all() == (first, second)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_plugin("first"))


def test_registry_enabled_honors_defaults_and_explicit_overrides():
    default_on = _plugin("default_on", enabled=True)
    default_off = _plugin("default_off", enabled=False)
    registry = ProviderRegistry((default_on, default_off))

    assert [plugin.id for plugin in registry.enabled(Config())] == ["default_on"]
    config = Config(
        providers={
            "default_on": ProviderConfig(enabled=False),
            "default_off": ProviderConfig(enabled=True),
            "unknown": ProviderConfig(enabled=True),
        }
    )
    assert [plugin.id for plugin in registry.enabled(config)] == ["default_off"]


def test_plugin_and_capabilities_serialization_exposes_static_safe_shape_only():
    plugin = ProviderPlugin(
        id="synthetic",
        display_name="Synthetic",
        capabilities=ProviderCapabilities(coding=True, api_execution=True),
        usage_source_kind=UsageSourceKind.API,
        provider_factory=lambda context: object(),
        credential_env_vars=("SYNTHETIC_API_TOKEN",),
    )

    assert ProviderCapabilities(coding=True).to_dict() == {
        "planning": False,
        "architecture": False,
        "coding": True,
        "debugging": False,
        "exploration": False,
        "research": False,
        "testing": False,
        "review": False,
        "session_resume": False,
        "cli_execution": False,
        "api_execution": False,
    }
    payload = plugin.to_dict()
    assert payload["id"] == "synthetic"
    assert payload["capabilities"]["api_execution"] is True
    assert payload["has_usage_source"] is True
    assert payload["has_executor"] is False
    assert payload["has_adapter"] is False
    assert payload["host_adapters"] == []
    assert payload["credential_env_vars"] == ["SYNTHETIC_API_TOKEN"]
    assert "SYNTHETIC_API_TOKEN=value" not in repr(payload)
    assert not {"api_key", "secret", "token_value", "credential_value"} & set(payload)


def test_registry_accepts_a_test_local_fourth_plugin_without_special_cases():
    fourth = _plugin("fourth")
    registry = ProviderRegistry((_plugin("one"), _plugin("two"), _plugin("three"), fourth))

    assert [plugin.id for plugin in registry.all()] == ["one", "two", "three", "fourth"]
    assert registry.get("fourth").provider_factory is not None
    assert registry.get("fourth").executor_factory is None


def test_default_registry_describes_each_host_adapter_independently():
    registry = default_registry()
    hosts = [host for plugin in registry.all() for host in plugin.host_adapters]

    assert all(isinstance(host, HostAdapterDescriptor) for host in hosts)
    assert [
        (host.id, host.display_name, host.provider_id, host.verified)
        for host in hosts
    ] == [
        ("claude", "Claude Code", "claude", True),
    ]
    claude = registry.get("claude")
    assert claude is not None
    assert claude.to_dict()["host_adapters"] == [host.to_dict() for host in claude.host_adapters]


def test_host_detection_is_lazy_and_uses_runtime_path_and_home_probes(tmp_path, monkeypatch):
    import orchestrator.registry as registry_module

    probes: list[str] = []
    monkeypatch.setattr(registry_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        registry_module.hostruntime,
        "discovery_which",
        lambda executable: probes.append(executable) or None,
    )
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    registry = default_registry()
    hosts = {host.id: host for plugin in registry.all() for host in plugin.host_adapters}

    assert probes == []
    assert set(hosts) == {"claude"}
    assert hosts["claude"].detect is not None and hosts["claude"].detect() is False
    assert probes == ["claude"]

    (tmp_path / ".claude").mkdir()
    assert hosts["claude"].detect()


def test_unknown_provider_name_is_refused(tmp_path):
    """A provider the registry does not know is refused before any worker starts."""
    from orchestrator.config import load_config
    from orchestrator.execution.base import WorkerTaskStatus
    from orchestrator.execution.manager import ExecutionManager

    unknown = "retired-tool"
    registry = default_registry()
    assert registry.get(unknown) is None
    assert unknown not in {plugin.id for plugin in registry.enabled(load_config())}

    manager = ExecutionManager(load_config(), health_db_path=tmp_path / "history.db")
    assert unknown not in manager.executors
    result = manager.delegate("review", "read the code", tmp_path, provider=unknown)

    assert result.success is False
    assert result.task_status is WorkerTaskStatus.BLOCKED
    assert result.task_status_reason == "provider_not_registered"
