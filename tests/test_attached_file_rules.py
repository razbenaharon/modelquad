# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The user controls exactly which files are sent to Codex.

Only the explicit ``files`` argument is attached; Claude Code Read deny
rules, Git ignores and ``.modelquadignore`` are honoured before any read; a
named file the rules withhold stops the review before Codex starts; the
package and the reply show what is sent; each launch carries a metadata-only
audit of it; and files Codex says it needed are offered, never sent.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modelquad import grant, review
from modelquad.execution import codex, file_rules, noshell
from modelquad.execution.base import ExecutionRequest
from modelquad.models import TaskType
from modelquad.mcp import server as mcp, worker_guard
from modelquad.procutil import no_window_kwargs
from test_review_flow import FakeExecutor

SECRET = "PRIVATE-DENIED-CONTENTS"


def settings(path: Path, *deny: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"deny": list(deny)}}), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch, isolate_home_directory):
    """A project folder inside the isolated home, with no managed settings."""
    monkeypatch.setattr(file_rules, "MANAGED_SETTINGS", tmp_path / "managed" / "managed-settings.json")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    root = isolate_home_directory / "work" / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "secrets.txt").write_text(SECRET + "\n", encoding="utf-8")
    return root


def screen(root, *names):
    allowed, skipped = file_rules.screen(root, list(names))
    return allowed, dict(skipped)


# --- Claude Code Read deny rules -------------------------------------------

