# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The hook timing record keeps numbers and fixed words only, and never breaks the hook."""

from __future__ import annotations

import io
import json

import pytest

from modelquad import switch
from modelquad.adapters import hook_timing, review_reminder

ALLOWED_START = {"at", "id", "phase", "python_ms", "launcher_ms"}
ALLOWED_END = {"id", "phase", "event", "tool", "work_ms"}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(switch, "enabled", lambda *a, **k: True)


def _lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / hook_timing.FILE_NAME).read_text("utf-8").splitlines()]


def test_a_call_leaves_a_start_and_an_end_line_with_numbers_only(tmp_path, on):
    mark = hook_timing.started(state=tmp_path, now=lambda: 1_000.0, created=lambda pid: 999.5)
    hook_timing.finished(mark, {"event": "PostToolUse", "tool": "Edit"})
    start, end = _lines(tmp_path)
    assert set(start) == ALLOWED_START and set(end) == ALLOWED_END
    assert start["phase"] == "start" and end["phase"] == "end" and start["id"] == end["id"]
    assert start["python_ms"] == 500 and start["launcher_ms"] == 500
    assert end["event"] == "PostToolUse" and end["tool"] == "edit"
    assert isinstance(end["work_ms"], int) and end["work_ms"] >= 0


def test_names_outside_the_fixed_sets_are_not_kept(tmp_path, on):
    mark = hook_timing.started(state=tmp_path, created=lambda pid: None)
    hook_timing.finished(mark, {"event": "C:/Users/someone/secret", "tool": "mcp__x__read_private_notes"})
    start, end = _lines(tmp_path)
    assert start["python_ms"] is None and start["launcher_ms"] is None  # unknown stays unknown
    assert end["event"] is None and end["tool"] == "other"
    text = (tmp_path / hook_timing.FILE_NAME).read_text("utf-8")
    assert "secret" not in text and "private" not in text


def test_the_delegate_tool_and_a_run_are_named_by_kind(tmp_path, on):
    for tool, kind in (("mcp__plugin_modelquad_review__delegate", "modelquad"),
                       ("mcp__modelquad__delegate", "modelquad"),
                       ("mcp__plugin_modelquad_modelquad__modelquad_delegate", "modelquad"),
                       ("mcp__plugin_modelquad_modelquad__delegate", "modelquad"),
                       ("mcp__plugin_modelquad_review__modelquad_delegate", "modelquad"),
                       ("mcp__other__delegate", "other"),
                       ("PowerShell", "run")):
        hook_timing.finished(hook_timing.started(state=tmp_path), {"event": "PostToolUse", "tool": tool})
        assert _lines(tmp_path)[-1]["tool"] == kind


def test_an_old_parent_is_not_reported_as_the_launcher(tmp_path, on):
    def created(pid):
        return 0.0 if pid != hook_timing.os.getpid() else 999.9
    hook_timing.started(state=tmp_path, now=lambda: 10_000.0, created=created)
    (start,) = _lines(tmp_path)
    assert start["launcher_ms"] is None and start["python_ms"] == round((10_000.0 - 999.9) * 1000)


def test_nothing_is_kept_while_modelquad_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(switch, "enabled", lambda *a, **k: False)
    assert hook_timing.started(state=tmp_path) is None
    hook_timing.finished(None, {"event": "Stop"})
    assert not (tmp_path / hook_timing.FILE_NAME).exists()


def test_the_file_is_cut_back_when_it_grows(tmp_path, on, monkeypatch):
    monkeypatch.setattr(hook_timing, "MAX_BYTES", 2_000)
    for _ in range(100):
        hook_timing.finished(hook_timing.started(state=tmp_path), {"event": "Stop", "tool": None})
    path = tmp_path / hook_timing.FILE_NAME
    assert path.stat().st_size <= 2_000 + 400
    _lines(tmp_path)  # every kept line is whole


def test_a_record_that_cannot_be_written_never_breaks_the_hook(tmp_path, on):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    assert hook_timing.started(state=blocker) is None
    hook_timing.finished({"path": blocker / "x" / "y", "id": "1", "clock": 0.0}, {"event": "Stop"})


def test_run_hook_hands_the_event_and_tool_to_the_record(tmp_path, on):
    facts: dict = {}
    event = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
             "cwd": str(tmp_path), "tool_input": {"command": "dir"}}
    stdin = io.BytesIO(json.dumps(event).encode("utf-8"))
    review_reminder.run_hook("claude", stdin, io.StringIO(), state=tmp_path, facts=facts)
    assert facts == {"event": "PostToolUse", "tool": "Bash"}
