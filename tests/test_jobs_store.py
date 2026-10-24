# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Bounded synthetic contract and native filesystem/process tests for #493a."""

import hashlib
import itertools
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

import test_jobs_seam as contract
from test_jobs_runner import PROCESS_SECONDS, probe_interpreter
from modelquad.jobs.seam import JobRef, RETENTION, MAX_RETENTION
from modelquad.jobs.store import FileOutputStore, MAX_OUTPUT_BYTES, StoreCorrupted, OUTCOME_RETENTION, REMOVED_RETENTION
from modelquad.procutil import no_window_kwargs
from modelquad.execution.base import WorkerTaskStatus
from modelquad.jobs.seam import JobStatus

NOW = contract.NOW
output = contract.output


def _scenarios():
    names = [
        "test_extend_is_bounded_from_acceptance_and_preserves_receipt",
        "test_extend_to_default_expiry_is_noop_without_explicit_extension",
        "test_missing_job_returns_none_before_until_validation",
        "test_interrupted_job_is_put_with_reconciliation_facts",
        "test_older_mutation_time_after_query_is_visible_and_inclusive",
        "test_single_clock_warning_and_expiry_boundaries",
        "test_returning_host_discovers_outcomes_without_job_ids_and_pending_order",
        "test_every_sink_method_requires_explicit_utc_now",
        "test_identical_content_has_independent_random_receipts",
        "test_purge_orders_by_expiry_before_job_id",
    ]
    for name in names:
        fn = getattr(contract, name)
        cases = [{}]
        for mark in getattr(fn, "pytestmark", []):
            if mark.name == "parametrize":
                keys = mark.args[0].split(",")
                values = mark.args[1]
                cases = [dict(case, **dict(zip(keys, value if len(keys) > 1 else (value,))))
                         for case, value in itertools.product(cases, values)]
        for case in cases:
            yield pytest.param(fn, case, id=name + str(case))


@pytest.mark.parametrize("scenario,arguments", list(_scenarios()))
def test_reviewed_fake_sink_scenarios(tmp_path, monkeypatch, scenario, arguments):
    # Reuse the reviewed behavioral scenarios with a fresh on-disk sink for
    # each factory invocation. In-memory identity/internal-container tests
    # have equivalent disk-specific assertions below.
    counter = itertools.count()
    monkeypatch.setattr(contract, "MemorySink", lambda: FileOutputStore(tmp_path / str(next(counter))))
    scenario(**arguments)


def test_repeatable_collection_and_acknowledgment(tmp_path):
    store = FileOutputStore(tmp_path)
    item = output()
    job_id = item.job.job_id
    assert store.state("missing", NOW) is None
    assert store.collect("missing", NOW) is None
    assert not store.acknowledge("missing", "wrong", NOW)
    assert store.put(item, NOW)
    first = store.collect(job_id, NOW)
    assert first.output == item
    assert not store.put(replace(item), NOW)
    assert FileOutputStore(tmp_path).collect(job_id, NOW) == first
    assert not store.acknowledge(job_id, "wrong", NOW)
    assert store.collect(job_id, NOW) == first
    assert store.acknowledge(job_id, first.receipt, NOW)
    assert FileOutputStore(tmp_path).acknowledge(job_id, first.receipt, NOW)
    assert not store.acknowledge(job_id, "wrong", NOW)


@pytest.mark.parametrize("finish", ["delete", "expire", "acknowledge", "delete_all"])
def test_removal_never_retains_content_or_fingerprint(tmp_path, finish):
    store = FileOutputStore(tmp_path)
    item = replace(output(), output_text="private-output-canary", changed_files=("src/private.py",))
    store.put(item, NOW)
    result = store.collect(item.job.job_id, NOW)
    now = NOW
    if finish == "expire":
        now += RETENTION
        assert store.purge_expired(now) == (item.job,)
    elif finish == "acknowledge":
        assert store.acknowledge(item.job.job_id, result.receipt, now)
    elif finish == "delete_all":
        assert store.delete_all(now) == 1
        assert store.delete_all(now) == 0
    else:
        assert store.delete(item.job.job_id, now)
    expected = {"expire": "expired", "delete_all": "deleted"}.get(finish, "acknowledged" if finish == "acknowledge" else "deleted")
    store = FileOutputStore(tmp_path)
    assert store.state(item.job.job_id, now) == expected
    assert store.collect(item.job.job_id, now) is None
    assert store.truncation(item.job.job_id, now) is None
    assert store.extend(item.job.job_id, "invalid", now) is None
    assert not store.delete(item.job.job_id, now)
    assert not store.put(replace(item, output_text="different"), now)
    assert store.acknowledge(item.job.job_id, result.receipt, now) is (finish == "acknowledge")
    assert store.pending(now) == ()
    assert not list(tmp_path.glob("*.json"))
    remaining = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    for forbidden in (item.output_text, "src/private.py", hashlib.sha256(item.output_text.encode()).hexdigest()):
        assert forbidden.encode() not in remaining


