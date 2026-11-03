# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""What happened after the work: the split summary for one project and period.

``modelquad split summary`` and the ``split`` block of ``modelquad_activity``
read ModelQuad's own history and nothing else:

* Codex: the requests ModelQuad recorded for this project in the period, by
  kind (review, exploration, architecture), and how each ended: answered,
  withheld because the event stream showed a tool call (invariant 8),
  refused (ModelQuad's own rules, the repository's ``.modelquad.toml``, the
  user's per-repo setting, or Codex's own limit), or not answered. The
  median time to an answer is over the answered ones.
* Claude: one sentence. ModelQuad sees only what goes to Codex, so it does
  not measure Claude's own work, and a decline in Claude Code's own approval
  prompt never reaches it. A decline in the handoff window is not stored
  either; the summary says so instead of counting it.
* Quota: for each provider and each of its own windows, the percentage
  points used between the first and the last reading of that window inside
  the period. Each provider and window is its own column: never added
  together, never converted into anything else. A window that reset during
  the period says so instead of a difference, and a window with fewer than
  two readings, or a reading without a reset time, reads unknown, never 0.

The readings are the rows of ``provider_usage_history`` observed inside the
period (the background service writes one on a timer; without it ModelQuad
writes one at each Codex request), plus the current stored reading of each
provider when it was observed inside the period.

Read-only: nothing is written, no process is started, and no path, folder
name, prompt or answer appears in the result. The project is selected by its
workspace key, the same hash the activity filter uses.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import assistance
from .models import Freshness, ProviderStatus

PERIODS = ("1d", "7d", "session")
DEFAULT_PERIOD = "1d"
_PERIOD_DAYS = {"1d": 1, "7d": 7}
_PERIOD_WORDS = {"1d": "the last day", "7d": "the last 7 days", "session": "this session"}

KINDS = ("review", "exploration", "architecture")
PROVIDERS = ("claude", "codex")
#: The windows each provider is expected to report; one it does not report
#: still gets its column, reading unknown.
EXPECTED_WINDOWS = ("five_hour", "seven_day")
_WINDOW_WORDS = {"five_hour": "5-hour", "seven_day": "weekly"}
_PROVIDER_WORDS = {"claude": "Claude", "codex": "Codex"}

ANSWERED = "answered"
WITHHELD = "withheld"
REFUSED_POLICY = "refused_policy"
REFUSED_REPO_FILE = "refused_repo_file"
REFUSED_USER_SETTING = "refused_user_setting"
REFUSED_CODEX_LIMIT = "refused_codex_limit"
NOT_ANSWERED = "not_answered"
RUNNING = "running"
OUTCOMES = (ANSWERED, WITHHELD, REFUSED_POLICY, REFUSED_REPO_FILE, REFUSED_USER_SETTING,
            REFUSED_CODEX_LIMIT, NOT_ANSWERED, RUNNING)
REFUSALS = (REFUSED_POLICY, REFUSED_REPO_FILE, REFUSED_USER_SETTING, REFUSED_CODEX_LIMIT)

MEASURED = "measured"
RESET = "reset_during_period"
UNKNOWN = "unknown"
#: Two readings belong to one window when their reset times agree this
#: closely (the same tolerance as the status line's pace estimate).
SAME_WINDOW_SECONDS = 120
#: Rounding noise below this is not a decrease.
_DECREASE_TOLERANCE = 0.5

CLAUDE_LINE = ("Claude: everything else in this period. ModelQuad does not measure Claude's "
               "own work (not measured).")
PROMPT_DECLINES_LINE = ("Declines in Claude Code's own approval prompt are not visible to "
                        "ModelQuad and are not counted.")
HANDOFF_DECLINES_LINE = "Declines in the handoff window: not recorded by ModelQuad."
QUOTA_NOTE = ("Each provider and window is shown on its own, in percentage points of that "
              "window; they are not added together.")
