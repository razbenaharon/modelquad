# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""W07 regression fixtures: independent windows, clock corrections and pools.

These tests never read a real profile or
launch a provider; all provider files live under tmp_path and clocks are fixed.
"""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json

import pytest

from modelquad.catalog import ModelCatalog, ModelDescriptor
from modelquad.config import Config
from modelquad.models import Freshness, FreshnessThresholds
from modelquad.providers.base import freshness_for, observation_from
from modelquad.providers.claude import ClaudeProvider
from modelquad.providers.codex import CodexProvider
from modelquad.quota_policy import (
    QuotaRefusal, QuotaTier, apply_refusal_overlay, quota_evidence_for, refusal_in_force,
    stale_conserving_providers,
)
from modelquad.router import Router
from modelquad.usage.base import FUTURE_TIMESTAMP_TOLERANCE_SECONDS


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def _read(provider, tmp_path, payload):
    path = tmp_path / provider.filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return provider(path, now=lambda: NOW).get_status()


@pytest.mark.parametrize(
    "short,long,short_reset,expected,binding",
    [
        (0, 0, -10, QuotaTier.EXHAUSTED, "seven_day"),
        (20, 25, 300, QuotaTier.TIGHT, "seven_day"),
        (25, 25, 7200, QuotaTier.TIGHT, "five_hour"),
        ("unreadable", 25, 300, QuotaTier.TIGHT, "seven_day"),
    ],
    ids=["expired_5h_exhausted_week", "grace_5h_tight_week", "equal_percent", "unknown_window"],
)
def test_windows_keep_independent_constraints(tmp_path, short, long, short_reset, expected, binding):
    payload = {
        "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "five_hour_remaining_percentage": short,
        "seven_day_remaining_percentage": long,
        "five_hour_resets_at": (NOW + timedelta(seconds=short_reset)).isoformat(),
        "seven_day_resets_at": (NOW + timedelta(days=6)).isoformat(),
    }
    status = _read(CodexProvider, tmp_path, payload)
    evidence = quota_evidence_for(status, now=NOW)
    assert evidence.tier is expected
    assert evidence.window == binding
    assert len(evidence.windows) == 2
    weekly = next(w for w in evidence.windows if w.window == "seven_day")
    assert weekly.reset_in_seconds == 6 * 24 * 3600
    if short == "unreadable":
        assert evidence.windows[0].tier is QuotaTier.UNKNOWN
        assert evidence.to_dict()["windows"][0]["remaining"] is None
        # Unknown plus a healthy window cannot claim the whole quota is healthy.
        healthy = {**payload, "seven_day_remaining_percentage": 80}
        assert quota_evidence_for(_read(CodexProvider, tmp_path, healthy), now=NOW).tier is QuotaTier.UNKNOWN
    if short == 25:
        reversed_payload = dict(reversed(list(payload.items())))
        assert quota_evidence_for(_read(CodexProvider, tmp_path, reversed_payload), now=NOW) == evidence
    result = Router(Config()).route("review", [status], now=NOW)
    assert next(p for p in result.providers if p.provider == "codex").quota_tier == expected.value
    # The tier is shown on the candidate; it never refuses or moves the advice.
    assert result.recommendation == "codex"
    native = {**payload, "windows": [
        {"slot": "five_hour", "remaining_percentage": short,
         "resets_at": payload["five_hour_resets_at"]},
        {"slot": "seven_day", "remaining_percentage": long,
         "resets_at": payload["seven_day_resets_at"]},
    ]}
    # A native window whose reset deadline has passed no longer describes
    # the reported capacity, so the whole reading is unknown.
    native_expected = QuotaTier.UNKNOWN if short_reset <= 0 else expected
    assert quota_evidence_for(_read(CodexProvider, tmp_path, native), now=NOW).tier is native_expected


def test_clock_correction_cannot_clear_refusal(tmp_path):
    refusal = QuotaRefusal("codex", NOW - timedelta(minutes=1), window="five_hour")
    payload = {
        "five_hour_remaining_percentage": 80,
        "five_hour_resets_at": (NOW + timedelta(hours=2)).isoformat(),
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
    }
    poisoned = _read(CodexProvider, tmp_path, payload)
    assert poisoned.freshness is Freshness.UNKNOWN
    assert quota_evidence_for(poisoned, now=NOW).tier is QuotaTier.UNKNOWN
    assert refusal_in_force(refusal, poisoned, NOW)
    assert apply_refusal_overlay([poisoned], [refusal], NOW)[0].available is False
    corrected = _read(CodexProvider, tmp_path, {
        **payload, "observed_at": (NOW - timedelta(minutes=2)).isoformat(),
    })
    assert corrected.freshness is Freshness.FRESH
    assert refusal_in_force(refusal, corrected, NOW)
    # The exact shared tolerance is accepted; crossing it invalidates evidence.
    for offset, expected in [(FUTURE_TIMESTAMP_TOLERANCE_SECONDS, Freshness.FRESH),
                             (FUTURE_TIMESTAMP_TOLERANCE_SECONDS + 1, Freshness.UNKNOWN)]:
        assert freshness_for(NOW + timedelta(seconds=offset), FreshnessThresholds(), NOW) is expected


@pytest.mark.parametrize("case,value,known", [
    ("absent", None, True), ("null", None, False), ("empty", "", False),
    ("malformed", "not-a-time", False), ("valid", NOW.isoformat(), True),
], ids=["absent", "null", "empty", "malformed", "valid"])
def test_observation_presence_is_authoritative(tmp_path, case, value, known):
    payload = {"collected_at": NOW.isoformat(), "five_hour_remaining_percentage": 80}
    if case != "absent":
        payload["observed_at"] = value
    assert observation_from(payload) == (NOW if known else None)
    status = _read(CodexProvider, tmp_path, payload)
    assert status.observed_at == (NOW if known else None)
    assert status.freshness is (Freshness.FRESH if known else Freshness.UNKNOWN)
    evidence = quota_evidence_for(status, now=NOW)
    assert evidence.tier is (QuotaTier.ABUNDANT if known else QuotaTier.UNKNOWN)


def test_future_refusal_survives_clock_correction_and_yields_to_real_refusal(tmp_path):
    status = _read(CodexProvider, tmp_path, {
        "observed_at": NOW.isoformat(), "five_hour_remaining_percentage": 80,
    })
    future = QuotaRefusal("codex", NOW + timedelta(minutes=10), window="five_hour")
    assert refusal_in_force(future, status, NOW)
    (overlaid,) = apply_refusal_overlay([status], [future], NOW)
    assert not overlaid.available
    # The clock correction cannot turn the refusal into an absence of evidence.
    earlier = replace(status, observed_at=NOW - timedelta(minutes=2))
    real = QuotaRefusal("codex", NOW - timedelta(minutes=1), window="seven_day")
    for refusals in ([future, real], [real, future]):
        (overlaid,) = apply_refusal_overlay([earlier], refusals, NOW)
        assert not overlaid.available
        assert "seven_day" in overlaid.reason
        assert overlaid.metadata["quota_refusal_observed_at"] == real.observed_at.isoformat()
    assert not refusal_in_force(future, None, NOW + timedelta(minutes=14), ttl_seconds=180)


@pytest.mark.parametrize("weekly", [None, "unreadable"], ids=["not_reported", "malformed"])
def test_claude_single_window_with_all_six_aliases(tmp_path, weekly):
    status = _read(ClaudeProvider, tmp_path, {
        "observed_at": NOW.isoformat(),
        "five_hour_remaining_percentage": 80,
        "five_hour_used_percentage": 20,
        "five_hour_resets_at": (NOW + timedelta(hours=2)).isoformat(),
        "seven_day_remaining_percentage": weekly,
        "seven_day_used_percentage": None,
        "seven_day_resets_at": None,
    })
    evidence = quota_evidence_for(status, now=NOW)
    if weekly is None:
        assert evidence.tier is QuotaTier.ABUNDANT
        assert [w.window for w in evidence.windows] == ["five_hour"]
    else:
        assert evidence.tier is QuotaTier.UNKNOWN
        assert len(evidence.windows) == 2
        assert evidence.windows[1].tier is QuotaTier.UNKNOWN


@pytest.mark.parametrize("observation", [
    (NOW + timedelta(minutes=10)).isoformat(), None, "", "invalid",
], ids=["future", "null", "empty", "invalid"])
@pytest.mark.parametrize("window", ["five_hour", None], ids=["named", "binding"])
@pytest.mark.parametrize("reset_seconds", [-30, 7200], ids=["past_reset", "future_reset"])
def test_unusable_observation_reset_cannot_recover_or_extend_refusal(
    tmp_path, observation, window, reset_seconds,
):
    status = _read(CodexProvider, tmp_path, {
        "observed_at": observation,
        "five_hour_remaining_percentage": 80,
        "five_hour_resets_at": (NOW + timedelta(seconds=reset_seconds)).isoformat(),
    })
    refusal = QuotaRefusal("codex", NOW - timedelta(minutes=1), window=window)
    assert status.freshness is Freshness.UNKNOWN
    assert refusal_in_force(refusal, status, NOW, ttl_seconds=180)
    assert not refusal_in_force(refusal, status, NOW + timedelta(seconds=121), ttl_seconds=180)


@pytest.mark.parametrize("ranked", [False, True], ids=["no_model_named", "model_named_before_clamp"])
def test_incompatible_effort_note_requires_a_model_proposal(tmp_path, ranked):
    catalog = ModelCatalog(descriptors=(
        ModelDescriptor("codex", "high-only", "High only",
                        relative_strength=0.9 if ranked else None,
                        supported_reasoning_levels=("low", "medium", "high")),
    ))
    config = Config(model_policy={"codex": {"review": {"reasoning_effort": "xhigh"}}})
    status = _read(CodexProvider, tmp_path, {
        "observed_at": NOW.isoformat(), "five_hour_remaining_percentage": 80,
    })
    result = Router(config).route("review", [status], now=NOW, catalog=catalog)
    assert result.recommendation == "codex"
    assert result.target.model is None
    assert result.target.reasoning_effort == "xhigh"
    # The router never proposes a model from the catalog, so there is no
    # incompatibility to explain, ranked descriptor or not.
    assert result.reason is None


@pytest.mark.parametrize("freshness", list(Freshness))
def test_refusal_refresh_includes_unknown_freshness_and_stays_deduplicated(tmp_path, freshness):
    status = _read(CodexProvider, tmp_path, {"observed_at": None, "collected_at": NOW.isoformat()})
    status = replace(status, freshness=freshness)
    (overlaid,) = apply_refusal_overlay([status], [QuotaRefusal("codex", NOW)], NOW)
    expected = ("codex",) if freshness in (
        Freshness.STALE, Freshness.VERY_STALE, Freshness.UNKNOWN,
    ) else ()
    assert stale_conserving_providers([overlaid, overlaid], NOW) == expected
    assert stale_conserving_providers([status], NOW) == ()
    assert stale_conserving_providers([replace(status, available=False)], NOW) == ()
