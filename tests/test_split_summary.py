# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The split summary: what happened after the work (design memo 3.3).

Codex requests for one project, by kind and outcome, from ModelQuad's own
history; one sentence for Claude, whose work ModelQuad does not measure; and
each provider's percentage points used in each of its own windows, never
added together, never converted, never called saved. A window with too few
readings reads unknown, never 0, and a window that reset in the period says
so instead of a misleading difference (legal self-review 3.5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import itertools
import json
import re

import pytest

from modelquad import split_summary as ss
from modelquad.adapters.pending_reminder import project_key
from modelquad.cli import main
from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
from modelquad.models import Freshness, FreshnessThresholds, ProviderStatus
from modelquad.routing_policy import LaunchRefused
from modelquad.service import db as service_db
from modelquad.service import history

mcp_server = importlib.import_module("modelquad.mcp.server")

THRESHOLDS = FreshnessThresholds()
NOW = datetime.now(timezone.utc)

#: Wording the legal self-review (3.5) rules out, also in the negative,
#: plus any money or message equivalent.
BANNED = (
    "save", "saving", "waste", "every last percent", "stretch", "extend your", "maximise",
    "maximize", "get more out of", "beat", "bypass", "get around", "escape", "never hit",
    "keep coding", "runs out", "continue after", "failover", "more usage", "double", "unlimited",
    "combine", "pool", "merge", "route", "routing", "load balancing", "auto-switch", "arbitrage",
    "cheaper", "official", "partner", "endorsed", "secure", "guaranteed", "autonomous",
    "total", "$", "dollar", "usd", "price", "messages", "share of",
)


def banned_in(text: str) -> list[str]:
    folded = text.casefold()
    return [phrase for phrase in BANNED
            if (re.search(rf"\b{re.escape(phrase)}", folded) if phrase.isalpha() else phrase in folded)]


# ------------------------------------------------------------- fixtures


def _attempt(db_path, folder, decision_id, task_type="review", *, status="started", detail=None):
    row = history.record_delegation_attempt(
        db_path, task_type=task_type, requested_provider="codex", decision_id=decision_id,
        working_directory=folder, status=status,
        failure_kind="policy_refused" if status == "refused" else None, detail=detail)
    assert row is not None
    return row


def _run(db_path, folder, decision_id, task_type, *, task_status, reason=None, seconds=30.0,
         success=True, metadata=None):
    attempt = _attempt(db_path, folder, decision_id, task_type)
    history.record_execution(
        db_path, task_type,
        ExecutionResult(provider="codex", success=success, output="", exit_code=0 if success else 1,
                        task_status=task_status, task_status_reason=reason,
                        metadata=metadata or {}),
        source="mcp", project=folder.name, decision_id=decision_id, duration_seconds=seconds)
    history.resolve_delegation_attempt(db_path, attempt, status="completed" if success else "failed",
                                       routed_provider="codex")


@pytest.fixture
def projects(tmp_path):
    db_path = tmp_path / "history.db"
    one, two = tmp_path / "Project One", tmp_path / "project-two"
    one.mkdir()
    two.mkdir()
    _run(db_path, one, "a1", "review", task_status=WorkerTaskStatus.COMPLETED, seconds=20.0)
    _run(db_path, one, "a2", "review", task_status=WorkerTaskStatus.COMPLETED, seconds=40.0)
    _run(db_path, one, "a3", "exploration", task_status=WorkerTaskStatus.BLOCKED,
         reason="noshell_violated", success=False)
    _run(db_path, one, "a4", "architecture", task_status=WorkerTaskStatus.FAILED, success=False,
         metadata={"worker_metrics": {"rate_limit_events": 1}})
    _run(db_path, one, "a5", "review", task_status=WorkerTaskStatus.FAILED, reason="timed_out",
         success=False)
    _attempt(db_path, one, "a6", status="refused", detail="repo_policy_codex_off")
    _attempt(db_path, one, "a7", status="refused", detail="user_repo_codex_off")
    _attempt(db_path, one, "a8", status="refused", detail="routing is paused")
    _attempt(db_path, one, "a9", "exploration")  # still open
    _run(db_path, two, "b1", "architecture", task_status=WorkerTaskStatus.COMPLETED, seconds=99.0)
    return db_path, one, two


def _summary(db_path, folder=None, *, period="1d", current=(), now=None, session=None):
    key = project_key(str(folder)) if folder is not None else None
    return ss.summarize(db_path, workspace_key=key, period=period,
                        now=now or datetime.now(timezone.utc),
                        current=current, thresholds=THRESHOLDS, session_started_at=session)


def _claude(observed, five=None, week=None, five_reset=None, week_reset=None):
    metadata = {"source": "claude_statusline",
                "five_hour_used_percentage": five, "seven_day_used_percentage": week,
                "five_hour_resets_at": five_reset, "seven_day_resets_at": week_reset}
    return ProviderStatus(name="claude", available=True, observed_at=observed,
                          freshness=Freshness.FRESH, metadata=metadata)


def _codex(observed, five=None, week=None, five_reset=None, week_reset=None):
    windows = [{"slot": "five_hour", "window_minutes": 300, "used_percentage": five,
                "resets_at": five_reset},
               {"slot": "seven_day", "window_minutes": 10080, "used_percentage": week,
                "resets_at": week_reset}]
    return ProviderStatus(name="codex", available=True, observed_at=observed,
                          freshness=Freshness.FRESH,
                          metadata={"source": "codex_app_server", "windows": windows})


def _epoch(moment):
    return int(moment.timestamp())


# ----------------------------------------------------------- Codex side


def test_codex_requests_are_counted_by_kind_and_outcome_for_one_project(projects):
    db_path, one, _ = projects
    codex = _summary(db_path, one)["codex"]
    assert codex["requests"] == 9
    assert codex["by_kind"] == {"review": 6, "exploration": 2, "architecture": 1}
    outcomes = codex["outcomes"]
    assert outcomes[ss.ANSWERED] == 2
    assert outcomes[ss.WITHHELD] == 1
    assert outcomes[ss.NOT_ANSWERED] == 1
    assert outcomes[ss.RUNNING] == 1
    assert codex["refused"] == {ss.REFUSED_POLICY: 1, ss.REFUSED_REPO_FILE: 1,
                                ss.REFUSED_USER_SETTING: 1, ss.REFUSED_CODEX_LIMIT: 1}
    assert codex["median_answer_seconds"] == 30.0


def test_declines_the_summary_cannot_see_are_said_and_not_counted(projects):
    db_path, one, _ = projects
    summary = _summary(db_path, one)
    assert summary["codex"]["handoff_window_declines"] is None
    assert summary["codex"]["handoff_window_declines_recorded"] is False
    assert summary["codex"]["approval_prompt_declines_visible"] is False
    text = "\n".join(summary["lines"])
    assert ss.PROMPT_DECLINES_LINE in text and ss.HANDOFF_DECLINES_LINE in text


def test_the_per_project_filter_isolates_projects(projects):
    db_path, one, two = projects
    other = _summary(db_path, two)["codex"]
    assert other["requests"] == 1
    assert other["by_kind"] == {"review": 0, "exploration": 0, "architecture": 1}
    assert other["outcomes"][ss.ANSWERED] == 1
    assert other["median_answer_seconds"] == 99.0
    assert _summary(db_path, one)["codex"]["requests"] == 9
    everything = _summary(db_path)
    assert everything["scope"] == "all_projects" and everything["codex"]["requests"] == 10


def test_a_folder_with_no_history_reads_zero_requests_and_an_unknown_median(projects, tmp_path):
    db_path, _, _ = projects
    empty = tmp_path / "empty"
    empty.mkdir()
    codex = _summary(db_path, empty)["codex"]
    assert codex["requests"] == 0 and codex["median_answer_seconds"] is None
    assert "unknown" in "\n".join(_summary(db_path, empty)["lines"])


def test_requests_before_the_period_are_left_out(projects):
    db_path, one, _ = projects
    later = NOW + timedelta(days=2)
    assert _summary(db_path, one, now=later)["codex"]["requests"] == 0
    assert _summary(db_path, one, period="7d", now=later)["codex"]["requests"] == 9


def test_a_refusal_from_the_delegate_path_carries_the_project_hash_not_the_path(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)
    folder = tmp_path / "Refused Here"
    folder.mkdir()

    @mcp_server._launch_failures
    def refused(task_type, prompt, working_directory):
        raise LaunchRefused("repo_policy_codex_off")

    payload = refused("review", "look", str(folder))
    assert payload["refused"] is True and payload["recorded"] is True
    assert "working_directory" not in payload
    assert _summary(db_path, folder)["codex"]["refused"][ss.REFUSED_REPO_FILE] == 1
    raw = "".join(path.read_bytes().decode("latin-1") for path in tmp_path.glob("history.db*"))
    assert str(folder) not in raw and "Refused Here" not in raw


# ---------------------------------------------------------------- Claude


def test_the_claude_side_reads_not_measured(projects):
    db_path, one, _ = projects
    summary = _summary(db_path, one)
    assert summary["claude"] == {"measured": False, "text": ss.CLAUDE_LINE}
    assert "not measure" in summary["claude"]["text"]
    assert ss.CLAUDE_LINE in summary["lines"]


# ----------------------------------------------------------------- quota


def _service_history(db_path):
    """Readings as the service writes them: several in the period, per provider."""
    five_reset, week_reset = NOW + timedelta(hours=2), NOW + timedelta(days=3)
    new_five = NOW + timedelta(hours=4)
    steps = [
        (NOW - timedelta(hours=3), 10, 40, 20, 30, five_reset),
        (NOW - timedelta(hours=2), 18, 42, 60, 31, five_reset),
        (NOW - timedelta(hours=1), 25, 45, 5, 33, new_five),  # Codex's 5-hour window reset
    ]
    for observed, c_five, c_week, x_five, x_week, x_five_reset in steps:
        history.record_provider_usage(db_path, (
            _claude(observed, c_five, c_week, _epoch(five_reset), _epoch(week_reset)),
            _codex(observed, x_five, x_week, _epoch(x_five_reset), None),
        ))


def test_quota_deltas_come_per_provider_and_window_from_service_history(tmp_path):
    db_path = tmp_path / "history.db"
    _service_history(db_path)
    quota = _summary(db_path)["quota"]
    claude, codex = quota["providers"]["claude"], quota["providers"]["codex"]
    assert claude["source"] == codex["source"] == "history"
    assert claude["windows"]["five_hour"] == {**claude["windows"]["five_hour"],
                                              "state": ss.MEASURED, "points": 15.0, "readings": 3}
    assert claude["windows"]["seven_day"]["points"] == 5.0
    # A reset inside the period is said, not turned into a difference.
    assert codex["windows"]["five_hour"]["state"] == ss.RESET
    assert codex["windows"]["five_hour"]["points"] is None
    # No reset time reported: the difference cannot be trusted, so unknown.
    assert codex["windows"]["seven_day"]["state"] == ss.UNKNOWN
    assert codex["windows"]["seven_day"]["points"] is None
    text = "\n".join(_summary(db_path)["lines"])
    assert "Claude 5-hour" in text and "15 points" in text and "reset during the period" in text
    assert "unknown (no reset time)" in text


def test_without_service_history_the_stored_reading_alone_reads_unknown(tmp_path):
    db_path = tmp_path / "history.db"
    current = (_claude(NOW - timedelta(minutes=5), 30, 50, _epoch(NOW + timedelta(hours=1)),
                       _epoch(NOW + timedelta(days=2))),
               _codex(NOW - timedelta(minutes=5), 12, 3, _epoch(NOW + timedelta(hours=1)),
                      _epoch(NOW + timedelta(days=2))))
    for statuses in (current, ()):
        quota = _summary(db_path, current=statuses)["quota"]
        for provider in ss.PROVIDERS:
            block = quota["providers"][provider]
            assert block["source"] == ("stored_reading" if statuses else "none")
            for window in block["windows"].values():
                assert window["state"] == ss.UNKNOWN and window["points"] is None
        text = "\n".join(_summary(db_path, current=statuses)["lines"])
        assert "0 points" not in text
        assert ("one reading" if statuses else "no readings") in text
    assert not db_path.exists()  # read-only: no database is created


def test_plugin_mode_rows_at_each_request_and_the_stored_reading_give_a_delta(tmp_path):
    """Without the service, ModelQuad stores a reading at each Codex request."""
    db_path = tmp_path / "history.db"
    reset = _epoch(NOW + timedelta(hours=3))
    week = _epoch(NOW + timedelta(days=4))
    history.record_provider_usage(db_path, (_claude(NOW - timedelta(hours=2), 20, 60, reset, week),))
    current = (_claude(NOW - timedelta(minutes=1), 27, 61, reset, week),)
    claude = _summary(db_path, current=current)["quota"]["providers"]["claude"]
    assert claude["source"] == "history"
    assert claude["windows"]["five_hour"]["points"] == 7.0
    assert claude["windows"]["seven_day"]["points"] == 1.0


def test_a_reading_from_before_the_period_is_not_used(tmp_path):
    db_path = tmp_path / "history.db"
    reset = _epoch(NOW + timedelta(days=3))
    history.record_provider_usage(db_path, (_claude(NOW - timedelta(days=2), 10, 10, reset, reset),))
    history.record_provider_usage(db_path, (_claude(NOW - timedelta(hours=1), 50, 50, reset, reset),))
    window = _summary(db_path)["quota"]["providers"]["claude"]["windows"]["five_hour"]
    assert window["state"] == ss.UNKNOWN and window["why"] == "one_reading"
    week = _summary(db_path, period="7d")["quota"]["providers"]["claude"]["windows"]["five_hour"]
    assert week["state"] == ss.MEASURED and week["points"] == 40.0


@pytest.mark.parametrize("points, state", [
    ([], ss.UNKNOWN),
    ([(NOW, 5.0, NOW + timedelta(hours=1))], ss.UNKNOWN),
    ([(NOW, 5.0, None), (NOW + timedelta(minutes=5), 9.0, None)], ss.UNKNOWN),
    ([(NOW, 5.0, NOW + timedelta(hours=1)), (NOW + timedelta(minutes=5), 5.0, NOW + timedelta(hours=1))],
     ss.MEASURED),
    # Usage went down: the window reset even though the reset times agree.
    ([(NOW, 50.0, NOW + timedelta(hours=1)), (NOW + timedelta(minutes=5), 2.0, NOW + timedelta(hours=1))],
     ss.RESET),
    # The earlier reading's reset time passed before the later reading.
    ([(NOW, 50.0, NOW + timedelta(minutes=1)), (NOW + timedelta(minutes=5), 60.0, NOW + timedelta(minutes=1))],
     ss.RESET),
    # The reset time moved: a new window.
    ([(NOW, 50.0, NOW + timedelta(hours=1)), (NOW + timedelta(minutes=5), 60.0, NOW + timedelta(hours=6))],
     ss.RESET),
])
def test_window_delta_states(points, state):
    result = ss.window_delta(points)
    assert result["state"] == state
    if state == ss.MEASURED:
        assert result["points"] == 0.0  # a measured zero, from two readings
    else:
        assert result["points"] is None
        assert "0 points" not in ss.cell_text(result)


# ------------------------------------------------- wording, over a grid


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _grid(tmp_path):
    db_path = tmp_path / "grid.db"
    _service_history(db_path)
    folder = tmp_path / "Grid Project"
    folder.mkdir()
    _run(db_path, folder, "g1", "review", task_status=WorkerTaskStatus.COMPLETED)
    empty_db = tmp_path / "none.db"
    stored = (_claude(NOW - timedelta(minutes=2), 70, 80, _epoch(NOW + timedelta(hours=1)),
                      _epoch(NOW + timedelta(days=1))),)
    for db, scope, period, current in itertools.product(
            (db_path, empty_db), (folder, None), ("1d", "7d"), ((), stored)):
        yield _summary(db, scope, period=period, current=current), folder


def test_no_summary_sums_providers_saves_converts_or_shows_a_path(tmp_path):
    seen = 0
    for summary, folder in _grid(tmp_path):
        seen += 1
        text = "\n".join(summary["lines"])
        dumped = json.dumps(summary)
        assert banned_in(text) == [], text
        assert not re.search(r"\bsav", dumped.casefold())
        for key in _keys(summary):
            assert not re.search(r"total|sum(?!mar)|combined|saved|cost|message", key), key
        assert summary["quota"]["added_together"] is False
        assert str(tmp_path) not in dumped and folder.name not in dumped
        assert "\\" not in dumped and project_key(str(folder)) not in dumped
        # No number shown is one provider's window plus another's.
        cells = {}
        for provider, block in summary["quota"]["providers"].items():
            for label, window in block["windows"].items():
                if window["points"] is not None:
                    cells[(provider, label)] = window["points"]
        for (a, a_label), (b, b_label) in itertools.combinations(cells, 2):
            if a != b:
                added = cells[(a, a_label)] + cells[(b, b_label)]
                assert f"{added:g} points" not in text
        # Unknown never reads as 0.
        for block in summary["quota"]["providers"].values():
            for window in block["windows"].values():
                if window["state"] != ss.MEASURED:
                    assert window["points"] is None
    assert seen == 16


def test_each_provider_and_window_is_its_own_column(tmp_path):
    db_path = tmp_path / "history.db"
    _service_history(db_path)
    headers = [header for header, _ in ss.quota_columns(_summary(db_path)["quota"])]
    assert headers == ["Claude 5-hour", "Claude weekly", "Codex 5-hour", "Codex weekly"]


# ------------------------------------------------------ CLI and the MCP block


def test_cli_prints_the_summary_and_refuses_an_unknown_session(projects, tmp_path, capsys):
    db_path, one, _ = projects
    usage = tmp_path / "usage"
    usage.mkdir()
    base = ["--history-db", str(db_path), "--usage-directory", str(usage)]
    assert main([*base, "split", "summary", "--project", str(one)]) == 0
    out = capsys.readouterr().out
    assert "Split summary for this project, the last day" in out
    assert "9 requests (review 6, exploration 2, architecture 1)" in out
    assert str(one) not in out and one.name not in out
    assert banned_in(out) == []

    assert main([*base, "split", "summary", "--project", str(one), "--since", "7d", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["period"] == "7d" and payload["codex"]["requests"] == 9

    assert main([*base, "split", "summary", "--project", str(one), "--since", "session"]) == 2
    assert "known only inside Claude Code" in capsys.readouterr().out


def test_cli_refuses_a_missing_project_folder(tmp_path, capsys):
    assert main(["--history-db", str(tmp_path / "h.db"), "split", "summary",
                 "--project", str(tmp_path / "missing")]) == 2
    assert not (tmp_path / "h.db").exists()


def test_activity_carries_a_session_split_block_for_the_project(projects, monkeypatch, tmp_path):
    db_path, one, two = projects
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(mcp_server, "USAGE_DIRECTORY", str(tmp_path / "usage"))
    monkeypatch.setattr(mcp_server, "_SESSION_STARTED_AT", NOW - timedelta(minutes=30))
    call = getattr(mcp_server.modelquad_activity, "fn", None) or mcp_server.modelquad_activity
    block = call(project_directory=str(one))["split"]
    assert block["period"] == "session" and block["scope"] == "this_project"
    assert block["codex"]["requests"] == 9
    assert block["claude"]["measured"] is False
    assert call(project_directory=str(two))["split"]["codex"]["requests"] == 1
    dumped = json.dumps(call(project_directory=str(one)))
    assert one.name not in dumped.split('"split"')[1]
    # Before the session started, nothing counts.
    monkeypatch.setattr(mcp_server, "_SESSION_STARTED_AT", NOW + timedelta(minutes=30))
    assert call(project_directory=str(one))["split"]["codex"]["requests"] == 0


def test_the_activity_listing_survives_a_failing_summary(projects, monkeypatch):
    db_path, one, _ = projects
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(ss, "summarize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    call = getattr(mcp_server.modelquad_activity, "fn", None) or mcp_server.modelquad_activity
    payload = call(project_directory=str(one))
    assert payload["split"] == {"known": False, "why": "unavailable"}
    assert payload["activity"]
