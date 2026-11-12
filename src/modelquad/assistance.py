# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Content-free quota facts, bounded advice and the near-limit note.

Pure: no collection, provider calls, persistence or worker launch. Windows are
copied individually from the existing normalized snapshots. Missing data never
becomes zero, and an original observation time is never replaced by collection
time.

Quota never orders, admits or refuses a worker, and alone it starts nothing.
It may produce :func:`handoff_suggestion`, a note that Claude is near its
limit, with the facts, and that the user may wait for the reset or ask Codex
for a read-only review; and the same thresholds let ``split.quota_lean``
propose a medium or large read-only step for Codex in a ``modelquad_split``
reply. The user decides. Neither can authorize or start anything: a job
starts only when Claude makes a Codex call and Claude Code's approval prompt
(or the user's standing grant) allows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math

from .models import Freshness
from .providers.base import (
    finite_float, freshness_for, parse_percentage, parse_timestamp, reset_at_from_delay,
)

# Provider collector provenance is a closed label, not arbitrary metadata.
SOURCES = frozenset({"claude_statusline", "codex_app_server", "synthetic"})
WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10080}
WINDOW_NAMES = {"five_hour": "5-hour window", "seven_day": "weekly window"}

# Used percentage of a Claude window at which a reply notes that Claude is
# near its limit. The weekly window gets a higher bar: at 80% of a week there
# are usually days left, while 80% of five hours is about one more long task.
HANDOFF_USED_PERCENT = {"five_hour": 80.0, "seven_day": 90.0}
# A Codex window at or above this used percentage makes the note moot:
# mentioning a Codex review when Codex is nearly out would only be noise.
HANDOFF_CODEX_EXHAUSTED_USED = 90.0
# When Claude's 5-hour window resets within this many minutes, the note says
# only that waiting may be simpler; the weekly window never does.
HANDOFF_WAIT_MINUTES = 30
# A pace estimate needs two readings at least this far apart, and growth.
PACE_MIN_SECONDS = 600
#: Before the projected time a window fills, on both status line forms.
PACE_WORDS = "at this pace full by about"


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
    freshness = freshness_for(observed, thresholds, now)
    stale = freshness in (Freshness.STALE, Freshness.VERY_STALE)
    # No time, or a time ahead of the clock: the reading's age is unknown, so
    # its figures are not presented as current anywhere.
    undated = observed is None or freshness is Freshness.UNKNOWN
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
                        "unknown": undated or (used is None and remaining is None)})

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


def _used_whole(used):
    """Used percent as a whole number for display, rounded down.

    Rounding to nearest would say "90%" for 89.99 while the rule that acts at
    90 has not acted; rounded down, the shown number crosses a threshold only
    when the reading did.
    """
    return max(0, min(100, math.floor(round(used, 6))))


def _left_whole(used):
    """Percent left as a whole number: 0 only when nothing is left, 100 only when nothing is used."""
    left = 100 - _used_whole(used)
    return 99 if left == 100 and used > 0 else left


def _window_phrase(window, now):
    name = WINDOW_NAMES.get(window["window"], "window")
    text = f"{_used_whole(_used(window))}% of its {name}"
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


#: What the model reads beside the note: it informs, it never starts anything.
NOTE_STARTS_NOTHING = (
    "Nothing starts from this note. Show it to the user in one line. Call Codex for a "
    "read-only review because of this note only if the user asks for one.")


