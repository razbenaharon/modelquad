# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Deterministic checks for the test-only lock and handshake helpers."""

from io import BytesIO, StringIO
from types import SimpleNamespace

import pytest

import load_helpers as helpers


def test_temp_permission_retries_only_the_failed_operation(monkeypatch):
    waits, calls = [], []
    monkeypatch.setattr(helpers, 'sleep', waits.append)

    def write():
        calls.append('write')
        if len(calls) < 3:
            raise PermissionError('transient temp lock')
        return 'written'

    assert helpers.retry_temp_permission(write) == 'written'
    assert calls == ['write'] * 3 and waits == [0.05] * 2


def test_temp_permission_preserves_persistent_error_at_deadline(monkeypatch):
    ticks = iter((0.0, 0.0, 10.0))
    monkeypatch.setattr(helpers, 'monotonic', lambda: next(ticks))
    waits, calls = [], []
    monkeypatch.setattr(helpers, 'sleep', waits.append)
    error = PermissionError('persistent temp lock')

    def write():
        calls.append('write')
        raise error

    with pytest.raises(PermissionError) as info:
        helpers.retry_temp_permission(write)
    assert info.value is error
    assert len(calls) == 2 and waits == [0.05]


def test_temp_permission_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(helpers, 'sleep', lambda _: pytest.fail('must not retry'))
    error = FileNotFoundError('missing fixture')
    with pytest.raises(FileNotFoundError) as info:
        helpers.retry_temp_permission(lambda: (_ for _ in ()).throw(error))
    assert info.value is error


def test_ready_line_does_not_wait_for_child_exit():
    child = SimpleNamespace(stdout=StringIO('ready\n'),
                            poll=lambda: pytest.fail('ready child must stay running'))
    assert helpers.read_child_line(child, BytesIO()) == 'ready\n'


def test_eof_reports_stderr_and_returncode():
    waits = []
    child = SimpleNamespace(stdout=StringIO(''), pid=123, returncode=7,
                            poll=lambda: 7, wait=lambda timeout: waits.append(timeout))
    with pytest.raises(AssertionError, match='hit EOF') as info:
        helpers.read_child_line(child, BytesIO(b'failed synthetic import'))
    assert 'returncode=7' in str(info.value)
    assert 'failed synthetic import' in str(info.value)
    assert waits == [60]


def test_handshake_deadline_kills_only_the_owned_child(monkeypatch):
    deadlines, kills, waits = [], [], []

    class TimedOutQueue:
        def put(self, _line):
            pass

        def get(self, timeout):
            deadlines.append(timeout)
            raise helpers.Empty()

    child = SimpleNamespace(stdout=StringIO(''), pid=123, returncode=None)

    def kill():
        kills.append(child.pid)
        child.returncode = -9

    child.poll = lambda: child.returncode
    child.kill = kill
    child.wait = lambda timeout: waits.append(timeout)
    monkeypatch.setattr(helpers, 'Queue', TimedOutQueue)
    with pytest.raises(AssertionError, match='timed out') as info:
        helpers.read_child_line(child, BytesIO(b'stalled import'))
    assert deadlines == [60] and kills == [123] and waits == [60]
    assert 'returncode=-9' in str(info.value) and 'stalled import' in str(info.value)
