# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""One detached runner per job. Its worker Job Object dies with this process.

Kill-on-close covers Job Object members only, not Windows alias targets started
outside it. Existing outside-job evidence remains separate. No crash recovery or
native Host support is claimed here. Delegations use the existing manager and
normalized answer; raw argv is retained for synthetic fixtures only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time

from modelquad import paths, procutil
from modelquad.execution.base import (
    ExecutionResult, WorkerTaskStatus, attempt_resolution, bound_descendant_facts,
    describe_worker_termination, parse_completion_envelope, worker_confirmed_dead,
)
from modelquad.execution.lifecycle import TerminalResolution
from modelquad.jobs.handle import (
    JobSpec, STARTUP_SECONDS, _log_runner_event, runner_containment_verdict,
    CONTAINED_RUNNER_LIMITATION,
)
from modelquad.jobs.changes import changed_files, snapshot
from modelquad.jobs.seam import (
    JobOutput, JobRef, JobStatus, OutputSink, cause_flags_from_result, job_status_from_resolution,
)
from modelquad.service import history

# Provisional, to be measured later. No default timeout is chosen.
HEARTBEAT_SECONDS = 0.5


class _NoOutputSink:
    def put(self, output: JobOutput, now: datetime) -> bool:
        return False

    def collect(self, job_id, now):
        return None

    def acknowledge(self, job_id, receipt, now):
        return False

    def extend(self, job_id, until, now):
        return None

    def delete(self, job_id, now):
        return False

    def pending(self, now):
        return ()

    def purge_expired(self, now):
        return ()

    def state(self, job_id, now):
        return None

    def outcomes(self, now, *, since):
        return ()


def _synthetic_jobs_allowed() -> bool:
    """Synthetic fixtures (test sinks, raw argv) run only from a source
    checkout or under the explicit test opt-in, never in an installed package."""
    return paths.is_dev_checkout() or os.environ.get('MODELQUAD_TEST_SINK_OK') == '1'


def output_sink_factory(name: str) -> OutputSink:
    """Real store plus the guarded, installed-mode synthetic file sink."""
    if name == 'store':
        from modelquad.jobs.store import default_store
        return default_store()
    if name == 'none':
        return _NoOutputSink()
    if name.startswith('file:'):
        if not _synthetic_jobs_allowed():
            raise ValueError('test output sink unavailable')
        from modelquad.jobs._test_sink import FileSink
        return FileSink(Path(name[5:]))
    raise ValueError('unknown output sink factory')


def _text(value) -> str:
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value or ''


def _execute(spec: JobSpec, ref: JobRef) -> ExecutionResult:
    history.require_unreleased_job(spec.db_path, ref.job_id)
    provider = spec.provider or 'unknown'
    if history.attempt_stop_requested(spec.db_path, ref.attempt_id):
        return ExecutionResult(provider, False, '', None,
                               metadata={'worker_started': False, 'stop_requested': True},
                               task_status_reason='stop_requested')

    before = snapshot(spec.cwd)

    def observed(result):
        # Private transient handoff, never metadata/asdict/history.
        object.__setattr__(result, '_job_changed_files', changed_files(before, spec.cwd))
        return result

    def spawned(pid):
        history.record_worker_pid(spec.db_path, ref.attempt_id, pid)
        history.update_job(spec.db_path, ref.job_id, worker_pid=pid, worker_created=procutil.process_creation_epoch(pid), status='running')

    try:
        completed = procutil.run_with_tree_kill(
            spec.argv, cwd=spec.cwd, env={**os.environ, **spec.env, 'MODELQUAD_WORKER': '1'}, timeout=spec.timeout,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            on_spawn=spawned, kill_on_close=True,
            stop_requested=lambda: history.attempt_stop_requested(spec.db_path, ref.attempt_id),
        )
        output = _text(completed.stdout)
        envelope = parse_completion_envelope(output)
        metadata = {'worker_started': True, 'timed_out': False, 'stop_requested': False}
        for key in ('worker_termination', 'worker_descendants'):
            facts = getattr(completed, key, None)
            if facts is not None:
                metadata[key] = bound_descendant_facts(facts) if key == 'worker_descendants' else facts
        return observed(ExecutionResult(
            provider, completed.returncode == 0, output, completed.returncode, metadata=metadata,
            task_status=WorkerTaskStatus(envelope['task_status']) if envelope else WorkerTaskStatus.UNKNOWN,
            task_status_reason=envelope.get('reason') if envelope and isinstance(envelope.get('reason'), str) else None,
        ))
    except (procutil.WorkerStopRequested, subprocess.TimeoutExpired) as error:
        stopped = isinstance(error, procutil.WorkerStopRequested)
        return observed(ExecutionResult(
            provider, False, _text(error.stdout), None,
            metadata={'worker_started': True, 'timed_out': not stopped, 'stop_requested': stopped,
                      'worker_termination': getattr(error, 'worker_termination', {})},
            task_status_reason='stop_requested' if stopped else None,
        ))
    except Exception:
        return observed(ExecutionResult(provider, False, '', None, task_status_reason='launch_failed'))


