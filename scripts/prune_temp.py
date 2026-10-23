#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Prune known test leftovers, or explicitly selected per-run roots.

Stdlib only. Dry-run is the CLI default; no ambient system-temp fallback.
Links and Windows reparse points are never traversed. Top-level links are
kept; links inside an eligible tree are removed as links, without opening
their targets. Every traversal and deletion checks the cooperative deadline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path, PureWindowsPath
import shlex
import stat
import sys
import time

LEAK_PREFIXES = (
    "ns-logs-", "ns-e2e-", "mq-wt-", "mq-integrate-",
    "modelquad-gh-no-authority-", "modelquad-release-state-",
    "mao-remove-",
)
TWO_DAYS = 2 * 24 * 60 * 60
_WINDOWS = os.name == "nt"
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def _link_text(path):
    """The link's target text, without Windows' extended-length prefix.

    On Windows `os.readlink` returns an absolute target spelled with the
    extended-length prefix (for a drive or a UNC share). Containment is
    compared lexically, and to `Path.is_relative_to` that prefixed drive is a
    different drive, so an internal target would look external. Only the
    prefix is rewritten; the target is never resolved, so no link is followed
    before containment is established.
    """
    text = os.readlink(path)
    if _WINDOWS:
        if text[:len(_EXTENDED_UNC)].upper() == _EXTENDED_UNC.upper():
            return "\\\\" + text[len(_EXTENDED_UNC):]
        if (text.startswith(_EXTENDED) and text[4:5].isalpha()
                and text[5:7] == ":\\"):
            return text[len(_EXTENDED):]
    return text


def _contains(child, target):
    r"""Whether `target` lies lexically inside `child`, with exact case.

    Windows `Path.is_relative_to` compares components without regard to case,
    but a directory can have case sensitivity enabled, and there `E:\Runs`
    and `E:\runs` are different trees. A prefix that differs in case is
    treated as outside, before any metadata of the target is read. Drive
    letters are always case-insensitive, so only a drive-letter anchor such
    as `E:\` is compared folded; a UNC anchor such as `\\server\share\` is
    compared exactly, like every other component.
    """
    if not target.is_relative_to(child):
        return False
    inner = target.parts[:len(child.parts)]
    if isinstance(child, PureWindowsPath) and len(child.drive) == 2:
        return (inner[0].casefold() == child.parts[0].casefold()
                and inner[1:] == child.parts[1:])
    return inner == child.parts


class _BudgetExpired(Exception):
    pass


class _KeepRun(Exception):
    def __init__(self, reason):
        self.reason = reason


def _is_link(path, info):
    junction = getattr(path, "is_junction", None)
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        or bool(junction and junction())
    )


