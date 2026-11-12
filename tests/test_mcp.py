# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import importlib
import json
import subprocess

import pytest

from conftest import codex_event_stream, tool_payload

from modelquad.execution import CodexExecutor, ExecutionManager
from modelquad.mcp.server import server
from routing_mode_fixtures import save_legacy_mode


@pytest.fixture(autouse=True)
def _tool_usage_directory(tmp_path, monkeypatch):
    """The MCP tools read quota snapshots from `server.USAGE_DIRECTORY`, never
    from a tool argument. This module's snapshots live in the
    test's tmp_path, which every call here used to pass as `usage_directory`."""
    from modelquad.mcp import server as mcp_server_module

    monkeypatch.setattr(mcp_server_module, "USAGE_DIRECTORY", str(tmp_path))


mcp_server = importlib.import_module("modelquad.mcp.server")


class RecordingRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        output = codex_event_stream() if "--json" in argv else "ok"
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=output, stderr="")


def write_usage_fixtures(tmp_path):
    (tmp_path / "usage-claude.json").write_text(
        json.dumps({"five_hour_used_percentage": 10, "seven_day_used_percentage": 20}),
        encoding="utf-8",
    )
    (tmp_path / "usage-codex.json").write_text(
        json.dumps(
            {
                "available": True,
                "five_hour_remaining_percentage": 80,
                "seven_day_remaining_percentage": 70,
            }
        ),
        encoding="utf-8",
    )


def _tool_payload(name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return tool_payload(result)


def test_modelquad_status_tool(tmp_path):
    write_usage_fixtures(tmp_path)

    payload = _tool_payload("modelquad_status", {})

    assert {provider["name"] for provider in payload["providers"]} == {
        "claude",
        "codex",
    }
    assert payload["summary"]


def test_modelquad_delegate_tool_uses_mocked_execution(tmp_path, monkeypatch, *, synthetic_worker_binary, qualified_codex_inventory):
    write_usage_fixtures(tmp_path)
    run = RecordingRun()

    def build_execution_manager(config):
        return ExecutionManager(
            config,
            executors={
                "codex": CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run,
                                       inventory=qualified_codex_inventory),
            },
        )

    monkeypatch.setattr(mcp_server, "_build_execution_manager", build_execution_manager)

    payload = _tool_payload(
        "modelquad_delegate",
        {
            "task_type": "review",
            "prompt": "do the thing",
            "working_directory": str(tmp_path),
            "provider": "codex",
        },
    )

    assert payload["success"] is True
    assert payload["provider"] == "codex"
    assert len(run.calls) == 1


def test_modelquad_delegate_tool_records_history_and_returns_structured_status(tmp_path, monkeypatch, *, synthetic_worker_binary, qualified_codex_inventory):
    """MCP-driven delegation previously left no trace in execution_history/
    routing_decisions at all -- confirm it now records both, and that the
    returned payload exposes the trustworthy structured signal (not just the
    legacy process-level `success` field) so a clean exit code without a
    completion envelope cannot read as task success from the MCP client's
    point of view either.
    """

    from modelquad.service import db as service_db

    write_usage_fixtures(tmp_path)
    db_path = tmp_path / "mcp-history.db"
    original_default = service_db.DEFAULT_DB_PATH
    service_db.DEFAULT_DB_PATH = db_path
    run = RecordingRun()

    def build_execution_manager(config):
        return ExecutionManager(
            config,
            executors={"codex": CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run,
                                            inventory=qualified_codex_inventory)},
        )

    monkeypatch.setattr(mcp_server, "_build_execution_manager", build_execution_manager)

    try:
        payload = _tool_payload(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": "do the thing",
                "working_directory": str(tmp_path),
                "provider": "codex",
            },
        )

        # RecordingRun's canned "ok" agent message has no completion envelope, so
        # even though the fake process exited 0, the task is not confirmed
        # complete -- exactly the process-success-vs-task-success distinction
        # this patch exists to enforce.
        assert payload["process_success"] is True
        assert payload["task_status"] == "unknown"
        assert payload["task_completed"] is False

        activity = _tool_payload("modelquad_activity", {})
        execution_entries = [entry for entry in activity["activity"] if entry["kind"] == "execution"]
        routing_entries = [entry for entry in activity["activity"] if entry["kind"] == "routing"]
        assert len(execution_entries) == 1
        assert execution_entries[0]["provider"] == "codex"
        assert execution_entries[0]["task_status"] == "unknown"
        assert execution_entries[0]["result"] == "error"
        assert len(routing_entries) == 1
    finally:
        service_db.DEFAULT_DB_PATH = original_default


