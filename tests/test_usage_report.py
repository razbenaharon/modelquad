# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The usage report: a closed set of counts the user reads and passes on; nothing is sent."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import modelquad
import pytest
from conftest import no_window_kwargs
from modelquad import paths as paths_module
from modelquad import review_ledger as rl
from modelquad import usage_report as ur
from modelquad.cli import main

SECRET = "hunter2-long-secret-value"
ANSWER = f"""Review.

FINDING F1 | high | billing/secret_plan.py:7 | bug | VAT_BEFORE_DISCOUNT_CLAIM is wrong
FINDING F2 | medium | billing/render.py:12 | test-gap | password = "{SECRET}" in a log
FINDING F3 | low | - | style | naming
"""
NOTE = "checked-by-hand-note"


def _seed(db: Path, folder: Path) -> None:
    def payload(decision, output=ANSWER, tokens=True, **extra):
        metrics = {"input_tokens": 1400, "output_tokens": 700} if tokens else {}
        return {"decision_id": decision, "output": output, "duration_seconds": 41.6,
                "metadata": {"worker_metrics": metrics}, **extra}

    def package(decision, files, round_number=0):
        manifest = {"package_id": f"pkg-{decision}", "file_count": files, "total_bytes": 10,
                    "files": [{"path": "billing/secret_plan.py", "sha256": "abcdef123456"}]}
        rl.record_package(decision, manifest, working_directory=folder, round_number=round_number,
                          path=db)

    rl.record_run(payload("dec-one"), working_directory=folder, task_type="review",
                  trigger="review_gate", path=db)
    package("dec-one", 2)
    rl.record_run(payload("dec-two", output="FINDINGS: none\n", tokens=False),
                  working_directory=folder, task_type="architecture", trigger="user", path=db)
    package("dec-two", 12, round_number=1)
    rl.record_run(payload("dec-three", output="", refused=True, reason="repo_disabled"),
                  working_directory=folder, task_type="made-up-kind", trigger="claude", path=db)
    rl.record_verdicts("dec-one", [
        {"finding": "F1", "verdict": "confirmed", "evidence": "test", "action": "fixed",
         "fix_verified": True, "note": NOTE},
        {"finding": "F2", "verdict": "confirmed", "evidence": "code_reading", "action": "no_change"},
    ], source="claude", path=db)
    rl.record_gate_event(folder, "session-a", "blocked", 4, path=db)
    rl.record_gate_event(folder, "session-a", "review_done", 4, path=db)


