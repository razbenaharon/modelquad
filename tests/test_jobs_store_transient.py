# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic transient inspections; no worker processes or real retry sleeps."""

import errno
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from modelquad.jobs import store as module
from modelquad.jobs.seam import MAX_RETENTION
from test_jobs_seam import NOW, output


# The fallback lets the behavioural assertions run on the unmodified base.
TransientError = getattr(module, 'PendingOutputTemporarilyUnavailable', OSError)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(module, 'INSPECTION_RETRY_DELAY_SECONDS', 0, raising=False)
    monkeypatch.setattr(module, 'INSPECTION_RETRY_BUDGET_SECONDS', 3600, raising=False)


def retained(tmp_path):
    store = module.FileOutputStore(tmp_path)
    item = output()
    assert store.put(item, NOW)
    return store, item, tmp_path / store._filename(item.job.job_id)


def patch_lstat(monkeypatch, filename, failure):
    original = Path.lstat
    calls = []

    def inspect(path, *args, **kwargs):
        if path.name == filename:
            calls.append(path.name)
            result = failure(len(calls))
            if result is not None:
                return result
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', inspect)
    return calls


def denied(_call):
    raise PermissionError(errno.EACCES, 'synthetic inspection denial')


def regular(*, links=1, attributes=0):
    return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=links,
                           st_file_attributes=attributes)


def test_transient_lstat_failure_is_retried(tmp_path, monkeypatch, caplog):
    store, item, path = retained(tmp_path)

    def first_two(call):
        if call <= 2:
            denied(call)

    calls = patch_lstat(monkeypatch, path.name, first_two)
    assert store.pending(NOW)[0].job == item.job
    assert len(calls) >= 3
    assert 'lstat error' in caplog.text and path.name in caplog.text
    messages = [record.getMessage() for record in caplog.records if path.name in record.getMessage()]
    assert len(messages) == 1 and '3 attempts; safe' in messages[0]
    assert str(tmp_path) not in caplog.text


@pytest.mark.parametrize('operation', ['pending', 'collect', 'state'],
                         ids=['pending', 'collect', 'state'])
def test_persistent_uninspectable_entry_is_not_reported_absent(tmp_path, monkeypatch, operation):
    store, item, path = retained(tmp_path)
    patch_lstat(monkeypatch, path.name, denied)
    if operation == 'collect':
        from modelquad.mcp import jobs_tools

        monkeypatch.setattr(jobs_tools, '_guard', lambda: None)
        monkeypatch.setattr(jobs_tools, 'default_store', lambda: store)
        monkeypatch.setattr(jobs_tools, 'datetime', SimpleNamespace(now=lambda tz: NOW))
        monkeypatch.setattr(jobs_tools.history, 'read_job', lambda *a: None)
        monkeypatch.setattr(jobs_tools, '_scoped_job', lambda job_id, project: ({'attempt_id': 1}, None))
        assert jobs_tools.modelquad_job_collect(item.job.job_id, str(tmp_path)) == {
            'error': 'pending_output_temporarily_unavailable'}
    else:
        with pytest.raises(TransientError) as error:
            if operation == 'pending':
                store.pending(NOW)
            else:
                store.state(item.job.job_id, NOW)
        assert error.value.errno == errno.EAGAIN


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'reparse_attribute'],
                         ids=['symlink', 'hardlink', 'reparse_attribute'])
def test_unsafe_entry_is_still_skipped(tmp_path, monkeypatch, caplog, kind):
    """Base-passing security regression guard."""
    store, item, path = retained(tmp_path / 'store')
    if kind == 'reparse_attribute':
        patch_lstat(monkeypatch, path.name, lambda call: regular(attributes=0x400))
    else:
        external = tmp_path / 'external'
        external.write_bytes(path.read_bytes())
        path.unlink()
        if kind == 'hardlink':
            os.link(external, path)
        else:
            try:
                path.symlink_to(external)
            except OSError:
                pytest.skip('native symlink creation unavailable')
    assert store.pending(NOW) == ()
    assert path.exists()
    assert path.name in caplog.text and str(tmp_path) not in caplog.text


@pytest.mark.parametrize('kind', ['stale-temp', 'removed-entry'],
                         ids=['stale-temp', 'removed-entry'])
def test_uninspectable_recovery_file_is_never_deleted(tmp_path, monkeypatch, kind):
    """Base-passing deletion regression guard."""
    store, item, path = retained(tmp_path)
    if kind == 'stale-temp':
        path = tmp_path / 'stale.tmp'
        path.write_bytes(b'synthetic staging')
    else:
        content = path.read_bytes()
        assert store.delete(item.job.job_id, NOW)
        store.pending(NOW)  # Transfer the slot so _load's removed-file cleanup runs.
        path.write_bytes(content)
    calls = patch_lstat(monkeypatch, path.name, denied)
    store.pending(NOW)
    assert path.exists() and len(calls) == 1  # Delete-only sites never retry.


