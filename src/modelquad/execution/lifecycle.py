# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The fixed attempt verdict of an execution result.

It starts no work, observes no process, and chooses no deadline or persistence policy.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalResolution:
    """The fixed attempt verdict, separate from the worker's objective claim.

    Captured from a normalized execution result, not a caller transport event.
    An attempt may complete with an unknown or failed worker objective. A
    timeout with no confirmed tree death remains a timeout.
    """

    status: str
    failure_kind: str | None
