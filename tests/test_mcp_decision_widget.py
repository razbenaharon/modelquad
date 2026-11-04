# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The conversational decision surface: what Claude sees vs what a person sees.

The presentation builders turn a canonical routing or delegation payload into
a short human-visible view that carries none of the internals (every
candidate's `fit`, `usage_score`, `quota_penalty`, a `decision_id`, `null`s),
while the canonical object itself is left untouched. The route tool these
views once served is not part of the release surface; the builders remain
live for delegation results.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from modelquad.mcp.presentation import (
    PRESENTATION_SCHEMA_VERSION,
    ROUTE_STATES,
    capability_view,
    capacity_view,
    execution_lifecycle,
    build_delegation_presentation,
    build_route_presentation,
    build_technical_details,
    execution_state_from_result,
    format_percent,
    render_presentation_text,
    route_lifecycle,
)


# The names that must never reach a user's conversation as visible text.
_INTERNALS = (
    "usage_score",
    "usage_confidence",
    "quota_penalty",
    "preference_score",
    "exclusion_stage",
    "resource_pool",
    "agent_runtime",
    "decision_source",
    "decision_id",
    "model_source",
    "ineligibility_reasons",
)


# ---------------------------------------------------------------- fixtures


def _decision(**overrides) -> dict:
    """A canonical routing payload, in the exact shape RoutingResult emits."""

    decision = {
        "task_type": "review",
        "recommendation": "zeta",
        "reason": "highest score",
        "decision_source": "deterministic",
        "decision_id": "dec-1",
        "stay_with_host": False,
        "target": {
            "provider": "zeta",
            "model": None,
            "reasoning_effort": "high",
            "model_source": "configured_prior",
            "resource_pool": None,
            "agent_runtime": None,
        },
        "expected_consumption": {"bucket": "low", "source": "host_supplied", "basis": "supplied by host"},
        "providers": [
            {
                "provider": "zeta",
                "available": True,
                "fit": 0.8,
                "remaining_percentage": 99.65999999999999,
                "freshness": "fresh",
                "usage_score": 0.9965999999999999,
                "usage_confidence": 0.5,
                "quota_penalty": 0.0,
                "score": 0.84,
                "reason": "session strategy: spend",
                "strategy": "spend",
                "strategy_adjustment": None,
                "capable": True,
                "missing_capabilities": [],
                "resource_pool": None,
                "agent_runtime": None,
                "eligible": True,
                "ineligibility_reasons": [],
                "exclusion_stage": None,
                "preference_score": 0.84,
                "required_capabilities": ["read"],
                "available_capabilities": ["read", "write"],
            },
            {
                "provider": "codex",
                "available": True,
                "fit": 0.8,
                "remaining_percentage": 99.0,
                "freshness": "stale",
                "usage_score": 0.745,
                "usage_confidence": 0.5,
                "quota_penalty": 0.0,
                "score": 0.78,
                "reason": None,
                "strategy": None,
                "strategy_adjustment": None,
                "capable": True,
                "missing_capabilities": [],
                "resource_pool": None,
                "agent_runtime": None,
                "eligible": True,
                "ineligibility_reasons": [],
                "exclusion_stage": None,
                "preference_score": 0.78,
                "required_capabilities": ["read"],
                "available_capabilities": ["read", "write", "command"],
            },
        ],
    }
    decision.update(overrides)
    return decision


# ---------------------------------- the human-visible text is concise


def test_the_visible_text_is_a_short_summary_with_no_internals():
    presentation = build_route_presentation(_decision(), invocation_mode="automatic")
    assert presentation["schema_version"] == PRESENTATION_SCHEMA_VERSION
    text = render_presentation_text(presentation)
    assert text.startswith("ModelQuad:")
    assert len(text.splitlines()) <= 5, text
    assert not text.lstrip().startswith("{")
    for internal in _INTERNALS:
        assert internal not in text, f"{internal} leaked into user-visible text"
    assert "0.9965999999999999" not in text
    assert "None" not in text
    assert "null" not in text


def test_percentages_are_rounded_for_people_and_left_alone_for_machines():
    assert format_percent(0.9965999999999999 * 100) == "99.66%"
    assert format_percent(99.7) == "99.7%"
    assert format_percent(36.0) == "36%"
    assert format_percent(0) == "0%"
    assert format_percent(None) is None
    assert format_percent("99") is None
    assert format_percent(True) is None


def test_a_stale_reading_is_never_presented_as_a_current_fact():
    decision = _decision()
    decision["providers"][0]["freshness"] = "stale"
    presentation = build_route_presentation(decision)

    capacity = next(r for r in presentation["reasons"] if r["kind"] == "capacity")
    assert capacity["text"] == "99.66% capacity at the last check (may be outdated)"
    assert "capacity available" not in capacity["text"]

    row = next(r for r in presentation["details"]["rows"] if r["label"] == "Capacity reading")
    assert row["value"] == "May be outdated"


def test_an_unknown_quota_reads_as_unknown_never_as_zero():
    decision = _decision()
    decision["providers"][0]["remaining_percentage"] = None
    decision["providers"][0]["freshness"] = "unknown"
    presentation = build_route_presentation(decision)

    capacity = next(r for r in presentation["reasons"] if r["kind"] == "capacity")
    assert capacity["text"] == "Remaining capacity is not reported by this provider"
    assert capacity["tone"] == "muted"
    assert "0%" not in render_presentation_text(presentation)

    alternative = presentation["details"]["alternatives"][0]
    decision["providers"][1]["remaining_percentage"] = None
    assert build_route_presentation(decision)["details"]["alternatives"][0]["capacity"] == "Capacity unknown"
    assert alternative["capacity_known"] is True


def test_null_internal_fields_never_appear_as_user_facing_values():
    presentation = build_route_presentation(_decision())
    rendered = json.dumps(
        {key: value for key, value in presentation.items() if key != "technical"},
        ensure_ascii=False,
    )
    for forbidden in ("resource_pool", "agent_runtime", "exclusion_stage", "usage_score"):
        assert forbidden not in rendered, f"{forbidden} reached the presentation"
    # A model that was never chosen is stated as a fact about the runtime, not
    # rendered as `model: null`.
    assert presentation["selection"]["model"] == {
        "known": False,
        "label": "Chosen by Zeta",
        "value": None,
    }
    assert "Model" not in [fact["label"] for fact in presentation["facts"]]
    assert {"label": "Model", "value": "Chosen by Zeta"} in presentation["details"]["rows"]


def test_a_named_model_is_shown_as_a_primary_fact():
    decision = _decision()
    decision["target"]["model"] = "zeta-pro-1"
    presentation = build_route_presentation(decision)
    assert presentation["selection"]["model"]["known"] is True
    assert {"key": "model", "label": "Model", "value": "zeta-pro-1"} in presentation["facts"]


def test_expected_consumption_and_reasoning_effort_are_never_conflated():
    presentation = build_route_presentation(_decision())
    facts = {fact["label"]: fact["value"] for fact in presentation["facts"]}
    assert facts["Expected task usage"] == "Low"
    assert facts["Reasoning level"] == "High"


# --------------------------------------------------- 3. lifecycle honesty


def test_a_route_recommendation_is_never_labelled_as_a_completed_delegation():
    presentation = build_route_presentation(_decision())
    assert presentation["lifecycle"]["state"] == "selected"
    assert presentation["lifecycle"]["executed"] is False
    # The headline names the actor and the choice; the lifecycle summary
    # beside it is what says nothing has run.
    assert presentation["headline"] == "ModelQuad chose Zeta"
    assert presentation["lifecycle"]["summary"] == "Not started yet"
    assert presentation["lifecycle"]["progress"] == "not_started"

    text = render_presentation_text(presentation)
    for forbidden in ("Delegated", "Completed", "Running"):
        assert forbidden not in text


@pytest.mark.parametrize("state", ["completed", "running", "delegating", "failed"])
def test_the_route_lifecycle_refuses_execution_vocabulary(state):
    with pytest.raises(ValueError):
        route_lifecycle(state)


def test_stay_with_host_renders_as_the_task_staying_put():
    decision = _decision(recommendation="claude", stay_with_host=True)
    decision["providers"][0]["provider"] = "claude"
    presentation = build_route_presentation(decision, host_name="claude-code", host_runtime="claude_code")

    assert presentation["lifecycle"]["state"] == "stayed_with_host"
    assert presentation["flow"]["stay_with_host"] is True
    assert presentation["headline"] == "ModelQuad kept this task in Claude Code"
    nodes = presentation["flow"]["nodes"]
    assert [node["role"] for node in nodes] == ["host", "hub", "target"]
    # Not the same label twice: "Claude Code -> ModelQuad -> Claude Code"
    # reads as a round trip through a worker.
    assert nodes[0]["label"] == "Claude Code"
    assert nodes[2]["label"] == "Stay with Claude Code"
    assert {"label": "Where the task runs", "value": "Stayed with Claude"} in presentation["details"]["rows"]


def test_no_eligible_provider_is_blocked_not_selected():
    presentation = build_route_presentation(_decision(recommendation=None, target=None))
    assert presentation["lifecycle"]["state"] == "blocked"
    assert presentation["headline"] == "ModelQuad found no provider for this task"
    assert presentation["lifecycle"]["executed"] is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"task_status": "completed"}, "completed"),
        ({"task_status": "partial"}, "partial"),
        ({"task_status": "blocked"}, "blocked"),
        ({"task_status": "failed"}, "failed"),
        # A timeout overrides whatever the worker last reported.
        ({"task_status": "completed", "metadata": {"timed_out": True}}, "timeout"),
        # No completion envelope is not success and not a crash.
        ({"task_status": "unknown"}, "blocked"),
        ({"delegated": False, "refused": True}, "refused"),
    ),
)
def test_execution_state_comes_from_objective_backend_state(payload, expected):
    assert execution_state_from_result(payload) == expected