def _safe_root(root):
    # Do not resolve() away a link before checking it. A linked ancestor is
    # equally unsafe: the spelling would hide the actual directory being pruned.
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        if _is_link(ancestor, info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Root must be a directory with no linked ancestors: {path}")
    return path


def _checked_info(path, root, tick):
    tick()
    if path == root or not path.is_relative_to(root):
        raise ValueError("Entry escaped the pruning root")
    # Recheck ancestors immediately before accessing the entry. A nested link
    # is never opened, even if a directory was replaced after enumeration.
    for parent in path.parents:
        tick()
        info = parent.lstat()
        if _is_link(parent, info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("An entry's ancestor became a link or ceased to be a directory")
        if parent == root:
            break
    return path.lstat()


def _walk(path, root, tick, depth=0):
    """Yield metadata pre-order, with open scans bounded by directory depth."""
    info = _checked_info(path, root, tick)
    linked = _is_link(path, info)
    yield path, info, linked, depth
    if stat.S_ISDIR(info.st_mode) and not linked:
        with os.scandir(path) as children:
            for child in children:
                tick()
                yield from _walk(Path(child.path), root, tick, depth + 1)


def _entry_timestamp(info):
    """Prefix eligibility only: creation age survives partial tree removal."""
    # Deleting children updates a directory's mtime. Creation time lets the
    # next budgeted pass resume immediately instead of waiting another two days.
    birthtime = getattr(info, "st_birthtime", None)
    if birthtime is not None:
        return birthtime
    return info.st_ctime if _WINDOWS else info.st_mtime


def _unlink_file(path, root, tick):
    try:
        path.unlink()
    except PermissionError:
        latest = _checked_info(path, root, tick)
        if _is_link(path, latest) or not stat.S_ISREG(latest.st_mode):
            raise
        readonly = (
            not latest.st_mode & stat.S_IWUSR
            or getattr(latest, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY
        )
        if not readonly:
            raise
        # Python 3.11 on Windows cannot chmod with follow_symlinks=False.
        # The fallback is used only for a freshly checked regular file.
        options = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
        tick()
        os.chmod(path, latest.st_mode | stat.S_IWUSR, **options)
        latest = _checked_info(path, root, tick)
        if _is_link(path, latest) or not stat.S_ISREG(latest.st_mode):
            raise OSError("File became a link or ceased to be regular during pruning")
        path.unlink()  # One retry only; a genuinely locked file stays counted.


def _remove(path, root, tick, removed):
    info = _checked_info(path, root, tick)
    if _is_link(path, info):
        # Directory junctions use rmdir, which removes the junction itself.
        # Directory symlinks use unlink (also supported by Python on Windows).
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            path.unlink()
        else:
            path.rmdir()
    elif stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as children:
            for child in children:
                tick()
                _remove(Path(child.path), root, tick, removed)
        latest = _checked_info(path, root, tick)
        if _is_link(path, latest):
            raise OSError("Directory became a link during pruning")
        path.rmdir()
    else:
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Refusing a non-regular entry")
        _unlink_file(path, root, tick)
        removed[0] += info.st_size


def _git_guard(child, path, info, linked, tick):
    if path.name == "worktrees" and path.parent.name == ".git" and (
        linked or stat.S_ISDIR(info.st_mode)
    ):
        raise _KeepRun("kept_because_worktree")
    if path.name == ".git" and linked:
        raise _KeepRun("kept_because_worktree")
    if path.name == ".git" and stat.S_ISREG(info.st_mode) and not linked:
        tick()
        with path.open(encoding="utf-8") as handle:
            line = handle.readline(4096).strip()
        if line.startswith("gitdir:"):
            gitdir = Path(line.partition(":")[2].strip())
            target = Path(os.path.abspath(path.parent / gitdir))
            if not _contains(child, target):
                raise _KeepRun("kept_because_worktree")
            # A lexical in-tree pointer can still escape through a link.
            # Inspect ancestors, without resolving or opening the target.
            for ancestor in (target, *target.parents):
                tick()
                try:
                    target_info = ancestor.lstat()
                except FileNotFoundError:
                    continue
                if _is_link(ancestor, target_info):
                    raise _KeepRun("kept_because_worktree")
                if ancestor == child:
                    break
        else:
            # An unrecognised git marker is not permission to delete a checkout.
            raise _KeepRun("kept_because_worktree")


def _run_guard(child, path, info, linked, depth, cutoff, tick):
    # Run roots use modification time for every activity guard, including
    # locks and current-link targets. Creation time is not evidence of activity.
    # Do not use _entry_timestamp here: a recreated link can point at a stale run.
    if depth <= 2 and info.st_mtime >= cutoff:
        raise _KeepRun("kept_because_young")
    if path.name == ".lock" and not linked and info.st_mtime >= cutoff:
        raise _KeepRun("kept_because_active")
    _git_guard(child, path, info, linked, tick)
    if path.name == "pytest-current" and linked:
        tick()
        # Inspect the link text first; an external target is never stat'ed.
        target = Path(os.path.abspath(path.parent / _link_text(path)))
        if not _contains(child, target):
            raise _KeepRun("kept_because_active")
        try:
            target_info = _checked_info(target, child.parent, tick)
        except FileNotFoundError:
            # A missing target does not prove the run is idle: pytest may be
            # recreating it right now, or an ancestor vanished mid-check. Keep
            # the run (counted as active, not as a failure) rather than risk
            # deleting a directory that is about to be live again.
            raise _KeepRun("kept_because_active")
        if _is_link(target, target_info):
            raise _KeepRun("kept_because_active")
        if stat.S_ISDIR(target_info.st_mode) and target_info.st_mtime >= cutoff:
            raise _KeepRun("kept_because_active")


def _prune(root, *, now, min_age_seconds, apply, prefixes, protect, runs,
           time_budget_seconds):
    for value in (now, min_age_seconds):
        if not math.isfinite(value):
            raise ValueError("Age and time must be finite")
    if min_age_seconds < 0 or (time_budget_seconds is not None and (
        not math.isfinite(time_budget_seconds) or time_budget_seconds < 0
    )):
        raise ValueError("Age and budget must be non-negative")
    root = _safe_root(root)
    # The caller can narrow the known prefixes, never widen them.
    prefixes = tuple(prefixes)
    if any(prefix not in LEAK_PREFIXES for prefix in prefixes):
        raise ValueError("Only known leak prefixes may be pruned")
    protect = {name.casefold() if _WINDOWS else name for name in protect}
    deadline = None if time_budget_seconds is None else time.monotonic() + time_budget_seconds
    report = {
        "apply": apply, "per_prefix": {}, "eligible": 0, "bytes": 0,
        "deleted": 0, "deleted_bytes": 0, "kept_because_young": 0,
        "kept_because_active": 0, "kept_because_worktree": 0,
        "kept_because_protected": 0, "skipped_links": 0,
        "failed": 0, "budget_stopped": False,
    }

    def tick():
        if deadline is not None and time.monotonic() >= deadline:
            raise _BudgetExpired()

    cutoff = now - min_age_seconds
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                tick()
                prefix = "run-roots" if runs else next(
                    (p for p in prefixes if entry.name.startswith(p)), None
                )
                if prefix is None:
                    continue
                counts = report["per_prefix"].setdefault(prefix, {
                    "eligible": 0, "bytes": 0, "deleted": 0, "deleted_bytes": 0,
                    "kept_because_young": 0, "failed": 0,
                })
                path = Path(entry.path)
                removed = [0]
                try:
                    info = _checked_info(path, root, tick)
                    if _is_link(path, info):
                        report["skipped_links"] += 1
                        continue
                    if runs:
                        if not stat.S_ISDIR(info.st_mode):
                            continue
                        if (entry.name.casefold() if _WINDOWS else entry.name) in protect:
                            raise _KeepRun("kept_because_protected")
                    elif _entry_timestamp(info) >= cutoff:
                        raise _KeepRun("kept_because_young")
                    size = 0
                    for node, metadata, linked, depth in _walk(path, root, tick):
                        if runs:
                            _run_guard(path, node, metadata, linked, depth, cutoff, tick)
                        else:
                            _git_guard(path, node, metadata, linked, tick)
                        if stat.S_ISREG(metadata.st_mode) and not linked:
                            size += metadata.st_size
                    for bucket in (report, counts):
                        bucket["eligible"] += 1
                        bucket["bytes"] += size
                    if apply:
                        # Recheck guards at the handoff to deletion as well.
                        if runs:
                            for node, metadata, linked, depth in _walk(path, root, tick):
                                _run_guard(path, node, metadata, linked, depth, cutoff, tick)
                        else:
                            if _entry_timestamp(_checked_info(path, root, tick)) >= cutoff:
                                raise _KeepRun("kept_because_young")
                            for node, metadata, linked, depth in _walk(path, root, tick):
                                _git_guard(path, node, metadata, linked, tick)
                        _remove(path, root, tick, removed)
                        report["deleted"] += 1
                        counts["deleted"] += 1
                except _KeepRun as kept:
                    report[kept.reason] += 1
                    if kept.reason == "kept_because_young":
                        counts[kept.reason] += 1
                except (OSError, ValueError, RuntimeError):
                    report["failed"] += 1
                    counts["failed"] += 1
                finally:
                    report["deleted_bytes"] += removed[0]
                    counts["deleted_bytes"] += removed[0]
    except _BudgetExpired:
        report["budget_stopped"] = True
    return report


def prune_temp_root(root, *, now, min_age_seconds, apply, prefixes=LEAK_PREFIXES,
                    time_budget_seconds=None):
    """Prune known prefixes by creation age when available, otherwise mtime."""
    return _prune(root, now=now, min_age_seconds=min_age_seconds, apply=apply,
                  prefixes=prefixes, protect=(), runs=False,
                  time_budget_seconds=time_budget_seconds)


def prune_run_roots(root, *, now, min_age_seconds, apply, protect=(),
                    time_budget_seconds=None):
    """Prune per-run directories by mtime activity, preserving live/worktree roots."""
    return _prune(root, now=now, min_age_seconds=min_age_seconds, apply=apply,
                  prefixes=(), protect=protect, runs=True,
                  time_budget_seconds=time_budget_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--runs-parent", type=Path)
    parser.add_argument("--protect", action="append", default=[], metavar="NAME")
    parser.add_argument("--days", type=float, default=2)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.root is None and args.runs_parent is None:
        requested = os.environ.get("MODELQUAD_TEMP_ROOT", "").strip()
        if not requested:
            parser.error("Set MODELQUAD_TEMP_ROOT or pass --root/--runs-parent; no ambient temp fallback")
        args.root = Path(requested)
    if not math.isfinite(args.days) or args.days < 1:
        parser.error("--days must be finite and at least 1")
    reports = {}
    try:
        if args.root is not None:
            reports["temp_root"] = prune_temp_root(
                args.root, now=time.time(), min_age_seconds=args.days * 86400, apply=args.apply,
            )
        if args.runs_parent is not None:
            reports["run_roots"] = prune_run_roots(
                args.runs_parent, now=time.time(), min_age_seconds=args.days * 86400,
                apply=args.apply, protect=args.protect,
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(reports, indent=2))
    if not args.apply:
        command = [sys.executable, "scripts/prune_temp.py", "--days", str(args.days), "--apply"]
        for flag, value in (("--root", args.root), ("--runs-parent", args.runs_parent)):
            if value is not None:
                command += [flag, str(value.absolute())]
        for name in args.protect:
            command += ["--protect", name]
        # PowerShell quotes are literal and escape a quote by doubling it.
        rendered = "& " + " ".join("'" + token.replace("'", "''") + "'" for token in command) if os.name == "nt" else shlex.join(command)
        print("Dry-run only. Apply with:\n" + rendered)
    return 1 if any(report["failed"] for report in reports.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
