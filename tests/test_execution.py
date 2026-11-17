# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The Codex executor seen from outside: argv, stdin, environment, the
completion envelope, and the closed projection a result is reduced to."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

from modelquad import review
from modelquad.execution import (
    CodexExecutor,
    ExecutionRequest,
    ExecutionResult,
    OutcomeEvidence,
    WorkerTaskStatus,
    no_window_kwargs,
)
from modelquad.execution.base import WORKER_ENV_VAR
from modelquad.execution.closed import closed_result
from modelquad.models import TaskType


@pytest.fixture(autouse=True)
def isolate_executor_tempfiles(tmp_path, monkeypatch):
    # Even a failed scoped-HOME cleanup stays within this test's fixture.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


@pytest.fixture(autouse=True)
def qualified_codex(monkeypatch):
    """Codex tests here own argv and results, not feature qualification."""
    from modelquad.execution import codex as codex_module, noshell

    monkeypatch.setattr(codex_module, "_probe", lambda executable: "0.160.0")
    monkeypatch.setattr(noshell, "qualify", lambda executable, version, **kwargs:
                        noshell.Qualification(True, noshell.file_digest(executable), version, True,
                                              disabled_features=noshell.DISABLED_FEATURES))


class RecordingRun:
    def __init__(self, result=None, exc=None):
        from conftest import codex_event_stream

        self.calls = []
        self.result = result or subprocess.CompletedProcess(
            args=[], returncode=0, stdout=codex_event_stream(), stderr=""
        )
        self.exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return self.result


def _request(tmp_path, *, provider="codex", task_type=TaskType.REVIEW, **kwargs):
    return ExecutionRequest(
        provider=provider,
        task_type=task_type,
        prompt="do the thing",
        working_directory=tmp_path,
        **kwargs,
    )


def test_codex_fresh_run_command_construction(tmp_path, *, synthetic_worker_binary):
    from modelquad.execution import noshell

    resolved = synthetic_worker_binary('codex.exe')
    run = RecordingRun()
    request = _request(tmp_path)

    CodexExecutor(which=lambda _: resolved, run=run).execute(request)

    call = run.calls[0]
    argv = [os.path.normcase(os.path.realpath(call["argv"][0])), *call["argv"][1:]]
    assert argv == [resolved, "--ask-for-approval", "never", "exec", *noshell.NOSHELL_ARGS,
                    "--skip-git-repo-check", "-"]
    assert request.prompt not in call["argv"]
    # The task is delivered on stdin, followed by the instruction and the
    # completion-envelope protocol.
    assert call["kwargs"]["input"].startswith(request.prompt)
    assert call["kwargs"].get("shell", False) is False
    # An empty folder, never the workspace: no project configuration loads.
    assert call["kwargs"]["cwd"] != str(request.working_directory)


UNICODE_PROMPTS = [
    "Inspect this repository \u2014 read-only.",  # em dash
    "\u05d1\u05d3\u05d5\u05e7 \u05d0\u05ea \u05d4\u05e8\u05d9\u05e4\u05d5 \u05d4\u05d6\u05d4",  # Hebrew
    "Claude \u2192 Codex \u2713",  # arrow + checkmark
]


@pytest.mark.parametrize("prompt", UNICODE_PROMPTS)
def test_codex_forces_utf8_encoding_for_stdin(tmp_path, prompt, monkeypatch, *, synthetic_worker_binary):
    """Without an explicit encoding, `text=True` falls back to the OS locale
    codepage on Windows, which mis-encodes non-ASCII prompts and makes
    Codex's own UTF-8 stdin parser reject them -- reproduced live as
    "invalid byte at offset ...". The fix must not sanitize the prompt to
    ASCII; it must round-trip the exact Unicode text via explicit UTF-8.
    """

    from modelquad.execution import codex as codex_module, noshell
    # This test owns UTF-8 delivery; no provider/version listing is run.
    monkeypatch.setattr(codex_module, "_probe", lambda executable: "0.160.0")
    monkeypatch.setattr(noshell, "qualify", lambda executable, version, **kwargs:
                        noshell.Qualification(True, noshell.file_digest(executable), version, True))
    run = RecordingRun()
    request = ExecutionRequest(
        provider="codex",
        task_type=TaskType.EXPLORATION,
        prompt=prompt,
        working_directory=tmp_path,
    )

    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    call = run.calls[0]
    assert call["kwargs"]["encoding"] == "utf-8"
    assert call["kwargs"]["input"].startswith(prompt)
    assert "--ignore-user-config" in call["argv"]
    assert not any(token.startswith("mcp_servers.") for token in call["argv"])


@pytest.mark.parametrize("prompt", UNICODE_PROMPTS)
def test_codex_utf8_stdin_round_trips_through_a_real_subprocess(tmp_path, prompt):
    """End-to-end proof the fix technique actually works on this OS: a real
    subprocess (not a mock) that echoes stdin back must receive the exact
    Unicode prompt when invoked the same way CodexExecutor invokes Codex
    (text=True, encoding="utf-8", input=<prompt>) -- including the
    detach_stdin opt-out that input= requires.
    """

    echo_stdin = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]

    completed = subprocess.run(
        echo_stdin,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        **no_window_kwargs(detach_stdin=False),
    )

    assert completed.returncode == 0
    assert completed.stdout == prompt


