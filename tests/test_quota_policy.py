# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for the pure quota-aware resource policy.

Mirrors `test_router_consumption.py`: hand-built normalized snapshots, an
injected clock, no I/O. The router-level half lives in `test_router_quota.py`;
each of the Issue's eight example tests is named `test_example_N_...` in one
file or the other so the mapping is checkable by grep.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modelquad.models import (
    CapacityState,
    Freshness,
    ProviderStatus,
    ResourcePool,
)
from modelquad.quota_policy import (
    QUOTA_ABUNDANT_MINIMUM,
    QUOTA_EXHAUSTED_BELOW,
    QUOTA_HEALTHY_MINIMUM,
    QuotaEvidence,
    QuotaRefusal,
    QuotaTier,
    REFUSAL_TTL_SECONDS,
    RESET_GRACE_SECONDS,
    apply_refusal_overlay,
    binding_pool_id,
    classify_quota,
    quota_evidence_for,
    refusal_in_force,
    stale_conserving_providers,
    usable_capacity,
)

NOW = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)


def status(
    name: str,
    remaining: float | None,
    *,
    freshness: Freshness = Freshness.FRESH,
    reset_at: datetime | None = None,
    window: str | None = None,
    pools: tuple[ResourcePool, ...] = (),
    observed_at: datetime | None = NOW,
    available: bool = True,
) -> ProviderStatus:
    return ProviderStatus(
        name=name,
        available=available,
        remaining_percentage=remaining,
        observed_at=observed_at,
        freshness=freshness,
        capacity=(
            CapacityState(
                remaining_percentage=remaining,
                reset_at=reset_at,
                window=window,
                freshness=freshness,
            )
            if reset_at is not None
            else None
        ),
        pools=pools,
    )


# --- capacity discount is shared, not duplicated ------------------------------


def test_usable_capacity_is_the_one_confidence_discount():
    fresh = status("codex", 80)
    stale = status("codex", 80, freshness=Freshness.STALE)
    unknown = status("codex", 80, freshness=Freshness.UNKNOWN)

    assert usable_capacity(fresh) == 80.0
    assert usable_capacity(stale) == 65.0  # halfway toward neutral 50
    assert usable_capacity(unknown) is None


# --- tiers --------------------------------------------------------------------


@pytest.mark.parametrize(
    "remaining,expected",
    [
        (100.0, QuotaTier.ABUNDANT),
        (QUOTA_ABUNDANT_MINIMUM, QuotaTier.ABUNDANT),
        (QUOTA_ABUNDANT_MINIMUM - 0.1, QuotaTier.HEALTHY),
        (QUOTA_HEALTHY_MINIMUM, QuotaTier.HEALTHY),
        (QUOTA_HEALTHY_MINIMUM - 0.1, QuotaTier.TIGHT),
        (QUOTA_EXHAUSTED_BELOW, QuotaTier.TIGHT),
        (QUOTA_EXHAUSTED_BELOW - 0.1, QuotaTier.EXHAUSTED),
        (0.0, QuotaTier.EXHAUSTED),
        (None, QuotaTier.UNKNOWN),
    ],
)
def test_classify_quota_bands(remaining, expected):
    assert classify_quota(remaining) is expected


def test_reset_grace_makes_a_tight_window_read_healthy():
    """A window resetting inside the grace period is timing, not scarcity."""
    assert classify_quota(20.0, RESET_GRACE_SECONDS - 1) is QuotaTier.HEALTHY
    assert classify_quota(20.0, RESET_GRACE_SECONDS) is QuotaTier.HEALTHY
    assert classify_quota(20.0, RESET_GRACE_SECONDS + 1) is QuotaTier.TIGHT
    # The grace never manufactures capacity out of an exhausted window.
    assert classify_quota(3.0, 60) is QuotaTier.EXHAUSTED


def test_a_reset_already_in_the_past_reads_unknown():
    """A snapshot describing a window that no longer exists is not evidence."""
    assert classify_quota(80.0, -1) is QuotaTier.UNKNOWN
    assert classify_quota(2.0, -3600) is QuotaTier.UNKNOWN


