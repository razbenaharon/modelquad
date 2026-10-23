# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Bounded synthetic recovery, leases, privacy and crash-boundary controls."""

from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest
from process_diagnostics import non_killing_runner_spawn

from modelquad.jobs import recovery
from modelquad.jobs.seam import JobOutput, JobRef, JobStatus
from modelquad.jobs.store import FileOutputStore
from modelquad.execution.base import WorkerTaskStatus
from modelquad.service import db, history

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=5)

# Scheduling and teardown need slack on the shared, memory-constrained runner.
LOAD_HEADROOM_S = 60


class Probe:
    def __init__(self):
        self.states = {101: 'dead', 202: 'dead'}
        self.created = {101: 100.0, 202: 200.0}
        self.killed = []

    def pid_state(self, pid):
        return self.states.get(pid, 'unknown')

    def process_creation_epoch(self, pid):
        return self.created.get(pid)

    def _terminate_exact(self, pid, created):
        assert self.created.get(pid) == created
        self.killed.append((pid, created))
        self.states[pid] = 'dead'
        return True


@pytest.fixture
def case(tmp_path):
    path = tmp_path / 'jobs.sqlite'
    attempt = history.record_delegation_attempt(path, task_type='implementation', decision_id='decision')
    row = history.reserve_job(path, 'recovery-job', attempt)
    history.update_job(path, row['job_id'], runner_pid=101, runner_created=100.0,
                       worker_pid=202, worker_created=200.0, status='running', heartbeat_at=OLD.isoformat())
    conn = db.connect_and_init(path)
    conn.execute('UPDATE jobs SET created_at=?, updated_at=?', (OLD.isoformat(), OLD.isoformat()))
    conn.commit()
    conn.close()
    return path, attempt, FileOutputStore(tmp_path / 'answers'), Probe()


def run(case, now=NOW):
    path, _, sink, probe = case
    return recovery.recover_jobs(path, now=now, sink=sink, probe=probe)


def row(case):
    return history.read_job(case[0], 'recovery-job')


def finish(case, status='failed', kind='orphaned', detail=None):
    assert history.resolve_delegation_attempt(case[0], case[1], status=status,
                                              failure_kind=kind, detail=detail, only_if_open=True)


def answer(case, status=JobStatus.TIMED_OUT):
    item = JobOutput(JobRef('recovery-job', case[1], 'decision'), status,
                     WorkerTaskStatus.PARTIAL, 'answer reason', 'private partial output',
                     True, ('src/edit.py',), None, None, OLD, NOW)
    case[2].put(item, NOW)
    return item


def test_unresolved_dead_job_is_interrupted_and_collectable(case):
    assert run(case)['restored'] == 1
    assert row(case)['status'] == 'interrupted'
    attempt = history.read_job_attempt(case[0], case[1])
    assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'orphaned'
    assert attempt['detail'] == 'recovered_after_runner_loss'
    output = case[2].collect('recovery-job', NOW).output
    assert output.job_status is JobStatus.INTERRUPTED
    assert output.worker_claim is WorkerTaskStatus.UNKNOWN
    assert output.claim_reason == 'interrupted'
    assert output.output_text == '' and output.partial is False
    assert run(case)['restored'] == 0


def test_live_runner_untouched(case):
    case[3].states[101] = 'alive'
    before = row(case)
    assert run(case)['alive'] == 1
    assert row(case) == before
    assert case[3].killed == []


@pytest.mark.parametrize('identity', ['legacy', 'unknown', 'recycled'])
def test_root_identity_is_not_inferred(case, identity):
    case[3].states[202] = 'alive'
    if identity == 'legacy':
        history.update_job(case[0], 'recovery-job', worker_created=None)
    elif identity == 'unknown':
        case[3].created[202] = None
    else:
        case[3].created[202] = 201.0
    run(case)
    assert case[3].killed == []
    assert (row(case)['ended_at'] is not None) == (identity == 'recycled')


def test_surviving_root_stopped_but_unresolved_job_stays_unknown(case):
    case[3].states[202] = 'alive'
    assert run(case)['stopped_roots'] == 1
    assert case[3].killed == [(202, 200.0)]
    assert row(case)['status'] == 'unknown'
    assert history.read_job_attempt(case[0], case[1])['resolved_at'] is None
    assert run(case, NOW + timedelta(seconds=61))['unknown'] == 0
    assert row(case)['recovery_decision'] == 'unknown'
    assert row(case)['status'] == 'unknown' and row(case)['ended_at'] is None
    assert case[2].pending(NOW + timedelta(seconds=61)) == ()


def test_unconfirmed_root_stop_stays_unknown(case):
    case[3].states[202] = 'alive'
    case[3]._terminate_exact = lambda *args: False
    run(case)
    assert row(case)['status'] == 'unknown' and row(case)['ended_at'] is None


def test_outside_job_facts_never_killed_or_resolved(case):
    conn = db.connect_and_init(case[0])
    conn.execute('UPDATE delegation_attempts SET worker_descendants=? WHERE id=?',
                 (json.dumps({'enumerated': True, 'descendants': [{'pid': 303, 'created': 300.0}]}), case[1]))
    conn.commit()
    conn.close()
    run(case)
    assert row(case)['status'] == 'unknown'
    assert case[3].killed == []
    assert case[2].pending(NOW) == ()


