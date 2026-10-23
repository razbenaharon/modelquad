# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Private synthetic-test diagnostics; never imported by the product.

Capture files avoid waiting on a pipe inherited by a descendant. Deadlines,
assertions and admission decisions remain the caller's responsibility.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path
import subprocess
import time

import pytest

from conftest import no_window_kwargs


def runner_driver_command(bootstrap, arguments, *, command=None):
    """Test-only instrumentation using the runner's admitted owning Python.

    Avoid the Windows venv redirector's extra killing job, just as production
    does. Restore the same installation before importing the instrumented code;
    explicit test import roots provide only the synthetic executor/diagnostics.
    No containment verdict or startup check is replaced.
    """
    from orchestrator.jobs import handle

    if command is None:
        command = handle._runner_command('fixture', 'fixture.json', 'fixture.db')
    if command[1:3] == ['-m', 'orchestrator.jobs.runner']:
        return [command[0], '-c', bootstrap, *arguments]
    assert command[1:3] == ['-I', '-S'] and command[4] == '--installation-python', command
    root = str(Path(command[3]).resolve().parents[2])
    prelude = (
        'import os, sys\n'
        f'sys.path.insert(0, {root!r})\n'
        'from orchestrator.service.entry import _restore_installation\n'
        f'_restore_installation({command[5]!r})\n'
        # -I intentionally ignores PYTHONPATH; synthetic fixtures explicitly
        # restore their own absolute roots after installation verification.
        'sys.path[:0] = [os.path.abspath(p) for p in '
        "os.environ.get('PYTHONPATH', '').split(os.pathsep) if p]\n"
    )
    return [command[0], '-I', '-S', '-c', prelude + bootstrap, *arguments]


@contextmanager
def non_killing_runner_job(*, resume):
    """Launch test-owned runners into a measured, non-killing innermost job.

    Outer harness jobs remain unknown. Assignment occurs while suspended and
    is verified before resume; the real runner measures/records the limitation.
    The worker's own kill-on-close job remains untouched. Closing this fixture
    handle happens only after its runners have exited.
    """
    from orchestrator import procutil

    popen = subprocess.Popen
    if os.name != 'nt':
        yield popen
        return
    job = procutil._create_census_job(kill_on_close=False)
    assert job, 'test runner job unavailable'
    children = []

    def spawn(command, **kwargs):
        kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | 0x4
        child = popen(command, **kwargs)
        children.append(child)
        assert procutil._assign_to_census_job(job, child.pid), 'test runner assignment failed'
        assert procutil._process_in_job(child.pid, job) is True, 'test runner assignment unverified'
        if resume:
            assert procutil._resume_suspended_process(child.pid), 'test runner resume failed'
        return child

    try:
        yield spawn
    finally:
        try:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
        finally:
            procutil._close_census_job(job)


@pytest.fixture
def non_killing_runner_spawn():
    with non_killing_runner_job(resume=True) as spawn:
        yield spawn


@pytest.fixture
def non_killing_service_spawn():
    # launch_reserved owns recording identity and resuming this child.
    with non_killing_runner_job(resume=False) as spawn:
        yield spawn


def process_snapshot(pid):
    """Read identities/status only; no command lines or environment contents."""
    from orchestrator import procutil

    if os.name != "nt":
        return [{"pid": pid, "unavailable": "unsupported_platform"}]
    try:
        table = procutil._windows_process_snapshot()
        result = [{"pid": pid, "created": procutil.process_creation_epoch(pid),
                   "status": procutil.pid_state(pid)}]
        if table is None:
            result[0]["census"] = "unknown"
            return result
        selected = {pid}
        for _ in range(31):
            child = next(((child, parent) for child, parent, _image in table
                          if parent in selected and child not in selected), None)
            if child is None:
                break
            child_pid, parent_pid = child
            selected.add(child_pid)
            result.append({"pid": child_pid, "parent": parent_pid,
                           "created": procutil.process_creation_epoch(child_pid),
                           "status": procutil.pid_state(child_pid)})
        return result
    except Exception as error:
        return [{"pid": pid, "unavailable": type(error).__name__}]


