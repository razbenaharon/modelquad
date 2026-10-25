# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which installed Codex CLI ModelQuad runs.

A machine can hold more than one Codex CLI: the official desktop install keeps
versions under ``%LOCALAPPDATA%\\OpenAI\\Codex\\bin`` (the current one in a
hashed subfolder, older ones beside it), the official standalone installer
uses ``%LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin``, and an npm or other
install can sit earlier on PATH. Running whichever comes first on PATH ran a stale npm
codex-cli 0.155.0 that the service refused for the configured default model,
while the official 0.160.0 was installed.

The rule: the newest version wins; on an equal version the official install
wins; a PATH copy older than the chosen CLI is reported as stale, never
updated or removed. With no official install, or no readable version, the
PATH lookup stands exactly as before. Every Codex lookup -- worker launch,
version capture, quota read and doctor -- resolves through here, so the CLI
that runs is the CLI that is recorded.

An npm install puts a batch launcher (``codex.cmd``) on PATH, and a batch
file cannot carry a prompt safely. A prompt-carrying launch therefore asks
for ``native=True``: a launcher is replaced by the native ``codex.exe`` its
own package ships (``npm_native_codex``), and a launcher with no native
binary behind it is not a candidate at all.

Probing costs no provider quota (``--version`` contacts no backend) and runs
only when an official install exists beside the PATH lookup. Results are
cached per executable identity (path, size, modification time).
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import executable_resolution

CODEX_BINARY = "codex"
_VERSION_TIMEOUT_SECONDS = 10.0
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")

_version_cache: dict[tuple[str, int, int], str | None] = {}


@dataclass(frozen=True)
class CodexCli:
    """The chosen executable, where it came from, and what was passed over."""

    path: str
    source: str  # "official" or "path"
    version: str | None
    stale_path_version: str | None = None


