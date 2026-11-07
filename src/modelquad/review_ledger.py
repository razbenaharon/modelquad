# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The review ledger: what each Codex consultation found, and what became of it.

A small SQLite file of its own (``review-ledger.db`` in the state folder),
separate from the history database: that one has no migrations, so a new
table there would make every existing history unreadable. This file has its
own schema version and only ever gains tables with ``CREATE TABLE IF NOT
EXISTS``.

What it keeps, per Codex call: the time, a digest of the project folder (not
its path), the task kind, why the call was made (``trigger``), the version of
the code Codex saw (git HEAD plus a digest of the sent files' digests), the
outcome, the duration and the token counts Codex reported. Per finding: its
reference, the repository-relative file and line, severity, category and the
claim, redacted and capped. Per verdict: what Claude or the user decided, on
what evidence, and what was done. Per review-gate event: what the gate did.

What it never keeps: prompts, Codex's full answer, code, or conversation
text. Claim text passes the same credential redaction as the files sent to
Codex, then the path redaction history uses, and is capped.

Stdlib only, so the hooks can write gate events without importing the rest
of ModelQuad.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2
FILE_NAME = "review-ledger.db"
MAX_CLAIM_CHARS = 300
MAX_REASON_CHARS = 400
MAX_NOTE_CHARS = 200
MAX_FINDINGS = 50

#: Why a Codex call was made. ``split_quota``: a step ``modelquad_split``
#: proposed for Codex because Claude was near its limit (``split.QUOTA_TRIGGER``).
TRIGGERS = ("review_gate", "pre_change", "user", "claude", "split_quota")
VERDICTS = ("confirmed", "refuted", "undecidable")
#: Evidence a verdict rests on. The first three are independent of Claude's
#: own reading; ``code_reading`` is Claude's judgment and is never counted as
#: proof that a finding helped.
EVIDENCE = ("test", "reproduced", "ci", "code_reading", "none")
INDEPENDENT_EVIDENCE = frozenset({"test", "reproduced", "ci"})
#: ``to_fix``: the verdict recorded before the fix (accepted, to be fixed);
#: ``fixed``: recorded again after the fix, with ``fix_verified``.
ACTIONS = ("to_fix", "fixed", "no_change", "deferred")
SEVERITIES = ("high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")
GATE_EVENTS = ("blocked", "review_done", "review_failed", "not_done_reported", "consult_reminded",
               "consult_blocked", "consult_done", "consult_failed", "skipped_small")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    decision_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    workspace TEXT NOT NULL,
    task_type TEXT,
    trigger TEXT,
    code_version TEXT,
    outcome TEXT NOT NULL,
    duration_seconds REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    findings_count INTEGER
);
CREATE TABLE IF NOT EXISTS findings (
    decision_id TEXT NOT NULL,
    ref TEXT NOT NULL,
    file TEXT,
    line INTEGER,
    severity TEXT,
    category TEXT,
    claim TEXT,
    line_end INTEGER,
    confidence TEXT,
    confidence_note TEXT,
    reasoning TEXT,
    PRIMARY KEY (decision_id, ref)
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    ref TEXT NOT NULL,
    created_at REAL NOT NULL,
    verdict TEXT NOT NULL,
    evidence TEXT NOT NULL,
    action TEXT NOT NULL,
    fix_verified INTEGER,
    note TEXT,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    workspace TEXT NOT NULL,
    session TEXT NOT NULL,
    event TEXT NOT NULL,
    files INTEGER
);
"""


class LedgerVersionError(RuntimeError):
    """The ledger was written by a newer ModelQuad."""


def ledger_path(state_root: Path | None = None) -> Path:
    if state_root is None:
        from .paths import state_root as _state_root

        state_root = _state_root()
    return Path(state_root) / FILE_NAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise LedgerVersionError(f"review ledger schema {version} is newer than this build")
    if version < SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        if version == 1:
            # Version 2 added the reasoned-finding columns; rows from version 1 keep NULL there.
            for column in ("line_end INTEGER", "confidence TEXT", "confidence_note TEXT",
                           "reasoning TEXT"):
                try:
                    conn.execute(f"ALTER TABLE findings ADD COLUMN {column}")
                except sqlite3.OperationalError:
                    pass  # A concurrent opener added it first.
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn


def workspace_id(directory) -> str:
    """A digest of the project folder: the same folder always gives the same id."""
    try:
        text = os.path.normcase(os.path.realpath(os.fspath(directory)))
    except (TypeError, ValueError, OSError):
        text = str(directory)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# ------------------------------------------------------------------ findings

#: The line Codex is asked to write for each finding (noshell.INSTRUCTION):
#: FINDING F1 | high | path/file.py:12-18 | bug | confidence medium: why | claim | reasoning
#: The location is ``path:line``, ``path:start-end``, ``path`` alone, or ``-`` for a
#: design-level point with no line. The earlier five-field form (no confidence,
#: no reasoning) is still read.
_FINDING_LINE = re.compile(r"^\s*[-*]?\s*`?FINDING\s+(F\d{1,3})\s*\|(.*?)`?\s*$",
                           re.IGNORECASE | re.MULTILINE)
_LOCATION = re.compile(r"^(?P<file>[^:|]+?)(?::(?P<line>\d{1,7})(?:\s*[-\u2013]\s*(?P<end>\d{1,7}))?)?$")
_CONFIDENCE = re.compile(r"^(?:confidence\s*:?\s*)?(high|medium|low)\b\s*[:,(\-\u2013]?\s*(.*?)\)?$",
                         re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    ref: str
    severity: str
    file: str | None
    line: int | None
    category: str
    claim: str
    line_end: int | None = None
    confidence: str | None = None
    confidence_note: str | None = None
    reasoning: str | None = None


def _clean_file(text: str) -> str | None:
    text = text.strip().strip("`").replace("\\", "/")
    if not text or text.lower() in ("-", "none", "n/a", "general"):
        return None
    # Only repository-relative paths are kept; an absolute one is dropped.
    if re.match(r"^[A-Za-z]:/|^/|^//", text) or ".." in text.split("/"):
        return None
    return text[:200]


def _redact(text: str, limit: int) -> str:
    """Credential redaction (as for files sent to Codex), then path redaction, then the cap."""
    from .execution.noshell import _sanitize_supplied
    from .privacy import redact_paths

    try:
        text = _sanitize_supplied(text)
    except Exception:  # noqa: BLE001 -- fall through to the path redaction and the cap
        text = "<redacted>"
    return redact_paths(text)[:limit]


def _location(text: str) -> tuple[str | None, int | None, int | None]:
    text = text.strip().strip("`")
    match = _LOCATION.match(text)
    if match is None:
        return _clean_file(text), None, None
    line = int(match.group("line")) if match.group("line") else None
    end = int(match.group("end")) if match.group("end") else None
    if line is not None and end is not None and end < line:
        end = None
    return _clean_file(match.group("file")), line, end


def _parse_line(ref: str, rest: str) -> Finding | None:
    parts = [part.strip() for part in rest.split("|")]
    if len(parts) < 4:
        return None
    severity = parts[0].lower()
    if severity not in SEVERITIES:
        return None
    file, line, end = _location(parts[1])
    category = parts[2].lower()
    if not re.fullmatch(r"[a-z][a-z-]{0,24}", category):
        return None
    confidence = note = reasoning = None
    if len(parts) >= 6:
        match = _CONFIDENCE.match(parts[3])
        if match:
            confidence = match.group(1).lower()
            note = _redact(match.group(2).strip(), MAX_NOTE_CHARS) or None
            claim = parts[4]
            reasoning = " | ".join(parts[5:]).strip() or None
        else:
            claim = " | ".join(parts[3:])
    else:
        claim = " | ".join(parts[3:])
    if not claim.strip():
        return None
    return Finding(ref, severity, file, line, category, _redact(claim.strip(), MAX_CLAIM_CHARS),
                   end, confidence, note,
                   _redact(reasoning, MAX_REASON_CHARS) if reasoning else None)


def parse_findings(output) -> list[Finding] | None:
    """The findings in Codex's answer, or None when it lists none in the agreed form.

    None means "unknown", not zero: an answer that found nothing says
    ``FINDINGS: none`` and gives an empty list.
    """
    if not isinstance(output, str) or not output:
        return None
    found: list[Finding] = []
    seen: set[str] = set()
    for match in _FINDING_LINE.finditer(output):
        ref = match.group(1).upper()
        if ref in seen:
            continue
        finding = _parse_line(ref, match.group(2))
        if finding is None:
            continue
        seen.add(ref)
        found.append(finding)
        if len(found) >= MAX_FINDINGS:
            break
    if found:
        return found
    if re.search(r"^\s*FINDINGS:\s*none\b", output, re.IGNORECASE | re.MULTILINE):
        return []
    return None


# ------------------------------------------------------------ code version

def _commit(text: str) -> str | None:
    text = text.strip()
    return text[:12] if re.fullmatch(r"[0-9a-f]{40,64}", text) else None


def _git_head(directory) -> str | None:
    """HEAD's commit, read from the .git files without starting git; None when unknown."""
    try:
        from .repo_settings import _git_common_dir, _git_marker, _small_text, remote_pointer

        marker = _git_marker(Path(os.path.abspath(directory)))
        if marker is None:
            return None
        dot = marker / ".git"
        if dot.is_dir():
            git_dir = dot
        else:
            line = _small_text(dot)
            pointer = line[len("gitdir:"):].strip() if line.startswith("gitdir:") else ""
            if not pointer or remote_pointer(pointer):
                return None
            git_dir = (marker / pointer).resolve()
        common = _git_common_dir(marker)
        head = _small_text(git_dir / "HEAD").strip()
        if not head.startswith("ref:"):
            return _commit(head)
        ref = head[4:].strip()
        if ".." in ref or not ref.startswith("refs/"):
            return None
        for base in (git_dir, common):
            candidate = base / ref
            if candidate.is_file():
                return _commit(_small_text(candidate))
        packed = common / "packed-refs"
        if packed.is_file():
            for row in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = row.split()
                if len(parts) == 2 and parts[1] == ref:
                    return _commit(parts[0])
    except (OSError, ValueError, ImportError):
        return None
    return None


