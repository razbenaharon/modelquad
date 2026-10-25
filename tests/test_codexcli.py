# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which installed Codex CLI ModelQuad runs: newest wins, official on a tie (F-1005-2)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from modelquad import codexcli
from modelquad.codexcli import resolve_codex_cli, version_key, which_codex
from modelquad.execution.codex import CodexExecutor

PATH_CLI = "C:/npm/codex.cmd"
OFFICIAL_NEW = str(Path("C:/local/OpenAI/Codex/bin/8aaf/codex.exe"))
OFFICIAL_OLD = str(Path("C:/local/OpenAI/Codex/bin/codex.exe"))
# Bound at import, before the autouse fixture replaces it with "no install".
REAL_OFFICIAL_CANDIDATES = codexcli.official_candidates


def _resolve(versions: dict[str, str | None], *, on_path: str | None = PATH_CLI, official=(OFFICIAL_NEW, OFFICIAL_OLD)):
    probed: list[str] = []

    def probe(path: str) -> str | None:
        probed.append(path)
        return versions.get(path)

    chosen = resolve_codex_cli(
        which=lambda name: on_path if name == "codex" else None,
        candidates=lambda: [Path(path) for path in official],
        probe=probe,
    )
    return chosen, probed


def test_newest_official_wins_over_stale_path_copy():
    """The observed machine: npm 0.155.0 on PATH, official 0.160.0 and an old alpha."""
    chosen, _ = _resolve({PATH_CLI: "0.155.0", OFFICIAL_NEW: "0.160.0", OFFICIAL_OLD: "0.130.0-alpha.5"})
    assert chosen.path == str(Path(OFFICIAL_NEW)) and chosen.source == "official"
    assert chosen.version == "0.160.0" and chosen.stale_path_version == "0.155.0"


def test_newer_path_copy_wins_and_is_not_stale():
    chosen, _ = _resolve({PATH_CLI: "0.161.0", OFFICIAL_NEW: "0.160.0"})
    assert chosen.path == PATH_CLI and chosen.source == "path" and chosen.stale_path_version is None


def test_official_wins_a_tie():
    chosen, _ = _resolve({PATH_CLI: "0.160.0", OFFICIAL_NEW: "0.160.0"})
    assert chosen.source == "official" and chosen.stale_path_version is None


def test_prerelease_sorts_before_its_release():
    assert version_key("0.160.0-alpha.1") < version_key("0.160.0") < version_key("0.160.1")
    assert version_key("0.130.0-alpha.5") < version_key("0.155.0")
    with pytest.raises(ValueError):
        version_key("not a version")


@pytest.mark.parametrize("older,newer", [
    ("alpha.9", "alpha.10"),
    ("alpha.10", "alpha.beta"),
    ("9", "alpha"),
    ("alpha", "alpha.1"),
    ("alpha.1", "beta"),
    ("beta.11", "rc.1"),
])
def test_semver_prerelease_precedence(older, newer):
    assert version_key(f"0.160.0-{older}") < version_key(f"0.160.0-{newer}")


def test_newer_numeric_prerelease_wins_selection():
    chosen, _ = _resolve({PATH_CLI: "0.160.0-alpha.10", OFFICIAL_NEW: "0.160.0-alpha.9"})
    assert chosen.path == PATH_CLI and chosen.stale_path_version is None


def test_default_which_is_resolved_at_call_time(monkeypatch):
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [])
    monkeypatch.setattr(codexcli, "probe_which", lambda name: f"C:/mock/{name}")
    assert which_codex() == "C:/mock/codex"
    assert resolve_codex_cli().path == "C:/mock/codex"
    assert which_codex("claude") == "C:/mock/claude"


def test_no_official_install_keeps_the_path_lookup_without_probing():
    chosen, probed = _resolve({}, official=())
    assert chosen.path == PATH_CLI and chosen.source == "path" and chosen.version is None
    assert probed == []


def test_only_official_install():
    chosen, _ = _resolve({OFFICIAL_NEW: "0.160.0"}, on_path=None)
    assert chosen.source == "official" and chosen.stale_path_version is None


def test_unreadable_versions_fall_back_to_path():
    chosen, _ = _resolve({}, official=(OFFICIAL_NEW,))
    assert chosen.path == PATH_CLI and chosen.source == "path"


def test_nothing_installed():
    chosen, _ = _resolve({}, on_path=None, official=())
    assert chosen is None


def test_which_codex_leaves_other_binaries_alone():
    assert which_codex("claude", which=lambda name: f"C:/bin/{name}.exe") == "C:/bin/claude.exe"


