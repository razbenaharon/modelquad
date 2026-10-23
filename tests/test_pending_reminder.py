# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline startup contracts; written for a maintainer, not run by worker."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.adapters import pending_reminder as reminder
from orchestrator.jobs import service_client
from orchestrator.service import listener
from orchestrator.service.socket_identity import ListenerError


KEY = 'a' * 32
SUMMARY = {'count': 1, 'more': False, 'jobs': [{
    'job_id': 'job-01', 'job_status': 'finished', 'task_type': 'review',
    'may_have_changed_files': True, 'expires_at': '2026-10-11T12:00:00Z',
    'expiring_soon': True,
}]}


def invoke(monkeypatch, *, host='claude', event=None, raw=None, fetch=None, environ=None, clock=lambda: 10):
    monkeypatch.setattr(reminder, 'project_key', lambda cwd: KEY if cwd == 'C:/synthetic/project' else None)
    payload = event if event is not None else {'hook_event_name': 'SessionStart', 'cwd': 'C:/synthetic/project'}
    stream = io.BytesIO(raw if raw is not None else json.dumps(payload).encode())
    out = io.StringIO()
    code = reminder.run_hook(host, stream, out, environ=environ or {}, clock=clock,
                             fetch=fetch or (lambda *a, **kw: SUMMARY))
    return code, out.getvalue()


@pytest.mark.parametrize('host', ['claude', 'codex'], ids=['PR-CLAUDE-01', 'PR-CODEX-01'])
def test_exact_context_envelope(monkeypatch, host, capsys):
    code, output = invoke(monkeypatch, host=host)
    assert code == 0
    assert json.loads(output) == {'hookSpecificOutput': {
        'hookEventName': 'SessionStart', 'additionalContext': reminder.reminder_text(SUMMARY)}}
    assert set(json.loads(output)) == {'hookSpecificOutput'}
    assert capsys.readouterr() == ('', '')
    assert 'codex-cli 0.162.0' in reminder.CODEX_DELIVERY_EVIDENCE
    assert 'UNVERIFIED' not in output


@pytest.mark.parametrize('raw', [b'{', b'[]', b'x' * (reminder.BYTE_CAP + 1),
    b'{"hook_event_name":"PreToolUse","cwd":"C:/synthetic/project"}',
    b'{"hook_event_name":"SessionStart","cwd":"."}',
    b'{"hook_event_name":"SessionStart","cwd":null}'], ids=[
    'PR-INPUT-01-malformed', 'PR-INPUT-01-array', 'PR-INPUT-01-oversize',
    'PR-INPUT-01-event', 'PR-INPUT-01-relative', 'PR-INPUT-01-absent'])
def test_bad_input_never_reads_service(monkeypatch, raw, capsys):
    def forbidden(*a, **kw):
        pytest.fail('service read for invalid input')
    assert invoke(monkeypatch, raw=raw, fetch=forbidden) == (0, '')
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('failure', ['service_absent', 'service_owner_mismatch',
    'service_redirect_refused', 'database_unavailable', 'store_unavailable'], ids=[
    'PR-DOWN-01-absent', 'PR-DOWN-01-foreign', 'PR-AUTH-02-redirect',
    'PR-DOWN-01-db', 'PR-DOWN-01-store'])
def test_unavailable_is_silent(monkeypatch, failure, capsys):
    def fetch(*a, **kw):
        raise OSError(failure + ' private-answer /private/path SECRET-CANARY')
    assert invoke(monkeypatch, fetch=fetch) == (0, '')
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('summary', [{'count': 0, 'more': False, 'jobs': []},
    {**SUMMARY, 'output': 'SECRET-CANARY'},
    {**SUMMARY, 'jobs': [{**SUMMARY['jobs'][0], 'job_id': 'private/path'}]},
    {**SUMMARY, 'jobs': [{**SUMMARY['jobs'][0], 'task_type': 'PROMPT-CANARY'}]}],
    ids=['PR-DOWN-01-empty', 'PR-PRIVACY-01-content', 'PR-PRIVACY-01-id', 'PR-PRIVACY-01-label'])
