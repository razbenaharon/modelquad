# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic launch reservation, exact identity and controlled breakaway tests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import site
import tempfile
import subprocess
import sys

import pytest

from load_helpers import retry_temp_permission
from orchestrator import procutil
from orchestrator.jobs import handle
from orchestrator.service import db, history
from test_jobs_runner import allow_test_sink, cleanup, native, require_launch_environment, spec_for, wait_for, PROCESS_SECONDS, probe_interpreter, product_import_paths


JOB_FLAGS = {'explicit': 0x2800, 'silent': 0x3000, 'forbidden': 0x2000, 'contained': 0}


def observed_policy(in_job, limits):
    """Test oracle from observed limits, independent of the product policy."""
    if not in_job:
        return 0, False, None
    if limits & 0x1000:
        return 0, True, None
    if limits & 0x800:
        return 0x01000000, True, None
    if limits & 0x2000:
        return None, False, 'breakaway_not_permitted'
    return 0, False, 'contained_innermost_job_without_kill_on_close'


def verify_remote_job(pid, remote_job, evidence):
    """Borrow the venv caller's test-owned job for a parent-side membership check."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                        wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(0x40, False, pid)  # PROCESS_DUP_HANDLE
    job = wintypes.HANDLE()
    try:
        if not process or not kernel32.DuplicateHandle(process, remote_job, kernel32.GetCurrentProcess(),
                                                     ctypes.byref(job), 0, False, 2):
            pytest.skip(f'{evidence}; cannot inspect the venv caller test-owned job')
        if procutil._process_in_job(pid, job.value) is not True:
            pytest.skip(f'{evidence}; venv caller membership in controlled job not verified')
    finally:
        if job.value:
            kernel32.CloseHandle(job)
        if process:
            kernel32.CloseHandle(process)


def process_in_any_job(pid):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        raise OSError('test process containment unavailable')
    try:
        return handle._process_in_any_job(process)
    finally:
        kernel32.CloseHandle(process)


@contextmanager
def verified_parent(tmp_path, code, jobs, *, escape=False):
    """Check the actual interpreter's memberships and limits before launch.

    Use the base interpreter for controlled topology: a venv redirector can
    create another innermost job after we assign its suspended stub. The real
    venv test below deliberately retains that separate launcher behavior.
    """
    in_job = handle._caller_in_job()
    limits = handle._caller_job_limits() if in_job else 0
    description = f'outer test-harness Job Object (in_job={in_job}, limit flags 0x{limits:x})'
    flags, _, limitation = observed_policy(in_job, limits)
    if escape and in_job and (flags is None or limitation):
        pytest.skip(f'{description}; cannot establish a parent outside every job')
    state_path = tmp_path / 'parent-state.json'
    proceed = tmp_path / 'parent-proceed'
    preamble = (
        'import json,os,subprocess,sys,time; from pathlib import Path; from orchestrator import procutil; from orchestrator.jobs import handle; '
        'from orchestrator.adapters.atomicwrite import atomic_write; from load_helpers import retry_temp_permission; '
        'in_job=handle._caller_in_job(); limits=handle._caller_job_limits() if in_job else 0; '
        'probe_in_job=None; '
        '\nif in_job and limits & 0x1800:\n'
        ' flags=0 if limits & 0x1000 else 0x01000000\n'
        ' probe=subprocess.Popen([sys.executable,"-c","pass"], creationflags=0x08000000|0x8|0x4|flags, '
        'stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,close_fds=True)\n'
        ' try: probe_in_job=handle._process_in_any_job(probe._handle)\n'
        f' finally: probe.kill(); _,probe_error=probe.communicate(timeout={PROCESS_SECONDS}); assert not probe_error, probe_error\n'
        f'state_path=Path({str(state_path)!r})\n'
        'retry_temp_permission(lambda: atomic_write(state_path, json.dumps({"pid":os.getpid(),"in_job":in_job,"limits":limits,"probe_in_job":probe_in_job})))\n'
        f'\ndeadline=time.monotonic()+{PROCESS_SECONDS}\n'
        f'while not Path({str(proceed)!r}).exists():\n'
        ' assert time.monotonic()<deadline, "parent verification timed out"\n'
        ' time.sleep(.01)\n'
    )
    python = getattr(sys, '_base_executable', sys.executable)
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join([*product_import_paths(), str(Path(__file__).resolve().parent)])
    probe_interpreter(python, env=env, cwd=tmp_path, evidence=description)
    parent = None
    stderr = tempfile.TemporaryFile()
    try:
        kwargs = procutil.no_window_kwargs()
        kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | 0x4 | (flags or 0)
        try:
            parent = subprocess.Popen([python, '-c', preamble + code],
                                      stdout=subprocess.DEVNULL,
                                      stderr=stderr, cwd=tmp_path, env=env, close_fds=True, **kwargs)
        except OSError as error:
            if error.winerror == 5:
                pytest.skip(f'{description}; controlled parent creation denied: {error}')
            raise
        if escape and handle._process_in_any_job(parent._handle):
            pytest.skip(f'{description}; parent remains in an enclosing job despite breakaway')
        for name, job in jobs:
            if not procutil._assign_to_census_job(job, parent.pid):
                pytest.skip(f'{description}; cannot assign {name} job (limit flags 0x{JOB_FLAGS[name]:x})')
            if procutil._process_in_job(parent.pid, job) is not True:
                pytest.skip(f'{description}; {name} membership not verified (limit flags 0x{JOB_FLAGS[name]:x})')
        assert procutil._resume_suspended_process(parent.pid), description
        try:
            wait_for(lambda: state_path.exists(), bool)
        except AssertionError as error:
            raise AssertionError(f'{description}; parent observation missing: {error}') from error
        state = json.loads(retry_temp_permission(lambda: state_path.read_text(encoding='utf-8')))
        description += f'; parent innermost limit flags 0x{state["limits"]:x}, pid={state["pid"]}'
        for name, job in jobs:
            if procutil._process_in_job(state['pid'], job) is not True:
                pytest.skip(f'{description}; running interpreter is not in intended {name} job')
        expected_limits = JOB_FLAGS[jobs[-1][0]] if jobs else 0
        if state['in_job'] != bool(jobs) or state['limits'] != expected_limits:
            pytest.skip(f'{description}; intended innermost limit flags 0x{expected_limits:x} not established')
        if escape and state['probe_in_job']:
            pytest.skip(f'{description}; verified initially outside every job but breakaway probe remains in a job: {state}')
        proceed.touch()
        try:
            yield parent, state, description
        except AssertionError as error:
            raise AssertionError(f'{description}; {error}') from error
    except (AssertionError, subprocess.TimeoutExpired, pytest.skip.Exception) as error:
        if parent is not None and parent.poll() is None:
            parent.kill()
            parent.wait(timeout=PROCESS_SECONDS)
        stderr.seek(0)
        message = f'{description}; {error}; child stderr: {stderr.read().decode(errors="replace")}'
        if isinstance(error, pytest.skip.Exception):
            pytest.skip(message)
        raise AssertionError(message) from error
    finally:
        if parent is not None:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=PROCESS_SECONDS)
        stderr.close()


def expected_launch(state):
    flags, outside, limitation = observed_policy(state['in_job'], state['limits'])
    if outside and state['probe_in_job']:
        return False, 'breakaway_not_effective'
    return flags is not None, limitation


def controlled_job(breakaway):
    """Test-owned job; contained mode forbids escape without kill-on-close."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t),
                    ('max_ws', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

    job = procutil._create_census_job()
    outer_in_job = handle._caller_in_job()
    outer_limits = handle._caller_job_limits() if outer_in_job else 0
    evidence = f'outer test-harness Job Object (limit flags 0x{outer_limits:x}); controlled {breakaway} limit flags 0x{JOB_FLAGS[breakaway]:x}'
    assert job, evidence
    info = Extended()
    info.basic.flags = (0 if breakaway == 'contained' else 0x2000) | {
        'explicit': 0x800, 'silent': 0x1000, 'forbidden': 0, 'contained': 0,
    }[breakaway]
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    try:
        assert kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)), evidence
    except BaseException:
        procutil._close_census_job(job)
        raise
    return job


