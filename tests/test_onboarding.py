# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for the first-run onboarding wizard."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
import shutil

from modelquad.adapters import ActionKind, FileAction, InstallReport
import modelquad.cli as cli_module
from modelquad.cli import main
from modelquad.doctor import CheckStatus, DoctorCheck, DoctorReport
from modelquad.adapters.registration import RegistrationStatus
from modelquad.onboarding import (
    OnboardingResult,
    OnboardingStepResult,
    ProviderDetection,
    detect_providers,
    install_detected_hosts,
    run_onboarding,
)
from modelquad.provider_connection import (
    InstallState,
    SignInState,
    describe_connections,
    probe_sign_in,
)
from modelquad.registry import (
    HostAdapterDescriptor,
    ProviderCapabilities,
    ProviderPlugin,
    ProviderRegistry,
    UsageSourceKind,
)
from modelquad.service import db as service_db
from modelquad.service import history as service_history
from routing_mode_fixtures import save_legacy_mode


class StubAdapter:
    installed = True
    installed_calls: list[Path] = []
    install_calls: list[tuple[Path, bool]] = []

    def __init__(self) -> None:
        pass

    def is_installed(self, target_dir: Path) -> bool:
        StubAdapter.installed_calls.append(target_dir)
        return self.installed

    def install(self, target_dir: Path, *, dry_run: bool = False) -> InstallReport:
        StubAdapter.install_calls.append((target_dir, dry_run))
        return InstallReport(
            client="stub",
            dry_run=dry_run,
            actions=(FileAction(path=target_dir / "config.json", kind=ActionKind.CREATED),),
        )


def _make_stub_registry(tmp_path: Path) -> ProviderRegistry:
    StubAdapter.installed_calls = []
    StubAdapter.install_calls = []
    return ProviderRegistry(
        (
            ProviderPlugin(
                id="tool-a",
                display_name="Tool Alpha",
                capabilities=ProviderCapabilities(coding=True),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                cli_executable="tool-a-cli",
                host_adapters=(
                    HostAdapterDescriptor(
                        id="tool-a-host",
                        display_name="Tool Alpha Host",
                        provider_id="tool-a",
                        adapter_cls=StubAdapter,
                        default_target_dir=lambda: tmp_path / "tool-a",
                        detect=lambda: True,
                    ),
                ),
            ),
            ProviderPlugin(
                id="tool-b",
                display_name="Tool Beta",
                capabilities=ProviderCapabilities(planning=True),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                cli_executable="tool-b-cli",
                host_adapters=(
                    HostAdapterDescriptor(
                        id="tool-b-host",
                        display_name="Tool Beta Host",
                        provider_id="tool-b",
                        adapter_cls=StubAdapter,
                        default_target_dir=lambda: tmp_path / "tool-b",
                        detect=lambda: False,
                    ),
                ),
            ),
            ProviderPlugin(
                id="tool-c",
                display_name="Tool Gamma",
                capabilities=ProviderCapabilities(review=True),
                usage_source_kind=UsageSourceKind.API,
                cli_executable=None,
                host_adapters=(),
            ),
        )
    )


def test_detect_providers_shapes_and_connection_status(tmp_path):
    registry = _make_stub_registry(tmp_path)

    def fake_which(exe: str) -> str | None:
        if exe == "tool-a-cli":
            return "/bin/tool-a-cli"
        return None

    detections = detect_providers(which=fake_which, registry=registry)
    assert len(detections) == 3

    det_a = next(d for d in detections if d.provider_id == "tool-a")
    assert det_a.display_name == "Tool Alpha"
    assert det_a.cli_found is True
    assert det_a.cli_path == "/bin/tool-a-cli"
    assert det_a.hosts == ("tool-a-host",)
    assert det_a.adapter_installed is True
    assert det_a.to_dict()["cli_found"] is True

    det_b = next(d for d in detections if d.provider_id == "tool-b")
    assert det_b.cli_found is False
    assert det_b.cli_path is None
    assert det_b.adapter_installed is False

    det_c = next(d for d in detections if d.provider_id == "tool-c")
    assert det_c.cli_found is False
    assert det_c.hosts == ()
    assert det_c.adapter_installed is False


