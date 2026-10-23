# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Codex usage detail must reach the card, and must stay what Codex reported.

UX-3. The Codex card showed a bare headline percentage while Claude showed both
windows, because ``CodexProvider`` dropped every percentage on the way out:
``rateLimitWindows`` saw the reset keys, took the Claude/Codex branch, found no
numbers and rendered nothing. These tests pin the fix and the invariant that
governs it -- absent evidence stays unknown, and is never rounded up into a
number, a window, a reset or a plan that Codex did not publish.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from modelquad.providers.codex import CodexProvider
from modelquad.usage.codex_appserver import _unobserved, parse_rate_limits_result


NOW = datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc)

# An ``account/rateLimits/read`` result with both windows and a credit block.
BOTH_WINDOWS = {
    "rateLimits": {
        "primary": {"usedPercent": 3.0, "windowDurationMins": 300, "resetsAt": 1787943931},
        "secondary": {"usedPercent": 3.0, "windowDurationMins": 10080, "resetsAt": 1788507037},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "plus",
    }
}


def _reading(result):
    return parse_rate_limits_result(result, now=lambda: NOW)


def _status_for(tmp_path, snapshot):
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir(exist_ok=True)
    (usage_dir / "usage-codex.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return CodexProvider(usage_directory=usage_dir).get_status()


def test_provider_metadata_carries_both_windows_percentages(tmp_path):
    """The UX-3 defect itself: these four keys never reached the former Control Center."""

    status = _status_for(tmp_path, _reading(BOTH_WINDOWS))

    assert status.metadata["five_hour_used_percentage"] == 3.0
    assert status.metadata["five_hour_remaining_percentage"] == 97.0
    assert status.metadata["seven_day_used_percentage"] == 3.0
    assert status.metadata["seven_day_remaining_percentage"] == 97.0
    # The reset instants and the window list survive alongside them.
    assert status.metadata["five_hour_resets_at"] == 1787943931
    assert status.metadata["seven_day_resets_at"] == 1788507037
    assert len(status.metadata["windows"]) == 2


def test_provider_reports_the_plan_codex_named(tmp_path):
    status = _status_for(tmp_path, _reading(BOTH_WINDOWS))

    assert status.plan == "plus"
    assert status.metadata["plan_type"] == "plus"


def test_unknown_usage_reaches_the_card_as_unknown_not_as_zero(tmp_path):
    snapshot = _unobserved(reason="app-server unreachable", available=True, now=lambda: NOW)
    status = _status_for(tmp_path, snapshot)

    assert status.remaining_percentage is None
    assert status.plan is None
    assert status.metadata.get("five_hour_remaining_percentage") is None
    assert status.metadata.get("seven_day_remaining_percentage") is None


def test_credits_and_balance_never_reach_the_card(tmp_path):
    """Codex credit balance is billing-adjacent and is not subscription quota."""

    status = _status_for(tmp_path, _reading(BOTH_WINDOWS))

    assert "credits" not in status.metadata
    assert "balance" not in status.metadata
    # ``source_file`` is ModelQuad's own snapshot path, which the base provider
    # stamps on every provider alike; it is excluded here because a temporary
    # directory's name is not evidence about Codex.
    observed = {key: value for key, value in status.metadata.items() if key != "source_file"}
    serialised = json.dumps(observed)
    assert "credits" not in serialised
    assert "balance" not in serialised
    assert "spend_control" not in serialised