@native
@pytest.mark.parametrize('breakaway', ['explicit', 'silent', 'forbidden', 'none', 'contained'])
def test_controlled_caller_job_survival_or_explicit_refusal(tmp_path, breakaway):
    release = tmp_path / 'worker-release'
    worker = (
        'import time; from pathlib import Path\n'
        f'deadline=time.monotonic()+{PROCESS_SECONDS}\n'
        f'while not Path({str(release)!r}).exists():\n'
        ' assert time.monotonic()<deadline, "caller observation timed out"\n'
        ' time.sleep(.05)\n'
        'print("survived caller")\n'
    )
    # Completion follows caller exit/termination, never a one-second timer.
    spec = spec_for(tmp_path, worker, timeout=PROCESS_SECONDS * 2)
    launch = tmp_path / 'launch.json'
    spec_path = tmp_path / 'parent-spec.json'
    retry_temp_permission(lambda: spec_path.write_text(json.dumps(asdict(spec)), encoding='utf-8'))
    code = (
        'import json,time; from pathlib import Path; from dataclasses import asdict; '
        'from orchestrator.jobs.handle import JobSpec,start_job,_caller_in_job; '
        + ('assert not _caller_in_job(); ' if breakaway == 'none' else '') +
        f'spec=JobSpec(**json.loads(retry_temp_permission(lambda: Path({str(spec_path)!r}).read_text(encoding="utf-8")))); '
        'result=start_job(spec); '
        f'retry_temp_permission(lambda: atomic_write(Path({str(launch)!r}), json.dumps(asdict(result)))); '
        + ('' if breakaway == 'contained' else f'time.sleep({PROCESS_SECONDS * 2})')
    )
    job = controlled_job(breakaway) if breakaway != 'none' else None
    ref = None
    try:
        jobs = [(breakaway, job)] if job else []
        with verified_parent(tmp_path, code, jobs, escape=breakaway in ('explicit', 'silent', 'none')) as (parent, state, evidence):
            wait_for(lambda: launch.exists(), bool)
            result = json.loads(retry_temp_permission(lambda: launch.read_text(encoding='utf-8')))
            if result['ref']:
                from orchestrator.jobs.seam import JobRef
                ref = JobRef(**result['ref'])
            launched, limitation = expected_launch(state)
            assert result['launched'] is launched, (evidence, result)
            assert result['limitation'] == limitation, (evidence, result)
            if not launched:
                row = history.read_job(spec.db_path, ref.job_id)
                assert row['runner_pid'] is None, evidence
                assert row['status'] == 'failed', evidence
                assert not list(tmp_path.glob('job-*.json')), evidence
            else:
                row = wait_for(lambda: handle.job_status(ref.job_id, db_path=spec.db_path),
                               lambda r: r['status'] == 'running')
                assert row['launch_limitation'] == limitation, evidence
                if breakaway == 'contained':
                    assert procutil._process_in_job(row['runner_pid'], job) is True, evidence
                    # Close the non-killing job after normal caller exit while
                    # the runner is still working; supported cases stay strict.
                    assert parent.wait(timeout=PROCESS_SECONDS) == 0, evidence
                else:
                    if job:
                        assert procutil._process_in_job(row['runner_pid'], job) is False, evidence
                    else:
                        assert process_in_any_job(row['runner_pid']) is False, evidence
                    parent.kill()
                    parent.wait(timeout=PROCESS_SECONDS)
                if job:
                    procutil._close_census_job(job)
                job = None
                release.touch()
                final = wait_for(lambda: handle.job_status(ref.job_id, db_path=spec.db_path),
                                 lambda r: r['ended_at'])
                assert final['status'] == 'finished', evidence
                assert final['launch_limitation'] == limitation, evidence
    finally:
        if job:
            procutil._close_census_job(job)
        cleanup(spec, ref)


