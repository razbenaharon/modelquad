# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Collection and transformation helpers for provider usage snapshots.

Exports load on first use, so the status-line hook does not import the
refresh machinery.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import (
        observation_epoch,
        read_usage_snapshot,
        write_usage_snapshot,
        write_usage_snapshot_if_newer,
    )
    from .claude import (
        parse_claude_statusline_payload,
        retain_last_known_claude,
    )
    from .codex_appserver import (
        CODEX_APPSERVER_SOURCE,
        collect_codex_appserver_usage,
        parse_rate_limits_result,
    )
    from .refresh import (
        RefreshResult,
        refresh_all,
        refresh_provider,
    )

_EXPORTS = {
    "observation_epoch": ".base",
    "read_usage_snapshot": ".base",
    "write_usage_snapshot": ".base",
    "write_usage_snapshot_if_newer": ".base",
    "parse_claude_statusline_payload": ".claude",
    "retain_last_known_claude": ".claude",
    "CODEX_APPSERVER_SOURCE": ".codex_appserver",
    "collect_codex_appserver_usage": ".codex_appserver",
    "parse_rate_limits_result": ".codex_appserver",
    "RefreshResult": ".refresh",
    "refresh_all": ".refresh",
    "refresh_provider": ".refresh",
}

__all__ = [
    "CODEX_APPSERVER_SOURCE",
    "RefreshResult",
    "collect_codex_appserver_usage",
    "observation_epoch",
    "parse_claude_statusline_payload",
    "parse_rate_limits_result",
    "read_usage_snapshot",
    "refresh_all",
    "refresh_provider",
    "retain_last_known_claude",
    "write_usage_snapshot",
    "write_usage_snapshot_if_newer",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
