# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Authenticated service-owned launches of private, prepared delegations.

The trust middleware (Host, Origin, install token) runs before this router.
A missing token is its refusal. The route additionally excludes every browser
request, including same-origin pages holding a valid token. There is no MCP
spec digest: any token holder is the same Windows user who can already run the
CLI or write the DB, and auth.py makes no hostile-same-account guarantee.
Only fixed labels and the validated job id may enter diagnostics; contents,
prompts and paths never do. The validated in-memory spec is rewritten locally.
Second-opinion/delegation fallback is limited by jobs.launch_answer.refusal_label
to transport
ambiguity, service absence/unreachability, service_contained and job_runner_disabled.
It runs in-process only after winning the ownership CAS; other refusals are terminal.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from orchestrator import procutil
from orchestrator.jobs import handle
from orchestrator.jobs.seam import JobRef
from . import auth, history
from .db import connect_and_init

router = APIRouter(prefix='/api/jobs')
SPEC_MAX_BYTES = 8 * 1024 * 1024
BODY_MAX_BYTES = 4096
STARTUP_EVIDENCE_SECONDS = 2.0
# tempfile's _RandomNameSequence uses lowercase ASCII, digits and underscore.
SPEC_NAME = re.compile(r'job-[a-z0-9_]{8}\.json\Z')
_logger = logging.getLogger(__name__)
_state_lock = threading.Lock()


@dataclass
class _Admission:
    lock: threading.Lock = field(default_factory=threading.Lock)
    launches: deque = field(default_factory=deque)


def _refuse(status: int, reason: str, **facts) -> JSONResponse:
    return JSONResponse({'launched': False, 'reason': reason, **facts}, status_code=status)


def _reparse(info) -> bool:
    return bool(getattr(info, 'st_file_attributes', 0) & 0x400) or stat.S_ISLNK(info.st_mode)


_ADMINISTRATORS_SID = 'S-1-5-32-544'


def _read_private_spec(path: Path) -> bytes:
    """One open, bounded read; inspect attributes, size and DACL on that handle.

    describe_handle is available, so no path-based DACL check can race the
    open. The lstat identity must also agree, refusing a file swapped before
    opening. No write/delete sharing is granted while this handle is held.
    """
    import ctypes
    import msvcrt

    parent = path.parent.lstat()
    before = path.lstat()
    if (_reparse(parent) or not stat.S_ISDIR(parent.st_mode) or _reparse(before)
            or not stat.S_ISREG(before.st_mode) or before.st_size > SPEC_MAX_BYTES):
        raise ValueError('invalid_spec')
    k32, _adv = auth._win()
    native = k32.CreateFileW(str(path), 0x80000000 | 0x00020000, 0x1, None,
                             3, 0x00200000, None)
    if native in (None, auth._INVALID_HANDLE_VALUE):
        raise OSError('invalid_spec')
    fd = None
    try:
        info = auth._BY_HANDLE_FILE_INFORMATION()
        if not k32.GetFileInformationByHandle(native, ctypes.byref(info)):
            raise OSError('invalid_spec')
        size = (info.nFileSizeHigh << 32) | info.nFileSizeLow
        index = (info.nFileIndexHigh << 32) | info.nFileIndexLow
        if info.dwFileAttributes & (0x400 | 0x10) or size > SPEC_MAX_BYTES:
            raise ValueError('invalid_spec')
        # A protected DACL granting only this user (and SYSTEM), and an owner
        # that is this user or BUILTIN\Administrators. An elevated run owns the
        # files it creates as Administrators, who can take ownership of any
        # file anyway (SECURITY.md's administrator exception); any other
        # owner could grant itself access, so it is refused.
        described = auth.describe_handle(native)
        user = auth.current_user_sid()
        if (not auth.directory_is_private(described, user)
                or described.owner not in (auth.canonical_sid(user), _ADMINISTRATORS_SID)):
            raise ValueError('invalid_spec')
        fd = msvcrt.open_osfhandle(native, os.O_RDONLY)
        native = None  # fd owns the native handle from here.
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or before.st_ino != index
                or before.st_dev != opened.st_dev):
            raise ValueError('invalid_spec')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            contents = stream.read(SPEC_MAX_BYTES + 1)
        if len(contents) > SPEC_MAX_BYTES:
            raise ValueError('invalid_spec')
        return contents
    finally:
        if fd is not None:
            os.close(fd)
        if native is not None:
            k32.CloseHandle(native)


