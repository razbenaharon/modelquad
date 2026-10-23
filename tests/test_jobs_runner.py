# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic, bounded runner acceptance; never starts a provider CLI."""

from orchestrator.execution import bound_launch

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import site
import sys
import time

import pytest

from load_helpers import retry_temp_permission


from orchestrator import paths, procutil
from orchestrator.jobs._test_sink import FileSink
from orchestrator.jobs.handle import JobSpec, job_status, start_job, stop_job
from orchestrator.jobs import handle, runner
from orchestrator.service import db, history
from test_jobs_changes import git, git_repo  # shared throwaway-repository fixture


@pytest.fixture(autouse=True)
def allow_test_sink(monkeypatch):
    """Installed-mode runner children opt in explicitly to synthetic output."""
    monkeypatch.setenv('MODELQUAD_TEST_SINK_OK', '1')
    # In-process unit fixtures measure a synthetic outside runner. Native
    # spawned runners do not inherit this patch and measure real containment.
    monkeypatch.setattr(runner, 'runner_containment_verdict', lambda: (True, 'not_in_job'))


PROCESS_SECONDS = 60


@pytest.mark.parametrize('ending', ['finished', 'failed', 'stopped', 'timed_out'])
def test_changed_files_collected_from_raw_worker(git_repo, tmp_path, monkeypatch, caplog, ending):
    from orchestrator.execution.base import WorkerTaskStatus
    from orchestrator.jobs.store import FileOutputStore
    from dataclasses import replace
    caplog.set_level('DEBUG')
    spec = replace(spec_for(tmp_path, 'pass'), cwd=str(git_repo))
    row = history.reserve_job(spec.db_path, 'changes-' + ending, spec.attempt_id)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)), encoding='utf-8')
    store = FileOutputStore(tmp_path / 'pending')
    monkeypatch.setattr(runner, 'output_sink_factory', lambda name: store)

    def worker(*args, **kwargs):
        assert kwargs['cwd'] == str(git_repo)
        (git_repo / 'worker-change.txt').write_text('private content canary', encoding='utf-8')
        if ending == 'stopped':
            raise procutil.WorkerStopRequested([], stdout='')
        if ending == 'timed_out':
            raise subprocess.TimeoutExpired([], 1, output='')
        if ending == 'failed':
            raise OSError(f'private exception canary {git_repo / "private.txt"}')
        return subprocess.CompletedProcess([], 0, '```json\n{"task_status":"completed","reason":"fixture"}\n```', '')

    monkeypatch.setattr(procutil, 'run_with_tree_kill', worker)
    assert runner.run_job(row['job_id'], path) == 0
    collected = store.collect(row['job_id'], datetime.now(timezone.utc))
    assert collected.output.changed_files is None
    assert collected.output.job_status.value == ending
    assert collected.output.worker_claim is (WorkerTaskStatus.COMPLETED if ending == 'finished'
                                            else WorkerTaskStatus.UNKNOWN)
    assert collected.output.claim_reason == {'finished': 'fixture', 'failed': 'launch_failed',
                                             'stopped': 'stop_requested', 'timed_out': None}[ending]
    assert collected.output.partial is False
    conn = db.connect(spec.db_path)
    try:
        persisted = repr([tuple(r) for r in conn.execute('SELECT * FROM delegation_attempts')])
        persisted += repr([tuple(r) for r in conn.execute('SELECT * FROM jobs')])
        for forbidden in ('worker-change.txt', 'private content canary', 'private exception canary'):
            assert forbidden not in persisted
            assert forbidden not in caplog.text
        assert str(git_repo) not in caplog.text
    finally:
        conn.close()


