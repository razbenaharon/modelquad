# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Install and uninstall treat the user's Claude Code files carefully.

One backup per changed file per run, taken from the original bytes; no
backup when nothing changes; the user's key order and characters kept; a file
that cannot be read safely is skipped with a reason while the rest proceeds;
links are never replaced; the hooks never fail closed; and nothing is written
on a platform ModelQuad does not support.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from modelquad import cli, cli_entry, hostruntime, usage_hook
from modelquad.adapters import gate
from modelquad.adapters import staging
from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.adapters.registration import RegistrationStatus
from modelquad.adapters.report import ActionKind
from modelquad.disclosure import DATA_FLOW
from modelquad.onboarding import install_detected_hosts

from test_host_registration_transaction import (  # noqa: F401 -- fixtures + helpers
    HOST_ID,
    _read_registration,
    _registry,
    _write_registration,
    claude_home,
    state_root,
)


@pytest.fixture(autouse=True)
def no_config_dir_override(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.modelquad-backup-*"))


def _mcp_config(home: Path) -> Path:
    return home.parent / f"{home.name}.json"


# ---------------------------------------------------------------------------
# Backups: once per file, from the original bytes, only when something changes
# ---------------------------------------------------------------------------


def test_backup_bytes_equal_the_pre_install_bytes(state_root, claude_home):
    settings = claude_home / "settings.json"
    mcp = _mcp_config(claude_home)
    settings_before = '{"zeta": 1, "alpha": "שלום"}\n'.encode("utf-8")
    settings.write_bytes(settings_before)
    mcp_before = b'{"mcpServers": {"other": {"command": "keep-other"}}, "numStartups": 3}'
    mcp.write_bytes(mcp_before)

    report = ClaudeAdapter().install(claude_home)

    assert [backup.read_bytes() for backup in _backups(settings)] == [settings_before]
    assert [backup.read_bytes() for backup in _backups(mcp)] == [mcp_before]
    settings_actions = [a for a in report.actions if a.path == settings and a.kind is ActionKind.UPDATED]
    assert settings_actions and {a.backup_path for a in settings_actions} == set(_backups(settings))


def test_a_run_that_changes_nothing_takes_no_backup(state_root, claude_home):
    (claude_home / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    (claude_home / "settings.json").write_text("{}", encoding="utf-8")
    ClaudeAdapter().install(claude_home)
    targets = [claude_home / "settings.json", claude_home / "CLAUDE.md", _mcp_config(claude_home)]
    counts = {path: len(_backups(path)) for path in targets}

    report = ClaudeAdapter().install(claude_home)

    assert all(action.backup_path is None for action in report.actions)
    assert {path: len(_backups(path)) for path in targets} == counts


def test_uninstall_backs_up_each_changed_file_once(state_root, claude_home):
    ClaudeAdapter().install(claude_home)
    assert not (claude_home / "CLAUDE.md").exists()
    targets = [claude_home / "settings.json", _mcp_config(claude_home)]
    originals = {path: path.read_bytes() for path in targets}
    counts = {path: len(_backups(path)) for path in targets}

    ClaudeAdapter().uninstall(claude_home)

    for path in targets:
        backups = _backups(path)
        assert len(backups) == counts[path] + 1
        assert originals[path] in {backup.read_bytes() for backup in backups}


def test_the_users_key_order_and_characters_are_kept(state_root, claude_home):
    settings = claude_home / "settings.json"
    settings.write_text(json.dumps({"zeta": 1, "alpha": "שלום"}, ensure_ascii=False), encoding="utf-8")

    ClaudeAdapter().install(claude_home)

    text = settings.read_text(encoding="utf-8")
    assert "שלום" in text and "\\u05e9" not in text
    assert list(json.loads(text))[:2] == ["zeta", "alpha"]


def test_install_and_onboard_never_write_permissions(state_root, claude_home, monkeypatch, tmp_path):
    """ModelQuad never allows its own tools on the user's behalf."""
    from modelquad.doctor import DoctorReport
    from modelquad.onboarding import run_onboarding

    settings = claude_home / "settings.json"
    ClaudeAdapter().install(claude_home)
    assert "permissions" not in json.loads(settings.read_text(encoding="utf-8"))
    assert "mcp__modelquad" not in settings.read_text(encoding="utf-8")

    ClaudeAdapter().uninstall(claude_home)
    monkeypatch.setattr("modelquad.onboarding.run_doctor", lambda *a, **k: DoctorReport(()))
    run_onboarding(
        selected_providers=["claude"], assume_yes=True, print_fn=lambda *a, **k: None,
        which=lambda _: None, registry=_registry(claude_home), db_path=tmp_path / "h.db",
    )
    assert "hooks" in json.loads(settings.read_text(encoding="utf-8")), "onboarding did install"
    assert "permissions" not in json.loads(settings.read_text(encoding="utf-8"))
    assert "mcp__modelquad" not in settings.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Unreadable files are skipped, never half-applied
# ---------------------------------------------------------------------------


def test_a_non_utf8_claude_md_is_left_alone_and_the_rest_installs(state_root, claude_home):
    claude_md = claude_home / "CLAUDE.md"
    original = "# הערות שלי\n".encode("cp1255")
    claude_md.write_bytes(original)

    report = ClaudeAdapter().install(claude_home)

    # ModelQuad no longer writes CLAUDE.md; one it cannot read is simply left alone.
    assert all(a.path != claude_md for a in report.actions)
    assert claude_md.read_bytes() == original
    assert _read_registration(claude_home) is not None
    assert "hooks" in json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))


