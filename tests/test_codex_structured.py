# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Codex structured execution and the privacy boundary.

The point of structured mode is better behavioral evidence. The *condition*
of structured mode is that none of the raw material it is derived from --
prompts, command lines, tool stdout/stderr, absolute paths, secrets -- may
survive into anything ModelQuad persists, returns, logs, or raises.

These tests are adversarial by construction: every fixture carries planted
secrets, and the assertions hunt for them across every surface a real
delegation touches.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from orchestrator.execution.base import ExecutionRequest, WorkerTaskStatus
from orchestrator.execution.codex import CodexExecutor
from orchestrator.models import TaskType

# --- planted secrets -------------------------------------------------------
# Canary values for the adversarial privacy assertions below.
#
# These are NOT credentials. They are deliberately shaped to be
# LOW-ENTROPY and to carry no credential prefix (no "sk-", no random
# alphanumeric run), because a realistic-looking fake key trips secret
# scanners and every false positive makes a future real leak easier to
# miss. Keep them readable and boring: the tests only need a distinctive
# token to prove never reaches persistence.
CANARY_APIKEY = "canary-apikey-must-never-be-persisted"
CANARY_PASSWORD = "canary-password-must-never-be-persisted"
WINDOWS_PATH = "C:\\Users\\SecretPerson\\PrivateRepo\\src\\billing.py"
UNIX_HOME = "/home/secretperson/work/acme-internal/auth.py"
REPO_NAME = "acme-internal-payments"
PRIVATE_FILENAME = "customer_ssns.csv"
SOURCE_FRAGMENT = "def charge_card(pan, cvv):\n    return gateway.post(pan, cvv)"
SHELL_WITH_SECRET = f"curl -H 'Authorization: Bearer {CANARY_APIKEY}' https://api.internal/x"
STDERR_WITH_CREDS = f"FATAL: auth failed for user=root password={CANARY_PASSWORD}"
RAW_PROMPT = f"Fix the bug in {WINDOWS_PATH}; the key is {CANARY_APIKEY}"

ALL_SECRETS = (
    CANARY_APIKEY,
    CANARY_PASSWORD,
    WINDOWS_PATH,
    UNIX_HOME,
    REPO_NAME,
    PRIVATE_FILENAME,
    SOURCE_FRAGMENT,
    SHELL_WITH_SECRET,
    STDERR_WITH_CREDS,
    RAW_PROMPT,
    "SecretPerson",
    "secretperson",
)


def _hostile_stream(envelope_status: str = "completed") -> str:
    """A realistic codex --json stream whose every event carries a secret."""
    envelope = (
        "Work finished.\n\n```json\n"
        + json.dumps({"task_status": envelope_status, "reason": "done", "evidence": {"summary": "ok"}})
        + "\n```"
    )
    events = [
        {"type": "thread.started", "thread_id": "thread-abc-123"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "item_type": "command_execution",
                "command": SHELL_WITH_SECRET,
                "aggregated_output": STDERR_WITH_CREDS,
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "item_type": "command_execution",
                "command": f"pytest {PRIVATE_FILENAME}",
                "aggregated_output": SOURCE_FRAGMENT,
                "exit_code": 1,
                "status": "failed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "item_type": "file_change",
                "changes": [{"path": WINDOWS_PATH, "update": {"diff": SOURCE_FRAGMENT}}],
            },
        },
        {
            "type": "item.completed",
            "item": {"item_type": "file_change", "changes": [{"path": UNIX_HOME}]},
        },
        {
            "type": "item.completed",
            "item": {
                "item_type": "mcp_tool_call",
                "server": REPO_NAME,
                "arguments": {"key": CANARY_APIKEY},
                "result": SOURCE_FRAGMENT,
            },
        },
        {"type": "item.completed", "item": {"item_type": "web_search", "query": REPO_NAME}},
        {"type": "item.completed", "item": {"item_type": "reasoning", "text": RAW_PROMPT}},
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": envelope}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 31000,
                "cached_input_tokens": 12000,
                "cache_write_input_tokens": 0,
                "output_tokens": 6200,
                "reasoning_output_tokens": 410,
                "total_tokens": 37200,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


def _clean_stream(envelope_status: str = "completed") -> str:
    """The same run without its tool events: what a no-shell worker may emit."""
    events = [json.loads(line) for line in _hostile_stream(envelope_status).splitlines()]
    kept = [event for event in events
            if event.get("item", {}).get("item_type") not in {"command_execution", "file_change",
                                                              "mcp_tool_call", "web_search"}]
    return "\n".join(json.dumps(event) for event in kept)


class _Run:
    """A subprocess.run stand-in returning a canned CompletedProcess."""

    def __init__(self, stdout="", stderr="", returncode=0, exc=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.exc = exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture
def execute(tmp_path, synthetic_worker_binary, qualified_codex_inventory):
    def run_request(run, *, prompt=RAW_PROMPT, **request_kwargs):
        executor = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run,
                                 inventory=qualified_codex_inventory)
        request = ExecutionRequest(
            provider="codex",
            task_type=TaskType.REVIEW,
            prompt=prompt,
            working_directory=tmp_path,
            **request_kwargs,
        )
        return executor.execute(request)

    return run_request


