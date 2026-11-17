# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Resolve executable names without implicit current-directory searches.

Resolution identifies a file; it grants no launch authority. ``Resolution.path``
is a canonical absolute path for immediate use, never persisted as evidence.
Refusals and rejected-entry diagnostics contain bounded reason codes only.

Out of scope: swapping files or junctions between resolution and launch, hard
links, DLL planting into a child, and executable authenticity. Callers retain
their own launch authorization and any stronger admission/hash guarantees.
This module performs no probes or process launches, including at import time.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PureWindowsPath


SECURITY_REFUSALS = frozenset({
    "not_bare_name", "path_empty", "path_quoted", "path_device",
    "path_relative", "path_drive_relative", "path_root_relative",
    "invalid_path", "path_unreadable", "cwd_unknown", "excluded_cwd",
    "excluded_root", "extension_not_allowed", "not_regular_file",
    "not_executable", "batch_script", "not_a_system_utility",
    "system_api_failed", "unsupported_platform", "empty_argv",
})
_WINDOWS_EXTENSIONS = frozenset({".COM", ".EXE", ".BAT", ".CMD"})
_SYSTEM_UTILITIES = frozenset({"tasklist", "taskkill", "schtasks", "cmd", "ping"})


@dataclass(frozen=True)
class Resolution:
    """An immediate-use path, omitted from repr and never persisted as evidence."""

    path: str | None = field(repr=False)
    source: str
    kind: str | None
    refusal: str | None
    rejected: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rejected", tuple(dict.fromkeys(self.rejected))[:16])


class UnsafeExecutable(Exception):
    """A launch refusal whose message/args carry only a reason code."""

    def __init__(self, reason: str) -> None:
        # Do not let an accidental caller-supplied path become exception text.
        self.reason = reason if reason in SECURITY_REFUSALS or reason == "not_found" else "invalid_path"
        super().__init__(self.reason)


def _refused(
    source: str, reason: str, rejected: Sequence[str] = (), *, kind: str | None = None,
) -> Resolution:
    return Resolution(None, source, kind, reason, tuple(rejected))


def _path_reason(path: str) -> str | None:
    if not path:
        return "path_empty"
    if "\x00" in path:
        return "invalid_path"
    if '"' in path:
        return "path_quoted"
    if os.name == "nt":
        normalized = path.replace("/", "\\")
        if normalized.startswith(("\\\\.\\", "\\\\?\\")):
            return "path_device"
        drive, tail = ntpath.splitdrive(path)
        if not PureWindowsPath(path).is_absolute():
            if drive:
                return "path_drive_relative"
            if tail.startswith(("\\", "/")):
                return "path_root_relative"
            return "path_relative"
        # Refuse alternate data streams (allow only the drive's colon).
        if ":" in tail or ":" in drive[2:]:
            return "invalid_path"
    elif not os.path.isabs(path):
        return "path_relative"
    return None


def _canonical(path: str) -> str:
    return os.path.realpath(path)


def _comparison(path: str) -> str:
    return os.path.normcase(_canonical(path))


def _exclusions(
    exclude_roots: Sequence[str | os.PathLike[str]], cwd: str | os.PathLike[str] | None,
) -> tuple[str, tuple[str, ...]]:
    # The process CWD is always included, even when the child has another CWD.
    current = _comparison(os.getcwd())
    roots = tuple(_comparison(os.fspath(root)) for root in exclude_roots)
    if cwd is not None:
        roots += (_comparison(os.fspath(cwd)),)
    return current, roots


def _excluded(path: str, exclusions: tuple[str, tuple[str, ...]]) -> str | None:
    current, roots = exclusions
    # Callers supply either a canonical file or a canonical directory joined
    # with its original basename. Do not resolve the latter file link again:
    # both the original search directory and the final target are excluded.
    candidate = os.path.normcase(path)
    if os.path.dirname(candidate) == current:
        return "excluded_cwd"
    for root in roots:
        try:
            if os.path.commonpath((candidate, root)) == root:
                return "excluded_root"
        except ValueError:
            # Different Windows drives have no common ancestor.
            continue
    return None


