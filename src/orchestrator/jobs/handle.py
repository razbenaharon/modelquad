# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Per-job launches and content-free handles; no MCP or recovery wiring."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from orchestrator import paths, procutil
from orchestrator.jobs.seam import JobRef
from orchestrator.service import history


# Provisional, to be measured later.
STARTUP_SECONDS = 15.0
RUNNER_LOG_MAX_BYTES = 1024 * 1024  # Provisional; keep one previous file.


def _open_runner_log():
    """Rotate on open after the cap is exceeded; stderr may overshoot it."""
    log = paths.logs_dir() / 'job-runner.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > RUNNER_LOG_MAX_BYTES:
        try:
            log.replace(log.with_name('job-runner.log.1'))
        except OSError:
            pass  # A live runner may hold it without delete sharing; append.
    return log.open('a', encoding='utf-8')


def _log_runner_event(job_id: str, kind: str) -> None:
    # Callers supply only fixed labels or exception class names, never bodies.
    line = f'{datetime.now(timezone.utc).isoformat()} {job_id} {kind}'
    try:
        with _open_runner_log() as stream:
            stream.write(line + '\n')
    except Exception:
        try:
            logging.getLogger(__name__).warning('%s', line)
        except Exception:
            pass  # Diagnostics must not prevent launch or reconciliation.


@dataclass(frozen=True)
class PreparedDelegation:
    """Complete deterministic call, private/transient; never a history payload.

    JSON fields are snapshots, not re-probed evidence. Unsupported JSON values
    fail preparation rather than silently dropping an execution input.
    """

    task_type: str
    prompt: str
    working_directory: str
    provider: str | None
    agent_runtime: str | None
    statuses: list[dict]
    session_id: str | None
    model: str | None
    reasoning_effort: str | None
    consumption: dict
    verification: dict | None
    required_capabilities: list[str] | None
    action_authority: dict | None
    metadata: dict | None
    timeout: float
    decision_id: str
    attempt_id: int
    db_path: str
    config_path: str
    invocation_mode: str | None
    user_approved: bool
    caller_host: str | None
    host: str | None
    mode: str
    paused: bool
    mode_source: str | None
    usage_directory: str
    host_snapshot: dict
    lineage: dict
    capsule: dict | None
    decision_mode: str
    reasoning_fell_back: bool
    plan_decision_id: int | None
    capability: str = 'delegate'
    decision_now: str | None = None
    catalog: dict | None = None
    policy_excluded: dict[str, str] | None = None

    def __post_init__(self):
        self.rebuild_decision_now()
        self.rebuild_catalog()
        if self.policy_excluded is not None and (
            type(self.policy_excluded) is not dict or
            any(type(key) is not str or type(value) is not str
                for key, value in self.policy_excluded.items())
        ):
            raise ValueError('invalid decision policy snapshot')
        if self.decision_now is not None:
            if type(self.policy_excluded) is not dict or type(self.statuses) is not list:
                raise ValueError('invalid_spec: captured decision inputs must be present together')
        elif self.catalog is not None or self.policy_excluded is not None:
            raise ValueError('invalid_spec: legacy decision inputs must be absent together')
        if self.capability != 'delegate':
            raise ValueError('delegate_spec_invalid')

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), allow_nan=False))

    @classmethod
    def from_dict(cls, value: dict) -> PreparedDelegation:
        return cls(**value)

    def rebuild_statuses(self):
        from orchestrator.models import CapacityState, Freshness, ProviderStatus, ResourcePool

        result = []
        for raw in self.statuses:
            data = dict(raw)
            data['freshness'] = Freshness(data['freshness'])
            if data['observed_at'] is not None:
                data['observed_at'] = datetime.fromisoformat(data['observed_at'])
            if data['capacity'] is not None:
                capacity = dict(data['capacity'])
                for key in ('reset_at', 'collected_at'):
                    if capacity[key] is not None:
                        capacity[key] = datetime.fromisoformat(capacity[key])
                data['capacity'] = CapacityState(**capacity)
            data['pools'] = tuple(ResourcePool.from_dict(pool) for pool in data['pools'])
            result.append(ProviderStatus(**data))
        return result

    def rebuild_decision_now(self):
        if self.decision_now is None:
            return None
        try:
            if type(self.decision_now) is not str:
                raise ValueError
            now = datetime.fromisoformat(self.decision_now)
            if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
                raise ValueError
            return now
        except (TypeError, ValueError) as error:
            raise ValueError('invalid decision_now: aware ISO UTC required') from error

    def rebuild_catalog(self):
        """Rebuild private routing input, preserving absent versus empty catalogs."""
        from orchestrator.catalog import ModelCatalog, ModelDescriptor

        if self.catalog is None:
            return None
        try:
            if (type(self.catalog) is not dict or set(self.catalog) != {'models'} or
                    type(self.catalog['models']) is not list):
                raise ValueError
            descriptors = []
            keys = set(ModelDescriptor('', '', '').to_dict())
            for raw in self.catalog['models']:
                if type(raw) is not dict or set(raw) != keys:
                    raise ValueError
                data = dict(raw)
                for key in ('provider', 'model_id', 'display_name', 'availability', 'source'):
                    if type(data[key]) is not str:
                        raise ValueError
                for key in ('agent_runtime', 'speed_class'):
                    if data[key] is not None and type(data[key]) is not str:
                        raise ValueError
                for key in ('capabilities', 'supported_reasoning_levels'):
                    if type(data[key]) is not list or any(type(item) is not str for item in data[key]):
                        raise ValueError
                    data[key] = tuple(data[key])
                for key in ('relative_strength', 'relative_cost'):
                    if data[key] is not None and (
                        type(data[key]) not in (int, float) or not math.isfinite(data[key])
                    ):
                        raise ValueError
                if data['freshness'] is not None:
                    if type(data['freshness']) is not str:
                        raise ValueError
                    data['freshness'] = datetime.fromisoformat(data['freshness'])
                descriptors.append(ModelDescriptor(**data))
            return ModelCatalog(tuple(descriptors))
        except (TypeError, ValueError, KeyError, OverflowError) as error:
            raise ValueError('invalid decision catalog snapshot') from error


