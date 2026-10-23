# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Normalization for Codex collector snapshots."""

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


# (label, remaining field, used field, reset field). Codex reports both
# windows and names the reset for each; the BINDING one -- the window with
# less left, which is also the one `constrained_remaining` reports as the
# headline -- is the reset a decision must reason about. The 2026-09-06
# dogfood log is the evidence: the five-hour window read 10% and the weekly
# 8%, so the weekly was the constraint and the five-hour reset (17:58Z) was
# not the fact that mattered for new work.
CODEX_WINDOWS = (
    ("five_hour", "five_hour_remaining_percentage", "five_hour_used_percentage", "five_hour_resets_at"),
    ("seven_day", "seven_day_remaining_percentage", "seven_day_used_percentage", "seven_day_resets_at"),
)


class CodexProvider(JsonUsageProvider):
    name = "codex"
    filename = "usage-codex.json"

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
                "provider",
                "plan_type",
                # Both windows' used and remaining capacity travel alongside the
                # headline percentage, exactly as the Claude provider already
                # does. Without them the former Control Center's window breakdown had
                # no numbers to render: `rateLimitWindows` saw the reset keys,
                # took the Claude/Codex branch, found every percentage absent
                # and returned no windows at all -- which is why the Codex card
                # showed a bare headline while Claude showed both windows.
                "five_hour_used_percentage",
                "five_hour_remaining_percentage",
                "seven_day_used_percentage",
                "seven_day_remaining_percentage",
                "five_hour_resets_at",
                "seven_day_resets_at",
                # Codex names the limit it has actually hit. Present only when
                # a limit really is reached; absent stays absent, because "not
                # reported" is not evidence that nothing is throttled.
                "rate_limit_reached_type",
                "collected_at",
                # The app-server path reports every rate-limit window it saw,
                # not only the two named slots; the pool keeps them all (V6:
                # preserve provider-native quota facts).
                "windows",
                "source",
                "refresh_kind",
            )
            if key in payload
        }
        plan_raw = payload.get("plan_type")
        plan = plan_raw if isinstance(plan_raw, str) and plan_raw else None
        if not available:
            return self._status(
                remaining_percentage=None,
                observed_at=observation_from(payload),
                available=False,
                metadata=metadata,
                reason=reason or "Provider explicitly reported itself unavailable.",
                plan=plan,
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
            plan=plan,
            capacity=binding_window_capacity(
                payload,
                CODEX_WINDOWS,
                observed_at=observed_at,
                freshness=freshness_for(observed_at, self.freshness_thresholds, self._now()),
            ),
        )
