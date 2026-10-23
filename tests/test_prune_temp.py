# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Pruning is confined to fake roots; no test reads the machine's temp root."""

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import no_window_kwargs


spec = importlib.util.spec_from_file_location(
    "_test_prune_temp", Path(__file__).resolve().parents[1] / "scripts" / "prune_temp.py",
)
pruner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pruner)
NOW = int(time.time())
AGE = pruner.TWO_DAYS
OLD = NOW - AGE - 60


def set_mtime(path, timestamp):
    options = {"follow_symlinks": False} if os.utime in os.supports_follow_symlinks else {}
    # Link callers use SetFileTime on Windows, so this fallback never follows
    # a Windows link. Regular entries are safe to follow on Python 3.11.
    os.utime(path, (timestamp, timestamp), **options)


def set_windows_times(path, timestamp, *, creation):
    """Set fixture timestamps without following links; skip if Win32 denies it."""
    import ctypes
    from ctypes import wintypes

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        kernel.CreateFileW.restype = wintypes.HANDLE
        filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
        kernel.SetFileTime.argtypes = (
            wintypes.HANDLE, filetime_pointer, filetime_pointer, filetime_pointer,
        )
        kernel.SetFileTime.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        # WRITE_ATTRIBUTES, share read/write/delete, OPEN_EXISTING,
        # BACKUP_SEMANTICS (directories) | OPEN_REPARSE_POINT (the link itself).
        handle = kernel.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ticks = int(timestamp * 10_000_000) + 116_444_736_000_000_000
            value = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
            pointer = ctypes.byref(value)
            if not kernel.SetFileTime(handle, pointer if creation else None, None, pointer):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    except AttributeError as exc:
        # Only a missing Win32 API skips; a real CreateFileW or SetFileTime
        # failure must fail the test rather than hide it as a skip.
        pytest.skip(f"Win32 SetFileTime unavailable for this fixture: {exc}")


def set_times(path, timestamp, *, creation=True):
    """Backdate actual creation time where prefix mode uses it, plus mtime."""
    if os.name == "nt":
        set_windows_times(path, timestamp, creation=creation)
    else:
        if creation and hasattr(path.lstat(), "st_birthtime"):
            pytest.skip("this platform exposes creation time but has no SetFileTime")
        set_mtime(path, timestamp)
    info = path.lstat()
    assert info.st_mtime == pytest.approx(timestamp, abs=0.001, rel=0)
    if creation and os.name == "nt":
        assert pruner._entry_timestamp(info) == pytest.approx(timestamp, abs=0.001, rel=0)
    return path


def old(path, *, creation=True):
    set_times(path, OLD, creation=creation)
    return path


def prune(root, **kwargs):
    return pruner.prune_temp_root(
        root, now=NOW, min_age_seconds=AGE, apply=kwargs.pop("apply", True), **kwargs,
    )


def runs(root, **kwargs):
    return pruner.prune_run_roots(
        root, now=NOW, min_age_seconds=AGE, apply=kwargs.pop("apply", True), **kwargs,
    )


@pytest.mark.parametrize("prefix", pruner.LEAK_PREFIXES, ids=pruner.LEAK_PREFIXES)
@pytest.mark.parametrize("kind", ["file", "directory"], ids=["file", "directory"])
def test_only_known_old_entries_are_removed_and_accounted(tmp_path, prefix, kind):
    target = tmp_path / (prefix + "old")
    if kind == "directory":
        target.mkdir()
        (target / "payload").write_bytes(b"1234")
    else:
        target.write_bytes(b"1234")
    old(target)
    unknown = tmp_path / "unrelated"
    unknown.write_bytes(b"keep")
    old(unknown)
    report = prune(tmp_path)
    assert not target.exists() and unknown.read_bytes() == b"keep"
    assert report["eligible"] == report["deleted"] == 1
    assert report["bytes"] == report["deleted_bytes"] == 4
    assert report["per_prefix"][prefix]["deleted"] == 1


@pytest.mark.parametrize("mtime", [NOW, NOW - AGE], ids=["young", "age-boundary"])
def test_young_entries_are_kept(tmp_path, mtime):
    target = tmp_path / "mao-remove-young.cmd"
    target.write_bytes(b"x")
    set_times(target, mtime)
    report = prune(tmp_path)
    assert target.exists() and report["kept_because_young"] == 1
    assert report["deleted"] == 0


