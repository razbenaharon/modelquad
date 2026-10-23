# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""W04 retained-answer evidence and disk-full recovery; temporary roots only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import zlib

import pytest

from conftest import no_window_kwargs
from modelquad.execution.base import DESCENDANTS_RECORDED, WorkerTaskStatus
from modelquad.jobs import handle
from modelquad.jobs.seam import JobOutput, JobRef, JobStatus, RETENTION
from modelquad.jobs import store as storage
from modelquad.jobs.store import FileOutputStore, PendingOutputCapacityUnavailable
from modelquad.mcp import jobs_tools, server
from modelquad.service import db, history


NOW = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)


def answer(job_id='retained', *, attempt_id=1, text='durable answer'):
    return JobOutput(
        JobRef(job_id, attempt_id, None), JobStatus.FINISHED,
        WorkerTaskStatus.COMPLETED, None, text, False, None,
        {'confirmed_dead': True, 'tree_kill': 'ok'},
        {'enumerated': True, 'truncated': False, 'descendants': []},
        NOW - timedelta(seconds=1), NOW, tree_confirmed_dead=True,
    )


def disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, 'synthetic disk full')


def project(database):
    """The workspace the terminal_job attempt was delegated into."""
    return str(Path(database).parent / 'project')


def after_scope_check(replacement):
    """Replace history.read_job everywhere except the job tools' workspace scope check.

    Job tools first confirm the job belongs to the caller's project; these
    tests exercise what happens to a history read after that check passed.
    """
    real = history.read_job

    def read_job(*args, **kwargs):
        if sys._getframe(1).f_code.co_name == '_scoped_job':
            return real(*args, **kwargs)
        return replacement(*args, **kwargs)
    return read_job


@pytest.fixture
def terminal_job(tmp_path, monkeypatch):
    database = tmp_path / 'history.db'
    db.connect_and_init(database).close()
    workspace = Path(project(database))
    workspace.mkdir()
    attempt = history.record_delegation_attempt(database, task_type='implementation',
                                                working_directory=workspace)
    row = history.reserve_job(database, 'retained', attempt)
    history.update_job(database, row['job_id'], status='finished', ended_at=NOW.isoformat())
    store = FileOutputStore(tmp_path / 'answers')
    monkeypatch.setattr(db, 'DEFAULT_DB_PATH', database)
    monkeypatch.setattr(jobs_tools, 'default_store', lambda: store)
    monkeypatch.setattr(jobs_tools, 'schedule_recovery', lambda *args, **kwargs: None)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(jobs_tools, 'datetime', Clock)
    return database, store, answer(attempt_id=attempt)


def terminal_response(surface, database, item, tmp_path, monkeypatch):
    if surface == 'collect':
        return jobs_tools.modelquad_job_collect(item.job.job_id, project(database))
    if surface == 'wait':
        return jobs_tools.modelquad_job_wait(item.job.job_id, project(database))
    # Exercise the delegate's actual terminal branch, including job_status.
    # Only reservation and service launch are synthetic; no CLI is executed.
    prepared = SimpleNamespace(
        working_directory=str(tmp_path), db_path=str(database),
        attempt_id=item.job.attempt_id, timeout=10.0, provider='codex',
        agent_runtime=None, capability='delegate', decision_id=None,
        action_authority=None, prompt='synthetic request',
    )
    reserved = SimpleNamespace(ref=item.job, spec_path=tmp_path / 'unused-spec')
    monkeypatch.setattr(handle, 'reserve_and_write', lambda *args, **kwargs: reserved)
    client = SimpleNamespace(launch=lambda *args: ('launched', 'launched'))
    token = server._launch_call.set(None)
    try:
        return server._start_prepared_job(
            prepared, client, lambda *args: pytest.fail('unexpected in-session fallback'))
    finally:
        server._launch_call.reset(token)


@pytest.mark.parametrize('surface', ['delegate', 'wait', 'collect'])
@pytest.mark.parametrize('fate,expected', [
    ('true', True), ('false', False), ('unknown', None),
    ('outside_descendant', None), ('recovered_restore', None),
    ('launch_limitation', None), ('incomplete_census', None),
    ('missing_termination', None), ('truncated_census', None),
], ids=['true', 'false', 'unknown', 'outside_descendant', 'recovered_restore',
        'launch_limitation', 'incomplete_census', 'missing_termination', 'truncated_census'])
