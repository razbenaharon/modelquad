# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A usage snapshot may only ever be replaced by a newer provider observation.

The live regression these cover: the former Control Center's refresh reached Codex's
app-server and showed 100% remaining, then the card snapped back to a 3%
reading taken four days earlier, because an older reading overwrote a live one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from modelquad.usage import write_usage_snapshot, write_usage_snapshot_if_newer
from modelquad.usage.refresh import refresh_provider


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
FOUR_DAYS_AGO = NOW - timedelta(days=4)


def _snapshot(observed: datetime, seven_day_used: float, **extra) -> dict:
    return {
        "provider": "codex",
        "available": True,
        "five_hour_used_percentage": None,
        "five_hour_remaining_percentage": None,
        "seven_day_used_percentage": seven_day_used,
        "seven_day_remaining_percentage": 100.0 - seven_day_used,
        "observed_at": observed.isoformat(),
        "collected_at": observed.isoformat(),
        **extra,
    }


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The guard itself
# --------------------------------------------------------------------------


def test_writes_when_no_snapshot_exists(tmp_path):
    target = tmp_path / "usage-codex.json"

    assert write_usage_snapshot_if_newer(target, _snapshot(NOW, 0.0)) is True
    assert _read(target)["seven_day_remaining_percentage"] == 100.0


def test_writes_a_newer_observation(tmp_path):
    target = tmp_path / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(FOUR_DAYS_AGO, 97.0))

    assert write_usage_snapshot_if_newer(target, _snapshot(NOW, 0.0)) is True
    assert _read(target)["seven_day_remaining_percentage"] == 100.0


def test_writes_an_equally_old_observation(tmp_path):
    """Re-publishing the same reading is not a rollback, so it is allowed."""

    target = tmp_path / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(NOW, 40.0))

    assert write_usage_snapshot_if_newer(target, _snapshot(NOW, 40.0)) is True


def test_declines_an_older_observation_but_records_the_check(tmp_path):
    target = tmp_path / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(NOW, 0.0))

    stale = _snapshot(FOUR_DAYS_AGO, 97.0)
    stale["collected_at"] = NOW.isoformat()
    assert write_usage_snapshot_if_newer(target, stale) is False

    kept = _read(target)
    assert kept["seven_day_remaining_percentage"] == 100.0
    assert kept["observed_at"] == NOW.isoformat()
    # We did look again, and that is worth recording even though the reading
    # was discarded -- the debounce upstream reads this back.
    assert kept["collected_at"] == NOW.isoformat()


def test_declines_a_reading_that_carries_no_observation(tmp_path):
    target = tmp_path / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(NOW, 0.0))

    blank = _snapshot(NOW, 0.0)
    blank["observed_at"] = None
    blank["seven_day_remaining_percentage"] = None
    blank["seven_day_used_percentage"] = None

    assert write_usage_snapshot_if_newer(target, blank, now=lambda: NOW) is False
    assert _read(target)["seven_day_remaining_percentage"] == 100.0


def test_supersedes_a_snapshot_that_cannot_be_placed_in_time(tmp_path):
    target = tmp_path / "usage-codex.json"
    undatable = _snapshot(FOUR_DAYS_AGO, 97.0)
    undatable["observed_at"] = None
    write_usage_snapshot(target, undatable)

    assert write_usage_snapshot_if_newer(target, _snapshot(NOW, 0.0)) is True
    assert _read(target)["seven_day_remaining_percentage"] == 100.0


# --------------------------------------------------------------------------
# The three writers that used to roll the reading back
# --------------------------------------------------------------------------


def test_refresh_keeps_the_newer_reading_when_the_active_probe_fails(tmp_path):
    """A reading older than what we already have is discarded."""

    usage_dir = tmp_path / ".claude"
    usage_dir.mkdir()
    target = usage_dir / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(NOW, 0.0, source="codex_app_server"))

    def stale_reading(_directory, *, now=None):
        return _snapshot(FOUR_DAYS_AGO, 97.0, source="codex_app_server")

    result = refresh_provider(
        "codex",
        usage_dir,
        now=lambda: NOW,
        force=True,
        refreshers={"codex": stale_reading},
    )

    assert result.remaining_percentage == 100.0
    assert result.observed_at == NOW.isoformat()
    assert result.newer_observation is False
    assert _read(target)["seven_day_remaining_percentage"] == 100.0


# --------------------------------------------------------------------------
# The compare and the write are one critical section
# --------------------------------------------------------------------------
#
# Each write was already atomic; the read-then-compare-then-write was not.
# Two processes could both read the same old snapshot, both judge themselves
# newer, and the one that finished second won regardless of which reading was
# newer. These pin the per-file OS advisory lock that serialises the whole
# operation. The lock lives on a persistent sibling file whose *existence*
# means nothing -- only the kernel lock on it does -- so a dead holder needs no
# stale-lock takeover, and nothing is ever unlinked.

import threading
import time

from modelquad.usage import base as usage_base

# Lock coordination needs slack on the shared, memory-constrained runner.
LOAD_HEADROOM_S = 60


def _hold_lock(target: Path, *, until: threading.Event, held: threading.Event) -> None:
    """Hold the snapshot lock the way another process would, until told."""

    descriptor = usage_base._claim_snapshot_lock(usage_base._snapshot_lock_path(target), wait_seconds=LOAD_HEADROOM_S)
    assert descriptor is not None
    held.set()
    try:
        until.wait()  # The test always releases the holder in finally.
    finally:
        usage_base._release_snapshot_lock(descriptor)


