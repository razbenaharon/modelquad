# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The review reminder: advisory context after several files change, never a decision."""

from __future__ import annotations

import io
import json

import pytest

from modelquad.adapters import review_reminder as rr

SESSION = "session-7e3c1f0a"


def _event(tool="Edit", path="C:/proj/a.py", session=SESSION, **extra):
    event = {"hook_event_name": "PostToolUse", "tool_name": tool, "session_id": session,
             "cwd": "C:/proj", "transcript_path": "C:/never/opened.jsonl",
             "tool_input": {"file_path": path}}
    event.update(extra)
    return io.BytesIO(json.dumps(event).encode("utf-8"))


def _run(tmp_path, stdin, *, mode="decide", environ=None):
    out = io.StringIO()
    assert rr.run_hook("claude", stdin, out, environ=environ or {}, state=tmp_path,
                       mode=lambda cwd: mode) == 0
    return out.getvalue()


def _context(output):
    payload = json.loads(output)
    assert set(payload) == {"hookSpecificOutput"}
    inner = payload["hookSpecificOutput"]
    assert set(inner) == {"hookEventName", "additionalContext"}
    assert inner["hookEventName"] == "PostToolUse"
    return inner["additionalContext"]


def _run_tests(tmp_path, mode="decide"):
    return _run(tmp_path, _event(tool="Bash", tool_input={"command": "python -m pytest -q"}),
                mode=mode)


def test_it_reminds_when_a_command_runs_after_three_different_files(tmp_path):
    assert _run(tmp_path, _event(path="C:/proj/a.py")) == ""
    assert _run(tmp_path, _event(path="C:/proj/a.py")) == ""  # the same file again
    assert _run(tmp_path, _event(tool="Write", path="C:/proj/b.py")) == ""
    assert _run_tests(tmp_path) == ""  # two files: not yet
    assert _run(tmp_path, _event(tool="MultiEdit", path="C:/proj/c.py")) == ""  # no reminder mid-edit
    text = _context(_run_tests(tmp_path))
    assert "changed 3 files" in text and "Before your final message" in text
    assert "`modelquad_delegate`" in text and "trigger review_gate" in text
    assert "declines, carry on without Codex" in text
    assert _run_tests(tmp_path) == ""  # no new edit since


