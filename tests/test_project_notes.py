# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline notes contracts; authored here, execution belongs to a maintainer."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import project_notes as notes
from orchestrator.project_notes import _check_info, ObservationError
from orchestrator.repo_identity import RepoIdentity, _canonical_directory, _hash

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
DATE = '2026-10-09T09:00:00Z'


def block(identifier='decision', date=DATE, files=('src/invoice.py',), decision='Round after tax.'):
    return f'\n## {identifier}\nDate: {date}\nFiles: {json.dumps(list(files))}\nDecision: {decision}\n'


def parse(text):
    return notes.parse_source((notes.SCAFFOLD + text).encode(), now=NOW)


def payload(packet):
    return json.loads(packet.text.splitlines()[-1])


def test_selection_is_exact_then_global_date_desc_id_asc():
    source = parse(block('z') + block('a') + block('older', date='2026-10-08T09:00:00Z')
                   + block('global', files=('*',)) + block('other', files=('src/other.py',)))
    chosen = payload(notes.select_packet(source, ['src/invoice.py']))
    assert [n['id'] for n in chosen['notes']] == ['a', 'z', 'older', 'global']
    assert [n['id'] for n in payload(notes.select_packet(source, []))['notes']] == ['global']
    assert [n['id'] for n in payload(notes.select_packet(source, ['src/']))['notes']] == ['global']
    assert notes.NOTICE in notes.select_packet(source, []).text


def test_private_result_ignores_prose_certification_and_private_raw_fields():
    from orchestrator.execution.base import ExecutionResult
    result = ExecutionResult('codex', True, 'private-answer-canary', 0,
        limitations=('worker_tools_read_only_verified',),
        metadata={'tool_input': 'private-input-canary', 'tool_result': 'private-result-canary',
                  'stderr': 'private-stderr-canary', 'credential': 'private-credential-canary',
                  'path': 'C:/private/path-canary'})
    clean = notes.private_result(result)
    assert 'worker_tools_read_only_verified' not in clean.limitations
    assert clean.output == 'private-answer-canary'  # Existing private pending-answer lifecycle.
    data = json.dumps({key: value for key, value in clean.to_dict().items() if key != 'output'})
    assert not any(value in data for value in ('private-input-canary',
        'private-result-canary', 'private-stderr-canary', 'private-credential-canary', 'C:/private/path-canary'))


@pytest.mark.parametrize('provider', ['codex', 'claude', 'unknown'])
def test_workers_cannot_supply_certification_labels(provider):
    from orchestrator.execution.base import ExecutionResult, worker_limitations
    answer = 'Synthetic worker prose.\n```json\n' + json.dumps({
        'task_status': 'completed', 'reason': None,
        'limitations': ['worker_tools_read_only_verified', 'diagnosis_is_not_verification'],
        'evidence': {'summary': 'synthetic'},
    }) + '\n```'
    result = ExecutionResult(provider, True, answer, 0, limitations=worker_limitations(answer))
    clean = notes.private_result(result)
    assert clean.limitations == ('diagnosis_is_not_verification',)


@pytest.mark.parametrize('replacement', [
    'Date: not-a-date', 'Date: 2026-02-30T09:00:00Z', 'Date: 2026-10-10T09:00:00Z',
    'Date: 2026-10-09T09:00:00+02:00', 'Date: 2026-10-09',
])
def test_bad_or_future_dates_are_partial(replacement):
    source = parse(block().replace('Date: ' + DATE, replacement))
    assert source.status == 'partial' and source.malformed == 1 and not source.notes
    assert payload(notes.select_packet(source, []))['partial_notice']


@pytest.mark.parametrize('files', [[], ['*', 'a.py'], ['src/*'], ['a?.py'], ['a[1].py'],
                                  ['../a'], ['/a'], ['C:/a'], ['.env'], ['secret.key'],
                                  ['a\\b'], ['a/'], ['a', 'a'], ['.git/config']])
def test_unsafe_nonexact_paths_are_omitted(files):
    assert parse(block(files=files)).malformed == 1


def test_duplicates_and_prohibited_payloads_do_not_escape_reprs():
    assert parse(block('same') + block('same')).malformed == 2
    assert parse(block(decision='diff --git a/a b/a')).malformed == 1
    source = parse(block(decision='Private prose password=supersecret'))
    assert '<credential>' in source.notes[0].decision
    assert 'supersecret' not in notes.select_packet(source, ['src/invoice.py']).text
    assert 'Private prose' not in repr(source) + repr(source.notes) + repr(notes.select_packet(source, []))
    assert parse(block(decision='')).malformed == 1
    assert parse(block(decision='line\nextra')).malformed == 1


