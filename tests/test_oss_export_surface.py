# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0

"""Release-facing MCP schema checks for the open-source export."""

from modelquad.mcp.server import server


def test_release_tools_are_small_and_delegation_requires_interaction():
    tools = server._tool_manager._tools
    assert set(tools) == {
        "modelquad_status", "modelquad_delegate", "modelquad_handoff",
        "modelquad_delegation_status", "modelquad_job_status", "modelquad_job_collect",
        "modelquad_job_wait", "modelquad_job_acknowledge", "modelquad_job_stop",
        "modelquad_job_list_pending", "modelquad_doctor", "modelquad_usage_refresh",
        "modelquad_activity", "modelquad_activity_detail",
    }
    for name in ("modelquad_delegate", "modelquad_handoff"):
        assert tools[name].meta == {"anthropic/requiresUserInteraction": True}


def test_every_tool_has_a_title_and_only_pure_reads_are_marked_read_only():
    tools = server._tool_manager._tools
    assert all(tool.title for tool in tools.values())
    read_only = {name for name, tool in tools.items()
                 if tool.annotations is not None and tool.annotations.read_only_hint}
    assert read_only == {
        "modelquad_status", "modelquad_activity", "modelquad_activity_detail",
        "modelquad_delegation_status", "modelquad_doctor", "modelquad_job_collect",
        "modelquad_job_list_pending",
    }
    # A retry of a Codex call would send the code again and spend usage.
    for name in ("modelquad_delegate", "modelquad_handoff"):
        hints = tools[name].annotations
        assert hints.read_only_hint is False and hints.idempotent_hint is False
        assert hints.open_world_hint is True


def test_delegate_schema_has_only_read_only_release_arguments():
    properties = server._tool_manager._tools["modelquad_delegate"].parameters["properties"]
    assert {"provider", "session_id", "agent_runtime", "decision_id", "expected_consumption", "user_approved"}.isdisjoint(properties)
    assert properties["timeout"]["exclusiveMinimum"] == 0
    assert properties["timeout"]["maximum"] == 840

