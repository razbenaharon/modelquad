# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline F1 behavior, written but not executed by the implementation worker."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Barrier, Event
from types import SimpleNamespace
import uuid

import pytest

from modelquad import procutil
from modelquad.provenance import new_decision_id
from modelquad.jobs import fate, recovery
from modelquad.jobs.seam import JobRef
from modelquad.service import db, history, fate_history as fh

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
START = NOW - timedelta(hours=1)
BOOT_A = str(uuid.UUID(int=1))
BOOT_B = str(uuid.UUID(int=2))
ACTOR = 'installation:' + 'a' * 12


class Probe:
    def __init__(self, *, worker='unknown', created=None, runner='dead'):
        self.states = {101: runner, 202: worker}
        self.starts = {101: 10.0, 202: created}

    def pid_state(self, pid):
        return self.states.get(pid, 'unknown')

    def process_creation_epoch(self, pid):
        return self.starts.get(pid)

    def _terminate_exact(self, *args):
        raise AssertionError('release must never stop a process')


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(procutil, 'observe_boot', lambda: None)
    monkeypatch.setattr(procutil, 'pid_state', lambda _: 'unknown')
    monkeypatch.setattr(procutil, '_windows_process_snapshot', lambda: None)
    monkeypatch.setattr(procutil, 'native_process_creation_epoch', lambda _: None)
    path = tmp_path / 'fate.sqlite'
    db.connect_and_init(path).close()

    def add(index=1, *, directory=tmp_path, status='unknown', terminal=False, descendants=None,
            decision_form='dashed'):
        job, decision = str(uuid.UUID(int=100 + index)), str(uuid.UUID(int=200 + index))
        if decision_form == 'hex':
            decision = new_decision_id()
        workspace, ancestors = history.workspace_identity(directory)
        with sqlite3.connect(path) as conn:
            conn.execute('INSERT INTO delegation_attempts '
                         '(id,decision_id,created_at,task_type,status,resolved_at,failure_kind,workspace_key,workspace_ancestors,worker_descendants) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (index, decision, START.isoformat(), 'exploration', 'failed' if terminal else 'started',
                          NOW.isoformat() if terminal else None, 'timeout' if terminal else None,
                          workspace, ancestors, json.dumps(descendants) if descendants is not None else None))
            conn.execute('INSERT INTO jobs '
                         '(job_id,attempt_id,decision_id,status,created_at,updated_at,runner_pid,runner_created,worker_pid,worker_created,recovery_decision,ended_at) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (job, index, decision, status, START.isoformat(), START.isoformat(), 101, 10.0,
                          202, 20.0, 'unknown' if status == 'unknown' else None,
                          NOW.isoformat() if terminal else None))
        return job

    return SimpleNamespace(path=path, root=tmp_path, add=add)


def manual(lab, job, *, probe=None, snapshot=None):
    return fh.release(lab.path, snapshot or fh.snapshot(lab.path, job),
                      proof=fate.Proof('owner_released_unknown_fate', NOW), now=NOW,
                      actor=ACTOR, authentication_method='installation_token', surface='cli',
                      probe=probe or Probe())


def sql(lab, query, args=()):
    with sqlite3.connect(lab.path) as conn:
        conn.execute(query, args)


@pytest.mark.parametrize('state,observed,recorded,reason', [
    ('dead', None, None, 'worker_pid_gone'),
    ('dead', None, 20.0, 'worker_pid_gone'),
    ('alive', 30.0, 20.0, 'worker_pid_reused'),
    ('alive', 20.0, 20.0, None),
    ('alive', None, 20.0, None),
    ('alive', 30.0, None, None),
    ('unknown', None, 20.0, None),
], ids=['gone-no-start', 'gone', 'reused', 'same-live', 'unreadable-start', 'missing-start', 'denied-ambiguous'])
@pytest.mark.parametrize('decision_form', ['dashed', 'hex'])
def test_pid_proof_and_atomic_automatic_release(lab, state, observed, recorded, reason, decision_form):
    job = lab.add(decision_form=decision_form)
    sql(lab, 'UPDATE jobs SET worker_created=?', (recorded,))
    snap = fh.snapshot(lab.path, job)
    probe = Probe(worker=state, created=observed)
    proof = fate.collect_proof(snap.job, now=NOW, probe=probe)
    assert (proof.reason if proof else None) == reason
    if proof:
        audit = fh.release(lab.path, snap, proof=proof, now=NOW, actor='local_recovery',
                           authentication_method='local_recovery', surface='recovery', probe=probe)
        assert audit['reason'] == reason and audit['proof_pid'] == 202
        attempt = history.read_job_attempt(lab.path, 1)
        assert (attempt['status'], attempt['failure_kind'], attempt['outcome']) == ('failed','fate_released','unknown')
        assert history.read_job(lab.path, job)['recovery_decision'] == 'unknown'
    else:
        assert history.read_job(lab.path, job)['ended_at'] is None


