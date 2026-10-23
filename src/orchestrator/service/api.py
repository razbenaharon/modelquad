# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Localhost HTTP API of the local service.

Only the routes a release client calls exist: the listener proof, health,
the background-work setting, the host's pending-output summary, and the job
routes in ``jobs_api.py`` and ``fate_api.py``. Every route except
``POST /api/listener-proof`` requires the installation secret
(``service/trust.py``). There is no execute/delegate/chat endpoint here.
"""

from __future__ import annotations

import time
import threading
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..branding import installed_version
from ..buildinfo import current_build_info
from ..jobs.seam import MAX_RETENTION
from ..jobs.store import default_store
from . import history
from .models import SettingsUpdateRequest

router = APIRouter(prefix="/api")


@router.get("/pending-output")
def get_pending_output(request: Request) -> Any:
    """Retention summaries only: never serialize an answer or receipt."""
    now = datetime.now(timezone.utc)
    if request.query_params:
        from ..adapters.pending_reminder import valid_workspace_key
        params = request.query_params
        if (set(params) != {"summary", "workspace_key"} or len(params.multi_items()) != 2
                or params.get("summary") != "host" or not valid_workspace_key(params.get("workspace_key"))):
            raise HTTPException(status_code=400, detail="pending_scope_invalid")
        try:
            return default_store(metadata_only=True).host_summary(
                request.app.state.db_path, params["workspace_key"], now=now)
        except Exception:
            raise HTTPException(status_code=503, detail="pending_summary_unavailable") from None
    try:
        store = default_store()
        entries = []
        for entry in store.pending(now):
            retained = store.collect(entry.job.job_id, now)
            if retained is None:  # Acknowledged/deleted by another caller meanwhile.
                continue
            try:
                attempt = history.read_job_attempt(request.app.state.db_path, entry.job.attempt_id)
            except (sqlite3.Error, OSError):
                attempt = None  # Optional labels do not hide a waiting answer.
            entries.append({
                "job_id": entry.job.job_id,
                "job_status": entry.job_status.value,
                "expires_at": retained.expires_at.isoformat(),
                "expiring_soon": retained.expiring_soon,
                "extended_until": retained.extended_until.isoformat() if retained.extended_until else None,
                "max_expires_at": (retained.retained_at + MAX_RETENTION).isoformat(),
                "task_label": attempt.get("task_type") if attempt else None,
                "provider": attempt.get("routed_provider") if attempt and attempt.get("routed_provider") in (
                    "codex", "claude", "claude_code") else None,
            })
        return entries
    except Exception:
        raise HTTPException(status_code=503, detail="Waiting results are unavailable. Try again.") from None


def _package_version() -> str:
    return installed_version("n/a")


def state_identity_tag(state_root: Any) -> str:
    """A stable, non-reversible id for one state store.

    Hashing rather than publishing the path keeps the identity comparable
    everywhere -- logs, diagnostics, a screenshot -- without
    carrying the user's home directory with it. The path is
    normalised first so two spellings of one directory do not read as two
    different stores.
    """

    import hashlib
    from pathlib import Path

    try:
        normalised = str(Path(str(state_root)).resolve()).casefold()
    except (OSError, ValueError):
        normalised = str(state_root).casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]


def _db_identity(request, default_path) -> str | None:
    """A hash tag of the DB this service serves; None when it is not known.

    Health must never fail over it: before startup sets ``app.state.db_path``
    (or in a test client without lifespan) the identity is simply absent, and
    an MCP client then treats the service as foreign (it never sends the
    secret to an unproven service).
    """
    from pathlib import Path

    try:
        db_path = getattr(request.app.state, "db_path", None) if request is not None else default_path
        return state_identity_tag(Path(db_path).resolve()) if db_path else None
    except Exception:  # noqa: BLE001 -- identity is never worth failing health over
        return None


@router.get("/health")
def get_health(request: Request = None) -> dict[str, Any]:
    """Health plus a service-identity handshake.

    Any local client must be able to verify it reached the intended
    ModelQuad control plane -- not a random local HTTP
    server or an incompatible instance --
    before treating the port as ready. Everything here is non-sensitive and
    served on loopback only.

    ``instance_kind`` comes from ``statecontext`` rather than from a packaging
    predicate. Those two answers once differed for a plain
    non-editable install, which reported ``development`` here while writing
    *installed* state -- an identity claim that contradicted the store it was
    serving. ``state_root`` and ``state_source`` are published alongside it so
    a client can check the thing that actually matters: not "are we the same
    packaging shape" but "are we serving the same product state".
    """

    import os as _os
    from pathlib import Path

    from ..statecontext import resolve_state_context
    from . import db as _service_db

    state = resolve_state_context()
    try:
        from ..jobs.handle import service_runner_verdict
        scope = getattr(request, "scope", None) if request is not None else None
        if isinstance(scope, dict) and scope.get("path") == "/api/listener-proof":
            detached_ok, detached_reason = _proof_runner_verdict(request, service_runner_verdict)
        else:
            # Diagnosis and launch admission always use a fresh measurement.
            detached_ok, detached_reason = service_runner_verdict()
    except Exception:
        detached_ok, detached_reason = False, 'containment_unknown'
    return {
        "status": "ok",
        "product": "modelquad",
        "service_role": "control-plane",
        "listener_auth_protocol": 1,  # Public advertisement, never authentication.
        "detached_runner": {"ok": detached_ok, "reason": detached_reason},
        "instance_kind": state.mode.value,
        "state_root": str(state.state_root),
        # A non-reversible tag for the SAME question `state_root` answers,
        # safe to log, screenshot or compare where the absolute
        # path (which contains a username) must not appear. Two instances
        # serving one store agree here; the split this guard exists to catch
        # does not.
        "state_root_id": state_identity_tag(state.state_root),
        "state_source": state.source.value,
        "version": _package_version(),
        # Which exact build is answering: the package version names a
        # release line, not a build. Commit, build time and channel only --
        # never a path.
        "build": current_build_info().to_dict(),
        "db_schema_version": _service_db.SCHEMA_VERSION,
        "db_identity": _db_identity(request, _service_db.DEFAULT_DB_PATH),
        "pid": _os.getpid(),
        "time": datetime.now(timezone.utc).isoformat(),
    }


_PROOF_RUNNER_TTL_SECONDS = 30.0
_PROOF_RUNNER_LOCK = threading.Lock()


def _proof_runner_verdict(request: Request, measure: Any) -> tuple[bool, str]:
    """Cache only the proof's readiness observation, never its identity facts."""
    with _PROOF_RUNNER_LOCK:
        cached = getattr(request.app.state, "proof_runner_cache", None)
        if cached is not None and time.monotonic() - cached[0] < _PROOF_RUNNER_TTL_SECONDS:
            return cached[1]
        try:
            verdict = measure()
        except Exception:
            # An exception is not a measured verdict; the next proof retries.
            return False, "containment_unknown"
        # containment_unknown is how the policy reports a failed measurement
        # (handle._service_runner_policy catches it), so it is never cached.
        if tuple(verdict)[1:2] != ("containment_unknown",):
            request.app.state.proof_runner_cache = (time.monotonic(), verdict)
        return verdict