@pytest.mark.skipif(codexcli.os.name != "nt", reason="the official install layout is Windows-only")
def test_official_candidates_reads_the_desktop_layout(tmp_path, monkeypatch):
    bin_dir = tmp_path / "OpenAI" / "Codex" / "bin"
    (bin_dir / "8aaf").mkdir(parents=True)
    (bin_dir / "8aaf" / "codex.exe").write_bytes(b"")
    (bin_dir / "rgonly").mkdir()
    (bin_dir / "rgonly" / "rg.exe").write_bytes(b"")
    (bin_dir / "codex.exe").write_bytes(b"")
    standalone = tmp_path / "Programs" / "OpenAI" / "Codex" / "bin"
    standalone.mkdir(parents=True)
    (standalone / "codex.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    found = sorted(str(path) for path in REAL_OFFICIAL_CANDIDATES())
    assert found == sorted([str(bin_dir / "8aaf" / "codex.exe"), str(bin_dir / "codex.exe"),
                            str(standalone / "codex.exe")])


def test_probe_reads_the_version_line_and_caches(tmp_path, monkeypatch):
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"x")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "codex-cli 0.160.0\n", "")

    monkeypatch.setattr(codexcli.subprocess, "run", run)
    codexcli.clear_cache()
    assert codexcli._probe(str(exe)) == "0.160.0"
    assert codexcli._probe(str(exe)) == "0.160.0"
    assert calls == [[str(exe), "--version"]]


def test_probe_runs_with_the_worker_environment_in_an_empty_folder(tmp_path, monkeypatch):
    """The version probe gets the same environment a Codex run gets: other
    services' secrets removed, the worker marker set, an empty folder."""
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"w")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_for_codex")
    seen = {}

    def run(argv, **kwargs):
        seen.update(env=kwargs["env"], cwd=kwargs["cwd"], files=list(Path(kwargs["cwd"]).iterdir()))
        return subprocess.CompletedProcess(argv, 0, "codex-cli 0.160.0\n", "")

    monkeypatch.setattr(codexcli.subprocess, "run", run)
    codexcli.clear_cache()
    assert codexcli._probe(str(exe)) == "0.160.0"
    assert "GITHUB_TOKEN" not in seen["env"] and seen["env"]["MODELQUAD_WORKER"] == "1"
    assert seen["files"] == [] and Path(seen["cwd"]) != Path.cwd()


@pytest.mark.parametrize("completed", [
    subprocess.CompletedProcess([], 1, "codex-cli 0.160.0", ""),
    subprocess.CompletedProcess([], 0, "", ""),
    subprocess.CompletedProcess([], 0, "no version here", ""),
])
def test_probe_unreadable_is_none(tmp_path, monkeypatch, completed):
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"y")
    monkeypatch.setattr(codexcli.subprocess, "run", lambda argv, **kwargs: completed)
    codexcli.clear_cache()
    assert codexcli._probe(str(exe)) is None


def test_probe_launch_failure_is_none(tmp_path, monkeypatch):
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"z")

    def run(argv, **kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(codexcli.subprocess, "run", run)
    codexcli.clear_cache()
    assert codexcli._probe(str(exe)) is None


def test_executor_launches_the_chosen_official_cli(tmp_path, monkeypatch, synthetic_worker_binary,
                                                   qualified_codex_inventory):
    from conftest import codex_event_stream

    official = Path(synthetic_worker_binary("codex.exe"))
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [official])
    monkeypatch.setattr(codexcli, "_probe", lambda path: {PATH_CLI: "0.155.0", str(official): "0.160.0"}.get(path))
    launched = []

    def run(argv, **kwargs):
        launched.append(argv)
        return subprocess.CompletedProcess(argv, 0, codex_event_stream(), "")

    from modelquad.execution import ExecutionRequest
    from modelquad.models import TaskType

    executor = CodexExecutor(which=lambda name: PATH_CLI if name == "codex" else None, run=run,
                             inventory=qualified_codex_inventory)
    assert executor.is_available()
    executor.execute(ExecutionRequest(provider="codex", task_type=TaskType.REVIEW, prompt="x",
                                      working_directory=tmp_path))
    assert os.path.normcase(os.path.realpath(launched[0][0])) == os.path.normcase(os.path.realpath(official))


def _npm_install(root: Path, layout: str) -> tuple[Path, Path]:
    """An npm global folder with the launcher and its vendored native binary."""
    root.mkdir(parents=True, exist_ok=True)
    launcher = root / "codex.cmd"
    launcher.write_text("@ECHO off\r\n", encoding="utf-8")
    scope = root / "node_modules" / "@openai"
    triple = "x86_64-pc-windows-msvc"
    native = {
        "nested": scope / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / triple / "bin",
        "hoisted": scope / "codex-win32-x64" / "vendor" / triple / "bin",
        "legacy": scope / "codex" / "vendor" / triple / "codex",
    }[layout] / "codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"synthetic fixture - never execute\n")
    return launcher, native


