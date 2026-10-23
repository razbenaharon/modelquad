# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Collected identities, phase boundaries and failure propagation."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from conftest import no_window_kwargs

ROOT = Path(__file__).resolve().parents[1]
TIMING_NODES = {
    "tests/test_procutil_tree_kill.py::test_timeout_kills_the_whole_process_tree",
    "tests/test_rel1_caller_abandonment.py::test_the_caller_leaves_over_the_direct_dispatcher_and_the_worker_does_not",
    "tests/test_delegation_workspace_liveness.py::test_the_tree_kill_a_worker_timeout_uses_reaches_the_grandchild",
    "tests/test_rel1_worker_termination.py::test_the_timeout_carries_what_became_of_the_worker",
}


def load_script(path):
    spec = importlib.util.spec_from_file_location("_serial_timing_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_timing_nodes(arguments, *, env=None):
    child_env = dict(os.environ) if env is None else dict(env)
    child_env.pop("PYTEST_ADDOPTS", None)
    files = sorted({node.split("::")[0] for node in TIMING_NODES})
    # pytest walks ancestors down to confcutdir even with explicit files and
    # --rootdir. Cut collection at tests/, but load the real parent conftest
    # explicitly so its opt-in TEMP setup is retained, along with the real
    # tests/conftest.py and pyproject configuration.
    bootstrap = (
        "import importlib.util, pathlib, sys, pytest; "
        "path = pathlib.Path('conftest.py').resolve(); "
        "spec = importlib.util.spec_from_file_location('_timing_root_conftest', path); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "sys.exit(pytest.main(sys.argv[1:], plugins=[module]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, "--collect-only", "-q",
         "--rootdir", str(ROOT / "tests"), "--confcutdir", str(ROOT / "tests"),
         "-c", str(ROOT / "pyproject.toml"), "-o", "addopts=", *arguments, *files],
        cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=120,
        **no_window_kwargs(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    nodes = ["tests/" + line.strip() for line in result.stdout.splitlines()
             if line.startswith("test_") and "::" in line]
    assert nodes, "collection must produce actual test identities"
    assert len(nodes) == len(set(nodes)), "a selected test was collected twice"
    return set(nodes)


@pytest.mark.parametrize("plugin", [False, True])
@pytest.mark.parametrize("failure_phase", [None, 0, 1])
def test_preflight_runs_once_per_phase_and_stops_on_failure(monkeypatch, plugin, failure_phase, capsys):
    check = load_script(ROOT / "scripts/check.py")
    monkeypatch.setattr(check, "_pytest_parallel_args", lambda: ["-n", "auto"] if plugin else [])
    probe = types.SimpleNamespace(returncode=0, stdout=str(check.SRC / "modelquad/__init__.py"), stderr="")
    monkeypatch.setattr(check, "subprocess", types.SimpleNamespace(run=lambda *a, **k: probe))
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return 1 if len(calls) - 1 == failure_phase else 0

    monkeypatch.setattr(check, "_run", run)
    failed = failure_phase is not None and (plugin or failure_phase == 0)
    assert check.check_python() == (check.EXIT_FAILED if failed else check.EXIT_OK)
    assert len(calls) == (failure_phase + 1 if failed else 2 if plugin else 1)
    if plugin:
        assert calls[0][0][-4:] == ["-n", "auto", "-m", "not serial_timing"]
        if len(calls) == 2:
            assert calls[1][0][-4:] == ["-n", "0", "-m", "serial_timing"]
            assert calls[0][1] == calls[1][1]
    else:
        assert calls[0][0] == [sys.executable, "-m", "pytest", "-q"]
    output = capsys.readouterr().out
    assert "import root:" in output
    assert ("FAIL  python" if failed else "PASS  python (all phases)") in output


def test_standalone_trusted_checker_needs_no_new_phase_dependency(tmp_path):
    standalone = tmp_path / "scripts" / "check.py"
    standalone.parent.mkdir()
    standalone.write_bytes((ROOT / "scripts/check.py").read_bytes())
    check = load_script(standalone)
    assert check._load_devenv() is None
    assert check._pytest_phases(), "phase selection works without sibling modules or the product"


def test_real_collection_partitions_the_four_live_files_without_duplicates():
    """Use pytest collection, including all other tests in the same files.

    This catches module-wide marking, misspelled identities and overlapping
    selectors. It collects only; full-suite execution is run separately.
    """
    check = load_script(ROOT / "scripts/check.py")
    # The import root is the outer gate's choice (including installed mode).
    # Collection itself needs no xdist workers or inherited extra selectors.
    all_nodes = collect_timing_nodes([])
    # Apply the selectors the entry point generates. No workers are started
    # during collection; xdist's execution option is tested above.
    check._pytest_parallel_args = lambda: ["-n", "auto"]
    parallel_args = check._pytest_phases()[0][1][2:]
    serial_args = check._pytest_phases()[1][1][2:]
    parallel = collect_timing_nodes(parallel_args)
    serial = collect_timing_nodes(serial_args)
    assert serial == TIMING_NODES
    assert not parallel & serial
    assert parallel | serial == all_nodes
    assert len(all_nodes) > len(TIMING_NODES), "other tests in these files stay parallel"


def test_collection_never_scans_transient_root_probes(tmp_path):
    """Deterministically model the disappearing probe at the scandir seam.

    Any collection scan of the shared root fails, without racing a real file
    or writing to that root. The actual four files are still collected.
    """
    plugin = tmp_path / "timing_collection_guard.py"
    plugin.write_text('''
from pathlib import Path
import _pytest.main

def pytest_sessionstart(session):
    root = session.config.inipath.parent
    original = _pytest.main.scandir
    def guarded(path):
        if Path(path).resolve() == root.resolve():
            raise FileNotFoundError("disappearing test_devenv_probe_synthetic.py")
        return original(path)
    _pytest.main.scandir = guarded

def pytest_collection_finish(session):
    root = session.config.inipath.parent
    loaded = {Path(p.__file__).resolve() for p in session.config.pluginmanager.get_plugins()
              if getattr(p, "__file__", None)}
    assert root / "conftest.py" in loaded
    assert root / "tests" / "conftest.py" in loaded
    assert any(m.startswith("serial_timing:") for m in session.config.getini("markers"))
''', encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTEST_PLUGINS"] = ",".join(filter(None, [env.get("PYTEST_PLUGINS"), plugin.stem]))
    assert collect_timing_nodes(["-m", "serial_timing"], env=env) == TIMING_NODES


def test_collection_works_without_optional_pytest_plugins():
    """The no-xdist fallback must not require xdist's command-line flags."""
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    assert collect_timing_nodes(["-m", "serial_timing"], env=env) == TIMING_NODES
