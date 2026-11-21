# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0

"""Release-facing MCP schema checks for the open-source export."""

from modelquad.mcp.server import server


def test_release_tools_are_small_and_delegation_requires_interaction():
    tools = server._tool_manager._tools
    assert set(tools) == {
        "review_package", "delegate", "review_verdict",
        "status",
    }
    # Without the user's standing grant (grant.py; the suite never has one),
    # Claude Code prompts for every call. tests/test_standing_grant.py covers
    # the session that starts in a granted folder.
    assert tools["delegate"].meta["anthropic/requiresUserInteraction"] is True
    # Only the tool that starts Codex asks: the others start nothing.
    for name in ("review_package", "review_verdict", "status"):
        assert "anthropic/requiresUserInteraction" not in (tools[name].meta or {}), name
    # Recording verdicts starts nothing and writes only the local ledger.
    verdict = tools["review_verdict"]
    assert verdict.annotations.open_world_hint is False


def test_every_tool_has_a_title_and_only_pure_reads_are_marked_read_only():
    tools = server._tool_manager._tools
    assert all(tool.title for tool in tools.values())
    read_only = {name for name, tool in tools.items()
                 if tool.annotations is not None and tool.annotations.read_only_hint}
    assert read_only == {"status", "review_package"}
    for name in read_only:
        assert tools[name].annotations.open_world_hint is False, name
    # A retry of a Codex call would send the code again and spend usage.
    hints = tools["delegate"].annotations
    assert hints.read_only_hint is False and hints.idempotent_hint is False
    assert hints.open_world_hint is True


def test_delegate_schema_has_only_read_only_release_arguments():
    properties = server._tool_manager._tools["delegate"].parameters["properties"]
    assert {"provider", "session_id", "agent_runtime", "decision_id", "expected_consumption",
            "user_approved", "capsule"}.isdisjoint(properties)
    assert properties["timeout"]["exclusiveMinimum"] == 0
    assert properties["timeout"]["maximum"] == 840


def test_no_tool_points_to_a_tool_that_does_not_exist():
    tools = server._tool_manager._tools
    for tool in tools.values():
        for gone in ("modelquad_route", "modelquad_handoff", "modelquad_split", "modelquad_job_",
                     "modelquad_activity", "modelquad_doctor", "modelquad_usage_refresh",
                     "modelquad_delegation_status"):
            assert gone not in (tool.description or ""), (tool.name, gone)


def test_security_md_names_the_tool_that_can_start_codex():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "SECURITY.md").read_text(encoding="utf-8")
    assert "`delegate`" in text
