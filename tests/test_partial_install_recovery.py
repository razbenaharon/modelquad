# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A partially-applied install must never destroy its own recovery evidence.

An adapter install is not atomic. ``ClaudeAdapter.install`` performs several
writes and the MCP registration is one of the first, so this sequence is real
and was demonstrated live:

1. the registration is replaced -- the user's original is now overwritten;
2. a later write in the same install (CLAUDE.md, settings.json) fails;
3. onboarding catches the failure and retires the recovery record;
4. the result is reported ``FAILED``.

Which leaves the machine in exactly the state the transaction work exists to
make unreachable:

    registration replaced + original destroyed + record deleted + "FAILED"

The rule that closes it is asymmetric on purpose. Keeping a record that turns
out to be unnecessary costs one small file in the state folder.
Deleting one that *was* necessary destroys a registration the user had before
ModelQuad existed, permanently and silently. So evidence is retired only on
proof that the replacement did not happen, and every ambiguous reading keeps
it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.adapters import registration as reg
from orchestrator.adapters.claude_adapter import ClaudeAdapter
from orchestrator.adapters.registration import (
    RegistrationStatus,
    RestoreStatus,
    recorded_replacement,
    restore_previous_registration,
)
from orchestrator.onboarding import install_detected_hosts

from test_host_registration_transaction import (  # noqa: F401 -- fixtures + helpers
    HOST_ID,
    _read_registration,
    _registry,
    _write_registration,
    claude_home,
    state_root,
)


ORIGINAL = {"command": "modelquad-mcp", "args": ["--the-users-own"]}


def _install(home: Path):
    return install_detected_hosts([HOST_ID], dry_run=False, registry=_registry(home))[0]


def _fail_after_the_registration_write(monkeypatch) -> None:
    """Let the real adapter write the registration, then fail the next step.

    Patched at ``pending_action``, which ``ClaudeAdapter.install`` calls
    *after* every staged file -- the registration included -- was written.
    """

    def explode(*a, **k):
        raise OSError("CLAUDE.md is not writable")

    monkeypatch.setattr(
        "orchestrator.adapters.claude_adapter.pending_action", explode
    )


def _fail_before_the_registration_write(monkeypatch) -> None:
    """Fail while planning, before any file -- the registration included -- is written."""

    def explode(*a, **k):
        raise OSError("skills directory is not writable")

    monkeypatch.setattr("orchestrator.adapters.claude_adapter._plan_owned_copy", explode)


# ---------------------------------------------------------------------------
# A. failure BEFORE the registration write -> evidence may be retired
# ---------------------------------------------------------------------------


def test_a_failure_before_the_registration_write_retires_the_record(
    state_root, claude_home, monkeypatch
):
    _write_registration(claude_home, ORIGINAL)
    _fail_before_the_registration_write(monkeypatch)

    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.FAILED
    assert _read_registration(claude_home) == ORIGINAL, "nothing was displaced"
    assert recorded_replacement(HOST_ID) is None, "a fictional obligation is retired"


# ---------------------------------------------------------------------------
# B. failure AFTER the registration write -> evidence MUST survive
# ---------------------------------------------------------------------------


def test_a_failure_after_the_registration_write_keeps_the_record(
    state_root, claude_home, monkeypatch
):
    """The exact class the reviewer demonstrated."""

    _write_registration(claude_home, ORIGINAL)
    _fail_after_the_registration_write(monkeypatch)

    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.FAILED
    # The registration really was replaced before the failure...
    assert _read_registration(claude_home) != ORIGINAL
    # ...so the evidence must still be there.
    record = recorded_replacement(HOST_ID)
    assert record is not None, "recovery evidence was destroyed by a partial install"
    assert record["previous_entry"] == ORIGINAL