def test_codex_sandbox_override_via_metadata(tmp_path, *, synthetic_worker_binary):
    run = RecordingRun()
    request = _request(tmp_path, metadata={"sandbox": "read-only"})

    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    argv = run.calls[0]["argv"]
    sandbox_index = argv.index("--sandbox")
    assert argv[sandbox_index + 1] == "read-only"


def test_codex_sandbox_override_rejects_unsafe_value(tmp_path, *, synthetic_worker_binary):
    run = RecordingRun()
    request = _request(tmp_path, metadata={"sandbox": "danger-full-access & calc.exe"})

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    assert result.success is False
    assert "invalid sandbox mode" in (result.error or "").lower()
    assert run.calls == []


def test_execution_request_rejects_shell_metacharacters_in_session_id(tmp_path):
    try:
        _request(tmp_path, session_id="123 & echo PWNED > pwned.txt & ")
    except ValueError as exc:
        assert "does not pass to codex" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError for unsafe session_id")


def test_execution_request_rejects_flag_like_session_id(tmp_path):
    try:
        _request(tmp_path, session_id="--dangerously-bypass-approvals-and-sandbox")
    except ValueError as exc:
        assert "must not start with" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError for flag-like session_id")


def test_execution_request_accepts_uuid_like_session_id(tmp_path):
    request = _request(tmp_path, session_id="01a0015e-a53f-7e63-8fb8-abad07eaf13c")
    assert request.session_id == "01a0015e-a53f-7e63-8fb8-abad07eaf13c"


def test_execution_request_rejects_unsafe_model_id(tmp_path):
    try:
        _request(tmp_path, model="gpt-5 & calc.exe")
    except ValueError as exc:
        assert "not a valid codex model id" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError for unsafe model id")


def test_codex_argv_unchanged_without_model_or_effort(tmp_path, *, synthetic_worker_binary):
    """Regression guard: omitting model/reasoning_effort must not alter argv at all."""
    from modelquad.execution import noshell

    run = RecordingRun()
    request = _request(tmp_path)

    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    argv = run.calls[0]["argv"]
    assert "-m" not in argv
    assert not any(token.startswith("model_reasoning_effort=") for token in argv)
    assert [os.path.normcase(os.path.realpath(argv[0])), *argv[1:]] == [
        synthetic_worker_binary('codex.exe'), "--ask-for-approval", "never", "exec",
        *noshell.NOSHELL_ARGS, "--skip-git-repo-check", "-",
    ]


def test_codex_argv_includes_model_and_reasoning_effort(tmp_path, *, synthetic_worker_binary):
    run = RecordingRun()
    request = _request(tmp_path, model="gpt-5.6-terra", reasoning_effort="high")

    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    argv = run.calls[0]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-5.6-terra"
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=high"


def test_codex_accepts_xhigh_reasoning_effort(tmp_path, *, synthetic_worker_binary):
    """gpt-5.2-codex documents xhigh as valid -- must not be rejected locally."""

    run = RecordingRun()
    request = _request(tmp_path, reasoning_effort="xhigh")

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    assert result.task_status_reason != "invalid_reasoning_effort"
    argv = run.calls[0]["argv"]
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=xhigh"


def test_codex_rejects_invalid_reasoning_effort_before_launch(tmp_path, *, synthetic_worker_binary):
    run = RecordingRun()
    request = _request(tmp_path, reasoning_effort="ludicrous")

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    assert result.success is False
    assert result.task_status_reason == "invalid_reasoning_effort"
    assert run.calls == []


def test_codex_result_metadata_reports_requested_model_honestly_unconfirmed(tmp_path, *, synthetic_worker_binary):
    run = RecordingRun()
    request = _request(tmp_path, model="gpt-5.6-terra", reasoning_effort="high")

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(request)

    assert result.metadata["requested_model"] == "gpt-5.6-terra"
    assert result.metadata["requested_reasoning_effort"] == "high"
    assert result.metadata["confirmed_model"] is None


def test_codex_not_found(tmp_path):
    run = RecordingRun()

    result = CodexExecutor(which=lambda _: None, run=run).execute(_request(tmp_path))

    assert result.success is False
    assert result.exit_code is None
    assert "not found" in (result.error or "").lower()
    assert run.calls == []


def test_codex_timeout(tmp_path, *, synthetic_worker_binary):
    run = RecordingRun(
        exc=subprocess.TimeoutExpired(cmd=["codex"], timeout=1, output="partial", stderr="")
    )

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    assert result.success is False
    assert result.exit_code is None
    assert "timed_out" in (result.error or "").lower()
    # A partial stream is never returned as an answer.
    assert result.output == ""
    assert result.metadata["timed_out"] is True


def test_codex_non_zero_exit(tmp_path, *, synthetic_worker_binary):
    from conftest import codex_event_stream

    run = RecordingRun(
        result=subprocess.CompletedProcess(args=[], returncode=2, stdout=codex_event_stream(), stderr="boom")
    )

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    assert result.success is False
    assert result.exit_code == 2
    # The stderr BODY is never persisted, in any evidence
    # mode, so `error` reports its category and size instead of quoting it.
    assert "boom" not in (result.error or "")
    assert "withheld from persistence" in (result.error or "")
    assert result.metadata["stderr_category"] == "narration"
    assert result.metadata["stderr_bytes"] == 4


