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
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3
FILE_NAME = "review-ledger.db"
MAX_CLAIM_CHARS = 300
MAX_REASON_CHARS = 400
MAX_NOTE_CHARS = 200
MAX_FINDINGS = 50

#: Why a Codex call was made.
TRIGGERS = ("review_gate", "pre_change", "user", "claude")
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
GATE_EVENTS = ("blocked", "review_done", "review_failed", "not_done_reported",
               "consult_reminded", "consult_blocked", "consult_done", "consult_failed",
               "skipped_small")

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
CREATE TABLE IF NOT EXISTS packages (
    decision_id TEXT PRIMARY KEY,
    package_id TEXT,
    workspace TEXT NOT NULL,
    round INTEGER NOT NULL,
    follows TEXT,
    file_count INTEGER,
    total_bytes INTEGER,
    skipped_count INTEGER,
    files_needed INTEGER,
    model_calls INTEGER,
    cached_input_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    manifest TEXT
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


def _redact(text: str, limit: int, roots=()) -> str:
    """Credential redaction (as for files sent to Codex), then path redaction, then the cap.

    ``roots`` names the working folder, removed whole even when this process
    runs elsewhere.
    """
    from .execution.noshell import _sanitize_supplied
    from .privacy import redact_paths

    try:
        text = _sanitize_supplied(text)
    except Exception:  # noqa: BLE001 -- fall through to the path redaction and the cap
        text = "<redacted>"
    return redact_paths(text, roots=roots)[:limit]


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


def _parse_line(ref: str, rest: str, roots=()) -> Finding | None:
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
            note = _redact(match.group(2).strip(), MAX_NOTE_CHARS, roots) or None
            claim = parts[4]
            reasoning = " | ".join(parts[5:]).strip() or None
        else:
            claim = " | ".join(parts[3:])
    else:
        claim = " | ".join(parts[3:])
    if not claim.strip():
        return None
    return Finding(ref, severity, file, line, category, _redact(claim.strip(), MAX_CLAIM_CHARS, roots),
                   end, confidence, note,
                   _redact(reasoning, MAX_REASON_CHARS, roots) if reasoning else None)


def parse_findings(output, *, roots=()) -> list[Finding] | None:
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
        finding = _parse_line(ref, match.group(2), roots)
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

#: How one call ended. Every view (``modelquad review report``, ``modelquad
#: settings``, the split summary and ``modelquad_activity``) takes these words
#: from :func:`classify_call`, so they count the same calls the same way.
#: ``answered``: Codex ran and a non-empty answer was delivered to Claude.
#: ``withheld``: Codex ran and its answer was withheld (a tool call was seen).
#: ``failed``: Codex was started and no usable answer came back ("not answered").
#: ``refused``: a rule or a setting stopped the call before Codex started.
#: ``not_started``: the request itself could not be used (a missing file, an
#: unknown folder or model, too much to send), so Codex was never started.
ANSWERED = "answered"
WITHHELD = "withheld"
FAILED = "failed"
REFUSED = "refused"
NOT_STARTED = "not_started"
#: ``unfinished``: Codex was started and no end was recorded. The row is
#: written when Codex starts and replaced when the call ends, so it stays only
#: for a call that is still running or whose session closed in the middle.
UNFINISHED = "unfinished"
CALL_OUTCOMES = (ANSWERED, WITHHELD, FAILED, REFUSED, NOT_STARTED, UNFINISHED)
#: The outcomes of a call in which Codex was actually started: only these are "Codex calls".
STARTED_OUTCOMES = frozenset({ANSWERED, WITHHELD, FAILED, UNFINISHED})
OUTCOME_WORDS = {ANSWERED: "answered", WITHHELD: "withheld (tool call seen)", FAILED: "not answered",
                 REFUSED: "refused", NOT_STARTED: "not started for another reason",
                 UNFINISHED: "no end recorded (still running, or the session closed mid-run)"}
#: Reasons that are a plain problem with the request, not a rule that refused it.
INPUT_REASONS = frozenset({
    "supplied_file_missing", "supplied_files_over_limit", "supplied_file_unavailable",
    "supplied_file_changed", "relevant_files_ambiguous", "rendered_prompt_cap",
    "invalid_request", "working_directory_missing", "working_directory_not_a_folder",
    "model_not_valid", "reasoning_effort_not_valid", "invalid_reasoning_effort", "prompt_empty"})
_TOOL_CALL_SEEN = "noshell_violated"
_NOT_CONFIRMED = "worker_noshell_unavailable"


def classify_call(*, refused: bool, started: bool | None, delivered: bool,
                  reason: str | None = None) -> str:
    """The one definition of how a call ended; one of :data:`CALL_OUTCOMES`.

    ``refused``: the record says the call was refused. ``started``: whether
    Codex was started (None: not recorded). ``delivered``: a non-empty answer
    reached Claude. ``reason``: the closed reason code, when there is one.
    """
    if reason == _TOOL_CALL_SEEN or (refused and started is True and reason != _NOT_CONFIRMED):
        return WITHHELD
    if reason == _NOT_CONFIRMED:
        return FAILED
    if started is not True and reason in INPUT_REASONS:
        return NOT_STARTED
    if refused:
        return REFUSED
    if delivered:
        return ANSWERED
    return NOT_STARTED if started is False else FAILED


def _outcome(payload: dict) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    started = payload.get("started")
    if not isinstance(started, bool):
        started = metadata.get("worker_started")
    reason = payload.get("task_status_reason") or payload.get("reason")
    return classify_call(
        refused=payload.get("refused") is True,
        started=started if isinstance(started, bool) else None,
        delivered=isinstance(payload.get("output"), str) and bool(payload["output"].strip()),
        reason=reason if isinstance(reason, str) else None)


MAX_MANIFEST_CHARS = 32_000


def record_package(decision_id: str, manifest, *, working_directory, round_number: int = 0,
                   follows: str | None = None, files_needed=(), usage=None,
                   path: Path | None = None) -> None:
    """Record what one call sent: the package manifest, its round and what it used.

    The manifest holds relative paths, sizes, digest prefixes and skip
    reasons only. A call that packed nothing (refused before the launch)
    still gets its row, with no package id.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    usage = usage if isinstance(usage, dict) else {}

    def count(value):
        return value if type(value) is int and 0 <= value < 10**10 else None

    text = json.dumps(manifest, sort_keys=True, separators=(",", ":")) if manifest else None
    if text is not None and len(text) > MAX_MANIFEST_CHARS:
        text = None  # Counts and the id are kept; an oversized list is not.
    conn = connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO packages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, manifest.get("package_id"), workspace_id(working_directory),
             int(round_number), follows if isinstance(follows, str) else None,
             count(manifest.get("file_count")), count(manifest.get("total_bytes")),
             len(manifest.get("skipped") or ()) if manifest else None,
             len(tuple(files_needed or ())), count(usage.get("model_calls")),
             count(usage.get("cached_input_tokens")), count(usage.get("reasoning_output_tokens")),
             text))
        conn.commit()
    finally:
        conn.close()


def package_round(decision_id: str, working_directory, *, path: Path | None = None) -> int | None:
    """The completion round of a recorded call in this folder; None when there is no such call."""
    target = Path(path) if path is not None else ledger_path()
    if not isinstance(decision_id, str) or not target.is_file():
        return None
    conn = connect(target)
    try:
        row = conn.execute("SELECT round FROM packages WHERE decision_id = ? AND workspace = ?",
                           (decision_id, workspace_id(working_directory))).fetchone()
    finally:
        conn.close()
    return None if row is None else int(row["round"])


def record_start(decision_id: str, *, working_directory, task_type, trigger,
                 path: Path | None = None) -> None:
    """Record that Codex was started for this call, before anything comes back.

    :func:`record_run` replaces the row when the call ends. A session that
    closes in the middle never reaches that, and without this row a package
    would have gone to Codex with no local record of it. Time, tokens and
    findings are unknown at this point and are stored as unknown.
    """
    conn = connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, time.time(), workspace_id(working_directory), str(task_type)[:40],
             trigger if trigger in TRIGGERS else "claude", None, UNFINISHED, None, None, None, None))
        conn.commit()
    finally:
        conn.close()


def record_run(payload: dict, *, working_directory, task_type, trigger, path: Path | None = None,
               seconds: float | None = None) -> list[Finding] | None:
    """Record one Codex call and its findings; returns the findings (None: unknown)."""
    decision_id = payload.get("decision_id")
    findings = parse_findings(payload.get("output"),
                              roots=(working_directory,) if working_directory else ())
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


USER_VERDICT_NOTE = (
    "The user already gave a verdict on {refs}, and the user's word outranks Claude's, so "
    "this record was not kept. Only fix progress on a finding the user confirmed is kept: "
    "verdict confirmed with action fixed (and fix_verified).")


def record_verdicts_detail(decision_id: str, verdicts: list[dict], *, source: str,
                           path: Path | None = None) -> dict:
    """Append verdicts for findings of one run. Raises VerdictRefused, writing nothing.

    The user's verdict outranks Claude's: once the user judged a finding,
    Claude's record for it is not kept, except fix progress (verdict
    confirmed, action fixed) on a finding the user confirmed. Returns
    ``recorded`` (rows written) and ``ignored`` (the findings whose record
    was not kept), so a caller never reports a dropped record as recorded.
    """
    if source not in ("claude", "user"):
        raise VerdictRefused("source must be claude or user")
    conn = connect(path)
    try:
        known = {row["ref"] for row in conn.execute(
            "SELECT ref FROM findings WHERE decision_id = ?", (decision_id,))}
        if not known:
            raise VerdictRefused("no recorded findings for this decision_id")
        by_user = {}
        if source != "user":
            for row in conn.execute("SELECT ref, verdict FROM verdicts WHERE decision_id = ? "
                                    "AND source = 'user' ORDER BY id", (decision_id,)):
                by_user[row["ref"]] = row["verdict"]
        rows, ignored = [], []
        for item in verdicts:
            ref = str(item.get("finding", "")).upper()
            if ref not in known:
                raise VerdictRefused(f"unknown finding {ref or '(none)'}; known: {', '.join(sorted(known))}")
            verdict, evidence, action = item.get("verdict"), item.get("evidence"), item.get("action")
            if verdict not in VERDICTS or evidence not in EVIDENCE or action not in ACTIONS:
                raise VerdictRefused(
                    f"{ref}: verdict must be one of {VERDICTS}, evidence one of {EVIDENCE}, "
                    f"action one of {ACTIONS}")
            if ref in by_user and not _fix_progress(by_user[ref], verdict, action):
                ignored.append(ref)
                continue
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
        result = {"recorded": len(rows), "ignored": ignored}
        if ignored:
            result["note"] = USER_VERDICT_NOTE.format(refs=", ".join(ignored))
        return result
    finally:
        conn.close()


def record_verdicts(decision_id: str, verdicts: list[dict], *, source: str,
                    path: Path | None = None) -> int:
    """As :func:`record_verdicts_detail`; returns only the number of rows written."""
    return record_verdicts_detail(decision_id, verdicts, source=source, path=path)["recorded"]


def _fix_progress(user_verdict, verdict, action) -> bool:
    """Whether Claude's record is fix progress on a finding the user confirmed."""
    return user_verdict == "confirmed" and verdict == "confirmed" and action == "fixed"


