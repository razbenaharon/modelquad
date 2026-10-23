# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Periodic idle expiry and runner-loss recovery, independent of usage polls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading

from ..jobs.recovery import recover_jobs
from .collector import _DaemonCall

logger = logging.getLogger(__name__)
_maintenance_flight = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobMaintenance:
    """Wait a full interval before each single-flight daemon recovery pass."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        interval_seconds: float = 600.0,
        clock: Callable[[], datetime] = _utc_now,
        pass_function: Callable[..., object] | None = None,
        stop_timeout: float = 1.0,
        wait_function: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("maintenance interval must be positive")
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.pass_function = pass_function or recover_jobs
        self.wait_function = wait_function or asyncio.sleep
        self._worker = _DaemonCall("Job maintenance pass", _maintenance_flight, stop_timeout)
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> bool:
        """Await one pass; never queue a pass behind a running or stopped one."""
        return await self._worker.run_once(
            lambda: self.pass_function(self.db_path, now=self.clock())
        )

    async def _run(self) -> None:
        while True:
            # Startup already schedules recovery. No immediate maintenance.
            await self.wait_function(self.interval_seconds)
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - retry on the next interval
                logger.warning("Job maintenance scheduling failed", exc_info=True)

    def start(self) -> None:
        if self._task is None:
            self._worker.stopped = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._worker.stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._worker.stop()
