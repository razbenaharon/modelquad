# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad_split``: a proposed division of a task, which starts nothing.

Task fit decides who takes a step; Codex through ModelQuad never takes a step
that edits files or runs commands. Plans are facts shown beside the owners and
never move one. Quota moves an owner only one way: a medium or large
read-only step the task fit gave Claude goes to Codex when Claude's fresh
reading is near its limit and both of Codex's fresh readings show room; over
a grid of quota states and plan kinds, nothing else moves. The tool starts
no process, stores no title, and every Codex step asks the user when it runs
(design memo 3.1-3.2; legal self-review 3.5; owner decision 2026-11-08).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import itertools
import json
import logging
from pathlib import Path
import re
import subprocess

import pytest

from modelquad import plans, procutil, repo_policy as rp, repo_settings as rs, split
from modelquad.adapters.server_instructions import server_instructions
from modelquad.config import Config
from modelquad.execution import ExecutionManager
from modelquad.execution.base import READ_ONLY_TASK_TYPES
from modelquad.mcp import server as mcp
from modelquad.models import Freshness, FreshnessThresholds, ProviderStatus, TaskType
from modelquad.router import Router

THRESHOLDS = FreshnessThresholds()

#: Wording the legal self-review (3.5) rules out, also in the negative.
BANNED = (
    "save quota", "save usage", "waste your quota", "every last percent", "stretch", "extend your",
    "maximise", "maximize", "get more out of", "beat", "bypass", "get around", "escape", "skip",
    "never hit", "keep coding", "runs out", "continue after", "failover", "more usage", "double",
    "unlimited", "combine", "pool", "merge", "route", "routing", "load balancing", "auto-switch",
    "switch", "arbitrage", "cheaper", "official", "partner", "endorsed", "safe", "secure",
    "guaranteed", "locked-down", "nothing leaves", "autonomous", "take over", "hand off",
)


def banned_in(text: str) -> list[str]:
    folded = text.casefold()
    return [phrase for phrase in BANNED if phrase in folded]


def git_repo(folder: Path) -> Path:
    (folder / ".git").mkdir(parents=True)
    return folder


# --------------------------------------------------------------- the rule


def test_the_read_only_kinds_are_the_executors_and_the_repo_files():
    assert set(split.READ_ONLY_KINDS) == {kind.value for kind in READ_ONLY_TASK_TYPES}
    assert tuple(split.READ_ONLY_KINDS) == rp.TASK_KINDS
    assert set(split.TASK_TYPES) == {kind.value for kind in TaskType}


def _propose(steps, *, statuses=None, config=None, policy=None, user_mode=None,
             views=None, global_proactive=False, now=None, standing=False):
    now = now or datetime.now(timezone.utc)
    statuses = statuses if statuses is not None else [
        ProviderStatus(name="claude"), ProviderStatus(name="codex")]
    return split.propose(steps, router=Router(config or Config()), statuses=statuses,
                         thresholds=THRESHOLDS, now=now,
                         plan_views=views or plans.merged(None, {}), policy=policy,
                         user_mode=user_mode, global_proactive=global_proactive,
                         standing=standing)


#: Codex as the better fit for everything: the hard gate must still hold.
CODEX_FAVOURED = Config(task_fit={kind.value: {"claude": 0.1, "codex": 1.0} for kind in TaskType})


@pytest.mark.parametrize("config", [Config(), CODEX_FAVOURED], ids=["default", "codex-favoured"])
def test_steps_that_edit_or_run_or_are_not_read_only_are_never_codex(config):
    steps = [split.Step(f"step {index}", kind, needs, independent)
             for index, (kind, needs, independent) in enumerate(
                 itertools.product(split.TASK_TYPES, split.NEEDS, (False, True)))]
    for chunk in range(0, len(steps), split.MAX_STEPS):
        reply = _propose(steps[chunk:chunk + split.MAX_STEPS], config=config)
        for row in reply["steps"]:
            assert row["reason"] in split.REASONS and row["owner"] in split.OWNERS
            if row["needs"] != "none":
                assert (row["owner"], row["reason"]) == (split.CLAUDE_ONLY, split.EDITS_OR_COMMANDS)
            elif row["task_type"] not in split.READ_ONLY_KINDS:
                assert (row["owner"], row["reason"]) == (split.CLAUDE_ONLY, split.KIND_NOT_READ_ONLY)
            assert row["asks_user"] is (row["owner"] == split.CODEX)
        assert reply["launches"] is False


