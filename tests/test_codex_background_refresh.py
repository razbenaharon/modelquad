# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The background Codex quota refresh: debounced, non-blocking, display only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import functools
import json
from pathlib import Path
import threading
import time

import pytest

from modelquad.usage import background, refresh
from modelquad.usage.codex_appserver import _unobserved, parse_rate_limits_result


def _rate_limits(used_five=30.0, used_week=55.0):
    """A real ``account/rateLimits/read`` result shape, normalized by the parser."""
    resets = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    return parse_rate_limits_result({"rateLimits": {
        "primary": {"usedPercent": used_five, "windowDurationMins": 300, "resetsAt": resets},
        "secondary": {"usedPercent": used_week, "windowDurationMins": 10080, "resetsAt": resets + 86400},
    }})


@pytest.fixture(autouse=True)
def notice_already_shown(monkeypatch):
    """These tests are about the loop; the notice gate has its own test below."""
    monkeypatch.setattr(background, "notice_shown", lambda: True)


def test_no_probe_runs_before_the_data_flow_notice(tmp_path, monkeypatch):
    # A plugin install can start the server before anything showed the notice
    # that describes this probe, so the loop waits for it.
    shown = []
    monkeypatch.setattr(background, "notice_shown", lambda: bool(shown))
    calls = []
    stop = threading.Event()
    rounds = iter(range(3))

    def record(provider, directory):
        calls.append(provider)
        stop.set()

    original_wait = stop.wait

    def wait(interval):
        if next(rounds, None) == 1:
            shown.append(True)
        return original_wait(0)

    stop.wait = wait
    background.run_codex_refresh_loop(tmp_path, stop, interval=0.01, refresh=record)
    assert calls == ["codex"], "skipped until the notice was shown, then refreshed"


def _one_pass(refresh_function):
    """Run exactly one loop iteration and return."""
    stop = threading.Event()

    def once(*args, **kwargs):
        try:
            return refresh_function(*args, **kwargs)
        finally:
            stop.set()

    return stop, once


