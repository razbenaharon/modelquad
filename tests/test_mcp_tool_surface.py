# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The MCP tool surface takes no settings from the model.

The Host's model reaches ModelQuad through these tools, and the server cannot
tell a call the model made from one made on the user's behalf. So, under the
rule "Host tools cannot change settings the user set":

* no tool takes a settings file, or a settings or state directory, as an
  argument -- a folder the model names could hold settings it wrote itself;
* no tool changes a persisted user setting -- the on/off switch, the standing
  grant, the proactive choice, a repository's own setting -- all of which live
  in ``preferences.json``.

These tests read the registered schemas and the stored preferences, not only
the docstrings, so they fail if either promise is broken by a new tool as well
as by an old parameter coming back.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from conftest import tool_payload

from modelquad import grant, repo_settings, switch
from modelquad.adapters.server_instructions import preferences_path, write_proactive
from modelquad.mcp import server as mcp_server, worker_guard


# -- the schemas take no settings file and no settings or state directory ------

#: The parameter names a tool must never take from the model.
_FORBIDDEN_NAMES = frozenset({
    "config_path", "usage_directory", "repo_policy", "repo_policy_json",
    "repo_registrations", "repo_policy_versions", "repo_state",
    "metered_allowed", "allowed_runtimes",
    "format_version", "capabilities", "roles", "strategy",
    "actor", "repo_key", "context_key", "state_root", "installation_key", "retention",
})
_PATH_LIKE = re.compile(r"(_path|_dir|directory)$")

#: Project/workspace selectors are pinned tool by tool. They cannot redirect
#: settings or state storage; a new directory argument fails until its
#: purpose is explicitly reviewed here.
_ALLOWED_PATH_PARAMETERS = frozenset(
    {
        ("modelquad_delegate", "working_directory"),
        # The same workspace selector the review takes; it shows what the
        # review would send and starts nothing.
        ("modelquad_review_package", "working_directory"),
        # The project whose per-repo setting and .modelquad.toml the status
        # reply describes; read-only.
        ("modelquad_status", "project_directory"),
    }
)


def _tool_parameters() -> dict[str, set[str]]:
    tools = asyncio.run(mcp_server.server.list_tools())
    return {tool.name: set((tool.input_schema or {}).get("properties", {})) for tool in tools}


def _forbidden_path_parameters(parameters: dict[str, set[str]]) -> list[tuple[str, str]]:
    found = []
    for tool, names in parameters.items():
        for name in names:
            if (tool, name) in _ALLOWED_PATH_PARAMETERS:
                continue
            if name in _FORBIDDEN_NAMES or _PATH_LIKE.search(name):
                found.append((tool, name))
    return sorted(found)


def test_no_tool_takes_a_settings_file_or_a_settings_or_state_directory():
    parameters = _tool_parameters()
    assert parameters, "no MCP tools are registered"
    assert _forbidden_path_parameters(parameters) == []


def test_the_allowed_path_parameters_all_still_exist():
    """A stale allow-list entry would quietly widen the exception."""

    parameters = _tool_parameters()
    for tool, name in _ALLOWED_PATH_PARAMETERS:
        assert name in parameters.get(tool, set()), (tool, name)


@pytest.mark.parametrize("name", ["config_path", "usage_directory", "settings_path", "state_dir", "cache_directory"])
def test_the_contract_would_catch_the_parameter_coming_back(name):
    """The check itself, against a schema with the parameter added back."""

    parameters = _tool_parameters()
    parameters["modelquad_review_verdict"] = parameters["modelquad_review_verdict"] | {name}
    assert _forbidden_path_parameters(parameters) == [("modelquad_review_verdict", name)]


def test_the_contract_would_catch_working_directory_on_a_new_tool():
    parameters = _tool_parameters()
    parameters["modelquad_status"] = parameters["modelquad_status"] | {"working_directory"}
    assert _forbidden_path_parameters(parameters) == [("modelquad_status", "working_directory")]


def test_a_tool_argument_naming_a_settings_folder_is_never_read(tmp_path, monkeypatch):
    """An argument the schema does not declare reaches nothing.

    The folder the model names holds a preferences file saying ModelQuad is
    off; the one ModelQuad reads says on. The status must report the second.
    """

    from modelquad import settings_view

    monkeypatch.setattr(settings_view, "_claude_installed", lambda: True)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    fabricated = tmp_path / "fabricated"
    fabricated.mkdir()
    switch.write(False, path=fabricated / preferences_path().name)
    switch.write(True)

    payload = tool_payload(
        asyncio.run(
            mcp_server.server.call_tool(
                "modelquad_status",
                {"state_root": str(fabricated), "config_path": str(fabricated / "preferences.json"),
                 "usage_directory": str(fabricated)},
            )
        )
    )
    assert payload["modelquad"] == "on"


