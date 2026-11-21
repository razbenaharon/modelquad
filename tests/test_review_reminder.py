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


def _still():
    """A clock that does not move: these tests check what the hook decides, not how
    fast a loaded CI runner is. The 1.5 s budget would otherwise silence the hook
    when the runner is slow (seen on a hosted 3.11 runner)."""
    return 0.0


def _run(tmp_path, stdin, *, mode="decide", environ=None):
    out = io.StringIO()
    assert rr.run_hook("claude", stdin, out, environ=environ or {}, state=tmp_path, clock=_still,
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
    assert "`mcp__plugin_modelquad_review__delegate`" in text and "trigger review_gate" in text
    assert "`mcp__modelquad__delegate`" in text
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
    assert _run(tmp_path, _event(tool="mcp__plugin_modelquad_review__delegate",
                                 tool_input={"task_type": "review"})) == ""
    for name in ("d", "e"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"))
    assert _run_tests(tmp_path) == ""
    _run(tmp_path, _event(path="C:/proj/f.py"))
    assert "changed 3 files" in _context(_run_tests(tmp_path))


def test_in_offer_mode_it_says_offer(tmp_path):
    for name in ("a", "b", "c"):
        _run(tmp_path, _event(path=f"C:/proj/{name}.py"), mode="offer")
    text = _context(_run_tests(tmp_path, mode="offer"))
    assert "offer the user a Codex review" in text and "delegate" not in text


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
                 "mcp__plugin_modelquad_review__delegate"):
        assert re.fullmatch(entry["matcher"], tool)
    assert not re.fullmatch(entry["matcher"], "Read")


# ---------------------------------------------------------------- the gate


def _stop(tmp_path, *, granted=True, active=False, session=SESSION):
    event = {"hook_event_name": "Stop", "session_id": session, "cwd": "C:/proj",
             "transcript_path": "C:/never/opened.jsonl", "stop_hook_active": active}
    out = io.StringIO()
    assert rr.run_hook("claude", io.BytesIO(json.dumps(event).encode()), out, environ={}, clock=_still,
                       state=tmp_path, mode=lambda cwd: "decide",
                       standing=lambda cwd: granted) == 0
    return out.getvalue()


def _edits(tmp_path, *names):
    for name in names:
        _run(tmp_path, _event(path=f"C:/proj/{name}"))


def _delegate(tmp_path, response, tool="mcp__plugin_modelquad_review__delegate"):
    return _run(tmp_path, _event(tool=tool,
                                 tool_input={"task_type": "review", "trigger": "review_gate"},
                                 tool_response=response))


def test_the_gate_blocks_once_then_tells_the_user(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    first = json.loads(_stop(tmp_path))
    assert first["decision"] == "block" and "trigger review_gate" in first["reason"]
    assert "Do not run Codex any other way" in first["reason"]
    # Claude stops again without a review: no second block, a message to the user instead.
    second = json.loads(_stop(tmp_path, active=True))
    assert "decision" not in second and "did not review this change" in second["systemMessage"]
    assert _stop(tmp_path) == ""


def test_a_further_edit_to_a_reported_change_does_not_block_again(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"
    assert "systemMessage" in json.loads(_stop(tmp_path, active=True))
    # Follow-up tweaks to files of the reported change: no second block, no second message.
    _edits(tmp_path, "a.py")
    assert _stop(tmp_path) == ""
    _edits(tmp_path, "b.py", "c.py")
    assert _stop(tmp_path) == ""
    assert _pre(tmp_path, "C:/proj/a.py", granted=True) == ""


def test_a_new_file_after_a_reported_change_starts_a_new_change_set(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    _stop(tmp_path)
    _stop(tmp_path, active=True)
    _edits(tmp_path, "d.py")
    assert _stop(tmp_path) == ""  # one file of a new change set
    _edits(tmp_path, "e.py", "f.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"
    assert "(3 file(s))" in json.loads(_stop(tmp_path, active=True))["systemMessage"]
    _edits(tmp_path, "d.py")
    assert _stop(tmp_path) == ""


def test_a_new_change_after_a_reported_one_gets_its_own_consultation(tmp_path):
    _edits(tmp_path, "a.py")
    _denied(_pre(tmp_path, "C:/proj/src/auth.py", granted=True))
    _edits(tmp_path, "src/auth.py")
    _stop(tmp_path)
    _stop(tmp_path, active=True)
    # A sensitive file of the reported change: not stopped again.
    assert _pre(tmp_path, "C:/proj/src/auth.py", granted=True) == ""
    # A sensitive file outside it starts a new change, stopped once.
    _denied(_pre(tmp_path, "C:/proj/src/token_store.py", granted=True))
    assert _pre(tmp_path, "C:/proj/src/token_store.py", granted=True) == ""


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


@pytest.mark.parametrize("tool", sorted(
    f"mcp__{server}__{name}"
    for server in ("plugin_modelquad_review", "plugin_modelquad_modelquad", "modelquad")
    for name in ("delegate", "modelquad_delegate")))
def test_a_review_by_any_live_id_clears_the_gate(tmp_path, tool):
    # After an upgrade the package and the cached plugin can differ in version.
    _edits(tmp_path, "a.py", "b.py", "c.py")
    _stop(tmp_path)
    _delegate(tmp_path, {"content": [{"type": "text", "text": "FINDINGS: none"}]}, tool=tool)
    assert _stop(tmp_path) == ""


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
    rr.run_hook("claude", io.BytesIO(json.dumps(event).encode()), out, environ={}, state=tmp_path, clock=_still,
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


# ------------------------------------------------------- consultation gate


def _consult(tmp_path, failed=False):
    text = "Codex was not started." if failed else "Use a Decimal for money."
    return _run(tmp_path, _event(tool="mcp__plugin_modelquad_review__delegate",
                                 tool_input={"task_type": "architecture", "trigger": "pre_change"},
                                 tool_response=[{"type": "text", "text": text}]))


def _denied(output):
    inner = json.loads(output)["hookSpecificOutput"]
    assert inner["hookEventName"] == "PreToolUse" and inner["permissionDecision"] == "deny"
    assert "trigger pre_change" in inner["permissionDecisionReason"]
    return inner["permissionDecisionReason"]


def test_a_sensitive_edit_is_stopped_once_until_codex_is_consulted(tmp_path):
    assert "sensitive file" in _denied(_pre(tmp_path, "C:/proj/src/auth.py", granted=True))
    # Stopped once per change: the retry goes ahead even without a consultation.
    assert _pre(tmp_path, "C:/proj/src/auth.py", granted=True) == ""


def test_the_edit_that_makes_three_files_is_stopped_once(tmp_path):
    _edits(tmp_path, "a.py", "b.py")
    assert _pre(tmp_path, "C:/proj/a.py", granted=True) == ""  # a file already in the change
    assert "3 files" in _denied(_pre(tmp_path, "C:/proj/c.py", granted=True))
    assert _pre(tmp_path, "C:/proj/c.py", granted=True) == ""


def test_a_consultation_before_the_edit_passes_the_gate(tmp_path):
    _consult(tmp_path)
    assert _pre(tmp_path, "C:/proj/src/auth.py", granted=True) == ""
    _edits(tmp_path, "a.py", "b.py")
    assert _pre(tmp_path, "C:/proj/c.py", granted=True) == ""


def test_a_failed_consultation_does_not_count_and_does_not_stop_twice(tmp_path):
    _denied(_pre(tmp_path, "C:/proj/src/auth.py", granted=True))
    _consult(tmp_path, failed=True)
    assert _pre(tmp_path, "C:/proj/src/auth.py", granted=True) == ""


def test_without_a_grant_the_consultation_is_only_a_reminder(tmp_path):
    out = _pre(tmp_path, "C:/proj/src/auth.py", granted=False)
    assert "permissionDecision" not in out and "additionalContext" in out
    _edits(tmp_path, "a.py", "b.py")
    assert _pre(tmp_path, "C:/proj/c.py", granted=False) == ""


def test_a_new_change_after_a_reviewed_one_gets_its_own_consultation(tmp_path):
    _consult(tmp_path)
    _edits(tmp_path, "a.py", "b.py", "c.py")
    _delegate(tmp_path, {"content": [{"type": "text", "text": "FINDINGS: none"}]})
    assert _stop(tmp_path) == ""
    _edits(tmp_path, "d.py", "e.py")
    _denied(_pre(tmp_path, "C:/proj/f.py", granted=True))


def test_the_gate_never_allows_a_tool_call(tmp_path):
    _edits(tmp_path, "a.py", "b.py")
    for path in ("C:/proj/c.py", "C:/proj/install.ps1", "C:/proj/readme.md"):
        assert '"allow"' not in _pre(tmp_path, path, granted=True)


def test_an_answer_quoting_a_refusal_marker_is_not_a_failed_review():
    answer = "Codex answered\nFINDINGS: the code prints 'Codex was not started' and \"refused\": true."
    assert rr._review_failed([{"type": "text", "text": answer}]) is False
    assert rr._review_failed({"content": [{"type": "text", "text": answer}]}) is False
    assert rr._review_failed({"content": [{"type": "text", "text": answer}],
                              "structuredContent": {"refused": False, "output": answer}}) is False
    # Real refusals still are failed: by the data, or by the outcome line.
    assert rr._review_failed({"structuredContent": {"refused": True}}) is True
    assert rr._review_failed({"structured_content": {"reason": "no_standing_grant"}}) is True
    assert rr._review_failed([{"type": "text", "text": "Codex was not started.\nwhy"},
                              {"type": "text", "text": '{"refused": true}'}]) is True
    assert rr._review_failed([{"type": "text", "text": "Codex answer withheld"}]) is True
    assert rr._review_failed([{"type": "text", "text": '{"refused": true, "started": false}'}]) is True


def test_the_per_session_file_name_is_keyed_to_the_installation(tmp_path):
    import hashlib

    _run(tmp_path, _event(path="C:/proj/a.py"))
    (record,) = (tmp_path / rr.FOLDER_NAME).iterdir()
    assert record.stem != hashlib.sha256(SESSION.encode("utf-8")).hexdigest()[:32]
    from modelquad.review_ledger import session_pseudonym

    assert record.stem == session_pseudonym(SESSION, tmp_path, length=32)


# ------------------------------------------ only a delivered answer is a review done

NOTICE = {"status": "notice_first", "codex_started": False, "notice": ["what goes where"]}
NOTICE_REPLY = {"content": [{"type": "text", "text": "Codex was not started. Before the first "
                                                     "Codex call, ModelQuad shows what it sends where:"},
                            {"type": "text", "text": json.dumps(NOTICE)}], "structuredContent": NOTICE}
ANSWER_DATA = {"task_type": "review", "output": "FINDINGS: none", "decision_id": "d-1"}


def _state(tmp_path, session=SESSION):
    from modelquad.review_ledger import session_pseudonym

    record = tmp_path / rr.FOLDER_NAME / f"{session_pseudonym(session, tmp_path, length=32)}.json"
    return json.loads(record.read_text(encoding="utf-8"))


def _gate_counts():
    from modelquad import review_ledger

    return review_ledger.report()["gate"]


@pytest.mark.parametrize("response, state", [
    (NOTICE_REPLY, rr.NOT_ANSWERED),
    (NOTICE_REPLY["content"], rr.NOT_ANSWERED),
    # A run that delivered nothing: failed, by the data.
    ({"structuredContent": {"output": "", "task_status": "failed"}}, rr.NOT_ANSWERED),
    ([{"type": "text", "text": "ModelQuad: Codex did not finish this task."},
      {"type": "text", "text": json.dumps({"task_type": "review", "output": ""})}], rr.NOT_ANSWERED),
    ([{"type": "text", "text": "ModelQuad: Codex was not started.\nwhy"}], rr.NOT_ANSWERED),
    ([{"type": "text", "text": "ModelQuad: Codex did not return in time."}], rr.NOT_ANSWERED),
    # Codex's answer was delivered.
    ({"structuredContent": ANSWER_DATA}, rr.ANSWERED),
    ([{"type": "text", "text": "ModelQuad: Codex finished this task."},
      {"type": "text", "text": json.dumps(ANSWER_DATA)}], rr.ANSWERED),
])
def test_what_a_delegate_reply_delivered(response, state):
    assert rr._reply_state(response) == state


def test_the_first_use_notice_is_not_a_review_done(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"
    _delegate(tmp_path, NOTICE_REPLY)
    out = json.loads(_stop(tmp_path, active=True))
    assert "decision" not in out and "did not complete" in out["systemMessage"]
    assert _stop(tmp_path) == ""  # reported once, never stopped again


def test_a_run_without_an_answer_is_not_a_review_done(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    _delegate(tmp_path, {"structuredContent": {"output": "", "task_status": "failed"}})
    out = json.loads(_stop(tmp_path))
    assert "decision" not in out and "did not complete" in out["systemMessage"]


# ------------------------------------------------- the gate says only what happened


def test_a_stop_first_seen_while_continuing_does_not_claim_the_gate_asked(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    out = json.loads(_stop(tmp_path, active=True))
    assert "decision" not in out
    assert out["systemMessage"] == "ModelQuad: Codex did not review this change (3 file(s))."
    assert "asked" not in out["systemMessage"]
    assert _stop(tmp_path) == ""
    assert _gate_counts().get("blocked", 0) == 0


def test_after_the_gate_asked_the_message_says_so(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"
    out = json.loads(_stop(tmp_path, active=True))
    assert "The review gate asked for one and none was done." in out["systemMessage"]


# ----------------------------------- a gate is used only once its message was written


def _late():
    """A clock that is inside the budget while the event is read and past it at the write."""
    ticks = iter([0.0, 0.0])
    return lambda: next(ticks, 10.0)


def _hook(tmp_path, event, clock):
    out = io.StringIO()
    assert rr.run_hook("claude", io.BytesIO(json.dumps(event).encode()), out, environ={}, clock=clock,
                       state=tmp_path, mode=lambda cwd: "decide", standing=lambda cwd: True) == 0
    return out.getvalue()


STOP = {"hook_event_name": "Stop", "session_id": SESSION, "cwd": "C:/proj", "stop_hook_active": False}


def test_a_review_gate_that_ran_out_of_time_is_not_used_up(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert _hook(tmp_path, STOP, _late()) == ""
    assert _state(tmp_path)["gate"] == "none"
    assert _gate_counts().get("blocked", 0) == 0
    # The next Stop asks, as the first one meant to: no false "asked for one" line.
    out = json.loads(_stop(tmp_path))
    assert out["decision"] == "block"
    assert _gate_counts()["blocked"] == 1
    # And still only once.
    assert "decision" not in json.loads(_stop(tmp_path, active=True))
    assert _stop(tmp_path) == ""


def test_a_report_that_ran_out_of_time_is_made_at_the_next_stop(tmp_path):
    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert json.loads(_stop(tmp_path))["decision"] == "block"
    assert _hook(tmp_path, dict(STOP, stop_hook_active=True), _late()) == ""
    assert _state(tmp_path)["gate"] == "blocked"
    assert "none was done" in json.loads(_stop(tmp_path, active=True))["systemMessage"]


def test_a_consultation_gate_that_ran_out_of_time_is_not_used_up(tmp_path):
    event = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": SESSION,
             "cwd": "C:/proj", "tool_input": {"file_path": "C:/proj/src/auth.py"}}
    assert _hook(tmp_path, event, _late()) == ""
    assert _state(tmp_path)["consult_gate"] is False
    assert _gate_counts().get("consult_blocked", 0) == 0
    _denied(_pre(tmp_path, "C:/proj/src/auth.py", granted=True))
    assert _gate_counts()["consult_blocked"] == 1
    # At most once: the same edit again passes.
    assert _pre(tmp_path, "C:/proj/src/auth.py", granted=True) == ""


def test_a_gate_whose_message_could_not_be_written_is_not_used_up(tmp_path):
    class Closed(io.StringIO):
        def write(self, text):
            raise OSError("closed")

    _edits(tmp_path, "a.py", "b.py", "c.py")
    assert rr.run_hook("claude", io.BytesIO(json.dumps(STOP).encode()), Closed(), environ={},
                       clock=_still, state=tmp_path, standing=lambda cwd: True) == 0
    assert _state(tmp_path)["gate"] == "none"
    assert json.loads(_stop(tmp_path))["decision"] == "block"
