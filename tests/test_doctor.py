# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os

import pytest

from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.cli import main, render_doctor
from modelquad.config import Config, ProviderConfig, load_config
from modelquad.catalog import AvailabilityState, ModelDescriptor
from modelquad.doctor import (
    CheckStatus,
    DoctorCheck,
    DoctorReport,
    _codex_shell_check,
    _model_discovery_check,
    run_doctor,
)
from modelquad.registry import (
    HostAdapterDescriptor,
    ProviderCapabilities,
    ProviderPlugin,
    ProviderPluginContext,
    ProviderRegistry,
    UsageSourceKind,
)


def _checks_by_name(report):
    return {check.name: check for check in report.checks}


def test_doctor_reports_controlled_cli_usage_and_adapter_state(tmp_path):
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    for filename in ("usage-claude.json", "usage-codex.json"):
        (usage_dir / filename).write_text("{}", encoding="utf-8")
    claude_dir = tmp_path / "claude"
    ClaudeAdapter().install(claude_dir)

    def which(binary):
        return {"codex": "/fake/codex", "claude": "/fake/claude"}.get(binary)

    report = run_doctor(
        load_config(),
        usage_directory=usage_dir,
        claude_dir=claude_dir,
        which=which,
        control_plane_probe=_no_listener,
    )
    checks = _checks_by_name(report)

    assert checks["ModelQuad package"].status is CheckStatus.OK
    assert checks["codex CLI"].status is CheckStatus.OK
    assert checks["claude CLI"].status is CheckStatus.OK
    assert checks["usage sources"].status is CheckStatus.OK
    assert checks["claude adapter installed"].status is CheckStatus.OK
    assert checks["claude MCP registered"].status is CheckStatus.OK
    # Claude Code is the only host (decision H1).
    assert not [name for name in checks if name.startswith(("codex adapter", "codex MCP", "claude-desktop"))]
    assert report.ok is True


def _no_listener(_port):
    """A control plane with nothing on the port.

    Injected wherever a test asserts something about overall doctor health.
    The control-plane check is the one diagnostic whose honest answer depends
    on what is running on the developer's machine right now, so leaving it
    live would make "do warnings flip overall health?" depend on whether a
    ModelQuad happens to be up.
    """

    from modelquad.service import control_plane

    return control_plane.ControlPlaneVerdict(control_plane.PROBE_ABSENT, "no listener")


def test_doctor_warnings_do_not_fail_an_otherwise_available_configuration(tmp_path):
    report = run_doctor(
        load_config(),
        usage_directory=tmp_path / "missing-usage",
        claude_dir=tmp_path / "missing-claude",
        codex_dir=tmp_path / "missing-codex",
        which=lambda _: "/fake/cli",
        control_plane_probe=_no_listener,
    )

    checks = _checks_by_name(report)
    assert checks["usage sources"].status is CheckStatus.WARN
    assert checks["claude adapter installed"].status is CheckStatus.WARN
    assert checks["claude MCP registered"].status is CheckStatus.WARN
    assert report.ok is True


