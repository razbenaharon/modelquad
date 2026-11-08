# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The standing grant: Codex calls without the per-call prompt only where the user said so."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from modelquad import grant, repo_settings
from modelquad.procutil import no_window_kwargs


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_off_by_default_and_on_for_one_repository_only(tmp_path):
    one, two = _repo(tmp_path / "one"), _repo(tmp_path / "two")
    assert not grant.granted(one) and not grant.allows(one)
    assert grant.write(True, directory=one) == "this repository"
    assert grant.granted(one) and grant.allows(one)
    assert grant.allows(one / "sub" if (one / "sub").mkdir() is None else one)
    assert not grant.granted(two)
    grant.write(False, directory=one)
    assert not grant.granted(one)


def test_global_grant_covers_every_folder(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    grant.write(True, everywhere=True)
    assert grant.allows(folder)
    grant.write(False, everywhere=True)
    assert not grant.allows(folder)


def test_codex_off_in_the_repository_outranks_the_grant(tmp_path):
    repo = _repo(tmp_path / "r")
    grant.write(True, directory=repo)
    repo_settings.write_mode(repo, "off")
    assert grant.granted(repo) and not grant.allows(repo)
    repo_settings.write_mode(repo, "inherit")
    (repo / ".modelquad.toml").write_text('schema = 1\n[codex]\nallowed = false\n', encoding="utf-8")
    assert not grant.allows(repo)


def test_the_grant_keeps_other_preferences(tmp_path):
    from modelquad.adapters.server_instructions import preferences_path, read_proactive, write_proactive

    write_proactive(False)
    grant.write(True, directory=_repo(tmp_path / "r"))
    assert read_proactive() is False
    stored = json.loads(preferences_path().read_text(encoding="utf-8"))
    assert stored["proactive"] is False and stored["standing_grant"]["repos"]


def test_active_at_start_reads_the_session_folder(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    grant.write(True, directory=repo)
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    assert grant.active_at_start(env) is True
    assert grant.active_at_start({"CLAUDE_PROJECT_DIR": str(tmp_path)}) is False
    assert grant.active_at_start({**env, "MODELQUAD_WORKER": "1"}) is False
    assert grant.active_at_start({**env, "MODELQUAD_NO_STANDING_GRANT": "1"}) is False


def test_no_mcp_tool_can_turn_a_grant_on():
    source = Path(grant.__file__).with_name("mcp").joinpath("server.py").read_text(encoding="utf-8")
    assert "_grant.write" not in source and "grant.write(" not in source
    from modelquad.mcp import server

    names = {tool.name for tool in asyncio.run(server.server.list_tools())}
    assert not any("grant" in name or "auto" in name for name in names)


def test_a_session_without_the_marker_refuses_an_ungranted_folder(tmp_path, monkeypatch):
    from modelquad.mcp import server

    monkeypatch.setattr(server, "_STANDING_GRANT", True)
    monkeypatch.setattr(server._grant, "allows", lambda directory: False)
    result = asyncio.run(server.modelquad_delegate(
        task_type="review", prompt="look", working_directory=str(tmp_path), timeout=10))
    payload = result.structured_content
    assert payload["refused"] is True and payload["started"] is False
    assert payload["reason"] == grant.NO_GRANT


def test_the_marker_is_set_without_a_grant():
    from modelquad.mcp import server

    tools = {tool.name: tool for tool in asyncio.run(server.server.list_tools())}
    assert tools["modelquad_delegate"].meta["anthropic/requiresUserInteraction"] is True
    assert tools["modelquad_handoff"].meta["anthropic/requiresUserInteraction"] is True


def test_with_a_grant_the_server_registers_delegate_without_the_marker(tmp_path):
    repo = _repo(tmp_path / "r")
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               APPDATA=str(home / "AppData" / "Roaming"), LOCALAPPDATA=str(home / "AppData" / "Local"))
    env.pop("MODELQUAD_NO_STANDING_GRANT", None)
    # The package under test, wherever it was imported from, also in the other folder.
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(Path(grant.__file__).resolve().parents[1]), env.get("PYTHONPATH")) if part)
    code = ("import sys; from pathlib import Path; from modelquad import grant; "
            "grant.write(True, directory=Path(sys.argv[1]))")
    subprocess.run([sys.executable, "-c", code, str(repo)], env=env, check=True, timeout=120,
                   **no_window_kwargs())
    show = ("import asyncio, json; from modelquad.mcp import server; "
            "tools = {t.name: t.meta for t in asyncio.run(server.server.list_tools())}; "
            "print(json.dumps({'standing': server._STANDING_GRANT, "
            "'delegate': tools['modelquad_delegate'], 'handoff': tools['modelquad_handoff']}))")
    out = subprocess.run([sys.executable, "-c", show], env=dict(env, CLAUDE_PROJECT_DIR=str(repo)),
                         check=True, capture_output=True, text=True, timeout=180, cwd=str(repo),
                         **no_window_kwargs())
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen["standing"] is True
    assert "anthropic/requiresUserInteraction" not in (seen["delegate"] or {})
    # The low-quota handoff keeps its prompt either way.
    assert seen["handoff"]["anthropic/requiresUserInteraction"] is True


