# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for the routing policy decision authority."""

from __future__ import annotations

import pytest

from orchestrator.routing_policy import DelegationPolicyDecision, check_delegation_policy


def test_delegation_policy_decision_to_dict():
    decision = DelegationPolicyDecision(
        allowed=False,
        routing_mode="ask",
        needs_user_approval=True,
        reason="some reason",
        routing_paused=True,
    )
    assert decision.to_dict() == {
        "allowed": False,
        "routing_mode": "ask",
        "needs_user_approval": True,
        "reason": "some reason",
        "routing_paused": True,
    }


@pytest.mark.parametrize("invocation_mode", [None, "automatic", "explicit_user_request"])
@pytest.mark.parametrize("user_approved", [False, True])
def test_automatic_mode_always_allowed(invocation_mode, user_approved):
    decision = check_delegation_policy(
        routing_mode="automatic",
        invocation_mode=invocation_mode,
        user_approved=user_approved,
    )
    assert decision.allowed is True
    assert decision.routing_mode == "automatic"
    assert decision.needs_user_approval is False
    assert decision.reason is None


@pytest.mark.parametrize("routing_mode", ["ask", "manual"])
@pytest.mark.parametrize("invocation_mode", [None, "automatic", "explicit_user_request"])
@pytest.mark.parametrize("user_approved", [False, True])
def test_retired_active_modes_are_invalid(routing_mode, invocation_mode, user_approved):
    decision = check_delegation_policy(routing_mode=routing_mode,
        invocation_mode=invocation_mode, user_approved=user_approved)
    assert not decision.allowed and not decision.needs_user_approval
    assert "launch_state_unknown" in decision.reason


def test_disconnect_remains_independent_of_automatic_mode():
    decision = check_delegation_policy(routing_mode="automatic", invocation_mode="explicit_user_request",
        user_approved=True, disconnected_hosts=["codex"], caller_host="codex")
    assert not decision.allowed and "host_disconnected" in decision.reason


@pytest.mark.parametrize("invocation_mode", [None, "automatic", "explicit_user_request"])
@pytest.mark.parametrize("user_approved", [False, True])
def test_unknown_routing_mode_refused(invocation_mode, user_approved):
    decision = check_delegation_policy(
        routing_mode="unknown_mode",
        invocation_mode=invocation_mode,
        user_approved=user_approved,
    )
    assert decision.allowed is False
    assert decision.routing_mode == "unknown_mode"
    assert decision.needs_user_approval is False
    assert "launch_state_unknown" in decision.reason


@pytest.mark.parametrize("routing_mode", ["automatic"])
@pytest.mark.parametrize("invocation_mode", [None, "automatic", "explicit_user_request"])
@pytest.mark.parametrize("user_approved", [False, True])
def test_pause_is_absolute(routing_mode, invocation_mode, user_approved):
    decision = check_delegation_policy(
        routing_mode=routing_mode, invocation_mode=invocation_mode,
        user_approved=user_approved, routing_paused=True,
    )
    assert not decision.allowed
    assert not decision.needs_user_approval
    assert decision.routing_paused is True
    assert "routing is paused" in decision.reason


@pytest.mark.parametrize("paused", [None, "false", 0, 1])
def test_unknown_pause_refused(paused):
    decision = check_delegation_policy(
        routing_mode="automatic", routing_paused=paused,
        invocation_mode="explicit_user_request", user_approved=True,
    )
    assert not decision.allowed
    assert "launch_state_unknown" in decision.reason


def test_pause_never_requests_approval():
    decision = check_delegation_policy(
        routing_mode="automatic", invocation_mode="automatic", user_approved=False, routing_paused=True,
    )
    assert not decision.allowed and not decision.needs_user_approval
    assert "routing is paused" in decision.reason
    assert "routing mode is 'ask'" not in decision.reason
