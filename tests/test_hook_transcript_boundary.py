# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The Claude Code hook ModelQuad installs is advisory and reads no conversation.

The hook never opens Claude Code's conversation transcript, and it is
advisory. A ModelQuad recommendation never stops the Host, so the hook never
denies -- and it never approves either, because a PreToolUse ``allow`` skips
the permission prompt the user would otherwise see.

Every test here drives the real ``hook-gate`` CLI entry point, the command
Claude Code runs, with a *canary* transcript: a file holding a route call that
recommends another provider. The pre-advisory gate read that file and denied
the call; the advisory gate must not so much as stat it.
"""

from __future__ import annotations

import ast
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
import modelquad.cli as cli_module
from modelquad.adapters import gate as gate_module
from modelquad.cli import main
from modelquad.execution.base import WORKER_ENV_VAR
from modelquad.procutil import no_window_kwargs
from modelquad.service import history as service_history

# The imported package, not the source tree: CI also runs these tests against a
# non-editable install, where the code that executes lives in site-packages.
PKG = Path(modelquad.__file__).resolve().parent
# Looked up rather than imported so the regression test below runs -- and
# fails on its assertions -- against the pre-advisory gate, which has no note.
ADVISORY_NOTE = getattr(gate_module, "ADVISORY_NOTE", None)
RAW_SESSION_ID = "RAWSESSIONID-7e3c1f0a-9b2d-4c6e-8a1f-3d5b7c9e0f24"
ADVISORY_OUTPUT = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ADVISORY_NOTE}}
DECISION_KEYS = (
    "permissionDecision",
    "permissionDecisionReason",
    "updatedInput",
    "decision",
    "continue",
    "systemMessage",
)


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
    """A transcript that the old gate would have read and denied on."""

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


_FORBIDDEN_FILES = tuple(
    os.path.normcase(str(p))
    for p in (PKG / "usage" / "refresh.py", PKG / "usage" / "codex_appserver.py")
)
_FORBIDDEN_DIRS = (os.path.normcase(str(PKG / "providers")) + os.sep,)


@functools.lru_cache(maxsize=None)
def _real(filename: str) -> str:
    # Resolved like PKG, so a linked venv or site-packages still matches.
    return os.path.normcase(os.path.realpath(filename))


@contextmanager
def _local_and_quiet(monkeypatch):
    """No subprocess, no socket, no provider or usage-collection code."""

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
    assert called == [], f"provider or usage code ran inside the hook: {called}"
    assert events == [], f"the hook started a process or connected: {events}"


def _run_hook_gate(monkeypatch, capsys, stdin_text: str, canary: Path) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    with _canary_armed(monkeypatch, canary), _local_and_quiet(monkeypatch):
        code = main(["hook-gate"])
    return code, capsys.readouterr().out


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setattr(cli_module, "state_root", lambda: root)
    return root / "gate-state"


# ---------------------------------------------------------------------------
# Regression: the hook neither reads the transcript nor decides the call
# ---------------------------------------------------------------------------


def test_hook_gate_never_opens_the_transcript_and_never_decides(tmp_path, capsys, monkeypatch, state_dir):
    """Fails on main at 2a2e9f5: the old gate read the whole transcript and
    printed ``permissionDecision: deny`` for exactly this payload."""

    canary = _write_canary(tmp_path)
    payload = {"tool_name": "Bash", "session_id": RAW_SESSION_ID, "transcript_path": str(canary)}

    code, out = _run_hook_gate(monkeypatch, capsys, json.dumps(payload), canary)

    assert code == 0
    assert "permissionDecision" not in _keys(json.loads(out))


# ---------------------------------------------------------------------------
# No decision in any branch
# ---------------------------------------------------------------------------


def _unwritable(state_dir: Path, monkeypatch) -> None:
    # The state root is a FILE, so gate-state beneath it can never be created.
    state_dir.parent.parent.mkdir(parents=True, exist_ok=True)
    state_dir.parent.write_text("not a directory", encoding="utf-8")


BRANCHES = {
    "worker-env": ({"tool_name": "Bash"}, {}),
    "paused": ({"tool_name": "Bash"}, {}),
    "manual": ({"tool_name": "Bash"}, ADVISORY_OUTPUT),
    "non-gated-tool": ({"tool_name": "Read"}, {}),
    "first-gated-call": ({"tool_name": "Bash"}, ADVISORY_OUTPUT),
    "second-gated-call": ({"tool_name": "Edit"}, {}),
    "missing-session-id": ({"tool_name": "Bash", "session_id": None}, {}),
    "unwritable-state-dir": ({"tool_name": "Bash"}, {}),
    "malformed-stdin": (None, {}),
}


@pytest.mark.parametrize("branch", list(BRANCHES))
def test_no_branch_returns_a_decision(tmp_path, capsys, monkeypatch, state_dir, branch):
    overrides, expected = BRANCHES[branch]
    canary = _write_canary(tmp_path)
    if branch == "worker-env":
        monkeypatch.setenv(WORKER_ENV_VAR, "1")
    elif branch == "paused":
        monkeypatch.setattr(service_history, "get_routing_paused", lambda db: True)
    elif branch == "manual":
        monkeypatch.setattr(service_history, "get_routing_mode", lambda db: "manual")
    elif branch == "second-gated-call":
        first = {"tool_name": "Bash", "session_id": RAW_SESSION_ID, "transcript_path": str(canary)}
        assert _run_hook_gate(monkeypatch, capsys, json.dumps(first), canary) == (0, json.dumps(ADVISORY_OUTPUT) + "\n")
    elif branch == "unwritable-state-dir":
        _unwritable(state_dir, monkeypatch)

    if overrides is None:
        stdin_text = "{not json"
    else:
        payload = {"session_id": RAW_SESSION_ID, "transcript_path": str(canary), **overrides}
        stdin_text = json.dumps(payload)

    code, out = _run_hook_gate(monkeypatch, capsys, stdin_text, canary)
    output = json.loads(out)

    assert code == 0
    assert not (_keys(output) & set(DECISION_KEYS)), output
    assert output == expected


def test_unwritable_state_dir_never_repeats_the_note(tmp_path, capsys, monkeypatch, state_dir):
    canary = _write_canary(tmp_path)
    _unwritable(state_dir, monkeypatch)
    payload = json.dumps({"tool_name": "Bash", "session_id": RAW_SESSION_ID, "transcript_path": str(canary)})

    for _ in range(3):
        assert _run_hook_gate(monkeypatch, capsys, payload, canary) == (0, "{}\n")


# ---------------------------------------------------------------------------
# Gate state: zero-byte, fingerprint-named, legacy raw-id markers removed
# ---------------------------------------------------------------------------


def test_gate_state_holds_no_raw_session_id_and_no_content(tmp_path, capsys, monkeypatch, state_dir):
    canary = _write_canary(tmp_path)
    state_dir.mkdir(parents=True)
    legacy = state_dir / f"{RAW_SESSION_ID}.0.prompted"
    legacy.write_text("", encoding="utf-8")
    payload = json.dumps({"tool_name": "Write", "session_id": RAW_SESSION_ID, "transcript_path": str(canary)})

    code, out = _run_hook_gate(monkeypatch, capsys, payload, canary)

    assert code == 0 and json.loads(out) == ADVISORY_OUTPUT
    assert not legacy.exists()
    files = list(state_dir.iterdir())
    assert len(files) == 1
    for entry in files:
        assert entry.is_file() and entry.stat().st_size == 0
        assert RAW_SESSION_ID not in entry.name
        assert RAW_SESSION_ID.lower() not in entry.name.lower()


# ---------------------------------------------------------------------------
# Static: nothing in gate.py can read a file
# ---------------------------------------------------------------------------


def test_gate_module_has_no_transcript_reader_and_no_file_read():
    tree = ast.parse(Path(gate_module.__file__).read_text(encoding="utf-8"))

    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert not defined & {"scan_modelquad_calls", "transcript_has_modelquad_call", "ModelQuadCallState"}

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    assert not called & {"read_text", "read_bytes", "open"}, called

    (evaluate,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "evaluate_gate"]
    loads = [
        n for n in ast.walk(evaluate) if isinstance(n, ast.Name) and n.id == "transcript_path" and isinstance(n.ctx, ast.Load)
    ]
    assert loads == []


# ---------------------------------------------------------------------------
# Status lines: they receive transcript_path too, and never touch it.
# This guard also passes on main at 2a2e9f5 -- no status-line path ever read it.
# ---------------------------------------------------------------------------


def test_status_line_hooks_never_open_the_transcript(tmp_path, capsys, monkeypatch):
    canary = _write_canary(tmp_path)
    payload = {
        "session_id": RAW_SESSION_ID,
        "transcript_path": str(canary),
        "cwd": str(tmp_path),
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "version": "2.1.280",
        "rate_limits": {"five_hour": {"used_percentage": 12}},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    with _canary_armed(monkeypatch, canary):
        code = main(["--usage-directory", str(tmp_path / "usage"), "usage-hook", "claude"])

    assert code == 0
    capsys.readouterr()


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
    from modelquad.providers import codex as providers_codex
    from modelquad.usage import codex_appserver
    from modelquad.usage import refresh as usage_refresh

    for module in (codex_appserver, usage_refresh):
        assert _real(module.__file__) in _FORBIDDEN_FILES, module.__file__
    assert _real(providers_codex.__file__).startswith(_FORBIDDEN_DIRS), providers_codex.__file__


def test_quiet_guard_catches_usage_collection_code(tmp_path, monkeypatch):
    from modelquad.usage import codex_appserver

    canary = _write_canary(tmp_path)
    public = [
        value
        for value in vars(codex_appserver).values()
        if inspect.isfunction(value) and value.__module__ == codex_appserver.__name__
    ]
    assert public
    with pytest.raises(AssertionError, match="provider or usage code ran"):
        with _canary_armed(monkeypatch, canary), _local_and_quiet(monkeypatch):
            function = public[0]
            required = [
                param
                for param in inspect.signature(function).parameters.values()
                if param.default is param.empty and param.kind is param.POSITIONAL_OR_KEYWORD
            ]
            try:
                # Arguments must bind, or no frame is ever entered.
                function(*[None] * len(required))
            except Exception:  # noqa: BLE001 -- only the call itself matters
                pass
