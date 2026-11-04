# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Host job handles and repeatable collection; no application or path inputs."""

from datetime import datetime, timezone
import logging
import sqlite3
import time

from modelquad.execution.base import DESCENDANTS_RECORDED, worker_confirmed_dead
from modelquad.jobs.handle import job_status, stop_job
from modelquad.jobs.recovery import schedule_recovery
from modelquad.jobs.store import (
    PendingOutputCapacityUnavailable, PendingOutputTemporarilyUnavailable, default_store,
)
from modelquad.service import db, history
from .worker_guard import WORKER_REFUSAL, is_worker


def _guard():
    if is_worker(db.DEFAULT_DB_PATH):
        # Shared service readers do not require the optional MCP SDK. Only
        # the guarded MCP entry points construct its protocol exception.
        from mcp.server.mcpserver.exceptions import ToolError

        raise ToolError(WORKER_REFUSAL)


def _scoped_job(job_id: str, project_directory: str):
    """Return a job only when its attempt belongs to this exact workspace."""
    from modelquad.adapters.pending_reminder import project_key
    key = project_key(project_directory)
    if key is None:
        return None, {"job_id": job_id, "status": "not_found", "reason": "project_scope_invalid"}
    try:
        row = history.read_job(db.DEFAULT_DB_PATH, job_id)
        if row is None:
            return None, {"job_id": job_id, "status": "not_found"}
        attempt = history.read_job_attempt(db.DEFAULT_DB_PATH, row["attempt_id"])
    except (OSError, sqlite3.Error, history.LaunchStateUnavailable):
        return None, {"job_id": job_id, "status": "unknown", "reason": "project_scope_unavailable"}
    if attempt is None or attempt.get("workspace_key") != key:
        return None, {"job_id": job_id, "status": "not_found"}
    return row, None


def _job_refused(record, *, result_available=False, output_state=None, db_path=None):
    db_path = db.DEFAULT_DB_PATH if db_path is None else db_path
    if record is None:
        return {}
    try:
        attempt = history.read_job_attempt(db_path, record['attempt_id'])
    except (OSError, sqlite3.Error, history.LaunchStateUnavailable):
        return {}  # Optional refusal evidence cannot prevent output collection.
    fields = ({'refused': True, 'reason': attempt['detail']}
              if attempt is not None and attempt['status'] == 'refused' else {})
    return fields


def _worker_fate(output, record, *, history_readable=True):
    """Publish bounded facts, never process identities or executor metadata."""
    if output is None:
        return {'tree_confirmed_dead': None,
                'descendant_uncertainty': {'census': 'not_observed', 'listed': 0,
                                           'more_unlisted': False}}
    descendants = output.descendants
    listed = descendants.get('descendants') if descendants is not None else None
    observed = isinstance(listed, (tuple, list))
    count = len(listed) if observed else 0
    complete = (descendants is not None and descendants.get('enumerated') is True
                and descendants.get('truncated') is False and observed)
    summary = {
        'census': ('not_observed' if descendants is None else
                   'complete' if complete else 'incomplete'),
        'listed': min(count, DESCENDANTS_RECORDED),
        'more_unlisted': bool(descendants is not None and (
            descendants.get('truncated') or count > DESCENDANTS_RECORDED)),
    }
    confirmed = (output.tree_confirmed_dead is True
                 and worker_confirmed_dead({'worker_termination': dict(output.termination or {})})
                 and (descendants is None or (complete and count == 0))
                 and record is not None
                 and 'recovery_decision' in record and record['recovery_decision'] is None
                 and 'launch_limitation' in record and record['launch_limitation'] is None)
    fate = (None if not history_readable else True if confirmed else
            False if output.tree_confirmed_dead is False else None)
    return {'tree_confirmed_dead': fate, 'descendant_uncertainty': summary}


#: The most answer text a direct delegate reply carries inline. Collected
#: background answers keep their own retained bound (jobs.store), and Claude
#: Code saves a very long tool result to a file instead of showing it whole.
MAX_SHOWN_OUTPUT_CHARS = 20_000