def test_claim_fences_stale_owner_and_requires_old_heartbeat(case):
    path = case[0]
    snapshot = row(case)
    first = history.claim_job_recovery(path, snapshot, token='first', now=NOW, lease_seconds=60)
    assert first
    assert history.claim_job_recovery(path, first, token='second', now=NOW, lease_seconds=60) is None
    second = history.claim_job_recovery(path, first, token='second', now=NOW + timedelta(seconds=61), lease_seconds=60)
    assert second
    assert history.write_job_recovery(path, first, token='first', now=NOW, unknown=True) is None
    history.update_job(path, 'recovery-job', heartbeat_at=(NOW + timedelta(seconds=100)).isoformat())
    assert history.claim_job_recovery(path, row(case), token='third', now=NOW + timedelta(seconds=122), lease_seconds=60) is None


def test_runner_write_invalidates_claim_snapshot(case):
    claimed = history.claim_job_recovery(case[0], row(case), token='first', now=NOW, lease_seconds=60)
    history.update_job(case[0], 'recovery-job', heartbeat_at=NOW.isoformat())
    assert history.write_job_recovery(case[0], claimed, token='first', now=NOW, unknown=True) is None


def test_expiry_purged_even_with_no_candidates(case):
    item = answer(case)
    history.update_job(case[0], 'recovery-job', status='timed_out', ended_at=NOW.isoformat())
    assert run(case, NOW + timedelta(days=8))['expired'] == 1
    assert case[2].collect(item.job.job_id, NOW + timedelta(days=8)) is None


def test_background_schedule_returns_while_sink_is_blocked(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    returned = threading.Event()
    workers, results, errors = [], [], []
    native_thread = threading.Thread

    def tracked_thread(*args, **kwargs):
        worker = native_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    def factory(name):
        assert name == 'store'
        entered.set()
        release.wait()  # Only the test's finally block may unblock the sink.
        return case[2]

    def schedule():
        try:
            results.append(recovery.schedule_recovery(case[0]))
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()

    monkeypatch.setattr(recovery, 'output_sink_factory', factory)
    monkeypatch.setattr(recovery.threading, 'Thread', tracked_thread)
    caller = native_thread(target=schedule, daemon=True)
    caller.start()
    try:
        assert entered.wait(LOAD_HEADROOM_S)
        assert returned.wait(LOAD_HEADROOM_S), 'schedule blocked on the sink'
        assert not release.is_set() and results == [True] and not errors
        assert not recovery.schedule_recovery(case[0])
        assert run(case)['deferred'] == 1
    finally:
        release.set()
        caller.join(LOAD_HEADROOM_S)
        for worker in workers:
            worker.join(LOAD_HEADROOM_S)
        assert not caller.is_alive() and not any(worker.is_alive() for worker in workers)
    assert not recovery._scheduled


def test_deadline_checked_after_store_operation(case, monkeypatch):
    original = case[2].purge_expired
    clock = [0.0]
    def purge(now):
        result = original(now)
        clock[0] = 11.0
        return result
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(case[2], 'purge_expired', purge)
    before = row(case)
    assert run(case)['deferred'] == 1
    assert row(case) == before


@pytest.mark.parametrize('kind,detail,expected', [
    ('orphaned', None, 'interrupted'),
    ('timeout', None, 'timed_out'),
    ('terminated', 'stop_requested; confirmed', 'stopped'),
    ('terminated', 'unconfirmed stop_requested', 'timed_out'),
    ('terminated', None, 'timed_out'),
    ('timeout', 'stop_requested; unconfirmed', 'stopped'),
    ('exception', None, 'failed'),
])
def test_resolved_attempt_restores_outcome_and_collectable_output(case, kind, detail, expected):
    finish(case, kind=kind, detail=detail)
    assert run(case)['restored'] == 1
    assert row(case)['status'] == expected
    assert row(case)['ended_at'] == row(case)['recovery_ended_at']
    output = case[2].collect('recovery-job', NOW).output
    assert output.job_status.value == expected
    assert output.worker_claim is WorkerTaskStatus.UNKNOWN
    assert output.output_text == '' and output.partial is False
    assert output.claim_reason == ('interrupted' if expected == 'interrupted' else None)
    assert run(case)['restored'] == 0


def test_pending_output_precedes_conflicting_resolved_attempt(case):
    item = answer(case)
    finish(case, status='completed', kind=None)
    before_attempt = history.read_job_attempt(case[0], case[1])
    assert run(case)['restored'] == 1
    assert row(case)['status'] == 'timed_out'
    assert case[2].collect('recovery-job', NOW).output == item
    assert history.read_job_attempt(case[0], case[1]) == before_attempt


@pytest.mark.parametrize('removed', ['acknowledged', 'deleted', 'expired'])
def test_removed_answer_never_resurrected(case, removed):
    item = answer(case)
    finish(case, kind='timeout')
    clock = NOW
    if removed == 'acknowledged':
        receipt = case[2].collect(item.job.job_id, clock).receipt
        assert case[2].acknowledge(item.job.job_id, receipt, clock)
    elif removed == 'deleted':
        assert case[2].delete(item.job.job_id, clock)
    else:
        clock += timedelta(days=8)
    assert run(case, clock)['restored'] == 1
    assert row(case)['status'] == 'timed_out'
    assert case[2].collect(item.job.job_id, clock) is None
    assert case[2].state(item.job.job_id, clock) == removed


def test_recorded_terminal_timeout_untouched(case):
    item = answer(case)
    history.update_job(case[0], 'recovery-job', status='timed_out', ended_at=NOW.isoformat())
    before = row(case)
    assert run(case)['restored'] == 0
    assert row(case) == before
    assert case[2].collect(item.job.job_id, NOW).output == item


def test_starting_deadline_without_identity(case):
    conn = db.connect_and_init(case[0])
    conn.execute("UPDATE jobs SET status='starting', runner_pid=NULL, runner_created=NULL, "
                 "worker_pid=NULL, worker_created=NULL, heartbeat_at=NULL")
    conn.commit()
    conn.close()
    assert run(case)['restored'] == 1
    assert row(case)['status'] == 'failed'
    assert history.read_job_attempt(case[0], case[1])['failure_kind'] == 'failed_to_start'


def test_missing_runner_identity_leaves_nonterminal_job_untouched(case):
    history.update_job(case[0], 'recovery-job', runner_created=None)
    before = row(case)
    assert run(case)['deferred'] == 1
    assert row(case) == before
    assert case[2].pending(NOW) == ()


@pytest.mark.parametrize('stage', ['claimed', 'timestamp', 'put', 'finalized'])
def test_crash_boundary_retry_is_identical(case, monkeypatch, stage):
    finish(case)
    module, name = ((history, 'claim_job_recovery') if stage == 'claimed' else
                    (history, 'write_job_recovery') if stage == 'timestamp' else
                    (case[2], 'put') if stage == 'put' else
                    (history, 'finalize_job_recovery'))
    original = getattr(module, name)
    captured = []
    class Crash(BaseException):
        pass
    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        if name == 'put':
            captured.append(args[0])
        raise Crash
    monkeypatch.setattr(module, name, crash)
    with pytest.raises(Crash):
        run(case)
    monkeypatch.setattr(module, name, original)
    after = row(case)
    if stage != 'finalized':
        assert after['ended_at'] is None and after['recovery_claim']
        assert run(case, NOW + timedelta(seconds=59))['restored'] == 0
        assert run(case, NOW + timedelta(seconds=61))['restored'] == 1
    else:
        assert run(case, NOW + timedelta(seconds=61))['restored'] == 0
    result = case[2].collect('recovery-job', NOW + timedelta(seconds=61)).output
    if captured:
        assert result == captured[0]
    assert result.ended_at.isoformat() == row(case)['ended_at']


def test_racing_runner_final_write_wins(case, monkeypatch):
    finish(case)
    original = case[2].put
    def runner_wins(output, now):
        result = original(output, now)
        history.update_job(case[0], 'recovery-job', status='timed_out', ended_at=now.isoformat())
        return result
    monkeypatch.setattr(case[2], 'put', runner_wins)
    result = run(case)
    assert result['restored'] == 0 and result['failed'] == 0
    assert row(case)['status'] == 'timed_out'
    assert row(case)['ended_at'] == NOW.isoformat()


def test_two_concurrent_claims_only_one_owner(case):
    from concurrent.futures import ThreadPoolExecutor
    barrier = threading.Barrier(2)
    snapshot = row(case)
    def claim(token):
        barrier.wait(timeout=LOAD_HEADROOM_S)
        return history.claim_job_recovery(case[0], snapshot, token=token, now=NOW, lease_seconds=60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ('one', 'two')))
    assert sum(result is not None for result in results) == 1


