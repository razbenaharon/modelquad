# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline admission, identity, file privacy and ownership-transfer acceptance."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import pytest
from process_diagnostics import non_killing_service_spawn

from modelquad.jobs import handle, recovery
from modelquad.service import auth, db, history, jobs_api
from modelquad.service.app import create_app

native = pytest.mark.skipif(os.name != 'nt', reason='Windows file-handle/DACL acceptance')
TOKEN = {'X-ModelQuad-Token': 'test-install-secret'}


def prepared(db_path, attempt, directory):
    config = directory / 'config.yaml'
    config.write_text('{}', encoding='utf-8')
    return handle.PreparedDelegation(
        task_type='review', prompt='private prompt never echoed',
        working_directory=str(directory), provider='codex', agent_runtime='codex',
        statuses=[], session_id=None, model=None, reasoning_effort=None,
        consumption={}, verification=None, required_capabilities=None,
        action_authority=None, metadata=None, timeout=30, decision_id='test-decision',
        attempt_id=attempt, db_path=str(db_path), config_path=str(config),
        invocation_mode=None, user_approved=False,
        caller_host=None, host=None, mode='automatic', paused=False, mode_source=None,
        usage_directory=str(directory), host_snapshot={}, lineage={}, capsule=None,
        decision_mode='deterministic', reasoning_fell_back=False, plan_decision_id=None,
    )


def reservation(tmp_path, database=None):
    database = database or tmp_path / 'history.db'
    attempt = history.record_delegation_attempt(database, task_type='review', decision_id='test-decision')
    spec = handle.JobSpec((), str(tmp_path), str(database), attempt, 30,
                          sink='store', delegation=prepared(database, attempt, tmp_path))
    job_id = str(uuid.uuid4())
    history.reserve_job(database, job_id, attempt)
    return spec, job_id


@pytest.fixture
def service(tmp_path, monkeypatch):
    database = tmp_path / 'history.db'
    db.connect_and_init(database).close()
    history.put_job_runner_enabled(database, True)
    # No collector, provider calls, or asynchronous recovery touching fixtures.
    monkeypatch.setattr(recovery, 'schedule_recovery', lambda *a, **kw: False)
    monkeypatch.setattr(history, 'reconcile_stale_attempts', lambda *a, **kw: None)
    monkeypatch.setattr(handle, 'service_runner_verdict', lambda: (True, 'not_in_job'))
    app = create_app(db_path=database, poll_providers=False, write_secret=TOKEN['X-ModelQuad-Token'])
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client, database


def post(client, job_id, name, **kwargs):
    return client.post('/api/jobs/' + job_id + '/launch', json={'spec': name}, headers=TOKEN, **kwargs)


def assert_refusal(response, status, reason):
    assert response.status_code == status, response.text
    assert response.json()['reason'] == reason
    assert 'private prompt' not in response.text
    assert 'history.db' not in response.text


@pytest.mark.parametrize('headers', [{'Origin': 'http://127.0.0.1'}, {'Sec-Fetch-Site': 'same-origin'},
                                    {'Sec-Fetch-Mode': 'cors'}, {'Sec-Fetch-Dest': 'empty'}])
def test_browser_with_valid_token_precedes_every_route_check(service, headers):
    client, database = service
    history.put_job_runner_enabled(database, False)
    response = client.post('/api/jobs/not-a-uuid/launch', content='broken', headers=TOKEN | headers)
    assert_refusal(response, 403, 'browser_request_refused')


def test_missing_token_is_middleware_refusal(service):
    client, _ = service
    response = client.post('/api/jobs/bad/launch', json={})
    assert response.status_code == 403
    assert response.json()['code'] == 'service_token_missing'
    assert 'reason' not in response.json()


def test_disabled_precedes_containment_uuid_and_body(service, monkeypatch):
    client, database = service
    history.put_job_runner_enabled(database, False)
    monkeypatch.setattr(handle, 'service_runner_verdict', lambda: pytest.fail('must not probe'))
    assert_refusal(client.post('/api/jobs/bad/launch', content='broken', headers=TOKEN),
                   409, 'job_runner_disabled')


@pytest.mark.parametrize('raising', [False, True])
def test_containment_precedes_uuid_and_body(service, monkeypatch, raising):
    client, _ = service
    def probe():
        if raising:
            raise OSError('must not echo')
        return False, 'job_kill_on_close'
    monkeypatch.setattr(handle, 'service_runner_verdict', probe)
    response = client.post('/api/jobs/bad/launch', content='broken', headers=TOKEN)
    assert_refusal(response, 409, 'service_contained')
    assert response.json()['containment_reason'] == ('containment_unknown' if raising else 'job_kill_on_close')


