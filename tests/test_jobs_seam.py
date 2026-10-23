# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Inert contract tests; no runner, database, or retained content on disk."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from functools import wraps
from inspect import signature
from secrets import token_urlsafe
from threading import RLock

import pytest

from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus, attempt_resolution
from orchestrator.execution.lifecycle import TerminalResolution
from orchestrator.jobs.seam import (
    EXPIRY_WARNING, MAX_RETENTION, RETENTION, RETENTION_DAYS, CollectResult, JobOutput,
    JobRef, JobStatus, JobOutcome, OutputSink, PendingEntry, job_status_from_resolution,
    cause_flags_from_result,
)
from orchestrator.service.history import DELEGATION_FAILURE_KINDS


def output() -> JobOutput:
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    return JobOutput(
        JobRef("opaque-job-a", 1, "decision-a"), JobStatus.FINISHED,
        WorkerTaskStatus.UNKNOWN, None, "available answer", False,
        None, None, None, now, now + timedelta(seconds=1),
    )


@pytest.mark.parametrize("path", [
    "/tmp/file", "C:/file", "C:\\file", "C:file", "\\\\server\\share\\file",
    "\\file", "../file", "src/../file", "src\\..\\file", "D:relative", "",
    "src/file:stream", "src/\x00file", ".", "./", ".\\", "././",
])
def test_rejects_non_project_relative_paths(path):
    with pytest.raises(ValueError):
        replace(output(), changed_files=(path,))


def test_relative_paths_and_observation_distinction():
    assert output().changed_files is None
    assert replace(output(), changed_files=()).changed_files == ()
    paths = ["src/./app.py", "src\\other.py", "src//other.py", "./src/app.py"]
    result = replace(output(), changed_files=paths)
    paths.append("later.py")
    assert result.changed_files == ("src/app.py", "src/other.py")


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@pytest.mark.parametrize("tz", [None, timezone(timedelta(hours=2))])
def test_rejects_naive_or_non_utc_output_times(field, tz):
    with pytest.raises(ValueError):
        replace(output(), **{field: datetime(2026, 10, 5, tzinfo=tz)})


def test_rejects_reversed_times():
    with pytest.raises(ValueError):
        replace(output(), ended_at=output().started_at - timedelta(seconds=1))


@pytest.mark.parametrize("field", ["retained_at", "expires_at", "extended_until"])
def test_collect_times_are_utc(field):
    result = collection(output())
    with pytest.raises(ValueError):
        replace(result, **{field: datetime(2026, 10, 5)})