def test_poll_hooks_rate_limited_and_worker_guarded(monkeypatch, case):
    from modelquad.adapters.pending_reminder import project_key
    from modelquad.jobs import service_client
    from modelquad.mcp import jobs_tools
    project = case[0].parent
    conn = db.connect_and_init(case[0])
    conn.execute('UPDATE delegation_attempts SET workspace_key=? WHERE id=?',
                 (project_key(str(project)), case[1]))
    conn.commit()
    conn.close()
    calls = []
    monkeypatch.setattr(jobs_tools, 'schedule_recovery', lambda path, **kw: calls.append((path, kw)))
    monkeypatch.setattr(jobs_tools.db, 'DEFAULT_DB_PATH', case[0])
    monkeypatch.setattr(jobs_tools, 'default_store', lambda: case[2])

    def no_service(*a, **kw):
        raise OSError('service absent')

    monkeypatch.setattr(service_client, 'read_pending_summary', no_service)
    assert jobs_tools.modelquad_job_status('recovery-job', str(project))['status'] == 'running'
    # The scoped pending list is a read-only fallback without the service:
    # it schedules no recovery of its own.
    assert jobs_tools.modelquad_job_list_pending(str(project))['running'][0]['job_id'] == 'recovery-job'
    assert calls == [(case[0], {'rate_limited': True})]
    # Another workspace's job is refused before any recovery is scheduled.
    other = project / 'elsewhere'
    other.mkdir()
    assert jobs_tools.modelquad_job_status('recovery-job', str(other))['status'] == 'not_found'
    assert calls == [(case[0], {'rate_limited': True})]


def test_schedule_rate_limit_with_inert_thread(monkeypatch, case):
    calls = []
    class Thread:
        def __init__(self, **kw):
            assert kw['daemon'] is True
            self.target = kw['target']
        def start(self):
            self.target()
    monkeypatch.setattr(recovery.threading, 'Thread', Thread)
    monkeypatch.setattr(recovery, 'recover_jobs', lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(recovery, '_last_poll', {})
    clock = [0.0]
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: clock[0])
    assert recovery.schedule_recovery(case[0], rate_limited=True)
    assert not recovery.schedule_recovery(case[0], rate_limited=True)
    clock[0] = 30
    assert recovery.schedule_recovery(case[0], rate_limited=True)
    assert len(calls) == 2


def test_service_and_mcp_startup_schedule_one_call(monkeypatch, case):
    from fastapi.testclient import TestClient
    from modelquad.service.app import create_app
    from modelquad.mcp import server
    calls = []
    monkeypatch.setattr(recovery, 'schedule_recovery', lambda path: calls.append(path))
    with TestClient(create_app(db_path=case[0], poll_providers=False)):
        assert calls == [case[0]]
    monkeypatch.setattr(server.service_db, 'DEFAULT_DB_PATH', case[0])
    monkeypatch.setattr(server.server, 'run', lambda: None)
    server.main()
    assert calls == [case[0], case[0]]


