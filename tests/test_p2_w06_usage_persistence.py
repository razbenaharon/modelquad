# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""W06 contracts: synthetic collectors and temporary state, never real profiles.

Hook ids exercise the operation W02 will call, not cli.main integration.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Thread
import json
import subprocess
import time

import pytest

from modelquad.models import Freshness, ProviderStatus
from modelquad.service import health
from modelquad.usage import base, refresh
from modelquad.usage.claude import retain_last_known_claude


# Coordination needs slack on the shared, memory-constrained runner.
LOAD_HEADROOM_S = 60


NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=20)
LATER = NOW + timedelta(minutes=1)


def _reading(provider="codex", observed=OLD, remaining=80, **extra):
    return {
        "provider": provider, "available": True, "observed_at": observed.isoformat(),
        "collected_at": observed.isoformat(), "refresh_kind": refresh.ACTIVE,
        "effective_remaining_percentage": remaining,
        "quota": {"five_hour": {"remaining_percentage": remaining}},
        "five_hour_used_percentage": 100 - remaining, **extra,
    }


def _disk(target):
    return dict(base.read_usage_snapshot(target) or {})


@pytest.fixture
def background(monkeypatch):
    """Every stalled collector is released and joined, including on assertion failure."""
    threads, releases = [], []

    def tracked_thread(*args, **kwargs):
        thread = Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(refresh, "Thread", tracked_thread)
    yield threads, releases
    for release in releases:
        release.set()
    for thread in threads:
        thread.join(timeout=LOAD_HEADROOM_S)
        assert not thread.is_alive(), "collector leaked its refresh claim"


@pytest.mark.parametrize("writer", ["refresh", "claude_hook"], ids=["refresh", "claude_hook"])
def test_all_writer_races_keep_newest_quota(tmp_path, background, writer):
    provider = {"refresh": "codex", "claude_hook": "claude"}[writer]
    target = tmp_path / refresh.SNAPSHOT_FILENAMES[provider]
    base.write_usage_snapshot(target, _reading(provider))
    barrier, release = Barrier(2), Event()
    background[1].append(release)
    clock = [NOW]

    def blocked():
        barrier.wait(timeout=LOAD_HEADROOM_S)
        assert release.wait()

    def handler(directory, *, now):
        blocked()
        return _reading(provider, observed=OLD, remaining=80)

    def write():
        if writer == "refresh":
            return refresh.refresh_provider(
                provider, tmp_path, now=lambda: clock[0], force=True,
                refreshers={provider: handler},
            )
        # W02 captures the hook start before parsing and passes the existing
        # retention callback unchanged into this operation.
        raw = {"observed_at": None, "collected_at": NOW.isoformat(),
               "probe_started_at": NOW.isoformat()}
        blocked()
        return base.publish_usage_snapshot(target, raw, now=lambda: clock[0], merge=retain_last_known_claude)

    future = refresh.daemon_future(write)
    barrier.wait(timeout=LOAD_HEADROOM_S)
    clock[0] = LATER
    assert base.publish_usage_snapshot(
        target, _reading(provider, observed=LATER, remaining=0,
                         probe_started_at=LATER.isoformat()), now=lambda: LATER,
    )
    expected = target.read_bytes()
    release.set()
    future.result(timeout=LOAD_HEADROOM_S)
    assert target.read_bytes() == expected
    assert _disk(target)["effective_remaining_percentage"] == 0


def test_retention_callback_reads_under_the_shared_lock(tmp_path):
    merge = retain_last_known_claude
    target = tmp_path / "usage-synthetic.json"
    base.write_usage_snapshot(target, _reading())
    seen = []

    def checked_merge(snapshot, path):
        descriptor = base._claim_snapshot_lock(base._snapshot_lock_path(path), wait_seconds=0)
        if descriptor is not None:
            base._release_snapshot_lock(descriptor)
            pytest.fail("merge ran outside the publication lock")
        seen.append(path)
        return merge(snapshot, path)

    assert base.publish_usage_snapshot(
        target, {"observed_at": None, "probe_started_at": NOW.isoformat()},
        now=lambda: NOW, merge=checked_merge,
    )
    assert seen == [target]
    assert _disk(target)["observed_at"] == OLD.isoformat()
    assert _disk(target)["stale"] is True


