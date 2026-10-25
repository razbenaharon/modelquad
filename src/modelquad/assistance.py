# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Content-free quota facts, bounded advice and the suggested handoff.

Pure: no collection, provider calls, persistence or worker launch. Windows are
copied individually from the existing normalized snapshots. Missing data never
becomes zero, and an original observation time is never replaced by collection
time.

Quota is display-only. It never orders, admits or refuses a worker; the only
thing it may produce is :func:`handoff_suggestion`, a sentence that ASKS the
user whether to hand the task to Codex. A suggestion cannot authorize or start
anything: a job starts only when the user approves inside Claude Code (the
elicitation of ``modelquad_handoff``) or asks for a delegation themselves.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from .models import Freshness
from .providers.base import (
    finite_float, freshness_for, parse_percentage, parse_timestamp, reset_at_from_delay,
)

# Provider collector provenance is a closed label, not arbitrary metadata.
SOURCES = frozenset({"claude_statusline", "codex_app_server", "synthetic"})
WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10080}
WINDOW_NAMES = {"five_hour": "5-hour window", "seven_day": "weekly window"}

# Used percentage of a Claude window at which a reply suggests handing the
# task to Codex. The weekly window gets a higher bar: at 80% of a week there
# are usually days left, while 80% of five hours is about one more long task.
HANDOFF_USED_PERCENT = {"five_hour": 80.0, "seven_day": 90.0}
# A Codex window at or above this used percentage makes the suggestion moot:
# offering a worker that is nearly out would only be noise.
HANDOFF_CODEX_EXHAUSTED_USED = 90.0
# When Claude's 5-hour window resets within this many minutes, waiting is
# usually simpler than moving the task; the weekly window never waits.
HANDOFF_WAIT_MINUTES = 30
# A pace estimate needs two readings at least this far apart, and growth.
PACE_MIN_SECONDS = 600


def _source(value):
    return value if isinstance(value, str) and value in SOURCES else "unknown"


