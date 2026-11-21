# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which exact build this process is.

Every build used to say ``ModelQuad 0.2.0``: the package version names a
release line, not a build, so two installs of different commits were
indistinguishable from ``--version``, ``/api/health`` and the MCP handshake.
The answer comes from the checkout this package was imported from, never
from a guess: ``git rev-parse --verify HEAD``, read-only (no fetch, no index
refresh, no lock).

Anything that cannot be read, or that contradicts itself, is ``unknown``.
Nothing here carries an absolute path, so the whole record is safe to
publish on ``/api/health``.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .branding import installed_version
from .executable_resolution import resolve_executable

UNKNOWN = "unknown"

CHANNEL_SOURCE = "source"
#: An installed copy pip took from a package index: the version names the
#: release, whose files and hashes are published with it.
CHANNEL_INDEX = "release from a package index"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class BuildInfo:
    version: str
    source_commit: str
    short: str
    built_at_utc: str
    channel: str

    @property
    def known(self) -> bool:
        return self.source_commit != UNKNOWN

    @property
    def identity(self) -> str:
        """``<version>+<sha7>``, or the bare version when the commit is unknown."""

        return f"{self.version}+{self.short}" if self.known else self.version

    @property
    def label(self) -> str:
        """``<version>+<sha7> (<channel>)``, or ``<version> (unknown build)``."""

        if self.known:
            return f"{self.identity} ({self.channel})"
        if self.channel == CHANNEL_INDEX:
            return f"{self.version} ({CHANNEL_INDEX})"
        return f"{self.version} (unknown build)"

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "source_commit": self.source_commit,
            "short": self.short,
            "built_at_utc": self.built_at_utc,
            "channel": self.channel,
        }


GitRunner = Callable[[Path], "str | None"]
InstallReader = Callable[[], str]


def _install_kind() -> str:
    """How pip installed this copy (PEP 610), as ``updater.detect_install`` reads it."""

    from .updater import detect_install

    return detect_install().kind


def _commit_or_unknown(value: object) -> str:
    if isinstance(value, str) and _FULL_SHA.match(value.strip().lower()):
        return value.strip().lower()
    return UNKNOWN


def _source_checkout_root() -> Path | None:
    """The checkout this package was imported from, or None for an installed copy."""

    src_dir = Path(__file__).resolve().parent.parent
    root = src_dir.parent
    if src_dir.name == "src" and (root / "pyproject.toml").is_file():
        return root
    return None


def _git_head(root: Path) -> str | None:
    from .gitmeta import _Unavailable, _git_exclude_roots
    from .procutil import no_window_kwargs

    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        executable = resolve_executable("git", exclude_roots=_git_exclude_roots(root))
        if executable.path is None:
            return None
        completed = subprocess.run(
            [executable.path, "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
            check=False,
            **no_window_kwargs(),
        )
    except (_Unavailable, OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def read_build_info(
    *,
    source_root: Path | None = None,
    git_head: GitRunner | None = None,
    install_kind: InstallReader | None = None,
) -> BuildInfo:
    """Resolve the build identity now. Every argument defaults to this process."""

    version = installed_version(UNKNOWN) or UNKNOWN
    root = source_root if source_root is not None else _source_checkout_root()
    commit = _commit_or_unknown((git_head or _git_head)(root)) if root is not None else UNKNOWN
    channel = CHANNEL_SOURCE
    if root is None and version != UNKNOWN:
        from .updater import PYPI

        try:
            from_index = (install_kind or _install_kind)() == PYPI
        except Exception:  # noqa: BLE001 -- unknown stays unknown
            from_index = False
        if from_index:
            channel = CHANNEL_INDEX
    return BuildInfo(
        version=version,
        source_commit=commit,
        short=commit[:7] if commit != UNKNOWN else UNKNOWN,
        built_at_utc=UNKNOWN,
        channel=channel,
    )


@lru_cache(maxsize=1)
def current_build_info() -> BuildInfo:
    """This process's identity, resolved once.

    A process does not change builds while it runs; the cache keeps a polled
    endpoint (``/api/health``) from spawning ``git`` on every request.
    """

    return read_build_info()
