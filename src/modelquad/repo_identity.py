# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Read-only repository identity, computed in memory and never stored here.

Keys are domain-separated HMAC-SHA256 (32 hex characters), keyed by the
caller-supplied 32-hex installation identifier. That identifier is a pseudonym,
not a secret: keys are scoped to one installation's state, and copying that
state carries the scope. Resetting the identifier changes every key. This
module never reads, creates or replaces identity.json.

The repo key binds ONLY the OS-canonical common Git directory. Linked worktrees
share it, including orphan/unborn branches; separate clones do not.
The separate root fingerprint binds sorted root-commit SHAs reachable from ALL
local refs (``git rev-list --max-parents=0 --all``), or literal ``unborn`` when
there are none. Shallow-clone boundary commits count as roots; deepening can
change this fingerprint. A different project re-cloned at the same path keeps
its repo key but has a different root fingerprint.
Moving a folder changes its key: the safe default is to register again.
Canonicalization resolves junctions/symlinks and actual Windows letter case,
including extended paths, without case-folding distinct case-sensitive dirs.
On macOS, realpath does not normalize letter case: case aliases can split keys,
which is the safe direction (require registration rather than merge grants).
A submodule is its own repository. The worktree key additionally binds the
canonical checkout root. Branch and HEAD kind are separate facts, never key
inputs; HEAD SHA is not returned. Display names disclose only the checkout's
leaf name (or drive, e.g. ``C:``, at a Windows drive root). Raw paths, URLs,
credentials and installation ids are never returned.

Origin is a separate normalized, credential-stripped fingerprint, not a repo
key input. An unparseable origin yields None. Binding requirement: a grant
registered under one remote fingerprint must not be reused after it changes or
disappears; require explicit reactivation. A changed root_fingerprint likewise
requires explicit reactivation ("a different project at the same path").
This module grants nothing. Future persistence requires privacy,
and retention/delete reconciliation in a later change.

Git observations reuse jobs.changes._git, including its bounded time/output,
closed stdin, no-window flags, no lazy fetch/optional locks/hooks/fsmonitor and
removal of EVERY inherited GIT_* variable (including GIT_CEILING_DIRECTORIES).
Only Git metadata commands run: no working-tree walks or clean filters.
Ownership refusals are never bypassed. The current shared runner discards
stderr and collapses unsuccessful exits into _Unavailable, so ownership
refusals receive generic git_unavailable under a recorded
waiver. A bounded stderr category may be added to the shared runner later.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from modelquad.jobs.changes import GIT_SECONDS, _git, _Unavailable

RESOLVE_SECONDS = 20
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


@dataclass(frozen=True)
class Unknown:
    reason: str


class _Failure(Exception):
    pass


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


def _normalize_remote(raw: str) -> str:
    raw = raw.strip()
    if not raw or "\0" in raw or "\n" in raw or "\r" in raw:
        raise ValueError
    if "://" in raw:
        url = urlsplit(raw)
        host = url.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        port = url.port
        scheme = url.scheme.lower()
        if port is not None and (scheme, port) not in {
            ("https", 443), ("http", 80), ("ssh", 22), ("git", 9418)
        }:
            host += ":" + str(port)
        path = url.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        # Userinfo, query and fragment may all carry credentials; drop them.
        return urlunsplit((scheme, host.lower(), path, "", ""))
    # SCP-style SSH URLs; do not mistake a Windows drive for an SSH host.
    scp = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", raw)
    if scp and not re.match(r"^[A-Za-z]:[\\/]", raw):
        host, path = scp.groups()
        path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return "ssh://" + host.lower() + "/" + path
    return raw.replace("\\", "/").rstrip("/")


def _display_name(root: Path) -> str:
    if root.name:
        return root.name
    if re.fullmatch(r"(?:\\\\\?\\)?[A-Za-z]:", root.drive):
        return root.drive[-2:]
    return "(root)"