def test_only_closed_metadata_is_allowed(monkeypatch, summary):
    assert invoke(monkeypatch, fetch=lambda *a, **kw: summary) == (0, '')


@pytest.mark.parametrize('host', ['claude', 'codex'], ids=['PR-PRIVACY-02-claude', 'PR-PRIVACY-02-codex'])
def test_transcript_is_not_read_or_statted(monkeypatch, host):
    def forbidden(*a, **kw):
        pytest.fail('transcript/file operation')
    for method in ('open', 'read_bytes', 'read_text', 'stat', 'lstat'):
        monkeypatch.setattr(Path, method, forbidden)
    code, output = invoke(monkeypatch, host=host, event={
        'hook_event_name': 'SessionStart', 'cwd': 'C:/synthetic/project',
        'transcript_path': '/private/TRANSCRIPT-CANARY', 'prompt': 'PROMPT-CANARY',
        'receipt': 'RECEIPT-CANARY', 'secret': 'SECRET-CANARY'})
    assert code == 0 and output
    assert all(c not in output for c in ('TRANSCRIPT-CANARY', 'PROMPT-CANARY',
                                         'RECEIPT-CANARY', 'SECRET-CANARY'))


@pytest.mark.parametrize('host', ['claude', 'codex'], ids=['PR-WORKER-01-claude', 'PR-WORKER-01-codex'])
def test_worker_returns_before_input_or_service(monkeypatch, host):
    def forbidden(*a, **kw):
        pytest.fail('worker touched stdin, clock or service')
    out = io.StringIO()
    assert reminder.run_hook(host, SimpleNamespace(read=forbidden), out,
        environ={'ORCHESTRATOR_WORKER': '1'}, clock=forbidden, fetch=forbidden) == 0
    assert out.getvalue() == ''


@pytest.mark.parametrize('phase', ['input', 'reply'], ids=['PR-BUDGET-01-input', 'PR-BUDGET-01-reply'])
def test_single_deadline_discards_late_results(monkeypatch, phase):
    times = iter([10, 12] if phase == 'input' else [10, 10, 10, 10, 12])
    assert invoke(monkeypatch, clock=lambda: next(times, 12)) == (0, '')


def test_management_read_keeps_one_authenticated_connection(monkeypatch):
    """PR-MGMT-01: synthetic bounded listener with runner requirement off."""
    calls, closed = [], []
    connection = SimpleNamespace(
        request=lambda *a, **kw: calls.append((a, kw)) or listener.Reply(
            200, 'application/json', json.dumps(SUMMARY).encode()),
        close=lambda: closed.append(True))
    def authenticate(port, **kw):
        assert kw['require_runner'] is False and kw['deadline'] == 20
        return connection
    monkeypatch.setattr(service_client.listener, 'authenticate', authenticate)
    monkeypatch.setattr(service_client, '_service_port', lambda **kw: 8765)
    monkeypatch.setattr(service_client.time, 'monotonic', lambda: 10)
    assert service_client.read_pending_summary(KEY, deadline=20) == SUMMARY
    assert len(calls) == 1 and calls[0][1] == {'cap': 32768, 'deadline': 20}
    assert calls[0][0] == ('GET', '/api/pending-output?summary=host&workspace_key=' + KEY)
    assert closed == [True]


def test_bounded_management_transport_is_available(transport):
    """PR-MGMT-01: actual proof/auth/HTTP client works with the runner off."""
    assert service_client.read_pending_summary(KEY, deadline=11.5, db_path=transport.database) == SUMMARY
    assert transport.paths == ['/api/listener-proof',
        '/api/pending-output?summary=host&workspace_key=' + KEY]
    assert transport.secret_reads == [True]
    assert transport.owner_checks >= 3
    assert b'X-ModelQuad-Token' not in transport.headers[0]
    assert transport.secret.encode() in transport.headers[1]
    assert transport.sock.closed and transport.owner_closed
    assert len(transport.connections) == 1
    assert all(timer.cancelled for timer in transport.timers)


