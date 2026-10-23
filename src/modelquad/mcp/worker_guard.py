# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Best-effort worker ancestry detection; no observations are persisted."""

from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path

from .. import procutil
from ..execution.base import WORKER_ENV_VAR
from ..service.history import _SPAWN_GRACE_SECONDS, unresolved_worker_attempts

MAX_ANCESTOR_DEPTH = 32
ATTEMPT_CLOCK_TOLERANCE_SECONDS = 2.0
WORKER_REFUSAL = (
    "ModelQuad refuses worker MCP calls: a delegated worker must never re-enter "
    "orchestration and cannot report a host outcome."
)


def is_worker(db_path: str | Path) -> bool:
    """Check each call, so a terminal attempt never leaves a cached refusal.

    Layer 2 deliberately fails open on unreadable history or process metadata:
    an observation failure must not break a normal Host. It is not proof of
    worker exclusion. The environment layer remains unconditional.
    """
    if os.environ.get(WORKER_ENV_VAR) == "1":
        return True
    try:
        candidates = unresolved_worker_attempts(db_path)
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
            # Windows retains a departed parent's number in its child's row.
            # A newer process at that number is not an ancestor.
            if created is None or not math.isfinite(created) or created > child_created:
                break
            ancestors[parent] = created
            seen.add(parent)
            child, child_created = parent, created
        if not ancestors:
            return False
        for pid, started_at in candidates:
            if pid not in ancestors:
                continue
            started = datetime.fromisoformat(started_at)
            if started.tzinfo is None:
                continue
            # Intent precedes spawn. Bound creation to the existing spawn grace,
            # with two seconds before start for clock tolerance. Residual reuse
            # can only match a process created within this window while the row
            # is unresolved; spawn creation identity is not stored here.
            if (started.timestamp() - ATTEMPT_CLOCK_TOLERANCE_SECONDS
                    <= ancestors[pid] <= started.timestamp() + _SPAWN_GRACE_SECONDS
                    and procutil.pid_state(pid) == "alive"):
                # Recheck identity after liveness: a process can exit/reuse its
                # number between the snapshot and this check.
                if procutil.process_creation_epoch(pid) == ancestors[pid]:
                    return True
        return False
    except Exception:  # noqa: BLE001 -- layer 2 observation failure is not a Host refusal
        return False
