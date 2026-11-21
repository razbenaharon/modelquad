# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``python -m modelquad update``: upgrade the package and refresh the plugin, in one
step the user starts.

There is no automatic update. ModelQuad has no background service, its own
processes connect only to loopback (``netpolicy.py``), and Windows does not
let pip replace a program that is running: ``modelquad-mcp.exe`` while
Claude Code runs it, and ``modelquad.exe`` itself. An upgrade started then
fails halfway and leaves ModelQuad uninstalled. So the update refuses:

* unless it was started as ``python -m modelquad update``, so that no
  ``modelquad.exe`` of this install is running (it prints that exact command);
* while Claude Code, the Claude app, ``modelquad-mcp.exe`` or another
  ``modelquad.exe`` is running;
* in a ModelQuad worker (AGENTS.md invariant 1).

The network work is done by child processes, pip and git, and Claude Code's
own ``claude plugin`` commands, which go online themselves. ModelQuad's own
process opens no socket here; the loopback guard stays on.

How the package was installed decides the commands. From PyPI:
``python -P -m pip install --upgrade modelquad`` with this same interpreter.
From a clone (a Git checkout installed with ``pip install .``, or an editable
install): ``git pull --ff-only`` in that checkout, only on a clean ``main``,
then ``pip install .`` (``pip install -e .`` for an editable one). When it
cannot tell, it says so and prints both, and runs neither. Every child
Python runs with ``-P``, so the current folder is not put first on its
import path.

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
#: ModelQuad's MCP server and the modelquad command itself.
BLOCKING_PROCESSES = ("claude.exe", "modelquad-mcp.exe", "modelquad.exe")
PLUGIN_ID = f"{PLUGIN_NAME}@{PLUGIN_NAME}"
#: The branch a clone is updated on.
BRANCH = "main"

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
    """The blocking processes that are running, by image name; None when unknown.
    A modelquad.exe that is this process or one that started it is left out;
    Claude Code and the Claude app count even when they started this one."""
    if os.name != "nt":
        return None
    from .procutil import _windows_process_snapshot

    snapshot = _windows_process_snapshot()
    if snapshot is None:
        return None
    parents = {pid: parent for pid, parent, _image in snapshot}
    mine = {os.getpid()}
    current = os.getpid()
    while current in parents and parents[current] not in mine and len(mine) < 64:
        current = parents[current]
        mine.add(current)
    wanted = {name.casefold() for name in BLOCKING_PROCESSES}
    return sorted({image for pid, _parent, image in snapshot
                   if image.casefold() in wanted
                   and not (image.casefold() == "modelquad.exe" and pid in mine)})


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


def _which(name: str) -> tuple[str | None, str | None]:
    """The program's path, or None and why (``not_found``, ``batch_script``, ...)."""
    from .executable_resolution import resolve_executable

    found = resolve_executable(name)
    return found.path, found.refusal


def _is_worker() -> bool:
    from .mcp import worker_guard

    return worker_guard.is_worker()


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


def started_as_module(argv0: str) -> bool:
    """Whether this process is ``python -m modelquad``: then no modelquad.exe of this
    install is running for pip to fail to replace."""
    path = Path(argv0)
    return path.name == "__main__.py" and path.parent.name == "modelquad"


@dataclass
class Tools:
    """Every outside effect of the update; tests replace them."""

    running: Callable[[], list[str] | None]
    run: Callable[..., Result]
    which: Callable[[str], tuple[str | None, str | None]]
    install: Callable[[], Install]
    worker: Callable[[], bool]
    python: str = field(default_factory=lambda: sys.executable)
    argv0: str = field(default_factory=lambda: sys.argv[0] if sys.argv else "")
    out: Callable[[str], None] = print


def default_tools() -> Tools:
    """The real ones, looked up when the update starts."""
    return Tools(running=_blocking_running, run=_run, which=_which, install=detect_install,
                 worker=_is_worker)


def _quoted(part: str) -> str:
    return f'"{part}"' if " " in part or "\\" in part else part


def _show(argv: list[str], cwd: Path | None = None) -> str:
    text = " ".join(_quoted(part) for part in argv)
    return f"{text}   (in {cwd})" if cwd else text


def module_command(python: str) -> str:
    """The exact command that runs the update without modelquad.exe."""
    return f'"{python}" -m modelquad update'


def package_steps(install: Install, python: str) -> list[tuple[list[str], Path | None]]:
    """The commands that upgrade the package, with the folder each runs in."""
    if install.kind == PYPI:
        return [([python, "-P", "-m", "pip", "install", "--upgrade", DISTRIBUTION_NAME], None)]
    if install.kind in (CLONE, EDITABLE):
        pip = ["-e", "."] if install.kind == EDITABLE else ["."]
        return [(["git", "pull", "--ff-only"], install.folder),
                ([python, "-P", "-m", "pip", "install", *pip], install.folder)]
    return []


MARKETPLACE_STEP = ["claude", "plugin", "marketplace", "update", PLUGIN_NAME]