def test_doctor_json_output_has_serializable_checks(tmp_path, capsys):
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()

    exit_code = main(["--usage-directory", str(usage_dir), "doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code in {0, 1}
    assert isinstance(payload["checks"], list)
    assert {"name", "status", "detail"} <= set(payload["checks"][0])


def test_disabled_provider_remains_visible_in_doctor_report(tmp_path):
    report = run_doctor(
        Config(providers={"codex": ProviderConfig(enabled=False)}),
        usage_directory=tmp_path / "usage",
        claude_dir=tmp_path / "claude",
        codex_dir=tmp_path / "codex",
        which=lambda _: "/fake/cli",
    )
    checks = _checks_by_name(report)
    assert checks["codex provider"].status is CheckStatus.WARN
    assert checks["codex provider"].detail == "Disabled in configuration."
    assert "codex CLI" in checks


def test_registry_credential_check_reports_only_configuration_state(tmp_path, monkeypatch):
    plugin = ProviderPlugin(
        id="synthetic",
        display_name="Synthetic",
        capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.API,
        credential_env_vars=("SYNTHETIC_TEST_ENV",),
    )
    registry = ProviderRegistry((plugin,))
    monkeypatch.delenv("SYNTHETIC_TEST_ENV", raising=False)
    missing = run_doctor(Config(), usage_directory=tmp_path, registry=registry)
    missing_check = _checks_by_name(missing)["synthetic configured"]
    assert missing_check.status is CheckStatus.WARN
    assert "SYNTHETIC_TEST_ENV" in missing_check.detail

    monkeypatch.setenv("SYNTHETIC_TEST_ENV", "must-never-leak")
    configured = run_doctor(Config(), usage_directory=tmp_path, registry=registry)
    configured_check = _checks_by_name(configured)["synthetic configured"]
    assert configured_check.status is CheckStatus.OK
    assert "must-never-leak" not in configured_check.detail


def test_model_discovery_check_reports_ok_with_descriptor_count():
    plugin = ProviderPlugin(
        id="synthetic",
        display_name="Synthetic",
        capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.API,
        model_discovery=lambda ctx: (
            ModelDescriptor(
                provider="synthetic",
                model_id="a",
                display_name="A",
                availability=AvailabilityState.KNOWN,
            ),
        ),
    )
    context = ProviderPluginContext(config=Config(), provider_config=None)
    check = _model_discovery_check(plugin, context)
    assert check is not None
    assert check.status is CheckStatus.OK
    assert "known" in check.detail
    assert "count: 1" in check.detail


def test_model_discovery_check_is_none_when_plugin_has_no_discovery():
    plugin = ProviderPlugin(
        id="synthetic",
        display_name="Synthetic",
        capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.API,
    )
    context = ProviderPluginContext(config=Config(), provider_config=None)
    assert _model_discovery_check(plugin, context) is None


def test_model_discovery_check_warns_honestly_on_empty_result():
    plugin = ProviderPlugin(
        id="synthetic",
        display_name="Synthetic",
        capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.API,
        model_discovery=lambda ctx: (),
    )
    context = ProviderPluginContext(config=Config(), provider_config=None)
    check = _model_discovery_check(plugin, context)
    assert check.status is CheckStatus.WARN


def test_model_discovery_failure_warns_but_never_raises(tmp_path):
    def raising_discovery(ctx):
        raise RuntimeError("simulated discovery failure")

    plugin = ProviderPlugin(
        id="synthetic",
        display_name="Synthetic",
        capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.API,
        model_discovery=raising_discovery,
    )
    context = ProviderPluginContext(config=Config(), provider_config=None)
    check = _model_discovery_check(plugin, context)
    assert check.status is CheckStatus.WARN
    assert "simulated discovery failure" in check.detail


def test_model_discovery_failure_does_not_flip_overall_doctor_health(tmp_path):
    """Point 21: discovery failure/absence is independent of provider execution health."""

    def raising_discovery(ctx):
        raise RuntimeError("simulated discovery failure")

    plugin = ProviderPlugin(
        id="synthetic",
        display_name="Synthetic",
        capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.API,
        model_discovery=raising_discovery,
    )
    registry = ProviderRegistry((plugin,))
    report = run_doctor(
        Config(),
        usage_directory=tmp_path,
        registry=registry,
        control_plane_probe=_no_listener,
    )
    checks = _checks_by_name(report)

    assert checks["synthetic model discovery"].status is CheckStatus.WARN
    assert report.ok is True


def test_doctor_reports_model_discovery_check_for_every_wired_default_plugin(tmp_path):
    """Only available integrations emit model-discovery checks."""

    for filename in ("usage-claude.json", "usage-codex.json"):
        (tmp_path / filename).write_text("{}", encoding="utf-8")
    report = run_doctor(load_config(), usage_directory=tmp_path)
    checks = _checks_by_name(report)

    for provider in ("claude", "codex"):
        check = checks[f"{provider} model discovery"]
        assert check.status in (CheckStatus.OK, CheckStatus.WARN)

    # Exactly the wired defaults emit a model-discovery check -- no more, no
    # fewer. This replaces a trailing `assert report.ok is True`, which
    # contradicted this test's own docstring: report.ok folds in whether the
    # provider CLIs are installed on the host, so it asserted a property of the
    # machine rather than of the registry wiring. On a host with no providers
    # installed -- a clean machine, and CI -- doctor correctly reports not-ok,
    # and this wiring test has no business failing because of it.
    assert {name for name in checks if name.endswith("model discovery")} == {
        "claude model discovery",
        "codex model discovery",
    }


def _verified_host_registry(claude_dir):
    return ProviderRegistry(
        (
            ProviderPlugin(
                id="claude",
                display_name="Claude",
                capabilities=ProviderCapabilities(),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                host_adapters=(
                    HostAdapterDescriptor("claude", "Claude Code", "claude", ClaudeAdapter, lambda: claude_dir),
                ),
            ),
        )
    )


def test_doctor_mcp_registration_checks_cover_present_absent_and_malformed_config(tmp_path):
    claude_dir = tmp_path / "claude"
    registry = _verified_host_registry(claude_dir)
    ClaudeAdapter().install(claude_dir)

    present = _checks_by_name(
        run_doctor(Config(), usage_directory=tmp_path / "usage", registry=registry, which=lambda _: "/fake")
    )
    assert present["claude MCP registered"].status is CheckStatus.OK

    ClaudeAdapter().uninstall(claude_dir)
    absent = _checks_by_name(
        run_doctor(Config(), usage_directory=tmp_path / "usage", registry=registry, which=lambda _: "/fake")
    )
    assert absent["claude MCP registered"].status is CheckStatus.WARN
    assert "modelquad install claude" in absent["claude MCP registered"].detail

    (claude_dir.parent / "claude.json").write_text("{not json", encoding="utf-8")
    malformed = _checks_by_name(
        run_doctor(Config(), usage_directory=tmp_path / "usage", registry=registry, which=lambda _: "/fake")
    )
    assert malformed["claude MCP registered"].status is CheckStatus.WARN


def test_codex_shell_check_resolves_after_filtering_windows_apps_alias():
    path_with_only_alias = r"C:\Users\example\AppData\Local\Microsoft\WindowsApps"
    blocked = _codex_shell_check(path_with_only_alias)
    assert blocked.status is CheckStatus.WARN
    assert "no non-alias" in blocked.detail.lower()

    path_with_real_shell = (
        r"C:\Users\example\AppData\Local\Microsoft\WindowsApps"
        + os.pathsep
        + r"C:\Windows\System32\WindowsPowerShell\v1.0"
    )
    ok = _codex_shell_check(path_with_real_shell)
    assert ok.status is CheckStatus.OK

    unreadable = _codex_shell_check(None)
    assert unreadable.status is CheckStatus.WARN
    assert "could not read path" in unreadable.detail.lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only doctor check")
def test_doctor_reports_codex_shell_execution_capability_on_windows(tmp_path):
    which = lambda binary: "/fake/codex" if binary == "codex" else None

    blocked = _checks_by_name(
        run_doctor(
            load_config(),
            usage_directory=tmp_path / "usage",
            which=which,
            windows_path=r"C:\Users\example\AppData\Local\Microsoft\WindowsApps",
        )
    )
    assert blocked["codex shell execution"].status is CheckStatus.WARN

    ok = _checks_by_name(
        run_doctor(
            load_config(),
            usage_directory=tmp_path / "usage",
            which=which,
            windows_path=r"C:\Windows\System32",
        )
    )
    assert ok["codex shell execution"].status is CheckStatus.OK


def test_grouped_doctor_rendering_includes_every_check_and_uses_report_verdict(capsys):
    report = DoctorReport(
        (
            DoctorCheck("ModelQuad package", CheckStatus.OK, "0.2.0"),
            DoctorCheck("usage sources", CheckStatus.WARN, "Missing snapshots."),
            DoctorCheck("claude adapter installed", CheckStatus.OK, "Installed."),
            DoctorCheck("claude MCP registered", CheckStatus.WARN, "Not registered."),
            DoctorCheck("codex CLI", CheckStatus.MISSING, "Not on PATH."),
            DoctorCheck("future check", CheckStatus.WARN, "Still visible."),
        )
    )

    render_doctor(report)

    output = capsys.readouterr().out
    for check in report.checks:
        assert check.name in output
    assert "Core:" in output
    assert "Claude Code:" in output
    assert "Codex:" in output
    assert "Other:" in output
    assert "Overall: DEGRADED" in output


def test_doctor_flags_a_gate_hook_an_earlier_version_left_for_repair(tmp_path):
    """The gate is no longer installed; a leftover owned one is a repairable warning."""
    from modelquad.repair import _map_check_to_host

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    kwargs = dict(usage_directory=tmp_path / "usage", claude_dir=claude_dir, which=lambda _: "/fake/cli")

    assert "claude hook-gate runtime" not in _checks_by_name(run_doctor(load_config(), **kwargs))
    assert "claude automatic-routing hook (structural)" not in _checks_by_name(
        run_doctor(load_config(), **kwargs))

    (claude_dir / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "modelquad hook-gate"}]},
    ]}}), encoding="utf-8")
    check = _checks_by_name(run_doctor(load_config(), **kwargs))["claude hook-gate runtime"]
    assert check.status is CheckStatus.WARN
    assert "earlier version" in check.detail and "modelquad repair" in check.detail
    assert _map_check_to_host(check.name) == "claude"

    ClaudeAdapter().install(claude_dir)
    assert "claude hook-gate runtime" not in _checks_by_name(run_doctor(load_config(), **kwargs))