@dataclass(frozen=True)
class JobSpec:
    """Transient launch data. timeout is explicit, never a product default."""

    argv: tuple[str, ...]
    cwd: str
    db_path: str
    attempt_id: int
    timeout: float
    env: dict[str, str] = field(default_factory=dict)
    sink: str = 'none'
    provider: str | None = None
    delegation: PreparedDelegation | None = None

    def __post_init__(self) -> None:
        if isinstance(self.argv, list):
            object.__setattr__(self, 'argv', tuple(self.argv))
        if isinstance(self.delegation, dict):
            object.__setattr__(self, 'delegation', PreparedDelegation.from_dict(self.delegation))
        if (not self.argv and self.delegation is None) or any(not isinstance(arg, str) or '\x00' in arg for arg in self.argv):
            raise ValueError('argv must contain valid strings')
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        if type(self.attempt_id) is not int or self.attempt_id <= 0:
            raise ValueError('an existing attempt is required')
        if any(not isinstance(k, str) or not isinstance(v, str) or '\x00' in k + v
               or '=' in k for k, v in self.env.items()):
            raise ValueError('invalid environment additions')


@dataclass(frozen=True)
class JobStart:
    ref: JobRef | None
    launched: bool
    limitation: str | None


def _process_in_any_job(process_handle=None) -> bool:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    answer = wintypes.BOOL()
    current = process_handle if process_handle is not None else kernel32.GetCurrentProcess()
    if not current or not kernel32.IsProcessInJob(current, None, ctypes.byref(answer)):
        raise OSError('caller containment unknown')
    return bool(answer.value)