def test_quota_evidence_uses_the_binding_pool_and_its_reset():
    reset = NOW + timedelta(hours=2)
    pools = (
        ResourcePool(id="codex:five_hour", provider="codex", remaining_percentage=23.0),
        ResourcePool(id="codex:seven_day", provider="codex", remaining_percentage=8.0),
    )
    snapshot = status("codex", 8.0, reset_at=reset, window="seven_day", pools=pools)

    evidence = quota_evidence_for(snapshot, None, NOW)

    assert evidence.tier is QuotaTier.EXHAUSTED
    assert evidence.pool_id == "codex:seven_day"
    assert evidence.reset_in_seconds == pytest.approx(7200.0)
    assert binding_pool_id(snapshot) == "codex:seven_day"


def test_quota_evidence_without_a_reading_stays_unknown():
    evidence = quota_evidence_for(status("claude", None, freshness=Freshness.UNKNOWN), None, NOW)
    assert evidence == QuotaEvidence(
        tier=QuotaTier.UNKNOWN, remaining=None, freshness=Freshness.UNKNOWN
    )


@pytest.mark.parametrize(
    "remaining,freshness,reset_seconds,expected",
    [
        (2, Freshness.STALE, None, ("codex",)),
        (19, Freshness.STALE, None, ("codex",)),
        (20, Freshness.STALE, None, ()),  # discounted 35 is HEALTHY
        (90, Freshness.STALE, None, ()),
        (2, Freshness.FRESH, None, ()),
        (2, Freshness.VERY_STALE, None, ()),
        (None, Freshness.STALE, None, ()),
        (2, Freshness.UNKNOWN, None, ()),
        (2, Freshness.STALE, RESET_GRACE_SECONDS, ()),
        (2, Freshness.STALE, -1, ()),
        (2, Freshness.STALE, RESET_GRACE_SECONDS + 1, ("codex",)),
    ],
)
def test_refresh_trigger_uses_the_decisions_discount_and_reset_grace(
    remaining, freshness, reset_seconds, expected,
):
    snapshot = status(
        "codex", remaining, freshness=freshness,
        reset_at=NOW + timedelta(seconds=reset_seconds) if reset_seconds is not None else None,
    )
    assert stale_conserving_providers([snapshot, snapshot], NOW) == expected


@pytest.mark.parametrize("freshness", list(Freshness))
def test_only_a_stale_refusal_overlay_triggers_even_without_a_quota(freshness):
    snapshot = status("codex", None, freshness=freshness)
    overlaid = apply_refusal_overlay([snapshot], [QuotaRefusal("codex", NOW)], NOW)
    # W07: an UNKNOWN reading under a refusal also refreshes (a null observation
    # is unknown since U7, and would otherwise never lift the refusal early).
    expected = (("codex",) if freshness in (Freshness.STALE, Freshness.VERY_STALE, Freshness.UNKNOWN)
                else ())
    assert stale_conserving_providers(overlaid, NOW) == expected
    assert stale_conserving_providers(
        [status("codex", 2, freshness=freshness, available=False)], NOW,
    ) == ()


# --- fresh refusal overrides the snapshot -------------------------------------


def test_example_3_a_fresh_refusal_overrides_a_positive_snapshot():
    """Issue example 3: 33% in the snapshot, 'usage limit reached' at runtime."""
    snapshot = status("codex", 33.0, observed_at=NOW - timedelta(minutes=30))
    refusal = QuotaRefusal(
        provider="codex", observed_at=NOW - timedelta(minutes=5), window="five_hour"
    )

    (overlaid,) = apply_refusal_overlay([snapshot], [refusal], NOW)

    assert overlaid.available is False
    assert "fresh runtime refusal" in (overlaid.reason or "")
    assert "five_hour" in (overlaid.reason or "")
    # The quota fields are UNTOUCHED: the refusal is an availability fact, and
    # nobody re-measured the percentage.
    assert overlaid.remaining_percentage == 33.0
    assert overlaid.freshness is snapshot.freshness
    assert overlaid.observed_at == snapshot.observed_at


