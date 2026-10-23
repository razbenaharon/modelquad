# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Quota tiers and evidence: what a provider's reported windows say.

Pure: no I/O, no ambient clock, no randomness. The caller injects ``now``;
every input is a normalized snapshot the product already collects.

Quota is DISPLAY-ONLY. Nothing in this module chooses a provider, a model or
a reasoning effort, and nothing that reads it may: the worker is always the
one the user named. The tiers feed the status payloads, the statusline and
the suggested handoff in ``assistance.py``, which only ever asks the user.

Two honesty rules:

* **Unknown stays unknown.** A missing or untrustworthy quota reading yields
  ``QuotaTier.UNKNOWN`` -- never an invented percentage and never an assumed
  exhaustion.
* **Observed refusals are facts.** A refusal a worker reported is overlaid on
  the snapshot (``apply_refusal_overlay``) so the display does not claim
  capacity the runtime has just refused.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from .models import Freshness, ProviderScore, ProviderStatus
from .quota_windows import quota_view, reported_windows


# --- Tier thresholds ---------------------------------------------------------
#
# Evidence (dogfood log, 2026-09-06):
#
# * One independent review consumed ~13 points of Codex's five-hour window
#   (23% -> 10%) and ~2 points of its weekly window. A provider under ~10%
#   of its binding window therefore cannot be assumed to survive one more
#   substantive assignment: EXHAUSTED starts there.
# * Codex refused new work at 14:04Z while its five-hour window read 10% and
#   its weekly read 8%; a maintainer had already stopped assigning it
#   routine reviews at ~23%/10%. TIGHT therefore has to begin well above the
#   refusal point, not at it: 10..35 is the "conserve, do not spend on
#   routine work" band.
# * A provider at 79% five-hour / 96% weekly absorbed reassigned reviews at
#   ~6-8 points per review -- roughly ten reviews of headroom. That is what
#   "abundant" meant operationally, and 60 is the round number below it that
#   still leaves that margin.
# * The band between them (35..60) is where a provider is a normal, unworried
#   choice: enough for several substantive assignments, not enough to stop
#   noticing.
QUOTA_ABUNDANT_MINIMUM = 60.0
QUOTA_HEALTHY_MINIMUM = 35.0
QUOTA_EXHAUSTED_BELOW = 10.0

# A window about to reset is not scarcity, it is timing: within 900 seconds
# the capacity is back before a substantive task would finish.
RESET_GRACE_SECONDS = 900

# How long one observed runtime refusal keeps a provider operationally
# unavailable when no reset time is known. Evidence: Codex refused at 10:30Z
# and the snapshot still read 33% at 10:31Z, disagreeing for nearly three
# hours (until 13:23Z); it refused again at 14:04Z naming a 17:58Z reset.
# 1800 seconds is deliberately shorter than the observed snapshot lag -- long
# enough that the stale reading cannot immediately overrule the refusal, short
# enough that a provider is never held down on evidence that has itself gone
# stale. A KNOWN reset time always wins over this default.
REFUSAL_TTL_SECONDS = 1800


class QuotaTier(str, Enum):
    """How much of a provider's binding quota window is really available."""

    ABUNDANT = "abundant"
    HEALTHY = "healthy"
    TIGHT = "tight"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


_TIER_RANK: dict[QuotaTier, int] = {
    QuotaTier.EXHAUSTED: 0,
    QuotaTier.TIGHT: 1,
    QuotaTier.UNKNOWN: 2,
    QuotaTier.HEALTHY: 3,
    QuotaTier.ABUNDANT: 4,
}


class _Observation(Protocol):
    """The two fields every capacity reading in the product carries."""

    remaining_percentage: float | None
    freshness: Freshness


def usable_capacity(observation: _Observation) -> float | None:
    """Confidence-adjusted usable remaining percentage, or None.

    The single capacity discount in the product, so the tiers and any other
    reader cannot disagree about what a reading is worth. FRESH is used raw;
    STALE is blended halfway toward a neutral 50 (a stale 80% counts as 65%);
    UNKNOWN and VERY_STALE are never usable, because capacity is never
    invented from unreliable data.
    """

    remaining = observation.remaining_percentage
    if remaining is None:
        return None
    if observation.freshness is Freshness.FRESH:
        return float(remaining)
    if observation.freshness is Freshness.STALE:
        return 50.0 + 0.5 * (float(remaining) - 50.0)
    return None