# Fixed values: pytest-xdist workers must collect identical test ids.
@pytest.mark.parametrize('job_id', ['bad', 'b8b5c1de-a3f6-11f0-8de9-0242ac120002',
                                    'F3B2C8E4-5D6A-4B7C-9E8F-0A1B2C3D4E5F', 'f3b2c8e45d6a4b7c9e8f0a1b2c3d4e5f'])
def test_exact_uuid4_required_before_body(service, job_id):
    client, _ = service
    assert_refusal(client.post('/api/jobs/' + job_id + '/launch', content='broken', headers=TOKEN),
                   400, 'invalid_job_id')


@pytest.mark.parametrize('body', ['broken', '[]', 'null', '1', '"object required"', ' ' * 4097])
def test_bounded_object_body_precedes_spec(service, body):
    client, _ = service
    assert_refusal(client.post('/api/jobs/' + str(uuid.uuid4()) + '/launch', content=body, headers=TOKEN),
                   400, 'invalid_body')


@pytest.mark.parametrize('name', ['../job-abcdefgh.json', 'job-ABCdefgh.json', 'job-abc.json',
                                'job-abcdefgh.json/extra', 'job-abcdefgh.JSON', '/job-abcdefgh.json',
                                'job-abcdefgh.json\n', None])
def test_invalid_name_and_outside_directory_refused(service, name, monkeypatch):
    client, _ = service
    monkeypatch.setattr(jobs_api, '_read_private_spec', lambda p: pytest.fail('must not open'))
    assert_refusal(post(client, str(uuid.uuid4()), name), 400, 'invalid_spec')


@native
def test_real_private_spec_roundtrip_and_service_rewrite(service, tmp_path, monkeypatch):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    assert jobs_api.SPEC_NAME.fullmatch(caller.name)
    loaded, path = jobs_api._load_spec(database, caller.name)
    assert loaded == spec and loaded.argv == () and path == caller
    seen = []
    def launch(ref, service_path, validated, *, service_admission):
        assert service_admission is True
        assert not caller.exists()
        assert service_path != caller and service_path.exists()
        assert jobs_api._load_spec(database, service_path.name)[0] == spec
        row = history.read_job(database, job_id)
        assert row['runner_pid'] == os.getpid() and row['runner_created'] is not None
        assert not history.release_unlaunched_job(database, job_id)
        seen.append(service_path)
        assert history.update_job_startup(database, job_id, startup_seconds=handle.STARTUP_SECONDS,
                                          runner_pid=123456, runner_created=1.0)
        return handle.JobStart(ref, True, None)
    monkeypatch.setattr(handle, 'launch_reserved', launch)
    response = post(client, job_id, caller.name)
    assert response.json() == {'launched': True, 'job_id': job_id, 'limitation': None}
    assert history.read_job(database, job_id)['runner_pid'] == 123456
    seen[0].unlink()


@native
@pytest.mark.parametrize('change', ['argv', 'env', 'sink', 'db_path', 'attempt_id',
                                  'nested_db', 'nested_attempt', 'nested_timeout', 'nested_decision',
                                  'config_missing', 'config_directory', 'work_missing', 'work_file',
                                  'row_decision', 'attempt_decision', 'row_missing', 'row_old',
                                  'row_runner', 'row_heartbeat', 'row_recovery', 'row_status',
                                  'row_ended', 'attempt_ended'])