@native
def test_concurrent_starts_reserve_one_runner(tmp_path):
    require_launch_environment()
    spec = spec_for(tmp_path, 'import time; time.sleep(.5)')
    results = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: handle.start_job(spec), range(2)))
        assert sum(r.launched for r in results) == 1, results
        assert [r.limitation for r in results if not r.launched] == ['attempt_already_reserved']
        conn = db.connect(spec.db_path)
        try:
            assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        finally:
            conn.close()
        ref = next(r.ref for r in results if r.launched)
        final = wait_for(lambda: handle.job_status(ref.job_id, db_path=spec.db_path), lambda r: r['ended_at'])
        assert final['status'] == 'finished'
    finally:
        for result in results:
            cleanup(spec, result.ref)


def test_reservation_requires_open_attempt_and_unique_identity(tmp_path):
    spec = spec_for(tmp_path, 'pass')
    row = history.reserve_job(spec.db_path, 'job-one', spec.attempt_id)
    assert row['decision_id'] == 'test-decision'
    with pytest.raises(sqlite3.IntegrityError):
        history.reserve_job(spec.db_path, 'job-two', spec.attempt_id)
    with pytest.raises(ValueError):
        history.reserve_job(spec.db_path, 'missing', spec.attempt_id + 99)
    history.resolve_delegation_attempt(spec.db_path, spec.attempt_id, status='completed')
    with pytest.raises(ValueError):
        history.reserve_job(spec.db_path, 'resolved', spec.attempt_id)
    with pytest.raises(ValueError):
        history.update_job(spec.db_path, 'job-one', output='never persist')
    for name in ('runner_pid', 'worker_pid', 'runner_created', 'heartbeat_at', 'ended_at', 'launch_limitation'):
        with pytest.raises(ValueError):
            history.update_job(spec.db_path, 'job-one', **{name: 'content or a path'})


def test_status_checks_creation_time_and_never_reconciles(tmp_path, monkeypatch):
    spec = spec_for(tmp_path, 'pass')
    history.reserve_job(spec.db_path, 'identity', spec.attempt_id)
    history.update_job(spec.db_path, 'identity', runner_pid=123, runner_created=1.0)
    monkeypatch.setattr(procutil, 'pid_state', lambda p: 'alive')
    monkeypatch.setattr(procutil, 'process_creation_epoch', lambda p: 2.0)
    assert handle.job_status('identity', db_path=spec.db_path)['runner_liveness'] == 'dead'
    monkeypatch.setattr(procutil, 'process_creation_epoch', lambda p: None)
    assert handle.job_status('identity', db_path=spec.db_path)['runner_liveness'] == 'unknown'
    assert history.read_job(spec.db_path, 'identity')['status'] == 'starting'
    assert handle.stop_job('missing', db_path=spec.db_path) == 'not_found'


@native
def test_missing_attempt_never_spawns(tmp_path, monkeypatch):
    spec = spec_for(tmp_path, 'pass')
    history.resolve_delegation_attempt(spec.db_path, spec.attempt_id, status='completed')
    monkeypatch.setattr(handle, '_caller_in_job', lambda: False)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('must not spawn'))
    assert handle.start_job(spec).limitation == 'open_attempt_unavailable'


@native
@pytest.mark.parametrize('contained', [False, True])
def test_launch_failure_wait_timeout_still_closes_records(tmp_path, monkeypatch, contained):
    spec = spec_for(tmp_path, 'pass')
    monkeypatch.setattr(handle, '_launch_policy', lambda: (0, True, None))
    monkeypatch.setattr(handle, '_process_in_any_job', lambda h: contained)
    monkeypatch.setattr(procutil, 'process_creation_epoch', lambda p: None)
    monkeypatch.setattr(procutil, '_resume_suspended_process', lambda p: pytest.fail('must not resume'))
    calls = []

    class Suspended:
        pid = 12345
        _handle = 1

        def kill(self):
            calls.append('kill')

        def wait(self, timeout):
            calls.append('wait')
            raise subprocess.TimeoutExpired('synthetic', timeout)

    def spawn(argv, **kwargs):
        expected_image = (str(Path(sys._base_executable).resolve())
                          if os.name == 'nt' and sys.prefix != sys.base_prefix else handle._runner_executable())
        assert argv[0] == expected_image
        assert kwargs['creationflags'] & 0x4
        assert Path(kwargs['stderr'].name).name == 'job-runner.log'
        assert kwargs['stderr'].mode == 'a'
        return Suspended()

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    result = handle.start_job(spec)
    assert not result.launched
    assert result.limitation == ('breakaway_not_effective' if contained else 'runner_launch_failed')
    assert calls == ['kill', 'wait']
    assert not list(tmp_path.glob('job-*.json'))
    assert history.read_job(spec.db_path, result.ref.job_id)['status'] == 'failed'
    conn = db.connect(spec.db_path)
    try:
        attempt = conn.execute('SELECT status,resolved_at FROM delegation_attempts').fetchone()
        assert attempt['status'] == 'failed' and attempt['resolved_at']
    finally:
        conn.close()


def test_runner_command_uses_the_runner_module(monkeypatch, tmp_path):
    from types import SimpleNamespace
    # This case covers the ordinary interpreter. The bound venv bootstrap has
    # separate admitted/refused-image regression cases.
    monkeypatch.setattr(handle, 'sys', SimpleNamespace(executable=sys.executable,
                                                      prefix='fixture', base_prefix='fixture'))
    spec_path = tmp_path / 'job-x.json'
    assert handle._runner_command('job-x', spec_path, 'state.db') == [
        handle._runner_executable(), '-m', 'orchestrator.jobs.runner',
        '--job-id', 'job-x', '--spec', str(spec_path), '--db-path', 'state.db']


def test_cli_job_runner_subcommand_parses_the_runner_arguments(monkeypatch, tmp_path):
    from orchestrator import cli
    from orchestrator.jobs import runner

    seen = []
    monkeypatch.setattr(runner, 'run_job', lambda job_id, spec, db_path=None: seen.append(
        (job_id, spec, db_path)) or 0)
    spec_path = tmp_path / 'job-y.json'
    assert cli.main(['job-runner', '--job-id', 'job-y', '--spec', str(spec_path),
                     '--db-path', str(tmp_path / 'state.db')]) == 0
    assert seen == [('job-y', spec_path, tmp_path / 'state.db')]


