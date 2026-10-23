# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""SQLite schema and connection helpers for the local control-plane service.

This is the only persistence layer for history/state. It never stores
prompts, secrets, or credentials -- only normalized provider/routing/execution
observations that are already safe to display to a client of the local service.

There are no migrations. A new database is created at ``SCHEMA_VERSION``
from the DDL below. A database left by a pre-release build (an older
``PRAGMA user_version``) is refused with :class:`PreReleaseDatabase`, which
says to move it aside; ModelQuad then creates a new one.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import paths

# Resolves to ~/.orchestrator/orchestrator.db in a dev checkout, and %LOCALAPPDATA%\MultiAgentOrchestrator\orchestrator.db in installed mode.
DEFAULT_DB_PATH = paths.db_path()

#: The settings-table key for the global routing pause, and its default.
ROUTING_PAUSED_KEY = "routing_paused"
DEFAULT_ROUTING_PAUSED = False

# PRAGMA user_version records which schema a database file was created with.
SCHEMA_VERSION = 24

#: Notes on the DDL below.
#:
#: * ``idx_delegation_attempts_active_request`` is the database-level half of
#:   duplicate-worker prevention: "at most one ACTIVE attempt per logical
#:   request" is an invariant SQLite enforces, so the loser of a race between
#:   the in-flight check and the INSERT is rejected at INSERT time instead of
#:   becoming a second worker on one workspace. It is scoped to
#:   ``status='started'`` and to ``request_key IS NOT NULL``.
#: * ``outcome_revisions`` is append-only delayed evidence: an execution's
#:   immediate outcome is never mutated; anything learned later lands there.
#: * ``job_fate_releases`` is immutable, and its triggers fence the job and
#:   attempt rows it releases.
_SCHEMA = """

CREATE TABLE IF NOT EXISTS provider_usage_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    observed_at TEXT,
    recorded_at TEXT NOT NULL,
    remaining_percentage REAL,
    freshness TEXT NOT NULL,
    available INTEGER NOT NULL,
    usage_source TEXT NOT NULL DEFAULT 'unknown',
    metadata_json TEXT,
    resource_pool TEXT
);

CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    task_id TEXT,
    task_type TEXT NOT NULL,
    recommendation TEXT,
    selected_provider TEXT,
    overridden INTEGER NOT NULL DEFAULT 0,
    override_reason TEXT,
    providers_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'cli',
    invocation_mode TEXT,
    invocation_mode_source TEXT,
    host TEXT,
    model_recommended TEXT,
    reasoning_effort_recommended TEXT,
    decision_source TEXT,
    expected_consumption_bucket TEXT,
    expected_consumption_source TEXT,
    resource_pool_recommended TEXT,
    agent_runtime_recommended TEXT,
    decision_id TEXT,
    reason TEXT,
    evidence_json TEXT,
    plan_decision_id INTEGER
);

CREATE TABLE IF NOT EXISTS execution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    task_id TEXT,
    task_type TEXT NOT NULL,
    project TEXT,
    provider TEXT NOT NULL,
    session_id TEXT,
    success INTEGER,
    exit_code INTEGER,
    duration_seconds REAL,
    error TEXT,
    source TEXT NOT NULL DEFAULT 'cli',
    result TEXT,
    fallback INTEGER NOT NULL DEFAULT 0,
    fallback_from TEXT,
    metadata_json TEXT,
    task_status TEXT,
    task_status_reason TEXT,
    invocation_mode TEXT,
    invocation_mode_source TEXT,
    host TEXT,
    model_requested TEXT,
    model_confirmed TEXT,
    reasoning_effort_requested TEXT,
    outcome_evidence_json TEXT,
    resource_pool TEXT,
    agent_runtime TEXT,
    decision_id TEXT
);

CREATE TABLE IF NOT EXISTS provider_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    available INTEGER NOT NULL,
    cli_detected INTEGER,
    adapter_installed INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_provider_recorded
    ON provider_usage_history (provider, recorded_at);
CREATE INDEX IF NOT EXISTS idx_routing_created ON routing_decisions (created_at);
CREATE INDEX IF NOT EXISTS idx_execution_created ON execution_history (created_at);
CREATE INDEX IF NOT EXISTS idx_routing_decision_id ON routing_decisions (decision_id);
CREATE INDEX IF NOT EXISTS idx_execution_decision_id ON execution_history (decision_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    attempt_id INTEGER NOT NULL UNIQUE,
    decision_id TEXT,
    runner_pid INTEGER,
    runner_created REAL,
    worker_pid INTEGER,
    launch_limitation TEXT CHECK(launch_limitation IS NULL OR
        launch_limitation = 'contained_innermost_job_without_kill_on_close'),
    status TEXT NOT NULL CHECK(status IN
        ('starting','running','finished','failed','stopped','interrupted','timed_out','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    heartbeat_at TEXT,
    ended_at TEXT,
    worker_created REAL,
    recovery_claim TEXT,
    recovery_claimed_at TEXT,
    recovery_ended_at TEXT,
    recovery_decision TEXT,
    reservation_boot_id TEXT,
    reservation_uptime_ms INTEGER,
    reservation_boot_qualified INTEGER
);

CREATE TABLE IF NOT EXISTS plan_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    task_type TEXT NOT NULL,
    decision_mode TEXT NOT NULL,
    planner_provider TEXT,
    planner_model TEXT,
    planner_model_source TEXT,
    reasoning_source TEXT,
    strategy TEXT,
    confidence REAL,
    steps_json TEXT,
    reason_summary TEXT,
    validation_passed INTEGER,
    validation_errors_json TEXT,
    executed INTEGER NOT NULL DEFAULT 0,
    fallback_occurred INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT,
    deterministic_recommendation TEXT,
    agreement INTEGER,
    final_status TEXT,
    duration_seconds REAL,
    replan_used INTEGER NOT NULL DEFAULT 0,
    packet_json TEXT,
    source TEXT NOT NULL DEFAULT 'mcp',
    host TEXT,
    invocation_mode TEXT,
    decision_id TEXT,
    reasoning_attempted INTEGER,
    reasoning_status TEXT,
    reasoning_started_at TEXT,
    reasoning_completed_at TEXT,
    engine_versions_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_decisions_created ON plan_decisions (created_at);
CREATE INDEX IF NOT EXISTS idx_plan_decisions_decision_id ON plan_decisions (decision_id);

CREATE TABLE IF NOT EXISTS delegation_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    task_type TEXT NOT NULL,
    requested_provider TEXT,
    routed_provider TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    failure_kind TEXT,
    detail TEXT,
    duration_seconds REAL,
    source TEXT NOT NULL DEFAULT 'mcp',
    host TEXT,
    invocation_mode TEXT,
    worker_pid INTEGER,
    request_key TEXT,
    verification_started_at TEXT,
    verification_resolved_at TEXT,
    caller_abandoned_at TEXT,
    caller_abandonment_reason TEXT,
    workspace_key TEXT,
    workspace_ancestors TEXT,
    worker_descendants TEXT,
    repo_key TEXT,
    policy_version INTEGER,
    capability TEXT,
    target_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_delegation_attempts_created ON delegation_attempts (created_at);
CREATE INDEX IF NOT EXISTS idx_delegation_attempts_decision_id ON delegation_attempts (decision_id);
CREATE INDEX IF NOT EXISTS idx_delegation_attempts_request_key ON delegation_attempts (request_key);
CREATE INDEX IF NOT EXISTS idx_delegation_attempts_workspace ON delegation_attempts (workspace_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_attempts_active_request ON delegation_attempts (request_key) WHERE request_key IS NOT NULL AND status = 'started';

CREATE TABLE IF NOT EXISTS outcome_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT,
    execution_id INTEGER,
    created_at TEXT NOT NULL,
    revision_reason TEXT NOT NULL,
    new_semantic_status TEXT,
    outcome_source TEXT NOT NULL,
    detail TEXT,
    assessor_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcome_revisions_decision_id ON outcome_revisions (decision_id);
CREATE INDEX IF NOT EXISTS idx_outcome_revisions_execution_id ON outcome_revisions (execution_id);

CREATE TABLE IF NOT EXISTS repo_registrations (
    repo_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    remote_fingerprint TEXT,
    state TEXT NOT NULL CHECK(state IN ('active','paused','reactivation_required')),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repo_policy_versions (
    repo_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('window','default')),
    PRIMARY KEY (repo_key, version)
);

CREATE TABLE IF NOT EXISTS job_fate_releases (
    job_id TEXT PRIMARY KEY,
    attempt_id INTEGER NOT NULL UNIQUE CHECK(attempt_id > 0),
    decision_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    released_at TEXT NOT NULL,
    actor TEXT NOT NULL CHECK(length(actor) BETWEEN 1 AND 100),
    authentication_method TEXT NOT NULL CHECK(authentication_method IN ('installation_token','local_recovery')),
    surface TEXT NOT NULL CHECK(surface IN ('ui','cli','recovery')),
    reason TEXT NOT NULL CHECK(reason IN ('boot_after_start','worker_pid_gone',
        'worker_pid_reused','owner_released_unknown_fate')),
    proof_pid INTEGER CHECK(proof_pid IS NULL OR proof_pid BETWEEN 1 AND 4294967295),
    recorded_created REAL,
    observed_created REAL,
    reservation_boot_id TEXT,
    observed_boot_id TEXT,
    observed_uptime_ms INTEGER,
    boot_lower TEXT,
    boot_upper TEXT
);
CREATE TRIGGER IF NOT EXISTS fate_no_update BEFORE UPDATE ON job_fate_releases BEGIN SELECT RAISE(ABORT, 'immutable fate release'); END;
CREATE TRIGGER IF NOT EXISTS fate_no_delete BEFORE DELETE ON job_fate_releases BEGIN SELECT RAISE(ABORT, 'immutable fate release'); END;
CREATE TRIGGER IF NOT EXISTS fate_fence_jobs_update BEFORE UPDATE ON jobs WHEN EXISTS (SELECT 1 FROM job_fate_releases WHERE job_id=OLD.job_id) BEGIN SELECT RAISE(ABORT, 'job fate released'); END;
CREATE TRIGGER IF NOT EXISTS fate_fence_jobs_delete BEFORE DELETE ON jobs WHEN EXISTS (SELECT 1 FROM job_fate_releases WHERE job_id=OLD.job_id) BEGIN SELECT RAISE(ABORT, 'job fate released'); END;
CREATE TRIGGER IF NOT EXISTS fate_fence_delegation_attempts_update BEFORE UPDATE ON delegation_attempts WHEN EXISTS (SELECT 1 FROM job_fate_releases WHERE attempt_id=OLD.id) BEGIN SELECT RAISE(ABORT, 'job fate released'); END;
CREATE TRIGGER IF NOT EXISTS fate_fence_delegation_attempts_delete BEFORE DELETE ON delegation_attempts WHEN EXISTS (SELECT 1 FROM job_fate_releases WHERE attempt_id=OLD.id) BEGIN SELECT RAISE(ABORT, 'job fate released'); END;"""