def test_a_refusal_older_than_the_snapshot_is_spent():
    snapshot = status("codex", 33.0, observed_at=NOW - timedelta(minutes=1))
    refusal = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(minutes=30))

    assert refusal_in_force(refusal, snapshot, NOW) is False
    (overlaid,) = apply_refusal_overlay([snapshot], [refusal], NOW)
    assert overlaid.available is True


def test_a_read_that_observed_nothing_does_not_spend_a_refusal():
    """An empty read is not a later observation.

    A collector that finds nothing still returns a status. Codex's, when no
    recent session file carries rate-limit data, reports available with every
    percentage None and an `observed_at` of the moment it looked. Compared as
    an observation, that timestamp retired a refusal the executor had written
    seconds earlier -- the provider refused the work, we looked, we learned
    nothing, and the refusal was declared seen through. Nothing had seen
    through it.
    """
    empty_read = ProviderStatus(
        name="codex",
        available=True,
        remaining_percentage=None,
        observed_at=NOW,
        freshness=Freshness.FRESH,
        capacity=None,
    )
    refusal = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(minutes=1))

    assert refusal_in_force(refusal, empty_read, NOW) is True
    (overlaid,) = apply_refusal_overlay([empty_read], [refusal], NOW)
    assert overlaid.available is False


def test_an_empty_capacity_object_is_not_an_observation():
    """The carrier is not the reading. Every `CapacityState` field is optional,
    so a collector that builds one and fills nothing in must not read as a
    provider that measured something."""
    hollow = ProviderStatus(
        name="codex",
        available=True,
        remaining_percentage=None,
        observed_at=NOW,
        freshness=Freshness.FRESH,
        capacity=CapacityState(),
    )
    refusal = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(minutes=1))

    assert refusal_in_force(refusal, hollow, NOW) is True


def test_a_pool_level_reading_is_an_observation():
    """A multi-pool subscription keeps its truth in `pools`; the headline
    scalar is derived from it and is not the only place a reading can live."""
    pooled = ProviderStatus(
        name="zeta",
        available=True,
        remaining_percentage=None,
        observed_at=NOW,
        freshness=Freshness.FRESH,
        capacity=None,
        pools=(
            ResourcePool(id="zeta:main", provider="zeta", remaining_percentage=42.0),
        ),
    )
    refusal = QuotaRefusal(provider="zeta", observed_at=NOW - timedelta(minutes=1))

    assert refusal_in_force(refusal, pooled, NOW) is False


def test_an_empty_read_still_lets_the_refusal_expire_on_its_ttl():
    """Not observing anything keeps a refusal standing; it does not make it
    permanent. Nothing here can outlive the clock the refusal already had."""
    empty_read = ProviderStatus(
        name="codex", available=True, observed_at=NOW, freshness=Freshness.FRESH
    )
    refusal = QuotaRefusal(
        provider="codex", observed_at=NOW - timedelta(seconds=REFUSAL_TTL_SECONDS + 60)
    )

    assert refusal_in_force(refusal, empty_read, NOW) is False


def test_a_real_reading_still_spends_an_older_refusal():
    """The control: a snapshot that DID measure something keeps its authority."""
    measured = status("codex", 33.0, observed_at=NOW)
    refusal = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(minutes=1))

    assert refusal_in_force(refusal, measured, NOW) is False


def test_a_refusal_expires_with_its_ttl():
    snapshot = status("codex", 33.0, observed_at=NOW - timedelta(hours=3))
    inside = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(seconds=REFUSAL_TTL_SECONDS - 60))
    outside = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(seconds=REFUSAL_TTL_SECONDS + 60))

    assert refusal_in_force(inside, snapshot, NOW) is True
    assert refusal_in_force(outside, snapshot, NOW) is False


def test_a_known_reset_outlives_the_default_ttl():
    """Codex refused at 14:04Z naming a 17:58Z reset: the reset is the fact."""
    snapshot = status(
        "codex", 10.0, observed_at=NOW - timedelta(hours=4), reset_at=NOW + timedelta(hours=3)
    )
    refusal = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(hours=1))

    assert refusal_in_force(refusal, snapshot, NOW) is True
    assert refusal_in_force(refusal, snapshot, NOW + timedelta(hours=4)) is False


