# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Dev-only synthetic registry. Never launches a provider CLI or a model."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from modelquad import procutil
from modelquad.execution.base import ExecutionResult, Executor, WorkerTaskStatus
from modelquad.models import Freshness, ProviderStatus
from modelquad.registry import (ProviderCapabilities, ProviderPlugin, ProviderRegistry,
                                   UsageSourceKind, WORKSPACE_DEVELOPMENT_GRANT)
from modelquad.workerevidence import parse_codex_stream


class FakeUsage:
    def get_status(self):
        return ProviderStatus('codex', available=True, remaining_percentage=90,
                              observed_at=datetime.now(timezone.utc), freshness=Freshness.FRESH)


TREE_PROMPT = '\x1eMODELQUAD_TEST_TREE\x1f'
SLOW_PROMPT = '\x1eMODELQUAD_TEST_SLOW\x1f'

CHILD = r"""
import json, subprocess, sys, time
prompt = sys.stdin.read()
scenario = prompt.partition('\n')[0]
tree = scenario == '\x1eMODELQUAD_TEST_TREE\x1f'
slow = scenario == '\x1eMODELQUAD_TEST_SLOW\x1f'
if tree:
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(json.dumps({'type':'item.completed', 'item':{'type':'command_execution',
    'command':'private-command-canary', 'aggregated_output':'private-tool-output-canary'}}), flush=True)
if slow or tree:
    time.sleep(30)
print(json.dumps({'type':'item.completed', 'item':{'type':'agent_message',
    'text':'synthetic extracted answer'}}), flush=True)
"""


class FakeExecutor(Executor):
    name = 'codex'

    def is_available(self):
        return True

    def execute(self, request):
        try:
            completed = procutil.run_with_tree_kill(
                [sys.executable, '-c', CHILD], input=request.prompt,
                cwd=request.working_directory, timeout=request.timeout,
                env={**os.environ, 'MODELQUAD_WORKER': '1'},
                capture_output=True, text=True, encoding='utf-8',
                on_spawn=request.on_spawn, stop_requested=request.stop_requested,
            )
            parsed = parse_codex_stream(completed.stdout)
            facts = {key: getattr(completed, key) for key in ('worker_descendants',)
                     if getattr(completed, key, None) is not None}
            return ExecutionResult('codex', True, parsed.assistant_text, 0,
                                   task_status=WorkerTaskStatus.COMPLETED, metadata=facts)
        except (procutil.WorkerStopRequested, subprocess.TimeoutExpired) as error:
            stopped = isinstance(error, procutil.WorkerStopRequested)
            text = error.stdout or ''
            if isinstance(text, bytes):
                text = text.decode('utf-8', 'replace')
            return ExecutionResult('codex', False, parse_codex_stream(text).assistant_text, None,
                                   task_status=WorkerTaskStatus.PARTIAL,
                                   task_status_reason='stop_requested' if stopped else None,
                                   metadata={'stop_requested': stopped, 'timed_out': not stopped,
                                             'worker_termination': getattr(error, 'worker_termination', {})})


def registry():
    return ProviderRegistry((ProviderPlugin(
        id='codex', display_name='Synthetic Codex',
        capabilities=ProviderCapabilities(coding=True, testing=True, debugging=True, cli_execution=True),
        usage_source_kind=UsageSourceKind.LOCAL_LOGS,
        provider_factory=lambda context: FakeUsage(), executor_factory=lambda context: FakeExecutor(),
        capability_profile=dict(WORKSPACE_DEVELOPMENT_GRANT),
    ),))