def test_recovery_logs_contain_no_output(case, caplog):
    item = answer(case)
    finish(case, kind='timeout')
    run(case)
    assert item.output_text not in caplog.text
    assert 'src/edit.py' not in caplog.text
    assert str(case[0]) not in caplog.text


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows exact-handle root termination')
def test_synthetic_surviving_root_terminated_by_exact_identity(case):
    import subprocess
    import sys
    from modelquad import procutil
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **procutil.no_window_kwargs())
    try:
        created = procutil.process_creation_epoch(child.pid)
        if created is None:
            pytest.skip('synthetic child identity unavailable')
        finish(case)
        history.update_job(case[0], 'recovery-job', worker_pid=child.pid, worker_created=created)
        # Runner identity is from a second test-owned process, now gone.
        parent = subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, **procutil.no_window_kwargs())
        parent_created = procutil.process_creation_epoch(parent.pid)
        parent.wait(timeout=5)
        if parent_created is None:
            pytest.skip('synthetic runner identity unavailable')
        history.update_job(case[0], 'recovery-job', runner_pid=parent.pid, runner_created=parent_created)
        counts = recovery.recover_jobs(case[0], now=NOW, sink=case[2], probe=procutil)
        child.wait(timeout=5)
        assert counts['unknown'] == 1
        assert row(case)['status'] == 'unknown' and row(case)['ended_at'] is None
        assert case[2].collect('recovery-job', NOW) is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows runner kill-on-close Job Object')
def test_hard_killed_contained_runner_stays_unknown(tmp_path, non_killing_runner_spawn):
    from modelquad.jobs import handle

    _assert_hard_killed_runner_recovery(
        tmp_path, non_killing_runner_spawn, handle.CONTAINED_RUNNER_LIMITATION)


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows runner kill-on-close Job Object')
def test_hard_killed_synthetic_runner_recovers_interrupted_output(tmp_path):
    import subprocess
    from modelquad import procutil
    from modelquad.jobs import handle

    in_job = handle._caller_in_job()
    limits = handle._caller_job_limits() if in_job else 0
    evidence = f'outer test-harness Job Object (in_job={in_job}, limit flags 0x{limits:x})'
    if in_job and not limits & 0x1800:
        pytest.skip(f'{evidence}; cannot establish a runner outside every job')
    breakaway = 0x01000000 if in_job and not limits & 0x1000 else 0

    def spawn(command, **kwargs):
        # Verify the actual admitted runner image before it executes any code.
        kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | 0x4 | breakaway
        try:
            child = subprocess.Popen(command, close_fds=True, **kwargs)
        except OSError as error:
            if error.winerror == 5:
                pytest.skip(f'{evidence}; outside-job runner creation denied: {error}')
            raise
        try:
            if handle._process_in_any_job(child._handle):
                pytest.skip(f'{evidence}; runner remains in an enclosing job despite breakaway')
            assert procutil._resume_suspended_process(child.pid), evidence
            return child
        except BaseException:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            raise

    _assert_hard_killed_runner_recovery(tmp_path, spawn, None)


