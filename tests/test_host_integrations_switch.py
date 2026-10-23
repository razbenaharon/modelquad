# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Temporary state and synthetic backends only; no native launches."""

from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from orchestrator import cli, doctor, host_integrations, onboarding, repair
from orchestrator.adapters import InstallReport
from orchestrator.adapters.report import ActionKind, FileAction
from orchestrator.adapters import registration
from orchestrator.config import load_config
from orchestrator.doctor import CheckStatus, DoctorCheck, DoctorReport
from orchestrator.registry import (
    HostAdapterDescriptor, ProviderCapabilities, ProviderPlugin, ProviderRegistry,
    UsageSourceKind,
)
from orchestrator.service import lifecycle
from orchestrator.statecontext import STATE_ROOT_ENV_VAR


def forbidden(*args, **kwargs):
    pytest.fail("Unexpected adapter write, process launch, registry or scheduler call")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_ROOT_ENV_VAR, str(tmp_path / "state"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(cli, "preseed_windows_uname", lambda: None)
    monkeypatch.setattr(lifecycle.TaskSchedulerRegistrar, "_scheduler", forbidden)
    yield tmp_path / "state" / "host-integrations.json"


@pytest.mark.parametrize("raw,enabled", [
    (None, True),
    ('{"schema":1,"global_host_integrations":"on"}', True),
    ('{"schema":1,"global_host_integrations":"off"}', False),
    ("broken", True), ("[]", True), ("null", True),
    ('{"schema":2,"global_host_integrations":"off"}', True),
    ('{"schema":1,"global_host_integrations":"invalid"}', True),
])
def test_switch_read_defaults(isolated_state, raw, enabled):
    if raw is not None:
        isolated_state.parent.mkdir(parents=True)
        isolated_state.write_text(raw, encoding="utf-8")
    assert host_integrations.global_integrations_enabled() is enabled


def test_unreadable_switch_defaults_on(isolated_state):
    isolated_state.mkdir(parents=True)  # reading a directory raises OSError
    assert host_integrations.global_integrations_enabled() is True


def test_switch_atomic_write_and_roundtrip(isolated_state, monkeypatch):
    real_write = host_integrations.atomic_write
    writes = []

    def write(path, content):
        writes.append(path)
        real_write(path, content)

    monkeypatch.setattr(host_integrations, "atomic_write", write)
    for enabled in (False, True):
        host_integrations.set_global_integrations(enabled)
        data = json.loads(isolated_state.read_text(encoding="utf-8"))
        assert data["schema"] == 1
        assert data["global_host_integrations"] == ("on" if enabled else "off")
        assert datetime.fromisoformat(data["changed_at"]).utcoffset().total_seconds() == 0
        assert host_integrations.global_integrations_enabled() is enabled
    assert writes == [isolated_state, isolated_state]
    assert list(isolated_state.parent.iterdir()) == [isolated_state]


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    calls = []
    descriptors = {}

    class Adapter:
        install = forbidden

        def is_installed(self, target):
            return target.name == "claude"

        def uninstall(self, target, *, dry_run=False, **kwargs):
            assert dry_run or host_integrations.global_integrations_enabled() is False
            calls.append((target, dry_run, kwargs))
            return InstallReport(client=target.name, dry_run=dry_run, actions=())

    for client in ("claude", "codex"):
        descriptors[client] = HostAdapterDescriptor(
            id=client, display_name=client, provider_id="synthetic",
            adapter_cls=Adapter, default_target_dir=lambda name=client: tmp_path / name,
            detect=lambda: False,  # off must clean wiring even without host detection
        )
    registry = ProviderRegistry((ProviderPlugin(
        id="synthetic", display_name="Synthetic", capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.UNKNOWN, host_adapters=tuple(descriptors.values()),
    ),))
    monkeypatch.setattr(cli, "CLIENT_NAMES", tuple(descriptors))
    monkeypatch.setattr(cli, "get_host_descriptor", descriptors.__getitem__)
    monkeypatch.setattr(cli, "default_target_dir", lambda client: descriptors[client].default_target_dir())
    monkeypatch.setattr(cli, "get_adapter", lambda client: Adapter())
    return SimpleNamespace(calls=calls, descriptors=descriptors, registry=registry)


@pytest.mark.parametrize("dry_run", [False, True])
def test_onboarding_skips_without_inspecting_or_writing(hosts, monkeypatch, dry_run):
    host_integrations.set_global_integrations(False)
    monkeypatch.setattr(onboarding, "_find_host_descriptor", forbidden)
    monkeypatch.setattr(registration, "install_with_registration", forbidden)
    outcomes = onboarding.install_detected_hosts(["claude", "codex"], dry_run=dry_run)
    assert [outcome.host for outcome in outcomes] == ["claude", "codex"]
    assert all(outcome.status is registration.RegistrationStatus.REGISTRATION_SKIPPED
               and outcome.error == host_integrations.OFF_DETAIL
               and not outcome.ok and outcome.dry_run is dry_run for outcome in outcomes)


def test_onboarding_step_reports_choice_without_claiming_connection(hosts, monkeypatch, tmp_path):
    host_integrations.set_global_integrations(False)
    from orchestrator import provider_connection
    connection = SimpleNamespace(
        provider_id="synthetic", display_name="Synthetic",
        install=provider_connection.InstallState.INSTALLED_ON_PATH,
        headline="Installed",
    )
    monkeypatch.setattr(provider_connection, "describe_connections", lambda **kwargs: (connection,))
    monkeypatch.setattr(onboarding, "run_doctor", lambda *args, **kwargs: DoctorReport(checks=(
        DoctorCheck("claude adapter installed", CheckStatus.OK, host_integrations.OFF_DETAIL),
        DoctorCheck("claude MCP registered", CheckStatus.WARN, "missing"),
    )))
    messages = []
    result = onboarding.run_onboarding(
        selected_providers=["synthetic"], registry=hosts.registry, assume_yes=True,
        which=lambda _: None, db_path=tmp_path / "history.db", print_fn=messages.append,
    )
    assert result.ok is True
    assert result.connected_providers == ()
    skipped = [step for step in result.steps if step.name.startswith("Install ")]
    assert len(skipped) == 2 and all(step.ok and "off by choice" in step.detail for step in skipped)
    assert not any("adapter verified" in step.name or "MCP registration" in step.name
                   for step in result.steps)


@pytest.mark.parametrize("dry_run", [False, True])
def test_repair_skips_warn_fixture(hosts, monkeypatch, dry_run):
    host_integrations.set_global_integrations(False)
    report = DoctorReport(checks=(
        DoctorCheck("claude adapter installed", CheckStatus.WARN, "missing"),
        DoctorCheck("codex MCP runtime", CheckStatus.WARN, "wrong runtime"),
    ))
    monkeypatch.setattr(repair, "run_doctor", lambda *args, **kwargs: report)
    monkeypatch.setattr(repair, "install_detected_hosts", forbidden)
    messages = []
    result = repair.run_repair(
        registry=hosts.registry, dry_run=dry_run, print_fn=messages.append, which=lambda _: None,
    )
    assert result["repaired_hosts"] == []
    assert result["global_host_integrations"] == "off"
    assert "claude adapter installed" in result["skipped"]
    assert "codex MCP runtime" in result["skipped"]
    assert any("off by choice" in message for message in messages)


def test_doctor_off_by_choice_only_for_global_targets(hosts, monkeypatch, tmp_path):
    host_integrations.set_global_integrations(False)
    monkeypatch.setattr(doctor, "_mcp_registration_check", forbidden)
    monkeypatch.setattr(doctor, "_leftover_gate_check", forbidden)
    monkeypatch.setattr(doctor, "_mcp_runtime_check", lambda *args, **kwargs: None)
    from orchestrator.service import control_plane as shell
    kwargs = dict(
        registry=hosts.registry, which=lambda _: None, usage_directory=tmp_path / "usage",
        control_plane_probe=lambda _: shell.ControlPlaneVerdict(shell.PROBE_ABSENT, "synthetic"),
    )
    report = doctor.run_doctor(load_config(), **kwargs)
    checks = {check.name: check for check in report.checks}
    check = checks["claude adapter installed"]
    assert check.status is CheckStatus.WARN
    assert f"off but still wired: claude {tmp_path / 'claude'}" in check.detail
    check = checks["codex adapter installed"]
    assert check.status is CheckStatus.OK and check.detail == host_integrations.OFF_DETAIL
    # Explicit isolated targets still receive real adapter diagnostics.
    monkeypatch.setattr(doctor, "_mcp_registration_check", lambda *args: DoctorCheck(
        "codex MCP registered", CheckStatus.WARN, "synthetic missing"))
    report = doctor.run_doctor(load_config(), codex_dir=tmp_path / "explicit", **kwargs)
    check = next(check for check in report.checks if check.name == "codex adapter installed")
    assert check.status is CheckStatus.WARN


@pytest.mark.parametrize("arguments", [["claude"], ["--all"], ["--global"]])
def test_install_default_refuses_when_off(hosts, monkeypatch, capsys, arguments):
    host_integrations.set_global_integrations(False)
    monkeypatch.setattr(registration, "install_with_registration", forbidden)
    assert cli.main(["install", *arguments, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    errors = payload["errors"] if "errors" in payload else [payload]
    assert errors and all("modelquad integrations on" in error["error"] for error in errors)


def test_install_explicit_targets_allowed(hosts, monkeypatch, tmp_path):
    host_integrations.set_global_integrations(False)
    calls = []

    def install(client, target, adapter, *, dry_run):
        calls.append((client, target))
        return InstallReport(client=client, dry_run=dry_run, actions=())

    monkeypatch.setattr(registration, "install_with_registration", install)
    assert cli.main(["install", "claude", "--target-dir", str(tmp_path / "test-claude")]) == 0
    assert calls == [("claude", tmp_path / "test-claude")]


def test_all_explicit_targets_install_while_other_global_targets_refuse(hosts, monkeypatch, tmp_path, capsys):
    host_integrations.set_global_integrations(False)
    calls = []

    def install(client, target, adapter, *, dry_run):
        calls.append((client, target))
        return InstallReport(client=client, dry_run=dry_run, actions=())

    monkeypatch.setattr(registration, "install_with_registration", install)
    assert cli.main(["install", "--all", "--claude-dir", str(tmp_path / "test-claude"), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert calls == [("claude", tmp_path / "test-claude")]
    assert result["errors"] == [{"client": "codex", "error": host_integrations.OFF_GUIDANCE}]


def test_cli_status_on_off(hosts, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_restore_displaced_registration", forbidden)
    assert cli.main(["integrations", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["global_host_integrations"] == "on"
    assert status["hosts"] == {"claude": {"is_installed": True}, "codex": {"is_installed": False}}
    assert cli.main(["integrations", "off", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["global_host_integrations"] == "off"
    assert len(hosts.calls) == 2 and all(not call[1] and not call[2] for call in hosts.calls)
    assert cli.main(["integrations", "on", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["global_host_integrations"] == "on" and "modelquad onboard" in result["next_step"]
    assert len(hosts.calls) == 2  # on does not install


def test_cli_dry_run_never_saves_or_writes(hosts, monkeypatch, isolated_state, capsys):
    host_integrations.set_global_integrations(False)
    original = isolated_state.read_bytes()
    monkeypatch.setattr(host_integrations, "set_global_integrations", forbidden)
    for action in ("status", "on", "off"):
        assert cli.main(["integrations", action, "--dry-run", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["dry_run"] is True
        assert isolated_state.read_bytes() == original
    assert len(hosts.calls) == 2 and all(call[1] for call in hosts.calls)


def test_cli_off_dry_run_reports_current_choice(hosts, isolated_state, capsys):
    assert cli.main(["integrations", "off", "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["global_host_integrations"] == "on" and result["would_set"] == "off"
    assert not isolated_state.exists()
    assert len(hosts.calls) == 2 and all(call[1] for call in hosts.calls)


def test_uninstall_still_works_off_without_restoring_global_registration(hosts, monkeypatch):
    host_integrations.set_global_integrations(False)
    monkeypatch.setattr(registration, "restore_previous_registration", forbidden)
    assert cli.main(["uninstall", "claude"]) == 0
    assert len(hosts.calls) == 1


def test_cli_off_failure_keeps_switch_off(hosts, monkeypatch, capsys):
    def failing_uninstall(*args, **kwargs):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(hosts.descriptors["claude"].adapter_cls, "uninstall", failing_uninstall)
    assert cli.main(["integrations", "off", "--json"]) == 1
    assert host_integrations.global_integrations_enabled() is False
    assert "synthetic cleanup failure" in capsys.readouterr().out


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_off_reports_skipped_removal(hosts, monkeypatch, capsys, tmp_path, as_json, dry_run):
    target = tmp_path / "codex" / "config.toml"
    original_uninstall = hosts.descriptors["codex"].adapter_cls.uninstall

    def uninstall(self, directory, *, dry_run=False, **kwargs):
        if directory.name != "codex":
            return original_uninstall(self, directory, dry_run=dry_run, **kwargs)
        return InstallReport("codex", dry_run, (
            FileAction(target, ActionKind.SKIPPED, detail="Malformed managed markers; file left unchanged."),
        ))

    monkeypatch.setattr(hosts.descriptors["codex"].adapter_cls, "uninstall", uninstall)
    arguments = ["integrations", "off", *(["--json"] if as_json else []),
                 *(["--dry-run"] if dry_run else [])]
    assert cli.main(arguments) == 1
    assert host_integrations.global_integrations_enabled() is dry_run
    output = capsys.readouterr().out
    if as_json:
        payload = json.loads(output)
        assert payload["global_host_integrations"] == ("on" if dry_run else "off")
        assert len(payload["incomplete"]) == 1
        item = payload["incomplete"][0]
        assert item["client"] == "codex" and item["path"] == str(target)
        assert "Malformed managed markers" in item["detail"]
        assert "modelquad integrations off" in item["next_step"]
    else:
        assert f"Removal incomplete: codex {target}" in output
        assert "Malformed managed markers" in output
        assert "Inspect this file" in output and "modelquad integrations off" in output
