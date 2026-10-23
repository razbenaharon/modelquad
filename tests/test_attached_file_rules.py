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
    ("plus", "Codex account: personal ChatGPT plan. If this is work code, check you may send it "
             "to a personal account."),
    ("pro", "Codex account: personal ChatGPT plan. If this is work code, check you may send it "
            "to a personal account."),
    ("business", "Codex account: workspace plan."),
    ("enterprise", "Codex account: workspace plan."),
    ("edu", "Codex account: workspace plan."),
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
    assert f"Files to send (1, {size} bytes):\n- src/app.py ({size} bytes)" in message
    assert "Not sent: config/secrets.txt (denied_by_claude_settings)" in message
    assert message.endswith("Codex account: personal ChatGPT plan. If this is work code, check you may "
                            "send it to a personal account.")
    assert "plus" not in message.split("Codex account:")[1]
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
