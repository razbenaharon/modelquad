# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""One way to ask every provider for the freshest usage it will give us.

Three distinct things are deliberately not conflated here:

``collected_at``
    when *we* last looked at the source.
``observed_at``
    when the *provider* produced the reading. Only this moving means the
    usage data actually got newer.
``refresh_kind``
    how the reading was obtained -- ``active`` when the provider went and got
    a new observation for us, ``passive`` when all we could do was re-read
    something it wrote earlier, ``unavailable`` when we got nothing.

A refresh that only advances ``collected_at`` is reported as such rather than
being dressed up as fresh data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, wait
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Thread
import time
from typing import Any

from .base import (
    claim_usage_refresh, release_usage_refresh,
    publish_usage_snapshot, valid_timestamp_epoch,
)
from .claude import CLAUDE_STATUSLINE_SOURCE


# How long a provider may go without a fresh probe. Every refresher below is
# either a local file read or a local CLI/IPC call that costs no model tokens,
# so this only exists to stop redundant work, not to ration anything.
MIN_REFRESH_INTERVAL_SECONDS = 60.0

# Per-provider ceiling, so one wedged CLI cannot hold up the others.
PROVIDER_REFRESH_TIMEOUT = 60.0

# Ceiling for a whole refresh_all() sweep, so a caller (an HTTP API request,
# say) is never blocked indefinitely.
TOTAL_REFRESH_TIMEOUT = 90.0

ACTIVE = "active"
PASSIVE = "passive"
UNAVAILABLE = "unavailable"

# Mirrors the router's FreshnessThresholds defaults (config default.yaml) so a
# card in the UI and a routing decision never disagree about what "fresh" is.
FRESH_SECONDS = 300.0
STALE_SECONDS = 3600.0
CLAUDE_LOCAL_NOTE = 'Claude quota cannot be requested directly; this is its latest local observation.'
CLAUDE_LOCAL_TIMEOUT = 2.0
SNAPSHOT_READ_CAP = 1024 * 1024


_refresh_deadline: ContextVar[float | None] = ContextVar("usage_refresh_deadline", default=None)


@contextmanager
def refresh_deadline(deadline: float) -> Iterator[None]:
    """Fence publication without changing the injected refresher's signature.

    Scoped to this probe thread, including wrappers calling refresh_provider.
    """

    token = _refresh_deadline.set(deadline)
    try:
        yield
    finally:
        _refresh_deadline.reset(token)


@dataclass(frozen=True)
class RefreshResult:
    """What one provider's refresh attempt actually achieved."""

    provider: str
    refresh_kind: str
    available: bool
    collected_at: str
    observed_at: str | None = None
    previous_observed_at: str | None = None
    remaining_percentage: float | None = None
    source: str | None = None
    error: str | None = None
    debounced: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    published: bool | None = None

    @property
    def newer_observation(self) -> bool:
        """True only when the provider really produced a newer reading."""

        reference = _datetime_of(self.collected_at)
        if self.published is False or reference is None:
            return False
        incoming = valid_timestamp_epoch(self.observed_at, now=reference)
        previous = valid_timestamp_epoch(self.previous_observed_at, now=reference)
        return incoming is not None and (previous is None or incoming > previous)

    @property
    def observation_age_seconds(self) -> float | None:
        """How old the provider's own reading is, or None when there is none.

        Deliberately measured from ``observed_at``: the age a user cares about
        is the age of the quota reading, not of our last look at it.
        """

        reference = _datetime_of(self.collected_at)
        if reference is None:
            return None
        observed = valid_timestamp_epoch(self.observed_at, now=reference)
        collected = reference.timestamp()
        if observed is None or collected is None:
            return None
        return max(0.0, collected - observed)

    @property
    def freshness(self) -> str:
        """Classify the observation the same way the router's thresholds do."""

        age = self.observation_age_seconds
        if age is None:
            return "unknown"
        if age <= FRESH_SECONDS:
            return "fresh"
        if age <= STALE_SECONDS:
            return "stale"
        return "very_stale"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "refresh_kind": self.refresh_kind,
            "available": self.available,
            "collected_at": self.collected_at,
            "observed_at": self.observed_at,
            "previous_observed_at": self.previous_observed_at,
            "newer_observation": self.newer_observation,
            "observation_age_seconds": self.observation_age_seconds,
            "freshness": self.freshness,
            "remaining_percentage": self.remaining_percentage,
            "source": self.source,
            "error": self.error,
            "debounced": self.debounced,
            "metadata": dict(self.metadata),
            "published": self.published,
            **({'note': CLAUDE_LOCAL_NOTE} if self.provider == 'claude' else {}),
        }