def test_raw_interrupted_output_carries_observation(git_repo, tmp_path, monkeypatch):
    from dataclasses import replace
    from orchestrator.execution.base import WorkerTaskStatus
    from orchestrator.jobs.store import FileOutputStore
    from process_diagnostics import capture_text
    # This in-process publication test does not measure the startup deadline.
    monkeypatch.setattr(runner, 'STARTUP_SECONDS', PROCESS_SECONDS)
    spec = replace(spec_for(tmp_path, 'pass', timeout=PROCESS_SECONDS), cwd=str(git_repo))
    row = history.reserve_job(spec.db_path, 'raw-interrupted', spec.attempt_id)
    path = tmp_path / 'spec.json'
    retry_temp_permission(lambda: path.write_text(json.dumps(asdict(spec)), encoding='utf-8'))
    store = FileOutputStore(tmp_path / 'pending')
    monkeypatch.setattr(runner, 'output_sink_factory', lambda name: store)

    def worker(*args, **kwargs):
        (git_repo / 'interrupted-change.txt').write_text('partial edit', encoding='utf-8')
        return subprocess.CompletedProcess([], 0, '', '')

    monkeypatch.setattr(procutil, 'run_with_tree_kill', worker)
    # Supply a synthetic orphaned resolution at the raw publication seam;
    # execution and publication still run through the raw path.
    monkeypatch.setattr(runner, 'attempt_resolution', lambda result: ('failed', 'orphaned'))
    result = runner.run_job(row['job_id'], path)
    assert result == 0, {
        'job': history.read_job(spec.db_path, row['job_id']),
        'attempt': history.read_job_attempt(spec.db_path, spec.attempt_id),
        'runner_log': capture_text(paths.logs_dir() / 'job-runner.log'),
    }
    output = store.collect(row['job_id'], datetime.now(timezone.utc)).output
    assert output.job_status.value == 'interrupted'
    assert output.worker_claim is WorkerTaskStatus.UNKNOWN
    assert output.claim_reason == 'interrupted'
    assert output.changed_files is None
    assert output.partial is False


def test_raw_worker_commits_previously_clean_file(git_repo, tmp_path, monkeypatch):
    from orchestrator.jobs.store import FileOutputStore
    from dataclasses import replace
    spec = replace(spec_for(tmp_path, 'pass'), cwd=str(git_repo))
    row = history.reserve_job(spec.db_path, 'committed-changes', spec.attempt_id)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)), encoding='utf-8')
    store = FileOutputStore(tmp_path / 'pending')
    monkeypatch.setattr(runner, 'output_sink_factory', lambda name: store)
    def worker(*args, **kwargs):
        (git_repo / 'modified.txt').write_text('committed', encoding='utf-8')
        git(git_repo, 'add', '.')
        git(git_repo, 'commit', '-m', 'synthetic worker')
        return subprocess.CompletedProcess([], 0, '', '')
    monkeypatch.setattr(procutil, 'run_with_tree_kill', worker)
    assert runner.run_job(row['job_id'], path) == 0
    assert store.collect(row['job_id'], datetime.now(timezone.utc)).output.changed_files is None


@pytest.mark.parametrize('ending', ['finished', 'failed', 'stopped', 'timed_out', 'interrupted'])
@pytest.mark.parametrize('task_type', ['implementation', 'review'])
def test_delegation_changed_files_status_matrix(git_repo, tmp_path, monkeypatch, ending, task_type):
    from dataclasses import fields, replace
    from types import SimpleNamespace
    from orchestrator.config import Config
    from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus
    from orchestrator.execution.lifecycle import TerminalResolution
    from orchestrator.jobs.handle import PreparedDelegation
    from orchestrator.jobs.seam import JobRef
    from orchestrator.jobs.store import FileOutputStore
    from orchestrator.execution import manager as manager_module
    from orchestrator import config as config_module
    from orchestrator.mcp import server as mcp
    base = spec_for(tmp_path, 'pass')
    # Intentionally different from spec.cwd: execute in the prepared folder.
    # All execution/provider seams are synthetic.
    values = {field.name: None for field in fields(PreparedDelegation)}
    values.update(task_type=task_type, prompt='fixture', working_directory=str(git_repo),
                  provider='codex', statuses=[], consumption={'bucket': 'low'}, timeout=5,
                  decision_id='test-decision', attempt_id=base.attempt_id, db_path=base.db_path,
                  config_path=str(tmp_path / 'no-config'), user_approved=False,
                  mode='normal', paused=False, usage_directory=str(tmp_path), host_snapshot={},
                  lineage={'decision_id': 'test-decision', 'decision_id_source': 'caller_supplied',
                           'prior_route': True, 'notes': []}, decision_mode='deterministic',
                  reasoning_fell_back=False, capability='delegate')
    spec = replace(base, delegation=PreparedDelegation(**values))
    store = FileOutputStore(tmp_path / 'pending')
    monkeypatch.setattr(runner, 'output_sink_factory', lambda name: store)
    monkeypatch.setattr(config_module, 'load_config', lambda path: Config())
    monkeypatch.setattr(manager_module, 'ExecutionManager', lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(bound_launch, '_bind_launch', lambda *a, **kw: None)
    result = ExecutionResult('codex', ending == 'finished', '', 0 if ending == 'finished' else None,
                             metadata={'stop_requested': ending == 'stopped',
                                       'timed_out': ending == 'timed_out'},
                             task_status=WorkerTaskStatus.COMPLETED)
    resolution = TerminalResolution('completed', None) if ending == 'finished' else TerminalResolution(
        'failed', {'failed': 'exception', 'stopped': 'timeout', 'timed_out': 'timeout',
                   'interrupted': 'orphaned'}[ending])
    def execute(*args, **kwargs):
        assert kwargs['working_directory'] == str(git_repo)
        (git_repo / 'delegated-change.txt').write_text('private', encoding='utf-8')
        kwargs['publish'](result, resolution)
    monkeypatch.setattr(bound_launch, 'run_bound_delegation', execute)
    ref = JobRef('delegated-' + ending, base.attempt_id, 'test-decision')
    assert runner._run_delegation(spec, ref, datetime.now(timezone.utc), {}) == 0
    output = store.collect(ref.job_id, datetime.now(timezone.utc)).output
    assert output.job_status.value == ending
    assert output.changed_files is None
    assert output.partial is False
    if ending == 'interrupted':
        assert output.worker_claim is WorkerTaskStatus.UNKNOWN
        assert output.claim_reason == 'interrupted'
    assert 'changed_files' not in result.metadata
    assert '_job_changed_files' not in asdict(result)


def probe_interpreter(python, *, env=None, cwd=None, evidence="child interpreter"):
    """Prove the exact child environment can import the product dependencies."""
    try:
        result = subprocess.run([str(python), "-c", "import orchestrator.jobs.handle"],
                                env=env, cwd=cwd, capture_output=True, text=True,
                                timeout=PROCESS_SECONDS, **procutil.no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"{evidence}; import probe unavailable: {error}; stderr={getattr(error, 'stderr', None)!r}")
    if result.returncode:
        pytest.skip(f"{evidence}; import probe failed (exit {result.returncode}): {result.stderr}")


def wait_for(read, predicate, seconds=PROCESS_SECONDS):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = read()
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f'bounded wait expired: {last}')


