# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Tests for the v0.4 local control-plane HTTP service and history layer."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from modelquad.cli import build_parser, main
from modelquad.execution.base import ExecutionResult, OutcomeEvidence, WorkerTaskStatus
from modelquad.models import Freshness, ProviderStatus, RouteTarget, RoutingResult, TaskType
from modelquad.procutil import no_window_kwargs
from modelquad.registry import (
    ProviderCapabilities,
    ProviderPlugin,
    ProviderRegistry,
    UsageSourceKind,
    default_registry,
)
from modelquad.service import db as service_db
from modelquad.service import history
from modelquad.service.app import create_app


SECRET_MARKERS = ("bearer", "api_key", "token", "secret", "password")

# Every change through the service needs the installation secret; these tests
# exercise the routes behind it, so the client sends it.
SERVICE_SECRET = "test-installation-secret-0123456789abcdef"


def _client(tmp_path, **kwargs):
    """A TestClient over the real app, minus the background usage collector.

    ``poll_providers=False`` matters: entering the client context runs the
    app's lifespan, and the collector polls immediately rather than waiting
    out ``poll_interval``. That shells out to whatever provider CLIs happen to
    be installed on the machine running the suite -- making results depend on
    the developer's tooling, and popping a console window on Windows whenever
    one of those CLIs is a ``.cmd`` shim. Tests that want collector behaviour
    exercise ``poll_once`` directly.
    """
    usage_dir = kwargs.pop("usage_directory", tmp_path / "usage")
    usage_dir.mkdir(exist_ok=True)
    db_path = kwargs.pop("db_path", tmp_path / "modelquad.db")
    kwargs.setdefault("poll_providers", False)
    kwargs.setdefault("write_secret", SERVICE_SECRET)
    app = create_app(db_path=db_path, usage_directory=usage_dir, poll_interval=3600.0, **kwargs)
    return TestClient(app, base_url="http://127.0.0.1", headers={"X-ModelQuad-Token": SERVICE_SECRET}), db_path


def _assert_no_secrets(response) -> None:
    body = json.dumps(response.json()).lower()
    for marker in SECRET_MARKERS:
        assert marker not in body


def test_job_runner_settings_require_authenticated_write_and_cli_consent(tmp_path, monkeypatch):
    from modelquad.jobs import handle
    monkeypatch.setattr(handle, 'service_runner_verdict', lambda: (False, 'job_kill_on_close'))
    client, path = _client(tmp_path)
    with client:
        assert client.put('/api/settings', json={'job_runner_enabled': True},
                          headers={'X-ModelQuad-Token': 'wrong'}).status_code == 403
        assert client.get('/api/settings').status_code in (404, 405)
        # No consent recorded: the API cannot turn the runner on.
        refused = client.put('/api/settings', json={'job_runner_enabled': True})
        assert refused.status_code == 409
        assert refused.json()['detail'] == 'job_runner_consent_required'
        assert history.get_job_runner_enabled(path) is False
        # Turning it off needs no consent.
        off = client.put('/api/settings', json={'job_runner_enabled': False})
        assert off.status_code == 200 and off.json()['job_runner_enabled'] is False
        assert off.json()['job_runner_readiness'] == 'unavailable'
        assert off.json()['job_runner_limitation'] == 'job_kill_on_close'
        # The CLI records consent after the typed answer; then the API may enable.
        history.record_job_runner_consent(path)
        assert history.get_job_runner_consent_at(path)
        saved = client.put('/api/settings', json={'job_runner_enabled': True})
        assert saved.status_code == 200
        assert saved.json()['job_runner_enabled'] is True
        assert saved.json()['job_runner_changed_at']
        assert history.get_job_runner_enabled(path) is True
        for invalid in (1, 'true', None, {}):
            assert client.put('/api/settings', json={'job_runner_enabled': invalid}).status_code == 422
        assert client.put('/api/settings', json={'preset': 'balanced'}).status_code == 422
        assert client.put('/api/settings', json={'job_runner_enabled': False}).json()['job_runner_enabled'] is False


def test_health_endpoint(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["version"], str)
    _assert_no_secrets(response)


