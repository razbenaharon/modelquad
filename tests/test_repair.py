# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
import pytest

from modelquad.adapters import ActionKind, FileAction, InstallReport
from modelquad.cli import build_parser, main
from modelquad.doctor import CheckStatus, DoctorCheck, DoctorReport
from modelquad.registry import (
    HostAdapterDescriptor,
    ProviderCapabilities,
    ProviderPlugin,
    ProviderRegistry,
    UsageSourceKind,
)
from modelquad.repair import _map_check_to_host, run_repair


class StubAdapter:
    install_calls: list[tuple[Path, bool]] = []
    #: False makes the stub finish without registering, like a skipped config.
    registers = True

    def __init__(self) -> None:
        pass

    def is_installed(self, target_dir: Path) -> bool:
        return False

    def install(self, target_dir: Path, *, dry_run: bool = False) -> InstallReport:
        StubAdapter.install_calls.append((target_dir, dry_run))
        if not dry_run and StubAdapter.registers:
            # Where the Claude host keeps its registration, so the outcome
            # repair reads back is a real connection.
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            (target_dir.parent / f"{target_dir.name}.json").write_text(
                json.dumps({"mcpServers": {"modelquad": {"command": "modelquad-mcp"}}}),
                encoding="utf-8",
            )
        return InstallReport(
            client="stub",
            dry_run=dry_run,
            actions=(FileAction(path=target_dir / "config.json", kind=ActionKind.CREATED),),
        )


def _make_test_registry(tmp_path: Path, detected_hosts: dict[str, bool] | None = None) -> ProviderRegistry:
    StubAdapter.install_calls = []
    StubAdapter.registers = True
    detected = detected_hosts or {}
    return ProviderRegistry(
        (
            ProviderPlugin(
                id="claude",
                display_name="Claude Code",
                capabilities=ProviderCapabilities(coding=True),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                cli_executable="claude",
                host_adapters=(
                    HostAdapterDescriptor(
                        id="claude",
                        display_name="Claude Code",
                        provider_id="claude",
                        adapter_cls=StubAdapter,
                        default_target_dir=lambda: tmp_path / "claude",
                        detect=lambda: detected.get("claude", True),
                    ),
                    HostAdapterDescriptor(
                        id="claude-desktop",
                        display_name="Claude Desktop",
                        provider_id="claude",
                        adapter_cls=StubAdapter,
                        default_target_dir=lambda: tmp_path / "claude-desktop",
                        detect=lambda: detected.get("claude-desktop", True),
                    ),
                ),
            ),
            ProviderPlugin(
                id="codex",
                display_name="Codex",
                capabilities=ProviderCapabilities(planning=True),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                cli_executable="codex",
                host_adapters=(
                    HostAdapterDescriptor(
                        id="codex",
                        display_name="Codex CLI",
                        provider_id="codex",
                        adapter_cls=StubAdapter,
                        default_target_dir=lambda: tmp_path / "codex",
                        detect=lambda: detected.get("codex", True),
                    ),
                ),
            ),
        )
    )


# --- Helper mapping tests ---


def test_map_check_to_host():
    assert _map_check_to_host("claude adapter installed") == "claude"
    assert _map_check_to_host("claude-desktop adapter installed") == "claude-desktop"
    assert _map_check_to_host("codex MCP registered") == "codex"
    assert _map_check_to_host("claude automatic-routing hook (structural)") == "claude"
    assert _map_check_to_host("usage sources") is None
    assert _map_check_to_host("codex CLI") is None
    assert _map_check_to_host("claude configured") is None


# --- run_repair Tests ---


def test_repair_warned_adapter_reinstalls_and_passes(monkeypatch, tmp_path):
    registry = _make_test_registry(tmp_path)

    before_report = DoctorReport(
        checks=(
            DoctorCheck("modelquad package", CheckStatus.OK, "0.8.0"),
            DoctorCheck("claude adapter installed", CheckStatus.WARN, "Not installed"),
            DoctorCheck("claude CLI", CheckStatus.OK, "Found"),
        )
    )
    after_report = DoctorReport(
        checks=(
            DoctorCheck("modelquad package", CheckStatus.OK, "0.8.0"),
            DoctorCheck("claude adapter installed", CheckStatus.OK, "Installed"),
            DoctorCheck("claude CLI", CheckStatus.OK, "Found"),
        )
    )

    doctor_calls: list[str] = []

    def fake_run_doctor(*args, **kwargs):
        if not doctor_calls:
            doctor_calls.append("before")
            return before_report
        doctor_calls.append("after")
        return after_report

    monkeypatch.setattr("modelquad.repair.run_doctor", fake_run_doctor)

    result = run_repair(registry=registry)

    assert result["repaired_hosts"] == ["claude"]
    assert result["skipped"] == []
    assert result["before_warn_count"] == 1
    assert result["after_warn_count"] == 0
    assert result["ok"] is True
    assert len(StubAdapter.install_calls) == 1
    assert StubAdapter.install_calls[0][1] is False  # dry_run = False
    assert doctor_calls == ["before", "after"]


