# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A routed delegation starts at most one worker.

A product decision (2026-09-26): "Do not automatically try a chain
of alternative agents." When a delegated action does not succeed, a truthful
result returns to the original task owner -- the Host -- who
decides how to proceed. So in `ExecutionManager.delegate`'s routed path:

* which worker runs is decided BEFORE anything starts, and never by the
  recommendation or by quota: the default worker (Codex) when it can start,
  otherwise none -- nothing is substituted (OSS v1 step 5);
* once a worker is dispatched, its result is the delegation's result,
  whatever its status and whether or not it carries `safe_to_retry`;
* `no_viable_worker` naming `unknown` is reserved for "nothing could be
  started at all".

The explicit-provider path already started exactly the named provider or
refused, and is not exercised here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from modelquad.config import load_config
from modelquad.execution import (
    ExecutionManager,
    ExecutionResult,
    WorkerTaskStatus,
)
from modelquad.models import Freshness, ProviderStatus

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
ZETA = "zeta"


def _status(name, remaining):
    return ProviderStatus(
        name=name,
        available=True,
        remaining_percentage=remaining,
        observed_at=_NOW,
        freshness=Freshness.FRESH,
    )


def _statuses(*, codex=80.0):
    """Fresh statuses. With the default configuration the router recommends
    the host-only Claude runtime for `exploration`, `review` and
    `architecture`, the only task types the read-only Codex worker takes;
    each test asserts the recommendation it relies on.
    `zeta` is the synthetic second worker `_manager` registers."""

    return (_status("claude", 80.0), _status("codex", codex), _status(ZETA, 60.0))


def _manager(executors, *, config=None, extra_plugins=()):
    """A manager whose registry also holds a startable, lower-ranked second
    worker (`zeta`), so "no second worker starts" is a real claim."""

    from modelquad.registry import (
        ProviderCapabilities, ProviderPlugin, UsageSourceKind,
        WORKSPACE_DEVELOPMENT_GRANT, default_registry,
    )

    registry = default_registry()
    zeta = ProviderPlugin(
        id=ZETA, display_name="Zeta",
        capabilities=ProviderCapabilities(coding=True, cli_execution=True),
        usage_source_kind=UsageSourceKind.UNKNOWN,
        capability_profile=dict(WORKSPACE_DEVELOPMENT_GRANT),
    )
    fits = {ZETA: 0.3}
    for plugin, fit in ((zeta, None), *extra_plugins):
        registry.register(plugin)
        if fit is not None:
            fits[plugin.id] = fit
    base = config or load_config()
    config = replace(base, task_fit={
        task: {**task_fits, **fits} for task, task_fits in base.task_fit.items()
    })
    for plugin_id in fits:
        state = registry.get(plugin_id).capability_state()
        config.provider_capability_profiles[plugin_id] = state
        config.provider_capabilities[plugin_id] = state.operational_capabilities
    return ExecutionManager(config, registry=registry, executors=executors)


class _Worker:
    """A worker test double: counts dispatches and returns what it is told."""

    def __init__(self, name, result=None, *, available=True):
        self.name = name
        self._result = result
        self.available = available
        self.calls = 0

    def is_available(self):
        return self.available

    def execute(self, request):
        self.calls += 1
        if self._result is not None:
            return replace(self._result, provider=request.provider)
        return ExecutionResult(
            provider=request.provider,
            success=True,
            output="done",
            exit_code=0,
            task_status=WorkerTaskStatus.COMPLETED,
        )


def _blocked_safe_to_retry(reason="sandbox_process_spawn_failed", **extra):
    return ExecutionResult(
        provider="placeholder",
        success=False,
        output="",
        exit_code=None,
        error="the worker could not start its sandbox",
        task_status=WorkerTaskStatus.BLOCKED,
        task_status_reason=reason,
        safe_to_retry=True,
        **extra,
    )


def _delegate(manager, tmp_path, task_type="review", **kwargs):
    kwargs.setdefault("statuses", _statuses())
    return manager.delegate(task_type, "do the thing", str(tmp_path), **kwargs)


# --- once dispatched, the worker's result is the delegation's result -------


def test_a_blocked_safe_to_retry_recommendation_is_returned_and_no_second_worker_starts(
    tmp_path,
):
    """The regression: on main at 79f1c51 a second worker ran after Codex."""

    codex = _Worker("codex", _blocked_safe_to_retry())
    zeta = _Worker(ZETA)
    manager = _manager({"codex": codex, ZETA: zeta})

    result = _delegate(manager, tmp_path)

    assert codex.calls == 1
    assert zeta.calls == 0
    assert result.provider == "codex"
    assert result.task_status is WorkerTaskStatus.BLOCKED
    # A read-only result is a closed projection: reason and error are labels.
    assert result.task_status_reason == "worker_reported"
    assert result.error == "worker_error_reported"
    assert result.safe_to_retry is True
    assert result.task_completed is False
    assert result.metadata["recommended_provider"] == "claude"
    assert result.metadata["attempted_providers"] == ["codex"]
    assert result.metadata["fallback"] is False
    assert "fallback_from" not in result.metadata


def test_a_single_safe_to_retry_candidate_is_returned_not_no_viable_worker(tmp_path):
    """On main the chain ran out and replaced this with `no_viable_worker`."""

    codex = _Worker("codex", _blocked_safe_to_retry(reason="permission_required"))
    manager = ExecutionManager(load_config(), executors={"codex": codex})

    result = _delegate(manager, tmp_path)

    assert codex.calls == 1
    assert result.provider == "codex"
    assert result.task_status_reason == "worker_reported"  # closed read-only label
    assert result.task_status_reason != "no_viable_worker"
    assert result.safe_to_retry is True
    assert result.metadata["attempted_providers"] == ["codex"]