def test_start_returns_at_once_on_a_daemon_thread_even_while_the_probe_hangs(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def hanging_probe(provider, directory):
        entered.set()
        release.wait(10)  # the app-server probe can take up to 45 s

    started = time.monotonic()
    thread, stop = background.start_codex_refresh(tmp_path, interval=60, refresh=hanging_probe)
    returned_after = time.monotonic() - started
    try:
        assert returned_after < 1.0
        assert thread.daemon is True
        assert thread.name == background.THREAD_NAME
        assert entered.wait(5), "the first refresh runs immediately"
        assert thread.is_alive()
    finally:
        stop.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_the_loop_only_ever_asks_codex_for_its_quota(tmp_path):
    calls = []
    stop = threading.Event()

    def record(provider, directory):
        calls.append((provider, directory))
        if len(calls) == 3:
            stop.set()

    background.run_codex_refresh_loop(tmp_path, stop, interval=0.01, refresh=record)
    assert calls == [("codex", Path(tmp_path))] * 3


def test_a_failing_probe_never_stops_the_loop_or_raises(tmp_path):
    calls = []
    stop = threading.Event()

    def failing(provider, directory):
        calls.append(provider)
        if len(calls) == 2:
            stop.set()
        raise OSError("app-server unreachable")

    background.run_codex_refresh_loop(tmp_path, stop, interval=0.01, refresh=failing)
    assert calls == ["codex", "codex"]


def test_refreshes_are_debounced_across_sessions(tmp_path):
    """Two sessions starting together probe Codex once, not twice."""
    probes = []

    def app_server(directory, *, now=None):
        probes.append(directory)
        return _rate_limits()

    shared = functools.partial(refresh.refresh_provider, refreshers={"codex": app_server})
    results = []
    for _session in range(2):
        stop, once = _one_pass(lambda provider, directory: results.append(shared(provider, directory)))
        background.run_codex_refresh_loop(tmp_path, stop, interval=60, refresh=once)

    assert len(probes) == 1
    assert results[0].refresh_kind == refresh.ACTIVE and results[0].published
    assert results[1].debounced is True and results[1].refresh_kind == refresh.PASSIVE
    assert refresh.MIN_REFRESH_INTERVAL_SECONDS > 0


def test_the_refresh_is_display_only(tmp_path, monkeypatch):
    """It writes the Codex usage snapshot and nothing else, and starts no work."""
    from modelquad.execution.manager import ExecutionManager
    from modelquad.jobs import handle

    def tripwire(*args, **kwargs):
        raise AssertionError("a background quota refresh started work")

    monkeypatch.setattr(ExecutionManager, "delegate", tripwire)
    monkeypatch.setattr(handle, "start_job", tripwire)
    monkeypatch.setattr(handle, "launch_reserved", tripwire)

    real = functools.partial(refresh.refresh_provider,
                             refreshers={"codex": lambda directory, *, now=None: _rate_limits()})
    stop, once = _one_pass(real)
    background.run_codex_refresh_loop(tmp_path, stop, interval=60, refresh=once)

    written = sorted(p.name for p in tmp_path.iterdir() if p.is_file() and not p.name.endswith(".lock"))
    assert written == ["usage-codex.json"]


def test_an_unreachable_app_server_shows_unknown_never_zero(tmp_path):
    from modelquad.cli import _status_line_text

    unreachable = functools.partial(refresh.refresh_provider, refreshers={
        "codex": lambda directory, *, now=None: _unobserved(reason="app_server_unreachable",
                                                            available=True, now=now)})
    stop, once = _one_pass(unreachable)
    background.run_codex_refresh_loop(tmp_path, stop, interval=60, refresh=once)

    line = _status_line_text(tmp_path, {})
    assert "Codex 5h unknown / 7d unknown" in line
    assert "0%" not in line.split("|", 1)[1]


def test_a_fresh_reading_reaches_the_status_line(tmp_path):
    from modelquad.cli import _status_line_text

    real = functools.partial(refresh.refresh_provider,
                             refreshers={"codex": lambda directory, *, now=None: _rate_limits(30, 55)})
    stop, once = _one_pass(real)
    background.run_codex_refresh_loop(tmp_path, stop, interval=60, refresh=once)

    line = _status_line_text(tmp_path, {})
    assert "Codex 5h 70% left, resets " in line and "/ 7d 45% left" in line


@pytest.mark.parametrize("worker", [False, True], ids=["host", "worker"])
def test_the_mcp_server_starts_the_refresh_only_outside_a_worker(monkeypatch, worker):
    from modelquad.mcp import server

    started = []
    monkeypatch.setattr(server, "is_worker", lambda db_path: worker)
    monkeypatch.setattr(background, "start_codex_refresh", lambda directory: started.append(directory))
    monkeypatch.setattr(server, "USAGE_DIRECTORY", "synthetic-usage")
    server._start_background_quota_refresh()
    assert started == ([] if worker else ["synthetic-usage"])


def test_a_failing_start_never_stops_the_server(monkeypatch):
    from modelquad.mcp import server

    def explode(directory):
        raise RuntimeError("thread could not start")

    monkeypatch.setattr(server, "is_worker", lambda db_path: False)
    monkeypatch.setattr(background, "start_codex_refresh", explode)
    server._start_background_quota_refresh()  # logs, does not raise


def _write_codex_reading(directory, **fields):
    (Path(directory) / "usage-codex.json").write_text(json.dumps(fields), encoding="utf-8")


def test_the_loop_wakes_just_after_a_window_resets(tmp_path):
    # A reading taken 51 s before its 5-hour window reset (live, 2026-11-09)
    # left that window unknown for a whole cycle; the loop now wakes after it.
    wall = 1_000_000.0
    _write_codex_reading(tmp_path, five_hour_resets_at=wall + 51, seven_day_resets_at=wall + 86400)
    wait = background.next_wait(tmp_path, 240.0, wall=wall)
    assert wait == pytest.approx(51 + background.RESET_GRACE_SECONDS)


def test_the_loop_keeps_its_interval_when_no_reset_is_near(tmp_path):
    wall = 1_000_000.0
    _write_codex_reading(tmp_path, five_hour_resets_at=wall + 3600, seven_day_resets_at=wall + 86400)
    assert background.next_wait(tmp_path, 240.0, wall=wall) == 240.0
    # A reset already past never makes the loop spin.
    _write_codex_reading(tmp_path, five_hour_resets_at=wall - 10)
    assert background.next_wait(tmp_path, 240.0, wall=wall) == 240.0


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_no_readable_reading_keeps_the_interval(tmp_path, content):
    if content is not None:
        (tmp_path / "usage-codex.json").write_text(content, encoding="utf-8")
    assert background.next_wait(tmp_path, 240.0, wall=1_000_000.0) == 240.0
