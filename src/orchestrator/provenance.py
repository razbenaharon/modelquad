# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Decision provenance helpers shared by the production recording paths.

One routing decision produces up to three history rows (routing_decisions,
plan_decisions, execution_history). This module supplies the correlation id
that joins them, the engine-version stamp that makes a replayed decision's
differences attributable, and the normalized evidence snapshot the router
actually decided over -- so a historical decision can later be replayed from
its own inputs instead of from current live provider state.

Deliberately dependency-light and side-effect free: production recording
paths write these values; readers consume the stored evidence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .hostobs import HostDecisionSnapshot

from .branding import installed_version
from .config import Config
from .models import ExpectedConsumption, ProviderStatus
from .router import ROUTER_ENGINE_VERSION


def new_decision_id() -> str:
    """A fresh correlation id for one routing decision's history rows."""

    return uuid.uuid4().hex


def config_fingerprint(config: Config) -> str:
    """Stable hash of the routing-relevant configuration in force.

    Covers the inputs that can change a routing decision without a code
    change. Not reversible; never includes paths or provider options.
    """

    payload = {
        "freshness": {
            "fresh_seconds": config.freshness.fresh_seconds,
            "stale_seconds": config.freshness.stale_seconds,
        },
        "task_fit": config.task_fit,
        "provider_capabilities": {
            provider: sorted(caps)
            for provider, caps in sorted((config.provider_capabilities or {}).items())
        },
        "task_capability_requirements": {
            task: sorted(caps)
            for task, caps in sorted((config.task_capability_requirements or {}).items())
        },
        "model_policy": config.model_policy,
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def engine_versions(config: Config | None = None) -> dict[str, Any]:
    """The reproducibility stamp recorded alongside every traced decision."""

    from .service.db import SCHEMA_VERSION  # local import: avoid a config<->db cycle

    versions: dict[str, Any] = {
        "modelquad_version": installed_version(default=None),
        "router_engine_version": ROUTER_ENGINE_VERSION,
        "db_schema_version": SCHEMA_VERSION,
    }
    if config is not None:
        versions["config_fingerprint"] = config_fingerprint(config)
    return versions


def evidence_snapshot_json(
    statuses: Iterable[ProviderStatus],
    config: Config,
    *,
    routing_mode: str | None = None,
    routing_paused: bool | None = None,
    expected_consumption: ExpectedConsumption | None = None,
    host_snapshot: "HostDecisionSnapshot | None" = None,
) -> str:
    """Serialize the router's decision-time inputs for replay.

    This is the deterministic-path counterpart of plan_decisions.packet_json:
    the normalized provider snapshots the router scored, the session resource
    strategy, the routing context in force, and -- since V8 slice 6 -- the
    host that owned the context when the decision was made. Inputs, not
    conclusions. Contains no prompts, task text, source code, or credentials
    (the status snapshots are the same normalized observations
    provider_usage_history already persists, and the host snapshot is
    normalized at its own ephemeral boundary in ``hostobs``).

    The host snapshot rides this existing column deliberately: a routing row
    is written once and never updated, so freezing the host here makes
    "a later observation cannot rewrite an earlier decision" a property of
    the storage rather than a rule someone has to remember. It also means no
    schema migration.
    """

    snapshot = {
        "schema": 2,
        "providers": [status.to_dict() for status in statuses],
        "routing_mode": routing_mode,
        "routing_paused": routing_paused,
        "expected_consumption": (
            expected_consumption.to_dict() if expected_consumption is not None else None
        ),
        "engine_versions": engine_versions(config),
    }
    # Absent rather than null when there is no snapshot at all, so a reader
    # can tell "this build did not capture host state" (pre-slice-6 rows)
    # from "we looked and could not identify the host" (observed=False).
    if host_snapshot is not None:
        snapshot["host"] = host_snapshot.to_dict()
    return json.dumps(snapshot, default=str)
