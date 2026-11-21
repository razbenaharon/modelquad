# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""How long each review hook call took to start and to finish: numbers only.

Claude Code stops waiting for a hook after its timeout and says "A hook didn't
answer in time". A call it stopped never reaches its own end, so the record is
written in two lines: one when the hook's code begins (how long the process
took to get there) and one when it ends (how long the work took). A call with a
start line and no end line was stopped while working; a notice with no start
line at all was stopped before ModelQuad's code ran.

Each line holds a time, a random call id, the hook event, a tool kind from a
fixed set and milliseconds. No path, session id, file name or text. The file
is cut back to its newest half when it passes ``MAX_BYTES``. Nothing here may
make the hook fail or print: every error is swallowed.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

FILE_NAME = "hook-timing.jsonl"
MAX_BYTES = 64 * 1024
#: A parent created longer ago than this is not the launcher that started the
#: hook (Claude Code itself, or a shell): its age says nothing, so it is left out.
LAUNCHER_LIMIT_MS = 10 * 60 * 1000
TOOL_KINDS = {
    "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Bash": "run", "PowerShell": "run",
    # The review tool, from the plugin and from `modelquad install claude`
    # (review_reminder.DELEGATE_TOOLS).
    "mcp__plugin_modelquad_review__delegate": "modelquad", "mcp__modelquad__delegate": "modelquad",
}


def _created(pid: int) -> float | None:
    """The process's creation time as a UTC epoch (Windows), or None."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import byref, wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(byref(t) for t in times)):
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)


def _age_ms(created: float | None, now: float) -> int | None:
    if created is None:
        return None
    age = round((now - created) * 1000)
    return age if 0 <= age else None


def _append(path: Path, line: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_BYTES:
            kept = path.read_bytes()[-(MAX_BYTES // 2):]
            kept = kept[kept.find(b"\n") + 1:]
            path.write_bytes(kept)
    except FileNotFoundError:
        pass
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, separators=(",", ":")) + "\n")


def started(*, state=None, now=time.time, created=_created) -> dict | None:
    """Write the start line; returns what ``finished`` needs, or None (off, or an error)."""
    try:
        from modelquad import switch
        if switch.enabled() is not True:
            return None  # ModelQuad off: the hook does nothing, and keeps nothing.
        if state is None:
            from modelquad.paths import state_root
            state = state_root()
        moment = now()
        launcher = _age_ms(created(os.getppid()), moment)
        mark = {"path": Path(state) / FILE_NAME, "id": secrets.token_hex(4),
                "clock": time.monotonic()}
        _append(mark["path"], {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)),
            "id": mark["id"], "phase": "start",
            "python_ms": _age_ms(created(os.getpid()), moment),
            "launcher_ms": launcher if launcher is not None and launcher <= LAUNCHER_LIMIT_MS else None,
        })
        return mark
    except Exception:
        return None


def finished(mark: dict | None, facts: dict) -> None:
    """Write the end line for a call ``started`` recorded."""
    if mark is None:
        return
    try:
        event = facts.get("event")
        tool = facts.get("tool")
        _append(mark["path"], {
            "id": mark["id"], "phase": "end",
            "event": event if event in ("PreToolUse", "PostToolUse", "Stop") else None,
            "tool": ((TOOL_KINDS.get(tool) if isinstance(tool, str) else None) or "other") if tool else None,
            "work_ms": round((time.monotonic() - mark["clock"]) * 1000),
        })
    except Exception:
        pass
