# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Authenticated, exact-job management. Replies and bodies have closed shapes."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from modelquad import procutil
from modelquad.jobs.fate import Proof
from . import fate_history as store
from .api import state_identity_tag

router = APIRouter(prefix='/api/jobs')
BODY_LIMIT = 256


def _utc_now():
    return datetime.now(timezone.utc)


def _error(reason, status=409):
    return JSONResponse({'detail': reason, 'code': reason}, status_code=status)


def _target(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError):
        return False


def _public_release(row):
    if row is None:
        return None
    return {key: row[key] for key in ('job_id', 'attempt_id', 'decision_id', 'observed_at',
                                    'released_at', 'reason', 'surface')}


def _get(request, job_id):
    try:
        with store.connection(request.app.state.db_path) as conn:
            observed = store.linked(conn, job_id)
            released = store.validated_release(conn, job_id)
            blocker = released is None and store.eligible(conn, job_id)
        processes = procutil.live_codex_processes()
        return {'job_id': job_id, 'created_at': observed.job['created_at'],
                'blocker': blocker, 'release': _public_release(released),
                'live_codex': asdict(processes)}
    except store.FateError as error:
        return _error(str(error), 404 if str(error) == 'fate_not_found' else 409)


@router.get('/{job_id}/fate')
def get_fate(request: Request, job_id: str):
    if request.scope.get('modelquad_authenticated') is not True:
        return _error('service_token_missing', 403)
    if not _target(job_id):
        return _error('fate_request_invalid', 400)
    return _get(request, job_id)


def _release(request, job_id, surface):
    try:
        observed = store.snapshot(request.app.state.db_path, job_id)
        now = _utc_now()
        actor = 'installation:' + state_identity_tag(request.app.state.db_path)
        row = store.release(request.app.state.db_path, observed,
                            proof=Proof('owner_released_unknown_fate', now), now=now,
                            actor=actor, authentication_method='installation_token', surface=surface)
        return {'job_id': job_id, 'release': _public_release(row)}
    except store.FateError as error:
        return _error(str(error), 404 if str(error) == 'fate_not_found' else 409)


@router.post('/{job_id}/release-fate')
async def release_fate(request: Request, job_id: str):
    if request.scope.get('modelquad_authenticated') is not True:
        return _error('service_token_missing', 403)
    if not _target(job_id):
        return _error('fate_request_invalid', 400)
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > BODY_LIMIT:
                return _error('fate_request_invalid', 400)
            raw.extend(chunk)
        # Reject duplicate keys, extras and coercions without reflecting input.
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        body = json.loads(raw, object_pairs_hook=unique)
        if (type(body) is not dict or set(body) != {'acknowledge_unknown_worker_fate', 'surface'}
                or body['acknowledge_unknown_worker_fate'] is not True
                or type(body['surface']) is not str or body['surface'] not in ('ui', 'cli')):
            return _error('fate_request_invalid', 400)
    except (ValueError, UnicodeError):
        return _error('fate_request_invalid', 400)
    return await run_in_threadpool(_release, request, job_id, body['surface'])
