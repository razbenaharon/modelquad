# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Claude's two rate-limit windows, from statusLine payload to routing evidence.

Claude Code reports a five-hour and a seven-day window independently, and the
two can disagree sharply -- plenty of headroom right now while the week is
nearly spent, or the reverse. These tests pin that both windows survive the
whole pipeline separately, that a window nobody reported stays unknown rather
than becoming a zero, and that re-reading an old push never makes it fresh.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from modelquad.providers import ClaudeProvider
from modelquad.usage import parse_claude_statusline_payload, write_usage_snapshot
from modelquad.usage.claude import CLAUDE_STATUSLINE_SOURCE
from modelquad.usage.refresh import refresh_provider


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(minutes=30)

# The exact shape documented for the statusLine payload: `resets_at` is a Unix
# epoch second, not an ISO string, so it must survive as the integer it is.
FIVE_HOUR_RESET = 1738425600
SEVEN_DAY_RESET = 1738857600


def payload(**windows):
    return {"rate_limits": {name: value for name, value in windows.items()}}


def test_both_windows_are_kept_separately():
    snapshot = parse_claude_statusline_payload(
        payload(
            five_hour={"used_percentage": 8.0, "resets_at": FIVE_HOUR_RESET},
            seven_day={"used_percentage": 92.0, "resets_at": SEVEN_DAY_RESET},
        ),
        now=lambda: NOW,
    )

    # The strategically important case: usable right now, nearly out for the
    # week. Collapsing these into one number would erase exactly that.
    assert snapshot["five_hour_used_percentage"] == 8.0
    assert snapshot["five_hour_remaining_percentage"] == 92.0
    assert snapshot["seven_day_used_percentage"] == 92.0
    assert snapshot["seven_day_remaining_percentage"] == 8.0
    assert snapshot["five_hour_resets_at"] == FIVE_HOUR_RESET
    assert snapshot["seven_day_resets_at"] == SEVEN_DAY_RESET
    assert snapshot["source"] == CLAUDE_STATUSLINE_SOURCE


def test_only_five_hour_present_leaves_seven_day_unknown():
    snapshot = parse_claude_statusline_payload(
        payload(five_hour={"used_percentage": 30.0, "resets_at": FIVE_HOUR_RESET}),
        now=lambda: NOW,
    )

    assert snapshot["five_hour_remaining_percentage"] == 70.0
    # The docs say each window may be independently absent; an absent window is
    # unknown, never a full or empty one.
    assert snapshot["seven_day_used_percentage"] is None
    assert snapshot["seven_day_remaining_percentage"] is None
    assert snapshot["seven_day_resets_at"] is None
    assert snapshot["observed_at"] == NOW.isoformat()


def test_only_seven_day_present_leaves_five_hour_unknown():
    snapshot = parse_claude_statusline_payload(
        payload(seven_day={"used_percentage": 55.0, "resets_at": SEVEN_DAY_RESET}),
        now=lambda: NOW,
    )

    assert snapshot["seven_day_remaining_percentage"] == 45.0
    assert snapshot["five_hour_used_percentage"] is None
    assert snapshot["five_hour_remaining_percentage"] is None
    assert snapshot["observed_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    "windows",
    (
        {"five_hour": None, "seven_day": None},
        {"five_hour": "nonsense", "seven_day": []},
        {"five_hour": {"used_percentage": None}, "seven_day": {"used_percentage": "x"}},
        # bool is an int subclass in Python; a JSON true is not a percentage.
        {"five_hour": {"used_percentage": True}},
    ),
)
def test_malformed_windows_read_as_unknown_not_zero(windows):
    snapshot = parse_claude_statusline_payload(payload(**windows), now=lambda: NOW)

    assert snapshot["five_hour_used_percentage"] is None
    assert snapshot["five_hour_remaining_percentage"] is None
    assert snapshot["seven_day_used_percentage"] is None
    assert snapshot["seven_day_remaining_percentage"] is None
    # Nothing was observed, so this is not an observation.
    assert "observed_at" not in snapshot


def test_zero_used_is_a_real_reading_not_a_missing_one():
    snapshot = parse_claude_statusline_payload(
        payload(five_hour={"used_percentage": 0}, seven_day={"used_percentage": 0}),
        now=lambda: NOW,
    )

    # 0% used is full capacity and a genuine observation. Treating a falsy
    # percentage as "absent" would silently discard the best possible news.
    assert snapshot["five_hour_used_percentage"] == 0
    assert snapshot["five_hour_remaining_percentage"] == 100.0
    assert snapshot["seven_day_remaining_percentage"] == 100.0
    assert snapshot["observed_at"] == NOW.isoformat()


def test_fully_spent_window_reads_as_zero_remaining():
    snapshot = parse_claude_statusline_payload(
        payload(five_hour={"used_percentage": 100}, seven_day={"used_percentage": 100}),
        now=lambda: NOW,
    )

    assert snapshot["five_hour_remaining_percentage"] == 0.0
    assert snapshot["seven_day_remaining_percentage"] == 0.0