def handoff_suggestion(statuses, thresholds, *, now):
    """A note that Claude is near its limit, for the user to decide on; or None.

    Pure and side-effect free: it reads normalized statuses and returns a
    dictionary. Returned when a readable Claude window is at or above its
    ``HANDOFF_USED_PERCENT``. The note states the facts (which window, how
    much is used, when it resets) and that the user may wait for the reset or
    ask Codex for a read-only review (``"to": "codex"``). When only the
    5-hour window is over and it resets within ``HANDOFF_WAIT_MINUTES``, it
    says only that waiting may be simpler (``"to": "wait"``). When Codex is
    known to be nearly out itself there is no note. Unknown Claude quota never
    produces one; unknown Codex quota is stated as unknown, never as zero.
    ``launches`` is always False: the note carries no authority, and a job
    starts only when the user asks for a Codex call and approves it.
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
    codex = by_name.get("codex")
    codex_windows = [w for w in (quota_windows(codex, thresholds, now=now) if codex else [])
                     if w["window"] in WINDOW_MINUTES and _readable(w, now)]
    codex_window = max(codex_windows, key=lambda w: (_used(w), w["window"] == "five_hour"), default=None)
    if codex_window is not None and _used(codex_window) >= HANDOFF_CODEX_EXHAUSTED_USED:
        return None
    if all(w["window"] == "five_hour" for w in over):
        resets = _moment(claude_window.get("resets_at"))
        if resets is not None and resets - now <= timedelta(minutes=HANDOFF_WAIT_MINUTES):
            return {
                "suggest": True,
                "to": "wait",
                "message": (f"Claude is at {_used_whole(_used(claude_window))}% of its 5-hour window, "
                            f"which resets at {local_clock(resets, now)}. Waiting for the "
                            "reset may be simpler. You decide."),
                "claude_window": _window_view(claude_window),
                "codex_window": None,
                "threshold_used_percent": HANDOFF_USED_PERCENT["five_hour"],
                "approval": NOTE_STARTS_NOTHING,
                "launches": False,
            }
    codex_text = (f"Codex is at {_window_phrase(codex_window, now)}." if codex_window is not None
                  else "Codex quota is unknown.")
    message = (f"Claude is at {_window_phrase(claude_window, now)}. {codex_text} "
               "You can wait for the reset, or ask Codex for a read-only review. You decide.")
    return {
        "suggest": True,
        "to": "codex",
        "message": message,
        "claude_window": _window_view(claude_window),
        "codex_window": _window_view(codex_window) if codex_window is not None else None,
        "threshold_used_percent": HANDOFF_USED_PERCENT[claude_window["window"]],
        "approval": NOTE_STARTS_NOTHING,
        "launches": False,
    }


# --- plain-language status ------------------------------------------------------

# One wording for both tools, on every quota surface: "<Tool> 5h <state> / 7d
# <state>", Claude first, a whole-tool state as "<Tool>: <state>", and an old
# reading as "(as of 14:02)" once at the end of that tool's part.
FIRST_MESSAGE = "shows after your first message"
NEXT_MESSAGE = "shows after your next message"
NO_FIVE_HOUR = "no 5-hour limit on your plan"
SIGN_IN = "not signed in (run codex login)"
CODEX_NOT_FOUND = "not found"
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
    """Codex answered with windows, and none of them can be a 5-hour window.

    A window whose length Codex did not state in a form ModelQuad recognises
    might be the 5-hour one, so with such a window the plan is not known.
    """
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    native = metadata.get("windows")
    if not isinstance(native, (list, tuple)):
        return False
    observed = [w for w in native if isinstance(w, Mapping)
                and parse_percentage(w.get("used_percentage")) is not None]
    return bool(observed) and all(
        w.get("slot") in WINDOW_MINUTES and w.get("slot") != "five_hour" for w in observed)


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
        if not isinstance(key, str) or not isinstance(first, Mapping):
            continue
        resets = _moment(first.get("resets_at"))
        # An entry that cannot be read (a hand edit, a torn write) is dropped
        # here, so the current reading takes its place below.
        if (resets is not None and resets > now and parse_percentage(first.get("used")) is not None
                and _moment(first.get("observed_at")) is not None):
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
                and parse_percentage(first.get("used")) <= _used(window)
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


#: What a window shows: its figure, nothing, or nothing because it has reset.
VALUE, UNKNOWN, RESET_PASSED = "value", "unknown", "reset_passed"
RESET_PASSED_REASON = "A quota window has reset since this reading; the current figure is not known yet."
UNTRUSTED_TIME_REASON = "The reading's time is ahead of this computer's clock, so its figures are not shown."


def window_state(window, now):
    """The one decision every surface shares about a window.

    ``VALUE``: the figure is shown (an old reading is still shown, marked as
    old). ``UNKNOWN``: there is no figure, or its time cannot be trusted.
    ``RESET_PASSED``: the figure describes a window that has since reset.
    """
    if window is None or window.get("unknown") or _used(window) is None:
        return UNKNOWN
    resets = _moment(window.get("resets_at"))
    return VALUE if resets is None or resets > now else RESET_PASSED


def _shows_number(window, now):
    """The window's figure is shown: known, and its window has not reset."""
    return window_state(window, now) == VALUE


