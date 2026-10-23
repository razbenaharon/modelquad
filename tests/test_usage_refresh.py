# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from modelquad.usage.codex_appserver import (
    CODEX_APPSERVER_SOURCE,
    collect_codex_appserver_usage,
    parse_rate_limits_result,
)
from modelquad.usage import refresh as usage_refresh
from modelquad.usage.base import write_usage_snapshot
from modelquad.usage.refresh import (
    ACTIVE,
    PASSIVE,
    UNAVAILABLE,
    RefreshResult,
    refresh_all,
    refresh_provider,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=30)

# Captured live from `codex app-server` -> account/rateLimits/read.
REAL_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 96, "windowDurationMins": 10080, "resetsAt": 1787317030},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None,
        "spendControlReached": False,
        "planType": "plus",
        "rateLimitReachedType": None,
    },
    "rateLimitResetCredits": {"availableCount": 0, "credits": None},
}


def _observed(snapshot):
    return snapshot.get("observed_at")


# --------------------------------------------------------------- codex parse


def test_live_read_dates_the_observation_to_now():
    """A live backend read really is observed now, so both stamps coincide."""

    snapshot = parse_rate_limits_result(REAL_RESULT, now=lambda: NOW)

    assert snapshot["observed_at"] == NOW.isoformat()
    assert snapshot["collected_at"] == NOW.isoformat()
    assert snapshot["refresh_kind"] == ACTIVE
    assert snapshot["source"] == CODEX_APPSERVER_SOURCE


def test_a_weekly_window_is_never_reported_as_the_five_hour_one():
    """The passive collector reuses one window for both slots; this must not."""

    snapshot = parse_rate_limits_result(REAL_RESULT, now=lambda: NOW)

    assert snapshot["seven_day_used_percentage"] == 96.0
    assert snapshot["seven_day_remaining_percentage"] == 4.0
    assert snapshot["five_hour_used_percentage"] is None
    assert snapshot["five_hour_remaining_percentage"] is None
    # ...and the window is still preserved rather than dropped.
    assert [w["slot"] for w in snapshot["windows"]] == ["seven_day"]


def test_missing_cli_marks_codex_unavailable():
    result = collect_codex_appserver_usage(which=lambda _: None, now=lambda: NOW)

    assert result["available"] is False
    assert result["observed_at"] is None
    assert result["refresh_kind"] == UNAVAILABLE


def test_appserver_failure_keeps_codex_available_with_unknown_quota():
    class _Boom:
        stdin = stdout = stderr = None

        def __init__(self, *a, **k):
            raise OSError("cannot spawn")

    result = collect_codex_appserver_usage(
        which=lambda _: "C:/fake/codex.exe", popen=_Boom, now=lambda: NOW
    )

    assert result["available"] is True
    assert result["observed_at"] is None


def test_malformed_result_does_not_invent_quota():
    snapshot = parse_rate_limits_result({"rateLimits": "nonsense"}, now=lambda: NOW)

    assert snapshot["five_hour_used_percentage"] is None
    assert snapshot["seven_day_used_percentage"] is None
    assert snapshot["windows"] == []


# ------------------------------------------------------------- refresh layer


def _fake_active(observed, remaining=42.0, kind=ACTIVE):
    def handler(_directory, *, now=None):
        return {
            "provider": "p",
            "available": True,
            "source": "fake",
            "refresh_kind": kind,
            "observed_at": observed,
            "collected_at": observed,
            "effective_remaining_percentage": remaining,
        }

    return handler


def test_codex_reading_persists_after_overlapping_hook_write(tmp_path, monkeypatch):
    """Publish a hook snapshot exactly between refresh's close and replace."""
    target = tmp_path / "usage-codex.json"
    hook_reading = {
        "provider": "codex", "available": True, "source": "test-hook",
        "observed_at": NOW.isoformat(), "effective_remaining_percentage": 60.0,
    }
    reading = {
        "provider": "codex", "available": True, "source": "test-codex-cli",
        "refresh_kind": ACTIVE, "observed_at": LATER.isoformat(),
        "effective_remaining_percentage": 37.5,
    }
    real_replace = Path.replace
    published_hook = False
    calls = []

    def refresher(directory, *, now):
        calls.append(directory)
        assert now() == LATER
        return dict(reading)

    def replace_after_hook(temporary, destination):
        nonlocal published_hook
        if destination == target and not published_hook:
            published_hook = True
            write_usage_snapshot(target, hook_reading)
            assert json.loads(target.read_text(encoding="utf-8")) == hook_reading
        return real_replace(temporary, destination)

    monkeypatch.setattr(Path, "replace", replace_after_hook)
    result = refresh_provider(
        "codex", tmp_path, refreshers={"codex": refresher},
        now=lambda: LATER, force=True,
    )

    assert calls == [tmp_path]
    assert published_hook
    assert result.error is None
    assert result.available is True
    assert result.refresh_kind == ACTIVE
    assert result.observed_at == reading["observed_at"]
    assert result.remaining_percentage == reading["effective_remaining_percentage"]
    assert result.source == reading["source"]
    assert json.loads(target.read_text(encoding="utf-8")) == {
        **reading, "collected_at": LATER.isoformat(),
        "probe_started_at": LATER.isoformat(),
        "availability_checked_at": LATER.isoformat(),
    }
    # No staging file survives. Two persistent lock siblings legitimately stay
    # beside the snapshot: the monotonic write guard's and the refresh
    # debounce's. Both are held by the kernel and never unlinked.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        target.name, f"{target.name}.lock", "usage-refresh-codex.lock",
    ]