def _assert_hard_killed_runner_recovery(tmp_path, spawn, expected_limitation):
    from dataclasses import asdict
    import os
    import site
    import sys
    from modelquad import procutil
    from modelquad.jobs.handle import JobSpec
    from modelquad.jobs import handle
    from process_diagnostics import capture_text, instrument_runner_command, note_failure, process_snapshot
    database = tmp_path / 'crash.sqlite'
    attempt = history.record_delegation_attempt(database, task_type='implementation')
    history.reserve_job(database, 'crash-job', attempt)
    source = __import__('pathlib').Path(__file__).resolve().parents[1] / 'src'
    imports = ([str(source)] if __import__('pathlib').Path(handle.__file__).resolve().is_relative_to(source) else []) + site.getsitepackages()
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(imports),
           'MODELQUAD_TEST_SINK_OK': '1'}
    spec = JobSpec((sys.executable, '-c', 'import time; time.sleep(20)'), str(tmp_path),
                   str(database), attempt, 20, sink='none')
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(asdict(spec)), encoding='utf-8')
    stages_path = tmp_path / 'runner-stages.jsonl'
    stdout_path, stderr_path = tmp_path / 'runner.stdout.log', tmp_path / 'runner.stderr.log'
    command = instrument_runner_command(
        handle._runner_command('crash-job', spec_path, database), stages_path)
    with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
        parent = spawn(command, env=env, stdout=stdout, stderr=stderr,
                       **procutil.no_window_kwargs())
    current = None
    termination_requested = False
    try:
        limit = time.monotonic() + 12
        while time.monotonic() < limit:
            current = history.read_job(database, 'crash-job')
            if current['worker_pid'] and current['worker_created']:
                break
            if parent.poll() is not None:
                break  # A dead runner cannot satisfy the required precondition.
            time.sleep(.05)
        assert current['worker_pid'] and current['worker_created']
        assert current['launch_limitation'] == expected_limitation
        # Ignore only an unfinished trailing record while instrumentation writes.
        stages = [json.loads(line) for line in stages_path.read_text(encoding='utf-8').split('\n')[:-1]]
        containment = [stage for stage in stages if stage['stage'] == 'containment']
        assert len(containment) == 1
        assert containment[0]['pid'] == parent.pid == current['runner_pid']
        assert containment[0]['in_job'] is (expected_limitation is not None)
        assert current['heartbeat_at'] is not None
        termination_requested = True
        assert procutil._terminate_exact(current['runner_pid'], current['runner_created'])
        parent.wait(timeout=5)
        limit = time.monotonic() + 5
        while procutil.pid_state(current['worker_pid']) == 'alive' and time.monotonic() < limit:
            time.sleep(.05)
        assert procutil.pid_state(current['worker_pid']) == 'dead'
        sink = FileOutputStore(tmp_path / 'crash-answers')
        counts = recovery.recover_jobs(database, now=datetime.now(timezone.utc), sink=sink, probe=procutil)
        if expected_limitation is not None:
            # Runner admission now records the real innermost limitation.
            # Root death cannot prove every outer-job/descendant fate: retain
            # the conservative recovery block and publish no invented answer.
            assert counts['unknown'] == 1 and counts['restored'] == 0
            result = history.read_job(database, 'crash-job')
            assert result['status'] == 'unknown' and result['ended_at'] is None
            assert result['recovery_decision'] == 'unknown'
            assert result['launch_limitation'] == current['launch_limitation']
            resolved = history.read_job_attempt(database, attempt)
            assert resolved['status'] == 'started' and resolved['resolved_at'] is None
            assert sink.collect('crash-job', datetime.now(timezone.utc)) is None
            again = recovery.recover_jobs(database, now=datetime.now(timezone.utc), sink=sink, probe=procutil)
            assert again['restored'] == 0
            assert history.read_job(database, 'crash-job')['ended_at'] is None
            return
        assert current['launch_limitation'] is None
        assert counts['restored'] == 1
        result = history.read_job(database, 'crash-job')
        assert result['status'] == 'interrupted' and result['ended_at'] is not None
        resolved = history.read_job_attempt(database, attempt)
        assert resolved['status'] == 'failed' and resolved['failure_kind'] == 'orphaned'
        assert resolved['detail'] == 'recovered_after_runner_loss'
        output = sink.collect('crash-job', datetime.now(timezone.utc)).output
        assert output.job_status is JobStatus.INTERRUPTED
        assert output.worker_claim is WorkerTaskStatus.UNKNOWN
        assert output.claim_reason == 'interrupted'
        assert output.output_text == '' and output.partial is False
    except BaseException as error:
        # Observe before forced cleanup, so cleanup's exit is not called a crash.
        note_failure(error, lambda: {
            'exit_before_cleanup': parent.poll(), 'job': current,
            'termination_requested': termination_requested,
            'processes': process_snapshot(parent.pid),
            'stages': capture_text(stages_path), 'stdout': capture_text(stdout_path),
            'stderr': capture_text(stderr_path),
        })
        raise
    finally:
        if current:
            for prefix in ('runner', 'worker'):
                pid, created = current.get(prefix + '_pid'), current.get(prefix + '_created')
                if pid and created and procutil.pid_state(pid) == 'alive':
                    procutil._terminate_exact(pid, created)
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)



def test_two_simultaneous_recoverer_processes(case):
    import os
    from pathlib import Path
    import site
    import subprocess
    import sys
    from modelquad import procutil
    finish(case)
    source = Path(__file__).resolve().parents[1] / 'src'
    imports = ([str(source)] if Path(recovery.__file__).resolve().is_relative_to(source) else []) + site.getsitepackages()
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(imports)}
    script = """
import json, sys
from datetime import datetime
from modelquad.jobs.recovery import recover_jobs
from modelquad.jobs.store import FileOutputStore
class Probe:
    def pid_state(self, pid): return 'dead'
    def process_creation_epoch(self, pid): raise AssertionError('dead identity needs no creation probe')
print(json.dumps(recover_jobs(sys.argv[1], now=datetime.fromisoformat(sys.argv[3]),
                             sink=FileOutputStore(sys.argv[2]), probe=Probe())))
"""
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, '-c', script, str(case[0]),
                                              str(case[2].root), NOW.isoformat()], env=env,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, **procutil.no_window_kwargs()))
        outputs = []
        for child in children:
            stdout, stderr = child.communicate(timeout=LOAD_HEADROOM_S)
            assert child.returncode == 0, stderr
            outputs.append(json.loads(stdout))
        assert sum(result['restored'] for result in outputs) == 1
        assert row(case)['status'] == 'interrupted'
        assert case[2].collect('recovery-job', NOW).output.claim_reason == 'interrupted'
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=LOAD_HEADROOM_S)


@pytest.mark.parametrize('status,attempt_status,kind', [
    (JobStatus.FINISHED, 'completed', None),
    (JobStatus.FAILED, 'failed', 'exception'),
    (JobStatus.STOPPED, 'failed', 'timeout'),
    (JobStatus.INTERRUPTED, 'failed', 'orphaned'),
    (JobStatus.TIMED_OUT, 'failed', 'timeout'),
])
def test_pending_output_finalizes_and_resolves_open_attempt(case, status, attempt_status, kind):
    item = answer(case, status)
    assert run(case)['restored'] == 1
    assert row(case)['status'] == status.value
    assert row(case)['ended_at'] == item.ended_at.isoformat()
    attempt = history.read_job_attempt(case[0], case[1])
    assert attempt['status'] == attempt_status and attempt['failure_kind'] == kind
    assert attempt['resolved_at'] == row(case)['ended_at']
    assert 'recovered_after_runner_loss' in attempt['detail']
    if status is JobStatus.STOPPED:
        assert 'stop_requested' in attempt['detail']
    assert case[2].collect('recovery-job', NOW).output == item
    assert run(case)['restored'] == 0