def _effective_verdicts(conn, ids) -> dict:
    """{(decision_id, ref): the verdict that counts}, for these runs.

    The user's verdict outranks Claude's; otherwise the latest wins. Fix
    progress Claude recorded after the user confirmed a finding is carried
    onto the user's verdict (``action``, ``fix_verified``, and the evidence
    of the fix as ``fix_evidence``); the verdict and its source stay the user's.
    """
    latest: dict = {}
    if not ids:
        return latest
    marks = ",".join("?" * len(ids))
    for row in conn.execute(
            f"SELECT * FROM verdicts WHERE decision_id IN ({marks}) ORDER BY id", list(ids)):
        row = dict(row)
        key = (row["decision_id"], row["ref"])
        held = latest.get(key)
        if held is not None and held["source"] == "user" and row["source"] != "user":
            if _fix_progress(held["verdict"], row["verdict"], row["action"]):
                latest[key] = dict(held, action="fixed", fix_verified=row["fix_verified"],
                                   fix_evidence=row["evidence"])
            continue
        latest[key] = row
    return latest


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
        findings = []
        if ids:
            marks = ",".join("?" * len(ids))
            findings = [dict(row) for row in conn.execute(
                f"SELECT * FROM findings WHERE decision_id IN ({marks})", ids)]
        latest = _effective_verdicts(conn, ids)
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
        # Only calls in which Codex was started are Codex calls; the rest never reached it.
        "codex_started": sum(count for name, count in by_outcome.items() if name in STARTED_OUTCOMES),
        "answered": by_outcome.get(ANSWERED, 0),
        "not_started": sum(count for name, count in by_outcome.items()
                           if name not in STARTED_OUTCOMES),
        # Answers that did not use the findings format: their findings are unknown.
        "runs_with_unknown_findings": sum(1 for run in runs if run["outcome"] == ANSWERED
                                          and run["findings_count"] is None),
        "median_seconds": durations[len(durations) // 2] if durations else None,
        "total_seconds": round(sum(durations), 1) if durations else None,
        "input_tokens": sum(tokens_in) if tokens_in else None,
        "output_tokens": sum(tokens_out) if tokens_out else None,
        "findings": counts,
        "by_confidence": by_confidence,
        "gate": gate,
    }


