# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""MCP tools exposing routing, provider status, and task delegation."""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, Field
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .. import netpolicy
from ..branding import MCP_SERVER_NAME
from ..buildinfo import current_build_info
from ..catalog import ModelCatalog, cached_catalog
from ..config import load_config
from ..jobs.handle import JobSpec, PreparedDelegation, start_job, job_status
from ..doctor import run_doctor
from ..execution import DEFAULT_TIMEOUT_SECONDS, ExecutionManager
from ..execution.bound_launch import (
    _bind_launch,
    _launch_call,
    run_bound_delegation,
)
from ..execution.base import CallerWaitHandle, WORKER_ENV_VAR
from ..assistance import handoff_suggestion, statusline_text
from ..models import ExpectedConsumption
from ..hostobs import (
    HostDecisionSnapshot,
    HostProtocolIdentity,
    HostRequestMetadata,
    capture_host_decision_snapshot,
    with_decision_context,
)
from ..provenance import evidence_snapshot_json, new_decision_id
from ..providers import build_providers
from ..providers.base import DEFAULT_USAGE_DIRECTORY, read_status
from ..routing_policy import LaunchRefused, check_delegation_policy
from ..service import db as service_db
from ..service import health as service_health
from ..service import history as service_history
from .livelifecycle import derive_recovery_outlook, facts_from_attempt
from .presentation import (
    build_activity_feed,
    build_delegation_presentation,
    render_presentation_text,
)
from .callerinfo import (
    host_from_context,
    host_protocol_from_context,
    host_request_meta_from_context,
    resolve_invocation_mode,
)
from .worker_guard import WORKER_REFUSAL, is_worker

logger = logging.getLogger(__name__)

# Call-scoped evidence reaches the existing tool guards, including their
# refusal recorders, without changing the process environment across calls.
_worker_call: ContextVar[bool] = ContextVar("mcp_worker_call", default=False)
WORKER_REFUSAL_TOOLS = frozenset({"modelquad_delegate", "modelquad_handoff"})


def _worker_refused() -> bool:
    return _worker_call.get() or os.environ.get(WORKER_ENV_VAR) == "1"


def _worker_refusal_prefix() -> str:
    """Name the refusing layer without claiming an absent environment marker."""
    if os.environ.get(WORKER_ENV_VAR) == "1":
        return f"{WORKER_ENV_VAR}=1:"
    return "worker ancestry match:"


# `serverInfo.version` in the initialize handshake: `<version>+<sha7>`, so a
# host's MCP log names the exact build it spoke to.
class WorkerGuardMCPServer(MCPServer):
    """One dispatch boundary for every registered tool."""

    async def call_tool(self, name, arguments, context=None):
        # Both database and native observation run off the async event loop.
        worker = await anyio.to_thread.run_sync(is_worker, service_db.DEFAULT_DB_PATH)
        if worker and name not in WORKER_REFUSAL_TOOLS:
            payload = {
                "refused": True, "delegated": False, "executed": False,
                "recorded": False, "reason": WORKER_REFUSAL,
            }
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(payload))],
                structuredContent=payload, isError=True,
            )
        # Preserve each existing refusal's reason, payload and evidence writer.
        # AnyIO propagates this context into synchronous tool worker threads.
        token = _worker_call.set(worker)
        try:
            return await super().call_tool(name, arguments, context)
        finally:
            _worker_call.reset(token)


# The model guidance lives here, not in the user's global CLAUDE.md: Claude
# Code reads a server's instructions at session start (adapters/server_instructions.py).
from ..adapters.server_instructions import server_instructions as _server_instructions

#: Tools that only read local state. Claude Code may run these in parallel and
#: retry them after a dropped connection. Never give this to a tool that starts
#: Codex: a retry would send the code again and spend the user's Codex usage.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
#: The two tools that start Codex: they send the listed files to OpenAI.
STARTS_CODEX = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                               open_world_hint=True)

from mcp.server.apps import Apps
from . import handoff_widget

# The handoff approval page, for a client that renders MCP Apps (see handoff_widget).
_apps = Apps()
_apps.add_html_resource(handoff_widget.URI, handoff_widget.HTML, name="modelquad-handoff",
                        title="Ask Codex for a second opinion")

server = WorkerGuardMCPServer(MCP_SERVER_NAME, version=current_build_info().identity,
                              instructions=_server_instructions(), extensions=[_apps])

# Where every tool reads its configuration and its quota snapshots. The model
# names neither: a tool that took a settings file let the Host's
# model deep-merge any YAML over the packaged defaults -- re-enabling a
# provider, rewriting the weights -- and a tool that took a usage directory let
# it route on quota it had written itself. None means the packaged
# configuration and the default usage directory. Tests inject by
# monkeypatching these module attributes; no tool argument reaches them.
CONFIG_PATH: str | None = None
USAGE_DIRECTORY: str | None = None

@contextmanager
def anticipated_failure():
    """Translate expected validation failures without exposing their details.

    The pinned SDK sends ``ToolError`` text to the model. Domain exceptions can
    contain paths or input, so the tool boundary uses a stable public message.
    """
    try:
        yield
    except ValueError as exc:
        raise ToolError("The request could not be accepted.") from exc


def _launch_policy(db_path, invocation_mode=None, user_approved=False, caller_host=None):
    try:
        state = service_history.get_launch_state(db_path)
    except service_history.LaunchStateUnavailable:
        state = {"routing_mode": None, "routing_paused": None}
    return check_delegation_policy(
        routing_mode=state["routing_mode"], routing_paused=state["routing_paused"],
        routing_paused_since=state.get('routing_paused_since'),
        invocation_mode=invocation_mode, user_approved=user_approved,
        disconnected_hosts=state.get("disconnected_hosts"), caller_host=caller_host,
    )


def _prelaunch_refusal(exc):
    call = _launch_call.get()
    return isinstance(exc, LaunchRefused) and not (call and call["worker_entered"])


def _launch_refusal(task_type, reason, *, decision_id=None, provider=None,
                    host=None, invocation_mode=None, **extra):
    """A refusal identity exists only when its lifecycle evidence was written."""
    if reason.startswith("repo_"):
        global_policy = _launch_policy(service_db.DEFAULT_DB_PATH)
        extra.update(routing_mode=global_policy.routing_mode,
                     routing_paused=global_policy.routing_paused)
    call = _launch_call.get()
    if call is not None and call.get("attempt_id") is not None:
        if call.get("worker_entered"):
            raise RuntimeError("launch refusal after execution entry")
        recorded = service_history.resolve_delegation_attempt(
            service_db.DEFAULT_DB_PATH, call["attempt_id"], status="refused",
            failure_kind="policy_refused", detail=reason, only_if_open=True,
        )
        return {
            "delegated": False, "executed": False, "refused": True,
            **extra, "reason": reason, "recorded": recorded is True,
            "decision_id": call["decision_id"] if recorded is True else None,
        }
    candidate_id = decision_id or new_decision_id()
    try:
        row_id = service_history.record_delegation_attempt(
            service_db.DEFAULT_DB_PATH, task_type=task_type, decision_id=candidate_id,
            requested_provider=provider, status="refused", failure_kind="policy_refused",
            detail=reason, source="mcp", host=host, invocation_mode=invocation_mode,
            **(call["repo_context"].attempt_fields(capability=call.get('capability', 'delegate'))
               if call and call.get("repo_context") else {}),
        )
    except Exception:
        row_id = None
    return {
        "delegated": False, "executed": False, "refused": True,
        **extra, "reason": reason, "recorded": row_id is not None,
        "decision_id": candidate_id if row_id is not None else None,
    }


def _job_for_attempt_or_none(db_path, attempt_id):
    try:
        return service_history.job_for_attempt(db_path, attempt_id)
    except (sqlite3.Error, OSError, service_history.LaunchStateUnavailable):
        return None


