# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Deterministic metadata/storage/auth tests. No live service or workers."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import ntpath
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from modelquad.adapters import pending_reminder as reminder
from modelquad.execution.base import WorkerTaskStatus
from modelquad.jobs.seam import JobOutput, JobRef, JobStatus, RETENTION
from modelquad.jobs import store as storage
from modelquad.service import api, db, history, trust
from modelquad.service.app import create_app

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
KEY = 'a' * 32
OTHER_KEY = 'b' * 32
TOKEN = 'synthetic-pending-secret-0123456789'  # gitleaks:allow (synthetic test token)


@pytest.fixture
def state(tmp_path):
    database = tmp_path / 'synthetic.db'
    db.connect_and_init(database).close()
    retained = storage.FileOutputStore(tmp_path / 'retained')
    return database, retained


def seed(state, index=1, *, workspace=KEY, changed=None, retained_at=NOW):
    database, retained = state
    job_id, decision = f'job-{index:03}', f'decision-{index:03}'
    stamp = NOW.isoformat()
    with sqlite3.connect(database) as conn:
        conn.execute('INSERT INTO routing_decisions (decision_id,created_at,task_type,providers_json) '
                     "VALUES (?,?,'review','[]')", (decision, stamp))
        conn.execute('INSERT INTO delegation_attempts (id,decision_id,created_at,task_type,status,workspace_key) '
                     "VALUES (?,?,?,'review','completed',?)", (index, decision, stamp, workspace))
        conn.execute('INSERT INTO jobs (job_id,attempt_id,decision_id,status,created_at,updated_at,ended_at) '
                     "VALUES (?,?,?,'finished',?,?,?)", (job_id, index, decision, stamp, stamp, stamp))
    output = JobOutput(JobRef(job_id, index, decision), JobStatus.FINISHED,
        WorkerTaskStatus.COMPLETED, 'CLAIM-CANARY', 'ANSWER-CANARY /private/path',
        False, changed, None, None, retained_at, retained_at)
    retained.put(output, retained_at)
    return output


def summary(state, *, key=KEY, now=NOW, **kw):
    return storage.host_summary(state[0], key, now=now, root=state[1].root,
                                 deadline=100, clock=lambda: 1, **kw)


def snapshot(retained):
    return {p.name: p.read_bytes() for p in retained.root.iterdir() if p.is_file()}


def test_exact_project_linkage(state):
    """PR-PROJECT-01, PR-PRIVACY-01: only the closed projection crosses wire."""
    seed(state)
    response = summary(state)
    assert response == {'count': 1, 'more': False, 'jobs': [{
        'job_id': 'job-001', 'job_status': 'finished', 'task_type': 'review',
        'may_have_changed_files': True, 'expires_at': '2026-10-17T12:00:00Z',
        'expiring_soon': False}]}
    assert all(c not in json.dumps(response) for c in ('CLAIM-CANARY', 'ANSWER-CANARY',
        '/private/path', 'receipt', 'decision-001', KEY))
    forbidden_fields = {'changed_files', 'output', 'output_text', 'prompt', 'receipt',
                        'decision_id', 'attempt_id', 'workspace_key'}
    assert forbidden_fields.isdisjoint(response)
    assert all(forbidden_fields.isdisjoint(row) for row in response['jobs'])


@pytest.mark.parametrize('path', [r'C:\synthetic\project-two', r'C:\synthetic',
    r'C:\synthetic\project\child', r'C:\synthetic\worktrees\sibling'], ids=[
    'PR-PROJECT-02-sibling', 'PR-PROJECT-02-parent', 'PR-PROJECT-02-child', 'PR-PROJECT-02-worktree'])
def test_nonexact_identity_never_matches(state, path):
    seed(state, workspace=history.workspace_identity(path)[0])
    key = history.workspace_identity(r'C:\synthetic\project')[0]
    assert summary(state, key=key) == {'count': 0, 'more': False, 'jobs': []}


