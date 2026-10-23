# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Transform Claude Code statusline payloads into provider snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .base import UsageSnapshot


# Provenance for readings that came from Claude Code's own statusLine push,
# the only Claude quota source. It carries the windows' ``resets_at`` instants.
CLAUDE_STATUSLINE_SOURCE = "claude_statusline"


def _as_number(value: Any) -> int | float | None:
    # bool is an int subclass; a JSON true must never read as a percentage.
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def _remaining_from(used: int | float | None) -> float | None:
    """Capacity left in a window, from the used percentage Claude reports.

    Claude states only how much of each window is spent; every consumer wants
    the complement. Derived once here so the router, the CLI and the Control
    Center cannot each subtract it slightly differently.
    """

    if used is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(used)))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _has_observation(snapshot: Any) -> bool:
    return (
        isinstance(snapshot, Mapping)
        and (
            snapshot.get("five_hour_used_percentage") is not None
            or snapshot.get("seven_day_used_percentage") is not None
        )
    )


def retain_last_known_claude(snapshot: UsageSnapshot, target: Path) -> UsageSnapshot:
    """Keep the previous Claude reading when a push carries no rate limits.

    Claude is the one provider we cannot re-ask: nothing re-fetches its quota,
    so an empty push that overwrote a real reading would destroy data that
    only a future Claude Code session could ever replace. The retained
    reading keeps its original ``observed_at`` so freshness ages it honestly.
    """

    if _has_observation(snapshot):
        return snapshot
    try:
        previous = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return snapshot
    if not _has_observation(previous):
        return snapshot
    retained = dict(previous)
    retained["stale"] = True
    retained["collected_at"] = snapshot.get("collected_at")
    retained["reason"] = "Claude Code pushed a status line carrying no rate limits."
    return retained


def parse_claude_statusline_payload(
    payload: Mapping[str, Any] | object,
    *,
    now: Callable[[], datetime] | None = None,
) -> UsageSnapshot:
    """Return the legacy ``usage-claude.json`` shape for a hook payload.

    The legacy statusline.py this replaces never stamped a collection time,
    so ClaudeProvider always read back Freshness.UNKNOWN -- a faithful port
    would keep that gap. Since the package now owns the whole pipeline
    (unlike the legacy script), add ``collected_at`` here to match the
    codex collector and give Claude real freshness tracking.
    """

    rate_limits = _mapping(_mapping(payload).get("rate_limits"))
    five_hour = _mapping(rate_limits.get("five_hour"))
    seven_day = _mapping(rate_limits.get("seven_day"))
    collected_at = (now or (lambda: datetime.now(timezone.utc)))().isoformat()
    five_used = _as_number(five_hour.get("used_percentage"))
    seven_used = _as_number(seven_day.get("used_percentage"))
    snapshot: UsageSnapshot = {
        "provider": "claude",
        "source": CLAUDE_STATUSLINE_SOURCE,
        "five_hour_used_percentage": five_used,
        "seven_day_used_percentage": seven_used,
        "five_hour_remaining_percentage": _remaining_from(five_used),
        "seven_day_remaining_percentage": _remaining_from(seven_used),
        "five_hour_resets_at": _as_number(five_hour.get("resets_at")),
        "seven_day_resets_at": _as_number(seven_day.get("resets_at")),
        "collected_at": collected_at,
    }
    # A push carrying real percentages IS a provider observation made right
    # now, so it is stamped as one here. Leaving it unstamped (as this used to)
    # made the snapshot unorderable, and write_usage_snapshot_if_newer treats an
    # unorderable reading as superseded by anything -- so the older Claude
    # Desktop plan-history scan silently overwrote fresher statusLine pushes and
    # took their resets_at with it. A payload with no rate_limits stays
    # deliberately unstamped: that is not an observation, and must read UNKNOWN
    # rather than become a fresh-looking zero.
    if five_used is not None or seven_used is not None:
        snapshot["observed_at"] = collected_at
    return snapshot
