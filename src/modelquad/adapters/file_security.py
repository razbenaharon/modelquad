# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Handle-based Windows DACLs for adapter writes; Win32 is loaded on demand.

Only DACLs are copied, never owners or SACLs. Unprotected copies retain their
explicit ACEs and inherit from today's parent; protected copies retain all ACEs.
All handles returned as CRT descriptors belong to the caller.
An unprotected NULL DACL is verified as NULL after application; if Windows
merges inherited ACEs into it, verification refuses the write before content.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import errno
import os
from pathlib import Path
from types import SimpleNamespace


class FileSecurityError(OSError):
    """A Windows security operation failed, without exposing file content."""


def _fail(code: int, operation: str, *, permission: bool = False) -> None:
    kind = PermissionError if permission or code == 5 else FileSecurityError
    error = kind(errno.EACCES if kind is PermissionError else errno.EIO, operation)
    error.winerror = code
    raise error


_API = None


def _win():
    global _API
    if _API is not None:
        return _API
    import ctypes as c
    from ctypes import wintypes as w

    class SecurityAttributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]

    class FileInfo(c.Structure):
        _fields_ = [
            ("attributes", w.DWORD), ("created", w.FILETIME),
            ("accessed", w.FILETIME), ("modified", w.FILETIME),
            ("volume", w.DWORD), ("size_high", w.DWORD), ("size_low", w.DWORD),
            ("links", w.DWORD), ("index_high", w.DWORD), ("index_low", w.DWORD),
        ]

    class AclInfo(c.Structure):
        _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]

    class FileAttributeTagInfo(c.Structure):
        _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    security = c.WinDLL("advapi32", use_last_error=True)
    signatures = [
        (kernel, "CreateFileW", c.c_void_p, [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]),
        (kernel, "CloseHandle", w.BOOL, [w.HANDLE]),
        (kernel, "LocalFree", c.c_void_p, [c.c_void_p]),
        (kernel, "GetCurrentProcess", w.HANDLE, []),
        (kernel, "GetFileInformationByHandle", w.BOOL, [w.HANDLE, c.POINTER(FileInfo)]),
        (kernel, "GetFileInformationByHandleEx", w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]),
        (kernel, "GetVolumeInformationByHandleW", w.BOOL, [w.HANDLE, w.LPWSTR, w.DWORD, c.POINTER(w.DWORD), c.POINTER(w.DWORD), c.POINTER(w.DWORD), w.LPWSTR, w.DWORD]),
        (kernel, "SetFileTime", w.BOOL, [w.HANDLE, c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME)]),
        (security, "OpenProcessToken", w.BOOL, [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]),
        (security, "GetTokenInformation", w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]),
        (security, "ConvertSidToStringSidW", w.BOOL, [c.c_void_p, c.POINTER(w.LPWSTR)]),
        (security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]),
        (security, "GetSecurityInfo", w.DWORD, [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p), c.c_void_p, c.POINTER(c.c_void_p)]),
        (security, "SetSecurityInfo", w.DWORD, [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p]),
        (security, "GetSecurityDescriptorControl", w.BOOL, [c.c_void_p, c.POINTER(w.WORD), c.POINTER(w.DWORD)]),
        (security, "GetSecurityDescriptorDacl", w.BOOL, [c.c_void_p, c.POINTER(w.BOOL), c.POINTER(c.c_void_p), c.POINTER(w.BOOL)]),
        (security, "GetAclInformation", w.BOOL, [c.c_void_p, c.c_void_p, w.DWORD, c.c_int]),
        (security, "GetAce", w.BOOL, [c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)]),
    ]
    for dll, name, result, arguments in signatures:
        function = getattr(dll, name)
        function.restype = result
        function.argtypes = arguments
    _API = SimpleNamespace(c=c, w=w, kernel=kernel, security=security,
                           SecurityAttributes=SecurityAttributes, FileInfo=FileInfo,
                           FileAttributeTagInfo=FileAttributeTagInfo,
                           AclInfo=AclInfo, invalid=c.c_void_p(-1).value)
    return _API