def test_unchanged_usage_snapshot_is_not_rewritten_every_tick(tmp_path):
    """Measured 2026-08-25: identical snapshots every 60s grew the DB ~10
    KB/minute at pure idle. An unchanged snapshot is skipped; any change in
    the charted/routed values writes; the heartbeat bounds the silence."""

    import sqlite3 as _sqlite3
    from dataclasses import replace as _replace

    db_path = tmp_path / "modelquad.db"
    status = ProviderStatus(name="codex", remaining_percentage=50.0, freshness=Freshness.FRESH)

    history.record_provider_usage(db_path, (status,))
    history.record_provider_usage(db_path, (status,))
    history.record_provider_usage(db_path, (status,))

    def count():
        conn = _sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM provider_usage_history").fetchone()[0]
        finally:
            conn.close()

    assert count() == 1  # three identical ticks, one row

    history.record_provider_usage(db_path, (_replace(status, remaining_percentage=42.0),))
    assert count() == 2  # a real change writes

    history.record_provider_usage(db_path, (_replace(status, remaining_percentage=42.0, freshness=Freshness.STALE),))
    assert count() == 3  # a freshness transition is news even with no new observation


def test_unchanged_usage_snapshot_still_heartbeats(tmp_path, monkeypatch):
    db_path = tmp_path / "modelquad.db"
    status = ProviderStatus(name="codex", remaining_percentage=50.0, freshness=Freshness.FRESH)
    history.record_provider_usage(db_path, (status,))

    # An unchanged snapshot recorded after the heartbeat window must write.
    monkeypatch.setattr(history, "_UNCHANGED_ROW_HEARTBEAT_SECONDS", 0.0)
    history.record_provider_usage(db_path, (status,))

    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM provider_usage_history").fetchone()[0] == 2
    finally:
        conn.close()


def test_unchanged_health_row_is_not_rewritten_every_tick(tmp_path):
    from modelquad.doctor import DoctorReport

    db_path = tmp_path / "modelquad.db"
    report = DoctorReport(checks=())
    history.record_provider_health(db_path, report, provider_names=("codex",))
    history.record_provider_health(db_path, report, provider_names=("codex",))

    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM provider_health").fetchone()[0] == 1
    finally:
        conn.close()


def test_resource_strategy_persistence_and_validation(tmp_path):
    db_path = tmp_path / "resource_strategy.db"

    # Fresh DB returns empty dict
    assert history.get_resource_strategy(db_path) == {}

    # put_resource_strategy persists and round-trips
    saved = history.put_resource_strategy(db_path, {"claude": "spend", "codex": "preserve"})
    assert saved == {"claude": "spend", "codex": "preserve"}
    assert history.get_resource_strategy(db_path) == {"claude": "spend", "codex": "preserve"}

    # Invalid value raises ValueError and does not persist
    with pytest.raises(ValueError, match="Invalid strategy"):
        history.put_resource_strategy(db_path, {"zeta": "invalid"})
    assert history.get_resource_strategy(db_path) == {"claude": "spend", "codex": "preserve"}

    # Full replacement (not merge)
    replaced = history.put_resource_strategy(db_path, {"zeta": "spend"})
    assert replaced == {"zeta": "spend"}
    assert history.get_resource_strategy(db_path) == {"zeta": "spend"}

    # Passing {} clears the strategy
    cleared = history.put_resource_strategy(db_path, {})
    assert cleared == {}
    assert history.get_resource_strategy(db_path) == {}


def test_routing_mode_persistence_and_validation(tmp_path):
    db_path = tmp_path / "routing_mode.db"

    # Fresh DB returns DEFAULT_ROUTING_MODE ("automatic")
    assert history.get_routing_mode(db_path) == history.DEFAULT_ROUTING_MODE == "automatic"

    # put_routing_mode persists and round-trips for all valid modes
    for mode in history.VALID_ROUTING_MODES:
        saved = history.put_routing_mode(db_path, mode)
        assert saved == mode
        assert history.get_routing_mode(db_path) == mode

    # Invalid mode raises ValueError and does not change persisted value
    with pytest.raises(ValueError, match="Routing mode must be automatic"):
        history.put_routing_mode(db_path, "invalid_mode")
    assert history.get_routing_mode(db_path) == "automatic"

    # Unreadable or invalid stored value is tolerated and falls back to default
    conn = service_db.connect_and_init(db_path)
    try:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            ("not-a-valid-mode", history._ROUTING_MODE_KEY),
        )
        conn.commit()
    finally:
        conn.close()
    assert history.get_routing_mode(db_path) == history.DEFAULT_ROUTING_MODE


