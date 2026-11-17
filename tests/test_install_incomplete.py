# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""An install or uninstall that could not finish does not end as a success.

A file that refuses to open for a moment (Windows: a scanner holds a file that
was just written) is read again; a file that stays unreadable, or that could
not be removed, makes the command exit 1 and say what was left undone. A file
ModelQuad leaves alone on purpose is still not an error.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelquad.adapters import ActionKind
from modelquad.adapters import staging
from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.cli import main


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch, isolate_home_directory):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(staging.time, "sleep", lambda seconds: None)


def _refuse_reads(monkeypatch, target: Path, *, times: int | None) -> list[int]:
    """Make reading ``target`` fail ``times`` times (None: always)."""

    real = Path.read_bytes
    failures = [0]

    def read_bytes(self):
        if Path(self) == target and (times is None or failures[0] < times):
            failures[0] += 1
            raise PermissionError(13, "The process cannot access the file")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return failures


def _stale_skill(claude_dir: Path) -> Path:
    skill = claude_dir / "skills" / "modelquad" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("an earlier build's skill\n", encoding="utf-8")
    return skill


def test_a_file_that_refuses_to_open_once_is_read_again(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    skill = _stale_skill(claude_dir)
    failures = _refuse_reads(monkeypatch, skill, times=2)

    report = ClaudeAdapter(via_plugin=False).install(claude_dir)

    assert failures[0] == 2
    assert not report.failures
    assert next(a for a in report.actions if a.path == skill).kind is ActionKind.UPDATED
    assert "an earlier build" not in skill.read_text(encoding="utf-8")


def test_install_with_an_unreadable_skill_is_not_a_success(tmp_path, monkeypatch, capsys):
    claude_dir = tmp_path / "claude"
    skill = _stale_skill(claude_dir)
    _refuse_reads(monkeypatch, skill, times=None)

    code = main(["install", "--all", "--claude-dir", str(claude_dir)])

    out = capsys.readouterr().out
    assert code == 1
    assert "Not finished" in out and "Could not read" in out
    assert skill.read_text(encoding="utf-8") == "an earlier build's skill\n"


def test_install_json_names_the_unfinished_file(tmp_path, monkeypatch, capsys):
    claude_dir = tmp_path / "claude"
    skill = _stale_skill(claude_dir)
    _refuse_reads(monkeypatch, skill, times=None)

    code = main(["install", "claude", "--target-dir", str(claude_dir), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"].startswith("Not finished: Could not read")


def test_uninstall_with_unreadable_settings_is_not_a_success(tmp_path, monkeypatch, capsys):
    claude_dir = tmp_path / "claude"
    assert main(["install", "--all", "--claude-dir", str(claude_dir)]) == 0
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
    capsys.readouterr()
    _refuse_reads(monkeypatch, settings, times=None)

    code = main(["uninstall", "--all", "--claude-dir", str(claude_dir)])

    assert code == 1
    assert "Not finished" in capsys.readouterr().out


def test_a_dry_run_that_cannot_read_a_file_changes_nothing_and_is_not_an_error(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    skill = _stale_skill(claude_dir)
    _refuse_reads(monkeypatch, skill, times=None)

    assert main(["install", "--all", "--claude-dir", str(claude_dir), "--dry-run"]) == 0


def test_a_file_left_alone_on_purpose_is_still_not_an_error(tmp_path, capsys):
    """Settings the user's own program wrote as broken JSON are preserved, as before."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text("{ not json", encoding="utf-8")

    assert main(["install", "--all", "--claude-dir", str(claude_dir)]) == 0

    assert settings.read_text(encoding="utf-8") == "{ not json"
    assert "Not finished" not in capsys.readouterr().out