def _launch_failures(function):
    """Keep coarse admission/storage refusals out of infrastructure fallback."""
    import inspect
    signature = inspect.signature(function)

    @functools.wraps(function)
    def guarded(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        call = {"worker_entered": False}
        token = _launch_call.set(call)
        try:
            return function(*args, **kwargs)
        except (LaunchRefused, service_history.DuplicateActiveAttempt,
                service_history.LaunchStateUnavailable) as exc:
            # Entering an executor is already an uncertain side effect, even
            # when it throws before returning a result or reporting a pid.
            if call["worker_entered"] or call.get("job_owned"):
                raise
            if call.get("attempt_id") is not None and _job_for_attempt_or_none(
                service_db.DEFAULT_DB_PATH, call["attempt_id"]
            ) is not None:
                raise  # reservation, not the Host wait, owns this attempt
            reason = ("duplicate_in_flight" if isinstance(exc, service_history.DuplicateActiveAttempt)
                      else str(exc))
            if call.get('private_notes'):
                from ..project_notes import closed_reason
                reason = closed_reason(reason)
            return _launch_refusal(
                bound.arguments.get("task_type", "unknown"), reason,
                decision_id=bound.arguments.get("decision_id"),
                provider=bound.arguments.get("provider"),
                invocation_mode=bound.arguments.get("invocation_mode"),
                **(call["repo_context"].facts(capability=call.get('capability', 'delegate'))
                   if call.get("repo_context") else {}),
            )
        finally:
            _launch_call.reset(token)
    return guarded


def _reserve_launch(db_path, *, working_directory, request_key, **kwargs):
    try:
        attempt_id = service_history.record_delegation_attempt(
            db_path, working_directory=working_directory, request_key=request_key, **kwargs,
        )
    except service_history.DuplicateActiveAttempt:
        raise
    except Exception:
        raise LaunchRefused("attempt_unrecorded") from None
    if attempt_id is None:
        raise LaunchRefused("attempt_unrecorded")
    call = _launch_call.get()
    if call is not None:
        call.update(attempt_id=attempt_id, decision_id=kwargs.get("decision_id"))
    return attempt_id


def _build_execution_manager(config: object) -> ExecutionManager:
    """Create the default manager; isolated for dependency injection in tests.

    The health database is bound here because this is a path that really
    spends a provider's quota: a worker that comes back refused must leave a
    row the next routing decision reads. The signature is
    unchanged on purpose -- every caller and every test double keeps working,
    and the database is the one the rest of this module already resolves.
    """
    return ExecutionManager(config, health_db_path=service_db.DEFAULT_DB_PATH)


DELEGATE_PROGRESS_INTERVAL_SECONDS = 15.0
_PROGRESS_REPORT_TIMEOUT_SECONDS = 0.1


class _DelegateProgress:
    """Best-effort, request-scoped progress delivery with no task material."""

    def __init__(self, ctx: Context | None) -> None:
        self.ctx = ctx
        self.enabled = ctx is not None
        self.value = 0
        self.worker_started = False

    async def report(self, message: str) -> None:
        if not self.enabled:
            return
        self.value += 1
        try:
            # A Host that cannot accept progress is not allowed to hold up a
            # delegation. Stop trying after the first failed/timed-out send.
            with anyio.move_on_after(_PROGRESS_REPORT_TIMEOUT_SECONDS) as scope:
                await self.ctx.report_progress(self.value, total=None, message=message)
            if scope.cancel_called:
                self.enabled = False
        except Exception:  # noqa: BLE001 - progress is strictly advisory
            self.enabled = False

    async def report_started(self) -> None:
        await self.report("Codex started")
        self.worker_started = self.enabled


async def _delegate_heartbeat(progress: _DelegateProgress, started_at: float) -> None:
    while progress.enabled:
        await anyio.sleep(DELEGATE_PROGRESS_INTERVAL_SECONDS)
        if not progress.worker_started:
            continue
        elapsed = max(0, int(time.monotonic() - started_at))
        await progress.report(f"Codex is working ({elapsed}s)")


async def _delegate_observing_the_caller(
    call: Any, handle: CallerWaitHandle, progress: _DelegateProgress | None = None,
) -> Any:
    """Run one blocking delegation on a worker thread, watching for the caller to leave.

    This is the whole of the caller-abandonment mechanism, and it exists because of a
    structural fact about the MCP runtime rather than a preference. A *sync*
    tool body is dispatched with ``anyio.to_thread.run_sync(...)`` at its
    ``abandon_on_cancel=False`` default (``mcp/server/mcpserver/utilities/
    func_metadata.py``), so the request's cancellation is deferred until the
    body returns: a synchronous delegation tool cannot see its caller give
    up, no matter what it inspects. The signal itself is real and does
    arrive -- a client read timeout sends ``notifications/cancelled`` and the
    server cancels that request's scope (``peer_cancel_mode="interrupt"``,
    ``mcp/shared/jsonrpc_dispatcher.py``) -- so an ``async`` body that
    dispatches with ``abandon_on_cancel=True`` observes it directly.

    Three properties are deliberate:

    - **The thread is abandoned, not stopped.** ``abandon_on_cancel=True``
      returns control to this coroutine; the delegation keeps running to its
      own end on that thread and resolves its own row. Caller abandonment
      does not imply worker termination (the worker's disposition is
      recorded separately).
    - **The write is shielded.** It happens inside an already-cancelled
      scope, and the shield guarantees that no checkpoint introduced later
      inside the write -- or the ``raise`` after it -- can be pre-empted
      there, recording nothing, which is the bug this part fixes. Today's
      write is synchronous and has no checkpoint of its own, so the shield
      is as much a statement of intent as a mechanism.
    - **The cancellation is re-raised.** The dispatcher settles a cancelled
      request itself; swallowing it here would return a result to a caller
      that is no longer there, and would break structured concurrency. That
      holds even if the recording below goes wrong: nothing inside the
      shield may replace a cancellation with an error of its own, because a
      caller that has already gone cannot be told about it and the runtime
      would see a request that failed rather than one that was cancelled.
    """

    async with anyio.create_task_group() as task_group:
        if progress is not None and progress.enabled:
            task_group.start_soon(_delegate_heartbeat, progress, time.monotonic())
        try:
            return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                # Half of a two-sided hand-off. The reason is stamped on the
                # handle FIRST, so that a cancellation arriving before the row
                # exists is still visible to `_delegate_payload`, which writes it
                # as soon as it publishes the attempt id. Both sides may then
                # attempt the write; it is write-once, so whichever side holds
                # both facts records the departure and the other is a no-op.
                handle.abandoned_reason = "transport_cancelled"
                # One read of one field, so the two halves of the identity are
                # either both there or both absent: the publishing thread writes
                # them as a single tuple. Absent means the row does not exist
                # yet, and there is nothing here to mark -- `_delegate_payload`
                # writes the mark when it publishes, having just read the reason
                # stamped above.
                identity = handle.identity
                if identity is not None:
                    db_path, attempt_id = identity
                    try:
                        service_history.record_caller_abandonment(
                            db_path, attempt_id, reason=handle.abandoned_reason
                        )
                    except Exception:
                        logger.warning(
                            "could not record the caller of attempt %s leaving; "
                            "the cancellation is re-raised regardless",
                            attempt_id,
                            exc_info=True,
                        )
            raise
        finally:
            task_group.cancel_scope.cancel()


def _resolve_consumption_evidence(prompt, parsed_capsule, expected_consumption) -> ExpectedConsumption:
    """Resolve the same consumption evidence for single-unit advice and execution."""
    if expected_consumption is not None:
        return ExpectedConsumption(
            bucket=expected_consumption,
            source="host_supplied",
            basis="supplied by host",
        )
    elif parsed_capsule is not None and parsed_capsule.expected_consumption is not None:
        return ExpectedConsumption(
            bucket=parsed_capsule.expected_consumption,
            source="host_supplied",
            basis="supplied via task capsule",
        )
    else:
        from ..consumption import estimate_expected_consumption

        # Pass the ORIGINAL prompt, not effective_prompt: effective_prompt
        # already embeds the rendered capsule, and the estimator counts the
        # capsule separately -- using effective_prompt would count capsule
        # text twice and inflate the bucket.
        return estimate_expected_consumption(prompt, parsed_capsule)


def _developer_mode() -> bool:
    """Whether the developer/debug surfaces may be populated.

    Read through `devmode` so the flag has exactly one name in the product.
    """

    from ..devmode import is_dev_mode

    return is_dev_mode()


def _decision_result(
    payload: dict[str, Any],
    presentation: dict[str, Any],
) -> CallToolResult:
    """The MCP response shape for a decision: one channel per audience.

    Two audiences, two channels, and the whole point is that they are no
    longer the same one:

    - `content` -- what a HUMAN sees in the conversation. Two or three lines.
      Returning the decision object here is what made every automatic route
      look like a debugging API (the SDK JSON-dumps any returned `dict` into
      exactly this block), and it is the thing this function exists to stop.
    - `structured_content` -- what the MODEL and any programmatic consumer
      read. The canonical decision object, complete and untrimmed, plus the
      derived `presentation` view.
    """

    summary = render_presentation_text(presentation)
    suggestion = payload.get("handoff_suggestion")
    if isinstance(suggestion, dict) and suggestion.get("message"):
        # The one line a human must see: the reply informs, it never acts.
        summary = f"{summary}\n{suggestion['message']}"
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content={
            **payload,
            "presentation": presentation,
            "summary_text": summary,
        },
    )


def _redact_local_paths(text: Any, paths: Any) -> Any:
    """``text`` with each of ``paths`` in it reduced to its file name.

    A provider's reason can quote its usage file ("Usage file not found:
    <path>", or an OSError that repr-escapes the path). An MCP client gets the
    file name, never the local path.
    """

    if not isinstance(text, str):
        return text
    spellings = {}
    for path in paths:
        if path:
            raw = str(path)
            for spelling in (raw, raw.replace("\\", "\\\\"), Path(raw).as_posix()):
                spellings[spelling] = Path(raw).name
    for spelling in sorted(spellings, key=len, reverse=True):
        text = text.replace(spelling, spellings[spelling])
    return text


def _usage_files(usage_directory: str | None = None, providers: Any = None) -> list[Path]:
    """The usage files a status reason may quote, for :func:`_redact_local_paths`."""

    directory = Path(usage_directory) if usage_directory else DEFAULT_USAGE_DIRECTORY
    files = [directory / "usage-claude.json", directory / "usage-codex.json"]
    files += [Path(item.usage_file) for item in providers or ()
              if getattr(item, "usage_file", None)]
    return files


def _provider_status_payload(
    usage_directory: str | None = None,
    config_path: str | None = None,
) -> dict:
    config = load_config(config_path)
    providers = build_providers(config, Path(usage_directory) if usage_directory else None)
    statuses = tuple(read_status(provider, refresh=True) for provider in providers)
    files = _usage_files(usage_directory, providers)
    # Do not disclose provider plans, raw collector metadata, or local source
    # paths to an MCP client.  They are implementation evidence, not status.
    def public(status):
        view = status.to_dict()
        view["reason"] = _redact_local_paths(view.get("reason"), files)
        return {key: view.get(key) for key in (
            "name", "available", "remaining_percentage", "observed_at", "freshness", "reason",
        )} | {"windows": [
            {"window": window.window, "remaining_percentage": window.remaining_percentage,
             "used_percentage": window.used_percentage,
             "reset_at": window.reset_at.isoformat() if window.reset_at else None,
             "freshness": window.freshness.value if window.freshness else "unknown"}
            for window in __import__("modelquad.quota_windows", fromlist=["reported_windows"]).reported_windows(status)
        ]}
    now = datetime.now(timezone.utc)
    return {
        "providers": [public(status) for status in statuses],
        "summary": statusline_text(statuses, config.freshness, now=now),
        # Quota is shown, never acted on: the note leaves the choice to the user.
        "handoff_suggestion": handoff_suggestion(
            statuses, config.freshness, now=now),
        # Account kind words and where they come from; never a plan name.
        "plans": _public_plans(),
    }


def _public_plans() -> dict:
    from .. import plans

    try:
        return plans.public_plans(_plan_views())
    except Exception:  # noqa: BLE001 -- a status reply must not fail over a preference file
        unknown = {"kind": plans.UNKNOWN, "source": plans.SOURCE_UNKNOWN}
        return {provider: dict(unknown) for provider in plans.PROVIDERS}


def _session_close_stops_worker() -> bool:
    """Only Windows holds the worker in a kill-on-close Job Object."""
    import os

    return os.name == "nt"


# AnyIO copies this request-local context into the preparation worker thread.
# Never retain a Host context globally or pass it into a detached job spec.
_runner_notice_context: ContextVar[Context | None] = ContextVar("runner_notice_context", default=None)
_runner_progress_context: ContextVar[_DelegateProgress | None] = ContextVar(
    "runner_progress_context", default=None,
)


@contextmanager
def _runner_notifications(ctx):
    token = _runner_notice_context.set(ctx)
    progress_token = _runner_progress_context.set(_DelegateProgress(ctx))
    try:
        yield _runner_progress_context.get()
    finally:
        _runner_progress_context.reset(progress_token)
        _runner_notice_context.reset(token)


def _runner_notice(message: str) -> None:
    """Deliver a phase from the worker thread on its owning MCP request."""
    progress = _runner_progress_context.get()
    if progress is None or not progress.enabled:
        return
    try:
        sender = progress.report_started if message == "Codex started" else progress.report
        anyio.from_thread.run(sender, *(() if message == "Codex started" else (message,)))
    except Exception:  # noqa: BLE001 - progress cannot affect a delegation
        progress.enabled = False


def _runner_packed(audit: dict[str, Any]) -> None:
    """Report the closed audit's counts, never its paths or contents."""
    sent = audit.get("sent") if isinstance(audit, dict) else None
    if not isinstance(sent, list) or not sent:
        _runner_notice("Sending only the task text to Codex")
        return
    sizes = [item.get("bytes") for item in sent if isinstance(item, dict)]
    if len(sizes) != len(sent) or any(type(size) is not int or size < 0 for size in sizes):
        return
    _runner_notice(f"Packed {len(sent)} file(s), {sum(sizes)} bytes for Codex")


def _warn_in_session_runner(reason):
    """Finish the MCP warning attempt before starting session-bound work."""
    ctx = _runner_notice_context.get()
    if ctx is None:
        return  # Internal callers without an MCP request have no client to notify.

    async def send():
        # A disconnected or unresponsive Host must not change launch authority.
        with anyio.move_on_after(2):
            note = _in_session_runner_note(reason, running=True)
            await ctx.report_progress(0, message=note)
            await ctx.warning(note)

    try:
        anyio.from_thread.run(send)
    except Exception as error:
        logger.debug("runner warning unavailable: %s", type(error).__name__)


def _in_session_runner_note(reason=None, *, running=False) -> str:
    prefix = "Running" if running else "Ran"
    if reason in {"service_absent", "service_unreachable"}:
        explanation = "the ModelQuad background service is not running, so "
    elif reason == "service_contained":
        explanation = ("the ModelQuad background service cannot keep work running "
                       "after this session closes, so ")
    elif reason is not None:
        explanation = "the ModelQuad background service did not accept this task, so "
    else:
        explanation = ""
    note = f"{prefix} inside this session: {explanation}this task depends on this session. "
    if _session_close_stops_worker():
        return (note + "When this session's ModelQuad server process ends, the task's "
                "worker is stopped; anything Windows started outside ModelQuad's "
                "control for it may keep running.")
    return note + "Ending this session's ModelQuad server process may leave the task's worker running."


