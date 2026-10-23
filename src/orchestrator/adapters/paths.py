# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Paths for repository-owned adapter templates and client target directories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def _client_names() -> tuple[str, ...]:
    """Read adapter-capable ids from the single registry source of truth."""

    from .registry import CLIENT_NAMES

    return CLIENT_NAMES


def repo_root() -> Path:
    """Return the checkout root containing the repository-level adapters directory."""

    return Path(__file__).resolve().parents[3]


@contextmanager
def adapters_template_dir(client: str) -> Iterator[Path]:
    """Yield one known client's packaged template directory."""

    client_names = _client_names()
    if client not in client_names:
        choices = ", ".join(client_names)
        raise ValueError(f"Unknown adapter client {client!r}; choose one of: {choices}")
    template_resource = resources.files("orchestrator.adapters") / "templates" / client
    with resources.as_file(template_resource) as template_dir:
        yield template_dir


def default_target_dir(client: str) -> Path:
    """Return the conventional client target path at call time."""

    from .registry import get_host_descriptor

    return get_host_descriptor(client).default_target_dir()
