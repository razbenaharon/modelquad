# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Native adapter DACL regressions. Every changed ACL is inside tmp_path.

These run outside the Codex sandbox. Readers and setters here
bind Win32 independently of the production security-description implementation.
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from orchestrator.adapters import atomicwrite, backup, file_security, registration


windows = pytest.mark.skipif(os.name != "nt", reason="Requires native Windows persistent DACLs")


@pytest.fixture
def native():
    if os.name != "nt":
        pytest.skip("Independent Win32 security reader requires Windows")
    kernel = c.WinDLL("kernel32", use_last_error=True)
    security = c.WinDLL("advapi32", use_last_error=True)
    signatures = [
        (kernel, "CreateFileW", c.c_void_p, [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]),
        (kernel, "CloseHandle", w.BOOL, [w.HANDLE]),
        (kernel, "LocalFree", c.c_void_p, [c.c_void_p]),
        (security, "GetNamedSecurityInfoW", w.DWORD, [w.LPCWSTR, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]),
        (security, "GetSecurityInfo", w.DWORD, [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]),
        (security, "ConvertSecurityDescriptorToStringSecurityDescriptorW", w.BOOL, [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.POINTER(w.DWORD)]),
        (security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]),
        (security, "GetSecurityDescriptorDacl", w.BOOL, [c.c_void_p, c.POINTER(w.BOOL), c.POINTER(c.c_void_p), c.POINTER(w.BOOL)]),
        (security, "GetSecurityDescriptorControl", w.BOOL, [c.c_void_p, c.POINTER(w.WORD), c.POINTER(w.DWORD)]),
        (security, "SetNamedSecurityInfoW", w.DWORD, [w.LPWSTR, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p]),
    ]
    for dll, name, result, arguments in signatures:
        method = getattr(dll, name)
        method.restype, method.argtypes = result, arguments
    return SimpleNamespace(kernel=kernel, security=security, invalid=c.c_void_p(-1).value)


def _read(native, path=None, *, handle=None):
    descriptor = c.c_void_p()
    if handle is None:
        status = native.security.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, c.byref(descriptor))
    else:
        status = native.security.GetSecurityInfo(handle, 1, 4, None, None, None, None, c.byref(descriptor))
    assert status == 0, status
    text = w.LPWSTR()
    try:
        control, revision = w.WORD(), w.DWORD()
        assert native.security.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision))
        assert native.security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, c.byref(text), None)
        return bool(control.value & 0x1000), text.value
    finally:
        if text:
            native.kernel.LocalFree(c.cast(text, c.c_void_p))
        native.kernel.LocalFree(descriptor)


def _explicit(description):
    protected, text = description
    aces = tuple(ace for ace in re.findall(r"\([^)]*\)", text) if "ID" not in ace.split(";")[1])
    return protected, aces


def _entries(description):
    protected, text = description
    return protected, tuple(re.findall(r"\([^)]*\)", text))


def _set(native, path, sddl):
    descriptor, dacl = c.c_void_p(), c.c_void_p()
    assert native.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None)
    try:
        present, defaulted = w.BOOL(), w.BOOL()
        assert native.security.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted))
        protection = 0x80000000 if sddl.startswith("D:P") else 0x20000000
        status = native.security.SetNamedSecurityInfoW(str(path), 1, 4 | protection, None, None, dacl, None)
        assert status == 0, status
    finally:
        native.kernel.LocalFree(descriptor)


@pytest.fixture
def acl_tree(tmp_path, native):
    """Reset every test-set DACL, even when a replaced file has a new identity."""
    changed = []

    def set_acl(path, sddl):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        changed.append(Path(path))
        _set(native, path, sddl)

    try:
        yield set_acl
    finally:
        # Include files created by production and renamed sources as well.
        # Reset parents first; child resets pick up the normal inheritance.
        paths = set(changed) | {tmp_path} | set(tmp_path.rglob("*"))
        _reset_acl_paths(native, paths)


def _reset_acl_paths(native, paths, setter=None):
    setter = setter or _set
    failures = []
    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        try:
            if path.exists() and not path.is_symlink():
                setter(native, path, "D:")
        except Exception as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup("Could not reset every test DACL", failures)