def _caller_in_job() -> bool:
    return _process_in_any_job()


def _caller_job_limits() -> int:
    """Inspect only the innermost job; enclosing jobs are not enumerated."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t),
                    ('max_ws', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    info = Extended()
    if not kernel32.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        raise OSError('caller job limits unknown')
    return info.basic.flags


_breakaway_lock = threading.Lock()
_breakaway_result: bool | None = None
_breakaway_checked = False


def _background_child_kwargs() -> dict[str, object]:
    from orchestrator.background_cwd import background_child_kwargs

    return background_child_kwargs([_runner_executable()])


def _probe_breakaway() -> bool:
    """Prove escape from ALL jobs using a suspended, windowless trivial child."""
    limits = _caller_job_limits()
    command = [sys.executable, '-c', 'pass']
    kwargs = procutil.no_window_kwargs()
    kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | 0x8 | 0x4
    if not limits & 0x1000:
        kwargs['creationflags'] |= 0x01000000
    kwargs.update(_background_child_kwargs())
    kwargs['env'] = {**kwargs.get('env', os.environ), 'ORCHESTRATOR_WORKER': '1'}
    try:
        child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True, **kwargs)
    except OSError as error:
        if getattr(error, 'winerror', None) == 5:
            return False  # A containing job refused breakaway.
        raise
    try:
        return not _process_in_any_job(child._handle)
    finally:
        try:
            child.kill()
        except (subprocess.TimeoutExpired, OSError):
            pass  # Cleanup must not replace the measured containment result.
        try:
            child.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _verified_breakaway() -> bool:
    global _breakaway_result, _breakaway_checked
    with _breakaway_lock:
        if not _breakaway_checked:
            try:
                _breakaway_result = _probe_breakaway()
            finally:
                _breakaway_checked = True
        if _breakaway_result is None:
            raise OSError('breakaway verification unavailable')
        return _breakaway_result


def detached_launch_verdict(*, in_job=None, limits=None, breakaway_probe=None) -> tuple[bool, str]:
    """Fail closed; injectable probes make containment decisions deterministic.

    Innermost flags alone never prove escape from a nested outer job. No job
    ownership is inferred from a process list, shell membership or descendants.
    """
    try:
        if not (in_job or _caller_in_job)():
            return True, 'not_in_job'
        flags = (limits or _caller_job_limits)()
        if flags & 0x1800:
            if (breakaway_probe or _verified_breakaway)():
                return True, 'breakaway_verified'
            return False, 'job_breakaway_ineffective'
        if flags & 0x2000:
            return False, 'job_kill_on_close'
        return False, 'job_shared'
    except Exception:
        return False, 'containment_unknown'


def _launch_policy() -> tuple[int | None, bool, str | None]:
    """Return creation flag, whether escape is expected, and containment limit."""
    if not _caller_in_job():
        return 0, False, None
    limits = _caller_job_limits()
    if limits & 0x1000:  # JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        return 0, True, None
    if limits & 0x800:  # JOB_OBJECT_LIMIT_BREAKAWAY_OK
        return 0x01000000, True, None
    if limits & 0x2000:  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        return None, False, 'breakaway_not_permitted'
    return 0, False, 'contained_innermost_job_without_kill_on_close'


CONTAINED_RUNNER_LIMITATION = 'contained_innermost_job_without_kill_on_close'


def _service_runner_policy(*, in_job=None, limits=None, breakaway_probe=None) -> tuple[bool, str, int | None, bool]:
    """Measure this process; return ok, reason, creation flags, escape required.

    Unlike detached_launch_verdict, this deliberately accepts a measured
    non-killing innermost job. Outer-job limits are UNKNOWN and may terminate
    the runner, including when Windows ends the sign-in session. No Explorer
    or Task Scheduler ancestry is evidence of escape. Probe results are fresh
    at each admission fence; breakaway permission bits alone prove nothing.
    """
    try:
        membership = (in_job or _caller_in_job)()
        if type(membership) is not bool:
            raise ValueError('invalid membership')
        if not membership:
            return True, 'not_in_job', 0, True
        flags = (limits or _caller_job_limits)()
        if type(flags) is not int or not 0 <= flags <= 0xffffffff:
            raise ValueError('invalid limits')
        if flags & 0x1800:
            escaped = (breakaway_probe or _probe_breakaway)()
            if type(escaped) is not bool:
                raise ValueError('invalid probe')
            if escaped:
                return True, 'breakaway_verified', 0 if flags & 0x1000 else 0x01000000, True
        if flags & 0x2000:
            return False, 'job_kill_on_close', None, False
        # Ineffective permission must not set expect_outside or request an
        # explicit breakaway which Windows has already refused.
        return True, CONTAINED_RUNNER_LIMITATION, 0, False
    except Exception:
        return False, 'containment_unknown', None, False


def service_runner_verdict(*, in_job=None, limits=None, breakaway_probe=None) -> tuple[bool, str]:
    """Readiness for service-owned work, separate from strict service startup."""
    ok, reason, _, _ = _service_runner_policy(
        in_job=in_job, limits=limits, breakaway_probe=breakaway_probe)
    return ok, reason


def runner_containment_verdict(*, in_job=None, limits=None) -> tuple[bool, str]:
    """Authoritative current runner measurement, before any worker work.

    A breakaway probe proves a CHILD can escape; it cannot remove an already
    running runner from its own Job. Only its own all-job membership answer
    proves actual escape. A still-contained runner therefore always retains
    the limitation, and a killing/unknown runner refuses even when a parent
    or a probe child was outside. Outer-job limits remain unknown.
    """
    try:
        membership = (in_job or _caller_in_job)()
        if type(membership) is not bool:
            raise ValueError('invalid membership')
        if not membership:
            return True, 'not_in_job'
        flags = (limits or _caller_job_limits)()
        if type(flags) is not int or not 0 <= flags <= 0xffffffff:
            raise ValueError('invalid limits')
        if flags & 0x2000:
            return False, 'job_kill_on_close'
        return True, CONTAINED_RUNNER_LIMITATION
    except Exception:
        return False, 'containment_unknown'


def _runner_executable() -> str:
    # The owning interpreter supplies dependencies; _runner_command avoids a
    # Windows venv redirector's killing Job through the admitted base image.
    return sys.executable


def _runner_command(job_id: str, spec_path, db_path) -> list[str]:
    """The argv that starts the detached runner for one job.

    Source and installed-wheel runs use the runner module; a Windows venv
    uses a bound direct-base bootstrap so its redirector cannot put the
    actual runner in a new killing Job.
    """
    if os.name == 'nt' and sys.prefix != sys.base_prefix:
        from orchestrator.executable_resolution import UnsafeExecutable, admit_executable
        images = [admit_executable(image, allow_batch=False)
                  for image in (getattr(sys, '_base_executable', sys.executable), sys.executable)]
        for image in images:
            if image.path is None:
                raise UnsafeExecutable(image.refusal or 'not_found')
        # Restore the admitted owning venv before importing dependencies. Its
        # pyvenv home must match this base image (service.entry's existing rule).
        prefix = [images[0].path, '-I', '-S', str(Path(__file__).with_name('entry.py').resolve()),
                  '--installation-python', images[1].path]
    else:
        prefix = [_runner_executable(), '-m', 'orchestrator.jobs.runner']
    return [*prefix, '--job-id', job_id,
            '--spec', str(spec_path), '--db-path', str(db_path)]


def _protect_spec(path):
    """Protect an existing file by path; no longer used for spec creation.

    _write_spec installs and verifies protection through the creation handle.
    This legacy helper cannot close the creation-to-protection race.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(buffer), ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sddl = f"D:P(A;;FA;;;{sid.value})"
        finally:
            k32.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
    finally:
        k32.CloseHandle(token)
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        status = adv.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if status:
            raise ctypes.WinError(status)
    finally:
        k32.LocalFree(descriptor)