def test_repair_reports_a_host_it_could_not_reconnect(monkeypatch, tmp_path):
    """A reinstall that leaves the host unregistered is a failure, not a repair."""
    registry = _make_test_registry(tmp_path)
    StubAdapter.registers = False
    report = DoctorReport(checks=(
        DoctorCheck("claude adapter installed", CheckStatus.WARN, "Not installed"),
    ))
    monkeypatch.setattr("modelquad.repair.run_doctor", lambda *a, **k: report)
    printed: list[str] = []

    result = run_repair(registry=registry, print_fn=printed.append)

    assert result["repaired_hosts"] == []
    assert [item["host"] for item in result["failed_hosts"]] == ["claude"]
    assert result["ok"] is False
    assert any(line.startswith("[failed] claude") for line in printed)


def test_repair_unrelated_warn_is_skipped(monkeypatch, tmp_path):
    registry = _make_test_registry(tmp_path)

    report = DoctorReport(
        checks=(
            DoctorCheck("modelquad package", CheckStatus.OK, "0.8.0"),
            DoctorCheck("usage sources", CheckStatus.WARN, "Missing snapshots"),
            DoctorCheck("claude configured", CheckStatus.WARN, "No API key"),
        )
    )
    monkeypatch.setattr("modelquad.repair.run_doctor", lambda *a, **k: report)

    result = run_repair(registry=registry)

    assert result["repaired_hosts"] == []
    assert "usage sources" in result["skipped"]
    assert "claude configured" in result["skipped"]
    assert len(StubAdapter.install_calls) == 0
    assert result["ok"] is True


def test_repair_dry_run_passes_flag_and_skips_second_doctor_run(monkeypatch, tmp_path):
    registry = _make_test_registry(tmp_path)

    before_report = DoctorReport(
        checks=(
            DoctorCheck("modelquad package", CheckStatus.OK, "0.8.0"),
            DoctorCheck("claude adapter installed", CheckStatus.WARN, "Not installed"),
        )
    )
    doctor_calls: list[str] = []

    def fake_run_doctor(*args, **kwargs):
        doctor_calls.append("call")
        return before_report

    monkeypatch.setattr("modelquad.repair.run_doctor", fake_run_doctor)

    result = run_repair(dry_run=True, registry=registry)

    assert doctor_calls == ["call"]  # No second doctor run in dry_run mode
    assert result["repaired_hosts"] == ["claude"]
    assert result["before_warn_count"] == 1
    assert result["after_warn_count"] == 1
    assert result["ok"] is True
    assert len(StubAdapter.install_calls) == 1
    assert StubAdapter.install_calls[0][1] is True  # dry_run = True passed through


def test_repair_missing_cli_reported_not_repairable(monkeypatch, tmp_path):
    registry = _make_test_registry(tmp_path)

    report = DoctorReport(
        checks=(
            DoctorCheck("modelquad package", CheckStatus.OK, "0.8.0"),
            DoctorCheck("codex CLI", CheckStatus.MISSING, "Install Codex CLI"),
        )
    )
    monkeypatch.setattr("modelquad.repair.run_doctor", lambda *a, **k: report)

    result = run_repair(registry=registry)

    assert result["repaired_hosts"] == []
    assert "codex CLI" in result["skipped"]
    assert result["ok"] is False  # report.ok is False due to MISSING


def test_repair_undetected_host_is_skipped(monkeypatch, tmp_path):
    registry = _make_test_registry(tmp_path, detected_hosts={"claude-desktop": False})

    report = DoctorReport(
        checks=(
            DoctorCheck("modelquad package", CheckStatus.OK, "0.8.0"),
            DoctorCheck("claude-desktop adapter installed", CheckStatus.WARN, "Not installed"),
        )
    )
    monkeypatch.setattr("modelquad.repair.run_doctor", lambda *a, **k: report)

    result = run_repair(registry=registry)

    assert result["repaired_hosts"] == []
    assert "claude-desktop adapter installed" in result["skipped"]
    assert len(StubAdapter.install_calls) == 0


def test_repair_claude_hook_and_mcp_checks(monkeypatch, tmp_path):
    registry = _make_test_registry(tmp_path)

    before_report = DoctorReport(
        checks=(
            DoctorCheck("claude automatic-routing hook (structural)", CheckStatus.WARN, "Hook missing"),
            DoctorCheck("codex MCP registered", CheckStatus.WARN, "MCP missing"),
        )
    )
    after_report = DoctorReport(
        checks=(
            DoctorCheck("claude automatic-routing hook (structural)", CheckStatus.OK, "Hook wired"),
            DoctorCheck("codex MCP registered", CheckStatus.OK, "MCP wired"),
        )
    )

    calls = []

    def fake_doctor(*a, **k):
        if not calls:
            calls.append(1)
            return before_report
        return after_report

    monkeypatch.setattr("modelquad.repair.run_doctor", fake_doctor)

    result = run_repair(registry=registry)
    assert set(result["repaired_hosts"]) == {"claude", "codex"}
    assert result["before_warn_count"] == 2
    assert result["after_warn_count"] == 0
    assert result["ok"] is True


# --- CLI tests for repair ---


def test_cli_repair_parser():
    parser = build_parser()
    args = parser.parse_args(["repair", "--dry-run", "--json"])
    assert args.command == "repair"
    assert args.dry_run is True
    assert args.as_json is True


def test_cli_repair_execution(monkeypatch, capsys):
    fake_result = {
        "repaired_hosts": ["claude"],
        "skipped": [],
        "before_warn_count": 1,
        "after_warn_count": 0,
        "ok": True,
    }
    monkeypatch.setattr("modelquad.repair.run_repair", lambda **k: fake_result)

    exit_code = main(["repair", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["repaired_hosts"] == ["claude"]