@pytest.fixture(autouse=True)
def reset_production_acls(request):
    """Also reset private ACLs created by tests that do not set their own."""
    if os.name == "nt" and "tmp_path" in request.fixturenames:
        request.getfixturevalue("acl_tree")


def test_acl_teardown_attempts_every_reset(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("first", "second", "third")]
    for path in paths:
        path.touch()
    failures = [AssertionError("first reset failed"), OSError("second reset failed")]
    attempted = []

    def reset(native, path, sddl):
        assert sddl == "D:"
        attempted.append(path)
        if path in paths[:2]:
            raise failures[paths.index(path)]

    # Injected rather than monkeypatched: the autouse ACL teardown runs after
    # monkeypatch is undone only for fixtures set up later, so a patched _set
    # would leak into this test's own teardown.
    with pytest.raises(ExceptionGroup) as raised:
        _reset_acl_paths(None, paths, reset)
    assert attempted == paths
    assert raised.value.exceptions == tuple(failures)


@pytest.fixture
def user():
    return file_security.current_user_sid()


def _broad_folder(acl_tree, tmp_path, user):
    acl_tree(tmp_path, f"D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)(A;OICI;GR;;;BG)")


# SDDL renders well-known accounts by alias: the self-hosted preflight runs as
# NETWORK SERVICE ("NS"), the hosted runner as the local Administrator ("LA").
# Compare trustees by their SID string, converted by Windows itself, so the
# assertion holds for whatever account runs the suite.
def _trustee(value):
    if os.name != "nt":
        return value
    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    to_sid = advapi.ConvertStringSidToSidW
    to_sid.restype, to_sid.argtypes = w.BOOL, [w.LPCWSTR, c.POINTER(c.c_void_p)]
    to_text = advapi.ConvertSidToStringSidW
    to_text.restype, to_text.argtypes = w.BOOL, [c.c_void_p, c.POINTER(w.LPWSTR)]
    kernel.LocalFree.restype, kernel.LocalFree.argtypes = c.c_void_p, [c.c_void_p]
    sid = c.c_void_p()
    assert to_sid(value, c.byref(sid)), f"not a SID or SDDL alias: {value!r}"
    try:
        text = w.LPWSTR()
        assert to_text(sid, c.byref(text))
        try:
            return text.value
        finally:
            kernel.LocalFree(c.cast(text, c.c_void_p))
    finally:
        kernel.LocalFree(sid)


def test_trustee_normalises_sddl_aliases():
    if os.name != "nt":
        pytest.skip("SDDL aliases are a Windows concept")
    assert _trustee("SY") == "S-1-5-18"
    assert _trustee("NS") == "S-1-5-20"
    assert _trustee("S-1-5-20") == "S-1-5-20"
    assert _trustee("LA").startswith("S-1-5-21-") and _trustee("LA").endswith("-500")


def _assert_private(description, user):
    protected, text = description
    assert protected
    aces = re.findall(r"\(([^)]*)\)", text)
    assert len(aces) == 2
    assert {_trustee(ace.split(";")[-1]) for ace in aces} == {_trustee(user), "S-1-5-18"}
    assert all(ace.split(";")[:3] == ["A", "", "FA"] for ace in aces)


def _assert_exclusive(native, path):
    handle = native.kernel.CreateFileW(str(path), 0x80000000, 1 | 2 | 4, None, 3, 0x80, None)
    error = c.get_last_error()
    if handle not in (None, native.invalid):
        native.kernel.CloseHandle(handle)
    assert handle == native.invalid
    assert error == 32  # ERROR_SHARING_VIOLATION, not just an ACL denial.


@windows
def test_windows_replacement_preserves_restricted_dacl(tmp_path, native, acl_tree, user):
    _broad_folder(acl_tree, tmp_path, user)
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;FA;;;{user})")
    before = _read(native, target)
    atomicwrite.atomic_write(target, b"replacement")
    assert _read(native, target) == before
    assert target.read_bytes() == b"replacement"


@windows
def test_windows_explicit_deny_survives_replacement(tmp_path, native, acl_tree, user):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(D;;GR;;;BG)(A;;FA;;;{user})(A;;FA;;;SY)")
    before = _read(native, target)
    atomicwrite.atomic_write(target, b"replacement")
    assert _read(native, target) == before


