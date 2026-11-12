# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A quota figure is shown as current only when the reading supports it.

One reading, four surfaces: the status line, the near-limit note in an MCP
reply, ``modelquad status`` (table and JSON) and the ``modelquad_split``
reply. They must agree on whether a window has a current figure, an old one,
none because its window has reset, or none at all. A reading dated in the
future, a non-finite number and a lower figure inside the same window are
never presented as a current percentage.
"""

from __future__ import annotations

import io
import json
import math
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from modelquad import assistance, cli, plans, split, usage_hook
from modelquad.config import Config
from modelquad.mcp import server as mcp
from modelquad.models import FreshnessThresholds
from modelquad.providers.claude import ClaudeProvider
from modelquad.providers.codex import CodexProvider
from modelquad.router import Router
from modelquad.split_args import SplitStep
from modelquad.usage import refresh as usage_refresh
from modelquad.usage.claude import parse_claude_statusline_payload
from modelquad.usage.codex_appserver import parse_rate_limits_result

THRESHOLDS = FreshnessThresholds()


def _epoch(moment):
    return int(moment.timestamp())


def _claude_file(directory, now, *, five, seven=None, observed=timedelta(seconds=-10),
                 five_reset=timedelta(hours=2), seven_reset=timedelta(days=3)):
    stamp = (now + observed).isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "usage-claude.json").write_text(json.dumps({
        "provider": "claude", "source": "claude_statusline",
        "five_hour_used_percentage": five, "seven_day_used_percentage": seven,
        "five_hour_remaining_percentage": None if five is None else 100.0 - five,
        "seven_day_remaining_percentage": None if seven is None else 100.0 - seven,
        "five_hour_resets_at": _epoch(now + five_reset),
        "seven_day_resets_at": _epoch(now + seven_reset),
        "collected_at": stamp, "observed_at": stamp}), encoding="utf-8")


def _codex_file(directory, now, *, five, seven, observed=timedelta(seconds=-10),
                five_reset=timedelta(hours=3), seven_reset=timedelta(days=4)):
    stamp = (now + observed).isoformat()

    def window(used, minutes, slot, reset):
        return {"used_percentage": used, "remaining_percentage": round(100 - used, 2),
                "window_minutes": minutes, "resets_at": _epoch(now + reset), "slot": slot}

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "usage-codex.json").write_text(json.dumps({
        "provider": "codex", "available": True, "source": "codex_app_server",
        "five_hour_used_percentage": five, "five_hour_remaining_percentage": round(100 - five, 2),
        "five_hour_resets_at": _epoch(now + five_reset),
        "seven_day_used_percentage": seven, "seven_day_remaining_percentage": round(100 - seven, 2),
        "seven_day_resets_at": _epoch(now + seven_reset),
        "windows": [window(five, 300, "five_hour", five_reset),
                    window(seven, 10080, "seven_day", seven_reset)],
        "observed_at": stamp, "collected_at": stamp}), encoding="utf-8")


def _statuses(directory, now):
    return (ClaudeProvider(usage_directory=directory, now=lambda: now).get_status(),
            CodexProvider(usage_directory=directory, now=lambda: now).get_status())


def _case(name, directory, now):
    """Write the readings for one named state and return the statuses."""
    if name == "missing":
        directory.mkdir(parents=True, exist_ok=True)
    elif name == "normal":
        _claude_file(directory, now, five=85, seven=40)
        _codex_file(directory, now, five=10, seven=10)
    elif name == "reset_passed":
        _claude_file(directory, now, five=95, five_reset=timedelta(seconds=-30))
        _codex_file(directory, now, five=92, seven=10, five_reset=timedelta(seconds=-30))
    elif name == "stale":
        _claude_file(directory, now, five=85, seven=40, observed=timedelta(minutes=-20))
        _codex_file(directory, now, five=10, seven=10, observed=timedelta(minutes=-20))
    elif name == "future":
        _claude_file(directory, now, five=85, seven=40, observed=timedelta(hours=1))
        _codex_file(directory, now, five=10, seven=10, observed=timedelta(hours=1))
    return _statuses(directory, now)


EXPLORE = split.Step(title="Map the callers", task_type="exploration", needs="none", size="medium")


def _split_reply(statuses, now):
    return split.propose([EXPLORE], router=Router(Config()), statuses=statuses,
                         thresholds=THRESHOLDS, now=now, plan_views=plans.merged(None, {}),
                         policy=None, user_mode=None, global_proactive=False)


def _surfaces(statuses, now, monkeypatch, capsys):
    """What each of the four surfaces says about Claude's 5-hour window."""
    line = assistance.statusline_text(statuses, THRESHOLDS, now=now)
    providers = [SimpleNamespace(name=s.name, get_status=lambda s=s: s) for s in statuses]
    monkeypatch.setattr(mcp, "build_providers", lambda *a, **k: providers)
    payload = mcp._provider_status_payload()
    capsys.readouterr()
    cli.render_status(statuses)
    table = capsys.readouterr().out
    machine = [cli.status_json(status, THRESHOLDS, now) for status in statuses]
    reply = _split_reply(statuses, now)
    return SimpleNamespace(line=line, payload=payload, table=table, machine=machine, reply=reply)


