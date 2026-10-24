# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

import modelquad.cli as cli_module

from modelquad.adapters import ActionKind, FileAction, InstallReport
from modelquad.cli import main
from modelquad.doctor import CheckStatus, DoctorCheck, DoctorReport
from modelquad.execution import ExecutionResult
from modelquad.registry import HostAdapterDescriptor
from routing_mode_fixtures import save_legacy_mode


@pytest.fixture(autouse=True)
def isolated_cleanup_home(tmp_path, monkeypatch, isolate_home_directory):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Keep the import-time DB default aligned with this module's later home
    # override.
    from modelquad import paths
    monkeypatch.setattr(cli_module.service_db, "DEFAULT_DB_PATH", paths.db_path())


def write_usage_fixtures(tmp_path):
    (tmp_path / "usage-claude.json").write_text(
        json.dumps({"five_hour_used_percentage": 10, "seven_day_used_percentage": 20}), encoding="utf-8"
    )
    (tmp_path / "usage-codex.json").write_text(
        json.dumps({"available": True, "five_hour_remaining_percentage": 80, "seven_day_remaining_percentage": 70}),
        encoding="utf-8",
    )


def test_status_and_json_route_use_injected_usage_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    write_usage_fixtures(tmp_path)

    assert main(["--usage-directory", str(tmp_path), "status"]) == 0
    status_output = capsys.readouterr().out
    assert "Provider" in status_output
    assert "claude" in status_output

    assert main(["--usage-directory", str(tmp_path), "route", "implementation", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task_type"] == "implementation"
    # Codex is intentionally restricted to read-only work in the public build.
    assert payload["recommendation"] == "claude"


def test_install_all_uses_the_explicit_target_directory(tmp_path):
    claude_dir = tmp_path / "claude-target"

    assert main(["install", "--all", "--claude-dir", str(claude_dir)]) == 0

    assert (claude_dir / "skills" / "modelquad" / "SKILL.md").is_file()
    assert not (claude_dir / "AGENTS.md").exists()


def test_install_has_no_codex_or_desktop_target_option(capsys):
    """Claude Code is the only host (decision H1)."""
    with pytest.raises(SystemExit):
        main(["install", "--all", "--codex-dir", "anywhere"])
    with pytest.raises(SystemExit):
        main(["install", "codex"])


def test_install_all_dry_run_reports_actions_without_creating_directories(tmp_path, capsys):
    claude_dir = tmp_path / "claude-target"

    assert main(["install", "--all", "--claude-dir", str(claude_dir), "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert not claude_dir.exists()
    kinds = {
        action["kind"]
        for report in payload["reports"]
        for action in report["actions"]
    }
    assert kinds == {ActionKind.WOULD_CREATE.value}


class _RecordingAdapter:
    calls: list[tuple[str, object, bool]] = []

    def install(self, target_dir, *, dry_run=False):
        self.calls.append(("install", target_dir, dry_run))
        return InstallReport("recording", dry_run, (FileAction(target_dir / "managed", ActionKind.WOULD_CREATE),))

    def uninstall(self, target_dir, *, dry_run=False):
        self.calls.append(("uninstall", target_dir, dry_run))
        return InstallReport("recording", dry_run, (FileAction(target_dir / "managed", ActionKind.WOULD_REMOVE),))


def _patch_global_hosts(monkeypatch, descriptors):
    original_descriptor = cli_module.get_host_descriptor
    monkeypatch.setattr(cli_module, "CLIENT_NAMES", tuple(descriptors))
    monkeypatch.setattr(cli_module, "get_host_descriptor", lambda client:
                        descriptors[client] if client in descriptors else original_descriptor(client))


def test_global_install_dry_run_uses_lazy_detection_and_skips_undetected_host(tmp_path, monkeypatch, capsys):
    _RecordingAdapter.calls.clear()
    detected_target = tmp_path / "detected"
    descriptors = {
        "detected": HostAdapterDescriptor(
            "detected", "Detected Host", "synthetic", _RecordingAdapter, lambda: detected_target, detect=lambda: True
        ),
        "missing": HostAdapterDescriptor(
            "missing", "Missing Host", "synthetic", _RecordingAdapter, lambda: tmp_path / "missing", detect=lambda: False
        ),
    }
    _patch_global_hosts(monkeypatch, descriptors)

    assert main(["install", "--global", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert not detected_target.exists()
    assert _RecordingAdapter.calls == [("install", detected_target, True)]
    from modelquad.disclosure import DATA_FLOW

    assert payload.pop("data_flow_notice") == list(DATA_FLOW)
    assert payload == {
        "dry_run": True,
        "global": True,
        "results": [
            {
                "client": "detected",
                "detected": True,
                "error": None,
                # Present on every result, not only on uninstall, so the JSON
                # schema is the same shape whichever command produced it. An
                # install displaces nothing, so it is always null here.
                "restore": None,
                "status_line_recipe": None,
                "report": {
                    "actions": [
                        {"backup_path": None, "detail": None, "kind": "would_create", "path": str(detected_target / "managed")}
                    ],
                    "client": "recording",
                    "dry_run": True,
                    "unchanged": True,
                },
            },
            {"client": "missing", "detected": False, "error": None, "report": None, "restore": None,
             "status_line_recipe": None},
        ],
    }


def test_global_install_then_uninstall_round_trips_only_test_targets(tmp_path, monkeypatch):
    from modelquad.adapters.claude_adapter import ClaudeAdapter

    descriptors = {
        "claude": HostAdapterDescriptor("claude", "Claude Code", "claude", ClaudeAdapter, lambda: tmp_path / "claude", detect=lambda: True),
    }
    _patch_global_hosts(monkeypatch, descriptors)

    assert main(["install", "--global"]) == 0
    assert main(["uninstall", "--global"]) == 0
    assert all(not descriptor.adapter_cls().is_installed(descriptor.default_target_dir()) for descriptor in descriptors.values())


def test_status_global_json_uses_doctor_adapter_checks_and_detection(tmp_path, monkeypatch, capsys):
    write_usage_fixtures(tmp_path)
    descriptors = {
        "detected": HostAdapterDescriptor(
            "detected", "Detected Host", "claude", _RecordingAdapter, lambda: tmp_path / "detected", detect=lambda: True
        ),
        "missing": HostAdapterDescriptor(
            "missing", "Missing Host", "codex", _RecordingAdapter, lambda: tmp_path / "missing", detect=lambda: False
        ),
    }
    _patch_global_hosts(monkeypatch, descriptors)
    report = DoctorReport(
        (
            DoctorCheck("ModelQuad package", CheckStatus.OK, "0.2.0"),
            DoctorCheck("detected adapter installed", CheckStatus.OK, "Installed."),
            DoctorCheck("missing adapter installed", CheckStatus.WARN, "Not installed."),
        )
    )
    monkeypatch.setattr(cli_module, "run_doctor", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        cli_module,
        "ServiceProcessManager",
        lambda: type(
            "FakeServiceProcessManager",
            (),
            {
                "status": lambda self: cli_module.ProcessStatus(
                    running=False,
                    pid=None,
                )
            },
        )(),
    )

    assert main(["--usage-directory", str(tmp_path), "status", "--global", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["service"] == "not running"
    assert payload["overall"] == "HEALTHY"
    assert payload["hosts"] == [
        {"detected": True, "display_name": "Detected Host", "id": "detected", "installed": True, "verified": True},
        {"detected": False, "display_name": "Missing Host", "id": "missing", "installed": False, "verified": True},
    ]


def test_activity_cli_filters_and_renders_project_without_an_absolute_path(tmp_path, capsys):
    from modelquad.service import history

    db_path = tmp_path / "history.db"
    history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(provider="codex", success=True, output="", exit_code=0),
        project=str(tmp_path / "nested" / "project-one"),
    )
    history.record_execution(
        db_path,
        "debugging",
        ExecutionResult(provider="claude", success=False, output="", exit_code=1),
        project=str(tmp_path / "nested" / "project-two"),
    )

    assert main(["--history-db", str(db_path), "activity", "--provider", "codex", "--json"]) == 0

    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1
    assert entries[0]["provider"] == "codex"
    assert entries[0]["project"] == "project-one"
    assert os.sep not in entries[0]["project"]


def test_usage_hook_claude_writes_snapshot_and_prints_summary(tmp_path, capsys, monkeypatch):
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 25, "resets_at": 100},
            "seven_day": {"used_percentage": 60, "resets_at": 200},
        }
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert main(["--usage-directory", str(tmp_path), "usage-hook", "claude"]) == 0

    # Both windows reset in 1970, so the line gives no number for a window
    # that no longer exists -- the next message brings a new reading -- and
    # the snapshot keeps the facts.
    assert capsys.readouterr().out.strip() == (
        "Claude 5h shows after your next message / 7d shows after your next message"
        " | Codex 5h unknown / 7d unknown")
    written = json.loads((tmp_path / "usage-claude.json").read_text(encoding="utf-8"))
    collected_at = written.pop("collected_at")
    assert collected_at
    # A push carrying real percentages is an observation made now, and is
    # stamped as one so it can outrank an older Claude Desktop reading.
    assert written.pop("observed_at") == collected_at
    assert written == {
        "provider": "claude",
        "source": "claude_statusline",
        "five_hour_used_percentage": 25,
        "seven_day_used_percentage": 60,
        "five_hour_remaining_percentage": 75.0,
        "seven_day_remaining_percentage": 40.0,
        "five_hour_resets_at": 100,
        "seven_day_resets_at": 200,
    }


def test_usage_hook_degrades_gracefully_on_malformed_stdin(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    assert main(["--usage-directory", str(tmp_path), "usage-hook", "claude"]) == 0

    assert capsys.readouterr().out.strip() == (
        "Claude: shows after your first message | Codex 5h unknown / 7d unknown")
    assert (tmp_path / "usage-claude.json").is_file()


def _rerun_hook_gate(monkeypatch, capsys, payload) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook-gate"]) == 0
    return json.loads(capsys.readouterr().out)


def test_build_parser_accepts_mcp_subcommand():
    parser = cli_module.build_parser()
    args = parser.parse_args(["mcp"])
    assert args.command == "mcp"


def test_run_mcp_command_reports_error_when_mcp_extra_missing(capsys, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "modelquad.mcp.server", None)

    parser = cli_module.build_parser()
    args = parser.parse_args(["mcp"])
    assert cli_module._run_mcp_command(args) == 1
    captured = capsys.readouterr()
    assert 'Install the mcp extra: pip install -e ".[mcp]"' in captured.err


def test_run_mcp_command_invokes_mcp_main_on_success(monkeypatch):
    import sys
    import types

    called = []
    fake_module = types.ModuleType("modelquad.mcp.server")
    fake_module.main = lambda: called.append(True)
    monkeypatch.setitem(sys.modules, "modelquad.mcp.server", fake_module)

    parser = cli_module.build_parser()
    args = parser.parse_args(["mcp"])
    assert cli_module._run_mcp_command(args) == 0
    assert called == [True]


def test_main_dispatches_to_mcp_command(monkeypatch):
    called = []
    monkeypatch.setattr(cli_module, "_run_mcp_command", lambda args: called.append(args.command) or 0)

    assert main(["mcp"]) == 0
    assert called == ["mcp"]


def _expected_service_options(port):
    from modelquad.statecontext import resolve_state_context
    args = cli_module.build_parser().parse_args(["serve"])
    context = resolve_state_context()
    expected = ["--port", str(port),
                "--db", str(cli_module.service_db.DEFAULT_DB_PATH.expanduser().resolve()),
                "--poll-interval", "60.0", "--state-root", str(context.state_root.expanduser().resolve()),
                "--state-mode", context.mode.value]
    if args.config is not None:
        expected += ["--config", str(args.config.expanduser().resolve())]
    if args.usage_directory is not None:
        expected += ["--usage-directory", str(args.usage_directory.expanduser().resolve())]
    return expected


def test_service_launch_command_uses_the_isolated_bootstrap(monkeypatch):
    cmd = cli_module._service_launch_command(8765)
    executable = Path(cli_module.sys.executable)
    image = Path(getattr(cli_module.sys, "_base_executable", executable)
                 if cli_module.sys.platform == "win32" else executable)
    expected = [str(image.resolve()), "-I"]
    installation = cli_module.sys.platform == "win32" and executable != image
    if installation:
        expected.append("-S")
    # The isolated bootstrap imports the CLI module from its own package root;
    # neither cwd nor inherited PYTHONPATH selects another checkout.
    expected.append(str(Path(cli_module.__file__).resolve().parent / "service" / "entry.py"))
    if installation:
        expected += ["--installation-python", str(executable.resolve())]
    assert cmd == expected + ["serve", "--managed-child"] + _expected_service_options(8765)


def _pe_subsystem(path) -> int:
    """The PE optional header's Subsystem field: 2 is GUI, 3 is console.

    The whole console-window finding is a property of the image rather than of any
    argument, so the test that holds it reads the image. Offsets: the PE
    signature's location lives at 0x3C, the COFF header is 20 bytes, and
    Subsystem sits 68 bytes into the optional header that follows.
    """

    import struct

    head = Path(path).read_bytes()[:0x400]
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    assert head[pe:pe + 4] == b"PE\0\0", f"{path} is not a PE image"
    return struct.unpack_from("<H", head, pe + 24 + 68)[0]


def test_windowless_launcher_is_never_offered_off_windows(monkeypatch):
    monkeypatch.setattr(cli_module.sys, "platform", "linux")

    assert cli_module._windowless_launcher() is None


def test_windowless_launcher_resolves_the_interpreters_w_sibling(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(cli_module.sys, "executable", str(tmp_path / "python.exe"))

    assert cli_module._windowless_launcher() == tmp_path / "pythonw.exe"


def test_windowless_launcher_keeps_an_already_windowless_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(cli_module.sys, "executable", str(tmp_path / "pythonw.exe"))

    assert cli_module._windowless_launcher() == tmp_path / "pythonw.exe"


def test_service_autostart_command_targets_the_windowless_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    for name in ("python.exe", "pythonw.exe"):
        image = tmp_path / name
        image.write_bytes(b"fixture; never execute")
        image.chmod(0o700)
    monkeypatch.setattr(cli_module.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(cli_module.sys, "_base_executable", str(tmp_path / "python.exe"), raising=False)
    assert cli_module._service_autostart_command(8765) == [
        str(tmp_path / "pythonw.exe"), "-I",
        str(Path(cli_module.__file__).resolve().parent / "service" / "entry.py"),
        "service", "start", "--supervise",
    ] + _expected_service_options(8765)


def test_service_autostart_command_degrades_when_no_windowless_sibling_exists(monkeypatch, tmp_path):
    """An absent admitted windowless image refuses, with no console fallback."""
    from modelquad.executable_resolution import UnsafeExecutable
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    (tmp_path / "python.exe").write_bytes(b"fixture")
    monkeypatch.setattr(cli_module.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(cli_module.sys, "_base_executable", str(tmp_path / "python.exe"), raising=False)
    with pytest.raises(UnsafeExecutable, match="not_found"):
        cli_module._service_autostart_command(8765)


@pytest.mark.skipif(sys.platform != "win32", reason="PE subsystems are a Windows fact")
def test_the_registered_autostart_image_is_not_console_subsystem_on_this_machine():
    """The invariant, read off the image rather than off a path string.

    A subsystem-3 action in a logon task holds a console window on
    the desktop for the life of the service, and nothing in the task definition
    overrides it. So what `service install` would register must be subsystem 2.
    """

    launcher = cli_module._windowless_launcher()
    if launcher is None:
        pytest.skip("this interpreter layout ships no windowless sibling")
    assert _pe_subsystem(launcher) == 2
    assert _pe_subsystem(cli_module.sys.executable) == 3, (
        "the console interpreter is the thing being avoided; if it is not "
        "subsystem 3 this test is no longer measuring anything"
    )


@pytest.mark.parametrize("redirected", [True, False])
def test_managed_service_stream_binding_discards_existing_or_absent_streams(monkeypatch, redirected):
    import modelquad.windowless_diagnostics as diagnostics
    sentinel_out, sentinel_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(cli_module.sys, 'stdout', sentinel_out if redirected else None)
    monkeypatch.setattr(cli_module.sys, 'stderr', sentinel_err if redirected else None)
    monkeypatch.setattr(diagnostics, '_active', False)
    monkeypatch.setattr(diagnostics, '_sink', None)
    events = []
    monkeypatch.setattr(diagnostics, 'bind_selected_state', lambda role: False)
    monkeypatch.setattr(diagnostics, 'emit', lambda event: events.append(event))
    cli_module._bind_absent_std_streams()
    print('private-content-canary', file=cli_module.sys.stdout)
    print('private-error-canary', file=cli_module.sys.stderr)
    assert cli_module.sys.stdout.isatty() is False
    assert sentinel_out.getvalue() == sentinel_err.getvalue() == ''
    assert events == ['entry']


def test_service_binding_does_not_roll_or_append_to_legacy_log(monkeypatch, tmp_path):
    import modelquad.windowless_diagnostics as diagnostics
    log_path = tmp_path / 'service.log'
    log_path.write_text('old legacy content', encoding='utf-8')
    monkeypatch.setattr(cli_module.sys, 'stdout', None)
    monkeypatch.setattr(cli_module.sys, 'stderr', None)
    monkeypatch.setattr(diagnostics, '_active', False)
    monkeypatch.setattr(diagnostics, '_sink', None)
    monkeypatch.setattr(diagnostics, 'bind_selected_state', lambda role: False)
    cli_module._bind_absent_std_streams()
    print('raw output canary', file=cli_module.sys.stdout)
    assert log_path.read_text(encoding='utf-8') == 'old legacy content'


def test_service_diagnostic_binding_failure_does_not_break_streams(monkeypatch):
    import modelquad.windowless_diagnostics as diagnostics
    monkeypatch.setattr(cli_module.sys, 'stdout', None)
    monkeypatch.setattr(cli_module.sys, 'stderr', None)
    monkeypatch.setattr(diagnostics, '_active', False)
    monkeypatch.setattr(diagnostics, '_sink', None)
    monkeypatch.setattr(diagnostics, 'bind_selected_state', lambda role: False)
    cli_module._bind_absent_std_streams()
    assert cli_module.sys.stdout is not None
    assert cli_module.sys.stdout.isatty() is False


def test_run_service_command_binds_absent_streams_before_touching_uvicorn(monkeypatch):
    """Ordering is the whole point: uvicorn reads sys.stdout on the way in."""
    import uvicorn
    import modelquad.service.app as service_app_module

    order = []
    monkeypatch.setattr(cli_module, "_bind_absent_std_streams", lambda: order.append("bind"))
    monkeypatch.setattr(service_app_module, "create_app", lambda **kw: order.append("app") or object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: order.append("uvicorn"))
    monkeypatch.setattr(cli_module.ServiceProcessManager, "admit_child", lambda self, **kw: True)
    args = cli_module.build_parser().parse_args(["serve", "--managed-child"])
    assert cli_module._run_service_command(args) == 0
    assert order == ["bind", "app", "uvicorn"]


def test_dev_command_is_removed(capsys):
    parser = cli_module.build_parser()
    assert "dev" not in parser._subparsers._group_actions[0].choices
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["dev"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_run_service_command_has_no_static_serving(monkeypatch):
    import uvicorn
    import modelquad.service.app as service_app_module

    captured = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(service_app_module, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli_module.ServiceProcessManager, "admit_child", lambda self, **kw: True)

    parser = cli_module.build_parser()
    args = parser.parse_args(["serve", "--managed-child"])
    assert cli_module._run_service_command(args) == 0
    assert "static_dir" not in captured
    with pytest.raises(SystemExit):
        parser.parse_args(["dashboard"])
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--static-dir", "."])


def test_build_parser_accepts_onboard_subcommand():
    parser = cli_module.build_parser()
    args = parser.parse_args(
        ["onboard", "--providers", "claude,codex", "--yes", "--dry-run", "--json"]
    )
    assert args.command == "onboard"
    assert args.providers == "claude,codex"
    assert args.assume_yes is True
    assert args.dry_run is True
    assert args.as_json is True


def test_cli_network_command(tmp_path, capsys):
    db_path = tmp_path / "cli_network.db"

    assert main(["--history-db", str(db_path), "network"]) == 0
    out = capsys.readouterr().out
    assert "connects only to loopback addresses" in out
    assert "audit hook" in out
    assert "Codex CLI" in out

    assert main(["--history-db", str(db_path), "network", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"modelquad_process", "enforcement", "codex_cli"}
    assert "loopback" in payload["modelquad_process"]


def test_cli_privacy_command_reports_no_telemetry_and_the_loopback_rule(tmp_path, capsys, monkeypatch):
    import modelquad.paths as paths_module

    monkeypatch.setattr(paths_module, "state_root", lambda: tmp_path / "state")
    db_path = tmp_path / "cli_privacy.db"

    assert main(["--history-db", str(db_path), "privacy"]) == 0
    out = capsys.readouterr().out
    assert "ModelQuad has no telemetry." in out
    assert "loopback" in out
    assert "consent" not in out.lower()

    assert main(["--history-db", str(db_path), "privacy", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["telemetry"] == "none"
    assert "loopback" in payload["network"]
    assert not any("consent" in key for key in payload)

# --- recorded routing evidence agrees with the route taken -----


def test_run_service_command_discards_raw_logs_for_exactly_the_life_of_the_run(monkeypatch):
    """No raw log/watchdog; quiet uvicorn on success and interruption."""

    import uvicorn
    import modelquad.service.app as service_app_module

    order: list[str] = []

    monkeypatch.setattr(service_app_module, "create_app", lambda **kwargs: object())
    def quiet_run(*a, **kwargs):
        assert kwargs['log_config'] is None and kwargs['access_log'] is False
        assert kwargs['log_level'] == 'critical'
        order.append('uvicorn')
    monkeypatch.setattr(uvicorn, "run", quiet_run)
    monkeypatch.setattr(cli_module.ServiceProcessManager, "admit_child", lambda self, **kw: True)

    parser = cli_module.build_parser()
    assert cli_module._run_service_command(parser.parse_args(["serve", "--managed-child"])) == 0

    assert order == ["uvicorn"]

    order.clear()

    def explode(*a, **k):
        quiet_run(*a, **k)
        raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn, "run", explode)
    with pytest.raises(KeyboardInterrupt):
        cli_module._run_service_command(parser.parse_args(["serve", "--managed-child"]))

    assert order == ["uvicorn"]


def test_local_only_is_no_longer_a_command(tmp_path, capsys):
    """Regression: the Local Only switch is removed.

    ``modelquad local-only`` is not a subcommand any more, with or without
    its old flags: argparse rejects it as an invalid choice, and it is absent
    from the help text.
    """

    db_path = tmp_path / "cli_retired_switch.db"
    for argv in (["local-only"], ["local-only", "--enable"], ["local-only", "--json"]):
        with pytest.raises(SystemExit) as exc_info:
            main(["--history-db", str(db_path), *argv])
        assert exc_info.value.code == 2, argv
        assert "invalid choice: 'local-only'" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "local-only" not in capsys.readouterr().out


def test_delegate_is_rejected_by_parser(capsys):
    with pytest.raises(SystemExit) as error:
        cli_module.build_parser().parse_args(["delegate"])
    assert error.value.code == 2
    assert "invalid choice: 'delegate'" in capsys.readouterr().err


@pytest.mark.parametrize("stdin", ['{"tool_name": "Bash", "session_id": "s1"}', "not json", ""])
def test_hook_gate_is_a_silent_no_op_for_sessions_from_before_the_upgrade(capsys, monkeypatch, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert main(["hook-gate"]) == 0
    assert capsys.readouterr().out == ""