def test_browser_dashboard_and_its_exclusive_endpoints_are_removed(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        for path in ("/", "/providers", "/api/analytics", "/api/usage/history"):
            assert client.get(path).status_code == 404


def test_db_init_is_idempotent_and_creates_all_tables(tmp_path):
    db_path = tmp_path / "fresh.db"
    conn = service_db.connect(db_path)
    service_db.init_db(conn)
    service_db.init_db(conn)  # must not raise on a second call
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    routing_columns = {row[1] for row in conn.execute("PRAGMA table_info(routing_decisions)")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert {
        "provider_usage_history",
        "routing_decisions",
        "execution_history",
        "provider_health",
        "settings",
    } <= tables
    assert {"expected_consumption_bucket", "expected_consumption_source"} <= routing_columns
    assert version == service_db.SCHEMA_VERSION == 24


def test_db_v24_fresh_install_has_repository_tables_and_nullable_attempt_columns(tmp_path):
    conn = service_db.connect_and_init(tmp_path / "fresh-v24.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 24
        assert {row[1] for row in conn.execute("PRAGMA table_info(repo_registrations)")} == {
            "repo_key", "display_name", "root_fingerprint", "remote_fingerprint", "state", "registered_at", "updated_at",
        }
        assert {row[1] for row in conn.execute("PRAGMA table_info(repo_policy_versions)")} == {
            "repo_key", "version", "policy_json", "created_at", "source",
        }
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(delegation_attempts)")}
        for name, sql_type in (("repo_key", "TEXT"), ("policy_version", "INTEGER"), ("capability", "TEXT"), ("target_key", "TEXT")):
            assert columns[name][2] == sql_type
            assert columns[name][3] == 0  # nullable on legacy attempts
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO repo_registrations VALUES ('key', 'name', 'root', NULL, 'invalid', 'now', 'now')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO repo_policy_versions VALUES ('key', 1, '{}', 'now', 'mcp')")
    finally:
        conn.close()


def test_record_routing_decision_persists_expected_consumption_and_surfaces_in_activity_detail(tmp_path):
    from modelquad.models import ConsumptionBucket, ConsumptionSource, ExpectedConsumption

    db_path = tmp_path / "history_consumption.db"
    evidence = ExpectedConsumption(
        bucket=ConsumptionBucket.HIGH,
        source=ConsumptionSource.HOST_SUPPLIED,
        basis="supplied by host",
    )
    result = RoutingResult(
        task_type=TaskType.IMPLEMENTATION,
        providers=(),
        recommendation="codex",
        expected_consumption=evidence,
    )
    history.record_routing_decision(db_path, "implementation", result, source="mcp")

    detail = history.fetch_activity_detail(db_path, "routing", 1)
    assert detail is not None
    assert detail["expected_consumption_bucket"] == "high"
    assert detail["expected_consumption_source"] == "host_supplied"


def test_record_routing_decision_persists_decision_source(tmp_path):
    db_path = tmp_path / "history.db"
    result = RoutingResult(
        task_type=TaskType.IMPLEMENTATION,
        providers=(),
        recommendation="codex",
        decision_source="user_override",
    )
    history.record_routing_decision(db_path, "implementation", result, source="mcp")

    conn = service_db.connect_and_init(db_path)
    row = conn.execute(
        "SELECT recommendation, decision_source FROM routing_decisions"
    ).fetchone()
    conn.close()
    assert tuple(row) == ("codex", "user_override")


def test_record_routing_decision_persists_route_target_fields(tmp_path):
    db_path = tmp_path / "history.db"
    result = RoutingResult(
        task_type=TaskType.IMPLEMENTATION,
        providers=(),
        recommendation="codex",
        target=RouteTarget(provider="codex", model="gpt-5.6-terra", reasoning_effort="high"),
    )
    history.record_routing_decision(db_path, "implementation", result, source="mcp")

    conn = service_db.connect_and_init(db_path)
    row = conn.execute(
        "SELECT model_recommended, reasoning_effort_recommended FROM routing_decisions"
    ).fetchone()
    conn.close()
    assert tuple(row) == ("gpt-5.6-terra", "high")


def test_record_routing_decision_persists_null_model_fields_when_no_target(tmp_path):
    db_path = tmp_path / "history.db"
    result = RoutingResult(task_type=TaskType.IMPLEMENTATION, providers=(), recommendation="codex")
    history.record_routing_decision(db_path, "implementation", result, source="mcp")

    conn = service_db.connect_and_init(db_path)
    row = conn.execute(
        "SELECT model_recommended, reasoning_effort_recommended FROM routing_decisions"
    ).fetchone()
    conn.close()
    assert tuple(row) == (None, None)


def test_record_execution_persists_requested_model_from_result_metadata(tmp_path):
    db_path = tmp_path / "history.db"
    execution_result = ExecutionResult(
        provider="codex",
        success=True,
        output="ok",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        metadata={"requested_model": "gpt-5.6-terra", "requested_reasoning_effort": "high", "confirmed_model": None},
    )
    history.record_execution(db_path, "implementation", execution_result, source="mcp")

    conn = service_db.connect_and_init(db_path)
    row = conn.execute(
        "SELECT model_requested, model_confirmed, reasoning_effort_requested FROM execution_history"
    ).fetchone()
    conn.close()
    assert tuple(row) == ("gpt-5.6-terra", None, "high")


def test_record_execution_explicit_model_kwarg_wins_over_metadata(tmp_path):
    db_path = tmp_path / "history.db"
    execution_result = ExecutionResult(
        provider="codex",
        success=True,
        output="ok",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        metadata={"requested_model": "from-metadata"},
    )
    history.record_execution(
        db_path, "implementation", execution_result, source="mcp", model_requested="explicit-override"
    )

    conn = service_db.connect_and_init(db_path)
    row = conn.execute("SELECT model_requested FROM execution_history").fetchone()
    conn.close()
    assert row[0] == "explicit-override"


def test_record_execution_persists_outcome_evidence_json(tmp_path):
    db_path = tmp_path / "history.db"
    evidence = OutcomeEvidence(
        process_success=True,
        tests_passed=True,
        verification_level="objective",
        files_changed=("foo.py",),
    )
    execution_result = ExecutionResult(
        provider="codex",
        success=True,
        output="ok",
        exit_code=0,
        task_status=WorkerTaskStatus.COMPLETED,
        evidence=evidence,
    )
    history.record_execution(db_path, "implementation", execution_result, source="mcp")

    conn = service_db.connect_and_init(db_path)
    row = conn.execute("SELECT outcome_evidence_json FROM execution_history").fetchone()
    conn.close()
    assert row[0] is not None
    saved = json.loads(row[0])
    assert saved["verification_level"] == "objective"
    assert saved["verified"] is True
    assert saved["files_changed"] == ["foo.py"]


def test_record_execution_persists_only_project_basename_and_activity_surfaces_it(tmp_path):
    db_path = tmp_path / "history.db"
    working_directory = tmp_path / "nested" / "unrelated-project"
    history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(provider="codex", success=True, output="", exit_code=0),
        project=str(working_directory),
    )

    entry = next(item for item in history.fetch_activity(db_path) if item["kind"] == "execution")

    assert entry["project"] == "unrelated-project"
    assert os.sep not in entry["project"]
    assert str(working_directory) not in json.dumps(entry)


def test_record_execution_persists_task_status_and_derives_result_bucket(tmp_path):
    db_path = tmp_path / "history.db"

    history.record_execution(
        db_path,
        "exploration",
        ExecutionResult(
            provider="zeta",
            success=False,
            output="",
            exit_code=None,
            task_status=WorkerTaskStatus.BLOCKED,
            task_status_reason="permission_required",
            safe_to_retry=True,
        ),
    )
    history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(
            provider="codex",
            success=True,
            output="",
            exit_code=0,
            task_status=WorkerTaskStatus.PARTIAL,
            task_status_reason="worker_reported_partial",
        ),
    )
    history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(
            provider="codex",
            success=True,
            output="",
            exit_code=0,
            task_status=WorkerTaskStatus.COMPLETED,
        ),
    )

    entries = {
        entry["provider"]: entry
        for entry in history.fetch_activity(db_path)
        if entry["kind"] == "execution"
    }
    assert entries["zeta"]["task_status"] == "blocked"
    assert entries["zeta"]["task_status_reason"] == "permission_required"
    assert entries["zeta"]["result"] == "blocked"
    assert entries["codex"]["task_status"] in {"partial", "completed"}

    all_results = {entry["result"] for entry in history.fetch_activity(db_path) if entry["kind"] == "execution"}
    assert all_results == {"blocked", "partial", "success"}