@windows
@pytest.mark.parametrize("private", [False, True], ids=["preserved-dacl", "private-overrides"])
def test_windows_temp_is_restricted_before_content(tmp_path, native, acl_tree, user, monkeypatch, private):
    import msvcrt

    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;FA;;;{user})")
    expected = _read(native, target)
    real_fdopen = os.fdopen
    inspected = []

    def before_write(descriptor, *args, **kwargs):
        temporary, = tmp_path.glob("config.json.*.tmp")
        assert os.fstat(descriptor).st_size == 0
        actual = _read(native, handle=msvcrt.get_osfhandle(descriptor))
        if private:
            _assert_private(actual, user)
        else:
            assert actual == expected
        _assert_exclusive(native, temporary)
        inspected.append(temporary)
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", before_write)
    atomicwrite.atomic_write(target, b"secret", private=private)
    assert len(inspected) == 1


@windows
def test_creation_is_private_and_exclusive_before_dacl_copy(tmp_path, native, acl_tree, user, monkeypatch):
    import msvcrt

    _broad_folder(acl_tree, tmp_path, user)
    target = tmp_path / "new.json"
    real_prepare = file_security.prepare_write
    inspected = []

    def before_prepare(descriptor, *args, **kwargs):
        temporary, = tmp_path.glob("new.json.*.tmp")
        assert os.fstat(descriptor).st_size == 0
        _assert_private(_read(native, handle=msvcrt.get_osfhandle(descriptor)), user)
        _assert_exclusive(native, temporary)
        inspected.append(temporary)
        return real_prepare(descriptor, *args, **kwargs)

    monkeypatch.setattr(file_security, "prepare_write", before_prepare)
    atomicwrite.atomic_write(target, b"new")
    assert len(inspected) == 1


@windows
@pytest.mark.parametrize("failure", ["read_descriptor", "apply_descriptor", "verify"], ids=["read_descriptor", "apply_descriptor", "verify"])
def test_security_failure_preserves_original(tmp_path, native, acl_tree, user, monkeypatch, failure):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;FA;;;{user})")
    before = _read(native, target)
    seam = {"read_descriptor": "destination_dacl", "apply_descriptor": "apply_dacl", "verify": "verify_dacl"}[failure]

    def refused(*args, **kwargs):
        raise PermissionError("Injected security failure")

    monkeypatch.setattr(file_security, seam, refused)
    with pytest.raises(PermissionError):
        atomicwrite.atomic_write(target, b"secret")
    assert target.read_bytes() == b"original"
    assert _read(native, target) == before
    assert list(tmp_path.iterdir()) == [target]


@windows
def test_new_file_keeps_inherited_acl_by_default(tmp_path, native, acl_tree, user):
    _broad_folder(acl_tree, tmp_path, user)
    reference = tmp_path / "reference"
    reference.write_bytes(b"reference")
    target = tmp_path / "new.json"
    atomicwrite.atomic_write(target, b"new")
    assert _entries(_read(native, target)) == _entries(_read(native, reference))
    assert "BG" in _read(native, target)[1]
    assert not _read(native, target)[0]


@windows
def test_new_file_without_inheritable_aces_is_private_and_readable(tmp_path, native, acl_tree, user, monkeypatch):
    import msvcrt

    acl_tree(tmp_path, f"D:P(A;;FA;;;{user})(A;;FA;;;SY)")
    target = tmp_path / "new.json"
    real_fdopen = os.fdopen
    inspected = []

    def before_write(descriptor, *args, **kwargs):
        temporary, = tmp_path.glob("new.json.*.tmp")
        assert os.fstat(descriptor).st_size == 0
        _assert_private(_read(native, handle=msvcrt.get_osfhandle(descriptor)), user)
        _assert_exclusive(native, temporary)
        inspected.append(temporary)
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", before_write)
    atomicwrite.atomic_write(target, b"readable")
    assert len(inspected) == 1
    assert target.read_bytes() == b"readable"
    _assert_private(_read(native, target), user)
    assert list(tmp_path.iterdir()) == [target]


