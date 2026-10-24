# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Changed-files-off regressions and outcome compatibility; no real provider execution."""

from modelquad.execution import bound_launch

from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from modelquad import procutil
from modelquad.jobs import changes, runner
from modelquad.jobs.handle import JobSpec, PreparedDelegation
from modelquad.jobs.seam import JobRef
from modelquad.jobs.store import FileOutputStore
from test_jobs_changes import git, git_repo


def fail_observation(*args, **kwargs):
    raise AssertionError('disabled observation accessed Git or the filesystem')


@contextmanager
def observation_forbidden(monkeypatch):
    # Scope patches to the calls, after paths, snapshots and fixtures are built.
    calls = []

    def forbid(name):
        def record(*args, **kwargs):
            calls.append(name)
            fail_observation(*args, **kwargs)
        return record

    with monkeypatch.context() as guard:
        for target, name in (
            (changes.subprocess, 'Popen'), (changes, 'resolve_executable'),
            (changes, '_git'), (subprocess, 'Popen'), (os, 'lstat'),
            (os, 'stat'), (os, 'open'), (os, 'scandir'),
            (Path, 'lstat'), (Path, 'resolve'),
        ):
            guard.setattr(target, name, forbid(target.__name__ + '.' + name))
        yield
    # The old observation catches Exception, including AssertionError. Check
    # outside the guarded call so it cannot swallow evidence of forbidden I/O.
    assert not calls, 'disabled observation called: ' + ', '.join(calls)


@pytest.mark.parametrize('kind', ['normal', 'unborn', 'non_git', 'inaccessible'],
                         ids=['normal', 'unborn', 'non_git', 'inaccessible'])
def test_snapshot_disabled_never_launches_git(git_repo, tmp_path, monkeypatch, kind):
    directory = git_repo
    if kind == 'unborn':
        directory = tmp_path / 'unborn'
        directory.mkdir()
        git(directory, 'init')
    elif kind == 'non_git':
        directory = tmp_path / 'non-git'
        directory.mkdir()
    elif kind == 'inaccessible':
        directory = tmp_path / 'does-not-exist'
    with observation_forbidden(monkeypatch):
        assert changes.snapshot(directory) is None
        assert changes.snapshot(str(directory)) is None


def test_changed_files_disabled_rejects_legacy_snapshot_without_observation(tmp_path, monkeypatch):
    legacy = changes.Snapshot(tmp_path, tmp_path, '1' * 40, {'old.txt': b'fake-hash'})
    directory = str(tmp_path)
    with observation_forbidden(monkeypatch):
        assert changes.changed_files(legacy, directory) is None
        assert changes.changed_files(None, directory) is None


FILTER_SCRIPT = '''from pathlib import Path
import sys

Path('filter-ran').touch()
if sys.argv[1] == 'required':
    sys.exit(1)
if sys.argv[1] == 'clean':
    sys.stdout.buffer.write(sys.stdin.buffer.read())
    sys.exit(0)

source, target = sys.stdin.buffer, sys.stdout.buffer

def read_packet():
    header = source.read(4)
    if not header:
        return None
    size = int(header, 16)
    return b'' if size == 0 else source.read(size - 4)

def read_list():
    packets = []
    while True:
        packet = read_packet()
        if packet is None:
            return None
        if packet == b'':
            return packets
        packets.append(packet)

def send_list(packets):
    for packet in packets:
        target.write(('%04x' % (len(packet) + 4)).encode() + packet)
    target.write(b'0000')
    target.flush()

assert read_list() == [b'git-filter-client\\n', b'version=2\\n']
send_list([b'git-filter-server\\n', b'version=2\\n'])
assert read_list() is not None
send_list([b'capability=clean\\n', b'capability=smudge\\n'])
while read_list() is not None:
    content = read_list()
    assert content is not None
    send_list([b'status=success\\n'])
    send_list(content)
    send_list([])
'''