def test_a_delegation_widget_reports_the_execution_not_the_recommendation():
    presentation = build_delegation_presentation(
        {"provider": "zeta", "task_status": "completed", "decision_id": "dec-1", "evidence": {"verified": True}},
        task_type="review",
        host_name="claude-code",
        host_runtime="claude_code",
    )
    assert presentation["surface"] == "delegation"
    assert presentation["lifecycle"]["state"] == "completed"
    assert presentation["lifecycle"]["executed"] is True
    assert presentation["headline"] == "Zeta finished this task"
    assert {"key": "evidence", "label": "Result", "value": "Checked by ModelQuad"} in presentation["facts"]


def test_an_unverified_completion_is_labelled_as_the_workers_own_claim():
    presentation = build_delegation_presentation(
        {"provider": "codex", "task_status": "completed", "evidence": {"verified": False}},
        task_type="testing",
    )
    assert {"key": "evidence", "label": "Result", "value": "Codex's own report (not checked by ModelQuad)"} in presentation["facts"]


# --------------------------------------------------- 4. eligibility vs rank


def test_an_ineligible_candidate_is_disqualified_not_ranked_lower():
    decision = _decision()
    decision["providers"][1].update(
        {
            "eligible": False,
            "score": None,
            "preference_score": None,
            "capable": False,
            "missing_capabilities": ["command"],
            "exclusion_stage": "capability",
            "ineligibility_reasons": ["missing capability: command"],
        }
    )
    alternative = build_route_presentation(decision)["details"]["alternatives"][0]
    assert alternative["eligible"] is False
    assert alternative["status"] == "Cannot run this task"
    # A legacy record carries WHICH capability excluded the candidate but not
    # WHY. The note now says exactly that much: the old "Cannot run commands"
    # asserted an inability this record cannot support, and for the provider
    # that phrasing was actually shown for, it was false.
    assert alternative["note"] == "Not able to run commands for this task"


