# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The one client ModelQuad installs into: Claude Code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class HostAdapterDescriptor:
    """How to find and install into one client."""

    id: str
    display_name: str
    adapter_cls: type
    default_target_dir: Callable[[], Path]
    detect: Callable[[], bool] | None = None


def _claude() -> HostAdapterDescriptor:
    from .. import hostruntime
    from .claude_adapter import ClaudeAdapter

    return HostAdapterDescriptor(
        id="claude", display_name="Claude Code", adapter_cls=ClaudeAdapter,
        default_target_dir=lambda: Path.home() / ".claude",
        detect=lambda: hostruntime.discovery_which("claude") is not None
        or (Path.home() / ".claude").is_dir())


CLIENT_NAMES: tuple[str, ...] = ("claude",)


def get_adapter(client: str) -> object:
    """Construct the adapter for the client."""

    return get_host_descriptor(client).adapter_cls()


def get_host_descriptor(client: str) -> HostAdapterDescriptor:
    if client != "claude":
        raise ValueError(f"Unknown adapter client {client!r}; choose one of: {', '.join(CLIENT_NAMES)}")
    return _claude()
