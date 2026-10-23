# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Fresh runtime quota refusals, recorded beside the periodic health checks.

A provider's usage snapshot and a provider's actual answer to a request are
two different observations, and on 2026-09-06 they disagreed twice: Codex
refused work at 10:30Z while its snapshot read 33% and kept reading 33% until
13:23Z, then refused again at 14:04Z while the pool read `available: true`.
Principle 3 is the rule that settles such a disagreement --
fresh operational evidence overrides stale quota state.

This module owns the decision-time I/O around that fact: it writes a refusal
into the existing ``provider_health`` table (no schema change) and reads back
the newest one per provider. It never reads quota itself: quota is
display-only, and a decision never starts or waits for a quota read. The POLICY -- how long a refusal stands, whether it still overrules
a given snapshot, and what the resulting status says -- lives in the pure
``quota_policy`` module, which has no database at all.

What a refusal must NOT do is edit the quota numbers. The refusal is an
availability fact; the percentage is an observation nobody re-measured. The
overlay in ``quota_policy.apply_refusal_overlay`` therefore rewrites only
``available`` and ``reason``, and the snapshot stays honest about what it saw.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
import logging
import sqlite3

from ..models import ProviderStatus
from ..quota_policy import QuotaRefusal, apply_refusal_overlay
from . import db as _db

_logger = logging.getLogger(__name__)


#: Marker prefix on ``provider_health.detail``. The periodic doctor poll writes
#: rows about CLI and adapter presence into the same table; the prefix is what
#: keeps the two kinds of row from being mistaken for each other, in both
#: directions -- a doctor row never reads as a refusal, and a doctor row never
#: silently clears one either. Those answer different questions: "is the CLI
#: installed" cannot retire "the provider refused the work sixty seconds ago".
REFUSAL_PREFIX = "quota_refusal:"

#: Marker prefix for the explicit clear. A refusal ends when the provider
#: actually serves work again (or its window resets, or the TTL expires) --
#: this is how the first of those is recorded.
RECOVERY_PREFIX = "quota_recovery:"

#: How far back a refusal is even worth reading -- a scan bound, NOT a policy.
#:
#: The TTL (1800 s) is only the default for a refusal whose window reset is
#: unknown; ``quota_policy.refusal_in_force`` keeps a refusal in force until a
#: KNOWN reset however far away that is. A lookback of twice the TTL therefore
#: silently capped that clause: Codex refused at 14:04Z naming a 17:58Z reset,
#: and an hour later the row was already outside a 3600 s window, so the
#: provider read available again three hours before its own quota said so.
#:
#: Seven days is the longest reset window any provider ModelQuad drives
#: reports (Codex's ``seven_day``), so it is
#: the smallest bound that cannot truncate a real reset clause. The scan stays
#: bounded and indexed on ``checked_at``; deciding whether a returned row still
#: overrules a snapshot remains ``refusal_in_force``'s job, not this one's.
REFUSAL_LOOKBACK_SECONDS = 7 * 24 * 3600

#: Backwards-compatible alias for the private name this constant used to have.
_LOOKBACK_SECONDS = REFUSAL_LOOKBACK_SECONDS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_quota_refusal(
    db_path: str | Path,
    provider: str,
    *,
    window: str | None = None,
    observed_at: datetime | None = None,
    detail: str | None = None,
) -> None:
    """Record that ``provider`` refused work for a quota/rate-limit reason.

    Writes one ``provider_health`` row with ``available=0`` and a
    ``quota_refusal:`` detail. Additive: no schema change, no new table, and
    nothing else in the product has to know this row exists.

    Silent on failure, exactly like ``record_provider_health``: losing one
    observation must never break the execution or routing path that produced
    it.

    The executor-side caller is ``ExecutionManager._record_quota_outcome``,
    which classifies a finished run with
    ``execution/base.quota_refusal_evidence`` -- Codex's typed
    ``rate_limit_events`` first, the coarse ``stderr_category`` second, and
    neither on a run that succeeded. It names no window: a refusal derived
    from a result whose stderr body was discarded cannot honestly say which
    cap was hit, and ``quota_policy.refusal_in_force`` already reads an
    unnamed window against the snapshot's binding reset.
    """

    stamp = (observed_at or _now())
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    suffix = window or "unknown_window"
    body = f"{REFUSAL_PREFIX}{suffix}"
    if detail:
        body = f"{body} | {detail}"
    _insert(db_path, provider, stamp, available=0, detail=body)


