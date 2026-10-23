# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Read-only provider usage snapshot implementations.

Exports load on first use, so reading a usage file does not import the
provider factory, its registry or the YAML configuration loader.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import Provider
    from .claude import ClaudeProvider
    from .codex import CodexProvider
    from .factory import build_providers

_EXPORTS = {
    "Provider": ".base",
    "ClaudeProvider": ".claude",
    "CodexProvider": ".codex",
    "build_providers": ".factory",
}

__all__ = [
    "ClaudeProvider",
    "CodexProvider",
    "Provider",
    "build_providers",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
