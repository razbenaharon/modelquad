# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Codex runs read-only consultations only.

The task types the product treats as read-only -- `review`, `exploration`,
`architecture` -- run with qualified no-shell flags and `--sandbox read-only`;
a caller's metadata cannot widen them, every other task type is refused
before any process starts, and resumes always refuse.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from modelquad import procutil
from modelquad.execution import base as base_module
from modelquad.execution.base import (
    POLICY_REFUSAL_REASONS,
    READ_ONLY_TASK_TYPES,
    ExecutionRequest,
    WorkerTaskStatus,
    attempt_resolution,
    workspace_stability_note,
)
from modelquad.execution.codex import CodexExecutor
from modelquad.execution import codex as codex_module, noshell
from modelquad.models import TaskType

READ_ONLY = [TaskType.REVIEW, TaskType.EXPLORATION, TaskType.ARCHITECTURE]
WRITE = [TaskType.IMPLEMENTATION, TaskType.DEBUGGING, TaskType.TESTING, TaskType.TRIVIAL]
WRITABLE_SANDBOXES = ["workspace-write", "danger-full-access"]


@pytest.fixture(autouse=True)
def qualified_no_shell_argv_fixture(monkeypatch):
    # These tests own sandbox/argv behavior, not qualification.
    monkeypatch.setattr(codex_module, "_probe", lambda executable: "0.160.0")
    monkeypatch.setattr(noshell, "qualify", lambda executable, version, **kwargs:
                        noshell.Qualification(True, noshell.file_digest(executable), version, True,
                                              disabled_features=noshell.DISABLED_FEATURES))


class RecordingRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok", stderr="")


def _request(tmp_path, task_type, **kwargs):
    return ExecutionRequest(
        provider="codex",
        task_type=task_type,
        prompt="look at the thing",
        working_directory=tmp_path,
        **kwargs,
    )


def _execute(tmp_path, task_type, *, synthetic_worker_binary, **request_kwargs):
    run = RecordingRun()
    executor = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run)
    result = executor.execute(_request(tmp_path, task_type, **request_kwargs))
    return run, result


def _sandbox_of(argv):
    return argv[argv.index("--sandbox") + 1]


def test_the_read_only_set_is_exactly_the_three_consultation_task_types():
    assert READ_ONLY_TASK_TYPES == frozenset(READ_ONLY)
    assert isinstance(READ_ONLY_TASK_TYPES, frozenset)


def test_read_only_is_the_only_sandbox_mode():
    assert codex_module.VALID_SANDBOX_MODES == frozenset({"read-only"})


@pytest.mark.parametrize("task_type", READ_ONLY)
def test_a_new_read_only_session_runs_with_the_read_only_sandbox(tmp_path, task_type, *, synthetic_worker_binary):
    run, result = _execute(tmp_path, task_type, synthetic_worker_binary=synthetic_worker_binary)

    argv = run.calls[0]["argv"]
    assert _sandbox_of(argv) == "read-only"
    assert "--json" in argv
    assert "workspace-write" not in argv
    assert "danger-full-access" not in argv
    assert result.task_status_reason != "invalid_sandbox_mode"


@pytest.mark.parametrize("task_type", WRITE)
@pytest.mark.parametrize("session_id", [None, "abc123"], ids=["fresh", "resume"])
def test_a_write_task_type_is_refused_before_any_process(tmp_path, task_type, session_id, *, synthetic_worker_binary):
    run, result = _execute(tmp_path, task_type, session_id=session_id,
                           synthetic_worker_binary=synthetic_worker_binary)

    assert run.calls == []
    assert result.success is False
    assert result.task_status is WorkerTaskStatus.BLOCKED
    assert result.task_status_reason == "beta_read_only_required"
    assert result.metadata["worker_started"] is False
    assert "read-only consultations only" in result.error
    assert attempt_resolution(result) == ("refused", "policy_refused")


