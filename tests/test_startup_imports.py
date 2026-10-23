# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Fresh-process startup boundaries; never start a provider or a service."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import orchestrator
import pytest

from conftest import no_window_kwargs


def _fresh(code):
    # Test the same package as the parent, including installed-mode gates.
    import_root = Path(orchestrator.__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(import_root)},
        capture_output=True, text=True, timeout=30, **no_window_kwargs(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


NO_SPAWNS = """
import sys
def forbid_spawn(event, args):
    if event == 'subprocess.Popen':
        raise AssertionError('startup must not spawn another process')
sys.addaudithook(forbid_spawn)
"""


def test_runner_launch_sources_do_not_import_mcp():
    root = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
    sources = [root / "jobs" / "runner.py", *(root / "execution").rglob("*.py")]
    for path in sources:
        package = "orchestrator." + ".".join(path.parent.relative_to(root).parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package)
                modules = [module, *(module + "." + alias.name for alias in node.names)]
            else:
                continue
            assert not any(module == "mcp" or module.startswith("mcp.") or
                           module == "orchestrator.mcp.server" for module in modules), path


def test_runner_delegation_imports_are_light():
    _fresh(NO_SPAWNS + """
import ast
import inspect
import sys
from orchestrator.jobs import runner

# Import the real delegation path's immediate dependencies without executing
# admission, history writes, repository probes or a worker.
function = ast.parse(inspect.getsource(runner._run_delegation)).body[0]
imports = ast.Module(body=[node for node in function.body
    if isinstance(node, (ast.Import, ast.ImportFrom))], type_ignores=[])
namespace = {}
exec(compile(imports, 'runner-delegation-imports', 'exec'), namespace)
assert namespace['_bind_launch'].__module__ == 'orchestrator.execution.bound_launch'
assert namespace['run_bound_delegation'].__module__ == 'orchestrator.execution.bound_launch'
assert 'orchestrator.mcp.server' not in sys.modules
assert not any(name == 'mcp' or name.startswith('mcp.') for name in sys.modules)
""")


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("count", [0, 1])
def test_hook_entry_imports_are_light(host, count):
    output = _fresh(NO_SPAWNS + f"""
import io
import json
import sys
from pathlib import Path
from orchestrator.cli_entry import main
from orchestrator.jobs import service_client

# Exercise valid input, directory hashing and serialization with an offline
# service reply, so an invalid-event shortcut cannot hide heavy imports.
service_client.read_pending_summary = lambda *args, **kwargs: {{
    'count': {count}, 'more': False, 'jobs': [{ {
        'job_id': 'startup-fixture', 'job_status': 'finished', 'task_type': 'review',
        'may_have_changed_files': False, 'expires_at': '2099-01-01T00:00:00Z',
        'expiring_soon': False,
    }!r}] if {count} else []}}
sys.stdin = io.StringIO(json.dumps({{'hook_event_name': 'SessionStart',
                                   'cwd': str(Path.cwd())}}))
sys.stdout = io.StringIO()
assert main(['hook-pending', {host!r}]) == 0
output = sys.stdout.getvalue()
assert bool(output) == bool({count})
if output:
    assert json.loads(output)['hookSpecificOutput']['hookEventName'] == 'SessionStart'
assert 'orchestrator.cli' not in sys.modules
assert 'orchestrator.mcp.server' not in sys.modules
assert 'orchestrator.service.app' not in sys.modules
assert not any(name == 'mcp' or name.startswith('mcp.') or
               name == 'fastapi' or name.startswith('fastapi.') for name in sys.modules)
""")
    assert output == ""


def test_worker_hook_entry_does_not_import_handler_or_read_input():
    _fresh(NO_SPAWNS + """
import os
import sys
from orchestrator.cli_entry import main
class ForbiddenInput:
    def read(self, *args):
        raise AssertionError('worker read stdin')
sys.stdin = ForbiddenInput()
os.environ['ORCHESTRATOR_WORKER'] = '1'
assert main(['hook-pending', 'claude']) == 0
assert 'orchestrator.adapters.pending_reminder' not in sys.modules
assert 'orchestrator.cli' not in sys.modules
""")


def test_other_commands_keep_cli_dispatch_and_arguments(monkeypatch):
    from types import SimpleNamespace
    from orchestrator.cli_entry import main

    calls = []
    monkeypatch.setitem(sys.modules, "orchestrator.cli", SimpleNamespace(
        main=lambda argv: calls.append(argv) or 7))
    args = ["--history-db", "fixture.db", "status"]
    assert main(args) == 7
    assert calls == [args]


def test_installed_commands_use_the_light_entry():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = config["project"]["scripts"]
    assert scripts["modelquad"] == "orchestrator.cli_entry:main"
    # The pre-rename `orchestrator` alias is not shipped.
    assert "orchestrator" not in scripts


@pytest.mark.parametrize("command", ["hook-pending", "usage-hook", "hook-gate", "mcp"])
def test_light_entry_preserves_live_protocol_stdout_and_discards_stderr(monkeypatch, command):
    import io
    from types import SimpleNamespace
    from orchestrator import cli_entry, windowless_diagnostics as diagnostics
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(diagnostics, "_active", False)
    monkeypatch.setattr(diagnostics, "_sink", None)
    monkeypatch.setattr(diagnostics, "bind_selected_state", lambda role: pytest.fail("protocol bound a log"))
    # The light handlers are bypassed so every protocol command reaches the
    # same fixture dispatch; they have their own tests.
    monkeypatch.setattr(cli_entry, "dispatch_pending", lambda argv: None)
    monkeypatch.setattr(cli_entry, "dispatch_usage_hook", lambda argv: None)
    monkeypatch.setattr(cli_entry, "dispatch_gate", lambda argv: None)
    def dispatch(argv):
        sys.stdout.write('{"fixture":"live-protocol"}\n')
        sys.stdout.flush()
        sys.stderr.write('private traceback canary')
        return 0
    monkeypatch.setitem(sys.modules, "orchestrator.cli", SimpleNamespace(main=dispatch))
    assert cli_entry.main([command]) == 0
    assert out.getvalue() == '{"fixture":"live-protocol"}\n'


def test_cli_import_failure_after_explicit_state_has_only_fixed_diagnostics(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from orchestrator import cli_entry, windowless_diagnostics as diagnostics
    default, chosen = tmp_path / "default", tmp_path / "chosen"
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(default))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(diagnostics, "_active", False)
    monkeypatch.setattr(diagnostics, "_sink", None)
    monkeypatch.setattr(diagnostics, "_budgets", {})
    def fail(argv):
        assert isinstance(sys.stdout, diagnostics.NullText)
        assert (chosen / "logs" / "service-diagnostics.log").exists()
        sys.stderr.write('private task contents canary')
        raise ImportError('private import exception canary')
    monkeypatch.setitem(sys.modules, "orchestrator.cli", SimpleNamespace(main=fail))
    assert cli_entry.main(["serve", "--managed-child", "--state-root", str(chosen)]) == 1
    lines = (chosen / "logs" / "service-diagnostics.log").read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["entry", "startup_failed", "exit"]
    assert not default.exists()


def test_broken_protocol_dispatch_cannot_escape_as_a_startup_traceback(monkeypatch):
    from types import SimpleNamespace
    from orchestrator import cli_entry, windowless_diagnostics as diagnostics
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(diagnostics, "_active", False)
    monkeypatch.setattr(diagnostics, "_sink", None)
    monkeypatch.setitem(sys.modules, "orchestrator.cli", SimpleNamespace(
        main=lambda argv: (_ for _ in ()).throw(OSError("private transport failure"))))
    assert cli_entry.main(["mcp"]) == 0
