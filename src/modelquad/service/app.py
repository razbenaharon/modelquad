# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""FastAPI application factory for the local control-plane service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from ..branding import PRODUCT_NAME
from ..config import Config, FreshnessThresholds, ProviderConfig, load_config
from ..registry import ProviderRegistry, default_registry
from . import history
from .auth import ServiceSecret
from .api import router as api_router
from .jobs_api import router as jobs_router
from .fate_api import router as fate_router
from .collector import Scheduler
from .db import DEFAULT_DB_PATH, connect_and_init
from .job_maintenance import JobMaintenance, _utc_now
from .trust import LoopbackTrustMiddleware


class LoopbackFastAPI(FastAPI):
    """Run the trust boundary before FastAPI dispatch and its entire stack."""

    def __init__(
        self,
        *,
        write_secret: str | None = None,
        secret_problem: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Wrap the bound parent call, never self: accepted requests enter
        # FastAPI once; refused HTTP requests never enter framework dispatch.
        self._trust_boundary = LoopbackTrustMiddleware(
            super().__call__,
            write_secret=write_secret,
            secret_problem=secret_problem,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The existing boundary checks HTTP once and passes WebSocket and
        # lifespan scopes through unchanged.
        await self._trust_boundary(scope, receive, send)


def _active_config(base: Config, settings: dict) -> Config:
    """Layer persisted weights/penalties/freshness onto a base Config.

    The task-fit matrix and sessions path are not service-editable in v0.4,
    so they always come from the base config (packaged defaults plus any
    ``--config`` override), never from persisted service settings.
    """

    return Config(
        freshness=FreshnessThresholds(**settings["freshness"]),
        task_fit=base.task_fit,
        sessions_path=base.sessions_path,
        providers=dict(base.providers),
    )


def _with_provider_overrides(config: Config, overrides: dict[str, bool]) -> Config:
    providers = dict(config.providers)
    for provider, enabled in overrides.items():
        previous = providers.get(provider, ProviderConfig())
        providers[provider] = ProviderConfig(enabled=enabled, options=previous.options)
    return replace(config, providers=providers)


def create_app(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    usage_directory: str | Path | None = None,
    config_path: str | Path | None = None,
    poll_interval: float = 60.0,
    registry: ProviderRegistry | None = None,
    poll_providers: bool = True,
    write_secret: ServiceSecret | str | None = None,
    poll_function: Callable[..., object] | None = None,
    maintenance_interval: float = 600.0,
    maintenance_clock: Callable[[], datetime] = _utc_now,
    maintenance_pass: Callable[..., object] | None = None,
    background_stop_timeout: float = 1.0,
) -> FastAPI:
    """Build the local control-plane app.

    The app answers only to loopback ``Host`` names; every other ``Host`` is
    refused before a route runs (``service/trust.py``).

    ``write_secret`` is the per-installation secret every request must
    present in ``X-ModelQuad-Token`` except the bounded listener proof
    exchange (Host/Origin checks still apply). ``service/auth.py`` defines
    the verified secret. With None, or a :class:`ServiceSecret` that failed
    verification, the app refuses every request, saying why.

    ``poll_providers=False`` builds the same app without the background usage
    collector. The collector shells out to whichever provider CLIs are
    installed on the machine, which is correct in production and wrong in a
    test: it makes results depend on the developer's own tooling, and on
    Windows a CLI that is itself a ``.cmd`` shim surfaces a console window
    that ``CREATE_NO_WINDOW`` on the direct child cannot suppress (the shim's
    own grandchildren get their own console host).
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = connect_and_init(db_path)
        conn.close()
        try:
            from ..jobs.recovery import schedule_recovery
            schedule_recovery(db_path)
        except Exception:
            logging.getLogger(__name__).warning('job recovery startup unavailable')
        # Startup reconciliation: a service restart is the natural moment to
        # close delegation attempts whose recorded worker verifiably no
        # longer exists -- an attempt must never stay 'started' forever.
        history.reconcile_stale_attempts(db_path)
        base_config = load_config(config_path)
        settings = history.get_settings(db_path)
        app.state.db_path = db_path
        app.state.usage_directory = Path(usage_directory) if usage_directory is not None else None
        app.state.config = _with_provider_overrides(
            _active_config(base_config, settings), history.get_provider_overrides(db_path)
        )

        maintenance = JobMaintenance(
            db_path,
            interval_seconds=maintenance_interval,
            clock=maintenance_clock,
            pass_function=maintenance_pass,
            stop_timeout=background_stop_timeout,
        )
        app.state.job_maintenance = maintenance
        scheduler = None
        if poll_providers:
            scheduler = Scheduler(
                app.state.config,
                usage_directory=app.state.usage_directory,
                db_path=db_path,
                interval_seconds=poll_interval,
                registry=app.state.registry,
                poll_function=poll_function,
                stop_timeout=background_stop_timeout,
            )
        app.state.scheduler = scheduler
        try:
            maintenance.start()
            if scheduler is not None:
                scheduler.start()
            yield
        finally:
            if scheduler is None:
                await maintenance.stop()
            else:
                # Stop both scheduling loops before waiting for either worker.
                await asyncio.gather(maintenance.stop(), scheduler.stop())

    if isinstance(write_secret, ServiceSecret):
        secret_value, secret_problem = write_secret.value, write_secret.problem
    else:
        secret_value, secret_problem = write_secret, None
    app = LoopbackFastAPI(
        title=PRODUCT_NAME,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        write_secret=secret_value,
        secret_problem=secret_problem,
    )
    app.state.registry = registry or default_registry()
    from .listener_proof import derive_key
    from .socket_identity import ListenerError
    try:
        app.state.listener_proof_key = derive_key(secret_value)
    except ListenerError:
        app.state.listener_proof_key = None
    app.include_router(fate_router)
    app.include_router(api_router)
    app.include_router(jobs_router)

    return app
