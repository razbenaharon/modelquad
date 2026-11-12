# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad settings`` (or ``settings show``): the three settings, in plain words.

1. Whether ModelQuad is on for Claude Code: first whether it is installed
   there at all (the plugin record or the regular install, as ``modelquad
   doctor`` checks them), then the ``modelquad host`` control, read exactly
   as ``modelquad host status`` reads it.
2. The mode: automatic (the standing grant, for every folder or for this
   folder's repository) or approve each call, and whether this folder is
   excluded (``modelquad settings repo off`` or a ``.modelquad.toml`` that
   turns Codex off), read exactly as ``modelquad settings auto status`` and
   ``modelquad settings repo`` read them.
3. History: one line from the review ledger for this project; the detail is
   ``modelquad review report``.

Then the command that changes each one. It only reads: it writes no setting,
migrates no preference and creates no file the reading commands would not.
Anything it cannot read reads unknown, never a default.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: The period the history line covers; ``modelquad review report`` uses the same default.
HISTORY_DAYS = 30


def _claude_installed() -> bool | None:
    """Whether ModelQuad is installed in Claude Code, as ``modelquad doctor`` tells.

    True: Claude Code's plugin record lists the plugin, or the regular install
    (the skill and the ``modelquad`` server entry) is in place. False: neither,
    and no plugin record exists or it does not list ModelQuad. None: unknown.
    """
    from .adapters.claude_adapter import ClaudeAdapter, plugin_recorded
    from .adapters.paths import default_target_dir

    try:
        target = default_target_dir("claude")
        plugin = plugin_recorded(target)
        if plugin is True or ClaudeAdapter(via_plugin=False).is_installed(target):
            return True
        if plugin is False or not (target / "plugins" / "installed_plugins.json").exists():
            return False
    except (OSError, ValueError):
        pass
    return None


def _host_line(db_path) -> str:
    from .service import history as service_history

    installed = _claude_installed()
    if installed is False:
        return ("1. ModelQuad in Claude Code: not installed "
                "(run modelquad install claude, or install the plugin).")
    if installed is None:
        return "1. ModelQuad in Claude Code: unknown (whether it is installed could not be read)."
    try:
        hosts = service_history.get_launch_state(db_path)["disconnected_hosts"]
    except (service_history.LaunchStateUnavailable, OSError, ValueError, sqlite3.Error):
        return "1. ModelQuad in Claude Code: unknown (its launch settings could not be read)."
    if "claude" in hosts:
        return "1. ModelQuad in Claude Code: off (disconnected: Claude Code starts no new Codex work)."
    return "1. ModelQuad in Claude Code: on (connected)."


def _mode_lines(folder: Path) -> tuple[list[str], bool]:
    """The mode lines, and whether this folder's repository has its own grant."""
    from . import grant, repo_settings
    from .adapters.server_instructions import _load_preferences, preferences_path, read_proactive

    try:
        _load_preferences(preferences_path())
    except Exception:  # noqa: BLE001 -- unreadable: the mode is unknown, not "approve each call"
        return (["2. Mode: unknown (ModelQuad could not read its preferences.json)."], False)
    everywhere = grant.granted(None)
    here = grant.granted_for_repository(folder)
    if everywhere:
        lines = ["2. Mode: automatic. The standing grant is on for every folder: Claude may ask "
                 "Codex without Claude Code's prompt for each call."]
    elif here:
        lines = ["2. Mode: automatic. The standing grant is on for this folder's repository: "
                 "Claude may ask Codex here without Claude Code's prompt for each call."]
    else:
        lines = ["2. Mode: approve each call. Claude Code asks you before every Codex call."]
    try:
        mode = repo_settings.read_mode(folder)
        result = repo_settings.effective(global_proactive=read_proactive(), user_mode=mode,
                                         policy=repo_settings.local_policy(folder))
    except Exception:  # noqa: BLE001 -- unknown stays unknown
        lines.append("   Whether this folder is excluded: unknown (its settings could not be read).")
        return lines, here
    if result.refusal == repo_settings.USER_CODEX_OFF:
        lines.append("   This folder is excluded: you turned Codex off here "
                     "(modelquad settings repo off), which outranks the mode.")
    elif result.refusal is not None:
        lines.append(f"   This folder is excluded: this repository's {repo_settings.REPO_FILE_NAME} "
                     "turns Codex off, which outranks the mode.")
    return lines, here


def _history_line(folder: Path) -> str:
    from . import review_ledger

    try:
        if not review_ledger.ledger_path().is_file():
            return "3. History here: none yet (no Codex calls recorded)."
        data = review_ledger.report(days=HISTORY_DAYS,
                                    workspace=review_ledger.workspace_id(folder))
    except (OSError, sqlite3.Error, review_ledger.LedgerVersionError):
        return "3. History here: unknown (the review ledger could not be read)."
    # The same counts as modelquad review report: a Codex call is one in which Codex was
    # started; a call stopped before that is named apart, never counted as a call.
    stopped = (f" ({data['not_started']} more stopped before Codex started)"
               if data["not_started"] else "")
    if not data["codex_started"]:
        return f"3. History here: no Codex calls in the last {HISTORY_DAYS} days{stopped}."
    f = data["findings"]
    confirmed = f["confirmed_independent"] + f["confirmed_claude_only"]
    text = (f"3. History here, last {HISTORY_DAYS} days: {data['codex_started']} Codex call(s), "
            f"{data['answered']} answered{stopped}, "
            f"{f['findings']} finding(s): {confirmed} confirmed")
    if f["confirmed_claude_only"]:
        text += f" ({f['confirmed_claude_only']} by Claude's reading only, not proof)"
    text += f", {f['refuted']} refuted, {f['undecidable']} undecided, {f['unjudged']} not judged yet"
    if data["runs_with_unknown_findings"]:
        text += (f"; findings unknown for {data['runs_with_unknown_findings']} answer(s) "
                 "not in the findings format")
    return text + "."


def lines(folder: Path, db_path) -> list[str]:
    """Everything ``modelquad settings`` prints for this folder."""
    folder = Path(folder)
    mode_lines, own_grant = _mode_lines(folder)
    out = ["ModelQuad settings", _host_line(db_path), *mode_lines, _history_line(folder), "",
           "To change them (pick it in /modelquad:settings, or run it in your own terminal):",
           "  ModelQuad off in Claude Code:  modelquad host disconnect claude",
           "  ModelQuad on in Claude Code:   modelquad host connect claude",
           "  Automatic in every folder:     modelquad settings auto on --global --yes"
           " (it shows what that means first)",
           "  Approve each call:             modelquad settings auto off --global"]
    if own_grant:
        out.append(f'                                 and modelquad settings auto off --project "{folder}"')
    out += ["  History in detail:             modelquad review report",
            "  Exclude a folder:              modelquad settings repo off [--project DIR]"
            " (documented in modelquad settings repo --help)"]
    return out
