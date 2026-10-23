# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Coverage for provider install guidance and PATH repair.

Both behaviours exist because of the 2026-08-22 clean-machine run: Claude
Code's official Windows installer finished without wiring PATH, and ModelQuad
then reported the working install as absent -- byte-identically to it not
being installed.
"""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from orchestrator.onboarding import ProviderDetection, _find_in_install_dirs, detect_providers
from orchestrator.provider_setup import fix_provider_path, providers_needing_path_fix
from orchestrator.registry import ProviderInstallInfo, ProviderRegistry, default_registry


class FakeEnvBackend:
    """Minimal user-PATH backend mirroring product.pathenv's interface."""

    def __init__(self, path: str | None = ""):
        self.path = path
        self.broadcast_called = False

    def get_path(self):
        return self.path, False

    def set_path(self, value, is_expandable):
        self.path = value

    def broadcast_change(self):
        self.broadcast_called = True


def _detection(**kwargs) -> ProviderDetection:
    base = dict(
        provider_id="claude",
        display_name="Claude",
        cli_found=True,
        cli_path=None,
        hosts=(),
        adapter_installed=False,
        on_path=False,
        install=None,
    )
    base.update(kwargs)
    return ProviderDetection(**base)


def _claude_plugin_with(install):
    """A claude plugin carrying the given install info.

    Built by replacing the field on a fresh copy rather than mutating the
    shared registry: ``default_registry()`` constructs new plugin objects on
    every call, so a monkeypatched instance is silently discarded by the next
    caller -- which is exactly how the first version of these tests fooled
    itself into passing.
    """
    plugin = next(p for p in default_registry().all() if p.id == "claude")
    return replace(plugin, install=install)


# --- detection must not equate "not on PATH" with "not installed" ----------


def test_installed_but_not_on_path_is_detected(tmp_path, monkeypatch):
    """The exact clean-machine failure: same binary, PATH the only difference."""
    monkeypatch.setattr(sys, "platform", "win32")
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "claude.exe").write_text("", encoding="utf-8")

    plugin = _claude_plugin_with(
        ProviderInstallInfo(
            docs_url="https://example.invalid", extra_search_dirs=(lambda: bin_dir,)
        )
    )
    found = _find_in_install_dirs(plugin)
    assert found is not None
    assert found.name.lower() == "claude.exe"


def test_detection_reports_on_path_false_for_fallback_hits(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude.exe").write_text("", encoding="utf-8")
    plugin = _claude_plugin_with(
        ProviderInstallInfo(
            docs_url="https://example.invalid", extra_search_dirs=(lambda: bin_dir,)
        )
    )

    detections = detect_providers(
        which=lambda name: None, registry=ProviderRegistry((plugin,))
    )
    claude = next(d for d in detections if d.provider_id == "claude")
    assert claude.cli_found is True
    assert claude.on_path is False


def test_missing_everywhere_stays_missing(tmp_path):
    plugin = _claude_plugin_with(
        ProviderInstallInfo(
            docs_url="https://example.invalid", extra_search_dirs=(lambda: tmp_path,)
        )
    )
    assert _find_in_install_dirs(plugin) is None


def test_search_dir_that_raises_is_not_fatal():
    """A probe of an unreadable directory means "not there", never a crash."""

    def explode():
        raise OSError("unreadable")

    plugin = _claude_plugin_with(
        ProviderInstallInfo(docs_url="https://example.invalid", extra_search_dirs=(explode,))
    )
    assert _find_in_install_dirs(plugin) is None


# --- PATH repair -----------------------------------------------------------


def test_fix_path_adds_the_directory_holding_the_executable(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")
    backend = FakeEnvBackend(path="C:/existing")

    result = fix_provider_path(_detection(cli_path=str(exe)), backend=backend)

    assert result.ok is True
    assert str(tmp_path) in backend.path
    assert "existing" in backend.path, "existing PATH entries must be preserved"
    assert backend.broadcast_called is True


def test_fix_path_refuses_a_path_that_no_longer_exists(tmp_path):
    """Detection can go stale; PATH must not gain an empty directory."""
    result = fix_provider_path(
        _detection(cli_path=str(tmp_path / "gone" / "claude.exe")), backend=FakeEnvBackend()
    )
    assert result.ok is False
    assert "no longer exists" in result.detail


def test_fix_path_is_a_no_op_when_already_reachable():
    result = fix_provider_path(_detection(on_path=True), backend=FakeEnvBackend())
    assert result.ok is True
    assert "Already on PATH" in result.detail


def test_hashed_only_codex_is_detected_but_never_written_to_path(tmp_path, monkeypatch):
    from orchestrator import codexcli

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    exe = tmp_path / "OpenAI" / "Codex" / "bin" / "8aaf" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"test")
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [exe])
    monkeypatch.setattr(codexcli, "_probe", lambda _: "0.160.0")
    plugin = next(p for p in default_registry().all() if p.id == "codex")
    plugin = replace(plugin, host_adapters=())
    detected, = detect_providers(which=lambda _: None, registry=ProviderRegistry((plugin,)))
    assert detected.cli_found is True
    assert detected.cli_path == str(exe)
    assert codexcli.resolve_codex_cli(which=lambda _: None).path == detected.cli_path

    class NoWriteBackend(FakeEnvBackend):
        def get_path(self):
            pytest.fail("A desktop version folder must not access the PATH writer")

    backend = NoWriteBackend(path="C:/existing")
    result = fix_provider_path(detected, backend=backend)
    assert result.ok is False
    assert "ModelQuad can use this Codex install directly" in result.detail
    assert backend.path == "C:/existing"
    assert backend.broadcast_called is False

    # Reject a stale caller-provided detection too, before constructing the
    # real registry backend when none was supplied.
    assert fix_provider_path(detected).ok is False