@pytest.mark.parametrize(("command", "expected"), [
    (None, None),
    ("modelquad usage-hook claude", None),
    ("npx -y ccstatusline@latest", "Custom Command widget"),
    ("bash ~/.claude/my-status.sh", "statusLine.command"),
    ("bash ~/.claude/my-status.sh; modelquad usage-hook claude --quiet", "OK"),
])
def test_doctor_gives_the_one_line_recipe_for_a_users_own_status_line(tmp_path, command, expected):
    from modelquad.adapters.claude_adapter import segment_command

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    if command is not None:
        (claude_dir / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": command}}), encoding="utf-8")
    report = run_doctor(load_config(), usage_directory=tmp_path / "usage", claude_dir=claude_dir,
                        which=lambda _: "/fake/cli")
    check = _checks_by_name(report).get("claude status line (yours)")
    if expected is None:
        assert check is None
    elif expected == "OK":
        assert check.status is CheckStatus.OK
    else:
        assert check.status is CheckStatus.WARN
        assert expected in check.detail
        assert segment_command() in check.detail
        if expected == "statusLine.command":
            # Theirs first, then ours, on the same input: one line, ready to paste.
            assert f'printf \'%s\' "$input" | {command};' in check.detail
            assert "\n" not in check.detail


def test_claude_version_comes_from_the_status_line_never_from_running_claude(tmp_path):
    from modelquad.doctor import _claude_version_check

    usage = tmp_path / "usage"
    usage.mkdir()
    unseen = _claude_version_check(usage, read_push=lambda: None)
    assert unseen.status is CheckStatus.UNKNOWN and "send one message" in unseen.detail
    seen = _claude_version_check(usage, read_push=lambda: {"runtime_version": "2.1.300"})
    assert seen.status is CheckStatus.WARN
    assert "2.1.300" in seen.detail and "Pro or Max" in seen.detail and "claude update" in seen.detail
    (usage / "usage-claude.json").write_text(json.dumps({"five_hour_used_percentage": 12}), encoding="utf-8")
    ready = _claude_version_check(usage, read_push=lambda: {"runtime_version": "2.1.300"})
    assert ready.status is CheckStatus.OK and "2.1.300" in ready.detail


def test_doctor_detects_the_microsoft_store_python_alias(tmp_path):
    from modelquad.doctor import _python_alias_check

    local = tmp_path / "Local"
    stub = local / "Microsoft" / "WindowsApps"
    stub.mkdir(parents=True)
    (stub / "python.exe").write_bytes(b"")
    real = tmp_path / "Python311"
    real.mkdir()
    (real / "python.exe").write_bytes(b"MZ")

    trap = _python_alias_check(path_value=os.pathsep.join((str(stub), str(real))),
                               local_app_data=str(local), platform="nt")
    assert trap.status is CheckStatus.WARN
    assert "py -m pip install --upgrade modelquad" in trap.detail
    assert "App execution aliases" in trap.detail
    assert _python_alias_check(path_value=os.pathsep.join((str(real), str(stub))),
                               local_app_data=str(local), platform="nt") is None
    assert _python_alias_check(path_value=str(stub), local_app_data=str(local), platform="posix") is None


def test_doctor_prints_how_to_put_the_scripts_folder_on_path(tmp_path):
    from modelquad.doctor import _scripts_on_path_check

    scripts = tmp_path / "Python311" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "modelquad.exe").write_bytes(b"MZ")
    missing = _scripts_on_path_check(path_value=str(tmp_path), scripts_dirs=(str(scripts),), platform="nt")
    assert missing.status is CheckStatus.WARN
    assert str(scripts) in missing.detail and "SetEnvironmentVariable" in missing.detail
    assert _scripts_on_path_check(path_value=f"{tmp_path}{os.pathsep}{scripts}",
                                  scripts_dirs=(str(scripts),), platform="nt") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _scripts_on_path_check(path_value="", scripts_dirs=(str(empty),), platform="nt") is None


