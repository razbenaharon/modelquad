# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Exact source launch inventory and Host integration boundary regressions.

Scope is src/modelquad only; scripts/ has separate guards. The allow-list
records admission evidence, not executable authenticity or launch authority.
Qualnames use class/function chains, <locals> for nested scopes, <lambda> for
lambdas and <module> for top-level expressions. Line numbers diagnose drift;
they never identify an allowed site. V composes after W02a (ce477bf).
"""

from __future__ import annotations

import ast
from collections import Counter
import os
from pathlib import Path

import pytest

from modelquad import executable_resolution as resolver, hostruntime


win_only = pytest.mark.skipif(os.name != "nt", reason="Windows system-directory/batch discovery semantics")
SPAWN_METHODS = frozenset({"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"})
WRAPPERS = frozenset({"run_with_tree_kill", "run_worker", "start_contained", "_launch_background_child", "_git",
                      "run_inventory", "bounded_run"})


def _subprocess_module_names(scope):
    # Copied semantics from test_procutil: imports anywhere in a file count.
    names = {"subprocess"}
    for node in ast.walk(scope):
        if isinstance(node, ast.Import):
            names.update(a.asname for a in node.names if a.name == "subprocess" and a.asname)
    return frozenset(names)


def _is_spawn_attribute(node, module_names):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in module_names and node.attr in SPAWN_METHODS)


def _spawn_aliases(scope, module_names):
    # Same tail-paired defaults, conditional assignment and lambda exclusion
    # as test_procutil.py; this copy has no dependency on another test module.
    aliases = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            aliases.update(a.asname or a.name for a in node.names if a.name in SPAWN_METHODS)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
            pairs += list(zip(args.kwonlyargs, args.kw_defaults))
            for parameter, default in pairs:
                if default is not None and _is_spawn_attribute(default, module_names):
                    aliases.add(parameter.arg)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None or isinstance(node.value, ast.Lambda):
                continue
            if any(_is_spawn_attribute(inner, module_names) for inner in ast.walk(node.value)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases.update(target.id for target in targets if isinstance(target, ast.Name))
    return aliases


def _inventory(source):
    tree = ast.parse(source)
    modules = {name: name for name in ("subprocess", "os", "pty", "asyncio", "shutil", "webbrowser")}
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported[alias.asname or alias.name] = (node.module or "") + "." + alias.name
    aliases = _spawn_aliases(tree, _subprocess_module_names(tree))
    injected = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and (
            node.arg in {"popen", "run", "runner", "spawner"} or node.arg.startswith("spawn")
        ):
            injected.add(node.arg)

    def spelling(node):
        if isinstance(node, ast.Name):
            return imported.get(node.id, modules.get(node.id, node.id))
        if isinstance(node, ast.Attribute):
            return spelling(node.value) + "." + node.attr
        return ""

    def launch_reference(node):
        name = spelling(node)
        receiver, _, method = name.rpartition(".")
        if receiver == "subprocess" and method in SPAWN_METHODS:
            return True
        if receiver == "os" and (method in {"system", "popen", "startfile"}
                                 or method.startswith(("exec", "spawn", "posix_spawn"))):
            return True
        if receiver == "pty" and method == "spawn":
            return True
        if receiver == "asyncio" and method in {"create_subprocess_exec", "create_subprocess_shell"}:
            return True
        if method in {"subprocess_exec", "subprocess_shell", "WinExec"} or method.startswith(("ShellExecute", "CreateProcess")):
            return True
        return method in WRAPPERS or name in WRAPPERS or name == "webbrowser.open"

    def environment_read(node):
        key = None
        if isinstance(node, ast.Subscript) and spelling(node.value) == "os.environ":
            key = node.slice
        elif isinstance(node, ast.Call) and spelling(node.func) in {"os.getenv", "os.environ.get"}:
            key = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "key"), None)
        return isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.casefold() in {"systemroot", "comspec"}

    sites = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.chain = []
            self.function = []

        def qualname(self):
            return ".".join(self.chain) or "<module>"

        def scoped(self, node, name, is_function):
            old = self.chain[:]
            if self.function and self.function[-1]:
                self.chain.append("<locals>")
            self.chain.append(name)
            self.function.append(is_function)
            # Type annotations are declarations, never executable references.
            if is_function:
                for decorator in getattr(node, "decorator_list", ()):
                    self.visit(decorator)
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if default is not None:
                        self.visit(default)
                body = node.body if isinstance(node.body, list) else [node.body]
                for child in body:
                    self.visit(child)
            else:
                self.generic_visit(node)
            self.function.pop()
            self.chain = old

        def visit_FunctionDef(self, node):
            self.scoped(node, node.name, True)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.scoped(node, node.name, False)

        def visit_Lambda(self, node):
            self.scoped(node, "<lambda>", True)

        def visit_AnnAssign(self, node):
            self.visit(node.target)
            if node.value is not None:
                self.visit(node.value)

        def visit_Call(self, node):
            target = node.func
            if environment_read(node) or spelling(target) == "shutil.which":
                sites.append((self.qualname(), "lookup", node.lineno))
            elif launch_reference(target) or (
                isinstance(target, ast.Name) and target.id in aliases | injected
            ) or (isinstance(target, ast.Attribute) and target.attr in {"_popen", "_run"}
                  and isinstance(target.value, ast.Name) and target.value.id == "self"):
                sites.append((self.qualname(), "launch", node.lineno))
            else:
                self.visit(target)
            for child in [*node.args, *(k.value for k in node.keywords)]:
                self.visit(child)

        def visit_Subscript(self, node):
            if environment_read(node):
                sites.append((self.qualname(), "lookup", node.lineno))
            self.generic_visit(node)

        def visit_Attribute(self, node):
            if launch_reference(node):
                sites.append((self.qualname(), "launch", node.lineno))
            elif spelling(node) == "shutil.which":
                sites.append((self.qualname(), "lookup", node.lineno))
            else:
                self.generic_visit(node)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load) and (launch_reference(node) or (
                node.id in imported and imported[node.id] == "shutil.which"
            )):
                kind = "lookup" if spelling(node) == "shutil.which" else "launch"
                sites.append((self.qualname(), kind, node.lineno))

    Visitor().visit(tree)
    return sites


# (module relative to src/modelquad, qualname, kind, count, admission reason).
# Exact counts include non-call references/defaults as well as actual calls.
# Sites that are not admitted are never listed here: they are pinned in
# OPEN_FINDINGS below with the same exact counts until their owners close them.
ALLOW_LIST = (
    ("buildinfo.py", "_git_head", "launch", 1,
     "resolver call: resolve_executable excludes enclosing repositories before the Git probe"),
    ("codexcli.py", "_probe", "launch", 1,
     "resolver call: admit_executable precedes both cached and live version probes"),
    ("execution/codex.py", "CodexExecutor.__init__", "launch", 1,
     "already-admitted value passed in: stored runner receives prepare_codex_launch output in _execute_noshell"),
    ("execution/codex.py", "CodexExecutor._execute_noshell", "launch", 1,
     "resolver call: native executable admission/digest qualification, then prepare_codex_launch before worker entry"),
    ("execution/codex.py", "CodexExecutor._execute_noshell.<locals>.inventory", "launch", 1,
     "forwarding wrapper, guard inside: run_inventory prepares bounded provider-free listing argv"),
    ("execution/file_rules.py", "git_ignored", "launch", 1,
     "resolver call: resolve_executable admits Git with all enclosing repository exclusions"),
    ("execution/noshell.py", "qualify", "launch", 1,
     "already-admitted value passed in: CodexExecutor admits the native executable and forwards through run_inventory"),
    ("execution/codex_worker_mcp.py", "run_inventory", "launch", 1,
     "resolver call: prepare_codex_launch admits argv and pins the batch system cmd"),
    ("gitmeta.py", "_git", "launch", 1,
     "resolver call: resolve_executable admits Git with all enclosing repository exclusions"),
    ("procutil.py", "_taskkill_tree", "launch", 1,
     "system_executable: Windows taskkill.path is pinned by the system-directory API"),
    ("procutil.py", "_windows_system_directory", "lookup", 1,
     "non-launch classification: environment fallback classifies a process image; never selects a launch"),
    ("procutil.py", "list_descendants", "launch", 2,
     "resolver call: POSIX resolve_executable pins ps argv[0] or returns unknown without launch; "
     "Windows uses the toolhelp API; includes default reference"),
    ("procutil.py", "process_creation_epoch", "launch", 2,
     "resolver call: POSIX resolve_executable pins ps argv[0] or returns unknown without launch; "
     "Windows reads the process handle; includes default reference"),
    ("setupcheck.py", "_codex", "launch", 1,
     "forwarding wrapper, guard inside: CodexExecutor.unavailable_reason admits the native executable "
     "first, then run_inventory prepares the bounded provider-free listing argv in an empty folder"),
    ("repo_policy.py", "find_root", "launch", 1,
     "forwarding wrapper, guard inside: gitmeta._git resolves/excludes Git; one rev-parse under a deadline"),
    ("workerevidence.py", "_probe_version", "launch", 1,
     "resolver call: probe_which/which_codex and admit_executable precede version argv"),
    # Classification of the sites an earlier pass left unlisted.
    ("procutil.py", "run_with_tree_kill", "launch", 1,
     "forwarding wrapper, guard at callers: run_worker"),
    ("procutil.py", "run_worker", "launch", 1,
     "forwarding wrapper, guard at callers: CodexExecutor default runner "
     "(argv from prepare_codex_launch) and codex_worker_mcp.run_inventory (prepare_codex_launch)"),
)

# Sites that are NOT admitted. Pinned with the same exact counts so a new or
# removed site still fails, but never presented as safe (task rule 1).
OPEN_FINDINGS = ()


def _inventory_problems(found, allowed):
    expected = Counter({(module, qualname, kind): count for module, qualname, kind, count, reason in allowed})
    actual = Counter((module, qualname, kind) for module, qualname, kind, line in found)
    problems = []
    for key in sorted(actual.keys() | expected.keys()):
        if actual[key] != expected[key]:
            locations = ", ".join(f"{module}:{line}" for module, qualname, kind, line in found
                                  if (module, qualname, kind) == key)
            problems.append(f"{key}: expected {expected[key]}, found {actual[key]} at {locations or 'site removed'}")
    return problems


def test_src_launches_use_central_resolution():
    root = Path(__file__).resolve().parents[1] / "src" / "modelquad"
    found = [(path.relative_to(root).as_posix(), qualname, kind, line)
             for path in sorted(root.rglob("*.py"))
             for qualname, kind, line in _inventory(path.read_text(encoding="utf-8-sig"))]
    pinned = ALLOW_LIST + OPEN_FINDINGS
    assert len({row[:3] for row in pinned}) == len(pinned), "duplicate allow-list key"
    assert all(count > 0 and reason for _, _, _, count, reason in pinned)
    assert not _inventory_problems(found, pinned), "\n".join(_inventory_problems(found, pinned))


def test_startup_probe_launch_is_a_fixed_interpreter():
    """PERF-1010-1: the fresh-process boundary probe launches only Python."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "tests" / "test_startup_imports.py").read_text(encoding="utf-8")
    allowed = (("test_startup_imports.py", "_fresh", "launch", 1,
                "fixed self-launch sys.executable: provider-free import boundary probe"),)
    found = [("test_startup_imports.py", qualname, kind, line)
             for qualname, kind, line in _inventory(source)]
    assert not _inventory_problems(found, allowed)
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_fresh")
    launch = next(node for node in ast.walk(function) if isinstance(node, ast.Call)
                  and ast.unparse(node.func) == "subprocess.run")
    assert ast.unparse(launch.args[0].elts[0]) == "sys.executable"