@windows
@pytest.mark.parametrize("failure", ["apply", "verify"], ids=["private-fallback-apply", "private-fallback-verify"])
def test_new_file_private_fallback_failure_leaves_no_temp(tmp_path, native, acl_tree, user, monkeypatch, failure):
    acl_tree(tmp_path, f"D:P(A;;FA;;;{user})(A;;FA;;;SY)")
    target = tmp_path / "new.json"
    seam = "apply_dacl" if failure == "apply" else "verify_dacl"
    real_operation = getattr(file_security, seam)
    refused = []

    def operation(handle, expected):
        if expected.protected:
            refused.append(handle)
            raise PermissionError("Injected private fallback failure")
        return real_operation(handle, expected)

    def must_not_write(*args, **kwargs):
        pytest.fail("Content must not be written when private fallback fails")

    monkeypatch.setattr(file_security, seam, operation)
    monkeypatch.setattr(os, "fdopen", must_not_write)
    with pytest.raises(PermissionError):
        atomicwrite.atomic_write(target, b"secret")
    assert refused
    assert list(tmp_path.iterdir()) == []


@windows
@pytest.mark.parametrize("kind", ["explicit", "inherited", "inherit-only", "system-only", "empty"],
                         ids=["explicit-user", "inherited-user", "inherit-only-user", "other-account", "empty-dacl"])
def test_new_file_user_grant_detection(user, kind):
    sddl = {
        "explicit": f"D:(A;;FA;;;{user})",
        "inherited": f"D:(A;ID;FA;;;{user})",
        "inherit-only": f"D:(A;OIIO;FA;;;{user})",
        "system-only": "D:(A;;FA;;;SY)",
        "empty": "D:",
    }[kind]
    description = file_security._dacl_from_sddl(sddl)
    assert file_security._has_current_user_allow_ace(description) == (kind in ("explicit", "inherited"))


@windows
def test_private_new_file_has_user_and_system_only(tmp_path, native, acl_tree, user):
    _broad_folder(acl_tree, tmp_path, user)
    target = tmp_path / "new.json"
    atomicwrite.atomic_write(target, b"secret", private=True)
    _assert_private(_read(native, target), user)


@windows
def test_unprotected_replacement_preserves_explicit_aces(tmp_path, native, acl_tree, user):
    _broad_folder(acl_tree, tmp_path, user)
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:(D;;GR;;;BG)(A;;FA;;;{user})")
    before = _explicit(_read(native, target))
    assert not before[0]
    atomicwrite.atomic_write(target, b"replacement")
    assert _explicit(_read(native, target)) == before


@pytest.mark.parametrize("protected", [True, False], ids=["protected-null", "unprotected-null"])
def test_null_dacl_is_preserved(monkeypatch, protected):
    """Verify NULL handling without granting real local accounts access."""
    calls = []
    expected = file_security.Dacl(protected, None, None)

    def set_security(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(file_security, "_win", lambda: SimpleNamespace(
        c=c, security=SimpleNamespace(SetSecurityInfo=set_security)))
    monkeypatch.setattr(file_security, "read_dacl", lambda handle: expected)
    file_security.apply_dacl(123, expected)
    file_security.verify_dacl(123, expected)
    assert calls == [(123, 1, 4 | (0x80000000 if protected else 0x20000000), None, None, None, None)]


def test_null_dacl_verification_rejects_empty_dacl(monkeypatch):
    monkeypatch.setattr(file_security, "read_dacl", lambda handle: file_security.Dacl(True, b"empty", ()))
    with pytest.raises(PermissionError):
        file_security.verify_dacl(123, file_security.Dacl(True, None, None))


@windows
def test_read_only_user_dacl_has_clean_outcome(tmp_path, native, acl_tree, user):
    """Rename may need parent delete permission; failure must preserve the file."""
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;GR;;;{user})")
    before = _read(native, target)
    try:
        atomicwrite.atomic_write(target, b"replacement")
    except OSError:
        assert target.read_bytes() == b"original"
    else:
        assert target.read_bytes() == b"replacement"
    assert _read(native, target) == before
    assert list(tmp_path.iterdir()) == [target]