def test_current_mcp_tools_share_canonical_status_and_delegation(tmp_path, monkeypatch, *, synthetic_worker_binary, qualified_codex_inventory):
    write_usage_fixtures(tmp_path)

    status = _tool_payload("modelquad_status", {})
    assert _tool_payload("modelquad_status", {}) == status
    # Removed in this release: plan metadata and the route/limits tools.
    for removed in ("plan", "plan_type", "metadata", "source_file", "memory"):
        assert removed not in status
    for provider in status["providers"]:
        assert {"plan", "plan_type", "metadata", "source_file"}.isdisjoint(provider)

    run = RecordingRun()
    monkeypatch.setattr(
        mcp_server,
        "_build_execution_manager",
        lambda config: ExecutionManager(config, executors={"codex": CodexExecutor(
            which=lambda _: synthetic_worker_binary('codex.exe'), run=run,
            inventory=qualified_codex_inventory)}),
    )
    delegated = _tool_payload(
        "modelquad_delegate",
        {
            "task_type": "review",
            "prompt": "do the thing",
            "working_directory": str(tmp_path),
        },
    )
    assert delegated["success"] is True
    assert delegated["provider"] == "codex"


def test_mcp_activity_and_doctor_tools_return_expected_shapes(tmp_path):
    from modelquad.execution import ExecutionResult
    from modelquad.service import db, history

    write_usage_fixtures(tmp_path)
    db_path = tmp_path / "activity.db"
    original_default = db.DEFAULT_DB_PATH
    db.DEFAULT_DB_PATH = db_path
    history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(provider="codex", success=True, output="", exit_code=0),
        project="project-name",
    )

    try:
        activity = _tool_payload("modelquad_activity", {})
        doctor = _tool_payload("modelquad_doctor", {})

        assert activity["activity"][0]["project"] == "project-name"
        assert "checks" in doctor and "ok" in doctor
        with pytest.raises(TypeError):
            mcp_server.modelquad_activity(db_path=str(tmp_path / "untrusted.db"))
    finally:
        db.DEFAULT_DB_PATH = original_default


@pytest.mark.parametrize("mode", ["ask", "manual"])
def test_retired_saved_routing_modes_use_automatic_policy(tmp_path, monkeypatch, mode):
    from modelquad.service import db as service_db
    from modelquad.service import history
    path = tmp_path / "legacy.db"
    save_legacy_mode(path, mode)
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", path)
    assert history.get_routing_mode(path) == "automatic"
    assert mcp_server._launch_policy(path, invocation_mode="automatic").allowed
    history.put_routing_paused(path, True)
    assert not mcp_server._launch_policy(path, user_approved=True).allowed


def test_retired_modes_cannot_be_saved(tmp_path):
    from modelquad.service import history
    for mode in ("ask", "manual"):
        with pytest.raises(ValueError, match="Routing mode must be automatic"):
            history.put_routing_mode(tmp_path / "mode.db", mode)


def test_routing_paused_enforcement(tmp_path, monkeypatch):
    from modelquad.service import db as service_db
    from modelquad.service import history as service_history

    write_usage_fixtures(tmp_path)
    db_path = tmp_path / "paused_mcp.db"
    original_default = service_db.DEFAULT_DB_PATH
    service_db.DEFAULT_DB_PATH = db_path
    service_history.put_routing_paused(db_path, True)

    def _boom_manager(config):
        raise AssertionError("_build_execution_manager must NOT be called when routing is paused")

    monkeypatch.setattr(mcp_server, "_build_execution_manager", _boom_manager)

    try:
        # 1. Automatic invocation without explicit user request -> blocked
        payload = _tool_payload(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": "do the thing",
                "working_directory": str(tmp_path),
                "invocation_mode": "automatic",
            },
        )
        assert payload["delegated"] is False
        assert payload["allowed"] is False
        assert payload["routing_paused"] is True
        assert "routing is paused" in payload["reason"]

        # Caller assertions cannot lift an absolute pause.
        explicit_payload = _tool_payload(
            "modelquad_delegate",
            {"task_type": "review", "prompt": "do the thing",
             "working_directory": str(tmp_path), "provider": "codex",
             "invocation_mode": "explicit_user_request", "user_approved": True},
        )
        assert explicit_payload["delegated"] is False
        assert explicit_payload["allowed"] is False
        assert explicit_payload["routing_paused"] is True
        assert explicit_payload["recorded"] is True
    finally:
        service_db.DEFAULT_DB_PATH = original_default