def test_windows_identity_spellings_without_real_folders(monkeypatch):
    """PR-PROJECT-02-windows: existing spelling canonicalization, no mkdir/stat."""
    monkeypatch.setattr(history.os.path, 'normcase', ntpath.normcase)
    monkeypatch.setattr(history.os.path, 'normpath', ntpath.normpath)
    from pathlib import PureWindowsPath
    assert history._workspace_hash(PureWindowsPath(r'C:\SYNTHETIC\project')) == history._workspace_hash(
        PureWindowsPath(r'\\?\c:\synthetic\PROJECT'))
    assert history._workspace_hash(PureWindowsPath(r'\\server\share\project')) == history._workspace_hash(
        PureWindowsPath(r'\\?\UNC\server\share\project'))
    assert history._workspace_hash(PureWindowsPath(r'C:\synthetic\project')) != history._workspace_hash(
        PureWindowsPath(r'C:\synthetic\project-two'))


@pytest.mark.parametrize('mutation', ['missing_scope', 'wrong_decision', 'wrong_attempt',
    'missing_decision', 'duplicate_decision', 'arbitrary_task'], ids=[
    'PR-PROJECT-03-no-scope', 'PR-PROJECT-03-decision', 'PR-PROJECT-03-attempt',
    'PR-PROJECT-03-missing', 'PR-PROJECT-03-duplicate', 'PR-PROJECT-03-task'])
def test_invalid_linkage_excluded(state, mutation):
    seed(state)
    commands = {
        'missing_scope': 'UPDATE delegation_attempts SET workspace_key=NULL',
        'wrong_decision': "UPDATE jobs SET decision_id='forged'",
        'wrong_attempt': 'UPDATE jobs SET attempt_id=99',
        'missing_decision': 'DELETE FROM routing_decisions',
        'duplicate_decision': 'INSERT INTO routing_decisions (created_at,task_type,providers_json,decision_id) '
                              'SELECT created_at,task_type,providers_json,decision_id FROM routing_decisions',
        'arbitrary_task': "UPDATE delegation_attempts SET task_type='PROMPT-CANARY'",
    }
    with sqlite3.connect(state[0]) as conn:
        conn.execute(commands[mutation])
    assert summary(state) == {'count': 0, 'more': False, 'jobs': []}


def test_filter_precedes_limits_and_overflow(state):
    """PR-PROJECT-04: earlier unrelated rows cannot crowd matching work out."""
    for i in range(1, 25):
        seed(state, i, workspace=OTHER_KEY)
    for i in range(25, 47):
        seed(state, i)
    response = summary(state)
    assert response['count'] == 22 and response['more'] is True
    assert [r['job_id'] for r in response['jobs']] == [f'job-{i:03}' for i in range(25, 45)]


@pytest.mark.parametrize('remaining,warning,count', [
    (timedelta(days=1, microseconds=1), False, 1), (timedelta(days=1), True, 1),
    (timedelta(microseconds=1), True, 1), (timedelta(), False, 0)], ids=[
    'PR-EXPIRY-01-outside', 'PR-EXPIRY-01-one-day', 'PR-EXPIRY-01-before', 'PR-EXPIRY-01-instant'])
def test_expiry_boundaries(state, remaining, warning, count):
    seed(state)
    response = summary(state, now=NOW + RETENTION - remaining)
    assert response['count'] == count
    if count:
        assert response['jobs'][0]['expiring_soon'] is warning


def test_actual_extended_expiry(state):
    """PR-EXPIRY-02: no invented seven-day ceiling."""
    item = seed(state)
    state[1].extend(item.job.job_id, NOW + timedelta(days=30), NOW)
    response = summary(state, now=NOW + timedelta(days=8))
    assert response['jobs'][0]['expires_at'] == '2026-11-09T12:00:00Z'


