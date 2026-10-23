# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Deterministic local estimator for expected task resource consumption. Pure function of its inputs: no I/O, no clock, no randomness. Produces EVIDENCE (ExpectedConsumption with source=ESTIMATED), never a routing decision; the router owns how evidence influences recommendations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import ConsumptionBucket, ConsumptionSource, ExpectedConsumption

if TYPE_CHECKING:
    from .capsule import TaskCapsule


# Character thresholds for task prompt and capsule content:
# - Under 4,000 characters is considered LOW consumption.
# - Between 4,000 and 19,999 characters is MEDIUM consumption.
# - 20,000 characters or more is HIGH consumption.
CHAR_MEDIUM_THRESHOLD = 4_000
CHAR_HIGH_THRESHOLD = 20_000

# Relevant file count thresholds:
# - Under 8 files is LOW consumption.
# - Between 8 and 19 files is MEDIUM consumption.
# - 20 files or more is HIGH consumption.
FILES_MEDIUM_THRESHOLD = 8
FILES_HIGH_THRESHOLD = 20

_BUCKET_ORDER = {
    ConsumptionBucket.LOW: 0,
    ConsumptionBucket.MEDIUM: 1,
    ConsumptionBucket.HIGH: 2,
}


def _classify_count(count: int, medium_threshold: int, high_threshold: int) -> ConsumptionBucket:
    """Map a numerical count to a ConsumptionBucket tier based on thresholds."""
    if count >= high_threshold:
        return ConsumptionBucket.HIGH
    if count >= medium_threshold:
        return ConsumptionBucket.MEDIUM
    return ConsumptionBucket.LOW


def estimate_expected_consumption(
    prompt: str | None = None,
    capsule: TaskCapsule | None = None,
) -> ExpectedConsumption:
    """Estimate expected task resource consumption from prompt length and capsule context.

    Pure function of its inputs: produces an ExpectedConsumption evidence object
    with source=ESTIMATED. Never makes a routing decision.
    """
    text_chars = len(prompt or "") + (len(capsule.to_prompt()) if capsule is not None else 0)
    file_count = len(capsule.relevant_files) if capsule is not None else 0

    char_bucket = _classify_count(text_chars, CHAR_MEDIUM_THRESHOLD, CHAR_HIGH_THRESHOLD)
    file_bucket = _classify_count(file_count, FILES_MEDIUM_THRESHOLD, FILES_HIGH_THRESHOLD)

    final_bucket = max(char_bucket, file_bucket, key=lambda b: _BUCKET_ORDER[b])
    basis = f"estimated from ~{text_chars} prompt chars and {file_count} relevant files"

    return ExpectedConsumption(
        bucket=final_bucket,
        source=ConsumptionSource.ESTIMATED,
        basis=basis,
    )
