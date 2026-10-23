# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""OSS v1 step 5: quota is display-only, and a low-quota suggestion only asks.

The suggestion is a sentence in a reply. These tests pin that it is shown at
the agreed thresholds, that missing data reads as unknown, and above all that
nothing starts until the user approves inside Claude Code: an accepted
elicitation of ``modelquad_handoff``, or a delegation the user asked for.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from modelquad import assistance
from modelquad.assistance import handoff_suggestion, statusline_text
from modelquad.config import Config
from modelquad.execution import ExecutionManager
from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
from modelquad.mcp import server as mcp
from modelquad.models import Freshness, FreshnessThresholds, ProviderStatus
from modelquad.router import Router
from modelquad.service import db


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
THRESHOLDS = FreshnessThresholds()


def reading(name, *, five=None, seven=None, observed=NOW, five_reset=80, seven_reset=60 * 24 * 3, base=NOW):
    """A normalized status built the way the collectors publish one.

    Reset times are relative to ``base``; a fixture read against the real
    clock passes the real ``now``, or its windows reset as the day goes on.
    """
    metadata = {"source": "claude_statusline" if name == "claude" else "codex_app_server"}
    if five is not None:
        metadata["five_hour_used_percentage"] = five
        metadata["five_hour_resets_at"] = (base + timedelta(minutes=five_reset)).isoformat()
    if seven is not None:
        metadata["seven_day_used_percentage"] = seven
        metadata["seven_day_resets_at"] = (base + timedelta(minutes=seven_reset)).isoformat()
    used = [value for value in (five, seven) if value is not None]
    return ProviderStatus(
        name=name, available=True,
        remaining_percentage=100.0 - max(used) if used else None,
        observed_at=observed, freshness=Freshness.FRESH if observed else Freshness.UNKNOWN,
        metadata=metadata,
    )


# --- the suggestion itself -----------------------------------------------------


def test_suggestion_wording_at_the_five_hour_threshold():
    suggestion = handoff_suggestion(
        [reading("claude", five=85, seven=40), reading("codex", five=30, five_reset=180)],
        THRESHOLDS, now=NOW)
    assert suggestion["message"] == (
        "Claude is at 85% of its 5-hour window (resets in 1h 20m); hand this task to Codex? "
        "Codex is at 30% of its 5-hour window (resets in 3h 00m).")
    assert suggestion["to"] == "codex"
    assert suggestion["threshold_used_percent"] == 80.0
    assert suggestion["launches"] is False


@pytest.mark.parametrize("five,seven,expected", [
    (79, None, None), (80, None, "five_hour"), (None, 89, None), (None, 90, "seven_day"),
    (82, 95, "seven_day"), (0, 0, None),
])
def test_thresholds_are_80_for_five_hours_and_90_for_the_week(five, seven, expected):
    suggestion = handoff_suggestion([reading("claude", five=five, seven=seven)], THRESHOLDS, now=NOW)
    if expected is None:
        assert suggestion is None
    else:
        assert suggestion["claude_window"]["window"] == expected


def test_unknown_claude_quota_never_suggests():
    assert handoff_suggestion([reading("claude", observed=None)], THRESHOLDS, now=NOW) is None
    assert handoff_suggestion([reading("codex", five=10)], THRESHOLDS, now=NOW) is None


def test_a_reading_past_its_reset_says_nothing():
    old = reading("claude", five=95, five_reset=-5)
    assert handoff_suggestion([old], THRESHOLDS, now=NOW) is None


def test_a_stale_reading_still_counts_inside_its_window():
    """Usage only grows inside a window: an older used percentage is a floor."""
    stale = reading("claude", five=88, observed=NOW - timedelta(hours=2), five_reset=40)
    suggestion = handoff_suggestion([stale], THRESHOLDS, now=NOW)
    assert suggestion is not None and suggestion["claude_window"]["stale"] is True


def test_unknown_codex_quota_is_stated_as_unknown_never_zero():
    suggestion = handoff_suggestion([reading("claude", five=90), reading("codex", observed=None)],
                                    THRESHOLDS, now=NOW)
    assert suggestion["message"].endswith("Codex quota is unknown.")
    assert suggestion["codex_window"] is None
    assert "Codex is at" not in suggestion["message"]