def test_terminal_surfaces_preserve_fate(
    terminal_job, tmp_path, monkeypatch, surface, fate, expected,
):
    database, store, item = terminal_job
    if fate == 'false':
        item = replace(item, tree_confirmed_dead=False)
    elif fate == 'unknown':
        item = replace(item, tree_confirmed_dead=None, descendants=None, termination=None)
    elif fate == 'outside_descendant':
        item = replace(item, descendants={
            'enumerated': True, 'truncated': False,
            'descendants': [{'pid': 987654, 'image': 'private-worker.exe',
                             'created': 1.0, 'path': 'C:/private/path'}],
        })
    elif fate == 'recovered_restore':
        with sqlite3.connect(database) as connection:
            connection.execute('UPDATE jobs SET recovery_decision=? WHERE job_id=?',
                               ('restore', item.job.job_id))
    elif fate == 'launch_limitation':
        # Seed a limitation retained from launch. update_job correctly refuses
        # changing a terminal row, which this fixture has already created.
        with sqlite3.connect(database) as connection:
            connection.execute('UPDATE jobs SET launch_limitation=? WHERE job_id=?',
                               ('contained_innermost_job_without_kill_on_close', item.job.job_id))
        assert history.read_job(database, item.job.job_id)['launch_limitation'] is not None
    elif fate == 'incomplete_census':
        item = replace(item, descendants={'enumerated': False, 'truncated': False,
                                         'descendants': []})
    elif fate == 'missing_termination':
        item = replace(item, termination=None)
    elif fate == 'truncated_census':
        item = replace(item, descendants={'enumerated': True, 'truncated': True,
                                         'descendants': [{'pid': n} for n in range(100)]})
    store.put(item, NOW)
    first = store.collect(item.job.job_id, NOW)
    response = terminal_response(surface, database, item, tmp_path, monkeypatch)
    assert response['tree_confirmed_dead'] is expected
    summary = response['descendant_uncertainty']
    assert set(summary) == {'census', 'listed', 'more_unlisted'}
    assert summary['census'] in {'complete', 'incomplete', 'not_observed'}
    assert 0 <= summary['listed'] <= DESCENDANTS_RECORDED
    if fate == 'outside_descendant':
        assert summary == {'census': 'complete', 'listed': 1, 'more_unlisted': False}
    elif fate == 'truncated_census':
        assert summary == {'census': 'incomplete', 'listed': DESCENDANTS_RECORDED,
                           'more_unlisted': True}
    elif fate == 'unknown':
        assert summary == {'census': 'not_observed', 'listed': 0, 'more_unlisted': False}
    serialized = json.dumps(response)
    assert all(value not in serialized for value in ('987654', 'private-worker', 'C:/private'))
    assert not {'termination', 'descendants', 'metadata', 'exit_code'} & response.keys()
    assert response['changed_files'] is None
    assert response['worker_claim'] == 'completed' and response['partial'] is False
    assert response['receipt'] == first.receipt
    assert store.state(item.job.job_id, NOW) == 'pending'
    assert jobs_tools.modelquad_job_collect(item.job.job_id, project(database)) == response
    assert store.collect(item.job.job_id, NOW).receipt == first.receipt


