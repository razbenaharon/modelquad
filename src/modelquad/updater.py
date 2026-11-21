# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad update``: upgrade the package and refresh the plugin, in one step the user starts.

There is no automatic update. ModelQuad has no background service, its own
processes connect only to loopback (``netpolicy.py``), and Windows does not
let pip replace ``modelquad-mcp.exe`` while Claude Code runs it: an upgrade
started then fails halfway and leaves ModelQuad uninstalled. So the update
refuses while Claude Code, the Claude app or ``modelquad-mcp.exe`` is running.

The network work is done by child processes, pip and git (and Claude Code's
own ``claude plugin`` commands), which go online themselves. ModelQuad's own
process opens no socket here; the loopback guard stays on.

How the package was installed decides the commands. From PyPI:
``python -m pip install --upgrade modelquad`` with this same interpreter.
From a clone (a Git checkout installed with ``pip install .``, or an editable
install): ``git pull --ff-only`` in that checkout, then ``pip install .``
(``pip install -e .`` for an editable one). When it cannot tell, it says so
and prints both, and runs neither.

Every outside effect goes through :class:`Tools`, so tests replace it whole.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from .branding import DISTRIBUTION_NAME, PLUGIN_NAME

#: Processes that hold ModelQuad's programs open: Claude Code (claude.exe),
#: the Claude desktop app (Claude.exe; Windows names are not case-sensitive),
#: and ModelQuad's MCP server.
BLOCKING_PROCESSES = ("claude.exe", "modelquad-mcp.exe")
PLUGIN_ID = f"{PLUGIN_NAME}@{PLUGIN_NAME}"

PYPI = "pypi"
CLONE = "clone"
EDITABLE = "editable"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Install:
    """How the package was installed: ``kind`` and, for a clone, its folder."""

    kind: str
    folder: Path | None = None
    why: str = ""


@dataclass
class Result:
    code: int
    output: str = ""


def _blocking_running() -> list[str] | None:
    """The blocking processes that are running, by image name; None when unknown."""
    if os.name != "nt":
        return None
    from .procutil import _windows_process_snapshot

    snapshot = _windows_process_snapshot()
    if snapshot is None:
        return None
    wanted = {name.casefold() for name in BLOCKING_PROCESSES}
    return sorted({image for _pid, _parent, image in snapshot if image.casefold() in wanted})


def _run(argv: list[str], cwd: Path | None = None) -> Result:
    """Run one child process without a console window of its own, and return its
    exit code and output (the caller prints it)."""
    from .executable_resolution import UnsafeExecutable, prepare_launch
    from .procutil import no_window_kwargs

    try:
        # A bare name (git, claude) is looked up on PATH, never in the folder it
        # runs in. A full path is this same interpreter, which is running already;
        # it may live in a virtual environment inside the clone.
        command = prepare_launch(argv, cwd=None if os.path.isabs(argv[0]) else cwd)
    except UnsafeExecutable as exc:
        return Result(127, f"{argv[0]} could not be started ({exc}).")
    try:
        completed = subprocess.run(command, cwd=str(cwd) if cwd else None, check=False,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   encoding="utf-8", errors="replace", **no_window_kwargs())
    except OSError as exc:
        return Result(127, f"{argv[0]} could not be started ({exc.strerror or exc}).")
    return Result(completed.returncode, completed.stdout or "")


def _echo(tools: "Tools", result: Result) -> None:
    for line in result.output.rstrip().splitlines():
        tools.out("  " + line)


def _which(name: str) -> str | None:
    from .executable_resolution import safe_which

    return safe_which(name)


