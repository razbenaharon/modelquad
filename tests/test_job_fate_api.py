# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline authenticated fate management; no service lifespan or real probes."""
from datetime import datetime, timezone
import json
import sqlite3
import uuid

from fastapi.testclient import TestClient
import pytest

from modelquad.service import fate_history as _fate_store
from modelquad import procutil
from modelquad.provenance import new_decision_id
from modelquad.service import db, fate_api, history
from modelquad.service.app import LoopbackFastAPI

JOB = str(uuid.UUID(int=101))
DECISION = str(uuid.UUID(int=201))
STAMP = '2026-10-10T12:00:00+00:00'
SECRET = 'synthetic-installation-token-0123456789'
TOKEN = {'X-ModelQuad-Token': SECRET}
CANARY = 'C:/secret/canary --command TOKEN_DO_NOT_ECHO'


@pytest.fixture
def lab(tmp_path, monkeypatch):
    path = tmp_path/'fate.sqlite'
    db.connect_and_init(path).close()
    workspace, ancestors = history.workspace_identity(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO delegation_attempts (id,decision_id,created_at,task_type,status,workspace_key,workspace_ancestors,detail) "
                     "VALUES (1,?,?,'exploration','started',?,?,?)", (DECISION,STAMP,workspace,ancestors,CANARY))
        conn.execute("INSERT INTO jobs (job_id,attempt_id,decision_id,status,created_at,updated_at,worker_pid,worker_created) "
                     "VALUES (?,1,?,'unknown',?,?,202,20)", (JOB,DECISION,STAMP,STAMP))
    probes = []
    def listing():
        probes.append(True)
        return procutil.CodexProcesses(STAMP,'partial',(procutil.CodexProcess(202,None),),
                                     'entries_unreadable_or_bounded')
    monkeypatch.setattr(procutil,'live_codex_processes',listing)
    monkeypatch.setattr(procutil,'pid_state',lambda _: 'unknown')
    monkeypatch.setattr(procutil,'native_process_creation_epoch',lambda _: None)
    monkeypatch.setattr(procutil,'observe_boot',lambda:None)
    monkeypatch.setattr(fate_api,'_utc_now',lambda:datetime(2026,10,10,12,tzinfo=timezone.utc))
    return path, probes


@pytest.fixture(params=['dashed', 'hex'])
def decision_id(lab, request):
    decision = DECISION if request.param == 'dashed' else new_decision_id()
    with sqlite3.connect(lab[0]) as conn:
        conn.execute('UPDATE jobs SET decision_id=?', (decision,))
        conn.execute('UPDATE delegation_attempts SET decision_id=?', (decision,))
    return decision


def client(lab,secret=SECRET, *, secret_problem=None):
    app = LoopbackFastAPI(write_secret=secret, secret_problem=secret_problem)
    app.state.db_path = lab[0]
    app.include_router(fate_api.router)
    return TestClient(app,base_url='http://127.0.0.1:8765')


@pytest.mark.parametrize('method', ['GET','POST'])
@pytest.mark.parametrize('token,secret,code', [
    ({},SECRET,'service_token_missing'),
    ({'X-ModelQuad-Token':'wrong'},SECRET,'service_token_invalid'),
    (TOKEN,None,'service_token_unavailable'),
])
def test_token_refusals_precede_probing_and_writing(lab,method,token,secret,code):
    suffix = 'fate' if method == 'GET' else 'release-fate'
    response = client(lab,secret).request(method,f'/api/jobs/{JOB}/{suffix}',headers=token,
                                         json={'acknowledge_unknown_worker_fate':True,'surface':'cli'})
    assert response.status_code == 403 and response.json()['code'] == code
    assert lab[1] == []
    with sqlite3.connect(lab[0]) as conn:
        assert conn.execute('SELECT count(*) FROM job_fate_releases').fetchone()[0] == 0


@pytest.mark.parametrize('method,suffix', [('GET', 'fate'), ('POST', 'release-fate')],
                         ids=['read', 'release'])