def test_runner_preserves_venv_interpreter(monkeypatch):
    monkeypatch.setattr(sys, 'executable', 'venv-launcher.exe')
    monkeypatch.setattr(sys, '_base_executable', 'real-python.exe')
    assert handle._runner_executable() == 'venv-launcher.exe'


def test_runner_log_rotates_with_one_previous_file(tmp_path, monkeypatch):
    monkeypatch.setattr(handle.paths, 'logs_dir', lambda: tmp_path)
    monkeypatch.setattr(handle, 'RUNNER_LOG_MAX_BYTES', 32)
    log = tmp_path / 'job-runner.log'
    previous = tmp_path / 'job-runner.log.1'
    previous.write_text('old previous')
    log.write_text('x' * 33)
    handle._log_runner_event('log-test', 'OSError')
    assert previous.read_text() == 'x' * 33
    assert log.read_text().split()[1:] == ['log-test', 'OSError']
    first = log.read_text()
    handle._log_runner_event('log-test', 'ValueError')
    assert previous.read_text() == first
    assert log.read_text().split()[1:] == ['log-test', 'ValueError']
    assert {p.name for p in tmp_path.iterdir()} == {'job-runner.log', 'job-runner.log.1'}


@native
@pytest.mark.parametrize('failure', ['folder', 'open'])
def test_unwritable_runner_log_falls_back_and_launches(tmp_path, monkeypatch, caplog, failure):
    spec = spec_for(tmp_path, 'pass')
    monkeypatch.setattr(handle, '_launch_policy', lambda: (0, False, None))
    log_dir = tmp_path / 'logs'
    if failure == 'folder':
        log_dir.write_text('not a directory')
    else:
        log_dir.mkdir()
        (log_dir / 'job-runner.log').mkdir()
    monkeypatch.setattr(handle.paths, 'logs_dir', lambda: log_dir)
    monkeypatch.setattr(procutil, 'process_creation_epoch', lambda pid: 1.0)
    monkeypatch.setattr(procutil, '_resume_suspended_process', lambda pid: True)

    class Suspended:
        pid = 12345

    def spawn(argv, **kwargs):
        assert kwargs['stderr'] == subprocess.DEVNULL
        return Suspended()

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    result = handle.start_job(spec)
    assert result.launched
    assert caplog.records[-1].getMessage().split()[1:] == [result.ref.job_id, 'runner_stderr_devnull']
    assert history.read_job(spec.db_path, result.ref.job_id)['status'] == 'starting'
    for path in tmp_path.glob('job-*.json'):
        path.unlink()


@native
def test_forbidden_policy_refuses_before_spawn(tmp_path, monkeypatch):
    spec = spec_for(tmp_path, 'pass')
    monkeypatch.setattr(handle, '_caller_in_job', lambda: True)
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: 0x2000)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('must not spawn'))
    result = handle.start_job(spec)
    assert result.limitation == 'breakaway_not_permitted'
    assert history.read_job(spec.db_path, result.ref.job_id)['status'] == 'failed'


@native
@pytest.mark.parametrize('limits, expected', [
    (0, (0, False, 'contained_innermost_job_without_kill_on_close')),
    (0x2000, (None, False, 'breakaway_not_permitted')),
    (0x1000, (0, True, None)), (0x3000, (0, True, None)),
    (0x800, (0x01000000, True, None)), (0x2800, (0x01000000, True, None)),
])
def test_launch_policy_distinguishes_containment_from_escape(monkeypatch, limits, expected):
    monkeypatch.setattr(handle, '_caller_in_job', lambda: True)
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: limits)
    assert handle._launch_policy() == expected


@native
def test_no_job_needs_no_breakaway_flag(monkeypatch):
    monkeypatch.setattr(handle, '_caller_in_job', lambda: False)
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: pytest.fail('no caller job'))
    assert handle._launch_policy() == (0, False, None)


def test_startup_identity_replaces_starter_and_cannot_expire(tmp_path, monkeypatch):
    spec = spec_for(tmp_path, 'pass')
    history.reserve_job(spec.db_path, 'reported', spec.attempt_id)
    history.update_job(spec.db_path, 'reported', runner_pid=123, runner_created=1.0)
    assert history.update_job_startup(spec.db_path, 'reported', startup_seconds=15,
                                      runner_pid=456, runner_created=2.0)
    monkeypatch.setattr(procutil, 'pid_state', lambda pid: 'alive' if pid == 456 else 'dead')
    monkeypatch.setattr(procutil, 'process_creation_epoch', lambda pid: 2.0 if pid == 456 else None)
    monkeypatch.setattr(handle, 'STARTUP_SECONDS', 0.000001)
    row = handle.job_status('reported', db_path=spec.db_path)
    assert row['runner_pid'] == 456 and row['runner_created'] == 2.0
    assert row['heartbeat_at'] and row['runner_liveness'] == 'alive'
    assert row['status'] == 'starting' and row['ended_at'] is None


@pytest.mark.parametrize('starter_state', ['alive', 'dead', 'unknown'])
def test_unreported_startup_deadline_closes_attempt(tmp_path, monkeypatch, starter_state):
    spec = spec_for(tmp_path, 'pass')
    history.reserve_job(spec.db_path, 'never-reported', spec.attempt_id)
    history.update_job(spec.db_path, 'never-reported', runner_pid=123, runner_created=1.0)
    monkeypatch.setattr(procutil, 'pid_state', lambda pid: starter_state)
    monkeypatch.setattr(procutil, 'process_creation_epoch', lambda pid: 1.0)
    row = handle.job_status('never-reported', db_path=spec.db_path)
    assert row['runner_liveness'] == starter_state
    assert row['status'] == 'starting'
    conn = db.connect(spec.db_path)
    try:
        old = (datetime.now(timezone.utc) - timedelta(seconds=handle.STARTUP_SECONDS + 1)).isoformat()
        conn.execute('UPDATE jobs SET created_at=?', (old,))
        conn.commit()
        row = handle.job_status('never-reported', db_path=spec.db_path)
        assert row['status'] == 'failed' and row['ended_at']
        attempt = conn.execute('SELECT * FROM delegation_attempts').fetchone()
        assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'failed_to_start'
        assert attempt['resolved_at'] and attempt['detail'] == 'runner_startup_deadline_exceeded'
        assert not history.update_job_startup(spec.db_path, 'never-reported', startup_seconds=15,
                                              runner_pid=456, runner_created=2.0)
        assert handle.job_status('never-reported', db_path=spec.db_path)['ended_at'] == row['ended_at']
    finally:
        conn.close()