def test_dry_run_reports_bytes_without_deleting(tmp_path):
    target = tmp_path / "mao-remove-old"
    target.mkdir()
    (target / "data").write_bytes(b"123")
    old(target)
    report = prune(tmp_path, apply=False)
    assert report["eligible"] == 1 and report["bytes"] == 3
    assert report["deleted"] == report["deleted_bytes"] == 0
    assert (target / "data").read_bytes() == b"123"


def test_locked_entry_is_counted_and_does_not_abort_other_entries(tmp_path, monkeypatch):
    locked = tmp_path / "mao-remove-locked.cmd"
    free = tmp_path / "mao-remove-free.cmd"
    for path in (locked, free):
        path.write_bytes(b"x")
        old(path)
    unlink = Path.unlink

    def refuse(path, *args, **kwargs):
        if path == locked:
            raise PermissionError("fake locked file")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    report = prune(tmp_path)
    assert locked.exists() and not free.exists()
    assert report["failed"] == report["per_prefix"]["mao-remove-"]["failed"] == 1
    assert report["deleted"] == 1


def link_to(link, target, kind):
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("junctions are Windows-only")
        done = subprocess.run(
            [str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "cmd.exe"),
             "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, **no_window_kwargs(),
        )
        if done.returncode:
            pytest.skip("this platform cannot create a junction")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this platform cannot create a directory symlink")


@pytest.mark.parametrize("kind", ["symlink", "junction"], ids=["symlink", "junction"])
def test_top_level_and_nested_links_never_delete_the_target(tmp_path, kind):
    root = tmp_path / "scratch"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"preserve")
    link_to(root / "mao-remove-link", outside, kind)
    directory = root / "mao-remove-old"
    directory.mkdir()
    link_to(directory / "nested-link", outside, kind)
    old(directory)
    report = prune(root)
    assert (outside / "sentinel").read_bytes() == b"preserve"
    assert report["skipped_links"] == 1
    assert report["deleted"] == 1 and not directory.exists()


def test_root_with_a_linked_ancestor_is_refused(tmp_path):
    target = tmp_path / "actual"
    target.mkdir()
    (target / "scratch").mkdir()
    link_to(tmp_path / "alias", target, "symlink")
    with pytest.raises(ValueError, match="linked ancestors"):
        prune(tmp_path / "alias" / "scratch")


def test_windows_reparse_attribute_and_junction_detection_are_guards(tmp_path, monkeypatch):
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    assert pruner._is_link(tmp_path, metadata)
    monkeypatch.setattr(Path, "is_junction", lambda _: True, raising=False)
    assert pruner._is_link(tmp_path, SimpleNamespace(st_mode=stat.S_IFDIR))


def test_time_budget_stops_before_deleting_and_during_a_tree(tmp_path, monkeypatch):
    target = tmp_path / "ns-e2e-old"
    target.mkdir()
    for index in range(5):
        (target / str(index)).write_bytes(b"x")
    old(target)
    report = prune(tmp_path, time_budget_seconds=0)
    assert report["budget_stopped"] and target.exists()
    clock = iter(range(10000))
    monkeypatch.setattr(pruner.time, "monotonic", lambda: next(clock))
    report = prune(tmp_path, time_budget_seconds=8)
    assert report["budget_stopped"] and report["deleted"] == 0
    assert target.exists()


def test_budget_can_stop_partial_deletion_and_accounts_for_removed_bytes(tmp_path, monkeypatch):
    target = tmp_path / "ns-logs-old"
    target.mkdir()
    for index in range(3):
        (target / str(index)).write_bytes(b"x")
    old(target)
    expired = False
    unlink = Path.unlink

    def remove_one(path, *args, **kwargs):
        nonlocal expired
        result = unlink(path, *args, **kwargs)
        expired = True
        return result

    monkeypatch.setattr(Path, "unlink", remove_one)
    monkeypatch.setattr(pruner.time, "monotonic", lambda: 10 if expired else 0)
    report = prune(tmp_path, time_budget_seconds=1)
    assert report["budget_stopped"] and target.exists()
    assert report["deleted"] == 0 and report["deleted_bytes"] == 1
    assert len(list(target.iterdir())) == 2


