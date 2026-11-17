# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Closed vocabularies for what leaves a Codex run.

A worker's answer is the only free text a reply carries. Reasons, limitation
labels, the error and every metadata value are re-projected here onto fixed
sets, so nothing a worker echoed (a path, a file's line, a prompt) can reach
a reply field or the review ledger by another name.
"""

from __future__ import annotations

from dataclasses import replace
import math
import os
import re

#: The reasoning efforts Codex accepts, as a union across its models.
CODEX_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")


def safe_model_id(value):
    """The model id when it is a plain, bounded id (Codex's ``/`` namespace allowed), else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _MODEL_ID.match(value.replace("/", ":")) else None


def closed_reason(reason, *, capability='delegate'):
    """No ordinary prose survives a worker echo into a persisted reason."""
    from .base import ATTEMPT_FAILURE_REASONS, POLICY_REFUSAL_REASONS
    from .noshell import REFUSAL_LABELS as NOSHELL_REFUSAL_LABELS
    allowed = {*ATTEMPT_FAILURE_REASONS, *POLICY_REFUSAL_REASONS,
               *NOSHELL_REFUSAL_LABELS,
               'task_type_not_read_only', 'worker_reported',
               'review_completed', 'review_worker_reported', 'review_malformed',
               'review_correction_failed', 'correction_failed', 'correction_completed',
               'independence_unavailable', 'requested_tests_not_verified'}
    allowed.update({'policy_refused', 'no_completion_envelope',
                    'stdin_write_failed', 'timed_out', 'unsafe_executable', 'task_not_completed',
                    'worker_exception', 'completion_unconfirmed', 'process_failed', 'engine_start_failed',
                    'duplicate_in_flight', 'attempt_unrecorded', 'launch_state_unavailable',
                    'review_block_invalid', 'review_findings_invalid', 'review_correction_failed'})
    # Translate trusted launch wording in the packet's own capability domain.
    # Already closed reasons pass unchanged; worker prose never does.
    translated = reason if type(reason) is str and reason in allowed else None
    return None if reason is None else translated if translated in allowed else 'worker_reported'


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def closed_limitations(values):
    """Keep fixed limitation labels; worker prose can quote supplied files."""
    allowed = {'worker_command_refusal_not_enforced', 'diagnosis_is_not_verification',
               'live_workspace_not_isolated', 'worker_reads_not_confined',
               'inline_runner_host_bound', 'diagnosis_truncated',
               'worker_has_no_command_tools', 'noshell_verified_by_event_stream', 'noshell_violated',
               'review_is_not_verification', 'frozen_execution_not_isolated',
               'correction_failed', 'provider_independence_unknown',
               'provider_independence_same_provider', 'session_strategy_overrides_prefer_role'}
    return tuple(v for v in values if type(v) is str and v in allowed)


def closed_result(result):
    """The result with closed metadata, reason, error and limitations; the answer is kept.

    Worker dictionaries are never copied: their keys and values can be
    supplied prose. An unknown process image name is withheld and the census
    becomes uncertain, rather than its loss claiming a settled workspace.
    """
    from .base import POLICY_REFUSAL_REASONS
    from .noshell import closed_file_audit

    metadata = result.metadata
    clean = {}
    for name in ('worker_started', 'timed_out', 'stop_requested', 'noshell_qualified',
                 'noshell_unified_exec_observed', 'noshell_verified_by_event_stream',
                 'noshell_violated', 'noshell_collaboration_called'):
        if type(metadata.get(name)) is bool:
            clean[name] = metadata[name]
    tokens = metadata.get('worker_tokens')
    if type(tokens) is dict:  # Codex's own token counts: closed keys, integers only
        counts = {key: tokens[key] for key in ('input_tokens', 'cached_input_tokens', 'output_tokens',
                                               'reasoning_output_tokens')
                  if type(tokens.get(key)) is int and 0 <= tokens[key] < 10**10}
        if counts:
            clean['worker_tokens'] = counts
    requested = safe_model_id(metadata.get('requested_model'))
    if requested is not None:
        clean['requested_model'] = requested
    effort = metadata.get('requested_reasoning_effort')
    if type(effort) is str and effort in CODEX_REASONING_EFFORTS:
        clean['requested_reasoning_effort'] = effort
    for name in ('model_source', 'reasoning_effort_source'):
        value = metadata.get(name)
        if type(value) is str and value in {'user_config', 'explicit', 'default_policy'}:
            clean[name] = value
    # What a launch sent: relative paths, sizes and digest prefixes only,
    # re-validated into the closed shape rather than copied.
    audit = closed_file_audit(metadata.get('file_audit'))
    if audit is not None:
        clean['file_audit'] = audit
    version = metadata.get('runtime_version')
    if type(version) is str and re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}(?:\.\d{1,4})?", version):
        clean['runtime_version'] = version
    fate = metadata.get('worker_termination')
    if type(fate) is dict:
        safe = {}
        for name in ('pid', 'job_members', 'escaped', 'close_cleanup_members'):
            if type(fate.get(name)) is int:
                safe[name] = fate[name]
        for name in ('confirmed_dead', 'tree_confirmed_dead', 'kill_attempted'):
            if type(fate.get(name)) is bool:
                safe[name] = fate[name]
        tree = fate.get('tree_kill')
        safe['tree_kill'] = tree if type(tree) is str and tree in {'ok', 'direct_child_only', 'uncontained', 'census_unknown',
                                           'job_not_empty', 'outside_unknown', 'escaped_job'} else 'census_unknown'
        clean['worker_termination'] = safe
    descendants = metadata.get('worker_descendants')
    if type(descendants) is dict:
        safe = {'enumerated': descendants.get('enumerated') is True,
                'truncated': descendants.get('truncated') is True, 'descendants': []}
        for name in ('observed_at', 'workers'):
            if _number(descendants.get(name)):
                safe[name] = descendants[name]
        method = descendants.get('method')
        safe['method'] = method if type(method) is str and method in {'process_table', 'procfs', 'toolhelp', 'toolhelp32', 'merged', 'windows_process_table', 'job_object', 'none'} else None
        for name in ('complete',):
            if type(descendants.get(name)) is bool:
                safe[name] = descendants[name]
        if descendants.get('incomplete_reason') in ('snapshot_failed', 'ancestry_broken', 'unknown'):
            safe['incomplete_reason'] = descendants['incomplete_reason']
        reason = descendants.get('reason')
        safe['reason'] = reason if reason is None or type(reason) is str and reason in {
            'worker_not_observed', 'process_table_unavailable', 'enumeration_unavailable',
            'unsupported_platform', 'snapshot_failed', 'unknown'} else 'unknown'
        images = {'node.exe', 'python.exe', 'pythonw.exe', 'cmd.exe', 'codex.exe', 'claude.exe', 'claude',
                  'powershell.exe', 'pwsh.exe', 'git.exe', 'node', 'python', 'python3', 'codex', 'sh', 'bash'}
        listed = descendants.get('descendants')
        for item in listed[:32] if type(listed) is list else ():
            image = os.path.basename(item.get('image')) if type(item) is dict and type(item.get('image')) is str else None
            if (type(item) is dict and type(item.get('pid')) is int and _number(item.get('created'))
                    and image in images):
                safe['descendants'].append({'pid': item['pid'], 'created': item['created'], 'image': image})
            else:
                safe.update(enumerated=False, reason='unknown')
                if type(item) is dict:
                    safe['descendants'].append({'pid': item.get('pid') if type(item.get('pid')) is int else None,
                                               'created': item.get('created') if _number(item.get('created')) else None,
                                               'image': None})
        if type(listed) is not list or len(listed) > 32:
            safe.update(enumerated=False, truncated=True, reason='unknown')
        clean['worker_descendants'] = safe
    error = (result.task_status_reason if type(result.task_status_reason) is str
             and result.task_status_reason in POLICY_REFUSAL_REASONS else 'worker_error_reported')
    clean_result = replace(result, provider='codex' if result.provider == 'codex' else 'unknown',
                           error=error if result.error else None, session_id=None,
                           task_status_reason=closed_reason(result.task_status_reason),
                           metadata=clean,
                           limitations=closed_limitations(result.limitations))
    object.__setattr__(clean_result, '_terminal_resolution', result.terminal_resolution)
    return clean_result