def _moment(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return parse_timestamp(value)


def quota_windows(status, thresholds, *, now: datetime):
    """Every observed window; no other window lends it a number or clock."""
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    observed = _moment(status.observed_at)
    source = _source(metadata.get("source"))
    stale = freshness_for(observed, thresholds, now) in (Freshness.STALE, Freshness.VERY_STALE)
    retained = metadata.get("stale") is True
    windows = []

    def add(label, raw, *, minutes=None, reset=None):
        used = parse_percentage(raw.get("used_percentage"))
        remaining = parse_percentage(raw.get("remaining_percentage"))
        if remaining is None and raw.get("remaining_fraction") is not None:
            fraction = finite_float(raw.get("remaining_fraction"))
            remaining = parse_percentage(fraction * 100) if fraction is not None else None
        resets = _moment(raw.get("resets_at")) or _moment(reset)
        if used is None and remaining is None and resets is None:
            return
        duration = raw.get("window_minutes", minutes)
        duration = duration if type(duration) is int and duration > 0 else None
        windows.append({"window": label, "source": source,
                        "observed_at": observed.isoformat() if observed else None,
                        "window_minutes": duration, "used_percent": used,
                        "remaining_percent": remaining,
                        "resets_at": resets.isoformat() if resets else None,
                        "stale": stale or retained,
                        "unknown": observed is None or (used is None and remaining is None)})

    native = metadata.get("windows")
    if isinstance(native, (list, tuple)):
        for index, window in enumerate(native):
            if isinstance(window, Mapping):
                slot = window.get("slot")
                label = slot if isinstance(slot, str) and slot in WINDOW_MINUTES else f"window_{index}"
                add(label, window)
    # Native windows take precedence; named snapshot fields are the legacy
    # representation of those same windows, not extra observations.
    labels = {window["window"] for window in windows}
    for slot, minutes in WINDOW_MINUTES.items():
        if slot not in labels:
            add(slot, {"used_percentage": metadata.get(f"{slot}_used_percentage"),
                       "remaining_percentage": metadata.get(f"{slot}_remaining_percentage"),
                       "resets_at": metadata.get(f"{slot}_resets_at")}, minutes=minutes)
    buckets = metadata.get("quota") or metadata.get("buckets")
    if isinstance(buckets, Mapping):
        # Indices prevent untrusted bucket labels from reflecting a path,
        # URL, branch or other arbitrary text in the content-free facts block.
        for index, bucket in enumerate(buckets.values()):
            if isinstance(bucket, Mapping):
                reset = parse_timestamp(bucket.get("reset_time")) or reset_at_from_delay(
                    observed, bucket.get("reset_in_seconds"))
                add(f"bucket_{index}", bucket, reset=reset)
    if not windows and status.capacity is not None:
        capacity = status.capacity
        label = capacity.window if isinstance(capacity.window, str) and capacity.window in WINDOW_MINUTES else "binding_window"
        add(label, {"remaining_percentage": capacity.remaining_percentage,
                    "used_percentage": capacity.used_percentage},
            minutes=WINDOW_MINUTES.get(label), reset=capacity.reset_at)
    return windows


def _used(window):
    used = window.get("used_percent")
    if used is None and window.get("remaining_percent") is not None:
        used = 100.0 - window["remaining_percent"]
    return used


def _readable(window, now):
    """A window whose number still describes the current window.

    Unknown readings never count. A stale reading counts while its window has
    not reset: usage only grows inside a window, so an older used percentage
    is a lower bound, never an overstatement. Past its reset it says nothing.
    """
    if window.get("unknown") or _used(window) is None:
        return False
    resets = _moment(window.get("resets_at"))
    if window.get("stale"):
        return resets is not None and resets > now
    return resets is None or resets > now


def _in(seconds):
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def _window_phrase(window, now):
    name = WINDOW_NAMES.get(window["window"], "window")
    text = f"{_used(window):.0f}% of its {name}"
    resets = _moment(window.get("resets_at"))
    if resets is not None and resets > now:
        text += f" (resets in {_in((resets - now).total_seconds())})"
    return text




def local_clock(moment, now=None):
    """A reset or pace time in the user's local time: "15:00", or "Tue 15:00" on another day."""
    moment = _moment(moment)
    if moment is None:
        return None
    local = moment.astimezone()
    reference = _moment(now)
    if reference is not None and local.date() != reference.astimezone().date():
        return local.strftime("%a %H:%M")
    return local.strftime("%H:%M")


def _window_view(window):
    return {key: window.get(key) for key in
            ("window", "used_percent", "resets_at", "observed_at", "stale")}


def handoff_suggestion(statuses, thresholds, *, now):
    """Advice that ASKS the user to hand the task to Codex, or to wait; or None.

    Pure and side-effect free: it reads normalized statuses and returns a
    dictionary. Returned when a readable Claude window is at or above its
    ``HANDOFF_USED_PERCENT``. When only the 5-hour window is over and it
    resets within ``HANDOFF_WAIT_MINUTES``, the advice is to wait
    (``"to": "wait"``) rather than move the task; a weekly window over its
    threshold always suggests Codex, unless Codex is known to be nearly out
    itself. Unknown Claude quota never suggests anything; unknown Codex quota
    is stated as unknown, never as zero. ``launches`` is always False: the
    suggestion carries no authority, and the only ways a job starts are the
    user accepting ``modelquad_handoff``'s prompt or asking for a delegation.
    """
    now = _moment(now)
    by_name = {status.name: status for status in statuses}
    claude = by_name.get("claude")
    if claude is None or now is None:
        return None
    readable = [w for w in quota_windows(claude, thresholds, now=now)
                if w["window"] in HANDOFF_USED_PERCENT and _readable(w, now)]
    over = [w for w in readable if _used(w) >= HANDOFF_USED_PERCENT[w["window"]]]
    if not over:
        return None
    claude_window = max(over, key=lambda w: (_used(w), w["window"] == "five_hour"))
    if all(w["window"] == "five_hour" for w in over):
        resets = _moment(claude_window.get("resets_at"))
        if resets is not None and resets - now <= timedelta(minutes=HANDOFF_WAIT_MINUTES):
            return {
                "suggest": True,
                "to": "wait",
                "message": (f"Claude's 5-hour window resets at {local_clock(resets, now)}; "
                            "waiting may be simpler."),
                "claude_window": _window_view(claude_window),
                "codex_window": None,
                "threshold_used_percent": HANDOFF_USED_PERCENT["five_hour"],
                "approval": "Nothing starts: this only suggests waiting for the reset.",
                "launches": False,
            }
    codex = by_name.get("codex")
    codex_windows = [w for w in (quota_windows(codex, thresholds, now=now) if codex else [])
                     if w["window"] in WINDOW_MINUTES and _readable(w, now)]
    codex_window = max(codex_windows, key=lambda w: (_used(w), w["window"] == "five_hour"), default=None)
    if codex_window is not None and _used(codex_window) >= HANDOFF_CODEX_EXHAUSTED_USED:
        return None
    codex_text = (f"Codex is at {_window_phrase(codex_window, now)}." if codex_window is not None
                  else "Codex quota is unknown.")
    message = (f"Claude is at {_window_phrase(claude_window, now)}; hand this task to Codex? "
               f"{codex_text}")
    return {
        "suggest": True,
        "to": "codex",
        "message": message,
        "claude_window": _window_view(claude_window),
        "codex_window": _window_view(codex_window) if codex_window is not None else None,
        "threshold_used_percent": HANDOFF_USED_PERCENT[claude_window["window"]],
        "approval": ("Nothing starts until the user approves: call modelquad_handoff, which asks "
                     "the user in Claude Code, or delegate with provider='codex' only when the "
                     "user asked for it."),
        "launches": False,
    }


# --- plain-language status ------------------------------------------------------

FIRST_MESSAGE = "shows after your first message"
NEXT_MESSAGE = "shows after your next message"
NO_FIVE_HOUR = "no 5-hour limit on your plan"
SIGN_IN = "sign in to Codex"
CODEX_NOT_FOUND = "Codex not found"
# Words a Codex app-server or CLI answer uses when nobody is signed in.
_SIGN_IN_WORDS = ("not logged in", "log in", "login", "logged out", "sign in", "signed in",
                  "unauthorized", "unauthenticated", "authentication", "401")


def _reason_text(status):
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    parts = (status.reason, metadata.get("quota_error"))
    return " ".join(part for part in parts if isinstance(part, str)).casefold()


def _codex_state(status):
    """"Codex not found" or "sign in to Codex" when the reading says so, else None."""
    if status is None:
        return None
    reason = _reason_text(status)
    if "codex cli not found" in reason:
        return CODEX_NOT_FOUND
    if any(word in reason for word in _SIGN_IN_WORDS):
        return SIGN_IN
    return None


def _has_no_five_hour_window(status):
    """Codex answered with windows, and none of them is a 5-hour window."""
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    native = metadata.get("windows")
    if not isinstance(native, (list, tuple)):
        return False
    observed = [w for w in native if isinstance(w, Mapping)
                and parse_percentage(w.get("used_percentage")) is not None]
    return bool(observed) and not any(w.get("slot") == "five_hour" for w in observed)


def _plan_without_five_hour(name, status, windows):
    """Codex's plan has no 5-hour window, judged only from a current reading.

    A stale or missing weekly reading (say, one an earlier install left
    behind) says nothing about the plan today, so the window stays unknown.
    """
    seven = windows.get("seven_day")
    return (name == "codex" and status is not None and "five_hour" not in windows
            and seven is not None and not seven.get("unknown") and not seven.get("stale")
            and _has_no_five_hour_window(status))


def pace_full_at(first, window, now):
    """When this window fills at the pace since ``first``, or None.

    ``first`` is the earliest reading kept for the same window
    (``{"used", "observed_at"}``). Nothing is said without two readings at
    least ``PACE_MIN_SECONDS`` apart, real growth, a fresh current reading, and
    a projected time that is still ahead and before the window resets.
    """
    if not isinstance(first, Mapping) or window is None or window.get("unknown") or window.get("stale"):
        return None
    used = _used(window)
    observed = _moment(window.get("observed_at"))
    first_used = parse_percentage(first.get("used"))
    first_at = _moment(first.get("observed_at"))
    if None in (used, observed, first_used, first_at) or used >= 100:
        return None
    elapsed = (observed - first_at).total_seconds()
    gained = used - first_used
    if elapsed < PACE_MIN_SECONDS or gained <= 0:
        return None
    full = observed + timedelta(seconds=(100.0 - used) * elapsed / gained)
    resets = _moment(window.get("resets_at"))
    if full <= now or (resets is not None and full >= resets):
        return None
    return full


def next_pace_readings(stored, statuses, thresholds, *, now):
    """The earliest fresh reading of each current 5-hour window, for :func:`pace_full_at`.

    Keeps a stored first reading while its window has not reset and usage has
    not gone down; otherwise the current reading becomes the first. Entries
    whose window has reset are dropped. Pure: the caller stores the result.
    """
    now = _moment(now)
    kept = {}
    for key, first in (stored or {}).items():
        resets = _moment(first.get("resets_at")) if isinstance(first, Mapping) else None
        if isinstance(key, str) and resets is not None and resets > now:
            kept[key] = dict(first)
    for status in statuses:
        for window in quota_windows(status, thresholds, now=now):
            if window["window"] != "five_hour" or window["stale"] or not _readable(window, now):
                continue
            key = f"{status.name}:five_hour"
            first = kept.get(key)
            resets = _moment(window.get("resets_at"))
            same_window = (
                first is not None and resets is not None
                and abs((_moment(first.get("resets_at")) - resets).total_seconds()) <= 120
                and (parse_percentage(first.get("used")) or 0.0) <= _used(window)
            )
            if not same_window and resets is not None:
                kept[key] = {"used": _used(window), "observed_at": window["observed_at"],
                             "resets_at": window["resets_at"]}
    return kept


def _provider_view(status, thresholds, now, pace):
    """(windows by label, pace time for the 5-hour window)."""
    if status is None:
        return {}, None
    windows = {w["window"]: w for w in quota_windows(status, thresholds, now=now)}
    full = pace_full_at((pace or {}).get(f"{status.name}:five_hour"), windows.get("five_hour"), now)
    return windows, full


def _window_text(window, now, *, name):
    """"38% left" (with "(stale)"), or the plain-language state; never a made-up number.

    The line says what is left, not what is used: "0% used" on a fresh
    window read to the owner as nothing left (live test, 2026-10-25)."""
    if window is None or window.get("unknown") or _used(window) is None:
        return "unknown"
    resets = _moment(window.get("resets_at"))
    if resets is not None and resets <= now:
        # The reading describes a window that has reset.
        return NEXT_MESSAGE if name == "claude" else "unknown"
    text = f"{min(100.0, max(0.0, 100.0 - _used(window))):.0f}% left"
    return f"{text} (stale)" if window.get("stale") else text


def _lead_state(name, status, windows):
    """A whole-provider state that replaces both windows, or None."""
    if name == "codex":
        state = _codex_state(status)
        if state == CODEX_NOT_FOUND:
            return CODEX_NOT_FOUND
        if state is not None and not any(_used(w) is not None for w in windows.values()):
            return f"Codex: {state}"
        return None
    if status is None or not any(_used(w) is not None for w in windows.values()):
        return f"Claude: {FIRST_MESSAGE}"
    return None


def _handoff_suffix(statuses, thresholds, now):
    suggestion = handoff_suggestion(statuses, thresholds, now=now)
    if suggestion is None:
        return ""
    if suggestion["to"] == "wait":
        resets = local_clock(suggestion["claude_window"]["resets_at"], now)
        return f" | Claude is low: resets {resets}, waiting may be simpler"
    return " | Claude is low: hand off to Codex?"


def statusline_text(statuses, thresholds, *, now, pace=None):
    """One plain-ASCII line for Claude Code's status line.

    Claude's and Codex's 5-hour and weekly windows from the local readings,
    with the 5-hour reset in local time and, when ``pace`` holds an earlier
    reading of the same window, when it fills at that pace. A missing reading
    is said in plain words ("shows after your first message", "sign in to
    Codex", "Codex not found", "no 5-hour limit on your plan") or "unknown",
    never as 0%. When the handoff threshold is met the line ends with the
    question or the advice to wait, nothing more: the status line cannot
    start a job.
    """
    now = _moment(now)
    by_name = {status.name: status for status in statuses}
    parts = []
    for name, label in (("claude", "Claude"), ("codex", "Codex")):
        status = by_name.get(name)
        windows, full = _provider_view(status, thresholds, now, pace)
        lead = _lead_state(name, status, windows)
        if lead is not None:
            parts.append(lead)
            continue
        seven = _window_text(windows.get("seven_day"), now, name=name)
        if _plan_without_five_hour(name, status, windows):
            parts.append(f"Codex: {NO_FIVE_HOUR} / 7d {seven}")
            continue
        five_window = windows.get("five_hour")
        five = _window_text(five_window, now, name=name)
        resets = _moment((five_window or {}).get("resets_at"))
        if five.endswith(("left", "(stale)")) and resets is not None:
            five += f", resets {local_clock(resets, now)}"
        if full is not None:
            five += f", at this pace full by about {local_clock(full, now)}"
        parts.append(f"{label} 5h {five} / 7d {seven}")
    return " | ".join(parts) + _handoff_suffix(statuses, thresholds, now)


def statusline_segment(statuses, thresholds, *, now, pace=None):
    """The compact ModelQuad part for a status line the user already has.

    For example "Codex 5h 70% left | Claude 5h 38% left": each provider's
    5-hour window (or its weekly one when the plan has no 5-hour limit), the
    same plain-language states as :func:`statusline_text`, and the handoff
    advice when it applies. Plain ASCII, so any status line can append it.
    """
    now = _moment(now)
    by_name = {status.name: status for status in statuses}
    parts = []
    for name, label in (("codex", "Codex"), ("claude", "Claude")):
        status = by_name.get(name)
        windows, full = _provider_view(status, thresholds, now, pace)
        lead = _lead_state(name, status, windows)
        if lead is not None:
            parts.append(lead)
            continue
        if _plan_without_five_hour(name, status, windows):
            parts.append(f"Codex week {_window_text(windows.get('seven_day'), now, name=name)}")
            continue
        five = _window_text(windows.get("five_hour"), now, name=name)
        if full is not None:
            five += f", full by about {local_clock(full, now)}"
        parts.append(f"{label} 5h {five}")
    return " | ".join(parts) + _handoff_suffix(statuses, thresholds, now)
