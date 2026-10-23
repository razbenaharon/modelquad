# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Shared synthetic repositories and metadata Git hardening."""

import os
import shutil
import subprocess
import time

import pytest

from conftest import no_window_kwargs
from modelquad.jobs import changes


def git(directory, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, *args], cwd=directory,
                          env=env, check=True, capture_output=True, timeout=300,  # fixture setup; preflight headroom
                          **no_window_kwargs()).stdout


@pytest.fixture
def git_repo(tmp_path):
    if not shutil.which('git'):
        pytest.skip('Git unavailable')
    directory = tmp_path / 'repo'
    directory.mkdir()
    git(directory, 'init')
    git(directory, 'config', 'user.name', 'Fixture')
    git(directory, 'config', 'user.email', 'fixture@example.invalid')
    for name in ('modified.txt', 'deleted.txt', 'predirty.txt', 'again.txt', 'nested/old.txt'):
        path = directory / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('original\n', encoding='utf-8', newline='\n')
    git(directory, 'add', '.')
    git(directory, 'commit', '-m', 'initial')
    return directory


def test_metadata_git_call_is_hardened_and_windowless(git_repo, monkeypatch):
    calls = []
    original = changes.subprocess.Popen
    def record(command, **kwargs):
        calls.append((command, kwargs))
        return original(command, **kwargs)
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_TRACE', 'GIT_CONFIG_COUNT'):
        monkeypatch.setenv(name, 'secret-invalid-value')
    monkeypatch.setattr(changes.subprocess, 'Popen', record)
    assert changes._git(git_repo, ['rev-parse', 'HEAD'], time.monotonic() + 20).strip()
    assert len(calls) == 1
    for command, kwargs in calls:
        env = kwargs['env']
        assert {k: v for k, v in env.items() if k.upper().startswith('GIT_')} == {
            'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'}
        for config in ('core.fsmonitor=false', 'core.untrackedCache=false', 'diff.external=',
                       'core.hooksPath=' + os.devnull, 'filter.lfs.process=', 'filter.lfs.clean=',
                       'filter.lfs.smudge=', 'filter.lfs.required=false'):
            assert config in command
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['cwd'] == git_repo
        if os.name == 'nt':
            assert kwargs['creationflags'] & 0x08000000