def resolve_repo_identity(path: str | Path, *, installation_key: str) -> RepoIdentity | Unknown:
    """Resolve one existing checkout without writes, network or guessed keys.

    The required installation key uses identity.py's UUID-hex format. Failures
    expose fixed labels only; subprocess bodies and paths never become reasons.
    """
    if not isinstance(installation_key, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", installation_key):
        return Unknown("installation_key_unavailable")
    key = bytes.fromhex(installation_key)
    deadline = time.monotonic() + RESOLVE_SECONDS

    def observe(directory: Path, *args: str) -> str:
        call_deadline = min(deadline, time.monotonic() + GIT_SECONDS)
        try:
            return _git(directory, list(args), call_deadline).decode("utf-8", "strict").rstrip("\n")
        except FileNotFoundError:
            raise _Failure("git_missing") from None
        except subprocess.TimeoutExpired:
            raise _Failure("git_timeout") from None
        except _Unavailable:
            reason = "git_timeout" if time.monotonic() >= call_deadline else "git_unavailable"
            raise _Failure(reason) from None
        except (OSError, UnicodeError):
            raise _Failure("git_unavailable") from None

    try:
        directory = _canonical_directory(Path(path))
    except (OSError, ValueError, TypeError, RuntimeError):
        return Unknown("path_unavailable")
    try:
        if observe(directory, "rev-parse", "--is-bare-repository") == "true":
            return Unknown("bare_repository")
        root = _canonical_directory(Path(observe(directory, "rev-parse", "--show-toplevel")))
        common_path = Path(observe(directory, "rev-parse", "--git-common-dir"))
        # Git resolves relative common-dir paths against its cwd, not root.
        common = _canonical_directory(directory / common_path)
        with os.scandir(common):
            pass  # Confirm readability, without collecting directory entries.
        # These two quiet queries normally exit nonzero for detached and
        # unborn HEAD respectively. The shared runner exposes that as
        # git_unavailable; missing Git and timeouts must still propagate.
        def head_query(*args: str) -> str | None:
            try:
                return observe(root, *args)
            except _Failure as failure:
                if failure.args[0] != "git_unavailable":
                    raise
                return None

        branch = head_query("symbolic-ref", "-q", "--short", "HEAD")
        oid = head_query("rev-parse", "-q", "--verify", "HEAD")
        if branch == "" or (oid is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)):
            raise _Failure("git_unavailable")
        if oid is None:
            if branch is None:
                raise _Failure("git_unavailable")
            # _Unavailable does not distinguish an expected nonzero exit
            # from a bounded-output/read failure. Corroborate absence of the
            # branch ref instead of guessing unborn when a real ref is broken.
            full_branch = observe(root, "symbolic-ref", "-q", "HEAD")
            if not full_branch.startswith("refs/heads/") or observe(
                root, "for-each-ref", "--format=%(refname)", full_branch
            ):
                raise _Failure("git_unavailable")
            kind = "unborn"
        else:
            if branch is None and observe(root, "rev-parse", "--symbolic-full-name", "HEAD") != "HEAD":
                raise _Failure("git_unavailable")
            kind = "detached" if branch is None else "branch"
        commits = observe(root, "rev-list", "--max-parents=0", "--all").splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", c) for c in commits):
            raise _Failure("git_unavailable")
        roots = "\n".join(sorted(commits)) if commits else "unborn"
        remotes = observe(root, "remote").splitlines()
        fingerprint = None
        if "origin" in remotes:
            raw_remote = observe(root, "remote", "get-url", "origin")
            try:
                normalized = _normalize_remote(raw_remote)
            except ValueError:
                pass  # A malformed origin is unavailable, not a lost repo.
            else:
                fingerprint = _hash(key, "remote", normalized)
        if time.monotonic() >= deadline:
            raise _Failure("git_timeout")
        return RepoIdentity(
            repo_key=_hash(key, "repo", str(common)),
            worktree_key=_hash(key, "worktree", str(common), str(root)),
            root_fingerprint=_hash(key, "root", roots),
            remote_fingerprint=fingerprint, display_name=_display_name(root),
            branch=branch, head_kind=kind,
        )
    except _Failure as failure:
        return Unknown(failure.args[0])
    except (OSError, ValueError, TypeError, RuntimeError):
        return Unknown("repository_unavailable")