@pytest.mark.parametrize('pid', [None, 0, -1, '202', True, 2**32])
def test_missing_and_invalid_pid_never_proves_death(lab, pid):
    job = lab.add()
    row = fh.snapshot(lab.path, job).job | {'worker_pid': pid}
    assert fate.collect_proof(row, now=NOW, probe=Probe(worker='dead')) is None


def boot(*, session=BOOT_B, lower=START + timedelta(minutes=1), qualified=True):
    return procutil.BootObservation(session, 1000, NOW, lower, lower + timedelta(milliseconds=32), qualified)


@pytest.mark.parametrize('marker,lower,qualified,legacy,expected', [
    (BOOT_B, START + timedelta(seconds=1), True, False, True),
    (BOOT_B, START - timedelta(seconds=1), True, False, False),
    (BOOT_B, START, True, False, False),
    (BOOT_B, START - timedelta(milliseconds=16), True, False, False),
    (BOOT_A, START + timedelta(hours=1), True, False, False),
    (BOOT_A, START - timedelta(days=1), True, False, False),
    (BOOT_B, START + timedelta(seconds=1), False, False, False),
    (BOOT_B, START + timedelta(seconds=1), True, True, False),
], ids=['post-start', 'pre-start', 'equal', 'overlap', 'forward-clock-same-session',
        'backward-clock-sleep-resume', 'fast-startup-unqualified', 'legacy'])
def test_boot_requires_independent_session_and_strict_interval(lab, marker, lower, qualified, legacy, expected):
    job = lab.add()
    if not legacy:
        sql(lab, 'UPDATE jobs SET reservation_boot_id=?,reservation_uptime_ms=100000,reservation_boot_qualified=1', (BOOT_A,))
    snap = fh.snapshot(lab.path, job)
    proof = fate.collect_proof(snap.job, now=NOW, probe=Probe(), boot=boot(session=marker, lower=lower, qualified=qualified))
    assert (proof is not None) == expected
    if expected:
        audit = fh.release(lab.path, snap, proof=proof, now=NOW, actor='local_recovery',
                           authentication_method='local_recovery', surface='recovery', probe=Probe())
        assert audit['reason'] == 'boot_after_start' and audit['observed_boot_id'] == BOOT_B
    assert fate.collect_proof(snap.job, now=NOW, probe=Probe(), boot=None) is None


def test_boot_observation_brackets_ticks_and_rejects_clock_or_session_changes():
    times = iter([NOW, NOW + timedelta(milliseconds=20)])
    samples = iter([(BOOT_A,100000,True), (BOOT_A,100010,True)])
    observed = procutil.observe_boot(clock=lambda: next(times), sample=lambda: next(samples))
    assert observed.boot_lower == NOW - timedelta(milliseconds=100026)
    assert observed.boot_upper == NOW + timedelta(milliseconds=20) - timedelta(milliseconds=99984)
    for end, marker in [(NOW - timedelta(seconds=1), BOOT_A), (NOW + timedelta(seconds=2), BOOT_A), (NOW, BOOT_B)]:
        times = iter([NOW, end])
        samples = iter([(BOOT_A,100000,True), (marker,100001,True)])
        assert procutil.observe_boot(clock=lambda: next(times), sample=lambda: next(samples)) is None
    assert procutil.observe_boot(clock=lambda: NOW, sample=lambda: None) is None


