# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The one global switch: off means no Codex start and silent hooks; unreadable never reads as on."""

from __future__ import annotations

import io
import json

from modelquad import switch
from modelquad.adapters import review_reminder as rr
from modelquad.adapters.server_instructions import preferences_path, read_proactive, write_proactive


def test_on_is_the_default_and_the_switch_round_trips():
    assert switch.enabled() is True and switch.refusal() is None

    switch.write(False)
    assert switch.enabled() is False
    assert switch.refusal()["reason"] == switch.OFF

    switch.write(True)
    assert switch.enabled() is True


def test_the_switch_keeps_the_other_settings():
    write_proactive(False)

    switch.write(False)

    assert read_proactive() is False
    assert json.loads(preferences_path().read_text(encoding="utf-8"))[switch.KEY] is False


def test_an_unreadable_file_is_unknown_and_is_not_overwritten():
    target = preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{broken", encoding="utf-8")

    assert switch.enabled() is None
    assert switch.refusal()["reason"] == switch.UNKNOWN
    try:
        switch.write(True)
    except OSError:
        pass
    else:
        raise AssertionError("a file that cannot be read must not be replaced")
    assert target.read_text(encoding="utf-8") == "{broken"


def test_a_stored_value_that_is_not_a_boolean_does_not_turn_it_off():
    target = preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({switch.KEY: "no"}), encoding="utf-8")

    assert switch.enabled() is True


def _edits(state, count):
    """`count` edits of different files, then a test run: enough for a reminder."""
    out = io.StringIO()
    for index in range(count):
        event = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "s-1",
                 "cwd": "C:/proj", "tool_input": {"file_path": f"C:/proj/file{index}.py"}}
        rr.run_hook("claude", io.BytesIO(json.dumps(event).encode("utf-8")), out, environ={},
                    state=state, clock=lambda: 0.0, mode=lambda cwd: "decide")
    event = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s-1",
             "cwd": "C:/proj", "tool_input": {"command": "python -m pytest -q"}}
    rr.run_hook("claude", io.BytesIO(json.dumps(event).encode("utf-8")), out, environ={},
                state=state, clock=lambda: 0.0, mode=lambda cwd: "decide")
    return out.getvalue()


def test_the_review_hook_is_silent_while_modelquad_is_off(tmp_path):
    switch.write(False)
    assert _edits(tmp_path / "off", 3) == ""

    switch.write(True)
    assert "modelquad_delegate" in _edits(tmp_path / "on", 3)


def test_the_review_hook_is_silent_when_the_switch_cannot_be_read(tmp_path):
    target = preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{broken", encoding="utf-8")

    assert _edits(tmp_path, 3) == ""