def current_user_sid() -> str:
    api = _win()
    c, w = api.c, api.w
    token = w.HANDLE()
    if not api.security.OpenProcessToken(api.kernel.GetCurrentProcess(), 8, c.byref(token)):
        _fail(c.get_last_error(), "Cannot inspect the current account.")
    try:
        needed = w.DWORD()
        api.security.GetTokenInformation(token, 1, None, 0, c.byref(needed))
        if not needed.value:
            _fail(c.get_last_error(), "Cannot inspect the current account.")
        buffer = c.create_string_buffer(needed.value)
        if not api.security.GetTokenInformation(token, 1, buffer, needed.value, c.byref(needed)):
            _fail(c.get_last_error(), "Cannot inspect the current account.")
        sid = c.c_void_p.from_buffer(buffer)
        text = w.LPWSTR()
        if not api.security.ConvertSidToStringSidW(sid, c.byref(text)):
            _fail(c.get_last_error(), "Cannot inspect the current account.")
        try:
            return text.value
        finally:
            api.kernel.LocalFree(c.cast(text, c.c_void_p))
    finally:
        api.kernel.CloseHandle(token)


def _sddl(value: str):
    api = _win()
    descriptor = api.c.c_void_p()
    if not api.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(value, 1, api.c.byref(descriptor), None):
        _fail(api.c.get_last_error(), "Cannot construct a private access list.")
    return descriptor


@dataclass(frozen=True)
class Dacl:
    protected: bool
    acl: bytes | None  # None is a NULL DACL, not an empty ACL.
    aces: tuple[bytes, ...] | None


def _describe(descriptor, dacl) -> Dacl:
    api = _win()
    c, w = api.c, api.w
    control, revision = w.WORD(), w.DWORD()
    if not api.security.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)):
        _fail(c.get_last_error(), "Cannot read the access-list protection flag.", permission=True)
    protected = bool(control.value & 0x1000)  # SE_DACL_PROTECTED
    if not dacl.value:
        return Dacl(protected, None, None)
    info = api.AclInfo()
    if not api.security.GetAclInformation(dacl, c.byref(info), c.sizeof(info), 2):
        _fail(c.get_last_error(), "Cannot read the access list.", permission=True)
    aces = []
    for index in range(info.count):
        ace = c.c_void_p()
        if not api.security.GetAce(dacl, index, c.byref(ace)):
            _fail(c.get_last_error(), "Cannot read an access entry.", permission=True)
        header = c.string_at(ace, 4)
        raw = c.string_at(ace, int.from_bytes(header[2:4], "little"))
        if protected or not header[1] & 0x10:  # INHERITED_ACE
            aces.append(raw)
    size = int.from_bytes(c.string_at(dacl, 4)[2:4], "little")
    return Dacl(protected, c.string_at(dacl, size), tuple(aces))


def read_dacl(handle: int) -> Dacl:
    api = _win()
    descriptor, dacl = api.c.c_void_p(), api.c.c_void_p()
    status = api.security.GetSecurityInfo(handle, 1, 4, None, None, api.c.byref(dacl), None, api.c.byref(descriptor))
    if status:
        _fail(status, "Cannot read the existing access list.", permission=True)
    try:
        return _describe(descriptor, dacl)
    finally:
        api.kernel.LocalFree(descriptor)


def apply_dacl(handle: int, expected: Dacl) -> None:
    api = _win()
    buffer = api.c.create_string_buffer(expected.acl) if expected.acl is not None else None
    protection = 0x80000000 if expected.protected else 0x20000000
    status = api.security.SetSecurityInfo(handle, 1, 4 | protection, None, None, buffer, None)
    if status:
        _fail(status, "Cannot preserve the access list.", permission=True)


def verify_dacl(handle: int, expected: Dacl) -> None:
    actual = read_dacl(handle)
    if (actual.protected, actual.aces) != (expected.protected, expected.aces):
        _fail(5, "The written access list could not be verified.")


