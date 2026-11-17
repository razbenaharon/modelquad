# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for process utilities and console suppression across all subprocess call sites."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import modelquad.execution as exec_mod
import modelquad.execution.base as exec_base
import modelquad.procutil as procutil
from modelquad.procutil import no_window_kwargs, run_with_tree_kill


class RecordingRun:
    """Mock runner recording argv and keyword arguments."""

    def __init__(
        self,
        result: subprocess.CompletedProcess | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        self.exc = exc

    def __call__(self, argv: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append({"argv": argv, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return self.result


# --- 1. Canonical helper and re-exports ---------------------------------------

def test_canonical_no_window_kwargs_branches():
    """Both branches are exercised via explicit os_name without global mutation."""
    nt_kwargs = procutil.no_window_kwargs("nt")
    assert nt_kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert nt_kwargs["creationflags"] & 0x08000000 != 0
    assert nt_kwargs["stdin"] is subprocess.DEVNULL

    assert procutil.no_window_kwargs("posix") == {"stdin": subprocess.DEVNULL}


def test_canonical_no_window_kwargs_fallback_when_attribute_missing(monkeypatch):
    """When subprocess lacks CREATE_NO_WINDOW, fallback literal 0x08000000 is used."""
    monkeypatch.setattr(procutil, "subprocess", object())
    assert procutil.no_window_kwargs("nt") == {"stdin": subprocess.DEVNULL, "creationflags": 0x08000000}


def test_no_window_kwargs_opt_out_drops_only_stdin():
    """The opt-out exists for callers that drive stdin themselves.

    subprocess.run refuses ``input=`` alongside ``stdin``, and the app-server
    hands the child a real pipe. Both are non-interactive by construction, so
    they may skip the detachment -- but they must not lose CREATE_NO_WINDOW
    with it.
    """

    posix = procutil.no_window_kwargs("posix", detach_stdin=False)
    assert posix == {}

    nt = procutil.no_window_kwargs("nt", detach_stdin=False)
    assert "stdin" not in nt
    assert nt["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def test_run_with_tree_kill_detaches_a_child_from_inherited_stdin():
    """A child that reads stdin must see EOF, not a hidden console prompt."""

    completed = run_with_tree_kill(
        [sys.executable, "-c", "import sys; print('detached' if sys.stdin.read() == '' else 'inherited')"],
        capture_output=True,
        text=True,
        # Python startup needs slack on the shared, self-hosted Windows runner.
        timeout=30.0,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "detached"


def test_a_product_spawned_helper_that_reads_stdin_cannot_block_on_it():
    """The live hang, in the closest shape repository tooling can reach.

    In Windows Sandbox ``taskkill`` failed with error 1326 and then prompted
    for credentials on the console stdin it had inherited from the engine that
    Inno had started with SW_HIDE. ``subprocess.run`` had no timeout and never
    returned, so the wizard sat at "Preparing to Install" and the user-message
    file was never written.

    A test cannot hand pytest a hidden console, so it asserts the property that
    makes the prompt impossible: a child spawned through the canonical kwargs
    reads EOF rather than an inherited handle. The ``timeout`` is the failure
    mode itself -- if stdin were ever inherited from a terminal, this blocks.
    """

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('eof' if sys.stdin.read() == '' else 'inherited')",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        **no_window_kwargs(),
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "eof"


def test_no_window_kwargs_reexported_identically():
    """All re-exports must refer to the exact same canonical function object."""
    assert exec_base.no_window_kwargs is procutil.no_window_kwargs
    assert exec_mod.no_window_kwargs is procutil.no_window_kwargs


# --- 5. Window-suppression invariant -----------------------------------------

#: ``check_call`` was once missing here, while the ``scripts/`` scan in
#: ``test_scripts_no_window.py`` had always listed it. A spawn method the scan
#: does not name is a spawn the invariant does not cover, and the two scans
#: disagreeing about which methods exist is the quietest way to grow one.
SPAWN_METHODS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _subprocess_module_names(scope: ast.AST) -> frozenset[str]:
    """Every name the ``subprocess`` module is reachable under inside one file.

    ``import subprocess as sp`` is not a stylistic variant -- it is a hole. The
    scan matched the literal name only, so ``sp.Popen([sys.executable, "-c",
    "pass"])`` in ``tests/test_procutil_tree_kill.py`` -- the same dead-pid
    idiom fixed in five other files -- was invisible to it, and the ``tests/``
    scan reported green over an unsuppressed child of pytest. Found in review of
    this Task.

    Collected over the whole file rather than per scope, because four of the
    five aliased imports in ``tests/`` are *function-local*; a module-level walk
    that respected scope would have to model imports it can see no other way.
    The cost of the wider net is over-matching -- an unrelated local named
    ``sp`` would be read as the module -- which flags a call that did not need
    the flag. That direction is safe: it fails loudly and is fixed by passing
    the flag anyway. The other direction is this defect.
    """

    names = {"subprocess"}
    for node in ast.walk(scope):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and alias.asname:
                    names.add(alias.asname)
    return frozenset(names)


#: What :func:`_is_spawn_attribute` matches when no file has been read yet.
DEFAULT_MODULE_NAMES = frozenset({"subprocess"})


def _is_spawn_attribute(node: ast.AST, module_names: frozenset[str] = DEFAULT_MODULE_NAMES) -> bool:
    """True for a direct ``subprocess.run``/``Popen``/``call``/``check_output`` reference.

    ``module_names`` is the file's own answer to "what is ``subprocess`` called
    here", from :func:`_subprocess_module_names`.
    """

    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in module_names
        and node.attr in SPAWN_METHODS
    )


def _spawn_aliases(scope: ast.AST, module_names: frozenset[str] | None = None) -> set[str]:
    """Names bound to a spawn function inside one scope.

    This codebase almost never calls ``subprocess.run`` directly. The dominant
    convention is an injected test seam -- ``run: Callable[...] = subprocess.run``
    as a keyword-only parameter, invoked as a bare ``run(...)`` -- and a scan
    that only recognised the literal attribute form would silently skip
    the executor and every other seam written that way. That is worse than no test: it reports green while the
    invariant it claims to protect is unenforced. Verified by deleting the flag
    from one such call site and watching the attribute-only version still pass.
    """

    if module_names is None:
        module_names = _subprocess_module_names(scope)
    aliases: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            # ``from subprocess import Popen`` -- the third spelling, and the
            # last one. Nothing in the repository uses it today; it is here
            # because the alias form was not used either until it was, and the
            # cost of the scan learning about a spelling only after a spawn is
            # written in it is measured in console windows.
            for imported in node.names:
                if imported.name in SPAWN_METHODS:
                    aliases.add(imported.asname or imported.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            # Positional defaults bind to the TAIL of posonlyargs + args, and
            # kw_defaults bind 1:1 with kwonlyargs (None where required). Pairing
            # them any more loosely would mark every sibling parameter as a spawn
            # alias and flag innocent calls like ``which(binary)``.
            positional = list(args.posonlyargs) + list(args.args)
            for parameter, default in zip(positional[len(positional) - len(args.defaults):], args.defaults):
                if _is_spawn_attribute(default, module_names):
                    aliases.add(parameter.arg)
            for parameter, default in zip(args.kwonlyargs, args.kw_defaults):
                if default is not None and _is_spawn_attribute(default, module_names):
                    aliases.add(parameter.arg)
        elif isinstance(node, ast.Assign):
            # e.g. ``spawner = _spawn_detached if ... else subprocess.Popen``
            #
            # A ``lambda`` is the one assigned value that is NOT an alias: it is
            # a wrapper, and the spawn inside it is a call site of its own that
            # this scan already visits. Binding the wrapper's name too moved the
            # finding onto every ``g("init")`` in a since-removed deploy test
            # -- six reports of one defect, none of them at the line where the
            # flag has to be written, and none fixable where they were reported
            # because ``lambda *a`` accepts no keywords. Nothing is lost by
            # skipping it: flag the inner spawn and every call through the
            # wrapper is hidden with it.
            if isinstance(node.value, ast.Lambda):
                continue
            if any(_is_spawn_attribute(inner, module_names) for inner in ast.walk(node.value)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases.add(target.id)
    return aliases


def _names_carrying_creationflags(scope: ast.AST) -> set[str]:
    """Dict names that get a ``creationflags`` entry assigned somewhere in ``scope``.

    The retired ``product/uninstaller.py`` built ``popen_kwargs``
    incrementally -- it had to, because it ORed DETACHED_PROCESS and
    CREATE_NEW_PROCESS_GROUP together with CREATE_NO_WINDOW behind a
    ``sys.platform`` check -- and then splatted it. That is a legitimate
    third spelling of the invariant, so the scan has to recognise it or it
    reports a false positive on correct code.
    """

    carriers: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "creationflags"
            ):
                carriers.add(target.value.id)
    return carriers


def _names_a_helper(expression: ast.AST) -> bool:
    """True for either spelling: ``no_window_kwargs`` or ``x.no_window_kwargs``.

    The attribute form is not a stylistic variant. ``tests/`` reaches the helper
    three ways -- the test-support copy (``from conftest import ...``), the
    product's (``procutil.no_window_kwargs``) and the harness's, loaded by path
    (``module.no_window_kwargs``, in ``tests/test_scripts_no_window.py``) -- and
    a scan that saw only the bare name reported eight call sites as missing the
    flag they were in fact passing.
    """

    return (isinstance(expression, ast.Name) and expression.id == "no_window_kwargs") or (
        isinstance(expression, ast.Attribute) and expression.attr == "no_window_kwargs"
    )


def _has_window_suppression(call: ast.Call, creationflag_carriers: set[str]) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "creationflags":
            return True
        if keyword.arg is not None:
            continue
        if any(_names_a_helper(expression) for expression in ast.walk(keyword.value)):
            return True
        if isinstance(keyword.value, ast.Name) and keyword.value.id in creationflag_carriers:
            return True
    return False


#: Written at the call site -- on the spawn, or in the comment block directly
#: above it -- by a spawn that must NOT be hidden. There are two in the whole
#: repository and :data:`CONSOLE_EXEMPT_FILES` names them, so the marker cannot
#: become a quiet way out: a third one fails the scan until this file is edited
#: on purpose.
CONSOLE_EXEMPT_MARKER = "# console-window-exempt:"

#: The files allowed to hold such a spawn, and why.
#:
#: * ``test_scripts_no_window.py`` -- the unflagged *control*. Its whole job is
#:   to allocate the console window the flagged arm must not have, and it runs
#:   only when this process already owns one for the child to attach to.
#: * ``test_rel1_worker_termination.py`` -- a monkeypatch shim standing in for
#:   ``subprocess.run`` itself, forwarding its caller's ``**kwargs``. The flag is
#:   the caller's (``procutil``'s, tested above); a second ``creationflags`` here
#:   would be a ``TypeError`` on every call it exists to pass through.
CONSOLE_EXEMPT_FILES = {
    "tests/test_rel1_worker_termination.py",
    "tests/test_scripts_no_window.py",
}


def _marked_exempt(lines: list[str], call: ast.Call) -> bool:
    """Whether :data:`CONSOLE_EXEMPT_MARKER` is on the call, or just above it."""

    span = list(lines[call.lineno - 1 : (call.end_lineno or call.lineno)])
    above = call.lineno - 2
    while above >= 0 and lines[above].lstrip().startswith("#"):
        span.append(lines[above])
        above -= 1
    return any(CONSOLE_EXEMPT_MARKER in line for line in span)


def _spawns_without_suppression(paths, repo_root: Path) -> tuple[list[str], list[str]]:
    """``(missing, exempt)`` over a set of files, by the rules above.

    One function for both scans below, so ``src/`` and ``tests/`` cannot come to
    disagree about what a spawn is -- which is exactly how ``check_call`` went
    unscanned.
    """

    missing: list[str] = []
    exempt: list[str] = []
    for source_path in paths:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        lines = source.splitlines()
        module_names = _subprocess_module_names(tree)
        aliases = _spawn_aliases(tree, module_names)
        carriers = _names_carrying_creationflags(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            direct = _is_spawn_attribute(node.func, module_names)
            aliased = isinstance(node.func, ast.Name) and node.func.id in aliases
            if not (direct or aliased):
                continue
            if _has_window_suppression(node, carriers):
                continue
            where = f"{source_path.relative_to(repo_root).as_posix()}:{node.lineno}"
            (exempt if _marked_exempt(lines, node) else missing).append(where)
    return missing, exempt


def test_no_subprocess_spawn_in_src_omits_window_suppression():
    """Every spawn in ``src/`` and ``scripts/`` must carry the console-suppression flag.

    Written as a source scan rather than per-call behavioral tests because the
    call sites live in modules with wildly different setup costs (a
    win32-only path, a real git repo, a live provider CLI), while the property
    itself -- no spawn without the flag -- is a whole-codebase invariant. A
    behavioral test per site would be expensive and would still leave the next
    new call site unguarded.
    """

    repo_root = Path(__file__).parents[1]
    src_root = repo_root / "src"
    # The development harness spawns too (every window once seen flashing
    # came from `scripts/`), and it carries its own inline copy of
    # the helper under the same name, so the same scan covers it unchanged.
    scanned = sorted(src_root.rglob("*.py")) + sorted((repo_root / "scripts").glob("*.py"))
    missing, exempt = _spawns_without_suppression(scanned, repo_root)

    assert not missing, f"subprocess spawn calls without window suppression: {missing}"
    # The product and the harness get no exemptions at all: every marked one is
    # in `tests/`, and is a test *about* console windows.
    assert not exempt, f"src/ and scripts/ may not carry a console-window exemption: {exempt}"


def test_the_spawn_scan_detects_an_aliased_call_site():
    """Guard the guard: prove the scan sees the injected-seam form.

    The first version of this scan only matched literal ``subprocess.run(...)``
    and therefore passed even after the flag was deleted from a real aliased
    call site. This locks that gap closed.
    """

    source = "\n".join(
        (
            "import subprocess",
            "def probe(*, run=subprocess.run):",
            "    return run(['x'], capture_output=True)",
        )
    )
    tree = ast.parse(source)
    aliases = _spawn_aliases(tree)
    assert "run" in aliases

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert calls and not _has_window_suppression(calls[0], set())


# --- 5b. The same invariant, over the suite's own spawns ----------------------

def test_no_subprocess_spawn_in_tests_omits_window_suppression():
    """The suite's own spawns are held to the rule the suite enforces on ``src/``.

    ``tests/`` was covered by neither this file's scan nor the one in
    ``test_scripts_no_window.py``, and 48 spawns in it carried nothing. That was
    harmless only for as long as pytest is started from something that already
    owns a console: on Windows a console application launched by a parent with
    no console of its own is given a brand new one, *with a window*, and every
    spawn underneath then inherits that. Measured with ``scripts/windowwatch.py``:
    ``test_worktree_lifecycle.py test_devshift_policy.py
    test_rel1_worker_termination.py`` from a ``DETACHED_PROCESS`` parent produced
    3,559 console-class windows in 300 seconds -- 890 WindowsTerminal, 325
    git.exe, 270 OpenConsole, 180 conhost.

    Whether the launcher has a console is not a property of any test, so the fix
    cannot live in the launcher. It lives at each call site, and this is what
    keeps it there: an IDE test runner, a scheduled task or a new tool reopens
    the storm the moment one spawn is written without the flag.
    """

    repo_root = Path(__file__).parents[1]
    scanned = sorted(
        path for path in (repo_root / "tests").rglob("*.py") if "__pycache__" not in path.parts
    )
    missing, exempt = _spawns_without_suppression(scanned, repo_root)

    assert not missing, f"test subprocess spawns without window suppression: {missing}"
    assert {site.rsplit(":", 1)[0] for site in exempt} == CONSOLE_EXEMPT_FILES, (
        f"the marked exemptions are no longer the two documented ones: {exempt}"
    )


def test_the_tests_scan_reads_both_helper_spellings_and_refuses_an_unmarked_spawn():
    """Guard the guard, for the two rules the ``tests/`` scan needed.

    Both were real. The attribute spelling made the scan report call sites that
    *were* passing the flag; the lambda rule decides whether the finding lands on
    the wrapper's definition, where it can be fixed, or on each call through it,
    where it cannot.
    """

    source = "\n".join(
        (
            "import subprocess",
            "g = lambda *a: subprocess.run(['git', *a])",
            "def probe():",
            "    g('status')",
            "    subprocess.run(['x'], **procutil.no_window_kwargs())",
            "    subprocess.run(['y'])",
        )
    )
    tree = ast.parse(source)

    # The wrapper's name is not an alias, so `g('status')` is not a call site.
    assert _spawn_aliases(tree) == set()

    calls = {
        ast.get_source_segment(source, node): node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_spawn_attribute(node.func)
    }
    assert not _has_window_suppression(calls["subprocess.run(['git', *a])"], set())
    assert _has_window_suppression(calls["subprocess.run(['x'], **procutil.no_window_kwargs())"], set())
    assert not _has_window_suppression(calls["subprocess.run(['y'])"], set())

    # And the marker is read from the comment block above a call, not only from
    # the call's own lines -- which is where both real exemptions carry it.
    marked = "\n".join(("import subprocess", CONSOLE_EXEMPT_MARKER + " why", "subprocess.run(['z'])"))
    marked_tree = ast.parse(marked)
    call = next(n for n in ast.walk(marked_tree) if isinstance(n, ast.Call))
    assert _marked_exempt(marked.splitlines(), call)
    assert not _marked_exempt(source.splitlines(), calls["subprocess.run(['y'])"])


def test_the_tests_scan_sees_a_spawn_through_an_aliased_import():
    """Guard the guard: ``import subprocess as sp`` is the same module.

    Review of this Task found the hole in the real suite, not in theory:
    ``tests/test_procutil_tree_kill.py`` spawned ``sp.Popen([sys.executable,
    "-c", "pass"])`` -- an unsuppressed, unguarded child of pytest, the same
    dead-pid idiom five other files carry the flag on -- and the ``tests/`` scan
    reported green over it purely because of the import spelling. Four of the
    five aliased imports in ``tests/`` reference ``CompletedProcess`` or
    monkeypatch only; nothing tells you which, so the scan may not depend on the
    name being spelled out.
    """

    source = "\n".join(
        (
            "import subprocess as sp",
            "def probe():",
            "    sp.Popen(['x'])",
            "    sp.run(['y'], **no_window_kwargs())",
        )
    )
    tree = ast.parse(source)
    module_names = _subprocess_module_names(tree)
    assert module_names == {"subprocess", "sp"}

    spawns = {
        ast.get_source_segment(source, node): node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_spawn_attribute(node.func, module_names)
    }
    assert set(spawns) == {"sp.Popen(['x'])", "sp.run(['y'], **no_window_kwargs())"}
    assert not _has_window_suppression(spawns["sp.Popen(['x'])"], set())
    assert _has_window_suppression(spawns["sp.run(['y'], **no_window_kwargs())"], set())

    # And a plain ``import subprocess`` binds no second name, so the wider net
    # is only as wide as the file actually asks for.
    assert _subprocess_module_names(ast.parse("import subprocess")) == {"subprocess"}
    assert not _is_spawn_attribute(spawns["sp.Popen(['x'])"].func, DEFAULT_MODULE_NAMES)

    # ``from subprocess import Popen`` lands in the alias set instead, so the
    # bare-name call is a call site like any other.
    from_form = ast.parse("from subprocess import Popen, CompletedProcess as C")
    assert _spawn_aliases(from_form) == {"Popen"}


def test_the_test_helper_agrees_with_the_products_flag():
    """``tests/conftest.py`` carries its own helper; it may not drift from this one.

    Separate on purpose -- the product's also detaches stdin, and several test
    call sites drive the child's stdin themselves (a pipe for a protocol,
    ``input=`` for a script) -- but the creation flag is one fact stated twice,
    and two copies of a fact are how one of them goes stale.
    """

    from conftest import no_window_kwargs as test_helper

    assert test_helper("nt") == {"creationflags": procutil.no_window_kwargs("nt")["creationflags"]}
    assert test_helper("posix") == {}
    # The product's stdin opt-out is exactly this helper's whole contract.
    assert test_helper("nt") == procutil.no_window_kwargs("nt", detach_stdin=False)


# --------------------------------------------------------------------------
# process_image_name: the identity half of "is that pid really
# the recorded service?"
# --------------------------------------------------------------------------


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_process_image_name_reads_this_process_for_real():
    import os

    name = procutil.process_image_name(os.getpid())
    assert name is not None
    assert name.casefold() == Path(sys.executable).name.casefold()


def test_process_image_name_spawns_its_lookup_without_a_console(tmp_path, monkeypatch):
    from modelquad.executable_resolution import Resolution

    executable = tmp_path / 'synthetic system' / 'tasklist.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'synthetic fixture - never execute\n')
    resolved = os.path.realpath(executable)

    def resolve(name):
        assert name == 'tasklist'
        return Resolution(resolved, 'system', 'binary', None)

    monkeypatch.setattr(procutil, 'system_executable', resolve)
    runner = RecordingRun(result=_completed('"python.exe","4242","Console","1","10,000 K"\n'))
    monkeypatch.setattr(procutil.os, "name", "nt")

    assert procutil.process_image_name(4242, run=runner) == "python.exe"

    call = runner.calls[0]
    assert os.path.normcase(call["argv"][0]) == os.path.normcase(resolved)
    assert call["kwargs"]["stdin"] is subprocess.DEVNULL
    assert call["kwargs"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert call["kwargs"]["shell"] is False


@pytest.mark.parametrize(
    "stdout",
    [
        "",  # nothing matched
        "INFO: No tasks are running which match the specified criteria.\n",  # not a CSV row for that pid
        '"python.exe","4242","Console","1","10,000 K"\n"python.exe","4243","Console","1","10,000 K"\n',  # two rows
        '"python.exe","9999","Console","1","10,000 K"\n',  # a different pid
    ],
)
def test_process_image_name_is_unknown_unless_exactly_that_pid_is_reported(monkeypatch, stdout):
    monkeypatch.setattr(procutil.os, "name", "nt")
    assert procutil.process_image_name(4242, run=RecordingRun(result=_completed(stdout))) is None


def test_process_image_name_is_unknown_when_the_lookup_fails(monkeypatch):
    monkeypatch.setattr(procutil.os, "name", "nt")
    assert procutil.process_image_name(4242, run=RecordingRun(result=_completed("", returncode=1))) is None
    assert procutil.process_image_name(4242, run=RecordingRun(exc=FileNotFoundError())) is None
    assert procutil.process_image_name(4242, run=RecordingRun(exc=subprocess.TimeoutExpired("tasklist", 15))) is None
    assert procutil.process_image_name(0, run=RecordingRun()) is None
    assert procutil.process_image_name("x", run=RecordingRun()) is None


def test_process_image_name_uses_ps_on_posix(monkeypatch):
    from modelquad.executable_resolution import Resolution

    resolved = "/usr/bin/ps"
    monkeypatch.setattr(procutil, "resolve_executable", lambda name: Resolution(resolved, "path", "binary", None))
    monkeypatch.setattr(procutil.os, "name", "posix")
    runner = RecordingRun(result=_completed("/usr/bin/python3.11\n"))

    assert procutil.process_image_name(4242, run=runner) == "python3.11"

    assert runner.calls[0]["argv"][:2] == [resolved, "-p"]
    assert "creationflags" not in runner.calls[0]["kwargs"]
    assert procutil.process_image_name(4242, run=RecordingRun(result=_completed("a\nb\n"))) is None



# --------------------------------------------------------------------------
# process_creation_epoch: the ordering half of identity
# --------------------------------------------------------------------------


def test_process_creation_epoch_reads_this_process_for_real():
    import os
    import time

    created = procutil.process_creation_epoch(os.getpid())
    assert created is not None
    now = time.time()
    assert created <= now + 1.0
    assert created > now - 7 * 24 * 3600, "this process was created recently, not last week"


def test_process_creation_epoch_is_unknown_for_no_such_process():
    assert procutil.process_creation_epoch(0) is None
    assert procutil.process_creation_epoch(-5) is None
    assert procutil.process_creation_epoch("x") is None


def test_process_creation_epoch_uses_elapsed_time_on_posix(monkeypatch):
    """Elapsed time needs no zone, locale or DST reading."""

    import time
    from modelquad.executable_resolution import Resolution

    resolved = "/usr/bin/ps"
    monkeypatch.setattr(procutil, "resolve_executable", lambda name: Resolution(resolved, "path", "binary", None))
    monkeypatch.setattr(procutil.os, "name", "posix")
    runner = RecordingRun(result=_completed("   01:02:03\n"))

    before = time.time()
    created = procutil.process_creation_epoch(4242, run=runner)
    after = time.time()

    elapsed = 1 * 3600 + 2 * 60 + 3
    assert before - elapsed <= created <= after - elapsed
    call = runner.calls[0]
    assert call["argv"] == [resolved, "-p", "4242", "-o", "etime="]
    assert call["kwargs"]["env"]["LC_ALL"] == "C"
    assert "TZ" not in call["kwargs"]["env"] or call["kwargs"]["env"]["TZ"] == procutil.os.environ.get("TZ")
    assert call["kwargs"]["stdin"] is subprocess.DEVNULL
    assert procutil.process_creation_epoch(4242, run=RecordingRun(result=_completed("garbage\n"))) is None
    assert procutil.process_creation_epoch(4242, run=RecordingRun(result=_completed("", returncode=1))) is None
    assert procutil.process_creation_epoch(4242, run=RecordingRun(exc=FileNotFoundError())) is None


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("00:07", 7.0),
        ("05:30", 330.0),
        ("01:02:03", 3723.0),
        ("2-01:02:03", 2 * 86400 + 3723.0),
        ("  3-00:00:01  ", 3 * 86400 + 1.0),
        ("garbage", None),
        ("1:2:3:4", None),
        ("x-01:02:03", None),
        ("", None),
        ("1-02:03", None),  # a day count needs hh:mm:ss
        ("00:60", None),  # out-of-range seconds
        ("60:00", None),  # out-of-range minutes
        ("1-24:00:00", None),  # out-of-range hours beside a day count
        ("1:02", None),  # fields are two digits
    ],
)
def test_parse_ps_etime_covers_every_ps_shape(text, seconds):
    assert procutil.parse_ps_etime(text) == seconds



# --------------------------------------------------------------------------
# What a worker left running when it exited
# --------------------------------------------------------------------------


def _worker_that_leaves_a_child(seconds: int = 60) -> str:
    """A worker script that spawns a detached child and exits, printing its pid."""

    import os

    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    return (
        "import subprocess, sys; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({seconds})'], creationflags={flags}, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); print(p.pid)"
    )


def test_run_with_tree_kill_reports_what_the_worker_left_running():
    """A real worker, a real detached child, a real process table -- no stubs.

    The worker exits 0 with its child still running. The result must name
    that child with its image and creation time and say enumeration
    happened, and nothing here may have stopped the child.
    """

    import os
    import time

    started = time.time()
    completed = run_with_tree_kill(
        [sys.executable, "-c", _worker_that_leaves_a_child()], capture_output=True, text=True, timeout=60
    )
    child = int(completed.stdout.strip())
    try:
        facts = completed.worker_descendants
        assert completed.returncode == 0
        assert started - 1 <= facts["observed_at"] <= time.time() + 1
        if os.name != "nt":
            # Not looked at, and said so: the row keeps the worker-process accounting.
            assert facts == {
                "observed_at": facts["observed_at"],
                "descendants": [],
                "enumerated": False,
                "reason": "unsupported_platform",
                "method": "none",
            }
            return
        assert facts["enumerated"] is True
        # The census replaced the walk here: a Job Object's
        # membership survives an intermediate's exit, which is the whole
        # reason the walk was not enough. The walk remains the fallback and
        # is exercised by its own test below.
        assert facts["method"] == "job_object"
        assert facts["complete"] is True
        listed = {item["pid"]: item for item in facts["descendants"]}
        assert child in listed, facts
        assert listed[child]["image"].casefold() == Path(sys.executable).name.casefold()
        assert started - 5 <= listed[child]["created"] <= time.time() + 1
        # Nothing was stopped: the child is still there for the caller to read.
        assert procutil.pid_state(child) == "alive"
    finally:
        _release(child)


def _release(pid: int) -> None:
    import os
    import signal

    if os.name == "nt":
        procutil.terminate_pid(pid)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_a_worker_that_leaves_nothing_reports_an_empty_enumeration_not_unknown():
    import os

    if os.name != "nt":
        pytest.skip("enumeration is Windows-only; POSIX reports unsupported_platform")
    completed = run_with_tree_kill([sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=60)

    facts = completed.worker_descendants
    assert facts["enumerated"] is True
    assert facts["descendants"] == []


def test_the_workers_own_console_host_is_never_a_left_behind_process():
    """Every worker runs CREATE_NO_WINDOW, which still allocates a hidden console
    whose conhost.exe Windows owns and which can outlive the client by an
    instant -- exactly when the walk runs. It is not something the worker
    launched and cannot write a workspace, so it is never listed."""

    import os

    if os.name != "nt":
        pytest.skip("Windows console hosts only")
    for _ in range(5):
        completed = run_with_tree_kill([sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=60)
        assert completed.worker_descendants["enumerated"] is True
        assert [item["image"] for item in completed.worker_descendants["descendants"]] == []


def test_descendant_facts_are_absent_for_a_runner_that_did_not_look():
    from modelquad.execution.base import descendant_facts

    plain = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    assert descendant_facts(plain) == {}
    looked = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    looked.worker_descendants = {"enumerated": True, "descendants": []}
    facts = descendant_facts(looked)["worker_descendants"]
    assert (facts["enumerated"], facts["descendants"], facts["truncated"]) == (True, [], False)


def test_list_descendants_is_unknown_for_a_pid_that_cannot_be_asked_about():
    assert procutil.list_descendants(0) is None
    assert procutil.list_descendants(-1) is None
    assert procutil.list_descendants("x") is None


def test_list_descendants_on_posix_needs_a_process_group():
    """Without a group of its own, a reparented grandchild is invisible: say so."""

    assert procutil.list_descendants(4242, process_group=False, run=RecordingRun(), os_name="posix") is None


def test_list_descendants_on_posix_reads_the_process_group(monkeypatch):
    from modelquad.executable_resolution import Resolution

    resolved = "/usr/bin/ps"
    monkeypatch.setattr(procutil, "resolve_executable", lambda name: Resolution(resolved, "path", "binary", None))
    # A grandchild inherits the session's group; an unrelated shell does not.
    table = "  4242  4242 python3\n  4300  4242 /usr/bin/node\n  4301  4242 node\n  9999  9999 bash\n"

    class Runner:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            if argv[:2] == [resolved, "-e"]:
                return _completed(table)
            return _completed("00:05\n")  # etime for creation

    runner = Runner()
    listed = procutil.list_descendants(4242, process_group=True, run=runner, os_name="posix")

    assert [item["pid"] for item in listed] == [4300, 4301]
    assert [item["image"] for item in listed] == ["node", "node"]
    assert all(isinstance(item["created"], float) for item in listed)
    assert runner.calls[0] == [resolved, "-e", "-o", "pid=,pgid=,comm="]


def test_a_console_host_is_excluded_only_when_it_is_the_system_directorys_own(monkeypatch):
    """A name is not ownership: conhost.exe under the system directory is
    Windows's; the same name anywhere else is listed like anything else, and a
    LIVE process whose path cannot be read is listed too."""

    monkeypatch.setattr(procutil, "_windows_system_directory", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "alive")
    monkeypatch.setattr(procutil, "windows_process_image_path", lambda pid: r"C:\Windows\System32\conhost.exe")
    assert procutil._is_console_host_not_left_behind(1, "conhost.exe") is True
    assert procutil._is_console_host_not_left_behind(1, "CONHOST.EXE") is True
    monkeypatch.setattr(procutil, "windows_process_image_path", lambda pid: r"C:\Users\x\build\conhost.exe")
    assert procutil._is_console_host_not_left_behind(1, "conhost.exe") is False
    monkeypatch.setattr(procutil, "windows_process_image_path", lambda pid: None)
    assert procutil._is_console_host_not_left_behind(1, "conhost.exe") is False
    assert procutil._is_console_host_not_left_behind(1, "node.exe") is False


def test_a_console_host_that_exited_before_its_path_was_read_is_not_left_behind(monkeypatch):
    """The race this exists for: the pid comes from a snapshot, and
    the host can exit before the image-path query runs. OpenProcess then fails
    -- and so does QueryFullProcessImageNameW on a handle whose process is
    merely signalled -- so the path arrives as None with the process already
    gone. A process that has exited is not left behind by anyone."""

    monkeypatch.setattr(procutil, "_windows_system_directory", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(procutil, "windows_process_image_path", lambda pid: None)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "dead")
    assert procutil._is_console_host_not_left_behind(4242, "conhost.exe") is True
    assert procutil._is_console_host_not_left_behind(4242, "CONHOST.EXE") is True
    # The name gate still comes first: a dead descendant that is not a console
    # host at all is nothing this exclusion has an opinion about.
    assert procutil._is_console_host_not_left_behind(4242, "node.exe") is False


def test_an_unreadable_path_on_a_console_host_still_alive_is_listed(monkeypatch):
    """Fail-closed is preserved where it was earned. Only a positive "dead"
    reading excludes: a live process whose ownership cannot be established is
    listed, and so is one whose liveness could not be observed at all --
    a failure to observe is not an observation (pid_state)."""

    monkeypatch.setattr(procutil, "_windows_system_directory", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(procutil, "windows_process_image_path", lambda pid: None)
    for state in ("alive", "unknown"):
        monkeypatch.setattr(procutil, "pid_state", lambda pid, state=state: state)
        assert procutil._is_console_host_not_left_behind(4242, "conhost.exe") is False


def test_liveness_is_asked_only_when_the_console_hosts_path_is_unreadable(monkeypatch):
    """A readable path settles ownership on its own -- both ways -- and the
    walk pays for no extra process probe on the ordinary path."""

    asked = []
    monkeypatch.setattr(procutil, "_windows_system_directory", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(procutil, "pid_state", lambda pid: asked.append(pid) or "dead")
    monkeypatch.setattr(procutil, "windows_process_image_path", lambda pid: r"C:\Windows\System32\conhost.exe")
    assert procutil._is_console_host_not_left_behind(4242, "conhost.exe") is True
    monkeypatch.setattr(procutil, "windows_process_image_path", lambda pid: r"C:\Users\x\build\conhost.exe")
    assert procutil._is_console_host_not_left_behind(4242, "conhost.exe") is False
    assert asked == []


def test_windows_process_image_path_reads_this_process():
    import os

    if os.name != "nt":
        assert procutil.windows_process_image_path(os.getpid()) is None
        return
    path = procutil.windows_process_image_path(os.getpid())
    assert path is not None
    assert Path(path).name.casefold() == Path(sys.executable).name.casefold()
    assert procutil.windows_process_image_path(0) is None


def test_run_with_tree_kill_does_not_change_how_a_worker_is_spawned(monkeypatch):
    """No new session, no new group: what Ctrl+C reaches is unchanged."""

    seen = {}

    class FakeProc:
        pid = 4242
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return ("", "")

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(procutil.subprocess, "Popen", fake_popen)
    # A fake pid must never reach the Job Object calls: 4242 may be a real
    # process on this machine.
    monkeypatch.setattr(procutil, "_contain_suspended_worker", lambda proc: None)
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: None)
    monkeypatch.setattr(procutil, "_descendants_at_exit", lambda *a, **k: {"enumerated": False, "descendants": []})
    run_with_tree_kill(["x"], capture_output=True)
    assert "start_new_session" not in seen
    if os.name == "nt":
        # Created suspended, so it is inside its job before it runs;
        # still without a console window.
        assert seen["creationflags"] & procutil._CREATE_SUSPENDED
        assert seen["creationflags"] & 0x08000000


# --------------------------------------------------------------------------
# pid_state and ACCESS_DENIED: a zombie is dead, not alive
#
# OpenProcess returns ERROR_ACCESS_DENIED for two processes that could not be
# more different -- one that is running under another token, and one that
# exited long ago and is only still a pid because some other process leaked a
# handle to it. Reading both as "alive" kept a settled workspace unsettled
# permanently. The corroborating enumeration is what separates them.
# --------------------------------------------------------------------------


def _deny_openprocess(monkeypatch) -> None:
    """OpenProcess refuses: the one reading that settles nothing by itself."""

    monkeypatch.setattr(procutil.os, "name", "nt")
    monkeypatch.setattr(procutil, "_windows_openprocess_state", lambda pid: procutil._ACCESS_DENIED)


def test_access_denied_on_an_enumerable_pid_is_still_alive(monkeypatch):
    """The earlier pid_state behaviour is preserved exactly where it was right.

    Another user's process, or the kernel's, is refused by OpenProcess and IS
    listed by an enumeration that needs no access to it. That is a real
    process, and closing its attempt row would be the destructive error
    pid_state exists to prevent.
    """

    _deny_openprocess(monkeypatch)
    monkeypatch.setattr(procutil, "_windows_pid_in_snapshot", lambda pid: True)

    assert procutil.pid_state(4321) == "alive"


def test_access_denied_on_a_pid_no_enumerator_lists_is_dead(monkeypatch):
    """The zombie. Refused by OpenProcess, absent from the process table.

    Its token is gone, which is why the access check fails; it has exited,
    which is why nothing enumerates it. It cannot be writing a workspace, so
    it must not hold a delegation row open forever.
    """

    _deny_openprocess(monkeypatch)
    monkeypatch.setattr(procutil, "_windows_pid_in_snapshot", lambda pid: False)

    assert procutil.pid_state(4321) == "dead"


def test_access_denied_with_no_usable_snapshot_stays_unknown(monkeypatch):
    """Two probes that cannot answer are still not an observation.

    "unknown" is the fail-safe of the three-valued contract and a
    snapshot that could not be taken -- or that stopped before the end of the
    table -- may not be downgraded into "dead".
    """

    _deny_openprocess(monkeypatch)
    monkeypatch.setattr(procutil, "_windows_pid_in_snapshot", lambda pid: None)

    assert procutil.pid_state(4321) == "unknown"


@pytest.mark.parametrize("settled", ["alive", "dead", "unknown"])
def test_an_openprocess_answer_is_never_second_guessed_by_the_snapshot(monkeypatch, settled):
    """Corroboration is scoped to the denial and nothing else.

    A handle plus GetExitCodeProcess, ERROR_INVALID_PARAMETER, and an
    unrecognised error each already settle the question. Consulting the
    enumeration there would let a pid that is merely missing from a snapshot
    override a direct observation.
    """

    consulted = []
    monkeypatch.setattr(procutil.os, "name", "nt")
    monkeypatch.setattr(procutil, "_windows_openprocess_state", lambda pid: settled)
    monkeypatch.setattr(procutil, "_windows_pid_in_snapshot", lambda pid: consulted.append(pid) or False)

    assert procutil.pid_state(4321) == settled
    assert consulted == [], "a settled reading must not be re-opened"


def test_pid_state_short_circuits_before_any_windows_probe(monkeypatch):
    """A non-pid is answered without a probe, so nothing was prevented."""

    probed = []
    monkeypatch.setattr(procutil.os, "name", "nt")
    monkeypatch.setattr(procutil, "_windows_openprocess_state", lambda pid: probed.append(pid) or "alive")

    assert procutil.pid_state(0) == "dead"
    assert procutil.pid_state(-5) == "dead"
    assert procutil.pid_state("x") == "unknown"
    assert procutil.pid_state(None) == "unknown"
    assert probed == []


def test_the_posix_branch_is_untouched_by_the_corroboration(monkeypatch):
    """Non-goal, asserted: the corroboration changes Windows only.

    The POSIX reading of PermissionError stays "alive" and never reaches the
    Toolhelp enumeration, which does not exist there.
    """

    consulted = []
    monkeypatch.setattr(procutil.os, "name", "posix")
    monkeypatch.setattr(procutil, "_windows_pid_in_snapshot", lambda pid: consulted.append(pid) or False)

    def fake_kill(pid, sig):
        raise PermissionError()

    monkeypatch.setattr(procutil.os, "kill", fake_kill)
    assert procutil.pid_state(4321) == "alive"
    assert consulted == []


def test_pid_in_snapshot_reports_absence_only_from_a_complete_snapshot(monkeypatch):
    """None in, None out: an unfinished scan is not evidence of absence."""

    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: None)
    assert procutil._windows_pid_in_snapshot(4321) is None

    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: [(4, 0, "System"), (4321, 4, "x.exe")])
    assert procutil._windows_pid_in_snapshot(4321) is True
    assert procutil._windows_pid_in_snapshot(9999) is False

    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: [])
    assert procutil._windows_pid_in_snapshot(4321) is False


def test_the_descendant_walk_reads_the_same_snapshot_and_its_failure(monkeypatch):
    """One enumerator, two callers.

    The walk must keep returning None -- "could not enumerate", never "found
    nothing" -- when the shared snapshot could not be read completely.
    """

    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: None)
    assert procutil._list_descendants_toolhelp(4321, created_after=None) is None

    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: [(4321, 1, "parent.exe"), (77, 4321, "kid.exe")])
    monkeypatch.setattr(procutil, "_is_console_host_not_left_behind", lambda pid, image: False)
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: 100.0)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "alive")
    found = procutil._list_descendants_toolhelp(4321, created_after=None)
    assert [item["pid"] for item in found] == [77]
    assert found[0]["image"] == "kid.exe"


def _snapshot_of_one_child(parent: int, child: int, image: str):
    return lambda: [(parent, 1, "parent.exe"), (child, parent, image)]


def test_a_descendant_that_has_exited_is_not_listed_even_with_a_readable_creation_time(monkeypatch):
    """``GetProcessTimes`` still answers for a process that exited
    while another process holds a handle to it -- measured on
    Windows 11 -- so a real creation-time number is not evidence of liveness.
    A positive "dead" reading excludes on its own, for every image, not only
    for the ``conhost.exe`` that an earlier name gate covered."""

    monkeypatch.setattr(procutil, "_windows_process_snapshot", _snapshot_of_one_child(4321, 77, "node.exe"))
    monkeypatch.setattr(procutil, "_is_console_host_not_left_behind", lambda pid, image: False)
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: 1789400213.32)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "dead")
    assert procutil._list_descendants_toolhelp(4321, created_after=None) == []
    # An unreadable creation time on a dead pid was already excluded, and is.
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: None)
    assert procutil._list_descendants_toolhelp(4321, created_after=None) == []