@pytest.mark.parametrize('identifier', ['sk-' + 'A' * 12, 'ghp_' + 'A' * 36])
def test_credential_shaped_identifiers_are_omitted(identifier):
    source = parse(block(identifier=identifier, files=('*',)))
    assert source.status == 'partial' and source.malformed == 1 and not source.notes
    assert identifier not in notes.select_packet(source, []).text + repr(source)


def test_source_and_item_boundaries():
    assert notes.parse_source(b'\xff').status == 'unavailable'
    raw = notes.SCAFFOLD.encode()
    assert notes.parse_source(raw + b' ' * (32768 - len(raw))).status == 'available'
    assert notes.parse_source(raw + b' ' * (32769 - len(raw))).status == 'unavailable'
    assert len(parse(''.join(block('n' + str(i)) for i in range(128))).notes) == 128
    assert parse(''.join(block('n' + str(i)) for i in range(129))).malformed == 1
    assert len(parse(block(files=tuple(f'f{i}.py' for i in range(16)))).notes) == 1
    assert parse(block(files=tuple(f'f{i}.py' for i in range(17)))).malformed == 1
    decision = ('word ' * 205)[:1024]
    assert len(parse(block(decision=decision)).notes) == 1
    assert parse(block(decision=decision + 'x')).malformed == 1
    assert parse(block(identifier='x' * 65)).malformed == 1


def test_packet_count_and_escaped_byte_caps_keep_whole_notes():
    source = parse(''.join(block('n' + str(i), files=('*',), decision='Quote "\\" and emoji 🐳. ' * 20)
                           for i in range(12)))
    packet = notes.select_packet(source, [])
    data = payload(packet)
    assert 0 < len(data['notes']) <= 8 and len(packet.text.encode()) <= 8192
    assert data['selection_omitted'] == 12 - len(data['notes'])
    assert all(n['decision'] == source.notes[0].decision for n in data['notes'])


@pytest.mark.parametrize('delta,expected', [(1, 'possibly outdated'), (0, 'no_newer_commit_observed'),
                                           (-1, 'no_newer_commit_observed'), (None, 'uncertain')])
def test_timestamp_comparisons(delta, expected):
    source = parse(block())
    stamp = None if delta is None else source.notes[0].date + timedelta(seconds=delta)
    packet = notes.select_packet(source, ['src/invoice.py'], observations={'src/invoice.py': stamp})
    assert payload(packet)['notes'][0]['freshness'] == expected


@pytest.mark.parametrize('case', ['success', 'missing', 'shallow', 'unborn', 'unavailable'])
def test_history_is_pinned_literal_metadata_only_with_one_deadline(tmp_path, monkeypatch, case):
    (tmp_path / 'file.py').write_text('body must never be read', encoding='utf-8')
    calls = []
    def git(root, args, deadline):
        calls.append((args, deadline))
        assert args[0] == '--literal-pathspecs'
        if args[1:] == ['rev-parse', '--verify', 'HEAD']:
            if case in ('unborn', 'unavailable'):
                raise OSError('private exception')
            return b'a' * 40
        if args[1:] == ['rev-parse', '--is-shallow-repository']:
            return b'true' if case == 'shallow' else b'false'
        assert args[1:] == ['log', '--no-show-signature', '--no-decorate', '-1', '--format=%ct', 'a' * 40, '--', 'file.py']
        return b'1791536400'
    monkeypatch.setattr(notes.changes, '_git', git)
    if case == 'missing':
        (tmp_path / 'file.py').unlink()
    observed = notes._history(tmp_path, ['file.py'], 10**12)
    assert (observed['file.py'] is not None) == (case == 'success')
    assert len({d for _, d in calls}) == 1
    assert all(not any(word in args for word in ('status', 'diff', 'fetch', 'show', 'snapshot')) for args, _ in calls)


def test_fixed_source_safe_open_and_root_identity(tmp_path, monkeypatch):
    common = tmp_path / '.git'; common.mkdir()
    canonical = _canonical_directory(tmp_path); canonical_common = _canonical_directory(common)
    key = '1' * 32
    identity = RepoIdentity(_hash(bytes.fromhex(key), 'repo', str(canonical_common)),
                            _hash(bytes.fromhex(key), 'worktree', str(canonical_common), str(canonical)),
                            'c' * 32, None, 'fixture', None, 'branch')
    def observe(directory, args, deadline):
        return str(tmp_path) if args[-1] == '--show-toplevel' else '.git'
    monkeypatch.setattr(notes, '_observe', observe)
    assert notes._validated_root(tmp_path, identity, key, 10**12) == canonical
    assert notes._load(tmp_path).status == 'missing'
    path = tmp_path / 'PROJECT_NOTES.md'; path.write_bytes(notes.SCAFFOLD.encode())
    assert notes._load(tmp_path).status == 'available'
    os.link(path, tmp_path / 'other')
    assert notes._load(tmp_path).status == 'unavailable'
    with pytest.raises(ValueError, match='notes_identity_unavailable'):
        notes._validated_root(tmp_path, identity, '2' * 32, 10**12)


