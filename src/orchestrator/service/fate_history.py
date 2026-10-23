# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Independent immutable fate-release audit, with exact snapshot write fences."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import json
import sqlite3
import uuid
from pathlib import Path

from orchestrator import procutil
from orchestrator.jobs.fate import Proof, collect_proof, identity, runner_positively_live, valid_pid, valid_created
from . import db, history

REASONS = {'boot_after_start', 'worker_pid_gone', 'worker_pid_reused',
           'owner_released_unknown_fate'}
UNKNOWN_CLAUSE = "(j.status='unknown' OR j.recovery_decision='unknown' OR " \
    "(a.resolved_at IS NOT NULL AND (a.failure_kind IS NOT NULL " \
    "OR a.worker_descendants IS NOT NULL)) OR " \
    "(j.ended_at IS NULL AND a.resolved_at IS NOT NULL) OR " \
    "(j.launch_limitation IS NOT NULL AND j.status <> 'finished'))"


class FateError(ValueError):
    pass


@contextmanager
def publication_lock(path):
    """Serialize publication/release without locking lifecycle DB writes.

    The persistent sibling lock contains no evidence or content. Its kernel
    lock dies with the holder; failure to acquire it refuses the operation.
    Both callers take it BEFORE opening a write transaction or the sink.
    """
    from orchestrator.usage.base import _claim_snapshot_lock, _release_snapshot_lock

    try:
        target = Path(path).resolve(strict=True)
        descriptor = _claim_snapshot_lock(target.with_name(target.name + '.fate.lock'),
                                          wait_seconds=5.0)
    except (OSError, ValueError):
        raise FateError('fate_store_unavailable') from None
    if descriptor is None:
        raise FateError('fate_store_unavailable')
    try:
        yield
    finally:
        _release_snapshot_lock(descriptor)


def _query(conn, statement, params=()):
    """Named local rows without changing an admission caller's row factory."""
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(statement, params)


def _uuid(value):
    return type(value) is str and str(uuid.UUID(value)) == value


def _decision_id(value):
    """Normalize only canonical UUIDs and provenance.new_decision_id() hex."""
    if (type(value) is not str or re.fullmatch(
            r'(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
            value) is None):
        return None
    return uuid.UUID(value).hex


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise FateError('fate_request_invalid')
    return value.isoformat()


@contextmanager
def connection(path, *, write=False):
    conn = None
    try:
        mode = 'rw' if write else 'ro'
        conn = sqlite3.connect(history._read_only_uri(path).replace('mode=ro', 'mode=' + mode),
                               uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        if _query(conn, 'PRAGMA user_version').fetchone()[0] != db.SCHEMA_VERSION:
            raise FateError('fate_store_unavailable')
        _query(conn, 'BEGIN IMMEDIATE' if write else 'BEGIN')
        yield conn
        if write:
            conn.commit()
    except FateError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError):
        raise FateError('fate_store_unavailable') from None
    finally:
        if conn is not None:
            conn.close()


@dataclass(frozen=True)
class Snapshot:
    job: dict
    attempt: dict


