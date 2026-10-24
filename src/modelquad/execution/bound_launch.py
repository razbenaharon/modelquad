# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Shared launch binding and evidence writing, independent of MCP registration.

The detached runner imports this module without importing a server or its SDK.
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

from .. import netpolicy
from .base import attempt_resolution, describe_worker_termination, workspace_stability_note
from ..hostobs import payload_size_bucket, with_decision_context
from ..provenance import evidence_snapshot_json
from ..router import Router
from ..routing_policy import LaunchRefused
from ..service import history as service_history

_launch_call = ContextVar("launch_call", default=None)


def _bind_launch(manager, attempt_id, invocation_mode, user_approved=False, caller_host=None, *, decision_id=None,
                 capability='delegate', independence_excluded=()):
    manager.capability = capability
    manager.independence_excluded = frozenset(independence_excluded)
    manager.launch_attempt_id = attempt_id
    manager.launch_invocation_mode = invocation_mode
    manager.launch_user_approved = user_approved
    manager.launch_caller_host = caller_host
    manager.launch_refusal = None
    manager.launch_result = None
    manager.launch_host_diagnostics = ()
    manager.launch_decision_id = decision_id
    call = _launch_call.get()
    if call is not None:
        call['capability'] = capability
        manager.launch_decision_id = decision_id or call.get("decision_id")
        manager.on_launch_entered = lambda: call.update(worker_entered=True)


