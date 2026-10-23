# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Ordering, upgrade identity, failure atomicity and secret containment.

Companion to ``test_host_registration_transaction.py``, which covers *what*
the registration outcomes are. This file covers the properties an independent
review found were asserted too weakly or not at all:

* the record-once guard, exercised by a case that actually reaches it;
* the installed-value **lineage**, so an upgrade that legitimately rewrites
  ModelQuad's own registration is not mistaken for a user edit;
* failure atomicity -- the forbidden end state is *replaced, unrecoverable,
  reported as success*, and every injected failure is checked against it;
* the backup may hold ``env`` secrets, so every other surface is proven not
  to carry them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad.adapters import registration as reg
from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.adapters.registration import (
    RegistrationStatus,
    RestoreStatus,
    backup_path,
    recorded_replacement,
    restore_previous_registration,
)
from modelquad.onboarding import install_detected_hosts

from test_host_registration_transaction import (  # noqa: F401 -- fixtures + helpers
    HOST_ID,
    _config_path,
    _read_registration,
    _registry,
    _write_registration,
    claude_home,
    state_root,
)


def _install(home: Path):
    return install_detected_hosts([HOST_ID], dry_run=False, registry=_registry(home))[0]


def _relabel_installed_command(monkeypatch, command: str) -> None:
    """Make the next install write a DIFFERENT ModelQuad registration.

    Stands in for an upgrade, which is the only way ModelQuad's own recorded
    value legitimately changes.
    """

    monkeypatch.setattr(
        "modelquad.hostruntime.mcp_launch_command", lambda: command, raising=False
    )


# ---------------------------------------------------------------------------
# The record-once guard, exercised for real
# ---------------------------------------------------------------------------


def test_a_later_install_cannot_displace_the_recorded_original(state_root, claude_home):
    """The guard's real job, which a reinstall-only test never reaches.

    A plain reinstall writes the same value, so nothing is displaced. The hard
    question is a SECOND displacement: the user repoints the registration after
    install A, and install B replaces *that*. Without the guard, B records the
    user's intermediate value as though it were the original, and the value
    that predates ModelQuad entirely -- the only one nobody can reconstruct --
    is lost on install two rather than install one.

    Mutation-checked: deleting the ``if host in hosts`` guard fails this test
    and ``test_reinstalling_does_not_overwrite_the_recorded_original``.
    """

    original = {"command": "modelquad-mcp", "args": ["--original"]}
    _write_registration(claude_home, original)
    _install(claude_home)
    assert recorded_replacement(HOST_ID)["previous_entry"] == original

    intermediate = {"command": "C:\\somewhere\\else.cmd"}
    _write_registration(claude_home, intermediate)
    _install(claude_home)

    record = recorded_replacement(HOST_ID)
    assert record["previous_entry"] == original, "the pre-ModelQuad original must survive"
    assert record["previous_entry"] != intermediate
    assert record["previous_command"] == "modelquad-mcp"


# ---------------------------------------------------------------------------
# Upgrade-safe identity: the lineage
# ---------------------------------------------------------------------------


def test_an_upgrade_that_rewrites_our_own_registration_still_restores_the_original(
    state_root, claude_home, monkeypatch
):
    """original -> install A -> upgrade to B -> uninstall -> original restored.

    Pinning one ``installed_command`` made B look like a user edit, so
    uninstall declined to restore and the original was stranded for good.
    """

    original = {"command": "modelquad-mcp"}
    _write_registration(claude_home, original)

    _relabel_installed_command(monkeypatch, "C:\\install-A\\modelquad-mcp.cmd")
    _install(claude_home)

    _relabel_installed_command(monkeypatch, "C:\\install-B\\modelquad-mcp.cmd")
    upgraded = _install(claude_home)
    assert upgraded.status is RegistrationStatus.CONNECTED_REPLACED

    before_entry = _read_registration(claude_home)
    ClaudeAdapter().uninstall(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=upgraded.new_command,
        current_entry=before_entry,
    )

    assert result.status is RestoreStatus.RESTORED
    assert _read_registration(claude_home) == original