@pytest.mark.parametrize("layout", [
    ("OpenAI", "Codex", "bin", "desktop-version"),
    ("OpenAI", "Codex", "bin"),
    ("Programs", "OpenAI", "Codex", "bin"),
])
def test_detect_codex_in_shared_official_locations(tmp_path, monkeypatch, layout):
    from modelquad import codexcli

    official = tmp_path.joinpath(*layout) / "codex.exe"
    official.parent.mkdir(parents=True)
    official.write_bytes(b"")
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [official])

    monkeypatch.setattr(codexcli, "_probe", lambda _: "0.160.0")
    registry = ProviderRegistry((ProviderPlugin(
        id="codex", display_name="Codex", capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.LOCAL_LOGS, cli_executable="codex", host_adapters=(),
    ),))
    detected, = detect_providers(which=lambda _: None, registry=registry)
    assert detected.cli_found is True
    assert detected.cli_path == str(official)
    assert detected.on_path is False
    detected, = detect_providers(which=lambda _: "C:/npm/codex.cmd", registry=registry)
    assert detected.cli_path == "C:/npm/codex.cmd"
    assert detected.on_path is True


@pytest.mark.parametrize("layouts, expected", [
    ([('OpenAI', 'Codex', 'bin')], 0),
    ([('Programs', 'OpenAI', 'Codex', 'bin')], 0),
    ([('OpenAI', 'Codex', 'bin', '8aaf')], 0),
    ([('OpenAI', 'Codex', 'bin', '8aaf'), ('OpenAI', 'Codex', 'bin')], 1),
    ([('OpenAI', 'Codex', 'bin', '8aaf'), ('Programs', 'OpenAI', 'Codex', 'bin')], 1),
])
def test_detect_codex_with_unreadable_versions_still_finds_install(tmp_path, monkeypatch, layouts, expected):
    from modelquad import codexcli

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    candidates = [tmp_path.joinpath(*layout) / "codex.exe" for layout in layouts]
    for exe in candidates:
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"test")
        if os.name != "nt":
            exe.chmod(0o755)  # fallback admission needs an executable file on POSIX
    monkeypatch.setattr(codexcli, "official_candidates", lambda: candidates)
    monkeypatch.setattr(codexcli, "_probe", lambda _: None)
    registry = ProviderRegistry((ProviderPlugin(
        id="codex", display_name="Codex", capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.LOCAL_LOGS, cli_executable="codex", host_adapters=(),
    ),))
    assert codexcli.resolve_codex_cli(which=lambda _: None) is None
    detected, = detect_providers(which=lambda _: None, registry=registry)
    assert detected.cli_found is True
    assert detected.cli_path == str(candidates[expected])
    assert detected.on_path is False


def test_detect_codex_chooses_newest_desktop_version_and_reuses_probe(tmp_path, monkeypatch):
    import subprocess
    from modelquad import codexcli

    old = tmp_path / "OpenAI" / "Codex" / "bin" / "aaaa" / "codex.exe"
    new = old.parent.parent / "bbbb" / "codex.exe"
    for exe in (old, new):
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"test")
    # The directory listing encounters the older version first.
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [old, new])
    versions = {str(old): "0.155.0", str(new): "0.160.0"}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"codex-cli {versions[argv[0]]}\n", "")

    monkeypatch.setattr(codexcli.subprocess, "run", run)
    registry = ProviderRegistry((ProviderPlugin(
        id="codex", display_name="Codex", capabilities=ProviderCapabilities(),
        usage_source_kind=UsageSourceKind.LOCAL_LOGS, cli_executable="codex", host_adapters=(),
    ),))
    detected, = detect_providers(which=lambda _: None, registry=registry)
    chosen = codexcli.resolve_codex_cli(which=lambda _: None)
    detected_again, = detect_providers(which=lambda _: None, registry=registry)
    assert detected.cli_found is True
    assert detected.cli_path == detected_again.cli_path == chosen.path == str(new)
    assert detected.on_path is False
    assert calls == [[str(old), "--version"], [str(new), "--version"]]


