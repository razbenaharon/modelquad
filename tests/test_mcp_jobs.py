# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Bounded synthetic acceptance for the opt-in Host job path."""

import asyncio
import importlib
import json
import os
import sqlite3
import sys
import time
import threading
import subprocess
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import tool_payload
from process_diagnostics import non_killing_runner_spawn, runner_driver_command
from modelquad import paths, procutil, registry
from modelquad.execution import bound_launch
from modelquad.jobs import handle, runner, service_client
from modelquad.jobs.handle import PreparedDelegation
from modelquad.jobs.store import FileOutputStore
from modelquad.mcp import jobs_tools, server as mcp
from modelquad.service import db, history
from fixtures.fake_executor_plugin import CHILD, SLOW_PROMPT, TREE_PROMPT
from test_adapter_file_security import acl_tree, native, user


TOOLS = {'modelquad_job_status', 'modelquad_job_collect', 'modelquad_job_wait', 'modelquad_job_acknowledge',
         'modelquad_job_stop', 'modelquad_job_list_pending'}
_PROJECT_DIRECTORY = None


@pytest.mark.parametrize('prompt,spawn_count,sleep_count', [
    ('Working-tree notice: review the tree', 0, 0),
    ('tree\nslow\nWorking-tree', 0, 0),
    ('ordinary\n' + TREE_PROMPT, 0, 0),
    (TREE_PROMPT + '\nWorking-tree', 1, 1),
    (SLOW_PROMPT + '\nWorking-tree', 0, 1),
])
def test_synthetic_scenarios_require_exact_original_request_marker(monkeypatch, capsys,
                                                                  prompt, spawn_count, sleep_count):
    from io import StringIO
    from unittest.mock import Mock
    spawn, sleep = Mock(), Mock()
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(time, 'sleep', sleep)
    monkeypatch.setattr(sys, 'stdin', StringIO(prompt))
    exec(CHILD, {})  # The script runs in-process, with spawning and waiting mocked.
    assert spawn.call_count == spawn_count and sleep.call_count == sleep_count
    assert 'synthetic extracted answer' in capsys.readouterr().out


def call(name, **args):
    if name in TOOLS and 'project_directory' not in args and _PROJECT_DIRECTORY is not None:
        args['project_directory'] = _PROJECT_DIRECTORY
    return tool_payload(asyncio.run(mcp.server.call_tool(name, args)))


def record_attempt(database, **kwargs):
    """An attempt delegated into this test's project, so job tools may read it."""
    return history.record_delegation_attempt(
        database, working_directory=_PROJECT_DIRECTORY, **kwargs)


