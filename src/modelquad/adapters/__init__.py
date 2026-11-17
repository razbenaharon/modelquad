# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Client adapter installation helpers.

Exports load on first use, so a hook that needs one helper module (the
status line's atomic write, the gate) does not import the adapter registry.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .paths import default_target_dir
    from .registry import (
        CLIENT_NAMES,
        get_adapter,
        get_host_descriptor,
    )
    from .report import (
        ActionKind,
        FileAction,
        InstallReport,
    )
    from .registry import HostAdapterDescriptor

_EXPORTS = {
    "default_target_dir": ".paths",
    "CLIENT_NAMES": ".registry",
    "get_adapter": ".registry",
    "get_host_descriptor": ".registry",
    "ActionKind": ".report",
    "FileAction": ".report",
    "InstallReport": ".report",
    "HostAdapterDescriptor": ".registry",
}

__all__ = [
    "ActionKind",
    "CLIENT_NAMES",
    "FileAction",
    "HostAdapterDescriptor",
    "InstallReport",
    "default_target_dir",
    "get_adapter",
    "get_host_descriptor",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