def plugin_steps(scope: str = "user") -> list[list[str]]:
    """The plugin refresh by hand, for one installation scope."""
    return [MARKETPLACE_STEP, ["claude", "plugin", "update", PLUGIN_ID, "--scope", scope]]


def _fallback(tools: Tools) -> None:
    tools.out("Upgrade by hand instead, with Claude Code and the Claude app closed:")
    tools.out("  From PyPI:     " + _show(package_steps(Install(PYPI), tools.python)[0][0]))
    tools.out(f"  From a clone:  git pull --ff-only on a clean {BRANCH}, then "
              + _show([tools.python, "-P", "-m", "pip", "install", "."]) + ", in the clone")
    _manual_plugin(tools, "user", "  Then refresh the plugin:")
    tools.out("  Then: modelquad doctor")


def _manual_plugin(tools: Tools, scope: str, heading: str) -> None:
    tools.out(heading)
    for argv in plugin_steps(scope):
        tools.out("    " + " ".join(argv))


def _echo(tools: Tools, result: Result) -> None:
    for line in result.output.rstrip().splitlines():
        tools.out("  " + line)


def _clone_ready(tools: Tools, folder: Path | None) -> str | None:
    """Why ``git pull --ff-only`` must not run in the clone now; None when it may."""
    status = tools.run(["git", "status", "--porcelain"], cwd=folder)
    if status.code != 0:
        return "Git could not say whether the clone has local changes"
    if status.output.strip():
        return "the clone has local changes"
    branch = tools.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=folder)
    name = branch.output.strip()
    if branch.code != 0 or not name:
        return "Git could not say which branch the clone is on"
    if name != BRANCH:
        return f"the clone is on {name[:60]}, not {BRANCH}"
    return None


def update(dry_run: bool = False, tools: Tools | None = None) -> int:
    """Run the update, or with ``dry_run`` only print its steps. Returns the exit code."""
    tools = tools or default_tools()
    install = tools.install()
    steps = package_steps(install, tools.python)
    running = tools.running()
    as_module = started_as_module(tools.argv0)

    if dry_run:
        tools.out("The update would do this (nothing is run now):")
        if not as_module:
            tools.out("  First it would stop: run it as " + module_command(tools.python)
                      + ", not through modelquad.exe, which pip would have to replace.")
        if running:
            tools.out("  First it would stop, because this is running: " + ", ".join(running))
        elif running is None:
            tools.out("  First it would stop: whether Claude Code is running could not be checked.")
        if not steps:
            tools.out(f"  It cannot tell how ModelQuad was installed ({install.why}), so it "
                      "would run nothing and print the commands.")
        if install.kind in (CLONE, EDITABLE):
            tools.out(f"  It checks that the clone is on {BRANCH} with no local changes.")
        for argv, cwd in steps:
            tools.out("  " + _show(argv, cwd))
        tools.out("  If claude plugin list --json shows the plugin installed and enabled:")
        for argv in plugin_steps("<its scope>"):
            tools.out("    " + " ".join(argv))
        tools.out("  Then the checks of modelquad doctor, and the new version.")
        tools.out("pip and git connect to the internet themselves (PyPI, or the clone's Git remote); "
                  "ModelQuad does not.")
        return 0

    if tools.worker():
        tools.out("Not updated: a ModelQuad worker does not run the update.")
        return 1
    if not as_module:
        tools.out("Not updated: started through modelquad.exe, which pip would have to replace "
                  "while it runs; that leaves ModelQuad uninstalled. Run this instead:")
        tools.out("  " + module_command(tools.python))
        return 1
    if running is None:
        tools.out("Not updated: whether Claude Code is running could not be checked.")
        tools.out("Close every Claude Code window and the Claude app, then update by hand.")
        _fallback(tools)
        return 1
    if running:
        tools.out("Not updated: close these first, then run the update again:")
        for name in running:
            tools.out(f"  {name}")
        tools.out("A running Claude Code (claude.exe), the Claude app (Claude.exe), ModelQuad's "
                  "server (modelquad-mcp.exe) or another modelquad command (modelquad.exe) keeps "
                  "ModelQuad's programs open, and Windows does not let pip replace them: the "
                  "upgrade would stop halfway and leave ModelQuad uninstalled. In a Claude Code "
                  "terminal session, type /exit.")
        return 1
    if not steps:
        tools.out(f"Not updated: ModelQuad cannot tell how it was installed ({install.why}).")
        _fallback(tools)
        return 1
    if install.kind in (CLONE, EDITABLE):
        why = _clone_ready(tools, install.folder)
        if why is not None:
            tools.out(f"Not updated: {why}. Update the clone yourself, then install it:")
            for argv, cwd in steps:
                tools.out("  " + _show(argv, cwd))
            _manual_plugin(tools, "user", "Then refresh the plugin:")
            return 1

    where = "PyPI" if install.kind == PYPI else f"the clone in {install.folder}"
    tools.out(f"Updating ModelQuad from {where}. pip" + (" and git" if install.kind != PYPI else "")
              + " connect to the internet now; ModelQuad itself does not.")
    for argv, cwd in steps:
        tools.out("> " + _show(argv, cwd))
        result = tools.run(argv, cwd=cwd)
        _echo(tools, result)
        if result.code != 0:
            if argv[0] == "git":
                tools.out(f"Stopped: git pull did not finish (exit {result.code}). Nothing was "
                          "installed. Check the clone, then run the update again.")
            else:
                tools.out(f"Stopped: pip did not finish (exit {result.code}). ModelQuad may now "
                          "be only partly installed. Run the same command again in this window:")
                tools.out("  " + _show(argv, cwd))
            return 1

    plugin_ok = _refresh_plugin(tools)
    tools.out("> modelquad doctor")
    doctor = tools.run([tools.python, "-P", "-c",
                        "from modelquad.cli_entry import main; raise SystemExit(main(['doctor']))"])
    _echo(tools, doctor)
    version = tools.run([tools.python, "-P", "-c", "import modelquad; print(modelquad.__version__)"])
    shown = version.output.strip() if version.code == 0 and version.output.strip() else "unknown"
    tools.out(f"The package was upgraded: ModelQuad is now version {shown}.")
    if not plugin_ok:
        tools.out("The Claude Code plugin was NOT refreshed (see above), so Claude Code may "
                  "still run the old plugin with the new package.")
    if doctor.code != 0:
        problems = [line.strip() for line in doctor.output.splitlines() if line.startswith("[PROBLEM]")]
        tools.out("The upgrade itself succeeded, but modelquad doctor found a problem"
                  + (": " + " ".join(problems) if problems else f" (exit {doctor.code})") + ".")
    if plugin_ok and doctor.code == 0:
        tools.out("Start Claude Code again to use it.")
        return 0
    return 1


