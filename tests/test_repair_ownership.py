# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""What repair is allowed to touch, and what it must leave exactly as it found it.

``tests/test_repair.py`` covers the orchestration -- which doctor findings map
to which host, what gets skipped, what the dry run does. This file covers the
harder promise underneath it: repair edits **only the entry ModelQuad owns**,
in a file that belongs to somebody else.

That promise is why repair can be offered to a normal user as a button. If
repairing ModelQuad could disturb an unrelated MCP server, a hook the user
wrote, or a development checkout, "Repair" would be a destructive action
wearing a friendly label.

Every test here drives the **real** adapters against fixture configuration in
``tmp_path``. Nothing in this file reads or writes the developer's own
``~/.claude`` or ``~/.codex``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from orchestrator.adapters.claude_adapter import ClaudeAdapter
from orchestrator.adapters.report import ActionKind
from orchestrator.branding import (
    LEGACY_MCP_SERVER_TOML_KEY,
    MANAGED_BLOCK_TAG,
    MCP_COMMAND,
    MCP_SERVER_NAME,
    MCP_SERVER_TOML_KEY,
)
from orchestrator.hostruntime import read_registered_command


#: The absolute launcher an installed ModelQuad registers. Repair's whole job
#: is to move a stale registration onto this.
INSTALLED_SHIM = "C:\\Users\\Someone\\AppData\\Local\\MultiAgentOrchestrator\\bin\\modelquad-mcp.cmd"

#: What P0-A found live on 2026-08-26: a bare name that resolved through PATH
#: to an editable checkout while the installed product used another database.
STALE_BARE = "modelquad-mcp"
STALE_EDITABLE = "C:\\Python311\\Scripts\\modelquad-mcp.exe"


@pytest.fixture
def installed_runtime(monkeypatch):
    """Make the adapters believe they belong to an installed product."""

    monkeypatch.setattr(
        "orchestrator.hostruntime.mcp_launch_command", lambda: INSTALLED_SHIM
    )
    return INSTALLED_SHIM


# --------------------------------------------------------------------------
# Fixtures that look like a real user's machine, not an empty directory.
# --------------------------------------------------------------------------


def _claude_home(tmp_path: Path, modelquad_command: str | None) -> Path:
    """A ``~/.claude`` carrying other people's work alongside ours."""

    home = tmp_path / ".claude"
    home.mkdir(parents=True, exist_ok=True)

    servers = {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        "postgres": {"command": "/usr/local/bin/pg-mcp", "args": ["--readonly"]},
    }
    if modelquad_command is not None:
        servers[MCP_SERVER_NAME] = {"command": modelquad_command, "args": ["--stdio"]}

    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": servers,
                "theme": "dark",
                "hasCompletedOnboarding": True,
                "projects": {"C:\\work\\thing": {"allowedTools": ["Bash(git *)"]}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (home / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "env": {"MY_OWN_VAR": "keep-me"},
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Edit", "hooks": [{"type": "command", "command": "my-formatter"}]}
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return home


def _claude_servers(tmp_path: Path) -> dict:
    payload = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    return payload["mcpServers"]


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stale", [STALE_BARE, STALE_EDITABLE])
def test_claude_repair_moves_a_stale_registration_onto_the_installed_runtime(
    tmp_path, installed_runtime, stale
):
    home = _claude_home(tmp_path, stale)
    ClaudeAdapter().install(home)

    assert _claude_servers(tmp_path)[MCP_SERVER_NAME]["command"] == INSTALLED_SHIM


def test_claude_repair_preserves_unrelated_mcp_servers(tmp_path, installed_runtime):
    home = _claude_home(tmp_path, STALE_BARE)
    before = _claude_servers(tmp_path)
    github_before = dict(before["github"])
    postgres_before = dict(before["postgres"])

    ClaudeAdapter().install(home)

    after = _claude_servers(tmp_path)
    assert after["github"] == github_before
    assert after["postgres"] == postgres_before


def test_claude_repair_preserves_unrelated_host_configuration(tmp_path, installed_runtime):
    home = _claude_home(tmp_path, STALE_BARE)
    ClaudeAdapter().install(home)

    payload = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert payload["theme"] == "dark"
    assert payload["hasCompletedOnboarding"] is True
    assert payload["projects"]["C:\\work\\thing"]["allowedTools"] == ["Bash(git *)"]

    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "opus"
    # The user's own environment entry survives, and ModelQuad adds none of
    # its own: its tool timeout lives on the modelquad server entry.
    assert settings["env"] == {"MY_OWN_VAR": "keep-me"}
    post = settings["hooks"]["PostToolUse"]
    assert post == [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "my-formatter"}]}
    ]


