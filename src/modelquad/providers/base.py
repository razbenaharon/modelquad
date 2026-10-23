# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Provider interfaces and common JSON usage-file behaviour."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any

from ..models import CapacityState, Freshness, FreshnessThresholds, ProviderStatus, ResourcePool


# The providers share this directory: it is where the
# external collector processes for Claude and Codex each write
# their own usage-*.json snapshot. It is a shared convention, not something
# specific to the Claude provider. Callers on a different layout (or in
# tests) should pass `usage_directory`/`usage_file` explicitly rather than
# relying on this default; the CLI exposes this as --usage-directory.
DEFAULT_USAGE_DIRECTORY = Path.home() / ".claude"


#: The widest reset delay any provider ModelQuad drives can truthfully report.
#:
#: The longest quota window in the product is seven days (Codex's ``seven_day``),
#: so a relative reset beyond a year is not a
#: long window -- it is a malformed reading. Bounding it is what keeps a
#: nonsense payload from becoming an arithmetic failure: ``reset_in_seconds``
#: of ``1e100`` raised ``OverflowError`` out of ``get_status()`` and took the
#: whole status collection with it. A rejected value
#: yields NO reset fact, which the policy reads as an UNKNOWN reset -- never an
#: invented one, and never an exception.
MAX_RESET_DELAY_SECONDS = 366 * 24 * 3600


def finite_float(value: Any) -> float | None:
    """``float(value)`` when that is a finite number, and None otherwise.

    The ONE numeric conversion every reset/quota helper below goes through, so
    a malformed reading can never become an exception in any of them. It
    rejects, without raising: booleans, ``None``, anything non-numeric or
    unparsable, NaN and the infinities -- and, the case that made this a shared
    function, a value too large for a float at all. ``1e100`` is finite and was
    handled by the bound below; a valid JSON *integer* like ``10 ** 1000`` is
    not representable, so ``float(value)`` itself raised ``OverflowError`` and
    took the whole status collection with it.
    """

    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def parse_reset_delay(value: Any) -> float | None:
    """A relative reset delay in seconds, or None when it is not usable.

    Rejects, without raising: booleans, ``None``, anything non-numeric or
    unparsable, NaN, the infinities and magnitudes no float can hold, and
    magnitudes beyond ``MAX_RESET_DELAY_SECONDS``. A NEGATIVE delay inside the
    bound is kept: a window that has already rolled over is a real observation,
    and the policy reads a past reset as "this reading predates its own reset",
    which is the honest UNKNOWN rather than a discarded fact.
    """

    parsed = finite_float(value)
    if parsed is None:
        return None
    if abs(parsed) > MAX_RESET_DELAY_SECONDS:
        return None
    return parsed


def reset_at_from_delay(base: datetime | None, value: Any) -> datetime | None:
    """``base`` plus a guarded relative delay, or None.

    The single place a relative reset becomes an absolute one, shared by every
    parser so all three behave identically on the same malformed payload.
    """

    seconds = parse_reset_delay(value)
    if seconds is None or base is None:
        return None
    try:
        return base + timedelta(seconds=seconds)
    except (OverflowError, OSError, ValueError):
        return None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 string or epoch value into a UTC datetime.

    Never raises: a non-finite or out-of-range epoch, an unparsable string and
    an absent value all yield ``None`` (an UNKNOWN reset), because a reset time
    is copied only when a provider objectively reported a usable one.
    """

    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            epoch = finite_float(value)
            if epoch is None:
                return None
            parsed = datetime.fromtimestamp(epoch, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            normalized = value.strip()
            if normalized.endswith("Z"):
                normalized = f"{normalized[:-1]}+00:00"
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
        else:
            return None
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    return parsed


def parse_percentage(value: Any) -> float | None:
    """Normalize finite percentages to the POC's inclusive 0..100 range."""

    parsed = finite_float(value)
    if parsed is None:
        return None
    return max(0.0, min(100.0, parsed))


