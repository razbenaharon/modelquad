# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Worker launch boundaries: synthetic files and recording fakes only.

The single real launch is an ASCII batch recorder using this interpreter.
No test starts an installed provider or makes a provider/model call.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from modelquad import codexcli, executable_resolution as resolver, procutil
from modelquad.execution import codex, codex_worker_mcp as mcp, noshell
from modelquad.execution.base import ExecutionRequest, WorkerTaskStatus
from modelquad.models import TaskType


win_only = pytest.mark.skipif(os.name != "nt", reason="Windows batch and CreateProcess semantics only")


def canonical(path):
    return os.path.normcase(os.path.realpath(path))


def file(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"synthetic fixture - never execute\n")
    if os.name != "nt":
        path.chmod(0o755)
    return path


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    current = tmp_path / "process cwd"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD;.BAT")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    assert codexcli.official_candidates() == []  # conftest's provider isolation
    codexcli.clear_cache()
    yield current
    codexcli.clear_cache()


@pytest.fixture
def launches(monkeypatch):
    from conftest import codex_event_stream

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, codex_event_stream(), "")

    monkeypatch.setattr(mcp, "run_worker", run)
    # These tests own launch admission, not feature qualification.
    monkeypatch.setattr(codex, "_probe", lambda executable: "0.160.0")
    monkeypatch.setattr(noshell, "qualify", lambda executable, version, **kwargs:
                        noshell.Qualification(True, noshell.file_digest(executable), version, True,
                                              disabled_features=noshell.DISABLED_FEATURES))
    return calls, run


def request(tmp_path, provider="codex", **kwargs):
    working = tmp_path / "request folder"
    working.mkdir(exist_ok=True)
    return ExecutionRequest(provider=provider, task_type=TaskType.REVIEW,
                            prompt="בדוק את הקוד", working_directory=working, **kwargs)


def codex_executor(run, **kwargs):
    return codex.CodexExecutor(run=run, **kwargs)


@pytest.mark.parametrize("provider,case", [
    pytest.param("codex", "cwd-exe", marks=win_only),
    pytest.param("codex", "cwd-cmd", marks=win_only),
    ("codex", "relative-path"),
], ids=["codex-cwd-exe", "codex-cwd-cmd", "codex-relative-path"])
def test_worker_never_launches_planted_binary(provider, case, tmp_path, monkeypatch, isolated, launches):
    calls, run = launches
    name = provider + (".cmd" if case == "cwd-cmd" else ".exe" if os.name == "nt" else "")
    folder = isolated / "relative" if case == "relative-path" else isolated
    planted = file(folder, name)
    monkeypatch.setenv("PATH", "relative" if case == "relative-path" else "")
    worker = codex_executor(run)
    result = worker.execute(request(tmp_path, provider=worker.name))
    assert result.task_status_reason == "cli_not_found"
    assert worker.is_available() is False
    assert calls == []
    assert all(canonical(argv[0]) != canonical(planted) for argv, _ in calls)


@pytest.mark.parametrize("provider,case", [
    ("codex", "direct"), ("codex", "sibling"), ("codex", "link"),
], ids=["codex", "sibling-allowed", "link-in-working-directory"])
def test_worker_refuses_binary_in_request_working_directory(provider, case, tmp_path, isolated, launches):
    calls, run = launches
    req = request(tmp_path, provider=provider)
    folder = req.working_directory if case == "direct" else tmp_path / "sibling install"
    candidate = file(folder, "codex.exe")
    if case == "link":
        link = req.working_directory / "codex.exe"
        try:
            link.symlink_to(candidate)
        except OSError:
            pytest.skip("File symlinks cannot be created on this account")
        candidate = link
    result = codex_executor(run, which=lambda _: str(candidate)).execute(req)
    if case == "sibling":
        assert result.task_status_reason != "unsafe_executable"
        assert canonical(calls[-1][0][0]) == canonical(candidate)
    else:
        assert result.task_status_reason == "unsafe_executable"
        assert result.task_status is WorkerTaskStatus.FAILED and result.success is False
        assert result.safe_to_retry is False
        assert result.metadata == {"executable_refusal": "excluded_cwd"}
        assert result.error.endswith("excluded_cwd).") or result.error.endswith("excluded_cwd.")
        assert str(candidate) not in result.error
        assert calls == []