def _one_decimal(value):
    return None if value is None else round(value, 1)


def shown_status(status, thresholds, *, now):
    """One provider as ``modelquad status`` and the status tool show it.

    Built from the same windows and the same :func:`window_state` as the
    status line, so the surfaces cannot disagree: ``remaining_percentage`` is
    the tightest shown window, and it is None when any window has reset since
    the reading, when the reading's time cannot be trusted, or when there is
    no reading. Figures are rounded to one decimal; ``used_percentage`` is
    always ``100 - remaining_percentage``. Carries no path and no plan name.
    """
    now = _moment(now)
    observed = _moment(status.observed_at)
    freshness = freshness_for(observed, thresholds, now) if observed is not None else status.freshness
    windows, shown, passed = [], [], False
    for window in quota_windows(status, thresholds, now=now):
        state = window_state(window, now)
        remaining = round(100.0 - _used(window), 1) if state == VALUE else None
        if remaining is not None:
            shown.append(remaining)
        passed = passed or state == RESET_PASSED
        windows.append({
            "window": window["window"], "state": state,
            "remaining_percentage": remaining,
            "used_percentage": None if remaining is None else round(100.0 - remaining, 1),
            "reset_at": window["resets_at"],
            "freshness": freshness.value if state == VALUE else Freshness.UNKNOWN.value})
    reason = status.reason
    if not status.available:
        remaining = None
    elif passed:
        remaining, freshness, reason = None, Freshness.UNKNOWN, RESET_PASSED_REASON
    elif observed is not None and freshness is Freshness.UNKNOWN:
        remaining, reason = None, UNTRUSTED_TIME_REASON
    elif windows:
        remaining = min(shown) if shown else None
    else:
        # A status that carries only a headline figure and no window.
        remaining = _one_decimal(status.remaining_percentage)
    if remaining is None and reason is None:
        reason = "No current quota reading."
    return {"name": status.name, "available": status.available,
            "remaining_percentage": remaining,
            "observed_at": observed.isoformat() if observed else None,
            "freshness": freshness.value, "reason": reason, "windows": windows}


def shown_status_json(status, thresholds, *, now):
    """``status --json`` for one provider: the full record, with the shown figures.

    The provider's own facts stay under ``metadata`` and each pool's ``raw``;
    every headline figure (the status, its capacity, its pools) is the one
    :func:`shown_status` shows, so none of them presents a figure the status
    line would not.
    """
    view = shown_status(status, thresholds, now=now)
    remaining = view["remaining_percentage"]
    freshness = Freshness(view["freshness"])
    capacity = status.capacity
    if capacity is not None:
        capacity = None if remaining is None else replace(
            capacity, freshness=freshness,
            remaining_percentage=_one_decimal(capacity.remaining_percentage),
            used_percentage=_one_decimal(capacity.used_percentage))
    pools = tuple(replace(pool, freshness=freshness, reason=view["reason"],
                          remaining_percentage=(None if remaining is None
                                                else _one_decimal(pool.remaining_percentage)))
                  for pool in status.pools)
    shown = replace(status, remaining_percentage=remaining, freshness=freshness,
                    reason=view["reason"], capacity=capacity, pools=pools)
    return shown.to_dict() | {"windows": view["windows"]}


def _window_text(window, now, *, name):
    """"38% left", or the plain-language state; never a made-up number.

    The line says what is left, not what is used: "0% used" on a fresh
    window read to the owner as nothing left (live test, 2026-10-25). An old
    reading is said once per tool, by :func:`_as_of`."""
    state = window_state(window, now)
    if state == RESET_PASSED:
        # The reading describes a window that has reset.
        return NEXT_MESSAGE if name == "claude" else "unknown"
    if state != VALUE:
        return "unknown"
    return f"{_left_whole(_used(window))}% left"