def test_collected_at_moving_alone_is_not_a_newer_observation(tmp_path):
    """The core semantic: re-checking is not the same as new provider data."""

    stamp = "2026-08-20T09:00:00+00:00"
    first = refresh_provider(
        "p", tmp_path, now=lambda: NOW, refreshers={"p": _fake_active(stamp)}, force=True
    )
    second = refresh_provider(
        "p", tmp_path, now=lambda: LATER, refreshers={"p": _fake_active(stamp)}, force=True
    )

    assert first.newer_observation is True
    assert second.newer_observation is False
    assert second.collected_at != first.collected_at
    assert second.observed_at == first.observed_at


def test_failed_refresh_preserves_last_known_good(tmp_path):
    good = refresh_provider(
        "p", tmp_path, now=lambda: NOW,
        refreshers={"p": _fake_active("2026-08-20T09:00:00+00:00", remaining=55.0)},
        force=True,
    )

    def failing(_directory, *, now=None):
        return {"provider": "p", "available": True, "observed_at": None, "reason": "probe failed"}

    after = refresh_provider(
        "p", tmp_path, now=lambda: LATER, refreshers={"p": failing}, force=True
    )

    assert after.refresh_kind == UNAVAILABLE
    assert after.observed_at == good.observed_at
    assert after.remaining_percentage == 55.0
    assert after.available is True
    on_disk = json.loads((tmp_path / "usage-p.json").read_text(encoding="utf-8"))
    assert on_disk["effective_remaining_percentage"] == 55.0


def test_refresh_never_fabricates_quota_when_nothing_is_known(tmp_path):
    def failing(_directory, *, now=None):
        return {"provider": "p", "available": True, "observed_at": None, "reason": "no data"}

    result = refresh_provider("p", tmp_path, refreshers={"p": failing}, force=True, now=lambda: NOW)

    assert result.remaining_percentage is None
    assert result.observed_at is None


def test_a_raising_refresher_is_contained(tmp_path):
    def boom(_directory, *, now=None):
        raise RuntimeError("provider exploded")

    result = refresh_provider("p", tmp_path, refreshers={"p": boom}, force=True, now=lambda: NOW)

    assert result.refresh_kind == UNAVAILABLE
    assert result.available is True
    assert "provider exploded" in (result.error or "")


def test_minimum_interval_debounces_a_second_probe(tmp_path):
    calls = []

    def counting(_directory, *, now=None):
        calls.append(1)
        return {
            "provider": "p", "available": True, "refresh_kind": ACTIVE,
            "observed_at": f"2026-08-20T09:00:0{len(calls)}+00:00",
            "collected_at": "x", "effective_remaining_percentage": 10.0,
        }

    refresh_provider("p", tmp_path, refreshers={"p": counting}, force=True, now=lambda: NOW)
    second = refresh_provider(
        "p", tmp_path, refreshers={"p": counting}, min_interval=3600, now=lambda: NOW
    )

    assert len(calls) == 1
    assert second.debounced is True


def _counting_refresher(calls: list[int]):
    def counting(_directory, *, now=None):
        calls.append(1)
        return {
            "provider": "p", "available": True, "refresh_kind": ACTIVE,
            "observed_at": "2026-08-20T09:00:00+00:00", "collected_at": "x",
            "effective_remaining_percentage": 10.0,
        }

    return counting


# The cross-process debounce is an OS advisory lock on a persistent sibling
# file. Only the kernel lock counts: the file's existence, its contents
# and its age say nothing, a dead holder's lock is released for it, and no
# refresher ever unlinks the sibling -- unlinking is how two contenders end up
# holding "the same" lock at two inodes, and how one removes another's.


def test_a_held_lock_makes_the_loser_serve_what_it_has(tmp_path):
    """Cross-process debounce: the loser reports, it never waits or fails."""

    calls: list[int] = []
    counting = _counting_refresher(calls)

    refresh_provider("p", tmp_path, refreshers={"p": counting}, force=True, now=lambda: NOW)
    # Simulate another live process mid-refresh: hold the lock the way it would.
    held = usage_refresh._claim(tmp_path, "p")
    assert held is not None
    try:
        started = time.monotonic()
        blocked = refresh_provider(
            "p", tmp_path, refreshers={"p": counting}, force=True, min_interval=0, now=lambda: NOW
        )
        elapsed = time.monotonic() - started
    finally:
        usage_refresh._release(held)

    assert len(calls) == 1
    assert blocked.debounced is True
    assert blocked.remaining_percentage == 10.0
    assert elapsed < 1.0, "the loser walks away; it must not queue behind the holder"