def test_no_suggestion_when_codex_is_nearly_out_too():
    assert handoff_suggestion([reading("claude", five=90), reading("codex", five=95)],
                              THRESHOLDS, now=NOW) is None


# --- the status line -----------------------------------------------------------


def clock(minutes):
    return assistance.local_clock(NOW + timedelta(minutes=minutes), NOW)


def test_status_line_shows_both_providers_and_unknown_never_zero():
    line = statusline_text([reading("claude", five=85, seven=40), reading("codex", five=30)],
                           THRESHOLDS, now=NOW)
    assert line == (f"Claude 5h 85% used, resets {clock(80)} / 7d 40% used"
                    f" | Codex 5h 30% used, resets {clock(80)} / 7d unknown"
                    " | Claude is low: hand off to Codex?")
    empty = statusline_text([reading("claude", observed=None)], THRESHOLDS, now=NOW)
    assert empty == "Claude: shows after your first message | Codex 5h unknown / 7d unknown"
    assert line.isascii() and empty.isascii()


def test_reset_times_are_local_and_name_the_day_when_it_is_not_today():
    assert assistance.local_clock(NOW, NOW) == NOW.astimezone().strftime("%H:%M")
    later = NOW + timedelta(days=3)
    assert assistance.local_clock(later, NOW) == later.astimezone().strftime("%a %H:%M")
    assert assistance.local_clock(None, NOW) is None


def codex_status(*, reason=None, available=True, windows=None, seven=None):
    metadata = {"source": "codex_app_server"}
    if windows is not None:
        metadata["windows"] = windows
    if seven is not None:
        metadata["seven_day_used_percentage"] = seven
        metadata["seven_day_resets_at"] = (NOW + timedelta(days=3)).isoformat()
    return ProviderStatus(name="codex", available=available, reason=reason,
                          observed_at=NOW if seven is not None else None,
                          freshness=Freshness.FRESH if seven is not None else Freshness.UNKNOWN,
                          metadata=metadata)


@pytest.mark.parametrize(("status", "full", "segment"), [
    (codex_status(reason="codex CLI not found on PATH (looked for 'codex').", available=False),
     "Codex not found", "Codex not found"),
    (codex_status(reason="codex app-server rate-limit read failed: codex app-server returned an "
                         "error: {'message': 'Not logged in. Run codex login.'}"),
     "Codex: sign in to Codex", "Codex: sign in to Codex"),
    (codex_status(windows=[{"slot": "seven_day", "used_percentage": 40.0, "window_minutes": 10080}], seven=40),
     "Codex: no 5-hour limit on your plan / 7d 40% used", "Codex week 40% used"),
])
def test_plain_language_codex_states(status, full, segment):
    claude = reading("claude", five=20, seven=10)
    assert statusline_text([claude, status], THRESHOLDS, now=NOW).endswith(f" | {full}")
    assert assistance.statusline_segment([claude, status], THRESHOLDS, now=NOW) == (
        f"{segment} | Claude 5h 20% used")


def test_a_missing_codex_usage_file_is_not_called_codex_not_found():
    status = codex_status(reason="Usage file not found: C:/Users/me/.claude/usage-codex.json",
                          available=False)
    line = statusline_text([reading("claude", five=20), status], THRESHOLDS, now=NOW)
    assert line.endswith("| Codex 5h unknown / 7d unknown")


def test_a_reset_claude_window_waits_for_the_next_message():
    old = reading("claude", five=40, seven=10, five_reset=-5)
    assert statusline_text([old], THRESHOLDS, now=NOW).startswith(
        "Claude 5h shows after your next message / 7d 10% used")


def test_segment_is_compact_and_ascii():
    segment = assistance.statusline_segment(
        [reading("claude", five=62, seven=40), reading("codex", five=30)], THRESHOLDS, now=NOW)
    assert segment == "Codex 5h 30% used | Claude 5h 62% used"
    assert segment.isascii()
    assert assistance.statusline_segment([reading("claude", observed=None)], THRESHOLDS, now=NOW) == (
        "Codex 5h unknown | Claude: shows after your first message")


# --- the pace hint ---------------------------------------------------------------


