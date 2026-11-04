# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The per-repository gate (``repo_policy.gate``), as the MCP tools and the executor use it.

* A file in another Git repository below the working folder refuses: that
  repository's own ``.modelquad.toml`` and the user's setting for it would be
  skipped (fail closed).
* ``account = "workspace"`` needs a fresh, successful Codex reading and an
  exact plan name; an old, failed or unknown one refuses.
* A kind that is not read-only is refused by the read-only gate, not blamed
  on a repository file.
* A valid file with an empty ``task_kinds`` reads as Codex off everywhere.
* The first-use notice is spent only on a call that would otherwise go ahead.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import subprocess
from unittest.mock import Mock

import pytest

from modelquad import disclosure, plans, repo_policy as rp, repo_settings as rs
from modelquad.execution import ExecutionManager, codex, noshell
from modelquad.execution.base import ExecutionRequest
from modelquad.models import TaskType
from modelquad.mcp import server as mcp
from modelquad.usage.refresh import FRESH_SECONDS


def git_repo(folder):
    (folder / ".git").mkdir(parents=True)
    return folder


def write_policy(folder, text):
    (folder / rp.FILE_NAME).write_text(text, encoding="utf-8", newline="\n")


def usage(directory, plan_type, *, age=0.0, available=True, error=None):
    directory.mkdir(parents=True, exist_ok=True)
    observed = datetime.now(timezone.utc) - timedelta(seconds=age)
    (directory / "usage-codex.json").write_text(json.dumps({
        "plan_type": plan_type, "available": available, "quota_error": error,
        "observed_at": observed.isoformat()}), encoding="utf-8")


@pytest.fixture
def no_start(monkeypatch):
    started = []

    class Reached(Exception):
        pass

    def tripwire(*args, **kwargs):
        started.append(1)
        raise Reached

    monkeypatch.setattr(mcp, "_build_execution_manager", tripwire)
    monkeypatch.setattr(mcp, "start_job", tripwire)
    monkeypatch.setattr(ExecutionManager, "delegate", tripwire)
    return started, Reached


# ------------------------------------------------ files in a nested repository


@pytest.fixture
def nested(tmp_path):
    parent = tmp_path / "parent"
    inner = git_repo(parent / "R")
    write_policy(inner, "schema = 1\n[codex]\nallowed = false\n")
    (inner / "src").mkdir()
    (inner / "src" / "secret_logic.py").write_text("x = 1\n", encoding="utf-8")
    (parent / "top.py").write_text("y = 2\n", encoding="utf-8")
    return parent, inner


def test_a_file_in_a_nested_repository_is_found(nested):
    parent, _ = nested
    assert rp.file_in_other_repository(parent, ["R/src/secret_logic.py"]) is True
    assert rp.file_in_other_repository(parent, ["top.py"]) is False
    assert rp.file_in_other_repository(parent, ["R"]) is False, "a name is a file, not a folder"
    assert rp.file_in_other_repository(parent / "R", ["src/secret_logic.py"]) is False
    # Names the packer refuses itself are left to it.
    assert rp.file_in_other_repository(parent, ["../R/src/secret_logic.py", "C:/x/y.py"]) is False


def test_a_submodule_pointer_file_counts_as_another_repository(tmp_path):
    outer = git_repo(tmp_path / "outer")
    sub = outer / "vendor" / "lib"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/lib\n", encoding="utf-8")
    assert rp.file_in_other_repository(outer, ["vendor/lib/code.py"]) is True


def test_delegate_and_handoff_refuse_a_file_from_a_nested_repository(nested, no_start):
    started, _ = no_start
    parent, _ = nested
    assert rp.load(parent).refusal_for("review") is None, "the outer folder's policy alone would allow it"
    payload = mcp._delegate_payload("review", "Review it.", str(parent), "codex",
                                    supplied_files=["R/src/secret_logic.py"])
    assert payload["reason"] == rp.FILE_OUTSIDE_REPO and started == []
    assert "another Git repository" in payload["message"]
    refusal = mcp._handoff_refusal("review", None, None, str(parent), ["R/src/secret_logic.py"])
    assert refusal["reason"] == rp.FILE_OUTSIDE_REPO


