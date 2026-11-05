# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Light installed command entry; startup hooks precede the full CLI imports."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Sequence

from . import windowless_diagnostics as diagnostics

# Import-safe null repair only. Files are bound in main after state selection.
diagnostics.bootstrap(protocol_stdout=(diagnostics.protocol_stdout_for(sys.argv[1:])
                                      or isinstance(sys.stdout, diagnostics.ProtocolText)))


def dispatch_pending(argv: Sequence[str]) -> int | None:
    """Return None for another command; pending hooks always fail silent."""
    if not argv or argv[0] != "hook-pending":
        return None
    # A worker returns before importing the handler or touching stdin/state.
    if os.environ.get("MODELQUAD_WORKER") == "1":
        return 0
    if len(argv) != 2 or argv[1] != "claude":
        return 0
    try:
        from .adapters.pending_reminder import run_hook
        return run_hook(argv[1], getattr(sys.stdin, "buffer", sys.stdin), sys.stdout)
    except Exception:
        return 0


def dispatch_review(argv: Sequence[str]) -> int | None:
    """``hook-review claude``: the advisory review reminder; always fails silent."""
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


_USAGE_HOOK_FLAGS = ("--quiet", "--segment")


def _usage_hook_arguments(argv: Sequence[str]) -> tuple[Path | None, bool, bool] | None:
    """(usage directory override, quiet, segment) for a plain status-line call, else None.

    Accepts only the shapes the installer writes and ``cli.main`` parses the
    same way: an optional leading ``--usage-directory DIR``, then
    ``usage-hook claude`` with optional ``--quiet`` and ``--segment``. Anything
    else goes to the full CLI, which owns every other option.
    """

    args = list(argv)
    directory: Path | None = None
    if args and args[0] == "--usage-directory" and len(args) >= 2:
        directory, args = Path(args[1]), args[2:]
    elif args and args[0].startswith("--usage-directory="):
        directory, args = Path(args[0].split("=", 1)[1]), args[1:]
    if args[:1] != ["usage-hook"]:
        return None
    tail = args[1:]
    if [item for item in tail if item not in _USAGE_HOOK_FLAGS] != ["claude"]:
        return None
    return directory, "--quiet" in tail, "--segment" in tail


def dispatch_usage_hook(argv: Sequence[str]) -> int | None:
    """Run the status hook without importing the main CLI or router.

    ``usage_hook.run`` is the same implementation ``cli.main`` runs, so this
    path writes the same usage file, host observation and status line.
    """
    parsed = _usage_hook_arguments(argv)
    if parsed is None:
        return None
    directory, quiet, segment = parsed
    try:
        from .providers.base import DEFAULT_USAGE_DIRECTORY
        from .usage_hook import run
        return run(usage_directory=directory or DEFAULT_USAGE_DIRECTORY, quiet=quiet, segment=segment)
    except Exception:
        return 0


def dispatch_gate(argv: Sequence[str]) -> int | None:
    """``hook-gate``: a no-op kept for Claude Code sessions started before an upgrade.

    Earlier builds installed a PreToolUse hook running ``modelquad hook-gate``
    that added a one-line note per session. The guidance now comes from the MCP
    server's instructions, install and uninstall remove the hook, and a session
    that still has it gets an immediate exit 0: no output, nothing read or kept.
    """
    if list(argv) != ["hook-gate"]:
        return None
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from . import netpolicy
    # ModelQuad's own process connects only to loopback (netpolicy.py).
    netpolicy.install_loopback_guard()
    args = list(sys.argv[1:] if argv is None else argv)
    protocol = diagnostics.protocol_stdout_for(args)
    windowless = diagnostics.bootstrap(
        force=not protocol and "--managed-child" in args,
        protocol_stdout=protocol,
    )
    # An earlier bootstrap in this process already replaced the streams.
    windowless = windowless or diagnostics.is_active()
    selected = diagnostics.select_state(args)
    if windowless and selected and not protocol:
        diagnostics.bind_selected_state(diagnostics.role_for(args))
        diagnostics.emit("entry")
    elif windowless:
        # A previous embedded invocation cannot receive an event before this
        # invocation has established its own state selection.
        diagnostics.clear_sink()
    try:
        result = dispatch_pending(args)
        if result is None:
            result = dispatch_review(args)
        if result is None:
            result = dispatch_usage_hook(args)
        if result is None:
            result = dispatch_gate(args)
        if result is None:
            from .cli import main as cli_main
            result = cli_main(argv)
        if windowless and not protocol and result:
            diagnostics.emit("startup_failed")
        return result
    except SystemExit as exc:
        if protocol:
            return 0
        if not windowless:
            raise
        if exc.code:
            diagnostics.emit("startup_failed")
        return exc.code if type(exc.code) is int else int(bool(exc.code))
    except Exception:
        if protocol:
            return 0  # Broken protocol streams must not produce a traceback.
        if not windowless:
            raise
        diagnostics.emit("startup_failed")
        return 1
    finally:
        if windowless and not protocol:
            diagnostics.emit("exit")


if __name__ == "__main__":
    raise SystemExit(main())