@_launch_failures
def _delegate_payload(
    task_type: str,
    prompt: str,
    working_directory: str,
    provider: str | None = None,
    usage_directory: str | None = None,
    config_path: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    session_id: str | None = None,
    invocation_mode: str | None = None,
    host: str | None = None,
    host_protocol: HostProtocolIdentity | None = None,
    host_request_meta: HostRequestMetadata | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    user_approved: bool = False,
    capsule: dict | None = None,
    expected_consumption: str | None = None,
    agent_runtime: str | None = None,
    decision_id: str | None = None,
    *,
    caller_wait: CallerWaitHandle | None = None,
    supplied_files: list[str] | None = None,
    approval: str = "delegate",
) -> dict:
    db_path = service_db.DEFAULT_DB_PATH
    from .callerinfo import caller_host_id
    caller_host = caller_host_id(host_protocol, host_request_meta, host)
    policy = _launch_policy(db_path, invocation_mode, user_approved, caller_host)
    mode = policy.routing_mode
    paused = policy.routing_paused
    resolved_mode, mode_source = resolve_invocation_mode(invocation_mode)
    # Lineage: when the caller passes the decision_id a prior
    # modelquad_route returned, THAT identity is preserved end to end so the
    # outcome joins the candidate set and quota evidence that caused it. No
    # decision_id means direct delegation -- a fresh id is minted and the
    # record honestly says no prior route happened. An id that does not
    # exist (or was routed for a different task type) is refused rather than
    # absorbed, because fabricating lineage is worse than refusing.
    from ..lineage import resolve_delegation_lineage

    lineage = resolve_delegation_lineage(
        db_path,
        decision_id=decision_id,
        task_type=task_type,
        requested_provider=provider,
    )
    decision_id = lineage.decision_id
    if not lineage.valid:
        detail = (
            f"{lineage.refusal_reason}: no routing decision {decision_id!r} exists"
            if lineage.refusal_reason == "unknown_decision_id"
            else f"{lineage.refusal_reason}: {'; '.join(lineage.notes)}"
        )
        return _launch_refusal(
            task_type, detail, decision_id=decision_id, provider=provider,
            host=host, invocation_mode=resolved_mode, lineage=lineage.to_dict(),
        )
    # Recursion guard: same rule modelquad_plan/submit_plan enforce. Without
    # it a delegated worker whose own host has this MCP configured (or that
    # runs the CLI) can delegate again, unbounded -- proven live 2026-08-25.
    if _worker_refused():
        detail = (
            f"{_worker_refusal_prefix()} a delegated worker must never re-enter "
            "orchestration by delegating again"
        )
        return _launch_refusal(
            task_type, detail, decision_id=decision_id, provider=provider,
            host=host, invocation_mode=resolved_mode,
        )
    decision = policy
    if not decision.allowed:
        return _launch_refusal(
            task_type, decision_id=decision_id, provider=provider,
            host=host, invocation_mode=resolved_mode, **decision.to_dict(),
        )
    # The repository's .modelquad.toml, read fresh for this call. It can only
    # refuse or lower caps; only its closed reason code reaches history.
    repo_refusal = _repo_policy_refusal(task_type, working_directory, usage_directory)
    if repo_refusal is not None:
        return _launch_refusal(
            task_type, repo_refusal["reason"], decision_id=decision_id, provider=provider,
            host=host, invocation_mode=resolved_mode, message=repo_refusal["message"],
        )

    # From here a worker may actually be invoked. First close any stale rows
    # whose worker verifiably no longer exists, then refuse a RETRY of the
    # same logical request while its earlier worker is still alive -- a
    # caller that timed out and asked again must never start a second
    # mutation worker on the same workspace (observed live 2026-08-24).
    request_key = service_history.request_fingerprint(task_type, working_directory, prompt)
    # The abandoned-caller pass runs FIRST, and only because it is the more
    # specific one: both close a dead-pid row as orphaned, but this one can
    # say the caller had already left, which the general pass cannot.
    # It observes -- a live pid is left running and open.
    # Both passes are housekeeping ON the delegation path: useful, never
    # load-bearing. A pass that fails must leave the delegation to proceed
    # rather than becoming the reason no work can be delegated at all.
    try:
        service_history.reconcile_abandoned_attempts(db_path)
    except Exception:  # noqa: BLE001 -- housekeeping must not block a delegation
        logger.warning("abandoned-attempt reconciliation failed", exc_info=True)
    try:
        service_history.reconcile_stale_attempts(db_path)
    except Exception:  # noqa: BLE001 -- housekeeping must not block a delegation
        logger.warning("stale-attempt reconciliation failed", exc_info=True)

    def _refuse_duplicate(duplicate: dict[str, Any] | None) -> dict[str, Any]:
        """Record and explain a refusal to start a second worker for one request."""

        if duplicate is not None:
            age = duplicate.get("age_seconds")
            detail = (
                f"attempt {duplicate['id']} (decision {duplicate.get('decision_id')}) for this "
                "same task/workspace/prompt is still in flight"
                + (f" after {int(age)}s" if age is not None else "")
                + (
                    f" (worker pid {duplicate['worker_pid']} is alive)"
                    if duplicate.get("worker_pid")
                    else " (worker may still be starting)"
                )
            )
        else:
            # The database rejected this attempt but the winning row is no
            # longer visible as in-flight (it resolved in between). Say what
            # is actually known rather than naming a row we cannot show.
            detail = "a concurrent delegation of this same task/workspace/prompt claimed it first"
        refusal_row = service_history.record_delegation_attempt(
            db_path,
            task_type=task_type,
            decision_id=decision_id,
            requested_provider=provider,
            status="refused",
            failure_kind="duplicate_in_flight",
            detail=detail,
            source="mcp",
            host=host,
            invocation_mode=resolved_mode,
            request_key=request_key,
        )
        response: dict[str, Any] = {
            "delegated": False,
            "refused": True,
            "decision_id": decision_id if refusal_row is not None else None,
            "recorded": refusal_row is not None,
            "reason": (
                "A delegation for this exact request appears to still be running: "
                + detail
                + ". This refusal is about THAT worker still being in flight; it is not "
                "a verdict on the earlier call that stopped waiting. Ask "
                "modelquad_delegation_status(task_type=..., working_directory=..., "
                "prompt=...) with the same arguments for its state and, once it ends, "
                "its result -- or change the request if this is genuinely new work."
            ),
        }
        if duplicate is not None:
            response["duplicate_of_attempt"] = duplicate["id"]
        return response

    duplicate = service_history.find_in_flight_duplicate(db_path, request_key)
    if duplicate is not None:
        return _refuse_duplicate(duplicate)

    # The attempt is recorded BEFORE anything that can fail. If this process
    # dies mid-delegation the row stays unresolved -- which is exactly the
    # evidence that was missing. The insert is also the atomic half of
    # duplicate prevention: the check above and this write are two
    # statements, so a concurrent caller can slip between them, and only the
    # schema's partial unique index can decide which one wins.
    try:
        attempt_id = _reserve_launch(
            db_path,
            task_type=task_type,
            decision_id=decision_id,
            requested_provider=provider,
            source="mcp",
            host=host,
            invocation_mode=resolved_mode,
            request_key=request_key,
            # The workspace this delegation is pointed at, as a column rather
            # than as one term inside `request_key`'s hash: a validation run,
            # a review or a commit standing in this directory can then ask
            # whether anything is still writing here without holding the
            # prompt it never received.
            working_directory=working_directory,
        )
    except service_history.DuplicateActiveAttempt:
        return _refuse_duplicate(service_history.find_in_flight_duplicate(db_path, request_key))

    # The row now exists, so the coroutine waiting on this thread can name it
    # if the caller leaves. Published here and nowhere else: before this line
    # there is nothing to mark, and after a duplicate refusal there is no
    # attempt of ours to mark. One call, one
    # attribute write: the reader is another thread arriving at an arbitrary
    # instant, and must never see an id without the database that holds it.
    if caller_wait is not None:
        caller_wait.publish(db_path, attempt_id)
        # The other side of the hand-off: if the caller already left while
        # this row was being written, the coroutine had no id to mark and
        # left the reason on the handle instead. This side has both facts
        # now, so it records the departure.
        if caller_wait.abandoned_reason is not None:
            service_history.record_caller_abandonment(
                db_path, attempt_id, reason=caller_wait.abandoned_reason
            )

    try:
        return _delegate_after_policy(
            task_type,
            prompt,
            working_directory,
            provider=provider,
            usage_directory=usage_directory,
            config_path=config_path,
            timeout=timeout,
            session_id=session_id,
            host=host,
            host_protocol=host_protocol,
            host_request_meta=host_request_meta,
            model=model,
            reasoning_effort=reasoning_effort,
            capsule=capsule,
            expected_consumption=expected_consumption,
            agent_runtime=agent_runtime,
            db_path=db_path,
            mode=mode,
            paused=paused,
            resolved_mode=resolved_mode,
            mode_source=mode_source,
            decision_id=decision_id,
            attempt_id=attempt_id,
            lineage=lineage,
            user_approved=user_approved,
            caller_host=caller_host,
            supplied_files=supplied_files,
            approval=approval,
        )
    except Exception as exc:
        # An exception anywhere in delegation is itself the outcome: record it
        # rather than letting the attempt look abandoned, then re-raise.
        if _prelaunch_refusal(exc):
            raise
        call = _launch_call.get()
        if ((call is not None and call.get("job_owned")) or
                _job_for_attempt_or_none(db_path, attempt_id) is not None):
            raise
        service_history.resolve_delegation_attempt(
            db_path,
            attempt_id,
            status="refused" if _prelaunch_refusal(exc) else "failed",
            failure_kind="policy_refused" if _prelaunch_refusal(exc) else "exception",
            detail=str(exc) if _prelaunch_refusal(exc) else "delegation_exception",
        )
        raise


