# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from orchestrator.models import Freshness
from orchestrator.config import Config, ProviderConfig
from orchestrator.providers import ClaudeProvider, CodexProvider, build_providers


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def write_snapshot(tmp_path, filename: str, payload: object):
    path = tmp_path / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_claude_snapshot_uses_more_constrained_quota(tmp_path):
    path = write_snapshot(
        tmp_path,
        "usage-claude.json",
        {
            "five_hour_used_percentage": 20,
            "seven_day_used_percentage": 65,
            "collected_at": NOW.isoformat(),
        },
    )

    status = ClaudeProvider(path, now=lambda: NOW).get_status()

    assert status.available is True
    assert status.remaining_percentage == 35
    assert status.freshness is Freshness.FRESH


def test_claude_snapshot_honors_explicit_unavailability(tmp_path):
    path = write_snapshot(
        tmp_path,
        "usage-claude.json",
        {
            "available": False,
            "error": "rate limited",
            "five_hour_used_percentage": 20,
        },
    )

    status = ClaudeProvider(path, now=lambda: NOW).get_status()

    assert status.available is False
    assert status.remaining_percentage is None
    assert status.reason == "rate limited"


def test_codex_snapshot_uses_more_constrained_quota(tmp_path):
    path = write_snapshot(
        tmp_path,
        "usage-codex.json",
        {
            "provider": "codex",
            "available": True,
            "five_hour_remaining_percentage": 70,
            "seven_day_remaining_percentage": 30,
            "observed_at": NOW.timestamp(),
            "plan_type": "pro",
        },
    )

    status = CodexProvider(path, now=lambda: NOW).get_status()

    assert status.available is True
    assert status.remaining_percentage == 30
    assert status.freshness is Freshness.FRESH
    assert status.metadata["plan_type"] == "pro"


@pytest.mark.parametrize("provider", [ClaudeProvider, CodexProvider])
def test_missing_usage_file_is_unknown_not_unavailable(tmp_path, provider):
    status = provider(tmp_path / "not-present.json", now=lambda: NOW).get_status()

    assert status.available is True
    assert status.remaining_percentage is None
    assert status.freshness is Freshness.UNKNOWN
    assert "not found" in (status.reason or "").lower()


@pytest.mark.parametrize("provider", [ClaudeProvider, CodexProvider])
def test_malformed_usage_file_is_unknown_not_unavailable(tmp_path, provider):
    path = tmp_path / "bad.json"
    path.write_text("{not JSON", encoding="utf-8")

    status = provider(path, now=lambda: NOW).get_status()

    assert status.available is True
    assert status.remaining_percentage is None
    assert status.freshness is Freshness.UNKNOWN
    assert "malformed" in (status.reason or "").lower()


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (NOW, Freshness.FRESH),
        (NOW - timedelta(minutes=10), Freshness.STALE),
        (NOW - timedelta(hours=2), Freshness.VERY_STALE),
        (None, Freshness.UNKNOWN),
    ],
)
def test_timestamp_freshness_categories(tmp_path, timestamp, expected):
    payload = {"available": True, "five_hour_remaining_percentage": 50}
    if timestamp is not None:
        payload["observed_at"] = timestamp.isoformat()
    path = write_snapshot(tmp_path, "usage-codex.json", payload)

    status = CodexProvider(path, now=lambda: NOW).get_status()

    assert status.freshness is expected
    assert status.remaining_percentage == 50


def test_build_providers_keeps_default_registration_order(tmp_path):
    providers = build_providers(Config(), tmp_path)
    assert [provider.name for provider in providers] == ["claude", "codex"]


def test_build_providers_omits_disabled_provider_and_ignores_unknown_config_id(tmp_path):
    config = Config(
        providers={
            "codex": ProviderConfig(enabled=False),
            "not-registered": ProviderConfig(enabled=False),
        }
    )
    providers = build_providers(config, tmp_path)
    assert [provider.name for provider in providers] == ["claude"]


# --- malformed reset metadata is UNKNOWN, never an exception ---


@pytest.mark.parametrize("provider,filename", [(ClaudeProvider, "usage-claude.json"), (CodexProvider, "usage-codex.json")])
@pytest.mark.parametrize("resets_at", [1e100, float("nan"), float("inf"), "not-a-timestamp", 10**18])
def test_absurd_absolute_reset_is_unknown_in_the_shared_parser(
    tmp_path, provider, filename, resets_at
):
    """Both parsers behave identically on the same unusable reset value."""
    path = write_snapshot(
        tmp_path,
        filename,
        {
            "available": True,
            "five_hour_remaining_percentage": 40,
            "seven_day_remaining_percentage": 70,
            "five_hour_resets_at": resets_at,
            "collected_at": NOW.isoformat(),
        },
    )

    status = provider(path, now=lambda: NOW).get_status()

    assert status.remaining_percentage == 40
    assert status.capacity is not None
    assert status.capacity.window == "five_hour"
    assert status.capacity.reset_at is None


# --- An integer no float can hold ----------------------------------------------

#: `1e100` is a finite float and was already bounded away; `10 ** 1000` is a
#: perfectly valid JSON integer that `float()` itself cannot represent, so the
#: conversion raised `OverflowError` before any bound could reject it, and that
#: exception came out of `get_status()`.
UNREPRESENTABLE = 10**1000


@pytest.mark.parametrize(
    "provider,filename",
    [(ClaudeProvider, "usage-claude.json"), (CodexProvider, "usage-codex.json")],
)
@pytest.mark.parametrize(
    "value",
    [
        UNREPRESENTABLE,
        -UNREPRESENTABLE,
        1e30,  # a representable epoch, but far beyond datetime.max
    ],
)
def test_f1_residual_unrepresentable_reset_is_unknown_in_the_shared_parser(
    tmp_path, provider, filename, value
):
    """The same payload through Claude's and Codex's own `get_status()`."""
    path = write_snapshot(
        tmp_path,
        filename,
        {
            "available": True,
            "five_hour_remaining_percentage": 40,
            "seven_day_remaining_percentage": 70,
            "five_hour_resets_at": value,
            "seven_day_resets_at": value,
            "collected_at": NOW.isoformat(),
        },
    )

    status = provider(path, now=lambda: NOW).get_status()

    assert status.remaining_percentage == 40
    assert status.capacity is not None
    assert status.capacity.reset_at is None


@pytest.mark.parametrize("value", [UNREPRESENTABLE, -UNREPRESENTABLE, 1e30])
def test_f1_residual_unrepresentable_percentages_are_unknown_not_an_exception(
    tmp_path, value
):
    """A percentage is a numeric conversion too, and shares the same guard."""
    path = write_snapshot(
        tmp_path,
        "usage-codex.json",
        {
            "available": True,
            "five_hour_remaining_percentage": value,
            "seven_day_remaining_percentage": value,
            "collected_at": NOW.isoformat(),
        },
    )

    status = CodexProvider(path, now=lambda: NOW).get_status()

    assert status.remaining_percentage in (None, 0.0, 100.0)