def detect_install(distribution=None) -> Install:
    """Read how the running package was installed, from pip's own record (PEP 610)."""
    try:
        if distribution is None:
            from importlib.metadata import distribution as find

            distribution = find(DISTRIBUTION_NAME)
        text = distribution.read_text("direct_url.json")
        installer = (distribution.read_text("INSTALLER") or "").strip().lower()
    except Exception:  # noqa: BLE001 -- unknown stays unknown
        return Install(UNKNOWN, why="pip's record of this install could not be read")
    if installer and installer != "pip":
        return Install(UNKNOWN, why=f"it was installed with {installer[:20]}, not pip")
    if text is None:
        # No direct URL: pip installed it from a package index.
        return Install(PYPI)
    try:
        record = json.loads(text)
        url = record["url"]
    except (ValueError, KeyError, TypeError):
        return Install(UNKNOWN, why="pip's record of where this install came from could not be read")
    parsed = urlparse(url)
    info = record.get("dir_info")
    if parsed.scheme != "file" or not isinstance(info, dict):
        return Install(UNKNOWN, why="it was installed from a file or a link, not from PyPI or a clone")
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    folder = Path(path)
    if not (folder / ".git").exists():
        return Install(UNKNOWN, folder, why="the folder it was installed from is not a Git checkout")
    return Install(EDITABLE if info.get("editable") else CLONE, folder)


@dataclass
class Tools:
    """Every outside effect of the update; tests replace them."""

    running: Callable[[], list[str] | None]
    run: Callable[..., Result]
    which: Callable[[str], str | None]
    install: Callable[[], Install]
    python: str = field(default_factory=lambda: sys.executable)
    out: Callable[[str], None] = print


def default_tools() -> Tools:
    """The real ones, looked up when the update starts."""
    return Tools(running=_blocking_running, run=_run, which=_which, install=detect_install)


def package_steps(install: Install, python: str) -> list[tuple[list[str], Path | None]]:
    """The commands that upgrade the package, with the folder each runs in."""
    if install.kind == PYPI:
        return [([python, "-m", "pip", "install", "--upgrade", DISTRIBUTION_NAME], None)]
    if install.kind in (CLONE, EDITABLE):
        pip = ["-e", "."] if install.kind == EDITABLE else ["."]
        return [(["git", "pull", "--ff-only"], install.folder),
                ([python, "-m", "pip", "install", *pip], install.folder)]
    return []


PLUGIN_STEPS = (
    ["claude", "plugin", "marketplace", "update", PLUGIN_NAME],
    ["claude", "plugin", "uninstall", PLUGIN_ID],
    ["claude", "plugin", "install", PLUGIN_ID],
)


def _show(argv: list[str], cwd: Path | None) -> str:
    text = " ".join(f'"{part}"' if " " in part else part for part in argv)
    return f"{text}   (in {cwd})" if cwd else text


def _fallback(tools: Tools) -> None:
    tools.out("Upgrade by hand instead, with Claude Code and the Claude app closed:")
    tools.out("  From PyPI:     " + _show(package_steps(Install(PYPI), tools.python)[0][0], None))
    tools.out("  From a clone:  git pull --ff-only, then " + _show([tools.python, "-m", "pip", "install", "."], None)
              + ", in the clone")
    tools.out("  Then refresh the plugin:")
    for argv in PLUGIN_STEPS:
        tools.out("    " + " ".join(argv))
    tools.out("  Then: modelquad doctor")