@pytest.mark.parametrize('kind', [
    'clean', 'process', 'required', 'included-config', 'submodule-clean', 'submodule-process',
], ids=['clean', 'process', 'required', 'included-config', 'submodule-clean', 'submodule-process'])
def test_filter_observation_is_disabled(git_repo, tmp_path, monkeypatch, kind):
    directory = git_repo
    if kind.startswith('submodule-'):
        directory = git_repo / 'module'
        module_git = git_repo / '.git' / 'modules' / 'module'
        module_git.parent.mkdir()
        # Construct the real gitfile/gitlink layout directly. The old
        # submodule-add shell dispatcher timed out during setup on Windows,
        # before any assertion; no larger timeout or retry masks that failure.
        git(git_repo, 'clone', '--separate-git-dir', str(module_git), str(git_repo), str(directory))
        git(directory, 'config', 'core.worktree', str(directory))
        git(git_repo, 'config', '-f', '.gitmodules', 'submodule.module.path', 'module')
        git(git_repo, 'config', '-f', '.gitmodules', 'submodule.module.url', str(git_repo))
        git(git_repo, 'config', 'submodule.module.url', str(git_repo))
        git(git_repo, 'config', 'submodule.module.active', 'true')
        head = git(directory, 'rev-parse', 'HEAD').decode('ascii').strip()
        git(git_repo, 'update-index', '--add', '--cacheinfo', '160000,' + head + ',module')
        git(git_repo, 'add', '.gitmodules')
        git(git_repo, 'commit', '-m', 'module baseline')
        git(directory, 'config', 'user.name', 'Fixture')
        git(directory, 'config', 'user.email', 'fixture@example.invalid')
    # Commit attributes before any filter is configured, including in modules.
    (directory / '.gitattributes').write_text('modified.txt filter=hostile\n', encoding='utf-8', newline='\n')
    git(directory, 'add', '.gitattributes')
    git(directory, 'commit', '-m', 'attributes baseline')
    (directory / 'filter.py').write_text(FILTER_SCRIPT, encoding='utf-8', newline='\n')
    mode = 'process' if kind.endswith('process') else 'required' if kind == 'required' else 'clean'
    # Git uses sh: single-quote the interpreter and keep the script relative.
    # In particular, no double-quoted tmp_path may expand the runner's '$'.
    command = shlex.quote(sys.executable.replace('\\', '/')) + ' filter.py ' + mode
    config_args = []
    if kind == 'included-config':
        included = tmp_path / 'filter.config'
        config_args = ['--file', str(included)]
        git(directory, 'config', 'include.path', str(included))
    driver = 'process' if mode == 'process' else 'clean'
    git(directory, 'config', *config_args, 'filter.hostile.' + driver, command)
    git(directory, 'config', *config_args, 'filter.hostile.required', 'true')
    (directory / 'modified.txt').write_text('modified filter input with a different size\n', encoding='utf-8', newline='\n')
    marker = directory / 'filter-ran'
    # A zero-stat index entry forces status to compare content through the
    # filter, even for the required driver that fails before an add could reset.
    git(directory, 'read-tree', 'HEAD')
    control_failed = False
    try:
        git(git_repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
            '--ignore-submodules=none')
    except subprocess.CalledProcessError as error:
        control_failed = True
        stderr = error.stderr.decode('utf-8', 'replace')
        if not marker.exists() and any(reason in stderr.lower() for reason in (
            'permission denied', 'cannot run', 'not found', 'not recognized',
        )):
            pytest.skip('Hostile-filter positive control unavailable: ' + stderr)
        if kind != 'required':
            raise
    assert marker.exists(), 'positive control did not execute the configured filter'
    assert control_failed is (kind == 'required')
    # Do not let the control's index refresh suppress the observation's filter.
    git(directory, 'read-tree', 'HEAD')
    marker.unlink()
    legacy = changes.Snapshot(git_repo, git_repo, '1' * 40, {'modified.txt': b'baseline'})
    observed_snapshot = changes.snapshot(git_repo)
    observed_files = changes.changed_files(legacy, git_repo)
    assert not marker.exists(), 'disabled observation executed the configured filter'
    assert observed_snapshot is None
    assert observed_files is None
    with observation_forbidden(monkeypatch):
        assert changes.snapshot(git_repo) is None
        assert changes.changed_files(legacy, git_repo) is None
    assert not marker.exists()


