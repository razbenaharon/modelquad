# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Structured worker evidence: normalize rich CLI output into safe research
fields, and capture objective runtime versions.

Two separable things live here.

**1. Runtime version capture (IMPLEMENTED, used today).** ``agent_version`` is
first-class research metadata because a harness changes behavior independently
of its model: the same model under codex-cli 0.147.0 and 0.150.0 is not the
same agent. ``runtime_version()`` probes the CLI's own ``--version``, costs no
provider quota, and is cached per process. An unprobeable CLI stays UNKNOWN.

**2. Structured-output normalization.** The Codex worker always runs
``codex exec --json``. Verified against codex-cli 0.147.0 on 2026-08-26:

- ``codex exec --json`` emits JSONL ``ThreadEvent``s: ``thread.started``
  (thread_id), ``turn.completed`` (usage: input/cached_input/cache_write_input/
  output/reasoning_output tokens), ``turn.failed`` (error), and
  ``item.completed`` items typed ``command_execution`` / ``file_change`` /
  ``mcp_tool_call`` / ``web_search`` / ``reasoning`` / ``agent_message``.
  **The stream carries no model field** — the actual model is only in the
  session rollout file, which also contains the raw prompt, so this module
  does not read rollouts.

**The redaction boundary is the point of this module.** Structured output is
rich precisely because it embeds ``command_execution.command`` (full command
lines), ``aggregated_output`` (complete tool stdout/stderr), and
``file_change.path`` (absolute workspace paths) — i.e. exactly the prompts,
secrets, and paths ModelQuad refuses to collect. Nothing here returns, stores, or
forwards any of that: only counts, categories, token totals, and closed-
vocabulary error kinds cross this boundary. Callers get ``WorkerMetrics`` and
never the parsed payload.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from . import executable_resolution

# Tool categories are a closed vocabulary: a new upstream item type is counted
# under "other" rather than widening the schema implicitly.
TOOL_CATEGORIES: tuple[str, ...] = (
    "shell",
    "file_edit",
    "file_read",
    "search",
    "mcp_tool",
    "other",
)

_CODEX_ITEM_CATEGORY: dict[str, str] = {
    "command_execution": "shell",
    "file_change": "file_edit",
    "mcp_tool_call": "mcp_tool",
    "web_search": "search",
}

# Item types that are model output, not tool use -- counted separately so
# "tool calls" stays a measure of actions taken in the workspace.
_CODEX_NON_TOOL_ITEMS = frozenset({"agent_message", "reasoning", "todo_list"})

_MAX_EVENT_LINES = 50_000

# A line is only evidence of a structured stream if it is a recognized
# ThreadEvent. Plain-text worker output legitimately contains JSON objects --
# the completion envelope is one -- and must not be mistaken for an event
# stream, which would silently strip the envelope and lose the result.
_CODEX_EVENT_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)


@dataclass(frozen=True)
class WorkerMetrics:
    """Normalized, privacy-safe execution behavior.

    Every field is a count, a total, or a closed-vocabulary token. Unknown
    stays None -- a metric the runtime did not report is never a zero.
    """

    source: str  # "codex_json" | "unavailable"
    tool_calls_total: int | None = None
    tool_calls_by_category: dict[str, int] = field(default_factory=dict)
    shell_command_count: int | None = None
    file_change_count: int | None = None
    failed_tool_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    turn_count: int | None = None
    error_kind: str | None = None
    session_id: str | None = None
    parse_status: str = "ok"  # "ok" | "partial" | "unparseable" | "empty"
    # "zero observed" and "not observable" are different research facts and
    # must never collapse. A runtime that emits a typed event for something
    # yields a real count (possibly 0); a runtime with no such event yields
    # None. Codex has typed turn failures and rate-limit error codes, so
    # those are counted; it has no distinct permission-denial event, so
    # permission_blocks stays None rather than a misleading 0.
    structured_errors: int | None = None
    rate_limit_events: int | None = None
    permission_blocks: int | None = None
    # The runtime's OWN duration claim, kept beside (never replacing)
    # ModelQuad's measured wall clock -- two independent measurements of the
    # same run are more useful than one, and their provenance differs.
    provider_reported_duration_seconds: float | None = None
    provider_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "tool_calls_total": self.tool_calls_total,
            "tool_calls_by_category": dict(self.tool_calls_by_category),
            "shell_command_count": self.shell_command_count,
            "file_change_count": self.file_change_count,
            "failed_tool_calls": self.failed_tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "turn_count": self.turn_count,
            "error_kind": self.error_kind,
            "session_id": self.session_id,
            "parse_status": self.parse_status,
            "structured_errors": self.structured_errors,
            "rate_limit_events": self.rate_limit_events,
            "permission_blocks": self.permission_blocks,
            "provider_reported_duration_seconds": self.provider_reported_duration_seconds,
            "provider_status": self.provider_status,
        }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Typed codex error codes that mean "the provider throttled us", as opposed