def test_crafted_spec_and_row_not_launchable(service, tmp_path, change):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    raw = asdict(spec)
    if change in ('argv', 'env', 'sink', 'db_path', 'attempt_id'):
        raw[change] = {'argv': ['cmd.exe'], 'env': {'EXTRA': 'bad'}, 'sink': 'none',
                       'db_path': str(tmp_path / 'other.db'),
                       'attempt_id': spec.attempt_id + 1}[change]
    elif change.startswith('nested_'):
        key = {'nested_db': 'db_path', 'nested_attempt': 'attempt_id', 'nested_timeout': 'timeout',
               'nested_decision': 'decision_id'}[change]
        raw['delegation'][key] = {'db_path': str(tmp_path / 'other.db'), 'attempt_id': spec.attempt_id + 1,
                                  'timeout': 31, 'decision_id': 'other'}[key]
    elif change.startswith('config_'):
        raw['delegation']['config_path'] = str(tmp_path if change == 'config_directory' else tmp_path / 'missing')
    elif change.startswith('work_'):
        raw['delegation']['working_directory'] = str(Path(spec.delegation.config_path) if change == 'work_file' else tmp_path / 'missing')
    else:
        conn = db.connect(database)
        try:
            if change == 'row_missing':
                conn.execute('DELETE FROM jobs WHERE job_id=?', (job_id,))
            elif change in ('row_decision', 'attempt_decision'):
                table = 'jobs' if change == 'row_decision' else 'delegation_attempts'
                conn.execute('UPDATE ' + table + " SET decision_id='other'")
            elif change == 'row_old':
                conn.execute('UPDATE jobs SET created_at=?', ((datetime.now(timezone.utc) - timedelta(seconds=handle.STARTUP_SECONDS - 4)).isoformat(),))
            elif change == 'attempt_ended':
                conn.execute("UPDATE delegation_attempts SET status='completed', resolved_at=?", (datetime.now(timezone.utc).isoformat(),))
            else:
                key, value = {'row_runner': ('runner_pid', 999), 'row_heartbeat': ('heartbeat_at', 'present'),
                              'row_recovery': ('recovery_claim', 'claimed'), 'row_status': ('status', 'running'),
                              'row_ended': ('ended_at', 'present')}[change]
                conn.execute('UPDATE jobs SET ' + key + '=?', (value,))
            conn.commit()
        finally:
            conn.close()
    caller = handle._write_spec(handle.JobSpec(**raw))
    assert_refusal(post(client, job_id, caller.name), 409, 'job_not_launchable')
    assert caller.exists()


@native
@pytest.mark.parametrize('defect', ['public_dacl', 'oversize', 'directory', 'file_reparse', 'parent_reparse', 'bad_json'])
def test_invalid_private_file(service, tmp_path, monkeypatch, defect):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    if defect == 'public_dacl':
        monkeypatch.setattr(auth, 'describe_handle', lambda h: auth.SecurityDescription('wrong', False, None))
    elif defect == 'oversize':
        with caller.open('r+b') as stream:
            stream.truncate(jobs_api.SPEC_MAX_BYTES + 1)
    elif defect == 'directory':
        caller.unlink()
        caller.mkdir()
    elif defect in ('file_reparse', 'parent_reparse'):
        original = Path.lstat
        target = caller if defect == 'file_reparse' else tmp_path
        class Reparse:
            st_file_attributes = 0x400
        monkeypatch.setattr(Path, 'lstat', lambda p: Reparse() if p == target else original(p))
    else:
        caller.write_text('not json', encoding='utf-8')
    assert_refusal(post(client, job_id, caller.name), 400, 'invalid_spec')
    assert history.read_job(database, job_id)['runner_pid'] is None


@native
def test_file_swap_between_check_and_open_is_refused(tmp_path, monkeypatch):
    spec, _ = reservation(tmp_path)
    caller = handle._write_spec(spec)
    replacement = handle._write_spec(spec)
    k32, _ = auth._win()
    original = k32.CreateFileW
    def swap(*args):
        caller.unlink()
        replacement.replace(caller)
        return original(*args)
    monkeypatch.setattr(k32, 'CreateFileW', swap)
    with pytest.raises(ValueError, match='invalid_spec'):
        jobs_api._read_private_spec(caller)


@native
def test_parent_junction_refused(tmp_path):
    spec, _ = reservation(tmp_path)
    caller = handle._write_spec(spec)
    from modelquad import procutil
    link = tmp_path / 'linked-parent'
    try:
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(tmp_path)],
                                capture_output=True, timeout=10, **procutil.no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f'junction creation unavailable: {type(error).__name__}')
    if result.returncode != 0:
        pytest.skip(f'junction creation unavailable: exit {result.returncode}')
    try:
        with pytest.raises(ValueError, match='invalid_spec'):
            jobs_api._read_private_spec(link / caller.name)
    finally:
        link.rmdir()


@pytest.mark.parametrize('winner', ['launch', 'release'])
def test_claim_and_release_are_mutually_exclusive(tmp_path, winner):
    spec, job_id = reservation(tmp_path)
    claim = lambda: history.claim_job_launch(spec.db_path, job_id, attempt_id=spec.attempt_id,
                                             decision_id='test-decision', starter_pid=123, starter_created=1.0)
    release = lambda: history.release_unlaunched_job(spec.db_path, job_id)
    assert (claim if winner == 'launch' else release)()
    assert not (release if winner == 'launch' else claim)()
    assert history.read_job_attempt(spec.db_path, spec.attempt_id)['status'] == 'started'