@pytest.mark.parametrize(
    "status",
    [
        WorkerTaskStatus.COMPLETED,
        WorkerTaskStatus.PARTIAL,
        WorkerTaskStatus.FAILED,
        WorkerTaskStatus.UNKNOWN,
        WorkerTaskStatus.BLOCKED,
    ],
)
@pytest.mark.parametrize("safe_to_retry", [True, False])
def test_every_dispatched_outcome_is_final(tmp_path, status, safe_to_retry):
    outcome = ExecutionResult(
        provider="placeholder",
        success=status is WorkerTaskStatus.COMPLETED,
        output="",
        exit_code=0 if status is WorkerTaskStatus.COMPLETED else 1,
        task_status=status,
        task_status_reason=f"worker_said_{status.value}",
        safe_to_retry=safe_to_retry,
    )
    codex = _Worker("codex", outcome)
    zeta = _Worker(ZETA)
    manager = _manager({"codex": codex, ZETA: zeta})

    result = _delegate(manager, tmp_path)

    assert codex.calls == 1
    assert zeta.calls == 0
    assert result.provider == "codex"
    assert result.task_status is status
    # The worker's own reason survives only as a closed read-only label.
    assert result.task_status_reason == "worker_reported"
    assert result.safe_to_retry is safe_to_retry
    assert result.metadata["attempted_providers"] == ["codex"]


def test_the_dispatched_workers_leftovers_are_carried_as_is(tmp_path):
    facts = {
        "enumerated": True,
        "method": "toolhelp32",
        "observed_at": 2.0,
        "descendants": [{"pid": 111, "image": "node.exe", "created": 1.0}],
    }
    codex = _Worker("codex", _blocked_safe_to_retry(metadata={"worker_descendants": facts}))
    zeta = _Worker(ZETA)
    manager = _manager({"codex": codex, ZETA: zeta})

    result = _delegate(manager, tmp_path)

    assert zeta.calls == 0
    # The closed read-only projection states truncation and reason explicitly.
    assert result.metadata["worker_descendants"] == {**facts, "truncated": False, "reason": None}


# --- before anything starts, an unstartable recommendation is substituted --


def test_a_host_only_recommendation_still_runs_only_the_default_worker(tmp_path):
    """The router recommends the host-only Claude runtime for `review` by
    task fit. That advice never picks the worker: the default worker (Codex)
    runs, and only it, even when it returns `safe_to_retry`."""

    codex = _Worker("codex", _blocked_safe_to_retry())
    zeta = _Worker(ZETA)
    manager = _manager({"codex": codex, ZETA: zeta})

    result = _delegate(manager, tmp_path, task_type="review")

    assert result.metadata["recommended_provider"] == "claude"
    assert codex.calls == 1 and zeta.calls == 0
    assert result.provider == "codex"
    assert result.safe_to_retry is True
    assert result.metadata["worker_source"] == "default"
    assert result.metadata["attempted_providers"] == ["codex"]
    assert result.metadata["fallback"] is False
    assert "fallback_from" not in result.metadata


def test_an_uninstalled_default_worker_is_never_substituted(tmp_path):
    from modelquad.registry import (
        ProviderCapabilities, ProviderPlugin, UsageSourceKind,
        WORKSPACE_DEVELOPMENT_GRANT,
    )
    plugin = ProviderPlugin(
        id="test_worker", display_name="Synthetic worker",
        capabilities=ProviderCapabilities(coding=True, cli_execution=True),
        usage_source_kind=UsageSourceKind.UNKNOWN,
        capability_profile=dict(WORKSPACE_DEVELOPMENT_GRANT),
    )
    codex = _Worker("codex", available=False)
    other = _Worker(plugin.id)
    zeta = _Worker(ZETA)
    manager = _manager(
        {"codex": codex, plugin.id: other, ZETA: zeta},
        extra_plugins=((plugin, 0.8),),
    )

    result = _delegate(manager, tmp_path, statuses=(*_statuses(), _status(plugin.id, 80.0)))

    assert codex.calls == other.calls == zeta.calls == 0
    assert result.provider == "unknown"
    assert result.task_status_reason == "no_viable_worker"
    assert result.metadata["attempted_providers"] == []
    assert result.metadata["fallback"] is False


def test_an_exhausted_default_worker_still_runs_when_asked(tmp_path):
    """Quota is display-only: Codex at 5% runs when the caller asked for a
    delegation; nothing is skipped and no other worker is tried."""
    codex = _Worker("codex")
    zeta = _Worker(ZETA)
    manager = _manager({"codex": codex, ZETA: zeta})

    result = _delegate(manager, tmp_path, task_type="architecture", statuses=_statuses(codex=5.0))

    assert codex.calls == 1 and zeta.calls == 0
    assert result.provider == "codex"
    assert "skipped_providers" not in result.metadata


# --- nothing could be started ----------------------------------------------


def test_no_startable_candidate_is_still_no_viable_worker(tmp_path):
    codex = _Worker("codex", available=False)
    zeta = _Worker(ZETA, available=False)
    manager = _manager({"codex": codex, ZETA: zeta})

    result = _delegate(manager, tmp_path)

    assert codex.calls == 0
    assert zeta.calls == 0
    assert result.provider == "unknown"
    assert result.task_status is WorkerTaskStatus.BLOCKED
    assert result.task_status_reason == "no_viable_worker"
    assert result.safe_to_retry is False
    assert result.metadata["attempted_providers"] == []
    assert result.metadata["recommended_provider"] == "claude"
    assert result.metadata["fallback"] is False
    assert "fallback_from" not in result.metadata