# to any other turn failure. Closed vocabulary; an unrecognized code counts
# as a structured error but not as a rate-limit event.
_CODEX_RATE_LIMIT_ERRORS = frozenset(
    {"usage_limit_reached", "quota_exceeded", "rate_limited", "session_budget_exceeded"}
)


@dataclass(frozen=True)
class StructuredExecution:
    """The EPHEMERAL parse of one structured worker run.

    ``assistant_text`` is the model's own message text, reconstructed from
    ``agent_message`` items only. It exists so the executor can find the
    completion envelope and hand the caller a readable result -- exactly what
    plain-text mode already returns. It is **never persisted**: ModelQuad
    stores no worker output column, and nothing here is written to metadata.
    ``metrics`` is the only part that survives into persistence.

    ``usable`` is False when the text was not a parseable event stream at
    all, which is the signal to fall back to plain-text handling rather than
    to invent a result.
    """

    assistant_text: str
    metrics: WorkerMetrics
    usable: bool


def parse_codex_stream(text: str | None) -> StructuredExecution:
    """Parse a ``codex exec --json`` JSONL stream once, close to ingestion.

    This is the only function that ever sees raw provider events. It returns
    model message text (for envelope extraction) and safe counters; every
    other field on every event -- ``command_execution.command``,
    ``aggregated_output``, ``file_change.path``, ``mcp_tool_call.arguments``
    -- is read for its shape and immediately dropped. Nothing retains the
    parsed payload.
    """

    metrics = normalize_codex_json_events(text)
    usable = metrics.parse_status in {"ok", "partial"}
    if not usable or not text:
        return StructuredExecution(assistant_text="", metrics=metrics, usable=False)

    chunks: list[str] = []
    for index, line in enumerate(text.splitlines()):
        if index >= _MAX_EVENT_LINES:
            break
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if (item.get("item_type") or item.get("type")) != "agent_message":
            continue
        # ONLY the assistant's own message. Tool output, command text, and
        # reasoning traces are deliberately not collected here.
        for key in ("text", "content", "message"):
            value = item.get(key)
            if isinstance(value, str) and value:
                chunks.append(value)
                break

    return StructuredExecution(
        assistant_text="\n".join(chunks), metrics=metrics, usable=True
    )