def test_fetch_activity_stays_bounded_when_worker_output_is_huge(tmp_path):
    """Regression test for a real observed failure: a single review's raw
    stderr embedded in execution metadata reached ~1MB, and an
    `modelquad_activity` call with only 5 entries produced ~2.6M
    characters -- far past what a normal listing call should ever return.
    """

    db_path = tmp_path / "history.db"
    huge_stderr = "x" * 1_000_000
    for _ in range(5):
        history.record_execution(
            db_path,
            "review",
            ExecutionResult(
                provider="codex",
                success=True,
                output="",
                exit_code=0,
                metadata={
                    "stderr": huge_stderr,
                    "command": ["codex", "exec"],
                    "requested_model": "gpt-5.2-codex",
                },
                task_status=WorkerTaskStatus.COMPLETED,
            ),
        )

    entries = history.fetch_activity(db_path, limit=5)

    serialized_size = len(json.dumps(entries))
    assert serialized_size < 20_000, f"compact activity listing was {serialized_size} chars, expected < 20000"
    for entry in entries:
        assert "stderr" not in entry["metadata"]
        assert entry["metadata"]["command"] == ["codex", "exec"]
        assert entry["metadata"]["requested_model"] == "gpt-5.2-codex"


def test_fetch_activity_detail_returns_full_worker_output(tmp_path):
    db_path = tmp_path / "history.db"
    huge_stderr = "y" * 1_000_000
    history.record_execution(
        db_path,
        "review",
        ExecutionResult(
            provider="codex",
            success=True,
            output="",
            exit_code=0,
            metadata={"stderr": huge_stderr, "command": ["codex", "exec"]},
            task_status=WorkerTaskStatus.COMPLETED,
        ),
    )

    compact = next(item for item in history.fetch_activity(db_path) if item["kind"] == "execution")
    detail = history.fetch_activity_detail(db_path, "execution", compact["id"])

    assert detail is not None
    assert detail["metadata"]["stderr"] == huge_stderr

    assert history.fetch_activity_detail(db_path, "execution", 999_999) is None
    with pytest.raises(ValueError):
        history.fetch_activity_detail(db_path, "not-a-kind", 1)