def test_the_loser_does_not_release_a_lock_it_never_held(tmp_path):
    """Release by pathname let a loser unlink the winner's live lock."""

    calls: list[int] = []
    counting = _counting_refresher(calls)

    held = usage_refresh._claim(tmp_path, "p")
    assert held is not None
    try:
        refresh_provider(
            "p", tmp_path, refreshers={"p": counting}, force=True, min_interval=0, now=lambda: NOW
        )
        # The holder is still the holder: a third contender still loses.
        intruder = usage_refresh._claim(tmp_path, "p")
        assert intruder is None, "the loser took the holder's lock away"
    finally:
        usage_refresh._release(held)

    assert calls == []


def test_a_lock_file_nobody_holds_is_not_a_lock(tmp_path):
    """A process killed mid-refresh must not block refreshes forever."""

    calls: list[int] = []
    counting = _counting_refresher(calls)

    lock = usage_refresh._lock_path(tmp_path, "p")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("left behind by a process that is gone", encoding="utf-8")
    long_ago = time.time() - 7200
    os.utime(lock, (long_ago, long_ago))

    result = refresh_provider(
        "p", tmp_path, refreshers={"p": counting}, force=True, min_interval=0, now=lambda: NOW
    )

    assert len(calls) == 1
    assert result.debounced is False
    assert lock.exists(), "the sibling is persistent; it is never unlinked"


def test_the_lock_is_given_back_when_the_refresh_finishes(tmp_path):
    """A finished refresh leaves the lock free for the next one, file and all."""

    calls: list[int] = []
    counting = _counting_refresher(calls)

    refresh_provider("p", tmp_path, refreshers={"p": counting}, force=True, now=lambda: NOW)

    lock = usage_refresh._lock_path(tmp_path, "p")
    assert lock.exists()
    descriptor = usage_refresh._claim(tmp_path, "p")
    assert descriptor is not None, "the refresh did not hand its lock back"
    usage_refresh._release(descriptor)


def test_a_failing_refresher_still_gives_the_lock_back(tmp_path):
    """The kernel would free it on death; a raised exception must too."""

    def boom(_directory, *, now=None):
        raise RuntimeError("down")

    refresh_provider("p", tmp_path, refreshers={"p": boom}, force=True, now=lambda: NOW)

    descriptor = usage_refresh._claim(tmp_path, "p")
    assert descriptor is not None, "a failed probe kept the debounce lock"
    usage_refresh._release(descriptor)


def test_one_providers_failure_does_not_stop_the_others(tmp_path):
    def boom(_directory, *, now=None):
        raise RuntimeError("down")

    results = refresh_all(
        tmp_path,
        force=True,
        now=lambda: NOW,
        refreshers={
            "good": _fake_active("2026-08-20T09:00:00+00:00", remaining=70.0),
            "bad": boom,
        },
    )

    assert set(results) == {"good", "bad"}
    assert results["good"].remaining_percentage == 70.0
    assert results["bad"].refresh_kind == UNAVAILABLE
    assert results["bad"].available is True


def test_unknown_provider_is_reported_not_guessed(tmp_path):
    result = refresh_provider("nope", tmp_path, refreshers={}, force=True, now=lambda: NOW)

    assert result.refresh_kind == UNAVAILABLE
    assert result.remaining_percentage is None
    assert "nope" in (result.error or "")


@pytest.mark.parametrize(
    "snapshot,expected",
    (
        ({"effective_remaining_percentage": 12.5}, 12.5),
        ({"five_hour_remaining_percentage": 30.0, "seven_day_remaining_percentage": 8.0}, 8.0),
        ({"five_hour_used_percentage": 37, "seven_day_used_percentage": 52}, 48.0),
        ({}, None),
    ),
)
def test_headline_remaining_reads_every_provider_shape(tmp_path, snapshot, expected):
    def handler(_directory, *, now=None):
        return {"provider": "p", "available": True, "observed_at": "2026-08-20T09:00:00+00:00", **snapshot}

    result = refresh_provider("p", tmp_path, refreshers={"p": handler}, force=True, now=lambda: NOW)

    assert result.remaining_percentage == expected


def test_result_serializes_the_freshness_distinction():
    result = RefreshResult(
        provider="p",
        refresh_kind=PASSIVE,
        available=True,
        collected_at="2026-08-20T12:00:00+00:00",
        observed_at="2026-08-18T09:00:00+00:00",
        previous_observed_at="2026-08-18T09:00:00+00:00",
    )

    payload = result.to_dict()
    assert payload["newer_observation"] is False
    assert payload["collected_at"] != payload["observed_at"]