@win_only
@pytest.mark.parametrize("case", ["probe", "comspec-poisoned"])
def test_codex_batch_shim_uses_pinned_cmd(case, tmp_path, monkeypatch, isolated, launches):
    calls, run = launches
    shim = file(tmp_path / "npm install $ עברית (x86)", "codex.cmd")
    planted_cmd = file(isolated, "cmd.exe")
    if case == "comspec-poisoned":
        monkeypatch.setenv("ComSpec", str(planted_cmd))
    monkeypatch.setenv("PATH", os.pathsep.join(("relative", str(shim.parent))))
    req = request(tmp_path)
    command = resolver.system_executable("cmd").path
    assert command is not None
    logical = [str(shim), "features", "list", *noshell.QUALIFY_FEATURE_ARGS]
    mcp.run_inventory(logical, str(req.working_directory), dict(os.environ))
    line, kwargs = calls[-1]
    payload = '""' + os.path.realpath(shim) + '" ' + subprocess.list2cmdline(logical[1:]) + '"'
    assert line == '"' + command + '" /d /v:off /s /c ' + payload
    assert kwargs["executable"] == command
    assert canonical(command) != canonical(planted_cmd)
    assert "input" not in kwargs
    assert kwargs["env"]["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert kwargs["env"]["PATH"] == str(shim.parent)


@win_only
def test_a_prompt_never_goes_through_a_batch_launcher(tmp_path, monkeypatch, isolated, launches):
    calls, run = launches
    shim = file(tmp_path / "npm install", "codex.cmd")
    monkeypatch.setenv("PATH", str(shim.parent))
    result = codex.CodexExecutor(run=run).execute(request(tmp_path))
    assert result.task_status_reason == "codex_shim_only"
    assert calls == []


@win_only
@pytest.mark.parametrize("value,role", [
    ("gpt&calc", "model"), ("gpt%PATH%", "model"), ("gpt-5\n", "model"),
    ("server name", "config"), ('server"name', "config"), ("session^calc", "argument"),
], ids=["model-ampersand", "model-percent", "model-trailing-newline", "config-space", "config-quote",
        "argument-caret"])
def test_codex_batch_argument_policy(value, role, tmp_path, isolated):
    shim = file(tmp_path / "npm install", "codex.cmd")
    flag = {"model": ["-m"], "config": ["-c"], "argument": []}[role]
    with pytest.raises(mcp.UnsafeBatchArgument) as caught:
        mcp.prepare_codex_launch([str(shim), "exec", *flag, value], str(isolated))
    assert caught.value.role == role and value not in str(caught.value)


def test_fixed_launch_literals_pass_the_batch_policy():
    args = ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "-", "--json", "-m", "-c",
            "features", "list", 'sandbox_mode="read-only"', 'web_search="disabled"', *noshell.NOSHELL_ARGS]
    args += [f"model_reasoning_effort={effort}" for effort in codex.VALID_REASONING_EFFORTS]
    for arg in args:
        mcp.check_batch_argument(arg, "argument")


@win_only
@pytest.mark.parametrize("character", list('%!&|<>^'), ids=["percent", "bang", "ampersand", "pipe", "lt", "gt", "caret"])
def test_codex_batch_executable_argument_policy(character, tmp_path, isolated):
    if character in '|<>':
        # Windows filenames cannot contain these in a synthetic file.
        pytest.skip("Windows forbids this character in a filename")
    shim = file(tmp_path / ("npm " + character), "codex.cmd")
    with pytest.raises(mcp.UnsafeBatchArgument) as caught:
        mcp.prepare_codex_launch([str(shim), "features", "list"], str(isolated))
    assert caught.value.role == "executable"


