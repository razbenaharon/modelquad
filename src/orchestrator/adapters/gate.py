# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Runtime logic for the advisory Claude Code PreToolUse hook.

Distinct from *installing* the hook (``claude_adapter.py`` / ``hooksmerge.py``):
this module is the logic invoked by the ``modelquad hook-gate`` CLI command
each time a Claude Code process is about to run a gated tool.

The hook is **advisory**. It never decides anything
about the Host's tool call: it returns no ``permissionDecision`` -- neither
``deny`` nor ``allow`` nor ``ask`` -- so Claude Code's own permission flow
applies to every call exactly as it would with no hook installed. ModelQuad
neither stops the Host nor grants it a permission the user did not. All it
may do is add one short note to the Host's context, at most once per session,
suggesting that non-trivial work be routed through ``modelquad_route``.

It also **never reads a conversation file**. Claude Code passes every hook the
path of the live session transcript; this module never opens, reads or stats
it. Its only inputs are the tool name, the session id, the worker guard in the
environment, the routing mode and pause state it is handed, and its own state
directory. It carries no routing business logic and names no provider: which
provider, model or effort to use remains the Router's sole authority.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..branding import PRODUCT_NAME
from ..execution.base import WORKER_ENV_VAR

# Tools whose invocation constitutes "substantive engineering work". Mirrors
# the installed matcher (claude_adapter._HOOK_MATCHER); read-only
# reconnaissance tools never receive the note.
GATED_TOOL_NAMES = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Task"})

# A suggestion, not a policy: it restates no recommendation and names no
# provider. A consultation it prompts was triggered by this hook rather than by
# an explicit user request, which the MCP server cannot infer on its own --
# hence the invocation_mode hint.
ADVISORY_NOTE = (
    f"{PRODUCT_NAME} is available for a read-only Codex second opinion. "
    "This advisory note blocks and approves nothing, and appears once per session."
)

# One zero-byte marker per Host session, named by the session's fingerprint.
_MARKER_SUFFIX = ".advised"
# Markers the pre-advisory gate wrote, named by the RAW session id. Removed on
# sight: a raw host session id is never kept on disk.
_LEGACY_MARKER_GLOB = "*.prompted"
# Best-effort retention: the directory otherwise grows by one file per session.
_MARKER_RETENTION_SECONDS = 7 * 24 * 3600


def _advisory() -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ADVISORY_NOTE}}


def _tidy_state_dir(state_dir: Path) -> None:
    """Delete legacy raw-id markers and markers past their retention window.

    Best-effort and silent: tidying must never affect the output.
    """

    try:
        for legacy in state_dir.glob(_LEGACY_MARKER_GLOB):
            try:
                legacy.unlink()
            except OSError:
                continue
        cutoff = time.time() - _MARKER_RETENTION_SECONDS
        for candidate in state_dir.glob(f"*{_MARKER_SUFFIX}"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _session_marker_name(session_id: str) -> str | None:
    """The marker's file name: the session's installation-scoped fingerprint.

    Never the raw id. ``None`` when no fingerprint can be derived, in which
    case the caller emits nothing rather than a note on every call.
    """

    try:
        from ..hostobs import session_fingerprint

        fingerprint = session_fingerprint(session_id)
    except Exception:  # noqa: BLE001 -- the hook must never fail the Host
        return None
    if not fingerprint:
        return None
    return f"{fingerprint}{_MARKER_SUFFIX}"


def evaluate_gate(
    *,
    tool_name: str,
    transcript_path: str | None = None,
    session_id: str | None,
    environ: Mapping[str, str],
    state_dir: Path,
    routing_mode: str | None = None,
    routing_paused: bool = False,
) -> dict[str, Any]:
    """Return the hook's output for one PreToolUse invocation.

    The result is either ``{}`` (no output: Claude Code proceeds through its
    normal permission flow) or exactly ``{"hookSpecificOutput":
    {"hookEventName": "PreToolUse", "additionalContext": ADVISORY_NOTE}}``. It
    never carries a permission decision.

    ``transcript_path`` is accepted only because the CLI still passes the
    payload's value. It is never opened, read, stat'ed or otherwise used.

    The note is emitted on the first gated tool call of a Host session, and
    only when this process is not a delegated worker (``ORCHESTRATOR_WORKER``
    unset -- a worker must not be pointed back at ModelQuad), routing is not
    paused, a session id is present, and the
    session's marker was newly recorded. Every other case returns ``{}``,
    including a state directory that cannot be written: failing quiet is
    better than repeating the note on every call.
    """

    state_path = Path(state_dir)
    _tidy_state_dir(state_path)
    if environ.get(WORKER_ENV_VAR) == "1":
        return {}
    if routing_paused:
        return {}
    if tool_name not in GATED_TOOL_NAMES:
        return {}
    if not isinstance(session_id, str) or not session_id.strip():
        return {}
    marker_name = _session_marker_name(session_id)
    if marker_name is None:
        return {}
    try:
        state_path.mkdir(parents=True, exist_ok=True)
        # Exclusive create: exactly one call per session records the marker.
        (state_path / marker_name).touch(exist_ok=False)
    except OSError:
        return {}
    return _advisory()
