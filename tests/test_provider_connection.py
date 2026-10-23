# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for the four-fact provider connection model.

Every provider is audited independently here on purpose. The failure this
suite exists to prevent is *false symmetry* -- giving one provider another
provider's sign-in semantics because the code was easier to write that way -- so the
per-provider cases are deliberately not parameterised into one loop.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from modelquad.provider_connection import (
    ACTION_CONNECT,
    ACTION_FIX_PATH,
    ACTION_INSTALL,
    ACTION_REPAIR,
    ACTION_SIGN_IN,
    ConnectionState,
    HostIntegration,
    InstallState,
    IntegrationState,
    ProviderConnection,
    SignInEvidence,
    SignInState,
    describe_connections,
    needs_attention,
    probe_sign_in,
)
from modelquad.registry import (
    HostAdapterDescriptor,
    ProviderCapabilities,
    ProviderPlugin,
    ProviderRegistry,
    UsageSourceKind,
)


class StubAdapter:
    """Minimal adapter stand-in; connection inspection never installs anything."""

    def __init__(self) -> None:
        pass

    def is_installed(self, target_dir: Path) -> bool:
        return False


def _plugin(provider_id: str, display_name: str, *, cli: str | None, target: Path) -> ProviderPlugin:
    return ProviderPlugin(
        id=provider_id,
        display_name=display_name,
        capabilities=ProviderCapabilities(coding=True),
        usage_source_kind=UsageSourceKind.LOCAL_LOGS,
        cli_executable=cli,
        host_adapters=(
            (
                HostAdapterDescriptor(
                    id=provider_id,
                    display_name=f"{display_name} CLI",
                    provider_id=provider_id,
                    adapter_cls=StubAdapter,
                    default_target_dir=lambda: target,
                    detect=lambda: True,
                ),
            )
            if cli is not None
            else ()
        ),
    )


def _registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(
        (
            _plugin("claude", "Claude", cli="claude", target=tmp_path / "claude"),
            _plugin("codex", "Codex", cli="codex", target=tmp_path / "codex"),
        )
    )


def _which_none(_name: str) -> str | None:
    return None


def _which_all(name: str) -> str | None:
    return f"C:\\tools\\{name}.exe"


@pytest.fixture
def stub_hostruntime(monkeypatch):
    """Control what each host is recorded as launching, without touching disk."""

    state: dict[str, object] = {"commands": {}, "canonical": set(), "raise_on": set()}

    def fake_read(host_id: str, target_dir: Path):
        if host_id in state["raise_on"]:
            raise OSError("simulated unreadable host configuration")
        return state["commands"].get(host_id)

    class _Verdict:
        def __init__(self, command: str, canonical: bool) -> None:
            self.command = command
            self.resolved = command
            self.canonical = canonical
            self.reason = "" if canonical else f"The host launches {command}, but installed ModelQuad is elsewhere."

    def fake_classify(command: str, *, which=None):
        return _Verdict(command, command in state["canonical"])

    monkeypatch.setattr("modelquad.hostruntime.read_registered_command", fake_read)
    monkeypatch.setattr("modelquad.hostruntime.classify_registration", fake_classify)
    return state


# --------------------------------------------------------------------------
# Sign-in: Claude
# --------------------------------------------------------------------------


def test_claude_sign_in_true_when_credential_file_present(tmp_path):
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}", encoding="utf-8")

    evidence = probe_sign_in("claude", home=tmp_path)
    assert evidence.state is SignInState.SIGNED_IN


def test_claude_sign_in_false_on_windows_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("modelquad.provider_connection.sys.platform", "win32")
    evidence = probe_sign_in("claude", home=tmp_path)
    assert evidence.state is SignInState.SIGNED_OUT


def test_claude_sign_in_unknown_off_windows_because_of_the_keychain(tmp_path, monkeypatch):
    """Absence is not evidence on a platform that may use the system keychain."""

    monkeypatch.setattr("modelquad.provider_connection.sys.platform", "darwin")
    evidence = probe_sign_in("claude", home=tmp_path)
    assert evidence.state is SignInState.UNKNOWN
    assert "keychain" in evidence.detail.lower()


# --------------------------------------------------------------------------
# Sign-in: Codex
# --------------------------------------------------------------------------