def test_delete_all_and_expired_entries(tmp_path):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    other = replace(output(), job=JobRef("other", 2, None))
    store.put(other, NOW + timedelta(days=1))
    # Count every content file removed by this call, including expired files.
    assert store.delete_all(NOW + RETENTION) == 2
    assert store.state(output().job.job_id, NOW + RETENTION) == "expired"
    assert store.state("other", NOW + RETENTION) == "deleted"


@pytest.mark.parametrize("text", ["x" * MAX_OUTPUT_BYTES, "א" * (MAX_OUTPUT_BYTES // 2 + 1), "€" * (MAX_OUTPUT_BYTES // 3 + 1)], ids=["at-cap", "hebrew", "three-byte"])
def test_utf8_cap_and_full_duplicate_detection(tmp_path, text):
    store = FileOutputStore(tmp_path)
    item = replace(output(), output_text=text)
    assert store.put(item, NOW)
    result = store.collect(item.job.job_id, NOW)
    encoded = text.encode()
    assert result.output.output_text == encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    assert len(result.output.output_text.encode()) <= MAX_OUTPUT_BYTES
    assert result.output.worker_claim == item.worker_claim
    truncation = store.truncation(item.job.job_id, NOW)
    assert truncation.original_bytes == len(encoded)
    assert truncation.truncated is (len(encoded) > MAX_OUTPUT_BYTES)
    assert not store.put(item, NOW)
    with pytest.raises(ValueError, match="conflicting"):
        store.put(replace(item, output_text=text + "extra"), NOW)


@pytest.mark.parametrize("field,value", [
    ("partial", True), ("claim_reason", "different"), ("changed_files", ("src/a.py",)),
    ("job_status", JobStatus.FAILED), ("worker_claim", WorkerTaskStatus.COMPLETED),
    ("tree_confirmed_dead", True), ("termination", {"confirmed_dead": True}),
    ("descendants", {"enumerated": True}), ("job", JobRef("opaque-job-a", 99, None)),
    ("started_at", output().started_at - timedelta(seconds=1)),
    ("ended_at", output().ended_at + timedelta(seconds=1)),
])
def test_duplicate_compares_nontext_fields(tmp_path, field, value):
    store = FileOutputStore(tmp_path)
    item = output()
    store.put(item, NOW)
    with pytest.raises(ValueError, match="conflicting"):
        store.put(replace(item, **{field: value}), NOW)


def test_validation_since_and_extra_methods(tmp_path):
    store = FileOutputStore(tmp_path)
    with pytest.raises(ValueError, match="output.ended_at"):
        store.put(output(), output().ended_at - timedelta(seconds=1))
    assert store.state(output().job.job_id, NOW) is None
    with pytest.raises(ValueError):
        store.outcomes(NOW, since=NOW.replace(tzinfo=None))
    assert store.outcomes(NOW, since=NOW + timedelta(seconds=1)) == ()
    for method, args in [(store.delete_all, ()), (store.truncation, ("missing",))]:
        with pytest.raises(ValueError):
            method(*args, NOW.replace(tzinfo=None))


def test_durable_watermark(tmp_path):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    future = NOW + timedelta(seconds=1)
    store.outcomes(future, since=NOW)
    store = FileOutputStore(tmp_path)
    store.delete(output().job.job_id, NOW)
    assert store.outcomes(future, since=future)[0].ended_at == future


def test_submillisecond_served_time_survives_earlier_clock_write(tmp_path):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    receipt = store.collect(output().job.job_id, NOW).receipt
    served = NOW + timedelta(microseconds=500)
    assert store.outcomes(served, since=NOW) == ()
    earlier = NOW + timedelta(microseconds=200)
    reopened = FileOutputStore(tmp_path)
    assert reopened.acknowledge(output().job.job_id, receipt, earlier)
    events = FileOutputStore(tmp_path).outcomes(served + timedelta(microseconds=1), since=served)
    assert len(events) == 1 and events[0].ended_at == max(earlier, served)


@pytest.mark.parametrize("query", ["pending", "collect", "state", "outcomes", "truncation"])
def test_every_read_enforces_expiry(tmp_path, query):
    store = FileOutputStore(tmp_path)
    item = output()
    store.put(item, NOW)
    when = NOW + RETENTION
    if query == "outcomes":
        assert store.outcomes(when, since=NOW)[0].state == "expired"
    elif query == "pending":
        assert store.pending(when) == ()
    else:
        assert getattr(store, query)(item.job.job_id, when) == ("expired" if query == "state" else None)
    assert not list(tmp_path.glob("*.json"))
    event = FileOutputStore(tmp_path).outcomes(when, since=when)[0]
    assert event.job == item.job and event.ended_at == when


CHILD = '''
import os, signal, sys
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from modelquad.execution.base import WorkerTaskStatus
from modelquad.jobs.seam import JobOutput, JobRef, JobStatus
from modelquad.jobs.store import FileOutputStore
now = datetime(2026, 10, 5, 2, 0, 1, tzinfo=timezone.utc)
item = JobOutput(JobRef("opaque-job-a", 1, "decision-a"), JobStatus.FINISHED,
                 WorkerTaskStatus.UNKNOWN, None, "available answer", False,
                 None, None, None, now - timedelta(hours=2, seconds=1),
                 now - timedelta(hours=2))
store = FileOutputStore(sys.argv[1])
operation, stage = sys.argv[2:4]
receipt = store.collect(item.job.job_id, now).receipt if operation == "acknowledge" else None
def crash(at):
    if at == stage:
        print("checkpoint:" + at, flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
store._checkpoint = crash
if operation == "put":
    store.put(item, now)
elif operation == "extend":
    store.extend(item.job.job_id, now + timedelta(days=30), now)
elif operation == "acknowledge":
    store.acknowledge(item.job.job_id, receipt, now)
elif operation == "purge_expired":
    store.purge_expired(now + timedelta(days=7))
elif operation == "delete_all":
    store.delete_all(now)
elif operation == "watermark":
    store.pending(now + timedelta(seconds=1))
else:
    getattr(store, operation)(item.job.job_id, now)
'''


@pytest.mark.parametrize("operation", ["put", "delete", "extend", "acknowledge", "purge_expired", "delete_all", "watermark"])
@pytest.mark.parametrize("stage", ["staged", "journaled", "applied"])
def test_process_death_transaction_recovery(tmp_path, operation, stage):
    if operation not in {"put", "extend"} and stage == "staged":
        pytest.skip("this operation does not stage content")
    store = FileOutputStore(tmp_path)
    if operation != "put":
        store.put(output(), NOW)
    # put() has returned and released its byte-range lock before spawning.
    # A timeout remains a failure; 60 s allows slow service-profile startup.
    probe_interpreter(sys.executable)
    child = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path), operation, stage],
                           timeout=PROCESS_SECONDS, capture_output=True, **no_window_kwargs())
    assert child.returncode != 0, child.stderr.decode(errors="replace")
    assert child.stdout.strip() == ("checkpoint:" + stage).encode(), child.stderr.decode(errors="replace")
    for path in tmp_path.glob("*.json"):
        assert "output" in json.loads(path.read_text(encoding="utf-8"))
    recovered = FileOutputStore(tmp_path)
    if stage == "journaled" and operation in {"put", "extend"}:
        # Opening cannot allocate a close record. Keep the journaled staging
        # file hidden until a subsequent operation can finish the transaction.
        assert len(list(tmp_path.glob("*.tmp"))) == 1
    else:
        assert not list(tmp_path.glob("*.tmp"))
    if operation == "put":
        result = recovered.collect(output().job.job_id, NOW)
        assert (result is None) is (stage == "staged")
        if result:
            assert result.output == output()
    elif operation in {"delete", "acknowledge", "delete_all", "purge_expired"}:
        expected = {"acknowledge": "acknowledged", "purge_expired": "expired"}.get(operation, "deleted")
        assert recovered.state(output().job.job_id, NOW) == expected
        assert recovered.collect(output().job.job_id, NOW) is None
    elif operation == "watermark":
        recovered.delete(output().job.job_id, NOW)
        future = NOW + timedelta(seconds=1)
        assert recovered.outcomes(future, since=future)[0].ended_at == future
    else:
        result = recovered.collect(output().job.job_id, NOW)
        assert result.expires_at == NOW + (RETENTION if stage == "staged" else MAX_RETENTION)
    assert not list(tmp_path.glob("*.tmp"))  # Successful maintenance completes replay.


