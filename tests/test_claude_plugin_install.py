# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad install claude --plugin``: the plugin's copies are the only ones.

The ModelQuad plugin carries the server, the commands and the hooks. With
the regular install beside it Claude Code would run each twice, so install
for the plugin takes back ModelQuad's own copies, and the status line and
SessionStart reminder earlier builds wrote -- and only what is provably
ModelQuad's, with a backup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad import __version__, cli
from modelquad.adapters.claude_adapter import (
    ClaudeAdapter, _STATUS_LINE_ARGUMENTS, _status_line_command, plugin_recorded,
)
from modelquad.adapters.cliintegration import upsert_owned_status_line
from modelquad.adapters.codex_hooks import manage_pending_hook
from modelquad.adapters.registration import install_with_registration, read_owned_settings
from modelquad.adapters.report import ActionKind
from modelquad.adapters.server_instructions import read_claude_plugin, write_claude_plugin

from test_host_registration_transaction import claude_home, state_root  # noqa: F401 -- fixtures

_SKILLS = ("modelquad", "modelquad-review", "modelquad-setup", "modelquad-settings", "modelquad-plan-review",
           "modelquad-challenge-decision", "modelquad-check-requirements")
#: Command skills earlier builds installed for features this build no longer has.
_RETIRED_SKILLS = ("modelquad-handoff", "modelquad-quota", "modelquad-split")


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
    entry = value if value is not None else [{"scope": "user", "version": __version__}]
    (folder / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {key: entry}}), encoding="utf-8"
    )


def _regular_install(home: Path) -> None:
    install_with_registration("claude", home, ClaudeAdapter(via_plugin=False))
    assert "modelquad" in _servers(home)
    assert all((home / "skills" / name / "SKILL.md").is_file() for name in _SKILLS)


def _seed_what_an_earlier_install_wrote(home: Path) -> None:
    """The status line and the SessionStart reminder, each with its ownership evidence,
    and the retired command skills, each with ModelQuad's mark."""
    for name in _RETIRED_SKILLS:
        skill = home / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"---\nname: {name}\nmetadata:\n  managed-by: modelquad\n---\nearlier\n",
                         encoding="utf-8")
    settings = home / "settings.json"
    assert upsert_owned_status_line(
        settings, _STATUS_LINE_ARGUMENTS, _status_line_command(), dry_run=False, status_type="command",
    ).kind is ActionKind.CREATED
    assert manage_pending_hook(settings, "claude").kind is ActionKind.UPDATED
    seeded = _settings(home)
    assert "usage-hook claude" in seeded["statusLine"]["command"]
    assert "hook-pending" in json.dumps(seeded["hooks"])


def test_installing_for_the_plugin_takes_back_the_regular_copies(state_root, claude_home):
    _regular_install(claude_home)
    _seed_what_an_earlier_install_wrote(claude_home)

    report = install_with_registration("claude", claude_home, ClaudeAdapter(via_plugin=True))

    assert "modelquad" not in _servers(claude_home)
    # Each folder keeps only the backup of the removed copy.
    assert not any((claude_home / "skills" / name / "SKILL.md").exists()
                   for name in (*_SKILLS, *_RETIRED_SKILLS))
    # The plugin has no status line and runs its own hooks: ModelQuad's earlier ones go.
    assert _settings(claude_home) == {}
    removed = [action for action in report.actions if action.kind is ActionKind.REMOVED]
    assert removed and all(action.backup_path is not None for action in removed)
    assert any("plugin supplies the server" in (action.detail or "") for action in report.actions)
    assert any("this version has none" in (action.detail or "") for action in report.actions)
    assert not any("pending_hook" in key for key in read_owned_settings())


def test_installing_for_the_plugin_keeps_a_status_line_and_a_hook_that_are_the_users(
    state_root, claude_home
):
    settings = claude_home / "settings.json"
    users = {
        "statusLine": {"type": "command", "command": "my-line; modelquad usage-hook claude --segment"},
        "hooks": {"SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "my-tool hook-pending claude"}]}]},
    }
    settings.write_text(json.dumps(users), encoding="utf-8")
    untouched = settings.read_bytes()

    report = install_with_registration("claude", claude_home, ClaudeAdapter(via_plugin=True))

    assert settings.read_bytes() == untouched
    assert all(a.kind is ActionKind.UNCHANGED for a in report.actions if a.path == settings)


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


def test_beside_the_plugin_only_claude_codes_own_record_counts_as_installed(state_root, claude_home):
    write_claude_plugin(True)
    ClaudeAdapter().install(claude_home)

    # No plugin record: unknown, and unknown is not "installed".
    assert plugin_recorded(claude_home) is None
    assert ClaudeAdapter().is_installed(claude_home) is False
    _record_plugin(claude_home, "frontend-design@claude-plugins-official")
    assert ClaudeAdapter().is_installed(claude_home) is False
    _record_plugin(claude_home)
    assert ClaudeAdapter().is_installed(claude_home) is True