def _leaves(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            yield from _leaves(item)
    else:
        yield value


def _json_part(out: str) -> dict:
    lines = out.splitlines()
    start = lines.index("{")
    return json.loads("\n".join(lines[start:lines.index("}", start) + 1]))


def test_the_fields_are_exactly_the_documented_set(tmp_path):
    report = ur.build(path=tmp_path / "none.db")
    assert tuple(report) == ur.FIELDS == (
        "schema", "kind", "modelquad_version", "windows_major", "python_minor", "window_days",
        "ledger_found", "calls", "calls_by_outcome", "calls_by_trigger", "calls_by_task_kind",
        "completion_rounds", "calls_by_files_sent", "findings_by_severity", "findings_by_verdict",
        "median_call_seconds", "tokens_reported_by_codex", "gate_events")
    assert tuple(report["calls_by_outcome"]) == (*rl.CALL_OUTCOMES, "other")
    assert tuple(report["calls_by_trigger"]) == (*rl.TRIGGERS, "other")
    assert tuple(report["calls_by_task_kind"]) == ("review", "exploration", "architecture", "other")
    assert tuple(report["calls_by_files_sent"]) == ("0", "1-3", "4-10", "more", "unknown")
    assert tuple(report["findings_by_severity"]) == (*rl.SEVERITIES, "other")
    assert tuple(report["findings_by_verdict"]) == ur.FINDING_CLASSES
    assert tuple(report["gate_events"]) == (*rl.GATE_EVENTS, "other")
    assert tuple(report["tokens_reported_by_codex"]) == (
        "calls_with_a_count", "input_rounded_to_thousand", "output_rounded_to_thousand")
    # Every field is documented where a reader looks for it.
    privacy = (Path(__file__).resolve().parents[1] / "PRIVACY.md").read_text(encoding="utf-8")
    assert [name for name in ur.FIELDS if f"`{name}`" not in privacy] == []


def test_an_empty_ledger_gives_zero_calls_and_is_not_created(tmp_path):
    db = tmp_path / "state" / "review-ledger.db"
    report = ur.build(path=db)
    assert report["ledger_found"] is False and report["calls"] == 0
    assert report["median_call_seconds"] is None
    assert report["tokens_reported_by_codex"] == {
        "calls_with_a_count": 0, "input_rounded_to_thousand": None,
        "output_rounded_to_thousand": None}
    assert not db.exists() and not db.parent.exists()
    assert any("no review ledger" in line for line in ur.preview_lines(report))


def test_a_seeded_ledger_gives_counts_and_none_of_its_text(tmp_path):
    db = tmp_path / "ledger.db"
    _seed(db, tmp_path / "project-folder-name")
    before = db.read_bytes()
    report = ur.build(days=30, path=db)
    assert db.read_bytes() == before  # Read-only: the ledger is not touched.

    assert report["calls"] == 3
    assert report["calls_by_outcome"]["answered"] == 2 and report["calls_by_outcome"]["refused"] == 1
    assert report["calls_by_trigger"] == {"review_gate": 1, "pre_change": 0, "user": 1,
                                          "claude": 1, "other": 0}
    assert report["calls_by_task_kind"] == {"review": 1, "exploration": 0, "architecture": 1,
                                            "other": 1}
    assert report["completion_rounds"] == 1
    assert report["calls_by_files_sent"] == {"0": 0, "1-3": 1, "4-10": 0, "more": 1, "unknown": 1}
    assert report["findings_by_severity"] == {"high": 1, "medium": 1, "low": 1, "other": 0}
    assert report["findings_by_verdict"] == {
        "confirmed_independent": 1, "confirmed_claude_only": 1, "refuted": 0, "undecidable": 0,
        "unjudged": 1, "fixed_and_verified": 1}
    assert report["median_call_seconds"] == 42
    assert report["gate_events"]["blocked"] == 1 and report["gate_events"]["review_done"] == 1
    assert report["modelquad_version"] == modelquad.__version__

    text = "\n".join(ur.preview_lines(report))
    for leaked in (SECRET, NOTE, "VAT_BEFORE_DISCOUNT_CLAIM", "secret_plan", "billing", "render.py",
                   "project-folder-name", str(tmp_path), "dec-one", "pkg-", "abcdef123456",
                   "session-a", "made-up-kind", "repo_disabled", rl.workspace_id(tmp_path)):
        assert leaked not in text, leaked
    for leaf in _leaves(report):
        assert leaf is None or type(leaf) in (int, bool) or leaf in ur.CLOSED_STRINGS, leaf
    assert json.loads(ur.to_json(report)) == report


def test_unknown_tokens_stay_unknown_and_known_ones_are_rounded(tmp_path):
    db = tmp_path / "ledger.db"
    rl.record_run({"decision_id": "d-1", "output": "FINDINGS: none\n", "metadata": {}},
                  working_directory=tmp_path, task_type="review", trigger="user", path=db)
    report = ur.build(path=db)
    assert report["tokens_reported_by_codex"] == {
        "calls_with_a_count": 0, "input_rounded_to_thousand": None,
        "output_rounded_to_thousand": None}
    assert report["median_call_seconds"] is None
    text = "\n".join(ur.preview_lines(report))
    assert "as reported by Codex" in text and "input unknown, output unknown" in text
    assert "Median call time: unknown" in text

    rl.record_run({"decision_id": "d-2", "output": "FINDINGS: none\n", "duration_seconds": 3.4,
                   "metadata": {"worker_metrics": {"input_tokens": 12_600, "output_tokens": 400}}},
                  working_directory=tmp_path, task_type="review", trigger="user", path=db)
    report = ur.build(path=db)
    # The totals cover the one call that has a count, and say so: the call
    # without one is not counted as zero tokens.
    assert report["tokens_reported_by_codex"] == {
        "calls_with_a_count": 1, "input_rounded_to_thousand": 13_000,
        "output_rounded_to_thousand": 0}
    assert report["median_call_seconds"] == 3
    text = "\n".join(ur.preview_lines(report))
    assert "over the 1 of 2 calls with a count" in text and "under 500 shows as 0" in text


def test_calls_outside_the_window_are_not_counted(tmp_path):
    db = tmp_path / "ledger.db"
    _seed(db, tmp_path)
    conn = rl.connect(db)
    conn.execute("UPDATE runs SET created_at = created_at - 40 * 86400 WHERE decision_id = 'dec-one'")
    conn.commit()
    conn.close()
    assert ur.build(days=30, path=db)["calls"] == 2
    assert ur.build(days=30, path=db)["findings_by_severity"]["high"] == 0
    assert ur.build(days=90, path=db)["calls"] == 3


def test_the_command_prints_the_notice_the_lines_and_the_json(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(paths_module, "state_root", lambda: state)
    state.mkdir()
    _seed(state / rl.FILE_NAME, tmp_path / "project")

    assert main(["review", "usage-report", "--days", "45"]) == 0
    out = capsys.readouterr().out
    assert "Nothing was sent." in out and "the whole report" in out
    assert "does not hold" in out and "your own choice" in out
    payload = _json_part(out)
    assert tuple(payload) == ur.FIELDS and payload["window_days"] == 45 and payload["calls"] == 3
    assert SECRET not in out and str(tmp_path) not in out

    assert main(["review", "usage-report", "--days", "0"]) == 2
    assert main(["review", "usage-report", "--days", "29"]) == 2
    assert "more precisely when you used ModelQuad" in capsys.readouterr().err


def test_a_period_shorter_than_the_minimum_is_refused():
    with pytest.raises(ValueError):
        ur.build(days=ur.MIN_DAYS - 1, path=Path("none.db"))


def test_out_writes_only_the_named_file_and_refuses_to_replace_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(paths_module, "state_root", lambda: tmp_path / "state")
    folder = tmp_path / "out"
    folder.mkdir()
    target = folder / "report.json"

    assert main(["review", "usage-report", "--out", str(target)]) == 0
    out = capsys.readouterr().out
    assert json.loads(target.read_text(encoding="utf-8")) == _json_part(out)
    assert os.listdir(folder) == ["report.json"]
    assert not (tmp_path / "state").exists()

    target.write_text("mine", encoding="utf-8")
    assert main(["review", "usage-report", "--out", str(target)]) == 2
    captured = capsys.readouterr()
    assert "already exists" in captured.err and captured.out == ""
    assert target.read_text(encoding="utf-8") == "mine"

    assert main(["review", "usage-report", "--out", str(target), "--force"]) == 0
    assert tuple(json.loads(target.read_text(encoding="utf-8"))) == ur.FIELDS


def test_the_module_imports_nothing_that_can_connect():
    allowed = {"__future__", "json", "sqlite3", "statistics", "sys", "time", "pathlib", ""}
    tree = ast.parse(Path(ur.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Level 1 is ModelQuad itself: the version and the ledger module.
            assert node.level in (0, 1)
            imported.add(node.module or "")
            if node.level == 1:
                assert {alias.name for alias in node.names} <= {"__version__", "review_ledger"}
    assert imported <= allowed, imported - allowed


def test_building_and_printing_the_report_loads_no_network_module_and_opens_no_socket(tmp_path):
    db = tmp_path / "ledger.db"
    _seed(db, tmp_path)
    code = """
import sys
def refuse(event, args):
    if event.startswith(("socket.", "urllib.", "http.", "ssl.", "subprocess.", "os.system")):
        raise RuntimeError("the usage report raised " + event)
sys.addaudithook(refuse)
from modelquad import usage_report
report = usage_report.build(path=sys.argv[1])
usage_report.preview_lines(report)
usage_report.write(report, sys.argv[2])
loaded = sorted(name for name in ("socket", "ssl", "http.client", "urllib.request", "asyncio",
                                  "subprocess", "smtplib", "ftplib") if name in sys.modules)
print("loaded:", loaded, "calls:", report["calls"])
"""
    import_root = Path(modelquad.__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", code, str(db), str(tmp_path / "out.json")],
        env={**os.environ, "PYTHONPATH": str(import_root)}, capture_output=True, text=True,
        timeout=60, **no_window_kwargs())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "loaded: [] calls: 3"


def test_the_printed_text_says_what_the_counts_cover_and_who_sends_reports(tmp_path):
    text = "\n".join(ur.preview_lines(ur.build(days=30, path=tmp_path / "none.db")))
    assert "only the calls recorded in the review ledger on this computer" in text
    assert "could not be written is missing" in text
    assert "unjudged is not refuted" in text
    assert "only from people who choose to pass them on" in text
    assert "the last 30 days," in text


def test_windows_major_is_10_11_or_null(monkeypatch):
    from types import SimpleNamespace

    for major, build, expected in ((10, 19045, 10), (10, 22631, 11), (6, 9600, None)):
        monkeypatch.setattr(sys, "getwindowsversion",
                            lambda m=major, b=build: SimpleNamespace(major=m, build=b), raising=False)
        assert ur._windows_major() == expected
    monkeypatch.delattr(sys, "getwindowsversion", raising=False)
    assert ur._windows_major() is None


def test_the_installed_command_turns_the_loopback_guard_on_for_the_report(tmp_path, capsys,
                                                                          monkeypatch):
    """The usage report runs through the guarded entry point (netpolicy.py)."""
    from modelquad import cli_entry, netpolicy

    monkeypatch.setattr(paths_module, "state_root", lambda: tmp_path / "state")
    assert netpolicy.guard_active is False
    assert cli_entry.main(["review", "usage-report"]) == 0
    assert netpolicy.guard_active is True
    assert "Nothing was sent." in capsys.readouterr().out
