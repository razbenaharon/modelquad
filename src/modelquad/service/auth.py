# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The per-installation secret that authorizes changes through the local service.

The service listens on loopback and runs as the user's own account. Loopback is
reachable by every process on the computer, including processes of *other*
Windows accounts, so before this module any of them could change routing,
privacy or provider settings, or trigger a repair, by sending a request with no
``Origin`` header. The browser-facing checks in ``service/trust.py`` stop web
pages; they cannot tell one local process from another.

This module owns the secret those processes cannot read:

* ``<state_root>/service-secret`` holds ``secrets.token_urlsafe(32)``. It is
  created exclusively -- never overwritten -- and on Windows is created *with*
  a protected DACL granting only the current user and SYSTEM (``ctypes`` over
  ``advapi32``/``kernel32``; no ``pywin32``), so there is no moment at which it
  exists with the inherited ACL of its directory. On POSIX it is created with
  ``O_EXCL`` and mode 0600.
* Every service start reads it back and verifies it from the same open handle:
  the owner is the current user and nobody else is granted access. A file that
  fails verification is **not** repaired or replaced -- someone else may have
  planted it -- and the service instead refuses every change and says why.

What this does not protect against, stated once so no surface overstates it:
any process running as the *same* user can read this file, write the database
directly, or run the CLI. ModelQuad does not claim protection against hostile
software already running under the user's own account (``SECURITY.md``).