def _row(table, name):
    return next(row for row in table.splitlines() if row.startswith(name))


def _five(provider_view):
    return next((w for w in provider_view["windows"] if w["window"] == "five_hour"), None)


#: case -> (figure shown for Claude's 5-hour window, note, a split step moves)
EXPECTED = {
    "normal": (15.0, True, True),
    "reset_passed": (None, False, False),
    "stale": (15.0, True, False),
    "future": (None, False, False),
    "missing": (None, False, False),
}


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_the_four_surfaces_agree_on_one_reading(case, tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    statuses = _case(case, tmp_path / "usage", now)
    shown, note, moves = EXPECTED[case]
    seen = _surfaces(statuses, now, monkeypatch, capsys)

    claude_part = seen.line.split(" | ")[0]
    mcp_claude = seen.payload["providers"][0]
    cli_claude = seen.machine[0]
    for view in (mcp_claude, cli_claude):
        five = _five(view)
        if shown is None:
            assert view["remaining_percentage"] is None, view
            assert five is None or five["remaining_percentage"] is None, view
            assert five is None or five["used_percentage"] is None, view
        else:
            assert five["remaining_percentage"] == shown
            assert five["used_percentage"] == 100.0 - shown
            assert view["remaining_percentage"] == shown
    if shown is None:
        assert "% left" not in claude_part
        assert "unknown" in _row(seen.table, "claude")
        assert "%" not in _row(seen.table, "claude")
    else:
        assert f"Claude 5h {shown:.0f}% left" in claude_part
        assert f"{shown:.1f}%" in _row(seen.table, "claude")
    assert seen.payload["summary"] == seen.line
    assert seen.line in seen.reply["content"]
    assert (seen.payload["handoff_suggestion"] is not None) is note
    assert ("near its limit" in seen.line) is note
    assert seen.reply["facts"]["changes_owners"] is moves

    states = {"normal": "value", "stale": "value", "reset_passed": "reset_passed",
              "future": "unknown"}
    if case in states:
        assert _five(mcp_claude)["state"] == _five(cli_claude)["state"] == states[case]
    if case == "stale":
        assert "(as of " in claude_part
        assert mcp_claude["freshness"] == cli_claude["freshness"] == "stale"
        assert "stale" in _row(seen.table, "claude")
    if case == "normal":
        assert mcp_claude["freshness"] == "fresh" and "fresh" in _row(seen.table, "claude")
    if case == "reset_passed":
        assert "shows after your next message" in claude_part
        codex = seen.payload["providers"][1]
        assert codex["remaining_percentage"] is None
        assert _five(codex)["remaining_percentage"] is None
        assert "Codex 5h unknown" in seen.line
        assert "%" not in _row(seen.table, "codex")
    if case == "future":
        assert claude_part == "Claude 5h unknown / 7d unknown"
        assert "(as of" not in seen.line


def test_status_json_from_the_command_uses_the_same_view(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    usage = tmp_path / "usage"
    _case("reset_passed", usage, now)
    assert cli.main(["--usage-directory", str(usage), "status", "--json"]) == 0
    claude, codex = json.loads(capsys.readouterr().out)
    for view in (claude, codex):
        assert view["remaining_percentage"] is None
        assert view["freshness"] == "unknown"
        assert _five(view)["state"] == "reset_passed"
        assert view["capacity"] is None
        assert all(pool["remaining_percentage"] is None for pool in view["pools"])


def test_machine_numbers_are_rounded_and_used_is_the_complement(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    usage = tmp_path / "usage"
    _claude_file(usage, now, five=79.6, seven=33.333)
    _codex_file(usage, now, five=12.34, seven=56.78)
    statuses = _statuses(usage, now)
    providers = [SimpleNamespace(name=s.name, get_status=lambda s=s: s) for s in statuses]
    monkeypatch.setattr(mcp, "build_providers", lambda *a, **k: providers)
    for view in (*mcp._provider_status_payload()["providers"],
                 *(cli.status_json(status, THRESHOLDS, now) for status in statuses)):
        assert view["remaining_percentage"] == round(view["remaining_percentage"], 1)
        for window in view["windows"]:
            assert window["remaining_percentage"] == round(window["remaining_percentage"], 1)
            assert window["used_percentage"] == round(100.0 - window["remaining_percentage"], 1)
    assert _five(cli.status_json(statuses[0], THRESHOLDS, now))["remaining_percentage"] == 20.4


# ------------------------------------------------------- a future-dated reading


def test_a_future_dated_reading_is_unknown_and_moves_nothing(tmp_path):
    now = datetime.now(timezone.utc)
    statuses = _case("future", tmp_path / "usage", now)
    for status in statuses:
        windows = assistance.quota_windows(status, THRESHOLDS, now=now)
        assert windows and all(window["unknown"] for window in windows)
    assert assistance.handoff_suggestion(statuses, THRESHOLDS, now=now) is None
    assert split.quota_lean(statuses, THRESHOLDS, now) is None
    assert assistance.statusline_segment(statuses, THRESHOLDS, now=now) == (
        "Claude 5h unknown | Codex 5h unknown")
    assert assistance.next_pace_readings({}, statuses, THRESHOLDS, now=now) == {}


# ------------------------------------------------------------ non-finite numbers


NON_FINITE = [float("nan"), float("inf"), float("-inf"), "NaN", "1e400", "-1e400"]


@pytest.mark.parametrize("value", NON_FINITE, ids=str)
def test_a_non_finite_claude_percentage_reads_unknown(value, tmp_path, monkeypatch, capsys):
    text = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 0, "resets_at": 0}}})
    literal = value if isinstance(value, str) else json.dumps(value)
    text = text.replace('"used_percentage": 0', f'"used_percentage": {literal}')
    payload = json.loads(text)
    snapshot = parse_claude_statusline_payload(payload)
    assert snapshot["five_hour_used_percentage"] is None
    assert snapshot["five_hour_remaining_percentage"] is None
    assert "observed_at" not in snapshot

    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert cli.main(["--usage-directory", str(tmp_path), "usage-hook", "claude"]) == 0
    line = capsys.readouterr().out
    assert "% left" not in line and "near its limit" not in line


