# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The Claude Code hook ModelQuad installs reads no conversation and approves nothing.

AGENTS.md invariant 6. One hook is left, ``hook-review``: the review reminder
and its two gates. Claude Code hands every hook the path of the conversation
transcript; the hook never opens it, never stats it, starts no process and
opens no connection. It never approves a tool call. It denies or blocks only
as one of the two gates, in a folder with the user's standing grant, once.

The tests drive the real entry point, the command Claude Code runs, with a
*canary* transcript: a file the hook must not so much as stat. The hook
commands of earlier builds (``hook-gate``, ``hook-pending``, ``usage-hook``)
are no-ops kept for settings files that still name them; they read nothing at
all.
"""

from __future__ import annotations

import functools
import inspect
import io
import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import modelquad
from modelquad import cli_entry, grant, repo_settings
from modelquad.adapters import review_reminder
from modelquad.procutil import no_window_kwargs

# The imported package, not the source tree: CI also runs these tests against a
# non-editable install, where the code that executes lives in site-packages.
PKG = Path(modelquad.__file__).resolve().parent
RAW_SESSION_ID = "RAWSESSIONID-7e3c1f0a-9b2d-4c6e-8a1f-3d5b7c9e0f24"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

# An audit hook cannot be removed once added, so one is installed for the
# whole session and does nothing until a test arms it with a canary path.
_ARMED: dict[str, object] = {"canary": None, "opens": [], "events": []}
_WATCHED_EVENTS = ("subprocess.Popen", "socket.connect", "os.system", "os.startfile", "os.spawn", "os.exec")


def _norm(value) -> str | None:
    if isinstance(value, int):
        return None
    try:
        return os.path.normcase(os.path.abspath(os.fsdecode(value)))
    except (TypeError, ValueError):
        return None


def _audit(event: str, args: tuple) -> None:
    canary = _ARMED["canary"]
    if canary is None:
        return
    if event == "open" and args and _norm(args[0]) == canary:
        _ARMED["opens"].append(args[0])
    elif event in _WATCHED_EVENTS or event.startswith(("os.spawn", "os.exec", "os.posix_spawn")):
        _ARMED["events"].append(event)


sys.addaudithook(_audit)


def _write_canary(tmp_path: Path) -> Path:
    """A transcript an earlier gate would have read and denied on."""

    route_use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "route-canary",
                    "name": "mcp__modelquad__modelquad_route",
                    "input": {"task_type": "implementation"},
                }
            ]
        },
    }
    route_result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "route-canary",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"task_type": "implementation", "recommendation": "codex"}),
                        }
                    ],
                }
            ]
        },
    }
    canary = tmp_path / "canary-session.jsonl"
    canary.write_text(json.dumps(route_use) + "\n" + json.dumps(route_result) + "\n", encoding="utf-8")
    return canary


class _Touched(Exception):
    """Raised on any stat of the canary. Not an OSError, so no fail-open
    ``except OSError`` in the code under test can swallow it."""


@contextmanager
def _canary_armed(monkeypatch, canary: Path):
    target = _norm(canary)
    touched: list[str] = []

    def guard(name, real):
        def guarded(path, *args, **kwargs):
            if _norm(path) == target:
                touched.append(name)
                raise _Touched(f"{name} on the canary transcript")
            return real(path, *args, **kwargs)

        return guarded

    monkeypatch.setattr(os, "stat", guard("os.stat", os.stat))
    monkeypatch.setattr(os, "lstat", guard("os.lstat", os.lstat))
    for name in ("exists", "lexists", "isfile", "isdir", "getsize", "getmtime"):
        monkeypatch.setattr(os.path, name, guard(f"os.path.{name}", getattr(os.path, name)))
    _ARMED.update(canary=target, opens=[], events=[])
    try:
        yield touched
    finally:
        opened = list(_ARMED["opens"])
        _ARMED.update(canary=None, opens=[], events=[])
        assert opened == [], f"the canary transcript was opened: {opened}"
        assert touched == [], f"the canary transcript was stat'ed: {touched}"


# The code that starts Codex or serves MCP: a hook runs none of it.
_FORBIDDEN_FILES = tuple(
    os.path.normcase(str(p))
    for p in (PKG / "execution" / "codex.py", PKG / "execution" / "lifecycle.py",
              PKG / "execution" / "codex_worker_mcp.py", PKG / "codexcli.py", PKG / "review.py")
)
_FORBIDDEN_DIRS = (os.path.normcase(str(PKG / "mcp")) + os.sep,)


@functools.lru_cache(maxsize=None)
def _real(filename: str) -> str:
    # Resolved like PKG, so a linked venv or site-packages still matches.
    return os.path.normcase(os.path.realpath(filename))


@contextmanager
def _local_and_quiet(monkeypatch):
    """No subprocess, no socket, none of the code that starts Codex or serves MCP."""

    def refuse(*args, **kwargs):
        raise AssertionError("the hook must not start a subprocess or open a connection")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    called: list[str] = []

    def profile(frame, event, arg):
        if event != "call":
            return
        filename = _real(frame.f_code.co_filename)
        if filename in _FORBIDDEN_FILES or filename.startswith(_FORBIDDEN_DIRS):
            called.append(f"{filename}:{frame.f_code.co_name}")

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)
    events = list(_ARMED["events"])
    assert called == [], f"Codex or MCP code ran inside the hook: {called}"
    assert events == [], f"the hook started a process or connected: {events}"


class _Stdin:
    """What Claude Code gives a hook: bytes on ``sys.stdin.buffer``."""

    def __init__(self, event: dict):
        self.buffer = io.BytesIO(json.dumps(event).encode("utf-8"))

    def read(self, *args):
        raise AssertionError("the hook read text, not bytes")


def _review_hook(monkeypatch, capsys, canary: Path, event: dict) -> str:
    """Run ``modelquad hook-review claude`` on one event that names the canary."""

    event = dict(event, session_id=RAW_SESSION_ID, transcript_path=str(canary))
    monkeypatch.setattr("sys.stdin", _Stdin(event))
    # The hook gives itself 1.5 s; the guards here slow it down, and these
    # tests check what it touches, not how fast a loaded machine is.
    monkeypatch.setattr(review_reminder, "HOOK_BUDGET", 600.0)
    capsys.readouterr()
    with _canary_armed(monkeypatch, canary), _local_and_quiet(monkeypatch):
        code = cli_entry.main(["hook-review", "claude"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert RAW_SESSION_ID not in captured.out and "canary-session" not in captured.out
    # No hook ever approves a tool call, whatever else it says.
    assert '"allow"' not in captured.out and '"approve"' not in captured.out
    return captured.out


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _edit(repo: Path, name: str, hook_event: str = "PostToolUse", tool: str = "Edit") -> dict:
    return {"hook_event_name": hook_event, "tool_name": tool, "cwd": str(repo),
            "tool_input": {"file_path": str(repo / name)}}


def _command(repo: Path) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "python -m pytest -q"}}


def _stop(repo: Path, *, continuing: bool = False) -> dict:
    return {"hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": continuing}


def _stored_text() -> str:
    from modelquad.paths import state_root

    folder = state_root() / review_reminder.FOLDER_NAME
    return "".join(path.name + path.read_text(encoding="utf-8") for path in sorted(folder.iterdir()))


# ---------------------------------------------------------------------------
# The review hook: every event it acts on, each carrying the transcript path
# ---------------------------------------------------------------------------


def test_the_review_reminder_never_opens_the_transcript(tmp_path, capsys, monkeypatch):
    repo, canary = _repo(tmp_path), _write_canary(tmp_path)
    for name, tool in (("a.py", "Edit"), ("b.py", "Write"), ("c.py", "MultiEdit")):
        assert _review_hook(monkeypatch, capsys, canary, _edit(repo, name, tool=tool)) == ""
    out = json.loads(_review_hook(monkeypatch, capsys, canary, _command(repo)))
    # Advisory context only: no decision of any kind.
    assert set(out) == {"hookSpecificOutput"}
    assert set(out["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
    assert "changed 3 files" in out["hookSpecificOutput"]["additionalContext"]
    stored = _stored_text()
    assert RAW_SESSION_ID not in stored and "canary-session" not in stored


@pytest.mark.parametrize("choice", ["on", "off", "offer-only"])
def test_the_reminder_reads_the_folders_settings_not_the_transcript(tmp_path, capsys, monkeypatch, choice):
    """It reads the user's per-repository setting and ``.modelquad.toml``, and decides nothing."""
    repo, canary = _repo(tmp_path), _write_canary(tmp_path)
    (repo / ".modelquad.toml").write_text('schema = 1\n[offers]\ncodex = "default"\n', encoding="utf-8")
    repo_settings.write_mode(repo, choice)
    for name in ("a.py", "b.py", "c.py"):
        assert _review_hook(monkeypatch, capsys, canary, _edit(repo, name)) == ""
    out = _review_hook(monkeypatch, capsys, canary, _command(repo))
    assert "permissionDecision" not in out and "decision" not in out
    if choice == "off":
        assert out == ""
    else:
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert ("offer the user a Codex review" in text) is (choice == "offer-only")


