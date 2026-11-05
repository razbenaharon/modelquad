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


def test_it_reminds_once_after_three_different_files(tmp_path):
    assert _run(tmp_path, _event(path="C:/proj/a.py")) == ""
    assert _run(tmp_path, _event(path="C:/proj/a.py")) == ""  # the same file again
    assert _run(tmp_path, _event(tool="Write", path="C:/proj/b.py")) == ""
    text = _context(_run(tmp_path, _event(tool="MultiEdit", path="C:/proj/c.py")))
    assert "changed 3 files" in text and "`modelquad_delegate`" in text
    assert "asks the user first" in text
    assert _run(tmp_path, _event(path="C:/proj/d.py")) == ""


def test_a_codex_call_starts_the_count_again(tmp_path):
    for name in ("a", "b", "c"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"))
    assert _run(tmp_path, _event(tool="mcp__plugin_modelquad_modelquad__modelquad_delegate",
                                 tool_input={})) == ""
    for name in ("d", "e"):
        assert _run(tmp_path, _event(path=f"C:/proj/{name}.py")) == ""
    assert "changed 3 files" in _context(_run(tmp_path, _event(path="C:/proj/f.py")))


def test_in_offer_mode_it_says_offer(tmp_path):
    for name in ("a", "b"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"), mode="offer")
    text = _context(_run(tmp_path, _event(path="C:/proj/c.py"), mode="offer"))
    assert "offer the user a Codex review" in text and "modelquad_delegate" not in text


@pytest.mark.parametrize("mode", [None, "on_request"])
def test_nothing_when_codex_is_off_or_only_on_request(tmp_path, mode):
    for name in ("a", "b", "c", "d"):
        assert _run(tmp_path, _event(path=f"C:/proj/{name}.py"), mode=mode) == ""


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


@pytest.mark.parametrize("stdin", [
    io.BytesIO(b"not json"),
    io.BytesIO(json.dumps({"hook_event_name": "SessionStart"}).encode()),
    io.BytesIO(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "session_id": SESSION, "tool_input": {"command": "ls"}}).encode()),
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
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit",
                 "mcp__plugin_modelquad_modelquad__modelquad_delegate"):
        assert re.fullmatch(entry["matcher"], tool)
    assert not re.fullmatch(entry["matcher"], "Bash")