# -- no tool changes a protected setting --------------------------------------

#: Each former or prospective setter with the arguments it would apply. None
#: is registered; a call naming one must be refused and change nothing.
FORMER_SETTER_CALLS = (
    ("modelquad_settings_update", {"preset": "fastest", "telemetry_consent": True}),
    ("modelquad_routing_mode", {"mode": "manual"}),
    ("modelquad_decision_mode", {"mode": "reasoning_primary"}),
    ("modelquad_set_resource_strategy", {"strategy": {"claude": "spend", "codex": "preserve"}}),
    ("modelquad_settings", {"proactive": True, "auto": "on"}),
    ("modelquad_grant", {"on": True, "everywhere": True}),
    ("modelquad_on", {}),
    ("modelquad_repo_state", {"repo_key": "a" * 32, "state": "active"}),
    ("modelquad_repo_policy", {"repo_key": "a" * 32, "policy": {"metered_allowed": True}}),
)


@pytest.fixture
def user_settings(tmp_path, monkeypatch):
    """Preferences where every protected choice holds a user value, written by the real writers."""

    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    switch.write(True)
    write_proactive(False)
    grant.write(False, directory=repo)
    repo_settings.write_mode(other, "off")
    path = preferences_path()
    before = path.read_bytes()
    assert before
    return repo, path, before


@pytest.mark.parametrize("tool,arguments", FORMER_SETTER_CALLS, ids=[call[0] for call in FORMER_SETTER_CALLS])
def test_a_former_setter_is_unregistered_and_changes_no_protected_setting(user_settings, tool, arguments):
    _repo, path, before = user_settings

    assert tool not in _tool_parameters()
    with pytest.raises(mcp_server.ToolError):
        asyncio.run(mcp_server.server.call_tool(tool, arguments))

    assert path.read_bytes() == before


def test_the_read_only_tools_change_no_protected_setting(user_settings):
    repo, path, before = user_settings
    asyncio.run(mcp_server.server.call_tool("modelquad_status", {}))
    asyncio.run(mcp_server.server.call_tool("modelquad_status", {"project_directory": str(repo)}))
    asyncio.run(mcp_server.server.call_tool("modelquad_review_package", {
        "task_type": "review", "prompt": "Look.", "working_directory": str(repo), "files": ["a.py"]}))
    assert path.read_bytes() == before


# -- no MCP code path calls a protected writer --------------------------------

_MCP_PACKAGE = Path(mcp_server.__file__).resolve().parent
_REVIEW_MODULE = _MCP_PACKAGE.parent / "review.py"

#: The writers of the protected preferences. A call to any of them from the
#: MCP package, or from the review flow it runs, is a tool path that changes a
#: user setting.
_PROTECTED_WRITERS = (
    r"switch\.write",
    r"grant\.write",
    r"write_proactive",
    r"write_claude_plugin",
    r"write_mode",
    r"preferences_for_write",
)


def _scanned_modules() -> list[Path]:
    return sorted(_MCP_PACKAGE.rglob("*.py")) + [_REVIEW_MODULE]


@pytest.mark.parametrize("module", _scanned_modules(), ids=lambda path: path.name)
def test_no_mcp_module_calls_a_protected_settings_writer(module):
    source = module.read_text(encoding="utf-8")
    called = [writer for writer in _PROTECTED_WRITERS if re.search(rf"\b{writer}\s*\(", source)]
    assert called == [], f"{module.name} calls {called}"


def test_the_writer_scan_covers_server_py_and_the_review_flow():
    names = {path.name for path in _scanned_modules()}
    assert {"server.py", "review.py"} <= names


def test_legacy_tools_are_not_registered():
    assert {"route_task", "provider_status", "delegate_task"}.isdisjoint(_tool_parameters())


@pytest.mark.parametrize("name", ["repo_policy", "repo_state", "metered_allowed", "allowed_runtimes"])
def test_repo_policy_argument_guard_would_catch_future_setters(name):
    parameters = _tool_parameters()
    parameters["modelquad_status"] = parameters["modelquad_status"] | {name}
    assert _forbidden_path_parameters(parameters) == [("modelquad_status", name)]