def _plugin_entries(text: str) -> list[dict] | None:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict) and item.get("id") == PLUGIN_ID]


def _refresh_plugin(tools: Tools) -> bool:
    """Update the plugin in Claude Code so it copies the new files; True when it was
    refreshed or is not installed, False when it was skipped or failed."""
    path, refusal = tools.which("claude")
    if path is None:
        if refusal == "batch_script":
            tools.out("The claude command was found, but it is a batch script (claude.cmd), "
                      "which ModelQuad does not run, so the plugin was not refreshed.")
        else:
            tools.out("The claude program (claude.exe) was not found on PATH, so the plugin "
                      "was not refreshed.")
        _manual_plugin(tools, "user", "If you use the plugin, run:")
        return False
    listing = tools.run(["claude", "plugin", "list", "--json"])
    entries = _plugin_entries(listing.output) if listing.code == 0 else None
    if entries is None:
        tools.out("Whether the plugin is installed could not be read, so it was not refreshed.")
        _manual_plugin(tools, "user", "If you use the plugin, run:")
        return False
    if not entries:
        tools.out("The ModelQuad plugin is not installed in Claude Code; nothing to refresh.")
        return True
    tools.out("> " + " ".join(MARKETPLACE_STEP))
    result = tools.run(MARKETPLACE_STEP)
    _echo(tools, result)
    if result.code != 0:
        tools.out(f"Stopped: the marketplace update failed (exit {result.code}); the plugin "
                  "was not refreshed.")
        return False
    refreshed = True
    for entry in entries:
        scope = entry.get("scope") if isinstance(entry.get("scope"), str) else "unknown"
        if entry.get("enabled") is False:
            tools.out(f"The plugin is disabled (scope {scope}); it was not refreshed and stays "
                      "disabled. ModelQuad does not turn it back on.")
            refreshed = False
            continue
        if scope != "user":
            tools.out(f"The plugin is installed for scope {scope}; refresh it in that project "
                      "yourself:")
            tools.out("  " + " ".join(plugin_steps(scope)[1]))
            refreshed = False
            continue
        if not _update_one(tools, scope):
            refreshed = False
    return refreshed


def _update_one(tools: Tools, scope: str) -> bool:
    update = ["claude", "plugin", "update", PLUGIN_ID, "--scope", scope]
    tools.out("> " + " ".join(update))
    result = tools.run(update)
    _echo(tools, result)
    if result.code == 0:
        return True
    # Fallback: reinstall, keeping the plugin's saved data.
    for argv in (["claude", "plugin", "uninstall", PLUGIN_ID, "--scope", scope, "--keep-data"],
                 ["claude", "plugin", "install", PLUGIN_ID, "--scope", scope]):
        tools.out("> " + " ".join(argv))
        result = tools.run(argv)
        _echo(tools, result)
        if result.code != 0:
            tools.out(f"Stopped: that command failed (exit {result.code}); the plugin was not "
                      "refreshed. Run it yourself:")
            tools.out("  claude plugin install " + PLUGIN_ID + " --scope " + scope)
            return False
    return True
