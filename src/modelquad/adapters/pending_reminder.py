# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Metadata-only SessionStart context. No conversation files or state writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from datetime import datetime

BYTE_CAP = 32 * 1024
ROW_CAP = 20
HOOK_BUDGET = 1.5
CODEX_DELIVERY_EVIDENCE = 'one recorded run, codex-cli 0.162.0-alpha.2'
_TASKS = frozenset({'architecture', 'implementation', 'exploration', 'debugging',
                    'testing', 'review', 'trivial'})
_STATUSES = frozenset({'finished', 'failed', 'stopped', 'interrupted', 'timed_out', 'unknown'})
_JOB_FIELDS = frozenset({'job_id', 'job_status', 'task_type', 'may_have_changed_files',
                         'expires_at', 'expiring_soon'})


def valid_workspace_key(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) is not None


def project_key(cwd):
    # Never fall back to '.', a repo root or a guessed folder.
    if (not isinstance(cwd, str) or not cwd or len(cwd) > 4096
            or any(ord(c) < 32 for c in cwd) or not Path(cwd).is_absolute()):
        return None
    from modelquad.service.history import workspace_identity
    key, _ = workspace_identity(cwd)
    return key if valid_workspace_key(key) else None


def validate_summary(payload):
    """Closed wire contract. Arbitrary labels/content never enter context."""
    if (not isinstance(payload, dict) or set(payload) != {'count', 'more', 'jobs'}
            or type(payload['count']) is not int or not 0 <= payload['count'] <= 256
            or type(payload['more']) is not bool or not isinstance(payload['jobs'], list)
            or len(payload['jobs']) != min(payload['count'], ROW_CAP)
            or payload['more'] != (payload['count'] > ROW_CAP)):
        raise ValueError('pending_summary_invalid')
    ids = set()
    for row in payload['jobs']:
        if (not isinstance(row, dict) or set(row) != _JOB_FIELDS
                or not isinstance(row['job_id'], str)
                or not re.fullmatch('[A-Za-z0-9_-]{1,128}', row['job_id'])
                or row['job_id'] in ids or row['job_status'] not in _STATUSES
                or row['task_type'] not in _TASKS
                or type(row['may_have_changed_files']) is not bool
                or type(row['expiring_soon']) is not bool
                or not isinstance(row['expires_at'], str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', row['expires_at'])):
            raise ValueError('pending_summary_invalid')
        datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
        ids.add(row['job_id'])
    if len(json.dumps(payload).encode('utf-8')) > BYTE_CAP:
        raise ValueError('pending_summary_invalid')
    return payload


def reminder_text(summary):
    validate_summary(summary)
    boolean = lambda v: 'true' if v else 'false'
    lines = [f"ModelQuad retained delegated work for this project: count={summary['count']}; more={boolean(summary['more'])}."]
    lines.extend(
        f"job_id={r['job_id']}; status={r['job_status']}; task_type={r['task_type']}; "
        f"may_have_changed_files={boolean(r['may_have_changed_files'])}; "
        f"expires_at={r['expires_at']}; expiring_soon={boolean(r['expiring_soon'])}."
        for r in summary['jobs'])
    lines.append(
        'Host agent: refresh with modelquad_job_list_pending(project_directory=current absolute project directory). '
        'Collect relevant handles with modelquad_job_collect, independently verify the result, '
        'and tell the user what you found. Inspect possible project changes; explain observed '
        'changes before building on them. Acknowledge with modelquad_job_acknowledge using the '
        'collection receipt only after use. If collection is expired or acknowledged, report '
        'that race truthfully. If verification fails, report the failure; do not build on or '
        'acknowledge an unused result. Never auto-apply output or acknowledge because this hook fired. '
        'A finished status does not establish success or that all processes ended.')
    return '\n'.join(lines)


def serialize_claude(text):
    return {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}


def serialize_codex(text):
    # Same envelope, qualified by a native Codex delivery run.
    # Keep serializers separate for future provider schema changes.
    return {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}


def run_hook(host, stdin, stdout, *, environ=None, clock=time.monotonic, fetch=None):
    """Fail silent, including worker/input/transport/deadline failures."""
    environ = os.environ if environ is None else environ
    if environ.get('MODELQUAD_WORKER') == '1':
        return 0
    deadline = clock() + HOOK_BUDGET
    try:
        if host not in {'claude', 'codex'}:
            return 0
        # Pipe reads can block even with a byte cap. The daemon reads only the
        # bounded payload; the calling thread waits only the remaining budget.
        incoming = queue.Queue(maxsize=1)

        def read_input():
            try:
                incoming.put(stdin.read(BYTE_CAP + 1))
            except Exception:
                incoming.put(None)

        threading.Thread(target=read_input, daemon=True).start()
        raw = incoming.get(timeout=max(0, deadline - clock()))
        if not isinstance(raw, (bytes, str)):
            return 0
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        if len(raw) > BYTE_CAP or clock() >= deadline:
            return 0
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get('hook_event_name') != 'SessionStart':
            return 0
        key = project_key(event.get('cwd'))
        if key is None or clock() >= deadline:
            return 0
        if fetch is None:
            from modelquad.jobs.service_client import read_pending_summary
            fetch = read_pending_summary
        summary = fetch(key, deadline=deadline)
        validate_summary(summary)
        if not summary['count'] or clock() >= deadline:
            return 0
        serializer = serialize_claude if host == 'claude' else serialize_codex
        output = json.dumps(serializer(reminder_text(summary)), separators=(',', ':')) + '\n'
        if len(output.encode('utf-8')) <= BYTE_CAP and clock() < deadline:
            stdout.write(output)
    except Exception:
        pass  # No diagnostic text, state marker or user-facing message.
    return 0
