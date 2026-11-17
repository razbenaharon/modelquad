# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The restore outcome, exercised through the real CLI uninstall boundary.

The unit tests call ``restore_previous_registration`` directly, which is how a
defect survived review: the function returned a correct
:class:`RestoreResult`, and the CLI then **threw it away**. ``_run_adapter_command``
discarded the return value entirely, and ``_run_global_adapter_command`` stored
it and then dropped it from the JSON projection. A user uninstalling ModelQuad
was told nothing about the one thing an uninstall puts at risk.

So these tests run ``cli.main([...])`` and read stdout. They cover the whole
chain -- ``_registration_before_uninstall`` -> adapter uninstall ->
``_restore_displaced_registration`` -> rendering -- because that chain is
order-sensitive in both directions: the comparison must happen before the
adapter deletes the key, and the write must happen after.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad import cli
from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.adapters.registration import recorded_replacement

from test_host_registration_transaction import (  # noqa: F401 -- fixtures + helpers
    HOST_ID,
    _read_registration,
    _write_registration,
    claude_home,
    install_host,
    state_root,
)


ENV_VALUE_SENTINEL = "modelquad-test-sentinel-cli-must-not-print-this"


def _install(home: Path):
    """``modelquad install claude``'s path: the real adapter inside the registration transaction."""
    return install_host(home)


def _uninstall(home: Path, *, as_json: bool = False) -> int:
    argv = ["uninstall", "claude", "--target-dir", str(home)]
    if as_json:
        argv.append("--json")
    return cli.main(argv)


# ---------------------------------------------------------------------------
# A. prior registration restored
# ---------------------------------------------------------------------------


def test_uninstall_restores_and_says_so(state_root, claude_home, capsys):
    original = {"command": "modelquad-mcp"}
    _write_registration(claude_home, original)
    _install(claude_home)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) == original
    assert "Restored" in out
    assert "modelquad-mcp" in out


def test_uninstall_restore_is_in_the_json(state_root, claude_home, capsys):
    _write_registration(claude_home, {"command": "modelquad-mcp"})
    _install(claude_home)

    rc = _uninstall(claude_home, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["restore"]["status"] == "restored"
    assert payload["restore"]["restored_command"] == "modelquad-mcp"
    assert payload["restore"]["host"] == HOST_ID


# ---------------------------------------------------------------------------
# B. user changed the registration afterwards -- preserved
# ---------------------------------------------------------------------------


def test_uninstall_preserves_a_later_user_change_and_says_so(
    state_root, claude_home, capsys
):
    """The honest news here is a negative: ModelQuad deliberately did NOT put
    the old value back, because the newer one is the user's decision."""

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    _install(claude_home)

    users_own = {"command": "C:\\Users\\me\\my-own.cmd"}
    _write_registration(claude_home, users_own)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) == users_own
    assert "Kept your current" in out
    assert "not put back" in out


def test_user_modified_is_in_the_json(state_root, claude_home, capsys):
    _write_registration(claude_home, {"command": "modelquad-mcp"})
    _install(claude_home)
    _write_registration(claude_home, {"command": "C:\\mine.cmd"})

    _uninstall(claude_home, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert payload["restore"]["status"] == "user_modified"


# ---------------------------------------------------------------------------
# C. the user already removed the registration (`claude mcp remove modelquad`)
# ---------------------------------------------------------------------------


def _remove_registration(claude_home: Path) -> None:
    path = claude_home.parent / f"{claude_home.name}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["mcpServers"]["modelquad"]
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_an_already_removed_fresh_registration_is_a_clean_uninstall(
    state_root, claude_home, capsys
):
    _install(claude_home)
    _remove_registration(claude_home)

    rc = _uninstall(claude_home, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0, "nothing left to remove is not a failure"
    assert payload["restore"]["status"] == "already_removed"
    assert recorded_replacement(HOST_ID) is None, "nothing is owed back, so nothing is kept"
    assert _read_registration(claude_home) is None


def test_an_already_removed_registration_keeps_the_earlier_entry_on_record(
    state_root, claude_home, capsys
):
    """The user's removal stands; the earlier entry is kept, and they are told where."""

    original = {"command": "modelquad-mcp", "args": ["--mine"]}
    _write_registration(claude_home, original)
    _install(claude_home)
    _remove_registration(claude_home)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) is None, "a removal the user made is not undone"
    assert "already been removed" in out
    assert "host-registration-backup.json" in out
    assert recorded_replacement(HOST_ID)["previous_entry"] == original


# ---------------------------------------------------------------------------
# D. fresh install -- ordinary removal, nothing announced
# ---------------------------------------------------------------------------


def test_a_fresh_install_uninstalls_quietly(state_root, claude_home, capsys):
    """Nothing was displaced, so an uninstall must look exactly as it always
    did -- a restore line here would be noise on every normal machine."""

    _install(claude_home)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) is None
    assert "Registration:" not in out


