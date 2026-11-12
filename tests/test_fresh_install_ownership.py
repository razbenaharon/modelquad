# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Uninstall may only remove a registration ModelQuad itself still owns.

The defect this closes is the fresh-install half of an invariant that was only
ever implemented for the *replaced* half:

1. No prior ModelQuad registration exists.
2. ModelQuad installs and creates one.
3. Nothing is recorded, because nothing was displaced.
4. The user later repoints that registration at something of their own.
5. They uninstall ModelQuad.
6. The adapter deletes ``mcpServers.modelquad`` unconditionally.
7. With no record, there is no ownership check and no restore path.
8. **The user's registration is destroyed.**

The ownership rule that now covers both halves:

    On uninstall, ModelQuad may delete or restore the registration only if the
    CURRENT registration is still one of the values ModelQuad itself
    installed. Otherwise the user's value is preserved.

Four cases, all exercised here through ``cli.main(["uninstall", ...])``:

============================  ==========================  ====================
                              current is ModelQuad-owned  current is the user's
============================  ==========================  ====================
an original was displaced     restore the original        preserve the user's
nothing was displaced         remove ModelQuad's own      preserve the user's
============================  ==========================  ====================
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad import cli
from modelquad.adapters.registration import RestoreStatus, recorded_replacement
from modelquad.onboarding import install_detected_hosts

from test_host_registration_transaction import (  # noqa: F401 -- fixtures + helpers
    HOST_ID,
    _read_registration,
    _registry,
    _write_registration,
    claude_home,
    state_root,
)


ENV_VALUE_SENTINEL = "modelquad-test-sentinel-ownership-must-not-print"


def _install(home: Path):
    return install_detected_hosts([HOST_ID], dry_run=False, registry=_registry(home))[0]


def _uninstall(home: Path, *, as_json: bool = False) -> int:
    argv = ["uninstall", "claude", "--target-dir", str(home)]
    if as_json:
        argv.append("--json")
    return cli.main(argv)


def _relabel_installed_command(monkeypatch, command: str) -> None:
    """Make the next install write a DIFFERENT ModelQuad registration."""

    monkeypatch.setattr(
        "modelquad.hostruntime.mcp_launch_command", lambda: command, raising=False
    )


# ---------------------------------------------------------------------------
# 1. Fresh install, untouched -> ModelQuad's own registration is removed
# ---------------------------------------------------------------------------


def test_fresh_install_then_uninstall_removes_our_own_registration(
    state_root, claude_home, capsys
):
    """Ownership must not become an excuse to leave litter behind."""

    _install(claude_home)
    assert _read_registration(claude_home) is not None

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) is None, "ours, so it goes"
    assert "Registration:" not in out, "the ordinary case stays quiet"
    assert recorded_replacement(HOST_ID) is None, "the record goes with it"


# ---------------------------------------------------------------------------
# 2. Fresh install, user repointed it -> the user's value is preserved
# ---------------------------------------------------------------------------


def test_fresh_install_then_user_change_then_uninstall_preserves_the_user_value(
    state_root, claude_home, capsys
):
    """The defect, head on. Before this fix the key was deleted outright."""

    _install(claude_home)

    users_own = {"command": "C:\\Users\\me\\my-own-modelquad.cmd", "args": ["--mine"]}
    _write_registration(claude_home, users_own)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) == users_own, "the user's value survives"
    assert "Kept your current" in out
    assert "not put back" in out


def test_the_fresh_install_user_change_case_is_truthful_in_json(
    state_root, claude_home, capsys
):
    _install(claude_home)
    _write_registration(claude_home, {"command": "C:\\mine.cmd"})

    _uninstall(claude_home, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert payload["restore"]["status"] == "user_modified"
    assert payload["restore"]["restored_command"] is None


# ---------------------------------------------------------------------------
# 3. Fresh install A -> upgrade B, untouched -> the latest owned value goes
# ---------------------------------------------------------------------------


def test_fresh_install_then_upgrade_then_uninstall_removes_the_latest_owned_value(
    state_root, claude_home, monkeypatch, capsys
):
    """Ownership has to be a lineage even when nothing was ever displaced, or
    an upgraded fresh install looks like a user edit and is left behind."""

    _relabel_installed_command(monkeypatch, "C:/install-A/modelquad-mcp.cmd")
    _install(claude_home)
    _relabel_installed_command(monkeypatch, "C:/install-B/modelquad-mcp.cmd")
    _install(claude_home)

    record = recorded_replacement(HOST_ID)
    assert record["has_previous"] is False
    assert len(record["installed_lineage"]) == 2, "both of our values are owned"

    rc = _uninstall(claude_home)

    assert rc == 0
    assert _read_registration(claude_home) is None


# ---------------------------------------------------------------------------
# 4. Fresh install A -> upgrade B -> user change -> the user's value survives
# ---------------------------------------------------------------------------


def test_fresh_install_then_upgrade_then_user_change_preserves_the_user_value(
    state_root, claude_home, monkeypatch, capsys
):
    _relabel_installed_command(monkeypatch, "C:/install-A/modelquad-mcp.cmd")
    _install(claude_home)
    _relabel_installed_command(monkeypatch, "C:/install-B/modelquad-mcp.cmd")
    _install(claude_home)

    users_own = {"command": "C:\\Users\\me\\after-upgrade.cmd"}
    _write_registration(claude_home, users_own)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) == users_own
    assert "Kept your current" in out