def collect_job_response(job_id, *, record=None):
    """Terminal shortcut shares precisely the collect contract, without ack."""
    now = datetime.now(timezone.utc)
    try:
        store = default_store()
    except PendingOutputTemporarilyUnavailable as error:
        return {"error": error.label}
    payload = collect_job_from_store(job_id, db_path=db.DEFAULT_DB_PATH, store=store,
                                    now=now, record=record)
    if 'receipt' not in payload:
        return payload
    # The retained answer is collected before the optional transient overlay.
    # Restart/expiry/transport failure never claims that omissions were absent.
    from modelquad.jobs.service_client import transient_diagnostics
    items = transient_diagnostics(db.DEFAULT_DB_PATH, job_id)
    payload['host_diagnostics_status'] = 'unavailable' if items is None else 'available'
    if items:
        payload['host_diagnostics'] = items
        payload['host_diagnostics_stage'] = 'worker_authoritative_reread'
    return payload


def _with_needed_files(response, project_directory):
    """Add the files a collected answer says Codex needed, as for a direct reply."""
    from modelquad.execution import noshell

    needed = noshell.needed_files(response.get('output'), project_directory)
    if needed:
        response['files_needed'] = needed
        response['files_needed_note'] = noshell.NEEDED_FILES_NOTE
    return response


def read_job_summaries_from_store(*, store, now, since):
    """Read content-free public seam summaries from an explicit store.

    Keep pending/outcome access in this existing authorized consumer. No
    publication, acknowledgement, history/log/research write or executor entry.
    Store read failures propagate so the caller reports unavailable coverage.
    """
    return store.pending(now), store.outcomes(now, since=since)


def collect_job_from_store(job_id, *, db_path, store, now, record=None,
                           job_reader=None, optional_overlays=True):
    """Shared W04 collection with explicit state; no guard or executor entry.

    HTTP uses its own bounded readers and overlays. Public MCP callers retain
    their guard and historical response, including refusal/catalog overlays.
    Guard evidence is always read AFTER collection, including shortcut calls.
    """
    try:
        collected = store.collect(job_id, now)
    except PendingOutputTemporarilyUnavailable as error:
        return {"error": error.label}
    # A shortcut's earlier status row can predate a recovery downgrade. Read
    # the current guard facts after collecting, even when a row was supplied.
    history_readable = True
    try:
        fate_record = (job_reader or history.read_job)(db_path, job_id)
    except (OSError, sqlite3.Error, history.LaunchStateUnavailable):
        history_readable = False
        fate_record = record = None  # A durable answer survives a history read failure.
    # Preserve the caller's lifecycle observation (including terminal wait
    # shortcuts); only the fate guards require a fresh history read.
    record = record if record is not None else fate_record
    def overlays(**fields):
        return _job_refused(record, db_path=db_path, **fields) if optional_overlays else {}
    if collected is None:
        try:
            state = store.state(job_id, now)
        except PendingOutputTemporarilyUnavailable as error:
            return {"error": error.label}
        return {"job_id": job_id, "job_status": record['status'] if record else 'not_found',
                "output_state": state, **overlays(output_state=state),
                **_worker_fate(None, fate_record)}
    output = collected.output
    return {
        **overlays(result_available=True, output_state='pending'),
        "job_id": job_id, "receipt": collected.receipt,
        "job_status": record['status'] if record else output.job_status.value,
        "worker_claim": output.worker_claim.value, "output": output.output_text,
        "changed_files": None,
        "partial": output.partial, "expires_at": collected.expires_at.isoformat(),
        "expiring_soon": collected.expiring_soon,
        "output_truncated": collected.output_truncated,
        "original_bytes": collected.original_bytes,
        **_worker_fate(output, fate_record, history_readable=history_readable),
    }