_USER_OFF = "user_repo_codex_off"
_REPO_FILE_PREFIX = "repo_policy_"
_SCAN_LIMIT = 5000


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def period_start(period: str, now: datetime, *, session_started_at: datetime | None = None) -> datetime | None:
    """The start of the period, or None when it is not known here (a CLI ``session``)."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    if period == "session":
        return None if session_started_at is None else _utc(session_started_at)
    return _utc(now) - timedelta(days=_PERIOD_DAYS[period])


# --- Codex side -------------------------------------------------------------

def _connect(db_path: str | Path) -> sqlite3.Connection | None:
    """A read-only connection, or None when there is no history yet."""
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _refusal_kind(code: Any) -> str:
    code = code if isinstance(code, str) else ""
    if code.startswith(_USER_OFF):
        return REFUSED_USER_SETTING
    if code.startswith(_REPO_FILE_PREFIX):
        return REFUSED_REPO_FILE
    return REFUSED_POLICY


def _codex_limit(execution: Mapping[str, Any]) -> bool:
    """Codex's own typed limit, read the way ``quota_refusal_evidence`` reads it."""
    if execution.get("task_status") == "completed":
        return False
    events = execution.get("rate_limit_events")
    if isinstance(events, int) and not isinstance(events, bool) and events > 0:
        return True
    return not execution.get("success") and execution.get("stderr_category") == "rate_limit"


def outcome_of(attempt: Mapping[str, Any], execution: Mapping[str, Any] | None) -> str:
    """How one recorded Codex request ended, as one closed word."""
    from .execution import noshell

    status = attempt.get("status")
    if status == "refused":
        return _refusal_kind(attempt.get("detail"))
    if execution is not None:
        reason = execution.get("task_status_reason")
        if reason == noshell.VIOLATED:
            return WITHHELD
        if isinstance(reason, str) and reason in noshell.PRELAUNCH_REFUSALS:
            return _refusal_kind(reason)
        if _codex_limit(execution):
            return REFUSED_CODEX_LIMIT
        if execution.get("task_status") == "completed":
            return ANSWERED
        return NOT_ANSWERED
    if status == "started" and attempt.get("resolved_at") is None:
        return RUNNING
    return NOT_ANSWERED


_EXECUTION_COLUMNS = (
    "task_status, task_status_reason, duration_seconds, success, "
    "json_extract(metadata_json, '$.worker_metrics.rate_limit_events') AS rate_limit_events, "
    "json_extract(metadata_json, '$.stderr_category') AS stderr_category"
)


