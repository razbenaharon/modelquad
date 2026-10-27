# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad install claude --plugin``: the plugin's copies are the only ones.

The ModelQuad plugin carries the server, the commands and the reminder. With
the regular install beside it Claude Code would run each twice, so install
for the plugin keeps only the status line and takes back ModelQuad's own
copies -- and only what is provably ModelQuad's, with a backup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad import cli, doctor
from modelquad.adapters.claude_adapter import ClaudeAdapter, plugin_recorded
from modelquad.adapters.registration import install_with_registration
from modelquad.adapters.report import ActionKind
from modelquad.adapters.server_instructions import read_claude_plugin, write_claude_plugin

from test_host_registration_transaction import claude_home, state_root  # noqa: F401 -- fixtures

_SKILLS = ("modelquad", "modelquad-review", "modelquad-handoff", "modelquad-quota", "modelquad-setup")


@pytest.fixture(autouse=True)
def no_config_dir_override(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _mcp_config(home: Path) -> Path:
    return home.parent / f"{home.name}.json"


def _servers(home: Path) -> dict:
    if not _mcp_config(home).exists():
        return {}
    return json.loads(_mcp_config(home).read_text(encoding="utf-8")).get("mcpServers", {})


def _settings(home: Path) -> dict:
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))


def _record_plugin(home: Path, key: str = "modelquad@modelquad", value=None) -> None:
    folder = home / "plugins"
    folder.mkdir(exist_ok=True)
    entry = value if value is not None else [{"scope": "user", "version": "0.4.0"}]
    (folder / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {key: entry}}), encoding="utf-8"
    )


def _regular_install(home: Path) -> None:
    install_with_registration("claude", home, ClaudeAdapter(via_plugin=False))
    assert "modelquad" in _servers(home)
    assert all((home / "skills" / name / "SKILL.md").is_file() for name in _SKILLS)


def test_installing_for_the_plugin_takes_back_the_regular_copies(state_root, claude_home):
    _regular_install(claude_home)

    report = install_with_registration("claude", claude_home, ClaudeAdapter(via_plugin=True))

    assert "modelquad" not in _servers(claude_home)
    # Each folder keeps only the backup of the removed copy.
    assert not any((claude_home / "skills" / name / "SKILL.md").exists() for name in _SKILLS)
    settings = _settings(claude_home)
    assert "usage-hook claude" in settings["statusLine"]["command"]
    assert "hook-pending" not in json.dumps(settings.get("hooks", {}))
    removed = [action for action in report.actions if action.kind is ActionKind.REMOVED]
    assert removed and all(action.backup_path is not None for action in removed)
    assert any("plugin supplies the server" in (action.detail or "") for action in report.actions)


def test_a_server_entry_install_did_not_write_stays_and_is_reported(state_root, claude_home):
    _mcp_config(claude_home).write_text(json.dumps({"mcpServers": {"modelquad": {
        "command": "C:/Tools/my-own-modelquad.exe", "args": ["--mine"]}}}), encoding="utf-8")

    report = ClaudeAdapter(via_plugin=True).install(claude_home)

    assert _servers(claude_home)["modelquad"]["args"] == ["--mine"]
    skipped = [action for action in report.actions if action.kind is ActionKind.SKIPPED]
    assert any("did not write was left in place" in (action.detail or "") for action in skipped)


def test_a_command_skill_without_modelquads_mark_stays(state_root, claude_home):
    own = claude_home / "skills" / "modelquad-review" / "SKILL.md"
    own.parent.mkdir(parents=True)
    own.write_text("---\nname: modelquad-review\n---\nmy own\n", encoding="utf-8")

    ClaudeAdapter(via_plugin=True).install(claude_home)

    assert own.read_text(encoding="utf-8").endswith("my own\n")


def test_the_cli_remembers_the_choice_and_a_dry_run_does_not(state_root, claude_home):
    assert read_claude_plugin() is False

    assert cli.main(["install", "claude", "--target-dir", str(claude_home), "--plugin",
                     "--dry-run", "--json"]) == 0
    assert read_claude_plugin() is False

    assert cli.main(["install", "claude", "--target-dir", str(claude_home), "--plugin", "--json"]) == 0
    assert read_claude_plugin() is True
    assert ClaudeAdapter().via_plugin is True
    assert "modelquad" not in _servers(claude_home)

    assert cli.main(["install", "claude", "--target-dir", str(claude_home), "--no-plugin", "--json"]) == 0
    assert read_claude_plugin() is False
    assert "modelquad" in _servers(claude_home)


def test_the_plugin_record_is_evidence_and_unknown_stays_unknown(claude_home):
    assert plugin_recorded(claude_home) is None
    _record_plugin(claude_home, "frontend-design@claude-plugins-official")
    assert plugin_recorded(claude_home) is False
    _record_plugin(claude_home, "modelquad@modelquad", value={"scope": "user"})
    assert plugin_recorded(claude_home) is True
    (claude_home / "plugins" / "installed_plugins.json").write_text("{not json", encoding="utf-8")
    assert plugin_recorded(claude_home) is None


def test_doctor_names_a_double_install(state_root, claude_home):
    _regular_install(claude_home)
    assert doctor._claude_duplicates_check(claude_home) is None
    _record_plugin(claude_home)

    check = doctor._claude_duplicates_check(claude_home)

    assert check is not None and check.status is doctor.CheckStatus.WARN
    assert "--plugin" in check.detail


def test_doctor_accepts_the_plugins_registration(state_root, claude_home):
    write_claude_plugin(True)
    ClaudeAdapter().install(claude_home)

    unknown = doctor._mcp_registration_check("claude", claude_home)
    assert unknown.status is doctor.CheckStatus.WARN and "unknown" in unknown.detail
    _record_plugin(claude_home)
    assert doctor._mcp_registration_check("claude", claude_home).status is doctor.CheckStatus.OK
    assert doctor._claude_duplicates_check(claude_home) is None