def _delegate_after_policy(
    task_type: str,
    prompt: str,
    working_directory: str,
    *,
    provider: str | None,
    usage_directory: str | None,
    config_path: str | None,
    timeout: float,
    session_id: str | None,
    host: str | None,
    host_protocol: HostProtocolIdentity | None,
    host_request_meta: HostRequestMetadata | None,
    model: str | None,
    reasoning_effort: str | None,
    capsule: dict | None,
    expected_consumption: str | None,
    agent_runtime: str | None,
    db_path,
    mode: str,
    paused: bool,
    resolved_mode: str | None,
    mode_source: str | None,
    decision_id: str,
    attempt_id: int | None,
    lineage: Any = None,
    user_approved: bool = False,
    caller_host: str | None = None,
    capability='delegate',
    supplied_files: list[str] | None = None,
    approval: str = "delegate",
) -> dict:
    """The delegation body, after policy allowed it and the attempt is recorded."""

    if lineage is None:
        # Direct callers (tests, internal paths) that do not supply lineage
        # get the honest default: this delegation had no prior routing
        # decision, so the routing row it writes below IS its decision.
        from ..lineage import DecisionLineage

        lineage = DecisionLineage(
            decision_id=decision_id,
            decision_id_source="minted",
            prior_route=False,
            task_type=task_type,
            requested_provider=provider,
        )

    effective_prompt = prompt
    parsed_capsule = None
    if capsule is not None:
        from ..capsule import TaskCapsule

        parsed_capsule = TaskCapsule.from_dict(capsule)
        with anticipated_failure():
            if parsed_capsule.action_authority is not None and parsed_capsule.task_type != task_type:
                raise ValueError("authority-bearing capsule task_type must match the delegation task_type")
        rendered = parsed_capsule.to_prompt()
        if not prompt or not prompt.strip():
            effective_prompt = rendered
        else:
            effective_prompt = prompt.strip() + "\n\n" + rendered

    config = load_config(config_path)
    providers = build_providers(config, Path(usage_directory) if usage_directory else None)
    # The SAME fresh-refusal overlay the advisory routing helpers apply.
    # It belongs here rather than only there: these
    # statuses are what actually reach `ExecutionManager.delegate`, so without
    # this line a recorded refusal changed the advice and left the delegation
    # that spends the quota untouched. The executor-side
    # WRITER of a refusal is a separate, declared follow-up; this is the
    # reader half, and it is what makes an already-recorded refusal binding.
    statuses = service_health.overlay_quota_refusals(
        (provider_instance.get_status() for provider_instance in providers), db_path
    )
    # Quota is display-only: a delegation never starts or waits for a quota read.

    decision_mode = service_history.get_decision_mode(db_path)
    manager = _build_execution_manager(config)
    # Freeze once. All eligibility, execution and
    # recording paths (including the service runner) consume these inputs.
    statuses = tuple(statuses)
    decision_now = datetime.now(timezone.utc)
    decision_catalog = _decision_catalog(config)
    policy_excluded = netpolicy.providers_refused_by_policy(registry=manager.registry)
    _bind_launch(manager, attempt_id, resolved_mode, user_approved, caller_host, decision_id=decision_id,
                 capability=capability)
    policy = _launch_policy(db_path, resolved_mode, user_approved, caller_host)
    if not policy.allowed:
        raise LaunchRefused(policy.reason)

    # Keep the original early intent reservation/fingerprint. Notes are private
    # preparation material, attached only before sizing and job reservation.
    sizing_prompt = prompt
    if parsed_capsule is None or parsed_capsule.action_authority is None:
        from .. import project_notes, identity as installation
        from ..execution.base import READ_ONLY_TASK_TYPES
        from ..models import TaskType
        # Writable launches still earn their existing availability/runtime/
        # capability refusals before the beta restriction is considered.
        if TaskType(task_type) not in READ_ONLY_TASK_TYPES:
            baseline_consumption = _resolve_consumption_evidence(prompt, parsed_capsule, expected_consumption)
            _require_job_eligibility(manager, task_type, statuses, provider, agent_runtime,
                                     parsed_capsule, baseline_consumption, now=decision_now,
                                     catalog=decision_catalog, policy_excluded=policy_excluded)
        project_notes.require_read_only(task_type)
        call = _launch_call.get()
        if call is not None:
            call['private_notes'] = True
        enriched = project_notes.attach(
            effective_prompt, working_directory, None,
            parsed_capsule.relevant_files if parsed_capsule else (),
            installation_key=installation.peek_installation_id(),
        )
        sizing_prompt = prompt + enriched[len(effective_prompt):]
        effective_prompt = enriched
    consumption_evidence = _resolve_consumption_evidence(
        sizing_prompt, parsed_capsule, expected_consumption
    )

    # The decision_id (minted before the policy check) joins every history row
    # this decision produces -- delegation_attempts / routing_decisions /
    # plan_decisions / execution_history -- and the evidence snapshot freezes
    # the router's decision-time inputs for replay.
    #
    # The host is frozen here too, at the moment the decision is made, so the
    # research record can compare the agent that already owned the context
    # against the worker that ended up doing the work. Only the payload's
    # SIZE is recorded -- the prompt itself never enters an evidence snapshot.
    host_snapshot = with_decision_context(
        capture_host_decision_snapshot(
            protocol=host_protocol, request_meta=host_request_meta
        ),
        context_owner=(
            getattr(parsed_capsule, "context_ownership", None) if parsed_capsule else None
        ),
        context_richness=(
            getattr(parsed_capsule, "context_richness", None) if parsed_capsule else None
        ),
        handoff_payload_size=len(effective_prompt or ""),
    )

    def evidence_for_target(target: str | None) -> str:
        """Evidence snapshot with the handoff direction this row records.

        Direction is a property of (host, target), not of the host, so it is
        resolved per routing row rather than at observation time.
        """

        return evidence_snapshot_json(
            statuses,
            config,
            routing_mode=mode,
            routing_paused=paused,
            expected_consumption=consumption_evidence,
            host_snapshot=with_decision_context(host_snapshot, selected_runtime=target),
        )

    evidence_json = evidence_for_target(provider)
    # Verification spec: when the capsule names relevant tests and
    # the worker claims completion or partial completion, the evidence
    # records those tests as not verified; ModelQuad runs none of them.
    # A capsule that names none yields baseline (claim-level)
    # evidence only.
    verification_spec = None
    if parsed_capsule is not None and parsed_capsule.relevant_tests:
        try:
            from ..outcome import VerificationSpec

            verification_spec = VerificationSpec(
                relevant_tests=parsed_capsule.relevant_tests
            )
        except ValueError:
            # A malformed test target must not fail the delegation; the
            # execution simply proceeds without a verification spec.
            verification_spec = None

    call_args = dict(
        config=config, task_type=task_type, effective_prompt=effective_prompt,
        working_directory=working_directory, provider=provider, agent_runtime=agent_runtime,
        statuses=statuses, timeout=timeout, session_id=session_id, model=model,
        reasoning_effort=reasoning_effort, consumption_evidence=consumption_evidence,
        verification_spec=verification_spec, parsed_capsule=parsed_capsule,
        db_path=db_path, attempt_id=attempt_id, usage_directory=usage_directory,
        lineage=lineage, resolved_mode=resolved_mode, mode_source=mode_source,
        host=host, decision_id=decision_id, host_snapshot=host_snapshot, mode=mode,
        paused=paused, decision_mode="deterministic", reasoning_fell_back=False,
        plan_decision_id=None,
        now=decision_now, catalog=decision_catalog, policy_excluded=policy_excluded,
    )
    if supplied_files is not None:
        # The files the user approved in the tool call, and through which
        # prompt. The executor attaches exactly these and records what it sent.
        call_args["metadata"] = {"supplied_files": list(supplied_files),
                                 "approval": "handoff" if approval == "handoff" else "delegate"}
        # Bind the approval to the contents as they are now, right after the
        # user approved (or as the handoff window showed them). The executor
        # refuses if a file it would send has changed since.
        from ..execution import noshell
        digests = _approved_digests.get()
        if digests is None:
            digests = noshell.approved_digests(effective_prompt, working_directory, list(supplied_files),
                                               caps=_repo_caps(working_directory))
        if digests is not None:
            call_args["metadata"]["approved_digests"] = dict(digests)
    runner_enabled = service_history.get_job_runner_enabled(db_path)
    if runner_enabled:
        from ..jobs import service_client

        # Eligibility still refuses at the front door, before reserving a job.
        _require_job_eligibility(manager, task_type, statuses, provider, agent_runtime,
                                 parsed_capsule, consumption_evidence,
                                 now=decision_now, catalog=decision_catalog, policy_excluded=policy_excluded)
        client = service_client.connect(db_path)

        def in_session(reason):
            from .. import procutil

            _warn_in_session_runner(reason)
            # The executor spawns in this same anyio worker thread. Keep the
            # Job Object here so server process death stops contained workers.
            token = procutil.default_kill_on_close.set(True)
            try:
                payload = run_bound_delegation(
                    manager,
                    **call_args,
                    on_worker_packed=_runner_packed,
                    on_worker_spawn=lambda _pid: _runner_notice("Codex started"),
                )
            finally:
                procutil.default_kill_on_close.reset(token)
            payload["runner_note"] = _in_session_runner_note(reason)
            payload["runner_unavailable_reason"] = reason
            return payload

        if isinstance(client, service_client.Unavailable):
            return in_session(client.reason)
        prepared = prepare_delegation(
            call_args, config_path=config_path, user_approved=user_approved,
            caller_host=caller_host,
        )
        return _start_prepared_job(prepared, client, in_session)
    from .. import procutil

    # Same containment as the in-session fallback: if this server dies, the
    # worker's Job Object closes and Windows stops its tree.
    token = procutil.default_kill_on_close.set(True)
    try:
        payload = run_bound_delegation(
            manager,
            **call_args,
            on_worker_packed=_runner_packed,
            on_worker_spawn=lambda _pid: _runner_notice("Codex started"),
        )
    finally:
        procutil.default_kill_on_close.reset(token)
    return payload


JOB_WAIT_SECONDS = 45.0  # bounded inline shortcut under 60 s; never the worker timeout


def _decision_catalog(config):
    """Clean tolerated static fields before advice or either execution path routes."""
    catalog = cached_catalog(config)
    if catalog is None:
        return None
    descriptors = []
    changed = False
    for descriptor in catalog.descriptors:
        changes = {}
        if descriptor.speed_class is not None and type(descriptor.speed_class) is not str:
            changes['speed_class'] = None
        for field in ('relative_strength', 'relative_cost'):
            value = getattr(descriptor, field)
            if value is None:
                continue
            try:
                conforming = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                conforming = False
            if not conforming:
                changes[field] = None
        if changes:
            changed = True
            try:
                descriptor = replace(descriptor, **changes)
            except (TypeError, ValueError, OverflowError):
                continue  # Safety net only: clearing these fields cannot fail today.
        descriptors.append(descriptor)
    if not changed:
        return catalog
    # No descriptor values, source paths or exception bodies enter diagnostics.
    logger.debug('Cleaning non-conforming decision catalog entries')
    return ModelCatalog(tuple(descriptors))


def prepare_delegation(args, *, config_path, user_approved, caller_host):
    """Freeze every shared-call input before transferring attempt ownership."""
    from .. import config as config_module

    resolved_config = (Path(config_path).expanduser().resolve() if config_path else
                       Path(config_module.__file__).with_name("default.yaml").resolve())
    capsule = args["parsed_capsule"]
    prepared = PreparedDelegation(
        task_type=args["task_type"], prompt=args["effective_prompt"],
        working_directory=str(Path(args["working_directory"]).resolve()),
        provider=args["provider"], agent_runtime=args["agent_runtime"],
        statuses=[status.to_dict() for status in args["statuses"]],
        session_id=args["session_id"], model=args["model"],
        reasoning_effort=args["reasoning_effort"],
        consumption=args["consumption_evidence"].to_dict(),
        verification=asdict(args["verification_spec"]) if args["verification_spec"] else None,
        required_capabilities=list(capsule.capability_requirements) if capsule else None,
        action_authority=capsule.action_authority.to_dict() if capsule and capsule.action_authority else None,
        metadata=args.get("metadata"), timeout=args["timeout"],
        decision_id=args["decision_id"], attempt_id=args["attempt_id"],
        db_path=str(Path(args["db_path"]).resolve()), config_path=str(resolved_config),
        invocation_mode=args["resolved_mode"], user_approved=user_approved,
        caller_host=caller_host, host=args["host"], mode=args["mode"], paused=args["paused"],
        mode_source=args["mode_source"],
        usage_directory=str(Path(args["usage_directory"] or DEFAULT_USAGE_DIRECTORY).resolve()),
        host_snapshot=args["host_snapshot"].to_dict(), lineage=asdict(args["lineage"]),
        capsule=capsule.to_dict() if capsule else None,
        decision_mode=args["decision_mode"], reasoning_fell_back=args["reasoning_fell_back"],
        plan_decision_id=args["plan_decision_id"],
        decision_now=args['now'].isoformat() if args.get('now') is not None else None,
        catalog=args['catalog'].to_dict() if args.get('catalog') is not None else None,
        policy_excluded=args.get('policy_excluded'),
        **({'capability': args['capability'], 'review': args['review']} if args.get('review') is not None else {}),
    )
    # Strict JSON: no custom stringification of unsupported execution inputs.
    rebuilt = PreparedDelegation.from_dict(prepared.to_dict())
    if rebuilt.rebuild_statuses() != list(args["statuses"]):
        raise ValueError("provider evidence cannot be serialized reversibly")
    if HostDecisionSnapshot.from_dict(rebuilt.host_snapshot) != args["host_snapshot"]:
        raise ValueError("caller evidence cannot be serialized reversibly")
    if rebuilt.rebuild_decision_now() != args.get('now'):
        raise ValueError('decision clock cannot be serialized reversibly')
    if rebuilt.rebuild_catalog() != args.get('catalog'):
        raise ValueError('decision catalog cannot be serialized reversibly')
    return rebuilt


def _require_job_eligibility(manager, task_type, statuses, provider, runtime, capsule, consumption,
                             *, now=None, catalog=None, policy_excluded=None):
    required = capsule.capability_requirements if capsule else None
    if runtime is not None:
        descriptor = manager.registry.find_runtime(runtime)
        if descriptor is None or (provider is not None and descriptor.provider != provider):
            raise LaunchRefused("runtime_provider_mismatch")
        provider = provider or descriptor.provider
    policy = policy_excluded if policy_excluded is not None else netpolicy.providers_refused_by_policy(registry=manager.registry)
    if provider is not None:
        executor = manager.executors.get(manager._runtime_key(provider, runtime))
        if executor is None or not executor.is_available():
            raise LaunchRefused('provider_unavailable')
        from ..models import ProviderStatus
        status = next((item for item in statuses if item.name == provider), ProviderStatus(provider))
        if manager.router.eligibility_refusal(task_type, provider, status, required, now=now, policy_excluded=policy):
            raise LaunchRefused("routing_refused")
        if provider in manager.independence_excluded:
            raise LaunchRefused('independence_unavailable')
    else:
        route = manager.router.route(task_type, statuses, expected_consumption=consumption,
                                     now=now, catalog=catalog,
                                     required_capabilities=required, policy_excluded=policy,
                                     **({'independence_excluded': manager.independence_excluded}
                                        if manager.independence_excluded else {}))
        if route.recommendation is None:
            raise LaunchRefused('independence_unavailable' if manager.independence_refused(route.providers)
                                else 'routing_refused')
        if not any(candidate.eligible and candidate.score is not None and
                   candidate.provider not in manager.independence_excluded and
                   (executor := manager.executors.get(manager._runtime_key(candidate.provider, None))) is not None
                   and executor.is_available() for candidate in route.providers):
            raise LaunchRefused('independence_unavailable' if manager.independence_refused(route.providers, operational=True)
                                else 'provider_unavailable')


