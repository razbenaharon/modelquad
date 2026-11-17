# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The MCP server Claude Code talks to: four tools around one review flow.

* ``modelquad_review_package``: what a review would send. Starts nothing.
* ``modelquad_delegate``: the review itself. The only tool that starts Codex.
* ``modelquad_review_verdict``: what became of Codex's findings. Local record.
* ``modelquad_status``: whether ModelQuad is on, the mode and this folder's rules.

The flow, its gates and the package live in ``modelquad.review``; this module
only turns tool calls into requests and replies into text and data.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .. import grant as _grant
from .. import review
from ..adapters.server_instructions import server_instructions as _server_instructions
from ..branding import MCP_SERVER_NAME
from ..execution.base import DEFAULT_TIMEOUT_SECONDS
from . import worker_guard

#: Whether this Claude Code session started in a folder where the user turned
#: the standing grant on (grant.py). Read once: a tool's metadata is fixed when
#: the server starts. Each call still re-checks the grant for its own folder.
_STANDING_GRANT = _grant.active_at_start()
#: Without a standing grant, Claude Code shows its approval prompt for every
#: call (AGENTS.md invariant 7); with one, its ordinary permissions apply.
_DELEGATE_META = None if _STANDING_GRANT else {"anthropic/requiresUserInteraction": True}
#: The folder this session started in. Calls without the per-call prompt stay inside it.
_SESSION_PROJECT = _grant.session_project()

#: Tools that only read local state. Claude Code may run these in parallel and
#: retry them. Never give this to the tool that starts Codex: a retry would
#: send the code again and spend the user's Codex usage.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
#: The one tool that starts Codex: it sends the listed files to OpenAI.
STARTS_CODEX = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                               open_world_hint=True)
#: Writes only ModelQuad's own local ledger; starts nothing and sends nothing.
LOCAL_RECORD = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                               open_world_hint=False)

PROGRESS_INTERVAL_SECONDS = 15.0
_PROGRESS_TIMEOUT_SECONDS = 0.1

FilesArgument = Annotated[list[str] | None, Field(max_length=review.MAX_NAMED_FILES)]


def _build_identity() -> str | None:
    try:
        from ..buildinfo import current_build_info

        return current_build_info().identity
    except Exception:  # noqa: BLE001 -- the version line is a courtesy
        return None


server = MCPServer(MCP_SERVER_NAME, version=_build_identity(),
                   instructions=_server_instructions(standing=_STANDING_GRANT))


def _executor():
    """The one backend. Tests replace this function."""
    from ..execution.codex import CodexExecutor

    return CodexExecutor()


def _session() -> review.Session:
    return review.Session(standing=_STANDING_GRANT, project=_SESSION_PROJECT,
                          worker=worker_guard.is_worker())