@windows
@pytest.mark.parametrize("failure", ["write", "encoding", "replace"], ids=["partial-write", "wrapper-failure", "publication-failure"])
def test_read_only_atomic_failure_leaves_no_temp(tmp_path, native, acl_tree, user, monkeypatch, failure):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;GR;;;{user})")
    before = _read(native, target)
    error = OSError("Injected read-only atomic failure")
    real_fdopen = os.fdopen

    class BrokenWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def write(self, content):
            self.handle.write(content[:2])
            self.handle.flush()
            raise error

        def __exit__(self, *args):
            self.handle.close()

    def fdopen(descriptor, *args, **kwargs):
        return BrokenWriter(real_fdopen(descriptor, *args, **kwargs))

    def refused_replace(*args):
        raise error

    if failure == "write":
        monkeypatch.setattr(os, "fdopen", fdopen)
    elif failure == "replace":
        monkeypatch.setattr(Path, "replace", refused_replace)
    with pytest.raises(LookupError if failure == "encoding" else OSError) as raised:
        atomicwrite.atomic_write(
            target, "replacement", encoding="invalid-encoding" if failure == "encoding" else "utf-8"
        )
    if failure != "encoding":
        assert raised.value is error
    assert list(tmp_path.iterdir()) == [target]
    assert target.read_bytes() == b"original"
    assert _read(native, target) == before


@windows
def test_interrupt_after_publication_preserves_published_dacl(tmp_path, native, acl_tree, user, monkeypatch):
    _broad_folder(acl_tree, tmp_path, user)
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;FA;;;{user})")
    before = _read(native, target)
    real_replace = Path.replace

    def publish_then_interrupt(temporary, destination):
        real_replace(temporary, destination)
        raise KeyboardInterrupt("Injected interruption after successful publication")

    monkeypatch.setattr(Path, "replace", publish_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        atomicwrite.atomic_write(target, b"replacement")
    assert target.read_bytes() == b"replacement"
    assert _read(native, target) == before
    assert list(tmp_path.iterdir()) == [target]


@windows
def test_cleanup_does_not_change_published_dacl_when_temp_name_is_reused(tmp_path, native, acl_tree, user):
    target, temporary = tmp_path / "config.json", tmp_path / "stage.tmp"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;FA;;;{user})")
    before = _read(native, target)
    descriptor = file_security.create_exclusive(temporary)
    try:
        with pytest.raises(KeyboardInterrupt):
            with file_security.cleanup_access(temporary):
                file_security.prepare_write(descriptor, target)
                os.write(descriptor, b"replacement")
                os.close(descriptor)
                descriptor = None
                temporary.replace(target)
                temporary.write_bytes(b"decoy")
                raise KeyboardInterrupt("Injected interruption after temp name reuse")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    assert _read(native, target) == before
    assert target.read_bytes() == b"replacement"
    assert temporary.read_bytes() == b"decoy"


@windows
@pytest.mark.parametrize("private", [False, True], ids=["plain-write", "private-refused"])
def test_volume_without_persistent_acls(tmp_path, monkeypatch, private):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    monkeypatch.setattr(file_security, "persistent_acls", lambda handle: False)

    def must_not_read(*args):
        pytest.fail("A volume without ACLs must not try to preserve a DACL")

    monkeypatch.setattr(file_security, "destination_dacl", must_not_read)
    if private:
        with pytest.raises(file_security.FileSecurityError) as raised:
            atomicwrite.atomic_write(target, b"secret", private=True)
        assert raised.value.winerror == 50
        assert target.read_bytes() == b"original"
    else:
        atomicwrite.atomic_write(target, b"replacement")
        assert target.read_bytes() == b"replacement"
    assert list(tmp_path.iterdir()) == [target]


