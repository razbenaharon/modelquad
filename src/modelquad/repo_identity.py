# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Repository identity keys, computed in memory and never stored here.

Keys are domain-separated HMAC-SHA256 (32 hex characters), keyed by the
caller-supplied 32-hex installation identifier. That identifier is a pseudonym,
not a secret. This module never reads, creates or replaces identity.json, and
it runs no Git command.

What remains here is what project notes use: the ``RepoIdentity`` record, the
OS-canonical directory (junctions, symlinks and Windows letter case resolved,
extended prefix kept) and the keyed hash. The Git-backed resolver that used to
fill a ``RepoIdentity`` had no caller in the product and was removed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
from typing import Literal

_PREFIX = b"modelquad-repo-v1\0"


@dataclass(frozen=True)
class RepoIdentity:
    repo_key: str
    worktree_key: str
    root_fingerprint: str
    remote_fingerprint: str | None
    display_name: str
    branch: str | None
    head_kind: Literal["branch", "detached", "unborn"]


def _canonical_directory(path: Path) -> Path:
    if os.name == "nt":
        # The canonical cwd carries an extended prefix, whose CreateFileW
        # syntax does not interpret '.'/'..'. Resolve Git's relative common
        # directory using normal Win32 lexical semantics before opening it.
        path = Path(os.path.abspath(path))
    if not path.is_dir():
        raise OSError
    if os.name != "nt":
        return Path(os.path.realpath(path, strict=True))

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    final = kernel.GetFinalPathNameByHandleW
    final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Metadata-only access; share read/write/delete, open existing directories.
    handle = create(str(path.absolute()), 0x80, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError
    try:
        size = final(handle, None, 0, 0)
        if not size or size > 32768:
            raise OSError
        buffer = ctypes.create_unicode_buffer(size + 1)
        length = final(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError
        # Keep the OS-returned extended prefix; every alias gets this spelling.
        return Path(buffer.value)
    finally:
        close(handle)


def _hash(key: bytes, label: str, *parts: str) -> str:
    # Length framing prevents ambiguous concatenation of input fields.
    body = _PREFIX + label.encode("ascii") + b"\0"
    for part in parts:
        encoded = part.encode("utf-8", "surrogatepass")
        body += len(encoded).to_bytes(8, "big") + encoded
    return hmac.new(key, body, hashlib.sha256).hexdigest()[:32]