def test_claude_repair_is_idempotent_byte_for_byte(tmp_path, installed_runtime):
    """The second repair must change nothing at all.

    Byte-identical rather than merely "equivalent JSON": a repair that
    reserialises a file every time it runs produces a diff in the user's
    configuration on every doctor warning, and looks like damage in a
    dotfiles repository even when the content is unchanged.
    """

    home = _claude_home(tmp_path, STALE_BARE)
    ClaudeAdapter().install(home)

    config = tmp_path / ".claude.json"
    settings = home / "settings.json"
    first_config = config.read_bytes()
    first_settings = settings.read_bytes()

    second = ClaudeAdapter().install(home)

    assert config.read_bytes() == first_config
    assert settings.read_bytes() == first_settings
    assert all(action.kind.value == "unchanged" for action in second.actions), [
        (str(a.path), a.kind.value) for a in second.actions
    ]


def test_claude_repair_when_no_registration_exists_only_adds_ours(tmp_path, installed_runtime):
    home = _claude_home(tmp_path, None)
    ClaudeAdapter().install(home)

    servers = _claude_servers(tmp_path)
    assert servers[MCP_SERVER_NAME]["command"] == INSTALLED_SHIM
    assert set(servers) == {"github", "postgres", MCP_SERVER_NAME}


# --------------------------------------------------------------------------
# The development checkout must survive repair on a developer's machine
# --------------------------------------------------------------------------


def test_repair_never_touches_a_development_checkout_or_its_database(tmp_path, monkeypatch):
    """Repair rewrites host *registrations*. It does not delete anything.

    A developer's machine legitimately carries a second, development state
    store; ``statediag`` reports the split and deliberately refuses to
    reconcile it, because "copying either direction destroys real settings".
    Repair inherits that rule. This pins it: after a repair, the development
    checkout and its database are still there, untouched.
    """

    dev_root = tmp_path / "dev-state"
    dev_root.mkdir()
    dev_db = dev_root / "orchestrator.db"
    dev_db.write_bytes(b"development database contents")
    dev_checkout = tmp_path / "checkout" / "src" / "orchestrator"
    dev_checkout.mkdir(parents=True)
    (dev_checkout / "__init__.py").write_text("# dev checkout", encoding="utf-8")

    monkeypatch.setattr(
        "orchestrator.hostruntime.mcp_launch_command", lambda: INSTALLED_SHIM
    )
    home = _claude_home(tmp_path, STALE_EDITABLE)
    ClaudeAdapter().install(home)

    assert dev_db.read_bytes() == b"development database contents"
    assert (dev_checkout / "__init__.py").read_text(encoding="utf-8") == "# dev checkout"
    assert _claude_servers(tmp_path)[MCP_SERVER_NAME]["command"] == INSTALLED_SHIM


# --------------------------------------------------------------------------
# Repair, run twice, at the orchestration level
# --------------------------------------------------------------------------


def test_running_repair_twice_repairs_nothing_the_second_time(tmp_path, monkeypatch):
    """The second run is a no-op because the first one actually fixed it.

    Idempotency here is not "running it twice is harmless"; it is "the second
    run has nothing to do". A repair that keeps finding the same fault every
    time it runs has not repaired anything -- it has just stopped complaining
    for one doctor invocation.
    """

    from orchestrator.config import Config
    from orchestrator.doctor import CheckStatus, DoctorCheck, DoctorReport
    from orchestrator.registry import (
        HostAdapterDescriptor,
        ProviderCapabilities,
        ProviderPlugin,
        ProviderRegistry,
        UsageSourceKind,
    )
    from orchestrator.repair import run_repair

    repaired: list[Path] = []
    state = {"canonical": False}

    class Adapter:
        def is_installed(self, target_dir: Path) -> bool:
            return state["canonical"]

        def install(self, target_dir: Path, *, dry_run: bool = False):
            from orchestrator.adapters import ActionKind, FileAction, InstallReport

            if not dry_run:
                repaired.append(target_dir)
                state["canonical"] = True
                # A real install leaves a registration that repair reads back.
                (target_dir.parent / f"{target_dir.name}.json").write_text(
                    '{"mcpServers": {"modelquad": {"command": "modelquad-mcp"}}}', encoding="utf-8"
                )
            return InstallReport(
                client="claude",
                dry_run=dry_run,
                actions=(FileAction(path=target_dir / "config.json", kind=ActionKind.UPDATED),),
            )

    registry = ProviderRegistry(
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
                        adapter_cls=Adapter,
                        default_target_dir=lambda: tmp_path / "claude",
                        detect=lambda: True,
                    ),
                ),
            ),
        )
    )

    def fake_doctor(config, **kwargs):
        # The finding disappears once the registration names this installation.
        status = CheckStatus.OK if state["canonical"] else CheckStatus.WARN
        return DoctorReport((DoctorCheck("claude MCP runtime", status, "detail"),))

    monkeypatch.setattr("orchestrator.repair.run_doctor", fake_doctor)

    first = run_repair(registry=registry, print_fn=lambda *a, **k: None, config=Config())
    assert first["repaired_hosts"] == ["claude"]
    assert first["after_warn_count"] == 0

    second = run_repair(registry=registry, print_fn=lambda *a, **k: None, config=Config())
    assert second["repaired_hosts"] == []
    assert second["before_warn_count"] == 0
    assert len(repaired) == 1, "the second repair must not rewrite an already-correct host"