def test_a_descendant_whose_liveness_cannot_be_read_is_still_listed(monkeypatch):
    """Fail closed where nothing was observed: "unknown" is a probe that could
    not answer, not a dead process (:func:`pid_state`). An entry the
    snapshot listed with a readable creation time stays listed on it, exactly as
    an "alive" one does."""

    monkeypatch.setattr(procutil, "_windows_process_snapshot", _snapshot_of_one_child(4321, 77, "node.exe"))
    monkeypatch.setattr(procutil, "_is_console_host_not_left_behind", lambda pid, image: False)
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: 1789400213.32)
    for state in ("alive", "unknown"):
        monkeypatch.setattr(procutil, "pid_state", lambda pid, state=state: state)
        found = procutil._list_descendants_toolhelp(4321, created_after=None)
        assert [item["pid"] for item in found] == [77], state
        assert found[0]["created"] == 1789400213.32

    # Neither probe could read anything at all -- no creation time and no
    # liveness. That entry was excluded before the liveness reading and
    # still is: the reading narrowed nothing, it only stopped requiring the unreadable time.
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: None)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "unknown")
    assert procutil._list_descendants_toolhelp(4321, created_after=None) == []


def test_a_dead_descendants_own_children_are_still_walked(monkeypatch):
    """An exclusion never hides a subtree: a worker whose intermediate shim has
    exited still gets the grandchild that shim left running."""

    monkeypatch.setattr(
        procutil,
        "_windows_process_snapshot",
        lambda: [(4321, 1, "parent.exe"), (77, 4321, "shim.cmd"), (78, 77, "node.exe")],
    )
    monkeypatch.setattr(procutil, "_is_console_host_not_left_behind", lambda pid, image: False)
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: 1789400213.32)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "dead" if pid == 77 else "alive")
    found = procutil._list_descendants_toolhelp(4321, created_after=None)
    assert [item["pid"] for item in found] == [78]