# ------------------------------------------------ 5. developer disclosure


def test_developer_details_are_absent_by_default(monkeypatch):
    from modelquad.devmode import DEV_MODE_ENV

    # Explicit rather than ambient: the developer-mode environment variable
    # can be set in the live process, so "the suite happens not to have it set" is not the
    # property being asserted.
    monkeypatch.delenv(DEV_MODE_ENV, raising=False)
    assert "technical" not in build_route_presentation(_decision())


def test_developer_details_match_the_canonical_decision_object():
    decision = _decision()
    presentation = build_route_presentation(decision, developer_mode=True)
    technical = presentation["technical"]

    rows = {row["label"]: row["value"] for row in technical["rows"]}
    assert rows["decision_id"] == decision["decision_id"]
    assert rows["decision_source"] == decision["decision_source"]
    assert rows["recommendation"] == decision["recommendation"]
    assert rows["target.model"] is None
    assert rows["target.model_source"] == "configured_prior"
    # The candidates are the canonical objects, at full precision -- in the
    # developer view the raw value IS the product.
    assert technical["candidates"] == decision["providers"]
    assert technical["candidates"][0]["usage_score"] == 0.9965999999999999


def test_build_technical_details_is_a_view_never_a_copy():
    decision = _decision()
    build_technical_details(decision)
    assert decision == _decision(), "the technical view mutated the canonical object"


