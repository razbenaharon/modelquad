# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The standing grant: Codex calls without Claude Code's prompt, in folders the user chose.

Claude Code shows its approval prompt on every call to a tool marked
``anthropic/requiresUserInteraction``, and an allow rule cannot skip it. A
user who wants ModelQuad to consult Codex on its own, without a prompt each
time, turns a standing grant on for one repository or for every folder:

    modelquad settings auto on [--project DIR | --global] --yes

ModelQuad then registers ``delegate`` without that marker for a
Claude Code session started in a granted folder, so Claude Code's ordinary
permissions apply: the user's own allow rule (or "don't ask again" on the
first prompt) lets the calls run. ModelQuad never writes that rule.

Every call re-checks the grant for its own folder, and the repository's
choices outrank it: ``modelquad settings repo off [--project DIR]`` or a
``.modelquad.toml`` that turns Codex off refuses the call as before. Turning
the grant off refuses the next call in a session that started with it, with
no other route to Codex. No MCP tool can turn a grant on.

Stored in ``preferences.json`` under ``standing_grant``: ``global`` (bool)
and ``repos`` (the same per-repository keys as ``repo_settings``), plus when
and with which notice version it was turned on.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_KEY = "standing_grant"
NOTICE_VERSION = 1

#: Shown before the grant is turned on. The data-flow notice itself is
#: ``disclosure.DATA_FLOW``; these lines say what the grant changes.
NOTICE = (
    "With the standing grant on, Claude may ask Codex for a read-only review or opinion "
    "without Claude Code's approval prompt for each call, in the folders you grant.",
    "Codex still only reads: it gets the files ModelQuad sends, with secrets redacted, and "
    "cannot change files or run commands.",
    "Claude Code still needs your allow rule for the tool (or \"don't ask again\" on its "
    "first prompt). ModelQuad never writes permission rules.",
    "Turn it off any time with: modelquad settings auto off. A repository set to Codex off, "
    "or a .modelquad.toml that turns Codex off, outranks the grant.",
)

#: Shown after NOTICE when the grant is turned on for every folder.
GLOBAL_NOTICE = (
    "For every folder means every folder you open later too, including an employer's or "
    "a client's code. To keep a folder out, run in it: modelquad settings repo off",
)

#: The allow rule the user adds to Claude Code's settings (permissions.allow):
#: the plugin's id of the review tool, and its id without the plugin. Before
#: 0.5.0 they were mcp__plugin_modelquad_modelquad__modelquad_delegate and
#: mcp__modelquad__modelquad_delegate; an allow rule with an old id no longer
#: matches (`modelquad doctor` names it).
ALLOW_RULE = "mcp__plugin_modelquad_review__delegate"
INSTALL_ALLOW_RULE = "mcp__modelquad__delegate"

NO_GRANT = "no_standing_grant"
NO_GRANT_MESSAGE = (
    "This folder has no standing grant, and this Claude Code session started without "
    "Claude Code's per-call prompt for Codex, so ModelQuad started nothing. Tell the user "
    "plainly: the standing grant is off here, or this project is excluded from it. Only the "
    "user changes that, with /modelquad:settings or by running modelquad settings auto on in "
    "their own terminal. Do not run any command to turn it back on yourself. Otherwise the "
    "user can start a new Claude Code session in this folder; calls there ask them each time.")


def _path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    from .adapters.server_instructions import preferences_path

    return preferences_path()


def _stored(payload: dict) -> dict:
    value = payload.get(_KEY)
    if not isinstance(value, dict):
        return {"global": False, "repos": {}}
    repos = value.get("repos")
    stored = {"global": value.get("global") is True,
              "repos": {key: entry for key, entry in (repos.items() if isinstance(repos, dict) else ())
                        if isinstance(key, str) and isinstance(entry, dict)}}
    if isinstance(value.get(_BINDING), str):
        stored[_BINDING] = value[_BINDING]
    return stored


#: The global grant remembers which installation id it was given under. The
#: user's per-folder exclusions are keyed by that id, so under another id (a
#: reset, a damaged identity file) they no longer match, and a global grant
#: that still counted would cover the folders the user had excluded.
_BINDING = "global_binding"


def _binding(installation_key: str | None) -> str | None:
    from .repo_identity import _hash

    try:
        return _hash(bytes.fromhex(installation_key), "grant-binding")
    except (TypeError, ValueError):
        return None


def _global_holds(stored: dict) -> bool:
    if not stored["global"]:
        return False
    bound = stored.get(_BINDING)
    if bound is None:
        return True  # given before bindings were recorded
    return bound == _binding(_installation_key(create=False))


def _installation_key(create: bool) -> str | None:
    from .identity import peek_installation_id

    try:
        key = peek_installation_id()
    except Exception:  # noqa: BLE001 -- no id: nothing can match
        key = None
    if key is None and create:
        from .identity import installation_id

        key = installation_id()
    return key


def granted(directory, *, path: Path | None = None) -> bool:
    """Whether the user turned the grant on for this folder's repository (or globally).

    Says nothing about whether the repository allows Codex: :func:`allows` does both.
    """
    from .adapters.server_instructions import _read_preferences

    try:
        stored = _stored(_read_preferences(_path(path)))
    except Exception:  # noqa: BLE001 -- an unreadable file grants nothing
        return False
    if _global_holds(stored):
        return True
    return _repository_entry(stored, directory)


