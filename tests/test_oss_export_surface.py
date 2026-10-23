# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0

"""Release-facing MCP schema checks for the open-source export."""

from orchestrator.mcp.server import server


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


def test_delegate_schema_has_only_read_only_release_arguments():
    properties = server._tool_manager._tools["modelquad_delegate"].parameters["properties"]
    assert {"provider", "session_id", "agent_runtime", "decision_id", "expected_consumption", "user_approved"}.isdisjoint(properties)
    assert properties["timeout"]["exclusiveMinimum"] == 0
    assert properties["timeout"]["maximum"] == 840