def test_two_processes_put_and_acknowledge(tmp_path):
    code = CHILD.split('store = FileOutputStore')[0] + '''
import time
from pathlib import Path
store = FileOutputStore(sys.argv[1])
root = Path(sys.argv[1])
identity = sys.argv[2]
def wait_for(condition):
    deadline = time.monotonic() + BARRIER_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "barrier timed out"
        time.sleep(0.01)
(root / (identity + ".ready")).touch()
wait_for(lambda: (root / "go").exists())
accepted = store.put(item, now)
result = store.collect(item.job.job_id, now)
assert result is not None
(root / (identity + ".collected")).touch()
wait_for(lambda: all((root / (i + ".collected")).exists() for i in ("0", "1")))
assert store.acknowledge(item.job.job_id, result.receipt, now)
assert store.acknowledge(item.job.job_id, result.receipt, now)
assert not store.put(item, now)
assert store.state(item.job.job_id, now) == "acknowledged"
print(accepted)
'''
    probe_interpreter(sys.executable)
    code = code.replace('BARRIER_SECONDS', str(PROCESS_SECONDS))
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window_kwargs()) for i in range(2)]
    try:
        deadline = time.monotonic() + PROCESS_SECONDS
        while not all((tmp_path / (str(i) + ".ready")).exists() for i in range(2)):
            assert time.monotonic() < deadline, "children did not reach barrier"
            time.sleep(0.01)
        (tmp_path / "go").touch()
        accepted = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=PROCESS_SECONDS)
            assert process.returncode == 0, stderr.decode(errors="replace")
            accepted.append(stdout.strip())
        assert sorted(accepted) == [b"False", b"True"]
    except (AssertionError, subprocess.TimeoutExpired) as error:
        diagnostics = []
        for process in processes:
            if process.poll() is None:
                process.kill()
            _, stderr = process.communicate(timeout=PROCESS_SECONDS)
            diagnostics.append(stderr.decode(errors="replace"))
        raise AssertionError(f"{error}; child stderr: {diagnostics}") from error
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
    store = FileOutputStore(tmp_path)
    assert len(store.outcomes(NOW, since=NOW)) == 1
    assert not list(tmp_path.glob("*.json"))