def product_import_paths():
    """Keep child imports in the same source/installed mode as this test."""
    source = Path(__file__).resolve().parents[1] / 'src'
    return ([str(source)] if Path(handle.__file__).resolve().is_relative_to(source) else []) + site.getsitepackages()


def spec_for(tmp_path, code, timeout=5):
    child_env = {'PYTHONPATH': os.pathsep.join(product_import_paths())}
    probe_interpreter(sys.executable, env=dict(os.environ, **child_env), cwd=tmp_path)
    path = tmp_path / 'jobs.sqlite'
    attempt = history.record_delegation_attempt(path, task_type='implementation', decision_id='test-decision')
    return JobSpec((sys.executable, '-c', code), str(tmp_path), str(path), attempt, timeout,
                   env=child_env,
                   sink='file:' + str(tmp_path / 'sink'))


def cleanup(spec, ref):
    if ref is None:
        return
    stop_job(ref.job_id, db_path=spec.db_path)
    row = history.read_job(spec.db_path, ref.job_id)
    if row and row['runner_pid'] and row['runner_created'] == procutil.process_creation_epoch(row['runner_pid']):
        procutil.terminate_pid(row['runner_pid'])


native = pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object acceptance')


def require_launch_environment():
    """Probe outer containment independently before asserting starter behavior.

    Some nested sandbox jobs advertise breakaway on the innermost job but
    retain the child in an outer job. Such an environment cannot run escape
    acceptance. Non-killing contained launches remain testable.
    """
    probe_interpreter(sys.executable)
    in_job = handle._caller_in_job()
    limits = handle._caller_job_limits() if in_job else 0
    flags, expect_outside, _ = handle._launch_policy()
    reason = f'outer test-harness Job Object (limit flags 0x{limits:x})'
    if flags is None:
        pytest.skip(f'{reason} forbids breakaway and kills on close')
    if not expect_outside:
        return
    child = None
    try:
        kwargs = procutil.no_window_kwargs()
        kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | 0x4 | flags
        try:
            child = subprocess.Popen([handle._runner_executable(), '-c', 'pass'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     close_fds=True, **kwargs)
        except OSError as error:
            if error.winerror == 5:
                pytest.skip(f'{reason} denies requested breakaway')
            raise
        if handle._process_in_any_job(child._handle):
            pytest.skip(f'{reason} prevents escape to outside every job (nested containment)')
    finally:
        if child is not None:
            child.kill()
            _, error = child.communicate(timeout=PROCESS_SECONDS)
            assert not error, (reason, error.decode(errors='replace'))


@native
def test_running_finished_output_and_content_free_record(tmp_path, monkeypatch):
    from process_diagnostics import capture_text, instrument_runner_command, note_failure, process_snapshot

    require_launch_environment()
    spec = spec_for(tmp_path, "import time; time.sleep(.7); print('synthetic answer')")
    stages_path = tmp_path / 'runner-stages.jsonl'
    original_command = handle._runner_command
    monkeypatch.setattr(handle, '_runner_command', lambda *args: instrument_runner_command(
        original_command(*args), stages_path))
    # launch_reserved still owns suspension, identity registration, admission,
    # resumption and stderr capture. Only the Python entry gains diagnostics.
    caller_in_job = handle._caller_in_job()
    containment = {'in_job': caller_in_job,
                   'limits': handle._caller_job_limits() if caller_in_job else 0}
    start = start_job(spec)
    try:
        assert start.launched, start
        running = wait_for(lambda: job_status(start.ref.job_id, db_path=spec.db_path),
                           lambda r: r['status'] == 'running')
        assert running['runner_liveness'] == 'alive'
        assert running['worker_pid'] and running['heartbeat_at']
        final = wait_for(lambda: job_status(start.ref.job_id, db_path=spec.db_path),
                         lambda r: r['ended_at'] is not None)
        assert final['status'] == 'finished'
        output = json.loads((tmp_path / 'sink' / (start.ref.job_id + '.json')).read_text())
        assert output['text'] == 'synthetic answer\n'
        assert output['claim'] == 'unknown'
        assert 'synthetic answer' not in json.dumps(final)
        assert not list(tmp_path.glob('job-*.json'))
        conn = db.connect(spec.db_path)
        try:
            assert conn.execute('SELECT status, outcome FROM delegation_attempts').fetchone()[:] == ('completed', 'unknown')
            assert {r[1] for r in conn.execute('PRAGMA table_info(jobs)')} == {
                'job_id','attempt_id','decision_id','runner_pid','runner_created','worker_pid',
                'status','created_at','updated_at','heartbeat_at','ended_at','launch_limitation',
                'worker_created','recovery_claim','recovery_claimed_at','recovery_ended_at','recovery_decision',
                # v24 (F1 fate release): content-free boot evidence for the reservation.
                'reservation_boot_id','reservation_uptime_ms','reservation_boot_qualified',
            }
        finally:
            conn.close()
    except BaseException as error:
        def diagnostics():
            current = history.read_job(spec.db_path, start.ref.job_id) if start.ref else None
            attempt = history.read_job_attempt(spec.db_path, spec.attempt_id)
            pid = (current or {}).get('runner_pid')
            return {
                'caller_containment': containment, 'job': current,
                'attempt': {key: (attempt or {}).get(key) for key in
                            ('status', 'failure_kind', 'detail', 'created_at', 'resolved_at')},
                'processes': process_snapshot(pid) if pid else [],
                'stages': capture_text(stages_path),
                'stderr': capture_text(paths.logs_dir() / 'job-runner.log'),
            }
        note_failure(error, diagnostics)
        raise
    finally:
        cleanup(spec, start.ref)


@native
@pytest.mark.parametrize('cause', ['stop', 'timeout', 'runner_death'])
def test_worker_and_child_tree_end(tmp_path, cause):
    require_launch_environment()
    child_file = tmp_path / 'child.pid'
    child_stderr = tmp_path / 'child.stderr'
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "from orchestrator.procutil import no_window_kwargs; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)'], "
        f"stdout=subprocess.DEVNULL,stderr=open({str(child_stderr)!r}, 'wb'),**no_window_kwargs()); "
        f"Path({str(child_file)!r}).write_text(str(p.pid)); print('partial',flush=True); time.sleep(15)"
    )
    spec = spec_for(tmp_path, code, timeout=1.5 if cause == 'timeout' else 6)
    start = start_job(spec)
    child = None
    try:
        assert start.launched, start
        wait_for(lambda: child_file.exists(), bool)
        child = int(child_file.read_text())
        row = wait_for(lambda: job_status(start.ref.job_id, db_path=spec.db_path), lambda r: r['worker_pid'])
        if cause == 'stop':
            assert stop_job(start.ref.job_id, db_path=spec.db_path) == 'accepted'
        elif cause == 'runner_death':
            assert procutil.terminate_pid(row['runner_pid'])
        if cause != 'runner_death':
            row = wait_for(lambda: job_status(start.ref.job_id, db_path=spec.db_path), lambda r: r['ended_at'])
            assert row['status'] == ('stopped' if cause == 'stop' else 'timed_out')
            output = json.loads((tmp_path / 'sink' / (start.ref.job_id + '.json')).read_text())
            assert output['death'] is True
        else:
            # Recovery is deferred: observing runner death does not invent an
            # interrupted final record in this slice.
            assert row['ended_at'] is None
        wait_for(lambda: procutil.pid_state(child), lambda s: s == 'dead', seconds=PROCESS_SECONDS)
        wait_for(lambda: procutil.pid_state(row['worker_pid']), lambda s: s == 'dead', seconds=PROCESS_SECONDS)
    except AssertionError as error:
        diagnostics = child_stderr.read_text(errors="replace") if child_stderr.exists() else "not created"
        raise AssertionError(f"{error}; child stderr: {diagnostics}") from error
    finally:
        cleanup(spec, start.ref)
        if child is not None:
            procutil.terminate_pid(child)


