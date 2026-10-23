# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Private job specs at creation; native execution belongs to a maintainer.

The independent H1 reader compares trustees by SID, including SDDL aliases.
Every Windows test uses an inheritable broad parent DACL, reset at teardown.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from orchestrator.adapters import file_security
from orchestrator.jobs import handle
from orchestrator.service import jobs_api
from test_adapter_file_security import (
    _assert_exclusive, _assert_private, _broad_folder, _read,
    acl_tree, native, user,
)


windows = pytest.mark.skipif(os.name != 'nt', reason='Requires native Windows DACLs')
CANARY = 'private-transient-prompt-sec555-canary'


@pytest.fixture
def spec(tmp_path, request):
    directory = tmp_path / 'spec $ \u05e9 directory'
    directory.mkdir()
    if os.name == 'nt':
        _broad_folder(request.getfixturevalue('acl_tree'), directory,
                      request.getfixturevalue('user'))
    return handle.JobSpec((sys.executable, '-c', 'pass', CANARY, '\u05e9'),
                          str(tmp_path), str(directory / 'history.db'), 1, 3,
                          env={'SPEC_CANARY': CANARY})


def _spec_paths(spec):
    return list(Path(spec.db_path).parent.glob('job-*.json'))


def _assert_name(path, spec):
    assert path.parent == Path(spec.db_path).parent
    assert jobs_api.SPEC_NAME.fullmatch(path.name) is not None


@windows
def test_spec_is_private_from_creation(spec, native, user, monkeypatch):
    """Fails on base handle.py: inherited broad access survives the no-op."""
    import msvcrt

    # The regression must detect the race, even without path-based protection.
    monkeypatch.setattr(handle, '_protect_spec', lambda path: None)
    real_fdopen = os.fdopen
    inspected = []

    def before_write(descriptor, *args, **kwargs):
        path, = _spec_paths(spec)
        _assert_name(path, spec)
        assert os.fstat(descriptor).st_size == 0
        _assert_private(_read(native, handle=msvcrt.get_osfhandle(descriptor)), user)
        _assert_exclusive(native, path)
        inspected.append(path)
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, 'fdopen', before_write)
    path = handle._write_spec(spec)
    assert inspected == [path]
    _assert_private(_read(native, path), user)  # GetNamedSecurityInfoW after close.


def test_spec_content_round_trips(spec):
    path = handle._write_spec(spec)
    _assert_name(path, spec)
    # This is the runner's exact read and JobSpec reconstruction.
    assert handle.JobSpec(**json.loads(path.read_text(encoding='utf-8'))) == spec
    assert path.read_bytes() == handle.serialized_spec(spec)
    if os.name == 'nt':
        assert handle.JobSpec(**json.loads(jobs_api._read_private_spec(path))) == spec


def test_relative_db_path_keeps_absolute_returned_spec_path(spec, tmp_path, monkeypatch):
    relative = replace(spec, db_path=str(Path(spec.db_path).relative_to(tmp_path)))
    monkeypatch.chdir(tmp_path)
    path = handle._write_spec(relative)
    assert path.is_absolute()
    _assert_name(path, spec)
    assert handle.JobSpec(**json.loads(path.read_text(encoding='utf-8'))) == relative


@pytest.mark.parametrize('failure', [
    'creation', 'open_osfhandle', 'open_osfhandle_oserror', 'fdopen',
    'write', 'flush', 'interrupt',
])
def test_failure_leaves_no_partial_file(spec, monkeypatch, failure):
    if failure.startswith('open_osfhandle') and os.name != 'nt':
        pytest.skip('CRT native handle transfer is Windows-only')
    error = KeyboardInterrupt('Injected spec interruption') if failure in (
        'interrupt', 'open_osfhandle') else OSError('Injected spec failure')
    real_fdopen = os.fdopen
    descriptors = []

    def refused(*args, **kwargs):
        raise error

    class BrokenWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def write(self, contents):
            self.stream.write(contents[:2])
            if failure != 'flush':
                self.stream.flush()
                raise error

        def flush(self):
            raise error

        def __exit__(self, *args):
            self.stream.close()

    def fdopen(descriptor, *args, **kwargs):
        descriptors.append(descriptor)
        if failure == 'fdopen':
            raise error
        return BrokenWriter(real_fdopen(descriptor, *args, **kwargs))

    if failure == 'creation':
        if os.name == 'nt':
            monkeypatch.setattr(file_security, '_open', refused)
        else:
            monkeypatch.setattr(os, 'open', refused)
    elif failure.startswith('open_osfhandle'):
        import msvcrt
        monkeypatch.setattr(msvcrt, 'open_osfhandle', refused)
    else:
        monkeypatch.setattr(os, 'fdopen', fdopen)
    with pytest.raises(type(error)) as raised:
        handle._write_spec(spec)
    # H1 translates native CRT OSErrors to its content-free OSError subtype.
    if failure != 'open_osfhandle_oserror':
        assert raised.value is error
    assert CANARY not in str(raised.value)
    assert not _spec_paths(spec)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@windows
