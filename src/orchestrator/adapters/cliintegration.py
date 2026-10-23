# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Ownership and read-only inspection of host commands invoking our CLI.

Keep runtime imports lazy: adapters must import in a bare interpreter, before
the product config dependencies are available.
"""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any

from ..branding import CLI_COMMAND
from .report import FileAction
from .staging import Change, StagedJson, UNCHANGED

#: The only keys ModelQuad itself writes into ``statusLine``.
_OWNED_STATUS_LINE_KEYS = frozenset({"type", "command"})


def owns_cli_command(command: object, arguments: tuple[str, ...]) -> bool:
    """Recognize only our CLI name invoked with exactly our fixed arguments."""

    from .. import hostruntime

    if not isinstance(command, str):
        return False
    executable, recorded_arguments = hostruntime.split_command(command)
    # Never use a raw substring match: a user's helper can mention modelquad
    # without being our hook, and different arguments belong to another use.
    name = PureWindowsPath(executable).name.lower()
    if name.endswith((".cmd", ".exe", ".bat")):
        name = name[:-4]
    return recorded_arguments == arguments and name == CLI_COMMAND


def owns_status_line_command(command: object, arguments: tuple[str, ...]) -> bool:
    """Whether ModelQuad wrote this status line: exactly our own CLI command."""

    return owns_cli_command(command, arguments)


def plan_owned_status_line(
    payload: dict[str, Any], arguments: tuple[str, ...], command: str, *,
    status_type: str | None = None,
) -> Change:
    """Install or refresh our status line in memory without reclaiming the user's.

    ``status_type`` is written beside the command when the Host's schema needs
    it: Claude Code ignores a ``statusLine`` without ``"type": "command"``.
    """

    status = payload.get("statusLine")
    # The product owns this key only for as long as the user has not taken it
    # back. Repairing an unrelated integration is no licence to replace theirs.
    if isinstance(status, dict) and "command" in status:
        if not owns_status_line_command(status["command"], arguments):
            return Change(
                "skipped", f"Preserved user-owned status line command: {status['command']}."
            )
    if status is not None and not isinstance(status, dict):
        return Change(
            "skipped", "Could not safely update JSON config: 'statusLine' is not an object."
        )
    wanted = dict(status or {})
    if status_type is not None:
        wanted["type"] = status_type
    wanted["command"] = command
    if status == wanted:
        return UNCHANGED
    payload["statusLine"] = wanted
    return Change("set")


def plan_remove_owned_status_line(payload: dict[str, Any], arguments: tuple[str, ...]) -> Change:
    """Remove our status line in memory, leaving a user's replacement in place.

    When the object holds only what ModelQuad writes (``type`` and
    ``command``), the whole object goes: a ``statusLine`` reduced to
    ``{"type": "command"}`` is not a valid Claude Code setting. Any other key
    the user added keeps the object, minus our command.
    """

    status = payload.get("statusLine")
    if not isinstance(status, dict) or "command" not in status:
        return UNCHANGED
    if not owns_status_line_command(status["command"], arguments):
        return UNCHANGED
    if set(status) <= _OWNED_STATUS_LINE_KEYS:
        del payload["statusLine"]
    else:
        remaining = dict(status)
        del remaining["command"]
        payload["statusLine"] = remaining
    return Change("remove")


def upsert_owned_status_line(
    path: Path, arguments: tuple[str, ...], command: str, *, dry_run: bool,
    status_type: str | None = None,
) -> FileAction:
    """File-level :func:`plan_owned_status_line`, written once with a backup."""

    staged = StagedJson.load(path)
    change = (
        plan_owned_status_line(staged.payload, arguments, command, status_type=status_type)
        if staged.refusal is None else UNCHANGED
    )
    return staged.action(change, dry_run=dry_run)


def remove_owned_status_line(path: Path, arguments: tuple[str, ...], *, dry_run: bool) -> FileAction:
    """File-level :func:`plan_remove_owned_status_line`, written once with a backup."""

    staged = StagedJson.load(path)
    change = (
        plan_remove_owned_status_line(staged.payload, arguments)
        if staged.refusal is None else UNCHANGED
    )
    return staged.action(change, dry_run=dry_run)


def read_status_line_command(path: Path) -> str | None:
    """Read wiring without claiming ownership; malformed or absent is unknown."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    status = payload.get("statusLine") if isinstance(payload, dict) else None
    command = status.get("command") if isinstance(status, dict) else None
    return command if isinstance(command, str) else None