def test_task_fit_decides_and_an_independent_opinion_excludes_claude():
    reply = _propose([
        split.Step("Add the parser", "implementation", "edit"),
        split.Step("Run the tests", "testing", "run"),
        split.Step("Review steps 1-2", "review", "none", True),
        split.Step("Map the module", "exploration", "none"),
        split.Step("Weigh the two designs", "architecture", "none", True),
        split.Step("Review the docs", "review", "none"),
    ])
    owners = [(row["owner"], row["reason"]) for row in reply["steps"]]
    assert owners == [
        (split.CLAUDE_ONLY, split.EDITS_OR_COMMANDS),
        (split.CLAUDE_ONLY, split.EDITS_OR_COMMANDS),
        (split.CODEX, split.INDEPENDENT_REVIEW),
        (split.CLAUDE, split.TASK_FIT),
        (split.CODEX, split.SECOND_OPINION),
        (split.CLAUDE, split.TASK_FIT),
    ]
    lines = reply["content"].splitlines()
    assert lines[1] == "Claude: steps 1, 2, 4, 6 (edits or commands; task fit)."
    assert lines[2] == ("Codex (read-only): steps 3, 5 (independent review; "
                        "second opinion). Each Codex step shows its own approval prompt when it runs.")
    assert lines[-1].startswith("Quota now: ")
    assert reply["approval"] == "Each Codex step shows its own approval prompt when it runs."


def test_a_missing_codex_leaves_every_step_with_claude():
    for statuses in ([ProviderStatus(name="claude")],
                     [ProviderStatus(name="claude"),
                      ProviderStatus(name="codex", available=False, reason="codex CLI not found")]):
        reply = _propose([split.Step("Review", "review", "none", True),
                          split.Step("Explore", "exploration", "none")], statuses=statuses)
        assert {(row["owner"], row["reason"]) for row in reply["steps"]} == {
            (split.CLAUDE_ONLY, split.CODEX_UNAVAILABLE)}
        assert "No step is proposed for Codex." in reply["content"]


# ------------------------- plans move nothing; quota moves one kind of step


def _status(name, now, *, five=None, seven=None, age=None, reached=None, five_reset=80):
    metadata = {"source": "claude_statusline" if name == "claude" else "codex_app_server"}
    if age is not None:
        if five is not None:
            metadata["five_hour_used_percentage"] = five
            metadata["five_hour_resets_at"] = (now + timedelta(minutes=five_reset)).isoformat()
        if seven is not None:
            metadata["seven_day_used_percentage"] = seven
            metadata["seven_day_resets_at"] = (now + timedelta(days=3)).isoformat()
        if reached is not None:
            metadata["rate_limit_reached_type"] = reached
    used = [value for value in (five, seven) if value is not None and age is not None]
    return ProviderStatus(
        name=name, available=True, remaining_percentage=100.0 - max(used) if used else None,
        observed_at=None if age is None else now - age,
        freshness=Freshness.UNKNOWN if age is None else Freshness.FRESH, metadata=metadata)


FRESH, STALE = timedelta(0), timedelta(hours=2)
CLAUDE_QUOTA = {
    "unknown": {},
    "low": {"five": 20, "seven": 10, "age": FRESH},
    "5h-over": {"five": 88, "seven": 40, "age": FRESH},
    "full": {"five": 100, "seven": 100, "age": FRESH},
    "stale-high": {"five": 95, "age": STALE, "five_reset": 40},
}
CODEX_QUOTA = {
    "unknown": {},
    "low": {"five": 10, "seven": 5, "age": FRESH},
    "high": {"five": 95, "seven": 70, "age": FRESH},
    "5h-full": {"five": 100, "seven": 60, "age": FRESH},
    "week-full": {"five": 40, "seven": 100, "age": FRESH},
    "reached": {"five": 99, "seven": 50, "age": FRESH, "reached": "primary"},
    "stale-full": {"five": 100, "age": STALE, "five_reset": 40},
}
OBSERVED_CODEX_PLANS = (None, "plus", "pro", "team", "enterprise", "something-new")
DECLARED = ({}, {"claude": "pro"}, {"claude": "enterprise", "codex": "plus"}, {"codex": "business"})
STEPS = [
    split.Step("Change the code", "implementation", "edit"),
    split.Step("Run the suite", "testing", "run"),
    split.Step("Review the change", "review", "none", True),
    split.Step("Explore the module", "exploration", "none"),
    split.Step("Second look at the design", "architecture", "none", True),
    split.Step("Look at the design", "architecture", "none"),
    split.Step("Debug by reading", "debugging", "none"),
    split.Step("Rename a file", "trivial", "edit"),
]