logger = logging.getLogger(__name__)


def _finalization_unavailable_for_job(db_path, job_id):
    try:
        current = history.read_job(db_path, job_id)
        if current and current['ended_at'] is None:
            # update_job also enforces ended_at IS NULL atomically.
            history.update_job(db_path, job_id, status='unknown')
    except Exception:
        pass  # recovery remains uncertain if even the best-effort write fails
    logger.warning('job finalization unavailable; recovery required')


def _finalization_unavailable(spec, ref):
    _finalization_unavailable_for_job(spec.db_path, ref.job_id)


def _output(result, ref, started, resolution=None, *, observed_changes=None):
    ended = datetime.now(timezone.utc)
    stop, deadline = cause_flags_from_result(result)
    status = job_status_from_resolution(resolution or result.terminal_resolution,
                                        stop_requested=stop, deadline_passed=deadline)
    return JobOutput(
        ref, status, WorkerTaskStatus.UNKNOWN if status is JobStatus.INTERRUPTED else result.task_status,
        'interrupted' if status is JobStatus.INTERRUPTED else result.task_status_reason, result.output,
        bool(result.output or observed_changes) and (
            stop or deadline or status is JobStatus.INTERRUPTED or result.task_status is WorkerTaskStatus.PARTIAL),
        observed_changes, result.metadata.get('worker_termination'), result.metadata.get('worker_descendants'),
        started, ended, worker_confirmed_dead(result.metadata) if 'worker_termination' in result.metadata else None,
    )


def _finish_job(spec, ref, resolved, result, ended, resolution=None):
    status = _output(result, ref, ended, resolution).job_status.value
    if resolved is not True:
        try:
            existing = history.read_job_attempt(spec.db_path, ref.attempt_id)
        except Exception:
            existing = None
        if existing and existing['resolved_at'] is not None and existing['status'] != 'started':
            detail = existing.get('detail') or ''
            try:
                status = job_status_from_resolution(
                    TerminalResolution(existing['status'], existing['failure_kind']),
                    stop_requested='stop_requested' in detail,
                    deadline_passed=existing['failure_kind'] in {'timeout', 'terminated'}
                    and 'stop_requested' not in detail,
                ).value
            except ValueError:
                status = 'unknown'
            logger.warning('job attempt already terminal; published output retained')
        else:
            status = 'unknown'
            logger.warning('job attempt resolution unavailable; recovery required')
    current = history.read_job(spec.db_path, ref.job_id)
    # A startup-expiry sweep may already have ended this job. Its terminal
    # record wins; the separately published answer remains collectible.
    if current and current['ended_at'] is None:
        fields = {'status': status}
        if status != 'unknown':
            fields['ended_at'] = ended.isoformat()
        history.update_job(spec.db_path, ref.job_id, **fields)