def test_doctor_checks_git_for_windows(tmp_path):
    from modelquad.doctor import _git_for_windows_check

    absent = _git_for_windows_check(which=lambda _: None, environ={}, platform="nt")
    assert absent.status is CheckStatus.WARN and "git-scm.com" in absent.detail
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_bytes(b"MZ")
    found = _git_for_windows_check(which=lambda _: None, environ={"ProgramFiles": str(tmp_path)}, platform="nt")
    assert found.status is CheckStatus.OK
    assert _git_for_windows_check(which=lambda _: "C:/Git/cmd/git.exe", environ={}, platform="nt").status \
        is CheckStatus.OK
    assert _git_for_windows_check(which=lambda _: None, environ={}, platform="posix") is None


def test_first_run_checklist_is_plain_and_ends_the_report(tmp_path, capsys, monkeypatch):
    from modelquad.doctor import DoctorReport, first_run_checklist

    report = DoctorReport((
        DoctorCheck("ModelQuad package", CheckStatus.OK, "0.3.0"),
        DoctorCheck("setup: Git for Windows", CheckStatus.WARN, "Install Git for Windows."),
        DoctorCheck("codex CLI", CheckStatus.OK, "Found."),
        DoctorCheck("claude adapter installed", CheckStatus.OK, "Installed."),
        DoctorCheck("claude MCP registered", CheckStatus.WARN, "Not registered."),
        DoctorCheck("claude version", CheckStatus.UNKNOWN, "Not seen yet. Open Claude Code."),
    ))
    items = first_run_checklist(report)
    assert items[0] == (True, "ModelQuad is installed.")
    assert (False, "Install Git for Windows.") in items
    assert (True, "The Codex CLI is installed.") in items
    assert (False, "Connect ModelQuad to Claude Code by running: modelquad install claude") in items
    assert (False, "Not seen yet. Open Claude Code.") in items

    import modelquad.cli as cli_module
    monkeypatch.setattr(cli_module, "run_doctor", lambda *args, **kwargs: report)
    assert main(["doctor", "--first-run"]) == 0
    out = capsys.readouterr().out
    assert out.index("Checklist:") > out.index("Overall:")
    assert "  [x] ModelQuad is installed." in out
    assert "  [ ] Install Git for Windows." in out
    assert "3 things left to do" in out
    assert main(["doctor", "--first-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checklist"][0] == {"done": True, "item": "ModelQuad is installed."}


# A stale service pidfile. The reader is a seam, and nothing here
# touches a real process.


def _write_pidfile(state_dir, pid):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "service.pid").write_text(
        json.dumps({"pid": pid, "argv": ["C:/fake/modelquad.exe", "serve"], "started_at": "2026-10-01T00:00:00Z"}),
        encoding="utf-8",
    )


