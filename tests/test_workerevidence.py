# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Codex event-stream normalization.

The load-bearing property is the REDACTION BOUNDARY: structured worker output
is rich precisely because it embeds command lines, full tool stdout/stderr,
and absolute paths. Only counts, totals, and closed-vocabulary tokens may
cross out of this module.
"""

from __future__ import annotations

import json

from modelquad.workerevidence import (
    WorkerMetrics,
    normalize_codex_json_events,
)

SECRET_COMMAND = "aws s3 cp s3://prod/secrets.env . --profile prod-admin"
SECRET_PATH = "C:\\Users\\SecretUser\\PrivateRepo\\src\\credentials.py"
SECRET_OUTPUT = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _codex_stream() -> str:
    """A realistic codex exec --json stream, shaped per codex-cli 0.147.0."""
    events = [
        {"type": "thread.started", "thread_id": "0199c2f1-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "item_type": "command_execution",
                "command": SECRET_COMMAND,
                "aggregated_output": SECRET_OUTPUT,
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "item_type": "command_execution",
                "command": "pytest -q",
                "aggregated_output": "1 failed",
                "exit_code": 1,
                "status": "failed",
            },
        },
        {
            "type": "item.completed",
            "item": {"item_type": "file_change", "changes": [{"path": SECRET_PATH, "update": {}}]},
        },
        {"type": "item.completed", "item": {"item_type": "web_search"}},
        {"type": "item.completed", "item": {"item_type": "mcp_tool_call", "server": "x", "arguments": {}}},
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": "done"}},
        {"type": "item.completed", "item": {"item_type": "reasoning", "text": "thinking"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 17510,
                "cached_input_tokens": 11008,
                "cache_write_input_tokens": 0,
                "output_tokens": 165,
                "reasoning_output_tokens": 30,
                "total_tokens": 17675,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


# --- codex normalization ----------------------------------------------------


def test_codex_stream_yields_counts_and_tokens():
    metrics = normalize_codex_json_events(_codex_stream())
    assert metrics.parse_status == "ok"
    # agent_message and reasoning are model output, not tool use.
    assert metrics.tool_calls_total == 5
    assert metrics.tool_calls_by_category == {
        "shell": 2,
        "file_edit": 1,
        "search": 1,
        "mcp_tool": 1,
    }
    assert metrics.shell_command_count == 2
    assert metrics.file_change_count == 1
    assert metrics.failed_tool_calls == 1
    assert metrics.input_tokens == 17510
    assert metrics.output_tokens == 165
    assert metrics.cached_input_tokens == 11008
    assert metrics.reasoning_tokens == 30
    assert metrics.total_tokens == 17675
    assert metrics.turn_count == 1
    assert metrics.session_id == "0199c2f1-thread"


def test_codex_normalization_never_leaks_commands_output_or_paths():
    """The redaction boundary, asserted against the serialized result."""
    metrics = normalize_codex_json_events(_codex_stream())
    serialized = json.dumps(metrics.to_dict())
    assert SECRET_COMMAND not in serialized
    assert SECRET_OUTPUT not in serialized
    assert SECRET_PATH not in serialized
    assert "aws" not in serialized.lower()
    assert "SecretUser" not in serialized
    assert "pytest" not in serialized
    # Only the declared safe keys exist at all.
    assert set(metrics.to_dict()) == {
        "source",
        "tool_calls_total",
        "tool_calls_by_category",
        "shell_command_count",
        "file_change_count",
        "failed_tool_calls",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "total_tokens",
        "turn_count",
        "error_kind",
        "session_id",
        "parse_status",
        "structured_errors",
        "rate_limit_events",
        "permission_blocks",
        "provider_reported_duration_seconds",
        "provider_status",
    }


def test_codex_typed_error_kind_is_captured_without_its_message():
    stream = json.dumps(
        {
            "type": "turn.failed",
            "error": {"type": "usage_limit_reached", "message": f"failed running {SECRET_COMMAND}"},
        }
    )
    metrics = normalize_codex_json_events(stream)
    assert metrics.error_kind == "usage_limit_reached"
    assert SECRET_COMMAND not in json.dumps(metrics.to_dict())


def test_malformed_output_is_handled_safely():
    # Untrusted worker output must never raise.
    assert normalize_codex_json_events("not json at all").parse_status == "unparseable"
    assert normalize_codex_json_events("").parse_status == "empty"
    assert normalize_codex_json_events(None).parse_status == "empty"
    assert normalize_codex_json_events("{ broken").parse_status == "unparseable"
    assert normalize_codex_json_events("[1,2,3]").parse_status == "unparseable"


def test_partially_malformed_stream_is_marked_partial_not_ok():
    stream = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            "{ this line is broken",
            json.dumps({"type": "item.completed", "item": {"item_type": "command_execution"}}),
        ]
    )
    metrics = normalize_codex_json_events(stream)
    assert metrics.parse_status == "partial"
    assert metrics.tool_calls_total == 1


def test_unreported_metrics_stay_unknown_not_zero():
    """A turn with no usage block must not claim zero tokens."""
    stream = json.dumps({"type": "turn.completed"})
    metrics = normalize_codex_json_events(stream)
    assert metrics.input_tokens is None
    assert metrics.output_tokens is None
    assert metrics.total_tokens is None
    # But counts the parser genuinely observed are real zeros.
    assert metrics.tool_calls_total == 0


def test_unknown_item_type_counts_as_other_not_dropped():
    stream = json.dumps(
        {"type": "item.completed", "item": {"item_type": "some_future_tool"}}
    )
    metrics = normalize_codex_json_events(stream)
    assert metrics.tool_calls_by_category == {"other": 1}
    assert metrics.tool_calls_total == 1


def test_metrics_default_to_unknown():
    metrics = WorkerMetrics(source="unavailable")
    payload = metrics.to_dict()
    assert payload["tool_calls_total"] is None
    assert payload["total_tokens"] is None
    assert payload["tool_calls_by_category"] == {}
