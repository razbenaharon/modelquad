# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Explicit selection retains hard constraints without invoking preferences."""

from dataclasses import replace

import pytest

from modelquad import netpolicy
from modelquad.capability import CapabilityFact, CapabilityProfile, RestrictionReason, VerificationState
from modelquad.config import Config, ProviderConfig
from modelquad.execution import ExecutionManager, ExecutionResult, WorkerTaskStatus
from modelquad.execution.base import attempt_resolution
from modelquad.models import NETWORK_POLICY_REFUSED, ProviderStatus
from modelquad.router import Router
from modelquad.routing_policy import LaunchRefused
from modelquad.service import history


class RecordingExecutor:
    def __init__(self, *, available=True):
        self.available = available
        self.requests = []

    def is_available(self):
        return self.available

    def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(
            provider=request.provider, success=True, output="", exit_code=0,
            task_status=WorkerTaskStatus.COMPLETED,
        )


@pytest.mark.parametrize("case", [
    "available", "disabled", "unconfigured", "unavailable", "restricted",
    "unsupported", "needs_repair", "network", "network_disabled", "read_only", "caller_command",
])
def test_explicit_and_routed_hard_gates_agree(tmp_path, monkeypatch, case):
    config = Config()
    provider = (
        "unconfigured" if case == "unconfigured" else
        "codex"
    )
    task = "testing" if case in {"restricted", "unsupported", "needs_repair"} else "review"
    required = {"command"} if case == "caller_command" else None
    status = ProviderStatus(name=provider, available=case != "unavailable")
    policy = {provider: "ModelQuad synthetic network refusal"} if case.startswith("network") else {}
    if case in {"disabled", "network_disabled"}:
        config = replace(config, providers={provider: ProviderConfig(enabled=False)})
    if case in {"restricted", "read_only", "caller_command"}:
        facts = dict(config.capability_profile(provider).facts)
        facts["command"] = CapabilityFact(
            capability="command", supported=True, allowed=False,
            restriction_reason=RestrictionReason("synthetic_restriction", "ModelQuad synthetic restriction"),
            verification=VerificationState.UNVERIFIED,
        )
        monkeypatch.setitem(config.provider_capability_profiles, provider, CapabilityProfile(provider, facts))
    if case in {"unsupported", "needs_repair"}:
        facts = dict(config.capability_profile(provider).facts)
        facts["command"] = CapabilityFact(
            capability="command", supported=case != "unsupported", allowed=True,
            verification=VerificationState.FAILED if case == "needs_repair" else VerificationState.UNVERIFIED,
        )
        monkeypatch.setitem(config.provider_capability_profiles, provider, CapabilityProfile(provider, facts))
    monkeypatch.setattr(netpolicy, "providers_refused_by_policy", lambda **kw: policy)
    router = Router(config)
    routed = next(item for item in router.route(
        task, [status], required_capabilities=required, policy_excluded=policy,
    ).providers if item.provider == provider)
    refusal = router.eligibility_refusal(task, provider, status, required, policy_excluded=policy)
    assert (refusal is None) is routed.eligible
    if refusal is not None:
        assert refusal.ineligibility_reasons == routed.ineligibility_reasons
        assert refusal.capability_gaps == routed.capability_gaps
        assert refusal.score is None
    selected, alternate = RecordingExecutor(), RecordingExecutor()
    manager = ExecutionManager(config, router=router, executors={provider: selected, "claude": alternate})
    result = manager.delegate(task, "synthetic task", tmp_path, provider=provider,
                              statuses=iter([status]), required_capabilities=required)
    assert bool(selected.requests) is routed.eligible
    assert alternate.requests == []
    if not routed.eligible:
        assert result.provider == "unknown"
        assert result.task_status is WorkerTaskStatus.BLOCKED
        assert result.task_status_reason == (NETWORK_POLICY_REFUSED if case.startswith("network") else "routing_refused")
        if case.startswith("network"):
            assert result.safe_to_retry and result.metadata["network_policy"] == {
                "allowed": False, "category": "provider_api", "reason": policy[provider],
            }
        for reason in routed.ineligibility_reasons:
            assert reason in result.error
        from modelquad.execution.bound_launch import _executed_provider_for_row
        assert _executed_provider_for_row(result) is history.NOTHING_SELECTED