def _kind(path: str) -> tuple[str | None, str | None]:
    if os.name != "nt":
        return "binary", None
    if path.endswith((".", " ")):
        return None, "extension_not_allowed"
    extension = ntpath.splitext(path)[1].upper()
    if extension not in _WINDOWS_EXTENSIONS:
        return None, "extension_not_allowed"
    return ("batch" if extension in {".BAT", ".CMD"} else "binary"), None


def _admit(
    path: str, source: str, exclusions: tuple[str, tuple[str, ...]], allow_batch: bool,
) -> Resolution:
    reason = _path_reason(path)
    if reason:
        return _refused(source, reason)
    kind, reason = _kind(path)
    if reason:
        return _refused(source, reason)
    try:
        details = os.stat(path)  # Follow file symlinks, as realpath does.
        canonical = _canonical(path)
        reason = _path_reason(canonical)
        if reason:
            return _refused(source, reason)
        # Check both the search location and the matched file's link target.
        search_location = os.path.join(_canonical(os.path.dirname(path)), os.path.basename(path))
        reason = _excluded(search_location, exclusions)
        reason = reason or _excluded(canonical, exclusions)
        if reason:
            return _refused(source, reason, kind=kind)
        if not stat.S_ISREG(details.st_mode):
            return _refused(source, "not_regular_file")
        kind, reason = _kind(canonical)
        if reason:
            return _refused(source, reason)
        if kind == "batch" and not allow_batch:
            return _refused(source, "batch_script", kind=kind)
        if os.name != "nt" and not details.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            return _refused(source, "not_executable", kind=kind)
    except (FileNotFoundError, NotADirectoryError):
        return _refused(source, "not_found")
    except (OSError, ValueError):
        return _refused(source, "path_unreadable")
    return Resolution(canonical, source, kind, None)


def admit_executable(
    path: str | os.PathLike[str], *,
    exclude_roots: Sequence[str | os.PathLike[str]] = (),
    cwd: str | os.PathLike[str] | None = None,
    allow_batch: bool = False,
) -> Resolution:
    """Vet one explicit candidate without making relative input absolute."""
    try:
        exclusions = _exclusions(exclude_roots, cwd)
    except (OSError, ValueError):
        return _refused("explicit", "cwd_unknown")
    return _admit(os.fspath(path), "explicit", exclusions, allow_batch)


def _bare_name(name: str) -> bool:
    return bool(name) and not any(char in name for char in ("/", "\\", ":", "\x00"))