def test_delegate_expected_consumption_resolution_and_persistence(tmp_path, monkeypatch):
    import sqlite3
    from modelquad.models import ConsumptionBucket, ConsumptionSource
    from modelquad.service import db as service_db
    from modelquad.execution.base import ExecutionResult, WorkerTaskStatus

    write_usage_fixtures(tmp_path)
    db_path = tmp_path / "consumption-test.db"
    original_default = service_db.DEFAULT_DB_PATH
    service_db.DEFAULT_DB_PATH = db_path

    captured_kwargs: list[dict] = []

    class MockExecutionManager(ExecutionManager):
        def __init__(self, config):
            super().__init__(config, executors={})

        def delegate(self, task_type, prompt, working_directory, **kwargs):
            captured_kwargs.append(dict(kwargs))
            self.routed_result = self.router.route(
                task_type, kwargs['statuses'], expected_consumption=kwargs['expected_consumption'],
                required_capabilities=kwargs.get('required_capabilities'), now=kwargs.get('now'),
                catalog=kwargs.get('catalog'), policy_excluded=kwargs.get('policy_excluded'),
            )
            return ExecutionResult(
                provider="codex",
                success=True,
                output="completed",
                exit_code=0,
                task_status=WorkerTaskStatus.COMPLETED,
            )

    monkeypatch.setattr(mcp_server, "_build_execution_manager", lambda cfg: MockExecutionManager(cfg))

    try:
        # (a) The tool takes no expected_consumption argument any more; an
        # undeclared one is dropped and the capsule's bucket is used.
        assert "expected_consumption" not in server._tool_manager._tools["modelquad_delegate"].parameters["properties"]
        captured_kwargs.clear()
        _tool_payload(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": "short prompt",
                "working_directory": str(tmp_path),
                "expected_consumption": "low",
                "capsule": {
                    "goal": "capsule goal",
                    "task_type": "review",
                    "expected_consumption": "high",
                },
            },
        )
        assert len(captured_kwargs) == 1
        ev_a = captured_kwargs[0]["expected_consumption"]
        assert ev_a.bucket == ConsumptionBucket.HIGH
        assert ev_a.source == ConsumptionSource.HOST_SUPPLIED
        assert ev_a.basis == "supplied via task capsule"

        conn = sqlite3.connect(str(db_path))
        row_a = conn.execute(
            "SELECT expected_consumption_bucket, expected_consumption_source FROM routing_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row_a == ("high", "host_supplied")

        # (b) Capsule bucket used when explicit param absent
        captured_kwargs.clear()
        _tool_payload(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": "short prompt",
                "working_directory": str(tmp_path),
                "capsule": {
                    "goal": "capsule goal",
                    "task_type": "review",
                    "expected_consumption": "medium",
                },
            },
        )
        assert len(captured_kwargs) == 1
        ev_b = captured_kwargs[0]["expected_consumption"]
        assert ev_b.bucket == ConsumptionBucket.MEDIUM
        assert ev_b.source == ConsumptionSource.HOST_SUPPLIED
        assert ev_b.basis == "supplied via task capsule"

        conn = sqlite3.connect(str(db_path))
        row_b = conn.execute(
            "SELECT expected_consumption_bucket, expected_consumption_source FROM routing_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row_b == ("medium", "host_supplied")

        # (c) Neither -> estimated source "estimated" and long prompt produces "high"
        captured_kwargs.clear()
        long_prompt = "x" * 25000
        _tool_payload(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": long_prompt,
                "working_directory": str(tmp_path),
            },
        )
        assert len(captured_kwargs) == 1
        ev_c = captured_kwargs[0]["expected_consumption"]
        assert ev_c.bucket == ConsumptionBucket.HIGH
        assert ev_c.source == ConsumptionSource.ESTIMATED
        assert "estimated from" in ev_c.basis

        conn = sqlite3.connect(str(db_path))
        row_c = conn.execute(
            "SELECT expected_consumption_bucket, expected_consumption_source FROM routing_decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row_c == ("high", "estimated")

        # (d) Regression: capsule text must be counted exactly ONCE. A ~12k-char
        # capsule goal with an empty prompt sits squarely in the MEDIUM band
        # (4k-20k); double-counting (prompt embedding the rendered capsule AND
        # the capsule passed to the estimator) would push it over 20k -> HIGH.
        captured_kwargs.clear()
        _tool_payload(
            "modelquad_delegate",
            {
                "task_type": "review",
                "prompt": "",
                "working_directory": str(tmp_path),
                "capsule": {
                    "goal": "g" * 12000,
                    "task_type": "review",
                },
            },
        )
        assert len(captured_kwargs) == 1
        ev_d = captured_kwargs[0]["expected_consumption"]
        assert ev_d.source == ConsumptionSource.ESTIMATED
        assert ev_d.bucket == ConsumptionBucket.MEDIUM
    finally:
        service_db.DEFAULT_DB_PATH = original_default


# --- recorded routing evidence agrees with the route taken -----


def _routing_rows(db_path):
    from modelquad.service import history as service_history

    return [
        entry
        for entry in service_history.fetch_activity(db_path, limit=50)
        if entry["kind"] == "routing"
    ]


def _delegate_under_policy(tmp_path, monkeypatch, *, policy_refused, refused=("test_api",), synthetic_worker_binary,
                           qualified_codex_inventory):
    """Run one auto-routed MCP delegation with the controlled network policy gate.

    The policy verdict and API-provider registry are controlled; the database,
    router and recording paths remain real. A synthetic API worker registered
    through the public registry is preferred when allowed, and excluded at the
    network-policy gate whenever policy_refused is true. The `refused` parameter
    only controls whether Codex is also refused.
    """
    from modelquad import netpolicy
    from modelquad import registry as registry_module
    from modelquad.service import db as service_db
    from modelquad.service import history as service_history
    from dataclasses import replace
    from types import SimpleNamespace
    from modelquad.execution import ExecutionResult, WorkerTaskStatus
    from modelquad.models import Freshness, ProviderStatus
    from modelquad.providers import build_providers
    from modelquad.registry import ProviderCapabilities, ProviderPlugin, UsageSourceKind, WORKSPACE_DEVELOPMENT_GRANT

    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    write_usage_fixtures(tmp_path)
    db_path = tmp_path / "mcp-history.db"
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        netpolicy, "providers_refused_by_policy",
        lambda *, registry=None: (
            {name: "ModelQuad test policy refusal"
             for name in registry_module.api_execution_providers(registry)}
            if policy_refused else {}
        ),
    )
    registry = registry_module.default_registry()
    plugin = ProviderPlugin(
        id="test_api", display_name="Synthetic API worker",
        capabilities=ProviderCapabilities(exploration=True, api_execution=True),
        usage_source_kind=UsageSourceKind.UNKNOWN,
        capability_profile=dict(WORKSPACE_DEVELOPMENT_GRANT),
        provider_factory=lambda ctx: SimpleNamespace(name="test_api", get_status=lambda: ProviderStatus(
            "test_api", remaining_percentage=95, freshness=Freshness.FRESH)),
        executor_factory=lambda ctx: SimpleNamespace(name="test_api", is_available=lambda: True,
            execute=lambda request: ExecutionResult("test_api", True, "done", 0,
                                                   task_status=WorkerTaskStatus.COMPLETED)),
    )
    registry.register(plugin)
    # The all-workers-refused case also marks Codex as API-executed in this
    # synthetic registry; the production API classification function stays real.
    if "codex" in refused:
        registry = registry_module.ProviderRegistry(tuple(
            replace(p, capabilities=replace(p.capabilities, api_execution=True)) if p.id == "codex" else p
            for p in registry.all()))
    # The persisted result view (project_notes.private_result) names only
    # providers the default registry knows; make the synthetic one known there.
    monkeypatch.setattr(registry_module, "default_registry", lambda: registry)
    load_config = mcp_server.load_config
    def config_with_plugin(path=None):
        base = load_config(path)
        return replace(base, task_fit={task: {**fits, plugin.id: 1.0}
                                        for task, fits in base.task_fit.items()})
    monkeypatch.setattr(mcp_server, "load_config", config_with_plugin)
    monkeypatch.setattr(mcp_server, "build_providers", lambda config, directory:
                        build_providers(config, directory, registry=registry))

    run = RecordingRun()
    def make_manager(config):
        config.provider_capability_profiles[plugin.id] = plugin.capability_state()
        config.provider_capabilities[plugin.id] = plugin.capability_state().operational_capabilities
        # Bound to the health database like the production builder, so the
        # admission seam marks executor entry.
        return ExecutionManager(
            config,
            registry=registry,
            health_db_path=db_path,
            executors={
                "codex": CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'), run=run,
                                       inventory=qualified_codex_inventory),
                plugin.id: plugin.executor_factory(None),
            },
        )
    monkeypatch.setattr(mcp_server, "_build_execution_manager", make_manager)

    payload = _tool_payload(
        "modelquad_delegate",
        {
            "task_type": "exploration",
            "prompt": "look around",
            "working_directory": str(tmp_path),
        },
    )
    return payload, _routing_rows(db_path)