def code_version(directory, sent_files) -> str | None:
    """The version of the code Codex saw: HEAD (when known) and a digest of the sent files."""
    pairs = sorted((item.get("path"), item.get("sha256")) for item in sent_files or ()
                   if isinstance(item, dict) and item.get("path"))
    files = hashlib.sha256(repr(pairs).encode()).hexdigest()[:12] if pairs else None
    head = _git_head(directory)
    if head is None and files is None:
        return None
    return f"{head or 'nogit'}+files:{files or 'none'}"


# ----------------------------------------------------------------- writing

def _outcome(payload: dict) -> str:
    if payload.get("refused"):
        return "refused" if payload.get("started") is not True else "withheld"
    if isinstance(payload.get("output"), str) and payload["output"]:
        return "answered"
    return "failed"


def record_run(payload: dict, *, working_directory, task_type, trigger, path: Path | None = None,
               seconds: float | None = None) -> list[Finding] | None:
    """Record one Codex call and its findings; returns the findings (None: unknown)."""
    decision_id = payload.get("decision_id")
    findings = parse_findings(payload.get("output"))
    if not decision_id:
        return findings
    metadata = payload.get("metadata") or {}
    metrics = metadata.get("worker_tokens") or metadata.get("worker_metrics") or {}
    audit = metadata.get("file_audit") or {}
    duration = payload.get("duration_seconds")
    if not isinstance(duration, (int, float)):
        duration = seconds
    conn = connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, time.time(), workspace_id(working_directory), str(task_type)[:40],
             trigger if trigger in TRIGGERS else "claude",
             code_version(working_directory, audit.get("sent")), _outcome(payload),
             duration if isinstance(duration, (int, float)) else None,
             metrics.get("input_tokens") if isinstance(metrics.get("input_tokens"), int) else None,
             metrics.get("output_tokens") if isinstance(metrics.get("output_tokens"), int) else None,
             None if findings is None else len(findings)))
        for item in findings or ():
            conn.execute(
                "INSERT OR REPLACE INTO findings (decision_id, ref, file, line, severity, category, "
                "claim, line_end, confidence, confidence_note, reasoning) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, item.ref, item.file, item.line, item.severity, item.category,
                 item.claim, item.line_end, item.confidence, item.confidence_note, item.reasoning))
        conn.commit()
    finally:
        conn.close()
    return findings


