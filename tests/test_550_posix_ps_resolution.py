# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""POSIX ps launch boundaries simulated on either host; no files are executed."""

from __future__ import annotations

import posixpath
import stat
import subprocess
from types import SimpleNamespace

import pytest

from modelquad import executable_resolution as resolver, procutil


@pytest.fixture(params=["process_image_name", "process_creation_epoch", "list_descendants"])
def site(request):
    return request.param


@pytest.fixture
def posix(monkeypatch):
    # Replace only the modules' OS views, not global os.name/pathlib. The
    # resolver itself runs unchanged against a synthetic POSIX filesystem.
    current = "/repository"
    files = {current + "/ps", current + "/relative/ps", "/installed/bin/ps"}
    environment = {"PATH": ""}

    def canonical(path):
        return posixpath.normpath(posixpath.join(current, path))

    def details(path):
        if canonical(path) not in files:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o755)

    path = SimpleNamespace(
        **{name: getattr(posixpath, name) for name in
           ("isabs", "join", "dirname", "basename", "normcase", "commonpath")},
        realpath=canonical,
    )
    view = SimpleNamespace(name="posix", path=path, pathsep=":", environ=environment,
                           getcwd=lambda: current, fspath=str, stat=details)
    monkeypatch.setattr(resolver, "os", view)
    monkeypatch.setattr(procutil, "os", view)
    return environment


class RecordingRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "comm=" in argv:
            output = "/usr/bin/python3\n"
        elif "etime=" in argv:
            output = "00:05\n"
        else:
            output = "4242 4242 python3\n4300 4242 /usr/bin/node\n9999 9999 bash\n"
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")


def _probe(site, runner):
    kwargs = {"run": runner}
    if site == "list_descendants":
        kwargs["process_group"] = True
    return getattr(procutil, site)(4242, **kwargs)


@pytest.mark.parametrize("search", [
    pytest.param("/installed/bin", id="planted-cwd-not-on-path"),
    pytest.param(":/installed/bin", id="empty-before-safe"),
    pytest.param(".:/installed/bin", id="dot-before-safe"),
    pytest.param("relative:/installed/bin", id="relative-before-safe"),
    pytest.param("/repository:/installed/bin", id="absolute-cwd-before-safe"),
    pytest.param(":.:relative:/repository:/installed/bin", id="all-unsafe-before-safe"),
])
def test_ps_launches_only_the_admitted_absolute_path(site, posix, search):
    posix["PATH"] = search
    runner = RecordingRun()

    result = _probe(site, runner)

    if site == "process_image_name":
        assert result == "python3"
        arguments = [["-p", "4242", "-o", "comm="]]
    elif site == "process_creation_epoch":
        assert isinstance(result, float)
        arguments = [["-p", "4242", "-o", "etime="]]
    else:
        assert len(result) == 1
        assert result[0]["pid"] == 4300 and result[0]["image"] == "node"
        assert isinstance(result[0]["created"], float)
        arguments = [["-e", "-o", "pid=,pgid=,comm="],
                     ["-p", "4300", "-o", "etime="]]
    assert [argv for argv, _ in runner.calls] == [["/installed/bin/ps", *args] for args in arguments]
    for _, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 15.0
        assert "creationflags" not in kwargs
        if site != "process_image_name":
            assert kwargs["env"]["LC_ALL"] == "C"


@pytest.mark.parametrize("search", [
    pytest.param("", id="empty-only"),
    pytest.param(".", id="dot-only"),
    pytest.param("relative", id="relative-only"),
    pytest.param("/repository", id="absolute-cwd-only"),
    pytest.param(":.:relative:/repository", id="unsafe-only"),
    pytest.param("/missing", id="absent"),
])
def test_ps_unsafe_or_absent_candidates_are_unknown_without_launch(site, posix, search):
    posix["PATH"] = search
    runner = RecordingRun()

    assert _probe(site, runner) is None
    assert runner.calls == []


@pytest.mark.parametrize("reason", ["cwd_unknown", "path_unreadable"])
def test_each_ps_site_preserves_unknown_on_resolver_refusal(site, posix, monkeypatch, reason):
    posix["PATH"] = "/installed/bin"
    names = []

    def refuse(name):
        names.append(name)
        return resolver.Resolution(None, "path", None, reason)

    monkeypatch.setattr(procutil, "resolve_executable", refuse, raising=False)
    runner = RecordingRun()

    assert _probe(site, runner) is None
    assert names == ["ps"]
    assert runner.calls == []