def test_late_runner_cannot_register_or_start_worker(tmp_path, monkeypatch):
    from orchestrator.jobs import runner
    spec = spec_for(tmp_path, 'pass')
    history.reserve_job(spec.db_path, 'late', spec.attempt_id)
    conn = db.connect(spec.db_path)
    try:
        old = (datetime.now(timezone.utc) - timedelta(seconds=handle.STARTUP_SECONDS + 1)).isoformat()
        conn.execute('UPDATE jobs SET created_at=?', (old,))
        conn.commit()
    finally:
        conn.close()
    path = tmp_path / 'late-spec.json'
    path.write_text(json.dumps(asdict(spec)))
    monkeypatch.setattr(runner, '_execute', lambda *args: pytest.fail('late runner must not launch'))
    assert runner.run_job('late', path) == 1
    assert history.read_job(spec.db_path, 'late')['status'] == 'failed'
    conn = db.connect(spec.db_path)
    try:
        assert conn.execute('SELECT detail FROM delegation_attempts').fetchone()[0] == 'runner_startup_deadline_exceeded'
    finally:
        conn.close()


@native
def test_nested_outer_kill_on_close_is_not_a_survival_guarantee(tmp_path):
    # Keep both processes alive through observation on the shared, self-hosted
    # Windows runner; closing the outer job must cause their death, not a timer.
    spec = spec_for(tmp_path, 'import time; time.sleep(120)', timeout=180)
    spec_path = tmp_path / 'nested-spec.json'
    launch = tmp_path / 'nested-launch.json'
    retry_temp_permission(lambda: spec_path.write_text(json.dumps(asdict(spec)), encoding='utf-8'))
    code = (
        'import json,time; from pathlib import Path; from dataclasses import asdict; '
        'from orchestrator.jobs.handle import JobSpec,start_job; '
        f'result=start_job(JobSpec(**json.loads(retry_temp_permission(lambda: Path({str(spec_path)!r}).read_text(encoding="utf-8"))))); '
        f'retry_temp_permission(lambda: atomic_write(Path({str(launch)!r}), json.dumps(asdict(result)))); time.sleep(120)'
    )
    outer = controlled_job('forbidden')  # kill-on-close, no breakaway
    inner = controlled_job('contained')  # no kill-on-close, no breakaway
    ref = None
    try:
        with verified_parent(tmp_path, code, [('forbidden', outer), ('contained', inner)]) as (parent, state, evidence):
            wait_for(lambda: launch.exists(), bool)
            result = json.loads(retry_temp_permission(lambda: launch.read_text(encoding='utf-8')))
            from orchestrator.jobs.seam import JobRef
            ref = JobRef(**result['ref'])
            launched, limitation = expected_launch(state)
            assert result['launched'] is launched, (evidence, result)
            assert result['limitation'] == limitation, (evidence, result)
            row = wait_for(lambda: handle.job_status(ref.job_id, db_path=spec.db_path),
                           lambda r: r['status'] == 'running')
            assert row['launch_limitation'] == limitation, evidence
            assert procutil._process_in_job(row['runner_pid'], inner) is True, evidence
            assert procutil._process_in_job(row['runner_pid'], outer) is True, evidence
            assert procutil.pid_state(row['runner_pid']) == 'alive', evidence
            assert procutil.pid_state(row['worker_pid']) == 'alive', evidence
            procutil._close_census_job(outer)
            outer = None
            wait_for(lambda: procutil.pid_state(row['runner_pid']), lambda s: s == 'dead', seconds=PROCESS_SECONDS)
            wait_for(lambda: procutil.pid_state(row['worker_pid']), lambda s: s == 'dead', seconds=PROCESS_SECONDS)
            assert handle.job_status(ref.job_id, db_path=spec.db_path)['runner_liveness'] == 'dead', evidence
            # Post-report crash recovery is separate; no invented final completion.
            assert history.read_job(spec.db_path, ref.job_id)['ended_at'] is None, evidence
    finally:
        if outer:
            procutil._close_census_job(outer)
        procutil._close_census_job(inner)
        cleanup(spec, ref)


@native
def test_nested_venv_launcher_job_fails_closed(tmp_path):
    spec = spec_for(tmp_path, 'pass')
    spec_path = tmp_path / 'nested-launcher-spec.json'
    launch = tmp_path / 'nested-launcher-result.json'
    spec_path.write_text(json.dumps(asdict(spec)))
    code = (
        'import json; from pathlib import Path; from dataclasses import asdict; '
        'from orchestrator.jobs import handle; '
        'spawn=handle.subprocess.Popen; starters=[]; '
        '\ndef capture(*args, **kwargs):\n p=spawn(*args, **kwargs)\n starters.append(p.pid)\n return p\n'
        'handle.subprocess.Popen=capture; '
        f'result=handle.start_job(handle.JobSpec(**json.loads(Path({str(spec_path)!r}).read_text()))); '
        f'Path({str(launch)!r}).write_text(json.dumps({{"result":asdict(result),"starters":starters}}))'
    )
    outer = controlled_job('contained')  # forbids breakaway
    inner = controlled_job('silent')  # venv launcher: kill-on-close + silent breakaway
    ref = None
    try:
        with verified_parent(tmp_path, code, [('contained', outer), ('silent', inner)]) as (parent, state, evidence):
            assert parent.wait(timeout=PROCESS_SECONDS) == 0, evidence
            value = json.loads(launch.read_text())
            result = value['result']
            from orchestrator.jobs.seam import JobRef
            ref = JobRef(**result['ref'])
            launched, limitation = expected_launch(state)
            assert launched is False, evidence
            assert result['launched'] is launched, (evidence, result)
            assert result['limitation'] == limitation, (evidence, result)
            assert len(value['starters']) == 1, evidence
            assert procutil.pid_state(value['starters'][0]) == 'dead', evidence
            row = history.read_job(spec.db_path, ref.job_id)
            assert row['status'] == 'failed' and row['ended_at'], evidence
            assert row['worker_pid'] is None and row['heartbeat_at'] is None, evidence
            assert not list(tmp_path.glob('job-*.json')), evidence
            conn = db.connect(spec.db_path)
            try:
                attempt = conn.execute('SELECT * FROM delegation_attempts WHERE id=?',
                                       (spec.attempt_id,)).fetchone()
                assert attempt['status'] == 'failed' and attempt['resolved_at'], evidence
                assert attempt['failure_kind'] == 'failed_to_start', evidence
                assert conn.execute('SELECT count(*) FROM jobs WHERE ended_at IS NULL').fetchone()[0] == 0, evidence
                assert conn.execute('SELECT count(*) FROM delegation_attempts WHERE resolved_at IS NULL').fetchone()[0] == 0, evidence
            finally:
                conn.close()
    finally:
        procutil._close_census_job(inner)
        procutil._close_census_job(outer)
        cleanup(spec, ref)


