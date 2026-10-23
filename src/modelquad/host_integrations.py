# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Persist the user's choice about default global host integrations."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from . import paths
from .adapters.atomicwrite import atomic_write


OFF_DETAIL = "off by choice (modelquad integrations off)"
OFF_GUIDANCE = "Global host integrations are off; run modelquad integrations on to enable them."


def global_integrations_enabled() -> bool:
    """Default to existing on behavior when no readable switch is present."""
    try:
        data = json.loads((paths.state_root() / "host-integrations.json").read_text(encoding="utf-8"))
        return not (
            isinstance(data, dict)
            and data.get("schema") == 1
            and data.get("global_host_integrations") == "off"
        )
    except (OSError, ValueError):
        return True


def set_global_integrations(enabled: bool) -> None:
    """Atomically save intent before any global adapter cleanup occurs."""
    target = paths.state_root() / "host-integrations.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": 1,
        "global_host_integrations": "on" if enabled else "off",
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(target, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