@pytest.mark.parametrize('worker_started', [False, True])
def test_runner_crash_closes_records_without_content(tmp_path, monkeypatch, worker_started):
    from orchestrator import paths
    spec = spec_for(tmp_path, 'private-prompt-canary')
    row = history.reserve_job(spec.db_path, 'crashed', spec.attempt_id)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)))
    log_dir = tmp_path / 'logs'
    monkeypatch.setattr(paths, 'logs_dir', lambda: log_dir)

    def execute(*args):
        if worker_started:
            history.update_job(spec.db_path, row['job_id'], worker_pid=123, status='running')
        raise OSError('private-output-canary C:/private/path')

    monkeypatch.setattr(runner, '_execute', execute)
    assert runner.run_job(row['job_id'], path) == 1
    assert not path.exists()
    final = history.read_job(spec.db_path, row['job_id'])
    assert final['status'] == ('unknown' if worker_started else 'failed')
    assert bool(final['ended_at']) is (not worker_started)
    conn = db.connect(spec.db_path)
    try:
        attempt = conn.execute('SELECT * FROM delegation_attempts').fetchone()
        assert attempt['status'] == 'failed' and attempt['resolved_at']
        assert attempt['failure_kind'] == ('exception' if worker_started else 'failed_to_start')
        assert attempt['detail'] == ('runner_crashed; worker fate unknown' if worker_started else 'runner_pre_start_failed')
    finally:
        conn.close()
    timestamp, job_id, error_type = (log_dir / 'job-runner.log').read_text().split()
    assert datetime.fromisoformat(timestamp).tzinfo is not None
    assert job_id == row['job_id'] and error_type == 'OSError'