def _result(payload: dict, lines: list[str]) -> CallToolResult:
    """A reply: the plain lines a person reads first (the outcome on the first
    line), then the same facts as data for Claude. Codes stay in the data."""
    return CallToolResult(
        content=[TextContent(type="text", text="\n".join(lines)),
                 TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload)


#: What a review reply shows Claude. Process detail stays out of the reply.
_VISIBLE = ("task_type", "provider", "task_status", "task_completed", "output", "refused", "started",
            "delegated", "executed", "reason", "message", "decision_id", "package", "package_round",
            "skipped_files", "completion_request", "findings", "findings_note", "ledger_note", "usage",
            "limitations", "worker_note", "output_truncated", "output_chars", "status", "notice",
            "next", "note", "codex_started")


def _review_lines(payload: dict) -> list[str]:
    lines = [review.headline(payload)]
    if payload.get("status") == review.NOTICE_FIRST:
        return lines + list(payload["notice"]) + [
            "To go ahead, ask again. Claude Code shows its approval prompt before the call."]
    if payload.get("refused"):
        return lines + [review.refusal_line(payload)]
    if payload.get("message") and payload["message"] != lines[0]:
        lines.append(payload["message"])
    package = payload.get("package")
    if isinstance(package, dict):
        lines.append(f"Sent: the task text and {package['file_count']} file(s), "
                     f"{package['total_bytes']} bytes (package {package['package_id']}).")
        if package["skipped"]:
            lines.append("Left out: " + ", ".join(
                f"{item['path']} ({item['reason']})" for item in package["skipped"]))
    usage = payload.get("usage") or {}
    if usage.get("tokens_source"):
        lines.append(f"Codex reported {usage.get('input_tokens')} input and "
                     f"{usage.get('output_tokens')} output tokens, in {usage.get('seconds')} s.")
    request = payload.get("completion_request")
    if request:
        lines.append("Codex said it needed files that were not sent: " + ", ".join(request["files"])
                     + ". " + request["note"])
    note = (payload.get("worker_note") or {}).get("warning")
    if note:
        lines.append(note)
    if payload.get("ledger_note"):
        lines.append(payload["ledger_note"])
    return lines


class _Progress:
    """Best-effort progress lines with no task material. Never holds up a review."""

    def __init__(self, ctx: Context | None) -> None:
        self.ctx = ctx
        self.enabled = ctx is not None
        self.value = 0

    async def report(self, message: str) -> None:
        if not self.enabled:
            return
        self.value += 1
        try:
            with anyio.move_on_after(_PROGRESS_TIMEOUT_SECONDS) as scope:
                await self.ctx.report_progress(self.value, total=None, message=message)
            if scope.cancel_called:
                self.enabled = False
        except Exception:  # noqa: BLE001 -- progress is advisory
            self.enabled = False


async def _heartbeat(progress: _Progress, state: dict, started_at: float) -> None:
    announced = False
    while progress.enabled:
        await anyio.sleep(1.0 if not announced else PROGRESS_INTERVAL_SECONDS)
        if state.get("pid") is None:
            continue
        if not announced:
            announced = True
            await progress.report("Codex started")
            continue
        await progress.report(f"Codex is working ({max(0, int(time.monotonic() - started_at))}s)")


async def _run_review(request: review.ReviewRequest, ctx: Context | None) -> dict:
    """The review off the event loop; a cancelled call stops Codex."""
    stop = threading.Event()
    state: dict[str, Any] = {"pid": None}
    session = await anyio.to_thread.run_sync(_session)

    def spawned(pid: int) -> None:
        state["pid"] = pid
        worker_guard.register(pid)

    def call() -> dict:
        try:
            return review.run(request, _executor(), session, on_spawn=spawned,
                              stop_requested=stop.is_set)
        finally:
            worker_guard.unregister(state["pid"])

    progress = _Progress(ctx)
    started_at = time.monotonic()
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(_heartbeat, progress, state, started_at)
            try:
                payload = await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
            finally:
                group.cancel_scope.cancel()
    except BaseException:
        # The caller went away (or the server is closing): end the worker's tree.
        stop.set()
        raise
    await progress.report(review.headline(payload).rstrip("."))
    return payload


@server.tool(title="Show what a Codex review would send", annotations=READ_ONLY)
async def modelquad_review_package(
    task_type: str,
    prompt: str,
    working_directory: str,
    files: FilesArgument = None,
) -> CallToolResult:
    """Show the context package a review would send, without starting Codex: the files
    (paths relative to working_directory, at most 32), their sizes as sent, what is left
    out and why, the limits, and a package_id. Show it to the user when they want to see
    or change what Codex gets; add or remove paths in `files` and call again. Pass the
    package_id to modelquad_delegate to send exactly this package."""
    request = review.ReviewRequest(task_type=task_type, prompt=prompt,
                                   working_directory=working_directory, files=tuple(files or ()))

    def build() -> dict:
        policies: list = []
        refused = review.gate(request, _session(), starts_codex=False, policy_out=policies)
        if refused is not None:
            return refused
        return review.package(request, policies[0].caps if policies else None)

    payload = await anyio.to_thread.run_sync(build)
    return _result(payload, review.package_lines(payload))


@server.tool(title="Ask Codex for a read-only second opinion", annotations=STARTS_CODEX,
             meta=_DELEGATE_META)
async def modelquad_delegate(
    task_type: str,
    prompt: str,
    working_directory: str,
    timeout: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=840, allow_inf_nan=False),
    model: str | None = None,
    reasoning_effort: str | None = None,
    ctx: Context | None = None,
    files: FilesArgument = None,
    trigger: str = "claude",
    package_id: str | None = None,
    follows: str | None = None,
) -> CallToolResult:
    """Ask read-only Codex for a review, exploration, or architecture opinion.
    Codex sees only the task text and the files listed in `files` (paths relative to
    working_directory, at most 32); a path named only in the task text is not sent.
    `trigger` says why: review_gate, pre_change, user or claude. `package_id` (from
    modelquad_review_package) sends only that exact package. `follows` is the
    decision_id of an earlier reply whose completion_request this call answers, at
    most 2 rounds. Without the user's standing grant Claude Code asks before every call."""
    request = review.ReviewRequest(
        task_type=task_type, prompt=prompt, working_directory=working_directory,
        files=tuple(files or ()), timeout=timeout, model=model, reasoning_effort=reasoning_effort,
        trigger=trigger, package_id=package_id, follows=follows)
    payload = await _run_review(request, ctx)
    visible = {key: payload[key] for key in _VISIBLE if key in payload}
    return _result(visible, _review_lines(payload))