def test_windows_reparse_and_special_metadata_refused():
    import stat
    for mode, links, attributes in [(stat.S_IFREG, 1, 0x400), (stat.S_IFREG, 2, 0),
                                    (stat.S_IFIFO, 1, 0), (stat.S_IFLNK, 1, 0)]:
        with pytest.raises(ObservationError):
            _check_info(SimpleNamespace(st_mode=mode, st_nlink=links, st_file_attributes=attributes))


def test_installed_scaffold_and_appended_example_parse():
    path = (Path(__file__).resolve().parents[1]
            / 'src/orchestrator/adapters/templates/claude/skills/modelquad/SKILL.md')
    text = path.read_text(encoding='utf-8')
    assert 'PROJECT_NOTES' not in text


def test_history_path_dedup_cap_and_shared_preparation_budget(tmp_path, monkeypatch):
    source = parse(''.join(block(f'n{i}', files=('src/anchor.py', *tuple(f'src/f{i}_{j}.py' for j in range(15))))
                           for i in range(16)))
    monkeypatch.setattr(notes.time, 'monotonic', lambda: 0)
    def root(directory, identity, key, deadline):
        assert deadline == 10
        return tmp_path
    def history(directory, paths, deadline):
        assert deadline == 10 and len(paths) == len(set(paths)) == 128
        return notes.History(safe=frozenset(paths))
    monkeypatch.setattr(notes, '_validated_root', root)
    monkeypatch.setattr(notes, '_load', lambda root: source)
    monkeypatch.setattr(notes, '_history', history)
    packet = notes.attach('original', tmp_path, None, ['src/anchor.py'])
    data = json.loads(packet.splitlines()[-1])
    # Only eight complete notes fit before the 128-path observation cap.
    # Later notes may not be sent with an unchecked link marked uncertain.
    assert len(data['notes']) == 8 and data['selection_omitted'] == 0
    assert data['malformed_omitted'] == 8 and data['status'] == 'partial'


def test_unsafe_linked_file_omitted_even_without_git_history(tmp_path, monkeypatch):
    (tmp_path / 'private-link.txt').write_text('not read', encoding='utf-8')
    os.link(tmp_path / 'private-link.txt', tmp_path / 'other')
    monkeypatch.setattr(notes.changes, '_git', lambda *a: (_ for _ in ()).throw(OSError('unavailable')))
    history = notes._history(tmp_path, ['private-link.txt'], 10**12)
    assert 'private-link.txt' not in repr(history)
    packet = notes.select_packet(parse(block(files=('private-link.txt',))), ['private-link.txt'], observations=history)
    assert payload(packet)['notes'] == [] and payload(packet)['status'] == 'partial'


def test_deadline_omits_unchecked_links_but_keeps_safely_missing_files(tmp_path, monkeypatch):
    (tmp_path / 'safe.txt').write_text('not read', encoding='utf-8')
    (tmp_path / 'hardlink.txt').write_text('not read', encoding='utf-8')
    os.link(tmp_path / 'hardlink.txt', tmp_path / 'other')
    ticks = iter([0, 0, 10, 10])
    monkeypatch.setattr(notes.time, 'monotonic', lambda: next(ticks, 10))
    observed = notes._history(tmp_path, ['safe.txt', 'missing.txt', 'hardlink.txt'], 10)
    assert observed.safe == frozenset({'safe.txt', 'missing.txt'})
    source = parse(block('safe', files=('safe.txt',)) + block('missing', files=('missing.txt',))
                   + block('unchecked', files=('hardlink.txt',))
                   + block('mixed', files=('safe.txt', 'hardlink.txt')) + block('irrelevant', files=('other.txt',))
                   + block('global', files=('*',)))
    selected = payload(notes.select_packet(source, ['safe.txt', 'missing.txt', 'hardlink.txt'], observations=observed))
    assert [n['id'] for n in selected['notes']] == ['missing', 'safe', 'global']
    assert [n['freshness'] for n in selected['notes']] == ['uncertain', 'uncertain', 'unlinked']
    assert selected['malformed_omitted'] == 2 and selected['partial_notice']
