# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The review ledger: findings, verdicts and their evidence, and a report that never counts
Claude's agreement as proof."""

from __future__ import annotations

import sqlite3

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


@pytest.mark.parametrize("reason", ["free text with a reason", "C:/private/file", "x" * 65])
def test_an_open_reason_is_discarded_by_both_writers(tmp_path, reason):
    db = tmp_path / "ledger.db"
    fields = dict(working_directory=tmp_path, task_type="review", trigger="claude", path=db)
    rl.record_refusal(reason, **fields)
    rl.record_run({"decision_id": "d-2", "refused": True, "started": False, "reason": reason},
                  **fields)
    data = rl.report(path=db)
    assert data["not_started_by_reason"] == {"not recorded": 2}
    conn = rl.connect(db)
    try:
        assert [row["reason"] for row in conn.execute("SELECT reason FROM runs")] == [None, None]
    finally:
        conn.close()


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
    assert "Codex calls (Codex was started): 1 (answered 1)" in out
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


REASONED = """FINDING F1 | high | invoicing/tax.py:7-12 | bug | confidence medium: depends on the call order | VAT is computed before the discount | vat() reads subtotal(), not the discounted total
FINDING F2 | low | - | design | confidence: low (no requirements were given) | money is stored as float | float rounding can differ from decimal rounding by a cent
"""


def test_reasoned_findings_keep_range_confidence_and_reasoning():
    first, second = rl.parse_findings(REASONED)
    assert (first.file, first.line, first.line_end) == ("invoicing/tax.py", 7, 12)
    assert first.confidence == "medium" and first.confidence_note == "depends on the call order"
    assert first.claim == "VAT is computed before the discount"
    assert first.reasoning.startswith("vat() reads subtotal()")
    # A design-level point has no file and no invented line.
    assert second.file is None and second.line is None and second.line_end is None
    assert second.confidence == "low" and "no requirements" in second.confidence_note


def test_a_version_1_ledger_gains_the_new_columns(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE findings (decision_id TEXT NOT NULL, ref TEXT NOT NULL, file TEXT, line INTEGER,
            severity TEXT, category TEXT, claim TEXT, PRIMARY KEY (decision_id, ref));
        INSERT INTO findings VALUES ('old', 'F1', 'a.py', 1, 'low', 'bug', 'old claim');
        PRAGMA user_version=1;""")
    conn.close()
    conn = rl.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(findings)")}
    assert {"line_end", "confidence", "confidence_note", "reasoning"} <= columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == rl.SCHEMA_VERSION
    conn.close()


def test_verdict_before_the_fix_then_after_and_confidence_is_not_proof(tmp_path):
    db = tmp_path / "ledger.db"
    rl.record_run(_payload(output=REASONED), working_directory=tmp_path, task_type="review",
                  trigger="review_gate", path=db)
    rl.record_verdicts("d-1", [{"finding": "F1", "verdict": "confirmed", "evidence": "test",
                                "action": "to_fix"}], source="claude", path=db)
    assert rl.report(path=db)["findings"]["to_fix"] == 1
    rl.record_verdicts("d-1", [{"finding": "F1", "verdict": "confirmed", "evidence": "test",
                                "action": "fixed", "fix_verified": True},
                               {"finding": "F2", "verdict": "refuted", "evidence": "code_reading",
                                "action": "no_change"}], source="claude", path=db)
    report = rl.report(path=db)
    assert report["findings"]["to_fix"] == 0 and report["findings"]["fixed_and_verified"] == 1
    assert report["by_confidence"]["medium"]["confirmed"] == 1
    assert report["by_confidence"]["low"]["refuted"] == 1


def test_gate_events_store_an_installation_keyed_session_pseudonym(tmp_path):
    import hashlib

    db = tmp_path / "ledger.db"
    rl.record_gate_event(tmp_path, "session-a", "blocked", 4, path=db)
    conn = sqlite3.connect(db)
    try:
        (stored,) = conn.execute("SELECT session FROM gate_events").fetchone()
    finally:
        conn.close()
    # Keyed by this installation: not a plain digest anyone could recompute.
    assert stored != hashlib.sha256(b"session-a").hexdigest()[:16]
    assert "session-a" not in stored
    assert stored == rl.session_pseudonym("session-a")


def test_the_delegate_tool_records_its_trigger(tmp_path, monkeypatch):
    import anyio

    from modelquad.mcp import server as mcp, worker_guard
    from test_review_flow import FakeExecutor

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(mcp, "_executor", FakeExecutor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    def delegate(trigger, prompt):
        async def invoke():
            return await mcp.delegate(task_type="review", prompt=prompt,
                                                working_directory=str(tmp_path), files=["a.py"],
                                                timeout=60.0, trigger=trigger)
        return anyio.run(invoke)

    delegate("review_gate", "Review it.")
    data = rl.report(workspace=rl.workspace_id(tmp_path))
    assert data["runs_by_trigger"] == {"review_gate": 1}
    # An unknown word is still recorded as Claude's own call.
    delegate("made_up", "Review it again.")
    assert rl.report(workspace=rl.workspace_id(tmp_path))["runs_by_trigger"] == {
        "review_gate": 1, "claude": 1}
    assert "review_gate" in mcp.delegate.__doc__


# ------------------------------------------------------------ the package record


def test_a_version_2_ledger_gains_the_packages_table(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE findings (decision_id TEXT NOT NULL, ref TEXT NOT NULL, file TEXT, line INTEGER,
            severity TEXT, category TEXT, claim TEXT, PRIMARY KEY (decision_id, ref));
        PRAGMA user_version=2;""")
    conn.close()
    conn = rl.connect(db)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "packages" in tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == rl.SCHEMA_VERSION
    finally:
        conn.close()


