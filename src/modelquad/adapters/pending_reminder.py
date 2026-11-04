# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Metadata-only SessionStart context. No conversation files or state writes.

Two optional parts: one advisory line about Codex in this repository
(``repo_settings.advice_line``: the user's per-repo setting and the
repository's ``.modelquad.toml``, silent when nothing differs from the global
setting), and the list of retained delegated work. The hook never blocks or
approves, never opens the transcript path it is handed, and shares one budget.
"""

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
    # Before any file-system call: a network share or a device is never opened.
    from modelquad.localpath import reason as local_reason
    if local_reason(cwd) is not None:
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


def valid_cwd(cwd):
    if not (isinstance(cwd, str) and bool(cwd) and len(cwd) <= 4096
            and not any(ord(c) < 32 for c in cwd) and Path(cwd).is_absolute()):
        return False
    # A network share or a device folder is never opened from the hook.
    from modelquad.localpath import reason as local_reason
    return local_reason(cwd) is None


def repo_advice(cwd):
    """The one closed advisory line for this repository, or None.

    Reads only preferences.json, the installation id, the .git pointer files
    and the repository's .modelquad.toml; starts no process.
    """
    if not valid_cwd(cwd):
        return None
    from modelquad.repo_settings import advice_line
    return advice_line(cwd)


def run_hook(host, stdin, stdout, *, environ=None, clock=time.monotonic, fetch=None, advise=None):
    """Fail silent, including worker/input/transport/deadline failures.

    The repository line and the retained-work list are independent: either
    may appear alone, and a service that cannot answer leaves the line.
    """
    environ = os.environ if environ is None else environ
    if environ.get('MODELQUAD_WORKER') == '1':
        return 0
    deadline = clock() + HOOK_BUDGET
    try:
        # Claude Code is the one host (AGENTS.md); any other host id prints nothing.
        if host != 'claude':
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
        # The repository line first: closed text only (repo_settings.ADVICE),
        # never the event's. Kept when it was ready within the budget, even
        # if the retained-work read below runs out the rest of it.
        advice = None
        try:
            line = (advise or repo_advice)(event.get('cwd'))
            if line is not None:
                from modelquad.repo_settings import ADVICE, BROKEN_FILE_ADVICE
                if line in (*ADVICE.values(), BROKEN_FILE_ADVICE) and clock() < deadline:
                    advice = line
        except Exception:
            advice = None
        pending = None
        key = project_key(event.get('cwd'))
        if key is not None and clock() < deadline:
            try:
                if fetch is None:
                    from modelquad.jobs.service_client import read_pending_summary
                    fetch = read_pending_summary
                summary = fetch(key, deadline=deadline)
                validate_summary(summary)
                if summary['count'] and clock() < deadline:
                    pending = reminder_text(summary)
            except Exception:
                pending = None
        texts = [text for text in (advice, pending) if text]
        if not texts:
            return 0
        output = json.dumps(serialize_claude('\n'.join(texts)), separators=(',', ':')) + '\n'
        if len(output.encode('utf-8')) <= BYTE_CAP and (advice or clock() < deadline):
            stdout.write(output)
    except Exception:
        pass  # No diagnostic text, state marker or user-facing message.
    return 0