def _has_current_user_allow_ace(dacl: Dacl) -> bool:
    """Check all ACEs, including inherited ones, for a direct user grant.

    New files without this grant fall back to private protection. Group-only
    and unfamiliar ACE forms conservatively take the same private fallback.
    """
    if dacl.acl is None:
        return False
    api = _win()
    c = api.c
    buffer = c.create_string_buffer(dacl.acl)
    info = api.AclInfo()
    if not api.security.GetAclInformation(buffer, c.byref(info), c.sizeof(info), 2):
        _fail(c.get_last_error(), "Cannot inspect inherited access entries.", permission=True)
    user = current_user_sid()
    for index in range(info.count):
        ace = c.c_void_p()
        if not api.security.GetAce(buffer, index, c.byref(ace)):
            _fail(c.get_last_error(), "Cannot inspect inherited access entries.", permission=True)
        header = c.string_at(ace, 4)
        if header[0] != 0 or header[1] & 8:  # ACCESS_ALLOWED_ACE, not INHERIT_ONLY
            continue
        text = api.w.LPWSTR()
        # An ACCESS_ALLOWED_ACE has its SID after the header and access mask.
        if not api.security.ConvertSidToStringSidW(c.c_void_p(ace.value + 8), c.byref(text)):
            _fail(c.get_last_error(), "Cannot inspect an inherited account.", permission=True)
        try:
            if text.value == user:
                return True
        finally:
            api.kernel.LocalFree(c.cast(text, c.c_void_p))
    return False


def _private_sddl() -> str:
    return f"D:P(A;;FA;;;{current_user_sid()})(A;;FA;;;SY)"


def _dacl_from_sddl(value: str) -> Dacl:
    api = _win()
    descriptor = _sddl(value)
    try:
        present, defaulted, dacl = api.w.BOOL(), api.w.BOOL(), api.c.c_void_p()
        if not api.security.GetSecurityDescriptorDacl(descriptor, api.c.byref(present), api.c.byref(dacl), api.c.byref(defaulted)):
            _fail(api.c.get_last_error(), "Cannot construct the access list.")
        return _describe(descriptor, dacl)
    finally:
        api.kernel.LocalFree(descriptor)


def _open(path: Path, access: int, sharing: int, creation: int, flags: int, attributes=None) -> int:
    api = _win()
    handle = api.kernel.CreateFileW(str(path), access, sharing, attributes, creation, flags, None)
    if handle in (None, api.invalid):
        code = api.c.get_last_error()
        if code in (80, 183):
            error = FileExistsError(errno.EEXIST, "The candidate already exists.", str(path))
        elif code in (2, 3):
            error = FileNotFoundError(errno.ENOENT, "The file does not exist.", str(path))
        else:
            _fail(code, "Cannot open the file with ModelQuad's file checks (it may be a link or in use).")
        error.winerror = code
        raise error
    return handle


def _to_fd(handle: int, flags: int) -> int:
    import msvcrt

    try:
        return msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
    except OSError as error:
        _win().kernel.CloseHandle(handle)
        _fail(getattr(error, "winerror", None) or 6, "Cannot transfer ownership of the file handle.",
              permission=error.errno == errno.EACCES)
    except BaseException:
        _win().kernel.CloseHandle(handle)
        raise


def create_exclusive(path: Path) -> int:
    """Claim a private, non-shared empty file; return its writable CRT fd."""
    api = _win()
    descriptor = _sddl(_private_sddl())
    try:
        attributes = api.SecurityAttributes(api.c.sizeof(api.SecurityAttributes), descriptor, False)
        handle = _open(path, 0x40000000 | 0x20000 | 0x40000 | 0x10000,
                       0, 1, 0x80, api.c.byref(attributes))
    finally:
        api.kernel.LocalFree(descriptor)
    try:
        return _to_fd(handle, os.O_WRONLY)
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise


@contextmanager
def cleanup_access(path: Path):
    """Retain READ_CONTROL and WRITE_DAC for cleanup of restrictive copies.

    Enter while the newly created file is still private and exclusively open.
    SetSecurityInfo needs READ_CONTROL when merging inherited ACEs. Neither
    right requests data or DELETE access, so this metadata-only handle can
    coexist with the non-shared writer. Sharing read/write/delete lets
    Path.replace publish after the writer closes. On failure, restore private
    full control only if the temporary name still identifies this file, before
    unlink. A published file keeps its DACL even if interruption follows rename.
    """
    handle = _open(path, 0x40000 | 0x20000, 1 | 2 | 4, 3, 0x00200000)
    try:
        yield
    except BaseException:
        # Preserve the primary error; callers still attempt their unlink seam.
        with suppress(OSError):
            # A replace can succeed just before an asynchronous exception.
            # Its retained handle then refers to the published destination.
            # Never change that DACL if the name is gone or now names a decoy.
            named_handle = _open(path, 0x20000, 1 | 2 | 4, 3, 0x00200000)
            try:
                retained, named = file_info(handle), file_info(named_handle)
                if (retained.volume, retained.index_high, retained.index_low) == (
                        named.volume, named.index_high, named.index_low):
                    apply_dacl(handle, _dacl_from_sddl(_private_sddl()))
            finally:
                _win().kernel.CloseHandle(named_handle)
        raise
    finally:
        _win().kernel.CloseHandle(handle)