@windows
def test_backup_on_volume_without_persistent_acls(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    monkeypatch.setattr(file_security, "persistent_acls", lambda handle: False)

    def must_not_read(*args):
        pytest.fail("A volume without ACLs must not try to preserve a DACL")

    monkeypatch.setattr(file_security, "read_dacl", must_not_read)
    candidate = backup.backup_if_exists(target, dry_run=False)
    assert candidate.read_bytes() == b"original"


@windows
def test_backup_copies_source_dacl_before_content(tmp_path, native, acl_tree, user, monkeypatch):
    import msvcrt

    target = tmp_path / "config.json"
    payload = b"secret" * 20000
    target.write_bytes(payload)
    os.utime(target, ns=(1234567890000000000, 1234567890123456700))
    expected_mtime = target.stat().st_mtime_ns
    acl_tree(target, f"D:P(D;;GR;;;BG)(A;;FA;;;{user})(A;;FA;;;SY)")
    expected_dacl = _read(native, target)
    real_copy = backup.shutil.copyfileobj
    inspected = []

    def before_copy(source, destination, *args, **kwargs):
        candidate, = tmp_path.glob("config.json.*-backup-*")
        assert os.fstat(destination.fileno()).st_size == 0
        assert _read(native, handle=msvcrt.get_osfhandle(destination.fileno())) == expected_dacl
        _assert_exclusive(native, candidate)
        inspected.append(candidate)
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(backup.shutil, "copyfileobj", before_copy)
    candidate = backup.backup_if_exists(target, dry_run=False)
    assert inspected == [candidate]
    assert candidate.read_bytes() == payload
    assert _read(native, candidate) == expected_dacl
    assert candidate.stat().st_mtime_ns == expected_mtime


@windows
@pytest.mark.parametrize("failure", ["read", "apply", "verify", "copy", "length", "timestamps"], ids=["read", "apply", "verify", "copy", "length", "timestamps"])
def test_backup_failure_leaves_no_partial_backup(tmp_path, native, acl_tree, user, monkeypatch, failure):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;FA;;;{user})")
    before = _read(native, target)

    def refused(*args, **kwargs):
        raise PermissionError("Injected backup failure")

    if failure == "copy":
        def partial_copy(source, destination):
            destination.write(source.read(2))
            raise OSError("Injected copy failure")
        monkeypatch.setattr(backup.shutil, "copyfileobj", partial_copy)
    elif failure == "length":
        monkeypatch.setattr(backup.shutil, "copyfileobj", lambda source, destination: destination.write(source.read(2)))
    else:
        seam = {"read": "read_dacl", "apply": "apply_dacl", "verify": "verify_dacl", "timestamps": "copy_times"}[failure]
        monkeypatch.setattr(file_security, seam, refused)
    with pytest.raises(OSError):
        backup.backup_if_exists(target, dry_run=False)
    assert list(tmp_path.iterdir()) == [target]
    assert target.read_bytes() == b"original"
    assert _read(native, target) == before
    # The stable source handle was released, including on preparation failure.
    target.rename(tmp_path / "source-handle-closed")


@windows
@pytest.mark.parametrize("failure", ["verify", "copy", "timestamps"], ids=["before-copy", "partial-copy", "after-copy"])
def test_read_only_backup_failure_leaves_no_partial_backup(tmp_path, native, acl_tree, user, monkeypatch, failure):
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    acl_tree(target, f"D:P(A;;GR;;;{user})")
    before = _read(native, target)
    error = OSError("Injected read-only backup failure")

    def refused(*args):
        raise error

    def partial_copy(source, destination):
        destination.write(source.read(2))
        destination.flush()
        raise error

    if failure == "copy":
        monkeypatch.setattr(backup.shutil, "copyfileobj", partial_copy)
    else:
        monkeypatch.setattr(file_security, "verify_dacl" if failure == "verify" else "copy_times", refused)
    with pytest.raises(OSError) as raised:
        backup.backup_if_exists(target, dry_run=False)
    assert raised.value is error
    assert list(tmp_path.iterdir()) == [target]
    assert target.read_bytes() == b"original"
    assert _read(native, target) == before
    # Both the source reader and metadata-only cleanup handle must be released.
    acl_tree(target, f"D:P(A;;FA;;;{user})")
    target.rename(tmp_path / "source-handle-closed")


