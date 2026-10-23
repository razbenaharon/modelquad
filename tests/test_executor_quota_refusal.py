# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The executor-side quota-refusal write.

An earlier change shipped every half of principle 3 -- "a fresh runtime refusal
overrides a snapshot that still reports capacity" -- except the one that
observes a refusal in the first place. ``service/health.py`` could record
one, ``quota_policy`` could act on one, and the MCP routing and delegation
paths already applied the overlay; but the only thing that ever produced a
row was a person typing it. On 2026-09-06 that gap cost three misrouted
decisions: Codex refused work at 10:30Z while its snapshot read 33% and kept
reading 33% for nearly three hours, because nothing wrote down what the
runtime had just said.

These tests fix the behavior at both ends: what a finished run has to look
like before it counts as a refusal, and what a recorded refusal then does to
the next decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.config import Config
from orchestrator.execution import ExecutionManager, ExecutionResult, WorkerTaskStatus
from orchestrator.execution.base import quota_refusal_evidence
from orchestrator.models import Freshness, ProviderStatus
from orchestrator.router import Router
from orchestrator.service import health as service_health
from orchestrator.service import history as service_history


class _FakeExecutor:
    """Returns a pre-built result, so the shape under test is exact."""

    def __init__(self, name: str, result: ExecutionResult) -> None:
        self.name = name
        self._result = result
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def execute(self, request):  # noqa: ANN001 - test double
        self.calls += 1
        return self._result


def _refused(provider: str = "codex", **metadata) -> ExecutionResult:
    return ExecutionResult(
        provider=provider,
        success=False,
        output="",
        exit_code=1,
        error="worker reported a non-zero exit (rate_limit; 42 bytes of stderr withheld)",
        metadata=metadata or {"stderr_category": "rate_limit"},
        task_status=WorkerTaskStatus.FAILED,
        task_status_reason="worker_failed",
    )


def _served(provider: str = "codex", **metadata) -> ExecutionResult:
    return ExecutionResult(
        provider=provider,
        success=True,
        output="done",
        exit_code=0,
        metadata=dict(metadata),
        task_status=WorkerTaskStatus.COMPLETED,
    )


def _manager(db_path, result: ExecutionResult, provider: str = "codex", *, config=None, registry=None) -> ExecutionManager:
    manager = ExecutionManager(
        config or Config(), registry=registry,
        executors={provider: _FakeExecutor(provider, result)},
        health_db_path=db_path,
    )
    manager.launch_attempt_id = service_history.record_delegation_attempt(
        db_path, task_type="review", working_directory=db_path.parent,
        request_key=service_history.request_fingerprint("review", db_path.parent, "do the thing"),
    )
    assert manager.launch_attempt_id is not None
    return manager


def _delegate(manager: ExecutionManager, tmp_path, provider: str = "codex") -> ExecutionResult:
    result = manager.delegate(
        "review", "do the thing", tmp_path, provider=provider,
        on_spawn=lambda pid: service_history.record_worker_pid(
            manager.health_db_path, manager.launch_attempt_id, pid,
        ),
    )
    service_history.resolve_delegation_attempt(
        manager.health_db_path, manager.launch_attempt_id, status="completed" if result.task_completed else "failed",
        outcome=result.task_status.value,
    )
    return result


def _status(provider: str, remaining: int, *, observed_at: datetime) -> ProviderStatus:
    return ProviderStatus(
        name=provider,
        available=True,
        remaining_percentage=remaining,
        observed_at=observed_at,
        freshness=Freshness.FRESH,
    )


def _quota_rows(db_path) -> int:
    """Every quota-marked row, refusals and recoveries alike."""
    from orchestrator.service import db as service_db

    if not db_path.exists():
        return 0
    conn = service_db.connect_and_init(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM provider_health WHERE detail LIKE ? OR detail LIKE ?",
            (f"{service_health.REFUSAL_PREFIX}%", f"{service_health.RECOVERY_PREFIX}%"),
        ).fetchone()[0]
    finally:
        conn.close()


# -- what counts as a refusal ------------------------------------------------


def test_a_typed_rate_limit_event_is_the_strongest_evidence():
    """Codex's own turn.failed code -- the provider said it, nothing is guessed."""
    result = _refused(worker_metrics={"rate_limit_events": 2}, stderr_category="error")

    assert quota_refusal_evidence(result) == "worker_metrics.rate_limit_events"


def test_the_stderr_category_is_read_when_no_typed_signal_exists():
    """A result envelope with no rate-limit field at all leaves its refusals
    visible only as the coarse category the executor derived."""
    result = _refused("claude", stderr_category="rate_limit")

    assert quota_refusal_evidence(result) == "stderr_category.rate_limit"


