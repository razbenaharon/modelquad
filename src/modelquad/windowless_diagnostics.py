# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Content-free, best-effort diagnostics for processes without a console.

Streams are discard sinks, never log writers. Only ``emit`` can persist, and
it accepts a closed vocabulary. Importing this module does not select state,
open a file, import a service, or configure the application's logging graph.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import stat
import sys
from threading import Lock

MAX_LOG_BYTES = 1024 * 1024
MAX_PROCESS_BYTES = 8 * MAX_LOG_BYTES
MAX_PROCESS_EVENTS = 65536
ROLES = frozenset({"service", "cli"})
EVENTS = frozenset({
    "entry", "exit", "startup_failed", "service_start_failed",
    "service_start_contained", "backend_failed",
})
STATUSES = frozenset({"acquired", "duplicate", "unknown", "completed", "failed",
                      "job_shared", "containment_unknown"})
PROTOCOL_COMMANDS = frozenset({"hook-pending", "hook-review", "usage-hook", "hook-gate", "mcp"})


def _length(value) -> int:
    try:
        return len(value)
    except Exception:
        return 0


class NullBinary:
    closed = False

    def write(self, value) -> int:
        return _length(value)

    def writelines(self, values) -> None:
        # Do not consume an unbounded or faulty iterator just to discard it.
        pass

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return -1

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class NullText(NullBinary):
    encoding = "utf-8"
    errors = "replace"
    newlines = None
    line_buffering = False
    write_through = True

    def __init__(self) -> None:
        self.buffer = NullBinary()

    def reconfigure(self, **kwargs) -> None:
        pass


class ProtocolBinary(NullBinary):
    """Forward live protocol bytes; a broken pipe/handle drops output silently."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, value) -> int:
        try:
            written = self._stream.write(value)
            return written if type(written) is int else _length(value)
        except Exception:
            return _length(value)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        try:
            return self._stream.fileno()
        except Exception:
            return -1

    def writelines(self, values) -> None:
        try:
            for value in values:
                self.write(value)
        except Exception:
            pass


class ProtocolText(ProtocolBinary):
    encoding = "utf-8"
    errors = "replace"
    newlines = None
    line_buffering = False
    write_through = True

    def __init__(self, stream) -> None:
        super().__init__(stream)
        try:
            binary = stream.buffer
        except Exception:
            binary = None
        self.buffer = ProtocolBinary(binary)

    def reconfigure(self, **kwargs) -> None:
        # Do not change a live protocol's encoding or newline policy.
        pass

    def getvalue(self):
        # Retain StringIO's inspection surface for embedded callers/fixtures.
        return self._stream.getvalue()


def _invalid_stream(stream) -> bool:
    if stream is None:
        return True
    try:
        if stream.closed:
            return True
        try:
            fd = stream.fileno()
        except (AttributeError, io.UnsupportedOperation):
            # StringIO and other intentional in-memory protocol destinations.
            return not callable(getattr(stream, "write", None))
        os.fstat(fd)
        return False
    except Exception:
        return True


_active = False
_sink = None
_budgets = {}


class _Budget:
    def __init__(self) -> None:
        self.lock = Lock()
        self.bytes = 0
        self.events = 0


def is_active() -> bool:
    return _active


def clear_sink() -> None:
    global _sink
    _sink = None


def bootstrap(*, force: bool = False, protocol_stdout: bool = False) -> bool:
    """Install safe streams *before* optional product imports; no files yet.

    A live console stays a console. A windowless process drops all arbitrary
    diagnostics, including third-party tracebacks, regardless of whether a
    launcher supplied a writable file handle. Protocol stdout is separate.
    """
    global _active
    try:
        windowless = Path(sys.executable or "").name.lower() == "pythonw.exe"
        enabled = (force or windowless or protocol_stdout
                   or _invalid_stream(sys.stdout) or _invalid_stream(sys.stderr))
        if enabled:
            sys.stderr = NullText()
            if not protocol_stdout:
                sys.stdout = NullText()
            _active = True
        if protocol_stdout and not isinstance(sys.stdout, ProtocolText):
            sys.stdout = ProtocolText(sys.stdout)
        return enabled
    except Exception:
        return False


def select_state(argv) -> bool:
    """Declare existing explicit state flags before any product default import.

    This is not an argument parser. Invalid/duplicate declarations leave the
    null sink in place; the real CLI retains responsibility for rejecting them.
    No diagnostic file is created for an invalid declaration.
    """
    try:
        selected = {}
        if any(arg.startswith("--state-") and arg.split("=", 1)[0] not in
               {"--state-root", "--state-mode"} for arg in argv):
            return False  # An abbreviation must not select a default log first.
        for flag, variable in (("--state-root", "MODELQUAD_STATE_ROOT"),
                               ("--state-mode", "MODELQUAD_STATE_MODE")):
            values = []
            for index, arg in enumerate(argv):
                if arg == "--":
                    break
                if arg == flag:
                    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                        return False
                    values.append(argv[index + 1])
                elif arg.startswith(flag + "="):
                    values.append(arg[len(flag) + 1:])
            if len(values) > 1:
                return False
            if values:
                value = values[0]
                if flag == "--state-root" and (not value or not Path(value).is_absolute()):
                    return False
                if flag == "--state-mode" and value not in {"installed", "development", "test"}:
                    return False
                selected[variable] = value
        os.environ.update(selected)
        return True
    except Exception:
        return False


def command_for(argv) -> str | None:
    """Identify a command without confusing a global option's value for one."""
    values = {"--config", "--usage-directory", "--history-db"}
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in values:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def role_for(argv) -> str:
    command = command_for(argv)
    return "service" if command in {"serve", "service"} else "cli"