def test_reservation_requires_consistent_bracket_without_legacy_backfill():
    before = boot(session=BOOT_A)
    after = replace(before, observed_at=NOW + timedelta(milliseconds=20), uptime_ms=1020)
    assert history.reservation_boot_anchors(before, after, reserved_at=NOW.isoformat()) == (BOOT_A,1000,1)
    assert history.reservation_boot_anchors(before, replace(after,session=BOOT_B)) == (None,None,None)
    assert history.reservation_boot_anchors(before, after, reserved_at=START.isoformat()) == (None,None,None)


@pytest.mark.parametrize('decision_form', ['dashed', 'hex'])
def test_manual_release_preserves_terminal_attempt_and_is_idempotent(lab, decision_form):
    job = lab.add(terminal=True, decision_form=decision_form)
    snap = fh.snapshot(lab.path, job)
    audit = manual(lab, job)
    assert audit['job_id'] == job and audit['attempt_id'] == 1 and audit['decision_id'] == snap.job['decision_id']
    assert audit['actor'] == ACTOR and audit['authentication_method'] == 'installation_token'
    assert audit['surface'] == 'cli' and audit['released_at'] == NOW.isoformat()
    assert fh.snapshot(lab.path, job).attempt == snap.attempt
    assert manual(lab, job, snapshot=snap) == audit
    with sqlite3.connect(lab.path) as conn:
        assert conn.execute('SELECT count(*) FROM job_fate_releases').fetchone()[0] == 1
        for action in ('UPDATE job_fate_releases SET actor=actor', 'DELETE FROM job_fate_releases'):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(action)


@pytest.mark.parametrize('automatic', [False, True], ids=['owner', 'automatic'])
@pytest.mark.parametrize('other_job', [False, True], ids=['auxiliary', 'job'])
def test_multiple_attempts_release_only_exact_target(lab, monkeypatch, automatic, other_job):
    job = lab.add()
    other = lab.add(2)
    decision = fh.snapshot(lab.path, job).job['decision_id']
    sql(lab, 'UPDATE delegation_attempts SET decision_id=? WHERE id=2', (decision,))
    sql(lab, 'UPDATE jobs SET decision_id=? WHERE attempt_id=2', (decision,))
    if not other_job:
        sql(lab, 'DELETE FROM jobs WHERE job_id=?', (other,))
    before = history.read_job_attempt(lab.path, 2)
    other_before = history.read_job(lab.path, other)
    observed = fh.snapshot(lab.path, job)
    if automatic:
        # This assertion targets the first page; a retained fairness cursor
        # may legitimately select the other job. Isolate that scan state.
        monkeypatch.setattr(fate, '_pass_cursors', {})
        assert fh.candidates(lab.path, limit=1)[0]['job_id'] == job
        assert fate.release_pass(lab.path, now=NOW, probe=Probe(worker='dead'),
                                 boot_observer=lambda: None, monotonic=lambda: 0,
                                 limit=1)['released'] == 1
    else:
        manual(lab, job)
    with fh.connection(lab.path) as conn:
        audit = fh.validated_release(conn, job)
        assert audit['attempt_id'] == observed.attempt['id']
        assert audit['decision_id'] == decision
        if other_job:
            assert fh.validated_release(conn, other) is None
    assert history.read_job_attempt(lab.path, 2) == before
    assert history.read_job(lab.path, other) == other_before
    assert history.workspace_delegation_liveness(lab.path, lab.root)['verdict'] != 'no_delegation_in_flight'
    assert history.diagnosis_admission(lab.path, lab.root)['allowed'] is False


