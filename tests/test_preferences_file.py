# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``preferences.json`` is read tolerantly and never rewritten from a failed read.

* A UTF-8 byte order mark (PowerShell 5 writes one) and a file locked for a
  moment still read as the stored settings.
* A setting change after a failed read refuses, with a plain message, and
  leaves the file as it was: writing a fresh file would erase the user's
  per-repository choices, plans and every other setting.
* ``install claude --plugin`` reports a choice it could not save instead of
  failing after the adapter already changed files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from modelquad import cli, plans, repo_settings as rs
from modelquad.adapters import server_instructions as si

KEY = "0123456789abcdef0123456789abcdef"  # gitleaks:allow (synthetic installation id)


@pytest.fixture
def prefs(tmp_path):
    return tmp_path / "preferences.json"


def test_a_byte_order_mark_still_reads(prefs):
    prefs.write_bytes(b"\xef\xbb\xbf" + json.dumps({"proactive": True, "repos": {"a" * 32: "off"}}).encode())
    assert si.read_proactive(prefs) is True
    assert rs.read_modes(prefs) == {"a" * 32: "off"}


def test_a_briefly_locked_file_is_read_again(prefs, monkeypatch):
    prefs.write_text(json.dumps({"proactive": True}), encoding="utf-8")
    real = Path.read_text
    attempts = []

    def flaky(self, *args, **kwargs):
        if self == prefs and not attempts:
            attempts.append(1)
            raise PermissionError(13, "locked")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(si, "_RETRY_SECONDS", 0)
    monkeypatch.setattr(Path, "read_text", flaky)
    assert si.read_proactive(prefs) is True and attempts == [1]


@pytest.mark.parametrize("broken", [b"{not json", b"[1, 2]", b"\xff\xfe\x00garbage"])
@pytest.mark.parametrize("write", ["proactive", "plugin", "plan", "repo"])
def test_a_write_after_a_failed_read_changes_nothing(prefs, tmp_path, broken, write):
    prefs.write_bytes(broken)
    folder = tmp_path / "project"
    folder.mkdir()
    with pytest.raises(si.PreferencesUnreadable) as caught:
        if write == "proactive":
            si.write_proactive(True, prefs)
        elif write == "plugin":
            si.write_claude_plugin(True, prefs)
        elif write == "plan":
            plans.write_declared("codex", "plus", prefs)
        else:
            rs.write_mode(folder, "off", path=prefs, installation_key=KEY)
    assert "changed nothing" in str(caught.value)
    assert prefs.read_bytes() == broken


def test_a_write_keeps_every_other_setting(prefs, tmp_path):
    stored = {"proactive": True, "plans": {"codex": "team"}, "claude_plugin": True,
              "repos": {"b" * 32: "offer-only"}}
    prefs.write_bytes(b"\xef\xbb\xbf" + json.dumps(stored).encode())
    folder = tmp_path / "project"
    folder.mkdir()
    rs.write_mode(folder, "off", path=prefs, installation_key=KEY)
    after = json.loads(prefs.read_text(encoding="utf-8"))
    assert after["proactive"] is True and after["plans"] == {"codex": "team"}
    assert after["claude_plugin"] is True and after["repos"]["b" * 32] == "offer-only"
    assert len(after["repos"]) == 2


def test_no_file_is_still_a_fresh_start(prefs):
    si.write_proactive(True, prefs)
    assert json.loads(prefs.read_text(encoding="utf-8")) == {"proactive": True}


def test_the_cli_says_the_setting_was_not_saved(monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise si.PreferencesUnreadable(si.PREFERENCES_UNREADABLE)

    monkeypatch.setattr(si, "write_proactive", refuse)
    assert cli.main(["settings", "proactive", "on"]) == 1
    assert "changed nothing" in capsys.readouterr().err


def test_an_unsaved_plugin_choice_is_reported_not_raised(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(13, "denied")

    monkeypatch.setattr(si, "write_claude_plugin", refuse)
    args = argparse.Namespace(plugin=True, dry_run=False)
    assert cli._remember_plugin_choice(args) == cli.PLUGIN_CHOICE_NOT_SAVED
    assert cli._remember_plugin_choice(argparse.Namespace(plugin=None, dry_run=False)) is None
