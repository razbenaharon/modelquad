# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A folder or file path ModelQuad may touch: local, absolute, not a device.

A model-supplied folder such as ``\\\\host\\share`` makes Windows open an SMB
(or WebDAV) connection to that host the moment anything stats the path, and
send the user's NTLM response with it. The connection is made by the OS
redirector, not by a Python socket, so the loopback guard (``netpolicy.py``)
never sees it. Every entry point that takes a folder or a file from outside
checks it here first, before any file-system call:

* refused by text alone, with no file-system access: an empty or non-text
  path, a NUL, a relative or drive-relative path, a path that starts with two
  slashes (``\\\\host\\share``, ``//host/share``, ``\\\\?\\UNC\\...``,
  ``\\\\?\\C:\\...``, ``\\\\.\\device``), an alternate data stream, and a
  reserved device name (``CON``, ``NUL``, ``COM1`` ...) as any part;
* refused by asking Windows what kind of drive the letter is (no file is
  opened): a network drive (``GetDriveTypeW`` reports ``DRIVE_REMOTE``) or a
  letter with no drive behind it;
* refused after an ``lstat`` of each local part, from the drive down: a link
  whose target is a network path. Only the local parts are read, and the
  walk stops at the first link, so nothing remote is ever opened.

Imports stay light: the review hook calls this.
"""

from __future__ import annotations

import ntpath
import os
import stat

UNUSABLE = "project_directory_unusable"

_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$",
     *(f"com{index}" for index in "123456789\u00b9\u00b2\u00b3"),
     *(f"lpt{index}" for index in "123456789\u00b9\u00b2\u00b3")})
_DRIVE_UNKNOWN, _DRIVE_NO_ROOT_DIR, _DRIVE_REMOTE = 0, 1, 4
_MAX_PATH_CHARS = 4096


def _remote_text(path: str) -> bool:
    """A link target or pointer that names another machine (or a device)."""
    text = path.strip().replace("/", "\\")
    return text.startswith("\\\\") and not _extended_local(text)


def _extended_local(text: str) -> bool:
    # ``\\?\C:\...`` (how Windows reports a local link target) is local;
    # ``\\?\UNC\...`` and ``\\.\...`` are not.
    rest = text[4:] if text.startswith("\\\\?\\") else None
    return (rest is not None and len(rest) >= 3 and rest[0].isalpha()
            and rest[1] == ":" and rest[2] == "\\")


def text_reason(path) -> str | None:
    """Why ``path`` may not be touched, judged by its text alone; None when it may.

    Pure: no file-system access.
    """
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if type(path) is not str:
        return "not_a_path"
    if not path or len(path) > _MAX_PATH_CHARS or any(ord(char) < 32 for char in path):
        return "invalid_path"
    normalized = path.replace("/", "\\")
    if normalized.startswith("\\\\"):
        return "network_or_device_path"
    if os.name != "nt":
        return None if os.path.isabs(path) else "not_absolute"
    drive, tail = ntpath.splitdrive(normalized)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isascii() or not drive[0].isalpha():
        return "not_absolute"
    if not tail.startswith("\\"):
        return "not_absolute"
    if ":" in tail:
        return "invalid_path"
    for part in tail.split("\\"):
        if part.split(".")[0].rstrip(" ").casefold() in _DEVICE_NAMES:
            return "device_path"
    return None


def _drive_reason(path: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        kind = ctypes.windll.kernel32.GetDriveTypeW(ntpath.splitdrive(path)[0] + "\\")
    except Exception:  # noqa: BLE001 -- a drive whose kind cannot be read is not trusted
        return "drive_unknown"
    if kind == _DRIVE_REMOTE:
        return "network_drive"
    if kind in (_DRIVE_UNKNOWN, _DRIVE_NO_ROOT_DIR):
        return "drive_unknown"
    return None


_MAX_LINK_HOPS = 16
#: IO_REPARSE_TAG_SYMLINK and IO_REPARSE_TAG_MOUNT_POINT (a junction).
_LINK_TAGS = frozenset({0xA000000C, 0xA0000003})


def _split(path: str) -> tuple[str, list[str]]:
    if os.name != "nt":
        return os.sep, [piece for piece in path.split(os.sep) if piece]
    drive, tail = ntpath.splitdrive(path.replace("/", "\\"))
    return drive + "\\", [piece for piece in tail.split("\\") if piece]


def _link_reason(path: str) -> str | None:
    """A link on the way down whose target is remote; reads only local parts.

    Each part is ``lstat``-ed from the drive down. A link is never followed by
    the file system: its target is read as text, checked, and the walk goes
    on from that target, so a remote target is refused before it is opened.
    """
    current, pending = _split(path)
    hops = 0
    while pending:
        part = pending.pop(0)
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return None  # Nothing further exists: nothing further can link anywhere.
        except OSError:
            return "unreadable"
        # Only links lead elsewhere: a symbolic link or a junction. Other
        # reparse points (OneDrive's cloud folders, deduplicated files) stay
        # where they are and are walked like plain folders.
        tag = getattr(info, "st_reparse_tag", 0)
        if not (stat.S_ISLNK(info.st_mode) or tag in _LINK_TAGS):
            continue
        hops += 1
        if hops > _MAX_LINK_HOPS:
            return "too_many_links"
        try:
            target = os.readlink(current)
        except OSError:
            # A reparse point that is not a link (a cloud placeholder, a
            # dedup file) has no target to read: refuse rather than follow.
            return "link_unreadable"
        if _remote_text(target):
            return "links_to_network"
        if target.startswith("\\\\?\\"):
            target = target[4:]
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        if text_reason(target) is not None or _drive_reason(target) is not None:
            return "links_to_network"
        current, rest = _split(target)
        pending = rest + pending
    return None


def reason(path) -> str | None:
    """Why ``path`` may not be touched, or None. Text first, then the drive, then links."""
    found = text_reason(path)
    if found is not None:
        return found
    path = os.fspath(path)
    return _drive_reason(path) or _link_reason(path)


def is_local(path) -> bool:
    return reason(path) is None


def remote_pointer(text: str) -> bool:
    """A ``gitdir:`` or ``commondir`` value that points at another machine or a device."""
    return _remote_text(text)
