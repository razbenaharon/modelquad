# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Executor for the Codex command-line interface: read-only consultations only.

Every launch is a fresh ``codex exec`` with its command, code, plugin and app
tools turned off, fed the task and the files the user approved in the tool
call (request metadata ``supplied_files``; ``noshell.py``). A
task type outside ``READ_ONLY_TASK_TYPES`` is refused before any process
starts, and so is a resumed session. Delegated workers receive a
recursion-guard environment value used by adapters.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from ..codex_config import CODEX_REASONING_EFFORTS
from ..codexcli import _probe, launcher_only, which_codex
from ..executable_resolution import UnsafeExecutable, safe_child_env
from ..procutil import WorkerStopRequested, run_worker
from .codex_worker_mcp import InventoryRunner, UnsafeBatchArgument, prepare_codex_launch, run_inventory
from . import noshell

from .base import (
    READ_ONLY_RESUME_UNPINNED,
    READ_ONLY_TASK_TYPES,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    WORKER_ENV_VAR,
    WorkerTaskStatus,
    TerminalResolution,
    classify_stderr,
    classify_worker_result,
    worker_limitations,
    descendant_facts,
    no_window_kwargs,
    result_error,
    termination_facts,
    foundation_authority_refusal,
)

#: The only sandbox a Codex worker runs with.
VALID_SANDBOX_MODES = frozenset({"read-only"})

#: Credentials of other services, removed from the environment a Codex worker
#: (and its feature probe) inherits. Everything else passes through: PATH,
#: proxies, certificates, CODEX_HOME, locale. OPENAI_API_KEY and CODEX_API_KEY
#: stay because they are how the user chose to sign Codex itself in.
#: Patterns, matched case-insensitively as Windows names are.
WORKER_ENV_EXCLUDED = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_*TOKEN*",
    "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AZURE_*SECRET*", "AZURE_*PASSWORD*", "AZURE_*KEY", "AZURE_*TOKEN*", "AZURE_*CONNECTION_STRING*",
)


