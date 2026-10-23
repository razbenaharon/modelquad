# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Packaged Claude templates are the sole shipped instruction source."""

from pathlib import Path


def test_packaged_adapter_templates_exist_without_project_duplicates():
    root = Path(__file__).resolve().parents[1]
    templates = root / "src" / "modelquad" / "adapters" / "templates" / "claude"
    # The guidance moved to the MCP server's instructions; no CLAUDE.md block ships.
    assert not (templates / "CLAUDE.md.block").exists()
    assert (templates / "skills" / "modelquad" / "SKILL.md").is_file()
    assert not any((root / "adapters" / "claude").rglob("SKILL.md"))