def test_the_recycled_pid_filter_is_unchanged_by_the_liveness_reading(monkeypatch):
    """A live descendant older than the worker is still a recycled-pid parent,
    not something the worker spawned. The liveness reading added a reason to
    skip; it removed none."""

    monkeypatch.setattr(procutil, "_windows_process_snapshot", _snapshot_of_one_child(4321, 77, "node.exe"))
    monkeypatch.setattr(procutil, "_is_console_host_not_left_behind", lambda pid, image: False)
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: 100.0)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "alive")
    assert procutil._list_descendants_toolhelp(4321, created_after=200.0) == []
    assert [item["pid"] for item in procutil._list_descendants_toolhelp(4321, created_after=50.0)] == [77]


def test_a_real_denied_but_enumerable_process_is_alive():
    """Live, no stubs: Windows's own System process (pid 4).

    It cannot be opened even from an elevated session and it is always in the
    process table. It is the real shape of the case that must keep answering
    "alive".
    """

    import os

    if os.name != "nt":
        pytest.skip("the ACCESS_DENIED corroboration is Windows-only")
    if procutil._windows_openprocess_state(4) != procutil._ACCESS_DENIED:
        pytest.skip("pid 4 is openable here; this machine cannot show the denied case")
    assert procutil._windows_pid_in_snapshot(4) is True
    assert procutil.pid_state(4) == "alive"