def protocol_stdout_for(argv) -> bool:
    command = command_for(argv)
    return command in PROTOCOL_COMMANDS


def bind_selected_state(role: str) -> bool:
    """Called only once the entry path has established its state selection."""
    global _sink
    if not _active or type(role) is not str or role not in ROLES:
        return False
    try:
        from .paths import logs_dir
        sink = EventSink(logs_dir(), role)
        if not sink.prepare():
            _sink = None
            return False
        _sink = sink
        return True
    except Exception:
        _sink = None
        return False


def emit(event: str, status: str | None = None) -> None:
    """Never persist arbitrary text, even when supplied by an audited caller."""
    try:
        if type(event) is not str or event not in EVENTS:
            return
        if status is not None and (type(status) is not str or status not in STATUSES):
            return
        if _sink is not None:
            _sink.emit(event, status)
    except Exception:
        pass


def _reject_link(info, *, directory: bool = False) -> None:
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
            or (directory and not stat.S_ISDIR(info.st_mode))):
        raise OSError("unsafe diagnostic destination")


def _windows_file(path: Path, *, directory: bool = False) -> int:
    """Open the object itself, deny delete sharing, and reject reparse/hardlinks."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class Information(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # OPEN_ALWAYS for fixed files; OPEN_EXISTING for directories. Holding the
    # directory chain without FILE_SHARE_DELETE prevents ancestor replacement.
    handle = kernel.CreateFileW(str(path), 0x80 if directory else 0xC0000000,
                                3, None, 3 if directory else 4,
                                0x00200000 | (0x02000000 if directory else 0), None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError("diagnostic open failed")
    try:
        info = Information()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError("diagnostic inspection failed")
        if (info.attributes & 0x400 or bool(info.attributes & 0x10) != directory
                or (not directory and info.links != 1)):
            raise OSError("unsafe diagnostic destination")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if directory else os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return fd  # CRT now owns the handle.


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Pin every ancestor; never traverse a link, junction or redirected root."""
    if (not path.is_absolute() or ".." in path.parts or len(path.parts) > 64
            or str(path).startswith(("\\\\", "//"))):
        raise OSError("diagnostic directory must be absolute")
    descriptors = []
    try:
        current = Path(path.anchor)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
            kernel.GetDriveTypeW.restype = wintypes.UINT
            if kernel.GetDriveTypeW(str(current)) != 3:  # DRIVE_FIXED; no redirected/network log.
                raise OSError("diagnostic volume unavailable")
            fd = _windows_file(current, directory=True)
        else:
            fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in path.parts[1:]:
            current /= part
            if create:
                try:
                    if os.name == "nt":
                        current.mkdir(mode=0o700)
                    else:
                        os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            if os.name == "nt":
                fd = _windows_file(current, directory=True)
            else:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        yield fd
    finally:
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except Exception:
                pass


