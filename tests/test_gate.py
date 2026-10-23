# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import modelquad
from modelquad import hostobs
from modelquad.adapters import ActionKind
from modelquad.adapters.gate import (
    ADVISORY_NOTE,
    GATED_TOOL_NAMES,
    evaluate_gate,
)
from modelquad.adapters.hooksmerge import (
    has_hook_entry,
    remove_hook_entry,
    upsert_hook_entry,
)
from modelquad.execution.base import WORKER_ENV_VAR




# ---------------------------------------------------------------------------
# Module source inspection & isolation tests (Requirements 5 & 8)
# ---------------------------------------------------------------------------


def test_gate_and_hooksmerge_source_does_not_reference_legacy_routing():
    """Confirm the legacy path (router.py, modelquad.md) is never referenced.

    gate.py and hooksmerge.py must never import or reference the retired
    router.py or modelquad.md legacy chain.
    """
    repo_src = Path(modelquad.__file__).resolve().parent
    gate_source = (repo_src / "adapters" / "gate.py").read_text(encoding="utf-8")
    hooksmerge_source = (repo_src / "adapters" / "hooksmerge.py").read_text(encoding="utf-8")
    cli_source = (repo_src / "cli.py").read_text(encoding="utf-8")

    for path_name, text in [
        ("gate.py", gate_source),
        ("hooksmerge.py", hooksmerge_source),
    ]:
        assert "router.py" not in text, f"Legacy router.py referenced in {path_name}"
        assert "modelquad.md" not in text, f"Legacy modelquad.md referenced in {path_name}"

    # Verify hook-gate wiring in cli.py does not reference legacy routing
    hook_gate_section = cli_source[cli_source.find('args.command == "hook-gate"') :]
    hook_gate_section = hook_gate_section[: hook_gate_section.find("return 0")]
    assert "router.py" not in hook_gate_section
    assert "modelquad.md" not in hook_gate_section


def test_gate_source_omits_modelquad_routing_business_logic():
    """Routing business logic stays absent from the host policy layer.

    gate.py must only decide *whether* to consult ModelQuad, never
    *which* provider/model/effort to pick.
    """
    repo_src = Path(modelquad.__file__).resolve().parent
    gate_source = (repo_src / "adapters" / "gate.py").read_text(encoding="utf-8")

    forbidden_terms = ("task_fit", "quota_penalty", "resource_strategy", "reasoning_effort")
    for term in forbidden_terms:
        assert term not in gate_source, f"Forbidden routing concept {term!r} found in gate.py"




# ---------------------------------------------------------------------------
# gate.py: the advisory hook
#
# The gate never decides a tool call. Its only output is nothing, or one
# advisory note on the first gated call of a session.
# ---------------------------------------------------------------------------

ADVISORY_OUTPUT = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ADVISORY_NOTE}}
SESSION = "raw-session-id-4f1c9e2a-6b8d-4e0f-a1c3-5d7b9f2e8a64"


def _gate(state_dir, *, tool_name="Bash", session_id=SESSION, environ=None, **kwargs):
    return evaluate_gate(
        tool_name=tool_name,
        session_id=session_id,
        environ={} if environ is None else environ,
        state_dir=state_dir,
        **kwargs,
    )


def test_gated_tool_names_match_the_installed_matcher():
    assert GATED_TOOL_NAMES == frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Task"})


def test_advisory_note_is_short_read_only_and_names_no_removed_tool():
    assert len(ADVISORY_NOTE) <= 400
    assert "read-only" in ADVISORY_NOTE
    assert "blocks and approves nothing" in ADVISORY_NOTE
    # The route tool and the host-asserted invocation mode left the release.
    assert "modelquad_route" not in ADVISORY_NOTE
    assert "invocation_mode" not in ADVISORY_NOTE


def test_first_gated_call_of_a_session_gets_the_note_and_later_calls_nothing(tmp_path):
    state = tmp_path / "gate-state"

    assert _gate(state) == ADVISORY_OUTPUT
    for tool in sorted(GATED_TOOL_NAMES):
        assert _gate(state, tool_name=tool) == {}