def test_detect_providers_guards_detection_and_install_exceptions(tmp_path):
    class ExplodingAdapter:
        def is_installed(self, target_dir: Path) -> bool:
            raise OSError("Access denied to target")

        def install(self, target_dir: Path, *, dry_run: bool = False):
            raise OSError("Cannot write target")

    def exploding_detect():
        raise ValueError("Invalid detect environment")

    registry = ProviderRegistry(
        (
            ProviderPlugin(
                id="fragile-tool",
                display_name="Fragile Tool",
                capabilities=ProviderCapabilities(),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                cli_executable="fragile",
                host_adapters=(
                    HostAdapterDescriptor(
                        id="fragile-host",
                        display_name="Fragile Host",
                        provider_id="fragile-tool",
                        adapter_cls=ExplodingAdapter,
                        default_target_dir=lambda: tmp_path / "fragile",
                        detect=exploding_detect,
                    ),
                ),
            ),
        )
    )

    detections = detect_providers(which=lambda exe: "/bin/fragile", registry=registry)
    assert len(detections) == 1
    assert detections[0].adapter_installed is False


def test_run_onboarding_non_interactive_success(tmp_path, monkeypatch):
    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "onboard_test.db"

    # Stub run_doctor to return OK checks
    fake_report = DoctorReport(
        (
            DoctorCheck("tool-a CLI", CheckStatus.OK, "Found at /bin/tool-a-cli"),
            DoctorCheck("tool-a-host adapter installed", CheckStatus.OK, "Installed."),
        )
    )
    monkeypatch.setattr("modelquad.onboarding.run_doctor", lambda config, **kwargs: fake_report)

    output_lines: list[str] = []
    result = run_onboarding(
        selected_providers=["tool-a"],
        routing_mode="automatic",
        assume_yes=True,
        print_fn=output_lines.append,
        which=lambda exe: "/bin/tool-a-cli" if exe == "tool-a-cli" else None,
        registry=registry,
        db_path=db_path,
        dry_run=False,
    )

    assert result.ok is True
    assert result.routing_mode == "automatic"
    assert result.selected_providers == ("tool-a",)
    # The data-flow notice comes before anything is set up.
    from modelquad.disclosure import DATA_FLOW
    assert DATA_FLOW[0] in output_lines
    assert len(result.steps) >= 2
    assert any("Install tool-a-host adapter" in step.name for step in result.steps)
    assert any("Tool Alpha CLI check" in step.name for step in result.steps)
    assert any("Tool Alpha Host adapter verified" in step.name for step in result.steps)

    # Routing mode was persisted to DB
    assert service_history.get_routing_mode(db_path) == "automatic"

    # Check to_dict structure
    data = result.to_dict()
    assert data["ok"] is True
    assert data["routing_mode"] == "automatic"
    assert data["selected_providers"] == ["tool-a"]
    assert isinstance(data["steps"], list)
    assert len(data["steps"]) == len(result.steps)


def test_run_onboarding_unknown_provider_raises_value_error(tmp_path):
    registry = _make_stub_registry(tmp_path)

    with pytest.raises(ValueError, match="Unknown provider 'invalid-tool'"):
        run_onboarding(
            selected_providers=["invalid-tool"],
            registry=registry,
            assume_yes=True,
        )


