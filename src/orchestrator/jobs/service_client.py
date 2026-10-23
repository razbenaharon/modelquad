# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Identity-checked, loopback-only handoff to the background service.

No retries or redirects: the history row, not an HTTP failure, decides who
owns execution. Response bodies and the installation secret stay private.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
import time

from orchestrator import paths
from orchestrator.service import listener
from orchestrator.service.listener_proof import parse_json
from orchestrator.service.socket_identity import ListenerError


@dataclass(frozen=True)
class Unavailable:
    reason: str


_LAUNCH_REASONS = frozenset({
    'launch_unavailable', 'runner_launch_failed', 'job_not_launchable',
    'launch_rate_limited', 'service_contained', 'job_runner_disabled',
})


@dataclass(frozen=True)
class ServiceClient:
    _listener: listener.AuthenticatedListener = field(repr=False)
    launch_limitation: str | None = field(default=None, init=False)

    def job_runner_setting(self, enabled: bool) -> dict | Unavailable:
        """User management transport; never falls back to writing a local DB."""
        if type(enabled) is not bool:
            return Unavailable('settings_invalid')
        try:
            response = self._listener.request('PUT', '/api/settings',
                body=json.dumps({'job_runner_enabled': enabled}).encode('utf-8'), cap=16384)
            payload = parse_json(response.body, cap=16384)
            if (response.status == 409 and type(payload) is dict
                    and payload.get('detail') == 'job_runner_consent_required'):
                return Unavailable('job_runner_consent_required')
            if response.status != 200:
                return Unavailable('settings_save_failed')
            if (type(payload) is not dict or payload.get('job_runner_enabled') is not enabled
                    or payload.get('job_runner_setting_status') != 'available'
                    or type(payload.get('job_runner_changed_at')) is not str):
                return Unavailable('settings_bad_response')
            stamp = payload['job_runner_changed_at']
            moment = datetime.fromisoformat(stamp) if len(stamp) <= 64 else None
            limits = {None, 'job_shared', 'job_kill_on_close', 'job_breakaway_ineffective',
                      'containment_unknown', 'contained_innermost_job_without_kill_on_close',
                      'scheduler_containment_verified'}
            if (moment is None or moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(moment)
                    or payload.get('job_runner_readiness') not in ('ready', 'unavailable')
                    or payload.get('job_runner_limitation') not in limits):
                return Unavailable('settings_bad_response')
            return {key: payload.get(key) for key in ('job_runner_enabled', 'job_runner_changed_at',
                    'job_runner_readiness', 'job_runner_limitation')}
        except Exception:
            return Unavailable('settings_service_unavailable')
        finally:
            self._listener.close()

    def fate(self, job_id: str) -> dict | Unavailable:
        return self._manage(job_id, release=False)

    def release_fate(self, job_id: str, *, acknowledge: bool) -> dict | Unavailable:
        if acknowledge is not True:
            return Unavailable('fate_acknowledgment_required')
        return self._manage(job_id, release=True)

    def _manage(self, job_id, *, release):
        try:
            if type(job_id) is not str or str(uuid.UUID(job_id)) != job_id:
                return Unavailable('fate_request_invalid')
        except (ValueError, TypeError):
            return Unavailable('fate_request_invalid')
        try:
            body = (json.dumps({'acknowledge_unknown_worker_fate': True, 'surface': 'cli'})
                    .encode('utf-8') if release else b'')
            suffix = 'release-fate' if release else 'fate'
            response = self._listener.request(
                'POST' if release else 'GET', f'/api/jobs/{job_id}/{suffix}',
                body=body, cap=16384,
            )
            try:
                payload = parse_json(response.body, cap=16384)
            except ListenerError:
                return Unavailable('fate_bad_response')
            if response.status != 200:
                # Never return response prose or arbitrary error labels.
                code = payload.get('code') if isinstance(payload, dict) else None
                allowed = {'service_token_missing', 'service_token_invalid', 'service_token_unavailable',
                           'fate_not_found', 'fate_store_unavailable', 'fate_snapshot_changed',
                           'fate_runner_alive', 'fate_linkage_invalid', 'fate_not_unknown'}
                return Unavailable(code if code in allowed else 'fate_service_refused')
            if not _valid_fate_payload(payload, job_id, release=release):
                return Unavailable('fate_bad_response')
            return payload
        except ListenerError as exc:
            return Unavailable(exc.code)
        except Exception:  # noqa: BLE001 -- never expose transport or reply prose
            return Unavailable('fate_service_unavailable')

    def launch(self, job_id: str, spec_name: str) -> tuple[str, str]:
        try:
            response = self._listener.request(
                'POST', f'/api/jobs/{job_id}/launch',
                body=json.dumps({'spec': spec_name}).encode('utf-8'), cap=4096,
            )
            try:
                payload = parse_json(response.body, cap=4096)
            except ListenerError:
                payload = None
            if response.status == 200:
                if (isinstance(payload, dict) and payload.get('launched') is True
                        and payload.get('job_id') == job_id):
                    limitation = payload.get('limitation')
                    if limitation is not None and limitation not in {
                        'contained_innermost_job_without_kill_on_close', 'scheduler_containment_verified',
                        'inline_runner_host_bound',
                        'unknown',
                    }:
                        return 'ambiguous', 'bad_response'
                    object.__setattr__(self, 'launch_limitation', self.launch_limitation or limitation)
                    return 'launched', 'launched'
                return 'ambiguous', 'bad_response'
            reason = payload.get('reason') if isinstance(payload, dict) else None
            label = reason if isinstance(reason, str) and reason in _LAUNCH_REASONS else 'refused_other'
            if response.status >= 500:
                return 'ambiguous', label
            if 400 <= response.status < 500:
                return 'refused', label
            return 'ambiguous', 'bad_response'
        except ListenerError as exc:
            # A refused redirect or oversized reply arrives after the launch
            # may have been accepted. Preserve the consumers' ownership CAS
            # path using their existing response-ambiguity label, without retry.
            if exc.sent and exc.code in {
                'service_redirect_refused', 'service_proof_invalid', 'response_too_large',
            }:
                return 'ambiguous', 'bad_response'
            return ('ambiguous' if exc.sent else 'refused'), exc.code
        except Exception:  # noqa: BLE001 -- a hostile reply must still release the row
            return 'ambiguous', 'bad_response'
        finally:
            self._listener.close()