def test_guard_holds_the_file_lock_across_the_compare_and_the_write(tmp_path, monkeypatch):
    target = tmp_path / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(FOUR_DAYS_AGO, seven_day_used=97.0))
    lock = usage_base._snapshot_lock_path(target)
    contended_during_write: list[bool] = []
    real_write = usage_base.write_usage_snapshot

    def observing_write(path, snapshot):
        # Someone else must be unable to take the lock while we are inside.
        contended_during_write.append(usage_base._claim_snapshot_lock(lock, wait_seconds=0.05) is None)
        real_write(path, snapshot)

    monkeypatch.setattr(usage_base, "write_usage_snapshot", observing_write)

    assert write_usage_snapshot_if_newer(target, _snapshot(NOW, seven_day_used=0.0)) is True

    assert contended_during_write == [True]
    assert _read(target)["seven_day_used_percentage"] == 0.0
    # Released once the write is durable: the next claim succeeds at once.
    descriptor = usage_base._claim_snapshot_lock(lock, wait_seconds=0.05)
    assert descriptor is not None
    usage_base._release_snapshot_lock(descriptor)


def test_guard_waits_for_a_lock_another_writer_still_holds(tmp_path, monkeypatch):
    target = tmp_path / "usage-codex.json"
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(target,), kwargs={"until": release, "held": held})
    holder.start()
    contended, completed = threading.Event(), threading.Event()
    results, errors = [], []
    native_try_lock = usage_base._try_lock

    def try_lock(descriptor):
        acquired = native_try_lock(descriptor)
        if not acquired:
            contended.set()
        return acquired

    def publish():
        try:
            results.append(write_usage_snapshot_if_newer(target, _snapshot(NOW, seven_day_used=1.0)))
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    monkeypatch.setattr(usage_base, '_try_lock', try_lock)
    # This case checks successful waiting; the next case keeps the timeout contract.
    monkeypatch.setattr(usage_base, 'SNAPSHOT_LOCK_WAIT_SECONDS', 2 * LOAD_HEADROOM_S)
    writer = threading.Thread(target=publish)
    try:
        assert held.wait(timeout=LOAD_HEADROOM_S)
        writer.start()
        assert contended.wait(timeout=LOAD_HEADROOM_S)
        assert not completed.is_set(), 'the writer bypassed the held lock'
    finally:
        release.set()
        if writer.ident is not None:
            writer.join(timeout=LOAD_HEADROOM_S)
        holder.join(timeout=LOAD_HEADROOM_S)

    assert not writer.is_alive() and not holder.is_alive()
    assert completed.is_set() and results == [True] and not errors
    assert _read(target)["seven_day_used_percentage"] == 1.0


def test_a_lock_file_nobody_holds_is_not_a_lock(tmp_path):
    """A holder that died released its lock with its process: no takeover needed."""

    target = tmp_path / "usage-codex.json"
    lock = usage_base._snapshot_lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("left behind by a process that is gone", encoding="utf-8")
    long_ago = time.time() - 3600
    os.utime(lock, (long_ago, long_ago))

    started = time.monotonic()
    assert write_usage_snapshot_if_newer(target, _snapshot(NOW, seven_day_used=2.0)) is True
    elapsed = time.monotonic() - started

    assert elapsed < usage_base.SNAPSHOT_LOCK_WAIT_SECONDS / 2, "an unheld lock file costs no wait"
    assert _read(target)["seven_day_used_percentage"] == 2.0
    assert lock.exists(), "the sibling is persistent; it is never unlinked"


def test_guard_preserves_the_prior_reading_when_the_lock_times_out(tmp_path, monkeypatch):
    target = tmp_path / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(FOUR_DAYS_AGO, seven_day_used=97.0))
    before = target.read_bytes()
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(target,), kwargs={"until": release, "held": held})
    holder.start()
    monkeypatch.setattr(usage_base, "SNAPSHOT_LOCK_WAIT_SECONDS", 0.05)
    try:
        assert held.wait(timeout=LOAD_HEADROOM_S)
        assert write_usage_snapshot_if_newer(target, _snapshot(NOW, seven_day_used=3.0)) is False
    finally:
        release.set()
        holder.join(timeout=LOAD_HEADROOM_S)

    assert not holder.is_alive()
    assert target.read_bytes() == before
    assert _read(target)["seven_day_used_percentage"] == 97.0


def test_concurrent_writers_leave_the_newest_observation_on_disk(tmp_path, monkeypatch):
    """Every writer reads the same stale baseline unless the guard serialises them.

    The read-to-write window is widened inside the critical section, so an
    implementation that does not hold the lock across the compare *and* the
    write interleaves: the last thread to finish wins whatever it read, and
    the reading it read was the baseline.
    """

    target = tmp_path / "usage-codex.json"
    write_usage_snapshot(target, _snapshot(FOUR_DAYS_AGO, seven_day_used=99.0))
    real_read = usage_base.read_usage_snapshot

    def slow_read(path):
        snapshot = real_read(path)
        time.sleep(0.01)
        return snapshot

    monkeypatch.setattr(usage_base, "read_usage_snapshot", slow_read)

    # Scheduling and lock contention need slack on the shared, self-hosted
    # Windows runner. Keep the widened critical section and newest-wins oracle.
    monkeypatch.setattr(usage_base, "SNAPSHOT_LOCK_WAIT_SECONDS", 20.0)
    readings = [(NOW - timedelta(minutes=i), float(i)) for i in range(12)]
    barrier = threading.Barrier(len(readings))
    failures: list[BaseException] = []

    def publish(observed, used):
        try:
            barrier.wait(timeout=20)
            write_usage_snapshot_if_newer(target, _snapshot(observed, seven_day_used=used))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    threads = [threading.Thread(target=publish, args=reading) for reading in reversed(readings)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert _read(target)["observed_at"] == NOW.isoformat()
    assert _read(target)["seven_day_used_percentage"] == 0.0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["usage-codex.json", "usage-codex.json.lock"]
