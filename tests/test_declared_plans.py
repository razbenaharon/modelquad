# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Plans: reported by Codex, set by the user, or unknown.

A declared plan is a closed-list value in ``preferences.json``. Codex's own
report beats it for the account-kind notice. The model gets the kind word and
its source, never the plan name, and the worker choice never takes a plan.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modelquad import cli, plans
from modelquad.adapters.server_instructions import preferences_path, write_proactive
from modelquad.mcp import server as mcp

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "modelquad"

MISMATCH = ("Claude: work account (you set it). Codex: personal account (reported by Codex). "
            "This sends work code from a work account to a personal one.")


@pytest.fixture
def state(tmp_path, monkeypatch, isolate_usage_directory):
    root = tmp_path / "state with spaces"
    root.mkdir()
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(root))
    monkeypatch.setattr(mcp, "USAGE_DIRECTORY", None)
    from modelquad.execution import file_rules

    monkeypatch.setattr(file_rules, "MANAGED_SETTINGS", tmp_path / "managed" / "managed-settings.json")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    project = isolate_usage_directory.parent / "work" / "app"
    project.mkdir(parents=True)
    return SimpleNamespace(root=root, usage=isolate_usage_directory, project=project)


def observed(directory: Path, plan) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "usage-codex.json").write_text(
        json.dumps({"provider": "codex", "plan_type": plan}), encoding="utf-8")


def _all_plan_names() -> set[str]:
    return {plan for values in plans.DECLARABLE_PLANS.values() for plan in values}


# --- Storage -----------------------------------------------------------------

def test_unknown_by_default(state):
    assert plans.read_declared() == {"claude": None, "codex": None}
    views = plans.current(state.usage)
    assert plans.public_plans(views) == {
        "claude": {"kind": "unknown", "source": "unknown"},
        "codex": {"kind": "unknown", "source": "unknown"},
    }
    assert mcp._codex_account_line() == "Codex account: unknown."


@pytest.mark.parametrize("provider,value", [
    ("claude", "max-20x"), ("claude", "plus"), ("codex", "max"), ("codex", "ultra"),
    ("claude", ""), ("codex", "C:\\Users\\someone"),
])
def test_out_of_list_values_are_refused_and_nothing_is_written(state, provider, value):
    with pytest.raises(ValueError) as refused:
        plans.write_declared(provider, value)
    message = str(refused.value)
    assert all(plan in message for plan in plans.DECLARABLE_PLANS[provider])
    assert not preferences_path().exists()


def test_cli_refuses_an_unlisted_plan_with_the_allowed_list(state, capsys):
    assert cli.main(["settings", "plan", "codex", "platinum"]) == 2
    err = capsys.readouterr().err
    assert "free, go, plus, pro, team, business, enterprise, edu, api-key, other" in err
    assert not preferences_path().exists()


@pytest.mark.parametrize("provider", plans.PROVIDERS)
def test_set_and_unset_round_trip_keeps_other_preferences(state, provider):
    write_proactive(True)
    for value in plans.DECLARABLE_PLANS[provider]:
        plans.write_declared(provider, value.upper())
        assert plans.read_declared()[provider] == value
    plans.write_declared(provider, None)
    assert plans.read_declared()[provider] is None
    stored = json.loads(preferences_path().read_text(encoding="utf-8"))
    assert stored == {"proactive": True}


def test_cli_set_show_and_unset(state, capsys):
    assert cli.main(["settings", "plan", "claude", "enterprise"]) == 0
    out = capsys.readouterr().out
    assert "Claude: enterprise (you set it). Account: workspace." in out
    assert cli.main(["settings", "plan"]) == 0
    assert "Claude: enterprise (you set it)." in capsys.readouterr().out
    assert cli.main(["settings", "plan", "claude", "unset"]) == 0
    assert plans.read_declared()["claude"] is None