def test_the_recorded_routing_row_names_the_provider_the_delegation_actually_used(
    tmp_path, monkeypatch, *, synthetic_worker_binary, qualified_codex_inventory,
):
    """The recorded row reflects the network gate used by delegation."""
    payload, rows = _delegate_under_policy(tmp_path, monkeypatch, policy_refused=True, synthetic_worker_binary=synthetic_worker_binary,
        qualified_codex_inventory=qualified_codex_inventory)

    assert payload["provider"] == "codex"
    assert len(rows) == 1
    assert rows[0]["recommendation"] != "test_api"
    excluded = next(
        entry for entry in rows[0]["providers"] if entry["provider"] == "test_api"
    )
    assert excluded["eligible"] is False
    assert excluded["exclusion_stage"] == "network_policy"
    assert excluded["ineligibility_reasons"] == ["network_policy_refused"]
    assert excluded["preference_score"] is None


def test_an_auto_routed_delegation_selects_a_provider_it_could_have_delegated_to(
    tmp_path, monkeypatch
, *, synthetic_worker_binary, qualified_codex_inventory):
    """A delegation that ran must not SELECT a runtime it could not have used.

    `recommendation` is the advisory route and is allowed to name a host-only
    runtime: `exploration` genuinely prefers keeping the work with the host,
    and rewriting that would make this row disagree with the one
    the router computes for the same question. `selected_provider`
    answers the other question -- which provider this delegation went to --
    and left unset it defaults to the recommendation, which recorded `claude`
    as selected while Codex ran. `claude` declares no executor factory, so no
    delegation could ever have carried it out. This covers the
    case where a provider DID run; the fully refused case is the test below.
    """
    from modelquad.registry import is_host_only_runtime

    payload, rows = _delegate_under_policy(tmp_path, monkeypatch, policy_refused=True, synthetic_worker_binary=synthetic_worker_binary,
        qualified_codex_inventory=qualified_codex_inventory)

    assert rows[0]["selected_provider"] == payload["provider"] == "codex"
    assert is_host_only_runtime(rows[0]["selected_provider"]) is False
    # The control, and the reason the assertion above is about this row and
    # not about a request where the two could not have differed: this IS the
    # divergent case, and the advisory half is preserved rather than erased.
    assert rows[0]["recommendation"] == "claude"
    assert is_host_only_runtime(rows[0]["recommendation"]) is True


