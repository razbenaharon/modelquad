# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Shared projections of doctor evidence into capabilities and tool connections.

Both `api.py` (serving `/api/providers`) and `history.py` (writing
`provider_health` rows) need the same "is the CLI on PATH / is the adapter
installed" answer for a provider, derived from `doctor.run_doctor()`'s named
checks (e.g. "codex CLI", "codex adapter installed"). Keeping that name
convention in exactly one place means a future rename in `doctor.py` breaks
loudly (a lookup returning `None` everywhere) rather than silently drifting
between two independent copies of the same string interpolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from ..registry import ProviderRegistry


def provider_capabilities(
    checks_by_name: Mapping[str, Any], provider: str
) -> tuple[bool | None, bool | None]:
    """Return (cli_detected, adapter_installed) for one provider, or None if unknown."""

    cli_check = checks_by_name.get(f"{provider} CLI")
    adapter_check = checks_by_name.get(f"{provider} adapter installed")
    cli_detected = None if cli_check is None else cli_check.status.value == "ok"
    adapter_installed = None if adapter_check is None else adapter_check.status.value == "ok"
    return cli_detected, adapter_installed


def host_connections(checks_by_name: Mapping[str, Any], registry: ProviderRegistry) -> dict[str, dict[str, str]]:
    """Project the doctor's adapter/registration/runtime checks for Settings.

    No saved selection can supply evidence. Desktop's adapter check already
    checks its MCP entry; the verified CLI hosts also have a separate check.
    Missing evidence (including a stale report passed as an empty mapping)
    stays unknown. Runtime findings prevent a wrong installation looking ready.
    """
    result = {}
    for plugin in registry.all():
        for host in plugin.host_adapters:
            adapter = checks_by_name.get(f"{host.id} adapter installed")
            registration = checks_by_name.get(f"{host.id} MCP registered")
            runtime = checks_by_name.get(f"{host.id} MCP runtime")
            required = [adapter, runtime]
            if host.verified and host.id in {"claude", "codex"}:
                required.append(registration)
            observed = [check.status.value for check in required if check is not None]
            if any(status in {"warn", "missing"} for status in observed):
                state, headline = "not_connected", "Not connected yet; run Repair to check this tool"
            elif len(observed) != len(required) or any(status != "ok" for status in observed):
                state, headline = "unknown", "Status unknown"
            else:
                state, headline = "connected", "Connected"
            result[host.id] = {"state": state, "headline": headline}
    return result