def test_without_a_grant_a_sensitive_edit_gets_advice_and_no_decision(tmp_path, capsys, monkeypatch):
    repo, canary = _repo(tmp_path), _write_canary(tmp_path)
    out = _review_hook(monkeypatch, capsys, canary, _edit(repo, "auth/login.py", "PreToolUse"))
    inner = json.loads(out)["hookSpecificOutput"]
    assert set(inner) == {"hookEventName", "additionalContext"}
    assert "permissionDecision" not in out
    # Nor does the end of the turn block anything without the grant.
    for name in ("a.py", "b.py", "auth/login.py"):
        _review_hook(monkeypatch, capsys, canary, _edit(repo, name))
    assert _review_hook(monkeypatch, capsys, canary, _stop(repo)) == ""


def test_the_two_gates_never_open_the_transcript_and_act_once(tmp_path, capsys, monkeypatch):
    repo, canary = _repo(tmp_path), _write_canary(tmp_path)
    grant.write(True, directory=repo)
    sensitive = _edit(repo, "auth/login.py", "PreToolUse")

    out = json.loads(_review_hook(monkeypatch, capsys, canary, sensitive))
    inner = out["hookSpecificOutput"]
    assert set(out) == {"hookSpecificOutput"}
    assert set(inner) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
    assert inner["permissionDecision"] == "deny" and "trigger pre_change" in inner["permissionDecisionReason"]
    # Once per change set: the same edit again is not stopped.
    assert _review_hook(monkeypatch, capsys, canary, sensitive) == ""

    for name in ("auth/login.py", "b.py", "c.py"):
        assert _review_hook(monkeypatch, capsys, canary, _edit(repo, name)) == ""
    first = json.loads(_review_hook(monkeypatch, capsys, canary, _stop(repo)))
    assert set(first) == {"decision", "reason"} and first["decision"] == "block"
    # Once: the next Stop tells the user and blocks nothing.
    second = json.loads(_review_hook(monkeypatch, capsys, canary, _stop(repo)))
    assert set(second) == {"systemMessage"}
    assert _review_hook(monkeypatch, capsys, canary, _stop(repo)) == ""
    stored = _stored_text()
    assert RAW_SESSION_ID not in stored and "canary-session" not in stored