def test_resumed_release_pass_preserves_other_attempts(lab, monkeypatch):
    jobs = [lab.add(index) for index in (1, 2, 3)]
    before = {job: fh.snapshot(lab.path, job) for job in jobs}
    monkeypatch.setattr(fate, '_pass_cursors', {
        str(lab.path): (START.isoformat(), jobs[0]),
    })
    released = set()
    # A resumed scan starts after the cursor, then wraps only at the end.
    for target in (jobs[1], jobs[2], jobs[0]):
        assert fate.release_pass(lab.path, now=NOW, probe=Probe(worker='dead'),
                                 boot_observer=lambda: None, monotonic=lambda: 0,
                                 limit=1)['released'] == 1
        with fh.connection(lab.path) as conn:
            audit = fh.validated_release(conn, target)
            assert audit['attempt_id'] == before[target].attempt['id']
            assert audit['decision_id'] == before[target].job['decision_id']
        before[target] = fh.snapshot(lab.path, target)
        released.add(target)
        for other, observed in before.items():
            assert fh.snapshot(lab.path, other) == observed
            if other not in released:
                with fh.connection(lab.path) as conn:
                    assert fh.validated_release(conn, other) is None


def test_manual_matching_live_worker_does_not_certify_tree_death(lab):
    job = lab.add()
    manual(lab, job, probe=Probe(worker='alive', created=20.0))
    assert history.workspace_delegation_liveness(lab.path,lab.root)['verdict'] == 'unknown'
    assert history.diagnosis_admission(lab.path,lab.root)['allowed'] is True
    assert history.read_job_attempt(lab.path,1)['worker_descendants'] is None


@pytest.mark.parametrize('change,args,label', [
    ('UPDATE jobs SET heartbeat_at=?', (NOW.isoformat(),), 'fate_snapshot_changed'),
    ('UPDATE delegation_attempts SET detail=?', ('changed',), 'fate_snapshot_changed'),
    ('UPDATE jobs SET decision_id=?', (str(uuid.UUID(int=999)),), 'fate_linkage_invalid'),
    ('UPDATE delegation_attempts SET workspace_key=NULL', (), 'fate_linkage_invalid'),
])
def test_stale_snapshot_and_malformed_linkage_refuse(lab,change,args,label):
    job = lab.add()
    observed = fh.snapshot(lab.path,job)
    sql(lab,change,args)
    with pytest.raises(fh.FateError,match=label):
        manual(lab,job,snapshot=observed)
    assert history.read_job(lab.path,job)['ended_at'] is None


def test_positively_live_runner_and_audit_failure_leave_block(lab):
    job = lab.add()
    with pytest.raises(fh.FateError,match='fate_runner_alive'):
        manual(lab,job,probe=Probe(runner='alive'))
    sql(lab, "CREATE TRIGGER audit_failure BEFORE INSERT ON job_fate_releases BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(fh.FateError,match='fate_store_unavailable'):
        manual(lab,job)
    snap = fh.snapshot(lab.path,job)
    assert snap.job['ended_at'] is None and snap.attempt['status'] == 'started'


def test_concurrent_releases_return_one_original_record(lab):
    job = lab.add()
    observed = fh.snapshot(lab.path,job)
    barrier = Barrier(2)
    def write():
        barrier.wait(timeout=5)
        return manual(lab,job,snapshot=observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: write(), range(2)))
    assert a == b


@pytest.mark.parametrize('decision_form', ['dashed', 'hex'])
def test_late_lifecycle_recovery_and_spawn_writes_are_fenced(lab, decision_form):
    job = lab.add(decision_form=decision_form)
    prior = fh.snapshot(lab.path,job)
    manual(lab,job)
    with pytest.raises(ValueError):
        history.update_job(lab.path,job,status='running')
    assert not history.update_job_startup(lab.path,job,startup_seconds=15,runner_pid=101,runner_created=10.0)
    assert history.claim_job_recovery(lab.path,prior.job,token='late',now=NOW,lease_seconds=60) is None
    assert not history.resolve_delegation_attempt(lab.path,1,status='completed',only_if_open=True)
    history.resolve_delegation_attempt(lab.path,1,status='completed')
    history.record_worker_pid(lab.path,1,999)
    with pytest.raises(ValueError):
        with history.job_spawn_fence(lab.path,JobRef(job,1,prior.job['decision_id'])):
            pytest.fail('released jobs cannot reach spawning')
    assert fh.snapshot(lab.path,job).attempt['failure_kind'] == 'fate_released'