def freshness_for(
    observed_at: datetime | None,
    thresholds: FreshnessThresholds,
    now: datetime | None = None,
) -> Freshness:
    """Classify a timestamp without turning unknown data into a quota value."""

    if observed_at is None:
        return Freshness.UNKNOWN
    reference = now or datetime.now(timezone.utc)
    # W06 owns the clock-skew contract; use it for routing freshness too.
    from ..usage.base import valid_timestamp_epoch

    if valid_timestamp_epoch(observed_at, now=reference) is None:
        return Freshness.UNKNOWN
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)
    timestamp = observed_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    age_seconds = max(0.0, (reference - timestamp).total_seconds())
    if age_seconds <= thresholds.fresh_seconds:
        return Freshness.FRESH
    if age_seconds <= thresholds.stale_seconds:
        return Freshness.STALE
    return Freshness.VERY_STALE


def constrained_remaining(
    payload: Mapping[str, Any],
    windows: tuple[tuple[str, str], ...],
) -> float | None:
    """Return the lower remaining amount among usable quota windows.

    Each tuple is ``(remaining_field, used_field)``. A supplied remaining value
    wins for that window; otherwise it is derived from its used percentage.
    """

    amounts: list[float] = []
    for remaining_field, used_field in windows:
        remaining = parse_percentage(payload.get(remaining_field))
        if remaining is None:
            used = parse_percentage(payload.get(used_field))
            if used is not None:
                remaining = 100.0 - used
        if remaining is not None:
            amounts.append(remaining)
    return min(amounts) if amounts else None


def binding_window_capacity(
    payload: Mapping[str, Any],
    windows: tuple[tuple[str, str, str, str], ...],
    *,
    observed_at: datetime | None,
    freshness: Freshness,
) -> CapacityState | None:
    """The reset fact for the window that is actually the constraint.

    Each tuple is ``(label, remaining_field, used_field, reset_field)``. The
    BINDING window is the one with the smaller remaining percentage -- the same
    window ``constrained_remaining`` already chose for the headline scalar --
    and its reset is the one a decision should reason about. Reporting the
    other window's reset would answer a question nobody asked: 25% left with a
    reset in fifteen minutes is a different fact from 25% left with a reset in
    four hours, and only the binding window's clock decides which one is true.

    The binding window is therefore chosen among every window that reports a
    PERCENTAGE, and its reset is used even when that reset is ``None``.
    Selecting only among windows that also report a reset was a hybrid: a
    seven-day window at 20% with no reset would hand the decision the
    five-hour window's 50% reset five minutes away, and the reset-grace rule
    would promote a genuinely tight provider to HEALTHY on a clock belonging
    to a window that was never the constraint. A missing reset means no grace
    and ``reset_in_seconds=None``, which is the honest answer.

    Returns ``None`` only when no window reports a percentage at all. Reset
    timing is COPIED when a provider objectively reported it, never derived
    from a window's name or from an assumed cadence.
    """

    best: tuple[float, str, datetime | None] | None = None
    for label, remaining_field, used_field, reset_field in windows:
        remaining = parse_percentage(payload.get(remaining_field))
        if remaining is None:
            used = parse_percentage(payload.get(used_field))
            remaining = None if used is None else 100.0 - used
        if remaining is None:
            continue
        reset_at = parse_timestamp(payload.get(reset_field))
        if best is None or (remaining, label) < (best[0], best[1]):
            best = (remaining, label, reset_at)
    if best is None:
        return None
    remaining, label, reset_at = best
    return CapacityState(
        remaining_percentage=remaining,
        used_percentage=max(0.0, min(100.0, 100.0 - remaining)),
        reset_at=reset_at,
        window=label,
        freshness=freshness,
        collected_at=observed_at,
    )


class Provider(ABC):
    """Abstract read-only source of normalized provider state."""

    name: str

    @abstractmethod
    def get_status(self) -> ProviderStatus:
        """Return the provider's current normalized status."""


def read_status(provider: Provider, *, refresh: bool) -> ProviderStatus:
    """Read a provider's stored snapshot.

    ``refresh`` is kept for existing callers and no longer changes anything:
    a status read never collects. Snapshots are refreshed only through
    ``usage.refresh`` (Codex's app-server, Claude's statusLine push).
    """
    del refresh
    return provider.get_status()


