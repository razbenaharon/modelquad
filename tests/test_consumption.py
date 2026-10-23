# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for deterministic task resource consumption estimator and models."""

from __future__ import annotations

import pytest

from modelquad.capsule import TaskCapsule
from modelquad.consumption import (
    CHAR_HIGH_THRESHOLD,
    CHAR_MEDIUM_THRESHOLD,
    FILES_HIGH_THRESHOLD,
    FILES_MEDIUM_THRESHOLD,
    estimate_expected_consumption,
)
from modelquad.models import ConsumptionBucket, ConsumptionSource, ExpectedConsumption


def test_consumption_models_enums_and_dataclass():
    """Verify ConsumptionBucket, ConsumptionSource, and ExpectedConsumption basics."""
    assert ConsumptionBucket.LOW.value == "low"
    assert ConsumptionBucket.MEDIUM.value == "medium"
    assert ConsumptionBucket.HIGH.value == "high"

    assert ConsumptionSource.HOST_SUPPLIED.value == "host_supplied"
    assert ConsumptionSource.ESTIMATED.value == "estimated"
    assert ConsumptionSource.UNKNOWN.value == "unknown"

    ec = ExpectedConsumption(
        bucket=ConsumptionBucket.HIGH,
        source=ConsumptionSource.ESTIMATED,
        basis="large diff",
    )
    assert ec.bucket is ConsumptionBucket.HIGH
    assert ec.source is ConsumptionSource.ESTIMATED
    assert ec.basis == "large diff"

    payload = ec.to_dict()
    assert payload == {
        "bucket": "high",
        "source": "estimated",
        "basis": "large diff",
    }

    restored = ExpectedConsumption.from_dict(payload)
    assert restored == ec


def test_consumption_models_string_coercion_and_tolerances():
    """String coercion and tolerant from_dict parsing."""
    # Coercion from strings in constructor
    ec = ExpectedConsumption(bucket="medium", source="host_supplied", basis="given")
    assert ec.bucket is ConsumptionBucket.MEDIUM
    assert ec.source is ConsumptionSource.HOST_SUPPLIED

    # Invalid bucket string raises ValueError
    with pytest.raises(ValueError, match="bucket must be one of"):
        ExpectedConsumption(bucket="super_huge")

    # Invalid source coerces to UNKNOWN
    ec_invalid_src = ExpectedConsumption(bucket="low", source="invalid_src")
    assert ec_invalid_src.source is ConsumptionSource.UNKNOWN

    # from_dict with missing source and basis
    ec_from_minimal = ExpectedConsumption.from_dict({"bucket": "high"})
    assert ec_from_minimal.bucket is ConsumptionBucket.HIGH
    assert ec_from_minimal.source is ConsumptionSource.UNKNOWN
    assert ec_from_minimal.basis is None

    # from_dict with invalid source coerces to UNKNOWN
    ec_from_bad_src = ExpectedConsumption.from_dict({"bucket": "low", "source": "unrecognized"})
    assert ec_from_bad_src.bucket is ConsumptionBucket.LOW
    assert ec_from_bad_src.source is ConsumptionSource.UNKNOWN


def test_consumption_no_inputs():
    """No inputs produces LOW bucket with source=ESTIMATED and a basis string."""
    result = estimate_expected_consumption()
    assert isinstance(result, ExpectedConsumption)
    assert result.bucket is ConsumptionBucket.LOW
    assert result.source is ConsumptionSource.ESTIMATED
    assert result.basis is not None
    assert "0 prompt chars" in result.basis
    assert "0 relevant files" in result.basis


def test_consumption_determinism():
    """Repeated identical calls produce identical results (bucket, source, basis)."""
    prompt = "Optimize the database query pipeline"
    capsule = TaskCapsule(
        goal="Refactor queries",
        task_type="implementation",
        relevant_files=("db/query.py", "db/models.py"),
    )
    result1 = estimate_expected_consumption(prompt, capsule)
    result2 = estimate_expected_consumption(prompt, capsule)
    assert result1 == result2
    assert result1.bucket == result2.bucket
    assert result1.source == result2.source
    assert result1.basis == result2.basis


def test_consumption_char_boundaries():
    """Character threshold boundaries: 3999, 4000, 19999, 20000."""
    # Just below MEDIUM threshold (3999 chars) -> LOW
    res_3999 = estimate_expected_consumption("a" * (CHAR_MEDIUM_THRESHOLD - 1))
    assert res_3999.bucket is ConsumptionBucket.LOW

    # At MEDIUM threshold (4000 chars) -> MEDIUM
    res_4000 = estimate_expected_consumption("a" * CHAR_MEDIUM_THRESHOLD)
    assert res_4000.bucket is ConsumptionBucket.MEDIUM

    # Just below HIGH threshold (19999 chars) -> MEDIUM
    res_19999 = estimate_expected_consumption("a" * (CHAR_HIGH_THRESHOLD - 1))
    assert res_19999.bucket is ConsumptionBucket.MEDIUM

    # At HIGH threshold (20000 chars) -> HIGH
    res_20000 = estimate_expected_consumption("a" * CHAR_HIGH_THRESHOLD)
    assert res_20000.bucket is ConsumptionBucket.HIGH