def test_a_delegation_that_reached_no_executor_records_that_nothing_was_selected(
    tmp_path, monkeypatch
, *, synthetic_worker_binary, qualified_codex_inventory):
    """A delegation that reached no executor selected nothing, and says so.

    With every delegable candidate refused the manager reaches no executor and
    returns the `unknown` sentinel, which is the absence of a provider and is
    never written as if it were one. Passing `None` for it was not enough:
    `record_routing_decision` substitutes the recommendation for a `None`, so
    the row recorded `claude` -- host-only, no executor factory -- as selected
    and `activity_state` read it as `selected`. The row now records that
    nothing was selected, keeps the router's advisory recommendation, and
    reads as `not_routed`.
    """
    from modelquad.mcp import presentation

    payload, rows = _delegate_under_policy(
        tmp_path, monkeypatch, policy_refused=True, refused=("test_api", "codex")
    , synthetic_worker_binary=synthetic_worker_binary,
        qualified_codex_inventory=qualified_codex_inventory)

    # The precondition: this IS the delegation that reached no executor.
    assert payload["provider"] == "unknown"
    assert payload["success"] is False
    assert len(rows) == 1
    # Nothing was selected: neither the sentinel nor the host-only advice.
    assert rows[0]["selected_provider"] is None
    # The advisory half is preserved rather than erased.
    assert rows[0]["recommendation"] == "claude"
    assert presentation.activity_state({**rows[0], "kind": "routing"}) == "not_routed"