def test_a_real_exited_child_whose_handle_is_still_held_is_dead(monkeypatch):
    """Live, and the zombie case on demand: an exited process kept by a handle.

    A child is spawned, a handle to it is opened and deliberately kept open,
    and the child exits. The process object cannot be freed while that handle
    lives, so the pid is still a pid -- but no enumerator lists it, which is
    exactly the fact the ACCESS_DENIED branch relies on.

    The access check itself cannot be made to fail on demand from the process
    holding the handle (we have every right to open it), so the denial is the
    one thing simulated here: everything the corroboration reads is real.
    """

    import ctypes
    import os

    if os.name != "nt":
        pytest.skip("held-handle zombies are a Windows process-object behaviour")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # The canonical helper's stdin opt-out: this spawn names its own, and
        # `no_window_kwargs()` would hand `Popen` a second one.
        **procutil.no_window_kwargs(detach_stdin=False),
    )
    pid = proc.pid
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    assert handle, f"could not hold a handle to the child: {ctypes.get_last_error()}"
    try:
        assert proc.wait(timeout=60) == 0
        # The pid is not reused while the handle is held, so this is stable.
        assert procutil._windows_pid_in_snapshot(pid) is False, "an exited process is not enumerated"
        # Held handle, exited process: the direct reading still works.
        assert procutil.pid_state(pid) == "dead"

        # And with the access check denied -- the zombie whose token is gone
        # -- the same real pid must still read dead rather than alive.
        monkeypatch.setattr(procutil, "_windows_openprocess_state", lambda p: procutil._ACCESS_DENIED)
        assert procutil.pid_state(pid) == "dead"
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_this_running_process_is_alive_through_the_real_probes():
    """The floor: the whole Windows path, unfaked, on a pid known to be live."""

    import os

    assert procutil.pid_state(os.getpid()) == "alive"
    if os.name == "nt":
        assert procutil._windows_pid_in_snapshot(os.getpid()) is True
        assert procutil._windows_openprocess_state(os.getpid()) == "alive"


