# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for delegating from an unrelated caller project.

Every provider interaction in this module is a recording fake: no real CLI or
HTTP client can run.  That makes the exact recorded working directory sufficient
to prove a delegated worker cannot operate on this repository instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

import modelquad.mcp.server as mcp_server
from modelquad.config import Config
from modelquad.doctor import CheckStatus, run_doctor
from modelquad.execution import (
    CodexExecutor,
    ExecutionManager,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    WorkerTaskStatus,
)
from modelquad.execution.base import WORKER_ENV_VAR
from modelquad.models import TaskType
from modelquad.service import history


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PROVIDER = "cross-project-fake"


# This recorder is the only execution mechanism, so its exact cwd assertions
# structurally prove no delegated worker can touch the modelquad repository.
class RecordingExecutor(Executor):
    """A provider-free executor that records the validated request it receives."""

    name = FAKE_PROVIDER

    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    def is_available(self) -> bool:
        return True

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(
            provider=self.name,
            success=True,
            output="recorded without executing a provider",
            exit_code=0,
            task_status=WorkerTaskStatus.COMPLETED,
        )


class RecordingRun:
    """Subprocess replacement which records kwargs without launching anything."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok", stderr="")


@pytest.fixture
def unrelated_project(tmp_path: Path) -> Path:
    project = tmp_path / "unrelated-project"
    project.mkdir()
    (project / "example.py").write_text("answer = 42\n", encoding="utf-8")
    (project / "test_example.py").write_text(
        "from example import answer\n\n\ndef test_answer():\n    assert answer == 42\n",
        encoding="utf-8",
    )
    return project


def _assert_unrelated_to_repo(working_directory: Path, project: Path) -> None:
    assert working_directory == project.resolve()
    assert not working_directory.is_relative_to(REPO_ROOT)
    assert not REPO_ROOT.is_relative_to(working_directory)


def _write_usage_fixtures(directory: Path) -> None:
    (directory / "usage-claude.json").write_text(
        json.dumps({"five_hour_used_percentage": 10, "seven_day_used_percentage": 20}),
        encoding="utf-8",
    )
    (directory / "usage-codex.json").write_text(
        json.dumps(
            {
                "available": True,
                "five_hour_remaining_percentage": 80,
                "seven_day_remaining_percentage": 70,
            }
        ),
        encoding="utf-8",
    )


def test_execution_manager_delegates_only_to_caller_project(unrelated_project: Path) -> None:
    executor = RecordingExecutor()
    config = Config()
    config.task_fit["implementation"][FAKE_PROVIDER] = 0.8
    manager = ExecutionManager(config, executors={FAKE_PROVIDER: executor})

    result = manager.delegate(
        TaskType.IMPLEMENTATION,
        "inspect the small example",
        unrelated_project,
        provider=FAKE_PROVIDER,
    )

    assert result.success is True
    assert len(executor.requests) == 1
    _assert_unrelated_to_repo(executor.requests[0].working_directory, unrelated_project)


def test_mcp_delegate_tools_preserve_caller_project(
    tmp_path: Path,
    unrelated_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_directory = tmp_path / "usage"
    usage_directory.mkdir()
    _write_usage_fixtures(usage_directory)
    history_db = tmp_path / "history.db"
    monkeypatch.setattr(mcp_server.service_db, "DEFAULT_DB_PATH", history_db)
    # The tools take no usage directory from the caller.
    monkeypatch.setattr(mcp_server, "USAGE_DIRECTORY", str(usage_directory))
    # The tool admits only the read-only Codex worker, so the recorder stands
    # in for it under that provider name.
    executor = RecordingExecutor()
    executor.name = "codex"
    def build_manager(config, **kwargs):
        return ExecutionManager(config, executors={"codex": executor}, **kwargs)
    monkeypatch.setattr(
        mcp_server,
        "_build_execution_manager",
        build_manager,
    )

    # The delegation tool is a coroutine function: only an async tool body can
    # observe its caller giving up mid-call, so the
    # direct call here is awaited rather than read as a return value.
    returned = asyncio.run(
        mcp_server.modelquad_delegate(
            "review",
            "inspect the small example",
            str(unrelated_project),
            timeout=60,
        )
    )
    # `modelquad_delegate` wraps the payload in a CallToolResult so the
    # visible text can be a summary instead of the whole object.
    payload = getattr(returned, "structured_content", None) or returned

    assert payload["success"] is True
    assert len(executor.requests) == 1
    entry = next(item for item in history.fetch_activity(history_db) if item["kind"] == "execution")
    assert entry["project"] == unrelated_project.name
    assert str(unrelated_project.resolve()) not in json.dumps(entry)
    assert os.sep not in entry["project"]
    _assert_unrelated_to_repo(executor.requests[0].working_directory, unrelated_project)
    assert executor.requests[0].working_directory != Path.cwd().resolve()


@pytest.mark.parametrize(
    ("executor_type", "provider", "binary"),
    [
        (CodexExecutor, "codex", "codex.exe"),
    ],
)
def test_subprocess_workers_receive_recursion_guard(
    unrelated_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_type: type[CodexExecutor],
    provider: str,
    binary: str,
    synthetic_worker_binary,
) -> None:
    import tempfile
    from modelquad.execution import codex as codex_module, noshell

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(codex_module, "_probe", lambda executable: "0.160.0")
    monkeypatch.setattr(noshell, "qualify", lambda executable, version, **kwargs:
                        noshell.Qualification(True, noshell.file_digest(executable), version, True,
                                              disabled_features=noshell.DISABLED_FEATURES))
    run = RecordingRun()
    monkeypatch.delenv(WORKER_ENV_VAR, raising=False)
    request = ExecutionRequest(
        provider=provider,
        task_type=TaskType.EXPLORATION,
        prompt="inspect the small example",
        working_directory=unrelated_project,
    )

    resolved = synthetic_worker_binary(binary)
    executor_type(which=lambda _: resolved, run=run).execute(request)

    child_env = run.calls[0]["kwargs"]["env"]
    assert isinstance(child_env, dict)
    assert child_env[WORKER_ENV_VAR] == "1"


def test_doctor_reports_worker_guard_for_delegated_and_top_level_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = {
        "usage_directory": tmp_path / "usage",
        "claude_dir": tmp_path / "claude",
        "codex_dir": tmp_path / "codex",
        "which": lambda _: "/fake/cli",
    }

    monkeypatch.setenv(WORKER_ENV_VAR, "1")
    delegated_checks = {check.name: check for check in run_doctor(Config(), **kwargs).checks}
    assert delegated_checks["worker-guard env"].status is CheckStatus.WARN

    monkeypatch.delenv(WORKER_ENV_VAR, raising=False)
    top_level_checks = {check.name: check for check in run_doctor(Config(), **kwargs).checks}
    assert top_level_checks["worker-guard env"].status is CheckStatus.OK