def prepared_claim(case):
    claimed = history.claim_job_recovery(case[0], row(case), token='owner', now=NOW, lease_seconds=60)
    return history.write_job_recovery(case[0], claimed, token='owner', now=NOW, ended=NOW,
                                      decision='restore_pending', status='timed_out')


@pytest.mark.parametrize('fence', ['token', 'terminal', 'runner_write'])
def test_combined_write_claim_mismatch_changes_neither_row(case, fence):
    claimed = prepared_claim(case)
    token = 'owner'
    if fence == 'token':
        token = 'stale'
    elif fence == 'terminal':
        history.update_job(case[0], 'recovery-job', status='timed_out', ended_at=NOW.isoformat())
    else:
        history.update_job(case[0], 'recovery-job', heartbeat_at=NOW.isoformat())
    before_job, before_attempt = row(case), history.read_job_attempt(case[0], case[1])
    assert not history.finalize_job_recovery(case[0], claimed, token=token,
                                              status='timed_out', now=NOW)
    assert row(case) == before_job
    assert history.read_job_attempt(case[0], case[1]) == before_attempt


@pytest.mark.parametrize('stage', ['job_write', 'attempt_write', 'commit'])
def test_transaction_crash_rolls_back_both_and_retry_completes(case, monkeypatch, stage):
    item = answer(case)
    claimed = prepared_claim(case)
    before_attempt = history.read_job_attempt(case[0], case[1])
    original = db.connect_and_init
    operations = []
    class Crash(BaseException):
        pass
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if sql.startswith('UPDATE jobs SET status='):
                operations.append('job_write')
                if stage == 'job_write':
                    raise Crash
            if sql.startswith('UPDATE delegation_attempts SET status='):
                operations.append('attempt_write')
                assert self.conn.execute('SELECT ended_at FROM jobs').fetchone()[0] == NOW.isoformat()
                if stage == 'attempt_write':
                    raise Crash
            return result
        def commit(self):
            if stage == 'commit':
                raise Crash
            return self.conn.commit()
    with monkeypatch.context() as patch:
        patch.setattr(db, 'connect_and_init', lambda path: Connection(original(path)))
        with pytest.raises(Crash):
            history.finalize_job_recovery(case[0], claimed, token='owner', status='timed_out', now=NOW)
    assert row(case)['ended_at'] is None and row(case)['recovery_claim'] == 'owner'
    assert history.read_job_attempt(case[0], case[1]) == before_attempt
    assert operations == (['job_write'] if stage == 'job_write' else ['job_write', 'attempt_write'])
    assert case[2].collect('recovery-job', NOW).output == item
    assert run(case, NOW + timedelta(seconds=61))['restored'] == 1
    assert row(case)['status'] == 'timed_out'
    attempt = history.read_job_attempt(case[0], case[1])
    assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'timeout'
    assert attempt['resolved_at'] == row(case)['ended_at'] == NOW.isoformat()
    assert case[2].collect('recovery-job', NOW + timedelta(seconds=61)).output == item


@pytest.mark.parametrize('removed', ['acknowledged', 'deleted', 'expired'])
def test_removed_output_with_open_attempt_stays_unknown(case, removed):
    item = answer(case)
    clock = NOW
    if removed == 'acknowledged':
        receipt = case[2].collect(item.job.job_id, clock).receipt
        case[2].acknowledge(item.job.job_id, receipt, clock)
    elif removed == 'deleted':
        case[2].delete(item.job.job_id, clock)
    else:
        clock += timedelta(days=8)
    assert run(case, clock)['unknown'] == 1
    assert row(case)['ended_at'] is None
    assert history.read_job_attempt(case[0], case[1])['resolved_at'] is None
    assert case[2].collect(item.job.job_id, clock) is None


def test_startup_expiry_has_no_output_even_with_resolved_attempt(case):
    history.update_job(case[0], 'recovery-job', status='starting', runner_pid=None,
                       runner_created=None, worker_pid=None, worker_created=None, heartbeat_at=None)
    finish(case, status='completed', kind=None)
    assert run(case)['restored'] == 1
    assert row(case)['status'] == 'failed'
    assert history.read_job_attempt(case[0], case[1])['status'] == 'completed'
    assert case[2].collect('recovery-job', NOW) is None


@pytest.mark.parametrize('stage', ['claimed', 'timestamp', 'job_write', 'attempt_write', 'commit', 'finalized'])
def test_startup_crash_retry_uses_only_persisted_job_fields(case, monkeypatch, stage):
    history.update_job(case[0], 'recovery-job', status='starting', runner_pid=None,
                       runner_created=None, worker_pid=None, worker_created=None, heartbeat_at=None)
    class Crash(BaseException):
        pass
    original_connect = db.connect_and_init
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if ((stage == 'job_write' and sql.startswith('UPDATE jobs SET status=')) or
                    (stage == 'attempt_write' and sql.startswith('UPDATE delegation_attempts SET status='))):
                raise Crash
            return result
        def commit(self):
            if stage == 'commit': raise Crash
            return self.conn.commit()
    name = {'claimed': 'claim_job_recovery', 'timestamp': 'write_job_recovery'}.get(stage, 'finalize_job_recovery')
    original = getattr(history, name)
    def crash(*args, **kwargs):
        if stage in {'job_write', 'attempt_write', 'commit'}:
            with monkeypatch.context() as patch:
                patch.setattr(db, 'connect_and_init', lambda path: Connection(original_connect(path)))
                return original(*args, **kwargs)
        result = original(*args, **kwargs)
        raise Crash
    monkeypatch.setattr(history, name, crash)
    with pytest.raises(Crash): run(case)
    monkeypatch.setattr(history, name, original)
    first_stamp = row(case)['recovery_ended_at']
    assert case[2].state('recovery-job', NOW) is None
    assert run(case, NOW + timedelta(seconds=61))['restored'] == (0 if stage == 'finalized' else 1)
    result = row(case)
    attempt = history.read_job_attempt(case[0], case[1])
    assert result['status'] == 'failed'
    assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'failed_to_start'
    assert attempt['detail'] == 'runner_startup_deadline_exceeded'
    assert attempt['resolved_at'] == result['ended_at'] == result['recovery_ended_at']
    if first_stamp: assert result['ended_at'] == first_stamp
    assert case[2].state('recovery-job', NOW + timedelta(seconds=61)) is None


