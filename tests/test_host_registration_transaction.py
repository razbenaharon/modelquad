# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Installing must never silently destroy a previously valid ModelQuad host config.

These tests exist because a real Windows install on 2026-08-29 did exactly
that. The machine already carried ``"modelquad": {"command": "modelquad-mcp"}``
in ``~/.claude.json``; installing rewrote it to the frozen absolute shim and
said only "Connected Claude Code."; and uninstalling would then have *deleted*
the key outright, so the value the user had before ModelQuad existed would be
gone with no record that it had ever been there.

Two separate properties are covered, and they fail in different ways:

1. **Truthfulness** -- a new registration, an identical one and a replacement
   are three different outcomes and must be reported as three different
   outcomes.
2. **Transactionality** -- what install displaces, uninstall gives
   back, *unless* the user has since changed it themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.adapters.registration import (
    HostRegistrationOutcome,
    RegistrationStatus,
    RestoreStatus,
    backup_path,
    note_installed_registration,
    record_replaced_registration,
    recorded_replacement,
    restore_previous_registration,
)
from modelquad.onboarding import install_detected_hosts
from modelquad.registry import (
    HostAdapterDescriptor,
    ProviderCapabilities,
    ProviderPlugin,
    ProviderRegistry,
    UsageSourceKind,
)