def test_unavailable_secret_diagnostic_is_redacted_before_dispatch(lab, caplog, method, suffix):
    target = f'/api/jobs/{JOB}/{suffix}'
    c = client(lab, None, secret_problem=f'Cannot verify secret file {CANARY}; content={SECRET}')
    response = c.request(method, target, headers=TOKEN,
                         json={'acknowledge_unknown_worker_fate': True, 'surface': 'cli'})
    assert response.status_code == 403
    assert response.json() == {'code': 'service_token_unavailable', 'detail': 'service_token_unavailable'}
    assert CANARY not in response.text and SECRET not in response.text
    assert CANARY not in caplog.text and SECRET not in caplog.text
    assert lab[1] == []
    with sqlite3.connect(lab[0]) as conn:
        assert conn.execute('SELECT count(*) FROM job_fate_releases').fetchone()[0] == 0


def test_get_and_owner_release_allow_other_attempts_on_same_decision(lab, decision_id):
    workspace, ancestors = history.workspace_identity(lab[0].parent)
    with sqlite3.connect(lab[0]) as conn:
        conn.execute("INSERT INTO delegation_attempts (id,decision_id,created_at,task_type,status,workspace_key,workspace_ancestors) "
                     "VALUES (2,?,?,'exploration','started',?,?)", (decision_id, STAMP, workspace, ancestors))
    before = history.read_job_attempt(lab[0], 2)
    c = client(lab)
    assert c.get(f'/api/jobs/{JOB}/fate', headers=TOKEN).json()['blocker'] is True
    response = c.post(f'/api/jobs/{JOB}/release-fate', headers=TOKEN,
                      json={'acknowledge_unknown_worker_fate': True, 'surface': 'cli'})
    assert response.status_code == 200
    assert response.json()['release']['attempt_id'] == 1
    assert c.get(f'/api/jobs/{JOB}/fate', headers=TOKEN).json()['release'] == response.json()['release']
    assert history.read_job_attempt(lab[0], 2) == before
    assert history.diagnosis_admission(lab[0], lab[0].parent)['allowed'] is False


@pytest.mark.parametrize('method', ['GET','POST'])
@pytest.mark.parametrize('headers,status', [
    ({'Host':'foreign.invalid'},421),
    ({'Origin':'http://127.0.0.1:9999'},403),
    ({'Sec-Fetch-Site':'cross-site'},403),
])
def test_host_and_origin_protections_remain_authoritative(lab,method,headers,status):
    suffix = 'fate' if method == 'GET' else 'release-fate'
    response = client(lab).request(method,f'/api/jobs/{JOB}/{suffix}',headers=TOKEN|headers)
    assert response.status_code == status and lab[1] == []


def test_get_returns_bounded_pid_start_list_without_private_evidence(lab, decision_id):
    response = client(lab).get(f'/api/jobs/{JOB}/fate',headers=TOKEN)
    assert response.status_code == 200
    payload = response.json()
    assert payload['blocker'] is True and payload['release'] is None
    assert payload['live_codex'] == {'observed_at':STAMP,'status':'partial',
                                    'processes':[{'pid':202,'created_at':None}],
                                    'reason':'entries_unreadable_or_bounded'}
    assert CANARY not in response.text and SECRET not in response.text
    jobs = _fate_store.candidates(lab[0], limit=256)
    assert jobs[0]['job_id'] == JOB
    # The banner gets a content-free task kind and nothing else from the attempt.
    assert set(jobs[0]) == {'job_id', 'created_at', 'task_type'}
    assert jobs[0]['task_type'] is None or jobs[0]['task_type'].isidentifier()


@pytest.mark.parametrize('body', [
    {}, {'surface':'ui'}, {'acknowledge_unknown_worker_fate':False,'surface':'ui'},
    {'acknowledge_unknown_worker_fate':1,'surface':'ui'},
    {'acknowledge_unknown_worker_fate':True,'surface':'ui','actor':CANARY},
    {'acknowledge_unknown_worker_fate':True,'surface':CANARY},
    {'acknowledge_unknown_worker_fate':True,'surface':'ui','explanation':'x'*300},
])
def test_strict_acknowledged_body_rejects_input_without_echo(lab,body):
    response = client(lab).post(f'/api/jobs/{JOB}/release-fate',headers=TOKEN,json=body)
    assert response.status_code == 400 and response.json()['code'] == 'fate_request_invalid'
    assert CANARY not in response.text and SECRET not in response.text


def test_duplicate_body_keys_are_refused(lab):
    response = client(lab).post(f'/api/jobs/{JOB}/release-fate',headers=TOKEN,
                               content='{"surface":"ui","surface":"cli","acknowledge_unknown_worker_fate":true}')
    assert response.status_code == 400


