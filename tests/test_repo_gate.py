# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The per-repository gate (``repo_policy.gate``), as the MCP tools and the executor use it.

* A file in another Git repository below the working folder refuses: that
  repository's own ``.modelquad.toml`` and the user's setting for it would be
  skipped (fail closed).
* ``account = "workspace"`` always refuses: this build does not read which
  Codex account is signed in, and unknown never passes.
* A kind that is not read-only is refused by the read-only gate, not blamed
  on a repository file.
* A valid file with an empty ``task_kinds`` reads as Codex off everywhere.
* The first-use notice is spent only on a call that would otherwise go ahead.
"""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import Mock

import anyio
import pytest

from modelquad import disclosure, repo_policy as rp, repo_settings as rs, review
from modelquad.execution import codex, noshell
from modelquad.execution.base import ExecutionRequest
from modelquad.models import TaskType
from modelquad.mcp import server as mcp, worker_guard


def git_repo(folder):
    (folder / ".git").mkdir(parents=True)
    return folder


def write_policy(folder, text):
    (folder / rp.FILE_NAME).write_text(text, encoding="utf-8", newline="\n")


def delegate(task_type, prompt, working_directory, files=None):
    """The data half of one ``modelquad_delegate`` call."""
    async def invoke():
        return await mcp.modelquad_delegate(task_type=task_type, prompt=prompt,
                                            working_directory=working_directory,
                                            files=files, timeout=60.0)

    return anyio.run(invoke).structured_content


def package(task_type, prompt, working_directory, files=None):
    """The data half of one ``modelquad_review_package`` call."""
    async def invoke():
        return await mcp.modelquad_review_package(task_type=task_type, prompt=prompt,
                                                  working_directory=working_directory, files=files)

    return anyio.run(invoke).structured_content


@pytest.fixture
def no_start(monkeypatch):
    """The executor is a tripwire: ``started`` fills when a call gets as far as Codex."""
    started = []

    class Reached(Exception):
        pass

    class Tripwire:
        def execute(self, request):
            started.append(1)
            raise Reached

    monkeypatch.setattr(mcp, "_executor", Tripwire)
    return started, Reached


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite's own ancestry never decides; the worker test sets the marker itself."""
    real = worker_guard.is_worker
    monkeypatch.setattr(worker_guard, "is_worker",
                        lambda *args: os.environ.get("MODELQUAD_WORKER") == "1" and real(*args))


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


def test_the_review_and_its_package_refuse_a_file_from_a_nested_repository(nested, no_start):
    started, _ = no_start
    parent, _ = nested
    assert rp.load(parent).refusal_for("review") is None, "the outer folder's policy alone would allow it"
    payload = delegate("review", "Review it.", str(parent), ["R/src/secret_logic.py"])
    assert payload["reason"] == rp.FILE_OUTSIDE_REPO and started == []
    assert payload["refused"] is True and payload["started"] is False
    assert "another Git repository" in payload["message"]
    shown = package("review", "Review it.", str(parent), ["R/src/secret_logic.py"])
    assert shown["reason"] == rp.FILE_OUTSIDE_REPO and shown["refused"] is True
    assert "files" not in shown, "a refused package lists nothing"


def test_a_nested_repository_the_user_turned_off_is_refused_too(tmp_path, no_start):
    started, _ = no_start
    parent = tmp_path / "parent"
    inner = git_repo(parent / "R")
    (inner / "a.py").write_text("x = 1\n", encoding="utf-8")
    rs.write_mode(inner, "off")
    payload = delegate("review", "Review it.", str(parent), ["R/a.py"])
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


def test_the_workspace_rule_refuses_because_the_account_is_unknown(tmp_path, no_start):
    started, _ = no_start
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, 'schema = 1\n[codex]\naccount = "workspace"\n')
    payload = delegate("review", "Review it.", str(repo))
    assert payload["reason"] == rp.ACCOUNT_WORKSPACE_REQUIRED and started == []
    assert payload["refused"] is True and payload["started"] is False
    assert "last 5 minutes" in payload["message"]
    assert package("review", "Review it.", str(repo))["reason"] == rp.ACCOUNT_WORKSPACE_REQUIRED