# ----------------------------------------------- 7. the invocation is light


def test_an_automatic_invocation_is_marked_as_such_and_an_unreported_one_is_not():
    automatic = build_route_presentation(_decision(), invocation_mode="automatic")["invocation"]
    assert automatic == {"mode": "automatic", "label": "Auto", "automatic": True, "reported": True}

    unknown = build_route_presentation(_decision(), invocation_mode=None)["invocation"]
    assert unknown["reported"] is False
    assert unknown["label"] == ""


def test_the_automatic_route_summary_stays_short_enough_for_a_conversation():
    text = render_presentation_text(build_route_presentation(_decision(), invocation_mode="automatic"))
    assert text.count("\n") <= 3
    assert len(text) < 400, text


# ------------------------------------------- 6. lineage, capacity, capability


def test_the_origin_is_the_host_and_the_hub_is_never_named_twice():
    presentation = build_route_presentation(
        _decision(), host_name="claude-code", host_runtime="claude_code"
    )
    labels = [node["label"] for node in presentation["flow"]["nodes"]]
    assert labels == ["Claude Code", "ModelQuad", "Zeta"]
    assert labels.count("ModelQuad") == 1


@pytest.mark.parametrize(
    "client_name",
    ["ModelQuad", "modelquad", "Local Agent Mode Modelquad", "model-quad mcp"],
)
def test_a_client_that_calls_itself_modelquad_is_not_rendered_as_the_origin(client_name):
    """The observed defect: "Local Agent Mode Modelquad -> ModelQuad -> <worker>".

    ModelQuad is the hub. A client whose self-reported name is some spelling of
    the product is not evidence of where the task came from, so the origin node
    falls back to the neutral label rather than naming the hub twice.
    """

    presentation = build_route_presentation(_decision(), host_name=client_name)
    host = presentation["flow"]["nodes"][0]
    assert host["label"] == "Current host"
    assert host["known"] is False
    assert host["key"] is None
    labels = [node["label"] for node in presentation["flow"]["nodes"]]
    assert labels.count("ModelQuad") == 1


def test_an_unknown_host_is_neutral_and_is_never_named_claude_code():
    presentation = build_route_presentation(_decision())
    host = presentation["flow"]["nodes"][0]
    assert host == {"role": "host", "key": None, "label": "Current host", "known": False}
    assert "Claude" not in host["label"]