@pytest.mark.parametrize("task_type", READ_ONLY)
@pytest.mark.parametrize("sandbox", [*WRITABLE_SANDBOXES, "invalid", None, ""])
def test_metadata_cannot_widen_a_read_only_task_type(tmp_path, task_type, sandbox, *, synthetic_worker_binary):
    run, result = _execute(tmp_path, task_type, metadata={"sandbox": sandbox},
                           synthetic_worker_binary=synthetic_worker_binary)

    assert run.calls == []
    assert result.success is False
    assert result.task_status is WorkerTaskStatus.FAILED
    assert result.task_status_reason == "invalid_sandbox_mode"
    assert "only with the 'read-only' sandbox" in result.error
    assert attempt_resolution(result) == ("failed", "invalid_request")


@pytest.mark.parametrize("task_type", READ_ONLY)
def test_metadata_may_restate_read_only_for_a_read_only_task_type(tmp_path, task_type, *, synthetic_worker_binary):
    run, _ = _execute(tmp_path, task_type, metadata={"sandbox": "read-only"},
                      synthetic_worker_binary=synthetic_worker_binary)

    assert _sandbox_of(run.calls[0]["argv"]) == "read-only"


@pytest.mark.parametrize("task_type", READ_ONLY)
def test_resuming_a_read_only_task_type_is_refused_before_any_process(tmp_path, task_type, *, synthetic_worker_binary):
    run, result = _execute(tmp_path, task_type, session_id="sess-1", synthetic_worker_binary=synthetic_worker_binary)

    assert run.calls == []
    assert result.success is False
    assert result.task_status is WorkerTaskStatus.BLOCKED
    assert result.task_status_reason == "read_only_resume_unpinned"
    assert result.safe_to_retry is False
    assert result.task_completed is False
    assert attempt_resolution(result) == ("refused", "policy_refused")
    # The Host is told how to proceed: a new session, which runs read-only.
    assert "omit session_id" in result.error
    assert "--sandbox read-only" in result.error


def test_the_policy_refusals_are_closed_labels():
    assert "read_only_resume_unpinned" in POLICY_REFUSAL_REASONS
    assert "beta_read_only_required" in POLICY_REFUSAL_REASONS
    assert base_module.READ_ONLY_RESUME_UNPINNED == "read_only_resume_unpinned"
    # Every no-shell pre-launch refusal is a closed policy refusal with a message.
    assert base_module.NOSHELL_PRELAUNCH_REFUSALS == noshell.PRELAUNCH_REFUSALS
    assert noshell.PRELAUNCH_REFUSALS <= POLICY_REFUSAL_REASONS
    assert all(noshell.refusal_message(label) for label in noshell.REFUSAL_LABELS)
    assert noshell.UNAVAILABLE not in POLICY_REFUSAL_REASONS


def test_the_windows_path_filter_applies_to_read_only_runs(tmp_path, monkeypatch, *, synthetic_worker_binary):
    monkeypatch.setattr(os, "name", "nt")
    original_path = os.pathsep.join(
        (
            r"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe",
            r"C:\Users\example\AppData\Local\Microsoft\WindowsApps",
            r"C:\Windows\System32",
        )
    )
    monkeypatch.setenv("PATH", original_path)

    run, _ = _execute(tmp_path, TaskType.REVIEW, synthetic_worker_binary=synthetic_worker_binary)

    assert _sandbox_of(run.calls[0]["argv"]) == "read-only"
    child_path = run.calls[0]["kwargs"]["env"]["PATH"]
    assert "WindowsApps" not in child_path
    assert r"C:\Windows\System32" in child_path


def test_the_worker_environment_drops_other_services_secrets(tmp_path, monkeypatch, *, synthetic_worker_binary):
    removed = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN",
               "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AZURE_CLIENT_SECRET",
               "AZURE_OPENAI_API_KEY", "AZURE_STORAGE_CONNECTION_STRING"]
    kept = ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME", "HTTPS_PROXY", "AWS_REGION", "AZURE_TENANT_ID"]
    for name in removed + kept:
        monkeypatch.setenv(name, "inert-" + name.lower())

    run, _ = _execute(tmp_path, TaskType.REVIEW, synthetic_worker_binary=synthetic_worker_binary)

    env = {key.upper(): value for key, value in run.calls[0]["kwargs"]["env"].items()}
    assert not set(removed) & set(env)
    assert all(env[name] == "inert-" + name.lower() for name in kept)
    assert env["MODELQUAD_WORKER"] == "1"
    environment = codex_module.worker_environment({"github_token": "x", "Path": "y"})
    assert environment == {"Path": "y"}  # Windows names are case-insensitive.