def _as_of(windows, now):
    """" (as of 14:02)" when a figure shown for this tool is an old reading, else ""."""
    old = [window for window in windows
           if window is not None and window.get("stale") and _shows_number(window, now)]
    if not old:
        return ""
    known = [moment for moment in (_moment(window.get("observed_at")) for window in old)
             if moment is not None]
    return f" (as of {local_clock(min(known), now)})" if known else " (old reading)"


def _lead_state(name, status, windows):
    """A whole-provider state that replaces both windows, or None."""
    if name == "codex":
        state = _codex_state(status)
        if state == CODEX_NOT_FOUND:
            return f"Codex: {CODEX_NOT_FOUND}"
        if state is not None and not any(_used(w) is not None for w in windows.values()):
            return f"Codex: {state}"
        return None
    if status is None or not any(_used(w) is not None for w in windows.values()):
        return f"Claude: {FIRST_MESSAGE}"
    return None


def _handoff_suffix(statuses, thresholds, now):
    """The near-limit note at the end of the status line, or ""."""
    suggestion = handoff_suggestion(statuses, thresholds, now=now)
    if suggestion is None:
        return ""
    window = suggestion["claude_window"]
    label = "5h" if window["window"] == "five_hour" else "7d"
    resets = local_clock(window.get("resets_at"), now)
    facts = f" | Claude {label} near its limit" + (f", resets {resets}" if resets else "")
    if suggestion["to"] == "wait":
        return f"{facts}: waiting may be simpler"
    return f"{facts}: you can wait, or ask Codex for a review"


def statusline_text(statuses, thresholds, *, now, pace=None):
    """One plain-ASCII line for Claude Code's status line.

    Claude's and Codex's 5-hour and weekly windows from the local readings,
    with the 5-hour reset in local time and, when ``pace`` holds an earlier
    reading of the same window, when it fills at that pace. A missing reading
    is said in plain words ("shows after your first message", "not signed in
    (run codex login)", "not found", "no 5-hour limit on your plan") or
    "unknown", never as 0%, and an old reading ends that tool's part with
    "(as of 14:02)". When Claude is near its limit the line ends with that fact
    and the user's options (wait, or ask Codex for a review), nothing more:
    the status line cannot start a job.
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
        seven_window = windows.get("seven_day")
        seven = _window_text(seven_window, now, name=name)
        if _plan_without_five_hour(name, status, windows):
            parts.append(f"Codex: {NO_FIVE_HOUR} / 7d {seven}{_as_of([seven_window], now)}")
            continue
        five_window = windows.get("five_hour")
        five = _window_text(five_window, now, name=name)
        resets = _moment((five_window or {}).get("resets_at"))
        if five.endswith("left") and resets is not None:
            five += f", resets {local_clock(resets, now)}"
        if full is not None:
            five += f", {PACE_WORDS} {local_clock(full, now)}"
        parts.append(f"{label} 5h {five} / 7d {seven}{_as_of([five_window, seven_window], now)}")
    return " | ".join(parts) + _handoff_suffix(statuses, thresholds, now)


def statusline_segment(statuses, thresholds, *, now, pace=None):
    """The compact ModelQuad part for a status line the user already has.

    For example "Claude 5h 38% left | Codex 5h 70% left": each provider's
    5-hour window (or its weekly one when the plan has no 5-hour limit), in
    the same order and words as :func:`statusline_text`, and the near-limit
    note when it applies. Plain ASCII, so any status line can append it.
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
        if _plan_without_five_hour(name, status, windows):
            seven_window = windows.get("seven_day")
            parts.append(f"Codex 7d {_window_text(seven_window, now, name=name)}"
                         f"{_as_of([seven_window], now)}")
            continue
        five_window = windows.get("five_hour")
        five = _window_text(five_window, now, name=name)
        if full is not None:
            five += f", {PACE_WORDS} {local_clock(full, now)}"
        parts.append(f"{label} 5h {five}{_as_of([five_window], now)}")
    return " | ".join(parts) + _handoff_suffix(statuses, thresholds, now)
