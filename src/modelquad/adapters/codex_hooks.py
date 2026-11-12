# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The owned SessionStart reminder entry in Claude Code's ``settings.json``.

JSON has no comment markers. Ownership is recorded before adding or changing
the entry, and retired before removing it; unrelated groups/handlers and
user-modified owned entries are preserved.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from .cliintegration import owns_cli_command
from .report import ActionKind, FileAction
from .staging import Change, StagedJson, UNCHANGED

UNSAFE = 'The reminder configuration or ownership could not be safely updated.'


def pending_entry(host: str) -> dict[str, Any]:
    from modelquad.hostruntime import integration_command

    command = integration_command('hook-pending', host, shell='bash')
    return {'matcher': '', 'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]}


def _ownership_key(path: Path, host: str) -> str:
    return f'{host}_pending_hook:' + hashlib.sha256(
        os.path.normcase(str(path.resolve())).encode('utf-8')).hexdigest()


def _plain_own_handler(handler: dict[str, Any]) -> bool:
    """Our command (already matched by the caller) with only the keys we write."""

    return handler.get('type') == 'command' and set(handler) <= {'type', 'command', 'timeout'}


def plan_pending_hook(staged: StagedJson, host: str, *, dry_run: bool = False,
                      remove: bool = False) -> Change:
    """Add, refresh or remove the reminder entry in a staged settings file.

    Evidence leads the config change in both directions. On install the
    ownership record is written first and restored if the file write fails.
    On removal the record is retired first -- a removed entry whose record
    survived would read as "the user removed it" and never come back -- and
    put back if the file write fails.
    """

    from .registration import (
        BackupPersistenceError, read_pending_hook_ownership, write_owned_settings,
    )

    if staged.refusal is not None:
        return UNCHANGED
    payload = staged.payload
    try:
        events = payload.get('hooks', {})
        if not isinstance(events, dict):
            raise ValueError()
        groups = events.get('SessionStart', [])
        if not isinstance(groups, list):
            raise ValueError()
        records = read_pending_hook_ownership()
    except (OSError, ValueError, TypeError, BackupPersistenceError):
        return Change('skipped', UNSAFE)
    key = _ownership_key(staged.path, host)
    recorded = records.get(key)
    desired = pending_entry(host) if not remove else None
    found = []
    for gi, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            continue
        for hi, handler in enumerate(group['hooks']):
            if isinstance(handler, dict) and owns_cli_command(handler.get('command'), ('hook-pending', host)):
                found.append((gi, hi, group, handler))
    if len(found) > 1:
        return Change('skipped', 'Multiple reminder entries were preserved for manual review.')
    if found:
        gi, hi, group, handler = found[0]
        expected = {'matcher': group.get('matcher', ''), 'hook': handler}
        # Without a record (the state folder moved), uninstall still takes an
        # entry that is plainly ours: our own command and nothing a user added.
        unrecorded_own = remove and recorded is None and _plain_own_handler(handler)
        if recorded != expected and not unrecorded_own:
            return Change('skipped', 'The reminder entry was modified or has no ownership record; it was preserved.')
        if not remove and expected == {'matcher': desired['matcher'], 'hook': desired['hooks'][0]}:
            return UNCHANGED
    elif remove:
        return UNCHANGED
    elif recorded is not None:
        # A user removed/repointed the command. Do not resurrect it.
        return Change('skipped', 'The recorded reminder was changed or removed; settings were preserved.')

    if not dry_run:
        previous_records = dict(records)
        if remove:
            records.pop(key, None)
        else:
            records[key] = {'matcher': desired['matcher'], 'hook': desired['hooks'][0]}
        try:
            write_owned_settings(records)
        except (OSError, BackupPersistenceError):
            return Change('skipped', UNSAFE)
        staged.on_failure(lambda: write_owned_settings(previous_records))

    if found:
        gi, hi, group, handler = found[0]
        if remove:
            # Keep a user's sibling handler even inside our own group.
            group['hooks'].pop(hi)
            if not group['hooks']:
                groups.pop(gi)
            if not groups:
                events.pop('SessionStart', None)
            if not events:
                payload.pop('hooks', None)
            return Change('remove')
        group['hooks'][hi] = desired['hooks'][0]
        group['matcher'] = desired['matcher']
    else:
        groups.append(desired)
        events['SessionStart'] = groups
        payload['hooks'] = events
    return Change('set')


def pending_action(staged: StagedJson, change: Change, *, dry_run: bool) -> FileAction:
    """Report the reminder's outcome, naming the reminder when a write failed."""

    if staged.refusal is not None:
        return FileAction(staged.path, ActionKind.SKIPPED, detail=f'{UNSAFE} {staged.refusal}')
    action = staged.action(change, dry_run=dry_run)
    if action.kind is ActionKind.SKIPPED and change.changes_file:
        return FileAction(action.path, action.kind, detail=f'{UNSAFE} {action.detail}')
    return action


def manage_pending_hook(path: Path, host: str, *, dry_run=False, remove=False) -> FileAction:
    """File-level :func:`plan_pending_hook`, written once with a backup."""

    staged = StagedJson.load(Path(path))
    change = plan_pending_hook(staged, host, dry_run=dry_run, remove=remove)
    return pending_action(staged, change, dry_run=dry_run)
