# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The argument shape of one ``modelquad_split`` step."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SplitStep(BaseModel):
    """One step of the task, as Claude describes it to ``modelquad_split``."""

    title: str = Field(max_length=120, description="A short name for the step. Not stored.")
    task_type: Literal["architecture", "implementation", "exploration", "debugging",
                       "testing", "review", "trivial"]
    needs: Literal["edit", "run", "none"] = Field(
        description="edit: changes files; run: runs commands or tests; none: reading and thinking only.")
    independent: bool = Field(
        default=False,
        description="True for an opinion that should not come from Claude, such as a review of Claude's own change.")
    size: Literal["small", "medium", "large"] = Field(
        default="medium",
        description="How much reading and thinking the step needs. A small step is not worth a Codex call.")