def linked(conn, job_id):
    try:
        if not _uuid(job_id):
            raise ValueError
        j = _query(conn, 'SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if j is None:
            raise FateError('fate_not_found')
        a = _query(conn, 'SELECT * FROM delegation_attempts WHERE id=?', (j['attempt_id'],)).fetchone()
        if (a is None or type(j['attempt_id']) is not int or j['attempt_id'] <= 0
                or _decision_id(j['decision_id']) is None
                or _decision_id(j['decision_id']) != _decision_id(a['decision_id'])
                or _query(conn, 'SELECT count(*) FROM jobs WHERE attempt_id=?',
                                (j['attempt_id'],)).fetchone()[0] != 1
                or type(a['workspace_key']) is not str
                or re.fullmatch('[0-9a-f]{32}', a['workspace_key']) is None
                or history._workspace_ancestor_set(a['workspace_ancestors']) is None
                or a['status'] not in ('started', 'completed', 'failed', 'refused')
                or (a['status'] == 'started') != (a['resolved_at'] is None)):
            raise ValueError
        _utc(datetime.fromisoformat(j['created_at']))
        _utc(datetime.fromisoformat(a['created_at']))
        if a['resolved_at'] is not None:
            _utc(datetime.fromisoformat(a['resolved_at']))
        return Snapshot(dict(j), dict(a))
    except FateError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise FateError('fate_linkage_invalid') from None


def snapshot(path, job_id):
    with connection(path) as conn:
        return linked(conn, job_id)


def eligible(conn, job_id):
    j = _query(conn, 'SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if j is None:
        return False
    a = _query(conn, 'SELECT * FROM delegation_attempts WHERE id=?', (j['attempt_id'],)).fetchone()
    if a is None:
        return False
    never_launched = _never_launched(conn, j, a)
    if not never_launched and (j['status'] == 'unknown' or j['recovery_decision'] == 'unknown'
            or (j['launch_limitation'] is not None and j['status'] != 'finished')
            or (j['ended_at'] is None and a['resolved_at'] is not None)):
        return True
    if a['resolved_at'] is None:
        return False
    if (a['failure_kind'] not in history._ENDINGS_THAT_ACCOUNT_FOR_THE_WORKER
            or (a['failure_kind'] in history._ENDINGS_THAT_ASSERT_NOTHING_RAN and a['worker_pid'])):
        return True
    if (valid_pid(j['worker_pid']) and a['failure_kind'] is None
            and worker_ending_confirmed(conn, j, a)):
        # A collected normal exit accounts for this worker, not everything
        # it left behind. Workspace admission still checks those separately.
        return False
    # Missing exit proof preserves the existing descendant-based verdict.
    raw = a['worker_descendants']
    if raw is None:
        return False
    try:
        facts = json.loads(raw)
        return (type(facts) is not dict or facts.get('enumerated') is not True
                or bool(facts.get('truncated')) or bool(facts.get('descendants')))
    except (ValueError, TypeError):
        return True


def worker_ending_confirmed(conn, job, attempt):
    """A recorded exit plus completed output handoff, never whole-tree death.

    record_execution keeps the process exit code before publication; the
    attempt and job close only after the output sink accepted the result.
    Require a single execution in that attempt's interval and unique job
    linkage so another run on the same decision cannot supply its exit.
    Descendants (including detached helpers) remain diagnostic evidence.
    """
    decision = _decision_id(job['decision_id'])
    if (job['ended_at'] is None or job['status'] not in ('finished', 'failed')
            or job['recovery_decision'] is not None
            or attempt['status'] != 'completed' or attempt['resolved_at'] is None
            or attempt['failure_kind'] is not None
            or decision is None or decision != _decision_id(attempt['decision_id'])
            or job['attempt_id'] != attempt['id'] or not valid_pid(job['worker_pid'])
            or (attempt['worker_pid'] is not None and attempt['worker_pid'] != job['worker_pid'])):
        return False
    forms = (decision, str(uuid.UUID(decision)))
    if _query(conn, 'SELECT count(*) FROM jobs WHERE decision_id IN (?,?)', forms).fetchone()[0] != 1:
        return False
    try:
        start = datetime.fromisoformat(attempt['created_at'])
        end = datetime.fromisoformat(attempt['resolved_at'])
        job_end = datetime.fromisoformat(job['ended_at'])
        for stamp in (start, end, job_end):
            _utc(stamp)
        if end < start or job_end < start:
            return False
        rows = _query(conn, 'SELECT exit_code, provider, created_at FROM execution_history '
                      'WHERE decision_id IN (?,?) AND created_at>=? AND created_at<=? LIMIT 2',
                      (*forms, start.isoformat(), end.isoformat())).fetchall()
        if (len(rows) != 1 or type(rows[0]['exit_code']) is not int
                or (attempt['routed_provider'] is not None
                    and rows[0]['provider'] != attempt['routed_provider'])):
            return False
        observed = datetime.fromisoformat(rows[0]['created_at'])
        _utc(observed)
        if not start <= observed <= end:
            return False
    except (FateError, ValueError, TypeError):
        return False
    # A matching live worker contradicts a recorded exit. An unreadable live
    # probe does not erase a durable exit; it does not certify any descendants.
    verdict, _ = identity(job['worker_pid'], job['worker_created'])
    return verdict != 'alive'


def _never_launched(conn, job, attempt):
    """A failure label alone cannot override evidence that something ran."""
    REPLACEABLE_FAILURES = frozenset({'policy_refused', 'failed_to_start',
                                      'provider_unavailable', 'no_viable_worker'})

    decision = _decision_id(job['decision_id'])
    # An open job can still be spawned (job_spawn_fence checks the job only).
    if (job['ended_at'] is None
            or attempt['status'] not in ('failed', 'refused') or attempt['resolved_at'] is None
            or attempt['failure_kind'] not in REPLACEABLE_FAILURES
            or any(pid is not None for pid in (attempt['worker_pid'], job['runner_pid'], job['worker_pid']))
            or decision is None or decision != _decision_id(attempt['decision_id'])):
        return False
    # Check both stored forms through the existing decision index, without
    # rewriting history. An execution on this decision contradicts the label.
    return _query(conn, 'SELECT 1 FROM execution_history WHERE decision_id IN (?,?) LIMIT 1',
                  (decision, str(uuid.UUID(decision)))).fetchone() is None


def validated_release(conn, job_id):
    """Malformed or unscoped records cannot hide a hazard."""
    row = _query(conn, 'SELECT * FROM job_fate_releases WHERE job_id=?', (job_id,)).fetchone()
    if row is None:
        return None
    try:
        s = linked(conn, job_id)
        if (row['attempt_id'] != s.attempt['id']
                or _decision_id(row['decision_id']) != _decision_id(s.job['decision_id'])
                or s.job['ended_at'] is None or s.attempt['resolved_at'] is None
                or row['reason'] not in REASONS):
            return None
        _utc(datetime.fromisoformat(row['released_at']))
        _utc(datetime.fromisoformat(row['observed_at']))
        if datetime.fromisoformat(row['observed_at']) > datetime.fromisoformat(row['released_at']):
            return None
        boot_fields = ('reservation_boot_id','observed_boot_id','observed_uptime_ms','boot_lower','boot_upper')
        pid_fields = ('proof_pid','recorded_created','observed_created')
        if row['reason'] != 'boot_after_start' and any(row[key] is not None for key in boot_fields):
            return None
        if row['reason'] in ('boot_after_start','owner_released_unknown_fate') and any(row[key] is not None for key in pid_fields):
            return None
        if row['reason'] == 'worker_pid_gone' and (row['recorded_created'] is not None or row['observed_created'] is not None):
            return None
        if row['reason'] == 'owner_released_unknown_fate':
            if (row['authentication_method'] != 'installation_token' or row['surface'] not in ('ui', 'cli')
                    or re.fullmatch('installation:[0-9a-f]{12,64}', row['actor']) is None):
                return None
        elif row['authentication_method'] != 'local_recovery' or row['actor'] != 'local_recovery' or row['surface'] != 'recovery':
            return None
        if row['reason'] in ('worker_pid_gone', 'worker_pid_reused'):
            if not valid_pid(row['proof_pid']) or row['proof_pid'] != s.job['worker_pid']:
                return None
        if row['reason'] == 'worker_pid_reused' and (
                not valid_created(row['recorded_created']) or not valid_created(row['observed_created'])
                or row['recorded_created'] != s.job['worker_created']
                or row['recorded_created'] == row['observed_created']):
            return None
        if row['reason'] == 'boot_after_start' and (
                not _uuid(row['observed_boot_id']) or not _uuid(row['reservation_boot_id'])
                or row['reservation_boot_id'] != s.job['reservation_boot_id']
                or row['observed_boot_id'] == row['reservation_boot_id']
                or s.job['reservation_boot_qualified'] != 1
                or type(row['observed_uptime_ms']) is not int or row['observed_uptime_ms'] < 0
                or datetime.fromisoformat(row['boot_lower']) <= datetime.fromisoformat(s.job['created_at'])
                or datetime.fromisoformat(row['boot_upper']) < datetime.fromisoformat(row['boot_lower'])):
            return None
        return dict(row)
    except (FateError, ValueError, TypeError):
        return None


def candidates(path, *, limit=64, after=None):
    with connection(path) as conn:
        register_scan(conn)
        cursor_clause = '' if after is None else ' AND (j.created_at,j.job_id) > (?,?)'
        params = () if after is None else after
        rows = _query(conn, 
            'SELECT j.job_id, j.created_at, a.task_type FROM jobs j LEFT JOIN delegation_attempts a ON a.id=j.attempt_id '
            'WHERE ' + UNKNOWN_CLAUSE + ' AND fate_unknown(j.job_id)=1 AND fate_released(a.id)=0 '
            + cursor_clause + ' ORDER BY j.created_at, j.job_id LIMIT ?',
            (*params, max(1, min(limit, 256))),).fetchall()
        result = []
        for row in rows:
            try:
                if _uuid(row['job_id']):
                    _utc(datetime.fromisoformat(row['created_at']))
                    task = row['task_type']
                    # Content-free task kind for the banner; anything unexpected reads unknown.
                    result.append({'job_id': row['job_id'], 'created_at': row['created_at'],
                                   'task_type': task if isinstance(task, str) and task.isidentifier() and len(task) <= 32 else None})
            except (ValueError, TypeError):
                continue  # Surface no private malformed ids; admission still refuses.
        return result


def release(path, observed, *, proof, now, actor, authentication_method, surface, probe=procutil):
    stamp = _utc(now)
    _utc(proof.observed_at)
    if proof.observed_at > now or proof.reason not in REASONS:
        raise FateError('fate_proof_invalid')
    manual = proof.reason == 'owner_released_unknown_fate'
    if manual:
        if (authentication_method != 'installation_token' or surface not in ('ui', 'cli')
                or re.fullmatch('installation:[0-9a-f]{12,64}', actor) is None
                or any(v is not None for v in (proof.pid, proof.recorded_created, proof.observed_created, proof.boot))):
            raise FateError('fate_request_invalid')
    elif (authentication_method, actor, surface) != ('local_recovery', 'local_recovery', 'recovery'):
        raise FateError('fate_request_invalid')
    with publication_lock(path), connection(path, write=True) as conn:
        original = validated_release(conn, observed.job['job_id'])
        if original is not None:
            return original
        current = linked(conn, observed.job['job_id'])
        if current != observed:
            raise FateError('fate_snapshot_changed')
        if now < datetime.fromisoformat(current.job['created_at']) or now < datetime.fromisoformat(current.attempt['created_at']):
            raise FateError('fate_clock_unavailable')
        if not eligible(conn, current.job['job_id']):
            raise FateError('fate_not_unknown')
        if runner_positively_live(current.job, probe):
            raise FateError('fate_runner_alive')
        if not manual and collect_proof(current.job, now=proof.observed_at, probe=probe,
                                        boot=proof.boot) != proof:
            raise FateError('fate_proof_changed')
        # Write closure BEFORE insertion so the DB's durable release fences
        # apply even to old/unconditional writers after this atomic commit.
        _query(conn, "UPDATE jobs SET ended_at=COALESCE(ended_at, ?), updated_at=? "
                     "WHERE job_id=?", (stamp, stamp, current.job['job_id']))
        _query(conn, "UPDATE delegation_attempts SET status='failed', outcome='unknown', "
                     "failure_kind='fate_released', resolved_at=? "
                     "WHERE id=? AND status='started' AND resolved_at IS NULL",
                     (stamp, current.attempt['id']))
        b = proof.boot
        _query(conn, 'INSERT INTO job_fate_releases VALUES (' + ','.join('?' for _ in range(17)) + ')',
                     (current.job['job_id'], current.attempt['id'], current.job['decision_id'],
                      _utc(proof.observed_at), stamp, actor, authentication_method, surface, proof.reason,
                      proof.pid, proof.recorded_created, proof.observed_created,
                      current.job['reservation_boot_id'] if b else None, b.session if b else None,
                      b.uptime_ms if b else None, _utc(b.boot_lower) if b else None, _utc(b.boot_upper) if b else None))
        result = validated_release(conn, current.job['job_id'])
        if result is None:
            raise FateError('fate_audit_invalid')
        return result


def register_scan(conn):
    """Validated exemptions in SQL BEFORE caps; independently live leftovers stay."""
    exemptions = {}
    descendant_hazards = {}

    def released(attempt_id):
        rows = _query(conn, 'SELECT job_id FROM jobs WHERE attempt_id=?', (attempt_id,)).fetchall()
        if len(rows) != 1 or validated_release(conn, rows[0][0]) is None:
            return 0
        return 1

    def exempts(attempt_id):
        if attempt_id not in exemptions:
            exemptions[attempt_id] = observe_exemption(attempt_id)
        return exemptions[attempt_id]

    def observe_exemption(attempt_id):
        if not released(attempt_id):
            return 0
        a = _query(conn, 'SELECT * FROM delegation_attempts WHERE id=?', (attempt_id,)).fetchone()
        if a['worker_descendants'] is None:
            return 1
        try:
            facts = json.loads(a['worker_descendants'])
            if type(facts) is not dict:
                return 0
            descendants = facts.get('descendants', [])
            if type(descendants) is not list or len(descendants) > history._DESCENDANTS_RECORDED:
                return 0
            for item in descendants:
                if type(item) is not dict or not valid_pid(item.get('pid')):
                    return 0
        except (TypeError, ValueError):
            return 0
        if not descendants:
            return 1
        # A failed census is covered by the recorded unknown-fate release;
        # an independently observed live descendant is a separate blocker.
        hazard = history._descendant_hazard(dict(a), native_only=True)
        descendant_hazards[attempt_id] = hazard
        return int(hazard is None or hazard[0] != 'may_be_mutating')

    conn.create_function('fate_released', 1, released)
    conn.create_function('fate_exempts', 1, exempts)
    conn.create_function('fate_unknown', 1, lambda job_id: int(eligible(conn, job_id)))
    return descendant_hazards
