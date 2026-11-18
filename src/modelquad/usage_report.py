# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The usage report: counts from the review ledger, for the user to read and pass on.

``modelquad review usage-report`` prints it; ``--out FILE`` also writes it to
a file the user names. This module sends nothing and opens no connection: what
happens to the text is the user's choice.

The report is a fixed set of fields (:data:`FIELDS`). Every value is a
number, a boolean, null or a string from a set fixed in this file; no text
from the ledger passes through. It has no identifier, no folder digest, no
path or file name, no package or decision id, no claim, reasoning, note or
error text, and no time finer than the window. A count the ledger does not
hold is null, never zero.

Stdlib only. The ledger is opened read-only and is not created when absent.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

from . import __version__
from . import review_ledger as rl

REPORT_SCHEMA = 1
KIND = "modelquad-usage-report"
OTHER = "other"
UNKNOWN = "unknown"
PYTHON_MINORS = ("3.11", "3.12", "3.13", "3.14")
TASK_KINDS = ("review", "exploration", "architecture")
FILE_BUCKETS = ("0", "1-3", "4-10", "more")
FINDING_CLASSES = ("confirmed_independent", "confirmed_claude_only", "refuted", "undecidable",
                   "unjudged", "fixed_and_verified")
#: The whole report: these keys, in this order, and nothing else.
FIELDS = ("schema", "kind", "modelquad_version", "windows_major", "python_minor", "window_days",
          "ledger_found", "calls", "calls_by_outcome", "calls_by_trigger", "calls_by_task_kind",
          "completion_rounds", "calls_by_files_sent", "findings_by_severity", "findings_by_verdict",
          "median_call_seconds", "tokens_reported_by_codex", "gate_events")
#: Every string a report value may be.
CLOSED_STRINGS = frozenset({KIND, __version__, OTHER, *PYTHON_MINORS})


def _tally(values, names, extra=(OTHER,)) -> dict:
    """Counts per closed name; a value outside the names is counted under ``other``."""
    counts = {name: 0 for name in (*names, *extra)}
    for value in values:
        counts[value if value in names else OTHER] += 1
    return counts


def _file_bucket(count) -> str:
    if type(count) is not int or count < 0:
        return UNKNOWN
    return "0" if count == 0 else "1-3" if count <= 3 else "4-10" if count <= 10 else "more"


def _thousands(values):
    """The total rounded to the nearest thousand; None when Codex reported no count."""
    known = [value for value in values if type(value) is int]
    return int(round(sum(known), -3)) if known else None


def _windows_major():
    """10 or 11; None when this is not Windows or is another version."""
    try:
        version = sys.getwindowsversion()
    except AttributeError:
        return None
    if version.major != 10:
        return None
    return 11 if version.build >= 22000 else 10


def _python_minor() -> str:
    text = f"{sys.version_info[0]}.{sys.version_info[1]}"
    return text if text in PYTHON_MINORS else OTHER


def _read(path: Path, since: float) -> dict:
    """The rows the report counts, through a read-only connection."""
    # Not Path.as_uri(): on Python 3.14 it imports urllib.request, which loads
    # http.client, ssl and socket. SQLite's URI form needs only %, ? and # escaped.
    name = path.resolve().as_posix().lstrip("/")
    uri = "file:///" + name.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA user_version").fetchone()[0] > rl.SCHEMA_VERSION:
            raise rl.LedgerVersionError("the review ledger was written by a newer ModelQuad")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        window = "created_at >= ?"
        in_window = f"decision_id IN (SELECT decision_id FROM runs WHERE {window})"
        runs = [dict(row) for row in conn.execute(
            "SELECT decision_id, task_type, trigger, outcome, duration_seconds, input_tokens, "
            f"output_tokens FROM runs WHERE {window}", (since,))] if "runs" in tables else []
        packages = None  # A ledger from before the packages table: sizes and rounds are unknown.
        if "packages" in tables and "runs" in tables:
            packages = {row["decision_id"]: (row["round"], row["file_count"]) for row in conn.execute(
                f"SELECT decision_id, round, file_count FROM packages WHERE {in_window}", (since,))}
        findings = [(row["decision_id"], row["ref"], row["severity"]) for row in conn.execute(
            f"SELECT decision_id, ref, severity FROM findings WHERE {in_window}", (since,))
        ] if "findings" in tables and "runs" in tables else []
        verdicts = rl._effective_verdicts(conn, sorted({item[0] for item in findings})) \
            if "verdicts" in tables else {}
        gate = [row[0] for row in conn.execute(
            f"SELECT event FROM gate_events WHERE {window}", (since,))] if "gate_events" in tables else []
    finally:
        conn.close()
    return {"runs": runs, "packages": packages, "findings": findings, "verdicts": verdicts,
            "gate": gate}