def record_quota_recovery(
    db_path: str | Path,
    provider: str,
    *,
    observed_at: datetime | None = None,
    detail: str | None = None,
) -> None:
    """Record that ``provider`` served work again, clearing any refusal.

    The "cleared by a newer successful run or a refresh reporting
    availability" half. A recovery row is newer than the refusal it follows,
    so :func:`fetch_quota_refusals` stops reporting that provider.
    """

    stamp = (observed_at or _now())
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    body = f"{RECOVERY_PREFIX}served"
    if detail:
        body = f"{body} | {detail}"
    _insert(db_path, provider, stamp, available=1, detail=body)


def _insert(
    db_path: str | Path,
    provider: str,
    stamp: datetime,
    *,
    available: int,
    detail: str,
) -> None:
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        conn.execute(
            "INSERT INTO provider_health "
            "(provider, checked_at, available, cli_detected, adapter_installed, detail) "
            "VALUES (?, ?, ?, NULL, NULL, ?)",
            (provider, stamp.isoformat(), available, detail),
        )
        conn.commit()
    except (sqlite3.Error, OSError):
        pass
    finally:
        conn.close()


def fetch_quota_refusals(
    db_path: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[QuotaRefusal, ...]:
    """The newest quota refusal per provider within the lookback, newest first.

    "Within the lookback" is a scan bound (``REFUSAL_LOOKBACK_SECONDS``, seven
    days -- the longest reset window any provider reports), NOT the TTL. A
    refusal naming a reset four hours out must still be readable four hours
    later, or the "until the known reset" clause would be capped by the reader.

    A provider whose newest quota-marked row is a RECOVERY is absent from the
    result: the refusal it followed has been answered by the provider itself.
    Only quota-marked rows are considered at all -- the periodic doctor rows
    in the same table describe CLI presence and are silent on quota.

    Whether a returned refusal still OVERRULES a given snapshot is
    ``quota_policy.refusal_in_force``'s question, not this function's: reading
    and deciding are kept apart so the decision stays testable without a
    database.
    """

    reference = now or _now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff = datetime.fromtimestamp(
        reference.timestamp() - REFUSAL_LOOKBACK_SECONDS, tz=timezone.utc
    ).isoformat()
    try:
        conn = _db.connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return ()
    try:
        rows = conn.execute(
            "SELECT provider, checked_at, available, detail FROM provider_health "
            "WHERE checked_at >= ? AND (detail LIKE ? OR detail LIKE ?) "
            "ORDER BY checked_at DESC, id DESC",
            (cutoff, f"{REFUSAL_PREFIX}%", f"{RECOVERY_PREFIX}%"),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return ()
    finally:
        conn.close()

    refusals: list[QuotaRefusal] = []
    seen: set[str] = set()
    for row in rows:
        provider = str(row["provider"])
        if provider in seen:
            continue
        seen.add(provider)
        detail = row["detail"] or ""
        if not detail.startswith(REFUSAL_PREFIX):
            continue  # the newest quota-marked row is a recovery
        observed = _parse(row["checked_at"])
        if observed is None:
            continue
        marker = detail[len(REFUSAL_PREFIX) :].split(" | ", 1)[0].strip()
        refusals.append(
            QuotaRefusal(
                provider=provider,
                observed_at=observed,
                window=marker if marker and marker != "unknown_window" else None,
                detail=detail,
            )
        )
    return tuple(refusals)


def overlay_quota_refusals(
    statuses: Iterable[ProviderStatus],
    db_path: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[ProviderStatus, ...]:
    """Read the refusals and apply them -- the one call a routing path needs.

    The whole database half of principle 3 in a single function, so the hook in
    a routing surface is one line and every rule it applies stays in the pure
    module beneath it.
    """

    reference = now or _now()
    return apply_refusal_overlay(statuses, fetch_quota_refusals(db_path, now=reference), reference)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