@pytest.mark.parametrize('relation', ['exact','ancestor','descendant'])
def test_release_is_scoped_other_jobs_and_unscoped_hazards_remain(lab,relation):
    folder = {'exact':lab.root, 'ancestor':lab.root.parent, 'descendant':lab.root/'child'}[relation]
    first = lab.add(directory=folder)
    manual(lab,first)
    assert history.diagnosis_admission(lab.path,lab.root)['allowed'] is True
    lab.add(2)
    assert history.diagnosis_admission(lab.path,lab.root)['allowed'] is False
    sql(lab,'UPDATE delegation_attempts SET workspace_key=NULL WHERE id=2')
    assert history.diagnosis_admission(lab.path,lab.root)['allowed'] is False


@pytest.mark.parametrize('enumerated,truncated', [(True,False),(True,True),(False,False)])
def test_live_descendant_remains_independent_hazard(lab,monkeypatch,enumerated,truncated):
    job = lab.add(descendants={'enumerated':enumerated,'truncated':truncated,
                              'descendants':[{'pid':303,'image':'codex.exe','created':30.0}]})
    manual(lab,job)
    monkeypatch.setattr(procutil,'pid_state',lambda pid: 'alive' if pid == 303 else 'dead')
    monkeypatch.setattr(procutil,'_windows_process_snapshot',lambda: [(303,0,'codex.exe')])
    monkeypatch.setattr(procutil,'native_process_creation_epoch',lambda _:30.0)
    monkeypatch.setattr(procutil,'process_creation_epoch',lambda _:30.0)
    monkeypatch.setattr(procutil,'process_image_name',lambda _:'codex.exe')
    assert history.diagnosis_admission(lab.path,lab.root)['allowed'] is False


def test_released_history_cannot_exhaust_scan_caps(lab,monkeypatch):
    for index in range(1,6):
        manual(lab,lab.add(index,terminal=True))
    monkeypatch.setattr(history,'_WORKSPACE_SCAN_LIMIT',2)
    assert history.diagnosis_admission(lab.path,lab.root)['allowed'] is True
    assert history.workspace_delegation_liveness(lab.path,lab.root)['scan_truncated'] is False


@pytest.mark.parametrize('diagnosis', [False, True], ids=['liveness', 'diagnosis'])
def test_changing_descendant_observation_keeps_first_live_hazard(lab, monkeypatch, diagnosis):
    job = lab.add(descendants={'enumerated': True, 'truncated': False,
                              'descendants': [{'pid': 303, 'image': 'codex.exe', 'created': 30.0}]})
    manual(lab, job)
    calls = []

    def changing_probe(*args, **kwargs):
        calls.append(True)
        return ('may_be_mutating', 'recorded descendant is alive') if len(calls) == 1 else ('unknown', 'identity unreadable')

    monkeypatch.setattr(history, '_descendant_hazard', changing_probe)
    if diagnosis:
        assert history.diagnosis_admission(lab.path, lab.root)['allowed'] is False
    else:
        result = history.workspace_delegation_liveness(lab.path, lab.root)
        assert result['verdict'] == 'may_be_mutating'
        assert result['attempts'][0]['workspace_hazard'] == 'may_be_mutating'
    assert calls == [True]


@pytest.mark.parametrize('decision_form', ['dashed', 'hex'])
@pytest.mark.parametrize('status', ['unknown', 'timed_out'])
def test_bounded_pass_visits_ended_unknown_jobs_without_sink_or_stop(lab, decision_form, status):
    job = lab.add(terminal=True, decision_form=decision_form, status=status)
    before = fh.snapshot(lab.path, job)
    counts = fate.release_pass(lab.path,now=NOW,probe=Probe(worker='dead'),boot_observer=lambda:None,
                               monotonic=lambda:0)
    assert counts['released'] == 1
    with fh.connection(lab.path) as conn:
        audit = fh.validated_release(conn, job)
        assert audit['reason'] == 'worker_pid_gone'
        assert audit['decision_id'] == before.job['decision_id']
        assert audit['actor'] == audit['authentication_method'] == 'local_recovery'
        assert audit['surface'] == 'recovery' and audit['proof_pid'] == 202
    after = fh.snapshot(lab.path, job)
    assert after.attempt == before.attempt
    assert after.job == before.job | {'updated_at': NOW.isoformat()}
    assert fh.candidates(lab.path) == []