@pytest.mark.parametrize('case', ['below_cap', 'at_cap', 'above_cap', 'multibyte'])
def test_collection_truncation_matches_receipt(terminal_job, monkeypatch, case):
    database, store, item = terminal_job
    cap = storage.MAX_OUTPUT_BYTES
    text = {'below_cap': 'x' * (cap - 1), 'at_cap': 'x' * cap,
            'above_cap': 'x' * (cap + 9), 'multibyte': 'א€' * (cap // 5 + 1)}[case]
    item = replace(item, output_text=text)
    store.put(item, NOW)
    result = store.collect(item.job.job_id, NOW)
    # Truncation must come from the collected snapshot, not another transaction.
    monkeypatch.setattr(store, 'truncation', lambda *args: pytest.fail('separate truncation read'))
    response = jobs_tools.modelquad_job_collect(item.job.job_id, project(database))
    original = text.encode('utf-8')
    assert response['output'] == original[:cap].decode('utf-8', errors='ignore')
    assert response['output_truncated'] is (len(original) > cap)
    assert response['original_bytes'] == len(original) == result.original_bytes
    assert response['output_truncated'] is result.output_truncated
    assert response['receipt'] == result.receipt
    assert response['worker_claim'] == 'completed' and response['partial'] is False
    assert jobs_tools.modelquad_job_collect(item.job.job_id, project(database)) == response
    assert store.state(item.job.job_id, NOW) == 'pending'


@pytest.mark.parametrize('surface', ['collect', 'wait'])
@pytest.mark.parametrize('stored_fate', [True, False])
def test_history_read_failure_keeps_answer_and_unknown_fate(terminal_job, monkeypatch, surface, stored_fate):
    database, store, item = terminal_job
    item = replace(item, tree_confirmed_dead=stored_fate)
    store.put(item, NOW)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic read failure')

    monkeypatch.setattr(history, 'read_job', after_scope_check(unavailable))
    response = (jobs_tools.modelquad_job_collect(item.job.job_id, project(database))
                if surface == 'collect'
                else jobs_tools.modelquad_job_wait(item.job.job_id, project(database)))
    assert response['output'] == item.output_text
    assert response['job_status'] == item.job_status.value
    assert response['tree_confirmed_dead'] is None


def test_terminal_shortcut_rereads_recovery_guard(terminal_job):
    database, store, item = terminal_job
    store.put(item, NOW)
    earlier = history.read_job(database, item.job.job_id)
    with sqlite3.connect(database) as connection:
        connection.execute('UPDATE jobs SET recovery_decision=? WHERE job_id=?',
                           ('restore', item.job.job_id))
    response = jobs_tools.collect_job_response(item.job.job_id, record=earlier)
    assert response['output'] == item.output_text
    assert response['tree_confirmed_dead'] is None


@pytest.mark.parametrize('case', ['watermark', 'compaction', 'expiry', 'clock_rollback'])
def test_enospc_collect_returns_durable_unexpired_answer(tmp_path, monkeypatch, case):
    store = FileOutputStore(tmp_path)
    expired = answer('expired')
    surviving = answer('surviving')
    store.put(expired, NOW)
    store.put(surviving, NOW + timedelta(days=1))
    original = store.collect(surviving.job.job_id, NOW + timedelta(days=1))
    now = NOW + timedelta(days=2)
    if case in {'expiry', 'clock_rollback'}:
        now = NOW + RETENTION
    durable_now = now
    if case == 'clock_rollback':
        # Record a durable clock beyond the first answer's expiry, then roll back.
        store.collect(surviving.job.job_id, now)
        now = NOW + timedelta(days=3)
    if case == 'compaction':
        monkeypatch.setattr(storage, 'JOURNAL_MAX_LINES', 1)
    monkeypatch.setattr(FileOutputStore, '_allocating_write', disk_full)
    reopened = FileOutputStore(tmp_path)  # construction itself requires no space
    result = reopened.collect(surviving.job.job_id, now)
    assert result.output == surviving
    assert result.receipt == original.receipt
    assert result.original_bytes == original.original_bytes
    assert reopened.state(surviving.job.job_id, now) == 'pending'
    assert surviving.job in {entry.job for entry in reopened.pending(now)}
    if case in {'expiry', 'clock_rollback'}:
        assert reopened.collect(expired.job.job_id, now) is None
        assert reopened.state(expired.job.job_id, now) == 'expired'
        assert not (tmp_path / store._filename(expired.job.job_id)).exists()
    if case == 'clock_rollback':
        # An earlier read needs no watermark write, so ENOSPC alone does not
        # force failure. A later outcomes query must persist its served time.
        assert reopened.outcomes(now, since=NOW) == ()
    with pytest.raises(PendingOutputCapacityUnavailable, match='pending_output_capacity_unavailable'):
        reopened.outcomes(durable_now + timedelta(seconds=1), since=NOW)
    with pytest.raises(PendingOutputCapacityUnavailable):
        reopened.extend(surviving.job.job_id, NOW + timedelta(days=20), now)
    with pytest.raises(PendingOutputCapacityUnavailable):
        reopened.put(answer('new'), now)


@pytest.mark.parametrize('removed', [False, True], ids=['kept', 'removed'])
def test_enospc_with_open_transaction(tmp_path, monkeypatch, removed):
    store = FileOutputStore(tmp_path)
    item = answer()
    store.put(item, NOW)
    result = store.collect(item.job.job_id, NOW)
    operations = [{'kind': 'remove', 'file': store._filename(item.job.job_id)}] if removed else []
    intent = {'kind': 'transaction', 'id': 'open-transaction', 'watermark': NOW.isoformat(),
              'operations': operations, 'outcomes': [], 'removed': {}}
    with (tmp_path / 'outcomes.jsonl').open('ab') as stream:
        stream.write((json.dumps(intent) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    monkeypatch.setattr(FileOutputStore, '_allocating_write', disk_full)
    reopened = FileOutputStore(tmp_path)
    collected = reopened.collect(item.job.job_id, NOW + timedelta(seconds=1))
    assert (collected is None) is removed
    if not removed:
        assert collected.output == item and collected.receipt == result.receipt


@pytest.mark.parametrize('operation', ['new_put', 'extend'])
def test_open_write_is_not_served_under_enospc(tmp_path, monkeypatch, operation):
    store = FileOutputStore(tmp_path)
    original = None
    if operation == 'extend':
        store.put(answer(), NOW)
        original = store.collect('retained', NOW)

    def interrupted(stage):
        if stage == 'journaled':
            raise RuntimeError('interrupted journaled write')

    with monkeypatch.context() as patch:
        patch.setattr(store, '_checkpoint', interrupted)
        with pytest.raises(RuntimeError, match='interrupted journaled write'):
            if operation == 'extend':
                store.extend('retained', NOW + timedelta(days=20), NOW)
            else:
                store.put(answer(), NOW)
    monkeypatch.setattr(FileOutputStore, '_allocating_write', disk_full)
    reopened = FileOutputStore(tmp_path)
    for _ in range(2):
        collected = reopened.collect('retained', NOW)
        if operation == 'new_put':
            assert collected is None
            assert reopened.state('retained', NOW) is None
            assert reopened.pending(NOW) == ()
        else:
            assert collected.output == original.output
            assert collected.receipt == original.receipt
            assert reopened.state('retained', NOW) == 'pending'
            assert [entry.job for entry in reopened.pending(NOW)] == [original.output.job]


CRASH_CHILD = r'''
import errno, os, sys
from datetime import datetime, timezone
from modelquad.jobs.store import FileOutputStore
root, stage, operation = sys.argv[1:]
store = FileOutputStore(root)
def full(*args, **kwargs):
    raise OSError(errno.ENOSPC, 'synthetic disk full')
store._allocating_write = full
def checkpoint(at):
    if at == stage:
        os._exit(73)
store._checkpoint = checkpoint
now = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
if operation == 'all':
    store.delete_all(now)
else:
    store.delete('retained', now)
raise SystemExit('checkpoint not reached')
'''


@pytest.mark.parametrize('case', [
    'one', 'all', 'crash_after_intent', 'crash_after_unlink',
    'reserve_unavailable', 'expire', 'acknowledge',
])
def test_enospc_deletion_frees_space_without_resurrection(tmp_path, monkeypatch, case):
    store = FileOutputStore(tmp_path)
    items = [answer()]
    if case == 'all':
        items += [answer('other-' + str(n), attempt_id=n + 2) for n in range(3)]
    for item in items:
        store.put(item, NOW)
    receipt = store.collect(items[0].job.job_id, NOW).receipt
    states = {case: 'deleted', 'expire': 'expired', 'acknowledge': 'acknowledged'}
    now = NOW + RETENTION if case == 'expire' else NOW
    if case.startswith('crash_after'):
        stage = {'crash_after_intent': 'removal_intent_written',
                 'crash_after_unlink': 'removal_unlinked'}[case]
        env = dict(os.environ)
        # Child imports this checkout in source mode; installed-mode suites
        # already select a wheel, so preserve that import root when configured.
        if not os.environ.get('PYTHONPATH'):
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        child = subprocess.run(
            [sys.executable, '-c', CRASH_CHILD, str(tmp_path), stage, 'one'],
            env=env, timeout=30, capture_output=True, **no_window_kwargs(),
        )
        assert child.returncode == 73, child.stderr.decode(errors='replace')
    monkeypatch.setattr(FileOutputStore, '_allocating_write', disk_full)
    if case == 'reserve_unavailable':
        monkeypatch.setattr(store, '_slot_allocated', lambda: False)
        with pytest.raises(PendingOutputCapacityUnavailable) as error:
            store.delete(items[0].job.job_id, now)
        assert error.value.errno == errno.ENOSPC
        assert error.value.label == 'pending_output_capacity_unavailable'
        assert store.collect(items[0].job.job_id, now).receipt == receipt
        assert (tmp_path / store._filename(items[0].job.job_id)).exists()
        return
    if case == 'one':
        assert store.delete(items[0].job.job_id, now)
    elif case == 'all':
        assert store.delete_all(now) == len(items)
        assert store.delete_all(now) == 0
    elif case == 'expire':
        assert store.purge_expired(now) == (items[0].job,)
    elif case == 'acknowledge':
        assert store.acknowledge(items[0].job.job_id, receipt, now)
        assert store.acknowledge(items[0].job.job_id, receipt, now)
        assert not store.acknowledge(items[0].job.job_id, 'wrong', now)
    reopened = FileOutputStore(tmp_path)
    for item in items:
        assert not (tmp_path / store._filename(item.job.job_id)).exists()
        assert reopened.collect(item.job.job_id, now) is None
        assert reopened.state(item.job.job_id, now) == states[case]
    if case == 'acknowledge':
        assert reopened.acknowledge(items[0].job.job_id, receipt, now)
    # Once allocating writes work again, durable replay protection still wins.
    monkeypatch.undo()
    for item in items:
        assert not FileOutputStore(tmp_path).put(item, now)


def test_native_removal_slot_is_allocated_and_removal_never_allocates(tmp_path, monkeypatch):
    store = FileOutputStore(tmp_path)
    item = answer()
    store.put(item, NOW)
    slot = tmp_path / storage.REMOVAL_SLOT_NAME
    assert slot.stat().st_size == storage.REMOVAL_SLOT_BYTES
    assert store._slot_allocated()
    assert item.job.job_id.encode() not in slot.read_bytes()
    before_size = slot.stat().st_size
    monkeypatch.setattr(store, '_allocating_write', lambda *args, **kwargs: pytest.fail('removal allocated'))
    assert store.delete(item.job.job_id, NOW)
    assert item.job.job_id.encode() not in slot.read_bytes()
    assert slot.stat().st_size == before_size
    assert store._slot_allocated()


def test_capacity_reservation_and_opaque_job_id_stay_inside_store_root(tmp_path, monkeypatch):
    from modelquad import paths

    root = tmp_path / 'state $ \u05ea\u05e9\u05d5\u05d1\u05d4'
    monkeypatch.setattr(paths, 'state_root', lambda: root)
    store = storage.default_store()
    assert store.root == root / 'pending-output'
    item = answer('../../escape/C:\\elsewhere')
    assert store.put(item, NOW)
    assert store.collect(item.job.job_id, NOW).output == item
    assert {path.relative_to(store.root).as_posix() for path in tmp_path.rglob('*')
            if path.is_file()} == {
        'store.lock', 'outcomes.jsonl', storage.REMOVAL_SLOT_NAME,
        store._filename(item.job.job_id),
    }
    assert store._slot_allocated()
    assert store.delete(item.job.job_id, NOW)
    assert all(store.root in path.parents for path in tmp_path.rglob('*') if path.is_file())
    assert FileOutputStore(store.root).collect(item.job.job_id, NOW) is None


@pytest.mark.parametrize('corrupt', [False, True], ids=['healthy', 'corrupt_journal'])
def test_delete_all_streams_bounded_batches_and_crash_keeps_completed_batch(
    tmp_path, monkeypatch, corrupt,
):
    store = FileOutputStore(tmp_path)
    items = [answer('batch-' + str(n), attempt_id=n + 1)
             for n in range(storage.REMOVAL_BATCH_SIZE + 3)]
    for item in items:
        store.put(item, NOW)
    if corrupt:
        with (tmp_path / 'outcomes.jsonl').open('ab') as stream:
            stream.write(b'invalid complete record\n')
        os.utime(tmp_path / 'outcomes.jsonl', (NOW.timestamp(), NOW.timestamp()))
    sizes = []
    remove = store._removal_transaction

    def limited(entries, now):
        assert len(entries) <= storage.REMOVAL_BATCH_SIZE
        assert store._entries == {}  # no all-answer snapshot
        sizes.append(len(entries))
        return remove(entries, now)

    class Crash(RuntimeError):
        pass

    def checkpoint(stage):
        if stage == 'removal_batch_finished':
            raise Crash()

    monkeypatch.setattr(store, '_removal_transaction', limited)
    monkeypatch.setattr(store, '_checkpoint', checkpoint)
    with pytest.raises(Crash):
        store.delete_all(NOW)
    assert sizes == [storage.REMOVAL_BATCH_SIZE]
    deleted = [item for item in items if not (tmp_path / store._filename(item.job.job_id)).exists()]
    assert len(deleted) == storage.REMOVAL_BATCH_SIZE
    reopened = FileOutputStore(tmp_path)
    assert reopened.delete_all(NOW) == 3
    assert {event.job for event in reopened.outcomes(NOW, since=NOW)} == {
        item.job for item in items
    }
    for item in deleted:
        assert reopened.collect(item.job.job_id, NOW) is None
        assert not reopened.put(item, NOW)


def test_corrupt_answer_gets_hash_only_removal_under_enospc(tmp_path, monkeypatch):
    store = FileOutputStore(tmp_path)
    item = answer()
    store.put(item, NOW)
    path = tmp_path / store._filename(item.job.job_id)
    path.write_bytes(b'invalid private content')
    monkeypatch.setattr(FileOutputStore, '_allocating_write', disk_full)
    assert store.delete_all(NOW) == 1
    assert not path.exists()
    assert FileOutputStore(tmp_path).collect(item.job.job_id, NOW) is None
    # Simulate stale content replay before a second read of the store.
    path.write_bytes(b'still invalid private content')
    assert FileOutputStore(tmp_path).collect(item.job.job_id, NOW) is None
    assert not path.exists()


def test_acknowledge_capacity_label_is_mcp_visible(terminal_job, monkeypatch):
    database, store, item = terminal_job
    store.put(item, NOW)
    receipt = store.collect(item.job.job_id, NOW).receipt
    monkeypatch.setattr(store, '_slot_allocated', lambda: False)
    assert jobs_tools.modelquad_job_acknowledge(item.job.job_id, receipt, project(database)) == {
        'error': 'pending_output_capacity_unavailable',
    }
    assert store.collect(item.job.job_id, NOW).receipt == receipt


def test_enospc_delete_all_reuses_capacity_after_freeing_batch(tmp_path, monkeypatch):
    store = FileOutputStore(tmp_path)
    items = [answer('space-' + str(n), attempt_id=n + 1)
             for n in range(storage.REMOVAL_BATCH_SIZE + 3)]
    for item in items:
        store.put(item, NOW)
    allocating = store._allocating_write
    full = True

    def write(*args, **kwargs):
        if full:
            disk_full()
        return allocating(*args, **kwargs)

    def checkpoint(stage):
        nonlocal full
        if stage == 'removal_batch_finished':
            # The first batch must have unlinked real content before new
            # allocation becomes possible; no disk is actually filled.
            assert sum(path.is_file() for path in tmp_path.glob('*.json')) == 3
            full = False

    monkeypatch.setattr(store, '_allocating_write', write)
    monkeypatch.setattr(store, '_checkpoint', checkpoint)
    assert store.delete_all(NOW) == len(items)
    assert not list(tmp_path.glob('*.json'))
    reopened = FileOutputStore(tmp_path)
    assert {event.job for event in reopened.outcomes(NOW, since=NOW)} == {
        item.job for item in items
    }


def test_delete_all_cancels_open_journaled_put_before_discarding_temp(tmp_path, monkeypatch):
    store = FileOutputStore(tmp_path)
    item = answer('open-put')

    def interrupt(stage):
        if stage == 'journaled':
            raise RuntimeError('interrupted put')

    with monkeypatch.context() as patch:
        patch.setattr(store, '_checkpoint', interrupt)
        with pytest.raises(RuntimeError, match='interrupted put'):
            store.put(item, NOW)
    staged = list(tmp_path.glob('*.tmp'))
    assert len(staged) == 1
    assert not (tmp_path / store._filename(item.job.job_id)).exists()
    reopened = FileOutputStore(tmp_path)

    def intent_before_temp(stage):
        if stage == 'removal_intent_written':
            assert staged[0].exists()
            assert reopened._job_hash(item.job.job_id) in reopened._slot['removed']

    with monkeypatch.context() as patch:
        patch.setattr(reopened, '_checkpoint', intent_before_temp)
        patch.setattr(reopened, '_allocating_write', disk_full)
        assert reopened.delete_all(NOW) == 0  # no committed content file existed
    assert not staged[0].exists()
    assert reopened.collect(item.job.job_id, NOW) is None
    assert reopened.state(item.job.job_id, NOW) == 'deleted'
    assert reopened.put(answer('later-put'), NOW)
    assert reopened.collect('later-put', NOW).output == answer('later-put')
    assert not reopened.put(item, NOW)


@pytest.mark.parametrize('operation', ['delete', 'acknowledge'])
def test_removal_of_answer_with_open_extend(tmp_path, monkeypatch, operation):
    store = FileOutputStore(tmp_path)
    item = answer()
    store.put(item, NOW)
    receipt = store.collect(item.job.job_id, NOW).receipt

    def interrupt(stage):
        if stage == 'journaled':
            raise RuntimeError('interrupted extend')

    with monkeypatch.context() as patch:
        patch.setattr(store, '_checkpoint', interrupt)
        with pytest.raises(RuntimeError, match='interrupted extend'):
            store.extend(item.job.job_id, NOW + timedelta(days=20), NOW)
    with monkeypatch.context() as patch:
        patch.setattr(FileOutputStore, '_allocating_write', disk_full)
        reopened = FileOutputStore(tmp_path)
        if operation == 'delete':
            assert reopened.delete(item.job.job_id, NOW)
        else:
            assert reopened.acknowledge(item.job.job_id, receipt, NOW)
        assert reopened.collect(item.job.job_id, NOW) is None
        assert reopened.state(item.job.job_id, NOW) == (
            'deleted' if operation == 'delete' else 'acknowledged')
    assert not FileOutputStore(tmp_path).put(item, NOW)


def test_removal_slot_padding_resists_compression(tmp_path, monkeypatch):
    source = random.Random(4)
    monkeypatch.setattr(storage.secrets, 'token_bytes', source.randbytes)
    store = FileOutputStore(tmp_path)
    store.put(answer(), NOW)
    slot = (tmp_path / storage.REMOVAL_SLOT_NAME).read_bytes()
    assert len(zlib.compress(slot)) > storage.REMOVAL_SLOT_BYTES * 0.9
    assert store._slot_allocated()
    assert store.delete('retained', NOW)
    rewritten = (tmp_path / storage.REMOVAL_SLOT_NAME).read_bytes()
    assert len(zlib.compress(rewritten)) > storage.REMOVAL_SLOT_BYTES * 0.9
    assert FileOutputStore(tmp_path).state('retained', NOW) == 'deleted'


def test_delete_all_quarantines_corrupt_removal_slot(tmp_path):
    store = FileOutputStore(tmp_path)
    item = answer()
    store.put(item, NOW)
    slot = tmp_path / storage.REMOVAL_SLOT_NAME
    damaged = b'!' * storage.REMOVAL_SLOT_BYTES
    slot.write_bytes(damaged)
    reopened = FileOutputStore(tmp_path)
    assert reopened.delete_all(NOW) == 1
    archives = list(tmp_path.glob(storage.REMOVAL_SLOT_NAME + '.corrupt-*'))
    assert len(archives) == 1 and archives[0].read_bytes() == damaged
    assert reopened._slot_allocated()
    assert FileOutputStore(tmp_path).state(item.job.job_id, NOW) == 'deleted'
    assert not FileOutputStore(tmp_path).put(item, NOW)


def test_wait_without_answer_reports_unknown_when_history_is_unreadable(terminal_job, monkeypatch):
    database, _store, item = terminal_job
    with sqlite3.connect(database) as connection:
        connection.execute('UPDATE jobs SET status=?, ended_at=NULL WHERE job_id=?',
                           ('running', item.job.job_id))

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic unreadable history')

    monkeypatch.setattr(jobs_tools, 'job_status', unavailable)
    monkeypatch.setattr(history, 'read_job', after_scope_check(unavailable))
    response = jobs_tools.modelquad_job_wait(item.job.job_id, project(database))
    assert response['status'] == response['job_status'] == 'unknown'
    assert response['terminal'] is False
    assert response['wait_again_with'] == 'modelquad_job_wait'
    assert 'receipt' not in response


def test_readable_missing_history_row_preserves_stored_false(terminal_job, monkeypatch):
    database, store, item = terminal_job
    store.put(replace(item, tree_confirmed_dead=False), NOW)
    monkeypatch.setattr(history, 'read_job', after_scope_check(lambda *args, **kwargs: None))
    response = jobs_tools.modelquad_job_collect(item.job.job_id, project(database))
    assert response['tree_confirmed_dead'] is False
    assert response['output'] == item.output_text


@pytest.mark.parametrize('operation', ['delete_all', 'purge_expired'])
def test_first_removal_batch_uses_remaining_slot_capacity(tmp_path, monkeypatch, operation):
    store = FileOutputStore(tmp_path)
    items = [answer('partial-slot-' + str(n), attempt_id=n + 1)
             for n in range(storage.REMOVAL_BATCH_SIZE + 2)]
    for item in items:
        store.put(item, NOW)
    assert store.delete(items[0].job.job_id, NOW)
    allocating = store._allocating_write
    full = True
    first_unlink = []

    def write(*args, **kwargs):
        if full:
            disk_full()
        return allocating(*args, **kwargs)

    def checkpoint(stage):
        nonlocal full
        if stage == 'removal_unlinked' and full:
            assert sum(path.is_file() for path in tmp_path.glob('*.json')) == 2
            first_unlink.append(True)
            full = False

    monkeypatch.setattr(store, '_allocating_write', write)
    monkeypatch.setattr(store, '_checkpoint', checkpoint)
    if operation == 'delete_all':
        assert store.delete_all(NOW) == len(items) - 1
    else:
        assert set(store.purge_expired(NOW + RETENTION)) == {item.job for item in items[1:]}
    assert first_unlink == [True]
    assert not list(tmp_path.glob('*.json'))


def test_quarantined_journal_uses_hash_only_removal_under_enospc(tmp_path, monkeypatch):
    store = FileOutputStore(tmp_path)
    item = answer()
    store.put(item, NOW)
    journal = tmp_path / 'outcomes.jsonl'
    with journal.open('ab') as stream:
        stream.write(b'invalid complete record\n')
    os.utime(journal, (NOW.timestamp(), NOW.timestamp()))
    with monkeypatch.context() as patch:
        patch.setattr(FileOutputStore, '_allocating_write', disk_full)
        assert store.delete_all(NOW) == 1
        assert not (tmp_path / store._filename(item.job.job_id)).exists()
        reopened = FileOutputStore(tmp_path)
        assert reopened.collect(item.job.job_id, NOW) is None
        with reopened._locked():
            reopened._read_slot()
        assert reopened._job_hash(item.job.job_id) in reopened._slot['removed']
        assert reopened._slot['hash_outcomes'] == []
    assert len(list(tmp_path.glob('outcomes.jsonl.corrupt-*'))) == 1
    assert not FileOutputStore(tmp_path).put(item, NOW)


def test_expiry_read_fallback_never_persists_outcome_without_removed_hash(tmp_path, monkeypatch):
    store = FileOutputStore(tmp_path)
    item = answer()
    store.put(item, NOW)
    # Simulate a legacy journal without the pending-reference catalog, so
    # expiry needs an allocating reference write before it can unlink.
    journal = tmp_path / 'outcomes.jsonl'
    records = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
    for record in records:
        record.pop('references', None)
    journal.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
    before = journal.read_bytes()
    allocating = store._allocating_write
    calls = []

    def transient_full(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            disk_full()
        return allocating(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, '_allocating_write', transient_full)
        patch.setattr(storage, 'JOURNAL_MAX_LINES', 1)
        assert store.collect(item.job.job_id, NOW + RETENTION) is None
    assert calls == [True]
    assert journal.read_bytes() == before
    assert (tmp_path / store._filename(item.job.job_id)).exists()
    reopened = FileOutputStore(tmp_path)
    assert reopened.purge_expired(NOW + RETENTION) == (item.job,)
    # Replay remains blocked after the 30-day outcome retention has elapsed.
    assert not reopened.put(item, NOW + RETENTION + timedelta(days=31))
