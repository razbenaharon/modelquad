# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.models import Freshness
from orchestrator.providers import ClaudeProvider
from orchestrator.usage import parse_claude_statusline_payload, write_usage_snapshot


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def test_claude_parser_round_trips_through_provider(tmp_path):
    snapshot = parse_claude_statusline_payload(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 20, "resets_at": 100},
                "seven_day": {"used_percentage": 65, "resets_at": 200},
            }
        },
        now=lambda: NOW,
    )
    usage_file = tmp_path / "usage-claude.json"
    write_usage_snapshot(usage_file, snapshot)

    status = ClaudeProvider(usage_file, now=lambda: NOW).get_status()

    assert snapshot == {
        "provider": "claude",
        "source": "claude_statusline",
        "five_hour_used_percentage": 20,
        "seven_day_used_percentage": 65,
        "five_hour_remaining_percentage": 80,
        "seven_day_remaining_percentage": 35,
        "five_hour_resets_at": 100,
        "seven_day_resets_at": 200,
        "collected_at": NOW.isoformat(),
        # A push carrying real percentages is a provider observation made now.
        "observed_at": NOW.isoformat(),
    }
    assert status.available is True
    assert status.remaining_percentage == 35
    assert status.freshness is Freshness.FRESH
    # Both windows survive normalization, so evidence can tell short-term
    # capacity apart from weekly capacity rather than seeing only the tightest.
    assert status.metadata["five_hour_remaining_percentage"] == 80
    assert status.metadata["seven_day_remaining_percentage"] == 35
    assert status.metadata["five_hour_resets_at"] == 100
    assert status.metadata["seven_day_resets_at"] == 200


@pytest.mark.parametrize("payload", ({}, {"rate_limits": "bad"}, None))
def test_claude_parser_handles_missing_or_malformed_payloads(payload):
    snapshot = parse_claude_statusline_payload(payload, now=lambda: NOW)

    assert snapshot == {
        "provider": "claude",
        "source": "claude_statusline",
        "five_hour_used_percentage": None,
        "seven_day_used_percentage": None,
        "five_hour_remaining_percentage": None,
        "seven_day_remaining_percentage": None,
        "five_hour_resets_at": None,
        "seven_day_resets_at": None,
        "collected_at": NOW.isoformat(),
    }
    # Nothing was observed, so nothing is stamped as an observation: unknown
    # usage must stay unknown rather than read as a fresh 0%.
    assert "observed_at" not in snapshot


def test_usage_modules_contain_no_hardcoded_home_path():
    usage_directory = Path(__file__).parents[1] / "src" / "orchestrator" / "usage"
    source = "\n".join(path.read_text(encoding="utf-8") for path in usage_directory.glob("*.py"))

    assert "C:\\Users\\" not in source
