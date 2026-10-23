# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Non-launching lifecycle facts used by the existing execution boundaries.

Caller departure is independent of terminal execution evidence. These records
start no work, observe no process, and choose no deadline or persistence policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class CallerSnapshot:
    """One consistent view of an attempt identity and its caller's departure."""

    identity: tuple[Any, int] | None = None
    abandoned_reason: str | None = None


class CallerWaitHandle:
    """Publish an attempt once, retaining departure on either side of publication.

    The MCP observer records departure and then reads identity; the publishing
    thread publishes identity and then reads departure. Both updates share a
    lock, so whichever update runs second can see both facts. Reading an older
    snapshot is safe: snapshots cannot change, and departure asserts nothing
    about worker completion or termination.
    """

    def __init__(
        self,
        identity: tuple[Any, int] | None = None,
        abandoned_reason: str | None = None,
    ) -> None:
        self._lock = Lock()
        self._snapshot = CallerSnapshot()
        if identity is not None:
            self.publish(*identity)
        if abandoned_reason is not None:
            self.abandoned_reason = abandoned_reason

    @property
    def snapshot(self) -> CallerSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def identity(self) -> tuple[Any, int] | None:
        return self.snapshot.identity

    def publish(self, db_path: Any, attempt_id: int) -> None:
        """Bind once; identical publication is harmless, rebinding is an error."""
        if (
            db_path is None
            or not isinstance(attempt_id, int)
            or isinstance(attempt_id, bool)
            or attempt_id <= 0
        ):
            raise ValueError("an attempt identity requires a database and a positive integer id")
        identity = (db_path, attempt_id)
        with self._lock:
            previous = self._snapshot
            if previous.identity is not None:
                if previous.identity != identity:
                    raise ValueError("caller wait handle is already bound to another attempt")
                return
            self._snapshot = CallerSnapshot(identity, previous.abandoned_reason)

    @property
    def abandoned_reason(self) -> str | None:
        return self.snapshot.abandoned_reason

    @abandoned_reason.setter
    def abandoned_reason(self, reason: str | None) -> None:
        """Keep the first departure, as the existing history recorder does."""
        if reason is None:
            return
        with self._lock:
            previous = self._snapshot
            if previous.abandoned_reason is None:
                self._snapshot = CallerSnapshot(previous.identity, reason)

    @property
    def db_path(self) -> Any:
        identity = self.identity
        return None if identity is None else identity[0]

    @property
    def attempt_id(self) -> int | None:
        identity = self.identity
        return None if identity is None else identity[1]


@dataclass(frozen=True)
class TerminalResolution:
    """The fixed attempt verdict, separate from the worker's objective claim.

    Captured from a normalized execution result, not a caller transport event.
    An attempt may complete with an unknown or failed worker objective. A
    timeout with no confirmed tree death remains a timeout.
    """

    status: str
    failure_kind: str | None