def official_candidates() -> list[Path]:
    """Codex executables from OpenAI's own Windows installs.

    The desktop app keeps versions in ``OpenAI/Codex/bin`` and its hashed
    subfolders; the standalone installer links ``Programs/OpenAI/Codex/bin``
    both under ``%LOCALAPPDATA%``. Install detection shares this list.
    """
    if os.name != "nt":
        return []
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return []
    found: list[Path] = []
    desktop = Path(root) / "OpenAI" / "Codex" / "bin"
    try:
        found += [entry / "codex.exe" for entry in desktop.iterdir() if entry.is_dir()]
    except OSError:
        pass
    found.append(desktop / "codex.exe")
    found.append(Path(root) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe")
    return [path for path in found if path.is_file()]


def is_desktop_version_path(path: str | Path) -> bool:
    """Whether an executable lives in a disposable desktop version folder."""
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return False
    desktop = Path(root) / "OpenAI" / "Codex" / "bin"
    return Path(path).parent.parent == desktop


def project_roots() -> tuple[str, ...]:
    """The working folder, when its subfolders must not supply a Codex.

    Claude Code starts ModelQuad in the project folder. A project can carry a
    planted ``codex.exe`` in a folder an editor puts on PATH (an activated
    ``.venv\\Scripts``, say), and ModelQuad runs a found Codex for its version
    and its quota without a prompt. So everything under the working folder is
    refused for that lookup. The exception is a working folder that is the
    home folder or above it: excluding it would exclude the user's own install
    under AppData, and a project is never that high.
    """
    try:
        cwd = os.path.normcase(os.path.realpath(os.getcwd()))
        home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    except OSError:
        return ()
    try:
        if os.path.commonpath((cwd, home)) == cwd:
            return ()
    except ValueError:
        pass  # different drives: the working folder is not above home
    return (cwd,)


def probe_which(name: str) -> str | None:
    # Codex/Claude npm shims are admitted for selection and fixed probes only.
    # Prompt-carrying launchers must apply their own batch policy.
    allow_batch = Path(name).stem.lower() in {"codex", "claude"}
    roots = project_roots()
    if any(char in name for char in ("/", "\\", ":", "\x00")) or not name:
        return executable_resolution.admit_executable(name, allow_batch=allow_batch, exclude_roots=roots).path
    return executable_resolution.safe_which(name, allow_batch=allow_batch, exclude_roots=roots)


#: The npm package's Windows targets: ``platform.machine()`` -> (package
#: suffix, Rust target triple), as ``@openai/codex``'s own ``bin/codex.js``
#: maps them.
_NPM_TARGETS = {
    "amd64": ("x64", "x86_64-pc-windows-msvc"),
    "x86_64": ("x64", "x86_64-pc-windows-msvc"),
    "arm64": ("arm64", "aarch64-pc-windows-msvc"),
    "aarch64": ("arm64", "aarch64-pc-windows-msvc"),
}


def is_launcher(path: str | Path) -> bool:
    """Whether a resolved Codex is a batch launcher rather than an executable."""
    return Path(path).suffix.lower() in {".cmd", ".bat"}


def npm_native_codex(launcher: str | Path) -> str | None:
    """The native ``codex.exe`` an npm launcher would start, or None.

    ``bin/codex.js`` runs ``vendor/<triple>/bin/codex.exe`` from the platform
    package ``@openai/codex-win32-<arch>``, which npm installs either nested
    under ``@openai/codex`` or hoisted beside it; older packages kept the
    vendor folder in ``@openai/codex`` itself. Only those fixed locations
    beside the launcher are inspected, and a hit must pass the same
    executable admission as any other native candidate.
    """
    target = _NPM_TARGETS.get(platform.machine().lower())
    if target is None or not is_launcher(launcher):
        return None
    arch, triple = target
    scope = Path(launcher).parent / "node_modules" / "@openai"
    package = f"codex-win32-{arch}"
    candidates = (
        scope / "codex" / "node_modules" / "@openai" / package / "vendor" / triple / "bin" / "codex.exe",
        scope / package / "vendor" / triple / "bin" / "codex.exe",
        scope / "codex" / "vendor" / triple / "bin" / "codex.exe",
        scope / "codex" / "vendor" / triple / "codex" / "codex.exe",
    )
    for candidate in candidates:
        admitted = executable_resolution.admit_executable(str(candidate), allow_batch=False)
        if admitted.path is not None and admitted.kind == "binary":
            return admitted.path
    return None


def _native(path: str | None) -> str | None:
    if path is None or not is_launcher(path):
        return path
    return npm_native_codex(path)


def _probe(path: str) -> str | None:
    # Fixed --version admits npm batch shims; prompt launches must not inherit this policy.
    # Official candidates are codex.exe only, so batch admission is unused.
    # Admission is a gate: preserve the candidate spelling for selection.
    if executable_resolution.admit_executable(path, allow_batch=True).path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns)
    if key in _version_cache:
        return _version_cache[key]
    version = None
    try:
        import tempfile

        from .execution.codex import WORKER_ENV_VAR, worker_environment
        from .procutil import no_window_kwargs

        # The same environment and empty folder a Codex run gets: other
        # services' secrets removed and the worker marker set.
        env = worker_environment()
        env[WORKER_ENV_VAR] = "1"
        with tempfile.TemporaryDirectory(prefix="modelquad-probe-") as empty:
            completed = subprocess.run(  # noqa: S603 -- fixed argv, shell=False
                [path, "--version"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_VERSION_TIMEOUT_SECONDS, shell=False,
                cwd=empty, env=executable_resolution.safe_child_env(env),
                **no_window_kwargs(),
            )
        if completed.returncode == 0:
            match = _VERSION.search((completed.stdout or "").strip().splitlines()[0] if completed.stdout else "")
            version = match.group(0) if match else None
    except (OSError, subprocess.SubprocessError, IndexError):
        version = None
    _version_cache[key] = version
    return version


def version_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    """SemVer precedence: numeric identifiers precede text; releases sort last."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(version)
    major, minor, patch, pre = match.groups()
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in pre.split(".")
    ) if pre else ()
    return int(major), int(minor), int(patch), 0 if pre else 1, identifiers


def resolve_codex_cli(
    *,
    which: Callable[[str], str | None] | None = None,
    candidates: Callable[[], list[Path]] | None = None,
    probe: Callable[[str], str | None] | None = None,
    native: bool = False,
) -> CodexCli | None:
    """Apply newest-wins, official-on-tie; None when nothing is installed.

    Selection/probe only. Without ``native`` the result may be a batch
    launcher, and a launcher must re-admit through ``prepare_launch`` with its
    own batch policy. With ``native`` a launcher is replaced by the package's
    own native executable, or dropped when it has none, so only native
    executables compete.
    """
    on_path = (which if which is not None else probe_which)(CODEX_BINARY)
    if native:
        on_path = _native(on_path)
    official = [str(path) for path in (candidates or official_candidates)()]
    if not official:
        return CodexCli(on_path, "path", None) if on_path else None
    read = probe or _probe
    ranked = []
    path_version = read(on_path) if on_path else None
    if on_path and path_version:
        ranked.append((version_key(path_version), 0, on_path, "path", path_version))
    for path in official:
        version = read(path)
        if version:
            ranked.append((version_key(version), 1, path, "official", version))
    if not ranked:
        return CodexCli(on_path, "path", None) if on_path else None
    _, _, path, source, version = max(ranked)
    stale = (
        path_version
        if source == "official" and path_version and version_key(path_version) < version_key(version)
        else None
    )
    return CodexCli(path, source, version, stale)


def which_codex(
    binary: str = CODEX_BINARY, *, which: Callable[[str], str | None] | None = None, native: bool = False,
) -> str | None:
    """The chosen Codex CLI, or ``which`` for anything else.

    Selection/probe only. Without ``native`` it may be a batch launcher; a
    launcher must re-admit through ``prepare_launch`` with its own batch
    policy. ``native`` returns only a native executable (see
    :func:`resolve_codex_cli`).
    """
    which = which if which is not None else probe_which
    if binary != CODEX_BINARY:
        found = which(binary)
        return _native(found) if native else found
    chosen = resolve_codex_cli(which=which, native=native)
    return chosen.path if chosen else None


def launcher_only(binary: str = CODEX_BINARY, *, which: Callable[[str], str | None] | None = None) -> bool:
    """Whether the only Codex found is a launcher with no native binary behind it."""
    if which_codex(binary, which=which, native=True) is not None:
        return False
    found = which_codex(binary, which=which)
    return found is not None and is_launcher(found)


def clear_cache() -> None:
    _version_cache.clear()
