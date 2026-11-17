# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Fresh-process startup boundaries: a hook never loads the CLI or the MCP server."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import modelquad
import pytest

from conftest import no_window_kwargs


def _fresh(code):
    # Test the same package as the parent, including installed-mode gates.
    import_root = Path(modelquad.__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(import_root)},
        capture_output=True, text=True, timeout=120, **no_window_kwargs(),
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

#: What a hook process must never have loaded when it exits.
NOTHING_HEAVY = """
heavy = sorted(name for name in sys.modules if name in ('modelquad.cli', 'modelquad.mcp.server')
               or name.split('.')[0] in ('mcp', 'pydantic', 'anyio'))
assert not heavy, heavy
"""


def test_execution_sources_do_not_import_mcp():
    root = Path(__file__).resolve().parents[1] / "src" / "modelquad"
    sources = sorted((root / "execution").rglob("*.py"))
    assert sources
    for path in sources:
        package = ".".join(("modelquad", *path.parent.relative_to(root).parts))
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
                           module == "modelquad.mcp.server" for module in modules), path


@pytest.mark.parametrize("event", [
    # An edit: the reminder counts the file and may write its record.
    {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "startup-fixture",
     "tool_input": {"file_path": "a.py"}},
    # The end of a turn: the review gate's path.
    {"hook_event_name": "Stop", "session_id": "startup-fixture"},
], ids=["edit", "stop"])
def test_the_review_hook_imports_are_light(event, tmp_path):
    output = _fresh(NO_SPAWNS + f"""
import io
import json
import os
from modelquad.cli_entry import main

# Valid input in a real folder, so an invalid-event shortcut cannot hide heavy imports.
os.environ['MODELQUAD_STATE_ROOT'] = {str(tmp_path / "state")!r}
event = dict({event!r}, cwd={str(tmp_path)!r})
if 'tool_input' in event:
    event['tool_input']['file_path'] = os.path.join({str(tmp_path)!r}, event['tool_input']['file_path'])


class Stdin:
    buffer = io.BytesIO(json.dumps(event).encode('utf-8'))


sys.stdin = Stdin()
assert main(['hook-review', 'claude']) == 0
assert 'modelquad.adapters.review_reminder' in sys.modules
""" + NOTHING_HEAVY)
    assert output == ""


@pytest.mark.parametrize("argv", [
    ["hook-pending", "claude"], ["hook-gate"], ["usage-hook", "claude", "--quiet"],
    ["--usage-directory", "DIR", "usage-hook", "claude"],
])
def test_a_retired_hook_command_imports_nothing_and_reads_nothing(argv):
    output = _fresh(NO_SPAWNS + f"""
from modelquad.cli_entry import main


class ForbiddenInput:
    def read(self, *args):
        raise AssertionError('a retired hook read stdin')
    buffer = property(lambda self: self.read())


sys.stdin = ForbiddenInput()
assert main({argv!r}) == 0
loaded = sorted(name for name in sys.modules if name.startswith('modelquad'))
assert loaded == ['modelquad', 'modelquad.cli_entry', 'modelquad.netpolicy'], loaded
""" + NOTHING_HEAVY)
    assert output == ""


def test_worker_hook_entry_does_not_import_handler_or_read_input():
    _fresh(NO_SPAWNS + """
import os
from modelquad.cli_entry import main
class ForbiddenInput:
    def read(self, *args):
        raise AssertionError('worker read stdin')
    buffer = property(lambda self: self.read())
sys.stdin = ForbiddenInput()
os.environ['MODELQUAD_WORKER'] = '1'
assert main(['hook-review', 'claude']) == 0
assert 'modelquad.adapters.review_reminder' not in sys.modules
assert 'modelquad.cli' not in sys.modules
""")


def test_importing_the_cli_does_not_load_the_mcp_server():
    _fresh(NO_SPAWNS + """
import modelquad.cli
modelquad.cli.build_parser()
assert 'modelquad.mcp.server' not in sys.modules
assert not any(name.split('.')[0] in ('mcp', 'pydantic', 'anyio') for name in sys.modules)
""")


def test_other_commands_keep_cli_dispatch_and_arguments(monkeypatch):
    from types import SimpleNamespace
    from modelquad.cli_entry import main

    calls = []
    monkeypatch.setitem(sys.modules, "modelquad.cli", SimpleNamespace(
        main=lambda argv: calls.append(argv) or 7))
    args = ["settings", "show"]
    assert main(args) == 7
    assert calls == [args]


def test_installed_commands_use_the_light_entry():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = config["project"]["scripts"]
    assert scripts["modelquad"] == "modelquad.cli_entry:main"
    assert set(scripts) == {"modelquad", "modelquad-mcp"}