def location_label(file, line, line_end=None) -> str:
    """``path:line``, ``path:start-end``, ``path``, or ``-`` for a point with no file."""
    if not file:
        return "-"
    if line is None:
        return str(file)
    return f"{file}:{line}-{line_end}" if line_end else f"{file}:{line}"


def list_findings(*, days: float = 30.0, workspace: str | None = None, limit: int = 20,
                  path: Path | None = None) -> list[dict]:
    """The recent calls that listed findings, newest first, with each finding's current verdict.

    What ``modelquad review verdict`` needs: the decision id and the finding
    references. Only what the ledger already keeps is shown: the claim is the
    stored, redacted and capped one, and no path outside the repository.
    """
    since = time.time() - days * 86400
    conn = connect(path)
    try:
        where, args = "created_at >= ? AND findings_count > 0", [since]
        if workspace:
            where += " AND workspace = ?"
            args.append(workspace)
        runs = [dict(row) for row in conn.execute(
            f"SELECT decision_id, created_at, task_type, trigger FROM runs WHERE {where} "
            "ORDER BY created_at DESC LIMIT ?", (*args, max(1, int(limit))))]
        ids = [run["decision_id"] for run in runs]
        latest = _effective_verdicts(conn, ids)
        found: dict[str, list[dict]] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for row in conn.execute(
                    f"SELECT * FROM findings WHERE decision_id IN ({marks})", ids):
                found.setdefault(row["decision_id"], []).append(dict(row))
    finally:
        conn.close()
    decisions = []
    for run in runs:
        items = []
        for row in sorted(found.get(run["decision_id"], ()),
                          key=lambda item: (len(item["ref"]), item["ref"])):
            verdict = latest.get((run["decision_id"], row["ref"]))
            items.append({
                "finding": row["ref"], "severity": row["severity"], "category": row["category"],
                "location": location_label(row["file"], row["line"], row["line_end"]),
                "claim": row["claim"],
                "verdict": verdict["verdict"] if verdict else None,
                "by": verdict["source"] if verdict else None,
                "action": verdict["action"] if verdict else None,
                "fix_verified": (None if verdict is None or verdict["fix_verified"] is None
                                 else bool(verdict["fix_verified"]))})
        decisions.append({"decision_id": run["decision_id"], "created_at": run["created_at"],
                          "task_type": run["task_type"], "trigger": run["trigger"],
                          "findings": items})
    return decisions