def _worker_whose_own_parent_exits(seconds: int = 60) -> str:
    """A worker that spawns a child THROUGH an intermediate that then exits.

    The broken-ancestry shape, and the shape production has: a `.cmd` shim
    starts the real worker and exits; a venv's `Scripts\\python.exe` starts the
    base interpreter and exits. The surviving process's recorded parent is the
    intermediate, and the intermediate is gone.
    """

    import os

    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    inner = (
        "import subprocess, sys; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({seconds})'], "
        f"creationflags={flags}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, close_fds=True); print(p.pid)"
    )
    # The worker runs the intermediate, waits for it to exit, and reports the
    # grandchild's pid. By the time the worker itself exits, the only link
    # between it and the surviving process is already broken.
    return (
        "import subprocess, sys; "
        f"out = subprocess.run([sys.executable, '-c', {inner!r}], capture_output=True, text=True); "
        "sys.stdout.write(out.stdout)"
    )


@pytest.mark.skipif(os.name != "nt", reason="the broken-ancestry shape is a Windows process-table property")
def test_a_descendant_survives_the_exit_of_the_process_that_spawned_it():
    """The walk followed links; an exit destroys links.

    `list_descendants` reaches a child of a dead worker because a child keeps
    its parent's pid. That is true one level deep and the walk assumed it
    transitively: a GRANDchild whose intermediate parent has exited has no
    surviving link back, so the walk returned an empty list -- which reads as
    "the worker left nothing running" for the exact shape every provider CLI
    has on Windows (`cmd.exe` -> `node.exe`, shim exits).

    Found on the self-hosted CI lane, where the interpreter is a virtual
    environment and `Scripts\\python.exe` adds precisely that intermediate.
    """

    import time

    started = time.time()
    completed = run_with_tree_kill(
        [sys.executable, "-c", _worker_whose_own_parent_exits()],
        capture_output=True, text=True, timeout=90,
    )
    survivor = int(completed.stdout.strip())
    try:
        facts = completed.worker_descendants
        assert completed.returncode == 0
        assert procutil.pid_state(survivor) == "alive", "the fixture must leave something running"

        listed = {item["pid"]: item for item in facts["descendants"]}
        assert survivor in listed, facts
        assert listed[survivor]["image"].casefold() == Path(sys.executable).name.casefold()
        assert started - 5 <= listed[survivor]["created"] <= time.time() + 1
        # Counting must never become killing.
        assert procutil.pid_state(survivor) == "alive"
    finally:
        _release(survivor)