def test_runner_probe_unknown_after_claim_releases_without_status_change(case, monkeypatch):
    original = history.claim_job_recovery
    def claim(*args, **kwargs):
        result = original(*args, **kwargs)
        case[3].states[101] = 'unknown'
        return result
    monkeypatch.setattr(history, 'claim_job_recovery', claim)
    before = row(case)
    assert run(case)['deferred'] == 1
    assert row(case) == before
    assert history.read_job_attempt(case[0], case[1])['resolved_at'] is None
    assert case[2].pending(NOW) == ()


def test_launch_without_kill_on_close_stays_unknown(case):
    history.update_job(case[0], 'recovery-job',
                       launch_limitation='contained_innermost_job_without_kill_on_close')
    assert run(case)['unknown'] == 1
    assert row(case)['ended_at'] is None
    assert case[2].pending(NOW) == ()


@pytest.mark.parametrize('hook', ['service', 'mcp'])
@pytest.mark.parametrize('failure', ['import', 'lock', 'thread'])
def test_startup_hook_failures_are_ignored_content_free(case, monkeypatch, caplog, hook, failure):
    import builtins
    from fastapi.testclient import TestClient
    from modelquad.service.app import create_app
    from modelquad.mcp import server
    message = 'PRIVATE_STARTUP_EXCEPTION_BODY'
    def fail(*args, **kwargs): raise RuntimeError(message)
    if failure == 'import':
        original = builtins.__import__
        def imports(name, *args, **kwargs):
            if name.endswith('jobs.recovery'): fail()
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', imports)
    elif failure == 'lock':
        class Lock:
            def __enter__(self): fail()
            def __exit__(self, *args): pass
        monkeypatch.setattr(recovery, '_schedule_lock', Lock())
    else:
        monkeypatch.setattr(recovery.threading, 'Thread', fail)
    if hook == 'service':
        with TestClient(create_app(db_path=case[0], poll_providers=False)): pass
    else:
        monkeypatch.setattr(server, 'is_worker', lambda path: False)
        monkeypatch.setattr(server.server, 'run', lambda: None)
        server.main()
    assert 'job recovery' in caplog.text
    assert message not in caplog.text and str(case[0]) not in caplog.text


def test_mcp_worker_startup_does_not_schedule(case, monkeypatch):
    from modelquad.mcp import server
    monkeypatch.setattr(server, 'is_worker', lambda path: True)
    monkeypatch.setattr(recovery, 'schedule_recovery', lambda *a: pytest.fail('worker must not recover'))
    monkeypatch.setattr(server.server, 'run', lambda: None)
    server.main()


def test_crash_after_survival_observation_keeps_retry_unknown(case, monkeypatch):
    case[3].states[202] = 'alive'
    original = case[3]._terminate_exact
    class Crash(BaseException): pass
    def kill_then_crash(*args):
        original(*args)
        raise Crash
    monkeypatch.setattr(case[3], '_terminate_exact', kill_then_crash)
    with pytest.raises(Crash): run(case)
    assert row(case)['status'] == 'unknown' and row(case)['recovery_ended_at'] is not None
    assert run(case, NOW + timedelta(seconds=61))['unknown'] == 1
    assert row(case)['recovery_decision'] == 'unknown'
    assert row(case)['ended_at'] is None
    assert history.read_job_attempt(case[0], case[1])['resolved_at'] is None
    assert case[2].pending(NOW + timedelta(seconds=61)) == ()


_DECISION_BOUNDARIES = [
    (decision, stage)
    for decision in ('restore_pending', 'map_resolved', 'interrupted', 'startup_failed', 'unknown')
    for stage in ('claim', 'before_decision', 'decision', 'before_finalize',
                  'job_write', 'attempt_write', 'commit', 'finalized', 'before_put', 'put')
    if not (decision == 'unknown' and stage not in ('claim', 'before_decision', 'decision'))
    and not (stage in ('before_put', 'put') and decision not in ('map_resolved', 'interrupted'))
]