def test_a_zero_count_is_not_a_refusal():
    """"Observed zero" is a real reading and must not read as a refusal."""
    assert quota_refusal_evidence(_refused(worker_metrics={"rate_limit_events": 0})) is None


def test_a_malformed_count_is_not_a_refusal():
    """A bool is an int in Python; a malformed payload must not become evidence."""
    assert quota_refusal_evidence(_refused(worker_metrics={"rate_limit_events": True})) is None
    assert quota_refusal_evidence(_refused(worker_metrics={"rate_limit_events": "many"})) is None


def test_a_failure_for_any_other_reason_is_not_a_refusal():
    assert quota_refusal_evidence(_refused(stderr_category="permission")) is None
    assert quota_refusal_evidence(_refused(stderr_category="sandbox")) is None
    assert quota_refusal_evidence(_refused()) is not None  # the control


def test_a_completed_task_is_never_a_refusal_however_it_narrated():
    """The false positive the completion gate exists for.

    ``classify_stderr`` is a substring test over "usage limit" / "rate limit" /
    "quota". A worker that COMPLETES a task about quota-aware routing prints
    all three -- the workers on this repository do it routinely. Without the
    gate, a delegation that worked would take its own provider out of routing
    for the next half hour on the strength of a word in a log line.
    """
    served = _served(stderr_category="rate_limit", worker_metrics={"rate_limit_events": 3})

    assert quota_refusal_evidence(served) is None


def test_a_typed_refusal_counts_even_when_the_process_exits_cleanly():
    """The shape a clean-exit gate would have lost, and the reason there is none.

    `codex exec --json` reports `turn.failed` with `usage_limit_reached` and
    still exits 0. Read as a served run, that both loses the refusal and --
    through the recovery rule -- clears one already on record, so the very run
    the provider turned away would restore it to routing.
    """
    graceful = ExecutionResult(
        provider="codex",
        success=True,
        output="",
        exit_code=0,
        metadata={"stderr_category": "empty", "worker_metrics": {"rate_limit_events": 1}},
        task_status=WorkerTaskStatus.FAILED,
        task_status_reason="worker_failed",
    )

    assert quota_refusal_evidence(graceful) == "worker_metrics.rate_limit_events"


def test_narration_alone_needs_a_non_zero_exit_to_count():
    """The typed signal is trusted without the exit code; the heuristic is not.

    An uncompleted run that exited 0 tells us nothing about quota. Trusting
    the substring here would let any worker that merely mentioned a quota and
    produced no completion envelope -- which is most of them, on this
    repository -- retire its own provider.
    """
    clean_exit = ExecutionResult(
        provider="codex",
        success=True,
        output="",
        exit_code=0,
        metadata={"stderr_category": "rate_limit"},
        task_status=WorkerTaskStatus.UNKNOWN,
        task_status_reason="no_completion_envelope",
    )

    assert quota_refusal_evidence(clean_exit) is None


# -- what the manager then writes --------------------------------------------


def test_a_refused_run_leaves_a_refusal_row(tmp_path):
    db_path = tmp_path / "history.db"

    _delegate(_manager(db_path, _refused()), tmp_path)

    (refusal,) = service_health.fetch_quota_refusals(db_path)
    assert refusal.provider == "codex"
    assert refusal.detail is not None
    assert refusal.detail.startswith("quota_refusal:")
    assert "executor:stderr_category.rate_limit" in refusal.detail


def test_the_refusal_names_no_window(tmp_path):
    """A result whose stderr body was discarded cannot say which cap was hit.

    ``refusal_in_force`` reads an unnamed window against the snapshot's own
    binding reset, and falls back to the TTL when that is unknown too --
    inventing "five_hour" here would expire the refusal on a clock nobody read.
    """
    db_path = tmp_path / "history.db"

    _delegate(_manager(db_path, _refused()), tmp_path)

    (refusal,) = service_health.fetch_quota_refusals(db_path)
    assert refusal.window is None


def test_the_recorded_refusal_removes_the_provider_from_the_next_decision(tmp_path):
    """End to end at the manager: run -> row -> overlay -> the router declines."""
    db_path = tmp_path / "history.db"
    _delegate(_manager(db_path, _refused()), tmp_path)

    snapshot = _status("codex", 84, observed_at=datetime.now(timezone.utc) - timedelta(hours=1))
    overlaid = service_health.overlay_quota_refusals([snapshot], db_path)

    assert overlaid[0].available is False
    assert overlaid[0].remaining_percentage == 84  # the reading itself stands
    result = Router(Config()).route("implementation", overlaid)
    codex = next(p for p in result.providers if p.provider == "codex")
    assert codex.eligible is False


