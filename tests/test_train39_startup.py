# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic Windows XML/start-path regressions; no real OS launch or task."""
from contextlib import contextmanager
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from modelquad import cli
from modelquad.jobs import handle
from modelquad.service import lifecycle


NS = '{http://schemas.microsoft.com/windows/2004/02/mit/task}'
SID = 'S-1-5-21-101-202-303-1001'
ACCOUNT = 'FIXTURE-PC\\FixtureUser'


def live_xml(raw, *, drop_settings=False):
    root = ET.fromstring(raw)
    trigger = root.find('./' + NS + 'Triggers/' + NS + 'LogonTrigger')
    trigger.remove(trigger.find(NS + 'Enabled'))
    trigger.find(NS + 'UserId').text = ACCOUNT
    principal = root.find('./' + NS + 'Principals/' + NS + 'Principal')
    principal.remove(principal.find(NS + 'RunLevel'))
    if drop_settings:
        settings = root.find(NS + 'Settings')
        for key in ('MultipleInstancesPolicy', 'RunOnlyIfIdle', 'StartWhenAvailable', 'Enabled'):
            settings.remove(settings.find(NS + key))
        idle = settings.find(NS + 'IdleSettings')
        idle.remove(idle.find(NS + 'RestartOnIdle'))
    return ET.tostring(root, encoding='unicode')


@pytest.fixture
def task(tmp_path, monkeypatch):
    registrar = lifecycle.TaskSchedulerRegistrar()
    monkeypatch.setattr(registrar, '_is_supported', lambda: True)
    monkeypatch.setattr(registrar, '_current_user_id', lambda: SID)
    def token_account(sid):
        assert sid == SID
        return ACCOUNT
    monkeypatch.setattr(registrar, '_account_name_for_sid', token_account)
    monkeypatch.setattr(lifecycle, 'service_working_directory', lambda: str(tmp_path))
    command = [str(tmp_path / 'fixture.exe'), 'service', 'start', '--supervise', '--port', '8765']
    raw = registrar._task_definition_xml(command, SID, cwd=str(tmp_path))
    return registrar, command, raw


def test_expanded_defaults_are_equivalent_during_rechecks(task):
    registrar, command, raw = task
    expanded = raw.replace('</Settings>', '<AllowStartOnDemand>true</AllowStartOnDemand>'
        '<AllowHardTerminate>true</AllowHardTerminate><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>'
        '<WakeToRun>false</WakeToRun><Hidden>false</Hidden><Priority>7</Priority></Settings>')
    expanded = expanded.replace('</IdleSettings>', '<Duration>PT10M</Duration><WaitTimeout>PT1H</WaitTimeout></IdleSettings>')
    assert registrar.verify_definition(expanded, command) is None
    assert registrar._binding(expanded) == registrar._binding(raw)


def test_explicit_registration_accepts_normalized_owned_task_and_post_create_definition(task, monkeypatch):
    registrar, command, generated = task
    live = live_xml(generated, drop_settings=True)
    monkeypatch.setattr(registrar, 'definition', lambda: ('present', live))
    calls = []
    def create(argv):
        calls.append(argv)
        return lifecycle.RegistrationResult(True, True, True, 'synthetic created', 'register')
    monkeypatch.setattr(registrar, '_create_from_definition', create)
    result = registrar.register(command=command)
    assert result.success is True and len(calls) == 1
    assert calls[0][:4] == ('schtasks', '/create', '/tn', registrar.TASK_NAME)


@pytest.mark.parametrize('key', ['DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries',
                              'ExecutionTimeLimit', 'StopOnIdleEnd', 'RestartOnFailure'])
def test_omitting_nondefault_controls_requires_update(task, key):
    registrar, command, raw = task
    root = ET.fromstring(live_xml(raw))
    settings = root.find(NS + 'Settings')
    parent = settings.find(NS + 'IdleSettings') if key == 'StopOnIdleEnd' else settings
    parent.remove(parent.find(NS + key))
    assert registrar.verify_definition(ET.tostring(root, encoding='unicode'), command) == 'background_startup_update_required'