def test_torn_journal_tail_and_stray_temp(tmp_path):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    with (tmp_path / "outcomes.jsonl").open("ab") as stream:
        stream.write(b'{"kind":')
    (tmp_path / "stray.tmp").write_text("unfinished", encoding="utf-8")
    assert FileOutputStore(tmp_path).collect(output().job.job_id, NOW).output == output()
    assert not list(tmp_path.glob("*.tmp"))


def test_paths_factory_and_no_writes_outside_root(tmp_path, monkeypatch):
    from modelquad import paths
    from modelquad.jobs.store import default_store

    root = tmp_path / "state"
    monkeypatch.setattr(paths, "state_root", lambda: root)
    assert paths.pending_output_dir() == root / "pending-output"
    store = default_store()
    item = replace(output(), job=JobRef("../../escape/C:\\elsewhere", 1, None))
    store.put(item, NOW)
    assert store.collect(item.job.job_id, NOW).output == item
    assert all(store.root in path.parents for path in tmp_path.rglob("*") if path.is_file())
    assert {path.name for path in store.root.iterdir()} == {
        # W04: the durable removal-intent journal lives in the store root too.
        "store.lock", "outcomes.jsonl", "removal.intent", store._filename(item.job.job_id)}


def test_private_permissions(tmp_path):
    store = FileOutputStore(tmp_path / "store")
    store.put(output(), NOW)
    if os.name == "nt":
        from modelquad.service.auth import current_user_sid, describe_path, directory_is_private

        assert directory_is_private(describe_path(store.root), current_user_sid())
        # Read-only native ACL inspection, where the command is available.
        executable = shutil.which("icacls")
        if executable:
            check = subprocess.run([executable, str(store.root)], capture_output=True, timeout=5, **no_window_kwargs())
            assert check.returncode == 0, check.stderr.decode(errors="replace")
            # Inspect the actual ACL output, independent of describe_path.
            # /findsid uses a numeric SID, avoiding localized account names.
            acl = check.stdout.decode(errors="replace")
            assert "(OI)(CI)(F)" in acl
            assert "(I)" not in acl  # root must not inherit a wider parent ACL
            found = subprocess.run([executable, str(store.root), "/findsid", "*" + current_user_sid()],
                                   capture_output=True, timeout=5, **no_window_kwargs())
            assert found.returncode == 0, found.stderr.decode(errors="replace")
            assert str(store.root).lower() in found.stdout.decode(errors="replace").lower()
        for path in store.root.iterdir():
            assert all(ace[2] in {current_user_sid(), "S-1-5-18"} for ace in describe_path(path).aces)
    else:
        assert store.root.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in store.root.iterdir())


def test_repeated_idle_reads_write_nothing(tmp_path):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    journal = tmp_path / "outcomes.jsonl"
    before = journal.read_bytes(), journal.stat().st_mtime_ns
    # The property does not depend on the count; 200 reads show it as well
    # as ten thousand did, in a fraction of the time.
    for index in range(200):
        assert store.pending(NOW - timedelta(microseconds=index))
    assert (journal.read_bytes(), journal.stat().st_mtime_ns) == before


def test_seconds_apart_polls_are_bounded_by_compaction(tmp_path, monkeypatch):
    import modelquad.jobs.store as module

    monkeypatch.setattr(module, "JOURNAL_MAX_LINES", 40)
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    journal = tmp_path / "outcomes.jsonl"
    snapshots = 0
    for index in range(1, 151):
        now = NOW + timedelta(seconds=3 * index)
        assert store.pending(now)
        lines = journal.read_bytes().splitlines()
        assert len(lines) <= module.JOURNAL_MAX_LINES
        assert journal.stat().st_size <= module.JOURNAL_MAX_BYTES
        snapshots += len(lines) == 1
    assert snapshots > 1
    reopened = FileOutputStore(tmp_path)
    reopened.delete(output().job.job_id, NOW)
    assert reopened.outcomes(now, since=now)[0].ended_at == now


