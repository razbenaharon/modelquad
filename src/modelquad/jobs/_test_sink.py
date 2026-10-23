# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic file handoff, enabled only by the runner's test-only guard.

Contains output only in the caller-selected test directory, never in logs or
history. This is not a retained-output store or a production sink contract.
"""

from collections.abc import Mapping
from datetime import datetime
import json
from pathlib import Path
import re
import tempfile

from modelquad.jobs.seam import JobOutput


def _json_facts(value):
    if isinstance(value, Mapping):
        return {key: _json_facts(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_facts(item) for item in value]
    return value


class FileSink:
    """Write synthetic JobOutput JSON atomically without logging its content."""

    def __init__(self, directory: Path):
        self.directory = directory

    def put(self, output: JobOutput, now: datetime) -> bool:
        # Job ids are opaque to the seam; never let one become a path.
        if not re.fullmatch(r'[A-Za-z0-9_-]+', output.job.job_id):
            raise ValueError('invalid test sink job id')
        value = {
            'job_id': output.job.job_id, 'status': output.job_status.value,
            'claim': output.worker_claim.value, 'reason': output.claim_reason,
            'text': output.output_text, 'partial': output.partial,
            'death': output.tree_confirmed_dead,
            'descendants': _json_facts(output.descendants),
            'termination': _json_facts(output.termination),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.tmp',
                                             dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream)
            temporary.replace(self.directory / (output.job.job_id + '.json'))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True