@pytest.mark.parametrize('status', [302, 403, 503], ids=[
    'PR-AUTH-02-no-redirect', 'PR-AUTH-01-refused', 'PR-DOWN-01-reply-unavailable'])
def test_failed_reply_is_not_retried(monkeypatch, status):
    calls, closed = [], []
    connection = SimpleNamespace(request=lambda *a, **kw: calls.append(a) or
        listener.Reply(status, 'application/json', b'SECRET-CANARY'),
        close=lambda: closed.append(True))
    monkeypatch.setattr(service_client.listener, 'authenticate', lambda *a, **kw: connection)
    monkeypatch.setattr(service_client, '_service_port', lambda **kw: 8765)
    monkeypatch.setattr(service_client.time, 'monotonic', lambda: 10)
    with pytest.raises(ListenerError):
        service_client.read_pending_summary(KEY, deadline=20)
    assert len(calls) == 1 and closed == [True]


def test_cli_quiet_worker_dispatch(monkeypatch, capsys):
    """PR-WORKER-01-cli: no hook payload read after the worker guard."""
    from orchestrator import cli
    monkeypatch.setenv('ORCHESTRATOR_WORKER', '1')
    monkeypatch.setattr(cli.sys, 'stdin', SimpleNamespace(read=lambda *a: pytest.fail('stdin')))
    assert cli.main(['hook-pending', 'claude']) == 0
    assert capsys.readouterr() == ('', '')


def test_scoped_mcp_and_local_fallback(monkeypatch):
    """The scope is required; without the service, only this workspace's rows."""
    import inspect
    from orchestrator.mcp import jobs_tools
    assert inspect.signature(jobs_tools.modelquad_job_list_pending).parameters[
        'project_directory'].default is inspect.Parameter.empty
    monkeypatch.setattr(jobs_tools, '_guard', lambda: None)
    monkeypatch.setattr(jobs_tools, 'schedule_recovery', lambda *a, **kw: None)
    monkeypatch.setattr(jobs_tools.history, 'list_active_jobs', lambda *a: [
        {'job_id': 'open-01'}, {'job_id': 'other-02'}])
    monkeypatch.setattr(jobs_tools.history, 'read_job', lambda db, job_id: {
        'job_id': job_id, 'attempt_id': 1 if job_id == 'open-01' else 2})
    monkeypatch.setattr(jobs_tools.history, 'read_job_attempt', lambda db, attempt_id: {
        'workspace_key': KEY if attempt_id == 1 else 'another-workspace'})
    monkeypatch.setattr(jobs_tools, 'default_store', lambda: SimpleNamespace(pending=lambda now: ()))
    monkeypatch.setattr(reminder, 'project_key', lambda cwd: KEY if cwd == 'C:/synthetic/project' else None)
    monkeypatch.setattr(service_client, 'read_pending_summary', lambda *a, **kw: SUMMARY)
    assert jobs_tools.modelquad_job_list_pending('C:/synthetic/project') == SUMMARY
    assert jobs_tools.modelquad_job_list_pending('.') == {'error': 'pending_scope_invalid'}

    def absent(*a, **kw):
        raise ListenerError('service absent')
    monkeypatch.setattr(service_client, 'read_pending_summary', absent)
    assert jobs_tools.modelquad_job_list_pending('C:/synthetic/project') == {
        'jobs': [], 'running': [{'job_id': 'open-01'}]}