SPEC_MAX_AGE_SECONDS = 600.0  # abandoned specs are swept on the next start
SPEC_MAX_BYTES = 8 * 1024 * 1024  # Same ceiling as the service transport.


def serialized_spec(spec: JobSpec) -> bytes:
    """Check the bytes actually written, including JSON's ASCII expansion."""
    raw = json.dumps(asdict(spec), allow_nan=False).encode('utf-8')
    if len(raw) > SPEC_MAX_BYTES:
        raise ValueError('spec_limits')
    return raw


def _write_spec(spec: JobSpec, *, diagnostic_job_id: str | None = None) -> Path:
    from contextlib import suppress

    raw = serialized_spec(spec)
    directory = Path(spec.db_path).parent
    # This directory is lifecycle storage, not pending-output storage. The
    # exclusive file is private at creation; its handle is checked before writing.
    cutoff = time.time() - SPEC_MAX_AGE_SECONDS
    for abandoned in directory.glob('job-*.json'):
        try:
            if abandoned.is_file() and abandoned.stat().st_mtime < cutoff:
                abandoned.unlink()
        except OSError:
            if diagnostic_job_id is None:
                logging.getLogger(__name__).warning('stale job spec cleanup unavailable: %s', abandoned.name)
            else:
                logging.getLogger(__name__).warning('stale_spec_cleanup_unavailable %s', diagnostic_job_id)
    if os.name == 'nt':
        from orchestrator.adapters import file_security
        from orchestrator.adapters.file_security import create_exclusive

    creation_directory = directory.absolute()  # mkstemp returned absolute names.
    names = tempfile._get_candidate_names()
    for _ in range(100):
        path = creation_directory / f'job-{next(names)}.json'
        try:
            if os.name == 'nt':
                descriptor = create_exclusive(path)
            else:
                # Preserve mkstemp's exclusive creation and private POSIX mode.
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError('No unoccupied job spec filename is available.')
    try:
        if os.name == 'nt':
            native = file_security._handle(descriptor)
            if not file_security.persistent_acls(native):
                raise OSError('This volume cannot protect a private job spec.')
            expected = file_security._dacl_from_sddl(file_security._private_sddl())
            file_security.verify_dacl(native, expected)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
        return path
    except BaseException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Reserved:
    """Reservation plus private spec; the original probe is reused by start_job."""

    ref: JobRef
    spec_path: Path
    launch_policy: tuple[int | None, bool, str | None] = field(repr=False)