@pytest.mark.parametrize('failure', ['sink', 'resolution'])
def test_late_failure_keeps_known_worker_fate(tmp_path, monkeypatch, failure):
    from orchestrator.execution.base import ExecutionResult, describe_worker_termination
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'sink-failure', spec.attempt_id)
    history.update_job(spec.db_path, row['job_id'], worker_pid=123, status='running')
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)))
    metadata = {'worker_termination': {'pid': 123, 'confirmed_dead': True,
                                      'tree_confirmed_dead': True, 'tree_kill': 'job'}}
    monkeypatch.setattr(runner, '_execute', lambda *a: ExecutionResult('unknown', True, 'answer', 0,
                                                                     metadata=metadata))
    if failure == 'sink':
        class BrokenSink:
            def put(self, *args):
                raise OSError('private-sink-canary')
        monkeypatch.setattr(runner, 'output_sink_factory', lambda *a: BrokenSink())
    else:
        resolve = history.resolve_delegation_attempt
        calls = []
        def refuse_once(*args, **kwargs):
            calls.append(kwargs)
            return False if len(calls) == 1 else resolve(*args, **kwargs)
        monkeypatch.setattr(history, 'resolve_delegation_attempt', refuse_once)
    assert runner.run_job(row['job_id'], path) == 1
    assert not path.exists()
    final = history.read_job(spec.db_path, row['job_id'])
    assert final['status'] == 'unknown' and final['ended_at'] is None
    conn = db.connect(spec.db_path)
    try:
        attempt = conn.execute('SELECT * FROM delegation_attempts').fetchone()
        if failure == 'sink':
            assert attempt['status'] == 'failed' and attempt['resolved_at']
            assert attempt['failure_kind'] == 'exception'
            assert attempt['detail'] == 'runner_crashed; ' + describe_worker_termination(metadata)
        else:
            assert attempt['status'] == 'started' and attempt['resolved_at'] is None
            assert len(calls) == 1  # A failed write is not a second outcome.
    finally:
        conn.close()


