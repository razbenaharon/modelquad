# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Routing-aware execution manager."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import netpolicy
from ..action_authority import TaskAuthority
from ..config import Config, ProviderConfig
from ..catalog import ModelCatalog
from ..models import NETWORK_POLICY_REFUSED, ExpectedConsumption, ProviderStatus, RoutingResult, TaskType
from ..registry import ProviderPluginContext, ProviderRegistry, default_registry
from ..router import Router
from ..routing_policy import LaunchRefused, check_delegation_policy
from .base import (
    DEFAULT_TIMEOUT_SECONDS,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    WorkerTaskStatus,
    quota_refusal_evidence,
    foundation_authority_refusal,
    attempt_resolution,
    describe_worker_termination,
)

#: The worker a delegation runs on when the caller names none (see
#: ``ExecutionManager.default_worker``). Never chosen by quota.
DEFAULT_WORKER = "codex"


class _AdmittedExecutor(Executor):
    """Guard both normal execution and brokers using the executor directly."""

    def __init__(self, executor: Executor, manager: ExecutionManager) -> None:
        self._executor = executor
        self._manager = manager
        self.name = getattr(executor, "name", "unknown")

    def is_available(self) -> bool:
        return self._executor.is_available()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self._manager.require_launch_admission()
        from ..service import history

        manager = self._manager
        from .. import project_notes
        private = manager.private_notes_execution or project_notes.private_persistence_required(
            request.task_type, request.prompt,
        )
        # A planner or later plan step is a different worker. Reuse the
        # existing attempt lifecycle rather than replacing the main pid.
        separate_attempt = request.on_spawn is None or manager.main_worker_entered
        attempt_id = manager.launch_attempt_id
        if separate_attempt:
            if manager.capability == 'second_opinion':
                raise LaunchRefused('review_linkage_invalid')
            role = "planning_worker" if request.on_spawn is None else "plan_step_worker"

            def role_detail(detail: str | None) -> str:
                return f"{role}: {detail}" if detail else role

            try:
                attempt_id = history.record_delegation_attempt(
                    manager.health_db_path, task_type=request.task_type,
                    decision_id=manager.launch_decision_id,
                    requested_provider=request.provider,
                    invocation_mode=manager.launch_invocation_mode,
                    detail=role,
                    working_directory=request.working_directory,
                    request_key="worker:" + history.request_fingerprint(
                        request.task_type, request.working_directory,
                        (f"private_auxiliary:{manager.launch_attempt_id}:{role}" if private else request.prompt),
                    ),
                )
            except Exception:
                attempt_id = None
            if attempt_id is None:
                manager.launch_refusal = "attempt_unrecorded"
                raise LaunchRefused(manager.launch_refusal)
            try:
                manager.require_launch_admission()
                history.require_launch_attempt(manager.health_db_path, attempt_id)
            except (LaunchRefused, history.LaunchStateUnavailable) as exc:
                manager.launch_refusal = project_notes.closed_reason(str(exc), capability=manager.capability) if private else str(exc)
                history.resolve_delegation_attempt(
                    manager.health_db_path, attempt_id, status="refused",
                    failure_kind="policy_refused", detail=role_detail(manager.launch_refusal),
                )
                raise LaunchRefused(manager.launch_refusal) from None
            request = replace(request, on_spawn=lambda pid: history.record_worker_pid(
                manager.health_db_path, attempt_id, pid,
            ), stop_requested=lambda: history.attempt_stop_requested(
                manager.health_db_path, attempt_id,
            ) or history.attempt_stop_requested(
                manager.health_db_path, manager.launch_attempt_id,
            ))
        # The outer binding is authoritative even when the main request has
        # no callback. Auxiliary requests additionally check their own flag.
        if history.attempt_stop_requested(manager.health_db_path, manager.launch_attempt_id) or (
            request.stop_requested is not None and request.stop_requested()
        ):
            result = ExecutionResult(
                provider=request.provider, success=False, output="", exit_code=None,
                task_status=WorkerTaskStatus.FAILED, task_status_reason="stop_requested",
                metadata={"worker_started": False},
            )
            status, failure_kind = attempt_resolution(result)
            if manager.health_db_path is not None and (separate_attempt or not manager.job_owned_attempt):
                history.resolve_delegation_attempt(
                    manager.health_db_path, attempt_id, status=status,
                    failure_kind=failure_kind, outcome=result.task_status.value,
                    detail=role_detail(result.task_status_reason) if separate_attempt else result.task_status_reason,
                )
            manager.launch_result = result
            return result
        # Last admission seam shared by in-process and service-runner workers.
        # Do not mark executor entry until the current repository grant and
        # the version written on this worker's attempt have been checked.
        try:
            project_notes.require_read_only(request.task_type)
            if manager.before_worker_entry is not None:
                request = manager.before_worker_entry(request) or request
        except LaunchRefused as exc:
            manager.launch_refusal = project_notes.closed_reason(str(exc), capability=manager.capability) if private else str(exc)
            if separate_attempt:
                history.resolve_delegation_attempt(
                    manager.health_db_path, attempt_id, status="refused",
                    failure_kind="policy_refused", detail=role_detail(manager.launch_refusal),
                )
            raise LaunchRefused(manager.launch_refusal) from None
        if not separate_attempt:
            manager.main_worker_entered = True
            # The admitted request owns these choices, including router-filled
            # values. Worker metadata is not an authority for either field.
            manager.admitted_model = request.model
            manager.admitted_model_source = request.metadata.get('model_source')
            manager.admitted_reasoning_effort = request.reasoning_effort
        # This flag is set BEFORE executor entry: no result or spawn callback
        # is required to establish that side effects may have begun.
        manager.launch_entered = True
        if manager.on_launch_entered is not None:
            manager.on_launch_entered()
        try:
            result = self._executor.execute(request)
        except Exception:
            if separate_attempt:
                history.resolve_delegation_attempt(
                    manager.health_db_path, attempt_id, status="failed",
                    failure_kind="exception", detail=role_detail("worker_exception"),
                )
            raise
        persisted = project_notes.private_result(
            result, model=request.model, model_source=request.metadata.get('model_source'),
            reasoning_effort=request.reasoning_effort, catalog=manager.decision_catalog,
        ) if private else result
        if separate_attempt:
            status, failure_kind = attempt_resolution(result)
            history.resolve_delegation_attempt(
                manager.health_db_path, attempt_id, status=status,
                failure_kind=failure_kind, routed_provider=persisted.provider,
                outcome=result.task_status.value, detail=(
                    role_detail(persisted.task_status_reason)
                    + ("; " + fate if (fate := describe_worker_termination(persisted.metadata)) else "")
                ),
                worker_descendants=persisted.metadata.get("worker_descendants"),
            )
        self._manager.launch_result = persisted
        return result


