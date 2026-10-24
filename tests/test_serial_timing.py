# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Phase boundaries and failure propagation in scripts/check.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(path):
    spec = importlib.util.spec_from_file_location("_serial_timing_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.parametrize(("pace", "workers", "below_normal"), [
    ("normal", "auto", True), ("full", "auto", False), ("gentle", "half", True),
])
def test_pace_sets_workers_and_priority(monkeypatch, pace, workers, below_normal):
    check = load_script(ROOT / "scripts/check.py")
    monkeypatch.setattr(check, "PACE", pace)
    args = check._pytest_parallel_args()
    if args:
        expected = "auto" if workers == "auto" else str(max(2, (__import__("os").cpu_count() or 4) // 2))
        assert args == ["-n", expected]
    calls = []

    class _Kernel:
        def GetCurrentProcess(self):
            return -1

        def SetPriorityClass(self, handle, priority):
            calls.append(priority)
            return 1

    import ctypes
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _Kernel(), raising=False)
    applied = check._apply_pace_priority()
    if sys.platform == "win32":
        assert applied is below_normal
        assert calls == ([0x00004000] if below_normal else [])