@pytest.mark.parametrize('_round', range(8))
def test_threaded_claim_release_race(tmp_path, _round):
    spec, job_id = reservation(tmp_path)
    barrier = threading.Barrier(2)
    def race(launch):
        barrier.wait(timeout=5)
        if launch:
            return history.claim_job_launch(spec.db_path, job_id, attempt_id=spec.attempt_id,
                                             decision_id='test-decision', starter_pid=123, starter_created=1.0)
        return history.release_unlaunched_job(spec.db_path, job_id)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(race, [True, False]))
    assert sum(results) == 1


def test_recovery_claim_blocks_launch_and_release_and_stale_snapshot_loses(tmp_path):
    spec, job_id = reservation(tmp_path)
    row = history.read_job(spec.db_path, job_id)
    assert history.claim_job_launch(spec.db_path, job_id, attempt_id=spec.attempt_id,
                                    decision_id='test-decision', starter_pid=123, starter_created=1.0)
    assert history.claim_job_recovery(spec.db_path, row, token='stale', now=datetime.now(timezone.utc), lease_seconds=60) is None
    spec2, job2 = reservation(tmp_path, Path(spec.db_path))
    row2 = history.read_job(spec.db_path, job2)
    assert history.claim_job_recovery(spec.db_path, row2, token='recovery', now=datetime.now(timezone.utc), lease_seconds=60)
    assert not history.claim_job_launch(spec.db_path, job2, attempt_id=spec2.attempt_id,
                                        decision_id='test-decision', starter_pid=123, starter_created=1.0)
    assert not history.release_unlaunched_job(spec.db_path, job2)


@native
@pytest.mark.parametrize('step', ['rewrite', 'delete', 'launch', 'cancel_rewrite', 'cancel_delete', 'cancel_launch'])
def test_every_failure_after_claim_closes_both_records(service, tmp_path, monkeypatch, step):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    copies = []
    error = KeyboardInterrupt if step.startswith('cancel') else OSError
    original = handle._write_spec
    def write(validated, **kwargs):
        if step.endswith('rewrite'):
            raise error('private failure')
        copy = original(validated, **kwargs)
        copies.append(copy)
        return copy
    unlink = Path.unlink
    def delete(path, *args, **kw):
        if path == caller and step.endswith('delete'):
            raise error('private failure')
        return unlink(path, *args, **kw)
    def launch(*a, **kw):
        raise error('private failure')
    monkeypatch.setattr(handle, '_write_spec', write)
    monkeypatch.setattr(Path, 'unlink', delete)
    monkeypatch.setattr(handle, 'launch_reserved', launch)
    # Call the synchronous owner directly for BaseException cancellation:
    # propagating KeyboardInterrupt through TestClient stops its portal.
    if step.startswith('cancel'):
        from starlette.requests import Request
        request = Request({'type': 'http', 'app': client.app})
        with pytest.raises(KeyboardInterrupt):
            jobs_api._launch(request, job_id, {'spec': caller.name})
    else:
        assert_refusal(post(client, job_id, caller.name), 500, 'runner_launch_failed')
    row = history.read_job(database, job_id)
    attempt = history.read_job_attempt(database, spec.attempt_id)
    assert row['status'] == 'failed' and row['ended_at']
    assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'failed_to_start'
    assert not history.release_unlaunched_job(database, job_id)
    assert all(not copy.exists() for copy in copies)


@native
def test_launch_failure_result_is_returned_without_fallback(service, tmp_path, monkeypatch):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    def failure(ref, service_path, validated, *, service_admission):
        assert service_admission is True
        service_path.unlink()
        history.fail_job_attempt(database, ref.job_id, detail='launch_failed')
        return handle.JobStart(ref, False, 'breakaway_not_effective')
    monkeypatch.setattr(handle, 'launch_reserved', failure)
    response = post(client, job_id, caller.name)
    assert_refusal(response, 500, 'runner_launch_failed')
    assert response.json()['limitation'] == 'breakaway_not_effective'
    assert not history.release_unlaunched_job(database, job_id)