def test_each_session_gets_its_own_single_note(tmp_path):
    state = tmp_path / "gate-state"

    assert _gate(state, session_id="session-a") == ADVISORY_OUTPUT
    assert _gate(state, session_id="session-b") == ADVISORY_OUTPUT
    assert _gate(state, session_id="session-a") == {}
    assert _gate(state, session_id="session-b") == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"environ": {WORKER_ENV_VAR: "1"}}, id="worker"),
        pytest.param({"routing_paused": True}, id="paused"),
        pytest.param({"tool_name": "Read"}, id="non-gated"),
        pytest.param({"tool_name": ""}, id="no-tool-name"),
        pytest.param({"session_id": None}, id="no-session-id"),
        pytest.param({"session_id": "   "}, id="blank-session-id"),
    ],
)
def test_quiet_cases_emit_nothing_and_record_no_marker(tmp_path, kwargs):
    state = tmp_path / "gate-state"

    assert _gate(state, **kwargs) == {}
    assert not state.exists() or not any(state.iterdir())


@pytest.mark.parametrize("mode", ["automatic", "ask", "manual", None])
def test_modes_do_not_suppress_the_advisory_note(tmp_path, mode):
    assert _gate(tmp_path / "gate-state", routing_mode=mode) == ADVISORY_OUTPUT


def test_a_quiet_case_does_not_spend_the_sessions_note(tmp_path):
    """Paused, then resumed: the session's one note is still available."""

    state = tmp_path / "gate-state"

    assert _gate(state, routing_paused=True) == {}
    assert _gate(state) == ADVISORY_OUTPUT


def test_marker_is_a_zero_byte_file_named_by_fingerprint_never_the_raw_id(tmp_path):
    state = tmp_path / "gate-state"

    assert _gate(state) == ADVISORY_OUTPUT

    files = list(state.iterdir())
    assert [f.name for f in files] == [f"{hostobs.session_fingerprint(SESSION)}.advised"]
    assert all(f.stat().st_size == 0 for f in files)
    assert all(SESSION not in f.name for f in files)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="first-gated-call"),
        pytest.param({"environ": {WORKER_ENV_VAR: "1"}}, id="worker"),
        pytest.param({"tool_name": "Read"}, id="non-gated"),
    ],
)
def test_old_gate_raw_id_markers_are_deleted_on_the_next_run(tmp_path, kwargs):
    state = tmp_path / "gate-state"
    state.mkdir()
    for name in (f"{SESSION}.0.prompted", f"{SESSION}.3.prompted", "other_session.1.prompted"):
        (state / name).write_text("", encoding="utf-8")

    _gate(state, **kwargs)

    assert not list(state.glob("*.prompted"))


def test_markers_past_retention_are_pruned_and_fresh_ones_kept(tmp_path):
    state = tmp_path / "gate-state"
    state.mkdir()
    stale = state / "0123456789abcdef.advised"
    fresh = state / "fedcba9876543210.advised"
    stale.write_text("", encoding="utf-8")
    fresh.write_text("", encoding="utf-8")
    old = time.time() - 8 * 24 * 3600
    os.utime(stale, (old, old))

    _gate(state)

    assert not stale.exists()
    assert fresh.exists()


def test_unwritable_state_directory_emits_nothing_on_every_call(tmp_path, monkeypatch):
    def refuse(self, *args, **kwargs):
        raise PermissionError("read-only state directory")

    monkeypatch.setattr(Path, "touch", refuse)
    state = tmp_path / "gate-state"

    assert _gate(state) == {}
    assert _gate(state) == {}


def test_state_directory_path_that_is_a_file_emits_nothing(tmp_path):
    blocker = tmp_path / "gate-state"
    blocker.write_text("not a directory", encoding="utf-8")

    assert _gate(blocker) == {}


