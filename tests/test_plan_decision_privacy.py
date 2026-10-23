# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic round trips for the bounded plan persistence boundary."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from orchestrator.service import history


PATHS = (
    r"C:\Users\Someone\Projects\private\app.py",
    r"\\fileserver\team-share\private\app.py",
    "/home/someone/private/app.py",
)
FIELDS = ("objective", "success_criteria", "reason_summary")


def _write(db_path, **kwargs):
    row_id = history.record_plan_decision(
        db_path, task_type="implementation", decision_mode="reasoning_shadow", **kwargs
    )
    assert row_id is not None
    row = history.fetch_plan_decision(db_path, row_id)
    assert row is not None
    return row


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("field", FIELDS)
def test_plan_text_paths_are_redacted_in_database_roundtrip(tmp_path, path, field):
    db_path = tmp_path / "history.db"
    identity = {
        "role": "implement",
        "provider": "codex",
        "model": "gpt-example-שלום",
        "reasoning_effort": "high",
        "task_type": "implementation",
        "agent_runtime": "codex",
        "resource_pool": "chatgpt-pro",
    }
    step = {**identity, "objective": "ordinary", "success_criteria": None}
    text = f'בדיקה 😀 "{path}" succeeds'
    summary = "ordinary summary"
    if field == "reason_summary":
        summary = text
    else:
        step[field] = text
    steps = [step]
    original = copy.deepcopy(steps)

    row = _write(db_path, steps=steps, reason_summary=summary, confidence=0.7)

    stored = row["reason_summary"] if field == "reason_summary" else row["steps"][0][field]
    assert stored == 'בדיקה 😀 "<path>" succeeds'
    assert steps == original
    assert row["confidence"] == 0.7
    for key, value in identity.items():
        assert row["steps"][0][key].encode("utf-8") == value.encode("utf-8")
    assert row["steps"][0].keys() == step.keys()
    if field != "success_criteria":
        assert row["steps"][0]["success_criteria"] is None
    # Inspect the stored JSON as well as the public decoder: redacting a JSON
    # string wholesale can damage escapes or leave escaped Windows paths.
    with sqlite3.connect(db_path) as conn:
        raw, raw_summary = conn.execute(
            "SELECT steps_json, reason_summary FROM plan_decisions WHERE id = ?",
            (row["id"],),
        ).fetchone()
    assert json.loads(raw) == row["steps"]
    assert raw_summary == row["reason_summary"]


@pytest.mark.parametrize("field", FIELDS)
def test_existing_caps_apply_before_redaction_and_preserve_unicode(tmp_path, field):
    limit = 1000 if field == "reason_summary" else 500
    prefix = f'שלום 😀 "{PATHS[0]}" '
    text = prefix + "א" * 5000
    steps = [{"objective": "ordinary", "success_criteria": None}]
    kwargs = {"steps": steps}
    if field == "reason_summary":
        kwargs[field] = text
    else:
        steps[0][field] = text
    original = copy.deepcopy(kwargs)

    row = _write(tmp_path / "history.db", **kwargs)

    stored = row[field] if field == "reason_summary" else row["steps"][0][field]
    assert stored == 'שלום 😀 "<path>" ' + "א" * (limit - len(prefix)) + " ..."
    assert kwargs == original


def test_structure_nulls_numbers_and_path_shaped_identity_are_unchanged(tmp_path):
    steps = [
        {"role": PATHS[0], "provider": PATHS[1], "model": PATHS[2],
         "reasoning_effort": PATHS[0], "agent_runtime": None, "resource_pool": None},
        {"objective": None, "success_criteria": None, "count": 7},
        {"objective": 42, "success_criteria": 0.5, "nested": {"flag": True}},
        None,
        3,
    ]
    original = copy.deepcopy(steps)

    row = _write(tmp_path / "history.db", steps=steps, reason_summary=None)

    expected = copy.deepcopy(original)
    expected[0].update(objective=None, success_criteria=None)
    assert row["steps"] == expected
    assert row["reason_summary"] is None
    assert steps == original
    assert json.dumps(row["steps"], ensure_ascii=False).encode("utf-8") == (
        json.dumps(expected, ensure_ascii=False).encode("utf-8")
    )
    empty = _write(tmp_path / "history.db", steps=None, packet=None)
    assert empty["steps"] is None
    assert empty["packet"] is None


def test_explicit_residuals_are_preserved_without_mutating_packet(tmp_path):
    packet = {"providers": [{"provider": "codex", "resource_objective": PATHS[0]}],
              "host_context": {"model": "gpt-example"}, "handoff_cost": None}
    original = copy.deepcopy(packet)
    row = _write(
        tmp_path / "history.db", packet=packet, fallback_reason=PATHS[1],
        validation_errors=[PATHS[2]], steps=[{"extra_text": PATHS[0]}],
    )
    assert packet == original
    assert row["packet"] == original
    assert row["fallback_reason"] == PATHS[1]
    assert row["validation_errors"] == [PATHS[2]]
    assert row["steps"] == [
        {"extra_text": PATHS[0], "objective": None, "success_criteria": None}
    ]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("field", ("task_status_reason", "detail", "error"))
def test_execution_free_text_is_redacted_in_database_roundtrip(tmp_path, path, field):
    execution = {
        "task_status": "blocked", "replanned_provider": "codex",
        "provider": PATHS[0], "status": PATHS[1], "outcome": PATHS[2],
        "model": PATHS[0], "duration_seconds": 1.5, "replanned": False,
        "nullable": None, "count": 7,
        field: f'failed reading "{path}"',
    }
    steps = [{"execution": execution}]
    original = copy.deepcopy(steps)
    db_path = tmp_path / "history.db"

    row = _write(db_path, steps=steps)

    expected = {**execution, field: 'failed reading "<path>"'}
    assert row["steps"] == [
        {"execution": expected, "objective": None, "success_criteria": None}
    ]
    assert steps == original
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT steps_json FROM plan_decisions WHERE id = ?", (row["id"],)
        ).fetchone()[0]
    assert json.loads(raw) == row["steps"]


@pytest.mark.parametrize("execution", (None, 3, PATHS[0], {"task_status_reason": None}))
def test_execution_nulls_and_non_dict_values_pass_through(tmp_path, execution):
    steps = [{"execution": execution}]
    original = copy.deepcopy(steps)
    row = _write(tmp_path / "history.db", steps=steps)
    assert row["steps"] == [
        {"execution": execution, "objective": None, "success_criteria": None}
    ]
    assert steps == original


def test_new_write_does_not_backfill_historical_rows(tmp_path):
    db_path = tmp_path / "history.db"
    old = _write(db_path)
    legacy_steps = json.dumps([{"objective": PATHS[0]}])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE plan_decisions SET steps_json = ?, reason_summary = ? WHERE id = ?",
            (legacy_steps, PATHS[1], old["id"]),
        )
    _write(db_path, steps=[{"objective": PATHS[0]}], reason_summary=PATHS[1])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT steps_json, reason_summary FROM plan_decisions WHERE id = ?", (old["id"],)
        ).fetchone() == (legacy_steps, PATHS[1])
