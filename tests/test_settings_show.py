# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad settings`` / ``settings show`` and the /modelquad:settings skill.

The view shows three things (on or off in Claude Code, automatic or approve each
call, the history here), changes nothing, and reads unknown where it cannot read.
The skill only shows it and runs the one command the user picked.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modelquad import cli, grant, repo_settings, review_ledger, settings_view
from modelquad.adapters.server_instructions import preferences_path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src/modelquad/adapters/templates/claude/skills/modelquad-settings/SKILL.md"
PLUGIN_COPY = ROOT / "plugin/skills/settings/SKILL.md"


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _show(capsys, folder: Path, db: Path, *extra: str) -> str:
    assert cli.main(["--history-db", str(db), "settings", *extra, "show", "--project", str(folder)]) == 0
    return capsys.readouterr().out


def _install_claude() -> Path:
    """The regular install's two marks: the skill and the server entry."""
    import json

    from modelquad.adapters.paths import default_target_dir

    target = default_target_dir("claude")
    (target / "skills" / "modelquad").mkdir(parents=True, exist_ok=True)
    (target / "skills" / "modelquad" / "SKILL.md").write_text("skill", encoding="utf-8")
    (target.parent / f"{target.name}.json").write_text(
        json.dumps({"mcpServers": {"modelquad": {"command": "modelquad"}}}), encoding="utf-8")
    return target


@pytest.fixture
def installed():
    return _install_claude()


def _snapshot() -> dict:
    """Every stored file the view reads, to prove it writes none of them."""
    files = (preferences_path(), review_ledger.ledger_path())
    return {path: path.read_bytes() if path.exists() else None for path in files}


def test_defaults_read_on_approve_each_call_and_no_history_yet(tmp_path, capsys, installed):
    repo = _repo(tmp_path / "r")
    before = _snapshot()
    out = _show(capsys, repo, tmp_path / "history.db")
    assert "1. ModelQuad in Claude Code: on (connected)." in out
    assert "2. Mode: approve each call." in out
    assert "excluded" not in out.split("To change them")[0]
    assert "3. History here: none yet" in out
    # Reading creates neither the preferences nor the ledger.
    assert _snapshot() == before
    for command in ("modelquad host disconnect claude", "modelquad host connect claude",
                    "modelquad settings auto on --global --yes", "modelquad settings auto off --global",
                    "modelquad review report", "modelquad settings repo off [--project DIR]"):
        assert command in out
    assert "http" not in out and "browser" not in out


def test_settings_with_no_subcommand_shows_the_current_folder(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path / "r")
    monkeypatch.chdir(repo)
    assert cli.main(["--history-db", str(tmp_path / "history.db"), "settings"]) == 0
    assert "2. Mode: approve each call." in capsys.readouterr().out


def test_off_in_claude_code_reads_off(tmp_path, capsys, installed):
    db = tmp_path / "history.db"
    repo = _repo(tmp_path / "r")
    assert cli.main(["--history-db", str(db), "host", "disconnect", "claude"]) == 0
    capsys.readouterr()
    assert "1. ModelQuad in Claude Code: off (disconnected" in _show(capsys, repo, db)
    assert cli.main(["--history-db", str(db), "host", "connect", "claude"]) == 0
    capsys.readouterr()
    assert "1. ModelQuad in Claude Code: on (connected)." in _show(capsys, repo, db)


def test_unreadable_host_controls_read_unknown(tmp_path, capsys, installed):
    db = tmp_path / "history.db"
    db.write_bytes(b"not a database")
    out = _show(capsys, _repo(tmp_path / "r"), db)
    assert "1. ModelQuad in Claude Code: unknown" in out


def test_a_fresh_profile_reads_not_installed(tmp_path, capsys):
    """Nothing installed is not "on (connected)": unknown stays unknown."""
    out = _show(capsys, _repo(tmp_path / "r"), tmp_path / "history.db")
    assert "1. ModelQuad in Claude Code: not installed" in out
    assert "on (connected)" not in out


def test_the_plugin_record_counts_as_installed(tmp_path, capsys):
    import json

    from modelquad.adapters.paths import default_target_dir

    record = default_target_dir("claude") / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"plugins": {"other@market": []}}), encoding="utf-8")
    db = tmp_path / "history.db"
    assert "1. ModelQuad in Claude Code: not installed" in _show(capsys, _repo(tmp_path / "r"), db)
    record.write_text(json.dumps({"plugins": {"modelquad@modelquad": []}}), encoding="utf-8")
    assert "1. ModelQuad in Claude Code: on (connected)." in _show(capsys, tmp_path / "r", db)
    record.write_text("not json", encoding="utf-8")
    assert "1. ModelQuad in Claude Code: unknown" in _show(capsys, tmp_path / "r", db)