def test_codex_sign_in_true_when_credential_file_present(tmp_path):
    creds = tmp_path / ".codex" / "auth.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}", encoding="utf-8")

    evidence = probe_sign_in("codex", home=tmp_path)
    assert evidence.state is SignInState.SIGNED_IN


def test_codex_sign_in_false_when_absent_because_logout_removes_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    evidence = probe_sign_in("codex", home=tmp_path)
    assert evidence.state is SignInState.SIGNED_OUT


def test_codex_api_key_mode_is_signed_in_and_never_echoes_the_key(tmp_path, monkeypatch):
    secret = "sk-do-not-leak-this-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    evidence = probe_sign_in("codex", home=tmp_path)
    assert evidence.state is SignInState.SIGNED_IN
    assert secret not in evidence.summary
    assert secret not in evidence.detail
    assert secret not in json.dumps(evidence.to_dict())


def test_codex_blank_api_key_is_not_a_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    assert probe_sign_in("codex", home=tmp_path).state is SignInState.SIGNED_OUT


def test_unregistered_provider_probe_is_unknown_not_a_guess(tmp_path):
    assert probe_sign_in("some-future-tool", home=tmp_path).state is SignInState.UNKNOWN


# --------------------------------------------------------------------------
# Install detection
# --------------------------------------------------------------------------


def test_install_on_path(tmp_path, stub_hostruntime):
    connections = describe_connections(
        which=_which_all, registry=_registry(tmp_path), home=tmp_path
    )
    claude = next(c for c in connections if c.provider_id == "claude")
    assert claude.install is InstallState.INSTALLED_ON_PATH
    assert claude.install_path is not None


def test_install_not_found(tmp_path, stub_hostruntime):
    connections = describe_connections(
        which=_which_none, registry=_registry(tmp_path), home=tmp_path
    )
    for connection in connections:
        assert connection.install is InstallState.NOT_INSTALLED
        assert connection.state is ConnectionState.NOT_INSTALLED
        assert connection.action == ACTION_INSTALL


def test_install_off_path_is_reported_separately_from_missing(tmp_path, stub_hostruntime, monkeypatch):
    """A vendor installer that skips PATH must not be reported as "not installed"."""

    from modelquad import onboarding as onboarding_module

    real_detect = onboarding_module.detect_providers

    def detect_off_path(*, which, registry):
        detections = real_detect(which=_which_all, registry=registry)
        return tuple(
            dataclasses.replace(d, cli_found=True, on_path=False) for d in detections
        )

    monkeypatch.setattr(onboarding_module, "detect_providers", detect_off_path)

    connections = describe_connections(
        which=_which_all, registry=_registry(tmp_path), home=tmp_path
    )
    claude = next(c for c in connections if c.provider_id == "claude")
    assert claude.install is InstallState.INSTALLED_OFF_PATH
    assert claude.state is ConnectionState.NEEDS_PATH_FIX
    assert claude.action == ACTION_FIX_PATH


def test_provider_without_a_cli_is_not_applicable_rather_than_missing(tmp_path, stub_hostruntime):
    registry = ProviderRegistry((_plugin("example", "Example", cli=None, target=tmp_path / "example"),))
    connections = describe_connections(which=_which_none, registry=registry, home=tmp_path)
    assert connections[0].install is InstallState.NOT_APPLICABLE
    assert connections[0].state is not ConnectionState.NOT_INSTALLED


