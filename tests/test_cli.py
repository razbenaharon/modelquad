# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The ``modelquad`` command: install and uninstall argument handling, ``mcp``,
``privacy``, ``review package``, ``doctor`` and the retired commands.

``--version`` is in tests/test_cli_version.py, ``on``/``off`` in
tests/test_switch.py, ``settings`` in tests/test_settings_show.py,
tests/test_repo_settings.py and tests/test_standing_grant.py, and ``review
report``/``review verdict`` in tests/test_review_ledger.py.
"""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

import modelquad.cli as cli_module

from modelquad.adapters import ActionKind, FileAction, InstallReport
from modelquad.adapters.registry import HostAdapterDescriptor
from modelquad.cli import main


@pytest.fixture(autouse=True)
def isolated_cleanup_home(tmp_path, monkeypatch, isolate_home_directory):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


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
    # Install now also takes back an earlier build's status line, pending hook
    # and retired skills; on a fresh folder those steps are no-ops.
    assert kinds == {ActionKind.WOULD_CREATE.value, ActionKind.UNCHANGED.value}


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
            "detected", "Detected Host", _RecordingAdapter, lambda: detected_target, detect=lambda: True
        ),
        "missing": HostAdapterDescriptor(
            "missing", "Missing Host", _RecordingAdapter, lambda: tmp_path / "missing", detect=lambda: False
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
                "report": {
                    "actions": [
                        {"backup_path": None, "detail": None, "kind": "would_create", "path": str(detected_target / "managed")}
                    ],
                    "client": "recording",
                    "dry_run": True,
                    "unchanged": True,
                },
            },
            {"client": "missing", "detected": False, "error": None, "report": None, "restore": None},
        ],
    }


def test_global_install_then_uninstall_round_trips_only_test_targets(tmp_path, monkeypatch):
    from modelquad.adapters.claude_adapter import ClaudeAdapter

    descriptors = {
        "claude": HostAdapterDescriptor("claude", "Claude Code", ClaudeAdapter, lambda: tmp_path / "claude", detect=lambda: True),
    }
    _patch_global_hosts(monkeypatch, descriptors)

    assert main(["install", "--global"]) == 0
    assert main(["uninstall", "--global"]) == 0
    assert all(not descriptor.adapter_cls().is_installed(descriptor.default_target_dir()) for descriptor in descriptors.values())


def test_build_parser_accepts_mcp_subcommand():
    parser = cli_module.build_parser()
    args = parser.parse_args(["mcp"])
    assert args.command == "mcp"


def test_main_dispatches_to_mcp_server_main(monkeypatch):
    called = []
    fake_module = types.ModuleType("modelquad.mcp.server")
    fake_module.main = lambda: called.append(True)
    monkeypatch.setitem(sys.modules, "modelquad.mcp.server", fake_module)

    assert main(["mcp"]) == 0
    assert called == [True]


def test_dev_command_is_removed(capsys):
    parser = cli_module.build_parser()
    assert "dev" not in parser._subparsers._group_actions[0].choices
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["dev"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_privacy_command_reports_no_telemetry_and_the_loopback_rule(tmp_path, capsys, monkeypatch):
    import modelquad.paths as paths_module

    monkeypatch.setattr(paths_module, "state_root", lambda: tmp_path / "state")

    assert main(["privacy"]) == 0
    out = capsys.readouterr().out
    assert "ModelQuad has no telemetry." in out
    assert "loopback" in out
    assert "consent" not in out.lower()

    assert main(["privacy", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["telemetry"] == "none"
    assert "loopback" in payload["network"]
    assert not any("consent" in key for key in payload)


def test_cli_privacy_lists_retention_earlier_builds_and_delete_steps(tmp_path, capsys, monkeypatch):
    """`modelquad privacy` names every store, what earlier builds left and how to delete it all."""
    import modelquad.paths as paths_module

    monkeypatch.setattr(paths_module, "state_root", lambda: tmp_path / "state")

    assert main(["privacy", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["retention"] == dict(cli_module.PRIVACY_RETENTION)
    assert payload["retention"]["review ledger (review-ledger.db)"] == "no automatic expiry"
    assert payload["retention"]["review reminders (review-reminder)"] == "deleted after 2 days"
    assert "running-worker records (active-workers)" in payload["retention"]
    assert payload["left_by_earlier_builds"] == list(cli_module.PRIVACY_LEGACY)
    steps = " ".join(payload["delete_everything"])
    for needed in ("modelquad uninstall claude", "claude plugin uninstall modelquad",
                   "*.modelquad-backup-*", "secrets", str(tmp_path / "state")):
        assert needed in steps, needed
    # The service is gone, so deleting everything never sends anyone to it.
    assert "service" not in steps
    assert "--global" not in steps

    assert main(["privacy"]) == 0
    out = capsys.readouterr().out
    for store, rule in cli_module.PRIVACY_RETENTION:
        assert f"{store}: {rule}." in out
    for item in cli_module.PRIVACY_LEGACY:
        assert item in out
    assert "3. Delete the *.modelquad-backup-* files" in out


def test_cli_privacy_retention_and_delete_steps_match_privacy_md():
    """`modelquad privacy` says what PRIVACY.md says, store by store."""
    doc = (Path(__file__).resolve().parents[1] / "PRIVACY.md").read_text(encoding="utf-8")
    everything = doc.split("- **Everything**:", 1)[1].split("Deleting files is ordinary", 1)[0]
    rows = {line.split("|")[1].strip(): line for line in doc.splitlines()
            if line.startswith("| `")}
    for store, rule in cli_module.PRIVACY_RETENTION:
        name = store.split("(", 1)[1].rstrip(")")
        row = next((line for key, line in rows.items() if name in key), None)
        assert row is not None, name
        assert rule.split(";")[0].lower() in row.lower(), (name, rule)
    for needed in ("modelquad uninstall claude", "claude plugin uninstall modelquad",
                   "*.modelquad-backup-*", "secrets"):
        assert needed in everything, needed
    assert "modelquad service" not in everything


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (project / ".env").write_text("API_TOKEN=not-a-real-value\n", encoding="utf-8")
    return project


def test_review_package_prints_the_package_and_starts_nothing(tmp_path, capsys):
    project = _project(tmp_path)

    assert main(["review", "package", "app.py", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "- app.py (" in out
    assert "Limits:" in out

    assert main(["review", "package", "app.py", "--project", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["path"] for item in payload["files"]] == ["app.py"]
    assert payload["file_count"] == 1
    assert not payload.get("refused")


def test_review_package_refuses_a_secret_file(tmp_path, capsys):
    project = _project(tmp_path)

    assert main(["review", "package", ".env", "--project", str(project)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("This package cannot be sent.")
    assert "not-a-real-value" not in out

    assert main(["review", "package", ".env", "--project", str(project), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is True and payload["started"] is False
    assert "not-a-real-value" not in json.dumps(payload)


def _check(name, ok):
    return {"name": name, "ok": ok, "detail": f"{name} detail", "next": None}


@pytest.mark.parametrize("codex_ok, code", [(True, 0), (None, 0), (False, 1)])
def test_doctor_json_reports_every_check_without_starting_codex(tmp_path, capsys, monkeypatch, codex_ok, code):
    from modelquad import setupcheck

    monkeypatch.setattr(setupcheck, "_codex", lambda: _check("codex", codex_ok))
    monkeypatch.setattr(setupcheck, "_connection", lambda: _check("claude_code", True))
    project = tmp_path / "project"
    project.mkdir()

    assert main(["doctor", "--json", "--project", str(project)]) == code
    payload = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in payload["checks"]] == [
        "platform", "codex", "claude_code", "settings", "folder"]
    assert payload["ok"] is (code == 0)
    assert payload["checks"][1]["ok"] is codex_ok


def test_local_only_is_no_longer_a_command(capsys):
    """Regression: the Local Only switch is removed.

    ``modelquad local-only`` is not a subcommand any more, with or without
    its old flags: argparse rejects it as an invalid choice, and it is absent
    from the help text.
    """

    for argv in (["local-only"], ["local-only", "--enable"], ["local-only", "--json"]):
        with pytest.raises(SystemExit) as exc_info:
            main(argv)
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


@pytest.mark.parametrize("argv", [["hook-gate"], ["hook-pending", "claude"], ["usage-hook", "claude"],
                                  ["--usage-directory", "x", "usage-hook", "claude"]])
@pytest.mark.parametrize("stdin", ['{"tool_name": "Bash", "session_id": "s1"}', "not json", ""])
def test_retired_hooks_are_a_silent_no_op_for_sessions_from_before_the_upgrade(capsys, monkeypatch, stdin, argv):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