def test_the_cli_needs_a_typed_yes(tmp_path, capsys, monkeypatch):
    from modelquad import cli

    repo = _repo(tmp_path / "r")
    assert cli.main(["settings", "auto", "on", "--project", str(repo)]) == 2
    assert not grant.granted(repo)
    assert cli.main(["settings", "auto", "on", "--project", str(repo), "--yes"]) == 0
    out = capsys.readouterr().out
    assert grant.ALLOW_RULE in out and "never writes permission rules" in out
    assert grant.granted(repo)
    assert cli.main(["settings", "auto", "status", "--project", str(repo)]) == 0
    assert "Standing grant: on" in capsys.readouterr().out
    assert cli.main(["settings", "auto", "off", "--project", str(repo)]) == 0
    assert not grant.granted(repo)


def test_only_the_global_grant_warns_about_folders_opened_later(tmp_path, capsys):
    from modelquad import cli

    repo = _repo(tmp_path / "r")
    assert cli.main(["settings", "auto", "on", "--project", str(repo), "--yes"]) == 0
    assert "every folder you open later" not in capsys.readouterr().out
    assert cli.main(["settings", "auto", "on", "--global", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "every folder you open later" in out and "a client's code" in out
    assert "modelquad settings repo off" in out


def test_a_worker_cannot_turn_the_grant_on(tmp_path, capsys, monkeypatch):
    from modelquad import cli

    variable = "MODELQUAD_WORKER"
    monkeypatch.setenv(variable, "1")
    repo = _repo(tmp_path / "r")
    assert cli.main(["settings", "auto", "on", "--project", str(repo), "--yes"]) == 2
    assert "worker cannot turn" in capsys.readouterr().err
    assert not grant.granted(repo)
    assert cli.main(["settings", "auto", "on", "--global", "--yes"]) == 2
    folder = tmp_path / "plain"
    folder.mkdir()
    assert not grant.allows(folder)
    # Turning it off stays open from anywhere.
    monkeypatch.delenv(variable)
    grant.write(True, directory=repo)
    monkeypatch.setenv(variable, "1")
    assert cli.main(["settings", "auto", "off", "--project", str(repo)]) == 0
    assert not grant.granted(repo)


def test_the_refusal_tells_claude_to_tell_the_user_and_turn_nothing_on():
    from modelquad.adapters.server_instructions import (
        _NEVER_REENABLE, _REST, _STANDING, server_instructions)

    message = grant.NO_GRANT_MESSAGE
    assert "--yes" not in message
    assert "Tell the user" in message and "started nothing" in message
    assert "/modelquad:settings" in message and "own terminal" in message
    assert "Do not run any command to turn it back on yourself" in message
    assert "Never turn on a grant, reconnect ModelQuad or undo an exclusion unless the user asks" in _NEVER_REENABLE
    assert _STANDING.endswith(_NEVER_REENABLE) and _NEVER_REENABLE in _REST
    for proactive in (True, False):
        assert server_instructions(proactive).count(_NEVER_REENABLE) == 1
    assert server_instructions(True, standing=True).count(_NEVER_REENABLE) == 1


def test_the_users_own_settings_inside_claude_code_are_not_refused(tmp_path, monkeypatch):
    """/modelquad:settings runs the command inside Claude Code for the user who picked it."""
    from modelquad import cli

    monkeypatch.delenv("MODELQUAD_WORKER", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    repo = _repo(tmp_path / "r")
    assert cli.main(["settings", "auto", "on", "--project", str(repo), "--yes"]) == 0
    assert grant.granted(repo)
    assert cli.main(["settings", "auto", "off", "--project", str(repo)]) == 0
    assert not grant.granted(repo)