def test_a_non_finite_reset_time_is_dropped():
    snapshot = parse_claude_statusline_payload(
        {"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": float("inf")}}})
    assert snapshot["five_hour_used_percentage"] == 10
    assert snapshot["five_hour_resets_at"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=str)
def test_a_non_finite_codex_percentage_is_not_a_reading(value):
    snapshot = parse_rate_limits_result({"rateLimits": {"primary": {
        "usedPercent": value, "windowDurationMins": 300, "resetsAt": 1}}})
    assert snapshot["observed_at"] is None
    assert snapshot["five_hour_used_percentage"] is None
    assert usage_refresh._finite(value) is None
    assert usage_refresh._remaining_of({"five_hour_used_percentage": value}) is None


# ----------------------------------------------------- the hook's ordered writes


def _push(directory, five, *, resets, seven=None, seven_resets=None):
    limits = {"five_hour": {"used_percentage": five}}
    if resets is not None:
        limits["five_hour"]["resets_at"] = resets
    if seven is not None:
        limits["seven_day"] = {"used_percentage": seven, "resets_at": seven_resets}
    usage_hook.record_claude_payload({"rate_limits": limits}, directory)
    return json.loads((directory / "usage-claude.json").read_text(encoding="utf-8"))


@pytest.fixture
def no_host_push(monkeypatch):
    from modelquad import hostobs

    monkeypatch.setattr(hostobs, "record_host_push", lambda *a, **k: None)


