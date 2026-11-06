# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Advisory PostToolUse context: a reminder of the review moment after several files change.

Claude tends to forget an instruction it read at session start while it is
deep in a task, and a reminder given in the middle of the edits is forgotten
by the end (a recorded trial of fresh sessions, 2026-11-06). So the hook
counts the different files Claude changes in a session, and once there are
``THRESHOLD`` without a Codex review since, it adds one line of context when
Claude next runs a command (usually the tests, at the end of the change),
at most ``MAX_REMINDERS`` times, and only after further edits since the last
one. A ``modelquad_delegate`` call starts the count again. It is advisory (AGENTS.md invariant 6): it prints context or nothing,
never a decision, never blocks or approves, and never opens the transcript
path Claude Code hands it. It starts no process and never starts Codex.

What it keeps: one small file per session under ``<state>/review-reminder/``,
named by a digest of the session id, holding SHA-256 digests of the changed
file paths (never the paths) and how often it reminded. Files older than
two days are removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from pathlib import Path

#: A Write call carries the whole file, so the cap is larger than the startup hook's.
BYTE_CAP = 8 * 1024 * 1024
HOOK_BUDGET = 1.5
THRESHOLD = 3
MAX_REMINDERS = 2
MAX_DIGESTS = 64
KEEP_SECONDS = 2 * 24 * 3600
FOLDER_NAME = "review-reminder"
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
#: Running a command after the edits is usually the end of a change: the tests.
RUN_TOOLS = frozenset({"Bash", "PowerShell"})
DELEGATE_TOOL_SUFFIX = "modelquad_delegate"

DECIDE = (
    "ModelQuad: you have changed {count} files in this session and Codex has not reviewed "
    "them. Before your final message on this change, call `modelquad_delegate` for a "
    "review, with the changed files in `files`. Claude Code asks the user first; if they "
    "decline, carry on without Codex. A change across this many files is not a small "
    "one, so this applies to it.")
OFFER = (
    "ModelQuad: you have changed {count} files in this session and Codex has not reviewed "
    "them. In your final message on this change, offer the user a Codex review in one "
    "short sentence. A change across this many files is not a small one, so this "
    "applies to it.")

_EMPTY = {"files": [], "reminders": 0, "edits_since": 0}


def _read_event(stdin, clock, deadline):
    incoming = queue.Queue(maxsize=1)

    def read_input():
        try:
            incoming.put(stdin.read(BYTE_CAP + 1))
        except Exception:
            incoming.put(None)

    threading.Thread(target=read_input, daemon=True).start()
    raw = incoming.get(timeout=max(0, deadline - clock()))
    if not isinstance(raw, (bytes, str)):
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > BYTE_CAP:
        return None
    event = json.loads(raw)
    if not isinstance(event, dict) or event.get("hook_event_name") != "PostToolUse":
        return None
    return event


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _changed_path(event) -> str | None:
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path or len(path) > 4096:
        return None
    return os.path.normcase(os.path.normpath(path))


def _count(value) -> int:
    return value if type(value) is int and 0 <= value <= 1000 else 0


def _load(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_EMPTY, files=[])
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        return dict(_EMPTY, files=[])
    return {"files": files[:MAX_DIGESTS], "reminders": _count(payload.get("reminders")),
            "edits_since": _count(payload.get("edits_since"))}


def _save(path: Path, payload: dict) -> None:
    from modelquad.adapters.atomicwrite import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, sort_keys=True) + "\n")


def _prune(folder: Path, now: float) -> None:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.suffix == ".json" and now - entry.stat().st_mtime > KEEP_SECONDS:
                entry.unlink()
        except OSError:
            pass


def _codex_allowed_mode(cwd) -> str | None:
    """'decide' or 'offer' for this folder, or None when Codex is off or only on request."""
    from modelquad import repo_settings
    from modelquad.adapters.server_instructions import read_proactive
    from modelquad.localpath import reason as local_reason

    proactive = read_proactive()
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute() or local_reason(cwd):
        return "decide" if proactive else "offer"
    user_mode = repo_settings.read_mode(Path(cwd))
    result = repo_settings.effective(global_proactive=proactive, user_mode=user_mode,
                                     policy=repo_settings.local_policy(Path(cwd)))
    if result.refusal is not None:
        return None
    return {repo_settings.PROACTIVE: "decide", repo_settings.OFFER: "offer"}.get(result.offer)


def run_hook(host, stdin, stdout, *, environ=None, clock=time.monotonic, state=None,
             mode=None, now=time.time):
    """Fail silent: any failure prints nothing and exits 0."""
    environ = os.environ if environ is None else environ
    if environ.get("MODELQUAD_WORKER") == "1" or host != "claude":
        return 0
    deadline = clock() + HOOK_BUDGET
    try:
        event = _read_event(stdin, clock, deadline)
        if event is None:
            return 0
        tool = event.get("tool_name")
        session = event.get("session_id")
        if not isinstance(tool, str) or not isinstance(session, str) or not session:
            return 0
        is_delegate = tool.endswith(DELEGATE_TOOL_SUFFIX)
        if tool not in EDIT_TOOLS and tool not in RUN_TOOLS and not is_delegate:
            return 0
        if state is None:
            from modelquad.paths import state_root
            state = state_root()
        folder = Path(state) / FOLDER_NAME
        record = folder / f"{_digest(session)[:32]}.json"
        if is_delegate:
            if record.exists():
                _save(record, dict(_EMPTY, files=[]))
            return 0
        if tool in RUN_TOOLS:
            if not record.exists():
                return 0  # Nothing changed yet: a command alone keeps nothing.
            payload = _load(record)
            remind = (len(payload["files"]) >= THRESHOLD and payload["edits_since"] > 0
                      and payload["reminders"] < MAX_REMINDERS)
            if not remind:
                return 0
        else:
            changed = _changed_path(event)
            if changed is None:
                return 0
            payload = _load(record)
            digest = _digest(changed)
            if digest not in payload["files"] and len(payload["files"]) < MAX_DIGESTS:
                payload["files"].append(digest)
            payload["edits_since"] += 1
            remind = False
        text = None
        if remind and clock() < deadline:
            chosen = (mode or _codex_allowed_mode)(event.get("cwd"))
            if chosen in ("decide", "offer"):
                text = (DECIDE if chosen == "decide" else OFFER).format(
                    count=len(payload["files"]))
            payload["reminders"] += 1
            payload["edits_since"] = 0
        _save(record, payload)
        _prune(folder, now())
        if text is not None and clock() < deadline:
            stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": text}},
                separators=(",", ":")) + "\n")
    except Exception:
        pass  # No diagnostic text, state marker or user-facing message.
    return 0
