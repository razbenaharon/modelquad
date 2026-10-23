# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Active Host installs preserve later user decisions."""

import json
from dataclasses import replace

import pytest

from orchestrator import cli, hostruntime
from orchestrator.adapters import registration as reg
from orchestrator.adapters.claude_adapter import ClaudeAdapter, _timeout_env_key, _timeout_ownership_key
from orchestrator.onboarding import install_detected_hosts

from test_host_registration_transaction import state_root  # noqa: F401 -- fixture


@pytest.fixture
def targets(tmp_path, monkeypatch, state_root):
    homes = {host: tmp_path / host for host in ("claude",)}
    for home in homes.values():
        home.mkdir()
    original_descriptor = cli.get_host_descriptor
    monkeypatch.setattr(
        cli, "get_host_descriptor",
        lambda host: replace(
            original_descriptor(host), default_target_dir=lambda: homes[host], detect=lambda: host in homes
        ),
    )
    monkeypatch.setattr(cli, "default_target_dir", lambda host: homes[host])
    monkeypatch.setattr(
        "orchestrator.onboarding._find_host_descriptor",
        lambda host, registry=None: cli.get_host_descriptor(host),
    )
    return homes


def seed_earlier_timeout(settings, env=None):
    """What an earlier build left: a global MCP_TOOL_TIMEOUT it recorded as its own."""
    payload = json.loads(settings.read_text()) if settings.exists() else {}
    payload.setdefault("env", dict(env or {}))["MCP_TOOL_TIMEOUT"] = "900000"
    settings.write_text(json.dumps(payload))
    records = reg.read_owned_settings()
    records[_timeout_ownership_key(settings)] = "900000"
    if not env:
        records[_timeout_env_key(settings)] = True
    reg.write_owned_settings(records)


