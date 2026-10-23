# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``host-registration-backup.json`` is product-owned state, and documented.

It is the only product-owned file that can contain a third-party credential:
an MCP entry may carry an ``env`` block, and it is stored verbatim because a
restore without it would be unfaithful.

These tests hold two things together: the inventory that documents it, and
the surfaces that must never show its contents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad.adapters import registration as reg
from modelquad.adapters.registration import BACKUP_FILE_NAME, backup_path


ENV_VALUE_SENTINEL = "modelquad-test-sentinel-must-never-be-shown"

#: The data inventory lives in PRIVACY.md, the single privacy document.
INVENTORY = Path(__file__).resolve().parents[1] / "PRIVACY.md"


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(root))
    return root


def _write_backup_with_a_secret(state_root: Path) -> Path:
    reg.record_replaced_registration(
        "claude",
        previous_entry={
            "command": "modelquad-mcp",
            "env": {"ANTHROPIC_API_KEY": ENV_VALUE_SENTINEL},
        },
        previous_command="modelquad-mcp",
    )
    path = backup_path()
    assert path.is_file()
    assert ENV_VALUE_SENTINEL in path.read_text(encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_the_backup_is_documented_in_the_privacy_inventory():
    text = INVENTORY.read_text(encoding="utf-8")

    assert BACKUP_FILE_NAME in text
    assert any(line.startswith(f"| `{BACKUP_FILE_NAME}`") for line in text.splitlines())


def test_the_inventory_flags_it_as_possibly_sensitive():
    """The file's whole difference from every other row is that it can hold a
    credential; an inventory that does not say so is worse than no row."""

    text = INVENTORY.read_text(encoding="utf-8").lower()

    assert "may contain credentials" in text
    assert "env" in text


# ---------------------------------------------------------------------------
# The contents must not reach any reporting surface
# ---------------------------------------------------------------------------


def test_the_secret_never_reaches_doctor(state_root):
    from modelquad.config import Config
    from modelquad.service import control_plane as shell
    from modelquad.doctor import run_doctor

    _write_backup_with_a_secret(state_root)
    report = run_doctor(
        Config(),
        usage_directory=state_root,
        which=lambda _: "/fake/cli",
        control_plane_probe=lambda port: shell.ControlPlaneVerdict(
            shell.PROBE_ABSENT, "no listener"
        ),
    )

    assert ENV_VALUE_SENTINEL not in json.dumps(report.to_dict())


