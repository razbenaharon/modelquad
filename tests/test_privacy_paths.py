# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Absolute paths, including ones with spaces, do not reach stored history."""

from __future__ import annotations

import json
import sqlite3

import pytest

from modelquad import privacy
from modelquad.config import Config
from modelquad.models import TaskType
from modelquad.providers import CodexProvider
from modelquad.router import Router
from modelquad.service import history

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


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_no_absolute_path_reaches_routing_decisions(tmp_path, monkeypatch):
    """Provider metadata, the router's reason and the evidence go through the sanitizer.

    The usage folder sits under a home folder whose name has a space, the
    shape that used to leak the second half of the user's name.
    """

    home = tmp_path / "John Smith"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    usage = home / ".modelquad" / "usage"
    provider = CodexProvider(usage_directory=usage)
    status = provider.get_status()
    assert str(usage) in (status.reason or "") + json.dumps(status.metadata, default=str)
    result = Router(Config()).route(TaskType.REVIEW.value, (status,))
    evidence = json.dumps({"statuses": [status.to_dict()], "note": f"read {usage}"}, default=str)
    db_path = tmp_path / "history.db"

    assert history.record_routing_decision(
        db_path, TaskType.REVIEW, result, source="mcp", decision_id="d-paths",
        evidence_json=evidence, override_reason=f"override at {usage}",
    ) is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = dict(conn.execute("SELECT * FROM routing_decisions").fetchone())
    finally:
        conn.close()
    texts = []
    for column, value in row.items():
        if column in ("providers_json", "evidence_json"):
            texts.extend(_strings(json.loads(value)))
        elif isinstance(value, str):
            texts.append(value)
    assert any("<path>" in text for text in texts)
    for text in texts:
        assert "John" not in text and "Smith" not in text, text
        assert str(tmp_path) not in text, text
        assert not privacy.contains_absolute_path(text), text
