# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Provider probes use synthetic files and recording fakes, never real CLIs."""

from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

import pytest

from orchestrator import catalog, codexcli, doctor, workerevidence
from orchestrator.usage import codex_appserver

# Bind before conftest's autouse isolation replaces the functions under test.
from orchestrator.workerevidence import _probe_version as real_version
from orchestrator.usage.codex_appserver import collect_codex_appserver_usage as real_codex_usage


win_only = pytest.mark.skipif(os.name != "nt", reason="Windows batch-shim semantics only")
STAMP = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


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


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    current = tmp_path / "planted cwd"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD;.BAT")
    codexcli.clear_cache()
    workerevidence.clear_version_cache()
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [])
    yield current
    codexcli.clear_cache()
    workerevidence.clear_version_cache()


@pytest.fixture
def launches(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = "codex-cli 0.160.0 --json --output-format\n"
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    class Child:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(
                json.dumps({"id": 1, "result": {}}) + "\n" +
                json.dumps({"id": 2, "result": {"rateLimits": {
                    "primary": {"usedPercent": 50, "windowDurationMins": 300},
                }}}) + "\n"
            )
            self.stderr = io.StringIO()
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 5 and self.terminated
            return 0

        def kill(self):
            pytest.fail("A completed recording fake must not need killing")

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Child()

    # The modules share subprocess; none of these fakes calls through.
    monkeypatch.setattr(codexcli.subprocess, "run", run)
    monkeypatch.setattr(codex_appserver.subprocess, "Popen", popen)
    return calls, run, popen


@pytest.mark.parametrize("kind", ["exe", pytest.param("cmd", marks=win_only)], ids=["exe", "cmd"])
def test_codex_from_cwd_is_never_probed(kind, monkeypatch, isolated, launches):
    calls, _, _ = launches
    planted = _file(isolated, "codex.cmd" if kind == "cmd" else _name("codex"))
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(isolated))))
    assert codexcli.resolve_codex_cli() is None
    assert calls == []
    assert all(_canonical(argv[0]) != _canonical(planted) for argv, _ in calls)


@win_only
@pytest.mark.parametrize(
    "case,path_version,official_version,source",
    [("path-newer", "0.161.0", "0.160.0", "path"),
     ("official-newer", "0.159.0", "0.160.0", "official"),
     ("tie-official", "0.160.0", "0.160.0", "official")],
    ids=["path-newer", "official-newer", "tie-official"],
)
def test_codex_npm_shim_on_absolute_path_still_selected(
    case, path_version, official_version, source, tmp_path, monkeypatch, isolated, launches,
):
    calls, _, _ = launches
    shim = _file(tmp_path / "npm install", "codex.cmd")
    official = _file(tmp_path / "official install", "codex.exe")
    monkeypatch.setenv("PATH", str(shim.parent))
    # No official install retains today's no-probe fallback and source.
    chosen = codexcli.resolve_codex_cli()
    assert chosen.source == "path" and _canonical(chosen.path) == _canonical(shim)
    assert calls == []
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [official])
    versions = {_canonical(shim): path_version, _canonical(official): official_version}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "codex-cli " + versions[_canonical(argv[0])], "")

    monkeypatch.setattr(codexcli.subprocess, "run", run)
    chosen = codexcli.resolve_codex_cli()
    assert chosen.source == source
    expected = shim if source == "path" else official
    assert _canonical(chosen.path) == _canonical(expected)
    assert chosen.version == versions[_canonical(expected)]
    assert chosen.stale_path_version == (path_version if case == "official-newer" else None)
    assert {_canonical(argv[0]) for argv, _ in calls} == set(versions)
    assert all(argv[1:] == ["--version"] for argv, _ in calls)


@pytest.mark.parametrize("case", ["relative", "directory"], ids=["relative", "directory"])
def test_refused_official_candidate_is_skipped(case, tmp_path, monkeypatch, isolated, launches):
    calls, _, _ = launches
    if case == "relative":
        _file(isolated, "codex.exe")
        candidate = Path(".") / "codex.exe"
    else:
        candidate = tmp_path / "official install" / "codex.exe"
        candidate.mkdir(parents=True)
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [candidate])
    assert codexcli.resolve_codex_cli() is None
    assert calls == []


def test_doctor_relative_codex_is_never_probed(tmp_path, monkeypatch, isolated, launches):
    calls, _, _ = launches
    _file(isolated, "codex.exe")
    # An official candidate makes doctor actually request the PATH probe.
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [tmp_path / "missing" / "codex.exe"])
    assert doctor._codex_stale_path_check(lambda _: ".\\codex.exe") is None
    assert calls == []


@pytest.mark.parametrize("binary", ["codex"], ids=["codex"])
def test_runtime_version_probe_uses_safe_lookup(binary, tmp_path, monkeypatch, isolated, launches):
    calls, _, _ = launches
    _file(isolated, _name(binary))
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(isolated))))
    assert real_version(binary) is None
    assert calls == []
    admitted = _file(tmp_path / "admitted install", _name(binary))
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(isolated), str(admitted.parent))))
    assert real_version(binary) == "codex-cli 0.160.0 --json --output-format"
    (argv, kwargs), = calls
    assert _canonical(argv[0]) == _canonical(admitted)
    assert argv[1:] == ["--version"]
    assert kwargs["shell"] is False and kwargs["encoding"] == "utf-8"