@native
@pytest.mark.parametrize('other_count, expected', [(7, 200), (8, 429)])
def test_open_jobs_excludes_current_reservation(service, tmp_path, monkeypatch, other_count, expected):
    client, database = service
    # The other reservations come first so the launched one is fresh: under
    # load the route refuses a reservation older than STARTUP_SECONDS - 5 as
    # job_not_launchable (train 17 preflight: 409 instead of 429).
    for _ in range(other_count):
        reservation(tmp_path, database)
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    def launch(ref, path, spec, *, service_admission):
        assert service_admission is True
        path.unlink()
        history.fail_job_attempt(database, ref.job_id, detail='launch_failed')
        return handle.JobStart(ref, True, None)
    monkeypatch.setattr(handle, 'launch_reserved', launch)
    response = post(client, job_id, caller.name)
    assert response.status_code == expected
    if expected == 429:
        assert_refusal(response, 429, 'launch_rate_limited')
        assert history.release_unlaunched_job(database, job_id)


@native
def test_concurrent_distinct_jobs_admission_has_one_remaining_slot(service, tmp_path, monkeypatch):
    client, database = service
    pairs = []
    for _ in range(2):
        spec, job_id = reservation(tmp_path, database)
        pairs.append((job_id, handle._write_spec(spec).name))
    from starlette.requests import Request
    request = Request({'type': 'http', 'app': client.app})
    admission = jobs_api._admission(request)
    admission.launches.extend([jobs_api.time.monotonic()] * 5)
    barrier = threading.Barrier(2)
    def launch(ref, path, spec, *, service_admission):
        assert service_admission is True
        path.unlink()
        history.fail_job_attempt(database, ref.job_id, detail='launch_failed')
        return handle.JobStart(ref, True, None)
    monkeypatch.setattr(handle, 'launch_reserved', launch)
    def run(pair):
        barrier.wait(timeout=5)
        return post(client, *pair)
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(run, pairs))
    assert sorted(r.status_code for r in responses) == [200, 429]
    assert len(admission.launches) == 6
    assert sum(history.release_unlaunched_job(database, job_id) for job_id, _ in pairs) == 1
    # Expired entries restore admission without counting terminal jobs.
    admission.launches.clear()
    admission.launches.extend([jobs_api.time.monotonic() - 11] * 6)
    spec, job_id = reservation(tmp_path, database)
    assert post(client, job_id, handle._write_spec(spec).name).status_code == 200
    assert len(admission.launches) == 1


def test_not_launchable_precedes_rate_limit(service, tmp_path, monkeypatch):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    from starlette.requests import Request
    admission = jobs_api._admission(Request({'type': 'http', 'app': client.app}))
    admission.launches.extend([jobs_api.time.monotonic()] * 6)
    monkeypatch.setattr(jobs_api, '_load_spec', lambda *a: (replace(spec, argv=('cmd.exe',)), tmp_path / 'fake'))
    assert_refusal(post(client, job_id, 'job-abcdefgh.json'), 409, 'job_not_launchable')


@pytest.mark.parametrize('raising', [False, True])
def test_health_has_verdict_and_survives_probe_failure(service, monkeypatch, raising):
    client, _ = service
    def probe():
        if raising:
            raise RuntimeError('not returned')
        return False, 'job_kill_on_close'
    monkeypatch.setattr(handle, 'service_runner_verdict', probe)
    response = client.get('/api/health', headers=TOKEN)
    assert response.status_code == 200
    assert response.json()['detached_runner'] == {'ok': False, 'reason': 'containment_unknown' if raising else 'job_kill_on_close'}


@native
@pytest.mark.parametrize('failure', ['claim_lost', 'identity_unknown'])
def test_before_claim_failure_keeps_fallback_ownership(service, tmp_path, monkeypatch, failure):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    if failure == 'claim_lost':
        monkeypatch.setattr(history, 'claim_job_launch', lambda *a, **kw: False)
    else:
        from modelquad import procutil
        monkeypatch.setattr(procutil, 'process_creation_epoch', lambda pid: None)
    assert_refusal(post(client, job_id, caller.name), 409, 'job_not_launchable')
    assert history.release_unlaunched_job(database, job_id)
    assert history.read_job_attempt(database, spec.attempt_id)['status'] == 'started'
    assert caller.exists()


@pytest.mark.parametrize('field,value', [('attempt_id', 999), ('decision_id', 'other')])
def test_launch_claim_checks_both_identities(tmp_path, field, value):
    spec, job_id = reservation(tmp_path)
    arguments = dict(attempt_id=spec.attempt_id, decision_id='test-decision', starter_pid=123, starter_created=1.0)
    arguments[field] = value
    assert not history.claim_job_launch(spec.db_path, job_id, **arguments)
    assert history.release_unlaunched_job(spec.db_path, job_id)