@windows
@pytest.mark.parametrize("occupied", ["file", "symlink"], ids=["file", "symlink"])
def test_backup_exclusive_name_preserves_decoy(tmp_path, monkeypatch, occupied):
    from datetime import datetime, timezone

    class FixedClock:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(backup, "datetime", FixedClock)
    target = tmp_path / "config.json"
    target.write_bytes(b"original")
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"untouched")
    occupied_path = tmp_path / f"config.json.{backup.PRODUCT_SLUG}-backup-20261006T120000Z"
    if occupied == "symlink":
        _symlink_or_skip(occupied_path, decoy)
    else:
        occupied_path.write_bytes(b"untouched")
    result = backup.backup_if_exists(target, dry_run=False)
    assert result.name == occupied_path.name + "-2"
    assert result.read_bytes() == b"original"
    assert occupied_path.read_bytes() == decoy.read_bytes() == b"untouched"
    assert occupied_path.is_symlink() == (occupied == "symlink")


@windows
@pytest.mark.parametrize("existing", [False, True], ids=["new-private", "existing-inherited-tightened"])
def test_displaced_registration_backup_is_private(tmp_path, native, acl_tree, user, existing):
    _broad_folder(acl_tree, tmp_path, user)
    path = registration.backup_path(tmp_path)
    if existing:
        path.write_text('{"hosts": {}}', encoding="utf-8")
        assert not _read(native, path)[0]
        assert "BG" in _read(native, path)[1]
        registration._write_backups(path, {"claude_code": {"previous_entry": {"env": {"KEY": "fixture-secret"}}}})
    else:
        registration.record_replaced_registration(
            "claude_code", previous_entry={"command": "old", "env": {"KEY": "fixture-secret"}},
            previous_command="old", state_root=tmp_path,
        )
    _assert_private(_read(native, path), user)
    assert "fixture-secret" in path.read_text(encoding="utf-8")


@windows
@pytest.mark.parametrize("existing", [False, True], ids=["new-refused", "existing-preserved"])
def test_registration_backup_refused_when_protection_fails(tmp_path, native, monkeypatch, existing):
    path = registration.backup_path(tmp_path)
    if existing:
        path.write_bytes(b'{"hosts": {}}')
        before = _read(native, path)

    def refused(*args, **kwargs):
        raise PermissionError("fixture-secret must not reach the public reason")

    monkeypatch.setattr(file_security, "apply_dacl", refused)
    with pytest.raises(registration.BackupPersistenceError) as raised:
        registration.record_replaced_registration(
            "claude_code", previous_entry={"env": {"KEY": "fixture-secret"}},
            previous_command="old", state_root=tmp_path,
        )
    assert str(raised.value) == "Could not record the existing registration in a private backup."
    if existing:
        assert path.read_bytes() == b'{"hosts": {}}'
        assert _read(native, path) == before
        assert list(tmp_path.iterdir()) == [path]
    else:
        assert list(tmp_path.iterdir()) == []


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) in (5, 1314):
            pytest.skip("Windows symlink creation privilege is unavailable")
        raise


@pytest.mark.parametrize("dangling", [False, True], ids=["existing-target", "dangling-target"])
def test_destination_symlink_is_replaced_not_followed(tmp_path, dangling):
    target, decoy = tmp_path / "config.json", tmp_path / "decoy"
    if not dangling:
        decoy.write_bytes(b"untouched")
    _symlink_or_skip(target, decoy)
    atomicwrite.atomic_write(target, b"replacement")
    assert not target.is_symlink()
    assert target.read_bytes() == b"replacement"
    assert decoy.exists() == (not dangling)
    if not dangling:
        assert decoy.read_bytes() == b"untouched"


@windows
@pytest.mark.parametrize("dangling", [False, True], ids=["target-dacl-not-copied", "dangling-new-file"])
def test_symlink_destination_gets_new_file_dacl(tmp_path, native, acl_tree, user, dangling):
    _broad_folder(acl_tree, tmp_path, user)
    reference = tmp_path / "reference"
    reference.write_bytes(b"reference")
    target, decoy = tmp_path / "config.json", tmp_path / "decoy"
    if not dangling:
        decoy.write_bytes(b"untouched")
        acl_tree(decoy, f"D:P(A;;FA;;;{user})")
    _symlink_or_skip(target, decoy)
    atomicwrite.atomic_write(target, b"replacement")
    assert _entries(_read(native, target)) == _entries(_read(native, reference))
    if not dangling:
        assert decoy.read_bytes() == b"untouched"


