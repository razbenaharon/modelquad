# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The retired SessionStart reminder: the adapter writes none and takes back an owned
earlier one. Synthetic lifecycle and ownership; never user settings or trust."""

import copy
import json
from pathlib import Path

import pytest

from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.adapters.codex_hooks import manage_pending_hook
from modelquad.adapters import registration
from modelquad.adapters import staging
from modelquad.adapters.report import ActionKind


@pytest.fixture
def owned(monkeypatch, tmp_path):
    records = {}
    monkeypatch.setenv('MODELQUAD_STATE_MODE', 'dev')
    monkeypatch.setenv('MODELQUAD_STATE_ROOT', str(tmp_path / 'synthetic-state'))
    monkeypatch.setattr(registration, 'read_owned_settings', lambda *a: copy.deepcopy(records))
    def write(value):
        records.clear()
        records.update(copy.deepcopy(value))
    monkeypatch.setattr(registration, 'write_owned_settings', write)
    return records


OPERATIONS = ('install', 'plugin-install', 'uninstall')


def _run(operation, target, *, dry_run=False):
    """Every adapter operation takes the earlier reminder back; none writes one."""
    if operation == 'uninstall':
        return ClaudeAdapter(via_plugin=False).uninstall(target, dry_run=dry_run)
    return ClaudeAdapter(via_plugin=operation == 'plugin-install').install(target, dry_run=dry_run)


def _pending(records):
    return {k: v for k, v in records.items() if 'pending_hook:' in k}


@pytest.mark.parametrize('operation', OPERATIONS)
def test_adapter_writes_no_reminder_and_removes_an_owned_earlier_one(tmp_path, owned, operation):
    """PR-INSTALL-CLAUDE-01: the reminder an earlier install wrote goes, once, with its record."""
    target = tmp_path / 'claude'
    target.mkdir()
    settings = target / 'settings.json'
    foreign = {'matcher': 'foreign', 'hooks': [{'type': 'command', 'command': 'foreign-helper'}]}
    settings.write_text(json.dumps({'unrelated': {'value': 12}, 'hooks': {'SessionStart': [foreign]}}), encoding='utf-8')
    untouched = settings.read_bytes()
    report = _run(operation, target)
    assert all(a.kind is not ActionKind.SKIPPED for a in report.actions)
    # Nothing of ours was there: the user's file is byte-identical and no record appears.
    assert settings.read_bytes() == untouched and not _pending(owned)

    # What the earlier install wrote, with its ownership record.
    assert manage_pending_hook(settings, 'claude').kind is ActionKind.UPDATED
    seeded = json.loads(settings.read_text(encoding='utf-8'))
    handler = seeded['hooks']['SessionStart'][1]['hooks'][0]
    assert handler['timeout'] == 3
    assert set(handler) == {'type', 'command', 'timeout'}
    assert 'hook-pending' in handler['command'] and _pending(owned)

    before = settings.read_bytes()
    assert _run(operation, target, dry_run=True).actions
    assert settings.read_bytes() == before and _pending(owned)

    report = _run(operation, target)
    assert all(a.kind is not ActionKind.SKIPPED for a in report.actions)
    remaining = settings.read_bytes()
    payload = json.loads(remaining)
    assert payload['hooks']['SessionStart'] == [foreign]
    assert payload['unrelated'] == {'value': 12}
    assert 'hook-pending' not in remaining.decode('utf-8')
    assert not any('pending_hook' in key for key in owned)
    _run(operation, target)
    assert settings.read_bytes() == remaining
    assert not any('pending_hook' in key for key in owned)


@pytest.mark.parametrize('host', ['claude'], ids=['PR-INSTALL-02-claude-dry'])
def test_dry_run_creates_no_file_or_ownership(tmp_path, owned, host):
    path = tmp_path / 'not-created' / 'hooks.json'
    assert manage_pending_hook(path, host, dry_run=True).kind is ActionKind.WOULD_CREATE
    assert not path.exists() and owned == {}
    assert manage_pending_hook(path, host, dry_run=True, remove=True).kind is ActionKind.UNCHANGED


@pytest.mark.parametrize('value', ['{', '[]', '{"hooks":[]}', '{"hooks":{"SessionStart":{}}}'],
                         ids=['PR-INSTALL-02-json', 'PR-INSTALL-02-root', 'PR-INSTALL-02-hooks', 'PR-INSTALL-02-event'])
def test_malformed_json_is_preserved(tmp_path, owned, value):
    path = tmp_path / 'hooks.json'
    path.write_text(value, encoding='utf-8')
    before = path.read_bytes()
    assert manage_pending_hook(path, 'claude').kind is ActionKind.SKIPPED
    assert manage_pending_hook(path, 'claude', remove=True).kind is ActionKind.SKIPPED
    assert path.read_bytes() == before and owned == {}


@pytest.mark.parametrize('change', ['command', 'timeout', 'matcher', 'remove'], ids=[
    'PR-INSTALL-02-user-command', 'PR-INSTALL-02-user-timeout',
    'PR-INSTALL-02-user-matcher', 'PR-INSTALL-02-user-removal'])
@pytest.mark.parametrize('host', ['claude'], ids=['claude'])
def test_user_modified_content_is_never_reclaimed(tmp_path, owned, change, host):
    path = tmp_path / 'hooks.json'
    manage_pending_hook(path, host)
    payload = json.loads(path.read_text(encoding='utf-8'))
    group = payload['hooks']['SessionStart'][0]
    if change == 'command':
        group['hooks'][0]['command'] = 'my-own-helper'
    elif change == 'timeout':
        group['hooks'][0]['timeout'] = 7
    elif change == 'matcher':
        group['matcher'] = 'resume'
    else:
        payload['hooks']['SessionStart'] = []
    path.write_text(json.dumps(payload), encoding='utf-8')
    before = path.read_bytes()
    manage_pending_hook(path, host)
    manage_pending_hook(path, host, remove=True)
    assert path.read_bytes() == before


def test_foreign_handler_inside_owned_group_survives(tmp_path, owned):
    """PR-INSTALL-01-sibling: preserve siblings within the owned group."""
    path = tmp_path / 'hooks.json'
    manage_pending_hook(path, 'claude')
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['hooks']['SessionStart'][0]['hooks'].append({'command': 'foreign'})
    path.write_text(json.dumps(payload), encoding='utf-8')
    manage_pending_hook(path, 'claude', remove=True)
    assert json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart'][0]['hooks'] == [{'command': 'foreign'}]


def test_ownership_failure_leaves_config_untouched(tmp_path, monkeypatch, owned):
    """PR-INSTALL-02-ownership: evidence precedes a config write."""
    path = tmp_path / 'hooks.json'
    def unavailable(*a):
        raise registration.BackupPersistenceError()
    monkeypatch.setattr(registration, 'write_owned_settings', unavailable)
    assert manage_pending_hook(path, 'claude').kind is ActionKind.SKIPPED
    assert not path.exists()


@pytest.mark.parametrize('operation', OPERATIONS,
                         ids=['PR-BACKUP-install', 'PR-BACKUP-plugin-install', 'PR-BACKUP-remove'])
@pytest.mark.parametrize('failure', ['backup', 'write'], ids=['backup', 'write'])
def test_failed_update_restores_ownership_and_can_be_retried(
        tmp_path, monkeypatch, owned, operation, failure):
    target = tmp_path / 'claude'
    target.mkdir()
    path = target / 'settings.json'
    foreign = {'matcher': 'foreign', 'hooks': [{'command': 'foreign-helper'}]}
    path.write_text(json.dumps({'unrelated': 12, 'hooks': {'SessionStart': [foreign]}}),
                    encoding='utf-8', newline='\n')
    # What the earlier install wrote, with its ownership record.
    assert manage_pending_hook(path, 'claude').kind is ActionKind.UPDATED
    before, ownership = path.read_bytes(), copy.deepcopy(owned)
    assert _pending(ownership)
    def unavailable(*a, **kw):
        raise OSError('synthetic failure')
    with monkeypatch.context() as failed:
        failed.setattr(staging, 'backup_if_exists' if failure == 'backup' else 'atomic_write', unavailable)
        report = _run(operation, target)
        action = next(a for a in report.actions if 'reminder configuration' in (a.detail or ''))
        assert action.kind is ActionKind.SKIPPED
        # Other adapter actions may retire Claude's timeout ownership;
        # the reminder's prior evidence itself must survive unchanged.
        assert _pending(owned) == _pending(ownership)
        assert path.read_bytes() == before
    report = _run(operation, target)
    assert all('reminder configuration' not in (a.detail or '') for a in report.actions)
    groups = json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart']
    assert groups == [foreign]
    assert not _pending(owned)
    assert json.loads(path.read_text(encoding='utf-8'))['unrelated'] == 12
    stable = path.read_bytes()
    _run(operation, target)
    assert path.read_bytes() == stable


@pytest.mark.parametrize('failing,retry', [
    ('uninstall', 'uninstall'), ('uninstall', 'install'), ('install', 'install'),
    ('plugin-install', 'plugin-install')],
    ids=['PR-RETIRE-remove', 'PR-RETIRE-install', 'PR-RETIRE-install-install', 'PR-RETIRE-plugin'])
def test_failed_ownership_retirement_keeps_the_entry_and_can_be_retried(
        tmp_path, monkeypatch, owned, failing, retry):
    """Evidence is retired before the entry goes; if that fails, the entry stays.

    A removed entry whose record survived would read as "the user removed it",
    so a failed retirement leaves the entry in place, reports it, and lets the
    rest of the operation proceed.
    """
    target = tmp_path / 'claude'
    target.mkdir()
    path = target / 'settings.json'
    foreign = {'matcher': 'foreign', 'hooks': [{'command': 'foreign-helper'}]}
    path.write_text(json.dumps({'unrelated': 12, 'hooks': {'SessionStart': [foreign]}}),
                    encoding='utf-8', newline='\n')
    # What the earlier install wrote, with its ownership record.
    assert manage_pending_hook(path, 'claude').kind is ActionKind.UPDATED
    # A sibling added by the user to our group must survive.
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['hooks']['SessionStart'][1]['hooks'].append({'command': 'user-sibling'})
    path.write_text(json.dumps(payload), encoding='utf-8', newline='\n')
    ownership = copy.deepcopy(_pending(owned))
    assert ownership
    original_write = registration.write_owned_settings

    def reject_retirement(records):
        if not _pending(records):
            raise registration.BackupPersistenceError('synthetic retirement failure')
        original_write(records)

    with monkeypatch.context() as failed:
        failed.setattr(registration, 'write_owned_settings', reject_retirement)
        for _attempt in range(2):
            report = _run(failing, target)
            action = next(a for a in report.actions if 'reminder configuration' in (a.detail or ''))
            assert action.kind is ActionKind.SKIPPED
            assert _pending(owned) == ownership
            groups = json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart']
            assert groups[0] == foreign
            assert 'hook-pending' in groups[1]['hooks'][0]['command']
            assert groups[1]['hooks'][1] == {'command': 'user-sibling'}
            assert json.loads(path.read_text(encoding='utf-8'))['unrelated'] == 12

    report = _run(retry, target)
    assert all(a.kind is not ActionKind.SKIPPED for a in report.actions)
    groups = json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart']
    assert groups[0] == foreign
    assert groups[1]['hooks'] == [{'command': 'user-sibling'}]
    assert _pending(owned) == {}
    stable, stable_ownership = path.read_bytes(), copy.deepcopy(owned)
    _run(retry, target)
    assert path.read_bytes() == stable and owned == stable_ownership
    _run('uninstall', target)
    assert path.read_bytes() == stable and not _pending(owned)


@pytest.mark.parametrize('host', ['claude'], ids=['PR-RETIRE-edit-claude'])
def test_ownership_retirement_failure_preserves_concurrent_user_edit(tmp_path, monkeypatch, owned, host):
    path = tmp_path / 'hooks.json'
    manage_pending_hook(path, host)
    ownership = copy.deepcopy(owned)
    user_bytes = b'{"user_edit": true}\n'

    def reject_with_user_edit(records):
        path.write_bytes(user_bytes)
        raise registration.BackupPersistenceError('synthetic retirement failure')

    with monkeypatch.context() as failed:
        failed.setattr(registration, 'write_owned_settings', reject_with_user_edit)
        assert manage_pending_hook(path, host, remove=True).kind is ActionKind.SKIPPED
    assert path.read_bytes() == user_bytes and owned == ownership
    assert manage_pending_hook(path, host).kind is ActionKind.SKIPPED
    assert manage_pending_hook(path, host, remove=True).kind is ActionKind.UNCHANGED
    assert path.read_bytes() == user_bytes and owned == ownership