def modelquad_job_status(job_id: str, project_directory: str) -> dict:
    """Poll one stable handle; status is lifecycle evidence, not a worker claim."""
    _guard()
    row, refused = _scoped_job(job_id, project_directory)
    if refused:
        return refused
    schedule_recovery(db.DEFAULT_DB_PATH, rate_limited=True)
    row = job_status(job_id, db_path=db.DEFAULT_DB_PATH) or row
    return {"job_id": job_id, "status": row['status'],
            "terminal": row['ended_at'] is not None,
            "runner_liveness": row['runner_liveness']}


def modelquad_job_collect(job_id: str, project_directory: str) -> dict:
    """Repeatably read the extracted answer; never apply or acknowledge it.

    Retained output returns job_id, receipt, job_status, worker_claim, output,
    partial, expires_at and expiring_soon, output_truncated, original_bytes,
    tree_confirmed_dead and descendant_uncertainty.
    output_truncated=true means the answer's tail is lost; worker_claim and
    producer partial are preserved. tree_confirmed_dead other than true means
    a Codex process may still be running (it cannot write to the project,
    but it may still use Codex usage). No success, exit_code
    or executor metadata is claimed. Policy refusals also carry refused=True
    and reason from the attempt. Acknowledge separately after use.
    Second-opinion jobs add a content-free review catalog with current Host
    dispositions, catalog_status, result_available and output_state beside the
    unchanged output. A catalog failure is unknown; it never blocks collection.
    """
    _guard()
    _row, refused = _scoped_job(job_id, project_directory)
    if refused:
        return refused
    return _with_needed_files(collect_job_response(job_id), project_directory)


def modelquad_job_wait(job_id: str, project_directory: str, wait_seconds: int | None = None) -> dict:
    """Wait once for a retained answer; never apply, stop or acknowledge it.

    Defaults to 50 seconds, clamped to 1..540. A terminal job returns the
    collect contract. output_truncated=true means the answer's tail is lost;
    tree_confirmed_dead other than true means a Codex process may still be
    running (it cannot write to the project). While terminal is false, wait again with this tool.
    Second-opinion output and catalog registration are separate operations:
    a terminal wait can return catalog_status=unknown before registration.
    """
    _guard()
    _initial, refused = _scoped_job(job_id, project_directory)
    if refused:
        return refused
    requested = 50 if wait_seconds is None else wait_seconds
    bounded = min(540, max(1, requested))
    started = time.monotonic()
    deadline = started + bounded
    # The same crash recovery the status tool schedules, so a runner that
    # died mid-job is noticed while the Host only waits (rate limited).
    next_recovery = started
    while True:
        if time.monotonic() >= next_recovery:
            schedule_recovery(db.DEFAULT_DB_PATH, rate_limited=True)
            next_recovery = time.monotonic() + 30.0
        try:
            row = job_status(job_id, db_path=db.DEFAULT_DB_PATH)
        except (OSError, sqlite3.Error, history.LaunchStateUnavailable):
            response = collect_job_response(job_id)
            if 'receipt' in response or 'error' in response:
                return _with_needed_files(response, project_directory)
            return {**response, 'status': 'unknown', 'job_status': 'unknown',
                    'terminal': False, 'wait_again_with': 'modelquad_job_wait'}
        if row is None:
            return {"job_id": job_id, "status": "not_found"}
        if row['ended_at'] is not None or row['status'] not in {'starting', 'running'}:
            return _with_needed_files(collect_job_response(job_id, record=row), project_directory)
        now = time.monotonic()
        if now >= deadline:
            return {"job_id": job_id, "status": row['status'], "terminal": False,
                    "runner_liveness": row['runner_liveness'],
                    "waited_seconds": now - started,
                    "wait_again_with": "modelquad_job_wait",
                    **({"wait_clamped": True} if bounded != requested else {})}
        time.sleep(min(0.5, deadline - now))