The secret's value is never logged, never returned by an API, and never put in
a ``repr``: :class:`ServiceSecret` hides it.
"""

from __future__ import annotations

import ctypes as _ctypes
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: The file name under the state root. Part of the contract with the desktop
#: shell, which reads it to authorize changes from the window.
SECRET_FILE_NAME = "service-secret"

#: The request header that carries the secret.
TOKEN_HEADER = "X-ModelQuad-Token"

#: What ``secrets.token_urlsafe(32)`` produces, and the least a file must hold
#: to be taken as a secret at all.
_SECRET_SHAPE = re.compile(r"[A-Za-z0-9_-]{32,256}")

#: How long a reader waits for a concurrent creator to finish writing.
_EMPTY_READ_RETRIES = 20
_EMPTY_READ_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class ServiceSecret:
    """The secret this service accepts, or why it has none.

    Exactly one of ``value`` and ``problem`` is set. ``value`` is excluded from
    ``repr`` so a stray log line or traceback cannot print it.
    """

    value: str | None = field(default=None, repr=False)
    problem: str | None = None

    @property
    def usable(self) -> bool:
        return self.value is not None


class SecretFileError(Exception):
    """The secret file exists but cannot be trusted or read."""


def secret_path(state_root: str | os.PathLike[str]) -> Path:
    return Path(state_root) / SECRET_FILE_NAME


def ensure_service_secret(state_root: str | os.PathLike[str]) -> ServiceSecret:
    """Create the secret if it is absent, then read and verify it.

    Never raises: a failure is returned as ``ServiceSecret(problem=...)``, which
    makes the service start and refuse every change rather than not start.
    """

    path = secret_path(state_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _create_exclusively(path, secrets.token_urlsafe(32))
        except FileExistsError:
            pass  # created earlier, or by a concurrent start; verified below
        return ServiceSecret(value=_read_verified(path))
    except SecretFileError as exc:
        return ServiceSecret(problem=f"{exc} Delete {path} and restart the ModelQuad service to create a new one.")
    except OSError as exc:
        return ServiceSecret(problem=f"The service secret at {path} could not be created or read ({type(exc).__name__}).")


def read_service_secret(state_root: str | os.PathLike[str], *, retry_empty: bool = True) -> str | None:
    """The verified secret, or None. Never creates the file.

    For a client of the local service: it hands over what the service already
    created, and hands over nothing when the file is missing or fails the same
    verification the service applies. Bounded management reads use
    retry_empty=False: one read through that same verified handle, with an
    empty file treated as unavailable.
    """

    try:
        return (_read_verified(secret_path(state_root)) if retry_empty else
                _read_verified(secret_path(state_root), retry_empty=False))
    except (SecretFileError, OSError):
        return None


def _read_verified(path: Path, *, retry_empty: bool = True) -> str:
    for _ in range(_EMPTY_READ_RETRIES if retry_empty else 1):
        raw = _read_windows(path) if sys.platform == "win32" else _read_posix(path)
        text = raw.decode("ascii", errors="replace").strip()
        if text or not retry_empty:
            break
        # A concurrent start created the file and has not written it yet.
        time.sleep(_EMPTY_READ_INTERVAL_SECONDS)
    if not _SECRET_SHAPE.fullmatch(text):
        raise SecretFileError("The service secret file is empty or malformed.")
    return text


# --------------------------------------------------------------------------
# POSIX
# --------------------------------------------------------------------------


def _create_posix(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, value.encode("ascii"))
    finally:
        os.close(fd)


def _read_posix(path: Path) -> bytes:
    import stat

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SecretFileError(f"The service secret file could not be opened safely ({type(exc).__name__}).") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretFileError("The service secret is not a regular file.")
        if info.st_uid != os.geteuid():
            raise SecretFileError("The service secret file is owned by another account.")
        if info.st_mode & 0o077:
            raise SecretFileError("The service secret file is readable or writable by other accounts.")
        return os.read(fd, 4096)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

_SYSTEM_SID = "S-1-5-18"


def _create_windows(path: Path, value: str) -> None:
    import ctypes
    import msvcrt

    k32, _adv = _win()
    user = current_user_sid()
    # Owner: the user. DACL: Protected (inherits nothing), full access for the
    # user and for SYSTEM, and nothing else. Applied by CreateFileW itself.
    sd = _security_descriptor(f"O:{user}D:P(A;;FA;;;{user})(A;;FA;;;SY)")
    try:
        attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), sd, False)
        handle = k32.CreateFileW(
            str(path),
            0x40000000,  # GENERIC_WRITE
            0,  # no sharing while it is being written
            ctypes.byref(attributes),
            1,  # CREATE_NEW: fails if the file exists
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle in (None, _INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            if error in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
                raise FileExistsError(str(path))
            raise OSError(error, "CreateFileW failed", str(path))
    finally:
        k32.LocalFree(sd)
    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value.encode("ascii"))


def _read_windows(path: Path) -> bytes:
    import ctypes
    import msvcrt

    k32, _adv = _win()
    handle = k32.CreateFileW(
        str(path),
        0x80000000 | 0x00020000,  # GENERIC_READ | READ_CONTROL
        0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE: a concurrent creator
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT: never follow a link
        None,
    )
    if handle in (None, _INVALID_HANDLE_VALUE):
        error = ctypes.get_last_error()
        if error in (2, 3):  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
            raise FileNotFoundError(str(path))
        if error == 32:  # ERROR_SHARING_VIOLATION: a concurrent creator is writing it
            return b""
        raise SecretFileError(f"The service secret file could not be opened (Windows error {error}).")
    try:
        attrs = _file_attributes(handle)
        if attrs & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise SecretFileError("The service secret is a link, not a file.")
        if attrs & 0x10:  # FILE_ATTRIBUTE_DIRECTORY
            raise SecretFileError("The service secret is not a regular file.")
        verify_descriptor(describe_handle(handle))
    except BaseException:
        k32.CloseHandle(handle)
        raise
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        return stream.read(4096)


@dataclass(frozen=True)
class SecurityDescription:
    """A file's owner and DACL, as SID strings. ``aces`` is None for a NULL DACL."""

    owner: str | None
    protected: bool
    aces: tuple[tuple[int, int, str], ...] | None  # (ace_type, ace_flags, sid)