#: The steps of STEPS the task fit gives Claude that Codex could take.
LEANING = (3, 5)


def _owners(reply):
    return [(row["owner"], row["reason"], row["asks_user"]) for row in reply["steps"]]


@pytest.mark.parametrize("policy", [None, rp.parse(b"schema = 1\n"),
                                    rp.parse(b'schema = 1\n[codex]\naccount = "workspace"\n'),
                                    rp.parse(b'schema = 1\n[offers]\ncodex = "on_request"\n')],
                         ids=["no-file", "plain-file", "workspace-only", "on-request"])
def test_only_claude_near_its_limit_with_codex_room_moves_an_owner_and_no_plan_does(policy):
    now = datetime.now(timezone.utc)
    baseline = None
    cautions = set()
    for claude, codex, observed, declared in itertools.product(
            CLAUDE_QUOTA, CODEX_QUOTA, OBSERVED_CODEX_PLANS, DECLARED):
        statuses = [_status("claude", now, **CLAUDE_QUOTA[claude]),
                    _status("codex", now, **CODEX_QUOTA[codex])]
        reply = _propose(STEPS, statuses=statuses, policy=policy, now=now,
                         views=plans.merged(observed, {"claude": None, "codex": None, **declared}))
        if baseline is None:
            baseline = _owners(reply)
        # Claude fresh and near its limit, Codex fresh with room in both windows:
        # the two medium read-only steps the task fit gave Claude move, nothing else.
        expected = list(baseline)
        moves = claude in ("5h-over", "full") and codex == "low"
        if moves:
            for index in LEANING:
                expected[index] = (split.CODEX, split.CLAUDE_NEAR_LIMIT, True)
        assert _owners(reply) == expected, (claude, codex, observed, declared)
        assert reply["launches"] is False and reply["facts"]["changes_owners"] is moves
        assert (reply["facts"]["quota_lean"] is not None) is moves
        lines = reply["content"].splitlines()
        assert 3 <= len(lines) <= 8, lines
        assert banned_in(json.dumps(reply)) == [], reply["content"]
        # Kind words only: no plan name reaches the reply.
        for name in filter(None, (observed, *declared.values())):
            assert name not in json.dumps(reply["facts"]["plans"])
            # As a word: "pro" is also inside "approval prompt".
            assert re.search(rf"{re.escape(name)}", reply["content"]) is None, name
        cautions.add(reply["facts"]["caution"])
    assert (split.CODEX, split.INDEPENDENT_REVIEW, True) in baseline
    # The grid reaches both a caution and none.
    assert None in cautions and any(cautions - {None})


def test_the_caution_is_a_fact_about_codex_and_only_for_a_current_reading():
    now = datetime.now(timezone.utc)
    week = split.codex_caution(_status("codex", now, **CODEX_QUOTA["week-full"]), THRESHOLDS, now)
    assert week.startswith("Codex reports its weekly limit reached until ")
    assert week.endswith("; a Codex step would likely be refused.")
    five = split.codex_caution(_status("codex", now, **CODEX_QUOTA["5h-full"]), THRESHOLDS, now)
    assert five.startswith("Codex reports its 5-hour limit reached until ")
    reached = split.codex_caution(_status("codex", now, **CODEX_QUOTA["reached"]), THRESHOLDS, now)
    assert reached == "Codex reports a limit reached; a Codex step would likely be refused."
    for state in ("unknown", "low", "high", "stale-full"):
        assert split.codex_caution(_status("codex", now, **CODEX_QUOTA[state]), THRESHOLDS, now) is None
    assert split.codex_caution(None, THRESHOLDS, now) is None


def test_the_router_sees_no_quota_or_plan():
    """The task fit is decided without quota; quota acts only after it (quota_lean)."""
    now = datetime.now(timezone.utc)
    seen = split.router_statuses([_status("claude", now, **CLAUDE_QUOTA["full"]),
                                  _status("codex", now, **CODEX_QUOTA["reached"])])
    assert seen == [ProviderStatus(name="claude", available=True),
                    ProviderStatus(name="codex", available=True)]


# ------------------------------------------------- the quota rule, in detail

#: Claude fresh and over its 5-hour threshold; Codex fresh with room in both windows.
NEAR = {"five": 85, "seven": 40, "age": FRESH}
ROOM = {"five": 30, "seven": 20, "age": FRESH}
EXPLORE = split.Step("Map the module", "exploration", "none")