def test_a_later_served_run_clears_the_refusal(tmp_path):
    db_path = tmp_path / "history.db"
    _delegate(_manager(db_path, _refused()), tmp_path)
    assert service_health.fetch_quota_refusals(db_path)

    _delegate(_manager(db_path, _served()), tmp_path)

    assert service_health.fetch_quota_refusals(db_path) == ()


def test_a_clean_exit_without_a_completed_task_does_not_clear_a_refusal(tmp_path):
    """A recovery removes the protection, so it needs the stronger evidence.

    The consequences are asymmetric: a refusal that stands too long costs at
    most its TTL, while a false recovery spends the quota the refusal existed
    to preserve. A process that exited 0 without a completion envelope has not
    shown that the provider served anything.
    """
    db_path = tmp_path / "history.db"
    _delegate(_manager(db_path, _refused()), tmp_path)

    unverified = ExecutionResult(
        provider="codex",
        success=True,
        output="ok",
        exit_code=0,
        metadata={"stderr_category": "empty"},
        task_status=WorkerTaskStatus.UNKNOWN,
        task_status_reason="no_completion_envelope",
    )
    _delegate(_manager(db_path, unverified), tmp_path)

    (refusal,) = service_health.fetch_quota_refusals(db_path)
    assert refusal.provider == "codex"


def test_a_served_run_with_nothing_on_record_writes_nothing(tmp_path):
    """A recovery row per successful delegation would say nothing, and would
    bury the rows that do."""
    db_path = tmp_path / "history.db"

    _delegate(_manager(db_path, _served()), tmp_path)

    assert _quota_rows(db_path) == 0


def test_a_recovery_only_clears_the_provider_that_served(tmp_path):
    from orchestrator.registry import (
        ProviderCapabilities, ProviderPlugin, UsageSourceKind, WORKSPACE_DEVELOPMENT_GRANT,
        default_registry,
    )
    registry = default_registry()
    registry.register(ProviderPlugin(
        id="test_quota", display_name="Synthetic quota worker",
        capabilities=ProviderCapabilities(review=True), usage_source_kind=UsageSourceKind.UNKNOWN,
        capability_profile=WORKSPACE_DEVELOPMENT_GRANT,
    ))
    config = Config(task_fit={
        task: {**fits, "test_quota": fits["codex"]}
        for task, fits in Config().task_fit.items()
    })
    profile = registry.get("test_quota").capability_state()
    config.provider_capability_profiles["test_quota"] = profile
    config.provider_capabilities["test_quota"] = profile.operational_capabilities
    db_path = tmp_path / "history.db"
    _delegate(_manager(db_path, _refused("codex")), tmp_path)
    _delegate(_manager(db_path, _refused("test_quota"), "test_quota",
                       config=config, registry=registry), tmp_path, "test_quota")

    _delegate(_manager(db_path, _served("codex")), tmp_path)

    remaining = {refusal.provider for refusal in service_health.fetch_quota_refusals(db_path)}
    assert remaining == {"test_quota"}


def test_the_routed_path_records_too(tmp_path):
    """Both entrances to ``_execute_one`` -- the caller's explicit provider and
    the router's own ranked candidates -- pass the same call site."""
    db_path = tmp_path / "history.db"
    manager = _manager(db_path, _refused())

    manager.delegate(
        "review",
        "do the thing",
        tmp_path,
        statuses=[_status("codex", 84, observed_at=datetime.now(timezone.utc))],
    )

    (refusal,) = service_health.fetch_quota_refusals(db_path)
    assert refusal.provider == "codex"


# -- what it must never do ---------------------------------------------------


def test_recording_is_opt_in(tmp_path):
    """A scenario replay, an evaluation, or a unit test with a fake executor
    must not write availability facts about real providers."""
    db_path = tmp_path / "history.db"
    manager = ExecutionManager(
        Config(), executors={"codex": _FakeExecutor("codex", _refused())}
    )

    manager.delegate("review", "do the thing", tmp_path, provider="codex")

    assert manager.health_db_path is None
    assert not db_path.exists()


def test_a_failing_recorder_never_takes_the_result_away(tmp_path, monkeypatch):
    """The work already ran; its result is the caller's. Losing one
    observation must not turn a finished delegation into an error."""
    db_path = tmp_path / "history.db"

    def _explode(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(service_health, "record_quota_refusal", _explode)

    result = _delegate(_manager(db_path, _refused()), tmp_path)

    assert result.task_status is WorkerTaskStatus.FAILED
    assert result.error is not None


def test_a_pre_execution_failure_records_nothing(tmp_path):
    """No worker ran, so the provider said nothing about its own quota."""
    db_path = tmp_path / "history.db"
    manager = ExecutionManager(Config(), executors={}, health_db_path=db_path)

    result = manager.delegate("review", "do the thing", tmp_path, provider="codex")

    assert result.task_status_reason == "provider_not_registered"
    assert _quota_rows(db_path) == 0