def test_frozen_records_and_fact_snapshots():
    facts = {"descendants": [{"pid": 123}], "enumerated": True}
    result = replace(output(), descendants=facts, termination={"confirmed_dead": True})
    facts["descendants"][0]["pid"] = 999
    assert result.descendants["descendants"][0]["pid"] == 123
    with pytest.raises(TypeError):
        result.descendants["descendants"][0]["pid"] = 456
    with pytest.raises(TypeError):
        result.termination["confirmed_dead"] = False
    for record, field, value in [
        (result, "partial", True), (result.job, "job_id", "other"),
        (collection(result), "receipt", "other"),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(record, field, value)


RESOLUTIONS = [
    ("completed", None, JobStatus.FINISHED),
    ("refused", "policy_refused", JobStatus.FAILED),
    ("refused", "duplicate_in_flight", JobStatus.FAILED),
    ("failed", "terminated", None),
    ("failed", "timeout", None),
    ("failed", "orphaned", JobStatus.INTERRUPTED),
    *[("failed", kind, JobStatus.FAILED) for kind in (
        "exception", "no_viable_worker", "provider_unavailable",
        "failed_to_start", "invalid_request", "internal_error",
    )],
]


@pytest.mark.parametrize("status,kind,ordinary", RESOLUTIONS)
@pytest.mark.parametrize("stop,deadline", [(False, False), (True, False), (False, True), (True, True)])
def test_every_terminal_resolution_and_cause_combination(status, kind, ordinary, stop, deadline):
    resolution = TerminalResolution(status, kind)
    if not stop and not deadline and ordinary is None:
        with pytest.raises(ValueError, match="requires a stop or deadline"):
            job_status_from_resolution(resolution, stop_requested=stop, deadline_passed=deadline)
    else:
        expected = ordinary if status in {"completed", "refused"} or kind == "orphaned" else (
            JobStatus.STOPPED if stop else JobStatus.TIMED_OUT if deadline else ordinary
        )
        assert job_status_from_resolution(
            resolution, stop_requested=stop, deadline_passed=deadline,
        ) is expected


def test_mapping_covers_existing_failure_vocabulary_and_only_approved_statuses():
    assert {kind for _, kind, _ in RESOLUTIONS if kind} == set(DELEGATION_FAILURE_KINDS)
    assert {status.value for status in JobStatus} == {
        "finished", "failed", "stopped", "interrupted", "timed_out",
    }


@pytest.mark.parametrize("resolution", [
    TerminalResolution("started", None), TerminalResolution("unknown", None),
    TerminalResolution("failed", None), TerminalResolution("completed", "timeout"),
    TerminalResolution("failed", "new_kind"),
])
def test_unresolved_or_invalid_inputs_are_not_final_statuses(resolution):
    with pytest.raises(ValueError):
        job_status_from_resolution(resolution, stop_requested=False, deadline_passed=False)


@pytest.mark.parametrize("claim", list(WorkerTaskStatus))
def test_worker_claim_never_changes_job_fate(claim):
    result = ExecutionResult("codex", True, "answer", 0, task_status=claim)
    stop, deadline = cause_flags_from_result(result)
    assert job_status_from_resolution(TerminalResolution(*attempt_resolution(result)), stop_requested=stop, deadline_passed=deadline) is JobStatus.FINISHED
    assert replace(output(), worker_claim=claim).worker_claim is claim


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("stop,deadline", [(True, False), (False, True), (True, True)])
def test_execution_result_stop_and_deadline_are_independent_of_death(confirmed, stop, deadline):
    result = ExecutionResult(
        "codex", False, "partial", None,
        task_status_reason="stop_requested" if stop else None,
        metadata={"stop_requested": stop, "timed_out": deadline, "worker_termination": {
            "confirmed_dead": confirmed, "tree_kill": "ok" if confirmed else "unknown",
        }},
    )
    expected = JobStatus.STOPPED if stop else JobStatus.TIMED_OUT
    flags = cause_flags_from_result(result)
    assert flags == (stop, deadline)
    assert job_status_from_resolution(
        TerminalResolution(*attempt_resolution(result)), stop_requested=flags[0],
        deadline_passed=flags[1],
    ) is expected
    assert replace(output(), job_status=expected, tree_confirmed_dead=confirmed).tree_confirmed_dead is confirmed


def test_user_stop_before_launch_is_stopped_with_separate_attempt_failure():
    result = ExecutionResult(
        "codex", False, "", None, task_status_reason="stop_requested",
        metadata={"worker_started": False},
    )
    assert result.terminal_resolution == TerminalResolution("failed", "failed_to_start")
    stop, deadline = cause_flags_from_result(result)
    assert (stop, deadline) == (True, False)
    assert job_status_from_resolution(
        TerminalResolution(*attempt_resolution(result)), stop_requested=stop, deadline_passed=deadline,
    ) is JobStatus.STOPPED


def test_stop_reason_without_metadata_flag_is_stopped():
    result = ExecutionResult("codex", False, "partial", None,
                             task_status_reason="stop_requested")
    assert attempt_resolution(result) == ("failed", "timeout")
    stop, deadline = cause_flags_from_result(result)
    assert (stop, deadline) == (True, False)
    assert job_status_from_resolution(
        TerminalResolution(*attempt_resolution(result)), stop_requested=stop,
        deadline_passed=deadline,
    ) is JobStatus.STOPPED


@pytest.mark.parametrize("value", [None, False, 0, 1, "true"])
@pytest.mark.parametrize("flag", ["stop_requested", "timed_out"])
def test_cause_metadata_truthiness_matches_execution_classifier(value, flag):
    result = ExecutionResult("codex", True, "answer", 0,
                             metadata={flag: value})
    stop, deadline = cause_flags_from_result(result)
    assert (stop, deadline) == (bool(value) and flag == "stop_requested",
                                bool(value) and flag == "timed_out")
    assert attempt_resolution(result) == (("failed", "timeout") if value else ("completed", None))
    assert job_status_from_resolution(
        TerminalResolution(*attempt_resolution(result)), stop_requested=stop,
        deadline_passed=deadline,
    ) is (JobStatus.STOPPED if stop else JobStatus.TIMED_OUT if deadline else JobStatus.FINISHED)


def collection(item=None, retained_at=None):
    item = item or output()
    retained_at = retained_at or item.ended_at + timedelta(hours=2)
    return CollectResult(item, token_urlsafe(32), retained_at,
                         retained_at + RETENTION, False, None)


@pytest.mark.parametrize("job_id", ["", " ", None, 1])
def test_job_id_is_nonempty(job_id):
    with pytest.raises(ValueError):
        JobRef(job_id, 1, None)


@pytest.mark.parametrize("attempt_id", [True, False, "1", 1.0, None])
def test_attempt_id_is_integer_not_bool(attempt_id):
    with pytest.raises(ValueError):
        JobRef("opaque", attempt_id, None)


@pytest.mark.parametrize("claim", ["completed", None, True, 1])
def test_worker_claim_requires_enum(claim):
    with pytest.raises(ValueError):
        replace(output(), worker_claim=claim)


@pytest.mark.parametrize("dead", [None, True, False])
def test_tree_death_is_an_independent_optional_fact(dead):
    assert replace(output(), tree_confirmed_dead=dead).tree_confirmed_dead is dead


def test_retention_starts_at_store_acceptance_and_validates_effective_expiry():
    result = collection()
    assert result.retained_at > result.output.ended_at
    assert result.expires_at == result.retained_at + RETENTION
    assert RETENTION_DAYS == 7
    assert EXPIRY_WARNING == timedelta(days=1)
    assert MAX_RETENTION == timedelta(days=30)
    limit = result.retained_at + MAX_RETENTION
    assert replace(result, extended_until=limit, expires_at=limit).expires_at == limit
    for until in [limit + timedelta(microseconds=1), result.retained_at, result.retained_at + RETENTION - timedelta(microseconds=1)]:
        with pytest.raises(ValueError):
            replace(result, extended_until=until, expires_at=until)
    with pytest.raises(ValueError):
        replace(result, expires_at=result.expires_at + timedelta(seconds=1))
    with pytest.raises(ValueError):
        replace(result, extended_until=limit)


def test_stop_clicked_as_worker_finished_remains_finished():
    assert job_status_from_resolution(
        TerminalResolution("completed", None), stop_requested=True, deadline_passed=True,
    ) is JobStatus.FINISHED


@pytest.mark.parametrize("value", ["", " ", 1, True])
def test_decision_id_requires_nonempty_string_or_none(value):
    with pytest.raises(ValueError):
        JobRef("job", 1, value)


@pytest.mark.parametrize("field,value", [
    ("job_status", "finished"), ("job_status", None),
    ("partial", 1), ("partial", "false"), ("partial", None),
    ("output_text", 1), ("output_text", None),
])
def test_output_field_types(field, value):
    with pytest.raises(ValueError):
        replace(output(), **{field: value})


@pytest.mark.parametrize("value", [True, 1, [], {}])
def test_claim_reason_requires_none_or_str(value):
    with pytest.raises(ValueError, match="claim_reason"):
        replace(output(), claim_reason=value)


@pytest.mark.parametrize("value", [None, "", " ", True, 1])
def test_receipt_requires_nonempty_str(value):
    with pytest.raises(ValueError, match="receipt"):
        replace(collection(), receipt=value)


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_expiry_warning_requires_bool(value):
    with pytest.raises(ValueError, match="expiring_soon"):
        replace(collection(), expiring_soon=value)
    with pytest.raises(ValueError, match="expiring_soon"):
        PendingEntry(output().job, JobStatus.FINISHED, NOW + RETENTION, value)


def test_acceptance_cannot_precede_output_end():
    with pytest.raises(ValueError, match="retained_at"):
        collection(retained_at=output().ended_at - timedelta(microseconds=1))
    assert collection(retained_at=output().ended_at).retained_at == output().ended_at


NOW = output().ended_at + timedelta(hours=2)


def utc(value):
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("time must be aware UTC")


def serialized(method):
    """Model one atomic sink operation, publishing its now only after serving it."""
    parameters = signature(method)

    @wraps(method)
    def serve(self, *args, **kwargs):
        now = parameters.bind(self, *args, **kwargs).arguments["now"]
        utc(now)
        with self.lock:
            result = method(self, *args, **kwargs)
            self.latest_now = now if self.latest_now is None else max(now, self.latest_now)
            return result

    return serve


class MemorySink:
    """Test-only sink; explicit time and content-free removal records."""

    def __init__(self):
        self.entries: dict[str, CollectResult] = {}
        self.completed: dict[str, JobOutcome] = {}
        self.receipts: dict[str, str] = {}
        self.lock = RLock()
        self.latest_now: datetime | None = None

    def _remove(self, job_id, state, when):
        result = self.entries.pop(job_id)
        when = when if self.latest_now is None else max(when, self.latest_now)
        self.completed[job_id] = JobOutcome(result.output.job, state, when)

    @serialized
    def put(self, output: JobOutput, now: datetime) -> bool:
        utc(now)
        if now < output.ended_at:
            raise ValueError("now must not precede output.ended_at")
        self.purge_expired(now)
        job_id = output.job.job_id
        if job_id in self.completed:
            return False
        if job_id in self.entries:
            if self.entries[job_id].output != output:
                raise ValueError("conflicting content for job_id")
            return False
        self.entries[job_id] = collection(output, now)
        return True

    @serialized
    def collect(self, job_id: str, now: datetime) -> CollectResult | None:
        self.purge_expired(now)
        result = self.entries.get(job_id)
        if result is None:
            return None
        return replace(result, expiring_soon=result.expires_at - now <= EXPIRY_WARNING)

    @serialized
    def acknowledge(self, job_id: str, receipt: str, now: datetime) -> bool:
        result = self.collect(job_id, now)
        if result is None:
            outcome = self.completed.get(job_id)
            return (outcome is not None and outcome.state == "acknowledged"
                    and self.receipts.get(job_id) == receipt)
        if result.receipt != receipt:
            return False
        self._remove(job_id, "acknowledged", now)
        self.receipts[job_id] = receipt
        return True

    @serialized
    def extend(self, job_id: str, until: datetime, now: datetime) -> CollectResult | None:
        result = self.collect(job_id, now)
        if result is None:
            return None
        utc(until)
        if until == result.expires_at:
            return result
        extended = replace(result, extended_until=until, expires_at=until)
        if until < result.expires_at:
            raise ValueError("extension must not shorten retention")
        extended = replace(extended, expiring_soon=until - now <= EXPIRY_WARNING)
        self.entries[job_id] = extended
        return extended

    @serialized
    def delete(self, job_id: str, now: datetime) -> bool:
        if self.collect(job_id, now) is None:
            return False
        self._remove(job_id, "deleted", now)
        return True

    @serialized
    def pending(self, now: datetime) -> tuple[PendingEntry, ...]:
        self.purge_expired(now)
        ordered = sorted(self.entries.values(), key=lambda r: (r.expires_at, r.output.job.job_id))
        return tuple(PendingEntry(r.output.job, r.output.job_status, r.expires_at,
                                  r.expires_at - now <= EXPIRY_WARNING) for r in ordered)

    @serialized
    def purge_expired(self, now: datetime) -> tuple[JobRef, ...]:
        utc(now)
        expired = sorted((r for r in self.entries.values() if r.expires_at <= now),
                         key=lambda r: (r.expires_at, r.output.job.job_id))
        for result in expired:
            self._remove(result.output.job.job_id, "expired", now)
        return tuple(r.output.job for r in expired)

    @serialized
    def state(self, job_id: str, now: datetime):
        self.purge_expired(now)
        if job_id in self.entries:
            return "pending"
        outcome = self.completed.get(job_id)
        return None if outcome is None else outcome.state

    @serialized
    def outcomes(self, now: datetime, *, since: datetime) -> tuple[JobOutcome, ...]:
        utc(since)
        self.purge_expired(now)
        return tuple(sorted((r for r in self.completed.values() if since <= r.ended_at <= now),
                            key=lambda r: (r.ended_at, r.job.job_id)))


def test_sink_repeatable_collect_idempotent_put_and_acknowledge():
    sink: OutputSink = MemorySink()
    item = output()
    job_id = item.job.job_id
    assert sink.state("missing", NOW) is None
    assert sink.collect("missing", NOW) is None
    assert sink.acknowledge("missing", "wrong", NOW) is False
    assert sink.put(item, NOW) is True
    assert sink.state(job_id, NOW) == "pending"
    first = sink.collect(job_id, NOW)
    assert sink.put(replace(item), NOW) is False
    assert sink.collect(job_id, NOW) == first
    with pytest.raises(ValueError, match="conflicting"):
        sink.put(replace(item, output_text="different"), NOW)
    assert sink.collect(job_id, NOW) == first
    assert first.output is item
    assert sink.acknowledge(job_id, "wrong", NOW) is False
    assert sink.collect(job_id, NOW) == first
    assert sink.acknowledge(job_id, first.receipt, NOW) is True
    assert sink.acknowledge(job_id, first.receipt, NOW) is True
    assert sink.acknowledge(job_id, "wrong", NOW) is False
    assert sink.outcomes(NOW, since=NOW) == (JobOutcome(item.job, "acknowledged", NOW),)


def test_extend_is_bounded_from_acceptance_and_preserves_receipt():
    sink: OutputSink = MemorySink()
    item = output()
    assert sink.extend("missing", item.ended_at, NOW) is None
    sink.put(item, NOW)
    initial = sink.collect(item.job.job_id, NOW)
    limit = initial.retained_at + MAX_RETENTION
    extended = sink.extend(item.job.job_id, limit, NOW)
    assert extended.expires_at == extended.extended_until == limit
    assert extended.retained_at == initial.retained_at
    assert extended.receipt == initial.receipt
    assert sink.extend(item.job.job_id, limit, NOW) == extended
    assert sink.collect(item.job.job_id, NOW) == extended
    for until in [limit + timedelta(microseconds=1), initial.expires_at, limit.replace(tzinfo=None)]:
        with pytest.raises(ValueError):
            sink.extend(item.job.job_id, until, NOW)
    assert sink.collect(item.job.job_id, NOW) == extended


def test_extend_to_default_expiry_is_noop_without_explicit_extension():
    sink = MemorySink()
    item = output()
    sink.put(item, NOW)
    initial = sink.collect(item.job.job_id, NOW)
    assert initial.extended_until is None
    assert sink.extend(item.job.job_id, initial.expires_at, NOW) == initial
    assert sink.collect(item.job.job_id, NOW).extended_until is None


@pytest.mark.parametrize("until", [None, "invalid", NOW.replace(tzinfo=None),
                                   NOW - timedelta(days=1), NOW + MAX_RETENTION + timedelta(days=1)])
def test_missing_job_returns_none_before_until_validation(until):
    assert MemorySink().extend("missing", until, NOW) is None


@pytest.mark.parametrize("text,changes", [("", None), ("", ()), ("captured", None),
                                          ("", ("src/app.py",)), ("captured", ("src/app.py",))])
def test_interrupted_job_is_put_with_reconciliation_facts(text, changes):
    reconcile_time = NOW + timedelta(hours=1)
    interrupted = replace(
        output(), job_status=job_status_from_resolution(
            TerminalResolution("failed", "orphaned"), stop_requested=False,
            deadline_passed=False,
        ), ended_at=reconcile_time, worker_claim=WorkerTaskStatus.UNKNOWN,
        claim_reason="interrupted", output_text=text, partial=bool(text or changes),
        changed_files=changes,
    )
    sink: OutputSink = MemorySink()
    assert sink.put(interrupted, reconcile_time) is True
    assert sink.pending(reconcile_time)[0].job_status is JobStatus.INTERRUPTED
    collected = sink.collect(interrupted.job.job_id, reconcile_time).output
    assert collected.ended_at == reconcile_time
    assert collected.worker_claim is WorkerTaskStatus.UNKNOWN
    assert collected.claim_reason == "interrupted"
    assert collected.output_text == text
    assert collected.partial is bool(text or changes)
    assert collected.changed_files == changes


@pytest.mark.parametrize("operation", ["acknowledge", "delete", "purge_expired"])
def test_older_mutation_time_after_query_is_visible_and_inclusive(operation):
    sink = MemorySink()
    item = output()
    sink.put(item, NOW)
    receipt = sink.collect(item.job.job_id, NOW).receipt
    if operation == "purge_expired":
        older_time = NOW + RETENTION
        # A serialized put can follow a query with an older timestamp.
        sink.delete(item.job.job_id, NOW)
        previous_now = older_time + timedelta(seconds=1)
        sink.outcomes(previous_now, since=NOW)
        item = replace(item, job=JobRef("late-job", 2, None))
        sink.put(item, NOW)
    else:
        older_time = NOW
        previous_now = NOW + timedelta(seconds=1)
        assert sink.outcomes(previous_now, since=NOW) == ()
    if operation == "acknowledge":
        assert sink.acknowledge(item.job.job_id, receipt, older_time) is True
        state = "acknowledged"
    elif operation == "delete":
        assert sink.delete(item.job.job_id, older_time) is True
        state = "deleted"
    else:
        assert sink.purge_expired(older_time) == (item.job,)
        state = "expired"
    event = JobOutcome(item.job, state, previous_now)
    next_now = previous_now + timedelta(seconds=1)
    assert sink.outcomes(next_now, since=previous_now) == (event,)
    # Query endpoints overlap; Host deduplication retains the state once.
    repeat = sink.outcomes(next_now, since=previous_now)
    assert {(r.job.job_id, r.state) for r in (event, *repeat)} == {(item.job.job_id, state)}


@pytest.mark.parametrize("finish", ["delete", "expire", "acknowledge"])
def test_removed_content_leaves_only_outcome_and_optional_receipt(finish):
    sink = MemorySink()
    item = output()
    job_id = item.job.job_id
    sink.put(item, NOW)
    result = sink.collect(job_id, NOW)
    now = NOW
    if finish == "delete":
        assert sink.delete(job_id, now) is True
        expected = "deleted"
    elif finish == "expire":
        now = result.expires_at + timedelta(hours=1)
        assert sink.purge_expired(now) == (item.job,)
        expected = "expired"
    else:
        assert sink.acknowledge(job_id, result.receipt, now) is True
        expected = "acknowledged"
    when = now
    assert sink.state(job_id, now) == expected
    assert sink.entries == {}
    assert set(vars(sink)) == {"entries", "completed", "receipts", "lock", "latest_now"}
    assert sink.completed == {job_id: JobOutcome(item.job, expected, when)}
    assert sink.receipts == ({job_id: result.receipt} if finish == "acknowledge" else {})
    assert sink.collect(job_id, now) is None
    assert sink.acknowledge(job_id, result.receipt, now) is (finish == "acknowledge")
    assert sink.pending(now) == ()
    assert sink.extend(job_id, result.expires_at, now) is None
    assert sink.delete(job_id, now) is False
    assert sink.put(item, now) is False
    assert sink.put(replace(item, partial=True, output_text="different"), now) is False
    assert sink.outcomes(now, since=NOW) == (JobOutcome(item.job, expected, when),)
    assert sink.outcomes(now, since=when + timedelta(microseconds=1)) == ()


def test_single_clock_warning_and_expiry_boundaries():
    sink = MemorySink()
    item = output()
    sink.put(item, NOW)
    result = sink.collect(item.job.job_id, NOW)
    before = result.expires_at - EXPIRY_WARNING - timedelta(microseconds=1)
    assert sink.pending(before) == (PendingEntry(item.job, item.job_status, result.expires_at, False),)
    assert sink.collect(item.job.job_id, before).expiring_soon is False
    boundary = before + timedelta(microseconds=1)
    assert sink.pending(boundary)[0].expiring_soon is True
    assert sink.collect(item.job.job_id, boundary).expiring_soon is True
    assert sink.purge_expired(result.expires_at - timedelta(microseconds=1)) == ()
    assert sink.purge_expired(result.expires_at) == (item.job,)
    assert sink.purge_expired(result.expires_at) == ()
    assert sink.collect(item.job.job_id, result.expires_at) is None
    assert sink.state(item.job.job_id, result.expires_at) == "expired"


def test_returning_host_discovers_outcomes_without_job_ids_and_pending_order():
    sink = MemorySink()
    jobs = [replace(output(), job=JobRef(name, i, None))
            for i, name in enumerate(["z", "b", "a", "deleted", "acked"], 1)]
    for item in jobs:
        sink.put(item, NOW)
    sink.extend("a", NOW + RETENTION + timedelta(days=1), NOW)
    sink.delete("deleted", NOW)
    sink.acknowledge("acked", sink.collect("acked", NOW).receipt, NOW)
    assert [r.job.job_id for r in sink.pending(NOW)] == ["b", "z", "a"]
    assert sink.purge_expired(NOW + RETENTION) == (jobs[1].job, jobs[0].job)
    records = sink.outcomes(NOW + RETENTION, since=NOW)
    assert [r.job.job_id for r in records] == ["acked", "deleted", "b", "z"]
    assert {f.name for f in fields(records[0])} == {"job", "state", "ended_at"}
    with pytest.raises(FrozenInstanceError):
        records[0].state = "deleted"
    assert [r.job.job_id for r in sink.pending(NOW + RETENTION)] == ["a"]


@pytest.mark.parametrize("method", [
    "put", "collect", "acknowledge", "extend", "delete", "state", "pending", "purge_expired", "outcomes",
])
@pytest.mark.parametrize("invalid", [None, "now", datetime(2026, 10, 5),
                                      datetime(2026, 10, 5, tzinfo=timezone(timedelta(hours=2)))])
def test_every_sink_method_requires_explicit_utc_now(method, invalid):
    args = {"put": (output(),), "collect": ("missing",),
            "acknowledge": ("missing", "receipt"), "extend": ("missing", NOW + RETENTION),
            "delete": ("missing",), "state": ("missing",)}
    kwargs = {"since": NOW} if method == "outcomes" else {}
    with pytest.raises(ValueError):
        getattr(MemorySink(), method)(*args.get(method, ()), invalid, **kwargs)


def test_outcome_and_since_validation():
    with pytest.raises(ValueError):
        JobOutcome(output().job, "pending", NOW)
    with pytest.raises(ValueError):
        JobOutcome(output().job, "deleted", NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        MemorySink().outcomes(NOW, since=NOW.replace(tzinfo=None))


def test_put_rejects_time_before_output_end():
    sink = MemorySink()
    with pytest.raises(ValueError, match="output.ended_at"):
        sink.put(output(), output().ended_at - timedelta(microseconds=1))
    assert sink.entries == {}


def test_identical_content_has_independent_random_receipts():
    item = output()
    first, second = MemorySink(), MemorySink()
    assert first.put(item, NOW) and second.put(item, NOW)
    assert first.collect(item.job.job_id, NOW).receipt != second.collect(item.job.job_id, NOW).receipt
    other = replace(item, job=JobRef("other-job", 2, None))
    assert first.put(other, NOW)
    assert first.collect(item.job.job_id, NOW).receipt != first.collect(other.job.job_id, NOW).receipt


@pytest.mark.parametrize("query", ["pending", "collect", "state", "outcomes"])
def test_queries_materialize_expiry_before_host_advances_since(query):
    sink = MemorySink()
    item = output()
    sink.put(item, NOW)
    expiry = NOW + RETENTION
    query_time = expiry + timedelta(hours=1)
    # No purge/poll occurred between put and the returning Host's first query.
    if query == "pending":
        assert sink.pending(query_time) == ()
    elif query == "collect":
        assert sink.collect(item.job.job_id, query_time) is None
    elif query == "state":
        assert sink.state(item.job.job_id, query_time) == "expired"
    else:
        assert sink.outcomes(query_time, since=NOW) == (JobOutcome(item.job, "expired", query_time),)
    assert sink.completed[item.job.job_id] == JobOutcome(item.job, "expired", query_time)
    assert sink.outcomes(query_time, since=NOW) == (JobOutcome(item.job, "expired", query_time),)
    assert sink.outcomes(query_time + timedelta(hours=1), since=query_time) == (
        JobOutcome(item.job, "expired", query_time),
    )


def test_purge_orders_by_expiry_before_job_id():
    sink = MemorySink()
    jobs = [replace(output(), job=JobRef(name, i, None))
            for i, name in enumerate(["a-later", "z-earlier", "b-earlier"], 1)]
    sink.put(jobs[0], NOW + timedelta(hours=1))
    for item in jobs[1:]:
        sink.put(item, NOW)
    assert sink.purge_expired(NOW + RETENTION + timedelta(hours=1)) == (
        jobs[2].job, jobs[1].job, jobs[0].job,
    )