def _load_spec(db_path: Path, name) -> tuple[handle.JobSpec, Path]:
    if not isinstance(name, str) or SPEC_NAME.fullmatch(name) is None:
        raise ValueError('invalid_spec')
    directory = db_path.parent
    if _reparse(directory.lstat()):
        raise ValueError('invalid_spec')
    path = directory / name
    # Check the lexical path before resolving so a file link is never followed.
    if _reparse(path.lstat()) or path.resolve().parent != directory.resolve():
        raise ValueError('invalid_spec')
    value = json.loads(_read_private_spec(path))
    if not isinstance(value, dict):
        raise ValueError('invalid_spec')
    return handle.JobSpec(**value), path


def _launchable(db_path: Path, job_id: str, spec: handle.JobSpec) -> dict | None:
    row = history.read_job(db_path, job_id)
    delegation = spec.delegation
    # JobSpec requires argv or delegation; with empty argv, absence is unreachable.
    if (spec.argv != () or spec.env != {} or spec.sink != 'store' or delegation is None
            or Path(spec.db_path).resolve() != db_path.resolve()
            or Path(delegation.db_path).resolve() != db_path.resolve()
            or spec.attempt_id != delegation.attempt_id or spec.timeout != delegation.timeout
            or not Path(delegation.config_path).is_file()
            or not Path(delegation.working_directory).is_dir()):
        return None
    if (row is None or row['attempt_id'] != spec.attempt_id
            or row['decision_id'] != delegation.decision_id or row['status'] != 'starting'
            or any(row[key] is not None for key in ('runner_pid', 'heartbeat_at', 'ended_at', 'recovery_claim'))):
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['created_at'])).total_seconds()
    if not 0 <= age < handle.STARTUP_SECONDS - 5:
        return None
    attempt = history.read_job_attempt(db_path, spec.attempt_id)
    if (attempt is None or attempt['decision_id'] != delegation.decision_id
            or attempt['status'] != 'started' or attempt['resolved_at'] is not None):
        return None
    return row


def _admission(request: Request) -> _Admission:
    with _state_lock:
        if not hasattr(request.app.state, 'jobs_launch_admission'):
            request.app.state.jobs_launch_admission = _Admission()
        return request.app.state.jobs_launch_admission


def _diagnostics(request):
    from orchestrator.jobs.diagnostics import TransientDiagnostics
    with _state_lock:
        if not hasattr(request.app.state, 'job_transient_diagnostics'):
            request.app.state.job_transient_diagnostics = TransientDiagnostics()
        return request.app.state.job_transient_diagnostics