def test_service_claim_with_null_decision_uses_is_predicate(tmp_path):
    database = tmp_path / 'history.db'
    attempt = history.record_delegation_attempt(database, task_type='review')
    job_id = str(uuid.uuid4())
    history.reserve_job(database, job_id, attempt)
    assert history.claim_job_launch(database, job_id, attempt_id=attempt, decision_id=None,
                                    starter_pid=123, starter_created=1.0)
    assert not history.release_unlaunched_job(database, job_id)


def test_service_starter_deadline_still_closes_both_records(tmp_path):
    spec, job_id = reservation(tmp_path)
    assert history.claim_job_launch(spec.db_path, job_id, attempt_id=spec.attempt_id,
                                    decision_id='test-decision', starter_pid=123, starter_created=1.0)
    conn = db.connect(spec.db_path)
    try:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=handle.STARTUP_SECONDS + 1)).isoformat()
        conn.execute('UPDATE jobs SET created_at=? WHERE job_id=?', (stamp, job_id))
        conn.commit()
    finally:
        conn.close()
    assert history.update_job_startup(spec.db_path, job_id, startup_seconds=handle.STARTUP_SECONDS)
    row = history.read_job(spec.db_path, job_id)
    attempt = history.read_job_attempt(spec.db_path, spec.attempt_id)
    assert row['status'] == 'failed' and row['ended_at']
    assert attempt['failure_kind'] == 'failed_to_start' and attempt['resolved_at']
    assert not history.update_job_startup(spec.db_path, job_id, startup_seconds=handle.STARTUP_SECONDS,
                                          runner_pid=456, runner_created=2.0)


def test_dead_service_starter_is_recovered_without_new_sweep(tmp_path):
    from modelquad.jobs.runner import _NoOutputSink
    spec, job_id = reservation(tmp_path)
    assert history.claim_job_launch(spec.db_path, job_id, attempt_id=spec.attempt_id,
                                    decision_id='test-decision', starter_pid=123, starter_created=1.0)
    class Dead:
        def pid_state(self, pid):
            assert pid == 123
            return 'dead'
    result = recovery.recover_jobs(spec.db_path, now=datetime.now(timezone.utc), sink=_NoOutputSink(), probe=Dead())
    assert result['restored'] == 1
    assert history.read_job(spec.db_path, job_id)['status'] == 'interrupted'
    assert history.read_job_attempt(spec.db_path, spec.attempt_id)['failure_kind'] == 'orphaned'


@native
def test_one_handle_read_verifies_that_handle_and_prevents_swap(tmp_path, monkeypatch):
    spec, _ = reservation(tmp_path)
    caller = handle._write_spec(spec)
    k32, _ = auth._win()
    original_open = k32.CreateFileW
    original_describe = auth.describe_handle
    opened = []
    def create(*args):
        native_handle = original_open(*args)
        opened.append(native_handle)
        return native_handle
    def describe(native_handle):
        assert opened == [native_handle]
        with pytest.raises(PermissionError):
            caller.unlink()
        return original_describe(native_handle)
    monkeypatch.setattr(k32, 'CreateFileW', create)
    monkeypatch.setattr(auth, 'describe_handle', describe)
    monkeypatch.setattr(auth, 'describe_path', lambda *a: pytest.fail('verify the open handle'))
    assert handle.JobSpec(**json.loads(jobs_api._read_private_spec(caller))) == spec
    assert len(opened) == 1
    caller.unlink()  # The read closed its handle.


@native
def test_service_spec_cleanup_logs_only_fixed_label_and_job_id(service, tmp_path, monkeypatch, caplog):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    stale = tmp_path / 'job-private-filename-canary.json'
    stale.write_text('private-stale-prompt-canary', encoding='utf-8')
    import time
    os.utime(stale, (time.time() - 601, time.time() - 601))
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == stale:
            raise PermissionError('private-error-canary')
        return original(path, *args, **kwargs)
    def launch(ref, path, spec, *, service_admission):
        assert service_admission is True
        path.unlink()
        history.fail_job_attempt(database, ref.job_id, detail='launch_failed')
        return handle.JobStart(ref, False, 'runner_launch_failed')
    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(handle, 'launch_reserved', launch)
    assert_refusal(post(client, job_id, caller.name), 500, 'runner_launch_failed')
    assert 'stale_spec_cleanup_unavailable ' + job_id in caplog.text
    for marker in (str(tmp_path), stale.name, caller.name, spec.delegation.prompt,
                   'private-stale-prompt-canary', 'private-error-canary'):
        assert marker not in caplog.text