def test_an_unobserved_runtime_still_uses_the_client_name_when_it_names_a_host():
    presentation = build_route_presentation(_decision(), host_name="Claude Code")
    host = presentation["flow"]["nodes"][0]
    assert host["label"] == "Claude Code"
    assert host["known"] is True


def test_a_stay_names_the_outcome_rather_than_repeating_the_host():
    decision = _decision(recommendation="claude", stay_with_host=True)
    decision["providers"][0]["provider"] = "claude"
    presentation = build_route_presentation(decision, host_runtime="claude_code")
    nodes = presentation["flow"]["nodes"]
    assert nodes[2]["label"] == "Stay with Claude Code"
    assert nodes[0]["label"] != nodes[2]["label"]


def test_a_stay_from_an_unidentified_host_says_stay_here():
    decision = _decision(recommendation="claude", stay_with_host=True)
    decision["providers"][0]["provider"] = "claude"
    presentation = build_route_presentation(decision)
    assert presentation["flow"]["nodes"][2]["label"] == "Stay here"
    assert presentation["headline"] == "ModelQuad kept this task here"


def test_capacity_separates_the_percentage_from_how_old_it_is():
    capacity = build_route_presentation(_decision())["capacity"]
    assert capacity["percent"] == "99.66%"
    assert capacity["label"] == "99.66%"
    assert capacity["known"] is True
    assert capacity["freshness"] == "fresh"
    assert capacity["freshness_label"] == "Fresh"
    assert capacity["freshness_tone"] == "good"
    # The number says nothing about age and the category says nothing about
    # the number: two facts, two fields.
    assert "check" not in capacity["label"].lower()
    assert "%" not in capacity["freshness_label"]


@pytest.mark.parametrize(
    ("freshness", "label", "tone"),
    (
        ("fresh", "Fresh", "good"),
        ("stale", "Stale", "muted"),
        ("very_stale", "Very stale", "warning"),
        ("unknown", "Unknown", "muted"),
        # Anything outside the vocabulary is UNKNOWN, never promoted.
        ("something_new", "Unknown", "muted"),
        (None, "Unknown", "muted"),
    ),
)
def test_every_freshness_value_has_one_category(freshness, label, tone):
    view = capacity_view({"remaining_percentage": 50.0, "freshness": freshness})
    assert view["freshness_label"] == label
    assert view["freshness_tone"] == tone
    # A stale or unknown reading is still a reading -- never rewritten to
    # "unavailable".
    assert view["known"] is True
    assert view["percent"] == "50%"


def test_an_unreported_capacity_is_not_reported_never_zero():
    view = capacity_view({"remaining_percentage": None, "freshness": "unknown"})
    assert view["known"] is False
    assert view["percent"] is None
    assert view["label"] == "Not reported"
    assert "0" not in view["label"]


def test_an_age_is_derived_only_when_a_timestamp_supports_it():
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    fresh = capacity_view(
        {"remaining_percentage": 50, "freshness": "fresh", "observed_at": "2026-08-31T11:58:00+00:00"},
        now=now,
    )
    assert fresh["age"] == "2 minutes ago"
    assert fresh["freshness_detail"] == "Checked 2 minutes ago"

    # No timestamp: the categorical answer is the whole answer, and no age is
    # manufactured to fill the space.
    without = capacity_view({"remaining_percentage": 50, "freshness": "fresh"}, now=now)
    assert without["age"] is None
    assert without["freshness_detail"] == "Checked just now"