def test_consumption_file_boundaries():
    """File threshold boundaries: 7, 8, 19, 20 files."""
    # 7 files -> LOW
    files_7 = tuple(f"file_{i}.py" for i in range(FILES_MEDIUM_THRESHOLD - 1))
    cap_7 = TaskCapsule(goal="g", task_type="implementation", relevant_files=files_7)
    res_7 = estimate_expected_consumption(capsule=cap_7)
    assert res_7.bucket is ConsumptionBucket.LOW

    # 8 files -> MEDIUM
    files_8 = tuple(f"file_{i}.py" for i in range(FILES_MEDIUM_THRESHOLD))
    cap_8 = TaskCapsule(goal="g", task_type="implementation", relevant_files=files_8)
    res_8 = estimate_expected_consumption(capsule=cap_8)
    assert res_8.bucket is ConsumptionBucket.MEDIUM

    # 19 files -> MEDIUM
    files_19 = tuple(f"file_{i}.py" for i in range(FILES_HIGH_THRESHOLD - 1))
    cap_19 = TaskCapsule(goal="g", task_type="implementation", relevant_files=files_19)
    res_19 = estimate_expected_consumption(capsule=cap_19)
    assert res_19.bucket is ConsumptionBucket.MEDIUM

    # 20 files -> HIGH
    files_20 = tuple(f"file_{i}.py" for i in range(FILES_HIGH_THRESHOLD))
    cap_20 = TaskCapsule(goal="g", task_type="implementation", relevant_files=files_20)
    res_20 = estimate_expected_consumption(capsule=cap_20)
    assert res_20.bucket is ConsumptionBucket.HIGH


def test_consumption_combination_takes_max():
    """Combination of prompt length and file count takes the max tier."""
    # Short prompt (LOW chars) + 20 files (HIGH files) -> HIGH
    files_20 = tuple(f"file_{i}.py" for i in range(20))
    cap_high_files = TaskCapsule(goal="g", task_type="implementation", relevant_files=files_20)
    res_high_files = estimate_expected_consumption("short", capsule=cap_high_files)
    assert res_high_files.bucket is ConsumptionBucket.HIGH

    # 20000 chars (HIGH chars) + 0 files (LOW files) -> HIGH
    res_high_chars = estimate_expected_consumption("x" * CHAR_HIGH_THRESHOLD)
    assert res_high_chars.bucket is ConsumptionBucket.HIGH

    # 4000 chars (MEDIUM chars) + 7 files (LOW files) -> MEDIUM
    files_7 = tuple(f"file_{i}.py" for i in range(7))
    cap_7 = TaskCapsule(goal="g", task_type="implementation", relevant_files=files_7)
    res_med_chars = estimate_expected_consumption("x" * CHAR_MEDIUM_THRESHOLD, capsule=cap_7)
    assert res_med_chars.bucket is ConsumptionBucket.MEDIUM

    # 100 chars (LOW chars) + 8 files (MEDIUM files) -> MEDIUM
    files_8 = tuple(f"file_{i}.py" for i in range(8))
    cap_8 = TaskCapsule(goal="g", task_type="implementation", relevant_files=files_8)
    res_med_files = estimate_expected_consumption("x" * 100, capsule=cap_8)
    assert res_med_files.bucket is ConsumptionBucket.MEDIUM

    # 20000 chars (HIGH chars) + 8 files (MEDIUM files) -> HIGH
    res_high_med = estimate_expected_consumption("x" * CHAR_HIGH_THRESHOLD, capsule=cap_8)
    assert res_high_med.bucket is ConsumptionBucket.HIGH


def test_consumption_capsule_contribution():
    """TaskCapsule with many relevant_files raises bucket even with short prompt."""
    files_25 = tuple(f"src/module_{i}.py" for i in range(25))
    cap = TaskCapsule(
        goal="Small prompt",
        task_type="implementation",
        relevant_files=files_25,
    )
    res = estimate_expected_consumption(prompt="quick fix", capsule=cap)
    assert res.bucket is ConsumptionBucket.HIGH
    assert res.source is ConsumptionSource.ESTIMATED
    assert "25 relevant files" in res.basis