HOST_ID = "claude"


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point this installation's state root at the test's own directory.

    The displaced-registration record belongs to the installation, so it lives
    under the state root; without this a test would write into the developer's
    real one.
    """

    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(root))
    return root


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    """A ``.claude`` target directory whose config file is ``.claude.json``."""

    target = tmp_path / ".claude"
    target.mkdir()
    return target


def _registry(target: Path) -> ProviderRegistry:
    """A registry whose one host is the REAL Claude adapter, aimed at tmp_path.

    The real adapter is used deliberately: the defect lived in the interaction
    between the adapter's ``upsert``/``remove`` pair and the result model, so a
    stub adapter would test the reporting and miss the destruction.
    """

    return ProviderRegistry(
        (
            ProviderPlugin(
                id="claude",
                display_name="Claude",
                capabilities=ProviderCapabilities(coding=True),
                usage_source_kind=UsageSourceKind.LOCAL_LOGS,
                cli_executable="claude",
                host_adapters=(
                    HostAdapterDescriptor(
                        id=HOST_ID,
                        display_name="Claude Code",
                        provider_id="claude",
                        adapter_cls=ClaudeAdapter,
                        default_target_dir=lambda: target,
                        detect=lambda: True,
                    ),
                ),
            ),
        )
    )


def _config_path(claude_home: Path) -> Path:
    return claude_home.parent / f"{claude_home.name}.json"


def _write_registration(claude_home: Path, entry: dict) -> None:
    path = _config_path(claude_home)
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    payload.setdefault("mcpServers", {})["modelquad"] = entry
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_registration(claude_home: Path) -> dict | None:
    path = _config_path(claude_home)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("mcpServers", {}).get("modelquad")


def _install(claude_home: Path):
    return install_detected_hosts(
        [HOST_ID], dry_run=False, registry=_registry(claude_home)
    )[0]


# ---------------------------------------------------------------------------
# 1. Truthfulness
# ---------------------------------------------------------------------------


def test_a_first_registration_is_reported_as_new(state_root, claude_home):
    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.CONNECTED_NEW
    assert outcome.previous_registration_present is False
    assert outcome.previous_registration_different is False
    assert outcome.previous_command is None
    assert outcome.new_command


def test_reinstalling_over_our_own_value_is_reported_as_unchanged(state_root, claude_home):
    _install(claude_home)
    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.CONNECTED_UNCHANGED
    assert outcome.previous_registration_present is True
    assert outcome.previous_registration_different is False


def test_replacing_a_different_registration_says_so(state_root, claude_home):
    """The observed defect: this case used to be indistinguishable from a new one.

    Asserted on the whole entry rather than on ``command``, because which
    field changes depends on packaging: a build from the retired installer
    rewrote the command to its absolute shim, while a development checkout
    keeps the bare name and changes only the rest of the entry
    (``hostruntime`` explains why). Both are replacements, and the classifier has to say so either way.
    """

    original = {"command": "modelquad-mcp"}
    _write_registration(claude_home, original)

    outcome = _install(claude_home)

    assert outcome.status is RegistrationStatus.CONNECTED_REPLACED
    assert outcome.previous_registration_present is True
    assert outcome.previous_registration_different is True
    assert outcome.previous_command == "modelquad-mcp"
    assert _read_registration(claude_home) != original


def test_a_replacement_is_worded_differently_from_a_fresh_connection(
    state_root, claude_home, tmp_path
):
    """The three outcomes must not collapse back into one sentence."""

    from modelquad.onboarding import _host_step_wording

    host = _registry(claude_home).all()[0].host_adapters[0]
    fresh = _install(claude_home)
    fresh_detail, fresh_user = _host_step_wording(host, fresh, dry_run=False)

    other = tmp_path / "other.claude"
    other.mkdir()
    _write_registration(other, {"command": "modelquad-mcp"})
    replaced = _install(other)
    replaced_detail, replaced_user = _host_step_wording(host, replaced, dry_run=False)

    assert fresh_user != replaced_user
    assert "replac" in replaced_user.lower()
    assert fresh_detail != replaced_detail


def test_a_supported_host_may_promise_the_restore(state_root, claude_home):
    """Claude Code's entry is a JSON value this product can rewrite verbatim,
    so saying the previous one was saved is true."""

    from modelquad.onboarding import _host_step_wording

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    host = _registry(claude_home).all()[0].host_adapters[0]
    outcome = _install(claude_home)
    assert outcome.restore_supported is True

    detail, user_detail = _host_step_wording(host, outcome, dry_run=False)

    assert "saved" in user_detail.lower()
    assert "can be restored" in detail.lower()


def test_the_user_sentence_never_names_a_path(state_root, claude_home):
    """``user_detail`` is plain language; launch commands are absolute paths."""

    from modelquad.onboarding import _host_step_wording

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    host = _registry(claude_home).all()[0].host_adapters[0]
    outcome = _install(claude_home)
    _detail, user_detail = _host_step_wording(host, outcome, dry_run=False)

    assert outcome.new_command not in user_detail
    assert "modelquad-mcp" not in user_detail
    assert "\\" not in user_detail and "/" not in user_detail


def test_an_undetected_host_is_not_reported_as_connected(state_root, claude_home):
    registry = _registry(claude_home)
    plugin = registry.all()[0]
    absent = ProviderRegistry(
        (
            ProviderPlugin(
                id=plugin.id,
                display_name=plugin.display_name,
                capabilities=plugin.capabilities,
                usage_source_kind=plugin.usage_source_kind,
                cli_executable=plugin.cli_executable,
                host_adapters=(
                    HostAdapterDescriptor(
                        id=HOST_ID,
                        display_name="Claude Code",
                        provider_id="claude",
                        adapter_cls=ClaudeAdapter,
                        default_target_dir=lambda: claude_home,
                        detect=lambda: False,
                    ),
                ),
            ),
        )
    )
    outcome = install_detected_hosts([HOST_ID], dry_run=False, registry=absent)[0]

    assert outcome.status is RegistrationStatus.NOT_DETECTED
    assert outcome.ok is False


def test_the_record_never_captures_a_neighbouring_mcp_server(state_root, claude_home):
    """Only ModelQuad's own entry may be read into ModelQuad's state."""

    path = _config_path(claude_home)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "modelquad": {"command": "modelquad-mcp"},
                    "secret-tool": {"command": "x", "env": {"TOKEN": "s3cret"}},
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _install(claude_home)

    recorded = backup_path().read_text(encoding="utf-8")
    assert "secret-tool" not in recorded
    assert "s3cret" not in recorded


# ---------------------------------------------------------------------------
# 2. Transactionality
# ---------------------------------------------------------------------------


def test_uninstall_restores_what_install_displaced(state_root, claude_home):
    """The invariant. Before this, uninstall deleted the key and the user's
    own registration was gone for good."""

    original = {"command": "modelquad-mcp"}
    _write_registration(claude_home, original)
    installed = _install(claude_home)
    assert _read_registration(claude_home) != original

    before_entry = _read_registration(claude_home)
    before_command = installed.new_command
    ClaudeAdapter().uninstall(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=before_command,
        current_entry=before_entry,
    )

    assert result.status is RestoreStatus.RESTORED
    assert _read_registration(claude_home) == original


def test_uninstall_does_not_overwrite_a_change_the_user_made_afterwards(
    state_root, claude_home
):
    """Restoring a stale value over a newer deliberate one is the same
    destruction, aimed the other way."""

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    _install(claude_home)

    users_own = {"command": "C:\\Users\\me\\my-own-modelquad.cmd"}
    _write_registration(claude_home, users_own)

    result = restore_previous_registration(
        HOST_ID, claude_home, current_command=users_own["command"]
    )

    assert result.status is RestoreStatus.USER_MODIFIED
    assert _read_registration(claude_home) == users_own
    # LOW-1: the displaced original will never be restored automatically now
    # that the user's newer choice stands, so the record has no remaining
    # recovery purpose and is discharged rather than keeping an env block
    # indefinitely for a restore that cannot happen.
    assert recorded_replacement(HOST_ID) is None


def test_a_fresh_install_records_ownership_but_no_original(state_root, claude_home):
    """A machine that had no ModelQuad registration is owed nothing back -- but
    ModelQuad still has to know which registration it created.

    Without that, a later uninstall cannot tell its own value from one the user
    repointed afterwards, and deletes both alike. ``previous_entry`` stays
    absent: inventing one to make the logic uniform would be a fabricated
    claim that something was displaced.
    """

    outcome = _install(claude_home)

    record = recorded_replacement(HOST_ID)
    assert record is not None, "ownership must be recorded"
    assert record["previous_entry"] is None, "nothing was displaced; none may be invented"
    assert record["previous_command"] is None
    assert record["has_previous"] is False
    assert record["installed_lineage"], "the value we created must be owned"

    current_entry = _read_registration(claude_home)
    ClaudeAdapter().uninstall(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=outcome.new_command,
        current_entry=current_entry,
    )
    assert result.status is RestoreStatus.REMOVED
    assert result.restored_command is None


def test_a_removal_that_did_not_land_keeps_the_record(state_root, claude_home):
    """REMOVED is reported only once the registration is actually gone."""

    outcome = _install(claude_home)
    result = restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=outcome.new_command,
        current_entry=_read_registration(claude_home),
    )
    assert result.status is RestoreStatus.FAILED
    assert recorded_replacement(HOST_ID) is not None


def test_reinstalling_does_not_overwrite_the_recorded_original(state_root, claude_home):
    """The subtle one: a second install must not record ModelQuad's own value
    as though it were the user's, or the real original is lost on install two
    rather than install one."""

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    _install(claude_home)
    _install(claude_home)

    record = recorded_replacement(HOST_ID)
    assert record is not None
    assert record["previous_command"] == "modelquad-mcp"


def test_a_recorded_entry_is_restored_whole_not_just_its_command(
    state_root, claude_home
):
    """An entry may carry args or env; putting back only ``command`` would
    quietly discard the rest."""

    original = {"command": "modelquad-mcp", "args": ["--profile", "work"]}
    _write_registration(claude_home, original)
    installed = _install(claude_home)

    before_entry = _read_registration(claude_home)
    ClaudeAdapter().uninstall(claude_home)
    restore_previous_registration(
        HOST_ID,
        claude_home,
        current_command=installed.new_command,
        current_entry=before_entry,
    )

    assert _read_registration(claude_home) == original
