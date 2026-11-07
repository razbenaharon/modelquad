# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The review ledger: findings, verdicts and their evidence, and a report that never counts
Claude's agreement as proof."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modelquad import review_ledger as rl

ANSWER = """Review of the discount change.

FINDING F1 | high | invoicing/tax.py:7 | bug | VAT is computed before the discount
- FINDING F2 | medium | invoicing/render.py:12 | test-gap | no test for a 100% discount
FINDING F3 | low | C:/Users/someone/secret/tax.py:3 | style | password = "hunter2-long-secret-value" in a log
FINDING F1 | low | x.py:1 | bug | a duplicate reference is ignored
"""


def _payload(decision="d-1", output=ANSWER, **extra):
    payload = {"decision_id": decision, "output": output, "duration_seconds": 42.5,
               "metadata": {"worker_metrics": {"input_tokens": 1200, "output_tokens": 300},
                            "file_audit": {"sent": [{"path": "invoicing/tax.py", "sha256": "ab12"}]}}}
    payload.update(extra)
    return payload


def test_findings_are_parsed_and_cleaned():
    found = rl.parse_findings(ANSWER)
    assert [item.ref for item in found] == ["F1", "F2", "F3"]
    assert found[0].file == "invoicing/tax.py" and found[0].line == 7 and found[0].severity == "high"
    assert found[1].category == "test-gap"
    # An absolute path is not kept, and a secret in a claim is redacted.
    assert found[2].file is None
    assert "hunter2-long-secret-value" not in found[2].claim


def test_none_and_unknown_are_different():
    assert rl.parse_findings("All good.\nFINDINGS: none\n") == []
    assert rl.parse_findings("Looks fine to me.") is None
    assert rl.parse_findings(None) is None


def test_a_run_and_its_findings_are_recorded_without_the_answer(tmp_path):
    db = tmp_path / "ledger.db"
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review",
                  trigger="review_gate", path=db)
    conn = sqlite3.connect(db)
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert run[1] > 0 and run[4] == "review_gate" and run[6] == "answered"
    assert run[8] == 1200 and run[10] == 3
    stored = " ".join(str(value) for row in conn.execute("SELECT * FROM findings") for value in row)
    # The workspace is a digest, and the answer text itself is not stored.
    assert str(tmp_path) not in " ".join(str(value) for value in run)
    assert "Review of the discount change" not in stored


def test_a_refused_call_is_recorded_as_refused(tmp_path):
    db = tmp_path / "ledger.db"
    rl.record_run({"decision_id": "d-2", "refused": True, "started": False}, working_directory=tmp_path,
                  task_type="review", trigger="claude", path=db)
    assert rl.report(path=db)["runs_by_outcome"] == {"refused": 1}


def test_verdicts_need_a_known_finding_and_known_words(tmp_path):
    db = tmp_path / "ledger.db"
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review", trigger="claude", path=db)
    with pytest.raises(rl.VerdictRefused):
        rl.record_verdicts("d-1", [{"finding": "F9", "verdict": "confirmed", "evidence": "test",
                                    "action": "fixed"}], source="claude", path=db)
    with pytest.raises(rl.VerdictRefused):
        rl.record_verdicts("d-1", [{"finding": "F1", "verdict": "yes", "evidence": "test",
                                    "action": "fixed"}], source="claude", path=db)
    with pytest.raises(rl.VerdictRefused):
        rl.record_verdicts("nope", [{"finding": "F1", "verdict": "confirmed", "evidence": "test",
                                     "action": "fixed"}], source="claude", path=db)