RUNNER_CASES = [(mode, ending, '') for mode in ('raw', 'delegated')
                for ending in ('finished', 'failed', 'stopped', 'timed_out', 'interrupted')]
RUNNER_CASES += [('raw', 'stopped', 'retained output'), ('delegated', 'interrupted', 'retained output')]
RUNNER_IDS = [mode + '-' + ending.replace('_', '-') + ('-output' if output else '')
              for mode, ending, output in RUNNER_CASES]


@pytest.mark.parametrize('mode,ending,text', RUNNER_CASES, ids=RUNNER_IDS)
def test_runner_preserves_outcomes_without_change_observation(tmp_path, monkeypatch, mode, ending, text):
    # Outcome compatibility only: this non-Git folder also returned None before the change.
    from modelquad import config as config_module
    from modelquad.config import Config
    from modelquad.execution import manager as manager_module
    from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
    from modelquad.execution.lifecycle import TerminalResolution
    from modelquad.mcp import server as mcp

    fixed_time = datetime(2026, 10, 7, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_time

    monkeypatch.setattr(runner, 'datetime', FixedDatetime)
    label = '551-' + mode + '-' + ending
    ref = JobRef(label, 551, label + '-decision')
    spec = JobSpec((sys.executable, '-c', 'pass'), str(tmp_path), str(tmp_path / 'unused.sqlite'),
                   ref.attempt_id, 5)
    completed_text = '```json\n{"task_status":"completed","reason":"fixture"}\n```'
    expected_text = completed_text if mode == 'raw' and ending == 'finished' else text
    resolution = TerminalResolution('completed', None) if ending == 'finished' else TerminalResolution(
        'failed', {'failed': 'exception', 'stopped': 'timeout', 'timed_out': 'timeout',
                   'interrupted': 'orphaned'}[ending])
    termination = {'confirmed_dead': True, 'tree_kill': 'census_unknown'}
    descendants = {'enumerated': True, 'reason': None, 'method': 'fixture',
                   'observed_at': fixed_time.timestamp(), 'descendants': [], 'truncated': False}
    store = FileOutputStore(tmp_path / 'pending')
    if mode == 'raw':
        monkeypatch.setattr(runner.history, 'attempt_stop_requested', lambda *a: False)

        def worker(*args, **kwargs):
            if ending in {'stopped', 'timed_out'}:
                error = (procutil.WorkerStopRequested([], stdout=text) if ending == 'stopped'
                         else subprocess.TimeoutExpired([], 5, output=text))
                error.worker_termination = termination
                raise error
            if ending == 'failed':
                raise OSError('synthetic launch failure')
            completed = subprocess.CompletedProcess([], 0, expected_text, '')
            completed.worker_termination = termination
            completed.worker_descendants = descendants
            return completed

        monkeypatch.setattr(procutil, 'run_with_tree_kill', worker)
        result = runner._execute(spec, ref)
        output = runner._output(result, ref, fixed_time, resolution,
                                observed_changes=getattr(result, '_job_changed_files', None))
        store.put(output, output.ended_at)
    else:
        values = {field.name: None for field in fields(PreparedDelegation)}
        values.update(task_type='implementation', prompt='fixture', working_directory=str(tmp_path),
                      provider='codex', statuses=[], consumption={'bucket': 'low'}, timeout=5,
                      decision_id=ref.decision_id, attempt_id=ref.attempt_id, db_path=spec.db_path,
                      config_path=str(tmp_path / 'no-config'), user_approved=False,
                      mode='normal', paused=False, usage_directory=str(tmp_path), host_snapshot={},
                      lineage={'decision_id': ref.decision_id, 'decision_id_source': 'caller_supplied',
                               'prior_route': True, 'notes': []}, decision_mode='deterministic',
                      reasoning_fell_back=False, capability='delegate')
        spec = replace(spec, delegation=PreparedDelegation(**values))
        monkeypatch.setattr(runner, 'output_sink_factory', lambda name: store)
        monkeypatch.setattr(config_module, 'load_config', lambda path: Config())
        monkeypatch.setattr(manager_module, 'ExecutionManager', lambda *a, **kw: SimpleNamespace())
        monkeypatch.setattr(bound_launch, '_bind_launch', lambda *a, **kw: None)
        # Avoid changing global service state: all provider/lifecycle seams are fake.
        result = ExecutionResult('codex', ending == 'finished', text, 0 if ending == 'finished' else None,
                                 metadata={'stop_requested': ending == 'stopped',
                                           'timed_out': ending == 'timed_out',
                                           'worker_termination': termination, 'worker_descendants': descendants},
                                 task_status=WorkerTaskStatus.COMPLETED, task_status_reason='fixture')
        monkeypatch.setattr(bound_launch, 'run_bound_delegation',
                            lambda *a, **kw: kw['publish'](result, resolution))
        assert runner._run_delegation(spec, ref, fixed_time, {}) == 0
    collected = store.collect(ref.job_id, fixed_time)
    assert collected is not None
    output = collected.output
    assert output.job == ref
    assert output.job_status.value == ending
    assert output.changed_files is None
    assert output.output_text == expected_text
    assert output.partial is (bool(expected_text) and ending in {'stopped', 'timed_out', 'interrupted'})
    expected_claim = (WorkerTaskStatus.COMPLETED if mode == 'delegated' or ending == 'finished'
                      else WorkerTaskStatus.UNKNOWN)
    assert output.worker_claim is (WorkerTaskStatus.UNKNOWN if ending == 'interrupted' else expected_claim)
    expected_reason = ('fixture' if mode == 'delegated' or ending == 'finished' else
                       {'failed': 'launch_failed', 'stopped': 'stop_requested', 'timed_out': None,
                        'interrupted': None}[ending])
    assert output.claim_reason == ('interrupted' if ending == 'interrupted' else expected_reason)
    assert output.termination == (None if mode == 'raw' and ending == 'failed' else termination)
    expected_descendants = dict(descendants, descendants=())
    assert output.descendants == (expected_descendants if mode == 'delegated' or ending in {'finished', 'interrupted'} else None)
    assert output.tree_confirmed_dead is (None if mode == 'raw' and ending == 'failed' else False)
    assert output.started_at == fixed_time
    assert output.ended_at >= output.started_at
    assert 'changed_files' not in result.metadata


def test_raw_stopped_retains_output_through_run_job(git_repo, tmp_path, monkeypatch):
    from modelquad.execution.base import WorkerTaskStatus
    from modelquad.service import history

    db_path = tmp_path / 'jobs.sqlite'
    attempt = history.record_delegation_attempt(
        db_path, task_type='implementation', decision_id='551-raw-stopped-output-decision')
    spec = JobSpec((sys.executable, '-c', 'pass'), str(git_repo), str(db_path), attempt, 5)
    row = history.reserve_job(db_path, '551-raw-stopped-output', attempt)
    spec_path = tmp_path / 'spec.json'
    spec_path.write_text(json.dumps(asdict(spec)), encoding='utf-8')
    store = FileOutputStore(tmp_path / 'pending')
    monkeypatch.setattr(runner, 'output_sink_factory', lambda name: store)
    # A raw-argv spec is a synthetic fixture; installed-mode runs need the opt-in (F-1008-2).
    monkeypatch.setenv('MODELQUAD_TEST_SINK_OK', '1')
    # Raw execution is fake; declare the corresponding runner observation.
    monkeypatch.setattr(runner, 'runner_containment_verdict', lambda: (True, 'not_in_job'))

    def worker(*args, **kwargs):
        assert kwargs['cwd'] == str(git_repo)
        (git_repo / 'modified.txt').write_text('synthetic partial edit', encoding='utf-8')
        raise procutil.WorkerStopRequested([], stdout='retained output')

    monkeypatch.setattr(procutil, 'run_with_tree_kill', worker)
    assert runner.run_job(row['job_id'], spec_path) == 0
    collected = store.collect(row['job_id'], datetime.now(timezone.utc))
    assert collected is not None
    output = collected.output
    assert output.job_status.value == 'stopped'
    assert output.worker_claim is WorkerTaskStatus.UNKNOWN
    assert output.claim_reason == 'stop_requested'
    assert output.output_text == 'retained output'
    assert output.changed_files is None
    assert output.partial is True