def make_run(parent, name, *, creation=True):
    child = parent / name
    base = child / "pytest-of-fake" / "pytest-0"
    test = base / "test-example"
    test.mkdir(parents=True)
    (test / "payload").write_bytes(b"123")
    for path in (test / "payload", test, base, base.parent, child):
        old(path, creation=creation)
    return child, base


def test_old_run_root_is_pruned_but_protected_and_young_roots_stay(tmp_path):
    expired, _ = make_run(tmp_path, "old")
    protected, _ = make_run(tmp_path, "protected")
    young, base = make_run(tmp_path, "young")
    set_times(base, NOW, creation=False)
    report = runs(tmp_path, protect=["protected"])
    assert not expired.exists() and protected.exists() and young.exists()
    assert report["deleted"] == 1 and report["deleted_bytes"] == 3
    assert report["kept_because_protected"] == report["kept_because_young"] == 1


def test_a_live_pytest_lock_below_two_levels_keeps_the_run(tmp_path):
    child, base = make_run(tmp_path, "locked")
    lock = base / ".lock"
    lock.write_text("123", encoding="utf-8")
    set_times(lock, NOW, creation=False)
    old(base)  # Only the lock is fresh, so this exercises the lock guard.
    report = runs(tmp_path)
    assert child.exists() and report["kept_because_active"] == 1


def test_gitdir_pointing_outside_keeps_a_registered_worktree(tmp_path):
    parent = tmp_path / "runs"
    parent.mkdir()
    child, _ = make_run(parent, "worktree")
    (child / ".git").write_text("gitdir: ../../external/worktrees/item\n", encoding="utf-8")
    old(child / ".git")
    old(child)
    report = runs(parent)
    assert child.exists() and report["kept_because_worktree"] == 1


@pytest.mark.parametrize("live", [False, True], ids=["stale-current", "live-current"])
def test_pytest_current_link_is_safe_and_live_target_keeps_run(tmp_path, live):
    child, base = make_run(tmp_path, "run")
    target = base / "test-example"  # Depth 3: beyond the shallow young guard.
    link_to(base.parent / "pytest-current", target, "symlink")
    current = base.parent / "pytest-current"
    # Leave link creation recent; only its modification time denotes activity.
    old(current, creation=False)
    old(base.parent)
    if live:
        set_times(target, NOW, creation=False)
    assert current.lstat().st_mtime == OLD
    assert target.stat().st_mtime == (NOW if live else OLD)
    report = runs(tmp_path)
    assert child.exists() is live
    assert report["deleted"] == (0 if live else 1)
    assert report["kept_because_young"] == 0
    assert report["kept_because_active"] == (1 if live else 0)


def test_containment_requires_exact_case_except_for_the_drive_letter():
    from pathlib import PurePosixPath, PureWindowsPath

    child = PureWindowsPath(r"E:\Runs\old")
    assert pruner._contains(child, PureWindowsPath(r"E:\Runs\old\x"))
    assert pruner._contains(child, PureWindowsPath(r"e:\Runs\old\x"))
    assert not pruner._contains(child, PureWindowsPath(r"E:\runs\old\x"))
    assert not pruner._contains(child, PureWindowsPath(r"E:\Runs\OLD"))
    assert not pruner._contains(child, PureWindowsPath(r"E:\Other"))
    share = PureWindowsPath(r"\\server\Share\Runs\old")
    assert pruner._contains(share, PureWindowsPath(r"\\server\Share\Runs\old\x"))
    assert not pruner._contains(share, PureWindowsPath(r"\\server\share\Runs\old\x"))
    assert not pruner._contains(share, PureWindowsPath(r"\\SERVER\Share\Runs\old\x"))
    posix = PurePosixPath("/runs/old")
    assert pruner._contains(posix, PurePosixPath("/runs/old/x"))
    assert not pruner._contains(posix, PurePosixPath("/Runs/old/x"))


