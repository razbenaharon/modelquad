# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic W01 responsiveness, retention and migration interleavings."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import httpx
import pytest

from orchestrator.config import Config
from orchestrator.execution.base import WorkerTaskStatus
from orchestrator.jobs import handle, recovery
from orchestrator.jobs.seam import JobOutput, JobRef, JobStatus
from orchestrator.jobs.store import FileOutputStore
from orchestrator.service import collector, db
from orchestrator.service.app import create_app
from orchestrator.service.job_maintenance import JobMaintenance

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
JOB_ID = "00000000-0000-4000-8000-000000000101"
DECISION_ID = "00000000000000000000000000000101"
EVENT_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def synthetic_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(recovery, "schedule_recovery", lambda *a, **k: False)
    monkeypatch.setattr(handle, "detached_launch_verdict", lambda: (True, "synthetic"))

    def unexpected(*args, **kwargs):
        raise AssertionError("real provider/doctor calls are forbidden")

    monkeypatch.setattr(collector, "build_providers", unexpected)
    monkeypatch.setattr(collector, "run_doctor", unexpected)


def _run(coroutine):
    async def bounded():
        return await asyncio.wait_for(coroutine, timeout=15.0)

    return asyncio.run(bounded())


def _answer(sink):
    output = JobOutput(
        JobRef(JOB_ID, 101, DECISION_ID), JobStatus.FINISHED,
        WorkerTaskStatus.COMPLETED, None, "synthetic answer", False,
        (), None, None, NOW, NOW,
    )
    assert sink.put(output, NOW)


@pytest.mark.parametrize("stage", ["provider", "doctor"], ids=["provider", "doctor"])
def test_poll_keeps_health_responsive(tmp_path, stage):
    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        observed = []

        def poll(*args):
            try:
                for current in ("provider", "history", "doctor"):
                    observed.append(current)
                    if current == stage:
                        loop.call_soon_threadsafe(entered.set)
                        assert release.wait(EVENT_TIMEOUT)
            finally:
                finished.set()

        app = create_app(
            db_path=tmp_path / "health.sqlite", usage_directory=tmp_path / "usage",
            poll_function=poll, poll_interval=600.0,
            maintenance_pass=lambda *a, **k: None, write_secret="maintenance-secret",
        )
        try:
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(entered.wait(), EVENT_TIMEOUT)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
                ) as client:
                    response = await asyncio.wait_for(
                        client.get("/api/health", headers={"X-ModelQuad-Token": "maintenance-secret"}),
                        EVENT_TIMEOUT)
                assert response.status_code == 200
                assert response.json()["status"] == "ok"
                assert not release.is_set()
                assert not finished.is_set()  # A timed-out poll cannot fake responsiveness.
                release.set()
        finally:
            release.set()
        assert stage in observed

    _run(scenario())


def test_poll_is_single_flight(tmp_path, caplog):
    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        calls = []

        def poll(*args):
            calls.append("first")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(EVENT_TIMEOUT)

        scheduler = collector.Scheduler(
            Config(), db_path=tmp_path / "poll.sqlite", usage_directory=tmp_path / "usage",
            poll_function=poll, stop_timeout=0.0,
        )
        replacement = collector.Scheduler(
            Config(), db_path=tmp_path / "poll.sqlite", usage_directory=tmp_path / "usage",
            poll_function=lambda *a: calls.append("replacement"),
        )
        waiter = asyncio.create_task(scheduler.run_once())
        try:
            await asyncio.wait_for(entered.wait(), EVENT_TIMEOUT)
            assert not await scheduler.run_once()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await asyncio.wait_for(scheduler.stop(), EVENT_TIMEOUT)
            assert "poll may still be finishing" in caplog.text
            assert not await replacement.run_once()
            assert calls == ["first"]
        finally:
            release.set()
            # The worker's completion future survives cancellation of waiter.
            await asyncio.wait_for(asyncio.shield(scheduler._worker._future), EVENT_TIMEOUT)
            if not waiter.done():
                await asyncio.wait_for(waiter, EVENT_TIMEOUT)
        assert await replacement.run_once()
        await replacement.stop()
        assert calls == ["first", "replacement"]

    _run(scenario())