@pytest.mark.parametrize("source,qualname,count", [
    pytest.param('import subprocess\nsubprocess.run(["x"])', "<module>", 1, id="unlisted-run"),
    pytest.param('from subprocess import Popen as X\ndef f():\n X(["x"])', "f", 1, id="aliased-popen"),
    pytest.param('import subprocess\ndef f(run=subprocess.run):\n run(["x"])', "f", 2, id="default-alias"),
    pytest.param('import subprocess\ndef f(c, a):\n spawner = a if c else subprocess.Popen\n spawner(["x"])', "f", 2, id="conditional-spawner"),
    pytest.param('import subprocess\ndef f():\n forward(subprocess.Popen)', "f", 1, id="passed-launcher"),
    pytest.param('import subprocess\ndef f(proc: subprocess.Popen) -> subprocess.Popen:\n proc: subprocess.Popen', "f", 0, id="annotation-only"),
], ids=None)
def test_launch_inventory_detector(source, qualname, count):
    sites = _inventory(source)
    assert [(name, kind) for name, kind, _ in sites] == [(qualname, "launch")] * count
    found = [("synthetic.py", name, kind, line) for name, kind, line in sites]
    assert bool(_inventory_problems(found, ())) is bool(count)


def _file(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"inert fixture; never execute\n")
    if os.name != "nt":
        path.chmod(0o700)
    return path


