# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Copy the packaged Claude skills and MCP entry into the plugin folder.

The adapter templates under ``src/modelquad/adapters/templates/claude`` are
the single source. A plugin namespaces its skills (``/modelquad:review``),
so each copy only drops the ``modelquad-`` prefix from its ``name:`` line, and
the guidance skill ``modelquad`` becomes ``guide``. The MCP entry is copied
as it is: it already launches the bare ``modelquad-mcp`` that pip puts on
PATH. Run after editing a template; ``tests/test_plugin_prototype.py`` fails
while the copies differ.

    python scripts/sync_plugin.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "modelquad" / "adapters" / "templates" / "claude" / "skills"
MCP_TEMPLATE = TEMPLATES.parent / "mcp" / "mcp-servers.json"
PLUGIN_SKILLS = ROOT / "plugin" / "skills"
PLUGIN_MCP = ROOT / "plugin" / ".mcp.json"
#: Template folder -> skill name inside the plugin.
SKILLS = {
    "modelquad": "guide",
    "modelquad-review": "review",
    "modelquad-handoff": "handoff",
    "modelquad-quota": "quota",
    "modelquad-setup": "setup",
    "modelquad-split": "split",
}


def plugin_copy(template: str, plugin_name: str) -> bytes:
    text = (TEMPLATES / template / "SKILL.md").read_bytes().decode("utf-8")
    old = f"name: {template}\n"
    if old not in text:
        raise SystemExit(f"{template}/SKILL.md has no '{old.strip()}' line")
    return text.replace(old, f"name: {plugin_name}\n", 1).encode("utf-8")


def main() -> int:
    for template, plugin_name in SKILLS.items():
        target = PLUGIN_SKILLS / plugin_name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plugin_copy(template, plugin_name))
        print(f"wrote {target.relative_to(ROOT).as_posix()}")
    PLUGIN_MCP.write_bytes(MCP_TEMPLATE.read_bytes())
    print(f"wrote {PLUGIN_MCP.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
