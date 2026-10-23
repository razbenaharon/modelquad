# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Shared types and writing helpers for collector-produced usage snapshots."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any

from ..adapters.atomicwrite import atomic_write
from ..procutil import no_window_kwargs


UsageSnapshot = dict[str, Any]

# Allow small clock corrections; W07 should use this same predicate for
# freshness and refusal recovery.
FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 120.0


def valid_timestamp_epoch(value: Any, *, now: datetime | None = None) -> float | None:
    """UTC epoch unless absent, invalid, or beyond the writer's UTC clock.

    Naive datetimes mean UTC. The injected clock (else real UTC) and the
    shared tolerance apply equally to incoming and persisted evidence.
    """

    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    epoch = value.timestamp()
    return epoch if epoch <= reference.timestamp() + FUTURE_TIMESTAMP_TOLERANCE_SECONDS else None


def write_usage_snapshot(path: str | Path, snapshot: Mapping[str, Any]) -> None:
    """Atomically write one provider snapshot as formatted JSON."""

    atomic_write(Path(path), json.dumps(dict(snapshot), indent=2) + "\n")


def read_usage_snapshot(path: str | Path) -> Mapping[str, Any] | None:
    """Read a snapshot back, or None when there is nothing readable there."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def observation_epoch(snapshot: Mapping[str, Any] | None) -> float | None:
    """When the *provider* produced this reading, as a UTC epoch, or None.

    Deliberately reads only ``observed_at``. ``collected_at`` says when we
    last looked, which every re-read advances and so can never order two
    readings against each other.
    """

    if not isinstance(snapshot, Mapping):
        return None
    value = snapshot.get("observed_at")
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# Independent processes share a persistent sibling advisory lock. A failed
# claim never permits a write: preserving older evidence is safer than an
# unlocked lost update. The separate refresh claim prevents duplicate probes.

#: How long a writer waits for another process's compare-and-write to finish.
SNAPSHOT_LOCK_WAIT_SECONDS = 2.0
_SNAPSHOT_LOCK_POLL_SECONDS = 0.005

if os.name == "nt":
    import msvcrt

    def _try_lock(descriptor: int) -> bool:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(descriptor: int) -> bool:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _snapshot_lock_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.lock")


def _claim_snapshot_lock(lock: Path, *, wait_seconds: float) -> int | None:
    """Take the advisory lock on ``lock``, waiting up to ``wait_seconds``.

    Returns the open descriptor that holds the lock, which the caller must
    hand back to :func:`_release_snapshot_lock`, or None when the lock could
    not be had in time (or the filesystem refused), in which case the caller
    must not publish. The file is opened without O_EXCL and never removed:
    its existence means nothing, only the kernel lock on it does.
    """

    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    deadline = time.monotonic() + wait_seconds
    while True:
        if _try_lock(descriptor):
            return descriptor
        if time.monotonic() >= deadline:
            with suppress(OSError):
                os.close(descriptor)
            return None
        time.sleep(_SNAPSHOT_LOCK_POLL_SECONDS)


def _release_snapshot_lock(descriptor: int) -> None:
    try:
        _unlock(descriptor)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.close(descriptor)


@contextmanager
def _snapshot_write_lock(target: Path) -> Iterator[bool]:
    """Hold the per-file lock for one read-compare-write, if it can be had.

    Yields True while held; False forbids publication.
    """

    descriptor = _claim_snapshot_lock(
        _snapshot_lock_path(target), wait_seconds=SNAPSHOT_LOCK_WAIT_SECONDS
    )
    try:
        yield descriptor is not None
    finally:
        if descriptor is not None:
            _release_snapshot_lock(descriptor)


def publish_usage_snapshot(
    path: str | Path,
    snapshot: Mapping[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
    merge: Callable[[UsageSnapshot, Path], Mapping[str, Any]] | None = None,
    publish_if: Callable[[], bool] | None = None,
) -> bool:
    """Read, merge and atomically publish under one shared per-file lock.

    ``merge(snapshot, target)`` runs inside the lock, including any reads it
    makes. Existing retention functions can be passed unchanged. False means
    the incoming evidence was not accepted (lock failure, fence, deadline,
    invalid future observation, or older quota without availability evidence).
    OSError from atomic replacement propagates; the lock is always released.

    ``probe_started_at`` fences all evidence across processes. Current
    availability remains in top-level ``available``/``reason``, independently
    ordered by ``availability_checked_at``. Neither stamp renews quota age.
    """

    target = Path(path)
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    raw = dict(snapshot)
    start = raw.get("probe_started_at", moment.isoformat())
    start_epoch = valid_timestamp_epoch(start, now=moment)
    if start_epoch is None:
        return False
    incoming_epoch = valid_timestamp_epoch(raw.get("observed_at"), now=moment)
    # Invalid/undated readings may update availability, but a future reading
    # is never published, even into an empty file.
    if observation_epoch(raw) is not None and incoming_epoch is None:
        return False
    with _snapshot_write_lock(target) as held:
        if not held or (publish_if is not None and not publish_if()):
            return False
        existing = read_usage_snapshot(target)
        prior_starts = [
            valid_timestamp_epoch((existing or {}).get(key), now=moment)
            for key in ("probe_started_at", "availability_checked_at")
        ]
        previous_start = max((epoch for epoch in prior_starts if epoch is not None), default=None)
        # The compatibility wrapper cannot know when its caller collected a
        # snapshot. Only an explicit collector start may fence a probe. Plain
        # quota writers remain ordered by observation and never lower a fence.
        if "probe_started_at" in raw and previous_start is not None and start_epoch < previous_start:
            return False
        merged = dict(merge(dict(raw), target)) if merge is not None else dict(raw)
        existing_observed = (existing or {}).get(
            "observed_at", (existing or {}).get("collected_at")
        )
        current_epoch = valid_timestamp_epoch(existing_observed, now=moment)
        accepts_quota = incoming_epoch is not None and (
            current_epoch is None or incoming_epoch >= current_epoch
        )
        # An undated input never erases quota, including future-poisoned quota.
        kept = dict(merged) if existing is None or accepts_quota else dict(existing)
        if existing is not None and not accepts_quota:
            # Readers of legacy snapshots use collected_at as observation time.
            # Freeze that original time BEFORE advancing the check time, or a
            # passive empty read would make old quota retire a runtime refusal.
            # Preserve explicitly null/invalid observed_at here. Current
            # readers still fall back to collected_at; W07 owns that fix.
            if "observed_at" not in existing:
                kept["observed_at"] = existing_observed
            for key in ("stale", "quota_error"):
                if key in merged:
                    kept[key] = merged[key]
            if raw.get("reason") or raw.get("quota_error"):
                kept["quota_error"] = raw.get("quota_error") or raw.get("reason")
        availability_epoch = valid_timestamp_epoch(
            (existing or {}).get("availability_checked_at"), now=moment
        )
        # A passive re-read proves recovery only if the provider observed it
        # after the stored availability check. Its write time is not evidence
        # that a signed-out runtime recovered.
        positive_availability_is_new = (
            "probe_started_at" in raw
            or availability_epoch is None
            or (incoming_epoch is not None and incoming_epoch > availability_epoch)
        )
        accepts_availability = (
            (availability_epoch is None or start_epoch >= availability_epoch)
            and (
                raw.get("available") is False
                or (accepts_quota and positive_availability_is_new
                    and (raw.get("available") is True or existing is None))
            )
        )
        if existing is not None:
            for key in ("available", "reason", "availability_checked_at"):
                if key in existing:
                    kept[key] = existing[key]
                else:
                    kept.pop(key, None)
        if accepts_availability:
            kept["available"] = raw.get("available", True)
            kept["availability_checked_at"] = start
            if raw.get("reason") is not None:
                kept["reason"] = raw["reason"]
            else:
                kept.pop("reason", None)
        if "probe_started_at" in raw:
            kept["probe_started_at"] = start
        elif existing is not None and "probe_started_at" in existing:
            kept["probe_started_at"] = existing["probe_started_at"]
        kept["collected_at"] = raw.get("collected_at") or moment.isoformat()
        if publish_if is not None and not publish_if():
            return False
        write_usage_snapshot(target, kept)
        return existing is None or accepts_quota or raw.get("available") is False or "probe_started_at" in raw


def write_usage_snapshot_if_newer(
    path: str | Path,
    snapshot: Mapping[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Compatibility wrapper; False on lock timeout, never writes unlocked."""

    return publish_usage_snapshot(path, snapshot, now=now)


def claim_usage_refresh(usage_directory: Path, provider: str) -> int | None:
    """Nonblocking cross-process single-flight claim, held through publication."""

    lock = Path(usage_directory) / f"usage-refresh-{provider}.lock"
    return _claim_snapshot_lock(lock, wait_seconds=0.0)


def release_usage_refresh(descriptor: int) -> None:
    _release_snapshot_lock(descriptor)