def test_service_pidfile_naming_a_dead_pid_is_a_warning_with_its_fix(tmp_path):
    from modelquad.doctor import _service_pidfile_check
    from modelquad.service.lifecycle import ServiceProcessManager

    asked: list[int] = []

    def manager(alive):
        def pid_exists(pid):
            asked.append(pid)
            return alive

        return ServiceProcessManager(state_dir=tmp_path, pid_exists=pid_exists)

    assert _service_pidfile_check(manager(False)) is None  # no pidfile
    _write_pidfile(tmp_path, 4242)
    check = _service_pidfile_check(manager(False))
    assert check.status is CheckStatus.WARN
    assert "4242" in check.detail and "service stop" in check.detail
    assert str(tmp_path / "service.pid") in check.detail
    assert _service_pidfile_check(manager(True)) is None  # a live PID is not a finding
    assert asked == [4242, 4242]
    assert (tmp_path / "service.pid").is_file()  # reported, never removed


def test_run_doctor_reports_a_stale_service_pidfile(tmp_path):
    from modelquad import paths

    # A PID that cannot exist, checked through the real existence seam.
    _write_pidfile(paths.state_root(), 0x7FFFFFFC)
    report = run_doctor(Config(), usage_directory=tmp_path, registry=ProviderRegistry(()))
    check = _checks_by_name(report)["service pidfile"]
    assert check.status is CheckStatus.WARN
    assert str(0x7FFFFFFC) in check.detail


def test_a_live_service_pidfile_is_not_reported(tmp_path):
    from modelquad import paths

    _write_pidfile(paths.state_root(), os.getpid())
    report = run_doctor(Config(), usage_directory=tmp_path, registry=ProviderRegistry(()))
    assert "service pidfile" not in _checks_by_name(report)


@pytest.mark.parametrize(("value", "warned"), [
    (None, False), (r"C:\tools\lib", False), (".", True), ("src", True), (r"C:\tools\lib;", True),
])
def test_a_relative_pythonpath_entry_is_flagged(value, warned):
    from modelquad import doctor

    environ = {} if value is None else {"PYTHONPATH": value}
    check = doctor._python_path_check(environ)
    assert (check is not None) is warned
    if warned:
        assert check.status is CheckStatus.WARN and "relative or empty" in check.detail