@pytest.mark.parametrize("bound", ["JOURNAL_MAX_LINES", "JOURNAL_MAX_BYTES"])
def test_compaction_preserves_watermark_outcomes_and_pending(tmp_path, monkeypatch, bound):
    import modelquad.jobs.store as module

    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    other = replace(output(), job=JobRef("keep-pending", 2, None))
    store.put(other, NOW)
    receipt = store.collect(output().job.job_id, NOW).receipt
    store.acknowledge(output().job.job_id, receipt, NOW)
    monkeypatch.setattr(module, bound, 1)
    future = NOW + timedelta(seconds=1)
    store.pending(future)
    records = [json.loads(line) for line in (tmp_path / "outcomes.jsonl").read_bytes().splitlines()]
    assert len(records) == 1 and records[0]["kind"] == "snapshot"
    assert records[0]["watermark"] == future.isoformat()
    reopened = FileOutputStore(tmp_path)
    assert reopened.acknowledge(output().job.job_id, receipt, NOW)
    assert reopened.collect("keep-pending", NOW).output == other
    reopened.delete("keep-pending", NOW)
    assert reopened.outcomes(future, since=future)[0].ended_at == future


@pytest.mark.parametrize("stage", ["snapshot_staged", "compacted"])
def test_process_death_during_compaction(tmp_path, stage):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    removed = replace(output(), job=JobRef("removed", 2, None))
    store.put(removed, NOW)
    receipt = store.collect("removed", NOW).receipt
    store.acknowledge("removed", receipt, NOW)
    code = CHILD.split('if operation == "put":')[0] + '''
import modelquad.jobs.store as module
module.JOURNAL_MAX_LINES = 1
store.pending(now + timedelta(seconds=1))
'''
    probe_interpreter(sys.executable)
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), "compact", stage],
                           timeout=PROCESS_SECONDS, capture_output=True, **no_window_kwargs())
    assert child.returncode != 0, child.stderr.decode(errors="replace")
    assert child.stdout.strip() == ("checkpoint:" + stage).encode(), child.stderr.decode(errors="replace")
    reopened = FileOutputStore(tmp_path)
    assert reopened.collect(output().job.job_id, NOW).output == output()
    assert reopened.acknowledge("removed", receipt, NOW)
    assert reopened.collect("removed", NOW) is None
    assert not list(tmp_path.glob("*.tmp"))
    if stage == "compacted":
        reopened.delete(output().job.job_id, NOW)
        future = NOW + timedelta(seconds=1)
        assert reopened.outcomes(future, since=future)[0].ended_at == future


def test_outcomes_are_dropped_thirty_days_after_event(tmp_path):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    receipt = store.collect(output().job.job_id, NOW).receipt
    store.acknowledge(output().job.job_id, receipt, NOW)
    boundary = NOW + OUTCOME_RETENTION
    assert store.state(output().job.job_id, boundary - timedelta(microseconds=1)) == "acknowledged"
    assert store.state(output().job.job_id, boundary) is None
    assert not store.acknowledge(output().job.job_id, receipt, boundary)
    assert store.outcomes(boundary, since=NOW) == ()
    journal = (tmp_path / "outcomes.jsonl").read_bytes()
    for value in (receipt, output().job.job_id, output().job.decision_id):
        assert value.encode() not in journal
    assert FileOutputStore(tmp_path).state(output().job.job_id, boundary) is None


@pytest.mark.parametrize("finish", ["delete", "expire", "acknowledge", "delete_all"])
def test_removed_job_hash_blocks_replay_after_outcome_expiry(tmp_path, finish):
    store = FileOutputStore(tmp_path)
    item = output()
    store.put(item, NOW)
    receipt = store.collect(item.job.job_id, NOW).receipt
    event = NOW
    if finish == "expire":
        event += RETENTION
        store.purge_expired(event)
    elif finish == "acknowledge":
        store.acknowledge(item.job.job_id, receipt, event)
    elif finish == "delete_all":
        store.delete_all(event)
    else:
        store.delete(item.job.job_id, event)
    boundary = event + OUTCOME_RETENTION
    assert store.outcomes(boundary, since=event) == ()
    guard_end = boundary + REMOVED_RETENTION
    reopened = FileOutputStore(tmp_path)
    assert not reopened.put(replace(item, output_text="replayed content"), boundary)
    assert not reopened.put(item, guard_end - timedelta(microseconds=1))
    journal = (tmp_path / "outcomes.jsonl").read_bytes()
    assert hashlib.sha256(item.job.job_id.encode()).hexdigest().encode() in journal
    assert item.job.job_id.encode() not in journal
    assert receipt.encode() not in journal
    assert reopened.collect(item.job.job_id, boundary) is None
    assert FileOutputStore(tmp_path).put(item, guard_end)