def _finding_classes(findings, verdicts) -> dict:
    """The classes ``modelquad review report`` counts: Claude's reading alone is not proof."""
    counts = {name: 0 for name in FINDING_CLASSES}
    for decision_id, ref, _severity in findings:
        verdict = verdicts.get((decision_id, ref))
        if verdict is None:
            counts["unjudged"] += 1
            continue
        if verdict["verdict"] == "confirmed":
            independent = verdict["evidence"] in rl.INDEPENDENT_EVIDENCE or verdict["source"] == "user"
            counts["confirmed_independent" if independent else "confirmed_claude_only"] += 1
        elif verdict["verdict"] == "refuted":
            counts["refuted"] += 1
        else:
            counts["undecidable"] += 1
        if verdict["action"] == "fixed" and verdict["fix_verified"] == 1:
            counts["fixed_and_verified"] += 1
    return counts


#: The shortest period a report covers. A short period next to the date a
#: report is sent would narrow down when someone used ModelQuad. A longer
#: period makes that less precise; it does not stop someone inferring it.
MIN_DAYS = 30
MAX_DAYS = 3650


def build(*, days: int = 30, path: Path | None = None) -> dict:
    """The report for the last ``days`` days, over every project on this computer."""
    days = int(days)
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(f"the period is {MIN_DAYS} to {MAX_DAYS} days")
    target = Path(path) if path is not None else rl.ledger_path()
    found = target.is_file()
    data = _read(target, time.time() - days * 86400) if found else {
        "runs": [], "packages": {}, "findings": [], "verdicts": {}, "gate": []}
    runs, packages = data["runs"], data["packages"]
    durations = [run["duration_seconds"] for run in runs
                 if isinstance(run["duration_seconds"], (int, float))]
    sent = {name: 0 for name in (*FILE_BUCKETS, UNKNOWN)}
    for run in runs:
        sent[_file_bucket((packages or {}).get(run["decision_id"], (None, None))[1])] += 1
    report = {
        "schema": REPORT_SCHEMA,
        "kind": KIND,
        "modelquad_version": __version__,
        "windows_major": _windows_major(),
        "python_minor": _python_minor(),
        "window_days": days,
        "ledger_found": found,
        "calls": len(runs),
        "calls_by_outcome": _tally((run["outcome"] for run in runs), rl.CALL_OUTCOMES),
        "calls_by_trigger": _tally((run["trigger"] or "claude" for run in runs), rl.TRIGGERS),
        "calls_by_task_kind": _tally((run["task_type"] for run in runs), TASK_KINDS),
        "completion_rounds": None if packages is None else sum(
            1 for round_number, _count in packages.values()
            if type(round_number) is int and round_number > 0),
        "calls_by_files_sent": sent,
        "findings_by_severity": _tally((item[2] for item in data["findings"]), rl.SEVERITIES),
        "findings_by_verdict": _finding_classes(data["findings"], data["verdicts"]),
        "median_call_seconds": int(round(statistics.median(durations))) if durations else None,
        # The totals cover only the calls for which Codex reported a count; the
        # number of those calls is given, so a partial total is not read as all calls.
        "tokens_reported_by_codex": {
            "calls_with_a_count": sum(
                1 for run in runs
                if type(run["input_tokens"]) is int or type(run["output_tokens"]) is int),
            "input_rounded_to_thousand": _thousands(run["input_tokens"] for run in runs),
            "output_rounded_to_thousand": _thousands(run["output_tokens"] for run in runs)},
        "gate_events": _tally(data["gate"], rl.GATE_EVENTS),
    }
    assert tuple(report) == FIELDS
    return report


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2)