def test_final_job_write_failure_preserves_completed_attempt(tmp_path, monkeypatch):
    from orchestrator.execution.base import ExecutionResult
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'final-write', spec.attempt_id)
    history.update_job(spec.db_path, row['job_id'], worker_pid=123, status='running')
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)))
    monkeypatch.setattr(runner, '_execute', lambda *a: ExecutionResult('unknown', True, '', 0))
    monkeypatch.setattr(paths, 'logs_dir', lambda: tmp_path / 'logs')
    update = history.update_job
    calls = []
    def fail_final(*args, **kwargs):
        calls.append(kwargs)
        if 'ended_at' in kwargs:
            raise OSError('private-write-canary')
        return update(*args, **kwargs)
    monkeypatch.setattr(history, 'update_job', fail_final)
    assert runner.run_job(row['job_id'], path) == 1
    assert not path.exists()
    final = history.read_job(spec.db_path, row['job_id'])
    assert final['status'] == 'unknown' and final['ended_at'] is None
    assert not any(call.get('status') == 'failed' for call in calls)
    conn = db.connect(spec.db_path)
    try:
        attempt = conn.execute('SELECT * FROM delegation_attempts').fetchone()
        assert attempt['status'] == 'completed' and attempt['resolved_at']
    finally:
        conn.close()
    assert (tmp_path / 'logs' / 'job-runner.log').read_text().split()[1:] == ['final-write', 'OSError']


@native
def test_normal_exit_snapshots_before_close_cleanup(tmp_path):
    child_file = tmp_path / 'normal-child.pid'
    child_stderr = tmp_path / 'normal-child.stderr'
    code = (
        "import subprocess,sys; from pathlib import Path; "
        "from orchestrator.procutil import no_window_kwargs; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'], "
        f"stdout=subprocess.DEVNULL,stderr=open({str(child_stderr)!r}, 'wb'),**no_window_kwargs()); "
        f"Path({str(child_file)!r}).write_text(str(p.pid))"
    )
    child = None
    probe_interpreter(sys.executable)
    try:
        result = procutil.run_with_tree_kill(
            [sys.executable, '-c', code], capture_output=True, timeout=PROCESS_SECONDS,
            kill_on_close=True,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        child = int(child_file.read_text())
        assert child in [item['pid'] for item in result.worker_descendants['descendants']]
        assert result.worker_termination['close_cleanup_members'] >= 1
        assert result.worker_termination['close_cleanup_confirmed'] is True
        assert procutil.pid_state(child) == 'dead'
    except AssertionError as error:
        diagnostics = child_stderr.read_text(errors="replace") if child_stderr.exists() else "not created"
        raise AssertionError(f"{error}; child stderr: {diagnostics}") from error
    finally:
        if child is not None and procutil.pid_state(child) != 'dead':
            procutil.terminate_pid(child)


def test_stop_before_launch_and_structured_claim_are_separate(tmp_path, monkeypatch):
    from orchestrator.jobs.seam import JobRef, cause_flags_from_result, job_status_from_resolution
    spec = spec_for(tmp_path, 'pass')
    ref = JobRef('pre-stopped', spec.attempt_id, 'test-decision')
    assert history.request_attempt_stop(spec.db_path, spec.attempt_id) == 'accepted'
    with monkeypatch.context() as observation:
        observation.setattr(runner, 'snapshot', lambda *a: pytest.fail('must not snapshot before launch'))
        observation.setattr(runner, 'changed_files', lambda *a: pytest.fail('must not observe before launch'))
        observation.setattr(procutil, 'run_with_tree_kill', lambda *a, **kw: pytest.fail('must not start worker'))
        result = runner._execute(spec, ref)
    assert getattr(result, '_job_changed_files', None) is None
    stop, deadline = cause_flags_from_result(result)
    assert job_status_from_resolution(result.terminal_resolution, stop_requested=stop,
                                      deadline_passed=deadline).value == 'stopped'
    assert result.task_status.value == 'unknown'
    history.resolve_delegation_attempt(spec.db_path, spec.attempt_id, status='failed')
    other = spec_for(tmp_path, 'pass')
    ref = JobRef('structured', other.attempt_id, 'test-decision')
    completed = subprocess.CompletedProcess([], 0, '```json\n{"task_status":"failed","reason":"synthetic"}\n```', '')
    monkeypatch.setattr(procutil, 'run_with_tree_kill', lambda *a, **kw: completed)
    result = runner._execute(other, ref)
    assert result.task_status.value == 'failed'
    assert result.terminal_resolution.status == 'completed'


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('stage', ['pre_start', 'execute', 'sink'])
def test_base_exception_records_failure_then_reraises(tmp_path, monkeypatch, error_type, stage):
    from orchestrator.execution.base import ExecutionResult
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'interrupted', spec.attempt_id)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)))

    def interrupt(*args, **kwargs):
        raise error_type('private-content-canary')

    if stage == 'pre_start':
        monkeypatch.setattr(history, 'require_launch_attempt', interrupt)
    else:
        history.update_job(spec.db_path, row['job_id'], worker_pid=123, status='running')
        monkeypatch.setattr(runner, '_execute', interrupt if stage == 'execute' else
                            lambda *a: ExecutionResult('unknown', True, '', 0))
        if stage == 'sink':
            monkeypatch.setattr(runner, 'output_sink_factory', interrupt)
    monkeypatch.setattr(paths, 'logs_dir', lambda: tmp_path / 'logs')
    with pytest.raises(error_type, match='private-content-canary'):
        runner.run_job(row['job_id'], path)
    assert not path.exists()
    final = history.read_job(spec.db_path, row['job_id'])
    assert final['status'] == ('failed' if stage == 'pre_start' else 'unknown')
    assert bool(final['ended_at']) is (stage == 'pre_start')
    conn = db.connect(spec.db_path)
    try:
        attempt = conn.execute('SELECT * FROM delegation_attempts').fetchone()
        assert attempt['status'] == 'failed' and attempt['resolved_at']
        assert attempt['failure_kind'] == ('failed_to_start' if stage == 'pre_start' else 'exception')
    finally:
        conn.close()
    assert (tmp_path / 'logs' / 'job-runner.log').read_text().split()[1:] == ['interrupted', error_type.__name__]


