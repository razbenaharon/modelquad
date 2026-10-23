# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Process spawning utilities for cross-platform console suppression."""

from __future__ import annotations

import errno
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Literal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import uuid

from .executable_resolution import UnsafeExecutable, resolve_executable, system_executable


default_kill_on_close: ContextVar[bool] = ContextVar("default_kill_on_close", default=False)


@dataclass(frozen=True)
class BootObservation:
    session: str
    uptime_ms: int
    observed_at: datetime
    boot_lower: datetime
    boot_upper: datetime
    qualified: bool


def _native_boot_sample():
    """Read-only NT class 90; unavailable on unsupported Windows versions.

    DbgHiberBoot is deliberately unqualified: Fast Startup must not look like
    proof of a new cold boot. Native behavior still needs platform acceptance.
    """
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes

    class BootEnvironment(ctypes.Structure):
        _fields_ = [('identifier', ctypes.c_ubyte * 16),
                    ('firmware_type', wintypes.DWORD), ('flags', ctypes.c_uint64)]

    nt = ctypes.WinDLL('ntdll')
    nt.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p,
                                           wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    nt.NtQuerySystemInformation.restype = wintypes.LONG
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetTickCount64.argtypes = []
    kernel.GetTickCount64.restype = ctypes.c_uint64
    value, length = BootEnvironment(), wintypes.ULONG()
    if nt.NtQuerySystemInformation(90, ctypes.byref(value), ctypes.sizeof(value),
                                   ctypes.byref(length)) != 0 or length.value != ctypes.sizeof(value):
        return None
    identifier = uuid.UUID(bytes_le=bytes(value.identifier))
    if identifier.int == 0:
        return None
    return str(identifier), int(kernel.GetTickCount64()), not bool(value.flags & 2)


def observe_boot(*, clock=None, sample=None) -> BootObservation | None:
    """Bracket independent boot identity/uptime; never trust UTC minus uptime alone."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    sample = sample or _native_boot_sample
    try:
        before = clock()
        first = sample()
        last = sample()
        after = clock()
        if (first is None or last is None or first[0] != last[0]
                or first[2] is not True or last[2] is not True
                or type(first[1]) is not int or not 0 <= first[1] <= last[1]
                or before.utcoffset() != timedelta(0) or after.utcoffset() != timedelta(0)
                or not 0 <= (after - before).total_seconds() <= 1):
            return None
        # 16 ms bounds the documented ~10--16 ms tick resolution; the full
        # bracket adds observation uncertainty, not a historical clock tolerance.
        return BootObservation(first[0], last[1], after,
                               before - timedelta(milliseconds=last[1] + 16),
                               after - timedelta(milliseconds=max(0, first[1] - 16)), True)
    except Exception:
        return None


def _read_only_process_api():
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def native_process_creation_epoch(pid: int) -> float | None:
    """The existing FILETIME representation through a typed read-only binding."""
    try:
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            return None
        api = _read_only_process_api()
        if api is None:
            return None
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            return _creation_epoch_of_handle(api, handle)
        finally:
            api.CloseHandle(handle)
    except Exception:
        return None


@dataclass(frozen=True)
class CodexProcess:
    pid: int
    created_at: str | None


@dataclass(frozen=True)
class CodexProcesses:
    observed_at: str
    status: Literal['complete', 'partial', 'unavailable']
    processes: tuple[CodexProcess, ...]
    reason: Literal['snapshot_unavailable', 'entries_unreadable_or_bounded'] | None = None


def live_codex_processes(*, clock=None, snapshot=None, state=None, created=None,
                         limit=64) -> CodexProcesses:
    """Transient basenames only. No paths, argv, owners or workspace inference."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    stamp = clock().isoformat()
    snapshot = snapshot or (_windows_process_snapshot if os.name == 'nt' else lambda: None)
    state = state or pid_state
    created = created or native_process_creation_epoch
    try:
        rows = snapshot()
        if rows is None:
            return CodexProcesses(stamp, 'unavailable', (), 'snapshot_unavailable')
        result, partial = [], False
        for pid, _parent, image in rows:
            if image.casefold() != 'codex.exe':
                continue
            live = state(pid)
            if live == 'dead':
                continue
            if live != 'alive':
                partial = True
                continue
            if len(result) >= max(1, min(limit, 64)):
                partial = True
                continue
            epoch = created(pid)
            readable = type(epoch) in (int, float) and math.isfinite(epoch) and epoch > 0
            start = datetime.fromtimestamp(epoch, timezone.utc).isoformat() if readable else None
            partial |= start is None
            result.append(CodexProcess(pid, start))
        return CodexProcesses(stamp, 'partial' if partial else 'complete', tuple(result),
                              'entries_unreadable_or_bounded' if partial else None)
    except Exception:
        return CodexProcesses(stamp, 'unavailable', (), 'snapshot_unavailable')


_DEVNULL = subprocess.DEVNULL

# How long the post-kill output drain may wait for the captured pipes to close.
# Only the timeout path reaches it, and only a descendant that outlived the
# tree stop can hold it: the happy path never waits here at all.
_POST_KILL_DRAIN_SECONDS = 10.0

POLL_SECONDS = 1.0


class WorkerStopRequested(subprocess.SubprocessError):
    """An explicit stop, with captured output and observed termination facts."""

    def __init__(self, argv, *, stdout=None, stderr=None, worker_termination=None):
        super().__init__("Worker stop requested")
        self.cmd = argv
        self.stdout = self.output = stdout
        self.stderr = stderr
        self.worker_termination = worker_termination or {}


# How long a call that is returning a completed worker waits for its stdin
# writer. The writer is still blocked only when a process other
# than the worker holds the pipe's read end and does not read; the worker has
# exited, so its result must not wait on that process.
_STDIN_WRITER_JOIN_SECONDS = 1.0

# How long a stop may wait for the worker's job to read empty before it says
# the tree did not die. Terminating a job takes milliseconds (measured
# 3-115 ms); the bound exists so a member that will not die cannot
# hold the timeout path open.
_JOB_EMPTY_SECONDS = 5.0

# Provisional, to be measured later; opt-in close cleanup only.
_CLOSE_CLEANUP_SECONDS = 1.0

# `CreateProcess` flag: the new process's primary thread starts suspended.
_CREATE_SUSPENDED = 0x00000004


class WorkerContainmentError(OSError):
    """The worker was created but never ran, because it could not be contained.

    Raised by :func:`run_with_tree_kill` on Windows when the worker could not
    be put in its Job Object before its first instruction. The
    process is stopped while still suspended, so nothing of the worker's own
    ran and nothing it could have spawned exists. It is an ``OSError`` so the
    executors' existing launch-failure branch reports it as ``launch_failed``,
    which is exactly what happened.
    """


# Windows creates a NEW console window whenever a console application is
# spawned from a parent that has no console of its own -- a windowless MCP
# host, the pythonw.exe service controller, and any GUI parent all hit
# this. Every synchronous worker/probe invocation therefore passes
# CREATE_NO_WINDOW so delegation stays invisible to the user. The flag does
# not exist (and is not needed) on other platforms. Kept as a helper rather
# than inlined so a future executor cannot silently forget it.
def no_window_kwargs(os_name: str | None = None, *, detach_stdin: bool = True) -> dict[str, object]:
    """Return safe defaults for a non-interactive child process.

    ``os_name`` exists purely so tests can exercise both branches without
    monkeypatching the global ``os.name`` -- doing that breaks ``pathlib``
    (``Path`` would try to instantiate ``PosixPath`` on Windows).

    ``detach_stdin`` is the deliberate opt-out for the few callers that drive
    the child's stdin themselves -- passing ``input=`` to ``subprocess.run``
    (which refuses to accept ``stdin`` as well) or handing it a pipe to speak a
    protocol over. Those are already non-interactive by construction. It is a
    keyword, and named, so that every exception to the default is visible at
    the call site and greppable from here.
    """

    effective = os.name if os_name is None else os_name
    # A hidden parent can still own a real console input handle. Passing that
    # handle to a child lets a Windows utility prompt invisibly and wait
    # forever. Product-owned probes and process-control commands never need
    # interactive input, so detach it at the common spawning boundary.
    kwargs: dict[str, object] = {"stdin": _DEVNULL} if detach_stdin else {}
    if effective == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