def verify_descriptor(described: SecurityDescription, user_sid: str | None = None) -> None:
    """Raise unless the owner is the user and only the user and SYSTEM are granted access.

    Accounts are compared by SID value: ``described`` holds canonical SID
    strings read from the binary descriptor, and ``user_sid`` is brought to
    the same form, so an account that SDDL abbreviates (``NS``, ``LS``,
    ``SY``) is judged the same as its ``S-1-5-...`` spelling.

    The access list is judged before the owner, so a file that fails both
    gets the same reason under every account: an elevated administrator's
    files default to the Administrators group as owner, a plain user's to
    the user.
    """

    user = canonical_sid(user_sid) if user_sid else current_user_sid()
    if described.aces is None:
        raise SecretFileError("The service secret file grants access to everyone (no access list).")
    if not described.protected:
        raise SecretFileError("The service secret file inherits access from its folder.")
    allowed = {user, _SYSTEM_SID}
    for ace_type, ace_flags, sid in described.aces:
        if ace_type != 0:  # ACCESS_ALLOWED_ACE_TYPE only
            raise SecretFileError("The service secret file has an unexpected kind of access entry.")
        if ace_flags & 0x10:  # INHERITED_ACE
            raise SecretFileError("The service secret file inherits access from its folder.")
        if sid not in allowed:
            raise SecretFileError("The service secret file grants access to another account.")
    if described.owner != user:
        raise SecretFileError("The service secret file is owned by another account.")


def describe_handle(handle: int) -> SecurityDescription:
    """The owner and DACL of an open file handle (opened with ``READ_CONTROL``)."""

    import ctypes
    from ctypes import wintypes

    _k32, adv = _win()
    owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = adv.GetSecurityInfo(
        wintypes.HANDLE(handle),
        1,  # SE_FILE_OBJECT
        0x1 | 0x4,  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(sd),
    )
    if status != 0:
        raise SecretFileError(f"The service secret file's permissions could not be read (Windows error {status}).")
    return _describe_and_free(owner, dacl, sd)


def describe_path(path: str | os.PathLike[str]) -> SecurityDescription:
    """The owner and DACL of a file or directory, by name."""

    import ctypes

    _k32, adv = _win()
    owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = adv.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x1 | 0x4,  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(sd),
    )
    if status != 0:
        raise OSError(status, "GetNamedSecurityInfoW failed", str(path))
    return _describe_and_free(owner, dacl, sd)


def _describe_and_free(owner, dacl, sd) -> SecurityDescription:
    import ctypes
    from ctypes import wintypes

    k32, adv = _win()
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            raise SecretFileError("The service secret file's permissions could not be read.")
        protected = bool(control.value & 0x1000)  # SE_DACL_PROTECTED
        owner_sid = _sid_string(owner) if owner.value else None
        if not dacl.value:
            return SecurityDescription(owner_sid, protected, None)
        info = _ACL_SIZE_INFORMATION()
        if not adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):  # AclSizeInformation
            raise SecretFileError("The service secret file's access list could not be read.")
        aces = []
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                raise SecretFileError("The service secret file's access list could not be read.")
            header = _ACE_HEADER.from_address(ace.value)
            # ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE: header, Mask, then the SID.
            sid = _sid_string(ctypes.c_void_p(ace.value + 8)) if header.AceType in (0, 1) else "?"
            aces.append((header.AceType, header.AceFlags, sid))
        return SecurityDescription(owner_sid, protected, tuple(aces))
    finally:
        k32.LocalFree(sd)


def current_user_sid() -> str:
    """The SID string of the account this process runs as."""

    import ctypes
    from ctypes import wintypes

    k32, adv = _win()
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        needed = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first field is the PSID.
        return _sid_string(ctypes.c_void_p.from_buffer(buffer))
    finally:
        k32.CloseHandle(token)


def protect_directory(path: str | os.PathLike[str]) -> None:
    """Give a directory the secret file's access list, inherited by everything in it.

    For a directory that may come to hold the secret by accident -- a former
    desktop window's browser profile recorded the URL it was opened with. The
    new ACL propagates to the existing contents, so it is applied only when
    the directory is not private already. Raises ``OSError`` on failure.
    """

    if sys.platform != "win32":
        os.chmod(path, 0o700)
        return
    import ctypes

    k32, adv = _win()
    user = current_user_sid()
    if directory_is_private(describe_path(path), user):
        return
    sd = _security_descriptor(f"D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)")
    try:
        present = ctypes.c_int()
        dacl = ctypes.c_void_p()
        defaulted = ctypes.c_int()
        if not adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise OSError(ctypes.get_last_error(), "GetSecurityDescriptorDacl failed")
        status = adv.SetNamedSecurityInfoW(
            str(path),
            1,  # SE_FILE_OBJECT
            0x4 | 0x80000000,  # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            None,
            None,
            dacl,
            None,
        )
        if status != 0:
            raise OSError(status, "SetNamedSecurityInfoW failed", str(path))
    finally:
        k32.LocalFree(sd)