def _start_prepared_job(prepared, client, in_session):
    """Transient Host preparation guidance; never serialized into the spec.

    A detached worker cannot return transient names through its durable store.
    The Host reports its bounded local preparation observation instead; the
    worker independently rechecks files and admission when it executes.
    """
    diagnostics = []
    report = {}
    files = (getattr(prepared, "metadata", None) or {}).get("supplied_files") or ()
    if prepared.action_authority is None:
        from ..execution.noshell import supplied_prompt, SuppliedContextRefused
        try:
            supplied_prompt(prepared.prompt, prepared.working_directory, files=files,
                            completion_protocol=True, diagnostics=diagnostics, report=report,
                            caps=_repo_caps(prepared.working_directory))
        except SuppliedContextRefused:
            pass  # executor owns authoritative admission; no ownership change
    payload = _start_prepared_job_closed(prepared, client, in_session)
    if report and files and "sent_files" not in payload:
        # What preparation would send; the worker re-reads and its own record
        # (modelquad_activity_detail) is the authoritative one.
        payload["sent_files"] = report.get("sent", [])
        payload["skipped_files"] = report.get("skipped", [])
        payload["sent_files_stage"] = "host_preparation_worker_rechecks"
    from ..execution.bound_launch import host_diagnostic_payload
    rendered = host_diagnostic_payload(diagnostics)
    if rendered and 'host_diagnostics' not in payload:
        payload['host_diagnostics'] = rendered
        payload['host_diagnostics_stage'] = 'host_preparation_worker_rechecks'
    limitation = getattr(client, 'launch_limitation', None)
    if limitation is not None:
        payload['launch_limitation'] = limitation
    return payload


def _start_prepared_job_closed(prepared, client, in_session):
    from .jobs_tools import collect_job_response
    from ..jobs.handle import JobStart, reserve_and_write

    spec = JobSpec((), prepared.working_directory, prepared.db_path, prepared.attempt_id,
                   prepared.timeout, sink="store", provider=prepared.provider, delegation=prepared)
    started = reserve_and_write(spec, probe_caller=False)
    if isinstance(started, JobStart):
        # Before reservation the starter owns this failure. After reservation,
        # reserve_and_write has already closed both records in one transaction.
        if started.ref is None:
            service_history.resolve_delegation_attempt(
                prepared.db_path, prepared.attempt_id, status="refused", failure_kind="policy_refused",
                detail=started.limitation, only_if_open=True,
            )
        return {"delegated": False, "executed": False, "refused": True,
                "reason": started.limitation, "decision_id": prepared.decision_id,
                **({"job_id": started.ref.job_id} if started.ref else {})}
    # From reservation onward neither timeout, cancellation nor exceptions on
    # this Host's wait path may resolve the job-owned attempt.
    launch_call = _launch_call.get()
    if launch_call is not None:
        launch_call["job_owned"] = True
    from ..jobs.launch_answer import close_refused_handoff, launch_answer, refusal_label
    outcome, label = launch_answer(client, started.ref.job_id, started.spec_path.name)
    if outcome != "launched":
        refused = refusal_label(outcome, label)
        if refused is not None:
            try:
                closed = close_refused_handoff(prepared.db_path, started.ref, refused, prepared.capability)
            except Exception:
                closed = False
            if closed:
                try:
                    started.spec_path.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning("caller_spec_cleanup_unavailable")
                return {"delegated": False, "executed": False, "refused": True,
                        "reason": refused, "decision_id": prepared.decision_id,
                        "job_id": started.ref.job_id}
            # Another owner (or uncertain closure) retains the existing wait
            # path. A terminal answer never releases linkage or runs in-session.
        else:
            try:
                released = service_history.release_unlaunched_job(prepared.db_path, started.ref.job_id)
            except Exception:
                released = False
            if released:
                if launch_call is not None:
                    launch_call["job_owned"] = False
                try:
                    started.spec_path.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning("caller_spec_cleanup_unavailable")
                return in_session(None if label == 'job_runner_disabled' else label)
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    while True:
        row = job_status(started.ref.job_id, db_path=prepared.db_path)
        if row is None:
            return {"job_id": started.ref.job_id, "status": "unknown",
                    "poll_with": "modelquad_job_wait"}
        if row["ended_at"] is not None or row["status"] not in {"starting", "running"}:
            return collect_job_response(started.ref.job_id, record=row)
        if time.monotonic() >= deadline:
            return {"job_id": started.ref.job_id, "status": row["status"],
                    "poll_with": "modelquad_job_wait"}
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


@server.tool(title="Quota and provider status", annotations=READ_ONLY)
def modelquad_status() -> dict:
    """Show local provider availability and display-only quota readings."""
    return _provider_status_payload(USAGE_DIRECTORY, CONFIG_PATH)


#: Which approval prompt led to a delegation: the handoff form sets it for
#: the one delegate call it makes. Never a tool argument the model can set.
_approval_route: ContextVar[str] = ContextVar("modelquad_approval_route", default="delegate")
#: {path: digest} of the files as the handoff window showed them, when the
#: approval came from the window; otherwise None and the call takes them itself.
_approved_digests: ContextVar[dict | None] = ContextVar("modelquad_approved_digests", default=None)

MAX_DELEGATED_FILES = 32
FilesArgument = Annotated[list[str] | None, Field(max_length=MAX_DELEGATED_FILES)]


def _plan_views() -> dict:
    """Each side's plan view: Codex's own report first, then what the user set.

    Read locally from ``usage-codex.json`` and ``preferences.json``. Only the
    account kind word, as a short sentence or the ``plans`` block, ever
    reaches Claude; the plan name itself never does.
    """
    from .. import plans

    directory = Path(USAGE_DIRECTORY) if USAGE_DIRECTORY else DEFAULT_USAGE_DIRECTORY
    return plans.current(directory)


def _codex_account() -> str:
    """"personal", "workspace" or "unknown" for the Codex side."""
    return _plan_views()["codex"].kind


def _repo_policy_refusal(task_type: str, working_directory: str,
                         usage_directory: str | None = None) -> dict | None:
    """The repository file's closed refusal for this call, or None to go on.

    Read fresh on every call (never cached). ``account = "workspace"`` needs
    the Codex account kind to be workspace; unknown refuses.
    """
    from .. import plans, repo_policy

    policy = repo_policy.load(working_directory)
    kind = plans.UNKNOWN
    if policy.refusal is None and policy.account == "workspace":
        try:
            directory = Path(usage_directory or USAGE_DIRECTORY or DEFAULT_USAGE_DIRECTORY)
            kind = plans.current(directory)["codex"].kind
        except Exception:  # noqa: BLE001 -- an unreadable account reads unknown, and refuses
            kind = plans.UNKNOWN
    return repo_policy.refusal(policy, task_type, account_kind=kind)


def _repo_caps(working_directory: str) -> dict:
    """The packer caps the repository file allows now: never above the built-in."""
    from .. import repo_policy

    return repo_policy.load(working_directory).caps


def _codex_account_line() -> str:
    """The account line of a reply: both sides when one is work and one personal."""
    from .. import plans

    return plans.account_line(_plan_views())


def _checked_files(files: Any) -> list[str]:
    if files is None:
        return []
    if type(files) not in (list, tuple) or any(type(name) is not str for name in files):
        raise ToolError("files must be a list of paths relative to working_directory")
    if len(files) > MAX_DELEGATED_FILES:
        raise ToolError(f"files names at most {MAX_DELEGATED_FILES} paths")
    return list(files)


def _files_to_send(working_directory: str, files: list[str]) -> tuple[list[dict], list[str]]:
    """The files a handoff would send, as {path, bytes} (bytes None when not
    found), and the names it would not send with why.

    Applies the same read rules as the worker (Claude Code Read deny rules,
    Git, ``.modelquadignore``) and reads only file sizes, never contents.
    """
    from ..execution import file_rules, noshell

    names = noshell.explicit_names(files, working_directory)
    valid, invalid = [], []
    for name in dict.fromkeys(names):
        try:
            valid.append(noshell.canonical_path(name))
        except noshell.TargetError:
            invalid.append(name)
    allowed, withheld = file_rules.screen(working_directory, valid)
    root = Path(working_directory)
    entries = []
    for name in allowed:
        try:
            size = root.joinpath(*name.split("/")).lstat().st_size
        except OSError:
            size = None
        entries.append({"path": name, "bytes": size})
    skipped = [f"{name} ({reason})" for name, reason in withheld]
    skipped += [f"{name[:80]} (invalid path)" for name in invalid]
    return entries, skipped


def _files_preview(working_directory: str, files: list[str]) -> str:
    """The files a handoff would send, with sizes, for the approval form."""
    entries, skipped = _files_to_send(working_directory, files)
    if not entries and not skipped:
        return "Files to send: none (only the task text)."
    rows = [f"- {entry['path']} (not found)" if entry["bytes"] is None
            else f"- {entry['path']} ({entry['bytes']} bytes)" for entry in entries]
    total = sum(entry["bytes"] or 0 for entry in entries)
    lines = [f"Files to send ({len(entries)}, {total} bytes):", *rows]
    if skipped:
        lines.append("Not sent: " + ", ".join(skipped))
    return "\n".join(lines)


def _suggested_files(prompt: str, working_directory: str) -> list[str]:
    """Existing files the task text names. Suggested only; never attached."""
    from ..execution import noshell

    try:
        names = noshell.named_files(prompt, working_directory)
    except Exception:  # noqa: BLE001 - a suggestion must never fail a reply
        return []
    found: list[str] = []
    for name in names:
        try:
            name = noshell.canonical_path(name.replace("\\", "/"))
            if Path(working_directory).joinpath(*name.split("/")).is_file() and name not in found:
                found.append(name)
        except (noshell.TargetError, OSError, ValueError):
            continue
        if len(found) >= 8:
            break
    return found


def _report_files(payload: dict[str, Any], prompt: str, working_directory: str, files: list[str]) -> None:
    """Add what was sent, what was skipped and the account kind to a reply."""
    audit = (payload.get("metadata") or {}).get("file_audit")
    if isinstance(audit, dict):
        payload.setdefault("sent_files", audit.get("sent") or [])
        payload.setdefault("skipped_files", audit.get("skipped") or [])
    payload["codex_account"] = _codex_account_line()
    _report_needed_files(payload, working_directory)
    if not files:
        suggested = _suggested_files(prompt, working_directory)
        if suggested:
            payload["suggested_files"] = suggested
            payload["suggested_files_note"] = (
                "These files are named in the task but were not sent. To include them, ask "
                "again and pass them in files.")


def _report_needed_files(payload: dict[str, Any], working_directory: str) -> None:
    """Add the files Codex's answer says it needed, when there are any."""
    from ..execution import noshell

    sent = {item.get("path") for item in payload.get("sent_files") or () if isinstance(item, dict)}
    needed = noshell.needed_files(payload.get("output"), working_directory, sent)
    if needed:
        payload["files_needed"] = needed
        payload["files_needed_note"] = noshell.NEEDED_FILES_NOTE


def _delegate_completion_notice(payload: dict[str, Any]) -> str:
    """Return a closed, content-free terminal phase for one inline call."""
    reason = payload.get("task_status_reason")
    if payload.get("refused") and isinstance(reason, str) and reason.replace("_", "").isalnum():
        return f"Codex refused: {reason}"
    if payload.get("refused"):
        return "Codex answer withheld"
    return "Codex answered" if isinstance(payload.get("output"), str) and payload["output"] else "Codex answer withheld"


