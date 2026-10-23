# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Registry of supported parent-client adapter implementations."""

from __future__ import annotations

from ..registry import HostAdapterDescriptor, default_registry


CLIENT_NAMES: tuple[str, ...] = tuple(
    host.id for plugin in default_registry().all() for host in plugin.host_adapters
)


def get_adapter(client: str) -> object:
    """Construct an adapter for one supported parent client."""

    return get_host_descriptor(client).adapter_cls()


def get_host_descriptor(client: str) -> HostAdapterDescriptor:
    """Return the full descriptor for one supported parent client."""

    for plugin in default_registry().all():
        for host in plugin.host_adapters:
            if host.id == client:
                return host
    choices = ", ".join(CLIENT_NAMES)
    raise ValueError(f"Unknown adapter client {client!r}; choose one of: {choices}")
