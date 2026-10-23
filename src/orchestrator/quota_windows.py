# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Pure, display-only views of the quota windows a provider reported."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .models import CapacityState, Freshness, ProviderStatus

def _window(status: ProviderStatus, label: str, data: Mapping[str, Any]) -> CapacityState:
    from .providers.base import (
        finite_float, parse_percentage, parse_timestamp, reset_at_from_delay,
    )

    remaining = parse_percentage(data.get("remaining_percentage"))
    if remaining is None:
        used = parse_percentage(data.get("used_percentage"))
        remaining = None if used is None else 100.0 - used
    if remaining is None:
        fraction = finite_float(data.get("remaining_fraction"))
        remaining = None if fraction is None else parse_percentage(fraction * 100)
    reset = parse_timestamp(data.get("reset_time", data.get("resets_at")))
    if reset is None:
        reset = reset_at_from_delay(status.observed_at, data.get("reset_in_seconds"))
    return CapacityState(
        remaining_percentage=remaining,
        reset_at=reset,
        window=label,
        freshness=status.freshness,
        collected_at=status.observed_at,
    )


def _buckets(status: ProviderStatus) -> Mapping[str, Any]:
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    buckets = metadata.get("quota")
    if not isinstance(buckets, Mapping) or not buckets:
        buckets = metadata.get("buckets")
    return buckets if isinstance(buckets, Mapping) else {}


def quota_view(status: ProviderStatus, *, now: datetime | None = None) -> ProviderStatus:
    """Normalize a status for display; preserve raw facts for independent windows.

    An observation time that cannot be trusted (unparseable, or in the future)
    makes the whole reading UNKNOWN rather than fresh.
    """
    if not isinstance(status.metadata, Mapping):
        status = replace(status, metadata={})
    if now is not None and status.observed_at is not None:
        from .usage.base import valid_timestamp_epoch

        if valid_timestamp_epoch(status.observed_at, now=now) is None:
            status = replace(status, freshness=Freshness.UNKNOWN)
    reference = now or datetime.now(timezone.utc)
    # A reset deadline in the past no longer describes the reported window.
    # Preserve the source facts but do not present stale capacity as fresh.
    for window in reported_windows(status):
        if window.reset_at is not None and window.reset_at <= reference:
            return replace(status, remaining_percentage=None, freshness=Freshness.UNKNOWN,
                           reason="quota window reset has passed")
    return status


def reported_windows(status: ProviderStatus) -> tuple[CapacityState, ...]:
    """Every reported window, including unreadable ones, with its own reset."""
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    buckets = _buckets(status)
    if buckets:
        return tuple(
            _window(status, str(label), data if isinstance(data, Mapping) else {})
            for label, data in sorted(buckets.items(), key=lambda pair: str(pair[0]))
        )
    native = metadata.get("windows")
    if isinstance(native, list) and native:
        # Native windows are authoritative; null named aliases may mean that
        # no window of that duration was reported, rather than an unknown one.
        return tuple(
            _window(status, str(data.get("slot") or f"window_{i}"), data)
            if isinstance(data, Mapping) else _window(status, f"window_{i}", {})
            for i, data in enumerate(native)
        )
    windows = []
    for label in ("five_hour", "seven_day"):
        fields = (f"{label}_remaining_percentage", f"{label}_used_percentage",
                  f"{label}_resets_at")
        # Collectors publish null aliases for durations they did not report.
        # Non-null malformed values still express an unreadable window.
        if any(metadata.get(field) is not None for field in fields):
            windows.append(_window(status, label, {
                "remaining_percentage": metadata.get(fields[0]),
                "used_percentage": metadata.get(fields[1]),
                "resets_at": metadata.get(fields[2]),
            }))
    if windows:
        return tuple(windows)
    if status.capacity is not None:
        capacity = status.capacity
        if capacity.remaining_percentage is None and capacity.used_percentage is None:
            capacity = replace(capacity, remaining_percentage=status.remaining_percentage)
        elif capacity.remaining_percentage is None:
            capacity = replace(capacity, remaining_percentage=100.0 - capacity.used_percentage)
        return (capacity,)
    if status.remaining_percentage is None:
        return ()
    return (CapacityState(remaining_percentage=status.remaining_percentage,
                          freshness=status.freshness, collected_at=status.observed_at),)
