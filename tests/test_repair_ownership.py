# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""What a repairing install is allowed to touch, and what it must leave as it found it.

A stale registration is repaired by installing again. This file covers the
promise underneath that: the install edits **only the entry ModelQuad owns**,
in a file that belongs to somebody else. If repairing ModelQuad could disturb
an unrelated MCP server, a hook the user wrote, or a development checkout, it
would be a destructive action wearing a friendly label.

Every test here drives the **real** adapter against fixture configuration in
``tmp_path``. Nothing in this file reads or writes the developer's own
``~/.claude``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.branding import MCP_SERVER_NAME


#: The absolute launcher an installed ModelQuad registers. Repair's whole job
#: is to move a stale registration onto this.
INSTALLED_SHIM = "C:\\Users\\Someone\\AppData\\Local\\ModelQuad\\bin\\modelquad-mcp.cmd"

#: What P0-A found live on 2026-08-26: a bare name that resolved through PATH
#: to an editable checkout while the installed product used another database.
STALE_BARE = "modelquad-mcp"
STALE_EDITABLE = "C:\\Python311\\Scripts\\modelquad-mcp.exe"


@pytest.fixture
def installed_runtime(monkeypatch):
    """Make the adapters believe they belong to an installed product."""

    monkeypatch.setattr(
        "modelquad.hostruntime.mcp_launch_command", lambda: INSTALLED_SHIM
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
    store, and copying either direction would destroy real settings. This
    pins it: after a repair, the development checkout and its database are
    still there, untouched.
    """

    dev_root = tmp_path / "dev-state"
    dev_root.mkdir()
    dev_db = dev_root / "modelquad.db"
    dev_db.write_bytes(b"development database contents")
    dev_checkout = tmp_path / "checkout" / "src" / "modelquad"
    dev_checkout.mkdir(parents=True)
    (dev_checkout / "__init__.py").write_text("# dev checkout", encoding="utf-8")

    monkeypatch.setattr(
        "modelquad.hostruntime.mcp_launch_command", lambda: INSTALLED_SHIM
    )
    home = _claude_home(tmp_path, STALE_EDITABLE)
    ClaudeAdapter().install(home)

    assert dev_db.read_bytes() == b"development database contents"
    assert (dev_checkout / "__init__.py").read_text(encoding="utf-8") == "# dev checkout"
    assert _claude_servers(tmp_path)[MCP_SERVER_NAME]["command"] == INSTALLED_SHIM