def test_a_lower_figure_in_the_same_window_does_not_replace_the_higher(tmp_path, no_host_push):
    resets = _epoch(datetime.now(timezone.utc) + timedelta(hours=2))
    first = _push(tmp_path, 85, resets=resets)
    stored = _push(tmp_path, 20, resets=resets)
    assert stored["five_hour_used_percentage"] == 85
    assert stored["five_hour_remaining_percentage"] == 15.0
    # Nothing in the push was news, so the reading keeps its own time.
    assert stored["observed_at"] == first["observed_at"]
    statuses = _statuses(tmp_path, datetime.now(timezone.utc))
    assert "Claude 5h 15% left" in assistance.statusline_text(
        statuses, THRESHOLDS, now=datetime.now(timezone.utc))


def test_growth_a_new_window_and_an_equal_figure_replace_normally(tmp_path, no_host_push):
    now = datetime.now(timezone.utc)
    resets = _epoch(now + timedelta(hours=2))
    first = _push(tmp_path, 40, resets=resets)
    same = _push(tmp_path, 40, resets=resets)
    assert same["observed_at"] >= first["observed_at"]
    assert _push(tmp_path, 55, resets=resets)["five_hour_used_percentage"] == 55
    later = _push(tmp_path, 3, resets=resets + 5 * 3600)
    assert later["five_hour_used_percentage"] == 3
    assert later["five_hour_resets_at"] == resets + 5 * 3600
    # A push cached from the window before does not bring that window back.
    old = _push(tmp_path, 90, resets=resets)
    assert old["five_hour_used_percentage"] == 3
    assert old["five_hour_resets_at"] == resets + 5 * 3600


def test_a_stored_time_ahead_of_the_clock_is_not_kept(tmp_path, no_host_push):
    now = datetime.now(timezone.utc)
    resets = _epoch(now + timedelta(hours=2))
    _claude_file(tmp_path, now, five=85, observed=timedelta(hours=1))
    stored = _push(tmp_path, 20, resets=resets)
    assert stored["five_hour_used_percentage"] == 85
    assert assistance._moment(stored["observed_at"]) <= datetime.now(timezone.utc)


def test_each_window_keeps_its_own_higher_figure(tmp_path, no_host_push):
    now = datetime.now(timezone.utc)
    five, seven = _epoch(now + timedelta(hours=2)), _epoch(now + timedelta(days=3))
    _push(tmp_path, 85, resets=five, seven=30, seven_resets=seven)
    stored = _push(tmp_path, 20, resets=five, seven=35, seven_resets=seven)
    assert stored["five_hour_used_percentage"] == 85
    assert stored["five_hour_remaining_percentage"] == 15.0
    assert stored["seven_day_used_percentage"] == 35


def test_a_push_without_a_reset_time_follows_the_stored_window(tmp_path, no_host_push):
    now = datetime.now(timezone.utc)
    resets = _epoch(now + timedelta(hours=2))
    _push(tmp_path, 85, resets=resets)
    # The stored window has not reset, so a lower figure is the same window.
    stored = _push(tmp_path, 20, resets=None)
    assert stored["five_hour_used_percentage"] == 85
    assert stored["five_hour_resets_at"] == resets