def test_fetch_reliability_reports_blocked_and_partial_buckets(tmp_path):
    db_path = tmp_path / "history.db"
    history.record_execution(
        db_path,
        "exploration",
        ExecutionResult(
            provider="zeta",
            success=False,
            output="",
            exit_code=None,
            task_status=WorkerTaskStatus.BLOCKED,
            safe_to_retry=True,
        ),
    )
    history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(
            provider="zeta",
            success=True,
            output="",
            exit_code=0,
            task_status=WorkerTaskStatus.PARTIAL,
        ),
    )
    history.record_execution(
        db_path,
        "implementation",
        ExecutionResult(
            provider="zeta", success=True, output="", exit_code=0, task_status=WorkerTaskStatus.COMPLETED
        ),
    )

    reliability = {row["provider"]: row for row in history.fetch_reliability(db_path)}
    assert reliability["zeta"]["blocked"] == 1
    assert reliability["zeta"]["partial"] == 1
    assert reliability["zeta"]["success"] == 1
    assert reliability["zeta"]["total"] == 3


@pytest.mark.parametrize(
    "record_call",
    [
        lambda db_path: history.record_provider_usage(db_path, ()),
        lambda db_path: history.record_routing_decision(
            db_path,
            "implementation",
            __import__("modelquad.models", fromlist=["RoutingResult"]).RoutingResult(
                task_type=__import__("modelquad.models", fromlist=["TaskType"]).TaskType.IMPLEMENTATION,
                providers=(),
                recommendation=None,
            ),
        ),
        lambda db_path: history.record_execution(
            db_path,
            "implementation",
            ExecutionResult(provider="codex", success=False, output="", exit_code=1),
        ),
    ],
)
def test_history_helpers_never_raise_on_unwritable_db_path(tmp_path, record_call):
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("x", encoding="utf-8")
    bad_db_path = blocking_file / "modelquad.db"

    record_call(bad_db_path)  # must not raise