@native
def test_real_venv_runner_starts_and_finishes_offline(tmp_path):
    venv = tmp_path / 'venv'
    outer_in_job = handle._caller_in_job()
    outer_limits = handle._caller_job_limits() if outer_in_job else 0
    evidence = f'outer test-harness Job Object (in_job={outer_in_job}, limit flags 0x{outer_limits:x})'
    probe_interpreter(sys.executable, evidence=evidence)
    try:
        # ensurepip and the venv redirector have descendants: stop the entire
        # test-owned tree on timeout, including inherited pipe handles.
        result = procutil.run_with_tree_kill(
            [sys.executable, '-m', 'venv', '--system-site-packages', str(venv)],
            capture_output=True, text=True, timeout=180, kill_on_close=True)
    except (subprocess.TimeoutExpired, OSError) as error:
        pytest.skip(f'{evidence}; venv creation unavailable: {type(error).__name__}: {error}; stderr={getattr(error, "stderr", None)!r}')
    if result.returncode != 0:
        pytest.skip(f'{evidence}; venv creation failed (exit {result.returncode}): {result.stderr}')
    python = venv / 'Scripts' / 'python.exe'
    (venv / 'Lib' / 'site-packages' / 'worktree.pth').write_text(
        '\n'.join(product_import_paths()) + '\n', encoding='utf-8')
    spec = spec_for(tmp_path, 'pass')
    spec_path = tmp_path / 'venv-spec.json'
    spec_path.write_text(json.dumps(asdict(spec)))
    launch = tmp_path / 'venv-launch.json'
    state_path = tmp_path / 'venv-state.json'
    proceed = tmp_path / 'venv-proceed'
    # Put the venv caller in a controlled non-killing innermost job. This also
    # runs under an enclosing sandbox that forbids escape: startup and venv
    # import/identity are under test here; escape survival is tested separately.
    # Keep the caller alive until completion: its enclosing venv launcher may
    # own a kill-on-close job, which the innermost-job record does not inspect.
    code = (
        'import json,os,sys,time; from pathlib import Path; from dataclasses import asdict; '
        'from orchestrator import procutil; from orchestrator.jobs import handle; '
        f'assert Path(handle.__file__).resolve() == Path({str(Path(handle.__file__).resolve())!r}); '
        'assert sys.prefix != sys.base_prefix; '
        'job=procutil._create_census_job(); assert job; '
        'assert procutil._assign_to_census_job(job,os.getpid()); '
        'in_job=handle._caller_in_job(); limits=handle._caller_job_limits() if in_job else 0; '
        f'state_path=Path({str(state_path)!r}); temporary=state_path.with_suffix(".tmp"); '
        'temporary.write_text(json.dumps({"pid":os.getpid(),"job":job,"in_job":in_job,"limits":limits})); '
        'temporary.replace(state_path); '
        f'\ndeadline=time.monotonic()+{PROCESS_SECONDS}\n'
        f'while not Path({str(proceed)!r}).exists():\n'
        ' assert time.monotonic()<deadline, "venv parent verification timed out"\n'
        ' time.sleep(.01)\n'
        'spawn=handle.subprocess.Popen; starters=[]; '
        '\ndef capture(*args, **kwargs):\n p=spawn(*args, **kwargs)\n starters.append(p.pid)\n return p\n'
        'handle.subprocess.Popen=capture; '
        f'raw=json.loads(Path({str(spec_path)!r}).read_text()); '
        'raw["argv"]=[sys.executable,"-c","print(123)"]; raw["env"]={}; raw["sink"]="none"; '
        'result=handle.start_job(handle.JobSpec(**raw)); '
        f'Path({str(launch)!r}).write_text(json.dumps({{"result":asdict(result),"starter":starters[0] if starters else None,"caller_pid":os.getpid(),"in_job":in_job,"limits":limits}})); '
        f'\nlimit=time.monotonic()+{PROCESS_SECONDS}\n'
        'while result.launched and time.monotonic()<limit:\n'
        ' row=handle.job_status(result.ref.job_id,db_path=raw["db_path"])\n'
        ' if row["ended_at"]: break\n'
        ' time.sleep(.05)\n'
        'procutil._close_census_job(job)'
    )
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    probe_interpreter(python, env=env, cwd=tmp_path, evidence=evidence)
    parent = None
    state = None
    caller_created = None
    ref = None
    stderr = tempfile.TemporaryFile()
    try:
        parent = subprocess.Popen([str(python), '-c', code], cwd=tmp_path, env=env,
                                  stdout=subprocess.DEVNULL, stderr=stderr,
                                  close_fds=True, **procutil.no_window_kwargs())
        wait_for(lambda: state_path.exists(), bool)
        state = json.loads(state_path.read_text())
        caller_created = procutil.process_creation_epoch(state['pid'])
        evidence += f'; venv caller innermost limit flags 0x{state["limits"]:x}'
        verify_remote_job(state['pid'], state['job'], evidence)
        if not state['in_job'] or state['limits'] != 0:
            pytest.skip(f'{evidence}; controlled non-killing innermost job not established')
        proceed.touch()
        parent.wait(timeout=PROCESS_SECONDS)
        stderr.seek(0)
        assert parent.returncode == 0, (evidence, stderr.read().decode('utf-8', 'replace'))
        value = json.loads(launch.read_text())
        result = value['result']
        from orchestrator.jobs.seam import JobRef
        ref = JobRef(**result['ref'])
        assert result['launched'], (evidence, result)
        assert result['limitation'] == observed_policy(value['in_job'], value['limits'])[2], evidence
        final = wait_for(lambda: handle.job_status(ref.job_id, db_path=spec.db_path),
                         lambda r: r['ended_at'], seconds=PROCESS_SECONDS)
        assert final['status'] == 'finished', evidence
        assert final['heartbeat_at'] and final['runner_created'], evidence
        conn = db.connect(spec.db_path)
        try:
            assert conn.execute('SELECT status FROM delegation_attempts').fetchone()[0] == 'completed', evidence
        finally:
            conn.close()
        # The admitted base bootstrap avoids the redirector's killing Job.
        # The runner itself owns the spawned PID and registers its heartbeat;
        # differing-starter registration is separately covered synthetically.
        assert final['runner_pid'] == value['starter'], evidence
    except (AssertionError, subprocess.TimeoutExpired, pytest.skip.Exception) as error:
        stderr.seek(0)
        message = f'{evidence}; {error}; child stderr: {stderr.read().decode(errors="replace")}'
        if isinstance(error, pytest.skip.Exception):
            pytest.skip(message)
        raise AssertionError(message) from error
    finally:
        if state and caller_created is not None and caller_created == procutil.process_creation_epoch(state['pid']):
            procutil.terminate_pid(state['pid'])
        if parent is not None:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=PROCESS_SECONDS)
        if ref is None and launch.exists():
            value = json.loads(launch.read_text())
            if value['result']['ref']:
                from orchestrator.jobs.seam import JobRef
                ref = JobRef(**value['result']['ref'])
        cleanup(spec, ref)
        stderr.close()