@pytest.mark.parametrize('changes,may_change', [(None, True), ((), False), (('file-canary.py',), True)],
                         ids=['PR-CHANGES-01-unknown', 'PR-CHANGES-01-observed-empty', 'PR-CHANGES-01-observed-change'])
def test_file_change_uncertainty(state, changes, may_change):
    seed(state, changed=changes)
    assert summary(state)['jobs'][0]['may_have_changed_files'] is may_change


def test_projection_does_not_mutate(state, monkeypatch):
    """PR-READ-01: constructor, collect, repair, purge and commits forbidden."""
    seed(state)
    before = snapshot(state[1])
    def forbidden(*a, **kw):
        pytest.fail('read projection invoked a mutation/collection seam')
    for name in ('__init__', 'collect', 'pending', 'acknowledge', 'purge_expired', '_load',
                 '_commit', '_transaction', '_apply', '_compact'):
        monkeypatch.setattr(storage.FileOutputStore, name, forbidden)
    from modelquad import paths
    monkeypatch.setattr(paths, 'pending_output_dir', lambda: state[1].root)
    assert summary(state)['count'] == 1
    assert storage.default_store(metadata_only=True).host_summary(
        state[0], KEY, now=NOW, deadline=100, clock=lambda: 1)['count'] == 1
    assert snapshot(state[1]) == before


@pytest.mark.parametrize('damage', ['torn', 'open', 'slot'], ids=[
    'PR-READ-01-torn', 'PR-READ-01-open', 'PR-READ-01-slot-uncertain'])
def test_uncertain_journal_is_unavailable_and_unchanged(state, damage):
    seed(state)
    root = state[1].root
    if damage == 'slot':
        (root / storage.REMOVAL_SLOT_NAME).write_bytes(b'x' * storage.REMOVAL_SLOT_BYTES)
    else:
        with (root / 'outcomes.jsonl').open('ab') as stream:
            stream.write(b'{' if damage == 'torn' else
                (json.dumps({'kind': 'transaction', 'id': 'unclosed', 'watermark': NOW.isoformat(),
                             'operations': [], 'outcomes': []}) + '\n').encode())
    before = snapshot(state[1])
    with pytest.raises(storage.PendingOutputTemporarilyUnavailable):
        summary(state)
    assert snapshot(state[1]) == before


@pytest.mark.parametrize('bound', ['scan', 'bytes', 'time', 'lock'], ids=[
    'PR-BUDGET-01-scan', 'PR-BUDGET-01-bytes', 'PR-BUDGET-01-time', 'PR-BUDGET-01-lock'])
def test_projection_bounds_fail_closed(state, monkeypatch, bound):
    seed(state)
    kwargs = {'deadline': 100, 'clock': lambda: 1}
    if bound == 'scan':
        kwargs['scan_cap'] = 1
    elif bound == 'bytes':
        kwargs['byte_cap'] = 1
    elif bound == 'time':
        kwargs['deadline'] = 1
    else:
        monkeypatch.setitem(storage._locks, state[1].root.resolve(),
                            SimpleNamespace(acquire=lambda **kw: False))
    before = snapshot(state[1])
    with pytest.raises(storage.PendingOutputTemporarilyUnavailable):
        storage.pending_metadata(state[1].root, NOW, **kwargs)
    assert snapshot(state[1]) == before


@pytest.mark.parametrize('end', ['expired', 'acknowledged'], ids=['PR-RACE-01-expired', 'PR-RACE-01-acknowledged'])
def test_reminder_collection_races(state, end):
    item = seed(state)
    assert summary(state)['count'] == 1
    collected = state[1].collect(item.job.job_id, NOW)
    when = NOW
    if end == 'acknowledged':
        assert state[1].acknowledge(item.job.job_id, collected.receipt, NOW)
    else:
        when = NOW + RETENTION
    assert state[1].collect(item.job.job_id, when) is None
    assert summary(state, now=when)['count'] == 0