def test_a_nested_repository_the_user_turned_off_is_refused_too(tmp_path, no_start):
    started, _ = no_start
    parent = tmp_path / "parent"
    inner = git_repo(parent / "R")
    (inner / "a.py").write_text("x = 1\n", encoding="utf-8")
    rs.write_mode(inner, "off")
    payload = mcp._delegate_payload("review", "Review it.", str(parent), "codex", supplied_files=["R/a.py"])
    assert payload["reason"] == rp.FILE_OUTSIDE_REPO and started == []


@pytest.fixture
def worker(tmp_path, monkeypatch, synthetic_worker_binary):
    noshell._QUALIFICATIONS.clear()
    monkeypatch.setattr(codex, "_probe", lambda executable: "0.160.0")
    calls = []

    def inventory(argv, cwd, env):
        calls.append(list(argv))
        output = ("\n".join(f"{name} under development {str(name == 'unified_exec').lower()}"
                            for name in noshell.DISABLED_FEATURES) + "\n") if "features" in argv else "[]"
        return subprocess.CompletedProcess(argv, 0, output, "")

    stream = "\n".join(json.dumps(event) for event in (
        {"type": "thread.started", "thread_id": "inert"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})) + "\n"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stream, ""))
    executor = codex.CodexExecutor(which=lambda _: synthetic_worker_binary("codex.exe"),
                                   run=run, inventory=inventory)
    return executor, run, calls


def test_the_executor_rechecks_nested_repositories(worker, nested):
    executor, run, calls = worker
    parent, _ = nested
    request = ExecutionRequest(provider="codex", task_type=TaskType.REVIEW, prompt="Review it",
                               working_directory=parent,
                               metadata={"supplied_files": ["R/src/secret_logic.py"]})
    result = executor.execute(request)
    run.assert_not_called()
    assert calls == [] and result.task_status_reason == rp.FILE_OUTSIDE_REPO


# ------------------------------------------------------ the workspace account


@pytest.mark.parametrize("plan_type,age,available,error,kind", [
    ("team", 0, True, None, plans.WORKSPACE),
    ("business", FRESH_SECONDS - 30, True, None, plans.WORKSPACE),
    ("team", FRESH_SECONDS + 30, True, None, plans.UNKNOWN),      # an old reading
    ("team", 0, False, "not logged in", plans.UNKNOWN),           # signed out, kept fields
    ("team", 0, True, "app_server_failed", plans.UNKNOWN),        # a failed read
    ("team", -3600, True, None, plans.UNKNOWN),                   # from the future
    ("plus", 0, True, None, plans.PERSONAL),
    ("reduced", 0, True, None, plans.UNKNOWN),                    # contains "edu"
    ("steam", 0, True, None, plans.UNKNOWN),                      # contains "team"
    ("enterprise-trial", 0, True, None, plans.UNKNOWN),
])
def test_only_a_fresh_signed_in_exact_plan_counts(tmp_path, plan_type, age, available, error, kind):
    usage(tmp_path, plan_type, age=age, available=available, error=error)
    assert plans.reported_codex_kind(tmp_path) == kind


def test_the_account_window_is_the_quota_freshness_window():
    assert plans.ACCOUNT_EVIDENCE_SECONDS == FRESH_SECONDS


def test_an_old_workspace_reading_refuses_the_workspace_rule(tmp_path, no_start, isolate_usage_directory):
    started, _ = no_start
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, 'schema = 1\n[codex]\naccount = "workspace"\n')
    usage(isolate_usage_directory, "team", age=FRESH_SECONDS * 10)
    payload = mcp._delegate_payload("review", "Review it.", str(repo), "codex")
    assert payload["reason"] == rp.ACCOUNT_WORKSPACE_REQUIRED and started == []
    assert "last 5 minutes" in payload["message"]


# ------------------------------------------------------- kinds and empty kinds