@pytest.mark.parametrize("failure", ["none", "raises"])
def test_unavailable_fingerprint_emits_nothing_and_records_nothing(tmp_path, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        if failure == "raises":
            raise RuntimeError("identity store unreadable")
        return None

    monkeypatch.setattr(hostobs, "session_fingerprint", unavailable)
    state = tmp_path / "gate-state"

    assert _gate(state) == {}
    assert _gate(state) == {}
    assert not state.exists() or not any(state.iterdir())


def test_hostile_session_id_never_raises_and_never_reaches_the_file_name(tmp_path):
    hostile = 'bad:id\\with*chars?"<>|/../..'
    state = tmp_path / "gate-state"

    assert _gate(state, session_id=hostile) == ADVISORY_OUTPUT
    assert _gate(state, session_id=hostile) == {}
    assert all(f.parent == state for f in state.iterdir())


def test_output_is_json_serialisable_and_never_a_decision(tmp_path):
    state = tmp_path / "gate-state"
    for output in (_gate(state), _gate(state)):
        text = json.dumps(output)
        for key in ("permissionDecision", "updatedInput", '"decision"', '"continue"', "systemMessage"):
            assert key not in text




# ---------------------------------------------------------------------------
# hooksmerge.py Unit Tests
# ---------------------------------------------------------------------------


def test_upsert_hook_entry_creates_new_file_and_structures_json(tmp_path):
    settings_path = tmp_path / "settings.json"
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-cmd"}]}

    action = upsert_hook_entry(settings_path, "PreToolUse", entry, "my-cmd", dry_run=False)

    assert action.kind is ActionKind.CREATED
    assert settings_path.is_file()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload == {"hooks": {"PreToolUse": [entry]}}
    assert has_hook_entry(settings_path, "PreToolUse", "my-cmd") is True


def test_upsert_hook_entry_idempotence_and_preservation(tmp_path):
    settings_path = tmp_path / "settings.json"
    initial = {
        "theme": "dark",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Unrelated", "hooks": [{"type": "command", "command": "unrelated-cmd"}]}
            ]
        },
    }
    settings_path.write_text(json.dumps(initial, indent=2), encoding="utf-8")
    owned_entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "owned-cmd"}]}

    # First upsert: adds to existing array
    action1 = upsert_hook_entry(settings_path, "PreToolUse", owned_entry, "owned-cmd", dry_run=False)
    assert action1.kind is ActionKind.UPDATED
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload["theme"] == "dark"
    assert len(payload["hooks"]["PreToolUse"]) == 2
    assert payload["hooks"]["PreToolUse"][0]["matcher"] == "Unrelated"
    assert payload["hooks"]["PreToolUse"][1]["matcher"] == "Bash"

    # Second upsert: identical entry -> UNCHANGED
    bytes_before = settings_path.read_bytes()
    action2 = upsert_hook_entry(settings_path, "PreToolUse", owned_entry, "owned-cmd", dry_run=False)
    assert action2.kind is ActionKind.UNCHANGED
    assert settings_path.read_bytes() == bytes_before

    # Modified owned entry: updates in place at same index
    modified_entry = {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": "owned-cmd"}]}
    action3 = upsert_hook_entry(settings_path, "PreToolUse", modified_entry, "owned-cmd", dry_run=False)
    assert action3.kind is ActionKind.UPDATED
    updated_payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(updated_payload["hooks"]["PreToolUse"]) == 2
    assert updated_payload["hooks"]["PreToolUse"][1]["matcher"] == "Bash|Edit"


def test_upsert_hook_entry_preserves_sibling_hook_manually_added_to_owned_group(tmp_path):
    """Regression test for an independent-review finding: if a user manually
    added a second hook command into the same group we own (matching
    matcher), a later upsert (e.g. from `modelquad install claude` re-run
    for repair/upgrade) must not silently delete it by replacing the whole
    group object.
    """
    settings_path = tmp_path / "settings.json"
    initial = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "owned-cmd"},
                        {"type": "command", "command": "users-custom-logger"},
                    ],
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(initial), encoding="utf-8")

    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "owned-cmd"}]}
    action = upsert_hook_entry(settings_path, "PreToolUse", entry, "owned-cmd", dry_run=False)

    # Reordering our own entry to the end of the group's hooks list still
    # counts as a change, but nothing was lost -- the key guarantee under
    # test is that the sibling command survives.
    assert action.kind is ActionKind.UPDATED
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(payload["hooks"]["PreToolUse"]) == 1
    commands = [hook["command"] for hook in payload["hooks"]["PreToolUse"][0]["hooks"]]
    assert "users-custom-logger" in commands
    assert "owned-cmd" in commands

    # Re-running the identical upsert again is now genuinely idempotent.
    action2 = upsert_hook_entry(settings_path, "PreToolUse", entry, "owned-cmd", dry_run=False)
    assert action2.kind is ActionKind.UNCHANGED


def test_upsert_hook_entry_dry_run_never_writes_file(tmp_path):
    settings_path = tmp_path / "nonexistent" / "settings.json"
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-cmd"}]}

    action = upsert_hook_entry(settings_path, "PreToolUse", entry, "my-cmd", dry_run=True)
    assert action.kind is ActionKind.WOULD_CREATE
    assert not settings_path.exists()

    # Dry run on existing file
    existing_path = tmp_path / "existing.json"
    existing_path.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    action_existing = upsert_hook_entry(existing_path, "PreToolUse", entry, "my-cmd", dry_run=True)
    assert action_existing.kind is ActionKind.WOULD_UPDATE
    assert json.loads(existing_path.read_text(encoding="utf-8")) == {"theme": "light"}


