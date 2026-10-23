# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Window-aware freshness for routing; never changes the displayed snapshot."""

from collections.abc import Mapping
from dataclasses import replace

from .models import FreshnessThresholds
from .providers.base import finite_float, freshness_for
from .quota_windows import _buckets, reported_windows

WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10080}


def binding_window_minutes(status):
    windows = [w for w in reported_windows(status) if w.remaining_percentage is not None]
    binding = min(windows, key=lambda w: (w.remaining_percentage, w.window or ""), default=None)
    if binding is None:
        return None
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    bucket = _buckets(status).get(binding.window)
    if isinstance(bucket, Mapping):
        minutes = bucket.get('window_minutes')
        if (type(minutes) is int and minutes > 0
                and finite_float(minutes * 60) is not None):
            return minutes
    native = metadata.get("windows")
    if isinstance(native, list) and native:
        for index, window in enumerate(native):
            if (isinstance(window, Mapping)
                    and str(window.get("slot") or f"window_{index}") == binding.window):
                minutes = window.get("window_minutes")
                return (minutes if type(minutes) is int and minutes > 0
                        and finite_float(minutes * 60) is not None else None)
        return None
    if status.name in {"claude", "codex"}:
        return WINDOW_MINUTES.get(binding.window)
    return None


def routing_freshness(status, thresholds=None, *, now=None):
    """Keep today's label if the reading has no dated, known binding window."""
    minutes = binding_window_minutes(status)
    if minutes is None or status.observed_at is None:
        return status.freshness
    thresholds = thresholds or FreshnessThresholds()
    seconds = float(minutes * 60)
    return freshness_for(status.observed_at, FreshnessThresholds(
        fresh_seconds=max(thresholds.fresh_seconds, seconds * 0.01),
        stale_seconds=max(thresholds.stale_seconds, seconds * 0.12),
    ), now)


def routing_view(status, thresholds=None, *, now=None):
    """An ephemeral routing copy. ProviderStatus/display input stays untouched."""
    freshness = routing_freshness(status, thresholds, now=now)
    capacity = replace(status.capacity, freshness=freshness) if status.capacity is not None else None
    return replace(status, freshness=freshness, capacity=capacity)
