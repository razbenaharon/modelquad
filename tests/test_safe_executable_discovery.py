# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Discovery uses synthetic files only and never launches what it reports."""

from __future__ import annotations

import sys
import ast
from dataclasses import replace
import os
from pathlib import Path
import subprocess

import pytest

from orchestrator import (
    codexcli, doctor, hostruntime, onboarding, paths,
    provider_connection, registry, repair, statediag,
)
from orchestrator.config import Config
from orchestrator.statecontext import STATE_MODE_ENV_VAR, STATE_ROOT_ENV_VAR, StateMode


win_only = pytest.mark.skipif(os.name != "nt", reason="Windows PATH/PATHEXT and batch-shim semantics")


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
    tmp_path, monkeypatch, isolate_home_directory, isolate_usage_directory,
    isolate_provider_cli_spawns,
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
    # Same state-identity fixture policy as test_state_identity: installed mode
    # is explicit, rather than inherited from the runner's packaging shape.
    monkeypatch.setattr(paths, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    from orchestrator.statecontext import resolve_state_identity

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


def _plugin(binary):
    return next(plugin for plugin in registry.default_registry().all() if plugin.id == binary)


PROVIDERS = [
    pytest.param("claude", False, id="claude"),
    pytest.param("codex", False, id="codex"),
]


@pytest.mark.parametrize("binary,batch", [pytest.param("claude", False, id="claude")])
def test_registry_detection_ignores_planted_binaries(binary, batch, tmp_path, monkeypatch):
    host = _plugin(binary).host_adapters[0]
    assert host.detect() is False
    admitted = _file(tmp_path / "admitted install", binary + ".cmd" if batch else _name(binary))
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(Path.cwd()), str(admitted.parent))))
    assert host.detect() is True
    assert _canonical(hostruntime.discovery_which(binary)) == _canonical(admitted)


@pytest.mark.parametrize("binary,batch", PROVIDERS)
def test_detect_providers_ignores_planted_binaries(binary, batch, tmp_path, monkeypatch):
    active = registry.ProviderRegistry((_plugin(binary),))
    missing, = onboarding.detect_providers(registry=active)
    assert missing.cli_found is False and missing.cli_path is None
    admitted = _file(tmp_path / "admitted install", binary + ".cmd" if batch else _name(binary))
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(Path.cwd()), str(admitted.parent))))
    present, = onboarding.detect_providers(registry=active)
    assert present.cli_found is True and present.on_path is True
    assert _canonical(present.cli_path) == _canonical(admitted)


@pytest.mark.parametrize("case", [
    "relative-factory", "directory-named-like-exe", "admitted",
    "codex-fallback-unadmitted", "spelling-preserved",
], ids=[
    "relative-factory", "directory-named-like-exe", "admitted",
    "codex-fallback-unadmitted", "spelling-preserved",
])
def test_install_directory_fallback_admits_candidates(case, tmp_path, monkeypatch, isolated):
    if case == "codex-fallback-unadmitted":
        candidate = Path("codex.exe")  # Planted in CWD; P1 must never probe it.
        monkeypatch.setattr(codexcli, "official_candidates", lambda: [candidate])
        active = registry.ProviderRegistry((_plugin("codex"),))
        detected, = onboarding.detect_providers(registry=active)
        assert detected.cli_found is False and detected.cli_path is None
        return

    directory = tmp_path / "vendor install"
    candidate = directory / _name("claude")
    if case == "relative-factory":
        directory = Path("vendor install")
        _file(isolated / directory, _name("claude"))
    elif case == "directory-named-like-exe":
        candidate.mkdir(parents=True)
    else:
        _file(directory, _name("claude"))
        if case == "spelling-preserved":
            directory = directory / ".." / directory.name
        candidate = directory / _name("claude")
    plugin = replace(_plugin("claude"), install=registry.ProviderInstallInfo(
        docs_url="https://example.invalid", extra_search_dirs=(lambda: directory,),
    ))
    found = onboarding._find_in_install_dirs(plugin)
    detected, = onboarding.detect_providers(registry=registry.ProviderRegistry((plugin,)))
    if case in {"relative-factory", "directory-named-like-exe"}:
        assert found is None and detected.cli_found is False
    else:
        assert str(found) == str(candidate)
        assert detected.cli_found is True and detected.on_path is False
        assert detected.cli_path == str(candidate)


