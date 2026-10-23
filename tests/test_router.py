# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from orchestrator.config import Config
from orchestrator.models import Freshness, ProviderStatus
from orchestrator.providers import CodexProvider
from orchestrator.router import Router


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def status(name: str, remaining: float | None, *, available: bool = True, freshness=Freshness.FRESH, reason=None):
    return ProviderStatus(
        name=name,
        available=available,
        remaining_percentage=remaining,
        observed_at=NOW if freshness is not Freshness.UNKNOWN else None,
        freshness=freshness,
        reason=reason,
    )


def scores_by_provider(result):
    return {score.provider: score for score in result.providers}


def _codex_first_review_fit() -> dict[str, dict[str, float]]:
    """The packaged fit matrix with Codex ahead on review.

    Codex runs read-only, so it can only win a read-only task type; the
    packaged matrix puts Claude ahead on every one of them.
    """

    fit = {task: dict(row) for task, row in Config().task_fit.items()}
    fit["review"] = {"claude": 0.80, "codex": 1.00}
    return fit


def test_route_target_is_none_without_model_policy():
    """Backward compat: a Config with no model_policy attaches no RouteTarget."""

    router = Router(Config(task_fit=_codex_first_review_fit()))
    result = router.route(
        "review",
        [status("claude", 80), status("codex", 90)],
    )
    assert result.target is None
    assert result.recommendation == "codex"


def test_route_target_attaches_configured_prior_for_winning_provider():
    config = Config(
        task_fit=_codex_first_review_fit(),
        model_policy={
            "codex": {"review": {"model": None, "reasoning_effort": "medium"}},
        },
    )
    result = Router(config).route(
        "review",
        [status("claude", 80), status("codex", 90)],
    )
    assert result.recommendation == "codex"
    assert result.target is not None
    assert result.target.provider == "codex"
    assert result.target.model is None
    assert result.target.reasoning_effort == "medium"
    assert result.target.model_source == "configured_prior"


def test_route_target_absent_for_task_type_not_in_policy():
    config = Config(
        task_fit=_codex_first_review_fit(),
        model_policy={"codex": {"debugging": {"reasoning_effort": "high"}}},
    )
    result = Router(config).route(
        "review",
        [status("claude", 80), status("codex", 90)],
    )
    assert result.recommendation == "codex"
    assert result.target is None


def test_low_quota_never_lowers_the_score():
    task_fit = {
        task: {"claude": 0.9, "codex": 0.9}
        for task in Config().task_fit
    }
    router = Router(Config(task_fit=task_fit))

    result = router.route(
        "architecture",
        [status("claude", 1), status("codex", 50)],
    )
    scores = scores_by_provider(result)

    # Quota is display-only: 1% left changes neither the score nor its parts.
    assert scores["claude"].quota_penalty is None
    assert scores["claude"].score == scores["codex"].score == pytest.approx(0.9)


def test_source_reported_unavailable_provider_is_excluded_from_recommendation(tmp_path):
    usage_file = tmp_path / "usage-codex.json"
    usage_file.write_text(
        json.dumps({"available": False, "five_hour_remaining_percentage": 90, "observed_at": NOW.isoformat()}),
        encoding="utf-8",
    )
    codex_status = CodexProvider(usage_file, now=lambda: NOW).get_status()
    result = Router(Config()).route(
        "implementation",
        [
            status("claude", 80),
            codex_status,
        ],
    )
    codex = scores_by_provider(result)["codex"]

    assert codex.score is None
    assert codex_status.available is False
    assert "unavailable" in (codex.reason or "").lower()
    assert result.recommendation != "codex"


@pytest.mark.parametrize(
    ("task_type", "expected_provider"),
    [
        ("architecture", "claude"),
        ("review", "claude"),
        ("exploration", "claude"),
    ],
)
def test_recommendations_follow_task_fit_alone(task_type, expected_provider):
    config = Config()
    result = Router(config).route(
        task_type,
        [status("claude", 60), status("codex", 60)],
    )
    scores = scores_by_provider(result)
    expected_score = config.task_fit[task_type][expected_provider]

    assert scores[expected_provider].score == pytest.approx(expected_score)
    assert result.recommendation == expected_provider


def test_unknown_usage_is_not_zero_and_changes_nothing():
    result = Router(Config()).route(
        "architecture",
        [status("claude", None, freshness=Freshness.UNKNOWN), status("codex", 50)],
    )

    claude = scores_by_provider(result)["claude"]
    assert claude.remaining_percentage is None
    assert claude.usage_score is None and claude.quota_penalty is None
    assert claude.score == pytest.approx(Config().task_fit["architecture"]["claude"])


def test_stale_usage_changes_nothing():
    result = Router(Config()).route(
        "architecture",
        [status("claude", 20, freshness=Freshness.STALE), status("codex", 50)],
    )

    claude = scores_by_provider(result)["claude"]
    assert claude.usage_score is None and claude.usage_confidence is None
    assert claude.score == pytest.approx(Config().task_fit["architecture"]["claude"])


def test_very_stale_usage_is_treated_as_unknown_not_low():
    # Even though raw remaining is very low (2%), VERY_STALE data must not be
    # trusted enough to apply a quota penalty or drag the score down as if it
    # were a confirmed near-exhaustion reading.
    result = Router(Config()).route(
        "architecture",
        [status("claude", 2, freshness=Freshness.VERY_STALE), status("codex", 50)],
    )

    claude = scores_by_provider(result)["claude"]
    assert claude.usage_score is None and claude.quota_penalty is None
    assert claude.score == pytest.approx(Config().task_fit["architecture"]["claude"])


def test_available_provider_with_unknown_quota_stays_a_candidate():
    # Case E: a provider that is available but whose remaining quota is unknown
    # (e.g. a transient empty usage snapshot) must NOT be dropped from the
    # candidate set solely because its quota telemetry is unknown. It receives
    # neutral usage treatment and remains eligible to win.
    result = Router(Config(task_fit=_codex_first_review_fit())).route(
        "review",
        [
            status("codex", None, freshness=Freshness.VERY_STALE),
            status("claude", 50),
        ],
    )
    scores = scores_by_provider(result)
    codex = scores["codex"]

    assert codex.available is True
    assert codex.remaining_percentage is None
    assert codex.score is not None  # scored => still a routing candidate
    assert codex.usage_score is None and codex.quota_penalty is None
    # review fits codex best here, so it wins even with unknown quota.
    assert result.recommendation == "codex"


def test_resource_strategy_unconfigured_leaves_score_and_fields_untouched():
    config = Config()
    result = Router(config).route(
        "architecture",
        [status("claude", 60), status("codex", 60)],
    )
    scores = scores_by_provider(result)
    claude = scores["claude"]
    assert claude.score == pytest.approx(1.00)
    assert claude.strategy is None
    assert claude.strategy_adjustment is None
    assert claude.reason is None
