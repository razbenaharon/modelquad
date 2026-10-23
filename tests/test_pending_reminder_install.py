# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic adapter lifecycle and ownership; never user settings or trust."""

import copy
import json
from pathlib import Path

import pytest

from orchestrator.adapters.claude_adapter import ClaudeAdapter
from orchestrator.adapters.codex_hooks import manage_pending_hook
from orchestrator.adapters import registration
from orchestrator.adapters import staging
from orchestrator.adapters.report import ActionKind
from orchestrator import hostruntime


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


@pytest.mark.parametrize('adapter,host,filename', [
    (ClaudeAdapter, 'claude', 'settings.json')],
    ids=['PR-INSTALL-CLAUDE-01'])
def test_adapter_install_upgrade_uninstall_is_idempotent(tmp_path, monkeypatch, owned, adapter, host, filename):
    target = tmp_path / host
    target.mkdir()
    settings = target / filename
    foreign = {'matcher': 'foreign', 'hooks': [{'type': 'command', 'command': 'foreign-helper'}]}
    settings.write_text(json.dumps({'unrelated': {'value': 12}, 'hooks': {'SessionStart': [foreign]}}), encoding='utf-8')
    first = adapter().install(target)
    assert all(a.kind is not ActionKind.SKIPPED for a in first.actions)
    before = settings.read_bytes()
    adapter().install(target)
    assert settings.read_bytes() == before
    payload = json.loads(before)
    assert payload['hooks']['SessionStart'][0] == foreign
    assert payload['unrelated'] == {'value': 12}
    handler = payload['hooks']['SessionStart'][1]['hooks'][0]
    assert handler['timeout'] == 3
    assert set(handler) == {'type', 'command', 'timeout'}
    monkeypatch.setattr(hostruntime, 'cli_launch_command', lambda: '/synthetic/upgrade/modelquad.exe')
    adapter().install(target)
    upgraded = json.loads(settings.read_text(encoding='utf-8'))['hooks']['SessionStart']
    assert len(upgraded) == 2 and upgraded[0] == foreign
    assert '/synthetic/upgrade/modelquad.exe' in upgraded[1]['hooks'][0]['command']
    adapter().uninstall(target)
    remaining = settings.read_bytes()
    adapter().uninstall(target)
    assert settings.read_bytes() == remaining
    assert json.loads(remaining)['hooks']['SessionStart'] == [foreign]
    assert json.loads(remaining)['unrelated'] == {'value': 12}
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


@pytest.mark.parametrize('adapter,host,filename', [
    (ClaudeAdapter, 'claude', 'settings.json')],
    ids=['claude'])
@pytest.mark.parametrize('operation', ['install', 'upgrade', 'remove'],
                         ids=['PR-BACKUP-install', 'PR-BACKUP-upgrade', 'PR-BACKUP-remove'])
@pytest.mark.parametrize('failure', ['backup', 'write'], ids=['backup', 'write'])
def test_failed_update_restores_ownership_and_can_be_retried(
        tmp_path, monkeypatch, owned, adapter, host, filename, operation, failure):
    target = tmp_path / host
    target.mkdir()
    path = target / filename
    foreign = {'matcher': 'foreign', 'hooks': [{'command': 'foreign-helper'}]}
    path.write_text(json.dumps({'unrelated': 12, 'hooks': {'SessionStart': [foreign]}}),
                    encoding='utf-8', newline='\n')
    adapter().install(target)
    if operation == 'install':
        manage_pending_hook(path, host, remove=True)
    elif operation == 'upgrade':
        original = hostruntime.integration_command
        def upgraded(*args, **kwargs):
            if args[0] == 'hook-pending':
                return f'"/synthetic/upgrade/modelquad.exe" hook-pending {host}'
            return original(*args, **kwargs)
        monkeypatch.setattr(hostruntime, 'integration_command', upgraded)
    before, ownership = path.read_bytes(), copy.deepcopy(owned)
    def unavailable(*a, **kw):
        raise OSError('synthetic failure')
    with monkeypatch.context() as failed:
        failed.setattr(staging, 'backup_if_exists' if failure == 'backup' else 'atomic_write', unavailable)
        report = (adapter().uninstall(target) if operation == 'remove' else adapter().install(target))
        action = next(a for a in report.actions if 'reminder configuration' in (a.detail or ''))
        assert action.kind is ActionKind.SKIPPED
        # Other adapter actions may retire Claude's timeout ownership on remove;
        # the reminder's prior evidence itself must survive unchanged.
        pending_records = lambda records: {k: v for k, v in records.items() if 'pending_hook:' in k}
        assert pending_records(owned) == pending_records(ownership)
        if operation != 'remove':
            assert path.read_bytes() == before
        else:
            assert json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart'] == json.loads(before)['hooks']['SessionStart']
    report = (adapter().uninstall(target) if operation == 'remove' else adapter().install(target))
    assert all('reminder configuration' not in (a.detail or '') for a in report.actions)
    groups = json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart']
    assert groups[0] == foreign
    assert len(groups) == (1 if operation == 'remove' else 2)
    if operation == 'upgrade':
        assert '/synthetic/upgrade/modelquad.exe' in groups[1]['hooks'][0]['command']
    assert json.loads(path.read_text(encoding='utf-8'))['unrelated'] == 12
    stable = path.read_bytes()
    (adapter().uninstall(target) if operation == 'remove' else adapter().install(target))
    assert path.read_bytes() == stable


