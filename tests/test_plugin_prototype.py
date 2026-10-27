# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The experimental Claude Code plugin stays a thin, safe copy of the package."""

import importlib.util
import json
import re
from pathlib import Path

from modelquad import __version__

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


def _sync_module():
    spec = importlib.util.spec_from_file_location("sync_plugin", ROOT / "scripts" / "sync_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_version_matches_the_package():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "modelquad"
    assert manifest["version"] == __version__


def test_marketplace_lists_only_the_local_plugin():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert [(entry["name"], entry["source"]) for entry in marketplace["plugins"]] == [("modelquad", "./plugin")]


def test_plugin_skills_are_the_packaged_templates():
    sync = _sync_module()
    found = {path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md")}
    assert found == set(sync.SKILLS.values())
    for template, name in sync.SKILLS.items():
        assert (PLUGIN / "skills" / name / "SKILL.md").read_bytes() == sync.plugin_copy(template, name), (
            f"plugin/skills/{name} is stale: run python scripts/sync_plugin.py")


def test_plugin_server_is_the_packaged_entry():
    # The same entry `modelquad install claude` writes: the bare command pip
    # puts on PATH, no absolute path, no env, and the same tool timeout.
    sync = _sync_module()
    assert (PLUGIN / ".mcp.json").read_bytes() == sync.MCP_TEMPLATE.read_bytes(), (
        "plugin/.mcp.json is stale: run python scripts/sync_plugin.py")
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == ["modelquad"]
    assert servers["modelquad"]["command"] == "modelquad-mcp"
    assert "env" not in servers["modelquad"]


def test_plugin_ships_no_permissions_and_only_the_reminder_hook():
    # A plugin's server tools are approved like any other: every Codex call
    # keeps Claude Code's approval prompt, and nothing here pre-approves one.
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert not {"mcpServers", "settings", "agents", "outputStyles"} & set(manifest)
    assert not (PLUGIN / "agents").exists() and not (PLUGIN / "bin").exists()
    config = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert config["modules"] == ["./quota-band.tsx"]
    hooks = config["hooks"]
    assert set(hooks) == {"SessionStart"}
    (entry,) = hooks["SessionStart"]
    (hook,) = entry["hooks"]
    assert hook["command"] == "modelquad" and hook["args"] == ["hook-pending", "claude"]
    assert "permissions" not in json.dumps(manifest)


def test_the_quota_band_only_displays():
    # The band draws quotas above the prompt where no status line does. It
    # must never block, approve or rewrite anything (AGENTS.md invariant 6
    # holds for ModelQuad's hooks of every kind), nor start a process.
    source = (PLUGIN / "hooks" / "quota-band.tsx").read_text(encoding="utf-8")
    events = set(re.findall(r"""\bon\(\s*['"]([\w.]+)['"]""", source))
    assert events == {"session.start", "session.measure", "ui.render"}
    for forbidden in ("$.process", "$.tool", "$.model", "$.agent", "$.session.append",
                      "$.fs.write", "$.env.set", "deny"):
        assert forbidden not in source, forbidden
    assert "usage-codex.json" in source