def test_without_a_stored_reset_time_the_new_reading_is_taken(tmp_path, no_host_push):
    _push(tmp_path, 85, resets=None)
    stored = _push(tmp_path, 20, resets=None)
    # Nothing says it is the same window: the newest observation stands.
    assert stored["five_hour_used_percentage"] == 20


def test_a_stored_window_that_has_reset_is_replaced(tmp_path, no_host_push):
    now = datetime.now(timezone.utc)
    _push(tmp_path, 85, resets=_epoch(now - timedelta(minutes=1)))
    assert _push(tmp_path, 2, resets=None)["five_hour_used_percentage"] == 2


def test_a_push_without_rate_limits_keeps_the_reading_and_its_time(tmp_path, no_host_push):
    resets = _epoch(datetime.now(timezone.utc) + timedelta(hours=2))
    first = _push(tmp_path, 85, resets=resets)
    usage_hook.record_claude_payload({}, tmp_path)
    stored = json.loads((tmp_path / "usage-claude.json").read_text(encoding="utf-8"))
    assert stored["five_hour_used_percentage"] == 85
    assert stored["observed_at"] == first["observed_at"]
    assert stored["stale"] is True
    # And with nothing stored, nothing is invented.
    empty = tmp_path / "empty"
    empty.mkdir()
    usage_hook.record_claude_payload({}, empty)
    written = json.loads((empty / "usage-claude.json").read_text(encoding="utf-8"))
    assert written["five_hour_used_percentage"] is None and "observed_at" not in written


def test_concurrent_pushes_end_on_the_highest_figure(tmp_path, no_host_push):
    resets = _epoch(datetime.now(timezone.utc) + timedelta(hours=2))
    values = [10, 85, 30, 60, 20, 5, 70, 40]
    threads = [threading.Thread(target=usage_hook.record_claude_payload, args=(
        {"rate_limits": {"five_hour": {"used_percentage": value, "resets_at": resets}}}, tmp_path))
               for value in values]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stored = json.loads((tmp_path / "usage-claude.json").read_text(encoding="utf-8"))
    assert stored["five_hour_used_percentage"] == 85


def test_the_hook_does_not_write_when_the_lock_cannot_be_had(tmp_path, no_host_push, monkeypatch):
    from modelquad.usage import base

    resets = _epoch(datetime.now(timezone.utc) + timedelta(hours=2))
    _push(tmp_path, 85, resets=resets)
    monkeypatch.setattr(base, "_claim_snapshot_lock", lambda *a, **k: None)
    assert _push(tmp_path, 99, resets=resets)["five_hour_used_percentage"] == 85


# ------------------------------------------------------------ the smaller items


@pytest.mark.parametrize("minutes,slot", [(300.0, "five_hour"), (10080.0, "seven_day"),
                                          (299, "other"), (300.5, "other"), (None, "other"),
                                          (True, "other"), (float("nan"), "other")], ids=str)
def test_a_codex_window_duration_that_is_a_whole_float_is_recognised(minutes, slot):
    window = {"usedPercent": 30, "resetsAt": 1}
    if minutes is not None:
        window["windowDurationMins"] = minutes
    snapshot = parse_rate_limits_result({"rateLimits": {"primary": window}})
    assert snapshot["windows"][0]["slot"] == slot
    assert (snapshot["five_hour_used_percentage"] == 30) is (slot == "five_hour")


@pytest.mark.parametrize("minutes", [299, None], ids=str)
def test_a_window_of_unknown_length_is_not_a_fact_about_the_plan(minutes, tmp_path):
    now = datetime.now(timezone.utc)
    primary = {"usedPercent": 30, "resetsAt": _epoch(now + timedelta(hours=2))}
    if minutes is not None:
        primary["windowDurationMins"] = minutes
    snapshot = parse_rate_limits_result({"rateLimits": {
        "primary": primary,
        "secondary": {"usedPercent": 40, "windowDurationMins": 10080,
                      "resetsAt": _epoch(now + timedelta(days=3))}}})
    (tmp_path / "usage-codex.json").write_text(json.dumps(snapshot), encoding="utf-8")
    line = assistance.statusline_text(_statuses(tmp_path, datetime.now(timezone.utc)),
                                      THRESHOLDS, now=datetime.now(timezone.utc))
    assert "no 5-hour limit" not in line
    assert "Codex 5h unknown / 7d 60% left" in line