@pytest.mark.parametrize(
    ("observed_at", "expected"),
    (
        ("2026-08-31T11:59:40+00:00", "just now"),
        ("2026-08-31T11:00:00+00:00", "1 hour ago"),
        ("2026-08-29T12:00:00+00:00", "2 days ago"),
        ("2026-08-10T12:00:00+00:00", "3 weeks ago"),
        ("2026-08-31T11:58:00Z", "2 minutes ago"),
        # Unparseable, absent, and future readings say nothing at all.
        ("not a timestamp", None),
        (None, None),
        ("2026-08-31T13:00:00+00:00", None),
    ),
)
def test_the_age_label_never_guesses(observed_at, expected):
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    view = capacity_view({"remaining_percentage": 1, "observed_at": observed_at}, now=now)
    assert view["age"] == expected


def test_a_routing_payload_carries_no_timestamp_so_freshness_stays_categorical():
    """The honest consequence of the current contract, asserted rather than hidden.

    ``ProviderScore`` does not record ``observed_at`` (widening it would widen
    the telemetry egress allowlist, which is not this surface's decision to
    make). The widget therefore shows the category, and the moment the field
    does arrive the age appears with no frontend change.
    """

    assert build_route_presentation(_decision())["capacity"]["age"] is None


def test_capabilities_report_only_what_the_payload_recorded():
    decision = _decision()
    decision["providers"][0]["available_capabilities"] = ["read"]
    capabilities = build_route_presentation(decision)["capabilities"]

    assert capabilities["known"] is True
    assert capabilities["summary"] == "Allowed here: read files"
    assert [item["key"] for item in capabilities["allowed"]] == ["read"]
    # Knowing only that it may read must never become a claim about editing
    # or running anything.
    assert "change files" not in capabilities["summary"]
    assert "run commands" not in capabilities["summary"]


def test_an_implementation_capability_set_is_named_in_full():
    decision = _decision()
    decision["providers"][0]["available_capabilities"] = ["read", "write", "command"]
    capabilities = build_route_presentation(decision)["capabilities"]
    assert capabilities["summary"] == "Allowed here: read files, change files and run commands"
    assert [item["label"] for item in capabilities["required"]] == ["read files"]


def test_missing_capability_details_stay_missing_rather_than_becoming_a_denial():
    decision = _decision()
    decision["providers"][0].pop("available_capabilities")
    capabilities = build_route_presentation(decision)["capabilities"]
    assert capabilities == {
        "known": False,
        "reported": False,
        "allowed": [],
        "required": [{"key": "read", "label": "read files"}],
        "blocked": [],
        "summary": "Permissions were not reported",
    }


def test_an_empty_capability_profile_is_unconfigured_not_incapable():
    decision = _decision()
    decision["providers"][0]["available_capabilities"] = []
    capabilities = build_route_presentation(decision)["capabilities"]
    assert capabilities["reported"] is True
    assert capabilities["known"] is False
    assert capabilities["summary"] == "No permissions are configured for this agent"
    assert "Cannot" not in capabilities["summary"]


def test_a_capability_gap_keeps_policy_and_runtime_apart():
    view = capability_view(
        {
            "available_capabilities": ["read", "write"],
            "required_capabilities": ["command"],
            "capability_gaps": [
                {
                    "capability": "command",
                    "state": "restricted",
                    "is_policy_restriction": True,
                }
            ],
        }
    )
    assert view["summary"] == "ModelQuad does not allow this agent to run commands here"
    assert view["blocked"] == [
        {"key": "command", "label": "run commands", "state": "restricted", "by_policy": True}
    ]


# ------------------------------------------------- 7. wording and lifecycle


def test_internal_resource_strategy_wording_never_reaches_the_card():
    presentation = build_route_presentation(_decision())
    rendered = json.dumps(presentation, ensure_ascii=False)
    assert "spend" not in rendered.lower()

    strategy = next(r for r in presentation["reasons"] if r["kind"] == "strategy")
    assert strategy["text"] == "Your settings prefer Zeta for work like this"
    assert {"label": "Your preference", "value": "Use available capacity"} in presentation[
        "details"
    ]["rows"]