def _service_port(*, deadline=None) -> int:
    # ServiceProcessManager writes the service's complete argv to service.pid.
    # Importing the CLI constant and the shell classifier loads no GUI toolkit.
    from orchestrator.cli import SERVICE_DEFAULT_PORT

    try:
        hint = paths.state_root() / 'service.pid'
        if deadline is None:
            record = json.loads(hint.read_text(encoding='utf-8'))
        else:
            if time.monotonic() >= deadline:
                raise ListenerError('service_connection_changed')
            with hint.open('rb') as stream:
                raw = stream.read(32769)
            if len(raw) > 32768 or time.monotonic() >= deadline:
                raise ListenerError('service_connection_changed')
            record = json.loads(raw)
        argv = record.get('argv') if isinstance(record, dict) else None
        if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv):
            index = argv.index('--port')
            port = int(argv[index + 1])
            if 1 <= port <= 65535:
                return port
    except (OSError, ValueError, IndexError):
        pass
    return SERVICE_DEFAULT_PORT


def connect(db_path: str | Path, *, management=False) -> ServiceClient | Unavailable:
    """Authenticate once; gate launches using only the signed health snapshot."""
    from orchestrator.service.api import state_identity_tag

    connection = None
    try:
        # authenticate checks ownership, proof and shared compatibility/schema.
        # Its db_path option also gates detached launches, so perform the job
        # checks here to let management inspect/release a contained service.
        connection = listener.authenticate(_service_port())
        payload = connection.health
        if (type(payload.get('db_identity')) is not str
                or payload['db_identity'] != state_identity_tag(Path(db_path).resolve())):
            raise ListenerError('service_identity_mismatch')
        detached = payload.get('detached_runner')
        if type(detached) is not dict or type(detached.get('ok')) is not bool:
            raise ListenerError('service_identity_mismatch')
        if detached['ok'] is False and not management:
            raise ListenerError('service_contained')
        client = ServiceClient(connection)
        if detached.get('reason') == 'contained_innermost_job_without_kill_on_close':
            object.__setattr__(client, 'launch_limitation', detached['reason'])
        return client
    except ListenerError as exc:
        if connection is not None:
            connection.close()
        return Unavailable(exc.code)
    except Exception:
        if connection is not None:
            connection.close()
        return Unavailable('service_owner_unavailable')


def connect_management(db_path: str | Path) -> ServiceClient | Unavailable:
    """Same identity/token check, without the detached-provider launch gate."""
    return connect(db_path, management=True)


def transient_diagnostics(db_path, job_id, *, ref=None, diagnostics=None):
    """Bound local lookup and authenticated transport together; never persist."""
    from orchestrator.usage.refresh import daemon_future
    deadline = time.monotonic() + 2.0
    try:
        return daemon_future(_transient_diagnostics, db_path, job_id, ref=ref,
                             diagnostics=diagnostics, deadline=deadline).result(
                                 timeout=max(0.0, deadline - time.monotonic()))
    except Exception:
        return None