def test_release_pass_boot_observation_after_pass_start_uses_later_clock(lab):
    job = lab.add(terminal=True)
    sql(lab,'UPDATE jobs SET reservation_boot_id=?,reservation_uptime_ms=100000,reservation_boot_qualified=1',(BOOT_A,))
    later = NOW + timedelta(seconds=1)
    observed = replace(boot(),observed_at=later)
    counts = fate.release_pass(lab.path,now=NOW,probe=Probe(),boot_observer=lambda:observed,
                               monotonic=lambda:0,clock=lambda:later)
    assert counts['released'] == 1
    with fh.connection(lab.path) as conn:
        assert fh.validated_release(conn,job)['observed_at'] == later.isoformat()


def test_pending_answer_is_preserved_and_late_publication_refuses(lab):
    from modelquad.jobs.store import FileOutputStore
    from modelquad.jobs.seam import JobOutput, JobStatus
    from modelquad.execution.base import WorkerTaskStatus
    job = lab.add(terminal=True)
    snap = fh.snapshot(lab.path,job)
    sink = FileOutputStore(lab.root/'answers')
    output = JobOutput(JobRef(job,1,snap.job['decision_id']),JobStatus.TIMED_OUT,
                       WorkerTaskStatus.PARTIAL,'retained','private synthetic answer',
                       True,None,None,None,START,NOW)
    sink.put(output,NOW)
    before = sink.collect(job,NOW)
    manual(lab,job)
    assert sink.collect(job,NOW) == before
    with pytest.raises(ValueError,match='job fate released'):
        with history.job_publication_fence(lab.path,job):
            pytest.fail('late publication cannot reach the sink')


def test_release_waits_for_publication_without_locking_lifecycle_db(lab, monkeypatch):
    from contextlib import contextmanager
    from modelquad.jobs.seam import JobOutput, JobStatus
    from modelquad.jobs.store import FileOutputStore
    from modelquad.execution.base import WorkerTaskStatus

    job = lab.add(terminal=True)
    snap = fh.snapshot(lab.path, job)
    output = JobOutput(JobRef(job, 1, snap.job['decision_id']), JobStatus.TIMED_OUT,
                       WorkerTaskStatus.PARTIAL, 'retained', 'synthetic answer',
                       True, None, None, None, START, NOW)
    sink = FileOutputStore(lab.root / 'answers')
    waiting = Event()
    original = fh.publication_lock

    @contextmanager
    def release_lock(path):
        waiting.set()
        with original(path):
            yield

    with ThreadPoolExecutor(max_workers=1) as pool:
        with history.job_publication_fence(lab.path, job):
            monkeypatch.setattr(fh, 'publication_lock', release_lock)
            pending = pool.submit(manual, lab, job)
            assert waiting.wait(timeout=5)
            assert not pending.done()
            # A separate SQLite writer remains available while publication
            # owns the fence; it does not change this exact release snapshot.
            unrelated = lab.add(2)
            history.update_job(lab.path, unrelated, status='timed_out', ended_at=NOW.isoformat())
            assert sink.put(output, NOW)
        audit = pending.result(timeout=5)
    assert audit['job_id'] == job
    assert sink.collect(job, NOW).output == output
    assert fh.snapshot(lab.path, job).attempt == snap.attempt


def test_publication_lock_failure_refuses_release_and_sink_entry(lab, monkeypatch):
    from modelquad.usage import base

    job = lab.add()
    before = fh.snapshot(lab.path, job)
    monkeypatch.setattr(base, '_claim_snapshot_lock', lambda *a, **k: None)
    with pytest.raises(fh.FateError, match='fate_store_unavailable'):
        manual(lab, job)
    with pytest.raises(fh.FateError, match='fate_store_unavailable'):
        with history.job_publication_fence(lab.path, job):
            pytest.fail('an unavailable fence cannot permit publication')
    assert fh.snapshot(lab.path, job) == before
    with fh.connection(lab.path) as conn:
        assert fh.validated_release(conn, job) is None


