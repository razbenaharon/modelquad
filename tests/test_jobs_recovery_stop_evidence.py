# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Retained job status cannot stand in for observed whole-tree death."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from modelquad import procutil
from modelquad.execution.base import DESCENDANTS_RECORDED, WorkerTaskStatus
from modelquad.jobs import recovery
from modelquad.jobs.seam import JobOutput, JobRef, JobStatus
from modelquad.jobs.store import FileOutputStore
from modelquad.mcp.livelifecycle import derive_recovery_outlook, facts_from_attempt
from modelquad.service import db, history

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=5)
JOB_ID = 'audit-j1-stop'
DECISION_ID = 'audit-j1-decision'
TERMINATION = {'confirmed_dead': True, 'tree_kill': 'ok'}


class DeadProbe:
    def pid_state(self, pid):
        assert pid in {101, 202}
        return 'dead'

    def process_creation_epoch(self, pid):
        pytest.fail('a dead pid needs no identity probe')

    def _terminate_exact(self, pid, created):
        pytest.fail('synthetic recovery must never terminate a process')


@pytest.fixture
def case(tmp_path):
    path = tmp_path / 'jobs.sqlite'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    attempt = history.record_delegation_attempt(
        path, task_type='implementation', decision_id=DECISION_ID,
        working_directory=workspace,
    )
    assert attempt is not None
    history.reserve_job(path, JOB_ID, attempt)
    history.update_job(path, JOB_ID, status='running', runner_pid=101,
                       runner_created=100.0, worker_pid=202, worker_created=200.0,
                       heartbeat_at=OLD.isoformat())
    conn = db.connect_and_init(path)
    try:
        conn.execute('UPDATE jobs SET created_at=?, updated_at=? WHERE job_id=?',
                     (OLD.isoformat(), OLD.isoformat(), JOB_ID))
        conn.commit()
    finally:
        conn.close()
    return SimpleNamespace(path=path, attempt=attempt, workspace=workspace,
                           sink=FileOutputStore(tmp_path / 'answers'), probe=DeadProbe())


def output(case, *, status=JobStatus.STOPPED, termination=None, descendants=None,
           tree=None):
    return JobOutput(
        JobRef(JOB_ID, case.attempt, DECISION_ID), status, WorkerTaskStatus.PARTIAL,
        'stop_requested', 'retained partial answer', True, ('src/edit.py',),
        termination, descendants, OLD, NOW, tree,
    )


def retain(case, **kwargs):
    item = output(case, **kwargs)
    assert case.sink.put(item, NOW)
    # Force the same disk reload and deep freezing as real recovery.
    case.sink = FileOutputStore(case.sink.root)
    collected = case.sink.collect(JOB_ID, NOW)
    assert collected.output == item
    return collected


def run(case, now=NOW):
    counts = recovery.recover_jobs(case.path, now=now, sink=case.sink, probe=case.probe)
    assert counts['deferred'] == 0
    assert counts['failed'] == 0
    return counts


def rows(case):
    return (history.read_job(case.path, JOB_ID),
            history.read_job_attempt(case.path, case.attempt))


def assert_retained(case, collected, *, now=NOW):
    again = case.sink.collect(JOB_ID, now)
    assert again.output == collected.output
    assert again.receipt == collected.receipt


UNCONFIRMED = [
    pytest.param({'confirmed_dead': False, 'tree_kill': 'job_not_empty'}, None, False,
                 id='false'),
    pytest.param(None, None, None, id='missing'),
    pytest.param({'confirmed_dead': 'true', 'tree_kill': 'ok'}, None, True,
                 id='malformed'),
    pytest.param(TERMINATION, {'enumerated': True, 'truncated': False,
                              'descendants': [{'pid': 303, 'image': 'worker.exe',
                                               'created': 300.0}]}, True,
                 id='outside_descendant'),
]


@pytest.mark.parametrize('termination,descendants,tree', UNCONFIRMED)
def test_recovered_stop_preserves_unconfirmed_death(case, termination, descendants, tree):
    collected = retain(case, termination=termination, descendants=descendants, tree=tree)
    assert run(case)['restored'] == 1
    job, attempt = rows(case)
    assert job['status'] == 'stopped'
    assert job['ended_at'] == NOW.isoformat()
    assert job['recovery_decision'] == 'restore_pending'
    assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'timeout'
    assert attempt['detail'].startswith('stop_requested;')
    assert 'worker_tree_death_unconfirmed' in attempt['detail']
    outlook = derive_recovery_outlook(facts_from_attempt(attempt))
    assert outlook.safe_to_retry is not True
    assert outlook.worker_may_be_running is True
    assert_retained(case, collected)


