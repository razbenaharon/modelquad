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
from modelquad.jobs import changes
from modelquad.service import lifecycle


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
    for name in ("tasklist", "taskkill", "schtasks"):
        _file(directory, name + ".exe")
        _file(isolated, name + ".exe")
    monkeypatch.setenv("PATH", str(isolated))
    monkeypatch.setenv("SystemRoot", str(isolated))
    monkeypatch.setenv("ComSpec", str(isolated / "cmd.exe"))
    monkeypatch.setattr(resolver, "_system_directory", lambda: str(directory))
    return directory


@win_only
@pytest.mark.parametrize("utility", ["tasklist", "taskkill"], ids=["tasklist", "taskkill"])
def test_tasklist_and_taskkill_come_from_the_system_directory(utility, monkeypatch, synthetic_system):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout='"python.exe","4242","Console","1","10 K"\n')

    monkeypatch.setattr(procutil.subprocess, "run", run)

    def invoke():
        return procutil.process_image_name(4242, run=run) if utility == "tasklist" else procutil._taskkill_tree(4242)

    assert invoke() == ("python.exe" if utility == "tasklist" else "ok")
    (argv, kwargs), = calls
    assert _canonical(argv[0]) == _canonical(synthetic_system / (utility + ".exe"))
    arguments = ["/FI", "PID eq 4242", "/FO", "CSV", "/NH"] if utility == "tasklist" else ["/PID", "4242", "/T", "/F"]
    assert argv[1:] == arguments
    expected = {"capture_output": True, "timeout": procutil.IMAGE_NAME_SECONDS if utility == "tasklist" else 10.0,
                **procutil.no_window_kwargs()}
    if utility == "tasklist":
        expected.update(text=True, shell=False)
    assert kwargs == expected

    # A service may itself start in the system directory; no re-admission.
    monkeypatch.chdir(synthetic_system)
    assert invoke() == ("python.exe" if utility == "tasklist" else "ok")
    assert _canonical(calls[-1][0][0]) == _canonical(synthetic_system / (utility + ".exe"))
    monkeypatch.setattr(resolver, "_system_directory", lambda: None)
    count = len(calls)
    assert invoke() == (None if utility == "tasklist" else "launch_error: UnsafeExecutable")
    assert len(calls) == count


def _scheduler_call(registrar, action, command, *, dry_run=False):
    if action == "query":
        return registrar.is_registered()
    if action == "create":
        return registrar.register(command=command, dry_run=dry_run)
    return registrar.unregister(command=command, dry_run=dry_run)


@pytest.fixture
def scheduler_context(tmp_path, monkeypatch):
    from modelquad import cli
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")
    monkeypatch.setenv("USERDOMAIN", "SYNTHETIC")
    monkeypatch.setenv("USERNAME", "Fixture")
    # Identity now comes from the OS token; environment identity is untrusted.
    monkeypatch.setattr("modelquad.service.auth.current_user_sid", lambda: "S-1-5-21-1")
    monkeypatch.setattr(lifecycle.tempfile, "tempdir", str(tmp_path))
    # Keep cwd validation independent of the schtasks-resolution fault below.
    # The real service_working_directory still validates this existing path.
    monkeypatch.setattr(lifecycle, "system_directory", lambda: str(tmp_path))
    command = [str(_file(tmp_path / "synthetic product", "service.exe")), "serve"]
    monkeypatch.setattr(cli, "_service_autostart_command", lambda port: command)
    return command


