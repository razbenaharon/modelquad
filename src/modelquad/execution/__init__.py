# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Provider execution layer.

Exports load on first use, so a hook that only needs the worker guard does
not import the executors.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import (
        DEFAULT_TIMEOUT_SECONDS,
        ExecutionRequest,
        ExecutionResult,
        Executor,
        WORKER_ENV_VAR,
        WorkerTaskStatus,
        append_completion_protocol,
        classify_worker_result,
        no_window_kwargs,
        parse_completion_envelope,
    )
    from .codex import CodexExecutor

_EXPORTS = {
    "DEFAULT_TIMEOUT_SECONDS": ".base",
    "ExecutionRequest": ".base",
    "ExecutionResult": ".base",
    "Executor": ".base",
    "WORKER_ENV_VAR": ".base",
    "WorkerTaskStatus": ".base",
    "append_completion_protocol": ".base",
    "classify_worker_result": ".base",
    "no_window_kwargs": ".base",
    "parse_completion_envelope": ".base",
    "CodexExecutor": ".codex",
}

__all__ = [
    "CodexExecutor",
    "DEFAULT_TIMEOUT_SECONDS",
    "ExecutionRequest",
    "ExecutionResult",
    "Executor",
    "WORKER_ENV_VAR",
    "WorkerTaskStatus",
    "append_completion_protocol",
    "classify_worker_result",
    "no_window_kwargs",
    "parse_completion_envelope",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