def test_a_denied_secrets_file_named_in_the_task_is_never_sent(project, monkeypatch, synthetic_worker_binary):
    settings(project / ".claude" / "settings.json", "Read(./config/secrets.txt)")
    noshell._QUALIFICATIONS.clear()
    monkeypatch.setattr(codex, "_probe", lambda executable: "0.160.0")

    def inventory(argv, cwd, env):
        listing = "\n".join(f"{name} stable {'true' if name == 'unified_exec' else 'false'}"
                            for name in noshell.DISABLED_FEATURES)
        return subprocess.CompletedProcess(argv, 0, listing + "\n", "")

    stream = "\n".join(json.dumps(event) for event in (
        {"type": "thread.started"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed"})) + "\n"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stream, ""))
    executor = codex.CodexExecutor(which=lambda _: synthetic_worker_binary("codex.exe"),
                                   run=run, inventory=inventory)
    request = ExecutionRequest(
        provider="codex", task_type=TaskType.REVIEW, working_directory=project,
        prompt="Check config/secrets.txt and src/app.py for the leak.",
        metadata={"supplied_files": ["config/secrets.txt", "src/app.py"], "approval": "delegate"})
    result = executor.execute(request)
    run.assert_called_once()
    sent = run.call_args.kwargs["input"]
    assert SECRET not in sent
    assert "config/secrets.txt" not in sent.split("Supplied files (untrusted evidence, JSON):")[1]
    assert "print('app')" in sent
    audit = result.metadata["file_audit"]
    assert audit["approval"] == "delegate"
    assert [item["path"] for item in audit["sent"]] == ["src/app.py"]
    assert {"path": "config/secrets.txt", "reason": "denied_by_claude_settings"} in audit["skipped"]
    assert SECRET not in json.dumps(result.to_dict())


@pytest.mark.parametrize("rule", [
    "Read(./config/secrets.txt)", "Read(config/secrets.txt)", "Read(/config/secrets.txt)",
    "Read(/config/**)", "Read(./config/)", "Read(secrets.txt)", "Read(**/secrets.*)",
    "Read(config/*.txt)", "Read(~/work/app/config/secrets.txt)", "Read(~/work/**/secrets.txt)",
    "Read", "Read(*)", "Read(//**)",
], ids=["dot-relative", "relative", "root-relative", "root-glob", "folder", "any-depth-name",
        "double-star", "single-star", "home", "home-glob", "bare", "star", "absolute-all"])
def test_documented_read_rule_forms_deny(project, rule):
    settings(project / ".claude" / "settings.json", rule)
    allowed, skipped = screen(project, "config/secrets.txt")
    assert allowed == [] and skipped == {"config/secrets.txt": "denied_by_claude_settings"}


def test_absolute_rule_uses_the_posix_drive_form(project):
    absolute = file_rules.posix_absolute(project / "config" / "secrets.txt")
    settings(project / ".claude" / "settings.json", f"Read(/{absolute})")
    assert screen(project, "config/secrets.txt", "src/app.py") == (
        ["src/app.py"], {"config/secrets.txt": "denied_by_claude_settings"})


@pytest.mark.parametrize("rule", ["Read(./src/**)", "Read(*.py)", "Edit(./config/**)",
                                  "Bash(cat:*)", "Read(./config/other.txt)", "Read(//elsewhere/**)"],
                         ids=["other-folder", "other-extension", "edit-rule", "bash-rule",
                              "other-file", "other-absolute"])
def test_rules_that_do_not_cover_the_file_leave_it(project, rule):
    settings(project / ".claude" / "settings.json", rule)
    allowed, _ = screen(project, "config/secrets.txt")
    assert allowed == ["config/secrets.txt"]


@pytest.mark.parametrize("scope", ["managed", "user", "project", "local"])
def test_every_settings_scope_is_honoured(project, monkeypatch, isolate_home_directory, scope):
    path = {"managed": file_rules.MANAGED_SETTINGS,
            "user": isolate_home_directory / ".claude" / "settings.json",
            "project": project / ".claude" / "settings.json",
            "local": project / ".claude" / "settings.local.json"}[scope]
    settings(path, "Read(**/secrets.txt)")
    assert screen(project, "config/secrets.txt")[1] == {"config/secrets.txt": "denied_by_claude_settings"}


def test_claude_config_dir_moves_user_settings(project, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    settings(tmp_path / "custom" / "settings.json", "Read(./config/**)")
    assert screen(project, "config/secrets.txt")[1] == {"config/secrets.txt": "denied_by_claude_settings"}


@pytest.mark.parametrize("content", ["{not json", '{"permissions": {"deny": "Read(.env)"}}',
                                     '{"permissions": []}', "[]"],
                         ids=["unparseable", "deny-not-list", "permissions-not-object", "not-object"])
def test_unreadable_settings_skip_every_attachment(project, content):
    path = project / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    allowed, skipped = screen(project, "src/app.py", "config/secrets.txt")
    assert allowed == []
    assert set(skipped.values()) == {"claude_settings_unreadable"}


def test_unreadable_settings_still_run_the_task_without_files(project):
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{", encoding="utf-8")
    report, diagnostics = {}, []
    rendered = noshell.supplied_prompt("Review", project, files=["src/app.py"], report=report,
                                       diagnostics=diagnostics)
    assert "print('app')" not in rendered
    assert report["sent"] == []
    assert report["skipped"] == [{"path": "src/app.py", "reason": "claude_settings_unreadable"}]
    host, = [item.to_host_dict() for item in diagnostics]
    assert host["reason"] == "claude_settings_unreadable" and host["required"] is False
    assert "cannot honour Read deny rules" in host["guidance"]


def test_missing_settings_files_deny_nothing(project):
    assert screen(project, "src/app.py") == (["src/app.py"], {})


# --- .modelquadignore --------------------------------------------------------

def test_modelquadignore_uses_gitignore_syntax(project):
    (project / "notes").mkdir()
    for name in ("notes/a.md", "notes/keep.md", "src/gen.log", "src/deep/x.tmp"):
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text("x", encoding="utf-8")
    (project / ".modelquadignore").write_text(
        "# private notes\nnotes/\n!notes/keep.md\n*.log\n/src/deep/\nconfig/secrets.txt\n", encoding="utf-8")
    allowed, skipped = screen(project, "notes/a.md", "notes/keep.md", "src/gen.log", "src/deep/x.tmp",
                              "config/secrets.txt", "src/app.py")
    assert allowed == ["src/app.py"]
    # A file inside an excluded folder cannot be re-included, as in Git.
    assert skipped == {name: "modelquad_ignored" for name in (
        "notes/a.md", "notes/keep.md", "src/gen.log", "src/deep/x.tmp", "config/secrets.txt")}


def test_modelquadignore_negation_reincludes_a_file(project):
    (project / ".modelquadignore").write_text("config/*\n!config/secrets.txt\n", encoding="utf-8")
    assert screen(project, "config/secrets.txt") == (["config/secrets.txt"], {})


def test_modelquadignored_file_is_reported_not_refused(project):
    (project / ".modelquadignore").write_text("config/\n", encoding="utf-8")
    report = {}
    rendered = noshell.supplied_prompt("Review config/secrets.txt", project,
                                       files=["config/secrets.txt", "src/app.py"], report=report)
    assert SECRET not in rendered
    assert [item["path"] for item in report["sent"]] == ["src/app.py"]
    assert report["skipped"] == [{"path": "config/secrets.txt", "reason": "modelquad_ignored"}]


# --- Working folder other than the project root -------------------------------

def test_project_rules_apply_when_claude_names_a_subfolder(project):
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    (project / ".modelquadignore").write_text("src/app.py\n", encoding="utf-8")
    (project / "config" / "notes.txt").write_text("notes\n", encoding="utf-8")
    assert screen(project / "config", "secrets.txt", "notes.txt") == (
        [], {"secrets.txt": "denied_by_claude_settings", "notes.txt": "denied_by_claude_settings"})
    assert screen(project / "src", "app.py") == ([], {"app.py": "modelquad_ignored"})


def test_project_rules_apply_when_claude_names_a_folder_above_it(project):
    settings(project / ".claude" / "settings.local.json", "Read(./config/secrets.txt)")
    (project / ".modelquadignore").write_text("src/\n", encoding="utf-8")
    assert screen(project.parent, "app/config/secrets.txt", "app/src/app.py") == (
        [], {"app/config/secrets.txt": "denied_by_claude_settings", "app/src/app.py": "modelquad_ignored"})


def test_a_nested_modelquadignore_covers_only_its_own_folder(project):
    (project / "src" / ".modelquadignore").write_text("*.py\n", encoding="utf-8")
    (project / "top.py").write_text("x = 1\n", encoding="utf-8")
    assert screen(project, "src/app.py", "top.py") == (["top.py"], {"src/app.py": "modelquad_ignored"})


def test_unreadable_settings_above_the_working_folder_skip_every_attachment(project):
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    assert screen(project / "src", "app.py") == ([], {"app.py": "claude_settings_unreadable"})


def test_folders_above_the_home_folder_are_not_read(project, isolate_home_directory):
    settings(isolate_home_directory.parent / ".claude" / "settings.json", "Read(**)")
    (isolate_home_directory.parent / ".modelquadignore").write_text("*\n", encoding="utf-8")
    assert screen(project, "src/app.py") == (["src/app.py"], {})
    assert isolate_home_directory.parent not in file_rules.folders_above(project, ["src/app.py"])


def test_a_moved_user_config_leaves_the_default_folder_unread(project, monkeypatch, tmp_path,
                                                              isolate_home_directory):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    (isolate_home_directory / ".claude").mkdir(exist_ok=True)
    (isolate_home_directory / ".claude" / "settings.json").write_text("{stale", encoding="utf-8")
    assert screen(project / "src", "app.py") == (["app.py"], {})
    assert isolate_home_directory not in file_rules.folders_above(project, ["src/app.py"])


@pytest.mark.parametrize("rule", ["Read(//server/share/app/config/**)",
                                  r"Read(\\server\share\app\config\**)"])
def test_absolute_rules_cover_network_share_paths(rule):
    pattern = file_rules._read_rule(rule, Path(r"\\server\share\app"), Path.home())
    assert pattern.fullmatch(file_rules.posix_absolute(r"\\server\share\app\config\secrets.txt"))
    assert not pattern.fullmatch(file_rules.posix_absolute(r"\\server\share\app\src\app.py"))


def test_the_package_applies_project_rules_from_a_subfolder(project):
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    result = asyncio.run(mcp.modelquad_review_package(
        task_type="review", prompt="task", working_directory=str(project / "config"),
        files=["secrets.txt"]))
    text = "\n".join(item.text for item in result.content)
    assert result.structured_content["skipped"] == [
        {"path": "secrets.txt", "reason": "denied_by_claude_settings"}]
    assert result.structured_content["files"] == []
    assert "Left out: secrets.txt (denied_by_claude_settings)" in text
    assert SECRET not in text


def test_the_package_without_files_says_only_the_task_is_sent(project):
    result = asyncio.run(mcp.modelquad_review_package(
        task_type="review", prompt="task", working_directory=str(project)))
    assert result.content[0].text.startswith("Codex would get only the task text (")
    assert result.structured_content["files"] == [] and result.structured_content["skipped"] == []


# --- Delegate refuses before Codex starts when a named file is withheld --------

class _NotStarted:
    """The executor as a tripwire: a call that reaches it fails the test."""

    def execute(self, request):
        pytest.fail("Codex was started")


@pytest.fixture
def delegate(monkeypatch):
    """One ``modelquad_delegate`` call, as Claude Code makes it, outside a worker."""
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    def call(project, files, executor=None, prompt="task"):
        monkeypatch.setattr(mcp, "_executor", lambda: executor or _NotStarted())
        return asyncio.run(mcp.modelquad_delegate(
            task_type="review", prompt=prompt, working_directory=str(project), timeout=60.0,
            files=files))

    return call


def test_delegate_stops_before_codex_when_a_named_file_is_withheld(project, monkeypatch, delegate):
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    monkeypatch.setattr(mcp, "_STANDING_GRANT", False)
    result = delegate(project, ["config/secrets.txt", "src/app.py"])
    payload = result.structured_content
    assert payload["started"] is False and payload["reason"] == review.FILES_WITHHELD
    assert payload["skipped_files"] == ["config/secrets.txt (denied_by_claude_settings)"]
    text = "\n".join(item.text for item in result.content)
    assert result.content[0].text.splitlines()[0] == "Codex was not started."
    assert "config/secrets.txt" in text
    # Never invite Claude to paste what the user excluded.
    assert "Never paste an excluded file's contents into the task text" in text
    assert SECRET not in text


def test_a_folder_without_a_grant_is_refused_for_that_before_the_files(project, monkeypatch, delegate):
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    monkeypatch.setattr(mcp, "_STANDING_GRANT", True)
    monkeypatch.setattr(grant, "allows", lambda folder: False)
    monkeypatch.setattr(review, "_withheld_refusal", lambda *args: pytest.fail("screened"))
    result = delegate(project, ["config/secrets.txt"])
    assert result.structured_content["reason"] == grant.NO_GRANT


def test_unreadable_settings_name_the_cause_once(project):
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{", encoding="utf-8")
    refusal = review._withheld_refusal(str(project), ("src/app.py", "config/secrets.txt"))
    message = refusal["message"]
    assert message.count("could not be read") == 1
    assert "tell the user" in message and "Call again without them" not in message


def test_delegate_goes_on_when_every_named_file_is_sendable(project):
    assert review._withheld_refusal(str(project), ("src/app.py",)) is None
    assert review._withheld_refusal(str(project), ()) is None


def test_a_git_ignored_named_file_stops_the_call_before_codex(project):
    git = _git()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run([git, "init", "-q"], cwd=project, check=True, env=env, **no_window_kwargs())
    (project / ".gitignore").write_text("private/\n", encoding="utf-8")
    (project / "private").mkdir()
    (project / "private" / "change.diff").write_text("x", encoding="utf-8")
    refusal = review._withheld_refusal(str(project), ("private/change.diff", "src/app.py"))
    assert refusal is not None
    assert refusal["skipped_files"] == ["private/change.diff (git_ignored)"]
    assert "Git ignores it" in refusal["message"]


# --- Git ignores -------------------------------------------------------------

def _git():
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is not installed")
    return executable


def test_git_ignored_files_are_skipped(project):
    git = _git()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run([git, "init", "-q"], cwd=project, check=True, env=env, **no_window_kwargs())
    (project / ".gitignore").write_text("*.secret\nbuild/\n", encoding="utf-8")
    (project / "token.secret").write_text("x", encoding="utf-8")
    (project / "build").mkdir()
    (project / "build" / "out.py").write_text("x", encoding="utf-8")
    allowed, skipped = screen(project, "token.secret", "build/out.py", "src/app.py")
    assert allowed == ["src/app.py"]
    assert skipped == {"token.secret": "git_ignored", "build/out.py": "git_ignored"}


def test_outside_a_repository_the_git_check_is_skipped(project):
    _git()
    (project / ".gitignore").write_text("*.py\n", encoding="utf-8")  # no repository: inert
    assert screen(project, "src/app.py") == (["src/app.py"], {})


def _git_answers(monkeypatch, outcome):
    """Git as found, giving ``outcome``: an exception to raise or (exit code, stderr)."""
    from modelquad import executable_resolution

    monkeypatch.setattr(executable_resolution, "resolve_executable",
                        lambda *args, **kwargs: SimpleNamespace(path="git.exe", refusal=None))

    def run(command, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        code, stderr = outcome
        return subprocess.CompletedProcess(command, code, stdout=b"", stderr=stderr)

    monkeypatch.setattr(file_rules.subprocess, "run", run)


@pytest.mark.parametrize("outcome", [
    subprocess.TimeoutExpired("git", file_rules.GIT_SECONDS),
    (128, b"fatal: detected dubious ownership in repository at 'E:/x'"),
    (129, b"usage: git check-ignore"),
    (128, b"fatal: not a git repository: C:/moved/.git/worktrees/x"),
    PermissionError("blocked by security software"),
], ids=["timed_out", "dubious_ownership", "other_failure", "broken_worktree_link", "launch_blocked"])
def test_git_that_does_not_answer_sends_no_file(project, monkeypatch, outcome):
    _git_answers(monkeypatch, outcome)

    assert file_rules.git_ignored(project, ["src/app.py"]) is None
    assert screen(project, "src/app.py") == ([], {"src/app.py": "git_check_failed"})
    refusal = review._withheld_refusal(str(project), ("src/app.py",))
    assert refusal["skipped_files"] == ["src/app.py (git_check_failed)"]
    assert "Git did not answer" in refusal["message"] and "tell the user" in refusal["message"]


def test_git_outside_a_repository_is_no_check_not_a_failure(project, monkeypatch):
    _git_answers(monkeypatch, (128, b"fatal: not a git repository (or any of the parent directories): .git"))

    assert screen(project, "src/app.py") == (["src/app.py"], {})


def _commit_all(folder, git, env):
    subprocess.run([git, "add", "-A"], cwd=folder, check=True, env=env, capture_output=True,
                   **no_window_kwargs())
    subprocess.run([git, "-c", "user.email=lab@example.invalid", "-c", "user.name=lab", "commit",
                    "-q", "-m", "x"], cwd=folder, check=True, env=env, **no_window_kwargs())


def test_a_file_in_a_submodule_is_checked_against_the_submodule(project):
    git = _git()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    inner = project / "vendor"
    inner.mkdir()
    subprocess.run([git, "init", "-q"], cwd=inner, check=True, env=env, **no_window_kwargs())
    (inner / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    (inner / "lib.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(inner, git, env)
    (inner / "key.secret").write_text("x", encoding="utf-8")
    subprocess.run([git, "init", "-q"], cwd=project, check=True, env=env, **no_window_kwargs())
    _commit_all(project, git, env)  # records vendor as a submodule (an embedded repository)

    allowed, skipped = screen(project, "src/app.py", "vendor/lib.py", "vendor/key.secret")

    assert allowed == ["src/app.py", "vendor/lib.py"]
    assert skipped == {"vendor/key.secret": "git_ignored"}


def test_an_unchecked_rule_at_launch_is_not_the_approved_review():
    approved = {"app.py": "abc"}
    report = {"sent": [], "skipped": [{"path": "app.py", "reason": "git_check_failed"}]}
    assert noshell.changed_since_approval(report, approved)
    # A rule that was checked and now withholds the file sends less, which the approval covers.
    report["skipped"][0]["reason"] = "git_ignored"
    assert not noshell.changed_since_approval(report, approved)


def test_without_git_the_git_check_is_skipped(project, monkeypatch):
    from modelquad import executable_resolution

    monkeypatch.setattr(executable_resolution, "resolve_executable",
                        lambda *args, **kwargs: SimpleNamespace(path=None, refusal="not_found"))
    assert file_rules.git_ignored(project, ["src/app.py"]) == frozenset()


# --- Explicit files only -----------------------------------------------------

def test_task_text_traceback_and_diff_names_are_never_attached(project):
    prompt = ('Relevant files:\n- config/secrets.txt\n\nFile "src/app.py", line 1\n'
              "diff --git a/src/app.py b/src/app.py\nSee config/secrets.txt")
    report = {}
    rendered = noshell.supplied_prompt(prompt, project, report=report)
    assert SECRET not in rendered and "print('app')" not in rendered
    assert report == {"sent": [], "skipped": []}


def test_more_than_32_files_refuse(project):
    with pytest.raises(noshell.SuppliedContextRefused, match="supplied_files_over_limit"):
        noshell.supplied_prompt("Review", project, files=[f"f{index}.py" for index in range(33)])


def test_sent_entry_digest_is_of_the_sent_text(project):
    report = {}
    noshell.supplied_prompt("Review", project, files=["src/app.py"], report=report)
    data = "print('app')\n".encode("utf-8")
    assert report["sent"] == [{"path": "src/app.py", "bytes": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()[:16]}]


def test_closed_file_audit_drops_anything_outside_its_shape():
    audit = noshell.closed_file_audit({
        "approval": "delegate", "extra": "dropped",
        "sent": [{"path": "src/a.py", "bytes": 3, "sha256": "0123456789abcdef", "text": "x"},
                 {"path": "C:/abs.py", "bytes": 1, "sha256": "0123456789abcdef"},
                 {"path": "b.py", "bytes": 1, "sha256": "not-a-digest"}],
        "skipped": [{"path": ".env", "reason": "denied_by_claude_settings"},
                    {"path": "x.py", "reason": "free prose"}]})
    assert audit == {"approval": "delegate", "total_bytes": 4,
                     "sent": [{"path": "src/a.py", "bytes": 3, "sha256": "0123456789abcdef"},
                              {"path": "<path>", "bytes": 1, "sha256": "0123456789abcdef"}],
                     "skipped": [{"path": ".env", "reason": "denied_by_claude_settings"}]}
    assert noshell.closed_file_audit({"approval": "self-approved", "sent": [], "skipped": []}) is None


# --- MCP surface -------------------------------------------------------------

def test_both_tools_take_an_explicit_bounded_files_argument():
    tools = mcp.server._tool_manager._tools
    for name in ("modelquad_delegate", "modelquad_review_package"):
        schema = tools[name].parameters["properties"]["files"]
        array, = [item for item in schema["anyOf"] if item.get("type") == "array"]
        assert array["maxItems"] == 32 and array["items"] == {"type": "string"}
        assert "files" in tools[name].description


def test_a_direct_delegate_call_is_approved_through_the_delegate_prompt(project, delegate):
    executor = FakeExecutor()
    delegate(project, ["src/app.py"], executor)
    request, = executor.requests
    assert request.metadata["supplied_files"] == ["src/app.py"]
    assert request.metadata["approval"] == "delegate"


def test_more_than_32_files_are_refused_by_the_tool(project, delegate):
    from mcp.server.mcpserver.exceptions import ToolError

    names = [f"f{index}.py" for index in range(33)]
    with pytest.raises(ToolError):
        asyncio.run(mcp.server.call_tool("modelquad_delegate", {
            "task_type": "review", "prompt": "task", "working_directory": str(project),
            "files": names}))
    # Reached without the schema (a direct call), the flow refuses them itself.
    payload = delegate(project, names).structured_content
    assert payload["refused"] is True and payload["reason"] == "files_not_valid"


def test_reply_reports_what_was_sent(project, delegate):
    data = "print('app')\n".encode("utf-8")
    result = delegate(project, ["src/app.py"], FakeExecutor())
    payload = result.structured_content
    assert payload["package"]["files"] == [
        {"path": "src/app.py", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16]}]
    assert payload["skipped_files"] == []
    assert f"Sent: the task text and 1 file(s), {len(data)} bytes" in result.content[0].text


# --- Files Codex said it needed: offered, never sent --------------------------

def _answer(files_needed):
    envelope = {"task_status": "completed", "reason": None, "limitations": [],
                "files_needed": files_needed, "evidence": {"summary": "read the file"}}
    return "Looks fine.\n```json\n" + json.dumps(envelope) + "\n```"


def test_reply_lists_only_sendable_files_codex_said_it_needed(project):
    (project / "src" / "util.py").write_text("x = 1\n", encoding="utf-8")
    (project / "src" / "hidden.py").write_text("x = 2\n", encoding="utf-8")
    (project / ".modelquadignore").write_text("src/hidden.py\n", encoding="utf-8")
    (project / ".env").write_text("A=1\n", encoding="utf-8")
    executor = FakeExecutor(needed=["src/app.py", "src\\util.py", "./src/util.py", "src/hidden.py",
                                    ".env", "missing.py", "../outside.py", "C:/x.py", "src"])
    reply = review.run(review.ReviewRequest(task_type="review", prompt="task",
                                            working_directory=str(project), files=("src/app.py",)),
                       executor)
    # Sent already, ignored by the rules, secret-shaped, missing, outside the
    # folder or a folder: none of these is offered.
    assert reply["completion_request"]["files"] == ["src/util.py"]
    assert reply["completion_request"]["note"] == review.COMPLETION_NOTE
    # Offered to the user, never added by itself.
    assert "Nothing is added by itself" in review.COMPLETION_NOTE
    assert len(executor.requests) == 1


def test_an_answer_naming_no_usable_file_adds_nothing(project):
    malformed = "```json\n" + json.dumps({"task_status": "completed", "files_needed": "src/app.py"}) + "\n```"
    for output in ("A plain answer.", _answer([]), malformed, None, _answer([7, None, ""])):
        assert noshell.needed_files(output, str(project)) == [], output
    reply = review.run(review.ReviewRequest(task_type="review", prompt="task",
                                            working_directory=str(project), files=("src/app.py",)),
                       FakeExecutor(needed=[]))
    assert "completion_request" not in reply


def test_needed_files_are_bounded(project):
    names = [f"src/f{index}.py" for index in range(20)]
    for name in names:
        (project / name).write_text("x\n", encoding="utf-8")
    assert noshell.needed_files(_answer(names), str(project)) == names[:noshell.MAX_NEEDED_FILES]


def test_delegate_reply_offers_needed_files_and_starts_nothing_more(project, delegate):
    (project / "src" / "util.py").write_text("x = 1\n", encoding="utf-8")
    executor = FakeExecutor(needed=["src/util.py"])
    result = delegate(project, ["src/app.py"], executor)
    visible = json.loads(result.content[-1].text)
    assert visible["completion_request"]["files"] == ["src/util.py"]
    assert visible["completion_request"]["note"] == review.COMPLETION_NOTE
    assert "src/util.py" in result.content[0].text
    # The one approved call ran; naming a file never starts another.
    request, = executor.requests
    assert request.metadata["supplied_files"] == ["src/app.py"]


# --- The approval covers the contents the user approved ----------------------

def _clean_executor(monkeypatch, synthetic_worker_binary):
    noshell._QUALIFICATIONS.clear()
    monkeypatch.setattr(codex, "_probe", lambda executable: "0.160.0")

    def inventory(argv, cwd, env):
        listing = "\n".join(f"{name} stable {'true' if name == 'unified_exec' else 'false'}"
                            for name in noshell.DISABLED_FEATURES)
        return subprocess.CompletedProcess(argv, 0, listing + "\n", "")

    stream = "\n".join(json.dumps(event) for event in (
        {"type": "thread.started"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed"})) + "\n"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stream, ""))
    return run, codex.CodexExecutor(which=lambda _: synthetic_worker_binary("codex.exe"),
                                    run=run, inventory=inventory)


def _approved_request(project, digests):
    return ExecutionRequest(
        provider="codex", task_type=TaskType.REVIEW, working_directory=project, prompt="Review it.",
        metadata={"supplied_files": ["src/app.py"], "approval": "delegate", "approved_digests": digests})


def test_a_file_changed_after_approval_is_not_sent(project, monkeypatch, synthetic_worker_binary):
    digests = noshell.approved_digests("Review it.", project, ["src/app.py"])
    (project / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    run, executor = _clean_executor(monkeypatch, synthetic_worker_binary)
    result = executor.execute(_approved_request(project, digests))
    run.assert_not_called()
    assert result.task_status_reason == "supplied_file_changed"
    assert "changed after the user approved" in result.error


def test_an_unchanged_file_is_sent_as_approved(project, monkeypatch, synthetic_worker_binary):
    digests = noshell.approved_digests("Review it.", project, ["src/app.py"])
    run, executor = _clean_executor(monkeypatch, synthetic_worker_binary)
    result = executor.execute(_approved_request(project, digests))
    run.assert_called_once()
    assert [item["sha256"] for item in result.metadata["file_audit"]["sent"]] == [digests["src/app.py"]]


def test_a_file_the_approval_did_not_send_is_not_sent_later(project, monkeypatch, synthetic_worker_binary):
    # At approval the file was withheld (an empty record); now it would be sent.
    run, executor = _clean_executor(monkeypatch, synthetic_worker_binary)
    result = executor.execute(_approved_request(project, {}))
    run.assert_not_called()
    assert result.task_status_reason == "supplied_file_changed"


def test_approved_digests_match_the_audit_of_the_text_as_sent(project):
    digests = noshell.approved_digests("task", project, ["src/app.py"])
    report = {}
    noshell.supplied_prompt("task", project, files=["src/app.py"], completion_protocol=True, report=report)
    assert digests == {item["path"]: item["sha256"] for item in report["sent"]}
    assert noshell.changed_since_approval(report, digests) is False
    assert noshell.changed_since_approval(report, {"src/app.py": "0" * 16}) is True
