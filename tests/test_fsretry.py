# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A brief Windows sharing violation is retried; anything else is not."""

from __future__ import annotations

import pytest

from modelquad import fsretry


def _refusal(winerror):
    error = PermissionError("held by another process")
    error.winerror = winerror
    return error


@pytest.mark.parametrize("winerror", [5, 32])
def test_a_transient_sharing_refusal_is_retried_until_it_clears(winerror):
    attempts = []

    def operation():
        attempts.append(1)
        if len(attempts) < 3:
            raise _refusal(winerror)
        return "done"

    assert fsretry.retry_sharing(operation, seconds=5) == "done"
    assert len(attempts) == 3


def test_other_errors_are_raised_at_once():
    attempts = []

    def operation():
        attempts.append(1)
        raise PermissionError("not a sharing violation")

    with pytest.raises(PermissionError):
        fsretry.retry_sharing(operation, seconds=5)
    assert len(attempts) == 1


def test_a_refusal_that_lasts_past_the_deadline_is_raised():
    with pytest.raises(PermissionError):
        fsretry.retry_sharing(lambda: (_ for _ in ()).throw(_refusal(32)), seconds=0.1)