def summary_service_absent(*args, **kwargs):
    raise service_client.ListenerError('service_absent')


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    global _PROJECT_DIRECTORY
    _PROJECT_DIRECTORY = str(tmp_path)
    database = tmp_path / 'history.db'
    db.connect_and_init(database).close()
    monkeypatch.setattr(db, 'DEFAULT_DB_PATH', database)
    tests = Path(__file__).parent.resolve()
    monkeypatch.syspath_prepend(str(tests))
    # Preserve the caller's import mode: installed-mode children must use the
    # wheel, while checkout runs already supply src in their PYTHONPATH.
    child_path = [str(tests)]
    if os.environ.get('PYTHONPATH'):
        child_path.append(os.environ['PYTHONPATH'])
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(child_path))
    monkeypatch.setenv('MODELQUAD_TEST_EXECUTOR_PLUGIN', 'fixtures.fake_executor_plugin:registry')
    monkeypatch.setenv('MODELQUAD_TEST_EXECUTOR_OK', '1')
    # default_store is rebuilt from the same explicit isolated state root in
    # the Host and detached runner; no real user configuration is read/written.
    monkeypatch.setenv('MODELQUAD_STATE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setenv('MODELQUAD_STATE_MODE', 'dev')
    # The product's own inline wait: a loaded machine can take more than a few
    # seconds to finish the threaded runner, and a passing call returns as soon
    # as it is done. Tests that need "starting" set this to 0 themselves.
    monkeypatch.setattr(mcp, 'JOB_WAIT_SECONDS', 45.0)
    history.put_job_runner_enabled(database, True)
    # The simulated service runner is a separate logical process. Caller
    # containment mocks must not become that runner's observations merely
    # because this launch seam uses a thread in the same Python process.
    monkeypatch.setattr(runner, 'runner_containment_verdict', lambda: (True, 'not_in_job'))
    # The native starter correctly refuses this constrained session's nested
    # containment. Simulate only the launch seam; run the real runner, manager,
    # synthetic executor and store. This is NOT detached survival acceptance.
    class SimulatedService:
        def launch(self, job_id, spec_name):
            path = database.parent / spec_name
            thread = threading.Thread(target=runner.run_job, args=(job_id, path),
                                      kwargs={'db_path': database}, daemon=True)
            thread.start()
            return 'launched', 'launched'
    monkeypatch.setattr(service_client, 'connect', lambda database: SimulatedService())
    # Scoped pending listings never reach a real service from a test.
    monkeypatch.setattr(service_client, 'read_pending_summary', summary_service_absent)
    monkeypatch.setattr(mcp, 'start_job', lambda *a: pytest.fail('MCP started a runner'))
    monkeypatch.setattr(handle, 'launch_reserved', lambda *a, **kw: pytest.fail('MCP launched a runner'))
    yield database
    conn = sqlite3.connect(database)
    rows = conn.execute('SELECT job_id FROM jobs WHERE ended_at IS NULL').fetchall()
    conn.close()
    for (job_id,) in rows:
        handle.stop_job(job_id, db_path=database)
    deadline = time.monotonic() + 8
    while rows and time.monotonic() < deadline:
        rows = [(job_id,) for (job_id,) in rows if history.read_job(database, job_id)['ended_at'] is None]
        time.sleep(.05)
    _PROJECT_DIRECTORY = None


# Coordination needs slack on the shared, memory-constrained runner.
LOAD_HEADROOM_S = 60


#: What a delegate reply adds to the job payload: the files it sends and the
#: Codex account kind (tests/test_attached_file_rules.py). Collect omits them.
DELEGATION_KEYS = frozenset({'codex_account', 'sent_files', 'skipped_files', 'sent_files_stage',
                             'suggested_files', 'suggested_files_note'})


def job_payload(response):
    assert response.get('codex_account', '').startswith('Codex account: '), response
    return {key: value for key, value in response.items() if key not in DELEGATION_KEYS}


def delegate(tmp_path, prompt='quick', **kw):
    return job_payload(call('modelquad_delegate', task_type='review', prompt=prompt,
                            working_directory=str(tmp_path), timeout=60, **kw))  # preflight load headroom


def report_shortcut_response(response):
    """Capture response labels for loaded-run diagnosis, without answer content."""
    facts = {key: response[key] for key in (
        'delegated', 'executed', 'refused', 'reason', 'error', 'status',
        'job_status', 'output_state', 'poll_with',
    ) if key in response}
    facts['has_output'] = 'output' in response
    print('synthetic delegation shortcut: ' + json.dumps(facts, sort_keys=True))


def terminal(database, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = handle.job_status(job_id, db_path=database)
        if row['ended_at'] is not None:
            return row
        time.sleep(.05)
    pytest.fail('synthetic job did not finish within the bounded wait')


def test_setting_defaults_off_and_fails_closed(tmp_path):
    database = tmp_path / 'history.db'
    assert history.get_job_runner_enabled(database) is False
    history.put_job_runner_enabled(database, True)
    assert history.get_job_runner_enabled(database) is True
    with pytest.raises(ValueError):
        history.put_job_runner_enabled(database, 1)
    conn = sqlite3.connect(database)
    for value in ('1', '"true"', 'broken'):
        conn.execute("UPDATE settings SET value=? WHERE key='job_runner_enabled'", (value,))
        conn.commit()
        assert history.get_job_runner_enabled(database) is False
    conn.close()
    assert history.get_job_runner_enabled(tmp_path) is False


def test_quick_collect_ack_and_restart(synthetic, tmp_path, monkeypatch):
    response = delegate(tmp_path)
    assert response.get('job_status') == 'finished', response
    assert response['worker_claim'] == 'completed'
    assert response['output'] == 'synthetic extracted answer'
    assert {'job_id', 'receipt', 'job_status', 'worker_claim', 'output', 'changed_files',
            'partial', 'expires_at', 'expiring_soon', 'tree_confirmed_dead',
            'descendant_uncertainty', 'output_truncated', 'original_bytes',
            'host_diagnostics_status'} == set(response)
    # This fixture simulates launch without an authenticated diagnostic service.
    assert response['host_diagnostics_status'] == 'unavailable'
    assert not {'host_diagnostics', 'host_diagnostics_stage'} & response.keys()
    assert response['output_truncated'] is False
    assert response['original_bytes'] == len(response['output'].encode('utf-8'))
    assert not {'success', 'exit_code', 'metadata'} & response.keys()
    job_id = response['job_id']
    assert call('modelquad_job_collect', job_id=job_id) == response
    assert call('modelquad_job_collect', job_id=job_id) == response
    # Re-register into a new server (new Host connection), same DB/store.
    fresh = mcp.WorkerGuardMCPServer('restarted-host')
    jobs_tools.register(fresh)
    pending = tool_payload(asyncio.run(fresh.call_tool(
        'modelquad_job_list_pending', {'project_directory': str(tmp_path)})))
    assert pending['jobs'][0]['job_id'] == job_id
    assert 'synthetic extracted answer' not in json.dumps(pending)
    assert call('modelquad_job_acknowledge', job_id=job_id, receipt='wrong')['acknowledged'] is False
    assert call('modelquad_job_acknowledge', job_id=job_id, receipt=response['receipt'])['acknowledged']
    assert 'output' not in call('modelquad_job_collect', job_id=job_id)
    assert call('modelquad_job_list_pending')['jobs'] == []
    contents = synthetic.read_bytes()
    for marker in ('synthetic extracted answer', 'private-command-canary', 'private-tool-output-canary'):
        assert marker.encode() not in contents
    assert not list(tmp_path.glob('job-*.json'))


@pytest.mark.parametrize('task_type', ['implementation', 'review'])
@pytest.mark.parametrize('state', ['pending', 'acknowledged', 'expired', 'not_found', 'refused'])
def test_ordinary_collection_equals_pre_overlay_contract(pending_listing, monkeypatch, task_type, state):
    from modelquad.execution.base import WorkerTaskStatus
    from modelquad.jobs.seam import JobOutput, JobRef, JobStatus

    database, store, _ = pending_listing
    now = datetime(2030, 1, 2, tzinfo=timezone.utc)
    clock = {'now': now}
    monkeypatch.setattr(jobs_tools, 'datetime', type('Clock', (), {'now': staticmethod(lambda tz: clock['now'])}))
    job_id = 'ordinary-collection'
    if state != 'not_found':
        attempt_id = record_attempt(database, task_type=task_type,
            repo_key='a' * 32, policy_version=1, capability='delegate')
        row = history.reserve_job(database, job_id, attempt_id)
        history.resolve_delegation_attempt(database, attempt_id,
            status='refused' if state == 'refused' else 'completed',
            failure_kind='policy_refused' if state == 'refused' else None,
            detail='repo_paused' if state == 'refused' else None, only_if_open=True)
        history.update_job(database, job_id, status='failed' if state == 'refused' else 'finished',
                           ended_at=now.isoformat())
        if state != 'refused':
            store.put(JobOutput(JobRef(job_id, attempt_id, row['decision_id']), JobStatus.FINISHED,
                WorkerTaskStatus.COMPLETED, None, 'byte-identical\nsynthetic answer', False,
                ('src/one.py',), None, None, now, now), now)
            if state == 'acknowledged':
                receipt = store.collect(job_id, now).receipt
                assert store.acknowledge(job_id, receipt, now)
            elif state == 'expired':
                clock['now'] = now + timedelta(days=8)
    def legacy(record, **kwargs):
        if record is None:
            return {}
        attempt = history.read_job_attempt(database, record['attempt_id'])
        return {'refused': True, 'reason': attempt['detail']} if attempt['status'] == 'refused' else {}
    original = jobs_tools._job_refused
    monkeypatch.setattr(jobs_tools, '_job_refused', legacy)
    before = call('modelquad_job_collect', job_id=job_id)
    before_wait = call('modelquad_job_wait', job_id=job_id)
    monkeypatch.setattr(jobs_tools, '_job_refused', original)
    assert call('modelquad_job_collect', job_id=job_id) == before
    assert call('modelquad_job_wait', job_id=job_id) == before_wait
    assert not {'review', 'catalog_status', 'result_available'} & before.keys()


def test_ordinary_review_delegation_terminal_shortcut_has_no_overlay(synthetic, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_tools, 'schedule_recovery', lambda *a, **kw: None)
    response = job_payload(call('modelquad_delegate', task_type='review', prompt='quick',
                                working_directory=str(tmp_path), timeout=60))
    report_shortcut_response(response)
    assert response['output'] == 'synthetic extracted answer'
    assert call('modelquad_job_collect', job_id=response['job_id']) == response
    assert not {'review', 'catalog_status', 'result_available'} & response.keys()


@pytest.fixture
def pending_listing(tmp_path, monkeypatch):
    global _PROJECT_DIRECTORY
    # Job tools serve only jobs delegated from the caller's own project.
    _PROJECT_DIRECTORY = str(tmp_path)
    database = tmp_path / 'listing.db'
    db.connect_and_init(database).close()
    store = FileOutputStore(tmp_path / 'answers')
    scheduled = []
    monkeypatch.setattr(db, 'DEFAULT_DB_PATH', database)
    monkeypatch.setattr(jobs_tools, 'default_store', lambda: store)
    monkeypatch.setattr(jobs_tools, 'schedule_recovery',
                        lambda path, **kwargs: scheduled.append((path, kwargs)))
    monkeypatch.setattr(service_client, 'read_pending_summary', summary_service_absent)
    yield database, store, scheduled
    _PROJECT_DIRECTORY = None


@pytest.fixture
def wait_clock(monkeypatch):
    class Clock:
        now = 100.0
        sleeps = []
        after_sleep = None

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            assert 0 < seconds <= 0.5
            self.sleeps.append(seconds)
            self.now += seconds
            if self.after_sleep is not None:
                self.after_sleep()

    clock = Clock()
    monkeypatch.setattr(jobs_tools, 'time', clock)
    return clock


def test_delegate_inline_wait_budget():
    assert mcp.JOB_WAIT_SECONDS == 45


@pytest.mark.parametrize('status,ended', [
    ('finished', True), ('failed', False), ('unknown', False), ('running', True),
])
def test_wait_terminal_returns_exact_collect_contract(pending_listing, wait_clock, status, ended):
    from modelquad.execution.base import WorkerTaskStatus
    from modelquad.jobs.seam import JobOutput, JobRef, JobStatus

    database, store, scheduled = pending_listing
    attempt = record_attempt(database, task_type='review')
    row = history.reserve_job(database, 'wait-terminal', attempt)
    now = datetime.now(timezone.utc)
    store.put(JobOutput(JobRef(row['job_id'], attempt, None), JobStatus.FINISHED,
                        WorkerTaskStatus.COMPLETED, None, 'retained answer', False,
                        ('src/observed.py',), None, None, now, now), now)
    history.update_job(database, row['job_id'], status=status,
                       **({'ended_at': now.isoformat()} if ended else {}))
    collected = call('modelquad_job_collect', job_id=row['job_id'])
    before = database.read_bytes()
    # Even a clamped request must return exactly the collect payload at terminal.
    response = call('modelquad_job_wait', job_id=row['job_id'], wait_seconds=0)
    assert response == collected
    assert set(response) == {'job_id', 'receipt', 'job_status', 'worker_claim', 'output',
                             'changed_files', 'partial', 'expires_at', 'expiring_soon',
                             'tree_confirmed_dead', 'descendant_uncertainty',
                             'output_truncated', 'original_bytes', 'host_diagnostics_status'}
    assert response['host_diagnostics_status'] == 'unavailable'
    assert response['tree_confirmed_dead'] is None  # No retained termination facts.
    assert response['descendant_uncertainty'] == {
        'census': 'not_observed', 'listed': 0, 'more_unlisted': False}
    assert response['output_truncated'] is False
    assert response['original_bytes'] == len('retained answer'.encode('utf-8'))
    assert call('modelquad_job_collect', job_id=row['job_id']) == collected
    assert store.state(row['job_id'], now) == 'pending'
    assert database.read_bytes() == before
    assert wait_clock.sleeps == [] and len(scheduled) == 1  # recovery once, at the start


def test_wait_finishes_mid_wait_without_using_the_deadline(pending_listing, wait_clock):
    database, _, _ = pending_listing
    attempt = record_attempt(database, task_type='review')
    row = history.reserve_job(database, 'wait-finishing', attempt)
    history.update_job(database, row['job_id'], status='running')

    def finish():
        history.update_job(database, row['job_id'], status='finished',
                           ended_at=datetime.now(timezone.utc).isoformat())

    wait_clock.after_sleep = finish
    response = call('modelquad_job_wait', job_id=row['job_id'], wait_seconds=10)
    assert response == call('modelquad_job_collect', job_id=row['job_id'])
    assert response['job_status'] == 'finished'
    assert wait_clock.sleeps == [0.5]
    assert wait_clock.now == 100.5


@pytest.mark.parametrize('seconds,expected,clamped', [
    (0, 1, True), (541, 540, True), (None, 50, False), (2, 2, False),
])
def test_wait_deadline_and_clamps_leave_job_unchanged(pending_listing, wait_clock, seconds, expected, clamped):
    database, store, scheduled = pending_listing
    attempt = record_attempt(database, task_type='review')
    row = history.reserve_job(database, 'wait-running', attempt)
    history.update_job(database, row['job_id'], status='running')
    before = database.read_bytes()
    response = call('modelquad_job_wait', job_id=row['job_id'], wait_seconds=seconds)
    assert response == {'job_id': row['job_id'], 'status': 'running', 'terminal': False,
                        'runner_liveness': 'unknown',
                        'waited_seconds': expected, 'wait_again_with': 'modelquad_job_wait',
                        **({'wait_clamped': True} if clamped else {})}
    assert sum(wait_clock.sleeps) == expected
    # Crash recovery is scheduled at the start and about every 30 s of waiting.
    assert scheduled and all(kwargs == {'rate_limited': True} for _path, kwargs in scheduled)
    assert len(scheduled) <= 1 + expected // 30
    assert database.read_bytes() == before
    assert not store.pending(datetime.now(timezone.utc))


def test_wait_missing_job_returns_without_sleep(pending_listing, wait_clock):
    assert call('modelquad_job_wait', job_id='missing') == {'job_id': 'missing', 'status': 'not_found'}
    assert wait_clock.sleeps == []


def test_wait_direct_worker_guard_precedes_read(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setenv('MODELQUAD_WORKER', '1')
    monkeypatch.setattr(jobs_tools, 'job_status', lambda *a, **kw: pytest.fail('worker read a job'))
    with pytest.raises(ToolError, match='ModelQuad refuses worker MCP calls'):
        jobs_tools.modelquad_job_wait('guarded', str(Path.cwd()))


@pytest.mark.parametrize('cancel', [False, True])
def test_wait_allows_other_mcp_calls_and_cancellation_is_read_only(pending_listing, monkeypatch, cancel):
    database, _, _ = pending_listing
    attempt = record_attempt(database, task_type='review')
    row = history.reserve_job(database, 'wait-concurrent', attempt)
    history.update_job(database, row['job_id'], status='running')
    before = database.read_bytes()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    read = jobs_tools.job_status
    collect = jobs_tools.collect_job_response

    def blocking_read(job_id, **kwargs):
        if job_id == row['job_id']:
            entered.set()
            assert release.wait(2 * LOAD_HEADROOM_S), 'event loop could not service another call'
            result = read(job_id, **kwargs)
            # A terminal observation exits immediately, without a DB write.
            return {**result, 'status': 'finished'}
        return read(job_id, **kwargs)

    def collect_and_finish(*args, **kwargs):
        try:
            return collect(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(jobs_tools, 'job_status', blocking_read)
    monkeypatch.setattr(jobs_tools, 'collect_job_response', collect_and_finish)

    async def check():
        waiting = asyncio.create_task(mcp.server.call_tool(
            'modelquad_job_wait', {'job_id': row['job_id'], 'project_directory': _PROJECT_DIRECTORY}))
        try:
            assert await asyncio.to_thread(entered.wait, LOAD_HEADROOM_S)
            quick = await asyncio.wait_for(mcp.server.call_tool(
                'modelquad_job_status', {'job_id': 'missing', 'project_directory': _PROJECT_DIRECTORY}),
                LOAD_HEADROOM_S)
            assert tool_payload(quick) == {'job_id': 'missing', 'status': 'not_found'}
            assert not waiting.done()
            if cancel:
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            release.set()
            if not cancel:
                result = tool_payload(await waiting)
                assert result['job_status'] == 'finished'
        finally:
            release.set()
            assert await asyncio.wait_for(asyncio.to_thread(finished.wait, LOAD_HEADROOM_S), 2 * LOAD_HEADROOM_S)

    asyncio.run(check())
    assert database.read_bytes() == before
    assert history.read_job(database, row['job_id'])['status'] == 'running'


@pytest.mark.parametrize('status', ['starting', 'running', 'unknown'])
def test_reopened_host_lists_open_jobs_without_content_or_writes(pending_listing, status):
    database, _, scheduled = pending_listing
    attempt = record_attempt(database, task_type='review')
    row = history.reserve_job(database, 'open-job', attempt)
    history.update_job(database, row['job_id'], status=status)
    # Even an old starting row must not be expired by this discovery query.
    conn = sqlite3.connect(database)
    created_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute('UPDATE jobs SET created_at=?', (created_at,))
    conn.commit()
    conn.close()
    before = database.read_bytes()
    fresh = mcp.WorkerGuardMCPServer('reopened-host')
    jobs_tools.register(fresh)
    response = tool_payload(asyncio.run(fresh.call_tool(
        'modelquad_job_list_pending', {'project_directory': _PROJECT_DIRECTORY})))
    assert response == {'jobs': [], 'running': [
        {'job_id': row['job_id'], 'status': status, 'created_at': created_at}]}
    # The service-free fallback is a pure read: it schedules no recovery.
    assert scheduled == []
    assert database.read_bytes() == before


def test_finished_job_moves_from_running_to_retained_jobs(pending_listing):
    from modelquad.execution.base import WorkerTaskStatus
    from modelquad.jobs.seam import JobOutput, JobRef, JobStatus

    database, store, _ = pending_listing
    attempt = record_attempt(database, task_type='review')
    row = history.reserve_job(database, 'finished-job', attempt)
    history.update_job(database, row['job_id'], status='running')
    assert call('modelquad_job_list_pending')['running'][0]['job_id'] == row['job_id']
    now = datetime.now(timezone.utc)
    output = JobOutput(JobRef(row['job_id'], attempt, None), JobStatus.FINISHED,
                       WorkerTaskStatus.COMPLETED, None, 'private-answer-canary',
                       False, ('src/private-file.py',), None, None,
                       datetime.fromisoformat(row['created_at']), now)
    store.put(output, now)
    history.update_job(database, row['job_id'], status='finished', ended_at=now.isoformat())
    response = call('modelquad_job_list_pending')
    assert response['running'] == []
    assert len(response['jobs']) == 1
    assert response['jobs'][0]['job_id'] == row['job_id']
    assert response['jobs'][0]['job_status'] == 'finished'
    assert set(response['jobs'][0]) == {'job_id', 'job_status', 'expires_at', 'expiring_soon'}
    assert 'private-answer-canary' not in json.dumps(response)
    assert 'private-file.py' not in json.dumps(response)


def test_running_listing_is_newest_first_and_bounded(pending_listing):
    database, _, _ = pending_listing
    base = datetime.now(timezone.utc)
    attempts = [record_attempt(database, task_type='review') for _ in range(55)]
    conn = sqlite3.connect(database)
    conn.executemany(
        'INSERT INTO jobs (job_id, attempt_id, status, created_at, updated_at) '
        "VALUES (?, ?, 'running', ?, ?)",
        [(f'job-{index:02}', attempts[index], (base + timedelta(seconds=index)).isoformat(),
          base.isoformat()) for index in range(55)],
    )
    conn.commit()
    conn.close()
    response = call('modelquad_job_list_pending')
    assert response['jobs'] == []
    assert [row['job_id'] for row in response['running']] == [
        f'job-{index:02}' for index in range(54, 4, -1)]
    assert history.list_active_jobs(database, limit=100) == response['running']
    assert history.list_active_jobs(database, limit=2) == response['running'][:2]
    assert history.list_active_jobs(database, limit=0) == []


def test_active_job_reader_does_not_create_missing_database(tmp_path):
    database = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        history.list_active_jobs(database)
    assert not database.exists()


def test_unreadable_state_database_still_returns_retained_outputs(pending_listing, monkeypatch):
    from modelquad.execution.base import WorkerTaskStatus
    from modelquad.jobs.seam import JobOutput, JobRef, JobStatus

    database, store, _ = pending_listing
    now = datetime.now(timezone.utc)
    attempt = record_attempt(database, task_type='review')
    history.reserve_job(database, 'kept-job', attempt)
    history.update_job(database, 'kept-job', status='finished', ended_at=now.isoformat())
    store.put(JobOutput(JobRef('kept-job', attempt, None), JobStatus.FINISHED, WorkerTaskStatus.COMPLETED,
                        None, 'answer', False, None, None, None, now, now), now)

    def broken(_path, **_kwargs):
        raise sqlite3.OperationalError('no such table: jobs')

    monkeypatch.setattr(history, 'list_active_jobs', broken)
    response = call('modelquad_job_list_pending')
    assert response['running'] == []
    assert [entry['job_id'] for entry in response['jobs']] == ['kept-job']


def test_slow_handle_stop(synthetic, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, 'JOB_WAIT_SECONDS', 0)
    response = delegate(tmp_path, SLOW_PROMPT)
    assert response.get('status') in {'starting', 'running'}, response
    assert set(response) == {'job_id', 'status', 'poll_with'}
    assert response['poll_with'] == 'modelquad_job_wait'
    assert call('modelquad_job_stop', job_id=response['job_id'])['stop_requested']
    row = terminal(synthetic, response['job_id'])
    assert row['status'] == 'stopped'
    collected = call('modelquad_job_collect', job_id=response['job_id'])
    assert collected['job_status'] == 'stopped'
    assert 'private-tool-output-canary' not in json.dumps(collected)


def test_setting_off_equivalence_and_prepared_roundtrip(synthetic, tmp_path, monkeypatch):
    history.put_job_runner_enabled(synthetic, False)
    direct = delegate(tmp_path)
    direct_row = history.fetch_delegation_attempts(synthetic, limit=1)[0]
    assert direct['output'] == 'synthetic extracted answer'
    history.put_job_runner_enabled(synthetic, True)
    captured = []
    original = mcp.prepare_delegation
    def prepare(*args, **kw):
        value = original(*args, **kw)
        captured.append(value)
        assert PreparedDelegation.from_dict(json.loads(json.dumps(value.to_dict()))) == value
        return value
    monkeypatch.setattr(mcp, 'prepare_delegation', prepare)
    detached = delegate(tmp_path)
    row = history.read_job_attempt(synthetic, history.read_job(synthetic, detached['job_id'])['attempt_id'])
    assert direct['output'] == detached['output']
    for key in ('status', 'failure_kind', 'outcome', 'routed_provider', 'detail'):
        assert row[key] == direct_row[key]
    assert captured[0].decision_id == row['decision_id']
    assert Path(captured[0].config_path).is_absolute()


@pytest.mark.parametrize('control', ['pause', 'disconnect', 'eligibility'])
def test_front_door_refusals_start_no_job(synthetic, tmp_path, monkeypatch, control):
    if control == 'pause':
        history.put_routing_paused(synthetic, True)
    elif control == 'disconnect':
        history.set_host_disconnected(synthetic, 'codex', True)
    else:
        from types import SimpleNamespace
        from modelquad.models import ProviderStatus
        monkeypatch.setattr(mcp, 'build_providers', lambda *a: [
            SimpleNamespace(get_status=lambda: ProviderStatus('codex', available=False))])
    kwargs = {'host': 'codex'} if control == 'disconnect' else {}
    response = mcp._delegate_payload('review', 'quick', str(tmp_path), provider='codex',
                                     timeout=60, **kwargs)
    assert response['refused'], response
    conn = sqlite3.connect(synthetic)
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    conn.close()


def test_launch_refused_never_falls_back(synthetic, tmp_path, monkeypatch):
    def fail_write(*args, **kwargs):
        raise OSError('synthetic_spec_failure')
    monkeypatch.setattr(handle, '_write_spec', fail_write)
    monkeypatch.setattr(mcp, 'run_bound_delegation', lambda *a, **kw: pytest.fail('launch refusal executed'))
    response = delegate(tmp_path)
    assert response['refused'] and response['reason'] == 'runner_launch_failed'
    row = history.read_job(synthetic, response['job_id'])
    assert row['status'] == 'failed' and row['worker_pid'] is None
    attempt = history.read_job_attempt(synthetic, row['attempt_id'])
    assert attempt['failure_kind'] == 'failed_to_start'


def test_disconnect_after_preparation_refused_by_runner(synthetic, tmp_path, monkeypatch):
    original = service_client.connect(synthetic)
    class DisconnectingService:
        def launch(self, job_id, spec_name):
            history.set_host_disconnected(synthetic, 'codex', True)
            return original.launch(job_id, spec_name)
    monkeypatch.setattr(service_client, 'connect', lambda *a: DisconnectingService())
    response = mcp._delegate_payload('review', 'quick', str(tmp_path), provider='codex',
                                     timeout=60, host='codex')
    row = history.read_job(synthetic, response['job_id'])
    assert row['status'] == 'failed' and row['worker_pid'] is None
    attempt = history.read_job_attempt(synthetic, row['attempt_id'])
    assert attempt['status'] == 'refused' and attempt['failure_kind'] == 'policy_refused'
    assert response['refused'] is True
    assert response['reason'] == attempt['detail']


@pytest.mark.parametrize('opt_in', [None, '', '0', 'true', '1'])
@pytest.mark.parametrize('dev_checkout', [False, True])
def test_registry_hook_requires_checkout_or_explicit_opt_in(monkeypatch, opt_in, dev_checkout):
    monkeypatch.setenv('MODELQUAD_TEST_EXECUTOR_PLUGIN', 'invalid.module:crash')
    if opt_in is None:
        monkeypatch.delenv('MODELQUAD_TEST_EXECUTOR_OK', raising=False)
    else:
        monkeypatch.setenv('MODELQUAD_TEST_EXECUTOR_OK', opt_in)
    monkeypatch.setattr(paths, 'is_dev_checkout', lambda: dev_checkout)
    if dev_checkout or opt_in == '1':
        with pytest.raises(ModuleNotFoundError):
            registry.default_registry()
    else:
        assert len(registry.default_registry().all()) > 1


def test_synthetic_registry_importable_in_child_without_changing_product_root(synthetic):
    import modelquad

    probe = (
        "import json, modelquad; from modelquad import registry; "
        "print(json.dumps([modelquad.__file__, "
        "registry.default_registry().all()[0].display_name]))"
    )
    child = subprocess.run([sys.executable, '-c', probe], capture_output=True,
                           text=True, timeout=10, **procutil.no_window_kwargs())
    assert child.returncode == 0, child.stderr
    root, provider = json.loads(child.stdout)
    assert Path(root).resolve() == Path(modelquad.__file__).resolve()
    assert provider == 'Synthetic Codex'


def test_new_tool_schemas_and_worker_guard(synthetic, monkeypatch):
    tools = asyncio.run(mcp.server.list_tools())
    selected = {tool.name: set(tool.input_schema['properties']) for tool in tools if tool.name in TOOLS}
    assert set(selected) == TOOLS
    assert selected['modelquad_job_acknowledge'] == {'job_id', 'receipt', 'project_directory'}
    assert selected['modelquad_job_wait'] == {'job_id', 'wait_seconds', 'project_directory'}
    assert selected['modelquad_job_list_pending'] == {'project_directory'}
    for name in ('modelquad_job_status', 'modelquad_job_collect', 'modelquad_job_stop'):
        assert selected[name] == {'job_id', 'project_directory'}
    # Every job tool requires the caller's project: no job crosses workspaces.
    for tool in tools:
        if tool.name in TOOLS:
            assert 'project_directory' in tool.input_schema.get('required', []), tool.name
            project_schema = tool.input_schema['properties']['project_directory']
            assert project_schema.get('type') == 'string', tool.name
    monkeypatch.setenv('MODELQUAD_WORKER', '1')
    for name in TOOLS:
        result = asyncio.run(mcp.server.call_tool(name, {}))
        assert result.is_error and tool_payload(result)['refused']


def test_legacy_reconciliation_skips_job_attempt(synthetic):
    attempt_id = history.record_delegation_attempt(synthetic, task_type='review')
    history.reserve_job(synthetic, 'reserved', attempt_id)
    assert history.reconcile_stale_attempts(synthetic, now=datetime.now(timezone.utc)+timedelta(days=1)) == 0
    assert history.read_job_attempt(synthetic, attempt_id)['resolved_at'] is None
    history.fail_job_attempt(synthetic, 'reserved', detail='test_cleanup')


def test_spec_permission_failure_and_abandoned_cleanup(synthetic, tmp_path, monkeypatch):
    from modelquad.adapters import file_security

    attempt_id = history.record_delegation_attempt(synthetic, task_type='review')
    spec = handle.JobSpec((sys.executable, '-c', 'pass'), str(tmp_path), str(synthetic), attempt_id, 3)
    old = tmp_path / 'job-abandoned.json'
    old.write_text('private-transient-prompt', encoding='utf-8')
    os.utime(old, (time.time()-601, time.time()-601))
    monkeypatch.setattr(file_security, 'create_exclusive',
                        lambda path: (_ for _ in ()).throw(OSError('denied')))
    assert not handle.start_job(spec).launched
    assert not old.exists() and not list(tmp_path.glob('job-*.json'))


@pytest.mark.parametrize('stop_before_entry', [False, True])
def test_runner_publishes_before_single_resolution(synthetic, tmp_path, monkeypatch, stop_before_entry):
    observed = []
    original = history.resolve_delegation_attempt
    def resolution(database, attempt_id, **kwargs):
        row = history.job_for_attempt(database, attempt_id)
        if row is not None:
            retained = jobs_tools.default_store().collect(row['job_id'], datetime.now(timezone.utc))
            assert retained is not None, 'attempt was resolved before publication'
            assert kwargs['only_if_open'] is True
            observed.append((threading.get_ident(), retained.output.job_status.value))
        return original(database, attempt_id, **kwargs)
    monkeypatch.setattr(history, 'resolve_delegation_attempt', resolution)
    if stop_before_entry:
        registration = history.update_job_startup
        def register(database, job_id, **kwargs):
            answer = registration(database, job_id, **kwargs)
            if kwargs.get('runner_pid'):
                row = history.read_job(database, job_id)
                history.request_attempt_stop(database, row['attempt_id'])
            return answer
        monkeypatch.setattr(history, 'update_job_startup', register)
    response = delegate(tmp_path)
    assert len(observed) == 1
    assert observed[0][0] != threading.get_ident()
    assert response['job_status'] == ('stopped' if stop_before_entry else 'finished')


def test_host_wait_exception_does_not_resolve_runner_attempt(synthetic, tmp_path, monkeypatch):
    normal_status = mcp.job_status
    captured = []
    def fail_wait(job_id, **kwargs):
        captured.append(job_id)
        raise history.LaunchStateUnavailable('synthetic_wait_failed')
    monkeypatch.setattr(mcp, 'job_status', fail_wait)
    with pytest.raises(history.LaunchStateUnavailable):
        mcp._delegate_payload('review', SLOW_PROMPT, str(tmp_path), provider='codex', timeout=60)
    row = history.read_job(synthetic, captured[0])
    assert history.read_job_attempt(synthetic, row['attempt_id'])['resolved_at'] is None
    handle.stop_job(row['job_id'], db_path=synthetic)
    terminal(synthetic, row['job_id'])


def reserve_only(monkeypatch, captured):
    """Synthetic test driver: reserve a prepared call without spawning it."""
    class ReserveOnlyService:
        def launch(self, job_id, spec_name):
            database = db.DEFAULT_DB_PATH
            path = database.parent / spec_name
            spec = handle.JobSpec(**json.loads(path.read_text(encoding='utf-8')))
            row = history.read_job(database, job_id)
            ref = handle.JobRef(job_id, row['attempt_id'], row['decision_id'])
            captured.append((spec, ref, path))
            return 'launched', 'launched'
    monkeypatch.setattr(service_client, 'connect', lambda database: ReserveOnlyService())
    monkeypatch.setattr(mcp, 'JOB_WAIT_SECONDS', 0)


def test_startup_expiry_races_late_runner_without_orphan_output(synthetic, tmp_path, monkeypatch):
    captured = []
    reserve_only(monkeypatch, captured)
    delegate(tmp_path)
    spec, ref, path = captured[0]
    conn = sqlite3.connect(synthetic)
    conn.execute('CREATE TABLE resolution_observations (status TEXT)')
    conn.execute('CREATE TRIGGER observe_resolution AFTER UPDATE OF resolved_at ON delegation_attempts '
                 'WHEN OLD.resolved_at IS NULL AND NEW.resolved_at IS NOT NULL BEGIN '
                 'INSERT INTO resolution_observations VALUES (NEW.status); END')
    conn.commit()
    before_registration = threading.Event()
    release_registration = threading.Event()
    registration = history.update_job_startup
    registrations = []
    def register(database, job_id, **kwargs):
        if kwargs.get('runner_pid'):
            before_registration.set()
            assert release_registration.wait()
        answer = registration(database, job_id, **kwargs)
        if kwargs.get('runner_pid'):
            registrations.append(answer)
        return answer
    monkeypatch.setattr(history, 'update_job_startup', register)
    results = []
    def late():
        results.append(runner.run_job(ref.job_id, path, db_path=synthetic))
    thread = threading.Thread(target=late)
    thread.start()
    try:
        assert before_registration.wait(LOAD_HEADROOM_S), 'runner did not reach registration'
        assert history.read_job(synthetic, ref.job_id)['heartbeat_at'] is None
        # Only the expiry poll sees the advanced clock. Registration would
        # succeed at real time if it won; the reservation is not back-dated.
        class ExpiryClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(seconds=handle.STARTUP_SECONDS + 1)
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr(history, 'datetime', ExpiryClock)
            expired = handle.job_status(ref.job_id, db_path=synthetic)
        assert expired['status'] == 'failed' and expired['ended_at'] is not None
        assert registrations == []
        assert conn.execute('SELECT COUNT(*) FROM resolution_observations').fetchone()[0] == 1
    finally:
        release_registration.set()
        thread.join(LOAD_HEADROOM_S)
    assert not thread.is_alive() and results == [1]
    assert registrations == [False]
    assert conn.execute('SELECT COUNT(*) FROM resolution_observations').fetchone()[0] == 1
    conn.close()
    final = history.read_job(synthetic, ref.job_id)
    assert final['runner_pid'] is None and final['worker_pid'] is None
    assert history.read_job_attempt(synthetic, ref.attempt_id)['detail'] == 'runner_startup_deadline_exceeded'
    assert jobs_tools.default_store().collect(ref.job_id, datetime.now(timezone.utc)) is None
    assert jobs_tools.default_store().pending(datetime.now(timezone.utc)) == ()
    assert not path.exists()


def test_losing_resolution_retains_output_and_uses_existing_terminal(synthetic, tmp_path, monkeypatch, caplog):
    original = history.resolve_delegation_attempt
    def already_terminal(database, attempt_id, **kwargs):
        if history.job_for_attempt(database, attempt_id) is not None:
            original(database, attempt_id, status='failed', failure_kind='exception', only_if_open=True)
            return False
        return original(database, attempt_id, **kwargs)
    monkeypatch.setattr(history, 'resolve_delegation_attempt', already_terminal)
    response = delegate(tmp_path)
    assert response['job_status'] == 'failed'
    assert 'already terminal' in caplog.text
    assert 'recovery required' not in caplog.text
    assert response['output'] == 'synthetic extracted answer'
    assert call('modelquad_job_collect', job_id=response['job_id']) == response


@pytest.mark.parametrize('unreadable_attempt', [False, True])
def test_resolution_storage_failure_keeps_open_for_recovery(synthetic, tmp_path, monkeypatch, caplog, unreadable_attempt):
    finalized = threading.Event()
    original_finish = runner._finish_job
    def finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        finalized.set()
    monkeypatch.setattr(runner, '_finish_job', finish)
    monkeypatch.setattr(history, 'resolve_delegation_attempt', lambda *a, **k: False)
    read_attempt = history.read_job_attempt
    if unreadable_attempt:
        monkeypatch.setattr(history, 'read_job_attempt', lambda *a: (_ for _ in ()).throw(OSError('unavailable')))
    monkeypatch.setattr(mcp, 'JOB_WAIT_SECONDS', 0)
    response = delegate(tmp_path)
    # Headroom for a loaded hosted runner (train 15 hosted 1/2 did not observe
    # resolution within 5 s); the assertion is unchanged: the runner must reach it.
    assert finalized.wait(30), 'runner did not reach resolution within the bounded wait'
    row = history.read_job(synthetic, response['job_id'])
    assert row['status'] == 'unknown'
    observed = call('modelquad_job_status', job_id=row['job_id'])
    collected = call('modelquad_job_collect', job_id=row['job_id'])
    if unreadable_attempt:
        # The job's project cannot be confirmed, so the tools report unknown
        # without serving the job across a possibly different workspace.
        unscoped = {'job_id': row['job_id'], 'status': 'unknown',
                    'reason': 'project_scope_unavailable'}
        assert observed == collected == unscoped
    else:
        assert observed['status'] == 'unknown' and observed['terminal'] is False
        assert collected['job_status'] == 'unknown'
    assert row['ended_at'] is None
    assert read_attempt(synthetic, row['attempt_id'])['resolved_at'] is None
    assert jobs_tools.default_store().collect(row['job_id'], datetime.now(timezone.utc)) is not None
    assert 'recovery required' in caplog.text
    assert 'already terminal' not in caplog.text
    history.fail_job_attempt(synthetic, row['job_id'], detail='test_cleanup')


def test_private_spec_protected_before_prompt_and_deleted_on_read(synthetic, tmp_path, monkeypatch, request):
    from test_adapter_file_security import _assert_exclusive, _assert_private, _broad_folder, _read

    captured = []
    reserve_only(monkeypatch, captured)
    protected = []
    if os.name == 'nt':
        import msvcrt

        security_reader = request.getfixturevalue('native')
        account = request.getfixturevalue('user')
        _broad_folder(request.getfixturevalue('acl_tree'), tmp_path, account)
    original = os.fdopen

    def before_write(descriptor, *args, **kwargs):
        path, = tmp_path.glob('job-*.json')
        assert os.fstat(descriptor).st_size == 0
        if os.name == 'nt':
            _assert_private(_read(security_reader, handle=msvcrt.get_osfhandle(descriptor)), account)
            _assert_exclusive(security_reader, path)
        else:
            assert os.fstat(descriptor).st_mode & 0o777 == 0o600
        protected.append(path)
        return original(descriptor, *args, **kwargs)

    monkeypatch.setattr(handle, '_protect_spec', lambda path: None)
    original_write_spec = handle._write_spec

    def write_spec(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fdopen', before_write)
            return original_write_spec(*args, **kwargs)

    monkeypatch.setattr(handle, '_write_spec', write_spec)
    delegate(tmp_path, 'private-transient-prompt-canary')
    spec, ref, path = captured[0]
    assert protected == [path]
    if os.name == 'nt':
        _assert_private(_read(security_reader, path), account)
    assert 'private-transient-prompt-canary' in path.read_text(encoding='utf-8')
    assert b'private-transient-prompt-canary' not in synthetic.read_bytes()
    assert runner.run_job(ref.job_id, path, db_path=synthetic) == 0
    assert not path.exists()


@pytest.mark.serial_timing
def test_prepared_runner_in_fresh_process_kills_worker_tree_on_close(synthetic, tmp_path, monkeypatch,
                                                                  non_killing_runner_spawn):
    # An explicitly driven synthetic runner process, not start_job or proof
    # of any Host's detached survival/containment policy.
    captured = []
    reserve_only(monkeypatch, captured)
    # Startup needs slack on the shared, self-hosted Windows runner. The worker
    # budget must exceed the tree-observation bound so timeout cannot kill it first.
    call('modelquad_delegate', task_type='review', prompt=TREE_PROMPT,
         working_directory=str(tmp_path), timeout=120)
    spec, ref, path = captured[0]
    # A fresh interpreter does not inherit the parent's imported modules or
    # monkeypatches. Keep its provenance, startup stack and actual exit beside
    # the lifecycle evidence instead of diagnosing a missing PID as timing.
    child_driver = '''
import faulthandler, json, os, runpy, sys
faulthandler.enable()
faulthandler.dump_traceback_later(58, repeat=False)
try:
    import modelquad
    print(json.dumps({
        'event': 'synthetic_runner_bootstrap',
        'python': sys.executable,
        'source': modelquad.__file__,
        'plugin': os.environ.get('MODELQUAD_TEST_EXECUTOR_PLUGIN'),
        'plugin_opt_in': os.environ.get('MODELQUAD_TEST_EXECUTOR_OK'),
    }), flush=True)
    # A loaded machine can take longer than the product's 15 s to import and
    # register a fresh runner (tests/test_jobs_runner.py raises it the same way).
    import modelquad.jobs.handle as _handle
    _handle.STARTUP_SECONDS = 60.0
    runpy.run_module('modelquad.jobs.runner', run_name='__main__')
finally:
    faulthandler.cancel_dump_traceback_later()
'''
    stdout_path = tmp_path / 'runner.stdout.log'
    stderr_path = tmp_path / 'runner.stderr.log'

    def diagnostics():
        def record(reader, fields):
            try:
                row = reader() or {}
                return {key: row.get(key) for key in fields}
            except Exception as error:
                return {'read_error': type(error).__name__}

        def captured_text(log_path):
            try:
                with log_path.open('rb') as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 8192))
                    return stream.read().decode('utf-8', errors='replace')
            except OSError as error:
                return 'capture unavailable: ' + type(error).__name__

        lifecycle = {
            'runner_exit_code': proc.poll(),
            'spec_exists': path.exists(),
            'job': record(lambda: history.read_job(synthetic, ref.job_id), (
                'status', 'runner_pid', 'runner_created', 'worker_pid',
                'worker_created', 'heartbeat_at', 'ended_at', 'launch_limitation')),
            'attempt': record(lambda: history.read_job_attempt(synthetic, ref.attempt_id), (
                'status', 'worker_pid', 'resolved_at', 'failure_kind', 'detail')),
        }
        return (json.dumps(lifecycle, sort_keys=True) + '\nstdout tail:\n' +
                captured_text(stdout_path) + '\nstderr tail:\n' + captured_text(stderr_path))

    tree_launch_started = time.monotonic()
    with stdout_path.open('wb') as child_stdout, stderr_path.open('wb') as child_stderr:
        command = runner_driver_command(child_driver,
            ['--job-id', ref.job_id, '--spec', str(path), '--db-path', str(synthetic)])
        proc = non_killing_runner_spawn(command,
                                stdout=child_stdout, stderr=child_stderr,
                                **procutil.no_window_kwargs())
    worker = None
    descendants = []
    try:
        # Runner startup imports the MCP server module; preflight load once took >18 s.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            worker = history.read_job(synthetic, ref.job_id)['worker_pid']
            entries = procutil._windows_process_snapshot() if worker else []
            descendants = [pid for pid, parent, name in (entries or []) if parent == worker]
            if worker and descendants:
                break
            if proc.poll() is not None:
                break  # Preserve the real exit and lifecycle, never retry startup.
            time.sleep(.05)
        assert worker and descendants, ('synthetic plugin did not start its worker tree\n' + diagnostics())
        if os.name == 'nt':
            row = history.read_job(synthetic, ref.job_id)
            assert row['launch_limitation'] == handle.CONTAINED_RUNNER_LIMITATION, diagnostics()
            assert row['heartbeat_at'] is not None, diagnostics()
        assert proc.poll() is None, diagnostics()
        assert all(procutil.pid_state(pid) == 'alive' for pid in [worker, *descendants]), diagnostics()
        # The fixture sleeps 30 s (worker) and 60 s (grandchild): each must have at
        # least 15 s left, so natural exits cannot satisfy the 12 s death bound.
        # Age comes from the worker's OS creation time (slow runner startup does not
        # age the tree); counting from before runner launch is the fallback bound.
        worker_created = procutil.process_creation_epoch(worker)
        tree_age_upper_bound = (time.time() - worker_created if worker_created is not None
                                else time.monotonic() - tree_launch_started)
        assert 30 - tree_age_upper_bound >= 15, (
            f'worker tree observed too late: age upper bound {tree_age_upper_bound:.3f}s\n' + diagnostics()
        )
        killed_at = time.monotonic()
        proc.kill()
        # One shared bound gives the self-hosted runner slack without allowing
        # separate runner/tree waits to stretch into the fixture's natural exit.
        deadline = killed_at + 12
        while True:
            runner_exited = proc.poll() is not None
            states = {pid: procutil.pid_state(pid) for pid in [worker, *descendants]}
            elapsed = time.monotonic() - killed_at
            if (runner_exited and all(state == 'dead' for state in states.values())) or time.monotonic() >= deadline:
                break
            time.sleep(.05)
        assert runner_exited and all(state == 'dead' for state in states.values()) and elapsed <= 12, (
            f'runner/tree death took {elapsed:.3f}s (limit 12s): '
            f'runner_exited={runner_exited}, tree_states={states}\n' + diagnostics()
        )
    finally:
        # Capture the unresolved state before test cleanup replaces its reason.
        try:
            print('synthetic runner before cleanup:\n' + diagnostics())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
            try:
                print('synthetic runner after exit:\n' + diagnostics())
            finally:
                history.fail_job_attempt(synthetic, ref.job_id, detail='synthetic_runner_killed')



def test_prepared_full_inputs_survive_runner_rebuild(synthetic, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from modelquad.execution.manager import ExecutionManager
    from modelquad.models import CapacityState, Freshness, ProviderStatus, ResourcePool

    now = datetime.now(timezone.utc)
    status = ProviderStatus('codex', True, 90, now, Freshness.FRESH,
                            metadata={'windows': [{'remaining': 90}]},
                            capacity=CapacityState(90, 10, now+timedelta(hours=1), 'short',
                                                   Freshness.FRESH, now, 1.5, 'spend'),
                            pools=(ResourcePool('synthetic-pool', 'codex', observed_at=now,
                                                remaining_percentage=90, raw={'remaining': 90}),))
    monkeypatch.setattr(mcp, 'build_providers', lambda *a: [SimpleNamespace(get_status=lambda: status)])
    prepared = []
    inputs = []
    original_prepare = mcp.prepare_delegation
    original_delegate = ExecutionManager.delegate
    def prepare(args, **kwargs):
        value = original_prepare(args, **kwargs)
        prepared.append(value)
        return value
    def execute(manager, *args, **kwargs):
        inputs.append(kwargs)
        return original_delegate(manager, *args, **kwargs)
    monkeypatch.setattr(mcp, 'prepare_delegation', prepare)
    monkeypatch.setattr(ExecutionManager, 'delegate', execute)
    # The release tool takes no session or consumption hint; only model and effort pass through.
    response = delegate(tmp_path, model='synthetic-model', reasoning_effort='medium',
                        capsule={'goal': 'synthetic test', 'task_type': 'review',
                                 'capability_requirements': ['read'],
                                 'relevant_tests': ['tests/synthetic_test.py']})
    assert response.get('job_status') == 'finished', json.dumps(response)
    value = PreparedDelegation.from_dict(json.loads(json.dumps(prepared[0].to_dict())))
    assert value == prepared[0]
    assert value.rebuild_statuses() == [status]
    assert inputs[0]['statuses'] == [status]
    assert inputs[0]['model'] == 'synthetic-model'
    assert inputs[0]['session_id'] is None
    assert inputs[0]['reasoning_effort'] == 'medium'
    assert inputs[0]['expected_consumption'].source.value == 'estimated'
    assert inputs[0]['verification'].relevant_tests == ('tests/synthetic_test.py',)
    assert inputs[0]['required_capabilities'] == ['read']


def test_unserializable_status_fails_before_reservation(synthetic, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from modelquad.models import ProviderStatus
    monkeypatch.setattr(mcp, 'build_providers', lambda *a: [SimpleNamespace(
        get_status=lambda: ProviderStatus('codex', metadata={'unsupported': object()}))])
    with pytest.raises(TypeError):
        mcp._delegate_payload('review', 'quick', str(tmp_path), provider='codex', timeout=60)
    conn = sqlite3.connect(synthetic)
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    conn.close()


def test_legacy_resolution_cannot_race_job_reservation(synthetic, monkeypatch):
    attempt_id = history.record_delegation_attempt(synthetic, task_type='review')
    history.record_worker_pid(synthetic, attempt_id, 12345)
    def dead(pid):
        history.reserve_job(synthetic, 'racing-reservation', attempt_id)
        return 'dead'
    monkeypatch.setattr(procutil, 'pid_state', dead)
    assert history.reconcile_stale_attempts(synthetic) == 0
    assert history.read_job_attempt(synthetic, attempt_id)['resolved_at'] is None
    history.fail_job_attempt(synthetic, 'racing-reservation', detail='test_cleanup')



def test_module_entry_registers_job_tools_before_start(monkeypatch):
    import runpy
    from mcp.server.mcpserver import MCPServer
    captured = []
    def start(server):
        captured.append({tool.name for tool in asyncio.run(server.list_tools())})
    monkeypatch.setattr(MCPServer, 'run', start)
    with pytest.warns(RuntimeWarning, match='found in sys.modules'):
        runpy.run_module('modelquad.mcp.server', run_name='__main__')
    assert len(captured) == 1 and TOOLS <= captured[0]


def test_collect_unknown_id_matches_status(synthetic):
    status = call('modelquad_job_status', job_id='missing')
    assert status == {'job_id': 'missing', 'status': 'not_found'}
    collected = call('modelquad_job_collect', job_id='missing')
    assert collected == status
    assert 'output' not in collected and 'receipt' not in collected


def test_setting_off_stop_before_executor_keeps_base_resolution(synthetic, tmp_path, monkeypatch):
    from modelquad.execution.manager import ExecutionManager
    from fixtures.fake_executor_plugin import FakeExecutor

    history.put_job_runner_enabled(synthetic, False)
    original = ExecutionManager.delegate
    resolutions = []
    resolve = history.resolve_delegation_attempt
    def record(database, attempt_id, **kwargs):
        resolutions.append(kwargs)
        return resolve(database, attempt_id, **kwargs)
    def stop(manager, *args, **kwargs):
        assert not manager.job_owned_attempt
        assert history.request_attempt_stop(synthetic, manager.launch_attempt_id) == 'accepted'
        return original(manager, *args, **kwargs)
    def never_enter(*args):
        pytest.fail('stopped work entered the executor')
    monkeypatch.setattr(ExecutionManager, 'delegate', stop)
    monkeypatch.setattr(FakeExecutor, 'execute', never_enter)
    monkeypatch.setattr(history, 'resolve_delegation_attempt', record)
    response = delegate(tmp_path)
    row = history.fetch_delegation_attempts(synthetic, limit=1)[0]
    assert response['reason'] == 'stop_requested'
    assert len(resolutions) == 2
    assert all(not item.get('only_if_open', False) for item in resolutions)
    assert row['routed_provider'] == 'codex'
    assert row['duration_seconds'] is not None
    assert row['outcome'] == 'failed' and row['resolved_at'] is not None


def test_setting_off_final_outcome_overwrites_reconciliation(synthetic, tmp_path, monkeypatch):
    history.put_job_runner_enabled(synthetic, False)
    history.put_decision_mode(synthetic, 'reasoning_shadow')
    overtaken = []
    def reconcile(*args, **kwargs):
        row = history.fetch_delegation_attempts(synthetic, limit=1)[0]
        assert row['worker_pid'] and row['resolved_at'] is None
        assert procutil.pid_state(row['worker_pid']) == 'dead'
        assert history.reconcile_stale_attempts(synthetic) == 1
        overtaken.append(history.read_job_attempt(synthetic, row['id']))
    record = history.record_execution
    def record_then_reconcile(*args, **kwargs):
        record(*args, **kwargs)
        reconcile()
    monkeypatch.setattr(history, 'record_execution', record_then_reconcile)
    response = mcp._delegate_payload('review', 'quick', str(tmp_path), timeout=60)
    assert len(overtaken) == 1 and overtaken[0]['failure_kind'] == 'orphaned'
    row = history.fetch_delegation_attempts(synthetic, limit=1)[0]
    assert response['output'] == 'synthetic extracted answer'
    assert row['status'] == 'completed' and row['failure_kind'] is None
    assert row['routed_provider'] == 'codex' and row['duration_seconds'] is not None


def test_capsule_action_authority_is_refused_before_any_job(synthetic, tmp_path, monkeypatch):
    # The worker is read-only: a capsule cannot grant it action authority, and
    # the refusal comes before any attempt, job or worker exists.
    from mcp.server.mcpserver.exceptions import ToolError
    from modelquad.action_authority import TaskAuthority

    authority = TaskAuthority.from_dict({'mode': 'read_only', 'actions': ['read']})
    capsule = {'goal': 'synthetic authority test', 'task_type': 'review',
               'action_authority': authority.to_dict()}
    monkeypatch.setattr(mcp, 'prepare_delegation', lambda *a, **kw: pytest.fail('authority was prepared'))
    for enabled in (False, True):
        history.put_job_runner_enabled(synthetic, enabled)
        with pytest.raises(ToolError, match='capsule action authority is not accepted'):
            delegate(tmp_path, capsule=capsule)
    conn = sqlite3.connect(synthetic)
    try:
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM delegation_attempts').fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize('error_type', [sqlite3.OperationalError, OSError, history.LaunchStateUnavailable])
@pytest.mark.parametrize('prelaunch', [False, True])
def test_setting_off_job_read_failure_preserves_error_contract(synthetic, tmp_path, monkeypatch, error_type, prelaunch):
    history.put_job_runner_enabled(synthetic, False)
    error = history.LaunchStateUnavailable('synthetic_launch_unavailable') if prelaunch else RuntimeError('synthetic_exception')
    def fail_delegation(*args, **kwargs):
        raise error
    def fail_read(*args, **kwargs):
        raise error_type('synthetic_db_unavailable')
    monkeypatch.setattr(mcp, '_delegate_after_policy', fail_delegation)
    if prelaunch:
        expected = mcp._delegate_payload('review', 'quick', str(tmp_path), provider='codex', timeout=60)
    monkeypatch.setattr(history, 'job_for_attempt', fail_read)
    if prelaunch:
        response = mcp._delegate_payload('review', 'quick', str(tmp_path), provider='codex', timeout=60)
        assert response == expected
        assert response['refused'] and response['reason'] == 'synthetic_launch_unavailable'
    else:
        with pytest.raises(RuntimeError, match='synthetic_exception'):
            mcp._delegate_payload('review', 'quick', str(tmp_path), provider='codex', timeout=60)
        row = history.fetch_delegation_attempts(synthetic, limit=1)[0]
        assert row['status'] == 'failed' and row['failure_kind'] == 'exception'
        assert row['detail'] == 'delegation_exception' and row['resolved_at'] is not None


@pytest.mark.parametrize('failure', ['publish', 'finalize'])
def test_runner_publish_or_finalization_failure_is_unknown(synthetic, tmp_path, monkeypatch, caplog, failure):
    captured = []
    reserve_only(monkeypatch, captured)
    delegate(tmp_path)
    spec, ref, path = captured[0]
    def unavailable(*args, **kwargs):
        raise OSError('private-storage-error-canary')
    if failure == 'publish':
        monkeypatch.setattr(FileOutputStore, 'put', unavailable)
    else:
        monkeypatch.setattr(runner, '_finish_job', unavailable)
    assert runner.run_job(ref.job_id, path, db_path=synthetic) == 1
    row = history.read_job(synthetic, ref.job_id)
    assert row['status'] == 'unknown' and row['ended_at'] is None
    assert call('modelquad_job_status', job_id=ref.job_id)['terminal'] is False
    attempt = history.read_job_attempt(synthetic, ref.attempt_id)
    retained = jobs_tools.default_store().collect(ref.job_id, datetime.now(timezone.utc))
    if failure == 'publish':
        assert retained is None and attempt['resolved_at'] is None
    else:
        assert retained.output.output_text == 'synthetic extracted answer'
        assert attempt['resolved_at'] is not None
    assert 'recovery required' in caplog.text
    assert 'private-storage-error-canary' not in caplog.text
    assert 'synthetic extracted answer' not in caplog.text
    history.fail_job_attempt(synthetic, ref.job_id, detail='test_cleanup')


def test_undeletable_stale_spec_does_not_block_launch(synthetic, tmp_path, monkeypatch, caplog):
    stale = tmp_path / 'job-undeletable.json'
    stale.write_text('private-stale-prompt-canary', encoding='utf-8')
    os.utime(stale, (time.time()-601, time.time()-601))
    unlink = Path.unlink
    def deny_one(path, *args, **kwargs):
        if path == stale:
            raise PermissionError('private-deletion-error-canary')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', deny_one)
    response = delegate(tmp_path)
    assert response['job_status'] == 'finished'
    assert stale.exists()
    assert 'stale job spec cleanup unavailable: job-undeletable.json' in caplog.text
    assert str(tmp_path) not in caplog.text
    assert 'private-stale-prompt-canary' not in caplog.text
    assert 'private-deletion-error-canary' not in caplog.text
    assert list(tmp_path.glob('job-*.json')) == [stale]


@pytest.mark.parametrize('after_resolution', [False, True], ids=['open', 'resolved'])
def test_late_delegation_interrupt_keeps_known_fate(synthetic, tmp_path, monkeypatch, after_resolution):
    from modelquad.execution.base import ExecutionResult, describe_worker_termination
    captured = []
    reserve_only(monkeypatch, captured)
    delegate(tmp_path)
    spec, ref, path = captured[0]
    metadata = {'worker_started': True, 'worker_termination': {
        'pid': 123, 'confirmed_dead': True, 'tree_confirmed_dead': True, 'tree_kill': 'ok'}}
    result = ExecutionResult('codex', True, 'answer', 0, metadata=metadata)
    def shared(*args, **kwargs):
        assert procutil.default_kill_on_close.get() is True
        history.record_worker_pid(synthetic, ref.attempt_id, 123)
        kwargs['on_worker_spawn'](123)
        kwargs['publish'](result)
        if after_resolution:
            written = history.resolve_delegation_attempt(synthetic, ref.attempt_id,
                status='completed', only_if_open=True)
            kwargs['on_resolution'](written, result)
        raise KeyboardInterrupt('private-interrupt-canary')
    monkeypatch.setattr(bound_launch, 'run_bound_delegation', shared)
    monkeypatch.setattr(paths, 'logs_dir', lambda: tmp_path / 'logs')
    with pytest.raises(KeyboardInterrupt):
        runner.run_job(ref.job_id, path, db_path=synthetic)
    assert procutil.default_kill_on_close.get() is False
    assert not path.exists()
    attempt = history.read_job_attempt(synthetic, ref.attempt_id)
    row = history.read_job(synthetic, ref.job_id)
    if after_resolution:
        assert attempt['status'] == 'completed'
        assert row['status'] == 'finished' and row['ended_at']
    else:
        assert attempt['failure_kind'] == 'exception' and attempt['resolved_at']
        assert attempt['detail'] == 'runner_crashed; ' + describe_worker_termination(metadata)
        assert row['status'] == 'unknown' and row['ended_at'] is None
    log = (tmp_path / 'logs' / 'job-runner.log').read_text()
    assert log.split()[1:] == [ref.job_id, 'runner_error']
    assert 'private-interrupt-canary' not in log and 'KeyboardInterrupt' not in log
    history.fail_job_attempt(synthetic, ref.job_id, detail='test_cleanup')


def test_finalization_unavailable_preserves_ended_job(synthetic, tmp_path, monkeypatch, caplog):
    captured = []
    reserve_only(monkeypatch, captured)
    delegate(tmp_path)
    spec, ref, path = captured[0]
    # Simulate failure AFTER the shared call has published/resolved/ended.
    def fail_after_resolution(*args):
        raise OSError('private-after-resolution-canary')
    monkeypatch.setattr(bound_launch, 'workspace_stability_note', fail_after_resolution)
    assert runner.run_job(ref.job_id, path, db_path=synthetic) == 1
    row = history.read_job(synthetic, ref.job_id)
    assert row['status'] == 'finished' and row['ended_at']
    before = dict(row)
    runner._finalization_unavailable(spec, ref)
    assert history.read_job(synthetic, ref.job_id) == before
    assert history.read_job_attempt(synthetic, ref.attempt_id)['status'] == 'completed'
    assert jobs_tools.default_store().collect(ref.job_id, datetime.now(timezone.utc)) is not None
    assert 'private-after-resolution-canary' not in caplog.text

@pytest.mark.parametrize('legacy_mode', ['reasoning_shadow', 'reasoning_primary'])
def test_saved_legacy_job_runs_deterministically(synthetic, tmp_path, monkeypatch, legacy_mode):
    captured = []
    reserve_only(monkeypatch, captured)
    delegate(tmp_path)
    spec, ref, path = captured[0]
    raw = json.loads(path.read_text())
    raw['delegation']['provider'] = None
    raw['delegation']['decision_mode'] = legacy_mode
    raw['delegation']['reasoning_fell_back'] = True
    raw['delegation']['plan_decision_id'] = 999
    restored = PreparedDelegation.from_dict(raw['delegation'])
    assert restored.decision_mode == legacy_mode and restored.reasoning_fell_back
    path.write_text(json.dumps(raw), encoding='utf-8')
    assert runner.run_job(ref.job_id, path, db_path=synthetic) == 0
    assert history.read_job(synthetic, ref.job_id)['status'] == 'finished'
    rows = history.fetch_activity(synthetic)
    routing = next(row for row in rows if row['kind'] == 'routing')
    assert routing['decision_source'] == 'deterministic'
    assert routing['plan_decision_id'] is None


@pytest.mark.parametrize('reason', ['service_absent', 'service_foreign', 'service_contained',
                                   'service_unreachable', 'secret_unavailable'])
def test_service_unavailable_runs_once_with_honest_note(synthetic, tmp_path, monkeypatch, reason, caplog):
    probes, executions = [], []
    original = mcp.run_bound_delegation
    def connect(database):
        probes.append(database)
        return service_client.Unavailable(reason)
    def run(*args, **kwargs):
        executions.append(kwargs['attempt_id'])
        return original(*args, **kwargs)
    monkeypatch.setattr(service_client, 'connect', connect)
    monkeypatch.setattr(mcp, 'run_bound_delegation', run)
    response = delegate(tmp_path)
    assert response['output'] == 'synthetic extracted answer'
    assert response['runner_unavailable_reason'] == reason
    explanation = ('is not running' if reason in {'service_absent', 'service_unreachable'}
                   else 'cannot keep work running after this session closes' if reason == 'service_contained'
                   else 'did not accept this task')
    ending = ("When this session's ModelQuad server process ends, Codex is stopped for this task; "
              "anything Windows started outside ModelQuad's control for it may keep running."
              if os.name == 'nt' else
              "Ending this session's ModelQuad server process may leave Codex running for this task.")
    assert response['runner_note'] == ('Ran inside this session: the ModelQuad background service '
                                      + explanation + ', so this task depends on this session. ' + ending)
    assert len(probes) == len(executions) == 1
    assert history.job_for_attempt(synthetic, executions[0]) is None
    assert history.read_job_attempt(synthetic, executions[0])['status'] == 'completed'
    assert 'private-install-secret-canary' not in json.dumps(response) + caplog.text


def test_runner_off_never_probes_service_or_adds_note(synthetic, tmp_path, monkeypatch):
    history.put_job_runner_enabled(synthetic, False)
    monkeypatch.setattr(service_client, 'connect', lambda *a: pytest.fail('runner off probed service'))
    response = delegate(tmp_path)
    assert response['output'] == 'synthetic extracted answer'
    assert 'runner_note' not in response and 'runner_unavailable_reason' not in response


def test_contained_caller_uses_service_without_caller_probe(synthetic, tmp_path, monkeypatch):
    monkeypatch.setattr(handle, '_caller_in_job', lambda: True)
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: 0x2000)
    monkeypatch.setattr(handle, '_launch_policy', lambda: pytest.fail('service handoff probed caller'))
    response = delegate(tmp_path)
    report_shortcut_response(response)
    assert response['job_status'] == 'finished'
    assert 'runner_note' not in response


@pytest.mark.parametrize('scenario,expected_executions,expected_state', [
    pytest.param('503_before', 1, None, id='503_before'),
    pytest.param('500_after', 0, 'failed', id='500_after'),
    pytest.param('timeout_before', 1, None, id='timeout_before'),
    pytest.param('timeout_after', 1, 'finished', id='timeout_after'),
    pytest.param('reset_before', 1, None, id='reset_before'),
    pytest.param('reset_after', 1, 'finished', id='reset_after'),
    pytest.param('refused_before', 0, 'failed', id='rate_limited'),
    pytest.param('unlink_failure', 1, None, id='unlink_failure'),
    pytest.param('release_raises', 0, 'starting', id='release_raises'),
    pytest.param('wait_raises', 1, 'finished', id='wait_raises'),
])
def test_service_handoff_ownership_and_exact_execution_count(
        synthetic, tmp_path, monkeypatch, caplog, scenario, expected_executions, expected_state):
    executions, launched, wait_ownership = [], [], []
    for module in (mcp, bound_launch):
        def count(*args, _shared=module.run_bound_delegation, **kwargs):
            executions.append((kwargs['attempt_id'], kwargs.get('publish') is not None))
            return _shared(*args, **kwargs)
        monkeypatch.setattr(module, 'run_bound_delegation', count)
    monkeypatch.setattr(mcp, 'JOB_WAIT_SECONDS', 0)
    original_status = mcp.job_status
    def observe_wait(*args, **kwargs):
        wait_ownership.append(mcp._launch_call.get()['job_owned'])
        if scenario == 'wait_raises':
            raise history.LaunchStateUnavailable('synthetic_wait_failed')
        return original_status(*args, **kwargs)
    monkeypatch.setattr(mcp, 'job_status', observe_wait)

    class Service:
        def launch(self, job_id, spec_name):
            assert mcp._launch_call.get()['job_owned'] is True
            path = synthetic.parent / spec_name
            row = history.read_job(synthetic, job_id)
            launched.append((job_id, path, row['attempt_id']))
            if scenario.endswith('_after') or scenario == 'wait_raises':
                # This test service shares pytest's process; never use the
                # native cleanup helper which might terminate the recorded pid.
                assert history.claim_job_launch(synthetic, job_id, attempt_id=row['attempt_id'],
                    decision_id=row['decision_id'], starter_pid=os.getpid(),
                    starter_created=procutil.process_creation_epoch(os.getpid()))
                if scenario == '500_after':
                    history.fail_job_attempt(synthetic, job_id, detail='launch_failed')
                    path.unlink()
                    return 'ambiguous', 'runner_launch_failed'
                assert runner.run_job(job_id, path, db_path=synthetic) == 0
            if scenario == '503_before':
                return 'ambiguous', 'launch_unavailable'
            if scenario.startswith('timeout'):
                return 'ambiguous', 'timeout'
            if scenario.startswith('reset'):
                return 'ambiguous', 'connection_error'
            if scenario == 'wait_raises':
                return 'launched', 'launched'
            if scenario in {'unlink_failure', 'release_raises'}:
                return 'ambiguous', 'timeout'
            return 'refused', 'launch_rate_limited'

    monkeypatch.setattr(service_client, 'connect', lambda *a: Service())
    if scenario == 'release_raises':
        def cannot_release(*args):
            raise OSError('private-release-error-canary')
        monkeypatch.setattr(history, 'release_unlaunched_job', cannot_release)
    if scenario == 'unlink_failure':
        original_unlink = Path.unlink
        def cannot_unlink(path, *args, **kwargs):
            if launched and path == launched[0][1]:
                raise PermissionError('private-unlink-error-canary')
            return original_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', cannot_unlink)
    if scenario == 'wait_raises':
        with pytest.raises(history.LaunchStateUnavailable, match='synthetic_wait_failed'):
            mcp._delegate_payload('review', 'quick', str(tmp_path), provider='codex', timeout=60)
        response = {}
    else:
        response = delegate(tmp_path)
    assert len(executions) == expected_executions
    assert len(launched) == 1
    job_id, path, attempt_id = launched[0]
    row = history.read_job(synthetic, job_id)
    attempt = history.read_job_attempt(synthetic, attempt_id)
    assert (row['status'] if row else None) == expected_state
    assert all(execution[0] == attempt_id for execution in executions)
    if expected_state is None:
        assert executions == [(attempt_id, False)]
        assert response['runner_note'] and response['runner_unavailable_reason']
        assert attempt['status'] == 'completed' and attempt['resolved_at'] is not None
        assert not wait_ownership
        assert not path.exists() or scenario == 'unlink_failure'
    elif scenario == 'refused_before':
        assert not wait_ownership and not path.exists()
        assert response['refused'] is True and response['executed'] is False
        assert response['reason'] == 'service_busy'
        assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'failed_to_start'
        assert row['ended_at'] is not None and attempt['resolved_at'] is not None
        assert attempt['detail'] == 'service_busy'
        assert 'runner_note' not in response
    else:
        assert wait_ownership == [True]
        assert 'runner_note' not in response
        if expected_state == 'starting':
            assert attempt['status'] == 'started' and attempt['resolved_at'] is None
            history.fail_job_attempt(synthetic, job_id, detail='test_cleanup')
            path.unlink()
        else:
            assert row['ended_at'] is not None and attempt['resolved_at'] is not None
            assert attempt['status'] == ('failed' if expected_state == 'failed' else 'completed')
    if scenario == 'unlink_failure':
        assert 'caller_spec_cleanup_unavailable' in caplog.text
    assert 'private-unlink-error-canary' not in caplog.text
    assert 'private-release-error-canary' not in caplog.text
    assert 'private-install-secret-canary' not in json.dumps(response) + caplog.text


@pytest.mark.parametrize('after_reservation', [False, True])
def test_service_reservation_failure_executes_nothing(synthetic, tmp_path, monkeypatch, after_reservation):
    monkeypatch.setattr(mcp, 'run_bound_delegation', lambda *a, **kw: pytest.fail('reservation failure executed'))
    if after_reservation:
        def fail_write(*args, **kwargs):
            raise OSError('private-write-error-canary')
        monkeypatch.setattr(handle, '_write_spec', fail_write)
    else:
        monkeypatch.setattr(handle, 'reserve_and_write', lambda *a, **kw:
                            handle.JobStart(None, False, 'open_attempt_unavailable'))
    response = delegate(tmp_path)
    assert response['refused'] is True and response['executed'] is False
    attempt = history.fetch_delegation_attempts(synthetic, limit=1)[0]
    assert attempt['resolved_at'] is not None
    assert attempt['status'] == ('failed' if after_reservation else 'refused')
    row = history.job_for_attempt(synthetic, attempt['id'])
    assert (row['status'] if row else None) == ('failed' if after_reservation else None)
    assert not list(tmp_path.glob('job-*.json'))


@pytest.mark.parametrize('state', ['missing', 'recovery', 'terminal'])
def test_failed_release_always_retains_job_ownership(synthetic, tmp_path, monkeypatch, state):
    captured, ownership = [], []
    monkeypatch.setattr(mcp, 'JOB_WAIT_SECONDS', 0)
    monkeypatch.setattr(mcp, 'run_bound_delegation', lambda *a, **kw: pytest.fail('failed release executed'))
    class Service:
        def launch(self, job_id, spec_name):
            row = history.read_job(synthetic, job_id)
            captured.append((job_id, row['attempt_id']))
            if state == 'terminal':
                history.fail_job_attempt(synthetic, job_id, detail='launch_failed')
            else:
                conn = db.connect_and_init(synthetic)
                if state == 'missing':
                    conn.execute('DELETE FROM jobs WHERE job_id=?', (job_id,))
                else:
                    conn.execute('UPDATE jobs SET recovery_claim=? WHERE job_id=?', ('test-recovery', job_id))
                conn.commit()
                conn.close()
            return 'ambiguous', 'connection_error'
    monkeypatch.setattr(service_client, 'connect', lambda *a: Service())
    read = mcp.job_status
    def observe(*args, **kwargs):
        ownership.append(mcp._launch_call.get()['job_owned'])
        return read(*args, **kwargs)
    monkeypatch.setattr(mcp, 'job_status', observe)
    response = delegate(tmp_path)
    assert ownership == [True] and 'runner_note' not in response
    job_id, attempt_id = captured[0]
    attempt = history.read_job_attempt(synthetic, attempt_id)
    assert attempt['status'] == ('failed' if state == 'terminal' else 'started')
    if state == 'missing':
        assert history.read_job(synthetic, job_id) is None
        history.resolve_delegation_attempt(synthetic, attempt_id, status='failed', detail='test_cleanup')
    else:
        assert history.read_job(synthetic, job_id)['status'] == ('failed' if state == 'terminal' else 'starting')
        if state == 'recovery':
            history.fail_job_attempt(synthetic, job_id, detail='test_cleanup')
    with sqlite3.connect(synthetic) as conn:
        assert conn.execute('SELECT COUNT(*) FROM plan_decisions').fetchone() == (0,)