def _lean_reply(steps=(EXPLORE,), *, claude=NEAR, codex=ROOM, **kwargs):
    now = kwargs.pop("now", None) or datetime.now(timezone.utc)
    statuses = [_status("claude", now, **claude), _status("codex", now, **codex)]
    return _propose(list(steps), statuses=statuses, now=now, **kwargs)


def _first(reply):
    row = reply["steps"][0]
    return row["owner"], row["reason"]


def test_a_medium_read_only_step_moves_when_every_reading_holds():
    reply = _lean_reply()
    assert _first(reply) == (split.CODEX, split.CLAUDE_NEAR_LIMIT)
    row = reply["steps"][0]
    assert row["asks_user"] is True and row["trigger"] == split.QUOTA_TRIGGER == "split_quota"
    assert row["size"] == "medium"
    facts = reply["facts"]
    assert facts["changes_owners"] is True and reply["launches"] is False
    lean = facts["quota_lean"]
    assert lean["claude_window"]["window"] == "five_hour" and lean["claude_window"]["used_percent"] == 85
    assert lean["claude_threshold_used_percent"] == 80.0
    assert [window["window"] for window in lean["codex_windows"]] == ["five_hour", "seven_day"]
    assert lean["codex_under_used_percent"] == 90.0 and lean["trigger"] == "split_quota"


def test_the_thresholds_are_the_near_limit_notes_own():
    from modelquad import assistance

    assert assistance.HANDOFF_USED_PERCENT == {"five_hour": 80.0, "seven_day": 90.0}
    assert assistance.HANDOFF_CODEX_EXHAUSTED_USED == 90.0
    moves = lambda claude: _first(_lean_reply(claude=claude))[0] == split.CODEX  # noqa: E731
    assert moves({"five": 80, "seven": 10, "age": FRESH})
    assert not moves({"five": 79, "seven": 10, "age": FRESH})
    assert moves({"five": 10, "seven": 90, "age": FRESH})
    assert not moves({"five": 10, "seven": 89, "age": FRESH})


@pytest.mark.parametrize("claude", [
    {},                                                    # unknown
    {"five": 20, "seven": 10, "age": FRESH},               # fresh, low
    {"five": 95, "seven": 95, "age": STALE},               # old reading, still in its window
    {"five": 95, "age": timedelta(minutes=10), "five_reset": 80},  # past fresh_seconds
], ids=["unknown", "fresh-low", "stale-high", "aging-high"])
def test_claude_unknown_low_or_old_moves_nothing(claude):
    reply = _lean_reply(claude=claude)
    assert _first(reply) == (split.CLAUDE, split.TASK_FIT)
    assert reply["facts"]["changes_owners"] is False and reply["facts"]["quota_lean"] is None
    assert reply["steps"][0]["trigger"] is None


@pytest.mark.parametrize("codex", [
    {},                                                    # unknown
    {"five": 30, "age": FRESH},                            # weekly missing
    {"seven": 20, "age": FRESH},                           # 5-hour missing
    {"five": 30, "seven": 20, "age": STALE},               # old reading
    {"five": 90, "seven": 20, "age": FRESH},               # 5-hour at the bar
    {"five": 30, "seven": 95, "age": FRESH},               # weekly high
    {"five": 30, "seven": 20, "age": FRESH, "reached": "primary"},  # reports a limit reached
], ids=["unknown", "no-weekly", "no-5h", "stale", "5h-90", "weekly-high", "reached"])
def test_codex_unknown_old_or_high_moves_nothing(codex):
    reply = _lean_reply(codex=codex)
    assert _first(reply) == (split.CLAUDE, split.TASK_FIT)
    assert reply["facts"]["changes_owners"] is False


def test_an_unavailable_codex_moves_nothing():
    now = datetime.now(timezone.utc)
    codex = _status("codex", now, **ROOM)
    codex = ProviderStatus(name="codex", available=False, observed_at=codex.observed_at,
                           freshness=codex.freshness, metadata=codex.metadata)
    reply = _propose([EXPLORE], statuses=[_status("claude", now, **NEAR), codex], now=now)
    assert _first(reply) == (split.CLAUDE_ONLY, split.CODEX_UNAVAILABLE)
    assert reply["facts"]["changes_owners"] is False


def test_size_decides_whether_a_step_is_worth_a_codex_call():
    steps = [split.Step("a", "exploration", "none", size="small"),
             split.Step("b", "exploration", "none", size="medium"),
             split.Step("c", "architecture", "none", size="large"),
             split.Step("d", "review", "none", size="small")]
    reply = _lean_reply(steps)
    assert [(row["owner"], row["reason"]) for row in reply["steps"]] == [
        (split.CLAUDE, split.TASK_FIT), (split.CODEX, split.CLAUDE_NEAR_LIMIT),
        (split.CODEX, split.CLAUDE_NEAR_LIMIT), (split.CLAUDE, split.TASK_FIT)]
    assert split.SIZES == ("small", "medium", "large")