def test_a_user_edit_after_an_upgrade_is_still_preserved(
    state_root, claude_home, monkeypatch
):
    """The lineage must not become a licence to overwrite anything."""

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    _relabel_installed_command(monkeypatch, "C:\\install-A\\modelquad-mcp.cmd")
    _install(claude_home)
    _relabel_installed_command(monkeypatch, "C:\\install-B\\modelquad-mcp.cmd")
    _install(claude_home)

    users_own = {"command": "C:\\Users\\me\\my-own-modelquad.cmd"}
    _write_registration(claude_home, users_own)

    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=users_own["command"],
        current_entry=users_own,
    )

    assert result.status is RestoreStatus.USER_MODIFIED
    assert "preserved" in result.detail.lower()
    assert _read_registration(claude_home) == users_own
    # LOW-1: discharged, not retained -- see the sibling test for why.
    assert recorded_replacement(HOST_ID) is None


# ---------------------------------------------------------------------------
# Failure atomicity
# ---------------------------------------------------------------------------


def test_a_backup_that_cannot_be_persisted_prevents_the_replacement(
    state_root, claude_home, monkeypatch
):
    """The forbidden end state, head on.

    If the evidence cannot be written, the destructive write is not attempted:
    the host keeps a registration that works, and the user is told why.
    """

    original = {"command": "modelquad-mcp"}
    _write_registration(claude_home, original)

    def refuse(*a, **k):
        raise reg.BackupPersistenceError("disk full")

    monkeypatch.setattr(reg, "record_replaced_registration", refuse)

    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.FAILED
    assert outcome.ok is False
    assert "disk full" in (outcome.error or "")
    assert _read_registration(claude_home) == original, "the user's value must be untouched"


def test_an_adapter_write_failure_retires_the_obligation_it_invented(
    state_root, claude_home, monkeypatch
):
    """Evidence written for a replacement that never happened is cleaned up,
    so a later uninstall cannot 'restore' over something never displaced."""

    _write_registration(claude_home, {"command": "modelquad-mcp"})

    def explode(self, target_dir, *, dry_run=False):
        raise OSError("adapter could not write")

    monkeypatch.setattr(ClaudeAdapter, "install", explode)

    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.FAILED
    assert recorded_replacement(HOST_ID) is None


def test_a_failure_after_the_backup_leaves_the_original_recoverable(
    state_root, claude_home, monkeypatch
):
    """At every point after the backup exists, the original is recoverable.

    A lineage write that fails is survivable precisely because it is not the
    part that carries the user's data.
    """

    original = {"command": "modelquad-mcp", "args": ["--keepme"]}
    _write_registration(claude_home, original)

    def fail_after_backup(*a, **k):
        raise reg.BackupPersistenceError("lineage write failed")

    monkeypatch.setattr(reg, "note_installed_registration", fail_after_backup)

    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.CONNECTED_REPLACED
    assert recorded_replacement(HOST_ID)["previous_entry"] == original


def test_a_restore_write_failure_is_reported_and_keeps_the_record(
    state_root, claude_home, monkeypatch
):
    """A failed restore is neither swallowed nor allowed to forget the value."""

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    installed = _install(claude_home)
    before_entry = _read_registration(claude_home)

    from modelquad.adapters.report import ActionKind, FileAction

    monkeypatch.setattr(
        "modelquad.adapters.jsonmerge.upsert_json_key",
        lambda path, key, value, *, dry_run, backup=True: FileAction(
            path, ActionKind.SKIPPED, detail="unwritable"
        ),
    )

    ClaudeAdapter().uninstall(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=installed.new_command,
        current_entry=before_entry,
    )

    assert result.status is RestoreStatus.FAILED
    assert result.ok is False
    assert recorded_replacement(HOST_ID) is not None, "the value must stay recoverable"