def reserve_and_write(spec: JobSpec, *, probe_caller: bool = True) -> Reserved | JobStart:
    """Reserve and write; service handoffs do not probe the Host's containment."""
    if os.name != 'nt':
        return JobStart(None, False, 'unsupported_platform')
    try:
        policy = _launch_policy() if probe_caller else (0, False, None)
    except OSError:
        return JobStart(None, False, 'caller_containment_unknown')
    try:
        row = history.reserve_job(spec.db_path, str(uuid.uuid4()), spec.attempt_id)
    except sqlite3.IntegrityError:
        return JobStart(None, False, 'attempt_already_reserved')
    except (ValueError, sqlite3.Error, OSError):
        return JobStart(None, False, 'open_attempt_unavailable')
    ref = JobRef(row['job_id'], row['attempt_id'], row['decision_id'])
    limitation = 'runner_launch_failed'
    try:
        if policy[0] is None:
            limitation = 'breakaway_not_permitted'
            raise OSError('caller job forbids breakaway')
        return Reserved(ref, _write_spec(spec), policy)
    except Exception:
        history.fail_job_attempt(spec.db_path, ref.job_id, detail='launch_failed')
        return JobStart(ref, False, limitation)


def start_job(spec: JobSpec) -> JobStart:
    """Keep in-process probe/reserve/write/spawn ordering and failure ownership."""
    reserved = reserve_and_write(spec)
    if isinstance(reserved, JobStart):
        return reserved
    return launch_reserved(reserved.ref, reserved.spec_path, spec, _policy=reserved.launch_policy)