class JsonUsageProvider(Provider, ABC):
    """Base class for the collector-produced JSON snapshots used in v0.1."""

    filename: str

    def __init__(
        self,
        usage_file: str | Path | None = None,
        *,
        usage_directory: str | Path | None = None,
        freshness_thresholds: FreshnessThresholds | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if usage_file is not None and usage_directory is not None:
            raise ValueError("Pass usage_file or usage_directory, not both")
        directory = Path(usage_directory) if usage_directory is not None else DEFAULT_USAGE_DIRECTORY
        self.usage_file = Path(usage_file) if usage_file is not None else directory / self.filename
        self.freshness_thresholds = freshness_thresholds or FreshnessThresholds()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_status(self) -> ProviderStatus:
        try:
            contents = self.usage_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._unknown(f"Usage file not found: {self.usage_file}")
        except (OSError, UnicodeDecodeError) as exc:
            return self._unknown(f"Could not read usage file: {exc}")
        try:
            payload = json.loads(contents)
        except json.JSONDecodeError as exc:
            return self._unknown(f"Malformed JSON usage file: {exc.msg}")
        if not isinstance(payload, Mapping):
            return self._unknown("Usage JSON must contain an object")
        try:
            return self._parse_payload(payload)
        except (TypeError, ValueError, KeyError) as exc:
            return self._unknown(f"Could not normalize usage data: {exc}")

    @abstractmethod
    def _parse_payload(self, payload: Mapping[str, Any]) -> ProviderStatus:
        """Map one valid JSON object to its provider-specific status."""

    def _status(
        self,
        *,
        remaining_percentage: float | None,
        observed_at: datetime | None,
        available: bool = True,
        metadata: dict[str, Any] | None = None,
        reason: str | None = None,
        pools: "tuple[ResourcePool, ...]" = (),
        plan: str | None = None,
        capacity: CapacityState | None = None,
    ) -> ProviderStatus:
        if remaining_percentage is None and reason is None:
            reason = "Usage data has no valid remaining percentage."
        if not available and reason is None:
            reason = "Provider explicitly reported itself unavailable."
        freshness = freshness_for(observed_at, self.freshness_thresholds, self._now())
        if not pools:
            # Default single-pool view: this integration's subscription is
            # one pool whose id is the provider id -- an integration fact.
            # The raw provider-native quota facts travel in the metadata the
            # caller assembled, so they are preserved verbatim on the pool.
            pools = (
                ResourcePool(
                    id=self.name,
                    provider=self.name,
                    plan=plan,
                    plan_source="provider_reported" if plan else "unknown",
                    remaining_percentage=remaining_percentage,
                    raw=dict(metadata or {}),
                    observed_at=observed_at,
                    freshness=freshness,
                    available=available,
                    reason=reason,
                ),
            )
        return ProviderStatus(
            name=self.name,
            available=available,
            remaining_percentage=remaining_percentage,
            observed_at=observed_at,
            freshness=freshness,
            metadata={"source_file": str(self.usage_file), **(metadata or {})},
            reason=reason,
            capacity=capacity,
            pools=pools,
            plan=plan,
        )

    def _unknown(self, reason: str) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            available=True,
            remaining_percentage=None,
            observed_at=None,
            freshness=Freshness.UNKNOWN,
            metadata={"source_file": str(self.usage_file)},
            reason=reason,
        )


def observation_from(payload: Mapping[str, Any]) -> datetime | None:
    """Honor explicit observation presence; only legacy shapes use aliases."""

    if "observed_at" in payload:
        return parse_timestamp(payload["observed_at"])
    for key in ("collected_at", "updated_at", "timestamp"):
        timestamp = parse_timestamp(payload.get(key))
        if timestamp is not None:
            return timestamp
    return None


def explicit_availability(payload: Mapping[str, Any]) -> bool:
    """Only an explicit JSON false makes a provider unavailable."""

    return payload.get("available") is not False
