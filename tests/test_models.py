# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for shared models and foundation schemas."""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from orchestrator.models import (
    DECISION_SOURCES,
    CapacityState,
    Freshness,
    HostContext,
    ModelIdentitySource,
    ProviderScore,
    ProviderStatus,
    RoutingResult,
    TaskType,
)


def test_model_identity_source_enum_values():
    assert ModelIdentitySource.CONFIRMED.value == "confirmed"
    assert ModelIdentitySource.REQUESTED.value == "requested"
    assert ModelIdentitySource.REPORTED.value == "reported"
    assert ModelIdentitySource.CONFIGURED.value == "configured"
    assert ModelIdentitySource.UNKNOWN.value == "unknown"


def test_host_context_roundtrip():
    ctx = HostContext(
        host="claude-code",
        current_provider="claude",
        current_model="claude-3-5-sonnet-20241022",
        current_model_source=ModelIdentitySource.CONFIRMED,
        current_reasoning_mode="standard",
        context_owner="user",
        context_richness="rich",
    )
    serialized = ctx.to_dict()
    assert serialized["host"] == "claude-code"
    assert serialized["current_model_source"] == "confirmed"
    assert serialized["context_richness"] == "rich"

    restored = HostContext.from_dict(serialized)
    assert restored == ctx


def test_host_context_invalid_richness():
    with pytest.raises(ValueError, match="context_richness"):
        HostContext(context_richness="excessive")


def test_host_context_from_dict_tolerant_source():
    data = {
        "host": "test-host",
        "current_model_source": "invalid_source_name",
    }
    ctx = HostContext.from_dict(data)
    assert ctx.host == "test-host"
    assert ctx.current_model_source is ModelIdentitySource.UNKNOWN

    empty_ctx = HostContext.from_dict({})
    assert empty_ctx.current_model_source is ModelIdentitySource.UNKNOWN
    assert empty_ctx.host is None


def test_capacity_state_validation_and_to_dict():
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    state = CapacityState(
        remaining_percentage=85.5,
        used_percentage=14.5,
        reset_at=now,
        window="5h",
        freshness=Freshness.FRESH,
        collected_at=now,
        burn_rate_per_hour=2.5,
        resource_objective="balanced",
    )
    payload = state.to_dict()
    assert payload["remaining_percentage"] == 85.5
    assert payload["used_percentage"] == 14.5
    assert payload["reset_at"] == now.isoformat()
    assert payload["freshness"] == "fresh"
    assert payload["burn_rate_per_hour"] == 2.5

    # Validation: percentages must be 0..100
    with pytest.raises(ValueError, match="remaining_percentage"):
        CapacityState(remaining_percentage=-1.0)

    with pytest.raises(ValueError, match="remaining_percentage"):
        CapacityState(remaining_percentage=101.0)

    with pytest.raises(ValueError, match="used_percentage"):
        CapacityState(used_percentage=-5.0)

    with pytest.raises(ValueError, match="used_percentage"):
        CapacityState(used_percentage=105.0)


def test_provider_status_backward_compatibility_with_capacity():
    # Without capacity
    status_without = ProviderStatus(name="claude", remaining_percentage=80.0)
    assert status_without.capacity is None
    payload_without = status_without.to_dict()
    assert "capacity" in payload_without
    assert payload_without["capacity"] is None

    # With capacity
    cap = CapacityState(remaining_percentage=80.0, freshness=Freshness.FRESH)
    status_with = ProviderStatus(name="claude", remaining_percentage=80.0, capacity=cap)
    assert status_with.capacity == cap
    payload_with = status_with.to_dict()
    assert payload_with["capacity"]["remaining_percentage"] == 80.0
    assert payload_with["capacity"]["freshness"] == "fresh"


def test_routing_result_decision_source_default_and_validation():
    # Default is deterministic
    res = RoutingResult(
        task_type=TaskType.IMPLEMENTATION,
        providers=(),
        recommendation="codex",
    )
    assert res.decision_source == "deterministic"
    assert res.to_dict()["decision_source"] == "deterministic"

    # Custom valid decision source
    res_override = RoutingResult(
        task_type=TaskType.IMPLEMENTATION,
        providers=(),
        recommendation="claude",
        decision_source="user_override",
    )
    assert res_override.decision_source == "user_override"
    assert res_override.to_dict()["decision_source"] == "user_override"

    # Invalid decision source
    with pytest.raises(ValueError, match="decision_source"):
        RoutingResult(
            task_type=TaskType.IMPLEMENTATION,
            providers=(),
            recommendation="codex",
            decision_source="random_magic",
        )
