#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""ModelQuad pre-flight validation -- the checks a change must pass locally.

Stdlib-only, no dependencies, and identical on Windows PowerShell and POSIX
shells: every coding agent and every
maintainer must be able to drive the same behaviour by typing the same thing.

    python scripts/check.py --list
    python scripts/check.py python
    python scripts/check.py docs secrets
    python scripts/check.py all

This script is the fast subset a developer runs before the hosted CI run,
plus the two repository-hygiene checks that had no home.

Two traps it exists to remove, both evidenced rather than theoretical:

1. **The documented command could not be typed on the target platform.**
   Six places in this repository documented `PYTHONPATH=src python -m pytest -q`.
   That is POSIX shell syntax; in PowerShell it fails with "The term
   'PYTHONPATH=src' is not recognized". ModelQuad is Windows-first
   (`AGENTS.md`, "Current state"), and its CI runs `shell: pwsh`.
2. **A bare `pytest` in a worktree tests the wrong code.** The system
   interpreter carries an editable install pointing at the *primary* checkout,
   so a worktree's suite can import that checkout's source and pass green while
   the branch under test is broken.

This script sets the import root itself, prints the resolved
`modelquad.__file__` before running anything, and echoes every command it
runs. It is a shortcut, not a black box: if you would rather type the commands
yourself, `--list` prints them.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------- no console window (nt)

#: Windows: a console child of a console-less parent allocates its own console
#: window, and the desktop shows it for the child's lifetime. The
#: product suppresses that at every spawn through `modelquad.procutil`;
#: this file may not import the product (the harness never depends on what it
#: repairs), so it carries the same six lines itself. The copy is
#: identical in every `scripts/` file that spawns, and
#: `tests/test_scripts_no_window.py` asserts the copies still agree.
_CREATE_NO_WINDOW = 0x08000000


def _stdout_is_console() -> bool:
    """Whether this process's own stdout *or stderr* is a live console (Windows only).

    Both, because a streaming child inherits both: `check.py python > out.txt`
    still shows pytest's stderr on the terminal, and a child hidden from that
    console would lose it. Read through the Win32 handles, not `sys.stdout`,
    which a test runner or a wrapper can replace without touching them.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        for std_handle in (-11, -12):
            handle = kernel32.GetStdHandle(std_handle)
            if handle and kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_ulong())):
                return True
        return False
    except (AttributeError, OSError, ValueError):
        return False


def no_window_kwargs(*, streams_to_console: bool = False, os_name: str | None = None) -> dict:
    """`creationflags` that keep a non-interactive child off the desktop.

    Empty off Windows, so POSIX behaviour is untouched. `os_name` exists so a
    test can exercise both branches without patching `os.name`.

    `streams_to_console` is for the one kind of spawn that hands the child this
    process's own stdout/stderr instead of pipes. When either of those *is* a
    console the child has to attach to it: CREATE_NO_WINDOW would give it no
    console at all and its output would simply vanish (measured on Windows 11:
    exit 1, nothing displayed), and attaching to an existing console opens no
    new window anyway. When both are pipes or files -- a shift, a check run by
    another check -- the child is hidden and must *inherit* those handles; on
    Windows that happens only when the spawn names at least one standard
    handle, so stdin is detached to /dev/null for exactly that reason (the
    product's helper does the same). Measured:
    without it the hidden child got a fresh hidden console and every line of
    pytest's output was lost; with it the inherited file or pipe received them.
    A captured child never needs a console.
    """
    if (os.name if os_name is None else os_name) != "nt":
        return {}
    if streams_to_console and _stdout_is_console():
        return {}
    hidden: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)}
    if streams_to_console:
        hidden["stdin"] = subprocess.DEVNULL
    return hidden


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

#: This file's own directory, fixed at import time and deliberately NOT rebound
#: by `--repo`. `_bind_repo` moves the tree under *test*; the harness's own
#: policy modules come from the trusted copy that is doing the judging.
SCRIPTS_DIR = Path(__file__).resolve().parent


def _bind_repo(path: Path) -> Path:
    """Point every check at `path` instead of the tree this file lives in.

    By default the tree under test is the one this script is part of, which is
    right for a developer and for the shift's ordinary candidates. It is wrong
    for exactly one case: a candidate that changes this script. Judging that
    candidate with its own copy lets it change its judge, so the shift runs
    the *trusted* copy -- this file as it is on `origin/main` -- against the
    candidate's tree, and this is how it says which tree.
    """
    global REPO_ROOT, SRC
    REPO_ROOT = Path(path).resolve()
    SRC = REPO_ROOT / "src"
    return REPO_ROOT


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNAVAILABLE = 2

# Directories that are build output or third-party code. Nothing under these is
# authored here, so nothing under these is checked.
EXCLUDED_DIRS = frozenset(
    {".git", "node_modules", "dist", "build", "__pycache__", ".pytest_cache", ".venv", "private"}
)


# --------------------------------------------------------------- utilities


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _authored_markdown() -> list[Path]:
    """Every Markdown file this repository authors.

    `dist/` copies of the tester guide are packaged output; they contain
    deliberately flattened links that do not resolve in the source tree, and
    checking them would report a defect that is not one.
    """
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.md"):
        if EXCLUDED_DIRS.intersection(path.parts):
            continue
        out.append(path)
    return sorted(out)


def _load_devenv():
    """Import `scripts/devenv.py`, the development child-environment policy.

    Loaded from `SCRIPTS_DIR` rather than `REPO_ROOT`. Where a child process
    writes its temporary files is a property
    of *this* run's harness, so a `--repo` candidate does not get to redefine
    it any more than it gets to redefine the checks themselves.

    Returns `None` when the module is not beside this file, for example when
    `check.py` is copied out on its own. The redirection is an optimisation and
    the gate's verdict must not depend on it, so its absence degrades to the
    ambient temporary directory rather than failing the run.
    """
    path = SCRIPTS_DIR / "devenv.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_modelquad_devenv", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _child_env() -> tuple[dict[str, str], str]:
    """The environment a check hands a subprocess: `src/` on `PYTHONPATH`, and
    the development temp root applied when the machine asked for one.

    A copy of `os.environ`, never `os.environ` itself. The behaviour flags stay
    exactly as the developer's shell set them -- see `check_python` for why
    that is the point rather than an omission.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    devenv = _load_devenv()
    return env, (devenv.apply_temp_root(env) if devenv is not None else "")


def _echo(command: list[str], *, cwd: Path) -> None:
    print(f"\n  $ {' '.join(command)}    (in {_rel(cwd)})", flush=True)


#: How hard a check may load the machine: "normal" (default) uses every core
#: at below-normal priority, so it runs at full speed on an idle machine and
#: yields to whatever you are doing; "gentle" also halves the test workers;
#: "full" keeps normal priority. Set with --pace or MODELQUAD_TEST_PACE.
PACES = ("gentle", "normal", "full")
PACE = os.environ.get("MODELQUAD_TEST_PACE", "normal") if os.environ.get("MODELQUAD_TEST_PACE") in PACES else "normal"
#: Windows BELOW_NORMAL_PRIORITY_CLASS. Set on this process, so every process
#: it starts (pytest, its workers, and what the tests start) inherits it.
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def _apply_pace_priority() -> bool:
    """Lower this process to below-normal priority unless the pace is "full"."""
    if os.name != "nt" or PACE == "full":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), _BELOW_NORMAL_PRIORITY_CLASS))


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> int:
    _echo(command, cwd=cwd)
    return subprocess.run(command, cwd=str(cwd), env=env, **no_window_kwargs(streams_to_console=True)).returncode



