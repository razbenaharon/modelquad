# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The user's own per-repository setting, and how it combines with the repo file.

``modelquad settings repo <on|off|offer-only|inherit> [--project DIR]`` stores
one word per repository in ``preferences.json`` under ``repos``:

* ``off``: no Codex in this repository. Enforced at the same gates as the
  repository's ``.modelquad.toml`` (closed code ``user_repo_codex_off``).
* ``offer-only``: proactive use off here; Claude offers Codex and calls it
  only when the user agrees or asks.
* ``on``: proactive use on here. This is the user's own choice; the checked-in
  repository file can never turn proactive use on.
* ``inherit``: remove the entry; the global setting applies.

The entry is keyed by the existing repository key (``repo_identity._hash``,
HMAC-SHA256 keyed by this installation's pseudonymous id, over the Git common
folder), so ``preferences.json`` holds no path and no folder name
(``privacy.py``). Every worktree of one repository shares the key. Outside Git
the key is taken over the folder itself. Resetting the installation id
(``modelquad privacy --reset-identity``) unlinks every stored entry.

The key is computed from the file system alone, without starting Git, so the
SessionStart hook can use it within its time budget: the nearest folder holding
``.git``; a ``.git`` file's ``gitdir:`` line and that folder's ``commondir``
are followed the way Git does, and the result is the OS-canonical folder.

Precedence (design memo 5.3), computed in one place, :func:`effective`:

* Safety floors (code) always win; nothing here can relax them.
* Restrictions -- Codex allowed, task kinds, file caps, account -- are the
  most restrictive of every layer: the user's ``off`` and the repo file each
  refuse on their own.
* Preference (who decides to consult Codex): the user's per-repo word beats
  the global ``proactive`` setting; then the repo file's
  ``offers.codex = "on_request"`` may only lower the result.

Imports stay light: the SessionStart hook reads this.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

MODES = ("on", "off", "offer-only")
CHOICES = (*MODES, "inherit")
USER_CODEX_OFF = "user_repo_codex_off"
REASONS = frozenset({USER_CODEX_OFF})

#: Offer modes, from the most to the least active.
PROACTIVE, OFFER, ON_REQUEST, OFF = "proactive", "offer", "on_request", "off"
_RANK = {PROACTIVE: 3, OFFER: 2, ON_REQUEST: 1, OFF: 0}

_REPOS_KEY = "repos"
_KEY = re.compile(r"[0-9a-f]{32}")
_INSTALLATION_KEY = re.compile(r"[0-9a-fA-F]{32}")
_MAX_POINTER_BYTES = 4096
_MAX_ENTRIES = 4096


# ------------------------------------------------------------ the repo key


def _git_marker(directory: Path) -> Path | None:
    """The nearest folder, this one or above, holding a ``.git`` entry.

    The same walk as ``repo_policy._git_marker``, kept here so the hook does
    not import the packer just to find the folder.
    """
    for folder in (directory, *directory.parents):
        try:
            (folder / ".git").lstat()
        except OSError:
            continue
        return folder
    return None