@pytest.fixture
def transport(monkeypatch, tmp_path):
    """Actual HTTPConnection/HTTPResponse and signed proof over a fake socket.

    Blocking reads advance virtual time to a byte arrival, socket timeout or
    scheduled shutdown, whichever comes first. No sleeps, sockets, processes,
    settings or accounts are real; parser, deadlines and auth are production.
    """
    from orchestrator import paths
    from orchestrator.service import auth, db, listener_proof as proof
    from orchestrator.service.api import state_identity_tag

    state = SimpleNamespace(now=10.0, timers=[], paths=[], headers=[], connections=[],
        secret_reads=[], owner_checks=0, owner_closed=False, owned=False,
        secret='synthetic-installation-secret-0123456789abcdef', secret_bytes=None,
        database=tmp_path / 'synthetic.db', connect_delay=0.2,
        auth_head=0.0, auth_byte=0.0, read_head=0.0, read_byte=0.0,
        read_stall=False, owner_failure=False, proof_status=200, read_status=200)
    root = tmp_path / 'synthetic-state'
    monkeypatch.setenv('MODELQUAD_STATE_MODE', 'dev')
    monkeypatch.setenv('MODELQUAD_STATE_ROOT', str(root))
    monkeypatch.setattr(paths, 'state_root', lambda: root)
    state.health = {'product': 'modelquad', 'service_role': 'control-plane',
        'instance_kind': 'dev', 'state_root_id': state_identity_tag(root),
        'db_schema_version': db.SCHEMA_VERSION,
        'db_identity': state_identity_tag(state.database.resolve()),
        'detached_runner': {'ok': False}, 'version': '0.2.0', 'listener_auth_protocol': 1}

    class Timer:
        def __init__(self, seconds, callback, args=()):
            self.seconds, self.callback, self.args = seconds, callback, args
            self.at = None
            self.cancelled = False
            state.timers.append(self)
        def start(self):
            self.at = state.now + self.seconds
        def cancel(self):
            self.cancelled = True

    def advance(seconds):
        target = state.now + seconds
        while True:
            due = [t for t in state.timers if not t.cancelled and t.at is not None and t.at <= target]
            if not due:
                break
            timer = min(due, key=lambda t: t.at)
            state.now = timer.at
            timer.cancelled = True
            timer.callback(*timer.args)
        state.now = target

    def block(seconds, sock):
        # Model a blocked recv; a drip byte resets its socket timeout, but
        # cannot reset the transport's absolute timer.
        due = [t.at - state.now for t in state.timers
               if not t.cancelled and t.at is not None]
        waited = min([seconds, sock.timeout] + due)
        advance(waited)
        if sock.closed or waited < seconds:
            raise TimeoutError('SECRET-CANARY')

    class Stream(io.RawIOBase):
        def __init__(self, sock, head, body, head_delay, byte_delay, stall):
            self.sock, self.head, self.body = sock, head, body
            self.head_delay, self.byte_delay, self.stall = head_delay, byte_delay, stall
            self.offset = 0
        def readable(self):
            return True
        def readinto(self, buffer):
            if self.sock.closed:
                raise OSError('closed synthetic socket')
            if self.head:
                block(self.head_delay, self.sock)
                self.head_delay = 0
                chunk, self.head = self.head[:len(buffer)], self.head[len(buffer):]
            elif self.offset < len(self.body):
                block(10 if self.stall else self.byte_delay, self.sock)
                count = 1 if self.byte_delay else len(buffer)
                chunk = self.body[self.offset:self.offset + count]
                self.offset += len(chunk)
            else:
                return 0
            buffer[:len(chunk)] = chunk
            return len(chunk)

    class Sock:
        closed = False
        timeout = 0.75
        def getsockname(self):
            return ('127.0.0.1', 43210)
        def getpeername(self):
            return ('127.0.0.1', 8765)
        def settimeout(self, seconds):
            self.timeout = seconds
        def close(self):
            self.closed = True
        def shutdown(self, how):
            self.closed = True
        def sendall(self, raw):
            if raw.startswith((b'POST ', b'GET ', b'HEAD ')):
                state.headers.append(raw)
                state.paths.append(raw.split(b' ', 2)[1].decode('ascii'))
        def makefile(self, mode):
            is_proof = state.paths[-1] == '/api/listener-proof'
            payload = (proof.make_proof(proof.derive_key(state.secret), 'd' * 64,
                ['127.0.0.1', 43210, '127.0.0.1', 8765], state.health, boot='a' * 32)
                if is_proof else SUMMARY)
            body = proof.canonical(payload)
            status = state.proof_status if is_proof else state.read_status
            head = (f'HTTP/1.1 {status} Synthetic\r\nContent-Length: {len(body)}\r\n'
                    'Content-Type: application/json\r\n\r\n').encode()
            return io.BufferedReader(Stream(self, head, body,
                state.auth_head if is_proof else state.read_head,
                state.auth_byte if is_proof else state.read_byte,
                state.read_stall and not is_proof))
    state.sock = Sock()
    def connect(address, *, timeout):
        assert address == ('127.0.0.1', 8765)
        state.connections.append(timeout)
        state.sock.settimeout(timeout)
        block(state.connect_delay, state.sock)
        return state.sock
    def verify(sock, images):
        assert sock is state.sock and images == frozenset({'synthetic-image'})
        if state.owner_failure:
            raise ListenerError('service_owner_mismatch')
        state.owned = True
        def recheck(sock):
            assert sock is state.sock and not sock.closed
            state.owner_checks += 1
        def close():
            state.owner_closed = True
        return SimpleNamespace(recheck=recheck, close=close)
    def secret_read(path):
        assert state.owned and path == root / auth.SECRET_FILE_NAME
        state.secret_reads.append(True)
        return state.secret.encode() if state.secret_bytes is None else state.secret_bytes
    monkeypatch.setattr(listener.time, 'monotonic', lambda: state.now)
    monkeypatch.setattr(listener.threading, 'Timer', Timer)
    monkeypatch.setattr(listener.socket, 'create_connection', connect)
    monkeypatch.setattr(listener, 'admitted_service_images', lambda port: frozenset({'synthetic-image'}))
    monkeypatch.setattr(listener, 'verify_socket_owner', verify)
    monkeypatch.setattr(listener.secrets, 'token_hex', lambda n: 'd' * (2 * n))
    monkeypatch.setattr(auth, '_read_windows', secret_read)
    monkeypatch.setattr(auth, '_read_posix', secret_read)
    monkeypatch.setattr(auth.time, 'sleep', lambda *a: pytest.fail('secret retry'))
    monkeypatch.setattr(service_client, '_service_port', lambda **kw: 8765)
    state.advance = advance
    return state