def test_clean_finished_job_is_not_a_fate_blocker(lab):
    job = lab.add(terminal=True,status='finished',descendants={'enumerated':True,'truncated':False,'descendants':[]})
    sql(lab,'UPDATE delegation_attempts SET status=\'completed\',failure_kind=NULL')
    assert fh.candidates(lab.path) == []
    with pytest.raises(fh.FateError,match='fate_not_unknown'):
        manual(lab,job)


@pytest.mark.parametrize('job_form,attempt_form', [
    ('dashed', 'dashed'), ('hex', 'hex'), ('dashed', 'hex'), ('hex', 'dashed'),
])
def test_linked_snapshot_normalizes_decision_ids_without_rewriting(lab, job_form, attempt_form):
    job = lab.add(terminal=True)
    decision = new_decision_id()
    forms = {'hex': decision, 'dashed': str(uuid.UUID(decision))}
    sql(lab, 'UPDATE jobs SET decision_id=?', (forms[job_form],))
    sql(lab, 'UPDATE delegation_attempts SET decision_id=?', (forms[attempt_form],))
    before = fh.snapshot(lab.path, job)
    with fh.connection(lab.path) as conn:
        assert fh.linked(conn, job) == before
    audit = manual(lab, job)
    assert audit['decision_id'] == forms[job_form]
    assert fh.snapshot(lab.path, job).attempt == before.attempt
    assert fh.snapshot(lab.path, job).job['decision_id'] == forms[job_form]
    with fh.connection(lab.path) as conn:
        assert fh.validated_release(conn, job) == audit


@pytest.mark.parametrize('audit_form', ['dashed', 'hex', 'different', 'malformed'])
def test_release_audit_validates_normalized_decision_identity(lab, audit_form):
    job = lab.add(terminal=True, decision_form='hex')
    decision = fh.snapshot(lab.path, job).job['decision_id']
    audit_decision = {'hex': decision, 'dashed': str(uuid.UUID(decision)),
                      'different': new_decision_id(), 'malformed': '{' + decision + '}'}[audit_form]
    sql(lab, 'INSERT INTO job_fate_releases '
             '(job_id,attempt_id,decision_id,observed_at,released_at,actor,authentication_method,surface,reason) '
             'VALUES (?,1,?,?,?,?,?,?,?)',
        (job, audit_decision, NOW.isoformat(), NOW.isoformat(), ACTOR,
         'installation_token', 'cli', 'owner_released_unknown_fate'))
    with fh.connection(lab.path) as conn:
        audit = fh.validated_release(conn, job)
    if audit_form in ('hex', 'dashed'):
        assert audit is not None and audit['decision_id'] == audit_decision
        assert fh.candidates(lab.path) == []
    else:
        assert audit is None
        assert [row['job_id'] for row in fh.candidates(lab.path)] == [job]


@pytest.mark.parametrize('column', ['jobs', 'delegation_attempts'])
@pytest.mark.parametrize('invalid', [
    None, '', 'a' * 31, 'a' * 33, 'g' * 32, 'A' * 32,
    '{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}',
    'urn:uuid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA',
    'aaaaaaaaaaaa-aaaa-aaaa-aaaaaaaaaaaa', ' ' + 'a' * 32,
])
def test_decision_id_validation_keeps_closed_set(lab, column, invalid):
    job = lab.add(decision_form='hex')
    sql(lab, f'UPDATE {column} SET decision_id=?', (invalid,))
    with pytest.raises(fh.FateError, match='fate_linkage_invalid'):
        fh.snapshot(lab.path, job)
    assert history.read_job(lab.path, job)['ended_at'] is None


def add_never_launched(lab, *, failure='failed_to_start', status='failed',
                       decision_form='hex', descendants=None):
    job = lab.add(terminal=True, status=status, decision_form=decision_form, descendants=descendants)
    sql(lab, "UPDATE delegation_attempts SET status=?,failure_kind=?,worker_pid=NULL",
        ('refused' if failure == 'policy_refused' else 'failed', failure))
    sql(lab, "UPDATE jobs SET runner_pid=NULL,runner_created=NULL,worker_pid=NULL,worker_created=NULL,"
             "launch_limitation='contained_innermost_job_without_kill_on_close'")
    return job