def worker_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a Codex child receives: inherited, minus unrelated secrets."""
    source = os.environ if environ is None else environ
    return {
        key: value for key, value in source.items()
        if not any(fnmatch.fnmatchcase(key.upper(), pattern) for pattern in WORKER_ENV_EXCLUDED)
    }


class NoShellResult(ExecutionResult):
    """Keep the no-shell refusal lifecycle local, without changing shared policy.

    dataclasses.replace (including manager/answer normalization) retains this
    type and re-establishes the same terminal verdict from execution facts.
    Pre-launch refusal labels are policy refusals in ``POLICY_REFUSAL_REASONS``.
    """

    def __post_init__(self):
        super().__post_init__()
        if not self.metadata.get("timed_out") and not self.metadata.get("stop_requested"):
            if self.task_status_reason == noshell.UNAVAILABLE:
                terminal = (TerminalResolution("failed", "exception") if self.metadata.get("worker_started")
                            else TerminalResolution("refused", "policy_refused"))
                object.__setattr__(self, "_terminal_resolution", terminal)
            elif self.task_status_reason == noshell.VIOLATED:
                object.__setattr__(self, "_terminal_resolution", TerminalResolution("failed", "exception"))

    @property
    def host_diagnostics(self):
        return getattr(self, "_host_diagnostics", ())

    def __getstate__(self):
        # Generic pickle must not turn the transient Host channel into storage.
        return {key: value for key, value in self.__dict__.items() if key != "_host_diagnostics"}

    def __setstate__(self, state):
        self.__dict__.update(state)


# The `codex exec --help` output does not itself enumerate valid
# `-c model_reasoning_effort=` values (unlike `--sandbox`, which lists its
# choices). This is the union across Codex's own reasoning models, not a single
# model's exact set (OpenAI's docs for gpt-5.2-codex list low/medium/high/xhigh).
# A given model may not support every level in this union; Codex itself
# rejects an unsupported combination, which surfaces as a clean non-zero-exit
# result, never silently ignored.
VALID_REASONING_EFFORTS = CODEX_REASONING_EFFORTS

# Windows App Execution Alias directories hold stub executables that a
# restricted token cannot start (https://github.com/openai/codex/issues/20570).
_WINDOWS_APPS_ALIAS_SEGMENT = os.path.join("Microsoft", "WindowsApps")


def filter_windows_apps_alias_from_path(path_value: str) -> str:
    """Remove every WindowsApps directory from a PATH string.

    Two distinct WindowsApps cases break Codex's sandboxed process creation,
    both reproduced live:

    * the App Execution Alias dir (``...\\Microsoft\\WindowsApps`` -- stub
      exes; ``CreateProcessAsUserW failed: 1920``), and
    * Store *package* dirs (``C:\\Program Files\\WindowsApps\\<pkg>`` -- e.g.
      the Store-installed PowerShell 7; their package ACLs deny execution to
      Codex's restricted sandbox token: ``CreateProcessAsUserW failed: 5``,
      reproduced 2026-08-25 with codex-cli 0.147.0).

    Stripping any entry with a ``WindowsApps`` path segment lets a shell
    lookup fall through to classic ``powershell.exe`` in System32. The doctor
    uses this to report which shell such a lookup would find. This only ever
    narrows a PATH; it grants nothing.
    """

    entries = path_value.split(os.pathsep)
    kept = [
        entry
        for entry in entries
        if "windowsapps" not in {segment.lower() for segment in Path(entry).parts}
    ]
    return os.pathsep.join(kept)


class CodexExecutor(Executor):
    """Run fresh, no-shell, read-only Codex consultations with prompts on stdin."""

    name = "codex"

    def __init__(
        self,
        binary: str = "codex",
        *,
        which: Callable[[str], str | None] | None = None,
        # run_worker, not subprocess.run: a timeout takes the worker's whole
        # tree down, and so does this process's death (kill on close).
        run: Callable[..., subprocess.CompletedProcess] = run_worker,
        inventory: InventoryRunner | None = None,
    ) -> None:
        self.binary = binary
        # Runs the provider-free `codex features list` qualification probe.
        # None means the real CLI; tests inject a listing.
        self._inventory = inventory
        self._which = which
        self._run = run

    def _resolve(self) -> str | None:
        # The newest installed native Codex CLI, the official install on a
        # tie; an npm launcher is replaced by the codex.exe its package ships
        # (codexcli.py). Any other binary name is looked up as given.
        if self._which is None:
            return which_codex(self.binary, native=True)
        return which_codex(self.binary, which=self._which, native=True)

    def unavailable_reason(self) -> str | None:
        """None when a worker can be launched; otherwise a closed label.

        ``codex_shim_only``: only an npm launcher with no native binary behind
        it was found. ``cli_not_found``: no Codex at all. ``unsafe_executable``:
        the executable fails launch admission.
        """
        resolved = self._resolve()
        if resolved is None:
            shim_only = (launcher_only(self.binary) if self._which is None
                         else launcher_only(self.binary, which=self._which))
            return "codex_shim_only" if shim_only else "cli_not_found"
        try:
            prepare_codex_launch([resolved, "exec"], os.getcwd())
        except (UnsafeExecutable, UnsafeBatchArgument):
            return "unsafe_executable"
        return None

    def is_available(self) -> bool:
        return self.unavailable_reason() is None

    def _launch_refusal(self, exc: UnsafeExecutable | UnsafeBatchArgument) -> ExecutionResult:
        batch = isinstance(exc, UnsafeBatchArgument)
        reason = "unsafe_batch_argument" if batch else "unsafe_executable"
        detail = exc.role if batch else exc.reason
        return ExecutionResult(
            provider=self.name, success=False, output="", exit_code=None,
            error=f"ModelQuad refused the Codex launch: {reason} ({detail}).",
            task_status=WorkerTaskStatus.FAILED, task_status_reason=reason,
            safe_to_retry=False,
            metadata={"argument_role" if batch else "executable_refusal": detail},
        )

    def _noshell_refusal(self, reason: str, metadata: dict, diagnostics=()) -> NoShellResult:
        return noshell.bind_host_diagnostics(NoShellResult(
            provider=self.name, success=False, output="", exit_code=None,
            error="ModelQuad refused the Codex consultation: " + noshell.refusal_message(reason),
            task_status=WorkerTaskStatus.BLOCKED, task_status_reason=reason,
            metadata=metadata, safe_to_retry=False, limitations=noshell.limitations(metadata),
        ), diagnostics)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        refused = foundation_authority_refusal(request)
        if refused is not None:
            return refused
        if request.task_type not in READ_ONLY_TASK_TYPES:
            return ExecutionResult(
                provider=self.name, success=False, output="", exit_code=None,
                error=(f"Codex runs read-only consultations only; a {request.task_type.value} "
                       "task cannot be delegated."),
                task_status=WorkerTaskStatus.BLOCKED,
                task_status_reason="beta_read_only_required",
                safe_to_retry=False, metadata={"worker_started": False},
            )
        resolved = self._resolve()
        if resolved is None:
            if self.unavailable_reason() == "codex_shim_only":
                return self._noshell_refusal("codex_shim_only", {"worker_started": False})
            return ExecutionResult(
                provider=self.name,
                success=False,
                output="",
                exit_code=None,
                error=f"codex CLI not found on PATH (looked for {self.binary!r}).",
                task_status=WorkerTaskStatus.FAILED,
                task_status_reason="cli_not_found",
            )

        # Gate before any capability probe; a bad model id starts nothing.
        preflight = [resolved, "exec"]
        if request.model:
            preflight += ["-m", request.model]
        try:
            prepare_codex_launch(preflight, str(request.working_directory))
        except (UnsafeExecutable, UnsafeBatchArgument) as exc:
            return self._launch_refusal(exc)

        if request.reasoning_effort is not None and request.reasoning_effort not in VALID_REASONING_EFFORTS:
            return ExecutionResult(
                provider=self.name,
                success=False,
                output="",
                exit_code=None,
                error=(
                    f"Invalid reasoning_effort {request.reasoning_effort!r}; must be one of "
                    f"{sorted(VALID_REASONING_EFFORTS)}."
                ),
                task_status=WorkerTaskStatus.FAILED,
                task_status_reason="invalid_reasoning_effort",
            )
        model_args: list[str] = []
        if request.model:
            model_args += ["-m", request.model]
        if request.reasoning_effort:
            model_args += ["-c", f"model_reasoning_effort={request.reasoning_effort}"]

        sandbox = request.metadata.get("sandbox", "read-only")
        if not isinstance(sandbox, str) or sandbox not in VALID_SANDBOX_MODES:
            return ExecutionResult(
                provider=self.name, success=False, output="", exit_code=None,
                error=(f"Invalid sandbox mode {sandbox!r} for a {request.task_type.value} task: "
                       "Codex runs only with the 'read-only' sandbox."),
                task_status=WorkerTaskStatus.FAILED,
                task_status_reason="invalid_sandbox_mode",
            )
        metadata: dict[str, object] = {"requested_sandbox": sandbox}
        for value, field in ((request.model, "model_source"),
                             (request.reasoning_effort, "reasoning_effort_source")):
            if value is not None:
                source = request.metadata.get(field)
                metadata[field] = (source if type(source) is str and source in {"user_config", "explicit", "default_policy"}
                                   else "default_policy")
        # No-shell controls have never been qualified for resumed sessions.
        if request.session_id:
            return ExecutionResult(
                provider=self.name, success=False, output="", exit_code=None,
                error=("Codex consultations require a fresh session; omit session_id. "
                       "A fresh session runs with --sandbox read-only."),
                task_status=WorkerTaskStatus.BLOCKED,
                task_status_reason=READ_ONLY_RESUME_UNPINNED,
                safe_to_retry=False, metadata={**metadata, "worker_started": False},
            )
        return self._execute_noshell(request, resolved, model_args, metadata)

    def _execute_noshell(self, request, resolved, model_args, metadata):
        """One admission point shared by direct, inline and service workers."""
        from ..workerevidence import parse_codex_stream

        deadline = time.monotonic() + request.timeout
        metadata = {**metadata, "worker_started": False, "noshell_qualified": False,
                    "noshell_verified_by_event_stream": False}
        diagnostics = []
        child_env = worker_environment()
        if os.name == "nt" and "PATH" in child_env:
            child_env["PATH"] = filter_windows_apps_alias_from_path(child_env["PATH"])
        child_env[WORKER_ENV_VAR] = "1"
        child_env = safe_child_env(child_env)

        def refused(reason=noshell.UNAVAILABLE, *, code=None):
            return noshell.bind_host_diagnostics(NoShellResult(
                provider=self.name, success=False, output="", exit_code=code,
                error=("ModelQuad refused the Codex consultation: " + noshell.refusal_message(reason)
                       if reason in noshell.REFUSAL_LABELS
                       else "ModelQuad refused the no-shell worker result: " + reason + "."),
                task_status=(WorkerTaskStatus.FAILED if reason in {"timed_out", "stop_requested"}
                             else WorkerTaskStatus.BLOCKED), task_status_reason=reason,
                metadata=metadata, safe_to_retry=False,
                limitations=noshell.limitations(metadata),
            ), diagnostics)

        def inventory(argv, cwd, env):
            # The existing runner pins the executable and bounds/kills its tree.
            return (self._inventory or run_inventory)(argv, cwd, env)

        def verify_result(raw, result):
            metadata.update(descendant_facts(result))
            verification = noshell.verify_stream(raw)
            # A supplied-file worker has no expected executable descendants
            # left running at exit. Parent events cannot certify their tools,
            # even when that stream is clean. Keep the bounded census evidence
            # and withhold both the answer and certification on any sighting.
            if metadata.get("worker_descendants", {}).get("descendants"):
                verification = noshell.StreamVerification(
                    False, True, verification.collaboration_called,
                )
            metadata.update(verification.metadata)
            return verification

        try:
            # Refuse essential supplied-context failures before any native
            # probe/model launch. Include completion protocol in the same cap.
            # Only the files the user approved in the tool call are attached.
            files = request.metadata.get("supplied_files", ())
            if type(files) not in (list, tuple) or any(type(name) is not str for name in files):
                return refused("supplied_file_unavailable")
            report = {}
            try:
                prompt = noshell.supplied_prompt(request.prompt, request.working_directory,
                                                files=files, completion_protocol=True,
                                                diagnostics=diagnostics, report=report)
            except noshell.SuppliedContextRefused as refusal:
                return refused(refusal.reason)
            # Only native executables can qualify; no batch launcher can bind a
            # digest to the executable whose feature listing was inspected.
            from ..executable_resolution import admit_executable
            admitted = admit_executable(resolved, cwd=str(request.working_directory), allow_batch=False)
            if admitted.kind == "batch" or (admitted.path is None and admitted.refusal == "batch_script"):
                return refused("codex_shim_only")
            if (admitted.path is None or admitted.kind != "binary"
                    or os.path.normcase(os.path.realpath(admitted.path)) !=
                       os.path.normcase(os.path.realpath(resolved))):
                return refused("codex_executable_unverified")
            resolved = admitted.path
            digest_before_version = noshell.file_digest(resolved)
            if digest_before_version is None:
                return refused("codex_executable_unverified")
            version = _probe(resolved)
            # An empty, ephemeral folder keeps project instructions/configuration
            # out of both qualification and model entry. Auth is never copied.
            with tempfile.TemporaryDirectory(prefix="modelquad-noshell-") as empty:
                qualification = noshell.qualify(
                    resolved, version, cwd=empty, env=child_env, runner=inventory,
                )
                metadata.update(qualification.metadata)
                if not qualification.qualified:
                    return refused(qualification.refusal or "codex_feature_unverified")
                if qualification.executable_digest != digest_before_version:
                    return refused("codex_executable_unverified")
                # --ignore-user-config loads no MCP registrations, plugins or
                # profiles; an mcp_tool_call in the event stream is a violation.
                argv = [resolved, "--ask-for-approval", "never", "exec", *model_args,
                        *qualification.launch_args, "--skip-git-repo-check", "-"]
                metadata["command"] = [Path(resolved).name, *argv[1:]]
                metadata.update({"requested_model": request.model,
                                 "requested_reasoning_effort": request.reasoning_effort,
                                 "confirmed_model": None})
                if noshell.file_digest(resolved) != qualification.executable_digest:
                    return refused("codex_executable_unverified")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return refused()
                if request.stop_requested is not None and request.stop_requested():
                    metadata["stop_requested"] = True
                    return refused("stop_requested")
                launch, launch_kwargs = prepare_codex_launch(argv, empty)
                def spawned(pid):
                    metadata["worker_started"] = True
                    if request.on_spawn is not None:
                        request.on_spawn(pid)

                # Metadata-only record of what this launch hands the worker:
                # relative paths, sizes and digest prefixes, never contents.
                metadata["file_audit"] = noshell.file_audit(report, request.metadata.get("approval"))
                kwargs = {"on_spawn": spawned}
                if request.stop_requested is not None:
                    kwargs["stop_requested"] = request.stop_requested
                try:
                    completed = self._run(
                        launch, cwd=empty, input=prompt, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=remaining, shell=False,
                        env=child_env, **no_window_kwargs(detach_stdin=False), **kwargs, **launch_kwargs,
                    )
                except (WorkerStopRequested, subprocess.TimeoutExpired):
                    raise
                except Exception as error:
                    # The runner attaches a census only after normal worker
                    # exit, including when its stdin writer failed. Preserve
                    # that attribution; certify no event stream and never
                    # return the exception's prose.
                    if not descendant_facts(error):
                        raise
                    metadata["worker_started"] = True
                    verify_result("", error)
                    return noshell.bind_host_diagnostics(NoShellResult(
                        provider=self.name, success=False, output="", exit_code=None,
                        error="codex stdin writer failed", metadata=metadata,
                        task_status=WorkerTaskStatus.FAILED,
                        task_status_reason="stdin_write_failed", safe_to_retry=False,
                        limitations=noshell.limitations(metadata),
                    ), diagnostics)
                metadata["worker_started"] = True
        except (WorkerStopRequested, subprocess.TimeoutExpired) as error:
            raw = error.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            verification = verify_result(raw, error)
            metadata.update(termination_facts(error))
            metadata["stop_requested" if isinstance(error, WorkerStopRequested) else "timed_out"] = True
            metadata["worker_started"] = True
            return refused(noshell.VIOLATED if verification.violated else
                           "stop_requested" if isinstance(error, WorkerStopRequested) else "timed_out")
        except (UnsafeExecutable, UnsafeBatchArgument) as error:
            return self._launch_refusal(error)
        except Exception as error:
            # Never echo exception text, stdin, paths, source or raw CLI output.
            verification = verify_result("", error)
            return refused(noshell.VIOLATED if verification.violated else noshell.UNAVAILABLE)

        verification = verify_result(completed.stdout, completed)
        if not verification.verified:
            return refused(noshell.VIOLATED if verification.violated else noshell.UNAVAILABLE,
                           code=completed.returncode)
        parsed = parse_codex_stream(completed.stdout)
        if not parsed.usable:
            return refused(code=completed.returncode)
        # No stderr-body or plain-text fallback can bypass event verification.
        status, reason, retry = classify_worker_result(
            returncode=completed.returncode, combined_output=parsed.assistant_text,
            primary_output=parsed.assistant_text,
        )
        success = completed.returncode == 0
        metadata.update({"evidence_mode": "structured", "worker_metrics": parsed.metrics.to_dict(),
                         "stderr_bytes": len(completed.stderr or ""),
                         "stderr_category": classify_stderr(completed.stderr or "")})
        from ..project_notes import closed_reason
        return noshell.bind_host_diagnostics(NoShellResult(
            provider=self.name, success=success, output=parsed.assistant_text,
            exit_code=completed.returncode,
            error=result_error(completed.stderr, success=success, structured=True),
            # Nonzero-exit reasons are generated from the observed process
            # code, not envelope prose. Keep that evidence for the assessor.
            metadata=metadata, task_status=status,
            task_status_reason=reason if completed.returncode != 0 else closed_reason(reason),
            safe_to_retry=retry, limitations=noshell.limitations(metadata, worker_limitations(parsed.assistant_text)),
        ), diagnostics)