class PreReleaseDatabase(sqlite3.DatabaseError):
    """A history database from a pre-release build, which this build does not read."""

    def __init__(self, version: int) -> None:
        super().__init__(
            f"This history database is from a pre-release ModelQuad build (schema version {version}). "
            "Move it aside; ModelQuad will create a new one."
        )
        self.version = version


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating parent directories as needed) a WAL-mode connection."""

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema in a new database; refuse a pre-release one.

    ``connect_and_init`` is called on essentially every history read/write,
    so this checks ``PRAGMA user_version`` -- a cheap read of the database
    header, not a write -- first and runs no DDL once the schema exists.
    """

    try:
        if conn.in_transaction:
            conn.commit()
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version >= SCHEMA_VERSION:
            return
        if current_version != 0:
            raise PreReleaseDatabase(current_version)
        # Every statement uses IF NOT EXISTS, so concurrent creation is idempotent.
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def connect_and_init(path: str | Path) -> sqlite3.Connection:
    """Open a connection and ensure its schema exists in one step."""

    conn = connect(path)
    try:
        init_db(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def read_routing_paused(db_path: str | Path) -> bool:
    """Return persisted global routing pause state.

    Pausing blocks all new provider work. It does not stop existing workers,
    disable providers, change routing_mode, or affect usage collection. Resuming
    restores the previously configured routing_mode untouched (pause is an
    orthogonal boolean, not a routing_mode value).

    Missing, invalid, or unreadable values fall back to DEFAULT_ROUTING_PAUSED (False).
    Shared by ``history.get_routing_paused`` and the light hook-gate entry,
    which must not import the history module.
    """
    try:
        conn = connect_and_init(db_path)
    except (sqlite3.Error, OSError):
        return DEFAULT_ROUTING_PAUSED
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (ROUTING_PAUSED_KEY,)
        ).fetchone()
        if row is None:
            return DEFAULT_ROUTING_PAUSED
        try:
            decoded = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError, ValueError):
            decoded = row["value"]
        if isinstance(decoded, bool):
            return decoded
        if isinstance(decoded, str):
            if decoded.lower() == "true":
                return True
            if decoded.lower() == "false":
                return False
        return DEFAULT_ROUTING_PAUSED
    except (sqlite3.Error, OSError):
        return DEFAULT_ROUTING_PAUSED
    finally:
        conn.close()