def test_a_kind_that_is_not_read_only_is_not_blamed_on_a_repository_file(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(mcp, "start_job", lambda *a, **k: started.append(1))
    monkeypatch.setattr(ExecutionManager, "delegate", lambda *a, **k: started.append(1))
    repo = git_repo(tmp_path / "repo")
    assert rp.refusal(rp.load(repo), "implementation") is None
    assert rp.BUILTIN.refusal_for("implementation") is None
    payload = mcp._delegate_payload("implementation", "Change it.", str(repo), "codex")
    assert payload["refused"] is True and started == [], payload
    # The capability check or the read-only gate answers, as with no file at all.
    assert payload["reason"] in {"routing_refused", "task_type_not_read_only"}
    assert payload.get("reason") != rp.KIND_REMOVED
    assert "modelquad.toml" not in json.dumps(payload)


EMPTY_KINDS = b"schema = 1\n[codex]\ntask_kinds = []\n"


def test_empty_task_kinds_read_as_codex_off_everywhere(tmp_path):
    policy = rp.parse(EMPTY_KINDS)
    assert policy.facts()["codex_allowed"] is False
    applied = rs.effective(global_proactive=False, user_mode=None, policy=policy)
    assert (applied.refusal, applied.offer) == (rp.KIND_REMOVED, rs.OFF)
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, EMPTY_KINDS.decode())
    assert rs.advice_line(repo) == rs.ADVICE[rs.OFF]
    line = mcp._repo_policy_line(mcp._repo_policy_view(str(repo)))
    assert line == "This repository: Codex is off (its .modelquad.toml allows no task kinds)."


def test_the_shared_no_kinds_code_matches_the_repo_file_module():
    assert rs._REPO_FILE_NO_KINDS == rp.KIND_REMOVED


# ------------------------------------------------------------- status lines


@pytest.mark.parametrize("view,expected", [
    ({"state": "unknown", "why": "no_project_directory"}, "no project folder given"),
    ({"state": "unknown", "why": "project_directory_unusable"}, "not a local folder"),
    ({"state": "unknown", "why": "unreadable"}, "could not be read"),
])
def test_the_status_line_says_why_the_repository_was_not_checked(view, expected):
    assert expected in mcp._repo_policy_line(view)


def test_the_broken_file_line_does_not_ask_claude_to_echo_doctor():
    line = mcp._repo_policy_line({"state": "known", "refusal": rp.INVALID})
    assert "in a terminal" in line and "Run: modelquad doctor" not in line


# --------------------------------------------- one policy read per delegate


def test_a_delegate_reads_the_repository_file_once_before_the_executor(tmp_path, no_start, monkeypatch):
    started, reached = no_start
    repo = git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    loads = []
    real = rp.load
    monkeypatch.setattr(rp, "load", lambda folder: loads.append(1) or real(folder))
    with pytest.raises(reached):
        mcp._delegate_payload("review", "Review it.", str(repo), "codex", supplied_files=["a.py"])
    assert started and len(loads) == 1


# ------------------------------------------------------------ refusal shape


def test_both_per_repository_refusals_carry_the_launch_state():
    for reason in (rs.USER_CODEX_OFF, rp.CODEX_OFF):
        payload = mcp._launch_refusal("review", reason)
        assert "routing_mode" in payload and "routing_paused" in payload, reason


# --------------------------------------------------------- the first notice


REAL_NOTICE_SHOWN = disclosure.notice_shown


@pytest.fixture
def fresh_install(monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(root))
    monkeypatch.setattr(disclosure, "notice_shown", REAL_NOTICE_SHOWN)
    return root


def test_a_worker_gets_its_refusal_not_the_notice(fresh_install, tmp_path, monkeypatch):
    monkeypatch.setenv("MODELQUAD_WORKER", "1")
    result = asyncio.run(mcp.modelquad_delegate("review", "Review it.", str(tmp_path), files=None))
    payload = result.structured_content
    assert payload.get("status") != "notice_first" and payload["refused"] is True
    assert disclosure.notice_shown() is False, "no person saw a notice in a worker"


def test_a_repository_refusal_comes_before_the_notice(fresh_install, tmp_path, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, "schema = 1\n[codex]\nallowed = false\n")
    monkeypatch.setattr(mcp, "_delegate_observing_the_caller",
                        lambda function, *a, **k: asyncio.sleep(0, result=function()))
    result = asyncio.run(mcp.modelquad_delegate("review", "Review it.", str(repo), files=None))
    assert result.structured_content["reason"] == rp.CODEX_OFF
    assert disclosure.notice_shown() is False


def test_the_handoff_form_is_not_asked_before_the_notice(fresh_install, tmp_path):
    from types import SimpleNamespace

    ctx = SimpleNamespace(client_capabilities=SimpleNamespace(elicitation=object()),
                          protocol_version="2026-07-28", request_id=1)
    assert mcp._handoff_question("review", "Review it.", str(tmp_path), None, None, ctx) == "not_asked"
