# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A quota figure must never be attributed to a window it was not observed in."""

from __future__ import annotations

from datetime import datetime, timezone

from modelquad.usage.codex_appserver import parse_rate_limits_result


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _reading(primary, secondary=None):
    return parse_rate_limits_result(
        {"rateLimits": {"primary": primary, "secondary": secondary, "planType": "plus"}},
        now=lambda: NOW,
    )


def test_weekly_only_leaves_the_five_hour_window_unknown():
    """The real shape on a Codex "plus" account: one weekly window, no 5-hour one."""

    snapshot = _reading({"usedPercent": 96, "windowDurationMins": 10080, "resetsAt": 1787317030})

    assert snapshot["seven_day_remaining_percentage"] == 4.0
    assert snapshot["five_hour_remaining_percentage"] is None
    assert snapshot["five_hour_used_percentage"] is None


def test_durations_win_even_when_the_order_is_reversed():
    snapshot = _reading(
        {"usedPercent": 70.0, "windowDurationMins": 10080, "resetsAt": 222},
        {"usedPercent": 40.0, "windowDurationMins": 300, "resetsAt": 111},
    )

    assert snapshot["five_hour_used_percentage"] == 40.0
    assert snapshot["seven_day_used_percentage"] == 70.0


def test_an_unrecognised_duration_is_not_forced_into_a_slot():
    snapshot = _reading({"usedPercent": 10.0, "windowDurationMins": 60, "resetsAt": 1})

    assert snapshot["five_hour_used_percentage"] is None
    assert snapshot["seven_day_used_percentage"] is None
    assert [window["slot"] for window in snapshot["windows"]] == ["other"]