def test_upsert_hook_entry_skips_malformed_json_and_invalid_schema(tmp_path):
    # Malformed JSON
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    action1 = upsert_hook_entry(bad_json, "PreToolUse", {}, "cmd", dry_run=False)
    assert action1.kind is ActionKind.SKIPPED
    assert "Could not safely parse JSON config" in (action1.detail or "")

    # Non-dict root
    non_dict = tmp_path / "array.json"
    non_dict.write_text("[1, 2, 3]", encoding="utf-8")
    action2 = upsert_hook_entry(non_dict, "PreToolUse", {}, "cmd", dry_run=False)
    assert action2.kind is ActionKind.SKIPPED
    assert "top-level value is not an object" in (action2.detail or "")

    # 'hooks' is not an object
    bad_hooks = tmp_path / "bad_hooks.json"
    bad_hooks.write_text(json.dumps({"hooks": "invalid"}), encoding="utf-8")
    action3 = upsert_hook_entry(bad_hooks, "PreToolUse", {}, "cmd", dry_run=False)
    assert action3.kind is ActionKind.SKIPPED
    assert "'hooks' is not an object" in (action3.detail or "")

    # 'hooks.PreToolUse' is not an array
    bad_event = tmp_path / "bad_event.json"
    bad_event.write_text(json.dumps({"hooks": {"PreToolUse": "invalid"}}), encoding="utf-8")
    action4 = upsert_hook_entry(bad_event, "PreToolUse", {}, "cmd", dry_run=False)
    assert action4.kind is ActionKind.SKIPPED
    assert "'hooks.PreToolUse' is not an array" in (action4.detail or "")


def test_remove_hook_entry_surgical_removal_and_cleanup(tmp_path):
    settings_path = tmp_path / "settings.json"

    # Non-existent file -> UNCHANGED
    assert remove_hook_entry(settings_path, "PreToolUse", "owned-cmd", dry_run=False).kind is ActionKind.UNCHANGED

    # Sole hook in group and event -> cleans up empty event and hooks section
    initial = {
        "model": "sonnet",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "owned-cmd"}]}
            ]
        },
    }
    settings_path.write_text(json.dumps(initial), encoding="utf-8")

    # Dry run
    dry_action = remove_hook_entry(settings_path, "PreToolUse", "owned-cmd", dry_run=True)
    assert dry_action.kind is ActionKind.WOULD_REMOVE
    assert "hooks" in json.loads(settings_path.read_text(encoding="utf-8"))

    # Real removal
    action = remove_hook_entry(settings_path, "PreToolUse", "owned-cmd", dry_run=False)
    assert action.kind is ActionKind.REMOVED
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload == {"model": "sonnet"}
    assert "hooks" not in payload

    # Repeated removal -> UNCHANGED
    assert remove_hook_entry(settings_path, "PreToolUse", "owned-cmd", dry_run=False).kind is ActionKind.UNCHANGED


def test_remove_hook_entry_preserves_sibling_hooks_in_same_group(tmp_path):
    settings_path = tmp_path / "settings.json"
    initial = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "unrelated-cmd"},
                        {"type": "command", "command": "owned-cmd"},
                    ],
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(initial), encoding="utf-8")

    action = remove_hook_entry(settings_path, "PreToolUse", "owned-cmd", dry_run=False)
    assert action.kind is ActionKind.REMOVED
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    group = payload["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "Bash"
    assert group["hooks"] == [{"type": "command", "command": "unrelated-cmd"}]


def test_has_hook_entry_checks(tmp_path):
    settings_path = tmp_path / "settings.json"
    assert has_hook_entry(settings_path, "PreToolUse", "cmd") is False

    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "cmd"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert has_hook_entry(settings_path, "PreToolUse", "cmd") is True
    assert has_hook_entry(settings_path, "PreToolUse", "other-cmd") is False
    assert has_hook_entry(settings_path, "PostToolUse", "cmd") is False

    # Corrupt JSON -> False
    settings_path.write_text("{bad json", encoding="utf-8")
    assert has_hook_entry(settings_path, "PreToolUse", "cmd") is False