@pytest.mark.skipif(os.name != "nt", reason="the census is a Windows Job Object")
def test_the_census_reports_whether_its_count_was_provable():
    """`complete` is the answer to "was that everything?", and it is never a guess.

    A Job Object census can say yes: membership is not a link, so no exit can
    hide a member. The snapshot walk usually cannot, and saying so is the
    whole point -- an unprovable count that rendered like a proven one is what
    the broken-ancestry defect was.
    """

    completed = run_with_tree_kill(
        [sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=60
    )
    facts = completed.worker_descendants
    assert facts["enumerated"] is True
    assert facts["method"] == "job_object"
    assert facts["complete"] is True
    assert facts["descendants"] == []


@pytest.mark.skipif(os.name != "nt", reason="the census is a Windows Job Object")
def test_a_snapshot_walk_that_cannot_prove_completeness_says_so(monkeypatch):
    """With no census to count with, the walk's answer carries its own limit."""

    monkeypatch.setattr(procutil, "_census_job_pids", lambda handle: None)
    monkeypatch.setattr(procutil, "_toolhelp_walk_is_provable", lambda created_after: False)

    completed = run_with_tree_kill(
        [sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=60
    )
    facts = completed.worker_descendants
    assert facts["method"] == "toolhelp32"
    assert facts["enumerated"] is True
    assert facts["complete"] is False
    assert facts["incomplete_reason"] == "ancestry_broken"


@pytest.mark.skipif(os.name != "nt", reason="the census is a Windows Job Object")
def test_a_census_that_cannot_be_read_falls_back_rather_than_claiming_a_clean_count(monkeypatch):
    """No census is not "nothing was left running".

    The job itself always exists here -- a worker whose job cannot be made is
    never started -- so the fallback is for a census that cannot
    be read when the worker exits.

    What this pins is the fallback's honesty, and deliberately not what the
    walk finds. An earlier version of this test asserted the direct child was
    reachable "because one level is the case the walk always handled" -- and
    it failed on the self-hosted lane, where `sys.executable` is a virtual
    environment launcher, so what looks like a direct child is a grandchild
    with an exited intermediate. The test had made the exact assumption
    the broken-ancestry defect is about. Whether the walk reaches anything depends on the
    interpreter it was run with; whether it lies about having counted does
    not.
    """

    monkeypatch.setattr(procutil, "_census_job_pids", lambda handle: None)

    completed = run_with_tree_kill(
        [sys.executable, "-c", _worker_that_leaves_a_child()],
        capture_output=True, text=True, timeout=60,
    )
    child = int(completed.stdout.strip())
    try:
        facts = completed.worker_descendants
        assert facts["method"] == "toolhelp32", "it must not silently report a census it did not take"
        assert facts["enumerated"] is True
        found = {item["pid"] for item in facts["descendants"]}
        if child not in found:
            # The walk could not reach it. That is allowed -- and saying so is
            # the whole of the second level.
            assert facts["complete"] is False, facts
            assert facts["incomplete_reason"] == "ancestry_broken", facts
    finally:
        _release(child)


@pytest.mark.skipif(os.name != "nt", reason="the census is a Windows Job Object")
def test_a_census_member_whose_image_cannot_be_read_and_has_exited_is_not_left_behind(monkeypatch):
    """A failed read is not the fact "the worker left something running".

    The census asks `_is_console_host_not_left_behind` by NAME, and the name
    comes from a second probe that can fail. When it did, the census passed
    `""` to a gate that compares against the literal `"conhost.exe"`, matched
    nothing, and listed the member with `image: None` -- reporting a read it
    could not make as a positive finding. The same family as an earlier defect, in
    the other direction: there an unreadable list became "nothing is open",
    here an unreadable name becomes "something is running".

    This is what made
    `test_the_workers_own_console_host_is_never_a_left_behind_process`
    intermittent: the console host exits between the census and the name
    probe, which is precisely the instant its exclusion was written for.
    """

    member = 4242
    monkeypatch.setattr(procutil, "_census_job_pids", lambda handle: [member])
    monkeypatch.setattr(procutil, "_image_name_for_pid", lambda pid: None)
    # Alive to the census's own first guard, gone by the time the name is
    # asked for. Anything else does not reproduce the race.
    states = iter(["alive", "dead"])
    monkeypatch.setattr(procutil, "pid_state", lambda pid: next(states, "dead"))

    facts = procutil._descendants_at_exit(
        1, created_after=None, process_group=False, census_job=1234
    )

    assert facts["method"] == "job_object"
    assert facts["enumerated"] is True
    assert facts["descendants"] == [], facts


@pytest.mark.skipif(os.name != "nt", reason="the census is a Windows Job Object")
def test_a_live_census_member_whose_image_cannot_be_read_is_still_listed(monkeypatch):
    """Unproven is listed, never excluded.

    The guard above must close the exit race and nothing wider. A member that
    is still running when asked twice has not exited, so its unreadable name
    means ownership could not be established -- and unproven ownership is
    listed, the same fail-closed rule `_is_console_host_not_left_behind`
    already states for a missing image path.
    """

    member = 4242
    monkeypatch.setattr(procutil, "_census_job_pids", lambda handle: [member])
    monkeypatch.setattr(procutil, "_image_name_for_pid", lambda pid: None)
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "alive")
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid: 1.0)

    facts = procutil._descendants_at_exit(
        1, created_after=None, process_group=False, census_job=1234
    )

    assert [item["pid"] for item in facts["descendants"]] == [member], facts
    assert facts["descendants"][0]["image"] is None