def test_the_review_gate_never_blocks_while_claude_continues_for_a_stop_hook(tmp_path, capsys, monkeypatch):
    repo, canary = _repo(tmp_path), _write_canary(tmp_path)
    grant.write(True, directory=repo)
    for name in ("a.py", "b.py", "c.py"):
        _review_hook(monkeypatch, capsys, canary, _edit(repo, name))
    out = json.loads(_review_hook(monkeypatch, capsys, canary, _stop(repo, continuing=True)))
    assert set(out) == {"systemMessage"}


def test_a_codex_reply_is_read_from_the_event_not_the_transcript(tmp_path, capsys, monkeypatch):
    repo, canary = _repo(tmp_path), _write_canary(tmp_path)
    grant.write(True, directory=repo)
    for name in ("a.py", "b.py", "c.py"):
        _review_hook(monkeypatch, capsys, canary, _edit(repo, name))
    reply = {"hook_event_name": "PostToolUse", "cwd": str(repo),
             "tool_name": "mcp__plugin_modelquad_modelquad__modelquad_delegate",
             "tool_input": {"task_type": "review", "trigger": "review_gate"},
             "tool_response": [{"type": "text", "text": "ModelQuad: Codex answered.\nNo findings."}]}
    assert _review_hook(monkeypatch, capsys, canary, reply) == ""
    # The review was done, so the gate has nothing to ask at the end of the turn.
    assert _review_hook(monkeypatch, capsys, canary, _stop(repo)) == ""