def test_only_the_newest_refusal_per_provider_counts():
    snapshot = status("codex", 33.0, observed_at=NOW - timedelta(hours=2))
    old = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(hours=3), window="weekly")
    new = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(minutes=2), window="five_hour")

    (overlaid,) = apply_refusal_overlay([snapshot], [old, new], NOW)

    assert overlaid.available is False
    assert "five_hour" in (overlaid.reason or "")


def test_a_refusal_for_another_provider_leaves_this_one_alone():
    snapshot = status("claude", 90.0)
    refusal = QuotaRefusal(provider="codex", observed_at=NOW - timedelta(minutes=1))

    (overlaid,) = apply_refusal_overlay([snapshot], [refusal], NOW)

    assert overlaid is snapshot


# --- repairs from the independent review of 2026-09-06 -----------


def test_f8_a_single_pool_provider_still_names_its_pool():
    """The one-pool case (Codex, Claude) is identified by construction."""
    pool = ResourcePool(
        id="codex", provider="codex", remaining_percentage=40.0, freshness=Freshness.FRESH
    )
    snapshot = status("codex", 40.0, pools=(pool,))

    assert binding_pool_id(snapshot) == "codex"


def test_f8_an_unidentifiable_pool_is_unknown_rather_than_guessed():
    """No pool matches the capacity in use, so the record names none."""
    pools = (
        ResourcePool(id="x:a", provider="x", remaining_percentage=10.0, freshness=Freshness.FRESH),
        ResourcePool(id="x:b", provider="x", remaining_percentage=20.0, freshness=Freshness.FRESH),
    )
    snapshot = status("x", 55.0, pools=pools)

    assert binding_pool_id(snapshot) is None


def test_f3_a_refusal_expires_against_the_window_it_names():
    """F-3: a seven_day refusal outlives the five-hour reset of a different window."""
    snapshot = ProviderStatus(
        name="codex",
        available=True,
        remaining_percentage=8.0,
        observed_at=NOW - timedelta(hours=1),
        freshness=Freshness.FRESH,
        # The BINDING window is the five-hour one, resetting in ten minutes.
        capacity=CapacityState(
            remaining_percentage=8.0,
            reset_at=NOW + timedelta(minutes=10),
            window="five_hour",
            freshness=Freshness.FRESH,
        ),
        metadata={
            "five_hour_resets_at": (NOW + timedelta(minutes=10)).isoformat(),
            "seven_day_resets_at": (NOW + timedelta(days=3)).isoformat(),
        },
    )
    refusal = QuotaRefusal(provider="codex", observed_at=NOW, window="seven_day")

    # Long past both the TTL and the five-hour reset, and still in force: the
    # weekly window it actually named is three days out.
    assert refusal_in_force(refusal, snapshot, NOW + timedelta(minutes=30)) is True
    assert refusal_in_force(refusal, snapshot, NOW + timedelta(hours=6)) is True
    assert refusal_in_force(refusal, snapshot, NOW + timedelta(days=4)) is False


def test_f3_a_named_window_with_no_known_reset_falls_back_to_the_ttl():
    """Never another window's clock: an unknown reset means the TTL, nothing else."""
    snapshot = ProviderStatus(
        name="codex",
        available=True,
        remaining_percentage=8.0,
        observed_at=NOW - timedelta(hours=1),
        freshness=Freshness.FRESH,
        capacity=CapacityState(
            remaining_percentage=8.0,
            reset_at=NOW + timedelta(days=3),
            window="seven_day",
            freshness=Freshness.FRESH,
        ),
    )
    refusal = QuotaRefusal(provider="codex", observed_at=NOW, window="five_hour")

    assert refusal_in_force(refusal, snapshot, NOW + timedelta(seconds=60)) is True
    assert (
        refusal_in_force(refusal, snapshot, NOW + timedelta(seconds=REFUSAL_TTL_SECONDS + 60))
        is False
    )
