# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Transient omission transport. No disk, logging or history writer."""

from collections import OrderedDict
import json
import threading
import time

BODY_CAP = 65536
TTL_SECONDS = 600
MAX_JOBS = 16
TOTAL_CAP = 256 * 1024
REASONS = frozenset({
    'file_byte_cap', 'total_byte_cap', 'read_byte_cap', 'rendered_prompt_cap',
    'elapsed_work_cap', 'candidate_count_cap', 'omission_count_cap', 'file_count_cap', 'missing_file',
    'linked_path', 'not_regular_file', 'file_changed', 'binary_file',
    'undecodable_file', 'secret_file', 'file_identity_unavailable',
    'outside_workspace', 'file_unavailable', 'invalid_relative_path', 'repository_notes_source',
})


def validate(items):
    """Reconstruct typed guidance; never accept arbitrary prose or metadata."""
    from modelquad.execution.noshell import OmissionDiagnostic, MAX_SKIPPED, canonical_path
    if type(items) is not list or len(items) > MAX_SKIPPED + 1:
        raise ValueError('invalid_diagnostics')
    result = []
    for item in items:
        if (type(item) is not dict or set(item) != {'path', 'reason', 'required'}
                or type(item['path']) is not str or len(item['path'].encode('utf-8')) > 1024
                or type(item['reason']) is not str or item['reason'] not in REASONS
                or type(item['required']) is not bool):
            raise ValueError('invalid_diagnostics')
        name = item['path']
        if name not in ('<path>', '<prompt>', '<remaining references, if any>'):
            if canonical_path(name) != name or any(ord(char) < 32 for char in name):
                raise ValueError('invalid_diagnostics')
        result.append(OmissionDiagnostic(name, item['reason'], item['required']))
    return tuple(result)


def wire(diagnostics):
    from modelquad.execution.bound_launch import host_diagnostic_payload
    return [{key: item[key] for key in ('path', 'reason', 'required')}
            for item in host_diagnostic_payload(diagnostics)]


class TransientDiagnostics:
    """Bounded service-memory cache; expires or disappears on service restart."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._rows = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self):
        now = self._clock()
        for key in list(self._rows):
            if self._rows[key][0] <= now:
                del self._rows[key]

    def put(self, job_id, diagnostics):
        size = len(json.dumps(wire(diagnostics)).encode('utf-8'))
        if size > BODY_CAP:
            raise ValueError('invalid_diagnostics')
        with self._lock:
            self._expire()
            self._rows.pop(job_id, None)
            while self._rows and (len(self._rows) >= MAX_JOBS
                                  or sum(row[1] for row in self._rows.values()) + size > TOTAL_CAP):
                self._rows.popitem(last=False)
            self._rows[job_id] = (self._clock() + TTL_SECONDS, size, diagnostics)

    def get(self, job_id):
        with self._lock:
            self._expire()
            row = self._rows.get(job_id)
            return None if row is None else row[2]


INLINE = TransientDiagnostics()


def inline_key(db_path, job_id):
    from modelquad.service.api import state_identity_tag
    from pathlib import Path
    return (state_identity_tag(Path(db_path).resolve()), job_id)