class FindingVerdict(BaseModel):
    """What Claude decided about one Codex finding, and on what evidence."""

    finding: str = Field(description="The finding's reference, such as F1.")
    verdict: str = Field(description="confirmed, refuted or undecidable.")
    evidence: str = Field(description="test (a test failed before the fix), reproduced, ci, "
                                      "code_reading (your own reading only) or none.")
    action: str = Field(description="to_fix (accepted, before you fix it), fixed (after the "
                                    "fix), no_change or deferred.")
    fix_verified: bool | None = Field(default=None, description="After a fix: did a test or "
                                      "reproduction confirm it works?")
    note: str | None = Field(default=None, max_length=200, description="A short reason, no code.")


@server.tool(title="Record what became of Codex's findings", annotations=LOCAL_RECORD)
def modelquad_review_verdict(
    decision_id: str,
    verdicts: Annotated[list[FindingVerdict], Field(min_length=1, max_length=50)],
) -> CallToolResult:
    """Record, for each finding of one Codex reply, whether its claim holds, on what
    evidence, and what you do about it: before a fix (action to_fix) and again after it
    (action fixed, fix_verified). Agreement from reading alone is evidence code_reading,
    which ModelQuad never counts as proof. Starts nothing."""
    from .. import review_ledger

    if worker_guard.is_worker():
        raise ToolError("a worker does not record review verdicts")
    try:
        kept = review_ledger.record_verdicts_detail(
            decision_id, [item.model_dump() for item in verdicts], source="claude")
    except review_ledger.VerdictRefused as exc:
        raise ToolError(str(exc)) from None
    payload = {"recorded": kept["recorded"], "decision_id": decision_id}
    if kept["ignored"]:
        # Never a silent drop: the user's verdict stands, and Claude is told which were not kept.
        payload["not_recorded"] = kept["ignored"]
        payload["note"] = kept["note"]
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          structured_content=payload)


def status_payload(project_directory: str | None) -> tuple[dict, list[str]]:
    """Whether ModelQuad is on, the mode, and what this folder allows. Reads only."""
    from .. import localpath, settings_view

    if worker_guard.is_worker():
        return review.refusal(review.WORKER), ["ModelQuad status is not available to a worker."]
    folder = project_directory
    if folder is not None and localpath.reason(folder) is not None:
        folder = None
    view = settings_view.view(Path(folder) if folder else None)
    return view, settings_view.lines_from(view)


@server.tool(title="ModelQuad status", annotations=READ_ONLY)
def modelquad_status(project_directory: str | None = None) -> CallToolResult:
    """Whether ModelQuad is on, whether Codex calls are automatic or approved one by one,
    what this folder's own rules allow, and a one-line history. Reads only; starts nothing."""
    payload, lines = status_payload(project_directory)
    return _result(payload, lines)


def main() -> None:
    from .. import netpolicy

    # ModelQuad's own process connects only to loopback (netpolicy.py).
    netpolicy.install_loopback_guard()
    server.run()


if __name__ == "__main__":
    main()
