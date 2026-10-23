# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Content-free, leased job recovery after runner loss.

The runner alone holds its kill-on-close Job Object. Recovery never walks or
kills descendants; only an exactly identified surviving worker root may be
stopped. Outside-job evidence and unreadable identities stay unknown.
No partial bytes are persisted mid-run today. Existing pending answers are
kept exactly; reconstruction from an attempt has empty text, not a claim
that an answer or changed files were captured.

The ten-second deadline is checked BETWEEN operations. An in-flight store,
SQLite operation or process probe cannot be interrupted. Startup schedules
one daemon and never waits for it. A process-wide lock makes passes single
flight; tokens and the sixty-second lease fence separate processes.
"""

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import secrets
import sqlite3
import threading
import time

from orchestrator import procutil
from orchestrator.execution.base import WorkerTaskStatus
from orchestrator.execution.lifecycle import TerminalResolution
from orchestrator.jobs.handle import STARTUP_SECONDS
from orchestrator.jobs.runner import output_sink_factory
from orchestrator.jobs.seam import JobOutput, JobRef, JobStatus, _utc, job_status_from_resolution
from orchestrator.service import history

LEASE_SECONDS = 60.0
DEADLINE_SECONDS = 10.0
POLL_SECONDS = 30.0
_logger = logging.getLogger(__name__)
_flight = threading.Lock()
_schedule_lock = threading.Lock()
_scheduled = False
_last_poll = {}


def _identity(pid, created, probe):
    # Same pid + creation-time liveness rule as handle.job_status.
    if not pid or created is None:
        return 'unknown'
    state = probe.pid_state(pid)
    if state == 'dead':
        return 'dead'
    observed = probe.process_creation_epoch(pid) if state == 'alive' else None
    if observed is None:
        return 'unknown'
    return 'alive' if observed == created else 'dead'


def _outside_facts(attempt):
    raw = attempt.get('worker_descendants')
    if raw is None:
        return None, False
    facts = json.loads(raw)
    # A failed/truncated census or recorded leftovers cannot prove that only
    # Job Object members existed. They are never terminated by inference.
    uncertain = (not isinstance(facts, dict) or not facts.get('enumerated')
                 or bool(facts.get('truncated')) or bool(facts.get('descendants')))
    return facts, uncertain


def _attempt_status(attempt):
    detail = attempt.get('detail') or ''
    stop = (attempt['failure_kind'] in {'terminated', 'timeout'}
            and detail.startswith('stop_requested'))
    # A legacy terminated attempt without a stop prefix has no recorded
    # deadline flag; infer timeout as the existing runner does.
    return job_status_from_resolution(
        TerminalResolution(attempt['status'], attempt['failure_kind']),
        stop_requested=stop,
        deadline_passed=attempt['failure_kind'] in {'timeout', 'terminated'} and not stop,
    )


def recover_jobs(db_path, *, now, sink=None, probe=None):
    """Best-effort single-flight pass; return only fixed labels and counts.

    Tests inject a private sink and synthetic probe. Production obtains the
    sink from the runner's existing 'store' factory, never a second location.
    Removed outputs with an open attempt stay unknown and are never put again.
    A never-stored answer with proven runner/worker death is interrupted.
    """
    _utc(now, 'now')
    counts = dict(restored=0, unknown=0, alive=0, stopped_roots=0,
                  expired=0, failed=0, deferred=0)
    if not _flight.acquire(blocking=False):
        counts['deferred'] = 1
        return counts
    deadline = time.monotonic() + DEADLINE_SECONDS

    def budget():
        if time.monotonic() >= deadline:
            raise TimeoutError

    try:
        probe = procutil if probe is None else probe
        budget()
        from orchestrator.jobs.fate import release_pass
        from orchestrator.service.fate_history import FateError
        try:
            release_pass(db_path, now=now, probe=probe, deadline=deadline,
                         boot_observer=procutil.observe_boot if probe is procutil else lambda: None)
        except FateError:
            _logger.warning('job fate release pass unavailable')
        budget()
        sink = output_sink_factory('store') if sink is None else sink
        budget()
        counts['expired'] = len(sink.purge_expired(now))
        budget()
        rows = history.recovery_candidates(db_path)
        for row in rows:
            try:
                budget()
                startup = (row['recovery_decision'] == 'startup_failed' or (row['status'] == 'starting' and row['runner_pid'] is None
                           and row['runner_created'] is None and row['heartbeat_at'] is None
                           and (now - datetime.fromisoformat(row['created_at'])).total_seconds()
                           >= STARTUP_SECONDS))
                live = 'dead' if startup else _identity(row['runner_pid'], row['runner_created'], probe)
                budget()
                if live == 'alive':
                    counts['alive'] += 1
                    continue
                if live != 'dead' and row['recovery_claim'] is not None:
                    counts['unknown'] += 1
                    continue  # Lease takeover requires runner-death evidence.
                token = secrets.token_hex(16)
                row = history.claim_job_recovery(db_path, row, token=token, now=now,
                                                 lease_seconds=LEASE_SECONDS)
                budget()
                if row is None:
                    counts['deferred'] += 1
                    continue
                live = 'dead' if startup else _identity(row['runner_pid'], row['runner_created'], probe)
                budget()
                if live != 'dead':
                    history.write_job_recovery(db_path, row, token=token, now=now, release=True)
                    counts['deferred'] += 1
                    continue
                attempt = history.read_job_attempt(db_path, row['attempt_id'])
                budget()
                if attempt is None:
                    raise ValueError('missing attempt')
                descendants, outside = _outside_facts(attempt)
                decision = row['recovery_decision']
                evidence = None  # A persisted decision alone never proves tree death.
                if decision is None:
                    # An unknown reconciliation already stamped before a root stop
                    # records lost containment; root death on a retry cannot erase it.
                    uncertain = (outside or bool(row['launch_limitation']) or
                                 (row['status'] == 'unknown' and row['recovery_ended_at'] is not None))
                    if row['worker_pid']:
                        worker = _identity(row['worker_pid'], row['worker_created'], probe)
                        budget()
                        if worker == 'alive':
                            # Root survival disproves kill-on-close containment.
                            # Its death cannot prove that other job members died.
                            uncertain = True
                            # _terminate_exact reads creation time and terminates
                            # through one OS handle; no PID-reuse race at kill.
                            _logger.warning('job recovery observed surviving worker root')
                            row = history.write_job_recovery(
                                db_path, row, token=token, now=now, containment_lost=True)
                            budget()
                            if row is not None and history.recovery_claim_current(db_path, row, token=token):
                                budget()
                                probe._terminate_exact(row['worker_pid'], row['worker_created'])
                                budget()
                                counts['stopped_roots'] += 1
                                worker = _identity(row['worker_pid'], row['worker_created'], probe)
                                budget()
                            else:
                                continue
                        uncertain = uncertain or worker != 'dead'
                    budget()
                    state = sink.state(row['job_id'], now)
                    budget()
                    collected = sink.collect(row['job_id'], now) if state == 'pending' else None
                    budget()
                    resolved = attempt['resolved_at'] is not None and attempt['status'] != 'started'
                    if collected:
                        decision, status = 'restore_pending', collected.output.job_status
                        # Keep the answer/status even when containment is uncertain,
                        # but a contradictory live observation cannot prove death.
                        evidence = (replace(collected.output, tree_confirmed_dead=False)
                                    if uncertain else collected.output)
                    elif uncertain or (state is not None and not resolved and not startup):
                        decision, status = 'unknown', None
                    elif startup:
                        decision, status = 'startup_failed', JobStatus.FAILED
                    elif resolved:
                        decision, status = 'map_resolved', _attempt_status(attempt)
                    else:
                        decision, status = 'interrupted', JobStatus.INTERRUPTED
                    ended = collected.output.ended_at if collected else now
                    row = history.write_job_recovery(
                        db_path, row, token=token, now=now, ended=ended,
                        decision=decision, status=status.value if status else 'unknown')
                    budget()
                    if row is None:
                        continue
                # No store observation can revise this decision, even when an
                # answer is acknowledged or its outcome record has aged out.
                decision = row['recovery_decision']
                if decision == 'unknown':
                    counts['unknown'] += 1
                    continue
                status = JobStatus(row['status'])
                if decision in {'interrupted', 'map_resolved'}:
                    stamp = datetime.fromisoformat(row['recovery_ended_at'])
                    output = JobOutput(
                        JobRef(row['job_id'], row['attempt_id'], row['decision_id']), status,
                        WorkerTaskStatus.UNKNOWN, 'interrupted' if status is JobStatus.INTERRUPTED else None,
                        '', False, None, None, descendants,
                        datetime.fromisoformat(row['created_at']), stamp, None,
                    )
                    if not history.recovery_claim_current(db_path, row, token=token):
                        continue
                    budget()
                    # False means it was already published or removed. The
                    # persisted decision still finalizes without resurrection.
                    with history.job_publication_fence(db_path, row['job_id']):
                        sink.put(output, now)
                    budget()
                # Finalization is supplied by the persistence layer, with
                # token + snapshot + open-job guards on all writes.
                if history.finalize_job_recovery(db_path, row, token=token, status=status.value,
                                                 now=now, startup=decision == 'startup_failed',
                                                 evidence=evidence):
                    counts['restored'] += 1
                budget()
            except TimeoutError:
                counts['deferred'] += 1
                break
            except (OSError, ValueError, sqlite3.Error):
                counts['failed'] += 1
                _logger.warning('job recovery operation unavailable')
    except TimeoutError:
        counts['deferred'] += 1
    except (OSError, ValueError, sqlite3.Error):
        counts['failed'] += 1
        _logger.warning('job recovery unavailable')
    finally:
        _flight.release()
    _logger.info('job recovery counts %s', counts)
    return counts


def schedule_recovery(db_path, *, rate_limited=False):
    """Startup/poll hook: schedule one daemon, return immediately on failure."""
    global _scheduled
    with _schedule_lock:
        clock = time.monotonic()
        key = str(db_path)
        if _scheduled or (rate_limited and clock - _last_poll.get(key, float('-inf')) < POLL_SECONDS):
            return False
        _scheduled = True
        _last_poll[key] = clock

    def run():
        global _scheduled
        try:
            recover_jobs(db_path, now=datetime.now(timezone.utc))
        finally:
            with _schedule_lock:
                _scheduled = False

    try:
        threading.Thread(target=run, name='job-recovery', daemon=True).start()
        return True
    except Exception:
        with _schedule_lock:
            _scheduled = False
        _logger.warning('job recovery scheduling unavailable')
        return False