def test_codex_native_exe_argv_unchanged(tmp_path, monkeypatch, isolated, launches):
    calls, run = launches
    candidate = file(tmp_path / "official install", "codex.exe")
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [candidate])
    monkeypatch.setattr(codexcli, "_probe", lambda _: "0.160.0")
    req = request(tmp_path)
    object.__setattr__(req, "model", "gpt&native")
    result = codex_executor(run).execute(req)
    assert canonical(calls[-1][0][0]) == canonical(candidate)
    assert calls[-1][0][1:] == ["--ask-for-approval", "never", "exec", "-m", "gpt&native",
                                *noshell.NOSHELL_ARGS, "--skip-git-repo-check", "-"]
    assert "executable" not in calls[-1][1]
    assert result.metadata["command"][0] == "codex.exe"


@win_only
def test_cmd_resolution_failure_refuses_batch_launch(tmp_path, monkeypatch, isolated):
    shim = file(tmp_path / "npm install", "codex.cmd")
    monkeypatch.setattr(mcp, "system_executable", lambda _: resolver.Resolution(None, "system", None, "system_api_failed"))
    with pytest.raises(resolver.UnsafeExecutable) as caught:
        mcp.prepare_codex_launch([str(shim), "features", "list"], str(isolated))
    assert caught.value.reason == "system_api_failed"


@win_only
def test_batch_arguments_arrive_intact_windows(tmp_path, isolated):
    folder = tmp_path / "recorder $ עברית space"
    shim = file(folder, "codex.cmd")
    shim.write_bytes(b'@"%MQ_RECORDER%" "%~dp0rec.py" %*\r\n')
    (folder / "rec.py").write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "(root / 'argv.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "(root / 'stdin.bin').write_bytes(sys.stdin.buffer.read())\n", encoding="ascii",
    )
    args = ["exec", "resume", "-c", 'sandbox_mode="read-only"', "-c",
            "mcp_servers.fixed_server.enabled=false", "session-fixed", "-"]
    line, kwargs = mcp.prepare_codex_launch([str(shim), *args], str(isolated))
    prompt = "בדוק את הקוד בעברית"
    env = resolver.safe_child_env({**os.environ, "MQ_RECORDER": sys.executable})
    completed = procutil.run_with_tree_kill(line, input=prompt, cwd=str(isolated),
                                          capture_output=True, text=True, encoding="utf-8",
                                          errors="replace", timeout=60, shell=False, env=env, **kwargs)
    assert completed.returncode == 0, completed.stderr
    assert json.loads((folder / "argv.json").read_text(encoding="utf-8")) == args
    assert (folder / "stdin.bin").read_bytes().decode("utf-8") == prompt


@win_only
def test_procutil_passes_batch_string_and_suspension_to_popen(tmp_path, monkeypatch, isolated):
    shim = file(tmp_path / "npm install", "codex.cmd")
    line, kwargs = mcp.prepare_codex_launch([str(shim), "mcp", "list", "--json"], str(isolated))
    seen = []

    class Recorded(Exception):
        pass

    def popen(argv, **kw):
        seen.append((argv, kw))
        raise Recorded  # Stop before Job APIs; this test launches nothing.

    monkeypatch.setattr(procutil.subprocess, "Popen", popen)
    with pytest.raises(Recorded):
        procutil.run_with_tree_kill(line, shell=False, **kwargs)
    assert seen[0][0] == line and isinstance(seen[0][0], str)
    assert seen[0][1]["executable"] == kwargs["executable"]
    assert seen[0][1]["creationflags"] & procutil._CREATE_SUSPENDED
    assert "shell" not in seen[0][1]


def test_no_raw_shutil_which_in_worker_modules():
    for module in (codex, mcp):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "shutil")
            elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
                assert all(alias.name not in {"which", "*"} for alias in node.names), module.__name__
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Attribute) and node.attr == "which"
                        and isinstance(node.value, ast.Name) and node.value.id in aliases), module.__name__