def update(dry_run: bool = False, tools: Tools | None = None) -> int:
    """Run the update, or with ``dry_run`` only print its steps. Returns the exit code."""
    tools = tools or default_tools()
    install = tools.install()
    steps = package_steps(install, tools.python)
    running = tools.running()

    if dry_run:
        tools.out("modelquad update would do this (nothing is run now):")
        if running:
            tools.out("  First it would stop, because this is running: " + ", ".join(running))
        elif running is None:
            tools.out("  First it would stop: whether Claude Code is running could not be checked.")
        if not steps:
            tools.out(f"  It cannot tell how ModelQuad was installed ({install.why}), so it "
                      "would run nothing and print the commands.")
        for argv, cwd in steps:
            tools.out("  " + _show(argv, cwd))
        tools.out("  If the Claude Code plugin is installed (claude plugin list):")
        for argv in PLUGIN_STEPS:
            tools.out("    " + " ".join(argv))
        tools.out("  Then the checks of modelquad doctor, and the new version.")
        tools.out("pip and git connect to the internet themselves (PyPI, or the clone's Git remote); "
                  "ModelQuad does not.")
        return 0

    if running is None:
        tools.out("Not updated: whether Claude Code is running could not be checked.")
        tools.out("Close every Claude Code window and the Claude app, then update by hand.")
        _fallback(tools)
        return 1
    if running:
        tools.out("Not updated: close these first, then run modelquad update again:")
        for name in running:
            tools.out(f"  {name}")
        tools.out("A running Claude Code (claude.exe), the Claude app (Claude.exe) or ModelQuad's "
                  "server (modelquad-mcp.exe) keeps ModelQuad's programs open, and Windows does "
                  "not let pip replace them: the upgrade would stop halfway and leave ModelQuad "
                  "uninstalled. In a Claude Code terminal session, type /exit.")
        return 1
    if not steps:
        tools.out(f"Not updated: ModelQuad cannot tell how it was installed ({install.why}).")
        _fallback(tools)
        return 1

    where = "PyPI" if install.kind == PYPI else f"the clone in {install.folder}"
    tools.out(f"Updating ModelQuad from {where}. pip" + (" and git" if install.kind != PYPI else "")
              + " connect to the internet now; ModelQuad itself does not.")
    for argv, cwd in steps:
        tools.out("> " + _show(argv, cwd))
        result = tools.run(argv, cwd=cwd)
        _echo(tools, result)
        if result.code != 0:
            tools.out(f"Stopped: that command failed (exit {result.code}).")
            if argv[0] == "git":
                tools.out("Nothing was installed. Check the clone (local changes, or another "
                          "branch), then run modelquad update again.")
            else:
                tools.out("If ModelQuad no longer starts, run the same command again with Claude "
                          "Code closed.")
            return 1

    code = _refresh_plugin(tools)
    tools.out("> modelquad doctor")
    doctor = tools.run([tools.python, "-c",
                        "from modelquad.cli_entry import main; raise SystemExit(main(['doctor']))"])
    _echo(tools, doctor)
    version = tools.run([tools.python, "-c", "import modelquad; print(modelquad.__version__)"])
    shown = version.output.strip() if version.code == 0 and version.output.strip() else "unknown"
    tools.out(f"ModelQuad is now version {shown}. Start Claude Code again to use it.")
    return 1 if code or doctor.code else 0


def _refresh_plugin(tools: Tools) -> int:
    """Reinstall the plugin, so Claude Code copies the new files; only if it is installed."""
    if tools.which("claude") is None:
        tools.out("The claude program (claude.exe) was not found on PATH, so the plugin was "
                  "not refreshed. If you use the plugin, run:")
        for argv in PLUGIN_STEPS:
            tools.out("  " + " ".join(argv))
        return 0
    listing = tools.run(["claude", "plugin", "list"])
    if listing.code != 0:
        tools.out("Whether the plugin is installed could not be read. If you use it, run:")
        for argv in PLUGIN_STEPS:
            tools.out("  " + " ".join(argv))
        return 0
    if PLUGIN_ID not in listing.output:
        tools.out("The ModelQuad plugin is not installed in Claude Code; nothing to refresh.")
        return 0
    for argv in PLUGIN_STEPS:
        tools.out("> " + " ".join(argv))
        result = tools.run(argv)
        _echo(tools, result)
        if result.code != 0:
            tools.out(f"Stopped: that command failed (exit {result.code}). Run the remaining "
                      "plugin commands yourself:")
            for rest in PLUGIN_STEPS[PLUGIN_STEPS.index(argv):]:
                tools.out("  " + " ".join(rest))
            return 1
    return 0
