# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Real-shaped Windows leftovers are not worker-ending evidence.

Offline contracts only. Behavioral validation and native worker acceptance
happen separately.
"""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from orchestrator import procutil
from orchestrator.execution.base import bound_descendant_facts
from orchestrator.service import db, fate_history as fh, history

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
START = NOW - timedelta(minutes=1)
EXIT = NOW - timedelta(seconds=2)
OUTPUT = NOW - timedelta(seconds=1)
JOB = str(uuid.UUID(int=101))
DECISION = str(uuid.UUID(int=201))


@pytest.fixture
def lab(tmp_path, monkeypatch):
    path = tmp_path / 'fate.sqlite'
    db.connect_and_init(path).close()
    # Toolhelp rows retain parent PIDs even when the parent is already gone.
    # A live grandchild below can therefore name an absent intermediate.
    tree = {202: (101, 'codex.exe', 20.0),
            303: (202, 'codex-windows-sandbox-service.exe', 30.0),
            304: (202, 'conhost.exe', 31.0)}
    states = {101: 'dead', 202: 'dead', 303: 'alive', 304: 'alive'}
    monkeypatch.setattr(procutil, 'pid_state', lambda pid: states.get(pid, 'dead'))
    monkeypatch.setattr(procutil, 'native_process_creation_epoch',
                        lambda pid: tree[pid][2] if pid in tree else None)
    monkeypatch.setattr(procutil, 'process_creation_epoch',
                        lambda pid: tree[pid][2] if pid in tree else None)
    monkeypatch.setattr(procutil, 'process_image_name',
                        lambda pid: tree[pid][1] if pid in tree else None)
    monkeypatch.setattr(procutil, '_windows_process_snapshot',
                        lambda: [(pid, parent, image) for pid, (parent, image, _) in tree.items()
                                 if states.get(pid) == 'alive'])
    monkeypatch.setattr(procutil, 'observe_boot', lambda: None)

    def seed(*, provider='codex', decision=DECISION, leftovers=(303, 304), exit_code=0,
             execution=True, collected=True):
        tree[202] = (101, 'claude.exe' if provider == 'claude' else 'codex.exe', 20.0)
        descendants = None if leftovers is None else bound_descendant_facts({
            'enumerated': True, 'complete': True, 'method': 'job_object', 'truncated': False,
            'descendants': [{'pid': pid, 'image': tree[pid][1], 'created': tree[pid][2]}
                            for pid in leftovers],
        })
        workspace, ancestors = history.workspace_identity(tmp_path)
        with sqlite3.connect(path) as conn:
            conn.execute('INSERT INTO delegation_attempts '
                         '(id,decision_id,created_at,task_type,status,resolved_at,worker_pid,'
                         'routed_provider,workspace_key,workspace_ancestors,worker_descendants) '
                         "VALUES (1,?,?,'review','completed',?,202,?,?,?,?)",
                         (decision, START.isoformat(), NOW.isoformat(), provider,
                          workspace, ancestors,
                          json.dumps(descendants) if descendants is not None else None))
            conn.execute('INSERT INTO jobs '
                         '(job_id,attempt_id,decision_id,status,created_at,updated_at,ended_at,'
                         "worker_pid,worker_created) VALUES (?,1,?,'finished',?,?,?,202,20)",
                         (JOB, decision, START.isoformat(), NOW.isoformat(),
                          OUTPUT.isoformat() if collected else None))
            if execution:
                conn.execute('INSERT INTO execution_history '
                             '(created_at,task_type,provider,decision_id,exit_code) '
                             "VALUES (?,'review',?,?,?)",
                             (EXIT.isoformat(), provider, decision, exit_code))
        return descendants

    return SimpleNamespace(path=path, tree=tree, states=states, seed=seed)


def verdict(lab):
    with fh.connection(lab.path) as conn:
        snap = fh.linked(conn, JOB)
        return (fh.worker_ending_confirmed(conn, snap.job, snap.attempt), fh.eligible(conn, JOB))


@pytest.mark.parametrize('provider', ['codex', 'claude'])
@pytest.mark.parametrize('leftovers', [(303,), (304,), (303, 304)],
                         ids=['sandbox-service', 'console-host', 'both-helpers'])
@pytest.mark.parametrize('decision_form', ['dashed', 'hex'])
def test_collected_exit_zero_confirms_worker_despite_surviving_helpers(
        lab, provider, leftovers, decision_form):
    decision = DECISION if decision_form == 'dashed' else uuid.UUID(DECISION).hex
    descendants = lab.seed(provider=provider, leftovers=leftovers, decision=decision)
    assert verdict(lab) == (True, False)
    assert fh.candidates(lab.path) == []
    # Confirming the worker ending must not rewrite census evidence or bless
    # the workspace: a live descendant still has its independent hazard.
    attempt = history.read_job_attempt(lab.path, 1)
    assert json.loads(attempt['worker_descendants']) == descendants
    assert history._descendant_hazard(attempt, native_only=True)[0] == 'may_be_mutating'


@pytest.mark.parametrize('provider', ['codex', 'claude'])
@pytest.mark.parametrize('execution', [True, False], ids=['confirmed-exit', 'missing-exit'])
def test_grandchild_outlives_worker_and_missing_intermediate(lab, provider, execution):
    lab.tree[303] = (404, 'python.exe', 30.0)  # intermediate 404 already exited
    lab.seed(provider=provider, leftovers=(303,), execution=execution)
    assert procutil._windows_process_snapshot() == [
        (303, 404, 'python.exe'), (304, 202, 'conhost.exe'),
    ]
    assert verdict(lab) == (execution, not execution)
    assert [item['job_id'] for item in fh.candidates(lab.path)] == ([] if execution else [JOB])


@pytest.mark.parametrize('leftovers', [(303, 304), (), None],
                         ids=['helpers', 'empty-tree', 'no-census'])
def test_matching_worker_still_running_preserves_descendant_verdict(lab, leftovers):
    lab.seed(leftovers=leftovers)
    lab.states[202] = 'alive'
    # A matching live worker defeats positive exit proof. Eligibility still
    # follows the base census rule: helpers block; an empty/NULL tree does not.
    assert verdict(lab) == (False, bool(leftovers))
    assert [item['job_id'] for item in fh.candidates(lab.path)] == ([JOB] if leftovers else [])


@pytest.mark.parametrize('execution,exit_code', [(False, None), (True, None)])
@pytest.mark.parametrize('leftovers', [(303,), (304,), (303, 304), (), None],
                         ids=['sandbox-service', 'console-host', 'both-helpers',
                              'empty-tree', 'no-census'])
def test_missing_exit_evidence_preserves_base_descendant_verdict(
        lab, leftovers, execution, exit_code):
    lab.seed(leftovers=leftovers, execution=execution, exit_code=exit_code)
    assert verdict(lab) == (False, bool(leftovers))
    assert [item['job_id'] for item in fh.candidates(lab.path)] == ([JOB] if leftovers else [])


def test_recorded_exit_without_output_handoff_is_not_confirmed(lab):
    lab.seed(collected=False)
    assert verdict(lab) == (False, True)


@pytest.mark.parametrize('failure', ['timeout', 'orphaned', 'exception'])
def test_killed_worker_or_lost_runner_stays_unconfirmed(lab, failure):
    lab.seed(exit_code=None)
    with sqlite3.connect(lab.path) as conn:
        conn.execute("UPDATE delegation_attempts SET status='failed',failure_kind=?", (failure,))
        conn.execute("UPDATE jobs SET status='unknown',recovery_decision='unknown'")
    assert verdict(lab) == (False, True)
    assert [item['job_id'] for item in fh.candidates(lab.path)] == [JOB]


@pytest.mark.parametrize('failure,blocks_without_descendants', [
    ('timeout', True), ('orphaned', True), ('exception', True),
    ('failed_to_start', True), ('policy_refused', True), ('terminated', False),
])
@pytest.mark.parametrize('leftovers', [(303, 304), (), None],
                         ids=['helpers', 'empty-tree', 'no-census'])
def test_failure_kind_keeps_base_rules_despite_recorded_exit(
        lab, failure, blocks_without_descendants, leftovers):
    lab.seed(leftovers=leftovers)
    with sqlite3.connect(lab.path) as conn:
        conn.execute("UPDATE delegation_attempts SET status='failed',failure_kind=?", (failure,))
        conn.execute("UPDATE jobs SET status='failed'")
    # The no-launch labels contradict the recorded PID; terminated accounts
    # for the worker and therefore uses the existing descendant rules.
    eligible = blocks_without_descendants or bool(leftovers)
    assert verdict(lab) == (False, eligible)
    assert [item['job_id'] for item in fh.candidates(lab.path)] == ([JOB] if eligible else [])


@pytest.mark.parametrize('failure', ['failed_to_start', 'policy_refused',
                                    'provider_unavailable', 'no_viable_worker'])
@pytest.mark.parametrize('leftovers', [(303, 304), (), None],
                         ids=['helpers', 'empty-tree', 'no-census'])
def test_never_launched_keeps_base_rules(lab, failure, leftovers):
    lab.seed(leftovers=leftovers, execution=False)
    with sqlite3.connect(lab.path) as conn:
        conn.execute("UPDATE delegation_attempts SET status=?,failure_kind=?,worker_pid=NULL",
                     ('refused' if failure == 'policy_refused' else 'failed', failure))
        conn.execute("UPDATE jobs SET status='unknown',recovery_decision='unknown',"
                     "worker_pid=NULL,worker_created=NULL,"
                     "launch_limitation='contained_innermost_job_without_kill_on_close'")
    assert verdict(lab) == (False, bool(leftovers))
    assert [item['job_id'] for item in fh.candidates(lab.path)] == ([JOB] if leftovers else [])


@pytest.mark.parametrize('raw', [
    '{"enumerated":false,"descendants":[]}',
    '{"enumerated":true,"truncated":true,"descendants":[]}',
    '[]', 'not-json',
], ids=['not-enumerated', 'truncated', 'not-an-object', 'malformed-json'])
@pytest.mark.parametrize('execution', [True, False], ids=['confirmed-exit', 'missing-exit'])
def test_uncertain_descendant_census_only_yields_to_confirmed_exit(lab, raw, execution):
    lab.seed(execution=execution)
    with sqlite3.connect(lab.path) as conn:
        conn.execute('UPDATE delegation_attempts SET worker_descendants=?', (raw,))
    assert verdict(lab) == (execution, not execution)
    assert [item['job_id'] for item in fh.candidates(lab.path)] == ([] if execution else [JOB])


def test_nonzero_normal_exit_confirms_ending_without_changing_failure(lab):
    lab.seed(exit_code=1)
    with sqlite3.connect(lab.path) as conn:
        conn.execute("UPDATE jobs SET status='failed'")
        conn.execute("UPDATE delegation_attempts SET outcome='failed'")
    assert verdict(lab) == (True, False)
    assert history.read_job(lab.path, JOB)['status'] == 'failed'
    assert history.read_job_attempt(lab.path, 1)['outcome'] == 'failed'


@pytest.mark.parametrize('observation', ['unknown', 'reused'])
def test_durable_exit_survives_unavailable_probe_or_recycled_pid(lab, observation):
    lab.seed()
    lab.states[202] = 'unknown' if observation == 'unknown' else 'alive'
    if observation == 'reused':
        lab.tree[202] = (999, 'unrelated.exe', 90.0)
    assert verdict(lab) == (True, False)


@pytest.mark.parametrize('mismatch', ['decision', 'decision-form', 'provider', 'time',
                                     'unparsable-time', 'duplicate', 'worker-pid'])
@pytest.mark.parametrize('leftovers', [(303, 304), (), None],
                         ids=['helpers', 'empty-tree', 'no-census'])
def test_other_execution_cannot_supply_missing_job_exit(lab, mismatch, leftovers):
    lab.seed(leftovers=leftovers)
    with sqlite3.connect(lab.path) as conn:
        if mismatch == 'decision':
            conn.execute('UPDATE execution_history SET decision_id=?', (str(uuid.UUID(int=999)),))
        elif mismatch == 'decision-form':
            conn.execute('UPDATE execution_history SET decision_id=?', ('{' + DECISION + '}',))
        elif mismatch == 'unparsable-time':
            conn.execute("UPDATE execution_history SET created_at='not-a-timestamp'")
        elif mismatch == 'provider':
            conn.execute("UPDATE execution_history SET provider='claude'")
        elif mismatch == 'time':
            conn.execute('UPDATE execution_history SET created_at=?',
                         ((START - timedelta(seconds=1)).isoformat(),))
        elif mismatch == 'worker-pid':
            conn.execute('UPDATE delegation_attempts SET worker_pid=999')
        else:
            conn.execute('INSERT INTO execution_history (created_at,task_type,provider,decision_id,exit_code) '
                         "VALUES (?,'review','codex',?,0)", (EXIT.isoformat(), DECISION))
    assert verdict(lab) == (False, bool(leftovers))
    assert [item['job_id'] for item in fh.candidates(lab.path)] == ([JOB] if leftovers else [])


def test_two_jobs_on_one_decision_do_not_share_exit_proof(lab):
    lab.seed()
    with sqlite3.connect(lab.path) as conn:
        conn.execute('INSERT INTO delegation_attempts '
                     '(id,decision_id,created_at,task_type,status,resolved_at) '
                     "VALUES (2,?,?,'review','completed',?)",
                     (DECISION, START.isoformat(), NOW.isoformat()))
        conn.execute('INSERT INTO jobs '
                     '(job_id,attempt_id,decision_id,status,created_at,updated_at,ended_at) '
                     "VALUES (?,2,?,'finished',?,?,?)",
                     (str(uuid.UUID(int=102)), DECISION, START.isoformat(), NOW.isoformat(), OUTPUT.isoformat()))
    assert verdict(lab) == (False, True)