def test_pace_needs_two_readings_and_says_when_the_window_fills():
    first = {"used": 20.0, "observed_at": (NOW - timedelta(minutes=60)).isoformat()}
    status = reading("claude", five=50, seven=10, five_reset=240)
    # 30 points an hour from 50%: full in 100 minutes, before the reset in 240.
    pace = {"claude:five_hour": first}
    line = statusline_text([status], THRESHOLDS, now=NOW, pace=pace)
    assert line.startswith(
        f"Claude 5h 50% used, resets {clock(240)}, at this pace full by about {clock(100)} / 7d 10% used")
    assert assistance.statusline_segment([status], THRESHOLDS, now=NOW, pace=pace).endswith(
        f"Claude 5h 50% used, full by about {clock(100)}")


@pytest.mark.parametrize("first", [
    None,
    {"used": 50.0, "observed_at": (NOW - timedelta(minutes=60)).isoformat()},   # no growth
    {"used": 49.0, "observed_at": (NOW - timedelta(minutes=5)).isoformat()},    # too close together
    {"used": 45.0, "observed_at": (NOW - timedelta(minutes=60)).isoformat()},   # fills after the reset
    {"used": "x", "observed_at": "not a time"},
])
def test_no_pace_hint_without_two_meaningful_readings(first):
    status = reading("claude", five=50, seven=10, five_reset=240)
    line = statusline_text([status], THRESHOLDS, now=NOW, pace={"claude:five_hour": first})
    assert "pace" not in line


def test_the_first_reading_of_a_window_is_kept_until_it_resets():
    earlier = reading("claude", five=20, observed=NOW - timedelta(minutes=30), five_reset=110,
                      base=NOW - timedelta(minutes=30))
    kept = assistance.next_pace_readings({}, [earlier], THRESHOLDS, now=NOW - timedelta(minutes=30))
    assert kept["claude:five_hour"]["used"] == 20.0
    later = reading("claude", five=35, five_reset=80)
    assert assistance.next_pace_readings(kept, [later], THRESHOLDS, now=NOW) == kept
    # A new window (different reset) starts over; a reset window is dropped.
    moved = reading("claude", five=5, five_reset=300)
    assert assistance.next_pace_readings(kept, [moved], THRESHOLDS, now=NOW)["claude:five_hour"]["used"] == 5.0
    assert assistance.next_pace_readings(kept, [], THRESHOLDS, now=NOW + timedelta(hours=3)) == {}


def test_the_status_line_command_records_the_pace_reading(tmp_path, monkeypatch, capsys):
    import io
    import json

    from modelquad import cli

    usage = tmp_path / "usage"
    usage.mkdir()
    reset = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    payload = {"rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": reset}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["--usage-directory", str(usage), "usage-hook", "claude", "--quiet"]) == 0
    assert capsys.readouterr().out == ""
    stored = json.loads((usage / "quota-pace.json").read_text(encoding="utf-8"))
    assert stored["claude:five_hour"]["used"] == 40.0


def test_status_line_command_prints_both_providers(tmp_path, capsys, monkeypatch):
    import io
    import json
    import time

    from modelquad import cli

    usage = tmp_path / "usage"
    usage.mkdir()
    stamp = datetime.now(timezone.utc)
    (usage / "usage-codex.json").write_text(json.dumps({
        "provider": "codex", "source": "codex_app_server", "observed_at": stamp.isoformat(),
        "five_hour_used_percentage": 30, "five_hour_remaining_percentage": 70,
    }), encoding="utf-8")
    now = int(time.time())
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 85, "resets_at": now + 3600},
        "seven_day": {"used_percentage": 40, "resets_at": now + 300000},
    }}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["--usage-directory", str(usage), "usage-hook", "claude"]) == 0
    resets = assistance.local_clock(datetime.fromtimestamp(now + 3600, timezone.utc),
                                    datetime.fromtimestamp(now, timezone.utc))
    assert capsys.readouterr().out.strip() == (
        f"Claude 5h 85% used, resets {resets} / 7d 40% used | Codex 5h 30% used / 7d unknown"
        " | Claude is low: hand off to Codex?")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["--usage-directory", str(usage), "usage-hook", "claude", "--segment"]) == 0
    assert capsys.readouterr().out.strip() == (
        "Codex 5h 30% used | Claude 5h 85% used | Claude is low: hand off to Codex?")


# --- waiting may be simpler ---------------------------------------------------------