@win_only
@pytest.mark.parametrize("action", ["query", "create", "delete"], ids=["query", "create", "delete"])
def test_schtasks_comes_from_the_system_directory(action, monkeypatch, synthetic_system, scheduler_context):
    calls = []
    definitions = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "/query":
            if action == "create" and not definitions:
                return subprocess.CompletedProcess(argv, 1, stdout="",
                    stderr="ERROR: The system cannot find the file specified.")
            raw = definitions[-1] if definitions else lifecycle.TaskSchedulerRegistrar._task_definition_xml(
                scheduler_context, "S-1-5-21-1", cwd=lifecycle.service_working_directory())
            return subprocess.CompletedProcess(argv, 0, stdout=raw, stderr="")
        if argv[1] == "/create":
            definition = Path(argv[argv.index("/xml") + 1])
            definitions.append(definition.read_text(encoding="utf-16"))
            assert "<UserId>S-1-5-21-1</UserId>" in definitions[-1]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    registrar = lifecycle.TaskSchedulerRegistrar(run=run)
    result = _scheduler_call(registrar, action, scheduler_context)
    assert result.success is True
    assert result.registered is (action != "delete")
    # Uninstall checks ownership with the same query install uses, then deletes.
    expected_operations = {"create": ["/query", "/create", "/query"], "query": ["/query"],
                           "delete": ["/query", "/delete"]}[action]
    assert [argv[1] for argv, _ in calls] == expected_operations
    for actual, options in calls:
        assert _canonical(actual[0]) == _canonical(synthetic_system / "schtasks.exe")
        expected_options = {"capture_output": True, "text": True, "shell": False,
                            "timeout": lifecycle.WINDOWS_COMMAND_TIMEOUT_SECONDS,
                            **procutil.no_window_kwargs()}
        if actual[1] != "/delete":
            expected_options["cwd"] = lifecycle.service_working_directory()
        assert options == expected_options
    argv, kwargs = next(call for call in calls if call[0][1] == {
        "query": "/query", "create": "/create", "delete": "/delete"}[action])
    assert argv[1:] == list(result.command[1:])
    assert argv[1] == {"query": "/query", "create": "/create", "delete": "/delete"}[action]
    assert result.to_dict()["command"][0] == "schtasks"
    assert str(synthetic_system) not in result.message
    assert str(synthetic_system) not in repr(result.to_dict())
    if action == "create":
        assert not Path(argv[argv.index("/xml") + 1]).exists()

    monkeypatch.setattr(resolver, "_system_directory", lambda: None)
    count = len(calls)
    refused = _scheduler_call(registrar, action, scheduler_context)
    assert len(calls) == count
    assert refused.success is False and refused.registered is None
    assert refused.to_dict()["command"][0] == "schtasks"
    # Every action, uninstall included, starts with the ownership query.
    assert refused.message == lifecycle.SERVICE_DETAILS["background_startup_unknown"]
    assert str(synthetic_system) not in repr(refused.to_dict())


@pytest.mark.parametrize("action", ["create", "delete"], ids=["create", "delete"])
def test_scheduler_dry_run_never_resolves(action, monkeypatch, scheduler_context):
    def forbidden(*args, **kwargs):
        pytest.fail("A dry run must not resolve or launch schtasks")

    monkeypatch.setattr(lifecycle, "system_executable", forbidden)
    result = _scheduler_call(lifecycle.TaskSchedulerRegistrar(run=forbidden), action, scheduler_context, dry_run=True)
    assert result.success is True and result.dry_run is True
    assert result.to_dict()["command"][0] == "schtasks"


@pytest.mark.parametrize("action", ["query", "create", "delete"], ids=["query", "create", "delete"])
def test_unsupported_scheduler_never_resolves(action, monkeypatch, scheduler_context):
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    def forbidden(*args, **kwargs):
        pytest.fail("An unsupported scheduler must not resolve or launch schtasks")

    monkeypatch.setattr(lifecycle, "system_executable", forbidden)
    result = _scheduler_call(lifecycle.TaskSchedulerRegistrar(run=forbidden), action, scheduler_context)
    assert result.supported is False and result.success is False and result.registered is None


@win_only
@pytest.mark.parametrize("action", ["query", "create", "delete"], ids=["query", "create", "delete"])
def test_scheduler_launch_error_does_not_publish_the_executable_path(action, synthetic_system, scheduler_context):
    def run(argv, **kwargs):
        raise OSError("cannot launch " + argv[0])

    result = _scheduler_call(lifecycle.TaskSchedulerRegistrar(run=run), action, scheduler_context)
    assert result.success is False and result.registered is None
    assert result.to_dict()["command"][0] == "schtasks"
    assert result.message == lifecycle.SERVICE_DETAILS["background_startup_unknown"]
    assert str(synthetic_system) not in repr(result.to_dict())