@pytest.mark.parametrize("damage", ["json", "utf8", "missing-key", "bad-time", "bad-job"])
@pytest.mark.parametrize("journal_missing", [False, True])
def test_corrupt_content_reports_typed_error_and_delete_all_recovers(tmp_path, damage, journal_missing):
    store = FileOutputStore(tmp_path)
    acknowledged = replace(output(), job=JobRef("already-acknowledged", 9, None))
    store.put(acknowledged, NOW)
    receipt = store.collect(acknowledged.job.job_id, NOW).receipt
    assert store.acknowledge(acknowledged.job.job_id, receipt, NOW)
    store.put(output(), NOW)
    path = tmp_path / store._filename(output().job.job_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if damage == "json":
        path.write_bytes(b"not json")
    elif damage == "utf8":
        path.write_bytes(b"\xff")
    else:
        if damage == "missing-key":
            del raw["receipt"]
        elif damage == "bad-time":
            raw["retained_at"] = "invalid"
        else:
            raw["output"]["job"]["job_id"] = None
        path.write_text(json.dumps(raw), encoding="utf-8")
    if journal_missing:
        (tmp_path / "outcomes.jsonl").unlink()
    reopened = FileOutputStore(tmp_path)
    with pytest.raises(StoreCorrupted, match="invalid pending-output content"):
        reopened.pending(NOW)
    assert reopened.delete_all(NOW) == 1
    assert reopened.pending(NOW) == ()
    assert not list(tmp_path.glob("*.corrupt-*"))
    if not journal_missing:
        assert reopened.acknowledge(acknowledged.job.job_id, receipt, NOW)
        events = reopened.outcomes(NOW, since=NOW)
        assert any(event.job == acknowledged.job and event.state == "acknowledged" for event in events)
    if damage in {"missing-key", "bad-time"}:
        assert reopened.state(output().job.job_id, NOW) == "deleted"
        assert not reopened.put(output(), NOW)
    assert not list(tmp_path.glob("*.json")) and not list(tmp_path.glob("*.tmp"))
    if not journal_missing:
        assert not reopened.put(acknowledged, NOW + OUTCOME_RETENTION)


def test_content_only_recovery_preserves_preexisting_removed_hashes(tmp_path):
    store = FileOutputStore(tmp_path)
    removed = replace(output(), job=JobRef("old-removed-job", 2, None))
    store.put(removed, NOW)
    store.delete(removed.job.job_id, NOW)
    later = NOW + OUTCOME_RETENTION
    assert store.state(removed.job.job_id, later) is None
    store.put(output(), later)
    path = tmp_path / store._filename(output().job.job_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["receipt"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert FileOutputStore(tmp_path).delete_all(later) == 1
    assert not FileOutputStore(tmp_path).put(removed, later)
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("stage", ["journaled", "operation_applied"])
def test_corrupt_journal_cleanup_survives_another_process_death(tmp_path, stage):
    store = FileOutputStore(tmp_path)
    jobs = [replace(output(), job=JobRef(f"job-{index}", index, None)) for index in range(3)]
    for item in jobs:
        store.put(item, NOW)
    with (tmp_path / "outcomes.jsonl").open("ab") as stream:
        stream.write(b"corrupt\n")
    os.utime(tmp_path / "outcomes.jsonl", (NOW.timestamp(), NOW.timestamp()))
    probe_interpreter(sys.executable)
    child = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path), "delete_all", stage],
                           timeout=PROCESS_SECONDS, capture_output=True, **no_window_kwargs())
    assert child.returncode != 0, child.stderr.decode(errors="replace")
    assert child.stdout.strip() == ("checkpoint:" + stage).encode(), child.stderr.decode(errors="replace")
    reopened = FileOutputStore(tmp_path)
    assert reopened.pending(NOW) == ()
    assert {event.job for event in reopened.outcomes(NOW, since=NOW)} == {item.job for item in jobs}
    for item in jobs:
        assert not reopened.put(item, NOW + OUTCOME_RETENTION)
    assert not list(tmp_path.glob("*.json")) and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("operation", ["conflict", "bad-extension"])
def test_expiry_commits_even_when_operation_raises(tmp_path, operation):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    surviving = replace(output(), job=JobRef("surviving", 2, None))
    store.put(surviving, NOW + timedelta(days=1))
    when = NOW + RETENTION
    with pytest.raises(ValueError):
        if operation == "conflict":
            store.put(replace(surviving, output_text="conflict"), when)
        else:
            store.extend("surviving", when, when)
    reopened = FileOutputStore(tmp_path)
    assert reopened.outcomes(when + timedelta(hours=1), since=when)[0].ended_at == when
    assert reopened.state(output().job.job_id, when) == "expired"


def test_mapping_keys_normalized_before_identical_retry(tmp_path):
    store = FileOutputStore(tmp_path)
    item = replace(output(), termination={1: {2: [3.5]}}, descendants={False: "bounded"})
    assert store.put(item, NOW)
    assert not store.put(item, NOW)
    reopened = FileOutputStore(tmp_path)
    assert not reopened.put(item, NOW)
    result = reopened.collect(item.job.job_id, NOW)
    assert result.output.termination == {"1": {"2": (3.5,)}}
    assert result.output.descendants == {"False": "bounded"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["termination", "descendants"])
