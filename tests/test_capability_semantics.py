# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""M-CAP regression suite: a restriction must never be reported as an inability.

Every test here protects one half of the same invariant. ModelQuad makes policy
decisions about what a delegated worker may do; those decisions must always be
attributable to ModelQuad, and must never be surfaced, recorded, or learned as a
fact about the provider's ability.

The historical defect these guard against: a worker that ModelQuad kept
files-only by policy was explained as incapable -- the router said it "lacks
required capability (command)" and the former Control Center said "Cannot run
commands", although the runtime could run commands. The routing OUTCOME was
right; every explanation of it was false.
"""

from __future__ import annotations

import datetime as dt

import pytest

from orchestrator.capability import (
    ALL_CAPABILITIES,
    CAP_COMMAND,
    CAP_READ,
    CAP_WRITE,
    CapabilityFact,
    CapabilityProfile,
    CapabilityState,
    RestrictionReason,
    VerificationEvidence,
    VerificationState,
    describe_gaps,
    granted,
    profile_from_declaration,
    restricted,
    unsupported,
)
from orchestrator.config import Config
from orchestrator.mcp.presentation import _alternatives
from orchestrator.models import Freshness, ProviderStatus, TaskType
from orchestrator.registry import default_registry
from orchestrator.router import Router


def _statuses(names=("claude", "codex")):
    now = dt.datetime.now(dt.timezone.utc)
    return [
        ProviderStatus(
            name=name,
            available=True,
            remaining_percentage=90.0,
            observed_at=now,
            freshness=Freshness.FRESH,
        )
        for name in names
    ]


def _candidate(result, provider):
    return next(item for item in result.providers if item.provider == provider)


def _command_withheld_config():
    """ModelQuad withholds `command` from Claude; the runtime still supports it."""

    return Config(provider_capabilities={"claude": frozenset({CAP_READ, CAP_WRITE})})


# --- 1. restricted is not unsupported ---------------------------------------


def test_restricted_capability_never_reports_as_unsupported():
    fact = CapabilityFact(
        capability=CAP_COMMAND,
        supported=True,
        allowed=False,
        restriction_reason=RestrictionReason("policy", "withheld here"),
    )
    assert fact.state is CapabilityState.RESTRICTED
    assert fact.state is not CapabilityState.UNSUPPORTED
    # The distinction that matters downstream: it is still SUPPORTED.
    assert fact.supported is True
    assert fact.operational is False


def test_a_policy_denial_must_state_a_reason():
    """A denial with no stated reason is indistinguishable from an inability."""

    with pytest.raises(ValueError, match="restriction_reason"):
        CapabilityFact(capability=CAP_COMMAND, supported=True, allowed=False)


# --- 2. unverified is not unsupported ---------------------------------------


def test_unknown_support_is_unverified_never_unsupported():
    fact = CapabilityFact(capability=CAP_COMMAND, supported=None, allowed=True)
    assert fact.state is CapabilityState.UNVERIFIED
    assert fact.state is not CapabilityState.UNSUPPORTED
    # Invariant 4: unknown must not behave as a confident "no".
    assert fact.operational is True


def test_unverified_does_not_block_routing():
    """Absent evidence is not evidence of absence.

    Requiring proof before routing would make every capability unusable on a
    fresh install, which is the confident-zero invariant 4 forbids.
    """

    profile = CapabilityProfile(
        provider="newcomer",
        facts={CAP_COMMAND: CapabilityFact(capability=CAP_COMMAND, supported=None, allowed=True)},
    )
    assert profile.gaps([CAP_COMMAND]) == ()


def test_verification_failure_is_repair_not_incapability():
    """A broken PATH must never become a permanent "this agent cannot"."""

    fact = CapabilityFact(capability=CAP_COMMAND, supported=True, allowed=True)
    failed = fact.with_verification_failure(VerificationEvidence(method="probe", detail="exit 127"))
    assert failed.state is CapabilityState.NEEDS_REPAIR
    assert failed.supported is True
    assert failed.state is not CapabilityState.UNSUPPORTED


# --- 3 & 4. the historical false negative, end to end ------------------------


def test_withheld_worker_is_restricted_not_incapable():
    """The exact class of defect M-CAP owns, asserted end to end."""

    result = Router(_command_withheld_config()).route(TaskType.TESTING, _statuses())
    withheld = _candidate(result, "claude")

    # The OUTCOME: a worker without `command` is not routed to command-coupled work.
    assert withheld.eligible is False
    assert withheld.exclusion_stage == "capability"
    assert withheld.preference_score is None

    # The EXPLANATION names a restriction, never an inability.
    assert withheld.ineligibility_reasons == ("restricted_capability:command",)
    assert withheld.excluded_by_policy is True
    assert withheld.capability_gaps[0]["state"] == "restricted"


def test_refusal_text_never_claims_a_withheld_worker_lacks_the_capability():
    """The literal phrasing users reported, asserted against directly."""

    result = Router(_command_withheld_config()).route(TaskType.TESTING, _statuses())
    reason = _candidate(result, "claude").reason.lower()
    for falsehood in ("lacks", "cannot perform this stage: worker lacks", "incapable"):
        assert falsehood not in reason, f"refusal text reintroduced {falsehood!r}: {reason}"
    # And it names what is actually responsible: the installation's own policy.
    assert "withheld" in reason and "configuration" in reason


def test_routing_distinguishes_genuinely_incapable_from_merely_restricted():
    restricted_profile = profile_from_declaration(
        "restricted-runtime",
        {"run_tests": restricted("policy", "withheld by policy")},
    )
    unsupported_profile = profile_from_declaration(
        "limited-runtime",
        {"run_tests": unsupported("this runtime has no shell at all")},
    )
    (restricted_gap,) = restricted_profile.gaps([CAP_COMMAND])
    (unsupported_gap,) = unsupported_profile.gaps([CAP_COMMAND])

    assert restricted_gap.reason_code == "restricted_capability:command"
    assert unsupported_gap.reason_code == "unsupported_capability:command"
    assert restricted_gap.is_policy_restriction is True
    assert unsupported_gap.is_policy_restriction is False
    # And they read differently to a person.
    assert "ModelQuad" not in describe_gaps([unsupported_gap])
    assert "does not support" in describe_gaps([unsupported_gap])


# --- 5. the user-facing surface ---------------------------------------------


def test_user_facing_surface_blames_modelquad_not_the_agent():
    result = Router(_command_withheld_config()).route(TaskType.TESTING, _statuses())
    decision = {"providers": [candidate.to_dict() for candidate in result.providers]}
    rows = {row["provider"]: row for row in _alternatives(decision, result.recommendation)}
    withheld = rows["claude"]

    assert withheld["status"] == "Restricted by ModelQuad"
    assert withheld["note"] == "ModelQuad does not allow this agent to run commands here"
    # The old text said "Cannot run commands", which was false.
    assert not withheld["note"].startswith("Cannot")


# --- 6. workspace scope does not imply machine-wide authority ----------------


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("capability", ["push", "release", "external_write", "external_network"])
def test_broad_workspace_capability_never_implies_machine_wide_permission(provider, capability):
    """Every worker gets ordinary development inside its workspace and nothing
    outside it. Granting `command` must not drag any of these along."""

    declaration = default_registry().get(provider).capability_profile
    grant = declaration[capability]
    assert grant.allowed is False, f"{provider} must not be permitted {capability}"
    assert grant.restriction_reason is not None
    # Denied by POLICY -- these runtimes can all push a branch; none may.
    assert grant.supported is True


def test_out_of_workspace_authority_is_not_a_routing_capability():
    """push/release/external_* must never become coarse routing capabilities.

    Promoting them would let "this worker may run commands" be read as "this
    worker may publish", which is the confusion the security model forbids.
    """

    from orchestrator.capability import FINE_TO_COARSE

    for key in ("push", "release", "external_write", "external_network"):
        assert key not in FINE_TO_COARSE
    assert set(FINE_TO_COARSE.values()) <= ALL_CAPABILITIES


# --- 7. one source of truth --------------------------------------------------


def test_router_gate_and_registry_declaration_cannot_disagree():
    """The gate is DERIVED from the declaration, so drift is unrepresentable.

    Before M-CAP these were two independent tables, and the one the router used
    carried a claim (a worker CANNOT run shell commands) that this repository had
    already disproved elsewhere.
    """

    config = Config()
    for plugin in default_registry().all():
        if not plugin.capability_profile:
            continue
        declared = plugin.capability_state().operational_capabilities
        effective = config.provider_capabilities.get(plugin.id)
        assert effective is not None, f"{plugin.id} has no effective capability entry"
        assert set(effective) == set(declared), (
            f"{plugin.id}: router gate {sorted(effective)} disagrees with the "
            f"registry declaration {sorted(declared)}"
        )


# --- 8. configuration migration ----------------------------------------------


def test_legacy_flat_config_is_read_as_permission_never_as_incapability():
    """A local config may withhold a capability; it may not claim a runtime
    lacks one. Otherwise an installation could manufacture a false statement
    about a provider it has never even run."""

    config = Config(provider_capabilities={"claude": frozenset({CAP_READ, CAP_WRITE})})
    command = config.capability_profile("claude").fact(CAP_COMMAND)

    assert command.allowed is False
    assert command.supported is True, "config must not overwrite what the runtime supports"
    assert command.state is CapabilityState.RESTRICTED
    assert command.restriction_reason.code == "config_permission_override"


def test_permission_override_is_idempotent():
    override = {"claude": frozenset({CAP_READ, CAP_WRITE})}
    once = Config(provider_capabilities=dict(override)).capability_profile("claude")
    twice = Config(
        provider_capabilities={"claude": frozenset(once.operational_capabilities)}
    ).capability_profile("claude")
    assert once.operational_capabilities == twice.operational_capabilities
    assert once.supported_capabilities == twice.supported_capabilities


def test_unknown_provider_is_never_silently_excluded():
    """An unconfigured provider answers UNKNOWN, not "incapable of everything"."""

    profile = Config().capability_profile("some-future-runtime")
    assert profile.gaps(ALL_CAPABILITIES) == ()
    assert profile.fact(CAP_COMMAND).state is CapabilityState.UNVERIFIED


# --- 9. verification is evidence, and only evidence ---------------------------


def test_verification_records_evidence_without_fabricating_support():
    fact = CapabilityFact(capability=CAP_WRITE, supported=None, allowed=True)
    assert fact.evidence.observed_at is None, "unchecked facts must not invent a timestamp"

    observed = dt.datetime.now(dt.timezone.utc)
    verified = fact.with_verification(
        VerificationEvidence(method="workspace_probe", detail="wrote and read back", observed_at=observed)
    )
    assert verified.state is CapabilityState.AVAILABLE
    assert verified.supported is True
    assert verified.evidence.observed_at == observed


def test_verification_cannot_grant_permission():
    """Proving a worker CAN run a command does not permit it to run commands."""

    fact = CapabilityFact(
        capability=CAP_COMMAND,
        supported=True,
        allowed=False,
        restriction_reason=RestrictionReason("shell_grant_unbounded", "cannot be bounded"),
    )
    verified = fact.with_verification(VerificationEvidence(method="probe", detail="ran"))
    assert verified.allowed is False
    assert verified.state is CapabilityState.RESTRICTED
    assert verified.operational is False


def test_verified_state_requires_actual_support():
    with pytest.raises(ValueError, match="observing it work IS evidence of support"):
        CapabilityFact(
            capability=CAP_COMMAND,
            supported=False,
            allowed=False,
            verification=VerificationState.VERIFIED,
        )


# --- 10. decision evidence and outcome hygiene --------------------------------


def test_decision_trace_can_explain_eligibility_semantically():
    """A recorded decision must be able to answer "why" without re-deriving it
    from a config version that may since have changed."""

    result = Router(_command_withheld_config()).route(TaskType.TESTING, _statuses())
    payload = _candidate(result, "claude").to_dict()
    (gap,) = payload["capability_gaps"]

    assert gap["capability"] == CAP_COMMAND
    assert gap["state"] == "restricted"
    assert gap["is_policy_restriction"] is True
    assert gap["restriction_reason"]["code"] == "config_permission_override"
    assert payload["required_capabilities"] == ["command", "read", "write"]


def test_policy_exclusion_is_distinguishable_from_a_provider_limitation():
    """Outcome classification reads this. A stage refused for a policy reason
    carries no information about that provider's ability, and counting it as
    one would teach the product a falsehood it created itself."""

    policy = Router(_command_withheld_config()).route(TaskType.TESTING, _statuses())
    # A config override is ModelQuad's choice, so this is a policy exclusion.
    assert _candidate(policy, "claude").excluded_by_policy is True

    # Codex is kept read-only by ModelQuad: also a policy exclusion here.
    assert _candidate(policy, "codex").excluded_by_policy is True

    review = Router(_command_withheld_config()).route(TaskType.REVIEW, _statuses())
    eligible = _candidate(review, "codex")
    assert eligible.excluded_by_policy is False
    assert eligible.capability_gaps == ()


def test_granted_capabilities_produce_no_gaps():
    profile = profile_from_declaration(
        "fully-capable",
        {"workspace_read": granted(), "workspace_write": granted(), "run_tests": granted()},
    )
    assert profile.gaps([CAP_READ, CAP_WRITE, CAP_COMMAND]) == ()
    assert profile.operational_capabilities == {CAP_READ, CAP_WRITE, CAP_COMMAND}


def test_doctor_treats_a_withheld_capability_as_healthy_not_broken():
    """Restriction is by design; only a configured-but-not-working capability
    is something a user can and should repair."""

    from orchestrator.doctor import CheckStatus, run_doctor

    from orchestrator.registry import (
        WORKSPACE_DEVELOPMENT_GRANT, ProviderCapabilities, ProviderPlugin, UsageSourceKind,
    )

    shell_withheld = restricted("shell_grant_unbounded", "the shell grant cannot be bounded")
    registry = default_registry()
    registry.register(ProviderPlugin(
        id="test_files_only", display_name="Synthetic files-only worker",
        capabilities=ProviderCapabilities(review=True), usage_source_kind=UsageSourceKind.UNKNOWN,
        capability_profile={**WORKSPACE_DEVELOPMENT_GRANT,
                            "run_tests": shell_withheld, "run_builds": shell_withheld},
    ))
    checks = {c.name: c for c in run_doctor(Config(), registry=registry).checks}
    check = checks["test_files_only worker capabilities"]

    assert check.status is CheckStatus.OK, "a deliberate restriction is not a fault"
    assert "withheld by ModelQuad" in check.detail
    assert "not supported by this runtime" not in check.detail
