# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Narrow JSON config updates owned by one adapter key path.

The in-memory helpers (:func:`set_key`, :func:`delete_key`) are what install
and uninstall use, through :mod:`staging`, so each file is written once per
run. The file-level helpers wrap them for callers that change one key alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .report import FileAction
from .staging import Change, StagedJson, UNCHANGED


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read-only parse: ``({}, None)`` for an absent or empty file."""

    staged = StagedJson.load(path, read_only=True)
    if staged.refusal is not None:
        return None, staged.refusal
    return staged.payload, None


def set_key(payload: dict[str, Any], key_path: tuple[str, ...], value: Any) -> Change:
    """Set one nested key in memory while preserving unrelated object entries."""

    if not key_path:
        raise ValueError("key_path must contain at least one key")
    cursor = payload
    for key in key_path[:-1]:
        nested = cursor.get(key)
        if nested is None:
            nested = {}
            cursor[key] = nested
        if not isinstance(nested, dict):
            return Change(
                "skipped", f"Could not safely update JSON config: {key!r} is not an object."
            )
        cursor = nested
    leaf = key_path[-1]
    if leaf in cursor and cursor[leaf] == value:
        return UNCHANGED
    cursor[leaf] = value
    return Change("set")


def delete_key(payload: dict[str, Any], key_path: tuple[str, ...]) -> Change:
    """Remove one nested key in memory without disturbing sibling configuration."""

    if not key_path:
        raise ValueError("key_path must contain at least one key")
    cursor = payload
    for key in key_path[:-1]:
        nested = cursor.get(key)
        if not isinstance(nested, dict):
            return UNCHANGED
        cursor = nested
    if key_path[-1] not in cursor:
        return UNCHANGED
    del cursor[key_path[-1]]
    return Change("remove")


def upsert_json_key(
    path: Path,
    key_path: tuple[str, ...],
    value: Any,
    *,
    dry_run: bool,
    backup: bool = True,
) -> FileAction:
    """Set one nested JSON key in a file, backing the file up first.

    ``backup=False`` is for a second write in the same run, after an earlier
    write already saved the file's original bytes.
    """

    staged = StagedJson.load(path)
    staged.backup = backup
    change = set_key(staged.payload, key_path, value) if staged.refusal is None else UNCHANGED
    return staged.action(change, dry_run=dry_run)


def remove_json_key(
    path: Path,
    key_path: tuple[str, ...],
    *,
    dry_run: bool,
) -> FileAction:
    """Remove one nested JSON key from a file, backing the file up first."""

    staged = StagedJson.load(path)
    change = delete_key(staged.payload, key_path) if staged.refusal is None else UNCHANGED
    return staged.action(change, dry_run=dry_run)