@pytest.mark.parametrize(
    ("executor_type", "provider", "binary"),
    [
        (CodexExecutor, "codex", "C:/fake/codex"),
    ],
)
@pytest.mark.parametrize("existing_worker_value", [None, "already-set"])
def test_delegated_workers_always_receive_recursion_guard_environment(
    tmp_path,
    monkeypatch,
    executor_type,
    provider,
    binary,
    existing_worker_value,
    *, synthetic_worker_binary,
):
    run = RecordingRun()
    monkeypatch.setenv("MODELQUAD_EXECUTOR_TEST_VALUE", "preserved")
    if existing_worker_value is None:
        monkeypatch.delenv(WORKER_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(WORKER_ENV_VAR, existing_worker_value)

    executor_type(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(
        _request(tmp_path, provider=provider)
    )

    child_env = run.calls[0]["kwargs"]["env"]
    assert child_env[WORKER_ENV_VAR] == "1"
    assert child_env["MODELQUAD_EXECUTOR_TEST_VALUE"] == "preserved"
    assert child_env is not os.environ


@pytest.mark.parametrize("kind, reason", [("missing", "working_directory_missing"),
                                          ("file", "working_directory_not_a_folder")])
def test_a_working_directory_that_is_not_a_folder_is_refused_before_any_process(
        tmp_path, kind, reason, *, synthetic_worker_binary):
    codex_run = RecordingRun()
    executor = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=codex_run)
    target = tmp_path / ("does-not-exist" if kind == "missing" else "not_a_dir.txt")
    if kind == "file":
        target.write_text("not a directory", encoding="utf-8")

    reply = review.run(review.ReviewRequest("review", "do the thing", str(target)), executor)

    assert reply["refused"] is True
    assert reply["started"] is False and reply["executed"] is False
    assert reply["reason"] == reason
    # The refusal is a closed reply: the path never comes back.
    assert target.name not in json.dumps(reply)
    assert codex_run.calls == []


# --- task_status / task_completed semantics: process success != task success ---
#
# These four cases are the mandatory regression set: a clean exit code alone
# must never certify task completion, and a worker's own self-reported
# envelope can never override an independently detected failure signature.


def _answer(text):
    from conftest import codex_event_stream

    return RecordingRun(result=subprocess.CompletedProcess(args=[], returncode=0, stdout=codex_event_stream(text),
                                                           stderr=""))


def test_codex_exit0_with_success_prose_and_no_envelope_is_unknown_not_completed(tmp_path, *, synthetic_worker_binary):
    run = _answer("I successfully explored the repository and everything requested is done.")

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    assert result.task_status == WorkerTaskStatus.UNKNOWN
    assert result.task_completed is False


def test_codex_exit0_with_marker_and_no_envelope_is_unknown_not_completed(tmp_path, *, synthetic_worker_binary):
    run = _answer("DONE\nGLOBAL_MODELQUAD_E2E_OK\n")

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    assert result.task_status == WorkerTaskStatus.UNKNOWN
    assert result.task_completed is False


def test_codex_exit0_with_valid_completed_envelope_and_no_signature_is_completed(tmp_path, *, synthetic_worker_binary):
    envelope = (
        "```json\n"
        '{"task_status": "completed", "reason": null, '
        '"evidence": {"summary": "listed the repo"}}\n'
        "```"
    )
    run = _answer(f"codex\n{envelope}\n")

    result = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    assert result.task_status == WorkerTaskStatus.COMPLETED
    assert result.task_completed is True


def test_codex_strips_windows_apps_alias_from_path_on_windows(tmp_path, monkeypatch, *, synthetic_worker_binary):
    monkeypatch.setattr(os, "name", "nt")
    original_path = (
        r"C:\Users\example\AppData\Local\Microsoft\WindowsApps" + os.pathsep + r"C:\Windows\System32"
    )
    monkeypatch.setenv("PATH", original_path)
    run = RecordingRun()

    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    child_path = run.calls[0]["kwargs"]["env"]["PATH"]
    assert "WindowsApps" not in child_path
    assert r"C:\Windows\System32" in child_path


def test_codex_strips_store_package_windows_apps_dirs_too(tmp_path, monkeypatch, *, synthetic_worker_binary):
    """Store *package* dirs (C:\\Program Files\\WindowsApps\\<pkg>) carry ACLs
    that deny execution to Codex's restricted sandbox token: the Store
    PowerShell there caused CreateProcessAsUserW error 5 (reproduced live,
    codex-cli 0.147.0). Stripping them lets shell resolution fall through to
    System32 powershell.exe, which the same sandbox CAN spawn."""

    monkeypatch.setattr(os, "name", "nt")
    original_path = os.pathsep.join(
        (
            r"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe",
            r"C:\Users\example\AppData\Local\Microsoft\WindowsApps",
            r"C:\Windows\System32",
            r"C:\Windows\System32\WindowsPowerShell\v1.0",
        )
    )
    monkeypatch.setenv("PATH", original_path)
    run = RecordingRun()

    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    child_path = run.calls[0]["kwargs"]["env"]["PATH"]
    assert "WindowsApps" not in child_path
    assert r"C:\Windows\System32" in child_path
    assert r"C:\Windows\System32\WindowsPowerShell\v1.0" in child_path


def test_outcome_evidence_validation_and_roundtrip():
    evidence = OutcomeEvidence(
        process_success=True,
        semantic_status="completed",
        files_changed=("src/file1.py", "src/file2.py"),
        tests_executed=True,
        tests_passed=True,
        build_passed=True,
        lint_passed=True,
        artifact_validated=True,
        scope_matches=True,
        fallback_used=False,
        blocked_reason=None,
        duration_seconds=12.5,
        verification_level="objective",
    )
    payload = evidence.to_dict()
    assert payload["verification_level"] == "objective"
    assert payload["verified"] is True
    assert payload["files_changed"] == ["src/file1.py", "src/file2.py"]

    restored = OutcomeEvidence.from_dict(payload)
    assert restored == evidence

    # Invalid verification_level rejected
    with pytest.raises(ValueError, match="verification_level"):
        OutcomeEvidence(verification_level="speculative")


def test_outcome_evidence_verified_property_semantics():
    # Objective with tests_passed None and build_passed None -> True
    e1 = OutcomeEvidence(verification_level="objective")
    assert e1.verified is True

    # Objective with tests_passed True and build_passed True -> True
    e2 = OutcomeEvidence(verification_level="objective", tests_passed=True, build_passed=True)
    assert e2.verified is True

    # Objective with tests_passed False -> False
    e3 = OutcomeEvidence(verification_level="objective", tests_passed=False)
    assert e3.verified is False

    # Objective with build_passed False -> False
    e4 = OutcomeEvidence(verification_level="objective", build_passed=False)
    assert e4.verified is False

    # Non-objective levels are never verified
    for level in ("semantic", "process", "none"):
        e = OutcomeEvidence(verification_level=level, tests_passed=True, build_passed=True)
        assert e.verified is False


def test_execution_result_verified_completed_truth_table():
    objective_verified = OutcomeEvidence(verification_level="objective", tests_passed=True)
    objective_unverified = OutcomeEvidence(verification_level="objective", tests_passed=False)
    semantic_evidence = OutcomeEvidence(verification_level="semantic")

    # 1. Completed + verified objective evidence -> True
    r1 = ExecutionResult(
        provider="codex",
        success=True,
        output="ok",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        evidence=objective_verified,
    )
    assert r1.task_completed is True
    assert r1.verified_completed is True
    assert r1.to_dict()["evidence"]["verified"] is True

    # 2. Completed + no evidence -> False
    r2 = ExecutionResult(
        provider="codex",
        success=True,
        output="ok",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        evidence=None,
    )
    assert r2.task_completed is True
    assert r2.verified_completed is False
    assert r2.to_dict()["evidence"] is None

    # 3. Completed + unverified evidence -> False
    r3 = ExecutionResult(
        provider="codex",
        success=True,
        output="ok",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        evidence=objective_unverified,
    )
    assert r3.task_completed is True
    assert r3.verified_completed is False

    # 4. Non-completed + verified objective evidence -> False
    for status in (
        WorkerTaskStatus.FAILED,
        WorkerTaskStatus.BLOCKED,
        WorkerTaskStatus.PARTIAL,
        WorkerTaskStatus.UNKNOWN,
    ):
        r4 = ExecutionResult(
            provider="codex",
            success=False,
            output="fail",
            exit_code=1,
            task_status=status,
            evidence=objective_verified,
        )
        assert r4.task_completed is False
        assert r4.verified_completed is False


def test_no_window_kwargs_branches():
    """Both branches are exercised through the explicit os_name argument --
    monkeypatching the global ``os.name`` would break pathlib (Path would try
    to instantiate PosixPath on Windows), which is exactly why the helper
    takes this parameter."""

    nt_kwargs = no_window_kwargs("nt")
    assert nt_kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert nt_kwargs["creationflags"] & 0x08000000 != 0
    assert nt_kwargs["stdin"] is subprocess.DEVNULL

    assert no_window_kwargs("posix") == {"stdin": subprocess.DEVNULL}


def test_no_window_kwargs_fallback_when_attribute_missing(monkeypatch):
    import modelquad.procutil as procutil_module

    monkeypatch.setattr(procutil_module, "subprocess", object())
    assert no_window_kwargs("nt") == {"stdin": subprocess.DEVNULL, "creationflags": 0x08000000}


def test_no_window_kwargs_reexports_canonical():
    """Assert execution.base re-exports the exact canonical object."""
    import modelquad.execution.base as exec_base
    import modelquad.procutil as procutil_module

    assert exec_base.no_window_kwargs is procutil_module.no_window_kwargs


def test_codex_executor_passes_create_no_window_on_windows(tmp_path, *, synthetic_worker_binary):
    """This suite runs on Windows, so the executor must actually pass the flag
    here -- and it must come from the shared helper rather than a hardcoded
    literal, which the equality against ``no_window_kwargs()`` proves."""

    run = RecordingRun()
    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    call = run.calls[0]
    assert call["kwargs"].get("creationflags") == no_window_kwargs().get("creationflags")
    if os.name == "nt":
        assert call["kwargs"]["creationflags"] & 0x08000000 != 0


def test_codex_executor_omits_creationflags_on_non_windows(tmp_path, monkeypatch, *, synthetic_worker_binary):
    """A posix helper result must reach the executor as *no* kwarg at all --
    never ``creationflags=0``, which POSIX subprocess would reject."""

    import modelquad.execution.codex as codex_module

    monkeypatch.setattr(
        codex_module, "no_window_kwargs", lambda *a, **k: no_window_kwargs("posix", **k)
    )
    run = RecordingRun()
    CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run).execute(_request(tmp_path))

    assert "creationflags" not in run.calls[0]["kwargs"]