def test_it_reminds_at_most_twice(tmp_path):
    for name in ("a", "b", "c"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"))
    assert _context(_run_tests(tmp_path))
    _run(tmp_path, _event(path="C:/proj/d.py"))
    assert "changed 4 files" in _context(_run(tmp_path, _event(
        tool="PowerShell", tool_input={"command": "python -m pytest -q"})))
    _run(tmp_path, _event(path="C:/proj/e.py"))
    assert _run_tests(tmp_path) == ""


def test_a_command_alone_keeps_nothing(tmp_path):
    assert _run_tests(tmp_path) == ""
    assert not (tmp_path / rr.FOLDER_NAME).exists()


def test_a_codex_call_starts_the_count_again(tmp_path):
    for name in ("a", "b", "c"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"))
    assert _run(tmp_path, _event(tool="mcp__plugin_modelquad_modelquad__modelquad_delegate",
                                 tool_input={})) == ""
    for name in ("d", "e"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"))
    assert _run_tests(tmp_path) == ""
    _run(tmp_path, _event(path="C:/proj/f.py"))
    assert "changed 3 files" in _context(_run_tests(tmp_path))


def test_in_offer_mode_it_says_offer(tmp_path):
    for name in ("a", "b", "c"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"), mode="offer")
    text = _context(_run_tests(tmp_path, mode="offer"))
    assert "offer the user a Codex review" in text and "modelquad_delegate" not in text


@pytest.mark.parametrize("mode", [None, "on_request"])
def test_nothing_when_codex_is_off_or_only_on_request(tmp_path, mode):
    for name in ("a", "b", "c", "d"):
        assert _run(tmp_path, _event(path=f"C:/proj/{name}.py"), mode=mode) == ""
    assert _run_tests(tmp_path, mode=mode) == ""


def test_it_keeps_digests_not_paths(tmp_path):
    for name in ("secret-client-name", "b", "c"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"))
    (record,) = (tmp_path / rr.FOLDER_NAME).iterdir()
    stored = record.read_text(encoding="utf-8")
    assert "secret-client-name" not in stored and "proj" not in stored
    assert SESSION not in record.name and SESSION not in stored


def test_sessions_are_counted_apart(tmp_path):
    for index, name in enumerate(("a", "b", "c")):
        assert _run(tmp_path, _event(path=f"C:/proj/{name}.py", session=f"s{index}")) == ""
    for index in range(3):
        assert _run(tmp_path, _event(tool="Bash", session=f"s{index}",
                                     tool_input={"command": "ls"})) == ""


@pytest.mark.parametrize("stdin", [
    io.BytesIO(b"not json"),
    io.BytesIO(json.dumps({"hook_event_name": "SessionStart"}).encode()),
    io.BytesIO(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Read",
                           "session_id": SESSION, "tool_input": {"file_path": "C:/a"}}).encode()),
    io.BytesIO(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Edit",
                           "session_id": SESSION, "tool_input": "x"}).encode()),
])
def test_anything_else_prints_nothing_and_keeps_nothing(tmp_path, stdin):
    assert _run(tmp_path, stdin) == ""
    assert not (tmp_path / rr.FOLDER_NAME).exists()


def test_a_worker_never_runs_it(tmp_path):
    for name in ("a", "b", "c"):
        assert _run(tmp_path, _event(path=f"C:/proj/{name}.py"),
                    environ={"MODELQUAD_WORKER": "1"}) == ""
    assert not (tmp_path / rr.FOLDER_NAME).exists()


def test_old_records_are_removed(tmp_path):
    folder = tmp_path / rr.FOLDER_NAME
    folder.mkdir()
    old = folder / ("0" * 32 + ".json")
    old.write_text("{}", encoding="utf-8")
    import os
    os.utime(old, (0, 0))
    _run(tmp_path, _event())
    assert not old.exists()


def test_the_command_is_wired_and_fails_silent(capsys):
    from modelquad.cli import main
    from modelquad.cli_entry import dispatch_review

    assert dispatch_review(["hook-review", "codex"]) == 0
    assert dispatch_review(["something-else"]) is None
    assert main(["hook-review", "other"]) == 0
    assert capsys.readouterr().out == ""


def test_the_plugin_runs_it_after_edits_and_codex_calls():
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = config["hooks"]["PostToolUse"]
    (hook,) = entry["hooks"]
    assert hook["command"] == "modelquad" and hook["args"] == ["hook-review", "claude"]
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell",
                 "mcp__plugin_modelquad_modelquad__modelquad_delegate"):
        assert re.fullmatch(entry["matcher"], tool)
    assert not re.fullmatch(entry["matcher"], "Read")


# ---------------------------------------------------------------- the gate


def _stop(tmp_path, *, granted=True, active=False, session=SESSION):
    event = {"hook_event_name": "Stop", "session_id": session, "cwd": "C:/proj",
             "transcript_path": "C:/never/opened.jsonl", "stop_hook_active": active}
    out = io.StringIO()
    assert rr.run_hook("claude", io.BytesIO(json.dumps(event).encode()), out, environ={},
                       state=tmp_path, mode=lambda cwd: "decide",
                       standing=lambda cwd: granted) == 0
    return out.getvalue()


def _edits(tmp_path, *names):
    for name in names:
        _run(tmp_path, _event(path=f"C:/proj/{name}"))


def _delegate(tmp_path, response):
    return _run(tmp_path, _event(tool="mcp__plugin_modelquad_modelquad__modelquad_delegate",
                                 tool_input={}, tool_response=response))