@server.tool(title="Ask Codex for a second opinion", annotations=STARTS_CODEX, meta={"anthropic/requiresUserInteraction": True})
async def modelquad_delegate(
    task_type: str,
    prompt: str,
    working_directory: str,
    timeout: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=840, allow_inf_nan=False),
    model: str | None = None,
    reasoning_effort: str | None = None,
    ctx: Context | None = None,
    capsule: dict | None = None,
    files: FilesArgument = None,
) -> CallToolResult:
    """Ask read-only Codex for a review, exploration, or architecture opinion.
    Codex sees only the task text and the files listed in `files` (paths relative to
    working_directory, at most 32); a path named only in the task text is not sent.
    Claude Code asks the user before every call; quotas only inform the reply."""
    if isinstance(capsule, dict) and "action_authority" in capsule:
        raise ToolError("capsule action authority is not accepted")
    files = _checked_files(files)
    first = _notice_first()
    if first is not None:
        return first
    approval = _approval_route.get()
    # Everything that reads the request context is read BEFORE the await:
    # once the caller has gone, the context it came in on has gone with it,
    # and a host identity re-read afterwards would be a different (or an
    # absent) fact. The presentation below is built from the returned payload
    # and these already-captured values, so it stays after the await.
    host_name = host_from_context(ctx)
    host_protocol = host_protocol_from_context(ctx)
    host_request_meta = host_request_meta_from_context(ctx)
    handle = CallerWaitHandle()
    with _runner_notifications(ctx) as progress:
        payload = await _delegate_observing_the_caller(
            functools.partial(
                _delegate_payload,
                task_type,
                prompt,
                working_directory,
                "codex",
                USAGE_DIRECTORY,
                CONFIG_PATH,
                timeout,
                None,
                invocation_mode=None,
                host=host_name,
                host_protocol=host_protocol,
                host_request_meta=host_request_meta,
                model=model,
                reasoning_effort=reasoning_effort,
                user_approved=False,
                capsule=capsule,
                expected_consumption=None,
                agent_runtime=None,
                decision_id=None,
                caller_wait=handle,
                supplied_files=files,
                approval=approval,
            ),
            handle,
            progress,
        )
        if "job_id" not in payload:
            await progress.report(_delegate_completion_notice(payload))
    _report_files(payload, prompt, working_directory, files)
    if "job_id" in payload:
        # A job response has its own evidence shape. Legacy presentation
        # expects process fields that the output seam deliberately cannot claim.
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structuredContent=payload)
    presentation = build_delegation_presentation(
        payload,
        task_type=task_type,
        invocation_mode=resolve_invocation_mode(None)[0],
        host_name=host_name,
        # The origin runtime the execution row itself recorded -- the same
        # handoff fact, not a fresh observation that could disagree with it.
        host_runtime=((payload.get("metadata") or {}).get("host_handoff") or {}).get("origin_runtime"),
        developer_mode=_developer_mode(),
    )
    result = _decision_result(payload, presentation)
    visible = {key: payload.get(key) for key in ("task_type", "provider", "status", "partial", "output",
                                                 "sent_files", "skipped_files", "codex_account",
                                                 "suggested_files", "files_needed",
                                                 "files_needed_note")
               if key in payload}
    if isinstance(visible.get("output"), str):
        from .jobs_tools import MAX_SHOWN_OUTPUT_CHARS
        visible["output"] = visible["output"][:MAX_SHOWN_OUTPUT_CHARS]
    result.content.append(TextContent(type="text", text=json.dumps(visible)))
    return result


class HandoffApproval(BaseModel):
    """The one field of the handoff prompt shown to the user in Claude Code."""

    hand_off: bool = Field(
        default=False,
        title="Ask Codex for a read-only second opinion",
        description="Tick to let Codex, under your own login, answer this read-only task now.",
    )


def _plain_result(payload: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          structured_content=payload)


NOTICE_FIRST_NEXT = (
    "Codex was not started. Show the user the notice above in full, as written: it is "
    "what ModelQuad sends where, and it is shown once, before the first Codex call. If "
    "the user wants to go ahead, call the same tool again; Claude Code asks them again."
)


def _notice_first() -> CallToolResult | None:
    """The data-flow notice in place of the first Codex call, or None once it was shown.

    ``modelquad onboard`` and ``modelquad install`` print the notice, but the
    plugin can put ModelQuad in front of someone who ran neither. Their first
    call that would start Codex returns the notice instead, once.
    """

    from ..disclosure import DATA_FLOW, notice_shown, record_notice_shown

    if notice_shown():
        return None
    from .. import plans

    notice = list(DATA_FLOW)
    views = _plan_views()
    if plans.accounts_differ(views):
        notice.append(plans.account_line(views))
    payload: dict[str, Any] = {"status": "notice_first", "codex_started": False,
                               "notice": notice, "next": NOTICE_FIRST_NEXT}
    try:
        record_notice_shown("first_call")
    except OSError:
        payload["note"] = "ModelQuad could not record that the notice was shown, so it shows it again."
    return _plain_result(payload)


#: No person reads the handoff form, ticks its box and answers this fast. The
#: desktop app's Code tab answers a form it never shows ("print mode") in
#: about 0.1 s; such an answer is the client's, not the user's.
MIN_HUMAN_ANSWER_SECONDS = 1.0

#: When each request sent its form on a pre-2026-07-28 connection, keyed by
#: request id. The resolver stamps it; the tool body pops it.
_HANDOFF_ASKED_AT: dict[Any, float] = {}


def _request_key(ctx: Context | None) -> Any:
    """This request's id, or None outside a request (the SDK raises there)."""
    try:
        return ctx.request_id if ctx is not None else None
    except (AttributeError, ValueError):
        return None


def _form_rounds(ctx: Context | None) -> bool:
    """True when the form goes out as an input-required round (2026-07-28 and
    later), which comes back in a later request, so no single request can
    time the answer."""
    version = getattr(ctx, "protocol_version", None) if ctx is not None else None
    return isinstance(version, str) and version >= "2026-07-28"


def _client_shows_apps(ctx: Context | None) -> bool:
    """Whether this client renders MCP Apps (``ui://`` widgets)."""
    from mcp.server.apps import client_supports_apps
    try:
        return ctx is not None and client_supports_apps(ctx)
    except Exception:  # noqa: BLE001 - outside a request, or a client that sent no capabilities
        return False


def _desktop_card_approves(ctx: Context | None) -> bool:
    """True when Claude Code's own approval card is the user's whole answer.

    The desktop app's Code tab never shows an MCP form: its host answers every
    one with "decline" (Claude 3.6556, read from its app.asar on 2026-10-25).
    Claude Code sets ``CLAUDE_CODE_ENTRYPOINT=claude-desktop`` for the servers
    it starts there. The card it shows before this tool runs
    (``anthropic/requiresUserInteraction``) is a click Claude cannot make, the
    same approval ``modelquad_delegate`` rests on.

    This holds even where the desktop app renders MCP Apps. In the live test
    of 2026-10-25 it rendered the handoff window, but neither press of Approve
    reached ``modelquad_handoff_confirm``, so a window there could never start
    Codex. The card stays the answer until the window's press is shown to
    arrive."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop"


def _shows_handoff_window(ctx: Context | None) -> bool:
    """Whether this session asks with the handoff window (see handoff_widget)."""
    return handoff_widget.ENABLED and _client_shows_apps(ctx) and not _desktop_card_approves(ctx)


def _unseen_answer(answer: str, elapsed: float | None) -> str | None:
    """Why an answer counts as the form never shown, or None when it stands."""
    if answer in ("accept", "decline", "cancel") and elapsed is not None \
            and elapsed < MIN_HUMAN_ANSWER_SECONDS:
        return "answered_without_user"
    return None


async def _elicit_handoff(ctx: Context | None, message: str) -> tuple[str, str | None]:
    """Ask the user through MCP elicitation (form mode).

    Returns ("accept", None) only for an accepted form with the box ticked;
    "decline" or "cancel" as the user answered; ("unavailable", why) when
    there is no request context or the client cannot show the form, where
    ``why`` is the error's class name only (never its text). Anything but
    "accept" starts nothing.
    """
    if ctx is None:
        return "unavailable", "no_request_context"
    asked = time.monotonic()
    try:
        answer = await ctx.elicit(message=message, schema=HandoffApproval)
    except Exception as error:  # noqa: BLE001 - a client without elicitation, or a malformed answer
        return "unavailable", type(error).__name__
    action = getattr(answer, "action", None)
    if action == "accept":
        data = getattr(answer, "data", None)
        result = "accept" if getattr(data, "hand_off", False) is True else "decline"
    else:
        result = "decline" if action == "decline" else "cancel"
    unseen = _unseen_answer(result, time.monotonic() - asked)
    return ("unavailable", unseen) if unseen else (result, None)


async def _ask_handoff_approval(ctx: Context | None, message: str) -> str:
    """The user's answer to the handoff form; see :func:`_elicit_handoff`."""
    return (await _elicit_handoff(ctx, message))[0]


#: The handoff form's question. Codex through ModelQuad only answers read-only.
HANDOFF_QUESTION = "Ask Codex for a read-only second opinion on this task?"

#: What Claude does when this Claude Code client cannot show the handoff form
#: (the desktop app's Code tab, for one). The delegate tool is the fallback
#: because Claude Code shows its own approval prompt before that call.
_FORM_UNAVAILABLE_STEP = (
    "This Claude Code client could not show ModelQuad's handoff form, so nothing "
    "started. Tell the user that, then ask in the conversation whether they want "
    "Codex's read-only second opinion on this task. Only if they say yes, call "
    "modelquad_delegate with the same task and files; Claude Code asks them before "
    "that call too.")


def _handoff_message(task_type: str, working_directory: str) -> tuple[str, dict | None]:
    payload = _provider_status_payload(USAGE_DIRECTORY, CONFIG_PATH)
    suggestion = payload.get("handoff_suggestion")
    lead = suggestion["message"] if isinstance(suggestion, dict) else HANDOFF_QUESTION
    folder = Path(working_directory).name or working_directory
    return (f"{lead}\nTask: {task_type} in {folder}. Codex runs it under your own login. "
            "Nothing starts unless you tick the box and accept."), suggestion


def _handoff_refusal(task_type: str, capsule: dict | None, ctx: Context | None,
                     working_directory: str | None = None) -> dict | None:
    """Why a handoff must stop before the user is asked, or None to ask."""
    if _worker_refused():
        return {"refused": True, "started": False,
                "reason": f"{_worker_refusal_prefix()} a delegated worker must never hand off work"}
    if isinstance(capsule, dict) and "action_authority" in capsule:
        return {"started": False, "approved": False, "reason": "capsule_action_authority_rejected"}
    from ..project_notes import require_read_only
    try:
        require_read_only(task_type)
    except (LaunchRefused, ValueError):
        return {"started": False, "approved": False, "reason": "task_type_not_read_only"}
    # Check pause and host-disconnect policy before asking, so the form never
    # implies that a launch is possible.
    policy = _launch_policy(service_db.DEFAULT_DB_PATH, invocation_mode="explicit_user_request",
                            user_approved=True, caller_host=host_from_context(ctx))
    if not policy.allowed:
        return {"started": False, "approved": False, "reason": policy.reason}
    # The repository's .modelquad.toml: refuse before the user is asked, so
    # the form never offers what this repository does not allow.
    if working_directory is not None:
        repo_refusal = _repo_policy_refusal(task_type, working_directory)
        if repo_refusal is not None:
            return {"started": False, "approved": False, **repo_refusal}
    return None


def _handoff_form_text(task_type: str, prompt: str, working_directory: str, files: list[str]) -> str:
    """The handoff form's text. It must read the same on every round of a
    2026-07-28 call (a changed question is asked again), so it carries no
    live quota numbers: the files with sizes, the account kind, and the task.

    The Claude Code terminal shows only the first three lines of a form and
    folds the rest behind "(+N more lines)", so those three are the question,
    the files on one line, and the account."""
    folder = Path(working_directory).name or working_directory
    preview = " ".join(prompt.split())[:300]
    entries, skipped = _files_to_send(working_directory, files)
    if entries:
        listed = ", ".join(f"{entry['path']} (not found)" if entry["bytes"] is None
                           else f"{entry['path']} ({entry['bytes']} bytes)" for entry in entries)
        total = sum(entry["bytes"] or 0 for entry in entries)
        files_line = f"Files to send ({len(entries)}, {total} bytes): {listed}"
    else:
        files_line = "Files to send: none (only the task text)."
    return "\n".join((
        HANDOFF_QUESTION,
        files_line,
        _codex_account_line(),
        *(["Not sent: " + ", ".join(skipped)] if skipped else []),
        f"Task: {task_type} in {folder}. Codex runs it under your own login. "
        "Nothing starts unless you tick the box and accept.",
        f"Task preview: {preview}",
    ))


def _handoff_question(task_type: str, prompt: str, working_directory: str, files: list[str] | None,
                      capsule: dict | None, ctx: Context) -> Any:
    """Resolver: ask with the handoff form through whichever channel the
    connection allows (a 2026-07-28 input-required round, or a request on an
    older protocol). A handoff that must stop is not asked at all; the tool
    body sees that and refuses."""
    if _handoff_refusal(task_type, capsule, ctx, working_directory) is not None:
        return "not_asked"
    try:
        checked = _checked_files(files)
    except ToolError:
        return "not_asked"
    if _desktop_card_approves(ctx):
        return "card_approved"
    if _shows_handoff_window(ctx):
        return "widget"
    # A client that declared no forms cannot answer one; asking would end the
    # call in a protocol error. The body then reports the form as unavailable.
    capabilities = getattr(ctx, "client_capabilities", None)
    if getattr(capabilities, "elicitation", None) is None:
        return "not_asked"
    if not _form_rounds(ctx):
        # The framework sends the form right after this returns.
        _HANDOFF_ASKED_AT[_request_key(ctx)] = time.monotonic()
    return Elicit(_handoff_form_text(task_type, prompt, working_directory, checked), HandoffApproval)


