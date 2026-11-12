# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Transform Claude Code statusline payloads into provider snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from ..providers.base import finite_float, parse_percentage, parse_timestamp
from .base import (
    UsageSnapshot, _snapshot_write_lock, read_usage_snapshot, valid_timestamp_epoch,
    write_usage_snapshot,
)


# Provenance for readings that came from Claude Code's own statusLine push,
# the only Claude quota source. It carries the windows' ``resets_at`` instants.
CLAUDE_STATUSLINE_SOURCE = "claude_statusline"


def _as_number(value: Any) -> int | float | None:
    # bool is an int subclass; a JSON true must never read as a percentage.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # NaN, an infinity or a number no float can hold is not a reading. Clamped
    # into 0..100 it would become a confident "100% left" or "0% left".
    # ``finite_float`` is the one test every quota parser shares.
    return value if finite_float(value) is not None else None


def _remaining_from(used: int | float | None) -> float | None:
    """Capacity left in a window, from the used percentage Claude reports.

    Claude states only how much of each window is spent; every consumer wants
    the complement. Derived once here so the router, the CLI and the Control
    Center cannot each subtract it slightly differently.
    """

    if used is None:
        return None
    return round(max(0.0, min(100.0, 100.0 - float(used))), 2)


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


#: Two reset times this close name the same window (as the pace reading does).
SAME_WINDOW_SECONDS = 120.0
CLAUDE_WINDOW_SLOTS = ("five_hour", "seven_day")


def _keeps_stored_window(stored: Mapping[str, Any], incoming: Mapping[str, Any], slot: str,
                         now: datetime) -> bool:
    """True when the stored figure for ``slot`` must outlive this push.

    Usage only grows inside a window, so a lower figure for the SAME window is
    an older one: an idle Claude Code session re-rendering rate limits it
    cached earlier, not news. The stored figure is kept when it is higher and
    the push names the same window, an earlier one, or no window at all while
    the stored one has not reset. A later window, a stored window that has
    reset, and a stored reading with no reset time are replaced normally:
    nothing shows they are the same window, and the newest observation stands.
    """

    stored_used = parse_percentage(stored.get(f"{slot}_used_percentage"))
    stored_reset = parse_timestamp(stored.get(f"{slot}_resets_at"))
    if stored_used is None or stored_reset is None:
        return False
    used = parse_percentage(incoming.get(f"{slot}_used_percentage"))
    reset = parse_timestamp(incoming.get(f"{slot}_resets_at"))
    if used is None:
        return False
    if reset is None:
        return stored_reset > now and stored_used > used
    behind = (stored_reset - reset).total_seconds()
    if behind > SAME_WINDOW_SECONDS:
        return True  # the push describes a window before the stored one
    return abs(behind) <= SAME_WINDOW_SECONDS and stored_used > used


def merge_claude_push(snapshot: UsageSnapshot, target: Path, *,
                      now: datetime | None = None) -> UsageSnapshot:
    """What to store for one status-line push, given what is stored already.

    Called with the snapshot file's lock held. A push with no rate limits
    keeps the last reading (:func:`retain_last_known_claude`). Otherwise each
    window keeps the higher figure while it is the same window
    (:func:`_keeps_stored_window`). When no window of the push is news, the
    stored reading stays whole, with its own ``observed_at``, so its age
    stays honest; only ``collected_at`` moves.
    """

    if not _has_observation(snapshot):
        return retain_last_known_claude(snapshot, target)
    stored = read_usage_snapshot(target)
    if not _has_observation(stored):
        return snapshot
    moment = now or datetime.now(timezone.utc)
    kept = [slot for slot in CLAUDE_WINDOW_SLOTS
            if _keeps_stored_window(stored, snapshot, slot, moment)]
    if not kept:
        return snapshot
    news = [slot for slot in CLAUDE_WINDOW_SLOTS if slot not in kept
            and snapshot.get(f"{slot}_used_percentage") is not None]
    # A stored time ahead of the clock is not kept: the push's own time is the
    # trustworthy one, and the higher figure is still a lower bound at it.
    if not news and valid_timestamp_epoch(stored.get("observed_at"), now=moment) is not None:
        retained = dict(stored)
        retained["collected_at"] = snapshot.get("collected_at")
        return retained
    merged = dict(snapshot)
    for slot in kept:
        for field in ("used_percentage", "remaining_percentage", "resets_at"):
            merged[f"{slot}_{field}"] = stored.get(f"{slot}_{field}")
    return merged


def publish_claude_push(target: Path, snapshot: UsageSnapshot) -> bool:
    """Read, merge and atomically write one push under the snapshot's lock.

    The same per-file lock ``publish_usage_snapshot`` takes, so two Claude
    Code sessions (or a session and a refresh) cannot interleave a read and a
    write. False, and nothing written, when the lock cannot be had: the
    stored reading is safer than an unlocked update. ``OSError`` from the
    atomic replacement propagates.
    """

    target = Path(target)
    with _snapshot_write_lock(target) as held:
        if not held:
            return False
        write_usage_snapshot(target, merge_claude_push(snapshot, target))
        return True


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
