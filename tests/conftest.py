# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Test-wide isolation: never resolve a real user directory, never inherit a flag."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# The MCP server reads the user's standing grant when it is imported (grant.py).
# A grant on the developer's own machine must never change what the suite sees.
os.environ["MODELQUAD_NO_STANDING_GRANT"] = "1"

from modelquad import paths

if sys.platform == "win32":
    # A test that faults (a bad native call) must fail in the log, never open a
    # Windows "Application Error" dialog on the desktop. The error mode is
    # inherited by child processes. SEM_FAILCRITICALERRORS |
    # SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX.
    import ctypes as _ctypes

    _ctypes.WinDLL("kernel32").SetErrorMode(0x0001 | 0x0002 | 0x8000)
from modelquad import disclosure
from modelquad.execution.base import WORKER_ENV_VAR

#: Set to run the ``ci_only`` tests locally; CI (``CI=true``) always runs them.
FULL_SUITE_ENV_VAR = "MODELQUAD_FULL_SUITE"


def pytest_collection_modifyitems(config, items):
    """Skip ``ci_only`` tests in a local run.

    They check the repository and its packaging (a wheel built and installed,
    a real virtual environment, scans of every test file) rather than product
    behaviour, and take minutes. CI runs them on every push.
    """
    if os.environ.get("CI", "").casefold() == "true" or os.environ.get(FULL_SUITE_ENV_VAR) == "1":
        return
    skip = pytest.mark.skip(reason=f"ci_only: runs in CI, or locally with {FULL_SUITE_ENV_VAR}=1")
    for item in items:
        if item.get_closest_marker("ci_only") is not None:
            item.add_marker(skip)


@pytest.fixture
def synthetic_worker_binary(tmp_path_factory):
    """Real, inert files for injected worker lookups that must pass admission."""
    # The requested workspace is tmp_path and is excluded by real executable
    # admission. Model an installation beside it, never inside that subtree;
    # keep mixed casing to exercise Windows canonical-path identity too.
    directory = tmp_path_factory.mktemp("private Program Files worker install")

    def create(name="codex.exe"):
        path = directory / name
        if not path.exists():
            path.write_bytes(b"synthetic fixture - never execute\n")
            path.chmod(0o755)
        return os.path.normcase(os.path.realpath(path))

    return create


@pytest.fixture
def qualified_codex_inventory(monkeypatch):
    """Opt-in no-shell qualification for inert native-worker integration fixtures."""
    from modelquad.execution import codex, noshell

    monkeypatch.setattr(codex, "_probe", lambda executable: "0.160.0")
    # Synthetic binaries have identical bytes. Keep their digest/version cache
    # scoped to this test so it cannot qualify another test's unknown CLI.
    monkeypatch.setattr(noshell, "_QUALIFICATIONS", {})

    def inventory(argv, cwd, env):
        if argv[1:3] == ["features", "list"]:
            # Match the observed listing: unified_exec reports true even with
            # its disable flag; every feature required false is reported false.
            output = "\n".join(
                f"{feature} under development {str(feature == 'unified_exec').lower()}"
                for feature in noshell.DISABLED_FEATURES
            ) + "\n"
        else:
            output = "[]\n"
        return subprocess.CompletedProcess(list(argv), 0, output, "")

    return inventory


def codex_event_stream(text="ok", *, error=None):
    """Tool-free no-shell JSONL; plain answer text deliberately has no task envelope."""
    events = [{"type": "thread.started", "thread_id": "inert"}, {"type": "turn.started"}]
    if error is None:
        events += [
            {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
    else:
        events.append({"type": "turn.failed", "error": {"type": error}})
    return "\n".join(json.dumps(event) for event in events) + "\n"


# ------------------------------------------- the suite's own spawns stay hidden

#: The flag by value, not by import. ``subprocess.CREATE_NO_WINDOW`` only
#: exists on Windows, and this module is imported on POSIX too; the product's
#: ``procutil`` resolves it exactly this way, and
#: ``tests/test_procutil.py::test_the_test_helper_agrees_with_the_products_flag``
#: pins the two together so they cannot drift apart silently.
CREATE_NO_WINDOW = 0x08000000


def no_window_kwargs(os_name: str | None = None) -> dict[str, object]:
    """Keyword arguments that keep a *test's* child process off the desktop.

    The suite spawns for real -- git repositories, `scripts/` under a live
    interpreter, throwaway children whose pid must be genuinely dead -- and on
    Windows a console application launched by a parent that has no console of
    its own is given a brand new one, with a window. Whether the suite's parent
    has a console is not a property of any test: it is a property of whoever
    typed the command. Under a terminal every spawn silently inherits that
    terminal's console and nothing appears; under an IDE test runner, a
    scheduled task, an agent harness or any other `DETACHED_PROCESS` launcher,
    each one allocates. Measured on Windows 11 with `scripts/windowwatch.py`:
    three of these files under a console-less parent produced 3,559
    console-class windows in 300 seconds.

    So the flag is not a property of the launcher either -- it belongs at the
    call site, on every one of them, which is what
    `tests/test_procutil.py::test_no_subprocess_spawn_in_tests_omits_window_suppression`
    enforces.

    **Creation flags only.** The product's `procutil.no_window_kwargs` also
    detaches stdin, because a hidden *product* child that prompts would hang
    invisibly forever. This one deliberately does not: the Task that added it
    is bounded to window creation, several call sites here drive the child's
    stdin themselves (`subprocess.PIPE` for a protocol, `input=` for a script),
    and a helper that quietly redirected a handle would be changing what the
    tests test. `os_name` exists so both branches can be exercised without
    patching the global `os.name`, which breaks `pathlib`.
    """

    if (os.name if os_name is None else os_name) != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW)}