def test_execution_exception_is_failed_not_finished(tmp_path, monkeypatch):
    from orchestrator.jobs.seam import JobRef
    spec = spec_for(tmp_path, 'pass')
    monkeypatch.setattr(procutil, 'run_with_tree_kill', lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    result = runner._execute(spec, JobRef('exception', spec.attempt_id, None))
    assert not result.success
    assert result.terminal_resolution.status == 'failed'
    assert result.task_status.value == 'unknown'


@pytest.mark.parametrize('failure', ['identity', 'reservation', 'attempt', 'invalid_spec', 'wrong_attempt',
                                   'malformed_spec', 'missing_spec'])
def test_pre_start_failures_close_job_and_attempt(tmp_path, monkeypatch, failure):
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'pre-start', spec.attempt_id)
    history.update_job(spec.db_path, row['job_id'], runner_pid=os.getpid(),
                       runner_created=procutil.process_creation_epoch(os.getpid()))
    raw = asdict(spec)
    if failure == 'identity':
        monkeypatch.setattr(procutil, 'process_creation_epoch', lambda pid: None)
    elif failure == 'reservation':
        conn = db.connect(spec.db_path)
        conn.execute('DELETE FROM jobs')
        conn.commit()
        conn.close()
    elif failure == 'attempt':
        monkeypatch.setattr(history, 'require_launch_attempt',
                            lambda *a: (_ for _ in ()).throw(history.LaunchStateUnavailable('synthetic')))
    elif failure == 'invalid_spec':
        raw['timeout'] = -1
    elif failure == 'wrong_attempt':
        raw['attempt_id'] += 99
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(raw))
    if failure == 'malformed_spec':
        path.write_text('{')
    elif failure == 'missing_spec':
        path.unlink()
    monkeypatch.setattr(runner, '_execute', lambda *a: pytest.fail('must not execute'))
    assert runner.run_job(row['job_id'], path, db_path=spec.db_path) == 1
    assert not path.exists()
    final = history.read_job(spec.db_path, row['job_id'])
    if failure != 'reservation':
        assert final['status'] == 'failed' and final['ended_at']
    conn = db.connect(spec.db_path)
    try:
        attempt = conn.execute('SELECT status,resolved_at,failure_kind,detail FROM delegation_attempts WHERE id=?',
                               (spec.attempt_id,)).fetchone()
        assert attempt['status'] == 'failed' and attempt['resolved_at']
        assert attempt['failure_kind'] == 'failed_to_start'
        assert attempt['detail'] == 'runner_pre_start_failed'
    finally:
        conn.close()