def _verified_termination_api():
    """Private, fully typed binding; replaceable by deterministic test fakes."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _image_path_of_handle(kernel32, handle) -> str | None:
    import ctypes
    from ctypes import wintypes

    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value[:size.value] or None


def terminate_verified(
    pid: int, *, expected_created: float, tolerance: float,
    image_matches: Callable[[str], bool], wait_seconds: float,
) -> Literal["terminated", "not_found", "mismatch", "unconfirmed"]:
    """Verify identity, terminate and await exit through one Windows handle."""
    try:
        import ctypes

        kernel32 = _verified_termination_api()
        if kernel32 is None:
            return "unconfirmed"
        ctypes.set_last_error(0)
        # PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        handle = kernel32.OpenProcess(0x0001 | 0x1000 | 0x00100000, False, int(pid))
        if not handle:
            return "not_found" if ctypes.get_last_error() == 87 else "unconfirmed"
        try:
            state = kernel32.WaitForSingleObject(handle, 0)
            if state == 0:
                return "not_found"
            if state != 0x102:  # WAIT_TIMEOUT is the only proven live state.
                return "unconfirmed"
            created = _creation_epoch_of_handle(kernel32, handle)
            if created is None:
                return "unconfirmed"
            image = _image_path_of_handle(kernel32, handle)
            if image is None:
                return "unconfirmed"
            if abs(created - expected_created) > tolerance or not image_matches(image):
                return "mismatch"
            if not kernel32.TerminateProcess(handle, 1):
                # An already exiting process can deny termination but signal.
                return "terminated" if kernel32.WaitForSingleObject(handle, 0) == 0 else "unconfirmed"
            state = kernel32.WaitForSingleObject(handle, int(wait_seconds * 1000))
            return "terminated" if state == 0 else "unconfirmed"
        finally:
            # A failed close cannot change what the handle already proved.
            kernel32.CloseHandle(handle)
    except Exception:  # an API failure cannot prove that the service stopped
        return "unconfirmed"


def terminate_pid(pid: int) -> bool:
    """Terminate exactly one PID through the Windows process API.

    This is intentionally a product-owned, PID-scoped operation. Callers
    must establish ownership before invoking it; this helper never broadens a
    stop request into an image-name or process-tree sweep.
    """

    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    invalid_handle = wintypes.HANDLE(-1).value
    handle = kernel32.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
    if not handle or handle in (-1, invalid_handle, ctypes.c_void_p(-1).value):
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


#: The private fourth reading :func:`_windows_openprocess_state` can return.
#: It is deliberately NOT one of :func:`pid_state`'s three answers -- it is
#: the observation that ``OpenProcess`` was refused, which is not yet a
#: statement about whether the process exists.
_ACCESS_DENIED = "access_denied"


def _windows_openprocess_state(pid: int) -> str:
    """What ``OpenProcess`` on its own can say about ``pid``.

    Returns one of :func:`pid_state`'s three answers, or ``_ACCESS_DENIED``
    for the one error code that settles nothing by itself. Split out from
    :func:`pid_state` so the decision that follows a denial is readable, and
    testable, without a real process in either state.
    """

    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ctypes.set_last_error(0)
        from ctypes import wintypes
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == ERROR_ACCESS_DENIED:
                return _ACCESS_DENIED
            if error == ERROR_INVALID_PARAMETER:
                return "dead"
            return "unknown"
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "unknown"
            return "alive" if code.value == STILL_ACTIVE else "dead"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 -- a probe must never raise
        return "unknown"


def pid_state(pid: int) -> str:
    """Is this pid ``"alive"``, ``"dead"``, or is the answer ``"unknown"``?

    The third answer is the point. :func:`pid_alive`
    is a two-valued reading of a three-valued question: a probe that *cannot
    answer* -- ``OpenProcess`` handing back no handle because the process
    belongs to another user or to the kernel -- came back as False, which the
    reconcile passes then read as "the worker is dead" and used to close a
    LIVE worker's attempt row (releasing duplicate protection with it). A
    failure to observe is not an observation. This function keeps the two
    apart so callers that must not guess can refuse to.

    Windows: a handle plus ``GetExitCodeProcess`` answers directly. Without a
    handle the error code decides -- ``ERROR_INVALID_PARAMETER`` (87) is what
    a pid with no process behind it returns ("dead"), and anything other than
    ``ERROR_ACCESS_DENIED`` is "unknown".

    ``ERROR_ACCESS_DENIED`` (5) is the one answer ``OpenProcess`` cannot
    settle by itself. It is returned for a process that exists
    and is merely not ours to open -- another user's, or the kernel's -- and
    it is *also* returned for one that has already exited but whose process
    object a handle held by some other process keeps from being freed. Such a
    "zombie" has no token left for the access check to pass, no user-mode
    enumerator lists it, and it cannot be writing anything; reading it as
    "alive" left a settled workspace unsettled permanently rather than "a
    little longer". So the denial is corroborated by an enumeration that
    needs no access to the target (``CreateToolhelp32Snapshot``): present in
    that snapshot is "alive", absent from it is "dead", and a snapshot that
    cannot be taken leaves the fail-safe "unknown" in place -- two probes
    that cannot answer are still not an observation.

    POSIX: ``os.kill(pid, 0)`` succeeds for a live process,
    ``ProcessLookupError`` means no such process, ``PermissionError`` means it
    exists and is not ours to signal, and any other error is "unknown".

    A recycled pid can still read "alive" during the reuse window; that error
    is conservative (a duplicate refusal, or a row staying open a little
    longer), never destructive.
    """

    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if numeric <= 0:
        # Not a pid this product ever records. There is no process to find,
        # and no probe was prevented from finding one.
        return "dead"
    if os.name == "nt":
        observed = _windows_openprocess_state(numeric)
        if observed != _ACCESS_DENIED:
            return observed
        # "Not yours to open" is true of a live process owned by someone
        # else and equally true of an already-exited one a leaked handle is
        # holding open. Only an enumeration that does not have to
        # open the target can tell the two apart.
        present = _windows_pid_in_snapshot(numeric)
        if present is None:
            return "unknown"
        return "alive" if present else "dead"
    try:
        os.kill(numeric, 0)
        return "alive"
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"  # it exists; it is simply not ours to signal
    except OSError:
        return "unknown"


def process_image_name(pid: int, *, run=subprocess.run) -> str | None:
    """The executable image name the OS reports for ``pid``, or None.

    ``None`` means the question could not be answered -- the lookup tool is
    missing, timed out, failed, or reported something other than exactly one
    process -- and callers must treat it as "unknown", never as "matches" or
    "does not match". The answer is the bare file name (``python.exe``,
    ``orchestrator.exe``), so it compares against the basename of a recorded
    launch command. Windows asks ``tasklist``; POSIX asks ``ps``. Both are
    spawned without a console and with stdin detached (the
    identity half of "is the recorded service pid really that service?").
    """

    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    if os.name == "nt":
        executable = system_executable("tasklist")
        if executable.path is None:
            return None
        argv = [executable.path, "/FI", f"PID eq {numeric}", "/FO", "CSV", "/NH"]
    else:
        executable = resolve_executable("ps")
        if executable.path is None:
            return None
        argv = [executable.path, "-p", str(numeric), "-o", "comm="]
    try:
        completed = run(
            argv, capture_output=True, text=True, shell=False, timeout=15.0, **no_window_kwargs()
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = getattr(completed, "stdout", "") or ""
    if os.name == "nt":
        import csv

        try:
            rows = [row for row in csv.reader(output.splitlines()) if row]
        except csv.Error:
            return None
        if len(rows) != 1 or len(rows[0]) < 2 or rows[0][1] != str(numeric):
            return None
        image = rows[0][0].strip()
    else:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if len(lines) != 1:
            return None
        image = os.path.basename(lines[0])
    return image or None


def process_creation_epoch(pid: int, *, run=subprocess.run, os_name: str | None = None) -> float | None:
    """When the process ``pid`` was created, as a UTC epoch, or None.

    The second half of process identity: a pid can be recycled
    by another program with the same image name, but a process created
    *after* a record was written cannot be the process that record names.
    Windows answers from ``GetProcessTimes`` on a query-limited handle (no
    subprocess). POSIX asks ``ps`` for ``etime`` -- the process's elapsed
    time -- and subtracts it from the wall clock, so no time zone, locale or
    DST transition takes part: a zoned ``lstart`` read back through
    ``mktime`` differs from the aware UTC record by the local offset.
    ``etime`` has one-second resolution; callers that compare
    it against a timestamp must allow for that. ``None`` means the question
    could not be answered and must be read as "unknown", never as "before"
    or "after".

    Known limit, POSIX only: the kernel keeps no absolute creation
    timestamp, so every reading (``etime``, ``lstart``, ``/proc`` start
    ticks) is uptime-relative and re-anchored to the *current* wall clock.
    A wall-clock step between the moment a record was written and this
    probe shifts the reconstructed instant by the size of the step. Callers
    ordering it against a recorded timestamp get, in that case, a wrong
    answer in the direction of the step; on Windows ``GetProcessTimes``
    reports an absolute instant and has no such limit.
    """

    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    if (os.name if os_name is None else os_name) == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, numeric)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
            finally:
                kernel32.CloseHandle(handle)
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            # FILETIME: 100 ns intervals since 1601-01-01 UTC.
            return ticks / 10_000_000 - 11_644_473_600
        except Exception:  # noqa: BLE001 -- a probe must never raise
            return None
    import time

    executable = resolve_executable("ps")
    if executable.path is None:
        return None
    try:
        completed = run(
            [executable.path, "-p", str(numeric), "-o", "etime="],
            capture_output=True, text=True, shell=False, timeout=15.0,
            env={**os.environ, "LC_ALL": "C"},
            **no_window_kwargs(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    observed_at = time.time()
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in (getattr(completed, "stdout", "") or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    elapsed = parse_ps_etime(lines[0])
    if elapsed is None:
        return None
    return observed_at - elapsed


def parse_ps_etime(text: str) -> float | None:
    """Seconds in a ``ps -o etime`` value, or None for any other shape.

    Exactly the canonical shapes Linux procps and BSD/macOS ``ps`` emit:
    ``mm:ss``, ``hh:mm:ss`` and ``dd-hh:mm:ss``, with two-digit minute and
    second fields below 60 and hours below 24 when a day count is present.
    Anything else is unknown, not a guess.
    """

    value = text.strip()
    days = 0
    if "-" in value:
        day_part, _, value = value.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
        if value.count(":") != 2:
            return None
    parts = value.split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() and len(part) == 2 for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        hours, minutes, seconds = 0, numbers[0], numbers[1]
    else:
        hours, minutes, seconds = numbers
        if days and hours >= 24:
            return None
    if minutes >= 60 or seconds >= 60:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def pid_alive(pid: int) -> bool:
    """Best-effort check whether a process with this pid is currently running.

    Two-valued by construction, and therefore only for callers that have
    somewhere sensible to put a "no": an unanswerable probe reads False here.
    Anything that would CLOSE a record or release a guard on the strength of
    that False must ask :func:`pid_state` instead and act on ``"dead"`` alone.
    """

    return pid_state(pid) == "alive"


def _kill_process_tree(
    proc: subprocess.Popen, census_job: "int | None" = None, created_after: "float | None" = None
) -> dict:
    """Terminate ``proc`` and every descendant it spawned, and say how it went.

    The answer is the point: a timed-out worker
    used to be *attempted* dead and never *recorded* dead, so a caller that
    later asked "is anything still running?" had nothing to read.

    On Windows the worker has been a member of ``census_job`` since before its
    first instruction (:func:`_contain_suspended_worker`), and the job is what
    stops the tree: ``TerminateJobObject`` reaches every member, including a
    grandchild whose parent already exited and a child born while the stop
    runs -- the two shapes ``taskkill /T`` missed while still exiting 0.
    It is called first, and in a ``finally``, so nothing -- not
    a second Ctrl+C -- can skip it. Windows also starts some processes outside
    the job on a member's behalf (an App Execution Alias target, such as Store
    PowerShell). Those are found by parent pid from the worker and the
    members, which are held open for the whole stop so that their numbers
    cannot be reused, and stopped by exact pid
    (:func:`_descendants_outside_job`). ``tree_kill`` is read AFTER the stop,
    never from any tool's exit code:

    - ``ok``: the census read empty twice in a row, and every descendant found
      outside the job is dead.
    - ``job_not_empty``: members were still listed when the bounded wait
      ended; ``job_members`` says how many.
    - ``census_unknown``: the census could not be read. Unknown is not empty.
    - ``escaped_job``: a descendant outside the job was still alive after it
      was stopped; ``escaped`` says how many.
    - ``outside_unknown``: the process table could not be read to look for
      descendants outside the job.
    - ``uncontained``: there was no job to stop and count with. The runner
      never starts a worker without one, so this is only ever a defect showing.
    - ``direct_child_only``: POSIX, where there is no tree to reach.

    ``job_terminate``, ``outside_job`` (processes stopped outside the job) and
    ``taskkill`` record the attempts themselves -- evidence of what was tried,
    not of what died. ``taskkill`` stays as a last, PID-scoped sweep; nothing
    here ever kills by image name.

    **What ``ok`` does not cover.** A process that nothing held links to the
    worker: one Windows started outside the job for a member that had already
    exited when the stop began (``cmd /c start`` of an alias target), a child
    such a process left behind when it exited, or one a broker started on the
    worker's behalf (COM, the Task Scheduler, a service). No process-tree
    stop can see those, and ``taskkill /T`` never did either. Containing
    alias targets in the job while the worker runs would close the first two;
    it is not done here.

    Whatever happened, the direct child is ALWAYS killed and this function
    ALWAYS returns an outcome: it may not raise. A ``taskkill`` that could not
    even be launched used to escape as a ``FileNotFoundError``, which skipped
    the fallback kill, lost the termination facts, and left the executor
    calling a worker that had really started ``failed_to_start``.
    """
    outcome: dict = {"tree_kill": "direct_child_only"}
    if os.name != "nt":
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 -- the fallback kill is best-effort too
            pass
        return outcome
    pins = _Pins()
    try:
        stopped: "list[int] | None" = []
        terminated = walked = False
        try:
            if census_job:
                # Hold the roots before anything dies: a held pid cannot be
                # handed to another process, so the walk below can follow a
                # dead member's number and still mean that member.
                pins.pin(proc.pid)
                for member in _census_job_pids(census_job) or ():
                    pins.pin(member)
                outcome["job_terminate"] = "ok" if _terminate_job(census_job) else "failed"
                terminated = True
                stopped = _stop_outside_job(pins, census_job, created_after)
                walked = True
            outcome["taskkill"] = _taskkill_tree(proc.pid)
        finally:
            # A second interrupt may land anywhere above; the stops still run.
            if census_job and not terminated:
                _terminate_job(census_job)
            if census_job and not walked:
                try:
                    _stop_outside_job(pins, census_job, created_after)
                except Exception:  # noqa: BLE001 -- best effort on the way out
                    pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 -- the fallback kill is best-effort too
                pass
        if not census_job:
            outcome["tree_kill"] = "uncontained"
            return outcome
        outcome.update(_await_empty_job(census_job))
        if outcome["tree_kill"] == "ok":
            outcome.update(_account_outside_job(pins, census_job, created_after, stopped))
        return outcome
    finally:
        pins.close()


def _taskkill_tree(pid: int) -> str:
    """``taskkill /PID <pid> /T /F``, as a best-effort sweep, and how it went."""

    try:
        executable = system_executable("taskkill")
        if executable.path is None:
            raise UnsafeExecutable(executable.refusal or "not_found")
        walked = subprocess.run(
            [executable.path, "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10.0,
            **no_window_kwargs(),
        )
        return "ok" if walked.returncode == 0 else "exit " + str(walked.returncode)
    except subprocess.TimeoutExpired:
        # A timeout here must never leave the caller waiting indefinitely for
        # a best-effort sweep.
        return "timed_out"
    except Exception as exc:  # noqa: BLE001 -- taskkill missing, denied, ...
        # A cleanup tool that will not start is a fact about the cleanup, not
        # about the worker: name it and keep going.
        return "launch_error: " + type(exc).__name__


def _await_empty_job(job: int) -> dict:
    """Read the job census until it is empty, within :data:`_JOB_EMPTY_SECONDS`.

    A member still listed is terminated again: it may have been created after
    the last ``TerminateJobObject``. Nothing can join a job except through a
    member, so an empty job stays empty; the second empty reading is there so
    ``ok`` never rests on a single sample. A census that cannot be read ends
    the wait at once as ``census_unknown`` -- a failure to observe is not an
    observation (:func:`pid_state`).
    """

    import time

    deadline = time.monotonic() + _JOB_EMPTY_SECONDS
    empty_in_a_row = 0
    while True:
        members = _census_job_pids(job)
        if members is None:
            return {"tree_kill": "census_unknown"}
        if members:
            empty_in_a_row = 0
            if time.monotonic() >= deadline:
                return {"tree_kill": "job_not_empty", "job_members": len(members)}
            _terminate_job(job)
        else:
            empty_in_a_row += 1
            if empty_in_a_row >= 2:
                return {"tree_kill": "ok"}
        time.sleep(0.05)


def _stop_outside_job(pins: "_Pins", job: int, created_after: "float | None") -> "list[int] | None":
    """Stop, by exact pid, every held-root descendant the job does not hold.

    Returns the pids found (stopped, or at least attempted), or ``None`` when
    the process table could not be read.
    """

    found = _descendants_outside_job(
        dict(pins.created), set(pins.created), created_after,
        in_job=lambda pid: _process_in_job(pid, job),
    )
    if found is None:
        return None
    for pid, _image, created in found:
        if pins.pin(pid, expected=created) is not None:
            _terminate_exact(pid, created)
    return [pid for pid, _image, _created in found]


def _account_outside_job(
    pins: "_Pins", job: int, created_after: "float | None", stopped: "list[int] | None"
) -> dict:
    """Once the job is empty: is anything descended from the tree still alive outside it?

    A second walk runs from every held process, because a process outside
    the job may have started another while the stop ran; what it finds is
    stopped by exact pid too. Then the wait, bounded, for everything stopped
    outside the job to exit -- read through the held handles, so a reused
    number cannot pass for a survivor or hide one.
    """

    if stopped is None:
        return {"tree_kill": "outside_unknown"}
    later = _stop_outside_job(pins, job, created_after)
    if later is None:
        return {"tree_kill": "outside_unknown"}
    outside = set(stopped) | set(later)
    facts: dict = {}
    if outside:
        facts["outside_job"] = len(outside)
    alive = pins.still_running(outside, seconds=2.0)
    if alive:
        facts.update({"tree_kill": "escaped_job", "escaped": len(alive)})
    return facts


def _descendants_outside_job(
    roots: "dict[int, float | None]",
    members: "set[int]",
    created_after: "float | None",
    *,
    in_job=None,
) -> "list[tuple[int, str, float]] | None":
    """Live processes descended by parent pid from ``roots`` that the job does not hold.

    Job membership is inherited by every process a member creates -- except
    the ones Windows creates for it. An App Execution Alias target (Store
    PowerShell, the Python install manager's ``python``) is started by the
    system with the member as its parent pid but outside its job, and so is
    everything it starts in turn. Membership cannot see them;
    parent pids can, while the links last. Each entry is
    ``(pid, image, created)``.

    A parent pid is a number, and numbers are reused. The caller holds every
    root open (:class:`_Pins`), so a root's number keeps naming that root,
    dead or alive; a root with no creation time is not followed, and one whose
    number now belongs to a process created at another time is skipped. A
    process is taken only if it was created no earlier than the process it
    names as parent and no earlier than ``created_after`` (the worker's own
    creation), and never if its creation time cannot be read: ownership that
    cannot be shown is not assumed, least of all before a stop. ``in_job``,
    when given, says whether a process is (still) a job member; members are
    walked through but not returned. ``None`` means the process table could
    not be read.
    """

    entries = _windows_process_snapshot()
    if entries is None:
        return None
    children: dict[int, list[tuple[int, str]]] = {}
    live: set[int] = set()
    for entry_pid, parent_pid, image in entries:
        live.add(entry_pid)
        children.setdefault(parent_pid, []).append((entry_pid, image))
    frontier: list[tuple[int, float]] = []
    for pid, created in roots.items():
        if created is None:
            continue  # an undated number proves nothing about its children
        if pid in live and process_creation_epoch(pid) != created:
            continue  # that number now names a different process
        frontier.append((pid, created))
    seen = set(roots) | set(members)
    found: list[tuple[int, str, float]] = []
    while frontier:
        parent, parent_created = frontier.pop()
        for child, image in children.get(parent, ()):
            if child in seen:
                continue
            created = process_creation_epoch(child)
            if created is None or created < parent_created:
                continue  # undatable, or older than its supposed parent
            if created_after is not None and created < created_after:
                continue
            seen.add(child)
            frontier.append((child, created))
            if in_job is not None and in_job(child):
                continue  # a member the census read missed: the job holds it
            found.append((child, image, created))
    return found


def _creation_epoch_of_handle(kernel32, handle) -> "float | None":
    from ctypes import byref, wintypes

    creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel32.GetProcessTimes(handle, byref(creation), byref(exit_time), byref(kernel), byref(user)):
        return None
    ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return ticks / 10_000_000 - 11_644_473_600


def _terminate_exact(pid: int, created: float) -> bool:
    """Terminate ``pid`` only if it is still the process created at ``created``.

    The creation time is read through the same handle that terminates, so a
    pid recycled in between can never be the one stopped.
    """

    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        # PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(0x0001 | 0x1000, False, int(pid))
        if not handle:
            return False
        try:
            if _creation_epoch_of_handle(kernel32, handle) != created:
                return False
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 -- a stop that cannot be made is reported by the census, not raised
        return False


def _process_in_job(pid: int, job: int) -> "bool | None":
    """Is ``pid`` a member of ``job`` right now? None if it cannot be asked."""

    if os.name != "nt" or not job:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            result = wintypes.BOOL(False)
            if not kernel32.IsProcessInJob(handle, wintypes.HANDLE(job), ctypes.byref(result)):
                return None
            return bool(result.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


class _Pins:
    """Handles held on the processes a stop or a census reasons about.

    While a handle is open, Windows cannot hand that process's pid to anyone
    else, so "this number is that process" stays true for as long as the
    reasoning needs it -- after the process has died, too. :meth:`close`
    releases them all; nothing is terminated through these handles.
    """

    def __init__(self) -> None:
        self.created: "dict[int, float]" = {}
        self._handles: "dict[int, int]" = {}
        self._kernel32 = None

    def _api(self):
        if self._kernel32 is None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32 = kernel32
        return self._kernel32

    def pin(self, pid: int, expected: "float | None" = None) -> "float | None":
        """Hold ``pid`` and return its creation time; None if it cannot be held.

        With ``expected``, only the process created at that instant is held.
        """

        if pid in self.created:
            return self.created[pid] if expected in (None, self.created[pid]) else None
        if os.name != "nt":
            return None
        try:
            kernel32 = self._api()
            # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
            handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, int(pid))
            if not handle:
                return None
            created = _creation_epoch_of_handle(kernel32, handle)
            if created is None or (expected is not None and created != expected):
                kernel32.CloseHandle(handle)
                return None
            self._handles[pid] = handle
            self.created[pid] = created
            return created
        except Exception:  # noqa: BLE001
            return None

    def still_running(self, pids, *, seconds: float) -> "list[int]":
        """Which of ``pids`` are still running after waiting up to ``seconds``.

        A held process is read through its handle. One that could not be held
        counts as running unless the process table reads it dead: a failure
        to observe is not an observation.
        """

        import time

        deadline = time.monotonic() + seconds
        pending = list(pids)
        while True:
            running = []
            for pid in pending:
                handle = self._handles.get(pid)
                if handle is not None:
                    if self._api().WaitForSingleObject(handle, 0) != 0:
                        running.append(pid)
                elif pid_state(pid) != "dead":
                    running.append(pid)
            if not running or time.monotonic() >= deadline:
                return running
            pending = running
            time.sleep(0.05)

    def close(self) -> None:
        if not self._handles:
            return
        try:
            kernel32 = self._api()
            for handle in self._handles.values():
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
        self._handles.clear()


def list_descendants(
    pid: int,
    *,
    created_after: float | None = None,
    process_group: bool = False,
    run=subprocess.run,
    os_name: str | None = None,
) -> list[dict] | None:
    """The live processes descended from ``pid``, or None if that cannot be told.

    ``os_name`` exists so a test can exercise the other platform's branch
    without patching the global ``os.name`` (which breaks ``pathlib``).

    A worker that exits normally may leave a child running (on
    Windows the ordinary shape of a provider CLI is a ``.cmd`` shim spawning
    ``node.exe``), and nothing enumerated those on a normal exit. This does,
    without touching them.

    Windows walks a ``CreateToolhelp32Snapshot`` by parent pid. A child keeps
    its parent's pid after the parent dies, so the walk can start at a worker
    that has already exited. It takes only processes created after
    ``created_after`` (the worker's own creation time), so a pid recycled by
    an unrelated process cannot be adopted as a descendant. POSIX reparents
    orphans, so it enumerates the worker's **process group** instead
    (``process_group=True``; the runner spawns workers in their own session),
    through ``ps``.

    Each entry carries ``pid``, ``image`` (executable basename, never a path
    or a command line -- ``PRIVACY.md``) and ``created`` (epoch, or None when
    it could not be read). ``None`` means enumeration itself failed and the
    caller must not read that as "nothing"; an empty list is an observation.

    On Windows an entry the snapshot listed but :func:`pid_state` positively
    reads as ``"dead"`` is left out: it exited between the snapshot and the
    probe, and that is not a process anyone left behind.
    ``"alive"`` and ``"unknown"`` are both listed -- a probe that could not
    answer is not an observation.
    """

    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    effective = os.name if os_name is None else os_name
    if effective == "nt":
        return _list_descendants_toolhelp(numeric, created_after=created_after)
    if not process_group:
        # Without a group of its own, a reparented grandchild is invisible
        # to a parent-pid walk; say so rather than report a partial view.
        return None
    executable = resolve_executable("ps")
    if executable.path is None:
        return None
    try:
        completed = run(
            [executable.path, "-e", "-o", "pid=,pgid=,comm="],
            capture_output=True, text=True, shell=False, timeout=15.0,
            env={**os.environ, "LC_ALL": "C"},
            **no_window_kwargs(effective),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    found: list[dict] = []
    for line in (getattr(completed, "stdout", "") or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        member, group, command = int(parts[0]), int(parts[1]), parts[2].strip()
        if group != numeric or member == numeric:
            continue
        created = process_creation_epoch(member, run=run, os_name=effective)
        if created_after is not None and created is not None and created < created_after:
            continue
        found.append({"pid": member, "image": os.path.basename(command) or None, "created": created})
    return found


def _windows_process_snapshot() -> list[tuple[int, int, str]] | None:
    """Every process Windows will enumerate, as ``(pid, parent pid, image)``.

    ``CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS)`` is the enumeration that
    needs no access to the processes it lists. That is what makes it usable
    for two different questions: the descendant walk below, and the
    corroborating second opinion :func:`pid_state` needs when ``OpenProcess``
    is refused.

    ``None`` means the table could not be read *completely* -- the snapshot
    could not be taken, or the walk stopped for a reason other than
    exhaustion. A partial table read as complete is a false "not present",
    which is the whole failure this refuses to produce, so it is never
    returned as a short list.
    """

    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        ERROR_BAD_LENGTH = 24
        ERROR_NO_MORE_FILES = 18

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = None
        for _attempt in range(5):
            # The documentation's own instruction: a snapshot that fails with
            # ERROR_BAD_LENGTH is retried until it succeeds.
            ctypes.set_last_error(0)
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot and snapshot != INVALID_HANDLE_VALUE:
                break
            snapshot = None
            if ctypes.get_last_error() != ERROR_BAD_LENGTH:
                break
        if snapshot is None:
            return None
        entries: list[tuple[int, int, str]] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ctypes.set_last_error(0)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                entries.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile)))
                ctypes.set_last_error(0)
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            # A false return is exhaustion only when the API says so. Any
            # other error is a scan that did not finish, and a partial table
            # read as complete is exactly the false "nothing" this exists
            # to prevent.
            if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
                return None
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:  # noqa: BLE001 -- a probe must never raise
        return None
    return entries


def _windows_pid_in_snapshot(pid: int) -> bool | None:
    """Does a complete process enumeration list ``pid``? ``None`` if none could be taken.

    The corroboration :func:`pid_state` applies to ``ERROR_ACCESS_DENIED``.
    ``False`` is only ever returned from a snapshot that finished:
    "absent" has to mean absent, never "the scan stopped early".
    """

    entries = _windows_process_snapshot()
    if entries is None:
        return None
    return any(entry_pid == pid for entry_pid, _parent, _image in entries)


def _list_descendants_toolhelp(pid: int, *, created_after: float | None) -> list[dict] | None:
    entries = _windows_process_snapshot()
    if entries is None:
        return None
    children: dict[int, list[tuple[int, str]]] = {}
    for entry_pid, parent_pid, image in entries:
        children.setdefault(parent_pid, []).append((entry_pid, image))
    found: list[dict] = []
    seen: set[int] = {pid}
    frontier = [pid]
    while frontier:
        parent = frontier.pop()
        for child, image in children.get(parent, ()):
            if child in seen:
                continue
            seen.add(child)
            # Whatever this entry is, its children are still walked: an
            # exclusion below never hides a subtree.
            frontier.append(child)
            if _is_console_host_not_left_behind(child, image):
                continue
            created = process_creation_epoch(child)
            state = pid_state(child)
            if state == "dead":
                # Gone between the snapshot and the probe: it was in the
                # table a moment ago and is not running now. A process that
                # has exited is not left behind by anyone.
                #
                # The reading decides on its own, whether or not the creation
                # time came back. ``GetProcessTimes`` still
                # answers for a process that has exited while some other
                # process holds a handle to it -- a measurement showed exactly that
                # on Windows 11 (image path None, creation epoch a real
                # number, ``pid_state`` "dead") -- so a readable ``created``
                # is not evidence of liveness, and requiring it to be
                # unreadable listed such a pid as a left-behind descendant
                # for every image except the ``conhost.exe`` that an earlier name
                # gate happened to cover.
                continue
            if created is None and state == "unknown":
                # Nothing could be read about it at all: no creation time and
                # no liveness either. Unchanged, and deliberately not an
                # exclusion on "unknown" alone -- a failure to observe is not
                # an observation (:func:`pid_state`), so an entry that is
                # merely unreadable while still enumerable stays listed.
                continue
            if created_after is not None and created is not None and created < created_after:
                # Older than the worker: a pid the worker's own pid number
                # was recycled from, not something it spawned.
                continue
            found.append({"pid": child, "image": image or None, "created": created})
    return found


def _is_console_host_not_left_behind(pid: int, image: str) -> bool:
    """Is this ``conhost.exe`` descendant one the worker did not leave behind?

    Every worker runs CREATE_NO_WINDOW, which still allocates a console, and
    the console subsystem attaches ``conhost.exe`` to it as a child of the
    worker. Windows owns that process, the worker never launched it, it
    cannot write a workspace, and it can outlive its client by an instant --
    exactly when the walk runs. A name is not ownership, so it is excluded
    only when its full image path is the system directory's ``conhost.exe``;
    a worker-owned executable that happens to carry the name is listed like
    anything else.

    The path can also fail to arrive, and that ``None`` carries two different
    facts. For a LIVE process it means ownership could not be
    established, and unproven ownership is listed -- the fail-closed rule.
    For a process the table reports DEAD it means the console host exited
    between the snapshot that produced the pid and this query, which is the
    very instant above: ``OpenProcess`` fails once the process object is
    gone, and ``QueryFullProcessImageNameW`` fails even on a handle whose
    process is merely signalled, because the image is no longer mapped.
    Meanwhile ``GetProcessTimes`` still answers on that handle, so the walk's
    own "gone between the snapshot and the probe" guard used to need the
    creation time to be unreadable too and did not catch it, and the host was
    listed at random (the intermittent failure of
    ``test_the_workers_own_console_host_is_never_a_left_behind_process``).
    A process that has exited is by definition not left behind. The walk's
    guard now reads a positive ``"dead"`` on its own for every image,
    so this one no longer stands alone in closing that window;
    it still owns the *other* fact a missing path carries, which is ownership
    of a console host that is still running.

    Only a positive ``"dead"`` reading excludes it. ``"unknown"`` is a probe
    that could not answer, and a failure to observe is not an observation
    (:func:`pid_state`): it keeps the listing, like any other unproven case.
    """

    if image.casefold() != "conhost.exe":
        return False
    path = windows_process_image_path(pid)
    if path is None:
        return pid_state(pid) == "dead"
    system = _windows_system_directory()
    if system is None:
        return False
    return os.path.normcase(os.path.dirname(path)) == os.path.normcase(system)


def windows_process_image_path(pid: int) -> str | None:
    """The full image path of ``pid`` from ``QueryFullProcessImageNameW``, or None."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return buffer.value[: size.value] or None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 -- a probe must never raise
        return None


def _windows_system_directory() -> str | None:
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        length = ctypes.WinDLL("kernel32").GetSystemDirectoryW(buffer, 1024)
        if 0 < length < 1024:
            return buffer.value
    except Exception:  # noqa: BLE001
        pass
    root = os.environ.get("SystemRoot")
    return os.path.join(root, "System32") if root else None


# ---------------------------------------------------------------------------
# A census the chain cannot break
# ---------------------------------------------------------------------------
#
# `_list_descendants_toolhelp` walks the LIVE process table from the worker's
# pid. `list_descendants` explains why that works across the worker's own
# death: a child keeps its parent's pid after the parent dies. True -- and only
# one level deep. A GRANDchild whose intermediate parent has already exited has
# no surviving link back to the worker, so the walk never reaches it, and an
# empty result reads as "the worker left nothing running" when the truth is
# "the walk started from a root that no longer connects to what it left".
#
# That is not a corner: `run_with_tree_kill`'s own docstring names the shape --
# every provider CLI on Windows is a `.cmd` shim, `cmd.exe` starts `node.exe`
# and exits. From that moment the real worker is invisible to the walk. It was
# found because a virtual environment has the same shape (a venv's
# `Scripts\python.exe` launches the base interpreter as its own process), which
# is why it appeared on the self-hosted CI lane and on no local run.
#
# A Job Object does not have the problem, because membership is not a link:
# a process assigned to a job stays in it, every process it creates joins it,
# and an exit removes only the process that exited. Asking the job which pids
# are live is a census, and no intermediate death can break it.
#
# The same job is how a timeout stops the tree. The worker is
# created suspended and resumed only once it is a member, so every process it
# creates is a member too -- except what Windows creates on its behalf outside
# the job, such as an App Execution Alias target, which
# `_descendants_outside_job` finds by parent pid. On a timeout or a
# cancellation `_kill_process_tree` terminates the job, stops those by exact
# pid, and then reads this census to say whether the tree is gone.
#
# BY DEFAULT it has no `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, or other limits.
# The runner closes it on every path, and closing it stops nothing. A worker
# that exits on its own therefore leaves its descendants running, and the
# census reports them: counting must never become killing, and a
# kill-on-close job would make the normal-exit report describe a tree it had
# just destroyed. Stopping is an explicit act, taken on the timeout and
# cancellation paths only.
# The per-job runner alone opts into kill-on-close: its death owns member
# cleanup. It records descendants before closing, and cleanup separately.

_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3


def _create_census_job(*, kill_on_close: bool = False) -> "int | None":
    """An empty Job Object to hold the worker's tree, or None.

    None on every failure. The runner then does not start the worker at all:
    a job that cannot be made is a tree that could not be
    stopped, and there is no uncontained fallback.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if handle and kill_on_close:
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, native pointer alignment.
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

            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                        wintypes.LPVOID, wintypes.DWORD]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                _close_census_job(int(handle))
                return None
        return int(handle) if handle else None
    except Exception:  # noqa: BLE001 -- an unavailable census is not an error
        return None


def _assign_to_census_job(handle: int, pid: int) -> bool:
    """Put one process in the job. False if it could not be done.

    The runner calls this while the worker is still suspended,
    so the worker is a member before it has executed an instruction of its
    own and every child it creates inherits the membership. Opening the process
    by pid is safe there: the runner's ``Popen`` holds the process handle, so
    the pid cannot be recycled in the meantime.
    """

    if os.name != "nt" or not handle:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE are what the assignment needs.
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not process:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(wintypes.HANDLE(handle), process))
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(process))
    except Exception:  # noqa: BLE001
        return False


def _census_job_pids(handle: int) -> "list[int] | None":
    """Which pids are alive in the job right now, or None if it cannot be read."""

    if os.name != "nt" or not handle:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        capacity = 64
        for _ in range(8):
            class _IdList(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * capacity),
                ]

            buf = _IdList()
            returned = wintypes.DWORD(0)
            ok = kernel32.QueryInformationJobObject(
                wintypes.HANDLE(handle), _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(returned),
            )
            if ok:
                return [int(buf.ProcessIdList[i]) for i in range(int(buf.NumberOfProcessIdsInList))]
            # ERROR_MORE_DATA: the job holds more pids than the buffer fits.
            if ctypes.get_last_error() != 234:
                return None
            capacity *= 4
        return None
    except Exception:  # noqa: BLE001
        return None


def _close_census_job(handle: int) -> None:
    """Release the job handle. Nothing in it is stopped: no kill-on-close is set."""

    if os.name != "nt" or not handle:
        return
    try:
        import ctypes
        from ctypes import wintypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(handle))
    except Exception:  # noqa: BLE001
        pass


def _terminate_job(handle: int) -> bool:
    """Terminate every process in the job. False if the call failed.

    Exit code 1, the same one ``Popen.kill`` and :func:`terminate_pid` give.
    Whether anything is left is not this function's answer: the census is.
    """

    if os.name != "nt" or not handle:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        return bool(kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1))
    except Exception:  # noqa: BLE001
        return False


def _resume_suspended_process(pid: int) -> bool:
    """Resume the threads of a process created suspended. True if one was resumed.

    ``subprocess.Popen`` closes the primary thread's handle and keeps neither
    it nor the thread id, so the thread is found the one way that needs
    neither: a complete thread snapshot, filtered by owner pid. Each handle is
    checked to belong to ``pid`` before it is resumed, in case a thread id was
    recycled between the snapshot and the open. ``ResumeThread`` on a thread
    that was never suspended changes nothing, so resuming every thread of the
    process resumes exactly the suspended one.

    False when nothing was resumed, a thread could not be opened or resumed,
    or the snapshot did not finish: the caller then stops the worker instead
    of guessing.
    """

    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPTHREAD = 0x00000004
        THREAD_SUSPEND_RESUME = 0x0002
        THREAD_QUERY_LIMITED_INFORMATION = 0x0800
        ERROR_BAD_LENGTH = 24
        ERROR_NO_MORE_FILES = 18
        RESUME_FAILED = 0xFFFFFFFF
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
        kernel32.GetProcessIdOfThread.restype = wintypes.DWORD
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = None
        for _attempt in range(5):
            ctypes.set_last_error(0)
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
            if snapshot and snapshot != INVALID_HANDLE_VALUE:
                break
            snapshot = None
            if ctypes.get_last_error() != ERROR_BAD_LENGTH:
                break
        if snapshot is None:
            return False
        owned: list[int] = []
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            ctypes.set_last_error(0)
            more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if int(entry.th32OwnerProcessID) == pid:
                    owned.append(int(entry.th32ThreadID))
                ctypes.set_last_error(0)
                more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
                return False
        finally:
            kernel32.CloseHandle(snapshot)

        resumed = 0
        for thread_id in owned:
            thread = kernel32.OpenThread(
                THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION, False, thread_id
            )
            if not thread:
                return False
            try:
                if kernel32.GetProcessIdOfThread(thread) != pid:
                    continue
                previous = kernel32.ResumeThread(thread)
                if previous == RESUME_FAILED:
                    return False
                if previous > 0:
                    resumed += 1
            finally:
                kernel32.CloseHandle(thread)
        return resumed > 0
    except Exception:  # noqa: BLE001 -- a failed resume is reported, never raised
        return False


def _contain_suspended_worker(proc, *, kill_on_close: bool = False) -> int:
    """Put a worker created suspended into its job, then let it run. Returns the job.

    The order is the guarantee: the job exists, the worker is a
    member, and only then does its primary thread run. Every process it
    creates from its first instruction on joins the job, so there is no
    window in which a child can start outside it. (Windows itself can still
    start a process outside the job on its behalf; see
    :func:`_descendants_outside_job`.)

    Any failure stops the worker where it stands -- still suspended, or, after
    a resume that failed partway, inside its job -- and raises
    :class:`WorkerContainmentError`. There is no uncontained fallback: a
    worker whose tree could not be stopped is what this refuses to start.
    """

    job = None
    try:
        job = _create_census_job(kill_on_close=True) if kill_on_close else _create_census_job()
        if job is None:
            reason = "no Job Object could be created"
        elif not _assign_to_census_job(job, proc.pid):
            reason = "it could not be assigned to its Job Object"
        elif not _resume_suspended_process(proc.pid):
            reason = "it could not be resumed inside its Job Object"
        else:
            return job
    except BaseException:
        _abandon_unstarted_worker(proc, job)
        raise
    _abandon_unstarted_worker(proc, job)
    raise WorkerContainmentError(
        "the worker could not be contained, so it was stopped before it ran: " + reason
    )


def _abandon_unstarted_worker(proc, job: "int | None") -> None:
    """Stop a worker that was never let run, and release what it held. Never raises."""

    if job:
        _terminate_job(job)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5.0)
    except Exception:  # noqa: BLE001
        pass
    for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
    if job:
        _close_census_job(job)


def _toolhelp_walk_is_provable(created_after: "float | None") -> bool:
    """Could the snapshot walk have reached everything it should?

    It cannot prove it whenever the table holds an orphan -- an entry whose
    parent pid is not itself in the table -- that is new enough to belong to
    this worker. Such an entry is exactly the shape that defeats the walk:
    its link back was severed when its parent exited, so a walk
    from the worker's pid can neither reach it nor rule it out.

    An old orphan proves nothing either way and is ignored: every machine has
    services whose parents exited at boot, and treating those as doubt would
    mark every result unprovable and mean nothing.
    """

    entries = _windows_process_snapshot()
    if entries is None:
        return False
    live = {entry_pid for entry_pid, _, _ in entries}
    for entry_pid, parent_pid, _ in entries:
        if parent_pid in live or parent_pid == 0:
            continue
        if created_after is None:
            # Nothing to date it against, so it cannot be ruled out.
            return False
        created = process_creation_epoch(entry_pid)
        if created is None or created >= created_after:
            # New enough to be this worker's, or undatable. Either way the
            # walk's completeness is not established.
            return False
    return True


def _descendants_at_exit(
    pid: int,
    *,
    created_after: float | None,
    process_group: bool,
    census_job: int | None = None,
) -> dict:
    """What the worker left running, as facts a row can carry.

    ``complete`` answers a second question the caller used to have no way to
    ask: was this list everything? A Job Object census can say yes, because
    membership survives an intermediate's exit. A snapshot walk usually
    cannot, because it follows links that an exit destroys -- and
    an unprovable count must not read like a proven one.
    """

    import time

    facts: dict = {"observed_at": time.time(), "descendants": []}
    if os.name != "nt":
        # Not looked at, and said so. POSIX reparents orphans, so the only
        # complete handle on what a worker left would be a session of its
        # own -- which changes what Ctrl+C reaches and still lets a
        # daemonising child escape. The packaged product is Windows-only;
        # here the row keeps the descendant accounting with its stated limit.
        facts.update(
            {"enumerated": False, "reason": "unsupported_platform", "method": "none", "complete": False}
        )
        return facts

    if census_job:
        members = _census_job_pids(census_job)
        if members is not None:
            listed: list[dict] = []
            for member in members:
                if member == pid:
                    continue
                if pid_state(member) == "dead":
                    continue
                image = _image_name_for_pid(member)
                if _is_console_host_not_left_behind(member, image or ""):
                    continue
                if image is None and pid_state(member) == "dead":
                    # An unreadable image means the name gate in
                    # `_is_console_host_not_left_behind` never ran at all: it
                    # compares against the literal "conhost.exe", and the ""
                    # this passes it instead matches nothing, so the member
                    # fell straight through to be listed with `image: None`.
                    #
                    # The usual reason the name is missing is that the process
                    # left the process table between the census above and this
                    # probe -- the console host outliving its client by an
                    # instant, which is the very case that gate exists for. So
                    # liveness is read again before the miss is believed: a
                    # process that has exited is not left behind by anyone, and
                    # the toolhelp32 walk has read a positive "dead" on its own
                    # since an earlier fix. The walk's guard sits *after* its
                    # exclusion for exactly this reason; the census checked
                    # liveness only before, and so reported a failed read as
                    # the positive fact "the worker left something running"
                    # (the intermittent failure of
                    # `test_the_workers_own_console_host_is_never_a_left_behind_process`).
                    #
                    # Only a positive "dead" excludes. A member still alive
                    # whose image cannot be read stays listed with `image:
                    # None`: ownership is unproven, and unproven is listed
                    # rather than excluded, which is the same fail-closed rule
                    # `_is_console_host_not_left_behind` states for a missing
                    # image path.
                    continue
                listed.append(
                    {"pid": member, "image": image, "created": process_creation_epoch(member)}
                )
            # What Windows started outside the job on a member's behalf (an App
            # Execution Alias target) is no member, but it is still something
            # the worker left running. The roots are held while
            # the walk reads their numbers. What nothing held links to the
            # worker is not seen here either; the stop's docstring states
            # that limit.
            pins = _Pins()
            try:
                pins.pin(pid)
                for member in members:
                    pins.pin(member)
                outside = _descendants_outside_job(
                    dict(pins.created), set(members), created_after,
                    in_job=lambda child: _process_in_job(child, census_job),
                )
            finally:
                pins.close()
            for child, image, created in outside or ():
                if pid_state(child) == "dead" or _is_console_host_not_left_behind(child, image):
                    continue
                listed.append({"pid": child, "image": image or None, "created": created})
            facts.update(
                {
                    "enumerated": True,
                    "method": "job_object",
                    "descendants": listed,
                    "complete": outside is not None,
                }
            )
            if outside is None:
                # The job was counted; what it could not hold was not looked for.
                facts["incomplete_reason"] = "snapshot_failed"
            return facts

    walked = list_descendants(pid, created_after=created_after, process_group=process_group)
    facts["method"] = "toolhelp32"
    if walked is None:
        facts.update({"enumerated": False, "reason": "snapshot_failed", "complete": False})
        return facts
    facts.update({"enumerated": True, "descendants": walked})
    if _toolhelp_walk_is_provable(created_after):
        facts["complete"] = True
    else:
        # The one thing this must never do is let "I could not follow the
        # chain" render as "I followed it and it was empty".
        facts.update({"complete": False, "incomplete_reason": "ancestry_broken"})
    return facts


def _image_name_for_pid(pid: int) -> "str | None":
    """The executable name the process table reports for ``pid``, if any."""

    entries = _windows_process_snapshot()
    if entries is None:
        return None
    for entry_pid, _, image in entries:
        if entry_pid == pid:
            return image or None
    return None


def run_with_tree_kill(
    argv,
    *,
    input: str | bytes | None = None,
    capture_output: bool = False,
    timeout: float | None = None,
    on_spawn=None,
    stop_requested: Callable[[], bool] | None = None,
    kill_on_close: bool | None = None,
    **popen_kwargs,
) -> subprocess.CompletedProcess:
    """A ``subprocess.run`` drop-in whose timeout kills the whole process tree.

    ``subprocess.run`` handles a timeout by killing only the DIRECT child. On
    Windows every provider CLI is a ``.cmd`` shim (cmd.exe -> node.exe), so
    after a timed-out delegation the real worker survived the kill and kept
    mutating the workspace with nobody watching or recording it (observed
    live: a timed-out delegation had written 4 of 6 files after
    "Request timed out"). Killing the tree makes a timeout actually stop the
    worker, so ``timed_out`` in an ExecutionResult means the work stopped.

    On Windows the tree is held by a Job Object from the worker's first
    instruction: the worker is created suspended, assigned to the
    job, and only then resumed, so every process it creates is a member. A
    timeout -- or a cancellation once the worker runs -- terminates the job,
    which reaches what a walk along parent pids cannot: a grandchild whose
    parent already exited, and a child born while the stop runs. What
    Windows starts outside the job on the worker's behalf (an App Execution
    Alias target) is found by parent pid and stopped by exact pid. A worker
    that cannot be contained is stopped before it runs, and
    :class:`WorkerContainmentError` is raised instead.

    By default normal exit closes without stopping descendants.
    Opt-in kill_on_close holds a non-inherited worker Job Object in this
    process only, so this process's death stops its members. Windows-started
    processes outside that job are not covered by this guarantee. Normal
    exit snapshots descendants before close and separately reports cleanup.
    """

    if kill_on_close is None:
        kill_on_close = default_kill_on_close.get()
    popen_kwargs.pop("shell", None)  # these workers are never shell=True
    if capture_output:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    # Console suppression is a whole-codebase invariant (see the source scan
    # in test_procutil.py); applying it here means a caller cannot forget it.
    if "creationflags" not in popen_kwargs:
        popen_kwargs["creationflags"] = no_window_kwargs().get("creationflags", 0)
    contained = os.name == "nt"
    if contained:
        # Not one instruction of the worker's own runs before it is inside its
        # job: `_contain_suspended_worker` resumes it only after the assignment.
        popen_kwargs["creationflags"] = int(popen_kwargs["creationflags"] or 0) | _CREATE_SUSPENDED
    stdin = subprocess.PIPE if input is not None else popen_kwargs.pop("stdin", subprocess.DEVNULL)
    process_group = bool(popen_kwargs.get("start_new_session"))
    proc = subprocess.Popen(argv, stdin=stdin, **popen_kwargs)
    # One job both counts the worker's tree and, on a timeout or
    # a cancellation only, stops it. It is closed in the
    # `finally` below whatever happens, and closing it stops nothing.
    census_job = (
        (_contain_suspended_worker(proc, kill_on_close=True) if kill_on_close
         else _contain_suspended_worker(proc)) if contained else None
    )
    completed = None
    parent_created = None
    try:
        try:
            # Read while the handle is certainly the worker's: its creation
            # time is the floor below which nothing can be its descendant.
            parent_created = process_creation_epoch(proc.pid)
            if on_spawn is not None:
                # Report the worker's pid the moment it exists: delegation
                # attempt rows record it so a caller that later times out still
                # knows which OS process owns the work. A broken callback must
                # not kill the run.
                try:
                    on_spawn(proc.pid)
                except Exception:  # noqa: BLE001
                    pass
        except BaseException:
            # The worker is already running: a cancellation here (Ctrl+C while
            # `on_spawn` writes its row, say) stops the tree exactly as one
            # during the wait does, before the job handle is closed.
            _kill_process_tree(proc, census_job, parent_created)
            raise
        completed = _run_to_completion(
            proc, argv, input=input, timeout=timeout,
            parent_created=parent_created, process_group=process_group,
            census_job=census_job,
            stop_requested=stop_requested,
        )
        return completed
    finally:
        members = _census_job_pids(census_job) if census_job and kill_on_close else None
        _close_census_job(census_job) if census_job else None
        if completed is not None and census_job and kill_on_close:
            # The pre-close snapshot remains unchanged; close-time death is
            # a separate observation, never a claim that outside members died.
            deadline = time.monotonic() + _CLOSE_CLEANUP_SECONDS
            while members and any(pid_state(pid) != 'dead' for pid in members):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            dead = members is not None and all(pid_state(pid) == 'dead' for pid in members)
            descendants = completed.worker_descendants
            outside = any(item['pid'] not in (members or [])
                          for item in descendants.get('descendants', []))
            completed.worker_termination = {
                'pid': proc.pid, 'confirmed_dead': pid_state(proc.pid) == 'dead',
                'tree_kill': 'ok' if dead and descendants.get('complete') and not outside
                else 'outside_unknown',
                'close_cleanup_members': len(members) if members is not None else None,
                'close_cleanup_confirmed': dead,
            }


def start_contained(argv, **popen_kwargs) -> "tuple[subprocess.Popen, int | None]":
    """Start a long-lived helper inside a kill-on-close Job Object.

    For a child that speaks a protocol over pipes rather than running to
    completion (the Codex app-server). It is created suspended, assigned to
    its job, then resumed, exactly like a worker; this process's death stops
    the whole tree. Returns the process and its job (None off Windows). A
    child that cannot be contained is stopped and
    :class:`WorkerContainmentError` (an ``OSError``) is raised.
    """
    contained = os.name == "nt"
    if "creationflags" not in popen_kwargs:
        popen_kwargs["creationflags"] = no_window_kwargs().get("creationflags", 0)
    if contained:
        popen_kwargs["creationflags"] = int(popen_kwargs["creationflags"] or 0) | _CREATE_SUSPENDED
    proc = subprocess.Popen(argv, **popen_kwargs)
    job = _contain_suspended_worker(proc, kill_on_close=True) if contained else None
    return proc, job


def stop_contained(proc: subprocess.Popen, job: "int | None") -> None:
    """Stop a :func:`start_contained` child and everything it started. Never raises."""
    try:
        if job:
            _kill_process_tree(proc, job, None)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001 -- teardown is best effort and must not raise
        pass
    finally:
        if job:
            _close_census_job(job)
    try:
        proc.wait(timeout=5.0)
    except Exception:  # noqa: BLE001
        pass


def run_worker(argv, **kwargs) -> subprocess.CompletedProcess:
    """:func:`run_with_tree_kill` for a product worker: kill on close by default.

    A worker's Job Object is held with kill-on-close, so if this process dies
    (the MCP server is closed or killed) Windows stops the worker's tree
    instead of leaving it running unobserved. A caller may still pass
    ``kill_on_close=False`` explicitly.
    """
    kwargs.setdefault("kill_on_close", True)
    return run_with_tree_kill(argv, **kwargs)


class _StdinWriter:
    """Writes a worker's prompt to its stdin on a thread of its own.

    On Windows ``Popen.communicate`` writes all of ``input`` in the calling
    thread before it first looks at the deadline, so a worker that stops
    reading with more than a pipe buffer left unread held the call -- and its
    timeout -- until the worker exited on its own. Here the write blocks only
    this thread. It ends when the prompt is written, or when no process holds
    the read end any more (the stop broke the pipe). Errors are handled the
    way CPython's ``_stdin_write`` handles them: a broken pipe or ``EINVAL``
    means the reader is gone and is not an error; anything else is kept for
    the caller to raise.
    """

    def __init__(self, stream, data) -> None:
        self._stream = stream
        self._data = data
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="worker-stdin-writer", daemon=True)
        self._thread.start()

    @staticmethod
    def _ignore_closed_reader(action) -> None:
        try:
            action()
        except BrokenPipeError:
            pass
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise

    def _run(self) -> None:
        try:
            if self._data:
                self._ignore_closed_reader(lambda: self._stream.write(self._data))
        except BaseException as exc:  # noqa: BLE001 -- handed to the waiting call
            self.error = exc
        finally:
            # Always closed, even after an unexpected write error: closing
            # flushes and gives a reader EOF. The first error is the one kept.
            try:
                self._ignore_closed_reader(self._stream.close)
            except BaseException as exc:  # noqa: BLE001
                if self.error is None:
                    self.error = exc

    def join(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds; True when the writer has finished."""

        self._thread.join(timeout)
        return not self._thread.is_alive()