def test_the_gate_blocks_once_then_tells_the_user(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    first = json.loads(_stop(tmp_path))
    assert first["decision"] == "block" and "trigger review_gate" in first["reason"]
    assert "Do not run Codex any other way" in first["reason"]
    # Claude stops again without a review: no second block, a message to the user instead.
    second = json.loads(_stop(tmp_path, active=True))
    assert "decision" not in second and "did not review this change" in second["systemMessage"]
    assert _stop(tmp_path) == ""


def test_the_gate_never_blocks_while_claude_continues_for_a_stop_hook(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    out = json.loads(_stop(tmp_path, active=True))
    assert "decision" not in out and "systemMessage" in out


def test_without_a_grant_the_gate_is_silent(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert _stop(tmp_path, granted=False) == ""


def test_a_small_change_passes(tmp_path):
    _edits(tmp_path, "a.py", "b.py")
    assert _stop(tmp_path) == ""


def test_one_sensitive_file_is_enough(tmp_path):
    _edits(tmp_path, "src/auth/login.py")
    out = json.loads(_stop(tmp_path))
    assert out["decision"] == "block" and "sensitive" in out["reason"]


def test_a_review_clears_the_gate(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    _stop(tmp_path)
    _delegate(tmp_path, {"content": [{"type": "text", "text": "FINDINGS: none"}]})
    assert _stop(tmp_path) == ""
    # A new change set gets its own gate.
    _edits(tmp_path, "d.py", "e.py", "f.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"


def test_a_failed_review_is_reported_not_blocked_again(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    _stop(tmp_path)
    _delegate(tmp_path, [{"type": "text", "text": "Codex was not started."}])
    out = json.loads(_stop(tmp_path, active=True))
    assert "decision" not in out and "did not complete" in out["systemMessage"]
    _edits(tmp_path, "d.py")
    assert _stop(tmp_path) == ""


def test_files_outside_the_session_folder_do_not_count(tmp_path):
    for path in ("D:/scratch/a.py", "C:/elsewhere/b.py", "C:/proj2/c.py"):
        _run(tmp_path, _event(path=path))
    _edits(tmp_path, "a.py")
    assert _stop(tmp_path) == ""


def _pre(tmp_path, path, *, mode="decide", granted=False):
    event = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": SESSION,
             "cwd": "C:/proj", "tool_input": {"file_path": path}}
    out = io.StringIO()
    rr.run_hook("claude", io.BytesIO(json.dumps(event).encode()), out, environ={}, state=tmp_path,
                mode=lambda cwd: mode, standing=lambda cwd: granted)
    return out.getvalue()


def test_a_sensitive_file_asks_for_a_consultation_once(tmp_path):
    assert _pre(tmp_path, "C:/proj/readme.md") == ""
    payload = json.loads(_pre(tmp_path, "C:/proj/.github/workflows/ci.yml"))
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "PreToolUse" and "permissionDecision" not in inner
    assert "trigger pre_change" in inner["additionalContext"]
    assert _pre(tmp_path, "C:/proj/src/crypto.py") == ""


def test_no_consultation_reminder_in_offer_mode_without_a_grant(tmp_path):
    assert _pre(tmp_path, "C:/proj/src/auth.py", mode="offer") == ""
    assert _pre(tmp_path, "C:/proj/src/auth.py", mode=None) == ""


def test_the_hook_never_approves_or_denies(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    outputs = [_pre(tmp_path, "C:/proj/install.ps1"), _stop(tmp_path), _stop(tmp_path, active=True),
               _run_tests(tmp_path)]
    for output in outputs:
        assert "permissionDecision" not in output and '"approve"' not in output


def test_fixes_after_a_review_are_not_reported_as_unreviewed(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"
    _delegate(tmp_path, {"content": [{"type": "text", "text": "FINDING F1 | high | a.py:1 | bug | x"}]})
    _edits(tmp_path, "a.py", "b.py", "d.py")  # fixing the findings
    assert _stop(tmp_path, active=True) == ""
    # The next change set starts clean and gets its own gate.
    _edits(tmp_path, "e.py", "f.py", "g.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"
