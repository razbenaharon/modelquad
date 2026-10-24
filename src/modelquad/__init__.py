# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""ModelQuad: read-only Codex second opinions and quota display for Claude Code."""

__all__ = ["Config", "Freshness", "ProviderStatus", "Router", "TaskType", "load_config"]
__version__ = "0.3.0"


def __getattr__(name: str):
    """Load the public routing API only when a caller requests it.

    Resource-only imports such as ``modelquad.adapters.paths`` must remain
    usable from a wheel installed with ``--no-deps`` so adapter templates can
    be inspected before the optional routing dependencies are needed.
    """

    if name in {"Config", "load_config"}:
        from .config import Config, load_config

        return {"Config": Config, "load_config": load_config}[name]
    if name in {"Freshness", "ProviderStatus", "TaskType"}:
        from .models import Freshness, ProviderStatus, TaskType

        return {
            "Freshness": Freshness,
            "ProviderStatus": ProviderStatus,
            "TaskType": TaskType,
        }[name]
    if name == "Router":
        from .router import Router

        return Router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