def test_the_preserve_strategy_is_stated_as_the_users_own_preference():
    decision = _decision()
    decision["providers"][0]["strategy"] = "preserve"
    presentation = build_route_presentation(decision)
    strategy = next(r for r in presentation["reasons"] if r["kind"] == "strategy")
    assert strategy["text"] == "Your settings prefer to save the capacity you have left"
    assert {"label": "Your preference", "value": "Save remaining capacity"} in presentation[
        "details"
    ]["rows"]


@pytest.mark.parametrize("state", sorted(ROUTE_STATES))
def test_no_route_state_ever_claims_progress(state):
    lifecycle = route_lifecycle(state)
    assert lifecycle["executed"] is False
    assert lifecycle["progress"] == "not_started"
    assert lifecycle["summary"]
    assert "Running" not in lifecycle["summary"]


@pytest.mark.parametrize(
    ("state", "progress", "executed"),
    (
        ("delegating", "in_progress", False),
        ("running", "in_progress", True),
        ("completed", "finished", True),
        ("failed", "finished", True),
        ("refused", "not_started", False),
    ),
)
def test_the_execution_lifecycle_states_how_far_the_work_actually_got(state, progress, executed):
    lifecycle = execution_lifecycle(state)
    assert lifecycle["progress"] == progress
    assert lifecycle["executed"] is executed
    assert lifecycle["summary"]
    assert lifecycle["note"]


def test_delegating_is_not_executed_because_no_worker_is_confirmed():
    assert execution_lifecycle("delegating")["executed"] is False
    assert execution_lifecycle("running")["executed"] is True


def test_blocked_means_two_different_things_and_is_never_conflated():
    assert route_lifecycle("blocked")["progress"] == "not_started"
    assert route_lifecycle("blocked")["summary"] == "Nothing to run"
    assert execution_lifecycle("blocked")["progress"] == "finished"
    assert execution_lifecycle("blocked")["summary"] == "Could not proceed"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_the_reasoning_level_stays_visible_for_every_supported_value(effort):
    decision = _decision()
    decision["target"]["reasoning_effort"] = effort
    presentation = build_route_presentation(decision)
    facts = {fact["key"]: fact["value"] for fact in presentation["facts"]}
    assert facts["reasoning_effort"] == effort.capitalize()
    # A level with no recorded rationale shows the level and stops.
    assert "Reasoning level" not in [row["label"] for row in presentation["details"]["rows"]]


def test_a_recorded_reasoning_rationale_is_shown_and_an_absent_one_is_not():
    decision = _decision()
    decision["target"]["reasoning_effort_reason"] = "This review needs careful reading"
    rows = build_route_presentation(decision)["details"]["rows"]
    assert {
        "label": "Reasoning level",
        "value": "This review needs careful reading",
    } in rows


def test_the_disclosure_still_covers_every_question_it_is_supposed_to():
    rows = {row["label"] for row in build_route_presentation(_decision())["details"]["rows"]}
    assert {
        "How this was decided",
        "Where the task runs",
        "This task needs to",
        "What this agent may do",
        "Capacity left",
        "Capacity reading",
        "Your preference",
        "Handoff",
    } <= rows


def test_the_adapter_says_which_reasons_the_compact_card_shows():
    """The card must not answer the same question twice.

    Capacity and capability have their own elements on the widget, so their
    sentences are marked non-primary -- but they stay in ``reasons`` because
    the plain-text fallback has no such elements and would otherwise lose the
    fact entirely.
    """

    presentation = build_route_presentation(_decision())
    primary = {reason["kind"] for reason in presentation["reasons"] if reason["primary"]}
    secondary = {reason["kind"] for reason in presentation["reasons"] if not reason["primary"]}

    assert primary == {"fit", "strategy"}
    assert secondary == {"capacity", "capability"}
    # The fallback still carries every one of them.
    text = render_presentation_text(presentation)
    for reason in presentation["reasons"]:
        assert reason["text"] in text