def test_only_an_explicit_nothing_selected_records_no_selection(tmp_path):
    """`None` keeps meaning "the caller did not say".

    Recording nothing selected is opt-in: a caller that passes no selection
    still records the recommendation as it always has, a named provider is
    recorded as given, and only `NOTHING_SELECTED` leaves the selection empty.
    """
    from modelquad.models import RoutingResult, TaskType
    from modelquad.service import history as service_history

    db_path = tmp_path / "history.db"
    result = RoutingResult(
        task_type=TaskType.EXPLORATION, providers=(), recommendation="claude"
    )
    for selected in (None, "codex", service_history.NOTHING_SELECTED):
        service_history.record_routing_decision(
            db_path, TaskType.EXPLORATION, result, selected_provider=selected
        )

    recorded = sorted(
        (row["id"], row["selected_provider"], row["recommendation"])
        for row in _routing_rows(db_path)
    )
    assert [(sel, rec) for _, sel, rec in recorded] == [
        ("claude", "claude"),
        ("codex", "claude"),
        (None, "claude"),
    ]


def test_the_same_delegation_records_the_restricted_provider_when_the_policy_allows_it(
    tmp_path, monkeypatch
, *, synthetic_worker_binary, qualified_codex_inventory):
    """Allowing the same provider removes the network gate from the record."""
    payload, rows = _delegate_under_policy(tmp_path, monkeypatch, policy_refused=False, synthetic_worker_binary=synthetic_worker_binary,
        qualified_codex_inventory=qualified_codex_inventory)

    # The advice may name the permitted API worker, but an unnamed delegation
    # runs the default worker (Codex): advice and quota never pick it.
    assert payload["provider"] == "codex"
    assert rows[0]["recommendation"] == "test_api"
    assert rows[0]["selected_provider"] == "codex"
    permitted = next(entry for entry in rows[0]["providers"] if entry["provider"] == "test_api")
    assert permitted["eligible"] is True and permitted["exclusion_stage"] is None
    assert permitted["preference_score"] is not None


# --- the "unknown" sentinel is never a recorded selection ------


@pytest.mark.parametrize("entered", [False, True])
def test_explicit_history_distinguishes_refusal_from_executor_failure(tmp_path, monkeypatch, entered):
    from modelquad.service import db, history
    from modelquad.execution import ExecutionResult, WorkerTaskStatus

    write_usage_fixtures(tmp_path)
    path = tmp_path / "history.db"
    class FakeManager(ExecutionManager):
        def __init__(self, config):
            super().__init__(config, executors={})
            self.launch_entered = entered

        def delegate(self, *args, **kwargs):
            return ExecutionResult(
                provider="codex" if entered else "unknown", success=False, output="", exit_code=1 if entered else None,
                task_status=WorkerTaskStatus.FAILED if entered else WorkerTaskStatus.BLOCKED,
                task_status_reason="worker_failed" if entered else "routing_refused",
            )
    monkeypatch.setattr(mcp_server, "_build_execution_manager", lambda config: FakeManager(config))
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    payload = _tool_payload("modelquad_delegate", {
        "task_type": "review", "prompt": "synthetic task",
        "working_directory": str(tmp_path), "provider": "codex",
    })
    assert payload["success"] is False
    with db.connect_and_init(path) as connection:
        row = connection.execute("SELECT recommendation, selected_provider FROM routing_decisions").fetchone()
    assert row[0] is not None
    assert row[1] == ("codex" if entered else None)
    [attempt] = history.fetch_delegation_attempts(path)
    assert attempt["requested_provider"] == "codex"