def test_a_non_utf8_settings_file_is_skipped_whole(state_root, claude_home, capsys):
    settings = claude_home / "settings.json"
    original = '{"theme": "שלום"}'.encode("cp1255")
    settings.write_bytes(original)

    rc = cli.main(["install", "claude", "--target-dir", str(claude_home), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0, "the registration itself was written"
    skipped = [a for a in payload["actions"] if a["path"] == str(settings)]
    assert skipped and all(a["kind"] == "skipped" for a in skipped)
    assert settings.read_bytes() == original
    assert _read_registration(claude_home) is not None


def test_uninstall_survives_an_unreadable_file_and_still_restores(state_root, claude_home):
    original = {"command": "modelquad-mcp", "args": ["--mine"]}
    _write_registration(claude_home, original)
    assert cli.main(["install", "claude", "--target-dir", str(claude_home)]) == 0
    (claude_home / "CLAUDE.md").write_bytes("# שלום\n".encode("cp1255"))

    assert cli.main(["uninstall", "claude", "--target-dir", str(claude_home)]) == 0

    assert _read_registration(claude_home) == original
    settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
    assert "statusLine" not in settings and "hooks" not in settings


def test_install_claude_fails_when_the_registration_is_skipped(state_root, claude_home, capsys):
    _mcp_config(claude_home).write_text("not JSON", encoding="utf-8")

    rc = cli.main(["install", "claude", "--target-dir", str(claude_home), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["error"].startswith("Not registered")


def test_a_linked_settings_file_is_skipped_not_replaced(state_root, claude_home, monkeypatch):
    settings = claude_home / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(staging, "is_link", lambda path: Path(path) == settings)

    report = ClaudeAdapter().install(claude_home)

    actions = [a for a in report.actions if a.path == settings]
    assert actions and all(a.kind is ActionKind.SKIPPED for a in actions)
    assert all("link" in a.detail for a in actions)
    assert settings.read_text(encoding="utf-8") == "{}"


def test_a_real_symlinked_claude_md_stays_a_link(state_root, claude_home, tmp_path):
    real = tmp_path / "dotfiles" / "CLAUDE.md"
    real.parent.mkdir()
    real.write_text("# mine\n", encoding="utf-8")
    link = claude_home / "CLAUDE.md"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("creating a symlink needs Developer Mode or elevation here")

    report = ClaudeAdapter().install(claude_home)

    assert all(a.path != link for a in report.actions)
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "# mine\n"


# ---------------------------------------------------------------------------
# What install tells the user, and when
# ---------------------------------------------------------------------------


def test_the_data_flow_notice_comes_before_any_write(state_root, claude_home, monkeypatch):
    from modelquad import disclosure
    from modelquad.adapters import registration

    events: list[str] = []
    real_print = disclosure.print_lines
    real_install = registration.install_with_registration
    monkeypatch.setattr(disclosure, "print_lines",
                        lambda lines, *a: (events.append("notice"), real_print(lines, *a)))
    monkeypatch.setattr(registration, "install_with_registration",
                        lambda *a, **k: (events.append("install"), real_install(*a, **k))[1])

    assert cli.main(["install", "claude", "--target-dir", str(claude_home)]) == 0
    assert events == ["notice", "install"]


def test_the_json_install_carries_the_notice_as_a_field(state_root, claude_home, capsys):
    assert cli.main(["install", "claude", "--target-dir", str(claude_home), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data_flow_notice"] == list(DATA_FLOW)


def test_claude_config_dir_is_warned_about(state_root, claude_home, monkeypatch, capsys, tmp_path):
    from modelquad.config import Config
    from modelquad.doctor import CheckStatus, run_doctor
    from modelquad.service import control_plane as shell

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert cli.main(["install", "claude", "--target-dir", str(claude_home), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "CLAUDE_CONFIG_DIR" in payload["warnings"][0]

    report = run_doctor(
        Config(), claude_dir=claude_home, which=lambda _: None, usage_directory=tmp_path / "usage",
        control_plane_probe=lambda _: shell.ControlPlaneVerdict(shell.PROBE_ABSENT, "no listener"),
    )
    check = next(c for c in report.checks if c.name == "claude config folder")
    assert check.status is CheckStatus.WARN and "CLAUDE_CONFIG_DIR" in check.detail


def test_onboard_dry_run_says_replace_not_already_connected(state_root, claude_home):
    _write_registration(claude_home, {"command": "C:/someone-else/modelquad-mcp.exe"})
    outcome = install_detected_hosts([HOST_ID], dry_run=True, registry=_registry(claude_home))[0]
    assert outcome.status is RegistrationStatus.CONNECTED_REPLACED
    assert _read_registration(claude_home) == {"command": "C:/someone-else/modelquad-mcp.exe"}

    install_detected_hosts([HOST_ID], dry_run=False, registry=_registry(claude_home))
    again = install_detected_hosts([HOST_ID], dry_run=True, registry=_registry(claude_home))[0]
    assert again.status is RegistrationStatus.CONNECTED_UNCHANGED


# ---------------------------------------------------------------------------
# Platform refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["install", "claude", "--target-dir", "{home}"],
    ["install", "--global"],
    ["onboard", "--yes"],
    ["repair"],
    ["service", "install", "--dry-run"],
])
def test_setup_commands_refuse_off_native_windows(state_root, claude_home, monkeypatch, capsys, argv):
    monkeypatch.setattr(hostruntime, "unsupported_platform_message",
                        lambda **_: "ModelQuad supports native Windows only. Nothing was changed.")
    argv = [part.replace("{home}", str(claude_home)) for part in argv]

    assert cli.main(argv) == 1
    assert "native Windows only" in capsys.readouterr().err
    assert not any(claude_home.iterdir())
    assert not _mcp_config(claude_home).exists()


def test_unsupported_platform_message_names_wsl():
    assert hostruntime.unsupported_platform_message(os_name="nt") is None
    wsl = hostruntime.unsupported_platform_message(os_name="posix", environ={"WSL_DISTRO_NAME": "Ubuntu"})
    assert "native Windows only" in wsl and "WSL" in wsl
    kernel = hostruntime.unsupported_platform_message(os_name="posix", environ={},
                                                      release="5.15.0-microsoft-standard-WSL2")
    assert "WSL" in kernel
    linux = hostruntime.unsupported_platform_message(os_name="posix", environ={}, release="6.8.0")
    assert "native Windows only" in linux and "WSL" not in linux


# ---------------------------------------------------------------------------
# The installed hooks never fail closed, and read stdin as UTF-8 bytes
# ---------------------------------------------------------------------------


def _ansi_stdin(monkeypatch, payload: dict) -> None:
    """A stdin whose text layer is the ANSI code page, as on a CJK Windows."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252"))


def test_hook_gate_reads_a_cjk_path_from_utf8_bytes(monkeypatch, capsys):
    seen: dict = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(cli, "evaluate_gate", capture)
    _ansi_stdin(monkeypatch, {"tool_name": "Bash", "session_id": "s1",
                              "transcript_path": "C:\\用户\\项目\\会话.jsonl"})

    assert cli.main(["hook-gate"]) == 0
    assert seen["tool_name"] == "Bash"
    assert seen["transcript_path"] == "C:\\用户\\项目\\会话.jsonl"


def test_usage_hook_reads_a_cjk_path_from_utf8_bytes(monkeypatch, capsys, tmp_path):
    seen: list = []
    monkeypatch.setattr(usage_hook, "parse_claude_statusline_payload", lambda payload: seen.append(payload) or {})
    monkeypatch.setattr(usage_hook, "retain_last_known_claude", lambda snapshot, target: snapshot)
    monkeypatch.setattr(usage_hook, "write_usage_snapshot", lambda *a: None)
    monkeypatch.setattr(usage_hook, "status_line_text", lambda *a: "status")
    _ansi_stdin(monkeypatch, {"workspace": {"current_dir": "C:\\プロジェクト"}})

    assert cli.main(["usage-hook", "claude"]) == 0
    assert seen[0]["workspace"]["current_dir"] == "C:\\プロジェクト"


@pytest.mark.parametrize("entry", ["cli", "installed"])
@pytest.mark.parametrize("argv", [["hook-gate"], ["hook-gate", "--not-an-option"], ["usage-hook", "claude"]])
def test_hooks_exit_zero_and_print_nothing_on_any_failure(monkeypatch, capsys, argv, entry):
    """Exit code 2 from a PreToolUse hook blocks the user's tool call."""

    def explode(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "evaluate_gate", explode)
    monkeypatch.setattr(gate, "evaluate_gate", explode)
    monkeypatch.setattr(usage_hook, "parse_claude_statusline_payload", explode)
    _ansi_stdin(monkeypatch, {"tool_name": "Bash"})

    assert (cli.main if entry == "cli" else cli_entry.main)(argv) == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# tools --fix-path
# ---------------------------------------------------------------------------


class _Backend:
    def __init__(self, path):
        self.path, self.writes = path, []

    def get_path(self):
        return self.path, True

    def set_path(self, value, expandable):
        self.writes.append(value)

    def broadcast_change(self):
        pass


def test_path_entries_are_compared_after_expanding_variables(monkeypatch, tmp_path):
    from modelquad.pathenv import add_to_user_path

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    backend = _Backend(r"C:\Windows;%LOCALAPPDATA%\Programs\tool")
    assert add_to_user_path(tmp_path / "Local" / "Programs" / "tool", backend) is False
    assert backend.writes == []


def test_fix_path_dry_run_writes_nothing(tmp_path):
    from modelquad.onboarding import ProviderDetection
    from modelquad.provider_setup import fix_provider_path

    executable = tmp_path / "tool" / "claude.exe"
    executable.parent.mkdir()
    executable.touch()
    detection = ProviderDetection(
        provider_id="claude", display_name="Claude", cli_found=True, cli_path=str(executable),
        hosts=(), adapter_installed=False, on_path=False,
    )
    backend = _Backend(r"C:\Windows")
    result = fix_provider_path(detection, backend=backend, dry_run=True)
    assert result.ok and result.detail.startswith("Would add")
    assert backend.writes == []