def _approval_answer(outcome: Any) -> str:
    """accept / decline / cancel from the framework's form outcome."""
    action = getattr(outcome, "action", None)
    if action == "accept":
        data = getattr(outcome, "data", None)
        return "accept" if getattr(data, "hand_off", False) is True else "decline"
    return "decline" if action == "decline" else "cancel"


@server.tool(title="Ask Codex for a second opinion, after a form", annotations=STARTS_CODEX,
             meta={"anthropic/requiresUserInteraction": True,
                   **({"ui": {"resourceUri": handoff_widget.URI}} if handoff_widget.ENABLED else {})})
async def modelquad_handoff(
    task_type: str,
    prompt: str,
    working_directory: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    model: str | None = None,
    reasoning_effort: str | None = None,
    capsule: dict | None = None,
    ctx: Context | None = None,
    files: FilesArgument = None,
    approval: Annotated[ElicitationResult[HandoffApproval], Resolve(_handoff_question)] = None,
) -> CallToolResult:
    """Ask the user, in a form listing the files and their total size, before asking Codex
    for a read-only second opinion (review, exploration or architecture). Only the files
    in `files` (paths relative to working_directory, at most 32) are sent; a path named
    only in the task text is not."""
    asked_at = _HANDOFF_ASKED_AT.pop(_request_key(ctx), None)
    # The checks run again here, after the user answered: an answer never
    # outlives a pause, a disconnect or a refusal that arrived meanwhile.
    refusal = _handoff_refusal(task_type, capsule, ctx, working_directory)
    if refusal is not None:
        return _plain_result(refusal)
    files = _checked_files(files)
    first = _notice_first()
    if first is not None:
        return first
    _message, suggestion = _handoff_message(task_type, working_directory)
    if getattr(approval, "data", None) == "widget" and _shows_handoff_window(ctx):
        return _plain_result(_widget_pending(
            task_type=task_type, prompt=prompt, working_directory=working_directory, timeout=timeout,
            model=model, reasoning_effort=reasoning_effort, capsule=capsule, files=files))
    # Only the framework's resolver can say so, and only when the check still
    # holds here: a direct call never passed Claude Code's approval card.
    card = getattr(approval, "data", None) == "card_approved" and _desktop_card_approves(ctx)
    if card:
        answer, why = "accept", None
    elif approval is not None and not isinstance(getattr(approval, "data", None), str):
        # Asked by the framework before this body ran (an MCP client on any protocol).
        answer, why = _approval_answer(approval), None
        unseen = _unseen_answer(answer, None if asked_at is None else time.monotonic() - asked_at)
        if unseen:
            answer, why = "unavailable", unseen
    else:
        # Called directly, without the framework's resolver: ask here.
        answer, why = await _elicit_handoff(
            ctx, _handoff_form_text(task_type, prompt, working_directory, files))
    if answer != "accept":
        refusal = {
            "started": False,
            "approved": False,
            "approval": answer,
            "message": (suggestion or {}).get("message") or HANDOFF_QUESTION,
            "next_step": ("Ask the user in the conversation. Start nothing unless they ask; then "
                          "call modelquad_delegate."),
        }
        if answer == "unavailable":
            refusal["form_unavailable_reason"] = why
            refusal["files_preview"] = _files_preview(working_directory, files)
            refusal["next_step"] = _FORM_UNAVAILABLE_STEP
        return _plain_result(refusal)
    # The card is the same approval as modelquad_delegate's, so it is recorded as one.
    token = _approval_route.set("delegate" if card else "handoff")
    try:
        return await modelquad_delegate(
            task_type=task_type,
            prompt=prompt,
            working_directory=working_directory,
            timeout=timeout,
            model=model,
            reasoning_effort=reasoning_effort,
            ctx=ctx,
            capsule=capsule,
            files=files,
        )
    finally:
        _approval_route.reset(token)


#: The longest one modelquad_handoff_wait call waits for the page's answer.
WIDGET_WAIT_SECONDS = 120.0


def _widget_pending(**arguments: Any) -> dict:
    """Hold a handoff for the approval page and say what Claude does next."""
    from ..execution import noshell
    entries, skipped = _files_to_send(arguments["working_directory"], arguments["files"] or [])
    # The window shows the files as they are now; Approve covers these contents.
    digests = noshell.approved_digests(arguments["prompt"], arguments["working_directory"],
                                       arguments["files"] or [],
                                       caps=_repo_caps(arguments["working_directory"]))
    return {
        "started": False,
        "approved": False,
        "approval": "widget_pending",
        "pending_id": handoff_widget.hold({**arguments, "approved_digests": digests or {}}),
        "sent_files_preview": entries,
        "not_sent": skipped,
        "codex_account": _codex_account_line(),
        "task_preview": " ".join(arguments["prompt"].split())[:300],
        "next_step": ("ModelQuad shows the user a window with the files and Approve / Decline buttons. "
                      "Tell the user to answer there, then call modelquad_handoff_wait with this "
                      "pending_id. Nothing starts unless the user presses Approve."),
    }


@server.tool(title="Answer a handoff", annotations=STARTS_CODEX,
             meta={"ui": {"resourceUri": handoff_widget.URI, "visibility": ["app"]}})
def modelquad_handoff_confirm(pending_id: str, approve: bool, ctx: Context | None = None) -> CallToolResult:
    """The approval page's Approve or Decline button. Only the page calls this: its
    visibility is ["app"], so a host keeps it out of the model's tools."""
    if _worker_refused():
        return _plain_result({"refused": True, "recorded": False,
                              "reason": f"{_worker_refusal_prefix()} a delegated worker must never hand off work"})
    if not _client_shows_apps(ctx):
        # Only a client that renders the page also keeps this tool from the
        # model. Anywhere else an answer here could be Claude's own.
        outcome = "client_shows_no_widget"
    else:
        outcome = handoff_widget.decide(pending_id, bool(approve))
    # One line in Claude Code's MCP log, so a press that never arrives can be
    # told from one that arrived and was refused. No ids, paths or text.
    logger.warning("handoff window answer: %s", outcome)
    if outcome == "client_shows_no_widget":
        return _plain_result({"recorded": False, "outcome": outcome})
    return _plain_result({"recorded": outcome == "recorded", "outcome": outcome})


@server.tool(title="Wait for a handoff answer", annotations=STARTS_CODEX)
async def modelquad_handoff_wait(pending_id: str, ctx: Context | None = None) -> CallToolResult:
    """After modelquad_handoff showed its approval window: wait for the user's answer and,
    only if they pressed Approve, run the task in Codex and return its answer."""
    if _worker_refused():
        return _plain_result({"refused": True, "started": False,
                              "reason": f"{_worker_refusal_prefix()} a delegated worker must never hand off work"})
    decision, arguments = await anyio.to_thread.run_sync(
        handoff_widget.take, pending_id, WIDGET_WAIT_SECONDS, abandon_on_cancel=True)
    if decision == "waiting":
        return _plain_result({"started": False, "approval": "waiting", "pending_id": pending_id,
                              "next_step": "The user has not answered yet. Call modelquad_handoff_wait again."})
    if decision != "approve" or arguments is None:
        return _plain_result({"started": False, "approved": False, "approval": decision,
                              "next_step": "Nothing started. Start nothing unless the user asks again."})
    # The checks run again after the answer, as in modelquad_handoff.
    refusal = _handoff_refusal(arguments["task_type"], arguments["capsule"], ctx,
                               arguments["working_directory"])
    if refusal is not None:
        return _plain_result(refusal)
    digests = arguments.pop("approved_digests", None)
    token = _approval_route.set("handoff")
    bound = _approved_digests.set(digests if type(digests) is dict else {})
    try:
        return await modelquad_delegate(ctx=ctx, **arguments)
    finally:
        _approved_digests.reset(bound)
        _approval_route.reset(token)


def _open_attempts_for_activity(
    *, limit: int, provider: str | None, task_type: str | None,
    workspace_key: str | None = None,
) -> list[dict[str, Any]]:
    """Open and refused delegation attempts, filtered the way the feed is.

    `fetch_activity` filters in SQL; delegation attempts are few and are
    filtered here instead of growing a second query surface. The provider
    filter checks both the requested and the routed provider, because an
    attempt that never resolved has only the former.
    """

    attempts = service_history.fetch_open_delegation_attempts(
        service_db.DEFAULT_DB_PATH, limit=limit, workspace_key=workspace_key
    )
    if task_type is not None:
        attempts = [row for row in attempts if row.get("task_type") == task_type]
    if provider is not None:
        attempts = [
            row
            for row in attempts
            if provider in {row.get("routed_provider"), row.get("requested_provider")}
        ]
    return attempts


#: Stored replay evidence, not display fields. A routing row's
#: ``evidence_json`` is the router's full input snapshot, including each
#: provider's raw quota metadata (the Codex plan type among it).
_UNDISPLAYED_ACTIVITY_FIELDS = ("evidence_json", "outcome_evidence_json")


def _activity_display(entry: dict[str, Any]) -> dict[str, Any]:
    """One history row as an MCP client may see it."""

    return {key: value for key, value in entry.items()
            if key not in _UNDISPLAYED_ACTIVITY_FIELDS}


@server.tool(title="Recent ModelQuad activity", annotations=READ_ONLY)
def modelquad_activity(
    limit: int = 50,
    provider: str | None = None,
    task_type: str | None = None,
    project_directory: str | None = None,
) -> dict:
    """Return persisted routing and execution activity, compact by default.

    Each entry is a small summary (id, timestamp, task_type, kind, provider,
    model, status, reason, duration, host, invocation_mode, ...) -- raw
    worker stdout/stderr is deliberately stripped out, since a single
    execution's metadata can embed 1MB+ of it. Use
    `modelquad_activity_detail(kind, id)` to fetch one specific entry's
    full record when actually debugging a run.

    With `project_directory`, only that project's entries are returned: the
    ones whose delegation was pointed at that directory. Without it, every
    project's history is returned.

    `presentation` is the same history in the vocabulary a person reads --
    what ran, where it came from, what ran it, and why -- assembled by
    `mcp/presentation.py`. It carries no decision id, no provider scores and
    no storage vocabulary, and it includes the delegation attempts that no
    execution row accounts for (one still open, one refused), because a
    handoff that started and never came back IS evidence and must not be
    rounded up or hidden. `activity` itself is unchanged: it stays the
    complete machine-readable record.
    """
    key = None
    if project_directory is not None:
        from ..adapters.pending_reminder import project_key
        key = project_key(project_directory)
        if key is None:
            return {"error": "project_scope_invalid"}
    entries = service_history.fetch_activity(
        service_db.DEFAULT_DB_PATH,
        limit=limit,
        provider=provider,
        task_type=task_type,
        workspace_key=key,
    )
    entries = [_activity_display(entry) for entry in entries]
    payload: dict[str, Any] = {"activity": entries}
    payload["presentation"] = build_activity_feed(
        entries,
        attempts=_open_attempts_for_activity(limit=limit, provider=provider, task_type=task_type,
                                             workspace_key=key),
        decision_sources=service_history.fetch_decision_sources(
            service_db.DEFAULT_DB_PATH, [entry.get("decision_id") for entry in entries]
        ),
        limit=limit,
    )
    return payload


@server.tool(title="One activity record", annotations=READ_ONLY)
def modelquad_activity_detail(kind: str, id: int, project_directory: str | None = None) -> dict:
    """Return the full record for one routing or execution entry.

    `kind` is "routing" or "execution" (entries from `modelquad_activity`,
    raw worker stdout/stderr included). With `project_directory`, an entry
    from another project answers `not_found`.
    """
    workspace_key = None
    if project_directory is not None:
        from ..adapters.pending_reminder import project_key
        workspace_key = project_key(project_directory)
        if workspace_key is None:
            return {"error": "project_scope_invalid"}
    entry = service_history.fetch_activity_detail(service_db.DEFAULT_DB_PATH, kind, id,
                                                  workspace_key=workspace_key)
    if entry is None:
        if workspace_key is not None:
            return {"error": "not_found"}
        raise ValueError(f"No {kind} entry with id {id}")
    return _activity_display(entry)


