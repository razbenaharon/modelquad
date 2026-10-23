# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Changed-files observation is disabled.

Repository clean/process filters can execute outside worker containment during
Git status, including in submodules. snapshot() and changed_files() therefore
return None (not observed) without Git or filesystem access. Restoring the list
safely is a separate item. Shared metadata Git and identity helpers remain.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

from modelquad.procutil import no_window_kwargs
from modelquad.executable_resolution import resolve_executable

GIT_SECONDS = 15
MAX_GIT_BYTES = 4 * 1024 * 1024


class _Unavailable(Exception):
    pass


@dataclass(frozen=True, repr=False)
class Snapshot:
    directory: Path
    root: Path
    head: str
    dirty: dict[str, bytes | None]


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Unavailable
    return remaining


def _git_exclude_roots(directory: Path) -> tuple[Path, ...]:
    """Exclude the launch folder and every enclosing repository without git."""
    # repo_identity supplies OS-canonical extended paths. Compare exclusions
    # in normal Win32 spelling so the resolver cannot skip a mixed-drive pair.
    spelling = os.fspath(directory)
    if os.name == 'nt':
        spelling = spelling.replace('/', '\\')
        if spelling[:8].upper() == '\\\\?\\UNC\\':
            spelling = '\\\\' + spelling[8:]
        elif (spelling.startswith('\\\\?\\') and len(spelling) >= 7
              and spelling[4].isalpha() and spelling[5:7] == ':\\'):
            spelling = spelling[4:]
        if spelling.startswith(('\\\\?\\', '\\\\.\\')):
            raise _Unavailable
    try:
        folder = Path(os.path.abspath(spelling))
        roots = [folder]
        for ancestor in (folder, *folder.parents):
            try:
                (ancestor / '.git').lstat()
            except FileNotFoundError:
                continue
            # Submodules and linked worktrees have .git files. Keep walking:
            # an enclosing repository can also supply a planted executable.
            if ancestor != folder:
                roots.append(ancestor)
        return tuple(roots)
    except (OSError, ValueError):
        raise _Unavailable from None


def _git(directory: Path, args: list[str], deadline: float) -> bytes:
    # Inherited Git configuration/trace overrides can write files or redirect
    # the repository. Do not carry them into this read-only observer.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
    executable = resolve_executable('git', exclude_roots=_git_exclude_roots(directory))
    if executable.path is None:
        if executable.refusal == 'not_found':
            raise FileNotFoundError('git')
        raise _Unavailable
    command = [executable.path, '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
               '-c', 'diff.external=', '-c', 'core.hooksPath=' + os.devnull,
               '-c', 'filter.lfs.process=', '-c', 'filter.lfs.clean=',
               '-c', 'filter.lfs.smudge=', '-c', 'filter.lfs.required=false', *args]
    limit = time.monotonic() + min(GIT_SECONDS, _remaining(deadline))
    output = bytearray()
    overflow = threading.Event()
    failed = threading.Event()
    count = 0
    lock = threading.Lock()

    def drain(stream, capture):
        nonlocal count
        try:
            while block := stream.read(65536):
                with lock:
                    count += len(block)
                    if count > MAX_GIT_BYTES:
                        overflow.set()
                        return
                    if capture:
                        output.extend(block)
        except Exception:
            failed.set()

    with subprocess.Popen(command, cwd=directory, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **no_window_kwargs()) as child:
        readers = [threading.Thread(target=drain, args=(stream, capture), daemon=True)
                   for stream, capture in ((child.stdout, True), (child.stderr, False))]
        for reader in readers:
            reader.start()
        try:
            while child.poll() is None:
                if overflow.is_set() or failed.is_set():
                    raise _Unavailable
                try:
                    child.wait(timeout=min(.05, _remaining(limit)))
                except subprocess.TimeoutExpired:
                    continue
            for reader in readers:
                reader.join(timeout=_remaining(limit))
        except BaseException:
            if child.poll() is None:
                child.kill()
            child.wait()
            for reader in readers:
                reader.join(timeout=1)
            raise
        if (child.returncode or overflow.is_set() or failed.is_set()
                or any(reader.is_alive() for reader in readers)):
            raise _Unavailable
    _remaining(deadline)
    return bytes(output)


def _linked(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _safe_directory(directory: Path) -> Path:
    if '..' in directory.parts:
        # Normalizing first could hide a link traversed by the worker's cwd.
        raise _Unavailable
    directory = Path(os.path.abspath(directory))
    for ancestor in (*reversed(directory.parents), directory):
        info = ancestor.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise _Unavailable
    return directory.resolve(strict=True)


def snapshot(directory: str | Path) -> Snapshot | None:
    """Return None: changed-files observation is disabled."""
    return None


def changed_files(before: Snapshot | None, directory: str | Path) -> tuple[str, ...] | None:
    """Return None without observing, including for a legacy Snapshot."""
    return None
