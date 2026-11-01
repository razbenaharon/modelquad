# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Windows ownership of an established loopback connection, never of a port.

Only limited process queries and TOKEN_QUERY are used. A retained handle binds
creation time, image and SID to the process actually owning the four-tuple.
Unknown inspection refuses; there is deliberately no PID-file fallback.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import os
import math
from pathlib import Path
import socket
import struct
import sys

RESTART_MESSAGE = (
    "The running ModelQuad service cannot authenticate this connection. "
    "Restart it from the installation that started it, then reopen ModelQuad."
)
DETAILS = {
    "service_absent": "The ModelQuad service is not listening.",
    "service_unreachable": "The ModelQuad service connection could not be established.",
    "service_owner_mismatch": "The connection belongs to another Windows account.",
    "service_image_mismatch": "The connection belongs to a different service image.",
    "service_owner_unavailable": "The connection owner could not be inspected.",
    "service_owner_unsupported": "Connection ownership verification requires Windows.",
    "service_auth_unsupported": RESTART_MESSAGE,
    "service_proof_invalid": "The service connection could not be authenticated.",
    "service_connection_changed": "The authenticated service connection is no longer available.",
    "service_instance_changed": "The service restarted. Close and reopen ModelQuad.",
    "service_identity_mismatch": "The service serves a different ModelQuad installation or database.",
    "service_secret_unavailable": "The installation secret could not be verified.",
    "service_redirect_refused": "The service redirected the request; the connection was refused.",
    "request_too_large": "This request is too large. Choose a smaller input.",
    "response_too_large": "This response is too large to display.",
    "foreign_listener": "The listener is not the ModelQuad control plane.",
    "wrong_role": "The listener has a different service role.",
    "development_instance": "The listener is a ModelQuad development instance.",
    "state_root": "The listener serves a different ModelQuad state store.",
    "unidentified": "The listener does not identify its ModelQuad state store.",
    "stale_version": "The listener runs a different ModelQuad version.",
    "schema": "The listener uses a different ModelQuad data format.",
    "protocol": "The listener does not speak the ModelQuad protocol.",
    "service_contained": "The service cannot start detached job runners.",
}


class ListenerError(Exception):
    """Closed labels and fixed prose; never carries a response or exception body."""

    def __init__(self, code: str, *, sent: bool = False) -> None:
        self.code = code if code in DETAILS else "service_proof_invalid"
        self.detail = DETAILS[self.code]
        self.sent = sent
        super().__init__(self.code)

    def body(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def endpoint_of(sock: socket.socket) -> list:
    """The proof's order: client address/port, server address/port."""
    try:
        local, remote = sock.getsockname(), sock.getpeername()
        endpoint = [local[0], local[1], remote[0], remote[1]]
        if local[0] != "127.0.0.1" or remote[0] != "127.0.0.1":
            raise ValueError
        return endpoint
    except (OSError, ValueError):
        raise ListenerError("service_connection_changed") from None


def admitted_service_images(port: int) -> frozenset[str]:
    """Admission uses this installation's two actual launch configurations.

    An untrusted pidfile may suggest a port, but never an admitted image.
    Development Python and its windowless sibling are admitted only when the
    launch resolver admits their full paths; the proof still binds the store.
    """
    from ..cli import _service_autostart_command, _service_launch_command
    from ..executable_resolution import UnsafeExecutable, admit_executable

    images = set()
    commands = []
    for builder in (_service_launch_command, _service_autostart_command):
        try:
            commands.append(builder(port))
        except (UnsafeExecutable, ValueError, OSError):
            # Missing windowless controller must refuse registration without
            # withdrawing admission of this installation's normal child.
            continue
    for command in commands:
        admitted = admit_executable(command[0], allow_batch=False)
        if admitted.path is not None:
            images.add(_normal_image(str(admitted.path)))
            # A pythonw host resolves both helpers to pythonw. The console
            # service may nevertheless have been started by this installation's
            # sibling python.exe (and conversely). Vet that exact sibling with
            # the same resolver; no PATH search or name-only admission.
            executable = Path(command[0])
            if (sys.platform == "win32"
                    and executable.name.lower() in {"python.exe", "pythonw.exe"}):
                sibling = executable.with_name(
                    "python.exe" if executable.name.lower() == "pythonw.exe" else "pythonw.exe"
                )
                sibling_admitted = admit_executable(sibling, allow_batch=False)
                if sibling_admitted.path is not None:
                    images.add(_normal_image(str(sibling_admitted.path)))
    if not images:
        raise ListenerError("service_image_mismatch")
    return frozenset(images)


def _normal_image(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=True)))


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "state", "local_address", "local_port", "remote_address", "remote_port", "pid",
    )]


