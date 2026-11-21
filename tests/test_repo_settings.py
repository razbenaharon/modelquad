# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The user's own per-repository setting and how the layers combine.

``modelquad settings repo <on|off|offer-only|inherit>`` is stored under a keyed
hash, never a path or a folder name. One function combines the layers: safety
floors in code, restrictions as the most restrictive layer, and the offer mode
as the user's per-repo word over the global setting, then only lowered by the
repository file. ``off`` refuses before any process starts, at the MCP gates
and in the executor's recheck. The repository file still cannot raise
proactive use. ``account = "workspace"`` always refuses: this build does not
read which Codex account is signed in, and neither a plan the user declared
nor a reading an earlier build left behind counts.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from unittest.mock import Mock

import anyio
import pytest

from modelquad import repo_policy as rp, repo_settings as rs
from modelquad.adapters.server_instructions import preferences_path, write_proactive
from modelquad.cli import main
from modelquad.execution import codex, noshell
from modelquad.execution.base import (
    NOSHELL_PRELAUNCH_REFUSALS, ExecutionRequest, attempt_resolution,
)
from modelquad.identity import installation_id, reset_installation_id
from modelquad.mcp import server as mcp, worker_guard
from modelquad.models import TaskType

KEY = "0123456789abcdef0123456789abcdef"  # gitleaks:allow (synthetic installation id)


def git_repo(folder):
    """A repository as Git lays it out, without running Git."""
    (folder / ".git").mkdir(parents=True)
    return folder


def write_policy(folder, text):
    (folder / rp.FILE_NAME).write_text(text, encoding="utf-8", newline="\n")


# ------------------------------------------------------------- precedence

ON_REQUEST_FILE = rp.parse(b'schema = 1\n[offers]\ncodex = "on_request"\n')
OFF_FILE = rp.parse(b"schema = 1\n[codex]\nallowed = false\n")
OFF_AND_ON_REQUEST = rp.parse(b'schema = 1\n[codex]\nallowed = false\n[offers]\ncodex = "on_request"\n')
BROKEN_FILE = rp.parse(b"schema = 9\n")
PLAIN_FILE = rp.parse(b"schema = 1\n")
POLICIES = {"none": None, "plain": PLAIN_FILE, "on_request": ON_REQUEST_FILE,
            "off": OFF_FILE, "off+on_request": OFF_AND_ON_REQUEST, "broken": BROKEN_FILE}

# (global proactive, user per-repo word) -> offer mode with no repo file.
PREFERENCE = {
    (False, None): rs.OFFER, (True, None): rs.PROACTIVE,
    (False, "on"): rs.PROACTIVE, (True, "on"): rs.PROACTIVE,
    (False, "offer-only"): rs.OFFER, (True, "offer-only"): rs.OFFER,
}

TABLE = []
for (glob, user), base in PREFERENCE.items():
    TABLE += [
        (glob, user, "none", None, base),
        (glob, user, "plain", None, base),
        (glob, user, "on_request", None, rs.ON_REQUEST),
        (glob, user, "off", rp.CODEX_OFF, rs.OFF),
        (glob, user, "off+on_request", rp.CODEX_OFF, rs.OFF),
        (glob, user, "broken", rp.INVALID, rs.OFF),
    ]
for glob, policy in itertools.product((False, True), POLICIES):
    # The user's own off refuses whatever the other layers say.
    TABLE.append((glob, "off", policy, rs.USER_CODEX_OFF, rs.OFF))


def test_the_table_covers_every_combination():
    combos = {(glob, user, policy) for glob, user, policy, _, _ in TABLE}
    assert combos == set(itertools.product((False, True), (None, *rs.MODES), POLICIES))


@pytest.mark.parametrize("glob,user,policy,refusal,offer", TABLE)
def test_precedence(glob, user, policy, refusal, offer):
    result = rs.effective(global_proactive=glob, user_mode=user, policy=POLICIES[policy])
    assert (result.refusal, result.offer) == (refusal, offer)
    assert result.codex_allowed is (refusal is None)


