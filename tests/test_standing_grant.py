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


# ------------------------------------- exclusions the grant must not outlive


def test_off_on_a_plain_folder_covers_the_folders_below_it(tmp_path):
    from modelquad import repo_policy

    plain = tmp_path / "plain"
    (plain / "src" / "deep").mkdir(parents=True)
    grant.write(True, everywhere=True)
    repo_settings.write_mode(plain, "off")
    for folder in (plain, plain / "src", plain / "src" / "deep"):
        refusal, _ = repo_policy.gate(str(folder), "review")
        assert refusal and refusal["reason"] == repo_settings.USER_CODEX_OFF, folder
        assert not grant.allows(folder), folder
    # The parent is not covered.
    assert grant.allows(tmp_path)
    # The user's own word for a folder below still wins there.
    repo_settings.write_mode(plain / "src", "on")
    assert grant.allows(plain / "src")
    assert not grant.allows(plain)


def test_on_for_a_plain_folder_does_not_travel_down(tmp_path):
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)
    repo_settings.write_mode(plain, "on")
    assert repo_settings.read_mode(plain) == "on"
    assert repo_settings.read_mode(plain / "src") is None


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows link type")
def test_a_junction_into_a_repository_is_that_repository(tmp_path):
    from modelquad import repo_policy

    repo = _repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    link = tmp_path / "link"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(repo / "sub")],
                          capture_output=True, **no_window_kwargs())
    if made.returncode != 0:
        pytest.skip("this machine does not allow creating a junction")
    grant.write(True, everywhere=True)
    repo_settings.write_mode(repo, "off")
    refusal, _ = repo_policy.gate(str(link), "review")
    assert refusal and refusal["reason"] == repo_settings.USER_CODEX_OFF
    assert not grant.allows(link)
    repo_settings.write_mode(repo, "inherit")
    (repo / ".modelquad.toml").write_text("schema = 1\n[codex]\nallowed = false\n", encoding="utf-8")
    assert repo_policy.gate(str(link), "review")[0] is not None
    assert not grant.allows(link)


def test_resetting_the_installation_id_leaves_no_grant_over_a_lost_exclusion(tmp_path, capsys):
    from modelquad.cli import main

    repo = _repo(tmp_path / "r")
    other = _repo(tmp_path / "other")
    grant.write(True, everywhere=True)
    grant.write(True, directory=other)
    repo_settings.write_mode(repo, "off")
    assert not grant.allows(repo)
    assert main(["privacy", "--reset-identity"]) == 0
    out = capsys.readouterr().out
    assert "were removed" in out and "turned off" in out
    assert "1 removed" in out
    assert not grant.granted(repo) and not grant.allows(repo)
    assert not grant.granted(other)
    assert repo_settings.read_modes() == {}


def test_a_global_grant_does_not_survive_a_changed_installation_id(tmp_path):
    from modelquad.identity import reset_installation_id

    repo = _repo(tmp_path / "r")
    grant.write(True, everywhere=True)
    repo_settings.write_mode(repo, "off")
    # The id changes without the command (a damaged or replaced identity file).
    reset_installation_id()
    assert repo_settings.read_mode(repo) is None  # the exclusion no longer matches
    assert not grant.granted(repo) and not grant.allows(repo)
    # Turning the grant on again is the user's act, and holds under the new id.
    grant.write(True, everywhere=True)
    assert grant.granted(repo)


def test_a_global_grant_given_before_bindings_still_counts(tmp_path):
    from modelquad.adapters.server_instructions import preferences_path

    preferences_path().parent.mkdir(parents=True, exist_ok=True)
    preferences_path().write_text(json.dumps({grant._KEY: {"global": True, "repos": {}}}),
                                  encoding="utf-8")
    assert grant.granted(tmp_path)


# ------------------------------------------------ what the grant is said to do


def test_turning_one_repository_off_under_the_global_grant_says_it_still_applies(tmp_path, capsys):
    from modelquad.cli import main

    repo = _repo(tmp_path / "r")
    grant.write(True, everywhere=True)
    assert main(["settings", "auto", "off", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "still on" in out and "settings repo off" in out and "--global" in out
    assert "Standing grant: off" not in out
    assert grant.granted(repo)
    grant.write(False, everywhere=True)
    assert main(["settings", "auto", "off", "--project", str(repo)]) == 0
    assert "Standing grant: off" in capsys.readouterr().out


def test_under_the_grant_the_startup_line_does_not_contradict_the_guidance(tmp_path, capsys):
    from modelquad.cli import main

    repo = _repo(tmp_path / "r")
    repo_settings.write_mode(repo, "offer-only")
    assert repo_settings.advice_line(repo) == repo_settings.ADVICE[repo_settings.OFFER]
    grant.write(True, directory=repo)
    assert repo_settings.advice_line(repo) is None
    assert main(["settings", "repo", "--project", str(repo)]) == 0
    assert "standing grant covers this folder" in capsys.readouterr().out
    repo_settings.write_mode(repo, "inherit")
    (repo / ".modelquad.toml").write_text('schema = 1\n[offers]\ncodex = "on_request"\n',
                                          encoding="utf-8")
    assert repo_settings.advice_line(repo) is None
    grant.write(False, directory=repo)
    assert repo_settings.advice_line(repo) == repo_settings.ADVICE[repo_settings.ON_REQUEST]
    # Off still reaches Claude, grant or not.
    grant.write(True, directory=repo)
    repo_settings.write_mode(repo, "off")
    assert repo_settings.advice_line(repo) == repo_settings.ADVICE[repo_settings.OFF]


def test_without_the_prompt_a_call_stays_inside_the_sessions_folder(tmp_path, monkeypatch):
    from modelquad.mcp import server

    project, other = tmp_path / "project", tmp_path / "other"
    (project / "sub").mkdir(parents=True)
    other.mkdir()
    monkeypatch.setattr(server, "_STANDING_GRANT", True)
    monkeypatch.setattr(server, "_SESSION_PROJECT", str(project))
    monkeypatch.setattr(server._grant, "allows", lambda directory: True)
    reached = []
    monkeypatch.setattr(server, "_notice_before_first_call",
                        lambda *args: reached.append(args[1]) or server._refused_result({"stop": True}))
    result = asyncio.run(server.modelquad_delegate(
        task_type="review", prompt="look", working_directory=str(other), timeout=10))
    payload = result.structured_content
    assert payload["refused"] is True and payload["started"] is False
    assert payload["reason"] == grant.OTHER_FOLDER
    assert reached == []
    for inside in (project, project / "sub"):
        asyncio.run(server.modelquad_delegate(
            task_type="review", prompt="look", working_directory=str(inside), timeout=10))
    assert reached == [str(project), str(project / "sub")]
    # With the per-call prompt (no grant at start) any folder may be named: the user sees it.
    monkeypatch.setattr(server, "_STANDING_GRANT", False)
    asyncio.run(server.modelquad_delegate(
        task_type="review", prompt="look", working_directory=str(other), timeout=10))
    assert reached[-1] == str(other)