def test_with_quota_leaning_edits_runs_and_other_kinds_still_stay_with_claude():
    steps = [split.Step(f"step {index}", kind, needs, independent, size)
             for index, (kind, needs, independent, size) in enumerate(
                 itertools.product(split.TASK_TYPES, split.NEEDS, (False, True), split.SIZES))]
    seen = set()
    for chunk in range(0, len(steps), split.MAX_STEPS):
        reply = _lean_reply(steps[chunk:chunk + split.MAX_STEPS])
        for row in reply["steps"]:
            seen.add(row["reason"])
            if row["needs"] != "none":
                assert (row["owner"], row["reason"]) == (split.CLAUDE_ONLY, split.EDITS_OR_COMMANDS)
            elif row["task_type"] not in split.READ_ONLY_KINDS:
                assert (row["owner"], row["reason"]) == (split.CLAUDE_ONLY, split.KIND_NOT_READ_ONLY)
            if row["reason"] == split.CLAUDE_NEAR_LIMIT:
                assert row["needs"] == "none" and row["task_type"] in split.READ_ONLY_KINDS
                assert row["size"] != "small" and not row["independent"]
    assert split.CLAUDE_NEAR_LIMIT in seen


@pytest.mark.parametrize("kwargs,reason", [
    ({"user_mode": "off"}, rs.USER_CODEX_OFF),
    ({"policy": rp.parse(b"schema = 1\n[codex]\nallowed = false\n")}, rp.CODEX_OFF),
    ({"policy": rp.parse(b'schema = 1\n[codex]\ntask_kinds = ["review"]\n')}, rp.KIND_REMOVED),
], ids=["user-off", "repo-file-off", "kind-removed"])
def test_a_repository_that_refuses_codex_moves_nothing(kwargs, reason):
    reply = _lean_reply(**kwargs)
    assert _first(reply) == (split.CLAUDE_ONLY, reason)
    assert reply["facts"]["changes_owners"] is False


def test_while_codex_calls_are_paused_quota_moves_nothing():
    now = datetime.now(timezone.utc)
    reply = split.propose([EXPLORE], router=Router(Config()),
                          statuses=[_status("claude", now, **NEAR), _status("codex", now, **ROOM)],
                          thresholds=THRESHOLDS, now=now, plan_views=plans.merged(None, {}),
                          policy=None, user_mode=None, global_proactive=False, codex_paused=True)
    assert _first(reply) == (split.CLAUDE_ONLY, split.CODEX_PAUSED)
    assert reply["facts"]["changes_owners"] is False


def test_on_request_still_proposes_and_says_claude_only_offers():
    reply = _lean_reply(policy=rp.parse(b'schema = 1\n[offers]\ncodex = "on_request"\n'))
    assert _first(reply) == (split.CODEX, split.CLAUDE_NEAR_LIMIT)
    assert "In this repository, Claude calls Codex only when you ask." in reply["content"]


def test_the_lines_state_the_readings_and_promise_nothing():
    steps = [split.Step("Add it", "implementation", "edit"),
             split.Step("Review it", "review", "none", True),
             split.Step("Map it", "exploration", "none", size="large")]
    reply = _lean_reply(steps)
    lines = reply["content"].splitlines()
    assert lines[1] == "Claude: step 1 (edits or commands)."
    assert lines[2] == ("Codex (read-only): steps 2, 3 (independent review; Claude near its limit, "
                        "Codex has room). Each Codex step shows its own approval prompt when it runs.")
    assert re.fullmatch(
        r"Claude has used 85% of its 5-hour window \(resets in 1h 2\dm\); Codex has room\. "
        r"Proposed for Codex \(read-only\): step 3\. Claude passes trigger split_quota when it "
        r"asks Codex, so the review report counts these calls\.", lines[3]), lines[3]
    assert lines[-1].startswith("Quota now: ") and 3 <= len(lines) <= 8
    assert banned_in(json.dumps(reply)) == []
    for word in ("save", "saving", "extend", "stretch", "avoid"):
        assert word not in reply["content"].casefold(), word
    assert [row["trigger"] for row in reply["steps"]] == [None, None, "split_quota"]


# ------------------------------------------------------------- the tool