def test_cli_route_and_delegate_history_hooks_never_break_core_behavior(tmp_path):
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    db_path = tmp_path / "hist.db"

    exit_code = main(
        ["--usage-directory", str(usage_dir), "--history-db", str(db_path), "route", "implementation"]
    )
    assert exit_code == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT task_type, source FROM routing_decisions").fetchall()
    finally:
        conn.close()
    assert rows == [("implementation", "cli")]

    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("x", encoding="utf-8")
    bad_db = blocking_file / "hist.db"
    exit_code = main(
        ["--usage-directory", str(usage_dir), "--history-db", str(bad_db), "route", "implementation"]
    )
    assert exit_code == 0

    exit_code = main(["--usage-directory", str(usage_dir), "--no-history", "route", "implementation"])
    assert exit_code == 0


def test_cli_core_commands_do_not_hard_depend_on_fastapi(tmp_path):
    """`modelquad route` must work even when the `service` extra is absent."""

    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    # This subprocess does not inherit pytest's isolate_home_directory
    # monkeypatch (HOME/USERPROFILE), so it must pass --history-db explicitly
    # to avoid touching the real ~/.modelquad, matching the convention
    # already used for DEFAULT_SESSIONS_PATH/DEFAULT_USAGE_DIRECTORY elsewhere
    # in this test suite.
    history_db = tmp_path / "history.db"
    script = (
        "import sys, builtins\n"
        "_real_import = builtins.__import__\n"
        "def _blocked(name, *a, **k):\n"
        "    if name in ('fastapi', 'uvicorn', 'pydantic') or name.startswith(('fastapi.', 'uvicorn.', 'pydantic.')):\n"
        "        raise ImportError(f'blocked for test: {name}')\n"
        "    return _real_import(name, *a, **k)\n"
        "builtins.__import__ = _blocked\n"
        "from modelquad.cli import main\n"
        "raise SystemExit(main(['--usage-directory', sys.argv[1], '--history-db', sys.argv[2], "
        "'route', 'implementation']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(usage_dir), str(history_db)],
        capture_output=True,
        text=True,
        timeout=60,
        **no_window_kwargs(),
    )
    assert result.returncode == 0, result.stderr


def test_serve_always_binds_loopback_and_takes_no_host_option(capsys):
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert not hasattr(args, "host")
    assert args.port == 8765
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--host", "0.0.0.0"])
    capsys.readouterr()


def test_serve_prefers_explicit_db_then_global_history_db_then_default(tmp_path, monkeypatch, managed_service_child):
    import uvicorn

    import modelquad.cli as cli_module
    import modelquad.service.app as service_app_module

    captured: dict = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(service_app_module, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    history_db = tmp_path / "history.db"
    explicit_db = tmp_path / "explicit.db"

    argv, _ = managed_service_child(["--usage-directory", str(usage_dir), "--history-db", str(history_db), "serve"])
    assert cli_module.main(argv) == 0
    assert captured["db_path"] == history_db

    captured.clear()
    argv, _ = managed_service_child(
        ["--usage-directory", str(usage_dir), "--history-db", str(history_db), "serve", "--db", str(explicit_db)]
    )
    assert cli_module.main(argv) == 0
    assert captured["db_path"] == explicit_db
    captured.clear()
    argv, _ = managed_service_child(["--usage-directory", str(usage_dir), "serve"])
    assert cli_module.main(argv) == 0
    assert captured["db_path"] == cli_module.service_db.DEFAULT_DB_PATH.resolve()


def test_serve_reports_a_clear_error_when_service_extra_missing(tmp_path, capsys):
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    script = (
        "import sys, builtins\n"
        "_real_import = builtins.__import__\n"
        "def _blocked(name, *a, **k):\n"
        "    if name == 'uvicorn' or name.startswith('uvicorn.'):\n"
        "        raise ImportError('blocked for test')\n"
        "    return _real_import(name, *a, **k)\n"
        "builtins.__import__ = _blocked\n"
        "from modelquad.cli import main\n"
        "raise SystemExit(main(['--usage-directory', sys.argv[1], 'serve']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(usage_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        **no_window_kwargs(),
    )
    assert result.returncode == 1
    assert "service" in result.stderr.lower()
    assert "Install the service extra" in result.stderr


def test_routing_paused_persistence_and_defensive_fallback(tmp_path):
    db_path = tmp_path / "routing_paused.db"

    # Default is False
    assert history.get_routing_paused(db_path) == history.DEFAULT_ROUTING_PAUSED is False

    # put_routing_paused persists and round-trips True
    history.put_routing_paused(db_path, True)
    assert history.get_routing_paused(db_path) is True

    # put_routing_paused persists and round-trips False
    history.put_routing_paused(db_path, False)
    assert history.get_routing_paused(db_path) is False

    # Invalid type raises ValueError
    with pytest.raises(ValueError, match="must be a boolean"):
        history.put_routing_paused(db_path, "not-a-bool")  # type: ignore[arg-type]

    # Stored string "true" / "false" / corrupted JSON parsed defensively
    conn = service_db.connect_and_init(db_path)
    try:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (json.dumps("true"), history._ROUTING_PAUSED_KEY),
        )
        conn.commit()
    finally:
        conn.close()
    assert history.get_routing_paused(db_path) is True

    conn = service_db.connect_and_init(db_path)
    try:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            ("invalid-corrupted-json", history._ROUTING_PAUSED_KEY),
        )
        conn.commit()
    finally:
        conn.close()
    assert history.get_routing_paused(db_path) is False