def test_over_hundred_used_is_clamped_rather_than_going_negative():
    snapshot = parse_claude_statusline_payload(
        payload(five_hour={"used_percentage": 130}), now=lambda: NOW
    )

    assert snapshot["five_hour_remaining_percentage"] == 0.0


def test_claude_stays_available_when_usage_is_unknown(tmp_path):
    usage_file = tmp_path / "usage-claude.json"
    write_usage_snapshot(
        usage_file, parse_claude_statusline_payload({}, now=lambda: NOW)
    )

    status = ClaudeProvider(usage_file, now=lambda: NOW).get_status()

    # No rate limits yet is not an outage: no request has happened in the
    # session yet, or the host simply never pushed one. Claude is still usable,
    # and its capacity is unknown -- never 0, and never "unavailable".
    assert status.available is True
    assert status.remaining_percentage is None
    assert status.reason == "Usage data has no valid five-hour or seven-day percentage."


def test_routing_evidence_receives_both_windows(tmp_path):
    usage_file = tmp_path / "usage-claude.json"
    write_usage_snapshot(
        usage_file,
        parse_claude_statusline_payload(
            payload(
                five_hour={"used_percentage": 8.0, "resets_at": FIVE_HOUR_RESET},
                seven_day={"used_percentage": 92.0, "resets_at": SEVEN_DAY_RESET},
            ),
            now=lambda: NOW,
        ),
    )

    status = ClaudeProvider(usage_file, now=lambda: NOW).get_status()

    # The headline figure is the tightest window, so routing cannot spend past
    # the weekly cap just because the five-hour one looks roomy...
    assert status.remaining_percentage == 8.0
    # ...but both windows and both reset instants remain readable in evidence.
    assert status.metadata["five_hour_remaining_percentage"] == 92.0
    assert status.metadata["seven_day_remaining_percentage"] == 8.0
    assert status.metadata["five_hour_resets_at"] == FIVE_HOUR_RESET
    assert status.metadata["seven_day_resets_at"] == SEVEN_DAY_RESET


def test_a_refresh_keeps_the_statusline_push_and_its_reset_times(tmp_path):
    """A refresh re-reads the push; it must not lose the reset instants."""

    usage_file = tmp_path / "usage-claude.json"
    write_usage_snapshot(
        usage_file,
        parse_claude_statusline_payload(
            payload(
                five_hour={"used_percentage": 8.0, "resets_at": FIVE_HOUR_RESET},
                seven_day={"used_percentage": 92.0, "resets_at": SEVEN_DAY_RESET},
            ),
            now=lambda: NOW,
        ),
    )

    result = refresh_provider("claude", tmp_path, now=lambda: NOW, force=True)

    assert result.source == CLAUDE_STATUSLINE_SOURCE
    assert result.observed_at == NOW.isoformat()
    assert result.freshness == "fresh"
    # The reset instants only the statusLine knows must still be on disk.
    persisted = json.loads(usage_file.read_text(encoding="utf-8"))
    assert persisted["five_hour_resets_at"] == FIVE_HOUR_RESET
    assert persisted["seven_day_resets_at"] == SEVEN_DAY_RESET


def test_rereading_an_old_snapshot_does_not_make_it_fresh(tmp_path):
    """Looking again is not learning something new."""

    usage_file = tmp_path / "usage-claude.json"
    write_usage_snapshot(
        usage_file,
        parse_claude_statusline_payload(
            payload(five_hour={"used_percentage": 8.0, "resets_at": FIVE_HOUR_RESET}),
            now=lambda: EARLIER,
        ),
    )

    much_later = NOW + timedelta(hours=3)
    result = refresh_provider("claude", tmp_path, now=lambda: much_later, force=True)

    # collected_at advances because we looked; observed_at does not, because
    # the provider produced nothing newer -- so the age keeps growing.
    assert result.collected_at == much_later.isoformat()
    assert result.observed_at == EARLIER.isoformat()
    assert result.newer_observation is False
    assert result.freshness == "very_stale"


def test_legacy_snapshot_without_observed_at_still_reads(tmp_path):
    """Installs upgraded from the pre-observed_at shape must not lose data."""

    usage_file = tmp_path / "usage-claude.json"
    # Exactly the old on-disk shape: no source, no observed_at, no remaining.
    write_usage_snapshot(
        usage_file,
        {
            "provider": "claude",
            "five_hour_used_percentage": 20,
            "seven_day_used_percentage": 65,
            "five_hour_resets_at": FIVE_HOUR_RESET,
            "seven_day_resets_at": SEVEN_DAY_RESET,
            "collected_at": NOW.isoformat(),
        },
    )

    result = refresh_provider("claude", tmp_path, now=lambda: NOW, force=True)

    # The legacy file's collection time is accepted as its observation time.
    assert result.source == CLAUDE_STATUSLINE_SOURCE
    assert result.observed_at == NOW.isoformat()

    status = ClaudeProvider(usage_file, now=lambda: NOW).get_status()
    assert status.remaining_percentage == 35
    assert status.metadata["five_hour_resets_at"] == FIVE_HOUR_RESET