def write_entry(host, home, entry):
    path = hostruntime.json_mcp_config_path(host, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(path.read_text()) if path.exists() else {}
    payload.setdefault("mcpServers", {})["modelquad"] = entry
    payload["mcpServers"]["other"] = {"command": "keep-other"}
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("host", ["claude"])
@pytest.mark.parametrize("global_install", [False, True])
def test_cli_install_then_uninstall_preserves_later_user_edit(
    targets, host, global_install, capsys
):
    argv = ["install", "--global"] if global_install else ["install", host]
    assert cli.main(argv) == 0
    assert reg.recorded_replacement(host)["has_previous"] is False
    users_entry = {"command": "C:/mine/modelquad.cmd", "args": ["--mine"]}
    write_entry(host, targets[host], users_entry)
    argv = ["uninstall", "--global"] if global_install else ["uninstall", host]
    assert cli.main(argv) == 0
    assert hostruntime.read_registered_command(host, targets[host]) == users_entry["command"]
    if host != "codex":
        assert hostruntime.read_registered_entry(host, targets[host]) == users_entry
        path = hostruntime.json_mcp_config_path(host, targets[host])
        assert json.loads(path.read_text())["mcpServers"]["other"]["command"] == "keep-other"
    assert reg.recorded_replacement(host) is None


@pytest.mark.parametrize("value", ["123456", "900000", None])
def test_timeout_present_before_install_is_kept(targets, value):
    home = targets["claude"]
    settings = home / "settings.json"
    settings.write_text(json.dumps({"env": {"MCP_TOOL_TIMEOUT": value, "OTHER": "keep"}}))
    ClaudeAdapter().install(home)
    assert _timeout_ownership_key(settings) not in reg.read_owned_settings()
    ClaudeAdapter().uninstall(home)
    assert json.loads(settings.read_text())["env"] == {"MCP_TOOL_TIMEOUT": value, "OTHER": "keep"}


def test_timeout_user_edit_after_an_earlier_install_is_kept(targets):
    home = targets["claude"]
    settings = home / "settings.json"
    seed_earlier_timeout(settings)
    key = _timeout_ownership_key(settings)
    assert reg.read_owned_settings()[key] == "900000"
    payload = json.loads(settings.read_text())
    payload["env"]["MCP_TOOL_TIMEOUT"] = "123456"
    settings.write_text(json.dumps(payload))
    ClaudeAdapter().install(home)
    ClaudeAdapter().uninstall(home)
    assert json.loads(settings.read_text())["env"]["MCP_TOOL_TIMEOUT"] == "123456"
    assert _timeout_ownership_key(settings) not in reg.read_owned_settings()


@pytest.mark.parametrize("global_install", [False, True])
def test_cli_refuses_install_if_backup_cannot_be_written(targets, monkeypatch, global_install):
    home = targets["claude"]
    original = {"command": "C:/original.cmd"}
    write_entry("claude", home, original)

    def fail(*args, **kwargs):
        raise reg.BackupPersistenceError("cannot record")

    monkeypatch.setattr(reg, "_write_backups", fail)
    argv = ["install", "--global"] if global_install else ["install", "claude"]
    assert cli.main(argv) == 1
    assert hostruntime.read_registered_entry("claude", home) == original
    assert not (home / "CLAUDE.md").exists()


def test_cli_fresh_install_lineage_failure_never_blind_deletes_user_edit(targets, monkeypatch):
    def fail(*args, **kwargs):
        raise reg.BackupPersistenceError("cannot record lineage")

    monkeypatch.setattr(reg, "note_installed_registration", fail)
    assert cli.main(["install", "claude"]) == 0
    assert reg.recorded_replacement("claude") is not None
    users_entry = {"command": "C:/mine.cmd"}
    write_entry("claude", targets["claude"], users_entry)
    assert cli.main(["uninstall", "claude"]) == 0
    assert hostruntime.read_registered_entry("claude", targets["claude"]) == users_entry


def test_timeout_dry_runs_leave_setting_and_record_unchanged(targets):
    home = targets["claude"]
    settings = home / "settings.json"
    ClaudeAdapter().install(home, dry_run=True)
    assert not settings.exists()
    seed_earlier_timeout(settings)
    record = reg.backup_path()
    before = settings.read_bytes()
    evidence = record.read_bytes()
    ClaudeAdapter().install(home, dry_run=True)
    ClaudeAdapter().uninstall(home, dry_run=True)
    assert settings.read_bytes() == before
    assert record.read_bytes() == evidence
    ClaudeAdapter().install(home)
    # ModelQuad created ``env`` for the timeout alone, so none is left behind.
    assert "env" not in json.loads(settings.read_text())
    assert _timeout_ownership_key(settings) not in reg.read_owned_settings()


def test_timeout_without_ownership_evidence_is_kept(targets):
    home = targets["claude"]
    settings = home / "settings.json"
    seed_earlier_timeout(settings)
    reg.write_owned_settings({})
    ClaudeAdapter().install(home)
    ClaudeAdapter().uninstall(home)
    assert json.loads(settings.read_text())["env"]["MCP_TOOL_TIMEOUT"] == "900000"


def test_cli_install_partial_failure_keeps_backup_and_restores_original(targets, monkeypatch):
    home = targets["claude"]
    original = {"command": "C:/original.cmd", "args": ["--original"]}
    write_entry("claude", home, original)

    def fail(*args, **kwargs):
        raise OSError("late write failure")

    # Reporting runs after every file was written: a failure there is "late".
    with monkeypatch.context() as patch:
        patch.setattr("orchestrator.adapters.claude_adapter.pending_action", fail)
        assert cli.main(["install", "claude"]) == 1
    assert reg.recorded_replacement("claude")["installed_lineage"]
    assert cli.main(["uninstall", "claude"]) == 0
    assert hostruntime.read_registered_entry("claude", home) == original


def test_cli_install_sets_the_timeout_on_the_server_entry_only(targets):
    assert cli.main(["install", "claude"]) == 0
    settings = targets["claude"] / "settings.json"
    assert "env" not in json.loads(settings.read_text())
    assert _timeout_ownership_key(settings) not in reg.read_owned_settings()
    entry = hostruntime.read_registered_entry("claude", targets["claude"])
    assert entry["timeout"] == 900000
    assert cli.main(["uninstall", "claude"]) == 0
    assert "env" not in json.loads(settings.read_text())
    assert reg.recorded_replacement("claude") is None


def test_cli_fresh_install_connects_when_evidence_fails(targets, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise reg.BackupPersistenceError("cannot record")

    monkeypatch.setattr(reg, "_write_backups", fail)
    assert cli.main(["install", "claude"]) == 0
    assert hostruntime.read_registered_entry("claude", targets["claude"]) is not None
    assert not (targets["claude"] / "CLAUDE.md").exists()
    assert reg.recorded_replacement("claude") is None
    assert "registration ownership could not be recorded" in capsys.readouterr().out


@pytest.mark.parametrize("prior", [False, True])
def test_timeout_record_failure_does_not_skip_registration_lineage(targets, monkeypatch, prior, capsys):
    home = targets["claude"]
    original = {"command": "C:/original.cmd"}
    if prior:
        write_entry("claude", home, original)

    def fail(*args, **kwargs):
        raise reg.BackupPersistenceError("timeout record locked")

    with monkeypatch.context() as patch:
        patch.setattr(reg, "write_owned_settings", fail)
        cli.main(["install", "claude"])
    assert reg.recorded_replacement("claude")["installed_lineage"]
    assert "MCP_TOOL_TIMEOUT" not in json.loads((home / "settings.json").read_text()).get("env", {})
    assert hostruntime.read_registered_entry("claude", home)["timeout"] == 900000
    assert cli.main(["uninstall", "claude"]) == 0
    assert hostruntime.read_registered_entry("claude", home) == (original if prior else None)


@pytest.mark.parametrize("global_uninstall", [False, True])
@pytest.mark.parametrize("user_edit", [False, True])
def test_timeout_retirement_failure_still_restores_registration(
    targets, monkeypatch, global_uninstall, user_edit
):
    home = targets["claude"]
    original = {"command": "C:/original.cmd"}
    write_entry("claude", home, original)
    assert cli.main(["install", "claude"]) == 0
    later = {"command": "C:/later.cmd", "args": ["--mine"]}
    if user_edit:
        write_entry("claude", home, later)

    def fail(*args, **kwargs):
        raise reg.BackupPersistenceError("timeout record locked")

    monkeypatch.setattr(reg, "write_owned_settings", fail)
    argv = ["uninstall", "--global"] if global_uninstall else ["uninstall", "claude"]
    cli.main(argv)
    assert hostruntime.read_registered_entry("claude", home) == (later if user_edit else original)
    assert reg.recorded_replacement("claude") is None


def test_removed_timeout_reports_removal_when_ownership_retirement_fails(targets, monkeypatch):
    from orchestrator.adapters.report import ActionKind

    home = targets["claude"]
    settings = home / "settings.json"
    settings.write_text('{"env": {"MINE": "keep"}}')
    ClaudeAdapter().install(home)
    seed_earlier_timeout(settings, {"MINE": "keep"})
    before = settings.read_bytes()
    records = reg.read_owned_settings()

    def fail(*args, **kwargs):
        raise reg.BackupPersistenceError("timeout record locked")

    monkeypatch.setattr(reg, "write_owned_settings", fail)
    action = next(a for a in ClaudeAdapter().uninstall(home).actions
                  if a.detail and "MCP_TOOL_TIMEOUT" in a.detail)

    assert action.kind is ActionKind.REMOVED
    assert action.path == settings
    assert action.backup_path.read_bytes() == before
    assert "Could not retire timeout ownership; its record was kept." in action.detail
    assert json.loads(settings.read_text())["env"] == {"MINE": "keep"}
    assert reg.read_owned_settings() == records


@pytest.mark.parametrize("host", ["claude"])
@pytest.mark.parametrize("failure", ["raise", "skip"])
def test_fresh_install_without_registration_retires_placeholder(targets, monkeypatch, host, failure):
    home = targets[host]
    if failure == "raise":
        def fail(*args, **kwargs):
            raise OSError("before registration")

        if host == "claude":
            monkeypatch.setattr("orchestrator.adapters.claude_adapter._plan_owned_copy", fail)
        else:
            monkeypatch.setattr(type(cli.get_adapter(host)), "install", fail)
    elif host == "claude":
        hostruntime.json_mcp_config_path(host, home).write_text("not JSON")
    else:
        (home / "config.toml").write_text("not TOML")
    cli.main(["install", host])
    assert hostruntime.read_registered_command(host, home) is None
    assert reg.recorded_replacement(host) is None
    assert reg.restore_previous_registration(host, home).status is reg.RestoreStatus.NOTHING_RECORDED
    assert cli.main(["uninstall", host]) == 0


def test_a_failed_timeout_removal_keeps_the_setting_and_its_ownership(targets, monkeypatch):
    from orchestrator.adapters import staging
    from orchestrator.adapters.report import ActionKind

    home = targets["claude"]
    settings = home / "settings.json"
    seed_earlier_timeout(settings, {"MINE": "keep"})
    before = settings.read_bytes()

    def fail(path, *args, **kwargs):
        if path == settings:
            raise OSError("setting write failed")
        return real_write(path, *args, **kwargs)

    real_write = staging.atomic_write
    with monkeypatch.context() as patch:
        patch.setattr(staging, "atomic_write", fail)
        action = next(a for a in ClaudeAdapter().install(home).actions
                      if a.path == settings and a.kind is ActionKind.SKIPPED)
    assert settings.read_bytes() == before
    assert _timeout_ownership_key(settings) in reg.read_owned_settings()
    ClaudeAdapter().uninstall(home)
    assert json.loads(settings.read_text())["env"] == {"MINE": "keep"}
    assert _timeout_ownership_key(settings) not in reg.read_owned_settings()
    assert action.detail


def test_host_without_reader_gets_no_placeholder_or_preservation_keyword(targets, monkeypatch):
    from orchestrator.adapters.report import InstallReport

    class Adapter:
        def install(self, target_dir, *, dry_run=False):
            return InstallReport("third-party", dry_run, ())

        def uninstall(self, target_dir, *, dry_run=False):
            return InstallReport("third-party", dry_run, ())

    home = targets["claude"]
    reg.install_with_registration("third-party", home, Adapter())
    assert reg.recorded_replacement("third-party") is None


@pytest.mark.parametrize("prior", [False, True])
def test_raised_backup_error_after_mcp_write_runs_failed_install_recovery(targets, monkeypatch, prior):
    home = targets["claude"]
    original = {"command": "C:/original.cmd"}
    if prior:
        write_entry("claude", home, original)

    def fail(*args, **kwargs):
        raise reg.BackupPersistenceError("late backup failure")

    with monkeypatch.context() as patch:
        patch.setattr("orchestrator.adapters.claude_adapter.pending_action", fail)
        assert cli.main(["install", "claude"]) == 1
    assert reg.recorded_replacement("claude")["installed_lineage"]
    assert cli.main(["uninstall", "claude"]) == 0
    assert hostruntime.read_registered_entry("claude", home) == (original if prior else None)