@dataclass(frozen=True)
class QuotaEvidence:
    """Exactly the quota facts one decision used, and nothing inferred."""

    tier: QuotaTier = QuotaTier.UNKNOWN
    remaining: float | None = None
    freshness: Freshness = Freshness.UNKNOWN
    reset_in_seconds: float | None = None
    pool_id: str | None = None
    refusal_observed_at: str | None = None
    window: str | None = None
    windows: tuple[QuotaEvidence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = {
            "tier": self.tier.value,
            "remaining": self.remaining,
            "freshness": self.freshness.value,
            "reset_in_seconds": self.reset_in_seconds,
            "pool_id": self.pool_id,
            "refusal_observed_at": self.refusal_observed_at,
        }
        if self.window is not None:
            result["window"] = self.window
        if self.windows:
            result["windows"] = [window.to_dict() for window in self.windows]
        return result


def _as_utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def reset_in_seconds(status: ProviderStatus | None, now: datetime | None) -> float | None:
    """Seconds until the binding window resets; negative when already past.

    Returns None when either fact is missing -- reset timing is copied only
    when a provider objectively reported it (the same rule
    ``reasoning/evidence.py`` applies), never derived from a window name.
    """

    if status is None or status.capacity is None or status.capacity.reset_at is None:
        return None
    reference = _as_utc(now)
    reset_at = _as_utc(status.capacity.reset_at)
    if reference is None or reset_at is None:
        return None
    return (reset_at - reference).total_seconds()


def binding_pool_id(status: ProviderStatus | None) -> str | None:
    """Which pool the capacity this decision used actually came from.

    Evidence must not contradict the number it explains: the pool named must
    be the one whose capacity is in use.

    So the pool is identified the same way the capacity was:

    1. the pool whose remaining percentage IS the capacity in use (the
       binding window's, else the headline) -- true by construction for every
       single-pool provider;
    2. failing that, ``None``. An unidentifiable pool is an unknown pool; a
       guess here would be exactly the contradiction this function exists to
       prevent.
    """

    if status is None or not status.pools:
        return None
    known = [pool for pool in status.pools if pool.remaining_percentage is not None]
    if not known:
        return None
    used = status.capacity.remaining_percentage if status.capacity is not None else None
    if used is None:
        used = status.remaining_percentage
    if used is not None:
        matching = [pool for pool in known if abs(pool.remaining_percentage - used) <= 1e-9]
        if matching:
            return min(matching, key=lambda pool: pool.id).id
    return None


def classify_quota(
    remaining: float | None,
    reset_seconds: float | None = None,
) -> QuotaTier:
    """Map a confidence-adjusted remaining percentage to a tier.

    ``remaining`` must already have been through :func:`usable_capacity`;
    ``None`` means "no trustworthy reading", which is UNKNOWN and stays
    UNKNOWN -- it is never treated as empty and never as full.

    Reset awareness, both directions:

    * A TIGHT window whose reset lands within ``RESET_GRACE_SECONDS`` reads
      HEALTHY: the capacity is minutes away.
    * A reset time already in the past means the snapshot describes a window
      that no longer exists. That is not "refreshed capacity" and not
      "exhausted" -- it is an observation we can no longer trust, so it reads
      UNKNOWN. This is the common case for Claude, whose statusline snapshot
      is often days old.
    """

    if reset_seconds is not None and reset_seconds < 0:
        return QuotaTier.UNKNOWN
    if remaining is None:
        return QuotaTier.UNKNOWN
    if remaining < QUOTA_EXHAUSTED_BELOW:
        return QuotaTier.EXHAUSTED
    if remaining >= QUOTA_ABUNDANT_MINIMUM:
        return QuotaTier.ABUNDANT
    if remaining >= QUOTA_HEALTHY_MINIMUM:
        return QuotaTier.HEALTHY
    if reset_seconds is not None and 0 <= reset_seconds <= RESET_GRACE_SECONDS:
        return QuotaTier.HEALTHY
    return QuotaTier.TIGHT


def quota_evidence_for(
    status: ProviderStatus | None,
    score: ProviderScore | None = None,
    now: datetime | None = None,
    refusal_observed_at: str | None = None,
    *,
    freshness_thresholds=None,
) -> QuotaEvidence:
    """Assemble the quota facts for one provider without inventing any."""

    if status is not None:
        from .routing_freshness import routing_view
        status = quota_view(status, now=now)
        status = routing_view(status, freshness_thresholds, now=now)
        pool_id = binding_pool_id(status)
        reference = _as_utc(now)
        windows = tuple(
            QuotaEvidence(
                tier=classify_quota(
                    usable_capacity(window),
                    (reset - reference).total_seconds()
                    if (reset := _as_utc(window.reset_at)) is not None and reference is not None
                    else None,
                ),
                remaining=usable_capacity(window),
                freshness=(Freshness.UNKNOWN if status.freshness is Freshness.UNKNOWN
                           else window.freshness),
                reset_in_seconds=(reset - reference).total_seconds()
                if reset is not None and reference is not None else None,
                pool_id=pool_id,
                window=window.window,
            )
            for window in reported_windows(status)
        )
        # A future observation cannot support any of its window readings,
        # even if a caller handed us a capacity with an old FRESH label.
        if status.freshness is Freshness.UNKNOWN:
            windows = tuple(
                replace(w, tier=QuotaTier.UNKNOWN, remaining=None, freshness=Freshness.UNKNOWN)
                for w in windows
            )
        constrained = [w for w in windows if w.tier in (QuotaTier.EXHAUSTED, QuotaTier.TIGHT)]
        unknown = [w for w in windows if w.tier is QuotaTier.UNKNOWN]
        binding = min(
            constrained or unknown or list(windows),
            key=lambda w: (_TIER_RANK[w.tier], w.remaining if w.remaining is not None else 101,
                           w.window or ""),
            default=None,
        )
        if binding is not None:
            return replace(binding, refusal_observed_at=refusal_observed_at, windows=windows)
    observation: _Observation | None = status if status is not None else score
    if observation is None:
        return QuotaEvidence(refusal_observed_at=refusal_observed_at)
    remaining = usable_capacity(observation)
    seconds = reset_in_seconds(status, now)
    return QuotaEvidence(
        tier=classify_quota(remaining, seconds),
        remaining=remaining,
        freshness=observation.freshness,
        reset_in_seconds=seconds,
        pool_id=binding_pool_id(status),
        refusal_observed_at=refusal_observed_at,
    )


def _refusal_stamp(status: ProviderStatus) -> object:
    """The overlay's own refusal marker on a status, or None.

    Read through the same ``Mapping`` guard the rest of this module uses for
    ``status.metadata`` (see :func:`window_reset_at` and :func:`_observed_quota`)
    so a status carrying no metadata at all is a provider with no refusal on it,
    never an ``AttributeError`` raised out of a routing decision.
    """

    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    return metadata.get("quota_refusal_observed_at")


def stale_conserving_providers(
    statuses: Iterable[ProviderStatus], now: datetime | None = None,
    *, freshness_thresholds=None,
) -> tuple[str, ...]:
    """Providers whose stale reading is worth refreshing before it is shown.

    A stale reading can disagree with the provider in both directions (a
    Codex reading once showed 33% while the runtime refused, and 2% while the
    app-server read 100%), so the caller asks the existing refresher to look.

    These are the TIGHT and EXHAUSTED bands: a stale reading in them would
    be shown as scarcity, so it is worth a fresh look first. HEALTHY,
    ABUNDANT and UNKNOWN make no such claim; VERY_STALE alone is UNKNOWN,
    not exhaustion.
    A refusal already applied by ``apply_refusal_overlay`` is independent
    evidence of exclusion and can therefore trigger even with UNKNOWN quota
    or UNKNOWN freshness (a refresh that published no observation).
    Other availability failures are not quota evidence.

    This selects evidence to refresh. It never admits, ranks or picks a
    provider.
    """

    from .routing_freshness import routing_view
    statuses = (routing_view(quota_view(status, now=now), freshness_thresholds, now=now)
                for status in statuses)
    return tuple(dict.fromkeys(
        status.name
        for status in statuses
        if (
            (status.freshness in (Freshness.STALE, Freshness.VERY_STALE, Freshness.UNKNOWN)
             and not status.available and bool(_refusal_stamp(status)))
            or (
                status.freshness in (Freshness.STALE, Freshness.VERY_STALE)
                and status.available
                and quota_evidence_for(status, now=now, freshness_thresholds=freshness_thresholds).tier
                in (QuotaTier.TIGHT, QuotaTier.EXHAUSTED)
            )
        )
    ))


# --- Fresh runtime refusal overlay -------------------------------------------


@dataclass(frozen=True)
class QuotaRefusal:
    """One observed runtime quota refusal, as read back from provider health."""

    provider: str
    observed_at: datetime
    window: str | None = None
    detail: str | None = None


def window_reset_at(status: ProviderStatus | None, window: str | None) -> datetime | None:
    """The reset time of ONE named quota window, or None when unknown.

    A refusal names the window it hit ("usage limit ... try again at 8:58 PM"
    is a five-hour limit; a weekly cap is a different fact three days out), and
    that window is not always the snapshot's binding one. Resolved from the
    facts already collected, in the order they are trustworthy:

    * the structured ``CapacityState`` when it IS that window;
    * the provider-native reset keys preserved in ``metadata``
      (``<window>_resets_at`` for Codex and Claude);
    * a per-bucket quota mapping (``quota`` / ``buckets``), whose bucket ids ARE the window names, absolute
      ``reset_time`` preferred and the relative ``reset_in_seconds`` used only
      to derive one.

    Returns None when the named window reports no reset -- an unknown reset, on
    which the caller falls back to the TTL rather than to some other window's
    clock.
    """

    if status is None or not window:
        return None
    capacity = status.capacity
    if capacity is not None and capacity.window == window and capacity.reset_at is not None:
        return _as_utc(capacity.reset_at)
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    # Imported lazily so this module keeps no provider dependency at import
    # time, while still using the ONE guarded timestamp parser rather than a
    # second copy that could drift from it.
    from .providers.base import parse_timestamp, reset_at_from_delay

    direct = parse_timestamp(metadata.get(f"{window}_resets_at"))
    if direct is not None:
        return _as_utc(direct)
    buckets: Any = metadata.get("quota")
    if not isinstance(buckets, Mapping) or not buckets:
        buckets = metadata.get("buckets")
    if isinstance(buckets, Mapping):
        bucket = buckets.get(window)
        if isinstance(bucket, Mapping):
            absolute = parse_timestamp(bucket.get("reset_time"))
            if absolute is not None:
                return _as_utc(absolute)
            derived = reset_at_from_delay(
                _as_utc(status.observed_at), bucket.get("reset_in_seconds")
            )
            if derived is not None:
                return _as_utc(derived)
    return None


def _observed_quota(status: ProviderStatus | None) -> bool:
    """Whether this snapshot actually read a quota, rather than merely looking.

    A collector that finds nothing still returns a status: Codex's, when the
    app-server gives no rate-limit data, reports `available=True`,
    every percentage None, and a `collected_at` of the moment it looked --
    which `ProviderStatus.observed_at` then carries. That timestamp is "when
    we last looked", not "when the provider made a reading", and treating the
    two as one retires a refusal that nothing has contradicted: the executor
    records "codex refused this work", the next status read finds no quota
    data, and the refusal is spent by an observation that observed nothing.
    "A later observation has already seen through it" requires
    an observation; an empty read has seen through nothing.

    This is deliberately about EVIDENCE, not freshness. Such a read is FRESH
    -- we really did just look -- and it is still the right answer for every
    other question the router asks it. It is only unfit for this one, where
    the whole point is that a refusal outranks a snapshot until something is
    measured that disagrees with it.

    Every place a reading can live is checked, and each is checked for a
    VALUE rather than for the object that would carry one: the headline
    scalar, then the capacity window (whose fields are all optional, so an
    empty `CapacityState` is an object and not an observation), then the
    per-pool readings, which are where a multi-pool subscription keeps its
    truth.
    """

    if status is None:
        return False
    if status.remaining_percentage is not None:
        return True
    capacity = status.capacity
    if capacity is not None and (
        capacity.remaining_percentage is not None
        or capacity.used_percentage is not None
        or capacity.reset_at is not None
    ):
        return True
    return any(pool.remaining_percentage is not None for pool in status.pools)


def refusal_in_force(
    refusal: QuotaRefusal,
    status: ProviderStatus | None,
    now: datetime,
    ttl_seconds: float = REFUSAL_TTL_SECONDS,
) -> bool:
    """Whether a refusal still overrules the snapshot.

    Principle 3: fresh operational evidence overrides stale quota
    state. Two conditions, both required:

    * the refusal is NEWER than the snapshot it contradicts (an older refusal
      that a later observation has already seen through is spent) -- but only
      a snapshot that OBSERVED something counts, see :func:`_observed_quota`;
    * it has not itself gone stale -- until the reset of THE WINDOW IT NAMES
      when that is known from a usable observation, otherwise
      ``REFUSAL_TTL_SECONDS``.

    A clock correction cannot erase a refusal. A future refusal is treated as
    observed now rather than allowed to claim ordering beyond this clock.

    The window matters. A ``seven_day`` refusal
    expired against the snapshot's binding FIVE-HOUR reset read as recovered
    hours before the weekly window it actually named came back -- one window's
    clock answering another window's question. The named window's reset is now
    resolved by :func:`window_reset_at`, and when THAT window reports no reset
    the fallback is the TTL, never a different window's time. A refusal with no
    window named at all has nothing to resolve, so the binding window's reset
    -- the only reset the snapshot claims -- remains its best available clock.
    """

    observed = _as_utc(refusal.observed_at)
    reference = _as_utc(now)
    if observed is None or reference is None:
        return False
    future_refusal = observed > reference
    observed = min(observed, reference)
    from .usage.base import valid_timestamp_epoch

    snapshot_at = _as_utc(status.observed_at) if _observed_quota(status) else None
    if snapshot_at is not None and valid_timestamp_epoch(snapshot_at, now=reference) is None:
        snapshot_at = None
    if snapshot_at is not None and not future_refusal and observed <= snapshot_at:
        return False
    if snapshot_at is None:
        # An unusable observation cannot prove recovery through its reset.
        return (reference - observed).total_seconds() <= ttl_seconds
    if refusal.window:
        reset_at = window_reset_at(status, refusal.window)
    else:
        reset_at = (
            _as_utc(status.capacity.reset_at) if status is not None and status.capacity else None
        )
    if reset_at is not None and reset_at > observed:
        return reference < reset_at
    return (reference - observed).total_seconds() <= ttl_seconds


def apply_refusal_overlay(
    statuses: Iterable[ProviderStatus],
    refusals: Iterable[QuotaRefusal],
    now: datetime,
    ttl_seconds: float = REFUSAL_TTL_SECONDS,
) -> tuple[ProviderStatus, ...]:
    """Mark providers unavailable where a fresh runtime refusal contradicts them.

    Pure: takes the statuses and the refusals, returns new statuses. Only
    ``available`` and ``reason`` change -- **the quota fields are untouched**.
    The snapshot stays honest about what it observed; the refusal is an
    availability fact standing beside it, not a correction to a percentage
    nobody re-measured. Overwriting the percentage would fabricate a reading.
    """

    reference = _as_utc(now)
    newest: dict[str, QuotaRefusal] = {}
    ordering: dict[str, tuple[bool, datetime]] = {}
    for refusal in refusals:
        observed = _as_utc(refusal.observed_at)
        if observed is None:
            continue
        # A future timestamp cannot establish that a refusal happened after
        # one recorded on the real clock. Prefer real chronology whenever
        # available; keep a lone future refusal, bounded at this clock.
        key = (observed <= reference, min(observed, reference)) if reference else (True, observed)
        if refusal.provider not in ordering or key > ordering[refusal.provider]:
            newest[refusal.provider] = refusal
            ordering[refusal.provider] = key
    result: list[ProviderStatus] = []
    for status in statuses:
        refusal = newest.get(status.name)
        if refusal is None or not refusal_in_force(refusal, status, now, ttl_seconds):
            result.append(status)
            continue
        window = f" ({refusal.window})" if refusal.window else ""
        observed = _as_utc(refusal.observed_at)
        stamp = observed.isoformat() if observed else "recently"
        result.append(
            ProviderStatus(
                name=status.name,
                available=False,
                remaining_percentage=status.remaining_percentage,
                observed_at=status.observed_at,
                freshness=status.freshness,
                metadata={**status.metadata, "quota_refusal_observed_at": stamp},
                reason=(
                    f"fresh runtime refusal{window} at {stamp} overrides the quota "
                    "snapshot; no new work until it recovers or resets"
                ),
                capacity=status.capacity,
                pools=status.pools,
                plan=status.plan,
            )
        )
    return tuple(result)
