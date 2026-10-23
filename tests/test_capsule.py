# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for TaskCapsule context container and prompt rendering."""

from __future__ import annotations

import asyncio
import json
import pytest

from orchestrator.capsule import TaskCapsule
from orchestrator.config import CAP_COMMAND, CAP_READ, CAP_WRITE
from orchestrator.execution import CodexExecutor, ExecutionManager
from orchestrator.mcp.server import server
import orchestrator.mcp.server as mcp_server


def test_task_capsule_required_fields_and_validation():
    # Valid minimal capsule
    capsule = TaskCapsule(goal="Fix database deadlock", task_type="debugging")
    assert capsule.goal == "Fix database deadlock"
    assert capsule.task_type == "debugging"

    # Empty goal rejected
    with pytest.raises(ValueError, match="goal"):
        TaskCapsule(goal="", task_type="debugging")
    with pytest.raises(ValueError, match="goal"):
        TaskCapsule(goal="   ", task_type="debugging")

    # Invalid task_type rejected
    with pytest.raises(ValueError, match="task_type"):
        TaskCapsule(goal="Fix bug", task_type="non_existent_stage")


def test_task_capsule_capability_validation():
    # Valid capabilities
    capsule = TaskCapsule(
        goal="Run tests",
        task_type="testing",
        capability_requirements=(CAP_READ, CAP_COMMAND),
    )
    assert capsule.capability_requirements == (CAP_READ, CAP_COMMAND)

    # Invalid capability rejected
    with pytest.raises(ValueError, match="capability_requirements"):
        TaskCapsule(
            goal="Do impossible task",
            task_type="implementation",
            capability_requirements=("teleportation",),
        )


def test_task_capsule_richness_validation():
    with pytest.raises(ValueError, match="context_richness"):
        TaskCapsule(goal="Write docs", task_type="architecture", context_richness="overflowing")


def test_task_capsule_roundtrip_and_unknown_key_rejection():
    capsule = TaskCapsule(
        goal="Refactor auth",
        task_type="implementation",
        relevant_files=("auth.py", "jwt.py"),
        constraints=("no external deps",),
        decisions_made=("use HMAC-SHA256",),
        attempts_tried=("tried RS256 without certs",),
        failure_evidence="KeyError: cert_path",
        git_state="branch: feature/auth, clean working tree",
        relevant_tests=("tests/test_auth.py",),
        expected_deliverable="all tests green",
        capability_requirements=(CAP_WRITE,),
        do_not_do=("do not touch user_model.py",),
        context_ownership="host",
        context_richness="rich",
    )
    payload = capsule.to_dict()
    assert payload["goal"] == "Refactor auth"
    assert payload["relevant_files"] == ["auth.py", "jwt.py"]

    restored = TaskCapsule.from_dict(payload)
    assert restored == capsule

    # Unknown key in from_dict is rejected
    bad_payload = dict(payload)
    bad_payload["unknown_extra_field"] = "surprise"
    with pytest.raises(ValueError, match="Unknown keys.*unknown_extra_field"):
        TaskCapsule.from_dict(bad_payload)


def test_task_capsule_expected_consumption():
    from orchestrator.models import ConsumptionBucket

    # Valid values
    capsule_high = TaskCapsule(
        goal="Heavy refactoring",
        task_type="implementation",
        expected_consumption="high",
    )
    assert capsule_high.expected_consumption == "high"

    capsule_enum = TaskCapsule(
        goal="Heavy refactoring",
        task_type="implementation",
        expected_consumption=ConsumptionBucket.MEDIUM,
    )
    assert capsule_enum.expected_consumption == "medium"

    # Invalid value rejected
    with pytest.raises(ValueError, match="expected_consumption"):
        TaskCapsule(
            goal="Heavy refactoring",
            task_type="implementation",
            expected_consumption="astronomical",
        )

    # Roundtrip to_dict/from_dict
    payload = capsule_high.to_dict()
    assert payload["expected_consumption"] == "high"
    restored = TaskCapsule.from_dict(payload)
    assert restored.expected_consumption == "high"
    assert restored == capsule_high

    # Old payload without the key still parses
    old_payload = {
        "goal": "Legacy task",
        "task_type": "testing",
    }
    legacy = TaskCapsule.from_dict(old_payload)
    assert legacy.expected_consumption is None

    # to_prompt() output does NOT contain the bucket or expected_consumption
    prompt = capsule_high.to_prompt()
    assert "high" not in prompt
    assert "consumption" not in prompt.lower()
    assert "expected_consumption" not in prompt