# ------------------------------------------------------------ check: python


#: Environment flags that change ModelQuad's behaviour, reported before the
#: suite runs. Deliberately reported and NOT cleared -- see `check_python`.
BEHAVIOUR_FLAGS = ("MODELQUAD_WORKER", "MODELQUAD_DEV_MODE", "MODELQUAD_DEV")


def _pytest_parallel_args() -> list[str]:
    """`-n auto` when pytest-xdist is importable here, else nothing.

    Without the plugin the
    suite runs serially and proves the same thing more slowly; the answer is
    printed so a green run says how it was produced.
    """
    if importlib.util.find_spec("xdist") is None:
        return []
    if PACE == "gentle":
        return ["-n", str(max(2, (os.cpu_count() or 4) // 2))]
    return ["-n", "auto"]


def _pytest_phases() -> list[tuple[str, list[str]]]:
    """Disjoint selections; timing tests only start after the parallel pass.

    Kept here so a standalone trusted copy needs no new helper dependency.
    Without xdist one serial invocation covers the entire suite.
    """
    parallel = _pytest_parallel_args()
    if not parallel:
        return [("serial-all (pytest-xdist not installed)", [])]
    return [
        ("parallel", [*parallel, "-m", "not serial_timing"]),
        ("serial-timing", ["-n", "0", "-m", "serial_timing"]),
    ]


def check_python() -> int:
    """The Python suite, with the import root pinned to this worktree's src/.

    Runs in the default (source-tree) import mode, which is what
    `windows-ci.yml` inverts by testing the installed, non-editable package.
    Both directions are deliberate.

    The behaviour flags are **reported, not cleared**, and the difference is
    the whole point. This is a pre-flight subset whose job is to agree
    with a hand-typed `python -m pytest`; clearing them here would make it
    disagree, and would hide exactly the defect that produced this line --
    `MODELQUAD_WORKER=1`, inherited from an agent session's environment,
    failed 77 tests because the suite read it from the ambient
    environment. `tests/conftest.py` now states that variable for itself, so
    the suite is hermetic wherever it runs rather than only under this script.
    Printing the flags means a green run still says which environment produced
    it, for the same reason the import root is printed below -- and, when the
    machine set `MODELQUAD_TEMP_ROOT`, the temp root the suite was given.
    """
    env, temp_root = _child_env()

    observed = {name: os.environ[name] for name in BEHAVIOUR_FLAGS if name in os.environ}
    print(
        "  environment: "
        + (
            ", ".join(f"{name}={value}" for name, value in observed.items())
            if observed
            else "no ModelQuad behaviour flags set"
        )
    )
    if temp_root:
        print(f"  temp root: {temp_root}  (TEMP/TMP for the suite below)")

    print("  workers: " + ("pytest-xdist, -n auto" if _pytest_parallel_args() else "none (pytest-xdist not installed; serial)"))
    probe = subprocess.run(
        [sys.executable, "-c", "import modelquad, sys; sys.stdout.write(modelquad.__file__)"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        **no_window_kwargs(),
    )
    resolved = probe.stdout.strip()
    if probe.returncode != 0 or not resolved:
        print("FAIL  python: `import modelquad` failed even with PYTHONPATH set.")
        print(probe.stderr.strip())
        return EXIT_FAILED

    print(f"  import root: {resolved}")
    print(f"  pace: {PACE} ({'normal priority' if PACE == 'full' or os.name != 'nt' else 'below-normal priority'}"
          f"{', half the workers' if PACE == 'gentle' else ''})")
    try:
        Path(resolved).resolve().relative_to(SRC.resolve())
    except ValueError:
        # Not fatal by itself -- an installed-mode run is a legitimate way to
        # test -- but it is never what someone validating a worktree wants, and
        # it is the exact failure that made a green suite meaningless before.
        print(
            "WARN  python: modelquad resolved OUTSIDE this worktree's src/.\n"
            "      The suite below is testing that installation, not this branch."
        )

    for phase, arguments in _pytest_phases():
        print(f"  Python phase: {phase}", flush=True)
        code = _run([sys.executable, "-m", "pytest", "-q", *arguments], cwd=REPO_ROOT, env=env)
        if code != 0:
            print(f"FAIL  python ({phase}: pytest exited {code}; no retry)")
            return EXIT_FAILED
    print("PASS  python (all phases)")
    return EXIT_OK


# ----------------------------------------------------------- check: focused


def _git_lines(*args: str) -> tuple[list[str], str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout or "git exited " + str(proc.returncode)).strip()
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()], ""


def changed_paths() -> tuple[list[str], str]:
    """What this branch changes against the integration target, or why that is unknown.

    Committed, staged, unstaged and untracked alike: the review gate is run on a
    tree that may still be mid-edit, and a test file that is not yet `git add`ed
    is exactly the one a reviewer is about to be shown.
    """
    base = ""
    for target in ("origin/main", "main"):
        lines, _ = _git_lines("merge-base", target, "HEAD")
        if lines:
            base = lines[0]
            break
    if not base:
        return [], "no integration target to diff against (neither origin/main nor main resolves)"
    changed, error = _git_lines("diff", "--name-only", base)
    if error:
        return [], error
    untracked, error = _git_lines("ls-files", "--others", "--exclude-standard")
    if error:
        return [], error
    return sorted(set(changed) | set(untracked)), ""


#: Whole-repository invariants that live in test files no change is named after.
#: Selecting by stem alone never picked them: one change added an unflagged
#: spawn in a test file, the review gate passed, and only `check.py all` and a
#: paid hosted run found it through `test_procutil.py`'s scan.
#: Each entry reads the source tree and starts no process, so the review gate runs
#: all of them on every change -- the two tests that keep this list complete
#: included, so a new scan left off it fails the review gate as well.
#: `tests/test_devenv.py` holds this list to every
#: test in `tests/` that walks the repository, directly or through a module-level
#: helper that takes no arguments (one that does walks what it is handed).
REPO_WIDE_SCANS = (
    "tests/test_adapter_template_packaging.py::test_packaged_adapter_templates_exist_without_project_duplicates",
    "tests/test_delegation_workspace_liveness.py::test_every_path_that_opens_an_attempt_records_its_workspace",
    "tests/test_delegation_workspace_liveness.py::test_every_result_boundary_that_closes_an_attempt_carries_the_note",
    "tests/test_devenv.py::test_every_repo_wide_scan_names_a_test_that_exists",
    "tests/test_devenv.py::test_every_test_that_walks_the_repository_is_a_repo_wide_scan",
    "tests/test_devenv.py::test_the_root_conftest_cannot_reach_the_product_runtime",
    "tests/test_docs_policy.py::test_no_document_carries_a_control_byte",
    "tests/test_executable_resolution_boundary.py::test_src_launches_use_central_resolution",
    "tests/test_procutil.py::test_no_subprocess_spawn_in_src_omits_window_suppression",
    "tests/test_procutil.py::test_no_subprocess_spawn_in_tests_omits_window_suppression",
    "tests/test_scripts_no_window.py::test_every_spawning_file_carries_the_same_helper",
    "tests/test_service_entrypoints.py::test_every_cli_service_call_site_uses_managed_lifecycle",
    "tests/test_service_entrypoints.py::test_installed_command_entry_has_no_service_call_sites",
    "tests/test_startup_imports.py::test_runner_launch_sources_do_not_import_mcp",
    "tests/test_third_party_notices.py::test_every_python_file_carries_the_spdx_identifier",
)


#: Report broad selections without truncating the coverage they require.
FOCUSED_TEST_FILE_LIMIT = 40


def _import_nodes(path: Path) -> list[ast.Import | ast.ImportFrom] | None:
    """Read imports at every scope without executing source; None is unreadable."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (SyntaxError, UnicodeError, OSError):
        return None
    return [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]


def _import_from_module(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    try:
        return importlib.util.resolve_name("." * node.level + (node.module or ""), package)
    except (ImportError, ValueError):
        return ""


def _importing_tests(changed: list[str], candidates: list[Path]) -> set[str]:
    modules = set()
    packages = set()
    for path in changed:
        if not path.startswith("src/modelquad/") or not path.endswith(".py"):
            continue
        parts = list(Path(path).with_suffix("").parts[1:])
        if parts[-1] == "__init__":
            parts.pop()
            packages.add(".".join(parts))
        modules.add(".".join(parts))

    def affected(module: str) -> bool:
        return module in modules or any(module.startswith(pkg + ".") for pkg in packages)

    # Read each changed module's immediate package for re-exports. Resolve
    # relative imports statically, retaining aliases as the package's names.
    exports: dict[str, set[str]] = {}
    for package in sorted({module.rpartition(".")[0] for module in modules} - {""}):
        init = REPO_ROOT / "src" / Path(*package.split(".")) / "__init__.py"
        if not init.is_file():
            continue
        nodes = _import_nodes(init)
        names = exports.setdefault(package, set())
        if nodes is None:
            names.add("*")  # Unknown exports must not hide an importer.
            continue
        for node in nodes:
            if isinstance(node, ast.ImportFrom):
                source = _import_from_module(node, package)
                for alias in node.names:
                    if affected(source) or affected(source + "." + alias.name):
                        names.add(alias.asname or alias.name)
            else:
                for alias in node.names:
                    if affected(alias.name):
                        names.add(alias.asname or alias.name.split(".")[0])

    selected = set()
    for candidate in candidates:
        nodes = _import_nodes(candidate)
        if nodes is None:
            # Let pytest report broken syntax, even if its imports are unknown.
            selected.add(_rel(candidate))
            continue
        for node in nodes:
            if isinstance(node, ast.Import):
                matches = any(affected(alias.name) or exports.get(alias.name) for alias in node.names)
            else:
                source = _import_from_module(node, "")
                names = exports.get(source, set())
                matches = affected(source) or any(
                    affected(source + "." + alias.name)
                    or exports.get(source + "." + alias.name)
                    or alias.name in names
                    or (bool(names) and (alias.name == "*" or "*" in names))
                    for alias in node.names
                )
            if matches:
                selected.add(_rel(candidate))
                break
    return selected


def _path_guard_tests(changed: list[str], candidates: list[Path]) -> tuple[set[str], set[str]]:
    """Select guards naming non-package files or their parent by constant paths."""
    paths = {path for path in changed if not path.startswith("src/")}
    if not paths:
        return set(), set()
    selected = set()
    directory_guards = set()
    parents = {Path(path).parent.as_posix() for path in paths} - {"."}
    for candidate in candidates:
        try:
            tree = ast.parse(candidate.read_text(encoding="utf-8-sig"), filename=str(candidate))
        except (SyntaxError, UnicodeError, OSError):
            selected.add(_rel(candidate))  # Let pytest report unreadable guards.
            continue
        literals = set()
        directories = set()
        constant_bases = {
            id(node.value) for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        }
        constants = {}
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = statement.value

        def constant_parts(node: ast.AST, seen: frozenset[str] = frozenset()) -> list[str]:
            # Only strings, / chains and module-level names; never evaluate a
            # root expression. Cycles and chains over 32 names stop here.
            parts = []
            while (
                isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
            ):
                parts.append(node.right.value)
                node = node.left
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                parts.append(node.value)
            elif isinstance(node, ast.Name) and node.id in constants:
                if node.id not in seen and len(seen) < 32:
                    return constant_parts(constants[node.id], seen | {node.id}) + list(reversed(parts))
            return list(reversed(parts))

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                is_constant_base = id(node) in constant_bases
                resolved = "/".join(constant_parts(node))
                if resolved:
                    literals.add(resolved)
                    if is_constant_base:
                        directories.add(resolved)
                # REPO_ROOT / "scripts" / "check.py": collect the constant
                # suffix, without evaluating or guessing the root expression.
                parts = []
                while (
                    isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
                ):
                    parts.append(node.right.value)
                    node = node.left
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    parts.append(node.value)
                if parts:
                    suffix = "/".join(reversed(parts))
                    literals.add(suffix)
                    if is_constant_base:
                        directories.add(suffix)
        # Directory guards may join variable filenames to a constant base
        # (STATUS = REPO / "docs" / "status"). Select the whole guard for
        # changes directly in that directory without executing its source.
        if paths & literals:
            selected.add(_rel(candidate))
        if parents & directories:
            directory_guards.add(_rel(candidate))
    return selected, directory_guards


def focused_tests(changed: list[str]) -> list[str]:
    """Stem matches, AST importers (including package re-exports), path literals, workflow
    guards and repository scans. Selection is deterministic from the diff and
    source alone; broad selections are reported, never trimmed."""
    if not changed:
        return []
    selected: set[str] = set()
    workflow: set[str] = set()
    tests_dir = REPO_ROOT / "tests"
    for path in changed:
        if path.startswith(".github/workflows/"):
            # A workflow file has no stem to match; its guards live in one place.
            guard = tests_dir / "test_ci_workflow.py"
            if guard.is_file():
                workflow.add(_rel(guard))
            continue
        if not path.endswith(".py"):
            continue
        name = Path(path).name
        if path.startswith("tests/") and name.startswith("test_"):
            if (REPO_ROOT / path).is_file():
                selected.add(path)
            continue
        stem = Path(path).stem
        for candidate in tests_dir.glob("test_" + stem + "*.py"):
            selected.add(_rel(candidate))
    candidates = sorted(tests_dir.glob("test_*.py"))
    importers = _importing_tests(changed, candidates)
    path_guards, directory_guards = _path_guard_tests(changed, candidates)
    whole = selected | workflow | importers | path_guards | directory_guards
    scans = {scan for scan in REPO_WIDE_SCANS if scan.split("::", 1)[0] not in whole}
    rules = {"stem": selected, "importer": importers, "path": path_guards, "dir": directory_guards,
             "workflow": workflow, "scan": scans}
    broad = len(whole) > FOCUSED_TEST_FILE_LIMIT or any(
        path in {"src/modelquad/__init__.py", "src/modelquad/config.py", "src/modelquad/models.py"}
        for path in changed
    )
    print(
        f"  focused selection: {len(whole)} whole test file(s); "
        + "; ".join(f"{rule}={len(entries)} [{', '.join(sorted(entries))}]" for rule, entries in rules.items())
        + (f"; broad selection (limit {FOCUSED_TEST_FILE_LIMIT}), all files retained" if broad else "")
    )
    selected = whole | scans
    return sorted(selected)


def check_focused() -> int:
    """The review-readiness half of the review gate: cheap, deterministic, scoped to the diff.

    Seconds to a few minutes, never the release stack: every changed Python file
    must compile, the package must import from this worktree, and the tests a
    change names must pass. It is the proof that a head is *worth reviewing*,
    not the proof that it may merge -- `check.py all` on the exact approved
    candidate remains that.
    """
    changed, error = changed_paths()
    if error:
        print("FAIL  focused: cannot establish what changed (" + error + ")")
        return EXIT_FAILED
    python_files = [p for p in changed if p.endswith(".py") and (REPO_ROOT / p).is_file()]
    print(f"  changed: {len(changed)} path(s), {len(python_files)} Python file(s)")
    if python_files:
        code = _run([sys.executable, "-m", "py_compile", *python_files], cwd=REPO_ROOT)
        if code != 0:
            print("FAIL  focused: a changed Python file does not compile")
            return EXIT_FAILED
    env, temp_root = _child_env()
    if temp_root:
        print(f"  temp root: {temp_root}")
    probe = subprocess.run(
        [sys.executable, "-c", "import modelquad"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        **no_window_kwargs(),
    )
    if probe.returncode != 0:
        print("FAIL  focused: `import modelquad` failed with PYTHONPATH set.")
        print(probe.stderr.strip())
        return EXIT_FAILED
    tests = focused_tests(changed)
    if not tests:
        print("  focused tests: none named by this change")
        print("PASS  focused (compile + import only)")
        return EXIT_OK
    print("  focused tests: " + ", ".join(tests))
    code = _run([sys.executable, "-m", "pytest", "-q", *tests], cwd=REPO_ROOT, env=env)
    print("PASS  focused" if code == 0 else f"FAIL  focused (pytest exited {code})")
    return EXIT_OK if code == 0 else EXIT_FAILED


# -------------------------------------------------------------- check: docs

_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")

#: `AGENTS.md` "Current state" states four numbers that live in code. A wrong
#: number in the orientation document is worse than an absent one, because it
#: is the file an arriving agent trusts first. Each entry is
#: (label, module-relative path, constant name).
CURRENT_STATE_FACTS = (
    ("Version", "__init__.py", "__version__"),
    ("DB schema", "service/db.py", "SCHEMA_VERSION"),
    ("router engine", "router.py", "ROUTER_ENGINE_VERSION"),
    ("outcome assessor", "outcome.py", "ASSESSOR_VERSION"),
)

#: `VAR=value command` -- POSIX-only. PowerShell rejects it outright, and this
#: repository is Windows-first with a `shell: pwsh` CI.
_POSIX_ENV_PREFIX = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=(\S+)\s+(python|pytest|npm|node)\b")

#: Only documents an agent is told to *follow* are checked for portability.
#:
#: A line beginning `$ ` is a transcript of what was typed, in the shell it was
#: typed in, wherever it appears; rewriting it to look portable would falsify
#: the record, so it is exempt.
INSTRUCTIONAL_DOCS = (
    "AGENTS.md",
    "README.md",
    "scripts/README.md",
    "adapters/",
)

#: A line carrying this marker is a deliberate, reviewed exception. Used where a
#: credential *shape* is the subject rather than a credential -- the scanner's
#: own fixtures. Per line, never per file, so a real secret elsewhere in an
#: exempted file still fails.
SECRET_ALLOW_MARKER = "secret-scan: allow"


def _read_constant(module_relpath: str, name: str) -> str | None:
    path = SRC / "modelquad" / module_relpath
    if not path.is_file():
        return None
    match = re.search(rf"^{re.escape(name)}\s*=\s*[\"']?([\w.]+)[\"']?", path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def _check_links() -> list[str]:
    problems: list[str] = []
    for doc in _authored_markdown():
        for target in _LINK.findall(doc.read_text(encoding="utf-8")):
            target = target.strip()
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            resolved = doc.parent / target.split("#", 1)[0]
            if not resolved.exists():
                problems.append(f"{_rel(doc)} -> {target}")
    return problems


def _check_current_state() -> list[str]:
    agents = REPO_ROOT / "AGENTS.md"
    if not agents.is_file():
        return ["AGENTS.md is missing"]
    line = next(
        (l for l in agents.read_text(encoding="utf-8").splitlines() if "DB schema" in l),
        None,
    )
    if line is None:
        return ["AGENTS.md states no 'DB schema' line in Current state"]

    problems: list[str] = []
    for label, module_relpath, constant in CURRENT_STATE_FACTS:
        actual = _read_constant(module_relpath, constant)
        if actual is None:
            problems.append(f"cannot read {constant} from src/modelquad/{module_relpath}")
            continue
        stated = re.search(rf"{re.escape(label)}\s*`([^`]+)`", line)
        if stated is None:
            problems.append(f"AGENTS.md Current state does not state {label}")
        elif stated.group(1) != actual:
            problems.append(
                f"AGENTS.md says {label} `{stated.group(1)}`, "
                f"{module_relpath} says {actual}"
            )
    return problems


def _is_instructional(doc: Path) -> bool:
    relative = _rel(doc)
    return any(
        relative == prefix or relative.startswith(prefix) for prefix in INSTRUCTIONAL_DOCS
    )


#: A comment naming the shell, on or just above the command. The defect is a
#: POSIX-*only* instruction, not the existence of a POSIX form: showing both
#: shells side by side is the fix, so a labelled alternative must not be
#: flagged as the thing it fixes.
_SHELL_LABEL = re.compile(r"#\s*(bash|zsh|sh|posix)\b", re.IGNORECASE)


def _check_posix_only_commands() -> list[str]:
    problems: list[str] = []
    for doc in _authored_markdown():
        if not _is_instructional(doc):
            continue
        lines = doc.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.lstrip().startswith("$ "):
                continue  # a transcript of what was typed, not an instruction
            match = _POSIX_ENV_PREFIX.match(line)
            if not match:
                continue
            # The label may sit on the line itself or immediately above it,
            # allowing for one blank line inside an indented block.
            window = lines[max(0, index - 3) : index + 1]
            if any(_SHELL_LABEL.search(candidate) for candidate in window):
                continue
            problems.append(
                f"{_rel(doc)}:{index + 1}: `{match.group(1)}={match.group(2)} ...` "
                f"is POSIX-only and fails in PowerShell, with no PowerShell form given"
            )
    return problems


# ------------------------------------------------- wording: banned phrases

#: The public text a reader takes as a description of the product: the
#: top-level documents, `docs/`, the skills (the plugin's copies and the
#: package templates they are synced from), and the MCP server's
#: instructions. Paths ending in `/` are folders, scanned recursively.
WORDING_SOURCES = (
    "README.md",
    "PRIVACY.md",
    "NETWORK.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "docs/",
    "plugin/skills/",
    "src/modelquad/adapters/templates/claude/skills/",
)
WORDING_TEXT_SUFFIXES = frozenset({".md", ".txt"})
#: Its text is the string constants assigned at module level, read with `ast`
#: so this script never imports the product.
WORDING_SERVER_INSTRUCTIONS = "src/modelquad/adapters/server_instructions.py"
#: Code whose strings people or Claude read: tool titles and descriptions,
#: replies, notices, refusals, CLI help and output. Read with `ast`, never
#: imported. Scanned: every string literal (f-string parts included) that
#: holds a space, so identifiers, keys and codes ("routing_mode", "safe_id")
#: never match. Skipped: docstrings, which are for developers, except the
#: docstring of a `modelquad_*` function, which is an MCP tool's description.
WORDING_CODE_SOURCES = (
    "src/modelquad/mcp/server.py",
    "src/modelquad/mcp/jobs_tools.py",
    "src/modelquad/mcp/presentation.py",
    "src/modelquad/split.py",
    "src/modelquad/split_summary.py",
    "src/modelquad/disclosure.py",
    "src/modelquad/onboarding.py",
    "src/modelquad/doctor.py",
    "src/modelquad/cli.py",
    "src/modelquad/repo_policy.py",
    "src/modelquad/repo_settings.py",
    "src/modelquad/plans.py",
    "src/modelquad/assistance.py",
    "src/modelquad/adapters/pending_reminder.py",
    "src/modelquad/adapters/file_security.py",
    "src/modelquad/execution/noshell.py",
    "src/modelquad/execution/base.py",
    "src/modelquad/usage/claude.py",
)
#: Scanned as plain text: the desktop band's source.
WORDING_PLAIN_CODE_SOURCES = ("plugin/hooks/",)
WORDING_PLAIN_CODE_SUFFIXES = frozenset({".tsx"})
#: A stored value a scanned string must be able to name, quoted as code
#: (`routing`, 'routing:7', "routing"). Only these exact quoted tokens are
#: skipped; the bare word still fails. Keep this list tiny, and say why.
BANNED_PHRASE_ALLOWED_IDENTIFIERS = frozenset({
    # The history keeps two record kinds, "routing" and "execution"
    # (service/db.py); `modelquad_activity_detail` and `modelquad history
    # --detail` take the kind as typed. Renaming it would break stored data.
    "routing",
})

#: Between two words of a phrase: up to three other words, across line breaks.
_GAP = r"(?:\W+\w+){0,3}?\W+"
#: Hebrew has no `\b` for `re`: a Hebrew word starts after a non-Hebrew
#: character, optionally with one or two prefix letters (ו ה ב ל מ ש כ).
_HE_START = r"(?<![֐-׿])[והבלמשכ]{0,2}"
_HE_END = r"(?![֐-׿])"


def _he(stem: str, suffixes: str = "") -> str:
    tail = f"(?:{suffixes})?" if suffixes else ""
    return _HE_START + stem + tail + _HE_END


#: Phrases the public text may not use, in any language and also not in the
#: negative ("not a router" still says router). The owner's wording rules for
#: public text, kept in sync by hand. Each entry is (what it bans, pattern);
#: matching is case-insensitive. Whole words throughout, so "pro" inside
#: "approval" or "route" inside "routine" never match; verbs that are ordinary
#: on their own ("save", "skip", "combine") are banned only next to a word
#: about usage, limits or plans.
BANNED_PHRASES: tuple[tuple[str, str], ...] = (
    # Usage as something to save or squeeze.
    ("save quota or usage", rf"\bsav(?:e|es|ed|ing)\b{_GAP}(?:quota|usage)s?\b"),
    ("quota or usage saved", r"\b(?:quota|usage)\s+sav(?:ed|ings?)\b"),
    ("waste your quota", rf"\bwast(?:e|es|ed|ing)\b{_GAP}(?:quota|usage)\b"),
    ("every last percent", r"\bevery\s+last\s+percent"),
    ("stretch or extend a plan",
     rf"\b(?:stretch|extend|maximi[sz]e)\w*{_GAP}(?:plan|subscription|quota|usage|limit)s?\b"),
    ("get more out of a plan", rf"\bget\s+more\s+out\s+of\b"),
    # Limits as an obstacle.
    ("beat or bypass a limit",
     rf"\b(?:beat|bypass|get\s+around|escape|skip|circumvent|never\s+hit)\w*{_GAP}limits?\b"),
    ("keep coding when Claude runs out",
     r"\b(?:keep|continue)\s+(?:coding|working|going)\s+(?:when|after)\b"),
    ("runs out", r"\bwhen\s+(?:claude|codex|it|your\s+\w+)\s+runs?\s+out\b"),
    ("continue after the limit", rf"\bafter\s+(?:the|a|your)\s+limit\b"),
    ("failover", r"\bfail[\s-]?over\b"),
    # More usage, or plans put together.
    ("more usage", r"\bmore\s+usage\b"),
    ("double your usage", r"\bdouble\s+your\b"),
    ("unlimited", r"\bunlimited\b"),
    ("combine or pool subscriptions",
     rf"\b(?:combin|pool|merg|stack)\w*{_GAP}(?:subscription|plan|account|quota)s?\b"),
    # Routing.
    ("route, router, routing", r"\brout(?:e|es|ed|er|ers|ing)\b"),
    ("load balancing", r"\bload[\s-]?balanc\w*"),
    ("auto-switch", r"\bauto[\s-]?switch\w*"),
    ("automatic switching", r"\bautomatic(?:ally)?\s+switch\w*"),
    ("arbitrage", r"\barbitrage\b"),
    # Money.
    ("cheaper than", r"\bcheaper\s+than\b"),
    ("for the price of", r"\bfor\s+the\s+price\s+of\b"),
    # Standing with the providers.
    ("official", r"\bofficial(?:ly)?\b"),
    ("partner", r"\bpartner(?:s|ed|ing|ship|ships)?\b"),
    ("integrated with", r"\bintegrated\s+with\b"),
    ("endorsed", r"\bendorse(?:s|d|ment|ments)?\b"),
    # Safety claims.
    ("safe", r"\bsaf(?:e|ely|er|est)\b"),
    ("secure", r"\bsecure(?:d|ly)?\b"),
    ("guaranteed", r"\bguarantee(?:s|d)?\b"),
    ("locked-down", r"\blocked[\s-]down\b"),
    ("nothing leaves your computer", r"\bnothing\s+leaves\s+your\b"),
    ("fully autonomous", r"\bfully\s+autonomous\b"),
    # Hebrew.
    ("לחסוך במכסה", _he(r"(?:לחסוך|חוסך|חיסכון|חסכון)\s+ב?(?:מכסה|מכסות|שימוש)")),
    ("למתוח או להגדיל את המנוי",
     _he(r"(?:למתוח|להגדיל|מותח|מגדיל)\s+(?:את\s+)?ה?(?:מנוי|מכסה)")),
    ("לעקוף מגבלות", _he(r"(?:לעקוף|עוקף|עוקפת|עקיפת)\s+ה?(?:מגבלה|מגבלות|מכסה)")),
    ("לא להיתקע במגבלה", _he(r"להיתקע\s+ב?ה?(?:מגבלה|מגבלות|מכסה)")),
    ("להמשיך כשנגמרת המכסה", _he(r"כשנגמר(?:ת|ה)?\s+ה?(?:מכסה|מנוי)")),
    ("יותר שימוש", _he(r"יותר\s+שימוש")),
    ("לאחד מנויים", _he(r"(?:לאחד|איחוד|מאחד)\s+(?:את\s+)?ה?(?:מנויים|מנוי|מכסות)")),
    ("ניתוב", _he("ניתוב", "ים")),
    ("מעבר אוטומטי", _he(r"מעבר\s+אוטומטי")),
    ("רשמי", _he("רשמי", "ת|ים|ות|יים")),
    ("שותף", _he("(?:שותף|שותפ)", "ה|ים|ות|ויות|ות")),
    ("בטוח", _he("בטוח", "ה|ים|ות")),
    ("מאובטח", _he("מאובטח", "ת|ים|ות")),
)

#: Lines that must quote a banned word to deny it, exactly as they appear
#: (stripped). Prefer rewording; every entry says why it cannot be reworded.
BANNED_PHRASE_ALLOWED_LINES = frozenset({
    # README "Not affiliated": the trademark disclaimer. Its wording is fixed
    # by the owner's legal review ("keep the Not affiliated section
    # unchanged"); "endorsed by or sponsored by" is the standard form a
    # reader looks for.
    "ModelQuad is an independent project. It is not affiliated with, endorsed by",
    # presentation.py `_REFUSAL_LINES`: the start of a closed reason code that
    # routing_policy.py writes and history stores; the table maps it to a
    # plain sentence, so a person never reads it.
    "routing is paused",
})

_BANNED = tuple((label, re.compile(pattern, re.IGNORECASE)) for label, pattern in BANNED_PHRASES)


def _wording_files() -> list[Path]:
    files: list[Path] = []
    for source in WORDING_SOURCES:
        path = REPO_ROOT / source.rstrip("/")
        if source.endswith("/"):
            if path.is_dir():
                files.extend(
                    p for p in sorted(path.rglob("*"))
                    if p.is_file() and p.suffix.lower() in WORDING_TEXT_SUFFIXES
                    and not EXCLUDED_DIRS.intersection(p.relative_to(REPO_ROOT).parts)
                )
        elif path.is_file():
            files.append(path)
    return files


def _server_instruction_texts() -> list[tuple[int, str]]:
    """(line, text) of every string constant assigned at module level."""
    path = REPO_ROOT / WORDING_SERVER_INSTRUCTIONS
    if not path.is_file():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    texts: list[tuple[int, str]] = []
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            for node in ast.walk(statement.value):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    texts.append((node.lineno, node.value))
    return texts


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the docstring constants to skip: all but the MCP tools' own."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            continue
        is_tool = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("modelquad_")
        if not is_tool:
            skipped.add(id(first.value))
    return skipped


_QUOTED_IDENTIFIER = re.compile(r"""([`'"])([A-Za-z_]+)(?=[`'":])""")


def _mask_allowed_identifiers(text: str) -> str:
    """Blank each allow-listed identifier where it is quoted as code."""
    def blank(match: re.Match[str]) -> str:
        if match.group(2) in BANNED_PHRASE_ALLOWED_IDENTIFIERS:
            return match.group(1) + "_" * len(match.group(2))
        return match.group(0)
    return _QUOTED_IDENTIFIER.sub(blank, text)


def _code_texts() -> list[tuple[str, int, str]]:
    """(source, line, text) of the people- and model-facing strings in code."""
    texts: list[tuple[str, int, str]] = []
    for source in WORDING_CODE_SOURCES:
        path = REPO_ROOT / source
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skipped and re.search(r"\s", node.value.strip())):
                texts.append((source, node.lineno, _mask_allowed_identifiers(node.value)))
    for source in WORDING_PLAIN_CODE_SOURCES:
        folder = REPO_ROOT / source.rstrip("/")
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix.lower() in WORDING_PLAIN_CODE_SUFFIXES:
                texts.append((_rel(path), 1, path.read_text(encoding="utf-8")))
    return texts


def _banned_in(text: str) -> list[tuple[int, str, str]]:
    """(1-based line, label, matched text) for each banned phrase in `text`."""
    lines = text.splitlines()
    found: list[tuple[int, str, str]] = []
    for label, pattern in _BANNED:
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = lines[line_no - 1].strip() if line_no <= len(lines) else ""
            if line in BANNED_PHRASE_ALLOWED_LINES:
                continue
            found.append((line_no, label, " ".join(match.group(0).split())))
    return sorted(found)


def _check_banned_phrases() -> list[str]:
    problems: list[str] = []
    for doc in _wording_files():
        for line_no, label, matched in _banned_in(doc.read_text(encoding="utf-8")):
            problems.append(f"{_rel(doc)}:{line_no}: {matched!r} ({label})")
    for line_no, text in _server_instruction_texts():
        for _, label, matched in _banned_in(text):
            problems.append(f"{WORDING_SERVER_INSTRUCTIONS}:{line_no}: {matched!r} ({label})")
    for source, first_line, text in _code_texts():
        for line_no, label, matched in _banned_in(text):
            problems.append(f"{source}:{first_line + line_no - 1}: {matched!r} ({label})")
    return problems


def check_docs() -> int:
    """Documentation invariants a human reviewer cannot reliably eyeball.

    Deliberately narrow: it checks things that are mechanically true or false,
    and never that a claim is *correct*. Nothing automated can do the second one.
    """
    groups = (
        ("relative links resolve", _check_links()),
        ("AGENTS.md Current state matches the code", _check_current_state()),
        ("documented commands work on Windows", _check_posix_only_commands()),
        ("public text uses none of the banned phrases", _check_banned_phrases()),
    )
    failed = False
    for label, problems in groups:
        if problems:
            failed = True
            print(f"FAIL  docs: {label} ({len(problems)})")
            for problem in problems[:20]:
                print(f"        {problem}")
            if len(problems) > 20:
                print(f"        ... and {len(problems) - 20} more")
        else:
            print(f"  ok  docs: {label}")
    print("FAIL  docs" if failed else "PASS  docs")
    return EXIT_FAILED if failed else EXIT_OK


# ----------------------------------------------------------- check: secrets


def _line_allows_secret(finding: dict) -> bool:
    """Is this finding's own line marked as a reviewed exception?

    Read from disk at the reported line rather than tracked in a side list, so
    the exemption lives where a reviewer sees it and disappears the moment the
    line does.
    """
    path = REPO_ROOT / finding["path"]
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:  # pragma: no cover - defensive
        return False
    index = finding["line"] - 1
    if 0 <= index < len(lines):
        return SECRET_ALLOW_MARKER in lines[index]
    return False


#: High-specificity credential shapes only. A generic "password"-ish rule
#: fires constantly, and a scanner that cries wolf is one that gets bypassed.
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("anthropic-api-key", r"sk-ant-[A-Za-z0-9_\-]{24,}"),
    ("openai-api-key", r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    ("github-token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    ("github-fine-grained-token", r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ("aws-access-key-id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("slack-token", r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"),
    ("private-key-block", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    ("signing-password", r"MODELQUAD_SIGN_PASSWORD\s*[:=]\s*\S+"),
)

#: Only text files we author. Compiled binaries are out of scope.
SECRET_SCAN_SUFFIXES = frozenset({
    ".yaml", ".yml", ".json", ".html", ".js", ".css", ".txt", ".md", ".cfg", ".ini",
    ".ps1", ".cmd", ".j2", ".tmpl", ".py", ".pyi", ".toml",
})
SECRET_SCAN_MAX_BYTES = 8 * 1024 * 1024


def scan_for_secrets(files: list[Path], *, root: Path) -> list[dict]:
    """One finding per (file, pattern) hit; the matched text is never kept.

    A scanner that prints the secret it found has published it.
    """
    findings: list[dict] = []
    compiled = [(name, re.compile(pattern)) for name, pattern in SECRET_PATTERNS]
    for path in files:
        if path.suffix.lower() not in SECRET_SCAN_SUFFIXES:
            continue
        try:
            if path.stat().st_size > SECRET_SCAN_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pattern in compiled:
            match = pattern.search(text)
            if match:
                try:
                    shown = path.relative_to(root).as_posix()
                except ValueError:
                    shown = str(path)
                findings.append({
                    "pattern": name,
                    "path": shown,
                    "line": text.count("\n", 0, match.start()) + 1,
                })
    return findings


def check_secrets() -> int:
    """Scan the authored source tree for credential shapes."""
    files = [
        path
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and not EXCLUDED_DIRS.intersection(path.parts)
        and path.suffix.lower() in SECRET_SCAN_SUFFIXES
    ]
    findings = [f for f in scan_for_secrets(files, root=REPO_ROOT) if not _line_allows_secret(f)]

    if findings:
        print(f"FAIL  secrets: {len(findings)} credential-shaped match(es)")
        for finding in findings:
            # The matched text is never echoed -- a scanner that prints what it
            # found has published it.
            print(f"        {finding['path']}:{finding['line']}  [{finding['pattern']}]")
        return EXIT_FAILED

    print(f"PASS  secrets ({len(files)} authored files, {len(SECRET_PATTERNS)} patterns)")
    return EXIT_OK


# --------------------------------------------------------------------- main

CHECKS = {
    "python": (check_python, "the Python suite, import root pinned to this worktree"),
    "docs": (check_docs, "links resolve, AGENTS.md matches code, commands run on Windows, no banned phrases"),
    "secrets": (check_secrets, "credential shapes in authored source"),
}

#: Not part of `all`: it is a subset of `python`, scoped to the diff.
FOCUSED = ("focused", (
    check_focused,
    "changed files compile, the package imports, the tests the change names and the repo-wide scans pass",
))

FAST = ("docs", "secrets")
#: The review gate: what a head must pass before an independent
#: reviewer's time is spent on it. Cheap by design; never the merge proof.
REVIEW = ("docs", "secrets", "focused")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check.py",
        description="ModelQuad pre-flight validation.",
    )
    parser.add_argument(
        "checks",
        nargs="*",
        help="one or more of: " + ", ".join(CHECKS) + ", fast, all (default: fast)",
    )
    parser.add_argument("--list", action="store_true", help="describe each check and exit")
    parser.add_argument(
        "--pace",
        choices=PACES,
        default=None,
        help="how hard the test run may load the machine: normal (default: every core, "
             "below-normal priority), gentle (half the workers too, for while you work) or "
             "full (normal priority, for an unattended run); also MODELQUAD_TEST_PACE",
    )
    parser.add_argument(
        "--repo",
        metavar="DIR",
        default=None,
        help="the repository tree to check (default: the one this script is in); lets a "
             "trusted copy of this script check a candidate that modifies it",
    )
    args = parser.parse_args(argv)
    if args.pace is not None:
        global PACE
        PACE = args.pace
    _apply_pace_priority()
    if args.repo:
        bound = _bind_repo(Path(args.repo))
        if not (bound / "scripts").is_dir() or not (bound / "src").is_dir():
            print(f"--repo {bound} is not a ModelQuad tree (no scripts/ and src/)", file=sys.stderr)
            return EXIT_UNAVAILABLE
        print(f"checking {bound} with the verifier at {Path(__file__).resolve()}", flush=True)

    if args.list:
        print("ModelQuad checks:\n")
        for name, (_, description) in CHECKS.items():
            print(f"  {name:<10} {description}")
        print(f"  {FOCUSED[0]:<10} {FOCUSED[1][1]}")
        print(f"\n  fast       {' + '.join(FAST)} (no test run, seconds)")
        print(f"  review     {' + '.join(REVIEW)} (the review gate: seconds to minutes)")
        print("  all        every check above except focused")
        return EXIT_OK

    requested = args.checks or ["fast"]
    selected: list[str] = []
    for name in requested:
        if name == "all":
            selected.extend(CHECKS)
        elif name == "fast":
            selected.extend(FAST)
        elif name == "review":
            selected.extend(REVIEW)
        elif name in CHECKS or name == FOCUSED[0]:
            selected.append(name)
        else:
            parser.error(f"unknown check {name!r}; try --list")
    # Preserve order, drop repeats.
    ordered = list(dict.fromkeys(selected))

    print(f"ModelQuad checks: {', '.join(ordered)}")
    print(f"repository: {REPO_ROOT}")

    results: dict[str, int] = {}
    for name in ordered:
        print(f"\n--- {name} " + "-" * (60 - len(name)))
        results[name] = (CHECKS[name] if name in CHECKS else FOCUSED[1])[0]()

    print("\n=== summary " + "=" * 52)
    for name, code in results.items():
        verdict = {EXIT_OK: "PASS", EXIT_FAILED: "FAIL", EXIT_UNAVAILABLE: "SKIP"}[code]
        print(f"  {verdict}  {name}")

    if any(code == EXIT_FAILED for code in results.values()):
        return EXIT_FAILED
    if any(code == EXIT_UNAVAILABLE for code in results.values()):
        # A skip is not a pass. It exits non-zero so a script cannot mistake
        # "a tool was missing" for "the check is green".
        return EXIT_UNAVAILABLE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