# --------------------------------------------------------------------------
# Persisted argv carries no absolute path, on EVERY exit path
# --------------------------------------------------------------------------


def _codex_metadata_command(run, tmp_path, *, synthetic_worker_binary):
    """The `command` metadata a codex run persists, whatever its outcome."""
    result = CodexExecutor(
        which=lambda _: synthetic_worker_binary("codex.exe"), run=run
    ).execute(_request(tmp_path))
    return result.metadata.get("command")


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(subprocess.TimeoutExpired(cmd="codex", timeout=1.0), id="timed_out"),
        pytest.param(FileNotFoundError(), id="cli_not_found"),
        pytest.param(OSError("denied"), id="launch_failed"),
    ],
)
def test_codex_failure_paths_persist_no_absolute_path(tmp_path, failure, synthetic_worker_binary):
    """AGENTS.md: "persisted argv carries no absolute path".

    The rule binds every exit from the executor -- not just the one that
    happened to build a redacted copy. The success path always redacted;
    timeout, missing-CLI and launch-failure stored `argv` raw, which is the
    resolved ABSOLUTE binary location.
    """

    def run(*_args, **_kwargs):
        raise failure

    command = _codex_metadata_command(run, tmp_path, synthetic_worker_binary=synthetic_worker_binary)

    assert command is not None
    assert command[0] == "codex.exe"
    assert not any("Program Files" in str(token) for token in command)
    assert not any(re.match(r"^[A-Za-z]:[\/]", str(token)) for token in command)


