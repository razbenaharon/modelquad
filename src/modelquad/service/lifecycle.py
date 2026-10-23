# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Windows lifecycle helpers for the local control-plane service.

The scheduler registration and the live process are intentionally separate:
registering auto-start does not launch a process until the next logon, while a
manual start works even when no scheduled task has been installed.
"""

from __future__ import annotations

import json
import math
import hashlib
import ntpath
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..procutil import no_window_kwargs, process_creation_epoch, terminate_pid, terminate_verified, windows_process_image_path
from ..adapters.atomicwrite import atomic_write
from ..executable_resolution import SECURITY_REFUSALS, UnsafeExecutable, system_directory, system_executable
from ..background_cwd import background_child_kwargs, validated_directory
from .socket_identity import DETAILS as LISTENER_DETAILS, ListenerError


WINDOWS_COMMAND_TIMEOUT_SECONDS = 10.0

SERVICE_START_GUARD_SECONDS = 25.0
SERVICE_READY_TIMEOUT_SECONDS = 15.0
SERVICE_READY_POLL_SECONDS = 0.1
SERVICE_CHILD_ADMISSION_SECONDS = 5.0
SERVICE_STOP_WAIT_SECONDS = 5.0
SERVICE_PID_PUBLISH_ATTEMPTS = 10
SERVICE_PID_PUBLISH_RETRY_SECONDS = 0.05

SERVICE_DETAILS = {
    **LISTENER_DETAILS,
    "service_ready_timeout": "The service did not authenticate before the readiness deadline.",
    "service_child_exited": "The started service process exited before it was ready.",
    "service_pid_record_unavailable": "The service process record could not be read.",
    "service_process_identity_unconfirmed": "The recorded service process identity is unconfirmed.",
    "service_record_changed": "A newer service record was left in place.",
    "service_unmanaged": "The authenticated listener has no managed service record.",
    "service_start_guard_timeout": "Service start guard timed out.",
    "service_start_guard_unavailable": "Service start guard is unavailable.",
    "service_cwd_unavailable": "The Windows service working directory could not be verified.",
    "service_bind_refused": "The managed service always binds 127.0.0.1; it takes no bind address.",
    "service_child_unmanaged": "The service child has no matching managed launch record.",
    "service_escape_unconfirmed": "The service could not be confirmed outside all Windows jobs. No direct startup was accepted.",
    "service_cleanup_confirmed": "The started process was stopped.",
    "service_cleanup_unconfirmed": "The started process could not be confirmed stopped.",
    "invalid_path": "The service launch options are invalid.",
    "path_unreadable": "The service launch or process record could not be written.",
    "unsupported_platform": "Service lifecycle requires Windows.",
    'background_startup_absent': 'Background startup is not registered',
    'background_startup_unknown': 'Background startup registration could not be verified. No service was started.',
    'background_startup_foreign': 'The background startup task belongs to another action or installation. It was left unchanged.',
    'background_startup_update_required': 'Background startup needs an explicit registration update.',
    'background_startup_changed': 'Background startup changed during verification. No direct service launch was attempted.',
    'background_startup_timeout': 'Background startup did not respond before the deadline. Its outcome is unknown.',
    'background_startup_failed': 'Windows refused the verified background startup task.',
}


def service_working_directory() -> str:
    """Use the OS system directory, never an inheritable environment root."""
    try:
        directory = system_directory()
        if directory is not None:
            return validated_directory(directory)
    except Exception:
        pass
    raise OSError("service_cwd_unavailable") from None


def _service_options(command: Sequence[str], state_dir: Path) -> dict[str, object]:
    """Read only the closed, nonsecret service selections from a launch marker."""
    def option(name, default=None):
        if name not in command:
            return default
        index = command.index(name)
        if index + 1 >= len(command) or command.count(name) != 1:
            raise ValueError
        return command[index + 1]

    port = int(option("--port", "8765"))
    poll = float(option("--poll-interval", "60"))
    if not 1 <= port <= 65535 or not math.isfinite(poll) or poll <= 0:
        raise ValueError
    if "--host" in command:
        # The service always binds 127.0.0.1; no launch names another address.
        raise OSError("service_bind_refused")
    result = {"port": port, "poll_interval": poll}
    root = Path(option("--state-root", str(state_dir))).expanduser()
    mode = option("--state-mode")
    if not root.is_absolute() or mode not in (None, "installed", "development", "test"):
        raise ValueError
    result.update(state_root=root.resolve(), state_mode=mode)
    for name, flag, default in (
        ("db_path", "--db", option("--history-db", str(state_dir / "modelquad.db"))),
        ("config_path", "--config", None),
        ("usage_directory", "--usage-directory", None),
    ):
        value = option(flag, default)
        if value is not None:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError
            value = path.resolve()
        result[name] = value
    return result


class _ServiceStartGuardError(Exception):
    """A launch must not proceed without owning the start guard."""


class _ServiceEscapeError(Exception):
    """A required uncontained launch was refused before creating a child."""


@contextmanager
def _service_start_guard(pid_path: Path, *, scope: str = 'start'):
    """Serialize status, spawn and pidfile publication across starters."""
    yielded = False
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            root = os.path.normcase(str(pid_path.parent.resolve()))
            lock_key = root if scope == 'start' else root + ':' + scope
            key = hashlib.sha256(lock_key.encode("utf-8")).hexdigest()
            handle = kernel32.CreateMutexW(None, False, f"Local\\ModelQuad-service-start-{key}")
            if not handle:
                raise _ServiceStartGuardError("Service start guard is unavailable.")
            acquired = False
            try:
                result = kernel32.WaitForSingleObject(handle, int(SERVICE_START_GUARD_SECONDS * 1000))
                if result == 0x102:
                    raise _ServiceStartGuardError("Service start guard timed out.")
                if result not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                    raise _ServiceStartGuardError("Service start guard is unavailable.")
                acquired = True
                yielded = True
                yield
            finally:
                if acquired:
                    kernel32.ReleaseMutex(handle)
                kernel32.CloseHandle(handle)
        else:
            import fcntl

            # Keep the inode: unlinking it would let a third caller lock a new
            # file while a waiter still owns the old one. OS locks release on exit.
            suffix = '.lock' if scope == 'start' else '.' + scope + '.lock'
            with pid_path.with_suffix(suffix).open("a+b") as stream:
                deadline = time.monotonic() + SERVICE_START_GUARD_SECONDS
                while True:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise _ServiceStartGuardError("Service start guard timed out.")
                        time.sleep(0.05)
                try:
                    yielded = True
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        if yielded:
            raise
        raise _ServiceStartGuardError("Service start guard is unavailable.") from exc


def _launch_background_child(popen, command, kwargs, *, require_escape=False):
    """Reuse the runner's job policy; UI direct starts require actual escape.

    Returns the child and a fixed containment fact. The policy and inspection
    functions are resolved at call time so synthetic tests can inject both.
    Strict callers refuse changed containment and never retry without breakaway.
    They must clean up a child whose returned containment is not proven absent.
    """
    from ..jobs.handle import _launch_policy, _process_in_any_job

    contained = None
    try:
        flag, escape_expected, limitation = _launch_policy()
        if flag is None:
            flag, contained = 0, "job_kill_on_close"
        elif limitation == "contained_innermost_job_without_kill_on_close":
            contained = "job_shared"
    except Exception:
        flag, escape_expected, contained = 0, False, "containment_unknown"
    if require_escape and contained is not None:
        raise _ServiceEscapeError
    launch_kwargs = dict(kwargs)
    if launch_kwargs.get("cwd") is None:
        # Fill only what the caller left out; a caller-supplied env stays.
        for key, value in background_child_kwargs(command).items():
            if launch_kwargs.get(key) is None:
                launch_kwargs[key] = value
    launch_kwargs["creationflags"] = int(launch_kwargs.get("creationflags", 0)) | flag
    try:
        child = popen(command, **launch_kwargs)
    except OSError as exc:
        if not flag & 0x01000000 or getattr(exc, "winerror", None) != 5:
            raise
        if require_escape:
            raise _ServiceEscapeError from None
        # An outer job can forbid the innermost job's advertised breakaway.
        launch_kwargs["creationflags"] &= ~0x01000000
        child = popen(command, **launch_kwargs)
        escape_expected, contained = False, "job_breakaway_ineffective"
    if escape_expected or require_escape:
        try:
            in_job = _process_in_any_job(child._handle)
            if in_job is True:
                contained = "job_breakaway_ineffective"
            elif in_job is not False:
                contained = "containment_unknown"
        except Exception:
            contained = "containment_unknown"
    return child, contained


def _unsupported_message() -> str:
    return "Service lifecycle management is not supported on this platform yet (Windows only)."


def _completed_text(completed: subprocess.CompletedProcess) -> str:
    return f"{getattr(completed, 'stdout', '') or ''}\n{getattr(completed, 'stderr', '') or ''}".lower()


def _task_or_process_not_found(completed: subprocess.CompletedProcess) -> bool:
    text = _completed_text(completed)
    return any(
        phrase in text
        for phrase in (
            "does not exist",
            "not found",
            "cannot find",
            "no running instance",
            "not running",
        )
    )


def _process_exists(pid: int) -> bool | None:
    """Tri-state, dependency-free PID liveness check; None is uncertainty.

    On Windows this must NOT use ``os.kill(pid, 0)``: any signal other than
    the CTRL events makes CPython call ``TerminateProcess`` -- the "probe"
    would kill the very service it is checking (observed 2026-08-23 when the
    uninstall path first called ``stop()`` against a live service). A
    query-only process handle is the real liveness check.
    """

    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        # A private, fully typed binding: the shared ``ctypes.windll.kernel32``
        # carries whatever argtypes/restype another module set last, and an
        # untyped OpenProcess returns a C int that can truncate a 64-bit handle.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                # An exited process stays openable while any handle to it is
                # held; the exit code distinguishes live from zombie.
                return code.value == STILL_ACTIVE
            return None
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A process that exists but cannot be signalled is still live.
        return True
    except OSError:
        return None
    return True


#: How far a live process's creation time may sit from the pidfile's
#: ``started_at`` and still be the recorded service. ``start``
#: stamps ``started_at`` just after ``Popen`` returns, so the real service is
#: created a moment *before* it; five seconds absorbs that gap on a loaded
#: machine. A process that inherited the PID after the service exited was
#: created later than the window, unless the service died within seconds of
#: its launch and Windows reused its PID at once for the same image.
SERVICE_IDENTITY_TOLERANCE_SECONDS = 5.0


def _normalized_image_path(path: str) -> str:
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def _same_image_path(actual: str, expected: str) -> bool:
    """Whether a process image path and a recorded ``argv[0]`` name one file.

    Compared as normalized, case-insensitive Windows paths; when the strings
    differ, a file-identity check still accepts two spellings of one file
    (an 8.3 short name, a substituted drive), because a false mismatch would
    discard the pidfile of a service that is still running.
    """

    if _normalized_image_path(actual) == _normalized_image_path(expected):
        return True
    try:
        return os.path.samefile(actual, expected)
    except (OSError, ValueError):
        return False


def _parse_started_at(value: object) -> float | None:
    """The pidfile's ``started_at`` as a UTC epoch, or None if unreadable."""

    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _format_epoch(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return str(epoch)


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of a per-user Task Scheduler operation."""

    success: bool
    supported: bool
    registered: bool | None
    message: str
    action: str
    dry_run: bool = False
    command: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "command": list(self.command) if self.command is not None else None,
            "dry_run": self.dry_run,
            "message": self.message,
            "registered": self.registered,
            "success": self.success,
            "supported": self.supported,
        }


@dataclass(frozen=True)
class ProcessStatus:
    """Observed control-plane process state."""

    running: bool
    pid: int | None
    started_at: str | None = None
    argv: tuple[str, ...] | None = None
    supported: bool = True
    message: str | None = None
    ready: bool | None = None
    reason: str | None = None
    cleanup_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv) if self.argv is not None else None,
            "message": self.message,
            "pid": self.pid,
            "running": self.running,
            "started_at": self.started_at,
            "supported": self.supported,
            "ready": self.ready,
            "reason": self.reason,
            "cleanup_reason": self.cleanup_reason,
        }


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of a manual control-plane process operation."""

    success: bool
    supported: bool
    running: bool
    pid: int | None
    message: str
    action: str
    dry_run: bool = False
    command: tuple[str, ...] | None = None
    contained: str | None = None
    ready: bool | None = None
    reason: str | None = None
    cleanup_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "command": list(self.command) if self.command is not None else None,
            "dry_run": self.dry_run,
            "contained": self.contained,
            "message": self.message,
            "pid": self.pid,
            "running": self.running,
            "success": self.success,
            "supported": self.supported,
            "ready": self.ready,
            "reason": self.reason,
            "cleanup_reason": self.cleanup_reason,
        }