def codex_requests(db_path: str | Path, *, workspace_key: str | None, since: datetime,
                   until: datetime) -> list[dict[str, Any]]:
    """One row per recorded Codex request in the period: kind, outcome, seconds."""
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        scope, params = "", [_utc(since).isoformat(), _utc(until).isoformat()]
        if workspace_key is not None:
            scope = " AND workspace_key = ?"
            params.append(workspace_key)
        attempts = conn.execute(
            "SELECT id, decision_id, task_type, status, detail, resolved_at, duration_seconds "
            "FROM delegation_attempts WHERE created_at >= ? AND created_at <= ?" + scope
            + " AND COALESCE(routed_provider, requested_provider, 'codex') = 'codex'"
            " ORDER BY id LIMIT ?", (*params, _SCAN_LIMIT),
        ).fetchall()
        rows = []
        for attempt in attempts:
            execution = None
            if attempt["decision_id"]:
                found = conn.execute(
                    f"SELECT {_EXECUTION_COLUMNS} FROM execution_history "
                    "WHERE decision_id = ? AND provider = 'codex' ORDER BY id DESC LIMIT 1",
                    (attempt["decision_id"],),
                ).fetchone()
                execution = dict(found) if found is not None else None
            outcome = outcome_of(dict(attempt), execution)
            seconds = (execution or {}).get("duration_seconds")
            if seconds is None:
                seconds = attempt["duration_seconds"]
            rows.append({"task_type": attempt["task_type"], "outcome": outcome,
                         "seconds": seconds if isinstance(seconds, (int, float)) else None})
        return rows
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def codex_block(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts by kind and by outcome, and the median time to an answer."""
    rows = list(rows)
    kinds = {kind: 0 for kind in KINDS}
    other = 0
    outcomes = {outcome: 0 for outcome in OUTCOMES}
    for row in rows:
        if row["task_type"] in kinds:
            kinds[row["task_type"]] += 1
        else:
            other += 1
        outcomes[row["outcome"]] += 1
    seconds = [row["seconds"] for row in rows
               if row["outcome"] == ANSWERED and row["seconds"] is not None]
    by_kind = dict(kinds)
    if other:
        by_kind["other"] = other
    return {
        "requests": len(rows),
        "by_kind": by_kind,
        "outcomes": outcomes,
        "refused": {outcome: outcomes[outcome] for outcome in REFUSALS},
        "median_answer_seconds": round(statistics.median(seconds), 1) if seconds else None,
        "handoff_window_declines": None,
        "handoff_window_declines_recorded": False,
        "approval_prompt_declines_visible": False,
    }


# --- Quota ------------------------------------------------------------------

def _parse_moment(value: Any) -> datetime | None:
    from .providers.base import parse_timestamp

    return parse_timestamp(value)


def _history_readings(db_path: str | Path, provider: str, since: datetime,
                      until: datetime) -> list[ProviderStatus]:
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        # A row is recorded at or after its observation, so every reading
        # observed in the period was recorded in it too.
        rows = conn.execute(
            "SELECT observed_at, remaining_percentage, freshness, available, metadata_json "
            "FROM provider_usage_history WHERE provider = ? "
            "AND (resource_pool IS NULL OR resource_pool = provider) "
            "AND observed_at IS NOT NULL AND recorded_at >= ? ORDER BY id DESC LIMIT ?",
            (provider, _utc(since).isoformat(), _SCAN_LIMIT),
        ).fetchall()[::-1]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    readings = []
    for row in rows:
        observed = _parse_moment(row["observed_at"])
        if observed is None or not since <= observed <= until:
            continue
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except (TypeError, ValueError):
            metadata = {}
        try:
            freshness = Freshness(row["freshness"])
        except ValueError:
            freshness = Freshness.UNKNOWN
        remaining = row["remaining_percentage"]
        if not (isinstance(remaining, (int, float)) and 0 <= remaining <= 100):
            remaining = None
        readings.append(ProviderStatus(
            name=provider, available=bool(row["available"]), remaining_percentage=remaining,
            observed_at=observed, freshness=freshness,
            metadata=metadata if isinstance(metadata, dict) else {}))
    return readings


def _points(readings: Iterable[ProviderStatus], thresholds) -> dict[str, list[tuple]]:
    """{window: [(observed_at, used, resets_at), ...]} in time order, one per moment."""
    points: dict[str, dict[datetime, tuple]] = {}
    for reading in readings:
        if reading.observed_at is None:
            continue
        observed = _utc(reading.observed_at)
        for window in assistance.quota_windows(reading, thresholds, now=observed):
            used = assistance._used(window)
            if used is None or window.get("unknown"):
                continue
            resets = _parse_moment(window.get("resets_at"))
            points.setdefault(window["window"], {})[observed] = (observed, float(used), resets)
    return {label: [by_time[moment] for moment in sorted(by_time)] for label, by_time in points.items()}


def window_delta(points: list[tuple]) -> dict[str, Any]:
    """Points used in one window between its first and last reading in the period."""
    base = {"readings": len(points), "first_reading_at": None, "last_reading_at": None}
    if points:
        base["first_reading_at"] = points[0][0].isoformat()
        base["last_reading_at"] = points[-1][0].isoformat()
    if len(points) < 2:
        why = "no_reading" if not points else "one_reading"
        return {**base, "state": UNKNOWN, "points": None, "why": why}
    if any(resets is None for _at, _used, resets in points):
        return {**base, "state": UNKNOWN, "points": None, "why": "no_reset_time"}
    for (_a_at, a_used, a_resets), (b_at, b_used, b_resets) in zip(points, points[1:]):
        moved = abs((b_resets - a_resets).total_seconds()) > SAME_WINDOW_SECONDS
        if moved or a_resets <= b_at or b_used < a_used - _DECREASE_TOLERANCE:
            return {**base, "state": RESET, "points": None, "why": "window_reset"}
    used = round(max(0.0, points[-1][1] - points[0][1]), 1)
    return {**base, "state": MEASURED, "points": used, "why": None}


def quota_block(db_path: str | Path, *, since: datetime, until: datetime,
                current: Iterable[ProviderStatus], thresholds) -> dict[str, Any]:
    """Per provider, per window: its own delta. No field adds providers together."""
    current = {status.name: status for status in current}
    providers: dict[str, Any] = {}
    for provider in PROVIDERS:
        readings = _history_readings(db_path, provider, since, until)
        source = "history" if readings else None
        now_reading = current.get(provider)
        if now_reading is not None and now_reading.observed_at is not None \
                and since <= _utc(now_reading.observed_at) <= until:
            readings.append(now_reading)
            source = source or "stored_reading"
        points = _points(readings, thresholds)
        labels = list(EXPECTED_WINDOWS) + sorted(set(points) - set(EXPECTED_WINDOWS))
        providers[provider] = {
            "source": source or "none",
            "windows": {label: window_delta(points.get(label, [])) for label in labels},
        }
    return {"unit": "percentage_points_of_each_window", "providers": providers,
            "added_together": False}


# --- The whole summary ------------------------------------------------------

def summarize(db_path: str | Path, *, workspace_key: str | None, period: str, now: datetime,
              current: Iterable[ProviderStatus], thresholds,
              session_started_at: datetime | None = None) -> dict[str, Any]:
    """The structured summary and its human lines (``lines``)."""
    now = _utc(now)
    since = period_start(period, now, session_started_at=session_started_at)
    scope = "this_project" if workspace_key is not None else "all_projects"
    if since is None:
        result = {"period": period, "scope": scope, "known": False,
                  "why": "session_start_unknown"}
        result["lines"] = ["The start of this session is known only inside Claude Code "
                           "(modelquad_activity). Use --since 1d or --since 7d here."]
        return result
    codex = codex_block(codex_requests(db_path, workspace_key=workspace_key, since=since, until=now))
    quota = quota_block(db_path, since=since, until=now, current=current, thresholds=thresholds)
    result = {
        "period": period, "scope": scope, "known": True,
        "since": since.isoformat(), "until": now.isoformat(),
        "codex": codex,
        "claude": {"measured": False, "text": CLAUDE_LINE},
        "quota": quota,
    }
    result["lines"] = render(result)
    return result


def _seconds_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown (no answer in the period)"
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest} s"


def cell_text(window: Mapping[str, Any]) -> str:
    """One quota cell: a measured number, a reset, or unknown. Never a bare 0 for unknown."""
    if window["state"] == MEASURED:
        return f"{window['points']:g} points"
    if window["state"] == RESET:
        return "reset during the period"
    return {"one_reading": "unknown (one reading)",
            "no_reset_time": "unknown (no reset time)"}.get(window.get("why"), "unknown (no readings)")


def quota_columns(quota: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(column header, cell) for each provider and window, in a fixed order."""
    columns = []
    for provider in PROVIDERS:
        for label, window in quota["providers"][provider]["windows"].items():
            name = _WINDOW_WORDS.get(label, label.replace("_", " "))
            columns.append((f"{_PROVIDER_WORDS[provider]} {name}", cell_text(window)))
    return columns


def render(summary: Mapping[str, Any]) -> list[str]:
    """The lines a person reads. Columns per provider and window; no totals."""
    codex = summary["codex"]
    outcomes, refused = codex["outcomes"], codex["refused"]
    where = "this project" if summary["scope"] == "this_project" else "all projects"
    since = datetime.fromisoformat(summary["since"]).astimezone()
    lines = [f"Split summary for {where}, {_PERIOD_WORDS[summary['period']]} "
             f"(since {since:%Y-%m-%d %H:%M})."]
    kinds = ", ".join(f"{kind} {count}" for kind, count in codex["by_kind"].items())
    lines.append(f"Codex, read-only, through ModelQuad: {codex['requests']} "
                 f"request{'s' if codex['requests'] != 1 else ''} ({kinds}).")
    lines.append(f"  Answered {outcomes[ANSWERED]}; withheld (tool call seen) {outcomes[WITHHELD]}; "
                 f"not answered {outcomes[NOT_ANSWERED]}"
                 + (f"; still running {outcomes[RUNNING]}" if outcomes[RUNNING] else "") + ".")
    lines.append(f"  Refused {sum(refused.values())}: ModelQuad's rules {refused[REFUSED_POLICY]}, "
                 f"repository file {refused[REFUSED_REPO_FILE]}, your repo setting "
                 f"{refused[REFUSED_USER_SETTING]}, Codex's own limit {refused[REFUSED_CODEX_LIMIT]}.")
    lines.append(f"  Median time to an answer: {_seconds_text(codex['median_answer_seconds'])}.")
    lines.append(f"  {HANDOFF_DECLINES_LINE}")
    lines.append(f"  {PROMPT_DECLINES_LINE}")
    lines.append(CLAUDE_LINE)
    columns = quota_columns(summary["quota"])
    widths = [max(len(header), len(cell)) for header, cell in columns]
    lines.append("Quota used in the period by each whole account (not only this project), "
                 "from ModelQuad's stored readings:")
    lines.append("  " + " | ".join(header.ljust(width) for (header, _), width in zip(columns, widths)))
    lines.append("  " + " | ".join(cell.ljust(width) for (_, cell), width in zip(columns, widths)))
    lines.append(f"  {QUOTA_NOTE}")
    return [line.rstrip() for line in lines]