def _small_text(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_POINTER_BYTES:
        raise OSError("not a small plain file")
    with open(path, "rb") as stream:
        return stream.read(_MAX_POINTER_BYTES).decode("utf-8", "strict").strip()


def _git_common_dir(marker: Path) -> Path:
    """The Git common folder of the work tree whose top holds ``.git``."""
    dot = marker / ".git"
    info = dot.lstat()
    if stat.S_ISDIR(info.st_mode):
        git_dir = dot
    else:
        line = _small_text(dot)
        if not line.startswith("gitdir:"):
            raise OSError("not a gitdir pointer")
        target = Path(line[len("gitdir:"):].strip())
        git_dir = target if target.is_absolute() else marker / target
    try:
        common = _small_text(git_dir / "commondir")
    except FileNotFoundError:
        return git_dir
    common_path = Path(common)
    return common_path if common_path.is_absolute() else git_dir / common_path


def repo_key(working_directory, installation_key: str | None) -> str | None:
    """The HMAC key of the repository holding ``working_directory``, or None.

    Never starts a process and never stores anything. None when there is no
    installation id yet or the folder cannot be resolved.
    """
    from .repo_identity import _canonical_directory, _hash

    if type(installation_key) is not str or not _INSTALLATION_KEY.fullmatch(installation_key):
        return None
    try:
        directory = Path(os.path.abspath(working_directory))
        marker = _git_marker(directory)
        key = bytes.fromhex(installation_key)
        if marker is None:
            return _hash(key, "folder", str(_canonical_directory(directory)))
        return _hash(key, "repo", str(_canonical_directory(_git_common_dir(marker))))
    except (OSError, ValueError, UnicodeError):
        return None


# ------------------------------------------------------ preferences.json


def _preferences_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    from .adapters.server_instructions import preferences_path

    return preferences_path()


def _stored(payload: dict) -> dict[str, str]:
    stored = payload.get(_REPOS_KEY)
    if not isinstance(stored, dict):
        return {}
    return {key: value for key, value in stored.items()
            if type(key) is str and _KEY.fullmatch(key) and value in MODES}


def read_modes(path: Path | None = None) -> dict[str, str]:
    """Every stored per-repo word, by key; malformed entries are dropped."""
    from .adapters.server_instructions import _read_preferences

    try:
        return _stored(_read_preferences(_preferences_path(path)))
    except Exception:  # noqa: BLE001 -- an unreadable state folder means nothing was set
        return {}


def read_mode(working_directory, *, path: Path | None = None,
              installation_key: str | None = None, modes: dict | None = None) -> str | None:
    """The user's word for this repository, or None (inherit).

    Reads ``preferences.json`` and, only when it holds an entry, this
    installation's id (never created here).
    """
    modes = read_modes(path) if modes is None else modes
    if not modes:
        return None
    if installation_key is None:
        from .identity import peek_installation_id

        try:
            installation_key = peek_installation_id()
        except Exception:  # noqa: BLE001 -- no id means no entry can match
            return None
    key = repo_key(working_directory, installation_key)
    return modes.get(key) if key else None


def write_mode(working_directory, choice: str, *, path: Path | None = None,
               installation_key: str | None = None) -> str | None:
    """Store (or, with ``inherit``, remove) the word for this repository.

    Returns the stored word, or None after ``inherit``. Raises ValueError for
    an unknown choice or a folder whose key cannot be computed.
    """
    from .adapters.atomicwrite import atomic_write
    from .adapters.server_instructions import _read_preferences

    if choice not in CHOICES:
        raise ValueError(f"Use one of: {', '.join(CHOICES)}.")
    if installation_key is None:
        from .identity import installation_id

        installation_key = installation_id()
    key = repo_key(working_directory, installation_key)
    if key is None:
        raise ValueError("ModelQuad could not identify this folder. Pass an existing project folder.")
    target = _preferences_path(path)
    payload = _read_preferences(target)
    modes = _stored(payload)
    if choice == "inherit":
        modes.pop(key, None)
    else:
        if key not in modes and len(modes) >= _MAX_ENTRIES:
            raise ValueError("Too many per-repository settings are stored. Remove some with inherit first.")
        modes[key] = choice
    if modes:
        payload[_REPOS_KEY] = dict(sorted(modes.items()))
    else:
        payload.pop(_REPOS_KEY, None)
    atomic_write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return None if choice == "inherit" else choice


# ------------------------------------------------------------- precedence


@dataclass(frozen=True)
class Effective:
    """What applies in one repository: the closed refusal and the offer mode."""

    refusal: str | None
    offer: str

    @property
    def codex_allowed(self) -> bool:
        return self.refusal is None


def _lowest(*modes: str) -> str:
    return min(modes, key=_RANK.__getitem__)


def effective(*, global_proactive: bool, user_mode: str | None, policy=None) -> Effective:
    """The one place the layers combine (design memo 5.3).

    ``policy`` is the repository file's ``repo_policy.RepoPolicy`` (None: no
    file). Restrictions are the most restrictive of the layers; the offer
    mode is the user's per-repo word over the global setting, then lowered,
    never raised, by the repository file.
    """
    refusal = None
    if user_mode == "off":
        refusal = USER_CODEX_OFF
    elif policy is not None and policy.refusal is not None:
        refusal = policy.refusal
    elif policy is not None and not policy.allowed:
        refusal = _REPO_FILE_OFF
    if refusal is not None:
        return Effective(refusal, OFF)
    offer = PROACTIVE if global_proactive else OFFER
    if user_mode == "on":
        offer = PROACTIVE
    elif user_mode == "offer-only":
        offer = OFFER
    if policy is not None and policy.offers_codex == "on_request":
        offer = _lowest(offer, ON_REQUEST)
    return Effective(None, offer)


def call_refusal(user_mode: str | None, policy, task_type, *, account_kind=None,
                 check_account=True) -> str | None:
    """The closed code one Codex call is refused for, or None to go on.

    The user's ``off`` first, then the repository file (which also checks the
    task kind and the account rule).
    """
    if user_mode == "off":
        return USER_CODEX_OFF
    return policy.refusal_for(task_type, account_kind=account_kind, check_account=check_account)


USER_OFF_MESSAGE = (
    "You turned Codex off for this repository (modelquad settings repo off), so ModelQuad "
    "started nothing. To allow it here again, run: modelquad settings repo inherit")


# --------------------------------------------------------------- guidance

#: The one advisory line per offer mode, when it differs from the global one.
ADVICE = {
    PROACTIVE: ("ModelQuad: In this repository, you may ask Codex on your own; "
                "Claude Code still asks the user each time."),
    OFFER: ("ModelQuad: In this repository, offer Codex in one short sentence when it would "
            "help, and call it only when the user agrees or asks."),
    ON_REQUEST: ("ModelQuad: In this repository, do not offer Codex on your own; call it "
                 "only when the user asks for it."),
    OFF: "ModelQuad: In this repository, Codex is off. Do not offer or call it.",
}
BROKEN_FILE_ADVICE = (
    "ModelQuad: In this repository, Codex is off until its .modelquad.toml is fixed. "
    "Do not offer or call it.")


REPO_FILE_NAME = ".modelquad.toml"  # repo_policy.FILE_NAME; a test checks they agree
_REPO_FILE_OFF = "repo_policy_codex_off"  # repo_policy.CODEX_OFF; likewise


def advice_line(working_directory, *, preferences: Path | None = None) -> str | None:
    """The SessionStart line for this repository, or None when it matches the default.

    Reads only ``preferences.json``, this installation's id (only when a
    per-repo entry exists), the ``.git`` pointer files and the repository's
    ``.modelquad.toml``. Never starts a process and never writes. The file is
    looked for in the nearest folder holding ``.git`` (the walk the gates use
    when Git cannot answer), so no Git process is needed here.
    """
    from .adapters.server_instructions import _read_preferences

    directory = Path(working_directory)
    payload = _read_preferences(_preferences_path(preferences))
    global_proactive = payload.get("proactive") is True
    modes = _stored(payload)
    user_mode = read_mode(directory, modes=modes) if modes else None
    marker = _git_marker(directory)
    root = marker if marker is not None else directory
    policy = None
    try:
        (root / REPO_FILE_NAME).lstat()
    except FileNotFoundError:
        pass  # No file: nothing more to read, and the packer is not imported.
    except OSError:
        from .repo_policy import UNREADABLE, _broken

        policy = _broken(UNREADABLE, "unreadable")
    else:
        from .repo_policy import load_from_root

        policy = load_from_root(root)
    result = effective(global_proactive=global_proactive, user_mode=user_mode, policy=policy)
    if result.refusal is not None and result.refusal not in (USER_CODEX_OFF, _REPO_FILE_OFF):
        return BROKEN_FILE_ADVICE
    default = PROACTIVE if global_proactive else OFFER
    if result.offer == default:
        return None
    return ADVICE[result.offer]