# --- structured mode mechanics ---------------------------------------------


def test_structured_mode_passes_json_and_parses_the_envelope(execute):
    run = _Run(stdout=_clean_stream())
    result = execute(run)

    assert "--json" in run.calls[0]["argv"]
    assert result.metadata["evidence_mode"] == "structured"
    # The completion envelope arrived inside agent_message, not raw stdout.
    assert result.task_status is WorkerTaskStatus.COMPLETED
    metrics = result.metadata["worker_metrics"]
    assert metrics["tool_calls_total"] == 0
    assert metrics["input_tokens"] == 31000
    assert metrics["output_tokens"] == 6200
    assert metrics["reasoning_tokens"] == 410
    assert metrics["turn_count"] == 1
    assert metrics["session_id"] == "thread-abc-123"


def test_a_stream_with_tool_events_is_withheld_whole(execute):
    """A no-shell worker that ran a tool is a violation, never a parsed answer."""
    result = execute(_Run(stdout=_hostile_stream()))
    assert result.task_status_reason == "noshell_violated"
    assert result.output == ""
    assert result.metadata["noshell_violated"] is True


# --- semantic completion doctrine survives ---------------------------------


def test_structured_stream_without_an_envelope_is_unknown_not_success(execute):
    """A finished stream and exit 0 are NOT completion evidence."""
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": "All done!"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ]
    result = execute(_Run(stdout="\n".join(json.dumps(e) for e in events)))
    assert result.metadata["evidence_mode"] == "structured"
    assert result.task_status is WorkerTaskStatus.UNKNOWN
    assert result.task_status_reason == "no_completion_envelope"


def test_structured_blocked_envelope_stays_blocked(execute):
    result = execute(_Run(stdout=_clean_stream(envelope_status="blocked")))
    assert result.task_status is WorkerTaskStatus.BLOCKED


def test_structured_partial_and_failed_envelopes_survive(execute):
    assert execute(_Run(stdout=_clean_stream(envelope_status="partial"))).task_status is WorkerTaskStatus.PARTIAL
    assert execute(_Run(stdout=_clean_stream(envelope_status="failed"))).task_status is WorkerTaskStatus.FAILED


def test_nonzero_exit_still_dominates_a_completed_envelope(execute):
    result = execute(_Run(stdout=_clean_stream(), returncode=3))
    assert result.task_status is WorkerTaskStatus.FAILED
    assert result.task_status_reason == "process_exit_3"


@pytest.mark.parametrize("stdout", [
    "this is not JSON at all, just narration",
    "narration\n```json\n{\"task_status\": \"completed\"}\n```",
    "",
], ids=["narration", "plain-envelope", "empty"])
def test_unparseable_output_is_never_read_as_an_answer(execute, stdout):
    # No plain-text fallback: an unverifiable stream is withheld.
    result = execute(_Run(stdout=stdout, stderr="```json\n{\"task_status\": \"completed\"}\n```"))
    assert result.output == ""
    assert result.task_status_reason == "worker_noshell_unavailable"
    assert result.task_completed is False


def test_early_termination_mid_stream_is_not_success(execute):
    truncated = _clean_stream().split("\n")[:3]
    result = execute(_Run(stdout="\n".join(truncated) + '\n{"type": "item.comp'))
    assert result.task_completed is False
    assert result.output == ""


# --- THE PRIVACY BOUNDARY --------------------------------------------------


def _assert_no_secrets(blob: str, *, where: str):
    for secret in ALL_SECRETS:
        assert secret not in blob, f"{secret!r} leaked into {where}"


@pytest.mark.parametrize("stream", [_hostile_stream, _clean_stream], ids=["hostile", "clean"])
def test_no_secret_survives_into_execution_result_metadata(execute, stream):
    result = execute(_Run(stdout=stream(), stderr=STDERR_WITH_CREDS))
    _assert_no_secrets(json.dumps(result.metadata, default=str), where="ExecutionResult.metadata")
    serialized = json.dumps(result.metadata, default=str)
    # None of the event scaffolding survives either.
    for marker in ("item.completed", "aggregated_output", "thread.started", "turn.completed"):
        assert marker not in serialized


@pytest.mark.parametrize("stream", [_hostile_stream, _clean_stream], ids=["hostile", "clean"])
def test_secrets_do_not_reach_sqlite_history(tmp_path, execute, stream):
    from orchestrator.service import history as service_history

    db = tmp_path / "history.db"
    result = execute(_Run(stdout=stream(), stderr=STDERR_WITH_CREDS))

    service_history.record_execution(
        db, "review", result, duration_seconds=2.0, decision_id="d-1",
        project=tmp_path.name,
    )
    # Directly against the raw stored row.
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT * FROM execution_history").fetchall()
    finally:
        conn.close()
    _assert_no_secrets(json.dumps([list(map(str, row)) for row in rows]), where="SQLite row")