def test_idle_service_expires_output(tmp_path, monkeypatch):
    sink = FileOutputStore(tmp_path / "answers")
    _answer(sink)
    content = tuple(sink.root.glob("*.json"))
    assert len(content) == 1
    monkeypatch.setattr(recovery, "output_sink_factory", lambda mode: sink)
    clock = [NOW]

    async def scenario():
        app = create_app(
            db_path=tmp_path / "idle.sqlite", usage_directory=tmp_path / "usage",
            poll_providers=False, maintenance_clock=lambda: clock[0],
        )
        async with app.router.lifespan_context(app):
            assert app.state.scheduler is None
            conn = db.connect(tmp_path / "idle.sqlite")
            try:
                assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            finally:
                conn.close()
            clock[0] = NOW + timedelta(days=7, seconds=1)
            assert content[0].exists()
            assert await app.state.job_maintenance.run_once()
            # Inspect the file BEFORE any store read can itself enforce expiry.
            assert not content[0].exists()
            assert sink.collect(JOB_ID, clock[0]) is None

    _run(scenario())


@pytest.mark.parametrize("polling", [False, True], ids=["no_polling", "polling"])
def test_maintenance_respects_extension_and_stops(tmp_path, monkeypatch, polling):
    sink = FileOutputStore(tmp_path / "answers")
    _answer(sink)
    assert sink.extend(JOB_ID, NOW + timedelta(days=14), NOW)
    monkeypatch.setattr(recovery, "output_sink_factory", lambda mode: sink)
    calls = []
    clock = [NOW]

    def maintenance_pass(path, *, now):
        calls.append(now)
        return recovery.recover_jobs(path, now=now)

    async def scenario():
        app = create_app(
            db_path=tmp_path / "extended.sqlite", usage_directory=tmp_path / "usage",
            poll_providers=polling, poll_function=lambda *a: None,
            maintenance_interval=600.0, maintenance_clock=lambda: clock[0],
            maintenance_pass=maintenance_pass,
        )
        async with app.router.lifespan_context(app):
            maintenance = app.state.job_maintenance
            assert calls == []  # No maintenance pass at startup.
            clock[0] = NOW + timedelta(days=8)
            assert await maintenance.run_once()
            assert sink.collect(JOB_ID, clock[0]).extended_until == NOW + timedelta(days=14)
        assert maintenance._task is None
        clock[0] = NOW + timedelta(days=15)
        assert not await maintenance.run_once()
        assert calls == [NOW + timedelta(days=8)]

    _run(scenario())


def test_maintenance_single_flight_and_bounded_shutdown(tmp_path, caplog):
    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        calls = []

        def maintenance_pass(path, *, now):
            calls.append(now)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(EVENT_TIMEOUT)

        maintenance = JobMaintenance(
            tmp_path / "maintenance.sqlite", clock=lambda: NOW,
            pass_function=maintenance_pass, stop_timeout=0.0,
        )
        waiter = asyncio.create_task(maintenance.run_once())
        try:
            await asyncio.wait_for(entered.wait(), EVENT_TIMEOUT)
            assert not await maintenance.run_once()
            await asyncio.wait_for(maintenance.stop(), EVENT_TIMEOUT)
            assert "pass may still be finishing" in caplog.text
            assert not await maintenance.run_once()
            assert calls == [NOW]
        finally:
            release.set()
            await asyncio.wait_for(waiter, EVENT_TIMEOUT)

    _run(scenario())


def test_maintenance_tick_is_cancelled_on_shutdown(tmp_path):
    async def scenario():
        loop = asyncio.get_running_loop()
        waiting = asyncio.Queue()
        ticks = asyncio.Queue()
        passed = asyncio.Event()
        calls = []

        async def tick(interval):
            waiting.put_nowait(interval)
            await ticks.get()

        def maintenance_pass(path, *, now):
            calls.append(now)
            loop.call_soon_threadsafe(passed.set)

        maintenance = JobMaintenance(
            tmp_path / "ticks.sqlite", clock=lambda: NOW,
            pass_function=maintenance_pass, wait_function=tick,
        )
        maintenance.start()
        try:
            assert await asyncio.wait_for(waiting.get(), EVENT_TIMEOUT) == 600.0
            assert calls == []
            ticks.put_nowait(None)
            await asyncio.wait_for(passed.wait(), EVENT_TIMEOUT)
            # Wait until the next interval is actually scheduled before stop.
            assert await asyncio.wait_for(waiting.get(), EVENT_TIMEOUT) == 600.0
            await maintenance.stop()
            ticks.put_nowait(None)
            assert maintenance._task is None
            assert not await maintenance.run_once()
            assert calls == [NOW]
        finally:
            await maintenance.stop()

    _run(scenario())