@native
@pytest.mark.parametrize('step', ['count', 'identity', 'claim'])
def test_pre_claim_exception_is_unavailable_and_releasable(service, tmp_path, monkeypatch, step):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    caller = handle._write_spec(spec)
    def fail(*args, **kwargs):
        raise OSError('private failure never echoed')
    if step == 'count':
        monkeypatch.setattr(jobs_api, 'connect_and_init', fail)
    elif step == 'identity':
        monkeypatch.setattr(jobs_api.procutil, 'process_creation_epoch', fail)
    else:
        monkeypatch.setattr(history, 'claim_job_launch', fail)
    response = post(client, job_id, caller.name)
    assert_refusal(response, 503, 'launch_unavailable')
    row = history.read_job(database, job_id)
    assert row['runner_pid'] is None and row['ended_at'] is None
    assert history.release_unlaunched_job(database, job_id)
    assert history.read_job_attempt(database, spec.attempt_id)['status'] == 'started'
    assert caller.exists()


def test_setting_and_verdict_run_off_event_loop_in_order(service, monkeypatch):
    import asyncio
    client, _ = service
    events = []
    def off_loop(label, result):
        with pytest.raises(RuntimeError, match='no running event loop'):
            asyncio.get_running_loop()
        events.append(label)
        return result
    monkeypatch.setattr(history, 'get_job_runner_enabled', lambda db: off_loop('setting', True))
    monkeypatch.setattr(handle, 'service_runner_verdict', lambda: off_loop('verdict', (True, 'not_in_job')))
    assert_refusal(client.post('/api/jobs/bad/launch', content='broken', headers=TOKEN),
                   400, 'invalid_job_id')
    assert events == ['setting', 'verdict']


@native
def test_service_rewrite_uses_own_database_spelling(service, tmp_path, monkeypatch):
    client, database = service
    spec, job_id = reservation(tmp_path, database)
    alternate = str(database.parent / '..' / database.parent.name / database.name)
    spec = replace(spec, db_path=alternate, delegation=replace(spec.delegation, db_path=alternate))
    caller = handle._write_spec(spec)
    write = handle._write_spec
    def service_write(validated, **kwargs):
        assert validated.db_path == str(database)
        assert validated.delegation.db_path == str(database)
        return write(validated, **kwargs)
    def launch(ref, path, validated, *, service_admission):
        assert service_admission is True
        assert jobs_api._load_spec(database, path.name)[0] == validated
        path.unlink()
        history.fail_job_attempt(database, ref.job_id, detail='launch_failed')
        return handle.JobStart(ref, False, 'runner_launch_failed')
    monkeypatch.setattr(handle, '_write_spec', service_write)
    monkeypatch.setattr(handle, 'launch_reserved', launch)
    assert_refusal(post(client, job_id, caller.name), 500, 'runner_launch_failed')