class VerdictRefused(ValueError):
    """A verdict that names no recorded finding or uses an unknown word."""


def record_verdicts(decision_id: str, verdicts: list[dict], *, source: str,
                    path: Path | None = None) -> int:
    """Append verdicts for findings of one run. Raises VerdictRefused, writing nothing."""
    if source not in ("claude", "user"):
        raise VerdictRefused("source must be claude or user")
    conn = connect(path)
    try:
        known = {row["ref"] for row in conn.execute(
            "SELECT ref FROM findings WHERE decision_id = ?", (decision_id,))}
        if not known:
            raise VerdictRefused("no recorded findings for this decision_id")
        rows = []
        for item in verdicts:
            ref = str(item.get("finding", "")).upper()
            if ref not in known:
                raise VerdictRefused(f"unknown finding {ref or '(none)'}; known: {', '.join(sorted(known))}")
            verdict, evidence, action = item.get("verdict"), item.get("evidence"), item.get("action")
            if verdict not in VERDICTS or evidence not in EVIDENCE or action not in ACTIONS:
                raise VerdictRefused(
                    f"{ref}: verdict must be one of {VERDICTS}, evidence one of {EVIDENCE}, "
                    f"action one of {ACTIONS}")
            fix_verified = item.get("fix_verified")
            note = item.get("note")
            rows.append((decision_id, ref, time.time(), verdict, evidence, action,
                         None if fix_verified is None else int(bool(fix_verified)),
                         _redact(note, MAX_NOTE_CHARS) if isinstance(note, str) and note else None,
                         source))
        conn.executemany(
            "INSERT INTO verdicts (decision_id, ref, created_at, verdict, evidence, action, "
            "fix_verified, note, source) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def session_pseudonym(session: str, state_dir: Path | None = None, length: int = 16) -> str:
    """An installation-keyed pseudonym for a host session id; never raises.

    The same derivation as ``hostobs.session_fingerprint`` (HMAC on the
    installation id), kept here with the standard library only so the hook
    that calls it stays light. A plain hash of the session id would let anyone
    holding the id find its rows. If the installation id cannot be read or
    made, a plain hash stands in so the hook still works.
    """
    raw = session.strip().encode("utf-8", "surrogatepass")
    try:
        from .identity import installation_id

        key = installation_id(state_dir).encode("utf-8")
        digest = hmac.new(key, b"modelquad:host-session-fingerprint:v1:" + raw,
                          hashlib.sha256).hexdigest()
    except Exception:  # noqa: BLE001 -- a pseudonym must never break a hook
        digest = hashlib.sha256(raw).hexdigest()
    return digest[:length]


def record_gate_event(directory, session: str, event: str, files: int | None = None,
                      path: Path | None = None) -> None:
    """Record what the review gate did. Never raises: a hook must not fail on it."""
    if event not in GATE_EVENTS:
        return
    try:
        conn = connect(path)
        try:
            conn.execute("INSERT INTO gate_events (created_at, workspace, session, event, files) "
                         "VALUES (?,?,?,?,?)",
                         (time.time(), workspace_id(directory),
                          session_pseudonym(session),
                          event, files))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- a record must never break a hook
        pass


# ------------------------------------------------------------------ report

def count_runs(*, trigger: str, since: float, until: float | None = None,
               workspace: str | None = None, path: Path | None = None) -> dict | None:
    """Calls recorded with one trigger in a period, by outcome; read-only.

    ``since`` and ``until`` are epoch seconds; ``workspace`` a
    :func:`workspace_id` (None: every project). No ledger file yet means no
    call was recorded: zero. A ledger that cannot be read, or one a newer
    build wrote, returns None (unknown), never zero. Nothing is created or written.
    """
    target = Path(path) if path is not None else ledger_path()
    if not target.is_file():
        return {"calls": 0, "by_outcome": {}}
    try:
        conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
            return None
        where, args = "trigger = ? AND created_at >= ?", [trigger, since]
        if until is not None:
            where += " AND created_at <= ?"
            args.append(until)
        if workspace is not None:
            where += " AND workspace = ?"
            args.append(workspace)
        by_outcome = {row[0]: row[1] for row in conn.execute(
            f"SELECT outcome, COUNT(*) FROM runs WHERE {where} GROUP BY outcome", args)}
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {"calls": sum(by_outcome.values()), "by_outcome": by_outcome}


def report(*, days: float = 30.0, workspace: str | None = None, path: Path | None = None) -> dict:
    """Counts for the review report. Claude's agreement alone is never counted as proof."""
    since = time.time() - days * 86400
    conn = connect(path)
    try:
        where, args = "created_at >= ?", [since]
        if workspace:
            where += " AND workspace = ?"
            args.append(workspace)
        runs = [dict(row) for row in conn.execute(f"SELECT * FROM runs WHERE {where}", args)]
        ids = [run["decision_id"] for run in runs]
        findings, latest = [], {}
        if ids:
            marks = ",".join("?" * len(ids))
            findings = [dict(row) for row in conn.execute(
                f"SELECT * FROM findings WHERE decision_id IN ({marks})", ids)]
            for row in conn.execute(
                    f"SELECT * FROM verdicts WHERE decision_id IN ({marks}) ORDER BY id", ids):
                row = dict(row)
                key = (row["decision_id"], row["ref"])
                # The user's word outranks Claude's; otherwise the latest wins.
                if latest.get(key, {}).get("source") == "user" and row["source"] != "user":
                    continue
                latest[key] = row
        gate = {}
        for row in conn.execute(f"SELECT event, COUNT(*) AS n FROM gate_events WHERE {where} "
                                "GROUP BY event", args):
            gate[row["event"]] = row["n"]
    finally:
        conn.close()

    by_outcome: dict[str, int] = {}
    by_trigger: dict[str, int] = {}
    for run in runs:
        by_outcome[run["outcome"]] = by_outcome.get(run["outcome"], 0) + 1
        by_trigger[run["trigger"] or "claude"] = by_trigger.get(run["trigger"] or "claude", 0) + 1
    counts = {"findings": len(findings), "unjudged": 0, "confirmed_independent": 0,
              "confirmed_claude_only": 0, "refuted": 0, "undecidable": 0, "fixed": 0,
              "fixed_and_verified": 0, "to_fix": 0, "refuted_but_acted_on": 0, "by_user": 0}
    by_confidence: dict[str, dict[str, int]] = {}
    for finding in findings:
        verdict = latest.get((finding["decision_id"], finding["ref"]))
        bucket = by_confidence.setdefault(finding.get("confidence") or "not stated",
                                          {"findings": 0, "confirmed": 0, "refuted": 0,
                                           "undecidable": 0, "unjudged": 0})
        bucket["findings"] += 1
        bucket[verdict["verdict"] if verdict else "unjudged"] += 1
        if verdict is None:
            counts["unjudged"] += 1
            continue
        if verdict["source"] == "user":
            counts["by_user"] += 1
        if verdict["verdict"] == "confirmed":
            key = ("confirmed_independent" if verdict["evidence"] in INDEPENDENT_EVIDENCE
                   or verdict["source"] == "user" else "confirmed_claude_only")
            counts[key] += 1
        elif verdict["verdict"] == "refuted":
            counts["refuted"] += 1
            if verdict["action"] == "fixed":
                counts["refuted_but_acted_on"] += 1
        else:
            counts["undecidable"] += 1
        if verdict["action"] == "to_fix":
            counts["to_fix"] += 1
        if verdict["action"] == "fixed":
            counts["fixed"] += 1
            if verdict["fix_verified"] == 1:
                counts["fixed_and_verified"] += 1
    durations = sorted(run["duration_seconds"] for run in runs if run["duration_seconds"] is not None)
    tokens_in = [run["input_tokens"] for run in runs if run["input_tokens"] is not None]
    tokens_out = [run["output_tokens"] for run in runs if run["output_tokens"] is not None]
    return {
        "days": days,
        "runs": len(runs),
        "runs_by_outcome": by_outcome,
        "runs_by_trigger": by_trigger,
        "runs_with_unknown_findings": sum(1 for run in runs if run["findings_count"] is None),
        "median_seconds": durations[len(durations) // 2] if durations else None,
        "total_seconds": round(sum(durations), 1) if durations else None,
        "input_tokens": sum(tokens_in) if tokens_in else None,
        "output_tokens": sum(tokens_out) if tokens_out else None,
        "findings": counts,
        "by_confidence": by_confidence,
        "gate": gate,
    }