class TaskSchedulerRegistrar:
    r"""Register a limited, per-user logon task without elevation.

    The trigger names the user, and that is the whole reason this class does
    not use ``schtasks /sc onlogon``. A bare ``/sc onlogon`` is an
    **any-user** logon trigger -- it fires for whoever signs in -- and Windows
    will not let a non-elevated caller create one. Measured on Windows 11 Pro
    10.0.26200, non-elevated, medium integrity, with the root task folder
    granting ``Authenticated Users`` ``FW``:

        schtasks /create /sc onlogon /rl limited /f /tr ...   Access is denied
        ... the same, plus /ru <computer>\<user>             Access is denied
        Register-ScheduledTask, InteractiveToken, Limited     Access is denied
        a logon trigger with no <UserId>                      0x80070005
        a logon trigger carrying <UserId>                     succeeds

    ``/ru`` does not help: it names the account the task *runs as*, not the
    account whose logon fires it, and it is the trigger Windows objects to. So
    registration goes through ``/xml`` with a definition whose ``LogonTrigger``
    carries this user's ``UserId``, which is the one shape the measurement found
    a normal user may create.

    Passing a definition rather than ``/tr`` also removes a hazard rather than
    adding one: ``Command`` and ``Arguments`` are separate XML elements, so the
    launch command is no longer flattened into a single string that Task
    Scheduler must re-parse later.
    """

    TASK_NAME = "ModelQuad_Service"

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self._run = run

    @staticmethod
    def _is_supported() -> bool:
        return sys.platform == "win32"

    @staticmethod
    def _format_task_command(command: Sequence[str]) -> str:
        """Quote argv for Task Scheduler's later Windows command-line parse.

        ``schtasks /tr`` accepts one command-line string, not an argv list. We
        quote every element and reject embedded double quotes rather than
        attempting a lossy escape. Trailing backslashes are doubled before the
        closing quote using the Windows command-line convention.
        """

        if not command:
            raise ValueError("The service launch command must not be empty.")
        quoted: list[str] = []
        for value in command:
            if not isinstance(value, str) or not value:
                raise ValueError("Each service launch command argument must be a non-empty string.")
            if "\x00" in value:
                raise ValueError("Service launch command arguments must not contain NUL characters.")
            if '"' in value:
                raise ValueError(
                    "Service launch command arguments must not contain double quotes; "
                    "Task Scheduler cannot safely store this command."
                )
            trailing_backslashes = len(value) - len(value.rstrip("\\"))
            escaped_suffix = "\\" * trailing_backslashes
            quoted.append(f'"{value}{escaped_suffix}"')
        return " ".join(quoted)

    @staticmethod
    def _current_user_id() -> str:
        """Bind the trigger/principal to the OS token, never environment text."""
        from .auth import current_user_sid, canonical_sid
        try:
            return canonical_sid(current_user_sid())
        except Exception:
            raise ValueError('The current Windows user identity is unavailable.') from None

    @staticmethod
    def _account_name_for_sid(user_id: str) -> str:
        """Resolve only the token's SID, never trust USERNAME/USERDOMAIN."""
        import ctypes
        from ctypes import wintypes
        from .auth import _win

        kernel32, advapi32 = _win()
        advapi32.LookupAccountSidW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
            wintypes.LPDWORD, wintypes.LPWSTR, wintypes.LPDWORD,
            wintypes.LPDWORD,
        ]
        advapi32.LookupAccountSidW.restype = wintypes.BOOL
        sid = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(user_id, ctypes.byref(sid)):
            raise ValueError('The current Windows user identity is unavailable.')
        try:
            name_size, domain_size, use = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
            advapi32.LookupAccountSidW(None, sid, None, ctypes.byref(name_size),
                                      None, ctypes.byref(domain_size), ctypes.byref(use))
            if ctypes.get_last_error() != 122 or not 0 < name_size.value <= 32768 or not 0 < domain_size.value <= 32768:
                raise ValueError('The current Windows account name is unavailable.')
            name = ctypes.create_unicode_buffer(name_size.value)
            domain = ctypes.create_unicode_buffer(domain_size.value)
            if not advapi32.LookupAccountSidW(None, sid, name, ctypes.byref(name_size),
                                             domain, ctypes.byref(domain_size), ctypes.byref(use)):
                raise ValueError('The current Windows account name is unavailable.')
            if use.value != 1 or not name.value or not domain.value:  # SidTypeUser
                raise ValueError('The current Windows account name is unavailable.')
            return domain.value + '\\' + name.value
        finally:
            kernel32.LocalFree(sid)

    @classmethod
    def _task_definition_xml(cls, command: Sequence[str], user_id: str, *, cwd: str | None = None) -> str:
        """The Task Scheduler definition this registrar creates.

        ``LogonTrigger/UserId`` is the load-bearing element -- see the class
        docstring. ``LeastPrivilege`` is what ``/rl limited`` meant, and
        ``InteractiveToken`` keeps the task in the user's own session rather
        than storing a password.
        """

        # Validate the whole command, including the image, before building
        # anything. XML escaping would make a quote in the image harmless here,
        # but a Windows path cannot contain one, and this method is not the
        # place to start accepting input the previous shape refused. The
        # arguments still have to survive Task Scheduler's own later parse, so
        # they keep the quoting; the image does not, because it is its own
        # element.
        cls._format_task_command(command)
        image = command[0]
        arguments = cls._format_task_command(command[1:]) if len(command) > 1 else ""
        principal = _xml_escape(user_id)
        lines = [
            '<?xml version="1.0" encoding="UTF-16"?>',
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
            "  <RegistrationInfo>",
            "    <Description>ModelQuad control plane, started at logon.</Description>",
            "  </RegistrationInfo>",
            "  <Triggers>",
            "    <LogonTrigger>",
            "      <Enabled>true</Enabled>",
            f"      <UserId>{principal}</UserId>",
            "    </LogonTrigger>",
            "  </Triggers>",
            "  <Principals>",
            '    <Principal id="Author">',
            f"      <UserId>{principal}</UserId>",
            "      <LogonType>InteractiveToken</LogonType>",
            "      <RunLevel>LeastPrivilege</RunLevel>",
            "    </Principal>",
            "  </Principals>",
            "  <Settings>",
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
            '    <RunOnlyIfIdle>false</RunOnlyIfIdle>',
            '    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>',
            '    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>',
            "    <StartWhenAvailable>false</StartWhenAvailable>",
            "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
            "    <Enabled>true</Enabled>",
            "  </Settings>",
            '  <Actions Context="Author">',
            "    <Exec>",
            f"      <Command>{_xml_escape(image)}</Command>",
        ]
        if arguments:
            lines.append(f"      <Arguments>{_xml_escape(arguments)}</Arguments>")
        if cwd is not None:
            lines.append(f"      <WorkingDirectory>{_xml_escape(validated_directory(cwd))}</WorkingDirectory>")
        lines += [
            "    </Exec>",
            "  </Actions>",
            "</Task>",
            "",
        ]
        return "\n".join(lines)

    def _unsupported_result(self, action: str) -> RegistrationResult:
        return RegistrationResult(
            success=False,
            supported=False,
            registered=None,
            message=_unsupported_message(),
            action=action,
        )

    def _scheduler(self, args):
        executable = system_executable('schtasks')
        if executable.path is None:
            raise UnsafeExecutable(executable.refusal or 'not_found')
        return self._run([executable.path, *args], capture_output=True, text=True,
                         shell=False, timeout=WINDOWS_COMMAND_TIMEOUT_SECONDS,
                         **background_child_kwargs([executable.path], cwd=service_working_directory()),
                         **no_window_kwargs())

    def definition(self) -> tuple[str, str | None]:
        """A name is never ownership. Only a positive not-found is absence."""
        if not self._is_supported():
            return 'background_startup_unknown', None
        try:
            result = self._scheduler(['/query', '/tn', self.TASK_NAME, '/xml'])
            if result.returncode != 0:
                # /query of this exact task with a positive OS not-found.
                # Unrecognized/localized errors, access denial and timeout stay unknown.
                body = _completed_text(result).strip()
                absent = body in {'error: the system cannot find the file specified.',
                                  'error: the specified task name "' + self.TASK_NAME.lower() + '" does not exist in the system.'}
                return ('background_startup_absent' if absent else 'background_startup_unknown'), None
            raw = result.stdout
            if type(raw) is not str or len(raw.encode('utf-8')) > 65536:
                return 'background_startup_unknown', None
            if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
                return 'background_startup_unknown', None
            return 'present', raw
        except subprocess.TimeoutExpired:
            return 'background_startup_timeout', None
        except Exception:
            return 'background_startup_unknown', None

    def _binding(self, raw: str):
        """Closed XML binding with only documented, equivalent defaults folded.

        Task Scheduler schema defaults: trigger/Settings Enabled=true,
        RunLevel=LeastPrivilege, MultipleInstancesPolicy=IgnoreNew,
        RunOnlyIfIdle/StartWhenAvailable/RestartOnIdle=false. Battery stops,
        StopOnIdleEnd and ExecutionTimeLimit have different defaults from our
        requested values and must remain present. The same binding is used for
        ownership and both dispatch rechecks.
        """
        if len(raw.encode('utf-8')) > 65536 or '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
            raise ValueError
        root = ET.fromstring(raw)
        ns = '{http://schemas.microsoft.com/windows/2004/02/mit/task}'
        if root.tag != ns + 'Task':
            raise ValueError
        allowed = {
            'Task': {'RegistrationInfo', 'Triggers', 'Principals', 'Settings', 'Actions'},
            'RegistrationInfo': {'Description', 'Author', 'URI', 'Date', 'Source', 'Version', 'Documentation'},
            'Triggers': {'LogonTrigger'}, 'LogonTrigger': {'Enabled', 'UserId', 'Delay'},
            'Principals': {'Principal'}, 'Principal': {'UserId', 'LogonType', 'RunLevel'},
            'Actions': {'Exec'}, 'Exec': {'Command', 'Arguments', 'WorkingDirectory'},
            'Settings': {'MultipleInstancesPolicy', 'DisallowStartIfOnBatteries',
                         'StopIfGoingOnBatteries', 'RunOnlyIfIdle', 'IdleSettings',
                         'RestartOnFailure', 'StartWhenAvailable', 'ExecutionTimeLimit',
                         'Enabled', 'AllowStartOnDemand', 'AllowHardTerminate',
                         'RunOnlyIfNetworkAvailable', 'WakeToRun', 'Hidden', 'Priority'},
            'IdleSettings': {'StopOnIdleEnd', 'RestartOnIdle', 'Duration', 'WaitTimeout'},
            'RestartOnFailure': {'Interval', 'Count'},
        }
        defaults = {
            'LogonTrigger': {'Enabled': 'true'},
            'Principal': {'RunLevel': 'LeastPrivilege'},
            'Settings': {'MultipleInstancesPolicy': 'IgnoreNew', 'RunOnlyIfIdle': 'false',
                         'StartWhenAvailable': 'false', 'Enabled': 'true'},
            'IdleSettings': {'RestartOnIdle': 'false'},
        }
        expanded = {
            'Settings': {'AllowStartOnDemand': 'true', 'AllowHardTerminate': 'true',
                         'RunOnlyIfNetworkAvailable': 'false', 'WakeToRun': 'false',
                         'Hidden': 'false', 'Priority': '7'},
            'IdleSettings': {'Duration': 'PT10M', 'WaitTimeout': 'PT1H'},
        }
        def walk(node):
            if not node.tag.startswith(ns):
                raise ValueError
            name = node.tag[len(ns):]
            keys = [child.tag for child in node]
            if len(keys) != len(set(keys)):
                raise ValueError
            if any(key not in {ns + value for value in allowed.get(name, ())} for key in keys):
                raise ValueError
            attributes = {'Task': {'version'}, 'Principal': {'id'}, 'Actions': {'Context'}}
            if set(node.attrib) - attributes.get(name, set()):
                raise ValueError
            if any((child.tail or '').strip() for child in node):
                raise ValueError
            value = (node.text or '').strip()
            if name == 'UserId' and node is root.find('./' + ns + 'Triggers/' + ns + 'LogonTrigger/' + ns + 'UserId'):
                user_id = self._current_user_id()
                if value != user_id and not value.upper().startswith('S-1-'):
                    if value.casefold() == self._account_name_for_sid(user_id).casefold():
                        value = user_id
            children = [walk(child) for child in node]
            for key, default in defaults.get(name, {}).items():
                if ns + key not in keys:
                    children.append((ns + key, (), default, ()))
            children = [child for child in children if not (
                child[0][len(ns):] in expanded.get(name, {}) and not child[1] and not child[3]
                and child[2] == expanded[name][child[0][len(ns):]])]
            return (node.tag, tuple(sorted(node.attrib.items())), value, tuple(sorted(children)))
        tree = walk(root)
        def part(name):
            return next((value for value in tree[3] if value[0] == ns + name), None)
        if {child.tag for child in root} - {ns + key for key in ('RegistrationInfo','Triggers','Principals','Settings','Actions')}:
            raise ValueError
        return part('Triggers'), part('Principals'), part('Actions'), part('Settings')

    def verify_definition(self, raw: str, command: Sequence[str], *, allow_update=False) -> str | None:
        try:
            expected_xml = self._task_definition_xml(command, self._current_user_id(), cwd=service_working_directory())
            actual, expected = self._binding(raw), self._binding(expected_xml)
            if actual[:3] != expected[:3]:
                # Earlier owned controllers lack only the supervision flag.
                legacy = [arg for arg in command if arg != '--supervise']
                legacy_binding = self._binding(self._task_definition_xml(legacy, self._current_user_id(), cwd=service_working_directory()))
                if actual[:3] != legacy_binding[:3]:
                    return 'background_startup_foreign'
                if not allow_update:
                    return 'background_startup_update_required'
            if allow_update:
                return None
            if actual[3] != expected[3]:
                return 'background_startup_update_required'
            return None
        except Exception:
            return 'background_startup_unknown'

    def is_registered(self) -> RegistrationResult:
        """Read actual registration; a failed query never means absent."""
        if not self._is_supported():
            return self._unsupported_result('status')
        command = ('schtasks', '/query', '/tn', self.TASK_NAME, '/xml')
        state, raw = self.definition()
        if state == 'present':
            try:
                from ..cli import _service_autostart_command, SERVICE_DEFAULT_PORT
                reason = self.verify_definition(raw, _service_autostart_command(SERVICE_DEFAULT_PORT))
            except Exception:
                reason = 'background_startup_unknown'
            return RegistrationResult(reason is None, self._is_supported(),
                                      None if reason == 'background_startup_unknown' else True,
                                      SERVICE_DETAILS[reason] if reason else 'Background startup is verified.',
                                      'status', command=command)
        return RegistrationResult(state == 'background_startup_absent', self._is_supported(),
                                  False if state == 'background_startup_absent' else None,
                                  SERVICE_DETAILS[state], 'status', command=command)

    def register(self, *, command: list[str], dry_run: bool = False, cwd: str | None = None,
                 existing_only: bool = False) -> RegistrationResult:
        """Create or replace this project's limited, per-user logon task.

        ``command`` must name a **windowless** executable. Task Scheduler is not
        a console-owning parent, so Windows gives a console-subsystem action its
        own console -- with a window, on the user's desktop, for as long as the
        service runs. Measured on 2026-09-15: 9.937 s of a
        9.953 s run, ended only by the observation. ``schtasks`` has no flag
        that changes this and neither does ``<Hidden>true</Hidden>``, which
        hides the task from the Task Scheduler snap-in and nothing else.
        ``cli._service_autostart_command`` is what resolves such an executable;
        this method cannot check the PE subsystem of a path it is merely handed.
        ``existing_only`` lets upgrades recheck ownership without creating a
        task that disappeared since their initial inspection.
        """

        if not self._is_supported():
            return self._unsupported_result("register")
        # Describe the requested operation without the admitted system image
        # or a temporary XML path, including failures of the ownership query.
        command_preview = ('schtasks', '/create', '/tn', self.TASK_NAME,
                           '/xml', '<generated task definition>', '/f')
        try:
            definition = self._task_definition_xml(command, self._current_user_id(),
                                                   cwd=cwd if cwd is not None else service_working_directory())
        except OSError:
            return RegistrationResult(False, True, None, SERVICE_DETAILS["service_cwd_unavailable"],
                                      "register", dry_run=dry_run,
                                      command=("schtasks", "/create", "/tn", self.TASK_NAME,
                                               "/xml", "<generated task definition>", "/f"))
        except (ValueError, IndexError) as exc:
            message = str(exc) or "The service launch command must not be empty."
            return RegistrationResult(
                success=False,
                supported=True,
                registered=None,
                message=message,
                action="register",
                dry_run=dry_run,
            )

        if dry_run:
            # The definition path is reported as a placeholder rather than as a
            # temporary file name, because a dry run writes no file and naming
            # one that does not exist would be a false statement about the
            # system -- a defect class seen elsewhere.
            return RegistrationResult(
                success=True,
                supported=True,
                registered=None,
                message=(
                    "Would register the limited per-user auto-start task from a "
                    "generated definition whose logon trigger names this user."
                ),
                action="would_register",
                dry_run=True,
                command=(
                    "schtasks", "/create", "/tn", self.TASK_NAME,
                    "/xml", "<generated task definition>", "/f",
                ),
            )

        state, existing = self.definition()
        reason = self.verify_definition(existing, command, allow_update=True) if state == 'present' else (
            None if state == 'background_startup_absent' and not existing_only else state)
        if reason:
            return RegistrationResult(False, True, True if existing else None,
                                      SERVICE_DETAILS[reason], 'register', command=command_preview)

        # schtasks reads the definition from a file, and reads it as UTF-16.
        # The file carries no secret -- it is the same command the task will
        # show in the Task Scheduler snap-in -- but it is removed either way.
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-16", suffix=".xml", prefix="modelquad-task-", delete=False
        )
        try:
            with handle as stream:
                stream.write(definition)
            definition_path = handle.name
            # Ownership was checked above; the task name alone is not proof.
            argv = (
                "schtasks",
                "/create",
                "/tn",
                self.TASK_NAME,
                "/xml",
                definition_path,
                "/f",
            )
            created = self._create_from_definition(argv)
            if not created.success:
                return created
            state, actual = self.definition()
            reason = self.verify_definition(actual, command) if state == 'present' else state
            if reason:
                return RegistrationResult(False, True, None, SERVICE_DETAILS[reason],
                                          'register', command=command_preview)
            return created
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    def _create_from_definition(self, argv: tuple[str, ...]) -> RegistrationResult:
        """Run one ``schtasks /create /xml`` and classify what came back."""

        try:
            executable = system_executable("schtasks")
            if executable.path is None:
                raise UnsafeExecutable(executable.refusal or "not_found")
            completed = self._scheduler(argv[1:])
        except subprocess.TimeoutExpired:
            return RegistrationResult(
                success=False,
                supported=True,
                registered=None,
                message="Windows Task Scheduler did not respond in time.",
                action="register",
                command=argv,
            )
        except (UnsafeExecutable, FileNotFoundError, OSError) as exc:
            return RegistrationResult(
                success=False,
                supported=True,
                registered=None,
                message=("Could not register the auto-start task: "
                         + (exc.reason if isinstance(exc, UnsafeExecutable) else type(exc).__name__)),
                action="register",
                command=argv,
            )
        if completed.returncode == 0:
            return RegistrationResult(
                success=True,
                supported=True,
                registered=True,
                message="Registered the limited per-user auto-start task.",
                action="register",
                command=argv,
            )
        return RegistrationResult(
            success=False,
            supported=True,
            registered=False,
            message="Task Scheduler rejected the registration.",
            action="register",
            command=argv,
        )

    def unregister(self, *, command: Sequence[str], dry_run: bool = False) -> RegistrationResult:
        """Delete this project's task, treating an already-absent task as success.

        ``command`` is the action install would register. Before deleting, the
        existing task must pass the same ownership check install applies: a
        task of the same name whose action belongs to another installation, or
        that cannot be read, is left in place.
        """

        if not self._is_supported():
            return self._unsupported_result("unregister")
        argv = ("schtasks", "/delete", "/tn", self.TASK_NAME, "/f")
        if dry_run:
            return RegistrationResult(
                success=True,
                supported=True,
                registered=None,
                message="Would unregister the auto-start task.",
                action="would_unregister",
                dry_run=True,
                command=argv,
            )
        state, existing = self.definition()
        if state == 'background_startup_absent':
            return RegistrationResult(True, True, False, "Auto-start task is not registered.",
                                      "unregister", command=argv)
        reason = self.verify_definition(existing, command, allow_update=True) if state == 'present' else state
        if reason:
            return RegistrationResult(False, True, True if existing else None,
                                      SERVICE_DETAILS[reason], 'unregister', command=argv)
        try:
            executable = system_executable("schtasks")
            if executable.path is None:
                raise UnsafeExecutable(executable.refusal or "not_found")
            completed = self._run(
                [executable.path, *argv[1:]], capture_output=True, text=True, shell=False,
                timeout=WINDOWS_COMMAND_TIMEOUT_SECONDS, **no_window_kwargs()
            )
        except subprocess.TimeoutExpired:
            return RegistrationResult(
                success=False,
                supported=True,
                registered=None,
                message="Windows Task Scheduler did not respond in time.",
                action="unregister",
                command=argv,
            )
        except (UnsafeExecutable, FileNotFoundError, OSError) as exc:
            return RegistrationResult(
                success=False,
                supported=True,
                registered=None,
                message=("Could not unregister the auto-start task: "
                         + (exc.reason if isinstance(exc, UnsafeExecutable) else type(exc).__name__)),
                action="unregister",
                command=argv,
            )
        if completed.returncode == 0 or _task_or_process_not_found(completed):
            return RegistrationResult(
                success=True,
                supported=True,
                registered=False,
                message="Auto-start task is not registered.",
                action="unregister",
                command=argv,
            )
        return RegistrationResult(
            success=False,
            supported=True,
            registered=None,
            message=(getattr(completed, "stderr", "") or "Task Scheduler rejected the removal.").strip(),
            action="unregister",
            command=argv,
        )