@pytest.mark.parametrize('adapter,host,filename', [
    (ClaudeAdapter, 'claude', 'settings.json')],
    ids=['claude'])
@pytest.mark.parametrize('retry', ['remove', 'install'], ids=['PR-RETIRE-remove', 'PR-RETIRE-install'])
def test_failed_ownership_retirement_keeps_the_entry_and_can_be_retried(
        tmp_path, monkeypatch, owned, adapter, host, filename, retry):
    """Evidence is retired before the entry goes; if that fails, the entry stays.

    A removed entry whose record survived would read as "the user removed it"
    and never come back, so a failed retirement leaves the entry in place,
    reports it, and lets the rest of the uninstall proceed.
    """
    target = tmp_path / host
    target.mkdir()
    path = target / filename
    foreign = {'matcher': 'foreign', 'hooks': [{'command': 'foreign-helper'}]}
    path.write_text(json.dumps({'unrelated': 12, 'hooks': {'SessionStart': [foreign]}}),
                    encoding='utf-8', newline='\n')
    adapter().install(target)
    # A sibling added by the user to our group must survive.
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['hooks']['SessionStart'][1]['hooks'].append({'command': 'user-sibling'})
    path.write_text(json.dumps(payload), encoding='utf-8', newline='\n')
    pending_records = lambda records: {k: v for k, v in records.items() if 'pending_hook:' in k}
    ownership = copy.deepcopy(pending_records(owned))
    original_write = registration.write_owned_settings

    def reject_retirement(records):
        if not pending_records(records):
            raise registration.BackupPersistenceError('synthetic retirement failure')
        original_write(records)

    with monkeypatch.context() as failed:
        failed.setattr(registration, 'write_owned_settings', reject_retirement)
        for _attempt in range(2):
            report = adapter().uninstall(target)
            action = next(a for a in report.actions if 'reminder configuration' in (a.detail or ''))
            assert action.kind is ActionKind.SKIPPED
            assert pending_records(owned) == ownership
            groups = json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart']
            assert groups[0] == foreign
            assert groups[1]['hooks'][1] == {'command': 'user-sibling'}
            assert json.loads(path.read_text(encoding='utf-8'))['unrelated'] == 12

    operation = adapter().uninstall if retry == 'remove' else adapter().install
    report = operation(target)
    assert all(a.kind is not ActionKind.SKIPPED for a in report.actions)
    groups = json.loads(path.read_text(encoding='utf-8'))['hooks']['SessionStart']
    assert groups[0] == foreign
    assert groups[1]['hooks'][-1] == {'command': 'user-sibling'}
    assert len(groups[1]['hooks']) == (1 if retry == 'remove' else 2)
    assert pending_records(owned) == ({} if retry == 'remove' else ownership)
    stable, stable_ownership = path.read_bytes(), copy.deepcopy(owned)
    operation(target)
    assert path.read_bytes() == stable and owned == stable_ownership
    adapter().uninstall(target)
    assert not pending_records(owned)
    adapter().install(target)
    stable = path.read_bytes()
    adapter().install(target)
    assert path.read_bytes() == stable


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