def test_secrets_do_not_reach_outcome_evidence(tmp_path, execute):
    from orchestrator.outcome import finalize_execution_result

    result = finalize_execution_result(execute(_Run(stdout=_clean_stream(), stderr=STDERR_WITH_CREDS)),
                                       working_directory=tmp_path)
    assert result.evidence is not None
    _assert_no_secrets(json.dumps(result.evidence.to_dict(), default=str), where="OutcomeEvidence")


def test_secrets_do_not_reach_error_text_on_failure(execute):
    """A failing run must not turn provider events or stderr into an error body."""
    result = execute(_Run(stdout=_clean_stream(), stderr=STDERR_WITH_CREDS, returncode=1))
    _assert_no_secrets(json.dumps(result.metadata, default=str), where="metadata on failure")
    assert result.error is not None
    assert CANARY_PASSWORD not in result.error
    assert "withheld from persistence" in result.error


def test_stderr_body_is_not_persisted(execute):
    """Only stderr's SHAPE is kept; a narration body can carry credentials."""
    result = execute(_Run(stdout=_clean_stream(), stderr=STDERR_WITH_CREDS))
    assert "stderr" not in result.metadata
    assert result.metadata["stderr_bytes"] == len(STDERR_WITH_CREDS)
    # The category is derived from shape; no substring of the input escapes.
    assert result.metadata["stderr_category"] == "error"


def test_timeout_path_never_persists_a_secret_bearing_stderr(execute):
    exc = subprocess.TimeoutExpired(cmd="codex", timeout=1.0)
    exc.stderr = STDERR_WITH_CREDS
    exc.stdout = SOURCE_FRAGMENT
    result = execute(_Run(exc=exc))
    assert result.task_status is WorkerTaskStatus.FAILED
    assert result.task_status_reason == "timed_out"
    assert result.metadata["timed_out"] is True
    _assert_no_secrets(json.dumps(result.metadata, default=str), where="timeout metadata")


def test_the_prompt_never_appears_in_argv(execute):
    """Codex takes the prompt on stdin; argv is persisted, so it must stay clean."""
    run = _Run(stdout=_clean_stream())
    execute(run, prompt=RAW_PROMPT)
    _assert_no_secrets(json.dumps(run.calls[0]["argv"]), where="argv")


def test_caller_output_is_assistant_text_not_the_event_stream(execute):
    """The caller gets the model's message, not JSONL scaffolding."""
    result = execute(_Run(stdout=_clean_stream()))
    assert "item.completed" not in result.output
    assert "Work finished." in result.output


# --- provenance honesty ----------------------------------------------------


def test_model_stays_requested_never_confirmed(execute):
    """codex --json carries no model field; confirmation stays UNKNOWN."""
    result = execute(_Run(stdout=_clean_stream()), model="gpt-5-codex")
    assert result.metadata["requested_model"] == "gpt-5-codex"
    assert result.metadata["confirmed_model"] is None


def test_unreported_token_counts_stay_unknown(execute):
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": "```json\n{\"task_status\": \"completed\"}\n```"}},
        {"type": "turn.completed"},
    ]
    metrics = execute(_Run(stdout="\n".join(json.dumps(e) for e in events))).metadata["worker_metrics"]
    assert metrics["input_tokens"] is None
    assert metrics["total_tokens"] is None
    # A genuinely observed zero is still a zero.
    assert metrics["tool_calls_total"] == 0
    # Codex emits no permission-denial event: None, not a misleading 0.
    assert metrics["permission_blocks"] is None


def test_rate_limit_error_is_counted_and_typed(execute):
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.failed", "error": {"type": "usage_limit_reached", "message": RAW_PROMPT}},
    ]
    result = execute(_Run(stdout="\n".join(json.dumps(e) for e in events)))
    metrics = result.metadata["worker_metrics"]
    assert metrics["rate_limit_events"] == 1
    assert metrics["structured_errors"] == 1
    assert metrics["error_kind"] == "usage_limit_reached"
    _assert_no_secrets(json.dumps(metrics), where="rate-limit metrics")


def test_worker_prose_limitations_are_not_carried_as_labels(execute):
    """Supplied files can be quoted back; only closed limitation labels survive."""
    envelope = ("Work finished.\n\n```json\n" + json.dumps({
        "task_status": "completed", "reason": None,
        "limitations": ["network fetch was sandboxed; used the cached fixture"],
        "evidence": {"summary": "ok"}}) + "\n```")
    events = [
        {"type": "thread.started", "thread_id": "thread-abc-123"},
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": envelope}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    result = execute(_Run(stdout="\n".join(json.dumps(e) for e in events)))
    assert result.task_status is WorkerTaskStatus.COMPLETED
    assert "network fetch was sandboxed; used the cached fixture" not in result.limitations
    assert "worker_has_no_command_tools" in result.limitations