@pytest.mark.parametrize('decision,stage', _DECISION_BOUNDARIES)
def test_every_decision_crash_boundary(case, monkeypatch, decision, stage):
    if decision == 'restore_pending':
        answer(case, JobStatus.FINISHED)
    elif decision == 'map_resolved':
        finish(case, status='failed', kind='timeout')
    elif decision == 'startup_failed':
        history.update_job(case[0], 'recovery-job', status='starting', runner_pid=None,
                           runner_created=None, worker_pid=None, worker_created=None, heartbeat_at=None)
    elif decision == 'unknown':
        answer(case)
        case[2].delete('recovery-job', NOW)
    before_attempt = history.read_job_attempt(case[0], case[1])
    class Crash(BaseException): pass
    original_connect = db.connect_and_init
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if ((stage == 'job_write' and sql.startswith('UPDATE jobs SET status=')) or
                    (stage == 'attempt_write' and sql.startswith('UPDATE delegation_attempts SET status='))):
                raise Crash
            return result
        def commit(self):
            if stage == 'commit': raise Crash
            return self.conn.commit()
    name = ('claim_job_recovery' if stage == 'claim' else
            'write_job_recovery' if stage in ('before_decision', 'decision') else
            'put' if stage in ('before_put', 'put') else 'finalize_job_recovery')
    module = case[2] if name == 'put' else history
    original = getattr(module, name)
    captured = []
    def crash(*args, **kwargs):
        if stage.startswith('before_'): raise Crash
        if stage in ('job_write', 'attempt_write', 'commit'):
            with monkeypatch.context() as patch:
                patch.setattr(db, 'connect_and_init', lambda path: Connection(original_connect(path)))
                return original(*args, **kwargs)
        result = original(*args, **kwargs)
        if name == 'put': captured.append(args[0])
        raise Crash
    with monkeypatch.context() as patch:
        patch.setattr(module, name, crash)
        with pytest.raises(Crash): run(case)
    saved = row(case)
    if stage not in ('claim', 'before_decision'):
        assert saved['recovery_decision'] == decision
    if stage != 'finalized':
        assert saved['ended_at'] is None
        assert history.read_job_attempt(case[0], case[1]) == before_attempt
    # After deciding, no retry is permitted to consult mutable output state.
    if saved['recovery_decision']:
        monkeypatch.setattr(case[2], 'state', lambda *a: pytest.fail('retry re-decided'))
        monkeypatch.setattr(case[2], 'collect', lambda *a: pytest.fail('retry re-collected'))
    counts = run(case, NOW + timedelta(seconds=61))
    result = row(case)
    assert result['recovery_decision'] == decision
    if decision == 'unknown':
        assert counts['restored'] == 0 and result['ended_at'] is None
        assert history.read_job_attempt(case[0], case[1]) == before_attempt
        assert run(case, NOW + timedelta(days=61))['unknown'] == 0
    else:
        assert counts['restored'] == (0 if stage == 'finalized' else 1)
        expected = {'restore_pending': 'finished', 'map_resolved': 'timed_out',
                    'interrupted': 'interrupted', 'startup_failed': 'failed'}[decision]
        assert result['status'] == expected
        assert result['ended_at'] == result['recovery_ended_at']
        if saved['recovery_ended_at']:
            assert result['ended_at'] == saved['recovery_ended_at']
        if captured:
            assert FileOutputStore(case[2].root).collect('recovery-job', NOW + timedelta(seconds=61)).output == captured[0]


@pytest.mark.parametrize('decision', ['interrupted', 'map_resolved', 'restore_pending'])
def test_decided_output_acknowledged_before_retry(case, monkeypatch, decision):
    if decision == 'map_resolved': finish(case, status='failed', kind='timeout')
    if decision == 'restore_pending': answer(case, JobStatus.FINISHED)
    class Crash(BaseException): pass
    def crash(*a, **kw): raise Crash
    with monkeypatch.context() as patch:
        patch.setattr(history, 'finalize_job_recovery', crash)
        with pytest.raises(Crash): run(case)
    assert row(case)['recovery_decision'] == decision
    receipt = case[2].collect('recovery-job', NOW).receipt
    assert case[2].acknowledge('recovery-job', receipt, NOW)
    assert run(case, NOW + timedelta(seconds=61))['restored'] == 1
    expected = {'interrupted': 'interrupted', 'map_resolved': 'timed_out', 'restore_pending': 'finished'}[decision]
    assert row(case)['status'] == expected and row(case)['ended_at'] == NOW.isoformat()
    if decision == 'interrupted':
        assert history.read_job_attempt(case[0], case[1])['failure_kind'] == 'orphaned'
    assert case[2].collect('recovery-job', NOW + timedelta(seconds=61)) is None


def test_removed_output_ages_out_but_decision_stays_unknown(case):
    answer(case)
    case[2].delete('recovery-job', NOW)
    assert run(case)['unknown'] == 1
    saved = row(case)
    assert saved['recovery_decision'] == 'unknown'
    later = NOW + timedelta(days=31)
    assert case[2].state('recovery-job', later) is None
    assert run(case, later)['restored'] == 0
    assert run(case, NOW + timedelta(days=61))['restored'] == 0
    assert row(case) == saved and saved['ended_at'] is None
    assert history.read_job_attempt(case[0], case[1])['resolved_at'] is None


@pytest.mark.parametrize('fence', ['token', 'terminal', 'runner_write', 'decided'])
def test_decision_write_is_fenced_and_immutable(case, fence):
    claimed = history.claim_job_recovery(case[0], row(case), token='owner', now=NOW, lease_seconds=60)
    token = 'owner'
    if fence == 'token': token = 'stale'
    elif fence == 'terminal':
        history.update_job(case[0], 'recovery-job', status='timed_out', ended_at=NOW.isoformat())
    elif fence == 'runner_write':
        history.update_job(case[0], 'recovery-job', heartbeat_at=NOW.isoformat())
    else:
        claimed = history.write_job_recovery(case[0], claimed, token=token, now=NOW,
                                             decision='unknown', status='unknown')
    saved = row(case)
    attempt = history.read_job_attempt(case[0], case[1])
    assert history.write_job_recovery(case[0], claimed, token=token, now=NOW,
                                     decision='interrupted', status='interrupted') is None
    assert row(case) == saved
    assert history.read_job_attempt(case[0], case[1]) == attempt
    if fence == 'decided':
        assert not history.finalize_job_recovery(case[0], claimed, token=token,
                                                 status='interrupted', now=NOW)
        assert row(case) == saved
