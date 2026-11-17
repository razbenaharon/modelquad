# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Resolver integration with synthetic files and recording fakes only.

No fake calls through to a process launch. Windows API/path cases skip with
an explicit reason on other platforms; all synthetic files live in tmp_path.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import time

import pytest

from modelquad import buildinfo, executable_resolution as resolver, procutil
from modelquad import gitmeta as changes


win_only = pytest.mark.skipif(os.name != "nt", reason="Windows system-directory and extended-path semantics only")
SHA = "0123456789abcdef0123456789abcdef01234567"


def _canonical(path):
    return os.path.normcase(os.path.realpath(path))


def _file(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"synthetic fixture - never execute\n")
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _git_file(directory):
    return _file(directory, "git.exe" if os.name == "nt" else "git")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    current = tmp_path / "process cwd"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    return current


def _record_git_popen(monkeypatch):
    calls = []

    class Child:
        returncode = 0

        def __init__(self):
            self.stdout = io.BytesIO(b"synthetic answer\n")
            self.stderr = io.BytesIO()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stdout.close()
            self.stderr.close()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pytest.fail("A completed recording fake must not need killing")

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Child()

    monkeypatch.setattr(changes.subprocess, "Popen", popen)
    return calls


@pytest.mark.parametrize(
    "case", ["root", "nested", "linked-worktree", "repository-bin", "process-cwd", "admitted",
             "submodule", "nested-repository", "submodule-refused"],
    ids=["root", "nested", "linked-worktree", "repository-bin", "process-cwd", "admitted",
         "submodule", "nested-repository", "submodule-refused"],
)
def test_git_from_the_repository_is_never_started(case, tmp_path, monkeypatch, isolated):
    repo = tmp_path / "repository"
    repo.mkdir()
    if case == "linked-worktree":
        (repo / ".git").write_text("gitdir: ../main/.git/worktrees/linked\n", encoding="utf-8")
    else:
        (repo / ".git").mkdir()
    nested_cases = {"nested", "linked-worktree", "submodule", "nested-repository", "submodule-refused"}
    directory = repo / "nested" if case in nested_cases else repo
    directory.mkdir(exist_ok=True)
    if case in {"submodule", "submodule-refused"}:
        (directory / ".git").write_text("gitdir: ../.git/modules/nested\n", encoding="utf-8")
    elif case == "nested-repository":
        (directory / ".git").mkdir()
    planted_repo = _git_file(repo / "tools" if case in {"submodule", "nested-repository", "submodule-refused"}
                             else repo / "bin" if case == "repository-bin" else repo)
    planted_cwd = _git_file(isolated)
    entries = [str(planted_repo.parent), str(isolated)]
    if case == "process-cwd":
        entries.reverse()
    admitted = (_git_file(tmp_path / "admitted install")
                if case in {"admitted", "submodule", "nested-repository"} else None)
    if admitted:
        entries.append(str(admitted.parent))
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    calls = _record_git_popen(monkeypatch)

    if admitted:
        assert changes._git(directory, ["rev-parse", "HEAD"], time.monotonic() + 60) == b"synthetic answer\n"
        (argv, kwargs), = calls
        assert _canonical(argv[0]) == _canonical(admitted)
        assert argv[-2:] == ["rev-parse", "HEAD"]
        assert kwargs["cwd"] == directory
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.PIPE
        for key, value in procutil.no_window_kwargs().items():
            assert kwargs[key] == value
    else:
        with pytest.raises(changes._Unavailable):
            changes._git(directory, ["rev-parse", "HEAD"], time.monotonic() + 60)
        assert calls == []
    for argv, _ in calls:
        assert _canonical(argv[0]) not in {_canonical(planted_repo), _canonical(planted_cwd)}


def test_git_excludes_all_enclosing_repositories(tmp_path, monkeypatch, isolated):
    outer = tmp_path / "outer repository"
    middle = outer / "submodule"
    inner = middle / "nested repository"
    directory = inner / "job folder"
    directory.mkdir(parents=True)
    (outer / ".git").mkdir()
    (middle / ".git").write_text("gitdir: ../.git/modules/submodule\n", encoding="utf-8")
    (inner / ".git").mkdir()
    planted = [_git_file(root / "tools") for root in (inner, middle, outer)]
    admitted = _git_file(tmp_path / "admitted install")
    monkeypatch.setenv("PATH", os.pathsep.join(str(path.parent) for path in (*planted, admitted)))
    calls = _record_git_popen(monkeypatch)

    assert changes._git(directory, ["rev-parse", "HEAD"], time.monotonic() + 60) == b"synthetic answer\n"

    (argv, kwargs), = calls
    assert _canonical(argv[0]) == _canonical(admitted)
    assert _canonical(argv[0]) not in {_canonical(path) for path in planted}
    assert kwargs["cwd"] == directory


@win_only
@pytest.mark.parametrize("case", ["drive", "unc"], ids=["drive", "unc"])
def test_git_excluded_roots_use_normal_win32_spelling(case, tmp_path, monkeypatch):
    # Do not open a UNC share: this fake supplies the .git entry observation.
    normal = os.path.realpath(tmp_path) if case == "drive" else "\\\\synthetic-host\\share\\repository"
    extended = "\\\\?\\" + normal if case == "drive" else "\\\\?\\UNC\\" + normal[2:]
    seen = []

    def entry(path):
        seen.append(str(path))
        if path == Path(normal) / ".git":
            return object()
        raise FileNotFoundError

    monkeypatch.setattr(Path, "lstat", entry)
    roots = changes._git_exclude_roots(Path(extended))
    assert tuple(map(str, roots)) == (normal,)
    assert seen == [str(ancestor / ".git") for ancestor in (Path(normal), *Path(normal).parents)]