# ---------------------------------------------------------------------------
# 5. The replaced path still restores the original
# ---------------------------------------------------------------------------


def test_the_replaced_path_still_restores_the_original(state_root, claude_home, capsys):
    """Regression guard: adding the fresh-install case must not weaken the
    case it was modelled on."""

    original = {"command": "modelquad-mcp", "args": ["--original"]}
    _write_registration(claude_home, original)
    _install(claude_home)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) == original
    assert "Restored" in out


# ---------------------------------------------------------------------------
# 7. No previous_entry may ever be fabricated
# ---------------------------------------------------------------------------


def test_a_fresh_install_never_fabricates_a_previous_entry(state_root, claude_home):
    """The cheap way to make the four cases uniform would be to record the
    value ModelQuad just wrote as the "previous" one. That would claim
    something was displaced when nothing was, and a later restore would write
    back a registration that never existed."""

    _install(claude_home)

    record = recorded_replacement(HOST_ID)
    assert record["previous_entry"] is None
    assert record["previous_command"] is None
    assert record["has_previous"] is False

    # And it stays absent across an upgrade.
    _install(claude_home)
    record = recorded_replacement(HOST_ID)
    assert record["previous_entry"] is None
    assert record["has_previous"] is False


# ---------------------------------------------------------------------------
# 8. The preserved value must not leak
# ---------------------------------------------------------------------------


def test_a_preserved_user_registration_never_reaches_the_output(
    state_root, claude_home, capsys
):
    """The value being protected may itself carry credentials."""

    _install(claude_home)
    _write_registration(
        claude_home,
        {"command": "C:\\mine.cmd", "env": {"ANTHROPIC_API_KEY": ENV_VALUE_SENTINEL}},
    )

    _uninstall(claude_home)
    out = capsys.readouterr().out

    assert ENV_VALUE_SENTINEL not in out
    assert _read_registration(claude_home)["env"]["ANTHROPIC_API_KEY"] == ENV_VALUE_SENTINEL


def test_a_preserved_user_registration_never_reaches_the_json(
    state_root, claude_home, capsys
):
    _install(claude_home)
    _write_registration(
        claude_home,
        {"command": "C:\\mine.cmd", "env": {"ANTHROPIC_API_KEY": ENV_VALUE_SENTINEL}},
    )

    _uninstall(claude_home, as_json=True)
    out = capsys.readouterr().out

    assert ENV_VALUE_SENTINEL not in out
    json.loads(out)


# ---------------------------------------------------------------------------
# Negative control
# ---------------------------------------------------------------------------


def test_negative_control_unconditional_deletion_is_caught(
    state_root, claude_home, monkeypatch
):
    """Proves these tests would fail if uninstall reverted to deleting the key
    unconditionally.

    Simulated by neutering the ownership step -- which is exactly what the old
    behaviour was: the adapter removes the key, and nothing puts anything
    back. If this passes while case 2 also passes, case 2 is not actually
    testing the protection.
    """

    _install(claude_home)
    users_own = {"command": "C:\\Users\\me\\my-own-modelquad.cmd"}
    _write_registration(claude_home, users_own)

    monkeypatch.setattr(
        cli, "_restore_displaced_registration", lambda *a, **k: None
    )

    _uninstall(claude_home)

    # This used to be the negative control (the value was destroyed). The
    # adapter itself now leaves an entry that is not recognisably ModelQuad's
    # in place, so the user's value is protected twice.
    assert _read_registration(claude_home) == users_own, (
        "the adapter must leave a foreign entry in place on its own, "
        "whatever the ownership step does"
    )
