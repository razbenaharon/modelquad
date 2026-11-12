# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The user controls exactly which files are sent to Codex.

Only the explicit ``files`` argument is attached; Claude Code Read deny
rules, Git ignores and ``.modelquadignore`` are honoured before any read; the
approval and the reply show what is sent; each launch leaves a metadata-only
audit record; and the Codex account kind is shown, never the plan name.
"""

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modelquad.execution import codex, file_rules, noshell
from modelquad.execution.base import ExecutionRequest, ExecutionResult, WorkerTaskStatus
from modelquad.models import TaskType
from modelquad.mcp import server as mcp
from modelquad.procutil import no_window_kwargs

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
        metadata={"supplied_files": ["config/secrets.txt", "src/app.py"], "approval": "handoff"})
    result = executor.execute(request)
    run.assert_called_once()
    sent = run.call_args.kwargs["input"]
    assert SECRET not in sent
    assert "config/secrets.txt" not in sent.split("Supplied files (untrusted evidence, JSON):")[1]
    assert "print('app')" in sent
    audit = result.metadata["file_audit"]
    assert audit["approval"] == "handoff"
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


def test_handoff_form_applies_project_rules_from_a_subfolder(project, monkeypatch):
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    ctx = _Ctx(SimpleNamespace(action="cancel", data=None))
    monkeypatch.setattr(mcp, "modelquad_delegate", lambda **kwargs: pytest.fail("started"))
    asyncio.run(mcp.modelquad_handoff("review", "task", str(project / "config"), ctx=ctx,
                                      files=["secrets.txt"]))
    assert "Not sent: secrets.txt (denied_by_claude_settings)" in ctx.asked[0]
    assert SECRET not in ctx.asked[0]



# --- Delegate refuses before Codex starts when a named file is withheld --------

def test_delegate_stops_before_codex_when_a_named_file_is_withheld(project, monkeypatch):
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    monkeypatch.setattr(mcp, "_STANDING_GRANT", False)
    monkeypatch.setattr(mcp, "_notice_before_first_call", lambda *args: None)
    monkeypatch.setattr(mcp, "_delegate_payload", lambda *args, **kwargs: pytest.fail("started"))
    result = asyncio.run(mcp.modelquad_delegate(
        "review", "task", str(project), files=["config/secrets.txt", "src/app.py"]))
    payload = result.structured_content
    assert payload["started"] is False and payload["reason"] == mcp.FILES_WITHHELD
    assert payload["skipped_files"] == ["config/secrets.txt (denied_by_claude_settings)"]
    text = "\n".join(item.text for item in result.content)
    assert "Codex was not started." in text and "config/secrets.txt" in text
    # Never invite Claude to paste what the user excluded.
    assert "Never paste an excluded file's contents into the task text" in text
    assert SECRET not in text


def test_a_folder_without_a_grant_is_refused_for_that_before_the_files(project, monkeypatch):
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    monkeypatch.setattr(mcp, "_STANDING_GRANT", True)
    monkeypatch.setattr(mcp._grant, "allows", lambda folder: False)
    monkeypatch.setattr(mcp, "_withheld_refusal", lambda *args: pytest.fail("screened"))
    result = asyncio.run(mcp.modelquad_delegate(
        "review", "task", str(project), files=["config/secrets.txt"]))
    assert result.structured_content["reason"] == mcp._grant.NO_GRANT


def test_unreadable_settings_name_the_cause_once(project):
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{", encoding="utf-8")
    refusal = mcp._withheld_refusal(str(project), ["src/app.py", "config/secrets.txt"])
    message = refusal.structured_content["message"]
    assert message.count("could not be read") == 1
    assert "tell the user" in message and "Call again without them" not in message


def test_delegate_goes_on_when_every_named_file_is_sendable(project):
    assert mcp._withheld_refusal(str(project), ["src/app.py"]) is None
    assert mcp._withheld_refusal(str(project), []) is None


def test_a_git_ignored_named_file_stops_the_call_before_codex(project):
    git = _git()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run([git, "init", "-q"], cwd=project, check=True, env=env, **no_window_kwargs())
    (project / ".gitignore").write_text("private/\n", encoding="utf-8")
    (project / "private").mkdir()
    (project / "private" / "change.diff").write_text("x", encoding="utf-8")
    refusal = mcp._withheld_refusal(str(project), ["private/change.diff", "src/app.py"])
    assert refusal is not None
    assert refusal.structured_content["skipped_files"] == ["private/change.diff (git_ignored)"]
    assert "Git ignores it" in refusal.structured_content["message"]


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
        "approval": "handoff", "extra": "dropped",
        "sent": [{"path": "src/a.py", "bytes": 3, "sha256": "0123456789abcdef", "text": "x"},
                 {"path": "C:/abs.py", "bytes": 1, "sha256": "0123456789abcdef"},
                 {"path": "b.py", "bytes": 1, "sha256": "not-a-digest"}],
        "skipped": [{"path": ".env", "reason": "denied_by_claude_settings"},
                    {"path": "x.py", "reason": "free prose"}]})
    assert audit == {"approval": "handoff", "total_bytes": 4,
                     "sent": [{"path": "src/a.py", "bytes": 3, "sha256": "0123456789abcdef"},
                              {"path": "<path>", "bytes": 1, "sha256": "0123456789abcdef"}],
                     "skipped": [{"path": ".env", "reason": "denied_by_claude_settings"}]}
    assert noshell.closed_file_audit({"approval": "self-approved", "sent": [], "skipped": []}) is None


# --- Audit record --------------------------------------------------------------

def test_audit_survives_persistence_and_is_scoped_to_the_project(project, tmp_path):
    from modelquad import project_notes
    from modelquad.adapters.pending_reminder import project_key
    from modelquad.service import history

    audit = noshell.file_audit({"sent": [{"path": "src/app.py", "bytes": 13, "sha256": "0123456789abcdef"}],
                                "skipped": [{"path": ".env", "reason": "denied_by_claude_settings"}]},
                               "handoff")
    result = ExecutionResult(provider="codex", success=True, output="ok", exit_code=0,
                             task_status=WorkerTaskStatus.COMPLETED,
                             metadata={"file_audit": audit, "unrelated": "dropped"})
    persisted = project_notes.private_result(result)
    assert persisted.metadata["file_audit"] == audit
    db = tmp_path / "history.db"
    history.record_delegation_attempt(db, task_type="review", decision_id="d-1",
                                      working_directory=str(project))
    history.record_execution(db, "review", replace(persisted), source="mcp", decision_id="d-1")
    row_id = history.fetch_activity(db, limit=1)[0]["id"]
    key = project_key(str(project))
    entry = history.fetch_activity_detail(db, "execution", row_id, workspace_key=key)
    record = entry["file_audit"]
    assert record["project_key"] == key
    assert record["approval"] == "handoff"
    assert record["sent_files"] == audit["sent"] and record["total_bytes"] == 13
    assert record["skipped_files"] == [{"path": ".env", "reason": "denied_by_claude_settings"}]
    assert record["recorded_at"]
    other = tmp_path / "other"
    other.mkdir()
    assert history.fetch_activity_detail(db, "execution", row_id,
                                         workspace_key=project_key(str(other))) is None


# --- MCP surface -------------------------------------------------------------

def test_both_tools_take_an_explicit_bounded_files_argument():
    tools = mcp.server._tool_manager._tools
    for name in ("modelquad_delegate", "modelquad_handoff"):
        schema = tools[name].parameters["properties"]["files"]
        array, = [item for item in schema["anyOf"] if item.get("type") == "array"]
        assert array["maxItems"] == 32 and array["items"] == {"type": "string"}
        assert "files" in tools[name].description


def usage(directory: Path, plan):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "usage-codex.json").write_text(json.dumps({"provider": "codex", "plan_type": plan}),
                                                encoding="utf-8")


@pytest.mark.parametrize("plan,line", [
    ("plus", "Codex account: personal. If this is work code, check you may send it "
             "to a personal account."),
    ("pro", "Codex account: personal. If this is work code, check you may send it "
            "to a personal account."),
    ("business", "Codex account: work."),
    ("enterprise", "Codex account: work."),
    ("edu", "Codex account: work."),
    ("mystery", "Codex account: unknown."),
    (None, "Codex account: unknown."),
])
def test_account_line_names_the_kind_never_the_plan(isolate_usage_directory, monkeypatch, plan, line):
    monkeypatch.setattr(mcp, "USAGE_DIRECTORY", None)
    usage(isolate_usage_directory, plan)
    assert mcp._codex_account_line() == line


def test_account_is_unknown_without_a_quota_read(isolate_usage_directory, monkeypatch):
    monkeypatch.setattr(mcp, "USAGE_DIRECTORY", None)
    assert mcp._codex_account_line() == "Codex account: unknown."


class _Ctx:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def elicit(self, message, schema):
        self.asked.append(message)
        return self.answer


def test_handoff_form_lists_files_bytes_skips_and_account(project, isolate_usage_directory, monkeypatch):
    monkeypatch.setattr(mcp, "USAGE_DIRECTORY", None)
    usage(isolate_usage_directory, "plus")
    settings(project / ".claude" / "settings.json", "Read(./config/**)")
    started = []
    monkeypatch.setattr(mcp, "modelquad_delegate", lambda **kwargs: started.append(kwargs))
    ctx = _Ctx(SimpleNamespace(action="decline", data=None))
    result = asyncio.run(mcp.modelquad_handoff(
        "review", "Look at config/secrets.txt", str(project), ctx=ctx,
        files=["src/app.py", "config/secrets.txt"]))
    assert started == [] and result.structured_content["started"] is False
    message, = ctx.asked
    size = (project / "src" / "app.py").stat().st_size
    assert f"Files to send (1, {size} bytes): src/app.py ({size} bytes)" in message
    assert "Not sent: config/secrets.txt (denied_by_claude_settings)" in message
    assert ("Codex account: personal. If this is work code, check you may "
            "send it to a personal account.") in message.splitlines()
    assert "plus" not in message.split("Codex account:")[1].split("\nTask:")[0]
    assert SECRET not in message


def test_handoff_form_without_files_says_only_the_task_is_sent(project, monkeypatch):
    ctx = _Ctx(SimpleNamespace(action="cancel", data=None))
    monkeypatch.setattr(mcp, "modelquad_delegate", lambda **kwargs: pytest.fail("started"))
    asyncio.run(mcp.modelquad_handoff("review", "task", str(project), ctx=ctx))
    assert "Files to send: none (only the task text)." in ctx.asked[0]
    assert "Codex account: unknown." in ctx.asked[0]


def test_accepted_handoff_delegates_the_files_as_a_handoff(project, monkeypatch):
    calls = []

    def fake_payload(*args, **kwargs):
        calls.append(kwargs)
        return {"job_id": "job-1", "status": "running"}

    monkeypatch.setattr(mcp, "_delegate_payload", fake_payload)
    monkeypatch.setattr(mcp, "MIN_HUMAN_ANSWER_SECONDS", 0.0)  # a person's answer
    ctx = _Ctx(SimpleNamespace(action="accept", data=SimpleNamespace(hand_off=True)))
    result = asyncio.run(mcp.modelquad_handoff("review", "task", str(project), ctx=ctx,
                                               timeout=60.0, files=["src/app.py"]))
    call, = calls
    assert call["supplied_files"] == ["src/app.py"] and call["approval"] == "handoff"
    assert result.structured_content["codex_account"] == "Codex account: unknown."
    # A direct delegate call is approved through the delegate prompt.
    asyncio.run(mcp.modelquad_delegate("review", "task", str(project), timeout=60.0, files=["src/app.py"]))
    assert calls[1]["approval"] == "delegate"


def test_more_than_32_files_are_refused_by_the_tool(project):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        asyncio.run(mcp.modelquad_delegate("review", "task", str(project), timeout=60.0,
                                           files=[f"f{index}.py" for index in range(33)]))


def test_reply_reports_sent_skipped_and_suggests_unsent_names(project):
    payload = {"metadata": {"file_audit": {
        "approval": "delegate", "total_bytes": 13,
        "sent": [{"path": "src/app.py", "bytes": 13, "sha256": "0123456789abcdef"}],
        "skipped": [{"path": "config/secrets.txt", "reason": "denied_by_claude_settings"}]}}}
    mcp._report_files(payload, "Look at src/app.py", str(project), ["src/app.py"])
    assert payload["sent_files"] == [{"path": "src/app.py", "bytes": 13, "sha256": "0123456789abcdef"}]
    assert payload["skipped_files"] == [{"path": "config/secrets.txt", "reason": "denied_by_claude_settings"}]
    assert payload["codex_account"] == "Codex account: unknown."
    assert "suggested_files" not in payload
    bare = {}
    mcp._report_files(bare, 'Traceback: File "src/app.py", line 1; see missing/none.py', str(project), [])
    assert bare["suggested_files"] == ["src/app.py"]
    assert "not sent" in bare["suggested_files_note"]


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
    payload = {"output": _answer(["src/app.py", "src\\util.py", "./src/util.py", "src/hidden.py", ".env",
                                  "missing.py", "../outside.py", "C:/x.py", "src"]),
               "metadata": {"file_audit": {
                   "approval": "delegate", "total_bytes": 13, "skipped": [],
                   "sent": [{"path": "src/app.py", "bytes": 13, "sha256": "0123456789abcdef"}]}}}
    mcp._report_files(payload, "task", str(project), ["src/app.py"])
    # Sent already, ignored by the rules, secret-shaped, missing, outside the
    # folder or a folder: none of these is offered.
    assert payload["files_needed"] == ["src/util.py"]
    assert payload["files_needed_note"] == noshell.NEEDED_FILES_NOTE
    assert "agreement" in noshell.NEEDED_FILES_NOTE


def test_an_answer_naming_no_usable_file_adds_nothing(project):
    malformed = "```json\n" + json.dumps({"task_status": "completed", "files_needed": "src/app.py"}) + "\n```"
    for output in ("A plain answer.", _answer([]), malformed, None, _answer([7, None, ""])):
        payload = {"output": output}
        mcp._report_files(payload, "task", str(project), [])
        assert "files_needed" not in payload and "files_needed_note" not in payload


def test_needed_files_are_bounded(project):
    names = [f"src/f{index}.py" for index in range(20)]
    for name in names:
        (project / name).write_text("x\n", encoding="utf-8")
    assert noshell.needed_files(_answer(names), str(project)) == names[:noshell.MAX_NEEDED_FILES]


def test_delegate_reply_offers_needed_files_and_starts_nothing_more(project, monkeypatch):
    (project / "src" / "util.py").write_text("x = 1\n", encoding="utf-8")
    calls = []

    def fake_payload(*args, **kwargs):
        calls.append(kwargs)
        return {"task_type": "review", "provider": "codex", "status": "completed",
                "output": _answer(["src/util.py"])}

    monkeypatch.setattr(mcp, "_delegate_payload", fake_payload)
    result = asyncio.run(mcp.modelquad_delegate("review", "task", str(project), timeout=60.0,
                                                files=["src/app.py"]))
    visible = json.loads(result.content[-1].text)
    assert visible["files_needed"] == ["src/util.py"]
    assert visible["files_needed_note"] == noshell.NEEDED_FILES_NOTE
    # The one approved call ran; naming a file never starts another.
    call, = calls
    assert call["supplied_files"] == ["src/app.py"]


def test_collected_answer_lists_needed_files(project):
    from modelquad.mcp import jobs_tools

    (project / "src" / "util.py").write_text("x = 1\n", encoding="utf-8")
    response = jobs_tools._with_needed_files({"output": _answer(["src/util.py"])}, str(project))
    assert response["files_needed"] == ["src/util.py"]
    assert "files_needed" not in jobs_tools._with_needed_files({"error": "unavailable"}, str(project))


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
