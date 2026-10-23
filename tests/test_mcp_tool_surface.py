# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The MCP tool surface takes no settings from the model.

The Host's model reaches ModelQuad through these tools, and the server cannot
tell a call the model made from one the MCP App made on the user's behalf. So,
under the rule "Host tools cannot change settings the user set":

* no tool takes a settings file, or a settings or usage directory, as an
  argument -- a settings file deep-merged over the packaged configuration,
  and a usage directory let the model route on quota it wrote itself;
* no tool changes a persisted user setting -- consent, the routing
  mode (which decides whether an automatic delegation is allowed), the decision
  mode, the resource strategy, or the routing preset and weights.

These tests read the registered schemas and the settings table, not only the
docstrings, so they fail if either promise is broken by a new tool as well as
by an old parameter coming back.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import pytest

from conftest import tool_payload

from modelquad.mcp import server as mcp_server
from modelquad.service import db as service_db


# -- the schemas take no settings file and no settings or usage directory -----

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
#: settings, quota or state storage; a new directory argument fails until its
#: purpose is explicitly reviewed here.
_ALLOWED_PATH_PARAMETERS = frozenset(
    {
        ("modelquad_delegate", "working_directory"),
        # The project folder the user-approved handoff runs in; the same
        # workspace selector `modelquad_delegate` takes, nothing more.
        ("modelquad_handoff", "working_directory"),
        ("modelquad_delegation_status", "working_directory"),
        # Activity can be scoped to one project; read-only filtering.
        ("modelquad_activity", "project_directory"),
        ("modelquad_activity_detail", "project_directory"),
        # Job tools require the caller's project and refuse jobs from another
        # workspace. Exact-folder read-only filtering of retained jobs in the
        # server-owned store; this grants no settings/grant writer and cannot
        # select another state or usage directory.
        ("modelquad_job_status", "project_directory"),
        ("modelquad_job_collect", "project_directory"),
        ("modelquad_job_wait", "project_directory"),
        ("modelquad_job_acknowledge", "project_directory"),
        ("modelquad_job_stop", "project_directory"),
        ("modelquad_job_list_pending", "project_directory"),
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


def test_no_tool_takes_a_settings_file_or_a_settings_or_usage_directory():
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
    parameters["modelquad_doctor"] = parameters["modelquad_doctor"] | {name}
    assert _forbidden_path_parameters(parameters) == [("modelquad_doctor", name)]


def test_the_contract_would_catch_working_directory_on_a_new_tool():
    parameters = _tool_parameters()
    parameters["modelquad_status"] = parameters["modelquad_status"] | {"working_directory"}
    assert _forbidden_path_parameters(parameters) == [("modelquad_status", "working_directory")]


def test_a_tool_argument_naming_a_directory_is_never_read(tmp_path, monkeypatch):
    """An argument the schema no longer declares reaches nothing.

    The usage directory the model names holds a quota snapshot it wrote; the
    one ModelQuad reads holds none. Routing must see the second.
    """

    fabricated = tmp_path / "fabricated"
    fabricated.mkdir()
    (fabricated / "usage-codex.json").write_text(
        '{"available": true, "five_hour_remaining_percentage": 100, "seven_day_remaining_percentage": 100}',
        encoding="utf-8",
    )
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setattr(mcp_server, "USAGE_DIRECTORY", str(real))
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", tmp_path / "surface.db")

    payload = tool_payload(
        asyncio.run(
            mcp_server.server.call_tool(
                "modelquad_status",
                {"usage_directory": str(fabricated), "config_path": str(tmp_path / "evil.yaml")},
            )
        )
    )
    codex = next(item for item in payload["providers"] if item["name"] == "codex")
    assert codex["remaining_percentage"] != 100


# -- no tool changes a protected setting --------------------------------------

#: Every settings-table key a user sets and a Host tool must not change.
PROTECTED_SETTINGS_KEYS = (
    "routing_settings",
    "telemetry_consent",
    "plan_advice_enabled",
    "routing_mode",
    "routing_paused",
    "job_runner_enabled",
    "decision_mode",
    "session_resource_strategy",
    "provider_overrides",
    "consent_state",
)

#: Each former setter with the arguments it used to apply. They are no longer
#: registered; a call naming them must be refused and change nothing.
FORMER_SETTER_CALLS = (
    (
        "modelquad_settings_update",
        {
            "preset": "fastest",
            "fit_weight": 0.1,
            "usage_weight": 0.9,
            "telemetry_consent": True,
        },
    ),
    ("modelquad_routing_mode", {"mode": "manual"}),
    ("modelquad_decision_mode", {"mode": "reasoning_primary"}),
    ("modelquad_set_resource_strategy", {"strategy": {"claude": "spend", "codex": "preserve"}}),
)


def _protected_rows(db_path: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    finally:
        conn.close()
    return {key: rows.get(key) for key in PROTECTED_SETTINGS_KEYS}


@pytest.fixture
def user_settings(tmp_path, monkeypatch):
    """A database whose every protected key holds a user value, seeded directly."""

    db_path = tmp_path / "user-settings.db"
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)
    conn = service_db.connect_and_init(db_path)
    try:
        with conn:
            for key in PROTECTED_SETTINGS_KEYS:
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (key, json.dumps({"user_value": key})),
                )
    finally:
        conn.close()
    before = _protected_rows(db_path)
    assert all(value is not None for value in before.values()), before
    return db_path, before


@pytest.mark.parametrize("tool,arguments", FORMER_SETTER_CALLS, ids=[call[0] for call in FORMER_SETTER_CALLS])
def test_a_former_setter_is_unregistered_and_changes_no_protected_setting(user_settings, tool, arguments):
    db_path, before = user_settings

    assert tool not in _tool_parameters()
    with pytest.raises(mcp_server.ToolError):
        asyncio.run(mcp_server.server.call_tool(tool, arguments))

    assert _protected_rows(db_path) == before


def test_the_read_only_release_tools_change_no_protected_setting(user_settings, monkeypatch, tmp_path):
    db_path, before = user_settings
    monkeypatch.setattr(mcp_server, "USAGE_DIRECTORY", str(tmp_path / "usage"))
    for tool in ("modelquad_status", "modelquad_doctor"):
        asyncio.run(mcp_server.server.call_tool(tool, {}))
    assert _protected_rows(db_path) == before


# -- no MCP code path calls a protected writer --------------------------------

_MCP_PACKAGE = Path(mcp_server.__file__).resolve().parent

#: The writers of the protected keys. A call to any of them from the MCP
#: package is a tool path that changes a user setting.
_PROTECTED_WRITERS = (
    "put_settings",
    "put_telemetry_consent",
    "put_plan_advice_enabled",
    "put_routing_mode",
    "put_routing_paused",
    "put_job_runner_enabled",
    "put_decision_mode",
    "put_resource_strategy",
    "put_provider_override",
    "put_consent_tier",
    "put_consent_state",
)


@pytest.mark.parametrize("module", sorted(path.name for path in _MCP_PACKAGE.glob("*.py")))
def test_no_mcp_module_calls_a_protected_settings_writer(module):
    source = (_MCP_PACKAGE / module).read_text(encoding="utf-8")
    called = [writer for writer in _PROTECTED_WRITERS if re.search(rf"\b{writer}\s*\(", source)]
    assert called == [], f"{module} calls {called}"


def test_the_writer_scan_covers_server_py():
    assert (_MCP_PACKAGE / "server.py").is_file()


def test_legacy_tools_are_not_registered():
    assert {"route_task", "provider_status", "delegate_task"}.isdisjoint(_tool_parameters())


# -- repository grants are independently protected from settings -------------

PROTECTED_REPO_TABLES = ("repo_registrations", "repo_policy_versions")
PROTECTED_REPO_WRITERS = ("register_repo", "put_repo_policy", "set_repo_state", "unregister_repo")
PROTECTED_REPO_ROUTES = ("/api/repos", "/repos/register", "/repos/{repo_key}/policy", "/repos/{repo_key}/state")


@pytest.mark.parametrize("table", PROTECTED_REPO_TABLES)
def test_mcp_calls_cannot_change_repository_grant_tables(tmp_path, monkeypatch, table):
    db_path = tmp_path / "protected-repos.db"
    service_db.connect_and_init(db_path).close()
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)

    def snapshot():
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        finally:
            conn.close()

    before = snapshot()
    for tool, arguments in FORMER_SETTER_CALLS:
        with pytest.raises(mcp_server.ToolError):
            asyncio.run(mcp_server.server.call_tool(tool, arguments))
        assert snapshot() == before
    # Prospective MCP setters remain unregistered; supplying their arguments
    # must reach neither HTTP routes nor the two independent grant tables.
    for tool in ("modelquad_repo_register", "modelquad_repo_policy", "modelquad_repo_state", "modelquad_repo_unregister"):
        assert tool not in _tool_parameters()
        with pytest.raises(mcp_server.ToolError):
            asyncio.run(mcp_server.server.call_tool(tool, {
                "repo_key": "a" * 32, "path": str(tmp_path),
                "policy": {"metered_allowed": True}, "state": "active",
            }))
        assert snapshot() == before


@pytest.mark.parametrize("module", sorted(str(path.relative_to(_MCP_PACKAGE)) for path in _MCP_PACKAGE.rglob("*.py")))
def test_no_mcp_module_calls_repository_writers_or_routes_or_mutates_grant_tables(module):
    source = (_MCP_PACKAGE / module).read_text(encoding="utf-8")
    called = [writer for writer in PROTECTED_REPO_WRITERS if re.search(rf"\b{writer}\s*\(", source)]
    assert called == [], f"{module} calls {called}"
    # Substring matching is a tripwire, not complete coverage of dynamically
    # constructed routes (for example f-strings or concatenation).
    assert not any(route in source for route in PROTECTED_REPO_ROUTES), module
    for table in PROTECTED_REPO_TABLES:
        assert re.search(rf"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO|DROP\s+TABLE)\s+{table}\b", source, re.I) is None, module


@pytest.mark.parametrize("name", ["repo_policy", "repo_state", "metered_allowed", "allowed_runtimes"])
def test_repo_policy_argument_guard_would_catch_future_setters(name):
    parameters = _tool_parameters()
    parameters["modelquad_status"] = parameters["modelquad_status"] | {name}
    assert _forbidden_path_parameters(parameters) == [("modelquad_status", name)]