def _memory_statuses(now, *, claude_five, codex_five=None, five_reset=80):
    from modelquad.models import Freshness, ProviderStatus

    def status(name, five):
        metadata = {"source": "claude_statusline" if name == "claude" else "codex_app_server"}
        if five is not None:
            metadata["five_hour_used_percentage"] = five
            metadata["five_hour_resets_at"] = (now + timedelta(minutes=five_reset)).isoformat()
        return ProviderStatus(name=name, remaining_percentage=None if five is None else 100.0 - five,
                              observed_at=None if five is None else now - timedelta(seconds=5),
                              freshness=Freshness.UNKNOWN if five is None else Freshness.FRESH,
                              metadata=metadata)

    return (status("claude", claude_five), status("codex", codex_five))


@pytest.mark.parametrize("used,left", [(99.5, "1% left"), (99.99, "1% left"), (100.0, "0% left"),
                                       (0.0, "100% left"), (0.4, "99% left"), (79.6, "21% left"),
                                       (80.0, "20% left"), (62.5, "38% left")])
def test_the_shown_whole_number_never_crosses_a_line_the_reading_did_not(used, left):
    now = datetime.now(timezone.utc)
    line = assistance.statusline_text(_memory_statuses(now, claude_five=used), THRESHOLDS, now=now)
    assert f"Claude 5h {left}" in line


def test_the_note_never_says_a_threshold_was_reached_when_it_was_not():
    now = datetime.now(timezone.utc)
    statuses = _memory_statuses(now, claude_five=85.7, codex_five=89.99)
    note = assistance.handoff_suggestion(statuses, THRESHOLDS, now=now)
    assert note is not None
    assert "Claude is at 85% of" in note["message"]
    assert "Codex is at 89% of" in note["message"]
    assert assistance.handoff_suggestion(
        _memory_statuses(now, claude_five=79.99), THRESHOLDS, now=now) is None


def test_no_wait_note_when_codex_is_nearly_out():
    now = datetime.now(timezone.utc)
    near_reset = dict(claude_five=90, five_reset=20)
    wait = assistance.handoff_suggestion(
        _memory_statuses(now, codex_five=50, **near_reset), THRESHOLDS, now=now)
    assert wait is not None and wait["to"] == "wait"
    assert assistance.handoff_suggestion(
        _memory_statuses(now, codex_five=95, **near_reset), THRESHOLDS, now=now) is None


@pytest.mark.parametrize("entry", [
    {"used": "x"}, {"used": float("nan")}, {"used": 10, "observed_at": None},
    {"used": None}, {"used": 10, "observed_at": "not a time"}, "not a mapping",
], ids=str)
def test_a_malformed_pace_entry_is_replaced(entry):
    now = datetime.now(timezone.utc)
    statuses = _memory_statuses(now, claude_five=40)
    resets = statuses[0].metadata["five_hour_resets_at"]
    if isinstance(entry, dict):
        entry = {"observed_at": (now - timedelta(minutes=30)).isoformat(),
                 "resets_at": resets, **entry}
    kept = assistance.next_pace_readings({"claude:five_hour": entry}, statuses, THRESHOLDS, now=now)
    first = kept["claude:five_hour"]
    assert first["used"] == 40 and math.isfinite(first["used"])
    assert assistance._moment(first["observed_at"]) is not None
    # A good earlier reading is still kept.
    good = {"used": 10, "observed_at": (now - timedelta(minutes=30)).isoformat(), "resets_at": resets}
    assert assistance.next_pace_readings(
        {"claude:five_hour": good}, statuses, THRESHOLDS, now=now)["claude:five_hour"] == good