@win_only
@pytest.mark.parametrize(
    "directory", [r"\\?\Volume{00000000-0000-0000-0000-000000000000}\repository",
                  r"\\?\GLOBALROOT\Device\HarddiskVolume1\repository",
                  r"\\.\C:\repository", "//?/Volume{00000000-0000-0000-0000-000000000000}/repository",
                  "//./C:/repository"],
    ids=["volume-guid", "globalroot", "device", "volume-guid-forward-slashes", "device-forward-slashes"],
)
@pytest.mark.parametrize("caller", ["changes", "buildinfo"], ids=["changes", "buildinfo"])
def test_unsupported_git_root_prefix_refuses_before_lookup(directory, caller, monkeypatch, isolated):
    def forbidden(*args, **kwargs):
        pytest.fail("An unsupported device root must not be inspected, resolved, or launched")

    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(changes, "resolve_executable", forbidden)
    monkeypatch.setattr(buildinfo, "resolve_executable", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    root = Path(directory)
    if caller == "changes":
        with pytest.raises(changes._Unavailable) as caught:
            changes._git(root, ["rev-parse", "HEAD"], time.monotonic() + 60)
        assert caught.value.args == ()
    else:
        assert buildinfo._git_head(root) is None


def test_git_exclusion_lookup_failure_is_unavailable(tmp_path, monkeypatch, isolated):
    calls = _record_git_popen(monkeypatch)

    def unreadable(path):
        raise PermissionError("private repository path")

    monkeypatch.setattr(Path, "lstat", unreadable)
    with pytest.raises(changes._Unavailable):
        changes._git(tmp_path, ["rev-parse", "HEAD"], time.monotonic() + 60)
    assert calls == []


@pytest.mark.parametrize(
    "case", ["refused", "admitted", "missing", "nested-checkout", "nested-checkout-refused",
             pytest.param("extended-prefix", marks=win_only)],
    ids=["refused", "admitted", "missing", "nested-checkout", "nested-checkout-refused", "extended-prefix"],
)
def test_buildinfo_git_is_resolved(case, tmp_path, monkeypatch, isolated):
    repo = tmp_path / "repository"
    repo.mkdir()
    root = repo
    if case in {"nested-checkout", "nested-checkout-refused"}:
        (repo / ".git").mkdir()
        root = repo / "checkout"
        root.mkdir()
        (root / ".git").mkdir()
    elif case == "extended-prefix":
        (repo / ".git").mkdir()
        root = Path("\\\\?\\" + os.path.realpath(repo))
    entries = []
    planted = []
    if case != "missing":
        planted = [_git_file(repo / "tools" if case in {"nested-checkout", "nested-checkout-refused"} else repo),
                   _git_file(isolated)]
        entries = [str(path.parent) for path in planted]
    admitted = (_git_file(tmp_path / "admitted install")
                if case in {"admitted", "nested-checkout", "extended-prefix"} else None)
    if admitted:
        entries.append(str(admitted.parent))
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=SHA + "\n", stderr="")

    monkeypatch.setattr(buildinfo.subprocess, "run", run)
    assert buildinfo._git_head(root) == (SHA if admitted else None)
    if admitted:
        (argv, kwargs), = calls
        assert _canonical(argv[0]) == _canonical(admitted)
        assert argv[1:] == ["-C", str(root), "rev-parse", "--verify", "HEAD"]
        assert kwargs == {
            "capture_output": True, "text": True, "timeout": buildinfo._GIT_TIMEOUT_SECONDS,
            "env": {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, "check": False,
            **procutil.no_window_kwargs(),
        }
    else:
        assert calls == []
    assert all(_canonical(argv[0]) not in {_canonical(path) for path in planted} for argv, _ in calls)


def test_buildinfo_git_exclusion_lookup_failure_is_unavailable(tmp_path, monkeypatch, isolated):
    def unreadable(path):
        raise PermissionError("private repository path")

    def forbidden(*args, **kwargs):
        pytest.fail("A failed root lookup must not resolve or launch git")

    monkeypatch.setattr(Path, "lstat", unreadable)
    monkeypatch.setattr(buildinfo, "resolve_executable", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert buildinfo._git_head(tmp_path) is None


@pytest.fixture
def synthetic_system(tmp_path, monkeypatch, isolated):
    directory = tmp_path / "synthetic system directory"
    for name in ("tasklist", "taskkill"):
        _file(directory, name + ".exe")
        _file(isolated, name + ".exe")
    monkeypatch.setenv("PATH", str(isolated))
    monkeypatch.setenv("SystemRoot", str(isolated))
    monkeypatch.setenv("ComSpec", str(isolated / "cmd.exe"))
    monkeypatch.setattr(resolver, "_system_directory", lambda: str(directory))
    return directory


@win_only
def test_taskkill_comes_from_the_system_directory(monkeypatch, synthetic_system):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout='"python.exe","4242","Console","1","10 K"\n')

    monkeypatch.setattr(procutil.subprocess, "run", run)

    def invoke():
        return procutil._taskkill_tree(4242)

    assert invoke() == "ok"
    (argv, kwargs), = calls
    assert _canonical(argv[0]) == _canonical(synthetic_system / "taskkill.exe")
    assert argv[1:] == ["/PID", "4242", "/T", "/F"]
    assert kwargs == {"capture_output": True, "timeout": 10.0, **procutil.no_window_kwargs()}

    # A process may itself start in the system directory; no re-admission.
    monkeypatch.chdir(synthetic_system)
    assert invoke() == "ok"
    assert _canonical(calls[-1][0][0]) == _canonical(synthetic_system / "taskkill.exe")
    monkeypatch.setattr(resolver, "_system_directory", lambda: None)
    count = len(calls)
    assert invoke() == "launch_error: UnsafeExecutable"
    assert len(calls) == count
