# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Shared factory for registry-configured provider snapshot readers.

Lives in `providers/` (not `cli.py`) so other top-level entry points --
the MCP server and the local HTTP service -- can build providers without
depending on the CLI module.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config, ProviderConfig
from ..registry import ProviderPluginContext, ProviderRegistry, default_registry


def build_providers(
    config: Config,
    usage_directory: Path | None = None,
    *,
    registry: ProviderRegistry | None = None,
):
    """Build enabled provider usage sources in registry registration order."""

    active_registry = registry or default_registry()
    providers = []
    for plugin in active_registry.enabled(config):
        if plugin.provider_factory is None:
            continue
        context = ProviderPluginContext(
            config=config,
            provider_config=config.providers.get(plugin.id, ProviderConfig()),
            usage_directory=usage_directory,
        )
        providers.append(plugin.provider_factory(context))
    return tuple(providers)