class ServiceProcessManager:
    """Start and stop a detached, locally bound control-plane process."""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        pid_exists: Callable[[int], bool | None] = _process_exists,
        process_image_path: Callable[[int], str | None] = windows_process_image_path,
        process_created_at: Callable[[int], float | None] = process_creation_epoch,
        terminate_pid: Callable[[int], bool] = terminate_pid,
        terminate_verified: Callable[..., str] = terminate_verified,
        authenticate: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        working_directory: Callable[[], str] = service_working_directory,
    ) -> None:
        """``terminate_pid`` remains accepted for compatibility, unused by stop."""
        # Default to the installation-mode state root (dev checkout: ~/.modelquad;
        # installed on Windows: %LOCALAPPDATA%\ModelQuad) so the
        # service pidfile lives alongside the history DB.
        from .. import paths

        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else paths.state_root()
        self.pid_path = self.state_dir / "service.pid"
        self.launch_barrier_path = self.state_dir / "service.launch-unconfirmed"
        self._popen = popen
        self._run = run
        self._pid_exists = pid_exists
        self._process_image_path = process_image_path
        self._process_created_at = process_created_at
        self._terminate_pid = terminate_pid
        self._terminate_verified = terminate_verified
        self._authenticate = authenticate
        self._clock, self._sleep = clock, sleep
        self._working_directory = working_directory
        self._started_child = None
        self._started_record = None

    @staticmethod
    def _is_supported() -> bool:
        return sys.platform == "win32"

    def _unsupported_status(self) -> ProcessStatus:
        return ProcessStatus(
            running=False,
            pid=None,
            supported=False,
            message=_unsupported_message(),
            reason="unsupported_platform",
        )

    def _unsupported_result(self, action: str) -> ProcessResult:
        return ProcessResult(
            success=False,
            supported=False,
            running=False,
            pid=None,
            message=_unsupported_message(),
            action=action,
            reason="unsupported_platform",
        )

    def _read_pid_record(self) -> tuple[dict[str, object] | None, str | None]:
        try:
            payload = json.loads(self.pid_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, "Unable to read the service pidfile: service_pid_record_unavailable"
        if not isinstance(payload, dict):
            return None, "The service pidfile has an invalid format."
        pid = payload.get("pid")
        argv = payload.get("argv")
        started_at = payload.get("started_at")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) and value for value in argv)
            or not isinstance(started_at, str)
            or not started_at
        ):
            return None, "The service pidfile is missing its identifying launch marker."
        return payload, None

    @staticmethod
    def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
        if not command or not all(isinstance(value, str) and value and "\x00" not in value for value in command):
            raise ValueError("The service launch command must contain non-empty string arguments without NUL characters.")
        return tuple(command)

    def _prove_service_identity(self, pid: int, record: dict[str, object]) -> tuple[bool | None, str]:
        """Whether the live ``pid`` is the process the pidfile ``record`` names.

        ``True`` means both halves of the identity were read and match: the
        process's full image path is the recorded ``argv[0]``, and it was
        created within ``SERVICE_IDENTITY_TOLERANCE_SECONDS`` of the recorded
        ``started_at``. ``False`` means one half was read and does not match,
        which proves the PID now belongs to another process -- Windows reuses
        PIDs, and every ModelQuad process shares the ``modelquad.exe``
        image name, so a Host's MCP server can inherit a dead service's PID.
        ``None`` means identity could not be proven either way.
        The reason is a short phrase for the caller's message.
        """

        expected_image = str(record["argv"][0])
        try:
            image = self._process_image_path(pid)
        except Exception:
            image = None
        if image is not None and not _same_image_path(image, expected_image):
            return False, "its image is different from the recorded service image"
        started_at = _parse_started_at(record["started_at"])
        try:
            created = self._process_created_at(pid)
            if created is not None and (type(created) not in (int, float) or not math.isfinite(created)):
                created = None
        except Exception:
            created = None
        if started_at is not None and created is not None:
            if abs(created - started_at) > SERVICE_IDENTITY_TOLERANCE_SECONDS:
                return False, (
                    f"it was created at {_format_epoch(created)}, not at the recorded start "
                    f"{_format_epoch(started_at)}"
                )
        unreadable = [
            label
            for label, value in (
                ("its image path", image),
                ("its creation time", created),
                ("the pidfile start time", started_at),
            )
            if value is None
        ]
        if unreadable:
            return None, f"could not read {' or '.join(unreadable)}"
        return True, ""

    def _inspect_process(self) -> ProcessStatus:
        """Return the best-effort live process status without modifying state."""

        if not self._is_supported():
            return self._unsupported_status()
        if self._launch_barrier_present():
            return ProcessStatus(running=True, pid=None, reason="service_cleanup_unconfirmed",
                                 message=SERVICE_DETAILS["service_cleanup_unconfirmed"])
        record, error = self._read_pid_record()
        if record is None:
            return ProcessStatus(
                running=error is not None,
                pid=None,
                message=error or "Service is not running.",
                reason="service_pid_record_unavailable" if error else None,
            )
        pid = int(record["pid"])
        argv = tuple(record["argv"])
        started_at = str(record["started_at"])
        try:
            present = self._pid_exists(pid)
        except Exception:
            present = None
        if present is False:
            return ProcessStatus(
                running=False,
                pid=None,
                message="Service is not running (a stale pidfile was found).",
            )
        identity, reason = self._prove_service_identity(pid, record)
        if type(present) is not bool:
            identity, reason = None, "could not inspect the recorded process"
        if identity is False:
            return ProcessStatus(
                running=False,
                pid=None,
                message=(
                    f"Service is not running (its stale pidfile names PID {pid}, which now "
                    f"belongs to another process: {reason})."
                ),
            )
        # An unproven identity still reads as running: reporting it stopped
        # would let ``start`` launch a second service over a live first one.
        return ProcessStatus(
            running=True,
            pid=pid,
            started_at=started_at,
            argv=argv,
            message=(
                "Service is running."
                if identity
                else f"Service appears to be running, but its process identity is unconfirmed ({reason})."
            ),
            reason=None if identity else "service_process_identity_unconfirmed",
        )

    def _authenticate_once(self, options: dict, *, deadline: float, record: dict | None = None) -> str | None:
        """The signed proof already contains health. Never send another request."""
        from .listener import authenticate
        call = self._authenticate or authenticate
        expected = {} if record is None else {
            "expected_pid": record["pid"],
            "expected_created": _parse_started_at(record["started_at"]),
            "expected_image": record["argv"][0],
        }
        connection = None
        try:
            if record is not None:
                # Older records were stamped just after Popen, with W02a's
                # tolerance. Bind the socket to the actual creation time,
                # already required to match that record, so migration can
                # still reach the precise legacy authentication refusal.
                created = self._process_created_at(int(record["pid"]))
                stamped = expected["expected_created"]
                if (type(created) not in (int, float) or not math.isfinite(created)
                        or stamped is None or abs(created - stamped) > SERVICE_IDENTITY_TOLERANCE_SECONDS):
                    return "service_process_identity_unconfirmed"
                if "--managed-child" not in record["argv"]:
                    expected["expected_created"] = created
            connection = call(options["port"], db_path=options["db_path"], deadline=deadline,
                              require_runner=False, **expected)
            return None
        except ListenerError as exc:
            return exc.code
        except Exception:
            return "service_proof_invalid"
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def status(self, *, port: int | None = None, deadline: float | None = None) -> ProcessStatus:
        """Bounded authenticated observation; never mutate or launch anything."""
        current = self._inspect_process()
        if not current.supported:
            return current
        if current.reason:
            return replace(current, ready=False)
        record, error = self._read_pid_record()
        if error:
            return replace(current, running=True, ready=False,
                           reason="service_pid_record_unavailable",
                           message=SERVICE_DETAILS["service_pid_record_unavailable"])
        if not current.running and record is not None:
            return replace(current, ready=False)
        if current.running and (record is None or current.pid != record["pid"]
                                or current.started_at != record["started_at"]
                                or current.argv != tuple(record["argv"])):
            return replace(current, ready=False, reason="service_record_changed",
                           message=SERVICE_DETAILS["service_record_changed"])
        try:
            options = _service_options(record["argv"] if record else (), self.state_dir)
            if port is not None:
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError
                options["port"] = port
        except Exception:
            return replace(current, ready=False, reason="invalid_path", message=SERVICE_DETAILS["invalid_path"])
        probe_deadline = self._clock() + 3.0
        if deadline is not None:
            probe_deadline = min(probe_deadline, deadline)
        code = self._authenticate_once(options, deadline=probe_deadline,
                                       record=record if current.running else None)
        if code is None and record is None:
            code = "service_unmanaged"
        return replace(current, ready=code is None, reason=code,
                       running=current.running or code == "service_unmanaged",
                       message="Service is running." if code is None else SERVICE_DETAILS[code])

    def _start_result(self, code: str | None, *, running: bool = False, pid: int | None = None,
                      command: tuple | None = None, action: str = "start", ready: bool | None = False,
                      cleanup_reason: str | None = None, contained: str | None = None,
                      message: str | None = None, dry_run: bool = False) -> ProcessResult:
        if code not in SERVICE_DETAILS and code not in SECURITY_REFUSALS and code is not None:
            code = "invalid_path"
        try:
            from ..windowless_diagnostics import emit
            if code:
                emit('service_start_failed')
            elif contained:
                emit('service_start_contained', contained if contained in {'job_shared', 'containment_unknown'} else 'unknown')
        except Exception:
            pass  # diagnostics cannot change the primary readiness outcome
        return ProcessResult(
            success=code is None, supported=True, running=running, pid=pid, message=message or SERVICE_DETAILS.get(code, "The service launch was refused."),
            action=action, command=command, ready=None if dry_run else ready, reason=code,
            cleanup_reason=cleanup_reason, contained=contained, dry_run=dry_run,
        )

    def start(self, *, command: list[str], dry_run: bool = False,
              require_escape: bool = False) -> ProcessResult:
        """Own inspection, launch, publication, readiness and cleanup under one guard."""
        if not self._is_supported():
            return replace(self._unsupported_result("start"), reason="unsupported_platform")
        try:
            launch_command = self._validate_command(command)
            options = _service_options(launch_command, self.state_dir)
        except OSError:
            return self._start_result("service_bind_refused", dry_run=dry_run)
        except Exception:
            return self._start_result("invalid_path", dry_run=dry_run)
        if dry_run:
            current = self._inspect_process()
            return self._start_result(None, running=current.running, pid=current.pid,
                                      command=launch_command, action="would_start", ready=None,
                                      dry_run=True, message="Would check or start the managed control-plane service.")
        try:
            with _service_start_guard(self.pid_path):
                if self._launch_barrier_present():
                    return self._start_result("service_cleanup_unconfirmed", running=True,
                                              command=launch_command)
                record, error = self._read_pid_record()
                if error:
                    return self._start_result("service_pid_record_unavailable", running=True,
                                              command=launch_command)
                if record is not None:
                    pid = int(record["pid"])
                    identity = False
                    try:
                        present = self._pid_exists(pid)
                    except Exception:
                        present = None
                    if type(present) is not bool:
                        identity = None
                    elif present:
                        try:
                            identity, _reason = self._prove_service_identity(pid, record)
                        except Exception:
                            identity = None
                    if identity is None:
                        return self._start_result("service_process_identity_unconfirmed", running=True,
                                                  pid=pid, command=launch_command)
                    if identity is True:
                        # Authenticate the requested port/store against this exact recorded process.
                        code = self._authenticate_once(options, deadline=self._clock() + 3.0, record=record)
                        return self._start_result(code, running=True, pid=pid, command=launch_command,
                                                  action="already_running" if code is None else "start",
                                                  ready=code is None,
                                                  message=f"Service is already running (PID {pid})." if code is None else None)
                    if not self._unlink_if_same(pid, record["started_at"], expected_record=record):
                        return self._start_result("service_record_changed", running=True, command=launch_command)
                code = self._authenticate_once(options, deadline=self._clock() + 3.0)
                if code != "service_absent":
                    return self._start_result(code or "service_unmanaged", running=True, command=launch_command)
                child = None
                reserved = False
                try:
                    cwd = self._working_directory()
                    popen_kwargs = background_child_kwargs(launch_command, cwd=cwd)
                except Exception:
                    return self._start_result("service_cwd_unavailable", command=launch_command)
                try:
                    popen_kwargs.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, close_fds=True)
                    if sys.platform == "win32":
                        popen_kwargs["creationflags"] = (
                            getattr(subprocess, "DETACHED_PROCESS", 0x8)
                            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                    self._reserve_launch_barrier()
                    reserved = True
                    launch_requirements = {"require_escape": True} if require_escape else {}
                    child, contained = _launch_background_child(
                        self._popen, list(launch_command), popen_kwargs, **launch_requirements)
                except _ServiceEscapeError:
                    if reserved:
                        self._release_launch_barrier()
                    return self._start_result("service_escape_unconfirmed", command=launch_command)
                except UnsafeExecutable as exc:
                    if reserved:
                        self._release_launch_barrier()
                    return self._start_result(exc.reason, command=launch_command)
                except Exception:
                    if child is not None:
                        return self._failed_child(child, getattr(child, "pid", None), None,
                                                  "path_unreadable", launch_command, contained)
                    if reserved:
                        self._release_launch_barrier()
                    return self._start_result("path_unreadable", command=launch_command)
                pid = getattr(child, "pid", None)
                if type(pid) is not int or pid <= 0:
                    return self._failed_child(child, None, None, "invalid_path", launch_command, contained)
                if require_escape and contained is not None:
                    return self._failed_child(child, pid, None, "service_escape_unconfirmed",
                                              launch_command, contained)
                try:
                    created = self._process_created_at(pid)
                    if type(created) not in (float, int) or not math.isfinite(created):
                        raise ValueError
                    started_at = datetime.fromtimestamp(created, timezone.utc).isoformat()
                except Exception:
                    return self._failed_child(child, pid, None, "service_process_identity_unconfirmed",
                                              launch_command, contained)
                published = {"argv": list(launch_command), "pid": pid, "started_at": started_at}
                try:
                    content = json.dumps(published, indent=2, sort_keys=True)
                    for attempt in range(SERVICE_PID_PUBLISH_ATTEMPTS):
                        try:
                            atomic_write(self.pid_path, content, private=False)
                            break
                        except PermissionError:
                            if attempt == SERVICE_PID_PUBLISH_ATTEMPTS - 1:
                                raise
                            self._sleep(SERVICE_PID_PUBLISH_RETRY_SECONDS)
                except Exception:
                    return self._failed_child(child, pid, published, "path_unreadable", launch_command, contained)
                deadline = self._clock() + SERVICE_READY_TIMEOUT_SECONDS
                while True:
                    live = self._child_live(child)
                    if live is not True:
                        code = "service_child_exited" if live is False else "service_process_identity_unconfirmed"
                        break
                    now = self._clock()
                    if now >= deadline:
                        code = "service_ready_timeout"
                        break
                    code = self._authenticate_once(options, deadline=min(deadline, now + 1.0), record=published)
                    live = self._child_live(child)
                    if live is not True:
                        code = "service_child_exited" if live is False else "service_process_identity_unconfirmed"
                        break
                    if self._clock() >= deadline:
                        code = "service_ready_timeout"
                        break
                    if code is None:
                        current, error = self._read_pid_record()
                        if error or current != published:
                            code = "service_record_changed"
                            break
                        if not self._release_launch_barrier():
                            code = "path_unreadable"
                            break
                        self._started_child, self._started_record = child, published
                        return self._start_result(None, running=True, pid=pid, command=launch_command,
                                                  ready=True, contained=contained,
                                                  message=f"Started the detached control-plane service (PID {pid}).")
                    if code not in {"service_absent", "service_unreachable", "service_connection_changed"}:
                        break
                    self._sleep(min(SERVICE_READY_POLL_SECONDS, max(0.0, deadline - self._clock())))
                return self._failed_child(child, pid, published, code, launch_command, contained)
        except _ServiceStartGuardError as exc:
            code = ("service_start_guard_timeout" if str(exc) == "Service start guard timed out."
                    else "service_start_guard_unavailable")
            record, error = self._read_pid_record()
            return self._start_result(code, running=record is not None or error is not None,
                                      pid=int(record["pid"]) if record else None, command=launch_command)

    @staticmethod
    def _child_live(child) -> bool | None:
        try:
            result = child.poll()
            return True if result is None else False if type(result) is int else None
        except Exception:
            return None

    def _failed_child(self, child, pid, published, code, command, contained) -> ProcessResult:
        """Only clean this invocation's child, never a replacement record/process."""
        current, error = self._read_pid_record()
        matching = published is not None and current == published and error is None
        changed = (current is not None or error is not None) and not matching
        confirmed = False
        if matching:
            # No guard recursion. W02a verifies the terminating handle itself.
            try:
                stopped = self._stop_locked(current, None, cleanup=True)
                confirmed = stopped.success and not stopped.running and stopped.action == "stop"
            except Exception:
                confirmed = False
        if not confirmed:
            # Popen is the retained exact child, never a new handle opened by PID.
            confirmed = self._cleanup_started_child(child, pid) == "The started process was stopped."
        if confirmed:
            self._release_launch_barrier()
        if confirmed and matching:
            if not self._unlink_if_same(pid, published["started_at"], expected_record=published):
                changed = True
        current, error = self._read_pid_record()
        if current is not None and published is not None and current != published:
            changed = True
        cleanup = "service_cleanup_confirmed" if confirmed else "service_cleanup_unconfirmed"
        message = SERVICE_DETAILS[code] + " " + SERVICE_DETAILS[cleanup]
        if changed:
            message += " service_record_changed: " + SERVICE_DETAILS["service_record_changed"]
        return self._start_result(code, running=not confirmed or changed, pid=pid if not confirmed else None,
                                  command=command, cleanup_reason=cleanup, contained=contained, message=message)

    def _launch_barrier_present(self) -> bool:
        try:
            self.launch_barrier_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            pass  # An unreadable barrier is never proof of absence.
        return True

    def _reserve_launch_barrier(self) -> None:
        # Exclusive creation, no symlink following or dependency on pidfile
        # publication. The fixed content contains no PID, path, argv or secret.
        with self.launch_barrier_path.open("xb") as marker:
            marker.write(b"service_cleanup_unconfirmed\n")
            marker.flush()
            os.fsync(marker.fileno())

    def _release_launch_barrier(self) -> bool:
        try:
            self.launch_barrier_path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def wait_started_child(self) -> int:
        """Foreground controller waits only after start released the guard."""
        if self._started_child is None:
            return 0
        try:
            result = self._started_child.wait()
            if type(result) is not int:
                return 1
            # Waited the retained Popen, then conditionally clean only its
            # record. A replacement listener/record is never removed.
            try:
                with _service_start_guard(self.pid_path):
                    current, error = self._read_pid_record()
                    if error is None and current == self._started_record:
                        self._unlink_if_same(current['pid'], current['started_at'], expected_record=current)
            except Exception:
                from ..windowless_diagnostics import emit
                emit('backend_failed')
            return result
        except KeyboardInterrupt:
            with _service_start_guard(self.pid_path):
                current, error = self._read_pid_record()
                if error is None and current == self._started_record:
                    self._stop_locked(current, None)
            return 130

    def admit_child(self, *, options: dict[str, object]) -> bool:
        """Child waits for its own record; it never takes the parent's guard."""
        deadline = self._clock() + SERVICE_CHILD_ADMISSION_SECONDS
        while self._clock() < deadline:
            record, error = self._read_pid_record()
            if record is not None and error is None:
                try:
                    pid = os.getpid()
                    created = self._process_created_at(pid)
                    image = self._process_image_path(pid)
                    if (record["pid"] == pid and image is not None and created is not None
                            and _same_image_path(image, record["argv"][0])
                            and _parse_started_at(record["started_at"]) is not None
                            and abs(created - _parse_started_at(record["started_at"])) <= 0.001
                            and "--managed-child" in record["argv"]
                            and _service_options(record["argv"], self.state_dir) == options):
                        return True
                except Exception:
                    pass
            self._sleep(min(SERVICE_READY_POLL_SECONDS, max(0.0, deadline - self._clock())))
        return False

    @staticmethod
    def _cleanup_started_child(child, pid: object) -> str:
        """Stop only the Popen-owned child; a returned wait confirms cleanup."""
        try:
            child.kill()
        except Exception:
            pass
        try:
            child.wait(timeout=SERVICE_STOP_WAIT_SECONDS)
        except Exception:
            label = "unknown" if pid is None else str(pid)
            return f"The started process could not be confirmed stopped (PID {label})."
        return "The started process was stopped."

    def _unlink_if_same(self, pid: int, started_at: object, *, expected_record: dict | None = None) -> bool:
        record, error = self._read_pid_record()
        if record is None and error is None:
            return True  # already gone: no record of this service remains
        if error is not None or record is None or record["pid"] != pid or record["started_at"] != started_at:
            return False
        if expected_record is not None and record != expected_record:
            return False
        try:
            self.pid_path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def stop(self, *, dry_run: bool = False) -> ProcessResult:
        """Stop the recorded process, while refusing ambiguous pidfile records."""

        if not self._is_supported():
            return self._unsupported_result("stop")
        record, error = self._read_pid_record()
        if dry_run or record is None:
            return self._stop_locked(record, error, dry_run=dry_run)
        try:
            with _service_start_guard(self.pid_path):
                record_locked, error_locked = self._read_pid_record()
                return self._stop_locked(record_locked, error_locked)
        except _ServiceStartGuardError as exc:
            return ProcessResult(
                success=False, supported=True, running=record is not None,
                pid=int(record["pid"]), message=str(exc), action="stop",
            )

    def _stop_locked(
        self, record: dict[str, object] | None, error: str | None, *, dry_run: bool = False,
        cleanup: bool = False,
    ) -> ProcessResult:
        # W02b: start's readiness-failure cleanup must call this unguarded
        # inner helper while owning the guard, never re-enter through stop().
        if record is None:
            if self._launch_barrier_present():
                return self._start_result("service_cleanup_unconfirmed", running=True,
                                          action="stop", ready=None, dry_run=dry_run)
            if error is not None:
                return ProcessResult(
                    success=False,
                    supported=True,
                    running=True,
                    pid=None,
                    message=f"Refused to stop a process: {error}",
                    reason="service_pid_record_unavailable",
                    action="stop",
                    dry_run=dry_run,
                )
            return ProcessResult(
                success=True,
                supported=True,
                running=False,
                pid=None,
                message="Service is not running.",
                action="not_running",
                dry_run=dry_run,
            )
        pid = int(record["pid"])
        try:
            present = self._pid_exists(pid)
        except Exception:
            present = None
        if type(present) is not bool:
            return self._start_result("service_process_identity_unconfirmed", running=True,
                                      pid=pid, action="stop", ready=None, dry_run=dry_run)
        if cleanup:
            # Cleanup retains the published marker until exit is confirmed by
            # verified termination or the exact Popen wait. A stale/unreadable
            # PID probe must not remove that marker before an unconfirmed wait.
            identity, _reason = self._prove_service_identity(pid, record)
            if present is not True or identity is not True:
                return self._start_result("service_process_identity_unconfirmed", running=True, pid=pid,
                                          action="stop", ready=None)
        if present is False:
            if dry_run:
                return ProcessResult(
                    success=True,
                    supported=True,
                    running=False,
                    pid=None,
                    message="Would remove the stale service pidfile.",
                    action="would_remove_stale_pidfile",
                    dry_run=True,
                )
            removed = self._unlink_if_same(pid, record["started_at"])
            return ProcessResult(
                success=True,
                supported=True,
                running=False,
                pid=None,
                message=("Service is not running; removed its stale pidfile."
                         + ("" if removed else " A newer service record was left in place.")),
                action="not_running",
            )
        identity, reason = self._prove_service_identity(pid, record)
        expected_created = _parse_started_at(record["started_at"])
        if identity is True and expected_created is None:
            identity, reason = None, "could not read the pidfile start time"
        if identity is False:
            # The recorded service is gone and its PID was reused: a stale
            # record, not a failed stop. Nothing is terminated -- the process
            # holding the PID now may be a Host's MCP server.
            if dry_run:
                return ProcessResult(
                    success=True,
                    supported=True,
                    running=False,
                    pid=None,
                    message=(
                        f"Would remove the stale service pidfile; PID {pid} now belongs to "
                        f"another process ({reason})."
                    ),
                    action="would_remove_stale_pidfile",
                    dry_run=True,
                )
            return self._stale_identity_result(pid, record["started_at"], reason)
        if identity is None:
            return ProcessResult(
                success=False,
                supported=True,
                running=True,
                pid=pid,
                message=(
                    f"Refused to stop PID {pid}: it could not be proven to be the recorded "
                    f"control-plane service ({reason}). Nothing was stopped."
                ),
                action="stop",
                dry_run=dry_run,
                reason="service_process_identity_unconfirmed",
            )
        if dry_run:
            return ProcessResult(
                success=True,
                supported=True,
                running=True,
                pid=pid,
                message=f"Would stop the control-plane service (PID {pid}).",
                action="would_stop",
                dry_run=True,
            )
        try:
            outcome = self._terminate_verified(
                pid, expected_created=expected_created,
                tolerance=SERVICE_IDENTITY_TOLERANCE_SECONDS,
                image_matches=lambda image: _same_image_path(image, record["argv"][0]),
                wait_seconds=SERVICE_STOP_WAIT_SECONDS,
            )
        except Exception:
            outcome = "unconfirmed"
        if outcome == "mismatch":
            if cleanup:
                return self._start_result("service_process_identity_unconfirmed", running=True, pid=pid,
                                          action="stop", ready=None)
            return self._stale_identity_result(
                pid, record["started_at"], "its process identity changed before termination",
            )
        if outcome not in ("terminated", "not_found"):
            return ProcessResult(
                success=False,
                supported=True,
                running=True,
                pid=pid,
                message=f"Could not stop the control-plane service: could not confirm it stopped (PID {pid}).",
                action="stop",
            )
        removed = self._unlink_if_same(pid, record["started_at"], expected_record=record if cleanup else None)
        return ProcessResult(
            success=True,
            supported=True,
            running=False,
            pid=None,
            message=("Stopped the control-plane service."
                     + ("" if removed else " A newer service record was left in place.")),
            action="stop",
        )

    def _stale_identity_result(self, pid: int, started_at: object, reason: str) -> ProcessResult:
        removed = self._unlink_if_same(pid, started_at)
        return ProcessResult(
            success=True, supported=True, running=False, pid=None,
            message=(
                f"Service is not running; its pidfile named PID {pid}, which now belongs to "
                f"another process ({reason}). Removed the stale pidfile and stopped nothing."
                + ("" if removed else " A newer service record was left in place.")
            ),
            action="not_running",
        )