def test_preferences_hold_no_path_or_folder_name(state):
    for provider in plans.PROVIDERS:
        for value in plans.DECLARABLE_PLANS[provider]:
            plans.write_declared(provider, value)
            text = preferences_path().read_text(encoding="utf-8")
            assert state.root.name not in text and "state with spaces" not in text
            assert not any(mark in text for mark in ("\\", "/", ":\\", "Users"))
            stored = json.loads(text)
            assert set(stored) == {"plans"}
            assert all(item in plans.DECLARABLE_PLANS[key] for key, item in stored["plans"].items())


def test_a_hand_edited_unlisted_value_reads_unknown(state):
    preferences_path().write_text(json.dumps({"plans": {"claude": "max-20x", "codex": 5}}),
                                  encoding="utf-8")
    assert plans.read_declared() == {"claude": None, "codex": None}


# --- Merge rule ----------------------------------------------------------------

def test_observed_codex_plan_beats_the_declared_one_for_the_notice(state, capsys):
    observed(state.usage, "plus")
    plans.write_declared("codex", "enterprise")
    views = plans.current(state.usage)
    assert views["codex"].public() == {"kind": "personal", "source": "reported by Codex"}
    assert mcp._codex_account_line().startswith("Codex account: personal ChatGPT plan.")
    assert cli.main(["settings", "plan"]) == 0
    assert "Codex reports plus; you set enterprise." in capsys.readouterr().out


def test_declared_codex_plan_counts_when_codex_reports_none(state):
    plans.write_declared("codex", "business")
    assert plans.current(state.usage)["codex"].public() == {"kind": "workspace", "source": "you set"}
    assert mcp._codex_account_line() == "Codex account: workspace plan (you set it)."
    observed(state.usage, "something-new")
    assert plans.current(state.usage)["codex"].source == "you set"


@pytest.mark.parametrize("plan,kind", [
    ("free", "personal"), ("pro", "personal"), ("max", "personal"), ("ultra", "personal"),
    ("team", "workspace"), ("enterprise", "workspace"), ("education", "workspace"),
    ("api-key", "unknown"), ("other", "unknown"),
])
def test_claude_kinds(state, plan, kind):
    plans.write_declared("claude", plan)
    assert plans.current(state.usage)["claude"].public() == {"kind": kind, "source": "you set"}


@pytest.mark.parametrize("plan,kind", [
    ("free", "personal"), ("go", "personal"), ("plus", "personal"), ("pro", "personal"),
    ("team", "workspace"), ("business", "workspace"), ("enterprise", "workspace"),
    ("edu", "workspace"), ("api-key", "unknown"), ("other", "unknown"),
])
def test_codex_kinds(state, plan, kind):
    plans.write_declared("codex", plan)
    assert plans.current(state.usage)["codex"].kind == kind


# --- The mismatch line -------------------------------------------------------

def test_work_claude_and_personal_codex_say_so_in_one_line(state):
    observed(state.usage, "plus")
    plans.write_declared("claude", "enterprise")
    line = mcp._codex_account_line()
    assert line == MISMATCH and "\n" not in line
    payload = {}
    mcp._report_files(payload, "task", str(state.project), [])
    assert payload["codex_account"] == MISMATCH


def test_personal_claude_and_work_codex_say_which_way(state):
    plans.write_declared("claude", "pro")
    plans.write_declared("codex", "business")
    assert mcp._codex_account_line() == (
        "Claude: personal account (you set it). Codex: work account (you set it). "
        "This sends code from a personal account to a work one.")


def test_unknown_claude_keeps_the_codex_only_line(state):
    observed(state.usage, "plus")
    assert mcp._codex_account_line() == (
        "Codex account: personal ChatGPT plan. If this is work code, check you may send it "
        "to a personal account.")
    plans.write_declared("claude", "other")
    assert mcp._codex_account_line().startswith("Codex account: personal ChatGPT plan.")


def test_same_kind_on_both_sides_keeps_the_codex_line(state):
    observed(state.usage, "business")
    plans.write_declared("claude", "team")
    assert mcp._codex_account_line() == "Codex account: workspace plan."