@pytest.mark.parametrize("case", [
    "cli-planted", "cli-admitted", "shell-planted-only", "shell-admitted", "shell-relative-entry",
], ids=[
    "cli-planted", "cli-admitted", "shell-planted-only", "shell-admitted", "shell-relative-entry",
])
def test_doctor_cli_check_and_shell_check(case, tmp_path, monkeypatch):
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    if case.startswith("cli"):
        if case == "cli-admitted":
            admitted = _file(tmp_path / "admitted install", _name("claude"))
            monkeypatch.setenv("PATH", str(admitted.parent))
        check = doctor._cli_check(
            "claude CLI", "claude", which=hostruntime.discovery_which,
            missing_status=doctor.CheckStatus.MISSING,
            missing_detail="Install Claude CLI; Claude delegation is unavailable.",
        )
        if case == "cli-admitted":
            assert check.status is doctor.CheckStatus.OK
            assert check.detail == f"Found at {hostruntime.discovery_which('claude')}."
        else:
            assert check.status is doctor.CheckStatus.MISSING
            assert check.detail == "Install Claude CLI; Claude delegation is unavailable."
        return
    empty = tmp_path / "empty path folder"
    empty.mkdir()
    if case == "shell-admitted":
        shell = _file(tmp_path / "admitted shell", "pwsh.exe")
        windows_path = str(shell.parent)
    elif case == "shell-relative-entry":
        windows_path = os.pathsep.join((".", str(empty)))
    else:
        windows_path = str(empty)
    check = doctor._codex_shell_check(windows_path=windows_path)
    assert check.status is (doctor.CheckStatus.OK if case == "shell-admitted" else doctor.CheckStatus.WARN)
    if check.status is doctor.CheckStatus.WARN:
        assert "No non-alias PowerShell/cmd found on PATH" in check.detail
        assert str(Path.cwd()) not in check.detail


CLASSIFIERS = [
    pytest.param(hostruntime.classify_registration, "modelquad-mcp", "", id="mcp"),
    pytest.param(hostruntime.classify_cli_registration, "modelquad", " hook-gate", id="cli"),
    pytest.param(hostruntime.classify_status_line_registration, "modelquad", " usage-hook claude", id="status-line"),
]


@pytest.mark.parametrize("classify,binary,suffix", CLASSIFIERS)
@pytest.mark.parametrize("case", [
    "bare-planted", "bare-admitted", "relative-dot", "relative-subdir", "explicit-absolute",
    pytest.param("root-relative", marks=win_only),
    pytest.param("drive-relative", marks=win_only),
], ids=[
    "bare-planted", "bare-admitted", "relative-dot", "relative-subdir", "explicit-absolute",
    "root-relative", "drive-relative",
])
def test_host_registration_classification(classify, binary, suffix, case, tmp_path, monkeypatch, isolated):
    _file(isolated, _name(binary))
    _file(isolated, binary + ".cmd")
    _file(isolated / "bin", _name(binary))
    absent = classify(binary + suffix)
    assert absent.explicit is False and absent.resolved is None
    assert absent.canonical is False
    if case == "bare-planted":
        verdict = absent
    elif case == "bare-admitted":
        admitted = _file(tmp_path / "admitted host", _name(binary))
        monkeypatch.setenv("PATH", str(admitted.parent))
        verdict = classify(binary + suffix)
        assert _canonical(verdict.resolved) == _canonical(admitted)
        # The pip-installed command on PATH is the canonical registration.
        assert verdict.canonical is True
        return
    elif case == "explicit-absolute":
        explicit = str(tmp_path / "absolute host" / _name(binary))
        # Absolute handling is structural and does not require an existing file.
        command = explicit if not suffix else hostruntime.render_launcher(explicit, shell="bash") + suffix
        verdict = classify(command)
        expected = explicit if not suffix else hostruntime.split_command(command)[0]
        assert verdict.explicit is True and verdict.resolved == expected
    else:
        relative = {
            "relative-dot": ".\\" + _name(binary),
            "relative-subdir": "bin/" + _name(binary),
            "root-relative": "\\x\\modelquad.cmd",
            "drive-relative": "C:modelquad.cmd",
        }[case]

        def unexpected_lookup(name):
            pytest.fail("a relative registration must not reach an injected resolver")

        verdict = classify(relative + suffix, which=unexpected_lookup)
        assert verdict.explicit is False and verdict.resolved is None
        assert "does not resolve on this PATH at all" in verdict.reason
    assert verdict.canonical is False