def _open_file(directory: Path, dir_fd: int, name: str) -> int:
    if os.name == "nt":
        fd = _windows_file(directory / name)
    else:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=dir_fd)
    try:
        _reject_link(os.fstat(fd))
        if os.name == "nt":
            import msvcrt
            from .service.auth import describe_handle, current_user_sid
            described = describe_handle(msvcrt.get_osfhandle(fd))
            allowed = {current_user_sid(), "S-1-5-18"}
            if described.aces is None or any(kind != 0 or sid not in allowed
                                             for kind, flags, sid in described.aces):
                raise OSError("diagnostic file is not private")
        else:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _protect_logs(directory: Path, dir_fd: int) -> None:
    # ACL inheritance must not touch an existing redirected/link destination.
    # The bounded inspection also refuses nested directories in this flat log
    # namespace. Content is never read. POSIX chmod uses the pinned descriptor.
    with os.scandir(directory if os.name == "nt" else dir_fd) as entries:
        for index, entry in enumerate(entries):
            if index >= 64:
                raise OSError("diagnostic directory inspection limit")
            # Windows DirEntry.stat uses cached enumeration data, whose link
            # count is zero. A fresh no-follow stat queries the actual object;
            # zero/unknown counts and multiple links still fail closed.
            info = (os.stat(entry.path, follow_symlinks=False) if os.name == "nt"
                    else entry.stat(follow_symlinks=False))
            _reject_link(info)
    if os.name == "nt":
        from .service.auth import protect_directory
        protect_directory(directory)
    else:
        os.fchmod(dir_fd, 0o700)


@contextmanager
def _file_lock(fd: int):
    """Single nonblocking kernel-lock attempt; no polling, stale lock or wait."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


class EventSink:
    def __init__(self, directory: Path, role: str) -> None:
        if type(role) is not str or role not in ROLES:
            raise ValueError("unknown diagnostic role")
        self.directory = Path(directory)
        self.name = role + "-diagnostics.log"
        self._budget = _budgets.setdefault(role, _Budget())
        self._lock = self._budget.lock

    @property
    def _bytes(self) -> int:
        return self._budget.bytes

    def prepare(self) -> bool:
        try:
            with _directory(self.directory, create=True) as directory_fd:
                _protect_logs(self.directory, directory_fd)
            return True
        except Exception:
            return False

    def emit(self, event: str, status: str | None = None) -> bool:
        try:
            if type(event) is not str or event not in EVENTS:
                return False
            if status is not None and (type(status) is not str or status not in STATUSES):
                return False
            if not self._lock.acquire(blocking=False):
                return False
            try:
                if self._budget.events >= MAX_PROCESS_EVENTS or self._bytes >= MAX_PROCESS_BYTES:
                    return False
                line = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        + " " + event + (" " + status if status else "") + "\n").encode("ascii")
                if self._bytes + len(line) > MAX_PROCESS_BYTES:
                    return False
                # Charge attempted writes too: repeated disk errors cannot
                # produce unbounded lifetime diagnostic work.
                self._budget.events += 1
                self._budget.bytes += len(line)
                return self._append(line)
            finally:
                self._lock.release()
        except Exception:
            return False

    def _append(self, line: bytes) -> bool:
        with _directory(self.directory) as directory_fd:
            # A replacement directory must earn the same protection as the
            # original. The pinned chain makes this path-based ACL call safe.
            _protect_logs(self.directory, directory_fd)
            lock_fd = _open_file(self.directory, directory_fd, self.name + ".lock")
            try:
                with _file_lock(lock_fd):
                    # Reject a linked prior generation even before rotation.
                    prior = self.name + ".1"
                    try:
                        info = (os.lstat(self.directory / prior) if os.name == "nt"
                                else os.stat(prior, dir_fd=directory_fd, follow_symlinks=False))
                    except FileNotFoundError:
                        pass
                    else:
                        _reject_link(info)
                        if info.st_size > MAX_LOG_BYTES:
                            prior_fd = _open_file(self.directory, directory_fd, prior)
                            try:
                                os.ftruncate(prior_fd, MAX_LOG_BYTES)
                            finally:
                                os.close(prior_fd)
                    fd = _open_file(self.directory, directory_fd, self.name)
                    try:
                        size = os.fstat(fd).st_size
                        if size > MAX_LOG_BYTES:
                            os.ftruncate(fd, MAX_LOG_BYTES)
                            size = MAX_LOG_BYTES
                    finally:
                        os.close(fd)
                    if size + len(line) > MAX_LOG_BYTES:
                        # Close before Windows rename; failure drops the event,
                        # never appends past the cap or falls back to .1.
                        if os.name == "nt":
                            os.replace(self.directory / self.name, self.directory / prior)
                        else:
                            os.replace(self.name, prior, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    fd = _open_file(self.directory, directory_fd, self.name)
                    try:
                        if os.fstat(fd).st_size + len(line) > MAX_LOG_BYTES:
                            return False
                        size = os.lseek(fd, 0, os.SEEK_END)
                        if os.write(fd, line) != len(line):
                            os.ftruncate(fd, size)
                            return False
                        return True
                    finally:
                        os.close(fd)
            finally:
                os.close(lock_fd)
