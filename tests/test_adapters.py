# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from contextlib import nullcontext

import pytest

from modelquad.adapters import ActionKind, default_target_dir
from modelquad.adapters.claude_adapter import (
    ClaudeAdapter,
    _status_line_command as _claude_status_line_command,
)
from modelquad.adapters.paths import adapters_template_dir
from modelquad.adapters.textblock import render_block, remove_block, upsert_block


@pytest.fixture(autouse=True)
def prevent_real_home_access(monkeypatch, tmp_path):
    """Make accidental default-target use fail instead of touching a home directory."""

    def fail_home_lookup():
        raise AssertionError("adapter tests must pass explicit tmp_path target directories")

    # Resolve/import "modelquad.cli" (and its transitive Path.home() use at
    # providers/base.py import time) *before* patching Path.home below --
    # otherwise running this file in isolation imports modelquad.cli for
    # the first time only once Path.home is already broken, failing at
    # collection instead of during an actual test.
    monkeypatch.setattr("modelquad.cli.default_target_dir", lambda client: fail_home_lookup())
    # `Path` is one shared class object across every module that imports it
    # (`from pathlib import Path`), so patching `.home` via any single
    # module path protects every default-home lookup.
    monkeypatch.setattr("modelquad.adapters.paths.Path.home", fail_home_lookup)
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "isolated-appdata"))
    return tmp_path


def _install_kwargs_for(adapter_type, target: Path) -> dict:
    """Active adapters take their complete location from the explicit target."""
    return {}