def _now_iso(now: Callable[[], datetime] | None) -> str:
    return (now or (lambda: datetime.now(timezone.utc)))().isoformat()


def _monotonic_wall(now: Callable[[], datetime] | None) -> float:
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


# --------------------------------------------------------------------------
# Cross-process debounce
# --------------------------------------------------------------------------
#
# MCP server, CLI and the background service are separate processes, so
# an in-process guard cannot stop them probing the same provider at once. The
# coordination point is therefore a file, which every one of them can see:
#
#   usage-refresh-<provider>.lock   held with an OS advisory lock
#
# What is held is the kernel's lock, not the file: the sibling is opened
# without O_EXCL, its existence means nothing, and it is never unlinked. That
# is the whole correctness argument, and the previous O_EXCL-plus-stale-
# takeover scheme had neither half of it. Two contenders could read the
# same stale timestamp, and the second would then unlink the lock the first had
# just re-created and probe alongside it; and release unlinked by pathname, so
# a process could remove a lock it no longer owned. A lock whose owner dies is
# released by the kernel, which is why no timestamp, no staleness horizon and
# no takeover survive here.
#
# The claim is non-blocking. The debounce's job is to stop a duplicate probe,
# so its loser reports the snapshot it already has rather than queueing behind
# the winner, which keeps callers responsive and makes deadlock
# impossible. The snapshot write lock in ``base.py`` is deliberately the other
# way round: it covers every writer and its loser waits. Nothing here needs a
# Windows API or a running daemon.


def _lock_path(usage_directory: Path, provider: str) -> Path:
    return Path(usage_directory) / f"usage-refresh-{provider}.lock"


def _claim(usage_directory: Path, provider: str) -> int | None:
    """Take the refresh lock for one provider, without ever waiting for it.

    Returns the open descriptor that holds the lock, which the caller must hand
    back to :func:`_release`, or None when another process holds it (or the
    filesystem refused us the file at all -- an unusable lock directory means
    we cannot coordinate, and not probing is the safe reading of that).
    """

    return claim_usage_refresh(usage_directory, provider)


def _release(descriptor: int) -> None:
    release_usage_refresh(descriptor)


def _snapshot_path(usage_directory: Path, filename: str) -> Path:
    return Path(usage_directory) / filename