def _windows():
    if sys.platform != "win32":
        raise ListenerError("service_owner_unsupported")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    ip.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    ip.GetExtendedTcpTable.restype = wintypes.DWORD
    return kernel, adv, ip


def _owner_pid(endpoint: list, *, server: bool) -> int:
    """Match exactly one ESTABLISHED IPv4 owner row, on the requested side."""
    _kernel, _adv, ip = _windows()
    size = wintypes.DWORD()
    status = ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
    if status not in (0, 122):
        raise ListenerError("service_owner_unavailable")
    for _ in range(8):
        if not 4 <= size.value <= 4 * 1024 * 1024:
            break
        buffer = ctypes.create_string_buffer(size.value)
        status = ip.GetExtendedTcpTable(buffer, ctypes.byref(size), False, 2, 5, 0)
        if status == 122:
            continue
        if status != 0:
            break
        count = ctypes.c_uint32.from_buffer(buffer).value
        if count > (len(buffer) - 4) // ctypes.sizeof(_TcpRow):
            break
        expected = endpoint[2:] + endpoint[:2] if server else endpoint
        matches = []
        for index in range(count):
            row = _TcpRow.from_buffer(buffer, 4 + index * ctypes.sizeof(_TcpRow))
            actual = [
                socket.inet_ntoa(struct.pack("=I", row.local_address)),
                socket.ntohs(row.local_port & 0xffff),
                socket.inet_ntoa(struct.pack("=I", row.remote_address)),
                socket.ntohs(row.remote_port & 0xffff),
            ]
            if row.state == 5 and actual == expected:
                matches.append(row.pid)
        if len(matches) == 1 and matches[0] > 0:
            return matches[0]
        # No row, or several rows, is unknown, never a listening-port fallback.
        break
    raise ListenerError("service_owner_unavailable")


def loopback_port_listening(port: int) -> bool | None:
    """Whether any IPv4 socket LISTENs on ``port`` (loopback or any address).

    Windows answers a connect to a closed loopback port with a reset only after
    its SYN retries (about one second), so a short connect timeout cannot tell
    "nothing listens" from "something listens but is stuck". The OS table can.
    None when the table is unavailable, or when it has a row on the port whose
    state it leaves blank (0, not a MIB_TCP_STATE; seen for listening sockets
    on Windows 11 build 26300); callers then keep the cautious answer.
    """
    try:
        _kernel, _adv, ip = _windows()
    except Exception:
        return None
    size = wintypes.DWORD()
    status = ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
    if status not in (0, 122):
        return None
    for _ in range(8):
        if not 4 <= size.value <= 4 * 1024 * 1024:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        status = ip.GetExtendedTcpTable(buffer, ctypes.byref(size), False, 2, 5, 0)
        if status == 122:
            continue
        if status != 0:
            return None
        count = ctypes.c_uint32.from_buffer(buffer).value
        if count > (len(buffer) - 4) // ctypes.sizeof(_TcpRow):
            return None
        blank = False
        for index in range(count):
            row = _TcpRow.from_buffer(buffer, 4 + index * ctypes.sizeof(_TcpRow))
            address = socket.inet_ntoa(struct.pack("=I", row.local_address))
            if socket.ntohs(row.local_port & 0xffff) == port and address in ("127.0.0.1", "0.0.0.0"):
                if row.state == 2:
                    return True
                blank = blank or row.state == 0
        return None if blank else False
    return None


