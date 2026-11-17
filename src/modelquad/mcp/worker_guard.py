# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Whether this process runs inside a Codex worker ModelQuad started.

A worker never asks Codex again (AGENTS.md invariant 1). Two layers:

1. The environment marker ``MODELQUAD_WORKER=1``, which every worker is
   started with. Unconditional.
2. Ancestry: while a worker runs, its process id and start time are kept in a
   small file under the state folder (``active-workers/``). A process that
   has such a worker among its ancestors is a worker even when the marker was
   dropped from its environment. Best effort: an observation that fails is
   not a refusal.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from .. import procutil
from ..execution.base import WORKER_ENV_VAR

MAX_ANCESTOR_DEPTH = 32
CLOCK_TOLERANCE_SECONDS = 2.0
#: A worker process appears within this long of the record being written.
SPAWN_GRACE_SECONDS = 120.0
#: Longer than any call may run (840 s); an older record is a leftover.
RECORD_SECONDS = 3600.0
FOLDER_NAME = "active-workers"
WORKER_REFUSAL = (
    "ModelQuad refuses worker MCP calls: a delegated worker must never re-enter "
    "orchestration and cannot report a host outcome."
)


def _folder(state_root: Path | None = None) -> Path:
    if state_root is None:
        from ..paths import state_root as _state_root

        state_root = _state_root()
    return Path(state_root) / FOLDER_NAME


def register(pid: int, *, state_root: Path | None = None, now: float | None = None) -> None:
    """Record a worker that has just been started. Never raises."""
    try:
        folder = _folder(state_root)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{int(pid)}.json").write_text(
            json.dumps({"pid": int(pid), "started_at": time.time() if now is None else now}),
            encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass


def unregister(pid: int | None, *, state_root: Path | None = None) -> None:
    """Forget a worker that ended. Never raises."""
    if pid is None:
        return
    try:
        (_folder(state_root) / f"{int(pid)}.json").unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def active_workers(*, state_root: Path | None = None, now: float | None = None) -> list[tuple[int, float]]:
    """(pid, start time) of each recorded worker; leftovers are removed."""
    now = time.time() if now is None else now
    found = []
    for entry in _folder(state_root).glob("*.json"):
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
            pid, started = record["pid"], record["started_at"]
            if type(pid) is not int or type(started) not in (int, float) or not math.isfinite(started):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if now - started > RECORD_SECONDS:
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        found.append((pid, float(started)))
    return found


def is_worker(state_root: Path | None = None) -> bool:
    """Check on each call, so an ended worker never leaves a cached refusal."""
    if os.environ.get(WORKER_ENV_VAR) == "1":
        return True
    try:
        candidates = active_workers(state_root=state_root)
        if not candidates:
            return False
        entries = procutil._windows_process_snapshot()
        if entries is None:
            return False
        parents = {pid: parent for pid, parent, _image in entries}
        child = os.getpid()
        child_created = procutil.process_creation_epoch(child)
        if child_created is None or not math.isfinite(child_created):
            return False
        ancestors: dict[int, float] = {}
        seen = {child}
        for _ in range(MAX_ANCESTOR_DEPTH):
            parent = parents.get(child)
            if not parent or parent in seen or parent not in parents:
                break
            created = procutil.process_creation_epoch(parent)
            # Windows keeps a departed parent's number in its child's row.
            # A newer process at that number is not an ancestor.
            if created is None or not math.isfinite(created) or created > child_created:
                break
            ancestors[parent] = created
            seen.add(parent)
            child, child_created = parent, created
        for pid, started in candidates:
            if pid not in ancestors:
                continue
            # The record is written when the worker is spawned: its creation
            # lies just before it. A reused number outside the window is not it.
            if (started - SPAWN_GRACE_SECONDS <= ancestors[pid] <= started + CLOCK_TOLERANCE_SECONDS
                    and procutil.pid_state(pid) == "alive"
                    and procutil.process_creation_epoch(pid) == ancestors[pid]):
                return True
        return False
    except Exception:  # noqa: BLE001 -- an observation failure is not a refusal
        return False