def test_while_modelquad_is_off_the_hook_says_nothing(tmp_path, capsys, monkeypatch):
    from modelquad import switch

    repo, canary = _repo(tmp_path), _write_canary(tmp_path)
    grant.write(True, directory=repo)
    switch.write(False)
    assert _review_hook(monkeypatch, capsys, canary, _edit(repo, "auth/login.py", "PreToolUse")) == ""
    for name in ("a.py", "b.py", "c.py"):
        assert _review_hook(monkeypatch, capsys, canary, _edit(repo, name)) == ""
    assert _review_hook(monkeypatch, capsys, canary, _command(repo)) == ""
    assert _review_hook(monkeypatch, capsys, canary, _stop(repo)) == ""


def test_the_hook_source_never_names_the_transcript_key():
    """The path arrives under ``transcript_path``; code that never reads the key cannot open it."""
    assert "transcript_path" not in inspect.getsource(review_reminder)
    assert "transcript_path" not in inspect.getsource(cli_entry)


def test_the_review_hook_is_the_only_hook_command():
    from modelquad.cli import build_parser

    commands = build_parser()._subparsers._group_actions[0].choices
    assert sorted(name for name in commands if "hook" in name) == ["hook-review"]


# ---------------------------------------------------------------------------
# Hook commands of earlier builds: no-ops that read nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["hook-gate"], ["hook-pending", "claude"], ["usage-hook", "claude"],
    ["usage-hook", "claude", "--quiet"], ["--usage-directory", "usage", "usage-hook", "claude"],
])
def test_retired_hook_commands_read_nothing_print_nothing_and_decide_nothing(
        tmp_path, capsys, monkeypatch, argv):
    """A settings file from an earlier build may still run one. It must exit 0
    without reading its input, opening the transcript, starting a process or printing."""

    class Unread:
        def read(self, *args):
            raise AssertionError("a retired hook read its input")

        @property
        def buffer(self):
            raise AssertionError("a retired hook read its input")

    canary = _write_canary(tmp_path)
    monkeypatch.setattr("sys.stdin", Unread())
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    with _canary_armed(monkeypatch, canary), _local_and_quiet(monkeypatch):
        code = cli_entry.main(argv)
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (0, "", "")
    assert sorted(tmp_path.rglob("*")) == before


# ---------------------------------------------------------------------------
# The guards themselves: a guard that cannot fire proves nothing
# ---------------------------------------------------------------------------


def test_canary_guard_catches_an_open(tmp_path, monkeypatch):
    canary = _write_canary(tmp_path)
    with pytest.raises(AssertionError, match="was opened"):
        with _canary_armed(monkeypatch, canary):
            canary.read_text(encoding="utf-8")


@pytest.mark.parametrize("probe", [lambda p: os.stat(p), lambda p: Path(p).exists(), lambda p: Path(p).is_file()])
def test_canary_guard_catches_a_stat(tmp_path, monkeypatch, probe):
    canary = _write_canary(tmp_path)
    with pytest.raises((_Touched, AssertionError)):
        with _canary_armed(monkeypatch, canary):
            probe(canary)


def test_quiet_guard_catches_a_subprocess(tmp_path, monkeypatch):
    canary = _write_canary(tmp_path)
    with pytest.raises(AssertionError):
        with _canary_armed(monkeypatch, canary), _local_and_quiet(monkeypatch):
            subprocess.run([sys.executable, "-c", "pass"], check=False, **no_window_kwargs())


def test_quiet_guard_names_the_code_that_is_imported():
    # The guard matches frames by file, so its paths must be the files that
    # actually run -- in site-packages under a non-editable install.
    from modelquad import codexcli, review
    from modelquad.execution import codex, codex_worker_mcp, lifecycle
    from modelquad.mcp import server, worker_guard

    for module in (codex, lifecycle, codex_worker_mcp, codexcli, review):
        assert _real(module.__file__) in _FORBIDDEN_FILES, module.__file__
    for module in (server, worker_guard):
        assert _real(module.__file__).startswith(_FORBIDDEN_DIRS), module.__file__


def test_quiet_guard_catches_codex_code(tmp_path, monkeypatch):
    from modelquad import codexcli

    canary = _write_canary(tmp_path)
    with pytest.raises(AssertionError, match="Codex or MCP code ran"):
        with _canary_armed(monkeypatch, canary), _local_and_quiet(monkeypatch):
            codexcli.clear_cache()