def test_case_different_current_link_keeps_the_run_without_reading_the_target(
    tmp_path, monkeypatch,
):
    child, base = make_run(tmp_path, "run")
    target = base / "test-example"
    link_to(base.parent / "pytest-current", target, "symlink")
    old(base.parent / "pytest-current", creation=False)
    old(base.parent)
    # On a case-sensitive directory this spelling is a different tree, so it
    # must count as outside the run, and its metadata must never be read.
    outside = str(target).replace(child.name, child.name.upper())
    assert outside != str(target)
    monkeypatch.setattr(pruner, "_link_text", lambda path: outside)
    checked = []
    original = pruner._checked_info

    def recording(path, root, tick):
        checked.append(str(path))
        return original(path, root, tick)

    monkeypatch.setattr(pruner, "_checked_info", recording)
    report = runs(tmp_path)
    assert child.exists()
    assert report["kept_because_active"] == 1 and report["deleted"] == 0
    assert outside not in checked


def test_dangling_current_link_keeps_the_run_without_a_failure(tmp_path):
    child, base = make_run(tmp_path, "run")
    gone = base / "test-gone"
    gone.mkdir()
    link_to(base.parent / "pytest-current", gone, "symlink")
    gone.rmdir()
    old(base.parent / "pytest-current", creation=False)
    old(base)
    old(base.parent)
    report = runs(tmp_path)
    # Absence cannot prove the run is idle (pytest may be recreating the
    # target), so the run stays, and it is not reported as a failure.
    assert child.exists()
    assert report["kept_because_active"] == 1
    assert report["deleted"] == 0 and report["failed"] == 0


def test_run_root_uses_old_activity_even_when_creation_is_recent(tmp_path, monkeypatch):
    child, _ = make_run(tmp_path, "recent-creation", creation=False)
    if os.name == "nt":
        assert pruner._entry_timestamp(child.lstat()) >= NOW - AGE

    def refuse_creation_age(info):
        raise AssertionError("run-root guards must not consult prefix creation age")

    monkeypatch.setattr(pruner, "_entry_timestamp", refuse_creation_age)
    report = runs(tmp_path)
    assert not child.exists() and report["deleted"] == 1
    assert report["kept_because_young"] == report["kept_because_active"] == 0


@pytest.mark.parametrize("kind", ["file", "directory"], ids=["file", "directory"])
@pytest.mark.parametrize("recent", ["creation", "mtime"], ids=["recent-creation", "recent-mtime"])
def test_real_prefix_creation_age_is_independent_of_mtime(tmp_path, kind, recent):
    target = tmp_path / "ns-logs-age"
    if kind == "directory":
        target.mkdir()
        (target / "payload").write_bytes(b"x")
    else:
        target.write_bytes(b"x")
    if os.name != "nt" and not hasattr(target.lstat(), "st_birthtime"):
        pytest.skip("this platform uses mtime because creation time is unavailable")
    if recent == "creation":
        old(target, creation=False)
        assert pruner._entry_timestamp(target.lstat()) >= NOW - AGE
    else:
        old(target)
        set_times(target, NOW, creation=False)
        assert pruner._entry_timestamp(target.lstat()) == OLD
    report = prune(tmp_path)
    assert target.exists() is (recent == "creation")
    assert report["kept_because_young"] == int(recent == "creation")
    assert report["deleted"] == int(recent == "mtime")


def test_run_root_dry_run_deletes_nothing(tmp_path):
    child, _ = make_run(tmp_path, "run")
    report = runs(tmp_path, apply=False)
    assert child.exists() and report["eligible"] == 1 and report["deleted"] == 0


def test_caller_cannot_widen_prefixes(tmp_path):
    with pytest.raises(ValueError, match="known leak"):
        prune(tmp_path, prefixes=("",))


def test_cli_refuses_an_unset_root(monkeypatch, capsys):
    monkeypatch.delenv("MODELQUAD_TEMP_ROOT", raising=False)
    with pytest.raises(SystemExit) as exc:
        pruner.main([])
    assert exc.value.code == 2
    assert "no ambient temp fallback" in capsys.readouterr().err


def test_cli_uses_explicit_or_opted_in_root_and_prints_apply_command(tmp_path, monkeypatch, capsys):
    target = tmp_path / "mao-remove-old.cmd"
    target.write_bytes(b"x")
    old(target)
    monkeypatch.setenv("MODELQUAD_TEMP_ROOT", str(tmp_path))
    monkeypatch.setattr(pruner.time, "time", lambda: NOW)
    assert pruner.main([]) == 0
    output = capsys.readouterr().out
    assert target.exists() and "--apply" in output and "--root" in output
    assert str(tmp_path) in output
    assert pruner.main(["--root", str(tmp_path), "--apply"]) == 0
    assert not target.exists()