def test_fetch_offload_summary_metrics(tmp_path):
    from datetime import datetime, timezone

    db_path = tmp_path / "offload.db"
    conn = service_db.connect_and_init(db_path)
    try:
        # Empty DB returns rate None and empty distribution
        empty_summary = history.fetch_offload_summary(db_path)
        assert empty_summary["executions_total"] == 0
        assert empty_summary["completed_total"] == 0
        assert empty_summary["classifiable_completed"] == 0
        assert empty_summary["offloaded_completed"] == 0
        assert empty_summary["offload_rate"] is None
        assert empty_summary["distribution"] == {}
        assert empty_summary["estimated"] is True
        assert "offloaded_completed" in empty_summary["basis"]
        assert empty_summary["window_hours"] == 168.0

        now_iso = datetime.now(timezone.utc).isoformat()
        # 1. host "claude", provider "codex", task_status "completed" -> classifiable, offloaded
        # 2. host "claude", provider "claude", task_status "completed" -> classifiable, same-provider (not offloaded)
        # 3. host "codex", provider "codex", task_status "completed", fallback 1 -> classifiable, fallback counted normally
        # 4. host "unknown_host", provider "zeta", task_status "completed" -> completed, but unclassifiable host
        # 5. host "claude", provider "codex", task_status "failed" -> failed (excluded from completed)
        # 6. host "claude", provider "codex", task_status "blocked" -> blocked (excluded from completed)
        # 7. host "claude", provider "codex", task_status "partial" -> partial (excluded from completed)
        # 8. host "claude", provider "codex", task_status "unknown" -> unknown (excluded from completed)
        rows = [
            ("codex", "Claude Code", "completed", 0, now_iso),
            ("claude", "Claude Desktop", "completed", 0, now_iso),
            ("codex", "Codex CLI", "completed", 1, now_iso),
            ("zeta", "CustomUnknownHost", "completed", 0, now_iso),
            ("codex", "Claude Code", "failed", 0, now_iso),
            ("codex", "Claude Code", "blocked", 0, now_iso),
            ("codex", "Claude Code", "partial", 0, now_iso),
            ("codex", "Claude Code", "unknown", 0, now_iso),
        ]
        for provider, host, status, fallback, created_at in rows:
            conn.execute(
                "INSERT INTO execution_history (created_at, task_type, provider, success, "
                "exit_code, duration_seconds, fallback, task_status, host) "
                "VALUES (?, 'implementation', ?, 1, 0, 1.0, ?, ?, ?)",
                (created_at, provider, fallback, status, host),
            )
        conn.commit()
    finally:
        conn.close()

    summary = history.fetch_offload_summary(db_path)
    assert summary["executions_total"] == 8
    assert summary["completed_total"] == 4
    assert summary["classifiable_completed"] == 3
    assert summary["offloaded_completed"] == 1
    assert summary["offload_rate"] == pytest.approx(1 / 3)
    assert summary["distribution"] == {"zeta": 1, "claude": 1, "codex": 2}
    assert summary["estimated"] is True