def modelquad_job_acknowledge(job_id: str, receipt: str, project_directory: str) -> dict:
    """A matching receipt removes retained content, after the Host used it."""
    _guard()
    _row, refused = _scoped_job(job_id, project_directory)
    if refused:
        return refused
    try:
        return {"job_id": job_id, "acknowledged": default_store().acknowledge(
            job_id, receipt, datetime.now(timezone.utc))}
    except (PendingOutputCapacityUnavailable, PendingOutputTemporarilyUnavailable) as error:
        return {"error": error.label}


def modelquad_job_stop(job_id: str, project_directory: str) -> dict:
    """Request stopping this job; acceptance does not assert whole-tree death."""
    _guard()
    _row, refused = _scoped_job(job_id, project_directory)
    if refused:
        return refused
    verdict = stop_job(job_id, db_path=db.DEFAULT_DB_PATH)
    return {"job_id": job_id, "stop_requested": verdict == 'accepted',
            **({"reason": verdict} if verdict != 'accepted' else {})}


def modelquad_job_list_pending(project_directory: str) -> dict:
    """List retained work; an explicit absolute folder scopes terminal handles.

    No argument preserves the legacy all-project retained/open listing. Scoped
    reads use exact workspace identity and the authenticated summary service;
    unavailable evidence does not become an empty list. Collection can race
    expiry/acknowledgment; refresh and report that race before using a handle.
    """
    _guard()
    from modelquad.adapters.pending_reminder import project_key
    from modelquad.jobs.service_client import read_pending_summary
    key = project_key(project_directory)
    if key is None:
        return {'error': 'pending_scope_invalid'}
    try:
        return read_pending_summary(key, deadline=time.monotonic() + 1.5)
    except Exception:
        pass
    # Service-free fallback: only local rows whose attempt has this workspace.
    try:
        # The open-handle rows are content-free; the attempt link is read per job.
        running = [row for row in history.list_active_jobs(db.DEFAULT_DB_PATH)
                   if (job := history.read_job(db.DEFAULT_DB_PATH, row['job_id']))
                   and (attempt := history.read_job_attempt(db.DEFAULT_DB_PATH, job['attempt_id']))
                   and attempt.get('workspace_key') == key]
    except (OSError, sqlite3.Error, history.LaunchStateUnavailable):
        running = []
    try:
        pending = default_store().pending(datetime.now(timezone.utc))
    except PendingOutputTemporarilyUnavailable as error:
        return {"error": error.label}
    return {"jobs": [
        {"job_id": entry.job.job_id, "job_status": entry.job_status.value,
         "expires_at": entry.expires_at.isoformat(), "expiring_soon": entry.expiring_soon}
        for entry in pending
        if (row := history.read_job(db.DEFAULT_DB_PATH, entry.job.job_id))
        and (attempt := history.read_job_attempt(db.DEFAULT_DB_PATH, row['attempt_id']))
        and attempt.get('workspace_key') == key
    ], "running": running}



def output_state(job_id, now=None):
    """The retained output's state for a job, for consumers outside this module.

    Only this module and the store touch the pending-output store (the
    privacy boundary); the second-opinion tools read the state through here.
    """
    return default_store().state(job_id, now or datetime.now(timezone.utc))


#: Display titles; collect and list only read the store. Status and wait also
#: schedule crash recovery, so they are not marked read-only.
_TITLES = {
    "modelquad_job_status": "Background job status",
    "modelquad_job_collect": "Read a background answer",
    "modelquad_job_wait": "Wait for a background answer",
    "modelquad_job_acknowledge": "Remove a used background answer",
    "modelquad_job_stop": "Stop a background job",
    "modelquad_job_list_pending": "List waiting background answers",
}
_READ_ONLY = {"modelquad_job_collect", "modelquad_job_list_pending"}


def register(server):
    from mcp.types import ToolAnnotations

    for tool in (modelquad_job_status, modelquad_job_collect, modelquad_job_wait,
                 modelquad_job_acknowledge, modelquad_job_stop, modelquad_job_list_pending):
        name = tool.__name__
        annotations = (ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
                       if name in _READ_ONLY else None)
        server.tool(title=_TITLES[name], annotations=annotations)(tool)
