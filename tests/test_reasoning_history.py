# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Persistence test: the decision mode stays deterministic."""

from __future__ import annotations

import pytest

from modelquad.service import history as service_history


def test_decision_mode_defaults_to_deterministic_and_roundtrips(tmp_path):
    db_path = tmp_path / "history.db"
    assert service_history.get_decision_mode(db_path) == "deterministic"
    assert service_history.put_decision_mode(db_path, "reasoning_shadow") == "reasoning_shadow"
    assert service_history.get_decision_mode(db_path) == "deterministic"
    with pytest.raises(ValueError):
        service_history.put_decision_mode(db_path, "vibes")
    assert service_history.get_decision_mode(db_path) == "deterministic"