def test_nonfinite_floats_rejected_before_comparison(tmp_path, value, field):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    with pytest.raises(ValueError, match="finite floats"):
        store.put(replace(output(), **{field: {1: {"nested": [value]}}}), NOW)
    assert not store.put(output(), NOW)


def test_normalized_key_collision_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="keys collide"):
        FileOutputStore(tmp_path).put(replace(output(), termination={1: "a", "1": "b"}), NOW)


@pytest.mark.parametrize("text", ["answer\udcff\ud800tail", "x" * (MAX_OUTPUT_BYTES - 2) + "\udcfftail"], ids=["round-trip", "cap-boundary"])
def test_surrogates_round_trip_and_truncate_on_character_boundary(tmp_path, text):
    store = FileOutputStore(tmp_path)
    item = replace(output(), output_text=text)
    store.put(item, NOW)
    assert not store.put(item, NOW)
    result = FileOutputStore(tmp_path).collect(item.job.job_id, NOW).output.output_text
    expected = text if len(text.encode("utf-8", "surrogatepass")) <= MAX_OUTPUT_BYTES else "x" * (MAX_OUTPUT_BYTES - 2)
    assert result == expected
    assert len(result.encode("utf-8", "surrogatepass")) <= MAX_OUTPUT_BYTES


@pytest.mark.parametrize("line", [b"not-json\n", b'{}\n', b'{"kind":"transaction","watermark":0}\n'])
def test_corrupt_journal_reports_typed_error_and_delete_all_recovers(tmp_path, line):
    store = FileOutputStore(tmp_path)
    for index in range(3):
        store.put(replace(output(), job=JobRef(f"job-{index}", index, None)), NOW)
    (tmp_path / "stray.tmp").write_bytes(b"private staging content")
    journal = tmp_path / "outcomes.jsonl"
    with journal.open("ab") as stream:
        stream.write(line)
    os.utime(journal, (NOW.timestamp(), NOW.timestamp()))
    corrupt = journal.read_bytes()
    reopened = FileOutputStore(tmp_path)
    with pytest.raises(StoreCorrupted, match="invalid pending-output journal"):
        reopened.pending(NOW)
    assert reopened.delete_all(NOW) == 3
    assert reopened.pending(NOW) == ()
    archives = list(tmp_path.glob("*.corrupt-*"))
    assert len(archives) == 1 and archives[0].read_bytes() == corrupt
    assert not list(tmp_path.glob("*.json")) and not list(tmp_path.glob("*.tmp"))
    assert {event.job.job_id for event in reopened.outcomes(NOW, since=NOW)} == {
        f"job-{index}" for index in range(3)}
    for index in range(3):
        assert reopened.state(f"job-{index}", NOW) == "deleted"
        assert not reopened.put(replace(output(), job=JobRef(f"job-{index}", index, None)), NOW)
    assert reopened.put(output(), NOW)
    assert FileOutputStore(tmp_path).collect(output().job.job_id, NOW).output == output()


@pytest.mark.parametrize("bound_source", ["prefix", "mtime", "now"])
def test_corrupt_journal_recovery_watermark_uses_all_lower_bounds(tmp_path, bound_source):
    store = FileOutputStore(tmp_path)
    store.put(output(), NOW)
    times = {name: NOW + timedelta(seconds=index + 1)
             for index, name in enumerate(("prefix", "mtime", "now"))}
    times[bound_source] = NOW + timedelta(seconds=10)
    store.pending(times["prefix"])
    journal = tmp_path / "outcomes.jsonl"
    with journal.open("ab") as stream:
        # A damaged middle record hides later, possibly higher watermarks.
        stream.write(b"corrupt\n")
        stream.write((json.dumps({"kind": "snapshot", "watermark":
                                 (NOW + timedelta(seconds=20)).isoformat(),
                                 "outcomes": []}) + "\n").encode())
    os.utime(journal, (times["mtime"].timestamp(), times["mtime"].timestamp()))
    assert FileOutputStore(tmp_path).delete_all(times["now"]) == 1
    watermark = max(times.values())
    events = FileOutputStore(tmp_path).outcomes(watermark, since=watermark)
    assert len(events) == 1 and events[0].ended_at == watermark


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "directory"])
@pytest.mark.parametrize("corrupt_journal", [False, True])
def test_recovery_skips_unsafe_files_without_repeated_archives(tmp_path, caplog, unsafe_kind, corrupt_journal):
    root = tmp_path / "store"
    store = FileOutputStore(root)
    store.put(output(), NOW)
    unsafe_job = replace(output(), job=JobRef("unsafe-job", 2, None))
    store.put(unsafe_job, NOW)
    external = tmp_path / "external.json"
    external.write_text("external-private-content-canary", encoding="utf-8")
    unsafe = root / store._filename(unsafe_job.job.job_id)
    unsafe.unlink()
    if unsafe_kind == "directory":
        unsafe.mkdir()
    elif unsafe_kind == "hardlink":
        os.link(external, unsafe)
    else:
        try:
            unsafe.symlink_to(external)
        except OSError:
            pytest.skip("native symlink creation unavailable")
    staging = root / "unsafe.tmp"
    staging.mkdir()
    journal = root / "outcomes.jsonl"
    if corrupt_journal:
        with journal.open("ab") as stream:
            stream.write(b"corrupt\n")
        os.utime(journal, (NOW.timestamp(), NOW.timestamp()))
    reopened = FileOutputStore(root)
    assert reopened.delete_all(NOW) == 1
    archive_count = int(corrupt_journal)
    for _ in range(3):
        assert FileOutputStore(root).delete_all(NOW) == 0
        assert len(list(root.glob("*.corrupt-*"))) == archive_count
    assert external.read_text(encoding="utf-8") == "external-private-content-canary"
    assert unsafe.exists() and staging.is_dir()
    assert unsafe.name in caplog.text and "unsafe.tmp" in caplog.text
    assert "external-private-content-canary" not in caplog.text
    assert str(external) not in caplog.text
    records = [json.loads(line) for line in journal.read_bytes().splitlines()]
    assert not any(op["file"] == unsafe.name and op["kind"] == "remove"
                   for record in records for op in record.get("operations", []))
    assert "unsafe.tmp" not in journal.read_text(encoding="utf-8")