@pytest.mark.parametrize("signed_in, integration, expected_state, expected_action", [
    (False, IntegrationState.CANONICAL, ConnectionState.NEEDS_SIGN_IN, ACTION_SIGN_IN),
    (True, IntegrationState.NOT_REGISTERED, ConnectionState.NEEDS_CONNECT, ACTION_CONNECT),
    (True, IntegrationState.WRONG_RUNTIME, ConnectionState.NEEDS_REPAIR, ACTION_REPAIR),
    (True, IntegrationState.CANONICAL, ConnectionState.READY, None),
    (True, IntegrationState.UNKNOWN, ConnectionState.UNKNOWN, None),
])
def test_hashed_codex_connection_skips_path_fix(
    tmp_path, stub_hostruntime, monkeypatch, capsys,
    signed_in, integration, expected_state, expected_action,
):
    from argparse import Namespace
    from modelquad import cli, codexcli, onboarding, provider_connection

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    exe = tmp_path / "OpenAI" / "Codex" / "bin" / "8aaf" / "codex.exe"
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [exe])
    monkeypatch.setattr(codexcli, "_probe", lambda _: "0.160.0")
    if signed_in:
        _signed_in_home(tmp_path)
    if integration is IntegrationState.CANONICAL:
        stub_hostruntime["commands"] = {"codex": "installed-runtime"}
        stub_hostruntime["canonical"] = {"installed-runtime"}
    elif integration is IntegrationState.WRONG_RUNTIME:
        stub_hostruntime["commands"] = {"codex": "other-runtime"}
    elif integration is IntegrationState.UNKNOWN:
        stub_hostruntime["raise_on"] = {"codex"}

    registry = _registry(tmp_path)
    detections = onboarding.detect_providers(which=_which_none, registry=registry)
    detected = next(d for d in detections if d.provider_id == "codex")
    assert detected.cli_found is True
    assert detected.on_path is False
    connection, = describe_connections(
        which=_which_none, registry=registry, home=tmp_path, selected_providers=["codex"],
    )
    assert connection.install is InstallState.INSTALLED_OFF_PATH
    assert connection.install_path == str(exe)
    assert connection.state is expected_state
    assert connection.action == expected_action
    assert connection.state is not ConnectionState.NEEDS_PATH_FIX
    assert connection.action != ACTION_FIX_PATH

    monkeypatch.setattr(onboarding, "detect_providers", lambda: detections)
    monkeypatch.setattr(provider_connection, "describe_connections", lambda: (connection,))
    assert cli._run_tools_command(Namespace(fix_path=False, as_json=False)) == 0
    output = capsys.readouterr().out
    assert str(exe) in output
    assert "Fix it:" not in output
    assert "tools --fix-path" not in output


@pytest.mark.parametrize("layout", [
    ("OpenAI", "Codex", "bin"),
    ("Programs", "OpenAI", "Codex", "bin"),
])
def test_stable_off_path_codex_connection_keeps_path_fix(
    tmp_path, stub_hostruntime, monkeypatch, capsys, layout,
):
    from argparse import Namespace
    from modelquad import cli, codexcli, onboarding, provider_connection

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    exe = tmp_path.joinpath(*layout) / "codex.exe"
    monkeypatch.setattr(codexcli, "official_candidates", lambda: [exe])
    monkeypatch.setattr(codexcli, "_probe", lambda _: "0.160.0")
    registry = _registry(tmp_path)
    detections = onboarding.detect_providers(which=_which_none, registry=registry)
    connection, = describe_connections(
        which=_which_none, registry=registry, home=tmp_path, selected_providers=["codex"],
    )
    assert connection.install is InstallState.INSTALLED_OFF_PATH
    assert connection.install_path == str(exe)
    assert connection.state is ConnectionState.NEEDS_PATH_FIX
    assert connection.action == ACTION_FIX_PATH

    monkeypatch.setattr(onboarding, "detect_providers", lambda: detections)
    monkeypatch.setattr(provider_connection, "describe_connections", lambda: (connection,))
    assert cli._run_tools_command(Namespace(fix_path=False, as_json=False)) == 0
    output = capsys.readouterr().out
    assert "Fix it:" in output
    assert "tools --fix-path" in output


# --------------------------------------------------------------------------
# Integration state
# --------------------------------------------------------------------------