def test_dead_runner_with_running_record_is_reported_truthfully(tmp_path, monkeypatch):
    spec = spec_for(tmp_path, 'pass')
    history.reserve_job(spec.db_path, 'dead-running', spec.attempt_id)
    history.update_job(spec.db_path, 'dead-running', status='running', runner_pid=123,
                       runner_created=1.0, worker_pid=456)
    monkeypatch.setattr(procutil, 'pid_state', lambda pid: 'dead')
    row = handle.job_status('dead-running', db_path=spec.db_path)
    assert row['runner_liveness'] == 'dead'
    assert row['status'] == 'running' and row['ended_at'] is None


@native
def test_runner_log_rotation_with_live_handle_appends_then_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(handle.paths, 'logs_dir', lambda: tmp_path)
    monkeypatch.setattr(handle, 'RUNNER_LOG_MAX_BYTES', 32)
    log = tmp_path / 'job-runner.log'
    previous = tmp_path / 'job-runner.log.1'
    log.write_text('x' * 33 + '\n')
    with log.open('a', encoding='utf-8') as live_runner:
        handle._log_runner_event('live-log', 'OSError')
        live_runner.write('still captured\n')
    assert not previous.exists()
    before = log.read_text()
    assert before.startswith('x' * 33 + '\n')
    assert 'live-log OSError' in before and before.endswith('still captured\n')
    handle._log_runner_event('next-log', 'ValueError')
    assert previous.read_text() == before
    assert log.read_text().split()[1:] == ['next-log', 'ValueError']


@pytest.mark.parametrize('in_job,flags,escaped,expected', [
    (False, 0, False, (True, 'not_in_job')),
    (True, 0x800, True, (True, 'breakaway_verified')),
    (True, 0x1000, True, (True, 'breakaway_verified')),
    (True, 0x2800, True, (True, 'breakaway_verified')),
    (True, 0x3000, True, (True, 'breakaway_verified')),
    (True, 0x800, False, (False, 'job_breakaway_ineffective')),
    (True, 0x3000, False, (False, 'job_breakaway_ineffective')),
    (True, 0x2000, False, (False, 'job_kill_on_close')),
    (True, 0, False, (False, 'job_shared')),
])
def test_detached_verdict_injected_probes(in_job, flags, escaped, expected):
    def limits():
        assert in_job
        return flags
    def probe():
        assert flags & 0x1800
        return escaped
    assert handle.detached_launch_verdict(in_job=lambda: in_job, limits=limits,
                                          breakaway_probe=probe) == expected


@pytest.mark.parametrize('failure', ['membership', 'limits', 'breakaway'])
def test_detached_verdict_api_failures_are_unknown(failure):
    def fail():
        raise OSError('API unavailable')
    assert handle.detached_launch_verdict(
        in_job=fail if failure == 'membership' else lambda: True,
        limits=fail if failure == 'limits' else lambda: 0x800,
        breakaway_probe=fail if failure == 'breakaway' else lambda: True,
    ) == (False, 'containment_unknown')


@native
@pytest.mark.parametrize('escaped', [True, False])
def test_breakaway_verification_cached_for_process(monkeypatch, escaped):
    monkeypatch.setattr(handle, '_breakaway_result', None)
    monkeypatch.setattr(handle, '_breakaway_checked', False)
    monkeypatch.setattr(handle, '_caller_in_job', lambda: True)
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: 0x800)
    calls = []
    monkeypatch.setattr(handle, '_probe_breakaway', lambda: calls.append(1) or escaped)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: handle.detached_launch_verdict(), range(8)))
    assert calls == [1]
    assert results == [(escaped, 'breakaway_verified' if escaped else 'job_breakaway_ineffective')] * 8


@native
@pytest.mark.parametrize('flags', [0x800, 0x1000])
def test_breakaway_probe_suspended_hidden_and_always_terminated(monkeypatch, flags):
    calls = []
    class Child:
        _handle = 1
        def kill(self):
            calls.append('kill')
        def wait(self, timeout):
            calls.append('wait')
    def popen(command, **kwargs):
        assert kwargs['creationflags'] & 0x4
        assert kwargs['creationflags'] & 0x08000000
        assert bool(kwargs['creationflags'] & 0x01000000) == (flags == 0x800)
        return Child()
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: flags)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(handle, '_process_in_any_job', lambda h: False)
    assert handle._probe_breakaway()
    assert calls == ['kill', 'wait']