def _usage(directory: Path, now, *, codex=None, claude=None):
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in (("codex", codex), ("claude", claude)):
        if payload is not None:
            (directory / f"usage-{name}.json").write_text(json.dumps(
                {"provider": name, "observed_at": now.isoformat(), **payload}), encoding="utf-8")


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """The MCP tool on isolated usage files, with every way to start work tripped."""
    usage = tmp_path / "usage"
    now = datetime.now(timezone.utc)
    _usage(usage, now,
           codex={"source": "codex_app_server", "plan_type": "plus",
                  "five_hour_used_percentage": 30,
                  "five_hour_resets_at": (now + timedelta(hours=2)).isoformat()},
           claude={"source": "claude_statusline", "five_hour_used_percentage": 50,
                   "five_hour_resets_at": (now + timedelta(hours=1)).isoformat()})
    monkeypatch.setattr(mcp, "USAGE_DIRECTORY", str(usage))
    monkeypatch.setattr(mcp, "CONFIG_PATH", None)
    started = []

    def tripwire(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("modelquad_split started something")

    for target, name in ((subprocess, "Popen"), (subprocess, "run"), (subprocess, "call"),
                         (subprocess, "check_output"), (procutil, "start_contained"),
                         (mcp, "_delegate_payload"), (mcp, "start_job"),
                         (mcp, "_build_execution_manager"), (ExecutionManager, "delegate")):
        monkeypatch.setattr(target, name, tripwire)
    from modelquad import gitmeta as changes
    monkeypatch.setattr(changes, "_git", tripwire)

    def call(directory, steps):
        result = mcp.modelquad_split(str(directory), [mcp.SplitStep(**step) for step in steps])
        return result.structured_content, result.content[0].text

    call.started = started
    call.usage = usage
    return call


STEP_ARGS = [
    {"title": "Add the cache", "task_type": "implementation", "needs": "edit"},
    {"title": "Review the cache change", "task_type": "review", "needs": "none", "independent": True},
    {"title": "Map who reads the cache", "task_type": "exploration", "needs": "none", "independent": True},
]


def test_the_tool_starts_no_process_and_says_so(tool, tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / rp.FILE_NAME).write_text('schema = 1\n[codex]\ntask_kinds = ["review"]\n', encoding="utf-8")
    payload, text = tool(repo, STEP_ARGS)
    assert tool.started == []
    assert payload["launches"] is False
    assert [(row["owner"], row["reason"]) for row in payload["steps"]] == [
        (split.CLAUDE_ONLY, split.EDITS_OR_COMMANDS), (split.CODEX, split.INDEPENDENT_REVIEW),
        (split.CLAUDE_ONLY, rp.KIND_REMOVED)]
    assert "Each Codex step shows its own approval prompt when it runs." in text
    assert 3 <= len(text.splitlines()) <= 8
    assert payload["facts"]["plans"]["codex"] == {"kind": "personal", "source": "reported by Codex"}
    assert "plus" not in json.dumps(payload) and "plus" not in text
    assert banned_in(text) == [] and banned_in(json.dumps(payload)) == []


@pytest.mark.parametrize("text,code", [
    ("schema = 1\n[codex]\nallowed = false\n", rp.CODEX_OFF),
    ("schema = 9\n", rp.INVALID),
])
def test_a_repo_file_that_turns_codex_off_gives_its_code(tool, tmp_path, text, code):
    repo = git_repo(tmp_path / "repo")
    (repo / rp.FILE_NAME).write_text(text, encoding="utf-8")
    payload, content = tool(repo, STEP_ARGS)
    assert [(row["owner"], row["reason"]) for row in payload["steps"]] == [
        (split.CLAUDE_ONLY, split.EDITS_OR_COMMANDS), (split.CLAUDE_ONLY, code),
        (split.CLAUDE_ONLY, code)]
    assert "No step is proposed for Codex." in content and tool.started == []


def test_the_users_own_off_gives_its_code_and_wins_over_the_file(tool, tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / rp.FILE_NAME).write_text("schema = 1\n", encoding="utf-8")
    rs.write_mode(repo, "off")
    payload, content = tool(repo, STEP_ARGS)
    assert [row["reason"] for row in payload["steps"]] == [
        split.EDITS_OR_COMMANDS, rs.USER_CODEX_OFF, rs.USER_CODEX_OFF]
    assert "you turned Codex off for this repository" in content
    rs.write_mode(repo, "inherit")
    payload, _ = tool(repo, STEP_ARGS)
    assert [row["owner"] for row in payload["steps"]] == [split.CLAUDE_ONLY, split.CODEX, split.CODEX]


def test_on_request_and_the_work_account_rule_add_a_line_and_move_nothing(tool, tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / rp.FILE_NAME).write_text(
        'schema = 1\n[codex]\naccount = "workspace"\n[offers]\ncodex = "on_request"\n', encoding="utf-8")
    payload, content = tool(repo, STEP_ARGS)
    assert [row["owner"] for row in payload["steps"]] == [split.CLAUDE_ONLY, split.CODEX, split.CODEX]
    assert payload["facts"]["repository"] == {"offer_mode": rs.ON_REQUEST, "codex_account_rule": "workspace"}
    assert "In this repository, Claude calls Codex only when you ask." in content
    assert "only on a work account; ModelQuad checks that when a Codex step runs." in content


def test_a_codex_that_is_not_installed_reads_unavailable(tool, tmp_path):
    _usage(tool.usage, datetime.now(timezone.utc),
           codex={"available": False, "reason": "codex CLI not found on PATH (looked for 'codex')."})
    payload, content = tool(git_repo(tmp_path / "repo"), STEP_ARGS)
    assert [row["reason"] for row in payload["steps"]] == [
        split.EDITS_OR_COMMANDS, split.CODEX_UNAVAILABLE, split.CODEX_UNAVAILABLE]
    assert "Codex is not available" in content


#: Credential-shaped, built at run time so the secret scan does not flag the file.
FAKE_KEY = "sk-" + "ant-api03-" + "A" * 40


def test_titles_are_redacted_and_never_stored_or_logged(tool, tmp_path, caplog, isolate_home_directory):
    marker = "zebra-quokka-7731"
    repo = git_repo(tmp_path / "repo")
    steps = [{"title": f"Review {marker} in C:\\Users\\alice\\secret\\app.py", "task_type": "review",
              "needs": "none", "independent": True},
             {"title": f"{marker} {FAKE_KEY}",
              "task_type": "exploration", "needs": "none"}]
    with caplog.at_level(logging.DEBUG):
        payload, content = tool(repo, steps)
    titles = [row["title"] for row in payload["steps"]]
    assert all(title is None or "alice" not in title for title in titles)
    assert FAKE_KEY not in json.dumps(payload)
    assert marker not in content  # the human lines name steps by number only
    assert marker not in caplog.text
    for folder in (isolate_home_directory, tool.usage, repo):
        for path in folder.rglob("*"):
            if path.is_file():
                assert marker.encode() not in path.read_bytes(), path


def test_the_schema_bounds_steps_and_titles():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        mcp.SplitStep(title="x" * 121, task_type="review", needs="none")
    with pytest.raises(ValidationError):
        mcp.SplitStep(title="x", task_type="review", needs="write")
    with pytest.raises(ValidationError):
        mcp.SplitStep(title="x", task_type="review", needs="none", size="tiny")
    assert mcp.SplitStep(title="x", task_type="review", needs="none").size == "medium"
    tools = {tool.name: tool for tool in asyncio.run(mcp.server.list_tools())}
    schema = tools["modelquad_split"].input_schema
    assert set(schema["properties"]) == {"working_directory", "steps"}
    assert schema["properties"]["steps"]["minItems"] == 1
    assert schema["properties"]["steps"]["maxItems"] == split.MAX_STEPS
    with pytest.raises(ValueError):
        _propose([split.Step("x", "review", "none")] * (split.MAX_STEPS + 1))


def test_the_tool_is_read_only_asks_for_nothing_and_says_so_plainly():
    tool = mcp.server._tool_manager._tools["modelquad_split"]
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert "anthropic/requiresUserInteraction" not in (tool.meta or {})
    assert "starts nothing" in tool.description
    assert banned_in(tool.title) == [] and banned_in(tool.description) == []


@pytest.mark.parametrize("proactive", [False, True])
def test_the_server_instructions_name_the_split_and_stay_small(proactive):
    text = server_instructions(proactive)
    assert "with `modelquad_split`, not by hand; it starts nothing." in text
    assert len(text.encode("utf-8")) <= 2400 and len(text) < 4096
    assert banned_in(text.replace("ModelQuad", "")) == []


def test_the_split_skill_is_user_invoked_and_plain():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "src/modelquad/adapters/templates/claude/skills/modelquad-split/SKILL.md",
                 root / "plugin/skills/split/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "disable-model-invocation: true" in text
        assert "modelquad_split" in text and "The proposal approves nothing." in text
        assert banned_in(text) == [], path


def test_no_step_goes_to_codex_while_modelquad_refuses_codex_calls():
    steps = [split.Step("Review", "review", "none", True), split.Step("Explore", "exploration", "none")]
    reply = split.propose(steps, router=Router(CODEX_FAVOURED),
                          statuses=[ProviderStatus(name="claude"), ProviderStatus(name="codex")],
                          thresholds=THRESHOLDS, now=datetime.now(timezone.utc),
                          plan_views=plans.merged(None, {}), policy=None, user_mode=None,
                          global_proactive=False, codex_paused=True)
    assert {row["owner"] for row in reply["steps"]} == {split.CLAUDE_ONLY}
    assert {row["reason"] for row in reply["steps"]} == {split.CODEX_PAUSED}
    assert "paused" in reply["content"] and "No step is proposed for Codex." in reply["content"]
    assert banned_in(reply["content"]) == []


def test_the_split_tool_reads_modelquads_own_pause(tmp_path, monkeypatch):
    from modelquad.mcp import server as mcp
    from modelquad.routing_policy import DelegationPolicyDecision

    monkeypatch.setattr(mcp, "_launch_policy", lambda *a, **k: DelegationPolicyDecision(
        allowed=False, routing_mode="automatic", reason="routing is paused", routing_paused=True))
    from modelquad.split_args import SplitStep

    reply = mcp._split_reply(str(tmp_path), [SplitStep(title="Review", task_type="review", needs="none")])
    assert reply["steps"][0]["reason"] == split.CODEX_PAUSED


# ------------------------------------------- under the user's standing grant


def test_under_the_standing_grant_the_reply_says_no_prompt_will_show():
    """Owner decision 2026-11-12: a quota-moved step is an ordinary call under the grant."""
    reply = _lean_reply(standing=True)
    row = reply["steps"][0]
    assert _first(reply) == (split.CODEX, split.CLAUDE_NEAR_LIMIT)
    assert row["asks_user"] is False and row["trigger"] == split.QUOTA_TRIGGER
    assert reply["approval"] == split.APPROVAL_STANDING
    assert split.APPROVAL_STANDING in reply["content"]
    assert split.APPROVAL not in reply["content"]
    assert reply["launches"] is False, "the proposal itself still starts nothing"


def test_the_grant_changes_no_owner():
    steps = [split.Step("Add it", "implementation", "edit"),
             split.Step("Run it", "testing", "run"),
             split.Step("Review it", "review", "none", True),
             split.Step("Map it", "exploration", "none", size="large")]
    plain, granted = _lean_reply(steps), _lean_reply(steps, standing=True)
    owners = lambda reply: [(row["owner"], row["reason"]) for row in reply["steps"]]  # noqa: E731
    assert owners(plain) == owners(granted)
    assert [row["asks_user"] for row in granted["steps"]] == [False] * 4
    assert [row["asks_user"] for row in plain["steps"]] == [False, False, True, True]


def test_under_the_grant_on_request_is_not_reported_as_waiting_for_the_user():
    policy = rp.parse(b'schema = 1\n[offers]\ncodex = "on_request"\n')
    assert "only when you ask" in _lean_reply(policy=policy)["content"]
    assert "only when you ask" not in _lean_reply(policy=policy, standing=True)["content"]


def test_the_tool_reports_the_grant_only_for_a_folder_it_covers(tool, tmp_path, monkeypatch):
    from modelquad import grant
    from modelquad.mcp import server as mcp

    repo = git_repo(tmp_path / "repo")
    other = git_repo(tmp_path / "other")
    payload, text = tool(repo, STEP_ARGS)
    assert payload["approval"] == split.APPROVAL and split.APPROVAL in text

    monkeypatch.setattr(mcp, "_STANDING_GRANT", True)
    monkeypatch.setattr(mcp, "_SESSION_PROJECT", str(repo))
    monkeypatch.setattr(grant, "allows", lambda directory, **_: True)
    payload, text = tool(repo, STEP_ARGS)
    assert payload["approval"] == split.APPROVAL_STANDING and split.APPROVAL_STANDING in text
    assert not any(row["asks_user"] for row in payload["steps"])
    assert payload["launches"] is False and tool.started == []

    # A folder outside the one the session started in gets no unprompted call.
    payload, _ = tool(other, STEP_ARGS)
    assert payload["approval"] == split.APPROVAL

    # A grant turned off since the session started: the plain line again.
    monkeypatch.setattr(grant, "allows", lambda directory, **_: False)
    payload, _ = tool(repo, STEP_ARGS)
    assert payload["approval"] == split.APPROVAL
