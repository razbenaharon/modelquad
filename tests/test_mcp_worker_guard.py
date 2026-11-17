# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A worker never re-enters ModelQuad (AGENTS.md invariant 1).

The environment marker is unconditional; the ancestry layer reads the small
record kept while a worker runs. The refusal each tool gives a worker is in
tests/test_review_flow.py.
"""

from __future__ import annotations

import time

from modelquad.mcp import worker_guard


def test_a_started_worker_is_recorded_until_it_ends(tmp_path):
    worker_guard.register(111, state_root=tmp_path, now=1000.0)
    worker_guard.register(222, state_root=tmp_path, now=1000.0 - worker_guard.RECORD_SECONDS - 1)

    # The leftover is dropped and removed; the live one stays.
    assert worker_guard.active_workers(state_root=tmp_path, now=1000.0) == [(111, 1000.0)]
    assert sorted(path.name for path in (tmp_path / worker_guard.FOLDER_NAME).iterdir()) == ["111.json"]

    worker_guard.unregister(111, state_root=tmp_path)
    assert worker_guard.active_workers(state_root=tmp_path, now=1000.0) == []


def test_the_environment_marker_alone_makes_a_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("MODELQUAD_WORKER", "1")

    assert worker_guard.is_worker(tmp_path) is True


def test_a_recorded_ancestor_makes_a_worker_even_without_the_marker(monkeypatch, tmp_path):
    import os

    from modelquad import procutil

    monkeypatch.delenv("MODELQUAD_WORKER", raising=False)
    me, now = os.getpid(), time.time()
    monkeypatch.setattr(procutil, "_windows_process_snapshot",
                        lambda: [(me, 900, "python.exe"), (900, 1, "codex.exe"), (1, 0, "init")])
    monkeypatch.setattr(procutil, "process_creation_epoch",
                        lambda pid: {me: now, 900: now - 1.0, 1: now - 5000.0}[pid])
    monkeypatch.setattr(procutil, "pid_state", lambda pid: "alive")

    assert worker_guard.is_worker(tmp_path) is False
    worker_guard.register(900, state_root=tmp_path, now=now - 0.5)
    assert worker_guard.is_worker(tmp_path) is True
    # A reused number that started long before the record is not that worker.
    worker_guard.register(1, state_root=tmp_path, now=now - 0.5)
    worker_guard.unregister(900, state_root=tmp_path)
    assert worker_guard.is_worker(tmp_path) is False
