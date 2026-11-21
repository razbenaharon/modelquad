# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""`modelquad doctor` names a Claude Code permission rule that still uses a
ModelQuad tool name from before 0.5.0, reads those files only, and never crashes."""

import json
from pathlib import Path

import pytest

from modelquad import setupcheck
from modelquad.branding import old_tool_ids, tool_ids

OLD_PLUGIN, OLD_INSTALL = old_tool_ids("delegate")
NEW_PLUGIN, NEW_INSTALL = tool_ids("delegate")


def _write(path: Path, permissions: dict | None = None, raw: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": "secret-ish-value", "env": {"TOKEN": "do-not-show"}}
    if permissions is not None:
        payload["permissions"] = permissions
    path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return path


def _check(tmp_path: Path) -> dict:
    return setupcheck._permission_rules(str(tmp_path / "project"))


@pytest.fixture
def folders(tmp_path, monkeypatch):
    from modelquad.execution import file_rules

    (tmp_path / "project").mkdir()
    (tmp_path / "claude").mkdir()
    # User settings through CLAUDE_CONFIG_DIR, managed settings in the test folder.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(file_rules, "MANAGED_SETTINGS", tmp_path / "managed" / "managed-settings.json")
    return tmp_path


def test_the_ids_are_the_short_names():
    assert tool_ids("delegate") == ("mcp__plugin_modelquad_review__delegate", "mcp__modelquad__delegate")
    assert old_tool_ids("delegate") == ("mcp__plugin_modelquad_modelquad__modelquad_delegate",
                                        "mcp__modelquad__modelquad_delegate")


@pytest.mark.parametrize("old, new", [
    ("mcp__plugin_modelquad_modelquad__modelquad_delegate", "mcp__plugin_modelquad_review__delegate"),
    ("mcp__plugin_modelquad_modelquad__modelquad_status", "mcp__plugin_modelquad_review__status"),
    ("mcp__plugin_modelquad_modelquad__*", "mcp__plugin_modelquad_review__*"),
    ("mcp__plugin_modelquad_modelquad", "mcp__plugin_modelquad_review"),
    ("mcp__modelquad__modelquad_review_package", "mcp__modelquad__review_package"),
    ("mcp__modelquad__modelquad_review_verdict", "mcp__modelquad__review_verdict"),
    (" mcp__modelquad__modelquad_delegate ", "mcp__modelquad__delegate"),
])
def test_an_old_name_maps_to_its_new_name(old, new):
    assert setupcheck.renamed_rule(old) == new


@pytest.mark.parametrize("rule", ["mcp__modelquad", "mcp__modelquad__*", "mcp__modelquad__delegate",
                                  "mcp__plugin_modelquad_review__*", "Bash(codex *)", "Read", ""])
def test_a_current_or_unrelated_rule_is_not_stale(rule):
    assert setupcheck.renamed_rule(rule) is None


def test_no_settings_files_is_fine(folders):
    check = _check(folders)
    assert check["ok"] is True and check["stale_rules"] == [] and check["unreadable"] == []


def test_current_rules_are_fine(folders):
    _write(folders / "claude" / "settings.json", {"allow": [NEW_PLUGIN], "deny": ["mcp__modelquad"]})
    check = _check(folders)
    assert check["ok"] is True and check["stale_rules"] == []


def test_a_stale_allow_is_named_with_its_new_id(folders):
    _write(folders / "claude" / "settings.json", {"allow": [OLD_PLUGIN]})
    check = _check(folders)
    assert check["ok"] is True
    assert OLD_PLUGIN in check["detail"] and NEW_PLUGIN in check["detail"]
    assert "asks again" in check["detail"]
    (item,) = check["stale_rules"]
    assert item["file"].startswith("your Claude Code settings") and item["scope"] == "user"
    assert (item["kind"], item["rule"], item["use"]) == ("allow", OLD_PLUGIN, NEW_PLUGIN)


def test_a_stale_deny_is_a_problem(folders):
    _write(folders / "project" / ".claude" / "settings.json",
           {"deny": ["mcp__plugin_modelquad_modelquad__*"]})
    check = _check(folders)
    assert check["ok"] is False
    assert "use mcp__plugin_modelquad_review__*" in check["detail"]
    assert "this project's .claude/settings.json" in check["detail"]
    assert "does not change these files" in check["next"]
    for rule in ("mcp__plugin_modelquad_review__*", "mcp__plugin_modelquad_modelquad__*", "mcp__modelquad"):
        assert rule in check["next"]


def test_a_stale_deny_next_to_its_new_name_is_covered(folders):
    _write(folders / "project" / ".claude" / "settings.local.json",
           {"deny": ["mcp__modelquad", "mcp__plugin_modelquad_review__*",
                     "mcp__plugin_modelquad_modelquad__*"]})
    check = _check(folders)
    assert check["ok"] is True
    assert [item["rule"] for item in check["stale_rules"]] == ["mcp__plugin_modelquad_modelquad__*"]


def test_a_user_deny_covers_an_old_project_deny(folders):
    _write(folders / "project" / ".claude" / "settings.json", {"deny": [OLD_INSTALL]})
    _write(folders / "claude" / "settings.json", {"deny": ["mcp__modelquad__*"]})
    assert _check(folders)["ok"] is True


def test_a_project_deny_does_not_cover_an_old_user_deny(folders):
    # The user's rule applies in every project; this project's new rule does not.
    _write(folders / "claude" / "settings.json", {"deny": ["mcp__plugin_modelquad_modelquad__*"]})
    _write(folders / "project" / ".claude" / "settings.local.json",
           {"deny": ["mcp__plugin_modelquad_review__*"]})
    check = _check(folders)
    assert check["ok"] is False and "your Claude Code settings" in check["detail"]


def test_a_managed_deny_covers_an_old_user_deny(folders):
    _write(folders / "claude" / "settings.json", {"deny": [OLD_PLUGIN]})
    _write(folders / "managed" / "managed-settings.json", {"deny": ["mcp__plugin_modelquad_review"]})
    assert _check(folders)["ok"] is True


def test_an_old_managed_deny_is_named(folders):
    _write(folders / "managed" / "managed-settings.json", {"deny": ["mcp__plugin_modelquad_modelquad"]})
    _write(folders / "claude" / "settings.json", {"deny": ["mcp__plugin_modelquad_review__*"]})
    check = _check(folders)
    assert check["ok"] is False and "managed settings" in check["detail"]


def test_an_exact_new_id_does_not_cover_an_old_deny(folders):
    # A mixed state can name the tool mcp__plugin_modelquad_review__modelquad_delegate.
    _write(folders / "claude" / "settings.json", {"deny": [OLD_PLUGIN, NEW_PLUGIN]})
    check = _check(folders)
    assert check["ok"] is False and "use mcp__plugin_modelquad_review__*" in check["detail"]


def test_the_user_folder_follows_claude_config_dir(folders, monkeypatch):
    moved = folders / "moved"
    _write(moved / "settings.json", {"deny": [OLD_PLUGIN]})
    _write(folders / "claude" / "settings.json", {"deny": ["mcp__plugin_modelquad_review__*"]})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    check = _check(folders)
    assert check["ok"] is False and str(moved / "settings.json") in check["detail"]


def test_a_user_local_settings_file_is_not_read(folders):
    _write(folders / "claude" / "settings.local.json", {"deny": [OLD_PLUGIN]})
    assert _check(folders)["stale_rules"] == []


def test_stale_allow_and_deny_report_the_deny(folders):
    _write(folders / "claude" / "settings.json", {"allow": [OLD_INSTALL], "deny": [OLD_PLUGIN]})
    check = _check(folders)
    assert check["ok"] is False
    assert {item["kind"] for item in check["stale_rules"]} == {"allow", "deny"}
    assert "mcp__plugin_modelquad_review__*" in check["detail"]


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '{"permissions": "x"}',
                                 '{"permissions": {"deny": "mcp__x", "allow": [1, null]}}'])
def test_odd_content_never_crashes(folders, raw):
    _write(folders / "claude" / "settings.json", raw=raw)
    check = _check(folders)
    assert check["ok"] in (True, None)
    if raw == "{not json":
        assert check["ok"] is None and len(check["unreadable"]) == 1
        assert check["unreadable"][0].startswith("your Claude Code settings")


def test_an_unreadable_file_is_unknown(folders):
    (folders / "claude" / "settings.json").mkdir()  # exists, cannot be read as a file
    check = _check(folders)
    assert check["ok"] is None and "unknown" in check["detail"]
    assert "your Claude Code settings" in check["detail"]


def test_an_unreadable_file_does_not_hide_a_stale_deny(folders):
    (folders / "claude" / "settings.json").mkdir()
    _write(folders / "project" / ".claude" / "settings.json", {"deny": [OLD_PLUGIN]})
    assert _check(folders)["ok"] is False


def test_only_modelquad_rules_are_shown_and_nothing_is_written(folders):
    path = _write(folders / "claude" / "settings.json",
                  {"allow": [OLD_PLUGIN, "Bash(git status)"], "deny": ["Read(./secrets/**)"]})
    before = path.read_bytes(), path.stat().st_mtime_ns
    check = _check(folders)
    text = json.dumps(check)
    for private in ("Bash(git status)", "Read(./secrets/**)", "secret-ish-value", "do-not-show"):
        assert private not in text
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_doctor_runs_the_check_from_the_home_claude_folder(tmp_path, monkeypatch):
    from modelquad.execution import file_rules

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(file_rules, "MANAGED_SETTINGS", tmp_path / "no-managed.json")
    home = Path(setupcheck.os.environ["USERPROFILE"])
    _write(home / ".claude" / "settings.json", {"deny": [OLD_PLUGIN]})
    project = tmp_path / "proj"
    project.mkdir()
    check = setupcheck._permission_rules(str(project))
    assert check["ok"] is False and "mcp__plugin_modelquad_review__*" in check["detail"]
    lines = setupcheck.lines({"ok": False, "checks": [check]})
    assert lines[0].startswith("[PROBLEM]")