def test_codex_unobserved_probe_cannot_reenable_signed_out_provider(tmp_path, monkeypatch):
    from modelquad.usage import codex_appserver

    target = tmp_path / "usage-codex.json"
    base.write_usage_snapshot(target, _reading("codex", available=False, reason="signed out"))
    monkeypatch.setattr(codex_appserver, "collect_codex_appserver_usage", lambda **k: {
        "available": True, "observed_at": None, "reason": "native probe timed out",
    })
    result = refresh.refresh_provider(
        "codex", tmp_path, now=lambda: NOW, force=True,
        refreshers={"codex": refresh._refresh_codex},
    )
    assert result.available is False
    assert _disk(target)["available"] is False
    assert _disk(target)["reason"] == "signed out"
    # Quota stays the last real reading; nothing newer was observed.
    assert _disk(target)["observed_at"] == OLD.isoformat()


@pytest.mark.parametrize("failure", ["timeout", "lock_file_oserror", "replace_oserror"],
                         ids=["timeout", "lock_file_oserror", "replace_oserror"])
def test_lock_timeout_never_writes_unlocked(tmp_path, monkeypatch, failure):
    target = tmp_path / "usage-codex.json"
    base.write_usage_snapshot(target, _reading("codex"))
    before = target.read_bytes()
    descriptor = None
    if failure == "timeout":
        descriptor = base._claim_snapshot_lock(base._snapshot_lock_path(target), wait_seconds=0)
        assert descriptor is not None
        monkeypatch.setattr(base, "SNAPSHOT_LOCK_WAIT_SECONDS", 0)
    elif failure == "lock_file_oserror":
        original = base.os.open
        def refused(path, *args, **kwargs):
            if Path(path) == base._snapshot_lock_path(target):
                raise OSError("synthetic lock-file denial")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(base.os, "open", refused)
    else:
        def refused_replace(*args, **kwargs):
            raise OSError("synthetic atomic replacement failure")
        monkeypatch.setattr(Path, "replace", refused_replace)
    try:
        if failure != "replace_oserror":
            assert base.write_usage_snapshot_if_newer(target, _reading("codex", observed=NOW), now=lambda: NOW) is False
        result = refresh.refresh_provider(
            "codex", tmp_path, now=lambda: NOW, force=True,
            refreshers={"codex": lambda *a, **k: _reading("codex", observed=NOW)},
        )
        assert result.published is False
        assert "not persisted" in result.error
        assert result.newer_observation is False
        assert target.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        if descriptor is not None:
            base._release_snapshot_lock(descriptor)


def test_future_snapshot_accepts_corrected_reading(tmp_path, monkeypatch):
    target = tmp_path / "usage-codex.json"
    future_time = NOW + timedelta(days=1)
    poisoned = _reading("codex", observed=future_time,
                        probe_started_at=future_time.isoformat(), availability_checked_at=future_time.isoformat())
    base.write_usage_snapshot(target, poisoned)
    assert base.write_usage_snapshot_if_newer(
        target, {"observed_at": None}, now=lambda: NOW,
    ) is False
    assert _disk(target)["observed_at"] == future_time.isoformat()
    before = target.read_bytes()
    assert base.publish_usage_snapshot(target, poisoned, now=lambda: NOW) is False
    assert target.read_bytes() == before

    refresh.refresh_provider(
        "codex", tmp_path, now=lambda: NOW, force=True,
        refreshers={"codex": lambda *a, **k: _reading("codex", observed=NOW, remaining=100)},
    )
    assert _disk(target)["observed_at"] == NOW.isoformat()
    result = refresh.RefreshResult("codex", refresh.ACTIVE, True, NOW.isoformat(),
                                   observed_at=NOW.isoformat(), previous_observed_at=future_time.isoformat())
    assert result.newer_observation is True
    assert replace(result, observed_at=future_time.isoformat()).newer_observation is False
    assert replace(result, observed_at=future_time.isoformat()).freshness == "unknown"


