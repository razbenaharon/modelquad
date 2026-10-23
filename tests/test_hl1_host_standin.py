# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Native Host Job Object termination, real loopback service, offline worker."""

import ctypes
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time

import pytest

from modelquad import procutil
from modelquad.jobs import handle
from modelquad.jobs.store import default_store
from modelquad.service import history
from test_jobs_runner import product_import_paths

native = pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object Host stand-in')
CHILD = Path(__file__).with_name('hl1_standin_child.py')
ANSWER = 'HL-1 synthetic survived answer'
SECONDS = 60


def wait_for(read, predicate=bool, seconds=SECONDS):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = read()
        if predicate(last):
            return last
        time.sleep(.05)
    raise AssertionError(f'bounded wait expired: {last!r}')


class HostJob:
    """Own a new no-breakaway Job and assign the Host before its first instruction."""

    def __init__(self, evidence):
        from ctypes import wintypes
        self.k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self.k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.k32.AssignProcessToJobObject.restype = wintypes.BOOL
        self.k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                      wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD]
        self.k32.QueryInformationJobObject.restype = wintypes.BOOL
        self.k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    wintypes.LPVOID, wintypes.DWORD]
        self.k32.SetInformationJobObject.restype = wintypes.BOOL
        self.k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.k32.IsProcessInJob.restype = wintypes.BOOL
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32.CloseHandle.restype = wintypes.BOOL
        self.evidence = evidence
        self.handle = self.k32.CreateJobObjectW(None, None)
        if not self.handle:
            self.unavailable('CreateJobObjectW')

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

        info = Extended()
        info.basic.flags = 0x2000
        if not self.k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            self.unavailable('SetInformationJobObject', error)
        assert self.k32.QueryInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info), None)
        assert info.basic.flags == 0x2000  # KILL_ON_JOB_CLOSE, neither breakaway bit.
        evidence['host_job_flags'] = hex(info.basic.flags)

    def unavailable(self, operation, error=None):
        error = ctypes.get_last_error() if error is None else error
        reason = f'{operation} failed: winerror={error}; outer flags={self.evidence["outer_job_flags"]}'
        self.evidence['skip_reason'] = reason
        pytest.skip(reason)

    def assign(self, process):
        if not self.k32.AssignProcessToJobObject(self.handle, process._handle):
            self.unavailable('AssignProcessToJobObject')
        assert self.contains(process._handle)

    def contains(self, process_handle):
        from ctypes import wintypes
        answer = wintypes.BOOL()
        assert self.k32.IsProcessInJob(process_handle, self.handle, ctypes.byref(answer))
        return bool(answer.value)

    def contains_pid(self, pid):
        process = self.k32.OpenProcess(0x1000, False, pid)
        assert process, f'cannot query running pid {pid}'
        try:
            return self.contains(process)
        finally:
            assert self.k32.CloseHandle(process)

    def terminate(self):
        assert procutil._terminate_job(self.handle), 'TerminateJobObject failed'
        self.close()

    def close(self):
        if self.handle:
            assert self.k32.CloseHandle(self.handle)
            self.handle = None