def test_automatic_global_versus_this_folder(tmp_path, capsys):
    db = tmp_path / "history.db"
    repo, other = _repo(tmp_path / "r"), _repo(tmp_path / "other")
    grant.write(True, directory=repo)
    out = _show(capsys, repo, db)
    assert "2. Mode: automatic. The standing grant is on for this folder's repository" in out
    # This folder's own grant survives "auto off --global", so its own command is shown.
    assert f'modelquad settings auto off --project "{repo.resolve()}"' in out
    assert "2. Mode: approve each call." in _show(capsys, other, db)

    grant.write(True, everywhere=True)
    out = _show(capsys, other, db)
    assert "2. Mode: automatic. The standing grant is on for every folder" in out
    assert "auto off --project" not in out
    grant.write(False, everywhere=True)
    assert "2. Mode: approve each call." in _show(capsys, other, db)


def test_an_excluded_folder_says_so(tmp_path, capsys):
    db = tmp_path / "history.db"
    repo = _repo(tmp_path / "r")
    grant.write(True, everywhere=True)
    repo_settings.write_mode(repo, "off")
    out = _show(capsys, repo, db)
    assert "2. Mode: automatic." in out
    assert "This folder is excluded: you turned Codex off here" in out
    repo_settings.write_mode(repo, "inherit")
    (repo / ".modelquad.toml").write_text('schema = 1\n[codex]\nallowed = false\n', encoding="utf-8")
    assert "This folder is excluded: this repository's .modelquad.toml turns Codex off" in _show(
        capsys, repo, db)


def test_unreadable_preferences_read_unknown_not_approve(tmp_path, capsys):
    target = preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not json", encoding="utf-8")
    out = _show(capsys, _repo(tmp_path / "r"), tmp_path / "history.db")
    assert "2. Mode: unknown" in out and "approve each call. Claude" not in out
    assert target.read_text(encoding="utf-8") == "not json"


def test_history_line_counts_and_unknowns(tmp_path, capsys, monkeypatch):
    db = tmp_path / "history.db"
    repo = _repo(tmp_path / "r")
    # A ledger with no calls here in the period: none, not a confident count of findings.
    review_ledger.connect().close()
    assert "3. History here: no Codex calls in the last 30 days." in _show(capsys, repo, db)

    answer = "FINDING F1 | high | a.py:1 | bug | one\nFINDING F2 | low | b.py:2 | style | two\n"
    review_ledger.record_run({"decision_id": "d-1", "output": answer}, working_directory=repo,
                             task_type="review", trigger="claude")
    review_ledger.record_run({"decision_id": "d-2", "output": "Looks fine."}, working_directory=repo,
                             task_type="review", trigger="claude")
    review_ledger.record_verdicts("d-1", [{"finding": "F1", "verdict": "confirmed",
                                           "evidence": "code_reading", "action": "no_change"}],
                                  source="claude")
    line = next(text for text in _show(capsys, repo, db).splitlines() if text.startswith("3."))
    assert "2 Codex call(s), 2 answered, 2 finding(s): 1 confirmed (1 by Claude's reading only, not proof)" in line
    assert "0 refuted" in line and "1 not judged yet" in line
    assert "findings unknown for 1 answer(s)" in line
    # Another project's history is not this one's.
    assert "3. History here: no Codex calls in the last 30 days." in _show(
        capsys, _repo(tmp_path / "other"), db)

    def broken(**_):
        raise sqlite3.DatabaseError("broken")

    monkeypatch.setattr(review_ledger, "report", broken)
    assert "3. History here: unknown" in _show(capsys, repo, db)


def test_a_missing_or_remote_folder_is_refused(tmp_path, capsys):
    assert cli.main(["settings", "show", "--project", str(tmp_path / "missing")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_the_view_writes_nothing(tmp_path, monkeypatch):
    """It reads the stored values; no setting writer is ever called."""
    from modelquad.adapters import atomicwrite

    def refuse(*_args, **_kwargs):
        raise AssertionError("the settings view wrote a file")

    monkeypatch.setattr(atomicwrite, "atomic_write", refuse)
    grant_on = _repo(tmp_path / "r")
    lines = settings_view.lines(grant_on, tmp_path / "history.db")
    assert lines[0] == "ModelQuad settings" and lines[1].startswith("1. ")


# --------------------------------------------------------------- the skill


@pytest.mark.parametrize("path", [TEMPLATE, PLUGIN_COPY], ids=["template", "plugin"])
def test_the_skill_only_shows_and_runs_what_the_user_picked(path):
    text = path.read_text(encoding="utf-8")
    front, body = text.split("---", 2)[1:]
    assert "disable-model-invocation: true" in front
    flat = " ".join(body.split())
    assert "modelquad settings show --project" in flat
    assert "AskUserQuestion" in flat
    for option in ("Keep as is", "Turn ModelQuad off", "Turn ModelQuad on", "Change to automatic",
                   "Change to approve each call", "Show the history"):
        assert option in flat
    assert "Never pick for the user" in flat
    assert "dismisses the question" in flat and "change nothing" in flat
    assert "Never undo an exclusion here" in flat
    assert "Never open a browser" in flat
    # Automatic: the notice first (no --yes), then the user's own yes.
    assert flat.index("`modelquad settings auto on --global` without `--yes`") < flat.index(
        "run `modelquad settings auto on --global --yes`")
    for gone in ("settings open", "settings page", "127.0.0.1", "localhost"):
        assert gone not in text
    # It never re-allows Codex in an excluded folder.
    for undo in ("settings repo on", "settings repo inherit", "host reset"):
        assert undo not in flat
