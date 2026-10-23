# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Synthetic files are never executed. Each parametrized ID states its platform.

Unmarked IDs run on Windows and POSIX. win_only/posix_only IDs explicitly
skip on the other platform; alias creation failures report their own reason.
All fixture writes and aliases remain under tmp_path.
"""

from __future__ import annotations

import ast
import ctypes
import ntpath
import os
import sys
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from modelquad import executable_resolution as resolver


win_only = pytest.mark.skipif(os.name != "nt", reason="Windows path/extension/API semantics only")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX execute-bit semantics only")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    current = tmp_path / "process cwd"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return current


def _file(directory, name=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name if name is not None else _binary_name())
    path.write_bytes(b"synthetic fixture - never execute\n")
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _binary_name():
    return "fixture.exe" if os.name == "nt" else "fixture"


def _assert_admitted(result, path, source="path", kind="binary"):
    assert result.path == os.path.realpath(path)
    assert result.source == source
    assert result.kind == kind
    assert result.refusal is None


@pytest.mark.parametrize(
    "case",
    ["empty", "dot", "relative",
     pytest.param("drive-relative", marks=win_only),
     pytest.param("root-relative", marks=win_only),
     "quoted", pytest.param("device", marks=win_only),
     "absolute-cwd", "repository-bin", "sibling-prefix"],
    ids=["empty", "dot", "relative", "drive-relative", "root-relative",
         "quoted", "device", "absolute-cwd", "repository-bin", "sibling-prefix"],
)
def test_resolver_excludes_cwd_and_nonabsolute_path_entries(case, tmp_path, monkeypatch, isolated):
    repo = tmp_path / "repo"
    directory = tmp_path / "safe install"
    reasons = {
        "empty": "path_empty", "dot": "path_relative", "relative": "path_relative",
        "drive-relative": "path_drive_relative", "root-relative": "path_root_relative",
        "quoted": "path_quoted", "device": "path_device", "absolute-cwd": "excluded_cwd",
        "repository-bin": "excluded_root",
    }
    if case in {"empty", "dot", "absolute-cwd"}:
        directory = isolated
        entry = {"empty": "", "dot": ".", "absolute-cwd": str(directory)}[case]
    elif case in {"relative", "drive-relative"}:
        directory = isolated / "relative"
        entry = "relative" if case == "relative" else ntpath.splitdrive(str(directory))[0] + "relative"
    elif case == "root-relative":
        entry = ntpath.splitdrive(str(directory))[1]
    elif case == "quoted":
        entry = '"' + str(directory) + '"'
    elif case == "device":
        entry = "\\\\?\\" + str(directory)
    elif case == "repository-bin":
        directory = repo / "bin"
        entry = str(directory)
    elif case == "sibling-prefix":
        directory = tmp_path / "repo2"
        entry = str(directory)
    path = _file(directory)
    result = resolver.resolve_executable("fixture", path_env=entry, exclude_roots=(repo,))
    if case == "sibling-prefix":
        _assert_admitted(result, path)
    else:
        assert result.path is None
        assert result.refusal == reasons[case]
        assert result.refusal in resolver.SECURITY_REFUSALS
        assert reasons[case] in result.rejected
        assert str(path) not in repr(result)


@pytest.mark.parametrize("location", ["ancestor", "drive-root"], ids=["ancestor", "drive-root"])
def test_cwd_descendants_and_drive_root_stay_admitted(location, tmp_path, monkeypatch, isolated):
    path = _file(tmp_path / "profile" / "Local AppData" / "Codex" / "bin")
    if location == "ancestor":
        monkeypatch.chdir(tmp_path / "profile")
    else:
        root = Path(tmp_path.anchor)
        if not root.is_dir():
            pytest.skip("This filesystem has no accessible drive/root directory")
        monkeypatch.chdir(root)
    _assert_admitted(resolver.resolve_executable("fixture", path_env=str(path.parent)), path)
    _assert_admitted(resolver.admit_executable(path), path, source="explicit")


def test_cwd_argument_adds_to_process_cwd(tmp_path, isolated):
    parent_file = _file(isolated)
    child_file = _file(tmp_path / "child" / "bin")
    result = resolver.resolve_executable("fixture", path_env=str(parent_file.parent), cwd=child_file.parent.parent)
    assert result.refusal == "excluded_cwd"
    result = resolver.resolve_executable("fixture", path_env=str(child_file.parent), cwd=child_file.parent.parent)
    assert result.refusal == "excluded_root"
    assert resolver.admit_executable(parent_file, cwd=child_file.parent.parent).refusal == "excluded_cwd"
    assert resolver.admit_executable(child_file, cwd=child_file.parent.parent).refusal == "excluded_root"


@pytest.mark.parametrize(
    "alias_kind", [pytest.param("junction", marks=win_only), "symlink", "file-symlink"],
    ids=["junction", "symlink", "file-symlink"],
)
def test_resolver_blocks_repository_path_aliases(alias_kind, tmp_path, isolated):
    repo = tmp_path / "repo with spaces שלום"
    target = _file(repo / "bin with spaces שלום")
    install = tmp_path / "install with spaces שלום"
    install.mkdir()
    alias = install / (target.name if alias_kind == "file-symlink" else "alias שלום")
    try:
        try:
            if alias_kind == "junction":
                import _winapi

                _winapi.CreateJunction(str(target.parent), str(alias))
            else:
                alias.symlink_to(target if alias_kind == "file-symlink" else target.parent,
                                 target_is_directory=alias_kind != "file-symlink")
        except (OSError, NotImplementedError, AttributeError) as exc:
            pytest.skip(f"{alias_kind} fixture unavailable: {type(exc).__name__}")
        search = install if alias_kind == "file-symlink" else alias
        result = resolver.resolve_executable("fixture", path_env=str(search), exclude_roots=(repo,))
        assert result.path is None
        assert result.refusal == "excluded_root"
        candidate = alias if alias_kind == "file-symlink" else alias / target.name
        assert resolver.admit_executable(candidate, exclude_roots=(repo,)).refusal == "excluded_root"
    finally:
        if alias_kind == "junction" and alias.exists():
            alias.rmdir()  # Remove only the junction, never its target tree.
        elif alias.is_symlink():
            alias.unlink()


@pytest.mark.parametrize(
    "case", ["absolute-order", "spaces-hebrew",
             pytest.param("vbs-ignored", marks=win_only),
             pytest.param("extensionless-ignored", marks=win_only),
             pytest.param("explicit-extension", marks=win_only),
             pytest.param("pathext-order", marks=win_only),
             pytest.param("path-before-pathext", marks=win_only)],
    ids=["absolute-order", "spaces-hebrew", "vbs-ignored", "extensionless-ignored",
         "explicit-extension", "pathext-order", "path-before-pathext"],
)
def test_resolver_preserves_absolute_path_order_and_extensions(case, tmp_path, monkeypatch, isolated):
    first = tmp_path / "install one שלום"
    second = tmp_path / "install two שלום"
    chosen = _file(first)
    _file(second)
    search = os.pathsep.join((str(first), str(second)))
    name = "fixture"
    kwargs = {}
    if case == "spaces-hebrew":
        chosen = _file(first, "כלי עם רווח.exe" if os.name == "nt" else "כלי עם רווח")
        name = chosen.name
    elif case == "vbs-ignored":
        chosen.unlink()
        _file(first, "fixture.vbs")
        chosen = second / "fixture.exe"
        kwargs["pathext"] = ".VBS;.JS;.EXE"
        assert resolver.resolve_executable("fixture.vbs", path_env=search).refusal == "extension_not_allowed"
    elif case == "extensionless-ignored":
        chosen.unlink()
        extensionless = _file(first, "fixture")
        chosen = second / "fixture.exe"
        kwargs["pathext"] = ";.EXE"
        assert resolver.admit_executable(extensionless).refusal == "extension_not_allowed"
    elif case == "explicit-extension":
        name = "fixture.exe"
        chosen.unlink()
        decoy = _file(first, "fixture.exe.exe")
        assert resolver.resolve_executable(name, path_env=str(first)).refusal == "not_found"
        decoy.unlink()
        chosen = _file(first)
    elif case == "pathext-order":
        chosen = _file(first, "fixture.com")
        kwargs["pathext"] = ".COM;.EXE"
        _assert_admitted(resolver.resolve_executable(name, path_env=search, pathext=".EXE;.COM"), first / "fixture.exe")
    elif case == "path-before-pathext":
        chosen.unlink()
        chosen = _file(first, "fixture.exe")
        _file(second, "fixture.com")
        kwargs["pathext"] = ".COM;.EXE"
    _assert_admitted(resolver.resolve_executable(name, path_env=search, **kwargs), chosen)
    monkeypatch.setenv("PATH", search)
    assert resolver.safe_which(name, **kwargs) == os.path.realpath(chosen)


@win_only
@pytest.mark.parametrize("extension", [".bat", ".cmd"], ids=["bat", "cmd"])
def test_batch_requires_opt_in(extension, tmp_path, monkeypatch, isolated):
    path = _file(tmp_path / "batch install", "fixture" + extension)
    monkeypatch.setenv("PATH", str(path.parent))
    refused = resolver.resolve_executable("fixture")
    assert refused.path is None
    assert refused.kind == "batch"
    assert refused.refusal == "batch_script"
    assert resolver.admit_executable(path).refusal == "batch_script"
    _assert_admitted(resolver.resolve_executable("fixture", allow_batch=True), path, kind="batch")
    _assert_admitted(resolver.admit_executable(path, allow_batch=True), path, "explicit", "batch")
    with pytest.raises(resolver.UnsafeExecutable, match="^batch_script$"):
        resolver.prepare_launch([str(path), "literal & arg"])
    assert resolver.prepare_launch([str(path), "literal & arg"], allow_batch=True) == [os.path.realpath(path), "literal & arg"]


@pytest.mark.parametrize(
    "case", ["absent-rejected", "found-rejected", "found-excluded", "first-reason", "later-safe"],
    ids=["absent-rejected", "found-rejected", "found-excluded", "first-reason", "later-safe"],
)
def test_refusal_precedence_not_found_vs_security(case, tmp_path, isolated):
    repo = tmp_path / "repo"
    search = os.pathsep.join(("", str(repo / "bin")))
    expected = "not_found"
    if case in {"found-rejected", "first-reason", "later-safe"}:
        _file(isolated)
        expected = "path_empty"
    if case in {"found-excluded", "first-reason"}:
        _file(repo / "bin")
        if case == "found-excluded":
            expected = "excluded_root"
    if case == "later-safe":
        safe = _file(tmp_path / "install")
        search += os.pathsep + str(safe.parent)
    result = resolver.resolve_executable("fixture", path_env=search, exclude_roots=(repo,))
    if case == "later-safe":
        _assert_admitted(result, safe)
        assert result.rejected == ("path_empty",)
    else:
        assert result.path is None
        assert result.refusal == expected
        assert (result.refusal in resolver.SECURITY_REFUSALS) == (expected != "not_found")


@pytest.mark.parametrize(
    "case", ["relative", pytest.param("root-relative", marks=win_only), "missing", "directory", "excluded", "ok"],
    ids=["relative", "root-relative", "missing", "directory", "excluded", "ok"],
)
def test_admit_executable_rules(case, tmp_path, isolated):
    path = _file(tmp_path / "install")
    roots = ()
    expected = {"relative": "path_relative", "root-relative": "path_root_relative",
                "missing": "not_found", "directory": "not_regular_file", "excluded": "excluded_root"}
    if case == "relative":
        path = Path("relative") / path.name
    elif case == "root-relative":
        path = ntpath.splitdrive(str(path))[1]
    elif case == "missing":
        path.unlink()
    elif case == "directory":
        path.unlink()
        path.mkdir()
    elif case == "excluded":
        roots = (path.parent,)
    result = resolver.admit_executable(path, exclude_roots=roots)
    assert result.source == "explicit"
    if case == "ok":
        _assert_admitted(result, path, "explicit")
    else:
        assert result.path is None
        assert result.refusal == expected[case]


@win_only
@pytest.mark.parametrize(
    "name", ["tasklist", "taskkill.exe", "schtasks", "CMD.EXE", "ping"],
    ids=["tasklist", "taskkill-exe", "schtasks", "cmd-exe", "ping"],
)
def test_system_utilities_ignore_path_and_environment_overrides(name, tmp_path, monkeypatch, isolated):
    directory = resolver._system_directory()
    assert directory is not None
    utility = name.lower().removesuffix(".exe") + ".exe"
    poison = _file(tmp_path / "poison", utility)
    monkeypatch.setenv("PATH", str(poison.parent))
    monkeypatch.setenv("SystemRoot", str(poison.parent))
    monkeypatch.setenv("ComSpec", str(poison))
    _assert_admitted(resolver.system_executable(name), Path(directory) / utility, "system")
    monkeypatch.setattr(resolver, "_system_directory", lambda: None)
    failed = resolver.system_executable(name)
    assert failed.path is None
    assert failed.refusal == "system_api_failed"
    assert failed.source == "system"


@pytest.mark.parametrize(
    "case", ["relative-sep", pytest.param("drive-relative", marks=win_only),
             pytest.param("root-relative", marks=win_only), "excluded-absolute", "missing",
             pytest.param("batch", marks=win_only)],
    ids=["relative-sep", "drive-relative", "root-relative", "excluded-absolute", "missing", "batch"],
)
def test_prepare_launch_rejects_unresolved_argv(case, tmp_path, monkeypatch, isolated):
    path = _file(tmp_path / "install")
    roots = ()
    expected = {"relative-sep": "path_relative", "drive-relative": "path_drive_relative",
                "root-relative": "path_root_relative", "excluded-absolute": "excluded_root",
                "missing": "not_found", "batch": "batch_script"}
    first = str(path)
    if case == "relative-sep":
        first = os.path.join("relative", path.name)
    elif case == "drive-relative":
        first = ntpath.splitdrive(str(path))[0] + path.name
    elif case == "root-relative":
        first = ntpath.splitdrive(str(path))[1]
    elif case == "excluded-absolute":
        roots = (path.parent,)
    elif case == "missing":
        first = "absent"
    elif case == "batch":
        first = str(_file(path.parent, "fixture.cmd"))
    argv = [first, "private path שלום", "", "--", "&", '"quoted"']
    with pytest.raises(resolver.UnsafeExecutable) as caught:
        resolver.prepare_launch(argv, exclude_roots=roots)
    assert caught.value.reason == expected[case]
    assert str(caught.value) == expected[case]
    assert caught.value.args == (expected[case],)
    assert first not in str(caught.value)
    assert argv[1] not in str(caught.value)


def test_prepare_launch_preserves_arguments(tmp_path, monkeypatch, isolated):
    path = _file(tmp_path / "safe install שלום")
    monkeypatch.setenv("PATH", str(path.parent))
    arguments = ["", "--", "שלום with spaces", "& | > % !", '"literal quotes"']
    for name in (str(path), "fixture"):
        original = [name, *arguments]
        assert resolver.prepare_launch(original) == [os.path.realpath(path), *arguments]
        assert original == [name, *arguments]
    with pytest.raises(resolver.UnsafeExecutable, match="^empty_argv$"):
        resolver.prepare_launch([])


def test_safe_child_env(tmp_path, isolated):
    one = tmp_path / "absolute one שלום"
    two = tmp_path / "absolute two"
    entries = ["", ".", "relative", str(one), '"' + str(one) + '"', str(two)]
    if os.name == "nt":
        entries += ["C:tools", "\\tools", "\\\\?\\C:\\tools", "\\\\.\\C:\\tools"]
    path_key = "Path" if os.name == "nt" else "PATH"
    env = {path_key: os.pathsep.join(entries), "KEEP": "literal שלום", "NoDefaultCurrentDirectoryInExePath": "0"}
    original = dict(env)
    safe = resolver.safe_child_env(env)
    assert safe is not env
    assert env == original
    assert safe[path_key] == os.pathsep.join((str(one), str(two)))
    assert safe["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert safe["KEEP"] == env["KEEP"]
    assert resolver.safe_child_env({"KEEP": "x"}) == {"KEEP": "x", "NoDefaultCurrentDirectoryInExePath": "1"}


def test_module_never_uses_shutil_or_spawns():
    tree = ast.parse(Path(resolver.__file__).read_text(encoding="utf-8"))
    forbidden_modules = {"shutil", "subprocess"}
    aliases = {"os": "os", "_winapi": "_winapi"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                assert imported.name.split(".")[0] not in forbidden_modules
                aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_modules
            for imported in node.names:
                aliases[imported.asname or imported.name] = f"{node.module}.{imported.name}"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            called = aliases.get(node.func.value.id, node.func.value.id) + "." + node.func.attr
        elif isinstance(node.func, ast.Name):
            called = aliases.get(node.func.id, node.func.id)
        else:
            continue
        assert called not in {"os.system", "os.popen", "os.startfile", "_winapi.CreateProcess"}
        assert not called.startswith(("os.spawn", "os.exec"))


def test_import_does_not_observe_environment_or_filesystem(monkeypatch):
    source = Path(resolver.__file__).read_text(encoding="utf-8")
    code = compile(source, resolver.__file__, "exec")
    module = types.ModuleType("resolver_import_boundary_fixture")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def forbidden(*args, **kwargs):
        pytest.fail("Import must not inspect CWD, files, environment, or system APIs")

    monkeypatch.setattr(os, "getcwd", forbidden)
    monkeypatch.setattr(os, "stat", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)
    exec(code, module.__dict__)
    assert module.Resolution.__dataclass_params__.frozen


@pytest.mark.parametrize(
    "operation", ["resolve", "admit", "launch"], ids=["resolve", "admit", "launch"],
)
def test_unreadable_process_cwd_refuses(operation, tmp_path, monkeypatch, isolated):
    path = _file(tmp_path / "install")

    def fail():
        raise OSError("private cwd must not leak")

    monkeypatch.setattr(resolver.os, "getcwd", fail)
    if operation == "launch":
        with pytest.raises(resolver.UnsafeExecutable, match="^cwd_unknown$"):
            resolver.prepare_launch([str(path)])
    else:
        result = (resolver.resolve_executable("fixture", path_env=str(path.parent))
                  if operation == "resolve" else resolver.admit_executable(path))
        assert result.refusal == "cwd_unknown"
        assert result.path is None


@pytest.mark.parametrize(
    "name", ["relative/fixture", "relative\\fixture", "C:fixture", "fixture:stream", ""],
    ids=["slash", "backslash", "drive", "colon", "empty"],
)
def test_resolve_requires_bare_name(name, isolated):
    assert resolver.resolve_executable(name).refusal == "not_bare_name"


@win_only
@pytest.mark.parametrize(
    "name", ["fixture.vbs", "fixture.js", "fixture", "fixture.exe.", "fixture.exe "],
    ids=["vbs", "js", "extensionless", "trailing-dot", "trailing-space"],
)
def test_windows_explicit_extensions_are_restricted(name, tmp_path, isolated):
    # Do not create trailing-dot/space files: Win32 normalizes those names.
    path = tmp_path / "install" / name
    if not name.endswith((".", " ")):
        _file(path.parent, name)
    assert resolver.admit_executable(path).refusal == "extension_not_allowed"
    if name != "fixture":
        assert resolver.resolve_executable(name, path_env=str(path.parent)).refusal == "extension_not_allowed"


@posix_only
def test_posix_requires_execute_bit_and_accepts_extensions(tmp_path, isolated):
    path = _file(tmp_path / "install", "fixture.vbs")
    _assert_admitted(resolver.admit_executable(path), path, "explicit")
    path.chmod(0o600)
    assert resolver.admit_executable(path).refusal == "not_executable"
    assert resolver.resolve_executable(path.name, path_env=str(path.parent)).refusal == "not_executable"


def test_resolution_is_frozen_and_diagnostics_are_bounded(tmp_path, isolated):
    path = _file(tmp_path / "install")
    result = resolver.admit_executable(path)
    assert str(path) not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.path = "replacement"
    codes = [f"reason_{index}" for index in range(30)]
    bounded = resolver.Resolution(None, "path", None, "not_found", tuple(codes + codes))
    assert bounded.rejected == tuple(codes[:16])
    searched = resolver.resolve_executable("fixture", path_env=os.pathsep.join(["."] * 50))
    assert searched.rejected == ("path_relative",)
    assert isinstance(resolver.SECURITY_REFUSALS, frozenset)
    assert "not_found" not in resolver.SECURITY_REFUSALS
    assert str(resolver.UnsafeExecutable(str(path))) == "invalid_path"


@pytest.mark.parametrize(
    "case", ["admitted", "aliased-cwd", "file-target-cwd", "file-from-cwd", "aliased-root"],
    ids=["admitted", "aliased-cwd", "file-target-cwd", "file-from-cwd", "aliased-root"],
)
def test_symlink_admission_and_both_exclusion_sides(case, tmp_path, isolated):
    path = _file(isolated if case in {"aliased-cwd", "file-target-cwd"} else tmp_path / "install")
    links = isolated if case == "file-from-cwd" else tmp_path / "links"
    links.mkdir(exist_ok=True)
    file_link = case in {"file-target-cwd", "file-from-cwd"}
    alias = links / (path.name if file_link else "directory alias")
    try:
        try:
            alias.symlink_to(path if file_link else path.parent, target_is_directory=not file_link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink fixture unavailable: {type(exc).__name__}")
        search = links if file_link else alias
        roots = (alias,) if case == "aliased-root" else ()
        result = resolver.resolve_executable("fixture", path_env=str(search), exclude_roots=roots)
        if case == "admitted":
            _assert_admitted(result, path)
        else:
            assert result.path is None
            assert result.refusal == ("excluded_root" if case == "aliased-root" else "excluded_cwd")
    finally:
        if alias.is_symlink():
            alias.unlink()


@pytest.mark.parametrize(
    "name", ["git", "cmd.cmd", "relative/tasklist", "tasklist.exe.exe"],
    ids=["git", "batch", "separator", "double-extension"],
)
def test_system_rejects_other_utilities(name):
    assert resolver.system_executable(name).refusal == "not_a_system_utility"


@posix_only
def test_system_utility_unsupported_on_posix():
    assert resolver.system_executable("tasklist").refusal == "unsupported_platform"


@win_only
@pytest.mark.parametrize(
    "case", ["zero", "retry", "retry-fails", "retry-still-small"],
    ids=["zero", "retry", "retry-fails", "retry-still-small"],
)
def test_system_directory_api_declares_types_and_retries(case, tmp_path, monkeypatch):
    target = str(tmp_path / "synthetic system directory")
    sizes = []

    class GetDirectory:
        def __call__(self, buffer, size):
            sizes.append(size)
            if case == "zero" or (case == "retry-fails" and len(sizes) == 2):
                return 0
            if len(sizes) == 1 or case == "retry-still-small":
                return 500
            buffer.value = target
            return len(target)

    get_directory = GetDirectory()

    class Kernel:
        GetSystemDirectoryW = get_directory

    def load(name, *, use_last_error):
        assert name == "kernel32"
        assert use_last_error is True
        return Kernel()

    monkeypatch.setattr(ctypes, "WinDLL", load)
    assert resolver._system_directory() == (target if case == "retry" else None)
    assert sizes == ([260] if case == "zero" else [260, 500])
    from ctypes import wintypes

    assert get_directory.argtypes == (wintypes.LPWSTR, wintypes.UINT)
    assert get_directory.restype == wintypes.UINT


@win_only
@pytest.mark.parametrize(
    "case", ["relative", "root-relative", "missing-file", "directory-file"],
    ids=["relative", "root-relative", "missing-file", "directory-file"],
)
def test_system_directory_must_be_absolute_and_utility_regular(case, tmp_path, monkeypatch, isolated):
    directory = tmp_path / "system"
    directory.mkdir()
    if case == "relative":
        supplied = "relative"
    elif case == "root-relative":
        supplied = ntpath.splitdrive(str(directory))[1]
    else:
        supplied = str(directory)
    if case == "directory-file":
        (directory / "cmd.exe").mkdir()
    monkeypatch.setattr(resolver, "_system_directory", lambda: supplied)
    assert resolver.system_executable("cmd").refusal == "system_api_failed"


@win_only
def test_system_utility_resolves_when_the_process_cwd_is_the_system_directory(tmp_path, monkeypatch, isolated):
    # A scheduled task or service often starts in System32; the location comes
    # from the OS API, so the current directory does not exclude it.
    path = _file(tmp_path / "synthetic system", "cmd.exe")
    monkeypatch.setattr(resolver, "_system_directory", lambda: str(path.parent))
    monkeypatch.chdir(path.parent)
    _assert_admitted(resolver.system_executable("cmd"), path, "system")


@win_only
def test_default_pathext_is_used_only_when_unset(tmp_path, monkeypatch, isolated):
    path = _file(tmp_path / "install")
    monkeypatch.delenv("PATHEXT")
    _assert_admitted(resolver.resolve_executable("fixture", path_env=str(path.parent)), path)
    monkeypatch.setenv("PATHEXT", "")
    assert resolver.resolve_executable("fixture", path_env=str(path.parent)).refusal == "not_found"
    assert resolver.resolve_executable("fixture", path_env=str(path.parent), pathext="").refusal == "not_found"


@win_only
@pytest.mark.parametrize("spelling", ["tasklist", "taskkill", "schtasks", "cmd", "ping"],
                         ids=["tasklist", "taskkill", "schtasks", "cmd", "ping"])
def test_system_utilities_accept_bare_and_exe_names(spelling, tmp_path, monkeypatch, isolated):
    path = _file(tmp_path / "synthetic system", spelling + ".exe")
    monkeypatch.setattr(resolver, "_system_directory", lambda: str(path.parent))
    for name in (spelling, spelling + ".exe"):
        _assert_admitted(resolver.system_executable(name), path, "system")
