# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Absolute paths, including ones with spaces, do not reach what ModelQuad stores."""

from __future__ import annotations

import json
import sqlite3

import pytest

from modelquad import privacy

HOME = r"C:\Users\John Smith"
ONEDRIVE = HOME + r"\OneDrive - Contoso Ltd"


@pytest.fixture
def roots(monkeypatch):
    monkeypatch.setenv("USERPROFILE", HOME)
    monkeypatch.setenv("OneDriveCommercial", ONEDRIVE)
    monkeypatch.setenv("LOCALAPPDATA", HOME + r"\AppData\Local")
    monkeypatch.setenv("TEMP", HOME + r"\AppData\Local\Temp")


@pytest.mark.parametrize("text", [
    r"Usage file not found: C:\Users\John Smith\.modelquad\usage\usage-codex.json",
    r"see c:/users/john smith/projects/app/main.py now",
    r"open C:\Users\John Smith\OneDrive - Contoso Ltd\Project Plan\notes.md failed",
    r"temp file C:\USERS\JOHN SMITH\APPDATA\LOCAL\TEMP\x 1.txt",
])
def test_known_roots_are_removed_with_their_spaces(roots, text):
    out = privacy.redact_paths(text)
    assert "John" not in out and "Smith" not in out.replace("Smithers", "")
    assert "Contoso" not in out
    assert "<path>" in out
    assert not privacy.contains_absolute_path(out)


def test_a_business_onedrive_name_goes_even_when_its_root_is_unknown(monkeypatch):
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(name, raising=False)
    out = privacy.redact_paths(r"D:\Work\OneDrive - Fabrikam Inc\plan.md and more")
    assert "Fabrikam" not in out
    assert out.endswith(" and more")


def test_a_tilde_path_is_a_home_path():
    out = privacy.redact_paths(r"could not read ~\projects\app\a.py or ~/b/c.py")
    assert out == "could not read <path> or <path>"


def test_a_caller_named_working_folder_is_removed_whole():
    folder = r"E:\Client Work\Secret Project"
    out = privacy.sanitize_persisted_text(rf"failed in {folder}\src\x.py", roots=[folder])
    assert "Client" not in out and "Secret" not in out
    assert out == "failed in <path>"


def test_a_root_does_not_swallow_a_longer_sibling_name(roots):
    out = privacy.redact_paths(r"C:\Users\John Smithers\file.txt")
    assert "Smithers" in out  # not the home folder; only the regex applies
    assert out.startswith("<path>")


def test_prose_and_bare_drive_roots_are_left_alone(roots):
    assert privacy.redact_paths("exit 1 (tests failed) on 3 target(s)") == "exit 1 (tests failed) on 3 target(s)"
    assert privacy.redact_paths("n/a ~ok") == "n/a ~ok"


@pytest.mark.parametrize("text", [
    r"open //fileserver/clients/acme/x.py failed",
    r"open /c/Users/dana/proj/x.py failed",
    r"open /e/Projects/AcmeSecret/x.py failed",
    r"open \Users\dana\proj\x.py failed",
    r"open C%3A%5CUsers%5Cdana%5Cx.py failed",
    r"open file:///C:/Users/dana/x.py failed",
    r"open file://fileserver/clients/acme/x.py failed",
])
def test_other_absolute_path_forms_are_removed(text):
    out = privacy.redact_paths(text)
    assert out == "open <path> failed", out


def test_redaction_leaves_no_stray_characters_around_the_placeholder():
    assert privacy.redact_paths(r"<C:\Users\dana\x>") == "<<path>>"
    assert privacy.redact_paths("see file:///C:/x.py") == "see <path>"


@pytest.mark.parametrize("text", ["and/or 1/2 a/b/c", "// a comment", "src/modelquad/x.py", "./a/b"])
def test_relative_paths_and_prose_with_slashes_stay(text):
    assert privacy.redact_paths(text) == text


def test_metadata_keys_are_redacted_too():
    out = json.loads(privacy.sanitize_persisted_metadata_json({r"C:\Users\dana\key": 1}))
    assert out == {"<path>": 1}


FOLDER_WITH_SPACES = r"D:\Client Work\Acme Corp"


def test_the_ledger_writer_removes_the_named_working_folder_whole(tmp_path, monkeypatch):
    """The MCP server's folder is not the project's, so the writer names the folder."""

    from modelquad import review_ledger

    monkeypatch.chdir(tmp_path)  # not the working folder
    error = rf"FileNotFoundError: {FOLDER_WITH_SPACES}\src\billing.py"
    ledger = tmp_path / "review-ledger.db"
    output = (f"FINDING F1 | high | src/billing.py:3 | correctness | "
              f"confidence low: seen in {FOLDER_WITH_SPACES}\\notes only | {error} | "
              f"because {FOLDER_WITH_SPACES}\\src is read\n")
    review_ledger.record_run({"decision_id": "d-paths", "output": output},
                             working_directory=FOLDER_WITH_SPACES, task_type="review",
                             trigger="claude", path=ledger)

    conn = sqlite3.connect(ledger)
    try:
        tables = [name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        stored = [value for table in tables for row in conn.execute(f"SELECT * FROM {table}")
                  for value in row if isinstance(value, str)]
        (claim,) = conn.execute("SELECT claim FROM findings").fetchone()
        note, reasoning = conn.execute("SELECT confidence_note, reasoning FROM findings").fetchone()
    finally:
        conn.close()
    assert claim == "FileNotFoundError: <path>"
    assert "<path>" in note and "<path>" in reasoning
    for text in stored:
        assert "Client" not in text and "Acme" not in text, text
        assert str(tmp_path) not in text, text
        assert not privacy.contains_absolute_path(text), text


def test_without_the_named_folder_the_tail_would_remain(tmp_path, monkeypatch):
    """Why the writers name it: the regex alone stops at the first space."""
    monkeypatch.chdir(tmp_path)
    assert "Acme" in privacy.redact_paths(rf"{FOLDER_WITH_SPACES}\src\billing.py")


def test_the_review_ledger_removes_the_named_working_folder_whole(tmp_path, monkeypatch):
    from modelquad import review_ledger

    monkeypatch.chdir(tmp_path)
    output = (rf"FINDING F1 | high | src/a.py:3 | correctness | reading {FOLDER_WITH_SPACES}\src\a.py fails"
              "\n")
    findings = review_ledger.parse_findings(output, roots=(FOLDER_WITH_SPACES,))
    assert findings and findings[0].claim == "reading <path> fails"
