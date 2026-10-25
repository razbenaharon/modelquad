# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Ride out a brief Windows sharing violation on a file rename or delete.

Antivirus scanners, backup tools and the search indexer open freshly written
files for a moment, without the sharing mode that lets another process
rename or delete them. Windows then refuses ``os.replace`` or ``unlink`` with
ERROR_SHARING_VIOLATION (32) or ERROR_ACCESS_DENIED (5) until the reader lets
go. This retries such a refusal for a short while; any other error, or the
same one past the deadline, is raised as it was.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import TypeVar

_T = TypeVar("_T")
#: ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION.
_TRANSIENT_WINERRORS = frozenset({5, 32})
RETRY_SECONDS = 2.0


def retry_sharing(operation: Callable[[], _T], *, seconds: float | None = None) -> _T:
    """Run ``operation``, retrying a transient Windows sharing refusal."""
    deadline = time.monotonic() + (RETRY_SECONDS if seconds is None else seconds)
    delay = 0.02
    while True:
        try:
            return operation()
        except PermissionError as error:
            if getattr(error, "winerror", None) not in _TRANSIENT_WINERRORS or time.monotonic() >= deadline:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 0.25)