def _run_to_completion(
    proc, argv, *, input, timeout, parent_created, process_group, census_job,
    stop_requested: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess:
    """Wait for the worker, then say what it left running.

    Split out so `run_with_tree_kill` can close the census job in one
    `finally` on every path -- the timeout path raises, and a handle leaked
    there would be a handle leaked per timed-out delegation.
    """

    writer = None
    writer_error: BaseException | None = None
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        if input is not None and os.name == "nt" and getattr(proc, "stdin", None) is not None:
            # The deadline must hold while the prompt is written,
            # so the write moves to its own thread and `communicate` never sees
            # the stream: Windows' `_communicate` closes stdin even with no
            # input, and that close waits for a write blocked in another thread.
            # Inside the `try`, so a cancellation here still stops the tree.
            writer = _StdinWriter(proc.stdin, input)
            proc.stdin = None
            input = None
        if stop_requested is None:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        else:
            first = True
            pending_stop = False
            while True:
                wait = POLL_SECONDS
                if deadline is not None:
                    wait = max(0.0, min(wait, deadline - time.monotonic()))
                try:
                    stdout, stderr = proc.communicate(input=input if first else None, timeout=wait)
                    break
                except subprocess.TimeoutExpired as poll_error:
                    first = False
                    partial_stdout, partial_stderr = poll_error.output, poll_error.stderr
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if pending_stop:
                    raise WorkerStopRequested(
                        argv, stdout=partial_stdout, stderr=partial_stderr,
                    )
                try:
                    requested = bool(stop_requested())
                except Exception:  # a failed observation leaves the worker running
                    requested = False
                if proc.poll() is not None:
                    # Give natural completion the next bounded output read.
                    # An exited child can leave a descendant holding its pipes;
                    # if that read times out, the deadline or pending stop wins.
                    pending_stop = requested
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if requested:
                    raise WorkerStopRequested(
                        argv, stdout=partial_stdout, stderr=partial_stderr,
                    )
        if writer is not None:
            # Never past the deadline: a writer still blocked here is held by
            # a process other than the worker, which has already exited.
            wait = _STDIN_WRITER_JOIN_SECONDS
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - time.monotonic()))
            if writer.join(wait):
                writer_error = writer.error
    except (subprocess.TimeoutExpired, WorkerStopRequested) as interrupted:
        termination = _kill_process_tree(proc, census_job, parent_created)
        if writer is not None:
            # The stop broke the pipe, so the writer ends; bounded like the
            # drain below, for a reader the stop did not reach.
            writer.join(_POST_KILL_DRAIN_SECONDS)
        try:
            # Bounded: a descendant the tree stop did not reach still holds the
            # captured pipes open, and an unbounded drain would make a
            # six-second timeout wait for that descendant's whole lifetime.
            # The output is a nicety here; the facts below are not.
            stdout, stderr = proc.communicate(timeout=_POST_KILL_DRAIN_SECONDS)
        except subprocess.TimeoutExpired as drain_error:
            if isinstance(interrupted, WorkerStopRequested):
                # Keep only output the platform supplies. Windows reader threads
                # wait for EOF and TimeoutExpired carries no partial output, so
                # a drain timeout there can leave stdout/stderr unavailable.
                stdout = drain_error.output if drain_error.output is not None else interrupted.stdout
                stderr = drain_error.stderr if drain_error.stderr is not None else interrupted.stderr
            else:
                stdout, stderr = None, None
            termination["output_drain"] = "timed_out"
        # What the caller can record: which pid was killed, how the tree stop
        # went, and whether the direct child is confirmed gone now -- read,
        # not assumed. `confirmed_dead` is a positive
        # reading of the process table: a probe that cannot answer leaves it
        # False, and the row then says "unaccounted for". It travels on the
        # exception because the exception is the only thing this path returns.
        termination.update({"pid": proc.pid, "confirmed_dead": pid_state(proc.pid) == "dead"})
        if isinstance(interrupted, WorkerStopRequested):
            termination["stop_requested"] = True
            if getattr(proc, "text_mode", False):
                encoding = getattr(proc, "encoding", None) or "utf-8"
                errors = getattr(proc, "errors", None) or "replace"
                stdout = stdout.decode(encoding, errors) if isinstance(stdout, bytes) else stdout
                stderr = stderr.decode(encoding, errors) if isinstance(stderr, bytes) else stderr
            error = WorkerStopRequested(argv, stdout=stdout, stderr=stderr)
        else:
            error = subprocess.TimeoutExpired(argv, timeout or 0, output=stdout, stderr=stderr)
        error.worker_termination = termination  # type: ignore[attr-defined]
        raise error
    except BaseException:
        # Cancelled while waiting: KeyboardInterrupt, SystemExit, anything
        # raised into this frame. `subprocess.run` kills the direct child
        # here; the worker's tree must not outlive the call that owned it
        # either, so the timeout's stop runs. There is no drain:
        # nobody will read the output, and the stop itself is bounded.
        _kill_process_tree(proc, census_job, parent_created)
        raise
    # Collect before propagating a writer error too: the worker exited on its
    # own, so its surviving descendants must be recorded without stopping them.
    descendants = _descendants_at_exit(
        proc.pid, created_after=parent_created, process_group=process_group,
        census_job=census_job,
    )
    if writer_error is not None:
        # Unexpected: neither a finished prompt nor a reader that went away.
        # Raised as `communicate` would have raised it, but outside the
        # cancellation branch: the worker exited on its own, and what it left
        # running is not stopped.
        writer_error.worker_descendants = descendants  # type: ignore[attr-defined]
        raise writer_error
    completed = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    # The worker exited on its own. What it left running is enumerated NOW,
    # while a child still names it as parent (Windows) or its group (POSIX),
    # and travels on the result the way termination facts travel on the
    # timeout -- nothing here stops or signals any of it.
    completed.worker_descendants = descendants  # type: ignore[attr-defined]
    return completed