def test_a_partial_install_claims_the_value_it_wrote_into_the_lineage(
    state_root, claude_home, monkeypatch
):
    """Without this the half-written value is in no lineage, so a later
    uninstall reads it as a user edit and 'preserves' ModelQuad's own
    registration instead of restoring the user's original."""

    _write_registration(claude_home, ORIGINAL)
    _fail_after_the_registration_write(monkeypatch)
    _install(claude_home)

    record = recorded_replacement(HOST_ID)
    current = _read_registration(claude_home)
    assert reg._fingerprint(current, current.get("command")) in record["installed_lineage"]


# ---------------------------------------------------------------------------
# C. a later uninstall still recovers the original
# ---------------------------------------------------------------------------


def test_the_original_is_still_recoverable_after_a_partial_install(
    state_root, claude_home, monkeypatch
):
    _write_registration(claude_home, ORIGINAL)
    with monkeypatch.context() as failing:
        _fail_after_the_registration_write(failing)
        _install(claude_home)

    before_entry = _read_registration(claude_home)
    before_command = before_entry.get("command")
    ClaudeAdapter().uninstall(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=before_command,
        current_entry=before_entry,
    )

    assert result.status is RestoreStatus.RESTORED
    assert _read_registration(claude_home) == ORIGINAL


# ---------------------------------------------------------------------------
# D. no path produces "replacement happened + record absent"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inject",
    [
        _fail_before_the_registration_write,
        _fail_after_the_registration_write,
    ],
    ids=["fail-before-write", "fail-after-write"],
)
def test_no_failure_path_leaves_a_replacement_without_evidence(
    state_root, claude_home, monkeypatch, inject
):
    """The forbidden state, asserted as a property over both injection points."""

    _write_registration(claude_home, ORIGINAL)
    inject(monkeypatch)

    _install(claude_home)

    replaced = _read_registration(claude_home) != ORIGINAL
    has_record = recorded_replacement(HOST_ID) is not None
    assert not (replaced and not has_record), (
        "the registration was replaced and the recovery record is gone"
    )


def test_an_unreadable_registration_after_failure_keeps_the_record(
    state_root, claude_home, monkeypatch
):
    """Ambiguity must resolve towards keeping evidence, never towards deleting it."""

    _write_registration(claude_home, ORIGINAL)

    # Only the re-read AFTER the failure may break -- the pre-install reads
    # have to succeed or there is no obligation to reason about at all. Gated
    # on the adapter having failed rather than on a call count, which is
    # brittle: ``read_registered_command`` calls ``read_registered_entry``
    # internally, so the pre-install phase already makes two of them.
    from orchestrator import hostruntime

    real = hostruntime.read_registered_entry
    state = {"install_failed": False}

    def explode(*a, **k):
        state["install_failed"] = True
        raise OSError("CLAUDE.md is not writable")

    monkeypatch.setattr(
        "orchestrator.adapters.claude_adapter.pending_action", explode
    )

    def unreadable_after_the_failure(*a, **k):
        if state["install_failed"]:
            raise OSError("config is unreadable right now")
        return real(*a, **k)

    monkeypatch.setattr(
        hostruntime, "read_registered_entry", unreadable_after_the_failure
    )

    _install(claude_home)

    assert recorded_replacement(HOST_ID) is not None


# ---------------------------------------------------------------------------
# Negative control
# ---------------------------------------------------------------------------


def test_negative_control_unconditional_retire_is_caught(
    state_root, claude_home, monkeypatch
):
    """Proves case B measures the fix rather than a coincidence: restoring the
    old unconditional ``forget_replacement`` on failure destroys the record."""

    _write_registration(claude_home, ORIGINAL)
    _fail_after_the_registration_write(monkeypatch)

    def always_retire(client, target_dir, *, before_entry, before_command):
        reg.forget_replacement(client)

    monkeypatch.setattr(
        reg, "_retire_or_keep_after_failed_install", always_retire
    )

    _install(claude_home)

    assert _read_registration(claude_home) != ORIGINAL
    assert recorded_replacement(HOST_ID) is None, (
        "with the old behaviour the evidence IS destroyed -- which is what "
        "case B proves no longer happens"
    )