@pytest.mark.parametrize("error_type", [TypeError, OSError, FileNotFoundError])
def test_codex_stdin_writer_error_preserves_descendants_in_the_reply(tmp_path, error_type, *, synthetic_worker_binary, qualified_codex_inventory):
    """The executor's error result reaches the reply closed: its facts, none of its text."""

    from modelquad.execution.base import bound_descendant_facts

    failure = error_type("private writer output C:/private/project/file")
    failure.worker_descendants = _left(123, image="C:/private/tools/python.exe")
    expected = bound_descendant_facts(failure.worker_descendants)
    run = RecordingRun(exc=failure)
    # Read-only workers qualify a native binary, never an npm batch shim.
    executor = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run,
                             inventory=qualified_codex_inventory)
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    payload = review.run(review.ReviewRequest("review", "do the thing", str(project), files=("app.py",)),
                         executor)
    assert len(run.calls) == 1
    assert payload["task_status"] == "failed"
    assert payload["reason"] == "stdin_write_failed"
    assert payload["metadata"]["worker_descendants"] == expected
    assert payload["process_success"] is False
    assert payload["task_completed"] is False
    assert payload["output"] == ""
    assert payload["metadata"]["worker_started"] is True
    assert payload["metadata"]["noshell_qualified"] is True
    assert payload["metadata"]["noshell_verified_by_event_stream"] is False
    assert payload["metadata"]["noshell_violated"] is True
    assert "noshell_violated" in payload["limitations"]
    assert "worker_has_no_command_tools" not in payload["limitations"]
    replied = json.dumps(payload)
    assert "private" not in replied
    assert "writer output" not in replied


@pytest.mark.parametrize(
    "qualified,unified_exec,verified,violated,collaboration",
    [
        pytest.param(True, True, True, False, False, id="verified"),
        pytest.param(True, False, True, False, False, id="verified-unified-exec-false"),
        pytest.param(True, True, False, True, False, id="violated"),
        pytest.param(True, True, False, True, True, id="collaboration"),
        pytest.param(False, None, False, False, False, id="unavailable"),
    ],
)
def test_closed_result_preserves_closed_noshell_evidence(
    qualified, unified_exec, verified, violated, collaboration,
):
    from modelquad.execution import noshell

    metadata = {
        **noshell.Qualification(qualified, "private digest", "private version", unified_exec).metadata,
        **noshell.StreamVerification(verified, violated, collaboration).metadata,
        "command": ["codex.exe", "C:/private/project/file"],
        "noshell_unknown": "private worker prose",
    }
    labels = noshell.limitations(metadata, (
        "worker_command_refusal_not_enforced", "worker_reads_not_confined",
        "diagnosis_is_not_verification", "live_workspace_not_isolated",
    ))
    result = ExecutionResult(
        provider="codex", success=verified, output="", exit_code=0,
        metadata=metadata, limitations=(*labels, "private worker prose", "noshell_unknown", 1),
    )

    clean = closed_result(result)

    expected = {
        "noshell_qualified": qualified,
        "noshell_verified_by_event_stream": verified,
        "noshell_violated": violated,
        "noshell_collaboration_called": collaboration,
    }
    if type(unified_exec) is bool:
        expected["noshell_unified_exec_observed"] = unified_exec
    assert clean.metadata == expected
    assert clean.limitations == labels


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="unknown"),
        pytest.param(0, id="integer-zero"),
        pytest.param(1, id="integer-one"),
        pytest.param("true C:/private/project/file", id="prose"),
        pytest.param({"command": "private command"}, id="mapping"),
        pytest.param([True], id="list"),
    ],
)
def test_closed_result_rejects_non_boolean_noshell_metadata(value):
    metadata = dict.fromkeys((
        "noshell_qualified", "noshell_unified_exec_observed",
        "noshell_verified_by_event_stream", "noshell_violated", "noshell_collaboration_called",
    ), value)
    result = ExecutionResult(provider="codex", success=False, output="", exit_code=None, metadata=metadata)

    assert closed_result(result).metadata == {}


