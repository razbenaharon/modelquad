# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Centralized authority for user-facing routing mode policy and pause enforcement.

Routing is automatic. Retired saved modes are normalized by
service/history.py before policy evaluation. Unknown controls still refuse.
Pausing blocks all NEW provider work, including explicit and approved requests.
It does not stop existing workers. Host disconnect controls remain independent.

CRITICAL: Hosts and UI layers must NEVER re-implement, bypass, or duplicate this logic.
This module is the single source of truth for routing mode and pause decision semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Persistence vocabulary for disconnected-Host settings.
VALID_HOST_IDS = frozenset({"claude", "codex"})


@dataclass(frozen=True)
class DelegationPolicyDecision:
    """Outcome of a delegation policy evaluation."""

    allowed: bool
    routing_mode: str | None
    needs_user_approval: bool = False
    reason: str | None = None
    routing_paused: bool | None = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "routing_mode": self.routing_mode,
            "needs_user_approval": self.needs_user_approval,
            "reason": self.reason,
            "routing_paused": self.routing_paused,
        }


def check_delegation_policy(
    *,
    routing_mode: str | None,
    invocation_mode: str | None,
    user_approved: bool = False,
    routing_paused: bool | None = False,
    disconnected_hosts: list[str] | tuple[str, ...] | None = (),
    caller_host: str | None = None,
    routing_paused_since: str | None = None,
) -> DelegationPolicyDecision:
    """Evaluate whether a delegation is permitted under the active routing mode and pause state.

    Precedence:
    1. Unknown/invalid controls or a pause refuse all new provider work.
    2. Disconnected Hosts and unidentified callers while any Host is disconnected refuse.
    3. Automatic routing is allowed.
    """
    valid_hosts = isinstance(disconnected_hosts, (list, tuple)) and all(
        isinstance(host, str) and host in VALID_HOST_IDS for host in disconnected_hosts
    )
    if not valid_hosts or routing_mode != "automatic" or type(routing_paused) is not bool:
        return DelegationPolicyDecision(
            allowed=False, routing_mode=routing_mode, routing_paused=routing_paused,
            reason="launch_state_unknown: routing controls are unavailable or invalid",
        )
    if routing_paused:
        return DelegationPolicyDecision(
            allowed=False,
            routing_mode=routing_mode,
            needs_user_approval=False,
            reason="routing is paused; no new provider work may start until routing is resumed; " + pause_label(routing_paused_since),
            routing_paused=routing_paused,
        )

    active_disconnected = list(disconnected_hosts)
    if active_disconnected:
        reason = None
        if caller_host not in VALID_HOST_IDS:
            reason = "host_unidentified: a tool is disconnected and the calling Host could not be identified"
        elif caller_host in active_disconnected:
            reason = "host_disconnected: this tool cannot start new work through ModelQuad until reconnected"
        if reason:
            return DelegationPolicyDecision(
                allowed=False, routing_mode=routing_mode, routing_paused=routing_paused,
                reason=reason,
            )

    # "automatic"
    return DelegationPolicyDecision(
        allowed=True,
        routing_mode=routing_mode,
        routing_paused=routing_paused,
    )


class LaunchRefused(RuntimeError):
    """Coarse, content-free refusal at the last provider admission seam."""


def pause_label(stamp: str | None) -> str:
    try:
        if type(stamp) is str and len(stamp) <= 64:
            value = datetime.fromisoformat(stamp)
            if value.tzinfo is not None:
                return 'Paused since ' + value.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        pass
    return 'Pause time unknown'