def _repository_entry(stored: dict, directory) -> bool:
    from .repo_settings import repo_key

    if not stored["repos"] or directory is None:
        return False
    key = repo_key(directory, _installation_key(create=False))
    return key is not None and key in stored["repos"]


def granted_for_repository(directory, *, path: Path | None = None) -> bool:
    """Whether this folder's repository has its own grant, whatever the global one says.

    ``settings auto off --global`` leaves this one on; ``modelquad settings``
    says so, with the command that turns it off.
    """
    from .adapters.server_instructions import _read_preferences

    try:
        stored = _stored(_read_preferences(_path(path)))
    except Exception:  # noqa: BLE001 -- an unreadable file grants nothing
        return False
    return _repository_entry(stored, directory)


def allows(directory, *, path: Path | None = None) -> bool:
    """A grant for this folder, and nothing in the repository's choices turns Codex off."""
    if directory is None or not granted(directory, path=path):
        return False
    from . import repo_settings
    from .adapters.server_instructions import read_proactive

    try:
        user_mode = repo_settings.read_mode(Path(directory))
        result = repo_settings.effective(global_proactive=read_proactive(), user_mode=user_mode,
                                         policy=repo_settings.local_policy(Path(directory)))
    except Exception:  # noqa: BLE001 -- a choice that cannot be read refuses
        return False
    return result.codex_allowed


def write(on: bool, *, directory=None, everywhere: bool = False, path: Path | None = None) -> str:
    """Turn the grant on or off for one repository or globally. Returns what changed.

    Raises ValueError when the folder's repository key cannot be computed.
    """
    from .adapters.atomicwrite import atomic_write
    from .adapters.server_instructions import preferences_for_write
    from .repo_settings import repo_key

    target = _path(path)
    payload = preferences_for_write(target)
    stored = _stored(payload)
    if everywhere:
        stored["global"] = bool(on)
        stored.pop(_BINDING, None)
        if on:
            bound = _binding(_installation_key(create=True))
            if bound is None:
                raise ValueError("ModelQuad could not read this installation's id.")
            stored[_BINDING] = bound
        scope = "every folder"
    else:
        key = repo_key(directory, _installation_key(create=on))
        if key is None:
            if on:
                raise ValueError("ModelQuad could not identify this folder's repository.")
            return "nothing to turn off"
        if on:
            stored["repos"][key] = {"since": int(time.time()), "notice": NOTICE_VERSION}
        else:
            stored["repos"].pop(key, None)
        scope = "this repository"
    payload[_KEY] = stored
    atomic_write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return scope


OTHER_FOLDER = "outside_session_project"
OTHER_FOLDER_MESSAGE = (
    "This Claude Code session started with the user's standing grant, so its Codex calls "
    "skip Claude Code's prompt, and for that reason they are limited to the folder the "
    "session started in. The folder named here is outside it, so ModelQuad started nothing. "
    "Tell the user plainly; to consult Codex about that folder they can start Claude Code there.")


def session_project(environ=None) -> str:
    """The folder this Claude Code session started in (see :func:`active_at_start`)."""
    import os

    environ = os.environ if environ is None else environ
    return environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def inside_session_project(directory, project) -> bool:
    """Whether ``directory`` is the session's project folder or inside it, links resolved."""
    import os

    try:
        inner = os.path.normcase(os.path.realpath(directory))
        outer = os.path.normcase(os.path.realpath(project))
        return os.path.commonpath((inner, outer)) == outer
    except (OSError, ValueError, TypeError):
        return False


def forget_keyed_settings(*, path: Path | None = None) -> dict:
    """Before the installation id changes: turn every grant off and drop the per-folder words.

    Both are stored under keys made from the id, and no folder path is kept,
    so they cannot be moved to a new id. Left in place, the exclusions would
    stop matching while a global grant went on covering those folders.
    Returns what was removed, for the user.
    """
    from .adapters.atomicwrite import atomic_write
    from .adapters.server_instructions import preferences_for_write
    from . import repo_settings

    target = _path(path)
    payload = preferences_for_write(target)
    stored = _stored(payload)
    modes = repo_settings._stored(payload)
    removed = {"global_grant": stored["global"], "repo_grants": len(stored["repos"]),
               "repo_settings": len(modes)}
    if not any(removed.values()):
        return removed
    payload.pop(_KEY, None)
    payload.pop(repo_settings._REPOS_KEY, None)
    atomic_write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return removed


def active_at_start(environ=None) -> bool:
    """Whether the Claude Code session this server belongs to started in a granted folder.

    Claude Code starts the server in the project folder and sets
    ``CLAUDE_PROJECT_DIR`` (observed 2026-11-07 with Claude Code 2.1.318).
    """
    import os

    environ = os.environ if environ is None else environ
    if environ.get("MODELQUAD_WORKER") == "1" or environ.get("MODELQUAD_NO_STANDING_GRANT") == "1":
        return False
    directory = environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        return allows(directory)
    except Exception:  # noqa: BLE001 -- the server must start; no grant then
        return False