def test_codex_closes_unattributed_runner_errors_without_their_text(tmp_path, synthetic_worker_binary):
    failure = TypeError("runner programming error")
    result = CodexExecutor(which=lambda _: synthetic_worker_binary("codex.exe"),
                           run=RecordingRun(exc=failure)).execute(_request(tmp_path))
    assert result.task_status_reason == "worker_noshell_unavailable"
    assert result.output == ""
    assert "runner programming error" not in json.dumps(result.to_dict(), default=str)


def test_codex_success_path_still_persists_no_absolute_path(tmp_path, synthetic_worker_binary):
    """The behaviour the failure paths were brought into line with."""

    from conftest import codex_event_stream

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, codex_event_stream(), "")

    command = _codex_metadata_command(run, tmp_path, synthetic_worker_binary=synthetic_worker_binary)

    assert command[0] == "codex.exe"
    assert not any(re.match(r"^[A-Za-z]:[\/]", str(token)) for token in command)


def _left(pid, image="node.exe", created=1.0, enumerated=True, **extra):
    facts = {"enumerated": enumerated, "method": "toolhelp32", "observed_at": 2.0, "descendants": [{"pid": pid, "image": image, "created": created}]}
    facts.update(extra)
    return facts


def test_a_workers_leftovers_are_carried_closed_and_never_invented():
    looked = ExecutionResult(
        provider="codex", success=True, output="", exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED, metadata={"worker_descendants": _left(222)},
    )
    # The closed projection states truncation and reason explicitly.
    assert closed_result(looked).metadata["worker_descendants"] == {
        **_left(222), "truncated": False, "reason": None}
    # A runner that did not look attaches nothing, and nothing is invented for it.
    did_not_look = ExecutionResult(
        provider="codex", success=True, output="", exit_code=0,
        task_status=WorkerTaskStatus.BLOCKED, task_status_reason="sandbox_process_spawn_failed",
        safe_to_retry=True,
    )
    assert "worker_descendants" not in closed_result(did_not_look).metadata


# --- The headline status answers one question --------------------------------
#
# Four consecutive delegations came back `blocked` with the work complete and
# correct, because the envelope instruction told every worker "use blocked if
# any tool call was denied". The contract (2026-09-14): `completed` when
# the objective was done, a denied NON-ESSENTIAL tool reported separately as a
# limitation, `blocked` only for a REQUIRED capability that was unavailable.


def test_envelope_instruction_states_the_completion_contract():
    from modelquad.execution.base import _COMPLETION_ENVELOPE_INSTRUCTION as text
    from modelquad.execution.base import parse_completion_envelope, worker_limitations

    lowered = text.lower()
    assert "any tool call was denied" not in lowered, (
        "the old rule turned every denied tool call into a blocked headline"
    )
    assert '"limitations"' in text
    assert "even if some tool call was denied" in lowered
    assert "only if the task could not be completed" in lowered
    assert "required" in lowered
    # The example the worker is told to copy "exactly" must itself be an
    # envelope the product's own parser accepts -- valid JSON is not enough,
    # the status has to be one of the allowed four -- or a worker that
    # follows it literally is classified UNKNOWN (Round 1 and Round 3
    # review blockers). Read it with the real parser, not a JSON decoder.
    parsed = parse_completion_envelope(text)
    assert parsed is not None
    assert set(parsed) == {"task_status", "reason", "limitations", "files_needed", "evidence"}
    assert parsed["task_status"] == "completed"
    assert isinstance(parsed["limitations"], list) and len(parsed["limitations"]) == 1
    assert worker_limitations(text) == tuple(parsed["limitations"])
    # The example names no file, so a worker copying it asks for nothing.
    assert parsed["files_needed"] == []
    assert '"files_needed"' in text and "were not given" in text
    for status in ("completed", "blocked", "partial", "failed"):
        assert f'"{status}"' in text