def test_task_capsule_to_prompt_renders_sections_in_order():
    capsule = TaskCapsule(
        goal="Fix token refresh deadlock",
        task_type="debugging",
        relevant_files=("src/auth.py", "src/token.py"),
        constraints=("Must not add third-party libraries",),
        decisions_made=("Keep session format unchanged",),
        attempts_tried=("Replaced Lock with RLock",),
        failure_evidence="Deadlock on token refresh at line 42",
        git_state="HEAD at 1a2b3c4",
        relevant_tests=("tests/test_auth.py",),
        expected_deliverable="Passing unit tests",
        do_not_do=("Do not break backward compatibility with v1 API",),
    )
    prompt = capsule.to_prompt()

    # Check goal is first
    assert prompt.startswith("TASK: Fix token refresh deadlock")

    # Check all section headers in exact order
    headers = [
        "Relevant files:",
        "Constraints:",
        "Decisions already made:",
        "Attempts already tried:",
        "Current failure evidence:",
        "Git state:",
        "Relevant tests:",
        "Expected deliverable:",
        "Do not do:",
    ]
    indices = [prompt.find(header) for header in headers]
    for idx, header in zip(indices, headers):
        assert idx != -1, f"Missing header {header}"
    assert indices == sorted(indices), "Headers are not in expected order"

    # Check content bullets
    assert "- src/auth.py" in prompt
    assert "- Must not add third-party libraries" in prompt
    assert "- Deadlock on token refresh at line 42" in prompt


def test_task_capsule_to_prompt_omits_empty_sections():
    capsule = TaskCapsule(
        goal="Quick fix",
        task_type="implementation",
        relevant_files=("src/fix.py",),
        expected_deliverable="Working fix",
    )
    prompt = capsule.to_prompt()

    assert "TASK: Quick fix" in prompt
    assert "Relevant files:" in prompt
    assert "- src/fix.py" in prompt
    assert "Expected deliverable:" in prompt
    assert "- Working fix" in prompt

    # Empty sections must be absent
    assert "Constraints:" not in prompt
    assert "Decisions already made:" not in prompt
    assert "Attempts already tried:" not in prompt
    assert "Current failure evidence:" not in prompt
    assert "Git state:" not in prompt
    assert "Relevant tests:" not in prompt
    assert "Do not do:" not in prompt


def test_mcp_delegate_with_capsule_renders_prompt(tmp_path, monkeypatch):
    captured_prompts: list[str] = []

    class RecordingExecutor:
        name = "codex"

        def is_available(self):
            return True

        def execute(self, request):
            captured_prompts.append(request.prompt)
            from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus

            return ExecutionResult(
                provider="codex",
                success=True,
                output="ok",
                exit_code=0,
                task_status=WorkerTaskStatus.COMPLETED,
            )

    def build_execution_manager(config):
        return ExecutionManager(
            config,
            executors={"codex": RecordingExecutor()},
        )

    monkeypatch.setattr(mcp_server, "_build_execution_manager", build_execution_manager)

    capsule_dict = {
        "goal": "Review query performance",
        "task_type": "review",
        "relevant_files": ["db.py"],
        "constraints": ["Assess index usage"],
    }

    # 1. Empty prompt with capsule
    res = asyncio.run(
        server.call_tool(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": "",
                "working_directory": str(tmp_path),
                "provider": "codex",
                "capsule": capsule_dict,
            },
        )
    )
    assert len(captured_prompts) == 1
    assert "TASK: Review query performance" in captured_prompts[0]
    assert "Relevant files:" in captured_prompts[0]
    assert "- db.py" in captured_prompts[0]

    # 2. Caller prompt + capsule
    captured_prompts.clear()
    res2 = asyncio.run(
        server.call_tool(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": "Extra caller instruction",
                "working_directory": str(tmp_path),
                "provider": "codex",
                "capsule": capsule_dict,
            },
        )
    )
    assert len(captured_prompts) == 1
    assert captured_prompts[0].startswith("Extra caller instruction\n\nTASK: Review query performance")