@pytest.mark.parametrize("offset,accepted", [(119, True), (120, True), (121, False)],
                         ids=["within_tolerance", "at_tolerance", "beyond_tolerance"])
def test_future_clock_tolerance_is_shared_by_writes_and_results(tmp_path, monkeypatch, offset, accepted):
    target = tmp_path / "usage-codex.json"
    base.write_usage_snapshot(target, _reading("codex"))
    stamp = NOW + timedelta(seconds=offset)
    assert base.FUTURE_TIMESTAMP_TOLERANCE_SECONDS == 120
    assert (base.valid_timestamp_epoch(stamp.isoformat(), now=NOW) is not None) is accepted
    before = target.read_bytes()
    assert base.write_usage_snapshot_if_newer(
        target, _reading("codex", observed=stamp), now=lambda: NOW,
    ) is accepted
    if not accepted:
        assert target.read_bytes() == before
        # A disk timestamp just outside the tolerance cannot block correction.
        base.write_usage_snapshot(target, _reading("codex", observed=stamp))
        assert base.write_usage_snapshot_if_newer(target, _reading("codex", observed=NOW), now=lambda: NOW)
        assert _disk(target)["observed_at"] == NOW.isoformat()
    result = refresh.RefreshResult("codex", refresh.ACTIVE, True, NOW.isoformat(),
                                   observed_at=stamp.isoformat(), previous_observed_at=OLD.isoformat(),
                                   remaining_percentage=80)
    assert result.newer_observation is accepted
    assert result.freshness == ("fresh" if accepted else "unknown")


def test_refresh_has_one_deadline_and_no_probe_pileup(tmp_path, monkeypatch, background):
    barrier, release = Barrier(3), Event()
    background[1].append(release)
    clock, calls, budgets = [0.0], [], []

    def stalled(directory, *, now):
        calls.append(1)
        barrier.wait(timeout=LOAD_HEADROOM_S)
        assert release.wait()
        return _reading(observed=NOW)

    def expire(futures, *, timeout):
        budgets.append(timeout)
        barrier.wait(timeout=LOAD_HEADROOM_S)
        clock[0] = 10.0
        return set(), set(futures)

    monkeypatch.setattr(refresh, "wait", expire)
    results = refresh.refresh_all(
        tmp_path, providers=["test_refresh", "codex"], now=lambda: NOW,
        refreshers={"test_refresh": stalled, "codex": stalled}, force=True,
        per_provider_timeout=20, total_timeout=10, monotonic=lambda: clock[0],
    )
    assert budgets == [10.0]
    assert len(calls) == 2
    assert all(result.published is False for result in results.values())
    assert all(thread.daemon for thread in background[0])
    for name in ("test_refresh", "codex"):
        again = refresh.refresh_provider(
            name, tmp_path, now=lambda: NOW, force=True,
            refreshers={name: lambda *a, **k: pytest.fail("duplicate probe")},
        )
        assert again.debounced
    release.set()
    for thread in background[0]:
        thread.join(timeout=LOAD_HEADROOM_S)
        assert not thread.is_alive()
    assert not list(tmp_path.glob("usage-*.json")), "expired probes must never publish"