@windows
@pytest.mark.parametrize("code", [2, 3, 5, 32], ids=["file-missing", "parent-missing", "access-denied", "sharing-violation"])
def test_destination_open_errors_are_not_silently_ignored(tmp_path, monkeypatch, code):
    def failed_open(*args):
        error = FileNotFoundError("missing") if code in (2, 3) else PermissionError("refused")
        error.winerror = code
        raise error

    monkeypatch.setattr(file_security, "_open", failed_open)
    if code in (2, 3):
        assert file_security.destination_dacl(tmp_path / "config") is None
    else:
        with pytest.raises(PermissionError):
            file_security.destination_dacl(tmp_path / "config")


@pytest.mark.parametrize("tag", [0xA000000C, 0xA0000003, 0x9000001A, 0x80000013],
                         ids=["symlink", "mount-point", "cloud-placeholder", "dedup-file"])
def test_only_name_surrogate_reparse_points_take_new_file_path(tmp_path, monkeypatch, tag):
    expected = file_security.Dacl(True, b"restricted", (b"user-only",))
    closed, reads = [], []
    monkeypatch.setattr(file_security, "_open", lambda *args: 123)
    monkeypatch.setattr(file_security, "file_info", lambda handle: SimpleNamespace(attributes=0x400))
    monkeypatch.setattr(file_security, "reparse_tag", lambda handle: tag)
    monkeypatch.setattr(file_security, "_win", lambda: SimpleNamespace(kernel=SimpleNamespace(CloseHandle=closed.append)))

    def read(handle):
        reads.append(handle)
        return expected

    monkeypatch.setattr(file_security, "read_dacl", read)
    actual = file_security.destination_dacl(tmp_path / "config")
    link = tag in (0xA000000C, 0xA0000003)
    assert actual == (None if link else expected)
    assert reads == ([] if link else [123])
    assert closed == [123]


@pytest.mark.parametrize("success", [True, False], ids=["tag-read", "tag-refused"])
def test_reparse_tag_uses_handle_information(monkeypatch, success):
    class TagInfo(c.Structure):
        _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]

    calls = []

    def get_info(handle, information_class, buffer, size):
        calls.append((handle, information_class, size))
        c.cast(buffer, c.POINTER(TagInfo)).contents.tag = 0x9000001A
        return success

    monkeypatch.setattr(file_security, "_win", lambda: SimpleNamespace(
        c=SimpleNamespace(byref=c.byref, sizeof=c.sizeof, get_last_error=lambda: 5),
        FileAttributeTagInfo=TagInfo,
        kernel=SimpleNamespace(GetFileInformationByHandleEx=get_info)))
    if success:
        assert file_security.reparse_tag(123) == 0x9000001A
    else:
        with pytest.raises(PermissionError) as raised:
            file_security.reparse_tag(123)
        assert raised.value.winerror == 5
    assert calls == [(123, 9, c.sizeof(TagInfo))]


def test_reparse_tag_failure_closes_destination_handle(tmp_path, monkeypatch):
    closed = []
    error = PermissionError("Injected tag query failure")
    monkeypatch.setattr(file_security, "_open", lambda *args: 123)
    monkeypatch.setattr(file_security, "file_info", lambda handle: SimpleNamespace(attributes=0x400))
    monkeypatch.setattr(file_security, "_win", lambda: SimpleNamespace(kernel=SimpleNamespace(CloseHandle=closed.append)))

    def refused(handle):
        raise error

    monkeypatch.setattr(file_security, "reparse_tag", refused)
    with pytest.raises(PermissionError) as raised:
        file_security.destination_dacl(tmp_path / "config")
    assert raised.value is error
    assert closed == [123]


@pytest.mark.parametrize("code", [5, 50, 87], ids=["access-denied", "not-supported", "invalid-parameter"])
def test_security_errors_are_oserrors_with_winerror(code):
    with pytest.raises(PermissionError if code == 5 else file_security.FileSecurityError) as raised:
        file_security._fail(code, "Fixed reason")
    assert raised.value.winerror == code
    assert isinstance(raised.value, OSError)
