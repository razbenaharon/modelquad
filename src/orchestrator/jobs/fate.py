# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Read-only, bounded proof collection. Process lists never prove worker death."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import time
import uuid

from orchestrator import procutil

_pass_cursors = {}  # In-memory scan fairness only, never persisted as audit.


def valid_pid(value):
    return type(value) is int and 0 < value <= 0xffffffff


def valid_created(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def identity(pid, recorded, probe=procutil):
    """Recovery's identity rule, except a dead valid PID needs no start time."""
    if not valid_pid(pid):
        return 'unknown', None
    try:
        state = probe.pid_state(pid)
        if state == 'dead':
            return 'gone', None
        if state != 'alive':
            return 'unknown', None
        read = (probe.native_process_creation_epoch if probe is procutil
                else probe.process_creation_epoch)
        observed = read(pid)
        if not valid_created(observed) or not valid_created(recorded):
            return 'unknown', None
        return ('alive' if observed == recorded else 'reused'), observed
    except Exception:
        return 'unknown', None


def runner_positively_live(job, probe=procutil):
    """Missing creation time cannot disguise a positively live runner PID."""
    if not valid_pid(job['runner_pid']):
        return False
    try:
        state = probe.pid_state(job['runner_pid'])
        if state != 'alive':
            return False
        verdict, _ = identity(job['runner_pid'], job['runner_created'], probe)
        return verdict != 'reused'
    except Exception:
        # Refuse release if the runner observation itself failed.
        return True


@dataclass(frozen=True)
class Proof:
    reason: str
    observed_at: datetime
    pid: int | None = None
    recorded_created: float | None = None
    observed_created: float | None = None
    boot: procutil.BootObservation | None = None


def collect_proof(job, *, now, probe=procutil, boot=None):
    """UTC ordering is conservative presentation evidence, never sole proof."""
    if boot is not None and isinstance(boot, procutil.BootObservation):
        try:
            start = datetime.fromisoformat(job['created_at'])
            if (job.get('reservation_boot_qualified') == 1
                    and job.get('reservation_boot_id')
                    and boot.qualified is True
                    and boot.session != job['reservation_boot_id']
                    and str(uuid.UUID(boot.session)) == boot.session
                    and str(uuid.UUID(job['reservation_boot_id'])) == job['reservation_boot_id']
                    and type(boot.uptime_ms) is int and 0 <= boot.uptime_ms <= 2**63 - 1
                    and type(job.get('reservation_uptime_ms')) is int
                    and job['reservation_uptime_ms'] >= 0
                    and boot.boot_lower > start
                    and boot.boot_upper >= boot.boot_lower
                    and boot.observed_at <= now):
                return Proof('boot_after_start', boot.observed_at, boot=boot)
        except (ValueError, TypeError):
            pass
    verdict, observed = identity(job['worker_pid'], job['worker_created'], probe)
    if verdict == 'gone':
        return Proof('worker_pid_gone', now, pid=job['worker_pid'])
    if verdict == 'reused':
        return Proof('worker_pid_reused', now, pid=job['worker_pid'],
                     recorded_created=job['worker_created'], observed_created=observed)
    return None


def release_pass(db_path, *, now, probe=procutil, boot_observer=None,
                 deadline=None, limit=64, monotonic=time.monotonic, clock=None):
    """No sink, launch, stop or answer reconstruction. Budget between operations."""
    from orchestrator.service import fate_history as store
    deadline = monotonic() + 5 if deadline is None else deadline
    clock = clock or ((lambda: datetime.now(timezone.utc)) if probe is procutil else lambda: now)
    counts = {'released': 0, 'unknown': 0, 'refused': 0}
    boot = (boot_observer or procutil.observe_boot)()
    key = str(db_path)
    targets = store.candidates(db_path, limit=limit, after=_pass_cursors.get(key))
    if not targets and key in _pass_cursors:
        _pass_cursors.pop(key, None)
        targets = store.candidates(db_path, limit=limit)
    for target in targets:
        if monotonic() >= deadline:
            break
        _pass_cursors[key] = (target['created_at'], target['job_id'])
        try:
            snapshot = store.snapshot(db_path, target['job_id'])
            observed_at = clock()
            proof = collect_proof(snapshot.job, now=observed_at, probe=probe, boot=boot)
            if proof is None:
                counts['unknown'] += 1
                continue
            store.release(db_path, snapshot, proof=proof, now=clock(), actor='local_recovery',
                          authentication_method='local_recovery', surface='recovery', probe=probe)
            counts['released'] += 1
        except store.FateError:
            counts['refused'] += 1
    return counts
