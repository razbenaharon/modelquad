# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The one global switch: whether ModelQuad may start Codex at all.

Stored in ``preferences.json`` under ``codex_enabled``. Only a stored
``false`` turns Codex off; a preferences file that cannot be read reads
unknown, and the caller refuses (a switch the user may have set to off must
never read as on).

Earlier builds kept this switch in the history database (``modelquad host
disconnect claude`` and a pause flag). While the user has not set the switch
here, an off stored there still counts as off: an upgrade never turns Codex
back on by itself. That one read opens the old file read-only and looks at
two rows; nothing else in ModelQuad uses that database any more.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

KEY = "codex_enabled"
OFF = "modelquad_off"
UNKNOWN = "modelquad_switch_unreadable"
OFF_MESSAGE = (
    "ModelQuad is turned off on this computer, so Codex was not started. Tell the user "
    "plainly. Only the user turns it back on, with /modelquad:settings or by running "
    "modelquad on in their own terminal. Do not run that command yourself.")
UNKNOWN_MESSAGE = (
    "ModelQuad could not read its own settings, so it cannot tell whether the user turned "
    "it off, and Codex was not started. Run: modelquad doctor")

_LEGACY_DB_NAME = "modelquad.db"


def _preferences(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    from .adapters.server_instructions import preferences_path

    return preferences_path()


def _legacy_off(state_root: Path) -> bool:
    """Whether an earlier build's database says Claude Code is disconnected or paused.

    Unreadable or absent reads as not off: it is another program's old file,
    and the switch itself is the stored setting above.
    """
    target = Path(state_root) / _LEGACY_DB_NAME
    try:
        if not target.is_file():
            return False
        conn = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = dict(conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('disconnected_hosts', 'routing_paused')"
            ).fetchall())
        finally:
            conn.close()
        hosts = json.loads(rows["disconnected_hosts"]) if "disconnected_hosts" in rows else []
        paused = json.loads(rows["routing_paused"]) if "routing_paused" in rows else False
        return paused is True or (isinstance(hosts, list) and "claude" in hosts)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def enabled(path: Path | None = None) -> bool | None:
    """True: on. False: the user turned ModelQuad off. None: the setting could not be read."""
    from .adapters.server_instructions import _load_preferences

    target = _preferences(path)
    try:
        stored = _load_preferences(target).get(KEY)
    except OSError:
        return None
    if type(stored) is bool:
        return stored
    return not _legacy_off(target.parent)


def refusal(path: Path | None = None) -> dict | None:
    """The closed refusal while ModelQuad is off or the switch is unreadable; None when on."""
    state = enabled(path)
    if state is True:
        return None
    if state is False:
        return {"reason": OFF, "message": OFF_MESSAGE}
    return {"reason": UNKNOWN, "message": UNKNOWN_MESSAGE}


def write(on: bool, path: Path | None = None) -> Path:
    """Store the switch, keeping every other preference. Raises when the file is unreadable."""
    from .adapters.atomicwrite import atomic_write
    from .adapters.server_instructions import preferences_for_write

    target = _preferences(path)
    payload = preferences_for_write(target)
    payload[KEY] = bool(on)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return target