# ---------------------------------------------------------------------------
# The backup may hold secrets; nothing else may
# ---------------------------------------------------------------------------


ENV_VALUE_SENTINEL = "modelquad-test-sentinel-must-not-leak-this"


def _install_with_secret(home: Path):
    _write_registration(
        home, {"command": "modelquad-mcp", "env": {"ANTHROPIC_API_KEY": ENV_VALUE_SENTINEL}}
    )
    return _install(home)


def test_the_secret_survives_only_where_a_faithful_restore_needs_it(
    state_root, claude_home
):
    """Dropping ``env`` would make the restore unfaithful -- putting back a
    registration without its credentials is its own kind of destruction. So it
    is kept, in the protected record, and proven to come back."""

    installed = _install_with_secret(claude_home)
    assert ENV_VALUE_SENTINEL in backup_path().read_text(encoding="utf-8")

    before_entry = _read_registration(claude_home)
    ClaudeAdapter().uninstall(claude_home)
    restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=installed.new_command,
        current_entry=before_entry,
    )

    assert _read_registration(claude_home)["env"]["ANTHROPIC_API_KEY"] == ENV_VALUE_SENTINEL


def test_the_secret_never_reaches_the_outcome_object(state_root, claude_home):
    outcome = _install_with_secret(claude_home)

    assert ENV_VALUE_SENTINEL not in json.dumps(outcome.to_dict())
    assert ENV_VALUE_SENTINEL not in repr(outcome)


def test_the_secret_never_reaches_the_onboarding_wording(state_root, claude_home):
    from modelquad.onboarding import _host_step_wording

    host = _registry(claude_home).all()[0].host_adapters[0]
    outcome = _install_with_secret(claude_home)
    detail, user_detail = _host_step_wording(host, outcome, dry_run=False)

    assert ENV_VALUE_SENTINEL not in detail
    assert ENV_VALUE_SENTINEL not in user_detail


def test_the_secret_never_reaches_a_restore_result(state_root, claude_home):
    installed = _install_with_secret(claude_home)
    before_entry = _read_registration(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=installed.new_command,
        current_entry=before_entry,
    )

    assert ENV_VALUE_SENTINEL not in json.dumps(result.to_dict())
    assert ENV_VALUE_SENTINEL not in result.detail


def test_the_secret_never_reaches_doctor(state_root, claude_home):
    """A support artefact is exactly where a leaked key would end up."""

    from modelquad.config import Config
    from modelquad.service import control_plane as shell
    from modelquad.doctor import run_doctor

    _install_with_secret(claude_home)
    report = run_doctor(
        Config(),
        usage_directory=claude_home,
        which=lambda _: "/fake/cli",
        control_plane_probe=lambda port: shell.ControlPlaneVerdict(
            shell.PROBE_ABSENT, "no listener"
        ),
    )

    assert ENV_VALUE_SENTINEL not in json.dumps(report.to_dict())


def test_the_record_is_removed_once_the_obligation_is_discharged(
    state_root, claude_home
):
    """Lifetime: the stored secret does not linger after a successful restore."""

    installed = _install_with_secret(claude_home)
    before_entry = _read_registration(claude_home)
    ClaudeAdapter().uninstall(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=installed.new_command,
        current_entry=before_entry,
    )

    assert result.status is RestoreStatus.RESTORED
    assert recorded_replacement(HOST_ID) is None
    assert ENV_VALUE_SENTINEL not in backup_path().read_text(encoding="utf-8")


def test_the_backup_file_is_not_world_readable(state_root, claude_home):
    """Best-effort on Windows (ACLs are not chmod's to give), meaningful on POSIX.

    Asserted rather than assumed because the file can hold an ``env`` block.
    """

    import os
    import sys

    _install_with_secret(claude_home)
    mode = os.stat(backup_path()).st_mode & 0o777

    if sys.platform == "win32":
        pytest.skip("POSIX permission bits are not the protection model on Windows")
    assert mode & 0o077 == 0, f"backup is group/other readable: {mode:o}"