@pytest.mark.parametrize("layout", [
    ("OpenAI", "Codex", "bin"),
    ("Programs", "OpenAI", "Codex", "bin"),
])
def test_stable_codex_folders_keep_path_repair(tmp_path, monkeypatch, layout):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    exe = tmp_path.joinpath(*layout) / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"test")
    backend = FakeEnvBackend(path="C:/existing")
    result = fix_provider_path(
        _detection(provider_id="codex", cli_path=str(exe)), backend=backend,
    )
    assert result.ok is True
    assert str(exe.parent) in backend.path
    assert backend.broadcast_called is True


def test_fix_path_surfaces_a_registry_failure_instead_of_claiming_success(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")

    class Failing(FakeEnvBackend):
        def get_path(self):
            raise OSError("registry unavailable")

    result = fix_provider_path(_detection(cli_path=str(exe)), backend=Failing())
    assert result.ok is False
    assert "Could not update PATH" in result.detail


def test_providers_needing_path_fix_selects_only_unreachable_installs():
    detections = (
        _detection(provider_id="a", on_path=False, cli_path="x"),
        _detection(provider_id="b", on_path=True, cli_path="y"),
        _detection(provider_id="c", cli_found=False, on_path=True),
    )
    assert [d.provider_id for d in providers_needing_path_fix(detections)] == ["a"]


# --- the shipped metadata must stay honest --------------------------------


@pytest.mark.parametrize("layout, needs_fix", [
    (("OpenAI", "Codex", "bin", "8aaf"), False),
    (("OpenAI", "Codex", "bin"), True),
    (("Programs", "OpenAI", "Codex", "bin"), True),
])
def test_path_fix_selection_excludes_only_codex_version_folders(tmp_path, monkeypatch, layout, needs_fix):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    codex = _detection(provider_id="codex", cli_path=str(tmp_path.joinpath(*layout) / "codex.exe"))
    claude = _detection(cli_path=str(tmp_path / "claude.exe"))
    expected = (claude, codex) if needs_fix else (claude,)
    assert providers_needing_path_fix((claude, codex)) == expected


@pytest.mark.parametrize("with_claude", [False, True])
def test_tools_fix_path_skips_hashed_codex(tmp_path, monkeypatch, capsys, with_claude):
    from orchestrator import onboarding, provider_setup
    from orchestrator.cli import main

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    codex = _detection(
        provider_id="codex",
        cli_path=str(tmp_path / "OpenAI" / "Codex" / "bin" / "8aaf" / "codex.exe"),
    )
    claude_exe = tmp_path / "claude" / "claude.exe"
    claude_exe.parent.mkdir()
    claude_exe.write_bytes(b"test")
    claude = _detection(cli_path=str(claude_exe))
    detections = (claude, codex) if with_claude else (codex,)
    monkeypatch.setattr(onboarding, "detect_providers", lambda: detections)
    fixed = []
    backend = FakeEnvBackend()
    real_fix = fix_provider_path

    def fix(detection, **kwargs):
        fixed.append(detection.provider_id)
        return real_fix(detection, backend=backend, **kwargs)

    monkeypatch.setattr(provider_setup, "fix_provider_path", fix)
    assert main(["tools", "--fix-path"]) == 0
    output = capsys.readouterr().out
    assert fixed == (["claude"] if with_claude else [])
    if with_claude:
        assert "[ok] claude" in output
        assert str(claude_exe.parent) in backend.path
        assert "] codex:" not in output
    else:
        assert "Nothing to fix" in output
        assert backend.path == ""


def test_every_installable_provider_documents_how_to_get_it():
    """A provider with a CLI but no install info is the dead end we removed."""
    for plugin in default_registry().all():
        if plugin.cli_executable is None:
            continue  # credential-based; there is nothing to install
        assert plugin.install is not None, f"{plugin.id} has no install guidance"
        assert plugin.install.docs_url.startswith("https://"), plugin.id


def test_install_commands_point_at_vendor_domains():
    """Guard against a command that fetches a script from anywhere else.

    These strings tell users to pipe a remote script into their shell, so the
    host it comes from is a security property, not a formatting detail.
    """
    allowed = ("claude.ai", "chatgpt.com", "npmjs", "winget", "npm ")
    for plugin in default_registry().all():
        info = plugin.install
        if info is None:
            continue
        for command in (info.command, info.alternative_command):
            if command:
                assert any(token in command for token in allowed), f"{plugin.id}: {command}"