def test_a_version_3_ledger_keeps_its_rows_when_reason_is_added(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE runs (
            decision_id TEXT PRIMARY KEY, created_at REAL NOT NULL, workspace TEXT NOT NULL,
            task_type TEXT, trigger TEXT, code_version TEXT, outcome TEXT NOT NULL,
            duration_seconds REAL, input_tokens INTEGER, output_tokens INTEGER, findings_count INTEGER);
        INSERT INTO runs VALUES ('old', strftime('%s', 'now'), 'ws', 'review', 'claude',
            NULL, 'refused', NULL, NULL, NULL, NULL);
        PRAGMA user_version=3;""")
    conn.close()
    rl.record_refusal("invalid_request", working_directory=tmp_path, task_type="review",
                      trigger="claude", path=db)
    conn = rl.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        row = conn.execute("SELECT * FROM runs WHERE decision_id = 'old'").fetchone()
        assert (row["workspace"], row["outcome"], row["reason"]) == ("ws", rl.REFUSED, None)
    finally:
        conn.close()
    data = rl.report(path=db)
    assert data["runs_by_outcome"] == {rl.REFUSED: 1, rl.NOT_STARTED: 1}
    assert data["not_started_by_reason"] == {"not recorded": 1, "invalid_request": 1}


def test_a_package_record_keeps_metadata_and_its_round_per_folder(tmp_path):
    db = tmp_path / "ledger.db"
    manifest = {"package_id": "p" * 32, "file_count": 1, "total_bytes": 6,
                "files": [{"path": "a.py", "bytes": 6, "sha256": "ab12"}], "skipped": []}
    rl.record_package("d-1", manifest, working_directory=tmp_path, round_number=1, follows="d-0",
                      files_needed=["b.py"], usage={"model_calls": 1}, path=db)
    assert rl.package_round("d-1", tmp_path, path=db) == 1
    # Another folder does not see this folder's record, and an unknown id is None.
    assert rl.package_round("d-1", tmp_path / "other", path=db) is None
    assert rl.package_round("d-2", tmp_path, path=db) is None
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT package_id, round, follows, file_count, total_bytes, files_needed "
                           "FROM packages WHERE decision_id = 'd-1'").fetchone()
    finally:
        conn.close()
    assert row == ("p" * 32, 1, "d-0", 1, 6, 1)


def test_reading_a_round_creates_no_ledger(tmp_path):
    missing = tmp_path / "none" / "ledger.db"
    assert rl.package_round("d-1", tmp_path, path=missing) is None
    assert not missing.parent.exists()


# ------------------------------------------------- one definition of a call's outcome


@pytest.mark.parametrize("payload, outcome", [
    ({"output": "An answer.", "metadata": {"worker_started": True}}, rl.ANSWERED),
    ({"output": "An answer."}, rl.ANSWERED),
    # Codex ran and nothing usable came back: started, not answered.
    ({"output": "", "metadata": {"worker_started": True}}, rl.FAILED),
    ({"output": "  \n", "metadata": {"worker_started": True}}, rl.FAILED),
    ({"output": "", "reason": "noshell_violated", "metadata": {"worker_started": True}}, rl.WITHHELD),
    ({"refused": True, "started": True}, rl.WITHHELD),
    ({"refused": True, "reason": "worker_noshell_unavailable"}, rl.FAILED),
    # A rule or a setting refused it before Codex started.
    ({"refused": True, "started": False, "reason": "user_repo_codex_off"}, rl.REFUSED),
    ({"refused": True, "reason": "repo_policy_codex_off"}, rl.REFUSED),
    ({"refused": True, "reason": "supplied_file_secret"}, rl.REFUSED),
    # The request itself could not be used: never started, and not a Codex failure.
    ({"refused": True, "reason": "supplied_file_missing", "output": ""}, rl.NOT_STARTED),
    ({"output": "", "task_status": "failed", "reason": "invalid_request"}, rl.NOT_STARTED),
    ({"refused": True, "started": False, "reason": "working_directory_missing"}, rl.NOT_STARTED),
    ({"refused": True, "started": False, "reason": "model_not_valid"}, rl.NOT_STARTED),
])
def test_one_function_names_how_a_call_ended(payload, outcome):
    assert rl._outcome(payload) == outcome
    assert outcome in rl.CALL_OUTCOMES


def test_only_started_calls_are_codex_calls_in_the_report(tmp_path):
    db = tmp_path / "ledger.db"
    calls = [
        {"decision_id": "s-1", "output": ANSWER, "metadata": {"worker_started": True}},
        {"decision_id": "s-2", "output": "Looks fine.", "metadata": {"worker_started": True}},
        {"decision_id": "s-3", "output": "", "metadata": {"worker_started": True}},
        {"decision_id": "s-4", "refused": True, "reason": "supplied_file_missing", "output": ""},
        {"decision_id": "s-5", "refused": True, "reason": "user_repo_codex_off"},
        {"decision_id": "s-6", "output": "", "reason": "invalid_request"},
        {"decision_id": "s-7", "output": "", "reason": "noshell_violated",
         "metadata": {"worker_started": True}},
    ]
    for payload in calls:
        rl.record_run(payload, working_directory=tmp_path, task_type="review", trigger="claude", path=db)
    data = rl.report(path=db)
    assert data["runs_by_outcome"] == {"answered": 2, "failed": 1, "withheld": 1, "refused": 1,
                                       "not_started": 2}
    assert data["runs"] == 7 and data["codex_started"] == 4 and data["not_started"] == 3
    assert data["not_started_by_reason"] == {
        "invalid_request": 1, "supplied_file_missing": 1, "user_repo_codex_off": 1}
    assert data["answered"] == 2
    # Only an answer can be "not in the findings format"; a refusal has no findings to miss.
    assert data["runs_with_unknown_findings"] == 1

    from modelquad import cli

    lines = cli.review_report_lines(data)
    assert ("  Codex calls (Codex was started): 4 (answered 2, not answered 1, "
            "withheld (tool call seen) 1)") in lines
    stopped = "  Stopped before Codex started: 3 (not started for another reason 2, refused 1)"
    assert lines[lines.index(stopped) + 1] == (
        "    Reasons: invalid_request 1, supplied_file_missing 1, user_repo_codex_off 1")
    assert any("Findings: 3 (and 1 answer(s) not in the findings format)" in line for line in lines)


# ------------------------------------------- the user's verdict and Claude's fix progress


def _one_finding(db):
    rl.record_run(_payload(), working_directory=db.parent, task_type="review", trigger="claude", path=db)


def _verdict(db, source, action, **extra):
    item = {"finding": "F1", "verdict": "confirmed", "evidence": "test", "action": action, **extra}
    return rl.record_verdicts_detail("d-1", [item], source=source, path=db)


def test_fix_progress_after_the_users_verdict_is_kept(tmp_path):
    db = tmp_path / "ledger.db"
    _one_finding(db)
    _verdict(db, "claude", "to_fix")
    _verdict(db, "user", "to_fix", evidence="none")
    kept = _verdict(db, "claude", "fixed", fix_verified=True)
    assert kept == {"recorded": 1, "ignored": []}
    counts = rl.report(path=db)["findings"]
    assert counts["to_fix"] == 0 and counts["fixed"] == 1 and counts["fixed_and_verified"] == 1
    # The verdict itself is still the user's.
    assert counts["by_user"] == 1 and counts["confirmed_independent"] == 1
    assert counts["confirmed_claude_only"] == 0


def test_claude_cannot_override_the_users_verdict_and_is_told_so(tmp_path):
    db = tmp_path / "ledger.db"
    _one_finding(db)
    rl.record_verdicts("d-1", [{"finding": "F1", "verdict": "refuted", "evidence": "none",
                                "action": "no_change"}], source="user", path=db)
    for item in ({"verdict": "confirmed", "evidence": "test", "action": "to_fix"},
                 # "Fixed" on a finding the user refuted is not fix progress on a confirmed one.
                 {"verdict": "confirmed", "evidence": "test", "action": "fixed", "fix_verified": True},
                 {"verdict": "undecidable", "evidence": "none", "action": "deferred"}):
        result = rl.record_verdicts_detail("d-1", [{"finding": "F1", **item}], source="claude", path=db)
        assert result["recorded"] == 0 and result["ignored"] == ["F1"]
        assert "user already gave a verdict on F1" in result["note"]
        assert rl.record_verdicts("d-1", [{"finding": "F1", **item}], source="claude", path=db) == 0
    counts = rl.report(path=db)["findings"]
    assert counts["refuted"] == 1 and counts["confirmed_independent"] == 0 and counts["fixed"] == 0
    # A finding the user did not judge is still Claude's to record, in the same call.
    mixed = rl.record_verdicts_detail("d-1", [
        {"finding": "F1", "verdict": "confirmed", "evidence": "test", "action": "to_fix"},
        {"finding": "F2", "verdict": "confirmed", "evidence": "test", "action": "to_fix"}],
        source="claude", path=db)
    assert mixed["recorded"] == 1 and mixed["ignored"] == ["F1"]


def test_the_verdict_tool_says_when_a_record_was_not_kept(tmp_path):
    import importlib

    server = importlib.import_module("modelquad.mcp.server")
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review", trigger="claude")
    rl.record_verdicts("d-1", [{"finding": "F1", "verdict": "confirmed", "evidence": "none",
                                "action": "to_fix"}], source="user")
    tool = getattr(server.review_verdict, "fn", None) or server.review_verdict

    def call(action, verdict="confirmed"):
        return tool("d-1", [server.FindingVerdict(finding="F1", verdict=verdict, evidence="test",
                                                  action=action, fix_verified=True)]).structured_content

    dropped = call("no_change", verdict="refuted")
    assert dropped["recorded"] == 0 and dropped["not_recorded"] == ["F1"] and "outranks" in dropped["note"]
    kept = call("fixed")
    assert kept == {"recorded": 1, "decision_id": "d-1"}
    assert rl.report()["findings"]["fixed_and_verified"] == 1


def test_the_user_can_record_a_verified_fix(tmp_path, capsys, monkeypatch):
    from modelquad import cli

    monkeypatch.chdir(tmp_path)
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review", trigger="claude")
    assert cli.main(["review", "verdict", "d-1", "F1", "confirmed", "--fix-verified"]) == 0
    counts = rl.report()["findings"]
    assert counts["fixed"] == 1 and counts["fixed_and_verified"] == 1 and counts["by_user"] == 1


# ------------------------------------------------------------ finding a decision id


def test_the_findings_listing_gives_what_a_verdict_needs(tmp_path, capsys, monkeypatch):
    import json

    from modelquad import cli

    monkeypatch.chdir(tmp_path)
    rl.record_run(_payload(), working_directory=tmp_path, task_type="review", trigger="review_gate")
    rl.record_run(_payload("d-2", "Looks fine."), working_directory=tmp_path, task_type="review",
                  trigger="claude")
    other = tmp_path / "other"
    other.mkdir()
    rl.record_run(_payload("d-3"), working_directory=other, task_type="review", trigger="claude")
    rl.record_verdicts("d-1", [{"finding": "F2", "verdict": "refuted", "evidence": "none",
                                "action": "no_change"}], source="user")

    assert cli.main(["review", "report", "--findings"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    # The id and the finding references, as `modelquad review verdict` takes them.
    assert any(line.startswith("d-1  ") and line.endswith("  review") for line in lines)
    assert any(line.startswith("  F1  high  invoicing/tax.py:7  [not judged yet]") for line in lines)
    assert any(line.startswith("  F2  medium  invoicing/render.py:12  [refuted by you]") for line in lines)
    # A call without findings, and another project's call, are not listed.
    assert "d-2" not in out and "d-3" not in out
    # Redacted like the rest of the ledger: no absolute path, no secret, no answer text.
    assert "C:/Users" not in out and "hunter2" not in out and str(tmp_path) not in out
    assert "Review of the discount change" not in out
    assert "modelquad review verdict <decision_id> <F1>" in lines[-1]

    assert cli.main(["review", "report", "--findings", "--json", "--all-projects"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {item["decision_id"] for item in data["decisions"]} == {"d-1", "d-3"}
    first = next(item for item in data["decisions"] if item["decision_id"] == "d-1")
    assert [(item["finding"], item["location"], item["verdict"], item["by"])
            for item in first["findings"]] == [
        ("F1", "invoicing/tax.py:7", None, None),
        ("F2", "invoicing/render.py:12", "refuted", "user"),
        ("F3", "-", None, None)]

    monkeypatch.chdir(other)
    (other / "empty").mkdir()
    assert cli.main(["review", "report", "--findings", "--project", str(other / "empty")]) == 0
    assert "No Codex findings recorded in the last 30 days (this project)." in capsys.readouterr().out