def _names(name: str, pathext: str | None) -> tuple[str, ...] | None:
    if os.name != "nt":
        return (name,)
    if name.endswith((".", " ")):
        return None
    extension = ntpath.splitext(name)[1].upper()
    if extension:
        return (name,) if extension in _WINDOWS_EXTENSIONS else None
    extensions = (os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD") if pathext is None else pathext)
    return tuple(name + ext for ext in dict.fromkeys(extensions.upper().split(";")) if ext in _WINDOWS_EXTENSIONS)


def resolve_executable(
    name: str, *,
    path_env: str | None = None,
    pathext: str | None = None,
    exclude_roots: Sequence[str | os.PathLike[str]] = (),
    cwd: str | os.PathLike[str] | None = None,
    allow_batch: bool = False,
) -> Resolution:
    """Search PATH in order; never insert CWD or admit a relative PATH entry.

    Rejected entries are inspected only for existence, to distinguish a safety
    refusal from absence. A later admitted hit can succeed despite earlier
    rejections; otherwise the first matching candidate's safety reason wins.
    """
    if not _bare_name(name):
        return _refused("path", "not_bare_name")
    names = _names(name, pathext)
    if names is None:
        return _refused("path", "extension_not_allowed")
    try:
        exclusions = _exclusions(exclude_roots, cwd)
    except (OSError, ValueError):
        return _refused("path", "cwd_unknown")
    rejected: list[str] = []
    first_refusal: Resolution | None = None

    def remember(reason: str) -> None:
        if reason not in rejected and len(rejected) < 16:
            rejected.append(reason)

    search = os.environ.get("PATH", "") if path_env is None else path_env
    for entry in search.split(os.pathsep):
        entry_reason = _path_reason(entry)
        if entry_reason:
            remember(entry_reason)
        # Quotes are not trusted syntax, but strip them for existence evidence.
        inspection_entry = entry.strip('"') if entry_reason == "path_quoted" else entry
        for filename in names:
            candidate = os.path.join(inspection_entry, filename)
            if entry_reason:
                try:
                    os.stat(candidate)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except (OSError, ValueError):
                    # An unreadable/invalid location is not evidence of absence.
                    result = _refused("path", "path_unreadable")
                else:
                    result = _refused("path", entry_reason)
            else:
                result = _admit(candidate, "path", exclusions, allow_batch)
            if result.path is not None:
                return Resolution(result.path, "path", result.kind, None, tuple(rejected))
            if result.refusal != "not_found":
                remember(result.refusal)
                if first_refusal is None:
                    first_refusal = result
    if first_refusal is not None:
        return _refused("path", first_refusal.refusal, rejected, kind=first_refusal.kind)
    return _refused("path", "not_found", rejected)


def safe_which(name: str, **same_keywords: object) -> str | None:
    """Discovery facade using the exact resolve_executable keyword contract."""
    return resolve_executable(name, **same_keywords).path


def _system_directory() -> str | None:
    """Read Windows' system directory at call time, with one bounded retry."""
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_directory = kernel32.GetSystemDirectoryW
        get_directory.argtypes = (wintypes.LPWSTR, wintypes.UINT)
        get_directory.restype = wintypes.UINT
        size = 260
        for _ in range(2):
            buffer = ctypes.create_unicode_buffer(size)
            count = get_directory(buffer, size)
            if count == 0:
                return None
            if count < size:
                return buffer.value
            size = count  # API's required size includes the terminating NUL.
    except (OSError, AttributeError, ValueError):
        return None
    return None


def system_executable(name: str) -> Resolution:
    """Resolve only the pinned OS utilities; never consult environment roots."""
    utility = name.lower()
    if utility.endswith(".exe"):
        utility = utility[:-4]
    if utility not in _SYSTEM_UTILITIES:
        return _refused("system", "not_a_system_utility")
    if os.name != "nt":
        return _refused("system", "unsupported_platform")
    try:
        directory = _system_directory()
        if directory is None or _path_reason(directory):
            return _refused("system", "system_api_failed")
    except (OSError, ValueError):
        return _refused("system", "system_api_failed")
    # The location comes from the OS, not from a search, so the current
    # directory is not excluded here: a scheduled task or service often starts
    # in System32 itself, and its utilities must still resolve.
    no_exclusions = ("", ())
    result = _admit(os.path.join(directory, utility + ".exe"), "system", no_exclusions, False)
    if result.path is None:
        return _refused("system", "system_api_failed")
    return result


def prepare_launch(
    argv: Sequence[str], *,
    exclude_roots: Sequence[str | os.PathLike[str]] = (),
    cwd: str | os.PathLike[str] | None = None,
    allow_batch: bool = False,
) -> list[str]:
    """Pin argv[0]; arguments are unchanged and launch authority remains outside.

    Batch opt-in admits a filename only. Callers must separately establish safe
    batch handling, including a pinned command processor and argument policy.
    """
    if not argv:
        raise UnsafeExecutable("empty_argv")
    resolve = resolve_executable if _bare_name(argv[0]) else admit_executable
    result = resolve(argv[0], exclude_roots=exclude_roots, cwd=cwd, allow_batch=allow_batch)
    if result.path is None:
        raise UnsafeExecutable(result.refusal or "not_found")
    return [result.path, *argv[1:]]


def safe_child_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment, suppress implicit CWD search, and filter PATH."""
    result = dict(env)
    # Windows environment names are case-insensitive; avoid duplicate spellings.
    for key in tuple(result):
        if key == "PATH" or (os.name == "nt" and key.upper() == "PATH"):
            result[key] = os.pathsep.join(
                entry for entry in result[key].split(os.pathsep) if _path_reason(entry) is None
            )
        if key == "NoDefaultCurrentDirectoryInExePath" or (os.name == "nt" and key.upper() == "NODEFAULTCURRENTDIRECTORYINEXEPATH"):
            del result[key]
    result["NoDefaultCurrentDirectoryInExePath"] = "1"
    return result
