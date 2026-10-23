# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Explicit opt-in to technical details in MCP decision presentations."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEV_MODE_ENV = "MODELQUAD_DEV_MODE"


def is_dev_mode(environ: Mapping[str, str] | None = None) -> bool:
    """True only when the explicit developer-mode flag is set."""

    env = environ if environ is not None else os.environ
    return env.get(DEV_MODE_ENV) == "1"