DEFAULTS = [
    "detect_providers", "run_onboarding", "run_doctor", "classify_registration",
    "classify_cli_registration", "classify_status_line_registration", "describe_connections",
    "run_repair", "survey_state",
    "detect-claude",
]


@pytest.mark.parametrize("entry", DEFAULTS, ids=DEFAULTS)
def test_defaults_resolve_at_call_time(entry, tmp_path, monkeypatch):
    # All entry points were imported before the hook is patched. Forwarding
    # boundaries stop before writes, but exercise which.
    class ReachedLookup(Exception):
        pass

    expected = None
    calls = []

    def boundary(*args, which, **kwargs):
        assert which("call-time-fixture") == expected
        raise ReachedLookup

    active = registry.ProviderRegistry((replace(_plugin("claude"), host_adapters=()),))
    invoke = None
    if entry == "run_onboarding":
        monkeypatch.setattr(provider_connection, "describe_connections", boundary)
        invoke = lambda: onboarding.run_onboarding(registry=active, dry_run=True, print_fn=lambda *a: None)
    elif entry == "run_doctor":
        monkeypatch.setattr(doctor, "_cli_check", boundary)
        # Keep unrelated state/registry/browser/loopback diagnostics outside
        # this default-forwarding test; none contributes to its assertion.
        for name in ("_state_consistency_check", "_control_plane_check"):
            monkeypatch.setattr(doctor, name, lambda **kw: doctor.DoctorCheck("fixture", doctor.CheckStatus.OK, ""))
        monkeypatch.setattr(doctor, "_service_pidfile_check", lambda *a, **kw: None)
        invoke = lambda: doctor.run_doctor(Config(), registry=active)
    elif entry == "describe_connections":
        monkeypatch.setattr(onboarding, "detect_providers", boundary)
        invoke = lambda: provider_connection.describe_connections(registry=active)
    elif entry == "run_repair":
        monkeypatch.setattr(repair, "run_doctor", boundary)
        invoke = lambda: repair.run_repair(registry=active, config=Config(), dry_run=True)
    elif entry == "survey_state":
        active_with_host = registry.ProviderRegistry((_plugin("claude"),))
        monkeypatch.setattr(statediag, "known_state_roots", lambda: [])
        monkeypatch.setattr(registry, "default_registry", lambda: active_with_host)
        monkeypatch.setattr(hostruntime, "read_registered_command", lambda *a: "modelquad-mcp")

    if entry.startswith("detect-"):
        detect = _plugin(entry.removeprefix("detect-")).host_adapters[0].detect

    for answer in (None, str(tmp_path / "call-time executable.exe")):
        expected = answer
        calls.clear()

        def hook(name):
            calls.append(name)
            return answer

        monkeypatch.setattr(hostruntime, "discovery_which", hook)
        if invoke is not None:
            with pytest.raises(ReachedLookup):
                invoke()
        elif entry == "detect_providers":
            detected, = onboarding.detect_providers(registry=active)
            assert detected.cli_path == expected
            assert detected.cli_found is (expected is not None)
        elif entry.startswith("classify_"):
            command = "modelquad-mcp" if entry == "classify_registration" else "modelquad usage-hook claude"
            assert getattr(hostruntime, entry)(command).resolved == expected
        elif entry == "survey_state":
            survey = statediag.survey_state()
            assert survey.hosts and all(host.resolved == expected for host in survey.hosts)
        else:
            assert detect() is (expected is not None)
        assert calls, "the entry point did not read the patched hook"


def test_no_raw_shutil_which_in_discovery_modules():
    for module in (registry, onboarding, doctor, hostruntime, provider_connection, repair, statediag):
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