def test_completed_with_limitations_is_completed_and_the_limitations_are_carried():
    from modelquad.execution.base import classify_worker_result, worker_limitations

    output = (
        "Fixed the bug and ran the focused tests.\n```json\n"
        '{"task_status": "completed", "reason": null, '
        '"limitations": ["temp-directory cleanup was denied; left the scratch files in place", '
        '"network test could not run in the sandbox; verified with the offline fixture"], '
        '"evidence": {"summary": "3 tests pass"}}\n```'
    )
    status, reason, retry = classify_worker_result(
        returncode=0, combined_output=output, primary_output=output
    )
    assert status is WorkerTaskStatus.COMPLETED and reason is None and retry is False
    assert worker_limitations(output) == (
        "temp-directory cleanup was denied; left the scratch files in place",
        "network test could not run in the sandbox; verified with the offline fixture",
    )


def test_limitations_are_absent_not_fatal_when_missing_or_malformed():
    from modelquad.execution.base import classify_worker_result, worker_limitations

    plain = '```json\n{"task_status": "completed", "reason": null}\n```'
    assert worker_limitations(plain) == ()
    assert worker_limitations("no envelope at all") == ()
    # A bare string is not the list the schema names and is dropped whole;
    # non-string and blank members are dropped; a dict is not a list and
    # yields nothing. None of it changes the headline.
    single = '```json\n{"task_status": "completed", "limitations": "one denied call"}\n```'
    assert worker_limitations(single) == ()
    mixed = (
        '```json\n{"task_status": "completed", '
        '"limitations": [42, "", "  kept  ", null, {"x": 1}]}\n```'
    )
    assert worker_limitations(mixed) == ("kept",)
    wrong_shape = '```json\n{"task_status": "completed", "limitations": {"a": "b"}}\n```'
    assert worker_limitations(wrong_shape) == ()
    for text in (single, mixed, wrong_shape):
        status, _, _ = classify_worker_result(returncode=0, combined_output=text, primary_output=text)
        assert status is WorkerTaskStatus.COMPLETED


def test_limitations_are_bounded():
    from modelquad.execution.base import worker_limitations
    import json as _json

    text = "```json\n" + _json.dumps(
        {"task_status": "completed", "limitations": ["x" * 2000] + [f"l{i}" for i in range(40)]}
    ) + "\n```"
    kept = worker_limitations(text)
    assert len(kept) == 20
    assert len(kept[0]) == 500


def test_a_blocked_envelope_with_limitations_stays_blocked_and_keeps_them():
    """`blocked` is the worker's judgement that a REQUIRED capability was
    missing; the contract keeps that judgement. Limitations ride beside it."""

    from modelquad.execution.base import classify_worker_result, worker_limitations

    text = (
        '```json\n{"task_status": "blocked", '
        '"reason": "the edit tool itself was denied; nothing could be written", '
        '"limitations": ["also could not read the docs directory"]}\n```'
    )
    status, reason, retry = classify_worker_result(returncode=0, combined_output=text, primary_output=text)
    assert status is WorkerTaskStatus.BLOCKED
    assert reason == "the edit tool itself was denied; nothing could be written"
    assert retry is True
    assert worker_limitations(text) == ("also could not read the docs directory",)


def test_execution_result_surfaces_limitations_beside_the_headline():
    from modelquad.execution.base import ExecutionResult

    result = ExecutionResult(
        provider="codex",
        success=True,
        output="",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        limitations=("cleanup denied",),
    )
    payload = result.to_dict()
    assert payload["task_status"] == "completed"
    assert payload["task_completed"] is True
    assert payload["limitations"] == ["cleanup denied"]
    assert ExecutionResult(provider="codex", success=True, output="", exit_code=0).to_dict()["limitations"] == []


# --- Reading the completion envelope takes linear time -------------------------
#
# Codex-authored text can be megabytes long and is read several times per
# result, outside the worker timeout. The scan used to be a lazy regex that
# re-read the rest of the text from every unclosed ```json opener.

_ENVELOPE_OK = '{"task_status": "completed", "reason": null, "limitations": ["x"]}'
_ENVELOPE_BLOCKED = '{"task_status": "blocked", "reason": "needs network"}'