@pytest.mark.parametrize("supported", [False, True], ids=["windows-311", "nofollow-supported"])
def test_mtime_helper_uses_only_supported_nofollow(tmp_path, monkeypatch, supported):
    target = tmp_path / "ordinary-file"
    target.write_bytes(b"x")
    utime = os.utime
    calls = []

    def portable_utime(path, times, **kwargs):
        calls.append(kwargs)
        # This regular fixture is safe to follow even on Windows 3.11.
        return utime(path, times)

    monkeypatch.setattr(os, "utime", portable_utime)
    monkeypatch.setattr(os, "supports_follow_symlinks", {portable_utime} if supported else set())
    set_mtime(target, OLD)
    assert calls == ([{"follow_symlinks": False}] if supported else [{}])
    assert target.stat().st_mtime == OLD


@pytest.mark.parametrize(
    "windows,birthtime,ctime,mtime,expected",
    [(True, OLD, NOW, NOW, OLD), (True, None, OLD, NOW, OLD),
     (False, None, NOW, OLD, OLD), (False, OLD, NOW, NOW, OLD)],
    ids=["windows-birthtime", "windows-311-ctime", "mtime-fallback", "posix-birthtime"],
)
def test_prefix_age_uses_creation_time_where_available(
    monkeypatch, windows, birthtime, ctime, mtime, expected,
):
    monkeypatch.setattr(pruner, "_WINDOWS", windows)
    info = SimpleNamespace(st_ctime=ctime, st_mtime=mtime)
    if birthtime is not None:
        info.st_birthtime = birthtime
    assert pruner._entry_timestamp(info) == expected


def test_recent_creation_keeps_an_entry_with_old_mtime(tmp_path, monkeypatch):
    target = tmp_path / "mao-remove-new.cmd"
    target.write_bytes(b"x")
    old(target)
    checked_info = pruner._checked_info

    def recent_creation(path, root, tick):
        info = checked_info(path, root, tick)
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_birthtime"] = NOW
        return SimpleNamespace(**fields)

    monkeypatch.setattr(pruner, "_checked_info", recent_creation)
    report = prune(tmp_path)
    assert target.exists() and report["kept_because_young"] == 1
    assert report["deleted"] == 0


@pytest.mark.parametrize("birthtime", [False, True], ids=["windows-311-ctime", "birthtime"])
def test_partial_directory_deletion_resumes_using_creation_age(tmp_path, monkeypatch, birthtime):
    target = tmp_path / "ns-logs-partial"
    target.mkdir()
    for index in range(3):
        (target / str(index)).write_bytes(b"x")
    old(target)
    checked_info = pruner._checked_info

    def creation_metadata(path, root, tick):
        info = checked_info(path, root, tick)
        if path != target:
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_ctime"] = OLD
        if birthtime:
            fields["st_birthtime"] = OLD
        else:
            fields.pop("st_birthtime", None)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(pruner, "_checked_info", creation_metadata)
    monkeypatch.setattr(pruner, "_WINDOWS", True)
    expired = False
    unlink = Path.unlink

    def remove_one(path, *args, **kwargs):
        nonlocal expired
        result = unlink(path, *args, **kwargs)
        set_times(target, NOW, creation=False)  # A partial pass changed the parent's mtime.
        expired = True
        return result

    monkeypatch.setattr(Path, "unlink", remove_one)
    monkeypatch.setattr(pruner.time, "monotonic", lambda: 10 if expired else 0)
    first = prune(tmp_path, time_budget_seconds=1)
    assert first["budget_stopped"] and first["deleted_bytes"] == 1
    assert target.stat().st_mtime == NOW
    monkeypatch.setattr(Path, "unlink", unlink)
    second = prune(tmp_path)
    assert second["deleted"] == 1 and second["kept_because_young"] == 0
    assert second["deleted_bytes"] == 2 and not target.exists()