def _handle(descriptor: int) -> int:
    import msvcrt

    try:
        return msvcrt.get_osfhandle(descriptor)
    except OSError as error:
        _fail(getattr(error, "winerror", None) or 6, "Cannot inspect the file handle.",
              permission=error.errno == errno.EACCES)


def persistent_acls(handle: int) -> bool:
    api = _win()
    flags = api.w.DWORD()
    if not api.kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, api.c.byref(flags), None, 0):
        _fail(api.c.get_last_error(), "Cannot inspect volume access-list support.")
    return bool(flags.value & 8)  # FILE_PERSISTENT_ACLS


def file_info(handle: int):
    api = _win()
    info = api.FileInfo()
    if not api.kernel.GetFileInformationByHandle(handle, api.c.byref(info)):
        _fail(api.c.get_last_error(), "Cannot inspect the file.")
    return info


def reparse_tag(handle: int) -> int:
    """Read the tag without following the reparse point."""
    api = _win()
    info = api.FileAttributeTagInfo()
    if not api.kernel.GetFileInformationByHandleEx(handle, 9, api.c.byref(info), api.c.sizeof(info)):
        _fail(api.c.get_last_error(), "Cannot inspect the file's reparse tag.", permission=True)
    return info.tag


def destination_dacl(path: Path) -> Dacl | None:
    """Read through a non-following handle; missing files and links are new."""
    try:
        handle = _open(path, 0x20000, 1 | 2 | 4, 3, 0x00200000)
    except FileNotFoundError:
        return None
    except OSError as error:
        _fail(getattr(error, "winerror", 5), "Cannot read the existing access list.", permission=True)
    try:
        if (file_info(handle).attributes & 0x400
                and reparse_tag(handle) & 0x20000000):  # Name surrogate: symlink/junction.
            return None
        return read_dacl(handle)
    finally:
        _win().kernel.CloseHandle(handle)


def prepare_write(descriptor: int, path: Path, *, private: bool = False,
                  source_descriptor: int | None = None) -> None:
    """Set and verify final security before content reaches the exclusive fd.

    Private overrides any existing DACL. On volumes without persistent ACLs,
    ordinary writes continue unchanged and private writes refuse. New files
    inherit the folder's ACEs, falling back to user-and-SYSTEM protection when
    there is no direct user grant, including folders with no inheritable ACEs.
    """
    handle = _handle(descriptor)
    if not persistent_acls(handle):
        if private:
            _fail(50, "This volume cannot protect a private backup.")
        return
    new_file = False
    if private:
        expected = _dacl_from_sddl(_private_sddl())
    elif source_descriptor is not None:
        expected = read_dacl(_handle(source_descriptor))
    else:
        expected = destination_dacl(path)
        if expected is None:
            new_file = True
            # An unprotected empty ACL inherits the parent without explicit ACEs.
            expected = _dacl_from_sddl("D:")
    apply_dacl(handle, expected)
    verify_dacl(handle, expected)
    if new_file and not _has_current_user_allow_ace(read_dacl(handle)):
        expected = _dacl_from_sddl(_private_sddl())
        apply_dacl(handle, expected)
        verify_dacl(handle, expected)


def open_source(path: Path) -> int:
    """Open one stable backup source, sharing reads but neither writes nor delete."""
    return _to_fd(_open(path, 0x80000000 | 0x20000, 1, 3, 0x80), os.O_RDONLY)


def source_info(descriptor: int):
    return file_info(_handle(descriptor))


def copy_times(descriptor: int, info) -> None:
    api = _win()
    if not api.kernel.SetFileTime(_handle(descriptor), api.c.byref(info.created),
                                  api.c.byref(info.accessed), api.c.byref(info.modified)):
        _fail(api.c.get_last_error(), "Cannot preserve backup timestamps.")