def _run_delegation(spec, ref, started, state, *, inline=False):
    history.require_unreleased_job(spec.db_path, ref.job_id)
    from modelquad.action_authority import TaskAuthority
    from modelquad.config import load_config
    from modelquad.execution.manager import ExecutionManager
    from modelquad.hostobs import HostDecisionSnapshot
    from modelquad.capsule import TaskCapsule
    from modelquad.lineage import DecisionLineage
    from modelquad.models import ExpectedConsumption
    from modelquad.outcome import VerificationSpec
    from modelquad.execution.bound_launch import _bind_launch, run_bound_delegation
    from modelquad.routing_policy import LaunchRefused

    prepared = spec.delegation
    from modelquad import project_notes
    private_notes = project_notes.has_packet(prepared.prompt)
    sink = output_sink_factory(spec.sink)
    published = []
    publication_attempted = False
    before = None
    is_review = False
    normalize = None
    manager = None
    catalog = None

    def publish(result, resolution=None):
        nonlocal publication_attempted
        history.require_unreleased_job(spec.db_path, ref.job_id)
        publication_attempted = True
        state['result'] = project_notes.private_result(result,
                           model=getattr(manager, 'admitted_model', None) or prepared.model,
                           reasoning_effort=getattr(manager, 'admitted_reasoning_effort', None)
                            or prepared.reasoning_effort, catalog=catalog) if private_notes else result
        output = _output(result, ref, started, resolution,
                         observed_changes=None if is_review else changed_files(before, prepared.working_directory))
        with history.job_publication_fence(spec.db_path, ref.job_id):
            sink.put(output, output.ended_at)
        published.append(output)
        if is_review:
            state['published'] = True

    def resolved(written, result):
        state['resolution_attempted'] = True
        _finish_job(spec, ref, written, result, published[-1].ended_at)

    def host_diagnostics(items):
        if inline:
            from modelquad.jobs.diagnostics import INLINE, inline_key
            INLINE.put(inline_key(spec.db_path, ref.job_id), items)
        else:
            from modelquad.jobs.service_client import transient_diagnostics
            transient_diagnostics(spec.db_path, ref.job_id, ref=ref, diagnostics=items)

    token = procutil.default_kill_on_close.set(True)
    try:
        config = load_config(prepared.config_path)
        # Legacy specs (including second opinion) carry no captured clock.
        # They retain the old routing defaults and process policy read. New
        # specs rebuild private inputs, without any clock/cache/policy probe.
        decision_inputs = (dict(now=None, catalog=None, policy_excluded=None)
                           if prepared.decision_now is None else
                           dict(now=prepared.rebuild_decision_now(), catalog=prepared.rebuild_catalog(),
                                policy_excluded=prepared.policy_excluded))
        catalog = decision_inputs['catalog']
        # No DEFAULT_DB_PATH or ContextVar decision identity is consulted.
        manager = ExecutionManager(config, health_db_path=prepared.db_path)
        _bind_launch(manager, prepared.attempt_id, prepared.invocation_mode,
                     prepared.user_approved, prepared.caller_host, decision_id=prepared.decision_id,
                     capability=prepared.capability,
                     independence_excluded=())
        manager.job_owned_attempt = True
        lineage = dict(prepared.lineage)
        lineage['notes'] = tuple(lineage['notes'])
        before = snapshot(prepared.working_directory)
        run_bound_delegation(
            manager, config=config, task_type=prepared.task_type, effective_prompt=prepared.prompt,
            working_directory=prepared.working_directory, provider=prepared.provider,
            agent_runtime=prepared.agent_runtime, statuses=prepared.rebuild_statuses(),
            timeout=prepared.timeout, session_id=prepared.session_id, model=prepared.model,
            reasoning_effort=prepared.reasoning_effort, metadata=prepared.metadata,
            required_capabilities=prepared.required_capabilities,
            action_authority=TaskAuthority.from_dict(prepared.action_authority) if prepared.action_authority else None,
            consumption_evidence=ExpectedConsumption.from_dict(prepared.consumption),
            verification_spec=VerificationSpec(**prepared.verification) if prepared.verification else None,
            parsed_capsule=TaskCapsule.from_dict(prepared.capsule) if prepared.capsule else None,
            db_path=prepared.db_path, attempt_id=prepared.attempt_id, usage_directory=prepared.usage_directory,
            lineage=DecisionLineage(**lineage), resolved_mode=prepared.invocation_mode,
            mode_source=prepared.mode_source, host=prepared.host, decision_id=prepared.decision_id,
            host_snapshot=HostDecisionSnapshot.from_dict(prepared.host_snapshot),
            mode=prepared.mode, paused=prepared.paused, decision_mode="deterministic",
            reasoning_fell_back=False, plan_decision_id=None,
            **decision_inputs,
            publish=publish, on_resolution=resolved, on_host_diagnostics=host_diagnostics,
            on_worker_spawn=lambda pid: history.update_job(prepared.db_path, ref.job_id,
                                                          worker_pid=pid, worker_created=procutil.process_creation_epoch(pid), status='running'),
        )
        return 0
    except Exception as error:
        if publication_attempted:
            _log_runner_event(ref.job_id, 'runner_error' if private_notes else type(error).__name__)
            # Publication/resolution storage failure is not a new worker
            # outcome. Preserve the published answer and leave recovery open.
            _finalization_unavailable(spec, ref)
            return 1
        refused = isinstance(error, LaunchRefused)
        if not refused:
            raise  # The outer handler owns crash classification and fate.
        result = ExecutionResult(
            prepared.provider or 'unknown', False, '', None,
            task_status=WorkerTaskStatus.BLOCKED, task_status_reason=(
                project_notes.closed_reason(str(error), capability=prepared.capability) if private_notes else str(error)),
        )
        if normalize is not None:
            result = normalize(result)
        if private_notes:
            result = project_notes.private_result(result, model=prepared.model,
                                                  reasoning_effort=prepared.reasoning_effort, catalog=catalog)
        resolution = TerminalResolution('refused', 'policy_refused')
        try:
            publish(result, resolution)  # failed publication leaves the attempt open
            state['resolution_attempted'] = True
            written = history.resolve_delegation_attempt(
                spec.db_path, ref.attempt_id, status=resolution.status,
                failure_kind=resolution.failure_kind, outcome=result.task_status.value,
                detail=result.task_status_reason, only_if_open=True,
                roots=(prepared.working_directory,) if prepared.working_directory else (),
            )
            _finish_job(spec, ref, written, result, published[-1].ended_at, resolution)
        except Exception:
            _finalization_unavailable(spec, ref)
            return 1
        return 0
    finally:
        procutil.default_kill_on_close.reset(token)


