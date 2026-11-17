# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Discovery uses synthetic files only and never launches what it reports."""

from __future__ import annotations

import sys
import ast
import os
from pathlib import Path
import subprocess

import pytest

from modelquad import codexcli, hostruntime, paths, setupcheck
from modelquad.adapters import registry
from modelquad.statecontext import STATE_MODE_ENV_VAR, STATE_ROOT_ENV_VAR, StateMode


def _canonical(path):
    return os.path.normcase(os.path.realpath(path))


def _file(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"synthetic fixture - never execute\n")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def _name(binary):
    return binary + ".exe" if os.name == "nt" else binary


@pytest.fixture(autouse=True)
def isolated(
    tmp_path, monkeypatch, isolate_home_directory, isolate_provider_cli_spawns,
):
    current = tmp_path / "planted repository"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", "relative", str(current))))
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD;.BAT")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
        assert list(directory.iterdir()) == []
    monkeypatch.delenv(STATE_MODE_ENV_VAR, raising=False)
    monkeypatch.delenv(STATE_ROOT_ENV_VAR, raising=False)
    # Installed mode is explicit, rather than inherited from the runner's
    # packaging shape.
    monkeypatch.setattr(paths, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    from modelquad.statecontext import resolve_state_identity

    assert resolve_state_identity()[0] is StateMode.INSTALLED
    assert all(not (Path.home() / name).exists() for name in (".claude", ".codex"))
    for name in ("codex.exe", "codex.cmd", "claude.cmd", "pwsh.exe"):
        _file(current, name)
    if os.name != "nt":
        for name in ("codex", "claude", "pwsh"):
            _file(current, name)
    codexcli.clear_cache()
    assert codexcli.official_candidates() == []
    attempts = []

    def forbidden_spawn(self, *args, **kwargs):
        attempts.append(("Popen", args, kwargs))
        raise AssertionError("discovery attempted to start a process")

    def run(*args, **kwargs):
        attempts.append(("run", args, kwargs))
        raise AssertionError("discovery attempted a provider probe")

    # The initializer also covers Popen references bound before this fixture.
    monkeypatch.setattr(subprocess.Popen, "__init__", forbidden_spawn)
    monkeypatch.setattr(codexcli.subprocess, "run", run)
    yield current
    assert attempts == []
    codexcli.clear_cache()


@pytest.mark.parametrize("binary,batch", [pytest.param("claude", False, id="claude")])
def test_registry_detection_ignores_planted_binaries(binary, batch, tmp_path, monkeypatch):
    host = registry.get_host_descriptor(binary)
    assert host.detect() is False
    admitted = _file(tmp_path / "admitted install", binary + ".cmd" if batch else _name(binary))
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(Path.cwd()), str(admitted.parent))))
    assert host.detect() is True
    assert _canonical(hostruntime.discovery_which(binary)) == _canonical(admitted)


def test_codex_discovery_ignores_planted_binaries(tmp_path, monkeypatch):
    """The Codex a review would run is never one planted in the project folder."""
    assert codexcli.which_codex() is None
    admitted = _file(tmp_path / "admitted install", _name("codex"))
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(Path.cwd()), str(admitted.parent))))
    assert _canonical(codexcli.which_codex()) == _canonical(admitted)


def test_an_unadmitted_official_candidate_is_never_probed(monkeypatch):
    """A relative candidate (planted in the working folder) is refused before any probe."""
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [Path("codex.exe")])
    assert codexcli.which_codex() is None


def test_defaults_resolve_at_call_time(tmp_path, monkeypatch):
    # The entry point was imported before the hook is patched.
    calls = []
    for answer in (None, str(tmp_path / "call-time executable.exe")):
        calls.clear()

        def hook(name):
            calls.append(name)
            return answer

        monkeypatch.setattr(hostruntime, "discovery_which", hook)
        assert registry.get_host_descriptor("claude").detect() is (answer is not None)
        assert calls, "the entry point did not read the patched hook"


def test_no_raw_shutil_which_in_discovery_modules():
    for module in (registry, hostruntime, codexcli, setupcheck):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "shutil")
            elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
                assert all(alias.name not in {"which", "*"} for alias in node.names), module.__name__
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.Attribute) and node.attr == "which"
                and isinstance(node.value, ast.Name) and node.value.id in aliases
            ), module.__name__