@pytest.mark.parametrize("minutes", [1, 30])
def test_a_five_hour_window_about_to_reset_suggests_waiting(minutes):
    statuses = [reading("claude", five=92, seven=40, five_reset=minutes), reading("codex", five=10)]
    suggestion = handoff_suggestion(statuses, THRESHOLDS, now=NOW)
    assert suggestion["to"] == "wait"
    assert suggestion["message"] == (
        f"Claude's 5-hour window resets at {clock(minutes)}; waiting may be simpler.")
    assert suggestion["launches"] is False
    assert statusline_text(statuses, THRESHOLDS, now=NOW).endswith(
        f" | Claude is low: resets {clock(minutes)}, waiting may be simpler")


def test_a_reset_more_than_half_an_hour_away_still_suggests_codex():
    suggestion = handoff_suggestion([reading("claude", five=92, five_reset=31)], THRESHOLDS, now=NOW)
    assert suggestion["to"] == "codex"


def test_the_weekly_limit_still_suggests_codex_even_when_the_five_hour_resets_soon():
    statuses = [reading("claude", five=95, seven=93, five_reset=10), reading("codex", five=10)]
    suggestion = handoff_suggestion(statuses, THRESHOLDS, now=NOW)
    assert suggestion["to"] == "codex"
    assert "hand this task to Codex?" in suggestion["message"]


# --- quota never chooses ---------------------------------------------------------


@pytest.mark.parametrize("claude,codex", [(2, 95), (95, 2), (50, 50), (None, None), (100, 0)])
def test_quota_never_changes_the_advice(claude, codex):
    def status(name, remaining):
        return ProviderStatus(name=name, available=True, remaining_percentage=remaining,
                              observed_at=NOW if remaining is not None else None,
                              freshness=Freshness.FRESH if remaining is not None else Freshness.UNKNOWN)
    baseline = Router(Config()).route("review", [status("claude", None), status("codex", None)], now=NOW)
    result = Router(Config()).route("review", [status("claude", claude), status("codex", codex)], now=NOW)
    assert result.recommendation == baseline.recommendation
    assert [c.score for c in result.providers] == [c.score for c in baseline.providers]
    assert all(c.usage_score is None and c.quota_penalty is None for c in result.providers)


class _Worker:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def is_available(self):
        return True

    def execute(self, request):
        self.calls.append(request)
        return ExecutionResult(provider=self.name, success=True, output="done", exit_code=0,
                               task_status=WorkerTaskStatus.COMPLETED)


@pytest.mark.parametrize("claude,codex", [(1, 99), (99, 1), (None, 3)])
def test_unnamed_delegation_runs_the_default_worker_whatever_the_quota(tmp_path, claude, codex):
    codex_worker, other = _Worker("codex"), _Worker("claude")
    manager = ExecutionManager(Config(), executors={"codex": codex_worker, "claude": other})
    statuses = [ProviderStatus(name="claude", available=True, remaining_percentage=claude,
                               freshness=Freshness.FRESH, observed_at=NOW),
                ProviderStatus(name="codex", available=True, remaining_percentage=codex,
                               freshness=Freshness.FRESH, observed_at=NOW)]
    result = manager.delegate("review", "task", tmp_path, statuses=statuses, now=NOW)
    assert len(codex_worker.calls) == 1 and other.calls == []
    assert result.metadata["worker_source"] == "default"
    assert result.metadata["attempted_providers"] == ["codex"]
    assert result.metadata["fallback"] is False
    assert not {"fallback_from", "skipped_providers"} & result.metadata.keys()


def test_no_substitute_when_the_default_worker_cannot_run(tmp_path):
    other = _Worker("claude")
    manager = ExecutionManager(Config(), executors={"claude": other})
    result = manager.delegate("implementation", "task", tmp_path,
                              statuses=[ProviderStatus(name="codex", available=True)], now=NOW)
    assert other.calls == []
    assert result.task_status is WorkerTaskStatus.BLOCKED
    assert result.task_status_reason == "no_viable_worker"


# --- a suggestion never starts a job ---------------------------------------------


