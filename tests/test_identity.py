# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Pseudonymous installation identity."""

import json

from modelquad.identity import (
    IDENTITY_FILE_NAME,
    installation_id,
    peek_installation_id,
    reset_installation_id,
)


def test_installation_id_is_created_lazily_and_stable(tmp_path):
    assert peek_installation_id(state_dir=tmp_path) is None
    assert not (tmp_path / IDENTITY_FILE_NAME).exists()

    first = installation_id(state_dir=tmp_path)
    assert len(first) == 32
    int(first, 16)  # uuid4 hex

    # Stable across reads, and peek sees the same value without creating.
    assert installation_id(state_dir=tmp_path) == first
    assert peek_installation_id(state_dir=tmp_path) == first


def test_identity_file_contains_no_machine_identifiers(tmp_path):
    installation_id(state_dir=tmp_path)
    record = json.loads((tmp_path / IDENTITY_FILE_NAME).read_text(encoding="utf-8"))
    # Exactly the declared fields — nothing derived from the machine.
    assert set(record) == {"identity_version", "installation_id", "created_at"}


def test_reset_mints_a_fresh_unlinked_id(tmp_path):
    first = installation_id(state_dir=tmp_path)
    second = reset_installation_id(state_dir=tmp_path)
    assert second != first
    assert installation_id(state_dir=tmp_path) == second


def test_corrupt_identity_file_is_replaced_not_fatal(tmp_path):
    target = tmp_path / IDENTITY_FILE_NAME
    target.write_text("not json{{", encoding="utf-8")
    fresh = installation_id(state_dir=tmp_path)
    assert len(fresh) == 32
    # The file was healed in place.
    assert peek_installation_id(state_dir=tmp_path) == fresh


def test_identity_record_with_wrong_shape_is_replaced(tmp_path):
    target = tmp_path / IDENTITY_FILE_NAME
    target.write_text(json.dumps({"installation_id": 123}), encoding="utf-8")
    fresh = installation_id(state_dir=tmp_path)
    assert len(fresh) == 32


def test_concurrent_first_runs_leave_one_well_formed_identity(tmp_path):
    """The identity is minted by whichever process reads first.

    The review hook, the CLI and the MCP server can all take that first read
    at once. Each writer stages in its own temp file, so the file on disk is
    always one complete record and no staging file is left behind; the
    losing processes used their own id for that one run only.
    """

    import threading

    barrier = threading.Barrier(12)
    minted: list[str] = []
    failures: list[BaseException] = []

    def first_run() -> None:
        try:
            barrier.wait(timeout=5)
            minted.append(installation_id(state_dir=tmp_path))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    threads = [threading.Thread(target=first_run) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert all(len(value) == 32 for value in minted)
    persisted = peek_installation_id(state_dir=tmp_path)
    assert persisted in minted
    record = json.loads((tmp_path / IDENTITY_FILE_NAME).read_text(encoding="utf-8"))
    assert set(record) == {"identity_version", "installation_id", "created_at"}
    assert [p.name for p in tmp_path.iterdir()] == [IDENTITY_FILE_NAME]
    # Settled: every later read agrees with the file.
    assert installation_id(state_dir=tmp_path) == persisted


def test_a_lost_first_run_race_adopts_the_winners_identity(tmp_path, monkeypatch):
    """Windows refuses a replace onto a file being replaced or read at that instant.

    The loser of a first-run race must not raise out of ``installation_id``
    (the review hook and the MCP server reach it); it adopts the record
    the winner wrote, so every later read agrees with the file.
    """

    from modelquad import identity as identity_mod

    target = tmp_path / IDENTITY_FILE_NAME
    winner = {"identity_version": 1, "installation_id": "a" * 32, "created_at": "2026-09-13T00:00:00+00:00"}
    real_write = identity_mod.atomic_write

    def refused_once_while_the_winner_lands(path, content, **kwargs):
        monkeypatch.setattr(identity_mod, "atomic_write", real_write)
        real_write(target, json.dumps(winner) + chr(10))
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(identity_mod, "atomic_write", refused_once_while_the_winner_lands)

    assert installation_id(state_dir=tmp_path) == "a" * 32
    assert peek_installation_id(state_dir=tmp_path) == "a" * 32


def test_a_persistent_refusal_with_nothing_on_disk_still_raises(tmp_path, monkeypatch):
    from modelquad import identity as identity_mod

    attempts: list[int] = []

    def always_refused(path, content, **kwargs):
        attempts.append(1)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(identity_mod, "atomic_write", always_refused)
    monkeypatch.setattr(identity_mod, "_WRITE_RETRY_SECONDS", 0.0)

    import pytest

    with pytest.raises(PermissionError):
        installation_id(state_dir=tmp_path)
    assert len(attempts) == identity_mod._WRITE_ATTEMPTS
    assert not (tmp_path / IDENTITY_FILE_NAME).exists()


def test_reset_never_adopts_a_concurrent_writers_identity(tmp_path, monkeypatch):
    from modelquad import identity as identity_mod

    target = tmp_path / IDENTITY_FILE_NAME
    other = {"identity_version": 1, "installation_id": "b" * 32, "created_at": "2026-09-13T00:00:00+00:00"}
    real_write = identity_mod.atomic_write
    calls: list[int] = []

    def refused_once(path, content, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            real_write(target, json.dumps(other) + chr(10))
            raise PermissionError(13, "Access is denied")
        real_write(path, content, **kwargs)

    monkeypatch.setattr(identity_mod, "atomic_write", refused_once)
    monkeypatch.setattr(identity_mod, "_WRITE_RETRY_SECONDS", 0.0)

    fresh = reset_installation_id(state_dir=tmp_path)

    assert fresh != "b" * 32
    assert peek_installation_id(state_dir=tmp_path) == fresh
    assert len(calls) == 2