@pytest.mark.parametrize('phase', ['connect', 'auth-head', 'auth-drip', 'read-stall', 'read-drip', 'cumulative'],
                         ids=['PR-TIME-connect', 'PR-TIME-auth-head', 'PR-TIME-auth-drip',
                              'PR-TIME-read-stall', 'PR-TIME-read-drip', 'PR-TIME-total'])
def test_actual_transport_bounds_blocked_and_dripping_reads(transport, phase):
    if phase == 'connect':
        transport.connect_delay = 10
    elif phase == 'auth-head':
        transport.auth_head = 10
    elif phase == 'auth-drip':
        transport.auth_byte = 0.01
    elif phase == 'read-stall':
        transport.read_stall = True
    elif phase == 'read-drip':
        transport.read_byte = 0.01
    else:
        transport.auth_head = 0.6
        transport.read_head = 0.6
        transport.read_byte = 0.002
    with pytest.raises(ListenerError):
        service_client.read_pending_summary(KEY, deadline=11.5, db_path=transport.database)
    assert transport.now <= 11.5
    assert len(transport.connections) == 1
    assert len(transport.paths) == (0 if phase == 'connect' else 1 if phase.startswith('auth') else 2)
    if phase != 'connect':
        assert transport.sock.closed and transport.owner_closed
    assert all(timer.cancelled for timer in transport.timers)


@pytest.mark.parametrize('later', [None, 20], ids=['PR-TIME-inherited', 'PR-TIME-later'])
def test_authentication_budget_cannot_be_extended_or_forgotten(transport, later):
    """PR-TIME-shared: a read's default or later deadline cannot reset time."""
    transport.auth_head = 0.6
    connected = listener.authenticate(8765, db_path=transport.database,
                                      deadline=11.5, require_runner=False)
    assert transport.now == pytest.approx(10.8)
    transport.advance(0.5)
    transport.read_head = 0.3
    with pytest.raises(ListenerError):
        connected.request('GET', '/api/pending-output', deadline=later)
    assert transport.now == pytest.approx(11.5)
    assert transport.sock.closed and transport.owner_closed


