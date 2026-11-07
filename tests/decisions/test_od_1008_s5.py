# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""In the beta a worker only reads and diagnoses.

Writable task types are refused before any launch, read-only task types run
the Codex worker with no command tools, and the Claude worker has
read/search/list tools only. Expected values are literals, not the sets under
test.
"""

import pytest

from modelquad import project_notes
from modelquad.execution import noshell
from modelquad.execution.base import READ_ONLY_TASK_TYPES
from modelquad.models import TaskType
from modelquad.routing_policy import LaunchRefused

READ_ONLY = ("review", "exploration", "architecture")
WRITABLE = ("implementation", "debugging", "testing", "trivial")


def test_task_types_split_into_read_only_and_writable():
    assert {task.value for task in TaskType} == set(READ_ONLY) | set(WRITABLE)
    assert {task.value for task in READ_ONLY_TASK_TYPES} == set(READ_ONLY)


@pytest.mark.parametrize("task_type", WRITABLE)
def test_writable_task_types_are_refused(task_type):
    with pytest.raises(LaunchRefused, match="^task_type_not_read_only$"):
        project_notes.require_read_only(task_type)


@pytest.mark.parametrize("task_type", READ_ONLY)
def test_read_only_task_types_are_admitted(task_type):
    assert project_notes.require_read_only(task_type) is None


def test_codex_consultation_runs_without_command_tools():
    for feature in ("shell_tool", "unified_exec", "code_mode", "code_mode_host", "apps", "plugins", "hooks"):
        assert ("--disable", feature) in zip(noshell.NOSHELL_ARGS, noshell.NOSHELL_ARGS[1:])
    assert noshell.NOSHELL_ARGS[-2:] == ("--sandbox", "read-only")
    assert "--ignore-rules" in noshell.NOSHELL_ARGS