@pytest.mark.parametrize("supported", [False, True], ids=["windows-311", "nofollow-supported"])
def test_readonly_file_is_cleared_and_retried_once(tmp_path, monkeypatch, supported):
    directory = tmp_path / "ns-e2e-readonly"
    directory.mkdir()
    target = directory / "readonly-object"
    target.write_bytes(b"123")
    target.chmod(stat.S_IRUSR)
    old(directory)
    unlink, chmod = Path.unlink, os.chmod
    attempts, changes = [], []

    def reject_readonly(path, *args, **kwargs):
        if path == target:
            attempts.append(path)
            if not path.lstat().st_mode & stat.S_IWUSR:
                raise PermissionError("read-only fixture")
        return unlink(path, *args, **kwargs)

    def portable_chmod(path, mode, **kwargs):
        changes.append(kwargs)
        return chmod(path, mode)

    monkeypatch.setattr(Path, "unlink", reject_readonly)
    monkeypatch.setattr(os, "chmod", portable_chmod)
    monkeypatch.setattr(os, "supports_follow_symlinks", {portable_chmod} if supported else set())
    try:
        report = prune(tmp_path)
        assert report["deleted"] == 1 and report["deleted_bytes"] == 3
        assert report["failed"] == 0 and not directory.exists()
        assert attempts == [target, target]
        assert changes == ([{"follow_symlinks": False}] if supported else [{}])
    finally:
        if target.exists():
            chmod(target, stat.S_IRUSR | stat.S_IWUSR)


def test_locked_readonly_file_is_retried_only_once(tmp_path, monkeypatch):
    target = tmp_path / "mao-remove-locked-readonly.cmd"
    target.write_bytes(b"x")
    target.chmod(stat.S_IRUSR)
    old(target)
    attempts = []

    def locked(path, *args, **kwargs):
        attempts.append(path)
        raise PermissionError("still locked")

    monkeypatch.setattr(Path, "unlink", locked)
    try:
        report = prune(tmp_path)
        assert attempts == [target, target]
        assert target.exists() and report["failed"] == 1
    finally:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.parametrize("mode", ["prefix", "runs"], ids=["prefix", "run-roots"])
@pytest.mark.parametrize("marker", ["git-file", "linked-gitdir", "git-worktrees"],
                         ids=["external-git-file", "linked-gitdir", "git-directory-with-worktrees"])
def test_git_worktree_guards_apply_in_both_modes(tmp_path, mode, marker):
    parent = tmp_path / "roots"
    parent.mkdir()
    child, _ = make_run(parent, "mq-wt-old")
    if marker == "git-file":
        (child / ".git").write_text("gitdir: ../../external/worktrees/item\n", encoding="utf-8")
    elif marker == "linked-gitdir":
        outside = tmp_path / "outside"
        outside.mkdir()
        link_to(child / "linked-gitdir", outside, "symlink")
        # Back-dated, so the young guard cannot answer first whatever the
        # directory listing order.
        old(child / "linked-gitdir", creation=False)
        (child / ".git").write_text("gitdir: linked-gitdir\n", encoding="utf-8")
    else:
        (child / ".git" / "worktrees" / "item").mkdir(parents=True)
    for node, metadata, linked, depth in pruner._walk(child, parent, lambda: None):
        if not linked:
            old(node)
    report = prune(parent) if mode == "prefix" else runs(parent)
    assert child.exists() and report["kept_because_worktree"] == 1
    assert report["deleted"] == 0 and report["failed"] == 0


@pytest.mark.parametrize("windows", [False, True], ids=["case-sensitive", "windows-case-insensitive"])
def test_protected_run_names_follow_platform_case_rules(tmp_path, monkeypatch, windows):
    child, _ = make_run(tmp_path, "Protected-Run")
    monkeypatch.setattr(pruner, "_WINDOWS", windows)
    report = runs(tmp_path, protect=["protected-run"])
    assert child.exists() is windows
    assert report["kept_because_protected"] == int(windows)
    assert report["deleted"] == int(not windows)


@pytest.mark.parametrize("days", ["-1", "0", "0.5", "nan", "inf"],
                         ids=["negative", "zero", "below-one", "nan", "infinite"])
def test_cli_refuses_days_below_one_or_nonfinite(tmp_path, capsys, days):
    with pytest.raises(SystemExit) as exc:
        pruner.main(["--root", str(tmp_path), "--days", days])
    assert exc.value.code == 2
    assert "finite and at least 1" in capsys.readouterr().err