def test_the_report_separates_evidence_from_agreement(tmp_path):
    db = tmp_path / "ledger.db"
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review", trigger="review_gate", path=db)
    rl.record_verdicts("d-1", [
        {"finding": "F1", "verdict": "confirmed", "evidence": "test", "action": "fixed", "fix_verified": True},
        {"finding": "F2", "verdict": "confirmed", "evidence": "code_reading", "action": "no_change"},
        {"finding": "F3", "verdict": "refuted", "evidence": "code_reading", "action": "fixed"},
    ], source="claude", path=db)
    report = rl.report(path=db)
    f = report["findings"]
    assert f["confirmed_independent"] == 1 and f["confirmed_claude_only"] == 1
    assert f["refuted"] == 1 and f["refuted_but_acted_on"] == 1
    assert f["fixed"] == 2 and f["fixed_and_verified"] == 1 and f["unjudged"] == 0
    assert report["median_seconds"] == 42.5 and report["input_tokens"] == 1200


def test_the_users_verdict_outranks_claudes(tmp_path):
    db = tmp_path / "ledger.db"
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review", trigger="claude", path=db)
    rl.record_verdicts("d-1", [{"finding": "F2", "verdict": "refuted", "evidence": "none",
                                "action": "no_change"}], source="user", path=db)
    rl.record_verdicts("d-1", [{"finding": "F2", "verdict": "confirmed", "evidence": "code_reading",
                                "action": "no_change"}], source="claude", path=db)
    f = rl.report(path=db)["findings"]
    assert f["refuted"] == 1 and f["confirmed_claude_only"] == 0 and f["by_user"] == 1


def test_gate_events_and_the_report_by_project(tmp_path):
    db = tmp_path / "ledger.db"
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    rl.record_gate_event(one, "session-a", "blocked", 4, path=db)
    rl.record_gate_event(one, "session-a", "not_done_reported", 4, path=db)
    rl.record_gate_event(two, "session-b", "blocked", 3, path=db)
    rl.record_gate_event(two, "session-b", "made-up", 3, path=db)
    assert rl.report(path=db, workspace=rl.workspace_id(one))["gate"] == {"blocked": 1, "not_done_reported": 1}
    assert rl.report(path=db)["gate"] == {"blocked": 2, "not_done_reported": 1}


def test_code_version_reads_head_without_git(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git" / "refs" / "heads" / "main").write_text("0123456789abcdef" * 2 + "01234567\n",
                                                            encoding="utf-8")
    version = rl.code_version(repo, [{"path": "a.py", "sha256": "ff"}])
    assert version.startswith("0123456789ab+files:")
    assert rl.code_version(tmp_path / "plain", []) is None


def test_a_newer_ledger_is_refused(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()
    with pytest.raises(rl.LedgerVersionError):
        rl.connect(db)


def test_the_cli_report_says_what_is_proof(tmp_path, capsys, monkeypatch):
    from modelquad import cli

    monkeypatch.chdir(tmp_path)
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review", trigger="review_gate")
    assert cli.main(["review", "verdict", "d-1", "F1", "confirmed", "--evidence", "test",
                     "--action", "fixed"]) == 0
    assert cli.main(["review", "report"]) == 0
    out = capsys.readouterr().out
    assert "Confirmed with independent evidence" in out and "not proof" in out
    assert "Calls: 1 (answered 1)" in out
    assert cli.main(["review", "verdict", "d-1", "F7", "refuted"]) == 2


def test_tokens_come_from_codex_turn_completed_and_time_from_the_call(tmp_path):
    from modelquad.execution import noshell

    raw = "\n".join(['{"type":"thread.started"}',
                     '{"type":"turn.completed","usage":{"input_tokens":900,"output_tokens":120,'
                     '"cached_input_tokens":700,"evil":"x"}}'])
    assert noshell.stream_usage(raw) == {"input_tokens": 900, "cached_input_tokens": 700,
                                         "output_tokens": 120}
    assert noshell.stream_usage("not json") is None
    db = tmp_path / "ledger.db"
    payload = {"decision_id": "d-9", "output": "FINDINGS: none",
               "metadata": {"worker_tokens": noshell.stream_usage(raw)}}
    rl.record_run(payload, working_directory=tmp_path, task_type="review", trigger="claude",
                  path=db, seconds=31.0)
    report = rl.report(path=db)
    assert report["input_tokens"] == 900 and report["median_seconds"] == 31.0
