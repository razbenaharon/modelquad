# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Normalization for Claude collector snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import (
    JsonUsageProvider,
    binding_window_capacity,
    constrained_remaining,
    explicit_availability,
    freshness_for,
    observation_from,
)
from ..models import ProviderStatus


# (label, remaining field, used field, reset field) for the two windows the
# Claude statusline reports. Both resets were already preserved in metadata;
# this is what turns them into the structured CapacityState the router and
# `reasoning/evidence.py` read.
CLAUDE_WINDOWS = (
    ("five_hour", "five_hour_remaining_percentage", "five_hour_used_percentage", "five_hour_resets_at"),
    ("seven_day", "seven_day_remaining_percentage", "seven_day_used_percentage", "seven_day_resets_at"),
)


class ClaudeProvider(JsonUsageProvider):
    name = "claude"
    filename = "usage-claude.json"

    def _parse_payload(self, payload: Mapping[str, Any]) -> ProviderStatus:
        remaining = constrained_remaining(
            payload,
            (
                ("five_hour_remaining_percentage", "five_hour_used_percentage"),
                ("seven_day_remaining_percentage", "seven_day_used_percentage"),
            ),
        )
        available = explicit_availability(payload)
        supplied_reason = payload.get("reason") or payload.get("error")
        reason = supplied_reason if isinstance(supplied_reason, str) else None
        metadata = {
            key: payload[key]
            for key in (
                "five_hour_used_percentage",
                "seven_day_used_percentage",
                # Both windows' remaining capacity travels alongside the used
                # figures so evidence and API clients can tell "plenty
                # left right now, almost none left this week" from its inverse.
                # Collapsing to one headline percentage cannot express that.
                "five_hour_remaining_percentage",
                "seven_day_remaining_percentage",
                # Provider-native facts previously dropped at this layer; a
                # resource pool owns its raw quota facts (V6), so reset times
                # and collection provenance now survive normalization.
                "five_hour_resets_at",
                "seven_day_resets_at",
                "org",
                "source",
                "refresh_kind",
                "sample_count",
            )
            if key in payload
        }
        if not available:
            return self._status(
                remaining_percentage=None,
                observed_at=observation_from(payload),
                available=False,
                metadata=metadata,
                reason=reason or "Provider explicitly reported itself unavailable.",
            )
        if remaining is None and reason is None:
            reason = "Usage data has no valid five-hour or seven-day percentage."
        observed_at = observation_from(payload)
        return self._status(
            remaining_percentage=remaining,
            observed_at=observed_at,
            available=available,
            metadata=metadata,
            reason=reason,
            capacity=binding_window_capacity(
                payload,
                CLAUDE_WINDOWS,
                observed_at=observed_at,
                freshness=freshness_for(observed_at, self.freshness_thresholds, self._now()),
            ),
        )