# ------------------------------------------------------- kinds and empty kinds


def test_a_kind_that_is_not_read_only_is_not_blamed_on_a_repository_file(tmp_path, no_start):
    started, _ = no_start
    repo = git_repo(tmp_path / "repo")
    assert rp.refusal(rp.load(repo), "implementation") is None
    assert rp.BUILTIN.refusal_for("implementation") is None
    payload = delegate("implementation", "Change it.", str(repo))
    assert payload["refused"] is True and started == [], payload
    # The read-only gate answers, as with no file at all.
    assert payload["reason"] == "task_type_not_read_only"
    assert "modelquad.toml" not in json.dumps(payload)


@pytest.mark.parametrize("kind", ["mutation", "write", "shell", ""])
def test_an_unknown_kind_is_a_closed_refusal_before_anything_starts(tmp_path, no_start, kind):
    started, _ = no_start
    repo = git_repo(tmp_path / "repo")
    # The tool itself answers with the refusal, not a raw ValueError.
    payload = delegate(kind, "Change it.", str(repo))
    assert payload["refused"] is True and payload["reason"] == "task_type_not_read_only"
    assert started == []
    shown = package(kind, "Change it.", str(repo))
    assert shown["refused"] is True and shown["reason"] == "task_type_not_read_only"


EMPTY_KINDS = b"schema = 1\n[codex]\ntask_kinds = []\n"


def test_empty_task_kinds_read_as_codex_off_everywhere(tmp_path, no_start):
    started, _ = no_start
    policy = rp.parse(EMPTY_KINDS)
    assert policy.facts()["codex_allowed"] is False
    applied = rs.effective(global_proactive=False, user_mode=None, policy=policy)
    assert (applied.refusal, applied.offer) == (rp.KIND_REMOVED, rs.OFF)
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, EMPTY_KINDS.decode())
    assert rs.advice_line(repo) == rs.ADVICE[rs.OFF]
    rules = mcp.modelquad_status(str(repo)).structured_content["folder_rules"]
    assert (rules["state"], rules["codex_allowed"], rules["task_kinds"]) == ("file", False, [])
    for kind in rp.TASK_KINDS:
        assert delegate(kind, "Review it.", str(repo))["reason"] == rp.KIND_REMOVED
    assert started == []


def test_the_shared_no_kinds_code_matches_the_repo_file_module():
    assert rs._REPO_FILE_NO_KINDS == rp.KIND_REMOVED


# --------------------------------------------- one policy read per delegate


def test_a_delegate_reads_the_repository_file_once_before_the_executor(tmp_path, no_start, monkeypatch):
    started, reached = no_start
    repo = git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    loads = []
    real = rp.load
    monkeypatch.setattr(rp, "load", lambda folder: loads.append(1) or real(folder))
    with pytest.raises(BaseException) as raised:
        delegate("review", "Review it.", str(repo), ["a.py"])
    assert raised.group_contains(reached) or raised.errisinstance(reached)
    assert started and len(loads) == 1


# --------------------------------------------------------- the first notice


REAL_NOTICE_SHOWN = disclosure.notice_shown


@pytest.fixture
def fresh_install(monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(root))
    monkeypatch.setattr(disclosure, "notice_shown", REAL_NOTICE_SHOWN)
    return root


def test_a_worker_gets_its_refusal_not_the_notice(fresh_install, tmp_path, monkeypatch, no_start):
    started, _ = no_start
    monkeypatch.setenv("MODELQUAD_WORKER", "1")
    payload = delegate("review", "Review it.", str(tmp_path))
    assert payload.get("status") != "notice_first" and payload["refused"] is True
    assert payload["reason"] == review.WORKER and started == []
    assert disclosure.notice_shown() is False, "no person saw a notice in a worker"


def test_a_repository_refusal_comes_before_the_notice(fresh_install, tmp_path, no_start):
    started, _ = no_start
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, "schema = 1\n[codex]\nallowed = false\n")
    payload = delegate("review", "Review it.", str(repo))
    assert payload["reason"] == rp.CODEX_OFF and started == []
    assert disclosure.notice_shown() is False