def test_link_count_zero_is_uninspectable(tmp_path, monkeypatch, caplog):
    """Main regression: Windows stat fallback cannot prove a single link."""
    store, _item, path = retained(tmp_path)
    calls = patch_lstat(monkeypatch, path.name, lambda call: regular(links=0))
    with pytest.raises(TransientError) as error:
        store.pending(NOW)
    assert error.value.errno == errno.EAGAIN and len(calls) > 1
    assert 'link count 0' in caplog.text and path.name in caplog.text
    messages = [record.getMessage() for record in caplog.records if path.name in record.getMessage()]
    assert len(messages) == 1 and messages[0].startswith('Cannot inspect pending-output file:')
    assert f'{len(calls)} attempts' in messages[0]
    assert str(tmp_path) not in caplog.text


def test_put_uninspectable_staging_keeps_transaction_open(tmp_path, monkeypatch):
    store = module.FileOutputStore(tmp_path)
    item = output()

    def staged(stage):
        if stage == 'staged':
            name = next(tmp_path.glob('*.tmp')).name
            patch_lstat(monkeypatch, name, denied)

    with monkeypatch.context() as patch:
        patch.setattr(store, '_checkpoint', staged)
        with pytest.raises(TransientError):
            store.put(item, NOW)
    records = [json.loads(line) for line in (tmp_path / 'outcomes.jsonl').read_text().splitlines()]
    assert records[-1]['kind'] == 'transaction'
    assert list(tmp_path.glob('*.tmp'))
    # Remove the injected denial; recovery must publish the durable intent.
    monkeypatch.undo()
    assert store.collect(item.job.job_id, NOW).output == item


@pytest.mark.parametrize('site', ['validate', 'replay'], ids=['validate', 'replay'])
def test_extend_uninspectable_target_raises_transient(tmp_path, monkeypatch, site):
    store, item, path = retained(tmp_path)
    if site == 'validate':
        original = store._collect

        def collect(*args):
            result = original(*args)
            patch_lstat(monkeypatch, path.name, denied)
            return result

        monkeypatch.setattr(store, '_collect', collect)
    else:
        def checkpoint(stage):
            if stage == 'staged':
                patch_lstat(monkeypatch, path.name, denied)

        monkeypatch.setattr(store, '_checkpoint', checkpoint)
    with pytest.raises(TransientError) as error:
        store.extend(item.job.job_id, NOW + MAX_RETENTION, NOW)
    assert error.value.errno == errno.EAGAIN
    monkeypatch.undo()
    collected = store.collect(item.job.job_id, NOW)
    assert collected.expires_at == NOW + (MAX_RETENTION if site == 'replay' else module.RETENTION)


@pytest.mark.parametrize('site', ['before-replay', 'candidate'], ids=['before-replay', 'candidate'])
def test_delete_all_uninspectable_answer_cannot_report_done(tmp_path, monkeypatch, site):
    store, _item, path = retained(tmp_path)
    if site == 'before-replay':
        patch_lstat(monkeypatch, path.name, denied)
    else:
        original = store._load

        def load(**kwargs):
            original(**kwargs)
            patch_lstat(monkeypatch, path.name, denied)

        monkeypatch.setattr(store, '_load', load)
    with pytest.raises(TransientError) as error:
        store.delete_all(NOW)
    assert error.value.errno == errno.EAGAIN and path.exists()


def test_idle_pending_preserves_uninspectable_reference_without_snapshot(tmp_path, monkeypatch):
    store, item, path = retained(tmp_path)
    journal = tmp_path / 'outcomes.jsonl'
    before = journal.read_bytes(), journal.stat().st_mtime_ns
    original = Path.read_text
    read_entry = False

    def read(path_to_read, *args, **kwargs):
        nonlocal read_entry
        result = original(path_to_read, *args, **kwargs)
        if path_to_read.name == path.name:
            read_entry = True
        return result

    def after_read(call):
        if read_entry:
            denied(call)

    monkeypatch.setattr(Path, 'read_text', read)
    patch_lstat(monkeypatch, path.name, after_read)
    assert store.pending(NOW)[0].job == item.job
    assert (journal.read_bytes(), journal.stat().st_mtime_ns) == before