def directory_is_private(described: SecurityDescription, user_sid: str) -> bool:
    """A protected DACL that allows only the user and SYSTEM. The owner is not
    consulted: an elevated run may have made Administrators the owner."""

    if described.aces is None or not described.protected:
        return False
    user = canonical_sid(user_sid)
    return all(ace_type == 0 and sid in (user, _SYSTEM_SID) for ace_type, _flags, sid in described.aces)


def _create_exclusively(path: Path, value: str) -> None:
    if sys.platform == "win32":
        _create_windows(path, value)
    else:
        _create_posix(path, value)


# ctypes plumbing, bound lazily so importing this module never touches Win32.

_INVALID_HANDLE_VALUE = _ctypes.c_void_p(-1).value

try:  # pragma: no cover - the structures are plain ctypes declarations
    from ctypes import wintypes as _wintypes

    class _SECURITY_ATTRIBUTES(_ctypes.Structure):
        _fields_ = [("nLength", _wintypes.DWORD), ("lpSecurityDescriptor", _ctypes.c_void_p), ("bInheritHandle", _wintypes.BOOL)]

    class _ACL_SIZE_INFORMATION(_ctypes.Structure):
        _fields_ = [("AceCount", _wintypes.DWORD), ("AclBytesInUse", _wintypes.DWORD), ("AclBytesFree", _wintypes.DWORD)]

    class _ACE_HEADER(_ctypes.Structure):
        _fields_ = [("AceType", _ctypes.c_ubyte), ("AceFlags", _ctypes.c_ubyte), ("AceSize", _wintypes.WORD)]

    class _BY_HANDLE_FILE_INFORMATION(_ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", _wintypes.DWORD),
            ("ftCreationTime", _wintypes.FILETIME),
            ("ftLastAccessTime", _wintypes.FILETIME),
            ("ftLastWriteTime", _wintypes.FILETIME),
            ("dwVolumeSerialNumber", _wintypes.DWORD),
            ("nFileSizeHigh", _wintypes.DWORD),
            ("nFileSizeLow", _wintypes.DWORD),
            ("nNumberOfLinks", _wintypes.DWORD),
            ("nFileIndexHigh", _wintypes.DWORD),
            ("nFileIndexLow", _wintypes.DWORD),
        ]
except (ImportError, ValueError):  # pragma: no cover - wintypes is Windows-only on some builds
    pass

_WIN: tuple | None = None


def _win() -> tuple:
    global _WIN
    if _WIN is not None:
        return _WIN
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.GetSecurityInfo.restype = wintypes.DWORD
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _WIN = (k32, adv)
    return _WIN


def _security_descriptor(sddl: str):
    import ctypes

    _k32, adv = _win()
    sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):  # SDDL_REVISION_1
        raise OSError(ctypes.get_last_error(), "ConvertStringSecurityDescriptorToSecurityDescriptorW failed")
    return sd


def _sid_string(sid) -> str:
    import ctypes
    from ctypes import wintypes

    k32, adv = _win()
    text = wintypes.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise SecretFileError("An account in the service secret file's access list could not be read.")
    try:
        return text.value or ""
    finally:
        k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def canonical_sid(value: str) -> str:
    """``value`` -- a SID string or an SDDL alias such as ``NS`` -- as the
    canonical ``S-1-...`` string that :func:`_sid_string` produces."""

    import ctypes

    k32, adv = _win()
    sid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(value, ctypes.byref(sid)):
        raise SecretFileError("An account name could not be read as a SID.")
    try:
        return _sid_string(sid)
    finally:
        k32.LocalFree(sid)


def _file_attributes(handle: int) -> int:
    import ctypes

    k32, _adv = _win()
    info = _BY_HANDLE_FILE_INFORMATION()
    if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise SecretFileError("The service secret file could not be inspected.")
    return info.dwFileAttributes