def launch_reserved(ref: JobRef, spec_path: Path, spec: JobSpec, *, _policy=None, service_admission=False) -> JobStart:
    """Spawn a reserved job; replace starter identity before resuming the runner.

    In-process callers retain their original launch policy. A service caller
    probes its own containment under the separate service-runner policy.
    Runner stderr uses the rotating log, with DEVNULL on log-open failure.
    """
    proc = None
    limitation = 'runner_launch_failed'
    launch_limitation = None
    try:
        breakaway_flags, expect_outside, launch_limitation = (
            (0, False, None) if service_admission else
            _launch_policy() if _policy is None else _policy)
        if breakaway_flags is None:
            limitation = 'breakaway_not_permitted'
            raise OSError('caller job forbids breakaway')
        kwargs = procutil.no_window_kwargs()
        kwargs['creationflags'] = int(kwargs.get('creationflags', 0)) | 0x00000008 | 0x00000004
        kwargs['creationflags'] |= breakaway_flags
        # Suspend until the creator records the starter identity. On startup
        # the runner replaces it with its own pid/creation time and heartbeat.
        with history.job_spawn_fence(spec.db_path, ref) as fence:
            if service_admission:
                # Last seam: health/admission from before spec rewriting cannot
                # authorize a changed service containment or disabled preference.
                setting = fence.execute("SELECT value FROM settings WHERE key='job_runner_enabled'").fetchone()
                enabled = setting is not None and json.loads(setting[0]) is True
                if not enabled:
                    limitation = 'job_runner_disabled'
                    raise OSError('service admission changed')
                owned = fence.execute('SELECT runner_pid, runner_created, heartbeat_at, status FROM jobs WHERE job_id=?',
                                      (ref.job_id,)).fetchone()
                starter_created = procutil.process_creation_epoch(os.getpid())
                if (type(starter_created) not in (int, float) or not math.isfinite(starter_created)
                        or starter_created <= 0 or owned is None or owned['runner_pid'] != os.getpid() or
                        owned['runner_created'] != starter_created or
                        owned['heartbeat_at'] is not None or owned['status'] != 'starting'):
                    raise OSError('service ownership changed')
                attempt = fence.execute('SELECT decision_id, status, resolved_at FROM delegation_attempts WHERE id=?',
                                        (ref.attempt_id,)).fetchone()
                if (attempt is None or attempt['decision_id'] != ref.decision_id or
                        attempt['status'] != 'started' or attempt['resolved_at'] is not None):
                    raise OSError('service linkage changed')
                ok, reason, flags, expect_outside = _service_runner_policy()
                if not ok:
                    limitation = reason
                    raise OSError('service admission changed')
                admitted_limitation = None if reason in ('not_in_job', 'breakaway_verified') else reason
                launch_limitation = launch_limitation or admitted_limitation
            existing = fence.execute('SELECT launch_limitation FROM jobs WHERE job_id=?', (ref.job_id,)).fetchone()
            if existing is not None and existing[0] is not None:
                launch_limitation = (CONTAINED_RUNNER_LIMITATION if existing[0] == CONTAINED_RUNNER_LIMITATION
                                     else 'unknown')
            if launch_limitation == CONTAINED_RUNNER_LIMITATION:
                fence.execute('UPDATE jobs SET launch_limitation=COALESCE(launch_limitation, ?) WHERE job_id=?',
                              (launch_limitation, ref.job_id))
            if service_admission:
                # Recompute actual creation flags at the same fence, then
                # check the suspended child against ALL jobs before resume.
                kwargs['creationflags'] = int(procutil.no_window_kwargs().get('creationflags', 0)) | 0x00000008 | 0x00000004 | flags
            try:
                runner_log = _open_runner_log()
            except Exception:
                runner_log = None
                _log_runner_event(ref.job_id, 'runner_stderr_devnull')
            try:
                kwargs.update(_background_child_kwargs())
                kwargs['env'] = {**kwargs.get('env', os.environ), 'ORCHESTRATOR_WORKER': '1'}
                proc = subprocess.Popen(
                    _runner_command(ref.job_id, spec_path, spec.db_path),
                    stdout=subprocess.DEVNULL, stderr=runner_log if runner_log is not None else subprocess.DEVNULL,
                    close_fds=True, **kwargs,
                )
            finally:
                if runner_log is not None:
                    runner_log.close()
            child_in_job = _process_in_any_job(proc._handle) if expect_outside else None
            if expect_outside and child_in_job is not False:
                # A venv launcher's silent-breakaway job cannot escape an enclosing
                # job that forbids breakaway: refuse and close records below.
                limitation = 'breakaway_not_effective'
                raise OSError('runner remains contained')
            created = procutil.process_creation_epoch(proc.pid)
            if created is None:
                raise OSError('runner identity unavailable')
            fence.execute('UPDATE jobs SET runner_pid=?, runner_created=?, launch_limitation=COALESCE(launch_limitation, ?), updated_at=? WHERE job_id=?',
                          (proc.pid, created, launch_limitation, datetime.now(timezone.utc).isoformat(), ref.job_id))
        if not procutil._resume_suspended_process(proc.pid):
            raise OSError('runner resume failed')
        return JobStart(ref, True, launch_limitation)
    except Exception:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass  # cleanup failure must not leave the spec or records open
        if spec_path is not None:
            spec_path.unlink(missing_ok=True)
        # A spawn/identity failure rolls back the fence transaction. Preserve
        # the accepted observation atomically with pre-start failure closure.
        accepted = CONTAINED_RUNNER_LIMITATION if launch_limitation == CONTAINED_RUNNER_LIMITATION else None
        history.fail_job_attempt(spec.db_path, ref.job_id, detail='launch_failed', launch_limitation=accepted)
        return JobStart(ref, False, accepted or limitation)


