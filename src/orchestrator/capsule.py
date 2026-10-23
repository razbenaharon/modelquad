# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Structured task context capsule for minimum-sufficient delegation.

Delegation should evolve from delegate(prompt) toward delegate(TaskCapsule)
carrying minimum-sufficient context; never auto-dump whole conversations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ALL_CAPABILITIES
from .models import ConsumptionBucket, TaskType
from .action_authority import TaskAuthority


_KNOWN_KEYS = frozenset(
    {
        "goal",
        "task_type",
        "relevant_files",
        "constraints",
        "decisions_made",
        "attempts_tried",
        "failure_evidence",
        "git_state",
        "relevant_tests",
        "expected_deliverable",
        "capability_requirements",
        "do_not_do",
        "context_ownership",
        "context_richness",
        "expected_consumption",
        "action_authority",
    }
)


@dataclass(frozen=True)
class TaskCapsule:
    """Structured container for task context passed during delegation."""

    goal: str
    task_type: str
    relevant_files: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    decisions_made: tuple[str, ...] = ()
    attempts_tried: tuple[str, ...] = ()
    failure_evidence: str | None = None
    git_state: str | None = None
    relevant_tests: tuple[str, ...] = ()
    expected_deliverable: str | None = None
    capability_requirements: tuple[str, ...] = ()
    do_not_do: tuple[str, ...] = ()
    context_ownership: str | None = None
    context_richness: str | None = None
    expected_consumption: str | None = None
    action_authority: TaskAuthority | None = None

    _VALID_RICHNESS = frozenset({"none", "sparse", "rich"})

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("goal must be a non-empty string")

        task_val = self.task_type.value if isinstance(self.task_type, TaskType) else self.task_type
        if not isinstance(task_val, str) or task_val not in {t.value for t in TaskType}:
            valid = sorted(t.value for t in TaskType)
            raise ValueError(f"task_type must be a valid TaskType ({valid}), got {self.task_type!r}")
        if isinstance(self.task_type, TaskType):
            object.__setattr__(self, "task_type", self.task_type.value)

        if self.action_authority is not None:
            from .execution.base import READ_ONLY_TASK_TYPES

            if not isinstance(self.action_authority, TaskAuthority):
                raise ValueError("action_authority must be a typed TaskAuthority")
            self.action_authority.validate_consultation(TaskType(self.task_type) in READ_ONLY_TASK_TYPES)

        for field_name in (
            "relevant_files",
            "constraints",
            "decisions_made",
            "attempts_tried",
            "relevant_tests",
            "capability_requirements",
            "do_not_do",
        ):
            val = getattr(self, field_name)
            if isinstance(val, (list, set, frozenset)):
                object.__setattr__(self, field_name, tuple(val))
            elif val is None:
                object.__setattr__(self, field_name, ())
            elif not isinstance(val, tuple):
                raise ValueError(f"{field_name} must be a sequence of strings")

        for req in self.capability_requirements:
            if req not in ALL_CAPABILITIES:
                valid_caps = sorted(ALL_CAPABILITIES)
                raise ValueError(
                    f"capability_requirements contains invalid capability {req!r}; must be one of {valid_caps}"
                )

        if self.context_richness is not None and self.context_richness not in self._VALID_RICHNESS:
            raise ValueError(
                f"context_richness must be one of {sorted(self._VALID_RICHNESS)}, got {self.context_richness!r}"
            )

        if self.expected_consumption is not None:
            val = (
                self.expected_consumption.value
                if isinstance(self.expected_consumption, ConsumptionBucket)
                else self.expected_consumption
            )
            if not isinstance(val, str) or val not in {b.value for b in ConsumptionBucket}:
                valid_buckets = sorted(b.value for b in ConsumptionBucket)
                raise ValueError(
                    f"expected_consumption must be one of {valid_buckets}, got {self.expected_consumption!r}"
                )
            if isinstance(self.expected_consumption, ConsumptionBucket):
                object.__setattr__(self, "expected_consumption", val)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "goal": self.goal,
            "task_type": self.task_type,
            "relevant_files": list(self.relevant_files),
            "constraints": list(self.constraints),
            "decisions_made": list(self.decisions_made),
            "attempts_tried": list(self.attempts_tried),
            "failure_evidence": self.failure_evidence,
            "git_state": self.git_state,
            "relevant_tests": list(self.relevant_tests),
            "expected_deliverable": self.expected_deliverable,
            "capability_requirements": list(self.capability_requirements),
            "do_not_do": list(self.do_not_do),
            "context_ownership": self.context_ownership,
            "context_richness": self.context_richness,
            "expected_consumption": self.expected_consumption,
        }
        if self.action_authority is not None:
            result["action_authority"] = self.action_authority.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskCapsule:
        if not isinstance(data, dict):
            raise ValueError("TaskCapsule data must be a dictionary")
        unknown = set(data) - _KNOWN_KEYS
        if unknown:
            raise ValueError(f"Unknown keys in TaskCapsule payload: {', '.join(sorted(unknown))}")

        return cls(
            goal=data.get("goal", ""),
            task_type=data.get("task_type", ""),
            relevant_files=tuple(data.get("relevant_files") or ()),
            constraints=tuple(data.get("constraints") or ()),
            decisions_made=tuple(data.get("decisions_made") or ()),
            attempts_tried=tuple(data.get("attempts_tried") or ()),
            failure_evidence=data.get("failure_evidence"),
            git_state=data.get("git_state"),
            relevant_tests=tuple(data.get("relevant_tests") or ()),
            expected_deliverable=data.get("expected_deliverable"),
            capability_requirements=tuple(data.get("capability_requirements") or ()),
            do_not_do=tuple(data.get("do_not_do") or ()),
            context_ownership=data.get("context_ownership"),
            context_richness=data.get("context_richness"),
            expected_consumption=data.get("expected_consumption"),
            action_authority=(TaskAuthority.from_dict(data["action_authority"])
                              if "action_authority" in data else None),
        )

    def to_prompt(self) -> str:
        """Render a clear worker prompt with minimum-sufficient structured context."""
        sections_order = [
            ("Relevant files", self.relevant_files),
            ("Constraints", self.constraints),
            ("Decisions already made", self.decisions_made),
            ("Attempts already tried", self.attempts_tried),
            ("Current failure evidence", self.failure_evidence),
            ("Git state", self.git_state),
            ("Relevant tests", self.relevant_tests),
            ("Expected deliverable", self.expected_deliverable),
            ("Do not do", self.do_not_do),
        ]

        parts: list[str] = [f"TASK: {self.goal.strip()}"]

        for header, content in sections_order:
            if content is None:
                continue
            if isinstance(content, (tuple, list)):
                if not content:
                    continue
                parts.append(f"\n{header}:")
                for item in content:
                    parts.append(f"- {item}")
            elif isinstance(content, str):
                stripped = content.strip()
                if not stripped:
                    continue
                parts.append(f"\n{header}:")
                for line in stripped.splitlines():
                    line_str = line.strip()
                    if line_str:
                        if not line_str.startswith("- "):
                            parts.append(f"- {line_str}")
                        else:
                            parts.append(line_str)

        return "\n".join(parts)