def _counts(counts: dict) -> str:
    return ", ".join(f"{name.replace('_', ' ')} {value}" for name, value in counts.items())


def _known(value, unit: str = "") -> str:
    return "unknown" if value is None else f"{value}{unit}"


def preview_lines(report: dict) -> list[str]:
    """The report as readable lines, then the same data as one JSON object."""
    tokens = report["tokens_reported_by_codex"]
    lines = [
        "ModelQuad usage report",
        "",
        "Nothing was sent. This command only reads the review ledger on this computer and",
        "prints what you see here. The text below is the whole report.",
        "It holds counts and versions only. It does not hold code, task text, Codex's answers,",
        "finding text, notes, error text, file, folder or repository names, digests, package or",
        "decision ids, an installation id, or any time finer than the period.",
        "Passing it on, for example by e-mail or in a GitHub issue, is your own choice. An",
        "e-mail carries your address and a public issue carries your account.",
        "",
        "What the counts cover: only the calls recorded in the review ledger on this computer.",
        "A call whose record could not be written is missing, and so is a row removed by hand.",
        "A finding counts as unjudged until a verdict is recorded; unjudged is not refuted.",
        "Reports come only from people who choose to pass them on, so a set of reports does",
        "not stand for everyone who uses ModelQuad.",
        "",
        f"ModelQuad {report['modelquad_version']}, Windows {_known(report['windows_major'])}, "
        f"Python {report['python_minor']}",
        f"Period: the last {report['window_days']} "
        f"{'day' if report['window_days'] == 1 else 'days'}, every project on this computer",
    ]
    if not report["ledger_found"]:
        lines.append("There is no review ledger on this computer yet, so no call is counted.")
    lines += [
        f"Calls recorded: {report['calls']}",
        f"  how they ended: {_counts(report['calls_by_outcome'])}",
        f"  why they were made: {_counts(report['calls_by_trigger'])}",
        f"  task kind: {_counts(report['calls_by_task_kind'])}",
        "  files sent per call: " + ", ".join(
            f"{name} files: {value} {'call' if value == 1 else 'calls'}" for name, value in report["calls_by_files_sent"].items()),
        f"Completion rounds: {_known(report['completion_rounds'])}",
        f"Findings by severity: {_counts(report['findings_by_severity'])}",
        f"Findings by verdict: {_counts(report['findings_by_verdict'])}",
        f"Median call time: {_known(report['median_call_seconds'], ' seconds')}",
        "Tokens as reported by Codex, to the nearest thousand (a total under 500 shows as 0): "
        f"input {_known(tokens['input_rounded_to_thousand'])}, "
        f"output {_known(tokens['output_rounded_to_thousand'])}, "
        f"over the {tokens['calls_with_a_count']} of {report['calls']} calls with a count",
        f"Gate events: {_counts(report['gate_events'])}",
        "",
        "The same data as JSON:",
    ]
    return lines + to_json(report).splitlines()


def write(report: dict, out, *, force: bool = False) -> Path:
    """Write the JSON to the file the user named. FileExistsError unless ``force``."""
    target = Path(out)
    with open(target, "w" if force else "x", encoding="utf-8", newline="\n") as handle:
        handle.write(to_json(report) + "\n")
    return target