@pytest.fixture
def low_claude_world(tmp_path, monkeypatch):
    """Claude at 95% of its 5-hour window; every way to start work is a tripwire."""
    path = tmp_path / "state.db"
    db.connect_and_init(path).close()
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    now = datetime.now(timezone.utc)
    statuses = [reading("claude", five=95, seven=50, observed=now, base=now),
                reading("codex", five=20, observed=now, base=now)]
    providers = [SimpleNamespace(name=s.name, get_status=lambda s=s: s) for s in statuses]
    monkeypatch.setattr(mcp, "load_config", lambda *a: Config())
    monkeypatch.setattr(mcp, "build_providers", lambda *a, **k: providers)
    monkeypatch.setattr(mcp, "cached_catalog", lambda *a: None)
    started = []

    def tripwire(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("a suggestion started work")

    monkeypatch.setattr(mcp, "_delegate_payload", tripwire)
    monkeypatch.setattr(mcp, "start_job", tripwire)
    monkeypatch.setattr(mcp, "_build_execution_manager", tripwire)
    monkeypatch.setattr(ExecutionManager, "delegate", tripwire)
    return started


def test_status_replies_suggest_but_start_nothing(low_claude_world):
    status = mcp._provider_status_payload()
    assert status["handoff_suggestion"]["message"].startswith(
        "Claude is at 95% of its 5-hour window")
    assert status["handoff_suggestion"]["to"] == "codex"
    assert status["handoff_suggestion"]["launches"] is False
    assert low_claude_world == []


class _Ctx:
    """A request context whose client answers the elicitation as scripted."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    async def elicit(self, message, schema):
        self.asked.append((message, schema))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("answer,expected", [
    (SimpleNamespace(action="decline", data=None), "decline"),
    (SimpleNamespace(action="cancel", data=None), "cancel"),
    (SimpleNamespace(action="accept", data=SimpleNamespace(hand_off=False)), "decline"),
    (RuntimeError("client does not support elicitation"), "unavailable"),
    (None, "unavailable"),
])
def test_handoff_starts_nothing_without_an_accepted_prompt(low_claude_world, monkeypatch, answer, expected):
    delegated = []

    async def fake_delegate(**kwargs):
        delegated.append(kwargs)

    monkeypatch.setattr(mcp, "modelquad_delegate", fake_delegate)
    ctx = None if answer is None else _Ctx(answer)
    result = asyncio.run(mcp.modelquad_handoff("review", "task", "C:/work/app", ctx=ctx))
    assert delegated == [] and low_claude_world == []
    assert result.structured_content["started"] is False
    assert result.structured_content["approval"] == expected
    assert result.structured_content["message"].startswith("Claude is at 95% of its 5-hour window")
    if ctx is not None:
        message, schema = ctx.asked[0]
        assert message.startswith("Claude is at 95% of its 5-hour window")
        assert "Nothing starts unless you tick the box and accept." in message
        assert schema is mcp.HandoffApproval


def test_an_accepted_prompt_starts_exactly_one_codex_delegation(low_claude_world, monkeypatch):
    delegated = []

    async def fake_delegate(**kwargs):
        delegated.append(kwargs)
        return "delegated"

    monkeypatch.setattr(mcp, "modelquad_delegate", fake_delegate)
    ctx = _Ctx(SimpleNamespace(action="accept", data=SimpleNamespace(hand_off=True)))
    assert asyncio.run(mcp.modelquad_handoff("review", "check it", "C:/work/app", ctx=ctx)) == "delegated"
    assert len(delegated) == 1
    call = delegated[0]
    # The delegate tool always runs read-only Codex; the caller cannot pick a
    # provider or assert its own approval or invocation mode.
    assert {"provider", "user_approved", "invocation_mode"}.isdisjoint(call)
    assert (call["task_type"], call["prompt"], call["working_directory"]) == ("review", "check it", "C:/work/app")


def test_a_writable_handoff_is_refused_before_the_user_is_asked(low_claude_world):
    ctx = _Ctx(SimpleNamespace(action="accept", data=SimpleNamespace(hand_off=True)))
    result = asyncio.run(mcp.modelquad_handoff("implementation", "task", "C:/work/app", ctx=ctx))
    assert ctx.asked == [] and low_claude_world == []
    assert result.structured_content["started"] is False
    assert result.structured_content["reason"] == "task_type_not_read_only"


def test_suggestion_module_has_no_launch_path():
    """The pure module cannot start anything: it imports no executor or job code."""
    import inspect

    source = inspect.getsource(assistance)
    for forbidden in ("ExecutionManager", "start_job", "_delegate_payload", "subprocess", "execute("):
        assert forbidden not in source
