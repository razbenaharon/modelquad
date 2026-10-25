# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Test-only child entry points and executor, using the existing offline seam."""

import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess

from fixtures import fake_executor_plugin as fake
from modelquad.registry import (
    ProviderCapabilities, ProviderPlugin, ProviderRegistry, UsageSourceKind,
    WORKSPACE_DEVELOPMENT_GRANT,
)
from modelquad import executable_resolution, workerevidence

# Unlike the in-process fixtures, a standalone child does not inherit
# conftest's probe isolation. Never run a real CLI, even for its version.
workerevidence._probe_version = lambda executable: None
workerevidence.runtime_supports_structured_output = lambda *args, **kwargs: None
_popen = subprocess.Popen


class PythonOnlyPopen(_popen):
    # Keep Popen a class: the Windows MCP SDK uses Popen[bytes] annotations.
    def __init__(self, argv, *args, **kwargs):
        assert not kwargs.get('shell'), 'HL-1 children must not start a shell'
        assert isinstance(argv, (list, tuple)), 'HL-1 requires explicit argv'
        # Source-mode /api/health reads its build SHA; this is the only non-Python
        # command allowed, and cannot write Git or contact a remote.
        git_head = (len(argv) == 6 and argv[1] == '-C'
                    and list(argv[3:]) == ['rev-parse', '--verify', 'HEAD']
                    and argv[0] == executable_resolution.resolve_executable(
                        'git', exclude_roots=(argv[2],)).path)
        assert git_head or Path(argv[0]).resolve() == Path(sys.executable).resolve(), 'HL-1 refuses provider and other executable calls'
        super().__init__(argv, *args, **kwargs)


subprocess.Popen = PythonOnlyPopen

ANSWER = 'HL-1 synthetic survived answer'
WORK_SECONDS = 8
PROBE_PROMPT = '\x1eMODELQUAD_TEST_HL1_PROBE\x1f'
fake.CHILD = r'''
import json, sys, time
from pathlib import Path
scenario = sys.stdin.read().partition('\n')[0]
probe = scenario == '\x1eMODELQUAD_TEST_HL1_PROBE\x1f'
Path('probe.started' if probe else 'work.started').write_text(str(__import__('os').getpid()))
time.sleep(.05 if probe else 8)
Path('probe.marker' if probe else 'work.marker').write_text('finished', encoding='utf-8')
print(json.dumps({'type': 'item.completed', 'item': {
    'type': 'agent_message', 'text': 'HL-1 synthetic survived answer'}}), flush=True)
'''


def registry():
    return ProviderRegistry((ProviderPlugin(
        id='codex', display_name='HL-1 synthetic worker',
        capabilities=ProviderCapabilities(coding=True, testing=True, debugging=True, cli_execution=True),
        usage_source_kind=UsageSourceKind.LOCAL_LOGS,
        provider_factory=lambda context: fake.FakeUsage(),
        executor_factory=lambda context: fake.FakeExecutor(),
        capability_profile=dict(WORKSPACE_DEVELOPMENT_GRANT),
    ),))


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    # Explicit here as well as inherited from conftest: no crash dialog in any child.
    if os.name == 'nt':
        import ctypes
        ctypes.WinDLL('kernel32').SetErrorMode(0x0001 | 0x0002 | 0x8000)
    os.environ.pop('MODELQUAD_WORKER', None)
    role, root_text = sys.argv[1:3]
    root = Path(root_text)
    from modelquad.service import db
    db.DEFAULT_DB_PATH = root / 'history.db'
    if role == 'service':
        import uvicorn
        from modelquad import paths
        from modelquad.jobs import handle
        from modelquad.service import auth
        from modelquad.service.app import create_app
        ok, reason = handle.detached_launch_verdict()
        patched = not ok and os.environ.get('HL1_TEST_PATCH_SERVICE_VERDICT') == '1'
        observation = {'verdict_patched': patched, 'original_verdict': {'ok': ok, 'reason': reason},
                       'pid': os.getpid(), 'job_flags': hex(handle._caller_job_limits())
                       if handle._caller_in_job() else '0x0'}
        (root / 'service-observation.json').write_text(json.dumps(observation), encoding='utf-8')
        if patched:
            # Test-only admission override. Launch policy and real runner remain untouched.
            handle.detached_launch_verdict = lambda: (True, 'not_in_job')
        secret = auth.ensure_service_secret(paths.state_root())
        assert secret.usable, secret.problem
        app = create_app(db_path=db.DEFAULT_DB_PATH, config_path=root / 'config.yaml',
                         poll_providers=False, write_secret=secret)
        uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[3]), log_level='warning')
    elif role in ('host', 'fallback'):
        from modelquad.mcp import server
        server.CONFIG_PATH = str(root / 'config.yaml')
        server.USAGE_DIRECTORY = str(root / 'usage')
        # Return a handle while eight-second fake work is running. The product's
        # normal 20-second shortcut otherwise collects it before Host death.
        server.JOB_WAIT_SECONDS = .05

        async def delegate(prompt):
            result = await server.modelquad_delegate(
                'review', prompt, str(root), timeout=45)
            return result.structured_content

        async def host():
            if role == 'fallback':
                # The note is returned only AFTER synchronous work ends. Prove it
                # on a short call first, then kill a second call while it works.
                payload = await delegate(PROBE_PROMPT)
                assert not payload.get('refused'), payload
                assert payload.get('runner_unavailable_reason') in ('service_absent', 'service_unreachable'), payload
                emit({'runner_note': payload['runner_note'],
                      'reason': payload['runner_unavailable_reason'], 'probe_output': payload['output']})
            payload = await delegate('survival')
            assert not payload.get('refused'), payload
            if role == 'host':
                # The service accepted the launch: no in-session fallback note.
                assert 'runner_note' not in payload, payload
            emit({'job_id': payload['job_id']} if role == 'host' else {'unexpected_completion': True})
            while True:
                await asyncio.sleep(1)
        asyncio.run(host())
    elif role == 'collect':
        from modelquad.mcp import jobs_tools
        job_id = sys.argv[3]
        collected = jobs_tools.modelquad_job_collect(job_id, str(root))
        assert collected['output'] == ANSWER, collected
        assert collected['job_status'] == 'finished', collected
        acknowledged = jobs_tools.modelquad_job_acknowledge(job_id, collected['receipt'], str(root))
        assert acknowledged['acknowledged'], acknowledged
        assert jobs_tools.modelquad_job_list_pending(str(root))['jobs'] == []
        emit({'pid': os.getpid(), 'collected': True, 'acknowledged': True, 'pending_empty': True})
    else:
        raise ValueError(role)


if __name__ == '__main__':
    main()