def test_manual_release_derives_installation_principal_and_refreshes_audit(lab,caplog,decision_id):
    c = client(lab)
    body = {'acknowledge_unknown_worker_fate':True,'surface':'ui'}
    response = c.post(f'/api/jobs/{JOB}/release-fate',headers=TOKEN,json=body)
    assert response.status_code == 200
    audit = response.json()['release']
    assert audit['job_id'] == JOB and audit['reason'] == 'owner_released_unknown_fate'
    assert audit['decision_id'] == decision_id
    assert c.post(f'/api/jobs/{JOB}/release-fate',headers=TOKEN,json=body).json() == response.json()
    assert c.get(f'/api/jobs/{JOB}/fate',headers=TOKEN).json()['release'] == audit
    assert _fate_store.candidates(lab[0], limit=256) == []
    with sqlite3.connect(lab[0]) as conn:
        row = conn.execute('SELECT actor,authentication_method,surface FROM job_fate_releases').fetchone()
        assert row[0].startswith('installation:') and row[1:] == ('installation_token','ui')
        assert CANARY not in repr(conn.execute('SELECT * FROM job_fate_releases').fetchall())
    assert CANARY not in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize('status', ['unknown', 'timed_out'])
def test_get_and_post_release_for_gone_worker_without_boot(lab, monkeypatch, decision_id, status):
    monkeypatch.setattr(procutil, 'pid_state', lambda _: 'dead')
    with sqlite3.connect(lab[0]) as conn:
        conn.execute('UPDATE jobs SET status=?,ended_at=?', (status, STAMP))
        conn.execute("UPDATE delegation_attempts SET status='failed',failure_kind='timeout',resolved_at=?",
                     (STAMP,))
    before = history.read_job_attempt(lab[0], 1)
    c = client(lab)
    response = c.get(f'/api/jobs/{JOB}/fate', headers=TOKEN)
    assert response.status_code == 200
    assert response.json()['blocker'] is True and response.json()['release'] is None
    released = c.post(f'/api/jobs/{JOB}/release-fate', headers=TOKEN,
                      json={'acknowledge_unknown_worker_fate': True, 'surface': 'ui'})
    assert released.status_code == 200
    audit = released.json()['release']
    assert audit['decision_id'] == decision_id and audit['attempt_id'] == 1
    assert audit['reason'] == 'owner_released_unknown_fate'
    assert history.read_job_attempt(lab[0], 1) == before
    refreshed = c.get(f'/api/jobs/{JOB}/fate', headers=TOKEN)
    assert refreshed.status_code == 200
    assert refreshed.json()['blocker'] is False and refreshed.json()['release'] == audit
    assert _fate_store.candidates(lab[0], limit=256) == []


@pytest.mark.parametrize('status', ['complete','partial','unavailable'])
def test_empty_or_incomplete_process_lists_never_prove_death(lab,monkeypatch,status):
    monkeypatch.setattr(procutil,'live_codex_processes',lambda:procutil.CodexProcesses(STAMP,status,()))
    assert client(lab).get(f'/api/jobs/{JOB}/fate',headers=TOKEN).json()['blocker'] is True
    with sqlite3.connect(lab[0]) as conn:
        assert conn.execute('SELECT count(*) FROM job_fate_releases').fetchone()[0] == 0


def test_native_listing_only_returns_pid_utc_and_truthful_status():
    clock = lambda: datetime(2026,10,10,12,tzinfo=timezone.utc)
    listing = procutil.live_codex_processes(clock=clock,
        snapshot=lambda:[(101,0,'codex.exe'),(202,0,CANARY),(303,0,'CODEX.EXE')],
        state=lambda _:'alive',created=lambda pid:10.0 if pid == 101 else None)
    assert listing.status == 'partial' and [p.pid for p in listing.processes] == [101,303]
    assert listing.processes[1].created_at is None and CANARY not in repr(listing)
    unavailable = procutil.live_codex_processes(clock=clock,snapshot=lambda:None)
    assert unavailable.status == 'unavailable'
    bounded = procutil.live_codex_processes(clock=clock,snapshot=lambda:[(i,0,'codex.exe') for i in range(1,10)],
                                           state=lambda _:'alive',created=lambda _:10.0,limit=2)
    assert bounded.status == 'partial' and len(bounded.processes) == 2