def _transient_diagnostics(db_path, job_id, *, ref, diagnostics, deadline):
    """Filesystem/transport work stays off the caller's bounded wait thread."""
    from orchestrator import procutil
    from orchestrator.jobs.diagnostics import BODY_CAP, validate, wire
    connection = None
    try:
        if type(job_id) is not str or str(uuid.UUID(job_id)) != job_id:
            return None
        if time.monotonic() >= deadline:
            return None
        if ref is None:
            from orchestrator.jobs.diagnostics import INLINE, inline_key
            from orchestrator.execution.bound_launch import host_diagnostic_payload
            local = INLINE.get(inline_key(db_path, job_id))
            # Path resolution or the cache lock can stall. The caller's wait
            # remains bounded; late lookup completion must not start transport.
            if time.monotonic() >= deadline:
                return None
            if local is not None:
                return host_diagnostic_payload(local)
        # Same runner-independent management admission as connect_management:
        # authenticate installation/process, then bind the requested store.
        connection = listener.authenticate(_service_port(deadline=deadline), deadline=deadline)
        from orchestrator.service.api import state_identity_tag
        if connection.health.get('db_identity') != state_identity_tag(Path(db_path).resolve()):
            return None
        body = b''
        if ref is not None:
            body = json.dumps({'attempt_id': ref.attempt_id, 'decision_id': ref.decision_id,
                               'runner_pid': os.getpid(),
                               'runner_created': procutil.process_creation_epoch(os.getpid()),
                               'diagnostics': wire(diagnostics)}).encode('utf-8')
            if len(body) > BODY_CAP:
                return None
        response = connection.request('POST' if ref is not None else 'GET',
                                      f'/api/jobs/{job_id}/diagnostics', body=body,
                                      cap=BODY_CAP, deadline=deadline)
        if response.status != 200:
            return None
        payload = parse_json(response.body, cap=BODY_CAP)
        if ref is not None:
            return True if payload == {'status': 'available'} else None
        if type(payload) is dict and set(payload) == {'status', 'diagnostics'} and payload['status'] == 'available':
            from orchestrator.execution.bound_launch import host_diagnostic_payload
            return host_diagnostic_payload(validate(payload['diagnostics']))
    except Exception:
        pass  # This optional Host channel cannot change readiness or outcomes.
    finally:
        if connection is not None:
            connection.close()
    return None


def _valid_fate_payload(payload, job_id, *, release):
    from datetime import datetime, timedelta
    from orchestrator.service.fate_history import REASONS
    def stamp(value):
        try:
            return type(value) is str and datetime.fromisoformat(value).utcoffset() == timedelta(0)
        except ValueError:
            return False
    if type(payload) is not dict or payload.get('job_id') != job_id:
        return False
    expected = {'job_id', 'release'} if release else {'job_id', 'created_at', 'blocker', 'release', 'live_codex'}
    if set(payload) != expected:
        return False
    audit = payload['release']
    if audit is not None:
        if (type(audit) is not dict or set(audit) != {'job_id', 'attempt_id', 'decision_id', 'observed_at',
                                                    'released_at', 'reason', 'surface'}
                or audit['job_id'] != job_id or type(audit['attempt_id']) is not int
                or audit['attempt_id'] <= 0 or audit['reason'] not in REASONS
                or audit['surface'] not in ('ui', 'cli', 'recovery')
                or not stamp(audit['observed_at']) or not stamp(audit['released_at'])):
            return False
        try:
            if str(uuid.UUID(audit['decision_id'])) != audit['decision_id']:
                return False
        except (ValueError, TypeError):
            return False
    if release:
        return audit is not None
    listing = payload['live_codex']
    if (type(payload['blocker']) is not bool or not stamp(payload['created_at'])
            or type(listing) is not dict or set(listing) != {'observed_at', 'status', 'processes', 'reason'}
            or not stamp(listing['observed_at']) or listing['status'] not in ('complete','partial','unavailable')
            or listing['reason'] not in (None,'snapshot_unavailable','entries_unreadable_or_bounded')
            or type(listing['processes']) is not list or len(listing['processes']) > 64):
        return False
    return all(type(item) is dict and set(item) == {'pid', 'created_at'}
               and type(item['pid']) is int and 0 < item['pid'] <= 0xffffffff
               and (item['created_at'] is None or stamp(item['created_at']))
               for item in listing['processes'])


def read_pending_summary(workspace_key: str, *, deadline: float, db_path=None):
    """Authenticated management read with one caller-owned deadline.

    Uses the same owner/proof/verified-secret transport as launch, with
    runner-independent read-only admission and no empty-secret retries.
    """
    from orchestrator.adapters.pending_reminder import (
        BYTE_CAP, valid_workspace_key, validate_summary,
    )
    from orchestrator.service import db

    if not valid_workspace_key(workspace_key) or time.monotonic() >= deadline:
        raise ListenerError('service_identity_mismatch')
    # No secret read, direct DB access, service startup, retries or redirects
    # here. The listener owns socket identity and the verified secret.
    connection = listener.authenticate(
        _service_port(deadline=deadline), db_path=db_path or db.DEFAULT_DB_PATH,
        deadline=deadline, require_runner=False,
    )
    try:
        response = connection.request(
            'GET', '/api/pending-output?summary=host&workspace_key=' + workspace_key,
            cap=BYTE_CAP, deadline=deadline,
        )
        if response.status != 200 or time.monotonic() >= deadline:
            raise ListenerError('service_connection_changed')
        return validate_summary(parse_json(response.body, cap=BYTE_CAP))
    finally:
        connection.close()