def run_bound_delegation(
    manager, *, config, task_type, effective_prompt, working_directory,
    provider, agent_runtime, statuses, timeout, session_id, model,
    reasoning_effort, consumption_evidence, verification_spec, parsed_capsule,
    db_path, attempt_id, usage_directory, lineage, resolved_mode, mode_source,
    host, decision_id, host_snapshot, mode, paused, decision_mode,
    reasoning_fell_back=False, plan_decision_id=None, metadata=None,
    publish=None, on_resolution=None, on_worker_spawn=None, on_worker_packed=None,
    required_capabilities=None, action_authority=None,
    normalize_result=None, on_host_diagnostics=None,
    now=None, catalog=None, policy_excluded=None,
):
    """Same bound deterministic execution and evidence writers for both paths.

    The runner publishes the normalized answer before the one conditional
    attempt resolution. In-process callers supply no publication callback.
    """
    def evidence_for_target(target):
        return _with_decision_now(evidence_snapshot_json(
            statuses, config, routing_mode=mode, routing_paused=paused,
            expected_consumption=consumption_evidence,
            host_snapshot=with_decision_context(host_snapshot, selected_runtime=target),
        ), now)

    def spawned(pid):
        service_history.record_worker_pid(db_path, attempt_id, pid)
        if on_worker_spawn is not None:
            on_worker_spawn(pid)

    if required_capabilities is None and parsed_capsule is not None:
        required_capabilities = parsed_capsule.capability_requirements
    if action_authority is None and parsed_capsule is not None:
        action_authority = parsed_capsule.action_authority
    from .. import project_notes
    private_notes = project_notes.has_packet(effective_prompt)
    manager.private_notes_execution = private_notes
    if action_authority is None:
        project_notes.require_read_only(task_type)
    started_at = time.monotonic()
    execution_result = manager.delegate(
        task_type,
        effective_prompt,
        working_directory,
        provider=provider,
        agent_runtime=agent_runtime,
        statuses=statuses,
        timeout=timeout,
        session_id=session_id,
        model=model,
        reasoning_effort=reasoning_effort,
        now=now, catalog=catalog, policy_excluded=policy_excluded,
        **({"metadata": metadata} if metadata is not None else {}),
        expected_consumption=consumption_evidence,
        verification=verification_spec,
        # What this particular request needs a worker to be able to DO, as
        # declared by the caller's capsule. The task type only sets a floor;
        # this is how "this debugging task has to run the failing test"
        # deterministically excludes a worker that cannot run commands,
        # instead of routing to it and coming back blocked.
        required_capabilities=required_capabilities,
        **({"action_authority": action_authority} if action_authority is not None else {}),
        # Record the worker's pid on the attempt row the moment it spawns:
        # if this caller later times out, which process owns the work stays
        # a checkable fact instead of a guess.
        on_spawn=spawned,
        on_packed=on_worker_packed,
        stop_requested=lambda: service_history.attempt_stop_requested(db_path, attempt_id),
        # The attempt row can record when objective verification starts and
        # finishes, but no verification runs: outcome.py never
        # invokes this callback, so no attempt is stamped as verifying.
        on_verification=lambda phase: (
            service_history.record_verification_started(db_path, attempt_id)
            if phase == "started"
            else service_history.record_verification_resolved(db_path, attempt_id)
        ),
    )
    duration_seconds = time.monotonic() - started_at
    if normalize_result is not None:
        execution_result = normalize_result(execution_result)
    if private_notes:
        execution_result = project_notes.private_result(execution_result,
            model=getattr(manager, "admitted_model", None) or model,
            reasoning_effort=getattr(manager, "admitted_reasoning_effort", None) or reasoning_effort, catalog=catalog)

    # Record MCP usage, routing and execution so activity is auditable.
    service_history.record_provider_usage(db_path, statuses)
    # Record the decision the manager actually routed, even for a terminal
    # routing refusal. Explicit provider/runtime calls have no routed result;
    # only those calls need an advisory result from the same frozen inputs.
    auto_result = manager.routed_result
    explicit_choice = provider is not None or agent_runtime is not None
    if auto_result is None and explicit_choice:
        if policy_excluded is None:  # Legacy specs retain the process policy read.
            policy_excluded = netpolicy.providers_refused_by_policy()
        auto_result = Router(config).route(
            task_type, statuses, expected_consumption=consumption_evidence,
            required_capabilities=required_capabilities,
            now=now, catalog=catalog, policy_excluded=policy_excluded,
            **({'independence_excluded': manager.independence_excluded} if manager.independence_excluded else {}),
        )
    if lineage.prior_route or auto_result is None:
        # This delegation EXECUTES a decision that was already recorded by
        # modelquad_route. Writing another routing row here would duplicate
        # the decision and overwrite what the router actually recommended at
        # decision time. The execution row below carries the same
        # decision_id, so the join is preserved without rewriting history.
        # A safety refusal before routing also has no routing fact to write.
        pass
    elif explicit_choice and (manager.capability != 'second_opinion' or lineage.requested_provider is not None):
        service_history.record_routing_decision(
            db_path,
            task_type,
            auto_result,
            source="mcp",
            overridden=True,
            override_reason="explicit provider argument" if provider is not None else "explicit runtime argument",
            selected_provider=(
                _executed_provider_for_row(execution_result)
                if getattr(manager, "launch_entered", False)
                else service_history.NOTHING_SELECTED
            ),
            invocation_mode=resolved_mode,
            invocation_mode_source=mode_source,
            host=host,
            decision_source="user_override",
            decision_id=decision_id,
            evidence_json=evidence_for_target(provider or agent_runtime),
        )
    else:
        service_history.record_routing_decision(
            db_path,
            task_type,
            auto_result,
            source="mcp",
            # WHAT THE DELEGATION COULD ACTUALLY CARRY OUT. `recommendation`
            # stays exactly as the router computed it: it is the advisory
            # route, and for `exploration` it is legitimately a host-only
            # runtime ("keep this here"). Rewriting it would make this row
            # disagree with the one `modelquad_route` writes for the same
            # question. `selected_provider` answers a DIFFERENT question --
            # which provider this delegation went to -- and left unset it
            # silently defaults to the recommendation, so an auto-routed
            # delegation recorded `claude` as selected while Codex ran. That
            # names a provider with no delegated executor as the one that was
            # selected, and every reader of a routing row (`lineage.py`,
            # `presentation.py`, `cli.py`) reads
            # `selected_provider or recommendation` as "what ran". The
            # "unknown" sentinel is the ABSENCE of a provider
            # and is never written as if it were one: a delegation that
            # reached no executor records that NOTHING was selected, not the
            # host-only recommendation.
            selected_provider=_executed_provider_for_row(execution_result),
            invocation_mode=resolved_mode,
            invocation_mode_source=mode_source,
            host=host,
            # Saved planner-mode inputs never change this single-unit execution.
            decision_source="deterministic",
            decision_id=decision_id,
            evidence_json=evidence_for_target(auto_result.recommendation),
            plan_decision_id=None,
        )
    # Record what the decision recommended vs what actually executed. Both
    # are preserved: a fallback or a caller override is a fact about the
    # lifecycle, not a discrepancy to reconcile away.
    if (not lineage.prior_route and lineage.recommended_provider is None
            and manager.routed_result is not None):
        target = manager.routed_result.target
        lineage = replace(lineage, recommended_provider=manager.routed_result.recommendation,
                          recommended_model=target.model if target else None,
                          recommended_runtime=target.agent_runtime if target else None)
    terminal = execution_result.terminal_resolution
    execution_result = replace(
        execution_result,
        metadata={
            **execution_result.metadata,
            "decision_lineage": lineage.resolved(
                execution_result.provider,
                execution_result.metadata.get("agent_runtime"),
            ),
            # The handoff, recorded where it actually happened. The
            # decision's frozen snapshot says whether a handoff was
            # EXPECTED; this says what the handoff to the worker that ran
            # actually was, which is the pairing the direction analysis
            # needs ("Claude -> Codex" is not "Codex -> Claude"). Only the
            # payload's size is kept -- the prompt itself never lands here.
            "host_handoff": _host_handoff_record(
                host_snapshot, execution_result.provider, effective_prompt
            ),
        },
    )
    if private_notes:
        object.__setattr__(execution_result, '_terminal_resolution', terminal)
    service_history.record_execution(
        db_path,
        task_type,
        execution_result,
        duration_seconds=duration_seconds,
        source="mcp",
        project=None if manager.capability == 'second_opinion' else Path(working_directory).name,
        invocation_mode=resolved_mode,
        invocation_mode_source=mode_source,
        host=host,
        decision_id=decision_id,
    )

    # The delegation returned: close the attempt with the objective outcome.
    # This is the ONLY writer of the row's status/resolved_at/failure_kind.
    # Whether the caller was still waiting when it landed is recorded
    # separately, beside this, and never instead of it.
    attempt_status, attempt_failure_kind = attempt_resolution(execution_result)
    attempt_detail = execution_result.task_status_reason
    fate = describe_worker_termination(execution_result.metadata)
    if fate:
        # What became of the worker is on the row next to why it was stopped:
        # terminated and confirmed, or unaccounted.
        attempt_detail = str(attempt_detail or "timed_out") + "; " + fate
    if publish is not None:
        publish(execution_result)
    resolved = service_history.resolve_delegation_attempt(
        db_path,
        attempt_id,
        status=attempt_status,
        routed_provider=execution_result.provider,
        outcome=execution_result.task_status.value,
        failure_kind=attempt_failure_kind,
        detail=attempt_detail,
        duration_seconds=duration_seconds,
        # What the worker left running when it exited, for the workspace
        # question to read later.
        worker_descendants=execution_result.metadata.get("worker_descendants"),
        **({"only_if_open": True} if publish is not None else {}),
    )
    if on_resolution is not None:
        on_resolution(resolved, execution_result)

    payload = execution_result.to_dict()
    payload["decision_id"] = decision_id
    # ModelQuad stopped waiting and tried to stop the worker. Whether it
    # SUCCEEDED is the difference between a workspace that has settled and one
    # still being written to, and the caller is the party about to test, review
    # or commit that tree -- so the difference is stated HERE, in the result it
    # actually reads, and not only on the attempt row nobody thought to look up.
    # Empty for anything that neither timed out nor was stopped.
    payload.update(workspace_stability_note(execution_result.metadata, working_directory))
    # History, publication and attempt closure all use the closed
    # result above. File-specific guidance exists only in this Host response.
    diagnostics = host_diagnostic_payload(getattr(manager, 'launch_host_diagnostics', ()))
    if diagnostics:
        payload['host_diagnostics'] = diagnostics
    if on_host_diagnostics is not None:
        try:
            on_host_diagnostics(getattr(manager, 'launch_host_diagnostics', ()))
        except Exception:
            pass  # Transient delivery cannot change the closed worker outcome.
    from ..execution.base import POLICY_REFUSAL_REASONS

    if execution_result.task_status_reason in POLICY_REFUSAL_REASONS:
        # A ModelQuad policy gate terminally refused and no worker was started
        # -- the router, or `netpolicy`.
        # Say so in the SAME shape the duplicate
        # guard already uses, so a caller has one way to recognise "we
        # deliberately did not run this" rather than having to read a
        # task-status enum.
        payload["delegated"] = False
        payload["refused"] = True
        payload["reason"] = execution_result.error
    return payload


