# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Working directory and inherited import context for product children."""

from __future__ import annotations

from collections.abc import Sequence
import logging
import os
from pathlib import Path
import sys

from . import paths
from .executable_resolution import _path_reason
from .statecontext import STATE_ROOT_ENV_VAR


_logger = logging.getLogger(__name__)


def background_directory() -> str:
    """Return a validated state root, home, or interpreter directory.

    Never use the parent's working folder: for source ``-m``/``-c`` children
    this directory becomes ``sys.path[0]``. Check raw paths before realpath
    so relative overrides cannot become trusted just by canonicalizing them.
    Only the state root is created. Refusals log reason codes, never paths.
    """
    try:
        current = os.path.normcase(os.path.realpath(os.getcwd()))
    except (OSError, ValueError):
        # A deleted working folder is unknown, not a reason to refuse.
        current = None
    for source in ("state", "home", "interpreter"):
        try:
            candidate = (paths.state_root() if source == "state" else
                         Path.home() if source == "home" else
                         Path(sys.executable).resolve().parent)
            raw = str(candidate)
            reason = _path_reason(raw)
            if reason is None:
                canonical = os.path.realpath(raw)
                reason = _path_reason(canonical)
                if reason is None and os.path.normcase(canonical) == current:
                    reason = "state_root_is_cwd" if source == "state" else "fallback_is_cwd"
                if reason is None:
                    if source == "state":
                        Path(canonical).mkdir(parents=True, exist_ok=True)
                    if Path(canonical).is_dir():
                        return canonical
                    reason = "directory_missing"
        except (OSError, ValueError, RuntimeError):
            reason = "directory_unavailable"
        _logger.debug("%s", reason)
    raise OSError("background_cwd_unavailable")


def validated_directory(directory: str | os.PathLike[str]) -> str:
    """An explicit local directory, without creating or trusting relative input."""
    raw = os.fspath(directory)
    if _path_reason(raw) or raw.startswith(("\\\\", "//")):
        raise OSError("service_cwd_unavailable")
    canonical = os.path.realpath(raw)
    if (_path_reason(canonical) or canonical.startswith(("\\\\", "//"))
            or not Path(canonical).is_dir()):
        raise OSError("service_cwd_unavailable")
    return canonical


def background_child_kwargs(command: Sequence[str], *, cwd: str | os.PathLike[str] | None = None) -> dict[str, object]:
    """Return cwd and, only when changed by value, a complete child env.

    Relative state overrides keep the parent's store after the cwd change.
    Python children keep existing PYTHONPATH roots, made absolute
    against the parent at launch time. No import root is injected when absent.
    The job runner executable is also sys.executable.
    """
    kwargs: dict[str, object] = {"cwd": background_directory() if cwd is None else validated_directory(cwd)}
    inherited = dict(os.environ)
    env = dict(inherited)
    raw_root = inherited.get(STATE_ROOT_ENV_VAR)
    if raw_root:
        expanded = os.path.expanduser(raw_root)
        if not Path(expanded).is_absolute():
            env[STATE_ROOT_ENV_VAR] = os.path.abspath(expanded)
    if (command and
            os.path.normcase(os.path.realpath(command[0])) ==
            os.path.normcase(os.path.realpath(sys.executable)) and
            "PYTHONPATH" in inherited):
        entries = []
        seen = set()
        for entry in inherited["PYTHONPATH"].split(os.pathsep):
            if not entry:
                continue
            absolute = entry if Path(entry).is_absolute() else os.path.abspath(entry)
            key = os.path.normcase(os.path.realpath(absolute))
            if key not in seen:
                entries.append(absolute)
                seen.add(key)
        env["PYTHONPATH"] = os.pathsep.join(entries)
    if env != inherited:
        kwargs["env"] = env
    return kwargs
