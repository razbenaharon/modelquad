# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The installed ``modelquad`` entry point; the hooks run before the full CLI is imported."""

from __future__ import annotations

import os
import sys
from typing import Sequence

#: Commands earlier builds wrote into Claude Code's settings: a status line
#: (``usage-hook``), a SessionStart note (``hook-pending``) and a PreToolUse
#: note (``hook-gate``). A session or a settings file that still runs one gets
#: exit 0 and no output; nothing is read, written or shown.
RETIRED_HOOK_COMMANDS = frozenset({"hook-pending", "hook-gate", "usage-hook"})
_OPTIONS_WITH_VALUE = ("--usage-directory", "--history-db", "--config")


def _command(argv: Sequence[str]) -> str | None:
    """The first word that is a command, past the global options earlier builds accepted."""
    args = list(argv)
    while args:
        word = args.pop(0)
        if word in _OPTIONS_WITH_VALUE:
            if args:
                args.pop(0)
            continue
        if word.startswith("-"):
            continue
        return word
    return None


def dispatch_retired(argv: Sequence[str]) -> int | None:
    """Exit 0, silently, for a hook command this build no longer has; None otherwise."""
    return 0 if _command(argv) in RETIRED_HOOK_COMMANDS else None


def dispatch_review(argv: Sequence[str]) -> int | None:
    """``hook-review claude``: the review reminder and its two gates; always fails silent."""
    if not argv or argv[0] != "hook-review":
        return None
    if os.environ.get("MODELQUAD_WORKER") == "1":
        return 0
    if len(argv) != 2 or argv[1] != "claude":
        return 0
    try:
        from .adapters.review_reminder import run_hook
        return run_hook(argv[1], getattr(sys.stdin, "buffer", sys.stdin), sys.stdout)
    except Exception:
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    from . import netpolicy
    # ModelQuad's own process connects only to loopback (netpolicy.py).
    netpolicy.install_loopback_guard()
    args = list(sys.argv[1:] if argv is None else argv)
    for dispatch in (dispatch_retired, dispatch_review):
        result = dispatch(args)
        if result is not None:
            return result
    from .cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