def _stub_doctor(monkeypatch) -> None:
    fake_report = DoctorReport(
        (
            DoctorCheck("tool-a CLI", CheckStatus.OK, "Found"),
            DoctorCheck("tool-a-host adapter installed", CheckStatus.OK, "Installed"),
        )
    )
    monkeypatch.setattr("modelquad.onboarding.run_doctor", lambda config, **kwargs: fake_report)


def test_interactive_onboarding_never_asks_for_a_routing_mode(tmp_path, monkeypatch):
    """The last normal-user routing-mode picker in the product is gone.

    `ask` and `manual` are enforced
    cooperatively, so offering them promises an enforcement the product does
    not provide. The wizard still asks which tools the user has -- that is a
    real question -- and asks nothing about routing.
    """

    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "interactive_onboard.db"
    _stub_doctor(monkeypatch)

    prompts: list[str] = []

    def record_prompt(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    printed: list[str] = []
    result = run_onboarding(
        selected_providers=None,
        routing_mode=None,
        assume_yes=False,
        input_fn=record_prompt,
        print_fn=printed.append,
        which=lambda exe: "/bin/tool-a-cli" if exe == "tool-a-cli" else None,
        registry=registry,
        db_path=db_path,
        dry_run=False,
    )

    assert result.ok is True
    assert result.selected_providers == ("tool-a",)
    # Exactly one question, and it is not about routing.
    assert len(prompts) == 1
    combined = " ".join(prompts + printed).lower()
    for banned in ("automatic/ask/manual", "routing mode options", "choose routing mode"):
        assert banned not in combined
    assert "ask" not in " ".join(prompts).lower()

    # Automatic is established, not offered.
    assert result.routing_mode == "automatic"
    assert service_history.get_routing_mode(db_path) == "automatic"
    assert any("Welcome!" in line for line in printed)
    assert any("ModelQuad Routing: Active" in line for line in printed)


def test_first_run_establishes_automatic_without_being_asked(tmp_path, monkeypatch):
    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "first_run.db"
    _stub_doctor(monkeypatch)

    assert service_history.routing_mode_if_set(db_path) is None
    result = run_onboarding(
        selected_providers=["tool-a"],
        assume_yes=True,
        registry=registry,
        db_path=db_path,
    )
    assert result.routing_mode == "automatic"
    assert service_history.routing_mode_if_set(db_path) == "automatic"


def test_rerunning_setup_reads_legacy_mode_as_automatic(tmp_path, monkeypatch):
    """Upgrades read retired modes as automatic without rewriting saved rows."""

    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "rerun.db"
    _stub_doctor(monkeypatch)

    save_legacy_mode(db_path, "manual")

    result = run_onboarding(
        selected_providers=["tool-a"],
        assume_yes=True,
        registry=registry,
        db_path=db_path,
    )

    assert result.routing_mode == "automatic"
    assert service_history.get_routing_mode(db_path) == "automatic"


def test_retired_saved_mode_is_reported_as_automatic(tmp_path, monkeypatch):
    """Retired saved settings have automatic behavior."""

    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "reported.db"
    _stub_doctor(monkeypatch)
    save_legacy_mode(db_path, "ask")

    printed: list[str] = []
    run_onboarding(
        selected_providers=["tool-a"],
        assume_yes=True,
        print_fn=printed.append,
        registry=registry,
        db_path=db_path,
    )

    summary = " ".join(printed)
    assert "ModelQuad Routing: Active" in summary
    assert "'ask'" not in summary
    assert "--reset-routing" not in summary


def test_reset_routing_mode_restores_the_normal_beta_default(tmp_path, monkeypatch):
    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "reset.db"
    _stub_doctor(monkeypatch)
    save_legacy_mode(db_path, "manual")

    result = run_onboarding(
        selected_providers=["tool-a"],
        reset_routing_mode=True,
        assume_yes=True,
        registry=registry,
        db_path=db_path,
    )

    assert result.routing_mode == "automatic"
    assert service_history.get_routing_mode(db_path) == "automatic"


@pytest.mark.parametrize("mode", ["ask", "manual"])
def test_retired_explicit_mode_is_refused_including_qa(tmp_path, mode):
    from modelquad.onboarding import _resolve_routing_mode
    with pytest.raises(ValueError, match="choose one of: automatic"):
        _resolve_routing_mode(mode, tmp_path / "qa.db")


def test_invalid_explicit_routing_mode_still_raises(tmp_path, monkeypatch):
    registry = _make_stub_registry(tmp_path)
    _stub_doctor(monkeypatch)
    with pytest.raises(ValueError, match="Invalid routing mode"):
        run_onboarding(
            selected_providers=["tool-a"],
            routing_mode="turbo",
            assume_yes=True,
            registry=registry,
            db_path=tmp_path / "invalid.db",
        )


def test_run_onboarding_dry_run_skips_persistence_and_uses_dry_run_install(tmp_path, monkeypatch):
    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "dry_run.db"

    fake_report = DoctorReport(
        (
            DoctorCheck("tool-a CLI", CheckStatus.OK, "Found"),
            DoctorCheck("tool-a-host adapter installed", CheckStatus.OK, "Installed"),
        )
    )
    monkeypatch.setattr("modelquad.onboarding.run_doctor", lambda config, **kwargs: fake_report)

    StubAdapter.install_calls = []
    result = run_onboarding(
        selected_providers=["tool-a"],
        routing_mode="automatic",
        assume_yes=True,
        registry=registry,
        db_path=db_path,
        dry_run=True,
    )

    assert result.ok is True
    assert result.routing_mode == "automatic"
    # Adapter install was invoked with dry_run=True
    assert len(StubAdapter.install_calls) == 1
    assert StubAdapter.install_calls[0][1] is True

    # Database was not written: remains default "automatic"
    assert service_history.get_routing_mode(db_path) == "automatic"


def test_run_onboarding_doctor_failure_tolerance(tmp_path, monkeypatch):
    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "doctor_fail.db"

    def exploding_doctor(*args, **kwargs):
        raise RuntimeError("Doctor diagnostics crashed")

    monkeypatch.setattr("modelquad.onboarding.run_doctor", exploding_doctor)

    result = run_onboarding(
        selected_providers=["tool-a"],
        routing_mode="automatic",
        assume_yes=True,
        registry=registry,
        db_path=db_path,
        dry_run=False,
    )

    # The wizard must not crash; returns OnboardingResult with ok=False
    assert isinstance(result, OnboardingResult)
    assert result.ok is False
    diag_step = next(s for s in result.steps if s.name == "Verification diagnostics")
    assert diag_step.ok is False
    assert "Doctor diagnostics crashed" in diag_step.detail


def test_install_detected_hosts_skips_undetected_hosts(tmp_path):
    registry = _make_stub_registry(tmp_path)

    # tool-a-host is detected, tool-b-host is not detected
    results = install_detected_hosts(["tool-a-host", "tool-b-host"], dry_run=False, registry=registry)
    assert len(results) == 2

    res_a = next(r for r in results if r.host == "tool-a-host")
    assert res_a.detected is True
    assert res_a.error is None
    assert res_a.report is not None

    res_b = next(r for r in results if r.host == "tool-b-host")
    assert res_b.detected is False
    assert res_b.status is RegistrationStatus.NOT_DETECTED
    assert res_b.report is None
    assert res_b.error is None


def test_cli_onboard_parser_accepts_all_arguments():
    parser = cli_module.build_parser()
    args = parser.parse_args(["onboard", "--providers", "claude,codex", "--yes", "--dry-run", "--json"])
    assert args.command == "onboard"
    assert args.providers == "claude,codex"
    # The routing-mode flag left with the `mode` command.
    assert not hasattr(args, "mode")
    with pytest.raises(SystemExit):
        parser.parse_args(["onboard", "--mode", "automatic"])
    assert args.assume_yes is True
    assert args.dry_run is True
    assert args.as_json is True


def test_cli_onboard_json_command(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "cli_onboard.db"
    fake_report = DoctorReport(
        (
            DoctorCheck("claude CLI", CheckStatus.OK, "Found"),
            DoctorCheck("claude adapter installed", CheckStatus.OK, "Installed"),
        )
    )
    monkeypatch.setattr("modelquad.onboarding.run_doctor", lambda config, **kwargs: fake_report)

    exit_code = main(
        [
            "--history-db",
            str(db_path),
            "onboard",
            "--providers",
            "claude",
            "--yes",
            "--dry-run",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["routing_mode"] == "automatic"
    assert payload["selected_providers"] == ["claude"]
    assert isinstance(payload["steps"], list)


def test_an_unselected_provider_is_not_installed_and_does_not_fail_setup(tmp_path, monkeypatch):
    """Choosing one tool must not install another, or make the product look broken.

    A Beta user with only Claude should finish setup successfully. If an
    unselected, absent tool contributed a failing step, ModelQuad would report
    itself as broken for declining to use software the user does not have.
    """

    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "selection.db"

    # Doctor is honest about tool-b being missing; setup must simply not care.
    fake_report = DoctorReport(
        (
            DoctorCheck("tool-a CLI", CheckStatus.OK, "Found"),
            DoctorCheck("tool-a-host adapter installed", CheckStatus.OK, "Installed"),
            DoctorCheck("tool-b CLI", CheckStatus.MISSING, "Install Tool Beta CLI"),
            DoctorCheck("tool-b-host adapter installed", CheckStatus.MISSING, "Not installed"),
        )
    )
    monkeypatch.setattr("modelquad.onboarding.run_doctor", lambda config, **kwargs: fake_report)

    StubAdapter.install_calls = []
    result = run_onboarding(
        selected_providers=["tool-a"],
        assume_yes=True,
        registry=registry,
        db_path=db_path,
    )

    assert result.ok is True, "an unselected, absent tool must not fail setup"
    assert result.selected_providers == ("tool-a",)
    installed_targets = [str(target) for target, _dry in StubAdapter.install_calls]
    assert all("tool-b" not in target for target in installed_targets)
    assert not any("Tool Beta" in step.name for step in result.steps)


def test_selecting_nothing_is_allowed_and_still_establishes_routing(tmp_path, monkeypatch):
    """A user may install ModelQuad before installing any AI tool."""

    registry = _make_stub_registry(tmp_path)
    db_path = tmp_path / "nothing.db"
    monkeypatch.setattr(
        "modelquad.onboarding.run_doctor",
        lambda config, **kwargs: DoctorReport(()),
    )

    StubAdapter.install_calls = []
    result = run_onboarding(
        selected_providers=[],
        assume_yes=True,
        registry=registry,
        db_path=db_path,
    )

    assert result.selected_providers == ()
    assert StubAdapter.install_calls == []
    assert result.ok is True
    assert service_history.get_routing_mode(db_path) == "automatic"


# --------------------------------------------------------------------------
# One authority for "connected"
#
# This wizard used to answer the question itself, from
# `ProviderDetection.adapter_installed`. That field means "every detected host
# adapter reports itself installed", which for Codex is satisfied by a managed
# block in AGENTS.md -- on a machine with no Codex CLI. So the wizard printed
# "Connected" next to a tool that was not installed, and printed "Not
# connected" next to a provider it cannot connect at all.
#
# The tests below drive `run_onboarding` and compare its output against
# `describe_connections`, so the two cannot drift apart again. They use the
# stub registry rather than the real one: the real providers' state is a
# property of the machine the suite runs on, and an assertion about the
# developer's own `~/.claude` is not one CI can reproduce.
# --------------------------------------------------------------------------


def _onboard_lines(registry, tmp_path, **kwargs) -> list[str]:
    """Run the wizard with nothing on PATH and return what it printed."""

    printed: list[str] = []
    run_onboarding(
        selected_providers=(),
        assume_yes=True,
        db_path=tmp_path / "history.db",
        which=lambda _name: None,
        registry=registry,
        print_fn=lambda *args, **_kw: printed.append(" ".join(str(a) for a in args)),
        dry_run=True,
        **kwargs,
    )
    return printed


def _tool_lines(printed: list[str]) -> dict[str, str]:
    """The detection-table rows, keyed by display name."""

    rows = {}
    for line in printed:
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            name, _, status = stripped[2:].partition(":")
            rows[name.strip()] = status.strip()
    return rows


def test_onboarding_does_not_call_a_tool_connected_when_its_cli_is_missing(tmp_path):
    """The Codex shape: our adapter is installed, the vendor's CLI is not.

    `StubAdapter.is_installed` returns True while `which` finds nothing, which
    is exactly the state that produced "CLI Not found | Connected".
    """

    registry = _make_stub_registry(tmp_path)
    StubAdapter.installed = True

    rows = _tool_lines(_onboard_lines(registry, tmp_path))

    assert "Connected" not in rows["Tool Alpha"]
    assert rows["Tool Alpha"] == "Not installed"


def test_onboarding_does_not_present_an_unmanageable_provider_as_connectable(tmp_path):
    """A provider with no CLI and no host integration: nothing to connect.

    "Not connected" is a claim about a tool that could be connected. Saying it
    about one the wizard cannot touch sends the user looking for an action
    that does not exist.
    """

    registry = _make_stub_registry(tmp_path)
    rows = _tool_lines(_onboard_lines(registry, tmp_path))

    assert "Not connected" not in rows["Tool Gamma"]
    assert "does not manage it here" in rows["Tool Gamma"]


def test_onboarding_does_not_default_to_selecting_an_unmanageable_provider(tmp_path):
    """A provider with no CLI is never in the default set, so `--yes` skips it."""

    registry = _make_stub_registry(tmp_path)
    chosen: list[str] = []
    run_onboarding(
        assume_yes=True,
        db_path=tmp_path / "history.db",
        which=lambda name: "/bin/tool-a-cli" if name == "tool-a-cli" else None,
        registry=registry,
        print_fn=lambda *_a, **_k: None,
        dry_run=True,
        input_fn=lambda _prompt: chosen.append("prompted") or "",
    )
    # Nothing was prompted for, and tool-c cannot have been picked up: it has
    # no CLI, so it is not in `connectable`.
    assert chosen == []


def test_onboarding_rows_are_exactly_the_provider_connection_headlines(tmp_path):
    """The anti-drift test: the wizard renders the authority, it does not paraphrase it."""

    registry = _make_stub_registry(tmp_path)
    rows = _tool_lines(_onboard_lines(registry, tmp_path))

    connections = describe_connections(which=lambda _name: None, registry=registry)
    for connection in connections:
        if connection.install is InstallState.NOT_APPLICABLE:
            assert "does not manage it here" in rows[connection.display_name]
        else:
            assert rows[connection.display_name] == connection.headline


def test_onboarding_never_prints_a_host_config_path_or_mcp_internals(tmp_path):
    """The rule against printing host internals, kept true for this surface too."""

    registry = _make_stub_registry(tmp_path)
    printed = "\n".join(_onboard_lines(registry, tmp_path))

    for leak in (".claude.json", "config.toml", "mcp_config.json", "mcpServers", "adapter"):
        assert leak not in printed


def test_claude_and_codex_keep_their_own_stronger_evidence(tmp_path):
    """Claude and Codex sign-in can be answered from their own evidence, and is."""

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

    assert probe_sign_in("claude", home=home).state is SignInState.SIGNED_IN
    assert probe_sign_in("codex", home=home).state is SignInState.SIGNED_IN