def host_diagnostic_payload(diagnostics):
    """Accept C's bounded transient type, never arbitrary executor metadata."""
    from .noshell import OmissionDiagnostic, MAX_SKIPPED
    if type(diagnostics) not in (tuple, list):
        return []
    return [item.to_host_dict() for item in diagnostics[:MAX_SKIPPED + 1]
            if type(item) is OmissionDiagnostic]



def _executed_provider_for_row(execution_result) -> str | service_history.NothingSelected:
    """The provider an execution result may record as a routing row's selection.

    The manager returns the "unknown" sentinel when it reached no viable
    worker; that is the ABSENCE of a provider and is never written as if it
    were one. Nor is None passed for it: that would let
    `record_routing_decision` substitute the recommendation, recording a
    selection for a delegation that reached no executor. The row records
    that nothing was selected instead.
    """

    provider = execution_result.provider
    if execution_result.task_status_reason == "action_authority_unmapped":
        return service_history.NOTHING_SELECTED
    return provider if provider != "unknown" else service_history.NOTHING_SELECTED


def _host_handoff_record(host_snapshot, actual_provider: str | None, prompt: str | None) -> dict:
    """What the handoff to the worker that actually ran cost, in safe terms.

    ``handoff_required`` is None -- not False -- when the host could not be
    identified: "the task did not move" and "we do not know whether it moved"
    are different research facts.
    """

    from ..hostobs import normalize_runtime_id

    origin = host_snapshot.identity.runtime if host_snapshot is not None else None
    required = None
    if origin and actual_provider:
        required = normalize_runtime_id(actual_provider) != normalize_runtime_id(origin)
    return {
        "origin_runtime": origin,
        "target_runtime": actual_provider,
        "handoff_required": required,
        "payload_bucket": payload_size_bucket(len(prompt or "")),
    }


def _with_decision_now(evidence_json, now):
    """Only the decision clock enters history; the catalog stays private."""
    if now is None:  # Legacy specs did not capture a decision clock.
        return evidence_json
    evidence = json.loads(evidence_json)
    evidence['decision_now'] = now.isoformat()
    return json.dumps(evidence)