@pytest.mark.parametrize('change,reason', [
    ({'db_identity': 'foreign'}, 'service_identity_mismatch'),
    ({'db_schema_version': True}, 'schema'),
    ({'detached_runner': {'ok': 1}}, 'service_identity_mismatch'),
    ({'product': 'foreign'}, 'foreign_listener')],
    ids=['PR-ID-db', 'PR-ID-schema', 'PR-ID-runner', 'PR-ID-product'])
def test_management_keeps_signed_identity_checks(transport, change, reason):
    transport.health.update(change)
    with pytest.raises(ListenerError, match=reason):
        service_client.read_pending_summary(KEY, deadline=11.5, db_path=transport.database)
    assert transport.paths == ['/api/listener-proof']
    assert b'X-ModelQuad-Token' not in transport.headers[0]
    assert transport.sock.closed and transport.owner_closed


def test_management_secret_has_one_verified_read_and_no_retry(transport):
    """PR-AUTH-empty: an empty secret does not spend time retrying."""
    transport.secret_bytes = b''
    with pytest.raises(ListenerError, match='service_secret_unavailable'):
        service_client.read_pending_summary(KEY, deadline=11.5, db_path=transport.database)
    assert transport.secret_reads == [True] and transport.paths == []
    assert transport.now == pytest.approx(10.2)
    assert transport.sock.closed and transport.owner_closed


def test_foreign_owner_is_refused_before_verified_secret_read(transport):
    """PR-AUTH-owner: management reads do not bypass socket ownership."""
    transport.owner_failure = True
    with pytest.raises(ListenerError, match='service_owner_mismatch'):
        service_client.read_pending_summary(KEY, deadline=11.5, db_path=transport.database)
    assert transport.secret_reads == transport.paths == []
    assert transport.sock.closed


@pytest.mark.parametrize('phase', ['proof', 'read'], ids=['PR-REDIRECT-proof', 'PR-REDIRECT-read'])
def test_actual_transport_refuses_redirect_without_retry(transport, phase):
    if phase == 'proof':
        transport.proof_status = 302
    else:
        transport.read_status = 302
    with pytest.raises(ListenerError, match='service_redirect_refused'):
        service_client.read_pending_summary(KEY, deadline=11.5, db_path=transport.database)
    assert len(transport.connections) == 1
    assert len(transport.paths) == (1 if phase == 'proof' else 2)
    assert transport.sock.closed


def test_launch_still_requires_runner_admission(transport):
    """PR-MGMT-launch: default launch connect remains refused when runner off."""
    assert service_client.connect(transport.database) == service_client.Unavailable('service_contained')
    assert transport.paths == ['/api/listener-proof']
    assert b'X-ModelQuad-Token' not in transport.headers[0]
    assert transport.sock.closed and transport.owner_closed


def test_management_connection_cannot_send_a_launch(transport):
    """PR-MGMT-read-only: skipping runner admission cannot grant launch."""
    connected = listener.authenticate(8765, db_path=transport.database,
                                      deadline=11.5, require_runner=False)
    with pytest.raises(ListenerError, match='service_contained') as caught:
        connected.request('POST', '/api/jobs/job-01/launch', body=b'{}')
    assert not caught.value.sent
    assert transport.paths == ['/api/listener-proof']
    assert transport.sock.closed and transport.owner_closed


@pytest.mark.parametrize('phase', ['auth', 'read'], ids=['PR-HOOK-auth-drip', 'PR-HOOK-read-drip'])
def test_hook_is_silent_on_actual_transport_timeout(monkeypatch, transport, phase, capsys):
    if phase == 'auth':
        transport.auth_byte = 0.01
    else:
        transport.read_byte = 0.01
    fetch = lambda key, **kw: service_client.read_pending_summary(key, db_path=transport.database, **kw)
    assert invoke(monkeypatch, fetch=fetch, clock=lambda: transport.now) == (0, '')
    assert transport.now <= 11.5 and transport.sock.closed
    assert capsys.readouterr() == ('', '')