def _read_snapshot(path: Path) -> Mapping[str, Any] | None:
    try:
        with path.open('rb') as stream:
            raw = stream.read(SNAPSHOT_READ_CAP + 1)
        if len(raw) > SNAPSHOT_READ_CAP:
            return None
        payload = json.loads(raw.decode('utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _datetime_of(value: Any) -> datetime | None:
    epoch = _epoch_of(value)
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else None


def _epoch_of(value: Any) -> float | None:
    """Parse an ISO-8601 stamp into a UTC epoch, or None."""

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


def _seconds_since_last_check(
    snapshot: Mapping[str, Any],
    path: Path,
    *,
    wall: float,
) -> float | None:
    """How long since we last looked at this provider's source.

    Prefers the snapshot's own ``collected_at`` -- that is precisely "when we
    last checked", and it stays on the same clock the caller supplied. File
    mtime is only a fallback, and deliberately not the primary signal: it
    lives on the wall clock, so an injected/logical ``now`` would otherwise
    compare two different clocks against each other.
    """

    recorded = _epoch_of(snapshot.get("collected_at"))
    if recorded is not None:
        return wall - recorded
    try:
        return wall - path.stat().st_mtime
    except OSError:
        return None


# --------------------------------------------------------------------------
# Per-provider refreshers
# --------------------------------------------------------------------------


def _refresh_codex(usage_directory: Path, *, now: Callable[[], datetime] | None) -> dict[str, Any]:
    """Ask Codex's app-server for current limits; the only Codex quota source.

    ``account/rateLimits/read`` makes Codex fetch current limits from its
    backend. When that cannot be reached the reading stays unknown (every
    percentage None, no ``observed_at``); nothing is guessed from older files.
    """

    from .codex_appserver import collect_codex_appserver_usage

    return dict(collect_codex_appserver_usage(now=now))


def _persisted_claude_reading(
    usage_directory: Path, *, now: Callable[[], datetime] | None
) -> dict[str, Any] | None:
    """The last Claude reading written to disk, or None when there is none.

    This is Claude Code's statusLine push. The recorded ``source`` is trusted
    when present and only assumed to be the statusLine for legacy files that
    predate the field.
    """

    snapshot = _read_snapshot(_snapshot_path(usage_directory, "usage-claude.json"))
    if snapshot is None:
        return None
    payload = dict(snapshot)
    payload["refresh_kind"] = PASSIVE
    payload.setdefault("source", CLAUDE_STATUSLINE_SOURCE)
    # Legacy snapshots carry only a collection time, but a push that carried
    # real percentages WAS a provider observation made at that moment. Newer
    # pushes stamp observed_at themselves; this backfills the older shape.
    if "observed_at" not in payload and (
        payload.get("five_hour_used_percentage") is not None
        or payload.get("seven_day_used_percentage") is not None
    ):
        payload["observed_at"] = payload.get("collected_at")
    # Explain the gap only when there is actually a gap. This note is reported
    # as the result's `error`, so attaching it to a perfectly good reading would
    # dress a successful refresh up as a failed one; a reading that IS dated
    # already says so through its freshness.
    if payload.get("observed_at") is None and payload.get("source") == CLAUDE_STATUSLINE_SOURCE:
        payload["reason"] = (
            "Claude Code has no command that reports rate limits; its usage only "
            "advances when a running Claude Code session pushes a statusLine update."
        )
    return payload


def _refresh_claude(usage_directory: Path, *, now: Callable[[], datetime] | None) -> dict[str, Any]:
    """Re-read the last Claude reading Claude Code's statusLine pushed.

    Claude Code exposes no command that reports subscription rate limits
    (`claude auth status` returns the plan but no quota, and there is no
    usage/status subcommand), so nothing here can *ask* for a new reading.
    The statusLine pushes live rate limits while an interactive session is
    open; without a push the reading stays unknown.
    """

    persisted = _persisted_claude_reading(usage_directory, now=now)
    if persisted is not None:
        return persisted
    return {
        "provider": "claude",
        "available": True,
        "source": CLAUDE_STATUSLINE_SOURCE,
        "refresh_kind": UNAVAILABLE,
        "observed_at": None,
        "collected_at": _now_iso(now),
        "reason": "No Claude usage snapshot has been pushed yet.",
    }


REFRESHERS: dict[str, Callable[..., dict[str, Any]]] = {
    "codex": _refresh_codex,
    "claude": _refresh_claude,
}

SNAPSHOT_FILENAMES = {
    "codex": "usage-codex.json",
    "claude": "usage-claude.json",
}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _remaining_of(snapshot: Mapping[str, Any]) -> float | None:
    """Best-effort headline remaining value, whatever shape the provider uses.

    A pooled figure wins when present; Codex publishes per-window
    remaining values; Claude publishes only *used* percentages. The tightest
    window wins, matching how the router already constrains quota.
    """

    pooled = _finite(snapshot.get("effective_remaining_percentage"))
    if pooled is not None:
        return pooled

    candidates: list[float] = []
    for window in ("five_hour", "seven_day"):
        remaining = _finite(snapshot.get(f"{window}_remaining_percentage"))
        if remaining is None:
            used = _finite(snapshot.get(f"{window}_used_percentage"))
            if used is not None:
                remaining = 100.0 - used
        if remaining is not None:
            candidates.append(remaining)
    return min(candidates) if candidates else None


def refresh_provider(
    provider: str,
    usage_directory: Path | str,
    *,
    now: Callable[[], datetime] | None = None,
    min_interval: float = MIN_REFRESH_INTERVAL_SECONDS,
    force: bool = False,
    refreshers: Mapping[str, Callable[..., dict[str, Any]]] | None = None,
    _deadline: float | None = None,
    _monotonic: Callable[[], float] = time.monotonic,
) -> RefreshResult:
    """Ask one provider for the freshest usage it will give us.

    Quota is retained on an undated response; explicit unavailability is
    persisted separately. Lock/deadline/fence failures report non-publication.
    """

    directory = Path(usage_directory)
    if _deadline is None:
        _deadline = _refresh_deadline.get()
    table = refreshers if refreshers is not None else REFRESHERS
    collected_at = _now_iso(now)
    wall = _monotonic_wall(now)
    filename = SNAPSHOT_FILENAMES.get(provider, f"usage-{provider}.json")
    target = _snapshot_path(directory, filename)
    previous = _read_snapshot(target) or {}
    # Older snapshot shapes carry no observation time at all. Falling back to
    # their collection time keeps the "is this newer?" comparison honest: an
    # absent baseline would otherwise make simply re-reading the same old
    # reading look like a brand-new observation.
    previous_observed = previous.get("observed_at", previous.get("collected_at"))

    handler = table.get(provider)
    if handler is None:
        return RefreshResult(
            provider=provider,
            refresh_kind=UNAVAILABLE,
            available=True,
            collected_at=collected_at,
            previous_observed_at=previous_observed,
            error=f"No refresher is registered for provider {provider!r}.",
        )

    age = _seconds_since_last_check(previous, target, wall=wall)
    if not force and age is not None and 0 <= age < min_interval:
        # Recently refreshed by someone -- possibly another process. Report
        # what is already on disk instead of probing again.
        return RefreshResult(
            provider=provider,
            refresh_kind=PASSIVE,
            available=previous.get("available", True),
            collected_at=collected_at,
            observed_at=previous_observed,
            previous_observed_at=previous_observed,
            remaining_percentage=_remaining_of(previous),
            source=previous.get("source"),
            debounced=True,
        )

    lock = _claim(directory, provider)
    if lock is None:
        # Another process is refreshing this provider right now. Do not queue
        # behind it; serve what we have so no caller is ever blocked.
        return RefreshResult(
            provider=provider,
            refresh_kind=PASSIVE,
            available=previous.get("available", True),
            collected_at=collected_at,
            observed_at=previous_observed,
            previous_observed_at=previous_observed,
            remaining_percentage=_remaining_of(previous),
            source=previous.get("source"),
            debounced=True,
        )

    # Capture before the handler: collectors date at completion, which cannot
    # fence a stalled result against evidence from another process.
    started_at = _now_iso(now)
    try:
        try:
            snapshot = dict(handler(directory, now=now))
        except Exception as exc:  # noqa: BLE001 - isolate provider failure
            return RefreshResult(
                provider=provider, refresh_kind=UNAVAILABLE,
                available=previous.get("available", True), collected_at=collected_at,
                observed_at=previous_observed, previous_observed_at=previous_observed,
                remaining_percentage=_remaining_of(previous), source=previous.get("source"),
                error=f"{type(exc).__name__}: {exc}", published=False,
            )
        collected_at = _now_iso(now)
        snapshot["collected_at"] = collected_at
        snapshot["probe_started_at"] = started_at
        try:
            published = publish_usage_snapshot(
                target, snapshot, now=now,
                publish_if=(lambda: _monotonic() < _deadline) if _deadline is not None else None,
            )
            persistence_error = None if published else "lock, ordering fence, invalid time or deadline"
        except OSError as exc:
            published = False
            persistence_error = str(exc)
        stored = _read_snapshot(target) or previous
        if not published:
            return RefreshResult(
                provider=provider, refresh_kind=str(snapshot.get("refresh_kind") or PASSIVE),
                available=stored.get("available", True), collected_at=collected_at,
                observed_at=stored.get("observed_at"), previous_observed_at=previous_observed,
                remaining_percentage=_remaining_of(stored), source=stored.get("source"),
                error=f"Reading obtained but not persisted: {persistence_error}", published=False,
            )
        observed = stored.get("observed_at")
        kind = str(snapshot.get("refresh_kind") or PASSIVE)
        if snapshot.get("observed_at") is None:
            kind = UNAVAILABLE
        return RefreshResult(
            provider=provider, refresh_kind=kind,
            available=bool(stored.get("available", True)), collected_at=collected_at,
            observed_at=observed, previous_observed_at=previous_observed,
            remaining_percentage=_remaining_of(stored), source=stored.get("source"),
            error=snapshot.get("reason") or snapshot.get("quota_error"), published=True,
            metadata={"availability_reported": snapshot.get("available")},
        )
    finally:
        _release(lock)


def daemon_future(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """Run bounded collectors without executor threads joined at process exit.

    The collector's cross-process claim remains held until it actually ends.
    A process exit mid atomic-write can leave a private sibling *.tmp file;
    it cannot replace the existing JSON with a partial file.
    """

    future = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)

    Thread(target=run, daemon=True, name="usage-refresh").start()
    return future


def refresh_all(
    usage_directory: Path | str,
    *,
    providers: Iterable[str] | None = None,
    now: Callable[[], datetime] | None = None,
    min_interval: float = MIN_REFRESH_INTERVAL_SECONDS,
    force: bool = False,
    per_provider_timeout: float = PROVIDER_REFRESH_TIMEOUT,
    total_timeout: float = TOTAL_REFRESH_TIMEOUT,
    refreshers: Mapping[str, Callable[..., dict[str, Any]]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, RefreshResult]:
    """Concurrent daemon probes, one deadline, and no join on stalled work.

    The shared budget is the smaller of the sweep and provider ceilings:
    60 seconds at the defaults, rather than the prior effective 90 seconds.
    """

    table = refreshers if refreshers is not None else REFRESHERS
    names = list(dict.fromkeys(providers if providers is not None else table))
    if not names:
        return {}
    started = monotonic()
    deadline = started + max(0.0, min(total_timeout, per_provider_timeout))
    claude_deadline = min(deadline, started + CLAUDE_LOCAL_TIMEOUT)
    # Capture last-good data inside the same time budget, off the caller's
    # thread. A stalled snapshot read must not turn timeout into another read.
    previous_reads = {
        name: daemon_future(_read_snapshot, Path(usage_directory) / SNAPSHOT_FILENAMES[name])
        for name in names if name in SNAPSHOT_FILENAMES
    }
    futures = {
        name: daemon_future(
            refresh_provider, name, usage_directory, now=now,
            min_interval=min_interval, force=force, refreshers=table,
            _deadline=claude_deadline if name == 'claude' else deadline, _monotonic=monotonic,
        )
        for name in names
    }
    expired_claude = False
    if 'claude' in futures:
        done_claude, _ = wait([futures['claude']], timeout=max(0.0, claude_deadline - monotonic()))
        expired_claude = not done_claude
    waiting = [future for name, future in futures.items() if name != 'claude' or not expired_claude]
    done, _ = wait(waiting, timeout=max(0.0, deadline - monotonic()))
    results = {}
    for name, future in futures.items():
        if future not in done:
            future.cancel()
            previous = {}
            prior = previous_reads.get(name)
            if prior is not None and prior.done():
                try:
                    previous = prior.result() or {}
                except Exception:
                    pass
            results[name] = RefreshResult(
                provider=name, refresh_kind=UNAVAILABLE,
                available=previous.get('available', True), collected_at=_now_iso(now),
                observed_at=previous.get('observed_at'), previous_observed_at=previous.get('observed_at'),
                remaining_percentage=_remaining_of(previous), source=previous.get('source'),
                error="Refresh exceeded its time budget.", published=False,
            )
            continue
        try:
            results[name] = future.result()
        except Exception as exc:  # noqa: BLE001 - isolate provider failure
            results[name] = RefreshResult(
                provider=name, refresh_kind=UNAVAILABLE,
                available=True, collected_at=_now_iso(now),
                error=f"{type(exc).__name__}: {exc}", published=False,
            )
    return results
