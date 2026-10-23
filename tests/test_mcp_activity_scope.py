# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""MCP history and status answers: scoped to a project, free of local detail.

- ``modelquad_activity`` / ``modelquad_activity_detail`` with a
  ``project_directory`` return that project's rows. The history rows carry
  no workspace; the delegation attempt sharing their ``decision_id`` does.
- Neither returns the stored replay evidence (the router's input snapshot,
  which carries each provider's raw quota metadata, the Codex plan type
  among it).
- ``modelquad_status`` names a missing quota file by its file name only.
"""

from __future__ import annotations

import importlib
import json

import pytest

from orchestrator.execution.base import ExecutionResult, WorkerTaskStatus
from orchestrator.models import Freshness, ProviderScore, RoutingResult, TaskType
from orchestrator.service import db as service_db
from orchestrator.service import history as service_history

mcp_server = importlib.import_module("orchestrator.mcp.server")


def _call(tool, **kwargs):
    return (getattr(tool, "fn", None) or tool)(**kwargs)


def _evidence(usage_file) -> str:
    return json.dumps({"schema": 2, "providers": [{
        "name": "codex", "plan": "pro",
        "metadata": {"plan_type": "pro", "source": str(usage_file)},
    }]})


def _record_project(db_path, directory, decision_id, usage_file, *, run=True):
    attempt = service_history.record_delegation_attempt(
        db_path, task_type="review", requested_provider="codex", decision_id=decision_id,
        working_directory=directory, status="started")
    score = ProviderScore(provider="codex", available=False, fit=None, remaining_percentage=None,
                          freshness=Freshness.UNKNOWN, usage_score=None, usage_confidence=None,
                          quota_penalty=None, score=None,
                          reason=f"Usage file not found: {usage_file}")
    service_history.record_routing_decision(
        db_path, "review",
        RoutingResult(task_type=TaskType.REVIEW, providers=(score,), recommendation="codex"),
        source="mcp", decision_id=decision_id, evidence_json=_evidence(usage_file))
    if run:
        service_history.record_execution(
            db_path, "review",
            ExecutionResult(provider="codex", success=True, output="", exit_code=0,
                            task_status=WorkerTaskStatus.UNKNOWN),
            source="mcp", project=directory.name, decision_id=decision_id)
        service_history.resolve_delegation_attempt(db_path, attempt, status="completed",
                                                   routed_provider="codex")


@pytest.fixture
def history(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)
    usage = tmp_path / "usage"
    monkeypatch.setattr(mcp_server, "USAGE_DIRECTORY", str(usage))
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    _record_project(db_path, one, "decision-one", usage / "usage-codex.json")
    _record_project(db_path, two, "decision-two", usage / "usage-codex.json")
    # Still open in project two: its attempt has no execution row yet.
    _record_project(db_path, two, "decision-two-open", usage / "usage-codex.json", run=False)
    # A CLI `route` with no delegation: not attributable to any project.
    service_history.record_routing_decision(
        db_path, "review", RoutingResult(task_type=TaskType.REVIEW, providers=(),
                                         recommendation="codex"), source="cli")
    return db_path, one, two, usage


def _ids(payload, kind):
    return {entry["id"] for entry in payload["activity"] if entry["kind"] == kind}


def test_activity_with_a_project_directory_returns_that_project_only(history):
    _, one, two, _ = history
    scoped = _call(mcp_server.modelquad_activity, project_directory=str(one))
    assert {entry["decision_id"] for entry in scoped["activity"]} == {"decision-one"}
    assert {entry["kind"] for entry in scoped["activity"]} == {"routing", "execution"}
    # The open attempt in project two is not in project one's feed.
    assert not [entry for entry in scoped["presentation"]["entries"] if entry["kind"] == "attempt"]

    other = _call(mcp_server.modelquad_activity, project_directory=str(two))
    assert {entry["decision_id"] for entry in other["activity"]} == {
        "decision-two", "decision-two-open"}
    assert [entry["kind"] for entry in other["presentation"]["entries"]].count("attempt") == 1

    everything = _call(mcp_server.modelquad_activity)
    assert len(everything["activity"]) == 6
    assert None in {entry["decision_id"] for entry in everything["activity"]}


def test_activity_never_returns_replay_evidence_or_local_paths(history):
    _, one, _, usage = history
    for payload in (_call(mcp_server.modelquad_activity),
                    _call(mcp_server.modelquad_activity, project_directory=str(one))):
        text = json.dumps(payload)
        assert "evidence_json" not in text
        assert "plan_type" not in text
        assert str(usage) not in text and json.dumps(str(usage))[1:-1] not in text


def test_activity_detail_is_scoped_and_display_only(history):
    _, one, two, _ = history
    everything = _call(mcp_server.modelquad_activity)
    by_decision = {(entry["kind"], entry["decision_id"]): entry["id"]
                   for entry in everything["activity"]}
    mine = by_decision[("execution", "decision-one")]
    theirs = by_decision[("execution", "decision-two")]

    detail = _call(mcp_server.modelquad_activity_detail, kind="execution", id=mine,
                   project_directory=str(one))
    assert detail["decision_id"] == "decision-one"
    assert "outcome_evidence_json" not in detail
    assert _call(mcp_server.modelquad_activity_detail, kind="execution", id=theirs,
                 project_directory=str(one)) == {"error": "not_found"}

    routing = _call(mcp_server.modelquad_activity_detail, kind="routing",
                    id=by_decision[("routing", "decision-two")], project_directory=str(two))
    assert routing["decision_id"] == "decision-two"
    assert "evidence_json" not in routing and "plan_type" not in json.dumps(routing)
    unscoped = _call(mcp_server.modelquad_activity_detail, kind="routing",
                     id=by_decision[("routing", None)])
    assert "evidence_json" not in unscoped


def test_status_reason_names_the_quota_file_not_its_path(tmp_path, monkeypatch):
    usage = tmp_path / "private folder" / "usage"
    monkeypatch.setattr(mcp_server, "USAGE_DIRECTORY", str(usage))
    payload = _call(mcp_server.modelquad_status)
    text = json.dumps(payload)
    assert str(tmp_path) not in text and json.dumps(str(tmp_path))[1:-1] not in text
    reasons = {provider["name"]: provider["reason"] for provider in payload["providers"]}
    assert reasons["codex"] == "Usage file not found: usage-codex.json"


def test_redaction_covers_the_escaped_spelling_of_an_os_error(tmp_path):
    target = tmp_path / "usage-codex.json"
    error = PermissionError(13, "Permission denied", str(target))
    reason = f"Could not read usage file: {error}"
    assert mcp_server._redact_local_paths(reason, [target]) == (
        "Could not read usage file: [Errno 13] Permission denied: 'usage-codex.json'")