@pytest.mark.parametrize("layout", [
    pytest.param("native-exe", id="native-exe"),
    pytest.param("npm-cmd-shim", id="npm-cmd-shim", marks=win_only),
    pytest.param("planted-current-folder", id="planted-current-folder"),
    pytest.param("relative-path-entry", id="relative-path-entry"),
    pytest.param("missing", id="missing"),
])
def test_discovery_finds_only_admitted_host_installs(layout, tmp_path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    directory = tmp_path / "install"
    directory.mkdir()
    search = str(directory)
    for name in ("claude", "codex"):
        filename = name + (".exe" if os.name == "nt" else "")
        if layout == "native-exe":
            _file(directory, filename)
        elif layout == "npm-cmd-shim":
            _file(directory, name + ".cmd")
        elif layout == "planted-current-folder":
            _file(current, filename)
            search = str(current)
        elif layout == "relative-path-entry":
            _file(current / "relative", filename)
            search = "relative"
    monkeypatch.setenv("PATH", search)
    answers = {name: hostruntime.discovery_which(name) for name in ("claude", "codex")}
    expected = layout in {"native-exe", "npm-cmd-shim"}
    assert all((path is not None) is expected for path in answers.values())


@win_only
def test_host_hook_normal_system_directory_spelling():
    directory = resolver._system_directory()
    if directory is None or os.path.normcase(directory) != os.path.normcase(r"C:\Windows\System32"):
        pytest.skip(r"Windows is not installed at C:\Windows on this machine")
    resolution = resolver.system_executable("cmd")
    assert resolution.path is not None
    assert hostruntime.render_launcher(resolution.path, shell="bash") == "C:/Windows/System32/cmd.exe"