@pytest.mark.parametrize('decision_form', ['dashed', 'hex'])
@pytest.mark.parametrize('failure', ['failed_to_start', 'policy_refused',
                                    'provider_unavailable', 'no_viable_worker'])
@pytest.mark.parametrize('status', ['failed', 'unknown'])
def test_never_launched_attempt_is_not_candidate_or_rewritten(lab, decision_form, failure, status):
    job = add_never_launched(lab, failure=failure, status=status, decision_form=decision_form)
    before = fh.snapshot(lab.path, job)
    with fh.connection(lab.path) as conn:
        assert fh.eligible(conn, job) is False
    assert fh.candidates(lab.path) == []
    assert fate.release_pass(lab.path, now=NOW, probe=Probe(worker='dead'),
                             boot_observer=lambda: None, monotonic=lambda: 0) == {
        'released': 0, 'unknown': 0, 'refused': 0,
    }
    with pytest.raises(fh.FateError, match='fate_not_unknown'):
        manual(lab, job)
    assert fh.snapshot(lab.path, job) == before
    with fh.connection(lab.path) as conn:
        assert fh.validated_release(conn, job) is None


@pytest.mark.parametrize('evidence', ['attempt_pid', 'runner_pid', 'worker_pid',
                                    'invalid_pid', 'execution_hex', 'execution_dashed'])
def test_never_launched_label_cannot_hide_started_worker_evidence(lab, evidence):
    job = add_never_launched(lab)
    if evidence == 'attempt_pid':
        sql(lab, 'UPDATE delegation_attempts SET worker_pid=202')
    elif evidence in ('runner_pid', 'worker_pid'):
        sql(lab, f'UPDATE jobs SET {evidence}=202')
    elif evidence == 'invalid_pid':
        sql(lab, 'UPDATE jobs SET worker_pid=0')
    else:
        decision = fh.snapshot(lab.path, job).job['decision_id']
        if evidence == 'execution_dashed':
            decision = str(uuid.UUID(decision))
        sql(lab, 'INSERT INTO execution_history (created_at,task_type,provider,decision_id) VALUES (?,?,?,?)',
            (NOW.isoformat(), 'exploration', 'codex', decision))
    with fh.connection(lab.path) as conn:
        assert fh.eligible(conn, job) is True
    assert [row['job_id'] for row in fh.candidates(lab.path)] == [job]


def test_never_launched_label_cannot_hide_an_open_job(lab):
    # The spawn fence checks only the job row, so an open job may still start.
    job = add_never_launched(lab)
    sql(lab, 'UPDATE jobs SET ended_at=NULL')
    with fh.connection(lab.path) as conn:
        assert fh.eligible(conn, job) is True
    assert [row['job_id'] for row in fh.candidates(lab.path)] == [job]


@pytest.mark.parametrize('failure', ['timeout', 'orphaned', 'exception', None])
def test_missing_pids_without_never_launched_ending_remain_unknown(lab, failure):
    job = add_never_launched(lab, failure=failure)
    with fh.connection(lab.path) as conn:
        assert fh.eligible(conn, job) is True
    assert [row['job_id'] for row in fh.candidates(lab.path)] == [job]


@pytest.mark.parametrize('descendants', [
    {'enumerated': False, 'descendants': []},
    {'enumerated': True, 'truncated': True, 'descendants': []},
    {'enumerated': True, 'descendants': [{'pid': 303, 'image': 'codex.exe', 'created': 30.0}]},
    [],
])
def test_never_launched_label_cannot_hide_descendant_hazard(lab, descendants):
    job = add_never_launched(lab, descendants=descendants)
    assert [row['job_id'] for row in fh.candidates(lab.path)] == [job]


def test_started_worker_launch_limitation_remains_candidate(lab):
    job = lab.add(terminal=True, status='failed', decision_form='hex')
    sql(lab, 'UPDATE delegation_attempts SET failure_kind=NULL')
    sql(lab, "UPDATE jobs SET launch_limitation='contained_innermost_job_without_kill_on_close'")
    assert [row['job_id'] for row in fh.candidates(lab.path)] == [job]