@native
def test_probe_failure_before_reservation_and_start_order(tmp_path, monkeypatch):
    spec = spec_for(tmp_path, 'pass')
    def fail():
        raise OSError('unknown')
    monkeypatch.setattr(handle, '_launch_policy', fail)
    assert handle.reserve_and_write(spec) == handle.JobStart(None, False, 'caller_containment_unknown')
    assert history.job_for_attempt(spec.db_path, spec.attempt_id) is None
    events = []
    monkeypatch.setattr(handle, '_launch_policy', lambda: events.append('probe') or (0, False, None))
    reserve = history.reserve_job
    write = handle._write_spec
    monkeypatch.setattr(history, 'reserve_job', lambda *a: events.append('reserve') or reserve(*a))
    monkeypatch.setattr(handle, '_write_spec', lambda *a: events.append('write') or write(*a))
    def launch(ref, path, spec, **kwargs):
        events.append('spawn')
        assert kwargs['_policy'] == (0, False, None)
        path.unlink()
        return handle.JobStart(ref, True, None)
    monkeypatch.setattr(handle, 'launch_reserved', launch)
    assert handle.start_job(spec).launched
    assert events == ['probe', 'reserve', 'write', 'spawn']


@native
def test_nested_outer_job_disproves_service_breakaway(tmp_path):
    observed = tmp_path / 'verdict.json'
    code = (
        'from orchestrator.jobs.handle import detached_launch_verdict; '
        f'Path({str(observed)!r}).write_text(json.dumps(detached_launch_verdict())); '
    )
    outer = controlled_job('contained')
    inner = controlled_job('silent')
    try:
        with verified_parent(tmp_path, code, [('contained', outer), ('silent', inner)]) as (parent, state, evidence):
            assert parent.wait(timeout=PROCESS_SECONDS) == 0, evidence
            assert json.loads(observed.read_text()) == [False, 'job_breakaway_ineffective'], evidence
    finally:
        procutil._close_census_job(inner)
        procutil._close_census_job(outer)


@native
def test_service_launch_survives_termination_of_separate_host_job(tmp_path):
    """Actual runner membership and completion, with a separate controlled Host."""
    import ctypes
    from ctypes import wintypes
    in_job = handle._caller_in_job()
    flags = handle._caller_job_limits() if in_job else 0
    evidence = f'test service harness: in_job={in_job}, flags=0x{flags:x}'
    if handle._launch_policy()[0] is None:
        pytest.skip(f'{evidence}; launch policy forbids breakaway')
    spec = spec_for(tmp_path, "import time; time.sleep(1); print('survived separate host')", timeout=5)
    host_job = controlled_job('contained')
    host = None
    ref = None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    try:
        kwargs = procutil.no_window_kwargs()
        kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | 0x4
        host = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', 'import time; time.sleep(20)'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
        assert procutil._assign_to_census_job(host_job, host.pid)
        assert procutil._process_in_job(host.pid, host_job) is True
        assert procutil._resume_suspended_process(host.pid)
        reserved = handle.reserve_and_write(spec)
        assert isinstance(reserved, handle.Reserved), reserved
        ref = reserved.ref
        result = handle.launch_reserved(ref, reserved.spec_path, spec)
        if result.limitation == 'breakaway_not_effective':
            pytest.skip(f'{evidence}; breakaway_not_effective')
        assert result.launched, (evidence, result)
        running = wait_for(lambda: handle.job_status(ref.job_id, db_path=spec.db_path),
                           lambda row: row['status'] == 'running')
        assert procutil._process_in_job(running['runner_pid'], host_job) is False
        assert kernel.TerminateJobObject(host_job, 1)
        host.wait(timeout=PROCESS_SECONDS)
        assert host.returncode != 0
        final = wait_for(lambda: handle.job_status(ref.job_id, db_path=spec.db_path), lambda row: row['ended_at'])
        assert final['status'] == 'finished'
        output = json.loads((tmp_path / 'sink' / (ref.job_id + '.json')).read_text())
        assert output['text'] == 'survived separate host\n'
    finally:
        if host is not None:
            if host.poll() is None:
                host.kill()
            host.wait(timeout=PROCESS_SECONDS)
        procutil._close_census_job(host_job)
        cleanup(spec, ref)


@native
def test_breakaway_probe_failure_cached_as_unknown(monkeypatch):
    monkeypatch.setattr(handle, '_breakaway_result', None)
    monkeypatch.setattr(handle, '_breakaway_checked', False)
    monkeypatch.setattr(handle, '_caller_in_job', lambda: True)
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: 0x800)
    calls = []
    def fail():
        calls.append(1)
        raise OSError('API failure')
    monkeypatch.setattr(handle, '_probe_breakaway', fail)
    assert handle.detached_launch_verdict() == (False, 'containment_unknown')
    assert handle.detached_launch_verdict() == (False, 'containment_unknown')
    assert calls == [1]


@native
@pytest.mark.parametrize('escaped', [True, False])
@pytest.mark.parametrize('step,error', [('kill', OSError), ('wait', OSError),
                                       ('wait', subprocess.TimeoutExpired)])
def test_breakaway_probe_cleanup_failure_keeps_measured_result(monkeypatch, escaped, step, error):
    calls = []
    class Child:
        _handle = 1
        def kill(self):
            calls.append('kill')
            if step == 'kill':
                raise error('cleanup unavailable')
        def wait(self, timeout):
            calls.append('wait')
            if step == 'wait':
                if error is subprocess.TimeoutExpired:
                    raise error('probe', timeout)
                raise error('cleanup unavailable')
    monkeypatch.setattr(handle, '_caller_job_limits', lambda: 0x1000)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: Child())
    monkeypatch.setattr(handle, '_process_in_any_job', lambda h: not escaped)
    assert handle._probe_breakaway() is escaped
    assert calls == ['kill', 'wait']
