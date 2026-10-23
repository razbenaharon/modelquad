# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline CLI and identity-checked management transport; no live listeners."""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from modelquad import cli
from modelquad.jobs import service_client as sc
from modelquad.service import db, history
from modelquad.service.api import state_identity_tag
from modelquad.service import listener, listener_proof
from modelquad.service.socket_identity import ListenerError
from modelquad.statecontext import resolve_state_context

JOB = str(uuid.UUID(int=101))
DECISION = str(uuid.UUID(int=201))
STAMP = '2026-10-10T12:00:00+00:00'
SECRET = 'synthetic-installation-token-0123456789'
CANARY = 'C:/secret/canary TOKEN_DO_NOT_ECHO'


@pytest.fixture(autouse=True)
def no_platform_probe(monkeypatch):
    monkeypatch.setattr(cli,'preseed_windows_uname',lambda:None)


def payload(*, released=False):
    audit = {'job_id':JOB,'attempt_id':1,'decision_id':DECISION,'observed_at':STAMP,
             'released_at':STAMP,'reason':'owner_released_unknown_fate','surface':'cli'}
    if released:
        return {'job_id':JOB,'release':audit}
    return {'job_id':JOB,'created_at':STAMP,'blocker':True,'release':None,
            'live_codex':{'observed_at':STAMP,'status':'partial',
                          'processes':[{'pid':202,'created_at':None}],
                          'reason':'entries_unreadable_or_bounded'}}


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setenv('MODELQUAD_STATE_ROOT',str(tmp_path/'state'))
    monkeypatch.setenv('MODELQUAD_STATE_MODE','dev')
    path = tmp_path/'history.sqlite'
    root = resolve_state_context()
    replies = [(200,{'product':'modelquad','service_role':'control-plane',
                      'instance_kind':root.mode.value,'state_root_id':state_identity_tag(root.state_root),
                      'version':'0.2.0','listener_auth_protocol':1,
                      'db_schema_version':db.SCHEMA_VERSION,'db_identity':state_identity_tag(path.resolve()),
                      'detached_runner':{'ok':False}})]
    requests, reads, events = [], [], []
    endpoint = ['127.0.0.1',49152,'127.0.0.1',8765]
    class Socket:
        closed = False
        def getsockname(self):
            return tuple(endpoint[:2])
        def getpeername(self):
            return tuple(endpoint[2:])
        def close(self):
            self.closed = True
        def shutdown(self,*_args):
            self.close()
        def settimeout(self,_timeout):
            pass
    sock = Socket()
    class Owner:
        changed = False
        closed = False
        def recheck(self,connection):
            assert connection is sock
            if self.changed:
                raise ListenerError('service_connection_changed')
        def close(self):
            self.closed = True
    owner = Owner()
    class Response:
        def __init__(self,status,body):
            self.status, self.body = status, json.dumps(body).encode()
        def read(self,limit):
            return self.body[:limit]
        def getheader(self,_name,default=None):
            return default
    class Connection:
        def __init__(self,host,port,timeout):
            assert host == '127.0.0.1' and port == 8765
            self.sock = None
        def request(self,method,path,body=b'',headers=None):
            # A closed authenticated connection must never silently reconnect.
            assert self.sock is sock and not sock.closed
            requests.append((method,path,body,headers))
        def getresponse(self):
            status,body = replies.pop(0)
            if requests[-1][1] == '/api/listener-proof':
                nonce = json.loads(requests[-1][2])['nonce']
                body = listener_proof.make_proof(listener_proof.derive_key(SECRET),
                                                nonce,endpoint,body,boot='a'*32)
            return Response(status,body)
        def close(self):
            self.sock = None
            sock.close()
    def verify(*_args,**_kwargs):
        events.append('owner')
        return owner
    def secret(root):
        assert events == ['owner']
        events.append('secret')
        reads.append(root)
        return SECRET
    monkeypatch.setattr(listener.socket,'create_connection',lambda *_a,**_k:sock)
    monkeypatch.setattr(listener,'admitted_service_images',lambda _:frozenset({'image'}))
    monkeypatch.setattr(listener,'verify_socket_owner',verify)
    monkeypatch.setattr(listener,'_BoundHTTPConnection',Connection)
    monkeypatch.setattr(sc,'_service_port',lambda:8765)
    monkeypatch.setattr(listener.auth,'read_service_secret',secret)
    return SimpleNamespace(path=path,replies=replies,requests=requests,reads=reads,
                           events=events,sock=sock,owner=owner)


def test_management_keeps_identity_and_secret_checks_without_launch_containment(transport):
    path,replies,requests,reads = transport.path,transport.replies,transport.requests,transport.reads
    connection = sc.connect_management(path)
    assert isinstance(connection,sc.ServiceClient) and len(reads) == 1
    assert requests[0][0:2] == ('POST','/api/listener-proof')
    assert 'X-ModelQuad-Token' not in requests[0][3]
    assert transport.events == ['owner','secret']
    replies.append((200,payload()))
    assert connection.fate(JOB) == payload()
    assert requests[-1][0:2] == ('GET',f'/api/jobs/{JOB}/fate')
    assert requests[-1][3]['X-ModelQuad-Token'] == SECRET
    replies.append((200,payload(released=True)))
    assert connection.release_fate(JOB,acknowledge=True) == payload(released=True)
    assert json.loads(requests[-1][2]) == {'acknowledge_unknown_worker_fate':True,'surface':'cli'}
    assert SECRET not in repr(connection)
    assert all(request[1] != '/api/health' for request in requests)
    assert not transport.sock.closed
    connection._listener.close()