@pytest.mark.parametrize('descendants', [
    pytest.param(None, id='no_census'),
    pytest.param({'enumerated': True, 'truncated': False, 'descendants': []},
                 id='empty_census'),
])
def test_recovered_confirmed_stop_records_termination(case, descendants):
    collected = retain(case, termination=TERMINATION, descendants=descendants, tree=True)
    assert run(case)['restored'] == 1
    job, attempt = rows(case)
    assert job['status'] == 'stopped' and job['ended_at'] == NOW.isoformat()
    assert attempt['failure_kind'] == 'terminated'
    assert attempt['detail'].startswith('stop_requested;')
    assert 'worker_tree_death_confirmed' in attempt['detail']
    assert derive_recovery_outlook(facts_from_attempt(attempt)).safe_to_retry is True
    assert_retained(case, collected)


@pytest.mark.parametrize('termination,descendants,tree', [
    pytest.param(TERMINATION, None, False, id='tree_flag_false'),
    pytest.param(TERMINATION, None, None, id='tree_flag_missing'),
    pytest.param(None, None, True, id='termination_missing'),
    pytest.param({'confirmed_dead': False, 'tree_kill': 'ok'}, None, True,
                 id='direct_child_not_confirmed'),
    pytest.param({'confirmed_dead': True, 'tree_kill': 'census_unknown'}, None, True,
                 id='tree_kill_unknown'),
    pytest.param(TERMINATION, {'enumerated': False, 'truncated': False,
                              'descendants': []}, True, id='census_failed'),
    pytest.param(TERMINATION, {'enumerated': True, 'truncated': True,
                              'descendants': []}, True, id='census_truncated'),
    pytest.param(TERMINATION, {'enumerated': True, 'truncated': False}, True,
                 id='census_missing_list'),
    pytest.param(TERMINATION, {'enumerated': 'true', 'truncated': False,
                              'descendants': []}, True, id='census_malformed'),
])
def test_contradictory_or_incomplete_proof_never_confirms_death(
        case, termination, descendants, tree):
    retain(case, termination=termination, descendants=descendants, tree=tree)
    assert run(case)['restored'] == 1
    assert rows(case)[1]['failure_kind'] == 'timeout'


@pytest.mark.parametrize('tree,kind,retry', [
    pytest.param(True, 'terminated', True, id='timed_out_confirmed'),
    pytest.param(False, 'timeout', None, id='timed_out_unconfirmed'),
])
def test_recovered_timeout_matches_runner_resolution(case, tree, kind, retry):
    collected = retain(case, status=JobStatus.TIMED_OUT, termination=TERMINATION, tree=tree)
    assert run(case)['restored'] == 1
    job, attempt = rows(case)
    assert job['status'] == 'timed_out'
    assert attempt['failure_kind'] == kind
    assert not attempt['detail'].startswith('stop_requested')
    assert derive_recovery_outlook(facts_from_attempt(attempt)).safe_to_retry is retry
    assert_retained(case, collected)


def descendant_facts(count=2):
    return {'enumerated': True, 'truncated': False, 'method': 'snapshot',
            'observed_at': NOW.timestamp(),
            'descendants': [{'pid': 303 + i, 'image': 'worker.exe', 'created': 300.0 + i}
                            for i in range(count)]}


def test_recovery_persists_descendants_atomically(case, monkeypatch):
    facts = descendant_facts()
    collected = retain(case, status=JobStatus.FINISHED, descendants=facts)
    assert isinstance(collected.output.descendants['descendants'], tuple)
    connect = db.connect_and_init

    class ObserveConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if sql.startswith('UPDATE delegation_attempts SET status='):
                job = self.conn.execute('SELECT ended_at FROM jobs WHERE job_id=?',
                                        (JOB_ID,)).fetchone()
                attempt = self.conn.execute(
                    'SELECT resolved_at, worker_descendants FROM delegation_attempts WHERE id=?',
                    (case.attempt,),
                ).fetchone()
                assert job['ended_at'] == attempt['resolved_at'] == NOW.isoformat()
                assert json.loads(attempt['worker_descendants'])['descendants'] == facts['descendants']
                # An independent reader sees neither uncommitted change.
                outside = rows(case)
                assert outside[0]['ended_at'] is None
                assert outside[1]['resolved_at'] is None
                assert outside[1]['worker_descendants'] is None
            return result

    with monkeypatch.context() as patch:
        patch.setattr(db, 'connect_and_init', lambda path: ObserveConnection(connect(path)))
        assert run(case)['restored'] == 1
    job, attempt = rows(case)
    assert job['status'] == 'finished'
    assert len(json.loads(attempt['worker_descendants'])['descendants']) == 2
    monkeypatch.setattr(procutil, 'pid_state', lambda pid: 'unknown')
    workspace = history.workspace_delegation_liveness(case.path, case.workspace, now=NOW)
    assert workspace['verdict'] == 'unknown'
    assert_retained(case, collected)


@pytest.mark.parametrize('fence', ['token', 'terminal', 'runner_write'],
                         ids=['stale_token', 'ended_job', 'runner_write'])
