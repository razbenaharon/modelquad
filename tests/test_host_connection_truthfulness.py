# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Onboarding may only claim a host is connected when it actually registered it.

The verification steps once computed success as ``check.status is not
CheckStatus.MISSING``. ``doctor`` returns ``WARN`` for "Not registered", and
``WARN`` is not ``MISSING``, so a check that did not pass became a sentence
telling the user it had. The invariant: a host registration step may report
success to the user only when a registration was actually verified or applied.
"""

from __future__ import annotations

from pathlib import Path


def test_no_onboarding_step_grades_a_check_by_is_not_missing():
    """`is not MISSING` treats WARN as success. `doctor` may grade WARN as
    advisory for its own roll-up -- that is its documented contract -- but
    onboarding turns a check into a claim made to a person, and a claim needs
    the check to have actually passed. Asserted at the source so a fourth
    verification step cannot reintroduce it."""

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "orchestrator"
        / "onboarding.py"
    ).read_text(encoding="utf-8")

    assert "is not CheckStatus.MISSING" not in source
    assert "!= CheckStatus.MISSING" not in source