def test_a_fresh_install_reports_removal_in_the_json(state_root, claude_home, capsys):
    """Quiet on the terminal, explicit in JSON.

    A caller needs to distinguish "removed the registration ModelQuad created"
    from "left the user's own alone" -- they are different outcomes with
    different consequences, and both are reachable from a fresh install.
    """

    _install(claude_home)

    _uninstall(claude_home, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert payload["restore"]["status"] == "removed"
    assert payload["restore"]["restored_command"] is None


# ---------------------------------------------------------------------------
# E. a failed restore is visible, and is a failure
# ---------------------------------------------------------------------------


def test_a_failed_restore_is_reported_and_exits_non_zero(
    state_root, claude_home, capsys, monkeypatch
):
    _write_registration(claude_home, {"command": "modelquad-mcp"})
    _install(claude_home)

    from modelquad.adapters.report import ActionKind, FileAction

    monkeypatch.setattr(
        "modelquad.adapters.jsonmerge.upsert_json_key",
        lambda path, key, value, *, dry_run, backup=True: FileAction(
            path, ActionKind.SKIPPED, detail="unwritable"
        ),
    )

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 1, "a registration that could not be given back is a failure"
    assert "Could not restore" in out
    assert recorded_replacement(HOST_ID) is not None


# ---------------------------------------------------------------------------
# Secrets must not reach the terminal or the JSON
# ---------------------------------------------------------------------------


def test_the_cli_never_prints_a_stored_env_secret(state_root, claude_home, capsys):
    _write_registration(
        claude_home, {"command": "modelquad-mcp", "env": {"ANTHROPIC_API_KEY": ENV_VALUE_SENTINEL}}
    )
    _install(claude_home)

    _uninstall(claude_home)
    out = capsys.readouterr().out

    assert ENV_VALUE_SENTINEL not in out
    # ...even though the restore genuinely put it back on disk.
    assert _read_registration(claude_home)["env"]["ANTHROPIC_API_KEY"] == ENV_VALUE_SENTINEL


def test_the_json_output_never_carries_a_stored_env_secret(
    state_root, claude_home, capsys
):
    _write_registration(
        claude_home, {"command": "modelquad-mcp", "env": {"ANTHROPIC_API_KEY": ENV_VALUE_SENTINEL}}
    )
    _install(claude_home)

    _uninstall(claude_home, as_json=True)
    out = capsys.readouterr().out

    assert ENV_VALUE_SENTINEL not in out
    json.loads(out)  # and it is still well-formed


# ---------------------------------------------------------------------------
# Ordering: the two halves must stay on opposite sides of the removal
# ---------------------------------------------------------------------------


def test_the_comparison_happens_before_the_adapter_deletes_the_key(
    state_root, claude_home, monkeypatch
):
    """If the reading were taken after the removal it would compare against the
    hole the removal just left, and every restore would decline as
    USER_MODIFIED. Asserted by observing what the boundary actually read."""

    _write_registration(claude_home, {"command": "modelquad-mcp"})
    installed = _install(claude_home)

    seen: list[tuple] = []
    real = cli._registration_before_uninstall

    def spy(client, target_dir, method_name):
        result = real(client, target_dir, method_name)
        seen.append(result)
        return result

    monkeypatch.setattr(cli, "_registration_before_uninstall", spy)
    _uninstall(claude_home)

    assert seen, "the boundary must take a reading"
    entry, command = seen[0]
    assert command == installed.new_command, "read before removal, not after"
    assert entry is not None


# ---------------------------------------------------------------------------
# F. the switch to the plugin removes ModelQuad's entry itself; uninstall
#    still owes the user the entry that install replaced
# ---------------------------------------------------------------------------


def _switch_to_plugin(home: Path) -> None:
    from modelquad.adapters.registration import install_with_registration

    install_with_registration(HOST_ID, home, ClaudeAdapter(via_plugin=True))
    assert _read_registration(home) is None


def test_uninstall_after_the_plugin_switch_restores_the_replaced_entry(
    state_root, claude_home, capsys
):
    original = {"command": "modelquad-mcp", "env": {"MY_KEY": ENV_VALUE_SENTINEL}}
    _write_registration(claude_home, original)
    _install(claude_home)
    _switch_to_plugin(claude_home)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) == original
    assert "Restored" in out
    assert ENV_VALUE_SENTINEL not in out
    assert recorded_replacement(HOST_ID) is None, "the env copy is not kept once restored"


