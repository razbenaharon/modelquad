# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""No harness spawn opens a console window on Windows.

The product already carries this invariant (``tests/test_procutil.py`` scans
``src/``); the ``scripts/`` files that spawn were the remaining sources of the
windows seen flashing during a shift. Each carries an identical inline
``no_window_kwargs`` -- the harness may not import the product -- so the first
test here is that the copies have not drifted apart, and the rest prove the
property the Issue asks for: hidden on ``nt``, untouched on POSIX, and
stdout/stderr capture, exit codes, timeouts and UTF-8 all preserved under the
flag. The uncaptured spawn (``check.py``'s step runner, which streams to the
developer's terminal) is the measured exception: a ``CREATE_NO_WINDOW`` child
of a console parent loses its output entirely, so that site attaches to an
existing console instead -- and that opens no window.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

#: A file is identified by its repository-relative path rather than its bare
#: name.
CARRIER_ROOTS = (SCRIPTS,)


def _spawn_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
        ):
            yield node

CREATE_NO_WINDOW = 0x08000000


def _defines_helper(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(isinstance(n, ast.FunctionDef) and n.name == "no_window_kwargs" for n in tree.body)


def _spawns(path: Path) -> bool:
    return any(True for _ in _spawn_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))


def _candidates() -> list[Path]:
    return sorted(path for root in CARRIER_ROOTS for path in root.glob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


#: Discovered, not listed: the next file in either tree that starts spawning is
#: held to the same tests the moment it does, with no list to forget to update.
SPAWNING_FILES = tuple(_rel(path) for path in _candidates() if _spawns(path))


def _load(name: str):
    path = REPO_ROOT / name
    spec = importlib.util.spec_from_file_location("_no_window_" + path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a module that declares dataclasses needs it,
    # because `dataclasses` resolves the defining module through `sys.modules`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _helper_source(path: Path) -> str:
    """The text of the three helper definitions, as the file carries them."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"_CREATE_NO_WINDOW", "_stdout_is_console", "no_window_kwargs"}
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            pieces.append(ast.get_source_segment(path.read_text(encoding="utf-8"), node))
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted for t in node.targets
        ):
            pieces.append(ast.get_source_segment(path.read_text(encoding="utf-8"), node))
    assert len(pieces) == 3, f"{path.name} carries {len(pieces)} of the 3 helper definitions"
    return "\n".join(pieces)


# --- 1. the copies agree ------------------------------------------------------

def test_every_spawning_file_carries_the_same_helper():
    assert set(SPAWNING_FILES) >= {"scripts/check.py"}
    carriers = [_rel(path) for path in _candidates() if _defines_helper(path)]
    assert carriers == list(SPAWNING_FILES), "exactly the files that spawn carry the helper"
    reference = _helper_source(SCRIPTS / "check.py")
    drifted = [name for name in SPAWNING_FILES if _helper_source(REPO_ROOT / name) != reference]
    assert not drifted, f"no_window_kwargs differs from check.py's copy in: {drifted}"


# --- 2. the helper's contract --------------------------------------------------

@pytest.mark.parametrize("script", SPAWNING_FILES)
def test_helper_sets_the_flag_on_nt_and_nothing_on_posix(script):
    module = _load(script)
    assert module.no_window_kwargs(os_name="nt") == {"creationflags": CREATE_NO_WINDOW}
    assert module.no_window_kwargs(os_name="posix") == {}
    assert module.no_window_kwargs(os_name="posix", streams_to_console=True) == {}


def test_streaming_child_attaches_to_a_live_console_and_hides_otherwise(monkeypatch):
    module = _load("scripts/check.py")
    monkeypatch.setattr(module, "_stdout_is_console", lambda: True)
    assert module.no_window_kwargs(os_name="nt", streams_to_console=True) == {}
    # A captured child never needs the console, even when one is there.
    assert module.no_window_kwargs(os_name="nt") == {"creationflags": CREATE_NO_WINDOW}
    monkeypatch.setattr(module, "_stdout_is_console", lambda: False)
    # Hidden and streaming: stdin is detached so the spawn names a standard
    # handle and Windows passes the parent's stdout/stderr to the child.
    assert module.no_window_kwargs(os_name="nt", streams_to_console=True) == {
        "creationflags": CREATE_NO_WINDOW, "stdin": subprocess.DEVNULL,
    }
    # A captured spawn still gets only the flag: a caller that passes `input=`
    # or its own `stdin=` must not receive a second one.
    assert "stdin" not in module.no_window_kwargs(os_name="nt")


@pytest.mark.skipif(os.name != "nt", reason="handle inheritance under CREATE_NO_WINDOW is a Windows fact")
def test_a_hidden_streaming_child_of_a_console_less_parent_still_reaches_the_parents_output(tmp_path):
    """The live failure: check.py under a runner
    service, stdout a pipe, spawned pytest hidden -- and every line of pytest
    was lost. Reproduced with a console-less parent (itself hidden) that runs
    the step runner with its stdout redirected to a file: the grandchild's
    lines must land in that file."""
    out = tmp_path / "parent.out"
    parent = (
        "import importlib.util, sys, os; from pathlib import Path; "
        "spec = importlib.util.spec_from_file_location('c', sys.argv[1]); m = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(m); "
        "rc = m._run([sys.executable, '-c', \"print('GRANDCHILD_STDOUT'); import sys; sys.stderr.write('GRANDCHILD_STDERR' + chr(10)); sys.exit(4)\"], "
        "cwd=Path(sys.argv[2]), env=dict(os.environ)); print('parent saw rc', rc)"
    )
    with out.open("w", encoding="utf-8") as sink:
        proc = subprocess.run(
            [sys.executable, "-c", parent, str(SCRIPTS / "check.py"), str(REPO_ROOT)],
            stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=120,
        )
    text = out.read_text(encoding="utf-8", errors="replace")
    assert proc.returncode == 0, text
    assert "parent saw rc 4" in text
    assert "GRANDCHILD_STDOUT" in text and "GRANDCHILD_STDERR" in text, text


def test_console_probe_reads_the_win32_handles_not_sys_stdout():
    """The probe answers about the process's real standard handles, which a
    test runner's capture does not touch -- so it is not asserted in-process,
    where the answer depends on how pytest was launched. It is asserted in a
    child whose stdout and stderr are both pipes (False, on every platform),
    and, on Windows, in a child that is handed this process's own handles,
    where it must agree with a direct GetConsoleMode on them."""
    module = _load("scripts/check.py")
    assert isinstance(module._stdout_is_console(), bool)
    probe = (
        "import importlib.util, sys; "
        "spec = importlib.util.spec_from_file_location('c', sys.argv[1]); m = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(m); print(m._stdout_is_console())"
    )
    piped = subprocess.run(
        [sys.executable, "-c", probe, str(SCRIPTS / "check.py")], capture_output=True, text=True,
        timeout=60, **module.no_window_kwargs()
    )
    assert piped.returncode == 0, piped.stderr
    assert piped.stdout.strip() == "False"
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        expected = any(
            k32.GetStdHandle(h) and k32.GetConsoleMode(k32.GetStdHandle(h), ctypes.byref(ctypes.c_ulong()))
            for h in (-11, -12)
        )
        assert module._stdout_is_console() is bool(expected)


# --- 3. every call site uses it ------------------------------------------------

def test_every_harness_spawn_passes_the_helper():
    missing = []
    for path in _candidates():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _spawn_calls(tree):
            splats = [
                keyword
                for keyword in call.keywords
                if keyword.arg is None
                and any(isinstance(e, ast.Name) and e.id == "no_window_kwargs" for e in ast.walk(keyword.value))
            ]
            if not splats:
                missing.append(f"{_rel(path)}:{call.lineno}")
    assert not missing, f"spawns without no_window_kwargs: {missing}"


#: The only spawns allowed to hand a child this process's own stdout/stderr,
#: and therefore the only ones that may ask for `streams_to_console`. Each is
#: a long build step whose output the operator watches live, and for each one a
#: `CREATE_NO_WINDOW` child of a console parent would lose that output outright
#: (measured). Everything else in both trees captures, so a new
#: uncaptured spawn is a decision that has to be made here, on purpose.
STREAMING_SITES = ("scripts/check.py",)


def test_only_the_named_build_steps_stream_to_the_console():
    """The uncaptured sites are exactly the ones that say they are."""
    streaming = []
    for path in _candidates():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _spawn_calls(tree):
            for keyword in call.keywords:
                if keyword.arg is None and isinstance(keyword.value, ast.Call):
                    if any(k.arg == "streams_to_console" for k in keyword.value.keywords):
                        streaming.append(_rel(path))
            captures = any(k.arg in {"capture_output", "stdout"} for k in call.keywords)
            if not captures and _rel(path) not in STREAMING_SITES:
                pytest.fail(f"{_rel(path)}:{call.lineno} is uncaptured; only {STREAMING_SITES} may be")
    # check.py's step runner streams once.
    assert sorted(set(streaming)) == sorted(STREAMING_SITES)


def test_call_sites_forward_the_flag_on_nt(monkeypatch):
    module = _load("scripts/check.py")
    recorded: list[dict] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        recorded.append(kwargs)
        return _Done()

    real = module.no_window_kwargs
    monkeypatch.setattr(module, "no_window_kwargs", lambda **kw: real(os_name="nt", **kw))
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module._git_lines("--version")
    assert recorded and recorded[0].get("creationflags") == CREATE_NO_WINDOW
    assert recorded[0].get("capture_output") is True


# --- 4. the property holds live ---------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is a Windows creation flag")
def test_hidden_spawn_still_captures_output_exit_code_and_utf8():
    module = _load("scripts/check.py")
    child = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('caf\\u00e9 \\u05e9\\u05dc\\u05d5\\u05dd'); sys.stderr.write('err'); sys.exit(7)",
    ]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        child, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60, **module.no_window_kwargs()
    )
    assert proc.returncode == 7
    assert proc.stdout == "café שלום"
    assert proc.stderr == "err"


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is a Windows creation flag")
def test_hidden_spawn_still_honours_the_timeout():
    module = _load("scripts/check.py")
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            capture_output=True,
            timeout=1,
            **module.no_window_kwargs(),
        )


@pytest.mark.skipif(os.name != "nt", reason="a console window is a Windows fact")
def test_hidden_spawn_has_no_console_window():
    """The child reports its own console window handle: under the flag it has
    none. Measured as the child sees it rather than by counting desktop windows
    from outside, where a console window belongs to ``conhost`` and not to the
    child's pid. The unflagged control is asserted only when this process has
    a console of its own for the child to attach to; a parent that already has
    no console window (an agent harness, a hidden launcher) can hand the
    control nothing observable, and that says nothing about the flag."""
    import ctypes

    module = _load("scripts/check.py")
    report = [sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow() or 0)"]
    hidden = subprocess.run(report, capture_output=True, text=True, timeout=60, **module.no_window_kwargs())
    assert hidden.returncode == 0 and hidden.stdout.strip() == "0"
    if ctypes.windll.kernel32.GetConsoleWindow():
        # console-window-exempt: the unflagged control, and the only reason the
        # line above means anything. Hiding it would delete the comparison. It
        # allocates nothing extra: it runs only when this process already owns a
        # console for the child to attach to, and attaching opens no new window
        # -- the genuinely console-less case is the opt-in rig further down.
        control = subprocess.run(report, capture_output=True, text=True, timeout=60)
        assert control.returncode == 0 and control.stdout.strip() != "0"


# --- 4. the rig that proves the negative, and its cost ------------------------
#: The positive control below is the one test in this suite that *deliberately*
#: puts a console window on the developer's desktop, so it is opt-in.
#:
#: Not fastidiousness. The acceptance criterion is that a normal workflow
#: produces no ModelQuad-owned console window, and a re-observation found that
#: after every real defect was fixed, the only two windows left in a flagged
#: 240 s pytest run were this rig's -- the regression suite for "no console
#: windows" had become the last remaining source of console windows, and it
#: failed the criterion on the criterion's own terms.
#:
#: The invariant it proves is real and stays runnable as an opt-in:
#:
#:     MODELQUAD_CONSOLE_ALLOCATION_CONTROL=1 python -m pytest \
#:         tests/test_scripts_no_window.py -k console_less_parent
#:
#: An isolated window station was considered and rejected. ``CreateWindowStation``
#: plus ``CreateDesktop`` would prove allocation without touching the visible
#: desktop, but a child started on a non-interactive station is a different
#: process-creation path from the one under test, the rig would need a token
#: dance to be reliable, and a rig that is subtly not-the-thing is worse
#: evidence than a clean skip that says how to run the real thing.
CONSOLE_CONTROL_ENV = "MODELQUAD_CONSOLE_ALLOCATION_CONTROL"
CONSOLE_CONTROL_REASON = (
    f"Allocates a real console window on the desktop; opt-in (set {CONSOLE_CONTROL_ENV}=1). "
    f"See the note above the test: a default run of this suite must open no console window."
)


@pytest.mark.skipif(os.name != "nt", reason="console allocation is a Windows fact")
@pytest.mark.skipif(os.environ.get(CONSOLE_CONTROL_ENV) != "1", reason=CONSOLE_CONTROL_REASON)
def test_a_console_less_parent_gets_a_window_without_the_flag_and_none_with_it(tmp_path):
    """The condition the whole invariant exists for, reproduced deliberately.

    ``test_hidden_spawn_has_no_console_window`` can only assert its *control*
    when the test process happens to own a console, so under an agent harness
    -- which is where this repository is actually developed -- the control
    silently does nothing and the test proves only half of what it reads as
    proving. Here the parent is launched ``DETACHED_PROCESS`` and therefore has
    no console at all, which is what an MCP host really
    is. Under that parent the unflagged child MUST report a
    console window and the flagged one MUST report none, so both halves are
    load-bearing on every machine.

    Measured rather than asserted from the desktop: a console window belongs to
    ``conhost``/Windows Terminal, not to the child's pid, so the child reading
    its own ``GetConsoleWindow`` is the attributable reading. That reading was
    cross-checked against a desktop-window observer while the popups were being
    investigated -- the desktop saw exactly the windows the children reported,
    and no others. That observer, ``scripts/windowwatch.py``, is part of this
    candidate again, but this test still needs nothing from it: the assertions
    below are the child's own report, and they run on a machine that has never
    seen the instrument.

    **Opt-in**, for the reason in the note above this test: proving that an
    unflagged child allocates a console means allocating one, and this rig was
    measured to be the only remaining source of visible console windows in a
    normal flagged run. Set ``MODELQUAD_CONSOLE_ALLOCATION_CONTROL=1`` to
    run it. Nothing else in this file needs the opt-in -- every other test
    either spawns hidden or asserts on source.
    """

    result = tmp_path / "result.json"
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            (
                "import ctypes, json, subprocess, sys",
                "from pathlib import Path",
                "report = [sys.executable, '-c', 'import ctypes;print(ctypes.windll.kernel32.GetConsoleWindow() or 0)']",
                "plain = subprocess.run(report, capture_output=True, text=True, timeout=60)",
                "hidden = subprocess.run(report, capture_output=True, text=True, timeout=60,",
                "                        creationflags=0x08000000)",
                "Path(sys.argv[1]).write_text(json.dumps({",
                "    'parent': ctypes.windll.kernel32.GetConsoleWindow() or 0,",
                "    'plain': plain.stdout.strip(), 'plain_rc': plain.returncode,",
                "    'hidden': hidden.stdout.strip(), 'hidden_rc': hidden.returncode,",
                "}), encoding='utf-8')",
            )
        ),
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(child), str(result)],
        creationflags=DETACHED_PROCESS,  # no console, and none inherited
        close_fds=True,
    )
    assert parent.wait(timeout=180) == 0
    facts = json.loads(result.read_text(encoding="utf-8"))

    assert facts["parent"] == 0, f"the rig itself owns a console, so it proves nothing: {facts}"
    assert facts["plain_rc"] == 0 and facts["hidden_rc"] == 0, facts
    assert facts["plain"] != "0", f"an unflagged child of a console-less parent must allocate a console: {facts}"
    assert facts["hidden"] == "0", f"CREATE_NO_WINDOW must leave the child with no console: {facts}"


#: ``DETACHED_PROCESS``. The literal is written once, here, so the scan below
#: has a name to look for rather than a magic number to re-spell.
DETACHED_PROCESS = 0x00000008


def test_every_console_allocating_rig_in_this_file_is_opt_in():
    """The gate above is a property of the file, not of one test.

    A ``DETACHED_PROCESS`` parent is how this suite proves that an *unflagged*
    child allocates a console -- which means the rig allocates one, on the
    developer's desktop, during a run whose whole subject is that nothing
    should. One such rig, explicitly opted into, is a reasonable trade. A
    second one added later without the gate would quietly restore exactly the
    condition the re-observation had to record as a finding.

    So the rule is scanned rather than remembered: any test function in this
    file that *spawns* with ``DETACHED_PROCESS`` must carry the opt-in
    ``skipif``. Matched on the spawn rather than on the word, so a docstring
    that discusses the rig -- this one does -- is not mistaken for one.
    Reads this file's own source, imports nothing, and runs everywhere.
    """

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=__file__)

    def detaches(function: ast.FunctionDef) -> bool:
        for call in _spawn_calls(function):
            for keyword in call.keywords:
                if keyword.arg != "creationflags":
                    continue
                flag = keyword.value
                if isinstance(flag, ast.Name) and flag.id == "DETACHED_PROCESS":
                    return True
                if isinstance(flag, ast.Constant) and flag.value == DETACHED_PROCESS:
                    return True
        return False

    gated, offenders = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        if not detaches(node):
            continue
        decorators = "\n".join(ast.get_source_segment(source, d) or "" for d in node.decorator_list)
        (gated if "CONSOLE_CONTROL_ENV" in decorators else offenders).append(node.name)

    assert not offenders, (
        f"these allocate a console window on every default run; gate them on {CONSOLE_CONTROL_ENV}: {offenders}"
    )
    # And the scan is not vacuously empty: the one rig it exists to govern.
    assert gated == ["test_a_console_less_parent_gets_a_window_without_the_flag_and_none_with_it"], gated