@pytest.mark.parametrize('case', ['success', 'nonzero', 'exception', 'stop', 'timeout'])
def test_resolution_detail_keeps_reason_and_worker_fate(tmp_path, monkeypatch, case):
    from orchestrator.execution.base import ExecutionResult, describe_worker_termination
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'detail', spec.attempt_id)
    history.update_job(spec.db_path, row['job_id'], runner_pid=os.getpid(),
                       runner_created=procutil.process_creation_epoch(os.getpid()))
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)))
    reason = {'success': 'done', 'nonzero': 'synthetic_failure', 'exception': 'launch_failed',
              'stop': 'stop_requested', 'timeout': None, 'interrupted': 'interrupted'}[case]
    metadata = {'worker_termination': {'pid': 123, 'confirmed_dead': True,
                                      'tree_confirmed_dead': True, 'tree_kill': 'job'}}
    if case == 'stop':
        metadata['stop_requested'] = True
    if case == 'timeout':
        metadata['timed_out'] = True
    if case in ('exception', 'interrupted'):
        metadata = {}
    result = ExecutionResult('unknown', case == 'success', '', 0 if case == 'success' else 1,
                             metadata=metadata, task_status_reason=reason)

    def execute(*args):
        if case == 'interrupted':
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(runner, '_execute', execute)
    assert runner.run_job(row['job_id'], path) == 0
    fate = describe_worker_termination(metadata)
    expected = (reason + '; ' if reason and fate else reason or '') + fate
    conn = db.connect(spec.db_path)
    try:
        assert conn.execute('SELECT detail FROM delegation_attempts').fetchone()[0] == (expected or None)
    finally:
        conn.close()


@pytest.mark.parametrize('dev,opt_in,allowed', [
    (True, None, True),
    (False, '1', True),
    (False, None, False),
    (False, '0', False),
])
def test_file_sink_guard(tmp_path, monkeypatch, dev, opt_in, allowed):
    monkeypatch.setattr(paths, 'is_dev_checkout', lambda: dev)
    monkeypatch.delenv('MODELQUAD_TEST_SINK_OK', raising=False)
    if opt_in is not None:
        monkeypatch.setenv('MODELQUAD_TEST_SINK_OK', opt_in)
    directory = tmp_path / 'sink'
    if allowed:
        assert isinstance(runner.output_sink_factory('file:' + str(directory)), FileSink)
    else:
        with pytest.raises(ValueError, match='test output sink unavailable'):
            runner.output_sink_factory('file:' + str(directory))
    assert not directory.exists()


@pytest.mark.parametrize('worker_started', [False, True])
def test_crash_handler_read_failure_still_resolves(tmp_path, monkeypatch, worker_started):
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'handler-read', spec.attempt_id)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)))
    read_job = history.read_job
    handler_reads = []
    crashed = False
    def execute(*args):
        nonlocal crashed
        if worker_started:
            history.record_worker_pid(spec.db_path, spec.attempt_id, 123)
            history.update_job(spec.db_path, row['job_id'], worker_pid=123, status='running')
        crashed = True
        raise RuntimeError('private-execute-canary')
    def fail_first_handler_read(*args):
        if crashed:
            handler_reads.append(1)
            if len(handler_reads) == 1:
                raise OSError('private-read-canary')
        return read_job(*args)
    monkeypatch.setattr(runner, '_execute', execute)
    monkeypatch.setattr(history, 'read_job', fail_first_handler_read)
    assert runner.run_job(row['job_id'], path, db_path=spec.db_path) == 1
    attempt = history.read_job_attempt(spec.db_path, spec.attempt_id)
    assert attempt['status'] == 'failed' and attempt['resolved_at']
    assert attempt['failure_kind'] == ('exception' if worker_started else 'failed_to_start')
    final = read_job(spec.db_path, row['job_id'])
    assert final['status'] == ('unknown' if worker_started else 'failed')
    assert bool(final['ended_at']) is (not worker_started)
    assert not path.exists()


@pytest.mark.parametrize('opt_in', [None, '0'], ids=['installed-no-opt-in', 'installed-opt-out'])
def test_raw_argv_refused_outside_synthetic_runs(tmp_path, monkeypatch, opt_in):
    """F-1008-2: an installed package never launches a raw-argv spec."""
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'raw-argv-' + str(opt_in), spec.attempt_id)
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(asdict(spec)))
    monkeypatch.setattr(paths, 'is_dev_checkout', lambda: False)
    monkeypatch.delenv('MODELQUAD_TEST_SINK_OK', raising=False)
    if opt_in is not None:
        monkeypatch.setenv('MODELQUAD_TEST_SINK_OK', opt_in)
    executed = []
    monkeypatch.setattr(runner, '_execute', lambda *args: executed.append(args))
    assert runner.run_job(row['job_id'], path) == 1
    assert executed == []
    assert not path.exists()
    final = history.read_job(spec.db_path, row['job_id'])
    assert final['status'] == 'failed'
    assert final['ended_at']