@router.post("/listener-proof")
async def listener_proof(request: Request) -> JSONResponse:
    from .listener_proof import proof_response
    return await proof_response(request)


def _require_settings_auth(request: Request) -> None:
    # Reuse the app's exact trust authority. Treat this read like a PUT for
    # Origin and token checks without widening any other public read route.
    boundary = getattr(request.app, '_trust_boundary', None)
    refusal = boundary.refusal(request.headers, 'PUT', '/api/settings') if boundary else (403, 'Settings authentication unavailable')
    if refusal:
        raise HTTPException(status_code=refusal[0], detail='Settings authentication required')


def _runner_settings(request: Request) -> dict:
    from ..jobs.handle import service_runner_verdict
    try:
        ok, reason = service_runner_verdict()
    except Exception:
        ok, reason = False, 'containment_unknown'
    return {**history.get_job_runner_setting(request.app.state.db_path),
            'job_runner_readiness': 'ready' if ok else 'unavailable',
            'job_runner_limitation': None if ok and reason in ('not_in_job', 'breakaway_verified') else reason}


@router.put("/settings")
def put_settings(payload: SettingsUpdateRequest, request: Request) -> dict[str, Any]:
    """Turn the job runner on or off.

    Turning it on requires the consent ``modelquad settings on`` records
    after the user typed the answer (or passed ``--yes``); no API caller can
    give that consent.
    """
    _require_settings_auth(request)
    db_path = request.app.state.db_path
    try:
        if payload.job_runner_enabled is True and history.get_job_runner_consent_at(db_path) is None:
            raise HTTPException(status_code=409, detail="job_runner_consent_required")
        if payload.job_runner_enabled is not None:
            history.put_job_runner_enabled(db_path, payload.job_runner_enabled)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error):
        raise HTTPException(status_code=503, detail='Settings could not be saved') from None
    return _runner_settings(request)