@pytest.mark.parametrize('failure', ['volume', 'read', 'unprotected', 'null', 'entries'])
def test_unverified_dacl_refuses_content(spec, monkeypatch, failure):
    if failure == 'volume':
        monkeypatch.setattr(file_security, 'persistent_acls', lambda native: False)
    elif failure == 'read':
        def refused(native):
            raise OSError('Injected DACL read failure')
        monkeypatch.setattr(file_security, 'read_dacl', refused)
    else:
        real_read = file_security.read_dacl

        def invalid(native):
            actual = real_read(native)
            if failure == 'unprotected':
                return replace(actual, protected=False)
            if failure == 'null':
                return file_security.Dacl(True, None, None)
            return replace(actual, aces=actual.aces + (b'unexpected trustee',))

        monkeypatch.setattr(file_security, 'read_dacl', invalid)

    def must_not_write(*args, **kwargs):
        pytest.fail('Unverified protection must refuse content before fdopen')

    monkeypatch.setattr(os, 'fdopen', must_not_write)
    with pytest.raises(OSError) as raised:
        handle._write_spec(spec)
    assert CANARY not in str(raised.value)
    assert not _spec_paths(spec)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permissions are not Windows DACLs')
def test_posix_mode_is_0600(spec):
    """Regression guard: mkstemp at base already provided this POSIX mode."""
    path = handle._write_spec(spec)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_name_collision_retries_without_touching_existing_file(spec, monkeypatch):
    directory = Path(spec.db_path).parent
    occupied = directory / 'job-aaaaaaaa.json'
    occupied.write_bytes(b'existing')
    monkeypatch.setattr(handle.tempfile, '_get_candidate_names',
                        lambda: iter(('aaaaaaaa', 'bbbbbbbb')))
    path = handle._write_spec(spec)
    assert path.name == 'job-bbbbbbbb.json'
    _assert_name(path, spec)
    assert occupied.read_bytes() == b'existing'


def test_name_collision_retries_are_bounded(spec, monkeypatch):
    directory = Path(spec.db_path).parent
    occupied = directory / 'job-aaaaaaaa.json'
    occupied.write_bytes(b'existing')
    candidates = []

    def names():
        for index in range(101):
            candidates.append(index)
            yield 'aaaaaaaa'

    monkeypatch.setattr(handle.tempfile, '_get_candidate_names', names)
    with pytest.raises(FileExistsError) as raised:
        handle._write_spec(spec)
    assert len(candidates) == 100
    assert CANARY not in str(raised.value)
    assert _spec_paths(spec) == [occupied]
    assert occupied.read_bytes() == b'existing'


def test_serialization_failure_precedes_creation(spec, monkeypatch):
    error = ValueError('review_spec_limits')

    def refused(spec):
        raise error

    monkeypatch.setattr(handle, 'serialized_spec', refused)
    with pytest.raises(ValueError) as raised:
        handle._write_spec(spec)
    assert raised.value is error
    assert CANARY not in str(raised.value)
    assert not _spec_paths(spec)


def test_cleanup_closes_before_unlink_and_preserves_original_error(spec, monkeypatch):
    error = KeyboardInterrupt('Injected wrapper interruption')
    real_unlink = Path.unlink
    descriptors = []
    attempted = []

    def fdopen(descriptor, *args, **kwargs):
        descriptors.append(descriptor)
        raise error

    def refused_unlink(path, *args, **kwargs):
        if path.parent == Path(spec.db_path).parent:
            for descriptor in descriptors:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
            attempted.append(path)
            raise OSError('Injected unlink refusal')
        return real_unlink(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fdopen', fdopen)
            patch.setattr(Path, 'unlink', refused_unlink)
            with pytest.raises(KeyboardInterrupt) as raised:
                handle._write_spec(spec)
            assert raised.value is error
            assert CANARY not in str(raised.value)
            assert attempted == _spec_paths(spec)
            assert len(attempted) == 1
    finally:
        for path in _spec_paths(spec):
            path.unlink()
