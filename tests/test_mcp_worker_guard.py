# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Inert MCP boundary tests: fake Windows trees and temporary history only."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp.types import CallToolRequestParams

from conftest import tool_payload
from modelquad.execution.base import WORKER_ENV_VAR
from modelquad.mcp import server as mcp_server
from modelquad.mcp import worker_guard as guard
from modelquad.service import db as service_db
from modelquad.service import history


@pytest.fixture
def tree(tmp_path, monkeypatch):
    database = tmp_path / "history.db"
    conn = service_db.connect_and_init(database)
    conn.close()
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", database)
    monkeypatch.setattr(guard, "os", SimpleNamespace(environ=os.environ, getpid=lambda: 300))
    facts = SimpleNamespace(
        entries=[(300, 200, "server.exe"), (200, 100, "node.exe"), (100, 0, "worker.exe")],
        created={300: 300.0, 200: 200.0, 100: 100.0},
        state="alive", database=database,
    )
    monkeypatch.setattr(guard.procutil, "_windows_process_snapshot", lambda: facts.entries)
    monkeypatch.setattr(guard.procutil, "process_creation_epoch", lambda pid: facts.created.get(pid))
    monkeypatch.setattr(guard.procutil, "pid_state", lambda pid: facts.state)
    return facts


def attempt(tree, *, pid=100, started=99.0, status="started", resolved=None):
    conn = sqlite3.connect(tree.database)
    try:
        conn.execute(
            "INSERT INTO delegation_attempts "
            "(created_at, task_type, status, resolved_at, worker_pid) VALUES (?, ?, ?, ?, ?)",
            (datetime.fromtimestamp(started, timezone.utc).isoformat(), "exploration", status, resolved, pid),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("layer", ["environment", "ancestry"])
def test_every_registered_tool_refuses_before_tool_work(tree, monkeypatch, layer):
    if layer == "environment":
        monkeypatch.setenv(WORKER_ENV_VAR, "1")
    else:
        attempt(tree)

    original = mcp_server.server._tool_manager.call_tool

    async def forbidden(name, *args, **kwargs):
        assert name in mcp_server.WORKER_REFUSAL_TOOLS
        return await original(name, *args, **kwargs)

    monkeypatch.setattr(mcp_server.server._tool_manager, "call_tool", forbidden)

    async def check():
        tools = await mcp_server.server.list_tools()
        names = {tool.name for tool in tools}
        assert {"modelquad_status", "modelquad_delegate", "modelquad_handoff"} <= names
        assert {"modelquad_route", "modelquad_second_opinion", "modelquad_plan"}.isdisjoint(names)
        for tool in tools:
            if tool.name in mcp_server.WORKER_REFUSAL_TOOLS:
                continue  # Existing refusal paths are checked with valid args below.
            # Missing mandatory arguments proves the guard precedes validation.
            result = await mcp_server.server.call_tool(tool.name, {})
            assert result.is_error is True
            payload = tool_payload(result)
            assert payload["refused"] is True
            assert payload["recorded"] is False
            assert payload["reason"] == guard.WORKER_REFUSAL
            # The real SDK wire handler delegates through the same boundary.
            wire = await mcp_server.server._handle_call_tool(
                Mock(), CallToolRequestParams(name=tool.name, arguments={}),
            )
            assert wire.is_error is True
            assert tool_payload(wire) == payload
        return len(tools)

    print(f"guarded registry: {asyncio.run(check())} tools ({layer})")


def test_environment_refuses_without_any_observation(tree, monkeypatch):
    monkeypatch.setenv(WORKER_ENV_VAR, "1")
    monkeypatch.setattr(guard.procutil, "_windows_process_snapshot", lambda: pytest.fail("observation"))
    assert guard.is_worker(tree.database)


@pytest.mark.parametrize("layer", ["environment", "ancestry"])
@pytest.mark.parametrize("name,arguments,reason", [
    ("modelquad_delegate", {"task_type": "review", "prompt": "synthetic", "working_directory": "."},
     "orchestration by delegating again"),
    ("modelquad_handoff", {"task_type": "review", "prompt": "synthetic", "working_directory": "."},
     "a delegated worker must never hand off work"),
])
def test_existing_refusals_keep_payload_reason_and_evidence(tree, monkeypatch, name, arguments, reason, layer):
    if layer == "environment":
        monkeypatch.setenv(WORKER_ENV_VAR, "1")
    else:
        attempt(tree)
    result = asyncio.run(mcp_server.server.call_tool(name, arguments))
    payload = tool_payload(result)
    assert payload["refused"] is True
    assert reason in payload["reason"]
    prefix = f"{WORKER_ENV_VAR}=1:" if layer == "environment" else "worker ancestry match:"
    assert payload["reason"].startswith(prefix)
    if layer == "ancestry":
        assert WORKER_ENV_VAR not in payload["reason"]
    rows = history.fetch_delegation_attempts(tree.database)
    refused = [row for row in rows if row["status"] == "refused"]
    if name == "modelquad_delegate":
        assert len(refused) == 1
        assert refused[0]["failure_kind"] == "policy_refused"
        assert refused[0]["decision_id"] == payload["decision_id"]
        assert refused[0]["detail"] == payload["reason"]
    else:
        assert refused == []  # The handoff's worker refusal writes no row.
        assert set(payload) == {"refused", "started", "reason"}
        assert payload["started"] is False
    assert not mcp_server._worker_call.get()


def test_live_unresolved_ancestor_refuses(tree):
    attempt(tree)
    assert guard.is_worker(tree.database)


@pytest.mark.parametrize("state", ["dead", "unknown"])
def test_only_positive_liveness_refuses(tree, state):
    attempt(tree)
    tree.state = state
    assert not guard.is_worker(tree.database)


def test_process_created_before_attempt_start_does_not_refuse(tree):
    attempt(tree, started=150.0)
    assert not guard.is_worker(tree.database)


def test_worker_created_after_attempt_start_refuses(tree):
    # Normal production ordering: intent first, worker spawn afterwards.
    attempt(tree, started=99.0)
    assert guard.is_worker(tree.database)


@pytest.mark.parametrize("started,refused", [
    (100.0, True), (102.0, True), (102.001, False),
])
def test_attempt_clock_tolerance_boundary(tree, started, refused):
    attempt(tree, started=started)
    assert guard.is_worker(tree.database) is refused


def test_reused_intermediate_parent_is_not_a_worker_ancestor(tree):
    attempt(tree)
    tree.created[200] = 301.0
    assert not guard.is_worker(tree.database)


def test_pid_identity_changes_during_probe_do_not_refuse(tree, monkeypatch):
    attempt(tree)
    def state(pid):
        tree.created[pid] = 101.0
        return "alive"
    monkeypatch.setattr(guard.procutil, "pid_state", state)
    assert not guard.is_worker(tree.database)


@pytest.mark.parametrize("status,resolved", [
    ("completed", None), ("refused", None), ("failed", None),
    ("started", "1970-01-01T00:03:00+00:00"),
])
def test_terminal_or_resolved_attempt_does_not_refuse(tree, status, resolved):
    attempt(tree, status=status, resolved=resolved)
    assert not guard.is_worker(tree.database)


def test_resolving_attempt_removes_refusal_on_next_call(tree):
    attempt(tree)
    assert guard.is_worker(tree.database)
    conn = sqlite3.connect(tree.database)
    conn.execute("UPDATE delegation_attempts SET status = 'completed'")
    conn.commit()
    conn.close()
    assert not guard.is_worker(tree.database)


@pytest.mark.parametrize("failure", ["missing_db", "bad_db", "snapshot", "snapshot_error", "creation"])
def test_unreadable_observation_does_not_refuse_normal_host(tree, monkeypatch, failure):
    attempt(tree)
    if failure == "missing_db":
        tree.database = tree.database.with_name("missing.db")
    elif failure == "bad_db":
        tree.database.write_bytes(b"not a database")
    elif failure == "snapshot":
        tree.entries = None
    elif failure == "snapshot_error":
        def unreadable():
            raise OSError("private path must never escape")
        monkeypatch.setattr(guard.procutil, "_windows_process_snapshot", unreadable)
    else:
        tree.created = {}
    assert not guard.is_worker(tree.database)
    if failure == "missing_db":
        assert not tree.database.exists()


def test_ancestry_depth_is_bounded(tree, monkeypatch):
    depth = guard.MAX_ANCESTOR_DEPTH
    tree.entries = [(pid, pid - 1, "fake.exe") for pid in range(300 - depth - 1, 301)]
    tree.created = {pid: float(pid) for pid, _, _ in tree.entries}
    calls = []
    def created(pid):
        calls.append(pid)
        return tree.created[pid]
    monkeypatch.setattr(guard.procutil, "process_creation_epoch", created)
    attempt(tree, pid=300 - depth - 1, started=260.0)
    assert not guard.is_worker(tree.database)
    assert len(calls) == depth + 1  # the server plus at most 32 ancestors
    attempt(tree, pid=300 - depth, started=260.0)
    assert guard.is_worker(tree.database)


def test_ancestry_cycle_terminates(tree):
    tree.entries = [(300, 200, "server"), (200, 100, "parent"), (100, 200, "cycle")]
    assert not guard.is_worker(tree.database)


def test_history_query_is_read_only_and_returns_only_open_candidates(tree):
    attempt(tree)
    attempt(tree, pid=999)
    attempt(tree, status="completed")
    before = tree.database.read_bytes()
    assert history.unresolved_worker_attempts(tree.database) == [
        (100, datetime.fromtimestamp(99, timezone.utc).isoformat()),
        (999, datetime.fromtimestamp(99, timezone.utc).isoformat()),
    ]
    assert tree.database.read_bytes() == before


def test_normal_host_guard_timing_with_inert_process_table(tree):
    # Real temporary SQLite reads, synthetic 1,000-process snapshot and an
    # eight-hop Host chain. This excludes native Windows snapshot latency.
    tree.entries = [(pid, pid - 1 if 292 < pid <= 300 else 0, "fake.exe") for pid in range(1, 1001)]
    tree.created = {pid: float(pid) for pid, _, _ in tree.entries}
    attempt(tree, pid=900, started=950.0)
    samples = []
    for _ in range(200):
        start = time.perf_counter()
        assert not guard.is_worker(tree.database)
        samples.append((time.perf_counter() - start) * 1000)
    print(
        f"Host guard inert timing (200 calls): median={statistics.median(samples):.3f} ms, "
        f"p95={sorted(samples)[189]:.3f} ms; native snapshot cost unmeasured"
    )


@pytest.mark.parametrize("created,refused", [(219.0, True), (219.001, False)])
def test_orphaned_row_reused_pid_is_bounded_by_spawn_grace(tree, created, refused):
    attempt(tree, started=99.0)
    tree.created[100] = created
    tree.created[200] = 250.0
    assert guard.is_worker(tree.database) is refused


@pytest.mark.parametrize("kind", ["none", "terminal", "resolved", "pidless"])
def test_no_open_worker_candidates_skips_snapshot(tree, monkeypatch, kind):
    if kind == "terminal":
        attempt(tree, status="completed")
    elif kind == "resolved":
        attempt(tree, resolved="1970-01-01T00:03:00+00:00")
    elif kind == "pidless":
        attempt(tree, pid=None)
    monkeypatch.setattr(guard.procutil, "_windows_process_snapshot", lambda: pytest.fail("snapshot"))
    assert not guard.is_worker(tree.database)


def test_mcp_observation_runs_off_event_loop(tree, monkeypatch):
    import threading
    loop_thread = threading.get_ident()
    observed = []

    def observe(db_path):
        observed.append(threading.get_ident())
        return True

    monkeypatch.setattr(mcp_server, "is_worker", observe)
    asyncio.run(mcp_server.server.call_tool("modelquad_status", {}))
    assert observed and observed[0] != loop_thread
