# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The kinds of task a request can name."""

from __future__ import annotations

from enum import Enum

NETWORK_POLICY_REFUSED = "network_policy_refused"


class TaskType(str, Enum):
    """Only review, exploration and architecture run; the rest are named so they can be refused."""

    ARCHITECTURE = "architecture"
    IMPLEMENTATION = "implementation"
    EXPLORATION = "exploration"
    DEBUGGING = "debugging"
    TESTING = "testing"
    REVIEW = "review"
    TRIVIAL = "trivial"
