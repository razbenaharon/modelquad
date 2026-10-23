# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Worker launch contracts over real adapters and job seams, with inert runners.

These tests never start a provider or a child process. Review and second
opinion use the same adapter entry points as delegation; environment assertions
sit at their runner boundary, rather than merely inspecting argv builders.
"""

from dataclasses import replace
import os
import subprocess
from types import SimpleNamespace

import pytest

from test_noshell_workers import worker as codex_worker
from orchestrator.execution import codex
from orchestrator.execution.base import ExecutionRequest
from orchestrator.jobs import handle, runner
from orchestrator.jobs.seam import JobRef
from orchestrator.models import TaskType
from orchestrator.service import history


@pytest.mark.parametrize("task", [TaskType.REVIEW, TaskType.EXPLORATION, TaskType.ARCHITECTURE])
@pytest.mark.parametrize("inherited", [None, "0"])
def test_codex_noshell(codex_worker, monkeypatch, task, inherited):
    if inherited is None:
        monkeypatch.delenv("ORCHESTRATOR_WORKER", raising=False)
    else:
        monkeypatch.setenv("ORCHESTRATOR_WORKER", inherited)
    executor, request, run, inventories = codex_worker
    executor.execute(replace(request, task_type=task))
    run.assert_called_once()
    assert run.call_args.kwargs["env"]["ORCHESTRATOR_WORKER"] == "1"
    assert inventories and all(env["ORCHESTRATOR_WORKER"] == "1" for _, _, env in inventories)
    assert os.environ.get("ORCHESTRATOR_WORKER") == inherited


def test_codex_probe_and_worker_carry_the_marker(tmp_path, monkeypatch, synthetic_worker_binary,
                                                 qualified_codex_inventory):
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "0")
    calls = []
    def inventory(argv, cwd, env):
        assert env["ORCHESTRATOR_WORKER"] == "1"
        return qualified_codex_inventory(argv, cwd, env)
    def run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "answer", "")
    executor = codex.CodexExecutor(which=lambda _: synthetic_worker_binary("codex.exe"),
        run=run, inventory=inventory)
    executor.execute(ExecutionRequest("codex", TaskType.REVIEW, "Inspect evidence", tmp_path))
    assert len(calls) == 1 and calls[0]["env"]["ORCHESTRATOR_WORKER"] == "1"
    assert os.environ["ORCHESTRATOR_WORKER"] == "0"


@pytest.mark.parametrize("service_admission", [False, True])
def test_jobs_runner_launch(tmp_path, monkeypatch, service_admission):
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "0")
    path = tmp_path / "jobs.db"
    attempt = history.record_delegation_attempt(path, task_type="review", decision_id="decision")
    row = history.reserve_job(path, "worker-contract", attempt)
    ref = JobRef(row["job_id"], attempt, "decision")
    spec = handle.JobSpec(("inert-worker",), str(tmp_path), str(path), attempt, 5)
    monkeypatch.setattr(handle, "_runner_command", lambda *args: ["inert-runner"])
    monkeypatch.setattr(handle, "_background_child_kwargs", lambda: {"cwd": str(tmp_path)})
    monkeypatch.setattr(handle, "_open_runner_log", lambda: None)
    monkeypatch.setattr(handle, "_log_runner_event", lambda *args: None)
    monkeypatch.setattr(handle, "_service_runner_policy", lambda: (True, "not_in_job", 0, False))
    monkeypatch.setattr(handle.procutil, "process_creation_epoch", lambda pid: 1.0)
    monkeypatch.setattr(handle.procutil, "_resume_suspended_process", lambda pid: True)
    if service_admission:
        history.put_job_runner_enabled(path, True)
        history.update_job(path, ref.job_id, runner_pid=os.getpid(), runner_created=1.0)
    seen = []
    def popen(argv, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(pid=12345)
    monkeypatch.setattr(handle.subprocess, "Popen", popen)
    result = handle.launch_reserved(ref, tmp_path / "inert-spec", spec,
        _policy=(0, False, None), service_admission=service_admission)
    assert result.launched and len(seen) == 1
    assert seen[0]["ORCHESTRATOR_WORKER"] == "1"
    assert os.environ["ORCHESTRATOR_WORKER"] == "0"


def test_jobs_raw_worker_overrides_spec_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "0")
    path = tmp_path / "raw.db"
    attempt = history.record_delegation_attempt(path, task_type="review", decision_id="decision")
    row = history.reserve_job(path, "raw-contract", attempt)
    ref = JobRef(row["job_id"], attempt, "decision")
    spec = handle.JobSpec(("inert-worker",), str(tmp_path), str(path), attempt, 5,
                          env={"ORCHESTRATOR_WORKER": "0"})
    monkeypatch.setattr(runner, "snapshot", lambda cwd: None)
    monkeypatch.setattr(runner, "changed_files", lambda before, cwd: None)
    seen = []
    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "answer", "")
    monkeypatch.setattr(runner.procutil, "run_with_tree_kill", run)
    runner._execute(spec, ref)
    assert len(seen) == 1 and seen[0]["ORCHESTRATOR_WORKER"] == "1"
    assert os.environ["ORCHESTRATOR_WORKER"] == "0"


def test_packaged_skill_and_host_templates_stand_down():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src/orchestrator/adapters/templates"
    from orchestrator.adapters.server_instructions import server_instructions
    texts = [(root / "claude/skills/modelquad/SKILL.md").read_text(encoding="utf-8"),
             server_instructions(False), server_instructions(True)]
    for text in texts:
        # The rewritten instructions state the guard in one plain line.
        guard = next(line for line in text.splitlines() if "`ORCHESTRATOR_WORKER=1`" in line)
        assert "do not call ModelQuad" in guard


def test_jobs_breakaway_probe_stands_down(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKER", "0")
    monkeypatch.setattr(handle, "_caller_job_limits", lambda: 0x1000)
    monkeypatch.setattr(handle, "_background_child_kwargs", lambda: {})
    monkeypatch.setattr(handle, "_process_in_any_job", lambda value: False)
    seen = []
    def popen(argv, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(_handle=1, kill=lambda: None, wait=lambda **kwargs: None)
    monkeypatch.setattr(handle.subprocess, "Popen", popen)
    assert handle._probe_breakaway()
    assert len(seen) == 1 and seen[0]["ORCHESTRATOR_WORKER"] == "1"
    assert os.environ["ORCHESTRATOR_WORKER"] == "0"