def _rank(mode):
    return {rs.OFF: 0, rs.ON_REQUEST: 1, rs.OFFER: 2, rs.PROACTIVE: 3}[mode]


@pytest.mark.parametrize("text", [
    b"schema = 1\nproactive = true\n",
    b'schema = 1\n[offers]\ncodex = "default"\nproactive = "on"\n',
    b'schema = 1\n[codex]\nproactive = true\nallowed = true\n',
    b'schema = 1\n[offers]\ncodex = "proactive"\n',
    b'schema = 1\n[offers]\ncodex = "on"\n',
    b"schema = 1\ntrusted = true\nauto_approve = true\n",
    b'schema = 1\n[offers]\ncodex = "on_request"\n',
    b"schema = 2\n",
])
@pytest.mark.parametrize("glob,user", list(PREFERENCE))
def test_the_repo_file_can_never_raise_the_offer_mode(text, glob, user):
    without = rs.effective(global_proactive=glob, user_mode=user, policy=None)
    with_file = rs.effective(global_proactive=glob, user_mode=user, policy=rp.parse(text))
    assert _rank(with_file.offer) <= _rank(without.offer)
    if not glob and user is None:
        assert with_file.offer != rs.PROACTIVE


def test_the_gate_puts_the_users_off_first_and_keeps_the_file_rules(tmp_path):
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, "schema = 1\n[codex]\nallowed = false\n")
    assert rp.gate(str(repo), "review")[0]["reason"] == rp.CODEX_OFF
    rs.write_mode(repo, "off")
    assert rp.gate(str(repo), "review")[0]["reason"] == rs.USER_CODEX_OFF
    rs.write_mode(repo, "on")
    write_policy(repo, 'schema = 1\n[codex]\ntask_kinds = ["review"]\n')
    assert rp.gate(str(repo), "architecture")[0]["reason"] == rp.KIND_REMOVED
    refusal, policy = rp.gate(str(repo), "review")
    assert refusal is None and policy.task_kinds == frozenset({"review"})


def test_shared_constants_agree_with_the_repo_file_module():
    assert rs.REPO_FILE_NAME == rp.FILE_NAME
    assert rs._REPO_FILE_OFF == rp.CODEX_OFF
    assert rs.REASONS <= NOSHELL_PRELAUNCH_REFUSALS
    assert rs.REASONS <= noshell.REFUSAL_LABELS


# ------------------------------------------------------------- the repo key