def _signed_in_home(tmp_path: Path) -> Path:
    for relative in (
        (".claude", ".credentials.json"),
        (".codex", "auth.json"),
    ):
        path = tmp_path.joinpath(*relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    return tmp_path


def test_integration_canonical_reads_ready(tmp_path, stub_hostruntime):
    home = _signed_in_home(tmp_path)
    stub_hostruntime["commands"] = {
        "claude": "C:\\installed\\modelquad-mcp.cmd",
        "codex": "C:\\installed\\modelquad-mcp.cmd",
    }
    stub_hostruntime["canonical"] = {"C:\\installed\\modelquad-mcp.cmd"}

    connections = describe_connections(which=_which_all, registry=_registry(tmp_path), home=home)
    for connection in connections:
        assert connection.state is ConnectionState.READY
        assert connection.action is None
        assert connection.headline == "Connected"


def test_integration_wrong_runtime_reads_needs_repair(tmp_path, stub_hostruntime):
    home = _signed_in_home(tmp_path)
    stub_hostruntime["commands"] = {"claude": "modelquad-mcp"}
    stub_hostruntime["canonical"] = set()

    connections = describe_connections(which=_which_all, registry=_registry(tmp_path), home=home)
    claude = next(c for c in connections if c.provider_id == "claude")
    assert claude.integrations[0].state is IntegrationState.WRONG_RUNTIME
    assert claude.state is ConnectionState.NEEDS_REPAIR
    assert claude.action == ACTION_REPAIR


def test_integration_absent_reads_needs_connect(tmp_path, stub_hostruntime):
    home = _signed_in_home(tmp_path)
    stub_hostruntime["commands"] = {}

    connections = describe_connections(which=_which_all, registry=_registry(tmp_path), home=home)
    claude = next(c for c in connections if c.provider_id == "claude")
    assert claude.integrations[0].state is IntegrationState.NOT_REGISTERED
    assert claude.state is ConnectionState.NEEDS_CONNECT
    assert claude.action == ACTION_CONNECT


def test_unreadable_host_configuration_is_a_state_not_an_exception(tmp_path, stub_hostruntime):
    home = _signed_in_home(tmp_path)
    stub_hostruntime["raise_on"] = {"claude", "codex"}

    connections = describe_connections(which=_which_all, registry=_registry(tmp_path), home=home)
    for connection in connections:
        assert connection.integrations[0].state is IntegrationState.UNKNOWN
        assert connection.state is ConnectionState.UNKNOWN
        assert connection.action is None


# --------------------------------------------------------------------------
# Precedence and UNKNOWN handling
# --------------------------------------------------------------------------


def test_sign_out_outranks_registration_problems(tmp_path, stub_hostruntime, monkeypatch):
    """Signing in comes first: connecting a signed-out tool solves nothing."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch_home = tmp_path
    (monkeypatch_home / ".codex").mkdir(parents=True, exist_ok=True)
    stub_hostruntime["commands"] = {"codex": "modelquad-mcp"}

    connections = describe_connections(
        which=_which_all, registry=_registry(tmp_path), home=monkeypatch_home
    )
    codex = next(c for c in connections if c.provider_id == "codex")
    assert codex.state is ConnectionState.NEEDS_SIGN_IN
    assert codex.action == ACTION_SIGN_IN


def test_not_installed_outranks_everything_else(tmp_path, stub_hostruntime):
    stub_hostruntime["commands"] = {"claude": "modelquad-mcp"}
    connections = describe_connections(
        which=_which_none, registry=_registry(tmp_path), home=tmp_path
    )
    claude = next(c for c in connections if c.provider_id == "claude")
    assert claude.state is ConnectionState.NOT_INSTALLED
    assert claude.action == ACTION_INSTALL


def test_needs_attention_omits_healthy_and_unknown(tmp_path, stub_hostruntime):
    home = _signed_in_home(tmp_path)
    stub_hostruntime["commands"] = {
        "claude": "C:\\installed\\modelquad-mcp.cmd",
        "codex": "modelquad-mcp",
    }
    stub_hostruntime["canonical"] = {"C:\\installed\\modelquad-mcp.cmd"}

    connections = describe_connections(which=_which_all, registry=_registry(tmp_path), home=home)
    flagged = {c.provider_id for c in needs_attention(connections)}
    assert "codex" in flagged
    assert "claude" not in flagged


# --------------------------------------------------------------------------
# Selection and serialisation
# --------------------------------------------------------------------------


def test_selected_providers_filters(tmp_path, stub_hostruntime):
    connections = describe_connections(
        which=_which_all,
        registry=_registry(tmp_path),
        home=tmp_path,
        selected_providers=["codex"],
    )
    assert [c.provider_id for c in connections] == ["codex"]


def test_unknown_selected_provider_names_the_valid_choices(tmp_path, stub_hostruntime):
    with pytest.raises(ValueError, match="valid choices are"):
        describe_connections(
            which=_which_all,
            registry=_registry(tmp_path),
            home=tmp_path,
            selected_providers=["nope"],
        )


def test_to_dict_is_json_serialisable(tmp_path, stub_hostruntime):
    home = _signed_in_home(tmp_path)
    stub_hostruntime["commands"] = {"claude": "modelquad-mcp"}

    connections = describe_connections(which=_which_all, registry=_registry(tmp_path), home=home)
    encoded = json.dumps([c.to_dict() for c in connections], sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded[0]["provider_id"] == "claude"
    assert isinstance(decoded[0]["sign_in"]["state"], str)
    assert isinstance(decoded[0]["integrations"], list)


# --------------------------------------------------------------------------
# The language guard
# --------------------------------------------------------------------------


#: Vocabulary a normal-user string may never contain. The product bans these
#: from primary UI; this test is what stops the ban being merely
#: intended. "PATH" is here because the product says "Windows cannot find it
#: yet" instead.
BANNED_TERMS = (
    "mcp",
    "json",
    "sqlite",
    "path",
    "state root",
    "editable",
    "runtime identity",
    "host adapter",
    "adapter",
    "registration",
    "registry",
    "traceback",
    "exception",
    "schema",
    "stdio",
    "subprocess",
    "oauth",
    "config",
)


def _all_fixture_connections(tmp_path, stub_hostruntime) -> list[ProviderConnection]:
    """Every connection shape this module can emit, across a fixture matrix."""

    produced: list[ProviderConnection] = []
    homes = {
        "empty": tmp_path / "empty",
        "signed-in": _signed_in_home(tmp_path / "full"),
    }
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)

    command_sets = (
        {},
        {"claude": "modelquad-mcp", "codex": "modelquad-mcp"},
        {
            "claude": "C:\\installed\\modelquad-mcp.cmd",
            "codex": "C:\\installed\\modelquad-mcp.cmd",
            },
    )
    for which in (_which_all, _which_none):
        for commands in command_sets:
            for home in homes.values():
                stub_hostruntime["commands"] = commands
                stub_hostruntime["canonical"] = {"C:\\installed\\modelquad-mcp.cmd"}
                produced.extend(
                    describe_connections(which=which, registry=_registry(tmp_path), home=home)
                )

    stub_hostruntime["raise_on"] = {"claude", "codex"}
    produced.extend(
        describe_connections(which=_which_all, registry=_registry(tmp_path), home=tmp_path)
    )
    stub_hostruntime["raise_on"] = set()
    return produced


def test_normal_user_copy_never_leaks_internal_vocabulary(tmp_path, stub_hostruntime):
    for connection in _all_fixture_connections(tmp_path, stub_hostruntime):
        for label, text in (
            ("headline", connection.headline),
            ("next_step", connection.next_step),
            ("sign_in.summary", connection.sign_in.summary),
        ):
            lowered = text.lower()
            for term in BANNED_TERMS:
                assert term not in lowered, (
                    f"{connection.provider_id} {label} leaks internal vocabulary "
                    f"{term!r}: {text!r}"
                )


def test_normal_user_copy_never_contains_a_file_path(tmp_path, stub_hostruntime):
    for connection in _all_fixture_connections(tmp_path, stub_hostruntime):
        for text in (connection.headline, connection.next_step, connection.sign_in.summary):
            assert "\\" not in text
            assert "/" not in text
            assert ".json" not in text


def test_every_actionable_state_offers_exactly_one_action(tmp_path, stub_hostruntime):
    for connection in _all_fixture_connections(tmp_path, stub_hostruntime):
        if connection.state in {
            ConnectionState.NOT_INSTALLED,
            ConnectionState.NEEDS_PATH_FIX,
            ConnectionState.NEEDS_SIGN_IN,
            ConnectionState.NEEDS_REPAIR,
            ConnectionState.NEEDS_CONNECT,
        }:
            assert connection.action is not None
        else:
            assert connection.action is None


def test_dataclasses_are_frozen():
    evidence = SignInEvidence(SignInState.UNKNOWN, "a", "b")
    with pytest.raises(Exception):
        evidence.state = SignInState.SIGNED_IN  # type: ignore[misc]

    integration = HostIntegration("h", "H", IntegrationState.UNKNOWN, "d")
    with pytest.raises(Exception):
        integration.state = IntegrationState.CANONICAL  # type: ignore[misc]