@pytest.mark.parametrize("case", ["planted-cwd", "admitted"], ids=["planted-cwd", "admitted"])
def test_codex_appserver_lookup(case, tmp_path, monkeypatch, isolated, launches):
    calls, _, popen = launches
    _file(isolated, _name("codex"))
    admitted = _file(tmp_path / "admitted install", _name("codex")) if case == "admitted" else None
    entries = [".", str(isolated)] + ([str(admitted.parent)] if admitted else [])
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    snapshot = real_codex_usage(popen=popen, now=lambda: STAMP)
    assert snapshot["available"] is (case == "admitted")
    if admitted:
        (argv, kwargs), = calls
        assert _canonical(argv[0]) == _canonical(admitted)
        assert argv[1:] == ["app-server"]
        assert kwargs["stdin"] is subprocess.PIPE and kwargs["shell"] is False
        assert snapshot["five_hour_remaining_percentage"] == 50
    else:
        assert calls == [] and snapshot["observed_at"] is None
        assert snapshot["reason"] == "codex CLI not found on PATH (looked for 'codex')."


@pytest.mark.parametrize("case", ["explicit-absolute-binary"], ids=["explicit-absolute-binary"])
def test_explicit_absolute_binary(case, tmp_path, isolated, launches):
    calls, _, popen = launches
    codex = _file(tmp_path / "admitted install", _name("codex"))
    assert _canonical(codexcli.which_codex(str(codex))) == _canonical(codex)
    assert real_codex_usage(binary=str(codex), popen=popen, now=lambda: STAMP)["available"] is True
    assert [_canonical(argv[0]) for argv, _ in calls] == [_canonical(codex)]


def test_refused_explicit_binary_does_not_publish_path(isolated, launches):
    calls, _, popen = launches
    planted = _file(isolated, _name("codex"))
    snapshot = real_codex_usage(binary=str(planted), popen=popen, now=lambda: STAMP)
    expected = "codex CLI not found on PATH (looked for 'codex')."
    assert snapshot["available"] is False and snapshot["reason"] == expected
    assert str(planted) not in repr(snapshot)
    assert calls == []


@win_only
@pytest.mark.parametrize("probe", ["codex-version", "app-server"], ids=["codex-version", "app-server"])
def test_batch_probe_env_suppresses_cwd_search(probe, tmp_path, monkeypatch, isolated, launches):
    calls, _, popen = launches
    shim = _file(tmp_path / "npm install", "codex.cmd")
    _file(isolated, "node.exe")
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", "relative", str(shim.parent))))
    original = dict(os.environ)
    if probe == "codex-version":
        assert codexcli._probe(str(shim)) == "0.160.0"
    else:
        assert real_codex_usage(popen=popen, now=lambda: STAMP)["available"] is True
    (argv, kwargs), = calls
    assert _canonical(argv[0]) == _canonical(shim)
    assert argv[1:] == {"codex-version": ["--version"], "app-server": ["app-server"]}[probe]
    env = kwargs["env"]
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert env["PATH"] == str(shim.parent)
    assert all(Path(entry).is_absolute() for entry in env["PATH"].split(os.pathsep))
    assert dict(os.environ) == original


def test_codex_probe_admission_precedes_cache(tmp_path, monkeypatch, isolated, launches):
    calls, _, _ = launches
    planted = _file(isolated, "codex.exe")
    details = planted.stat()
    key = (os.path.normcase(os.path.abspath(planted)), details.st_size, details.st_mtime_ns)
    codexcli._version_cache[key] = "99.0.0"
    assert codexcli._probe(str(planted)) is None
    assert calls == []


def test_the_claude_binary_is_never_probed(monkeypatch, launches):
    """Claude Code is the host; ModelQuad never launches it, even for --version."""
    calls, _, _ = launches
    from orchestrator import workerevidence

    workerevidence.clear_version_cache()
    assert workerevidence.runtime_version("claude") is None
    assert calls == []


def test_official_candidate_spelling_is_preserved(tmp_path, monkeypatch, isolated, launches):
    calls, _, _ = launches
    official = _file(tmp_path / "official install", "codex.exe")
    # The parent component changes spelling without changing the target.
    candidate = official.parent / ".." / official.parent.name / official.name
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [candidate])
    chosen = codexcli.resolve_codex_cli()
    assert chosen.source == "official" and chosen.path == str(candidate)
    (argv, _), = calls
    assert argv == [str(candidate), "--version"]


def test_no_raw_shutil_which_in_owned_modules():
    for module in (codexcli, workerevidence, codex_appserver, catalog):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "shutil")
            elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
                assert all(alias.name not in {"which", "*"} for alias in node.names), module.__name__
        # Attribute nodes include calls, default values and explicit forwarding;
        # string literals/docstrings do not become attributes in the AST.
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.Attribute) and node.attr == "which"
                and isinstance(node.value, ast.Name) and node.value.id in aliases
            ), module.__name__