def capture_text(path):
    """Only the tail enters the failure message; the whole file stays local."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 8192))
            return stream.read(8192).decode("utf-8", "replace")
    except OSError as error:
        return type(error).__name__


def note_failure(error, collect):
    """Diagnostics must never replace the assertion or exception that failed."""
    try:
        error.add_note(json.dumps(collect()))
    except Exception as diagnostic_error:
        error.add_note("diagnostics_unavailable:" + type(diagnostic_error).__name__)


def run_logged(command, *, cwd, env, timeout, prefix):
    """Run one test-owned child with the original deadline and retained output."""
    prefix = Path(prefix)
    stdout_path = prefix.with_suffix(".stdout.log")
    stderr_path = prefix.with_suffix(".stderr.log")
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        with subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                              **no_window_kwargs()) as child:
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                observation = {"pid": child.pid, "exit_before_cleanup": child.poll(),
                               "elapsed_seconds": time.monotonic() - started,
                               "processes": process_snapshot(child.pid)}
                error.add_note(json.dumps(observation))
                error.add_note("stdout=" + capture_text(stdout_path))
                error.add_note("stderr=" + capture_text(stderr_path))
                # Kill only the child we own. Never signal a census result.
                child.kill()
                child.wait(timeout=5)
                raise
    return subprocess.CompletedProcess(command, child.returncode,
                                       capture_text(stdout_path), capture_text(stderr_path))


def instrument_runner_command(command, stages_path):
    """Observe a Python runner without changing its spec or launch policy.

    Each wrapper calls the original function exactly once, with identical
    arguments/results. Startup and containment failures propagate unchanged.
    No provider, report module, test module or environment dump is introduced.
    """
    module = "orchestrator.jobs.runner"
    if command[1:3] == ['-I', '-S']:
        assert command[4] == '--installation-python', command
        arguments = command[6:]
    else:
        assert command[1:3] == ["-m", module], command
        arguments = command[3:]
    bootstrap = f"""
import faulthandler, functools, importlib, json, os, sys, time
stage_file = open({str(stages_path)!r}, 'a', encoding='utf-8')
def emit(stage, **facts):
    stage_file.write(json.dumps(dict(stage=stage, monotonic=time.monotonic(),
                                    pid=os.getpid(), **facts)) + '\\n')
    stage_file.flush()
emit('python_entry')
faulthandler.enable()
faulthandler.dump_traceback_later(8, repeat=True)
try:
    emit('runner_import_begin')
    runner = importlib.import_module({module!r})
    emit('runner_import_end', source=runner.__file__)
    try:
        handle = importlib.import_module('orchestrator.jobs.handle')
        in_job = handle._caller_in_job()
        emit('containment', in_job=in_job,
             limits=handle._caller_job_limits() if in_job else 0)
    except Exception as error:
        emit('containment_unavailable', error=type(error).__name__)
    def observe(owner, name):
        original = getattr(owner, name)
        @functools.wraps(original)
        def observed(*args, **kwargs):
            stage = owner.__name__ + '.' + name
            emit(stage + ':call')
            try:
                result = original(*args, **kwargs)
            except BaseException as error:
                emit(stage + ':error', error=type(error).__name__)
                raise
            emit(stage + ':return', result=result if type(result) is bool else None)
            return result
        setattr(owner, name, observed)
    for owner, names in (
        (runner, ('_execute', '_finish_job', '_synthetic_jobs_allowed')),
        (runner.history, ('require_launch_attempt', 'update_job_startup',
                          'resolve_delegation_attempt')),
        (runner.procutil, ('run_with_tree_kill', '_contain_suspended_worker',
                          '_create_census_job', '_assign_to_census_job',
                          '_resume_suspended_process')),
    ):
        for name in names:
            observe(owner, name)
    emit('runner_main_begin')
    code = runner.main(sys.argv[1:])
    emit('runner_main_end', exit_code=code)
    raise SystemExit(code)
except BaseException as error:
    emit('python_exit', error=type(error).__name__,
         exit_code=error.code if isinstance(error, SystemExit) and type(error.code) is int else None)
    raise
finally:
    faulthandler.cancel_dump_traceback_later()
    stage_file.close()
"""
    return runner_driver_command(bootstrap, arguments, command=command)