def test_the_first_call_notice_carries_the_mismatch_line(state, monkeypatch):
    from modelquad import disclosure

    monkeypatch.setattr(mcp, "_delegate_observing_the_caller", lambda *a, **k: pytest.fail("started"))
    monkeypatch.setattr("modelquad.disclosure.notice_shown", lambda: False)
    observed(state.usage, "plus")
    plans.write_declared("claude", "enterprise")
    result = asyncio.run(mcp.modelquad_delegate("review", "Review it.", str(state.project), files=None))
    payload = result.structured_content
    assert payload["status"] == "notice_first"
    assert payload["notice"] == [*disclosure.DATA_FLOW, MISMATCH]


def test_the_handoff_form_shows_the_mismatch_as_its_account_line(state, monkeypatch):
    observed(state.usage, "plus")
    plans.write_declared("claude", "enterprise")
    asked = []

    class Ctx:
        async def elicit(self, message, schema):
            asked.append(message)
            return SimpleNamespace(action="decline", data=None)

    monkeypatch.setattr(mcp, "modelquad_delegate", lambda **kwargs: pytest.fail("started"))
    asyncio.run(mcp.modelquad_handoff("review", "task", str(state.project), ctx=Ctx()))
    message, = asked
    assert message.splitlines()[2] == MISMATCH
    assert "enterprise" not in message and "plus" not in message


# --- Plan names stay on human surfaces --------------------------------------------

def _status_payload(monkeypatch) -> dict:
    monkeypatch.setattr(mcp, "build_providers", lambda *a, **k: [])
    return mcp._provider_status_payload()


def _words(text: str) -> set[str]:
    for mark in '.,():{}"':
        text = text.replace(mark, " ")
    return set(text.casefold().split())


@pytest.mark.parametrize("codex_seen", [None, "plus", "pro", "prolite", "business", "enterprise", "edu"])
def test_plan_names_never_reach_model_facing_replies(state, monkeypatch, codex_seen):
    observed(state.usage, codex_seen)
    names = _all_plan_names() | {"prolite"}
    for claude in plans.DECLARABLE_PLANS["claude"]:
        for codex in plans.DECLARABLE_PLANS["codex"]:
            plans.write_declared("claude", claude)
            plans.write_declared("codex", codex)
            line = mcp._codex_account_line()
            block = mcp._public_plans()
            assert not (_words(line) & names), line
            assert not (_words(json.dumps(block)) & names), block
            for side in block.values():
                assert set(side) == {"kind", "source"}
                assert side["kind"] in {"personal", "workspace", "unknown"}
                assert side["source"] in {"reported by Codex", "you set", "unknown"}
        status = _status_payload(monkeypatch)
        assert set(status["plans"]) == {"claude", "codex"}
        assert not (_words(json.dumps(status["plans"])) & names)


def test_modelquad_status_tool_carries_kind_words(state, monkeypatch):
    observed(state.usage, "plus")
    plans.write_declared("claude", "enterprise")
    status = _status_payload(monkeypatch)
    assert status["plans"] == {
        "claude": {"kind": "workspace", "source": "you set"},
        "codex": {"kind": "personal", "source": "reported by Codex"},
    }
    text = json.dumps(status)
    assert "enterprise" not in text and "plus" not in text


# --- The worker choice takes no plan --------------------------------------------

def test_worker_choice_and_its_configuration_take_no_plan():
    from modelquad import config, router, routing_policy

    callables = [router.Router.__init__, router.Router.route, config.load_config,
                 routing_policy.check_delegation_policy]
    for function in callables:
        for name in inspect.signature(function).parameters:
            assert "plan" not in name.casefold(), (function.__qualname__, name)
    for module in ("router.py", "routing_freshness.py", "routing_policy.py", "config.py",
                   "quota_policy.py"):
        tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or "plans" not in node.module.split("."), module
                assert all(alias.name != "plans" for alias in node.names), module
            elif isinstance(node, ast.Import):
                assert all("plans" not in alias.name.split(".") for alias in node.names), module
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"plans", "plan_type", "planType"}, module
            elif isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert name not in {"plans", "read_declared", "plan_type"}, module