def child_env(root, monkeypatch):
    values = {
        'PYTHONPATH': os.pathsep.join([str(CHILD.parent), *product_import_paths()]),
        'MODELQUAD_TEST_EXECUTOR_PLUGIN': 'hl1_standin_child:registry',
        'MODELQUAD_TEST_EXECUTOR_OK': '1',
        'MODELQUAD_STATE_ROOT': str(root / 'state'), 'MODELQUAD_STATE_MODE': 'dev',
        'APPDATA': str(root / 'appdata'), 'LOCALAPPDATA': str(root / 'localappdata'),
        'HOME': str(root / 'home'), 'USERPROFILE': str(root / 'home'),
        'HL1_TEST_PATCH_SERVICE_VERDICT': '1',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('MODELQUAD_WORKER', raising=False)
    return dict(os.environ)


def launch(role, root, env, *args, suspended=False, stderr=None):
    kwargs = procutil.no_window_kwargs()
    kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | (0x4 if suspended else 0)
    return subprocess.Popen([sys.executable, str(CHILD), role, str(root), *map(str, args)],
                            cwd=root, env=env, stdout=subprocess.PIPE, stderr=stderr,
                            text=True, encoding='utf-8', close_fds=True, **kwargs)


def stdout_messages(process):
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            messages.put(line)
        messages.put(None)
    threading.Thread(target=read, daemon=True).start()
    return messages


def next_message(messages):
    line = messages.get(timeout=SECONDS)
    assert line is not None, 'Host exited before reporting its handle/control note'
    return json.loads(line)


def health(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
    try:
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        return json.loads(response.read()) if response.status == 200 else None
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


@native
@pytest.mark.serial_timing
@pytest.mark.parametrize('service_running', [True, False], ids=['survives-host-job', 'fallback-dies-with-host'])
def test_host_job_termination(tmp_path, monkeypatch, service_running):
    started = time.monotonic()
    evidence = {'case': 'service' if service_running else 'fallback', 'test_pid': os.getpid(),
                'outer_job_flags': hex(handle._caller_job_limits()) if handle._caller_in_job() else '0x0',
                'verdict_patched': False, 'host_result_wait_seconds': .05,
                'pids': {}, 'timings_seconds': {}}
    evidence_path = tmp_path / 'hl1-evidence.json'
    if service_running:
        # The test service is this process's child and shares its job. Where
        # that job's breakaway is ineffective (for example a venv launcher's
        # 0x3000 job inside a job that forbids breakaway) or forbidden with
        # kill-on-close, no runner can be launched from here at all: that is
        # the environment, not the product, so skip with the measured facts.
        reason = handle.detached_launch_verdict()[1]
        if reason in ('job_breakaway_ineffective', 'job_kill_on_close'):
            pytest.skip(f"test runner's own job cannot launch a runner: {reason}, "
                        f"flags {evidence['outer_job_flags']}")
    job = service = host = None
    runner_pid = runner_created = None
    stderr_path = tmp_path / 'children.stderr'
    try:
        env = child_env(tmp_path, monkeypatch)
        database = tmp_path / 'history.db'
        history.put_routing_mode(database, 'automatic')
        history.put_routing_paused(database, False)
        assert history.put_job_runner_enabled(database, True)
        (tmp_path / 'config.yaml').write_text('{}\n', encoding='utf-8')
        # Retain the bound socket for the absent-service control, so this never
        # probes an installed service or races a different server onto the port.
        with socket.socket() as reservation, stderr_path.open('w', encoding='utf-8') as stderr:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
            state = tmp_path / 'state'
            state.mkdir()
            (state / 'service.pid').write_text(json.dumps({'argv': ['serve', '--port', str(port)]}), encoding='utf-8')
            if service_running:
                reservation.close()
                service = launch('service', tmp_path, env, port, stderr=stderr)
                evidence['pids']['service'] = service.pid
                payload = wait_for(lambda: health(port))
                observation = json.loads((tmp_path / 'service-observation.json').read_text(encoding='utf-8'))
                evidence['service_observation'] = observation
                evidence['verdict_patched'] = observation['verdict_patched']
                assert payload['detached_runner']['ok'], payload
            job = HostJob(evidence)
            host = launch('host' if service_running else 'fallback', tmp_path, env, suspended=True, stderr=stderr)
            evidence['pids']['host'] = host.pid
            job.assign(host)
            if service:
                assert not job.contains(service._handle)
                evidence['service_in_host_job'] = False
            assert procutil._resume_suspended_process(host.pid)
            messages = stdout_messages(host)
            message = next_message(messages)
            if service_running:
                job_id = message['job_id']
                evidence['job_id'] = job_id
                row = wait_for(lambda: history.read_job(database, job_id),
                               lambda value: value and (value['ended_at'] or
                                                        value['status'] == 'running' and value['worker_pid']))
                if row['ended_at']:
                    attempt = history.read_job_attempt(database, row['attempt_id'])
                    evidence['launch_failure'] = {'job_status': row['status'],
                                                  'attempt_detail': attempt['detail']}
                assert row['status'] == 'running' and row['worker_pid'], evidence
                runner_pid, runner_created = row['runner_pid'], row['runner_created']
                evidence['pids'].update(runner=runner_pid, worker=row['worker_pid'])
                assert not job.contains_pid(runner_pid)
                assert not job.contains_pid(row['worker_pid'])
                evidence['runner_in_host_job'] = False
                assert not (tmp_path / 'work.marker').exists()
            else:
                assert message['reason'] in ('service_absent', 'service_unreachable')
                assert 'the ModelQuad background service is not running' in message['runner_note']
                assert message['probe_output'] == ANSWER
                evidence['fallback_note'] = message['runner_note']
                evidence['fallback_reason'] = message['reason']
                worker = wait_for(lambda: (tmp_path / 'work.started').read_text()
                                  if (tmp_path / 'work.started').exists() else None)
                evidence['pids']['worker'] = int(worker)
                assert job.contains_pid(int(worker))
                assert not (tmp_path / 'work.marker').exists()
            evidence['timings_seconds']['terminate_host'] = round(time.monotonic() - started, 3)
            job.terminate()
            assert host.wait(timeout=10) != 0
            evidence['host_dead'] = True
            if service_running:
                def observe():
                    row = history.read_job(database, job_id)
                    if row and not row['ended_at']:
                        assert row['runner_pid'] == runner_pid
                    return row
                final = wait_for(observe, lambda value: value and value['ended_at'] is not None)
                assert final['status'] == 'finished', final
                assert (tmp_path / 'work.marker').read_text(encoding='utf-8') == 'finished'
                output = default_store().collect(job_id, datetime.now(timezone.utc))
                assert output and output.output.output_text == ANSWER
                collector = launch('collect', tmp_path, env, job_id, stderr=stderr)
                try:
                    stdout, _ = collector.communicate(timeout=SECONDS)
                    assert collector.returncode == 0, stderr_path.read_text(encoding='utf-8')
                    collected = json.loads(stdout)
                    assert collected['pid'] not in (host.pid, service.pid, runner_pid)
                    evidence['new_host'] = collected
                    assert default_store().pending(datetime.now(timezone.utc)) == ()
                finally:
                    if collector.poll() is None:
                        collector.kill()
                        collector.wait(timeout=10)
                evidence['status'] = 'finished'
            else:
                wait_for(lambda: procutil.pid_state(evidence['pids']['worker']), lambda state: state == 'dead')
                # Observe past the worker's promised write time as well as death.
                wait_for(lambda: time.monotonic() - started, lambda elapsed: elapsed >= 10)
                assert not (tmp_path / 'work.marker').exists()
                assert default_store().pending(datetime.now(timezone.utc)) == ()
                evidence['worker_dead'] = True
                evidence['marker_absent'] = True
            evidence['result'] = 'passed'
    except BaseException as error:
        evidence['result'] = 'skipped' if isinstance(error, pytest.skip.Exception) else 'failed'
        evidence['error_type'] = type(error).__name__
        raise
    finally:
        if job:
            job.close()  # kill-on-close also cleans up any failed Host case.
        for process in (host, service):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                process.stdout.close()
        if runner_pid and procutil.process_creation_epoch(runner_pid) == runner_created:
            handle.stop_job(evidence['job_id'], db_path=database)
            procutil.terminate_pid(runner_pid)
        evidence['timings_seconds']['total'] = round(time.monotonic() - started, 3)
        if evidence.get('result') == 'failed' and stderr_path.exists():
            with stderr_path.open('rb') as log:
                log.seek(max(0, stderr_path.stat().st_size - 4096))
                print(log.read().decode('utf-8', errors='replace'))
        evidence_path.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        print(f'HL-1 evidence: {evidence_path}')