# --------------------------------------------- a request that could not be used


def _codex(monkeypatch, synthetic_worker_binary, qualified_codex_inventory, text="ok"):
    """A Codex that answers ``text`` and records every start."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        output = codex_event_stream(text) if "--json" in argv else "ok"
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(mcp_server, "_build_execution_manager", lambda config: ExecutionManager(
        config, executors={"codex": CodexExecutor(which=lambda _: synthetic_worker_binary("codex.exe"),
                                                  run=run, inventory=qualified_codex_inventory)}))
    return calls


def _history_rows():
    import sqlite3

    from modelquad.service import db as service_db

    try:
        conn = sqlite3.connect(service_db.DEFAULT_DB_PATH)
        return (conn.execute("SELECT COUNT(*) FROM delegation_attempts").fetchone()[0]
                + conn.execute("SELECT COUNT(*) FROM execution_history").fetchone()[0])
    except sqlite3.Error:
        return 0


@pytest.mark.parametrize("change, reason, words", [
    ({"working_directory": "missing"}, "working_directory_missing", "does not exist"),
    ({"working_directory": "file"}, "working_directory_not_a_folder", "is a file, not a folder"),
    ({"model": "x; rm -rf /"}, "model_not_valid", "not a valid Codex model id"),
    ({"reasoning_effort": "ultra"}, "reasoning_effort_not_valid", "reasoning_effort"),
    ({"prompt": "   \n"}, "prompt_empty", "is empty"),
])
def test_a_request_that_cannot_be_used_is_refused_before_anything_starts(
        tmp_path, monkeypatch, change, reason, words, *, synthetic_worker_binary,
        qualified_codex_inventory):
    from modelquad import review_ledger

    write_usage_fixtures(tmp_path)
    calls = _codex(monkeypatch, synthetic_worker_binary, qualified_codex_inventory)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    folders = {"missing": str(tmp_path / "no-such-folder" / "deeper"), "file": str(tmp_path / "a.py")}
    arguments = {"task_type": "review", "prompt": "do the thing", "working_directory": str(tmp_path)}
    arguments.update(change)
    if "working_directory" in change:
        arguments["working_directory"] = folders[change["working_directory"]]

    result = asyncio.run(server.call_tool("modelquad_delegate", arguments))

    payload = tool_payload(result)
    assert payload == {"delegated": False, "executed": False, "refused": True, "started": False,
                       "recorded": False, "reason": reason}
    text = result.content[0].text
    # Not a Codex failure, and nothing about a report from Codex: it never started.
    assert text.startswith("Codex was not started.") and words in text
    assert "did not finish" not in text and "Codex's own report" not in text
    assert "no-such-folder" not in text and str(tmp_path) not in text
    assert calls == []
    # Nothing is stored, so no path reaches the history or the ledger.
    assert _history_rows() == 0
    assert not review_ledger.ledger_path().exists() or review_ledger.report()["runs"] == 0


# --------------------------------------------------- an error names its real cause


@pytest.mark.parametrize("capsule, words", [
    ({"goal": 5}, "goal must be a non-empty string"),
    ({}, "goal must be a non-empty string"),
    ({"zzz": 1}, "Unknown keys in TaskCapsule payload: zzz"),
])
def test_a_bad_capsule_says_what_is_wrong_with_it(tmp_path, monkeypatch, capsule, words, *,
                                                  synthetic_worker_binary, qualified_codex_inventory):
    write_usage_fixtures(tmp_path)
    calls = _codex(monkeypatch, synthetic_worker_binary, qualified_codex_inventory)
    with pytest.raises(Exception) as raised:
        asyncio.run(server.call_tool("modelquad_delegate", {
            "task_type": "review", "prompt": "do the thing", "working_directory": str(tmp_path),
            "capsule": capsule}))
    text = str(raised.value)
    assert words in text and "Codex was not started" in text and "Correct it and call again" in text
    assert "TaskGroup" not in text and "sub-exception" not in text
    assert calls == []


def test_an_unexpected_error_is_short_and_carries_no_path(tmp_path, monkeypatch):
    write_usage_fixtures(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError(f"cannot open {tmp_path / 'private' / 'notes.txt'}")

    monkeypatch.setattr(mcp_server, "_delegate_payload", broken)
    with pytest.raises(Exception) as raised:
        asyncio.run(server.call_tool("modelquad_delegate", {
            "task_type": "review", "prompt": "do the thing", "working_directory": str(tmp_path)}))
    text = str(raised.value)
    assert "internal error (RuntimeError)" in text and "modelquad doctor" in text
    assert "TaskGroup" not in text and "Traceback" not in text
    assert "notes.txt" not in text and str(tmp_path) not in text


def test_an_argument_error_with_a_path_in_it_is_shown_without_the_path(tmp_path, monkeypatch):
    write_usage_fixtures(tmp_path)

    def broken(*args, **kwargs):
        raise ValueError(f"working directory is odd: {tmp_path / 'private'}")

    monkeypatch.setattr(mcp_server, "_delegate_payload", broken)
    with pytest.raises(Exception) as raised:
        asyncio.run(server.call_tool("modelquad_delegate", {
            "task_type": "review", "prompt": "do the thing", "working_directory": str(tmp_path)}))
    text = str(raised.value)
    assert "working directory is odd" in text and str(tmp_path) not in text


def test_the_task_groups_wrapper_is_removed_from_a_single_error():
    inner = ValueError("goal must be a non-empty string")
    assert mcp_server._only_error(ExceptionGroup("unhandled errors in a TaskGroup", [inner])) is inner
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    assert mcp_server._only_error(nested) is inner
    several = ExceptionGroup("two", [ValueError("a"), ValueError("b")])
    assert mcp_server._only_error(several) is several


# ------------------------------------------------------- a very long answer is cut


def test_a_very_long_answer_is_cut_in_the_text_and_the_data_and_says_so(
        tmp_path, monkeypatch, *, synthetic_worker_binary, qualified_codex_inventory):
    from modelquad import review_ledger
    from modelquad.mcp.jobs_tools import MAX_SHOWN_OUTPUT_CHARS

    write_usage_fixtures(tmp_path)
    answer = "A" * 600_000 + "\nFINDING F1 | low | a.py:1 | style | found past the cut\n"
    _codex(monkeypatch, synthetic_worker_binary, qualified_codex_inventory, text=answer)
    result = asyncio.run(server.call_tool("modelquad_delegate", {
        "task_type": "review", "prompt": "do the thing", "working_directory": str(tmp_path)}))
    payload = tool_payload(result)
    shown = payload["output"]
    assert shown.startswith("A" * MAX_SHOWN_OUTPUT_CHARS)
    assert len(shown) < MAX_SHOWN_OUTPUT_CHARS + 300
    assert f"the first {MAX_SHOWN_OUTPUT_CHARS} of {len(answer)} characters are shown" in shown
    assert payload["output_truncated"] is True and payload["output_chars"] == len(answer)
    visible = json.loads(result.content[-1].text)
    assert visible["output"] == shown and visible["output_truncated"] is True
    assert all(len(block.text) < MAX_SHOWN_OUTPUT_CHARS + 2000 for block in result.content)
    # The ledger read the whole answer before the cut.
    assert [item["finding"] for item in payload["findings"]] == ["F1"]
    assert review_ledger.report()["findings"]["findings"] == 1


def test_a_short_answer_is_not_marked_as_cut(tmp_path, monkeypatch, *, synthetic_worker_binary,
                                             qualified_codex_inventory):
    write_usage_fixtures(tmp_path)
    _codex(monkeypatch, synthetic_worker_binary, qualified_codex_inventory, text="Looks fine.")
    payload = _tool_payload("modelquad_delegate", {
        "task_type": "review", "prompt": "do the thing", "working_directory": str(tmp_path)})
    assert payload["output"] == "Looks fine." and "output_truncated" not in payload