@pytest.fixture(autouse=True)
def _loaded_runner_git_headroom():
    """Suite-wide headroom for Git-backed identity and observation budgets.

    The self-hosted preflight runs four xdist workers on a developer PC
    (six before 2026-10-15), and runs on 7-8 October lost a different Git-timing test each time
    (repository identity under the 20 s resolve / 15 s per-call budgets). The
    claims are identities and outcomes, not speed. Tests that exercise these
    budgets set their own values with monkeypatch, which wins. This fixture
    saves and restores by itself instead of requesting monkeypatch, so it does
    not move monkeypatch's teardown ahead of other fixtures' teardown."""
    from modelquad import gitmeta as changes
    saved = [(changes, "GIT_SECONDS", changes.GIT_SECONDS)]  # _git clamps to its own module value
    changes.GIT_SECONDS = 60
    try:
        yield
    finally:
        for module, name, value in saved:
            setattr(module, name, value)


@pytest.fixture(autouse=True)
def isolate_worker_recursion_guard(monkeypatch):
    """The suite states whether it is a delegated worker; it never inherits it.

    ``MODELQUAD_WORKER=1`` is ModelQuad's recursion guard (``AGENTS.md``
    invariant 1): every delegation entry point refuses outright when it is set.
    The guard is correct. What was wrong is that the suite read it from the
    ambient environment, so 77 tests that assert *successful* delegation got a
    policy refusal instead and failed -- while the same tests passed on a shell
    that did not have it set.

    That was not a hypothetical shell. The former development harness set it in
    every agent session it launched, and then told that session its required
    validation was ``python scripts/check.py all`` -- a command that could not go
    green in that environment. An honest worker reported a failure it did not
    cause; a careless one reported green.

    So the variable is cleared here, for every test, unconditionally. A test
    that wants worker semantics sets it itself with ``monkeypatch.setenv`` --
    ``tests/test_mcp_worker_guard.py`` does exactly that at each entry point, so
    clearing it here cannot quietly delete the guard's coverage.

    ``MODELQUAD_DEV_MODE`` and ``MODELQUAD_DEV`` are deliberately left
    alone: both were run against this suite and neither changes a single
    result; a fixture that neutralises a variable nothing reads is a claim
    with no test behind it.
    """

    monkeypatch.delenv(WORKER_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def isolate_loopback_guard():
    """No test inherits the loopback guard another test's entry point turned on.

    ``netpolicy.install_loopback_guard`` is process-scoped by design: an audit
    hook cannot be removed. In the suite, "the process" is the whole pytest
    run, so a test that calls ``cli_entry.main`` would otherwise leave every
    later test refusing non-loopback sockets. The hook stays installed; this
    switches its refusal back off after each test.
    """

    from modelquad import netpolicy

    netpolicy.guard_active = False
    try:
        yield
    finally:
        netpolicy.guard_active = False


@pytest.fixture(autouse=True)
def isolate_home_directory(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))
    # LOCALAPPDATA is isolated too. The state folder no longer lives under it
    # (statecontext.installed_state_root), but Codex discovery and the setup
    # checks still read it, and this fixture's promise is that no test ever
    # resolves a real user directory.
    monkeypatch.setenv("LOCALAPPDATA", str(fake_home / "AppData" / "Local"))
    # A fresh home has never seen the data-flow notice, so every Codex call
    # would return the notice instead. The suite tests calls, not the notice:
    # treat it as shown. tests/test_first_use_notice.py restores the real check.
    monkeypatch.setattr(disclosure, "notice_shown", _notice_already_shown)
    return fake_home


def _notice_already_shown() -> bool:
    return True


@pytest.fixture(autouse=True)
def isolate_provider_cli_spawns(monkeypatch):
    """Never let a test shell out to a provider CLI installed on this machine.

    Results must not depend on which AI CLIs the developer happens to have
    installed, so the suite means the same thing on every machine and in CI,
    where none are.
    """

    # Codex resolution prefers an official desktop install over PATH
    # (codexcli.py), so the same rule applies: a developer machine with the
    # Codex desktop app would resolve differently from CI. Tests see no
    # official install unless they inject candidates themselves.
    from modelquad import codexcli

    monkeypatch.setattr(codexcli, "official_candidates", lambda: [])
    codexcli.clear_cache()

    # The Codex executor runs `codex features list` before each launch, so
    # the same rule applies: a developer machine's Codex must not qualify a
    # test's worker. Tests default to an unusable listing (no launch);
    # integration tests opt in with `qualified_codex_inventory`.
    import subprocess

    from modelquad.execution import codex as codex_module

    monkeypatch.setattr(
        codex_module, "run_inventory",
        lambda argv, cwd, env: subprocess.CompletedProcess(list(argv), 0, "[]\n", ""),
    )


def tool_payload(result) -> object:
    """The machine-readable payload of an MCP tool result, whichever channel it used.

    Two shapes coexist deliberately:

    - Some tools return a plain ``dict``. The SDK JSON-dumps it into the
      result's single text block and sets no ``structuredContent``.
    - Others (``delegate`` among them) return a ``CallToolResult``
      whose text block is a short human summary and whose
      ``structuredContent`` carries the canonical object; their text is not
      JSON.

    Tests asserting on the *human-visible* text must read ``result.content``
    directly -- this helper is for the data half.
    """

    import json

    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    return json.loads(result.content[0].text)