def _facts(handle) -> tuple[int, str, str]:
    kernel, adv, _ip = _windows()
    exit_code = wintypes.DWORD()
    if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
        raise ListenerError("service_connection_changed")
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ListenerError("service_owner_unavailable")
    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    if not created:
        raise ListenerError("service_owner_unavailable")
    length = wintypes.DWORD(32768)
    image = ctypes.create_unicode_buffer(length.value)
    if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
        raise ListenerError("service_owner_unavailable")
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
        raise ListenerError("service_owner_unavailable")
    try:
        size = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise ListenerError("service_owner_unavailable")
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ListenerError("service_owner_unavailable")
        from .auth import _sid_string
        sid = _sid_string(ctypes.c_void_p.from_buffer(buffer))
        if not sid:
            raise ListenerError("service_owner_unavailable")
        return created, _normal_image(image.value), sid
    finally:
        kernel.CloseHandle(token)


@dataclass(repr=False)
class SocketOwner:
    _handle: object = field(repr=False)
    _pid: int
    _endpoint: list = field(repr=False)
    _server: bool
    _identity: tuple = field(repr=False)

    def verify_expected_process(self, *, pid: int | None = None,
                                created: float | None = None,
                                image: str | None = None) -> None:
        """Bind a managed caller to facts read through this retained handle.

        Windows times are 100 ns ticks since 1601, while lifecycle records
        use seconds since 1970. No new handle or PID-only query is used.
        """
        try:
            if self._handle is None:
                raise ListenerError("service_owner_unavailable")
            facts = _facts(self._handle)
            if facts != self._identity:
                raise ListenerError("service_connection_changed")
            epoch = (facts[0] - 116444736000000000) / 10000000
            if ((pid is not None and (type(pid) is not int or self._pid != pid))
                    or (created is not None and
                        (type(created) not in (int, float) or not math.isfinite(created)
                         or abs(epoch - created) > 0.001))
                    or (image is not None and facts[1] != _normal_image(image))):
                raise ListenerError("service_identity_mismatch")
        except ListenerError:
            raise
        except Exception:
            raise ListenerError("service_owner_unavailable") from None

    def recheck(self, sock: socket.socket, *, accepted: bool = False) -> None:
        try:
            endpoint = endpoint_of(sock)
            if accepted:
                endpoint = endpoint[2:] + endpoint[:2]
            if (self._handle is None or endpoint != self._endpoint
                    or _owner_pid(endpoint, server=self._server) != self._pid
                    or _facts(self._handle) != self._identity):
                raise ListenerError("service_connection_changed")
        except Exception:
            raise ListenerError("service_connection_changed") from None

    def close(self) -> None:
        if self._handle is not None:
            _windows()[0].CloseHandle(self._handle)
            self._handle = None


def verify_socket_owner(sock: socket.socket, images: frozenset[str] | None = None,
                        *, accepted: bool = False) -> SocketOwner:
    """For clients verify server; for gateway accepts verify renderer peer SID."""
    handle = None
    try:
        kernel, _adv, _ip = _windows()
        endpoint = endpoint_of(sock)
        if accepted:
            endpoint = endpoint[2:] + endpoint[:2]
        pid = _owner_pid(endpoint, server=not accepted)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            raise ListenerError("service_owner_unavailable")
        facts = _facts(handle)
        from .auth import current_user_sid
        if facts[2] != current_user_sid():
            raise ListenerError("service_owner_mismatch")
        if not accepted and (not images or facts[1] not in images):
            raise ListenerError("service_image_mismatch")
        owner = SocketOwner(handle, pid, endpoint, not accepted, facts)
        owner.recheck(sock, accepted=accepted)
        return owner
    except Exception as exc:
        if handle:
            _windows()[0].CloseHandle(handle)
        if isinstance(exc, ListenerError):
            raise
        raise ListenerError("service_owner_unavailable") from None
