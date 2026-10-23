# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Closed service-answer classification and unclaimed handoff closure.

Review/delegation retain the J2 containment, runner-disabled and stale-window
fallbacks.
"""

from datetime import datetime, timezone
from http.client import HTTPException
import logging
from pathlib import Path
import sqlite3

from ..service import db, history


def launch_answer(client, job_id, spec_name):
    """Only I/O exceptions establish transport ambiguity; never retain bodies."""
    try:
        return client.launch(job_id, spec_name)
    except (OSError, HTTPException) as error:
        logging.getLogger(__name__).debug('service handoff unavailable: %s', type(error).__name__)
        return 'ambiguous', 'connection_error'
    except Exception as error:
        logging.getLogger(__name__).debug('service handoff refused: %s', type(error).__name__)
        return 'refused', 'refused_other'


def refusal_label(outcome, reason):
    """None permits the ownership CAS; all other answers are terminal labels."""
    # A closed 5xx (runner_launch_failed/refused_other) is terminal even if
    # called ambiguous.
    if outcome == 'ambiguous' and type(reason) is str and reason in {
        'timeout', 'connection_error', 'bad_response', 'launch_unavailable',
        'service_absent', 'service_unreachable', 'service_connection_changed',
    }:
        return None
    # A stale service admission window does not claim or refuse the job.
    # Inline admission/linkage checks still apply; the CAS decides ownership.
    if outcome == 'refused' and reason in (
        'service_contained', 'job_runner_disabled', 'job_not_launchable',
    ):
        return None
    if outcome == 'refused' and reason == 'launch_rate_limited':
        return 'service_busy'
    return 'service_refused_launch'


def close_refused_handoff(db_path, ref, label, capability):
    """Atomically close both records only while the reservation is unclaimed.

    The capability is explicit. An existing owner/recovery/terminal row keeps
    its lifecycle and its spec.
    Only ordinary delegation accepts legacy attempts with no capability.
    """
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
    try:
        if conn.execute('PRAGMA user_version').fetchone()[0] != db.SCHEMA_VERSION:
            raise ValueError('launch_store_unavailable')
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE job_id=? AND attempt_id=? AND decision_id IS ? "
            "AND status='starting' AND runner_pid IS NULL AND heartbeat_at IS NULL "
            "AND ended_at IS NULL AND recovery_claim IS NULL",
            (ref.job_id, ref.attempt_id, ref.decision_id),
        ).fetchone()
        if row is None:
            return False
        stamp = datetime.now(timezone.utc).isoformat()
        conn.create_function('resolved_attempt_detail', 2, history._resolved_attempt_detail)
        changed = conn.execute(
            "UPDATE delegation_attempts SET status='failed', failure_kind='failed_to_start', "
            "outcome='unknown', resolved_at=?, detail=resolved_attempt_detail(detail, ?) "
            "WHERE id=? AND decision_id IS ? "
            "AND (capability=? OR (?='delegate' AND capability IS NULL)) "
            "AND status='started' AND resolved_at IS NULL",
            (stamp, label, ref.attempt_id, ref.decision_id, capability, capability),
        )
        if changed.rowcount != 1:
            raise ValueError('launch_linkage_invalid')
        conn.execute("UPDATE jobs SET status='failed', ended_at=?, updated_at=? WHERE job_id=?",
                     (stamp, stamp, ref.job_id))
        conn.commit()
        return True
    finally:
        conn.close()  # Uncommitted writes roll back, including linkage failures.