def test_the_key_is_shared_by_subfolders_and_worktrees_and_starts_no_process(tmp_path, monkeypatch):
    main_repo = git_repo(tmp_path / "main")
    (main_repo / "src" / "deep").mkdir(parents=True)
    worktree_dir = main_repo / ".git" / "worktrees" / "wt"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {worktree_dir}\n", encoding="utf-8")
    other = git_repo(tmp_path / "other")

    def refuse(*args, **kwargs):
        raise AssertionError("the repo key must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    key = rs.repo_key(main_repo, KEY)
    assert key and len(key) == 32 and int(key, 16) >= 0
    assert rs.repo_key(main_repo / "src" / "deep", KEY) == key
    assert rs.repo_key(worktree, KEY) == key
    assert rs.repo_key(other, KEY) != key
    assert rs.repo_key(main_repo, "f" * 32) != key
    plain = tmp_path / "no-git-here"
    plain.mkdir()
    assert rs.repo_key(plain, KEY) not in (None, key)
    assert rs.repo_key(main_repo, None) is None
    assert rs.repo_key(main_repo, "not-a-key") is None


def test_settings_follow_the_installation_id(tmp_path):
    repo = git_repo(tmp_path / "repo")
    rs.write_mode(repo, "off")
    assert rs.read_mode(repo) == "off"
    reset_installation_id()
    # A reset unlinks every stored entry: nothing matches the new id.
    assert rs.read_mode(repo) is None


@pytest.mark.parametrize("choice", rs.MODES)
def test_write_read_and_inherit(tmp_path, choice):
    repo = git_repo(tmp_path / "repo")
    other = git_repo(tmp_path / "other")
    assert rs.write_mode(repo, choice) == choice
    rs.write_mode(other, "on")
    assert rs.read_mode(repo) == choice and rs.read_mode(other) == "on"
    assert rs.write_mode(repo, "inherit") is None
    assert rs.read_mode(repo) is None and rs.read_mode(other) == "on"
    rs.write_mode(other, "inherit")
    assert "repos" not in json.loads(preferences_path().read_text(encoding="utf-8"))


def test_other_preferences_are_kept_and_malformed_entries_dropped(tmp_path):
    repo = git_repo(tmp_path / "repo")
    write_proactive(True)
    path = preferences_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["plans"] = {"codex": "plus"}  # a key this build does not write: kept as it is
    payload["repos"] = {"C:\\Users\\x": "on", "a" * 32: "always", "b" * 32: "off"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    rs.write_mode(repo, "offer-only")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["proactive"] is True and stored["plans"] == {"codex": "plus"}
    assert stored["repos"] == {"b" * 32: "off", rs.repo_key(repo, installation_id()): "offer-only"}


def test_an_unknown_choice_is_refused(tmp_path):
    with pytest.raises(ValueError):
        rs.write_mode(git_repo(tmp_path / "repo"), "always")


def test_no_path_or_folder_name_reaches_preferences(tmp_path):
    repo = git_repo(tmp_path / "John Smith" / "OneDrive - Contoso Ltd" / "Secret Project Falcon")
    (repo / "src").mkdir()
    for choice in rs.MODES:
        rs.write_mode(repo / "src", choice)
        text = preferences_path().read_text(encoding="utf-8")
        for word in ("John", "Smith", "Contoso", "Secret", "Falcon", "Project", "OneDrive", "src",
                     tmp_path.name, ":\\", ":/", "\\\\", "/"):
            assert word not in text, word
        stored = json.loads(text)["repos"]
        assert list(stored.values()) == [choice]
        assert all(len(key) == 32 and set(key) <= set("0123456789abcdef") for key in stored)


# -------------------------------------------------------------------- CLI


def test_cli_show_set_and_inherit(tmp_path, capsys):
    repo = git_repo(tmp_path / "repo")
    assert main(["settings", "repo", "--project", str(repo)]) == 0
    assert "inherit" in capsys.readouterr().out
    assert main(["settings", "repo", "off", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "This repository: off" in out and str(repo) not in out
    assert rs.read_mode(repo) == "off"
    assert main(["settings", "repo", "offer-only", "--project", str(repo)]) == 0
    assert "Claude offers Codex" in capsys.readouterr().out
    write_policy(repo, 'schema = 1\n[offers]\ncodex = "on_request"\n')
    assert main(["settings", "repo", "on", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "This repository: on" in out and "only when you ask" in out
    assert main(["settings", "repo", "inherit", "--project", str(repo)]) == 0
    assert rs.read_mode(repo) is None
    with pytest.raises(SystemExit) as exit_info:
        main(["settings", "repo", "always", "--project", str(repo)])
    assert exit_info.value.code == 2
    assert main(["settings", "repo", "on", "--project", str(tmp_path / "missing")]) == 2


# -------------------------------------------------------------- the gates


@pytest.fixture
def no_start(monkeypatch):
    """The executor is a tripwire: ``started`` fills when a call gets past the gates."""
    started = []

    class Reached(Exception):
        pass

    class Tripwire:
        def execute(self, request):
            started.append(1)
            raise Reached

    monkeypatch.setattr(mcp, "_executor", Tripwire)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    return started, Reached


def delegate(repo):
    async def invoke():
        return await mcp.delegate(task_type="review", prompt="Review this.",
                                            working_directory=str(repo), timeout=60.0)

    return anyio.run(invoke)


def package(repo):
    async def invoke():
        return await mcp.review_package(task_type="review", prompt="Review this.",
                                                  working_directory=str(repo))

    return anyio.run(invoke)


def test_off_refuses_the_review_and_its_package_before_anything_starts(tmp_path, no_start):
    started, _ = no_start
    repo = git_repo(tmp_path / "repo")
    rs.write_mode(repo, "off")
    result = delegate(repo)
    payload = result.structured_content
    assert started == []
    assert (payload["refused"], payload["executed"], payload["reason"]) == (True, False, rs.USER_CODEX_OFF)
    assert payload["started"] is False
    assert "settings repo" in payload["message"] and str(repo) not in json.dumps(payload)
    assert result.content[0].text.splitlines()[0] == "Codex was not started."
    shown = package(repo).structured_content
    assert shown["reason"] == rs.USER_CODEX_OFF and shown["started"] is False
    assert started == []


@pytest.mark.parametrize("choice", ["on", "offer-only"])
def test_on_and_offer_only_never_widen_the_repo_file(tmp_path, no_start, choice):
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, "schema = 1\n[codex]\nallowed = false\n")
    rs.write_mode(repo, choice)
    payload = delegate(repo).structured_content
    assert payload["reason"] == rp.CODEX_OFF and no_start[0] == []


def test_on_lets_an_allowed_call_through(tmp_path, no_start):
    started, reached = no_start
    repo = git_repo(tmp_path / "repo")
    rs.write_mode(repo, "on")
    with pytest.raises(BaseException) as raised:
        delegate(repo)
    assert raised.group_contains(reached) or raised.errisinstance(reached)
    assert started


# --------------------------------- account = "workspace": unknown never passes


def _usage(directory, plan_type):
    """The reading an earlier build stored: a successful Codex read made just now."""
    from datetime import datetime, timezone

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "usage-codex.json").write_text(json.dumps({
        "plan_type": plan_type, "available": True, "quota_error": None,
        "observed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")


def _declare(plan):
    """The plan an earlier build's ``settings plan`` stored in preferences."""
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plans": {"codex": plan}}), encoding="utf-8")


@pytest.mark.parametrize("declared", [None, "business", "enterprise", "team"])
def test_a_declared_plan_never_meets_the_workspace_rule(tmp_path, no_start, declared):
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, 'schema = 1\n[codex]\naccount = "workspace"\n')
    if declared:
        _declare(declared)
    payload = delegate(repo).structured_content
    assert payload["reason"] == rp.ACCOUNT_WORKSPACE_REQUIRED and no_start[0] == []
    assert "does not count" in payload["message"]


@pytest.mark.parametrize("reported", ["business", "enterprise", "plus", "mystery"])
def test_a_stored_reading_never_meets_the_workspace_rule(tmp_path, no_start, reported):
    started, _ = no_start
    repo = git_repo(tmp_path / "repo")
    write_policy(repo, 'schema = 1\n[codex]\naccount = "workspace"\n')
    from modelquad.paths import state_root

    for directory in (state_root(), state_root() / "usage"):
        _usage(directory, reported)
    refusal, _ = rp.gate(str(repo), "review")
    assert refusal["reason"] == rp.ACCOUNT_WORKSPACE_REQUIRED
    payload = delegate(repo).structured_content
    assert payload["reason"] == rp.ACCOUNT_WORKSPACE_REQUIRED and started == []


# ------------------------------------------------------ the executor recheck


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
    repo = git_repo(tmp_path / "repo")
    request = ExecutionRequest(provider="codex", task_type=TaskType.REVIEW, prompt="Review a.py",
                               working_directory=repo)
    return executor, request, run, calls


def test_executor_rechecks_the_users_off_and_spawns_nothing(worker):
    executor, request, run, calls = worker
    rs.write_mode(request.working_directory, "off")
    result = executor.execute(request)
    run.assert_not_called()
    assert calls == []
    assert result.task_status_reason == rs.USER_CODEX_OFF
    assert result.metadata["worker_started"] is False
    assert attempt_resolution(result) == ("refused", "policy_refused")


def test_executor_applies_the_account_rule(worker, tmp_path):
    executor, request, run, calls = worker
    write_policy(request.working_directory, 'schema = 1\n[codex]\naccount = "workspace"\n')
    _declare("business")
    _usage(tmp_path / "usage", "business")
    result = executor.execute(request)
    run.assert_not_called()
    assert calls == [] and result.task_status_reason == rp.ACCOUNT_WORKSPACE_REQUIRED
    assert result.metadata["worker_started"] is False
