# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Candidate eligibility vs preference (V8 slice 3, P1).

"Could this runtime legally perform the task?" is a hard constraint and must
never be expressed as a lower preference score. The case this fixes: a
files-only worker (here Claude, granted read and write but not command) cannot
complete an implementation task that must run its tests -- it is IMPOSSIBLE
for it, not merely a worse choice. Recording it as a slightly-lower-scored
alternative would teach a future learned router that Codex was mildly
preferred, when the truth is the files-only worker was disqualified.
"""

from __future__ import annotations

import pytest

from orchestrator.config import Config
from orchestrator.models import Freshness, ProviderScore, ProviderStatus, TaskType
from orchestrator.router import Router


# Claude is granted read+write only: a files-only worker for these tests.
FILES_ONLY = "claude"


def _files_only_config(**kwargs):
    return Config(provider_capabilities={FILES_ONLY: {"read", "write"}}, **kwargs)


def _statuses(**overrides):
    base = {
        "claude": ProviderStatus(name="claude", available=True, remaining_percentage=100.0, freshness=Freshness.FRESH),
        "codex": ProviderStatus(name="codex", available=True, remaining_percentage=90.0, freshness=Freshness.FRESH),
    }
    base.update(overrides)
    return list(base.values())


def _by_provider(result):
    return {candidate.provider: candidate for candidate in result.providers}


# --- the files-only worker: the generic fix, not a special case -----------


def test_command_requirement_makes_files_only_worker_ineligible():
    """The root-cause regression test.

    A bare `implementation` route leaves the files-only worker eligible (the
    task-type floor is only `write`, which it has). Declaring that THIS request must
    run commands is what disqualifies it -- and that declaration is the
    capability the advisory route path previously had no way to express.
    """
    router = Router(_files_only_config())

    without = _by_provider(router.route("implementation", _statuses()))
    assert without[FILES_ONLY].eligible is True

    with_tests = _by_provider(
        router.route("implementation", _statuses(), required_capabilities={"command"})
    )
    files_only = with_tests[FILES_ONLY]
    assert files_only.eligible is False
    assert files_only.ineligibility_reasons == ("restricted_capability:command",)
    assert files_only.exclusion_stage == "capability"
    assert files_only.capable is False


def test_ineligible_candidate_carries_no_preference_score():
    result = Router(_files_only_config()).route(
        "implementation", _statuses(), required_capabilities={"command"}
    )
    files_only = _by_provider(result)[FILES_ONLY]
    # No number that could read as "ranked alternative".
    assert files_only.preference_score is None
    assert files_only.score is None
    assert files_only.to_dict()["preference_score"] is None


def test_ineligible_candidate_is_never_recommended():
    result = Router(_files_only_config()).route(
        "testing", _statuses(), required_capabilities={"command"}
    )
    assert result.recommendation != FILES_ONLY
    assert _by_provider(result)[FILES_ONLY].eligible is False


def test_task_type_floor_alone_enforces_testing_stage():
    """`testing` requires command by its own floor -- no capsule needed."""
    result = Router(_files_only_config()).route("testing", _statuses())
    files_only = _by_provider(result)[FILES_ONLY]
    assert files_only.eligible is False
    assert "restricted_capability:command" in files_only.ineligibility_reasons


def test_capability_requirement_provenance_is_preserved():
    result = Router(_files_only_config()).route(
        "implementation", _statuses(), required_capabilities={"command"}
    )
    candidates = _by_provider(result)
    # Every candidate records what the stage required and what it had, so a
    # research record never has to re-derive it from a config that may have
    # changed since.
    assert candidates[FILES_ONLY].required_capabilities == ("command", "read", "write")
    assert candidates[FILES_ONLY].available_capabilities == ("read", "write")
    assert candidates["codex"].required_capabilities == ("command", "read", "write")
    # ModelQuad runs Codex read-only: write and command are withheld by policy.
    assert candidates["codex"].available_capabilities == ("read",)
    assert candidates["codex"].eligible is False


def test_low_preference_candidate_stays_eligible():
    """A poor fit is a preference fact, not a disqualification."""
    result = Router(Config()).route("architecture", _statuses())
    for candidate in result.providers:
        assert candidate.eligible is True
        assert candidate.preference_score is not None


def test_unavailable_provider_is_ineligible_at_the_availability_stage():
    statuses = _statuses(
        codex=ProviderStatus(
            name="codex", available=False, remaining_percentage=None, freshness=Freshness.UNKNOWN
        )
    )
    codex = _by_provider(Router(Config()).route("implementation", statuses))["codex"]
    assert codex.eligible is False
    assert codex.ineligibility_reasons == ("provider_unavailable",)
    assert codex.exclusion_stage == "availability"
    # Availability is not a capability failure -- the stages stay distinct.
    assert codex.capable is True


def test_missing_status_is_ineligible_with_its_own_stage():
    result = Router(Config()).route("implementation", [])
    for candidate in result.providers:
        assert candidate.eligible is False
        assert candidate.ineligibility_reasons == ("no_provider_status",)
        assert candidate.exclusion_stage == "availability"


def test_no_eligible_candidates_fails_honestly_with_reasons():
    """Every provider disqualified must produce an explained refusal, not a
    silent 'no score' shrug."""
    config = Config(
        provider_capabilities={
            "claude": {"read"},
            "codex": {"read"},
        }
    )
    result = Router(config).route("testing", _statuses())
    assert result.recommendation is None
    assert "No eligible providers" in result.reason
    assert "restricted_capability:command" in result.reason
    for candidate in result.providers:
        assert candidate.eligible is False


# --- the invariant itself ---------------------------------------------------


def test_provider_score_rejects_an_ineligible_candidate_with_a_score():
    with pytest.raises(ValueError, match="must not carry a preference score"):
        ProviderScore(
            provider="claude",
            available=True,
            fit=0.8,
            remaining_percentage=100.0,
            freshness=Freshness.FRESH,
            usage_score=1.0,
            usage_confidence=1.0,
            quota_penalty=0.0,
            score=0.87,
            eligible=False,
            ineligibility_reasons=("missing_capability:command",),
        )


def test_provider_score_rejects_an_unknown_exclusion_stage():
    with pytest.raises(ValueError, match="exclusion_stage"):
        ProviderScore(
            provider="codex",
            available=True,
            fit=1.0,
            remaining_percentage=90.0,
            freshness=Freshness.FRESH,
            usage_score=0.9,
            usage_confidence=1.0,
            quota_penalty=0.0,
            score=None,
            eligible=False,
            exclusion_stage="vibes",
        )


def test_eligibility_survives_serialization():
    result = Router(_files_only_config()).route(
        "implementation", _statuses(), required_capabilities={"command"}
    )
    payload = [candidate.to_dict() for candidate in result.providers]
    files_only = next(entry for entry in payload if entry["provider"] == FILES_ONLY)
    assert files_only["eligible"] is False
    assert files_only["ineligibility_reasons"] == ["restricted_capability:command"]
    assert files_only["exclusion_stage"] == "capability"
    assert files_only["preference_score"] is None
    assert files_only["required_capabilities"] == ["command", "read", "write"]


def test_required_capabilities_can_only_tighten_never_relax():
    """A request cannot drop a requirement the task type mandates."""
    result = Router(_files_only_config()).route("testing", _statuses(), required_capabilities={"read"})
    files_only = _by_provider(result)[FILES_ONLY]
    # `testing` still requires command despite the request naming only read.
    assert files_only.eligible is False
    assert "restricted_capability:command" in files_only.ineligibility_reasons


# --- the network-policy gate -----------------------------------


def test_network_policy_refusal_is_its_own_hard_gate_not_unavailability():
    """A refused provider is configured, reachable and entirely able.

    ModelQuad is simply not permitted to call its API from this process right
    now. Recording that as `provider_unavailable` would state a falsehood
    about the provider to explain a decision of ours -- the same conflation
    the capability stage exists to prevent.
    """
    refused = {"codex": "ModelQuad network policy refused this call"}
    result = Router(Config()).route("implementation", _statuses(), policy_excluded=refused)
    codex = _by_provider(result)["codex"]

    assert codex.eligible is False
    assert codex.ineligibility_reasons == ("network_policy_refused",)
    assert codex.exclusion_stage == "network_policy"
    # Neither the runtime's ability nor its availability is being questioned.
    assert codex.capable is True
    assert codex.available is True
    assert "network policy refused this call" in (codex.reason or "")


def test_a_policy_refused_candidate_carries_no_preference_score():
    result = Router(Config()).route(
        "implementation", _statuses(), policy_excluded={"codex": "refused"}
    )
    codex = _by_provider(result)["codex"]

    assert codex.preference_score is None
    assert codex.score is None
    assert result.recommendation != "codex"
    # Outcome classification must not read this as evidence about codex.
    assert codex.excluded_by_policy is True


def test_no_policy_exclusions_changes_nothing():
    """The gate is inert unless a surface hands it a refusal."""
    plain = Router(Config()).route("implementation", _statuses())
    empty = Router(Config()).route("implementation", _statuses(), policy_excluded={})

    assert plain.recommendation == empty.recommendation
    assert [c.to_dict() for c in plain.providers] == [c.to_dict() for c in empty.providers]


def test_policy_refusal_survives_serialization():
    result = Router(Config()).route(
        "implementation", _statuses(), policy_excluded={"claude": "refused"}
    )
    refused = next(
        entry
        for entry in (candidate.to_dict() for candidate in result.providers)
        if entry["provider"] == "claude"
    )

    assert refused["eligible"] is False
    assert refused["exclusion_stage"] == "network_policy"
    assert refused["ineligibility_reasons"] == ["network_policy_refused"]
    assert refused["preference_score"] is None


def test_every_candidate_refused_by_policy_fails_honestly():
    result = Router(Config()).route(
        "implementation",
        _statuses(),
        policy_excluded={name: "refused" for name in ("claude", "codex")},
    )

    assert result.recommendation is None
    assert "No eligible providers" in result.reason
    assert "network_policy_refused" in result.reason