@pytest.mark.parametrize("ceilings,expected", [
    ({}, 60.0),
    ({"total_timeout": 15.0}, 15.0),
    ({"per_provider_timeout": 20.0}, 20.0),
], ids=["default_budget_60_seconds", "shorter_sweep", "shorter_provider"])
def test_refresh_budget_uses_the_smaller_ceiling(tmp_path, monkeypatch, background, ceilings, expected):
    budgets = []

    def completed(futures, *, timeout):
        budgets.append(timeout)
        for future in futures:
            future.result(timeout=5)
        return set(futures), set()

    monkeypatch.setattr(refresh, "wait", completed)
    result = refresh.refresh_all(
        tmp_path, providers=["codex"], now=lambda: NOW, force=True,
        refreshers={"codex": lambda *a, **k: _reading("codex", observed=NOW)},
        monotonic=lambda: 100.0, **ceilings,
    )
    assert budgets == [expected]
    assert result["codex"].published is True


@pytest.mark.parametrize("late_available", [True, False], ids=["dated_quota", "undated_signed_out"])
def test_late_thread_result_does_not_overwrite_newer_reading(tmp_path, monkeypatch, background, late_available):
    target = tmp_path / "usage-codex.json"
    barrier, release = Barrier(2), Event()
    background[1].append(release)
    clock = [NOW]

    def late(directory, *, now):
        barrier.wait(timeout=LOAD_HEADROOM_S)
        assert release.wait()
        if late_available:
            return _reading("codex", observed=clock[0], remaining=80)
        return {"available": False, "observed_at": None, "reason": "not authenticated"}

    def expire(futures, *, timeout):
        barrier.wait(timeout=LOAD_HEADROOM_S)
        return set(), set(futures)

    monkeypatch.setattr(refresh, "wait", expire)
    # Freeze monotonic so the persisted start fence alone must reject the late
    # result, even when it is stamped at completion. No in-process generation.
    results = refresh.refresh_all(
        tmp_path, providers=["codex"], now=lambda: clock[0], force=True,
        refreshers={"codex": late}, monotonic=lambda: 0.0,
    )
    assert results["codex"].published is False
    clock[0] = LATER
    assert base.publish_usage_snapshot(
        target, _reading("codex", observed=LATER, remaining=0, probe_started_at=LATER.isoformat()),
        now=lambda: LATER,
    )
    expected = target.read_bytes()
    release.set()
    for thread in background[0]:
        thread.join(timeout=LOAD_HEADROOM_S)
        assert not thread.is_alive()
    assert target.read_bytes() == expected
    assert _disk(target)["available"] is True


@pytest.mark.parametrize("stamp", [None, "", "malformed"], ids=["null", "empty", "malformed"])
def test_explicit_invalid_observation_never_uses_collection_time(tmp_path, stamp):
    target = tmp_path / "usage-claude.json"
    raw = _reading("claude", observed=NOW)
    raw["observed_at"] = stamp
    base.write_usage_snapshot(target, raw)
    reading = refresh._persisted_claude_reading(tmp_path, now=lambda: NOW)
    assert reading["observed_at"] == stamp
    legacy = dict(raw)
    legacy.pop("observed_at")
    base.write_usage_snapshot(target, legacy)
    assert refresh._persisted_claude_reading(tmp_path, now=lambda: NOW)["observed_at"] == NOW.isoformat()


@pytest.mark.parametrize("stamp", [None, "", "malformed"], ids=["null", "empty", "malformed"])
def test_publisher_preserves_invalid_observation_with_current_reader_fallback(tmp_path, stamp):
    from modelquad.providers.base import observation_from

    target = tmp_path / "usage-codex.json"
    prior = _reading("codex")
    prior["observed_at"] = stamp
    base.write_usage_snapshot(target, prior)
    assert base.publish_usage_snapshot(
        target, {"available": False, "observed_at": None,
                 "probe_started_at": NOW.isoformat(), "reason": "signed out"},
        now=lambda: NOW,
    )
    stored = _disk(target)
    assert stored["observed_at"] == stamp
    assert stored["collected_at"] == NOW.isoformat()
    assert stored["effective_remaining_percentage"] == 80
    # W07 (U7): a present but invalid observation stays unknown; only an
    # absent field falls back to collection time.
    assert observation_from(stored) is None