@native
def test_real_route_reserved_spec_finishes_with_offline_output(tmp_path, monkeypatch,
                                                             non_killing_service_spawn):
    from modelquad.jobs.store import default_store
    from modelquad.lineage import DecisionLineage
    from modelquad.models import ExpectedConsumption
    from test_jobs_runner import cleanup, product_import_paths, wait_for

    # Load headroom: the service admits a reserved job only within
    # STARTUP_SECONDS - 5 of its creation; a loaded full suite exceeded 10 s.
    monkeypatch.setattr(handle, 'STARTUP_SECONDS', 120)
    in_job = handle._caller_in_job()
    flags = handle._caller_job_limits() if in_job else 0
    evidence = f'test service harness: in_job={in_job}, flags=0x{flags:x}'
    # Simulate a non-killing service; assign its real suspended child to a
    # test-owned non-killing innermost job. The child measures itself, and
    # launch fencing, identity-before-resume and offline execution stay real.
    monkeypatch.setattr(handle, '_caller_in_job', lambda: True)
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: 0)
    # Scope the job assignment to handle's runner spawn, leaving unrelated
    # diagnostic/cleanup subprocesses with their own ordinary lifecycle.
    monkeypatch.setattr(handle, 'subprocess', SimpleNamespace(
        **{**vars(subprocess), 'Popen': non_killing_service_spawn}))
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join([str(Path(__file__).parent.resolve()),
                                                   *product_import_paths()]))
    monkeypatch.setenv('MODELQUAD_TEST_EXECUTOR_PLUGIN', 'fixtures.fake_executor_plugin:registry')
    monkeypatch.setenv('MODELQUAD_TEST_EXECUTOR_OK', '1')
    monkeypatch.setenv('MODELQUAD_STATE_ROOT', str(tmp_path / 'state'))
    from process_diagnostics import instrument_runner_command
    original_command = handle._runner_command
    # The isolated venv entry intentionally ignores PYTHONPATH. Restore test
    # plugin imports only in the diagnostic driver, after the same owning
    # installation is verified; production entry remains isolated.
    monkeypatch.setattr(handle, '_runner_command', lambda *args: instrument_runner_command(
        original_command(*args), tmp_path / 'runner-stages.jsonl'))
    # Startup recovery runs asynchronously and may still be scanning when the
    # reservation below is created: it claims a fresh runnerless row and
    # releases it, and in that window the launch is refused as
    # job_not_launchable (train 16 hosted 1/2). This test covers the route,
    # not recovery, so recovery stays off as in the service fixture.
    monkeypatch.setattr(recovery, 'schedule_recovery', lambda *a, **kw: False)
    monkeypatch.setenv('MODELQUAD_STATE_MODE', 'dev')
    database = tmp_path / 'history.db'
    history.put_job_runner_enabled(database, True)
    app = create_app(db_path=database, poll_providers=False, write_secret=TOKEN['X-ModelQuad-Token'])
    reserved = None
    spec = None
    try:
        # Startup reconciliation sees no pending attempt; create it after startup.
        with TestClient(app, base_url='http://127.0.0.1') as client:
            attempt = history.record_delegation_attempt(database, task_type='review',
                                                        decision_id='test-decision')
            from fixtures.fake_executor_plugin import FakeUsage
            delegation = replace(prepared(database, attempt, tmp_path),
                                 statuses=[FakeUsage().get_status().to_dict()],
                                 consumption=ExpectedConsumption('low', source='host_supplied').to_dict(),
                                 lineage=asdict(DecisionLineage('test-decision', 'minted', False)))
            spec = handle.JobSpec((), str(tmp_path), str(database), attempt, delegation.timeout,
                                  sink='store', provider='codex', delegation=delegation)
            reserved = handle.reserve_and_write(spec, probe_caller=False)
            assert isinstance(reserved, handle.Reserved), (evidence, reserved)
            response = post(client, reserved.ref.job_id, reserved.spec_path.name)
            value = response.json()
            assert response.status_code == 200, (evidence, value)
            assert value['launched'] is True
            assert value['limitation'] == handle.CONTAINED_RUNNER_LIMITATION
            assert not reserved.spec_path.exists()
            final = wait_for(lambda: handle.job_status(reserved.ref.job_id, db_path=database),
                             lambda row: row['ended_at'] is not None)
            assert final['status'] == 'finished', (evidence, final)
            assert final['launch_limitation'] == handle.CONTAINED_RUNNER_LIMITATION
            output = default_store().collect(reserved.ref.job_id, datetime.now(timezone.utc))
            assert output is not None
            assert output.output.output_text == 'synthetic extracted answer'
            assert history.read_job_attempt(database, attempt)['status'] == 'completed'
            assert not list(tmp_path.glob('job-*.json'))
    finally:
        if isinstance(reserved, handle.Reserved):
            row = history.read_job(database, reserved.ref.job_id)
            # A refused launch retains the service's starter identity. Never
            # feed this pytest process to the runner-termination test helper.
            if row and row['runner_pid'] != os.getpid():
                cleanup(spec, reserved.ref)


@native
@pytest.mark.parametrize('owner,extra_ace,accepted', [
    # An elevated run (hosted CI, an elevated user) owns its files as
    # BUILTIN\Administrators: the private DACL decides, not the owner.
    ('S-1-5-32-544', None, True),
    ('S-1-5-32-544', 'S-1-1-0', False),  # Everyone granted: refused.
    # Another ordinary account as owner could grant itself access: refused.
    ('S-1-5-21-1111111111-2222222222-3333333333-1001', None, False),
])
def test_spec_privacy_judges_the_dacl_not_the_owner(tmp_path, monkeypatch, owner, extra_ace, accepted):
    spec, _ = reservation(tmp_path)
    caller = handle._write_spec(spec)
    original_describe = auth.describe_handle

    def describe(native_handle):
        real = original_describe(native_handle)
        aces = list(real.aces) + ([(0, 0, extra_ace)] if extra_ace else [])
        return auth.SecurityDescription(owner, real.protected, aces)

    monkeypatch.setattr(auth, 'describe_handle', describe)
    if accepted:
        assert handle.JobSpec(**json.loads(jobs_api._read_private_spec(caller))) == spec
    else:
        with pytest.raises(ValueError, match='invalid_spec'):
            jobs_api._read_private_spec(caller)
    caller.unlink()