@router.api_route('/{job_id}/diagnostics', methods=['GET', 'POST'])
async def transient_diagnostics(request: Request, job_id: str):
    """Installation-authenticated memory channel, never a persisted answer."""
    from orchestrator.jobs.diagnostics import BODY_CAP, validate, wire
    headers = {'Cache-Control': 'no-store'}
    def reply(body, status=200):
        return JSONResponse(body, status_code=status, headers=headers)
    if (request.scope.get('modelquad_authenticated') is not True or 'origin' in request.headers
            or any(key.startswith('sec-fetch-') for key in request.headers)):
        return reply({'status': 'unavailable'}, 403)
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError
        row = await run_in_threadpool(history.read_job, request.app.state.db_path, job_id)
        if row is None:
            return reply({'status': 'unavailable'}, 404)
        if request.method == 'GET':
            items = _diagnostics(request).get(job_id)
            return reply({'status': 'unavailable'} if items is None else
                         {'status': 'available', 'diagnostics': wire(items)})
        contents = bytearray()
        async for chunk in request.stream():
            if len(contents) + len(chunk) > BODY_CAP:
                raise ValueError
            contents.extend(chunk)
        body = json.loads(contents)
        if (type(body) is not dict or set(body) != {'attempt_id', 'decision_id', 'runner_pid',
                                                  'runner_created', 'diagnostics'}
                or type(body['attempt_id']) is not int or type(body['runner_pid']) is not int
                or type(body['runner_created']) not in (float, int)
                or body['runner_pid'] <= 0 or not math.isfinite(body['runner_created']) or body['runner_created'] <= 0
                or body['attempt_id'] != row['attempt_id'] or body['decision_id'] != row['decision_id']
                or body['runner_pid'] != row['runner_pid'] or body['runner_created'] != row['runner_created']):
            raise ValueError
        created = await run_in_threadpool(procutil.process_creation_epoch, row['runner_pid'])
        if type(created) not in (float, int) or not math.isfinite(created) or abs(created - body['runner_created']) > 0.001:
            return reply({'status': 'unavailable'}, 409)
        _diagnostics(request).put(job_id, validate(body['diagnostics']))
        return reply({'status': 'available'})
    except Exception:
        return reply({'status': 'unavailable'}, 400)