@pytest.mark.parametrize("layout", ["nested", "hoisted", "legacy"])
def test_an_npm_launcher_resolves_to_its_vendored_native_binary(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(codexcli.platform, "machine", lambda: "AMD64")
    launcher, native = _npm_install(tmp_path / "npm", layout)
    found = codexcli.npm_native_codex(str(launcher))
    assert found is not None
    assert os.path.normcase(found) == os.path.normcase(os.path.realpath(native))
    assert codexcli.which_codex(which=lambda name: str(launcher), native=True) == found
    # Selection without native keeps the launcher for fixed probes.
    assert codexcli.which_codex(which=lambda name: str(launcher)) == str(launcher)


def test_a_launcher_without_a_native_binary_is_shim_only(tmp_path, monkeypatch):
    monkeypatch.setattr(codexcli.platform, "machine", lambda: "AMD64")
    launcher = tmp_path / "npm" / "codex.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@ECHO off\r\n", encoding="utf-8")
    assert codexcli.which_codex(which=lambda name: str(launcher), native=True) is None
    assert codexcli.launcher_only(which=lambda name: str(launcher)) is True
    executor = CodexExecutor(which=lambda name: str(launcher), run=lambda *a, **k: pytest.fail("launched"))
    assert executor.is_available() is False
    assert executor.unavailable_reason() == "codex_shim_only"

    from modelquad.execution import ExecutionRequest
    from modelquad.execution.base import attempt_resolution
    from modelquad.models import TaskType

    result = executor.execute(ExecutionRequest(provider="codex", task_type=TaskType.REVIEW, prompt="x",
                                               working_directory=tmp_path))
    assert result.task_status_reason == "codex_shim_only"
    assert "codex.cmd" in result.error and "codex.exe" in result.error
    assert attempt_resolution(result) == ("refused", "policy_refused")


def test_prompt_launches_prefer_a_native_binary_over_a_newer_launcher(tmp_path, monkeypatch):
    """A newer npm codex.cmd with no native binary never beats an official codex.exe."""
    monkeypatch.setattr(codexcli.platform, "machine", lambda: "AMD64")
    launcher = tmp_path / "npm" / "codex.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@ECHO off\r\n", encoding="utf-8")
    versions = {str(launcher): "0.170.0", OFFICIAL_NEW: "0.160.0"}
    chosen = resolve_codex_cli(which=lambda name: str(launcher), candidates=lambda: [Path(OFFICIAL_NEW)],
                               probe=versions.get, native=True)
    assert chosen.path == str(Path(OFFICIAL_NEW)) and chosen.source == "official"
    plain = resolve_codex_cli(which=lambda name: str(launcher), candidates=lambda: [Path(OFFICIAL_NEW)],
                              probe=versions.get)
    assert plain.path == str(launcher)


def test_explicit_binary_is_not_redirected(tmp_path, monkeypatch):
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [tmp_path / "codex.exe"])
    executor = CodexExecutor(binary="C:/custom/codex.exe", which=lambda name: name)
    assert executor._resolve() == "C:/custom/codex.exe"


def test_doctor_warns_about_a_stale_path_copy(monkeypatch):
    from modelquad import doctor

    monkeypatch.setattr(doctor, "resolve_codex_cli", lambda which: codexcli.CodexCli(OFFICIAL_NEW, "official", "0.160.0", "0.155.0"))
    check = doctor._codex_stale_path_check(lambda name: PATH_CLI)
    assert check is not None and check.status is doctor.CheckStatus.WARN
    assert "0.155.0" in check.detail and "0.160.0" in check.detail


def test_doctor_is_quiet_without_a_stale_copy(monkeypatch):
    from modelquad import doctor

    monkeypatch.setattr(doctor, "resolve_codex_cli", lambda which: codexcli.CodexCli(PATH_CLI, "path", "0.160.0"))
    assert doctor._codex_stale_path_check(lambda name: PATH_CLI) is None


def test_a_codex_planted_under_the_project_is_never_found(tmp_path, monkeypatch):
    """A project folder can carry codex.exe in a folder an editor puts on PATH
    (an activated .venv). ModelQuad runs a found Codex for its version and its
    quota without a prompt, so nothing under the working folder is found."""
    home = tmp_path / "home"
    project = home / "projects" / "app"
    planted = project / ".venv" / "Scripts"
    planted.mkdir(parents=True)
    (planted / "codex.exe").write_bytes(b"MZ planted - never run\n")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(planted))
    monkeypatch.chdir(project)
    assert codexcli.project_roots() == (os.path.normcase(os.path.realpath(project)),)
    assert codexcli.probe_which("codex") is None


@pytest.mark.parametrize("where", ["home", "above-home"])
def test_a_working_folder_at_or_above_home_does_not_hide_the_users_own_install(tmp_path, monkeypatch, where):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home if where == "home" else tmp_path)
    assert codexcli.project_roots() == ()


def test_the_quota_refresh_never_finds_a_codex_planted_under_the_project(tmp_path, monkeypatch):
    """The app-server quota read runs without a prompt, so its own lookup
    must refuse a project-local codex.exe too."""
    from modelquad.usage import codex_appserver

    home = tmp_path / "home"
    project = home / "projects" / "app"
    planted = project / ".venv" / "Scripts"
    planted.mkdir(parents=True)
    (planted / "codex.exe").write_bytes(b"MZ planted - never run\n")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(planted))
    monkeypatch.chdir(project)
    assert codex_appserver.probe_which("codex") is None