def test_launch_connection_still_refuses_contained_service(transport):
    assert sc.connect(transport.path) == sc.Unavailable('service_contained')
    # Containment is signed health: ownership precedes the local secret read,
    # but no credential or job is sent before the launch gate refuses.
    assert len(transport.reads) == 1 and transport.events == ['owner','secret']
    assert len(transport.requests) == 1
    assert 'X-ModelQuad-Token' not in transport.requests[0][3]
    assert transport.sock.closed and transport.owner.closed


@pytest.mark.parametrize('failure', ['foreign','unavailable-secret','redirect'])
def test_management_refuses_identity_secret_and_redirect_failures(transport,monkeypatch,failure):
    path,replies,requests,reads = transport.path,transport.replies,transport.requests,transport.reads
    if failure == 'foreign':
        replies[0][1]['db_identity'] = 'foreign'
        assert sc.connect_management(path) == sc.Unavailable('service_identity_mismatch')
        assert len(reads) == 1 and transport.events == ['owner','secret']
        assert len(requests) == 1 and 'X-ModelQuad-Token' not in requests[0][3]
        assert transport.sock.closed and transport.owner.closed
    elif failure == 'unavailable-secret':
        monkeypatch.setattr(listener.auth,'read_service_secret',lambda _:None)
        assert sc.connect_management(path) == sc.Unavailable('service_secret_unavailable')
        assert requests == [] and reads == []
        assert transport.sock.closed and transport.owner.closed
    else:
        c = sc.connect_management(path)
        replies.append((302,{'detail':CANARY}))
        assert c.fate(JOB) == sc.Unavailable('service_redirect_refused')
        assert len(requests) == 2
        assert transport.sock.closed and transport.owner.closed


def test_management_payload_canaries_and_unacknowledged_release_are_refused(transport):
    path,replies,requests = transport.path,transport.replies,transport.requests
    c = sc.connect_management(path)
    assert c.release_fate(JOB,acknowledge=False) == sc.Unavailable('fate_acknowledgment_required')
    assert len(requests) == 1
    replies.append((200,payload() | {'command_line':CANARY}))
    assert c.fate(JOB) == sc.Unavailable('fate_bad_response')
    replies.append((409,{'detail':CANARY,'code':CANARY}))
    assert c.fate(JOB) == sc.Unavailable('fate_service_refused')
    c._listener.close()


@pytest.mark.parametrize('change,reason',[
    ({'product':'foreign'},'foreign_listener'),
    ({'db_schema_version':True},'schema'),
    ({'detached_runner':{'ok':1}},'service_identity_mismatch'),
    ({'detached_runner':None},'service_identity_mismatch'),
],ids=['foreign-product','boolean-schema','nonboolean-containment','missing-containment'])
def test_management_bypasses_only_launch_containment(transport,change,reason):
    transport.replies[0][1].update(change)
    assert sc.connect_management(transport.path) == sc.Unavailable(reason)
    assert len(transport.requests) == 1
    assert 'X-ModelQuad-Token' not in transport.requests[0][3]
    assert transport.sock.closed and transport.owner.closed


def test_management_foreign_owner_reads_and_sends_nothing(transport,monkeypatch):
    def refuse(*_args,**_kwargs):
        raise ListenerError('service_owner_mismatch')
    monkeypatch.setattr(listener,'verify_socket_owner',refuse)
    assert sc.connect_management(transport.path) == sc.Unavailable('service_owner_mismatch')
    assert transport.reads == transport.requests == []
    assert transport.sock.closed


@pytest.mark.parametrize('release',[False,True],ids=['fate-read','fate-release'])
def test_management_rechecks_owner_before_fate_credentials(transport,release):
    connection = sc.connect_management(transport.path)
    assert isinstance(connection,sc.ServiceClient)
    transport.owner.changed = True
    result = (connection.release_fate(JOB,acknowledge=True) if release else connection.fate(JOB))
    assert result == sc.Unavailable('service_connection_changed')
    assert len(transport.requests) == 1
    assert 'X-ModelQuad-Token' not in transport.requests[0][3]
    assert transport.sock.closed and transport.owner.closed


def test_cli_requires_acknowledgment_before_connection(monkeypatch,capsys):
    monkeypatch.setattr(sc,'connect_management',lambda _:pytest.fail('no default consent'))
    assert cli.main(['jobs','release-fate',JOB]) == 2
    assert '--acknowledge-unknown-worker-fate' in capsys.readouterr().err


def test_cli_targets_job_through_service_and_never_reads_database(tmp_path,monkeypatch,capsys):
    path = tmp_path/'never-created.sqlite'
    calls = []
    class Client:
        def fate(self,job):
            calls.append(('read',job))
            return payload()
        def release_fate(self,job,*,acknowledge):
            calls.append(('release',job,acknowledge))
            return payload(released=True)
    monkeypatch.setattr(sc,'connect_management',lambda db_path:calls.append(('connect',db_path)) or Client())
    monkeypatch.setattr(db,'connect_and_init',lambda _:pytest.fail('no database fallback'))
    assert cli.main(['--history-db',str(path),'jobs','fate',JOB,'--json']) == 0
    assert json.loads(capsys.readouterr().out) == payload()
    assert cli.main(['--history-db',str(path),'jobs','release-fate',JOB,'--acknowledge-unknown-worker-fate']) == 0
    assert 'Closed — workspace block released' in capsys.readouterr().out
    assert ('read',JOB) in calls and ('release',JOB,True) in calls and not path.exists()


def test_cli_unavailable_service_has_no_fallback(monkeypatch,capsys):
    monkeypatch.setattr(sc,'connect_management',lambda _:sc.Unavailable('service_foreign'))
    monkeypatch.setattr(db,'connect_and_init',lambda _:pytest.fail('no database fallback'))
    assert cli.main(['jobs','fate',JOB,'--json']) == 1
    assert capsys.readouterr().err.strip() == 'service_foreign'
