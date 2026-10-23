# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Exact model identity, runtime version, and role (V8 slice 5, P3-P7).

Six axes that must never collapse: runtime, runtime version, model family,
exact model id, provenance, and role. Collapsing any of them destroys the
ability to attribute a performance change to the right cause.
"""

from __future__ import annotations

import pytest

from modelquad.models import (
    ModelIdentitySource,
    RuntimeModelIdentity,
    RuntimeRole,
    model_family_from_exact_id,
)


# --- exact version vs family ------------------------------------------------


def test_exact_versions_of_the_same_family_stay_distinct():
    """Opus 4.8 and Opus 5 must never collapse into 'Claude / Opus'."""
    old = RuntimeModelIdentity(model_exact_id="claude-opus-4-5", model_source="reported")
    new = RuntimeModelIdentity(model_exact_id="claude-opus-5", model_source="reported")
    assert old.model_family == new.model_family == "opus"
    assert old.model_exact_id != new.model_exact_id
    assert old.to_dict() != new.to_dict()


def test_family_is_derived_without_upgrading_provenance():
    """Parsing a string is not runtime confirmation."""
    identity = RuntimeModelIdentity(model_exact_id="claude-opus-5", model_source="requested")
    assert identity.model_family == "opus"
    assert identity.model_source is ModelIdentitySource.REQUESTED
    assert identity.is_confirmed is False


@pytest.mark.parametrize(
    "exact_id,family",
    [
        ("claude-opus-5", "opus"),
        ("claude-sonnet-5", "sonnet"),
        ("gpt-5-codex", "gpt"),
        ("gemini-3.7-flash-high", "gemini"),
    ],
)
def test_known_families_are_recognized(exact_id, family):
    assert model_family_from_exact_id(exact_id) == family


def test_unrecognized_model_id_yields_no_guessed_family():
    assert model_family_from_exact_id("some-future-model-9") is None
    assert model_family_from_exact_id(None) is None
    assert model_family_from_exact_id("") is None


# --- provenance discipline --------------------------------------------------


@pytest.mark.parametrize("source", ["requested", "reported", "configured", "unknown"])
def test_nothing_but_confirmed_counts_as_confirmed(source):
    identity = RuntimeModelIdentity(model_exact_id="claude-opus-5", model_source=source)
    assert identity.is_confirmed is False


def test_confirmed_requires_an_exact_id():
    """A confirmed family with no exact id is not a confirmed identity."""
    identity = RuntimeModelIdentity(model_family="opus", model_source="confirmed")
    assert identity.is_confirmed is False


def test_confirmed_with_an_exact_id_is_confirmed():
    identity = RuntimeModelIdentity(model_exact_id="claude-opus-5", model_source="confirmed")
    assert identity.is_confirmed is True


def test_provenance_cannot_be_claimed_without_any_model_information():
    with pytest.raises(ValueError, match="carries no model information"):
        RuntimeModelIdentity(runtime="codex", model_source="reported")


def test_an_unrecognized_provenance_degrades_to_unknown():
    identity = RuntimeModelIdentity(model_exact_id="x-1", model_source="vibes")
    assert identity.model_source is ModelIdentitySource.UNKNOWN


# --- runtime version vs model version ---------------------------------------


def test_runtime_version_is_separate_from_model_version():
    """The two drift axes must be independently observable."""
    model_change = (
        RuntimeModelIdentity(
            runtime="claude_code", runtime_version="2.1.0",
            model_exact_id="claude-opus-4-5", model_source="reported",
        ),
        RuntimeModelIdentity(
            runtime="claude_code", runtime_version="2.1.0",
            model_exact_id="claude-opus-5", model_source="reported",
        ),
    )
    # Same harness, different model: isolates a MODEL change.
    assert model_change[0].runtime_version == model_change[1].runtime_version
    assert model_change[0].model_exact_id != model_change[1].model_exact_id

    harness_change = (
        RuntimeModelIdentity(
            runtime="claude_code", runtime_version="2.1.0",
            model_exact_id="claude-opus-5", model_source="reported",
        ),
        RuntimeModelIdentity(
            runtime="claude_code", runtime_version="2.2.0",
            model_exact_id="claude-opus-5", model_source="reported",
        ),
    )
    # Same model, different harness: isolates a RUNTIME change.
    assert harness_change[0].model_exact_id == harness_change[1].model_exact_id
    assert harness_change[0].runtime_version != harness_change[1].runtime_version


def test_runtime_version_is_not_written_into_any_model_field():
    identity = RuntimeModelIdentity(
        runtime="codex", runtime_version="codex-cli 0.147.0",
        model_exact_id="gpt-5-codex", model_source="requested",
    )
    payload = identity.to_dict()
    assert payload["runtime_version"] == "codex-cli 0.147.0"
    assert "0.147.0" not in (payload["model_exact_id"] or "")
    assert payload["model_family"] == "gpt"


# --- roles ------------------------------------------------------------------


def test_the_same_runtime_can_hold_either_role():
    """Role is not implied by runtime identity."""
    as_host = RuntimeModelIdentity(runtime="codex", role=RuntimeRole.CURRENT_HOST)
    as_worker = RuntimeModelIdentity(runtime="codex", role=RuntimeRole.DELEGATED_WORKER)
    assert as_host.runtime == as_worker.runtime
    assert as_host.role is not as_worker.role


def test_claude_is_representable_as_a_delegated_worker():
    """The current lack of a Claude executor is an implementation limit, not
    a schema assumption -- the data model must already express it."""
    identity = RuntimeModelIdentity(
        runtime="claude_code",
        model_exact_id="claude-opus-5",
        model_source="requested",
        role=RuntimeRole.DELEGATED_WORKER,
    )
    assert identity.role is RuntimeRole.DELEGATED_WORKER
    assert identity.to_dict()["role"] == "delegated_worker"


def test_unrecognized_role_degrades_to_unknown():
    assert RuntimeModelIdentity(runtime="x", role="sidekick").role is RuntimeRole.UNKNOWN


def test_round_trip_preserves_every_axis():
    identity = RuntimeModelIdentity(
        runtime="zeta",
        runtime_version="1.1.21",
        model_provider="zeta-labs",
        model_exact_id="zeta-3.7-flash-high",
        model_display_name="Zeta 3.7 Flash (High)",
        model_source="requested",
        role=RuntimeRole.DELEGATED_WORKER,
        observed_at="2026-08-26T00:00:00+00:00",
    )
    restored = RuntimeModelIdentity.from_dict(identity.to_dict())
    assert restored == identity
