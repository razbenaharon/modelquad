# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Centralized helpers for reading provider credentials from the environment.

Credentials are read from the environment at call time and are never cached,
logged, persisted to SQLite, returned through any ``to_dict()``/serialization
method, or accepted as a value from YAML config -- only environment variable
names travel through config and code. This module is the one place that
touches ``os.environ`` for credentials so every provider follows the same rule.
"""

from __future__ import annotations

from collections.abc import Mapping
import os


def read_credential(
    env_var: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return a non-empty credential value from an injectable environment."""

    environment = os.environ if environ is None else environ
    return environment.get(env_var) or None


def credentials_configured(
    env_vars: tuple[str, ...],
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether every named credential is configured."""

    return all(read_credential(env_var, environ=environ) is not None for env_var in env_vars)