_ENVELOPE_CASES = [
    "", "no fence at all", _ENVELOPE_OK, "```json\n" + _ENVELOPE_OK, _ENVELOPE_OK + "\n```",
    "```json\n" + _ENVELOPE_OK + "\n```",
    "```json" + _ENVELOPE_OK + "```",
    "```json \t\r\n\n" + _ENVELOPE_OK + " \n\t ```",
    "```json  " + _ENVELOPE_OK + "　\x1c```",
    "text\n```json\n" + _ENVELOPE_BLOCKED + "\n```\nmore\n```json\n" + _ENVELOPE_OK + "\n```\n",
    "```json\n" + _ENVELOPE_OK + "\n```\n```json\n" + _ENVELOPE_BLOCKED + "\n```",
    "```json\n" + _ENVELOPE_OK + "\n```\n```json\n{not json}\n```",
    "```json\n" + _ENVELOPE_OK + "\n```\n```json\n[1]\n```",
    "```json\n" + _ENVELOPE_OK + '\n```\n```json\n{"task_status": "unknown"}\n```',
    "```json\n" + _ENVELOPE_OK + '\n```\n```json\n{"task_status": "done"}\n```',
    "```json\n" + _ENVELOPE_OK + "\n```\n```json\n{xxx\n",
    "```json\n{xxx\n```json\n" + _ENVELOPE_OK + "\n```",
    "```json\n{xxx\n```\n```json\n" + _ENVELOPE_OK + "\n```",
    "```json\nx\n```\n```json\n" + _ENVELOPE_OK + "\n```",
    "```json\n```json\n" + _ENVELOPE_OK + "\n```",
    "```python\n" + _ENVELOPE_OK + "\n```",
    "```JSON\n" + _ENVELOPE_OK + "\n```",
    "```\n" + _ENVELOPE_OK + "\n```",
    "````json\n" + _ENVELOPE_OK + "\n````",
    "```json\n" + _ENVELOPE_OK + "\n``",
    "```json\n" + _ENVELOPE_OK + "} ```",
    '```json\n{"task_status": "partial", "evidence": {"summary": "a } ``` b"}}\n```',
    '```json\n{"task_status": "partial", "evidence": {"a": {}}\n}\n```',
    "```json\n{}\n```", "```json{}```", "```json\n{\n```", "```json\n}\n```", "```json{``````json{}```",
    "```json\n" + _ENVELOPE_OK + "\n```" * 3,
]


def _envelope_by_the_old_regex(text):
    for candidate in reversed(re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("task_status") in {"completed", "blocked", "partial", "failed"}:
            return parsed
    return None


@pytest.mark.parametrize("text", _ENVELOPE_CASES, ids=range(len(_ENVELOPE_CASES)))
def test_the_linear_envelope_scan_reads_what_the_regex_read(text):
    from modelquad.execution.base import parse_completion_envelope

    assert parse_completion_envelope(text) == _envelope_by_the_old_regex(text)


def test_the_envelope_scan_agrees_with_the_regex_on_generated_text():
    import random

    from modelquad.execution.base import parse_completion_envelope

    pieces = ["```json", "```", "``", "{", "}", " ", "\n", "x", _ENVELOPE_OK, _ENVELOPE_BLOCKED, "[1]",
              '{"task_status": "nope"}', " "]
    rng = random.Random(20261111)
    for _ in range(4000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        assert parse_completion_envelope(text) == _envelope_by_the_old_regex(text), repr(text)


def test_the_envelope_semantics_on_the_ordinary_cases():
    from modelquad.execution.base import classify_worker_result, parse_completion_envelope

    assert parse_completion_envelope(None) is None
    assert parse_completion_envelope(_ENVELOPE_OK) is None  # unfenced text is not an envelope
    fenced = "```json\n" + _ENVELOPE_OK + "\n```"
    assert parse_completion_envelope(fenced) == json.loads(_ENVELOPE_OK)
    # The last well-formed envelope wins; a later malformed one does not hide it.
    both = "```json\n" + _ENVELOPE_OK + "\n```\nlater\n```json\n" + _ENVELOPE_BLOCKED + "\n```"
    assert parse_completion_envelope(both)["task_status"] == "blocked"
    assert parse_completion_envelope(both + "\n```json\n{broken\n```")["task_status"] == "blocked"
    assert parse_completion_envelope('```json\n{"task_status": "unknown"}\n```') is None
    assert classify_worker_result(returncode=0, combined_output=both, primary_output=both) == (
        WorkerTaskStatus.BLOCKED, "needs network", True)


@pytest.mark.parametrize("shape", [
    "```json\n{xxx\n", "```json\n{", "```json", "```json\n", "```\n", "}\n```\n", "} ```", "}", "{", "`",
    "```json\n{``` ", "```json {} ``", "```json\n{}\n```\n", "```json\n{\"a\": 1}\n```\n", "} ", "```json ",
], ids=range(16))
def test_the_envelope_scan_is_linear_on_hostile_text(shape):
    import time

    from modelquad.execution.base import parse_completion_envelope, worker_files_needed, worker_limitations

    size = 2_000_000
    text = shape * (size // len(shape))
    tail = "\n```json\n" + _ENVELOPE_BLOCKED + "\n```"
    for candidate in (text, text + tail, "```json\n{" + text, "```json\n{" + text + "}\n```"):
        for read in (parse_completion_envelope, worker_limitations, worker_files_needed):
            started = time.perf_counter()
            read(candidate)
            assert time.perf_counter() - started < 2


@pytest.mark.parametrize("text", [
    "```json\n{" + "x" * 2_000_000, "```json" + " " * 2_000_000, "```json\n{}" + " " * 2_000_000,
    "```json\n{" + "}" * 2_000_000, "`" * 2_000_000, "```json\n{" + "`" * 2_000_000 + "}\n```",
    "```json\n" + "{" * 2_000_000 + "}\n```", "```json\n" + "[" * 200_000 + "\n```",
], ids=["long-line", "spaces-after-opener", "spaces-before-closer", "braces", "backticks", "nested-backticks",
        "open-braces", "deep-list"])
def test_the_envelope_scan_is_linear_on_one_very_long_run(text):
    import time

    from modelquad.execution.base import parse_completion_envelope

    started = time.perf_counter()
    assert parse_completion_envelope(text) is None
    assert time.perf_counter() - started < 2