def test_the_worker_is_stopped_when_this_process_dies(monkeypatch):
    # The default runner holds the worker's Job Object with kill-on-close, so a
    # killed MCP server takes the Codex tree with it; no call site has to ask.
    assert CodexExecutor()._run is procutil.run_worker
    seen = {}
    monkeypatch.setattr(procutil, "run_with_tree_kill", lambda argv, **kwargs: seen.update(kwargs) or "done")
    assert procutil.run_worker(["inert"], timeout=1) == "done"
    assert seen["kill_on_close"] is True
    procutil.run_worker(["inert"], kill_on_close=False)
    assert seen["kill_on_close"] is False


@pytest.mark.parametrize("confirmed", [False, True])
def test_an_interrupted_read_only_run_says_only_that_it_may_still_run(confirmed):
    metadata = {"timed_out": True, "requested_sandbox": "read-only", "worker_termination": {
        "pid": 7, "confirmed_dead": confirmed, "tree_confirmed_dead": confirmed,
        "tree_kill": "ok" if confirmed else "failed"}}
    note = workspace_stability_note(metadata, r"C:\work\repo")
    assert note["this_worker_may_still_be_writing"] is False
    assert note["this_worker_may_still_be_running"] is (not confirmed)
    assert "workspace_answer" not in note
    if confirmed:
        assert "warning" not in note
    else:
        assert "may still be running and using quota" in note["warning"]
        assert "writing" not in note["warning"]


def test_the_read_only_set_has_one_definition():
    assert codex_module.READ_ONLY_TASK_TYPES is base_module.READ_ONLY_TASK_TYPES


@pytest.mark.parametrize("name", [
    "NPM_TOKEN", "HF_TOKEN", "GITLAB_TOKEN", "SLACK_BOT_TOKEN", "DATABASE_URL", "POSTGRES_DATABASE_URL",
    "SENTRY_DSN", "GOOGLE_APPLICATION_CREDENTIALS", "STRIPE_SECRET_KEY", "MY_SERVICE_PASSWORD",
    "MAILGUN_API_KEY", "DEPLOY_PRIVATE_KEY", "VAULT_TOKEN_FILE", "SOME_APIKEY", "DB_PASSWD",
])
def test_secret_shaped_names_of_any_service_are_removed(name):
    assert codex_module.worker_environment({name: "x"}) == {}


@pytest.mark.parametrize("name", [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_HOME", "PATH", "SystemRoot", "TEMP",
    "USERPROFILE", "LOCALAPPDATA", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE", "ComSpec", "LANG",
])
def test_what_codex_needs_still_passes(name):
    assert codex_module.worker_environment({name: "x"}) == {name: "x"}


def test_off_windows_the_worker_refuses_before_any_process(tmp_path, monkeypatch, *, synthetic_worker_binary):
    """Containment (Job Object, census, final-path check) is Windows-only, so
    nothing runs elsewhere. os.name is patched only around the executor call:
    pathlib reads it too."""
    real_execute = codex_module.CodexExecutor._execute_noshell

    def posix_execute(self, *args, **kwargs):
        monkeypatch.setattr(codex_module.os, "name", "posix")
        try:
            return real_execute(self, *args, **kwargs)
        finally:
            monkeypatch.setattr(codex_module.os, "name", "nt")

    monkeypatch.setattr(codex_module.CodexExecutor, "_execute_noshell", posix_execute)
    run, result = _execute(tmp_path, TaskType.REVIEW, synthetic_worker_binary=synthetic_worker_binary)
    assert run.calls == []
    assert result.task_status_reason == "unsupported_platform"