def test_explicit_identity_is_preserved_without_any_preference(tmp_path, monkeypatch):
    config = Config(model_policy={
        "codex": {"review": {"model": "configured-prior", "reasoning_effort": "high"}},
    })
    router = Router(config)
    def forbidden(*args, **kwargs):
        pytest.fail("explicit selection invoked a preference or routing stage")
    for name in ("route", "_score_provider", "_apply_independence", "_build_target"):
        monkeypatch.setattr(router, name, forbidden)
    selected, alternate = RecordingExecutor(), RecordingExecutor()
    manager = ExecutionManager(config, router=router, executors={"codex": selected, "claude": alternate})
    observed = []
    original = router.eligibility_refusal
    def capture(task, provider, status, required=None, **kwargs):
        observed.append(status)
        return original(task, provider, status, required, **kwargs)
    monkeypatch.setattr(router, "eligibility_refusal", capture)
    result = manager.delegate(
        "review", "synthetic task", tmp_path, agent_runtime="codex",
        model="exact-requested-model", reasoning_effort="medium", session_id="requested-session",
    )
    assert result.task_status is WorkerTaskStatus.COMPLETED
    [request] = selected.requests
    assert (request.provider, request.agent_runtime, request.model, request.reasoning_effort,
            request.session_id) == ("codex", "codex", "exact-requested-model", "medium", "requested-session")
    assert alternate.requests == []
    [status] = observed
    assert status.available and status.remaining_percentage is None
    assert status.observed_at is None and status.freshness.value == "unknown" and status.pools == ()


@pytest.mark.parametrize("provider,runtime,registered,reason,failure_kind", [
    (None, "not-registered", True, "runtime_not_registered", "invalid_request"),
    ("codex", "not-registered", True, "runtime_not_registered", "invalid_request"),
    ("codex", "claude", True, "runtime_provider_mismatch", "invalid_request"),
    ("codex", "codex", False, "provider_not_registered", "provider_unavailable"),
])
def test_invalid_runtime_starts_nothing(tmp_path, provider, runtime, registered, reason, failure_kind):
    selected, alternate = RecordingExecutor(), RecordingExecutor()
    executors = {"claude": alternate}
    if registered:
        executors["codex"] = selected
    manager = ExecutionManager(Config(), executors=executors)
    result = manager.delegate("implementation", "task", tmp_path, provider=provider, agent_runtime=runtime)
    assert result.provider == "unknown" and result.task_status is WorkerTaskStatus.BLOCKED
    assert result.task_status_reason == reason
    assert attempt_resolution(result) == ("failed", failure_kind)
    assert selected.requests == alternate.requests == []


def test_actual_executor_unavailability_still_refuses(tmp_path):
    selected, alternate = RecordingExecutor(available=False), RecordingExecutor()
    manager = ExecutionManager(Config(), executors={"codex": selected, "claude": alternate})
    result = manager.delegate("implementation", "task", tmp_path, provider="codex",
                              statuses=[ProviderStatus(name="codex", available=True)])
    assert result.provider == "unknown" and result.task_status_reason == "provider_unavailable"
    assert attempt_resolution(result) == ("failed", "provider_unavailable")
    assert selected.requests == alternate.requests == []


def test_explicit_status_uses_the_same_last_observation_as_routing(tmp_path):
    selected = RecordingExecutor()
    statuses = [ProviderStatus(name="codex", available=True), ProviderStatus(name="codex", available=False)]
    router = Router(Config())
    routed = next(item for item in router.route("review", statuses).providers if item.provider == "codex")
    result = ExecutionManager(router.config, router=router, executors={"codex": selected}).delegate(
        "review", "task", tmp_path, provider="codex", statuses=iter(statuses),
    )
    assert not routed.eligible and result.task_status is WorkerTaskStatus.BLOCKED
    assert selected.requests == []


def test_pause_changed_after_advice_is_rechecked_before_explicit_execution(tmp_path):
    path = tmp_path / "synthetic-controls.db"
    history.put_routing_paused(path, False)
    selected = RecordingExecutor()
    def availability():
        history.put_routing_paused(path, True)
        return True
    selected.is_available = availability
    manager = ExecutionManager(Config(), executors={"codex": selected}, health_db_path=path)
    manager.launch_invocation_mode = "explicit_user_request"
    manager.launch_user_approved = True
    with pytest.raises(LaunchRefused, match="paused"):
        manager.delegate("review", "task", tmp_path, provider="codex")
    assert selected.requests == [] and not manager.launch_entered