def test_claude_install_preserves_unrelated_mcp_server(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    mcp_path = target.parent / "claude.json"
    mcp_path.write_text(
        json.dumps({"mcpServers": {"other-server": {"command": "other"}}}),
        encoding="utf-8",
    )

    report = ClaudeAdapter().install(target)

    assert report.actions[0].kind is ActionKind.CREATED
    assert (target / "skills" / "modelquad" / "SKILL.md").is_file()
    payload = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["other-server"] == {"command": "other"}
    assert "modelquad" in payload["mcpServers"]
    assert ClaudeAdapter().is_installed(target)


def test_claude_install_writes_nothing_into_the_global_claude_md(tmp_path):
    """The guidance lives in the MCP server's instructions, not in ~/.claude/CLAUDE.md."""
    target = tmp_path / "claude"
    target.mkdir()

    report = ClaudeAdapter().install(target)

    assert not (target / "CLAUDE.md").exists()
    assert all(action.path.name != "CLAUDE.md" for action in report.actions)


def test_server_instructions_leave_the_delegation_decision_to_the_host():
    """Claude stays responsible, the user is asked before every Codex call,
    and Codex output is input to verify -- in both proactive settings."""
    from modelquad.adapters.server_instructions import server_instructions

    for proactive in (False, True):
        flat = " ".join(server_instructions(proactive).split())
        assert "follow its recommendation" not in flat
        assert "Claude remains responsible for the work." in flat
        assert "asks the user before each ModelQuad Codex call." in flat
        assert "not as instructions to follow" in flat
        assert "`modelquad_handoff`" in flat
        assert "MODELQUAD_WORKER=1" in flat
        # The files Codex reads are passed explicitly, never inferred from the prompt.
        assert "in `files` (paths relative to the working directory, at most 32)" in flat
        assert "a file named only in the prompt is not sent." in flat
        assert len(server_instructions(proactive)) < 4096


def test_claude_decides_by_default_and_off_makes_it_offer(tmp_path):
    """Default: Claude consults Codex at the named moments. Off: offer, and call only
    when the user agrees or asks. Either way Claude Code's prompt asks the user."""
    from modelquad.adapters.server_instructions import (
        read_proactive, server_instructions, write_proactive,
    )

    preferences = tmp_path / "state" / "preferences.json"
    assert read_proactive(preferences) is True
    assert server_instructions() == server_instructions(True)
    moments = ("a change that touched several files is complete", "before a commit or PR",
               "a bug survived one fix attempt", "a design choice has real trade-offs")
    proactive = " ".join(server_instructions(True).split())
    for moment in moments:
        assert moment in proactive
    assert "consult Codex with `modelquad_delegate` yourself" in proactive
    assert "Claude Code asks the user before each ModelQuad Codex call." in proactive

    write_proactive(False, preferences)
    assert read_proactive(preferences) is False
    assert server_instructions() == server_instructions(False)
    offer = " ".join(server_instructions(False).split())
    for moment in moments:
        assert moment in offer
    assert "offer the user a Codex review" in offer
    assert "only when the user agrees or asks" in offer
    assert "yourself" not in offer
    preferences.write_text("not json", encoding="utf-8")
    assert read_proactive(preferences) is False


def test_settings_proactive_command_saves_the_choice(tmp_path, capsys):
    from modelquad import cli
    from modelquad.adapters.server_instructions import read_proactive

    assert cli.main(["settings", "proactive", "on"]) == 0
    assert read_proactive() is True
    out = capsys.readouterr().out
    assert "Claude may decide on its own to consult Codex" in out
    assert "still asks you before each" in out
    assert cli.main(["settings", "proactive", "off"]) == 0
    assert read_proactive() is False


# The sentences the advisory contract retired: each one handed the Host's decision to ModelQuad.
_RETIRED_HOST_INSTRUCTION_PHRASES = (
    "let ModelQuad choose",
    "follow its recommendation",
    "Treat the recommendation as the default",
    "Override it only for",
    "delegate-worthy",
    "execute that recommendation",
    "fallback rules",
)

# A block as the earlier Claude template wrote it, verbatim in the parts the
# advisory contract retires, so a re-install is proven to take it back.
_PRE_448_CLAUDE_BLOCK = """# ModelQuad

For non-trivial repository, engineering, implementation, debugging, testing,
review, or architecture work, consult the globally installed ModelQuad MCP
before substantive execution. Use its `modelquad_route` / `modelquad_delegate`
tools (or their CLI equivalents) and let ModelQuad choose the provider,
stage, and delegation strategy.

Once `modelquad_route` has been consulted, follow its recommendation rather
than treating it as optional background information. Do not silently ignore a
delegate-worthy recommendation just because doing the work yourself feels
faster in the moment.
"""

_HOST_INSTRUCTION_CLIENTS = [
    (ClaudeAdapter, "claude"),
]


def _installed_instruction_texts(target: Path) -> dict[str, str]:
    from modelquad.adapters.server_instructions import server_instructions

    return {
        "skills/modelquad/SKILL.md": (target / "skills/modelquad/SKILL.md").read_text(encoding="utf-8"),
        "server instructions (default)": server_instructions(False),
        "server instructions (proactive)": server_instructions(True),
    }


@pytest.mark.parametrize(("adapter_type", "client"), _HOST_INSTRUCTION_CLIENTS)
def test_installed_host_instructions_state_the_advisory_contract(tmp_path, adapter_type, client):
    """The skill and the server instructions say the user approves each call,
    that Codex through ModelQuad is read-only and its answer is input to
    verify, and keep the worker guard and project-scoped job tools."""
    target = tmp_path / client
    adapter_type().install(target, **_install_kwargs_for(adapter_type, target))

    for name, text in _installed_instruction_texts(target).items():
        flat = " ".join(text.split())
        for phrase in _RETIRED_HOST_INSTRUCTION_PHRASES:
            assert phrase not in flat, f"{client} {name} still says {phrase!r}"
        # The user approves every call; Codex through ModelQuad is read-only.
        assert "asks the user before" in flat, name
        assert "read-only" in flat, name
        assert re.search(r"not (?:as )?instructions", flat), name
        # Writing changes belongs to OpenAI's Codex plugin, not to ModelQuad.
        assert "`/codex:rescue`" in flat, name
        # Unchanged obligations: the worker guard and job scoping.
        assert "MODELQUAD_WORKER=1" in flat, name
        assert "`modelquad_job_*`" in flat and "absolute project directory" in flat, name
        # Files reach Codex only through `files`; the offer after an important task.
        assert "a file named only in the prompt is not sent." in flat, name
        assert "before a commit or PR" in flat, name
        # Removed tools and commands are never named.
        for removed in ("modelquad_route", "`modelquad route`", "delegate_task", "modelquad delegate",
                        "second_opinion"):
            assert removed not in flat, (name, removed)


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_install_and_uninstall_take_back_a_block_an_earlier_version_wrote(tmp_path, command):
    """The old managed block goes, the user's own lines stay, a backup is kept."""
    target = tmp_path / "claude"
    target.mkdir()
    claude_md = target / "CLAUDE.md"
    previous, _ = upsert_block("# Personal notes\n@~/.claude/modelquad.md\n", _PRE_448_CLAUDE_BLOCK,
                               comment_prefix="<!--", marker_suffix="-->")
    claude_md.write_text(previous, encoding="utf-8")
    skill = target / "skills" / "modelquad" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Once routing recommends a provider, execute that recommendation.\n", encoding="utf-8")

    report = getattr(ClaudeAdapter(), command)(target)

    assert claude_md.read_text(encoding="utf-8").rstrip("\n") == "# Personal notes\n@~/.claude/modelquad.md"
    action = next(a for a in report.actions if a.path.name == "CLAUDE.md")
    assert action.kind is ActionKind.REMOVED
    assert "earlier version" in action.detail
    assert action.backup_path.read_text(encoding="utf-8") == previous
    if command == "install":
        flat = " ".join(skill.read_text(encoding="utf-8").split())
        for phrase in _RETIRED_HOST_INSTRUCTION_PHRASES:
            assert phrase not in flat

    repeated = getattr(ClaudeAdapter(), command)(target)
    assert all(a.path.name != "CLAUDE.md" for a in repeated.actions)


def test_server_instructions_omit_routing_business_logic():
    from modelquad.adapters.server_instructions import server_instructions

    for proactive in (False, True):
        text = server_instructions(proactive)
        for term in ("fit_weight", "usage_weight", "score", "0.75", "0.25", "subprocess"):
            assert term not in text


def test_claude_install_leaves_a_users_claude_md_byte_identical(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    claude_md = target / "CLAUDE.md"
    original = b"# My personal notes\r\nAlways use tabs.\n@~/.claude/modelquad.md\n"
    claude_md.write_bytes(original)

    first = ClaudeAdapter().install(target)
    second = ClaudeAdapter().install(target)
    ClaudeAdapter().uninstall(target)

    assert claude_md.read_bytes() == original
    assert all(a.path.name != "CLAUDE.md" for a in (*first.actions, *second.actions))
    assert not list(target.glob("CLAUDE.md.*backup*"))


def test_claude_is_installed_requires_the_skill_and_the_server_entry(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()

    assert not ClaudeAdapter().is_installed(target)
    ClaudeAdapter().install(target)
    assert ClaudeAdapter().is_installed(target)
    assert not (target / "CLAUDE.md").exists()

    config = target.parent / "claude.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    del payload["mcpServers"]["modelquad"]
    config.write_text(json.dumps(payload), encoding="utf-8")
    assert not ClaudeAdapter().is_installed(target)

    ClaudeAdapter().install(target)
    (target / "skills" / "modelquad" / "SKILL.md").unlink()
    assert not ClaudeAdapter().is_installed(target)


def test_claude_home_policy_block_survives_malformed_duplicate_markers(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    start = "<!-- >>> modelquad managed block >>> -->\n"
    end = "<!-- <<< modelquad managed block <<< -->\n"
    claude_md = target / "CLAUDE.md"
    original = f"{start}user content that must survive\n{start}real content\n{end}"
    claude_md.write_text(original, encoding="utf-8")

    report = ClaudeAdapter().install(target)

    home_policy_action = next(a for a in report.actions if a.path.name == "CLAUDE.md")
    assert home_policy_action.kind is ActionKind.SKIPPED
    assert claude_md.read_text(encoding="utf-8") == original


def test_claude_install_preserves_a_status_line_script_the_user_runs(tmp_path):
    """A loose status-line script is the user's choice, not something to migrate."""
    target = tmp_path / "claude"
    target.mkdir()
    settings_path = target / "settings.json"
    script = "python \"C:\\Users\\example\\.claude\\statusline.py\""
    settings_path.write_text(
        json.dumps({"statusLine": {"command": script}, "unrelatedSetting": True}),
        encoding="utf-8",
    )

    report = ClaudeAdapter().install(target)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["statusLine"]["command"] == script
    assert settings["unrelatedSetting"] is True
    assert report.actions[2].kind is ActionKind.SKIPPED


def test_claude_uninstall_removes_status_line_command_only(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    ClaudeAdapter().install(target)

    ClaudeAdapter().uninstall(target)

    settings = json.loads((target / "settings.json").read_text(encoding="utf-8"))
    assert "statusLine" not in settings


def _status_line_action(report, settings_path):
    """The status line is the first settings.json action (then the startup reminder)."""
    actions = [a for a in report.actions if Path(a.path) == settings_path]
    return actions[0]


def test_claude_status_line_carries_type_command(tmp_path):
    """Claude Code ignores a statusLine without "type": "command" (2026-10-06)."""
    target = tmp_path / "claude"
    target.mkdir()
    ClaudeAdapter().install(target)
    settings = json.loads((target / "settings.json").read_text(encoding="utf-8"))
    assert settings["statusLine"] == {"type": "command", "command": _claude_status_line_command()}


def test_claude_reinstall_repairs_typeless_owned_status_line_and_keeps_extras(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    settings_path = target / "settings.json"
    settings_path.write_text(json.dumps({"statusLine": {
        "command": _claude_status_line_command(), "padding": 0,
    }}), encoding="utf-8")
    untouched = settings_path.read_bytes()
    assert _status_line_action(ClaudeAdapter().install(target, dry_run=True), settings_path).kind         == ActionKind.WOULD_UPDATE
    assert settings_path.read_bytes() == untouched
    ClaudeAdapter().install(target)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["statusLine"] == {
        "type": "command", "command": _claude_status_line_command(), "padding": 0,
    }
    before = settings_path.read_bytes()
    assert _status_line_action(ClaudeAdapter().install(target), settings_path).kind == ActionKind.UNCHANGED
    assert settings_path.read_bytes() == before  # idempotent


def test_claude_install_skips_non_object_status_line(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    settings_path = target / "settings.json"
    settings_path.write_text(json.dumps({"statusLine": "user-value"}), encoding="utf-8")
    assert _status_line_action(ClaudeAdapter().install(target), settings_path).kind == ActionKind.SKIPPED
    assert json.loads(settings_path.read_text(encoding="utf-8"))["statusLine"] == "user-value"


_USER_HOOK_GROUP = {
    "matcher": "SomeOtherTool",
    "hooks": [{"type": "command", "command": "some-other-tool"}],
}
_OLD_GATE_GROUP = {
    "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit|Task",
    "hooks": [{"type": "command", "command": "modelquad hook-gate"}],
}


def test_claude_install_adds_no_gate_hook_and_keeps_the_users_hooks(tmp_path):
    """The advisory gate is no longer installed: the server instructions carry its note."""
    target = tmp_path / "claude"
    target.mkdir()
    settings_path = target / "settings.json"
    settings_path.write_text(
        json.dumps({"model": "sonnet", "hooks": {"PreToolUse": [_USER_HOOK_GROUP]}}),
        encoding="utf-8",
    )

    ClaudeAdapter().install(target)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "sonnet"
    assert settings["statusLine"]["command"] == "modelquad usage-hook claude"
    assert settings["hooks"]["PreToolUse"] == [_USER_HOOK_GROUP]
    assert ClaudeAdapter().is_installed(target)


def test_claude_second_install_is_idempotent_for_settings(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    adapter = ClaudeAdapter()
    adapter.install(target)
    settings_path = target / "settings.json"
    bytes_after_first = settings_path.read_bytes()

    report = adapter.install(target)

    assert settings_path.read_bytes() == bytes_after_first
    assert report.unchanged


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_an_owned_gate_an_earlier_version_wrote_is_removed(tmp_path, command):
    target = tmp_path / "claude"
    target.mkdir()
    settings_path = target / "settings.json"
    stale = {"matcher": "Bash", "hooks": [{"type": "command", "command": '"C:/old/modelquad.exe" hook-gate'}]}
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-tool hook-gate"}]}
    settings_path.write_text(json.dumps({"model": "sonnet", "hooks": {"PreToolUse": [
        _USER_HOOK_GROUP, _OLD_GATE_GROUP, stale, foreign,
    ]}}), encoding="utf-8")

    report = getattr(ClaudeAdapter(), command)(target)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "sonnet"
    assert settings["hooks"]["PreToolUse"] == [_USER_HOOK_GROUP, foreign]
    removed = [a for a in report.actions if a.detail and "PreToolUse hook" in a.detail]
    assert removed and all(a.kind is ActionKind.REMOVED for a in removed)
    repeated = getattr(ClaudeAdapter(), command)(target)
    assert not [a for a in repeated.actions if a.detail and "PreToolUse hook" in a.detail]


@pytest.mark.parametrize(
    ("adapter_type", "client"),
    [
        (ClaudeAdapter, "claude"),
    ],
)
def test_second_install_is_idempotent_and_byte_identical(tmp_path, adapter_type, client):
    target = tmp_path / client
    adapter = adapter_type()
    install_kwargs = _install_kwargs_for(adapter_type, target)
    adapter.install(target, **install_kwargs)
    bytes_after_first = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    report = adapter.install(target, **install_kwargs)

    assert report.unchanged
    assert all(action.kind is ActionKind.UNCHANGED for action in report.actions)
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == bytes_after_first


@pytest.mark.parametrize(
    ("adapter_type", "client"),
    [
        (ClaudeAdapter, "claude"),
    ],
)
def test_uninstall_removes_only_owned_content_and_is_safe_when_repeated(tmp_path, adapter_type, client):
    target = tmp_path / client
    target.mkdir()
    if client == "claude":
        (target.parent / "claude.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "other"}}}),
            encoding="utf-8",
        )
    else:
        (target / "AGENTS.md").write_text("personal agents\n", encoding="utf-8")
        (target / "config.toml").write_text("[personal]\nvalue = 1\n", encoding="utf-8")
    adapter = adapter_type()
    install_kwargs = _install_kwargs_for(adapter_type, target)
    adapter.install(target, **install_kwargs)

    report = adapter.uninstall(target, **install_kwargs)
    repeated = adapter.uninstall(target, **install_kwargs)

    assert all(action.kind in {ActionKind.REMOVED, ActionKind.UNCHANGED} for action in report.actions)
    assert all(action.kind is ActionKind.UNCHANGED for action in repeated.actions)
    assert not adapter.is_installed(target)
    if client == "claude":
        payload = json.loads((target.parent / "claude.json").read_text(encoding="utf-8"))
        assert payload["mcpServers"] == {"other": {"command": "other"}}
    else:
        assert "personal agents" in (target / "AGENTS.md").read_text(encoding="utf-8")
        assert "[personal]" in (target / "config.toml").read_text(encoding="utf-8")


def test_missing_directory_is_created_only_for_a_real_install(tmp_path):
    target = tmp_path / "does-not-exist"

    ClaudeAdapter().install(target)

    assert (target / "skills" / "modelquad" / "SKILL.md").is_file()


def test_invalid_json_is_skipped_without_overwriting_user_configuration(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    malformed = target.parent / "claude.json"
    malformed.write_text("{not json", encoding="utf-8")

    report = ClaudeAdapter().install(target)

    assert report.actions[1].kind is ActionKind.SKIPPED
    assert malformed.read_text(encoding="utf-8") == "{not json"


def test_textblock_html_marker_convention_is_stable():
    block = render_block("content", comment_prefix="<!--", marker_suffix="-->")

    assert block.splitlines()[0] == "<!-- >>> modelquad managed block >>> -->"
    assert block.splitlines()[-1] == "<!-- <<< modelquad managed block <<< -->"


def test_adapter_templates_do_not_contain_machine_specific_paths():
    actual_home = os.path.expanduser("~")
    windows_home = re.compile(r"[A-Za-z]:\\Users\\")

    with adapters_template_dir("claude") as claude_dir:
        template_root = claude_dir.parent
        for path in template_root.rglob("*"):
            if path.is_file():
                contents = path.read_text(encoding="utf-8")
                assert windows_home.search(contents) is None
                assert actual_home not in contents


def test_default_target_dir_is_not_used_by_this_test_module():
    with pytest.raises(AssertionError, match="explicit tmp_path"):
        default_target_dir("claude")


def test_upsert_block_survives_backslashes_in_content_without_crashing():
    # Regression test: re.sub interprets backslash escapes/backreferences
    # (\1, \g<name>, \U, ...) in its *replacement* argument. Content that
    # legitimately contains a Windows-style path must not reach re.sub raw.
    existing = "before\n"
    content = "see C:\\Users\\example\\file.txt and group ref-looking \\1 \\g<name>"

    updated, changed = upsert_block(existing, content)

    assert changed
    assert "C:\\Users\\example\\file.txt" in updated
    assert "\\1 \\g<name>" in updated

    # Re-running against the now-managed text (replace path) must also survive.
    updated_again, changed_again = upsert_block(updated, content)
    assert not changed_again
    assert updated_again == updated


def test_remove_block_survives_backslashes_in_surrounding_content():
    content = "line"
    rendered = render_block(content)
    existing = f"before \\1 stuff\n{rendered}after \\2 stuff\n"

    updated, changed = remove_block(existing)

    assert changed
    assert "before \\1 stuff" in updated
    assert "after \\2 stuff" in updated
    assert "managed block" not in updated


def test_upsert_block_refuses_a_duplicated_start_marker():
    start = "# >>> modelquad managed block >>>\n"
    end = "# <<< modelquad managed block <<<\n"
    # Regression test: with a non-greedy `.*?` between markers, a *second*,
    # unmatched start marker planted above a real block would previously let
    # the regex match from the fake start to the real end, silently
    # swallowing the legitimate user content sitting between them.
    existing = (
        f"{start}"
        "user content that must not be treated as owned\n"
        f"{start}"
        "the real managed content\n"
        f"{end}"
    )

    with pytest.raises(ValueError, match="missing or duplicated"):
        upsert_block(existing, "new content")

    with pytest.raises(ValueError, match="missing or duplicated"):
        remove_block(existing)


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks on Windows needs elevated privilege")
def test_jsonmerge_write_leaves_a_foreign_symlinked_tmp_file_untouched(tmp_path):
    from modelquad.adapters.jsonmerge import upsert_json_key

    target_dir = tmp_path / "claude"
    target_dir.mkdir()
    mcp_path = target_dir / "mcp.json"
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("do not overwrite me", encoding="utf-8")
    foreign = target_dir / "mcp.json.tmp"
    foreign.symlink_to(decoy)

    action = upsert_json_key(mcp_path, ("mcpServers", "multi-agent-modelquad"), {"command": "x"}, dry_run=False)

    assert action.kind is ActionKind.CREATED
    assert foreign.is_symlink()
    assert decoy.read_text(encoding="utf-8") == "do not overwrite me"
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == {
        "mcpServers": {"multi-agent-modelquad": {"command": "x"}}
    }


# --- host tool-call timeout must outlast a delegated worker -------------------
#
# A worker gets DEFAULT_TIMEOUT_SECONDS (600s) to finish. The host must not
# give up on the tool call first, or a still-running delegation is reported as
# a failure. The timeout now lives on the modelquad server entry itself
# (Claude Code's per-server "timeout", in milliseconds), not in the global
# env.MCP_TOOL_TIMEOUT, which applied to every MCP server.


def test_claude_install_gives_the_host_longer_than_a_worker_may_run(tmp_path):
    from modelquad.execution.base import DEFAULT_TIMEOUT_SECONDS

    target = tmp_path / "claude"
    target.mkdir()

    ClaudeAdapter().install(target)

    entry = json.loads((target.parent / "claude.json").read_text(encoding="utf-8"))["mcpServers"]["modelquad"]
    assert type(entry["timeout"]) is int
    assert entry["timeout"] > DEFAULT_TIMEOUT_SECONDS * 1000, (
        "the host must stay patient past the worker's own budget, or a "
        "still-running delegation is reported as a failure"
    )
    settings = json.loads((target / "settings.json").read_text(encoding="utf-8"))
    assert "env" not in settings


def test_claude_install_leaves_env_alone(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    settings_path = target / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"SOMETHING_ELSE": "keep-me", "MCP_TOOL_TIMEOUT": "900000"}}), encoding="utf-8"
    )

    ClaudeAdapter().install(target)
    ClaudeAdapter().uninstall(target)

    # No ownership record: the value is the user's, even though it matches.
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["env"] == {"SOMETHING_ELSE": "keep-me", "MCP_TOOL_TIMEOUT": "900000"}


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize("had_env", [True, False])
def test_an_owned_global_timeout_an_earlier_version_wrote_is_removed(tmp_path, command, had_env):
    from modelquad.adapters import claude_adapter
    from modelquad.adapters.registration import read_owned_settings, write_owned_settings

    target = tmp_path / "claude"
    target.mkdir()
    settings_path = target / "settings.json"
    env = {"SOMETHING_ELSE": "keep-me"} if had_env else {}
    settings_path.write_text(json.dumps({"env": {**env, "MCP_TOOL_TIMEOUT": "900000"}}), encoding="utf-8")
    records = {claude_adapter._timeout_ownership_key(settings_path): "900000"}
    if not had_env:
        records[claude_adapter._timeout_env_key(settings_path)] = True
    write_owned_settings(records)

    report = getattr(ClaudeAdapter(), command)(target)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if had_env:
        assert settings["env"] == {"SOMETHING_ELSE": "keep-me"}
    else:
        assert "env" not in settings
    action = next(a for a in report.actions if a.detail and "MCP_TOOL_TIMEOUT" in a.detail)
    assert action.kind is ActionKind.REMOVED and action.backup_path is not None
    assert not [key for key in read_owned_settings() if key.startswith("claude_timeout")]


def test_no_adapter_wraps_atomic_write_in_a_dead_value_error_handler():
    """``atomic_write`` stopped raising ``ValueError`` with the rename-race fix.

    The handlers that survived it documented a refusal that can no longer
    happen and turned the adapters' ``SKIPPED`` outcome into a lie. Keep them
    out: a write that cannot happen must surface, not be reported as skipped.
    """

    import ast
    import inspect

    from modelquad.adapters import claude_adapter

    offenders: list[str] = []
    for module in (claude_adapter,):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            writes = any(
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and getattr(stmt.value.func, "id", None) == "atomic_write"
                for stmt in node.body
            )
            catches_value_error = any(
                handler.type is not None and "ValueError" in ast.dump(handler.type)
                for handler in node.handlers
            )
            if writes and catches_value_error:
                offenders.append(f"{module.__name__}:{node.lineno}")
    assert offenders == []


_COMMAND_SKILLS = ("modelquad-review", "modelquad-handoff", "modelquad-quota", "modelquad-setup",
                   "modelquad-split", "modelquad-settings")


def test_claude_install_and_uninstall_cover_the_command_skills(tmp_path):
    target = tmp_path / "claude"
    target.mkdir()
    user_skill = target / "skills" / "my-own" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("mine\n", encoding="utf-8")

    report = ClaudeAdapter().install(target)
    created = {action.path.parent.name for action in report.actions
               if action.kind is ActionKind.CREATED and action.path.name == "SKILL.md"}
    assert created == {"modelquad", *_COMMAND_SKILLS}
    for name in _COMMAND_SKILLS:
        assert (target / "skills" / name / "SKILL.md").is_file()
    # A second install changes nothing.
    again = ClaudeAdapter().install(target)
    assert not any(action.kind is ActionKind.CREATED for action in again.actions
                   if action.path.name == "SKILL.md")

    removal = ClaudeAdapter().uninstall(target)
    removed = {action.path.parent.name for action in removal.actions
               if action.kind is ActionKind.REMOVED and action.path.name == "SKILL.md"}
    assert removed == {"modelquad", *_COMMAND_SKILLS}
    for name in ("modelquad", *_COMMAND_SKILLS):
        assert not (target / "skills" / name / "SKILL.md").exists()
    assert user_skill.read_text(encoding="utf-8") == "mine\n"


def test_claude_command_skills_are_user_only_and_never_guess_files(tmp_path):
    from modelquad.adapters.paths import adapters_template_dir

    with adapters_template_dir("claude") as templates:
        for name in _COMMAND_SKILLS:
            text = (templates / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            front, body = text.split("---\n", 2)[1:]
            assert f"name: {name}\n" in front
            assert "disable-model-invocation: true\n" in front
            # No pre-run shell lines, no on-its-own wording.
            assert not any(line.lstrip().startswith("!") for line in body.splitlines())
            assert "proactive" not in text.casefold()


def test_a_users_own_file_at_a_command_skill_path_is_never_replaced_or_removed(tmp_path):
    target = tmp_path / "claude"
    mine = target / "skills" / "modelquad-review" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("my own review skill\n", encoding="utf-8")

    report = ClaudeAdapter().install(target)
    (action,) = [a for a in report.actions if a.path == mine]
    assert action.kind is ActionKind.SKIPPED and "not ModelQuad's" in action.detail
    assert mine.read_text(encoding="utf-8") == "my own review skill\n"
    assert (target / "skills" / "modelquad-quota" / "SKILL.md").is_file()

    removal = ClaudeAdapter().uninstall(target)
    (action,) = [a for a in removal.actions if a.path == mine]
    assert action.kind is ActionKind.SKIPPED
    assert mine.read_text(encoding="utf-8") == "my own review skill\n"
    assert not (target / "skills" / "modelquad-quota" / "SKILL.md").exists()