def close_inline_crash(spec, ref):
    """Conditional resolution on thread loss; a live process is not a live job.

    With worker evidence the terminal job remains unknown about worker fate.
    With no worker evidence it fails, permitting the reservation replacement rule.
    """
    try:
        row = history.read_job(spec.db_path, ref.job_id)
        attempt = history.read_job_attempt(spec.db_path, ref.attempt_id)
        worker = bool((row or {}).get('worker_pid') or (attempt or {}).get('worker_pid'))
        written = history.resolve_delegation_attempt(
            spec.db_path, ref.attempt_id, status='failed',
            failure_kind='exception' if worker else 'failed_to_start',
            outcome='unknown', detail='inline_crashed' if worker else 'inline_pre_start_failed',
            only_if_open=True,
        )
        if written:
            history.update_job(spec.db_path, ref.job_id, status='unknown' if worker else 'failed',
                               ended_at=datetime.now(timezone.utc).isoformat())
        else:
            _finalization_unavailable(spec, ref)
    except Exception:
        _finalization_unavailable(spec, ref)


def run_job(job_id: str, spec_path: Path, *, db_path: Path | str | None = None) -> int:
    raw = None
    row = None
    result = None
    state = {'resolution_attempted': False}
    try:
        # The starter supplies lifecycle storage independently of the spec so
        # even a missing or malformed spec can close its reserved attempt.
        if db_path is not None:
            row = history.read_job(db_path, job_id)
        try:
            raw = json.loads(spec_path.read_text(encoding='utf-8'))
        finally:
            spec_path.unlink(missing_ok=True)
        if db_path is None:
            db_path = raw['db_path']
            row = history.read_job(db_path, job_id)
        spec = JobSpec(**raw)
        if Path(spec.db_path).resolve() != Path(db_path).resolve():
            raise ValueError('job database mismatch')
        if row is None or row['attempt_id'] != spec.attempt_id or row['ended_at'] is not None:
            raise ValueError('missing job reservation')
        ref = JobRef(job_id, spec.attempt_id, row['decision_id'])
        if spec.delegation is not None and (
            spec.delegation.attempt_id != spec.attempt_id or
            spec.delegation.decision_id != ref.decision_id or
            Path(spec.delegation.db_path).resolve() != Path(db_path).resolve()
        ):
            raise ValueError('prepared delegation identity mismatch')
        history.require_launch_attempt(spec.db_path, spec.attempt_id)
        # Parent readiness never authorizes this runner. Measure here before
        # startup registration, snapshots, providers or synthetic execution.
        ok, reason = runner_containment_verdict()
        if not ok:
            raise ValueError('runner containment refused')
        if not history.update_job_startup(
            db_path, job_id, startup_seconds=STARTUP_SECONDS,
            runner_pid=os.getpid(), runner_created=procutil.process_creation_epoch(os.getpid()),
            launch_limitation=CONTAINED_RUNNER_LIMITATION if reason == CONTAINED_RUNNER_LIMITATION else None,
        ):
            raise ValueError('runner startup no longer available')
        row = history.read_job(db_path, job_id)
        started = datetime.now(timezone.utc)
        done = threading.Event()

        def heartbeat():
            while not done.is_set():
                try:
                    history.update_job(spec.db_path, job_id, heartbeat_at=datetime.now(timezone.utc).isoformat())
                except Exception:
                    pass  # failed observation never invents liveness
                done.wait(HEARTBEAT_SECONDS)

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        clock = time.monotonic()
        try:
            if spec.delegation is not None:
                return _run_delegation(spec, ref, started, state)
            # Product creators never write raw argv (they pass argv=() and a
            # prepared delegation); a raw-argv spec is a synthetic fixture.
            if not _synthetic_jobs_allowed():
                raise ValueError('raw job argv unavailable')
            result = _execute(spec, ref)
        finally:
            done.set()
            thread.join(timeout=2)
        ended = datetime.now(timezone.utc)
        resolution = TerminalResolution(*attempt_resolution(result))
        stop, deadline = cause_flags_from_result(result)
        status = job_status_from_resolution(resolution, stop_requested=stop, deadline_passed=deadline)
        output = JobOutput(
            ref, status, WorkerTaskStatus.UNKNOWN if status is JobStatus.INTERRUPTED else result.task_status,
            'interrupted' if status is JobStatus.INTERRUPTED else result.task_status_reason, result.output,
            bool(result.output or getattr(result, '_job_changed_files', None)) and (
                stop or deadline or status is JobStatus.INTERRUPTED or result.task_status is WorkerTaskStatus.PARTIAL),
            getattr(result, '_job_changed_files', None),
            result.metadata.get('worker_termination'), result.metadata.get('worker_descendants'),
            started, ended, worker_confirmed_dead(result.metadata) if 'worker_termination' in result.metadata else None,
        )
        # Publish completion only after the output handoff succeeds.
        history.require_unreleased_job(spec.db_path, ref.job_id)
        with history.job_publication_fence(spec.db_path, ref.job_id):
            output_sink_factory(spec.sink).put(output, ended)
        detail = result.task_status_reason
        fate = describe_worker_termination(result.metadata)
        if fate:
            detail = (detail + '; ' if detail else '') + fate
        state['resolution_attempted'] = True
        resolved = history.resolve_delegation_attempt(
            spec.db_path, ref.attempt_id, status=resolution.status, failure_kind=resolution.failure_kind,
            routed_provider=spec.provider, outcome=result.task_status.value, detail=detail,
            duration_seconds=time.monotonic() - clock, only_if_open=True,
            worker_descendants=result.metadata.get('worker_descendants'),
        )
        _finish_job(spec, ref, resolved, result, ended, resolution)
        return 0 if resolved is True else 1
    except BaseException as error:
        ended = datetime.now(timezone.utc).isoformat()
        # Record only type/identity/time, never exception text or task content.
        _log_runner_event(job_id, 'runner_error' if isinstance(raw, dict) and raw.get('delegation') is not None else type(error).__name__)
        result = result or state.get('result')
        if db_path is not None and not state['resolution_attempted']:
            # A read failure must not skip conditional resolution. Use the
            # reservation already read, never a mismatching product spec.
            current = row
            try:
                current = history.read_job(db_path, job_id) or row
            except Exception:
                pass
            attempt_id = (current or {}).get('attempt_id')
            if attempt_id is None and isinstance(raw, dict) and raw.get('delegation') is None:
                attempt_id = raw.get('attempt_id')
            worker_started = bool((current or {}).get('worker_pid')) or bool(
                result is not None and result.metadata.get('worker_started'))
            if not worker_started and attempt_id is not None:
                try:
                    attempt = history.read_job_attempt(db_path, attempt_id)
                    worker_started = bool(attempt and attempt.get('worker_pid'))
                except Exception:
                    pass
            fate = describe_worker_termination(result.metadata) if result is not None else ''
            detail = ('runner_crashed; ' + (fate or 'worker fate unknown') if worker_started
                      else 'runner_pre_start_failed')
            written = False
            try:
                if attempt_id is not None:
                    written = history.resolve_delegation_attempt(
                        db_path, attempt_id, status='failed',
                        failure_kind='exception' if worker_started else 'failed_to_start',
                        outcome=WorkerTaskStatus.UNKNOWN.value, detail=detail, only_if_open=True,
                        worker_descendants=result.metadata.get('worker_descendants') if result is not None else None,
                    )
            except Exception:
                pass  # Storage may itself be the cause of the crash.
            try:
                if written and not worker_started:
                    history.update_job(db_path, job_id, status='failed', ended_at=ended)
                else:
                    # Schema 21 admits uncertainty after a worker started or
                    # storage failed. Never contradict an already resolved attempt.
                    _finalization_unavailable_for_job(db_path, job_id)
            except Exception:
                pass
        elif db_path is not None:
            _finalization_unavailable_for_job(db_path, job_id)
        # A resolved attempt owns its result. Job recovery reconciles a failed final
        # job write; never overwrite that job with a contradictory failure.
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return 1



def add_runner_arguments(parser: argparse.ArgumentParser) -> None:
    """The runner's arguments, shared by ``-m`` and the CLI's ``job-runner``."""
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--db-path', type=Path)


def main(argv: list[str] | None = None) -> int:
    from modelquad import netpolicy
    netpolicy.install_loopback_guard()
    parser = argparse.ArgumentParser()
    add_runner_arguments(parser)
    args = parser.parse_args(argv)
    return run_job(args.job_id, args.spec, db_path=args.db_path)


if __name__ == '__main__':
    raise SystemExit(main())