def normalize_codex_json_events(text: str | None) -> WorkerMetrics:
    """Normalize a ``codex exec --json`` JSONL stream into safe counters.

    Malformed lines are skipped (a worker's output is untrusted input, and a
    parse failure must never break a delegation); if nothing parses, the
    result says so via ``parse_status`` rather than reporting zeros as fact.
    """

    if not text or not text.strip():
        return WorkerMetrics(source="codex_json", parse_status="empty")

    by_category: dict[str, int] = {}
    tool_calls = 0
    failed_calls = 0
    file_changes = 0
    shell_calls = 0
    turn_count = 0
    session_id: str | None = None
    error_kind: str | None = None
    structured_errors = 0
    rate_limit_events = 0
    usage: dict[str, Any] = {}
    parsed_any = False
    saw_bad_line = False

    for index, line in enumerate(text.splitlines()):
        if index >= _MAX_EVENT_LINES:
            saw_bad_line = True
            break
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            saw_bad_line = True
            continue
        if not isinstance(event, dict):
            saw_bad_line = True
            continue
        kind = event.get("type")
        if kind not in _CODEX_EVENT_TYPES:
            # A JSON object that is not a ThreadEvent (e.g. the completion
            # envelope in plain-text output). Not an event, not an error.
            continue
        parsed_any = True

        if kind == "thread.started":
            candidate = event.get("thread_id")
            if isinstance(candidate, str):
                session_id = candidate
        elif kind == "turn.completed":
            turn_count += 1
            reported = event.get("usage")
            if isinstance(reported, dict):
                usage = reported
        elif kind == "turn.failed":
            structured_errors += 1
            error = event.get("error")
            if isinstance(error, dict):
                # The typed error code only -- never the message, which can
                # quote commands, paths, or model output.
                code = error.get("type") or error.get("code")
                if isinstance(code, str):
                    error_kind = code
                    if code in _CODEX_RATE_LIMIT_ERRORS:
                        rate_limit_events += 1
            elif isinstance(error, str):
                error_kind = "unspecified"
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("item_type") or item.get("type")
            if not isinstance(item_type, str) or item_type in _CODEX_NON_TOOL_ITEMS:
                continue
            category = _CODEX_ITEM_CATEGORY.get(item_type, "other")
            by_category[category] = by_category.get(category, 0) + 1
            tool_calls += 1
            if category == "shell":
                shell_calls += 1
                # `status` and `exit_code` are safe scalars; `command` and
                # `aggregated_output` on this same item are deliberately
                # never read.
                if item.get("status") == "failed" or (
                    isinstance(item.get("exit_code"), int) and item["exit_code"] != 0
                ):
                    failed_calls += 1
            elif category == "file_edit":
                file_changes += 1

    if not parsed_any:
        return WorkerMetrics(source="codex_json", parse_status="unparseable")

    return WorkerMetrics(
        source="codex_json",
        tool_calls_total=tool_calls,
        tool_calls_by_category=by_category,
        shell_command_count=shell_calls,
        file_change_count=file_changes,
        failed_tool_calls=failed_calls,
        input_tokens=_int_or_none(usage.get("input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
        cached_input_tokens=_int_or_none(usage.get("cached_input_tokens")),
        reasoning_tokens=_int_or_none(usage.get("reasoning_output_tokens")),
        total_tokens=_int_or_none(usage.get("total_tokens")),
        turn_count=turn_count or None,
        error_kind=error_kind,
        session_id=session_id,
        parse_status="partial" if saw_bad_line else "ok",
        structured_errors=structured_errors,
        rate_limit_events=rate_limit_events,
        # Codex emits no distinct permission-denial event, so this is NOT
        # OBSERVABLE for this runtime -- None, never a misleading zero.
        permission_blocks=None,
    )


# --------------------------------------------------------------------------
# Runtime version capture
# --------------------------------------------------------------------------

_VERSION_CACHE: dict[str | tuple[str, str | None], str | None] = {}
_VERSION_TIMEOUT_SECONDS = 10.0

# The CLI whose --version identifies each agent runtime. A runtime absent
# here has no probe and stays UNKNOWN rather than being guessed.
#: Only the worker is probed. Claude Code is the host, never a worker, and
#: ModelQuad never launches the ``claude`` binary, not even for ``--version``:
#: the host's version comes from the MCP session (``hostobs``).
_RUNTIME_EXECUTABLES: dict[str, str] = {
    "codex": "codex",
}


def _probe_version(executable: str) -> str | None:
    from .codexcli import which_codex

    # Codex resolves to the CLI that actually runs (codexcli.py).
    # runtime_version, the only production caller, supplies Codex's already
    # admitted selection path so the cache and probe agree. Bare runtime
    # names are resolved here; patched selection hooks retain the test seam.
    path = (
        which_codex(executable, native=True)
        if executable in _RUNTIME_EXECUTABLES.values() else executable
    )
    if path is None:
        return None
    try:
        from .procutil import no_window_kwargs

        completed = subprocess.run(  # noqa: S603 -- fixed argv, shell=False
            [path, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS,
            shell=False,
            # Fixed --version only: Codex/Claude shims may use bare node.
            # Prompt launches must establish their own batch handling.
            env=executable_resolution.safe_child_env(os.environ),
            **no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    if not output:
        return None
    # A version banner is one short line; anything else is not a version.
    first = output.splitlines()[0].strip()
    return first[:120] or None


def runtime_version(runtime_or_provider: str, *, refresh: bool = False) -> str | None:
    """The agent runtime's own reported version, or None when unknowable.

    Costs no provider quota (a ``--version`` probe never contacts a backend)
    and is cached for the process, so a delegation pays at most one probe per
    runtime (per resolved executable for Codex). Never raises.
    """

    key = str(runtime_or_provider)
    executable = _RUNTIME_EXECUTABLES.get(key)
    if key == "codex":
        from .codexcli import which_codex

        executable = which_codex(native=True)
        key = (key, executable)
    if not refresh and key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    version = _probe_version(executable) if executable else None
    _VERSION_CACHE[key] = version
    return version


def clear_version_cache() -> None:
    """Drop cached probes (tests, and after a provider upgrade)."""

    _VERSION_CACHE.clear()