class ExecutionManager:
    """Select one available executor and run at most one provider process."""

    def __init__(
        self,
        config: Config,
        *,
        router: Router | None = None,
        executors: Mapping[str, Executor] | None = None,
        registry: ProviderRegistry | None = None,
        health_db_path: str | Path | None = None,
    ) -> None:
        self.registry = registry or default_registry()
        self.config = config
        self.router = router or Router(config)
        self.routed_result: RoutingResult | None = None
        # Where a runtime quota refusal observed by a worker is recorded.
        # Opt-in on purpose: a manager built for a scenario
        # replay, an evaluation, or a unit test with a fake executor must not
        # write availability facts about the real providers into the real
        # database. The delegation paths that actually spend quota -- the MCP
        # `modelquad_delegate` tool and the plan-execution tools -- pass it.
        self.health_db_path = health_db_path
        self.job_owned_attempt = False
        self.private_notes_execution = False
        self.admitted_model = None
        self.admitted_model_source = None
        self.admitted_reasoning_effort = None
        self.decision_catalog = None
        self.launch_attempt_id: int | None = None
        self.launch_invocation_mode: str | None = None
        self.launch_user_approved = False
        self.launch_caller_host: str | None = None
        self.launch_refusal: str | None = None
        self.launch_result: ExecutionResult | None = None
        self.launch_entered = False
        self.main_worker_entered = False
        self.launch_decision_id: str | None = None
        self.on_launch_entered: Any = None
        self.capability = 'delegate'
        self.independence_excluded = frozenset()
        self.before_worker_entry = None
        if executors is not None:
            # Injected maps are keyed by whatever id the caller uses -- for
            # every current integration the runtime id equals the provider id,
            # so provider-keyed test doubles keep working unchanged.
            self.executors = dict(executors)
        else:
            # Executors belong to AGENT RUNTIMES, not to providers: the
            # map is keyed by runtime id, and a plugin may declare several
            # runtimes. A host-only runtime (Claude Code) has no factory and
            # is simply absent here -- being un-spawnable is what makes it
            # host-only.
            self.executors = {}
            for plugin in self.registry.enabled(config):
                context = ProviderPluginContext(
                    config=config,
                    provider_config=config.providers.get(plugin.id, ProviderConfig()),
                )
                for runtime in plugin.agent_runtimes:
                    if runtime.executor_factory is None:
                        continue
                    self.executors[runtime.id] = runtime.executor_factory(context)

        if health_db_path is not None:
            self.executors = {
                key: _AdmittedExecutor(executor, self)
                for key, executor in self.executors.items()
            }

    def _consultation_permitted(self, provider):
        """Claude is the host and only reports quota; it is never a worker."""
        return provider != 'claude'

    def default_worker(self) -> str | None:
        """The worker a delegation runs on when the caller names none.

        Fixed by what is registered, never by quota or by routing advice:
        Codex when its executor is registered, otherwise the only permitted
        worker, otherwise None (the caller must name one).
        """
        permitted = sorted({
            runtime for runtime in self.executors if self._consultation_permitted(runtime)
        })
        if DEFAULT_WORKER in permitted:
            return DEFAULT_WORKER
        return permitted[0] if len(permitted) == 1 else None

    def require_launch_admission(self) -> None:
        """Recheck controls and the reserved intent at every new worker start.

        Managers without a database are synthetic/offline consumers. Production
        front doors bind the database and their claim before calling a broker.
        A refusal raises through reasoning fallback; it is never a planner fault.
        """
        if self.health_db_path is None:
            return
        if self.launch_refusal is not None:
            raise LaunchRefused(self.launch_refusal)
        from ..service import history

        try:
            state = history.get_launch_state(self.health_db_path)
            policy = check_delegation_policy(
                routing_mode=state["routing_mode"], routing_paused=state["routing_paused"],
                routing_paused_since=state.get('routing_paused_since'),
                invocation_mode=self.launch_invocation_mode,
                user_approved=self.launch_user_approved,
                disconnected_hosts=state["disconnected_hosts"],
                caller_host=self.launch_caller_host,
            )
            if not policy.allowed:
                self.launch_refusal = policy.reason
                raise LaunchRefused(policy.reason)
            history.require_launch_attempt(self.health_db_path, self.launch_attempt_id)
        except history.LaunchStateUnavailable as exc:
            self.launch_refusal = str(exc)
            raise LaunchRefused(str(exc)) from None

    def _runtime_key(self, provider: str, agent_runtime: str | None) -> str:
        """The executor-map key for a (provider, runtime) request.

        An explicit runtime wins; otherwise the provider's default runtime.
        For a provider the registry does not know (injected test doubles),
        the provider id itself is the key -- the historical behavior.
        """

        if agent_runtime is not None:
            return agent_runtime
        default = self.registry.default_runtime_for_provider(provider)
        return default.id if default is not None else provider

    def independence_refused(self, candidates, *, operational=False):
        """An exclusion is the cause only if it removes every usable candidate."""
        eligible = [c for c in candidates if c.eligible and c.score is not None
                    and self._consultation_permitted(c.provider)]
        if operational:
            eligible = [c for c in eligible if
                        (executor := self.executors.get(self._runtime_key(c.provider, c.agent_runtime))) is not None
                        and executor.is_available()]
        return bool(eligible) and bool(self.independence_excluded) and all(
            c.provider in self.independence_excluded for c in eligible)

    def _pool_for_runtime(self, runtime_id: str) -> str | None:
        runtime = self.registry.find_runtime(runtime_id)
        return runtime.resource_pool if runtime is not None else None

    def _with_execution_identity(self, result: ExecutionResult, runtime_id: str) -> ExecutionResult:
        """Record which runtime ran and which pool it drew from.

        Only registry-known identities are recorded; an unknown runtime stays
        unrecorded rather than guessed.
        """

        annotations: dict[str, Any] = {}
        if self.registry.find_runtime(runtime_id) is not None:
            annotations["agent_runtime"] = runtime_id
            pool = self._pool_for_runtime(runtime_id)
            if pool is not None:
                annotations["resource_pool"] = pool
        if not annotations:
            return result
        return replace(result, metadata={**result.metadata, **annotations})

    def _execute_one(
        self,
        provider_name: str,
        executor: Executor,
        task: TaskType,
        prompt: str,
        working_directory: str | Path,
        timeout: float,
        session_id: str | None,
        model: str | None,
        reasoning_effort: str | None,
        metadata: dict[str, Any] | None,
        *,
        agent_runtime: str | None = None,
        on_spawn: Any = None,
        stop_requested: Any = None,
    ) -> ExecutionResult:
        try:
            request = ExecutionRequest(
                provider=provider_name,
                task_type=task,
                prompt=prompt,
                working_directory=Path(working_directory),
                timeout=timeout,
                session_id=session_id,
                model=model,
                reasoning_effort=reasoning_effort,
                metadata=metadata or {},
                agent_runtime=agent_runtime,
                on_spawn=on_spawn,
                stop_requested=stop_requested,
            )
        except ValueError as exc:
            return ExecutionResult(
                provider=provider_name,
                success=False,
                output="",
                exit_code=None,
                error=str(exc),
                task_status=WorkerTaskStatus.FAILED,
                task_status_reason="invalid_request",
            )
        result = executor.execute(request)
        # dataclasses.replace/private_result intentionally discard C's private
        # channel. Retain it on this call's manager, outside every result field.
        self.launch_host_diagnostics = getattr(result, 'host_diagnostics', ())
        self._record_quota_outcome(provider_name, result)
        return result

    def _record_quota_outcome(self, provider: str, result: ExecutionResult) -> None:
        """Feed the routing overlay from what the worker actually did.

        The executor-side WRITER, long declared and deferred. Until it existed, `service/health.py` could
        record a refusal and `quota_policy` could act on one, but the only
        thing that ever produced a row was a human -- so the rule "a fresh
        runtime refusal overrides a snapshot that still reports capacity"
        described a fact the product never observed for itself.

        This is the single point every delegated run passes through, in both
        the explicit-provider path and the routed path, which is why it sits
        here rather than at each caller.

        No window is named. The refusal is derived from the SHAPE of a result
        whose stderr body was discarded at ingestion, so the executor cannot
        honestly say whether a five-hour or a weekly cap was hit.
        `quota_policy.refusal_in_force` handles an unnamed window correctly
        already -- it falls back to the snapshot's binding reset, and to the
        TTL when even that is unknown -- and inventing a window here would
        make a refusal expire on a clock nobody read.

        A recovery requires a COMPLETED task, not merely a clean exit, and it
        is written only when a refusal is actually on record for that
        provider. Both halves are deliberate. The evidence is asymmetric
        because the consequences are: a refusal that stands too long costs at
        most its TTL, while a recovery removes the protection and spends the
        quota the refusal existed to preserve. And `success` is process-level
        only -- `codex exec --json` exits 0 while reporting `turn.failed` with
        `usage_limit_reached`, so treating a clean exit as "served" would let
        the very run that was refused clear the refusal it just caused. A row
        per successful delegation would also say nothing, and would bury the
        rows that do.
        """

        if self.health_db_path is None:
            return
        try:
            from ..service import health as service_health

            evidence = quota_refusal_evidence(result)
            if evidence is not None:
                service_health.record_quota_refusal(
                    self.health_db_path, provider, detail=f"executor:{evidence}"
                )
                return
            if result.task_status is not WorkerTaskStatus.COMPLETED:
                return
            if any(
                refusal.provider == provider
                for refusal in service_health.fetch_quota_refusals(self.health_db_path)
            ):
                service_health.record_quota_recovery(
                    self.health_db_path, provider, detail="executor:served"
                )
        except Exception:  # noqa: BLE001 - an observation is never worth a delegation
            # Same rule as the writer beneath it: losing one observation must
            # not break the execution path that produced it. The work already
            # ran; its result is the caller's, and nothing here may take it away.
            return

    def delegate(
        self,
        task_type: TaskType | str,
        prompt: str,
        working_directory: str | Path,
        *,
        provider: str | None = None,
        agent_runtime: str | None = None,
        statuses: Iterable[ProviderStatus] = (),
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_consumption: ExpectedConsumption | None = None,
        required_capabilities: Iterable[str] | None = None,
        on_spawn: Any = None,
        stop_requested: Any = None,
        verification: Any = None,
        on_verification: Any = None,
        action_authority: TaskAuthority | None = None,
        now: datetime | None = None,
        catalog: ModelCatalog | None = None,
        policy_excluded: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        self.routed_result = None
        self.decision_catalog = catalog
        task = self.router._task_type(task_type)
        # Explicit declarations are carried into the actual request contract,
        # then refused before routing/availability/execution. Legacy callers
        # retain their current behavior without claiming migrated authority.
        if action_authority is not None:
            try:
                request = ExecutionRequest(
                    provider=provider or "unknown", task_type=task, prompt=prompt,
                    working_directory=Path(working_directory), timeout=timeout,
                    session_id=session_id, model=model, reasoning_effort=reasoning_effort,
                    agent_runtime=agent_runtime, action_authority=action_authority,
                )
            except ValueError as exc:
                return ExecutionResult(
                    provider=provider or "unknown", success=False, output="", exit_code=None,
                    error=str(exc), task_status=WorkerTaskStatus.FAILED,
                    task_status_reason="invalid_request",
                )
            return foundation_authority_refusal(request)

        def _finalize(executed: ExecutionResult) -> ExecutionResult:
            # Post-execution hook: stamp the environment snapshot and
            # assemble OutcomeEvidence from the facts the execution produced.
            # When a VerificationSpec names tests and the worker claims
            # completion or partial completion, those tests are recorded as
            # not verified; none is run.
            # Additive and failure-proof -- it can never break a delegation.
            from ..outcome import finalize_execution_result

            finalized = finalize_execution_result(
                executed,
                working_directory=working_directory,
                spec=verification,
                on_verification=on_verification,
            )
            from .. import project_notes
            if self.private_notes_execution or project_notes.private_persistence_required(task, prompt):
                # Front doors persist the returned result, while auxiliary
                # planners use launch_result. Close both views, after evidence
                # assembly, retaining raw output only for the response.
                return project_notes.private_result(
                    finalized, model=self.admitted_model or model,
                    model_source=self.admitted_model_source,
                    reasoning_effort=self.admitted_reasoning_effort or reasoning_effort,
                    catalog=catalog,
                )
            return finalized

        if agent_runtime is not None:
            runtime_descriptor = self.registry.find_runtime(agent_runtime)
            if runtime_descriptor is None:
                return ExecutionResult(
                    provider="unknown", success=False, output="", exit_code=None,
                    error=f"No agent runtime is registered as {agent_runtime!r}.",
                    task_status=WorkerTaskStatus.BLOCKED,
                    task_status_reason="runtime_not_registered",
                )
            if provider is None:
                provider = runtime_descriptor.provider
            elif runtime_descriptor.provider != provider:
                return ExecutionResult(
                    provider="unknown",
                    success=False,
                    output="",
                    exit_code=None,
                    error=(
                        f"Agent runtime {agent_runtime!r} belongs to provider "
                        f"{runtime_descriptor.provider!r}, not {provider!r}."
                    ),
                    task_status=WorkerTaskStatus.BLOCKED,
                    task_status_reason="runtime_provider_mismatch",
                )

        if provider is not None:
            runtime_id = self._runtime_key(provider, agent_runtime)
            executor = self.executors.get(runtime_id)
            if executor is None or not self._consultation_permitted(provider):
                return ExecutionResult(
                    provider="unknown",
                    success=False,
                    output="",
                    exit_code=None,
                    error=f"No executor is registered for provider {provider!r}.",
                    task_status=WorkerTaskStatus.BLOCKED,
                    task_status_reason="provider_not_registered",
                )
            if not executor.is_available():
                return ExecutionResult(
                    provider="unknown",
                    success=False,
                    output="",
                    exit_code=None,
                    error=f"{provider} CLI is not available.",
                    task_status=WorkerTaskStatus.BLOCKED,
                    task_status_reason="provider_unavailable",
                )
            status = {item.name: item for item in statuses}.get(provider)
            if status is None:
                # The executor supplies availability only. Quota, freshness,
                # and effective permissions remain unobserved.
                status = ProviderStatus(name=provider, available=True)
            if policy_excluded is None:  # Legacy callers have no captured decision inputs.
                policy_excluded = netpolicy.providers_refused_by_policy(registry=self.registry)
            refusal = self.router.eligibility_refusal(
                task, provider, status, required_capabilities,
                now=now, policy_excluded=policy_excluded,
            )
            if refusal is not None:
                network_refused = refusal.exclusion_stage == "network_policy"
                return ExecutionResult(
                    provider="unknown", success=False, output="", exit_code=None,
                    error=(f"Explicit choice {provider!r} refused: "
                           f"{', '.join(refusal.ineligibility_reasons)}; {refusal.reason}"),
                    task_status=WorkerTaskStatus.BLOCKED,
                    task_status_reason=NETWORK_POLICY_REFUSED if network_refused else "routing_refused",
                    safe_to_retry=network_refused,
                    metadata={"network_policy": netpolicy.NetworkPolicyDecision(
                        allowed=False, category=netpolicy.PROVIDER_API_CATEGORY,
                        reason=policy_excluded[provider],
                    ).to_dict()} if network_refused else {},
                )
            if provider in self.independence_excluded:
                raise LaunchRefused('independence_unavailable')
            # Explicit provider/runtime selection never falls back to a
            # different one -- the caller asked for this one specifically.
            # Model selection follows the same rule: whatever the caller
            # passed (including nothing) is used as-is, with no preference
            # stage consulted.
            if provider == 'codex':
                metadata = {**(metadata or {}),
                            **({'model_source': (metadata or {}).get('model_source', 'explicit')} if model else {}),
                            **({'reasoning_effort_source': (metadata or {}).get('reasoning_effort_source', 'explicit')}
                               if reasoning_effort else {})}
            return _finalize(
                self._with_execution_identity(
                    self._execute_one(
                        provider,
                        executor,
                        task,
                        prompt,
                        working_directory,
                        timeout,
                        session_id,
                        model,
                        reasoning_effort,
                        metadata,
                        agent_runtime=runtime_id,
                        on_spawn=on_spawn,
                        stop_requested=stop_requested,
                    ),
                    runtime_id,
                )
            )

        if policy_excluded is None:  # Legacy callers retain the process policy read.
            policy_excluded = netpolicy.providers_refused_by_policy(registry=self.registry)
        route_result = self.router.route(
            task,
            statuses,
            expected_consumption=expected_consumption,
            required_capabilities=required_capabilities,
            # The network policy against a provider whose API this process
            # would call itself. Consulted HERE rather than left to the executor's
            # own refusal: eligibility is checked before dispatch in both the
            # routed and explicit paths. The executor's gate remains the last
            # line of defense.
            policy_excluded=policy_excluded, catalog=catalog, now=now,
            **({'independence_excluded': self.independence_excluded} if self.independence_excluded else {}),
        )
        self.routed_result = route_result
        candidates = [
            candidate for candidate in route_result.providers if candidate.score is not None
            and candidate.provider not in self.independence_excluded
            and self._consultation_permitted(candidate.provider)
        ]
        if route_result.recommendation is None and candidates:
            # A TERMINAL refusal, not an absence of options: the router scored
            # candidates and still declined to recommend one (an independence
            # requirement that leaves nothing). Nothing is started and no
            # executor is called; the router's own reason is the outcome.
            return _finalize(
                ExecutionResult(
                    provider="unknown",
                    success=False,
                    output="",
                    exit_code=None,
                    error=route_result.reason or "Routing declined to recommend a provider.",
                    task_status=WorkerTaskStatus.BLOCKED,
                    task_status_reason=('independence_unavailable' if self.independence_refused(route_result.providers)
                                        else 'routing_refused'),
                )
            )
        # The worker is never chosen by the advice, and never by quota. A
        # delegation that names no provider runs on the default worker or on
        # none: no ranking, no substitute, no second worker. The
        # route above is kept only as the advisory record
        # and for its hard gates (availability, capability, network policy,
        # independence), which the default worker must pass like any other.
        worker = self.default_worker()
        candidate = next((c for c in candidates if c.provider == worker), None)
        runtime_id = self._runtime_key(worker, candidate.agent_runtime) if candidate is not None else None
        executor = self.executors.get(runtime_id) if runtime_id is not None else None
        if candidate is None or executor is None or not executor.is_available():
            if worker is None:
                error = ("No default worker: name the worker with the provider argument "
                         "(more than one is registered, or none).")
            elif candidate is None:
                gate = next((c for c in route_result.providers if c.provider == worker), None)
                detail = ", ".join(gate.ineligibility_reasons) if gate is not None else ""
                error = f"The default worker {worker!r} cannot take this task" + (f": {detail}." if detail else ".")
            else:
                error = f"{worker} CLI is not available."
            return _finalize(
                ExecutionResult(
                    provider="unknown",
                    success=False,
                    output="",
                    exit_code=None,
                    error=error,
                    task_status=WorkerTaskStatus.BLOCKED,
                    task_status_reason=('independence_unavailable' if self.independence_refused(route_result.providers, operational=True)
                                        else 'no_viable_worker'),
                    safe_to_retry=False,
                    metadata={"recommended_provider": route_result.recommendation,
                              "worker_source": "default", "attempted_providers": [],
                              "fallback": False},
                )
            )
        # Explicit caller values always win over the configured prior, and the
        # prior applies only when it was computed for this same provider.
        worker_model = model
        worker_effort = reasoning_effort
        target = route_result.target
        if target is not None and target.provider == worker:
            if worker_model is None:
                worker_model = target.model
            if worker_effort is None:
                worker_effort = target.reasoning_effort
        worker_metadata = metadata
        if worker == 'codex':
            worker_metadata = {**(metadata or {}),
                **({'model_source': (metadata or {}).get('model_source', 'explicit' if model else 'default_policy')}
                   if worker_model else {}),
                **({'reasoning_effort_source': (metadata or {}).get('reasoning_effort_source',
                                                                'explicit' if reasoning_effort else 'default_policy')}
                   if worker_effort else {})}
        # Once the worker is dispatched, its result IS the delegation's
        # result, whatever its status and whether or not it carries
        # `safe_to_retry`; the Host decides whether and where to retry.
        result = self._execute_one(
            worker,
            executor,
            task,
            prompt,
            working_directory,
            timeout,
            session_id,
            worker_model,
            worker_effort,
            worker_metadata,
            agent_runtime=runtime_id,
            on_spawn=on_spawn,
            stop_requested=stop_requested,
        )
        result = self._with_execution_identity(result, runtime_id)
        return _finalize(replace(result, metadata={
            **result.metadata,
            "recommended_provider": route_result.recommendation,
            "worker_source": "default",
            "attempted_providers": [worker],
            # Kept for the history column: nothing is ever substituted now.
            "fallback": False,
        }))