def test_descendant_write_obeys_both_row_fences(case, fence):
    collected = retain(case, termination=TERMINATION, descendants=descendant_facts(), tree=True)
    claimed = history.claim_job_recovery(case.path, rows(case)[0], token='owner',
                                         now=NOW, lease_seconds=60)
    claimed = history.write_job_recovery(case.path, claimed, token='owner', now=NOW,
                                         decision='restore_pending', status='stopped', ended=NOW)
    token = 'owner'
    if fence == 'token':
        token = 'stale'
    elif fence == 'terminal':
        history.update_job(case.path, JOB_ID, status='stopped', ended_at=NOW.isoformat())
    else:
        history.update_job(case.path, JOB_ID, heartbeat_at=NOW.isoformat())
    before = rows(case)
    assert not history.finalize_job_recovery(case.path, claimed, token=token,
                                             status='stopped', now=NOW, evidence=collected.output)
    assert rows(case) == before


def test_recovered_descendants_are_bounded(case):
    facts = descendant_facts(DESCENDANTS_RECORDED + 1)
    for item in facts['descendants']:
        item['image'] = str(case.workspace / 'worker.exe')
        item['command_line'] = 'PRIVATE_COMMAND_LINE'
    retain(case, status=JobStatus.FINISHED, descendants=facts)
    assert run(case)['restored'] == 1
    raw = rows(case)[1]['worker_descendants']
    bounded = json.loads(raw)
    assert len(bounded['descendants']) == DESCENDANTS_RECORDED
    assert bounded['truncated'] is True
    assert all(item['image'] == 'worker.exe' for item in bounded['descendants'])
    assert str(case.workspace) not in raw and 'PRIVATE_COMMAND_LINE' not in raw
    assert history.workspace_delegation_liveness(
        case.path, case.workspace, now=NOW)['verdict'] == 'unknown'


def test_already_resolved_attempt_keeps_original_evidence(case):
    retain(case, termination=TERMINATION, tree=True)
    assert history.resolve_delegation_attempt(
        case.path, case.attempt, status='failed', failure_kind='timeout',
        detail='stop_requested; original unconfirmed record', only_if_open=True,
        worker_descendants=descendant_facts(),
    )
    before = rows(case)[1]
    assert run(case)['restored'] == 1
    assert rows(case)[1] == before
    assert rows(case)[0]['status'] == 'stopped'


@pytest.mark.parametrize('removed', [False, True], ids=['lease_takeover', 'acknowledged'])
@pytest.mark.parametrize('proof', [False, True], ids=['missing_proof', 'previously_confirmed'])
def test_recovery_restart_cannot_promote_missing_proof(case, monkeypatch, removed, proof):
    collected = retain(case, termination=TERMINATION if proof else None,
                       tree=True if proof else None)

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(history, 'finalize_job_recovery', crash)
        with pytest.raises(Crash):
            run(case)
    job, attempt = rows(case)
    assert job['recovery_decision'] == 'restore_pending' and job['status'] == 'stopped'
    assert attempt['resolved_at'] is None
    if removed:
        assert case.sink.acknowledge(JOB_ID, collected.receipt, NOW)
    with monkeypatch.context() as patch:
        patch.setattr(case.sink, 'state', lambda *a: pytest.fail('takeover must not re-read state'))
        patch.setattr(case.sink, 'collect', lambda *a: pytest.fail('takeover must not re-collect'))
        assert run(case, NOW + timedelta(seconds=61))['restored'] == 1
    job, attempt = rows(case)
    assert job['status'] == 'stopped' and job['ended_at'] == NOW.isoformat()
    assert attempt['failure_kind'] == 'timeout'
    assert attempt['detail'].startswith('stop_requested;')
    assert derive_recovery_outlook(facts_from_attempt(attempt)).safe_to_retry is not True
    if removed:
        assert case.sink.collect(JOB_ID, NOW + timedelta(seconds=61)) is None
    else:
        assert_retained(case, collected, now=NOW + timedelta(seconds=61))


def test_retained_answer_keeps_status_and_descendants_despite_uncertainty(case):
    history.update_job(case.path, JOB_ID,
                       launch_limitation='contained_innermost_job_without_kill_on_close')
    collected = retain(case, termination=TERMINATION, descendants=descendant_facts(), tree=True)
    assert run(case)['restored'] == 1
    job, attempt = rows(case)
    assert job['status'] == 'stopped' and job['ended_at'] == NOW.isoformat()
    assert attempt['failure_kind'] == 'timeout'
    assert len(json.loads(attempt['worker_descendants'])['descendants']) == 2
    assert_retained(case, collected)


@pytest.mark.parametrize('error', [AttributeError, NameError, TypeError],
                         ids=['missing_attribute', 'missing_name', 'wrong_signature'])
def test_recovery_does_not_hide_programming_errors(case, monkeypatch, error):
    retain(case)

    def broken(*args, **kwargs):
        raise error('synthetic programming error')

    with monkeypatch.context() as patch:
        patch.setattr(history, 'finalize_job_recovery', broken)
        with pytest.raises(error):
            run(case)
    job, attempt = rows(case)
    assert job['ended_at'] is None and attempt['resolved_at'] is None
    assert run(case, NOW + timedelta(seconds=61))['restored'] == 1