@pytest.fixture
def surface(state, monkeypatch):
    app = create_app(db_path=state[0], poll_providers=False, write_secret=TOKEN)
    app.state.db_path = state[0]
    original = storage.host_summary
    monkeypatch.setattr(storage, 'host_summary', lambda path, key, **kw: original(
        path, key, now=NOW, root=state[1].root, deadline=100, clock=lambda: 1))
    # No lifespan: no service maintenance threads, scheduler or process census.
    return TestClient(app, base_url='http://127.0.0.1:8765'), state


@pytest.mark.parametrize('method', ['GET', 'HEAD'], ids=['PR-AUTH-01-get', 'PR-AUTH-01-head'])
@pytest.mark.parametrize('token,code', [(None, trust.TOKEN_MISSING), ('wrong', trust.TOKEN_INVALID)])
def test_token_refused_before_data_read(surface, monkeypatch, method, token, code):
    client, _ = surface
    monkeypatch.setattr(storage, 'host_summary', lambda *a, **kw: pytest.fail('data read before token'))
    response = client.request(method, '/api/pending-output?summary=host&workspace_key=' + KEY,
                              headers={'X-ModelQuad-Token': token} if token else {})
    assert response.status_code == 403
    if method == 'GET':
        assert response.json()['code'] == code


def test_secret_unavailable_refuses_summary_before_route():
    """PR-AUTH-01-unavailable: common guard, including HEAD."""
    boundary = trust.LoopbackTrustMiddleware(None)
    assert boundary.refusal(Headers({'host': '127.0.0.1:8765', 'X-ModelQuad-Token': TOKEN}),
                            'GET', '/api/pending-output')[2] == trust.TOKEN_UNAVAILABLE


@pytest.mark.parametrize('headers,status', [
    ({'Host': 'foreign.example'}, 421), ({'Origin': 'http://127.0.0.1:9999'}, 403),
    ({'Sec-Fetch-Site': 'cross-site'}, 403)], ids=['PR-AUTH-02-host', 'PR-AUTH-02-origin', 'PR-AUTH-02-fetch'])
def test_browser_guard_precedes_summary(surface, monkeypatch, headers, status):
    client, _ = surface
    monkeypatch.setattr(storage, 'host_summary', lambda *a, **kw: pytest.fail('browser boundary bypass'))
    response = client.get('/api/pending-output?summary=host&workspace_key=' + KEY,
                           headers={'X-ModelQuad-Token': TOKEN, **headers})
    assert response.status_code == status


def test_authenticated_summary_and_strict_query(surface):
    """PR-PROJECT-01-api: strict Host mode shares existing pending-output route."""
    client, state = surface
    seed(state)
    assert client.get('/api/pending-output?summary=host&workspace_key=' + KEY,
                      headers={'X-ModelQuad-Token': TOKEN}).json() == summary(state)
    for query in ('summary=host', 'summary=desktop&workspace_key=' + KEY,
                  'summary=host&workspace_key=bad', 'summary=host&workspace_key=' + KEY + '&prompt=canary',
                  'summary=host&workspace_key=' + KEY + '&summary=host'):
        assert client.get('/api/pending-output?' + query,
                          headers={'X-ModelQuad-Token': TOKEN}).status_code == 400


def test_summary_data_failures_are_fixed_labels(surface, monkeypatch):
    """PR-DOWN-01-api: store/DB failures carry no private exception body."""
    client, state = surface
    seed(state)
    def fail(*a, **kw):
        raise OSError('ANSWER-CANARY /private/path SECRET-CANARY')
    monkeypatch.setattr(storage, 'host_summary', fail)
    response = client.get('/api/pending-output?summary=host&workspace_key=' + KEY,
                           headers={'X-ModelQuad-Token': TOKEN})
    assert response.status_code == 503
    assert response.json() == {'detail': 'pending_summary_unavailable'}