#: What every recovery answer says about the two clocks, once, in the same
#: words. `timeout` bounds the WORKER; how long the calling host stays
#: attached to the tool call is the host's own setting, which ModelQuad
#: deliberately never reads (`tests/test_rel1_caller_abandonment.py`). A
#: worker budget larger than the caller's patience is legitimate -- it simply
#: means the result is collected here rather than returned inline.
_CLOCKS_STATEMENT: dict[str, str] = {
    "worker_budget": (
        "modelquad_delegate(timeout=...) is the worker's execution budget: how long the "
        "delegated worker may run before ModelQuad stops it."
    ),
    "caller_patience": (
        "How long the calling host waits for the tool call is the HOST's own setting. "
        "ModelQuad does not set it, does not read it, and cannot extend it."
    ),
    "when_they_differ": (
        "They are different clocks. When the caller's patience runs out first the tool call "
        "fails for the caller while the worker keeps running under its own budget -- that is "
        "the caller leaving, not the worker failing. Ask modelquad_delegation_status with the "
        "same task_type, working_directory and prompt to find out what actually became of it."
    ),
}


def _recovery_view(row: dict[str, Any]) -> dict[str, Any]:
    """One attempt, as the caller that lost it needs to read it.

    The worker liveness handed to the lifecycle is the PID probe's answer and
    only that: `annotate_attempt_liveness` derives `liveness` from an age
    fallback when a row carries no pid, and an attempt that is merely young is
    not a running worker. A pid that could not be probed stays None -- unknown,
    never "not alive".
    """

    worker_alive: bool | None = None
    # Falsy, deliberately, and it must stay falsy: `annotate_attempt_liveness`
    # gates its pid probe the same way, so a row with no pid -- or a 0, which
    # is not a worker -- got its `liveness` from the row's AGE. Reading that as
    # a probe would turn a merely-young attempt into a claimed live worker.
    if row.get("worker_pid"):
        worker_alive = {"in_flight": True, "abandoned": False}.get(str(row.get("liveness")))
    outlook = derive_recovery_outlook(facts_from_attempt(row, worker_alive=worker_alive))
    execution = row.get("execution")
    return {
        "attempt_id": row.get("id"),
        "decision_id": row.get("decision_id"),
        "task_type": row.get("task_type"),
        # How this attempt's own workspace stands to the one that was asked
        # about -- "same", "inside" it, or "contains" it -- so a reader is not
        # left assuming the attempt is assigned the exact directory they named.
        # The PATH is deliberately absent: the row stores hashes,
        # because `PRIVACY.md` promises the history keeps a project directory's
        # name and never its full path. Only a workspace question sets this.
        "workspace_relation": row.get("workspace_relation"),
        # What this row contributes to that question's verdict, and why. Absent
        # on a row that is genuinely settled.
        "workspace_hazard": row.get("workspace_hazard"),
        "workspace_hazard_reason": row.get("workspace_hazard_reason"),
        "created_at": row.get("created_at"),
        "resolved_at": row.get("resolved_at"),
        "requested_provider": row.get("requested_provider"),
        "routed_provider": row.get("routed_provider"),
        "state": outlook.state,
        "state_reason": outlook.reason,
        "terminal": outlook.terminal,
        "worker_may_be_running": outlook.worker_may_be_running,
        "safe_to_retry": outlook.safe_to_retry,
        "guidance": outlook.guidance,
        "caller_abandoned": bool(row.get("caller_abandoned")),
        "caller_abandoned_at": row.get("caller_abandoned_at"),
        "caller_abandonment_reason": row.get("caller_abandonment_reason"),
        "caller_waiting": bool(row.get("caller_waiting")),
        "worker": {"pid": row.get("worker_pid"), "liveness": row.get("liveness")},
        "detail": row.get("detail"),
        # The worker's own terminal record, or an honest None while there is
        # none. Compact by the same rule modelquad_activity follows; the full
        # record is one modelquad_activity_detail away and the id to ask with
        # is right here.
        "result": execution,
        "result_detail": (
            {"kind": "execution", "id": execution["id"]} if execution else None
        ),
    }


@server.tool(title="What became of a Codex request", annotations=READ_ONLY)
def modelquad_delegation_status(
    task_type: str | None = None,
    working_directory: str | None = None,
    prompt: str | None = None,
    attempt_id: int | None = None,
    decision_id: str | None = None,
    limit: int = 5,
) -> dict:
    """What became of a delegation, for a caller that stopped waiting for it.

    Read-only. Nothing here starts, stops, retries or reconciles anything.

    A tool call that fails for the CALLER -- the calling host's own patience
    running out, the connection dropping -- says nothing whatever about the
    WORKER, which keeps running under the budget `modelquad_delegate(timeout=...)`
    gave it and records its own outcome when it ends. This is where that
    outcome is collected. Never infer a delegation's fate from files left in
    a workspace; ask here.

    Name the delegation with ANY of:

    - `task_type` + `working_directory` + `prompt` -- the same three arguments
      you delegated with, which you still hold after a failed tool call. All
      three are needed together; they identify the logical request.
    - `attempt_id` -- from a duplicate refusal, or from a previous answer here.
    - `decision_id` -- the id `modelquad_route` returned, if you passed it.

    Or ask about a DIRECTORY instead of a delegation: pass `working_directory`
    ALONE and the answer's `workspace` field says whether any delegation may
    still be writing there -- `may_be_mutating`, `unknown`, or
    `no_delegation_in_flight`. Ask that before running tests, reviewing a
    diff, or committing in a workspace ModelQuad has delegated into: a worker
    whose caller stopped waiting keeps running under the budget it was given,
    and a gate that straddles its writes belongs to no tree state at all.
    `unknown` is not a "no". ModelQuad can only speak for
    delegations it started -- it says nothing about editors or other agents.

    Each match reports `state` (one of: running, unattended, delegating,
    completed, failed, blocked, partial, stopped, unknown),
    `safe_to_retry`, `guidance`, and `result` -- the worker's terminal record
    once it has one. `unattended` means the worker is still running and
    nobody is waiting for it; it is NOT a failure. `safe_to_retry` is False
    while a worker of that attempt may still be running, and null when that
    could not be established -- neither is a "yes", because starting a second
    worker on the same workspace is exactly what this answer prevents.
    Unknown stays unknown: an identity that matches nothing returns no
    attempts, which means this state store has no record of it, not that the
    work failed.
    """

    # A lone `working_directory` is the WORKSPACE question, not half of a
    # malformed request identity. It is the only identity a validation run, a
    # review or a commit reliably still holds -- the prompt left with the
    # result that never arrived -- so it has to be a legal question rather
    # than an error message telling the asker to produce what it lost.
    workspace_only = (
        working_directory is not None
        and task_type is None
        and prompt is None
        and attempt_id is None
        and decision_id is None
    )
    identity_given = [
        name
        for name, value in (
            ("attempt_id", attempt_id),
            ("decision_id", decision_id),
            ("workspace" if workspace_only else "request", task_type or working_directory or prompt),
        )
        if value is not None
    ]
    request_key: str | None = None
    with anticipated_failure():
        request_parts = (task_type, working_directory, prompt)
        if not workspace_only and any(part is not None for part in request_parts):
            if not all(part is not None for part in request_parts):
                raise ValueError(
                    "task_type, working_directory and prompt identify a request only "
                    "together; pass all three, or pass working_directory alone to ask "
                    "whether anything is still writing to that workspace, or use "
                    "attempt_id or decision_id instead"
                )
            request_key = service_history.request_fingerprint(
                str(task_type), str(working_directory), str(prompt)
            )
        if not identity_given:
            raise ValueError(
                "name the delegation to look up: task_type + working_directory + prompt "
                "(the arguments you delegated with), or attempt_id, or decision_id -- or "
                "working_directory alone to ask about a workspace"
            )

    # Asked whenever a workspace was named at all, including alongside a full
    # request identity: a caller recovering one delegation is standing in that
    # directory, and "did anything else of mine survive in here?" is the same
    # question with the same consequences.
    workspace: dict[str, Any] | None = None
    if working_directory is not None:
        workspace = service_history.workspace_delegation_liveness(
            service_db.DEFAULT_DB_PATH,
            working_directory,
            limit=max(1, min(int(limit), 50)),
        )
        # One attempt shape in this tool's whole answer, so a reader cannot be
        # handed a raw history row here and a recovery view three lines above.
        workspace["attempts"] = [_recovery_view(row) for row in workspace["attempts"]]

    rows = (
        []
        if workspace_only
        else service_history.lookup_delegation_attempts(
            service_db.DEFAULT_DB_PATH,
            attempt_id=attempt_id,
            decision_id=decision_id,
            request_key=request_key,
            limit=max(1, min(int(limit), 50)),
        )
    )
    return {
        "query": {
            "attempt_id": attempt_id,
            "decision_id": decision_id,
            "request_key": request_key,
            "identified_by": identity_given,
        },
        # For a workspace question this reports whether the workspace answer
        # matched anything. It is NOT the safety answer -- `workspace.verdict`
        # is -- because a store that could not be read also matches nothing,
        # and `found: false` must never be mistaken for "nothing is running".
        "found": bool(workspace["attempts"]) if workspace_only else bool(rows),
        "attempts": [_recovery_view(row) for row in rows],
        "workspace": workspace,
        "clocks": _CLOCKS_STATEMENT,
    }


@server.tool(title="ModelQuad health check", annotations=READ_ONLY)
def modelquad_doctor(ctx: Context | None = None) -> dict:
    """Return the same diagnostic report as the CLI doctor command, plus how this
    session's Claude Code asks before a handoff."""
    config = load_config(CONFIG_PATH)
    report = run_doctor(
        config,
        usage_directory=Path(USAGE_DIRECTORY) if USAGE_DIRECTORY else None,
    ).to_dict()
    if _desktop_card_approves(ctx):
        approval = "approval_card"  # the desktop app: Claude Code's own prompt is the answer
    elif _shows_handoff_window(ctx):
        approval = "window"  # the handoff window, with Approve / Decline
    else:
        approval = "form"
    report["this_session"] = {"handoff_window_shown": approval == "window", "handoff_approval": approval,
                              "client_renders_mcp_apps": _client_shows_apps(ctx)}
    return report


@server.tool(title="Refresh quota readings")
def modelquad_usage_refresh(
    force: bool = False,
) -> dict:
    """Ask every provider for the freshest usage it will give us.

    Costs no model tokens: Codex answers through its own app-server, and
    Claude's reading is the last one Claude Code pushed to ModelQuad's
    status line command. Display only: a refresh never starts a job.

    Providers refresh concurrently and independently, so one failing never
    withholds another's result, and a failed probe keeps the last known-good
    reading rather than blanking it. Repeated calls are debounced across
    processes, so a UI that asks twice does not probe twice.

    Each result separates `observed_at` -- when the provider actually made
    the reading, which is the only thing that means the quota got newer --
    from `collected_at`, which is merely when we last looked.
    """

    from ..providers.base import DEFAULT_USAGE_DIRECTORY
    from ..usage.refresh import refresh_all

    directory = Path(USAGE_DIRECTORY) if USAGE_DIRECTORY else DEFAULT_USAGE_DIRECTORY
    results = refresh_all(directory, force=force)
    refreshed = {name: result.to_dict() for name, result in results.items()}
    return {
        "refreshed": refreshed,
        "any_newer_observation": any(item["newer_observation"] for item in refreshed.values()),
    }


# All job tools pass through WorkerGuardMCPServer, like the existing surface.
from . import jobs_tools
jobs_tools.register(server)

def _start_background_quota_refresh() -> None:
    """Keep the Codex quota cache fresh for the status line; display only.

    A worker session never refreshes: it must not reach provider state. See
    ``usage/background.py`` for why this lives in the MCP server.
    """
    try:
        if is_worker(service_db.DEFAULT_DB_PATH):
            return
        from ..usage.background import start_codex_refresh
        start_codex_refresh(USAGE_DIRECTORY or DEFAULT_USAGE_DIRECTORY)
    except Exception:
        logger.warning('background quota refresh unavailable')


def main() -> None:
    """Run the MCP server over its default stdio transport."""
    netpolicy.install_loopback_guard()
    try:
        if not is_worker(service_db.DEFAULT_DB_PATH):
            from modelquad.jobs.recovery import schedule_recovery
            schedule_recovery(service_db.DEFAULT_DB_PATH)
    except Exception:
        logger.warning('job recovery startup unavailable')
    _start_background_quota_refresh()
    server.run()


if __name__ == "__main__":
    main()