# --- doctor report reuse ------------------------------------------------------
#
# Summary schedules doctor in the background; explicit diagnosis refreshes it.


def _counting_doctor(monkeypatch):
    """Replace run_doctor with a counter over a real (empty) report."""

    from modelquad.doctor import DoctorReport
    from modelquad.service import api as service_api

    calls = {"n": 0}

    def fake_run_doctor(*args, **kwargs):
        calls["n"] += 1
        return DoctorReport(checks=())

    monkeypatch.setattr(service_api, "run_doctor", fake_run_doctor)
    return calls


def _wait_for_doctor(client):
    from modelquad.service import api as service_api

    refresh = service_api._doctor_refresh_state(client.app)
    with refresh.lock:
        done = refresh.flight
    if done is not None:
        assert done.wait(2), "synthetic doctor did not finish"


# --------------------------------------------------------------------------
# /state projection: Repair is offered only for what Repair can fix
# --------------------------------------------------------------------------


def _survey_with_live_foreign_service(tmp_path):
    from modelquad import statediag
    from modelquad.statecontext import StateMode, resolve_state_context

    foreign = statediag.StateStore(
        root=tmp_path / "other" / ".modelquad",
        mode=StateMode.DEVELOPMENT,
        exists=True,
        database_exists=True,
        service_argv=["python.exe", "-m", "modelquad", "service", "run"],
        service_pid=4242,
        service_started_at="2026-09-13T10:00:00+00:00",
        service_process="alive",
        service_image_verified=True,
    )
    return statediag.StateSurvey(
        context=resolve_state_context(),
        stores=(foreign,),
        hosts=(),
        store_findings=("A ModelQuad service is running against " + str(foreign.root) + " (pid 4242).",),
        coexistence=statediag.Coexistence.CONFLICT,
        live_foreign_stores=(foreign,),
    )


# -- plan-advice user setting --

@pytest.mark.parametrize("stored", ['null', '1', '"true"', '[]', '{}', 'not-json'])
def test_invalid_stored_plan_advice_is_off(tmp_path, stored):
    db_path = tmp_path / "settings.db"
    conn = service_db.connect_and_init(db_path)
    try:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("plan_advice_enabled", stored))
        conn.commit()
    finally:
        conn.close()
    assert history.get_plan_advice_enabled(db_path) is False


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", [], {}])
def test_plan_advice_writer_requires_real_boolean(tmp_path, invalid):
    db_path = tmp_path / "settings.db"
    history.put_plan_advice_enabled(db_path, True)
    with pytest.raises(ValueError, match="boolean"):
        history.put_plan_advice_enabled(db_path, invalid)
    assert history.get_plan_advice_enabled(db_path) is True


@pytest.mark.parametrize("version", [1, 15, 23])
def test_a_pre_release_database_is_refused_and_left_untouched(tmp_path, version):
    """There are no migrations: an older schema is refused with a clear reason."""

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()

    with pytest.raises(service_db.PreReleaseDatabase, match="pre-release ModelQuad build") as raised:
        service_db.connect_and_init(path)
    assert isinstance(raised.value, sqlite3.DatabaseError)
    assert "Move it aside" in str(raised.value)
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == version
    finally:
        check.close()


@pytest.mark.parametrize("version", [service_db.SCHEMA_VERSION + 1, 99])
def test_a_database_from_a_newer_build_is_refused_and_left_untouched(tmp_path, version):
    """A newer schema is not this build's to write into: its tables and their
    meaning may differ, so writing would damage the newer build's history."""

    path = tmp_path / "newer.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()

    with pytest.raises(service_db.NewerDatabase, match="newer ModelQuad") as raised:
        service_db.connect_and_init(path)
    assert isinstance(raised.value, sqlite3.DatabaseError)
    assert raised.value.version == version
    assert "Update ModelQuad" in str(raised.value)
    with pytest.raises(sqlite3.DatabaseError):
        history.put_plan_advice_enabled(path, True)
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == version
        assert check.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    finally:
        check.close()