def job_status(job_id: str, *, db_path: str | Path) -> dict | None:
    """Read exact liveness; expire only a runner that never reported startup.

    Before the first heartbeat, pid/creation identify the spawned starter.
    The runner atomically replaces them and writes that first heartbeat.
    Reported runner death still belongs to deferred crash recovery.
    """
    row = history.read_job(db_path, job_id)
    if row is None:
        return None
    if row['status'] == 'starting' and row['heartbeat_at'] is None:
        # Provisional: wall-clock UTC deadline, evaluated on status queries
        # (and runner registration). Job recovery will sweep stuck starting jobs.
        history.update_job_startup(db_path, job_id, startup_seconds=STARTUP_SECONDS)
        row = history.read_job(db_path, job_id)
    pid, created = row['runner_pid'], row['runner_created']
    state = procutil.pid_state(pid) if pid else 'unknown'
    observed = procutil.process_creation_epoch(pid) if state == 'alive' else None
    row['runner_liveness'] = (
        'dead' if state == 'dead' or (observed is not None and created is not None and observed != created)
        else 'alive' if observed is not None and created is not None else 'unknown'
    )
    return row


def stop_job(job_id: str, *, db_path: str | Path) -> str:
    """Request a stop, never confuse acceptance with observed whole-tree death."""
    try:
        row = history.read_job(db_path, job_id)
        if row is None:
            return 'not_found'
        if row['ended_at'] is not None:
            return 'not_running'
        return history.request_attempt_stop(db_path, row['attempt_id'])
    except (OSError, sqlite3.Error):
        return 'unavailable'