def _startup_limitation(db_path: Path, ref: JobRef) -> str | None:
    """Wait briefly for the runner's atomic measurement/identity registration.

    An unreported, unreadable or overdue startup is unknown, never a safe
    observation. This read neither closes the job nor retries its launch.
    Read-only, zero-lock-wait connections keep the response budget bounded.
    """
    deadline = time.monotonic() + STARTUP_EVIDENCE_SECONDS
    while time.monotonic() < deadline:
        try:
            conn = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0)
            try:
                conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
                current = conn.execute(
                    'SELECT launch_limitation, heartbeat_at, ended_at FROM jobs '
                    'WHERE job_id=? AND attempt_id=? AND decision_id IS ?',
                    (ref.job_id, ref.attempt_id, ref.decision_id),
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            return 'unknown'
        if current is None:
            return 'unknown'
        limitation, heartbeat, ended = current
        if limitation == handle.CONTAINED_RUNNER_LIMITATION:
            return limitation
        if limitation is not None:
            return 'unknown'
        if heartbeat is not None:
            return None
        if ended is not None:
            return 'unknown'
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    return 'unknown'


def _launch(request: Request, job_id: str, body: dict) -> JSONResponse:
    db_path = Path(request.app.state.db_path)
    try:
        spec, caller_path = _load_spec(db_path, body.get('spec'))
    except Exception:
        return _refuse(400, 'invalid_spec')
    try:
        row = _launchable(db_path, job_id, spec)
    except Exception:
        row = None
    if row is None:
        return _refuse(409, 'job_not_launchable')
    admission = _admission(request)
    service_path = None
    claimed = False
    returned = False
    prior_limitation = (handle.CONTAINED_RUNNER_LIMITATION
                        if getattr(request.state, 'runner_limitation', None) == handle.CONTAINED_RUNNER_LIMITATION
                        or row.get('launch_limitation') == handle.CONTAINED_RUNNER_LIMITATION else None)
    try:
        # Admission and ownership transfer form one serialized operation.
        # The finally block covers everything after a successful claim,
        # including rate-accounting and releasing the admission lock.
        with admission.lock:
            now = time.monotonic()
            while admission.launches and admission.launches[0] <= now - 10:
                admission.launches.popleft()
            conn = connect_and_init(db_path)
            try:
                open_jobs = conn.execute('SELECT count(*) FROM jobs WHERE ended_at IS NULL AND job_id != ?',
                                         (job_id,)).fetchone()[0]
            finally:
                conn.close()
            if len(admission.launches) >= 6 or open_jobs >= 8:
                return _refuse(429, 'launch_rate_limited')
            starter_pid = os.getpid()
            starter_created = procutil.process_creation_epoch(starter_pid)
            if starter_created is None:
                return _refuse(409, 'job_not_launchable')
            claimed = history.claim_job_launch(db_path, job_id, attempt_id=spec.attempt_id,
                                               decision_id=row['decision_id'], starter_pid=starter_pid,
                                               starter_created=starter_created)
            if not claimed:
                return _refuse(409, 'job_not_launchable')
            admission.launches.append(now)
        # Every exit after a won claim either returns launch_reserved's
        # result, or fail_job_attempt closes BOTH records below.
        if getattr(request.state, 'runner_limitation', None) == handle.CONTAINED_RUNNER_LIMITATION:
            history.update_job(db_path, job_id, launch_limitation=handle.CONTAINED_RUNNER_LIMITATION)
        spec = replace(spec, db_path=str(db_path),
                       delegation=replace(spec.delegation, db_path=str(db_path)))
        service_path = handle._write_spec(spec, diagnostic_job_id=job_id)
        caller_path.unlink()
        result = handle.launch_reserved(JobRef(job_id, spec.attempt_id, row['decision_id']), service_path, spec,
                                        service_admission=True)
        response_limitation = prior_limitation or result.limitation
        if result.launched and response_limitation != handle.CONTAINED_RUNNER_LIMITATION:
            response_limitation = _startup_limitation(db_path, result.ref)
        if result.launched:
            response = JSONResponse({'launched': True, 'job_id': job_id, 'limitation': response_limitation})
        else:
            _logger.warning('runner_launch_failed %s', job_id)
            response = _refuse(500, 'runner_launch_failed', job_id=job_id, limitation=response_limitation,
                               containment_reason=result.limitation)
        returned = True
        return response
    except Exception:
        reason = 'runner_launch_failed' if claimed else 'launch_unavailable'
        _logger.warning('%s %s', reason, job_id)
        return (_refuse(500, reason, job_id=job_id, limitation=prior_limitation) if claimed
                else _refuse(503, reason, job_id=job_id))
    finally:
        if claimed and not returned:
            try:
                if service_path is not None:
                    service_path.unlink(missing_ok=True)
            finally:
                history.fail_job_attempt(db_path, job_id, detail='launch_failed')


def _service_ready(request: Request) -> JSONResponse | None:
    """Blocking admission probes, in their required order, off the event loop."""
    if not history.get_job_runner_enabled(request.app.state.db_path):
        return _refuse(409, 'job_runner_disabled')
    try:
        ok, reason = handle.service_runner_verdict()
    except Exception:
        ok, reason = False, 'containment_unknown'
    if not ok:
        return _refuse(409, 'service_contained', containment_reason=reason)
    request.state.runner_limitation = reason if reason == handle.CONTAINED_RUNNER_LIMITATION else None
    return None


@router.post('/{job_id}/launch')
async def launch_job(request: Request, job_id: str):
    """Launch a prepared job; explicit refusals normally stop client fallback.

    jobs.launch_answer.refusal_label permits the ownership CAS only for transport
    ambiguity, service absence/unreachability, service_contained, job_runner_disabled
    and job_not_launchable. Inline execution requires winning the ownership CAS.
    """
    if 'origin' in request.headers or any(key.startswith('sec-fetch-') for key in request.headers):
        return _refuse(403, 'browser_request_refused')
    refusal = await run_in_threadpool(_service_ready, request)
    if refusal is not None:
        return refusal
    try:
        identity = uuid.UUID(job_id)
        if identity.version != 4 or str(identity) != job_id:
            raise ValueError
    except ValueError:
        return _refuse(400, 'invalid_job_id')
    contents = bytearray()
    try:
        async for chunk in request.stream():
            if len(contents) + len(chunk) > BODY_MAX_BYTES:
                return _refuse(400, 'invalid_body')
            contents.extend(chunk)
        body = json.loads(contents)
        if not isinstance(body, dict):
            return _refuse(400, 'invalid_body')
    except Exception:
        return _refuse(400, 'invalid_body')
    # Sync launch is shielded by run_in_threadpool: cancellation of the HTTP
    # waiter does not cancel the handler that owns the claimed reservation.
    return await run_in_threadpool(_launch, request, job_id, body)