@pytest.mark.parametrize('old,new', [
    (ACCOUNT, 'FIXTURE-PC\\OtherUser'), (ACCOUNT, 'OTHER-PC\\FixtureUser'),
    (ACCOUNT, 'S-1-5-21-101-202-303-1002'),
    ('fixture.exe', 'other.exe'), ('--supervise', '--other'),
    ('<ns0:WorkingDirectory>', '<ns0:WorkingDirectory>other-'),
    ('<ns0:LogonTrigger>', '<ns0:LogonTrigger><ns0:Enabled>false</ns0:Enabled>'),
    ('<ns0:LogonType>InteractiveToken', '<ns0:LogonType>Password'),
])
def test_live_shape_does_not_hide_foreign_bindings(task, old, new):
    registrar, command, raw = task
    changed = live_xml(raw).replace(old, new)
    assert changed != live_xml(raw)
    assert registrar.verify_definition(changed, command) == 'background_startup_foreign'
    assert registrar.verify_definition(changed, command, allow_update=True) == 'background_startup_foreign'


def test_only_trigger_account_spelling_is_normalized(task, monkeypatch):
    registrar, command, raw = task
    assert registrar.verify_definition(live_xml(raw).replace(ACCOUNT, ACCOUNT.lower()), command) is None
    changed = live_xml(raw).replace('<ns0:UserId>' + SID, '<ns0:UserId>' + ACCOUNT)
    assert registrar.verify_definition(changed, command) == 'background_startup_foreign'
    def unavailable(_):
        raise OSError('synthetic identity unavailable')
    monkeypatch.setattr(registrar, '_account_name_for_sid', unavailable)
    assert registrar.verify_definition(live_xml(raw), command) == 'background_startup_unknown'
    assert registrar.verify_definition(raw, command) is None  # SID needs no name lookup


@pytest.mark.parametrize('change', ['unknown-trigger', 'unknown-settings', 'unknown-idle',
                                  'unknown-metadata', 'unknown-attribute', 'duplicate'])
def test_unknown_structure_never_becomes_owned_even_for_update(task, change):
    registrar, command, raw = task
    root = ET.fromstring(live_xml(raw))
    parents = {'unknown-trigger': './Triggers/LogonTrigger', 'unknown-settings': './Settings',
               'unknown-idle': './Settings/IdleSettings', 'unknown-metadata': './RegistrationInfo'}
    if change in parents:
        parent = root.find('/'.join(NS + part if part != '.' else part for part in parents[change].split('/')))
        ET.SubElement(parent, NS + 'UnknownControl').text = 'true'
    elif change == 'unknown-attribute':
        root.find(NS + 'Settings').set('unknown', 'true')
    else:
        ET.SubElement(root.find(NS + 'Settings'), NS + 'Enabled').text = 'true'
    changed = ET.tostring(root, encoding='unicode')
    assert registrar.verify_definition(changed, command) == 'background_startup_unknown'
    assert registrar.verify_definition(changed, command, allow_update=True) == 'background_startup_unknown'


def test_normalized_legacy_action_still_requires_explicit_update(task):
    registrar, command, raw = task
    legacy = live_xml(raw.replace('"--supervise" ', ''))
    assert '--supervise' not in legacy
    assert registrar.verify_definition(legacy, command) == 'background_startup_update_required'
    assert registrar.verify_definition(legacy, command, allow_update=True) is None


@pytest.mark.parametrize('fail', [False, True])
def test_token_account_lookup_uses_sid_sizes_and_always_frees_memory(monkeypatch, fail):
    import ctypes
    from modelquad.service import auth
    calls, freed = [], []
    def convert(value, output):
        assert value == SID
        output._obj.value = 123
        return True
    def lookup(system, sid, name, name_size, domain, domain_size, use):
        assert system is None and sid.value == 123
        calls.append('query' if name is None else 'read')
        if name is None:
            name_size._obj.value = 12
            domain_size._obj.value = 11
            return False
        assert name_size._obj.value == 12 and domain_size._obj.value == 11
        if fail:
            return False
        name.value, domain.value, use._obj.value = 'FixtureUser', 'FIXTURE-PC', 1
        return True
    adv = SimpleNamespace(ConvertStringSidToSidW=convert, LookupAccountSidW=lookup)
    kernel = SimpleNamespace(LocalFree=lambda sid: freed.append(sid.value))
    monkeypatch.setattr(auth, '_win', lambda: (kernel, adv))
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 122, raising=False)
    if fail:
        with pytest.raises(ValueError, match='account name is unavailable'):
            lifecycle.TaskSchedulerRegistrar._account_name_for_sid(SID)
    else:
        assert lifecycle.TaskSchedulerRegistrar._account_name_for_sid(SID) == ACCOUNT
    assert calls == ['query', 'read'] and freed == [123]
