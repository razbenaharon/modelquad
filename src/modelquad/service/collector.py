# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Background periodic provider-usage collection for the local service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
import threading

from ..config import Config
from ..doctor import run_doctor
from ..providers import build_providers
from ..providers.base import read_status
from ..registry import ProviderRegistry, default_registry
from . import history

logger = logging.getLogger(__name__)
_poll_flight = threading.Lock()


def poll_once(
    config: Config,
    usage_directory: Path | None,
    db_path: str | Path,
    registry: ProviderRegistry | None = None,
) -> None:
    """Read current provider status and doctor checks once and persist them."""

    active_registry = registry or default_registry()
    providers = build_providers(config, usage_directory, registry=active_registry)
    statuses = tuple(read_status(provider, refresh=True) for provider in providers)
    history.record_provider_usage(db_path, statuses)
    report = run_doctor(config, usage_directory=usage_directory, registry=active_registry)
    history.record_provider_health(
        db_path, report, tuple(plugin.id for plugin in active_registry.all())
    )


class _DaemonCall:
    """One synchronous call, whose flight survives cancellation of its waiter."""

    def __init__(self, name: str, flight: threading.Lock, stop_timeout: float) -> None:
        self.name = name
        self.flight = flight
        self.stop_timeout = stop_timeout
        self.stopped = False
        self._future: asyncio.Future[None] | None = None

    async def run_once(self, function: Callable[[], object]) -> bool:
        if self.stopped or not self.flight.acquire(blocking=False):
            return False
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._future = future

        def complete() -> None:
            if not future.done():
                future.set_result(None)

        def run() -> None:
            try:
                function()
            except Exception:  # noqa: BLE001 - keep subsequent intervals alive
                logger.warning("%s failed", self.name, exc_info=True)
            finally:
                # Only the worker releases flight, even if its waiter was
                # cancelled or another app starts while this one shuts down.
                self.flight.release()
                try:
                    loop.call_soon_threadsafe(complete)
                except RuntimeError:
                    pass  # The owning event loop has already closed.

        try:
            threading.Thread(target=run, name=self.name, daemon=True).start()
        except BaseException:
            self.flight.release()
            complete()
            raise
        await asyncio.shield(future)
        return True

    async def stop(self) -> None:
        self.stopped = True
        if self._future is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._future), timeout=self.stop_timeout)
        except asyncio.TimeoutError:
            logger.warning("%s may still be finishing after shutdown", self.name)


class Scheduler:
    """Poll on daemon threads, at most one poll across service app instances."""

    def __init__(
        self,
        config: Config,
        *,
        usage_directory: Path | None,
        db_path: str | Path,
        interval_seconds: float = 60.0,
        registry: ProviderRegistry | None = None,
        poll_function: Callable[..., object] | None = None,
        stop_timeout: float = 1.0,
    ) -> None:
        self.config = config
        self.usage_directory = usage_directory
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.registry = registry or default_registry()
        self.poll_function = poll_function or poll_once
        self._worker = _DaemonCall("Provider usage poll", _poll_flight, stop_timeout)
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> bool:
        return await self._worker.run_once(
            lambda: self.poll_function(
                self.config, self.usage_directory, self.db_path, self.registry
            )
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a single bad poll must not kill the app
                logger.warning("Provider usage poll failed", exc_info=True)
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise

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