def test_oversized_snapshot_is_not_rewritten_after_each_append(tmp_path, monkeypatch):
    import modelquad.jobs.store as module

    monkeypatch.setattr(module, "JOURNAL_MAX_BYTES", 4096)
    store = FileOutputStore(tmp_path)
    expires = (NOW + OUTCOME_RETENTION + REMOVED_RETENTION).isoformat()
    removed = {hashlib.sha256(str(index).encode()).hexdigest(): expires for index in range(100)}
    journal = tmp_path / "outcomes.jsonl"
    journal.write_text(json.dumps({"kind": "snapshot", "watermark": NOW.isoformat(),
                                   "outcomes": [], "removed": removed}) + "\n", encoding="utf-8")
    snapshots = []
    store._checkpoint = lambda stage: snapshots.append(stage) if stage == "compacted" else None
    original = journal.read_bytes()
    assert len(original) > module.JOURNAL_MAX_BYTES
    for index in range(1, 6):
        store.pending(NOW + timedelta(seconds=index))
        assert journal.read_bytes().startswith(original)
        # Reloads must remember the snapshot baseline too.
        FileOutputStore(tmp_path).pending(NOW + timedelta(seconds=index))
    assert not snapshots
    for index in range(6, 31):
        store.pending(NOW + timedelta(seconds=index))
    assert snapshots  # Accumulated appends still trigger bounded compaction.


@pytest.mark.parametrize("operation", ["delete", "extend"])
def test_inflight_recovery_skips_targets_replaced_with_unsafe_files(tmp_path, operation, caplog):
    root = tmp_path / "store"
    store = FileOutputStore(root)
    store.put(output(), NOW)

    def crash(stage):
        if stage == "journaled":
            raise RuntimeError("synthetic crash")

    store._checkpoint = crash
    with pytest.raises(RuntimeError, match="synthetic crash"):
        if operation == "delete":
            store.delete(output().job.job_id, NOW)
        else:
            store.extend(output().job.job_id, NOW + MAX_RETENTION, NOW)
    target = root / store._filename(output().job.job_id)
    target.unlink()
    external = tmp_path / "external.json"
    external.write_bytes(b"private-external-canary")
    os.link(external, target)
    reopened = FileOutputStore(root)
    assert reopened.pending(NOW) == ()
    assert reopened.delete_all(NOW) == 0
    assert external.read_bytes() == b"private-external-canary"
    assert target.exists() and not list(root.glob("*.corrupt-*"))
    assert target.name in caplog.text and "private-external-canary" not in caplog.text
    if operation == "extend":
        before = (root / "outcomes.jsonl").read_bytes()
        with pytest.raises(ValueError, match="unsafe pending-output target"):
            reopened.put(output(), NOW)
        assert (root / "outcomes.jsonl").read_bytes() == before


def test_multi_job_delete_all_recovers_after_first_unlink(tmp_path):
    store = FileOutputStore(tmp_path)
    jobs = [replace(output(), job=JobRef(f"job-{index}", index, None)) for index in range(3)]
    for item in jobs:
        store.put(item, NOW)
    probe_interpreter(sys.executable)
    child = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path), "delete_all", "operation_applied"],
                           timeout=PROCESS_SECONDS, capture_output=True, **no_window_kwargs())
    assert child.returncode != 0, child.stderr.decode(errors="replace")
    assert child.stdout.strip() == b"checkpoint:operation_applied", child.stderr.decode(errors="replace")
    assert len(list(tmp_path.glob("*.json"))) == 2
    reopened = FileOutputStore(tmp_path)
    assert reopened.pending(NOW) == ()
    assert len(reopened.outcomes(NOW, since=NOW)) == 3
    assert all(reopened.state(item.job.job_id, NOW) == "deleted" for item in jobs)
    assert not list(tmp_path.glob("*.json"))