def test_a_registration_the_user_removed_after_reinstalling_is_not_put_back(
    state_root, claude_home, capsys
):
    """The plugin switch's mark must not outlive a later regular install."""

    from modelquad.adapters.registration import install_with_registration

    _write_registration(claude_home, {"command": "modelquad-mcp", "env": {"A": "b"}})
    _install(claude_home)
    _switch_to_plugin(claude_home)
    install_with_registration(HOST_ID, claude_home, ClaudeAdapter(via_plugin=False))
    assert _read_registration(claude_home) is not None
    _remove_registration(claude_home)

    assert _uninstall(claude_home) == 0
    assert _read_registration(claude_home) is None
    assert "not put back" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# G. no record in this state folder: only what is recognisably ModelQuad's goes
# ---------------------------------------------------------------------------


def test_uninstall_without_a_record_leaves_a_foreign_entry_and_says_so(
    state_root, claude_home, capsys
):
    foreign = {"command": "my-own-mq", "env": {"MY_KEY": ENV_VALUE_SENTINEL}}
    _write_registration(claude_home, foreign)

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) == foreign
    assert "left in place" in out
    assert ENV_VALUE_SENTINEL not in out


def test_plugin_install_then_uninstall_leaves_a_foreign_entry(state_root, claude_home, capsys):
    foreign = {"command": "my-own-mq"}
    _write_registration(claude_home, foreign)
    from modelquad.adapters.registration import install_with_registration

    install_with_registration(HOST_ID, claude_home, ClaudeAdapter(via_plugin=True))
    assert _read_registration(claude_home) == foreign

    assert _uninstall(claude_home) == 0
    assert _read_registration(claude_home) == foreign


@pytest.mark.parametrize("entry", [
    {"command": "modelquad-mcp", "args": [], "timeout": 900000},
    {"command": "modelquad", "args": ["mcp"]},
    {"command": "C:/tools/Scripts/modelquad-mcp.exe"},
])
def test_uninstall_without_a_record_removes_what_is_plainly_modelquads(
    state_root, claude_home, capsys, entry
):
    """A state folder that moved (an editable install swapped for a plain one)
    leaves no record; ModelQuad's own commands still identify themselves."""

    from modelquad.adapters.codex_hooks import pending_entry

    _write_registration(claude_home, entry)
    reminder = pending_entry(HOST_ID)
    users_hook = {"matcher": "", "hooks": [{"type": "command", "command": "echo mine"}]}
    (claude_home / "settings.json").write_text(json.dumps({
        "statusLine": {"type": "command", "command": "modelquad usage-hook claude"},
        "hooks": {"SessionStart": [users_hook, reminder]},
    }), encoding="utf-8")

    rc = _uninstall(claude_home)
    out = capsys.readouterr().out

    assert rc == 0
    assert _read_registration(claude_home) is None
    settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
    assert "statusLine" not in settings
    assert settings["hooks"]["SessionStart"] == [users_hook]
    assert "no ownership record" not in out


def test_a_reminder_the_user_extended_is_kept_without_a_record(state_root, claude_home):
    from modelquad.adapters.codex_hooks import pending_entry

    reminder = pending_entry(HOST_ID)
    reminder["hooks"][0]["env"] = {"MINE": "1"}
    (claude_home / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [reminder]}}), encoding="utf-8"
    )

    assert _uninstall(claude_home) == 0
    settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"]["SessionStart"] == [reminder]


# ---------------------------------------------------------------------------
# H. uninstall leaves no folder behind for a skill file it wrote itself
# ---------------------------------------------------------------------------


def test_uninstall_leaves_no_skill_folders_and_keeps_a_copy_of_an_edited_skill(
    state_root, claude_home, capsys
):
    _install(claude_home)
    skills = claude_home / "skills"
    edited = skills / "modelquad-review" / "SKILL.md"
    edited.write_bytes(edited.read_bytes() + b"my own note\n")

    assert _uninstall(claude_home) == 0

    left = sorted(p.name for p in skills.iterdir()) if skills.exists() else []
    assert left == ["modelquad-review"], left
    copies = list((skills / "modelquad-review").iterdir())
    assert len(copies) == 1 and b"my own note" in copies[0].read_bytes()