@pytest.mark.parametrize('operation', ['pending', 'delete_all'], ids=['pending', 'delete_all'])
def test_content_permission_error_has_transient_label(tmp_path, monkeypatch, operation):
    store, _item, path = retained(tmp_path)
    original = Path.read_text

    def read(path_to_read, *args, **kwargs):
        if path_to_read.name == path.name:
            denied(0)
        return original(path_to_read, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(TransientError) as error:
        getattr(store, operation)(NOW)
    assert error.value.errno == errno.EAGAIN


def test_delete_all_uninspectable_temp_reports_retry(tmp_path, monkeypatch):
    store = module.FileOutputStore(tmp_path)
    path = tmp_path / 'stale.tmp'
    path.write_bytes(b'synthetic staging')
    calls = patch_lstat(monkeypatch, path.name, denied)
    with pytest.raises(TransientError) as error:
        store.delete_all(NOW)
    assert error.value.errno == errno.EAGAIN and path.exists()
    assert len(calls) == 2  # One cleanup inspection and one final inspection.


def test_uninspectable_removal_slot_is_not_quarantined(tmp_path, monkeypatch):
    store, _item, _path = retained(tmp_path)
    path = tmp_path / module.REMOVAL_SLOT_NAME
    patch_lstat(monkeypatch, path.name, denied)
    reopened = module.FileOutputStore(tmp_path)  # Construction stays usable.
    with pytest.raises(TransientError) as error:
        reopened.delete_all(NOW)
    assert error.value.errno == errno.EAGAIN
    assert path.exists() and not list(tmp_path.glob('*.corrupt-*'))


def test_reference_retries_share_one_operation_budget(tmp_path, monkeypatch):
    store = module.FileOutputStore(tmp_path)
    sleeps = []
    names = ['a' * 64 + '.json', 'b' * 64 + '.json']
    for name in names:
        (tmp_path / name).write_bytes(b'synthetic reference target')
    calls = dict.fromkeys(names, 0)
    original = Path.lstat

    def inspect(path, *args, **kwargs):
        if path.name in calls:
            calls[path.name] += 1
            denied(0)
        return original(path, *args, **kwargs)

    def sleep(delay):
        sleeps.append(delay)
        # Charge the requested sleep without sleeping or replacing the shared clock.
        store._inspection_spent += delay

    monkeypatch.setattr(module, 'INSPECTION_RETRY_DELAY_SECONDS', 0.1, raising=False)
    monkeypatch.setattr(module, 'INSPECTION_RETRY_BUDGET_SECONDS', 0.25, raising=False)
    monkeypatch.setattr(module.time, 'sleep', sleep)
    monkeypatch.setattr(Path, 'lstat', inspect)
    with store._locked():
        assert store._reference_pending(Path(names[0]).stem)
        assert store._reference_pending(Path(names[1]).stem)
    assert calls[names[0]] > 1 and calls[names[1]] == 1
    assert 0 < sum(sleeps) <= 0.25
    assert store._inspection_spent >= 0.25
    assert store._inspection_spent >= sum(sleeps)
    with store._locked():
        assert store._inspection_spent == 0


def test_absent_remove_target_still_emits_operation_checkpoint(tmp_path, monkeypatch):
    store = module.FileOutputStore(tmp_path)
    checkpoints = []
    monkeypatch.setattr(store, '_checkpoint', checkpoints.append)
    with store._locked():
        store._apply([{'kind': 'remove', 'file': 'absent.json'}])
    assert checkpoints == ['operation_applied']


@pytest.mark.parametrize('inspection', ['absent', 'uninspectable', 'unsafe'],
                         ids=['absent', 'uninspectable', 'unsafe'])
def test_capacity_slot_inspection_keeps_error_kind(tmp_path, monkeypatch, inspection):
    store, _item, _path = retained(tmp_path)
    slot = tmp_path / module.REMOVAL_SLOT_NAME
    before = slot.read_bytes()
    if inspection == 'absent':
        slot.unlink()
    elif inspection == 'uninspectable':
        patch_lstat(monkeypatch, slot.name, denied)
    else:
        patch_lstat(monkeypatch, slot.name, lambda call: regular(attributes=0x400))
    with store._locked():
        if inspection == 'absent':
            store._ensure_capacity()
            assert store._slot_allocated()
            assert store._slot is None
        else:
            expected = (TransientError if inspection == 'uninspectable'
                        else module.PendingOutputCapacityUnavailable)
            with pytest.raises(expected) as error:
                store._ensure_capacity()
            assert error.value.errno == (errno.EAGAIN if inspection == 'uninspectable' else errno.ENOSPC)
            assert slot.read_bytes() == before


@pytest.mark.parametrize('operation', ['pending', 'delete_all'], ids=['pending', 'delete_all'])
def test_slot_content_permission_error_has_transient_label(tmp_path, monkeypatch, operation):
    store, _item, _path = retained(tmp_path)
    slot = tmp_path / module.REMOVAL_SLOT_NAME
    before = slot.read_bytes()
    original = Path.read_bytes

    def read(path, *args, **kwargs):
        if path.name == slot.name:
            denied(0)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.raises(TransientError) as error:
        getattr(store, operation)(NOW)
    assert error.value.errno == errno.EAGAIN
    assert original(slot) == before and not list(tmp_path.glob('*.corrupt-*'))


@pytest.mark.parametrize('inspection', ['uninspectable', 'unsafe', 'absent'],
                         ids=['uninspectable', 'unsafe', 'absent'])
def test_staging_is_rechecked_immediately_before_replace(tmp_path, monkeypatch, inspection):
    store = module.FileOutputStore(tmp_path)
    item = output()
    staged = None
    calls = []

    def changed(call):
        if call == 1:
            return None
        if inspection == 'uninspectable':
            denied(call)
        if inspection == 'unsafe':
            return regular(attributes=0x400)
        raise FileNotFoundError(errno.ENOENT, 'synthetic disappearance')

    with monkeypatch.context() as patch:
        def checkpoint(stage):
            nonlocal staged, calls
            if stage == 'journaled':
                staged = next(tmp_path.glob('*.tmp'))
                calls = patch_lstat(patch, staged.name, changed)

        patch.setattr(store, '_checkpoint', checkpoint)
        with pytest.raises(TransientError) as error:
            store.put(item, NOW)
        assert error.value.errno == errno.EAGAIN
        assert len(calls) == 2  # The final inspection has no retries.
        assert staged.exists() and not (tmp_path / store._filename(item.job.job_id)).exists()
        records = [json.loads(line) for line in (tmp_path / 'outcomes.jsonl').read_text().splitlines()]
        assert records[-1]['kind'] == 'transaction'
    assert store.collect(item.job.job_id, NOW).output == item


@pytest.mark.parametrize('operation', ['wait', 'wait-history-unavailable', 'list', 'acknowledge', 'collect-state'],
                         ids=['wait', 'wait-history-unavailable', 'list', 'acknowledge', 'collect-state'])
def test_mcp_transient_error_mapping(tmp_path, monkeypatch, operation):
    from modelquad.mcp import jobs_tools

    store, item, path = retained(tmp_path)
    patch_lstat(monkeypatch, path.name, denied)
    monkeypatch.setattr(jobs_tools, '_guard', lambda: None)
    monkeypatch.setattr(jobs_tools, 'default_store', lambda: store)
    monkeypatch.setattr(jobs_tools, 'datetime', SimpleNamespace(now=lambda tz: NOW))
    monkeypatch.setattr(jobs_tools, 'schedule_recovery', lambda *a, **kw: None)
    monkeypatch.setattr(jobs_tools.history, 'list_active_jobs', lambda *a: [])
    monkeypatch.setattr(jobs_tools.history, 'read_job', lambda *a: None)
    monkeypatch.setattr(jobs_tools.history, 'read_job_attempt', lambda *a: None)
    monkeypatch.setattr(jobs_tools, 'job_status', lambda *a, **kw: {
        'status': 'finished', 'ended_at': NOW.isoformat(), 'attempt_id': 1})
    # Workspace scoping is covered elsewhere; this job belongs to the caller.
    monkeypatch.setattr(jobs_tools, '_scoped_job', lambda job_id, project: ({'attempt_id': 1}, None))
    project = str(tmp_path)
    if operation == 'wait-history-unavailable':
        monkeypatch.setattr(jobs_tools, 'job_status', lambda *a, **kw: denied(0))
        response = jobs_tools.modelquad_job_wait(item.job.job_id, project, 1)
    elif operation == 'wait':
        response = jobs_tools.modelquad_job_wait(item.job.job_id, project, 1)
    elif operation == 'list':
        from modelquad.jobs import service_client

        def no_service(*a, **kw):
            raise OSError('service absent')

        monkeypatch.setattr(service_client, 'read_pending_summary', no_service)
        response = jobs_tools.modelquad_job_list_pending(project)
    elif operation == 'acknowledge':
        response = jobs_tools.modelquad_job_acknowledge(item.job.job_id, 'receipt', project)
    else:
        monkeypatch.setattr(store, 'collect', lambda *a: None)
        response = jobs_tools.modelquad_job_collect(item.job.job_id, project)
    assert response == {'error': 'pending_output_temporarily_unavailable'}
