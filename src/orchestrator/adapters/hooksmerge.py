# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Managed hook-entry merge for a Claude Code ``settings.json``.

Distinct from ``jsonmerge.py``'s scalar/nested-object key upserts: a hook
event (e.g. ``"PreToolUse"``) is a *list* of independent hook groups, which
may already contain entries a user configured for unrelated tools/matchers.
This module owns exactly one entry within that list -- identified by one of
its nested ``hooks[].command`` values matching our own known command string
-- without disturbing any sibling group the user added.

:func:`merge_hook_entry` and :func:`drop_hook_entry` change a payload in
memory (install and uninstall stage the whole file and write it once); the
file-level helpers wrap them for single-change callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jsonmerge import _read_json
from .report import ActionKind, FileAction
from .staging import Change, StagedJson, UNCHANGED


def _find_owned_group_index(event_list: list[Any], owned_command: str) -> int | None:
    for index, group in enumerate(event_list):
        if not isinstance(group, dict):
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and hook.get("command") == owned_command:
                return index
    return None


def merge_hook_entry(
    payload: dict[str, Any], event: str, entry: dict[str, Any], owned_command: str
) -> Change:
    """Insert or update exactly one owned hook entry within one event's list.

    ``owned_command`` identifies which group (if any) already belongs to us,
    by exact match against a nested ``hooks[].command`` value -- never by
    list position, since a user's own unrelated groups may already occupy
    earlier slots. When an owned group already exists, only our own hook
    dict within its ``hooks`` list is replaced/inserted -- any sibling hook
    entry a user manually added into that same group (same matcher) is left
    untouched, matching ``drop_hook_entry``'s equally surgical behavior.
    """

    hooks_section = payload.get("hooks", {})
    if not isinstance(hooks_section, dict):
        return Change("skipped", "Could not safely update JSON config: 'hooks' is not an object.")
    event_list = hooks_section.get(event, [])
    if not isinstance(event_list, list):
        return Change(
            "skipped", f"Could not safely update JSON config: 'hooks.{event}' is not an array."
        )

    owned_index = _find_owned_group_index(event_list, owned_command)
    if owned_index is not None:
        existing_group = event_list[owned_index]
        existing_hooks = existing_group.get("hooks")
        sibling_hooks = [
            hook
            for hook in (existing_hooks if isinstance(existing_hooks, list) else [])
            if not (isinstance(hook, dict) and hook.get("command") == owned_command)
        ]
        our_hooks = entry.get("hooks") if isinstance(entry.get("hooks"), list) else []
        merged_group = dict(existing_group)
        merged_group["matcher"] = entry.get("matcher", existing_group.get("matcher"))
        merged_group["hooks"] = sibling_hooks + list(our_hooks)
        if merged_group == existing_group:
            return UNCHANGED
        event_list[owned_index] = merged_group
    else:
        event_list.append(entry)
    hooks_section[event] = event_list
    payload["hooks"] = hooks_section
    return Change("set")


def drop_hook_entry(payload: dict[str, Any], event: str, owned_command: str) -> Change:
    """Remove only our owned hook dict, and its group if that empties it.

    Surgical by design: if a user manually added a second hook entry into
    the same group we created, only our entry is removed and the group
    (with the user's entry intact) is kept -- never the whole group blindly.
    """

    hooks_section = payload.get("hooks")
    if not isinstance(hooks_section, dict):
        return UNCHANGED
    event_list = hooks_section.get(event)
    if not isinstance(event_list, list):
        return UNCHANGED

    changed = False
    new_event_list: list[Any] = []
    for group in event_list:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            new_event_list.append(group)
            continue
        remaining_hooks = [
            hook
            for hook in group["hooks"]
            if not (isinstance(hook, dict) and hook.get("command") == owned_command)
        ]
        if len(remaining_hooks) != len(group["hooks"]):
            changed = True
        if remaining_hooks:
            new_group = dict(group)
            new_group["hooks"] = remaining_hooks
            new_event_list.append(new_group)
        # else: this group only ever held our hook -- drop the whole group.

    if not changed:
        return UNCHANGED

    hooks_section[event] = new_event_list
    if not hooks_section[event]:
        del hooks_section[event]
    if not hooks_section:
        del payload["hooks"]
    return Change("remove")


def upsert_hook_entry(
    path: Path,
    event: str,
    entry: dict[str, Any],
    owned_command: str,
    *,
    dry_run: bool,
) -> FileAction:
    """File-level :func:`merge_hook_entry`, written once with a backup."""

    staged = StagedJson.load(path)
    change = (
        merge_hook_entry(staged.payload, event, entry, owned_command)
        if staged.refusal is None else UNCHANGED
    )
    return staged.action(change, dry_run=dry_run)


def remove_hook_entry(path: Path, event: str, owned_command: str, *, dry_run: bool) -> FileAction:
    """File-level :func:`drop_hook_entry`, written once with a backup."""

    if not path.exists():
        return FileAction(path, ActionKind.UNCHANGED)
    staged = StagedJson.load(path)
    change = (
        drop_hook_entry(staged.payload, event, owned_command)
        if staged.refusal is None else UNCHANGED
    )
    return staged.action(change, dry_run=dry_run)


def has_hook_entry(path: Path, event: str, owned_command: str) -> bool:
    """Structural check: does our owned hook entry exist in this settings file?

    Static file inspection only -- proves the hook is *wired*, not that it is
    actually invoked by a live Claude Code process (see doctor.py's
    docstring for that distinction).
    """

    payload, error = _read_json(path)
    if error is not None or payload is None:
        return False
    hooks_section = payload.get("hooks")
    if not isinstance(hooks_section, dict):
        return False
    event_list = hooks_section.get(event)
    if not isinstance(event_list, list):
        return False
    return _find_owned_group_index(event_list, owned_command) is not None


def payload_commands(payload: dict[str, Any] | None, event: str) -> tuple[str, ...]:
    """The command strings wired for an event in an already-parsed payload."""

    hooks_section = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks_section, dict):
        return ()
    event_list = hooks_section.get(event)
    if not isinstance(event_list, list):
        return ()
    return tuple(
        hook["command"]
        for group in event_list
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for hook in group["hooks"]
        if isinstance(hook, dict) and isinstance(hook.get("command"), str)
    )


def wired_commands(path: Path, event: str) -> tuple[str, ...]:
    """Read the command strings wired for an event, without judging ownership.

    Like ``has_hook_entry``, this proves wiring only, never live invocation.
    Malformed groups are ignored so a user's unrelated entries cannot turn
    inspection for superseded commands into an assumption about their shape.
    """

    payload, error = _read_json(path)
    if error is not None or payload is None:
        return ()
    return payload_commands(payload, event)
